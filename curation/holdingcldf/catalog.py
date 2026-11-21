"""Catalogue editions and per-batch local-code mappings.

A *local code* only has meaning inside the mapping table of the batch that
produced it.  The same string (e.g. ``X1``) can therefore map to different
holdings in different batches; resolution never looks at the shape of the
string.
"""

import csv
import json
import pathlib
from dataclasses import dataclass


class CatalogError(ValueError):
    """Raised when a catalogue or mapping file itself is malformed."""


@dataclass(frozen=True)
class HoldingCatalog:
    """One edition of the holding catalogue: stable holding -> official code."""

    edition: str
    catalog_id: str
    table_file: str
    holding_to_official: dict[str, str]

    @classmethod
    def from_dir(cls, catalog_dir: pathlib.Path) -> 'HoldingCatalog':
        catalog_dir = pathlib.Path(catalog_dir)
        manifest_path = catalog_dir / 'catalog_manifest.json'
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise CatalogError(f'catalogue manifest not found: {manifest_path}') from exc
        edition = manifest['edition']
        catalog_id = manifest['catalog_id']
        table_file = manifest['table_file']
        table_path = catalog_dir / table_file
        mapping: dict[str, str] = {}
        with table_path.open(encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle, delimiter='\t'):
                holding = row['holding_id'].strip()
                official = row['official_id'].strip()
                if holding in mapping and mapping[holding] != official:
                    raise CatalogError(
                        f'catalogue {edition!r} assigns conflicting official codes to '
                        f'holding {holding!r}: {mapping[holding]!r} vs {official!r}')
                mapping[holding] = official
        return cls(edition, catalog_id, table_file, mapping)

    def official_id(self, holding_id: str) -> str | None:
        return self.holding_to_official.get(holding_id)

    def reverse(self) -> dict[str, str]:
        """Official code -> holding id (for reading an older artifact back)."""
        return {official: holding for holding, official in self.holding_to_official.items()}


@dataclass(frozen=True)
class BatchMapping:
    """Local codes of one batch; a code may have zero, one or several holdings."""

    batch_id: str
    table_file: str
    local_to_holdings: dict[str, tuple[str, ...]]

    @classmethod
    def from_file(cls, batch_id: str, mapping_path: pathlib.Path) -> 'BatchMapping':
        mapping_path = pathlib.Path(mapping_path)
        grouped: dict[str, list[str]] = {}
        seen_pairs: set[tuple[str, str]] = set()
        with mapping_path.open(encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle, delimiter='\t'):
                row_batch = row['batch_id'].strip()
                if row_batch != batch_id:
                    raise CatalogError(
                        f'mapping {mapping_path} belongs to batch {row_batch!r}, '
                        f'expected {batch_id!r}')
                local = row['local_code'].strip()
                holding = row['holding_id'].strip()
                if (local, holding) in seen_pairs:
                    continue
                seen_pairs.add((local, holding))
                grouped.setdefault(local, []).append(holding)
        return cls(
            batch_id,
            mapping_path.name,
            {local: tuple(holdings) for local, holdings in grouped.items()},
        )

    def candidates(self, local_code: str) -> tuple[str, ...]:
        """All holdings a local code points at inside *this* batch (possibly empty)."""
        return self.local_to_holdings.get(local_code, ())


@dataclass(frozen=True)
class BatchManifest:
    """Declares the files and the two distinct local citation columns of a batch."""

    batch_id: str
    forms_file: str
    examples_file: str
    form_ref_column: str
    example_ref_column: str

    @classmethod
    def from_dir(cls, batch_dir: pathlib.Path) -> 'BatchManifest':
        batch_dir = pathlib.Path(batch_dir)
        path = batch_dir / 'batch_manifest.json'
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise CatalogError(f'batch manifest not found: {path}') from exc
        try:
            return cls(
                batch_id=manifest['batch_id'],
                forms_file=manifest['forms_file'],
                examples_file=manifest['examples_file'],
                form_ref_column=manifest['form_ref_column'],
                example_ref_column=manifest['example_ref_column'],
            )
        except KeyError as exc:
            raise CatalogError(f'batch manifest {path} misses key {exc.args[0]!r}') from exc
