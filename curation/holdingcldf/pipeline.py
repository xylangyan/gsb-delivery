"""Two-stage archival pipeline.

Stage 1 (:func:`build_edition_one`) merges the supplier batches into a readable
CLDF Wordlist whose BibTeX keys are edition-1 official catalogue codes.

Stage 2 (:func:`rearchive_edition_two`) takes that finished artifact plus a raw
supplier package that still uses supplier-local codes and re-archives
everything under catalogue edition 2.

All reference validation happens before any file is written; output is assembled
in a staging directory and swapped into place atomically, so a failure can never
leave a half-updated artifact.
"""

import csv
import json
import pathlib
import shutil
import tempfile

from pycldf.sources import Source, Sources

from curation.holdingcldf import references as refs
from curation.holdingcldf.catalog import (
    BatchManifest,
    BatchMapping,
    HoldingCatalog,
)
from curation.holdingcldf import cldf_builder as builder
from curation.holdingcldf.errors import ArchiveError, RefProblem
from curation.holdingcldf.provenance import (
    EDITION_ONE,
    EDITION_TWO,
    build_edition_one_manifest,
    build_edition_two_manifest,
    expected_manifest,
    write_handoff,
    write_manifest,
)

STAGE_ONE = 'edition-1 build'
STAGE_TWO = 'edition-2 rearchive'


def _read_tsv(path: pathlib.Path) -> list[dict]:
    with pathlib.Path(path).open(encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle, delimiter='\t'))


def _split_words(value: str | None) -> list[str] | None:
    if value is None or value == '':
        return None
    return [part.strip() for part in value.split('|')]


def _load_source_library(path: pathlib.Path) -> dict[str, dict]:
    library: dict[str, dict] = {}
    for row in _read_tsv(path):
        library[row['holding_id'].strip()] = {
            'genre': row['genre'].strip(),
            'author': row['author'].strip(),
            'title': row['title'].strip(),
            'year': row['year'].strip(),
        }
    return library


def _resolve_local(
    *,
    local_code: str,
    mapping: BatchMapping,
    catalog: HoldingCatalog,
) -> tuple[str | None, str | None, list[str]]:
    """Return ``(holding, official, candidates)``; empty holding means failure."""
    candidates = list(mapping.candidates(local_code))
    if len(candidates) != 1:
        return None, None, candidates
    holding = candidates[0]
    official = catalog.official_id(holding)
    if official is None:
        return holding, None, candidates
    return holding, official, candidates


def _check_and_rewrite_stage1(
    *,
    batch_id: str,
    table: str,
    column: str,
    rows: list[dict],
    mapping: BatchMapping,
    catalog: HoldingCatalog,
    problems: list[RefProblem],
) -> dict[str, list[str]]:
    """Validate every citation and return row ID -> rewritten citation list."""
    rewritten: dict[str, list[str]] = {}
    for row_number, row in enumerate(rows, start=1):
        row_id = row.get('ID')
        new_refs: list[str] = []
        for raw_ref in refs.split_refs(row.get(column)):
            local_code, description = refs.parse_ref(raw_ref)
            holding, official, candidates = _resolve_local(
                local_code=local_code, mapping=mapping, catalog=catalog)
            if holding is None:
                if candidates:
                    problems.append(RefProblem(
                        kind='ambiguous_target', batch=batch_id, table=table,
                        column=column, row_number=row_number, row_id=row_id,
                        raw_ref=raw_ref,
                        detail=(
                            f'local code {local_code!r} maps to {len(candidates)} '
                            'holdings in this batch mapping'
                        ),
                        candidates=candidates))
                else:
                    problems.append(RefProblem(
                        kind='missing_target', batch=batch_id, table=table,
                        column=column, row_number=row_number, row_id=row_id,
                        raw_ref=raw_ref,
                        detail=(
                            f'local code {local_code!r} is absent from this batch '
                            'mapping table'
                        )))
                continue
            if official is None:
                problems.append(RefProblem(
                    kind='missing_target', batch=batch_id, table=table,
                    column=column, row_number=row_number, row_id=row_id,
                    raw_ref=raw_ref,
                    detail=(
                        f'holding {holding!r} resolved from local code '
                        f'{local_code!r} has no official code in catalogue '
                        f'edition {catalog.edition!r}'
                    )))
                continue
            new_refs.append(refs.render_ref(official, description))
        rewritten[row_id] = new_refs
    return rewritten


