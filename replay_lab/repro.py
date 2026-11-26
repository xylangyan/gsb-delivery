from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from replay_lab.core import Divergence, PlanEvaluation


def _json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def build_repro_script(
    case: Dict[str, Any],
    plan: Dict[str, Any],
    evaluation: PlanEvaluation,
) -> str:
    divergence: Divergence = evaluation.divergence
    case_path = Path(case["_path"]).resolve()
    repo_root = case_path.parents[1] if case_path.parent.name == "replay_cases" else case_path.parent
    divergence_json = {
        "schedule": divergence.schedule,
        "job": divergence.job,
        "ordinal": divergence.ordinal,
        "global_index": divergence.global_index,
        "kind": divergence.kind,
        "expected": divergence.expected,
        "actual": divergence.actual,
        "message": divergence.message,
    }

    return f'''#!/usr/bin/env python3
"""Reproduce the earliest replay-package divergence with requests-mock."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if not (REPO_ROOT / "replay_lab").exists():
    REPO_ROOT = Path({str(repo_root)!r})
sys.path.insert(0, str(REPO_ROOT))

from replay_lab.core import evaluate_plan, load_case, run_schedule

CASE_PATH = REPO_ROOT / "replay_cases" / {case_path.name!r}
if not CASE_PATH.exists():
    CASE_PATH = Path({str(case_path)!r})
PLAN_ID = {plan["id"]!r}
BASELINE_PLAN_ID = {case["baseline_plan_id"]!r}
SCHEDULE = {divergence.schedule!r}
DIVERGENCE = json.loads({_json_block(divergence_json)!r})


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
        print(marker, json.dumps({{
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
        }}, ensure_ascii=False, sort_keys=True))

    target = next(event for event in events if event["global_index"] == target_index)
    prior = events[target_index - 2] if target_index >= 2 else None
    print("\\nearliest divergence:", DIVERGENCE["message"])
    print("failing request:", target["method"], target["url"])
    if prior is not None:
        print("causing receipt:", json.dumps({{
            "original_id": prior.get("original_id"),
            "url": prior.get("url"),
            "json": prior.get("json"),
        }}, ensure_ascii=False, sort_keys=True))
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
        print("\\nThe proposed registry unexpectedly passed all schedules.")
        return 0

    raise AssertionError(
        "{{}} at global event {{}}".format(
            candidate.divergence.message,
            candidate.divergence.global_index,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_repro_script(
    output_path: str | Path,
    case: Dict[str, Any],
    plan: Dict[str, Any],
    evaluation: PlanEvaluation,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_repro_script(case, plan, evaluation), encoding="utf-8")
    path.chmod(0o755)
    return path
