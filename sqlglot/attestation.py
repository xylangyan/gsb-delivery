"""Row-level attestation of a query output under partially received source rows.

The column lineage graph in :mod:`sqlglot.lineage` explains where the values of
an output column come from, but it cannot answer whether a *missing* output row
is genuinely excluded by the query or merely waiting on a warehouse receipt.
This module closes that gap.

The caller supplies:

* a query (a ``SELECT`` optionally combined with finite set operations),
* the output row to attest,
* the stable ids of the source rows already received,
* the finite set of source rows that may still arrive, and
* receipt policies that constrain which pending rows can arrive together
  (mutually exclusive arrivals and group arrivals).

Every legal completion of the pending rows is enumerated, the query is executed
against each completion with the multiset-aware :mod:`sqlglot.executor` (so
``NULL`` is never treated as unequal and rows are never merged ahead of
execution), and the minimum / maximum number of occurrences of the target row
is reported. When absence can be proved, minimal sets of received rows that
settle the question in every legal continuation are returned as witnesses,
each pointing at the subquery or set operation branch that blocks the row.
"""

from __future__ import annotations

import itertools
import typing as t
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from sqlglot import exp, maybe_parse
from sqlglot.dialects.dialect import DialectType
from sqlglot.errors import SqlglotError
from sqlglot.executor import execute
from sqlglot.executor.table import Table

RowId = tuple[str, t.Any]
RowSet = frozenset[RowId]


@dataclass(frozen=True)
class SourceRow:
    """A single readable source row, addressable by its stable ``row_id``."""

    table: str
    row_id: t.Any
    data: dict[str, t.Any]


@dataclass(frozen=True)
class ReceiptPolicy:
    """A constraint on how pending rows may be received together.

    Args:
        name: human-readable policy name used in scenario descriptions.
        row_ids: the ``(table, row_id)`` pairs governed by the policy.
        kind: ``"exclusive"`` allows at most one governed row to arrive,
            ``"group"`` requires arrivals within ``[min_count, max_count]``.
        min_count: minimum number of governed rows in a legal completion.
        max_count: maximum number; ``None`` means every governed row.
    """

    name: str
    row_ids: RowSet
    kind: str
    min_count: int = 0
    max_count: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("exclusive", "group"):
            raise SqlglotError(f"Unknown receipt policy kind: {self.kind}")
        if self.kind == "exclusive":
            if self.min_count or self.max_count not in (None, 1):
                raise SqlglotError("Exclusive policies allow at most one arriving row")
            object.__setattr__(self, "max_count", 1)
        if self.min_count < 0:
            raise SqlglotError("min_count must be non-negative")
        if self.max_count is not None and self.max_count < self.min_count:
            raise SqlglotError("max_count must be >= min_count")

    def max_arrivals(self) -> int:
        return len(self.row_ids) if self.max_count is None else self.max_count


@dataclass(frozen=True)
class RowScenario:
    """One legal way the pending rows may be received."""

    name: str
    present: RowSet
    note: str


@dataclass(frozen=True)
class BranchTrace:
    """The rows produced by one subquery or set operation branch."""

    kind: str
    sql: str
    output_rows: tuple[dict[str, t.Any], ...]


@dataclass(frozen=True)
class Witness:
    """A minimal set of received rows proving the target row stays absent.

    Rows omitted from ``rows`` are treated as still able to arrive: the witness
    only holds if every legal completion containing these rows keeps the target
    row out of the output.
    """

    scenario: str
    rows: tuple[SourceRow, ...]
    reason: str
    blocking_branches: tuple[str, ...]


@dataclass(frozen=True)
class BoundCase:
    """The source-row combination that realizes the min or max count."""

    bound: str
    count: int
    scenario: str
    present: RowSet


@dataclass(frozen=True)
class AttestationResult:
    target: dict[str, t.Any]
    min_count: int
    max_count: int
    fixed: RowSet
    scenarios: tuple[RowScenario, ...]
    witnesses: tuple[Witness, ...]
    bounds: tuple[BoundCase, ...]
    table_rows: dict[str, tuple[dict[str, t.Any], ...]]
    traces: tuple[BranchTrace, ...]

    @property
    def is_absent(self) -> bool:
        """True only when the target row is excluded under every legal completion."""
        return self.max_count == 0


