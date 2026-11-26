"""Shared storage locations for temporal ``nanmedian``/``nanquantile`` columns.

The two ns-duration columns use the same order positions at ``q=0.5``:

* an odd valid count uses the single middle rank;
* an even valid count uses the two middle ranks.

Their saved tick values are not always the same, because temporal median rounds
the exact integer sum half to even while the temporal quantile reproduces NumPy's
float64 interpolation.  This module records one slot when the resulting int64
bits are equal and two slots otherwise, together with the global original rows
behind the sorted neighbour ranks.

Long start ranges are stored as intervals that select the same valid rows.  A
Fenwick tree supplies the current order positions during one monotone sweep, so
building the table does not sort the whole window again for every start.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
import numpy.typing as npt

from numpy_groupies.npg_numpy import aggregate as numpy_aggregate
from numpy_groupies.npg_numpy.specials import _temporal_median, _temporal_quantile
from numpy_groupies.temporal import NAT_TICK

ColumnName = Literal["nanmedian", "nanquantile"]

_CELL_DTYPE = np.dtype(
    [
        ("first_start", np.int64),
        ("last_start", np.int64),
        ("valid_count", np.int64),
        ("shared", np.bool_),
        ("median_slot", np.int64),
        ("quantile_slot", np.int64),
        ("lower_rank", np.int64),
        ("upper_rank", np.int64),
        ("lower_row", np.int64),
        ("upper_row", np.int64),
        ("lower_tick", np.int64),
        ("upper_tick", np.int64),
        ("median_tick", np.int64),
        ("quantile_tick", np.int64),
    ]
)

_RUN_DTYPE = np.dtype(
    [
        ("first_start", np.int64),
        ("last_start", np.int64),
        ("first_interval", np.int64),
        ("last_interval", np.int64),
        ("shared", np.bool_),
    ]
)


def round_half_even_two(total: int) -> int:
    """Round an integer total divided by two to an even quotient."""
    quotient, remainder = divmod(int(total), 2)
    if remainder == 1 and quotient % 2 == 1:
        quotient += 1
    return quotient


def median_tick_of_neighbours(lower_tick: int, upper_tick: int) -> int:
    """The exact temporal median tick used by the NumPy backend."""
    return round_half_even_two(int(lower_tick) + int(upper_tick))


def quantile_half_tick_of_neighbours(
    lower_tick: int,
    upper_tick: int,
    valid_count: int,
) -> int:
    """Reproduce the temporal linear ``q=0.5`` interpolation tick.

    The position arithmetic is kept explicit rather than simplified: for an even
    count it reaches NumPy's upper-neighbour branch with ``gamma == 0.5``.
    """
    position = np.float64(0.5) * np.float64(int(valid_count) - 1)
    lower_index = int(np.floor(position))
    upper_index = int(np.ceil(position))
    gamma = position - np.float64(lower_index)
    difference = int(upper_tick) - int(lower_tick)

    if lower_index == upper_index:
        return int(upper_tick)
    if gamma < np.float64(0.5):
        return int(lower_tick) + int(np.float64(difference) * np.float64(gamma))
    return int(upper_tick) - int(np.float64(difference) * np.float64(np.float64(1.0) - gamma))


def ticks_from_values(values: npt.ArrayLike) -> npt.NDArray[np.int64]:
    """Convert ns durations or raw ticks to the normalized int64 tick store."""
    array = np.asarray(values)
    if array.dtype.kind in "mM":
        return array.astype("timedelta64[ns]").astype(np.int64)
    if np.issubdtype(array.dtype, np.integer):
        return array.astype(np.int64, copy=False)
    raise TypeError("duration values must be timedelta64[ns], datetime64[ns], or integer ticks")


@dataclass(frozen=True)
class SourceRef:
    """A registered regular-archive slot that maps to a global original row."""

    segment: int | str
    slot: int


@dataclass(frozen=True)
class RegularSegment:
    """One equal-step archive segment.

    Scheduled slot ``k`` maps to global original row
    ``start_row + k * stride``.  ``missing_slots`` are interruptions with no
    written original row; an actual NaT write stays in ``values``/``ticks`` or
    is named in ``nat_slots``.
    """

    start_row: int
    stride: int
    group: int
    values: npt.ArrayLike | None = None
    ticks: npt.ArrayLike | None = None
    count: int | None = None
    missing_slots: npt.ArrayLike = ()
    nat_slots: npt.ArrayLike = ()
    name: int | str | None = None

    def materialize(self) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64], npt.NDArray[np.int64]]:
        if self.ticks is not None:
            ticks = np.asarray(self.ticks, dtype=np.int64)
        elif self.values is not None:
            ticks = ticks_from_values(self.values)
        elif self.count is not None:
            ticks = np.zeros(int(self.count), dtype=np.int64)
        else:
            raise ValueError("RegularSegment needs values, ticks, or count")

        ticks = np.asarray(ticks, dtype=np.int64)
        count = int(self.count) if self.count is not None else int(ticks.size)
        if ticks.size == 0 and count > 0:
            ticks = np.zeros(count, dtype=np.int64)
        if ticks.size != count:
            raise ValueError("segment count does not match its value array")
        if self.stride <= 0:
            raise ValueError("segment stride must be positive")

        nat_slots = np.asarray(self.nat_slots, dtype=np.int64)
        if nat_slots.size:
            ticks[nat_slots] = NAT_TICK

        missing = np.asarray(self.missing_slots, dtype=np.int64)
        present = np.ones(count, dtype=bool)
        if missing.size:
            if np.any(missing < 0) or np.any(missing >= count):
                raise IndexError("missing segment slot is outside the segment")
            present[missing] = False

        slots = np.flatnonzero(present).astype(np.int64)
        rows = int(self.start_row) + slots * int(self.stride)
        groups = np.full(rows.shape, int(self.group), dtype=np.int64)
        return rows, groups, ticks[slots]


@dataclass(frozen=True)
class EvidencePoint:
    """One sorted neighbour position and the original row that supplies it."""

    rank: int
    row: int
    tick: int

    @property
    def value(self) -> np.timedelta64:
        return np.timedelta64(int(self.tick), "ns")


@dataclass(frozen=True)
class WindowEvidence:
    """Order positions, original-row evidence, and both computed ticks."""

    start: int
    length: int
    group: int
    valid_count: int
    lower_rank: int
    upper_rank: int
    points: tuple[EvidencePoint, ...]
    median_tick: int
    quantile_tick: int

    @property
    def empty(self) -> bool:
        return self.valid_count == 0

    @property
    def shared(self) -> bool:
        return self.median_tick == self.quantile_tick

    @property
    def sorted_positions(self) -> tuple[int, ...]:
        if self.valid_count == 0:
            return ()
        if self.lower_rank == self.upper_rank:
            return (int(self.lower_rank),)
        return (int(self.lower_rank), int(self.upper_rank))

    @property
    def evidence_rows(self) -> tuple[int, ...]:
        return tuple(point.row for point in self.points)

    @property
    def median_value(self) -> np.timedelta64:
        return np.timedelta64(int(self.median_tick), "ns")

    @property
    def quantile_value(self) -> np.timedelta64:
        return np.timedelta64(int(self.quantile_tick), "ns")


class Archive:
    """Original rows in global row order, with group labels and ns ticks."""

    def __init__(
        self,
        rows: npt.ArrayLike | None = None,
        groups: npt.ArrayLike | None = None,
        ticks: npt.ArrayLike | None = None,
        *,
        values: npt.ArrayLike | None = None,
        a: npt.ArrayLike | None = None,
        group_idx: npt.ArrayLike | None = None,
        row_id: npt.ArrayLike | None = None,
        group_count: int | None = None,
        segments: Sequence[RegularSegment] = (),
    ) -> None:
        if rows is None:
            rows = row_id
        if groups is None:
            groups = group_idx
        if values is None:
            values = a
        if ticks is None:
            if values is None:
                raise ValueError("Archive needs ticks or values")
            tick_array = ticks_from_values(values)
        else:
            tick_array = np.asarray(ticks, dtype=np.int64)

        n = int(tick_array.size)
        row_array = np.arange(n, dtype=np.int64) if rows is None else np.asarray(rows, dtype=np.int64)
        group_array = np.zeros(n, dtype=np.int64) if groups is None else np.asarray(groups, dtype=np.int64)
        if row_array.shape != (n,) or group_array.shape != (n,):
            raise ValueError("rows, groups, and values must be one-dimensional and equal length")
        if group_array.size and np.any(group_array < 0):
            raise ValueError("group labels must be non-negative")

        order = np.argsort(row_array, kind="stable")
        self.rows = row_array[order]
        self.groups = group_array[order]
        self.ticks = tick_array[order]
        self.segments = tuple(segments)
        self.group_count = (
            int(group_count) if group_count is not None else (int(self.groups.max()) + 1 if self.groups.size else 0)
        )
        if self.group_count < 0 or (self.groups.size and int(self.groups.max()) >= self.group_count):
            raise ValueError("group_count does not cover every group label")

    @classmethod
    def from_arrays(
        cls,
        values: npt.ArrayLike,
        group_idx: npt.ArrayLike | None = None,
        row_id: npt.ArrayLike | None = None,
        *,
        group_count: int | None = None,
    ) -> "Archive":
        return cls(rows=row_id, groups=group_idx, values=values, group_count=group_count)

    @classmethod
    def from_segments(
        cls,
        segments: Sequence[RegularSegment],
        *,
        group_count: int | None = None,
    ) -> "Archive":
        row_parts: list[npt.NDArray[np.int64]] = []
        group_parts: list[npt.NDArray[np.int64]] = []
        tick_parts: list[npt.NDArray[np.int64]] = []
        max_group = -1
        for segment in segments:
            rows, groups, ticks = segment.materialize()
            row_parts.append(rows)
            group_parts.append(groups)
            tick_parts.append(ticks)
            max_group = max(max_group, int(segment.group))
        if row_parts:
            rows = np.concatenate(row_parts)
            groups = np.concatenate(group_parts)
            ticks = np.concatenate(tick_parts)
        else:
            rows = groups = ticks = np.empty(0, dtype=np.int64)
        count = group_count if group_count is not None else max(0, max_group + 1)
        return cls(rows=rows, groups=groups, ticks=ticks, group_count=count, segments=segments)

    def window_mask(self, start: int, length: int) -> npt.NDArray[np.bool_]:
        if length < 0:
            raise ValueError("window length must be non-negative")
        end = int(start) + int(length)
        return (self.rows >= int(start)) & (self.rows < end)

    def source_of_row(self, row: int) -> tuple[SourceRef, ...]:
        """Map a global row back to every registered regular segment slot."""
        refs: list[SourceRef] = []
        for index, segment in enumerate(self.segments):
            delta = int(row) - int(segment.start_row)
            if delta < 0 or delta % int(segment.stride) != 0:
                continue
            slot = delta // int(segment.stride)
            if segment.count is not None:
                count = int(segment.count)
            elif segment.ticks is not None:
                count = int(np.asarray(segment.ticks).size)
            elif segment.values is not None:
                count = int(np.asarray(segment.values).size)
            else:
                continue
            if not 0 <= slot < count:
                continue
            missing = np.asarray(segment.missing_slots, dtype=np.int64)
            if missing.size and int(slot) in {int(x) for x in missing}:
                continue
            segment_name = index if segment.name is None else segment.name
            refs.append(SourceRef(segment_name, int(slot)))
        return tuple(refs)


def analyze_window(archive: Archive, start: int, length: int, group: int) -> WindowEvidence:
    """Sort one window/group's valid rows and compute both exact tick results."""
    mask = archive.window_mask(start, length) & (archive.groups == int(group))
    rows = archive.rows[mask]
    ticks = archive.ticks[mask]
    good = ticks != NAT_TICK
    rows = rows[good]
    ticks = ticks[good]
    count = int(ticks.size)

    if count == 0:
        return WindowEvidence(int(start), int(length), int(group), 0, -1, -1, (), NAT_TICK, NAT_TICK)

    order = np.lexsort((np.arange(count), rows, ticks))
    sorted_rows = rows[order]
    sorted_ticks = ticks[order]

    if count % 2 == 1:
        rank = count // 2
        tick = int(sorted_ticks[rank])
        point = EvidencePoint(rank, int(sorted_rows[rank]), tick)
        return WindowEvidence(start, length, group, count, rank, rank, (point,), tick, tick)

    lower_rank = count // 2 - 1
    upper_rank = count // 2
    lower_tick = int(sorted_ticks[lower_rank])
    upper_tick = int(sorted_ticks[upper_rank])
    points = (
        EvidencePoint(lower_rank, int(sorted_rows[lower_rank]), lower_tick),
        EvidencePoint(upper_rank, int(sorted_rows[upper_rank]), upper_tick),
    )
    median_tick = median_tick_of_neighbours(lower_tick, upper_tick)
    quantile_tick = quantile_half_tick_of_neighbours(lower_tick, upper_tick, count)
    return WindowEvidence(
        int(start),
        int(length),
        int(group),
        count,
        lower_rank,
        upper_rank,
        points,
        median_tick,
        quantile_tick,
    )


