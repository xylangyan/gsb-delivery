"""
Tests for single-pass, batch-oriented export with foreign-key resolution
(`pycldf.streaming`).

The fixture dataset encodes the scenario from the specification:

* `LanguageTable` uses a local primary key column `PKey`; the CLDF id column
  keeps its own, separate key domain.
* `FormTable.Language_ID` references that local primary key, while
  `ParameterTable.Language_ID` references the CLDF id of the same table.
* The first `LanguageTable` row `L` is reachable via both paths, another row
  `K` is shared between batches, and a later row repeats `L`'s local primary
  key under a different CLDF id.
* One form explicitly points at a non-existent key, another carries an empty
  language reference, and one form's parameter only appears later in the
  parameter file.
"""
import collections

import pytest

from pycldf import Wordlist
from pycldf.terms import term_uri
from pycldf.streaming import BatchExporter, RefStatus


def _make_dataset(tmp_path, *, duplicate_pk=True):
    ds = Wordlist.in_dir(tmp_path)
    ds.add_component('ParameterTable')
    ds.add_component('LanguageTable')
    ds.add_columns('LanguageTable', {'name': 'PKey'})
    ds['LanguageTable'].tableSchema.primaryKey = ['PKey']
    ds.add_columns(
        'ParameterTable',
        {'name': 'Language_ID', 'propertyUrl': term_uri('languageReference')})
    for fk in ds['FormTable'].tableSchema.foreignKeys:
        if fk.columnReference == ['Language_ID']:
            fk.reference.columnReference = ['PKey']
    # A blank language reference must survive strict reading for the empty case.
    ds['FormTable', 'Language_ID'].required = False

    languages = [
        dict(ID='L-id', PKey='L', Name='Lang L'),
        dict(ID='K-id', PKey='K', Name='Lang K'),
    ]
    if duplicate_pk:
        # Local primary key "L" repeats, but the CLDF id stays unique.
        languages.append(dict(ID='L-other', PKey='L', Name='Lang L duplicate'))

    ds.write(
        LanguageTable=languages,
        ParameterTable=[
            dict(ID='p1', Language_ID='L-id', Name='one'),
            dict(ID='p2', Language_ID='L-id', Name='two'),
            dict(ID='pLate', Language_ID='K-id', Name='late'),
        ],
        FormTable=[
            dict(ID='f1', Language_ID='L', Parameter_ID='p1', Form='a'),
            dict(ID='f2', Language_ID='L', Parameter_ID='p1', Form='b'),
            dict(ID='f3', Language_ID='K', Parameter_ID='pLate', Form='c'),
            dict(ID='f4', Language_ID='nope', Parameter_ID='p1', Form='d'),
            dict(ID='f5', Language_ID='', Parameter_ID='p1', Form='e'),
        ])
    # Note: the data intentionally contains duplicate/missing keys, so strict
    # CLDF validation would reject it; the exporter reads non-strict instead.
    return ds


def _count_file_opens(ds):
    """Make each table file fail if it is opened for iteration more than once."""
    opens = collections.Counter()
    for table in ds.tables:
        component = ds.get_tabletype(table)
        original = table.iterdicts

        def guarded(original=original, component=component, table=table):
            def wrapper(**kwargs):
                opens[component] += 1
                assert opens[component] == 1, \
                    f'{component} was read more than once (single sequential pass required)'
                return original(**kwargs)
            return wrapper

        table.iterdicts = guarded()
    return opens


def _ref(batch, form_id, relation):
    record = next(r for r in batch if r.id == form_id)
    return record.references[relation]


def test_first_batch_reads_only_what_locates_its_references(tmp_path):
    ds = _make_dataset(tmp_path)
    exporter = BatchExporter(ds)
    opens = _count_file_opens(ds)

    batch = exporter.export_batch('FormTable', ['f1', 'f2'])

    # The source file stops at the two requested rows; each target stops at the
    # first matching row.
    assert exporter.rows_read('FormTable') == 2
    assert exporter.rows_read('LanguageTable') == 1
    assert exporter.rows_read('ParameterTable') == 1
    assert not exporter.is_exhausted('LanguageTable')

    # Both paths (form -> language via local PK, parameter -> language via
    # CLDF id) point at the same row L, but are only "unique so far".
    for form_id in ('f1', 'f2'):
        lang_ref = _ref(batch, form_id, 'languageReference')
        assert lang_ref.status == RefStatus.UNIQUE
        assert lang_ref.key_domain == 'pk'
        assert lang_ref.target.id == 'L-id'

        param_ref = _ref(batch, form_id, 'parameterReference')
        assert param_ref.status == RefStatus.UNIQUE
        param_lang = exporter.reference(param_ref.target, 'languageReference')
        assert param_lang.status == RefStatus.UNIQUE
        assert param_lang.key_domain == 'id'
        assert param_lang.target.id == 'L-id'

    assert exporter.conflict_reports() == []
    assert opens  # the counters are armed and triggered during the batch


