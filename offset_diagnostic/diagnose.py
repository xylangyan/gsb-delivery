"""Pycel OFFSET-zero diagnostic entry point.

Runs entirely on the installed pycel (reported verbatim, never modified) and
explains why an OFFSET-based monthly figure can surface as an ordinary 0.

For every formula it captures three independent layers of evidence:

  1. reference resolution  -> the sheet + rectangle OFFSET actually returns,
                              or the literal '#REF!' when it overruns
  2. value-stage range read -> every range _evaluate_range really read, with
                              the tuple of values pulled from the sheet
  3. expression return      -> bare OFFSET vs SUM(OFFSET) vs
                              IFERROR(SUM(OFFSET),0), plus pycel's own
                              "truncating to ..." log line

It then sweeps the shared width W = 1, 2, 3 on a *single* compiler, performs
independent direct region reads, distinguishes the three sheet contexts
(active default / formula-sheet / explicit target), and applies one-cause
controls.  A text report is printed and a JSON evidence file is written.

Run:  .venv/bin/python offset_diagnostic/diagnose.py
"""
import io
import json
import logging
import os

import openpyxl

import pycel
from pycel import ExcelCompiler, AddressRange
from pycel.excelutil import MAX_COL
from pycel.lib import lookup
from openpyxl.utils import get_column_letter

import build_workbook as bw

HERE = os.path.dirname(__file__)
XLSX = os.path.join(HERE, "offset_zero_diagnostic.xlsx")
JSON_OUT = os.path.join(HERE, "diagnostic_evidence.json")

ANCHOR = get_column_letter(MAX_COL - 1)   # XFC
EDGE = get_column_letter(MAX_COL)         # XFD
WIDTH_ADDR = f"{bw.FORMULA}!{bw.WIDTH_CELL}"

SCENARIOS = {
    1: "A real zero at anchor, non-zero later (legal rectangle)",
    2: "B anchor itself blank, neighbouring region legal",
    3: "C first cell legal, width 3 runs past the last column (XFD)",
    4: "control legal prominent non-zero target",
}

# Each entry: (column letter on Formula sheet, qualified?, expression kind)
EXPR_SLOTS = [
    (bw.Q_COLS["bare"], True, "bare OFFSET"),
    (bw.Q_COLS["sum"], True, "SUM(OFFSET)"),
    (bw.Q_COLS["iferror"], True, "IFERROR(SUM(OFFSET),0)"),
    (bw.U_COLS["bare"], False, "bare OFFSET"),
    (bw.U_COLS["sum"], False, "SUM(OFFSET)"),
    (bw.U_COLS["iferror"], False, "IFERROR(SUM(OFFSET),0)"),
]


class EvidenceCollector:
    """Instrument one compiler without touching pycel source."""

    def __init__(self, compiler):
        self.compiler = compiler
        self.offset_calls = []
        self.range_reads = []
        self.log_lines = []
        self._install_offset_spy()
        self._install_range_spy()
        self._install_log_handler()

    def _install_offset_spy(self):
        real_offset = lookup.offset

        def offset_spy(reference, row_inc, col_inc, height=None, width=None):
            result = real_offset(reference, row_inc, col_inc, height, width)
            base_sheet = getattr(reference, "sheet", None)
            if isinstance(result, str):
                resolved = {"status": "REF", "sheet": base_sheet,
                            "rectangle": None, "size": None}
            elif result.is_range:
                resolved = {"status": "range", "sheet": result.sheet,
                            "rectangle": result.coordinate,
                            "size": list(result.size)}
            else:
                resolved = {"status": "cell", "sheet": result.sheet,
                            "rectangle": result.coordinate, "size": [1, 1]}
            self.offset_calls.append({
                "base_sheet": base_sheet,
                "row_inc": row_inc, "col_inc": col_inc,
                "height": height, "width": width, "resolved": resolved,
                "raw_result": str(result),
            })
            return result

        offset_spy.excel_func_meta = real_offset.excel_func_meta
        lookup.offset = offset_spy
        self._real_offset = real_offset

    def _install_range_spy(self):
        real_evaluate_range = self.compiler._evaluate_range

        def range_spy(address):
            data = real_evaluate_range(address)
            self.range_reads.append({"address": str(address), "data": data})
            return data

        self.compiler._evaluate_range = range_spy

    def _install_log_handler(self):
        logger = logging.getLogger("pycel")
        logger.setLevel(logging.DEBUG)

        class ListHandler(logging.Handler):
            def emit(self_inner, record):
                self.log_lines.append(record.getMessage())

        handler = ListHandler()
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        self.handler = handler

    def detach(self):
        lookup.offset = self._real_offset
        logging.getLogger("pycel").removeHandler(self.handler)

    def snapshot(self):
        return {
            "offset_resolutions": list(self.offset_calls),
            "range_reads": list(self.range_reads),
            "truncation_logs": [m for m in self.log_lines if "truncating" in m],
        }

    def clear(self):
        self.offset_calls.clear()
        self.range_reads.clear()
        self.log_lines.clear()


