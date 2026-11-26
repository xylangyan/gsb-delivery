import json
import subprocess
import sys
from pathlib import Path

from replay_lab.cli import main
from replay_lab.core import certify_minimal, evaluate_plan, load_case
from replay_lab.graph import explore_canonical_graph
from replay_lab.repro import build_repro_script

ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "replay_cases" / "supply_chain.json"


def load_reference_case():
    return load_case(CASE_PATH)


def plan_by_id(case, plan_id):
    return next(plan for plan in case["plans"] if plan["id"] == plan_id)


def test_approved_min_matches_baseline_every_schedule():
    case = load_reference_case()
    baseline = evaluate_plan(case, plan_by_id(case, "original-heavy"))
    assert baseline.equivalent

    result = evaluate_plan(
        case,
        plan_by_id(case, "approved-min"),
        baseline.job_signatures,
        baseline.branches,
    )

    assert result.equivalent
    assert result.schedule_count == 70
    assert result.cost_bytes == 94380
    assert result.response_item_count == 8
    assert result.missing_required == []


def test_selected_plan_is_local_minimum():
    case = load_reference_case()
    baseline = evaluate_plan(case, plan_by_id(case, "original-heavy"))
    selected = plan_by_id(case, "approved-min")

    checks = certify_minimal(
        case,
        selected,
        baseline.job_signatures,
        baseline.branches,
    )

    assert checks
    assert all(check["necessary"] for check in checks)


def test_shared_stateful_registry_fails_at_fourth_event():
    case = load_reference_case()
    baseline = evaluate_plan(case, plan_by_id(case, "original-heavy"))
    result = evaluate_plan(
        case,
        plan_by_id(case, "shared-narrow"),
        baseline.job_signatures,
        baseline.branches,
    )

    assert not result.equivalent
    divergence = result.divergence
    assert divergence.schedule == "BBBBAAAA"
    assert divergence.global_index == 4
    assert divergence.job == "beta"
    assert divergence.expected["original_id"] == "ORIG_DETAIL_B"
    assert divergence.actual["original_id"] == "ORIG_DETAIL_A"

    event = divergence.events[3]
    consumed = event["consumed"][0]
    assert consumed["registration"] == "detail-shared-sequence"
    assert consumed["position"] == 1
    assert consumed["remaining_item_ids"] == ["shared-ok-b"]


def test_canonical_graph_merges_equivalent_states():
    case = load_reference_case()
    result = explore_canonical_graph(case, plan_by_id(case, "approved-min"))
    assert result["state_count"] == 25
    assert set(result["terminal_branches"]) == {
        "alpha-manifest",
        "beta-supplement",
    }


def test_generated_repro_is_runnable(tmp_path):
    case = load_reference_case()
    baseline = evaluate_plan(case, plan_by_id(case, "original-heavy"))
    result = evaluate_plan(
        case,
        plan_by_id(case, "shared-narrow"),
        baseline.job_signatures,
        baseline.branches,
    )
    script = tmp_path / "repro.py"
    script.write_text(
        build_repro_script(case, plan_by_id(case, "shared-narrow"), result),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "earliest divergence" in completed.stdout
    assert "detail-shared-sequence" in completed.stdout
    assert "remaining_item_ids" in completed.stdout


def test_cli_select_writes_package_and_report(tmp_path):
    output = tmp_path / "replay"
    exit_code = main(["select", "--out", str(output)])

    assert exit_code == 0
    selected = json.loads((output / "selected_package.json").read_text())
    assert selected["selection"]["selected_plan_id"] == "approved-min"
    assert (output / "selection_report.md").exists()
    assert (output / "repro_shared_narrow.py").exists()
