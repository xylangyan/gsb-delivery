"""Tests for indexed reissue queries over ``move_rank`` corrections."""

import numpy as np
import pytest

import bottleneck as bn


def _same(left, right):
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    return np.array_equal(np.isnan(left), np.isnan(right)) and np.array_equal(
        left[~np.isnan(left)], right[~np.isnan(right)]
    )


def _brute_rank(values, window, min_count, corrections):
    corrected = np.array(values, dtype=np.float64, copy=True)
    for position, reading in corrections.items():
        corrected[position] = reading
    return bn.move_rank(corrected, window, min_count=min_count)


def _expected_changes(values, window, min_count, corrections):
    issued = bn.move_rank(values, window, min_count=min_count)
    corrected = _brute_rank(values, window, min_count, corrections)
    return np.flatnonzero(bn.reissue._ranks_differ(issued, corrected)), corrected


def test_swapping_inside_window_without_moving_endpoint_keeps_rank():
    values = np.array([0.0, 2.0, 1.0])
    index = bn.RankReissueIndex(values, window=3)

    together = index.slip({0: 2.0, 1: 0.0})
    assert together.changed_positions.size == 0

    only_first = index.slip({0: 2.0})
    only_second = index.slip({1: 0.0})
    assert only_first.changed_positions.tolist() == [2]
    assert only_second.changed_positions.tolist() == [2]


def test_gap_filled_changes_normalisation_without_crossing_endpoint():
    values = np.array([np.nan, 1.0, 2.0, np.nan])
    index = bn.RankReissueIndex(values, window=4, min_count=1)

    slip = index.slip({0: 0.0})
    expected, corrected = _expected_changes(values, 4, 1, {0: 0.0})
    assert np.array_equal(slip.changed_positions, expected)
    assert _same(slip.corrected, corrected[expected])


@pytest.mark.parametrize("seed", range(20))
def test_matches_native_move_rank(seed):
    rs = np.random.RandomState(seed)
    size = rs.randint(1, 40)
    values = rs.randn(size)
    values[rs.rand(size) < 0.35] = np.nan
    if seed % 2:
        values = np.round(values)
    window = rs.randint(1, size + 1)
    min_count = rs.randint(1, window + 1)
    issued = bn.move_rank(values, window, min_count=min_count)
    index = bn.RankReissueIndex(values, window, min_count, issued)

    corrections = {}
    for position in rs.choice(size, size=rs.randint(0, size + 1), replace=False):
        corrections[int(position)] = (
            np.nan if rs.rand() < 0.3 else float(np.round(rs.randn(), 2))
        )
    slip = index.slip(corrections)
    expected, corrected = _expected_changes(values, window, min_count, corrections)

    assert np.array_equal(slip.changed_positions, expected)
    assert _same(slip.issued, issued[expected])
    assert _same(slip.corrected, corrected[expected])
    for position in expected:
        assert _same(
            np.asarray([slip.corrected_rank(int(position))]),
            np.asarray([corrected[position]]),
        )


def test_interval_queries_and_correction_positions():
    rs = np.random.RandomState(3)
    values = rs.randn(500)
    values[rs.rand(500) < 0.05] = np.nan
    issued = bn.move_rank(values, 40, min_count=4)
    index = bn.RankReissueIndex(values, 40, 4, issued)
    corrections = {
        int(position): float(rs.randn()) for position in (20, 21, 200, 480, 481)
    }
    slip = index.slip(corrections)
    changed = slip.changed_positions

    for _ in range(20):
        start = int(rs.randint(0, 500))
        stop = int(rs.randint(start, 500))
        selected = changed[(changed >= start) & (changed <= stop)]
        assert slip.count(start, stop) == selected.size
        assert slip.earliest(start, stop) == (
            int(selected[0]) if selected.size else None
        )
        expected_corrections = [p for p in sorted(corrections) if start <= p <= stop]
        assert slip.corrections_in(start, stop).tolist() == expected_corrections


def test_slips_are_independent_of_each_other():
    values = np.array([0.0, 2.0, 1.0, 3.0])
    index = bn.RankReissueIndex(values, window=3)
    first = index.slip({0: 2.0}, name="first")
    second = index.slip({1: 0.0}, name="second")
    both = index.slip({0: 2.0, 1: 0.0}, name="both")

    assert first.changed_positions.tolist() == [2]
    assert second.changed_positions.tolist() == [2]
    assert both.changed_positions.tolist() == []
    assert index.slip({0: 2.0}, name="first") is first


def test_archive_keeps_each_slip_separate():
    values = np.array([0.0, 2.0, 1.0, 3.0])
    archive = bn.CorrectionArchive(values, window=3)
    archive.add("a", {0: 2.0})
    archive.add("b", {1: 0.0})

    assert archive.count("a") == 1
    assert archive.count("b") == 1
    assert archive["a"].changed_positions.tolist() == [2]
    assert archive["b"].changed_positions.tolist() == [2]


def test_earliest_ignores_later_changes():
    values = np.array([0.0, 2.0, 1.0, 5.0, 4.0])
    index = bn.RankReissueIndex(values, window=2)
    slip = index.slip({0: 9.0, 3: -5.0})
    changed = slip.changed_positions

    assert changed.size >= 2
    assert slip.earliest() == int(changed[0])
    assert slip.earliest(int(changed[0]) + 1) == (
        int(changed[1]) if changed.size > 1 else None
    )