class Fenwick:
    """Count selected unique ``(tick, row)`` keys and find the kth key."""

    def __init__(self, size: int) -> None:
        self.n = int(size)
        self.tree = np.zeros(self.n + 1, dtype=np.int64)

    def add(self, index: int, delta: int) -> None:
        i = int(index) + 1
        while i <= self.n:
            self.tree[i] += delta
            i += i & -i

    def kth(self, k: int) -> int:
        target = int(k)
        position = 0
        step = 1 << (self.n.bit_length() - 1)
        while step:
            candidate = position + step
            if candidate <= self.n and int(self.tree[candidate]) <= target:
                position = candidate
                target -= int(self.tree[candidate])
            step >>= 1
        return position


@dataclass(frozen=True)
class WindowBatch:
    """Allowed lengths and one inclusive start range for a batch."""

    lengths: tuple[int, ...]
    first_start: int
    last_start: int
    name: int | str | None = None

    @classmethod
    def make(
        cls,
        lengths: int | Sequence[int],
        start_range: tuple[int, int] | range | Sequence[int],
        name: int | str | None = None,
    ) -> "WindowBatch":
        length_tuple: tuple[int, ...]
        if isinstance(lengths, int):
            length_tuple = (int(lengths),)
        else:
            length_tuple = tuple(int(x) for x in lengths)
        if isinstance(start_range, range):
            first_start = int(start_range.start)
            last_start = int(start_range.stop - 1)
        else:
            if len(start_range) != 2:
                raise ValueError("start_range must be (first_start, last_start)")
            first_start, last_start = int(start_range[0]), int(start_range[1])
        if not length_tuple or any(x <= 0 for x in length_tuple):
            raise ValueError("window lengths must be positive")
        if last_start < first_start:
            raise ValueError("last_start must be at least first_start")
        return cls(length_tuple, first_start, last_start, name)

    @property
    def window_count(self) -> int:
        return len(self.lengths) * (int(self.last_start) - int(self.first_start) + 1)


