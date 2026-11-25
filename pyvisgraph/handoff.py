from __future__ import division

import heapq
import json
import math
import os

from pyvisgraph.graph import Point
from pyvisgraph.vis_graph import VisGraph
from pyvisgraph.visible_vertices import edge_distance, visible_vertices

EPS = 1e-9
EVENT_MERGE_EPS = 1e-11
DISTANCE_EPS = 1e-7


def as_point(value):
    if isinstance(value, Point):
        return Point(value.x, value.y)
    if isinstance(value, dict):
        if "x" in value and "y" in value:
            return Point(value["x"], value["y"])
        return Point(value[0], value[1])
    return Point(value[0], value[1])


def as_coord(value):
    point = as_point(value)
    return point.x, point.y


def point_at_parameter(start, vector, t):
    return Point(start[0] + vector[0] * t, start[1] + vector[1] * t)


def coordinates(polygon):
    return [(point.x, point.y) for point in polygon]


def polygon_vertex_count(polygon):
    count = len(polygon)
    if count > 1 and polygon[0] == polygon[-1]:
        count -= 1
    return count


def total_vertex_count(polygons):
    return sum(polygon_vertex_count(polygon) for polygon in polygons)


def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _point_on_segment_coords(px, py, ax, ay, bx, by, eps=EPS):
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    length2 = abx * abx + aby * aby
    cross = abs(_cross(abx, aby, apx, apy))
    scale = max(1.0, math.sqrt(length2))
    if cross > eps * scale * max(1.0, math.hypot(px, py)):
        return False
    if length2 <= eps * eps:
        return math.hypot(px - ax, py - ay) <= eps
    dot = apx * abx + apy * aby
    return -eps * length2 <= dot <= length2 * (1.0 + eps)


def point_on_segment(point, start, end, eps=EPS):
    px, py = as_coord(point)
    ax, ay = as_coord(start)
    bx, by = as_coord(end)
    return _point_on_segment_coords(px, py, ax, ay, bx, by, eps)


def point_in_polygon_strict(point, polygon):
    px, py = as_coord(point)
    points = [(p.x, p.y) if isinstance(p, Point) else as_coord(p) for p in polygon]
    count = len(points)
    if count < 3:
        return False
    for i in range(count):
        ax, ay = points[i]
        bx, by = points[(i + 1) % count]
        if _point_on_segment_coords(px, py, ax, ay, bx, by):
            return False
    inside = False
    for i in range(count):
        ax, ay = points[i]
        bx, by = points[(i + 1) % count]
        if (ay > py) != (by > py):
            x_intersection = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < x_intersection:
                inside = not inside
    return inside


def point_is_blocked(point, polygons):
    return any(point_in_polygon_strict(point, polygon) for polygon in polygons)


def _segment_parameter_with_edge(p1, p2, a, b):
    p1x, p1y = p1
    rx = p2[0] - p1x
    ry = p2[1] - p1y
    sx = b[0] - a[0]
    sy = b[1] - a[1]
    denominator = _cross(rx, ry, sx, sy)
    qpx = a[0] - p1x
    qpy = a[1] - p1y
    result = []
    if abs(denominator) > EPS:
        t = _cross(qpx, qpy, sx, sy) / denominator
        u = _cross(qpx, qpy, rx, ry) / denominator
        if -EPS <= t <= 1.0 + EPS and -EPS <= u <= 1.0 + EPS:
            result.append(min(1.0, max(0.0, t)))
        return result

    if abs(_cross(qpx, qpy, rx, ry)) > EPS:
        return result

    r_length2 = rx * rx + ry * ry
    if r_length2 <= EPS * EPS:
        return result
    for vertex in (a, b):
        t = ((vertex[0] - p1x) * rx + (vertex[1] - p1y) * ry) / r_length2
        if -EPS <= t <= 1.0 + EPS:
            result.append(min(1.0, max(0.0, t)))
    return result


