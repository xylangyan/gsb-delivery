import unittest

import numpy as np

from datasketch.hll_archive import (
    ArchiveConflictError,
    ArchivedSummary,
    detect_conflicts,
    downgrade_p5_to_p4,
    lift_p4_to_p5,
    realizing_hashes,
    recover_merged_registers,
)
from datasketch.hyperloglog import HyperLogLog
from test.utils import fake_hash_func


def sketch(values, p):
    h = HyperLogLog(p, hashfunc=fake_hash_func)
    for value in values:
        h.update(int(value))
    return h.reg.astype(int).tolist()


def zeros(p):
    return [0] * (1 << p)


class TestPrecisionConversions(unittest.TestCase):
    def test_downgrade_matches_real_sketch(self):
        rng = np.random.default_rng(0)
        hashes = [int(x) for x in rng.integers(0, 1 << 32, size=400)] + list(range(8))
        h5 = HyperLogLog(5, hashfunc=fake_hash_func)
        h4 = HyperLogLog(4, hashfunc=fake_hash_func)
        for value in hashes:
            h5.update(value)
            h4.update(value)
        self.assertTrue(np.array_equal(downgrade_p5_to_p4(h5.reg), h4.reg))

    def test_downgrade_corner_rank_28(self):
        # Low-half rank 28 (no remaining bits) downgrades to p=4 rank 29.
        reg5 = np.zeros(32, dtype=np.int8)
        reg5[3] = 28
        reg4 = downgrade_p5_to_p4(reg5)
        self.assertEqual(reg4[3], 29)

    def test_lift_is_pointwise_lower_bound(self):
        reg4 = np.array([29, 28, 10] + [0] * 13, dtype=np.int8)
        lifted = lift_p4_to_p5(reg4)
        self.assertEqual(lifted[0], 28)
        self.assertEqual(lifted[16], 28)
        self.assertEqual(lifted[1], 28)
        self.assertEqual(lifted[17], 28)
        self.assertEqual(lifted[2], 10)

    def test_realizing_hashes_builds_the_register(self):
        reg = np.array([0, 5, 28, 26] + [0] * 12, dtype=np.int8)
        hashes = realizing_hashes(reg, 4)
        self.assertEqual(sketch(hashes, 4), reg.astype(int).tolist())
        reg5 = np.array([27, 28, 0, 3] + [0] * 28, dtype=np.int8)
        self.assertEqual(sketch(realizing_hashes(reg5, 5), 5), reg5.astype(int).tolist())


