"""
Planning support for evaluating a window query over local shards.

``plan_shards`` takes a query, the coverage predicate of each shard and the
auxiliary columns that are allowed to cross a shard boundary, and produces a
``ShardPlan``: one rewritten query per shard plus the handoff contract that
lets a coordinator merge the partial results back into the result of the
original query.

The boundary contract is, per window:

* a deterministic sort boundary: the window's ORDER BY keys. ROWS frames and
  ROW_NUMBER additionally require a declared stable row id in the ORDER BY so
  the global row order is total,
* the necessary columns: partition keys, order keys and window arguments,
  either already projected or explicitly allowed via ``aux_columns``,
* peer counts: for RANK / DENSE_RANK, how many rows before the boundary share
  the boundary key, so ties split across shards keep the same rank,
* frame rows: for ROWS BETWEEN k PRECEDING AND CURRENT ROW, the last k rows of
  the partition. Borrowing only the previous shard's running total is wrong
  (rows slide out of the window) and treating a shard's first row as the
  global first row is wrong (the window would be too short).

Shards must be disjoint, cover the source table, and be ordered so that every
window partition forms contiguous segments in shard order under each window's
ORDER BY. Anything the boundary cannot cover is reported in
``ShardPlan.unsupported`` as the exact expression that cannot be sharded.
"""

from __future__ import annotations

import dataclasses
import typing as t

from sqlglot import exp, parse_one


class ShardingError(Exception):
    """Raised when a shard plan cannot be executed or merged."""


@dataclasses.dataclass
class ShardCoverage:
    """One local shard and the predicate describing the rows it holds."""

    name: str
    predicate: str


@dataclasses.dataclass
class WindowHandoff:
    """What must cross a shard boundary for one window to be mergeable."""

    alias: str
    kind: str  # "rank" | "dense_rank" | "row_number" | "frame"
    window: str
    partition_by: list[str]
    order_by: list[str]
    order_desc: list[bool]
    carry: dict[str, str]  # column name -> output key in shard rows
    peer_counts: bool = False
    row_counts: bool = False
    func: str | None = None
    arg: str | None = None
    frame_preceding: int | None = None
    unbounded: bool = False
    sum_key: str | None = None
    count_key: str | None = None


@dataclasses.dataclass
class ShardPlan:
    shards: list[ShardCoverage]
    shard_sql: list[str]
    handoffs: list[WindowHandoff]
    aux_columns: list[str]
    unsupported: list[str]
    order_by: list[tuple[str, bool]] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unsupported

    def describe(self) -> str:
        lines = []
        for coverage, sql in zip(self.shards, self.shard_sql):
            lines.append(f"shard {coverage.name} [{coverage.predicate}]: {sql}")
        for handoff in self.handoffs:
            needs = []
            if handoff.row_counts:
                needs.append("row count before shard")
            if handoff.peer_counts:
                needs.append("peer count at boundary key")
            if handoff.frame_preceding is not None:
                needs.append(f"last {handoff.frame_preceding} row(s) of {handoff.arg or '*'}")
            if handoff.unbounded:
                needs.append("running aggregate")
            lines.append(
                f"handoff for {handoff.alias} ({handoff.window}): "
                f"sort boundary ({', '.join(handoff.order_by)}); "
                f"carry ({', '.join(handoff.carry)}); "
                f"needs {', '.join(needs) if needs else 'nothing'}"
            )
        if self.unsupported:
            lines.append("unsupported: " + "; ".join(self.unsupported))
        return "\n".join(lines)


_RANK_FRAME_FUNCS = {
    "sum": exp.Sum,
    "count": exp.Count,
    "avg": exp.Avg,
    "min": exp.Min,
    "max": exp.Max,
}


