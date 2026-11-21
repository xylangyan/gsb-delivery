#!/usr/bin/env python3
"""
Offline diagnostic for CLDF language-reference migrations.

The script can either

* build a normal baseline plus four independent failure variants and compare the
  layers used to resolve a form's language reference, or
* inspect an existing local CLDF dataset without writing to it.

It never modifies a dataset. Generated fixtures are written below the directory
passed with ``--out`` (or a fresh temporary directory).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import pathlib
import shutil
import sys
import tempfile
from typing import Any, Iterable, Optional

import csvw  # noqa: F401  (reported in the header; imported lazily is overkill)
import pycldf
from pycldf import Dataset
from pycldf.dataset import SchemaError
from pycldf.terms import TERMS, term_uri

ID_URI = term_uri('id')
NAME_URI = term_uri('name')
LANGUAGE_REFERENCE_URI = term_uri('languageReference')
META_LANGUAGE_REFERENCE_URI = term_uri('metaLanguageReference')
LANGUAGE_TABLE = 'LanguageTable'
FORM_TABLE = 'FormTable'

DEFAULT_CURRENT_ROWS = [
    # CLDF id, local primary key, name.  The second row deliberately has the
    # CLDF id ``deu``, i.e. the raw local key of the first row.
    ('lang-deu', 'deu', 'German (modern)'),
    ('deu', 'other-pk', 'Another language'),
]
DEFAULT_LEGACY_ROWS = [
    ('deu-1900', 'deu', 'German (historical)'),
]


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class FixtureSpec:
    name: str
    description: str
    reference_property: str = LANGUAGE_REFERENCE_URI
    fk_resource: str = 'languages.csv'
    legacy_table: bool = False
    legacy_file: bool = True
    current_rows: list[tuple[str, str, str]] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_CURRENT_ROWS))
    legacy_rows: list[tuple[str, str, str]] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_LEGACY_ROWS))


def _column(name: str, property_url: Optional[str] = None, required: bool = False) -> dict:
    res: dict[str, Any] = {'name': name, 'datatype': 'string'}
    if required:
        res['required'] = True
    if property_url:
        res['propertyUrl'] = property_url
    return res


def _language_table(url: str, rows: list[tuple[str, str, str]], conforms: bool) -> dict:
    table: dict[str, Any] = {
        'url': url,
        'tableSchema': {
            'columns': [
                _column('ID', ID_URI, required=True),
                _column('Local_PK', required=True),
                _column('Name', NAME_URI),
            ],
            'primaryKey': ['Local_PK'],
        },
    }
    if conforms:
        table['dc:conformsTo'] = term_uri(LANGUAGE_TABLE)
    return table


def _write_csv(path: pathlib.Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ['ID,Local_PK,Name']
    lines.extend(','.join(row) for row in rows)
    path.write_text('\n'.join(lines) + '\n', encoding='utf8')


def write_fixture(root: pathlib.Path, spec: FixtureSpec) -> pathlib.Path:
    case_dir = root / spec.name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)

    metadata = {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'dc:conformsTo': term_uri('Wordlist'),
        'dialect': {'delimiter': ',', 'encoding': 'utf-8', 'header': True},
        'tables': [
            {
                'url': 'forms.csv',
                'dc:conformsTo': term_uri(FORM_TABLE),
                'tableSchema': {
                    'columns': [
                        _column('ID', ID_URI, required=True),
                        _column('Language_PK', spec.reference_property, required=True),
                        _column('Parameter_ID', term_uri('parameterReference'), required=True),
                        _column('Form', term_uri('form'), required=True),
                    ],
                    'primaryKey': ['ID'],
                    'foreignKeys': [{
                        'columnReference': ['Language_PK'],
                        'reference': {
                            'resource': spec.fk_resource,
                            'columnReference': ['Local_PK'],
                        },
                    }],
                },
            },
            _language_table('languages.csv', spec.current_rows, conforms=True),
        ],
    }
    if spec.legacy_table:
        metadata['tables'].append(
            _language_table('languages-legacy.csv', spec.legacy_rows, conforms=False))

    (case_dir / 'metadata.json').write_text(
        json.dumps(metadata, indent=2) + '\n', encoding='utf8')
    (case_dir / 'forms.csv').write_text(
        'ID,Language_PK,Parameter_ID,Form\nf1,deu,p1,Haus\n', encoding='utf8')
    _write_csv(case_dir / 'languages.csv', spec.current_rows)
    if spec.legacy_table:
        if spec.legacy_file:
            _write_csv(case_dir / 'languages-legacy.csv', spec.legacy_rows)
        else:
            # Keep the table definition in metadata, but make the path unreadable.
            stale = case_dir / 'languages-legacy.csv'
            if stale.exists():
                stale.unlink()
    (case_dir / 'case.json').write_text(json.dumps({
        'case': spec.name,
        'description': spec.description,
        'reference_property': spec.reference_property,
        'fk_resource': spec.fk_resource,
        'legacy_table_defined': spec.legacy_table,
        'legacy_file_present': spec.legacy_file if spec.legacy_table else None,
        'current_rows': spec.current_rows,
        'legacy_rows': spec.legacy_rows if spec.legacy_table else None,
    }, indent=2) + '\n', encoding='utf8')
    return case_dir


def build_cases(root: pathlib.Path) -> list[pathlib.Path]:
    no_current_pk = [DEFAULT_CURRENT_ROWS[1]]
    specs = [
        FixtureSpec(
            '00_baseline',
            'Normal baseline: renamed reference column stores the current local '
            'LanguageTable primary key; metadata FK points to the current table.',
        ),
        FixtureSpec(
            '01_property_url_changed',
            'Only the reference column CLDF property URL is changed '
            '(languageReference -> metaLanguageReference). The FK is unchanged.',
            reference_property=META_LANGUAGE_REFERENCE_URI,
        ),
        FixtureSpec(
            '02_stale_fk_target',
            'Post-migration LanguageTable lives at languages.csv, but metadata FK '
            'still points at a readable legacy table. The legacy table is an '
            'ordinary table whose Local_PK=deu row is a historical language.',
            fk_resource='languages-legacy.csv',
            legacy_table=True,
        ),
        FixtureSpec(
            '03_stale_target_unreadable',
            'Same stale FK as 02, but the legacy path is unreadable (file removed; '
            'table definition retained).',
            fk_resource='languages-legacy.csv',
            legacy_table=True,
            legacy_file=False,
        ),
        FixtureSpec(
            '04_current_pk_deleted',
            'Same stale FK as 02, but the real current LanguageTable row with '
            'Local_PK=deu has been deleted. Only the CLDF-id collision row remains.',
            fk_resource='languages-legacy.csv',
            legacy_table=True,
            current_rows=no_current_pk,
        ),
    ]
    return [write_fixture(root, spec) for spec in specs]


def repair_fk_resource(
        source: pathlib.Path,
        target: pathlib.Path,
        old_resource: str = 'languages-legacy.csv',
        new_resource: str = 'languages.csv',
) -> dict:
    """
    Copy a generated case and change exactly one metadata string: the FK target
    resource. Fail if that string does not occur exactly once.
    """
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    md = target / 'metadata.json'
    before = md.read_text(encoding='utf8')
    old = f'"resource": "{old_resource}"'
    new = f'"resource": "{new_resource}"'
    count = before.count(old)
    if count != 1:
        raise RuntimeError(
            f'expected exactly one {old!r} in {md}, found {count}')
    after = before.replace(old, new)
    md.write_text(after, encoding='utf8')
    assert before != after
    return {
        'source': source,
        'target': target,
        'old': old,
        'new': new,
        'occurrences': count,
    }


# ---------------------------------------------------------------------------
# Read-only inspection
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class RowView:
    resource: str
    table_type: Optional[str]
    pk_columns: tuple[str, ...]
    pk: tuple[Any, ...]
    cldf_id: Optional[str]
    name: Optional[str]

    @property
    def signature(self) -> tuple:
        return self.resource, tuple(self.pk)

    @property
    def label(self) -> str:
        return f'{self.name!r} [{self.resource} PK={fmt_pk(self.pk_columns, self.pk)}]'

@dataclasses.dataclass
class Probe:
    method: str
    target_table: Optional[str] = None
    via_column: Optional[str] = None
    status: str = 'ok'  # ok | none | error
    rows: list[RowView] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    note: Optional[str] = None


def fmt_pk(columns: Iterable[str], values: Iterable[Any]) -> str:
    pairs = [f'{c}={v!r}' for c, v in zip(columns, values)]
    return ', '.join(pairs) if pairs else '(no primary key)'


def display_path(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(pathlib.Path.cwd()))
    except ValueError:  # pragma: no cover - only for paths outside the working tree
        return str(path.resolve())


def _col_by_uri(table, uri: str):
    for col in table.tableSchema.columns:
        if col.propertyUrl and col.propertyUrl.uri == uri:
            return col
    return None


def _table_type(ds: Dataset, table) -> Optional[str]:
    try:
        return ds.get_tabletype(table)
    except ValueError:
        return None


def row_view(ds: Dataset, table, row: dict) -> RowView:
    pk_columns = tuple(table.tableSchema.primaryKey or [])
    pk = tuple(row.get(c) for c in pk_columns)
    id_col = _col_by_uri(table, ID_URI)
    name_col = _col_by_uri(table, NAME_URI)
    return RowView(
        resource=table.url.string,
        table_type=_table_type(ds, table),
        pk_columns=pk_columns,
        pk=pk,
        cldf_id=row.get(id_col.name) if id_col else None,
        name=row.get(name_col.name) if name_col else None,
    )


def resolve_metadata(path: pathlib.Path) -> pathlib.Path:
    path = path.expanduser().resolve()
    if path.is_dir():
        for candidate in [path / 'metadata.json', path / 'cldf-metadata.json']:
            if candidate.exists():
                return candidate
        matches = sorted(path.glob('*metadata.json'))
        if len(matches) == 1:
            return matches[0]
        raise SystemExit(f'no unique metadata file in {path}')
    if not path.exists():
        raise SystemExit(f'no such dataset metadata file: {path}')
    return path


def fresh(md: pathlib.Path) -> Dataset:
    """Every probe uses a fresh Dataset, so ORM caches cannot leak between layers."""
    return Dataset.from_metadata(md)


def find_form_row(ds: Dataset, form_table: str, form_id: Optional[str]) -> dict:
    table = ds[form_table]
    id_col = _col_by_uri(table, ID_URI)
    if id_col is None:
        raise ValueError(f'{form_table} has no CLDF id column')
    for row in table.iterdicts():
        if form_id is None or row.get(id_col.name) == form_id:
            return row
    raise ValueError(f'no {form_table} row with ID={form_id!r}')


def find_reference_column(ds: Dataset, form_table: str, explicit: Optional[str]):
    table = ds[form_table]
    if explicit:
        return ds[(form_table, explicit)]

    col = ds.get((form_table, 'languageReference'))
    if col:
        return col

    candidates = []
    for c in table.tableSchema.columns:
        if c.propertyUrl and c.propertyUrl.uri in TERMS.by_uri:
            if TERMS.by_uri[c.propertyUrl.uri].references == LANGUAGE_TABLE:
                candidates.append(c)
    if len(candidates) == 1:
        return candidates[0]

    fk_candidates = []
    for fk in table.tableSchema.foreignKeys:
        if len(fk.columnReference) != 1:
            continue
        try:
            target = ds[fk.reference.resource]
        except Exception:
            continue
        if _table_type(ds, target) == LANGUAGE_TABLE:
            fk_candidates.append(ds[(form_table, fk.columnReference[0])])
    if len(fk_candidates) == 1:
        return fk_candidates[0]

    raise ValueError(
        f'cannot identify the language-reference column in {form_table}; '
        'pass --ref-column')


def fk_declarations(ds: Dataset, form_table: str, col) -> list[dict]:
    res = []
    for fk in ds[form_table].tableSchema.foreignKeys:
        if col.name in fk.columnReference:
            res.append({
                'columnReference': list(fk.columnReference),
                'resource': fk.reference.resource.string,
                'targetColumnReference': list(fk.reference.columnReference),
            })
    return res


@dataclasses.dataclass
class RefResolution:
    declared: list[dict]
    table: Optional[str] = None
    column: Optional[str] = None
    column_property_url: Optional[str] = None
    table_type: Optional[str] = None
    target_primary_key: Optional[list[str]] = None
    error: Optional[str] = None


def resolve_reference(ds: Dataset, form_table: str, col) -> RefResolution:
    res = RefResolution(declared=fk_declarations(ds, form_table, col))
    try:
        ref = ds.get_foreign_key_reference(form_table, col.name)
    except Exception as e:  # e.g. FK resource not defined in metadata
        res.error = f'{type(e).__name__}: {e}'
        return res
    if ref is None:
        res.error = 'no matching foreign key'
        return res
    table, target_col = ref
    res.table = table.url.string
    res.column = target_col.name
    res.column_property_url = str(target_col.propertyUrl) if target_col.propertyUrl else None
    res.table_type = _table_type(ds, table)
    res.target_primary_key = list(table.tableSchema.primaryKey or [])
    return res


@dataclasses.dataclass
class ValidationView:
    status: str  # valid | invalid | exception
    messages: list[str]
    exception: Optional[str] = None
    # Rows that validation actually saw in the declared foreign-key target.
    fk_target_rows: Optional[int] = None
    fk_target_keys: Optional[list] = None


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):  # pragma: no cover - exercised through validate
        self.records.append(f'{record.levelname}: {record.getMessage()}')


def run_validation(md: pathlib.Path, form_table: str = FORM_TABLE,
                   ref_column: Optional[str] = None) -> ValidationView:
    ds = fresh(md)
    logger = logging.getLogger(f'pycldf.language-reference-diagnostic.{id(ds)}')
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = _ListHandler()
    logger.addHandler(handler)
    target_rows = target_keys = None
    if form_table in ds:
        col = ds.get((form_table, ref_column)) if ref_column \
            else ds.get((form_table, 'languageReference'))
        try:
            # Peek at the declared FK target the same way csvw's integrity check does.
            if col:
                fk = ds.get_foreign_key_reference(form_table, col.name)
                if fk:
                    table, key = fk
                    key_values = [row.get(key.name) for row in table.iterdicts()]
                    target_rows, target_keys = len(key_values), key_values
        except Exception as e:
            # Leave the visibility fields unset; validate() below reports the error.
            _ = e
    try:
        ok = ds.validate(log=logger)
        return ValidationView(
            'valid' if ok else 'invalid', handler.records,
            fk_target_rows=target_rows, fk_target_keys=target_keys)
    except Exception as e:
        return ValidationView(
            'exception', handler.records, f'{type(e).__name__}: {e}',
            fk_target_rows=target_rows, fk_target_keys=target_keys)
    finally:
        logger.handlers.clear()


def _ref_values(value) -> list:
    return value if isinstance(value, list) else [value]


def _matches(cell, values: list) -> bool:
    cells = cell if isinstance(cell, list) else [cell]
    return any(v in cells for v in values)


def probe_expected(md: pathlib.Path, ref_value) -> Probe:
    ds = fresh(md)
    table = ds[LANGUAGE_TABLE]
    values = _ref_values(ref_value)
    pk = list(table.tableSchema.primaryKey or [])
    pk_cols = ', '.join(pk)
    try:
        if not pk:
            raise ValueError(f'{LANGUAGE_TABLE} has no primary key')
        rows = [
            row_view(ds, table, row) for row in table.iterdicts()
            if row.get(pk[0]) in values
        ]
    except Exception as e:
        return Probe(
            'expected current row (LanguageTable PK)',
            target_table=table.url.string,
            via_column=pk_cols,
            status='error',
            error=f'{type(e).__name__}: {e}',
            note='oracle: current LanguageTable row whose local primary key stores the raw '
                 'reference',
        )
    return Probe(
        'expected current row (LanguageTable PK)',
        target_table=table.url.string,
        via_column=pk_cols,
        status='ok' if rows else 'none',
        rows=rows,
        note='oracle: current LanguageTable row whose local primary key stores the raw reference',
    )


def probe_direct(md: pathlib.Path, form_table: str, form_id: Optional[str],
                 ref_column: Optional[str], ref_value_override) -> Probe:
    ds = fresh(md)
    form = find_form_row(ds, form_table, form_id)
    col = find_reference_column(ds, form_table, ref_column)
    ref_value = ref_value_override if ref_value_override is not None else form.get(col.name)
    ref = resolve_reference(ds, form_table, col)
    probe = Probe('CSVW FK target row (direct read)', note=None)
    if ref.error:
        probe.status = 'error'
        probe.error = ref.error
        return probe
    target = ds[ref.table]
    target_col = ds[(ref.table, ref.column)]
    probe.target_table = target.url.string
    probe.via_column = target_col.name
    values = _ref_values(ref_value)
    try:
        probe.rows = [
            row_view(ds, target, row) for row in target.iterdicts()
            if _matches(row.get(target_col.name), values)
        ]
    except Exception as e:
        probe.status = 'error'
        probe.error = f'{type(e).__name__}: {e}'
        return probe
    probe.status = 'ok' if probe.rows else 'none'
    return probe


def probe_cldf_id(md: pathlib.Path, ref_value) -> Probe:
    ds = fresh(md)
    table = ds[LANGUAGE_TABLE]
    id_col = _col_by_uri(table, ID_URI)
    value = _ref_values(ref_value)[0]
    probe = Probe('CLDF id lookup', target_table=table.url.string,
                  via_column=id_col.name if id_col else 'ID')
    try:
        row = ds.get_row(LANGUAGE_TABLE, value)
    except Exception as e:
        probe.status = 'error'
        probe.error = f'{type(e).__name__}: {e}'
        return probe
    probe.rows = [row_view(ds, table, row)]
    probe.status = 'ok'
    return probe


def orm_resolution(ds: Dataset, form_table: str, col) -> tuple[str, str]:
    """
    Mirror the key selection in pycldf.orm.Object.related: the ORM ignores the
    resolved FK target *resource* and resolves against the ontology component.
    """
    ref = resolve_reference(ds, form_table, col)
    if ref.error or not ref.table:
        return 'none', ''
    if ref.column_property_url == ID_URI:
        return 'CLDF id', ds[(LANGUAGE_TABLE, 'id')].name
    if [ref.column] == list(ds[LANGUAGE_TABLE].tableSchema.primaryKey or []):
        return 'primary key', ref.column
    return 'unsupported', ref.column


def probe_orm_language(md: pathlib.Path, form_table: str, form_id: Optional[str],
                       ref_column: Optional[str]) -> Probe:
    ds = fresh(md)
    col = find_reference_column(ds, form_table, ref_column)
    kind, via = orm_resolution(ds, form_table, col)
    table = ds[LANGUAGE_TABLE]
    note = f'ORM key selection: {kind}'
    if _col_by_uri(ds[form_table], LANGUAGE_REFERENCE_URI) is None:
        note = ('ORM looks for the languageReference property on the form row; that '
                'property is absent, so form.language is None before any FK lookup')
    probe = Probe('ORM form.language', target_table=table.url.string, via_column=via,
                  note=note)
    try:
        form = ds.get_object(form_table, form_id) if form_id else ds.objects(form_table)[0]
        lang = form.language
    except Exception as e:
        probe.status = 'error'
        probe.error = f'{type(e).__name__}: {e}'
        return probe
    if lang is None:
        probe.status = 'none'
        return probe
    probe.rows = [row_view(ds, ds[lang.component], lang.data)]
    probe.status = 'ok'
    return probe


def probe_orm_pk(md: pathlib.Path, ref_value) -> Probe:
    ds = fresh(md)
    table = ds[LANGUAGE_TABLE]
    pk_cols = ', '.join(table.tableSchema.primaryKey or [])
    value = _ref_values(ref_value)[0]
    probe = Probe('ORM get_object(..., pk=True)', target_table=table.url.string,
                  via_column=pk_cols)
    try:
        lang = ds.get_object(LANGUAGE_TABLE, value, pk=True)
    except Exception as e:
        probe.status = 'error'
        probe.error = f'{type(e).__name__}: {e}'
        return probe
    probe.rows = [row_view(ds, table, lang.data)]
    probe.status = 'ok'
    return probe


@dataclasses.dataclass
class CaseReport:
    name: str
    description: str
    metadata: pathlib.Path
    form_table: str
    form_id: Optional[str]
    reference_column: Optional[str]
    reference_property_url: Optional[str]
    raw_reference: Any
    fk: RefResolution
    validation: ValidationView
    expected: Probe
    probes: list[Probe]
    orm_note: Optional[str] = None


def inspect(md: pathlib.Path, form_table: str, form_id: Optional[str],
            ref_column: Optional[str], ref_value_override=None,
            name: str = 'dataset', description: str = '') -> CaseReport:
    ds = fresh(md)
    if LANGUAGE_TABLE not in ds:
        raise SystemExit(
            f'{md}: no LanguageTable found; this diagnostic needs a CLDF LanguageTable')
    form = find_form_row(ds, form_table, form_id)
    col = find_reference_column(ds, form_table, ref_column)
    actual_form_id = form.get(_col_by_uri(ds[form_table], ID_URI).name)
    ref_value = ref_value_override if ref_value_override is not None else form.get(col.name)
    fk = resolve_reference(ds, form_table, col)
    orm_kind, orm_via = orm_resolution(ds, form_table, col)
    if _col_by_uri(ds[form_table], LANGUAGE_REFERENCE_URI) is None:
        orm_note = ('form.language reads the languageReference property, which is absent; '
                    'the ORM never reaches the FK lookup')
    else:
        orm_note = f'{orm_kind} via {orm_via or "n/a"}'

    expected = probe_expected(md, ref_value)
    probes = [
        probe_direct(md, form_table, actual_form_id, col.name, ref_value_override),
        probe_cldf_id(md, ref_value),
        probe_orm_language(md, form_table, actual_form_id, col.name),
        probe_orm_pk(md, ref_value),
    ]
    return CaseReport(
        name=name,
        description=description,
        metadata=md,
        form_table=form_table,
        form_id=actual_form_id,
        reference_column=col.name,
        reference_property_url=str(col.propertyUrl) if col.propertyUrl else None,
        raw_reference=ref_value,
        fk=fk,
        validation=run_validation(md, form_table, col.name),
        expected=expected,
        probes=probes,
        orm_note=orm_note,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _verdict(probe: Probe, expected: Probe) -> str:
    if probe.status == 'error':
        return 'ERROR'
    if probe.status == 'none' or not probe.rows:
        return 'NONE'
    if not expected.rows:
        return 'NO EXPECTED ROW'
    expected_sigs = {r.signature for r in expected.rows}
    if all(r.signature in expected_sigs for r in probe.rows):
        return 'SAME AS EXPECTED'
    return 'OTHER ROW (has a value, different language)'


def _sigs(probe: Probe) -> set[tuple]:
    return {row.signature for row in probe.rows}


def divergence_notes(report: CaseReport) -> list[tuple[str, str]]:
    """Cross-layer verdicts, including what validation silently accepted."""
    direct = report.probes[0]
    cldf_id = report.probes[1]
    orm = report.probes[2]
    notes: list[tuple[str, str]] = []

    if direct.status == 'ok' and orm.status == 'ok':
        if _sigs(direct) == _sigs(orm):
            notes.append(('AGREE', 'direct FK target and ORM form.language resolve to the '
                                   'same row(s)'))
        else:
            notes.append(('DIVERGENT', 'direct FK target and ORM form.language resolve to '
                                       'different rows'))
    elif direct.status == 'ok' and orm.status in ('error', 'none'):
        notes.append(('DIVERGENT',
                      f'direct FK target resolves to {direct.rows[0].label}, while ORM '
                      f'form.language is {orm.status.upper()}: {orm.error or "no row"}'))
    elif direct.status in ('error', 'none') and orm.status == 'ok':
        notes.append(('DIVERGENT',
                      f'ORM form.language resolves to {orm.rows[0].label}, while direct FK '
                      f'target is {direct.status.upper()}: {direct.error or "no row"}'))
    else:
        notes.append(('BOTH UNAVAILABLE',
                      f'direct FK target={direct.status}, ORM form.language={orm.status}'))

    if report.validation.status == 'valid':
        if direct.status == 'ok' and orm.status == 'ok' and _sigs(direct) != _sigs(orm):
            notes.append(('NOT CAUGHT BY VALIDATION',
                          'validation passed even though the declared FK target and the ORM '
                          'disagree'))
        elif direct.status == 'ok' and orm.status in ('error', 'none'):
            notes.append(('NOT CAUGHT BY VALIDATION',
                          'validation passed even though the ORM cannot resolve a row that '
                          'the declared FK target contains'))
        elif orm.status == 'ok' and report.expected.rows and _sigs(orm) != _sigs(report.expected):
            notes.append(('NOT CAUGHT BY VALIDATION',
                          'validation passed even though the ORM resolves a non-expected row'))
    elif report.validation.status == 'invalid' and direct.status == 'ok' and orm.status == 'ok' \
            and _sigs(direct) == _sigs(orm):
        notes.append(('ACCEPTED DESPITE INVALID',
                      'direct FK target and ORM agree, but validation rejects the schema'))

    if cldf_id.status == 'ok' and report.expected.rows \
            and _sigs(cldf_id) != _sigs(report.expected):
        notes.append(('CLDF-ID COLLISION',
                      f'looking up the raw reference as a CLDF id returns '
                      f'{cldf_id.rows[0].label}, not the expected PK row'))

    if report.validation.status == 'valid' and not report.expected.rows:
        raw = report.raw_reference
        notes.append(('MISSING KEY NOT REPORTED',
                      f'validation passed although no current LanguageTable row has '
                      f'the raw reference {raw!r} as primary key'))
    if report.validation.status == 'valid' and orm.status == 'error' \
            and 'KeyError' in (orm.error or ''):
        notes.append(('MISSING KEY NOT REPORTED',
                      f'validation passed although the ORM raises '
                      f'{orm.error} for missing primary key {report.raw_reference!r}'))
    return notes


def _clean(message: str, base: pathlib.Path) -> str:
    text = str(message)
    for prefix in {str(pathlib.Path.cwd()), str(base.resolve())}:
        text = text.replace(prefix.rstrip('/') + '/', '')
        text = text.replace(prefix, '.')
    return text


def render(report: CaseReport, base: Optional[pathlib.Path] = None) -> str:
    base = base or report.metadata.parent
    lines = []
    lines.append(f'## {report.name} — {report.description}')
    lines.append('')
    lines.append('Metadata / row facts:')
    lines.append(f'- metadata: `{display_path(report.metadata)}`')
    lines.append(
        f'- form row: `{report.form_table}` ID=`{report.form_id}`; '
        f'reference column=`{report.reference_column}` '
        f'propertyUrl={report.reference_property_url or "(none)"}; '
        f'raw reference={report.raw_reference!r}')
    if report.fk.declared:
        for decl in report.fk.declared:
            lines.append(
                f"- FK declaration: columnReference={decl['columnReference']} -> "
                f"resource=`{decl['resource']}` "
                f"columnReference={decl['targetColumnReference']}")
    else:
        lines.append('- FK declaration: (none)')
    if report.fk.error:
        lines.append(f'- FK definition lookup: ERROR {_clean(report.fk.error, base)}')
    else:
        lines.append(
            f"- FK definition lookup: target table=`{report.fk.table}` "
            f"type=`{report.fk.table_type or 'ordinary table'}` "
            f"target column=`{report.fk.column}` "
            f"propertyUrl={report.fk.column_property_url or '(none)'} "
            f"target primaryKey={report.fk.target_primary_key}")
    lines.append(f'- validation: {report.validation.status.upper()}')
    if report.validation.fk_target_rows is not None:
        lines.append(
            f"- CSVW check saw {report.validation.fk_target_rows} row(s) in the declared "
            f"FK target, key values={report.validation.fk_target_keys}")
    for msg in report.validation.messages:
        lines.append(f'  - {_clean(msg, base)}')
    if report.validation.exception:
        lines.append(f'  - exception: {_clean(report.validation.exception, base)}')
    if report.validation.status == 'valid' and not report.expected.rows:
        lines.append(
            f'- missing-key check: VALIDATION DID NOT REPORT a missing current primary '
            f'key for {report.raw_reference!r}; it only checked the declared FK target')
    if report.expected.rows:
        lines.append(
            f"- expected current row by LanguageTable PK: {report.expected.rows[0].label} "
            f"(CLDF id={report.expected.rows[0].cldf_id!r})")
    else:
        lines.append('- expected current row by LanguageTable PK: NONE (no current row carries '
                     'the raw reference as primary key)')
    lines.append(f'- ORM key selection derived from the FK: {report.orm_note}')
    lines.append('')
    lines.append(
        '| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for probe in [report.expected] + report.probes:
        if probe.status == 'error':
            lines.append(
                f'| {probe.method} | {probe.target_table or "—"} | {probe.via_column or "—"} | '
                f'— | — | — | — | ERROR: {_clean(probe.error, base)} |')
        elif not probe.rows:
            lines.append(
                f'| {probe.method} | {probe.target_table or "—"} | {probe.via_column or "—"} | '
                f'— | — | — | — | NONE |')
        else:
            for idx, row in enumerate(probe.rows):
                method = probe.method if idx == 0 else f'{probe.method} [hit {idx + 1}]'
                verdict = _verdict(probe, report.expected)
                lines.append(
                    f'| {method} | `{row.resource}` | {probe.via_column or "—"} | '
                    f'{", ".join(row.pk_columns) or "—"} | '
                    f'{fmt_pk(row.pk_columns, row.pk)} | {row.cldf_id!r} | '
                    f'{row.name!r} | {verdict} |')
    lines.append('')

    groups: dict[tuple, tuple[RowView, list[str]]] = {}
    for probe in [report.expected] + report.probes:
        for row in probe.rows:
            groups.setdefault(row.signature, (row, []))[1].append(probe.method)
    lines.append('Consensus groups (same resource + same primary key):')
    if not groups:
        lines.append('- (no rows resolved)')
    for row, methods in groups.values():
        lines.append(f'- {row.label}, CLDF id={row.cldf_id!r}: ' + ', '.join(methods))
    non_expected = [
        (row, methods) for row, methods in groups.values()
        if report.expected.rows and row.signature not in {r.signature for r in report.expected.rows}
    ]
    if non_expected:
        lines.append('Non-expected hits:')
        for row, methods in non_expected:
            lines.append(f'- {row.label}, CLDF id={row.cldf_id!r}: ' + ', '.join(methods))
    else:
        lines.append('Non-expected hits: none')
    lines.append('')
    lines.append('Cross-layer verdicts:')
    for verdict, message in divergence_notes(report):
        lines.append(f'- **{verdict}**: {_clean(message, base)}')
    lines.append('')
    return '\n'.join(lines)


def _summary_cell(probe: Probe, base: Optional[pathlib.Path] = None) -> str:
    if probe.status == 'error':
        return f'ERROR ({_clean(probe.error, base or pathlib.Path.cwd())})'
    if not probe.rows:
        return 'NONE'
    row = probe.rows[0]
    return f'{row.name!r} ({row.resource} PK={fmt_pk(row.pk_columns, row.pk)})'


def render_summary(reports: list[CaseReport], base: Optional[pathlib.Path] = None) -> str:
    lines = [
        '# CLDF language-reference migration diagnostic',
        '',
        f'Environment: pycldf {pycldf.__version__}, csvw {csvw.__version__}. '
        'Every probe uses a freshly loaded `Dataset`, so the ORM object cache cannot '
        'carry state from one layer to the next. No dataset is modified.',
        '',
        '| case | mutation | validation | direct FK target | CLDF id | ORM language |',
        '|---|---|---|---|---|---|',
    ]
    for report in reports:
        lines.append(
            f'| {report.name} | {report.description} | {report.validation.status.upper()} | '
            f'{_summary_cell(report.probes[0], base)} | '
            f'{_summary_cell(report.probes[1], base)} | '
            f'{_summary_cell(report.probes[2], base)} |')
    lines.append('')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Compare metadata, CSVW foreign-key and ORM language-reference '
                    'resolution. Read-only with --dataset; generated cases go to --out.')
    parser.add_argument('--out', type=pathlib.Path,
                        help='directory for generated fixtures (default: a temp dir)')
    parser.add_argument('--dataset', type=pathlib.Path,
                        help='inspect an existing local dataset metadata file or directory')
    parser.add_argument('--form-table', default=FORM_TABLE)
    parser.add_argument('--form-id', default=None,
                        help='FormTable row ID (default: first row)')
    parser.add_argument('--ref-column', default=None,
                        help='language-reference column name (default: auto-detect)')
    parser.add_argument('--ref-value', default=None,
                        help='override the raw reference value read from the form row')
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _arg_parser().parse_args(argv)

    if args.dataset:
        md = resolve_metadata(args.dataset)
        try:
            report = inspect(
                md, args.form_table, args.form_id, args.ref_column, args.ref_value,
                name='existing dataset (read-only)',
                description='supplied --dataset; nothing was written')
        except (ValueError, SchemaError) as e:
            raise SystemExit(f'{display_path(md)}: {e}') from e
        print(render_summary([report], base=md.parent))
        print(render(report))
        return 0

    if args.out:
        root = args.out.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = pathlib.Path(tempfile.mkdtemp(prefix='pycldf-langref-'))
    case_dirs = build_cases(root)
    reports = []
    for case_dir in case_dirs:
        meta = json.loads((case_dir / 'case.json').read_text(encoding='utf8'))
        reports.append(inspect(
            case_dir / 'metadata.json', FORM_TABLE, 'f1', 'Language_PK',
            name=meta['case'], description=meta['description']))

    # One-field repair, applied to a copy of the stale-target case (02).
    stale = root / '02_stale_fk_target'
    repair_dir = root / '05_repaired_fk_resource'
    change = repair_fk_resource(stale, repair_dir)
    repaired = inspect(
        repair_dir / 'metadata.json', FORM_TABLE, 'f1', 'Language_PK',
        name='05_repaired_fk_resource',
        description='One-field repair of 02: metadata FK resource is changed from '
                    'languages-legacy.csv to languages.csv. Nothing else changes.')
    reports.append(repaired)

    print(render_summary(reports, base=root))
    for report in reports:
        print(render(report, base=root))

    before = next(r for r in reports if r.name == '02_stale_fk_target')
    print('## Repair provenance')
    print('')
    print(f'- source case: `{display_path(stale)}`')
    print(f'- repaired copy: `{display_path(repair_dir)}`')
    print(f'- edit: replaced exactly one occurrence of `{change["old"]}` with '
          f'`{change["new"]}` in `metadata.json`')
    print(f'- occurrences before edit: {change["occurrences"]}')
    print('')
    print('| layer | before repair (02) | after repair (05) |')
    print('|---|---|---|')
    print(f'| validation | {before.validation.status.upper()} | '
          f'{repaired.validation.status.upper()} |')
    for idx, label in enumerate(['direct FK target', 'CLDF id', 'ORM form.language']):
        print(f'| {label} | {_summary_cell(before.probes[idx], root)} | '
              f'{_summary_cell(repaired.probes[idx], root)} |')
    print('')
    print(f'Generated fixtures: `{display_path(root)}`')
    return 0


if __name__ == '__main__':
    sys.exit(main())
