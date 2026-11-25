# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Exact geometry primitives for the cut usage engine.

The engine never lays out a complete stroke list for the totals. It describes the
visible strokes along a support line as periodic interval sets and measures them
with exact rational arithmetic. Measuring needs at most the first and the last
repeat of a period, therefore a wide window with millions of strokes is cheap.

Different periods on the same support line are measured by inclusion and
exclusion. An intersection iterates the dashes of the sparsest set inside the
requested interval only. The common repeat of the periods is never built and
never tiled.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import Iterable, Iterator, Sequence

from ezdxf.math import Vec2

__all__ = [
    "to_fraction",
    "CumulativePolyline",
    "SupportLine",
    "support_line",
    "PeriodicSet",
    "RangedSet",
    "normalize_intervals",
    "periodic_measure",
    "union_ranged_measure",
]

MAX_DENOMINATOR = 1_000_000_000

# One common repeat of several line patterns is built once and then tiled
# analytically. This limit protects against pathological pattern combinations
# which would need an unreasonable number of events inside a single repeat.
MAX_COMMON_EVENTS = 4_000_000


class CutUsageError(Exception):
    """Raised when a cut usage request cannot be answered exactly."""


def to_fraction(value: float) -> Fraction:
    """Converts a float to an exact rational number.

    Decimal-looking floats such as ``0.25`` or ``1.5`` stay exact. A float with a
    long binary tail is reduced to the closest rational with a bounded
    denominator.
    """
    value = float(value)
    if value == 0.0:
        return Fraction(0)
    return Fraction(value).limit_denominator(MAX_DENOMINATOR)


@dataclass
class CumulativePolyline:
    """A 2D polyline with exact cumulative distances at every vertex.

    The distance axis starts at zero at the first vertex and uses drawing units.
    The pattern phase of a source path uses this axis. A clipping window only
    selects ranges of the axis and never changes the phase origin.

    Attributes:
        points: the displayed points in the target coordinate system; a paperspace
            viewport stores paperspace points here
        distances: exact cumulative distances in drawing units
        measure_points: the same vertices in drawing units; the ratio to `points`
            is constant for a viewport transform
    """

    points: list[Vec2]
    distances: list[Fraction]
    measure_points: list[Vec2] | None = None

    def __post_init__(self) -> None:
        if self.measure_points is None:
            self.measure_points = self.points

    @classmethod
    def build(
        cls,
        points: Iterable[Vec2],
        length_points: Optional[Iterable[Vec2]] = None,
    ) -> "CumulativePolyline":
        """Builds a cumulative polyline.

        Args:
            points: the displayed 2D points in the target coordinate system
            length_points: optional points in drawing units used for the exact
                distance axis; the count must match `points`. A paperspace
                viewport uses the modelspace points here, so the linetype phase
                stays in drawing units while the display is transformed.
        """
        display: list[Vec2] = []
        for point in points:
            if not display or not display[-1].isclose(point, abs_tol=1e-12):
                display.append(Vec2(point))
        if not display:
            display = [Vec2(0, 0), Vec2(0, 0)]
        elif len(display) == 1:
            display.append(display[0])
        if length_points is None:
            measure = display
        else:
            measure = [Vec2(point) for point in length_points]
            if len(measure) != len(display):
                raise ValueError("length_points must match points")
        distances = [Fraction(0)]
        for index in range(1, len(measure)):
            step = (measure[index] - measure[index - 1]).magnitude
            distances.append(distances[-1] + to_fraction(step))
        return cls(display, distances, measure)

    @property
    def length(self) -> Fraction:
        return self.distances[-1]

    def segments(self) -> Iterator[tuple[int, Vec2, Vec2, Fraction, Fraction]]:
        """Yields the displayed segments with their drawing-unit distances."""
        for index in range(len(self.points) - 1):
            yield (
                index,
                self.points[index],
                self.points[index + 1],
                self.distances[index],
                self.distances[index + 1],
            )

    def measure_segments(
        self,
    ) -> Iterator[tuple[int, Vec2, Vec2, Fraction, Fraction]]:
        """Yields the drawing-unit segments with their drawing-unit distances."""
        points = self.measure_points or self.points
        for index in range(len(points) - 1):
            yield (
                index,
                points[index],
                points[index + 1],
                self.distances[index],
                self.distances[index + 1],
            )

    def point_at(self, segment_index: int, distance: Fraction) -> Vec2:
        d0 = self.distances[segment_index]
        d1 = self.distances[segment_index + 1]
        start = self.points[segment_index]
        end = self.points[segment_index + 1]
        if d1 == d0:
            return Vec2(start)
        t = float((distance - d0) / (d1 - d0))
        return start.lerp(end, t)