def _form_rows(batch_id: str, raw_rows: list[dict],
               rewritten: dict[str, list[str]]) -> list[dict]:
    out = []
    for row in raw_rows:
        out.append({
            'ID': f'{batch_id}-{row["ID"]}',
            'Language_ID': row['Language_ID'],
            'Parameter_ID': row['Parameter_ID'],
            'Form': row['Form'],
            'Comment': row.get('Comment'),
            'Batch_ID': batch_id,
            builder.FORM_REF_COLUMN: rewritten[row['ID']],
        })
    return out


def _example_rows(batch_id: str, raw_rows: list[dict],
                  rewritten: dict[str, list[str]]) -> list[dict]:
    out = []
    for row in raw_rows:
        out.append({
            'ID': f'{batch_id}-{row["ID"]}',
            'Language_ID': row['Language_ID'],
            'Primary_Text': row['Primary_Text'],
            'Analyzed_Word': _split_words(row.get('Analyzed_Word')),
            'Gloss': _split_words(row.get('Gloss')),
            'Translated_Text': row.get('Translated_Text'),
            'Comment': row.get('Comment'),
            'Batch_ID': batch_id,
            builder.EXAMPLE_REF_COLUMN: rewritten[row['ID']],
        })
    return out


def _holdings_view(catalog: HoldingCatalog, holdings: set[str]) -> dict[str, dict]:
    return {
        holding: {f'{catalog.edition}_official': catalog.official_id(holding)}
        for holding in sorted(holdings)
    }


def _stage1_sources(holdings: set[str], catalog: HoldingCatalog,
                    library: dict[str, dict]) -> tuple[Sources, dict[str, str]]:
    sources = Sources()
    holding_to_key: dict[str, str] = {}
    for holding in sorted(holdings):
        official = catalog.official_id(holding)
        fields = dict(library[holding])
        sources.add(Source(fields.pop('genre'), official, **fields))
        holding_to_key[holding] = official
    return sources, holding_to_key


def _verify_readback(artifact_dir: pathlib.Path,
                     expected: dict[str, list[list[str]]],
                     expected_source_keys: set[str]) -> None:
    cells = builder.read_back_citations(artifact_dir)
    for table_url, expected_cells in expected.items():
        actual_cells = cells.get(table_url)
        if actual_cells is None:
            raise AssertionError(f'read-back: table {table_url} has no source column')
        if actual_cells != expected_cells:
            raise AssertionError(
                f'read-back mismatch in {table_url}:\n'
                f'expected={expected_cells}\nactual={actual_cells}')
    sources = builder.read_back_sources(artifact_dir)
    actual_keys = set(sources.keys())
    if actual_keys != expected_source_keys:
        raise AssertionError(
            f'BibTeX key set mismatch: expected={sorted(expected_source_keys)} '
            f'actual={sorted(actual_keys)}')
    if len(actual_keys) != len(expected_source_keys):
        raise AssertionError('BibTeX must contain exactly one entry per holding')


def _atomic_swap(staging: pathlib.Path, output_dir: pathlib.Path) -> None:
    output_dir = pathlib.Path(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(output_dir))


