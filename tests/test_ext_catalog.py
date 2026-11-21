"""
Scenario: the old code X denoted entry L0 in 1990. L0 was split into L1 and L2
(marked with different historical regions), L1 was renamed (keeping its identity),
L2 was split again, and one of the final entries was later assigned the code X,
carrying a name that happens to match the other branch.
"""
import pathlib

import pytest

from pycldf import Wordlist, Dataset
from pycldf.ext.catalog import (
    Catalog,
    CatalogEntry,
    Revision,
    STATUS_AMBIGUOUS,
    STATUS_MISSING_CODE,
    STATUS_RESOLVED,
    STATUS_UNKNOWN_CODE,
    augment_wordlist,
    resolve,
)


@pytest.fixture
def catalog():
    entries = [
        CatalogEntry('L0-X', 'L0', 'X', 'Language Zero', 1985, 2000, {'R1', 'R2'}),
        CatalogEntry('L1-a', 'L1', 'A1', 'Language One', 2000, 2005, {'R1'}),
        CatalogEntry('L1-b', 'L1', 'A1', 'Language One New', 2005, None, {'R1'}),
        CatalogEntry('L2-a', 'L2', 'A2', 'Language Two', 2000, 2010, {'R2'}),
        # A final entry of the L2 branch, named exactly like the renamed L1:
        CatalogEntry('L2a-a', 'L2a', 'A3', 'Language One New', 2010, 2015, {'R2a'}),
        # ... and later re-assigned the old code X:
        CatalogEntry('L2a-x', 'L2a', 'X', 'Language One New', 2015, None, {'R2a'}),
        CatalogEntry('L2b-a', 'L2b', 'A4', 'Language Two B', 2010, None, {'R2b'}),
    ]
    revisions = [
        Revision('split', 'L0', ('L1', 'L2'), 2000),
        Revision('rename', 'L1', ('L1',), 2005),
        Revision('split', 'L2', ('L2a', 'L2b'), 2010),
        Revision('rename', 'L2a', ('L2a',), 2015),
    ]
    return Catalog(entries, revisions)


# Early X with unique region evidence, early X without region, X collected after
# the catalog re-assigned X, an unknown code and an empty code:
ROWS = [
    {'id': 'lang-r1', 'code': 'X', 'year': 1995, 'region': 'R1'},
    {'id': 'lang-r2', 'code': 'X', 'year': 1995, 'region': 'R2'},
    {'id': 'lang-noreg', 'code': 'X', 'year': 1995, 'region': None},
    {'id': 'lang-reused', 'code': 'X', 'year': 2018, 'region': None},
    {'id': 'lang-unknown', 'code': 'ZZZ', 'year': 1995, 'region': None},
    {'id': 'lang-empty', 'code': '', 'year': 1995, 'region': None},
]


def resolve_rows(catalog, rows, target):
    return resolve(
        catalog, rows, target,
        get_code=lambda row: row['code'],
        get_year=lambda row: row['year'],
        get_region=lambda row: row['region'])


def test_entry_for_code_at_collection_year(catalog):
    assert catalog.entry_for_code('X', 1990).identity == 'L0'
    # X has been re-assigned by the catalog in the meantime:
    assert catalog.entry_for_code('X', 2018).identity == 'L2a'
    assert catalog.entry_for_code('ZZZ', 1990) is None


def test_resolve_with_unique_region_evidence(catalog):
    res = resolve_rows(catalog, ROWS, 2018)['lang-r1']
    assert res.status == STATUS_RESOLVED
    assert res.collection_entry == 'L0'
    assert [(s.kind, s.predecessor, s.successors, s.version) for s in res.path] == [
        ('split', 'L0', ('L1',), 2000), ('rename', 'L1', ('L1',), 2005)]
    assert [c.identity for c in res.candidates] == ['L1']
    assert res.code == 'A1'


def test_resolve_region_supports_first_layer_only(catalog):
    res = resolve_rows(catalog, ROWS, 2018)['lang-r2']
    assert res.status == STATUS_AMBIGUOUS
    assert res.collection_entry == 'L0'
    # Region R2 selects L2 at the first split, but cannot decide the second one:
    assert [(s.kind, s.predecessor, s.successors) for s in res.path] == [
        ('split', 'L0', ('L2',)), ('split', 'L2', ('L2a', 'L2b')),
        ('rename', 'L2a', ('L2a',))]
    assert [c.identity for c in res.candidates] == ['L2a', 'L2b']
    assert res.code is None


def test_resolve_without_region(catalog):
    res = resolve_rows(catalog, ROWS, 2018)['lang-noreg']
    assert res.status == STATUS_AMBIGUOUS
    assert res.collection_entry == 'L0'
    assert [c.identity for c in res.candidates] == ['L1', 'L2a', 'L2b']


def test_resolve_code_collected_after_reassignment(catalog):
    res = resolve_rows(catalog, ROWS, 2018)['lang-reused']
    assert res.status == STATUS_RESOLVED
    # Identified as the *new* owner of code X, not as the historical L0:
    assert res.collection_entry == 'L2a'
    assert res.code == 'X'


def test_identities_are_not_merged_by_code_or_name(catalog):
    res = resolve_rows(catalog, ROWS, 2018)
    # L2a carries code X and the name "Language One New", but stays distinct
    # from L0 (same code) and L1 (same name):
    assert res['lang-reused'].collection_entry == 'L2a'
    candidates = {c.identity for c in res['lang-noreg'].candidates}
    assert candidates == {'L1', 'L2a', 'L2b'}
    names = {c.identity: c.name for c in res['lang-noreg'].candidates}
    assert names['L1'] == names['L2a'] == 'Language One New'


