# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

"""
Sliding-window merge of archived exponential histogram data points.

This module supports a dashboard that displays a sliding window of
consecutive whole minutes.  Every minute, each collector leaves behind an
:class:`~opentelemetry.sdk.metrics._internal.point.ExponentialHistogramDataPoint`
(its scale, the offset and counts of its positive and negative buckets, its
zero count and, when recorded, its min and max); the raw measurements are no
longer available.

For a window of minutes and a given ``max_size``/``max_scale`` pair, the
merge recomputes what
:class:`~opentelemetry.sdk.metrics._internal.aggregation._ExponentialBucketHistogramAggregation`
would have reported if every raw measurement in the window had been
replayed through it, using the mapping implementations of this version of
the SDK
(:class:`~opentelemetry.sdk.metrics._internal.exponential_histogram.mapping.exponent_mapping.ExponentMapping`
and
:class:`~opentelemetry.sdk.metrics._internal.exponential_histogram.mapping.logarithm_mapping.LogarithmMapping`).

The scale the SDK ends up with after a replay depends only on the smallest
and largest bucket indices seen for each sign: rescaling is triggered when
the index span reaches ``max_size`` and each downscale shifts indices
arithmetically, so the cumulative effect equals computing the required
scale change from the per-sign extreme indices at ``max_scale`` directly.
Those extremes are not always pinned down by the archived summaries:

* A minute archived with a scale lower than the scale the window would use
  cannot be refined: values in one of its archived buckets may fall into
  different finer buckets on the dashboard.
* The extreme indices themselves are only known up to the resolution of
  the archived scale, unless the recorded min/max values pin them.

When the window result is uniquely determined, :class:`DeterminateWindow`
carries exactly the scale, positive/negative buckets and zero count a full
replay would produce.  Otherwise :class:`AmbiguousWindow` carries the
counts that can still be confirmed plus two example measurement sets that
are both consistent with the archived summaries but that the current
dashboard configuration would separate into different results.

Whole-minute corrections are accepted at any time.  Every emitted window
report is kept immutable, so a report that was sent out before a
correction (because the window has not slid to the corrected minute yet,
or the correction only changes another window) can still be looked up with
the data it was originally computed from.
"""

from dataclasses import dataclass
from threading import Lock

from opentelemetry.sdk.metrics._internal.exponential_histogram.mapping.errors import (
    MappingOverflowError,
    MappingUnderflowError,
)
from opentelemetry.sdk.metrics._internal.exponential_histogram.mapping.exponent_mapping import (
    ExponentMapping,
)
from opentelemetry.sdk.metrics._internal.exponential_histogram.mapping.logarithm_mapping import (
    LogarithmMapping,
)
from opentelemetry.sdk.metrics._internal.point import (
    ExponentialHistogramDataPoint,
)

# The same limits as
# opentelemetry.sdk.metrics._internal.aggregation._ExponentialBucketHistogramAggregation.
_MIN_MAX_SIZE = 2
_MAX_MAX_SIZE = 16384

_POSITIVE = 1
_NEGATIVE = -1


def _new_mapping(scale: int):
    # Same rule as
    # _ExponentialBucketHistogramAggregation._new_mapping.
    if scale <= 0:
        return ExponentMapping(scale)
    return LogarithmMapping(scale)


def _change_needed(low: int, high: int, max_size: int) -> int:
    # Same computation as
    # _ExponentialBucketHistogramAggregation._get_scale_change: the number
    # of right shifts after which the index span fits in max_size.
    change = 0

    while high - low >= max_size:
        high >>= 1
        low >>= 1
        change += 1

    return change