class TestRecovery(unittest.TestCase):
    def test_full_p5_observation_recovers_exact_register(self):
        data = [12, 4097, 1 << 20, (1 << 6) | 5]
        reg5 = sketch(data, 5)
        summaries = [ArchivedSummary("s", ["A"], 5, reg5, "v1")]
        result = recover_merged_registers(summaries, ["A"], 5)
        self.assertEqual(result.determinable_precision, 5)
        self.assertTrue(np.all(result.fixed_mask))
        self.assertTrue(np.array_equal(result.reg, reg5))

    def test_p4_only_leaves_p5_half_ambiguous(self):
        # The problem's example: a p=4 bucket with rank 26 does not reveal
        # which p=5 half contributed it.
        reg4 = zeros(4)
        reg4[0] = 26
        summaries = [ArchivedSummary("a", ["A"], 4, reg4, "v1")]
        result = recover_merged_registers(summaries, ["A"], 5)
        self.assertEqual(result.determinable_precision, 4)
        self.assertFalse(result.fixed_mask[0])
        self.assertFalse(result.fixed_mask[16])
        low_witness, high_witness = result.witnesses(16)
        self.assertEqual(low_witness[16], 0)
        self.assertEqual(high_witness[16], 26)
        self.assertTrue(
            np.array_equal(
                downgrade_p5_to_p4(low_witness),
                downgrade_p5_to_p4(high_witness),
            )
        )

    def test_overlapping_p5_summary_covers_the_contribution(self):
        # Shard A is only known at p=4 (bucket 0 rank 26). Shard B has a p=5
        # summary whose low half already reaches 26: the merged low-half
        # register is pinned, only the high half stays ambiguous.
        reg_a4 = zeros(4)
        reg_a4[0] = 26
        reg_b5 = zeros(5)
        reg_b5[0] = 26
        summaries = [
            ArchivedSummary("a4", ["A"], 4, reg_a4, "v1"),
            ArchivedSummary("b5", ["B"], 5, reg_b5, "v1"),
        ]
        result = recover_merged_registers(summaries, ["A", "B"], 5)
        self.assertTrue(result.fixed_mask[0])
        self.assertEqual(result.reg[0], 26)
        self.assertFalse(result.fixed_mask[16])

    def test_overlapping_p5_summary_does_not_cover_the_contribution(self):
        # B's p=5 summary has nothing in the bucket, so A's rank-26 could
        # land in either half: both merged p=5 registers are ambiguous.
        reg_a4 = zeros(4)
        reg_a4[0] = 26
        summaries = [
            ArchivedSummary("a4", ["A"], 4, reg_a4, "v1"),
            ArchivedSummary("b5", ["B"], 5, zeros(5), "v1"),
        ]
        result = recover_merged_registers(summaries, ["A", "B"], 5)
        self.assertFalse(result.fixed_mask[0])
        self.assertFalse(result.fixed_mask[16])
        inputs_low, inputs_high = result.witness_inputs(0)
        for inputs in (inputs_low, inputs_high):
            self.assertEqual(sketch(inputs["A"], 4), reg_a4)
            self.assertEqual(sketch(inputs["B"], 5), zeros(5))
        merged_low = sketch(inputs_low["A"] + inputs_low["B"], 5)
        merged_high = sketch(inputs_high["A"] + inputs_high["B"], 5)
        self.assertNotEqual(merged_low[0], merged_high[0])
        self.assertTrue(
            np.array_equal(
                downgrade_p5_to_p4(np.array(merged_low, dtype=np.int8)),
                downgrade_p5_to_p4(np.array(merged_high, dtype=np.int8)),
            )
        )

    def test_multi_shard_union_uses_max_over_shards(self):
        data_a = [7, 1 << 18]
        data_b = [7, 1 << 22]
        summaries = [
            ArchivedSummary("a", ["A"], 5, sketch(data_a, 5), "v1"),
            ArchivedSummary("b", ["B"], 5, sketch(data_b, 5), "v1"),
        ]
        result = recover_merged_registers(summaries, ["A", "B"], 5)
        expected = sketch(data_a + data_b, 5)
        self.assertTrue(np.array_equal(result.reg, expected))
        self.assertTrue(np.all(result.fixed_mask))

    def test_p4_request_is_recovered_where_determined(self):
        data = [3, 1 << 10, (1 << 6)]
        reg4 = sketch(data, 4)
        result = recover_merged_registers([ArchivedSummary("a", ["A"], 4, reg4, "v1")], ["A"], 4)
        self.assertEqual(result.determinable_precision, 4)
        self.assertTrue(np.array_equal(result.reg, reg4))

    def test_summary_spanning_several_shards(self):
        data_a = [1, 2]
        data_b = [3, 4]
        reg5 = sketch(data_a + data_b, 5)
        summaries = [ArchivedSummary("ab", ["A", "B"], 5, reg5, "v1")]
        result = recover_merged_registers(summaries, ["A", "B"], 5)
        self.assertTrue(np.array_equal(result.reg, reg5))
        # A alone cannot be determined because the joint max might come from B.
        result_a = recover_merged_registers(summaries, ["A"], 5)
        self.assertTrue(np.any(~result_a.fixed_mask))

    def test_witnesses_satisfy_every_contained_summary(self):
        rng = np.random.default_rng(11)
        shard_data = {name: [int(x) for x in rng.integers(0, 1 << 32, size=8)] for name in ("A", "B", "C")}
        summaries = [
            ArchivedSummary("a", ["A"], 4, sketch(shard_data["A"], 4), "v1"),
            ArchivedSummary("b", ["B"], 5, sketch(shard_data["B"], 5), "v1"),
            ArchivedSummary("bc", ["B", "C"], 4, sketch(shard_data["B"] + shard_data["C"], 4), "v1"),
        ]
        result = recover_merged_registers(summaries, ["A", "B", "C"], 5)
        contained = summaries
        for index in result.ambiguous_registers:
            left, right = result.witness_inputs(index)
            for inputs in (left, right):
                for summary in contained:
                    values = [v for shard in summary.shards for v in inputs[shard]]
                    self.assertEqual(sketch(values, summary.p), summary.reg.astype(int).tolist())


