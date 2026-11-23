"""Incremental, stable-id visibility routing for survey corrections.

The classic :class:`pyvisgraph.vis_graph.VisGraph` API is unchanged.  This
module adds a publishing layer for a survey corrected in batches:

* polygons, vertices and stations have stable ids;
* each correction is applied atomically;
* the first version builds an index of the visibility pairs that each fence's
  bounding region can affect;
* later corrections re-evaluate only pairs related to the changed fences or
  nodes, plus station pairs whose route can have changed;
* each batch returns added/removed visibility edges and route changes, and the
  delta can be replayed onto the previous publication.

A segment is visible when its open interior does not pass through an obstacle
polygon.  Touching a polygon vertex or following a polygon boundary is allowed.
Shortest routes minimise Euclidean length.  Equal-length routes are ordered by
the sequence of stable vertex ids, lexicographically first.
"""
from __future__ import division

import pickle
from heapq import heappop, heappush
from math import sqrt


EPS = 1e-9
GEOM_EPS = 1e-9

VERTEX = 'vertex'
STATION = 'station'
_INF = float('inf')


class SurveyError(ValueError):
    """Raised when a survey definition or correction is inconsistent."""


def _stable_key(value):
    """Total ordering key for stable ids (numbers first, then strings)."""
    if isinstance(value, bool):
        return (2, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (3, repr(value))


def _node_order(node):
    kind, stable_id = node
    return (_stable_key(stable_id), 0 if kind == VERTEX else 1)


def _canonical_nodes(a, b):
    if _node_order(a) <= _node_order(b):
        return (a, b)
    return (b, a)


def _close(a, b):
    if a == b:
        return True
    return abs(a - b) <= EPS * max(1.0, abs(a), abs(b))


def _coord(value):
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    raise SurveyError(
        "coordinate must be a pair of numbers: {!r}".format(value))


def _distance(p1, p2):
    return sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


def _get(item, *names):
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return None


class Vertex(object):
    """A polygon vertex with a stable survey id."""

    __slots__ = ('vertex_id', 'x', 'y')

    def __init__(self, vertex_id, x=None, y=None):
        if y is None:
            x, y = _coord(x)
        self.vertex_id = vertex_id
        self.x = float(x)
        self.y = float(y)

    @property
    def id(self):
        return self.vertex_id

    @property
    def coord(self):
        return (self.x, self.y)

    def __eq__(self, other):
        return (isinstance(other, Vertex) and
                self.vertex_id == other.vertex_id and
                self.x == other.x and self.y == other.y)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.vertex_id, self.x, self.y))

    def __repr__(self):
        return "Vertex({!r}, {!r}, {!r})".format(
            self.vertex_id, self.x, self.y)


class Station(object):
    """A route endpoint with a stable survey id."""

    __slots__ = ('station_id', 'x', 'y')

    def __init__(self, station_id, x=None, y=None):
        if y is None:
            x, y = _coord(x)
        self.station_id = station_id
        self.x = float(x)
        self.y = float(y)

    @property
    def id(self):
        return self.station_id

    @property
    def coord(self):
        return (self.x, self.y)

    def __eq__(self, other):
        return (isinstance(other, Station) and
                self.station_id == other.station_id and
                self.x == other.x and self.y == other.y)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.station_id, self.x, self.y))

    def __repr__(self):
        return "Station({!r}, {!r}, {!r})".format(
            self.station_id, self.x, self.y)


SurveyVertex = Vertex
SurveyStation = Station


class PolygonDef(object):
    """A stable polygon id plus its ordered vertices.

    Vertex items may be :class:`Vertex` objects, dictionaries, or
    ``(vertex_id, x, y)`` / ``(vertex_id, (x, y))`` tuples.
    """

    __slots__ = ('polygon_id', 'vertices')

    def __init__(self, polygon_id, vertices):
        self.polygon_id = polygon_id
        self.vertices = list(vertices)

    @property
    def id(self):
        return self.polygon_id

    def __repr__(self):
        return "PolygonDef({!r}, {!r})".format(
            self.polygon_id, self.vertices)


Polygon = PolygonDef


class SurveyCorrection(object):
    """One atomic batch of survey edits.

    ``polygons_added`` / ``polygons_updated`` accept polygon definitions and
    ``polygons_removed`` accepts polygon ids.  ``vertices_updated`` accepts a
    mapping ``vertex_id -> (x, y)`` or vertex items.  Stations may be added,
    removed or moved in the same batch.  A vertex id may be shared by several
    polygons; moving it moves every polygon that references it.
    """

    def __init__(self, polygons_added=None, polygons_removed=None,
                 polygons_updated=None, vertices_updated=None,
                 stations_added=None, stations_removed=None,
                 stations_updated=None, directed=None, metadata=None):
        self.polygons_added = list(polygons_added or [])
        self.polygons_removed = list(polygons_removed or [])
        self.polygons_updated = list(polygons_updated or [])
        self.vertices_updated = vertices_updated or {}
        self.stations_added = list(stations_added or [])
        self.stations_removed = list(stations_removed or [])
        self.stations_updated = stations_updated or {}
        self.directed = directed
        self.metadata = metadata

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SurveyError(
                "correction must be a mapping or SurveyCorrection")

        def pick(*names):
            for name in names:
                if name in data:
                    return data[name]
            return None

        return cls(polygons_added=pick('polygons_added', 'added_polygons'),
                   polygons_removed=pick('polygons_removed',
                                         'removed_polygons'),
                   polygons_updated=pick('polygons_updated',
                                         'updated_polygons'),
                   vertices_updated=pick('vertices_updated',
                                         'vertex_updates'),
                   stations_added=pick('stations_added', 'added_stations'),
                   stations_removed=pick('stations_removed',
                                         'removed_stations'),
                   stations_updated=pick('stations_updated',
                                         'station_updates'),
                   directed=data.get('directed'),
                   metadata=data.get('metadata'))

    def __repr__(self):
        return ("SurveyCorrection(polygons_added={!r}, polygons_removed={!r}, "
                "polygons_updated={!r}, vertices_updated={!r}, "
                "stations_added={!r}, stations_removed={!r}, "
                "stations_updated={!r})").format(
                    self.polygons_added, self.polygons_removed,
                    self.polygons_updated, self.vertices_updated,
                    self.stations_added, self.stations_removed,
                    self.stations_updated)


