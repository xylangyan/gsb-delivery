# -*- coding: utf-8 -*-

import itertools

from . import geobuf_pb2
from .decode import Decoder
from .encode import Encoder

TOL = 1e-9
BOUNDARY_KEY = "boundary_segments"


class NoPacketArrangementError(Exception):
    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.reasons = reasons or {}


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _cross(u, v):
    return u[0] * v[1] - u[1] * v[0]


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1]


def _norm(u):
    return (_dot(u, u)) ** 0.5


def _line_key(a, b):
    d = _sub(b, a)
    length = _norm(d)
    alpha = d[1] / length
    beta = -d[0] / length
    gamma = -(alpha * a[0] + beta * a[1])
    if alpha < -TOL or (abs(alpha) <= TOL and beta < -TOL):
        alpha, beta, gamma = -alpha, -beta, -gamma
    return round(alpha, 9), round(beta, 9), round(gamma, 9)


def _edge_overlap(e1, e2):
    p1, q1 = e1
    p2, q2 = e2
    d1 = _sub(q1, p1)
    d2 = _sub(q2, p2)
    if abs(_cross(d1, d2)) > TOL:
        return None
    if abs(_cross(d1, _sub(p2, p1))) > TOL:
        return None
    n2 = _dot(d1, d1)
    if n2 <= TOL:
        return None
    t2 = sorted((
        _dot(_sub(p2, p1), d1) / n2,
        _dot(_sub(q2, p1), d1) / n2,
    ))
    lo = max(0.0, t2[0])
    hi = min(1.0, t2[1])
    if (hi - lo) * (n2 ** 0.5) <= TOL:
        return None
    start = (p1[0] + d1[0] * lo, p1[1] + d1[1] * lo)
    end = (p1[0] + d1[0] * hi, p1[1] + d1[1] * hi)
    return start, end


def _iter_polygons(geometry):
    gtype = geometry['type']
    if gtype == 'Polygon':
        return [geometry['coordinates']]
    if gtype == 'MultiPolygon':
        return geometry['coordinates']
    return []


def _iter_rings(geometry):
    for polygon in _iter_polygons(geometry):
        for ring in polygon:
            yield ring


def _ring_edges(ring):
    points = [tuple(p[:2]) for p in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]


def _all_edges(geometry):
    edges = []
    for ring in _iter_rings(geometry):
        edges.extend(_ring_edges(ring))
    return edges


def _dedupe_t(points_with_t):
    ordered = sorted(points_with_t, key=lambda item: item[0])
    result = []
    for t_value, point in ordered:
        if result and abs(t_value - result[-1][0]) <= TOL:
            continue
        result.append((t_value, point))
    return result


def extract_boundaries(features):
    edges_by_parcel = [
        _all_edges(feature['geometry']) if _iter_polygons(feature['geometry']) else []
        for feature in features
    ]
    boundaries = []
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            groups = {}
            for e1 in edges_by_parcel[i]:
                key1 = _line_key(*e1)
                for e2 in edges_by_parcel[j]:
                    if _line_key(*e2) != key1:
                        continue
                    overlap = _edge_overlap(e1, e2)
                    if overlap is None:
                        continue
                    groups.setdefault(key1, []).append(overlap)
            for key, overlaps in groups.items():
                alpha, beta, _ = key
                ux, uy = -beta, alpha
                intervals = []
                for start, end in overlaps:
                    t_start = _dot(start, (ux, uy))
                    t_end = _dot(end, (ux, uy))
                    intervals.append((min(t_start, t_end), max(t_start, t_end)))
                intervals.sort()
                merged = []
                for lo, hi in intervals:
                    if merged and lo - merged[-1][1] <= TOL:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                    else:
                        merged.append((lo, hi))
                vertices = []
                for edge in itertools.chain(edges_by_parcel[i], edges_by_parcel[j]):
                    for point in edge:
                        pk = _line_key(*edge)
                        if pk != key:
                            continue
                        t_value = _dot(point, (ux, uy))
                        vertices.append((t_value, point))
                for lo, hi in merged:
                    chain = [
                        (t_value, point)
                        for t_value, point in vertices
                        if lo - TOL <= t_value <= hi + TOL
                    ]
                    base_t, base_point = _dedupe_t(chain)[0]
                    points = [
                        (
                            base_point[0] + (t_value - base_t) * ux,
                            base_point[1] + (t_value - base_t) * uy,
                        )
                        for t_value, _ in _dedupe_t(chain)
                    ]
                    boundaries.append({
                        'parcels': (i, j),
                        'line': key,
                        'points': points,
                    })
    boundaries.sort(key=lambda item: (
        item['parcels'][0], item['parcels'][1],
        item['points'][0][0], item['points'][0][1],
    ))
    for index, boundary in enumerate(boundaries):
        boundary['id'] = 'B%d' % (index + 1)
    return boundaries