class TestConflictsAndVersions(unittest.TestCase):
    def test_conflicting_observations_raise(self):
        bad = [
            ArchivedSummary("x", ["A"], 4, [5, *zeros(4)[:15]], "v1"),
            ArchivedSummary("y", ["A", "B"], 5, zeros(5), "v1"),
        ]
        with self.assertRaises(ArchiveConflictError) as ctx:
            recover_merged_registers(bad, ["A", "B"], 5)
        conflict = ctx.exception.conflicts[0]
        self.assertEqual(conflict.summary.summary_id, "x")
        self.assertEqual(conflict.observed_rank, 5)
        self.assertIn("cannot be attained", conflict.reason)

    def test_detect_conflicts_reports_per_hash_version(self):
        # Same shard, contradictory p=4 ranks but under different hash
        # versions: not a conflict, because versions are never mixed.
        summaries = [
            ArchivedSummary("a1", ["A"], 4, [3, *zeros(4)[:15]], "v1"),
            ArchivedSummary("a2", ["A"], 4, [9, *zeros(4)[:15]], "v2"),
        ]
        self.assertEqual(detect_conflicts(summaries), {"v1": (), "v2": ()})
        with self.assertRaises(ValueError):
            recover_merged_registers(summaries, ["A"], 4)
        result = recover_merged_registers(summaries, ["A"], 4, hash_version="v2")
        self.assertEqual(result.reg[0], 9)

    def test_on_conflict_report_returns_conflicts(self):
        bad = [
            ArchivedSummary("x", ["A"], 4, [5, *zeros(4)[:15]], "v1"),
            ArchivedSummary("y", ["A", "B"], 5, zeros(5), "v1"),
        ]
        result = recover_merged_registers(bad, ["A", "B"], 5, on_conflict="report")
        self.assertTrue(result.conflicts)

    def test_unsupported_precision_and_uncovered_shard(self):
        summaries = [ArchivedSummary("a", ["A"], 5, zeros(5), "v1")]
        with self.assertRaises(ValueError):
            recover_merged_registers(summaries, ["A"], 6)
        with self.assertRaises(ValueError):
            recover_merged_registers(summaries, ["Z"], 5)


class TestPlatterInvariance(unittest.TestCase):
    def test_ordering_does_not_change_the_answer(self):
        data = {"A": [1, 2, 3], "B": [4, 5, 6]}
        summaries = [
            ArchivedSummary("a", ["A"], 4, sketch(data["A"], 4), "v1"),
            ArchivedSummary("b", ["B"], 5, sketch(data["B"], 5), "v1"),
            ArchivedSummary("ab", ["A", "B"], 4, sketch(data["A"] + data["B"], 4), "v1"),
        ]
        first = recover_merged_registers(summaries, ["A", "B"], 5)
        second = recover_merged_registers(list(reversed(summaries)), ["A", "B"], 5)
        self.assertTrue(np.array_equal(first.reg, second.reg))
        self.assertTrue(np.array_equal(first.fixed_mask, second.fixed_mask))

    def test_adding_downgraded_summary_is_a_noop(self):
        data = {"A": [1, 2], "B": [3, 4]}
        summaries = [
            ArchivedSummary("a", ["A"], 5, sketch(data["A"], 5), "v1"),
            ArchivedSummary("b", ["B"], 5, sketch(data["B"], 5), "v1"),
        ]
        base = recover_merged_registers(summaries, ["A", "B"], 5)
        reg5 = sketch(data["A"] + data["B"], 5)
        redundant = ArchivedSummary(
            "ab4", ["A", "B"], 4, downgrade_p5_to_p4(np.array(reg5, dtype=np.int8)).tolist(), "v1"
        )
        extended = recover_merged_registers([*summaries, redundant], ["A", "B"], 5)
        self.assertTrue(np.array_equal(base.reg, extended.reg))
        self.assertTrue(np.array_equal(base.fixed_mask, extended.fixed_mask))


class TestCornerRanks(unittest.TestCase):
    def test_rank_28_and_29_routing(self):
        # p=4 rank 29 must come from a low-half p=5 rank 28; p=4 rank 28 must
        # come from the high half because the low half is capped at 27.
        reg4_29 = zeros(4)
        reg4_29[0] = 29
        result29 = recover_merged_registers([ArchivedSummary("a", ["A"], 4, reg4_29, "v1")], ["A"], 5)
        self.assertTrue(result29.fixed_mask[0])
        self.assertEqual(result29.reg[0], 28)
        self.assertFalse(result29.fixed_mask[16])

        reg4_28 = zeros(4)
        reg4_28[0] = 28
        result28 = recover_merged_registers([ArchivedSummary("a", ["A"], 4, reg4_28, "v1")], ["A"], 5)
        self.assertFalse(result28.fixed_mask[0])
        self.assertEqual(result28.reg[16], 28)


if __name__ == "__main__":
    unittest.main()
