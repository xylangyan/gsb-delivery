# Errata night planner

Given a set of approved erratum batches, legacy signed slips and tonight's
designated readings, this library chooses which batches to ship tonight so
that:

- every legacy slip keeps pointing at the **original ticket** (a same-value
  swap to a different ticket is a failure, not a success);
- every designated reading is actually obtainable under the Daru
  `CategoricalIndex` `pos` / `subset` / `at` semantics;
- the total deferral cost of held-back batches is minimal;
- ties are resolved by sending the earlier approval numbers tonight.

Everything is pure Ruby stdlib. The long archive never has to be expanded:
material numbers and row positions are stored as arithmetic progressions, so
a legal table starting at position 1,000,000 and a batch covering a regular
run of original numbers stay symbolic throughout the search.

## Daru semantics reproduced

Verified against the vendored `Daru::CategoricalIndex` (`lib/daru/index`) with
200 randomized cross-checks in `test/errata_test.rb`:

- `pos(v)` first treats `v` as a category (numeric categories win over
  positions) and returns all positions carrying it; only if `v` is not a
  category is it treated as a bare position.
- repeated query values are kept, never merged (`pos(:甲, :甲)` keeps each
  occurrence).
- `pos` returning a single position yields the bare number; `subset` on that
  single result raises the old-library `NoMethodError` (`Integer#map`), and an
  unknown category raises `IndexError`. Both trip the registered `at` fallback
  when the work order declares one.
- `at` uses preprocessing/validation of `Daru::Index`, including ranges and
  negative positions.
- a number that was a position on one table can be a category or a position on
  the sub-table built from the previous step; every trace records the layer at
  which a number became a row position.

## Plan file

See `examples/feasible_plan.json` and `examples/infeasible_plan.json`.

- `tables[].materials`: `{first, step, count}` arithmetic progression of
  original material numbers, or an explicit array.
- `tables[].categories`: short template repeated cyclically over all rows.
- `batches[]`: fixed `id`, deferral `cost`, and per-table `targets` with
  `materials` (and optional explicit `positions`; otherwise row positions are
  derived from the material progression) plus `new_category`. A batch is
  either shipped whole or kept whole.
- `work_orders[]`: ordered `pos` / `subset` / `at` steps; `subset` may carry
  `fallback_at`; arguments may be `{"ref": "previous_step"}` to feed a scalar
  number from the previous table into the sub-table it actually used.
- `signatures.legacy` / `signatures.tonight`:
  - `table` + `tickets` (+ optional `allowed_results`, `match`) check rows by
    original material number;
  - `output` + `tickets` / `allowed_results` / `error_branch` anchor checks to
    a replay step (a slip with only one filled field is checked on that field
    alone);
  - `match: "any"` means the reading only has to be obtainable.

## Usage

```sh
ruby bin/errata_plan examples/feasible_plan.json
ruby bin/errata_plan --audit 104 examples/feasible_plan.json
ruby -Ilib test/errata_test.rb
```

When no selection can satisfy everything, the report is `feasible: false` and
includes an irreducible `unsat_core` of conflicting slips together with the
original observations, instead of presenting a same-value ticket swap as a
success.
