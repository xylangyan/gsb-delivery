# -*- coding: utf-8 -*-
# MinIO Python Library for Amazon S3 Compatible Cloud Storage, (C)
# [2014] - [2025] MinIO, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest import TestCase

from minio.notification import (EventEvidence, EventIdentity, EventState,
                                NotificationEvent, NotificationReader,
                                ObjectGeneration)


def record(
        object_name,
        event_name,
        generation,
        sequencer=None,
        event_id=None,
        version_id=None,
):
    object_info = {
        "key": object_name,
        "generation": str(generation),
        "sequencer": sequencer or f"{object_name}-{generation}",
    }
    if version_id:
        object_info["versionId"] = version_id
    result = {
        "eventName": event_name,
        "eventTime": "2026-11-22T00:00:00.000Z",
        "s3": {
            "bucket": {"name": "bucket"},
            "object": object_info,
        },
    }
    if event_id:
        result["eventId"] = event_id
    return result


def envelope(*records):
    return {"Records": list(records)}


class FakeEvidence:
    def __init__(self, events=None, generations=None):
        self.events = events or {}
        self.generations = generations or {}

    def event_evidence(self, *, bucket_name, event_id):
        return self.events.get((bucket_name, event_id))

    def object_generations(self, *, bucket_name, object_name):
        return self.generations.get((bucket_name, object_name), [])


class IdentityTest(TestCase):
    def test_sequencer_is_not_an_identity_across_objects(self):
        first = NotificationEvent.from_record(record(
            "a", "s3:ObjectCreated:Put", 4, sequencer="same",
        ))
        second = NotificationEvent.from_record(record(
            "c", "s3:ObjectCreated:Put", 1, sequencer="same",
        ))
        self.assertNotEqual(first.identity.key, second.identity.key)
        self.assertEqual("a", first.identity.object_name)
        self.assertEqual("c", second.identity.object_name)

    def test_event_id_is_preferred(self):
        first = NotificationEvent.from_record(record(
            "a", "s3:ObjectCreated:Put", 3, event_id="evt-1",
        ))
        replay = NotificationEvent.from_record(record(
            "a", "s3:ObjectCreated:Put", 3, sequencer="moved",
            event_id="evt-1",
        ))
        self.assertEqual(first.identity.key, replay.identity.key)

    def test_encoded_key_is_decoded(self):
        event = NotificationEvent.from_record(record(
            "dir%2Fobject", "s3:ObjectCreated:Put", 1,
        ))
        self.assertEqual("dir/object", event.object_name)

    def test_replay_with_different_markers_is_one_identity(self):
        reader = NotificationReader()
        first = NotificationEvent.from_record(record(
            "a", "s3:ObjectCreated:Put", 4, event_id="a-4",
        ))
        reader.feed(envelope(first.raw))
        self.assertTrue(reader.ack(first.identity))

        replay = record("a", "s3:ObjectCreated:Put", 4, event_id="a-4")
        replay["s3"]["object"]["versionId"] = "v4"
        del replay["s3"]["object"]["generation"]
        self.assertEqual([], reader.feed(envelope(replay)))
        self.assertEqual(2, reader.delivery_count(first.identity))
        self.assertEqual(1, reader.acknowledgement_count(first.identity))
        self.assertEqual(
            EventState.ACKED, reader.status(first.identity),
        )


