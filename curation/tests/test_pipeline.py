import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pycldf import Wordlist

from curation.holdingcldf import cldf_builder as builder
from curation.holdingcldf.errors import ArchiveError
from curation.holdingcldf.pipeline import build_edition_one, rearchive_edition_two
from curation.holdingcldf.provenance import (
    EDITION_ONE,
    EDITION_TWO,
    HANDOFF_NAME,
    MANIFEST_NAME,
    read_manifest,
)

DATA = ROOT / 'curation' / 'data'
CATALOG_V1 = DATA / 'catalog' / 'edition1'
CATALOG_V2 = DATA / 'catalog' / 'edition2'
LIBRARY = DATA / 'source_library.tsv'
BATCH_ALPHA = DATA / 'batches' / 'batch_alpha'
BATCH_BETA = DATA / 'batches' / 'batch_beta'
SUPPLEMENT = DATA / 'supplement_beta'


def _source_cells(artifact_dir):
    return builder.read_back_citations(artifact_dir)


def _bib_keys(artifact_dir):
    return set(builder.read_back_sources(artifact_dir).keys())


def _dataset(artifact_dir):
    return Wordlist.from_metadata(artifact_dir / builder.METADATA_NAME)


def test_edition_one_build_and_readback(tmp_path):
    out = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA],
        catalog_dir=CATALOG_V1,
        source_library=LIBRARY,
        output_dir=out,
    )
    # The artifact is a valid CLDF Wordlist.
    _dataset(out).validate()

    manifest = read_manifest(out)
    assert manifest['archive_version'] == EDITION_ONE
    assert manifest['numbering_context']['catalogue_edition'] == 'edition-1'

    # Exactly one BibTeX entry per holding, keyed by edition-1 official codes.
    assert _bib_keys(out) == {'X1', 'X2'}
    bib_text = (out / 'sources.bib').read_text(encoding='utf-8')
    assert bib_text.count('@book{X1,') == 1
    assert bib_text.count('@article{X2,') == 1

    cells = _source_cells(out)
    # Order, page ranges, appendix section and repeated citations are preserved.
    assert cells['forms.csv'] == [
        ['X1[3-5]', 'X1[12]', 'X1[18]'],   # alpha a-f1: local X1 -> R37
        ['X2[7]'],                          # alpha a-f2: local X2 -> R88
        ['X1[3-5]', 'X1[40]'],              # beta b-f1: local AB -> R37
        ['X2[22]'],                         # beta b-f2: beta local X1 -> R88
    ]
    assert cells['examples.csv'] == [
        ['X1[Appendix A]'],
        ['X1[Appendix A]'],
    ]

    # Citations are read back through the CLDF source property columns.
    ds = _dataset(out)
    form_refs = []
    for row in ds.iter_rows('FormTable', 'source'):
        form_refs.append([str(r) for r in ds.sources.expand_refs(row['source'])])
    assert form_refs == cells['forms.csv']
    descriptions = [
        ref.description for ref in ds.sources.expand_refs(
            ds.get_row('FormTable', 'alpha-a-f1')[builder.FORM_REF_COLUMN])]
    assert descriptions == ['3-5', '12', '18']

    # The same "X1" string in non-citation fields is left untouched.
    comments = [
        row['Comment'] for row in ds.iter_rows('FormTable') if row['Comment']]
    assert 'decoy string X1 in a non-citation field' in comments
    assert 'raw X1 appears here as prose too' in comments


def test_local_citation_columns_have_distinct_names(tmp_path):
    out = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA], catalog_dir=CATALOG_V1,
        source_library=LIBRARY, output_dir=out)
    ds = _dataset(out)
    columns = dict(builder.source_columns(ds))
    assert columns['forms.csv'] == builder.FORM_REF_COLUMN
    assert columns['examples.csv'] == builder.EXAMPLE_REF_COLUMN
    assert builder.FORM_REF_COLUMN != builder.EXAMPLE_REF_COLUMN
    # Both columns are declared with the CLDF source property.
    assert set(columns) == {'forms.csv', 'examples.csv'}


