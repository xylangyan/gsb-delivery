"""Minimum-cost planner that turns the scenario into a published kerchunk index.

Cost model
----------
* A target window that an in-place license covers and whose source block
  decodes to the ledger-approved values is a *direct* remote reference and
  costs nothing at build time.
* Every other window is *recomputed*.  The approved values are assembled into
  a new compressed block that is persisted in the published bundle and
  referenced remotely; ephemeral reads are never embedded in the index.
* Recomputation requires physically reading whole source blocks.  A physical
  range is identified by ``(file, start, end)``; overlapping windows or
  different variables sharing the exact same bytes pay only once.

The source archive for every position is fixed by the approval ledger, so the
set of forced reads is unique.  Preferring every zero-cost direct reference and
unioning the forced reads therefore reaches the provable minimum fee.
"""
from __future__ import annotations

import json
import os

import numcodecs
import numpy as np

from .scenario import DTYPE, FILL, N, build


def _windows(chunk: int):
    return [(lo, min(lo + chunk, N)) for lo in range(0, N, chunk)]


def _blocks_for(block_map: dict, archive: str, var: str, lo: int, hi: int):
    out = []
    for block in block_map[archive][var]:
        if block["first"] < hi and block["last"] > lo:
            out.append(block)
    return out


def _decode(path: str, block: dict, codec) -> list[int]:
    with open(path, "rb") as fh:
        fh.seek(block["offset"])
        comp = fh.read(block["length"])
    raw = codec.decode(comp)
    return list(np.frombuffer(raw, dtype=DTYPE))


def _range_key(manifest: dict, archive: str, block: dict):
    return (manifest["archives"][archive], block["offset"],
            block["offset"] + block["length"])


def plan(out_dir: str) -> dict:
    manifest = build(out_dir)
    codec = numcodecs.get_codec(manifest["codec"])
    ledger = {row["time_index"]: row for row in manifest["ledger"]}
    block_map = manifest["block_map"]
    contract = manifest["contract"]
    fee = contract["read_fee"]

    decisions = []
    infeasible = []
    paid_ranges: dict[tuple, str] = {}

    published_dir = os.path.join(out_dir, "published")
    os.makedirs(published_dir, exist_ok=True)

    for var, spec in contract["variables"].items():
        chunk = spec["target_chunks"][0]
        read_ranges = {}
        for item in spec["read"]:
            read_ranges[(item["archive"], item["var"])] = item.get("windows")
        window_rules = spec.get("window_rules", {})
        for win_index, (lo, hi) in enumerate(_windows(chunk)):
            approved = []
            sources = []
            for pos in range(lo, hi):
                row = ledger[pos]
                if row["status"] == "missing" or var not in row["choices"]:
                    approved.append(FILL)
                    sources.append(-1)
                    continue
                archive = row["choices"][var]
                approved.append(manifest["truth"][var][pos])
                sources.append({"A": 0, "B": 1}[archive])

            direct = _try_direct(manifest, spec, var, lo, hi, approved,
                                 sources, codec)
            if direct is not None:
                decisions.append(direct)
                continue

            recompute = _recompute(
                manifest, spec, var, win_index, lo, hi, approved, sources,
                read_ranges, window_rules, codec, paid_ranges,
                published_dir,
            )
            if recompute is None:
                allowed = [f"{a}/{v}{w or ''}" for (a, v), w in
                           sorted(read_ranges.items())]
                infeasible.append({
                    "variable": var,
                    "target_window": [lo, hi],
                    "missing_permission": (
                        f"no licensed source block covers the approved "
                        f"positions of {var}[{lo}:{hi}]; read license only "
                        f"covers {allowed} under the stated window rules"
                    ),
                })
            else:
                decisions.append(recompute)

    total_fee = sum(fee[archive] for archive in paid_ranges.values())
    plan_doc = {
        "decisions": decisions,
        "infeasible": infeasible,
        "paid_ranges": [
            {"file": f, "start": s, "end": e, "archive": a}
            for (f, s, e), a in sorted(paid_ranges.items(),
                                       key=lambda kv: kv[0])
        ],
        "total_fee": total_fee,
    }
    with open(os.path.join(out_dir, "plan.json"), "w") as fh:
        json.dump(plan_doc, fh, indent=2)
    return manifest, plan_doc


def _try_direct(manifest, spec, var, lo, hi, approved, sources, codec):
    if any(v == FILL for v in approved):
        return None
    for lic in spec.get("inplace", []):
        if "windows" in lic and [lo, hi] not in lic["windows"]:
            continue
        archive, src_var = lic["archive"], lic["var"]
        for block in manifest["block_map"][archive].get(src_var, []):
            if block["first"] != lo or block["last"] != hi:
                continue
            values = _decode(manifest["archives"][archive], block, codec)
            if values == approved:
                return {
                    "variable": var, "window": [lo, hi], "kind": "direct",
                    "archive": archive, "source_var": src_var,
                    "uri": manifest["archives"][archive],
                    "offset": block["offset"], "length": block["length"],
                    "fee": 0,
                }
    return None


def _recompute(manifest, spec, var, win_index, lo, hi, approved, sources,
               read_ranges, window_rules, codec, paid_ranges,
               published_dir):
    rules = window_rules.get(str(win_index))
    needed_blocks = []
    seen = set()
    for offset_in_window, pos in enumerate(range(lo, hi)):
        if approved[offset_in_window] == FILL:
            continue
        code = sources[offset_in_window]
        archive = "A" if code == 0 else "B"
        if (archive, var) not in read_ranges:
            return None
        licensed_windows = read_ranges[(archive, var)]
        if licensed_windows is not None and not any(
                a <= pos < b for a, b in licensed_windows):
            return None
        if rules is not None:
            permitted = any(a <= pos < b
                            for a, b in rules.get(archive, []))
            if not permitted:
                return None
        blocks = _blocks_for(manifest["block_map"], archive, var, pos, pos + 1)
        if not blocks:
            return None
        block = blocks[0]
        key = (archive, block["offset"], block["length"])
        if key not in seen:
            seen.add(key)
            needed_blocks.append((archive, block))
        # sanity: the licensed bytes must actually carry the approved value
        values = _decode(manifest["archives"][archive], block, codec)
        local = pos - block["first"]
        if values[local] != approved[offset_in_window]:
            return None

    for archive, block in needed_blocks:
        paid_ranges.setdefault(
            _range_key(manifest, archive, block), archive
        )

    raw = np.asarray(approved, dtype=DTYPE).tobytes()
    comp = codec.encode(raw)
    var_dir = os.path.join(published_dir, var)
    os.makedirs(var_dir, exist_ok=True)
    blob = os.path.join(var_dir, f"chunk_{lo}_{hi}.bin")
    with open(blob, "wb") as fh:
        offset = 0
        fh.write(comp)
    return {
        "variable": var, "window": [lo, hi], "kind": "recompute",
        "reads": [
            {"archive": a, "uri": manifest["archives"][a],
             "offset": b["offset"], "length": b["length"]}
            for a, b in needed_blocks
        ],
        "published_uri": os.path.abspath(blob),
        "offset": offset, "length": len(comp),
        "fee": 0,
    }
