"""Tests for the minimum-block revision planner and manifest model.

The headline scenario reproduces the archive's P / Q / R example exactly:
three interleaved compressed blocks, two repairable channels A and B that each
only get their first sample approved, and an expected retransmission list of
only Q and R.
"""
from __future__ import annotations

import pytest

from mdfreader.revision import (
    ManifestError,
    PlanningConflict,
    conflict_report,
    delivery_report,
    parse_manifest,
    plan_revision,
    write_delivery_bundle,
)

P, Q, R = "P", "Q", "R"


def _sample(index, block_id, values, approved=False, role="data", offset=0):
    pointer_table = {"P": 1000, "Q": 2000, "R": 3000}
    pointer = pointer_table.get(block_id, 1000 + 100 * (abs(hash(block_id)) % 1000))
    return {
        "sample_index": index,
        "block_id": block_id,
        "block_pointer": pointer,
        "block_role": role,
        "invalid_byte_offset": offset,
        "approved": approved,
        "candidate_bits": [
            {"bit_position": bit, "original_value": value}
            for bit, value in values.items()
        ],
    }


def _pqr_manifest():
    # Six samples, two per block; only the first sample (index 0) is approved.
    blocks = (P, P, Q, Q, R, R)
    pattern_x = "111111"
    pattern_y = "010101"
    samples_a = [
        _sample(i, blocks[i], {0: int(pattern_x[i]), 1: int(pattern_y[i])},
                approved=(i == 0), offset=8)
        for i in range(6)
    ]
    # B may use bit 1; bit 2 is occupied by an existing channel whose first
    # sample must stay invalid (so bit 2 keeps value 1 at index 0).
    pattern_z = "101010"
    samples_b = [
        _sample(i, blocks[i], {1: int(pattern_y[i]), 2: int(pattern_z[i])},
                approved=(i == 0), offset=8)
        for i in range(6)
    ]
    return {
        "block_order": [P, Q, R],
        "groups": [
            {
                "group_id": "G1",
                "layout": "interleaved",
                "data_group": 0,
                "channel_group": 0,
                "record_id": 1,
                "record_id_size": 1,
                "record_length": 12,
                "invalid_bytes": 1,
                "channels": [
                    {
                        "channel_name": "A",
                        "cn_pointer": 816,
                        "data_group": 0,
                        "channel_group": 0,
                        "current_bit_position": 0,
                        "current_flags": 0b01,
                        "candidate_bits": [
                            {"bit_position": 0, "occupied_by": []},
                            {"bit_position": 1, "occupied_by": []},
                        ],
                        "samples": samples_a,
                    },
                    {
                        "channel_name": "B",
                        "cn_pointer": 1040,
                        "data_group": 0,
                        "channel_group": 0,
                        "current_bit_position": 2,
                        "current_flags": 0b01,
                        "candidate_bits": [
                            {"bit_position": 1, "occupied_by": []},
                            {"bit_position": 2,
                             "occupied_by": ["ExistingChan"]},
                        ],
                        "samples": samples_b,
                    },
                ],
            }
        ],
    }


def test_pqr_example_retransmits_only_q_and_r():
    manifest = parse_manifest(_pqr_manifest())
    plan = plan_revision(manifest)

    assert plan.retransmit_blocks == [Q, R]
    assert plan.retransmit_count == 2

    group_plan = plan.group_plans["G1"]
    assert group_plan.chosen.bit_for("A") == 1
    assert group_plan.chosen.bit_for("B") == 1

    # Both channels reread with only the first sample valid.
    for assignment in group_plan.chosen.assignments:
        assert assignment.target_pattern == (0, 1, 1, 1, 1, 1)

    # P is never edited; Q and R carry the 0 -> 1 normalisation edits.
    assert P not in plan.block_edits
    edited_blocks = set(plan.block_edits)
    assert edited_blocks == {Q, R}
    for edits in plan.block_edits.values():
        assert all(edit.target_value == 1 for edit in edits)
        assert all(edit.bit_position == 1 for edit in edits)


