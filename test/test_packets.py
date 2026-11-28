import json
import os

import pytest

import geobuf
from geobuf import (
    Decoder,
    Encoder,
    NoPacketArrangementError,
    check_boundary,
    encode_run,
    extract_boundaries,
    plan_packets,
    verify_boundaries,
)
from geobuf.packets import BOUNDARY_KEY, _candidate_key

LONG_NAME = "very_long_survey_attribute_name_for_field_observation"


def rectangle(x0, x1, holes=None, extra_boundary_point=False):
    ring = [[x0, 0.0], [x1, 0.0], [x1, 1.0], [x0, 1.0], [x0, 0.0]]
    if extra_boundary_point:
        ring.insert(2, [x1, 0.5])
    rings = [ring]
    for hx0, hx1, hy0, hy1 in holes or []:
        rings.append([
            [hx0, hy0], [hx0, hy1], [hx1, hy1], [hx1, hy0], [hx0, hy0],
        ])
    return {'type': 'Polygon', 'coordinates': rings}


@pytest.fixture
def two_parcels():
    west = {
        'type': 'Feature',
        'id': 'W',
        'properties': {'owner': 'west-team'},
        'geometry': rectangle(0.0, 1.04, extra_boundary_point=True),
    }
    east = {
        'type': 'Feature',
        'id': 'E',
        'properties': {'owner': 'east-team'},
        # closed in the opposite winding direction, with an interior hole
        'geometry': {
            'type': 'Polygon',
            'coordinates': [
                [[1.04, 1.0], [2.0, 1.0], [2.0, 0.0], [1.04, 0.0], [1.04, 1.0]],
                [[1.23, 0.31], [1.23, 0.41], [1.24, 0.41], [1.24, 0.31], [1.23, 0.31]],
            ],
        },
    }
    return {'type': 'FeatureCollection', 'features': [west, east]}


def test_extract_boundary_keeps_collinear_station_and_normalizes_direction(two_parcels):
    boundaries = extract_boundaries(two_parcels['features'])
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary['id'] == 'B1'
    assert boundary['parcels'] == (0, 1)
    # the west surveyor's extra collinear station at y=0.5 survives as a segment point
    assert boundary['points'] == [(1.04, 0.0), (1.04, 0.5), (1.04, 1.0)]


def test_precision_one_collapses_hole_and_reports_failed_ring(two_parcels):
    with pytest.raises(NoPacketArrangementError) as exc_info:
        plan_packets(two_parcels, [1], max_bytes=10 ** 9)
    reasons = exc_info.value.reasons
    assert reasons['boundaries'][0]['parcels'] == ['W', 'E']
    failed = reasons['failed_rings'][0]
    assert failed['parcel'] == 'E'
    assert any(issue['ring'] == 1 and 'collapses' in issue['reason'] for issue in failed['issues'])
    assert reasons['size_limits']['max_bytes'] == 10 ** 9


def test_mismatched_boundary_precision_is_rejected(two_parcels):
    boundary = extract_boundaries(two_parcels['features'])[0]
    west, east = two_parcels['features']
    check = check_boundary(boundary, west['geometry'], 1, east['geometry'], 2)
    assert check['ok'] is False
    assert check['reason'] == 'read-back boundary points diverge'
    check = check_boundary(boundary, west['geometry'], 2, east['geometry'], 2)
    assert check['ok'] is True


def test_two_parcel_plan_uses_single_precision_and_verifies(two_parcels):
    result = plan_packets(two_parcels, [1, 2], max_bytes=10 ** 9)
    assert len(result['packets']) == 1
    packet = result['packets'][0]
    assert packet['precision'] == 2
    assert packet['feature_ids'] == ['W', 'E']

    verification = verify_boundaries(result['packets'], result['boundaries'])
    assert verification['ok'] is True

    decoded = geobuf.decode(packet['data'])
    assert decoded['type'] == 'FeatureCollection'
    by_id = {feature['id']: feature for feature in decoded['features']}
    assert set(by_id) == {'W', 'E'}
    assert by_id['W']['properties'] == {'owner': 'west-team'}
    assert by_id['E']['properties'] == {'owner': 'east-team'}
    for feature in decoded['features']:
        assert feature[BOUNDARY_KEY] == ['B1']


def test_split_packets_are_independently_decodable_and_share_boundary_id(two_parcels):
    result = plan_packets(two_parcels, [1, 2], max_bytes=120)
    assert len(result['packets']) == 2
    for packet in result['packets']:
        standalone = geobuf.decode(packet['data'])
        assert standalone['type'] == 'FeatureCollection'
        for feature in standalone['features']:
            assert feature[BOUNDARY_KEY] == ['B1']
    assert verify_boundaries(result['packets'], result['boundaries'])['ok'] is True
    assert verify_boundaries(result['packets'])['ok'] is True


def test_round_trip_only_rounds_original_points(two_parcels):
    result = plan_packets(two_parcels, [2], max_bytes=10 ** 9)
    decoded = geobuf.decode(result['packets'][0]['data'])
    for original, feature in zip(two_parcels['features'], decoded['features']):
        original_rings = original['geometry']['coordinates']
        decoded_rings = feature['geometry']['coordinates']
        assert len(decoded_rings) == len(original_rings)
        for original_ring, decoded_ring in zip(original_rings, decoded_rings):
            assert len(decoded_ring) == len(original_ring)
            for original_point, decoded_point in zip(original_ring, decoded_ring):
                expected = [round(value, 2) for value in original_point]
                assert decoded_point == expected


