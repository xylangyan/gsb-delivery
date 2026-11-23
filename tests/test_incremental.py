"""Tests for the stable-id incremental survey publisher."""
from __future__ import division

import random

import pytest

import pyvisgraph as vg


def rectangle(polygon_id, x, y, width, height):
    prefix = str(polygon_id)
    return vg.PolygonDef(polygon_id, [
        vg.Vertex(prefix + '-bl', x, y),
        vg.Vertex(prefix + '-br', x + width, y),
        vg.Vertex(prefix + '-tr', x + width, y + height),
        vg.Vertex(prefix + '-tl', x, y + height),
    ])


def assert_matches_full(graph):
    model, routes = graph.rebuild()
    assert graph.edges == model.edges
    assert graph.routes == routes


def assert_replays(report, old_routes, graph):
    replayed = report.replay(old_routes)
    assert replayed == graph.routes


def has_added_edge(report, first, second):
    wanted = {('vertex', first), ('vertex', second)}
    return any(set(edge.key) == wanted for edge in report.vertex_edges_added)


def test_build_and_shortest_path_use_stable_ids():
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', -5, 5), vg.Station('B', 15, 5)])
    assert graph.shortest_path('A', 'B') == [
        'A', 'fence-bl', 'fence-br', 'B']
    assert graph.shortest_path('B', 'A') == [
        'B', 'fence-br', 'fence-bl', 'A']
    assert graph.distance('A', 'B') == graph.distance('B', 'A')


def test_correction_is_atomic_when_invalid():
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', -5, 5), vg.Station('B', 15, 5)])
    before = (dict(graph.polygons), dict(graph.vertices),
              dict(graph.stations), set(graph.edges),
              dict(graph.routes), graph.version)
    with pytest.raises(vg.SurveyError):
        graph.apply(vg.SurveyCorrection(
            polygons_removed=['fence'],
            stations_added=[vg.Station('A', 100, 100)]))
    after = (dict(graph.polygons), dict(graph.vertices),
             dict(graph.stations), set(graph.edges),
             dict(graph.routes), graph.version)
    assert before == after


def test_removing_a_fence_opens_a_far_away_line():
    graph = vg.SurveyGraph(
        [rectangle('west', 0, 0, 10, 10),
         rectangle('east', 30, 0, 10, 10),
         rectangle('screen', 12, -10, 4, 30)],
        [vg.Station('A', -5, 5), vg.Station('B', 45, 5)])
    old_routes = dict(graph.routes)
    old_length = graph.distance('A', 'B')

    report = graph.apply(vg.SurveyCorrection(polygons_removed=['screen']))

    assert has_added_edge(report, 'west-bl', 'east-br') or \
        has_added_edge(report, 'west-br', 'east-bl')
    assert graph.distance('A', 'B') < old_length
    change = [c for c in report.route_changes
              if c.station_pair == ('A', 'B')][0]
    assert change.became_reachable is False
    assert change.new_length < change.old_length
    assert_matches_full(graph)
    assert_replays(report, old_routes, graph)


def test_second_fence_keeps_the_opened_line_blocked():
    graph = vg.SurveyGraph(
        [rectangle('west', 0, 0, 10, 10),
         rectangle('east', 30, 0, 10, 10),
         rectangle('screen', 12, -10, 4, 30),
         rectangle('backstop', 20, -10, 4, 30)],
        [vg.Station('A', -5, 5), vg.Station('B', 45, 5)])
    report = graph.apply(vg.SurveyCorrection(polygons_removed=['screen']))
    assert not has_added_edge(report, 'west-bl', 'east-br')
    assert not has_added_edge(report, 'west-br', 'east-bl')
    route = graph.shortest_route('A', 'B')
    assert route.path != ('A', 'B')
    assert 'screen-bl' not in route.path
    assert_matches_full(graph)


def test_no_route_becomes_route():
    graph = vg.SurveyGraph(
        [rectangle('top', -1, 10, 12, 1),
         rectangle('bottom', -1, -1, 12, 1),
         rectangle('left', -1, -1, 1, 12),
         rectangle('right', 10, -1, 1, 12)],
        [vg.Station('A', 5, 5), vg.Station('B', 30, 5)])
    assert graph.shortest_route('A', 'B') is None
    old_routes = dict(graph.routes)

    report = graph.apply(vg.SurveyCorrection(polygons_removed=['right']))

    assert graph.shortest_route('A', 'B') is not None
    change = [c for c in report.route_changes
              if c.station_pair == ('A', 'B')][0]
    assert change.became_reachable
    assert change.old_path is None
    assert_replays(report, old_routes, graph)
    assert_matches_full(graph)