def plan_shards(
    query: str | exp.Expression,
    coverages: t.Sequence[str | ShardCoverage],
    aux_columns: t.Sequence[str] = (),
    stable_keys: t.Sequence[str] = (),
    dialect: str | None = None,
) -> ShardPlan:
    """
    Build a shard plan for ``query``.

    Args:
        query: the original query; a plain SELECT over one base table.
        coverages: one coverage predicate (or ShardCoverage) per shard.
        aux_columns: columns allowed to cross a shard boundary as extras.
        stable_keys: declared stable row ids making a window ORDER BY total.
        dialect: SQL dialect used to parse and generate.

    Returns:
        A ShardPlan. If ``plan.ok`` is False, ``plan.unsupported`` names each
        expression that the boundary contract cannot cover.
    """
    tree = parse_one(query, read=dialect) if isinstance(query, str) else query.copy()
    shards = [
        coverage
        if isinstance(coverage, ShardCoverage)
        else ShardCoverage(name=f"shard_{i}", predicate=coverage)
        for i, coverage in enumerate(coverages)
    ]
    plan = ShardPlan(shards=shards, shard_sql=[], handoffs=[], aux_columns=[], unsupported=[])

    def fail(message: str) -> None:
        plan.unsupported.append(message)

    if not isinstance(tree, exp.Select):
        fail(f"only a single SELECT can be sharded: {tree.sql(dialect=dialect)}")
        return plan

    from_ = tree.args.get("from_")
    if not from_ or not isinstance(from_.this, exp.Table):
        fail(
            f"FROM must be a single base table: {from_.sql(dialect=dialect) if from_ else tree.sql(dialect=dialect)}"
        )
    for key in ("joins", "group", "having", "qualify", "distinct", "limit", "offset", "windows"):
        node = tree.args.get(key)
        if node:
            sql = node.sql(dialect=dialect) if isinstance(node, exp.Expression) else key
            fail(f"{key.upper()} is not coverable by the shard boundary: {sql}")
    if plan.unsupported:
        return plan

    projected: dict[str, str] = {}
    window_projections: list[tuple[str, exp.Window]] = []
    for projection in tree.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(target, exp.Star) or (
            isinstance(target, exp.Column) and isinstance(target.this, exp.Star)
        ):
            fail(
                f"SELECT * cannot be planned, project columns explicitly: {projection.sql(dialect=dialect)}"
            )
        elif isinstance(target, exp.Window):
            if not projection.alias:
                fail(f"window projection needs an alias: {projection.sql(dialect=dialect)}")
            else:
                window_projections.append((projection.alias, target))
        elif target.find(exp.Window) is not None:
            fail(f"window must be a standalone projection: {projection.sql(dialect=dialect)}")
        elif isinstance(target, exp.Column):
            projected[target.name] = projection.alias or target.name

    projection_windows = {id(window) for _, window in window_projections}
    for window in tree.find_all(exp.Window):
        if id(window) not in projection_windows:
            fail(
                f"window outside the SELECT projections cannot be sharded: {window.sql(dialect=dialect)}"
            )

    order = tree.args.get("order")
    if order:
        for ordered in order.expressions:
            if not isinstance(ordered.this, exp.Column):
                fail(f"ORDER BY must reference output columns: {ordered.sql(dialect=dialect)}")
            else:
                plan.order_by.append((ordered.this.name, bool(ordered.args.get("desc"))))
    if plan.unsupported:
        return plan

    allowed_aux = set(aux_columns)
    stable = set(stable_keys)
    carry_columns: dict[str, exp.Column] = {}
    aux_projections: dict[str, str] = {}  # column name -> aux output key
    avg_windows: list[tuple[exp.Window, str, str]] = []

    def carry_key(name: str, column: exp.Column, window_sql: str) -> str | None:
        if name in projected:
            return projected[name]
        if name in allowed_aux:
            if name not in aux_projections:
                aux_projections[name] = f"__shard_aux_{name}"
                carry_columns.setdefault(name, column)
            return aux_projections[name]
        fail(
            f"{window_sql} needs column {name} at the shard boundary; "
            "project it or allow it in aux_columns"
        )
        return None

    for alias, window in window_projections:
        handoff = _analyze_window(window, alias, stable, fail, dialect)
        if handoff is None:
            continue
        carry: dict[str, str] = {}
        needed = list(_window_columns(window))
        for name, column in needed:
            output_key = carry_key(name, column, handoff.window)
            if output_key is not None:
                carry[name] = output_key
        handoff.carry = carry
        if handoff.func == "avg":
            sum_key, count_key = f"__shard_aux_sum_{alias}", f"__shard_aux_count_{alias}"
            avg_windows.append((window, sum_key, count_key))
            handoff.sum_key, handoff.count_key = sum_key, count_key
        plan.handoffs.append(handoff)

    if plan.unsupported:
        return plan

    for coverage in shards:
        shard = tree.copy()
        for name, key in aux_projections.items():
            shard.select(exp.alias_(carry_columns[name].copy(), key), copy=False)
        for window, sum_key, count_key in avg_windows:
            arg = window.this.this
            for func, key in ((exp.Sum, sum_key), (exp.Count, count_key)):
                aux_window = window.copy()
                aux_window.set("this", func(this=arg.copy()))
                shard.select(exp.alias_(aux_window, key), copy=False)
        predicate = t.cast(exp.Expression, exp.maybe_parse(coverage.predicate, dialect=dialect))
        where = shard.args.get("where")
        shard.set(
            "where",
            exp.Where(this=exp.and_(where.this.copy(), predicate) if where else predicate),
        )
        plan.shard_sql.append(shard.sql(dialect=dialect))

    plan.aux_columns = list(aux_projections.values()) + [
        key for _, sum_key, count_key in avg_windows for key in (sum_key, count_key)
    ]
    return plan


