"""Delivery checkpoint tests: XA prepare/commit across two sources."""

import json
import os
import tempfile
import unittest

from pymysqlreplication.delivery import (
    DeliveryCheckpoint,
    MirrorManifest,
    TransactionIdentity,
    XAIdentity,
    parse_xa_query,
)

SID = "11111111-2222-3333-4444-555555555555"


def gtid(number):
    return "%s:%s" % (SID, number)


def update_row(value=2, key=1):
    return {
        "kind": "update",
        "schema": "warehouse",
        "table": "orders",
        "primary_key": key,
        "before": {"id": key, "amount": value - 1},
        "after": {"id": key, "amount": value},
    }


def insert_row(value=10, key=2):
    return {
        "kind": "insert",
        "schema": "warehouse",
        "table": "orders",
        "primary_key": key,
        "values": {"id": key, "amount": value},
    }


class XAQueryParserTest(unittest.TestCase):
    def test_hex_serialized_xid(self):
        command, identity, one_phase = parse_xa_query("XA COMMIT 0x78,0x,1")
        self.assertEqual(command, "COMMIT")
        self.assertEqual((identity.gtrid, identity.bqual), ("x", ""))
        self.assertEqual(identity.format_id, 1)
        self.assertFalse(one_phase)

    def test_quoted_xid_and_one_phase(self):
        command, identity, one_phase = parse_xa_query(
            "XA COMMIT 'gtrid', 'bqual', 42 ONE PHASE"
        )
        self.assertEqual(command, "COMMIT")
        self.assertEqual(identity.gtrid, "gtrid")
        self.assertEqual(identity.bqual, "bqual")
        self.assertEqual(identity.format_id, 42)
        self.assertTrue(one_phase)


