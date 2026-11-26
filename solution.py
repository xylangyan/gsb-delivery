"""Convenience entry point for the dual-column shared position table."""

from numpy_groupies.shared_position_table import (
    Archive,
    ColumnRead,
    EvidencePoint,
    RegularSegment,
    SharedPositionTable,
    WindowBatch,
    WindowEvidence,
    WindowRead,
    analyze_window,
    build_batches,
    build_position_table,
)

SharedLocationTable = SharedPositionTable
DualColumnTable = SharedPositionTable
LocationTable = SharedPositionTable

build_shared_position_table = build_position_table
build_shared_table = build_position_table
build_location_table = build_position_table
build_shared_location_table = build_position_table


def read_back(
    table: SharedPositionTable,
    *,
    start: int,
    length: int,
    group: int,
) -> WindowRead:
    """Read both columns through their shared or column-specific slots."""
    return table.read(start=start, length=length, group=group)


def read_column(
    table: SharedPositionTable,
    column: str,
    *,
    start: int,
    length: int,
    group: int,
) -> ColumnRead:
    """Read one old column name (``nanmedian`` or ``nanquantile``)."""
    return table.read_column(column, start=start, length=length, group=group)


def load_table(path: str, archive: Archive | None = None) -> SharedPositionTable:
    """Load an NPZ location table for the legacy reader."""
    return SharedPositionTable.load_npz(path, archive)


__all__ = [
    "Archive",
    "ColumnRead",
    "DualColumnTable",
    "EvidencePoint",
    "LocationTable",
    "RegularSegment",
    "SharedLocationTable",
    "SharedPositionTable",
    "WindowBatch",
    "WindowEvidence",
    "WindowRead",
    "analyze_window",
    "build_batches",
    "build_location_table",
    "build_shared_location_table",
    "build_shared_position_table",
    "build_shared_table",
    "build_position_table",
    "load_table",
    "read_back",
    "read_column",
]
