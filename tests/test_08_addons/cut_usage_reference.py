#  Copyright (c) 2026, Manfred Moitzi
#  License: MIT License
"""Reference implementation for the cut usage tests.

The reference follows the specification literally: draw each source path
completely with its linetype, then clip the drawn strokes with the window. This is
slow and only suitable for short sample drawings, where it is used to verify the
analytic totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from ezdxf.math import Vec2

from ezdxf.addons.cut_usage.clipping import Window
from ezdxf.addons.cut_usage.engine import CutUsage
from ezdxf.addons.cut_usage.geometry import to_fraction
from ezdxf.addons.cut_usage.strokes import source_periodic_set


@dataclass
class RefStroke:
    layer: str
    start: Vec2
    end: Vec2
    length: Fraction
    source_origin: str
    distance: Fraction


def draw_then_clip(
    usage: CutUsage,
    window: Optional[Window] = None,
    *,
    layer: Optional[str] = None,
) -> list[RefStroke]:
    """Draws every source path completely and clips the drawn strokes.

    The dash phase comes from the unclipped source path. Clipping only selects
    and splits the drawn strokes.
    """
    from ezdxf.addons.cut_usage.engine import _build_pieces, _piece_strokes
    from ezdxf.addons.cut_usage.sources import SourcePath

    query_windows = ()
    if window is not None:
        query_windows = (window,)
    result: list[RefStroke] = []
    for source in usage.sources:
        if layer is not None and source.layer.upper() != str(layer).upper():
            continue
        full_pieces = _build_pieces([source], tuple(source.windows))
        for piece in full_pieces:
            for stroke in _piece_strokes(piece):
                if query_windows:
                    for clipped in _clip_stroke(stroke, query_windows[0]):
                        result.append(clipped)
                else:
                    result.append(
                        RefStroke(
                            stroke.layer,
                            stroke.start,
                            stroke.end,
                            stroke.length,
                            stroke.origin,
                            stroke.distance,
                        )
                    )
    return result


def _clip_stroke(stroke, window: Window) -> list[RefStroke]:
    result: list[RefStroke] = []
    for lo, hi in window.clip_line_params(stroke.start, stroke.end):
        if hi <= lo:
            continue
        start = stroke.start.lerp(stroke.end, lo)
        end = stroke.start.lerp(stroke.end, hi)
        length = stroke.length * to_fraction(hi - lo)
        result.append(
            RefStroke(stroke.layer, start, end, length, stroke.origin, stroke.distance)
        )
    return result


def deduplicated_length(
    strokes: Iterable[RefStroke], *, per_layer: bool = True
) -> Fraction:
    """Exact union length of the reference strokes on their support lines.

    The total is grouped by support line and, by default, by layer. Only
    coincident strokes of the same layer are merged.
    """
    from ezdxf.addons.cut_usage.geometry import support_line

    by_line: dict[tuple, list[tuple[Fraction, Fraction]]] = {}
    for stroke in strokes:
        line = support_line(stroke.start, stroke.end)
        if line is None:
            key = (
                "point",
                stroke.layer if per_layer else "",
                round(stroke.start.x, 9),
                round(stroke.start.y, 9),
            )
            axis_start = Fraction(0)
            axis_end = Fraction(0)
        else:
            key = (
                line.key(),
                stroke.layer.upper() if per_layer else "",
            )
            u = Vec2(line.ux, line.uy)
            normal = Vec2(-line.uy, line.ux)
            origin = normal * line.rho
            a = to_fraction(u.dot(stroke.start - origin))
            b = to_fraction(u.dot(stroke.end - origin))
            axis_start, axis_end = min(a, b), max(a, b)
        by_line.setdefault(key, []).append((axis_start, axis_end))
    total = Fraction(0)
    for intervals in by_line.values():
        ordered = sorted(intervals)
        merged: list[tuple[Fraction, Fraction]] = []
        for lo, hi in ordered:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        total += sum((hi - lo) for lo, hi in merged if hi > lo)
    return total