@dataclass(frozen=True)
class SupportLine:
    """Canonical representation of an infinite 2D support line.

    Attributes:
        ux: x component of the unit direction, normalized to point to positive x
        uy: y component of the unit direction
        rho: signed distance of the line from the origin, ``normal . point``
    """

    ux: float
    uy: float
    rho: float

    def key(self) -> tuple[int, int, int]:
        return (
            round(self.ux * 1e9),
            round(self.uy * 1e9),
            round(self.rho * 1e6),
        )

    def coordinate(self, point: Vec2) -> float:
        return self.ux * point.x + self.uy * point.y


def support_line(start: Vec2, end: Vec2) -> SupportLine | None:
    """Returns the canonical support line of a segment or ``None`` for a point."""
    delta = end - start
    length = delta.magnitude
    if length < 1e-12:
        return None
    ux = delta.x / length
    uy = delta.y / length
    if ux < -1e-12 or (abs(ux) <= 1e-12 and uy < 0):
        ux, uy = -ux, -uy
    rho = -uy * start.x + ux * start.y
    return SupportLine(ux, uy, rho)


@dataclass(frozen=True)
class PeriodicSet:
    """A periodic set of intervals on one axis.

    The set contains every point ``x`` where ``(x - shift) mod period`` is inside
    one of the stored relative intervals. The shift preserves the phase of a
    source path and is never reset at a clipping boundary.

    Attributes:
        period: exact period length
        intervals: normalized intervals inside ``[0, period)``
        shift: phase shift of the set
    """

    period: Fraction
    intervals: tuple[tuple[Fraction, Fraction], ...]
    shift: Fraction = Fraction(0)

    def measure(self, start: Fraction, end: Fraction) -> Fraction:
        return periodic_measure(
            start - self.shift, end - self.shift, self.intervals, self.period
        )

    def stroke_count(self, start: Fraction, end: Fraction) -> int:
        return periodic_stroke_count(
            start - self.shift, end - self.shift, self.intervals, self.period
        )

    def dashes(
        self, start: Fraction, end: Fraction
    ) -> Iterator[tuple[Fraction, Fraction]]:
        yield from periodic_intervals(
            start - self.shift, end - self.shift, self.intervals, self.period
        )

    def contains(self, position: Fraction) -> bool:
        if self.period <= 0:
            return True
        relative = (position - self.shift) % self.period
        return any(lo <= relative < hi for lo, hi in self.intervals)

    def aligned(self) -> "PeriodicSet":
        """Returns this set with a zero shift by moving the intervals."""
        if not self.shift or self.period <= 0:
            return self
        shifted = ((lo + self.shift, hi + self.shift) for lo, hi in self.intervals)
        return PeriodicSet(self.period, normalize_intervals(shifted, self.period))


@dataclass(frozen=True)
class RangedSet:
    """A periodic set which is visible only inside finite ranges.

    Attributes:
        ranges: disjoint visible ranges in ascending order
        periodic: the periodic dash set, ``None`` for a solid line
    """

    ranges: tuple[tuple[Fraction, Fraction], ...]
    periodic: PeriodicSet | None

    def covers(self, position: Fraction) -> bool:
        return any(lo <= position < hi for lo, hi in self.ranges)

    def visible_measure(self) -> Fraction:
        return sum((hi - lo) for lo, hi in self.ranges)

    def covered_measure(self) -> Fraction:
        if self.periodic is None:
            return self.visible_measure()
        return sum(
            self.periodic.measure(lo, hi) for lo, hi in self.ranges
        )


def normalize_intervals(
    intervals: Iterable[tuple[Fraction, Fraction]], period: Fraction
) -> tuple[tuple[Fraction, Fraction], ...]:
    """Normalizes relative intervals to ``[0, period)`` and merges them."""
    if period <= 0:
        return tuple()
    parts: list[tuple[Fraction, Fraction]] = []
    for start, end in intervals:
        if end <= start:
            continue
        length = end - start
        if length >= period:
            return ((Fraction(0), period),)
        offset = start % period
        end_offset = offset + length
        if end_offset <= period:
            parts.append((offset, end_offset))
        else:
            parts.append((offset, period))
            parts.append((Fraction(0), end_offset - period))
    return merge_fraction_intervals(parts)


