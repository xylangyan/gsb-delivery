"""Tests for the shift reconciliation (``sqlparse.reconcile``)."""

from __future__ import annotations

import json

import pytest

from sqlparse.cli import main as cli_main
from sqlparse.reconcile import (
    AllowedSeal,
    ExternalVersion,
    LineageAnalyser,
    ReconcileError,
    Reconciler,
    RowMeta,
    load_work_order,
    render_json,
    render_text,
)


def _external(name, instance, columns, rows, **kwargs):
    meta = kwargs.pop(
        'row_meta',
        [RowMeta(line_no=index + 1, pk=row[0]) for index, row in enumerate(rows)],
    )
    return ExternalVersion(
        table=name,
        instance=instance,
        columns=list(columns),
        bytes=kwargs.pop('bytes', 100),
        fee=kwargs.pop('fee', 1.0),
        rows=[tuple(row) for row in rows],
        row_meta=meta,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# static lineage analysis
# ---------------------------------------------------------------------------

def test_create_and_insert_build_numbered_generations():
    externals = {'src': _external('src', 'i1', ['id', 'v'], [(1, 'a')])}
    sql = (
        'CREATE TEMP TABLE t (id INT, v TEXT);\n'
        'INSERT INTO t (id, v) SELECT id, v FROM src;\n'
        'INSERT INTO t SELECT id, v FROM src;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    assert list(analyser.generations) == ['t#1', 't#2', 't#3']
    first, second, third = analyser.generations.values()
    assert [column.display for column in first.columns] == ['id', 'v']
    assert second.parents == ['t#1']
    assert third.parents == ['t#2']
    # One physical read shared by both INSERT statements, registered once.
    assert len(analyser._reads) == 1


def test_rollback_restores_prior_generation_and_keeps_history():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    sql = (
        'SAVEPOINT sp1;\n'
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'ROLLBACK TO SAVEPOINT sp1;\n'
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    assert list(analyser.generations) == ['t#1', 't#2']
    # The visible name points at the post-rollback generation, but the
    # earlier generation is still reachable for archived pages.
    assert analyser._visible['t'] == 't#2'
    assert 't#1' in analyser.generations


def test_with_cte_shadows_same_named_temp_table():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    sql = (
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'WITH t AS (SELECT id FROM raw) SELECT id FROM t;\n'
        'SELECT id, v FROM t;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    shadowed, normal = analyser.reports
    assert shadowed.cte_names == frozenset({'t'})
    assert normal.cte_names == frozenset()
    shadow_binding = shadowed.temp_refs
    assert shadow_binding == frozenset()  # CTE, not the temp table
    assert normal.temp_refs == frozenset({'t#1'})


def test_using_join_keeps_both_column_sources():
    externals = {
        'a': _external('a', 'ia', ['k', 'x'], [(1, 'ax')]),
        'b': _external('b', 'ib', ['k', 'y'], [(1, 'by')]),
    }
    sql = 'SELECT a.k, a.x, b.y FROM a JOIN b USING (k);'
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    columns = {column.display: column for column in analyser.reports[0].columns}
    # One displayed value for k, but both physical sources are retained.
    assert columns['k'].sources == (
        ('ext:a@ia', 'k'),
        ('ext:b@ib', 'k'),
    )
    assert columns['x'].sources == (('ext:a@ia', 'x'),)
    assert columns['y'].sources == (('ext:b@ib', 'y'),)


def test_report_line_numbers_point_at_source():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    sql = (
        '\n\nCREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'SELECT id, v FROM t;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    assert analyser.generations['t#1'].line_no == 3
    assert analyser.reports[0].line_no == 4


def test_leading_comments_are_ignored_and_lines_stay_accurate():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    sql = (
        '-- work order header\n'
        'SELECT id FROM raw;\n'
        '-- note between pages\n'
        'SELECT v FROM raw;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    assert [report.line_no for report in analyser.reports] == [2, 4]
    assert [report.name for report in analyser.reports] == [
        'report@0',
        'report@1',
    ]


def test_unsupported_subset_is_rejected():
    externals = {'raw': _external('raw', 'i', ['id'], [(1,)])}
    with pytest.raises(ReconcileError):
        LineageAnalyser("UPDATE raw SET id = 2;", externals).run()
    with pytest.raises(ReconcileError):
        LineageAnalyser(
            'SELECT * FROM (SELECT id FROM raw) q;', externals
        ).run()


# ---------------------------------------------------------------------------
# reconciliation verdicts
# ---------------------------------------------------------------------------

def test_identical_order_reuses_delivered_page_without_new_reads():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    sql = 'SELECT id, v FROM raw;'
    result = Reconciler(externals).reconcile(sql, sql)

    assert [verdict.name for verdict in result.reusable] == ['report@0']
    assert result.redo == []
    assert result.total_bytes == 0
    assert result.total_fees == 0.0


def test_new_page_without_delivery_must_be_redone():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    result = Reconciler(externals).reconcile(
        'SELECT id FROM raw;',
        'SELECT id FROM raw;\nSELECT v FROM raw;',
    )

    assert [verdict.name for verdict in result.reusable] == ['report@0']
    assert [verdict.name for verdict in result.redo] == ['report@1']
    assert {read.table for read in result.reads} == {'raw'}


def test_cheapest_viable_alternative_is_selected():
    externals = {
        'raw': _external(
            'raw',
            'i',
            ['id', 'v'],
            [(1, 'a')],
            bytes=1000,
            fee=10.0,
            variants={'lean': [(1, 'a')]},
            variant_costs={'lean': (400, 4.0)},
        )
    }
    # The second page has no previous delivery, so raw must really be read;
    # the identical lean variant then wins on byte/fee cost.
    result = Reconciler(externals).reconcile(
        'SELECT v FROM raw;',
        'SELECT v FROM raw;\nSELECT id FROM raw;',
    )

    assert result.chosen.name == 'raw=lean'
    assert result.total_bytes == 400
    assert result.total_fees == 4.0


def test_breaking_alternative_is_reported_with_instance_and_row():
    externals = {
        'raw': _external(
            'raw',
            'i',
            ['id', 'v'],
            [(1, 'a')],
            variants={'cheap': [(1, 'Z')]},
            variant_costs={'cheap': (50, 0.5)},
        )
    }
    result = Reconciler(externals).reconcile(
        'SELECT v FROM raw;', 'SELECT v FROM raw;'
    )

    # The base alternative keeps the page; the cheap one is rejected.
    assert result.chosen.name == 'raw=base'
    failure = next(
        failure
        for failure in result.failures
        if failure.variant_label == 'cheap'
    )
    assert failure.table == 'raw'
    assert failure.instance == 'i'
    assert failure.column == 'v'
    assert (failure.expected, failure.observed) == ('a', 'Z')
    assert failure.row_ref == RowMeta(line_no=1, pk=1)


def test_source_instance_drift_forces_redo_on_identical_display():
    externals = {
        'raw': _external(
            'raw',
            'new-instance',
            ['id', 'v'],
            [(1, 'same')],
            previous_instance='old-instance',
            previous_rows=[(1, 'same')],
        )
    }
    result = Reconciler(externals).reconcile(
        'SELECT v FROM raw;', 'SELECT v FROM raw;'
    )

    assert result.reusable == []
    verdict = result.redo[0]
    assert 'physical source instance changed' in verdict.reason
    assert verdict.failures[0].instance == 'new-instance'


def test_shared_original_read_is_billed_once():
    externals = {
        'a': _external('a', 'ia', ['k', 'x'], [(1, 'x1')], bytes=100, fee=1.0),
        'b': _external('b', 'ib', ['k', 'y'], [(1, 'y1')], bytes=200, fee=3.0),
    }
    previous = 'SELECT a.x, b.y FROM a JOIN b USING (k);'
    current = previous + '\n' + previous
    result = Reconciler(externals).reconcile(previous, current)

    # The second execution is a new page, but each external is read once.
    assert len(result.reads) == 2
    assert sum(read.fee for read in result.reads) == 4.0


def test_sealed_snapshot_cannot_be_replaced_by_same_named_generation():
    externals = {
        'raw': _external(
            'raw',
            'i',
            ['id', 'v'],
            [(1, 'new')],
            previous_rows=[(1, 'old')],
        )
    }
    previous = (
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'SELECT id, v FROM t;'
    )
    current = (
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'SAVEPOINT s;\n'
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'ROLLBACK TO SAVEPOINT s;\n'
        'SELECT id, v FROM t;'
    )
    reconciler = Reconciler(externals, [AllowedSeal('t', 't#1')])
    result = reconciler.reconcile(previous, current)

    verdict = result.reusable[0]
    assert 'sealed read-only snapshot t#1' in verdict.reason


def test_actual_sqlite_rollback_and_with_shadow_replay():
    externals = {'raw': _external('raw', 'i', ['id', 'v'], [(1, 'a')])}
    previous = (
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'SELECT id, v FROM t;'
    )
    current = (
        'SAVEPOINT s;\n'
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'SELECT id, v FROM t;\n'
        'ROLLBACK TO SAVEPOINT s;\n'
        'CREATE TEMP TABLE t AS SELECT id, v FROM raw;\n'
        'WITH t AS (SELECT id FROM raw) SELECT id FROM t;\n'
        'SELECT id, v FROM t;'
    )
    result = Reconciler(externals).reconcile(previous, current)

    names = [verdict.name for verdict in result.reusable]
    # The first delivered page survives; the shadow query and final page
    # have no matching previous delivery.
    assert names == ['report@2']
    assert {verdict.name for verdict in result.redo} == {
        'report@5',
        'report@6',
    }


def test_alternative_label_is_consistent_across_all_reads_of_one_table():
    externals = {
        'raw': _external(
            'raw',
            'i',
            ['id', 'v'],
            [(1, 'a'), (2, 'b')],
            variants={'alt': [(1, 'A'), (2, 'B')]},
        )
    }
    reconciler = Reconciler(externals)
    current = reconciler.analyse('SELECT id FROM raw;\nSELECT v FROM raw;')

    alternatives = reconciler.alternatives(current)
    # One label per external table, so every read of one original row
    # uses the same permitted interpretation.
    assert set(alternatives[0].selection) == {'raw'}
    labels = {alt.selection['raw'] for alt in alternatives}
    assert labels == {'base', 'alt'}
    for alternative in alternatives:
        for table in alternative.selection:
            assert list(alternative.selection).count(table) == 1

    # Replay under 'alt' applies the same label to every read of raw:
    # the id page keeps integer keys while the v page shows A/B.
    pages, _ = reconciler._replay(current, False, alternatives[1])
    values = {row[0] for page in pages.values() for row in page}
    assert values == {1, 2, 'A', 'B'}


def test_intermediate_read_traces_back_to_original_row_metadata():
    externals = {
        'raw': _external(
            'raw',
            'i',
            ['id', 'v'],
            [(7, 'kept')],
            row_meta=[RowMeta(line_no=55, pk='pk-7')],
        )
    }
    sql = (
        'CREATE TEMP TABLE stage AS SELECT id, v FROM raw;\n'
        'SELECT v FROM stage;'
    )
    analyser = LineageAnalyser(sql, externals)
    analyser.run()

    stage = analyser.generations['stage#1']
    # The intermediate table keeps the external read and its lineage, so
    # an ordinary read of the stage still reaches the original table.
    assert [read.table for read in stage.reads] == ['raw']
    report_column = analyser.reports[0].columns[0]
    assert report_column.sources == (('ext:raw@i', 'v'),)

    result = Reconciler(externals).reconcile(
        'SELECT v FROM raw;',
        'SELECT v FROM raw;',
    )
    assert result.reusable[0].name == 'report@0'


# ---------------------------------------------------------------------------
# manifest + rendering + CLI
# ---------------------------------------------------------------------------

def test_load_work_order_manifest(filepath):
    order = load_work_order(filepath('recon/work_order.json'))

    assert set(order.externals) == {'orders', 'customers'}
    assert order.externals['orders'].instance == 'orders-v3'
    assert order.externals['orders'].cost_for('draft') == (420, 3.5)
    assert order.allowed_seals == [AllowedSeal('joined', 'joined#2')]
    assert 'ROLLBACK TO SAVEPOINT' in order.current_sql


def test_full_work_order_reconciliation(filepath):
    order = load_work_order(filepath('recon/work_order.json'))
    result = Reconciler(order.externals, order.allowed_seals).reconcile(
        order.previous_sql, order.current_sql
    )
    text = render_text(result)
    payload = json.loads(render_json(result))

    assert 'Reusable reports' in text
    assert 'Reports that must be redone' in text
    assert payload['chosen_alternative']
    assert {entry['table'] for entry in payload['reads']} <= {
        'orders',
        'customers',
    }


def test_cli_reconcile_text_and_json(filepath, capsys):
    manifest = filepath('recon/work_order.json')

    assert cli_main(['--reconcile', manifest]) == 0
    text = capsys.readouterr().out
    assert 'Shift reconciliation' in text

    assert cli_main(['--reconcile', manifest, '--reconcile-format', 'json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert 'reusable' in payload and 'redo' in payload
    verdicts = payload['reusable'] + payload['redo']
    assert all('line_no' in verdict for verdict in verdicts)


def test_cli_reconcile_missing_manifest(capsys):
    exit_code = cli_main(['--reconcile', '/no/such/manifest.json'])
    assert exit_code == 1
    assert 'Reconciliation failed' in capsys.readouterr().err