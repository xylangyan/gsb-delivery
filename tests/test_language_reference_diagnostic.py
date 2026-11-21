import hashlib
import importlib.util
import json
import pathlib

import sys

import pytest

DIAGNOSTIC = pathlib.Path(__file__).parents[1] / 'diagnostics' / 'language_reference_migration' / 'diagnose.py'


@pytest.fixture(scope='module')
def diag():
    spec = importlib.util.spec_from_file_location('language_reference_diagnose', DIAGNOSTIC)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def cases(diag, tmp_path_factory):
    root = tmp_path_factory.mktemp('langref')
    return root, diag.build_cases(root)


def _case(diag, cases, name):
    root, dirs = cases
    case_dir = next(d for d in dirs if d.name == name)
    meta = json.loads((case_dir / 'case.json').read_text(encoding='utf8'))
    return diag.inspect(
        case_dir / 'metadata.json', diag.FORM_TABLE, 'f1', 'Language_PK',
        name=meta['case'], description=meta['description'])


def _row(probe):
    assert probe.status == 'ok', probe.error
    assert len(probe.rows) == 1
    return probe.rows[0]


def test_core_divergence_is_not_caught_by_validation(diag, cases):
    report = _case(diag, cases, '02_stale_fk_target')
    assert report.validation.status == 'valid'
    direct, cldf_id, orm = report.probes[:3]
    assert _row(direct).resource == 'languages-legacy.csv'
    assert _row(direct).pk == ('deu',)
    assert _row(direct).name == 'German (historical)'
    assert _row(orm).resource == 'languages.csv'
    assert _row(orm).pk == ('deu',)
    assert _row(orm).name == 'German (modern)'
    assert _row(cldf_id).pk == ('other-pk',)
    assert _row(cldf_id).cldf_id == 'deu'
    assert _row(cldf_id).name == 'Another language'
    verdicts = [v for v, _ in diag.divergence_notes(report)]
    assert 'DIVERGENT' in verdicts
    assert 'NOT CAUGHT BY VALIDATION' in verdicts
    assert 'CLDF-ID COLLISION' in verdicts


def test_property_url_variant_is_schema_invalid_but_fk_still_reads(diag, cases):
    report = _case(diag, cases, '01_property_url_changed')
    assert report.validation.status == 'invalid'
    assert any(
        'languageReference' in message and 'requires column' in message
        for message in report.validation.messages)
    assert _row(report.probes[0]).resource == 'languages.csv'
    assert report.probes[2].status == 'none'


def test_unreadable_legacy_target(diag, cases):
    report = _case(diag, cases, '03_stale_target_unreadable')
    assert report.validation.status == 'exception'
    assert report.probes[0].status == 'error'
    assert 'FileNotFoundError' in report.probes[0].error
    assert _row(report.probes[2]).name == 'German (modern)'


def test_deleted_current_pk_is_silent_in_validation(diag, cases):
    report = _case(diag, cases, '04_current_pk_deleted')
    assert report.validation.status == 'valid'
    assert not report.expected.rows
    assert _row(report.probes[0]).name == 'German (historical)'
    assert report.probes[2].status == 'error'
    assert 'KeyError' in report.probes[2].error
    verdicts = [v for v, _ in diag.divergence_notes(report)]
    assert 'MISSING KEY NOT REPORTED' in verdicts


def test_repair_changes_one_field_and_reconciles_direct_target(diag, cases, tmp_path):
    root, _ = cases
    stale = root / '02_stale_fk_target'
    repaired_dir = tmp_path / 'repaired'
    change = diag.repair_fk_resource(stale, repaired_dir)
    assert change['occurrences'] == 1
    assert change['old'] == '"resource": "languages-legacy.csv"'
    assert change['new'] == '"resource": "languages.csv"'
    assert json.loads((repaired_dir / 'metadata.json').read_text())['tables'][0][
        'tableSchema']['foreignKeys'][0]['reference']['resource'] == 'languages.csv'

    before = _case(diag, cases, '02_stale_fk_target')
    after = diag.inspect(
        repaired_dir / 'metadata.json', diag.FORM_TABLE, 'f1', 'Language_PK',
        name='repaired', description='one-field FK resource repair')
    assert _row(before.probes[0]).name == 'German (historical)'
    assert _row(after.probes[0]).name == 'German (modern)'
    # The ORM never consulted the stale resource; it already agreed with the repair.
    assert _row(before.probes[2]).name == _row(after.probes[2]).name


def test_existing_dataset_mode_is_read_only(diag, cases, tmp_path):
    root, _ = cases
    source = root / '02_stale_fk_target'
    target = tmp_path / 'copy'
    import shutil
    shutil.copytree(source, target)

    def digest(path):
        return {
            p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(target.rglob('*')) if p.is_file()
        }

    before = digest(target)
    mtimes = {p: p.stat().st_mtime_ns for p in target.rglob('*') if p.is_file()}
    report = diag.inspect(
        diag.resolve_metadata(target), diag.FORM_TABLE, 'f1', 'Language_PK',
        name='read-only', description='existing dataset')
    assert _row(report.probes[0]).name == 'German (historical)'
    assert digest(target) == before
    assert {p: p.stat().st_mtime_ns for p in target.rglob('*') if p.is_file()} == mtimes


def test_cli_generates_report_without_absolute_workspace_paths(diag, tmp_path, capsys):
    out = tmp_path / 'cli'
    assert diag.main(['--out', str(out)]) == 0
    text = capsys.readouterr().out
    assert '# CLDF language-reference migration diagnostic' in text
    assert '## Repair provenance' in text
    assert 'NOT CAUGHT BY VALIDATION' in text
    assert 'MISSING KEY NOT REPORTED' in text
    assert str(pathlib.Path(__file__).parents[1]) not in text
    assert (out / '05_repaired_fk_resource' / 'metadata.json').exists()


def test_cli_dataset_mode_reads_supplied_case(diag, cases, capsys):
    root, _ = cases
    case = root / '02_stale_fk_target'
    assert diag.main(['--dataset', str(case), '--form-id', 'f1']) == 0
    text = capsys.readouterr().out
    assert 'existing dataset (read-only)' in text
    assert 'German (historical)' in text
    assert 'German (modern)' in text
    assert 'Another language' in text
