# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2020 Datadog, Inc.

"""Exact attainable quantile bounds from a DDSketch bucket summary.

A DDSketch summary stores integer bucket keys and integer weights only.  The
original observations are unknown; all that is known is that each observation
lies in the value interval attached to its bucket.  This module computes the
smallest and largest attainable difference between two quantiles under this
library's sample-rank convention::

    rank(q) = floor(q * (N - 1))

where ``N`` is the total integer weight and the observation at a rank is the
``rank + 1``-th smallest observation (ties included).

Weights are integer multiplicities.  No function here expands a weight into
repeated observations, so summaries with very large weights are processed in
time proportional to the number of buckets, not the number of observations.

The two target ranks are optimized jointly.  When they fall in the same old
bucket, taking each quantile's independent minimum and maximum can produce an
infeasible pair: the two ranks must share one bucket count and the whole
stream must still reproduce the supplied exact sum, true minimum and true
maximum.  If both ranks are in fact the same observation, the difference is
exactly zero.

Collapsed tails are treated as wide intervals.  A collapsed bucket says only
that every observation in the bucket came from somewhere in the old key range;
it does not say that observations are concentrated at the bucket
representative.
"""

from bisect import bisect_right
from fractions import Fraction
import json

import six


__all__ = [
    "QuantileBounds",
    "QuantileDifferenceBounds",
    "aggregate_witness",
    "bucket_summary",
    "describe_sketch",
    "get_quantile_difference_bounds",
    "quantile_difference_bounds",
    "summarize_sketch",
    "verify_witness",
]


class QuantileBounds(object):
    """Information about one of the two requested quantiles.

    Attributes:
        quantile (Fraction): requested quantile.
        rank (int): zero-based sample rank, ``floor(q * (N - 1))``.
        minimum: smallest attainable value at that rank in a joint solution.
        maximum: largest attainable value at that rank in a joint solution.
        representative: original library approximation, when a bucket
            representative was supplied.
        bucket_index: index into the normalized bucket list containing the
            rank.
    """

    def __init__(
        self,
        quantile,
        rank,
        minimum,
        maximum,
        representative=None,
        bucket_index=None,
    ):
        self.quantile = quantile
        self.rank = rank
        self.minimum = minimum
        self.maximum = maximum
        self.representative = representative
        self.bucket_index = bucket_index

    @property
    def min(self):
        return self.minimum

    @property
    def max(self):
        return self.maximum

    def __repr__(self):
        return (
            "QuantileBounds(quantile={!r}, rank={!r}, minimum={!r}, "
            "maximum={!r}, representative={!r})"
        ).format(
            self.quantile,
            self.rank,
            self.minimum,
            self.maximum,
            self.representative,
        )


class QuantileDifferenceBounds(object):
    """Attainable bounds for ``value(high) - value(low)``.

    Public numeric values are exact ``Fraction`` objects (integers when the
    unit is one).  The ``*_units`` attributes hold integer lattice
    coordinates, so a reader can re-aggregate witnesses without floating
    point error.

    Attributes:
        low, high (QuantileBounds): per-quantile information.
        minimum, maximum: attainable difference bounds in physical units.
        minimum_units, maximum_units: the same bounds in unit coordinates.
        minimum_witness, maximum_witness: lists of per-bucket assignments
            realizing the corresponding bound.  Each entry carries integer
            counts for each value; weights are never expanded.
        value_weights_minimum, value_weights_maximum: value -> integer weight
            mappings for the two witnesses.
        feasible (bool): whether the supplied constraints are consistent.
        errors (list): human-readable constraint violations.
        conflicts (list): structured descriptions of the conflicting old
            records (bucket keys, intervals, weight groups).
        total_weight, zero_count, exact_sum, true_min, true_max, unit.
    """

    def __init__(self, **kwargs):
        for name, value in kwargs.items():
            setattr(self, name, value)

    @property
    def min(self):
        return self.minimum

    @property
    def max(self):
        return self.maximum

    @property
    def min_difference(self):
        return self.minimum

    @property
    def max_difference(self):
        return self.maximum

    @property
    def min_witness(self):
        return self.minimum_witness

    @property
    def max_witness(self):
        return self.maximum_witness

    @property
    def ok(self):
        return self.feasible

    def __repr__(self):
        return (
            "QuantileDifferenceBounds(minimum={!r}, maximum={!r}, "
            "feasible={!r}, errors={!r})"
        ).format(self.minimum, self.maximum, self.feasible, self.errors)

    def to_dict(self):
        """Return a JSON-serializable representation."""

        def plain(value):
            if value is None:
                return None
            if isinstance(value, Fraction):
                if value.denominator == 1:
                    return value.numerator
                return float(value)
            return value

        def quantile_dict(bounds):
            if bounds is None:
                return None
            return {
                "quantile": plain(bounds.quantile),
                "rank": bounds.rank,
                "minimum": plain(bounds.minimum),
                "maximum": plain(bounds.maximum),
                "representative": plain(bounds.representative),
                "bucket_index": bounds.bucket_index,
            }

        def weight_map(mapping):
            if mapping is None:
                return None
            return {
                str(plain(value)): weight
                for value, weight in sorted(
                    mapping.items(), key=lambda pair: pair[0]
                )
            }

        def witness_list(witness):
            if witness is None:
                return None
            result = []
            for item in witness:
                result.append(
                    {
                        "bucket_index": item["bucket_index"],
                        "source_index": item.get("source_index"),
                        "key": item["key"],
                        "lower": plain(item["lower"]),
                        "upper": plain(item["upper"]),
                        "weight": item["weight"],
                        "values": weight_map(item["values"]),
                        "value_units": {
                            str(value): weight
                            for value, weight in sorted(
                                item["value_units"].items()
                            )
                        },
                    }
                )
            return result

        return {
            "minimum": plain(self.minimum),
            "maximum": plain(self.maximum),
            "minimum_units": self.minimum_units,
            "maximum_units": self.maximum_units,
            "feasible": self.feasible,
            "errors": list(self.errors),
            "conflicts": list(self.conflicts),
            "low": quantile_dict(self.low),
            "high": quantile_dict(self.high),
            "representative_difference": plain(
                self.representative_difference
            ),
            "minimum_witness": witness_list(self.minimum_witness),
            "maximum_witness": witness_list(self.maximum_witness),
            "value_weights_minimum": weight_map(
                self.value_weights_minimum
            ),
            "value_weights_maximum": weight_map(
                self.value_weights_maximum
            ),
            "value_units_minimum": {
                str(value): weight
                for value, weight in sorted(
                    (self.value_units_minimum or {}).items()
                )
            },
            "value_units_maximum": {
                str(value): weight
                for value, weight in sorted(
                    (self.value_units_maximum or {}).items()
                )
            },
            "total_weight": self.total_weight,
            "zero_count": self.zero_count,
            "exact_sum": plain(self.exact_sum),
            "exact_sum_units": self.exact_sum_units,
            "true_min": plain(self.true_min),
            "true_max": plain(self.true_max),
            "true_min_units": self.true_min_units,
            "true_max_units": self.true_max_units,
            "unit": plain(self.unit),
            "collapsed_low": self.collapsed_low,
            "collapsed_high": self.collapsed_high,
            "collapsed_boundaries": self.collapsed_boundaries,
        }


def _as_fraction(value, name="value"):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise TypeError("%s must be numeric, got bool" % name)
    if isinstance(value, six.integer_types):
        return Fraction(value, 1)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("%s must be finite, got %r" % (name, value))
        return Fraction(str(value))
    if isinstance(value, six.string_types):
        try:
            return Fraction(value.strip())
        except (ValueError, ZeroDivisionError):
            raise ValueError(
                "cannot interpret %r as a number for %s" % (value, name)
            )
    try:
        return Fraction(value)
    except (TypeError, ValueError):
        raise TypeError(
            "cannot interpret %r as a number for %s" % (value, name)
        )