@dataclass(frozen=True)
class MinutePoint:
    """
    The archived summary of one whole minute of measurements.

    ``positive_offset``/``positive_counts`` and
    ``negative_offset``/``negative_counts`` are the bucket offsets and
    counts as exported by the SDK for that minute, ``min`` and ``max`` are
    the recorded extreme values or None when the minute was collected with
    ``record_min_max=False``.
    """

    scale: int
    positive_offset: int
    positive_counts: tuple[int, ...]
    negative_offset: int
    negative_counts: tuple[int, ...]
    zero_count: int
    min: float | None = None
    max: float | None = None

    @classmethod
    def from_data_point(cls, point: ExponentialHistogramDataPoint) -> "MinutePoint":
        scale = point.scale
        if scale is None:
            # The SDK reports None for the scale of a point that has seen
            # no measurements at all; the scale is meaningless then.
            if point.count != 0:
                raise ValueError("Data point has no scale but its count is not zero")
            scale = 0

        return cls(
            scale=scale,
            positive_offset=point.positive.offset,
            positive_counts=tuple(point.positive.bucket_counts),
            negative_offset=point.negative.offset,
            negative_counts=tuple(point.negative.bucket_counts),
            zero_count=point.zero_count,
            min=point.min,
            max=point.max,
        )

    @property
    def count(self) -> int:
        return self.zero_count + sum(self.positive_counts) + sum(self.negative_counts)


@dataclass(frozen=True)
class WindowBuckets:
    """
    Buckets of one sign of a window result: ``counts[i]`` is the count of
    the bucket with index ``offset + i``.
    """

    offset: int
    counts: tuple[int, ...]


@dataclass(frozen=True)
class DeterminateWindow:
    """
    A window result identical to replaying every raw measurement of the
    window through the SDK aggregation with the dashboard's ``max_size``
    and ``max_scale``.
    """

    scale: int
    zero_count: int
    count: int
    positive: WindowBuckets
    negative: WindowBuckets


@dataclass(frozen=True)
class UnresolvedSegment:
    """
    One minute whose archived buckets cannot be refined to the resolution
    the window would use, kept at the minute's own archived scale.
    """

    minute: int
    scale: int
    positive: WindowBuckets
    negative: WindowBuckets


@dataclass(frozen=True)
class MinuteExample:
    """
    Example raw measurements for one minute, consistent with that minute's
    archived summary.
    """

    minute: int
    values: tuple[float, ...]


@dataclass(frozen=True)
class AmbiguousWindow:
    """
    A window result that the archived summaries do not determine uniquely.

    ``zero_count``, ``count``, ``positive_count`` and ``negative_count``
    are still exact.  ``scale`` is the final scale when it is known
    uniquely, otherwise ``scale_range`` bounds the scales that are
    consistent with the summaries.  ``definite_positive`` and
    ``definite_negative`` hold the bucket counts that are confirmed at the
    known scale (None when the scale itself is ambiguous).
    ``unresolved`` lists the minutes whose coarser archived buckets cause
    the ambiguity.  ``example_a`` and ``example_b`` are two measurement
    sets that both reproduce every archived summary of the window but that
    the current dashboard configuration separates into different results.
    """

    zero_count: int
    count: int
    positive_count: int
    negative_count: int
    scale: int | None
    scale_range: tuple[int, int] | None
    definite_positive: WindowBuckets | None
    definite_negative: WindowBuckets | None
    unresolved: tuple[UnresolvedSegment, ...]
    example_a: tuple[MinuteExample, ...]
    example_b: tuple[MinuteExample, ...]


@dataclass(frozen=True)
class WindowReport:
    """
    An emitted window result.  Reports are immutable: a report that was
    sent out before a correction can always be looked up with the data it
    was originally computed from.
    """

    report_id: int
    version: int
    start_minute: int
    length: int
    result: DeterminateWindow | AmbiguousWindow | None


@dataclass(frozen=True)
class _Segment:
    # The buckets of one sign of one minute.
    minute: int
    scale: int
    start: int
    end: int
    counts: tuple[int, ...]


def _validate_minute_point(point: MinutePoint) -> None:
    for counts in (point.positive_counts, point.negative_counts):
        if any(count < 0 for count in counts):
            raise ValueError("Bucket counts must not be negative")

    if point.zero_count < 0:
        raise ValueError("zero_count must not be negative")

    if point.min is not None and point.max is not None and point.min > point.max:
        raise ValueError("min is greater than max")


