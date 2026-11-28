"""Human- and machine-readable rendering of reconciliation results."""

from __future__ import annotations

import json
from dataclasses import asdict

from sqlparse.reconcile.model import ReadRef, ReportVerdict
from sqlparse.reconcile.verifier import ReconciliationResult


def render_text(result: ReconciliationResult) -> str:
    lines: list[str] = []
    lines.append('Shift reconciliation')
    lines.append('=' * 40)
    if result.chosen is not None:
        lines.append(f"chosen alternative : {result.chosen.name}")
    lines.append(
        f'real reads required: {len(result.reads)} '
        f'({result.total_bytes} bytes, fees {result.total_fees:.2f})'
    )
    lines.append('')
    lines.append('Reusable reports (previous page stays valid)')
    lines.append('-' * 40)
    lines.extend(_verdict_lines(result.reusable))
    lines.append('')
    lines.append('Reports that must be redone with this shift')
    lines.append('-' * 40)
    lines.extend(_verdict_lines(result.redo))
    if result.failures:
        lines.append('')
        lines.append('Substitutions that break reuse')
        lines.append('-' * 40)
        for failure in result.failures:
            row_ref = (
                f'row line {failure.row_ref.line_no} pk {failure.row_ref.pk}'
                if failure.row_ref is not None
                else 'row unknown'
            )
            lines.append(
                f'- {failure.table}@{failure.instance} variant '
                f'{failure.variant_label!r}, column {failure.column}, '
                f'{row_ref}: delivered {failure.expected!r} vs '
                f'recomputed {failure.observed!r}'
            )
    if result.considered:
        lines.append('')
        lines.append('alternatives considered: ' + ', '.join(result.considered))
    return '\n'.join(lines)


def _verdict_lines(verdicts: list[ReportVerdict]) -> list[str]:
    if not verdicts:
        return ['  (none)']
    lines = []
    for verdict in verdicts:
        location = (
            f' (line {verdict.line_no})' if verdict.line_no is not None else ''
        )
        reads = ', '.join(_read_label(read) for read in verdict.reads) or '-'
        lines.append(f'- {verdict.name}{location}: {verdict.reason}')
        lines.append(f'    reads: {reads}')
    return lines


def _read_label(read: ReadRef) -> str:
    return f'{read.table}@{read.instance}({read.byte_cost}B)'


def render_json(result: ReconciliationResult) -> str:
    payload = {
        'chosen_alternative': (
            result.chosen.name if result.chosen is not None else None
        ),
        'total_bytes': result.total_bytes,
        'total_fees': result.total_fees,
        'reads': [asdict(read) for read in result.reads],
        'reusable': [_verdict_dict(v) for v in result.reusable],
        'redo': [_verdict_dict(v) for v in result.redo],
        'failures': [asdict(failure) for failure in result.failures],
        'considered': result.considered,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _verdict_dict(verdict: ReportVerdict) -> dict:
    return {
        'name': verdict.name,
        'line_no': verdict.line_no,
        'reusable': verdict.reusable,
        'reason': verdict.reason,
        'reads': [asdict(read) for read in verdict.reads],
        'failures': [asdict(failure) for failure in verdict.failures],
    }
