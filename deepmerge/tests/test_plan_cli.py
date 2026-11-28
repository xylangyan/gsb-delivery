import json

import pytest

from deepmerge.plan import build_work_order, run


WORK_ORDER = {
    "hub": "hub",
    "reveal_after": 2,
    "scenarios": ["v1", "v2"],
    "stations": [
        {"name": "A", "level": 1},
        {"name": "B", "level": 1},
        {"name": "hub", "level": 2},
    ],
    "blueprints": {
        "v1": {"seed": 1},
        "v2": {"seed": 2},
    },
    "packets": [
        {"origin": "P1", "receiver": "A", "window": [0, 12], "permit": 1, "payload": {"a": 1}},
        {"origin": "P2", "receiver": "A", "window": [1, 13], "permit": 2, "payload": {"b": 2}},
        {"origin": "P3", "receiver": "B", "window": [2, 14], "permit": 3, "payload": {"c": 3}},
    ],
}


def test_cli_robust_plan_has_no_problems():
    result = run(WORK_ORDER)
    assert result["mode"] == "robust"
    for scenario in ("v1", "v2"):
        assert result["plans"][scenario]["problems"] == []
    assert result["common"] is not None


def test_cli_single_mode():
    data = dict(WORK_ORDER, mode="single", scenarios=["v1"])
    result = run(data)
    assert result["mode"] == "single"
    assert result["common"] is None
    assert result["plans"]["v1"]["problems"] == []


def test_cli_path_rules_are_applied():
    data = {
        "stations": [{"name": "A", "level": 1}, {"name": "hub", "level": 2}],
        "blueprints": {"bp": {}},
        "mode": "single",
        "scenarios": ["bp"],
        "path_rules": [{"list": "append_unique"}],
        "packets": [
            {"origin": "P1", "receiver": "A", "window": [0, 5], "payload": {"l": [1]}},
            {"origin": "P2", "receiver": "A", "window": [0, 5], "payload": {"l": [1, 2]}},
        ],
    }
    result = run(data)
    assert result["plans"]["bp"]["problems"] == []
    assert result["plans"]["bp"]["state"] == {"l": [1, 2]}


def test_cli_reads_use_expected_origin():
    data = dict(WORK_ORDER, reads=[{"after": 1, "path": ["a"], "expected_origin": "P1"}])
    order = build_work_order(data)
    assert order.serial("v1")[1].origins_under(("a",)) == ("P1",)


def test_cli_rejects_bad_read_expectation():
    data = dict(WORK_ORDER, reads=[{"after": 1, "path": ["a"], "expected": 99}])
    with pytest.raises(ValueError):
        build_work_order(data)


def test_cli_output_is_json_serializable(tmp_path):
    from deepmerge.plan import main

    path = tmp_path / "wo.json"
    path.write_text(json.dumps(WORK_ORDER), encoding="utf-8")
    assert main([str(path), "--json"]) == 0