def _segments(
    minutes: list[tuple[int, MinutePoint]],
) -> dict[int, list[_Segment]]:
    segments = {_POSITIVE: [], _NEGATIVE: []}

    for minute, point in minutes:
        for sign, offset, counts in (
            (_POSITIVE, point.positive_offset, point.positive_counts),
            (_NEGATIVE, point.negative_offset, point.negative_counts),
        ):
            # The SDK exports the whole circular backing array of its
            # buckets, so the archived counts may carry trailing zeros up
            # to the capacity.  The logical buckets end at the last
            # non-zero count.
            end = len(counts)

            while end > 0 and not counts[end - 1]:
                end -= 1

            if end:
                segments[sign].append(
                    _Segment(
                        minute=minute,
                        scale=point.scale,
                        start=offset,
                        end=offset + end - 1,
                        counts=tuple(counts[:end]),
                    )
                )

    return segments


def _merge_at_scale(segments: list[_Segment], scale: int) -> WindowBuckets:
    # Every segment must have a scale greater than or equal to scale, so
    # that shifting its indices right is exact.
    merged: dict[int, int] = {}

    for segment in segments:
        shift = segment.scale - scale

        if shift < 0:
            raise ValueError("Cannot merge a segment into a scale finer than its own")

        for position, count in enumerate(segment.counts):
            if count:
                index = (segment.start + position) >> shift
                merged[index] = merged.get(index, 0) + count

    if not merged:
        # This mirrors what the SDK exports for a sign that has seen no
        # values.
        return WindowBuckets(offset=0, counts=(0,))

    start = min(merged)
    end = max(merged)

    return WindowBuckets(
        offset=start,
        counts=tuple(merged.get(index, 0) for index in range(start, end + 1)),
    )


def _sign_bounds(
    segments: list[_Segment],
    points_by_minute: dict[int, MinutePoint],
    sign: int,
    max_scale: int,
) -> tuple[int, int, int, int]:
    # Bounds on the smallest and largest bucket index the sign's raw
    # measurements had at max_scale, as
    # (lowest possible low, highest possible low, lowest possible high,
    # highest possible high).
    mapping_max = _new_mapping(max_scale)
    low_min = None
    low_max = None
    high_min = None
    high_max = None

    for segment in segments:
        shift = max_scale - segment.scale

        if shift >= 0:
            segment_low_min = segment.start << shift
            segment_low_max = ((segment.start + 1) << shift) - 1
            segment_high_min = segment.end << shift
            segment_high_max = ((segment.end + 1) << shift) - 1
        else:
            # The minute was archived at a finer scale than max_scale, so
            # its indices at max_scale are known exactly.
            segment_low_min = segment_low_max = segment.start >> -shift
            segment_high_min = segment_high_max = segment.end >> -shift

        # The minute's own recorded extremes pin the indices of its
        # extreme buckets exactly.
        point = points_by_minute[segment.minute]

        if sign == _POSITIVE:
            high_extreme = point.max if point.max is not None and point.max > 0 else None
            low_extreme = point.min if point.min is not None and point.min > 0 else None
        else:
            high_extreme = -point.min if point.min is not None and point.min < 0 else None
            low_extreme = -point.max if point.max is not None and point.max < 0 else None

        if high_extreme is not None:
            pinned = mapping_max.map_to_index(high_extreme)

            if not segment_high_min <= pinned <= segment_high_max:
                raise ValueError(
                    "Inconsistent summary: the recorded extreme of minute "
                    f"{segment.minute} does not fall into its archived "
                    "buckets"
                )

            segment_high_min = segment_high_max = pinned

        if low_extreme is not None:
            pinned = mapping_max.map_to_index(low_extreme)

            if not segment_low_min <= pinned <= segment_low_max:
                raise ValueError(
                    "Inconsistent summary: the recorded extreme of minute "
                    f"{segment.minute} does not fall into its archived "
                    "buckets"
                )

            segment_low_min = segment_low_max = pinned

        if low_min is None:
            low_min, low_max = segment_low_min, segment_low_max
            high_min, high_max = segment_high_min, segment_high_max
        else:
            low_min = min(low_min, segment_low_min)
            low_max = min(low_max, segment_low_max)
            high_min = max(high_min, segment_high_min)
            high_max = max(high_max, segment_high_max)

    return low_min, low_max, high_min, high_max


