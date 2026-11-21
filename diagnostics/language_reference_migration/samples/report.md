# CLDF language-reference migration diagnostic

Environment: pycldf 2.2.1.dev0, csvw 4.2.0. Every probe uses a freshly loaded `Dataset`, so the ORM object cache cannot carry state from one layer to the next. No dataset is modified.

| case | mutation | validation | direct FK target | CLDF id | ORM language |
|---|---|---|---|---|---|
| 00_baseline | Normal baseline: renamed reference column stores the current local LanguageTable primary key; metadata FK points to the current table. | VALID | 'German (modern)' (languages.csv PK=Local_PK='deu') | 'Another language' (languages.csv PK=Local_PK='other-pk') | 'German (modern)' (languages.csv PK=Local_PK='deu') |
| 01_property_url_changed | Only the reference column CLDF property URL is changed (languageReference -> metaLanguageReference). The FK is unchanged. | INVALID | 'German (modern)' (languages.csv PK=Local_PK='deu') | 'Another language' (languages.csv PK=Local_PK='other-pk') | NONE |
| 02_stale_fk_target | Post-migration LanguageTable lives at languages.csv, but metadata FK still points at a readable legacy table. The legacy table is an ordinary table whose Local_PK=deu row is a historical language. | VALID | 'German (historical)' (languages-legacy.csv PK=Local_PK='deu') | 'Another language' (languages.csv PK=Local_PK='other-pk') | 'German (modern)' (languages.csv PK=Local_PK='deu') |
| 03_stale_target_unreadable | Same stale FK as 02, but the legacy path is unreadable (file removed; table definition retained). | EXCEPTION | ERROR (FileNotFoundError: [Errno 2] No such file or directory: 'diagnostics/language_reference_migration/samples/03_stale_target_unreadable/languages-legacy.csv') | 'Another language' (languages.csv PK=Local_PK='other-pk') | 'German (modern)' (languages.csv PK=Local_PK='deu') |
| 04_current_pk_deleted | Same stale FK as 02, but the real current LanguageTable row with Local_PK=deu has been deleted. Only the CLDF-id collision row remains. | VALID | 'German (historical)' (languages-legacy.csv PK=Local_PK='deu') | 'Another language' (languages.csv PK=Local_PK='other-pk') | ERROR (KeyError: 'deu') |
| 05_repaired_fk_resource | One-field repair of 02: metadata FK resource is changed from languages-legacy.csv to languages.csv. Nothing else changes. | VALID | 'German (modern)' (languages.csv PK=Local_PK='deu') | 'Another language' (languages.csv PK=Local_PK='other-pk') | 'German (modern)' (languages.csv PK=Local_PK='deu') |

## 00_baseline — Normal baseline: renamed reference column stores the current local LanguageTable primary key; metadata FK points to the current table.

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/00_baseline/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#languageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages.csv` type=`LanguageTable` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: VALID
- CSVW check saw 2 row(s) in the declared FK target, key values=['deu', 'other-pk']
- expected current row by LanguageTable PK: 'German (modern)' [languages.csv PK=Local_PK='deu'] (CLDF id='lang-deu')
- ORM key selection derived from the FK: primary key via Local_PK

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CSVW FK target row (direct read) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | OTHER ROW (has a value, different language) |
| ORM form.language | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| ORM get_object(..., pk=True) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |

Consensus groups (same resource + same primary key):
- 'German (modern)' [languages.csv PK=Local_PK='deu'], CLDF id='lang-deu': expected current row (LanguageTable PK), CSVW FK target row (direct read), ORM form.language, ORM get_object(..., pk=True)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits:
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup

Cross-layer verdicts:
- **AGREE**: direct FK target and ORM form.language resolve to the same row(s)
- **CLDF-ID COLLISION**: looking up the raw reference as a CLDF id returns 'Another language' [languages.csv PK=Local_PK='other-pk'], not the expected PK row

## 01_property_url_changed — Only the reference column CLDF property URL is changed (languageReference -> metaLanguageReference). The FK is unchanged.

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/01_property_url_changed/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#metaLanguageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages.csv` type=`LanguageTable` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: INVALID
- CSVW check saw 2 row(s) in the declared FK target, key values=['deu', 'other-pk']
  - WARNING: http://cldf.clld.org/v1.0/terms.rdf#FormTable requires column http://cldf.clld.org/v1.0/terms.rdf#languageReference
- expected current row by LanguageTable PK: 'German (modern)' [languages.csv PK=Local_PK='deu'] (CLDF id='lang-deu')
- ORM key selection derived from the FK: form.language reads the languageReference property, which is absent; the ORM never reaches the FK lookup

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CSVW FK target row (direct read) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | OTHER ROW (has a value, different language) |
| ORM form.language | languages.csv | Local_PK | — | — | — | — | NONE |
| ORM get_object(..., pk=True) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |

