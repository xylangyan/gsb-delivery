"""Memory-conscious mask geometry for night-shift delivery planning."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is available in the test env
    np = None  # type: ignore[assignment]


Point = dict[str, int | str]


def coordinate(point: Any, default_frame: int | None = None) -> tuple[int, int, int]:
    """Return a canonical 1-based ``(frame, row, column)`` tuple."""
    if isinstance(point, Mapping):
        frame = point.get("frame", point.get("frame_number", default_frame))
        row = point.get("row", point.get("row_index"))
        column = point.get("column", point.get("col", point.get("column_index")))
        if frame is None or row is None or column is None:
            raise ValueError(f"invalid coordinate: {point!r}")
        return int(frame), int(row), int(column)

    values = tuple(point)
    if len(values) == 3:
        return int(values[0]), int(values[1]), int(values[2])
    if len(values) == 2 and default_frame is not None:
        return int(default_frame), int(values[0]), int(values[1])
    raise ValueError(f"invalid coordinate: {point!r}")


def pack_pixel(row: int, column: int, columns: int, *, one_based: bool = True) -> int:
    if one_based:
        row -= 1
        column -= 1
    return row * columns + column


def unpack_pixel(packed: int, columns: int) -> tuple[int, int]:
    row0, column0 = divmod(int(packed), int(columns))
    return row0 + 1, column0 + 1


@dataclass
class OverlapWitness:
    segments: tuple[str, str]
    frame: int
    row: int
    column: int
    source_sop_instance_uid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": list(self.segments),
            "frame": self.frame,
            "row": self.row,
            "column": self.column,
            "coordinate": [self.frame, self.row, self.column],
            "source_sop_instance_uid": self.source_sop_instance_uid,
        }


@dataclass
class MaskCatalog:
    """Sparse and frame-array masks in original-image coordinates."""

    rows: int
    columns: int
    frame_numbers: list[int] = field(default_factory=list)
    source_sop_instance_uids: dict[int, str] = field(default_factory=dict)
    point_sets: dict[str, dict[int, set[int]]] = field(default_factory=dict)
    frame_arrays: dict[str, dict[int, Any]] = field(default_factory=dict)
    provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rows = int(self.rows)
        self.columns = int(self.columns)
        if not self.frame_numbers:
            frame_count = int(getattr(self, "number_of_frames", 0) or 0)
            if frame_count:
                self.frame_numbers = list(range(1, frame_count + 1))

    @property
    def number_of_frames(self) -> int:
        return len(self.frame_numbers)

    def source_uid(self, frame: int) -> str | None:
        return self.source_sop_instance_uids.get(frame) or self.source_sop_instance_uids.get(
            0
        )

    def segment_ids(self) -> list[str]:
        values = set(self.point_sets) | set(self.frame_arrays)
        return sorted(str(value) for value in values)

    def add_point(
        self, segment: str, frame: int, row: int, column: int, *, one_based: bool = True
    ) -> None:
        segment = str(segment)
        frame = int(frame)
        packed = pack_pixel(row, column, self.columns, one_based=one_based)
        self.point_sets.setdefault(segment, {}).setdefault(frame, set()).add(packed)

    def add_coordinates(
        self,
        segment: str,
        coordinates: Iterable[Any],
        *,
        frame: int | None = None,
        one_based: bool = True,
    ) -> None:
        for value in coordinates:
            resolved_frame, row, column = coordinate(value, frame)
            if not one_based and frame is not None and len(tuple(value)) == 2:
                # Explicit zero-based arrays are uncommon in public manifests;
                # callers normally use the documented 1-based coordinates.
                pass
            self.add_point(
                segment, resolved_frame, row, column, one_based=one_based
            )

    def add_frame_points(
        self, segment: str, frames: Mapping[int | str, Iterable[Any]]
    ) -> None:
        for frame, points in frames.items():
            frame_number = int(frame)
            for row_column in points:
                _, row, column = coordinate(row_column, frame_number)
                self.add_point(segment, frame_number, row, column)

    def add_array(self, segment: str, array: Any) -> None:
        if np is None:
            raise RuntimeError("NumPy is required to register array masks")
        array = np.asarray(array)
        segment = str(segment)
        if array.ndim == 2:
            frames = self.frame_numbers or [1]
            self.frame_arrays.setdefault(segment, {})[frames[0]] = array
            return
        if array.ndim != 3:
            raise ValueError("mask arrays must be 2D or 3D (frames, rows, columns)")
        if not self.frame_numbers:
            self.frame_numbers = list(range(1, array.shape[0] + 1))
        for index, frame in enumerate(self.frame_numbers):
            if index < array.shape[0]:
                self.frame_arrays.setdefault(segment, {})[int(frame)] = array[index]

    def add_mask_value(self, segment: str, mask: Any) -> None:
        if mask is None:
            return
        if isinstance(mask, Mapping):
            if "frames" in mask:
                self.add_frame_points(segment, mask["frames"])
                return
            if "coordinates" in mask:
                self.add_coordinates(segment, mask["coordinates"])
                return
            if "points" in mask:
                self.add_coordinates(segment, mask["points"])
                return
        if isinstance(mask, str):
            raise ValueError(f"unsupported mask reference for {segment!r}: {mask}")
        if np is not None and hasattr(mask, "shape"):
            self.add_array(segment, mask)
            return
        if isinstance(mask, Sequence) and mask and not isinstance(mask[0], (int, float)):
            first = mask[0]
            if isinstance(first, Sequence) and len(first) == 2:
                self.add_coordinates(segment, mask, frame=1)
                return
            if isinstance(first, Sequence) and len(first) == 3:
                self.add_coordinates(segment, mask)
                return
        if isinstance(mask, Iterable):
            self.add_coordinates(segment, mask)

    def _packed_for(self, segment: str, frame: int) -> set[int]:
        result = set(self.point_sets.get(segment, {}).get(frame, set()))
        array = self.frame_arrays.get(segment, {}).get(frame)
        if array is not None and np is not None:
            rows, columns = np.nonzero(array)
            for row0, column0 in zip(rows.tolist(), columns.tolist(), strict=False):
                result.add(row0 * self.columns + column0)
        return result

    def overlap(
        self,
        first: str,
        second: str,
        *,
        max_witnesses: int | None = 10_000,
    ) -> list[OverlapWitness]:
        first = str(first)
        second = str(second)
        frames = set(self.point_sets.get(first, {}))
        frames.update(self.point_sets.get(second, {}))
        frames.update(self.frame_arrays.get(first, {}))
        frames.update(self.frame_arrays.get(second, {}))
        witnesses: list[OverlapWitness] = []

        first_arrays = self.frame_arrays.get(first, {})
        second_arrays = self.frame_arrays.get(second, {})
        for frame in sorted(int(value) for value in frames):
            first_array = first_arrays.get(frame)
            second_array = second_arrays.get(frame)
            if first_array is not None and second_array is not None and np is not None:
                intersection = np.logical_and(first_array != 0, second_array != 0)
                rows, columns = np.nonzero(intersection)
                coordinates = [
                    (int(row) + 1, int(column) + 1)
                    for row, column in zip(
                        rows.tolist(), columns.tolist(), strict=False
                    )
                ]
            else:
                first_pixels = self._packed_for(first, frame)
                second_pixels = self._packed_for(second, frame)
                coordinates = [
                    unpack_pixel(pixel, self.columns)
                    for pixel in sorted(first_pixels & second_pixels)
                ]

            for row, column in coordinates:
                witnesses.append(
                    OverlapWitness(
                        (first, second),
                        frame,
                        row,
                        column,
                        self.source_uid(frame),
                    )
                )
                if max_witnesses is not None and len(witnesses) >= max_witnesses:
                    return witnesses
        return witnesses

    def paint(self, segment: str, volume: Any, label: int) -> int:
        """Paint one complete mask into a frame-first label volume."""
        segment = str(segment)
        painted = 0
        for frame in self.frame_numbers:
            frame_index = int(frame) - 1
            if frame_index >= volume.shape[0]:
                continue
            pixels = self.point_sets.get(segment, {}).get(int(frame), set())
            if pixels:
                packed = np.asarray(sorted(pixels), dtype=np.intp)
                rows0, columns0 = divmod(packed, self.columns)
                volume[frame_index, rows0, columns0] = label
                painted += len(packed)
            array = self.frame_arrays.get(segment, {}).get(int(frame))
            if array is not None and np is not None:
                mask = np.asarray(array) != 0
                volume[frame_index][mask] = label
                painted += int(np.count_nonzero(mask))
        return painted


def make_catalog(artifact: Mapping[str, Any]) -> MaskCatalog:
    rows = int(artifact.get("rows", artifact.get("Rows", 0)) or 0)
    columns = int(
        artifact.get("columns", artifact.get("Columns", artifact.get("cols", 0))) or 0
    )
    raw_frames = artifact.get(
        "number_of_frames",
        artifact.get("frame_count", artifact.get("frames", 1)),
    )
    if isinstance(raw_frames, Sequence) and not isinstance(raw_frames, (str, bytes, int)):
        frame_count = len(raw_frames)
    else:
        frame_count = int(raw_frames or 1)
    frame_numbers = artifact.get("frame_numbers")
    if frame_numbers is None:
        if isinstance(artifact.get("frames"), Sequence) and not isinstance(
            artifact.get("frames"), (str, bytes, int)
        ):
            frame_numbers = [int(value) for value in artifact["frames"]]
        elif isinstance(artifact.get("frame_numbers"), Sequence):
            frame_numbers = [int(value) for value in artifact["frame_numbers"]]
        else:
            frame_numbers = list(range(1, frame_count + 1))

    source_uid = artifact.get(
        "source_sop_instance_uid",
        artifact.get("source_sop_uid", artifact.get("sop_instance_uid")),
    )
    source_uids = {0: source_uid} if source_uid else {}
    for frame, uid in (artifact.get("frame_source_sop_uids") or {}).items():
        source_uids[int(frame)] = uid

    catalog = MaskCatalog(
        rows=rows,
        columns=columns,
        frame_numbers=[int(value) for value in frame_numbers],
        source_sop_instance_uids=source_uids,
    )

    segments = artifact.get("segments", {})
    if isinstance(segments, Mapping):
        for segment_id, segment_data in segments.items():
            catalog.provenance[str(segment_id)] = _provenance(segment_data)
            if isinstance(segment_data, Mapping):
                mask = (
                    segment_data.get("mask")
                    if "mask" in segment_data
                    else segment_data
                )
                catalog.add_mask_value(str(segment_id), mask)
            else:
                catalog.add_mask_value(str(segment_id), segment_data)
    elif isinstance(segments, Sequence):
        for segment_data in segments:
            segment_id = str(
                segment_data.get("id")
                or segment_data.get("segment_id")
                or segment_data.get("name")
            )
            catalog.provenance[segment_id] = _provenance(segment_data)
            catalog.add_mask_value(segment_id, segment_data)

    return catalog


def _provenance(segment_data: Any) -> list[dict[str, Any]]:
    if not isinstance(segment_data, Mapping):
        return []
    sources = segment_data.get("sources") or segment_data.get("provenance")
    if isinstance(sources, Sequence):
        return [dict(item) for item in sources if isinstance(item, Mapping)]
    vendor = segment_data.get("vendor_segment_number")
    if vendor is None:
        return []
    return [
        {
            "seg_instance": segment_data.get("seg_instance"),
            "vendor_segment_number": vendor,
            "vendor_segment_label": segment_data.get("vendor_segment_label"),
        }
    ]