def merge_fraction_intervals(
    intervals: Iterable[tuple[Fraction, Fraction]],
) -> tuple[tuple[Fraction, Fraction], ...]:
    """Merges overlapping or touching intervals."""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    result: list[tuple[Fraction, Fraction]] = []
    for start, end in ordered:
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return tuple(result)


def periodic_measure(
    start: Fraction,
    end: Fraction,
    intervals: Sequence[tuple[Fraction, Fraction]],
    period: Fraction,
) -> Fraction:
    """Exact measure of a periodic interval set over ``[start, end)``.

    The set is ``k * period + interval`` for all integers ``k``. Only the first
    and the last repeat are inspected, all repeats in between use the measure of
    a single repeat.
    """
    if end <= start or period <= 0 or not intervals:
        return Fraction(0)
    total_period = sum((hi - lo) for lo, hi in intervals)
    if total_period == period:
        return end - start
    first = _floor_div(start, period)
    last = _floor_div(end, period)
    if first == last:
        return _overlap_one_period(start, end, intervals, period)
    total = _overlap_one_period(start, (first + 1) * period, intervals, period)
    total += _overlap_one_period(last * period, end, intervals, period)
    full = last - first - 1
    if full > 0:
        total += full * total_period
    return total


def periodic_stroke_count(
    start: Fraction,
    end: Fraction,
    intervals: Sequence[tuple[Fraction, Fraction]],
    period: Fraction,
) -> int:
    """Exact count of non-empty strokes intersecting ``[start, end)``."""
    if end <= start or period <= 0 or not intervals:
        return 0
    first = _floor_div(start, period)
    last = _floor_div(end, period)
    if first == last:
        return _count_one_period(start, end, intervals, period)
    count = _count_one_period(start, (first + 1) * period, intervals, period)
    count += _count_one_period(last * period, end, intervals, period)
    full = last - first - 1
    if full > 0:
        count += full * len(intervals)
    return count


def periodic_intervals(
    start: Fraction,
    end: Fraction,
    intervals: Sequence[tuple[Fraction, Fraction]],
    period: Fraction,
) -> Iterator[tuple[Fraction, Fraction]]:
    """Yields the actual intervals of the set which intersect ``[start, end)``."""
    if end <= start or period <= 0 or not intervals:
        return
    index = _floor_div(start, period)
    while index * period < end:
        base = index * period
        for lo, hi in intervals:
            left = max(start, base + lo)
            right = min(end, base + hi)
            if right > left:
                yield (left, right)
        index += 1


def _overlap_one_period(
    start: Fraction,
    end: Fraction,
    intervals: Sequence[tuple[Fraction, Fraction]],
    period: Fraction,
) -> Fraction:
    base = period * _floor_div(start, period)
    total = Fraction(0)
    for lo, hi in intervals:
        left = max(start, base + lo)
        right = min(end, base + hi)
        if right > left:
            total += right - left
    return total


def _count_one_period(
    start: Fraction,
    end: Fraction,
    intervals: Sequence[tuple[Fraction, Fraction]],
    period: Fraction,
) -> int:
    base = period * _floor_div(start, period)
    count = 0
    for lo, hi in intervals:
        left = max(start, base + lo)
        right = min(end, base + hi)
        if right > left:
            count += 1
    return count


def _floor_div(value: Fraction, divisor: Fraction) -> int:
    quotient = value / divisor
    return quotient.numerator // quotient.denominator


def union_ranged_measure(
    pieces: Sequence[RangedSet], start: Fraction, end: Fraction
) -> Fraction:
    """Exact measure of the union of ranged periodic sets over ``[start, end)``.

    The range endpoints split the request into slabs. Inside a slab the set of
    active pieces is constant, so only the periodic union of these pieces is
    measured. The number of slabs depends on the ranges, not on the strokes.
    """
    if end <= start or not pieces:
        return Fraction(0)
    boundaries: set[Fraction] = {start, end}
    for piece in pieces:
        for lo, hi in piece.ranges:
            if start < lo < end:
                boundaries.add(lo)
            if start < hi < end:
                boundaries.add(hi)
    ordered = sorted(boundaries)
    total = Fraction(0)
    for lo, hi in zip(ordered, ordered[1:]):
        if hi <= lo:
            continue
        mid = (lo + hi) / 2
        active = [piece for piece in pieces if piece.covers(mid)]
        if not active:
            continue
        total += _union_measure_of_slab(active, lo, hi)
    return total