def segment_is_clear(start, end, polygons):
    p = as_coord(start)
    q = as_coord(end)
    if math.hypot(q[0] - p[0], q[1] - p[1]) <= EPS:
        return not point_is_blocked(p, polygons)

    cuts = [0.0, 1.0]
    for polygon in polygons:
        coords = [(point.x, point.y) if isinstance(point, Point) else as_coord(point)
                  for point in polygon]
        count = len(coords)
        for i in range(count):
            for t in _segment_parameter_with_edge(
                    p, q, coords[i], coords[(i + 1) % count]):
                cuts.append(t)
    cuts.sort()
    unique = []
    for t in cuts:
        t = min(1.0, max(0.0, t))
        if not unique or abs(t - unique[-1]) > EVENT_MERGE_EPS:
            unique.append(t)
    for left, right in zip(unique, unique[1:]):
        if right - left <= EVENT_MERGE_EPS:
            continue
        middle = (left + right) * 0.5
        midpoint = (p[0] + (q[0] - p[0]) * middle,
                    p[1] + (q[1] - p[1]) * middle)
        if point_is_blocked(midpoint, polygons):
            return False
    return True


class MapModel(object):
    def __init__(self, polygons, name="map"):
        self.name = name
        self.polygons = []
        for polygon in polygons:
            clean = []
            for point in polygon:
                clean.append(as_point(point))
            if len(clean) > 1 and clean[0] == clean[-1]:
                clean = clean[:-1]
            self.polygons.append(clean)
        self.vertex_count = total_vertex_count(self.polygons)
        self.visgraph = VisGraph()
        self.visgraph.build(self.polygons, status=False)
        self._target_cache = {}

    def vertices(self):
        return self.visgraph.graph.get_points()

    def edges_containing(self, point):
        result = []
        px, py = as_coord(point)
        for edge in self.visgraph.graph.get_edges():
            if point_on_segment(Point(px, py), edge.p1, edge.p2):
                result.append(edge)
        return result

    def visible_anchors(self, point, target=None):
        p = as_point(point)
        destination = as_point(target) if target is not None else None
        visible = visible_vertices(p, self.visgraph.graph, destination=destination)
        anchors = {}

        def add(candidate):
            q = as_point(candidate)
            if q == p:
                return
            anchors[(q.x, q.y)] = q

        for candidate in visible:
            add(candidate)
        if destination is not None:
            add(destination)
        for edge in self.edges_containing(p):
            add(edge.p1)
            add(edge.p2)

        clear = []
        for anchor in anchors.values():
            if segment_is_clear(p, anchor, self.polygons):
                clear.append(anchor)
        return clear

    def target_distances(self, target):
        target = as_point(target)
        key = (target.x, target.y)
        if key in self._target_cache:
            return self._target_cache[key]

        distances = {target: 0.0}
        predecessors = {}
        heap = []
        counter = 0
        heapq.heappush(heap, (0.0, counter, target))
        for anchor in self.visible_anchors(target):
            if not segment_is_clear(target, anchor, self.polygons):
                continue
            anchor_distance = edge_distance(target, anchor)
            if anchor_distance < distances.get(anchor, math.inf):
                distances[anchor] = anchor_distance
                predecessors[anchor] = target
                counter += 1
                heapq.heappush(heap, (anchor_distance, counter, anchor))

        while heap:
            current_distance, _, point = heapq.heappop(heap)
            if current_distance != distances.get(point):
                continue
            for edge in self.visgraph.visgraph[point]:
                neighbor = edge.get_adjacent(point)
                new_distance = current_distance + edge_distance(point, neighbor)
                if new_distance < distances.get(neighbor, math.inf) - DISTANCE_EPS:
                    distances[neighbor] = new_distance
                    predecessors[neighbor] = point
                    counter += 1
                    heapq.heappush(heap, (new_distance, counter, neighbor))

        result = (distances, predecessors)
        self._target_cache[key] = result
        return result

    def evaluate_point(self, point, target):
        p = as_point(point)
        target = as_point(target)
        if point_is_blocked(p, self.polygons) or point_is_blocked(target, self.polygons):
            return {"reachable": False, "distance": None, "path": None,
                    "blocked": True}

        distances, predecessors = self.target_distances(target)
        best_distance = math.inf
        best_anchor = None
        for anchor in self.visible_anchors(p, target):
            graph_distance = distances.get(anchor, math.inf)
            if not math.isfinite(graph_distance):
                continue
            candidate = edge_distance(p, anchor) + graph_distance
            if candidate < best_distance:
                best_distance = candidate
                best_anchor = anchor

        if not math.isfinite(best_distance):
            return {"reachable": False, "distance": None, "path": None,
                    "blocked": False}

        path = [p, best_anchor]
        current = best_anchor
        seen = set()
        while current != target:
            if current in seen:
                break
            seen.add(current)
            parent = predecessors.get(current)
            if parent is None:
                break
            path.append(parent)
            current = parent
        return {"reachable": True, "distance": best_distance, "path": path,
                "blocked": False}


