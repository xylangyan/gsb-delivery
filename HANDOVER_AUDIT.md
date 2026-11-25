# H3 handover audit

`h3_handover.py` audits ownership-allocation tables stored around H3 parent cells and child-position ranges. It counts fine-cell edges between two groups without enumerating billions of target cells or materializing every edge.

## Input rows

Rows are applied in list order. Later rows override earlier rows.

```python
rows = [
    {'id': 'p-full', 'parent': '85283083fffffff', 'group': 'A'},
    {'id': 'segment', 'parent': '85283083fffffff',
     'start': 10, 'end': 80, 'group': 'B'},
    {'id': 'cancel', 'parent': '85283083fffffff',
     'start': 20, 'end': 25, 'cancel': True},
]
```

- Omitting `start` and `end` assigns all target-resolution descendants.
- `[start, end)` uses H3 child positions from `h3.cell_to_child_pos`.
- `end_inclusive` / `last` is accepted for inclusive input.
- `cancel: true`, `action: 'cancel'`, `group: None`, and common Chinese keys are accepted.
- Tuple rows are `(parent, group, start?, end?, cancel?)`.

## Compare two versions

```python
import h3_handover as hh

index = hh.HandoverIndex(rows_v1, rows_v2, resolution=12).prepare()
result = index.query('A', 'B')

continuing, new, disappeared = result
print(result.summary())
```

The three accounting numbers are:

- `continuing`: present in both versions, counted once.
- `new`: present only in version two.
- `disappeared`: present only in version one.

`result.unique_pairs` is their union size. `result.v1_edges` and `result.v2_edges` are per-version totals.

## Trace an edge

```python
trace = result.trace_edge(cell_a, cell_b)
print(trace.status, trace.in_v1, trace.in_v2)
print(trace.v1[0].row, trace.v2[0].row)
```

Each endpoint provenance contains the last effective row in that version. A cancellation row is returned when it is the last effective operation. `VersionIndex.history(cell)` returns all matching rows, including superseded rows.

## Compression and verification

Child-position intervals are decomposed into canonical H3 subtree blocks. Adjacent same-resolution blocks at resolution `r` have exactly `3 ** (target_resolution - r)` crossing fine edges; pentagons are handled by H3 child-position and neighbor calls. Candidate block pairs are found with H3 ring/ancestor checks, not a full cell scan.

For short lists, compare against an ordinary cell-by-cell replay:

```python
replay = hh.replay_counts(rows_v1, rows_v2, 8, 'A', 'B')
assert result.counts == (
    replay['continuing'], replay['new'], replay['disappeared']
)
```

Bounding-box parent candidates are available through `hh.bbox_parent_candidates(...)`; `hh.verified_parent_pairs(...)` filters them with exact H3 hierarchy/adjacency checks.

## CLI

```bash
python h3_handover.py payload.json --replay
python h3_handover.py v1.json v2.json --resolution 8 --groups A B --replay
```

Run the tests:

```bash
.venv/bin/python -m pytest tests/test_handover_audit.py -o addopts=''
```
