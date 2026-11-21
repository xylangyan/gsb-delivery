"""
pyexcel.date_cleaning.loader
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read receiving ledgers from XLSX and CSV without evaluating formulas.

XLSX is opened twice: once for formula text and cell metadata, and once
for cached values.  The two views are joined by page and cell address so
that a formula's provenance is never lost.
"""

import csv
import os

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .dates import canonical_value
from .models import (
    DateCleaningError,
    DateRecord,
    Delivery,
    SOURCE_CSV_TEXT,
    SOURCE_FORMULA,
    SOURCE_XLSX_LITERAL,
    ledger_version_of,
)


DEFAULT_DATE_HEADER_TOKENS = ("收货日期", "日期", "date")
DEFAULT_SHIPMENT_HEADER_TOKENS = ("发货号", "发货单号", "发货编号", "shipment")


def load_delivery(
    path,
    kind=None,
    name=None,
    date_column=None,
    shipment_column=None,
    header_row=1,
    sheet_name=None,
):
    """Load a delivery from ``path``.

    :param str path: ledger file
    :param str kind: ``xlsx`` or ``csv``; inferred from suffix if omitted
    :param str name: delivery name used in receipts; defaults to filename
    :param date_column: header text, one-based index, or Excel column letter
    :param shipment_column: header text, one-based index, or column letter
    :param int header_row: one-based header row
    :param str sheet_name: CSV page name; defaults to the file stem
    """

    if kind is None:
        kind = _infer_kind(path)
    version = ledger_version_of(path)
    if kind == "xlsx":
        return _load_xlsx(
            path,
            version,
            name=name,
            date_column=date_column,
            shipment_column=shipment_column,
            header_row=header_row,
        )
    if kind == "csv":
        return _load_csv(
            path,
            version,
            name=name,
            date_column=date_column,
            shipment_column=shipment_column,
            header_row=header_row,
            sheet_name=(
                sheet_name
                or os.path.splitext(os.path.basename(path))[0]
            ),
        )
    raise DateCleaningError("unsupported ledger kind: %s" % kind)


def _infer_kind(path):
    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".xlsx", ".xlsm"):
        return "xlsx"
    if suffix in (".csv", ".txt"):
        return "csv"
    raise DateCleaningError(
        "cannot infer ledger kind from %s; pass kind='xlsx' or kind='csv'"
        % path
    )


def _load_xlsx(
    path,
    version,
    name=None,
    date_column=None,
    shipment_column=None,
    header_row=1,
):
    formula_book = load_workbook(path, data_only=False, read_only=False)
    value_book = load_workbook(path, data_only=True, read_only=False)
    delivery = Delivery(
        path=path,
        version=version,
        kind="xlsx",
        sheets=list(formula_book.sheetnames),
    )
    delivery.name = name or delivery.name
    delivery.sheet_headers = {}
    epoch = formula_book.epoch
    try:
        for sheet_name in formula_book.sheetnames:
            formula_sheet = formula_book[sheet_name]
            value_sheet = value_book[sheet_name]
            headers = _read_headers(
                value_sheet, header_row, formula_sheet.max_column
            )
            if not headers:
                delivery.sheet_headers[sheet_name] = ()
                continue
            date_index = _resolve_column(
                headers, date_column, DEFAULT_DATE_HEADER_TOKENS, "date"
            )
            shipment_index = _resolve_optional_column(
                headers,
                shipment_column,
                DEFAULT_SHIPMENT_HEADER_TOKENS,
            )
            delivery.sheet_headers[sheet_name] = tuple(headers)

            for row_number in range(header_row + 1, formula_sheet.max_row + 1):
                formula_cell = formula_sheet.cell(
                    row=row_number, column=date_index + 1
                )
                value_cell = value_sheet.cell(
                    row=row_number, column=date_index + 1
                )
                row_values = _row_values(
                    formula_sheet, row_number, len(headers)
                )
                if _row_is_empty(row_values):
                    continue

                formulas = _formula_text(formula_cell)
                raw_value = formula_cell.value
                cached_value = value_cell.value
                has_formula = formulas is not None
                has_cached_value = (
                    has_formula
                    and value_cell.value is not None
                    and value_cell.value != ""
                )
                shipment_no = None
                if shipment_index is not None:
                    shipment_no = canonical_value(
                        formula_sheet.cell(
                            row=row_number, column=shipment_index + 1
                        ).value
                    )
                number_format = formula_cell.number_format
                if isinstance(raw_value, (int, float)) and not isinstance(
                    raw_value, bool
                ):
                    if not _is_excel_date_format(number_format):
                        # A number without a date format is a plain number,
                        # not a date, even if its serial happens to parse.
                        number_format = None
                record = DateRecord(
                    ledger_version=version,
                    ledger_name=delivery.name,
                    sheet=sheet_name,
                    cell="%s%s"
                    % (get_column_letter(date_index + 1), row_number),
                    row=row_number,
                    column=date_index,
                    shipment_no=shipment_no,
                    source=(
                        SOURCE_FORMULA if has_formula else SOURCE_XLSX_LITERAL
                    ),
                    formula=formulas,
                    raw_value=raw_value,
                    cached_value=cached_value,
                    has_formula=has_formula,
                    has_cached_value=has_cached_value,
                    number_format=number_format,
                    headers=headers,
                    row_values=row_values,
                    date_column_index=date_index,
                    shipment_column_index=shipment_index,
                    epoch=epoch,
                )
                delivery.add_record(record)
    finally:
        formula_book.close()
        value_book.close()
    return delivery


