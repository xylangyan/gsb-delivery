# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""High level API of the cut usage add-on.

:class:`CutUsage` analyses one modelspace or one paperspace viewport. It collects
the source paths with their real linetype and layer inheritance, clips them with
the XCLIP boundaries of block references and the selected window, deduplicates the
strokes on each support line per layer and reports the results.

Two kinds of results are available:

- :meth:`CutUsage.layer_lengths` returns the exact cut length per layer. The
  computation is analytic and never expands the strokes of a wide window.
- :meth:`CutUsage.strokes` returns the exact visible strokes in a window with
  their original source. The window may be smaller than the analysed viewport.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Optional, Sequence

from ezdxf.math import Vec2

from .clipping import Window, make_window
from .geometry import CutUsageError, to_fraction
from .sources import SourceCollector, SourcePath
from .strokes import clip_segment_ranges, dashes_of, source_periodic_set
from .support import SegmentPiece, SupportGroup, group_sources

__all__ = [
    "CutUsage",
    "CutUsageError",
    "LayerLength",
    "Stroke",
    "layer_lengths",
    "window_strokes",
]


class LayerLength:
    """Cut length result of one process layer.

    Attributes:
        layer: layer name
        length: exact visible cut length in drawing units
        source_count: number of source paths which contribute to this layer
        support_lines: number of distinct support lines on this layer
    """

    __slots__ = ("layer", "length", "source_count", "support_lines")

    def __init__(
        self,
        layer: str,
        length: Fraction,
        source_count: int,
        support_lines: int = 0,
    ) -> None:
        self.layer = layer
        self.length = length
        self.source_count = source_count
        self.support_lines = support_lines

    @property
    def length_float(self) -> float:
        return float(self.length)

    def __repr__(self) -> str:
        return (
            f"LayerLength(layer={self.layer!r}, length={float(self.length):g}, "
            f"support_lines={self.support_lines}, "
            f"source_count={self.source_count})"
        )


class Stroke:
    """One visible stroke inside a clipping window.

    Attributes:
        layer: process layer
        start: start point of the stroke in target coordinates
        end: end point of the stroke in target coordinates
        length: exact stroke length in drawing units
        source: the :class:`~ezdxf.addons.cut_usage.sources.SourcePath` which
            produced this stroke
        source_index: index of the source path in the analysis
        segment_index: index of the source polyline segment
        distance: distance of the stroke start on the source path axis
        dash_index: index of the dash on the source path axis
        deduplicated: ``True`` when an earlier stroke on the same support line
            already covers this stroke
    """

    __slots__ = (
        "layer",
        "start",
        "end",
        "length",
        "source",
        "source_index",
        "segment_index",
        "distance",
        "dash_index",
        "deduplicated",
    )

    def __init__(
        self,
        layer: str,
        start: Vec2,
        end: Vec2,
        length: Fraction,
        source: SourcePath,
        source_index: int,
        segment_index: int,
        distance: Fraction,
        dash_index: int,
        deduplicated: bool = False,
    ) -> None:
        self.layer = layer
        self.start = start
        self.end = end
        self.length = length
        self.source = source
        self.source_index = source_index
        self.segment_index = segment_index
        self.distance = distance
        self.dash_index = dash_index
        self.deduplicated = deduplicated

    @property
    def origin(self) -> str:
        return self.source.origin

    def __repr__(self) -> str:
        return (
            f"Stroke(layer={self.layer!r}, "
            f"start=({float(self.start.x):g}, {float(self.start.y):g}), "
            f"end=({float(self.end.x):g}, {float(self.end.y):g}), "
            f"origin={self.origin!r})"
        )