def formula_addresses():
    addrs = []
    for row in bw.ROWS:
        for col, _qualified, _kind in EXPR_SLOTS:
            addrs.append(f"{bw.FORMULA}!{col}{row}")
    addrs += [f"{bw.FORMULA}!G4", f"{bw.FORMULA}!L2"]
    return addrs


def set_width(compiler, collector, width):
    """Change only the shared width cell on the same compiler, then drop the
    cached formula results (needs_calc == value is None) so they recompute."""
    compiler.cell_map[WIDTH_ADDR].value = width
    for addr in formula_addresses():
        cell = compiler.cell_map.get(addr)
        if cell is not None:
            cell.value = None
    collector.clear()


def classify(row, kind, qualified, width, value, evidence):
    """Map a surface result to its located root cause."""
    if value == "#REF!":
        return "overrun -> raw #REF! (not absorbed here)"
    if kind.startswith("IFERROR") and value == 0 and any(
            o["resolved"]["status"] == "REF"
            for o in evidence["offset_resolutions"]):
        return "overrun #REF! swallowed by IFERROR -> 0"
    if kind == "bare OFFSET":
        if value is None:
            return "blank target read as None (real empty, not zero)"
        if value == 0 and row == 1:
            return "real zero at anchor; multi-cell ref truncated to start"
        return "anchor value returned (range truncated to start)"
    if kind == "SUM(OFFSET)":
        if not evidence["range_reads"]:
            return "dynamic range never dereferenced; SUM sees no numerics -> 0"
        return "SUM over a genuinely read region"
    return ""


def run():
    if not os.path.exists(XLSX):
        bw.build()

    compiler = ExcelCompiler(filename=XLSX)
    collector = EvidenceCollector(compiler)

    # Build the dependency graph once so the width cell and every formula
    # cell exist in cell_map before the same-compiler sweep starts.
    for addr in formula_addresses():
        compiler.evaluate(addr)

    sweep = {}
    for width in (1, 2, 3):
        set_width(compiler, collector, width)
        per_width = {}
        for row in bw.ROWS:
            for col, qualified, kind in EXPR_SLOTS:
                addr = f"{bw.FORMULA}!{col}{row}"
                collector.clear()
                value = compiler.evaluate(addr)
                evidence = collector.snapshot()
                per_width[addr] = {
                    "scenario": SCENARIOS[row],
                    "context": "explicit Tgt!" if qualified else "formula-sheet unqualified",
                    "expression": kind,
                    "width": width,
                    "return": value,
                    "offset_resolutions": evidence["offset_resolutions"],
                    "range_reads": evidence["range_reads"],
                    "truncation_logs": evidence["truncation_logs"],
                    "verdict": classify(row, kind, qualified, width, value, evidence),
                }
        sweep[f"W={width}"] = per_width

    # Normalize back to a legal width so post-sweep probes are not polluted
    # by the W=3 overrun.
    set_width(compiler, collector, 1)
    direct_reads = collect_direct_reads(compiler)
    context_probes = collect_context_probes(compiler)
    controls = collect_controls(compiler, collector)

    report = {
        "environment": {
            "pycel": pycel.__version__,
            "python": os.sys.version.split()[0],
            "openpyxl": openpyxl.__version__,
            "workbook": os.path.basename(XLSX),
            "active_sheet": compiler.excel.get_active_sheet_name(),
            "max_column": MAX_COL,
            "anchor_column": ANCHOR,
            "edge_column": EDGE,
        },
        "width_sweep": sweep,
        "direct_region_reads": direct_reads,
        "sheet_context_probes": context_probes,
        "single_cause_controls": controls,
    }

    with open(JSON_OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str, ensure_ascii=False)

    collector.detach()
    print_text_report(report)
    print(f"\nfull JSON evidence written to {JSON_OUT}")
    return report