def _load_csv(
    path,
    version,
    name=None,
    date_column=None,
    shipment_column=None,
    header_row=1,
    sheet_name="CSV",
):
    delivery = Delivery(
        path=path, version=version, kind="csv", sheets=[sheet_name]
    )
    delivery.name = name or delivery.name
    delivery.sheet_headers = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows:
        delivery.sheet_headers[sheet_name] = ()
        return delivery

    headers = [cell.strip() for cell in rows[header_row - 1]]
    if not any(headers):
        delivery.sheet_headers[sheet_name] = ()
        return delivery
    delivery.sheet_headers[sheet_name] = tuple(headers)
    date_index = _resolve_column(
        headers, date_column, DEFAULT_DATE_HEADER_TOKENS, "date"
    )
    shipment_index = _resolve_optional_column(
        headers, shipment_column, DEFAULT_SHIPMENT_HEADER_TOKENS
    )

    for offset, row in enumerate(rows[header_row:], start=header_row + 1):
        if not any(cell.strip() for cell in row):
            continue
        row_values = list(row) + [""] * (len(headers) - len(row))
        row_values = row_values[: len(headers)]
        raw_value = row_values[date_index]
        shipment_no = None
        if shipment_index is not None:
            shipment_no = row_values[shipment_index]
        record = DateRecord(
            ledger_version=version,
            ledger_name=delivery.name,
            sheet=sheet_name,
            cell="%s%s" % (get_column_letter(date_index + 1), offset),
            row=offset,
            column=date_index,
            shipment_no=shipment_no or None,
            source=SOURCE_CSV_TEXT,
            formula=None,
            raw_value=raw_value,
            cached_value=None,
            has_formula=False,
            has_cached_value=False,
            number_format=None,
            headers=headers,
            row_values=row_values,
            date_column_index=date_index,
            shipment_column_index=shipment_index,
        )
        delivery.add_record(record)
    return delivery


def _read_headers(sheet, header_row, max_column):
    headers = []
    for column in range(1, max_column + 1):
        value = sheet.cell(row=header_row, column=column).value
        headers.append("" if value is None else str(value).strip())
    while headers and headers[-1] == "":
        headers.pop()
    return headers


def _row_values(sheet, row_number, width):
    values = []
    for column in range(1, width + 1):
        value = sheet.cell(row=row_number, column=column).value
        values.append(canonical_value(value))
    return values


def _row_is_empty(row_values):
    for value in row_values:
        if value is not None and str(value).strip() != "":
            return False
    return True


def _formula_text(cell):
    if cell.data_type == "f" or (
        isinstance(cell.value, str) and cell.value.startswith("=")
    ):
        return str(cell.value)
    return None


def _is_excel_date_format(number_format):
    if not number_format:
        return False
    try:
        from openpyxl.styles.numbers import is_date_format
    except ImportError:  # pragma: no cover
        return False
    return is_date_format(number_format)


def _resolve_column(headers, requested, default_tokens, label):
    index = _find_column(headers, requested)
    if index is not None:
        return index
    index = _find_column_by_tokens(headers, default_tokens)
    if index is not None:
        return index
    raise DateCleaningError(
        "cannot find %s column; headers are %r" % (label, headers)
    )


def _resolve_optional_column(headers, requested, default_tokens):
    index = _find_column(headers, requested)
    if index is not None:
        return index
    return _find_column_by_tokens(headers, default_tokens)


def _find_column(headers, requested):
    if requested is None:
        return None
    if isinstance(requested, int):
        if requested < 1 or requested > len(headers):
            raise DateCleaningError(
                "column index %r is outside the header range" % requested
            )
        return requested - 1
    text = str(requested).strip()
    for index, header in enumerate(headers):
        if header.strip() == text:
            return index
    if len(text) <= 3 and text.isalpha():
        target = text.upper()
        for index in range(len(headers)):
            if get_column_letter(index + 1) == target:
                return index
    return None


def _find_column_by_tokens(headers, tokens):
    for token in tokens:
        for index, header in enumerate(headers):
            if token.lower() in header.lower():
                return index
    return None


def load_confirmations(payload):
    """Build confirmation objects from a dict, list, or JSON-like payload."""

    from .models import Confirmation

    if isinstance(payload, dict):
        entries = payload.get("confirmations", [payload])
    elif isinstance(payload, list):
        entries = payload
    else:
        raise DateCleaningError("confirmations must be a dict or list")
    confirmations = []
    for index, entry in enumerate(entries):
        confirmation = Confirmation.from_dict(entry)
        if not confirmation.confirmation_id:
            confirmation.confirmation_id = "confirmation-%d" % (index + 1)
        confirmations.append(confirmation)
    return confirmations
