# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2020 Datadog, Inc.

"""Tests for exact quantile-difference bounds."""

from fractions import Fraction
from itertools import combinations_with_replacement

import pytest

from ddsketch import DDSketch
from ddsketch import LogCollapsingHighestDenseDDSketch
from ddsketch import LogCollapsingLowestDenseDDSketch
from ddsketch.bounds import aggregate_witness
from ddsketch.bounds import quantile_difference_bounds
from ddsketch.bounds import summarize_sketch
from ddsketch.bounds import verify_witness


def _summary(buckets, total_weight=None, zero_count=0, **extra):
    payload = {
        "buckets": [
            {"lower": lower, "upper": upper, "weight": weight, "key": key}
            for key, (lower, upper, weight) in enumerate(buckets)
        ],
        "total_weight": total_weight
        if total_weight is not None
        else sum(weight for _, _, weight in buckets),
        "sum": sum(
            value * weight
            for value, (_, _, weight) in zip(
                [bucket[0] for bucket in buckets], buckets
            )
        ),
        "min": buckets[0][0],
        "max": buckets[-1][1],
        "zero_count": zero_count,
    }
    payload.update(extra)
    return payload


def _brute(buckets, low, high, total_weight, exact_sum, true_min, true_max):
    low_rank = (low * (total_weight - 1)).numerator // (
        low * (total_weight - 1)
    ).denominator
    high_rank = (high * (total_weight - 1)).numerator // (
        high * (total_weight - 1)
    ).denominator
    choices = [
        list(combinations_with_replacement(range(lower, upper + 1), weight))
        for lower, upper, weight in buckets
    ]
    differences = set()
    low_values = set()
    high_values = set()

    def visit(index, values):
        if index == len(buckets):
            if (
                sum(values) == exact_sum
                and min(values) == true_min
                and max(values) == true_max
            ):
                ordered = sorted(values)
                low_values.add(ordered[low_rank])
                high_values.add(ordered[high_rank])
                differences.add(ordered[high_rank] - ordered[low_rank])
            return
        for choice in choices[index]:
            visit(index + 1, list(values) + list(choice))

    visit(0, [])
    return {
        "differences": (min(differences), max(differences)),
        "low": (min(low_values), max(low_values)),
        "high": (min(high_values), max(high_values)),
    }


