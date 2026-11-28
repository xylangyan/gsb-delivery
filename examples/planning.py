"""Runnable example: serial single-packet order vs. an upstream merge plan.

Run with ``python examples/planning.py``.
"""

from deepmerge.planner import Packet, Station, WorkOrder


def main() -> int:
    packets = [
        Packet(
            "P1", "A", (0, 12),
            {"device": {"id": "d1", "cfg": {"mode": "a"}}, "batch": ["1"], "owner": "A"},
            permit=1,
        ),
        Packet("P2", "A", (1, 13), {"device": ["d2"], "batch": ["2"], "owner": "A"}, permit=2),
        Packet("P3", "B", (2, 14), {"device": {"id": "d3"}, "batch": ["3"], "owner": "B"}, permit=3),
        Packet(
            "P4", "B", (3, 15),
            {"device": {"id": "d4", "cfg": {"mode": "b"}}, "batch": ["4"], "owner": "B"},
            permit=4,
        ),
    ]
    order = WorkOrder(
        packets,
        {
            "v1": {"device": {"id": "root", "cfg": {"mode": "root"}}, "batch": [], "owner": "root"},
            "v2": {"device": {"id": "root", "cfg": {"mode": "v2"}}, "batch": [], "owner": "root"},
        },
        stations=[Station("A", 1), Station("B", 1), Station("hub", 2)],
        reveal_after=2,
        selected_fields=[("owner",)],
    )
    print("serial single-packet order:", order.serial("v1")[-1].to_value())
    print("planned order result:      ", order.plan("v1").dispatch_order()[-1].node.to_value())
    print()
    print(order.plan("v1").describe(compressed=True))
    print()
    print(order.explain("v1", 2, 3).describe())
    print()
    print(order.robust_plan(("v1", "v2")).describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
