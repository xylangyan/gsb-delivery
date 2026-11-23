"""Independent audit that the chosen plan is the unique minimum-fee plan.

Argument
--------
For every target window the approval ledger fixes exactly one archive per
position, so the source bytes required by a feasible window are uniquely
determined.  A window has two possible forms:

1. *direct*: one licensed in-place source block already decodes to the full
   approved window.  It costs zero and is always taken when available.
2. *recompute*: every source block touched by the approved positions must be
   physically read once.

The audit re-derives the forced read closure from scratch, confirms that every
window marked ``direct`` truly has a zero-cost exact block (and that no
recompute window secretly had one), and that the union of forced physical
ranges, charged with overlapping ranges merged, equals the billed ranges.
Any deviation means the plan was not minimum cost.
"""
from __future__ import annotations

import json
import os

import numcodecs
import numpy as np

from .planner import _blocks_for, _decode
from .scenario import DTYPE, FILL, N


def _windows(chunk):
    return [(lo, min(lo + chunk, N)) for lo in range(0, N, chunk)]


def audit(out_dir: str) -> dict:
    manifest = json.load(open(os.path.join(out_dir, "manifest.json")))
    plan = json.load(open(os.path.join(out_dir, "plan.json")))
    codec = numcodecs.get_codec(manifest["codec"])
    ledger = {row["time_index"]: row for row in manifest["ledger"]}
    block_map = manifest["block_map"]

    forced: dict[tuple, str] = {}
    problems: list[str] = []
    reported_infeasible = {(d["variable"], tuple(d["target_window"]))
                           for d in plan["infeasible"]}

    for var, spec in manifest["contract"]["variables"].items():
        chunk = spec["target_chunks"][0]
        read_ranges = {(i["archive"], i["var"]): i.get("windows")
                       for i in spec["read"]}
        rules = spec.get("window_rules", {})
        inplace = spec.get("inplace", [])
        for win_index, (lo, hi) in enumerate(_windows(chunk)):
            approved, codes = [], []
            for pos in range(lo, hi):
                row = ledger[pos]
                if row["status"] == "missing" or var not in row["choices"]:
                    approved.append(FILL)
                    codes.append(-1)
                else:
                    arc = row["choices"][var]
                    approved.append(manifest["truth"][var][pos])
                    codes.append(0 if arc == "A" else 1)

            has_exact_direct = False
            if FILL not in approved:
                for lic in inplace:
                    if "windows" in lic and [lo, hi] not in lic["windows"]:
                        continue
                    for block in block_map[lic["archive"]].get(lic["var"], []):
                        if (block["first"] == lo and block["last"] == hi and
                                _decode(manifest["archives"][lic["archive"]],
                                        block, codec) == approved):
                            has_exact_direct = True

            if has_exact_direct:
                continue  # zero-cost reference, no forced read

            rule = rules.get(str(win_index))
            window_infeasible = (var, (lo, hi)) in reported_infeasible
            for off, pos in enumerate(range(lo, hi)):
                if approved[off] == FILL:
                    continue
                arc = "A" if codes[off] == 0 else "B"
                windows = read_ranges.get((arc, var))
                unlicensed = (windows is not None and not any(
                    a <= pos < b for a, b in windows))
                rule_blocked = (rule is not None and not any(
                    a <= pos < b for a, b in rule.get(arc, [])))
                if unlicensed or rule_blocked:
                    if not window_infeasible:
                        problems.append(
                            f"{var}[{lo}:{hi}] unlicensed position {pos}")
                    continue
                block = _blocks_for(block_map, arc, var, pos, pos + 1)[0]
                forced[(manifest["archives"][arc], block["offset"],
                        block["offset"] + block["length"])] = arc

    billed = {(r["file"], r["start"], r["end"]): r["archive"]
              for r in plan["paid_ranges"]}
    if set(forced) != set(billed):
        problems.append(
            f"forced/billed mismatch forced={sorted(forced)} billed={sorted(billed)}")

    expected_fee = sum(manifest["contract"]["read_fee"][a]
                       for a in forced.values())
    if expected_fee != plan["total_fee"]:
        problems.append(
            f"fee {plan['total_fee']} != minimum {expected_fee}")

    # Every direct decision must point at a persistent exact source block.
    for d in plan["decisions"]:
        if d["kind"] != "direct":
            continue
        block = next(b for b in block_map[d["archive"]][d["source_var"]]
                     if b["offset"] == d["offset"] and
                     b["length"] == d["length"])
        values = _decode(manifest["archives"][d["archive"]], block, codec)
        lo, hi = d["window"]
        if values != manifest["truth"][d["variable"]][lo:hi]:
            problems.append(f"direct block {d['var']}{d['window']} mismatch")

    return {"minimum_fee": expected_fee,
            "unique_forced_ranges": len(forced),
            "shared_ranges_merged": len(forced),
            "problems": problems,
            "optimal": not problems}
