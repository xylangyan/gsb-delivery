import json

import pyvisgraph as vg
from pyvisgraph.handoff import MapModel, solve_handoff, verify_line, write_deliverables


def middle_shortcut_scenario():
    obstacle = [
        vg.Point(-1, 4),
        vg.Point(1, 4),
        vg.Point(1, 6),
        vg.Point(-1, 6),
    ]
    line = ((-10.0, 0.0), (10.0, 0.0))
    target = vg.Point(0.0, 10.0)
    return [obstacle], line, target


def test_identical_map_covers_whole_line():
    polygons, line, target = middle_shortcut_scenario()
    report = verify_line(polygons, [polygons[0][:]], line, target)
    assert report["compliant"] is True


def test_endpoint_checks_miss_middle_shortcut():
    polygons, line, target = middle_shortcut_scenario()
    original = MapModel(polygons)
    candidate = MapModel([])

    start = vg.Point(line[0][0], line[0][1])
    end = vg.Point(line[1][0], line[1][1])
    for point in (start, end):
        old_distance = original.evaluate_point(point, target)["distance"]
        new_distance = candidate.evaluate_point(point, target)["distance"]
        assert abs(old_distance - new_distance) < 1e-8

    report = verify_line(original, candidate, line, target)
    assert report["compliant"] is False
    witness = report["witness"]
    assert 0.05 < witness["parameter"] < 0.95
    assert witness["original_distance"] - witness["candidate_distance"] > 1e-3


def test_solve_handoff_writes_reopenable_contours(tmp_path):
    handoff = {
        "source": "unit-test-original",
        "original": [[
            {"x": -1, "y": 4},
            {"x": 1, "y": 4},
            {"x": 1, "y": 6},
            {"x": -1, "y": 6},
        ]],
        "obstacles": [{
            "id": "O1",
            "source": "original.shp#0",
            "allow_omit": True,
            "choices": [{
                "id": "public-square",
                "source": "allowed/A#0",
                "polygon": [[-1, 4], [1, 4], [1, 6], [-1, 6]],
            }],
        }],
        "inspections": [{
            "id": "L1",
            "start_line": [[-10, 0], [10, 0]],
            "target": [0, 10],
        }],
    }

    result = solve_handoff(handoff)
    assert result["compliant"] is True
    assert result["vertex_count"] == 4
    files = write_deliverables(result, str(tmp_path))

    with open(files["contours"], encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["provenance"][0]["source"] == "allowed/A#0"

    reopened = vg.VisGraph()
    reopened.load(files["graph"])
    assert len(reopened.graph.get_points()) == 4