def _coerce_int(value, name):
    if isinstance(value, bool):
        raise TypeError("%s must be an integer, got bool" % name)
    if isinstance(value, six.integer_types):
        return int(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("%s must be finite" % name)
        if not value.is_integer():
            raise ValueError(
                "%s must be an exact integer, got %r" % (name, value)
            )
        return int(value)
    if isinstance(value, Fraction):
        if value.denominator != 1:
            raise ValueError(
                "%s must be an exact integer, got %r" % (name, value)
            )
        return int(value.numerator)
    if isinstance(value, six.string_types):
        try:
            return int(value.strip(), 10)
        except ValueError:
            raise ValueError("%s must be an integer, got %r" % (name, value))
    raise TypeError("%s must be an integer, got %r" % (name, value))


def _physical_to_units(value, unit, name):
    fraction = _as_fraction(value, name) / unit
    if fraction.denominator != 1:
        raise ValueError(
            "%s=%r is not an exact multiple of unit=%r"
            % (name, value, unit)
        )
    return int(fraction.numerator)


def _floor_fraction(value):
    return value.numerator // value.denominator


def _ceil_fraction(value):
    return -((-value.numerator) // value.denominator)


def _power(gamma, exponent):
    if exponent >= 0:
        return gamma ** exponent
    return Fraction(1, 1) / (gamma ** (-exponent))


def _bounds_to_units(lower, upper, unit, lower_open, upper_open, label):
    """Convert physical bucket endpoints to inclusive integer unit bounds.

    DDSketch positive buckets are lower-open and upper-closed; negative
    buckets are the reverse.  Callers may override the openness explicitly.
    Outward rounding preserves every possible old value.
    """
    lower_value = _as_fraction(lower, label + ".lower") / unit
    upper_value = _as_fraction(upper, label + ".upper") / unit
    if lower_open:
        lower_units = _floor_fraction(lower_value) + 1
    else:
        lower_units = _ceil_fraction(lower_value)
    if upper_open:
        upper_units = _ceil_fraction(upper_value) - 1
    else:
        upper_units = _floor_fraction(upper_value)
    return lower_units, upper_units


def _raw_bounds(raw, index, unit, gamma):
    """Return exact integer unit-coordinate bounds for one raw bucket."""
    lower_open = bool(raw.get("lower_open", raw.get("lower_exclusive", False)))
    upper_open = bool(raw.get("upper_open", raw.get("upper_exclusive", False)))
    if "lower_units" in raw and "upper_units" in raw:
        return (
            _coerce_int(raw["lower_units"], "bucket.lower_units"),
            _coerce_int(raw["upper_units"], "bucket.upper_units"),
        )
    if "lower" in raw and "upper" in raw:
        return _bounds_to_units(
            raw["lower"], raw["upper"], unit, lower_open, upper_open,
            "bucket",
        )
    if "min_units" in raw and "max_units" in raw:
        return (
            _coerce_int(raw["min_units"], "bucket.min_units"),
            _coerce_int(raw["max_units"], "bucket.max_units"),
        )
    if "min" in raw and "max" in raw:
        return _bounds_to_units(
            raw["min"], raw["max"], unit, lower_open, upper_open, "bucket"
        )
    if gamma is not None and "key" in raw:
        key = _coerce_int(raw["key"], "bucket.key")
        sign = _coerce_int(raw.get("sign", 1), "bucket.sign")
        if sign not in (-1, 1):
            raise ValueError("bucket.sign must be -1 or 1")
        if sign > 0:
            lower = _power(gamma, key - 1) / unit
            upper = _power(gamma, key) / unit
            # Positive buckets are (lower, upper].
            return _floor_fraction(lower) + 1, _floor_fraction(upper)
        magnitude = key
        lower = -_power(gamma, magnitude) / unit
        upper = -_power(gamma, magnitude - 1) / unit
        # Negative buckets are [lower, upper).
        return _ceil_fraction(lower), _ceil_fraction(upper) - 1
    raise ValueError(
        "bucket %d must provide exact lower/upper bounds (or key plus gamma)"
        % index
    )


def _normalize_buckets(raw_buckets, unit, gamma):
    buckets = []
    for index, raw in enumerate(raw_buckets):
        if isinstance(raw, dict):
            weight_value = raw.get(
                "weight",
                raw.get("count", raw.get("multiplicity", raw.get("cnt"))),
            )
            if weight_value is None:
                raise ValueError(
                    "bucket %d is missing weight/count" % index
                )
            key = raw.get("key")
            representative = raw.get(
                "representative",
                raw.get("value", raw.get("representative_value")),
            )
            collapsed_low = bool(
                raw.get(
                    "collapsed_low",
                    raw.get("folded_low", raw.get("collapsed_lower", False)),
                )
            )
            collapsed_high = bool(
                raw.get(
                    "collapsed_high",
                    raw.get("folded_high", raw.get("collapsed_upper", False)),
                )
            )
            old_lower = raw.get(
                "old_lower",
                raw.get(
                    "original_lower",
                    raw.get("folded_lower", raw.get("collapsed_lower_bound")),
                ),
            )
            old_upper = raw.get(
                "old_upper",
                raw.get(
                    "original_upper",
                    raw.get("folded_upper", raw.get("collapsed_upper_bound")),
                ),
            )
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            raw = list(raw)
            weight_value = raw[2]
            key = raw[3] if len(raw) > 3 else None
            representative = None
            collapsed_low = False
            collapsed_high = False
            old_lower = None
            old_upper = None
            raw = {
                "lower": raw[0],
                "upper": raw[1],
                "weight": weight_value,
                "key": key,
            }
        else:
            raise TypeError(
                "bucket %d must be a mapping or a sequence of at least "
                "three values" % index
            )

        weight = _coerce_int(weight_value, "bucket.weight")
        if weight < 0:
            raise ValueError(
                "bucket %d has negative weight %r" % (index, weight)
            )
        lower, upper = _raw_bounds(raw, index, unit, gamma)
        if old_lower is not None or old_upper is not None:
            if old_lower is None or old_upper is None:
                raise ValueError(
                    "bucket %d must provide both old_lower and old_upper"
                    % index
                )
            lower, upper = _bounds_to_units(
                old_lower, old_upper, unit, lower_open, upper_open,
                "bucket.old",
            )
        if old_lower is None and (collapsed_low or collapsed_high):
            if "lower" not in raw and "upper" not in raw and (
                "lower_units" not in raw and "upper_units" not in raw
            ):
                raise ValueError(
                    "bucket %d is marked collapsed but has no actual folded "
                    "bounds; supply lower/upper or old_lower/old_upper"
                    % index
                )
        if lower > upper:
            raise ValueError(
                "bucket %d has lower %r greater than upper %r"
                % (index, lower, upper)
            )
        if key is not None:
            key = _coerce_int(key, "bucket.key")
        if representative is not None:
            representative = _as_fraction(
                representative, "bucket.representative"
            )
        buckets.append(
            {
                "source_index": index,
                "key": key,
                "lower": lower,
                "upper": upper,
                "weight": weight,
                "representative": representative,
                "collapsed_low": collapsed_low,
                "collapsed_high": collapsed_high,
                "synthetic_zero": False,
            }
        )
    buckets.sort(
        key=lambda item: (
            item["lower"],
            item["upper"],
            item["key"] if item["key"] is not None else 0,
            item["source_index"],
        )
    )
    for index, bucket in enumerate(buckets):
        bucket["index"] = index
    return buckets


def _bucket_minmax(bucket, slots):
    """Exact minimum and maximum sums for one bucket with fixed slots.

    ``slots`` is a sorted sequence of ``(relative_position, value)``.
    """
    lower, upper, weight = bucket["lower"], bucket["upper"], bucket["weight"]
    if not slots:
        return lower * weight, upper * weight

    count = len(slots)
    minimum = slots[0][0] * lower
    for index in range(count - 1):
        minimum += (slots[index + 1][0] - slots[index][0]) * slots[index][1]
    minimum += (weight - slots[-1][0]) * slots[-1][1]

    maximum = (slots[0][0] + 1) * slots[0][1]
    for index in range(1, count):
        maximum += (slots[index][0] - slots[index - 1][0]) * slots[index][1]
    maximum += (weight - 1 - slots[-1][0]) * upper
    return minimum, maximum


def _bucket_affine(bucket, slots):
    """Affine minimum and maximum bucket sums in two free rank values.

    Each slot value is ``(constant, low_coefficient, high_coefficient)``.
    """
    lower, upper, weight = bucket["lower"], bucket["upper"], bucket["weight"]
    if not slots:
        return (lower * weight, 0, 0), (upper * weight, 0, 0)

    count = len(slots)
    minimum = [slots[0][0] * lower, 0, 0]
    for index in range(count - 1):
        multiplier = slots[index + 1][0] - slots[index][0]
        for axis in range(3):
            minimum[axis] += multiplier * slots[index][1][axis]
    multiplier = weight - slots[-1][0]
    for axis in range(3):
        minimum[axis] += multiplier * slots[-1][1][axis]

    maximum = [(slots[0][0] + 1) * slots[0][1][axis] for axis in range(3)]
    for index in range(1, count):
        multiplier = slots[index][0] - slots[index - 1][0]
        for axis in range(3):
            maximum[axis] += multiplier * slots[index][1][axis]
    maximum[0] += (weight - 1 - slots[-1][0]) * upper
    return tuple(minimum), tuple(maximum)


def _bucket_affine_one(bucket, slots):
    """Affine minimum and maximum bucket sums in one free rank value."""
    lower, upper, weight = bucket["lower"], bucket["upper"], bucket["weight"]
    if not slots:
        return (lower * weight, 0), (upper * weight, 0)

    count = len(slots)
    minimum = [slots[0][0] * lower, 0]
    for index in range(count - 1):
        multiplier = slots[index + 1][0] - slots[index][0]
        for axis in range(2):
            minimum[axis] += multiplier * slots[index][1][axis]
    multiplier = weight - slots[-1][0]
    for axis in range(2):
        minimum[axis] += multiplier * slots[-1][1][axis]

    maximum = [(slots[0][0] + 1) * slots[0][1][axis] for axis in range(2)]
    for index in range(1, count):
        multiplier = slots[index][0] - slots[index - 1][0]
        for axis in range(2):
            maximum[axis] += multiplier * slots[index][1][axis]
    maximum[0] += (weight - 1 - slots[-1][0]) * upper
    return tuple(minimum), tuple(maximum)


def _rank_positions(buckets, total_weight, ranks):
    """Map each rank to ``(bucket_index, relative_position)``."""
    prefix = []
    running = 0
    for bucket in buckets:
        running += bucket["weight"]
        prefix.append(running)
    if running != total_weight:
        raise ValueError("bucket weights do not add up to total_weight")
    positions = {}
    for rank in ranks:
        if rank < 0 or rank >= total_weight:
            raise ValueError("rank %r is outside [0, %r)" % (rank, total_weight))
        index = bisect_right(prefix, rank)
        before = prefix[index - 1] if index else 0
        positions[rank] = (index, rank - before)
    return positions, prefix


def _slots_for(bucket_index, positions, true_min, true_max, low_rank,
               high_rank, low_value, high_value, affine):
    """Fixed values for one bucket.

    ``positions`` maps each relevant rank to ``(bucket, relative_position)``.
    """
    result = []
    seen = set()

    def add(position, value):
        if position in seen:
            return
        seen.add(position)
        result.append((position, value))

    zero = positions[0]
    last = positions["last"]
    if zero[0] == bucket_index:
        add(zero[1], (true_min, 0, 0) if affine else true_min)
    if last[0] == bucket_index:
        add(last[1], (true_max, 0, 0) if affine else true_max)
    if low_rank == high_rank:
        if low_rank != 0:
            bucket, relative = positions[low_rank]
            if bucket == bucket_index:
                add(relative, (0, 1, 0) if affine else low_value)
    else:
        if low_rank != 0:
            bucket, relative = positions[low_rank]
            if bucket == bucket_index:
                add(relative, (0, 1, 0) if affine else low_value)
        if high_rank != "last":
            bucket, relative = positions[high_rank]
            if bucket == bucket_index:
                add(relative, (0, 0, 1) if affine else high_value)
    return sorted(result, key=lambda item: item[0])


def _normalize_positions(raw_positions, total_weight):
    positions = {}
    for key, value in raw_positions.items():
        positions[key] = value
    positions["last"] = raw_positions[total_weight - 1]
    return positions


def _solve_one_variable(minimum, maximum, exact_sum, lower, upper):
    """Feasible integer interval for one free rank value."""
    lower_bound = lower
    upper_bound = upper

    if maximum[1] > 0:
        lower_bound = max(
            lower_bound,
            _ceil_fraction(Fraction(exact_sum - maximum[0], maximum[1])),
        )
    elif maximum[0] < exact_sum:
        return None

    if minimum[1] > 0:
        upper_bound = min(
            upper_bound,
            _floor_fraction(Fraction(exact_sum - minimum[0], minimum[1])),
        )
    elif minimum[0] > exact_sum:
        return None

    if lower_bound > upper_bound:
        return None
    return lower_bound, upper_bound


def _vb_interval(minimum, maximum, exact_sum, low_value, box_low, box_high):
    """Exact integer interval for the high rank value at a fixed low value."""
    lower = box_low
    upper = box_high

    if low_value > lower:
        lower = low_value

    if maximum[2] > 0:
        lower = max(
            lower,
            _ceil_fraction(
                Fraction(exact_sum - maximum[0] - maximum[1] * low_value,
                         maximum[2])
            ),
        )
    elif maximum[0] + maximum[1] * low_value < exact_sum:
        return None

    if minimum[2] > 0:
        upper = min(
            upper,
            _floor_fraction(
                Fraction(exact_sum - minimum[0] - minimum[1] * low_value,
                         minimum[2])
            ),
        )
    elif minimum[0] + minimum[1] * low_value > exact_sum:
        return None

    if lower > upper:
        return None
    return lower, upper


def _line_candidates(minimum, maximum, exact_sum, low_box, high_box):
    """Candidate low values covering all polygon vertices."""
    lines = [
        (1, 0, low_box[0]),
        (1, 0, low_box[1]),
        (0, 1, high_box[0]),
        (0, 1, high_box[1]),
        (-1, 1, 0),
        (minimum[1], minimum[2], exact_sum - minimum[0]),
        (maximum[1], maximum[2], exact_sum - maximum[0]),
    ]
    candidates = {low_box[0], low_box[1]}

    for first in lines:
        for second in lines:
            determinant = first[0] * second[1] - first[1] * second[0]
            if determinant == 0:
                continue
            value = Fraction(
                first[2] * second[1] - first[1] * second[2],
                determinant,
            )
            floor = _floor_fraction(value)
            for offset in range(-2, 3):
                candidates.add(floor + offset)
    for line in lines:
        if line[1] == 0 and line[0] != 0:
            value = Fraction(line[2], line[0])
            floor = _floor_fraction(value)
            for offset in range(-2, 3):
                candidates.add(floor + offset)
    return sorted(candidates)


def _solve_two_ranks(buckets, positions, total_weight, low_rank, high_rank,
                     true_min, true_max, exact_sum):
    """Return extreme feasible ``(low, high)`` pairs and their ranges."""
    low_bucket = buckets[positions[low_rank][0]]
    high_bucket = buckets[positions[high_rank][0]]
    low_box = (low_bucket["lower"], low_bucket["upper"])
    high_box = (high_bucket["lower"], high_bucket["upper"])
    if low_rank == 0:
        low_box = (true_min, true_min)
    if high_rank == total_weight - 1:
        high_box = (true_max, true_max)

    minimum = [0, 0, 0]
    maximum = [0, 0, 0]
    for bucket in buckets:
        slots = _slots_for(
            bucket["index"],
            positions,
            true_min,
            true_max,
            low_rank,
            high_rank,
            None,
            None,
            affine=True,
        )
        bucket_min, bucket_max = _bucket_affine(bucket, slots)
        for axis in range(3):
            minimum[axis] += bucket_min[axis]
            maximum[axis] += bucket_max[axis]
    minimum = tuple(minimum)
    maximum = tuple(maximum)

    best_min = None
    best_max = None
    low_min = None
    low_max = None
    high_min = None
    high_max = None

    for low_value in _line_candidates(
        minimum, maximum, exact_sum, low_box, high_box
    ):
        if not (low_box[0] <= low_value <= low_box[1]):
            continue
        interval = _vb_interval(
            minimum, maximum, exact_sum, low_value, high_box[0], high_box[1]
        )
        if interval is None:
            continue
        high_low, high_high = interval
        if low_min is None or low_value < low_min:
            low_min = low_value
        if low_max is None or low_value > low_max:
            low_max = low_value
        if high_min is None or high_low < high_min:
            high_min = high_low
        if high_max is None or high_high > high_max:
            high_max = high_high

        min_point = (low_value, high_low)
        max_point = (low_value, high_high)
        if best_min is None or (min_point[1] - min_point[0]) < (
            best_min[1] - best_min[0]
        ):
            best_min = min_point
        if best_max is None or (max_point[1] - max_point[0]) > (
            best_max[1] - best_max[0]
        ):
            best_max = max_point

    if best_min is None:
        return None
    return {
        "minimum": best_min,
        "maximum": best_max,
        "low_min": low_min,
        "low_max": low_max,
        "high_min": high_min,
        "high_max": high_max,
    }


def _solve_same_rank(buckets, positions, rank, true_min, true_max, exact_sum):
    """Feasible range for a single rank occurring once (difference zero)."""
    bucket = buckets[positions[rank][0]]
    lower, upper = bucket["lower"], bucket["upper"]
    is_first = positions[rank] == positions[0]
    is_last = positions[rank] == positions["last"]
    if is_first:
        lower = max(lower, true_min)
        upper = min(upper, true_min)
    if is_last:
        lower = max(lower, true_max)
        upper = min(upper, true_max)
    if is_first and is_last and true_min != true_max:
        return None

    minimum = [0, 0]
    maximum = [0, 0]
    for item in buckets:
        slots = []
        if positions[0][0] == item["index"]:
            slots.append((positions[0][1], (true_min, 0)))
        if positions["last"][0] == item["index"]:
            slots.append((positions["last"][1], (true_max, 0)))
        if rank != 0 and positions[rank][0] == item["index"]:
            slots.append((positions[rank][1], (0, 1)))
        slots.sort()
        # Deduplicate a rank that is also the global minimum.
        deduped = []
        for position, value in slots:
            if deduped and deduped[-1][0] == position:
                continue
            deduped.append((position, value))
        bucket_min, bucket_max = _bucket_affine_one(item, deduped)
        for axis in range(2):
            minimum[axis] += bucket_min[axis]
            maximum[axis] += bucket_max[axis]

    solved = _solve_one_variable(
        tuple(minimum), tuple(maximum), exact_sum, lower, upper
    )
    if solved is None:
        return None
    return solved


def _counts_for_bucket(bucket, slots, target):
    """Value -> integer count for one bucket without expanding weight."""
    lower, upper, weight = bucket["lower"], bucket["upper"], bucket["weight"]
    minimum, maximum = _bucket_minmax(bucket, slots)
    if not (minimum <= target <= maximum):
        raise ValueError(
            "bucket sum target %r is outside [%r, %r]"
            % (target, minimum, maximum)
        )

    counts = {}

    def add(value, count):
        if count:
            counts[value] = counts.get(value, 0) + count

    fixed = sorted(slots)
    runs = []
    first_position = fixed[0][0] if fixed else weight
    if first_position > 0:
        add(lower, first_position)
        ceiling = fixed[0][1] if fixed else upper
        runs.append((first_position, lower, ceiling))
    for index, (position, value) in enumerate(fixed):
        add(value, 1)
        next_position = (
            fixed[index + 1][0] if index + 1 < len(fixed) else weight
        )
        if next_position > position + 1:
            ceiling = (
                fixed[index + 1][1] if index + 1 < len(fixed) else upper
            )
            length = next_position - position - 1
            add(value, length)
            runs.append((length, value, ceiling))

    remaining = target - minimum
    for length, base, ceiling in reversed(runs):
        if remaining <= 0:
            break
        span = ceiling - base
        if span <= 0 or length <= 0:
            continue
        capacity = span * length
        take = min(remaining, capacity)
        full = take // span
        partial = take - full * span
        if full:
            add(ceiling, full)
            add(base, -full)
        if partial:
            add(base + partial, 1)
            add(base, -1)
        remaining -= take
    if remaining:
        raise ValueError("could not distribute bucket sum remainder")

    counts = {value: count for value, count in counts.items() if count}
    if not counts:
        raise ValueError("bucket assignment is empty")
    if sum(counts.values()) != weight:
        raise ValueError("bucket counts do not add up to weight")
    if sum(value * count for value, count in counts.items()) != target:
        raise ValueError("bucket counts have the wrong weighted sum")
    if min(counts) < lower or max(counts) > upper:
        raise ValueError("bucket counts leave the bucket interval")
    return counts


def _witness_for_point(buckets, positions, total_weight, low_rank, high_rank,
                       low_value, high_value, true_min, true_max, exact_sum,
                       unit):
    """Build a count-only witness realizing the chosen rank values."""
    slots = []
    for bucket in buckets:
        slots.append(
            _slots_for(
                bucket["index"],
                positions,
                true_min,
                true_max,
                low_rank,
                high_rank,
                low_value,
                high_value,
                affine=False,
            )
        )

    bounds = []
    minimum_total = 0
    maximum_total = 0
    for bucket, bucket_slots in zip(buckets, slots):
        minimum, maximum = _bucket_minmax(bucket, bucket_slots)
        bounds.append((minimum, maximum))
        minimum_total += minimum
        maximum_total += maximum
    if not (minimum_total <= exact_sum <= maximum_total):
        raise ValueError(
            "chosen rank values cannot reproduce the exact sum "
            "([%r, %r] does not contain %r)"
            % (minimum_total, maximum_total, exact_sum)
        )

    remaining = exact_sum - minimum_total
    targets = []
    for minimum, maximum in bounds:
        take = min(remaining, maximum - minimum)
        targets.append(minimum + take)
        remaining -= take
    if remaining:
        raise ValueError("could not distribute the exact sum")

    witness = []
    aggregate_units = {}
    for bucket, bucket_slots, target in zip(buckets, slots, targets):
        counts = _counts_for_bucket(bucket, bucket_slots, target)
        entry = {
            "bucket_index": bucket["index"],
            "source_index": bucket.get("source_index"),
            "key": bucket["key"],
            "lower": bucket["lower"] * unit,
            "upper": bucket["upper"] * unit,
            "weight": bucket["weight"],
            "values": {
                value * unit: count
                for value, count in sorted(counts.items())
            },
            "value_units": dict(sorted(counts.items())),
        }
        witness.append(entry)
        for value, count in counts.items():
            aggregate_units[value] = aggregate_units.get(value, 0) + count

    if sum(aggregate_units.values()) != total_weight:
        raise ValueError("witness total weight is wrong")
    if sum(value * count for value, count in aggregate_units.items()) != exact_sum:
        raise ValueError("witness aggregate sum is wrong")
    if min(aggregate_units) != true_min:
        raise ValueError("witness aggregate minimum is wrong")
    if max(aggregate_units) != true_max:
        raise ValueError("witness aggregate maximum is wrong")

    ordered_positions = _rank_positions(
        buckets, total_weight, [low_rank, high_rank]
    )[0]
    low_bucket, low_relative = ordered_positions[low_rank]
    high_bucket, high_relative = ordered_positions[high_rank]
    if low_rank != high_rank:
        low_counts = witness[low_bucket]["value_units"]
        high_counts = witness[high_bucket]["value_units"]
        low_observed = _value_at(low_counts, low_relative)
        high_observed = _value_at(high_counts, high_relative)
        if low_observed != low_value or high_observed != high_value:
            raise ValueError("witness does not realize the selected ranks")
    else:
        shared_counts = witness[low_bucket]["value_units"]
        shared_observed = _value_at(shared_counts, low_relative)
        if shared_observed != low_value:
            raise ValueError("witness does not realize the shared rank")

    aggregate_values = {
        value * unit: count for value, count in sorted(aggregate_units.items())
    }
    return witness, aggregate_values, dict(sorted(aggregate_units.items()))


def _value_at(counts, position):
    running = 0
    for value in sorted(counts):
        running += counts[value]
        if position < running:
            return value
    raise IndexError("position %r is outside the bucket" % position)


class _Problem(object):
    def __init__(self, summary, low, high):
        self.errors = []
        self.conflicts = []

        unit = summary.get(
            "unit",
            summary.get(
                "quantum",
                summary.get(
                    "minimum_unit",
                    summary.get(
                        "resolution",
                        summary.get("smallest_unit", summary.get("lsb", 1)),
                    ),
                ),
            ),
        )
        self.unit = _as_fraction(unit, "unit")
        if self.unit <= 0:
            raise ValueError("unit must be positive, got %r" % unit)

        gamma = summary.get("gamma", summary.get("bucket_gamma"))
        if gamma is None and "relative_accuracy" in summary:
            accuracy = _as_fraction(
                summary["relative_accuracy"], "relative_accuracy"
            )
            gamma = (1 + accuracy) / (1 - accuracy)
        if gamma is not None:
            gamma = _as_fraction(gamma, "gamma")
            if gamma <= 1:
                raise ValueError("gamma must be greater than 1")
        self.gamma = gamma

        raw_buckets = summary.get("buckets")
        if raw_buckets is None:
            raise ValueError("summary is missing buckets")
        self.buckets = _normalize_buckets(raw_buckets, self.unit, gamma)

        for candidate in (
            "total_weight",
            "total_count",
            "weights_total",
            "count",
            "N",
            "n",
        ):
            if candidate in summary:
                weight_key = candidate
                break
        else:
            raise ValueError("summary is missing total_weight/count")
        self.total_weight = _coerce_int(summary[weight_key], weight_key)
        if self.total_weight < 0:
            raise ValueError("total_weight must be non-negative")

        self.exact_sum = self._read_integer(
            summary,
            ("sum_units", "exact_sum_units", "signed_sum_units"),
            (
                "sum",
                "exact_sum",
                "signed_sum",
                "exact_signed_sum",
            ),
            "sum",
        )
        self.true_min = self._read_integer(
            summary,
            ("min_units", "true_min_units", "lower_units"),
            (
                "min",
                "true_min",
                "true_lower",
                "lower",
                "minimum",
                "min_value",
            ),
            "min",
        )
        self.true_max = self._read_integer(
            summary,
            ("max_units", "true_max_units", "upper_units"),
            (
                "max",
                "true_max",
                "true_upper",
                "upper",
                "maximum",
                "max_value",
            ),
            "max",
        )

        zero_count = summary.get("zero_count", summary.get("zeros", 0))
        self.zero_count = _coerce_int(zero_count, "zero_count")
        if self.zero_count < 0:
            raise ValueError("zero_count must be non-negative")

        self.collapsed_low = summary.get("collapsed_low")
        self.collapsed_high = summary.get("collapsed_high")
        self.collapsed_boundaries = summary.get("collapsed_boundaries")

        self.low = low
        self.high = high
        self.low_rank = _floor_fraction(low * (self.total_weight - 1))
        self.high_rank = _floor_fraction(high * (self.total_weight - 1))

        self._normalize()

    def _read_integer(self, summary, unit_keys, physical_keys, label):
        for key in unit_keys:
            if key in summary:
                return _coerce_int(summary[key], key)
        for key in physical_keys:
            if key in summary:
                return _physical_to_units(summary[key], self.unit, label)
        raise ValueError("summary is missing %s" % label)

    def _conflict(self, kind, message, **details):
        self.errors.append(message)
        entry = {"kind": kind}
        entry.update(details)
        self.conflicts.append(entry)

    def _normalize(self):
        if self.true_min > self.true_max:
            self._conflict(
                "true_extrema",
                "true_min %r is greater than true_max %r"
                % (self.true_min, self.true_max),
                true_min_units=self.true_min,
                true_max_units=self.true_max,
            )

        bucket_weight = sum(bucket["weight"] for bucket in self.buckets)
        if bucket_weight + self.zero_count != self.total_weight:
            self._conflict(
                "total_weight",
                "bucket weights plus zero_count (%d + %d) do not equal "
                "total_weight %d"
                % (bucket_weight, self.zero_count, self.total_weight),
                bucket_weight=bucket_weight,
                zero_count=self.zero_count,
                total_weight=self.total_weight,
                buckets=[
                    {
                        "index": bucket["source_index"],
                        "key": bucket["key"],
                        "weight": bucket["weight"],
                    }
                    for bucket in self.buckets
                    if bucket["weight"]
                ],
            )

        if self.zero_count:
            self.buckets.append(
                {
                    "source_index": -1,
                    "key": 0,
                    "lower": 0,
                    "upper": 0,
                    "weight": self.zero_count,
                    "representative": Fraction(0),
                    "collapsed_low": False,
                    "collapsed_high": False,
                    "synthetic_zero": True,
                }
            )
        self.buckets.sort(
            key=lambda item: (
                item["lower"],
                item["upper"],
                item["key"] if item["key"] is not None else 0,
                item["source_index"],
            )
        )
        for index, bucket in enumerate(self.buckets):
            bucket["index"] = index

        if self.total_weight == 0:
            if self.exact_sum != 0:
                self._conflict(
                    "empty_sum",
                    "an empty summary must have sum 0",
                    sum_units=self.exact_sum,
                )
            return

        previous = None
        for bucket in self.buckets:
            if not bucket["weight"]:
                continue
            if bucket["lower"] > bucket["upper"]:
                self._conflict(
                    "bucket_interval",
                    "bucket at key %r has an empty interval [%r, %r]"
                    % (bucket["key"], bucket["lower"], bucket["upper"]),
                    key=bucket["key"],
                    lower_units=bucket["lower"],
                    upper_units=bucket["upper"],
                    weight=bucket["weight"],
                )
            if previous is not None and previous["upper"] > bucket["lower"]:
                self._conflict(
                    "bucket_overlap",
                    "bucket intervals overlap: key %r ends at %r but key %r "
                    "starts at %r"
                    % (
                        previous["key"],
                        previous["upper"],
                        bucket["key"],
                        bucket["lower"],
                    ),
                    first={
                        "key": previous["key"],
                        "lower_units": previous["lower"],
                        "upper_units": previous["upper"],
                        "weight": previous["weight"],
                    },
                    second={
                        "key": bucket["key"],
                        "lower_units": bucket["lower"],
                        "upper_units": bucket["upper"],
                        "weight": bucket["weight"],
                    },
                )
            previous = bucket

        occupied = [bucket for bucket in self.buckets if bucket["weight"]]
        if not occupied:
            self._conflict(
                "empty_buckets",
                "total_weight is positive but every bucket is empty",
                total_weight=self.total_weight,
            )
            return

        first = occupied[0]
        last = occupied[-1]
        if not (first["lower"] <= self.true_min <= first["upper"]):
            self._conflict(
                "true_min_bucket",
                "true_min %r is not inside the lowest occupied bucket "
                "interval [%r, %r] (key %r)"
                % (
                    self.true_min,
                    first["lower"],
                    first["upper"],
                    first["key"],
                ),
                true_min_units=self.true_min,
                bucket={
                    "key": first["key"],
                    "lower_units": first["lower"],
                    "upper_units": first["upper"],
                    "weight": first["weight"],
                },
            )
        if not (last["lower"] <= self.true_max <= last["upper"]):
            self._conflict(
                "true_max_bucket",
                "true_max %r is not inside the highest occupied bucket "
                "interval [%r, %r] (key %r)"
                % (
                    self.true_max,
                    last["lower"],
                    last["upper"],
                    last["key"],
                ),
                true_max_units=self.true_max,
                bucket={
                    "key": last["key"],
                    "lower_units": last["lower"],
                    "upper_units": last["upper"],
                    "weight": last["weight"],
                },
            )

        implied_sum_min = sum(
            bucket["lower"] * bucket["weight"] for bucket in self.buckets
        )
        implied_sum_max = sum(
            bucket["upper"] * bucket["weight"] for bucket in self.buckets
        )
        if not (implied_sum_min <= self.exact_sum <= implied_sum_max):
            self._conflict(
                "sum_range",
                "exact_sum %r is outside the bucket-implied range [%r, %r]"
                % (self.exact_sum, implied_sum_min, implied_sum_max),
                sum_units=self.exact_sum,
                implied_min_units=implied_sum_min,
                implied_max_units=implied_sum_max,
                buckets=[
                    {
                        "key": bucket["key"],
                        "lower_units": bucket["lower"],
                        "upper_units": bucket["upper"],
                        "weight": bucket["weight"],
                    }
                    for bucket in self.buckets
                    if bucket["weight"]
                ],
            )

    def feasible(self):
        return not self.errors

    def clamp(self):
        """Intersect every bucket interval with the true extrema.

        The true minimum and maximum are observations, so no value can lie
        outside ``[true_min, true_max]``.  This narrows wide DDSketch bucket
        intervals without discarding any feasible old value.
        """
        for bucket in self.buckets:
            bucket["lower"] = max(bucket["lower"], self.true_min)
            bucket["upper"] = min(bucket["upper"], self.true_max)
            if bucket["weight"] and bucket["lower"] > bucket["upper"]:
                self._conflict(
                    "bucket_outside_true_bounds",
                    "bucket at key %r is empty after intersecting with "
                    "[%r, %r]"
                    % (bucket["key"], self.true_min, self.true_max),
                    key=bucket["key"],
                    lower_units=bucket["lower"],
                    upper_units=bucket["upper"],
                    true_min_units=self.true_min,
                    true_max_units=self.true_max,
                    weight=bucket["weight"],
                )
        self.buckets.sort(
            key=lambda item: (
                item["lower"],
                item["upper"],
                item["key"] if item["key"] is not None else 0,
                item["source_index"],
            )
        )
        for index, bucket in enumerate(self.buckets):
            bucket["index"] = index


def quantile_difference_bounds(
    summary,
    low_quantile=Fraction(1, 10),
    high_quantile=Fraction(9, 10),
    **kwargs
):
    """Compute exact attainable bounds for ``value(high) - value(low)``.

    Args:
        summary (dict): Bucket summary.  Required keys are ``buckets``,
            ``total_weight`` (or ``count``/``N``), ``sum``/``sum_units`` and
            ``min``/``max`` (or their ``*_units`` forms).  Optional keys are
            ``zero_count``, ``unit``/``quantum``, ``gamma``,
            ``relative_accuracy``, ``collapsed_low`` and
            ``collapsed_high``.

            Each bucket is a mapping with exact integer ``weight`` (or
            ``count``), exact ``lower``/``upper`` bounds (or their
            ``*_units`` forms), plus optional ``key`` and
            ``representative``.  A bucket with only ``key`` uses ``gamma`` to
            derive its interval.  All weights are integer multiplicities.
            For a collapsed bucket, pass the actual folded boundary as
            ``lower``/``upper`` (or ``old_lower``/``old_upper``).
        low_quantile, high_quantile: quantiles in ``[0, 1]``.

    Returns:
        QuantileDifferenceBounds: exact bounds and witnesses.

    Raises:
        ValueError: for malformed input or a structurally inconsistent
            summary.  Structurally valid summaries whose constraints cannot
            all hold return ``feasible=False`` with specific ``errors`` and
            ``conflicts``.

    Examples:
        >>> summary = {
        ...     "buckets": [
        ...         {"lower": 0, "upper": 1, "weight": 3, "key": 0},
        ...         {"lower": 10, "upper": 11, "weight": 3, "key": 1},
        ...     ],
        ...     "total_weight": 6,
        ...     "sum": 33,
        ...     "min": 0,
        ...     "max": 11,
        ... }
        >>> result = quantile_difference_bounds(summary, 0.1, 0.9)
        >>> result.feasible
        True
        >>> int(result.minimum), int(result.maximum)
        (10, 11)
    """
    if "low" in kwargs:
        low_quantile = kwargs.pop("low")
    if "high" in kwargs:
        high_quantile = kwargs.pop("high")
    if kwargs:
        raise TypeError(
            "unexpected keyword arguments: %s" % ", ".join(sorted(kwargs))
        )

    low = _as_fraction(low_quantile, "low_quantile")
    high = _as_fraction(high_quantile, "high_quantile")
    if not (0 <= low <= 1):
        raise ValueError(
            "low_quantile must be in [0, 1], got %r" % low_quantile
        )
    if not (0 <= high <= 1):
        raise ValueError(
            "high_quantile must be in [0, 1], got %r" % high_quantile
        )
    if low > high:
        raise ValueError(
            "low_quantile must not exceed high_quantile, got %r > %r"
            % (low_quantile, high_quantile)
        )
    if not isinstance(summary, dict):
        raise TypeError("summary must be a mapping")

    problem = _Problem(summary, low, high)

    result = QuantileDifferenceBounds(
        minimum=None,
        maximum=None,
        minimum_units=None,
        maximum_units=None,
        minimum_witness=None,
        maximum_witness=None,
        value_weights_minimum=None,
        value_weights_maximum=None,
        value_units_minimum=None,
        value_units_maximum=None,
        representative_difference=None,
        feasible=problem.feasible(),
        errors=list(problem.errors),
        conflicts=list(problem.conflicts),
        total_weight=problem.total_weight,
        zero_count=problem.zero_count,
        exact_sum=problem.exact_sum * problem.unit,
        exact_sum_units=problem.exact_sum,
        true_min=problem.true_min * problem.unit,
        true_max=problem.true_max * problem.unit,
        true_min_units=problem.true_min,
        true_max_units=problem.true_max,
        unit=problem.unit,
        collapsed_low=problem.collapsed_low,
        collapsed_high=problem.collapsed_high,
        collapsed_boundaries=problem.collapsed_boundaries,
        low=None,
        high=None,
        summary=summary,
    )

    if problem.total_weight == 0:
        if not problem.errors:
            problem.errors.append("summary is empty")
            result.errors = list(problem.errors)
            result.feasible = False
        result.low = QuantileBounds(low, problem.low_rank, None, None)
        result.high = QuantileBounds(high, problem.high_rank, None, None)
        return result

    if problem.feasible():
        problem.clamp()
    if not problem.feasible():
        result.errors = list(problem.errors)
        result.conflicts = list(problem.conflicts)
        result.feasible = False
        result.low = QuantileBounds(low, problem.low_rank, None, None)
        result.high = QuantileBounds(high, problem.high_rank, None, None)
        return result

    ranks = sorted(
        {
            0,
            problem.total_weight - 1,
            problem.low_rank,
            problem.high_rank,
        }
    )
    positions, _ = _rank_positions(problem.buckets, problem.total_weight, ranks)
    positions = _normalize_positions(positions, problem.total_weight)

    if problem.low_rank == problem.high_rank:
        solved = _solve_same_rank(
            problem.buckets,
            positions,
            problem.low_rank,
            problem.true_min,
            problem.true_max,
            problem.exact_sum,
        )
        if solved is None:
            problem._conflict(
                "rank_infeasible",
                "the rank %d observation cannot reproduce the bucket "
                "counts, exact sum %r and true bounds [%r, %r]"
                % (
                    problem.low_rank,
                    problem.exact_sum,
                    problem.true_min,
                    problem.true_max,
                ),
                ranks=[problem.low_rank],
                buckets=[
                    {
                        "key": bucket["key"],
                        "lower_units": bucket["lower"],
                        "upper_units": bucket["upper"],
                        "weight": bucket["weight"],
                    }
                    for bucket in problem.buckets
                    if bucket["weight"]
                ],
            )
            result.errors = list(problem.errors)
            result.conflicts = list(problem.conflicts)
            result.feasible = False
            result.low = QuantileBounds(low, problem.low_rank, None, None)
            result.high = QuantileBounds(high, problem.high_rank, None, None)
            return result

        value_min, value_max = solved
        minimum_point = (value_min, value_min)
        maximum_point = (value_min, value_min)
        low_range = (value_min, value_max)
        high_range = (value_min, value_max)
    else:
        solved = _solve_two_ranks(
            problem.buckets,
            positions,
            problem.total_weight,
            problem.low_rank,
            problem.high_rank,
            problem.true_min,
            problem.true_max,
            problem.exact_sum,
        )
        if solved is None:
            problem._conflict(
                "rank_infeasible",
                "the rank %d and rank %d observations cannot jointly "
                "reproduce the bucket counts, exact sum %r and true bounds "
                "[%r, %r]"
                % (
                    problem.low_rank,
                    problem.high_rank,
                    problem.exact_sum,
                    problem.true_min,
                    problem.true_max,
                ),
                ranks=[problem.low_rank, problem.high_rank],
                buckets=[
                    {
                        "key": bucket["key"],
                        "lower_units": bucket["lower"],
                        "upper_units": bucket["upper"],
                        "weight": bucket["weight"],
                    }
                    for bucket in problem.buckets
                    if bucket["weight"]
                ],
            )
            result.errors = list(problem.errors)
            result.conflicts = list(problem.conflicts)
            result.feasible = False
            result.low = QuantileBounds(low, problem.low_rank, None, None)
            result.high = QuantileBounds(high, problem.high_rank, None, None)
            return result

        minimum_point = solved["minimum"]
        maximum_point = solved["maximum"]
        low_range = (solved["low_min"], solved["low_max"])
        high_range = (solved["high_min"], solved["high_max"])

    low_bucket_index = positions[problem.low_rank][0]
    high_bucket_index = positions[problem.high_rank][0]
    low_representative = problem.buckets[low_bucket_index]["representative"]
    high_representative = problem.buckets[high_bucket_index][
        "representative"
    ]

    result.low = QuantileBounds(
        quantile=low,
        rank=problem.low_rank,
        minimum=low_range[0] * problem.unit,
        maximum=low_range[1] * problem.unit,
        representative=low_representative,
        bucket_index=low_bucket_index,
    )
    result.high = QuantileBounds(
        quantile=high,
        rank=problem.high_rank,
        minimum=high_range[0] * problem.unit,
        maximum=high_range[1] * problem.unit,
        representative=high_representative,
        bucket_index=high_bucket_index,
    )

    minimum_units = minimum_point[1] - minimum_point[0]
    maximum_units = maximum_point[1] - maximum_point[0]
    result.minimum_units = minimum_units
    result.maximum_units = maximum_units
    result.minimum = minimum_units * problem.unit
    result.maximum = maximum_units * problem.unit
    if (
        low_representative is not None
        and high_representative is not None
    ):
        result.representative_difference = (
            high_representative - low_representative
        )

    minimum_witness, minimum_values, minimum_units_values = _witness_for_point(
        problem.buckets,
        positions,
        problem.total_weight,
        problem.low_rank,
        problem.high_rank,
        minimum_point[0],
        minimum_point[1],
        problem.true_min,
        problem.true_max,
        problem.exact_sum,
        problem.unit,
    )
    maximum_witness, maximum_values, maximum_units_values = _witness_for_point(
        problem.buckets,
        positions,
        problem.total_weight,
        problem.low_rank,
        problem.high_rank,
        maximum_point[0],
        maximum_point[1],
        problem.true_min,
        problem.true_max,
        problem.exact_sum,
        problem.unit,
    )
    result.minimum_witness = minimum_witness
    result.maximum_witness = maximum_witness
    result.value_weights_minimum = minimum_values
    result.value_weights_maximum = maximum_values
    result.value_units_minimum = minimum_units_values
    result.value_units_maximum = maximum_units_values

    return result


def aggregate_witness(witness):
    """Re-aggregate a witness without expanding its integer weights.

    Args:
        witness: a witness list as stored on
            :class:`QuantileDifferenceBounds`.

    Returns:
        dict: ``value_weights`` (value -> integer weight), ``total_weight``,
        ``sum``, ``min``, ``max`` and ``buckets`` with each bucket's
        ``weight`` and ``sum``.  This is what a reader can use to check the
        supplied bucket counts, exact sum and true bounds.
    """
    value_weights = {}
    bucket_rows = []
    for item in witness:
        total = 0
        for value, count in item["values"].items():
            if not isinstance(count, six.integer_types) or count < 0:
                raise ValueError("witness counts must be non-negative integers")
            value_weights[value] = value_weights.get(value, 0) + count
            total += value * count
        bucket_rows.append(
            {
                "bucket_index": item["bucket_index"],
                "key": item["key"],
                "weight": item["weight"],
                "sum": total,
            }
        )
    return {
        "value_weights": value_weights,
        "total_weight": sum(value_weights.values()),
        "sum": sum(value * count for value, count in value_weights.items()),
        "min": min(value_weights) if value_weights else None,
        "max": max(value_weights) if value_weights else None,
        "buckets": bucket_rows,
    }


def verify_witness(summary, witness):
    """Check a witness against a summary, using only integer counts.

    The check confirms the total weight, exact sum, true extrema and every
    bucket's weight.  It never expands a weight and is independent of bucket
    ordering.
    """
    if witness is None:
        return False
    aggregate = aggregate_witness(witness)
    expected = {}
    for index, bucket in enumerate(summary.get("buckets", [])):
        weight = bucket.get("weight", bucket.get("count"))
        if weight is None:
            return False
        bucket_id = bucket.get("bucket_id", index)
        expected[bucket_id] = _coerce_int(weight, "bucket.weight")
    zero_count = _coerce_int(summary.get("zero_count", 0), "zero_count")
    if zero_count:
        expected["zero"] = zero_count

    observed = {}
    for item in witness:
        bucket_id = item.get("source_index")
        if bucket_id is None:
            bucket_id = item.get("bucket_index")
        if bucket_id == -1:
            bucket_id = "zero"
        if bucket_id in observed:
            return False
        observed[bucket_id] = item["weight"]
    if observed != expected:
        return False

    unit = _as_fraction(summary.get("unit", 1), "unit")

    def expected_value(unit_keys, physical_keys, label):
        for key in unit_keys:
            if key in summary:
                return _coerce_int(summary[key], key) * unit
        for key in physical_keys:
            if key in summary:
                return _as_fraction(summary[key], label) 
        return None

    expected_sum = expected_value(
        ("sum_units", "exact_sum_units", "signed_sum_units"),
        ("sum", "exact_sum", "signed_sum", "exact_signed_sum"),
        "sum",
    )
    expected_min = expected_value(
        ("min_units", "true_min_units", "lower_units"),
        ("min", "true_min", "true_lower", "lower", "minimum", "min_value"),
        "min",
    )
    expected_max = expected_value(
        ("max_units", "true_max_units", "upper_units"),
        ("max", "true_max", "true_upper", "upper", "maximum", "max_value"),
        "max",
    )
    total_weight = summary.get(
        "total_weight", summary.get("count", summary.get("N"))
    )
    return (
        aggregate["total_weight"] == total_weight
        and aggregate["sum"] == expected_sum
        and aggregate["min"] == expected_min
        and aggregate["max"] == expected_max
    )


def _store_buckets(store, sign, mapping, unit, true_min_units,
                   true_max_units):
    """Exact unit-coordinate buckets for one positive or negative store."""
    from .store import CollapsingHighestDenseStore
    from .store import CollapsingLowestDenseStore

    if not store.count:
        return []

    accuracy = getattr(mapping, "relative_accuracy", None)
    if accuracy is not None:
        alpha = Fraction(str(accuracy))
        gamma = (1 + alpha) / (1 - alpha)
    else:
        gamma = Fraction(str(mapping.gamma))
    offset = _coerce_int(getattr(mapping, "_offset", 0), "mapping.offset")
    collapsed = bool(getattr(store, "is_collapsed", False))
    low_collapsed = collapsed and isinstance(
        store, CollapsingLowestDenseStore
    )
    high_collapsed = collapsed and isinstance(
        store, CollapsingHighestDenseStore
    )

    buckets = []
    for key in range(store.min_key, store.max_key + 1):
        weight = store.bins[key - store.offset]
        if not weight:
            continue
        if not float(weight).is_integer():
            raise ValueError(
                "bucket weight %r at key %r is not an exact integer"
                % (weight, key)
            )
        weight = int(weight)

        effective_key = key - offset
        lower = _power(gamma, effective_key - 1) / unit
        upper = _power(gamma, effective_key) / unit
        if sign > 0:
            lower_units = _floor_fraction(lower) + 1
            upper_units = _floor_fraction(upper)
        else:
            negative_lower = -upper
            negative_upper = -lower
            lower_units = _ceil_fraction(negative_lower)
            upper_units = _ceil_fraction(negative_upper) - 1

        bucket = {
            "key": key if sign > 0 else -key,
            "lower_units": lower_units,
            "upper_units": upper_units,
            "weight": weight,
            "representative": mapping.value(key) * sign,
            "collapsed_low": low_collapsed and key == store.min_key,
            "collapsed_high": high_collapsed and key == store.max_key,
        }
        if low_collapsed and key == store.min_key:
            # Keys below min_key were folded here.  For a positive store
            # that range runs up from zero (exclusive); for a negative
            # store it runs up towards zero (exclusive).
            if sign > 0:
                bucket["lower_units"] = max(true_min_units, 1)
            else:
                bucket["upper_units"] = min(true_max_units, -1)
        if high_collapsed and key == store.max_key:
            if sign > 0:
                bucket["upper_units"] = true_max_units
            else:
                bucket["lower_units"] = true_min_units
        buckets.append(bucket)
    return buckets


def summarize_sketch(
    sketch,
    unit=1,
    total_weight=None,
    exact_sum=None,
    true_min=None,
    true_max=None,
    zero_count=None,
):
    """Export a live DDSketch as an exact-unit bucket summary.

    The original approximate ``get_quantile_value`` API is untouched; this is
    an additional export for the bounds routines.

    Args:
        sketch: a :class:`~ddsketch.ddsketch.BaseDDSketch` instance.
        unit: minimum quantization unit.  All observations must be exact
            multiples of this value.
        total_weight, exact_sum, true_min, true_max, zero_count: optional
            overrides for externally supplied exact summary facts.  When
            omitted, the sketch attributes are used; they must be exact
            integers on the unit lattice.

    Returns:
        dict: summary with both physical values and exact ``*_units``
        coordinates, accepted by :func:`quantile_difference_bounds`.
    """
    unit_fraction = _as_fraction(unit, "unit")
    if unit_fraction <= 0:
        raise ValueError("unit must be positive, got %r" % unit)

    if total_weight is None:
        total_weight = _coerce_int(sketch._count, "sketch.count")
    else:
        total_weight = _coerce_int(total_weight, "total_weight")

    if zero_count is None:
        zero_count = _coerce_int(sketch._zero_count, "sketch.zero_count")
    else:
        zero_count = _coerce_int(zero_count, "zero_count")

    if exact_sum is None:
        sum_fraction = _as_fraction(sketch._sum, "sketch.sum")
    else:
        sum_fraction = _as_fraction(exact_sum, "exact_sum")
    sum_units = _physical_to_units(sum_fraction, unit_fraction, "sum")

    if true_min is None:
        min_fraction = _as_fraction(sketch._min, "sketch.min")
    else:
        min_fraction = _as_fraction(true_min, "true_min")
    min_units = _physical_to_units(min_fraction, unit_fraction, "min")

    if true_max is None:
        max_fraction = _as_fraction(sketch._max, "sketch.max")
    else:
        max_fraction = _as_fraction(true_max, "true_max")
    max_units = _physical_to_units(max_fraction, unit_fraction, "max")

    from .store import CollapsingHighestDenseStore
    from .store import CollapsingLowestDenseStore

    buckets = []
    boundaries = []
    for store, sign, label in (
        (sketch._negative_store, -1, "negative"),
        (sketch._store, 1, "positive"),
    ):
        buckets.extend(
            _store_buckets(
                store,
                sign,
                sketch._mapping,
                unit_fraction,
                min_units,
                max_units,
            )
        )
        collapsed = bool(getattr(store, "is_collapsed", False))
        if collapsed:
            if isinstance(store, CollapsingLowestDenseStore):
                side = "low"
                boundary_key = store.min_key
            elif isinstance(store, CollapsingHighestDenseStore):
                side = "high"
                boundary_key = store.max_key
            else:
                side = "unknown"
                boundary_key = None
            boundaries.append(
                {
                    "store": label,
                    "side": side,
                    "boundary_key": boundary_key,
                    "window_min_key": store.min_key,
                    "window_max_key": store.max_key,
                    "bin_limit": getattr(store, "bin_limit", None),
                }
            )

    buckets.sort(
        key=lambda item: (
            item["lower_units"],
            item["upper_units"],
            item["key"] if item["key"] is not None else 0,
        )
    )
    for bucket in buckets:
        bucket["lower"] = bucket["lower_units"] * unit_fraction
        bucket["upper"] = bucket["upper_units"] * unit_fraction

    return {
        "unit": unit_fraction,
        "buckets": buckets,
        "total_weight": total_weight,
        "zero_count": zero_count,
        "sum_units": sum_units,
        "sum": sum_units * unit_fraction,
        "min_units": min_units,
        "min": min_units * unit_fraction,
        "max_units": max_units,
        "max": max_units * unit_fraction,
        "gamma": sketch._mapping.gamma,
        "collapsed_low": any(
            note["side"] == "low" for note in boundaries
        ),
        "collapsed_high": any(
            note["side"] == "high" for note in boundaries
        ),
        "collapsed_boundaries": boundaries,
    }


bucket_summary = summarize_sketch
describe_sketch = summarize_sketch
get_quantile_difference_bounds = quantile_difference_bounds


def _json_ready(value):
    if isinstance(value, Fraction):
        return value.numerator if value.denominator == 1 else float(value)
    if isinstance(value, dict):
        return {
            str(_json_ready(key)): _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def main(argv=None):
    """Command line entry point: read a JSON summary and print bounds."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Compute exact DDSketch quantile-difference bounds"
    )
    parser.add_argument(
        "summary", help="path to a JSON summary, or - for stdin"
    )
    parser.add_argument(
        "--low", default="0.1", help="low quantile (default 0.1)"
    )
    parser.add_argument(
        "--high", default="0.9", help="high quantile (default 0.9)"
    )
    args = parser.parse_args(argv)

    if args.summary == "-":
        summary = json.load(sys.stdin)
    else:
        with open(args.summary, "r") as handle:
            summary = json.load(handle)

    result = quantile_difference_bounds(summary, args.low, args.high)
    json.dump(
        _json_ready(result.to_dict()),
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if result.feasible else 1


if __name__ == "__main__":
    raise SystemExit(main())
