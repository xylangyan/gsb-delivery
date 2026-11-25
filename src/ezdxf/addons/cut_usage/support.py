# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Support line bookkeeping for the cut usage engine.

Two strokes on the same support line are cut only once. This module groups the
straight visible pieces of all source paths by their exact support line and
projects them onto one axis. Every piece keeps its own periodic dash set with its
own phase.

The cut length of a group is the measure of the union of the projected periodic
sets. It is computed analytically: a piece contributes a period and the visible
ranges on the axis, never a stroke list. A wide window with millions of strokes
therefore stays cheap.

The axis of a group uses display coordinates. The period of a piece is scaled by
the ratio of drawing units to display units, so a paperspace viewport measures in
drawing units even when the window uses paper coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Optional

from ezdxf.math import Vec2

from .geometry import (
    PeriodicSet,
    RangedSet,
    SupportLine,
    normalize_intervals,
    support_line,
    to_fraction,
    union_ranged_measure,
)
from .sources import SourcePath

__all__ = ["SupportGroup", "SegmentPiece", "group_sources"]

Interval = tuple[Fraction, Fraction]


@dataclass
class SegmentPiece:
    """A visible straight piece of a source path.

    Attributes:
        source: the originating source path
        segment_index: index of the source polyline segment
        start: piece start in display coordinates
        end: piece end in display coordinates
        d0: distance of the piece start on the source path axis in drawing units
        d1: distance of the piece end on the source path axis in drawing units
        periodic: the dash set of the source path in drawing units
        axis_lo: piece start on the display axis
        axis_hi: piece end on the display axis
        display_scale: drawing units per display unit
    """

    source: SourcePath
    segment_index: int
    start: Vec2
    end: Vec2
    d0: Fraction
    d1: Fraction
    periodic: Optional[PeriodicSet]
    axis_lo: Fraction = Fraction(0)
    axis_hi: Fraction = Fraction(0)
    axis_forward: bool = True
    display_scale: Fraction = Fraction(1)

    @property
    def distance(self) -> Fraction:
        return self.d1 - self.d0

    def axis_set(self) -> RangedSet:
        """Returns the piece as a ranged periodic set on the support axis.

        The axis uses display units. The affine map ``a = base + sign * d`` maps
        the drawing-unit distance ``d`` of the source path to the display axis
        ``a``. ``sign`` is ``drawing_units_per_display_unit`` for a piece which
        follows the canonical support direction and its negation otherwise. The
        period is converted with the same factor and the base keeps the exact
        source phase, including the source offset ``d0``.
        """
        lo = self.axis_lo
        hi = self.axis_hi
        if hi <= lo:
            return RangedSet(tuple(), None)
        if self.periodic is None:
            return RangedSet(((lo, hi),), None)
        span = self.d1 - self.d0
        if span <= 0 or self.display_scale <= 0:
            return RangedSet(tuple(), None)
        # display units per drawing unit:
        factor = 1 / self.display_scale
        # axis(d) = anchor + factor * (d - d0); the anchor is the axis coordinate
        # of the piece start at d0, which is axis_hi for a reversed piece
        anchor = self.axis_lo if self.axis_forward else self.axis_hi
        if not self.axis_forward:
            factor = -factor
        period = abs(self.periodic.period * factor)
        if period <= 0:
            return RangedSet(((lo, hi),), None)
        base = anchor + factor * (self.periodic.shift - self.d0)
        intervals: list[tuple[Fraction, Fraction]] = []
        for element_lo, element_hi in self.periodic.intervals:
            a = base + factor * element_lo
            b = base + factor * element_hi
            intervals.append((min(a, b), max(a, b)))
        return RangedSet(
            ((lo, hi),),
            PeriodicSet(period, normalize_intervals(intervals, period), Fraction(0)),
        )


@dataclass
class SupportGroup:
    """All visible pieces which share one support line on one layer.

    Attributes:
        support: canonical support line
        layer: process layer
        pieces: visible pieces on this line
        origin: a point on the line used as axis origin
        direction: unit direction of the line
    """

    support: SupportLine
    layer: str
    pieces: list[SegmentPiece] = field(default_factory=list)
    origin: Vec2 = field(default_factory=lambda: Vec2(0, 0))
    direction: Vec2 = field(default_factory=lambda: Vec2(1, 0))

    @property
    def ranged_sets(self) -> list[RangedSet]:
        return [piece.axis_set() for piece in self.pieces]

    def bounds(self) -> tuple[Fraction, Fraction]:
        xs = [
            value
            for piece in self.pieces
            for value in (piece.axis_lo, piece.axis_hi)
        ]
        if not xs:
            return Fraction(0), Fraction(0)
        return min(xs), max(xs)

    def measure(self) -> Fraction:
        """Exact deduplicated cut length of this support line.

        The union is measured on the display axis and converted back to drawing
        units. All pieces of a group share the same display scale, because one
        support line belongs to one layout.
        """
        pieces = self.ranged_sets
        if not pieces:
            return Fraction(0)
        lo, hi = self.bounds()
        if hi <= lo:
            return Fraction(0)
        display_measure = union_ranged_measure(pieces, lo, hi)
        scale = self.pieces[0].display_scale
        return display_measure * scale


def group_sources(
    pieces: Iterable[SegmentPiece],
) -> list[SupportGroup]:
    """Groups visible pieces by layer and exact support line."""
    groups: dict[tuple[tuple[int, int, int], str], SupportGroup] = {}
    for piece in pieces:
        support = support_line(piece.start, piece.end)
        if support is None:
            continue
        key = (support.key(), piece.source.layer.upper())
        group = groups.get(key)
        if group is None:
            direction = Vec2(support.ux, support.uy)
            normal = Vec2(-support.uy, support.ux)
            origin = normal * support.rho
            group = SupportGroup(
                support,
                piece.source.layer,
                origin=origin,
                direction=direction,
            )
            groups[key] = group
        _place_piece(group, piece)
        group.pieces.append(piece)
    return list(groups.values())


def _place_piece(group: SupportGroup, piece: SegmentPiece) -> None:
    """Projects the piece onto the group axis and stores the axis interval."""
    u = group.direction
    origin = group.origin
    a = u.dot(piece.start - origin)
    b = u.dot(piece.end - origin)
    piece.axis_lo = to_fraction(min(a, b))
    piece.axis_hi = to_fraction(max(a, b))
    piece.axis_forward = b >= a
    piece.display_scale = _display_scale(piece)


def _display_scale(piece: SegmentPiece) -> Fraction:
    """Returns drawing units per display unit for one piece."""
    display_span = (piece.end - piece.start).magnitude
    drawing_span = float(piece.d1 - piece.d0)
    if display_span < 1e-15 or drawing_span < 1e-15:
        return Fraction(1)
    return to_fraction(drawing_span / display_span)
