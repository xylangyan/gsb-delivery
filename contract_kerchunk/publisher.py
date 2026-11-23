"""Publish the minimum-cost plan as a re-readable kerchunk reference store."""
from __future__ import annotations

import json
import os

import numcodecs
import numpy as np

from .scenario import DTYPE, FILL, N


def _zarray(shape_chunk: int, compressor: dict):
    return json.dumps({
        "zarr_format": 2,
        "shape": [N],
        "chunks": [shape_chunk],
        "dtype": DTYPE,
        "compressor": compressor,
        "filters": None,
        "fill_value": FILL,
        "order": "C",
        "dimension_separator": ".",
    })


def publish(manifest: dict, plan_doc: dict, out_dir: str) -> dict:
    published_dir = os.path.join(out_dir, "published")
    os.makedirs(os.path.join(published_dir, "time"), exist_ok=True)
    time_blob = os.path.join(published_dir, "time", "coords.bin")
    time_bytes = np.asarray(manifest["timestamps"], dtype="<i8").tobytes()
    with open(time_blob, "wb") as fh:
        fh.write(time_bytes)

    refs: dict[str, object] = {
        ".zgroup": json.dumps({"zarr_format": 2}),
        ".zattrs": json.dumps({"title": "merged station archive"}),
        "time/.zgroup": json.dumps({"zarr_format": 2}),
        "time/.zattrs": json.dumps({"_ARRAY_DIMENSIONS": ["time"],
                                    "units": "hours since epoch"}),
        "time/.zarray": json.dumps({
            "zarr_format": 2, "shape": [N], "chunks": [N],
            "dtype": "<i8", "compressor": None, "filters": None,
            "fill_value": None, "order": "C", "dimension_separator": ".",
        }),
        "time/0": [os.path.abspath(time_blob), 0, len(time_bytes)],
    }

    chunk_index: dict[tuple, dict] = {
        (d["variable"], tuple(d["window"])): d for d in plan_doc["decisions"]
    }
    infeasible = {(d["variable"], tuple(d["target_window"]))
                  for d in plan_doc["infeasible"]}
    codec = numcodecs.get_codec(manifest["codec"])
    for var, spec in manifest["contract"]["variables"].items():
        chunk = spec["target_chunks"][0]
        refs[f"{var}/.zgroup"] = json.dumps({"zarr_format": 2})
        refs[f"{var}/.zattrs"] = json.dumps({"_ARRAY_DIMENSIONS": ["time"]})
        refs[f"{var}/.zarray"] = _zarray(chunk, manifest["codec"])
        for key_index, lo in enumerate(range(0, N, chunk)):
            hi = min(lo + chunk, N)
            key = f"{var}/{key_index}"
            if (var, (lo, hi)) in infeasible:
                # The window cannot be assembled from licensed bytes.  Persist
                # a fill-only block (zero source reads, zero fee) instead of
                # leaving a dangling reference; the gap is still reported in
                # plan.json as a missing source-data permission.
                fill_blob = os.path.join(
                    published_dir, var, f"unlicensed_{lo}_{hi}.bin")
                os.makedirs(os.path.dirname(fill_blob), exist_ok=True)
                payload = codec.encode(
                    np.full(hi - lo, FILL, dtype=DTYPE).tobytes())
                with open(fill_blob, "wb") as fh:
                    fh.write(payload)
                refs[key] = [os.path.abspath(fill_blob), 0, len(payload)]
                continue
            decision = chunk_index[(var, (lo, hi))]
            if decision["kind"] == "direct":
                refs[key] = [decision["uri"], decision["offset"],
                             decision["length"]]
            else:
                refs[key] = [decision["published_uri"], decision["offset"],
                             decision["length"]]

    store = {"version": 1, "refs": refs}
    path = os.path.join(out_dir, "index.json")
    with open(path, "w") as fh:
        json.dump(store, fh)
    return store
