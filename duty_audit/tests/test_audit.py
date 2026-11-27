"""Regression tests for the read-only duty audit engine.

These tests pin the hand-derived numbers and cross-check that the engine
rejects histories at the first mismatching record. A second test invokes the
real-ADTK cross-check script as a subprocess so engine semantics stay aligned
with ADTK 0.6.2 in this offline environment.
"""

import copy
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DUTY_DIR = os.path.dirname(HERE)
sys.path.insert(0, DUTY_DIR)

import audit  # noqa: E402


@pytest.fixture
def case():
    with open(os.path.join(DUTY_DIR, "case_20261127.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_checkpoint_bounds_match_hand_calculation(case):
    distinct = audit.build_checkpoint(
        [audit.BranchSpec(**spec) for spec in case["branches"]],
        case["wirings"][0],
    )
    shared = audit.build_checkpoint(
        [audit.BranchSpec(**spec) for spec in case["branches"]],
        case["wirings"][1],
    )
    assert distinct.regression["A"].mean == 10.0
    assert distinct.regression["A"].bound == 1.0
    assert distinct.regression["B"].mean == 20.0
    assert distinct.regression["B"].bound == 2.0
    assert distinct.quantile["C"].bound == 1.0

    assert shared.regression["A"].mean == 20.0
    assert shared.regression["B"].mean == 20.0
    assert shared.regression["A"].bound == 1.0
    assert shared.regression["B"].bound == 2.0


def test_six_consistent_and_two_rejected_histories(case):
    report = audit.audit(case)
    assert len(report["consistent_histories"]) == 6
    assert len(report["rejected_histories"]) == 2
    for hist in report["rejected_histories"]:
        assert hist["choices"]["wiring"] == "distinct"
        assert hist["choices"]["process"] == "reload_checkpoint"
    mismatch = report["rejected_histories"][0]["first_mismatch"]
    assert mismatch["record"] == "r3_report_fit_detect"
    assert mismatch["row"] == 0


def test_minute_seven_attribution_and_minute_six_unknown(case):
    report = audit.audit(case)
    minute6 = report["minute_attribution"][5]
    minute7 = report["minute_attribution"][6]
    assert minute6["observed"] is None
    assert minute6["unknown_branches"] == ["A"]
    assert minute7["observed"] is True
    assert minute7["definite_triggers"] == ["B"]
    assert minute7["possible_triggers"] == ["B"]


def test_minimum_three_follow_up_minutes_and_gap_groups(case):
    report = audit.audit(case)
    plan = report["follow_up_plan"]
    assert plan["minimum_minutes"] == [
        "m1_A35_B0", "m2_A45_B0", "m3_A0_B55"
    ]
    assert len(plan["groups"]) == 4
    assert len(report["evidence_gaps"]) == 2
    for gap in report["evidence_gaps"]:
        assert gap["choice_dimensions_left_open"] == ["process"]
        assert len(gap["indistinguishable_histories"]) == 2


def test_archive_signatures_match_adtk_run(case):
    expected = {
        ("distinct", "continue_failed", "none"): {
            "m1_A35_B0": True, "m2_A45_B0": True,
            "m3_A0_B55": True, "m4_A0_B0": False,
        },
        ("distinct", "continue_failed", "applied"): {
            "m1_A35_B0": False, "m2_A45_B0": False,
            "m3_A0_B55": True, "m4_A0_B0": False,
        },
        ("shared_B", "continue_failed", "none"): {
            "m1_A35_B0": False, "m2_A45_B0": True,
            "m3_A0_B55": True, "m4_A0_B0": False,
        },
        ("shared_B", "continue_failed", "applied"): {
            "m1_A35_B0": False, "m2_A45_B0": False,
            "m3_A0_B55": False, "m4_A0_B0": False,
        },
        ("shared_B", "reload_checkpoint", "none"): {
            "m1_A35_B0": False, "m2_A45_B0": True,
            "m3_A0_B55": True, "m4_A0_B0": False,
        },
        ("shared_B", "reload_checkpoint", "applied"): {
            "m1_A35_B0": False, "m2_A45_B0": False,
            "m3_A0_B55": False, "m4_A0_B0": False,
        },
    }
    report = audit.audit(case)
    for hist in report["consistent_histories"]:
        key = (
            hist["choices"]["wiring"],
            hist["choices"]["process"],
            hist["choices"]["calibration"],
        )
        assert hist["archive_signature"] == expected[key]


def test_no_consistent_history_points_to_first_mismatch(case):
    broken = copy.deepcopy(case)
    broken["records"][2]["observed"] = [True] * 7
    report = audit.audit(broken)
    assert report["consistent_histories"] == []
    assert len(report["rejected_histories"]) == 8
    assert report["rejected_histories"][0]["first_mismatch"]["record"] == \
        "r3_report_fit_detect"


def test_real_adtk_crosscheck_still_passes():
    script = os.path.join(DUTY_DIR, "crosscheck_adtk.py")
    repo_root = os.path.dirname(DUTY_DIR)
    proc = subprocess.run(
        [sys.executable, script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    mismatch_lines = [
        line for line in proc.stdout.splitlines() if "MISMATCH" in line
    ]
    assert len(mismatch_lines) == 1
    assert "reload-distinct" in mismatch_lines[0]
    assert "reload-distinct" in proc.stdout
    assert proc.stdout.count("MATCH") - proc.stdout.count("MISMATCH") == 3