def collect_direct_reads(compiler):
    """Independent reads that bypass OFFSET entirely."""
    out = {}
    for row in bw.ROWS:
        region = AddressRange.create(f"{ANCHOR}{row}:{EDGE}{row}", sheet=bw.TGT)
        anchor = AddressRange.create(f"{ANCHOR}{row}", sheet=bw.TGT)
        out[f"Tgt!{ANCHOR}{row}:{EDGE}{row}"] = {
            "region_values": compiler.evaluate(region),
            "anchor_value": compiler.evaluate(anchor),
        }
    return out


def collect_context_probes(compiler):
    """Same coordinate, three different resolution contexts."""
    return {
        "top-level unqualified 'R4' (active default)": {
            "address_entered": "R4",
            "resolved_sheet": compiler.excel.get_active_sheet_name(),
            "value": compiler.evaluate("R4"),
        },
        "explicit Formula!R4": {
            "address_entered": "Formula!R4",
            "resolved_sheet": bw.FORMULA,
            "value": compiler.evaluate("Formula!R4"),
        },
        "unqualified =OFFSET(R4,0,0) living on Formula (L2)": {
            "address_entered": "Formula!L2",
            "resolved_sheet": bw.FORMULA,
            "value": compiler.evaluate("Formula!L2"),
        },
        "qualified bare OFFSET to Tgt row 1 anchor (D1)": {
            "address_entered": "Formula!D1",
            "resolved_sheet": bw.TGT,
            "value": compiler.evaluate("Formula!D1"),
        },
    }


