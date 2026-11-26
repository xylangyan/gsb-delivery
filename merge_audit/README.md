# Merge rule audit (restricted vs unrestricted Damerau-Levenshtein)

Both scorers use the local `textdistance` pure-Python implementation with
`external=False`. The transcription alias table is injected as `test_func`
(case-insensitive comparison), so original characters are never rewritten.

## Facts established against the library

- `bA` vs `AB`: restricted distance `1`, unrestricted distance `2`.
- Lowercasing first (`ba` vs `ab`) makes unrestricted return `1`; the alias
  table must stay inside `test_func`, not be applied to the strings.
- The unrestricted scorer keys its `da` table by the original character
  (`edit_based.py` `_pure_python_unrestricted`), so case aliases can make it
  score worse than the restricted scorer, never better at distance `1`.

## Threshold 1 verdict

- New-only admissions are impossible: any single edit (delete, insert,
  substitute, adjacent swap of case-aliased chars) is available to the
  restricted scorer. Exhaustive checks over several alphabets find zero
  new-only pairs.
- Old-only admissions exist and are exactly the case-alias adjacent swaps
  whose unrestricted `da` lookup is polluted (e.g. `bA`/`AB`).

## aA prefix family

With `F(k)` the alternating `aA` pattern truncated to length `k`:

- left head: `F(n-1) + b`
- right head: `F(n-2) + bA`

Restricted distance is always `1`. Unrestricted distance is `2` for even `n`
and `1` for odd `n`, so threshold 1 merges the pair at one length and splits
it again at the next. The shared long prefix does not collapse the lengths
into one verdict; parity of the terminal fragment decides it.

## Long bands

Legal length ranges can span hundreds of millions. Only the terminal
`threshold + 2` characters can differ, and their case is fixed by length
parity, so `audit_long_band` builds short suffix fragments directly and scores
a constant number of boundary/interior probes instead of one distance call per
length. Even lengths stay old-only; odd lengths stay released by both heads.

## Run

```
.venv/bin/python -m merge_audit.report
.venv/bin/pytest tests/test_merge_audit.py -q
```