Consensus groups (same resource + same primary key):
- 'German (modern)' [languages.csv PK=Local_PK='deu'], CLDF id='lang-deu': expected current row (LanguageTable PK), CSVW FK target row (direct read), ORM get_object(..., pk=True)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits:
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup

Cross-layer verdicts:
- **DIVERGENT**: direct FK target resolves to 'German (modern)' [languages.csv PK=Local_PK='deu'], while ORM form.language is NONE: no row
- **CLDF-ID COLLISION**: looking up the raw reference as a CLDF id returns 'Another language' [languages.csv PK=Local_PK='other-pk'], not the expected PK row

## 02_stale_fk_target — Post-migration LanguageTable lives at languages.csv, but metadata FK still points at a readable legacy table. The legacy table is an ordinary table whose Local_PK=deu row is a historical language.

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/02_stale_fk_target/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#languageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages-legacy.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages-legacy.csv` type=`ordinary table` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: VALID
- CSVW check saw 1 row(s) in the declared FK target, key values=['deu']
- expected current row by LanguageTable PK: 'German (modern)' [languages.csv PK=Local_PK='deu'] (CLDF id='lang-deu')
- ORM key selection derived from the FK: primary key via Local_PK

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CSVW FK target row (direct read) | `languages-legacy.csv` | Local_PK | Local_PK | Local_PK='deu' | 'deu-1900' | 'German (historical)' | OTHER ROW (has a value, different language) |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | OTHER ROW (has a value, different language) |
| ORM form.language | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| ORM get_object(..., pk=True) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |

Consensus groups (same resource + same primary key):
- 'German (modern)' [languages.csv PK=Local_PK='deu'], CLDF id='lang-deu': expected current row (LanguageTable PK), ORM form.language, ORM get_object(..., pk=True)
- 'German (historical)' [languages-legacy.csv PK=Local_PK='deu'], CLDF id='deu-1900': CSVW FK target row (direct read)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits:
- 'German (historical)' [languages-legacy.csv PK=Local_PK='deu'], CLDF id='deu-1900': CSVW FK target row (direct read)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup

Cross-layer verdicts:
- **DIVERGENT**: direct FK target and ORM form.language resolve to different rows
- **NOT CAUGHT BY VALIDATION**: validation passed even though the declared FK target and the ORM disagree
- **CLDF-ID COLLISION**: looking up the raw reference as a CLDF id returns 'Another language' [languages.csv PK=Local_PK='other-pk'], not the expected PK row

## 03_stale_target_unreadable — Same stale FK as 02, but the legacy path is unreadable (file removed; table definition retained).

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/03_stale_target_unreadable/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#languageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages-legacy.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages-legacy.csv` type=`ordinary table` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: EXCEPTION
  - WARNING: diagnostics/language_reference_migration/samples/03_stale_target_unreadable/languages-legacy.csv does not exist
  - exception: FileNotFoundError: [Errno 2] No such file or directory: 'diagnostics/language_reference_migration/samples/03_stale_target_unreadable/languages-legacy.csv'
- expected current row by LanguageTable PK: 'German (modern)' [languages.csv PK=Local_PK='deu'] (CLDF id='lang-deu')
- ORM key selection derived from the FK: primary key via Local_PK

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CSVW FK target row (direct read) | languages-legacy.csv | Local_PK | — | — | — | — | ERROR: FileNotFoundError: [Errno 2] No such file or directory: 'diagnostics/language_reference_migration/samples/03_stale_target_unreadable/languages-legacy.csv' |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | OTHER ROW (has a value, different language) |
| ORM form.language | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| ORM get_object(..., pk=True) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |

Consensus groups (same resource + same primary key):
- 'German (modern)' [languages.csv PK=Local_PK='deu'], CLDF id='lang-deu': expected current row (LanguageTable PK), ORM form.language, ORM get_object(..., pk=True)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits:
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup

Cross-layer verdicts:
- **DIVERGENT**: ORM form.language resolves to 'German (modern)' [languages.csv PK=Local_PK='deu'], while direct FK target is ERROR: FileNotFoundError: [Errno 2] No such file or directory: 'diagnostics/language_reference_migration/samples/03_stale_target_unreadable/languages-legacy.csv'
- **CLDF-ID COLLISION**: looking up the raw reference as a CLDF id returns 'Another language' [languages.csv PK=Local_PK='other-pk'], not the expected PK row

