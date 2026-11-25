# -*- coding: utf-8 -*-
import math

from jenkspy import query_archive


def five_segment_archive():
    return [
        (0, 1, 524304),
        (1, 524305, 524288),
        (2, 1048593, 524288),
        (3, 1572881, 524288),
        (4, 2097169, 524320),
    ]


def test_legacy_breaks_and_original_endpoints():
    result = query_archive(five_segment_archive(), 17, 2621456, 3)

    assert result["breaks"] == [0, 0, 2, 4]
    assert result["endpoints"] == [524304, 1572880, 2621456]


def test_first_seal_keeps_legacy_partition_at_limit():
    result = query_archive(
        five_segment_archive(),
        17,
        2621456,
        3,
        [(1, 524304, 786448)],
        524288,
    )

    sealed = result["constrained"]
    assert sealed["feasible"] is True
    assert sealed["endpoints"] == [524304, 1572880, 2621456]
    assert sealed["cost"] == 524288.0


def test_second_seal_selects_alternative_partition():
    result = query_archive(
        five_segment_archive(),
        17,
        2621456,
        3,
        [(2, 1835024, 2097168)],
        524288,
    )

    sealed = result["constrained"]
    assert sealed["feasible"] is True
    assert sealed["endpoints"] == [1048592, 2097168, 2621456]
    assert sealed["cost"] == 524288.0


def test_both_seals_infeasible_at_limit():
    result = query_archive(
        five_segment_archive(),
        17,
        2621456,
        3,
        [(1, 524304, 786448), (2, 1835024, 2097168)],
        524288,
    )

    assert result["feasible"] is False
    assert result["message"] == "签封无法兑现"
    assert result["breaks"] == [0, 0, 2, 4]
    assert result["endpoints"] == [524304, 1572880, 2621456]


def test_one_ulp_budget_changes_earliest_endpoint():
    seals = [(1, 524304, 786448), (2, 1835024, 2097168)]

    higher = query_archive(
        five_segment_archive(),
        17,
        2621456,
        3,
        seals,
        899999.7104796651,
    )["constrained"]
    lower = query_archive(
        five_segment_archive(),
        17,
        2621456,
        3,
        seals,
        899999.710479665,
    )["constrained"]

    assert higher["endpoints"] == [730907, 1835024, 2621456]
    assert higher["cost"] == 899999.7104796651
    assert lower["endpoints"] == [730908, 1835024, 2621456]
    assert lower["cost"] == 899999.3231131113
    assert math.nextafter(899999.710479665, math.inf) == 899999.7104796651


def test_five_million_small_value_window_does_not_expand():
    count = 312500
    segments = [(value, 1 + value * count, count) for value in range(16)]
    segments[-1] = (15, 1 + 15 * count, 5_000_000 - 15 * count)

    result = query_archive(segments, 1, 5_000_000, 8)

    assert len(result["breaks"]) == 9
    assert len(result["endpoints"]) == 8
    assert result["endpoints"][-1] == 5_000_000
