# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Resolve drawing content into cuttable source paths.

A source path is an unclipped polyline in modelspace coordinates. It carries
everything needed to reproduce its strokes later:

- the accumulated distance axis and therefore the exact linetype phase
- the exact line pattern and its effective scale
- the process layer which owns the cut length
- the clipping windows of enclosing block references
- the originating INSERT stack and DXF entity for traceability

Phases are always relative to the source path itself. Mirroring, rotation and
clipping only reorder or cut the distance axis; they never reset a phase.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from ezdxf.entities import DXFGraphic, Insert, LWPolyline, Polyline, Line, Viewport
from ezdxf.math import Matrix44, Vec2
from ezdxf.path import make_path
from ezdxf.xclip import XClip

from .clipping import Window, triangulate_polygon
from .geometry import CumulativePolyline, to_fraction

__all__ = ["SourcePath", "SourceCollector", "resolve_pattern", "FLATTENING_DISTANCE"]

FLATTENING_DISTANCE = 1e-4

CONTINUOUS_NAMES = {"CONTINUOUS", "BYLAYER", "BYBLOCK", ""}


@dataclass
class SourcePath:
    """An unclipped source path with exact cumulative distances.

    Attributes:
        geometry: cumulative polyline in modelspace coordinates
        dashes: scaled dash-gap sequence, an empty sequence is a solid line
        layer: process layer which owns the cut length
        entity: the originating DXF entity
        insert_stack: handles of the INSERT entities which produced this path
        origin: human readable origin, e.g. ``"INSERT A3 / LINE 2F"``
        dash_offset: pattern phase at distance zero, usually zero
        windows: XCLIP windows of enclosing block references in modelspace
    """

    geometry: CumulativePolyline
    dashes: tuple[Fraction, ...]
    layer: str
    entity: Optional[DXFGraphic] = None
    insert_stack: tuple[str, ...] = tuple()
    origin: str = ""
    dash_offset: Fraction = Fraction(0)
    windows: tuple[Window, ...] = tuple()
    index: int = -1

    @property
    def length(self) -> Fraction:
        return self.geometry.length

    @property
    def is_solid(self) -> bool:
        return len(self.dashes) < 2

    def phase_at(self, distance: Fraction) -> Fraction:
        return distance + self.dash_offset


def resolve_pattern(
    entity: DXFGraphic,
    layer: str,
    *,
    viewport: Optional[Viewport] = None,
    inherited_linetype: Optional[str] = None,
    global_scale: float = 1.0,
    paper_scale: bool = True,
) -> tuple[Fraction, ...]:
    """Returns the effective scaled dash-gap sequence for ``entity``.

    Rules applied in this order:

    1. an explicit entity linetype, otherwise the inherited linetype of the
       enclosing block reference, otherwise the linetype of the process layer
    2. a VIEWPORT layer override when the entity is rendered through that viewport
    3. the entity linetype scale ``ltscale`` and the global ``$LTSCALE``

    The pattern stays in drawing units. A paperspace viewport maps drawing units
    to paper units by its viewport transform; the paper appearance of the pattern
    is therefore already correct and must not be compensated twice. ``$PSLTSCALE``
    only affects the display and does not change the cut length.
    """
    doc = entity.doc
    if doc is None:
        return tuple()
    linetype_name = str(entity.dxf.get("linetype", "BYLAYER"))
    upper = linetype_name.upper()
    if upper == "BYBLOCK" and inherited_linetype:
        linetype_name = inherited_linetype
    elif upper in CONTINUOUS_NAMES:
        linetype_name = _layer_linetype(doc, layer)
    if viewport is not None:
        linetype_name = _viewport_linetype(doc, layer, viewport, linetype_name)
    linetype_name = str(linetype_name).upper()
    if linetype_name in CONTINUOUS_NAMES:
        return tuple()
    try:
        linetype = doc.linetypes.get(linetype_name)
    except Exception:
        return tuple()
    if linetype is None:
        return tuple()
    pattern = linetype.simplified_line_pattern()
    if len(pattern) < 2 or not any(value > 0 for value in pattern):
        return tuple()
    scale = float(entity.dxf.get("ltscale", 1.0)) * float(global_scale)
    if not math.isfinite(scale) or scale <= 0:
        return tuple()
    scaled: list[Fraction] = []
    for value in pattern:
        value = abs(float(value)) * scale
        if not math.isfinite(value) or value <= 0:
            return tuple()
        scaled.append(to_fraction(value))
    if len(scaled) % 2:
        scaled.pop()
    return tuple(scaled) if len(scaled) >= 2 else tuple()


def _layer_linetype(doc, layer: str) -> str:
    try:
        entry = doc.layers.get(layer)
    except Exception:
        return "CONTINUOUS"
    if entry is None:
        return "CONTINUOUS"
    return str(entry.dxf.get("linetype", "CONTINUOUS"))


