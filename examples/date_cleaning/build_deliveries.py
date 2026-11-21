"""
Build the two receiving-ledger deliveries used by the example and tests.

The two deliveries share the same displayed content, shipment numbers and
dates.  Delivery two changes the formula in ``North!B3`` while keeping the
same cached result, and adds an uncached formula and a genuinely blank
date cell.  ``North!B2`` and ``South!B2`` are the same cell address with
different shipment numbers.
"""

import datetime
import os
import re
import zipfile

from openpyxl import Workbook


def _base_workbook(changed_formula=False, extra_rows=False):
    workbook = Workbook()
    north = workbook.active
    north.title = "North"
    south = workbook.create_sheet("South")

    headers = ["发货号", "收货日期", "备注"]
    for sheet in (north, south):
        sheet.append(headers)

    north.append(["S-N-001", datetime.date(2026, 1, 5), "literal"])
    north.append(
        [
            "S-N-002",
            "=DATE(2026,1,6)" if not changed_formula else "=DATE(2026,1,6)+0",
            "formula cache",
        ]
    )
    north.append(["S-N-003", datetime.date(2026, 1, 7), "literal"])
    north.append(["S-N-004", 46000, "plain number"])
    if extra_rows:
        north.append(["S-N-005", "=DATE(2026,1,9)", "no cache"])
        north.append(["S-N-006", None, "true blank"])

    south.append(["S-S-001", datetime.date(2026, 1, 5), "literal"])
    south.append(["S-S-002", datetime.date(2026, 1, 8), "literal"])

    for sheet in (north, south):
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row=row, column=2).number_format = "yyyy-mm-dd"
    north.cell(row=5, column=2).number_format = "0"

    return workbook


def _inject_caches(path, caches):
    with zipfile.ZipFile(path) as source:
        payload = {name: source.read(name) for name in source.namelist()}

    sheet_path = "xl/worksheets/sheet1.xml"
    text = payload[sheet_path].decode("utf-8")
    for cell, serial in caches.items():
        pattern = r'(<c r="%s"[^>]*>)(<f>.*?</f>)(<v></v>)?(</c>)' % cell
        replacement = r"\1\2<v>%s</v>\4" % serial
        text, count = re.subn(pattern, replacement, text)
        if count != 1:
            raise RuntimeError("could not inject cache for %s" % cell)

    payload[sheet_path] = text.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as destination:
        for name, data in payload.items():
            destination.writestr(name, data)


def build_deliveries(directory):
    """Create both XLSX deliveries and the CSV control in ``directory``."""

    os.makedirs(directory, exist_ok=True)
    first = os.path.join(directory, "delivery-1.xlsx")
    second = os.path.join(directory, "delivery-2.xlsx")
    control = os.path.join(directory, "control.csv")

    _base_workbook(changed_formula=False, extra_rows=False).save(first)
    _inject_caches(first, {"B3": 46028})

    _base_workbook(changed_formula=True, extra_rows=True).save(second)
    _inject_caches(second, {"B3": 46028})

    with open(control, "w", encoding="utf-8", newline="") as stream:
        stream.write("发货号,收货日期,备注\n")
        stream.write("C-001,2026-01-05,literal\n")
        stream.write("C-002,2026-01-06,literal\n")
        stream.write("C-003,,empty\n")
        stream.write("C-004,2026-02-30,missing calendar day\n")
        stream.write("C-005,46000,plain number\n")

    return first, second, control


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "date-cleaning-fixtures"
    for path in build_deliveries(target):
        print(path)