def test_duplicate_local_key_invalidates_prior_pk_refs_but_keeps_id_refs(tmp_path):
    ds = _make_dataset(tmp_path)
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    first = exporter.export_batch('FormTable', ['f1', 'f2'])
    first_pk_refs = [_ref(first, fid, 'languageReference') for fid in ('f1', 'f2')]
    first_param = _ref(first, 'f1', 'parameterReference')
    first_id_ref = exporter.reference(first_param.target, 'languageReference')

    # Second batch: f3's parameter is read later; f4 forces the language file
    # to be exhausted, which surfaces the duplicate local primary key.
    second = exporter.export_batch('FormTable', ['f3', 'f4'])

    # f3 locates K (shared, unaffected) via both key domains.
    f3_lang = _ref(second, 'f3', 'languageReference')
    assert f3_lang.target.id == 'K-id'
    f3_param = _ref(second, 'f3', 'parameterReference')
    assert f3_param.target.id == 'pLate'
    k_id_ref = exporter.reference(f3_param.target, 'languageReference')
    assert k_id_ref.key_domain == 'id'
    assert k_id_ref.target.id == 'K-id'

    # Explicitly missing key is reported, not confused with the conflict.
    missing = _ref(second, 'f4', 'languageReference')
    assert missing.status == RefStatus.NOT_FOUND
    assert missing.target is None

    # The conflict report covers both earlier and current references reached
    # via the local primary key, with full source paths.
    reports = exporter.conflict_reports()
    assert len(reports) == 1
    report = reports[0]
    assert report.component == 'LanguageTable'
    assert report.key_domain == 'pk'
    assert report.key == 'L'
    assert [c.id for c in report.candidates] == ['L-id', 'L-other']
    assert report.source_paths == [
        'FormTable/f1#languageReference',
        'FormTable/f2#languageReference',
    ]
    for ref in first_pk_refs:
        assert ref.status == RefStatus.CONFLICTED
        assert ref.target is None
        assert [c.id for c in ref.candidates] == ['L-id', 'L-other']

    # The CLDF-id path to L and the unrelated K target remain usable. The
    # language file has been exhausted (via f4), so they are already confirmed.
    assert first_id_ref.status == RefStatus.CONFIRMED
    assert first_id_ref.target.id == 'L-id'
    assert f3_lang.status == RefStatus.CONFIRMED
    assert k_id_ref.target.id == 'K-id'

    result = exporter.finalize()

    # After full reading only the CLDF-id reference to L is confirmable.
    assert first_id_ref.status == RefStatus.CONFIRMED
    assert k_id_ref.status == RefStatus.CONFIRMED
    assert f3_lang.status == RefStatus.CONFIRMED
    assert all(ref.status == RefStatus.CONFLICTED for ref in first_pk_refs)
    statuses = {ref.status for ref in result.references}
    assert RefStatus.CONFIRMED in statuses
    assert RefStatus.CONFLICTED in statuses
    assert RefStatus.NOT_FOUND in statuses


def test_empty_reference_never_touches_target_file(tmp_path):
    ds = _make_dataset(tmp_path)
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    batch = exporter.export_batch('FormTable', ['f5'])
    empty = _ref(batch, 'f5', 'languageReference')

    assert empty.status == RefStatus.EMPTY
    assert empty.target is None
    assert exporter.rows_read('LanguageTable') == 0

    # The parameter reference still works, and finalization keeps the empty
    # reference empty without ever opening the language file.
    result = exporter.finalize()
    assert empty.status == RefStatus.EMPTY
    assert exporter.rows_read('LanguageTable') == 0
    assert all(ref.status in (RefStatus.CONFIRMED, RefStatus.EMPTY)
               for ref in result.references)


def test_confirmed_results_match_regular_orm_queries_without_conflicts(tmp_path):
    ds = _make_dataset(tmp_path, duplicate_pk=False)
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    exporter.export_batch('FormTable', ['f1', 'f2'])
    exporter.export_batch('FormTable', ['f3', 'f4', 'f5'])
    result = exporter.finalize()

    # Regular ORM access needs its own dataset instance: the exporter's
    # single-pass guard forbids reopening any table file.
    fresh = Wordlist.from_metadata(tmp_path / 'Wordlist-metadata.json')
    orm_forms = {form.id: form for form in fresh.objects('FormTable')}
    by_path = {(ref.source_id, ref.relation): ref for ref in result.references}

    for form_id in ('f1', 'f2', 'f3'):
        ref = by_path[(form_id, 'languageReference')]
        assert ref.status == RefStatus.CONFIRMED
        assert ref.target.id == orm_forms[form_id].language.id
        param_ref = by_path[(form_id, 'parameterReference')]
        assert param_ref.status == RefStatus.CONFIRMED
        assert param_ref.target.id == orm_forms[form_id].parameter.id

    assert by_path[('f4', 'languageReference')].status == RefStatus.NOT_FOUND
    assert by_path[('f5', 'languageReference')].status == RefStatus.EMPTY

    # Parameter-level language provenance resolves exactly like the ORM.
    for param in fresh.objects('ParameterTable'):
        matching = [ref for ref in result.references
                    if ref.source_component == 'ParameterTable'
                    and ref.source_id == param.id]
        expected = param.related('languageReference')
        for ref in matching:
            assert ref.status == RefStatus.CONFIRMED
            assert ref.target.id == expected.id