class EdgeChange(object):
    """One visibility edge, represented by stable survey ids."""

    __slots__ = ('node1', 'node2', 'x1', 'y1', 'x2', 'y2')

    def __init__(self, node1, node2, coord1, coord2):
        if _node_order(node1) <= _node_order(node2):
            self.node1, self.node2 = node1, node2
            self.x1, self.y1 = coord1
            self.x2, self.y2 = coord2
        else:
            self.node1, self.node2 = node2, node1
            self.x1, self.y1 = coord2
            self.x2, self.y2 = coord1

    @property
    def kind1(self):
        return self.node1[0]

    @property
    def kind2(self):
        return self.node2[0]

    @property
    def p1(self):
        return self.node1[1]

    @property
    def p2(self):
        return self.node2[1]

    @property
    def key(self):
        return (self.node1, self.node2)

    @property
    def coord1(self):
        return (self.x1, self.y1)

    @property
    def coord2(self):
        return (self.x2, self.y2)

    def to_dict(self):
        return {'node1': [self.kind1, self.p1],
                'node2': [self.kind2, self.p2],
                'coord1': list(self.coord1),
                'coord2': list(self.coord2)}

    def __eq__(self, other):
        return isinstance(other, EdgeChange) and self.key == other.key

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return "EdgeChange({!r}, {!r})".format(
            (self.kind1, self.p1), (self.kind2, self.p2))


class Route(object):
    """A shortest station route in a published revision."""

    __slots__ = ('origin', 'destination', 'path', 'nodes', 'length')

    def __init__(self, origin, destination, nodes, length):
        self.origin = origin
        self.destination = destination
        self.nodes = tuple(nodes)
        self.path = tuple(node[1] for node in nodes)
        self.length = length

    @property
    def station_pair(self):
        return (self.origin, self.destination)

    def to_dict(self):
        return {'origin': self.origin,
                'destination': self.destination,
                'path': list(self.path),
                'nodes': [list(node) for node in self.nodes],
                'length': self.length}

    def __eq__(self, other):
        return (isinstance(other, Route) and
                self.origin == other.origin and
                self.destination == other.destination and
                self.nodes == other.nodes and
                _same_length(self.length, other.length))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "Route({!r}, {!r}, path={!r}, length={!r})".format(
            self.origin, self.destination, list(self.path), self.length)


class RouteChange(object):
    """A station pair whose path or length differs from the old revision."""

    __slots__ = ('origin', 'destination', 'old_path', 'new_path',
                 'old_nodes', 'new_nodes', 'old_length', 'new_length')

    def __init__(self, origin, destination, old_route=None, new_route=None):
        self.origin = origin
        self.destination = destination
        self.old_path = None if old_route is None else list(old_route.path)
        self.new_path = None if new_route is None else list(new_route.path)
        self.old_nodes = None if old_route is None else list(old_route.nodes)
        self.new_nodes = None if new_route is None else list(new_route.nodes)
        self.old_length = None if old_route is None else old_route.length
        self.new_length = None if new_route is None else new_route.length

    @property
    def station_pair(self):
        return (self.origin, self.destination)

    @property
    def kind(self):
        if self.old_path is None:
            return 'added'
        if self.new_path is None:
            return 'removed'
        if self.old_path == self.new_path:
            return 'length'
        return 'path'

    @property
    def changed(self):
        return (self.old_path != self.new_path or
                not _same_length(self.old_length, self.new_length))

    @property
    def became_reachable(self):
        return self.old_path is None and self.new_path is not None

    @property
    def became_unreachable(self):
        return self.old_path is not None and self.new_path is None

    def new_route(self):
        if self.new_path is None:
            return None
        return Route(self.origin, self.destination, self.new_nodes,
                     self.new_length)

    def old_route(self):
        if self.old_path is None:
            return None
        return Route(self.origin, self.destination, self.old_nodes,
                     self.old_length)

    def to_dict(self):
        return {'origin': self.origin,
                'destination': self.destination,
                'kind': self.kind,
                'old_path': self.old_path,
                'new_path': self.new_path,
                'old_length': self.old_length,
                'new_length': self.new_length}

    def __repr__(self):
        return ("RouteChange({!r}, {!r}, old_path={!r}, new_path={!r}, "
                "old_length={!r}, new_length={!r})").format(
                    self.origin, self.destination, self.old_path,
                    self.new_path, self.old_length, self.new_length)


def _same_length(a, b):
    if a is None or b is None:
        return a is b
    return _close(a, b)


def _route_changed(old_route, new_route):
    if old_route is None or new_route is None:
        return old_route is not new_route
    return (old_route.path != new_route.path or
            not _same_length(old_route.length, new_route.length))