def build_edition_one(
    *,
    batch_dirs: list[pathlib.Path],
    catalog_dir: pathlib.Path,
    source_library: pathlib.Path,
    output_dir: pathlib.Path,
) -> pathlib.Path:
    catalog = HoldingCatalog.from_dir(catalog_dir)
    library = _load_source_library(source_library)
    problems: list[RefProblem] = []
    all_forms: list[dict] = []
    all_examples: list[dict] = []
    batch_records: list[dict] = []
    citation_columns = [
        {'table': builder.FORM_TABLE, 'url': 'forms.csv',
         'column': builder.FORM_REF_COLUMN, 'role': 'word-form citations'},
        {'table': builder.EXAMPLE_TABLE, 'url': 'examples.csv',
         'column': builder.EXAMPLE_REF_COLUMN, 'role': 'example-sentence citations'},
    ]
    touched_holdings: set[str] = set()
    expected_cells: dict[str, list[list[str]]] = {'forms.csv': [], 'examples.csv': []}

    for batch_dir in batch_dirs:
        batch_dir = pathlib.Path(batch_dir)
        manifest = BatchManifest.from_dir(batch_dir)
        mapping = BatchMapping.from_file(
            manifest.batch_id, batch_dir / 'mapping.tsv')
        form_rows = _read_tsv(batch_dir / manifest.forms_file)
        example_rows = _read_tsv(batch_dir / manifest.examples_file)

        form_rewritten = _check_and_rewrite_stage1(
            batch_id=manifest.batch_id, table=builder.FORM_TABLE,
            column=manifest.form_ref_column, rows=form_rows, mapping=mapping,
            catalog=catalog, problems=problems)
        example_rewritten = _check_and_rewrite_stage1(
            batch_id=manifest.batch_id, table=builder.EXAMPLE_TABLE,
            column=manifest.example_ref_column, rows=example_rows, mapping=mapping,
            catalog=catalog, problems=problems)

        for row in form_rows:
            for raw_ref in refs.split_refs(row.get(manifest.form_ref_column)):
                for holding in mapping.candidates(refs.parse_ref(raw_ref)[0]):
                    touched_holdings.add(holding)
        for row in example_rows:
            for raw_ref in refs.split_refs(row.get(manifest.example_ref_column)):
                for holding in mapping.candidates(refs.parse_ref(raw_ref)[0]):
                    touched_holdings.add(holding)

        batch_records.append({
            'batch_id': manifest.batch_id,
            'mapping_file': f'{manifest.batch_id}/mapping.tsv',
            'forms_file': manifest.forms_file,
            'examples_file': manifest.examples_file,
            'form_ref_column': manifest.form_ref_column,
            'example_ref_column': manifest.example_ref_column,
        })
        all_forms.extend(
            _form_rows(manifest.batch_id, form_rows, form_rewritten))
        all_examples.extend(
            _example_rows(manifest.batch_id, example_rows, example_rewritten))
        expected_cells['forms.csv'].extend(form_rewritten.values())
        expected_cells['examples.csv'].extend(example_rewritten.values())

    if problems:
        raise ArchiveError(problems, STAGE_ONE)

    missing_library = sorted(touched_holdings - set(library))
    if missing_library:
        raise ArchiveError([RefProblem(
            kind='missing_target', batch='<source-library>', table='sources.bib',
            column='<bibliographic record>', row_number=0, row_id=None,
            raw_ref=holding,
            detail='no bibliographic record registered for holding')], STAGE_ONE)

    sources, _ = _stage1_sources(touched_holdings, catalog, library)
    output_dir = pathlib.Path(output_dir)
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as tmp:
        staging = pathlib.Path(tmp) / 'artifact'
        staging.mkdir()
        builder.build_wordlist(staging, all_forms, all_examples, sources)
        expected_keys = {
            catalog.official_id(holding) for holding in touched_holdings}
        _verify_readback(staging, expected_cells, expected_keys)
        archive_manifest = build_edition_one_manifest(
            catalog=catalog,
            batch_records=batch_records,
            citation_columns=citation_columns,
            holdings_view=_holdings_view(catalog, touched_holdings),
            bib_file='sources.bib')
        write_manifest(staging, archive_manifest)
        write_handoff(staging, archive_manifest)
        _atomic_swap(staging, output_dir)
    return output_dir


def _read_prior_artifact(artifact_dir: pathlib.Path) -> dict[str, list[dict]]:
    """Read the finished edition-1 CLDF tables through pycldf (real columns)."""
    from pycldf import Wordlist
    dataset = Wordlist.from_metadata(pathlib.Path(artifact_dir) / builder.METADATA_NAME)
    tables: dict[str, list[dict]] = {}
    for table_url, column_name in builder.source_columns(dataset):
        table = dataset[table_url]
        column_names = [column.name for column in table.tableSchema.columns]
        rows = []
        for row in dataset.iter_rows(table_url):
            rows.append({name: row.get(name) for name in column_names})
        tables[table_url] = rows
    return tables


