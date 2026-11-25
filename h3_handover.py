"""Compressed H3 handover-edge auditing.

The module works with allocation rows in their original table order.  A row
assigns either all target-resolution descendants of an H3 parent or a
half-open child-position interval under that parent.  Intervals are converted
into canonical H3 subtree blocks, so counting never expands a long position
range into every target cell or every fine edge.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import h3

Number = int
Group = str
Cell = str
RowId = str | int

_POSITION_KEYS = (
    'start',
    'start_pos',
    'start_position',
    'start_child_pos',
    'start_child_position',
    'start_index',
    'lo',
    'from_pos',
    'from',
    '起始序号',
    '起始子格序号',
    '开始序号',
)
_END_KEYS = (
    'end',
    'stop',
    'end_pos',
    'end_position',
    'end_child_pos',
    'end_child_position',
    'finish',
    'hi',
    'to_pos',
    'to',
    '结束序号',
    '结束子格序号',
    '终止序号',
)
_INCLUSIVE_END_KEYS = (
    'end_inclusive',
    'inclusive_end',
    'last',
    'end_position_inclusive',
)
_CANCEL_KEYS = (
    'cancel',
    'cancelled',
    'canceled',
    'is_cancel',
    'remove',
    'revoke',
    '取消',
    '作废',
)
_ACTION_KEYS = ('action', 'op', 'operation', 'type', 'status')
_PARENT_KEYS = (
    'parent',
    'parent_cell',
    'h3',
    'h3_cell',
    'cell',
    'root',
    'hex',
    'index',
    '父格',
    '粗格',
)
_GROUP_KEYS = (
    'group',
    'owner',
    'team',
    'assignee',
    'name',
    'group_name',
    '小组',
    '组',
    '组别',
)
_ID_KEYS = ('id', 'row_id', 'line_id', 'source_row', 'row')


class HandoverError(ValueError):
    """Raised for an invalid allocation table or audit query."""


@dataclass(frozen=True)
class AllocationRow:
    index: int
    parent: Cell
    group: Group | None
    start: int | None = None
    end: int | None = None
    cancel: bool = False
    row_id: RowId | None = None
    raw: Any = None

    @property
    def parent_resolution(self) -> int:
        return h3.get_resolution(self.parent)

    @property
    def is_full_parent(self) -> bool:
        return self.start is None and self.end is None

    def interval(self, target_resolution: int) -> tuple[int, int]:
        parent_resolution = self.parent_resolution
        if target_resolution < parent_resolution:
            raise HandoverError(
                f'target resolution {target_resolution} is coarser than '
                f'parent {self.parent!r}'
            )
        size = h3.cell_to_children_size(self.parent, target_resolution)
        start = 0 if self.start is None else self.start
        end = size if self.end is None else self.end
        if end < start:
            raise HandoverError(f'row {self.row_id!r} has interval [{start}, {end})')
        if start < 0 or end > size:
            raise HandoverError(
                f'row {self.row_id!r} interval [{start}, {end}) is outside 0..{size}'
            )
        return start, end

    def contains(self, cell: Cell) -> bool:
        target_resolution = h3.get_resolution(cell)
        if target_resolution < self.parent_resolution:
            return False
        try:
            actual_parent = h3.cell_to_parent(cell, self.parent_resolution)
        except Exception:
            return False
        if actual_parent != self.parent:
            return False
        start, end = self.interval(target_resolution)
        position = h3.cell_to_child_pos(cell, self.parent_resolution)
        return start <= position < end


@dataclass(frozen=True)
class Provenance:
    group: Group | None
    row_id: RowId | None
    row_index: int | None
    block: Cell | None
    cancel: bool = False
    row: AllocationRow | None = None
    line_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'group': self.group,
            'owner': self.group,
            'row_id': self.row_id,
            'row_index': self.row_index,
            'line_number': self.line_number,
            'block': self.block,
            'cancel': self.cancel,
            'row': None if self.row is None else _row_to_json(self.row),
        }

    def __bool__(self) -> bool:
        return self.row is not None


@dataclass(frozen=True)
class EdgeTrace:
    edge: tuple[Cell, Cell]
    adjacent: bool
    in_v1: bool
    in_v2: bool
    status: str
    v1: tuple[Provenance, Provenance]
    v2: tuple[Provenance, Provenance]

    def to_dict(self) -> dict[str, Any]:
        return {
            'edge': list(self.edge),
            'adjacent': self.adjacent,
            'in_v1': self.in_v1,
            'in_v2': self.in_v2,
            'status': self.status,
            'continuing': self.status == 'continuing',
            'new': self.status == 'new',
            'disappeared': self.status == 'disappeared',
            'v1': [item.to_dict() for item in self.v1],
            'v2': [item.to_dict() for item in self.v2],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class InterfaceSpan:
    left: Cell
    right: Cell
    count: int
    status: str
    groups1: tuple[Group | None, Group | None]
    groups2: tuple[Group | None, Group | None]
    rows1: tuple[RowId | None, RowId | None]
    rows2: tuple[RowId | None, RowId | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            'left': self.left,
            'right': self.right,
            'count': self.count,
            'status': self.status,
            'groups1': list(self.groups1),
            'groups2': list(self.groups2),
            'rows1': list(self.rows1),
            'rows2': list(self.rows2),
        }


@dataclass
class AuditResult:
    continuing: int = 0
    new: int = 0
    disappeared: int = 0
    group_a: Group | None = None
    group_b: Group | None = None
    resolution: int | None = None
    unique_pairs: int = 0
    v1_edges: int = 0
    v2_edges: int = 0
    interfaces: list[InterfaceSpan] = field(default_factory=list)
    index1: 'VersionIndex | None' = None
    index2: 'VersionIndex | None' = None

    @property
    def kept(self) -> int:
        return self.continuing

    @property
    def stable(self) -> int:
        return self.continuing

    @property
    def shared(self) -> int:
        return self.continuing

    @property
    def added(self) -> int:
        return self.new

    @property
    def removed(self) -> int:
        return self.disappeared

    @property
    def gone(self) -> int:
        return self.disappeared

    @property
    def unchanged(self) -> int:
        return self.continuing

    @property
    def ongoing(self) -> int:
        return self.continuing

    @property
    def surviving(self) -> int:
        return self.continuing

    @property
    def retained(self) -> int:
        return self.continuing

    @property
    def deleted(self) -> int:
        return self.disappeared

    def __getattr__(self, name: str) -> int:
        aliases = {
            'continuing_count': self.continuing,
            'continuing_edges_count': self.continuing,
            'ongoing_count': self.continuing,
            'kept_count': self.continuing,
            'new_count': self.new,
            'new_edges_count': self.new,
            'added_count': self.new,
            'disappeared_count': self.disappeared,
            'disappeared_edges_count': self.disappeared,
            'removed_count': self.disappeared,
            'deleted_count': self.disappeared,
        }
        if name in aliases:
            return aliases[name]
        if name.endswith('_edges'):
            base = name[:-6]
            if base in {
                'continuing',
                'kept',
                'stable',
                'shared',
                'unchanged',
                'ongoing',
                'surviving',
                'retained',
            }:
                return self.continuing
            if base in {'new', 'added'}:
                return self.new
            if base in {'disappeared', 'removed', 'gone', 'deleted'}:
                return self.disappeared
        raise AttributeError(name)

    def to_dict(self) -> dict[str, int]:
        return self.as_dict()

    def model_dump(self) -> dict[str, int]:
        return self.as_dict()

    @property
    def counts(self) -> tuple[int, int, int]:
        return self.continuing, self.new, self.disappeared

    def as_tuple(self) -> tuple[int, int, int]:
        return self.counts

    def as_dict(self) -> dict[str, int]:
        return {
            'continuing': self.continuing,
            'kept': self.continuing,
            'stable': self.continuing,
            'new': self.new,
            'added': self.new,
            'disappeared': self.disappeared,
            'removed': self.disappeared,
            'gone': self.disappeared,
            'unique_pairs': self.unique_pairs,
            'v1_edges': self.v1_edges,
            'v2_edges': self.v2_edges,
            '继续有效的边': self.continuing,
            '新边': self.new,
            '消失的边': self.disappeared,
        }

    def summary(self) -> dict[str, int]:
        return {
            'continuing': self.continuing,
            'new': self.new,
            'disappeared': self.disappeared,
        }

    def trace_edge(self, first: Cell, second: Cell) -> EdgeTrace:
        if self.index1 is None or self.index2 is None:
            raise HandoverError('audit result does not retain version indexes')
        return trace_edge(
            first,
            second,
            self.index1,
            self.index2,
            self.group_a,
            self.group_b,
        )

    def iter_edges(self, status: str | None = None) -> Iterator[tuple[Cell, Cell]]:
        wanted = None if status is None else _normalize_status(status)
        for span in self.interfaces:
            if wanted is not None and _normalize_status(span.status) != wanted:
                continue
            yield from iter_interface_edges(span.left, span.right, self.resolution)

    def edge_list(self, status: str | None = None) -> list[tuple[Cell, Cell]]:
        return list(self.iter_edges(status))

    def one_edge(self, status: str | None = None) -> tuple[Cell, Cell]:
        return next(self.iter_edges(status))

    def trace_first_edge(self, status: str) -> EdgeTrace:
        return self.trace_edge(*self.one_edge(status))

    def __iter__(self) -> Iterator[int]:
        yield from self.counts

    def __getitem__(self, key: str | int) -> int:
        if isinstance(key, int):
            return self.counts[key]
        if key in {'continuing', 'kept', 'stable', 'shared', '继续有效的边'}:
            return self.continuing
        if key in {'new', 'added', '新边'}:
            return self.new
        if key in {'disappeared', 'removed', 'gone', '消失的边'}:
            return self.disappeared
        if key == 'unique_pairs':
            return self.unique_pairs
        if key == 'v1_edges':
            return self.v1_edges
        if key == 'v2_edges':
            return self.v2_edges
        raise KeyError(key)


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int
    group: Group | None
    row_id: RowId | None
    row_index: int


@dataclass(frozen=True)
class _RootInterval:
    root: Cell
    start: int
    end: int
    group: Group | None
    row_id: RowId | None
    row_index: int


@dataclass(frozen=True)
class _OverlayBlock:
    cell: Cell
    group1: Group | None
    row1: RowId | None
    index1: int | None
    group2: Group | None
    row2: RowId | None
    index2: int | None


class VersionIndex:
    """Effective ownership for one allocation table at one target resolution."""

    def __init__(
        self,
        rows: Iterable[AllocationRow | Mapping[str, Any] | Sequence[Any]],
        resolution: int,
        *,
        version: str | int = 'v',
    ) -> None:
        self.resolution = resolution
        self.version = version
        self.rows = normalize_rows(rows, version=version)
        self.blocks: dict[Cell, tuple[Group | None, RowId | None, int]] = {}
        self.root_intervals: dict[Cell, list[_RootInterval]] = {}
        self._candidate_pairs: list[tuple[Cell, Cell, int]] | None = None
        self._apply_rows()
        self._build_root_intervals()

    def _apply_rows(self) -> None:
        for row in self.rows:
            start, end = row.interval(self.resolution)
            owner = None if row.cancel else row.group
            for block in decompose_interval(row.parent, self.resolution, start, end):
                self._assign_block(block, owner, row.row_id, row.index)

    def _assign_block(
        self,
        cell: Cell,
        group: Group | None,
        row_id: RowId | None,
        row_index: int,
    ) -> None:
        cell_res = h3.get_resolution(cell)
        ancestor = None
        cursor = cell
        cursor_res = cell_res
        while True:
            if cursor in self.blocks:
                ancestor = cursor
                break
            if cursor_res == 0:
                break
            cursor_res -= 1
            cursor = h3.cell_to_parent(cell, cursor_res)

        if ancestor is not None:
            old = self.blocks.pop(ancestor)
            current = ancestor
            current_res = h3.get_resolution(current)
            while current_res < cell_res:
                next_res = current_res + 1
                path_child = h3.cell_to_parent(cell, next_res)
                for child in h3.cell_to_children(current, next_res):
                    if child != path_child:
                        self.blocks[child] = old
                current = path_child
                current_res = next_res
        else:
            for existing in list(self.blocks):
                existing_res = h3.get_resolution(existing)
                if (
                    existing_res > cell_res
                    and h3.cell_to_parent(existing, cell_res) == cell
                ):
                    del self.blocks[existing]

        self.blocks[cell] = (group, row_id, row_index)

    def _build_root_intervals(self) -> None:
        intervals: dict[Cell, list[_RootInterval]] = {}
        for cell, (group, row_id, row_index) in self.blocks.items():
            cell_res = h3.get_resolution(cell)
            root = cell if cell_res == 0 else h3.cell_to_parent(cell, 0)
            size = h3.cell_to_children_size(cell, self.resolution)
            first = h3.child_pos_to_cell(cell, self.resolution, 0)
            start = h3.cell_to_child_pos(first, 0)
            intervals.setdefault(root, []).append(
                _RootInterval(root, start, start + size, group, row_id, row_index)
            )
        for root, values in intervals.items():
            values.sort(key=lambda item: (item.start, item.end))
            self.root_intervals[root] = values

    def trace(self, cell: Cell) -> Provenance:
        cell = str(cell)
        cell_res = h3.get_resolution(cell)
        if cell_res != self.resolution:
            raise HandoverError(
                f'cell resolution {cell_res} does not match index '
                f'resolution {self.resolution}'
            )
        cursor = cell
        for current_res in range(cell_res, -1, -1):
            if cursor in self.blocks:
                group, row_id, row_index = self.blocks[cursor]
                row = (
                    self.rows[row_index]
                    if row_index is not None and row_index < len(self.rows)
                    else None
                )
                return Provenance(
                    group=group,
                    row_id=row_id,
                    row_index=row_index,
                    block=cursor,
                    cancel=bool(row and row.cancel)
                    or group is None
                    and row is not None
                    and row.cancel,
                    row=row,
                    line_number=None if row_index is None else row_index + 1,
                )
            if current_res > 0:
                cursor = h3.cell_to_parent(cell, current_res - 1)
        return Provenance(None, None, None, None)

    def history(self, cell: Cell) -> list[Provenance]:
        matches: list[Provenance] = []
        effective = self.trace(cell)
        for row in self.rows:
            if row.contains(cell):
                matches.append(
                    Provenance(
                        group=None if row.cancel else row.group,
                        row_id=row.row_id,
                        row_index=row.index,
                        block=effective.block
                        if row.index == effective.row_index
                        else None,
                        cancel=row.cancel,
                        row=row,
                        line_number=row.index + 1,
                    )
                )
        return matches

    def candidate_pairs(self) -> list[tuple[Cell, Cell, int]]:
        if self._candidate_pairs is None:
            self._candidate_pairs = candidate_cell_pairs(
                list(self.blocks), self.resolution
            )
        return self._candidate_pairs

    def edges_between(self, group_a: Group, group_b: Group) -> int:
        count = 0
        for left, right, edge_count in self.candidate_pairs():
            left_group = self.blocks[left][0]
            right_group = self.blocks[right][0]
            if is_handover_pair(left_group, right_group, group_a, group_b):
                count += edge_count
        return count


class HandoverIndex:
    """Cached two-version index for repeated group queries."""

    def __init__(
        self,
        version1: Any,
        version2: Any,
        resolution: int | None = None,
        *,
        target_resolution: int | None = None,
        res: int | None = None,
    ) -> None:
        chosen = resolution if resolution is not None else target_resolution
        chosen = chosen if chosen is not None else res
        if chosen is None:
            raise HandoverError('target resolution is required')
        self.resolution = int(chosen)
        self.index1 = as_version_index(version1, self.resolution, 'v1')
        self.index2 = as_version_index(version2, self.resolution, 'v2')
        self._overlay_blocks: list[_OverlayBlock] | None = None
        self._pairs: list[tuple[_OverlayBlock, _OverlayBlock, int]] | None = None

    def prepare(self) -> 'HandoverIndex':
        if self._overlay_blocks is None:
            self._overlay_blocks = build_overlay_blocks(
                self.index1, self.index2, self.resolution
            )
        if self._pairs is None:
            self._pairs = candidate_overlay_pairs(self._overlay_blocks, self.resolution)
        return self

    def query(self, group_a: Group, group_b: Group) -> AuditResult:
        self.prepare()
        assert self._pairs is not None
        if group_a == group_b:
            raise HandoverError('query requires two distinct group names')

        result = AuditResult(
            group_a=group_a,
            group_b=group_b,
            resolution=self.resolution,
            index1=self.index1,
            index2=self.index2,
        )
        for left, right, count in self._pairs:
            in_v1 = is_handover_pair(left.group1, right.group1, group_a, group_b)
            in_v2 = is_handover_pair(left.group2, right.group2, group_a, group_b)
            if in_v1 and in_v2:
                status = 'continuing'
                result.continuing += count
            elif in_v2:
                status = 'new'
                result.new += count
            elif in_v1:
                status = 'disappeared'
                result.disappeared += count
            else:
                continue
            result.interfaces.append(
                InterfaceSpan(
                    left=left.cell,
                    right=right.cell,
                    count=count,
                    status=status,
                    groups1=(left.group1, right.group1),
                    groups2=(left.group2, right.group2),
                    rows1=(left.row1, right.row1),
                    rows2=(left.row2, right.row2),
                )
            )

        result.v1_edges = result.continuing + result.disappeared
        result.v2_edges = result.continuing + result.new
        result.unique_pairs = result.continuing + result.new + result.disappeared
        return result

    compare = query
    audit = query
    diff = query

    def trace_edge(
        self,
        first: Cell,
        second: Cell,
        group_a: Group | None = None,
        group_b: Group | None = None,
    ) -> EdgeTrace:
        return trace_edge(first, second, self.index1, self.index2, group_a, group_b)


class AllocationTable:
    """Rows prepared once; compressed indexes are cached per resolution."""

    def __init__(self, rows: Any, *, version: str | int = 'v') -> None:
        self.version = version
        self.rows = normalize_rows(rows, version=version)
        self._indexes: dict[int, VersionIndex] = {}

    def at(self, resolution: int) -> VersionIndex:
        resolution = int(resolution)
        if resolution not in self._indexes:
            self._indexes[resolution] = VersionIndex(
                self.rows, resolution, version=self.version
            )
        return self._indexes[resolution]

    index = at
    for_resolution = at
    prepare = at

    def edges_between(self, resolution: int, group_a: Group, group_b: Group) -> int:
        return self.at(resolution).edges_between(group_a, group_b)

    def trace(self, cell: Cell) -> Provenance:
        return self.at(h3.get_resolution(cell)).trace(cell)


class HandoverWorkspace:
    """Two allocation tables prepared once, with per-resolution audit indexes."""

    def __init__(self, version1: Any, version2: Any) -> None:
        self.table1 = (
            version1
            if isinstance(version1, AllocationTable)
            else AllocationTable(version1, version='v1')
        )
        self.table2 = (
            version2
            if isinstance(version2, AllocationTable)
            else AllocationTable(version2, version='v2')
        )
        self._indexes: dict[int, HandoverIndex] = {}

    def at(self, resolution: int) -> HandoverIndex:
        resolution = int(resolution)
        if resolution not in self._indexes:
            self._indexes[resolution] = HandoverIndex(
                self.table1.at(resolution),
                self.table2.at(resolution),
                resolution,
            ).prepare()
        return self._indexes[resolution]

    index = at
    for_resolution = at
    prepare = at

    def query(self, *args: Any, **kwargs: Any) -> AuditResult:
        resolution, group_a, group_b = _query_arguments(args, kwargs)
        return self.at(resolution).query(group_a, group_b)

    compare = query
    audit = query
    diff = query


def _query_arguments(
    args: Sequence[Any], kwargs: Mapping[str, Any]
) -> tuple[int, Group, Group]:
    resolution = kwargs.get(
        'resolution', kwargs.get('target_resolution', kwargs.get('res'))
    )
    group_a = kwargs.get('group_a', kwargs.get('group1', kwargs.get('a')))
    group_b = kwargs.get('group_b', kwargs.get('group2', kwargs.get('b')))
    positional = list(args)
    if positional and isinstance(positional[0], int):
        if resolution is None:
            resolution = positional.pop(0)
    elif positional and resolution is None and isinstance(positional[-1], int):
        resolution = positional.pop()
    if group_a is None and positional:
        group_a = positional.pop(0)
    if group_b is None and positional:
        group_b = positional.pop(0)
    if resolution is None or group_a is None or group_b is None or positional:
        raise HandoverError(
            'call with (resolution, group_a, group_b) or (group_a, group_b, resolution)'
        )
    return int(resolution), str(group_a), str(group_b)


def normalize_rows(
    rows: Iterable[AllocationRow | Mapping[str, Any] | Sequence[Any]],
    *,
    version: str | int = 'v',
) -> list[AllocationRow]:
    if isinstance(rows, Mapping):
        wrapped = _first_value(
            rows,
            ('rows', 'allocations', 'allocation_table', 'items', 'data'),
        )
        rows = wrapped if wrapped is not None else list(rows.values())
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        raise HandoverError('allocation rows must be an iterable')
    if hasattr(rows, 'to_dict') and callable(rows.to_dict):
        try:
            rows = rows.to_dict('records')
        except TypeError:
            rows = rows.to_dict()
    normalized: list[AllocationRow] = []
    for index, raw in enumerate(rows):
        if isinstance(raw, AllocationRow):
            normalized.append(raw)
            continue
        if isinstance(raw, Mapping):
            parent = _first_value(raw, _PARENT_KEYS)
            if parent is None:
                raise HandoverError(f'row {index} has no H3 parent/cell')
            group = _first_value(raw, _GROUP_KEYS)
            action = str(_first_value(raw, _ACTION_KEYS, '') or '').lower()
            cancel = bool(_first_value(raw, _CANCEL_KEYS, False)) or action in {
                'cancel',
                'cancelled',
                'canceled',
                'remove',
                'revoke',
                'delete',
            }
            if action in {'assign', 'allocation', 'grant', 'set', 'update'}:
                cancel = False
            start = _parse_int(_first_value(raw, _POSITION_KEYS))
            end = _parse_int(_first_value(raw, _END_KEYS))
            inclusive_end = _parse_int(_first_value(raw, _INCLUSIVE_END_KEYS))
            if inclusive_end is not None:
                end = inclusive_end + 1
            positions = (
                raw.get('positions')
                or raw.get('child_positions')
                or raw.get('range')
                or raw.get('position_range')
            )
            if positions is not None and start is None and end is None:
                start, end = _parse_range(positions)
            row_id = _first_value(raw, _ID_KEYS)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            parent = raw[0]
            group = raw[1] if len(raw) > 1 else None
            start = raw[2] if len(raw) > 2 else None
            end = raw[3] if len(raw) > 3 else None
            cancel = len(raw) > 4 and bool(raw[4])
            row_id = None
        else:
            raise HandoverError(f'unsupported row at index {index}: {raw!r}')

        if cancel:
            group = None
        if parent is None:
            raise HandoverError(f'row {index} has no H3 parent/cell')
        parent = str(parent)
        if not h3.is_valid_cell(parent):
            raise HandoverError(f'row {index} has invalid H3 cell {parent!r}')
        normalized.append(
            AllocationRow(
                index=index,
                parent=parent,
                group=None if group is None else str(group),
                start=start,
                end=end,
                cancel=cancel,
                row_id=row_id if row_id is not None else index,
                raw=raw,
            )
        )
    return normalized


def as_version_index(data: Any, resolution: int, version: str) -> VersionIndex:
    if isinstance(data, VersionIndex):
        if data.resolution != resolution:
            raise HandoverError(
                f'{version} index resolution {data.resolution} does not '
                f'match query {resolution}'
            )
        return data
    if isinstance(data, HandoverIndex):
        raise HandoverError('a HandoverIndex cannot be used as one version')
    rows = getattr(data, 'rows', data)
    return VersionIndex(rows, resolution, version=version)


def decompose_interval(
    parent: Cell, target_resolution: int, start: int, end: int
) -> list[Cell]:
    """Return canonical H3 blocks exactly covering [start, end) child positions."""
    if start == end:
        return []
    parent_resolution = h3.get_resolution(parent)
    total = h3.cell_to_children_size(parent, target_resolution)
    if not 0 <= start < end <= total:
        raise HandoverError(
            f'interval [{start}, {end}) invalid for {parent} size {total}'
        )
    if start == 0 and end == total:
        return [parent]
    if parent_resolution == target_resolution:
        return [parent]

    blocks: list[Cell] = []
    child_resolution = parent_resolution + 1
    for child in h3.cell_to_children(parent, child_resolution):
        child_size_at_target = h3.cell_to_children_size(child, target_resolution)
        first_target_child = h3.child_pos_to_cell(child, target_resolution, 0)
        child_start = h3.cell_to_child_pos(first_target_child, parent_resolution)
        child_end = child_start + child_size_at_target
        overlap_start = max(start, child_start)
        overlap_end = min(end, child_end)
        if overlap_start >= overlap_end:
            continue
        if overlap_start == child_start and overlap_end == child_end:
            blocks.append(child)
        else:
            blocks.extend(
                decompose_interval(
                    child,
                    target_resolution,
                    overlap_start - child_start,
                    overlap_end - child_start,
                )
            )
    return blocks


def interface_edge_count(left: Cell, right: Cell, target_resolution: int) -> int:
    """Count target-resolution neighbor pairs between two complete H3 blocks."""
    left_res = h3.get_resolution(left)
    right_res = h3.get_resolution(right)
    if target_resolution < max(left_res, right_res):
        raise HandoverError('target resolution is coarser than an interface block')

    if left_res == right_res:
        if left == right or not h3.are_neighbor_cells(left, right):
            return 0
        return 3 ** (target_resolution - left_res)

    if left_res < right_res:
        coarse, fine, coarse_res, fine_res = left, right, left_res, right_res
    else:
        coarse, fine, coarse_res, fine_res = right, left, right_res, left_res

    count = 0
    multiplier = 3 ** (target_resolution - fine_res)
    for neighbor in h3.grid_ring(fine, 1):
        try:
            ancestor = h3.cell_to_parent(neighbor, coarse_res)
        except Exception:
            continue
        if ancestor == coarse:
            count += multiplier
    return count


def iter_interface_edges(
    left: Cell, right: Cell, target_resolution: int
) -> Iterator[tuple[Cell, Cell]]:
    left_res = h3.get_resolution(left)
    right_res = h3.get_resolution(right)
    if left_res != right_res:
        if left_res < right_res:
            coarse, fine = left, right
        else:
            coarse, fine = right, left
        for neighbor in h3.grid_ring(fine, 1):
            if h3.cell_to_parent(neighbor, h3.get_resolution(coarse)) == coarse:
                yield from _iter_same_resolution_edges(
                    fine, neighbor, target_resolution
                )
        return
    yield from _iter_same_resolution_edges(left, right, target_resolution)


def _iter_same_resolution_edges(
    a: Cell, b: Cell, target_resolution: int
) -> Iterator[tuple[Cell, Cell]]:
    resolution = h3.get_resolution(a)
    if resolution != h3.get_resolution(b) or not h3.are_neighbor_cells(a, b):
        return
    if resolution == target_resolution:
        yield canonical_edge(a, b)
        return
    next_resolution = resolution + 1
    children_b = set(h3.cell_to_children(b, next_resolution))
    for child_a in h3.cell_to_children(a, next_resolution):
        for child_b in h3.grid_ring(child_a, 1):
            if child_b in children_b:
                yield from _iter_same_resolution_edges(
                    child_a, child_b, target_resolution
                )


def canonical_edge(first: Cell, second: Cell) -> tuple[Cell, Cell]:
    return (first, second) if str(first) <= str(second) else (second, first)


def is_handover_pair(
    first_owner: Group | None,
    second_owner: Group | None,
    group_a: Group,
    group_b: Group,
) -> bool:
    return {first_owner, second_owner} == {group_a, group_b}


def build_overlay_blocks(
    index1: VersionIndex, index2: VersionIndex, resolution: int
) -> list[_OverlayBlock]:
    roots = set(index1.root_intervals) | set(index2.root_intervals)
    overlay: list[_OverlayBlock] = []
    for root in roots:
        intervals1 = index1.root_intervals.get(root, [])
        intervals2 = index2.root_intervals.get(root, [])
        for start, end, one, two in _overlay_root_intervals(intervals1, intervals2):
            for cell in decompose_interval(root, resolution, start, end):
                overlay.append(
                    _OverlayBlock(
                        cell=cell,
                        group1=one.group if one else None,
                        row1=one.row_id if one else None,
                        index1=one.row_index if one else None,
                        group2=two.group if two else None,
                        row2=two.row_id if two else None,
                        index2=two.row_index if two else None,
                    )
                )
    return overlay


def _overlay_root_intervals(
    intervals1: Sequence[_RootInterval],
    intervals2: Sequence[_RootInterval],
) -> Iterator[tuple[int, int, _RootInterval | None, _RootInterval | None]]:
    endpoints = sorted(
        {item.start for item in intervals1}
        | {item.end for item in intervals1}
        | {item.start for item in intervals2}
        | {item.end for item in intervals2}
    )
    for start, end in zip(endpoints, endpoints[1:]):
        if start >= end:
            continue
        one = _interval_at(intervals1, start)
        two = _interval_at(intervals2, start)
        if one is None and two is None:
            continue
        yield start, end, one, two


def _interval_at(
    intervals: Sequence[_RootInterval], position: int
) -> _RootInterval | None:
    for interval in intervals:
        if interval.start <= position < interval.end:
            return interval
    return None


def candidate_cell_pairs(
    cells: Sequence[Cell], resolution: int
) -> list[tuple[Cell, Cell, int]]:
    by_cell = set(cells)

    def containing_cell(cell: Cell) -> Cell | None:
        current_res = h3.get_resolution(cell)
        for current_resolution in range(current_res, -1, -1):
            candidate = (
                cell
                if current_resolution == current_res
                else h3.cell_to_parent(cell, current_resolution)
            )
            if candidate in by_cell:
                return candidate
        return None

    seen: set[tuple[Cell, Cell]] = set()
    pairs: list[tuple[Cell, Cell, int]] = []
    for cell in cells:
        for neighbor in h3.grid_ring(cell, 1):
            other = containing_cell(neighbor)
            if other is None or other == cell:
                continue
            left, right = canonical_edge(cell, other)
            if (left, right) in seen:
                continue
            seen.add((left, right))
            count = interface_edge_count(left, right, resolution)
            if count:
                pairs.append((left, right, count))
    return pairs


def candidate_overlay_pairs(
    blocks: Sequence[_OverlayBlock],
    resolution: int,
) -> list[tuple[_OverlayBlock, _OverlayBlock, int]]:
    by_cell = {block.cell: block for block in blocks}
    seen: set[tuple[Cell, Cell]] = set()
    pairs: list[tuple[_OverlayBlock, _OverlayBlock, int]] = []

    def containing_block(cell: Cell) -> _OverlayBlock | None:
        current = cell
        current_res = h3.get_resolution(current)
        for _ in range(current_res + 1):
            found = by_cell.get(current)
            if found is not None:
                return found
            if current_res == 0:
                break
            current_res -= 1
            current = h3.cell_to_parent(cell, current_res)
        return None

    for block in blocks:
        for neighbor in h3.grid_ring(block.cell, 1):
            other = containing_block(neighbor)
            if other is None or other.cell == block.cell:
                continue
            key = canonical_edge(block.cell, other.cell)
            if key in seen:
                continue
            seen.add(key)
            count = interface_edge_count(block.cell, other.cell, resolution)
            if count:
                ordered = (block, other) if block.cell <= other.cell else (other, block)
                pairs.append((ordered[0], ordered[1], count))
    return pairs


def trace_edge(
    first: Cell,
    second: Cell,
    index1: VersionIndex,
    index2: VersionIndex,
    group_a: Group | None = None,
    group_b: Group | None = None,
) -> EdgeTrace:
    first, second = canonical_edge(str(first), str(second))
    adjacent = (
        h3.is_valid_cell(first)
        and h3.is_valid_cell(second)
        and h3.get_resolution(first) == index1.resolution == index2.resolution
        and h3.are_neighbor_cells(first, second)
    )
    trace1_a = index1.trace(first)
    trace1_b = index1.trace(second)
    trace2_a = index2.trace(first)
    trace2_b = index2.trace(second)
    in_v1 = bool(
        group_a
        and group_b
        and is_handover_pair(trace1_a.group, trace1_b.group, group_a, group_b)
    )
    in_v2 = bool(
        group_a
        and group_b
        and is_handover_pair(trace2_a.group, trace2_b.group, group_a, group_b)
    )
    if in_v1 and in_v2:
        status = 'continuing'
    elif in_v2:
        status = 'new'
    elif in_v1:
        status = 'disappeared'
    else:
        status = 'none'
    return EdgeTrace(
        edge=(first, second),
        adjacent=adjacent,
        in_v1=in_v1,
        in_v2=in_v2,
        status=status,
        v1=(trace1_a, trace1_b),
        v2=(trace2_a, trace2_b),
    )


def cell_bbox(cell: Cell) -> tuple[float, float, float, float]:
    boundary = h3.cell_to_boundary(cell)
    longs = [float(point[1]) for point in boundary]
    lats = [float(point[0]) for point in boundary]
    unwrapped = [longs[0]]
    for value in longs[1:]:
        previous = unwrapped[-1]
        while value - previous > 180:
            value -= 360
        while value - previous < -180:
            value += 360
        unwrapped.append(value)
    return min(lats), min(unwrapped), max(lats), max(unwrapped)


def bboxes_intersect(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    epsilon: float = 1e-10,
) -> bool:
    lat1_min, lng1_min, lat1_max, lng1_max = left
    lat2_min, lng2_min, lat2_max, lng2_max = right
    if lat1_min > lat2_max + epsilon or lat2_min > lat1_max + epsilon:
        return False
    for shift in (-360, 0, 360):
        if (
            lng1_min <= lng2_max + shift + epsilon
            and lng2_min + shift <= lng1_max + epsilon
        ):
            return True
    return False


def cell_bbox_lnglat(cell: Cell) -> tuple[float, float, float, float]:
    min_lat, min_lng, max_lat, max_lng = cell_bbox(cell)
    return min_lng, min_lat, max_lng, max_lat


def bbox_parent_candidates(
    parents: Iterable[Cell | Mapping[str, Any] | AllocationRow],
) -> list[tuple[Cell, Cell]]:
    cells = [_as_parent_cell(item) for item in parents]
    boxes = {cell: cell_bbox(cell) for cell in cells}
    candidates: list[tuple[Cell, Cell]] = []
    for index, left in enumerate(cells):
        for right in cells[index + 1 :]:
            if bboxes_intersect(boxes[left], boxes[right]):
                candidates.append((left, right))
    return candidates


def verified_parent_pairs(
    parents: Iterable[Cell | Mapping[str, Any] | AllocationRow],
    target_resolution: int,
) -> list[tuple[Cell, Cell]]:
    cells = [_as_parent_cell(item) for item in parents]
    verified: list[tuple[Cell, Cell]] = []
    for left, right in bbox_parent_candidates(cells):
        if interface_edge_count(left, right, target_resolution) > 0:
            verified.append(canonical_edge(left, right))
    return verified


def brute_force_edges(
    version: Any,
    resolution: int,
    group_a: Group,
    group_b: Group,
) -> set[tuple[Cell, Cell]]:
    index = as_version_index(version, resolution, 'brute')
    cells: set[Cell] = set()
    for row in index.rows:
        cells.update(h3.cell_to_children(row.parent, resolution))
    owners = {cell: index.trace(cell).group for cell in cells}
    edges: set[tuple[Cell, Cell]] = set()
    for cell, owner in owners.items():
        if owner != group_a:
            continue
        for neighbor in h3.grid_ring(cell, 1):
            if owners.get(neighbor) == group_b:
                edges.add(canonical_edge(cell, neighbor))
    return edges


def replay_counts(
    version1: Any,
    version2: Any,
    resolution: int,
    group_a: Group,
    group_b: Group,
) -> dict[str, Any]:
    edges1 = brute_force_edges(version1, resolution, group_a, group_b)
    edges2 = brute_force_edges(version2, resolution, group_a, group_b)
    continuing = edges1 & edges2
    new = edges2 - edges1
    disappeared = edges1 - edges2
    return {
        'continuing': len(continuing),
        'new': len(new),
        'disappeared': len(disappeared),
        'edges1': edges1,
        'edges2': edges2,
        'continuing_edges': continuing,
        'new_edges': new,
        'disappeared_edges': disappeared,
    }


def compare(
    version1: Any = None,
    version2: Any = None,
    group_a: Group | None = None,
    group_b: Group | None = None,
    resolution: int | None = None,
    *,
    target_resolution: int | None = None,
    res: int | None = None,
    **kwargs: Any,
) -> AuditResult:
    if isinstance(version1, Mapping) and version2 is None:
        payload = version1
        version1 = payload.get('version1') or payload.get('v1') or payload.get('first')
        version2 = payload.get('version2') or payload.get('v2') or payload.get('second')
        group_a = (
            group_a
            or payload.get('group_a')
            or payload.get('group1')
            or payload.get('a')
        )
        group_b = (
            group_b
            or payload.get('group_b')
            or payload.get('group2')
            or payload.get('b')
        )
        resolution = resolution if resolution is not None else payload.get('resolution')
    resolution = resolution if resolution is not None else target_resolution
    resolution = resolution if resolution is not None else res
    if resolution is None:
        raise HandoverError('target resolution is required')
    if group_a is None or group_b is None:
        raise HandoverError('two group names are required')
    index = HandoverIndex(
        version1,
        version2,
        resolution,
        target_resolution=target_resolution,
        res=res,
    )
    return index.query(group_a, group_b)


def count_handover_edges(
    version: Any, resolution: int, group_a: Group, group_b: Group
) -> int:
    index = as_version_index(version, resolution, 'v')
    return index.edges_between(group_a, group_b)


def audit_handover_edges(*args: Any, **kwargs: Any) -> AuditResult:
    return compare(*args, **kwargs)


audit = compare
compare_versions = compare
handover_diff = compare
diff_handover_edges = compare
reconcile_handover_edges = compare


def build_ownership_index(rows: Any, resolution: int) -> VersionIndex:
    return VersionIndex(rows, resolution)


def prepare_ownership_index(rows: Any) -> AllocationTable:
    return AllocationTable(rows)


def build_audit_index(version1: Any, version2: Any, resolution: int) -> HandoverIndex:
    return HandoverIndex(version1, version2, resolution).prepare()


def build_index(*args: Any, **kwargs: Any) -> Any:
    resolution = kwargs.pop(
        'resolution', kwargs.pop('target_resolution', kwargs.pop('res', None))
    )
    version1 = kwargs.pop('version1', kwargs.pop('v1', None))
    version2 = kwargs.pop('version2', kwargs.pop('v2', None))
    if version1 is not None and version2 is not None:
        if resolution is None:
            raise HandoverError('resolution is required for a two-version index')
        return build_audit_index(version1, version2, resolution)
    if len(args) == 1 and resolution is None:
        return prepare_ownership_index(args[0])
    if len(args) == 2 and resolution is None and isinstance(args[1], int):
        return build_ownership_index(args[0], args[1])
    if len(args) == 2 and resolution is not None:
        return build_audit_index(args[0], args[1], resolution)
    if len(args) == 3 and resolution is None and isinstance(args[2], int):
        return build_audit_index(args[0], args[1], args[2])
    if resolution is not None and version1 is not None:
        return build_ownership_index(version1, resolution)
    raise HandoverError(
        'call build_index(rows, resolution) or build_index(v1, v2, resolution)'
    )


prepare_index = build_index
OwnershipIndex = VersionIndex
AllocationIndex = VersionIndex
OwnershipTable = AllocationTable
OwnershipCatalog = AllocationTable
AllocationCatalog = AllocationTable
HandoverAuditor = HandoverIndex
BoundaryAuditor = HandoverIndex
AuditWorkspace = HandoverWorkspace
HandoverAuditWorkspace = HandoverWorkspace
prepare_table = AllocationTable
prepare_workspace = HandoverWorkspace

candidate_parent_pairs = bbox_parent_candidates
bbox_candidate_pairs = bbox_parent_candidates
parent_bbox_candidates = bbox_parent_candidates
bounding_box = cell_bbox
cell_bounding_box = cell_bbox
exact_parent_pairs = verified_parent_pairs
verify_parent_pairs_exact = verified_parent_pairs
hierarchy_verified_parent_pairs = verified_parent_pairs
brute_force_compare = replay_counts
replay_audit = replay_counts
validate_counts_with_replay = replay_counts
trace_change = trace_edge
edge_provenance = trace_edge
provenance_for_edge = trace_edge
edge_appearances = trace_edge


def _first_value(
    mapping: Mapping[str, Any], keys: Sequence[str], default: Any = None
) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return int(text)
    return int(value)


def _parse_range(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, Mapping):
        return _parse_int(_first_value(value, _POSITION_KEYS)), _parse_int(
            _first_value(value, _END_KEYS)
        )
    if isinstance(value, slice):
        return value.start, value.stop
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            return _parse_int(value[0]), _parse_int(value[1])
        if len(value) == 3:
            return _parse_int(value[0]), _parse_int(value[0]) + _parse_int(value[2])
    raise HandoverError(f'invalid position range {value!r}')


def _as_parent_cell(item: Any) -> Cell:
    if isinstance(item, AllocationRow):
        return item.parent
    if isinstance(item, Mapping):
        parent = _first_value(item, _PARENT_KEYS)
        if parent is None:
            raise HandoverError(f'no parent cell in {item!r}')
        return str(parent)
    return str(item)


def _normalize_status(status: str) -> str:
    status = status.lower()
    if status in {'kept', 'stable', 'shared', 'both', 'continuing'}:
        return 'continuing'
    if status in {'added', 'new'}:
        return 'new'
    if status in {'removed', 'gone', 'disappeared'}:
        return 'disappeared'
    return status


def _row_to_json(row: AllocationRow) -> dict[str, Any]:
    return {
        'index': row.index,
        'line_number': row.index + 1,
        'row_id': row.row_id,
        'parent': row.parent,
        'group': row.group,
        'start': row.start,
        'end': row.end,
        'cancel': row.cancel,
    }


def _load_json(path: str) -> Any:
    if path == '-':
        return json.loads(Path('/dev/stdin').read_text())
    return json.loads(Path(path).read_text())


def _rows_from_json(data: Any) -> Any:
    if isinstance(data, Mapping):
        for key in ('rows', 'allocations', 'allocation_table', 'items', 'data'):
            if key in data:
                return data[key]
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Audit H3 handover edges between two allocation versions.'
    )
    parser.add_argument(
        'files', nargs='*', help='one combined payload or two row-list JSON files'
    )
    parser.add_argument('--resolution', '-r', type=int, default=None)
    parser.add_argument(
        '--groups', '-g', nargs=2, metavar=('GROUP_A', 'GROUP_B'), default=None
    )
    parser.add_argument(
        '--replay', action='store_true', help='also run short-list brute H3 replay'
    )
    args = parser.parse_args(argv)

    if len(args.files) == 1:
        payload = _load_json(args.files[0])
        result = compare(payload)
    elif len(args.files) == 2:
        if args.resolution is None or args.groups is None:
            raise HandoverError('two row files require --resolution and --groups')
        version1 = _rows_from_json(_load_json(args.files[0]))
        version2 = _rows_from_json(_load_json(args.files[1]))
        result = compare(
            version1, version2, args.groups[0], args.groups[1], args.resolution
        )
    else:
        raise HandoverError('provide one combined payload file or two row files')

    output: dict[str, Any] = {
        'counts': result.summary(),
        'unique_pairs': result.unique_pairs,
        'v1_edges': result.v1_edges,
        'v2_edges': result.v2_edges,
    }
    if args.replay:
        replay = replay_counts(
            result.index1.rows,
            result.index2.rows,
            result.resolution,
            result.group_a,
            result.group_b,
        )
        expected = (replay['continuing'], replay['new'], replay['disappeared'])
        output['replay'] = {
            'counts': {
                key: replay[key] for key in ('continuing', 'new', 'disappeared')
            },
            'matches': result.counts == expected,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