def _window_columns(window: exp.Window) -> t.Iterator[tuple[str, exp.Column]]:
    for column in window.find_all(exp.Column):
        yield column.name, column


def _analyze_window(
    window: exp.Window,
    alias: str,
    stable: set[str],
    fail: t.Callable[[str], None],
    dialect: str | None,
) -> WindowHandoff | None:
    window_sql = window.sql(dialect=dialect)
    func = window.this

    if window.args.get("alias"):
        fail(f"named window references cannot be sharded: {window_sql}")
        return None

    partition = window.args.get("partition_by") or []
    order = window.args.get("order")
    ordered = order.expressions if order else []
    spec = window.args.get("spec")

    for expression in (*partition, *(o.this for o in ordered)):
        if not isinstance(expression, exp.Column):
            fail(f"window PARTITION BY / ORDER BY must be plain columns: {window_sql}")
            return None

    partition_by = [e.name for e in partition]
    order_by = [o.this.name for o in ordered]
    order_desc = [bool(o.args.get("desc")) for o in ordered]
    handoff = WindowHandoff(
        alias=alias,
        kind="",
        window=window_sql,
        partition_by=partition_by,
        order_by=order_by,
        order_desc=order_desc,
        carry={},
    )

    if isinstance(func, (exp.Rank, exp.DenseRank, exp.RowNumber)):
        if not ordered:
            fail(f"ranking windows need an ORDER BY: {window_sql}")
            return None
        if isinstance(func, exp.Rank):
            handoff.kind = "rank"
        elif isinstance(func, exp.DenseRank):
            handoff.kind = "dense_rank"
        else:
            handoff.kind = "row_number"
            if not stable.intersection(order_by):
                fail(
                    f"ROW_NUMBER needs a deterministic ORDER BY; "
                    f"declare a stable row id in stable_keys: {window_sql}"
                )
                return None
        handoff.row_counts = True
        handoff.peer_counts = not isinstance(func, exp.RowNumber)
        return handoff

    func_name = next(
        (name for name, klass in _RANK_FRAME_FUNCS.items() if isinstance(func, klass)), None
    )
    if func_name is None:
        fail(f"window function cannot be sharded: {window_sql}")
        return None
    if spec is None:
        fail(f"only explicit ROWS frames are coverable by the shard boundary: {window_sql}")
        return None
    if spec.args.get("kind") != "ROWS":
        fail(f"only ROWS frames are coverable by the shard boundary: {window_sql}")
        return None
    if spec.args.get("exclude"):
        fail(f"frame EXCLUDE is not coverable by the shard boundary: {window_sql}")
        return None

    start, start_side = spec.args.get("start"), spec.args.get("start_side")
    end, end_side = spec.args.get("end"), spec.args.get("end_side")
    if start_side != "PRECEDING" or end_side is not None or end not in (None, "CURRENT ROW"):
        fail(f"frames reaching past CURRENT ROW cannot be sharded: {window_sql}")
        return None
    if start == "UNBOUNDED":
        handoff.unbounded = True
    elif isinstance(start, exp.Literal) and start.is_number:
        handoff.frame_preceding = int(start.to_py())
    else:
        fail(f"frame start is not coverable by the shard boundary: {window_sql}")
        return None

    if not stable.intersection(order_by):
        fail(
            f"ROWS frame needs a deterministic ORDER BY; "
            f"declare a stable row id in stable_keys: {window_sql}"
        )
        return None

    arg = func.this
    if isinstance(arg, exp.Star):
        if func_name != "count":
            fail(f"only COUNT(*) may use * as window argument: {window_sql}")
            return None
    elif not isinstance(arg, exp.Column):
        fail(f"window argument must be a plain column: {window_sql}")
        return None
    else:
        handoff.arg = arg.name

    handoff.kind = "frame"
    handoff.func = func_name
    return handoff


