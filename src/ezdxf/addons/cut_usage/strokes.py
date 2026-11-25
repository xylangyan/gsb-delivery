# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Describe the visible strokes of a source path.

A source path is an unclipped polyline with an exact distance axis. The dashes of
its line pattern are intervals on this axis. A clipping window changes the
visible ranges of the axis but never the phase of the pattern.

Both the analytic totals and the stroke list use the functions of this module,
therefore a total and a stroke list agree.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterator, Optional, Sequence

from ezdxf.math import Vec2

from .clipping import Window
from .geometry import (
    PeriodicSet,
    to_fraction,
)
from .sources import SourcePath

__all__ = [
    "clip_segment_ranges",
    "dashes_of",
    "source_periodic_set",
]


def source_periodic_set(source: SourcePath) -> Optional[PeriodicSet]:
    """Returns the periodic dash set of a source path.

    A solid line returns ``None``. The pattern phase is kept in the set shift, so
    two sources with different accumulated distances keep different phases.
    """
    dashes = source.dashes
    if len(dashes) < 2:
        return None
    period = sum(dashes, Fraction(0))
    if period <= 0:
        return None
    intervals: list[tuple[Fraction, Fraction]] = []
    position = Fraction(0)
    for index, value in enumerate(dashes):
        if index % 2 == 0 and value > 0:
            intervals.append((position, position + value))
        position += value
    return PeriodicSet(period, tuple(intervals), source.dash_offset)


def clip_segment_ranges(
    start: Vec2, end: Vec2, d0: Fraction, d1: Fraction, windows: Sequence[Window]
) -> list[tuple[Fraction, Fraction]]:
    """Returns the visible distance ranges of one straight segment.

    Every window clips the segment and the visible parameter is mapped back to
    the exact distance axis of the source path. The phase origin never moves.
    """
    if d1 <= d0:
        return []
    ranges: list[tuple[Fraction, Fraction]] = [(Fraction(0), Fraction(1))]
    for window in windows:
        if not ranges:
            return []
        next_ranges: list[tuple[Fraction, Fraction]] = []
        for lo, hi in ranges:
            p_lo = _point(start, end, lo)
            p_hi = _point(start, end, hi)
            for t_lo, t_hi in window.clip_line_params(p_lo, p_hi):
                if t_hi <= t_lo:
                    continue
                next_ranges.append(
                    (
                        lo + (hi - lo) * to_fraction(t_lo),
                        lo + (hi - lo) * to_fraction(t_hi),
                    )
                )
        ranges = _merge_ranges(next_ranges)
    span = d1 - d0
    return [
        (d0 + span * lo, d0 + span * hi)
        for lo, hi in _merge_ranges(ranges)
        if hi > lo
    ]


def dashes_of(
    periodic: Optional[PeriodicSet], start: Fraction, end: Fraction
) -> Iterator[tuple[Fraction, Fraction]]:
    """Yields the dash intervals of a periodic set which intersect a range."""
    if periodic is None:
        if end > start:
            yield (start, end)
        return
    for lo, hi in periodic.dashes(start, end):
        left = max(lo, start)
        right = min(hi, end)
        if right > left:
            yield (left, right)


def _point(start: Vec2, end: Vec2, t: Fraction) -> Vec2:
    return start.lerp(end, float(t))


def _merge_ranges(
    ranges: Sequence[tuple[Fraction, Fraction]],
) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted((lo, hi) for lo, hi in ranges if hi > lo)
    result: list[tuple[Fraction, Fraction]] = []
    for lo, hi in ordered:
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], hi))
        else:
            result.append((lo, hi))
    return result