def _boundary_ids_for(boundaries, parcel_index):
    return [
        boundary['id']
        for boundary in boundaries
        if parcel_index in boundary['parcels']
    ]


def _round_point(point, precision):
    factor = 10 ** precision
    return tuple(int(round(value * factor)) / factor for value in point[:2])


def _ring_area(ring):
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return area / 2.0


def _distinct(points):
    result = []
    for point in points:
        if not result or _norm(_sub(point, result[-1])) > TOL:
            result.append(point)
    if len(result) > 1 and _norm(_sub(result[0], result[-1])) <= TOL:
        result = result[:-1]
    return result


def _point_in_ring(point, ring, tolerance):
    x, y = point
    inside = False
    for i in range(len(ring) - 1):
        ax, ay = ring[i][0], ring[i][1]
        bx, by = ring[i + 1][0], ring[i + 1][1]
        intersects = ((ay > y) != (by > y)) and (
            x < (bx - ax) * (y - ay) / (by - ay) + ax
        )
        if intersects:
            inside = not inside
    if not inside and _point_on_ring(point, ring, tolerance):
        inside = True
    return inside


def _point_on_segment(point, start, end, tolerance=TOL):
    delta = _sub(end, start)
    if abs(_cross(delta, _sub(point, start))) > tolerance * max(1.0, _norm(delta)):
        return False
    return (
        min(start[0], end[0]) - tolerance <= point[0] <= max(start[0], end[0]) + tolerance
        and min(start[1], end[1]) - tolerance <= point[1] <= max(start[1], end[1]) + tolerance
    )


def _point_on_ring(point, ring, tolerance=TOL):
    return any(
        _point_on_segment(point, ring[i], ring[i + 1], tolerance)
        for i in range(len(ring) - 1)
    )


def validate_geometry(geometry, precision):
    issues = []
    for polygon_index, polygon in enumerate(_iter_polygons(geometry)):
        rounded_polygon = [
            [_round_point(point, precision) for point in ring]
            for ring in polygon
        ]
        polygon_issues = []
        for ring_index, ring in enumerate(rounded_polygon):
            distinct = _distinct(ring)
            if len(distinct) < 3 or abs(_ring_area(ring)) <= 1e-12:
                polygon_issues.append({
                    'kind': 'ring',
                    'polygon': polygon_index,
                    'ring': ring_index,
                    'reason': 'ring collapses at precision %d' % precision,
                })
        if polygon_issues:
            issues.extend(polygon_issues)
            continue
        outer = rounded_polygon[0]
        for ring_index, ring in enumerate(rounded_polygon[1:], start=1):
            centroid = (
                sum(p[0] for p in _distinct(ring)) / len(_distinct(ring)),
                sum(p[1] for p in _distinct(ring)) / len(_distinct(ring)),
            )
            if not _point_in_ring(centroid, outer, 10 ** -precision):
                issues.append({
                    'kind': 'ring',
                    'polygon': polygon_index,
                    'ring': ring_index,
                    'reason': 'hole falls outside outer ring at precision %d' % precision,
                })
    return issues


def _polyline_covers(ring, start, end, tolerance):
    if _norm(_sub(start, end)) <= tolerance:
        return _point_on_ring(start, ring, tolerance)
    for i in range(len(ring) - 1):
        edge_start, edge_end = ring[i], ring[i + 1]
        if (_point_on_segment(start, edge_start, edge_end, tolerance)
                and _point_on_segment(end, edge_start, edge_end, tolerance)):
            return True
    return False


