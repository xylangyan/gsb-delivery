from copy import deepcopy

import pytest

from deepmerge import Merger, always_merger, conservative_merger, merge_or_raise
from deepmerge.planner import (
    IntervalReport,
    Packet,
    PlanningError,
    ReadEvent,
    Station,
    WorkOrder,
    canonical_value,
    chapter_runs,
)
from deepmerge.trace import MergePolicy, Node, PathRule, TrackedMerger


# ---------------------------------------------------------------------------
# Tracked merger parity with the existing engine
# ---------------------------------------------------------------------------


def _serial(documents, merger=always_merger):
    state = deepcopy(documents[0])
    for document in documents[1:]:
        state = merger.merge(state, deepcopy(document))
    return state


@pytest.mark.parametrize(
    "documents",
    [
        [{"foo": ["bar"]}, {"foo": ["baz"]}],
        [{"a": {"b": 1}}, {"a": {"c": 2}}],
        [{"a": {"b": 1}}, {"a": 7}],
        [{"s": set("ab")}, {"s": set("bc")}],
        [{"l": [1]}, {"l": [1, 2]}],
        [1, "x"],
        [{"device": {"id": "D1"}}, ["D2"], {"device": {"id": "D3"}}],
        [{"l": [{"a": 1}]}, {"l": [{"a": 1}, {"b": 2}]}],
    ],
)
def test_tracked_matches_always_merger(documents):
    tracked = TrackedMerger().reduce_documents(documents).to_value()
    assert tracked == _serial(documents)


def test_tracked_matches_append_unique():
    merger = Merger(
        [(list, "append_unique"), (dict, "merge"), (set, "union")],
        ["override"],
        ["override"],
    )
    policy = MergePolicy([PathRule(list_strategy="append_unique")])
    documents = [
        [{"a": 1}, {"a": 1}],
        [{"a": 1}, {"b": 2}],
        [-1],
        [-2, -2],
    ]
    tracked = TrackedMerger(policy).reduce_documents(documents).to_value()
    assert tracked == _serial(documents, merger)


def test_tracked_matches_conservative_merger():
    documents = [{"a": {"b": 1}}, {"a": {"b": 2}}]
    tracked = TrackedMerger(MergePolicy.conservative()).reduce_documents(documents).to_value()
    assert tracked == _serial(documents, conservative_merger)


def test_tracked_matches_merge_or_raise_on_mergeable_input():
    documents = [{"a": {"b": 1}}, {"a": {"c": 2}}]
    tracked = TrackedMerger().reduce_documents(documents).to_value()
    assert tracked == _serial(documents, merge_or_raise)


@pytest.mark.parametrize(
    "path_rule,documents",
    [
        (PathRule(list_strategy="override"), [{"l": [1]}, {"l": [2, 3]}]),
        (PathRule(list_strategy="prepend"), [{"l": [1]}, {"l": [2, 3]}]),
        (PathRule(set_strategy="intersect"), [{"s": {1, 2}}, {"s": {2, 3}}]),
        (PathRule(type_conflict_strategy="use_existing"), [{"a": {"b": 1}}, {"a": 7}]),
        (PathRule(dict_strategy="override"), [{"a": {"b": 1}}, {"a": {"c": 2}}]),
    ],
)
def test_tracked_matches_path_policies(path_rule, documents):
    merger = Merger(
        [
            (list, path_rule.list_strategy),
            (dict, path_rule.dict_strategy),
            (set, path_rule.set_strategy),
        ],
        [path_rule.fallback_strategy],
        [path_rule.type_conflict_strategy],
    )
    policy = MergePolicy([path_rule])
    assert TrackedMerger(policy).reduce_documents(documents).to_value() == _serial(
        documents, merger
    )


def test_tracked_does_not_alias_inputs():
    base = {"a": {"x": 1}}
    incoming = {"a": {"y": 2}}
    node = TrackedMerger().merge(
        Node.from_value(base, "base"), Node.from_value(incoming, "next")
    )
    base["a"]["z"] = 9
    incoming["a"]["y"] = 99
    assert node.to_value() == {"a": {"x": 1, "y": 2}}


