#!/usr/bin/env python3
"""合并两卷台架 TProfile 存档，按新报告口径交付一份 uproot 可读的 profile。

方法要点：
- 一律在充分统计量（sumw, sumwy, sumwy2, sumw2, sumwx, sumwx2）层面合并，
  不拿两卷的显示均值合并，也不从新箱里扣旧均值。
- 卷一 temp 的旧箱 [2,6) 跨两个目标区间：用重叠批附表把 B03（右半）从该箱
  统计量中减出，左半 B02 与 B03 分别按各自区间的新系数作仿射复校准。
- 重叠批 B03 两卷各记一份：附表两行至原读数层面核对一致后，只计入一次。
- 复校准按区间分段：y_new = A * y_old + B，A/B 由新校准与旧校准复合而成；
  统计量变换为 sumwy' = A*sumwy + B*sumw，
  sumwy2' = A^2*sumwy2 + 2AB*sumwy + B^2*sumw，sumw2 不变。
- humid 旧文件缺逐箱 sumw2，台账证实等权 1，按 sumw2 = sumw 推回；
  temp 的逐箱 sumw2 与分批附表逐箱核准。
- 超界箱并入总体统计按新报告口径（kConsider），不沿用旧文件的 kIgnore。
- press 卷二只有预览（均值 + 相同的显示误差），原权重未证实：均值可算，
  误差缺证据，不写入完整 profile，阻塞项记入交档回单。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import uproot
import uproot.models.TArray

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario as sc

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = BASE / "out"
STAT_FIELDS = ("n", "sumw", "sumwy", "sumwy2", "sumw2", "sumwx", "sumwx2")
TOL = dict(rtol=1e-12, atol=1e-12)


def zero_stats():
    return dict.fromkeys(STAT_FIELDS, 0.0)


def add_into(dst, src, sign=1.0):
    for k in STAT_FIELDS:
        if src[k] is None:
            continue
        dst[k] += sign * src[k]


def read_csv(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def row_stats(row):
    return {
        "n": float(row["n"]),
        "sumw": float(row["sumw"]),
        "sumwy": float(row["sumwy_old"]),
        "sumwy2": float(row["sumwy2_old"]),
        "sumw2": float(row["sumw2"]) if row["sumw2"] else None,
        "sumwx": float(row["sumwx"]),
        "sumwx2": float(row["sumwx2"]),
    }


def read_profile(prof):
    (cont,) = prof.base(uproot.models.TArray.Model_TArray)
    sw2_member = prof.member("fBinSumw2", none_if_missing=True)
    sw2 = None if sw2_member is None or len(sw2_member) == 0 else np.asarray(sw2_member, float)
    ncells = prof.member("fNcells")
    cells = []
    for i in range(ncells):
        cells.append({
            "n": 0.0,
            "sumw": float(np.asarray(prof.member("fBinEntries"), float)[i]),
            "sumwy": float(np.asarray(cont, float)[i]),
            "sumwy2": float(np.asarray(prof.member("fSumw2"), float)[i]),
            "sumw2": None if sw2 is None else float(sw2[i]),
            "sumwx": 0.0,
            "sumwx2": 0.0,
        })
    return {
        "edges": [float(e) for e in prof.axis().edges()],
        "cells": cells,
        "entries": float(prof.member("fEntries")),
        "stat_overflows": int(prof.member("fStatOverflows")),
        "tsumw": float(prof.member("fTsumw")),
        "tsumw2": float(prof.member("fTsumw2")),
        "tsumwx": float(prof.member("fTsumwx")),
        "tsumwx2": float(prof.member("fTsumwx2")),
        "tsumwy": float(prof.member("fTsumwy")),
        "tsumwy2": float(prof.member("fTsumwy2")),
    }


def cell_range(icell, edges):
    if icell == 0:
        return (float("-inf"), edges[0])
    if icell == len(edges):
        return (edges[-1], float("inf"))
    return (edges[icell - 1], edges[icell])


def composed_coeffs(old_cal, new_cal):
    a_o, b_o = old_cal
    a_n, b_n = new_cal
    A = a_n / a_o
    B = b_n - A * b_o
    return A, B


def transform_stats(stats, A, B):
    out = dict(stats)
    out["sumwy2"] = A * A * stats["sumwy2"] + 2 * A * B * stats["sumwy"] + B * B * stats["sumw"]
    out["sumwy"] = A * stats["sumwy"] + B * stats["sumw"]
    return out


def new_cal_for(xlo, xhi):
    if xhi <= sc.AXIS_RANGE[0]:
        return sc.NEW_CAL[sc.INTERVALS[0]]
    if xlo >= sc.AXIS_RANGE[1]:
        return sc.NEW_CAL[sc.INTERVALS[1]]
    for iv in sc.INTERVALS:
        if xlo >= iv[0] and xhi <= iv[1]:
            return sc.NEW_CAL[iv]
    raise ValueError(f"piece [{xlo}, {xhi}) spans an interval boundary")


def out_cell_index(xlo, xhi):
    edges = sc.OUT_EDGES
    if xhi <= edges[0]:
        return 0
    if xlo >= edges[-1]:
        return len(edges)
    for i in range(len(edges) - 1):
        if xlo >= edges[i] and xhi <= edges[i + 1]:
            return i + 1
    raise ValueError(f"piece [{xlo}, {xhi}) does not fit an output bin")


def stats_close(a, b, fields, label):
    for k in fields:
        if a[k] is None or b[k] is None:
            continue
        np.testing.assert_allclose(a[k], b[k], err_msg=f"{label}: {k}", **TOL)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    spec = json.loads((DATA / "new_report_spec.json").read_text())
    assert spec["error_mode"] == sc.ERROR_MODE
    assert spec["stat_overflows"] == sc.STAT_OVERFLOWS

    batches = {}
    for row in read_csv("appendix_batches.csv"):
        key = (row["group"], row["stand"])
        batches.setdefault(key, []).append({
            "bin": (float(row["bin_low"]), float(row["bin_high"])),
            "batch": row["batch"],
            "xlo": float(row["x_min"]),
            "xhi": float(row["x_max"]),
            "stats": row_stats(row),
        })
    overlap = {}
    for row in read_csv("appendix_overlap.csv"):
        key = (row["group"], row["stand"])
        overlap.setdefault(key, []).append(
            (row["batch"], float(row["x_min"]), float(row["x_max"]), row_stats(row))
        )
    ledger = {}
    for row in read_csv("ledger_equal_weight.csv"):
        ledger.setdefault(row["group"], set()).add(row["batch"])

    profiles = {}
    with uproot.open(DATA / "standA_profiles.root") as fa, uproot.open(DATA / "standB_profiles.root") as fb:
        files = {"A": fa, "B": fb}
        raw = {}
        for (stand, group) in sc.OLD_BINNING:
            raw[(stand, group)] = read_profile(files[stand][group])
    profiles = raw

    # 1) 旧文件与分批附表逐箱核准（含 temp 的逐箱权重平方和）
    for (group, stand), rows in sorted(batches.items()):
        P = profiles[(stand, group)]
        assert P["stat_overflows"] == 0, "旧文件总体统计口径应为 kIgnore"
        agg = [zero_stats() for _ in P["cells"]]
        for row in rows:
            for icell in range(len(P["cells"])):
                if cell_range(icell, P["edges"]) == row["bin"]:
                    add_into(agg[icell], row["stats"])
                    break
            else:
                raise AssertionError(f"appendix bin {row['bin']} not in {stand}/{group}")
        fields = ["sumw", "sumwy", "sumwy2"]
        if (stand, group) in sc.STORE_SUMW2:
            fields.append("sumw2")
        for icell, (got, want) in enumerate(zip(P["cells"], agg)):
            stats_close(got, want, fields, f"{stand}/{group} cell {icell}")
        regular = agg[1:-1]
        for scalar, key in (("tsumw", "sumw"), ("tsumw2", "sumw2"),
                            ("tsumwx", "sumwx"), ("tsumwx2", "sumwx2"),
                            ("tsumwy", "sumwy"), ("tsumwy2", "sumwy2")):
            want = sum(c[key] for c in regular)
            if key == "sumw2" and (stand, group) not in sc.STORE_SUMW2:
                continue
            np.testing.assert_allclose(
                P[scalar], want, err_msg=f"{stand}/{group} {scalar} (kIgnore)", **TOL)
        np.testing.assert_allclose(
            P["entries"], sum(c["n"] for c in agg),
            err_msg=f"{stand}/{group} fEntries", **TOL)
        for icell in range(len(P["cells"])):
            P["cells"][icell]["sumwx"] = agg[icell]["sumwx"]
            P["cells"][icell]["sumwx2"] = agg[icell]["sumwx2"]

    # 2) 重叠批一致性：附表两卷两行回到原读数层面必须相同
    overlap_checks = []
    for group in ("temp", "humid"):
        rows = {stand: overlap[(group, stand)] for stand in ("A", "B")}
        assert len(rows["A"]) == len(rows["B"]) == 1
        bA, _xloA, _xhiA, sA = rows["A"][0]
        bB, xloB, xhiB, sB = rows["B"][0]
        assert bA == bB == sc.OVERLAP_BATCH
        raw_stats = []
        for stand, s in (("A", sA), ("B", sB)):
            a_o, b_o = sc.OLD_CAL[stand]
            raw_stats.append({
                "sumw": s["sumw"],
                "sumwy": (s["sumwy"] - b_o * s["sumw"]) / a_o,
                "sumwy2": (s["sumwy2"] - 2 * b_o * s["sumwy"] + b_o * b_o * s["sumw"]) / (a_o * a_o),
            })
        diff = max(abs(raw_stats[0][k] - raw_stats[1][k]) for k in raw_stats[0])
        scale = max(abs(raw_stats[0][k]) for k in raw_stats[0])
        np.testing.assert_allclose(
            diff / scale, 0.0, atol=1e-9,
            err_msg=f"overlap {group} raw mismatch")
        overlap_checks.append((group, bA, diff))

    # 3) 组片（pieces）：以附表分批分箱记录为件（已与旧文件逐箱核准一致），
    #    重叠批 B03 跳过卷二那份——同一次采样只占一个位置。
    #    卷一 temp 跨界箱 [2,6) 由此在统计量层面拆成 B02（左）与 B03（右）。
    def build_pieces(group):
        pieces = []
        for stand in ("A", "B"):
            for row in batches.get((group, stand), []):
                if stand == "B" and row["batch"] == sc.OVERLAP_BATCH:
                    continue
                pieces.append({
                    "stats": dict(row["stats"]),
                    "xlo": row["xlo"],
                    "xhi": row["xhi"],
                    "stand": stand,
                    "batch": row["batch"],
                })
        return pieces

    def resolve_sumw2(group, pieces):
        if group in sc.LEDGER_EQUAL_WEIGHT:
            known = {r["batch"] for (g, _s), rows in batches.items() if g == group for r in rows}
            missing = known - ledger.get(group, set())
            assert not missing, f"{group} batches not ledger-verified: {missing}"
            for p in pieces:
                if p["stats"]["sumw2"] is None:
                    p["stats"]["sumw2"] = p["stats"]["sumw"]
        for p in pieces:
            assert p["stats"]["sumw2"] is not None, f"{group}: missing sumw2"

    def merge_group(group):
        pieces = build_pieces(group)
        resolve_sumw2(group, pieces)
        ncells = len(sc.OUT_EDGES) + 1
        cells = [zero_stats() for _ in range(ncells)]
        for p in pieces:
            cal = new_cal_for(p["xlo"], p["xhi"])
            A, B = composed_coeffs(sc.OLD_CAL[p["stand"]], cal)
            t = transform_stats(p["stats"], A, B)
            add_into(cells[out_cell_index(p["xlo"], p["xhi"])], t)
        b03 = [p for p in pieces if p["batch"] == sc.OVERLAP_BATCH]
        assert len(b03) == 1, f"{group}: overlap batch counted {len(b03)} times"
        return cells

    merged = {group: merge_group(group) for group in ("temp", "humid")}

    # 4) press：卷二只有预览，均值可算、误差缺证据
    press_pieces = build_pieces("press")
    for p in press_pieces:
        assert p["stats"]["sumw2"] is not None  # 卷一 press 存了 fBinSumw2
    ncells = len(sc.OUT_EDGES) + 1
    press_cells = [zero_stats() for _ in range(ncells)]
    press_blocked_bins = set()
    for p in press_pieces:
        cal = new_cal_for(p["xlo"], p["xhi"])
        A, B = composed_coeffs(sc.OLD_CAL[p["stand"]], cal)
        t = transform_stats(p["stats"], A, B)
        add_into(press_cells[out_cell_index(p["xlo"], p["xhi"])], t)
    for row in read_csv("preview_standB_press.csv"):
        lo, hi = float(row["bin_low"]), float(row["bin_high"])
        entries = float(row["entries"])
        mean_old = float(row["mean_old"])
        cal = new_cal_for(lo, hi)
        A, B = composed_coeffs(sc.OLD_CAL["B"], cal)
        swy_new = A * (mean_old * entries) + B * entries
        icell = out_cell_index(lo, hi)
        press_cells[icell]["sumw"] += entries
        press_cells[icell]["sumwy"] += swy_new
        press_blocked_bins.add(icell)

    # 5) 地面真值：由原始逐点记录直接复算（B03 只算一次）
    def ground_truth(group):
        cells = [zero_stats() for _ in range(len(sc.OUT_EDGES) + 1)]
        for stand, by_batch in sc.FILLS[group].items():
            for batch, fills in by_batch.items():
                if stand == "B" and batch == sc.OVERLAP_BATCH:
                    continue
                for x, y, w in fills:
                    a_n, b_n = new_cal_for(x, x)
                    yn = a_n * y + b_n
                    c = cells[out_cell_index(x, x)]
                    c["n"] += 1
                    c["sumw"] += w
                    c["sumwy"] += w * yn
                    c["sumwy2"] += w * yn * yn
                    c["sumw2"] += w * w
                    c["sumwx"] += w * x
                    c["sumwx2"] += w * x * x
        return cells

    gt = {group: ground_truth(group) for group in ("temp", "humid", "press")}
    for group in ("temp", "humid"):
        for got, want in zip(merged[group], gt[group]):
            stats_close(got, want, STAT_FIELDS, f"ground-truth {group}")
    for icell in range(ncells):
        for k in ("sumw", "sumwy"):
            np.testing.assert_allclose(
                press_cells[icell][k], gt["press"][icell][k],
                err_msg=f"press mean-level cell {icell}", **TOL)

    # 6) 写交付文件（仅材料齐的 temp、humid）
    out_path = OUT / "merged_profiles.root"
    with uproot.recreate(out_path) as fout:
        for group, cells in sorted(merged.items()):
            fout[group] = uproot.writing.identify.to_TProfile(
                fName=group,
                fTitle=f"{group} merged, new calibration {sc.ERROR_MODE}",
                data=np.array([c["sumwy"] for c in cells], np.float64),
                fEntries=float(sum(c["n"] for c in cells)),
                fTsumw=float(sum(c["sumw"] for c in cells)),
                fTsumw2=float(sum(c["sumw2"] for c in cells)),
                fTsumwx=float(sum(c["sumwx"] for c in cells)),
                fTsumwx2=float(sum(c["sumwx2"] for c in cells)),
                fTsumwy=float(sum(c["sumwy"] for c in cells)),
                fTsumwy2=float(sum(c["sumwy2"] for c in cells)),
                fSumw2=np.array([c["sumwy2"] for c in cells], np.float64),
                fBinEntries=np.array([c["sumw"] for c in cells], np.float64),
                fBinSumw2=np.array([c["sumw2"] for c in cells], np.float64),
                fXaxis=uproot.writing.identify.to_TAxis(
                    fName="xaxis", fTitle="", fNbins=len(sc.OUT_EDGES) - 1,
                    fXmin=sc.OUT_EDGES[0], fXmax=sc.OUT_EDGES[-1],
                ),
                fErrorMode=0,
                fStatOverflows=1,  # 新报告口径 kConsider：超界箱并入总体统计
            )
    print(f"wrote {out_path}")

    # 7) uproot 读回验证：超界箱、有效条数、均值、误差口径
    def mean_err(c):
        if c["sumw"] == 0:
            return 0.0, 0.0, 0.0
        mean = c["sumwy"] / c["sumw"]
        neff = c["sumw"] ** 2 / c["sumw2"]
        var = abs(c["sumwy2"] / c["sumw"] - mean * mean)
        return mean, (var / neff) ** 0.5, neff

    readback = {}
    with uproot.open(out_path) as f:
        for group, cells in sorted(merged.items()):
            prof = f[group]
            means = np.array([mean_err(c)[0] for c in cells])
            errs = np.array([mean_err(c)[1] for c in cells])
            neffs = np.array([mean_err(c)[2] for c in cells])
            np.testing.assert_allclose(prof.values(flow=True), means, **TOL)
            np.testing.assert_allclose(prof.errors(flow=True), errs, **TOL)
            np.testing.assert_allclose(prof.counts(flow=True), neffs, **TOL)
            np.testing.assert_allclose(
                np.asarray(prof.member("fBinEntries"), float),
                [c["sumw"] for c in cells], **TOL)
            np.testing.assert_allclose(
                np.asarray(prof.member("fBinSumw2"), float),
                [c["sumw2"] for c in cells], **TOL)
            assert int(prof.member("fStatOverflows")) == 1
            assert int(prof.member("fErrorMode")) == 0
            np.testing.assert_allclose(
                prof.member("fEntries"), sum(c["n"] for c in cells), **TOL)
            np.testing.assert_allclose(list(prof.axis().edges()), sc.OUT_EDGES, **TOL)
            readback[group] = (prof, means, errs, neffs)
            assert cells[0]["n"] > 0 and cells[-1]["n"] > 0, "两端超界箱应非空"
    print("uproot read-back verification passed")

    write_receipt(merged, press_cells, press_blocked_bins, gt, overlap_checks, mean_err, out_path)


def write_receipt(merged, press_cells, press_blocked_bins, gt, overlap_checks, mean_err, out_path):
    edges = sc.OUT_EDGES
    labels = ["下溢 (-inf,0)"] + [f"[{edges[i]:g},{edges[i+1]:g})" for i in range(len(edges) - 1)] + ["上溢 [8,inf)"]

    def coeff_rows():
        rows = []
        for stand, old in sc.OLD_CAL.items():
            for iv in sc.INTERVALS:
                A, B = composed_coeffs(old, sc.NEW_CAL[iv])
                rows.append(f"| 卷{stand} | [{iv[0]:g},{iv[1]:g}) | {A:.10f} | {B:.10f} |")
        return "\n".join(rows)

    def group_table(group):
        lines = ["| 箱 | 均值 | 误差(kERRORMEAN) | fBinEntries(Σw) | 有效条数 neff |",
                 "|---|---|---|---|---|"]
        for label, c in zip(labels, merged[group]):
            mean, err, neff = mean_err(c)
            lines.append(f"| {label} | {mean:.6f} | {err:.6f} | {c['sumw']:.6f} | {neff:.6f} |")
        entries = sum(c["n"] for c in merged[group])
        tsumw = sum(c["sumw"] for c in merged[group])
        lines.append(f"| **合计** | fEntries={entries:.0f} | fTsumw={tsumw:.6f} | | |")
        return "\n".join(lines)

    press_lines = []
    for icell in sorted(press_blocked_bins):
        c = press_cells[icell]
        mean = c["sumwy"] / c["sumw"]
        press_lines.append(f"| {labels[icell]} | {mean:.6f} | {c['sumw']:.6f} | 缺逐箱 Σw²(fBinSumw2) 与 Σwy²(fSumw2) |")

    overlap_lines = [
        f"| {g} | {b} | 两卷附表行回原读数后一致，最大偏差 {d:.3e} |"
        for g, b, d in overlap_checks
    ]

    receipt = f"""# 交档回单：台架两卷 TProfile 合并交付

