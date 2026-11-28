import json
from decimal import Decimal

import pytest

from csv_diff import reconcile
from csv_diff.reconcile import (
    ReplayError,
    State,
    apply_occurrence,
    execute_segment,
    has_split_merge,
    normalize_rules,
    parse_occurrences,
    parse_stocktake,
    plan_reconciliation,
    render_plan,
    render_text,
    state_from_stocktake,
)


PREVIOUS = [
    {"package": "A", "location": "A仓", "qty": 7},
    {"package": "B0", "location": "B仓", "qty": 2},
]

CURRENT = [
    {"package": "A", "location": "A仓", "qty": 4},
    {"package": "B", "location": "B仓", "qty": 5},
]

ARCHIVE = [
    {
        "occurrence_id": "S1",
        "type": "split",
        "from_location": "A仓",
        "inputs": [{"package": "A", "qty": 7}],
        "outputs": [
            {"package": "A", "location": "A仓", "qty": 4},
            {"package": "A-3", "location": "A仓", "qty": 3},
        ],
        "teams": ["A"],
    },
    {
        "occurrence_id": "T1",
        "type": "transfer",
        "from_location": "A仓",
        "to_location": "B仓",
        "inputs": [{"package": "A-3", "qty": 3}],
        "outputs": [{"package": "A-3", "location": "B仓", "qty": 3}],
        "teams": ["A", "B"],
    },
    {
        "occurrence_id": "M1",
        "type": "merge",
        "to_location": "B仓",
        "inputs": [
            {"package": "A-3", "location": "B仓", "qty": 3},
            {"package": "B0", "location": "B仓", "qty": 2},
        ],
        "outputs": [{"package": "B", "location": "B仓", "qty": 5}],
        "teams": ["B"],
    },
]


def _state(rows):
    return state_from_stocktake(parse_stocktake(rows))


def test_split_transfer_merge_replays_to_correct_lineage():
    state = _state(PREVIOUS)
    for op in parse_occurrences(ARCHIVE):
        apply_occurrence(state, op)
    assert state.get("A仓", "A") == 4
    assert state.get("B仓", "B") == 5
    assert state.provenance[("B仓", "B")] == {"A": Decimal(3), "B0": Decimal(2)}


def test_plan_reaches_target_and_preserves_original_packages():
    plan = render_plan(PREVIOUS, CURRENT, ARCHIVE)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert plan["acceptance"]["complete"] is True
    assert plan["acceptance"]["issue_merge_count"] == 1
    assert plan["acceptance"]["origin_coverage"] == {"A": 10, "B0": 2}


def test_a_package_cannot_be_transferred_twice():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    ops = parse_occurrences(
        [
            {
                "occurrence_id": "S1",
                "type": "split",
                "from_location": "A仓",
                "inputs": [{"package": "A", "qty": 7}],
                "outputs": [
                    {"package": "A-4", "location": "A仓", "qty": 4},
                    {"package": "A-3", "location": "A仓", "qty": 3},
                ],
            },
            {
                "occurrence_id": "T1",
                "type": "transfer",
                "from_location": "A仓",
                "to_location": "B仓",
                "inputs": [{"package": "A-3", "qty": 3}],
                "outputs": [{"package": "A-3", "location": "B仓", "qty": 3}],
            },
            {
                "occurrence_id": "T2",
                "type": "transfer",
                "from_location": "B仓",
                "to_location": "C仓",
                "inputs": [{"package": "A-3", "qty": 3}],
                "outputs": [{"package": "A-3", "location": "C仓", "qty": 3}],
            },
        ]
    )
    state = _state(previous)
    apply_occurrence(state, ops[0])
    apply_occurrence(state, ops[1])
    with pytest.raises(ReplayError) as exc:
        apply_occurrence(state, ops[2])
    assert exc.value.details["kind"] == "tag_reused"


def test_negative_stock_is_blocked_midway():
    previous = [{"package": "A", "location": "A仓", "qty": 2}]
    op = parse_occurrences(
        [
            {
                "occurrence_id": "X1",
                "type": "issue",
                "location": "A仓",
                "inputs": [{"package": "A", "qty": 3}],
            }
        ]
    )[0]
    with pytest.raises(ReplayError):
        apply_occurrence(_state(previous), op)