def test_edition_two_rearchive_routes_each_code_to_its_work(tmp_path):
    v1 = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA], catalog_dir=CATALOG_V1,
        source_library=LIBRARY, output_dir=v1)

    v2 = tmp_path / 'edition2'
    rearchive_edition_two(
        edition_one_dir=v1, supplement_dir=SUPPLEMENT, catalog_dir=CATALOG_V2,
        source_library=LIBRARY, output_dir=v2)
    _dataset(v2).validate()

    manifest = read_manifest(v2)
    assert manifest['archive_version'] == EDITION_TWO
    assert manifest['numbering_context']['catalogue_edition'] == 'edition-2'
    assert manifest['official_code_migration'] == {
        'X1': {'holding_id': 'R37', 'edition_2_official': 'CAT37'},
        'X2': {'holding_id': 'R88', 'edition_2_official': 'CAT88'},
    }

    # One BibTeX entry per holding under edition-2 keys.
    assert _bib_keys(v2) == {'CAT37', 'CAT88'}
    bib_text = (v2 / 'sources.bib').read_text(encoding='utf-8')
    assert bib_text.count('@book{CAT37,') == 1
    assert bib_text.count('@article{CAT88,') == 1
    assert 'X1' not in bib_text and 'X2' not in bib_text

    cells = _source_cells(v2)
    # Prior artifact: official v1 keys migrated; descriptions and order kept.
    assert cells['forms.csv'][:4] == [
        ['CAT37[3-5]', 'CAT37[12]', 'CAT37[18]'],
        ['CAT88[7]'],
        ['CAT37[3-5]', 'CAT37[40]'],
        ['CAT88[22]'],
    ]
    # Supplement: supplier-local codes resolved through beta's own mapping.
    # Beta raw X1 means R88, beta AB means R37.
    assert cells['forms.csv'][4:] == [
        ['CAT88[9]'],
        ['CAT37[51]', 'CAT37[52]'],
    ]
    assert cells['examples.csv'] == [
        ['CAT37[Appendix A]'],
        ['CAT37[Appendix A]'],
        ['CAT37[Appendix A]'],
    ]

    ds = _dataset(v2)
    for row in ds.iter_rows('FormTable', 'source'):
        ds.sources.validate(row['source'])


def test_edition_two_is_repeatable_and_refuses_wrong_context(tmp_path):
    v1 = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA], catalog_dir=CATALOG_V1,
        source_library=LIBRARY, output_dir=v1)
    v2a = tmp_path / 'edition2a'
    v2b = tmp_path / 'edition2b'
    rearchive_edition_two(
        edition_one_dir=v1, supplement_dir=SUPPLEMENT, catalog_dir=CATALOG_V2,
        source_library=LIBRARY, output_dir=v2a)
    # Re-run the second-edition archival on the same finished inputs.
    rearchive_edition_two(
        edition_one_dir=v1, supplement_dir=SUPPLEMENT, catalog_dir=CATALOG_V2,
        source_library=LIBRARY, output_dir=v2b)
    assert _source_cells(v2a) == _source_cells(v2b)
    assert _bib_keys(v2a) == _bib_keys(v2b) == {'CAT37', 'CAT88'}

    # Re-archiving must not silently reinterpret an edition-2 artifact.
    wrong = tmp_path / 'wrong'
    with pytest.raises(ValueError, match='wrong numbering context'):
        rearchive_edition_two(
            edition_one_dir=v2a, supplement_dir=SUPPLEMENT, catalog_dir=CATALOG_V2,
            source_library=LIBRARY, output_dir=wrong)
    assert not wrong.exists()