def test_batches_reuse_already_read_targets_across_key_domains(tmp_path):
    ds = _make_dataset(tmp_path)
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    first = exporter.export_batch('FormTable', ['f1'])
    assert exporter.rows_read('LanguageTable') == 1

    # Establish a reference via the separate CLDF-id domain (parameter
    # provenance), which must stay unaffected by the primary-key conflict.
    id_ref = exporter.reference(
        first[0].references['parameterReference'].target, 'languageReference')
    assert id_ref.key_domain == 'id'

    # A conflict in the "L" primary-key domain must not discard row K nor the
    # separate CLDF-id index built from the same file.
    second = exporter.export_batch('FormTable', ['f3', 'f4'])
    k_ref = _ref(second, 'f3', 'languageReference')
    assert k_ref.target.id == 'K-id'

    reports = exporter.conflict_reports()
    assert len(reports) == 1
    assert {(report.component, report.key_domain) for report in reports} == \
        {('LanguageTable', 'pk')}

    exporter.finalize()
    assert k_ref.status == RefStatus.CONFIRMED
    # The id-domain index never saw a duplicate key.
    id_refs = [ref for ref in exporter.all_refs
               if ref.target_component == 'LanguageTable' and ref.key_domain == 'id']
    assert id_refs
    assert all(ref.status == RefStatus.CONFIRMED for ref in id_refs)


def test_unknown_batch_id_raises_after_draining_source(tmp_path):
    ds = _make_dataset(tmp_path, duplicate_pk=False)
    exporter = BatchExporter(ds)
    with pytest.raises(KeyError):
        exporter.export_batch('FormTable', ['does-not-exist'])
    assert exporter.is_exhausted('FormTable')


def test_duplicate_clidf_id_conflicts_only_the_id_domain(tmp_path):
    ds = Wordlist.in_dir(tmp_path)
    ds.add_component('ParameterTable')
    ds.add_component('LanguageTable')
    ds.add_columns(
        'ParameterTable',
        {'name': 'Language_ID', 'propertyUrl': term_uri('languageReference')})
    ds.write(
        LanguageTable=[
            dict(ID='dup', Name='first'),
            dict(ID='other', Name='second'),
            dict(ID='dup', Name='third'),
        ],
        ParameterTable=[
            dict(ID='p1', Language_ID='dup', Name='one'),
            dict(ID='p2', Language_ID='other', Name='two'),
        ],
        FormTable=[
            dict(ID='f1', Language_ID='l1', Parameter_ID='p1', Form='a'),
            dict(ID='f2', Language_ID='l2', Parameter_ID='p2', Form='b'),
        ])
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    batch = exporter.export_batch('FormTable', ['f1', 'f2'])
    p1 = next(r for r in batch if r.id == 'f1').references['parameterReference'].target
    dup_ref = exporter.reference(p1, 'languageReference')
    p2 = next(r for r in batch if r.id == 'f2').references['parameterReference'].target
    other_ref = exporter.reference(p2, 'languageReference')

    assert dup_ref.key_domain == 'id'
    assert dup_ref.status == RefStatus.CONFLICTED
    assert [c.id for c in dup_ref.candidates] == ['dup', 'dup']

    # With the default schema the ID column is also the primary key, so the
    # duplicate shows up in both domains; we only check the id-domain report.
    report, = [r for r in exporter.conflict_reports() if r.key_domain == 'id']
    assert report.key == 'dup'
    assert report.source_paths == ['ParameterTable/p1#languageReference']

    exporter.finalize()
    assert dup_ref.status == RefStatus.CONFLICTED
    assert other_ref.status == RefStatus.CONFIRMED
    assert other_ref.target.id == 'other'


def test_list_valued_reference_preserves_order_and_settles_on_finalize(tmp_path):
    ds = Wordlist.in_dir(tmp_path)
    ds.add_component('ParameterTable')
    ds['FormTable', 'Parameter_ID'].separator = ';'
    ds.write(
        ParameterTable=[
            dict(ID='a', Name='A'), dict(ID='b', Name='B'), dict(ID='c', Name='C')],
        FormTable=[
            dict(ID='f', Language_ID='l', Parameter_ID=['c', 'a', 'c'], Form='x')])
    exporter = BatchExporter(ds)
    _count_file_opens(ds)

    batch = exporter.export_batch('FormTable', ['f'], relations=('parameterReference',))
    refs = batch[0].references['parameterReference']
    assert [ref.key_value for ref in refs] == ['c', 'a', 'c']
    assert all(ref.status == RefStatus.UNIQUE for ref in refs)

    exporter.finalize()
    assert [ref.target.id for ref in refs] == ['c', 'a', 'c']
    assert all(ref.status == RefStatus.CONFIRMED for ref in refs)
