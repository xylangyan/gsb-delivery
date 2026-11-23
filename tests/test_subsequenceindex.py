import math

import numpy as np
import pytest

from dtaidistance.subsequence.dtw import subsequence_alignment
from dtaidistance.subsequence.subsequenceindex import (
    SubsequenceIndex,
    interval_subsequence_index,
)


def _stock_best(query, series, begin, end):
    """Brute-force reference using the stock subsequence alignment."""
    best = None
    for start in range(begin, end + 1):
        for stop in range(start, end + 1):
            sa = subsequence_alignment(query, series[start:stop + 1],
                                       penalty=0, use_c=False)
            try:
                match = sa.best_match(minlength=1)
            except Exception:
                continue
            match_end = start + match.segment[1]
            match_start = start + match.segment[0]
            key = (match.distance, match_end, match_start)
            if best is None or key < best[0]:
                best = (key, (match_start, match_end))
    return best


@pytest.mark.parametrize("block_size", [1, 2, 3, 8])
def test_interval_matches_stock(block_size):
    rng = np.random.default_rng(7)
    for _ in range(8):
        m = int(rng.integers(1, 7))
        n = int(rng.integers(m + 1, 30))
        query = rng.normal(size=m)
        series = rng.normal(size=n)
        index = SubsequenceIndex(query, series, block_size=block_size)
        for begin in range(n):
            for end in range(begin, n):
                got = index.query(begin, end)
                ref = _stock_best(query, series, begin, end)
                if ref is None:
                    continue
                (distance, ref_end, ref_start), _ = ref
                assert got.distance == pytest.approx(distance, rel=1e-9, abs=1e-9)
                assert got.begin == ref_start
                assert got.end == ref_end


@pytest.mark.parametrize("block_size", [1, 2, 5])
def test_interval_path_is_inside_and_recomputes(block_size):
    rng = np.random.default_rng(11)
    query = rng.normal(size=4)
    series = rng.normal(size=40)
    index = SubsequenceIndex(query, series, block_size=block_size)
    for begin in range(len(series)):
        for end in range(begin, len(series)):
            match = index.query(begin, end)
            if math.isinf(match.distance):
                continue
            path = match.path
            assert path
            assert match.begin <= path[0][1] <= end
            assert match.begin <= path[-1][1] <= end
            assert path[0][1] == match.begin
            assert path[-1][1] == match.end
            cost = sum((query[i] - series[j]) ** 2 for i, j in path)
            assert math.sqrt(cost) == pytest.approx(match.distance)


def test_left_boundary_does_not_borrow_outside_points():
    query = np.array([0., 1, 2, 1, 0])
    series = np.array([0., 1, 2, 1, 0, 5, 5, 5])
    index = SubsequenceIndex(query, series, block_size=2)
    full = index.query(0, len(series) - 1)
    assert full.begin == 0 and full.end == 4
    # Starting at archive point 1, the full-series path that started at 0 may
    # not be reused.  The best fully-inside path is strictly worse.
    clipped = index.query(1, len(series) - 1)
    assert clipped.begin == 1
    assert clipped.distance > full.distance
    assert all(match_begin >= 1 for _i, match_begin in clipped.path)


def test_truncated_full_path_is_not_reused():
    # The full-archive optimum starts at 0.  Dropping its first two points
    # would leave a zero-cost "path" inside [1, ...], but that path has
    # borrowed points from outside the interval.  The index must instead
    # return the genuine in-interval optimum.
    query = np.array([2., 1, 2])
    series = np.array([1., 2, 0, 2, 0, 0])
    index = SubsequenceIndex(query, series, block_size=1)
    full = index.query(0, len(series) - 1)
    assert full.begin == 0
    assert full.path[0][1] == 0
    inside = index.query(1, len(series) - 1)
    assert inside.begin >= 1
    assert inside.distance == pytest.approx(1.0)
    assert inside.path[0][1] >= 1


def test_tie_earliest_end_then_earliest_start():
    query = np.array([1., 2, 0])
    series = np.array([1., 0, 1, 2, 0, 1, 2, 0])
    index = SubsequenceIndex(query, series, block_size=1)
    match = index.query(0, len(series) - 1)
    # Multiple zero-ish matches; the first end must win, and for that end the
    # earlier start must win.
    assert match.end == 4
    assert match.begin == 2


def test_update_matches_rebuild():
    rng = np.random.default_rng(23)
    query = rng.normal(size=5)
    series = rng.normal(size=50)
    index = SubsequenceIndex(query, series, block_size=4)
    for position, value in [(0, -3.0), (17, 9.0), (49, 1.5), (8, -7.0)]:
        series = series.copy()
        series[position] = value
        index.update(position, value)
        rebuilt = SubsequenceIndex(query, series, block_size=4)
        for begin in (0, 3, 8, 20):
            for end in (len(series) - 1, 31, 19):
                if begin > end:
                    continue
                a = index.query(begin, end)
                b = rebuilt.query(begin, end)
                assert a.distance == pytest.approx(b.distance)
                assert (a.begin, a.end) == (b.begin, b.end)


def test_update_off_best_path_can_change_winner():
    query = np.array([0., 1, 2])
    series = np.array([0., 1, 1, 0, 1, 9], dtype=np.float64)
    index = SubsequenceIndex(query, series, block_size=2)
    before = index.query(0, len(series) - 1)
    assert before.begin == 0
    assert before.distance == pytest.approx(1.0)
    # Correct a point that was not on the winning path.  It turns the other
    # occurrence into an exact match.
    index.update(5, 2.0)
    after = index.query(0, len(series) - 1)
    assert after.begin == 3
    assert after.distance == pytest.approx(0.0)


def test_sequence_numbers_and_duplicates():
    query = np.array([0., 1, 0])
    series = np.array([0., 1, 0, 4, 4, 4], dtype=np.int16)
    sequence_numbers = [100, 100, 101, 102, 103, 104]
    index = SubsequenceIndex(query, series, block_size=2,
                             sequence_numbers=sequence_numbers)
    match = index.query(0, len(series) - 1)
    assert match.sequence_begin == 100
    assert match.sequence_end == 101
    index.update(2, 0.0, sequence_number=205)
    match = index.query(0, len(series) - 1)
    assert match.sequence_end == 205
    index.correct_sequence_number(0, 999)
    assert index.query(0, len(series) - 1).sequence_begin == 999


def test_short_and_empty_intervals():
    index = SubsequenceIndex(np.array([0., 1, 2]), np.array([1., 2, 3, 4]),
                             block_size=2)
    empty = index.query(2, 1)
    assert math.isinf(empty.distance)
    assert empty.path == []
    short = index.query(0, 1)
    # A three-point template can be compressed onto two archive points; the
    # match is finite but cannot use points outside the interval.
    assert math.isfinite(short.distance)
    assert {j for _i, j in short.path} <= {0, 1}


def test_template_limit_and_integer_input():
    series = np.arange(10, dtype=np.int16)
    with pytest.raises(ValueError):
        SubsequenceIndex(np.zeros(33), series)
    index = SubsequenceIndex(np.array([1., 2, 3]), series)
    assert index.query(0, 9).distance < math.inf


def test_constructor_alias():
    index = interval_subsequence_index(np.array([1., 2]),
                                       np.array([1., 2, 1, 2]))
    assert index.query(0, 3).distance == pytest.approx(0.0)