def _viewport_linetype(doc, layer: str, viewport: Viewport, default: str) -> str:
    try:
        entry = doc.layers.get(layer)
    except Exception:
        return default
    if entry is None:
        return default
    overrides = entry.get_vp_overrides()
    handle = viewport.dxf.get("handle", "")
    if handle and overrides.has_overrides(handle):
        return str(overrides.get_linetype(handle) or default)
    return default


class SourceCollector:
    """Collects source paths from a layout or from a block reference stack.

    Args:
        doc: the DXF document
        viewport: the paperspace viewport which defines layer overrides and
            linetype scaling, ``None`` for modelspace
        layers: optional process layer filter, ``None`` reports all layers
        base_matrix: optional transform from the layout coordinates into the target
            coordinate system, used to map modelspace into a paperspace viewport
    """

    def __init__(
        self,
        doc,
        *,
        viewport: Optional[Viewport] = None,
        layers: Optional[Iterable[str]] = None,
        global_scale: Optional[float] = None,
        paper_scale: Optional[bool] = None,
        base_matrix: Optional[Matrix44] = None,
    ) -> None:
        self.doc = doc
        self.viewport = viewport
        self.base_matrix = base_matrix
        if base_matrix is not None and viewport is None:
            raise ValueError("a base matrix requires a viewport for the linetype scale")
        self.layers = {str(name).upper() for name in layers} if layers else None
        if global_scale is None:
            global_scale = float(doc.header.get("$LTSCALE", 1.0))
        self.global_scale = global_scale
        if paper_scale is None:
            paper_scale = bool(doc.header.get("$PSLTSCALE", 1))
        self.paper_scale = paper_scale
        self._pattern_cache: dict[tuple, tuple[Fraction, ...]] = {}
        self._frozen: set[str] = set()
        if viewport is not None:
            self._frozen = {str(name).upper() for name in viewport.frozen_layers}

    def pattern_for(
        self, entity: DXFGraphic, layer: str, inherited_linetype: Optional[str]
    ) -> tuple[Fraction, ...]:
        key = (
            str(entity.dxf.get("linetype", "BYLAYER")),
            _round_scale(entity.dxf.get("ltscale", 1.0)),
            layer.upper(),
            (inherited_linetype or "").upper(),
            self.viewport.dxf.get("handle", "") if self.viewport else "",
        )
        cached = self._pattern_cache.get(key)
        if cached is None:
            cached = resolve_pattern(
                entity,
                layer,
                viewport=self.viewport,
                inherited_linetype=inherited_linetype,
                global_scale=self.global_scale,
                paper_scale=self.paper_scale,
            )
            self._pattern_cache[key] = cached
        return cached

    def collect(self, layout) -> list[SourcePath]:
        """Collects all supported source paths of the given layout."""
        result: list[SourcePath] = []
        self._collect_entities(
            list(layout),
            matrix=Matrix44(),
            layer_context=None,
            linetype_context=None,
            insert_stack=(),
            windows=(),
            result=result,
        )
        for index, source in enumerate(result):
            source.index = index
        return result

    def _collect_entities(
        self,
        entities: Iterable[DXFGraphic],
        *,
        matrix: Matrix44,
        layer_context: Optional[str],
        linetype_context: Optional[str],
        insert_stack: tuple[str, ...],
        windows: tuple[Window, ...],
        result: list[SourcePath],
    ) -> None:
        for entity in entities:
            if isinstance(entity, Insert):
                self._collect_insert(
                    entity,
                    matrix=matrix,
                    layer_context=layer_context,
                    linetype_context=linetype_context,
                    insert_stack=insert_stack,
                    windows=windows,
                    result=result,
                )
            else:
                self._collect_graphic(
                    entity,
                    matrix=matrix,
                    layer_context=layer_context,
                    linetype_context=linetype_context,
                    insert_stack=insert_stack,
                    windows=windows,
                    result=result,
                )

    def _collect_insert(
        self,
        insert: Insert,
        *,
        matrix: Matrix44,
        layer_context: Optional[str],
        linetype_context: Optional[str],
        insert_stack: tuple[str, ...],
        windows: tuple[Window, ...],
        result: list[SourcePath],
    ) -> None:
        block = insert.block()
        if block is None:
            return
        insert_layer = str(insert.dxf.get("layer", layer_context or "0"))
        if not self._layer_visible(insert_layer):
            return
        insert_linetype = str(insert.dxf.get("linetype", "BYLAYER"))
        if insert_linetype.upper() in CONTINUOUS_NAMES:
            insert_linetype = _layer_linetype(self.doc, insert_layer)
        stacked = insert_stack + (insert.dxf.get("handle", ""),)
        try:
            base_matrix = insert.matrix44()
            base_matrix.inverse()
            has_base = True
        except ZeroDivisionError:
            has_base = False
        for cell in insert.multi_insert():
            cell_matrix = cell.matrix44()
            # the XCLIP path is defined in the WCS of the base insert; a MINSERT
            # cell moves the whole clipped block, therefore its window moves too
            if has_base:
                cell_local = cell_matrix @ base_matrix
            else:
                cell_local = Matrix44()
            cell_windows = windows + self._insert_windows(
                insert, cell_local @ matrix
            )
            self._collect_entities(
                list(block),
                matrix=cell_matrix @ matrix,
                layer_context=insert_layer,
                linetype_context=insert_linetype,
                insert_stack=stacked,
                windows=cell_windows,
                result=result,
            )

    def _insert_windows(self, insert: Insert, matrix: Matrix44) -> tuple[Window, ...]:
        clip = XClip(insert)
        if not (clip.has_clipping_path and clip.is_clipping_enabled):
            return tuple()
        clipping_path = clip.get_wcs_clipping_path()
        vertices = [Vec2(matrix.transform(vertex)) for vertex in clipping_path.vertices]
        if len(vertices) < 2:
            return tuple()
        if clipping_path.is_inverted_clip:
            inner = [
                Vec2(matrix.transform(vertex))
                for vertex in clipping_path.inner_polygon()
            ]
            bounds_box = clipping_path.outer_bounds()
            corners = [
                Vec2(matrix.transform(vertex))
                for vertex in bounds_box.rect_vertices()
            ]
            xs = [vertex.x for vertex in corners]
            ys = [vertex.y for vertex in corners]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            return (
                Window(
                    name=f"XCLIP {insert.dxf.get('handle', '')}",
                    polygons=triangulate_polygon(inner),
                    inverted=True,
                    bounds=bounds,
                ),
            )
        return (
            Window(
                name=f"XCLIP {insert.dxf.get('handle', '')}",
                polygons=triangulate_polygon(vertices),
            ),
        )

    def _collect_graphic(
        self,
        entity: DXFGraphic,
        *,
        matrix: Matrix44,
        layer_context: Optional[str],
        linetype_context: Optional[str],
        insert_stack: tuple[str, ...],
        windows: tuple[Window, ...],
        result: list[SourcePath],
    ) -> None:
        if not isinstance(entity, (Line, LWPolyline, Polyline)):
            return
        if isinstance(entity, Polyline) and (
            entity.is_polygon_mesh or entity.is_poly_face_mesh
        ):
            return
        layer = self._process_layer(entity, layer_context)
        if not self._layer_visible(layer):
            return
        if self.layers is not None and layer.upper() not in self.layers:
            return
        try:
            path = make_path(entity)
        except (TypeError, ValueError):
            return
        drawing_points = [
            Vec2(matrix.transform(vertex))
            for vertex in path.flattening(FLATTENING_DISTANCE, segments=16)
        ]
        if len(drawing_points) < 2:
            return
        if self.base_matrix is None:
            display_points = drawing_points
            display_windows = windows
        else:
            display_points = [
                Vec2(self.base_matrix.transform(vertex))
                for vertex in drawing_points
            ]
            display_windows = tuple(
                window.transform(self.base_matrix) for window in windows
            )
        geometry = CumulativePolyline.build(display_points, drawing_points)
        if geometry.length <= 0:
            return
        dashes = self.pattern_for(entity, layer, linetype_context)
        result.append(
            SourcePath(
                geometry=geometry,
                dashes=dashes,
                layer=layer,
                entity=entity,
                insert_stack=insert_stack,
                origin=self._origin(entity, insert_stack),
                windows=display_windows,
            )
        )

    def _process_layer(
        self, entity: DXFGraphic, layer_context: Optional[str]
    ) -> str:
        layer = str(entity.dxf.get("layer", "0"))
        if layer == "0" and layer_context:
            return str(layer_context)
        return layer

    def _layer_visible(self, layer: str) -> bool:
        upper = layer.upper()
        if upper in self._frozen:
            return False
        try:
            entry = self.doc.layers.get(layer)
        except Exception:
            return True
        if entry is None:
            return True
        if entry.is_off():
            return False
        return True

    @staticmethod
    def _origin(entity: DXFGraphic, insert_stack: Sequence[str]) -> str:
        handle = entity.dxf.get("handle", "")
        if insert_stack:
            return "INSERT {} / {} {}".format(
                ",".join(insert_stack), entity.dxftype(), handle
            )
        return "{} {}".format(entity.dxftype(), handle)


def _round_scale(value) -> float:
    try:
        return round(float(value), 12)
    except (TypeError, ValueError):
        return 1.0