class BatchReport(object):
    """The publishable delta for one correction batch.

    ``added_edges`` and ``removed_edges`` are the complete visibility delta,
    including station-incident edges.  ``vertex_edges_added`` /
    ``vertex_edges_removed`` and ``station_edges_added`` /
    ``station_edges_removed`` expose the two subsets.
    """

    __slots__ = ('base_version', 'version', 'added_edges', 'removed_edges',
                 'route_changes', 'directed', 'metadata',
                 'station_edges_added', 'station_edges_removed',
                 'reevaluated_pairs', 'recomputed_routes',
                 'recomputed_origins')

    def __init__(self, base_version, version, added_edges, removed_edges,
                 route_changes, directed=False, metadata=None,
                 station_edges_added=None, station_edges_removed=None,
                 reevaluated_pairs=0, recomputed_routes=0,
                 recomputed_origins=None):
        self.base_version = base_version
        self.version = version
        self.added_edges = list(added_edges)
        self.removed_edges = list(removed_edges)
        self.route_changes = list(route_changes)
        self.directed = directed
        self.metadata = metadata
        self.station_edges_added = list(station_edges_added or [])
        self.station_edges_removed = list(station_edges_removed or [])
        self.reevaluated_pairs = reevaluated_pairs
        self.recomputed_routes = recomputed_routes
        self.recomputed_origins = list(recomputed_origins or [])

    @staticmethod
    def _split(changes):
        vertex = []
        station = []
        for edge in changes:
            if edge.kind1 == STATION or edge.kind2 == STATION:
                station.append(edge)
            else:
                vertex.append(edge)
        return vertex, station

    @property
    def changed_route_pairs(self):
        return [change.station_pair for change in self.route_changes]

    @property
    def added_edge_keys(self):
        return set(edge.key for edge in self.added_edges)

    @property
    def removed_edge_keys(self):
        return set(edge.key for edge in self.removed_edges)

    @property
    def added_station_edge_keys(self):
        return set(edge.key for edge in self.station_edges_added)

    @property
    def removed_station_edge_keys(self):
        return set(edge.key for edge in self.station_edges_removed)

    @property
    def vertex_edges_added(self):
        return self._split(self.added_edges)[0]

    @property
    def vertex_edges_removed(self):
        return self._split(self.removed_edges)[0]

    @property
    def all_added_edges(self):
        return list(self.added_edges)

    @property
    def all_removed_edges(self):
        return list(self.removed_edges)

    def replay(self, routes):
        """Apply this delta to a route mapping from ``base_version``.

        The result has the same keys and :class:`Route` objects as a fresh
        full computation on the corrected survey.
        """
        result = dict(routes)
        for change in self.route_changes:
            pair = change.station_pair
            if change.new_path is None:
                result.pop(pair, None)
            else:
                result[pair] = change.new_route()
        return result

    apply_to_routes = replay

    def to_dict(self):
        return {'base_version': self.base_version,
                'version': self.version,
                'directed': self.directed,
                'metadata': self.metadata,
                'reevaluated_pairs': self.reevaluated_pairs,
                'recomputed_routes': self.recomputed_routes,
                'recomputed_origins': self.recomputed_origins,
                'added_edges': [edge.to_dict() for edge in self.added_edges],
                'removed_edges': [edge.to_dict()
                                  for edge in self.removed_edges],
                'vertex_edges_added': [edge.to_dict()
                                       for edge in self.vertex_edges_added],
                'vertex_edges_removed': [
                    edge.to_dict() for edge in self.vertex_edges_removed],
                'station_edges_added': [edge.to_dict()
                                        for edge in self.station_edges_added],
                'station_edges_removed': [
                    edge.to_dict() for edge in self.station_edges_removed],
                'route_changes': [change.to_dict()
                                  for change in self.route_changes]}

    def __repr__(self):
        return ("BatchReport(base_version={!r}, version={!r}, "
                "added_edges={!r}, removed_edges={!r}, "
                "route_changes={!r})").format(
                    self.base_version, self.version, len(self.added_edges),
                    len(self.removed_edges), len(self.route_changes))


# ---------------------------------------------------------------------------
# Geometry


def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _point_on_segment(px, py, ax, ay, bx, by):
    if (px < min(ax, bx) - GEOM_EPS or px > max(ax, bx) + GEOM_EPS or
            py < min(ay, by) - GEOM_EPS or py > max(ay, by) + GEOM_EPS):
        return False
    c = _cross(bx - ax, by - ay, px - ax, py - ay)
    scale = max(1.0, abs(bx - ax) + abs(by - ay))
    return abs(c) <= GEOM_EPS * scale


def _point_strict_inside(px, py, polygon):
    inside = False
    count = len(polygon)
    for i in range(count):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % count]
        if _point_on_segment(px, py, ax, ay, bx, by):
            return False
        if (ay > py) != (by > py):
            x_intersect = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < x_intersect:
                inside = not inside
    return inside


def _bbox(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bboxes_intersect(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or
                a[3] < b[1] or b[3] < a[1])


def _segment_bbox_intersects(p1, p2, bbox):
    return _bboxes_intersect(
        (min(p1[0], p2[0]), min(p1[1], p2[1]),
         max(p1[0], p2[0]), max(p1[1], p2[1])), bbox)


def _segment_blocked_by_polygon(p1, p2, polygon, bbox):
    """True if the open segment p1-p2 passes through the polygon interior."""
    if not _segment_bbox_intersects(p1, p2, bbox):
        return False

    px, py = p1
    dx = p2[0] - px
    dy = p2[1] - py
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return False

    ts = [0.0, 1.0]
    count = len(polygon)
    for i in range(count):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % count]
        ex = bx - ax
        ey = by - ay
        edge_length_sq = ex * ex + ey * ey
        den = _cross(dx, dy, ex, ey)
        wx = ax - px
        wy = ay - py
        threshold = 1e-12 * max(1.0, sqrt(length_sq * edge_length_sq))
        if abs(den) > threshold:
            t = _cross(wx, wy, ex, ey) / den
            u = _cross(wx, wy, dx, dy) / den
            if -GEOM_EPS <= t <= 1.0 + GEOM_EPS and \
                    -GEOM_EPS <= u <= 1.0 + GEOM_EPS:
                ts.append(min(1.0, max(0.0, t)))
        elif abs(_cross(wx, wy, dx, dy)) <= \
                GEOM_EPS * max(1.0, sqrt(length_sq)):
            ta = ((ax - px) * dx + (ay - py) * dy) / length_sq
            tb = ((bx - px) * dx + (by - py) * dy) / length_sq
            lo = max(0.0, min(ta, tb))
            hi = min(1.0, max(ta, tb))
            if lo <= hi + GEOM_EPS:
                ts.append(lo)
                ts.append(hi)

    ts.sort()
    unique = []
    for t in ts:
        if not unique or t - unique[-1] > 1e-10:
            unique.append(t)
    for i in range(len(unique) - 1):
        tm = (unique[i] + unique[i + 1]) / 2.0
        if _point_strict_inside(px + tm * dx, py + tm * dy, polygon):
            return True
    return False


