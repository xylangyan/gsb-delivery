# Nanosecond median/quantile shared locations

`numpy_groupies.shared_position_table` builds a legacy-reader map for the two ns
duration columns:

- `nanmedian`
- `nanquantile(q=0.5, method="linear")`

The input is global original-row order.  A window is the half-open row interval
`[start, start + length)`.  `NaT` rows are passed through to the NumPy backend
and excluded by the nan reduction; absent/interrupted rows are simply not present.

## Position rules

Let `n` be the number of non-`NaT` rows of one group in the window.

| Valid count | Sorted ranks used by both columns | Storage |
| --- | --- | --- |
| `0` | none | one shared `NaT` slot |
| odd `n = 2k + 1` | `k` | always one shared slot |
| even `n = 2k` | `k - 1`, `k` | one slot if result bits agree, otherwise two |

Ties at a rank choose the smallest global original row, using the key
`(tick, row_id, input_order)`.

For an even group with neighbour ticks `a <= b`, the table reproduces the
temporal NumPy backend exactly:

```text
median   = round_half_even((a + b) / 2)
quantile = b - int(float64(b - a) * float64(0.5))
```

The explicit branch in the module also handles `gamma < 0.5`; at `q=0.5` the
upper branch is the relevant one.  Do not assume an ordering between the two
results: exact median rounding and float64 conversion/multiplication can put
either column on top.

## Table and readback

```python
import numpy as np
from numpy_groupies.shared_position_table import (
    Archive, RegularSegment, WindowBatch, build_batches
)

segments = [
    RegularSegment(
        start_row=40,
        stride=5,
        group=1,
        ticks=np.array([7, 9, -(2**63), 11, 13], dtype=np.int64),
        missing_slots=[1],  # scheduled slot 1 was interrupted
    )
]
archive = Archive.from_segments(segments, group_count=2)
table = build_batches(
    archive,
    [WindowBatch.make(lengths=(2, 3, 4), start_range=(0, 100_000))],
)

read = table.read(start=120, length=3, group=1)
median_value, ranks, slot = table.legacy_read(
    "nanmedian", start=120, length=3, group=1
)
```

`read.median` and `read.quantile` expose:

- the `timedelta64[ns]` value;
- the integer slot and location string;
- whether the columns share that slot;
- the sorted rank or two ranks;
- `EvidencePoint(rank, row, tick)` neighbours and their global original rows.

`table.separate_cells()` yields the compact rows that need two slots, including
the neighbour ranks/rows/ticks.  `table.layout_runs(length, group)` gives
run-length compressed ranges marked wholly shared or wholly separate; gaps
between registered start ranges are never merged.

The cell table and interned slot bank can be saved with `table.save_npz(path)`
and reopened with `SharedPositionTable.load_npz(path)`.

For small batches, `build_position_table(..., verify_small=True)` runs every
window through `numpy_groupies.npg_numpy.aggregate` and compares both columns
and every table tick bit-for-bit.  `table.verify_window(start, length)` is also
available for spot checks.

Long ranges are swept monotonically.  Breakpoints occur only when a registered
row enters or leaves the window; intervals selecting the same valid rows share
one cell record.  A Fenwick tree supplies current sorted ranks, so adjacent
windows do not sort from scratch and never reuse stale neighbour identities.