def _replay_result(
    values, max_size: int, max_scale: int
) -> tuple[int, tuple[int, tuple[int, ...]], tuple[int, tuple[int, ...]], int]:
    """
    Replays raw measurements the same way
    _ExponentialBucketHistogramAggregation does and returns
    (scale, (positive offset, positive counts), (negative offset, negative
    counts), zero_count).  Used to verify that the two example measurement
    sets of an ambiguous window really produce different results.
    """
    mapping = _new_mapping(max_scale)
    counts = {_POSITIVE: {}, _NEGATIVE: {}}
    starts = {_POSITIVE: 0, _NEGATIVE: 0}
    ends = {_POSITIVE: 0, _NEGATIVE: 0}
    zero_count = 0
    total = 0
    scale = None

    for value in values:
        total += 1

        if value == 0:
            zero_count += 1

            if total == zero_count:
                scale = 0

            continue

        sign = _POSITIVE if value > 0 else _NEGATIVE
        magnitude = abs(value)
        index = mapping.map_to_index(magnitude)

        if not counts[sign]:
            starts[sign] = index
            ends[sign] = index
        else:
            low = high = None

            if index < starts[sign] and ends[sign] - index >= max_size:
                low, high = index, ends[sign]
            elif index > ends[sign] and index - starts[sign] >= max_size:
                low, high = starts[sign], index

            if low is not None:
                change = _change_needed(low, high, max_size)

                for other_sign in (_POSITIVE, _NEGATIVE):
                    if counts[other_sign]:
                        shifted: dict[int, int] = {}

                        for bucket_index, bucket_count in counts[other_sign].items():
                            shifted_index = bucket_index >> change
                            shifted[shifted_index] = shifted.get(shifted_index, 0) + bucket_count

                        counts[other_sign] = shifted
                        starts[other_sign] >>= change
                        ends[other_sign] >>= change

                mapping = _new_mapping(mapping.scale - change)
                index = mapping.map_to_index(magnitude)

        buckets = counts[sign]
        buckets[index] = buckets.get(index, 0) + 1
        starts[sign] = min(starts[sign], index)
        ends[sign] = max(ends[sign], index)
        scale = mapping.scale

    def result_for(sign):
        if not counts[sign]:
            return 0, (0,)

        return starts[sign], tuple(counts[sign].get(index, 0) for index in range(starts[sign], ends[sign] + 1))

    return scale, result_for(_POSITIVE), result_for(_NEGATIVE), zero_count


# The bucket index of the value 1.0 at any scale; used as the center the
# example measurement sets are packed towards or away from.
_CENTER_INDEX = -1