class CutUsage:
    """Cut usage analysis for one layout or one paperspace viewport.

    Args:
        doc: the DXF document
        layout: the layout to analyse, the modelspace is used by default
        viewport: a paperspace VIEWPORT entity; the analysis then uses its window,
            its layer overrides and its linetype scale
        layers: optional limit to these process layers, ``None`` reports all
            layers
        window: optional additional clipping window in target coordinates
    """

    def __init__(
        self,
        doc,
        layout=None,
        *,
        viewport=None,
        layers: Optional[Iterable[str]] = None,
        window: Optional[Window] = None,
    ) -> None:
        self.doc = doc
        self.layout = layout if layout is not None else doc.modelspace()
        self.viewport = viewport
        if viewport is not None:
            from .viewports import viewport_matrix, viewport_window

            viewport_clip = viewport_window(viewport)
            base_matrix = viewport_matrix(viewport)
        else:
            viewport_clip = None
            base_matrix = None
        self.window = _combine_windows(viewport_clip, window)
        self._collector = SourceCollector(
            doc, viewport=viewport, layers=layers, base_matrix=base_matrix
        )
        self._sources: Optional[list[SourcePath]] = None
        self._groups: Optional[list[SupportGroup]] = None

    @property
    def sources(self) -> list[SourcePath]:
        """The collected source paths in drawing order."""
        if self._sources is None:
            self._sources = self._collector.collect(self.layout)
        return self._sources

    @property
    def groups(self) -> list[SupportGroup]:
        """The visible pieces grouped by process layer and support line."""
        if self._groups is None:
            self._groups = group_sources(self._pieces())
        return self._groups

    def _analysis_windows(self) -> tuple[Window, ...]:
        return (self.window,) if self.window is not None else tuple()

    def _pieces(self) -> list[SegmentPiece]:
        return _build_pieces(self.sources, self._analysis_windows())

    def layer_lengths(self) -> list[LayerLength]:
        """Returns the exact cut length of every process layer.

        The result is sorted by layer name. The method is analytic and never
        expands the strokes of the analysed window.
        """
        by_layer: dict[str, list[SupportGroup]] = {}
        for group in self.groups:
            by_layer.setdefault(group.layer, []).append(group)
        result: list[LayerLength] = []
        for layer in sorted(by_layer):
            groups = by_layer[layer]
            total = Fraction(0)
            for group in groups:
                total += group.measure()
            sources = set()
            for group in groups:
                for piece in group.pieces:
                    sources.add(id(piece.source))
            result.append(
                LayerLength(layer, total, len(sources), len(groups))
            )
        return result

    def layer_length(self, layer: str) -> Fraction:
        """Returns the exact cut length of one layer."""
        for entry in self.layer_lengths():
            if entry.layer.upper() == str(layer).upper():
                return entry.length
        return Fraction(0)

    def strokes(
        self,
        window: Optional[Window] = None,
        *,
        layer: Optional[str] = None,
        include_deduplicated: bool = False,
    ) -> list[Stroke]:
        """Returns the exact visible strokes of a window.

        Args:
            window: the query window in target coordinates, ``None`` uses the
                analysis window
            layer: optional layer filter
            include_deduplicated: when ``True`` all source strokes are returned
                and coincident strokes carry ``deduplicated=True``; when ``False``
                only the first stroke of coincident strokes is returned
        """
        query = window if window is not None else self.window
        windows = (query,) if query is not None else tuple()
        pieces = _build_pieces(self.sources, windows)
        if layer is not None:
            pieces = [
                piece
                for piece in pieces
                if piece.source.layer.upper() == str(layer).upper()
            ]
        groups = group_sources(pieces)
        result: list[Stroke] = []
        for group in groups:
            group_strokes: list[Stroke] = []
            for piece in group.pieces:
                group_strokes.extend(_piece_strokes(piece))
            if include_deduplicated:
                result.extend(_mark_deduplicated(group, group_strokes))
            else:
                result.extend(_drop_deduplicated(group, group_strokes))
        result.sort(
            key=lambda stroke: (
                stroke.layer.upper(),
                stroke.source_index,
                stroke.segment_index,
                stroke.distance,
            )
        )
        return result

    def strokes_in(
        self, p1: Vec2, p2: Vec2, *, layer: Optional[str] = None
    ) -> list[Stroke]:
        """Returns the exact visible strokes of a rectangular paper window."""
        p1 = Vec2(p1)
        p2 = Vec2(p2)
        low = Vec2(min(p1.x, p2.x), min(p1.y, p2.y))
        high = Vec2(max(p1.x, p2.x), max(p1.y, p2.y))
        window = make_window([low, Vec2(high.x, low.y), high, Vec2(low.x, high.y)])
        return self.strokes(Window("query", [window.vertices]), layer=layer)


def _build_pieces(
    sources: Iterable[SourcePath], windows: Sequence[Window]
) -> list[SegmentPiece]:
    pieces: list[SegmentPiece] = []
    for source in sources:
        periodic = source_periodic_set(source)
        clip_windows = source.windows + tuple(windows)
        for index, start, end, d0, d1 in source.geometry.segments():
            if d1 <= d0:
                continue
            ranges = clip_segment_ranges(start, end, d0, d1, clip_windows)
            span = d1 - d0
            for lo, hi in ranges:
                if hi <= lo:
                    continue
                t0 = float((lo - d0) / span)
                t1 = float((hi - d0) / span)
                pieces.append(
                    SegmentPiece(
                        source=source,
                        segment_index=index,
                        start=start.lerp(end, t0),
                        end=start.lerp(end, t1),
                        d0=lo,
                        d1=hi,
                        periodic=periodic,
                    )
                )
    return pieces


def _piece_strokes(piece: SegmentPiece) -> list[Stroke]:
    source = piece.source
    span = piece.d1 - piece.d0
    if span <= 0:
        return []
    result: list[Stroke] = []
    dash_index = _first_dash_index(piece.periodic, piece.d0)
    for lo, hi in dashes_of(piece.periodic, piece.d0, piece.d1):
        a = float((lo - piece.d0) / span)
        b = float((hi - piece.d0) / span)
        result.append(
            Stroke(
                layer=source.layer,
                start=piece.start.lerp(piece.end, a),
                end=piece.start.lerp(piece.end, b),
                length=hi - lo,
                source=source,
                source_index=source.index,
                segment_index=piece.segment_index,
                distance=lo,
                dash_index=dash_index,
            )
        )
        dash_index += 1
    return result


