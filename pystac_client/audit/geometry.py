"""Pure-Python geometry helpers for sealed-archive audits.

The audit only has to answer one spatial question: does an Item geometry
intersect a lon/lat rectangle? Boundary contact counts as an intersection and
rectangles may cross the antimeridian, so the helpers below implement those
semantics directly instead of taking on a third-party geometry dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

BBox = tuple[float, float, float, float]
Point = tuple[float, float]
Ring = Sequence[Sequence[float]]


def normalize_bbox(bbox: Any) -> BBox:
    """Return ``(minlon, minlat, maxlon, maxlat)`` as floats.

    A rectangle whose maximum longitude is smaller than its minimum crosses
    the antimeridian and is kept as-is.
    """
    if isinstance(bbox, str):
        values = tuple(float(part) for part in bbox.split(","))
    else:
        values = tuple(float(part) for part in bbox)
    if len(values) != 4:
        raise ValueError(
            "bbox must have four values: min longitude, min latitude, "
            f"max longitude, max latitude (got {values!r})"
        )
    minlon, minlat, maxlon, maxlat = values
    if minlat > maxlat:
        raise ValueError("bbox minimum latitude must not exceed maximum latitude")
    return minlon, minlat, maxlon, maxlat


def longitude_spans(bbox: BBox) -> tuple[tuple[float, float], ...]:
    """Return the disjoint longitude intervals covered by a rectangle."""
    minlon, _minlat, maxlon, _maxlat = bbox
    if maxlon >= minlon:
        return ((minlon, maxlon),)
    return ((minlon, 180.0), (-180.0, maxlon))


def point_in_bbox(point: Sequence[float], bbox: BBox) -> bool:
    """Closed-interval point-in-rectangle test, antimeridian aware."""
    lon, lat = float(point[0]), float(point[1])
    _minlon, minlat, _maxlon, maxlat = bbox
    if not (minlat <= lat <= maxlat):
        return False
    return any(
        minlon <= lon <= maxlon for minlon, maxlon in longitude_spans(bbox)
    )


def _orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, point: Point) -> bool:
    return (
        min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])
    )


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Closed segment intersection: touching or overlapping counts."""
    o1 = _orientation(c, d, a)
    o2 = _orientation(c, d, b)
    o3 = _orientation(a, b, c)
    o4 = _orientation(a, b, d)
    if o1 == 0 and _on_segment(c, d, a):
        return True
    if o2 == 0 and _on_segment(c, d, b):
        return True
    if o3 == 0 and _on_segment(a, b, c):
        return True
    if o4 == 0 and _on_segment(a, b, d):
        return True
    return (o1 > 0 > o2 or o1 < 0 < o2) and (o3 > 0 > o4 or o3 < 0 < o4)


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Closed point-in-polygon-ring test via ray casting."""
    coords = [(float(x), float(y)) for x, y in ring]
    for index in range(len(coords) - 1):
        if segments_intersect(
            coords[index], coords[index + 1], point, point
        ) and _on_segment(coords[index], coords[index + 1], point):
            return True
    inside = False
    x, y = point
    for index in range(len(coords) - 1):
        x1, y1 = coords[index]
        x2, y2 = coords[index + 1]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing == x:
                return True
            if crossing > x:
                inside = not inside
    return inside


def point_in_polygon(
    point: Point, exterior: Ring, holes: Iterable[Ring] = ()
) -> bool:
    if not point_in_ring(point, exterior):
        return False
    return not any(point_in_ring(point, hole) for hole in holes)


def _ring_bbox(ring: Ring) -> BBox:
    lons = [float(point[0]) for point in ring]
    lats = [float(point[1]) for point in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def _bboxes_overlap(first: BBox, second: BBox) -> bool:
    if not (
        first[1] <= second[3] and second[1] <= first[3]
    ):  # latitude overlap
        return False
    return any(
        span_a[0] <= span_b[1] and span_b[0] <= span_a[1]
        for span_a in longitude_spans(first)
        for span_b in longitude_spans(second)
    )


def _ring_intersects_bbox(ring: Ring, bbox: BBox) -> bool:
    """Fast reject / accept for a ring treated as a hole-free boundary."""
    if not _bboxes_overlap(_ring_bbox(ring), bbox):
        return False
    coords = [(float(x), float(y)) for x, y in ring]
    for point in coords[:-1]:
        if point_in_bbox(point, bbox):
            return True
    _minlon, minlat, _maxlon, maxlat = bbox
    rect_edges: list[tuple[Point, Point]] = []
    for minlon, maxlon in longitude_spans(bbox):
        corners = [
            (minlon, minlat),
            (maxlon, minlat),
            (maxlon, maxlat),
            (minlon, maxlat),
        ]
        rect_edges.extend(
            (corners[index], corners[(index + 1) % 4]) for index in range(4)
        )
    for index in range(len(coords) - 1):
        start, end = coords[index], coords[index + 1]
        if any(
            segments_intersect(start, end, rect_edge[0], rect_edge[1])
            for rect_edge in rect_edges
        ):
            return True
    corners = [point for edge in rect_edges for point in edge[:1]]
    return any(point_in_ring(corner, ring) for corner in corners)


def polygon_intersects_bbox(
    exterior: Ring, holes: Iterable[Ring], bbox: BBox
) -> bool:
    if not _ring_intersects_bbox(exterior, bbox):
        return False
    hole_rings = list(holes)
    if not hole_rings:
        return True
    # The outer ring reaches the rectangle, but the overlap could be entirely
    # swallowed by a hole. Any candidate contact point inside the polygon
    # (exterior minus holes) proves a real intersection.
    candidates: list[Point] = [
        (float(x), float(y)) for x, y in exterior[:-1] if point_in_bbox((x, y), bbox)
    ]
    _minlon, minlat, _maxlon, maxlat = bbox
    for minlon, maxlon in longitude_spans(bbox):
        candidates.extend(
            [
                (minlon, minlat),
                (maxlon, minlat),
                (maxlon, maxlat),
                (minlon, maxlat),
            ]
        )
    coords = [(float(x), float(y)) for x, y in exterior]
    rect_edges: list[tuple[Point, Point]] = []
    for minlon, maxlon in longitude_spans(bbox):
        corners = [
            (minlon, minlat),
            (maxlon, minlat),
            (maxlon, maxlat),
            (minlon, maxlat),
        ]
        rect_edges.extend(
            (corners[index], corners[(index + 1) % 4]) for index in range(4)
        )
    for index in range(len(coords) - 1):
        start, end = coords[index], coords[index + 1]
        for rect_start, rect_end in rect_edges:
            crossing = _segment_crossing(start, end, rect_start, rect_end)
            if crossing is not None:
                candidates.append(crossing)
    return any(
        point_in_polygon(point, exterior, hole_rings) for point in candidates
    )


def _segment_crossing(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    if not segments_intersect(a, b, c, d):
        return None
    o1 = _orientation(c, d, a)
    o2 = _orientation(c, d, b)
    if o1 == 0 and _on_segment(c, d, a):
        return a
    if o2 == 0 and _on_segment(c, d, b):
        return b
    o3 = _orientation(a, b, c)
    if o3 == 0 and _on_segment(a, b, c):
        return c
    o4 = _orientation(a, b, d)
    if o4 == 0 and _on_segment(a, b, d):
        return d
    denominator = (b[0] - a[0]) * (d[1] - c[1]) - (b[1] - a[1]) * (d[0] - c[0])
    if denominator == 0:
        return None
    t = (
        (a[1] - c[1]) * (d[0] - c[0]) - (a[0] - c[0]) * (d[1] - c[1])
    ) / denominator
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def geometry_intersects_bbox(geometry: Any, bbox: BBox) -> bool:
    """Does any part of a GeoJSON geometry touch or overlap a rectangle?"""
    if geometry is None:
        return False
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if geometry_type == "Point":
        return point_in_bbox(coordinates, bbox)
    if geometry_type == "MultiPoint":
        return any(point_in_bbox(point, bbox) for point in coordinates)
    if geometry_type == "LineString":
        return _line_intersects_bbox(coordinates, bbox)
    if geometry_type == "MultiLineString":
        return any(_line_intersects_bbox(line, bbox) for line in coordinates)
    if geometry_type == "Polygon":
        exterior = coordinates[0]
        holes = coordinates[1:]
        return polygon_intersects_bbox(exterior, holes, bbox)
    if geometry_type == "MultiPolygon":
        return any(
            polygon_intersects_bbox(polygon[0], polygon[1:], bbox)
            for polygon in coordinates
        )
    if geometry_type == "GeometryCollection":
        return any(
            geometry_intersects_bbox(member, bbox)
            for member in geometry.get("geometries", [])
        )
    raise ValueError(f"unsupported GeoJSON geometry type: {geometry_type!r}")


def _line_intersects_bbox(line: Ring, bbox: BBox) -> bool:
    points = [(float(x), float(y)) for x, y in line]
    if any(point_in_bbox(point, bbox) for point in points):
        return True
    _minlon, minlat, _maxlon, maxlat = bbox
    edges: list[tuple[Point, Point]] = []
    for minlon, maxlon in longitude_spans(bbox):
        corners = [
            (minlon, minlat),
            (maxlon, minlat),
            (maxlon, maxlat),
            (minlon, maxlat),
        ]
        edges.extend(
            (corners[index], corners[(index + 1) % 4]) for index in range(4)
        )
    for index in range(len(points) - 1):
        start, end = points[index], points[index + 1]
        if any(
            segments_intersect(start, end, edge[0], edge[1]) for edge in edges
        ):
            return True
    return False


def geometry_bbox(geometry: Any) -> BBox:
    """Bounding box of a GeoJSON geometry (lon/lat, no antimeridian split)."""
    lons: list[float] = []
    lats: list[float] = []

    def walk(coordinates: Any) -> None:
        if (
            isinstance(coordinates, (list, tuple))
            and coordinates
            and isinstance(coordinates[0], (int, float))
        ):
            lons.append(float(coordinates[0]))
            lats.append(float(coordinates[1]))
            return
        for member in coordinates:
            walk(member)

    if geometry.get("type") == "GeometryCollection":
        for member in geometry.get("geometries", []):
            minlon, minlat, maxlon, maxlat = geometry_bbox(member)
            lons.extend([minlon, maxlon])
            lats.extend([minlat, maxlat])
    else:
        walk(geometry.get("coordinates"))
    if not lons:
        raise ValueError("geometry has no coordinates")
    return (min(lons), min(lats), max(lons), max(lats))
