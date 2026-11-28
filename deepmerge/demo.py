"""Executable demo for the provenance-aware merge planner.

Run with::

    python -m deepmerge.demo
    python -m deepmerge.demo --json

It prints, for a small shift:

* the serial single-packet order and its final warehouse state;
* the planned order and its final warehouse state, which must be identical;
* one deliberately rejected late merge, with the paths and packet groups that
  would change what the warehouse sees;
* a two-station handoff where station A merges first, then station B continues
  after the published blueprint switches from ``v1`` to ``v2``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .planner import Packet, Station, WorkOrder, canonical_value


def _device_packets() -> list[Packet]:
    return [
        Packet(
            "P1",
            "A",
            (0, 12),
            {"device": {"id": "d1", "cfg": {"mode": "a"}}, "batch": ["1"], "owner": "A"},
            permit=1,
        ),
        Packet("P2", "A", (1, 13), {"device": ["d2"], "batch": ["2"], "owner": "A"}, permit=2),
        Packet(
            "P3", "B", (2, 14),
            {"device": {"id": "d3"}, "batch": ["3"], "owner": "B"},
            permit=3,
        ),
        Packet(
            "P4",
            "B",
            (3, 15),
            {"device": {"id": "d4", "cfg": {"mode": "b"}}, "batch": ["4"], "owner": "B"},
            permit=4,
        ),
    ]


def build_demo() -> dict[str, Any]:
    packets = _device_packets()
    blueprints = {
        "v1": {"device": {"id": "root", "cfg": {"mode": "root"}}, "batch": [], "owner": "root"},
        "v2": {"device": {"id": "root", "cfg": {"mode": "v2"}}, "batch": [], "owner": "root"},
    }
    order = WorkOrder(
        packets,
        blueprints,
        stations=[Station("A", 1), Station("B", 1), Station("hub", 2)],
        reveal_after=2,
        selected_fields=[("owner",)],
    )
    serial_single = order.package_each("v1")
    plan_v1 = order.plan("v1")
    rejected = order.explain("v1", 2, 3)
    robust = order.robust_plan(("v1", "v2"))
    return {
        "order": order,
        "packets": packets,
        "serial_single": serial_single,
        "plan_v1": plan_v1,
        "rejected": rejected,
        "robust": robust,
    }


def render_human(demo: dict[str, Any]) -> str:
    order: WorkOrder = demo["order"]
    serial_single = demo["serial_single"]
    plan_v1 = demo["plan_v1"]
    rejected = demo["rejected"]
    robust = demo["robust"]
    lines: list[str] = []

    lines.append("== 小样：单包串行 vs 上游并包计划 ==")
    serial_state = order.serial("v1")[-1].to_value()
    planned_state = plan_v1.dispatch_order()[-1].node.to_value()
    lines.append("串行单包顺序（逐包发送）：")
    for package in serial_single.dispatch_order():
        lines.append(f"  第{package.start}包 原件{package.origins} @{package.send_at}")
    lines.append(f"串行入库: {serial_state!r}")
    lines.append("计划顺序（中转合并后发送）：")
    for package in plan_v1.dispatch_order():
        lines.append(
            f"  第{package.start}–{package.end}包 原件{package.origins} @{package.send_at}"
        )
    lines.append(f"计划入库: {planned_state!r}")
    lines.append(f"两者相同: {canonical_value(serial_state) == canonical_value(planned_state)}")
    lines.append(f"计划自检: {order.verify(plan_v1, 'v1') or '通过'}")

    lines.append("")
    lines.append("== 被拒绝的晚两包合并 ==")
    lines.append(rejected.describe())

    lines.append("")
    lines.append("== 甲乙交接：甲先站合并，乙中途切换蓝本 ==")
    lines.append(robust.describe())
    for scenario in robust.scenarios:
        problems = robust.verify(scenario, order)
        lines.append(f"公布为 {scenario} 的整班自检: {problems or '通过'}")
        lines.append(f"  整班入库: {order.serial(scenario)[-1].to_value()!r}")
    return "\n".join(lines)


def render_json(demo: dict[str, Any]) -> str:
    order: WorkOrder = demo["order"]
    serial_single = demo["serial_single"]
    plan_v1 = demo["plan_v1"]
    rejected = demo["rejected"]
    robust = demo["robust"]

    def package_view(package):
        return {
            "start": package.start,
            "end": package.end,
            "level": package.level,
            "station": package.station,
            "send_at": package.send_at,
            "origins": list(package.origins),
        }

    payload = {
        "serial_single": {
            "sends": serial_single.sends,
            "packages": [package_view(p) for p in serial_single.dispatch_order()],
            "state": order.serial("v1")[-1].to_value(),
        },
        "plan_v1": {
            "sends": plan_v1.sends,
            "packages": [package_view(p) for p in plan_v1.dispatch_order()],
            "state": plan_v1.dispatch_order()[-1].node.to_value(),
            "problems": order.verify(plan_v1, "v1"),
        },
        "rejected": {
            "start": rejected.start,
            "end": rejected.end,
            "valid": rejected.valid,
            "left_group": list(rejected.left_group),
            "right_group": list(rejected.right_group),
            "differences": [
                {
                    "path": list(item.path),
                    "kind": item.kind,
                    "serial": _jsonable(item.serial),
                    "planned": _jsonable(item.planned),
                    "serial_origins": list(item.serial_origins),
                    "planned_origins": list(item.planned_origins),
                    "text": item.describe(),
                }
                for item in rejected.differences
            ],
        },
        "robust": {
            "reveal_after": robust.reveal_after,
            "common_sends": robust.common.sends,
            "worst_case_sends": robust.worst_case_sends,
            "branches": {
                name: {
                    "total_sends": robust.total_sends(name),
                    "problems": robust.verify(name, order),
                    "state": order.serial(name)[-1].to_value(),
                }
                for name in robust.scenarios
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=repr)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted((_jsonable(v) for v in value), key=repr)
    return repr(value)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    demo = build_demo()
    if "--json" in argv:
        print(render_json(demo))
    else:
        print(render_human(demo))
    order: WorkOrder = demo["order"]
    failures = order.verify(demo["plan_v1"], "v1")
    failures += demo["robust"].verify("v1", order)
    failures += demo["robust"].verify("v2", order)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