def _first_dash_index(periodic, position: Fraction) -> int:
    if periodic is None or periodic.period <= 0:
        return 0
    relative = (position - periodic.shift) % periodic.period
    index = 0
    for lo, hi in periodic.intervals:
        if lo <= relative < hi:
            return index
        index += 1
    return 0


def _axis_interval(
    group: SupportGroup, stroke: Stroke
) -> tuple[Fraction, Fraction]:
    a = to_fraction(group.direction.dot(stroke.start - group.origin))
    b = to_fraction(group.direction.dot(stroke.end - group.origin))
    return (min(a, b), max(a, b))


def _drop_deduplicated(
    group: SupportGroup, strokes: Sequence[Stroke]
) -> list[Stroke]:
    """Returns only the strokes which are not already cut.

    A stroke which is partly covered by an earlier stroke is split, so the
    returned strokes describe the exact new cut path.
    """
    result: list[Stroke] = []
    cut: list[tuple[Fraction, Fraction]] = []
    for stroke in strokes:
        lo, hi = _axis_interval(group, stroke)
        remainder = _subtract(cut, lo, hi)
        for part_lo, part_hi in remainder:
            if part_lo == lo and part_hi == hi:
                result.append(stroke)
            else:
                result.append(_slice_stroke(stroke, group, part_lo, part_hi, lo, hi))
        cut = _merge(cut + [(lo, hi)])
    return result


def _mark_deduplicated(
    group: SupportGroup, strokes: Sequence[Stroke]
) -> list[Stroke]:
    cut: list[tuple[Fraction, Fraction]] = []
    for stroke in strokes:
        lo, hi = _axis_interval(group, stroke)
        stroke.deduplicated = _is_fully_covered(cut, lo, hi)
        cut = _merge(cut + [(lo, hi)])
    return list(strokes)


def _slice_stroke(
    stroke: Stroke,
    group: SupportGroup,
    part_lo: Fraction,
    part_hi: Fraction,
    lo: Fraction,
    hi: Fraction,
) -> Stroke:
    if hi <= lo:
        return stroke
    t0 = float((part_lo - lo) / (hi - lo))
    t1 = float((part_hi - lo) / (hi - lo))
    return Stroke(
        layer=stroke.layer,
        start=stroke.start.lerp(stroke.end, t0),
        end=stroke.start.lerp(stroke.end, t1),
        length=stroke.length * (part_hi - part_lo) / (hi - lo),
        source=stroke.source,
        source_index=stroke.source_index,
        segment_index=stroke.segment_index,
        distance=stroke.distance + (stroke.length * to_fraction(t0)),
        dash_index=stroke.dash_index,
        deduplicated=False,
    )


def _subtract(
    cut: Sequence[tuple[Fraction, Fraction]], lo: Fraction, hi: Fraction
) -> list[tuple[Fraction, Fraction]]:
    """Returns the parts of ``[lo, hi)`` which are not covered by ``cut``."""
    if hi <= lo:
        return []
    pieces: list[tuple[Fraction, Fraction]] = []
    cursor = lo
    for a, b in cut:
        if b <= cursor:
            continue
        if a >= hi:
            break
        if a > cursor:
            pieces.append((cursor, min(a, hi)))
        cursor = max(cursor, b)
        if cursor >= hi:
            break
    if cursor < hi:
        pieces.append((cursor, hi))
    return pieces


def _is_fully_covered(
    cut: Sequence[tuple[Fraction, Fraction]], lo: Fraction, hi: Fraction
) -> bool:
    if hi <= lo:
        return True
    cursor = lo
    for a, b in cut:
        if b <= cursor:
            continue
        if a > cursor:
            return False
        cursor = max(cursor, b)
        if cursor >= hi:
            return True
    return False


def _merge(
    intervals: Sequence[tuple[Fraction, Fraction]]
) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted((lo, hi) for lo, hi in intervals if hi > lo)
    result: list[tuple[Fraction, Fraction]] = []
    for lo, hi in ordered:
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], hi))
        else:
            result.append((lo, hi))
    return result


def _combine_windows(*windows) -> Optional[Window]:
    active = [window for window in windows if window is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    polygons: list[list[Vec2]] = []
    inverted = False
    bounds = None
    for window in active:
        if window.inverted:
            inverted = True
            bounds = window.bounds
        else:
            polygons.extend(window.polygons)
    return Window("combined", polygons, inverted=inverted, bounds=bounds)


def layer_lengths(doc, layout=None, **kwargs) -> list[LayerLength]:
    """Convenience function for :meth:`CutUsage.layer_lengths`."""
    return CutUsage(doc, layout, **kwargs).layer_lengths()


def window_strokes(
    doc, layout=None, window: Optional[Window] = None, **kwargs
) -> list[Stroke]:
    """Convenience function for :meth:`CutUsage.strokes`."""
    usage = CutUsage(doc, layout, window=window, **kwargs)
    return usage.strokes()
