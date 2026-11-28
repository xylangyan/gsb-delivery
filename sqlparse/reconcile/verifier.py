"""SQLite-backed verification of reconciliation verdicts.

The verifier is a local model of the receiving end.  It materialises the
external table versions (each allowed alternative is a separate finite
data set), replays a work order in an in-memory SQLite database and
compares delivered report pages against recomputed pages.

A real database service is never contacted: SQLite is part of the
standard library and runs entirely in memory, while the byte and fee
accounting stays in Python.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from sqlparse.reconcile.analyser import LineageAnalyser
from sqlparse.reconcile.model import (
    AllowedSeal,
    ColumnRef,
    ExternalVersion,
    Failure,
    ReadRef,
    ReportVerdict,
    RowMeta,
)

_BASE = 'base'
_WHITESPACE = re.compile(r'\s+')


def _page_key(sql_text: str) -> str:
    """Stable identity for a delivered page across two near-identical orders."""
    return _WHITESPACE.sub(' ', sql_text).strip().rstrip(';').strip().lower()

@dataclass
class Alternative:
    """One coherent set of finite-value substitutions.

    ``selection`` maps an external table name to the variant label used
    for *every* read of that table, so one original row always gets one
    interpretation.
    """

    name: str
    selection: dict[str, str]
    bytes_total: int = 0
    fee_total: float = 0.0


@dataclass
class ReconciliationResult:
    reusable: list[ReportVerdict] = field(default_factory=list)
    redo: list[ReportVerdict] = field(default_factory=list)
    chosen: Alternative | None = None
    reads: list[ReadRef] = field(default_factory=list)
    total_bytes: int = 0
    total_fees: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)


class Reconciler:
    """Replay work orders and decide which reports survive each shift."""

    def __init__(
        self,
        externals: dict[str, ExternalVersion],
        allowed_seals: list[AllowedSeal] | None = None,
    ) -> None:
        self._externals = externals
        self._allowed_seals = {
            (seal.name, seal.generation) for seal in allowed_seals or []
        }

    # -- analysis ----------------------------------------------------------

    def _externals_view(self, previous: bool) -> dict[str, ExternalVersion]:
        if not previous:
            return self._externals
        view: dict[str, ExternalVersion] = {}
        for table, version in self._externals.items():
            if version.previous_instance is None:
                view[table] = version
                continue
            view[table] = ExternalVersion(
                table=table,
                instance=version.previous_instance,
                columns=list(version.columns),
                bytes=version.bytes,
                fee=version.fee,
                rows=list(
                    version.previous_rows
                    if version.previous_rows is not None
                    else version.rows
                ),
                row_meta=list(version.row_meta),
            )
        return view

    def analyse(self, source: str, previous: bool = False) -> LineageAnalyser:
        analyser = LineageAnalyser(
            source, self._externals_view(previous), _BASE
        )
        analyser.run()
        self._mark_sealed(analyser)
        return analyser

    def _reports_by_key(self, analyser: LineageAnalyser) -> dict:
        keys = self._select_keys(analyser)
        return {
            keys[report.statement_index]: report
            for report in analyser.reports
        }

    def _mark_sealed(self, analyser: LineageAnalyser) -> None:
        for record in analyser.generations.values():
            if (record.name, record.generation) in self._allowed_seals:
                record.sealed = True
        for report in analyser.reports:
            sealed_refs = sorted(
                generation_id
                for generation_id in report.temp_refs
                if analyser.generations[generation_id].sealed
            )
            report.sealed_snapshot = sealed_refs[0] if sealed_refs else None

    # -- alternatives ------------------------------------------------------

    def alternatives(self, analyser: LineageAnalyser) -> list[Alternative]:
        tables = sorted({read.table for read in analyser._reads.values()})
        labels = {
            table: [_BASE] + sorted(self._externals[table].variants)
            for table in tables
        }
        alternatives: list[Alternative] = []
        for selection in self._cartesian(labels):
            name = ','.join(
                f'{table}={label}'
                for table, label in sorted(selection.items())
            ) or _BASE
            alternatives.append(Alternative(name=name, selection=selection))
        return alternatives

    @staticmethod
    def _cartesian(labels_by_table: dict[str, list[str]]):
        combos: list[dict[str, str]] = [{}]
        for table, options in labels_by_table.items():
            combos = [
                {**partial, table: label}
                for partial in combos
                for label in options
            ]
        return combos

    # -- reconciliation ----------------------------------------------------

    def reconcile(
        self,
        previous_source: str,
        current_source: str,
    ) -> ReconciliationResult:
        previous = self.analyse(previous_source, previous=True)
        current = self.analyse(current_source)
        delivered = self._replay(previous, use_previous=True)
        previous_reports = self._reports_by_key(previous)
        result = ReconciliationResult()

        alternatives = self.alternatives(current)
        best: tuple[ReconciliationResult, int] | None = None
        all_failures: list[Failure] = []
        for alternative in alternatives:
            result.considered.append(alternative.name)
            pages, seals = self._replay(
                current, use_previous=False, alternative=alternative
            )
            verdicts: list[ReportVerdict] = []
            failures: list[Failure] = []
            for report in current.reports:
                key = self._report_key(current, report)
                verdict, report_failures = self._judge(
                    report,
                    pages.get(key),
                    delivered.get(key),
                    seals,
                    alternative,
                    previous_reports.get(key),
                )
                verdicts.append(verdict)
                failures.extend(report_failures)
            all_failures.extend(failures)
            needed_reads = self._needed_reads(current, verdicts, alternative)
            total_bytes = sum(read.byte_cost for read in needed_reads)
            total_fees = sum(read.fee for read in needed_reads)
            alternative.bytes_total = total_bytes
            alternative.fee_total = total_fees
            reusable = [v for v in verdicts if v.reusable]
            redo = [v for v in verdicts if not v.reusable]
            # Satisfy as many delivered pages as possible first, then pick
            # the cheapest (bytes, then fees) alternative that does so.
            score = (len(redo), total_bytes, total_fees)
            if best is None or score < best[1]:
                best = (
                    ReconciliationResult(
                        reusable=reusable,
                        redo=redo,
                        chosen=alternative,
                        reads=needed_reads,
                        total_bytes=total_bytes,
                        total_fees=total_fees,
                        failures=list(all_failures),
                        considered=list(result.considered),
                    ),
                    score,
                )
        assert best is not None
        final = best[0]
        final.considered = result.considered
        final.failures = self._dedupe_failures(all_failures)
        return final

    def _needed_reads(
        self,
        analyser: LineageAnalyser,
        verdicts: list[ReportVerdict],
        alternative: Alternative,
    ) -> list[ReadRef]:
        # Reusable reports need no new reads.  Redo reports pull every
        # external read reachable from their generations, stopping at
        # generations available as allowed sealed snapshots.  One original
        # read shared by several pages is billed a single time.
        keys: dict[tuple[str, str], ReadRef] = {}
        reusable = {v.name for v in verdicts if v.reusable}

        def collect(read: ReadRef) -> None:
            keys.setdefault(read.key, read)

        for report in analyser.reports:
            if report.name in reusable:
                continue
            for read in report.reads:
                collect(read)
            for generation_id in self._reachable_generations(analyser, report):
                record = analyser.generations.get(generation_id)
                if record is None or record.sealed:
                    continue
                for read in record.reads:
                    collect(read)

        reads: list[ReadRef] = []
        for read in sorted(keys.values(), key=lambda item: item.seq):
            label = alternative.selection.get(read.table, _BASE)
            byte_cost, fee = self._externals[read.table].cost_for(label)
            reads.append(
                ReadRef(
                    table=read.table,
                    instance=read.instance,
                    variant=label,
                    byte_cost=byte_cost,
                    fee=fee,
                    key=read.key,
                    seq=read.seq,
                )
            )
        return reads

    def _reachable_generations(self, analyser: LineageAnalyser, report):
        seen: set[str] = set()
        stack = list(analyser.generations)
        # Walk parents from the generations referenced by the report.
        roots = {
            source.split('@', 1)[0]
            for column in report.columns
            for source, _ in column.sources
            if '#' in source and not source.startswith('cte#')
        }
        stack = list(roots)
        while stack:
            generation_id = stack.pop()
            if generation_id in seen:
                continue
            seen.add(generation_id)
            record = analyser.generations.get(generation_id)
            if record is not None:
                stack.extend(record.parents)
        return seen

    # -- judgements --------------------------------------------------------

    @staticmethod
    def _external_sources(report) -> set[tuple[str, str]]:
        sources: set[tuple[str, str]] = set()
        for column in report.columns:
            for generation, _ in column.sources:
                if not generation.startswith('ext:'):
                    continue
                body = generation[len('ext:'):]
                table, instance = body.rsplit('@', 1)
                sources.add((table, instance))
        return sources

    def _source_drift(
        self, report, previous_report, alternative: Alternative
    ) -> list[Failure]:
        if previous_report is None:
            return []
        current_sources = self._external_sources(report)
        previous_sources = self._external_sources(previous_report)
        failures: list[Failure] = []
        for table, current_instance in sorted(current_sources):
            previous_instance = dict(previous_sources).get(table)
            if previous_instance is None or previous_instance == current_instance:
                continue
            label = alternative.selection.get(table, _BASE)
            failures.append(
                Failure(
                    variant_label=label,
                    table=table,
                    instance=current_instance,
                    column='*',
                    expected=f'physical instance {previous_instance}',
                    observed=f'physical instance {current_instance}',
                    row_ref=None,
                )
            )
        return failures

    def _judge(
        self,
        report,
        current_page,
        delivered_page,
        seals: dict[str, list[tuple]],
        alternative: Alternative,
        previous_report=None,
    ):
        failures: list[Failure] = []
        if delivered_page is None:
            return (
                ReportVerdict(
                    name=report.name,
                    reusable=False,
                    reason='no previously delivered page on record',
                    reads=list(report.reads),
                    line_no=report.line_no,
                ),
                failures,
            )
        if report.sealed_snapshot is not None and report.sealed_snapshot in seals:
            # The page is pinned to an allowed read-only snapshot.  That
            # generation is immutable and cannot be replaced by a
            # same-named variable, so the delivered page stays archived
            # as-is and needs no new external read.
            return (
                ReportVerdict(
                    name=report.name,
                    reusable=True,
                    reason=(
                        'pinned to sealed read-only snapshot '
                        f'{report.sealed_snapshot}; the same-named new '
                        'generation cannot replace it'
                    ),
                    reads=list(report.reads),
                    line_no=report.line_no,
                ),
                failures,
            )
        drift = self._source_drift(report, previous_report, alternative)
        if self._same_page(delivered_page, current_page) and not drift:
            return (
                ReportVerdict(
                    name=report.name,
                    reusable=True,
                    reason=(
                        'display values, row order and column sources '
                        'preserved'
                    ),
                    reads=list(report.reads),
                    line_no=report.line_no,
                ),
                failures,
            )
        if self._same_page(delivered_page, current_page) and drift:
            # The displayed rows still match, but they now come from a
            # different physical original, so the page must be redone.
            return (
                ReportVerdict(
                    name=report.name,
                    reusable=False,
                    reason=(
                        'displayed rows match but the physical source '
                        'instance changed'
                    ),
                    reads=list(report.reads),
                    line_no=report.line_no,
                    failures=drift,
                ),
                drift,
            )
        failures = self._explain(
            report, delivered_page, current_page, alternative
        )
        failures.extend(drift)
        return (
            ReportVerdict(
                name=report.name,
                reusable=False,
                reason=(
                    'displayed rows diverge under alternative '
                    f'{alternative.name!r}'
                ),
                reads=list(report.reads),
                line_no=report.line_no,
                failures=failures,
            ),
            failures,
        )

    @staticmethod
    def _same_page(left, right):
        return [tuple(row) for row in left] == [tuple(row) for row in right]

    def _explain(self, report, expected, page, alternative):
        failures: list[Failure] = []
        width = max(
            len(expected[0]) if expected else 0,
            len(page[0]) if page else 0,
        )
        for row_index in range(max(len(expected), len(page))):
            old_row = expected[row_index] if row_index < len(expected) else None
            new_row = page[row_index] if row_index < len(page) else None
            for column_index in range(width):
                old_value = old_row[column_index] if old_row else None
                new_value = new_row[column_index] if new_row else None
                if old_value == new_value:
                    continue
                column = self._column_at(report, column_index)
                failures.append(
                    self._failure_for(
                        column,
                        old_value,
                        new_value,
                        alternative,
                        row_index,
                    )
                )
        return failures

    @staticmethod
    def _column_at(report, index) -> ColumnRef | None:
        if index < len(report.columns):
            return report.columns[index]
        return None

    def _failure_for(
        self, column, expected, observed, alternative, row_index
    ) -> Failure:
        if column is not None and column.sources:
            generation, physical_column = column.sources[0]
        else:
            generation, physical_column = '?', '?'
        table = generation.removeprefix('ext:').split('@', 1)[0].split('#')[0]
        instance = (
            generation.rsplit('@', 1)[-1] if '@' in generation else '?'
        )
        label = alternative.selection.get(table, _BASE)
        version = self._externals.get(table)
        row_ref = self._row_ref(version, row_index)
        return Failure(
            variant_label=label,
            table=table,
            instance=instance,
            column=physical_column,
            expected=expected,
            observed=observed,
            row_ref=row_ref,
        )

    @staticmethod
    def _row_ref(version: ExternalVersion | None, row_index):
        if version is None or row_index >= len(version.row_meta):
            return None
        meta = version.row_meta[row_index]
        return RowMeta(line_no=meta.line_no, pk=meta.pk)

    @staticmethod
    def _dedupe_failures(failures: list[Failure]) -> list[Failure]:
        # Keep one failure per (alternative, table instance, column,
        # value pair) so each breaking substitution stays identifiable.
        seen: set[tuple] = set()
        unique: list[Failure] = []
        for failure in failures:
            key = (
                failure.variant_label,
                failure.table,
                failure.instance,
                failure.column,
                failure.expected,
                failure.observed,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(failure)
        return unique

    # -- sqlite replay -----------------------------------------------------

    def _report_key(self, analyser: LineageAnalyser, report) -> tuple[str, int]:
        return self._select_keys(analyser)[report.statement_index]

    def _select_keys(
        self, analyser: LineageAnalyser
    ) -> dict[int, tuple[str, int]]:
        """Ordinal key (normalized text, occurrence) per SELECT.

        Ordinal numbering is relative to each script, so the k-th
        execution of an identical query in the previous and current order
        matches.  The same query before and after a rollback gets
        different ordinals, while repeated pages keep their alignment.
        """
        seen: dict[str, int] = {}
        keys: dict[int, tuple[str, int]] = {}
        for index, stmt in enumerate(analyser.statements):
            if not self._significant(stmt):
                continue
            if analyser._statement_kind(stmt) != 'select':
                continue
            text = _page_key(str(stmt))
            occurrence = seen.get(text, 0)
            seen[text] = occurrence + 1
            keys[index] = (text, occurrence)
        return keys

    def _replay(
        self,
        analyser: LineageAnalyser,
        use_previous: bool,
        alternative: Alternative | None = None,
    ):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        pages: dict[tuple[str, int], list[tuple]] = {}
        seals: dict[str, list[tuple]] = {}
        keys = self._select_keys(analyser)
        gen_by_stmt = {
            record.statement_index: generation_id
            for generation_id, record in analyser.generations.items()
        }
        try:
            self._load_externals(connection, use_previous, alternative)
            for index, stmt in enumerate(analyser.statements):
                if not self._significant(stmt):
                    continue
                kind = analyser._statement_kind(stmt)
                if kind == 'select':
                    pages[keys[index]] = self._run_select(
                        connection, str(stmt)
                    )
                    continue
                if kind == 'create_temp':
                    # Re-creating the same logical table is legal in the
                    # work-order model (a new generation); SQLite needs the
                    # prior name dropped first.  Inside a savepoint the DROP
                    # is itself restored by ROLLBACK TO SAVEPOINT.
                    table_name = analyser._first_identifier(stmt)
                    connection.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                connection.execute(str(stmt))
                if kind == 'create_temp':
                    self._snapshot_sealed(
                        analyser, gen_by_stmt.get(index), connection, seals
                    )
        finally:
            connection.close()
        if use_previous:
            return pages
        return pages, seals

    def _load_externals(self, connection, use_previous, alternative):
        for table, version in self._externals.items():
            column_sql = ', '.join(f'"{col}"' for col in version.columns)
            connection.execute(f'CREATE TABLE "{table}" ({column_sql})')
            if use_previous:
                data = (
                    version.previous_rows
                    if version.previous_rows is not None
                    else version.rows
                )
            elif alternative is not None:
                label = alternative.selection.get(table, _BASE)
                data = (
                    version.variants[label]
                    if label != _BASE
                    else version.rows
                )
            else:
                data = version.rows
            placeholders = ', '.join('?' for _ in version.columns)
            connection.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})',
                list(data),
            )

    def _snapshot_sealed(
        self, analyser, generation_id, connection, seals
    ):
        if generation_id is None:
            return
        record = analyser.generations.get(generation_id)
        if record is None or not record.sealed:
            return
        frozen = self._frozen_name(generation_id)
        connection.execute(
            f'CREATE TABLE "{frozen}" AS SELECT * FROM "{record.name}"'
        )
        seals[generation_id] = self._run_select(
            connection, f'SELECT * FROM "{frozen}"'
        )

    @staticmethod
    def _frozen_name(generation_id: str) -> str:
        return 'seal__' + generation_id.replace('#', '_').replace('@', '_')

    def _run_select(self, connection, sql_text):
        cursor = connection.execute(sql_text)
        return [tuple(row) for row in cursor.fetchall()]

    @staticmethod
    def _significant(stmt):
        return [token for token in stmt.tokens if not token.is_whitespace]