def _example_values(
    minute: int,
    point: MinutePoint,
    max_scale: int,
    pack: str,
    overrides: dict | None = None,
) -> MinuteExample:
    """
    Builds example raw measurements for one minute.  The values of an
    archived bucket are placed on one fine index of the bucket's fine
    index range at ``max_scale``: the one closest to the index of 1.0
    (``pack="compact"``), the one farthest from it (``pack="spread"``), or
    an explicit end of the range (``pack="low"``/``"high"`` through
    ``overrides``).  A recorded min or max is reproduced exactly.
    """
    mapping_max = _new_mapping(max_scale)
    mapping_minute = _new_mapping(point.scale)
    shift = max_scale - point.scale
    values = []

    for sign, offset, counts, low_extreme, high_extreme in (
        (
            _POSITIVE,
            point.positive_offset,
            point.positive_counts,
            # The smallest and largest positive values, when recorded.
            point.min if point.min is not None and point.min > 0 else None,
            point.max if point.max is not None and point.max > 0 else None,
        ),
        (
            _NEGATIVE,
            point.negative_offset,
            point.negative_counts,
            # The smallest and largest negative magnitudes, when recorded.
            -point.max if point.max is not None and point.max < 0 else None,
            -point.min if point.min is not None and point.min < 0 else None,
        ),
    ):
        low_pinned = mapping_minute.map_to_index(low_extreme) if low_extreme is not None else None
        high_pinned = mapping_minute.map_to_index(high_extreme) if high_extreme is not None else None

        for pinned in (low_pinned, high_pinned):
            if pinned is None:
                continue

            if not 0 <= pinned - offset < len(counts):
                raise ValueError(
                    "Inconsistent summary: the recorded extreme of minute "
                    f"{minute} does not fall into its archived buckets"
                )

            if not counts[pinned - offset]:
                raise ValueError(
                    f"Inconsistent summary: the recorded extreme of minute {minute} falls into an empty archived bucket"
                )

        for position, count in enumerate(counts):
            if not count:
                continue

            index = offset + position

            bucket_pack = pack

            if overrides is not None and (sign, index) in overrides:
                bucket_pack = overrides[(sign, index)]

            if shift >= 0:
                low_fine = index << shift
                high_fine = ((index + 1) << shift) - 1
            else:
                low_fine = high_fine = index >> -shift

            if bucket_pack == "compact":
                fine = min(max(_CENTER_INDEX, low_fine), high_fine)
            elif bucket_pack == "spread":
                if abs(low_fine - _CENTER_INDEX) >= abs(high_fine - _CENTER_INDEX):
                    fine = low_fine
                else:
                    fine = high_fine
            elif bucket_pack == "low":
                fine = low_fine
            else:
                fine = high_fine

            if shift >= 0:
                # The midpoint of the fine bucket is strictly inside it,
                # so it maps to the fine index under both boundary
                # conventions (the exponent mapping's buckets contain
                # their upper boundary, the logarithm mapping's buckets
                # their lower one) and stays inside the archived bucket.
                representative = (mapping_max.get_lower_boundary(fine) + mapping_max.get_lower_boundary(fine + 1)) / 2
            else:
                # The minute was archived at a finer scale than
                # max_scale: the value must map to the archived bucket
                # exactly, so it is taken from inside the archived
                # bucket itself.
                representative = (
                    mapping_minute.get_lower_boundary(index) + mapping_minute.get_lower_boundary(index + 1)
                ) / 2

            # A bucket that contains a recorded extreme cannot be packed
            # past that extreme.
            if low_pinned == index and representative < low_extreme:
                representative = low_extreme

            if high_pinned == index and representative > high_extreme:
                representative = high_extreme

            pinned_values = []

            if low_pinned == index:
                pinned_values.append(low_extreme)

            if high_pinned == index and high_extreme != low_extreme:
                pinned_values.append(high_extreme)

            if len(pinned_values) > count:
                raise ValueError(
                    "Inconsistent summary: the recorded extremes of minute "
                    f"{minute} do not fit its archived bucket counts"
                )

            bucket_values = pinned_values + [representative] * (count - len(pinned_values))

            values.extend(sign * value for value in bucket_values)

    values.extend([0.0] * point.zero_count)

    return MinuteExample(minute=minute, values=tuple(values))


def _build_examples(
    minutes: list[tuple[int, MinutePoint]],
    max_size: int,
    max_scale: int,
) -> tuple[tuple[MinuteExample, ...], tuple[MinuteExample, ...]]:
    """
    Builds two example measurement sets that are both consistent with the
    archived summaries but produce different results when replayed with
    the dashboard's ``max_size`` and ``max_scale``.
    """
    non_empty = [(minute, point) for minute, point in minutes if point.count > 0]

    def replay(examples):
        values = []

        for example in examples:
            values.extend(example.values)

        return _replay_result(values, max_size, max_scale)

    example_compact = tuple(_example_values(minute, point, max_scale, "compact") for minute, point in non_empty)
    example_spread = tuple(_example_values(minute, point, max_scale, "spread") for minute, point in non_empty)

    result_compact = replay(example_compact)

    if replay(example_spread) != result_compact:
        return example_compact, example_spread

    # Packing every bucket towards or away from the center gave the same
    # result; move single buckets of coarser minutes to one end of their
    # fine index range until the replay differs.
    for minute, point in non_empty:
        if max_scale - point.scale <= 0:
            continue

        for sign, offset, counts in (
            (_POSITIVE, point.positive_offset, point.positive_counts),
            (_NEGATIVE, point.negative_offset, point.negative_counts),
        ):
            for position, count in enumerate(counts):
                if not count:
                    continue

                for end in ("low", "high"):
                    candidate = tuple(
                        _example_values(
                            example_minute,
                            example_point,
                            max_scale,
                            "compact",
                            overrides=(
                                {(sign, offset + position): end}
                                if (example_minute, example_point) == (minute, point)
                                else None
                            ),
                        )
                        for example_minute, example_point in non_empty
                    )

                    if replay(candidate) != result_compact:
                        return example_compact, candidate

    # The summaries pin every bucket of the window; this is not expected
    # to happen because the merge only asks for examples when the result
    # is not uniquely determined.
    return example_compact, example_spread