def _segment_visible(p1, p2, obstacles):
    """Return (visible, ids of obstacles whose bbox the segment crosses)."""
    if p1 == p2:
        return False, []
    visible = True
    hits = []
    for polygon_id, polygon, bbox in obstacles:
        if _segment_bbox_intersects(p1, p2, bbox):
            hits.append(polygon_id)
            if visible and _segment_blocked_by_polygon(
                    p1, p2, polygon, bbox):
                visible = False
    return visible, hits


# ---------------------------------------------------------------------------
# Survey model


class _Model(object):
    __slots__ = ('polygons', 'vertices', 'stations', 'nodes', 'bboxes',
                 'coords', 'obstacles', 'edges', 'pair_index')

    def __init__(self, polygons, vertices, stations):
        self.polygons = polygons
        self.vertices = vertices
        self.stations = stations
        self.nodes = set((VERTEX, vid) for vid in vertices)
        self.nodes.update((STATION, sid) for sid in stations)
        self.bboxes = {}
        for polygon_id, vertex_ids in polygons.items():
            self.bboxes[polygon_id] = _bbox(
                [vertices[vid] for vid in vertex_ids])
        self.coords = {}
        for vid, coord in vertices.items():
            self.coords[(VERTEX, vid)] = coord
        for sid, coord in stations.items():
            self.coords[(STATION, sid)] = coord
        self.obstacles = [
            (pid, [vertices[vid] for vid in polygons[pid]], self.bboxes[pid])
            for pid in polygons]
        self.edges = set()
        self.pair_index = dict((pid, set()) for pid in polygons)


def _parse_polygon(item, index):
    if isinstance(item, PolygonDef):
        return item.polygon_id, item.vertices
    if isinstance(item, dict):
        polygon_id = item.get('polygon_id', item.get('id', index))
        vertices = item.get('vertices', item.get('points'))
        if vertices is None:
            raise SurveyError(
                "polygon {!r} has no vertices".format(polygon_id))
        return polygon_id, vertices
    if isinstance(item, (tuple, list)) and len(item) == 2 and \
            not isinstance(item[0], (int, float)):
        return item[0], item[1]
    if isinstance(item, (tuple, list)):
        return index, item
    raise SurveyError("unsupported polygon definition: {!r}".format(item))


def _auto_vertex_id(polygon_id, index):
    return "{}#{}".format(polygon_id, index)


def _parse_vertex(item, polygon_id, index):
    if isinstance(item, Vertex):
        return item.vertex_id, item.x, item.y
    if isinstance(item, dict):
        vertex_id = item.get(
            'vertex_id', item.get('id', _auto_vertex_id(polygon_id, index)))
        if 'x' in item and 'y' in item:
            return vertex_id, float(item['x']), float(item['y'])
        coord = item.get('coord', item.get('coordinate'))
        if coord is None:
            raise SurveyError(
                "vertex {!r} has no coordinate".format(vertex_id))
        x, y = _coord(coord)
        return vertex_id, x, y
    if isinstance(item, (tuple, list)) and len(item) == 3:
        return item[0], float(item[1]), float(item[2])
    if isinstance(item, (tuple, list)) and len(item) == 2:
        if isinstance(item[1], (tuple, list)):
            x, y = _coord(item[1])
            return item[0], x, y
        return _auto_vertex_id(polygon_id, index), float(item[0]), \
            float(item[1])
    if hasattr(item, 'x') and hasattr(item, 'y'):
        vertex_id = _get(item, 'vertex_id', 'stable_id', 'id')
        if vertex_id is None:
            vertex_id = _auto_vertex_id(polygon_id, index)
        return vertex_id, float(item.x), float(item.y)
    raise SurveyError("unsupported vertex definition: {!r}".format(item))


def _parse_station(item, index):
    if isinstance(item, Station):
        return item.station_id, item.x, item.y
    if isinstance(item, dict):
        station_id = item.get('station_id', item.get('id', index))
        if 'x' in item and 'y' in item:
            return station_id, float(item['x']), float(item['y'])
        coord = item.get('coord', item.get('coordinate'))
        if coord is None:
            raise SurveyError(
                "station {!r} has no coordinate".format(station_id))
        x, y = _coord(coord)
        return station_id, x, y
    if isinstance(item, (tuple, list)) and len(item) == 3:
        return item[0], float(item[1]), float(item[2])
    if isinstance(item, (tuple, list)) and len(item) == 2:
        if isinstance(item[1], (tuple, list)):
            x, y = _coord(item[1])
            return item[0], x, y
        return index, float(item[0]), float(item[1])
    if hasattr(item, 'x') and hasattr(item, 'y'):
        station_id = _get(item, 'station_id', 'stable_id', 'id')
        if station_id is None:
            station_id = index
        return station_id, float(item.x), float(item.y)
    raise SurveyError("unsupported station definition: {!r}".format(item))


def _parse_vertex_updates(updates):
    result = {}
    if not updates:
        return result
    if isinstance(updates, dict):
        for vertex_id, value in updates.items():
            if isinstance(value, Vertex):
                result[vertex_id] = (value.x, value.y)
            else:
                result[vertex_id] = _coord(value)
        return result
    for item in updates:
        if isinstance(item, Vertex):
            result[item.vertex_id] = (item.x, item.y)
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            result[item[0]] = (float(item[1]), float(item[2]))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            result[item[0]] = _coord(item[1])
        elif isinstance(item, dict):
            vertex_id = item.get('vertex_id', item.get('id'))
            if 'x' in item and 'y' in item:
                result[vertex_id] = (float(item['x']), float(item['y']))
            else:
                result[vertex_id] = _coord(
                    item.get('coord', item.get('coordinate')))
        else:
            raise SurveyError(
                "unsupported vertex update: {!r}".format(item))
    return result