def test_duplicate_pending_transfer_is_applied_once():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [
        {"package": "A", "location": "A仓", "qty": 7},
        {"package": "A", "location": "B仓", "qty": 3},
    ]
    message = {
        "occurrence_id": "T9",
        "type": "transfer",
        "from": "A仓",
        "to": "B仓",
        "inputs": [{"package": "A", "qty": 3}],
        "outputs": [{"package": "A", "location": "B仓", "qty": 3}],
    }
    plan = render_plan(
        previous,
        current,
        [dict(message, teams=["A"]), dict(message, teams=["B"])],
    )
    assert plan["status"] == "ok"
    assert len(plan["actions"]) == 1
    assert plan["final_matches_target"] is True


def test_conflicting_shared_credentials_go_to_supervisor():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [
        {"package": "A", "location": "A仓", "qty": 7},
        {"package": "A", "location": "B仓", "qty": 3},
    ]
    plan = render_plan(
        previous,
        current,
        [
            {
                "occurrence_id": "T9",
                "type": "transfer",
                "from": "A仓",
                "to": "B仓",
                "inputs": [{"package": "A", "qty": 3}],
                "outputs": [{"package": "A", "location": "B仓", "qty": 3}],
                "teams": ["A"],
            },
            {
                "occurrence_id": "T9",
                "type": "transfer",
                "from": "A仓",
                "to": "B仓",
                "inputs": [{"package": "A", "qty": 4}],
                "outputs": [{"package": "A", "location": "B仓", "qty": 4}],
                "teams": ["B"],
            },
        ],
    )
    assert plan["status"] == "manual_review"
    assert plan["reason"] == "shared_credential_conflict"
    assert plan["shared_credentials"][0]["teams"] == ["A", "B"]
    assert "需主管处理" in render_text(plan)


def test_segments_pause_and_resume_from_checkpoint():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        segment_limits={"A": 1, "B": 1},
    )
    assert [segment["team"] for segment in plan["segments"]] == ["A", "B", "B"]

    first = execute_segment(plan, team="B", from_segment=2, limit=1)
    assert first["status"] == "paused"
    assert first["actions"] == ["T1:full:whole@B"]

    second = execute_segment(
        plan, team="B", from_segment=first["checkpoint"]["next_segment"]
    )
    assert second["status"] == "complete"
    assert second["complete"] is True
    assert second["final"]["balances"] == [
        {"location": "A仓", "package": "A", "qty": 4},
        {"location": "B仓", "package": "B", "qty": 5},
    ]


def test_half_posted_draft_uses_repair_legs():
    previous = [
        {"package": "A", "location": "A仓", "qty": 7},
        {"package": "B", "location": "B仓", "qty": 5},
        {"package": "B0", "location": "B仓", "qty": 2},
        {"package": "A-3", "location": "A仓", "qty": 3},
    ]
    plan = render_plan(previous, CURRENT, ARCHIVE)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert any(action["mode"] == "legs" for action in plan["selected"])


def test_illegal_transfer_pair_is_refused():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        rules={"transfers": [["B仓", "A仓"]]},
    )
    assert plan["status"] == "manual_review"
    assert plan["reason"] == "illegal_or_cyclic_archive"
    assert plan["shared_credentials"][0]["type"] == "illegal_transfer"
    assert plan["shared_credentials"][0]["occurrence_id"] == "T1"


def test_location_availability_is_enforced():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        rules={"location_limits": {"B仓": 4}},
    )
    assert plan["status"] == "manual_review"
    assert plan["conflicts"][-1]["type"] == "availability_conflict"


def test_origin_mismatch_is_not_accepted_even_when_totals_match():
    current = [
        {"package": "A", "location": "A仓", "qty": 4},
        {
            "package": "B",
            "location": "B仓",
            "qty": 5,
            "origins": [{"package": "A", "qty": 5}],
        },
    ]
    plan = render_plan(PREVIOUS, current, ARCHIVE)
    assert plan["status"] == "manual_review"
    assert plan["reason"] == "no_common_replay"