def _get_boundary(scale: int, index: int) -> float | None:
    try:
        return _new_mapping(scale).get_lower_boundary(index)
    except (MappingUnderflowError, MappingOverflowError):
        return None


def _high_is_pinned(
    minutes: list[tuple[int, MinutePoint]],
    segment_lookup: dict[tuple[int, int], _Segment],
    sign: int,
    extreme: float,
    attribute: str,
) -> bool:
    # The sign's largest index is exactly the index of extreme if every
    # minute that did not record the corresponding extreme value provably
    # stays below it.
    for minute, point in minutes:
        if point.count == 0 or getattr(point, attribute) is not None:
            continue

        segment = segment_lookup.get((minute, sign))

        if segment is None:
            continue

        boundary = _get_boundary(segment.scale, segment.end + 1)

        if boundary is None or boundary > extreme:
            return False

    return True


def _low_is_pinned(
    minutes: list[tuple[int, MinutePoint]],
    segment_lookup: dict[tuple[int, int], _Segment],
    sign: int,
    extreme: float,
    attribute: str,
) -> bool:
    # The sign's smallest index is exactly the index of extreme if every
    # minute that did not record the corresponding extreme value provably
    # stays above it.
    for minute, point in minutes:
        if point.count == 0 or getattr(point, attribute) is not None:
            continue

        if point.zero_count:
            return False

        if (minute, -sign) in segment_lookup:
            return False

        segment = segment_lookup.get((minute, sign))

        if segment is None:
            continue

        boundary = _get_boundary(segment.scale, segment.start)

        if boundary is None or boundary < extreme:
            return False

    return True