def _rewrite_prior_rows(
    *,
    rows: list[dict],
    column: str,
    v1_to_holding: dict[str, str],
    catalog: HoldingCatalog,
    batch_label: str,
    table: str,
    problems: list[RefProblem],
) -> list[list[str]]:
    cells: list[list[str]] = []
    for row_number, row in enumerate(rows, start=1):
        new_refs: list[str] = []
        for raw_ref in refs.split_refs(row.get(column)):
            v1_key, description = refs.parse_ref(raw_ref)
            holding = v1_to_holding.get(v1_key)
            if holding is None:
                problems.append(RefProblem(
                    kind='missing_target', batch=batch_label, table=table,
                    column=column, row_number=row_number, row_id=row.get('ID'),
                    raw_ref=raw_ref,
                    detail=(
                        f'citation key {v1_key!r} is not an edition-1 official '
                        'code recorded in the prior artifact manifest'
                    )))
                continue
            official = catalog.official_id(holding)
            if official is None:
                problems.append(RefProblem(
                    kind='missing_target', batch=batch_label, table=table,
                    column=column, row_number=row_number, row_id=row.get('ID'),
                    raw_ref=raw_ref,
                    detail=(
                        f'holding {holding!r} has no official code in catalogue '
                        f'edition {catalog.edition!r}'
                    )))
                continue
            new_refs.append(refs.render_ref(official, description))
        cells.append(new_refs)
    return cells