def test_normalize_rules_accepts_pairs_and_limits():
    rules = normalize_rules(
        {
            "transfers": [{"from": "A仓", "to": "B仓"}],
            "location_limits": {"A仓": "20", "B仓": 15},
        }
    )
    assert rules["transfers"] == {("A仓", "B仓")}
    assert rules["location_limits"] == {"A仓": Decimal("20"), "B仓": Decimal("15")}


def test_plain_tables_do_not_need_occurrences():
    assert has_split_merge(None) is False
    assert has_split_merge([{"type": "issue"}]) is False
    assert has_split_merge([{"type": "merge"}]) is True
    previous = [{"id": "1", "name": "Cleo", "age": "4"}]
    current = [{"id": "1", "name": "Cleo", "age": "5"}]
    diff = reconcile.compare_plain(previous, current, key="id")
    assert diff["changed"] == [{"key": "1", "changes": {"age": ["4", "5"]}}]


def test_plan_reconciliation_accepts_json_files(tmpdir):
    previous = tmpdir / "previous.json"
    current = tmpdir / "current.json"
    archive = tmpdir / "archive.json"
    previous.write(json.dumps(PREVIOUS, ensure_ascii=False))
    current.write(json.dumps(CURRENT, ensure_ascii=False))
    archive.write(json.dumps(ARCHIVE, ensure_ascii=False))
    plan = plan_reconciliation(
        str(previous), str(current), str(archive), format="json"
    )
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert plan["files"]["occurrences"].endswith("archive.json")


def test_plan_reconciliation_accepts_csv_files(tmpdir):
    previous = tmpdir / "previous.csv"
    current = tmpdir / "current.csv"
    archive = tmpdir / "archive.csv"
    previous.write(
        "package,location,qty\nA,A仓,7\nB0,B仓,2\n"
    )
    current.write(
        "package,location,qty\nA,A仓,4\nB,B仓,5\n"
    )
    archive.write(
        "occurrence_id,type,from_location,to_location,inputs,outputs\n"
        'S1,split,A仓,,A:7,A:4|A-3:3\n'
        'T1,transfer,A仓,B仓,A-3:3,A-3:3\n'
        'M1,merge,,B仓,A-3:3|B0:2,B:5\n'
    )
    plan = plan_reconciliation(str(previous), str(current), str(archive))
    assert plan["status"] == "ok", plan
    assert plan["final_matches_target"] is True