def _parse_station_updates(updates):
    if isinstance(updates, dict):
        result = {}
        for station_id, value in updates.items():
            if isinstance(value, Station):
                result[station_id] = (value.x, value.y)
            else:
                result[station_id] = _coord(value)
        return result
    result = {}
    for item in (updates or []):
        station_id, x, y = _parse_station(item, len(result))
        result[station_id] = (x, y)
    return result


def _coerce_correction(correction):
    if isinstance(correction, SurveyCorrection):
        return correction
    if isinstance(correction, dict):
        return SurveyCorrection.from_dict(correction)
    raise SurveyError("correction must be a mapping or SurveyCorrection")


def _strip_closed_ring(vertex_ids):
    if len(vertex_ids) > 1 and vertex_ids[0] == vertex_ids[-1]:
        return vertex_ids[:-1]
    return vertex_ids


def _model_from_definitions(polygons, stations):
    parsed_polygons = {}
    vertices = {}
    for index, item in enumerate(polygons):
        polygon_id, vertex_items = _parse_polygon(item, index)
        if polygon_id in parsed_polygons:
            raise SurveyError(
                "duplicate polygon id: {!r}".format(polygon_id))
        vertex_ids = []
        for vertex_index, vertex_item in enumerate(vertex_items):
            vertex_id, x, y = _parse_vertex(vertex_item, polygon_id,
                                            vertex_index)
            if vertex_id in vertices and vertices[vertex_id] != (x, y):
                raise SurveyError(
                    "vertex {!r} has two coordinates: {!r} and {!r}".format(
                        vertex_id, vertices[vertex_id], (x, y)))
            vertices[vertex_id] = (x, y)
            vertex_ids.append(vertex_id)
        vertex_ids = _strip_closed_ring(vertex_ids)
        if len(vertex_ids) < 3:
            raise SurveyError(
                "polygon {!r} has fewer than 3 vertices".format(polygon_id))
        parsed_polygons[polygon_id] = tuple(vertex_ids)

    parsed_stations = {}
    for index, item in enumerate(stations or []):
        station_id, x, y = _parse_station(item, index)
        if station_id in parsed_stations:
            raise SurveyError(
                "duplicate station id: {!r}".format(station_id))
        parsed_stations[station_id] = (x, y)
    return _Model(parsed_polygons, vertices, parsed_stations)


class _CorrectionResult(object):
    __slots__ = ('model', 'changed_polygons', 'changed_nodes',
                 'added_stations', 'removed_stations')

    def __init__(self, model, changed_polygons, changed_nodes,
                 added_stations, removed_stations):
        self.model = model
        self.changed_polygons = changed_polygons
        self.changed_nodes = changed_nodes
        self.added_stations = added_stations
        self.removed_stations = removed_stations


def _apply_correction(polygons, vertices, stations, correction):
    """Apply a batch to copies; validate everything before committing."""
    new_polygons = dict(polygons)
    new_vertices = dict(vertices)
    new_stations = dict(stations)
    changed_polygons = set()
    changed_nodes = set()
    added_stations = set()
    removed_stations = set()
    moved = set()

    for polygon_id in correction.polygons_removed:
        if polygon_id not in new_polygons:
            raise SurveyError(
                "cannot remove unknown polygon {!r}".format(polygon_id))
        changed_polygons.add(polygon_id)
        for vertex_id in new_polygons[polygon_id]:
            changed_nodes.add((VERTEX, vertex_id))
        del new_polygons[polygon_id]

    updates = _parse_vertex_updates(correction.vertices_updated)
    for vertex_id, coord in updates.items():
        if vertex_id not in new_vertices:
            raise SurveyError(
                "cannot update unknown vertex {!r}".format(vertex_id))
        if new_vertices[vertex_id] != coord:
            moved.add(vertex_id)
            new_vertices[vertex_id] = coord
            changed_nodes.add((VERTEX, vertex_id))

    for item in correction.polygons_updated:
        polygon_id, vertex_items = _parse_polygon(item, len(new_polygons))
        if polygon_id not in new_polygons:
            raise SurveyError(
                "cannot update unknown polygon {!r}".format(polygon_id))
        changed_polygons.add(polygon_id)
        old_vertex_ids = new_polygons[polygon_id]
        new_vertex_ids = []
        for index, vertex_item in enumerate(vertex_items):
            vertex_id, x, y = _parse_vertex(vertex_item, polygon_id, index)
            _check_batch_vertex(vertex_id, (x, y), updates)
            new_vertex_ids.append(vertex_id)
            if new_vertices.get(vertex_id) != (x, y):
                if vertex_id in new_vertices:
                    moved.add(vertex_id)
                new_vertices[vertex_id] = (x, y)
        new_vertex_ids = _strip_closed_ring(new_vertex_ids)
        if len(new_vertex_ids) < 3:
            raise SurveyError(
                "polygon {!r} has fewer than 3 vertices".format(polygon_id))
        new_polygons[polygon_id] = tuple(new_vertex_ids)
        for vertex_id in set(old_vertex_ids) | set(new_vertex_ids):
            changed_nodes.add((VERTEX, vertex_id))

    for item in correction.polygons_added:
        polygon_id, vertex_items = _parse_polygon(item, len(new_polygons))
        if polygon_id in new_polygons:
            raise SurveyError(
                "cannot add existing polygon {!r}".format(polygon_id))
        new_vertex_ids = []
        for index, vertex_item in enumerate(vertex_items):
            vertex_id, x, y = _parse_vertex(vertex_item, polygon_id, index)
            _check_batch_vertex(vertex_id, (x, y), updates)
            new_vertex_ids.append(vertex_id)
            if new_vertices.get(vertex_id) != (x, y):
                if vertex_id in new_vertices:
                    moved.add(vertex_id)
                new_vertices[vertex_id] = (x, y)
        new_vertex_ids = _strip_closed_ring(new_vertex_ids)
        if len(new_vertex_ids) < 3:
            raise SurveyError(
                "polygon {!r} has fewer than 3 vertices".format(polygon_id))
        new_polygons[polygon_id] = tuple(new_vertex_ids)
        changed_polygons.add(polygon_id)
        for vertex_id in new_vertex_ids:
            changed_nodes.add((VERTEX, vertex_id))

    station_updates = _parse_station_updates(correction.stations_updated)
    for station_id, coord in station_updates.items():
        if station_id not in new_stations:
            raise SurveyError(
                "cannot update unknown station {!r}".format(station_id))
        if new_stations[station_id] != coord:
            new_stations[station_id] = coord
            changed_nodes.add((STATION, station_id))

    for station_id in correction.stations_removed:
        if station_id not in new_stations:
            raise SurveyError(
                "cannot remove unknown station {!r}".format(station_id))
        changed_nodes.add((STATION, station_id))
        removed_stations.add(station_id)
        del new_stations[station_id]

    for item in correction.stations_added:
        station_id, x, y = _parse_station(item, len(new_stations))
        if station_id in new_stations:
            raise SurveyError(
                "cannot add existing station {!r}".format(station_id))
        new_stations[station_id] = (x, y)
        changed_nodes.add((STATION, station_id))
        added_stations.add(station_id)

    referenced = set()
    for vertex_ids in new_polygons.values():
        referenced.update(vertex_ids)
    for vertex_id in list(new_vertices):
        if vertex_id not in referenced:
            del new_vertices[vertex_id]

    for vertex_id in moved:
        for polygon_id, vertex_ids in polygons.items():
            if vertex_id in vertex_ids:
                changed_polygons.add(polygon_id)
        for polygon_id, vertex_ids in new_polygons.items():
            if vertex_id in vertex_ids:
                changed_polygons.add(polygon_id)

    model = _Model(new_polygons, new_vertices, new_stations)
    return _CorrectionResult(model, changed_polygons, changed_nodes,
                             added_stations, removed_stations)


