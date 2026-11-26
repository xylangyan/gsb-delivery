# JSON to TagValue handoff

This module turns archived SPDX-2 JSON catalogs into TagValue packages that read
back to the same relationship multiset under `spdx_tools`. It is meant for the
case where one JSON catalog batch is parsed by `spdx_tools` and then re-emitted
as TagValue for a legacy store, using a package declaration order shared by
several catalogs.

## Why a dedicated writer

The stock TagValue writer places files after the first package that contains
them. The TagValue reader, however, infers a `CONTAINS` relationship for a file
that directly follows any package. Writing a catalog whose JSON has, for example,
only a commented reverse `CONTAINED_BY` relationship would therefore add a
forward relationship the JSON never had.

The handoff writer plans the placement explicitly:

- `encoding.py` decides the exact `Relationship`/`RelationshipComment` text and
  rejects relationship text that the TagValue reader cannot reproduce (a
  carriage return, or a multi-line comment containing the literal `</text>`).
- `planning.py` explores package placements among the locked file order, scores
  each by the UTF-8 byte cost of the relationships that must still be written
  explicitly, and chooses a shared package order that minimizes the worst-case
  revision. A revision may pin per-package allowed gaps, and revisions whose own
  package orders form a cycle (so no shared total order exists) are reported as
  stuck.
- `emitter.py` writes the chosen placement and rereads it to confirm the
  relationship multiset, direction, comments and count.
- `batch.py` and `cli.py` drive a whole handoff from a manifest.

## Semantics preserved

- Packages are declared in the shared order; a package absent from a catalog is
  skipped.
- Files keep the locked order carried by the revision, and each snippet follows
  its file.
- A file placed directly after a package gives the reader a free
  `CONTAINS package file` (no comment). Any other relationship is written
  explicitly.
- The package and file declarations each appear once, and the reread result
  matches the JSON relationship count, direction, comments and multiplicity.
- Only interactions named by the handoff order are effective: a `hasFiles` entry
  that `spdx_tools` already collapsed against an explicit reverse relationship
  stays collapsed, because the handoff works from the parsed document.

## Manifest

```json
{
  "packageOrder": ["SPDXRef-A", "SPDXRef-B"],
  "revisions": [
    {"name": "rev-1", "json": "catalog-1.json"},
    {"name": "rev-2", "json": "catalog-2.json", "fileOrder": ["SPDXRef-F2", "SPDXRef-F1"]}
  ]
}
```

Run it with `pyspdxtools_handoff --manifest handoff.json --out-dir output/`, or
call `spdx_tools.spdx.handoff.batch.run_handoff` from Python. The result reports
the shared package order, the scoring revision with its explicit byte cost, per
revision costs, diagnostics for unwritable or conflicting rows, and reread
verification records.
