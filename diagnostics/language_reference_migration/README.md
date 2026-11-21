# Language-reference migration diagnostic

Offline, read-only triage for the case where a migrated CLDF dataset validates but a
language reviewer sees a different row than the one the data was migrated to.

The fixture deliberately models the awkward part of the migration:

- the form's reference column was renamed (`Language_ID` -> `Language_PK`);
- it stores the **local primary key** of `LanguageTable`, not the CLDF `ID`;
- one current language's CLDF `ID` equals that raw local key (`deu`), so a
  CLDF-id lookup silently returns a different language;
- one case keeps a historical table defined as an ordinary, non-`LanguageTable`
  table so the declared CSVW foreign key can still find it.

## Running

All commands are local and do not need network access:

```bash
# Build the baseline + variants in a scratch directory and print the report.
python diagnostics/language_reference_migration/diagnose.py --out /tmp/langref-cases

# Inspect an existing local dataset read-only.
python diagnostics/language_reference_migration/diagnose.py \
  --dataset path/to/metadata.json --form-id f1
```

`--dataset` only reads the metadata and CSVs. It never calls `write`, `copy`, or
edits the dataset. The generated fixtures are written only below `--out`; when
`--out` is omitted, a fresh temporary directory is used.

Useful optional flags:

- `--form-table` (default `FormTable`)
- `--form-id` (default: first row)
- `--ref-column` (default: auto-detect from `languageReference` or the FK)
- `--ref-value` (override the raw reference stored in the selected row)

The checked-in example output is `samples/report.md`.

## What each layer actually reads

| layer | source of truth | key used |
|---|---|---|
| Metadata / FK declaration | `forms.csv` `tableSchema.foreignKeys` | declared `resource` + `columnReference` |
| `cldf validate` (csvw integrity) | the declared FK `resource` in metadata | declared target column; it never consults the ORM |
| Direct FK-target read (`Dataset.get_foreign_key_reference` + iterate) | resolved `resource` | declared target column |
| CLDF-id lookup (`Dataset.get_row('LanguageTable', value)`) | current `LanguageTable` | CLDF `ID` |
| ORM `form.language` (`pycldf.orm.Object.related`) | ontology term `languageReference` and the `LanguageTable` component | CLDF `id` if the FK targets `id`, otherwise the current `LanguageTable` primary key |

The critical implementation detail is in `src/pycldf/orm.py`: after resolving
the FK definition it calls
`self.dataset.get_object(TERMS[relation].references, ...)`, i.e. it re-targets the
ontology component (`LanguageTable`) and does **not** use the FK's declared
`resource`. `src/pycldf/validators.py` instead checks
`tablegroup.check_referential_integrity`, which uses the declared CSVW resource.
That mismatch is why validation can pass while the ORM lands on another row.

`Dataset.get_object(..., pk=True)` uses the current primary-key index built in
`Dataset.objects` (`src/pycldf/dataset.py`). It is a useful third opinion: it
follows the current primary key regardless of where the metadata FK points.

## Variants generated

| case | one change | expected signal |
|---|---|---|
| `00_baseline` | none | validation passes; direct FK, ORM `form.language`, and `pk=True` agree; CLDF-id lookup does not |
| `01_property_url_changed` | reference column's `propertyUrl` changed to `metaLanguageReference` | schema invalid; FK still resolves directly; ORM `form.language` is `None` |
| `02_stale_fk_target` | FK `resource` still points to readable `languages-legacy.csv` (ordinary table) | validation **passes**; direct read gets the historical row; ORM gets the current row |
| `03_stale_target_unreadable` | same stale FK, legacy CSV removed, old table definition retained | validation raises `FileNotFoundError`; direct read errors; ORM still resolves the current row |
| `04_current_pk_deleted` | same stale FK, real current PK row deleted | validation **passes** but reports no missing current key; direct read finds the legacy row; ORM raises `KeyError` |
| `05_repaired_fk_resource` | 02 copied, exactly one metadata string changed (`languages-legacy.csv` -> `languages.csv`) | validation passes; direct FK and ORM agree |

The report prints, per method: target table, target column, the actual primary-key
value read, the CLDF id, the name, and a verdict (`SAME AS EXPECTED`,
`OTHER ROW`, `NONE`, or `ERROR`). It then groups identical `(resource, PK)` rows
so it is obvious which methods landed together, and ends each case with
cross-layer verdicts such as `DIVERGENT`, `NOT CAUGHT BY VALIDATION`,
`MISSING KEY NOT REPORTED`, and `CLDF-ID COLLISION`.

## Attribution

The behavior follows the upstream pycldf implementation at the snapshot in this
repository (`pycldf 2.2.1.dev0`, `csvw 4.2.0`):

- `src/pycldf/orm.py:211` resolves the FK definition, then
  `src/pycldf/orm.py:214` / `:216` re-targets the ontology component.
- `src/pycldf/dataset.py:379` (`get_foreign_key_reference`) follows the declared
  FK resource; `src/pycldf/dataset.py:708` (`objects`) builds separate id and
  primary-key indexes.
- `src/pycldf/validators.py:92` delegates referential integrity to csvw's
  `TableGroup.check_referential_integrity`, which reads the declared target table.

## Fix and verification

The production database is not touched. The repair exercised by
`05_repaired_fk_resource` is a one-field metadata change: point the form's FK
`resource` at the current `LanguageTable` file. The report's `Repair provenance`
section records the exact before/after value and the one-line occurrence count,
then shows that the direct read moves from the historical row to the current row
while the ORM result stays the same (it was already consulting `LanguageTable`).

If the goal is to make the ORM and validators agree without changing the
dataset, the code-level fix would be for `Object.related` to resolve against the
FK's declared target table rather than unconditionally using the ontology
component. No such production-code change is made here.
