# Reference index

`mistletoe.reference_index` builds a versioned index of the links, images and
link reference definitions in a Markdown document.  It is meant for documents
that are edited in place and re-indexed after every save, such as a review or
inspection handbook.

The index uses the normal mistletoe parser, so it follows mistletoe's built-in
Markdown rules.  In particular, definitions inside code blocks are not
definitions, and the first definition of a normalized label wins.

## Quick start

```python
from mistletoe.reference_index import ReferenceIndex

index = ReferenceIndex(open("manual.md").read())

for reference in index.index.references:
    print(reference.kind, reference.label, reference.destination,
          reference.line, reference.column)
```

Each `ReferenceRecord` names the normalized `label` it cites and the effective
`definition` it resolves to.  `reference_kind` is one of `inline`, `full`,
`collapsed`, `shortcut` or `autolink`.  Images are reported with
`kind == "image"`.

Definitions are available in `index.index.definitions`, and the first
definition of each label (the one that actually takes effect) is in
`index.index.effective_definitions`.  `index.index.resources` groups each
effective definition with the body references that use it.

## Applying edits

An editor normally reports the ranges it added or removed.  One call applies a
whole save; the batch is always interpreted against the previous version, and
exactly one new version is published:

```python
index.apply_edits([
    (1024, 1050, "replacement text"),
    (2048, 2048, "inserted text"),
])
```

Each edit may be:

- a `TextEdit(start, end, text)` using 0-based string offsets,
- a `LineColumnEdit(start_line, start_column, end_line, end_column, text)`
  using 1-based lines and columns,
- a `(start, end, replacement)` tuple,
- a mapping with the same keys.

A failed batch raises `ValueError` and leaves the index unchanged.  Overlapping
edits in one batch are rejected.

After a save, `index.changes` describes what needs review:

- `resources_to_recheck`: labels whose effective definition or body usage
  changed,
- `url_resources_to_recheck`: destinations used by inline links whose
  occurrence changed,
- `added_references`, `removed_references` and `changed_definitions` for
  details.

A pure line shift, such as inserting a paragraph above a definition, does not
mark that definition as changed.

## Versions

Every version is immutable and keeps its own source text and positions:

```python
old = index.version(0)          # positions in the first version
new = index.version(-1)         # positions in the latest version
old.references[0].line          # unchanged by later saves
```

`index.verify()` re-parses the current version from scratch and compares its
definitions, references and ranges with the incremental result.
`index.verify_all()` does the same for every kept version.

## Reuse

Unchanged blocks keep their previously parsed inline tokens; only blocks whose
source text changed, or whose referenced labels resolve differently, are
parsed again.  `index.index.stats` reports `inline_parsed_blocks`,
`reused_blocks` and `total_blocks`, which is useful when checking that an edit
touch only the expected part of a large document.