def test_tracked_append_does_not_alias_list_items():
    shared = {"inner": [1]}
    node = TrackedMerger().reduce_documents(
        [{"items": [shared]}, {"items": [shared]}], ["p1", "p2"]
    )
    shared["inner"].append(2)
    assert node.to_value() == {"items": [{"inner": [1]}, {"inner": [1]}]}


def test_tracked_reports_origins():
    node = TrackedMerger().reduce_documents(
        [{"a": {"x": 1, "y": 2}}, {"a": {"y": 3}}, {"a": {"z": 4}}],
        ["p1", "p2", "p3"],
    )
    assert node.origin_map() == {
        ("a", "x"): ("p1",),
        ("a", "y"): ("p2",),
        ("a", "z"): ("p3",),
    }
    assert node.origins_under(("a",)) == ("p1", "p2", "p3")


# ---------------------------------------------------------------------------
# Packet planning
# ---------------------------------------------------------------------------


def _device_order():
    return WorkOrder(
        [
            Packet("1", "A", (0, 10), {"device": {"id": "D1", "cfg": {"a": 1}}, "keep": "p1"}, 1),
            Packet("2", "A", (0, 10), ["D2"], 2),
            Packet("3", "A", (0, 10), {"device": {"id": "D3"}, "extra": "p3"}, 3),
            Packet("4", "B", (0, 10), {"x": 1}, 4),
        ],
        {"blueprint": {}},
        stations=[Station("A", 1), Station("B", 1), Station("hub", 2)],
    )


def test_rejects_the_tempting_late_merge():
    order = _device_order()
    report = order.explain("blueprint", 2, 3)
    assert not report.valid
    assert report.left_group == ("blueprint:blueprint", "1")
    assert report.right_group == ("2", "3")
    described = report.describe()
    assert "keep" in described and "device/cfg" in described
    assert "多看见" in described


def test_earlier_and_later_merges_are_safe():
    order = _device_order()
    assert order.explain("blueprint", 1, 2).valid
    assert order.explain("blueprint", 3, 4).valid
    assert order.explain("blueprint", 1, 3).valid


def test_plan_minimizes_sends_and_verifies():
    order = _device_order()
    plan = order.plan("blueprint")
    assert plan.sends == 3
    assert [(p.start, p.end) for p in plan.dispatch_order()] == [(1, 4)]
    assert order.verify(plan, "blueprint") == []


