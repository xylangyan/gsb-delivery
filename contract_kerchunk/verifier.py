"""Re-open the published index and prove it reproduces the approved truth."""
from __future__ import annotations

import json
import os

import fsspec
import numpy as np
import zarr

from .scenario import FILL, N


def verify(out_dir: str) -> dict:
    manifest = json.load(open(os.path.join(out_dir, "manifest.json")))
    plan_doc = json.load(open(os.path.join(out_dir, "plan.json")))
    index_path = os.path.join(out_dir, "index.json")
    store = fsspec.get_mapper("reference://", fo=index_path)
    group = zarr.open_group(store, mode="r", zarr_format=2)

    report = {"checks": [], "ok": True}

    def check(name: str, condition: bool, detail: str = ""):
        report["checks"].append({"name": name, "passed": bool(condition),
                                 "detail": detail})
        report["ok"] &= bool(condition)

    coords = group["time"][:]
    check("coordinates", np.array_equal(coords, np.asarray(manifest["timestamps"])),
          f"time={coords.tolist()}")

    infeasible = {(d["variable"], tuple(d["target_window"]))
                  for d in plan_doc["infeasible"]}
    for var in manifest["contract"]["variables"]:
        arr = group[var][:]
        expected = np.asarray(manifest["truth"][var])
        feasible = np.ones(N, dtype=bool)
        for ivar, (lo, hi) in infeasible:
            if ivar == var:
                feasible[lo:hi] = False
        check(f"values::{var}",
              np.array_equal(arr[feasible], expected[feasible]),
              f"got={arr[feasible].tolist()} want={expected[feasible].tolist()}")
        true_gap = (expected == FILL) & feasible
        check(f"missing::{var}",
              np.array_equal(arr[feasible] == FILL, true_gap[feasible]),
              f"fill_positions={np.flatnonzero(true_gap).tolist()}")
        for _, (lo, hi) in {(v, w) for v, w in infeasible if v == var}:
            check(f"infeasible_is_fill::{var}{[lo, hi]}",
                  np.all(arr[lo:hi] == FILL),
                  "unlicensed window must not expose stale source bytes")

    # Every byte reference must resolve to a real, persistent local file and
    # must never be an ephemeral/inline value.  Metadata documents (.zarray,
    # .zattrs, .zgroup) are intentionally inline JSON; only data chunk keys
    # (a numeric final path component) are required to be remote references.
    import re
    chunk_key = re.compile(r"^[^.]+/\d+$")
    index = json.load(open(index_path))
    unresolved = []
    inline = []
    for key, ref in index["refs"].items():
        if chunk_key.match(key) and isinstance(ref, (str, bytes)):
            inline.append(key)
        if not isinstance(ref, list):
            continue
        uri, offset, length = ref
        if not os.path.isfile(uri):
            unresolved.append((key, uri))
            continue
        if offset + length > os.path.getsize(uri):
            unresolved.append((key, "range beyond EOF"))
    check("persistent_remote_refs", not unresolved, str(unresolved))
    check("no_inline_ephemeral_refs", not inline, str(inline))

    # Physical-range dedup: each (file, start, end) is billed exactly once.
    seen = set()
    duplicates = []
    for item in plan_doc["paid_ranges"]:
        rng = (item["file"], item["start"], item["end"])
        if rng in seen:
            duplicates.append(rng)
        seen.add(rng)
    check("range_dedup", not duplicates, str(duplicates))

    expected_fee = sum(manifest["contract"]["read_fee"][item["archive"]]
                       for item in plan_doc["paid_ranges"])
    check("fee_accounting", plan_doc["total_fee"] == expected_fee,
          f"billed={plan_doc['total_fee']} recomputed={expected_fee}")

    # Provenance: direct blocks keep their source archive label; recomputed
    # blocks record every physical read behind them.
    for decision in plan_doc["decisions"]:
        if decision["kind"] == "direct":
            check(f"provenance::{decision['variable']}{decision['window']}",
                  decision["archive"] in ("A", "B"))
        else:
            check(f"provenance::{decision['variable']}{decision['window']}",
                  len(decision["reads"]) >= 1)

    check("infeasible_reported",
          any(d["variable"] == "soil" and d["target_window"] == [6, 8]
              for d in plan_doc["infeasible"]),
          json.dumps(plan_doc["infeasible"]))
    with open(os.path.join(out_dir, "verification.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    return report