def attest(
    sql: str | exp.Expr,
    target: Mapping[str, t.Any],
    fixed_rows: Mapping[str, Mapping[str, t.Any] | Sequence[t.Any]],
    tables: Mapping[str, Mapping[t.Any, Mapping[str, t.Any]]],
    policies: Sequence[ReceiptPolicy] = (),
    dialect: DialectType = None,
) -> AttestationResult:
    """Attest the occurrence range of ``target`` across all legal completions.

    Args:
        sql: the query, limited to ``SELECT``, its filters and finite set
            operations (``UNION [ALL]``, ``INTERSECT [ALL]``, ``EXCEPT [ALL]``).
        target: the output row to count, keyed by output column name.
        fixed_rows: already received row ids per table; these are present in
            every completion.
        tables: every candidate source row keyed by its stable id per table,
            including both received and still-pending rows. Rows are kept as
            individual multiset elements and never merged ahead of execution.
        policies: receipt policies constraining the pending rows.
        dialect: dialect of the input SQL.

    Returns:
        An :class:`AttestationResult` with the count range, recomputable
        bound cases, absence witnesses and branch traces.
    """
    return RowAttester(sql, target, fixed_rows, tables, policies, dialect).run()


class RowAttester:
    def __init__(
        self,
        sql: str | exp.Expr,
        target: Mapping[str, t.Any],
        fixed_rows: Mapping[str, Mapping[str, t.Any] | Sequence[t.Any]],
        tables: Mapping[str, Mapping[t.Any, Mapping[str, t.Any]]],
        policies: Sequence[ReceiptPolicy],
        dialect: DialectType,
    ) -> None:
        self.expression = maybe_parse(sql, dialect=dialect)
        if not isinstance(self.expression, exp.Query):
            raise SqlglotError("Attestation requires a SELECT query")
        if self.expression.args.get("limit") is not None or self.expression.args.get("offset"):
            raise SqlglotError("LIMIT / OFFSET queries cannot be attested row-wise")

        self.dialect = dialect
        self.target = dict(target)
        self.policies = list(policies)

        self.rows: dict[RowId, SourceRow] = {}
        for table_name, by_id in tables.items():
            if not by_id:
                raise SqlglotError(f"Table {table_name} has no candidate rows to infer its schema")
            for row_id, data in by_id.items():
                key = (table_name, row_id)
                if key in self.rows:
                    raise SqlglotError(f"Duplicate row id {row_id!r} in table {table_name}")
                self.rows[key] = SourceRow(table_name, row_id, dict(data))

        self.fixed: RowSet = frozenset(
            (table_name, row_id)
            for table_name, row_ids in fixed_rows.items()
            for row_id in self._iter_ids(row_ids)
        )
        self.all_ids: RowSet = frozenset(self.rows)
        unknown_fixed = self.fixed - self.all_ids
        if unknown_fixed:
            raise SqlglotError(
                f"Received rows absent from the candidate tables: {sorted(unknown_fixed, key=str)}"
            )

        self.pending: RowSet = self.all_ids - self.fixed
        self.schema = self._build_schema()
        self._count_cache: dict[RowSet, int] = {}
        self._validate_policies()

        self.arrivals: tuple[RowSet, ...] = tuple(self._enumerate_arrivals())
        if not self.arrivals:
            raise SqlglotError("The receipt policies admit no legal completion")
        self.scenarios = tuple(
            RowScenario(f"completion_{index}", self.fixed | arrival, self._arrival_note(arrival))
            for index, arrival in enumerate(self.arrivals)
        )

    @staticmethod
    def _iter_ids(row_ids: Mapping[str, t.Any] | Sequence[t.Any]) -> Iterator[t.Any]:
        ids: t.Iterable[t.Any] = row_ids.keys() if isinstance(row_ids, Mapping) else row_ids
        yield from ids

    def _build_schema(self) -> dict[str, dict[str, str]]:
        schema: dict[str, dict[str, str]] = {}
        for (table_name, _), source_row in self.rows.items():
            columns = schema.setdefault(table_name, {})
            for column, value in source_row.data.items():
                if value is not None and column not in columns:
                    columns[column] = type(value).__name__.upper()
        for table_name, columns in schema.items():
            for source_row in (row for row in self.rows.values() if row.table == table_name):
                for column in source_row.data:
                    columns.setdefault(column, "VARCHAR")
        return schema

    def _validate_policies(self) -> None:
        governed: set[tuple[str, t.Any]] = set()
        for policy in self.policies:
            unknown = policy.row_ids - self.all_ids
            if unknown:
                raise SqlglotError(
                    f"Policy {policy.name!r} references unknown rows: {sorted(unknown, key=str)}"
                )
            if not policy.row_ids <= self.pending:
                raise SqlglotError(f"Policy {policy.name!r} may only constrain pending rows")
            overlap = governed & policy.row_ids
            if overlap:
                raise SqlglotError(
                    f"Policy {policy.name!r} overlaps another policy on {sorted(overlap, key=str)}"
                )
            governed.update(policy.row_ids)

    def _enumerate_arrivals(self) -> Iterator[RowSet]:
        governed: set[tuple[str, t.Any]] = set()
        option_sets: list[tuple[ReceiptPolicy, list[RowSet]]] = []

        for policy in self.policies:
            members = sorted(policy.row_ids, key=lambda key: str(key))
            governed.update(members)
            choices = [
                frozenset(combination)
                for size in range(policy.min_count, policy.max_arrivals() + 1)
                for combination in itertools.combinations(members, size)
            ]
            option_sets.append((policy, choices))

        free_rows = sorted(self.pending - governed, key=lambda key: str(key))
        free_choices = [
            frozenset(combination)
            for size in range(len(free_rows) + 1)
            for combination in itertools.combinations(free_rows, size)
        ]

        seen: set[RowSet] = set()
        for free_choice in free_choices:
            products = (
                itertools.product(*(options for _, options in option_sets)) if option_sets else [()]
            )
            for product in products:
                arrival = free_choice | frozenset(itertools.chain.from_iterable(product))
                if arrival not in seen:
                    seen.add(arrival)
                    yield arrival

    def _arrival_note(self, arrival: RowSet) -> str:
        if not self.policies:
            return "no receipt policies apply"
        notes = []
        for policy in self.policies:
            chosen = sorted(arrival & policy.row_ids, key=str)
            notes.append(f"{policy.name}: {chosen or 'none arrives'}")
        return "; ".join(notes)

    def _materialize(self, present: RowSet) -> dict[str, list[dict[str, t.Any]]]:
        materialized: dict[str, list[dict[str, t.Any]]] = {
            table_name: [] for table_name in self.schema
        }
        for table_name, row_id in sorted(present, key=lambda key: (key[0], str(key[1]))):
            materialized[table_name].append(dict(self.rows[(table_name, row_id)].data))
        return materialized

    def _run_query(self, present: RowSet, sql: str | exp.Expr | None = None) -> Table:
        return execute(
            sql or self.expression,
            schema=self.schema,
            tables=self._materialize(present),
            dialect=self.dialect,
        )

    def _target_key(self, columns: tuple[str, ...]) -> tuple[t.Any, ...]:
        missing = [column for column in columns if column not in self.target]
        if missing:
            raise SqlglotError(f"Target row is missing output columns: {missing}")
        return tuple(self.target[column] for column in columns)

    def _count(self, present: RowSet) -> int:
        if present not in self._count_cache:
            result = self._run_query(present)
            self._count_cache[present] = Counter(result.rows).get(
                self._target_key(tuple(result.columns)), 0
            )
        return self._count_cache[present]

    def run(self) -> AttestationResult:
        evaluations = [(scenario, self._count(scenario.present)) for scenario in self.scenarios]

        min_count = min(count for _, count in evaluations)
        max_count = max(count for _, count in evaluations)
        bounds = self._bound_cases(evaluations, min_count, max_count)

        maximal = max(self.scenarios, key=lambda scenario: len(scenario.present))
        witnesses = (
            self._dedupe_witnesses(self._find_witnesses(maximal.present)) if max_count == 0 else ()
        )

        traces = tuple(self._trace_branches(maximal.present))
        table_rows = {
            table_name: tuple(row.data for row in self.rows.values() if row.table == table_name)
            for table_name in self.schema
        }

        return AttestationResult(
            target=self.target,
            min_count=min_count,
            max_count=max_count,
            fixed=self.fixed,
            scenarios=self.scenarios,
            witnesses=witnesses,
            bounds=bounds,
            table_rows=table_rows,
            traces=traces,
        )

    def _bound_cases(
        self,
        evaluations: list[tuple[RowScenario, int]],
        min_count: int,
        max_count: int,
    ) -> tuple[BoundCase, ...]:
        cases = []
        for bound, wanted in (("min", min_count), ("max", max_count)):
            scenario = next(scenario for scenario, count in evaluations if count == wanted)
            cases.append(BoundCase(bound, wanted, scenario.name, scenario.present))
        return tuple(cases)

    def _find_witnesses(self, diagnostic_world: RowSet) -> Iterator[Witness]:
        fixed_rows = sorted(self.fixed, key=lambda key: str(key))
        found: list[RowSet] = []

        for size in range(1, len(fixed_rows) + 1):
            for evidence_tuple in itertools.combinations(fixed_rows, size):
                evidence = frozenset(evidence_tuple)
                if any(known <= evidence for known in found):
                    continue
                worlds = (evidence | arrival for arrival in self.arrivals)
                if not all(self._count(world) == 0 for world in worlds):
                    continue
                if not self._blocking_branches(evidence)[1]:
                    continue
                found.append(evidence)
                yield self._build_witness(evidence, diagnostic_world)

        if not found and all(self._count(arrival) == 0 for arrival in self.arrivals):
            empty: RowSet = frozenset()
            yield self._build_witness(empty, diagnostic_world)

    def _build_witness(
        self,
        evidence: RowSet,
        diagnostic_world: RowSet,
    ) -> Witness:
        rows = tuple(
            self.rows[key] for key in sorted(evidence, key=lambda key: (key[0], str(key[1])))
        )
        blocking, reasons = self._blocking_branches(evidence)
        if not reasons:
            blocking, reasons = self._blocking_branches(diagnostic_world)
        reason = (
            "; ".join(reasons)
            if reasons
            else "no candidate source row produces the target row under any legal completion"
        )
        return Witness("absence_proof", rows, reason, tuple(blocking))

    def _blocking_branches(self, present: RowSet) -> tuple[list[str], list[str]]:
        blocking: list[str] = []
        reasons: list[str] = []
        target_key: tuple[t.Any, ...] | None = None

        for edge in self._setop_edges(self.expression):
            op_name = type(edge).__name__.upper()
            suffix = "" if edge.args.get("distinct") else " ALL"
            for side_name, operand in (("left", edge.left), ("right", edge.right)):
                branch = operand.unnest()
                branch_table = self._run_query(present, branch)
                target_key = self._target_key(tuple(branch_table.columns))
                counts = Counter(branch_table.rows)
                label = f"{op_name}{suffix} {side_name}"

                if isinstance(edge, exp.Except) and side_name == "right" and counts.get(target_key):
                    blocking.append(branch.sql(dialect=self.dialect))
                    reasons.append(
                        f"{label} branch contains {counts[target_key]} target copy "
                        f"removed by {op_name}{suffix}"
                    )
                elif isinstance(edge, exp.Intersect) and not counts.get(target_key):
                    blocking.append(branch.sql(dialect=self.dialect))
                    reasons.append(f"{label} branch produces no copy of the target row")

        for leaf in self._setop_leaves(self.expression):
            leaf_table = self._run_query(present, leaf)
            if target_key is None:
                target_key = self._target_key(tuple(leaf_table.columns))
            where = leaf.args.get("where")
            leaf_reasons_start = len(reasons)

            for in_node in leaf.find_all(exp.In):
                subquery = in_node.args.get("query")
                if subquery is None or self._is_correlated(subquery.unnest()):
                    continue
                subquery_table = self._run_query(present, subquery.unnest())
                if isinstance(in_node.parent, exp.Not) and any(
                    None in row for row in subquery_table.rows
                ):
                    blocking.append(subquery.unnest().sql(dialect=self.dialect))
                    reasons.append(
                        "NOT IN subquery contains a NULL part number, so the comparison is never true"
                    )

            if (
                where
                and not Counter(leaf_table.rows).get(target_key)
                and len(reasons) == leaf_reasons_start
                and any(
                    key[0] == source.alias_or_name
                    for source in leaf.find_all(exp.Table)
                    for key in present
                )
            ):
                blocking.append(leaf.sql(dialect=self.dialect))
                reasons.append(
                    f"WHERE clause {where.this.sql(dialect=self.dialect)} filters out every matching source row"
                )
        return blocking, reasons

    def _is_correlated(self, subquery: exp.Query) -> bool:
        root = subquery
        local_tables = {
            node.alias_or_name
            for node in root.walk(
                prune=lambda node: isinstance(node, exp.Subquery) and node is not root
            )
            if isinstance(node, exp.Table)
        }
        local_columns = {
            column for table_name in local_tables for column in self.schema.get(table_name, {})
        }
        for node in root.walk(
            prune=lambda node: isinstance(node, exp.Subquery) and node is not root
        ):
            if not isinstance(node, exp.Column):
                continue
            if node.table and node.table not in local_tables:
                return True
            if not node.table and node.name not in local_columns:
                return True
        return False

    def _trace_branches(self, present: RowSet) -> Iterator[BranchTrace]:
        for edge in self._setop_edges(self.expression):
            op_name = type(edge).__name__.upper()
            suffix = "" if edge.args.get("distinct") else " ALL"
            for side_name, operand in (("left", edge.left), ("right", edge.right)):
                branch = operand.unnest()
                branch_table = self._run_query(present, branch)
                yield BranchTrace(
                    kind=f"{op_name}{suffix} {side_name}",
                    sql=branch.sql(dialect=self.dialect),
                    output_rows=tuple(
                        dict(zip(branch_table.columns, row)) for row in branch_table.rows
                    ),
                )

        for leaf in self._setop_leaves(self.expression):
            for in_node in leaf.find_all(exp.In):
                subquery = in_node.args.get("query")
                if subquery is None or self._is_correlated(subquery.unnest()):
                    continue
                subquery_table = self._run_query(present, subquery.unnest())
                kind = "not_in_subquery" if isinstance(in_node.parent, exp.Not) else "in_subquery"
                yield BranchTrace(
                    kind=kind,
                    sql=subquery.unnest().sql(dialect=self.dialect),
                    output_rows=tuple(
                        dict(zip(subquery_table.columns, row)) for row in subquery_table.rows
                    ),
                )

    def _setop_edges(self, node: exp.Expr) -> Iterator[exp.SetOperation]:
        node = node.unnest()
        if isinstance(node, exp.SetOperation):
            yield node
            yield from self._setop_edges(node.left)
            yield from self._setop_edges(node.right)

    def _setop_leaves(self, node: exp.Expr) -> Iterator[exp.Select]:
        node = node.unnest()
        if isinstance(node, exp.SetOperation):
            yield from self._setop_leaves(node.left)
            yield from self._setop_leaves(node.right)
        else:
            yield t.cast(exp.Select, node)

    @staticmethod
    def _dedupe_witnesses(witnesses: Iterator[Witness]) -> tuple[Witness, ...]:
        unique: dict[RowSet, Witness] = {}
        for witness in witnesses:
            key = frozenset((row.table, row.row_id) for row in witness.rows)
            unique.setdefault(key, witness)
        return tuple(
            sorted(
                unique.values(), key=lambda witness: (len(witness.rows), witness.blocking_branches)
            )
        )