class ReconnectTest(TestCase):
    def setUp(self):
        self.reader = NotificationReader()

    def test_reconnect_split_keeps_one_entry_per_identity(self):
        first = self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 3),
            record("b", "s3:ObjectRemoved:Delete", 8),
            record("a", "s3:ObjectCreated:Put", 4),
        ))
        self.assertEqual(3, len(first))

        a3, b8, a4 = (event.identity for event in first)
        self.assertTrue(self.reader.ack(a3))
        self.reader.defer(b8, "delete needs evidence")
        self.assertEqual(EventState.PENDING, self.reader.status(b8))
        self.assertEqual(3, self.reader.current_generation("bucket", "a"))

        replay = self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 3),
            record("b", "s3:ObjectRemoved:Delete", 8),
        ))
        replay += self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 4),
        ))
        self.assertEqual([], replay)
        self.assertEqual(EventState.ACKED, self.reader.status(a3))
        self.assertEqual(EventState.PENDING, self.reader.status(b8))
        self.assertEqual(EventState.DELIVERED, self.reader.status(a4))
        self.assertEqual(2, self.reader.delivery_count(a3))
        self.assertEqual(1, self.reader.acknowledgement_count(a3))

        self.assertTrue(self.reader.ack(a4))
        self.assertEqual(4, self.reader.current_generation("bucket", "a"))
        self.assertFalse(self.reader.ack(a3))
        self.assertEqual(1, self.reader.acknowledgement_count(a3))
        self.assertEqual(4, self.reader.current_generation("bucket", "a"))

    def test_sequencer_collision_with_other_object_is_independent(self):
        events = self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 4, sequencer="same-seq"),
            record("c", "s3:ObjectCreated:Put", 1, sequencer="same-seq"),
        ))
        a4, c1 = (event.identity for event in events)
        self.assertNotEqual(a4.key, c1.key)
        self.assertTrue(self.reader.ack(a4))
        self.assertTrue(self.reader.ack(c1))
        self.assertEqual(4, self.reader.current_generation("bucket", "a"))
        self.assertEqual(1, self.reader.current_generation("bucket", "c"))

    def test_independent_object_is_not_blocked_by_pending_delete(self):
        events = self.reader.feed(envelope(
            record("b", "s3:ObjectRemoved:Delete", 8),
            record("c", "s3:ObjectCreated:Put", 1,
                   sequencer="b-8"),
        ))
        b8, c1 = (event.identity for event in events)
        self.reader.defer(b8, "delete needs evidence")
        self.assertTrue(self.reader.ack(c1))
        self.assertEqual(1, self.reader.current_generation("bucket", "c"))
        self.assertEqual(EventState.PENDING, self.reader.status(b8))

    def test_late_old_generation_never_becomes_current(self):
        events = self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 4),
        ))
        a4 = events[0].identity
        self.reader.ack(a4)
        self.assertEqual(4, self.reader.current_generation("bucket", "a"))

        late = self.reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 3),
        ))
        late_a3 = late[0].identity
        self.assertFalse(self.reader.ack(late_a3))
        self.assertEqual(EventState.ACKED, self.reader.status(late_a3))
        self.assertEqual(4, self.reader.current_generation("bucket", "a"))


