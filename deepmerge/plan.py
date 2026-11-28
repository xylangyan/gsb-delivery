"""Command-line entry point for the merge planner.

Usage::

    python -m deepmerge.plan work_order.json
    python -m deepmerge.plan work_order.json --json

The input JSON looks like::

    {
      "hub": "hub",
      "reveal_after": 2,
      "scenarios": ["v1", "v2"],
      "selected_fields": [["owner"]],
      "initial_config": {"seed": true},
      "stations": [{"name": "A", "level": 1}, {"name": "B", "level": 1}],
      "blueprints": {"v1": {...}, "v2": {...}},
      "packets": [
        {"origin": "P1", "receiver": "A", "window": [0, 12],
         "payload": {...}, "permit": 1}
      ],
      "reads": [
        {"after": 1, "path": ["owner"], "expected": "A", "expected_origin": "P1"}
      ]
    }

The output is a dispatch order per station, plus the warehouse boundary check
for every published version.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .planner import UNSET, Packet, PlanningError, ReadEvent, Station, WorkOrder
from .trace import MergePolicy, PathRule


def build_work_order(data: dict[str, Any]) -> WorkOrder:
    path_rules = []
    for rule in data.get("path_rules", []):
        path_rules.append(
            PathRule(
                prefix=tuple(rule.get("prefix", [])),
                dict_strategy=rule.get("dict", "merge"),
                list_strategy=rule.get("list", "append"),
                set_strategy=rule.get("set", "union"),
                fallback_strategy=rule.get("fallback", "override"),
                type_conflict_strategy=rule.get("type_conflict", "override"),
            )
        )
    policy = MergePolicy(path_rules) if path_rules else MergePolicy.always()
    stations = [
        Station(entry["name"], int(entry["level"]))
        for entry in data.get("stations", [])
    ] or None
    packets = [
        Packet(
            origin=entry["origin"],
            receiver=entry["receiver"],
            window=tuple(entry["window"]),
            payload=entry["payload"],
            permit=int(entry.get("permit", 0)),
        )
        for entry in data["packets"]
    ]
    reads = []
    for entry in data.get("reads", []):
        reads.append(
            ReadEvent(
                after=int(entry["after"]),
                path=tuple(entry.get("path", [])),
                expected=entry["expected"] if "expected" in entry else UNSET,
                expected_origin=entry.get("expected_origin"),
                reader=entry.get("reader", ""),
            )
        )
    return WorkOrder(
        packets,
        data["blueprints"],
        policy=policy,
        stations=stations,
        reads=reads,
        selected_fields=[tuple(path) for path in data.get("selected_fields", [])],
        reveal_after=data.get("reveal_after"),
        initial_config=data.get("initial_config"),
        hub=data.get("hub", "hub"),
    )


def run(data: dict[str, Any]) -> dict[str, Any]:
    order = build_work_order(data)
    scenarios = data.get("scenarios") or list(order.blueprints)
    mode = data.get("mode", "robust")
    if mode == "single":
        scenario = scenarios[0]
        plan = order.plan(scenario)
        plans = {scenario: plan}
        worst = plan.sends
        common = None
    else:
        robust = order.robust_plan(scenarios)
        plans = {name: robust.branches[name] for name in scenarios}
        worst = robust.worst_case_sends
        common = robust.common
    payload: dict[str, Any] = {
        "mode": mode,
        "worst_case_sends": worst,
        "common": _plan_view(common) if common is not None else None,
        "plans": {
            name: {
                **_plan_view(plans[name]),
                "problems": order.verify(plans[name], name)
                if plans[name].end >= plans[name].start
                else [],
                "state": order.serial(name)[-1].to_value()
                if plans[name].end >= plans[name].start
                else None,
            }
            for name in scenarios
        },
    }
    return payload


def _plan_view(plan) -> dict[str, Any]:
    return {
        "sends": plan.sends,
        "start": plan.start,
        "end": plan.end,
        "packages": [
            {
                "start": package.start,
                "end": package.end,
                "level": package.level,
                "station": package.station,
                "send_at": package.send_at,
                "origins": list(package.origins),
                "permit": package.permit,
            }
            for package in plan.packages()
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan upstream packet merges.")
    parser.add_argument("work_order", help="path to a JSON work order, or - for stdin")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    if args.work_order == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.work_order, encoding="utf-8") as handle:
            data = json.load(handle)
    try:
        result = run(data)
    except PlanningError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=repr))
    else:
        print(_render(result))
    return 0 if not any(plan["problems"] for plan in result["plans"].values()) else 1


def _render(result: dict[str, Any]) -> str:
    lines = [f"模式: {result['mode']}, 整班最坏发送: {result['worst_case_sends']}"]
    if result["common"] is not None:
        lines.append("公布前公共批次:")
        lines.extend(_render_plan(result["common"]))
    for name, plan in result["plans"].items():
        lines.append(f"公布为 {name}: 发送 {plan['sends']} 次")
        lines.extend(_render_plan(plan))
        if plan["problems"]:
            lines.append("  自检问题:")
            lines.extend(f"    {problem}" for problem in plan["problems"])
        else:
            lines.append("  自检: 通过")
    return "\n".join(lines)


def _render_plan(plan: dict[str, Any]) -> list[str]:
    lines = []
    for package in plan["packages"]:
        lines.append(
            f"  第{package['start']}–{package['end']}包 "
            f"第{package['level']}级站{package['station']} "
            f"@{package['send_at']} 原件{tuple(package['origins'])}"
        )
    return lines


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