## 04_current_pk_deleted — Same stale FK as 02, but the real current LanguageTable row with Local_PK=deu has been deleted. Only the CLDF-id collision row remains.

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/04_current_pk_deleted/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#languageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages-legacy.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages-legacy.csv` type=`ordinary table` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: VALID
- CSVW check saw 1 row(s) in the declared FK target, key values=['deu']
- missing-key check: VALIDATION DID NOT REPORT a missing current primary key for 'deu'; it only checked the declared FK target
- expected current row by LanguageTable PK: NONE (no current row carries the raw reference as primary key)
- ORM key selection derived from the FK: primary key via Local_PK

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | languages.csv | Local_PK | — | — | — | — | NONE |
| CSVW FK target row (direct read) | `languages-legacy.csv` | Local_PK | Local_PK | Local_PK='deu' | 'deu-1900' | 'German (historical)' | NO EXPECTED ROW |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | NO EXPECTED ROW |
| ORM form.language | languages.csv | Local_PK | — | — | — | — | ERROR: KeyError: 'deu' |
| ORM get_object(..., pk=True) | languages.csv | Local_PK | — | — | — | — | ERROR: KeyError: 'deu' |

Consensus groups (same resource + same primary key):
- 'German (historical)' [languages-legacy.csv PK=Local_PK='deu'], CLDF id='deu-1900': CSVW FK target row (direct read)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits: none

Cross-layer verdicts:
- **DIVERGENT**: direct FK target resolves to 'German (historical)' [languages-legacy.csv PK=Local_PK='deu'], while ORM form.language is ERROR: KeyError: 'deu'
- **NOT CAUGHT BY VALIDATION**: validation passed even though the ORM cannot resolve a row that the declared FK target contains
- **MISSING KEY NOT REPORTED**: validation passed although no current LanguageTable row has the raw reference 'deu' as primary key
- **MISSING KEY NOT REPORTED**: validation passed although the ORM raises KeyError: 'deu' for missing primary key 'deu'

## 05_repaired_fk_resource — One-field repair of 02: metadata FK resource is changed from languages-legacy.csv to languages.csv. Nothing else changes.

Metadata / row facts:
- metadata: `diagnostics/language_reference_migration/samples/05_repaired_fk_resource/metadata.json`
- form row: `FormTable` ID=`f1`; reference column=`Language_PK` propertyUrl=http://cldf.clld.org/v1.0/terms.rdf#languageReference; raw reference='deu'
- FK declaration: columnReference=['Language_PK'] -> resource=`languages.csv` columnReference=['Local_PK']
- FK definition lookup: target table=`languages.csv` type=`LanguageTable` target column=`Local_PK` propertyUrl=(none) target primaryKey=['Local_PK']
- validation: VALID
- CSVW check saw 2 row(s) in the declared FK target, key values=['deu', 'other-pk']
- expected current row by LanguageTable PK: 'German (modern)' [languages.csv PK=Local_PK='deu'] (CLDF id='lang-deu')
- ORM key selection derived from the FK: primary key via Local_PK

| method | target table | via column | PK column | PK value | CLDF id | Name | verdict |
|---|---|---|---|---|---|---|---|
| expected current row (LanguageTable PK) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CSVW FK target row (direct read) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| CLDF id lookup | `languages.csv` | ID | Local_PK | Local_PK='other-pk' | 'deu' | 'Another language' | OTHER ROW (has a value, different language) |
| ORM form.language | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |
| ORM get_object(..., pk=True) | `languages.csv` | Local_PK | Local_PK | Local_PK='deu' | 'lang-deu' | 'German (modern)' | SAME AS EXPECTED |

Consensus groups (same resource + same primary key):
- 'German (modern)' [languages.csv PK=Local_PK='deu'], CLDF id='lang-deu': expected current row (LanguageTable PK), CSVW FK target row (direct read), ORM form.language, ORM get_object(..., pk=True)
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup
Non-expected hits:
- 'Another language' [languages.csv PK=Local_PK='other-pk'], CLDF id='deu': CLDF id lookup

Cross-layer verdicts:
- **AGREE**: direct FK target and ORM form.language resolve to the same row(s)
- **CLDF-ID COLLISION**: looking up the raw reference as a CLDF id returns 'Another language' [languages.csv PK=Local_PK='other-pk'], not the expected PK row

## Repair provenance

- source case: `diagnostics/language_reference_migration/samples/02_stale_fk_target`
- repaired copy: `diagnostics/language_reference_migration/samples/05_repaired_fk_resource`
- edit: replaced exactly one occurrence of `"resource": "languages-legacy.csv"` with `"resource": "languages.csv"` in `metadata.json`
- occurrences before edit: 1

| layer | before repair (02) | after repair (05) |
|---|---|---|
| validation | VALID | VALID |
| direct FK target | 'German (historical)' (languages-legacy.csv PK=Local_PK='deu') | 'German (modern)' (languages.csv PK=Local_PK='deu') |
| CLDF id | 'Another language' (languages.csv PK=Local_PK='other-pk') | 'Another language' (languages.csv PK=Local_PK='other-pk') |
| ORM form.language | 'German (modern)' (languages.csv PK=Local_PK='deu') | 'German (modern)' (languages.csv PK=Local_PK='deu') |

Generated fixtures: `diagnostics/language_reference_migration/samples`
