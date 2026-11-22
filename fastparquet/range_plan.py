"""Byte-range planning for remote Parquet reads.

Given a :class:`~fastparquet.api.ParquetFile` whose footer metadata is already
available, this module plans which byte ranges of the underlying object-storage
file have to be fetched to produce a filtered, column-projected report.

Ranges fall into three roles:

``required``
    Pages (or dictionary pages) that carry values the report must return, or
    filter-column pages whose statistics are missing and which therefore can
    never be proven empty.

``locating``
    Bytes that are only needed to locate data - page headers of skipped pages,
    or pages belonging to filter columns that are not part of the projection.
    These never contribute output values themselves.

``skipped``
    Pages that statistics prove cannot contain matching rows, or whole column
    chunks of row groups excluded by row-group statistics.  A page without
    statistics is never skipped: missing statistics do not make a page empty.

Dictionary pages are a single byte range shared by every data page of a column
chunk.  Range merging guarantees the shared vocabulary is fetched and billed
exactly once.

The planned ranges can then be executed (:func:`execute_range_plan`) and the
result is aligned to the original, full-read row numbers.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from fastparquet import core, encoding, parquet_thrift, converted_types
from fastparquet.api import filter_out_stats, filter_val
from fastparquet.cencoding import ThriftObject
from fastparquet.util import ensure_bytes, ops

REQUIRED = "required"
LOCATING = "locating"
SKIPPED = "skipped"

_ROLE_RANK = {SKIPPED: 0, LOCATING: 1, REQUIRED: 2}
_HEADER_WINDOW_START = 64


@dataclass(frozen=True)
class ByteRange:
    """A half-open byte interval ``[offset, offset + length)``."""

    offset: int
    length: int
    role: str = REQUIRED
    reason: str = ""

    @property
    def end(self):
        return self.offset + self.length

    def to_dict(self):
        return {"offset": self.offset, "length": self.length,
                "end": self.end, "role": self.role, "reason": self.reason}


@dataclass
class PagePlan:
    """Planned fate of a single page inside a column chunk."""

    kind: str
    offset: int
    end: int
    num_values: int
    role: str
    reason: str
    row_start: int = 0
    has_statistics: bool = False
    header: object = None

    @property
    def range(self):
        return ByteRange(self.offset, self.end - self.offset, self.role,
                         self.reason)


@dataclass
class ChunkPlan:
    """Planned fate of one column chunk in one row group."""

    row_group: int
    column: str
    file_path: str
    pages: list = field(default_factory=list)
    role: str = REQUIRED
    reason: str = ""

    @property
    def ranges(self):
        return [p.range for p in self.pages]


@dataclass
class RangePlan:
    """The full, executable range plan for a query."""

    columns: list
    filters: list
    request_limit: int
    request_overhead: int
    file_path: str
    chunk_plans: list = field(default_factory=list)
    required_ranges: list = field(default_factory=list)
    locating_ranges: list = field(default_factory=list)
    skipped_ranges: list = field(default_factory=list)
    fetch_ranges: list = field(default_factory=list)
    fetch_ranges_by_file: dict = field(default_factory=dict)
    coalesced: bool = False
    coalesced_extra_bytes: int = 0

    @property
    def num_requests(self):
        return sum(len(ranges) for ranges in
                   self.fetch_ranges_by_file.values())

    @property
    def fetched_bytes(self):
        return sum(r.length for ranges in self.fetch_ranges_by_file.values()
                   for r in ranges)

    @property
    def skipped_bytes(self):
        return sum(r.length for r in self.skipped_ranges)

    @property
    def estimated_cost(self):
        """Bytes transferred plus per-request overhead, billed once per range."""
        return self.fetched_bytes + self.num_requests * self.request_overhead

    @property
    def within_budget(self):
        return all(len(ranges) <= self.request_limit
                   for ranges in self.fetch_ranges_by_file.values())

    def to_dict(self):
        return {
            "file_path": self.file_path,
            "columns": list(self.columns),
            "filters": self.filters,
            "request_limit": self.request_limit,
            "request_overhead": self.request_overhead,
            "num_requests": self.num_requests,
            "fetched_bytes": self.fetched_bytes,
            "skipped_bytes": self.skipped_bytes,
            "estimated_cost": self.estimated_cost,
            "within_budget": self.within_budget,
            "coalesced": self.coalesced,
            "coalesced_extra_bytes": self.coalesced_extra_bytes,
            "required_ranges": [r.to_dict() for r in self.required_ranges],
            "locating_ranges": [r.to_dict() for r in self.locating_ranges],
            "fetch_ranges": [r.to_dict() for r in self.fetch_ranges],
            "fetch_ranges_by_file": {
                fn: [r.to_dict() for r in ranges]
                for fn, ranges in self.fetch_ranges_by_file.items()},
            "skipped_ranges": [r.to_dict() for r in self.skipped_ranges],
        }

    def describe(self):
        lines = [
            "Range plan for %s" % self.file_path,
            "  columns: %s" % list(self.columns),
            "  filters: %s" % (self.filters or []),
            "  requests: %d (limit %d%s)" % (
                self.num_requests, self.request_limit,
                ", coalesced" if self.coalesced else ""),
            "  fetched bytes: %d, skipped bytes: %d, overhead/request: %d" % (
                self.fetched_bytes, self.skipped_bytes, self.request_overhead),
            "  estimated cost: %d bytes, within budget: %s" % (
                self.estimated_cost, self.within_budget),
        ]
        for role, ranges in ((REQUIRED, self.required_ranges),
                             (LOCATING, self.locating_ranges),
                             (SKIPPED, self.skipped_ranges)):
            lines.append("  %s (%d):" % (role, len(ranges)))
            for r in ranges:
                lines.append("    [%d, %d) %d bytes%s" % (
                    r.offset, r.end, r.length,
                    " - " + r.reason if r.reason else ""))
        return "\n".join(lines)


def _normalize_filters(filters):
    """Return OR-of-AND filter form: list of list of (column, op, value)."""
    if not filters:
        return [[]]
    if isinstance(filters[0][0], str):
        return [list(filters)]
    return [list(branch) for branch in filters]


def _merge_ranges(ranges):
    """Merge overlapping/adjacent byte ranges, keeping the strongest role."""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r.offset)
    merged = []
    for rng in ordered:
        if merged and rng.offset <= merged[-1].end:
            prev = merged[-1]
            end = max(prev.end, rng.end)
            role = prev.role if _ROLE_RANK[prev.role] >= _ROLE_RANK[rng.role] \
                else rng.role
            reasons = [x for x in (prev.reason, rng.reason) if x]
            merged[-1] = ByteRange(prev.offset, end - prev.offset, role,
                                   "; ".join(sorted(set(reasons))))
        else:
            merged.append(rng)
    return merged


def _coalesce_to_budget(ranges, request_limit):
    """Greedily merge the closest ranges until the request count fits."""
    ranges = list(ranges)
    extra = 0
    coalesced = False
    while len(ranges) > request_limit and len(ranges) > 1:
        gaps = [(ranges[i + 1].offset - ranges[i].end, i)
                for i in range(len(ranges) - 1)]
        _, idx = min(gaps)
        left, right = ranges[idx], ranges[idx + 1]
        gap = right.offset - left.end
        extra += max(gap, 0)
        role = left.role if _ROLE_RANK[left.role] >= _ROLE_RANK[right.role] \
            else right.role
        reasons = [x for x in (left.reason, right.reason) if x]
        merged = ByteRange(left.offset, right.end - left.offset, role,
                           "; ".join(sorted(set(reasons))))
        ranges[idx:idx + 2] = [merged]
        coalesced = True
    return ranges, coalesced, extra


def _column_name(column):
    return ".".join(column.meta_data.path_in_schema)


def _read_page_header(fobj, header_pos, max_end):
    """Decode one page header without pulling the whole page body.

    The window starts small and doubles until the thrift stop byte is found
    *inside* it.  A parse that consumes the whole window is untrusted: the
    compact-thrift decoder can run past the end and return a truncated header
    without raising, so completion must be proven by a trailing byte.
    """
    window = _HEADER_WINDOW_START
    while True:
        size = min(window, max_end - header_pos)
        fobj.seek(header_pos)
        buffer = fobj.read(size)
        io = encoding.NumpyIO(buffer)
        try:
            parsed = ThriftObject.from_buffer(io, "PageHeader")
        except (IndexError, ValueError) as exc:
            if size == max_end - header_pos:
                raise IOError(
                    "truncated page header at offset %d" % header_pos) from exc
            window *= 2
            continue
        complete = io.tell() < len(buffer)
        well_formed = (parsed.type is not None and
                       parsed.compressed_page_size is not None and
                       parsed.uncompressed_page_size is not None)
        at_limit = size == max_end - header_pos
        if well_formed and (complete or at_limit):
            return parsed, io.tell()
        if at_limit:
            raise IOError(
                "incomplete page header at offset %d" % header_pos)
        window *= 2


def scan_column_pages(pf, file_path, column, fobj=None):
    """Walk a column chunk using footer offsets only.

    Only page headers are read here; page bodies are never touched, so
    scanning is cheap on remote storage.
    """
    cmd = column.meta_data
    start = min(x for x in (cmd.dictionary_page_offset, cmd.data_page_offset)
                if x is not None)
    opened = fobj is None
    fobj = fobj or pf.open(file_path, mode="rb")
    fobj.seek(start)
    end = start + cmd.total_compressed_size
    pages = []
    row_start = 0
    try:
        while fobj.tell() < end:
            header_pos = fobj.tell()
            header, header_length = _read_page_header(fobj, header_pos, end)
            header_end = header_pos + header_length
            body_size = header.compressed_page_size
            page_end = header_end + body_size

            if header.type == parquet_thrift.PageType.DICTIONARY_PAGE:
                kind = "dictionary"
                num_values = header.dictionary_page_header.num_values
                stats = None
            elif header.type == parquet_thrift.PageType.DATA_PAGE:
                kind = "data_page"
                daph = header.data_page_header
                num_values = daph.num_values
                stats = daph.statistics
            elif header.type == parquet_thrift.PageType.DATA_PAGE_V2:
                kind = "data_page_v2"
                daph = header.data_page_header_v2
                num_values = daph.num_values
                stats = daph.statistics
            else:
                kind = "other"
                num_values = 0
                stats = None

            pages.append(PagePlan(
                kind=kind,
                offset=header_pos,
                end=page_end,
                num_values=num_values,
                role=REQUIRED,
                reason="",
                row_start=row_start,
                has_statistics=stats is not None,
                header=header,
            ))
            if kind.startswith("data_page"):
                row_start += num_values
            fobj.seek(page_end)
    finally:
        if opened:
            fobj.close()
    return pages


def _page_stats(page):
    header = page.header
    if page.kind == "data_page":
        return header.data_page_header.statistics
    if page.kind == "data_page_v2":
        return header.data_page_header_v2.statistics
    return None


def _page_stat_bounds(page, column, schema_helper):
    """Decode page-level min/max statistics, mirroring row-group stats logic."""
    cmd = column.meta_data
    stats = _page_stats(page)
    if stats is None:
        return None, None
    name = _column_name(column)
    se = schema_helper.schema_element(name)
    vmax = vmin = None
    raw_max = stats.max or stats.max_value
    if raw_max is not None:
        vmax = encoding.read_plain(ensure_bytes(raw_max), cmd.type, 1,
                                   stat=True)
        if se.converted_type is not None or se.logicalType is not None:
            vmax = converted_types.convert(vmax, se)
    raw_min = stats.min or stats.min_value
    if raw_min is not None:
        vmin = encoding.read_plain(ensure_bytes(raw_min), cmd.type, 1,
                                   stat=True)
        if se.converted_type is not None or se.logicalType is not None:
            vmin = converted_types.convert(vmin, se)
    return vmin, vmax


def _page_excluded(page, column, predicates, schema_helper):
    """True only when statistics prove every row in the page fails.

    A page without statistics is never excluded: absence of statistics is not
    evidence that the page is empty.
    """
    if not predicates:
        return False
    stats = _page_stats(page)
    if stats is None:
        return False
    if stats.null_count is not None and \
            stats.null_count == page.num_values and page.num_values > 0:
        return True
    vmin, vmax = _page_stat_bounds(page, column, schema_helper)
    for op, value in predicates:
        if filter_val(op, value, vmin, vmax):
            return True
    return False


def _row_group_excluded(rg, filters, schema_helper):
    """Row group is skippable only if every OR branch excludes it."""
    if rg.num_rows == 0:
        return True
    active = [branch for branch in filters if branch]
    if not active:
        return False
    return all(filter_out_stats(rg, branch, schema_helper)
               for branch in active)


def _data_pages(pages):
    return [p for p in pages if p.kind in ("data_page", "data_page_v2")]


def _dead_row_mask(rg, scans, filters, schema_helper):
    """Rows proven unmatchable from filter-column page statistics.

    ``scans`` maps column name to ``(column_chunk, pages)``.  A row is only
    marked dead when statistics prove it fails *every* OR branch.  Pages
    without statistics contribute no information and never widen the mask.
    """
    dead = np.ones(rg.num_rows, dtype=bool)
    active = [branch for branch in filters if branch]
    if not active:
        return np.zeros(rg.num_rows, dtype=bool)
    for branch in active:
        branch_dead = np.ones(rg.num_rows, dtype=bool)
        evidence = False
        involved = {name for name, _, _ in branch}
        for name in involved:
            if name not in scans:
                continue
            column, pages = scans[name]
            predicates = [(op, val) for (col, op, val) in branch
                          if col == name]
            if not predicates:
                continue
            column_dead = np.zeros(rg.num_rows, dtype=bool)
            for page in _data_pages(pages):
                if _page_excluded(page, column, predicates, schema_helper):
                    column_dead[page.row_start:page.row_start +
                                page.num_values] = True
            branch_dead &= column_dead
            evidence = True
        if not evidence:
            return np.zeros(rg.num_rows, dtype=bool)
        dead &= branch_dead
    return dead


def build_range_plan(pf, columns, filters=None, request_limit=100,
                     request_overhead=0):
    """Plan the byte ranges needed to deliver the filtered, projected report.

    Parameters
    ----------
    pf: ParquetFile
        Footer metadata must already be loaded; no data pages are read by the
        planner itself beyond page headers.
    columns: list[str]
        Reported (projected) columns.
    filters: list
        Standard fastparquet predicate form, either a flat list of
        ``(column, op, value)`` tuples or the OR-of-ANDs nested form.
    request_limit: int
        Maximum number of range requests allowed.  When the natural plan needs
        more, adjacent ranges are coalesced and the extra pulled bytes are
        reported.
    request_overhead: int
        Fixed byte cost attributed to every range request, used for cost
        reporting only.
    """
    columns = list(columns)
    filters = _normalize_filters(filters)
    filter_columns = {t[0] for branch in filters for t in branch}
    schema_helper = pf.schema

    chunk_plans = []
    role_ranges_by_file = {}
    open_files = {}

    def _bucket(path):
        return role_ranges_by_file.setdefault(
            path, {REQUIRED: [], LOCATING: [], SKIPPED: []})

    def _scan(path, column):
        if path not in open_files:
            open_files[path] = pf.open(path, mode="rb")
        return scan_column_pages(pf, path, column, fobj=open_files[path])

    try:
        for rg_idx, rg in enumerate(pf.row_groups):
            rg_excluded = _row_group_excluded(rg, filters, schema_helper)
            scans = {}
            relevant = []
            for column in rg.columns:
                name = _column_name(column)
                if name not in columns and name not in filter_columns:
                    continue
                file_path = pf.row_group_filename(rg)
                pages = _scan(file_path, column)
                scans[name] = (column, pages)
                relevant.append((name, column, file_path, pages))

            if rg_excluded:
                dead = np.ones(rg.num_rows, dtype=bool)
            else:
                dead = _dead_row_mask(rg, scans, filters, schema_helper)

            for name, column, file_path, pages in relevant:
                buckets = _bucket(file_path)
                is_filter_col = name in filter_columns
                is_projected = name in columns
                base_role = REQUIRED if is_projected else LOCATING
                base_reason = "" if is_projected else \
                    "filter-only column, used to locate matching rows"
                chunk_role = base_role
                all_data_skipped = bool(_data_pages(pages))

                for page in pages:
                    if page.kind == "dictionary":
                        continue
                    if page.kind not in ("data_page", "data_page_v2"):
                        page.role = base_role
                        page.reason = base_reason
                        continue
                    start = page.row_start
                    end = start + page.num_values
                    if dead[start:end].all():
                        page.role = SKIPPED
                        if not page.has_statistics:
                            page.reason = (
                                "aligned filter-column statistics prove rows "
                                "%d-%d fail; page itself has no statistics and "
                                "is not treated as empty on its own"
                                % (start, end - 1))
                        else:
                            page.reason = (
                                "page statistics prove rows %d-%d fail every "
                                "predicate branch" % (start, end - 1))
                        continue
                    all_data_skipped = False
                    page.role = base_role
                    page.reason = base_reason
                    if is_filter_col and not page.has_statistics:
                        page.reason = (
                            (page.reason + "; " if page.reason else "")
                            + "statistics unavailable, page cannot be assumed "
                            "empty")

                for page in pages:
                    if page.kind != "dictionary":
                        continue
                    if all_data_skipped:
                        page.role = SKIPPED
                        page.reason = \
                            "all data pages are provably excluded, shared " \
                            "dictionary vocabulary is not needed"
                    else:
                        page.role = base_role
                        page.reason = base_reason or \
                            "shared dictionary page, billed once"

                if all_data_skipped:
                    chunk_role = SKIPPED
                    chunk_reason = "all pages provably excluded"
                else:
                    chunk_reason = base_reason
                plan = ChunkPlan(row_group=rg_idx, column=name,
                                 file_path=file_path, pages=pages,
                                 role=chunk_role, reason=chunk_reason)
                chunk_plans.append(plan)
                for page in pages:
                    buckets[page.role].append(page.range)
    finally:
        for fobj in open_files.values():
            fobj.close()

    required_all, locating_all, skipped_all, fetch_by_file = [], [], [], {}
    coalesced_any = False
    extra_total = 0
    for file_path, buckets in role_ranges_by_file.items():
        required = _merge_ranges(buckets[REQUIRED])
        locating = _merge_ranges(buckets[LOCATING])
        skipped = _merge_ranges(buckets[SKIPPED])
        locating = [
            r for r in locating
            if not any(r.offset >= q.offset and r.end <= q.end
                       for q in required)
        ]
        fetch, coalesced, extra = _coalesce_to_budget(
            required + locating, request_limit)
        required_all.extend(required)
        locating_all.extend(locating)
        skipped_all.extend(skipped)
        fetch_by_file[file_path] = fetch
        coalesced_any = coalesced_any or coalesced
        extra_total += extra

    fetch_all = [r for ranges in fetch_by_file.values() for r in ranges]
    return RangePlan(
        columns=columns,
        filters=filters,
        request_limit=request_limit,
        request_overhead=request_overhead,
        file_path=pf.fn,
        chunk_plans=chunk_plans,
        required_ranges=sorted(required_all, key=lambda r: r.offset),
        locating_ranges=sorted(locating_all, key=lambda r: r.offset),
        skipped_ranges=sorted(skipped_all, key=lambda r: r.offset),
        fetch_ranges=sorted(fetch_all, key=lambda r: r.offset),
        fetch_ranges_by_file=fetch_by_file,
        coalesced=coalesced_any,
        coalesced_extra_bytes=extra_total,
    )


class _BufferArena:
    """Serves fetched range bytes by absolute file offset."""

    def __init__(self, buffers):
        self._buffers = sorted(buffers, key=lambda item: item[0])

    def slice(self, offset, length):
        for base, data in self._buffers:
            if base <= offset and offset + length <= base + len(data):
                start = offset - base
                return memoryview(data)[start:start + length]
        raise IOError("bytes [%d, %d) are not part of the range plan"
                      % (offset, offset + length))


def _default_range_reader(pf, file_path, ranges):
    """Fetch byte ranges through the ParquetFile's open_with (local/fsspec)."""
    fobj = pf.open(file_path, mode="rb")
    buffers = []
    try:
        for rng in ranges:
            fobj.seek(rng.offset)
            buffers.append((rng.offset, fobj.read(rng.length)))
    finally:
        fobj.close()
    return buffers


