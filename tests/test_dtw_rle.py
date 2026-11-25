import math
import random

import pytest

from dtaidistance import dtw, dtw_rle


def _expand(runs):
    return dtw_rle.expand(runs)


def test_compress_and_expand():
    series = [0, 0, 0, 3, 3, 0]
    runs = dtw_rle.compress(series)
    assert runs == [(0, 3), (3, 2), (0, 1)]
    assert dtw_rle.expand(runs) == series
    rle = dtw_rle.RunLengthSeries(series, expanded=True)
    assert rle.runs == runs
    assert rle.length == len(series)


def test_distance_matches_expanded():
    cases = [
        ([(0, 5), (3, 5)], [(0, 8), (3, 2)], None, None),
        ([(0, 5), (3, 5)], [(0, 8), (3, 2)], 2, 1),
        ([(0, 10), (3, 5)], [(0, 4), (2, 2), (0, 4), (3, 5)], 100, 0),
        ([(0, 10), (3, 10)], [(0, 12), (3, 8)], 3, 2),
        ([(1, 7), (0, 2), (4, 5)], [(0, 7), (1, 4)], 2, 0),
    ]
    for runs1, runs2, window, penalty in cases:
        expected = dtw.distance(_expand(runs1), _expand(runs2),
                                window=window, penalty=penalty)
        actual = dtw_rle.distance(runs1, runs2, window=window,
                                  penalty=penalty)
        assert actual == pytest.approx(expected)


def test_random_against_expanded():
    random.seed(42)
    for _ in range(200):
        runs1 = [(random.randint(0, 4), random.randint(1, 6))
                 for _ in range(random.randint(1, 4))]
        runs2 = [(random.randint(0, 4), random.randint(1, 6))
                 for _ in range(random.randint(1, 4))]
        penalty = random.choice([None, 0, 1, 3])
        window = random.choice([1, 2, 4, None, 50])
        expected = dtw.distance(_expand(runs1), _expand(runs2),
                                window=window, penalty=penalty)
        actual = dtw_rle.distance(runs1, runs2, window=window,
                                  penalty=penalty)
        assert actual == pytest.approx(expected)


def test_path_is_compact_and_optimal():
    runs1 = [(0, 6), (3, 6)]
    runs2 = [(0, 8), (3, 4)]
    distance, path = dtw_rle.distance_and_path(runs1, runs2,
                                               window=20, penalty=1)
    assert path[0] == (0, 0)
    assert path[-1] == (runs1[0][1] + runs1[1][1] - 1,
                        runs2[0][1] + runs2[1][1] - 1)
    for (i0, j0), (i1, j1) in zip(path, path[1:]):
        di, dj = i1 - i0, j1 - j0
        assert di >= 0 and dj >= 0
        assert di == dj or di == 0 or dj == 0
    score = dtw_rle.path_cost(runs1, runs2, path, penalty=1)
    assert math.sqrt(score) == pytest.approx(distance)


def test_path_can_leave_middle_of_run():
    # The optimal path turns inside the first constant run (at sample 5 of a
    # run that ends at sample 10), not at the run boundary.
    runs1 = [(3, 11), (3, 9), (1, 6), (1, 11)]
    runs2 = [(0, 4), (1, 12), (0, 7)]
    distance, path = dtw_rle.distance_and_path(runs1, runs2,
                                               window=2, penalty=1)
    assert (5, 5) in path
    assert dtw_rle.path_cost(runs1, runs2, path, penalty=1) == \
        pytest.approx(distance ** 2)


def test_unalignable_window():
    runs1 = [(0, 5), (3, 5)]
    runs2 = [(0, 5), (3, 5)]
    distance, path = dtw_rle.distance_and_path(runs1, runs2,
                                               window=0, penalty=0)
    assert distance == float("inf")
    assert path is None
    assert dtw_rle.warping_path(runs1, runs2, window=0) is None


def test_long_runs_do_not_expand():
    runs1 = [(0, 10 ** 6), (3, 10 ** 6)]
    runs2 = [(0, 2 * 10 ** 6), (3, 10 ** 6)]
    distance, path = dtw_rle.distance_and_path(runs1, runs2,
                                               window=10 ** 9, penalty=1)
    assert distance == pytest.approx(1000)
    assert len(path) <= 4
    assert dtw_rle.path_cost(runs1, runs2, path, penalty=1) == 10 ** 6


def test_segments_and_full_path():
    runs1 = [(0, 3), (3, 3)]
    runs2 = [(0, 4), (3, 2)]
    segments = dtw_rle.warping_path_segments(runs1, runs2, window=10)
    assert segments
    for i0, j0, i1, j1 in segments:
        di, dj = i1 - i0, j1 - j0
        assert di == dj or di == 0 or dj == 0
    full = dtw_rle.warping_path_full(runs1, runs2, window=10)
    assert full[0] == (0, 0)
    assert full[-1] == (5, 5)
    for (i0, j0), (i1, j1) in zip(full, full[1:]):
        assert (i1 - i0, j1 - j0) in ((1, 0), (0, 1), (1, 1))


def test_dtw_aliases():
    runs1 = [(0, 3), (3, 3)]
    runs2 = [(0, 4), (3, 2)]
    assert dtw.distance_rle(runs1, runs2, window=10) == \
        pytest.approx(dtw_rle.distance(runs1, runs2, window=10))
    assert dtw.warping_path_rle(runs1, runs2, window=10) == \
        dtw_rle.warping_path(runs1, runs2, window=10)
