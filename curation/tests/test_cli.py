import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from curation.holdingcldf.cli import main
from curation.holdingcldf.provenance import read_manifest

DATA = ROOT / 'curation' / 'data'
CATALOG_V1 = DATA / 'catalog' / 'edition1'
CATALOG_V2 = DATA / 'catalog' / 'edition2'
LIBRARY = DATA / 'source_library.tsv'


def test_cli_end_to_end(tmp_path, capsys):
    v1 = tmp_path / 'v1'
    code = main([
        'edition1', '--output', str(v1),
        '--batch', str(DATA / 'batches' / 'batch_alpha'),
        '--batch', str(DATA / 'batches' / 'batch_beta'),
        '--catalog', str(CATALOG_V1), '--library', str(LIBRARY),
    ])
    assert code == 0
    assert 'wrote' in capsys.readouterr().out
    assert read_manifest(v1)['archive_version'] == 'edition-1'

    v2 = tmp_path / 'v2'
    code = main([
        'edition2', '--output', str(v2),
        '--edition-one', str(v1),
        '--supplement', str(DATA / 'supplement_beta'),
        '--catalog', str(CATALOG_V2), '--library', str(LIBRARY),
    ])
    assert code == 0
    assert read_manifest(v2)['archive_version'] == 'edition-2'


def test_cli_reports_failure_without_traceback(tmp_path, capsys):
    out = tmp_path / 'nope'
    code = main([
        'edition1', '--output', str(out),
        '--batch', str(DATA / 'bad_missing'),
        '--catalog', str(CATALOG_V1), '--library', str(LIBRARY),
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "batch='bad-missing'" in err
    assert 'row=1' in err and "raw_ref='ZZ[10]'" in err
    assert not out.exists()


def test_cli_rejects_wrong_numbering_context(tmp_path, capsys):
    v1 = tmp_path / 'v1'
    main([
        'edition1', '--output', str(v1),
        '--batch', str(DATA / 'batches' / 'batch_alpha'),
        '--catalog', str(CATALOG_V1), '--library', str(LIBRARY),
    ])
    v2 = tmp_path / 'v2'
    main([
        'edition2', '--output', str(v2),
        '--edition-one', str(v1),
        '--supplement', str(DATA / 'supplement_beta'),
        '--catalog', str(CATALOG_V2), '--library', str(LIBRARY),
    ])
    wrong = tmp_path / 'wrong'
    code = main([
        'edition2', '--output', str(wrong),
        '--edition-one', str(v2),
        '--supplement', str(DATA / 'supplement_beta'),
        '--catalog', str(CATALOG_V2), '--library', str(LIBRARY),
    ])
    assert code == 1
    assert 'wrong numbering context' in capsys.readouterr().err
    assert not wrong.exists()