日期：2026-11-25
交付文件：`answers/out/merged_profiles.root`（uproot 直接可读）
交付对象：`temp`、`humid` 两份完整 profile；`press` 暂缓（见「阻塞项」）

## 口径（依新报告 new_report_spec.json，不沿用旧文件同名旧设置）

- 目标区间：[0,4)、[4,8)；输出分箱 [{", ".join(f"{e:g}" for e in edges)}]，两端带超界箱。
- 新校准（对原读数）：[0,4) 内 y'=1.10·y+0.20；[4,8) 内 y'=0.95·y+0.05；超界箱沿用相邻区间系数。
- 误差口径：kERRORMEAN（fErrorMode=0，均值误差）。
- 超界箱口径：kConsider（fStatOverflows=1，并入总体统计 fTsumw 等）；旧文件为 kIgnore，未沿用。

## 复校准复合系数（y_new = A·y_old + B，按卷按区间）

| 卷 | 区间 | A | B |
|---|---|---|---|
{coeff_rows()}

## 合并与去重核对

- 全程在充分统计量（Σw、Σwy、Σwy²、Σw²、Σwx、Σwx²）层面合并；未使用两卷显示均值合并、也未从新箱里扣旧均值。
- 卷一 temp 旧箱 [2,6) 跨两个目标区间：按分批分箱附表在统计量层面把该箱还原为 B02（左半，x∈[2.2,3.9]）与 B03（右半，x∈[4.2,5.6]）两个组片，分别按所在区间的新系数复校准后再入新箱；不是先合显示均值再从新箱里扣旧均值。
- 重叠批 B03（两台同时见到的同一批采样）：附表两卷两行回原读数核对一致，合并中只计入一次（核明同一次采样没有占两个位置）。

