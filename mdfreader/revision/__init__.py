"""MDF4 invalidation-bit revision toolkit.

Two-stage workflow matching the archive process:

1. :func:`plan_revision` consumes only the work-order manifest (no original
   data blocks) and produces the minimum set of physical data blocks to
   retransmit, or a conflict report.
2. :func:`build_revised_volume` opens the original volume once the plan is
   accepted and writes a revised volume that reuses every unselected block
   byte-for-byte and only rebuilds the billed data blocks plus link / channel
   flag updates.
"""
from __future__ import annotations

from .model import (
    COLUMN,
    INTERLEAVED,
    Manifest,
    ManifestError,
    RepairChannel,
    RepairGroup,
    parse_manifest,
)
from .planner import (
    BitAssignment,
    ByteEdit,
    Conflict,
    GroupOption,
    PlanningConflict,
    RevisionPlan,
    plan_revision,
)
from .rebuild import (
    ChannelFlagUpdate,
    RevisionBuildError,
    build_revised_volume,
)
from .report import (
    conflict_report,
    delivery_report,
    write_delivery_bundle,
)
from .pipeline import run_planning, run_revision

__all__ = [
    "COLUMN",
    "INTERLEAVED",
    "Manifest",
    "ManifestError",
    "RepairChannel",
    "RepairGroup",
    "parse_manifest",
    "BitAssignment",
    "ByteEdit",
    "Conflict",
    "GroupOption",
    "PlanningConflict",
    "RevisionPlan",
    "plan_revision",
    "ChannelFlagUpdate",
    "RevisionBuildError",
    "build_revised_volume",
    "conflict_report",
    "delivery_report",
    "write_delivery_bundle",
    "run_planning",
    "run_revision",
]
