# Merge example

This directory demonstrates the multi-institution merge end to end. The
`tools/bibmerge.rb` program reads a handover manifest plus one BibTeX file per
institution and writes a single file that reopens with `BibTeX.open`, expands
with `Bibliography#replace` / `#join`, and answers queries with `Entry#get`.

## Files

- `manifest.json` — the handover: institution files, formal macro names with
  their candidate definitions, formal conference keys with candidate parents,
  and the children belonging to each conference.
- `institutions/alpha/library.bib`, `institutions/beta/library.bib` — the
  original per-institution records.
- `merged.bib` — the delivered bibliography.
- `report.json` — the chosen macros/parents, every changed child field, the
  alternative costs, and the per-institution verification result.

## Running it

```sh
ruby tools/bibmerge.rb --manifest merge-example/manifest.json \
                       --out merge-example/merged.bib \
                       --report merge-example/report.json

ruby tools/bibquery.rb merge-example/merged.bib
ruby tools/bibquery.rb merge-example/merged.bib --query lovelace2024
```

`tools/bibmerge.rb` writes nothing if the handover cannot be reproduced; it
prints the conflicting field and the institution that has or lacks it and
exits non-zero.

## What the example exercises

The manifest declares four formal macros. `conf:base` and `conf:icse` form a
two-level chain: `conf:icse` is `"Proceedings of " # conf:base`. Alpha and Beta
define both differently.

`conf:icse-2024` has two candidate parents:

| candidate | institution | title | year | publisher |
| --- | --- | --- | --- | --- |
| 0 | Alpha | `conf:icse` | 2024 | — |
| 1 | Beta | `conf:icse` | 2024 | — |

The parents are only usable verbatim with a matching macro choice. Keeping
Alpha's macro chain makes Alpha's parent available, and then Beta's two
children each need their Alpha-incompatible values written out. Keeping Beta's
chain makes Beta's parent available, and only Alpha's one child needs two
values written out (its `publisher`, which Beta's parent lacks, and its
`booktitle`, which resolves through Alpha's own alias).

The solver therefore chooses Beta's macros and Beta's parent: two changed
child fields instead of four. This is the case where the kept macro changes
which parent is cheaper. Both costs appear under `alternatives` in
`report.json`.

The unrelated `ws:*` macros and `conf:ws-2022` conference form a separate
group. The report lists it as a separate component, and the solver never forms
the product of the two groups' choices. The two citations also form their own
group.

## Delivered child fields

`report.json` lists the two writes, both on Alpha's child:

- `lovelace2024` replaces its explicit `booktitle = conf:icse` with the exact
  value `{Proc. International Conference on Software}`. The expression itself
  is preserved only while its kept macro still expands to the original value;
  keeping Beta's macro would silently change it, so the exact value is written.
- `lovelace2024` gains an explicit `publisher = {ACM}` because the kept Beta
  parent has no publisher, while Alpha's original parent did.

Everything else keeps its original crossref. No crossref was deleted.