def critical_parameters(start, end, models, target):
    a = as_coord(start)
    b = as_coord(end)
    target_xy = as_coord(target)
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    cuts = [0.0, 1.0]

    def add(t):
        if t is None or not math.isfinite(t):
            return
        if -1e-8 <= t <= 1.0 + 1e-8:
            cuts.append(min(1.0, max(0.0, float(t))))

    anchors = [target_xy]
    blockers = []
    for model in models:
        for point in model.vertices():
            xy = (point.x, point.y)
            blockers.append(xy)
            anchors.append(xy)
        for polygon in model.polygons:
            coords = coordinates(polygon)
            count = len(coords)
            for i in range(count):
                edge_cuts = _segment_parameter_with_edge(
                    a, b, coords[i], coords[(i + 1) % count])
                    # The helper already returns parameters on start -> end.
                cuts.extend(edge_cuts)

    for qx, qy in anchors:
        for rx, ry in blockers:
            ex = rx - qx
            ey = ry - qy
            denominator = _cross(dx, dy, ex, ey)
            if abs(denominator) <= EPS:
                continue
            numerator = _cross(qx - a[0], qy - a[1], ex, ey)
            add(numerator / denominator)

    cuts.sort()
    merged = []
    for t in cuts:
        if not merged or abs(t - merged[-1]) > EVENT_MERGE_EPS:
            merged.append(t)
        else:
            merged[-1] = (merged[-1] + t) * 0.5
    return merged


def build_profile(model, start, vector, interval_t, target):
    left_t, right_t = interval_t
    middle_t = (left_t + right_t) * 0.5
    point = point_at_parameter(start, vector, middle_t)
    length = math.hypot(vector[0], vector[1])
    unit = (vector[0] / length, vector[1] / length) if length > EPS else (0.0, 0.0)
    distances, _ = model.target_distances(target)
    branches = []
    for anchor in model.visible_anchors(point, target):
        graph_distance = distances.get(anchor, math.inf)
        if not math.isfinite(graph_distance):
            continue
        ax, ay = anchor.x, anchor.y
        u = (ax - start[0]) * unit[0] + (ay - start[1]) * unit[1]
        distance_from_start = math.hypot(ax - start[0], ay - start[1])
        perpendicular2 = max(0.0, distance_from_start ** 2 - u ** 2)
        branches.append({
            "constant": graph_distance,
            "u": u,
            "h": math.sqrt(perpendicular2),
            "anchor": anchor,
        })
    return branches


def branch_value(branch, s):
    return branch["constant"] + math.hypot(s - branch["u"], branch["h"])