def _merge_window(
    minutes: list[tuple[int, MinutePoint]],
    max_size: int,
    max_scale: int,
) -> DeterminateWindow | AmbiguousWindow | None:
    total_zero_count = sum(point.zero_count for _, point in minutes)
    total_count = sum(point.count for _, point in minutes)
    positive_count = sum(sum(point.positive_counts) for _, point in minutes)
    negative_count = sum(sum(point.negative_counts) for _, point in minutes)

    if total_count == 0:
        # The SDK exports no point at all when there were no measurements.
        return None

    segments = _segments(minutes)

    if not segments[_POSITIVE] and not segments[_NEGATIVE]:
        # All measurements of the window are zero; the SDK reports scale 0
        # and empty buckets then.
        empty = WindowBuckets(offset=0, counts=(0,))

        return DeterminateWindow(
            scale=0,
            zero_count=total_zero_count,
            count=total_count,
            positive=empty,
            negative=empty,
        )

    # Recorded min/max values pin the window extremes exactly.  A minute
    # that did not record its min or max does not prevent pinning if its
    # archived buckets provably stay on one side of the extreme.
    recorded_points = [point for _, point in minutes if point.count > 0]
    recorded_maxes = [point.max for point in recorded_points if point.max is not None]
    recorded_mins = [point.min for point in recorded_points if point.min is not None]
    window_max = max(recorded_maxes) if recorded_maxes else None
    window_min = min(recorded_mins) if recorded_mins else None
    all_max_recorded = len(recorded_maxes) == len(recorded_points)
    all_min_recorded = len(recorded_mins) == len(recorded_points)

    points_by_minute = dict(minutes)
    segment_lookup = {
        (segment.minute, sign): segment for sign, sign_segments in segments.items() for segment in sign_segments
    }

    mapping_max = _new_mapping(max_scale)
    change_min = 0
    change_max = 0

    for sign in (_POSITIVE, _NEGATIVE):
        sign_segments = segments[sign]

        if not sign_segments:
            continue

        if sign == _POSITIVE:
            if all_max_recorded and window_max is not None and window_max <= 0:
                raise ValueError("Inconsistent summaries: positive buckets but max <= 0")

            high_extreme = window_max if window_max is not None and window_max > 0 else None
            low_extreme = window_min if window_min is not None and window_min > 0 else None
            high_attribute = "max"
            low_attribute = "min"
        else:
            if all_min_recorded and window_min is not None and window_min >= 0:
                raise ValueError("Inconsistent summaries: negative buckets but min >= 0")

            high_extreme = -window_min if window_min is not None and window_min < 0 else None
            low_extreme = -window_max if window_max is not None and window_max < 0 else None
            high_attribute = "min"
            low_attribute = "max"

        low_min, low_max, high_min, high_max = _sign_bounds(sign_segments, points_by_minute, sign, max_scale)

        if high_extreme is not None and _high_is_pinned(minutes, segment_lookup, sign, high_extreme, high_attribute):
            pinned = mapping_max.map_to_index(high_extreme)

            if not high_min <= pinned <= high_max:
                raise ValueError("Inconsistent summaries: the recorded extreme does not fall into the archived buckets")

            high_min = high_max = pinned

        if low_extreme is not None and _low_is_pinned(minutes, segment_lookup, sign, low_extreme, low_attribute):
            pinned = mapping_max.map_to_index(low_extreme)

            if not low_min <= pinned <= low_max:
                raise ValueError("Inconsistent summaries: the recorded extreme does not fall into the archived buckets")

            low_min = low_max = pinned

        # The smallest scale change any consistent measurement set could
        # need (most favorable extremes) and the largest one (least
        # favorable extremes).
        change_min = max(change_min, _change_needed(low_max, high_min, max_size))
        change_max = max(change_max, _change_needed(low_min, high_max, max_size))

    if change_min != change_max:
        example_a, example_b = _build_examples(minutes, max_size, max_scale)

        return AmbiguousWindow(
            zero_count=total_zero_count,
            count=total_count,
            positive_count=positive_count,
            negative_count=negative_count,
            scale=None,
            scale_range=(max_scale - change_max, max_scale - change_min),
            definite_positive=None,
            definite_negative=None,
            unresolved=_unresolved_segments(minutes, segments, max_scale),
            example_a=example_a,
            example_b=example_b,
        )

    scale = max_scale - change_min

    if all(segment.scale >= scale for sign_segments in segments.values() for segment in sign_segments):
        return DeterminateWindow(
            scale=scale,
            zero_count=total_zero_count,
            count=total_count,
            positive=_merge_at_scale(segments[_POSITIVE], scale),
            negative=_merge_at_scale(segments[_NEGATIVE], scale),
        )

    # The scale is known but at least one minute was archived at a coarser
    # scale, so the distribution of its counts inside the window's finer
    # buckets is not determined.
    example_a, example_b = _build_examples(minutes, max_size, max_scale)

    return AmbiguousWindow(
        zero_count=total_zero_count,
        count=total_count,
        positive_count=positive_count,
        negative_count=negative_count,
        scale=scale,
        scale_range=None,
        definite_positive=_merge_at_scale(
            [segment for segment in segments[_POSITIVE] if segment.scale >= scale],
            scale,
        ),
        definite_negative=_merge_at_scale(
            [segment for segment in segments[_NEGATIVE] if segment.scale >= scale],
            scale,
        ),
        unresolved=_unresolved_segments(minutes, segments, scale),
        example_a=example_a,
        example_b=example_b,
    )


def _unresolved_segments(
    minutes: list[tuple[int, MinutePoint]],
    segments: dict[int, list[_Segment]],
    scale: int,
) -> tuple[UnresolvedSegment, ...]:
    by_minute: dict[int, dict[int, _Segment]] = {}

    for sign, sign_segments in segments.items():
        for segment in sign_segments:
            if segment.scale < scale:
                by_minute.setdefault(segment.minute, {})[sign] = segment

    empty = WindowBuckets(offset=0, counts=(0,))
    unresolved = []

    for minute, point in minutes:
        if minute not in by_minute:
            continue

        minute_segments = by_minute[minute]
        unresolved.append(
            UnresolvedSegment(
                minute=minute,
                scale=point.scale,
                positive=(
                    WindowBuckets(
                        offset=minute_segments[_POSITIVE].start,
                        counts=minute_segments[_POSITIVE].counts,
                    )
                    if _POSITIVE in minute_segments
                    else empty
                ),
                negative=(
                    WindowBuckets(
                        offset=minute_segments[_NEGATIVE].start,
                        counts=minute_segments[_NEGATIVE].counts,
                    )
                    if _NEGATIVE in minute_segments
                    else empty
                ),
            )
        )

    return tuple(unresolved)