class CrossSourceXATest(unittest.TestCase):
    def test_prepare_rows_survive_commit_on_other_source(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6),
            xid=("order-1", "", 1),
            rows=[update_row()],
            source="A",
        )
        self.assertTrue(prepared.is_prepared_only)
        # A marks the prepared GTID as read (its real XA boundary).
        self.assertEqual(checkpoint.stream_read_gtids("A"), {gtid(6)})

        checkpoint.switch_source("B", resume_gtid=gtid(7))
        committed = checkpoint.record_commit(
            gtid=gtid(7), xid=("order-1", "", 1), source="B"
        )
        self.assertEqual(committed.group_id, prepared.group_id)
        self.assertEqual(committed.outcome, "committed")
        self.assertEqual(len(committed.rows), 1)

        published = checkpoint.publish()
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].prepare_gtid, gtid(6))
        self.assertEqual(published[0].final_gtid, gtid(7))
        self.assertEqual(len(published[0].rows), 1)
        self.assertTrue(checkpoint.verify()["ok"])

    def test_prepared_rows_remain_when_other_source_dropped_segment(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.switch_source("B", resume_gtid=gtid(7))
        # B no longer has the prepare segment; its checkpoint must not need
        # it because A's rows stay in the local delivery store.
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        self.assertEqual(len(checkpoint.publish()[0].rows), 1)

    def test_prepare_and_commit_rows_deduplicate_across_sources(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared_rows = [update_row(2, key=1), update_row(4, key=2)]
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=prepared_rows, source="A"
        )
        checkpoint.record_commit(
            gtid=gtid(7),
            xid=("order-1", "", 1),
            rows=list(prepared_rows),
            source="B",
        )
        published = checkpoint.publish()
        self.assertEqual(len(published[0].rows), 2)
        self.assertEqual([row.primary_key for row in published[0].rows], [1, 2])

    def test_second_publish_is_idempotent(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        self.assertEqual(len(checkpoint.publish()), 1)
        self.assertEqual(checkpoint.publish(), [])
        self.assertEqual(checkpoint.publication_count(), 1)
        self.assertEqual(len(checkpoint.published_transactions[0].rows), 1)

    def test_reopen_keeps_prepared_and_published_state(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "delivery.json")
        checkpoint = DeliveryCheckpoint(path=path, default_source="A", autosave=True)
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.switch_source("B", resume_gtid=gtid(7))
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        checkpoint.publish()

        reopened = DeliveryCheckpoint.load(path)
        self.assertEqual(reopened.publication_count(), 1)
        self.assertEqual(reopened.publish(), [])
        self.assertTrue(reopened.verify()["ok"])
        self.assertEqual(reopened.resume_plan()["gtid"], gtid(7))
        self.assertEqual(reopened.resume_plan()["sources"]["A"], gtid(6))

    def test_prepared_only_group_is_queryable_and_retained(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        status = checkpoint.status()
        self.assertEqual(status["prepared_only"][0]["prepare_gtid"], gtid(6))
        self.assertEqual(len(status["prepared_only"][0]["rows"]), 1)
        self.assertEqual(len(checkpoint.final_rows()), 0)
        self.assertEqual(len(checkpoint.prepared_only_rows()), 1)


class StreamReadVersusTargetTest(unittest.TestCase):
    def test_stream_read_does_not_confirm_target(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        # Both GTIDs are stream-read...
        self.assertEqual(checkpoint.stream_read_gtids("A"), {gtid(6)})
        self.assertEqual(checkpoint.stream_read_gtids("B"), {gtid(7)})
        # ...but neither is a target confirmation.
        self.assertEqual(checkpoint.target_confirmed_gtids(), set())
        checkpoint.publish()
        group = checkpoint.published_transactions[0]
        self.assertFalse(group.target_confirmed)

    def test_confirm_only_final_group_and_single_receipt(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        self.assertFalse(
            checkpoint.confirm_target(key=prepared.group_id, receipt="early")
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        self.assertTrue(checkpoint.confirm_target(gtid=gtid(7), receipt="write-1"))
        self.assertTrue(checkpoint.confirm_target(gtid=gtid(7), receipt="write-1"))
        self.assertFalse(checkpoint.confirm_target(gtid=gtid(7), receipt="write-2"))
        self.assertEqual(checkpoint.target_confirmed_gtids(), {gtid(6), gtid(7)})

    def test_manifest_can_supply_same_group_evidence(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        checkpoint.apply_manifest(
            {
                "source": "B",
                "groups": [{"group_id": "b-group-1", "gtids": [gtid(6), gtid(7)]}],
            },
            source="B",
        )
        evidence = checkpoint.same_group_evidence(prepared.group_id)
        self.assertTrue(evidence["stream_confirmed"])
        self.assertFalse(evidence["target_confirmed"])
        self.assertEqual(evidence["manifest_groups"], ["B", "b-group-1"])

    def test_pending_write_ack_survives_source_switch(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.switch_source(
            "B", resume_gtid=gtid(7), pending_write_ack=prepared.group_id
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="B")
        # Reading the same transaction on B did not settle the write.
        self.assertEqual(
            checkpoint.resume_plan()["pending_target_acks"], [prepared.group_id]
        )
        checkpoint.confirm_target(key=prepared.group_id, receipt="write-1")
        self.assertEqual(checkpoint.resume_plan()["pending_target_acks"], [])


class RollbackAndReuseTest(unittest.TestCase):
    def test_rolled_back_group_publishes_no_rows(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        rolled_back = checkpoint.record_rollback(
            gtid=gtid(7), xid=("order-1", "", 1), source="A"
        )
        self.assertEqual(rolled_back.group_id, prepared.group_id)
        published = checkpoint.publish()
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].outcome, "rolled_back")
        self.assertEqual(published[0].rows, [])
        self.assertEqual(len(checkpoint.final_rows(outcome="rolled_back")), 0)
        self.assertFalse(
            checkpoint.confirm_target(key=prepared.group_id, receipt="late-ack")
        )

    def test_reused_xa_name_creates_new_generation(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        first = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="A")
        self.assertTrue(checkpoint.confirm_target(key=first.group_id, receipt="old"))

        second = checkpoint.record_prepare(
            gtid=gtid(12), xid=("order-1", "", 1), rows=[update_row(5)], source="A"
        )
        self.assertNotEqual(second.group_id, first.group_id)
        checkpoint.record_commit(gtid=gtid(13), xid=("order-1", "", 1), source="A")
        self.assertFalse(checkpoint.confirm_target(key=second.group_id, receipt="old"))
        self.assertTrue(checkpoint.confirm_target(key=second.group_id, receipt="new"))
        self.assertTrue(
            any(
                rejection["reason"] == "receipt belongs to another transaction attempt"
                for rejection in checkpoint.target_rejections
            )
        )

    def test_ambiguous_reused_name_cannot_take_old_receipt_by_name(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        first = checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        second = checkpoint.record_prepare(
            gtid=gtid(8), xid=("order-1", "", 1), rows=[update_row(4)], source="A"
        )
        checkpoint.record_commit(gtid=gtid(7), xid=("order-1", "", 1), source="A")
        checkpoint.record_commit(gtid=gtid(9), xid=("order-1", "", 1), source="A")
        checkpoint.confirm_target(key=first.group_id, receipt="old")
        self.assertFalse(checkpoint.confirm_target(key=second.group_id, receipt="old"))
        self.assertFalse(checkpoint.confirm_target(key=second.group_id, receipt="new"))
        # Both commits were ambiguous: the XA name matched two prepared
        # generations, so neither may silently take either generation's
        # receipt.  The evidence is retained for a caller that can supply a
        # stronger identity.
        self.assertTrue(
            any(
                group.blocked_reason == "ambiguous XA identity"
                for group in checkpoint.blocked_groups()
            )
        )

    def test_effect_identity_supplied_by_test_source(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        identity = TransactionIdentity(
            key="test-effect-1",
            prepare_gtid=gtid(6),
            xid=XAIdentity(format_id=1, gtrid="order-1", bqual=""),
            basis="source-side transaction id",
        )
        prepared = checkpoint.record_prepare(
            gtid=gtid(6),
            rows=[update_row()],
            identity=identity,
            source="A",
        )
        self.assertEqual(prepared.group_id, "test-effect-1")
        self.assertEqual(prepared.match_basis, "source-side transaction id")
        committed = checkpoint.record_commit(
            gtid=gtid(7),
            identity=TransactionIdentity(
                key="test-effect-1",
                prepare_gtid=gtid(6),
                xid=XAIdentity(format_id=1, gtrid="order-1", bqual=""),
                basis="source-side transaction id",
            ),
            source="B",
        )
        self.assertEqual(committed.group_id, "test-effect-1")
        self.assertEqual(committed.outcome, "committed")


class TailOnlyManifestTest(unittest.TestCase):
    def test_tail_only_blocks_only_its_transaction(self):
        checkpoint = DeliveryCheckpoint(default_source="B")
        checkpoint.apply_manifest(
            {
                "source": "B",
                "tail_only": True,
                "groups": [{"group_id": "tail-1", "gtids": [gtid(9)]}],
            },
            source="B",
        )
        blocked = checkpoint.record_commit(
            gtid=gtid(9), xid=("lost-prepare", "", 1), source="B"
        )
        self.assertTrue(blocked.missing_rows)
        self.assertIsNotNone(blocked.blocked_reason)

        checkpoint.record_prepare(
            gtid=gtid(10),
            xid=("known", "", 1),
            rows=[insert_row()],
            source="B",
        )
        checkpoint.record_commit(gtid=gtid(11), xid=("known", "", 1), source="B")
        published = checkpoint.publish()
        self.assertEqual(len(published), 1)
        self.assertEqual(len(published[0].rows), 1)
        self.assertIn(
            blocked.group_id, [g.group_id for g in checkpoint.blocked_groups()]
        )
        self.assertTrue(checkpoint.verify()["ok"])

    def test_tail_only_manifest_never_supplies_prepare_content(self):
        checkpoint = DeliveryCheckpoint(default_source="B")
        manifest = MirrorManifest(
            source="B",
            tail_only=True,
            groups=[
                {
                    "group_id": "tail-1",
                    "gtids": [gtid(9)],
                    "has_prepare_content": True,
                    "prepare_rows": [update_row()],
                }
            ],
        )
        checkpoint.apply_manifest(manifest, source="B")
        self.assertIsNone(manifest.prepare_content(XAIdentity(gtrid="lost")))
        group = checkpoint.record_commit(gtid=gtid(9), xid=("lost", "", 1), source="B")
        self.assertTrue(group.missing_rows)
        self.assertEqual(checkpoint.publish(), [])

    def test_complete_manifest_may_supply_prepare_content(self):
        checkpoint = DeliveryCheckpoint(default_source="B")
        checkpoint.apply_manifest(
            {
                "source": "B",
                "groups": [
                    {
                        "group_id": "full-1",
                        "gtids": [gtid(6), gtid(7)],
                        "prepare_gtid": gtid(6),
                        "has_prepare_content": True,
                        "prepare_rows": [update_row()],
                    }
                ],
            },
            source="B",
        )
        group = checkpoint.record_commit(gtid=gtid(7), xid=("x", "", 1), source="B")
        self.assertTrue(group.prepare_seen)
        self.assertFalse(group.missing_rows)
        self.assertEqual(len(checkpoint.publish()), 1)
        self.assertEqual(len(checkpoint.published_transactions[0].rows), 1)

    def test_missing_ranges_are_preserved_for_resume(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_missing_range("A", gtid(4), gtid(5), "segment rotated away")
        checkpoint.record_missing_range("B", gtid(8), None, "tail starts here")
        plan = checkpoint.resume_plan()
        self.assertEqual(len(plan["missing_ranges"]), 2)
        self.assertEqual(plan["missing_ranges"][0]["reason"], "segment rotated away")
        self.assertTrue(checkpoint.verify()["missing_ranges"])


class NormalTransactionTest(unittest.TestCase):
    def test_non_xa_commit_publishes_once(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        group = checkpoint.record_commit(gtid=gtid(3), rows=[insert_row()], source="A")
        self.assertFalse(group.requires_prepare)
        self.assertTrue(group.publishable)
        self.assertEqual(len(checkpoint.publish()), 1)
        self.assertEqual(checkpoint.publish(), [])

    def test_transaction_identity_defaults_to_gtid(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        group = checkpoint.record_commit(gtid=gtid(3), rows=[insert_row()], source="A")
        self.assertEqual(group.group_id, "txn:%s" % gtid(3))

    def test_statement_inside_begin_is_not_a_transaction_boundary(self):
        from pymysqlreplication.event import GtidEvent, QueryEvent, XidEvent

        checkpoint = DeliveryCheckpoint(default_source="A")

        def gtid_event(number):
            event = GtidEvent.__new__(GtidEvent)
            event.sid = bytes.fromhex("11111111222233334444555555555555")
            event.gno = number
            return event

        def query(text):
            event = QueryEvent.__new__(QueryEvent)
            event.query = text
            return event

        checkpoint.observe_event(gtid_event(3), source="A")
        checkpoint.observe_event(query("BEGIN"), source="A")
        checkpoint.observe_event(query("INSERT INTO t VALUES (1)"), source="A")
        # No group was published and the GTID was not yet treated as done.
        self.assertEqual(checkpoint.groups, {})
        self.assertEqual(checkpoint.stream_read_gtids("A"), set())
        checkpoint.observe_event(XidEvent.__new__(XidEvent), source="A")
        self.assertEqual(checkpoint.stream_read_gtids("A"), {gtid(3)})

    def test_single_statement_group_is_a_boundary(self):
        from pymysqlreplication.event import GtidEvent, QueryEvent

        checkpoint = DeliveryCheckpoint(default_source="A")
        event = GtidEvent.__new__(GtidEvent)
        event.sid = bytes.fromhex("11111111222233334444555555555555")
        event.gno = 4
        checkpoint.observe_event(event, source="A")
        ddl = QueryEvent.__new__(QueryEvent)
        ddl.query = "CREATE TABLE t (id int)"
        checkpoint.observe_event(ddl, source="A")
        self.assertEqual(checkpoint.stream_read_gtids("A"), {gtid(4)})
        self.assertEqual(len(checkpoint.publish()), 1)

    def test_manifest_placeholder_merges_with_real_prepare(self):
        checkpoint = DeliveryCheckpoint(default_source="B")
        checkpoint.apply_manifest(
            {
                "source": "B",
                "groups": [
                    {
                        "group_id": "full-1",
                        "gtids": [gtid(6), gtid(7)],
                        "prepare_gtid": gtid(6),
                        "prepare_rows": [update_row()],
                    }
                ],
            },
            source="B",
        )
        checkpoint.switch_source("A")
        prepared = checkpoint.record_prepare(
            gtid=gtid(6), xid=("x", "", 1), rows=[update_row()], source="A"
        )
        self.assertEqual(len(checkpoint.groups), 1)
        self.assertEqual(len(prepared.rows), 1)
        checkpoint.record_commit(gtid=gtid(7), xid=("x", "", 1), source="A")
        self.assertEqual(len(checkpoint.publish()), 1)

    def test_one_phase_commit_needs_no_prepare(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        group = checkpoint.record_commit(
            gtid=gtid(5),
            xid=("one", "", 1),
            rows=[insert_row()],
            one_phase=True,
            source="A",
        )
        self.assertTrue(group.is_final)
        self.assertFalse(group.requires_prepare)
        self.assertEqual(len(checkpoint.publish()), 1)

    def test_ambiguous_confirmation_key_is_rejected(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(1), xid=("same", "", 1), rows=[update_row()]
        )
        checkpoint.record_prepare(
            gtid=gtid(2), xid=("same", "", 1), rows=[update_row(4)]
        )
        self.assertFalse(
            checkpoint.confirm_target(identity=("same", "", 1), receipt="r")
        )
        self.assertEqual(
            checkpoint.target_rejections[-1]["reason"], "unknown transaction"
        )
        self.assertEqual(checkpoint._ambiguous[-1]["phase"], "target-confirmation")

    def test_missing_receipt_is_rejected(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_commit(gtid=gtid(5), rows=[insert_row()], source="A")
        self.assertFalse(checkpoint.confirm_target(gtid=gtid(5)))
        self.assertEqual(
            checkpoint.target_rejections[-1]["reason"], "missing target receipt"
        )


class PackageTest(unittest.TestCase):
    def test_package_exports_checkpoint(self):
        from pymysqlreplication import DeliveryCheckpoint as Exported

        self.assertIs(Exported, DeliveryCheckpoint)

    def test_checkpoint_is_json_serialisable(self):
        checkpoint = DeliveryCheckpoint(default_source="A")
        checkpoint.record_prepare(
            gtid=gtid(6), xid=("order-1", "", 1), rows=[update_row()], source="A"
        )
        text = json.dumps(checkpoint.to_dict(), default=str)
        self.assertIn("order-1", text)


if __name__ == "__main__":
    unittest.main()
