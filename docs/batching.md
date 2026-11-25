# Batches, References and Late Binding

{cattrs} normally structures one record at a time.
Sometimes records only make sense as a batch: two tables describe the same parts
from different angles, records refer to each other by registration number, and a
record may report only a few attributes it happens to know.
The {mod}`cattrs.batch` module resolves such a batch as a whole.

```{currentmodule} cattrs.batch

```

## Overview

A batch load has three ingredients:

- a {class}`BatchSpec` describing the tables,
- a {class}`TableSpec` per table, mapping labels, versions and columns onto the
  model, and
- rows grouped by table name.

```python
from attrs import define
from cattrs import Converter
from cattrs.batch import BatchSpec, Reference, TableSpec, structure_batch

@define
class Part:
    id: str

@define
class Gear(Part):
    ratio: float = 1.0

@define
class Frame(Part):
    length: float = 0.0

@define
class Assembly:
    id: str
    part: Part | None = None

spec = BatchSpec(
    tables={
        "receiving": TableSpec(
            Part, labels={"Part": "part", "Gear": "gear", "Frame": "frame"}
        ),
        "assembly": TableSpec(Assembly, labels={"Assembly": "A"}),
    },
    references={"assembly": {"part": Reference("part_id")}},
    id_fields={"receiving": "id", "assembly": "id"},
)

batch = structure_batch(
    Converter(),
    spec,
    {
        "receiving": [{"id": "p1", "type": "gear", "ratio": 2.5}],
        "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
    },
)

batch.by_registration("a1").part is batch.by_registration("p1")  # True
```

The registration number identifies one *occurrence*.
Every row carrying that number is a mention of the same occurrence, and all
mentions are merged before any type is committed.

## Labels and versions

Legacy and modern tables may use different labels for the same subtype.
{attr}`TableSpec.labels` is per table, so the same `Gear` can be `gear` in the
legacy table and `G` in the modern one.

A label for the table's root model is *coarse*: legacy tables label everything
with the base type, so a base label admits subtypes.  A subtype label is
*specific* and names that subtype.

{attr}`TableSpec.versions` restricts which types a table can express.
A missing version entry means "not reported", not "unavailable"; an explicit
`null` in a row is still a reported value and participates in validation.

```{versionadded} NEXT
```

## Whole-batch evidence

Types are chosen from the entire batch, not from the first row seen.
A base-only view may be seen before a reference that requires a finer subtype;
the reference narrows the earlier candidate set.
Sibling references that share a subtype narrow it jointly, and a relation's own
declared field type rules out interpretations that cannot satisfy it.

Evidence is propagated to a fixpoint in both directions:

- a reference's declared target type narrows the referenced occurrence, and
- the referenced occurrence's type narrows the holder's viable types.

References may arrive before their target rows.

## Identity

Each occurrence is structured exactly once.
Every resolved reference returns the same object as the occurrence itself, so
mutating `batch.by_registration("p1")` is visible through every reference to it.

## Ambiguity, conflicts and partial delivery

When a component cannot be resolved uniquely, {attr}`Batch.diagnostics` records:

- {attr}`BatchDiagnostics.ambiguities`, each listing the viable
  {class}`CandidateEvidence` types and the fields that distinguish them,
- {attr}`BatchDiagnostics.conflicts`, pointing at the conflicting declarations
  and the originating references,
- missing references and missing required fields,
- cycles that were not declared late-bound.

Components unrelated to a problem are still delivered.

Pass `strict=True` to {func}`structure_batch` (or on the `BatchSpec`) to raise a
{class}`BatchIncompleteError` subclass instead; {meth}`Batch.raise_for_errors`
does the same on demand.

## Late binding and cycles

Mutually recursive models must declare which relations may bind late.
A cycle is legal when every edge in the ring is late-bound; undeclared cycles
are reported as {class}`BatchCycleError`.

```python
from cattrs.batch import late_field

@define
class Node:
    id: str
    peer: "Node | None" = late_field(default=None)
```

Late relations may also be marked on the reference itself
(`Reference("peer_id", late=True)`) or per table in
{attr}`BatchSpec.late_attributes`.

Late binding happens in a second phase, after every object exists.
Models that want ordinary attribute access to fail with
{class}`BatchIncompleteError` before that phase can inherit {class}`BatchModel`
or use the {func}`watch_model` decorator.

## Export

{func}`export_batch` writes the batch back per table.
Each table receives its own labels and its own column names, so a legacy table
is written with legacy tags even when the record was resolved through a modern
sibling.  References are written back as registration numbers, and explicit
nulls are preserved.

{attr}`TableSpec.field_map` renames columns, {attr}`TableSpec.model_codes`
remaps model values to table-specific codes, and
{attr}`TableSpec.export_fields` restricts which columns a table may write.

```python
exported = export_batch(batch)
exported["receiving"][0]["type"]  # "gear", the legacy label
```

## Compatibility

The batch module is opt-in.  Converters that never call {func}`structure_batch`
keep the behavior described in the rest of this documentation.
The registration number is treated as a regular model field, so existing
atypical conversions continue to work unchanged.

## Table-specific columns and codes

A table may name its columns differently from the model.
{attr}`TableSpec.field_map` maps model attribute names to columns in either
direction, and is used both when importing and when exporting.

Values may also need translating per table, for example a status whose spelling
changed between versions.  {attr}`TableSpec.model_codes` maps model values to
codes per column and per type:

```python
TableSpec(
    Part,
    labels={"Part": "part", "Gear": "gear"},
    model_codes={"Gear": {"status": {"new": "N", "used": "U"}}},
)
```

## Late binding details

A relation may be marked late in three places, in increasing scope:

- on the field, with `late_field` or the `cattrs_late` field metadata,
- on the reference, with `Reference(..., late=True)`,
- per table, with `BatchSpec.late_attributes`.

A cycle is accepted when it is broken by at least one late edge.  Late
relations are bound in a second phase, after every occurrence has been
constructed; `Batch.complete` is only true once that phase finished.

## Diagnostics reference

{attr}`Batch.diagnostics` is a {class}`BatchDiagnostics` value with:

- `ambiguities`: viable types and distinguishing fields per occurrence,
- `conflicts`: contradictory declarations with the references that caused them,
- `missing_references`: table, attribute and unresolved registration,
- `missing_fields`: required fields no mention reported,
- `cycles`: reference cycles not covered by a late-bound edge,
- `delivered` and `skipped`: which components are available.

{meth}`BatchDiagnostics.explain` renders a human-readable report, and
{meth}`Batch.raise_for_errors` raises the matching error subclass.