def test_pqr_chooses_shared_bit_over_cheaper_single_channel_view():
    # Without the global optimisation A alone could pick bit 0 (changes P only),
    # but B is forced onto bit 1 (changes Q, R), so the joint optimum shares
    # bit 1 and never touches P.
    manifest = parse_manifest(_pqr_manifest())
    plan = plan_revision(manifest)
    chosen = plan.group_plans["G1"].chosen
    assert chosen.changed_blocks == frozenset({Q, R})


def test_occupied_bit_that_must_change_is_rejected():
    payload = _pqr_manifest()
    # A free bit is always legal, so force a conflict by making B's two
    # candidate bits both occupied, with at least one requiring a change.
    channel_b = payload["groups"][0]["channels"][1]
    channel_b["candidate_bits"] = [
        {"bit_position": 1, "occupied_by": ["OccupiedOne"]},
        {"bit_position": 2, "occupied_by": ["OccupiedTwo"]},
    ]
    manifest = parse_manifest(payload)
    with pytest.raises(PlanningConflict) as exc:
        plan_revision(manifest)
    assert any("B" in c.channels for c in exc.value.conflicts)
    assert all("already used" in c.reason for c in exc.value.conflicts
               if "B" in c.channels)


def test_shared_bit_with_diverging_targets_is_conflict():
    payload = _pqr_manifest()
    # Pin both A and B to bit 1 only, then give B a different approved sample.
    group = payload["groups"][0]
    for channel in group["channels"]:
        channel["candidate_bits"] = [
            {"bit_position": 1, "occupied_by": []}
        ]
        for sample in channel["samples"]:
            sample["candidate_bits"] = [
                item for item in sample["candidate_bits"]
                if item["bit_position"] == 1
            ]
    b_samples = group["channels"][1]["samples"]
    for sample in b_samples:
        sample["approved"] = False
    b_samples[3]["approved"] = True  # B target now differs from A target
    manifest = parse_manifest(payload)
    with pytest.raises(PlanningConflict) as exc:
        plan_revision(manifest)
    assert any("share a bit" in c.reason for c in exc.value.conflicts)


def test_chained_groups_share_block_and_global_minimum_is_found():
    # X covers B1..B3 with two equally cheap-looking options (one block each):
    # bit 0 changes B1, bit 1 changes B3.  Y covers B2..B4 and is forced to
    # change B3 and B4.  The globally minimal union shares B3: {B3, B4}.
    payload = {
        "block_order": ["B1", "B2", "B3", "B4"],
        "groups": [
            {
                "group_id": "X", "layout": "interleaved",
                "data_group": 0, "channel_group": 0,
                "record_id": 1, "record_id_size": 1,
                "record_length": 8, "invalid_bytes": 1,
                "channels": [{
                    "channel_name": "Xc", "cn_pointer": 10,
                    "data_group": 0, "channel_group": 0,
                    "current_bit_position": 0, "current_flags": 1,
                    "candidate_bits": [
                        {"bit_position": 0, "occupied_by": []},
                        {"bit_position": 1, "occupied_by": []},
                    ],
                    # target 0,1,1 ; bit0 [1,1,1] -> change B1 ;
                    # bit1 [0,1,0] -> change B3
                    "samples": [
                        _sample(0, "B1", {0: 1, 1: 0}, approved=True),
                        _sample(1, "B2", {0: 1, 1: 1}),
                        _sample(2, "B3", {0: 1, 1: 0}),
                    ],
                }],
            },
            {
                "group_id": "Y", "layout": "interleaved",
                "data_group": 1, "channel_group": 0,
                "record_id": 2, "record_id_size": 1,
                "record_length": 8, "invalid_bytes": 1,
                "channels": [{
                    "channel_name": "Yc", "cn_pointer": 20,
                    "data_group": 1, "channel_group": 0,
                    "current_bit_position": 0, "current_flags": 1,
                    "candidate_bits": [
                        {"bit_position": 0, "occupied_by": []},
                    ],
                    # target 0,1,1 ; all-zero original -> change B3, B4
                    "samples": [
                        _sample(0, "B2", {0: 0}, approved=True),
                        _sample(1, "B3", {0: 0}),
                        _sample(2, "B4", {0: 0}),
                    ],
                }],
            },
        ],
    }
    manifest = parse_manifest(payload)
    plan = plan_revision(manifest)
    assert set(plan.retransmit_blocks) == {"B3", "B4"}
    assert plan.group_plans["X"].chosen.bit_for("Xc") == 1
    assert plan.retransmit_count == 2