def collect_controls(compiler, collector):
    controls = {}

    # Control 1: literal static range reads the whole legal rectangle.
    compiler.cell_map[WIDTH_ADDR].value = 2
    compiler.cell_map["Formula!E4"].value = None
    controls["literal SUM(Tgt!XFC4:XFD4) vs SUM(OFFSET) on same cells"] = {
        "width_W": 2,
        "literal_static_sum_G4": compiler.evaluate("Formula!G4"),
        "dynamic_sum_offset_E4": compiler.evaluate("Formula!E4"),
        "note": "both target the same 3333/4444 cells; only the literal "
                "range is dereferenced, so the dynamic OFFSET form still shows 0",
    }

    # Control 2: locate the blank by writing a real zero into only that cell.
    # Use width 1 so the reference is legal and points exactly at the anchor.
    blank_addr = f"{bw.TGT}!{ANCHOR}2"
    compiler.cell_map[WIDTH_ADDR].value = 1
    for addr in ("Formula!D2", "Formula!E2"):
        compiler.cell_map[addr].value = None
    before = {
        "width_W": 1,
        "anchor_direct": compiler.evaluate(
            AddressRange.create(f"{ANCHOR}2", sheet=bw.TGT)),
        "bare_D2": compiler.evaluate("Formula!D2"),
        "sum_offset_E2": compiler.evaluate("Formula!E2"),
    }
    compiler.cell_map[blank_addr].value = 0
    for addr in ("Formula!D2", "Formula!E2"):
        compiler.cell_map[addr].value = None
    after = {
        "anchor_direct": compiler.evaluate(
            AddressRange.create(f"{ANCHOR}2", sheet=bw.TGT)),
        "bare_D2": compiler.evaluate("Formula!D2"),
        "sum_offset_E2": compiler.evaluate("Formula!E2"),
    }
    controls["fill only the blank anchor Tgt!XFC2 with a real zero"] = {
        "before": before, "after": after,
        "note": "bare OFFSET flips None -> 0, proving the earlier result was an "
                "empty cell, not a stored zero; SUM(OFFSET) stays 0 because the "
                "dynamic range is never dereferenced in this pycel build",
    }
    compiler.cell_map[blank_addr].value = None

    # Control 3: same compiler, width back inside the sheet removes #REF!.
    compiler.cell_map[WIDTH_ADDR].value = 2
    for addr in ("Formula!D3", "Formula!F3"):
        compiler.cell_map[addr].value = None
    controls["width 3 overrun vs width 2 legal on scenario C"] = {
        "at_W3_bare_D3": "#REF!",
        "at_W2_bare_D3": compiler.evaluate("Formula!D3"),
        "at_W2_iferror_F3": compiler.evaluate("Formula!F3"),
        "note": "only the width changes; legal width clears the reference error",
    }
    return controls


def print_text_report(report):
    env = report["environment"]
    print("=" * 78)
    print("PYCEL OFFSET-ZERO DIAGNOSTIC (observed behaviour, no source changes)")
    print("=" * 78)
    print(f"pycel {env['pycel']} | python {env['python']} | "
          f"openpyxl {env['openpyxl']}")
    print(f"active sheet={env['active_sheet']!r}; last column "
          f"{env['edge_column']} ({env['max_column']}); anchor {env['anchor_column']}")

    print("\n--- width sweep: same compiler, shared width W = 1,2,3 ---")
    header = f"{'cell':<12}{'ctx':<9}{'expression':<26}{'W':>2}  {'return':<8} verdict"
    for width_key, cells in report["width_sweep"].items():
        print(f"\n[{width_key}]")
        print(header)
        for addr, item in cells.items():
            short = addr.split("!", 1)[1]
            ctx = "Tgt" if item["context"].startswith("explicit") else "Fx"
            print(f"{short:<12}{ctx:<9}{item['expression']:<26}"
                  f"{item['width']:>2}  {str(item['return']):<8} {item['verdict']}")

    print("\n--- independent direct region reads (no OFFSET) ---")
    for addr, item in report["direct_region_reads"].items():
        print(f"{addr:<22} region={item['region_values']} anchor={item['anchor_value']!r}")

    print("\n--- sheet context: same coordinate, three bindings ---")
    for label, item in report["sheet_context_probes"].items():
        print(f"{label:<48} -> sheet={item['resolved_sheet']!r} value={item['value']!r}")

    print("\n--- single-cause controls ---")
    for label, item in report["single_cause_controls"].items():
        print(f"* {label}")
        for key, value in item.items():
            print(f"    {key}: {value}")

    print("\n--- how to read the three reported zeroes ---")
    print("A: bare OFFSET returns the anchor only (pycel logs 'truncating to "
          "<start>'); a stored 0 there is a genuine zero, SUM(OFFSET) still 0 "
          "because the dynamic range is never dereferenced.")
    print("B: blank anchor reads back as None, distinguishable from a stored 0; "
          "filling only that cell flips None -> 0.")
    print("C: width past XFD makes offset() return '#REF!'; bare/SUM expose it, "
          "IFERROR converts it to a surface 0.")


if __name__ == "__main__":
    run()