def branch_derivative(branch, s):
    h = branch["h"]
    u = branch["u"]
    if h <= EPS and abs(s - u) <= EVENT_MERGE_EPS:
        return -1.0 if s < u else 1.0
    return (s - u) / math.hypot(s - u, h)


def equal_distance_roots(left_branch, right_branch, low, high):
    c1 = left_branch["constant"]
    u1 = left_branch["u"]
    h1 = left_branch["h"]
    c2 = right_branch["constant"]
    u2 = right_branch["u"]
    h2 = right_branch["h"]
    k = c2 - c1
    roots = []

    if abs(k) <= DISTANCE_EPS:
        linear = 2.0 * (u2 - u1)
        constant = u1 * u1 + h1 * h1 - u2 * u2 - h2 * h2
        if abs(linear) > EPS:
            roots.append(-constant / linear)
        elif abs(constant) <= DISTANCE_EPS:
            return []
    else:
        a = 2.0 * (u2 - u1)
        b = u1 * u1 + h1 * h1 - u2 * u2 - h2 * h2 - k * k
        quadratic_a = a * a - 4.0 * k * k
        quadratic_b = 2.0 * a * b + 8.0 * k * k * u2
        quadratic_c = b * b - 4.0 * k * k * (u2 * u2 + h2 * h2)
        if abs(quadratic_a) <= EPS:
            if abs(quadratic_b) > EPS:
                roots.append(-quadratic_c / quadratic_b)
        else:
            discriminant = (quadratic_b * quadratic_b -
                            4.0 * quadratic_a * quadratic_c)
            if discriminant >= -DISTANCE_EPS:
                root = math.sqrt(max(0.0, discriminant))
                roots.append((-quadratic_b - root) / (2.0 * quadratic_a))
                roots.append((-quadratic_b + root) / (2.0 * quadratic_a))

    valid = []
    for s in roots:
        if not math.isfinite(s):
            continue
        if low - DISTANCE_EPS <= s <= high + DISTANCE_EPS:
            s = min(high, max(low, s))
            residual = (branch_value(left_branch, s) -
                        branch_value(right_branch, s))
            if abs(residual) <= 1e-5:
                valid.append(s)
    return valid


def stationary_roots(first, second, low, high):
    u1, h1 = first["u"], first["h"]
    u2, h2 = second["u"], second["h"]
    roots = []
    for sign in (-1.0, 1.0):
        denominator = h2 - sign * h1
        numerator = h2 * u1 - sign * h1 * u2
        if abs(denominator) > EPS:
            s = numerator / denominator
            if low - DISTANCE_EPS <= s <= high + DISTANCE_EPS:
                s = min(high, max(low, s))
                if abs(branch_derivative(first, s) -
                       branch_derivative(second, s)) <= 1e-5:
                    roots.append(s)
    if h1 <= EPS:
        roots.append(u1)
    if h2 <= EPS:
        roots.append(u2)
    return [s for s in roots if low - DISTANCE_EPS <= s <= high + DISTANCE_EPS]


def _active_branch(branches, s):
    return min(branches, key=lambda branch: branch_value(branch, s))


def maximum_envelope_difference(first_branches, second_branches, low, high):
    if not first_branches or not second_branches:
        return math.inf, (low + high) * 0.5, None, None

    cuts = {low, high}
    all_branches = first_branches + second_branches
    for i, branch_a in enumerate(all_branches):
        if branch_a["h"] <= EVENT_MERGE_EPS:
            cuts.add(branch_a["u"])
        for branch_b in all_branches[i + 1:]:
            for root in equal_distance_roots(branch_a, branch_b, low, high):
                cuts.add(root)
    cuts = sorted(s for s in cuts if low - DISTANCE_EPS <= s <= high + DISTANCE_EPS)
    if not cuts or cuts[0] > low + EVENT_MERGE_EPS:
        cuts.insert(0, low)
    if cuts[-1] < high - EVENT_MERGE_EPS:
        cuts.append(high)

    worst_value = -math.inf
    worst_s = None
    worst_pair = None
    for left, right in zip(cuts, cuts[1:]):
        if right - left <= EVENT_MERGE_EPS:
            continue
        middle = (left + right) * 0.5
        first = _active_branch(first_branches, middle)
        second = _active_branch(second_branches, middle)
        candidates = [left, right, middle]
        candidates.extend(stationary_roots(first, second, left, right))
        for s in candidates:
            value = branch_value(first, s) - branch_value(second, s)
            if value > worst_value:
                worst_value = value
                worst_s = s
                worst_pair = (first, second)
    return worst_value, worst_s, worst_pair, cuts


