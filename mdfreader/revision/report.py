"""Delivery artefacts: planning list and revised-volume change report."""
from __future__ import annotations

import json
from pathlib import Path

from .model import Manifest
from .planner import PlanningConflict, RevisionPlan


def delivery_report(manifest: Manifest, plan: RevisionPlan) -> dict:
    """Build a JSON-serialisable description of the accepted revision."""
    groups = []
    for group in manifest.groups:
        group_plan = plan.group_plans[group.group_id]
        chosen = group_plan.chosen
        groups.append({
            "group_id": group.group_id,
            "data_group": group.data_group,
            "channel_group": group.channel_group,
            "layout": group.layout,
            "coverage_segment": list(plan.coverage_segments[group.group_id]),
            "channels": [
                {
                    "channel_name": assignment.channel_name,
                    "selected_bit_position": assignment.bit_position,
                    "target_validity_pattern": list(assignment.target_pattern),
                    "changed_samples": list(assignment.changed_samples),
                    "bit_shared": sum(
                        1 for other in chosen.assignments
                        if other.bit_position == assignment.bit_position
                    ) > 1,
                }
                for assignment in chosen.assignments
            ],
        })
    return {
        "retransmit_block_count": plan.retransmit_count,
        "retransmit_blocks": plan.retransmit_blocks,
        "block_edits": {
            block_id: [
                {
                    "byte_offset": edit.byte_offset + (edit.bit_position >> 3),
                    "bit_position": edit.bit_position,
                    "target_value": edit.target_value,
                    "block_role": edit.block_role,
                }
                for edit in edits
            ]
            for block_id, edits in plan.block_edits.items()
        },
        "groups": groups,
    }


def write_delivery_bundle(manifest: Manifest, plan: RevisionPlan,
                          output_dir: str | Path,
                          build_summary: dict | None = None) -> dict:
    """Persist the planning list and (optionally) the revision build summary."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    plan_path = output_dir / "revision_plan.json"
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump(delivery_report(manifest, plan), handle, indent=2)

    paths = {"plan": str(plan_path)}
    if build_summary is not None:
        summary_path = output_dir / "revision_build.json"
        with open(summary_path, "w", encoding="utf-8") as handle:
            json.dump(build_summary, handle, indent=2)
        paths["build_summary"] = str(summary_path)
    return paths


def conflict_report(error: PlanningConflict) -> dict:
    """Serialise the conflicts preventing a legal arrangement."""
    return {
        "status": "conflict",
        "conflicts": [
            {
                "group_id": conflict.group_id,
                "channels": list(conflict.channels),
                "samples": list(conflict.samples),
                "reason": conflict.reason,
            }
            for conflict in error.conflicts
        ],
    }