def test_manifest_rejects_invalid_bit_value():
    payload = _pqr_manifest()
    payload["groups"][0]["channels"][0]["samples"][0]["candidate_bits"][0][
        "original_value"] = 2
    with pytest.raises(ManifestError):
        parse_manifest(payload)


def test_manifest_rejects_column_group_with_data_role():
    payload = _pqr_manifest()
    payload["groups"][0]["layout"] = "column"
    with pytest.raises(ManifestError):
        parse_manifest(payload)


def test_manifest_rejects_more_than_three_overlapping_groups():
    groups = []
    for g in range(4):
        groups.append({
            "group_id": f"G{g}", "layout": "interleaved",
            "data_group": g, "channel_group": 0,
            "record_id": g, "record_id_size": 1,
            "record_length": 8, "invalid_bytes": 1,
            "channels": [{
                "channel_name": f"c{g}", "cn_pointer": 100 + g,
                "data_group": g, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 1,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": [_sample(0, "S", {0: 1}, approved=True)],
            }],
        })
    payload = {"block_order": ["S"], "groups": groups}
    manifest = parse_manifest(payload)
    with pytest.raises(ManifestError, match="three-group"):
        plan_revision(manifest)


def test_no_change_means_no_retransmitted_block():
    # Original bit already equals the target: nothing is billed.
    payload = {
        "block_order": ["D1"],
        "groups": [{
            "group_id": "G", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 1, "record_id_size": 1,
            "record_length": 8, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "C", "cn_pointer": 9,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 1,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": [
                    _sample(0, "D1", {0: 0}, approved=True),
                    _sample(1, "D1", {0: 1}),
                ],
            }],
        }],
    }
    manifest = parse_manifest(payload)
    plan = plan_revision(manifest)
    assert plan.retransmit_blocks == []
    assert plan.block_edits == {}


def test_delivery_report_and_bundle(tmp_path):
    manifest = parse_manifest(_pqr_manifest())
    plan = plan_revision(manifest)
    report = delivery_report(manifest, plan)

    assert report["retransmit_block_count"] == 2
    assert report["retransmit_blocks"] == [Q, R]
    group_report = next(g for g in report["groups"] if g["group_id"] == "G1")
    assert group_report["coverage_segment"] == [P, R]
    for channel in group_report["channels"]:
        assert channel["selected_bit_position"] == 1
        assert channel["target_validity_pattern"] == [0, 1, 1, 1, 1, 1]

    paths = write_delivery_bundle(manifest, plan, tmp_path,
                                  build_summary={"retransmitted_blocks": []})
    assert (tmp_path / "revision_plan.json").exists()
    assert (tmp_path / "revision_build.json").exists()
    assert set(paths) == {"plan", "build_summary"}


def test_conflict_report_identifies_group_and_channels():
    payload = _pqr_manifest()
    channel_b = payload["groups"][0]["channels"][1]
    channel_b["candidate_bits"] = [
        {"bit_position": 1, "occupied_by": ["OccupiedOne"]},
        {"bit_position": 2, "occupied_by": ["OccupiedTwo"]},
    ]
    manifest = parse_manifest(payload)
    with pytest.raises(PlanningConflict) as exc:
        plan_revision(manifest)
    report = conflict_report(exc.value)
    assert report["status"] == "conflict"
    assert any("G1" == item["group_id"] and "B" in item["channels"]
               for item in report["conflicts"])