def check_boundary(boundary, geometry_a, precision_a, geometry_b, precision_b):
    chain_a = [_round_point(point, precision_a) for point in boundary['points']]
    chain_b = [_round_point(point, precision_b) for point in boundary['points']]
    detail = {'boundary_id': boundary['id'], 'ok': True}
    for point, rounded_a, rounded_b in zip(boundary['points'], chain_a, chain_b):
        if _norm(_sub(rounded_a, rounded_b)) > TOL:
            detail.update({
                'ok': False,
                'reason': 'read-back boundary points diverge',
                'point': list(point),
                'rounded': [list(rounded_a), list(rounded_b)],
                'precisions': [precision_a, precision_b],
            })
            return detail
    rings_a = [
        [_round_point(p, precision_a) for p in ring]
        for ring in _iter_rings(geometry_a)
    ]
    rings_b = [
        [_round_point(p, precision_b) for p in ring]
        for ring in _iter_rings(geometry_b)
    ]
    tolerance = max(10 ** -precision_a, 10 ** -precision_b)
    for chain, rings, precision in (
        (chain_a, rings_a, precision_a),
        (chain_b, rings_b, precision_b),
    ):
        for start, end in zip(chain, chain[1:]):
            if _norm(_sub(start, end)) <= tolerance:
                continue
            if not any(_polyline_covers(ring, start, end, tolerance) for ring in rings):
                detail.update({
                    'ok': False,
                    'reason': 'parcel ring does not cover boundary segment at precision %d' % precision,
                    'segment': [list(start), list(end)],
                })
                return detail
    return detail


def _feature_id(feature, index):
    return feature.get('id', index)


def _packet_feature(feature, index, boundary_ids):
    packet_feature = {'type': 'Feature', 'geometry': feature['geometry']}
    if 'id' in feature:
        packet_feature['id'] = feature['id']
    if feature.get('properties'):
        packet_feature['properties'] = dict(feature['properties'])
    if boundary_ids:
        packet_feature[BOUNDARY_KEY] = list(boundary_ids)
    return packet_feature


def encode_run(features, start, end, precision, boundaries):
    packet_features = [
        _packet_feature(feature, index, _boundary_ids_for(boundaries, index))
        for index, feature in enumerate(features[start:end], start=start)
    ]
    collection = {'type': 'FeatureCollection', 'features': packet_features}
    return Encoder().encode(collection, precision=precision)


def _components(n, boundaries):
    parent = list(range(n))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        parent[find(a)] = find(b)

    for boundary in boundaries:
        union(*boundary['parcels'])
    groups = {}
    for index in range(n):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _candidate_key(total_bytes, packets):
    return (
        total_bytes,
        len(packets),
        tuple(packet['end'] for packet in packets),
        tuple(packet['precision'] for packet in packets),
    )


