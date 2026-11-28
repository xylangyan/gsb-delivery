import threading
import unittest

from datasketch import (
    BlockedEntry,
    DocumentVersion as DV,
    RetirementBlockedError,
    SegmentRolloverManager,
    ShardIndex,
    TokenAccessDeniedError,
)

THRESHOLD = 0.5
OLD_PERMS = 128
NEW_PERMS = 64


def build_manager():
    manager = SegmentRolloverManager(threshold=THRESHOLD)
    manager.add_segment(ShardIndex("S", "old", THRESHOLD, OLD_PERMS))
    manager.add_segment(ShardIndex("T", "old", THRESHOLD, OLD_PERMS))
    manager.add_segment(ShardIndex("N", "new", THRESHOLD, NEW_PERMS))
    return manager


class TestSegmentRolloverScenario(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.a1 = DV("A", 1)
        self.b1 = DV("B", 1)
        self.b2 = DV("B", 2)
        self.c1 = DV("C", 1)
        self.d1 = DV("D", 1)

        # Raw token shards and independent supporting sources.
        self.manager.register_document(self.a1, {"a", "b"}, {"s1", "s2"})
        self.manager.register_document(self.b1, {"a", "b", "c", "d"}, {"s1", "s2"})
        self.manager.register_document(self.c1, {"c", "d"}, {"s1", "s2"})
        self.manager.register_document(self.d1, {"d", "e"}, {"s3"})

        # Old segments: S holds A/C/D/B-v1, T holds B-v1.
        for segment_name, entries in (
            ("S", (self.a1, self.b1, self.c1, self.d1)),
            ("T", (self.b1,)),
        ):
            for document_version in entries:
                self.manager.index_insert(
                    segment_name,
                    document_version,
                    self.manager._vault._tokens[document_version],
                )

        # New generation already covers A, C and D.
        for document_version in (self.a1, self.c1, self.d1):
            self.manager.index_insert(
                "N", document_version, self.manager._vault._tokens[document_version]
            )

        issuance_permit = frozenset((self.a1, self.b1, self.c1))
        confirm_ab = self.manager.confirm_tokens(self.a1, self.b1, "s1", issuance_permit)
        confirm_cb = self.manager.confirm_tokens(self.c1, self.b1, "s1", issuance_permit)
        self.assertTrue(confirm_ab.meets_threshold)
        self.assertTrue(confirm_cb.meets_threshold)

        # Historical group G is served by both S and T; G2 (D only) by S.
        self.manager.register_historical_group(
            "G", ("S", "T"), (self.a1, self.b1, self.c1), (confirm_ab, confirm_cb)
        )
        self.manager.register_historical_group("G2", ("S",), (self.d1,), ())

    def test_prepare_plans_list_successor_locations(self):
        permit = frozenset((self.a1, self.b1, self.c1, self.d1))
        plan_s = self.manager.prepare_retirement("S", permit)
        plan_t = self.manager.prepare_retirement("T", permit)

        self.assertTrue(plan_s.ready)
        self.assertEqual(plan_s.placements["G"][self.a1], "N")
        self.assertEqual(plan_s.placements["G"][self.b1], "T")
        self.assertEqual(plan_s.placements["G"][self.c1], "N")
        self.assertEqual(plan_s.placements["G2"][self.d1], "N")

        self.assertTrue(plan_t.ready)
        self.assertEqual(plan_t.placements["G"][self.b1], "S")

    def test_raw_token_access_requires_permit(self):
        with self.assertRaises(TokenAccessDeniedError):
            self.manager.confirm_tokens(self.a1, self.b1, "s1", frozenset((self.a1,)))
        with self.assertRaises(TokenAccessDeniedError):
            self.manager.review_for_new_index(self.a1, frozenset())
        # Preparation without any permit still succeeds: it reads no raw shards.
        plan_s = self.manager.prepare_retirement("S", frozenset())
        self.assertTrue(plan_s.ready)

    def test_publish_s_succeeds_then_t_blocked_on_b_v2(self):
        permit = frozenset((self.a1, self.b1, self.c1, self.d1))
        self.manager.prepare_retirement("S", permit)
        self.manager.prepare_retirement("T", permit)

        # B v1 loses every independent support; B v2 arrives as a correction.
        self.manager.revoke_support(self.b1, "s1")
        self.manager.revoke_support(self.b1, "s2")
        self.assertFalse(self.manager.is_valid(self.b1))

        self.manager.apply_correction(self.b2, {"a", "b", "e", "f"}, {"s2", "s3"})
        for segment_name in ("S", "T"):
            self.manager.index_insert(
                segment_name, self.b2, self.manager._vault._tokens[self.b2]
            )

        correction_permit = frozenset((self.a1, self.b2, self.c1))
        confirm_ab2 = self.manager.confirm_tokens(self.a1, self.b2, "s2", correction_permit)
        confirm_cb2 = self.manager.confirm_tokens(self.c1, self.b2, "s2", correction_permit)
        self.assertTrue(confirm_ab2.meets_threshold)
        self.assertFalse(confirm_cb2.meets_threshold)

        view_before = self.manager.snapshot()
        view_after_s = self.manager.publish(self.manager.prepare_retirement("S", permit))
        self.assertNotIn("S", view_after_s.online_segments)
        self.assertIn("T", view_after_s.online_segments)

        # A query already running keeps the routing of its captured published view.
        self.assertIn("S", view_before.online_segments)
        self.assertEqual(view_before.routing[self.b1], ("S", "T"))

        plan_t = self.manager.prepare_retirement("T", permit)
        self.assertFalse(plan_t.ready)
        with self.assertRaises(RetirementBlockedError) as context:
            self.manager.publish(plan_t)
        self.assertEqual(context.exception.segment, "T")
        self.assertEqual(
            context.exception.blocked,
            [
                BlockedEntry(
                    group_id="G",
                    document_version=self.b2,
                    reason="no queryable entry in new index or surviving old segment",
                )
            ],
        )

        # T stays online and still serves B v2.
        view = self.manager.snapshot()
        self.assertIn("T", view.online_segments)
        self.assertEqual(view.locations(self.b2), ("T",))

        # Historical lookup by old group id keeps the issuance-time snapshot.
        issuance = self.manager.historical_group(view, "G")
        self.assertEqual(issuance.members, frozenset((self.a1, self.b1, self.c1)))
        self.assertEqual(len(issuance.confirmations), 2)

        # Current groups: A with B v2; C stands alone; D stays its own group.
        components, edges = self.manager.current_groups(view)
        self.assertIn(frozenset((self.a1, self.b2)), components)
        self.assertIn(frozenset((self.c1,)), components)
        self.assertIn(frozenset((self.d1,)), components)
        self.assertEqual({(edge.left, edge.right) for edge in edges}, {(self.a1, self.b2)})

    def test_t_can_retire_after_in_permit_review_promotes_b_v2(self):
        permit = frozenset((self.a1, self.b1, self.c1, self.d1))
        self.manager.publish(self.manager.prepare_retirement("S", permit))

        self.manager.revoke_support(self.b1, "s1")
        self.manager.revoke_support(self.b1, "s2")
        self.manager.apply_correction(self.b2, {"a", "b", "e", "f"}, {"s2", "s3"})
        self.manager.index_insert("T", self.b2, self.manager._vault._tokens[self.b2])

        # Promotion needs an in-permit raw token review; no permit at publish time.
        with self.assertRaises(TokenAccessDeniedError):
            self.manager.review_for_new_index(self.b2, frozenset())
        self.manager.review_for_new_index(self.b2, frozenset((self.b2,)))

        plan_t = self.manager.prepare_retirement("T", permit)
        self.assertTrue(plan_t.ready)
        view = self.manager.publish(plan_t)
        self.assertEqual(view.online_segments, frozenset(("N",)))
        self.assertEqual(view.locations(self.b2), ("N",))

    def test_unrelated_change_keeps_prepared_retirement_publishable(self):
        permit = frozenset((self.a1, self.b1, self.c1, self.d1))
        self.manager.publish(self.manager.prepare_retirement("S", permit))
        self.manager.revoke_support(self.b1, "s1")
        self.manager.revoke_support(self.b1, "s2")
        self.manager.apply_correction(self.b2, {"a", "b", "e", "f"}, {"s2", "s3"})
        self.manager.index_insert("T", self.b2, self.manager._vault._tokens[self.b2])
        self.manager.review_for_new_index(self.b2, frozenset((self.b2,)))

        plan_t = self.manager.prepare_retirement("T", permit)

        # A correction touching only G2 (unrelated to the groups T serves).
        d2 = DV("D", 2)
        self.manager.apply_correction(d2, {"d", "e", "g"}, {"s3"})
        self.manager.index_insert("N", d2, self.manager._vault._tokens[d2])

        view = self.manager.publish(plan_t)
        self.assertNotIn("T", view.online_segments)

    def test_segment_serving_two_groups_cannot_retire_on_one_covered(self):
        # Remove D from the new index: G2 would lose its only entry point if S goes.
        new_index = self.manager._segments["N"]
        new_index._entries.discard(self.d1)
        permit = frozenset((self.a1, self.b1, self.c1, self.d1))
        plan_s = self.manager.prepare_retirement("S", permit)
        self.assertFalse(plan_s.ready)
        self.assertEqual(plan_s.blocked[0].group_id, "G2")
        self.assertEqual(plan_s.blocked[0].document_version, self.d1)
        with self.assertRaises(RetirementBlockedError):
            self.manager.publish(plan_s)
        self.assertIn("S", self.manager.snapshot().online_segments)


class TestConfirmationAndRecallRules(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.x1 = DV("X", 1)
        self.y1 = DV("Y", 1)
        self.z1 = DV("Z", 1)

    def test_historical_confirmation_survives_lsh_miss(self):
        self.manager.register_document(self.x1, {"a", "b"}, {"s1"})
        self.manager.register_document(self.y1, {"a", "b"}, {"s1"})
        self.manager.index_insert("S", self.x1, {"a", "b"})
        self.manager.index_insert("T", self.y1, {"a", "b"})
        confirmation = self.manager.confirm_tokens(
            self.x1, self.y1, "s1", frozenset((self.x1, self.y1))
        )
        self.manager.register_historical_group(
            "G", ("S", "T"), (self.x1, self.y1), (confirmation,)
        )

        view = self.manager.snapshot()
        # Simulate LSH failing to recall the pair; the confirmation still groups them.
        for segment in view.segment_refs.values():
            segment.query = lambda minhash: []
        components, edges = self.manager.current_groups(view)
        self.assertEqual(components, [frozenset((self.x1, self.y1))])
        self.assertFalse(edges[0].recalled)

    def test_recalled_pair_without_token_confirmation_is_not_related(self):
        self.manager.register_document(self.x1, {"a", "b"}, {"s1"})
        self.manager.register_document(self.z1, {"a", "b"}, {"s1"})
        self.manager.index_insert("S", self.x1, {"a", "b"})
        self.manager.index_insert("S", self.z1, {"a", "b"})
        self.manager.register_historical_group("G", ("S",), (self.x1, self.z1), ())

        view = self.manager.snapshot()
        recalled = self.manager.recall_candidates(view, self.x1)
        self.assertIn(self.z1, recalled["S"])

        components, edges = self.manager.current_groups(view)
        self.assertEqual(set(components), {frozenset((self.x1,)), frozenset((self.z1,))})
        self.assertEqual(edges, [])

    def test_sources_keep_copies_and_confirmations_until_last_revocation(self):
        self.manager.register_document(self.x1, {"a", "b"}, {"s1", "s2"})
        self.manager.register_document(self.y1, {"a", "b"}, {"s1", "s2"})
        self.manager.index_insert("S", self.x1, {"a", "b"})
        self.manager.index_insert("S", self.y1, {"a", "b"})
        confirmation = self.manager.confirm_tokens(
            self.x1, self.y1, "s1", frozenset((self.x1, self.y1))
        )
        self.manager.register_historical_group(
            "G", ("S",), (self.x1, self.y1), (confirmation,)
        )

        self.manager.revoke_support(self.x1, "s2")
        self.assertTrue(self.manager.is_valid(self.x1))
        components, _ = self.manager.current_groups(self.manager.snapshot())
        self.assertEqual(components, [frozenset((self.x1, self.y1))])

        self.manager.revoke_support(self.x1, "s1")
        self.assertFalse(self.manager.is_valid(self.x1))
        components, edges = self.manager.current_groups(self.manager.snapshot())
        self.assertEqual(components, [frozenset((self.y1,))])
        self.assertEqual(edges, [])

    def test_generations_are_queried_with_their_own_sketches(self):
        self.manager.register_document(self.x1, {"a", "b"}, {"s1"})
        self.manager.index_insert("S", self.x1, {"a", "b"})
        self.manager.index_insert("N", self.x1, {"a", "b"})
        view = self.manager.snapshot()

        candidates = self.manager.recall_candidates(view, self.x1)
        self.assertEqual(set(candidates), {"S", "T", "N"})
        self.assertEqual(
            set(self.manager._sketches[self.x1]), {OLD_PERMS, NEW_PERMS}
        )

    def test_snapshot_stable_while_another_publish_runs(self):
        self.manager.register_document(self.x1, {"a", "b"}, {"s1"})
        self.manager.index_insert("S", self.x1, {"a", "b"})
        self.manager.index_insert("N", self.x1, {"a", "b"})
        self.manager.register_historical_group("G", ("S",), (self.x1,), ())
        permit = frozenset((self.x1,))
        plan = self.manager.prepare_retirement("S", permit)

        captured = self.manager.snapshot()
        errors = []

        def run_query():
            try:
                for _ in range(50):
                    self.manager.current_groups(captured)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        worker = threading.Thread(target=run_query)
        worker.start()
        self.manager.publish(plan)
        worker.join()
        self.assertEqual(errors, [])
        self.assertIn("S", captured.online_segments)
        self.assertNotIn("S", self.manager.snapshot().online_segments)


if __name__ == "__main__":
    unittest.main()
