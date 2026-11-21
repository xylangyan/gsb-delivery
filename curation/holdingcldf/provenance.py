"""Archive manifests and the human handoff note.

The archive manifest is the *only* authoritative statement of which numbering
context an artifact lives in.  BibTeX key shape (``X1`` vs ``CAT37``) must
never be used to guess that context: edition 1 happens to contain an official
key ``X1`` that looks just like a supplier local code.
"""

import json
import pathlib
from datetime import datetime, timezone
from typing import Optional

MANIFEST_NAME = 'archive_manifest.json'
HANDOFF_NAME = 'HANDOFF.md'

EDITION_ONE = 'edition-1'
EDITION_TWO = 'edition-2'


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write_manifest(output_dir: pathlib.Path, manifest: dict) -> pathlib.Path:
    path = pathlib.Path(output_dir) / MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return path


def read_manifest(artifact_dir: pathlib.Path) -> dict:
    path = pathlib.Path(artifact_dir) / MANIFEST_NAME
    return json.loads(path.read_text(encoding='utf-8'))


def build_edition_one_manifest(
    *,
    catalog,
    batch_records: list[dict],
    citation_columns: list[dict],
    holdings_view: dict[str, dict],
    bib_file: str,
) -> dict:
    return {
        'artifact': 'holding-wordlist',
        'archive_version': EDITION_ONE,
        'created_at': utc_now(),
        'numbering_context': {
            'catalogue_edition': catalog.edition,
            'catalogue_id': catalog.catalog_id,
            'catalogue_table_file': catalog.table_file,
            'bibtex_key_space': 'catalogue edition 1 official holding codes',
            'rule': (
                'BibTeX keys and citation keys are edition-1 official codes. '
                'Never infer this from key shape; always read this manifest.'
            ),
        },
        'resolution_chain': (
            'supplier local code -> per-batch mapping table -> stable holding id '
            '-> catalogue edition 1 official code'
        ),
        'batches': batch_records,
        'citation_columns': citation_columns,
        'bibtex': {
            'file': bib_file,
            'entries_per_holding': 1,
            'keyed_by': 'catalogue_edition_1_official_code',
        },
        'holdings': holdings_view,
        'prior_artifact': None,
        'supplements': [],
    }


def build_edition_two_manifest(
    *,
    catalog,
    prior_manifest: dict,
    prior_artifact_dir: pathlib.Path,
    supplement_records: list[dict],
    citation_columns: list[dict],
    holdings_view: dict[str, dict],
    migration: dict[str, dict],
    bib_file: str,
) -> dict:
    return {
        'artifact': 'holding-wordlist',
        'archive_version': EDITION_TWO,
        'created_at': utc_now(),
        'numbering_context': {
            'catalogue_edition': catalog.edition,
            'catalogue_id': catalog.catalog_id,
            'catalogue_table_file': catalog.table_file,
            'bibtex_key_space': 'catalogue edition 2 official holding codes',
            'rule': (
                'BibTeX keys and citation keys are edition-2 official codes. '
                'Never infer this from key shape; always read this manifest.'
            ),
        },
        'resolution_chain': (
            'edition-1 artifact keys -> stable holding id -> catalogue edition 2 '
            'official code; supplement local codes -> per-batch mapping -> stable '
            'holding id -> catalogue edition 2 official code'
        ),
        'prior_artifact': {
            'path': str(prior_artifact_dir),
            'archive_version': prior_manifest['archive_version'],
            'catalogue_edition': prior_manifest['numbering_context']['catalogue_edition'],
            'bibtex_key_space': prior_manifest['numbering_context']['bibtex_key_space'],
        },
        'batches': prior_manifest['batches'],
        'supplements': supplement_records,
        'citation_columns': citation_columns,
        'bibtex': {
            'file': bib_file,
            'entries_per_holding': 1,
            'keyed_by': 'catalogue_edition_2_official_code',
        },
        'holdings': holdings_view,
        'official_code_migration': migration,
    }


def write_handoff(output_dir: pathlib.Path, manifest: dict) -> pathlib.Path:
    ctx = manifest['numbering_context']
    lines = [
        '# Handoff: numbering context of this CLDF artifact',
        '',
        '## How to identify the numbering context',
        '',
        f'- Read `{MANIFEST_NAME}` -> `numbering_context.catalogue_edition`: '
        f'`{ctx["catalogue_edition"]}`.',
        f'- BibTeX key space: {ctx["bibtex_key_space"]}.',
        '- Do NOT guess from the shape of a key. The string "X1" is an official',
        '  catalogue code in the edition-1 artifact but a supplier-local code in',
        '  raw batch beta material; only the manifest and the per-batch mapping',
        '  tables disambiguate it.',
        '',
        '## Numbering layers',
        '',
        '1. Stable holding id (e.g. R37, R88).',
        '2. Supplier local code: meaningful only inside that batch mapping table.',
        '   Alpha X1 and beta AB both mean R37; beta raw X1 means R88.',
        '3. Catalogue official code, edition dependent: edition 1 gave R37 the',
        '   official code X1; edition 2 replaced that official code with CAT37.',
        '',
        '## Citation columns (CLDF source property)',
        '',
    ]
    for col in manifest['citation_columns']:
        lines.append(
            f'- Table `{col["table"]}` ({col["url"]}), local column '
            f'`{col["column"]}`: {col["role"]}.'
        )
    lines += [
        '',
        'Citation strings keep their original order and bracket descriptions',
        '(page ranges such as 3-5, sections such as Appendix A, repeated',
        'citations of the same work). Only citation keys are rewritten.',
        '',
        '## Batches and supplements packaged here',
        '',
    ]
    for batch in manifest['batches']:
        lines.append(
            f'- Batch `{batch["batch_id"]}`: mapping `{batch["mapping_file"]}`, '
            f'original form citation field `{batch["form_ref_column"]}`, '
            f'original example citation field `{batch["example_ref_column"]}`.'
        )
    for supplement in manifest['supplements']:
        lines.append(
            f'- Supplement `{supplement["package_id"]}` (raw material of batch '
            f'`{supplement["batch_id"]}`): mapping `{supplement["mapping_file"]}`, '
            f'original form citation field `{supplement["form_ref_column"]}`, '
            f'original example citation field `{supplement["example_ref_column"]}`.'
        )
    if manifest['archive_version'] == EDITION_TWO:
        lines += [
            '',
            '## Official-code migration (edition 1 -> edition 2)',
            '',
        ]
        for old, info in sorted(manifest['official_code_migration'].items()):
            lines.append(
                f'- {old} -> {info["edition_2_official"]} '
                f'(holding {info["holding_id"]})'
            )
    lines.append('')
    path = pathlib.Path(output_dir) / HANDOFF_NAME
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def expected_manifest(artifact_dir: pathlib.Path, archive_version: str) -> dict:
    manifest = read_manifest(artifact_dir)
    actual: Optional[str] = manifest.get('archive_version')
    if actual != archive_version:
        raise ValueError(
            f'{artifact_dir}/{MANIFEST_NAME} declares archive_version={actual!r}, '
            f'expected {archive_version!r}; refusing to process under the wrong '
            'numbering context'
        )
    return manifest