def merge_shard_results(
    plan: ShardPlan, shard_rows: t.Sequence[t.Sequence[dict[str, t.Any]]]
) -> list[dict[str, t.Any]]:
    """
    Merge per-shard result rows back into the original query's result.

    ``shard_rows`` are the rows produced by each ``plan.shard_sql`` query, in
    shard order. Rows are merged according to the plan's handoff contract and
    auxiliary columns are stripped from the output.
    """
    if plan.unsupported:
        raise ShardingError("cannot merge: " + "; ".join(plan.unsupported))

    shards = [[dict(row) for row in shard] for shard in shard_rows]
    for handoff in plan.handoffs:
        _merge_handoff(handoff, shards)

    rows = [row for shard in shards for row in shard]
    for row in rows:
        for key in plan.aux_columns:
            row.pop(key, None)
    _sort_rows(rows, plan.order_by)
    return rows


def _merge_handoff(handoff: WindowHandoff, shards: list[list[dict[str, t.Any]]]) -> None:
    state: dict[tuple[t.Any, ...], dict[str, t.Any]] = {}
    order_keys = [(handoff.carry[c], desc) for c, desc in zip(handoff.order_by, handoff.order_desc)]

    for shard in shards:
        segments: dict[tuple[t.Any, ...], list[dict[str, t.Any]]] = {}
        for row in shard:
            pkey = tuple(row[handoff.carry[c]] for c in handoff.partition_by)
            segments.setdefault(pkey, []).append(row)
        for pkey, segment in segments.items():
            _sort_rows(segment, order_keys)
            segment_state = state.setdefault(pkey, _initial_state(handoff))
            _adjust_segment(handoff, segment_state, segment)


def _initial_state(handoff: WindowHandoff) -> dict[str, t.Any]:
    return {
        "rows": 0,
        "boundary": None,
        "peers": 0,
        "distinct": 0,
        "tail": [],
        "sum": 0,
        "count": 0,
        "min": None,
        "max": None,
    }