@dataclass(frozen=True)
class ColumnRead:
    """What an old reader needs for one named column."""

    column: ColumnName
    value: np.timedelta64
    slot: int
    location: str
    shared: bool
    sorted_positions: tuple[int, ...]
    evidence: tuple[EvidencePoint, ...]

    @property
    def evidence_rows(self) -> tuple[int, ...]:
        return tuple(point.row for point in self.evidence)

    def legacy_tuple(self) -> tuple[np.timedelta64, tuple[int, ...]]:
        return self.value, self.sorted_positions


@dataclass(frozen=True)
class WindowRead:
    start: int
    length: int
    group: int
    median: ColumnRead
    quantile: ColumnRead
    cell: np.void

    @property
    def shared(self) -> bool:
        return bool(self.cell["shared"])


@dataclass(frozen=True)
class WindowMismatch:
    start: int
    length: int
    group: int
    column: str
    expected_tick: int
    actual_tick: int


@dataclass(frozen=True)
class VerificationReport:
    checked_windows: int
    checked_groups: int
    mismatches: tuple[WindowMismatch, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches


class SharedPositionTable:
    """Compressed shared-slot map for ``(window length, group)`` pairs."""

    def __init__(
        self,
        archive: Archive | None,
        cells: dict[tuple[int, int], np.ndarray],
        slot_ticks: npt.NDArray[np.int64],
        batches: Sequence[WindowBatch] = (),
    ) -> None:
        self.archive = archive
        self.cells_by_length_group = dict(cells)
        self.slot_ticks = np.asarray(slot_ticks, dtype=np.int64)
        self.batches = tuple(batches)
        self.runs_by_length_group = {key: _build_runs(cells) for key, cells in self.cells_by_length_group.items()}

    @property
    def keys(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted(self.cells_by_length_group))

    def layout_runs(self, length: int, group: int) -> np.ndarray:
        return self.runs_by_length_group[(int(length), int(group))]

    def cells(self, length: int, group: int) -> np.ndarray:
        return cast(np.ndarray, self.cells_by_length_group[(int(length), int(group))])

    def locate_cell(self, start: int, length: int, group: int) -> np.void:
        cells = self.cells(int(length), int(group))
        starts = cells["first_start"]
        index = int(np.searchsorted(starts, int(start), side="right")) - 1
        if index < 0 or int(start) > int(cells[index]["last_start"]):
            raise KeyError(f"no registered interval for start={start}, length={length}, group={group}")
        return cast(np.void, cells[index])

    def _column_read(self, column: ColumnName, cell: np.void) -> ColumnRead:
        shared = bool(cell["shared"])
        if column == "nanmedian":
            slot = int(cell["median_slot"])
        else:
            slot = int(cell["quantile_slot"])
        tick = int(self.slot_ticks[slot])
        if cell["valid_count"] == 0:
            positions: tuple[int, ...] = ()
            evidence: tuple[EvidencePoint, ...] = ()
        elif int(cell["lower_rank"]) == int(cell["upper_rank"]):
            positions = (int(cell["lower_rank"]),)
            evidence = (EvidencePoint(int(cell["lower_rank"]), int(cell["lower_row"]), int(cell["lower_tick"])),)
        else:
            positions = (int(cell["lower_rank"]), int(cell["upper_rank"]))
            evidence = (
                EvidencePoint(int(cell["lower_rank"]), int(cell["lower_row"]), int(cell["lower_tick"])),
                EvidencePoint(int(cell["upper_rank"]), int(cell["upper_row"]), int(cell["upper_tick"])),
            )
        location = f"shared-slot:{slot}" if shared else f"{column}-slot:{slot}"
        return ColumnRead(
            column=column,
            value=np.timedelta64(tick, "ns"),
            slot=slot,
            location=location,
            shared=shared,
            sorted_positions=positions,
            evidence=evidence,
        )

    def read_column(
        self,
        column: ColumnName,
        *,
        start: int,
        length: int,
        group: int,
    ) -> ColumnRead:
        if column not in ("nanmedian", "nanquantile"):
            raise ValueError("column must be 'nanmedian' or 'nanquantile'")
        return self._column_read(column, self.locate_cell(start, length, group))

    def read(self, *, start: int, length: int, group: int) -> WindowRead:
        cell = self.locate_cell(start, length, group)
        return WindowRead(
            start=int(start),
            length=int(length),
            group=int(group),
            median=self._column_read("nanmedian", cell),
            quantile=self._column_read("nanquantile", cell),
            cell=cell,
        )

    def legacy_read(
        self,
        column: ColumnName,
        *,
        start: int,
        length: int,
        group: int,
    ) -> tuple[np.timedelta64, tuple[int, ...], int]:
        read = self.read_column(column, start=start, length=length, group=group)
        return read.value, read.sorted_positions, read.slot

    def save_npz(self, path: str) -> None:
        """Save the compact cell table and slot bank for an old reader."""
        key_dtype = np.dtype([("length", np.int64), ("group", np.int64)])
        keys = np.empty(len(self.cells_by_length_group), dtype=key_dtype)
        for index, (length, group) in enumerate(self.cells_by_length_group):
            keys[index] = (length, group)
        arrays: dict[str, np.ndarray] = {"slot_ticks": self.slot_ticks, "keys": keys}
        for (length, group), cells in self.cells_by_length_group.items():
            arrays[f"cell_{length}_{group}"] = cells
        np.savez_compressed(path, **arrays)  # type: ignore[arg-type]

    @classmethod
    def load_npz(cls, path: str, archive: Archive | None = None) -> "SharedPositionTable":
        """Load a table saved by :meth:`save_npz`; archive is only for re-verification."""
        with np.load(path, allow_pickle=False) as store:
            keys = store["keys"]
            cells = {
                (int(key["length"]), int(key["group"])): store[f"cell_{int(key['length'])}_{int(key['group'])}"].copy()
                for key in keys
            }
            slot_ticks = store["slot_ticks"].copy()
        return cls(archive, cells, slot_ticks)

    def separate_cells(self, length: int | None = None, group: int | None = None) -> list[np.void]:
        """Iterate cells where the two columns need separate slots."""
        result: list[np.void] = []
        for (cell_length, cell_group), cells in self.cells_by_length_group.items():
            if length is not None and cell_length != int(length):
                continue
            if group is not None and cell_group != int(group):
                continue
            result.extend(cells[~cells["shared"]])
        return result

    def backend_values(self, start: int, length: int) -> tuple[np.ndarray, np.ndarray]:
        """Run the selected global original rows through the NumPy backend."""
        if self.archive is None:
            raise ValueError("an Archive is required to recompute NumPy backend values")
        mask = self.archive.window_mask(start, length)
        groups = self.archive.groups[mask]
        ticks = self.archive.ticks[mask]
        values = ticks.view("timedelta64[ns]")
        size = self.archive.group_count
        if groups.size == 0:
            empty_groups = np.empty(0, dtype=np.int64)
            empty_ticks = np.empty(0, dtype=np.int64)
            median_ticks = _temporal_median(empty_groups, empty_ticks, size, NAT_TICK)
            quantile_ticks = _temporal_quantile(empty_groups, empty_ticks, size, NAT_TICK, 0.5, "linear")
            return median_ticks.view("timedelta64[ns]"), quantile_ticks.view("timedelta64[ns]")

        median = numpy_aggregate(groups, values, "nanmedian", size=size)
        quantile = numpy_aggregate(groups, values, "nanquantile", q=0.5, method="linear", size=size)
        return np.asarray(median), np.asarray(quantile)

    def verify_window(self, start: int, length: int) -> VerificationReport:
        if self.archive is None:
            raise ValueError("an Archive is required to recompute NumPy backend values")
        archive = self.archive
        median_values, quantile_values = self.backend_values(start, length)
        mismatches: list[WindowMismatch] = []
        groups_checked = 0
        for group in range(archive.group_count):
            evidence = analyze_window(archive, start, length, group)
            groups_checked += 1
            expected_median = int(np.asarray(median_values[group]).astype("timedelta64[ns]").astype(np.int64))
            expected_quantile = int(np.asarray(quantile_values[group]).astype("timedelta64[ns]").astype(np.int64))
            if expected_median != evidence.median_tick:
                mismatches.append(
                    WindowMismatch(start, length, group, "nanmedian", expected_median, evidence.median_tick)
                )
            if expected_quantile != evidence.quantile_tick:
                mismatches.append(
                    WindowMismatch(start, length, group, "nanquantile", expected_quantile, evidence.quantile_tick)
                )
            cell = self.locate_cell(start, length, group)
            if int(cell["median_tick"]) != evidence.median_tick:
                mismatches.append(
                    WindowMismatch(start, length, group, "table-median", evidence.median_tick, int(cell["median_tick"]))
                )
            if int(cell["quantile_tick"]) != evidence.quantile_tick:
                mismatches.append(
                    WindowMismatch(
                        start, length, group, "table-quantile", evidence.quantile_tick, int(cell["quantile_tick"])
                    )
                )
        return VerificationReport(1, groups_checked, tuple(mismatches))

    def verify_small_batches(self, max_windows: int = 4096) -> VerificationReport:
        mismatches: list[WindowMismatch] = []
        windows = 0
        groups = 0
        for batch in self.batches:
            for length in batch.lengths:
                for start in range(batch.first_start, batch.last_start + 1):
                    report = self.verify_window(start, length)
                    windows += report.checked_windows
                    groups += report.checked_groups
                    mismatches.extend(report.mismatches)
                    if windows > max_windows:
                        return VerificationReport(windows, groups, tuple(mismatches))
        return VerificationReport(windows, groups, tuple(mismatches))


def _build_runs(cells: np.ndarray) -> np.ndarray:
    count = int(cells.shape[0])
    if count == 0:
        return np.empty(0, dtype=_RUN_DTYPE)
    shared = cells["shared"]
    first_start = cells["first_start"]
    last_start = cells["last_start"]
    breaks = (shared[1:] != shared[:-1]) | (first_start[1:] != last_start[:-1] + 1)
    run_starts = np.concatenate((np.array([0], dtype=np.int64), np.flatnonzero(breaks) + 1))
    run_ends = np.concatenate((run_starts[1:] - 1, np.array([count - 1], dtype=np.int64)))
    runs = np.empty(run_starts.size, dtype=_RUN_DTYPE)
    runs["first_start"] = first_start[run_starts]
    runs["last_start"] = last_start[run_ends]
    runs["first_interval"] = run_starts
    runs["last_interval"] = run_ends
    runs["shared"] = shared[run_starts]
    return runs


def _group_valid_rows(archive: Archive, group: int) -> tuple[np.ndarray, np.ndarray]:
    mask = archive.groups == int(group)
    rows = archive.rows[mask]
    ticks = archive.ticks[mask]
    good = ticks != NAT_TICK
    return rows[good], ticks[good]


def _slot_for_tick(slot_ticks: list[int], slot_index: dict[int, int], tick: int) -> int:
    tick = int(tick)
    existing = slot_index.get(tick)
    if existing is not None:
        return existing
    slot = len(slot_ticks)
    slot_ticks.append(tick)
    slot_index[tick] = slot
    return slot


def _sweep_group(
    archive: Archive,
    group: int,
    length: int,
    first_start: int,
    last_start: int,
    slot_ticks: list[int],
    slot_index: dict[int, int],
) -> np.ndarray:
    rows, ticks = _group_valid_rows(archive, group)
    n = int(rows.size)
    empty_slot = 0

    if n == 0:
        cells = np.empty(1, dtype=_CELL_DTYPE)
        cells[0] = (
            first_start,
            last_start,
            0,
            True,
            empty_slot,
            empty_slot,
            -1,
            -1,
            -1,
            -1,
            NAT_TICK,
            NAT_TICK,
            NAT_TICK,
            NAT_TICK,
        )
        return cells

    key_order = np.lexsort((np.arange(n), rows, ticks))
    observation_to_key = np.empty(n, dtype=np.int64)
    observation_to_key[key_order] = np.arange(n)

    entry_events = rows - int(length) + 1
    exit_events = rows + 1
    events = np.concatenate((np.array([first_start], dtype=np.int64), entry_events, exit_events))
    breakpoints = np.unique(events[(events >= first_start) & (events <= last_start)])

    left = int(np.searchsorted(rows, first_start, side="left"))
    right = int(np.searchsorted(rows, first_start + int(length), side="left"))
    fenwick = Fenwick(n)
    for observation in range(left, right):
        fenwick.add(int(observation_to_key[observation]), 1)

    cells = np.empty(breakpoints.size, dtype=_CELL_DTYPE)
    for interval_index, start_value in enumerate(breakpoints):
        start = int(start_value)
        if interval_index > 0:
            new_left = left
            while new_left < n and int(rows[new_left]) < start:
                new_left += 1
            new_right = right
            end = start + int(length)
            while new_right < n and int(rows[new_right]) < end:
                new_right += 1
            for observation in range(left, new_left):
                fenwick.add(int(observation_to_key[observation]), -1)
            for observation in range(right, new_right):
                fenwick.add(int(observation_to_key[observation]), 1)
            left, right = new_left, new_right

        if interval_index + 1 < int(breakpoints.size):
            interval_last = int(breakpoints[interval_index + 1]) - 1
        else:
            interval_last = int(last_start)

        valid_count = right - left
        if valid_count == 0:
            median_slot = quantile_slot = empty_slot
            lower_rank = upper_rank = -1
            lower_row = upper_row = -1
            lower_tick = upper_tick = NAT_TICK
            median_tick = quantile_tick = NAT_TICK
            shared = True
        elif valid_count % 2 == 1:
            rank = valid_count // 2
            key = fenwick.kth(rank)
            observation = int(key_order[key])
            lower_rank = upper_rank = rank
            lower_row = upper_row = int(rows[observation])
            lower_tick = upper_tick = median_tick = quantile_tick = int(ticks[observation])
            shared = True
            median_slot = quantile_slot = _slot_for_tick(slot_ticks, slot_index, median_tick)
        else:
            lower_rank = valid_count // 2 - 1
            upper_rank = valid_count // 2
            lower_observation = int(key_order[fenwick.kth(lower_rank)])
            upper_observation = int(key_order[fenwick.kth(upper_rank)])
            lower_row = int(rows[lower_observation])
            upper_row = int(rows[upper_observation])
            lower_tick = int(ticks[lower_observation])
            upper_tick = int(ticks[upper_observation])
            median_tick = median_tick_of_neighbours(lower_tick, upper_tick)
            quantile_tick = quantile_half_tick_of_neighbours(lower_tick, upper_tick, valid_count)
            shared = median_tick == quantile_tick
            median_slot = _slot_for_tick(slot_ticks, slot_index, median_tick)
            if shared:
                quantile_slot = median_slot
            else:
                quantile_slot = _slot_for_tick(slot_ticks, slot_index, quantile_tick)

        cells[interval_index] = (
            start,
            interval_last,
            valid_count,
            shared,
            median_slot,
            quantile_slot,
            lower_rank,
            upper_rank,
            lower_row,
            upper_row,
            lower_tick,
            upper_tick,
            median_tick,
            quantile_tick,
        )

    return cells


def build_position_table(
    archive: Archive,
    *,
    lengths: int | Sequence[int] = 1,
    start_range: tuple[int, int] | range = (0, 0),
    verify_small: bool = True,
    small_window_limit: int = 4096,
) -> SharedPositionTable:
    """Build the compressed shared-slot table for all lengths and groups."""
    batch = WindowBatch.make(lengths, start_range)
    slot_ticks: list[int] = [NAT_TICK]
    slot_index = {NAT_TICK: 0}
    cells: dict[tuple[int, int], np.ndarray] = {}
    for length in batch.lengths:
        for group in range(archive.group_count):
            cells[(int(length), int(group))] = _sweep_group(
                archive,
                group,
                int(length),
                batch.first_start,
                batch.last_start,
                slot_ticks,
                slot_index,
            )
    table = SharedPositionTable(archive, cells, np.array(slot_ticks, dtype=np.int64), (batch,))
    if verify_small and batch.window_count <= int(small_window_limit):
        report = table.verify_small_batches(small_window_limit)
        if not report.ok:
            first = report.mismatches[0]
            raise AssertionError(f"position table differs from NumPy backend: {first}")
    return table


def _merged_start_ranges(batches: Sequence[WindowBatch]) -> dict[int, list[tuple[int, int]]]:
    by_length: dict[int, list[tuple[int, int]]] = {}
    for batch in batches:
        for length in batch.lengths:
            by_length.setdefault(int(length), []).append((batch.first_start, batch.last_start))

    merged_by_length: dict[int, list[tuple[int, int]]] = {}
    for length, ranges in by_length.items():
        ordered = sorted(ranges)
        merged: list[tuple[int, int]] = []
        for first_start, last_start in ordered:
            if not merged or first_start > merged[-1][1] + 1:
                merged.append((first_start, last_start))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], last_start))
        merged_by_length[length] = merged
    return merged_by_length