def test_equal_length_routes_prefer_stable_vertex_ids():
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', 5, -5), vg.Station('B', 5, 15)])
    route = graph.shortest_route('A', 'B')
    assert route.path == ('A', 'fence-bl', 'fence-tl', 'B')

    # Renaming the competing corners changes only the tie-break order, not
    # the length of either route.
    renamed = vg.SurveyGraph(
        [vg.PolygonDef('fence', [
            vg.Vertex('m', 0, 0), vg.Vertex('n', 10, 0),
            vg.Vertex('o', 10, 10), vg.Vertex('b', 0, 10)])],
        [vg.Station('A', 5, -5), vg.Station('B', 5, 15)])
    assert renamed.distance('A', 'B') == pytest.approx(route.length)
    assert renamed.shortest_path('A', 'B') == ['A', 'm', 'b', 'B']


def test_station_move_updates_only_local_state():
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', -5, 5), vg.Station('B', 15, 5),
         vg.Station('C', 15, 15)])
    old_routes = dict(graph.routes)
    report = graph.apply(vg.SurveyCorrection(
        stations_updated={'C': (15, -5)}))
    assert report.reevaluated_pairs < 64
    assert_matches_full(graph)
    assert_replays(report, old_routes, graph)


def test_vertex_move_keeps_shared_geometry_consistent():
    graph = vg.SurveyGraph(
        [vg.PolygonDef('left', [
            vg.Vertex('v1', 0, 0), vg.Vertex('shared1', 10, 0),
            vg.Vertex('v2', 10, 10), vg.Vertex('shared2', 0, 10)]),
         vg.PolygonDef('right', [
            vg.Vertex('shared1', 10, 0), vg.Vertex('w1', 20, 0),
            vg.Vertex('w2', 20, 10), vg.Vertex('shared2', 0, 10)])],
        [vg.Station('A', 5, -5), vg.Station('B', 15, 15)])
    old_routes = dict(graph.routes)
    report = graph.apply(vg.SurveyCorrection(
        vertices_updated={'shared2': (0, 15)}))
    assert graph.vertices['shared2'] == (0, 15)
    assert_matches_full(graph)
    assert_replays(report, old_routes, graph)


def test_save_load_round_trip(tmp_path):
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', -5, 5), vg.Station('B', 15, 5)])
    graph.apply(vg.SurveyCorrection(
        stations_added=[vg.Station('C', 15, 15)]))
    path = str(tmp_path / 'survey.pkl')
    graph.save(path)
    loaded = vg.SurveyGraph().load(path)
    assert loaded.polygons == graph.polygons
    assert loaded.vertices == graph.vertices
    assert loaded.stations == graph.stations
    assert loaded.edges == graph.edges
    assert loaded.routes == graph.routes
    assert loaded.version == graph.version


def test_random_corrections_match_full_rebuild():
    random.seed(20261124)
    graph = vg.SurveyGraph(
        [rectangle('p0', 0, 0, 10, 10),
         rectangle('p1', 30, 0, 10, 10),
         rectangle('p2', 0, 30, 10, 10)],
        [vg.Station('s0', -5, 15), vg.Station('s1', 20, 20),
         vg.Station('s2', 45, 15)])

    for step in range(80):
        kind = random.choice(['add', 'remove', 'move', 'move_station'])
        if kind == 'add':
            pid = 'new-%d' % step
            graph.apply(vg.SurveyCorrection(polygons_added=[
                rectangle(pid, random.choice([-30, 20, 45]),
                          random.choice([-30, 20, 45]), 8, 8)]))
            continue
        if kind == 'remove':
            pid = random.choice(sorted(graph.polygons))
            if len(graph.polygons) == 1:
                continue
            correction = vg.SurveyCorrection(polygons_removed=[pid])
        elif kind == 'move':
            pid = random.choice(sorted(graph.polygons))
            vertices = []
            for vid in graph.polygons[pid]:
                x, y = graph.vertices[vid]
                vertices.append(vg.Vertex(
                    vid, x + random.uniform(-2, 2),
                    y + random.uniform(-2, 2)))
            correction = vg.SurveyCorrection(
                polygons_updated=[vg.PolygonDef(pid, vertices)])
        else:
            sid = random.choice(sorted(graph.stations))
            x, y = graph.stations[sid]
            correction = vg.SurveyCorrection(
                stations_updated={sid: (x + random.uniform(-4, 4),
                                        y + random.uniform(-4, 4))})
        old_routes = dict(graph.routes)
        report = graph.apply(correction)
        assert_matches_full(graph)
        assert_replays(report, old_routes, graph)


def test_report_serialisation_contains_both_edge_kinds():
    graph = vg.SurveyGraph(
        [rectangle('fence', 0, 0, 10, 10)],
        [vg.Station('A', -5, 5), vg.Station('B', 15, 5)])
    report = graph.apply(vg.SurveyCorrection(polygons_removed=['fence']))
    data = report.to_dict()
    assert data['base_version'] == 1
    assert data['version'] == 2
    assert data['removed_edges']
    assert data['vertex_edges_removed']
    assert data['station_edges_removed']
    assert all('node1' in edge for edge in data['removed_edges'])
    assert all('new_path' in change for change in data['route_changes'])
    assert report.removed_edge_keys == set(
        edge.key for edge in report.removed_edges)
    assert set(edge.key for edge in report.vertex_edges_removed) | \
        set(edge.key for edge in report.station_edges_removed) == \
        report.removed_edge_keys
