---
title: Bucket Reconciliation
weight: 6
---

When a histogram gains a new bucket boundary in a rolling deployment, two
scrapes of the same window can use different layouts. The canonical example is a
latency histogram whose old workshop only publishes the `500us` and `1000us`
lines while a new workshop adds a `750us` line between them.
`prometheus_client.bucket_reconcile` reconciles those overlapping scrapes into
window bucket counts without assuming anything that the raw ledgers do not
prove.

## What the reconciler guarantees

- A target line is **stamped with an exact count only when every feasible
  allocation gives the same value**. Otherwise the report gives the tight
  `[minimum, maximum]` range still compatible with every ledger.
- Pair queries test whether a specific `(count at bound A, count at bound B)`
  combination is possible. A feasible answer comes back with a concrete
  allocation; an infeasible answer names the snapshots and batches that rule it
  out.
- Ledger statements are joined into the same observations only when they
  reference the **same registered batch** (or the same per-process block). Equal
  labels or equal values never imply overlap, so nothing is deduplicated by
  accident.
- Buckets are half-open strips `(lo, hi]`, matching the receiver's
  upper-bound-inclusive `le` convention.
- Three figures carried by a bucket file stay distinct: the raw per-band
  increments, the cumulative `le` display, and the known integer sum of the
  observed latencies.

## Processes and restarts

Each process keeps its original multi-process file. When a process id is reused
after a restart, give the two incarnations different `generation` values. The
pair `(process_id, generation)` is an independent universe, so a post-restart
subtotal is never subtracted from the pre-restart counter.

## Input document

Reconciliation reads a small JSON document (the "small ledger" carries the few
integer latency values a registered batch may take; the "big ledger" carries
the value domain, batch ranges and the fixed boundary lines).

```json
{
  "manifest": {
    "metric_name": "request_latency_us",
    "lines": [
      {"bound": 500, "layouts": ["old", "new"], "introduced": "v1"},
      {"bound": 750, "layouts": ["new"], "introduced": "v2"},
      {"bound": 1000, "layouts": ["old", "new"], "introduced": "v1"}
    ],
    "value_domain": [0, null]
  },
  "ledgers": [
    {
      "ledger_id": "old-scrape",
      "layout": "old",
      "snapshot": {"snapshot_id": "snap-old", "path": "scrapes/old.db"},
      "blocks": [{
        "process_id": 7,
        "generation": 0,
        "increments": {"500": 2, "1000": 3},
        "cumulative": {"500": 2, "1000": 5},
        "sum": 3600,
        "batches": [{"batch_id": "b-9", "bound": 1000}]
      }]
    },
    {
      "ledger_id": "new-scrape",
      "layout": "new",
      "snapshot": {"snapshot_id": "snap-new", "path": "scrapes/new.db"},
      "blocks": [{
        "process_id": 7,
        "increments": {"500": 2, "750": 1, "1000": 3},
        "cumulative": {"500": 2, "750": 3, "1000": 6},
        "sum": 4400,
        "batches": [
          {"batch_id": "b-9", "bound": 750},
          {"batch_id": "b-12", "bound": 1000}
        ]
      }]
    }
  ],
  "batches": [
    {"batch_id": "b-9", "allowed_values": [600, 700, 750], "size": 1},
    {"batch_id": "b-12", "allowed_values": [800, 900], "size": 1}
  ],
  "target_bounds": [500, 750, 1000],
  "pair_queries": [
    {"bounds": [750, 1000], "counts": [4, 7]}
  ]
}
```

`increments` and `cumulative` are both accepted and cross-checked against each
other; a mismatch raises an error that names the offending snapshot. The
integer `sum` is validated against the reachable value interval of the bands.

## Command line

```
python -m prometheus_client.bucket_reconcile scrape.json \
    --target 500 --target 750 --target 1000 \
    --pair 750:4,1000:7
```

The output is JSON. Each target reports `minimum`, `maximum`, an `exact` flag
and, when it cannot be stamped, an `evidence` list explaining which snapshot and
boundary line leaves the split open.

## Programmatic API and ordinary scrapes

Ordinary same-layout multi-process scrapes keep using the existing
`MultiProcessCollector`. To reconcile them, build ledgers straight from the
per-process `histogram_<pid>.db` files (the helper reads the raw band
increments and derives the cumulative display separately):

```python
from prometheus_client.bucket_reconcile import (
    Manifest, Reconciler, Snapshot, ledger_from_mmap_files,
)

old = ledger_from_mmap_files(
    "old-scrape", "old", ["histogram_101.db"],
    Snapshot("snap-old", "histogram_101.db", "mmap"),
)
new = ledger_from_mmap_files(
    "new-scrape", "new", ["histogram_202.db"],
    Snapshot("snap-new", "histogram_202.db", "mmap"),
)
report = Reconciler(manifest, [old, new], batches).window_report(
    target_bounds=[500, 750, 1000],
    pair_queries=[{"bounds": [750, 1000], "counts": [4, 7]}],
)
```

## Why it stays tractable

The decision variables are integer counts per half-open strip, never one
variable per observed item or per possible microsecond. Items only one ledger
sees are residuals expressed as linear functions of the shared strip counts,
and the constraints decompose into small connected components. As a result the
runtime depends on the number of strips and ledgers, not on the magnitude of the
counts, so million-item windows do not expand into million-variable problems.
