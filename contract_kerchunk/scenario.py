"""Deterministic local multi-archive scenario for the Kerchunk contract task.

The scenario is deliberately tiny so every byte address and every fee can be
audited by hand, while still exercising every rule in the contract:

* two source archives A and B with different on-disk chunk grids;
* variables whose blocks sometimes point at the *same* physical bytes
  (``gust`` reuses the wind blocks of archive B);
* a time axis containing a duplicated/conflicting time point and a true gap;
* an approval ledger that decides the truth per time point and per variable;
* a contract stating the allowed target chunk grid, the ranges that may be
  referenced in place, the ranges that may be physically read for
  recomputation and the fee charged per physical range read.

Everything is written under ``out/`` and referenced by absolute ``file://``
URIs so the published index stays valid after the build process exits.
"""
from __future__ import annotations

import json
import os

import numcodecs
import numpy as np

DTYPE = "<i4"
FILL = -9999
N = 8
# Hours since an epoch.  Index 5 is the true gap (no archive holds a reading
# that the ledger accepts); index 2 is duplicated/conflicting between A and B.
TIMESTAMPS = [100, 110, 120, 130, 140, 150, 160, 170]

# Approved ground-truth values per variable.  FILL marks a true missing value.
TRUTH = {
    # A and B disagree at index 6 (B holds the corrupt value 99); the ledger
    # approves A for temp.
    "temp": [10, 11, 12, 13, 14, FILL, 16, 17],
    "rh": [50, 51, 52, 53, 54, FILL, 56, 57],
    "wind": [100, 101, 102, 103, 104, FILL, 106, 107],
    # gust stores exactly the wind bytes; same approved values.
    "gust": [100, 101, 102, 103, 104, FILL, 106, 107],
    # soil only exists on A; the back-half window [6, 8] is never licensed.
    "soil": [1, 2, 3, 4, 5, FILL, 7, 8],
}

# Raw values held by each archive before the ledger is applied.
ARCHIVE_VALUES = {
    "A": {
        "temp": [10, 11, 12, 13, 14, 15, 16, 17],
        "rh": [50, 51, 52, 53, 54, 55, 56, 57],
        "wind": [100, 101, 102, 103, 104, 105, 106, 107],
        "soil": [1, 2, 3, 4, 5, 6, 7, 8],
    },
    "B": {
        "temp": [10, 11, 12, 13, 14, 15, 99, 17],
        "rh": [50, 51, 52, 53, 54, 55, 56, 57],
        "wind": [100, 101, 102, 103, 104, 105, 106, 107],
    },
}

# On-disk chunk grid (logical block size) used by each archive/variable.
SOURCE_GRIDS = {
    "A": {"temp": 4, "rh": 4, "wind": 8, "soil": 4},
    "B": {"temp": 2, "rh": 2, "wind": 2},
}

# Read fee charged for one physical range read on each archive.
READ_FEE = {"A": 3, "B": 2}


def _zstd():
    return numcodecs.get_codec({"id": "zstd", "level": 3})