class ReconcileTest(TestCase):
    def test_event_evidence_confirms_lost_ack_once(self):
        evidence = FakeEvidence(events={
            ("bucket", "a-4"): EventEvidence(
                event_id="a-4", applied=True, generation=4, deleted=False,
            ),
        })
        reader = NotificationReader(evidence=evidence)
        events = reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 3, event_id="a-3"),
            record("b", "s3:ObjectRemoved:Delete", 8, event_id="b-8"),
            record("a", "s3:ObjectCreated:Put", 4, event_id="a-4"),
        ))
        a3, b8, a4 = (event.identity for event in events)
        reader.ack(a3)
        reader.defer(b8)

        report = reader.reconcile()
        self.assertEqual((a4,), report.confirmed)
        self.assertEqual((b8,), report.unconfirmed)
        self.assertEqual(4, reader.current_generation("bucket", "a"))
        self.assertEqual(EventState.ACKED, reader.status(a4))
        self.assertEqual(1, reader.acknowledgement_count(a4))

        twice = reader.reconcile()
        self.assertEqual((), twice.confirmed)
        self.assertEqual(1, reader.acknowledgement_count(a4))

        later = reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 3, event_id="a-3"),
        ))
        self.assertEqual([], later)
        self.assertEqual(4, reader.current_generation("bucket", "a"))
        self.assertEqual(1, reader.acknowledgement_count(a4))

    def test_delete_is_decided_by_its_own_object_generations(self):
        evidence = FakeEvidence(generations={
            ("bucket", "c"): [
                ObjectGeneration(generation=1),
            ],
        })
        reader = NotificationReader(evidence=evidence)
        events = reader.feed(envelope(
            record("b", "s3:ObjectRemoved:Delete", 8),
            record("c", "s3:ObjectCreated:Put", 1),
        ))
        b8, c1 = (event.identity for event in events)
        reader.defer(b8)
        report = reader.reconcile()

        self.assertEqual((c1,), report.confirmed)
        self.assertEqual((b8,), report.unconfirmed)
        self.assertEqual(EventState.PENDING, reader.status(b8))
        self.assertEqual(1, reader.current_generation("bucket", "c"))

    def test_delete_is_confirmed_by_tombstone_in_its_generation(self):
        evidence = FakeEvidence(generations={
            ("bucket", "b"): [
                ObjectGeneration(generation=8, is_delete_marker=True),
            ],
        })
        reader = NotificationReader(evidence=evidence)
        events = reader.feed(envelope(
            record("b", "s3:ObjectRemoved:Delete", 8),
        ))
        b8 = events[0].identity
        reader.defer(b8)
        report = reader.reconcile()
        self.assertEqual((b8,), report.confirmed)
        self.assertTrue(reader.is_deleted("bucket", "b"))

    def test_delete_without_tombstone_stays_pending(self):
        evidence = FakeEvidence(generations={
            ("bucket", "b"): [ObjectGeneration(generation=8)],
        })
        reader = NotificationReader(evidence=evidence)
        events = reader.feed(envelope(
            record("b", "s3:ObjectRemoved:Delete", 8),
        ))
        b8 = events[0].identity
        reader.defer(b8)
        report = reader.reconcile()
        self.assertEqual((b8,), report.unconfirmed)
        self.assertEqual(EventState.PENDING, reader.status(b8))

    def test_superseded_delete_is_not_confirmed(self):
        evidence = FakeEvidence(generations={
            ("bucket", "b"): [
                ObjectGeneration(generation=8, is_delete_marker=True),
                ObjectGeneration(generation=9),
            ],
        })
        reader = NotificationReader(evidence=evidence)
        events = reader.feed(envelope(
            record("b", "s3:ObjectRemoved:Delete", 8),
        ))
        b8 = events[0].identity
        reader.defer(b8)
        report = reader.reconcile()
        self.assertEqual((b8,), report.superseded)
        self.assertEqual(EventState.SUPERSEDED, reader.status(b8))

    def test_reconcile_resolves_generation_from_version_id(self):
        evidence = FakeEvidence(generations={
            ("bucket", "a"): [
                ObjectGeneration(generation=4, version_id="v4"),
            ],
        })
        reader = NotificationReader(evidence=evidence)
        item = record("a", "s3:ObjectCreated:Put", 4, version_id="v4")
        del item["s3"]["object"]["generation"]
        items = reader.feed(envelope(item))
        a4 = items[0].identity
        self.assertIsNone(a4.generation)
        reader.defer(a4)
        report = reader.reconcile()
        self.assertEqual((a4,), report.confirmed)
        self.assertEqual(4, reader.current_generation("bucket", "a"))


class ReplayGapTest(TestCase):
    def test_gap_separates_final_state_from_intermediate_delete(self):
        reader = NotificationReader()
        events = reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 4),
            record("b", "s3:ObjectRemoved:Delete", 8),
        ))
        a4, b8 = (event.identity for event in events)
        reader.ack(a4)
        reader.defer(b8)

        report = reader.note_replay_gap(
            reason="queue retention expired",
            objects=[("bucket", "a"), ("bucket", "b")],
        )

        self.assertEqual(("bucket", "b"), report.generation_history_required[0])
        self.assertEqual((b8,), report.cannot_confirm_by_final_state)
        self.assertIn(("bucket", "a"), report.final_state_can_confirm)
        self.assertIn("cannot prove", report.explain())
        self.assertNotIn("was delivered:", report.explain().split(
            "cannot prove",
        )[0])

    def test_gap_without_pending_delete(self):
        reader = NotificationReader()
        events = reader.feed(envelope(
            record("a", "s3:ObjectCreated:Put", 4),
        ))
        reader.ack(events[0].identity)
        report = reader.note_replay_gap(objects=[("bucket", "a")])
        self.assertEqual((), report.cannot_confirm_by_final_state)
        self.assertEqual((), report.generation_history_required)


class ReaderFromIterableTest(TestCase):
    def test_reader_consumes_event_iterable_envelopes(self):
        source = [
            envelope(record("a", "s3:ObjectCreated:Put", 3)),
            envelope(
                record("a", "s3:ObjectCreated:Put", 3),
                record("a", "s3:ObjectCreated:Put", 4),
            ),
        ]
        reader = NotificationReader(source)
        events = list(reader)
        self.assertEqual([3, 4], [event.generation for event in events])

        identity = EventIdentity(
            bucket_name="bucket",
            object_name="a",
            event_name="s3:ObjectCreated:Put",
            generation=3,
        )
        self.assertTrue(reader.ack(identity))
        self.assertEqual(3, reader.current_generation("bucket", "a"))