def test_alternative_legal_stocktake_id_is_preferred():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    current = [
        {"package": "A", "location": "A仓", "qty": 4},
        {"package": "A-3", "location": "A仓", "qty": 3},
    ]
    archive = [
        {
            "occurrence_id": "S1",
            "type": "split",
            "from_location": "A仓",
            "inputs": [{"package": "A", "qty": 7}],
            "outputs": [
                {"package": "A", "location": "A仓", "qty": 4},
                {"package": "A-3", "location": "A仓", "qty": 3},
            ],
            "alternatives": [
                {
                    "label": "keep-legal-id",
                    "inputs": [{"package": "A", "qty": 7}],
                    "outputs": [
                        {"package": "A", "location": "A仓", "qty": 4},
                        {"package": "A-3", "location": "A仓", "qty": 3},
                    ],
                },
                {
                    "label": "receive-original",
                    "inputs": [{"package": "A", "qty": 7}],
                    "outputs": [
                        {"package": "A", "location": "A仓", "qty": 7},
                    ],
                },
            ],
        }
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["selected"][0]["variant"] == "keep-legal-id"


def test_both_checklists_cover_every_archive_record():
    archive = list(ARCHIVE)
    archive.append(
        {
            "occurrence_id": "I1",
            "type": "issue",
            "from_location": "A仓",
            "inputs": [{"package": "A", "qty": 0}],
            "teams": ["A"],
        }
    )
    current = [
        {"package": "A", "location": "A仓", "qty": 4},
        {"package": "B", "location": "B仓", "qty": 5},
    ]
    plan = render_plan(PREVIOUS, current, archive)
    assert plan["status"] == "ok"
    acceptance = plan["acceptance"]
    assert {item["occurrence_id"] for item in acceptance["issue_and_merge"]} == {
        "M1",
        "I1",
    }
    for team in ("A", "B"):
        checklist = acceptance["checklists"][team]
        assert checklist["matches_archive"] is True
        assert [entry["occurrence_id"] for entry in checklist["entries"]] == [
            "M1",
            "I1",
        ]


def test_dependency_cycle_returns_credentials_and_teams():
    previous = [{"package": "A", "location": "A仓", "qty": 5}]
    current = [{"package": "A", "location": "A仓", "qty": 5}]
    archive = [
        {
            "occurrence_id": "X1",
            "type": "transfer",
            "from": "A仓",
            "to": "B仓",
            "inputs": [{"package": "A", "qty": 5}],
            "outputs": [{"package": "A", "location": "B仓", "qty": 5}],
            "teams": ["A"],
        },
        {
            "occurrence_id": "X2",
            "type": "transfer",
            "from": "B仓",
            "to": "A仓",
            "inputs": [{"package": "A", "qty": 5}],
            "outputs": [{"package": "A", "location": "A仓", "qty": 5}],
            "teams": ["B"],
        },
    ]
    plan = render_plan(previous, current, archive)
    # There may be a legal replay (move out then back is normally blocked by
    # the one-transfer rule), but if a cycle is reported it must carry teams.
    cycle = [
        conflict
        for conflict in plan.get("conflicts", [])
        if conflict["type"] == "dependency_cycle"
    ]
    for conflict in cycle:
        assert conflict["credentials"]
        assert {credential["occurrence_id"] for credential in conflict["credentials"]} <= {
            "X1",
            "X2",
        }


def test_issue_only_archive_with_row_level_location():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    current = [{"package": "A", "location": "A仓", "qty": 5}]
    plan = render_plan(
        previous,
        current,
        [
            {
                "occurrence_id": "I1",
                "type": "issue",
                "location": "A仓",
                "inputs": [{"package": "A", "qty": 2}],
            }
        ],
    )
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True


def test_available_column_does_not_override_physical_qty():
    previous = [
        {"package": "A", "location": "A仓", "qty": 7, "available": 7}
    ]
    current = [
        {"package": "A", "location": "A仓", "qty": 5, "available": 5}
    ]
    rows = parse_stocktake(previous)
    assert rows[0]["qty"] == Decimal("7")
    assert rows[0]["available"] == Decimal("7")
    plan = render_plan(
        previous,
        current,
        [
            {
                "occurrence_id": "I1",
                "type": "issue",
                "location": "A仓",
                "inputs": [{"package": "A", "qty": 2}],
            }
        ],
    )
    assert plan["status"] == "ok"


def test_partial_output_leg_uses_original_provenance():
    # The merged B is already on the draft; only B's source was half-retired.
    previous = [
        {"package": "A-3", "location": "B仓", "qty": 3},
        {"package": "B0", "location": "B仓", "qty": 2},
        {"package": "B", "location": "B仓", "qty": 5},
    ]
    current = [{"package": "B", "location": "B仓", "qty": 5}]
    archive = [
        {
            "occurrence_id": "M1",
            "type": "merge",
            "to_location": "B仓",
            "inputs": [
                {"package": "A-3", "location": "B仓", "qty": 3, "origins": [{"package": "A"}]},
                {"package": "B0", "location": "B仓", "qty": 2, "origins": [{"package": "B0"}]},
            ],
            "outputs": [{"package": "B", "location": "B仓", "qty": 5}],
        }
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True


def test_two_team_copies_of_one_transfer_keep_both_signed_ids():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [
        {"package": "A", "location": "A仓", "qty": 7},
        {"package": "A", "location": "B仓", "qty": 3},
    ]
    archive = [
        {
            "occurrence_id": "T-A",
            "type": "transfer",
            "from": "A仓",
            "to": "B仓",
            "inputs": [{"package": "A", "qty": 3}],
            "outputs": [{"package": "A", "location": "B仓", "qty": 3}],
            "teams": ["A"],
            "status": "pending",
            "approved": False,
        },
        {
            "occurrence_id": "T-B",
            "type": "transfer",
            "from": "A仓",
            "to": "B仓",
            "inputs": [{"package": "A", "qty": 3}],
            "outputs": [{"package": "A", "location": "B仓", "qty": 3}],
            "teams": ["B"],
            "status": "pending",
            "approved": False,
        },
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert len(plan["actions"]) == 1
    assert plan["archive"][0]["also_signed_as"] == ["T-B"]
    assert plan["acceptance"]["archive_count"] == 2


def test_two_team_copies_of_one_issue_keep_both_acceptance_ids():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [{"package": "A", "location": "A仓", "qty": 7}]
    archive = [
        {
            "occurrence_id": "I-A",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["A"],
            "status": "pending",
            "approved": False,
        },
        {
            "occurrence_id": "I-B",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["B"],
            "status": "pending",
            "approved": False,
        },
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert len(plan["actions"]) == 1
    acceptance = plan["acceptance"]
    assert acceptance["archive_count"] == 2
    assert {item["occurrence_id"] for item in acceptance["issue_and_merge"]} == {
        "I-A",
        "I-B",
    }


def test_same_non_pending_issue_from_both_teams_is_kept_as_two_events():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [{"package": "A", "location": "A仓", "qty": 4}]
    archive = [
        {
            "occurrence_id": "I-A",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["A"],
        },
        {
            "occurrence_id": "I-B",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["B"],
        },
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert len(plan["actions"]) == 2


def test_rules_survive_execution_segment():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        rules={"location_limits": {"B仓": 5}},
    )
    assert plan["status"] == "ok"
    out = execute_segment(plan, team="B", from_segment=2)
    assert out["status"] == "complete"
    assert out["complete"] is True

    too_tight = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        rules={"location_limits": {"B仓": 4}},
    )
    assert too_tight["status"] == "manual_review"


def test_approved_id_selects_matching_variant():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    current = [
        {"package": "A", "location": "A仓", "qty": 4},
        {"package": "A-3", "location": "A仓", "qty": 3},
    ]
    base = {
        "occurrence_id": "S1",
        "type": "split",
        "from_location": "A仓",
        "inputs": [{"package": "A", "qty": 7}],
        "outputs": [
            {"package": "A", "location": "A仓", "qty": 4},
            {"package": "A-3", "location": "A仓", "qty": 3},
        ],
    }
    archive = [
        dict(
            base,
            approved_ids=["S1-y"],
            alternatives=[
                {
                    "label": "x",
                    "occurrence_id": "S1-x",
                    "inputs": [{"package": "A", "qty": 7}],
                    "outputs": base["outputs"],
                },
                {
                    "label": "y",
                    "occurrence_id": "S1-y",
                    "inputs": [{"package": "A", "qty": 7}],
                    "outputs": base["outputs"],
                },
            ],
        )
    ]
    plan = render_plan(previous, current, archive)
    assert plan["selected"][0]["variant"] == "y"
    assert plan["selected"][0]["action_id"].startswith("S1-y")


def test_unexplained_quantity_difference_is_reported():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [{"package": "A", "location": "A仓", "qty": 6}]
    archive = [
        {
            "occurrence_id": "I1",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
        }
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "manual_review"
    conflict = plan["conflicts"][-1]
    assert conflict["type"] == "unexplained_difference"
    assert conflict["difference"] == [
        {"location": "A仓", "package": "A", "qty": -1}
    ]


def test_chinese_headers_and_team_aliases(tmpdir):
    previous = tmpdir / "prev.csv"
    current = tmpdir / "cur.csv"
    archive = tmpdir / "arc.csv"
    previous.write("包号,库位,数量\nA,A仓,7\nB0,B仓,2\n")
    current.write("包号,库位,数量\nA,A仓,4\nB,B仓,5\n")
    archive.write(
        "发生号,类型,调出库位,调入库位,来源包,产物,班组\n"
        "S1,拆包,A仓,,A:7,A:4|A-3:3,甲班\n"
        "T1,调拨,A仓,B仓,A-3:3,A-3:3,甲班;乙班\n"
        "M1,合包,,B仓,A-3:3|B0:2,B:5,乙班\n"
    )
    plan = plan_reconciliation(str(previous), str(current), str(archive))
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert plan["acceptance"]["complete"] is True
    assert set(plan["acceptance"]["checklists"]) == {"A", "B"}


def test_acceptance_detects_wrong_original_merge_source():
    previous = [
        {"package": "A-3", "location": "B仓", "qty": 3},
        {"package": "C0", "location": "B仓", "qty": 2},
    ]
    current = [{"package": "B", "location": "B仓", "qty": 5}]
    archive = [
        {
            "occurrence_id": "M1",
            "type": "merge",
            "to_location": "B仓",
            "inputs": [
                {"package": "A-3", "location": "B仓", "qty": 3},
                {"package": "C0", "location": "B仓", "qty": 2},
            ],
            "outputs": [{"package": "B", "location": "B仓", "qty": 5}],
        }
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["acceptance"]["origin_coverage"] == {
        "A-3": Decimal("3"),
        "C0": Decimal("2"),
    }
    journal = plan["acceptance"]["journal"][0]
    assert journal["outputs"][0]["expected_origins"] == [
        {"package": "A-3", "qty": 3},
        {"package": "C0", "qty": 2},
    ]


def test_starting_overage_may_be_reduced_but_not_increased():
    previous = [
        {"package": "A", "location": "A仓", "qty": 7, "available": 5}
    ]
    reduced = [{"package": "A", "location": "A仓", "qty": 4, "available": 5}]
    archive = [
        {
            "occurrence_id": "I1",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
        }
    ]
    plan = render_plan(previous, reduced, archive)
    assert plan["status"] == "ok"

    increased = [{"package": "A", "location": "A仓", "qty": 9, "available": 5}]
    archive = [
        {
            "occurrence_id": "R1",
            "type": "receive",
            "location": "A仓",
            "outputs": [{"package": "A", "qty": 2}],
        }
    ]
    plan = render_plan(previous, increased, archive)
    assert plan["status"] == "manual_review"


def test_search_exhaustion_is_reported_not_called_impossible():
    previous = []
    current = []
    archive = []
    for index in range(5):
        previous.append(
            {"package": "P{}".format(index), "location": "A仓", "qty": 10}
        )
        current.append(
            {"package": "P{}".format(index), "location": "A仓", "qty": 8}
        )
        archive.extend(
            [
                {
                    "occurrence_id": "I{}".format(index),
                    "type": "issue",
                    "location": "A仓",
                    "inputs": [{"package": "P{}".format(index), "qty": 3}],
                },
                {
                    "occurrence_id": "R{}".format(index),
                    "type": "receive",
                    "location": "A仓",
                    "outputs": [{"package": "P{}".format(index), "qty": 1}],
                },
                {
                    "occurrence_id": "J{}".format(index),
                    "type": "issue",
                    "location": "A仓",
                    "inputs": [{"package": "P{}".format(index), "qty": 2}],
                },
            ]
        )
    plan = render_plan(previous, current, archive, max_nodes=3)
    assert plan["status"] == "manual_review"
    assert any(
        conflict["type"] == "search_exhausted"
        for conflict in plan["conflicts"]
    )


def test_pause_inside_segment_resumes_at_offset():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        segment_limits={"A": 1, "B": 1},
    )
    first = execute_segment(plan, team="B", from_segment=2, limit=1)
    assert first["status"] == "paused"
    assert first["checkpoint"]["next_segment"] == 3
    assert first["checkpoint"]["next_offset"] == 0

    second = execute_segment(
        plan,
        team="B",
        from_segment=first["checkpoint"]["next_segment"],
        segment_offset=first["checkpoint"]["next_offset"],
    )
    assert second["status"] == "complete"
    assert second["complete"] is True


def test_interleaved_teams_keep_causal_order_in_segments():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [
        {"package": "A", "location": "A仓", "qty": 6},
        {"package": "B", "location": "B仓", "qty": 2},
        {"package": "C", "location": "C仓", "qty": 2},
    ]
    archive = [
        {
            "occurrence_id": "S1",
            "type": "split",
            "from_location": "A仓",
            "inputs": [{"package": "A", "qty": 4}],
            "outputs": [
                {"package": "A", "location": "A仓", "qty": 2},
                {"package": "P1", "location": "A仓", "qty": 2},
            ],
            "teams": ["A"],
        },
        {
            "occurrence_id": "T1",
            "type": "transfer",
            "from": "A仓",
            "to": "B仓",
            "inputs": [{"package": "P1", "qty": 2}],
            "outputs": [{"package": "P1", "location": "B仓", "qty": 2}],
            "teams": ["B"],
        },
        {
            "occurrence_id": "M1",
            "type": "merge",
            "to_location": "B仓",
            "inputs": [{"package": "P1", "location": "B仓", "qty": 2}],
            "outputs": [{"package": "B", "location": "B仓", "qty": 2}],
            "teams": ["B"],
        },
        {
            "occurrence_id": "S2",
            "type": "split",
            "from_location": "A仓",
            "inputs": [{"package": "A", "qty": 2}],
            "outputs": [{"package": "P2", "location": "A仓", "qty": 2}],
            "teams": ["A"],
        },
        {
            "occurrence_id": "T2",
            "type": "transfer",
            "from": "A仓",
            "to": "C仓",
            "inputs": [{"package": "P2", "qty": 2}],
            "outputs": [{"package": "P2", "location": "C仓", "qty": 2}],
            "teams": ["B"],
        },
        {
            "occurrence_id": "M2",
            "type": "merge",
            "to_location": "C仓",
            "inputs": [{"package": "P2", "location": "C仓", "qty": 2}],
            "outputs": [{"package": "C", "location": "C仓", "qty": 2}],
            "teams": ["B"],
        },
    ]
    plan = render_plan(
        previous, current, archive, segment_limits={"A": 1, "B": 2}
    )
    assert plan["status"] == "ok"
    assert [segment["team"] for segment in plan["segments"]] == ["A", "A", "B", "B"]
    # Each A segment must appear before the B segment that consumes its product.
    ids = [
        [action["occurrence_id"] for action in segment["actions"]]
        for segment in plan["segments"]
    ]
    assert ids[0] == ["S1"]
    assert ids[1] == ["S2"]
    assert ids[2] == ["T1", "M1"]
    assert ids[3] == ["T2", "M2"]


def test_independent_store_components_decompose_and_validate():
    previous = []
    current = []
    archive = []
    for index in range(6):
        previous.extend(
            [
                {"package": "A{}".format(index), "location": "A仓", "qty": 7},
                {"package": "B{}".format(index), "location": "B仓", "qty": 2},
                {"package": "C{}".format(index), "location": "B仓", "qty": 5},
            ]
        )
        current.extend(
            [
                {"package": "A{}".format(index), "location": "A仓", "qty": 4},
                {"package": "C{}".format(index), "location": "B仓", "qty": 5},
            ]
        )
        archive.extend(
            [
                {
                    "occurrence_id": "S{}".format(index),
                    "type": "split",
                    "from_location": "A仓",
                    "inputs": [{"package": "A{}".format(index), "qty": 7}],
                    "outputs": [
                        {"package": "A{}".format(index), "location": "A仓", "qty": 4},
                        {"package": "P{}".format(index), "location": "A仓", "qty": 3},
                    ],
                },
                {
                    "occurrence_id": "T{}".format(index),
                    "type": "transfer",
                    "from": "A仓",
                    "to": "B仓",
                    "inputs": [{"package": "P{}".format(index), "qty": 3}],
                    "outputs": [
                        {"package": "P{}".format(index), "location": "B仓", "qty": 3}
                    ],
                },
                {
                    "occurrence_id": "M{}".format(index),
                    "type": "merge",
                    "to_location": "B仓",
                    "inputs": [
                        {"package": "P{}".format(index), "location": "B仓", "qty": 3},
                        {"package": "B{}".format(index), "location": "B仓", "qty": 2},
                    ],
                    "outputs": [
                        {"package": "C{}".format(index), "location": "B仓", "qty": 5}
                    ],
                },
            ]
        )
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert len(plan["actions"]) == 18


def test_signed_adjustment_occurrence():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    current = [{"package": "A", "location": "A仓", "qty": 4}]
    decreasing = [
        {
            "occurrence_id": "D1",
            "type": "adjust",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": -3}],
        }
    ]
    plan = render_plan(previous, current, decreasing)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert plan["selected"][0]["mode"] == "write"

    increasing = [
        {
            "occurrence_id": "D2",
            "type": "adjust",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
        }
    ]
    plan = render_plan(previous, current, increasing)
    assert plan["status"] == "manual_review"


def test_absolute_set_occurrence():
    previous = [{"package": "A", "location": "A仓", "qty": 7}]
    current = [{"package": "A", "location": "A仓", "qty": 4}]
    archive = [
        {
            "occurrence_id": "S",
            "type": "set",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 4}],
        }
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert plan["selected"][0]["mode"] == "write"


def test_already_agreeing_sheets_do_not_repost_net_zero_vouchers():
    previous = [{"package": "A", "location": "A仓", "qty": 5}]
    current = [{"package": "A", "location": "A仓", "qty": 5}]
    archive = [
        {
            "occurrence_id": "R1",
            "type": "receive",
            "location": "A仓",
            "outputs": [{"package": "A", "qty": 2}],
        },
        {
            "occurrence_id": "I1",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 2}],
        },
    ]
    plan = render_plan(previous, current, archive)
    assert plan["status"] == "ok"
    assert plan["actions"] == []
    assert plan["final_matches_target"] is True


def test_text_plan_lists_execution_order_and_resume_points():
    plan = render_plan(
        PREVIOUS,
        CURRENT,
        ARCHIVE,
        segment_limits={"A": 1, "B": 1},
    )
    text = render_text(plan)
    assert "总重放顺序" in text
    assert "第 1 段（A 班组）" in text
    assert "--checkpoint 2" in text
    assert "验收清单" in text
    assert "[A 班组验收]" in text
    assert "[B 班组验收]" in text


def test_two_issue_team_copies_both_marked_replayed_via_primary_id():
    previous = [{"package": "A", "location": "A仓", "qty": 10}]
    current = [{"package": "A", "location": "A仓", "qty": 7}]
    archive = [
        {
            "occurrence_id": "I-A",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["A"],
            "status": "pending",
            "approved": False,
        },
        {
            "occurrence_id": "I-B",
            "type": "issue",
            "location": "A仓",
            "inputs": [{"package": "A", "qty": 3}],
            "teams": ["B"],
            "status": "pending",
            "approved": False,
        },
    ]
    plan = render_plan(previous, current, archive)
    journal = plan["acceptance"]["journal"]
    assert [(item["occurrence_id"], item["primary_occurrence_id"]) for item in journal] == [
        ("I-A", "I-A"),
        ("I-B", "I-A"),
    ]
    assert all(item["replayed"] for item in journal)
    assert plan["acceptance"]["unapplied_records"] == []


def test_plan_json_round_trip_can_be_executed(tmpdir):
    import json

    plan = render_plan(PREVIOUS, CURRENT, ARCHIVE, segment_limits={"A": 1, "B": 1})
    path = tmpdir / "plan.json"
    path.write(json.dumps(plan))
    loaded = json.loads(path.read())
    result = execute_segment(loaded, team="B", from_segment=2)
    assert result["status"] == "complete"
    assert result["complete"] is True
    assert result["final"]["balances"] == [
        {"location": "A仓", "package": "A", "qty": 4},
        {"location": "B仓", "package": "B", "qty": 5},
    ]