def build(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    codec = _zstd()
    block_map: dict = {}
    archives = {name: os.path.join(out_dir, f"archive_{name}.bin")
                for name in ("A", "B")}
    buffers = {name: bytearray() for name in archives}

    def add_block(archive: str, var: str, first: int, values: list[int]):
        raw = np.asarray(values, dtype=DTYPE).tobytes()
        comp = codec.encode(raw)
        buf = buffers[archive]
        offset = len(buf)
        buf.extend(comp)
        block_map.setdefault(archive, {}).setdefault(var, []).append(
            {"offset": offset, "length": len(comp),
             "first": first, "last": first + len(values)}
        )

    for archive, variables in ARCHIVE_VALUES.items():
        for var, values in variables.items():
            grid = SOURCE_GRIDS[archive][var]
            for first in range(0, N, grid):
                add_block(archive, var, first, values[first:first + grid])

    # gust on B reuses the *same physical bytes* as B/wind blocks: no new
    # bytes are written, the block map simply points at the wind ranges.
    block_map["B"]["gust"] = [dict(b) for b in block_map["B"]["wind"]]
    # gust on A is a normal grid-4 copy.
    for first in range(0, N, 4):
        add_block("A", "gust", first, TRUTH["gust"][first:first + 4])

    for name, path in archives.items():
        with open(path, "wb") as fh:
            fh.write(buffers[name])

    ledger = [
        {"time_index": 0, "timestamp": 100, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "A", "gust": "B",
                     "soil": "A"}},
        {"time_index": 1, "timestamp": 110, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "A", "gust": "B",
                     "soil": "A"}},
        # duplicated time point: A and B conflict for temp; ledger keeps A.
        # For wind the A back-half license is withheld, so position 2 is
        # sourced from B even though A holds the identical value.
        {"time_index": 2, "timestamp": 120, "status": "approved",
         "duplicate": True,
         "choices": {"temp": "A", "rh": "B", "wind": "B", "gust": "B",
                     "soil": "A"}},
        {"time_index": 3, "timestamp": 130, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "B", "gust": "B",
                     "soil": "A"}},
        {"time_index": 4, "timestamp": 140, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "B", "gust": "B",
                     "soil": "A"}},
        # true gap: neither archive contributes an accepted reading.
        {"time_index": 5, "timestamp": 150, "status": "missing",
         "choices": {}},
        {"time_index": 6, "timestamp": 160, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "B", "gust": "B",
                     "soil": "A"}},
        {"time_index": 7, "timestamp": 170, "status": "approved",
         "choices": {"temp": "A", "rh": "B", "wind": "B", "gust": "B",
                     "soil": "A"}},
    ]

    contract = {
        "read_fee": READ_FEE,
        "variables": {
            # target grid 4 == A's native grid: block 0 is a direct reference,
            # block 1 must be recomputed because of the gap at index 5.
            "temp": {
                "target_chunks": [4],
                "inplace": [{"archive": "A", "var": "temp"}],
                "read": [{"archive": "A", "var": "temp"},
                         {"archive": "B", "var": "temp"}],
            },
            # target grid 2 == B's native grid.
            "rh": {
                "target_chunks": [2],
                "inplace": [{"archive": "B", "var": "rh"}],
                "read": [{"archive": "A", "var": "rh"},
                         {"archive": "B", "var": "rh"}],
            },
            # A stores a single grid-8 compressed block, B stores grid-2
            # blocks; target grid 4 can only be built by recomputation, with
            # per-sub-window source licensing.
            "wind": {
                "target_chunks": [4],
                "inplace": [],
                "read": [{"archive": "A", "var": "wind"},
                         {"archive": "B", "var": "wind"}],
                # window index -> archive -> logical sub-windows that archive
                # is allowed to contribute.  A's back half is explicitly not
                # licensed even though it holds the same values as B; B
                # covers the second window except the true gap at index 5.
                "window_rules": {
                    "0": {"A": [[0, 2]], "B": [[2, 4]]},
                    "1": {"A": [], "B": [[4, 8]]},
                },
            },
            # B blocks are byte-identical to B/wind; in-place license only
            # covers blocks 2 and 3 so the first two windows share B reads
            # with the wind recomputation.
            "gust": {
                "target_chunks": [2],
                "inplace": [{"archive": "B", "var": "gust",
                             "windows": [[4, 6], [6, 8]]}],
                "read": [{"archive": "A", "var": "gust"},
                         {"archive": "B", "var": "gust"}],
            },
            # soil lives only on A.  The read license covers the first three
            # target windows; the back window [6, 8] has no licensed source at
            # all, so it must be reported as an unsatisfiable permission gap.
            "soil": {
                "target_chunks": [2],
                "inplace": [],
                "read": [{"archive": "A", "var": "soil",
                          "windows": [[0, 6]]}],
                "window_rules": {
                    "0": {"A": [[0, 2]]},
                    "1": {"A": [[2, 4]]},
                    "2": {"A": [[4, 6]]},
                    "3": {"A": []},
                },
            },
        },
    }

    manifest = {
        "archives": {k: os.path.abspath(v) for k, v in archives.items()},
        "block_map": block_map,
        "ledger": ledger,
        "contract": contract,
        "truth": TRUTH,
        "timestamps": TIMESTAMPS,
        "dtype": DTYPE,
        "fill": FILL,
        "codec": {"id": "zstd", "level": 3},
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest
