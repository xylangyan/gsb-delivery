"""Build the OFFSET-zero diagnostic workbook.

The workbook has three *distinct* sheets:

  Active   : the workbook active sheet (top-level, unqualified addresses
             entered at the compiler entry point resolve here)
  Formula  : every diagnostic formula lives here (unqualified references
             written *inside* a formula resolve here, at compile time)
  Tgt      : the explicit, qualified target sheet

Near the Excel last column (XFC = 16383, XFD = 16384) the same coordinates on
each sheet hold three clearly distinguishable marker kinds:

  * a prominent non-zero value
  * a distinguishable real zero (the integer 0)
  * an explicit blank (the cell is never written)

The markers differ per sheet, so the value returned reveals which sheet a
reference actually bound to.  Nothing in the pycel package is modified.

Run:  .venv/bin/python offset_diagnostic/build_workbook.py
"""
import os

import openpyxl
from openpyxl.utils import get_column_letter

from pycel.excelutil import MAX_COL

ACTIVE = "Active"
FORMULA = "Formula"
TGT = "Tgt"

ANCHOR = get_column_letter(MAX_COL - 1)   # XFC : first cell of every rect
EDGE = get_column_letter(MAX_COL)         # XFD : Excel's last column
ROWS = (1, 2, 3, 4)

# Shared, mutable reference width consumed by every OFFSET formula.
WIDTH_CELL = "B2"

# Qualified (Tgt!) formula columns: bare / SUM / IFERROR-SUM.
Q_COLS = {"bare": "D", "sum": "E", "iferror": "F"}
# Unqualified (no sheet) formula columns: bare / SUM / IFERROR-SUM.
U_COLS = {"bare": "H", "sum": "I", "iferror": "J"}

# (row -> {"anchor": value or None, "edge": value or None})
TGT_MARKERS = {
    1: {"anchor": 0, "edge": 4242},       # real zero, later item is non-zero
    2: {"anchor": None, "edge": 77},      # explicit blank, neighbour legal
    3: {"anchor": 5, "edge": 9},          # first cell legal, width 3 overruns
    4: {"anchor": 3333, "edge": 4444},    # legal prominent control target
}
FORMULA_MARKERS = {
    1: {"anchor": 1101, "edge": 1102},
    2: {"anchor": None, "edge": 1202},
    3: {"anchor": 1303, "edge": 1304},
    4: {"anchor": 1403, "edge": 1404},
}
ACTIVE_MARKERS = {
    1: {"anchor": 2101, "edge": 2102},
    2: {"anchor": None, "edge": 2202},
    3: {"anchor": 2303, "edge": 2304},
    4: {"anchor": 2403, "edge": 2404},
}

OUT_PATH = os.path.join(os.path.dirname(__file__), "offset_zero_diagnostic.xlsx")


def _write_markers(ws, markers):
    for row, data in markers.items():
        if data["anchor"] is not None:
            ws[f"{ANCHOR}{row}"] = data["anchor"]
        if data["edge"] is not None:
            ws[f"{EDGE}{row}"] = data["edge"]


def _offset_formula(anchor_ref, row):
    return f"=OFFSET({anchor_ref}{row},0,0,1,${WIDTH_CELL[0]}${WIDTH_CELL[1:]})"


def build():
    wb = openpyxl.Workbook()
    ws_active = wb.active
    ws_active.title = ACTIVE
    ws_formula = wb.create_sheet(FORMULA)
    ws_tgt = wb.create_sheet(TGT)

    _write_markers(ws_tgt, TGT_MARKERS)
    _write_markers(ws_formula, FORMULA_MARKERS)
    _write_markers(ws_active, ACTIVE_MARKERS)

    # Interior-coordinate context probes (R4 is far from the last column).
    ws_formula["R4"] = 111
    ws_active["R4"] = 222

    # Shared width input; the diagnostic sweeps 1 -> 2 -> 3 on one compiler.
    ws_formula[WIDTH_CELL] = 1
    ws_formula["A2"] = "shared width W (swept 1,2,3 by diagnose.py)"

    for row in ROWS:
        qualified_anchor = f"{TGT}!{ANCHOR}"
        unqualified_anchor = ANCHOR

        ws_formula[f"{Q_COLS['bare']}{row}"] = _offset_formula(
            qualified_anchor, row)
        ws_formula[f"{Q_COLS['sum']}{row}"] = (
            f"=SUM({_offset_formula(qualified_anchor, row)[1:]})")
        ws_formula[f"{Q_COLS['iferror']}{row}"] = (
            f"=IFERROR(SUM({_offset_formula(qualified_anchor, row)[1:]}),0)")

        ws_formula[f"{U_COLS['bare']}{row}"] = _offset_formula(
            unqualified_anchor, row)
        ws_formula[f"{U_COLS['sum']}{row}"] = (
            f"=SUM({_offset_formula(unqualified_anchor, row)[1:]})")
        ws_formula[f"{U_COLS['iferror']}{row}"] = (
            f"=IFERROR(SUM({_offset_formula(unqualified_anchor, row)[1:]}),0)")

    # Literal, static range over the same prominent legal cells (control).
    ws_formula["G4"] = f"=SUM({TGT}!{ANCHOR}4:{EDGE}4)"

    # Interior unqualified OFFSET (binds the Formula sheet at compile time).
    ws_formula["L2"] = "=OFFSET(R4,0,0)"

    # Labels so the sheet is readable when opened in Excel/LibreOffice.
    ws_formula["A6"] = "header row ->"
    ws_formula["D6"] = "qualified Tgt! bare OFFSET"
    ws_formula["E6"] = "qualified SUM(OFFSET)"
    ws_formula["F6"] = "qualified IFERROR(SUM(OFFSET),0)"
    ws_formula["H6"] = "unqualified bare OFFSET"
    ws_formula["I6"] = "unqualified SUM(OFFSET)"
    ws_formula["J6"] = "unqualified IFERROR(SUM(OFFSET),0)"
    ws_formula["G6"] = "literal static SUM control"
    ws_formula["L6"] = "unqualified interior OFFSET context"

    wb.active = wb.sheetnames.index(ACTIVE)
    wb.save(OUT_PATH)
    print(f"wrote {OUT_PATH}")
    print(f"active sheet={wb.active.title}; anchor col={ANCHOR}; edge col={EDGE}")


if __name__ == "__main__":
    build()
