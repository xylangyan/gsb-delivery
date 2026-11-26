# Relocation ledger

`relocation_ledger.py` computes where every sample of a collection index moves
when an offset-bearing RFC3339 label is folded to the equivalent `Z` label and
re-sorted by `strict_rfc3339.rfc3339_to_timestamp`.

The two library paths are not numerically identical. The old path is

    value = (float(local_whole_second) + float(fraction)) - offset

and the new path is

    value = float(local_whole_second - offset) + float(fraction)

Both additions round to the binade of the whole second they use. Folding the
offset changes that binade, so a sample can move by one or more grid steps, or
not at all. The library's decimal-to-double conversion and every subsequent
rounding are reproduced exactly with integers, so the ledger is independent of
the host FPU.

## Dossier format

```json
{
  "segments": [
    {
      "name": "A",
      "wall_start": "2038-01-19T04:15:00",
      "offset": "+02:00",
      "digits": 7,
      "tick_start": 2,
      "step_ticks": 1,
      "count": 100000000,
      "line_start": 1,
      "line_step": 2
    }
  ]
}
```

- `wall_start`: whole local second of the first sample.
- `offset`: `Z` or `+HH:MM` / `-HH:MM`.
- `digits`: fixed number of fractional digits.
- `tick_start`: integer value of the first fractional field.
- `step_ticks`: exact decimal step between consecutive samples, in ticks.
- `count`: number of samples.
- `line_start`, `line_step`: original line number and stride.

Each segment is a maximal run; original line order inside a segment is the
sample order. Segments may interleave line ranges but must not claim the same
line.

## Usage

```sh
.venv/bin/python relocation_ledger.py dossier.json --report ledger.json
.venv/bin/python relocation_ledger.py dossier.json --line 12345
.venv/bin/python relocation_ledger.py dossier.json --range 1000 2000
.venv/bin/python relocation_ledger.py dossier.json --sample A:0:9
```

`--line` reports the actual 1-based old and new positions, the position delta,
and whether the row stayed in place while its key changed. `--range` reports
the number of rows that really moved, the in-place key changes, and
displacement statistics for an inclusive original-line range. No range needs
to be ranked row by row.

## The shipped files

- `dossier.json`: the full two-station example (A with `+02:00`, B with `Z`),
  100,000,000 samples each, interleaved original lines.
- `ledger.json`: its relocation report.
- `sample_dossier.json`, `sample_ledger.json`: a small four-segment sample with
  different offsets, strides and fractional clocks, suitable for spot checks.
- `sample_library_sort.json`: the sample's rows with the real library's sort
  comparison keys and the ledger's old/new positions. All 810 rows agree.

## What the example shows

For `2038-01-19T04:15:00+02:00`, the local whole second is in binade 31
(ulp `2**-21`) while the UTC second is in binade 30 (ulp `2**-22`):

| fraction | old value (library) | equivalent Z value |
| --- | --- | --- |
| `.0000002` | `2147480100.0` | `2147480100.0000002` |
| `.0000003` | `2147480100.0000005` | `2147480100.0000002` |

So `.0000002` is one grid step lower than its equivalent Z label, while
`.0000003` is one step higher. With both stations present, rows that were
side by side exchange order, and some pairs land on exactly the same key. Ties
are broken by the old original-line order.

For the shipped dossier the exact results are:

| segment | rows | moved | max displacement | in-place key changed | identical |
| --- | ---: | ---: | ---: | ---: | ---: |
| A (`+02:00`) | 100,000,000 | 29,027,840 | 2 | 20,971,520 | 50,000,640 |
| B (`Z`) | 100,000,000 | 29,027,840 | 2 | 0 | 70,972,160 |

The A net displacement is `-24,999,680` positions; B is `+24,999,680`; the
two cancel exactly.

## Why a per-station monotone shift is not enough

Within one station, the returned value is non-decreasing, and the rank shift
at period-aligned samples is periodic. But the shift is not a single constant
range for all stations: the equality pattern, tie ordering and the rank shift
all depend on the other station. In the example the per-period delta
distribution is `{0: 55447, -1: 16384, +1: 3147, -2: 3147}` for A and the
mirror image for B. A ledger that only translated one station's key range
would miss both the swaps and the equal-key pairs.

## Long runs

For `D` fractional digits, advancing a sample by `5**D` ticks (`2**-D`
seconds) advances its key by exactly `2**-D` within a binade. The ledger:

1. Splits a range at every power-of-two binade change of every segment's local
   and UTC whole second; no periodicity is assumed across those points.
2. Inside each piece, computes one full period of rank deltas and verifies it
   against the next full period phase by phase. Two equal corner deltas cannot
   hide a changed interior.
3. Extends only a verified period by multiplication, then adds the tail.

If the period cannot be verified, the piece is split or evaluated directly;
if that would exceed the direct limit, an explicit error is raised instead of
reporting a guess.

## Equality families

`ledger.json` stores cross-segment equalities as compact families. A family is
a set of base index runs plus an index offset, valid over a block range:

    A[run_index + k*a_period]  ==  B[run_index + offset + k*b_period]
    key(k) = key(0) + k*key_step

The key step is exact (a power of two). For the shipped dossier there are 12
families (7 on the old key, 5 on the new key) accounting for 488,625,920
cross-station pairs. Each family's runs can be expanded to exact original
lines without walking the 200,000,000 shipped rows.

## Tests

```sh
.venv/bin/python -m unittest test_relocation_ledger test_strict_rfc3339 -v
```

The tests cover the exact key model against the real library, the prompt's
two fractions, brute-force position and range checks, forced periodic-path
comparisons against a library sort, and the long-range interior check.
