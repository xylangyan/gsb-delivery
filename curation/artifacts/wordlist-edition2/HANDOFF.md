# Handoff: numbering context of this CLDF artifact

## How to identify the numbering context

- Read `archive_manifest.json` -> `numbering_context.catalogue_edition`: `edition-2`.
- BibTeX key space: catalogue edition 2 official holding codes.
- Do NOT guess from the shape of a key. The string "X1" is an official
  catalogue code in the edition-1 artifact but a supplier-local code in
  raw batch beta material; only the manifest and the per-batch mapping
  tables disambiguate it.

## Numbering layers

1. Stable holding id (e.g. R37, R88).
2. Supplier local code: meaningful only inside that batch mapping table.
   Alpha X1 and beta AB both mean R37; beta raw X1 means R88.
3. Catalogue official code, edition dependent: edition 1 gave R37 the
   official code X1; edition 2 replaced that official code with CAT37.

## Citation columns (CLDF source property)

- Table `FormTable` (forms.csv), local column `Witness_Ref`: word-form citations.
- Table `ExampleTable` (examples.csv), local column `Example_Ref`: example-sentence citations.

Citation strings keep their original order and bracket descriptions
(page ranges such as 3-5, sections such as Appendix A, repeated
citations of the same work). Only citation keys are rewritten.

## Batches and supplements packaged here

- Batch `alpha`: mapping `alpha/mapping.tsv`, original form citation field `Witness_Ref_Alpha`, original example citation field `Example_Ref_Alpha`.
- Batch `beta`: mapping `beta/mapping.tsv`, original form citation field `Witness_Ref_Beta`, original example citation field `Example_Ref_Beta`.
- Supplement `beta-supplement-2026-09` (raw material of batch `beta`): mapping `mapping.tsv`, original form citation field `citation_refs`, original example citation field `example_citation_refs`.

## Official-code migration (edition 1 -> edition 2)

- X1 -> CAT37 (holding R37)
- X2 -> CAT88 (holding R88)