def test_unknown_and_missing_code_are_distinguished(catalog):
    res = resolve_rows(catalog, ROWS, 2018)
    assert res['lang-unknown'].status == STATUS_UNKNOWN_CODE
    assert res['lang-unknown'].collection_entry is None
    assert res['lang-empty'].status == STATUS_MISSING_CODE
    assert res['lang-empty'].candidates == []
    assert res['lang-r2'].status == STATUS_AMBIGUOUS


def test_input_order_does_not_matter(catalog):
    reference = resolve_rows(catalog, ROWS, 2018)
    for swapped in [list(reversed(ROWS)), [ROWS[i] for i in (2, 5, 0, 4, 1, 3)]]:
        assert resolve_rows(catalog, swapped, 2018) == reference


def test_results_follow_the_target_version(catalog):
    res = resolve_rows(catalog, ROWS, 2005)
    # The second split of L2 is not yet in effect at the target version:
    assert res['lang-r2'].status == STATUS_RESOLVED
    assert res['lang-r2'].code == 'A2'
    assert [(s.kind, s.predecessor) for s in res['lang-r2'].path] == [('split', 'L0')]
    assert {c.identity for c in res['lang-noreg'].candidates} == {'L1', 'L2'}
    # The rename of L1 is in effect at 2005, but not at 2000:
    assert res['lang-r1'].candidates[0].name == 'Language One New'
    assert resolve_rows(catalog, ROWS, 2000)['lang-r1'].candidates[0].name == 'Language One'
    # Results are still keyed by the original language rows:
    assert set(resolve_rows(catalog, ROWS, 2005)) == {row['id'] for row in ROWS}


@pytest.fixture
def wordlist(tmp_path):
    ds = Wordlist.in_dir(tmp_path / 'src')
    ds.add_component('LanguageTable')
    ds.add_component('ParameterTable')
    ds.add_columns(
        'LanguageTable',
        {'name': 'Code', 'dc:description': 'Originally recorded catalog code.'},
        {'name': 'Year', 'datatype': 'integer'},
        {'name': 'Region'})
    ds.write(
        LanguageTable=[
            {'ID': row['id'], 'Name': 'Name of ' + row['id'], 'Code': row['code'],
             'Year': row['year'], 'Region': row['region'] or ''}
            for row in ROWS],
        ParameterTable=[{'ID': 'p1', 'Name': 'hand'}],
        FormTable=[
            {'ID': 'f-' + row['id'], 'Language_ID': row['id'], 'Parameter_ID': 'p1',
             'Form': 'form of ' + row['id'], 'Source': []}
            for row in ROWS])
    return ds


def test_augment_wordlist(wordlist, catalog, tmp_path):
    src_forms = (wordlist.directory / 'forms.csv').read_bytes()
    outdir = tmp_path / 'out'
    results = augment_wordlist(
        wordlist, outdir, catalog, 2018,
        code_column='Code', year_column='Year', region_column='Region')
    assert set(results) == {row['id'] for row in ROWS}
    assert results['lang-r1'].code == 'A1'

    # Re-open the output and check the augmented LanguageTable:
    md = list(outdir.glob('*metadata.json'))[0]
    out = Dataset.from_metadata(md)
    assert (out.directory / 'forms.csv').read_bytes() == src_forms
    # Foreign keys from FormTable to LanguageTable are untouched:
    fks = out['FormTable'].tableSchema.foreignKeys
    assert any(fk.columnReference == ['Language_ID'] for fk in fks)
    forms = list(out['FormTable'].iterdicts())
    assert {f['Language_ID'] for f in forms} == {row['id'] for row in ROWS}

    languages = {row['ID']: row for row in out['LanguageTable'].iterdicts()}
    # Original values are preserved and can be reviewed:
    assert languages['lang-r1']['Code'] == 'X'
    assert languages['lang-r1']['Year'] == 1995
    # Standard results are kept in separate columns:
    assert languages['lang-r1']['Collection_Entry'] == 'L0'
    assert languages['lang-r1']['Trace'] == 'split:L0>L1@2000; rename:L1>L1@2005'
    assert languages['lang-r1']['Standard_Code'] == 'A1'
    assert languages['lang-r1']['Standard_Candidates'] == 'L1:A1'
    assert languages['lang-r1']['Resolution'] == STATUS_RESOLVED
    assert not languages['lang-r2']['Standard_Code']
    assert languages['lang-r2']['Standard_Candidates'] == 'L2a:X;L2b:A4'
    assert languages['lang-r2']['Resolution'] == STATUS_AMBIGUOUS
    assert languages['lang-noreg']['Standard_Candidates'] == 'L1:A1;L2a:X;L2b:A4'
    assert languages['lang-reused']['Collection_Entry'] == 'L2a'
    assert languages['lang-reused']['Standard_Code'] == 'X'
    assert languages['lang-unknown']['Resolution'] == STATUS_UNKNOWN_CODE
    assert languages['lang-empty']['Resolution'] == STATUS_MISSING_CODE
    out.validate()


def test_augment_wordlist_results_reference_original_rows(wordlist, catalog, tmp_path):
    # Swapping the input row order twice must not change the per-language results:
    src = wordlist.directory / 'languages.csv'
    lines = src.read_text(encoding='utf-8').splitlines(keepends=True)
    header, body = lines[0], lines[1:]
    for swapped in [list(reversed(body)), [body[i] for i in (2, 5, 0, 4, 1, 3)]]:
        src.write_text(header + ''.join(swapped), encoding='utf-8')
        results = augment_wordlist(
            wordlist, tmp_path / 'out', catalog, 2005,
            code_column='Code', year_column='Year', region_column='Region')
        assert results['lang-r2'].code == 'A2'  # follows the target version
        assert results['lang-r1'].candidates[0].name == 'Language One New'
        assert set(results) == {row['id'] for row in ROWS}