def _witness_from_s(start, direction, length, s):
    parameter = s / length if length > EPS else 0.0
    parameter = min(1.0, max(0.0, parameter))
    point = point_at_parameter(start, direction, s)
    return parameter, point


def verify_line(original, candidate, start_line, target, tolerance=DISTANCE_EPS):
    original_model = original if isinstance(original, MapModel) else MapModel(original, "original")
    candidate_model = candidate if isinstance(candidate, MapModel) else MapModel(candidate, "candidate")
    start_xy = as_coord(start_line[0])
    end_xy = as_coord(start_line[1])
    target = as_point(target)
    vector = (end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
    length = math.hypot(vector[0], vector[1])
    if length <= EPS:
        direction = (0.0, 0.0)
    else:
        direction = (vector[0] / length, vector[1] / length)

    events = critical_parameters(start_xy, end_xy,
                                 [original_model, candidate_model], target)
    worst = {
        "distance_delta": 0.0,
        "kind": "ok",
        "parameter": None,
        "point": None,
        "original_distance": None,
        "candidate_distance": None,
    }

    def consider_delta(delta, kind, t, point, old_result, new_result):
        if delta > worst["distance_delta"] + tolerance:
            worst.update({
                "distance_delta": delta,
                "kind": kind,
                "parameter": t,
                "point": (point.x, point.y),
                "original_distance": old_result.get("distance"),
                "candidate_distance": new_result.get("distance"),
                "original_reachable": old_result.get("reachable"),
                "candidate_reachable": new_result.get("reachable"),
                "original_path": coordinates(old_result["path"]) if old_result.get("path") else None,
                "candidate_path": coordinates(new_result["path"]) if new_result.get("path") else None,
            })
            return True
        return False

    for left_t, right_t in zip(events, events[1:]):
        if right_t - left_t <= EVENT_MERGE_EPS:
            continue
        left_s = left_t * length
        right_s = right_t * length
        original_profile = build_profile(
            original_model, start_xy, vector, (left_t, right_t), target)
        candidate_profile = build_profile(
            candidate_model, start_xy, vector, (left_t, right_t), target)

        original_empty = not original_profile
        candidate_empty = not candidate_profile
        middle_t = (left_t + right_t) * 0.5
        middle_point = point_at_parameter(start_xy, (
            end_xy[0] - start_xy[0], end_xy[1] - start_xy[1]), middle_t)
        old_mid = original_model.evaluate_point(middle_point, target)
        new_mid = candidate_model.evaluate_point(middle_point, target)
        if original_empty or candidate_empty:
            if original_empty != candidate_empty:
                consider_delta(math.inf, "reachability", middle_t, middle_point,
                               old_mid, new_mid)
            continue

        old_minus_new, s, _, _ = maximum_envelope_difference(
            original_profile, candidate_profile, left_s, right_s)
        if old_minus_new > tolerance:
            t, witness = _witness_from_s(start_xy, direction, length, s)
            old_result = original_model.evaluate_point(witness, target)
            new_result = candidate_model.evaluate_point(witness, target)
            actual_delta = (old_result["distance"] or math.inf) - (
                new_result["distance"] or math.inf)
            if actual_delta > tolerance:
                consider_delta(actual_delta, "shortened", t, witness,
                               old_result, new_result)

        new_minus_old, s, _, _ = maximum_envelope_difference(
            candidate_profile, original_profile, left_s, right_s)
        if new_minus_old > tolerance:
            t, witness = _witness_from_s(start_xy, direction, length, s)
            old_result = original_model.evaluate_point(witness, target)
            new_result = candidate_model.evaluate_point(witness, target)
            actual_delta = (new_result["distance"] or math.inf) - (
                old_result["distance"] or math.inf)
            if actual_delta > tolerance:
                consider_delta(actual_delta, "lengthened", t, witness,
                               old_result, new_result)

    for t in events:
        vector = (end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
        point = point_at_parameter(start_xy, vector, t)
        old_result = original_model.evaluate_point(point, target)
        new_result = candidate_model.evaluate_point(point, target)
        if old_result["reachable"] != new_result["reachable"]:
            consider_delta(math.inf, "reachability", t, point,
                           old_result, new_result)
            continue
        if old_result["reachable"] and new_result["reachable"]:
            delta = abs(old_result["distance"] - new_result["distance"])
            if delta > tolerance:
                consider_delta(delta, "distance", t, point,
                               old_result, new_result)

    if worst["kind"] != "ok":
        return _failure_report(worst, events)

    return {
        "compliant": True,
        "events": events,
        "worst": worst,
        "checked_parameters": events,
    }


def _failure_report(worst, events):
    return {
        "compliant": False,
        "events": events,
        "witness": {
            "parameter": worst["parameter"],
            "point": worst["point"],
            "kind": worst["kind"],
            "distance_delta": None if not math.isfinite(worst["distance_delta"])
            else worst["distance_delta"],
            "original_distance": worst["original_distance"],
            "candidate_distance": worst["candidate_distance"],
            "original_reachable": worst.get("original_reachable"),
            "candidate_reachable": worst.get("candidate_reachable"),
            "original_path": worst.get("original_path"),
            "candidate_path": worst.get("candidate_path"),
        },
    }


def _polygons_from_choice(choice):
    if choice is None:
        return []
    if isinstance(choice, dict):
        polygon = choice.get("polygon") or choice.get("points") or choice.get("coordinates")
    else:
        polygon = choice
    return [[as_point(point) for point in polygon]]


def iter_choice_sets(handoff):
    obstacles = handoff["obstacles"]
    options = []
    for obstacle in obstacles:
        choices = (obstacle.get("choices") or obstacle.get("options") or
                   obstacle.get("allowed_contours") or obstacle.get("contours") or [])
        choice_options = []
        if obstacle.get("allow_omit", obstacle.get("omittable", False)):
            choice_options.append(None)
        choice_options.extend(choices)
        options.append(choice_options)

    def enumerate_options(index, selected):
        if index == len(options):
            yield list(selected)
            return
        for choice in options[index]:
            selected.append(choice)
            for result in enumerate_options(index + 1, selected):
                yield result
            selected.pop()

    for selected in enumerate_options(0, []):
        polygons = []
        selection = []
        for obstacle, choice in zip(obstacles, selected):
            if choice is None:
                selection.append({
                    "obstacle_id": obstacle.get("id"),
                    "choice_id": obstacle.get("omit_id", "omitted"),
                    "omitted": True,
                    "source": obstacle.get("source"),
                    "vertex_count": 0,
                })
                continue
            polygon = _polygons_from_choice(choice)[0]
            polygons.append(polygon)
            selection.append({
                "obstacle_id": obstacle.get("id"),
                "choice_id": choice.get("id") if isinstance(choice, dict) else None,
                "omitted": False,
                "source": (choice.get("source") if isinstance(choice, dict) else None)
                          or obstacle.get("source"),
                "original_source": obstacle.get("source"),
                "vertex_count": polygon_vertex_count(polygon),
                "polygon": polygon,
            })
        yield polygons, selection


def solve_handoff(handoff, tolerance=DISTANCE_EPS):
    original_polygons = handoff.get("original") or handoff.get("original_polygons")
    if isinstance(original_polygons, dict):
        original_polygons = (original_polygons.get("polygons") or
                             original_polygons.get("contours"))
    if original_polygons is None:
        raise ValueError("handoff is missing 'original' polygons")
    original_model = MapModel(original_polygons, "original")
    inspections = handoff.get("inspections") or handoff.get("lines") or []
    if not inspections:
        raise ValueError("handoff is missing 'inspections'")

    ranked = []
    for polygons, selection in iter_choice_sets(handoff):
        vertex_count = total_vertex_count(polygons)
        candidate_model = MapModel(polygons, "candidate")
        line_reports = []
        compliant = True
        for inspection in inspections:
            start_line = (inspection.get("start_line") or inspection.get("line") or
                          inspection.get("start_segment") or inspection.get("start"))
            target = (inspection.get("target") or inspection.get("target_station") or
                      inspection.get("destination"))
            report = verify_line(original_model, candidate_model,
                                 start_line, target, tolerance=tolerance)
            line_reports.append({
                "inspection_id": inspection.get("id"),
                "report": report,
            })
            if not report["compliant"]:
                compliant = False
                break
        ranked.append({
            "compliant": compliant,
            "vertex_count": vertex_count,
            "selection": selection,
            "line_reports": line_reports,
            "polygons": polygons,
        })

    compliant = [item for item in ranked if item["compliant"]]
    if not compliant:
        failures = sorted(ranked, key=lambda item: item["vertex_count"])
        return {
            "compliant": False,
            "checked": len(ranked),
            "candidates": [_public_candidate(item, include_report=True)
                           for item in failures[:5]],
        }

    compliant.sort(key=lambda item: (
        item["vertex_count"],
        [str(entry.get("choice_id")) for entry in item["selection"]],
    ))
    best = compliant[0]
    return {
        "compliant": True,
        "checked": len(ranked),
        "vertex_count": best["vertex_count"],
        "selection": [{k: v for k, v in entry.items() if k != "polygon"}
                      for entry in best["selection"]],
        "polygons": best["polygons"],
        "line_reports": best["line_reports"],
        "deliverable": _deliverable(handoff, best),
    }


def _public_candidate(item, include_report=False):
    result = {
        "vertex_count": item["vertex_count"],
        "selection": [{k: v for k, v in entry.items() if k != "polygon"}
                      for entry in item["selection"]],
    }
    if include_report and item["line_reports"]:
        report = item["line_reports"][0]["report"]
        result["first_failure"] = report.get("witness")
    return result


def _deliverable(handoff, best):
    features = []
    contours = []
    provenance = []
    selected_polygons = best["polygons"]
    selected_entries = [entry for entry in best["selection"] if not entry["omitted"]]
    for entry in best["selection"]:
        if entry["omitted"]:
            provenance.append({
                "obstacle_id": entry["obstacle_id"],
                "choice_id": entry["choice_id"],
                "omitted": True,
                "vertex_count": 0,
                "source": entry["source"],
                "original_source": entry.get("original_source"),
            })
    for polygon, entry in zip(selected_polygons, selected_entries):
        ring = [[point.x, point.y] for point in polygon]
        contours.append({"id": entry["obstacle_id"], "points": ring})
        features.append({
            "type": "Feature",
            "properties": {
                "id": entry["obstacle_id"],
                "choice_id": entry["choice_id"],
                "source": entry["source"],
            },
            "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]},
        })
        provenance.append({
            "obstacle_id": entry["obstacle_id"],
            "choice_id": entry["choice_id"],
            "omitted": False,
            "vertex_count": entry["vertex_count"],
            "source": entry["source"],
            "original_source": entry.get("original_source"),
        })
    return {
        "format": "gsb-pyvisgraph-handoff-v1",
        "original_source": handoff.get("source"),
        "vertex_count": best["vertex_count"],
        "contours": contours,
        "provenance": provenance,
        "geojson": {"type": "FeatureCollection", "features": features},
    }


def load_handoff(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data


def write_deliverables(result, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    contour_path = os.path.join(output_dir, "selected_contours.json")
    geojson_path = os.path.join(output_dir, "public_map.geojson")
    report_path = os.path.join(output_dir, "handoff_report.json")
    provenance_path = os.path.join(output_dir, "provenance.json")
    graph_path = os.path.join(output_dir, "public_map.pkl")
    legacy_graph_path = os.path.join(output_dir, "public_map.pk1")

    deliverable = result["deliverable"]
    with open(contour_path, "w", encoding="utf-8") as handle:
        json.dump({
            "format": deliverable["format"],
            "vertex_count": deliverable["vertex_count"],
            "contours": deliverable["contours"],
            "provenance": deliverable["provenance"],
        }, handle, ensure_ascii=False, indent=2)
    with open(geojson_path, "w", encoding="utf-8") as handle:
        json.dump(deliverable["geojson"], handle, ensure_ascii=False, indent=2)
    with open(provenance_path, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(deliverable["provenance"]), handle,
                  ensure_ascii=False, indent=2)

    serializable = dict(result)
    serializable.pop("polygons", None)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(serializable), handle, ensure_ascii=False, indent=2)

    graph = VisGraph()
    graph.build(result["polygons"], status=False)
    graph.save(graph_path)
    graph.save(legacy_graph_path)
    return {
        "contours": contour_path,
        "geojson": geojson_path,
        "provenance": provenance_path,
        "report": report_path,
        "graph": graph_path,
        "legacy_graph": legacy_graph_path,
    }


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


verify_entire_line = verify_line
select_minimal_map = solve_handoff
find_compliant_handoff = solve_handoff


def analyze_handoff_file(path, output_dir="deliverable"):
    handoff = load_handoff(path)
    result = solve_handoff(handoff)
    if result["compliant"]:
        result["files"] = write_deliverables(result, output_dir)
    else:
        os.makedirs(output_dir, exist_ok=True)
        confirmation_path = os.path.join(output_dir, "confirmation_needed.json")
        with open(confirmation_path, "w", encoding="utf-8") as handle:
            json.dump(_json_safe(result), handle, ensure_ascii=False, indent=2)
        result["confirmation_file"] = confirmation_path
    return result


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Select the minimum-vertex public contour set while "
                    "preserving Euclidean distances from every point of each "
                    "maintenance start line.")
    parser.add_argument("handoff", nargs="?",
                        help="Path to the station handoff JSON file.")
    parser.add_argument("--out", default="deliverable",
                        help="Output directory for reopenable contours.")
    args = parser.parse_args(argv)

    candidate_paths = [
        args.handoff,
        os.environ.get("HANDOFF_FILE"),
        "handoff.json",
        "input/handoff.json",
        "data/handoff.json",
        "station_handoff.json",
    ]
    path = next((candidate for candidate in candidate_paths
                 if candidate and os.path.exists(candidate)), None)
    if path is None:
        parser.error(
            "no handoff file found. Provide a JSON file containing "
            "'original', 'obstacles', and 'inspections'.")

    result = analyze_handoff_file(path, args.out)
    summary = {
        "compliant": result["compliant"],
        "checked": result["checked"],
    }
    if result["compliant"]:
        summary.update({
            "vertex_count": result["vertex_count"],
            "selection": result["selection"],
            "files": result["files"],
        })
    else:
        summary.update({
            "confirmation_file": result["confirmation_file"],
            "nearest_candidates": result["candidates"],
        })
    print(json.dumps(_json_safe(summary), ensure_ascii=False, indent=2))
    return 0 if result["compliant"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