def plan_packets(collection, precisions, max_bytes):
    features = collection['features']
    count = len(features)
    boundaries = extract_boundaries(features)
    components = _components(count, boundaries)

    if count == 0:
        return {
            'packets': [],
            'total_bytes': 0,
            'parcel_precisions': [],
            'boundaries': [],
        }

    ring_issues = {}
    valid_at = {}
    for precision in precisions:
        valid_at[precision] = []
        for index, feature in enumerate(features):
            issues = validate_geometry(feature['geometry'], precision)
            valid_at[precision].append(not issues)
            if issues:
                ring_issues.setdefault(index, []).extend(issues)

    boundary_checks = {precision: {} for precision in precisions}
    for precision in precisions:
        for boundary in boundaries:
            i, j = boundary['parcels']
            check = check_boundary(
                boundary,
                features[i]['geometry'], precision,
                features[j]['geometry'], precision,
            )
            boundary_checks[precision][boundary['id']] = check

    component_options = []
    for component in components:
        members = set(component)
        internal_boundaries = [
            boundary['id']
            for boundary in boundaries
            if members.issuperset(boundary['parcels'])
        ]
        options = []
        for precision in precisions:
            if not all(valid_at[precision][index] for index in component):
                continue
            if any(
                not boundary_checks[precision][boundary_id]['ok']
                for boundary_id in internal_boundaries
            ):
                continue
            options.append(precision)
        component_options.append(options)

    size_report = {}
    for index, feature in enumerate(features):
        sizes = {}
        for precision in precisions:
            if valid_at[precision][index]:
                sizes[precision] = len(encode_run(features, index, index + 1, precision, boundaries))
        size_report[index] = sizes

    if any(not options for options in component_options):
        raise NoPacketArrangementError(
            'no precision keeps every linked parcel and its boundaries valid',
            {
                'boundaries': [
                    {'id': b['id'], 'parcels': [_feature_id(features[i], i) for i in b['parcels']]}
                    for b in boundaries
                ],
                'failed_rings': [
                    {
                        'parcel': _feature_id(features[index], index),
                        'issues': issues,
                    }
                    for index, issues in ring_issues.items()
                ],
                'boundary_failures': [
                    check
                    for checks in boundary_checks.values()
                    for check in checks.values()
                    if not check['ok']
                ],
                'size_limits': {
                    'max_bytes': max_bytes,
                    'parcel_min_bytes': {
                        _feature_id(features[index], index): (
                            min(sizes.values()) if sizes else None
                        )
                        for index, sizes in size_report.items()
                    },
                },
            },
        )

    encoded_cache = {}

    def encoded(start, end, precision):
        key = (start, end, precision)
        if key not in encoded_cache:
            encoded_cache[key] = encode_run(features, start, end, precision, boundaries)
        return encoded_cache[key]

    best = None
    for option_set in itertools.product(*component_options):
        parcel_precision = [0] * count
        for component, precision in zip(components, option_set):
            for index in component:
                parcel_precision[index] = precision

        for cut_mask in range(2 ** (count - 1)):
            runs = []
            start = 0
            for cut in range(count - 1):
                if cut_mask & (1 << cut):
                    runs.append((start, cut + 1))
                    start = cut + 1
            runs.append((start, count))

            if any(
                len({parcel_precision[index] for index in range(run_start, run_end)}) != 1
                for run_start, run_end in runs
            ):
                continue

            packets = []
            total = 0
            for run_start, run_end in runs:
                precision = parcel_precision[run_start]
                data = encoded(run_start, run_end, precision)
                if len(data) > max_bytes:
                    packets = None
                    break
                total += len(data)
                packets.append({
                    'index': len(packets),
                    'precision': precision,
                    'start': run_start,
                    'end': run_end,
                    'bytes': len(data),
                    'data': data,
                    'feature_ids': [
                        _feature_id(features[index], index)
                        for index in range(run_start, run_end)
                    ],
                    'boundary_segments': sorted({
                        boundary_id
                        for index in range(run_start, run_end)
                        for boundary_id in _boundary_ids_for(boundaries, index)
                    }),
                })
            if packets is None:
                continue

            key = _candidate_key(total, packets)
            if best is None or key < best[0]:
                best = (key, packets, total, list(parcel_precision))

    if best is None:
        raise NoPacketArrangementError(
            'no contiguous arrangement fits the byte limit',
            {
                'boundaries': [
                    {'id': b['id'], 'parcels': [_feature_id(features[i], i) for i in b['parcels']]}
                    for b in boundaries
                ],
                'failed_rings': [
                    {
                        'parcel': _feature_id(features[index], index),
                        'issues': issues,
                    }
                    for index, issues in ring_issues.items()
                ],
                'size_limits': {
                    'max_bytes': max_bytes,
                    'parcel_min_bytes': {
                        _feature_id(features[index], index): (
                            min(sizes.values()) if sizes else None
                        )
                        for index, sizes in size_report.items()
                    },
                },
            },
        )

    _, packets, total, parcel_precision = best
    return {
        'packets': packets,
        'total_bytes': total,
        'parcel_precisions': [
            {'parcel': _feature_id(features[index], index), 'precision': precision}
            for index, precision in enumerate(parcel_precision)
        ],
        'boundaries': [
            {
                'id': boundary['id'],
                'parcels': [
                    _feature_id(features[i], i)
                    for i in boundary['parcels']
                ],
                'points': [list(point) for point in boundary['points']],
            }
            for boundary in boundaries
        ],
    }


def verify_boundaries(packets, boundaries=None):
    decoded = []
    precisions = {}
    for packet in packets:
        data = packet['data'] if isinstance(packet, dict) else packet
        message = geobuf_pb2.Data()
        message.ParseFromString(data)
        geojson = Decoder().decode(data)
        for feature in geojson['features']:
            index = len(decoded)
            decoded.append(feature)
            precisions[index] = message.precision
            if feature.get('id') is not None:
                precisions[feature['id']] = message.precision
    if boundaries is None:
        boundaries = extract_boundaries(decoded)
    by_id = {}
    for index, feature in enumerate(decoded):
        by_id[index] = feature
        if feature.get('id') is not None:
            by_id[feature['id']] = feature
    checks = []
    for boundary in boundaries:
        id_a, id_b = boundary['parcels']
        feature_a = by_id[id_a]
        feature_b = by_id[id_b]
        checks.append(check_boundary(
            boundary,
            feature_a['geometry'], precisions[id_a],
            feature_b['geometry'], precisions[id_b],
        ))
    return {
        'ok': all(check['ok'] for check in checks),
        'checks': checks,
    }
