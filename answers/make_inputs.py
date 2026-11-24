#!/usr/bin/env python3
"""重建台架停机前的两卷 TProfile 存档及留存附表（合并管线的输入材料）。

产物（answers/data/）：
- standA_profiles.root / standB_profiles.root：两卷旧存档（旧校准、旧分箱）
- appendix_calibration.csv：各文件轴范围与旧校准系数
- appendix_batches.csv：分批分箱统计附表（旧校准下；humid 缺 sumw2 列值）
- appendix_overlap.csv：重叠批 B03 附表（两卷各自旧校准下的合并统计）
- preview_standB_press.csv：卷二 press 预览（均值 + 相同的显示误差）
- ledger_equal_weight.csv：台账——证实 humid 各批等权 1
- new_report_spec.json：新报告口径（区间、新校准、误差口径、超界箱口径）
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import uproot

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario as sc

DATA = Path(__file__).resolve().parent / "data"
STAT_FIELDS = ("n", "sumw", "sumwy", "sumwy2", "sumw2", "sumwx", "sumwx2")


def zero_stats():
    return dict.fromkeys(STAT_FIELDS, 0.0)


def accumulate_into(stats, x, y, w, cal):
    a, b = cal
    yo = a * y + b
    stats["n"] += 1
    stats["sumw"] += w
    stats["sumwy"] += w * yo
    stats["sumwy2"] += w * yo * yo
    stats["sumw2"] += w * w
    stats["sumwx"] += w * x
    stats["sumwx2"] += w * x * x


def cell_index(x, edges):
    if x < edges[0]:
        return 0
    if x >= edges[-1]:
        return len(edges)
    return int(np.searchsorted(edges, x, side="right"))


def cell_range(icell, edges):
    if icell == 0:
        return (float("-inf"), edges[0])
    if icell == len(edges):
        return (edges[-1], float("inf"))
    return (edges[icell - 1], edges[icell])


def build_cells(group, stand):
    edges = sc.OLD_BINNING[(stand, group)]
    cells = [zero_stats() for _ in range(len(edges) + 1)]
    cal = sc.OLD_CAL[stand]
    for fills in sc.FILLS[group][stand].values():
        for x, y, w in fills:
            accumulate_into(cells[cell_index(x, edges)], x, y, w, cal)
    return edges, cells


def write_root_files():
    for stand in ("A", "B"):
        path = DATA / f"stand{stand}_profiles.root"
        groups = [g for (s, g) in sc.OLD_BINNING if s == stand]
        with uproot.recreate(path) as fout:
            for group in groups:
                edges, cells = build_cells(group, stand)
                nbins = len(edges) - 1
                regular = cells[1:-1]
                keep_sumw2 = (stand, group) in sc.STORE_SUMW2
                kwargs = {}
                if edges != list(np.linspace(edges[0], edges[-1], nbins + 1)):
                    kwargs["fXbins"] = np.array(edges, np.float64)
                fout[group] = uproot.writing.identify.to_TProfile(
                    fName=group,
                    fTitle=f"{group} stand{stand} old-calibration archive",
                    data=np.array([c["sumwy"] for c in cells], np.float64),
                    fEntries=float(sum(c["n"] for c in cells)),
                    fTsumw=float(sum(c["sumw"] for c in regular)),
                    fTsumw2=float(sum(c["sumw2"] for c in regular)),
                    fTsumwx=float(sum(c["sumwx"] for c in regular)),
                    fTsumwx2=float(sum(c["sumwx2"] for c in regular)),
                    fTsumwy=float(sum(c["sumwy"] for c in regular)),
                    fTsumwy2=float(sum(c["sumwy2"] for c in regular)),
                    fSumw2=np.array([c["sumwy2"] for c in cells], np.float64),
                    fBinEntries=np.array([c["sumw"] for c in cells], np.float64),
                    fBinSumw2=np.array(
                        [c["sumw2"] for c in cells] if keep_sumw2 else [], np.float64
                    ),
                    fXaxis=uproot.writing.identify.to_TAxis(
                        fName="xaxis",
                        fTitle="",
                        fNbins=nbins,
                        fXmin=edges[0],
                        fXmax=edges[-1],
                        **kwargs,
                    ),
                    fErrorMode=0,
                    fStatOverflows=0,  # 旧设置 kIgnore：总体统计不含超界箱
                )
        print(f"wrote {path}")


def fmt(v):
    return f"{v:.17g}"


def write_appendix_calibration():
    with open(DATA / "appendix_calibration.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stand", "axis_min", "axis_max", "old_scale", "old_offset"])
        for stand, (a, b) in sc.OLD_CAL.items():
            w.writerow([stand, fmt(sc.AXIS_RANGE[0]), fmt(sc.AXIS_RANGE[1]), fmt(a), fmt(b)])


def iter_batch_cell_rows():
    for (stand, group), edges in sorted(sc.OLD_BINNING.items()):
        cal = sc.OLD_CAL[stand]
        for batch, fills in sorted(sc.FILLS[group][stand].items()):
            cells = {}
            for x, y, w in fills:
                icell = cell_index(x, edges)
                cells.setdefault(icell, zero_stats())
                accumulate_into(cells[icell], x, y, w, cal)
            for icell, stats in sorted(cells.items()):
                lo, hi = cell_range(icell, edges)
                xs = [x for x, _, _ in fills if cell_index(x, edges) == icell]
                yield group, stand, batch, lo, hi, min(xs), max(xs), stats


def write_appendix_batches():
    with open(DATA / "appendix_batches.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["group", "stand", "batch", "bin_low", "bin_high", "x_min", "x_max",
             "n", "sumw", "sumwy_old", "sumwy2_old", "sumw2", "sumwx", "sumwx2"]
        )
        for group, stand, batch, lo, hi, xlo, xhi, s in iter_batch_cell_rows():
            keep_sumw2 = (stand, group) in sc.STORE_SUMW2
            w.writerow([
                group, stand, batch, fmt(lo), fmt(hi), fmt(xlo), fmt(xhi),
                s["n"], fmt(s["sumw"]),
                fmt(s["sumwy"]), fmt(s["sumwy2"]),
                fmt(s["sumw2"]) if keep_sumw2 else "",
                fmt(s["sumwx"]), fmt(s["sumwx2"]),
            ])


def write_appendix_overlap():
    batch = sc.OVERLAP_BATCH
    with open(DATA / "appendix_overlap.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["batch", "group", "stand", "x_min", "x_max", "n", "sumw",
             "sumwy_old", "sumwy2_old", "sumw2", "sumwx", "sumwx2"]
        )
        for group in ("temp", "humid"):
            for stand in ("A", "B"):
                fills = sc.FILLS[group][stand][batch]
                cal = sc.OLD_CAL[stand]
                s = zero_stats()
                for x, y, wt in fills:
                    accumulate_into(s, x, y, wt, cal)
                xs = [x for x, _, _ in fills]
                keep_sumw2 = (stand, group) in sc.STORE_SUMW2
                w.writerow([
                    batch, group, stand, fmt(min(xs)), fmt(max(xs)), s["n"],
                    fmt(s["sumw"]), fmt(s["sumwy"]), fmt(s["sumwy2"]),
                    fmt(s["sumw2"]) if keep_sumw2 else "",
                    fmt(s["sumwx"]), fmt(s["sumwx2"]),
                ])


def write_preview_standb_press():
    edges = sc.PREVIEW_BINNING
    cal = sc.OLD_CAL["B"]
    cells = [zero_stats() for _ in range(len(edges) + 1)]
    for fills in sc.FILLS["press"]["B"].values():
        for x, y, w in fills:
            accumulate_into(cells[cell_index(x, edges)], x, y, w, cal)
    with open(DATA / "preview_standB_press.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bin_low", "bin_high", "entries", "mean_old", "displayed_error"])
        for icell in range(1, len(edges)):
            lo, hi = cell_range(icell, edges)
            s = cells[icell]
            if s["n"] == 0:
                continue
            w.writerow([
                fmt(lo), fmt(hi), fmt(s["sumw"]), fmt(s["sumwy"] / s["sumw"]),
                fmt(sc.PREVIEW_DISPLAYED_ERROR),
            ])


def write_ledger():
    with open(DATA / "ledger_equal_weight.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "batch", "weight", "verified_by"])
        for group, batches in sc.LEDGER_EQUAL_WEIGHT.items():
            for batch in batches:
                w.writerow([group, batch, "1", sc.LEDGER_REF])


def write_new_report_spec():
    spec = {
        "intervals": [list(iv) for iv in sc.INTERVALS],
        "new_calibration": [
            {"range": list(iv), "scale": ab[0], "offset": ab[1]}
            for iv, ab in sc.NEW_CAL.items()
        ],
        "flow_calibration": "extend_nearest_interval",
        "error_mode": sc.ERROR_MODE,
        "stat_overflows": sc.STAT_OVERFLOWS,
        "output_bin_edges": sc.OUT_EDGES,
        "groups": ["temp", "humid", "press"],
    }
    with open(DATA / "new_report_spec.json", "w") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    write_root_files()
    write_appendix_calibration()
    write_appendix_batches()
    write_appendix_overlap()
    write_preview_standb_press()
    write_ledger()
    write_new_report_spec()
    print(f"input materials written to {DATA}")


if __name__ == "__main__":
    main()
