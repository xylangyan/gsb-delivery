"""High-level orchestration of the two-stage revision workflow."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import Manifest, parse_manifest
from .planner import PlanningConflict, RevisionPlan, plan_revision
from .rebuild import build_revised_volume
from .report import conflict_report, delivery_report, write_delivery_bundle


def run_planning(manifest_source: dict | str | Path) -> tuple[Manifest, RevisionPlan | None,
                                                               dict | None]:
    """Plan from a manifest mapping or JSON file without opening the original volume.

    Returns ``(manifest, plan, None)`` on success or ``(manifest, None, report)``
    where ``report`` is the serialisable conflict description.
    """
    manifest = parse_manifest(manifest_source)
    try:
        plan = plan_revision(manifest)
    except PlanningConflict as error:
        return manifest, None, conflict_report(error)
    return manifest, plan, None


def run_revision(manifest_source: dict | str | Path,
                 source_volume: str | Path,
                 output_volume: str | Path,
                 output_dir: str | Path | None = None) -> dict[str, Any]:
    """Full workflow: plan, then (if legal) build the revised volume.

    The original volume is opened only after a legal minimum-block plan exists.
    The returned dictionary always carries ``status`` (``planned`` or
    ``conflict``) plus the delivery report; on success it also includes the
    build summary and, when ``output_dir`` is given, the written bundle paths.
    """
    manifest, plan, conflict = run_planning(manifest_source)
    if plan is None:
        return {"status": "conflict", "conflict": conflict}

    result: dict[str, Any] = {
        "status": "planned",
        "report": delivery_report(manifest, plan),
    }
    build_summary = build_revised_volume(
        manifest, plan, source_volume, output_volume)
    result["build_summary"] = build_summary
    if output_dir is not None:
        result["bundle_paths"] = write_delivery_bundle(
            manifest, plan, output_dir, build_summary=build_summary)
    return result