def test_sample_rank_matches_library_convention():
    summary = {
        "buckets": [
            {"lower": 0, "upper": 0, "weight": 1},
            {"lower": 10, "upper": 10, "weight": 9},
        ],
        "total_weight": 10,
        "sum": 90,
        "min": 0,
        "max": 10,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.low.rank == 0
    assert result.high.rank == 8
    assert result.feasible
    assert result.minimum == 10
    assert result.maximum == 10


def test_single_bucket_independent_extremes_are_infeasible_together():
    summary = {
        "buckets": [{"lower": 0, "upper": 5, "weight": 10}],
        "total_weight": 10,
        "sum": 30,
        "min": 0,
        "max": 5,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    assert result.low.rank == 0
    assert result.high.rank == 8
    # Rank 0 is the true minimum, so the highest rank is bounded jointly.
    assert (result.minimum_units, result.maximum_units) == (4, 5)
    assert result.minimum == 4
    assert result.maximum == 5
    assert verify_witness(summary, result.minimum_witness)
    assert verify_witness(summary, result.maximum_witness)

    # Independent per-quantile extremes would allow 0 and 5, a difference of
    # 5, which is attainable; but a pair of independent maxima at 5 and 5
    # would be feasible while some mixed extremes are not.
    aggregate = aggregate_witness(result.minimum_witness)
    assert aggregate["sum"] == 30
    assert aggregate["min"] == 0
    assert aggregate["max"] == 5
    assert sum(aggregate["value_weights"].values()) == 10


def test_same_observation_has_zero_difference():
    summary = {
        "buckets": [{"lower": -4, "upper": 9, "weight": 11}],
        "total_weight": 11,
        "sum": 27,
        "min": -4,
        "max": 9,
    }
    result = quantile_difference_bounds(summary, 0.2, 0.2)
    assert result.low.rank == result.high.rank
    assert result.minimum == 0
    assert result.maximum == 0


def test_zero_and_negative_buckets_round_trip():
    sketch = DDSketch(0.05)
    for value, weight in [(0, 3), (-2, 2), (1, 2), (5, 4)]:
        sketch.add(value, weight)
    summary = summarize_sketch(sketch)
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    assert verify_witness(summary, result.minimum_witness)
    assert verify_witness(summary, result.maximum_witness)
    assert result.total_weight == 11
    assert result.zero_count == 3


def test_large_weights_are_counted_not_expanded():
    ten_million = 10 ** 7
    summary = {
        "buckets": [
            {"lower": 0, "upper": 100, "weight": ten_million},
            {"lower": 1000, "upper": 1100, "weight": ten_million},
        ],
        "total_weight": 2 * ten_million,
        "sum": ten_million * 50 + ten_million * 1050,
        "min": 0,
        "max": 1100,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    # Witnesses list counts, so size depends on distinct values, not weight.
    assert len(result.minimum_witness) == 2
    minimum_counts = sum(
        len(item["values"]) for item in result.minimum_witness
    )
    assert minimum_counts <= 10
    assert verify_witness(summary, result.minimum_witness)
    assert verify_witness(summary, result.maximum_witness)


def test_collapsed_low_bucket_uses_the_folded_boundary():
    sketch = LogCollapsingLowestDenseDDSketch(0.05, bin_limit=8)
    for value in [1, 2, 100, 1000, 10000, 100000]:
        sketch.add(value)
    summary = summarize_sketch(sketch)
    assert summary["collapsed_low"] is True
    collapsed = [
        bucket for bucket in summary["buckets"] if bucket["collapsed_low"]
    ]
    assert len(collapsed) == 1
    assert collapsed[0]["lower_units"] == 1
    assert collapsed[0]["upper_units"] > collapsed[0]["lower_units"]
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    assert result.maximum_units > 1000
    assert verify_witness(summary, result.minimum_witness)
    assert verify_witness(summary, result.maximum_witness)


def test_collapsed_high_bucket_uses_the_folded_boundary():
    sketch = LogCollapsingHighestDenseDDSketch(0.05, bin_limit=8)
    for value in [1, 2, 100, 1000, 10000, 100000]:
        sketch.add(value)
    summary = summarize_sketch(sketch)
    assert summary["collapsed_high"] is True
    collapsed = [
        bucket for bucket in summary["buckets"] if bucket["collapsed_high"]
    ]
    assert len(collapsed) == 1
    assert collapsed[0]["upper_units"] == 100000
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    assert result.maximum_units >= result.minimum_units


def test_collapsed_bucket_requires_folded_bounds():
    with pytest.raises(ValueError):
        quantile_difference_bounds(
            {
                "buckets": [
                    {
                        "key": 5,
                        "weight": 2,
                        "collapsed_low": True,
                    }
                ],
                "gamma": 1.1,
                "total_weight": 2,
                "sum": 3,
                "min": 1,
                "max": 2,
            },
            0.1,
            0.9,
        )


def test_contradictory_constraints_report_the_group():
    summary = {
        "buckets": [
            {"key": 0, "lower": 0, "upper": 1, "weight": 4},
            {"key": 1, "lower": 10, "upper": 11, "weight": 1},
        ],
        "total_weight": 5,
        "sum": 30,
        "min": 0,
        "max": 11,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible is False
    assert result.minimum is None and result.maximum is None
    assert result.conflicts
    conflict = result.conflicts[0]
    assert conflict["kind"] == "sum_range"
    assert conflict["sum_units"] == 30
    assert conflict["buckets"][0]["key"] == 0
    assert conflict["buckets"][1]["key"] == 1


def test_weight_mismatch_is_reported():
    summary = {
        "buckets": [{"lower": 0, "upper": 1, "weight": 2}],
        "total_weight": 5,
        "sum": 1,
        "min": 0,
        "max": 1,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible is False
    assert result.conflicts[0]["kind"] == "total_weight"


def test_unit_lattice_uses_exact_integers():
    summary = {
        "unit": Fraction(1, 4),
        "buckets": [
            {"lower_units": 0, "upper_units": 4, "weight": 2},
            {"lower_units": 40, "upper_units": 44, "weight": 2},
        ],
        "total_weight": 4,
        "sum_units": 88,
        "min_units": 0,
        "max_units": 44,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    assert (result.minimum_units, result.maximum_units) == (40, 44)
    assert result.minimum == 10
    assert isinstance(result.minimum, Fraction)
    assert verify_witness(summary, result.minimum_witness)


def test_witness_counts_match_all_buckets():
    summary = {
        "buckets": [
            {"lower": -3, "upper": -1, "weight": 3},
            {"lower": 2, "upper": 4, "weight": 2},
            {"lower": 10, "upper": 12, "weight": 2},
        ],
        "total_weight": 7,
        "sum": 20,
        "min": -3,
        "max": 12,
    }
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    assert result.feasible
    for witness in (result.minimum_witness, result.maximum_witness):
        aggregate = aggregate_witness(witness)
        assert aggregate["total_weight"] == 7
        assert aggregate["sum"] == 20
        assert aggregate["min"] == -3
        assert aggregate["max"] == 12
        assert [row["weight"] for row in aggregate["buckets"]] == [3, 2, 2]


def test_random_small_summaries_match_brute_force():
    cases = [
        (
            [(-2, 1, 3), (2, 5, 1), (6, 7, 1)],
            Fraction(0),
            Fraction(7, 10),
            7,
            -1,
            6,
        ),
        ([(0, 5, 5)], Fraction(1, 10), Fraction(9, 10), 15, 0, 5),
        (
            [(-4, -2, 2), (-1, 1, 2), (2, 4, 2)],
            Fraction(1, 5),
            Fraction(4, 5),
            0,
            -4,
            4,
        ),
    ]
    for buckets, low, high, exact_sum, true_min, true_max in cases:
        total_weight = sum(weight for _, _, weight in buckets)
        summary = {
            "buckets": [
                {
                    "lower": lower,
                    "upper": upper,
                    "weight": weight,
                    "key": index,
                }
                for index, (lower, upper, weight) in enumerate(buckets)
            ],
            "total_weight": total_weight,
            "sum": exact_sum,
            "min": true_min,
            "max": true_max,
        }
        result = quantile_difference_bounds(summary, low, high)
        expected = _brute(
            buckets, low, high, total_weight, exact_sum, true_min, true_max
        )
        assert result.feasible
        assert (result.minimum_units, result.maximum_units) == expected[
            "differences"
        ]
        assert (
            int(result.low.minimum),
            int(result.low.maximum),
        ) == expected["low"]
        assert (
            int(result.high.minimum),
            int(result.high.maximum),
        ) == expected["high"]
        assert verify_witness(summary, result.minimum_witness)
        assert verify_witness(summary, result.maximum_witness)


def test_library_approximation_is_preserved():
    sketch = DDSketch(0.05)
    for value in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
        sketch.add(value)
    before = [sketch.get_quantile_value(q) for q in (0.1, 0.5, 0.9)]
    summary = summarize_sketch(sketch)
    result = quantile_difference_bounds(summary, 0.1, 0.9)
    after = [sketch.get_quantile_value(q) for q in (0.1, 0.5, 0.9)]
    assert before == after
    assert result.low.representative is not None
    assert result.high.representative is not None
    assert result.representative_difference is not None