def rearchive_edition_two(
    *,
    edition_one_dir: pathlib.Path,
    supplement_dir: pathlib.Path,
    catalog_dir: pathlib.Path,
    source_library: pathlib.Path,
    output_dir: pathlib.Path,
) -> pathlib.Path:
    edition_one_dir = pathlib.Path(edition_one_dir)
    prior_manifest = expected_manifest(edition_one_dir, EDITION_ONE)
    catalog = HoldingCatalog.from_dir(catalog_dir)
    library = _load_source_library(source_library)
    problems: list[RefProblem] = []

    v1_to_holding = {
        info['edition-1_official']: holding
        for holding, info in prior_manifest['holdings'].items()
    }
    prior_tables = _read_prior_artifact(edition_one_dir)
    prior_forms = prior_tables['forms.csv']
    prior_examples = prior_tables['examples.csv']

    prior_form_cells = _rewrite_prior_rows(
        rows=prior_forms, column=builder.FORM_REF_COLUMN,
        v1_to_holding=v1_to_holding, catalog=catalog,
        batch_label='edition-1-artifact', table=builder.FORM_TABLE, problems=problems)
    prior_example_cells = _rewrite_prior_rows(
        rows=prior_examples, column=builder.EXAMPLE_REF_COLUMN,
        v1_to_holding=v1_to_holding, catalog=catalog,
        batch_label='edition-1-artifact', table=builder.EXAMPLE_TABLE, problems=problems)

    supplement_manifest = json.loads(
        (pathlib.Path(supplement_dir) / 'supplement_manifest.json').read_text('utf-8'))
    package_id = supplement_manifest['package_id']
    batch_id = supplement_manifest['batch_id']
    mapping = BatchMapping.from_file(
        batch_id, pathlib.Path(supplement_dir) / supplement_manifest['mapping_file'])
    supp_form_rows = _read_tsv(
        pathlib.Path(supplement_dir) / supplement_manifest['forms_file'])
    supp_example_rows = _read_tsv(
        pathlib.Path(supplement_dir) / supplement_manifest['examples_file'])
    supp_form_rewritten = _check_and_rewrite_stage1(
        batch_id=f'{package_id} (batch {batch_id})', table=builder.FORM_TABLE,
        column=supplement_manifest['form_ref_column'], rows=supp_form_rows,
        mapping=mapping, catalog=catalog, problems=problems)
    supp_example_rewritten = _check_and_rewrite_stage1(
        batch_id=f'{package_id} (batch {batch_id})', table=builder.EXAMPLE_TABLE,
        column=supplement_manifest['example_ref_column'], rows=supp_example_rows,
        mapping=mapping, catalog=catalog, problems=problems)

    touched_holdings = set(prior_manifest['holdings'])
    for row in supp_form_rows:
        for raw_ref in refs.split_refs(row.get(supplement_manifest['form_ref_column'])):
            touched_holdings.update(
                mapping.candidates(refs.parse_ref(raw_ref)[0]))
    for row in supp_example_rows:
        for raw_ref in refs.split_refs(
                row.get(supplement_manifest['example_ref_column'])):
            touched_holdings.update(
                mapping.candidates(refs.parse_ref(raw_ref)[0]))

    if problems:
        raise ArchiveError(problems, STAGE_TWO)

    for row, cell in zip(prior_forms, prior_form_cells):
        row[builder.FORM_REF_COLUMN] = cell
    for row, cell in zip(prior_examples, prior_example_cells):
        row[builder.EXAMPLE_REF_COLUMN] = cell

    supplement_forms = _form_rows(
        package_id, supp_form_rows, supp_form_rewritten)
    supplement_examples = _example_rows(
        package_id, supp_example_rows, supp_example_rewritten)
    all_forms = prior_forms + supplement_forms
    all_examples = prior_examples + supplement_examples

    expected_cells = {
        'forms.csv': prior_form_cells + list(supp_form_rewritten.values()),
        'examples.csv': prior_example_cells + list(supp_example_rewritten.values()),
    }

    missing_library = sorted(touched_holdings - set(library))
    if missing_library:
        raise ArchiveError([RefProblem(
            kind='missing_target', batch='<source-library>', table='sources.bib',
            column='<bibliographic record>', row_number=0, row_id=None,
            raw_ref=holding,
            detail='no bibliographic record registered for holding')], STAGE_TWO)

    sources = Sources()
    for holding in sorted(touched_holdings):
        fields = dict(library[holding])
        sources.add(Source(
            fields.pop('genre'), catalog.official_id(holding), **fields))

    migration = {
        info['edition-1_official']: {
            'holding_id': holding,
            'edition_2_official': catalog.official_id(holding),
        }
        for holding, info in sorted(prior_manifest['holdings'].items())
    }
    supplement_records = [{
        'package_id': package_id,
        'batch_id': batch_id,
        'mapping_file': supplement_manifest['mapping_file'],
        'forms_file': supplement_manifest['forms_file'],
        'examples_file': supplement_manifest['examples_file'],
        'form_ref_column': supplement_manifest['form_ref_column'],
        'example_ref_column': supplement_manifest['example_ref_column'],
        'numbering_context': 'supplier-local codes of the declared batch',
    }]
    citation_columns = [
        {'table': builder.FORM_TABLE, 'url': 'forms.csv',
         'column': builder.FORM_REF_COLUMN, 'role': 'word-form citations'},
        {'table': builder.EXAMPLE_TABLE, 'url': 'examples.csv',
         'column': builder.EXAMPLE_REF_COLUMN, 'role': 'example-sentence citations'},
    ]

    output_dir = pathlib.Path(output_dir)
    with tempfile.TemporaryDirectory(dir=output_dir.parent) as tmp:
        staging = pathlib.Path(tmp) / 'artifact'
        staging.mkdir()
        builder.build_wordlist(staging, all_forms, all_examples, sources)
        expected_keys = {
            catalog.official_id(holding) for holding in touched_holdings}
        _verify_readback(staging, expected_cells, expected_keys)
        archive_manifest = build_edition_two_manifest(
            catalog=catalog,
            prior_manifest=prior_manifest,
            prior_artifact_dir=edition_one_dir,
            supplement_records=supplement_records,
            citation_columns=citation_columns,
            holdings_view={
                holding: {f'{catalog.edition}_official': catalog.official_id(holding)}
                for holding in sorted(touched_holdings)},
            migration=migration,
            bib_file='sources.bib')
        write_manifest(staging, archive_manifest)
        write_handoff(staging, archive_manifest)
        _atomic_swap(staging, output_dir)
    return output_dir
