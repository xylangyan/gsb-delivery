# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Paperspace viewport windows for the cut usage engine.

A paperspace viewport shows a part of modelspace through a rectangular or a
custom clipping path. The cut usage engine clips the modelspace source paths with
this window and maps the visible parts into paperspace coordinates.

Only top view viewports are supported. A viewport with a twisted view or a non
top view direction cannot be represented as a 2D paper window.
"""
from __future__ import annotations

from typing import Optional

from ezdxf.math import Vec2
from ezdxf.path import make_path

from .clipping import Window, triangulate_polygon

__all__ = ["viewport_window", "viewport_matrix", "is_supported_viewport"]

FLATTENING_DISTANCE = 1e-3


def is_supported_viewport(viewport) -> bool:
    """Returns ``True`` for a top view viewport without a view twist."""
    if not viewport.is_top_view:
        return False
    if abs(float(viewport.dxf.get("view_twist_angle", 0.0))) > 1e-9:
        return False
    return viewport.get_scale() > 0.0


def viewport_matrix(viewport):
    """Returns the matrix from modelspace coordinates into paperspace coordinates."""
    return viewport.get_transformation_matrix()


def viewport_window(viewport) -> Optional[Window]:
    """Returns the clipping window of a viewport in paperspace coordinates.

    The window is expressed in paperspace coordinates, so callers transform the
    modelspace source paths with :func:`viewport_matrix` before the strokes are
    computed.
    """
    if not is_supported_viewport(viewport):
        return None
    path = make_path(viewport)
    if len(path) == 0:
        return None
    vertices = list(viewport.clipping_rect_corners())
    if viewport.has_extended_clipping_path:
        try:
            vertices = [
                Vec2(vertex)
                for vertex in path.flattening(FLATTENING_DISTANCE, segments=16)
            ]
        except (TypeError, ValueError):
            vertices = list(viewport.clipping_rect_corners())
    if len(vertices) < 3:
        return None
    return Window(
        f"VIEWPORT {viewport.dxf.get('handle', '')}",
        triangulate_polygon(vertices),
    )