def _key_of(handoff: WindowHandoff, row: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return tuple(row[handoff.carry[c]] for c in handoff.order_by)


def _adjust_segment(
    handoff: WindowHandoff, state: dict[str, t.Any], segment: list[dict[str, t.Any]]
) -> None:
    kind = handoff.kind
    if kind in ("rank", "dense_rank", "row_number"):
        _adjust_ranking(handoff, state, segment)
    elif handoff.unbounded:
        _adjust_unbounded(handoff, state, segment)
    else:
        _adjust_frame(handoff, state, segment)


def _adjust_ranking(
    handoff: WindowHandoff, state: dict[str, t.Any], segment: list[dict[str, t.Any]]
) -> None:
    kind = handoff.kind
    boundary = state["boundary"]
    boundary_in_segment = boundary is not None and any(
        _key_of(handoff, row) == boundary for row in segment
    )
    offset = state["distinct"] - (1 if boundary_in_segment else 0)
    for row in segment:
        key = _key_of(handoff, row)
        if kind == "row_number":
            row[handoff.alias] += state["rows"]
        elif boundary is not None and key == boundary:
            row[handoff.alias] = (
                state["rows"] - state["peers"] + 1 if kind == "rank" else state["distinct"]
            )
        else:
            row[handoff.alias] += state["rows"] if kind == "rank" else offset

    new_boundary = _key_of(handoff, segment[-1])
    if kind == "dense_rank":
        keys = []
        for row in segment:
            key = _key_of(handoff, row)
            if key not in keys:
                keys.append(key)
        state["distinct"] += len(keys) - (1 if boundary is not None and boundary in keys else 0)
    else:
        peers = sum(1 for row in segment if _key_of(handoff, row) == new_boundary)
        state["peers"] = (state["peers"] if new_boundary == boundary else 0) + peers
    state["rows"] += len(segment)
    state["boundary"] = new_boundary


def _adjust_frame(
    handoff: WindowHandoff, state: dict[str, t.Any], segment: list[dict[str, t.Any]]
) -> None:
    k = t.cast(int, handoff.frame_preceding)
    arg_key = handoff.carry.get(handoff.arg) if handoff.arg else None
    for i, row in enumerate(segment):
        missing = min(k - i, state["rows"]) if k > i else 0
        if missing:
            _combine_frame(handoff, row, state["tail"][len(state["tail"]) - missing :])
    entries = [row[arg_key] for row in segment] if arg_key else [None] * len(segment)
    state["tail"] = (state["tail"] + entries)[-k:]
    state["rows"] += len(segment)


def _adjust_unbounded(
    handoff: WindowHandoff, state: dict[str, t.Any], segment: list[dict[str, t.Any]]
) -> None:
    arg_key = handoff.carry.get(handoff.arg) if handoff.arg else None
    func = handoff.func
    for row in segment:
        value = row[handoff.alias]
        if func == "sum":
            row[handoff.alias] = (
                (value or 0) + state["sum"] if value is not None or state["count"] else None
            )
        elif func == "count":
            row[handoff.alias] += state["count"] if arg_key else state["rows"]
        elif func == "min":
            row[handoff.alias] = _min_nullable(value, state["min"])
        elif func == "max":
            row[handoff.alias] = _max_nullable(value, state["max"])
        elif func == "avg":
            sum_key = t.cast(str, handoff.sum_key)
            count_key = t.cast(str, handoff.count_key)
            total_count = state["count"] + (row[count_key] or 0)
            row[handoff.alias] = (
                (state["sum"] + (row[sum_key] or 0)) / total_count if total_count else None
            )

    values = [row[arg_key] for row in segment] if arg_key else []
    non_null = [v for v in values if v is not None]
    state["sum"] += sum(non_null)
    state["count"] += len(non_null)
    state["rows"] += len(segment)
    if non_null:
        state["min"] = _min_nullable(state["min"], min(non_null))
        state["max"] = _max_nullable(state["max"], max(non_null))


def _combine_frame(handoff: WindowHandoff, row: dict[str, t.Any], values: list[t.Any]) -> None:
    func = handoff.func
    non_null = [v for v in values if v is not None]
    value = row[handoff.alias]
    if func == "sum":
        row[handoff.alias] = (value or 0) + sum(non_null) if value is not None or non_null else None
    elif func == "count":
        row[handoff.alias] += len(non_null) if handoff.arg else len(values)
    elif func == "min" and non_null:
        row[handoff.alias] = _min_nullable(value, min(non_null))
    elif func == "max" and non_null:
        row[handoff.alias] = _max_nullable(value, max(non_null))
    elif func == "avg":
        sum_key = t.cast(str, handoff.sum_key)
        count_key = t.cast(str, handoff.count_key)
        total_count = (row[count_key] or 0) + len(non_null)
        row[handoff.alias] = (
            ((row[sum_key] or 0) + sum(non_null)) / total_count if total_count else None
        )


def _min_nullable(a: t.Any, b: t.Any) -> t.Any:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_nullable(a: t.Any, b: t.Any) -> t.Any:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _sort_rows(rows: list[dict[str, t.Any]], keys: list[tuple[str, bool]]) -> None:
    for key, desc in reversed(keys):
        rows.sort(key=_sort_key(key), reverse=desc)


def _sort_key(column: str) -> t.Callable[[dict[str, t.Any]], tuple[bool, t.Any]]:
    return lambda row: (row[column] is None, row[column])
