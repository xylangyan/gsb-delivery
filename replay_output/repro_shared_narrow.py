#!/usr/bin/env python3
"""Reproduce the earliest replay-package divergence with requests-mock."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if not (REPO_ROOT / "replay_lab").exists():
    REPO_ROOT = Path('/Users/guo/Documents/GSB/1109/batches/accounts55-bao-20261127/control/questions/GSB1109-20261122-BAO-REQUESTS-MOCK-01/workspace')
sys.path.insert(0, str(REPO_ROOT))

from replay_lab.core import evaluate_plan, load_case, run_schedule

CASE_PATH = REPO_ROOT / "replay_cases" / 'supply_chain.json'
if not CASE_PATH.exists():
    CASE_PATH = Path('/Users/guo/Documents/GSB/1109/batches/accounts55-bao-20261127/control/questions/GSB1109-20261122-BAO-REQUESTS-MOCK-01/workspace/replay_cases/supply_chain.json')
PLAN_ID = 'shared-narrow'
BASELINE_PLAN_ID = 'original-heavy'
SCHEDULE = 'BBBBAAAA'
DIVERGENCE = json.loads('{\n  "actual": {\n    "attempt": 2,\n    "exception": null,\n    "method": "GET",\n    "original_id": "ORIG_DETAIL_A",\n    "status": 200,\n    "step": "detail",\n    "url": "http://supply-test.local/detail?line=B&station=ST-7&station=ST-7"\n  },\n  "expected": {\n    "attempt": 2,\n    "exception": null,\n    "method": "GET",\n    "original_id": "ORIG_DETAIL_B",\n    "status": 200,\n    "step": "detail",\n    "url": "http://supply-test.local/detail?line=B&station=ST-7&station=ST-7"\n  },\n  "global_index": 4,\n  "job": "beta",\n  "kind": "trace",\n  "message": "job beta step #4 differs",\n  "ordinal": 3,\n  "schedule": "BBBBAAAA"\n}')


def main():
    case = load_case(CASE_PATH)
    baseline_plan = next(p for p in case["plans"] if p["id"] == BASELINE_PLAN_ID)
    plan = next(p for p in case["plans"] if p["id"] == PLAN_ID)

    baseline = evaluate_plan(case, baseline_plan)
    if not baseline.equivalent:
        raise AssertionError("baseline registry is not schedule-stable")

    events, branches = run_schedule(case, plan, SCHEDULE)
    print("schedule:", SCHEDULE)
    print("branch state:", json.dumps(branches, ensure_ascii=False, sort_keys=True))

    target_index = DIVERGENCE["global_index"]
    for event in events:
        marker = ">>" if event["global_index"] == target_index else "  "
        print(marker, json.dumps({
            "global_index": event["global_index"],
            "job": event["job"],
            "step": event["step"],
            "attempt": event["attempt"],
            "method": event["method"],
            "url": event["url"],
            "status": event["status"],
            "exception": event["exception"],
            "original_id": event["original_id"],
            "consumed": event["consumed"],
        }, ensure_ascii=False, sort_keys=True))

    target = next(event for event in events if event["global_index"] == target_index)
    prior = events[target_index - 2] if target_index >= 2 else None
    print("\nearliest divergence:", DIVERGENCE["message"])
    print("failing request:", target["method"], target["url"])
    if prior is not None:
        print("causing receipt:", json.dumps({
            "original_id": prior.get("original_id"),
            "url": prior.get("url"),
            "json": prior.get("json"),
        }, ensure_ascii=False, sort_keys=True))
    print("consumption:", json.dumps(target.get("consumed"), ensure_ascii=False, sort_keys=True))
    print("expected:", json.dumps(DIVERGENCE["expected"], ensure_ascii=False, sort_keys=True))
    print("actual:", json.dumps(DIVERGENCE["actual"], ensure_ascii=False, sort_keys=True))

    candidate = evaluate_plan(
        case,
        plan,
        baseline.job_signatures,
        baseline.branches,
    )
    if candidate.equivalent:
        print("\nThe proposed registry unexpectedly passed all schedules.")
        return 0

    raise AssertionError(
        "{} at global event {}".format(
            candidate.divergence.message,
            candidate.divergence.global_index,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