def _data_offset(header, page):
    """Absolute offset of a page body given the page's header interval."""
    header_length = page.end - page.offset - header.compressed_page_size
    return page.offset + header_length


def _allocate_column(helper, column, num_rows):
    cmd = column.meta_data
    if not helper.is_required(cmd.path_in_schema):
        # object arrays carry NULLs and skipped-page placeholders safely
        return np.empty(num_rows, dtype=object)
    if cmd.type == parquet_thrift.Type.BYTE_ARRAY:
        return np.empty(num_rows, dtype=object)
    dtype = {
        parquet_thrift.Type.INT32: "int32",
        parquet_thrift.Type.INT64: "int64",
        parquet_thrift.Type.FLOAT: "float32",
        parquet_thrift.Type.DOUBLE: "float64",
    }.get(cmd.type, object)
    fill = np.zeros if dtype != object else np.empty
    return fill(num_rows, dtype=dtype)


def _read_planned_chunk(pf, chunk_plan, column, arena):
    """Decode the non-skipped pages of one column chunk from planned bytes.

    Positions belonging to skipped pages are left at the array default; the
    caller forces those rows out of the row mask, so placeholder values never
    leak into the result.
    """
    helper = pf.schema
    cmd = column.meta_data
    se = helper.schema_element(_column_name(column))
    rg = pf.row_groups[chunk_plan.row_group]
    out = _allocate_column(helper, column, rg.num_rows)
    dictionary = None
    row_cursor = 0

    for page in chunk_plan.pages:
        header = page.header
        if page.kind == "dictionary":
            if page.role == SKIPPED:
                continue
            payload = arena.slice(_data_offset(header, page),
                                  header.compressed_page_size)
            dictionary = core.read_dictionary_page(
                encoding.NumpyIO(payload), helper, header, cmd,
                utf=se.converted_type == 0)
            dictionary = converted_types.convert(dictionary, se)
            continue
        if page.kind not in ("data_page", "data_page_v2"):
            continue
        num_values = page.num_values
        if page.role == SKIPPED:
            row_cursor += num_values
            continue
        if page.kind == "data_page_v2":
            raise NotImplementedError(
                "page-level range execution only supports v1 data pages; "
                "column %s contains a v2 page" % chunk_plan.column)
        payload = arena.slice(_data_offset(header, page),
                              header.compressed_page_size)
        defi, rep, val = core.read_data_page(
            encoding.NumpyIO(payload), helper, header, cmd)
        if rep is not None:
            raise NotImplementedError(
                "range execution does not support nested/repeated column %s"
                % chunk_plan.column)
        daph = header.data_page_header
        is_dict = daph.encoding in (
            parquet_thrift.Encoding.PLAIN_DICTIONARY,
            parquet_thrift.Encoding.RLE_DICTIONARY,
        )
        if is_dict:
            values = dictionary[val]
        else:
            values = converted_types.convert(val, se)
        if defi is not None:
            max_def = helper.max_definition_level(cmd.path_in_schema)
            full = np.empty(num_values, dtype=object)
            full[defi == max_def] = values
            out[row_cursor:row_cursor + num_values] = full
        else:
            out[row_cursor:row_cursor + num_values] = values
        row_cursor += num_values
    return out


