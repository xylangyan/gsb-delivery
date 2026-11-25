# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""2D clipping support for the cut usage engine.

The engine clips dash intervals of a support line instead of complete entities.
Each dash is a straight segment in the plane of the target space. A clipping
window is a convex polygon, a rectangle or the complement of a polygon as used by
inverted XCLIP boundaries.

All line clipping keeps the parametric position of the original line, therefore a
clipped dash still knows its exact distance along the unclipped support line.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ezdxf.math import Vec2

__all__ = ["Window", "LineWindow", "make_window", "triangulate_polygon"]

EPS = 1e-9


def _cross(a: Vec2, b: Vec2) -> float:
    return a.x * b.y - a.y * b.x


def _signed_area(vertices: Sequence[Vec2]) -> float:
    area = 0.0
    count = len(vertices)
    for index in range(count):
        a = vertices[index]
        b = vertices[(index + 1) % count]
        area += a.x * b.y - b.x * a.y
    return area * 0.5


class LineWindow:
    """A convex 2D window used to clip straight segments.

    The window is a list of vertices in counter-clockwise order. A point is inside
    when it is on the left side of every edge.
    """

    __slots__ = ("vertices", "_edges", "_bbox")

    def __init__(self, vertices: Iterable[Vec2]) -> None:
        points = list(vertices)
        if len(points) > 1 and points[0].isclose(points[-1]):
            points.pop()
        if len(points) < 3:
            raise ValueError("at least 3 vertices required")
        if _signed_area(points) < 0:
            points.reverse()
        self.vertices: list[Vec2] = points
        self._edges = [
            (points[index], points[(index + 1) % len(points)])
            for index in range(len(points))
        ]
        xs = [point.x for point in points]
        ys = [point.y for point in points]
        self._bbox = (min(xs), min(ys), max(xs), max(ys))

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self._bbox

    def contains(self, point: Vec2, tol: float = EPS) -> bool:
        x0, y0, x1, y1 = self._bbox
        if not (x0 - tol <= point.x <= x1 + tol and y0 - tol <= point.y <= y1 + tol):
            return False
        for start, end in self._edges:
            if _cross(end - start, point - start) < -tol * max(
                1.0, (end - start).magnitude
            ):
                return False
        return True

    def clip_line_params(self, start: Vec2, end: Vec2) -> list[tuple[float, float]]:
        """Clips the segment ``start`` to ``end`` and returns the visible parameter
        intervals as ``(lo, hi)`` tuples in the range ``[0, 1]``.
        """
        direction = end - start
        if direction.magnitude < EPS:
            if self.contains(start):
                return [(0.0, 0.0)]
            return []
        lo = 0.0
        hi = 1.0
        for edge_start, edge_end in self._edges:
            edge = edge_end - edge_start
            offset = start - edge_start
            denominator = _cross(direction, edge)
            numerator = _cross(edge, offset)
            if abs(denominator) < 1e-12:
                if numerator < -EPS:
                    return []
                continue
            t = numerator / denominator
            if denominator < 0:
                lo = max(lo, t)
            else:
                hi = min(hi, t)
            if lo > hi:
                return []
        if hi > lo:
            return [(max(0.0, lo), min(1.0, hi))]
        return []


def make_window(vertices: Iterable[Vec2]) -> LineWindow:
    """Builds a convex window from the given vertices."""
    return LineWindow(vertices)


def triangulate_polygon(vertices: Iterable[Vec2]) -> list[list[Vec2]]:
    """Splits a simple polygon into counter-clockwise triangles.

    The triangles are the convex parts of a window. A convex polygon returns its
    own vertices as one part.
    """
    points = list(vertices)
    if len(points) > 1 and points[0].isclose(points[-1]):
        points.pop()
    if len(points) < 3:
        return []
    if len(points) == 3:
        return [points if _signed_area(points) > 0 else points[::-1]]
    if _is_convex(points):
        return [points if _signed_area(points) > 0 else points[::-1]]
    from ezdxf.math.triangulation import mapbox_earcut_2d

    result: list[list[Vec2]] = []
    for triangle in mapbox_earcut_2d(points):
        part = [Vec2(vertex) for vertex in triangle]
        if len(part) != 3:
            continue
        if _signed_area(part) < 0:
            part.reverse()
        if abs(_signed_area(part)) > EPS:
            result.append(part)
    return result


