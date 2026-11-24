import unittest

import numpy as np
import pytest

from datasketch.hnsw import HNSW
from datasketch.hnsw_merge import (
    OperationRecorder,
    compare_query_behavior,
    full_replay,
    index_snapshot,
    record_operations,
    replay_operation_log,
    snapshots_match,
)


def l2_distance(x, y):
    return np.linalg.norm(x - y)


def make_index(seed=41, reversed_edges=False, distance_func=None):
    return HNSW(
        distance_func=distance_func or l2_distance,
        m=8,
        ef_construction=24,
        seed=seed,
        reversed_edges=reversed_edges,
    )


def build_base(n=40, dim=6, seed=41, reversed_edges=False):
    rng = np.random.RandomState(123)
    data = rng.rand(n, dim)
    index = make_index(seed=seed, reversed_edges=reversed_edges)
    for key, point in enumerate(data):
        index.insert(key, point)
    return index, data


class TestOperationRecorder(unittest.TestCase):
    def test_recorded_branch_matches_plain_index(self):
        for reversed_edges in (False, True):
            with self.subTest(reversed_edges=reversed_edges):
                base_plain, data = build_base(reversed_edges=reversed_edges)
                base_recorded = base_plain.copy()
                plain = base_plain.copy()
                recorded = base_recorded.copy()

                new_points = np.random.RandomState(7).rand(8, data.shape[1])
                recorder = record_operations(recorded, "left")
                for i, point in enumerate(new_points):
                    key = 100 + i
                    if i % 2 == 0:
                        recorded.insert(key, point)
                        plain.insert(key, point)
                    else:
                        recorded[key] = point
                        plain[key] = point
                recorded.remove(100)
                plain.remove(100)
                recorded.remove(101, hard=True)
                plain.remove(101, hard=True)
                recorder.close()

                self.assertEqual(len(recorder.records), 10)
                self.assertEqual(plain, recorded)
                for record in recorder.records:
                    self.assertGreaterEqual(record.distance_calls, 0)
                    self.assertTrue(record.entry_point is not None or record.op == "remove")

    def test_failed_operation_leaves_no_record(self):
        base, _ = build_base(n=10)
        recorded = base.copy()
        recorder = record_operations(recorded, "left")
        with self.assertRaises(KeyError):
            recorded.remove("missing", hard=True)
        recorder.close()
        self.assertEqual(len(recorder.records), 0)
        self.assertEqual(base, recorded)

    def test_context_manager_detaches_recorder(self):
        base, _ = build_base(n=10)
        recorded = base.copy()
        with OperationRecorder(recorded, "left") as recorder:
            recorded.insert(200, np.random.RandomState(1).rand(6))
        self.assertEqual(len(recorder.records), 1)
        self.assertFalse(hasattr(recorded, "_merge_recorder"))
        self.assertEqual(type(recorded._graphs[0]).__name__, "_Layer")