def test_failure_missing_target_is_structured_and_atomic(tmp_path):
    out = tmp_path / 'should_not_exist'
    with pytest.raises(ArchiveError) as exc_info:
        build_edition_one(
            batch_dirs=[DATA / 'bad_missing'], catalog_dir=CATALOG_V1,
            source_library=LIBRARY, output_dir=out)
    problem = exc_info.value.problems[0]
    assert problem.kind == 'missing_target'
    assert problem.batch == 'bad-missing'
    assert problem.column == 'Witness_Ref'
    assert problem.row_number == 1
    assert problem.row_id == 'm-f1'
    assert problem.raw_ref == 'ZZ[10]'
    assert 'ZZ' in exc_info.value.args[0]
    # Nothing was written: no half-updated artifact.
    assert not out.exists()


def test_failure_ambiguous_target_lists_both_candidates(tmp_path):
    out = tmp_path / 'should_not_exist'
    with pytest.raises(ArchiveError) as exc_info:
        build_edition_one(
            batch_dirs=[DATA / 'bad_ambiguous'], catalog_dir=CATALOG_V1,
            source_library=LIBRARY, output_dir=out)
    problem = exc_info.value.problems[0]
    assert problem.kind == 'ambiguous_target'
    assert problem.batch == 'bad-ambiguous'
    assert problem.column == 'Witness_Ref'
    assert problem.row_number == 1
    assert problem.row_id == 'q-f1'
    assert problem.candidates == ['R37', 'R88']
    assert 'R37' in exc_info.value.args[0] and 'R88' in exc_info.value.args[0]
    assert not out.exists()


def test_supplement_failure_is_structured(tmp_path):
    v1 = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA], catalog_dir=CATALOG_V1,
        source_library=LIBRARY, output_dir=v1)
    bad_supplement = tmp_path / 'bad_supplement'
    import shutil
    shutil.copytree(SUPPLEMENT, bad_supplement)
    (bad_supplement / 'forms.tsv').write_text(
        'ID\tLanguage_ID\tParameter_ID\tForm\tComment\tcitation_refs\n'
        's-f9\twest2\tsmoke\tkwan\t\tNOPE[3]\n', encoding='utf-8')
    out = tmp_path / 'no_v2'
    with pytest.raises(ArchiveError) as exc_info:
        rearchive_edition_two(
            edition_one_dir=v1, supplement_dir=bad_supplement,
            catalog_dir=CATALOG_V2, source_library=LIBRARY, output_dir=out)
    problem = exc_info.value.problems[0]
    assert problem.kind == 'missing_target'
    assert problem.batch == 'beta-supplement-2026-09 (batch beta)'
    assert problem.column == 'citation_refs'
    assert problem.row_id == 's-f9'
    assert not out.exists()


def test_handoff_documents_numbering_context(tmp_path):
    v1 = tmp_path / 'edition1'
    build_edition_one(
        batch_dirs=[BATCH_ALPHA, BATCH_BETA], catalog_dir=CATALOG_V1,
        source_library=LIBRARY, output_dir=v1)
    v2 = tmp_path / 'edition2'
    rearchive_edition_two(
        edition_one_dir=v1, supplement_dir=SUPPLEMENT, catalog_dir=CATALOG_V2,
        source_library=LIBRARY, output_dir=v2)

    handoff_v1 = (v1 / HANDOFF_NAME).read_text(encoding='utf-8')
    assert 'Do NOT guess from the shape of a key' in handoff_v1
    assert 'edition-1' in handoff_v1
    assert 'Witness_Ref' in handoff_v1 and 'Example_Ref' in handoff_v1

    handoff_v2 = (v2 / HANDOFF_NAME).read_text(encoding='utf-8')
    assert 'X1 -> CAT37' in handoff_v2
    assert 'holding R37' in handoff_v2

    # The manifest files record batches and catalogue editions with the artifact.
    m1 = read_manifest(v1)
    assert {b['batch_id'] for b in m1['batches']} == {'alpha', 'beta'}
    assert m1['numbering_context']['catalogue_id'] == 'CATALOG-V1'
    m2 = read_manifest(v2)
    assert m2['supplements'][0]['package_id'] == 'beta-supplement-2026-09'
    assert m2['prior_artifact']['archive_version'] == EDITION_ONE
    assert (v1 / MANIFEST_NAME).exists() and (v2 / MANIFEST_NAME).exists()