def test_plan_keeps_a_boundary_when_the_run_must_split():
    packets = [
        Packet("1", "A", (0, 1), {"a": 1}),
        Packet("2", "A", (3, 5), {"b": 2}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.plan("bp")
    assert [(p.start, p.end) for p in plan.dispatch_order()] == [(1, 1), (2, 2)]
    assert order.verify(plan, "bp") == []


def test_plan_respects_the_two_staging_windows():
    packets = [
        Packet("1", "A", (0, 1), {"a": 1}),
        Packet("2", "A", (3, 5), {"b": 2}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    assert not order.explain("bp", 1, 2).valid
    plan = order.plan("bp")
    assert plan.sends == 4
    assert [p.send_at for p in plan.packages(level=1)] == [0, 3]
    assert [p.send_at for p in plan.packages(level=2)] == [1, 4]


def test_plan_reports_window_conflict():
    packets = [
        Packet("1", "A", (0, 0), {"a": 1}),
        Packet("2", "A", (0, 1), {"b": 2}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    with pytest.raises(PlanningError):
        order.plan("bp")


def test_reads_pin_a_boundary():
    packets = [
        Packet("1", "A", (0, 10), {"a": 1}),
        Packet("2", "A", (0, 10), {"b": 2}),
        Packet("3", "A", (0, 10), {"c": 3}),
    ]
    order = WorkOrder(
        packets,
        {"bp": {}},
        stations=[Station("A", 1), Station("hub", 2)],
        reads=[ReadEvent(1, ("a",), 1, "1")],
    )
    assert not order.explain("bp", 1, 3).valid
    plan = order.plan("bp")
    assert [(p.start, p.end) for p in plan.dispatch_order()] == [(1, 1), (2, 3)]
    assert order.verify(plan, "bp") == []


def test_read_expected_value_must_match_serial():
    packets = [Packet("1", "A", (0, 10), {"a": 1})]
    with pytest.raises(ValueError):
        WorkOrder(
            packets,
            {"bp": {}},
            stations=[Station("A", 1), Station("hub", 2)],
            reads=[ReadEvent(1, ("a",), 2)],
        )


def test_robust_plan_is_non_anticipatory():
    packets = [
        Packet("1", "A", (0, 10), {"device": {"id": "D1", "cfg": {"a": 1}}, "keep": "p1"}, 1),
        Packet("2", "A", (0, 10), ["D2"], 2),
        Packet("3", "A", (0, 10), {"device": {"id": "D3"}, "extra": "p3"}, 3),
        Packet("4", "B", (0, 10), {"x": 1}, 4),
    ]
    order = WorkOrder(
        packets,
        {"v1": {"seed": 1}, "v2": {"seed": 2}},
        stations=[Station("A", 1), Station("B", 1), Station("hub", 2)],
        reveal_after=2,
    )
    robust = order.robust_plan(("v1", "v2"))
    assert robust.common.end == 2
    for scenario in ("v1", "v2"):
        assert robust.total_sends(scenario) <= order.size * 2
        assert robust.verify(scenario, order) == []
    assert robust.worst_case_sends == max(
        robust.total_sends("v1"), robust.total_sends("v2")
    )


def test_robust_plan_handles_versions_that_disagree_late():
    packets = [
        Packet("1", "A", (0, 20), {"dev": {"id": "x", "cfg": 1}}),
        Packet("2", "A", (0, 20), ["x"]),
        Packet("3", "A", (0, 20), {"dev": {"id": "y"}}),
        Packet("4", "A", (0, 20), {"dev": {"id": "z", "cfg": 2}}),
    ]
    order = WorkOrder(
        packets,
        {"v1": {"seed": 1}, "v2": {"dev": {"id": "seed"}}},
        stations=[Station("A", 1), Station("hub", 2)],
        reveal_after=2,
    )
    robust = order.robust_plan()
    for scenario in ("v1", "v2"):
        assert robust.verify(scenario, order) == []


def test_tie_break_prefers_earlier_permit():
    packets = [
        Packet("1", "A", (0, 10), {"l": [1]}, permit=5),
        Packet("2", "A", (0, 10), {"l": [2]}, permit=1),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.plan("bp")
    assert plan.dispatch_order()[0].start == 1


def test_chapter_runs_compress_repeats():
    packets = [
        Packet(str(index), "A", (0, 100), {"n": index})
        for index in range(1, 9)
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.plan("bp")
    runs = chapter_runs(plan.packages(level=1))
    assert len(runs) == 1
    assert runs[0].count == 1
    assert runs[0].group_size == 8


def test_chapter_runs_collapse_equal_packages():
    packets = [
        Packet(str(index), "A", (0, 100), {"n": 1})
        for index in range(1, 9)
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.package_each("bp")
    runs = chapter_runs(plan.packages(level=1))
    assert len(runs) == 1
    assert runs[0].count == 8
    assert runs[0].group_size == 1
    assert runs[0].repeated_value


def test_canonical_value_is_type_sensitive():
    assert canonical_value(1) != canonical_value("1")
    assert canonical_value(1) == canonical_value(1)
    assert canonical_value({"a": 1}) == canonical_value({"a": 1})
    assert canonical_value([1, 2]) != canonical_value([2, 1])


def test_interval_report_is_cached_consistently():
    order = _device_order()
    first = order.explain("blueprint", 2, 3)
    second = order.explain("blueprint", 2, 3)
    assert isinstance(first, IntervalReport)
    assert first is second


def test_nested_template_edits_do_not_leak_between_packages():
    template = {"device": {"cfg": {"mode": "a"}}}
    first = deepcopy(template)
    second = deepcopy(template)
    node = TrackedMerger().reduce_documents(
        [{"template": first}, {"template": second}, {"pad": 1}],
        ["p1", "p2", "p3"],
    )
    node.to_value()["template"]["device"]["cfg"]["mode"] = "changed"
    assert second == {"device": {"cfg": {"mode": "a"}}}


def test_merging_two_dicts_shares_no_nested_reference():
    left = {"a": {"b": [1]}}
    right = {"c": {"d": [2]}}
    node = TrackedMerger().merge(
        Node.from_value(left, "p1"), Node.from_value(right, "p2")
    )
    value = node.to_value()
    value["a"]["b"].append(9)
    value["c"]["d"].append(8)
    assert left == {"a": {"b": [1]}}
    assert right == {"c": {"d": [2]}}


def test_type_flip_into_list_is_diagnosed_as_extra_fields():
    packets = [
        Packet("1", "A", (0, 10), {"device": {"id": "a", "cfg": {"m": 1}}}),
        Packet("2", "A", (0, 10), ["b"]),
        Packet("3", "A", (0, 10), {"device": {"id": "c"}}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    report = order.explain("bp", 2, 3)
    assert not report.valid
    described = report.describe()
    assert "device/cfg" in described
    assert "多看见" in described or "少看见" in described


def test_selected_field_origin_is_enforced():
    packets = [
        Packet("1", "A", (0, 10), {"owner": "A", "v": 1}),
        Packet("2", "A", (0, 10), {"owner": "B", "v": 2}),
    ]
    order = WorkOrder(
        packets,
        {"bp": {"owner": "root"}},
        stations=[Station("A", 1), Station("hub", 2)],
        selected_fields=[("owner",)],
    )
    plan = order.plan("bp")
    assert order.verify(plan, "bp") == []
    assert order.serial("bp")[-1].origins_under(("owner",)) == ("2",)


def test_verify_detects_a_fabricated_package():
    packets = [
        Packet("1", "A", (0, 10), {"a": 1}),
        Packet("2", "A", (0, 10), {"b": 2}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.plan("bp")
    honest = plan.dispatch_order()[0]
    broken = type(honest)(
        honest.start,
        honest.end,
        honest.level,
        honest.station,
        honest.window,
        {"a": 1},
        honest.origins,
        honest.permit,
        honest.send_at,
        Node.from_value({"a": 1}, "2"),
    )
    tampered = type(plan)(
        plan.scenario,
        (type(plan.stages[0])(2, "hub", (broken,)),),
        plan.sends,
        plan.start,
        plan.end,
    )
    assert order.verify(tampered, "bp")


def test_approved_initial_config_is_applied_before_packets():
    packets = [Packet("1", "A", (0, 10), {"a": 1})]
    plain = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    configured = WorkOrder(
        packets,
        {"bp": {}},
        stations=[Station("A", 1), Station("hub", 2)],
        initial_config={"seed": "yes"},
    )
    assert "seed" not in plain.serial("bp")[-1].to_value()
    assert configured.serial("bp")[-1].to_value()["seed"] == "yes"
    plan = configured.plan("bp")
    assert configured.verify(plan, "bp") == []


def test_list_conflict_reports_extra_elements():
    packets = [
        Packet("1", "A", (0, 10), {"l": [1, 2]}),
        Packet("2", "A", (0, 10), {"l": {"x": 1}}),
        Packet("3", "A", (0, 10), {"l": [3]}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    report = order.explain("bp", 2, 3)
    assert not report.valid
    kinds = {item.kind for item in report.differences}
    assert "extra" in kinds
    assert any("多看见" in item.describe() for item in report.differences)


def test_robust_plan_when_blueprint_is_known_from_the_start():
    packets = [
        Packet("1", "A", (0, 5), {"a": 1}),
        Packet("2", "A", (0, 5), {"b": 2}),
    ]
    order = WorkOrder(
        packets,
        {"only": {}},
        stations=[Station("A", 1), Station("hub", 2)],
        reveal_after=0,
    )
    robust = order.robust_plan()
    assert robust.common.sends == 0
    assert robust.total_sends("only") == robust.common.sends + robust.branches["only"].sends
    assert robust.verify("only", order) == []


def test_robust_plan_branches_may_differ_by_version():
    packets = [
        Packet("1", "A", (0, 20), {"x": {"v": 1}}),
        Packet("2", "A", (0, 20), {"x": 2}),
        Packet("3", "A", (0, 20), {"x": {"v": 3}}),
        Packet("4", "A", (0, 20), {"x": 4}),
    ]
    order = WorkOrder(
        packets,
        {"left": {}, "right": {"seed": 1}},
        stations=[Station("A", 1), Station("hub", 2)],
        reveal_after=2,
    )
    robust = order.robust_plan()
    for scenario in ("left", "right"):
        assert robust.verify(scenario, order) == []
    # Both versions share the same pre-publication batch.
    assert robust.common.end <= 2


def test_plan_uses_shortest_worst_case_cutoff():
    packets = [
        Packet("1", "A", (0, 1), {"a": 1}),
        Packet("2", "A", (5, 6), {"b": 2}),
        Packet("3", "A", (7, 8), {"c": 3}),
    ]
    order = WorkOrder(
        packets,
        {"v1": {}, "v2": {}},
        stations=[Station("A", 1), Station("hub", 2)],
        reveal_after=3,
    )
    robust = order.robust_plan()
    # Packet 2 cannot move before time 5, so the common batch must stop at 1.
    assert robust.common.end == 1
    assert robust.worst_case_sends == max(
        robust.total_sends("v1"), robust.total_sends("v2")
    )


def test_send_times_are_strictly_increasing_per_station():
    packets = [
        Packet("1", "A", (0, 10), {"a": 1}),
        Packet("2", "A", (0, 10), {"b": 2}),
        Packet("3", "A", (0, 10), {"c": 3}),
    ]
    order = WorkOrder(packets, {"bp": {}}, stations=[Station("A", 1), Station("hub", 2)])
    plan = order.plan("bp")
    times = [p.send_at for p in plan.packages(level=1)]
    assert times == sorted(set(times))
    l2 = [p.send_at for p in plan.packages(level=2)]
    assert l2 == sorted(set(l2))


def test_interval_report_names_both_original_groups_on_read_barrier():
    packets = [
        Packet("1", "A", (0, 10), {"a": 1}),
        Packet("2", "A", (0, 10), {"b": 2}),
        Packet("3", "A", (0, 10), {"c": 3}),
    ]
    order = WorkOrder(
        packets,
        {"bp": {}},
        stations=[Station("A", 1), Station("hub", 2)],
        reads=[ReadEvent(2)],
    )
    report = order.explain("bp", 1, 3)
    assert not report.valid
    assert report.right_group == ("1", "2", "3")
    assert "读取" in report.reason


def test_initial_config_overrides_blueprint_values():
    packets = [Packet("1", "A", (0, 10), {"a": 1})]
    order = WorkOrder(
        packets,
        {"bp": {"owner": "blueprint", "seed": 1}},
        stations=[Station("A", 1), Station("hub", 2)],
        initial_config={"owner": "approved", "extra": True},
    )
    state = order.serial("bp")[-1].to_value()
    assert state == {"owner": "approved", "seed": 1, "extra": True, "a": 1}


def test_equal_value_with_wrong_origin_is_rejected():
    from deepmerge.planner import _diff_nodes

    serial = Node.from_value({"owner": "same"}, "P2")
    planned = Node.from_value({"owner": "same"}, "P1")
    differences = _diff_nodes(serial, planned)
    assert [item.kind for item in differences] == ["origin"]
    assert "原件号" in differences[0].describe()
