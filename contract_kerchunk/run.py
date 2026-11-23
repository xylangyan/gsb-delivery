"""Build the scenario, plan at minimum cost, publish and verify."""
from __future__ import annotations

import json
import os
import sys

from .planner import plan
from .publisher import publish
from .verifier import verify
from .optimality import audit


def main(out_dir: str | None = None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = out_dir or os.path.join(here, "out")
    manifest, plan_doc = plan(out_dir)
    publish(manifest, plan_doc, out_dir)
    report = verify(out_dir)
    optimality = audit(out_dir)
    with open(os.path.join(out_dir, "optimality.json"), "w") as fh:
        json.dump(optimality, fh, indent=2)
    print(json.dumps({
        "total_fee": plan_doc["total_fee"],
        "minimum_fee": optimality["minimum_fee"],
        "unique_forced_ranges": optimality["unique_forced_ranges"],
        "optimal": optimality["optimal"],
        "paid_ranges": plan_doc["paid_ranges"],
        "infeasible": plan_doc["infeasible"],
        "checks_passed": sum(c["passed"] for c in report["checks"]),
        "checks_total": len(report["checks"]),
        "ok": report["ok"] and optimality["optimal"],
    }, indent=2))
    return 0 if report["ok"] and optimality["optimal"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