def build_five_parcels():
    features = [
        {'type': 'Feature', 'id': 'P0', 'properties': {'name': 'zero'},
         'geometry': rectangle(0.0, 1.0)},
        {'type': 'Feature', 'id': 'P1', 'properties': {'name': 'one', LONG_NAME: 'alpha'},
         'geometry': rectangle(1.0, 2.0)},
        {'type': 'Feature', 'id': 'P2', 'properties': {'name': 'two'},
         'geometry': rectangle(2.0, 3.0, holes=[(2.23, 2.24, 0.31, 0.41)])},
        {'type': 'Feature', 'id': 'P3', 'properties': {'name': 'three', LONG_NAME: 'beta'},
         'geometry': rectangle(10.0, 11.0)},
        {'type': 'Feature', 'id': 'P4', 'properties': {'name': 'four'},
         'geometry': rectangle(20.0, 21.0)},
    ]
    return {'type': 'FeatureCollection', 'features': features}


def test_five_parcel_components_and_shared_key_savings():
    collection = build_five_parcels()
    features = collection['features']
    boundaries = extract_boundaries(features)
    assert [(b['id'], b['parcels']) for b in boundaries] == [('B1', (0, 1)), ('B2', (1, 2))]

    separate = sum(
        len(encode_run(features, i, i + 1, 2, boundaries)) for i in range(len(features))
    )
    combined = len(encode_run(features, 0, len(features), 2, boundaries))
    # merging shares the global key/value tables: sizes are not additive
    assert combined != separate
    assert combined < separate


def test_five_parcel_prefers_minimum_real_bytes_then_mixed_precision():
    collection = build_five_parcels()
    big = plan_packets(collection, [1, 2], max_bytes=10 ** 9)
    assert len(big['packets']) == 1

    limited = plan_packets(collection, [1, 2], max_bytes=200)
    precisions = {item['parcel']: item['precision'] for item in limited['parcel_precisions']}
    # the three parcels linked by continuous shared boundaries must share precision 2
    assert precisions['P0'] == precisions['P1'] == precisions['P2'] == 2
    # unlinked parcels may use the cheaper precision 1
    assert precisions['P3'] == precisions['P4'] == 1
    for packet in limited['packets']:
        assert packet['bytes'] <= 200
    assert verify_boundaries(limited['packets'], limited['boundaries'])['ok'] is True

    all_candidates = []
    for packet in limited['packets']:
        all_candidates.extend(packet['feature_ids'])
    assert all_candidates == ['P0', 'P1', 'P2', 'P3', 'P4']


def test_byte_limit_failure_reports_review_details():
    collection = build_five_parcels()
    with pytest.raises(NoPacketArrangementError) as exc_info:
        plan_packets(collection, [1, 2], max_bytes=40)
    reasons = exc_info.value.reasons
    assert {boundary['id'] for boundary in reasons['boundaries']} == {'B1', 'B2'}
    assert reasons['size_limits']['max_bytes'] == 40
    assert all(value is not None for value in reasons['size_limits']['parcel_min_bytes'].values())


def build_independent_parcels():
    features = []
    for index, label in enumerate('ABC'):
        x0 = 10.0 * index
        features.append({
            'type': 'Feature',
            'id': label,
            'geometry': rectangle(x0, x0 + 1.0),
        })
    return {'type': 'FeatureCollection', 'features': features}


def test_end_position_tie_break_prefers_earlier_packet_end():
    collection = build_independent_parcels()
    features = collection['features']
    boundaries = extract_boundaries(features)
    assert boundaries == []
    cut_early = len(encode_run(features, 0, 1, 1, boundaries)) + \
        len(encode_run(features, 1, 3, 1, boundaries))
    cut_late = len(encode_run(features, 0, 2, 1, boundaries)) + \
        len(encode_run(features, 2, 3, 1, boundaries))
    assert cut_early == cut_late

    result = plan_packets(collection, [1], max_bytes=50)
    assert [packet['end'] for packet in result['packets']] == [1, 3]


def test_empty_collection_returns_no_packets():
    result = plan_packets({'type': 'FeatureCollection', 'features': []}, [1, 2], max_bytes=64)
    assert result == {
        'packets': [],
        'total_bytes': 0,
        'parcel_precisions': [],
        'boundaries': [],
    }


def test_precision_tie_break_orders_smallest_precision_first():
    packets_low = [{'end': 2, 'precision': 1}]
    packets_high = [{'end': 2, 'precision': 2}]
    assert _candidate_key(100, packets_low) < _candidate_key(100, packets_high)


def test_original_entrypoints_keep_default_precision():
    fixture = os.path.join(os.path.dirname(__file__), 'fixtures', 'polygon.json')
    with open(fixture) as handle:
        geojson = json.load(handle)
    encoded = geobuf.encode(geojson)
    message = geobuf.geobuf_pb2.Data()
    message.ParseFromString(encoded)
    assert message.precision == 6
    assert geobuf.decode(encoded) == geojson
    assert Encoder().encode(geojson) == encoded