def _check_batch_vertex(vertex_id, coord, updates):
    if vertex_id in updates and updates[vertex_id] != coord:
        raise SurveyError(
            "vertex {!r} has conflicting coordinates in one batch".format(
                vertex_id))


# ---------------------------------------------------------------------------
# Visibility and routes


def _visible(model, a, b):
    return _segment_visible(model.coords[a], model.coords[b],
                            model.obstacles)[0]


def _all_visibility(model):
    """Compute all visible edges and the first-revision pair index.

    Vertices and stations are all graph nodes, matching how the classic
    VisGraph treats points added with ``update``.
    """
    nodes = sorted(model.nodes, key=_node_order)
    for i, a in enumerate(nodes):
        coord_a = model.coords[a]
        for b in nodes[i + 1:]:
            visible, hits = _segment_visible(
                coord_a, model.coords[b], model.obstacles)
            if visible:
                model.edges.add((a, b))
            for polygon_id in hits:
                model.pair_index[polygon_id].add((a, b))


def _pairs_for_bbox(model, bbox):
    nodes = sorted(model.nodes, key=_node_order)
    result = set()
    for i, a in enumerate(nodes):
        coord_a = model.coords[a]
        for b in nodes[i + 1:]:
            if _segment_bbox_intersects(coord_a, model.coords[b], bbox):
                result.add((a, b))
    return result


def _adjacency(edges):
    adjacency = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    return adjacency


def _shortest_paths(adjacency, coords, origin):
    """Lexicographic shortest paths from one node.

    Distance is primary.  Ties are resolved by the tuple of stable node ids
    along the path, making the result independent of insertion order.
    """
    best = {origin: (0.0, (origin,))}
    start_rank = (_node_order(origin),)
    heap = [(0.0, start_rank, origin, (origin,))]
    while heap:
        distance, _, node, path = heappop(heap)
        current = best.get(node)
        if current is None or current[1] != path or \
                not _close(current[0], distance):
            continue
        for neighbour in adjacency.get(node, ()):
            step = _distance(coords[node], coords[neighbour])
            candidate = distance + step
            new_path = path + (neighbour,)
            old = best.get(neighbour)
            if old is not None:
                if candidate > old[0] + EPS * max(
                        1.0, abs(candidate), abs(old[0])):
                    continue
                if _close(candidate, old[0]) and \
                        _path_rank(new_path) >= _path_rank(old[1]):
                    continue
            best[neighbour] = (candidate, new_path)
            heappush(heap, (candidate, _path_rank(new_path), neighbour,
                            new_path))
    return best


def _path_rank(path):
    return tuple(_node_order(node) for node in path)


def _station_pair_list(stations):
    ids = sorted(stations, key=_stable_key)
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pairs.append((a, b))
    return pairs


def _routes_for(model, pairs):
    adjacency = _adjacency(model.edges)
    coords = model.coords
    by_origin = {}
    for origin_id, destination_id in pairs:
        by_origin.setdefault(origin_id, []).append(destination_id)

    routes = {}
    for origin_id, destinations in by_origin.items():
        origin = (STATION, origin_id)
        paths = _shortest_paths(adjacency, coords, origin)
        for destination_id in destinations:
            destination = (STATION, destination_id)
            found = paths.get(destination)
            if found is None:
                continue
            routes[(origin_id, destination_id)] = Route(
                origin_id, destination_id, found[1], found[0])
    return routes


def _all_routes(model, directed):
    pairs = _station_pair_list(model.stations)
    if directed:
        pairs = [(a, b) for a, b in pairs] + [(b, a) for a, b in pairs]
    return _routes_for(model, pairs)


def _edge_changes(old_edges, new_edges, old_model, new_model):
    added = []
    removed = []
    for a, b in sorted(set(new_edges) - set(old_edges),
                       key=lambda edge: (_node_order(edge[0]),
                                         _node_order(edge[1]))):
        added.append(EdgeChange(a, b, new_model.coords[a],
                                new_model.coords[b]))
    for a, b in sorted(set(old_edges) - set(new_edges),
                       key=lambda edge: (_node_order(edge[0]),
                                         _node_order(edge[1]))):
        removed.append(EdgeChange(a, b, old_model.coords[a],
                                  old_model.coords[b]))
    return added, removed