def build_batches(
    archive: Archive,
    batches: Sequence[WindowBatch],
    *,
    verify_small: bool = True,
    small_window_limit: int = 4096,
) -> SharedPositionTable:
    """Build one table containing every registered batch/length/group domain."""
    slot_ticks: list[int] = [NAT_TICK]
    slot_index = {NAT_TICK: 0}
    ranges_by_length = _merged_start_ranges(batches)
    cells: dict[tuple[int, int], np.ndarray] = {}
    for length, ranges in ranges_by_length.items():
        for group in range(archive.group_count):
            parts = [
                _sweep_group(
                    archive,
                    group,
                    length,
                    first_start,
                    last_start,
                    slot_ticks,
                    slot_index,
                )
                for first_start, last_start in ranges
            ]
            if len(parts) == 1:
                cells[(length, group)] = parts[0]
            else:
                combined = np.concatenate(parts)
                order = np.lexsort((combined["last_start"], combined["first_start"]))
                cells[(length, group)] = combined[order]

    table = SharedPositionTable(archive, cells, np.array(slot_ticks, dtype=np.int64), tuple(batches))
    if verify_small:
        total = sum(batch.window_count for batch in batches)
        if total <= int(small_window_limit):
            report = table.verify_small_batches(small_window_limit)
            if not report.ok:
                first = report.mismatches[0]
                raise AssertionError(f"position table differs from NumPy backend: {first}")
    return table


SharedLocationTable = SharedPositionTable
DualColumnTable = SharedPositionTable
LocationTable = SharedPositionTable
build_shared_position_table = build_position_table
build_location_table = build_position_table


__all__ = [
    "Archive",
    "ColumnRead",
    "DualColumnTable",
    "EvidencePoint",
    "Fenwick",
    "LocationTable",
    "RegularSegment",
    "SharedLocationTable",
    "SharedPositionTable",
    "SourceRef",
    "WindowBatch",
    "WindowEvidence",
    "WindowRead",
    "analyze_window",
    "build_batches",
    "build_location_table",
    "build_position_table",
    "build_shared_position_table",
    "median_tick_of_neighbours",
    "quantile_half_tick_of_neighbours",
    "round_half_even_two",
    "ticks_from_values",
]