class ExponentialHistogramWindowArchive:
    """
    Keeps one archived exponential histogram summary per whole minute and
    merges sliding windows of consecutive minutes as if every raw
    measurement in the window had been replayed through the SDK
    aggregation with this archive's ``max_size`` and ``max_scale``.

    Whole-minute corrections are accepted through :meth:`correct`.  Every
    emitted :class:`WindowReport` is kept and can be looked up through
    :meth:`get_report` with the data it was originally computed from, even
    after later corrections.
    """

    def __init__(self, max_size: int = 160, max_scale: int = 20):
        if max_size < _MIN_MAX_SIZE:
            raise ValueError(f"Buckets max size {max_size} is smaller than minimum max size {_MIN_MAX_SIZE}")

        if max_size > _MAX_MAX_SIZE:
            raise ValueError(f"Buckets max size {max_size} is larger than maximum max size {_MAX_MAX_SIZE}")

        self._max_size = max_size
        self._max_scale = max_scale
        self._minutes: dict[int, MinutePoint] = {}
        # minute -> list of (version, point), oldest first
        self._history: dict[int, list[tuple[int, MinutePoint]]] = {}
        self._version = 0
        self._reports: dict[int, WindowReport] = {}
        self._next_report_id = 0
        self._lock = Lock()

    @property
    def version(self) -> int:
        return self._version

    def ingest(self, minute: int, point: MinutePoint) -> int:
        """
        Archives the summary of a minute that has no summary yet and
        returns the new archive version.
        """
        with self._lock:
            if minute in self._minutes:
                raise ValueError(f"Minute {minute} already has a summary, use correct() to replace it")

            return self._put(minute, point)

    def correct(self, minute: int, point: MinutePoint) -> int:
        """
        Replaces the whole summary of a minute and returns the new archive
        version.  Reports that were emitted before the correction are not
        affected and remain available through :meth:`get_report`.
        """
        with self._lock:
            return self._put(minute, point)

    def _put(self, minute: int, point: MinutePoint) -> int:
        _validate_minute_point(point)
        self._version += 1
        self._minutes[minute] = point
        self._history.setdefault(minute, []).append((self._version, point))
        return self._version

    def window(self, start_minute: int, length: int) -> WindowReport:
        """
        Merges the window of ``length`` consecutive whole minutes starting
        at ``start_minute`` and stores the emitted report.
        """
        if length <= 0:
            raise ValueError("Window length must be positive")

        with self._lock:
            minutes = [
                (minute, self._minutes[minute])
                for minute in range(start_minute, start_minute + length)
                if minute in self._minutes
            ]
            result = _merge_window(minutes, self._max_size, self._max_scale)
            report = WindowReport(
                report_id=self._next_report_id,
                version=self._version,
                start_minute=start_minute,
                length=length,
                result=result,
            )
            self._reports[report.report_id] = report
            self._next_report_id += 1

            return report

    def get_report(self, report_id: int) -> WindowReport:
        """
        Returns a previously emitted report, unchanged, with the data it
        was originally computed from.
        """
        with self._lock:
            return self._reports[report_id]

    def window_as_of(self, start_minute: int, length: int, version: int) -> DeterminateWindow | AmbiguousWindow | None:
        """
        Recomputes a window with the summaries that were current at the
        given archive version, without emitting a report.
        """
        if length <= 0:
            raise ValueError("Window length must be positive")

        with self._lock:
            if not 0 <= version <= self._version:
                raise ValueError(f"Unknown version {version}")

            minutes = []

            for minute in range(start_minute, start_minute + length):
                point = None

                for record_version, record in self._history.get(minute, []):
                    if record_version <= version:
                        point = record
                    else:
                        break

                if point is not None:
                    minutes.append((minute, point))

            return _merge_window(minutes, self._max_size, self._max_scale)