def _predicate_mask(df, filters):
    """Evaluate OR-of-ANDs predicates on a full-row-group DataFrame."""
    mask = np.zeros(len(df), dtype=bool)
    for branch in filters:
        branch_mask = np.ones(len(df), dtype=bool)
        for name, op, value in branch:
            series = df[name]
            if op == "in":
                branch_mask &= np.asarray(series.isin(value), dtype=bool)
            elif op == "not in":
                branch_mask &= ~np.asarray(series.isin(value), dtype=bool)
            elif op == "~":
                branch_mask &= ~np.asarray(series, dtype=bool)
            elif op in ops:
                branch_mask &= np.asarray(ops[op](series, value), dtype=bool)
            else:
                raise ValueError("Unsupported filter operator: %s" % op)
        mask |= branch_mask
    return mask


def execute_range_plan(pf, plan, range_reader: Optional[Callable] = None):
    """Execute a :class:`RangePlan` and return the filtered report.

    Parameters
    ----------
    pf: ParquetFile
    plan: RangePlan
        Output of :func:`build_range_plan`.
    range_reader: callable, optional
        ``(pf, file_path, ranges) -> [(offset, bytes), ...]``.  Defaults to
        seek/read through ``pf.open``; inject an object-store range getter
        (e.g. fsspec ``cat_ranges``) for remote execution.

    Returns
    -------
    pandas.DataFrame
        Indexed by the original row numbers of a full, unfiltered read, so the
        output can be joined one-to-one against a full-read reference.
    """
    reader = range_reader or _default_range_reader
    buffers_by_file = {
        file_path: _BufferArena(reader(pf, file_path, ranges))
        for file_path, ranges in plan.fetch_ranges_by_file.items()
    }

    frames = []
    row_offset = 0
    filter_names = {t[0] for branch in plan.filters for t in branch}
    for rg_idx, rg in enumerate(pf.row_groups):
        file_path = pf.row_group_filename(rg)
        chunks_here = [c for c in plan.chunk_plans if c.row_group == rg_idx]
        if not chunks_here or all(c.role == SKIPPED for c in chunks_here):
            row_offset += rg.num_rows
            continue

        arena = buffers_by_file[file_path]
        filter_data = {}
        skipped_spans = []

        for chunk_plan in chunks_here:
            column = next(c for c in rg.columns
                          if _column_name(c) == chunk_plan.column)
            for page in chunk_plan.pages:
                if page.role == SKIPPED and \
                        page.kind in ("data_page", "data_page_v2"):
                    skipped_spans.append(
                        (page.row_start, page.row_start + page.num_values))
            if chunk_plan.role == SKIPPED:
                continue
            if chunk_plan.column in filter_names:
                filter_data[chunk_plan.column] = _read_planned_chunk(
                    pf, chunk_plan, column, arena)

        missing = filter_names - set(filter_data)
        if missing and not skipped_spans:
            raise RuntimeError(
                "filter column(s) %s have no readable pages in row group %d"
                % (sorted(missing), rg_idx))

        if filter_data:
            mask = _predicate_mask(pd.DataFrame(filter_data), plan.filters)
        else:
            mask = np.ones(rg.num_rows, dtype=bool)
        for start, end in skipped_spans:
            mask[start:end] = False

        result = {}
        for chunk_plan in chunks_here:
            if chunk_plan.column not in plan.columns:
                continue
            column = next(c for c in rg.columns
                          if _column_name(c) == chunk_plan.column)
            if chunk_plan.column in filter_data:
                values = filter_data[chunk_plan.column]
            else:
                values = _read_planned_chunk(pf, chunk_plan, column, arena)
            result[chunk_plan.column] = values[mask]

        frame = pd.DataFrame(result, columns=plan.columns)
        frame.index = np.nonzero(mask)[0] + row_offset
        frames.append(frame)
        row_offset += rg.num_rows

    if not frames:
        return pd.DataFrame(columns=plan.columns)
    return pd.concat(frames, axis=0)