def _route_uses_nodes(route, nodes):
    return any(node in nodes for node in route.nodes)


def _route_uses_edges(route, edges):
    for i in range(len(route.nodes) - 1):
        a, b = route.nodes[i], route.nodes[i + 1]
        if (a, b) in edges or (b, a) in edges:
            return True
    return False


def _route_diff(old_routes, new_routes):
    changes = []
    pairs = set(old_routes) | set(new_routes)
    for pair in sorted(pairs, key=lambda p: (_stable_key(p[0]),
                                             _stable_key(p[1]))):
        old_route = old_routes.get(pair)
        new_route = new_routes.get(pair)
        if not _route_changed(old_route, new_route):
            continue
        changes.append(RouteChange(pair[0], pair[1], old_route, new_route))
    return changes


def _connected_components(model):
    parent = {}

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for node in model.nodes:
        parent[node] = node
    for a, b in model.edges:
        union(a, b)
    return parent, find


class SurveyGraph(object):
    """A republishable visibility graph with stable survey ids.

    ``polygons`` and ``stations`` accept the same formats as
    :class:`SurveyCorrection`.  Vertices and stations are all graph nodes, so
    a shortest route may pass through another station, matching how the
    classic VisGraph handles points added with ``update``.

    ``routes`` maps an ordered station pair to :class:`Route` objects.  For an
    undirected survey only the canonical pair (lower stable id first) is
    stored; ``shortest_route`` reverses it on request.
    """

    def __init__(self, polygons=None, stations=None, directed=False):
        self.polygons = {}
        self.vertices = {}
        self.stations = {}
        self.edges = set()
        self.pair_index = {}
        self.routes = {}
        self.version = 0
        self.directed = directed
        self.history = []
        if polygons is not None:
            self.build(polygons, stations or [], directed=directed)

    def build(self, polygons, stations, directed=None, version=1, **kwargs):
        """Build a fresh survey revision and its visibility index."""
        model = _model_from_definitions(polygons, stations)
        if directed is not None:
            self.directed = directed
        _all_visibility(model)
        routes = _all_routes(model, self.directed)
        self.polygons = model.polygons
        self.vertices = model.vertices
        self.stations = model.stations
        self.edges = model.edges
        self.pair_index = model.pair_index
        self.routes = routes
        self.version = version
        self.history = []
        return self

    build_graph = build

    def rebuild(self):
        """Recompute the current revision from scratch.

        Useful as a consistency check: the returned routes equal the
        incrementally maintained routes for the same survey.
        """
        model = _Model(dict(self.polygons), dict(self.vertices),
                       dict(self.stations))
        _all_visibility(model)
        return model, _all_routes(model, self.directed)

    def apply(self, correction):
        """Apply one correction atomically and return a :class:`BatchReport`."""
        if self.version == 0:
            raise SurveyError("build() must be called before apply()")
        correction = _coerce_correction(correction)
        if correction.directed is not None:
            self.directed = bool(correction.directed)
        base_version = self.version

        old_model = _Model(dict(self.polygons), dict(self.vertices),
                           dict(self.stations))
        old_model.edges = set(self.edges)
        old_model.pair_index = dict(
            (pid, set(pairs)) for pid, pairs in self.pair_index.items())

        result = _apply_correction(self.polygons, self.vertices,
                                   self.stations, correction)
        new_model = result.model

        # Preserve every edge that still connects two surviving nodes; the
        # candidate pass below re-evaluates exactly the lines that can differ.
        new_model.edges.update(
            edge for edge in self.edges
            if edge[0] in new_model.nodes and edge[1] in new_model.nodes)

        moved_nodes = set(result.changed_nodes)
        stale_nodes = set(node for node in old_model.nodes
                          if node not in new_model.nodes)

        candidates = set()
        pair_index = dict(
            (pid, set(pairs)) for pid, pairs in old_model.pair_index.items())
        for polygon_id in result.changed_polygons:
            candidates.update(pair_index.get(polygon_id, ()))
            if polygon_id not in new_model.polygons:
                pair_index.pop(polygon_id, None)
            else:
                pair_index[polygon_id] = _pairs_for_bbox(
                    new_model, new_model.bboxes[polygon_id])
                candidates.update(pair_index[polygon_id])

        if moved_nodes or stale_nodes:
            # A moved node changes every line incident on it.  Drop only
            # those entries, then add the new incident entries back.
            changed_incident = moved_nodes | stale_nodes
            for entries in pair_index.values():
                doomed = [pair for pair in entries
                          if pair[0] in changed_incident or
                          pair[1] in changed_incident]
                entries.difference_update(doomed)
            all_nodes = sorted(new_model.nodes, key=_node_order)
            bbox_items = list(new_model.bboxes.items())
            for node in moved_nodes:
                if node not in new_model.nodes:
                    continue
                coord = new_model.coords[node]
                for other in all_nodes:
                    if other == node:
                        continue
                    other_coord = new_model.coords[other]
                    pair = _canonical_nodes(node, other)
                    for polygon_id, bbox in bbox_items:
                        if _segment_bbox_intersects(coord, other_coord, bbox):
                            pair_index[polygon_id].add(pair)

        for node in moved_nodes:
            if node in new_model.nodes:
                for other in new_model.nodes:
                    if other != node:
                        candidates.add(_canonical_nodes(node, other))
        for node in stale_nodes:
            for other in old_model.nodes:
                if other != node:
                    candidates.add(_canonical_nodes(node, other))

        for a, b in candidates:
            if a not in new_model.nodes or b not in new_model.nodes:
                continue
            if _visible(new_model, a, b):
                new_model.edges.add((a, b))
            else:
                new_model.edges.discard((a, b))

        all_added, all_removed = _edge_changes(
            self.edges, new_model.edges, old_model, new_model)
        _, station_added = BatchReport._split(all_added)
        _, station_removed = BatchReport._split(all_removed)
        added_all = set(edge.key for edge in all_added)
        removed_all = set(edge.key for edge in all_removed)

        old_routes = self.routes
        new_pairs = _station_pair_list(new_model.stations)
        if self.directed:
            new_pairs = new_pairs + [(b, a) for a, b in new_pairs]
        new_pair_set = set(new_pairs)
        old_pair_set = set(old_routes)

        changed_pairs = set()
        for pair, route in old_routes.items():
            if _route_uses_nodes(route, moved_nodes) or \
                    _route_uses_edges(route, removed_all):
                changed_pairs.add(pair)
        for station_id in result.added_stations:
            changed_pairs.update(
                pair for pair in new_pair_set if station_id in pair)

        # A pair with no old route can only become reachable if this batch
        # added connectivity.  When it did, retest every unreachable pair.
        if added_all:
            changed_pairs.update(new_pair_set - old_pair_set)

        # A new or shortened edge can improve a route that never touched a
        # changed node.  Use a Euclidean lower bound to skip pairs that
        # cannot reach the shortcut in time to beat the old length.
        shortcut_edges = set(added_all)
        for key in self.edges & new_model.edges:
            a, b = key
            if old_model.coords[a] != new_model.coords[a] or \
                    old_model.coords[b] != new_model.coords[b]:
                shortcut_edges.add(key)
        if shortcut_edges:
            for pair, route in old_routes.items():
                if pair in changed_pairs:
                    continue
                origin = (STATION, pair[0])
                destination = (STATION, pair[1])
                origin_coord = new_model.coords[origin]
                destination_coord = new_model.coords[destination]
                for a, b in shortcut_edges:
                    length = _distance(new_model.coords[a],
                                       new_model.coords[b])
                    lower = min(
                        _distance(origin_coord, new_model.coords[a]) +
                        length +
                        _distance(new_model.coords[b], destination_coord),
                        _distance(origin_coord, new_model.coords[b]) +
                        length +
                        _distance(new_model.coords[a], destination_coord))
                    if lower <= route.length + EPS * max(1.0,
                                                         route.length):
                        changed_pairs.add(pair)
                        break

            parent, find = _connected_components(new_model)
            for pair in new_pair_set - old_pair_set:
                a = (STATION, pair[0])
                b = (STATION, pair[1])
                if find(a) == find(b):
                    changed_pairs.add(pair)

        changed_pairs &= new_pair_set | old_pair_set
        recompute = changed_pairs & new_pair_set
        computed = _routes_for(new_model, sorted(
            recompute, key=lambda p: (_stable_key(p[0]), _stable_key(p[1]))))

        new_routes = {}
        for pair, route in old_routes.items():
            if pair in recompute or pair not in new_pair_set:
                continue
            new_routes[pair] = route
        new_routes.update(computed)

        route_changes = _route_diff(old_routes, new_routes)
        report = BatchReport(base_version, base_version + 1,
                             all_added, all_removed, route_changes,
                             self.directed, correction.metadata,
                             station_added, station_removed,
                             len(candidates), len(recompute),
                             sorted(set(pair[0] for pair in recompute),
                                    key=_stable_key))

        self.polygons = new_model.polygons
        self.vertices = new_model.vertices
        self.stations = new_model.stations
        self.edges = new_model.edges
        self.pair_index = pair_index
        self.routes = new_routes
        self.version = base_version + 1
        self.history.append(report)
        return report

    apply_correction = apply

    def add_polygon(self, polygon):
        return self.apply(SurveyCorrection(polygons_added=[polygon]))

    def remove_polygon(self, polygon_id):
        return self.apply(SurveyCorrection(polygons_removed=[polygon_id]))

    def update_polygon(self, polygon):
        return self.apply(SurveyCorrection(polygons_updated=[polygon]))

    def move_vertex(self, vertex_id, coord):
        return self.apply(SurveyCorrection(
            vertices_updated={vertex_id: coord}))

    def add_station(self, station):
        return self.apply(SurveyCorrection(stations_added=[station]))

    def remove_station(self, station_id):
        return self.apply(SurveyCorrection(stations_removed=[station_id]))

    def move_station(self, station_id, coord):
        return self.apply(SurveyCorrection(
            stations_updated={station_id: coord}))

    def shortest_path(self, origin, destination):
        """Return the sequence of stable ids for a published station pair."""
        route = self.shortest_route(origin, destination)
        return None if route is None else list(route.path)

    def shortest_route(self, origin, destination):
        pair = (origin, destination)
        route = self.routes.get(pair)
        if route is None and not self.directed:
            reverse = self.routes.get((destination, origin))
            if reverse is not None:
                return Route(destination, origin,
                             tuple(reversed(reverse.nodes)),
                             reverse.length)
        return route

    def distance(self, origin, destination):
        route = self.shortest_route(origin, destination)
        return None if route is None else route.length

    def replay(self, report, routes=None):
        """Replay a report over a route mapping."""
        return report.replay(self.routes if routes is None else routes)

    def save(self, filename):
        state = {'polygons': self.polygons,
                 'vertices': self.vertices,
                 'stations': self.stations,
                 'edges': self.edges,
                 'pair_index': self.pair_index,
                 'routes': self.routes,
                 'version': self.version,
                 'directed': self.directed,
                 'history': self.history}
        with open(filename, 'wb') as output:
            pickle.dump(state, output, -1)

    def load(self, filename):
        with open(filename, 'rb') as source:
            state = pickle.load(source)
        self.polygons = state['polygons']
        self.vertices = state['vertices']
        self.stations = state['stations']
        self.edges = state['edges']
        self.pair_index = state['pair_index']
        self.routes = state['routes']
        self.version = state['version']
        self.directed = state['directed']
        self.history = state.get('history', [])
        return self

    def __repr__(self):
        return ("SurveyGraph(version={!r}, polygons={!r}, vertices={!r}, "
                "stations={!r}, edges={!r})").format(
                    self.version, len(self.polygons), len(self.vertices),
                    len(self.stations), len(self.edges))


IncrementalVisGraph = SurveyGraph