class TestMergeReplay(unittest.TestCase):
    def _branches(self, reversed_edges=False):
        base, data = build_base(n=30, reversed_edges=reversed_edges)
        rng = np.random.RandomState(99)
        left = base.copy()
        right = base.copy()
        left_points = rng.rand(6, data.shape[1])
        right_points = rng.rand(6, data.shape[1])
        with OperationRecorder(left, "left") as left_recorder:
            for i, point in enumerate(left_points):
                left.insert(100 + i, point, level=0)
            left.remove(2, hard=True)
            left.remove(3)
            left.insert(3, left_points[0], level=0)
        with OperationRecorder(right, "right") as right_recorder:
            for i, point in enumerate(right_points):
                right.insert(200 + i, point, level=0)
            right.remove(4)
            right.insert(4, right_points[1], level=0)
            right.remove(5, hard=True)
        return base, left_recorder.records, right_recorder.records

    def test_replay_matches_full_replay_and_saves_distances(self):
        for reversed_edges in (False, True):
            with self.subTest(reversed_edges=reversed_edges):
                base, left_records, right_records = self._branches(reversed_edges)
                logs = {"left": left_records, "right": right_records}
                total_order = [
                    ("left", 0),
                    ("right", 0),
                    ("right", 1),
                    ("left", 1),
                    ("left", 2),
                    ("right", 2),
                    ("left", 3),
                    ("right", 3),
                    ("left", 4),
                    ("right", 4),
                    ("left", 5),
                    ("left", 6),
                    ("right", 5),
                    ("left", 7),
                    ("right", 6),
                    ("right", 7),
                ]
                result = replay_operation_log(base, logs, total_order)
                reference = full_replay(base, logs, total_order)

                self.assertTrue(snapshots_match(result.index, reference))
                snapshot = index_snapshot(result.index)
                self.assertEqual(snapshot["entry_point"], reference._entry_point)
                self.assertGreater(result.reused_count, 0)
                for decision in result.decisions:
                    if decision.reused:
                        self.assertEqual(decision.distance_calls, 0)
                        self.assertIn("still match", decision.reason)
                    else:
                        self.assertGreater(decision.distance_calls, 0)
                        self.assertNotIn("still match", decision.reason)
                self.assertLess(result.distance_calls, sum(r.distance_calls for r in left_records + right_records))
                report = result.audit_report()
                self.assertIn("reused", report)
                self.assertIn("recomputed", report)
                self.assertEqual(len(result.audit_rows()), len(total_order))

    def test_automatic_level_draws_align_between_paths(self):
        base, data = build_base(n=20)
        rng = np.random.RandomState(5)
        left = base.copy()
        right = base.copy()
        with OperationRecorder(left, "left") as left_recorder:
            for i in range(5):
                left.insert(300 + i, rng.rand(data.shape[1]))
        with OperationRecorder(right, "right") as right_recorder:
            for i in range(5):
                right.insert(400 + i, rng.rand(data.shape[1]))
        logs = {"left": left_recorder.records, "right": right_recorder.records}
        total_order = [
            ("left", 0),
            ("right", 0),
            ("left", 1),
            ("right", 1),
            ("left", 2),
            ("right", 2),
            ("left", 3),
            ("right", 3),
            ("left", 4),
            ("right", 4),
        ]
        result = replay_operation_log(base, logs, total_order)
        reference = full_replay(base, logs, total_order)
        self.assertTrue(snapshots_match(result.index, reference))
        self.assertTrue(
            np.array_equal(
                result.index._random.get_state()[1],
                reference._random.get_state()[1],
            )
        )

    def test_soft_delete_recovery_and_hard_reinsert_consume_rng_deterministically(self):
        base, data = build_base(n=15)
        left = base.copy()
        right = base.copy()
        with OperationRecorder(left, "left") as left_recorder:
            left.remove(0)
            left.insert(0, data[0] + 1.0)
            left.remove(1, hard=True)
            left.insert(1, data[1] - 1.0)
        with OperationRecorder(right, "right") as right_recorder:
            right.insert(600, np.random.RandomState(3).rand(data.shape[1]))
        logs = {"left": left_recorder.records, "right": right_recorder.records}
        total_order = [("right", 0), ("left", 0), ("left", 1), ("left", 2), ("left", 3)]
        result = replay_operation_log(base, logs, total_order)
        reference = full_replay(base, logs, total_order)
        self.assertTrue(snapshots_match(result.index, reference))
        recovery_record = left_recorder.records[1]
        self.assertTrue(recovery_record.recovered)
        self.assertIsNone(recovery_record.rng_before)
        hard_reinsert = left_recorder.records[3]
        self.assertFalse(hard_reinsert.recovered)
        self.assertIsNotNone(hard_reinsert.rng_before)

    def test_entry_point_deletion_invalidates_later_branch_evidence(self):
        base, data = build_base(n=25)
        entry_point = base._entry_point
        left = base.copy()
        right = base.copy()
        with OperationRecorder(left, "left") as left_recorder:
            left.remove(entry_point, hard=True)
        with OperationRecorder(right, "right") as right_recorder:
            right.insert(999, np.random.RandomState(2).rand(data.shape[1]), level=0)
        logs = {"left": left_recorder.records, "right": right_recorder.records}
        total_order = [("left", 0), ("right", 0)]
        result = replay_operation_log(base, logs, total_order)
        reference = full_replay(base, logs, total_order)
        self.assertTrue(snapshots_match(result.index, reference))
        decisions = {decision.key: decision for decision in result.decisions}
        self.assertTrue(decisions[entry_point].reused)
        self.assertFalse(decisions[999].reused)
        self.assertIn("entry point changed", decisions[999].reason)

    def test_evidence_verification_only_depends_on_recorded_reads(self):
        from datasketch.hnsw_merge import _verify_evidence

        base, data = build_base(n=20)
        recorded = base.copy()
        with OperationRecorder(recorded, "branch") as recorder:
            recorded.insert(500, data[0] + 0.5, level=0)
        record = recorder.records[0]
        untouched = base.copy()
        valid, _ = _verify_evidence(untouched, record)
        self.assertTrue(valid)
        touched_key = record.edge_reads[0][1]
        layer_idx = record.edge_reads[0][0]
        untouched._graphs[layer_idx][touched_key] = {}
        valid, reason = _verify_evidence(untouched, record)
        self.assertFalse(valid)
        self.assertIn("adjacency", reason)

    def test_distant_unrelated_change_keeps_evidence_valid(self):
        from datasketch.hnsw_merge import _verify_evidence

        base, data = build_base(n=80)
        recorded = base.copy()
        with OperationRecorder(recorded, "branch") as recorder:
            recorder_index = recorded
            recorder_index.insert(500, data[0] + 0.5, level=0)
        record = recorder.records[0]
        observed_nodes = {key for key, _, _ in record.node_reads}
        observed_edge_owners = {key for _, key, _ in record.edge_reads}
        untouched = {key for key in base._nodes if key not in observed_nodes and key not in observed_edge_owners}
        self.assertTrue(untouched, "a large enough index must contain nodes the operation never visited")
        target = min(untouched)

        candidate = base.copy()
        # Simulate another branch correcting a far-away point and rewiring its
        # adjacency. The recorded operation never looked at this node, so its
        # evidence must remain valid even though the graph elsewhere differs.
        candidate._nodes[target].point = data[target] + 10.0
        target_layer_idx = max(i for i, layer in enumerate(candidate._graphs) if target in layer)
        target_layer = candidate._graphs[target_layer_idx]
        original_neighbors = dict(target_layer[target])
        pruned_neighbors = dict(list(original_neighbors.items())[:-1])
        target_layer[target] = pruned_neighbors

        valid, reason = _verify_evidence(candidate, record)
        self.assertTrue(valid, reason)
        self.assertIn("still match", reason)

    def test_post_merge_insert_and_queries_match_reference(self):
        base, data = build_base(n=25)
        rng = np.random.RandomState(11)
        left = base.copy()
        right = base.copy()
        with OperationRecorder(left, "left") as left_recorder:
            for i in range(4):
                left.insert(100 + i, rng.rand(data.shape[1]))
            left.remove(7)
        with OperationRecorder(right, "right") as right_recorder:
            for i in range(4):
                right.insert(200 + i, rng.rand(data.shape[1]))
            right.remove(8, hard=True)
        logs = {"left": left_recorder.records, "right": right_recorder.records}
        total_order = [
            ("left", 0),
            ("right", 0),
            ("left", 1),
            ("right", 1),
            ("left", 2),
            ("right", 2),
            ("left", 3),
            ("left", 4),
            ("right", 3),
            ("right", 4),
        ]
        merged = replay_operation_log(base, logs, total_order).index
        reference = full_replay(base, logs, total_order)
        self.assertTrue(snapshots_match(merged, reference))

        post_point = rng.rand(data.shape[1])
        merged.insert(700, post_point)
        reference.insert(700, post_point)
        self.assertTrue(snapshots_match(merged, reference))

        queries = rng.rand(12, data.shape[1])
        compare_query_behavior(merged, reference, queries, k=5)
        compare_query_behavior(merged, reference, queries, k=10, ef=40)

    def test_entry_point_removal_collapses_top_layers_and_is_reused(self):
        data = np.random.RandomState(123).rand(20, 6)
        base = make_index(seed=41)
        base.insert(0, data[0], level=2)
        for key in range(1, len(data)):
            base.insert(key, data[key], level=0)
        entry_point = base._entry_point
        sole_layers = [i for i, layer in enumerate(base._graphs) if tuple(layer) == (entry_point,)]
        self.assertEqual(sole_layers, [1, 2])

        left = base.copy()
        right = base.copy()
        with OperationRecorder(left, "left") as left_recorder:
            left.remove(entry_point)
        with OperationRecorder(right, "right") as right_recorder:
            right.insert(800, np.random.RandomState(4).rand(data.shape[1]), level=0)
        logs = {"left": left_recorder.records, "right": right_recorder.records}

        remove_first_order = [("left", 0), ("right", 0)]
        remove_first = replay_operation_log(base, logs, remove_first_order)
        reference = full_replay(base, logs, remove_first_order)
        self.assertTrue(snapshots_match(remove_first.index, reference))
        remove_decision = next(d for d in remove_first.decisions if d.key == entry_point)
        self.assertTrue(remove_decision.reused)
        self.assertEqual(remove_decision.distance_calls, 0)
        self.assertTrue(left_recorder.records[0].layer_removes)
        self.assertEqual(len(remove_first.index._graphs), len(reference._graphs))

        # With the insert applied first, the entry point changes and the
        # recorded remove must be recomputed instead.
        insert_first_order = [("right", 0), ("left", 0)]
        insert_first = replay_operation_log(base, logs, insert_first_order)
        reference_two = full_replay(base, logs, insert_first_order)
        self.assertTrue(snapshots_match(insert_first.index, reference_two))
        remove_decision_two = next(d for d in insert_first.decisions if d.key == entry_point)
        self.assertFalse(remove_decision_two.reused)
        self.assertNotIn("still match", remove_decision_two.reason)

    def test_missing_record_raises(self):
        base, _ = build_base(n=10)
        with self.assertRaises(KeyError):
            replay_operation_log(base, {"left": []}, [("left", 0)])

    def test_duplicate_sequence_raises(self):
        base, data = build_base(n=10)
        recorded = base.copy()
        with OperationRecorder(recorded, "left") as recorder:
            recorded.insert(500, data[0], level=0)
        records = recorder.records
        with self.assertRaises(ValueError):
            replay_operation_log(base, {"left": [records[0], records[0]]}, [("left", 0)])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