| 组 | 批 | 核对结果 |
|---|---|---|
{chr(10).join(overlap_lines)}

## 权重平方和证据

- `temp`：两卷均存逐箱 fBinSumw2，并与分批附表逐箱核准一致（含加权点 w=2）。
- `humid`：两卷旧文件均缺逐箱 fBinSumw2；{sc.LEDGER_REF}证实其各批等权 1，按 Σw²=Σw 推回。
- `press`：卷一存 fBinSumw2；卷二只有预览，见「阻塞项」。

## 读回验证（uproot）

均值、误差、有效条数与地面真值（原始逐点记录复算，B03 只算一次）逐项一致（rtol=1e-12）；两端超界箱均非空且可读。

### temp
{group_table("temp")}

### humid
{group_table("humid")}

## 阻塞项：press 暂不交付完整 profile

- 均值已能算（卷一统计量 + 卷二预览的均值×条数，均复校准后合并），与地面真值一致。
- 实际误差证据不足：卷二 press 只有预览（各箱显示误差相同，均为 {sc.PREVIEW_DISPLAYED_ERROR}），实际原权重可能不同，无法据此反推。
- 卡住的位置与缺项：新箱 [4,6)、[6,8)，缺卷二对应旧箱的逐箱权重平方和 Σw²（fBinSumw2）与逐箱 Σwy²（fSumw2）。
- 处理：press 未写入交付文件；待补齐卷二原始分箱统计或台账等权证明后再归档。

| 新箱 | 可算均值（参考，未交付） | Σw | 缺项 |
|---|---|---|---|
{chr(10).join(press_lines)}
"""
    path = OUT / "delivery_receipt.md"
    path.write_text(receipt)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
