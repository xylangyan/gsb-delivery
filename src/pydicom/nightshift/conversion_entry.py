"""Stable conversion entry points for pydicom's read/write workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .delivery import deliver_plan, preview_delivery
from .dicom_io import read_binary_seg, read_labelmap, write_labelmap
from .planner import plan_batch


def convert_seg_to_labelmap(
    source: Any = None,
    output_path: str | Path | None = None,
    segment_map: dict[str | int, str] | None = None,
    artifact: dict[str, Any] | None = None,
    file_plan: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | Any:
    """Read a BINARY SEG through pydicom and write a LABELMAP.

    With only ``source`` and ``segment_map`` this returns the canonical mask
    catalog.  Provide ``output_path``/``file_plan`` or an artifact to write a
    DICOM Label Map.
    """
    segment_map = segment_map or kwargs.pop("mapping", None)
    artifact_id = kwargs.pop("artifact_id", "SEG-CONVERSION")
    if source is not None and artifact is None:
        catalog = read_binary_seg(source, segment_map=segment_map, **kwargs)
        if output_path is None:
            return catalog
        artifact = {
            "artifact_id": artifact_id,
            "rows": catalog.rows,
            "columns": catalog.columns,
            "frame_numbers": catalog.frame_numbers,
            "segments": {
                segment: {
                    "frames": {
                        frame: [
                            list(_unpack(pixel, catalog.columns))
                            for pixel in pixels
                        ]
                        for frame, pixels in frames.items()
                    }
                }
                for segment, frames in catalog.point_sets.items()
            },
        }
        segments = list(catalog.segment_ids())
        file_plan = file_plan or {
            "file_index": 0,
            "segments": segments,
            "label_values": {
                segment: index for index, segment in enumerate(segments, start=1)
            },
        }
        return write_labelmap(
            output_path,
            artifact=artifact,
            file_plan=file_plan,
            catalog=catalog,
        )

    if artifact is None:
        raise TypeError("provide artifact/file_plan or source with segment_map")
    if output_path is None:
        raise TypeError("output_path is required when writing a label map")
    return write_labelmap(
        output_path,
        artifact=artifact,
        file_plan=file_plan or _default_file_plan(artifact),
        **kwargs,
    )


def plan_labelmap_delivery(
    manifest: dict[str, Any] | str | Path | None = None,
    *,
    quota: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Preview the highest-scoring arrangement without building pixel volumes."""
    if isinstance(manifest, (str, Path)):
        return preview_delivery(manifest, quota=quota)
    return plan_batch(manifest, quota=quota, **kwargs)


def deliver_labelmaps(
    manifest: dict[str, Any] | str | Path,
    *,
    output_dir: str | Path | None = None,
    quota: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return deliver_plan(manifest, output_dir=output_dir, quota=quota, **kwargs)


def _default_file_plan(artifact: dict[str, Any]) -> dict[str, Any]:
    segments = artifact.get("segments", {})
    if isinstance(segments, dict):
        segment_ids = list(segments)
    else:
        segment_ids = [str(item.get("id") or item.get("name")) for item in segments]
    return {
        "file_index": 0,
        "segments": segment_ids,
        "label_values": {
            segment: label for label, segment in enumerate(sorted(segment_ids), start=1)
        },
    }


def _unpack(packed: int, columns: int) -> tuple[int, int]:
    row0, column0 = divmod(int(packed), int(columns))
    return row0 + 1, column0 + 1


convert_binary_seg_to_labelmap = convert_seg_to_labelmap
seg_to_labelmap = convert_seg_to_labelmap
labelmap_from_seg = convert_seg_to_labelmap

__all__ = [
    "convert_binary_seg_to_labelmap",
    "convert_seg_to_labelmap",
    "deliver_labelmaps",
    "labelmap_from_seg",
    "plan_labelmap_delivery",
    "read_binary_seg",
    "read_labelmap",
    "seg_to_labelmap",
    "write_labelmap",
]