def _is_convex(vertices: Sequence[Vec2]) -> bool:
    sign = 0
    count = len(vertices)
    for index in range(count):
        a = vertices[index]
        b = vertices[(index + 1) % count]
        c = vertices[(index + 2) % count]
        cross = _cross(b - a, c - b)
        if abs(cross) <= EPS:
            continue
        current = 1 if cross > 0 else -1
        if sign and current != sign:
            return False
        sign = current
    return True


@dataclass
class Window:
    """A clipping window in a target coordinate system.

    Attributes:
        name: short human readable name of the window
        polygons: convex parts of the window, a point is visible when it is inside
            any part
        inverted: set for inverted windows, visibility is then outside of all parts
        bounds: an outer rectangle for inverted windows which limits the visible
            area
    """

    name: str
    polygons: list[list[Vec2]]
    inverted: bool = False
    bounds: tuple[float, float, float, float] | None = None

    def clip_line_params(self, start: Vec2, end: Vec2) -> list[tuple[float, float]]:
        """Returns the visible parameter intervals of the segment ``start`` to
        ``end`` in the range ``[0, 1]``.
        """
        direction = end - start
        if direction.magnitude < EPS:
            return [(0.0, 0.0)] if self.contains(start) else []
        parts = _merge_interval_lists(
            [
                LineWindow(polygon).clip_line_params(start, end)
                for polygon in self.polygons
            ]
        )
        if self.inverted:
            return _invert_intervals(parts, self.bounds, start, end)
        return parts

    def contains(self, point: Vec2) -> bool:
        inside = any(_point_in_polygon(point, polygon) for polygon in self.polygons)
        if not self.inverted:
            return inside
        x0, y0, x1, y1 = self.bounds or (-1e300, -1e300, 1e300, 1e300)
        if not (x0 <= point.x <= x1 and y0 <= point.y <= y1):
            return False
        return not inside

    def transform(self, matrix) -> "Window":
        """Returns a new window with all vertices transformed by ``matrix``."""
        polygons = [
            [Vec2(matrix.transform(vertex)) for vertex in polygon]
            for polygon in self.polygons
        ]
        bounds = self.bounds
        if bounds is not None:
            corners = [
                Vec2(matrix.transform(vertex))
                for vertex in (
                    Vec2(bounds[0], bounds[1]),
                    Vec2(bounds[2], bounds[1]),
                    Vec2(bounds[2], bounds[3]),
                    Vec2(bounds[0], bounds[3]),
                )
            ]
            xs = [vertex.x for vertex in corners]
            ys = [vertex.y for vertex in corners]
            bounds = (min(xs), min(ys), max(xs), max(ys))
        return Window(self.name, polygons, self.inverted, bounds)


def _point_in_polygon(point: Vec2, polygon: Sequence[Vec2]) -> bool:
    inside = False
    count = len(polygon)
    for index in range(count):
        a = polygon[index]
        b = polygon[(index + 1) % count]
        if (a.y > point.y) != (b.y > point.y):
            x = (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x
            if point.x < x:
                inside = not inside
    return inside


def _merge_interval_lists(
    groups: Sequence[list[tuple[float, float]]],
) -> list[tuple[float, float]]:
    intervals = sorted(interval for group in groups for interval in group)
    return merge_intervals(intervals)


def merge_intervals(
    intervals: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Merges overlapping or touching intervals."""
    result: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if result and lo <= result[-1][1] + EPS:
            result[-1] = (result[-1][0], max(result[-1][1], hi))
        else:
            result.append((lo, hi))
    return result


def _invert_intervals(
    intervals: Sequence[tuple[float, float]],
    bounds: tuple[float, float, float, float] | None,
    start: Vec2,
    end: Vec2,
) -> list[tuple[float, float]]:
    """Inverts the visible parameter intervals inside an outer rectangle."""
    if bounds is None:
        outer = [(0.0, 1.0)]
    else:
        x0, y0, x1, y1 = bounds
        outer = LineWindow(
            [Vec2(x0, y0), Vec2(x1, y0), Vec2(x1, y1), Vec2(x0, y1)]
        ).clip_line_params(start, end)
    visible: list[tuple[float, float]] = []
    for lo, hi in outer:
        cursor = lo
        for c_lo, c_hi in intervals:
            if c_hi <= cursor:
                continue
            if c_lo >= hi:
                break
            if c_lo > cursor:
                visible.append((cursor, min(c_lo, hi)))
            cursor = max(cursor, c_hi)
            if cursor >= hi:
                break
        if cursor < hi:
            visible.append((cursor, hi))
    return merge_intervals(visible)