def union_measure(
    start: Fraction, end: Fraction, pieces: Sequence[PeriodicSet]
) -> Fraction:
    """Exact measure of the union of periodic sets over ``[start, end)``."""
    if end <= start or not pieces:
        return Fraction(0)
    return _union_of_sets(list(pieces), start, end)


def _union_measure_of_slab(
    active: Sequence[RangedSet], start: Fraction, end: Fraction
) -> Fraction:
    periodic = [piece.periodic for piece in active if piece.periodic is not None]
    if len(periodic) < len(active):
        return end - start  # at least one solid line covers the whole slab
    return _union_of_sets(periodic, start, end)


def _union_of_sets(
    pieces: Sequence[PeriodicSet], start: Fraction, end: Fraction
) -> Fraction:
    groups = _group_by_period(pieces)
    if len(groups) == 1:
        return groups[0].measure(start, end)
    period = groups[0].period
    for group in groups[1:]:
        period = _lcm(period, group.period)
    profile = _union_profile(groups, period)
    return periodic_measure(start, end, profile, period)


_PROFILE_CACHE: dict[
    tuple[Fraction, tuple[tuple[Fraction, tuple[tuple[Fraction, Fraction], ...]], ...]],
    tuple[tuple[Fraction, Fraction], ...],
] = {}


def _union_profile(
    pieces: Sequence[PeriodicSet], common: Fraction
) -> tuple[tuple[Fraction, Fraction], ...]:
    """Builds the union of all sets inside one common repeat.

    The common repeat is built once and cached. It is never tiled across the
    requested range; :func:`periodic_measure` tiles the profile analytically.
    """
    key = (
        common,
        tuple(sorted((piece.period, piece.intervals) for piece in pieces)),
    )
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        return cached
    events: set[Fraction] = {Fraction(0), common}
    for piece in pieces:
        if piece.period <= 0:
            continue
        repeats = common / piece.period
        count = repeats.numerator // repeats.denominator
        if count * len(piece.intervals) > MAX_COMMON_EVENTS:
            raise CutUsageError(
                "the common repeat of the line patterns on one support line has "
                "too many events; use compatible patterns or split the layer"
            )
        for repeat in range(count):
            base = repeat * piece.period
            for lo, hi in piece.intervals:
                events.add(base + lo)
                events.add(base + hi)
    ordered = sorted(events)
    merged: list[tuple[Fraction, Fraction]] = []
    for lo, hi in zip(ordered, ordered[1:]):
        if hi <= lo:
            continue
        mid = (lo + hi) / 2
        if any(_covers(piece, mid) for piece in pieces):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
            else:
                merged.append((lo, hi))
    result = tuple(merged)
    _PROFILE_CACHE[key] = result
    return result


def _covers(piece: PeriodicSet, position: Fraction) -> bool:
    if piece.period <= 0:
        return True
    relative = position % piece.period
    return any(lo <= relative < hi for lo, hi in piece.intervals)


def _lcm(a: Fraction, b: Fraction) -> Fraction:
    """Least common multiple of two positive rational numbers."""
    if a <= 0 or b <= 0:
        return Fraction(0)
    num = a.numerator * b.numerator // gcd(a.numerator, b.numerator)
    den = gcd(a.denominator, b.denominator)
    return Fraction(num, den)


def _group_by_period(
    pieces: Sequence[PeriodicSet],
) -> list[PeriodicSet]:
    """Merges all periodic sets which share the same period."""
    by_period: dict[Fraction, list[PeriodicSet]] = {}
    for piece in pieces:
        if piece.period <= 0 or not piece.intervals:
            continue
        by_period.setdefault(piece.period, []).append(piece)
    groups: list[PeriodicSet] = []
    for period, members in by_period.items():
        merged = merge_fraction_intervals(
            interval for member in members for interval in member.aligned().intervals
        )
        if merged:
            groups.append(PeriodicSet(period, merged))
    return groups
