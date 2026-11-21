"""
pyexcel.date_cleaning.output
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Write cleaned ledgers and an auditable problem receipt.

Only accepted records are written to the standard date outputs.  Issues
keep their original formula or value and their true source.
"""

import csv
import json

from openpyxl import Workbook

from .dates import canonical_value
from .models import (
    BLANK_CELL,
    FORMULA_WITHOUT_CACHE,
    INVALID_CACHED_VALUE,
    INVALID_CALENDAR_DATE,
    INVALID_DATE_TEXT,
    INVALID_VALUE,
    PLAIN_NUMBER,
    UNCONFIRMED_FORMULA_CACHE,
)


ISSUE_CATEGORIES = {
    UNCONFIRMED_FORMULA_CACHE: "unconfirmed_formula_cache",
    FORMULA_WITHOUT_CACHE: "formula_without_cache",
    BLANK_CELL: "blank_cell",
    PLAIN_NUMBER: "plain_number",
    INVALID_CALENDAR_DATE: "invalid_calendar_date",
    INVALID_DATE_TEXT: "invalid_date_text",
    INVALID_CACHED_VALUE: "invalid_cached_value",
    INVALID_VALUE: "invalid_value",
}


def select_records(
    records,
    include_shipments=None,
    exclude_shipments=None,
    order_by_shipment=None,
):
    """Filter and deterministically order records.

    Shipment numbers are used only for selection and ordering; they never
    feed into confirmation scope.
    """

    selected = []
    include = set(include_shipments) if include_shipments else None
    exclude = set(exclude_shipments) if exclude_shipments else set()
    for record in records:
        shipment = (
            "" if record.shipment_no is None else str(record.shipment_no)
        )
        if include is not None and shipment not in include:
            continue
        if shipment in exclude:
            continue
        selected.append(record)
    if order_by_shipment:
        position = {
            value: index for index, value in enumerate(order_by_shipment)
        }
        selected.sort(
            key=lambda record: (
                position.get(str(record.shipment_no), len(position)),
                str(record.shipment_no),
            )
        )
    return selected


def _ensure_accepted(records):
    for record in records:
        if not record.accepted or record.date_value is None:
            raise ValueError(
                "only accepted records with calendar dates may be written: "
                "%s" % record.record_id
            )


def write_xlsx(records, path):
    """Write accepted records as true Excel dates.

    The date column is rewritten as a ``date`` cell with a date number
    format.  Only the configured date column is changed.
    """

    _ensure_accepted(records)
    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    grouped = {}
    order = []
    for record in records:
        if record.sheet not in grouped:
            grouped[record.sheet] = []
            order.append(record.sheet)
        grouped[record.sheet].append(record)

    for sheet_name in order:
        sheet_records = grouped[sheet_name]
        worksheet = workbook.create_sheet(title=sheet_name)
        headers = list(sheet_records[0].headers)
        worksheet.append(headers)
        for record in sheet_records:
            row_values = list(record.row_values)
            while len(row_values) < len(headers):
                row_values.append(None)
            worksheet.append(row_values[: len(headers)])
            row_number = worksheet.max_row
            date_cell = worksheet.cell(
                row=row_number, column=record.date_column_index + 1
            )
            date_cell.value = record.date_value
            date_cell.number_format = "yyyy-mm-dd"

    workbook.save(path)


def write_csv(records, path, sheet=None):
    """Write accepted records as ISO calendar dates.

    :param records: accepted records, already filtered and ordered
    :param str path: destination CSV path
    :param str sheet: keep only records from this page
    """

    selected = [
        record for record in records if sheet is None or record.sheet == sheet
    ]
    _ensure_accepted(selected)
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if not selected:
            return
        headers = list(selected[0].headers)
        writer.writerow(headers)
        for record in selected:
            row_values = list(record.row_values)
            while len(row_values) < len(headers):
                row_values.append("")
            row_values = row_values[: len(headers)]
            date_index = record.date_column_index
            row_values[date_index] = record.date_value.isoformat()
            writer.writerow(row_values)


def problem_receipt(records, deliveries=None):
    """Build a JSON-compatible receipt for unaccepted records."""

    issues = []
    for record in records:
        if record.accepted:
            continue
        issues.append(
            {
                "record_id": record.record_id,
                "delivery": record.ledger_name,
                "ledger_version": record.ledger_version,
                "sheet": record.sheet,
                "cell": record.cell,
                "row": record.row,
                "shipment_no": record.shipment_no,
                "category": ISSUE_CATEGORIES.get(record.status, record.status),
                "status": record.status,
                "reason": record.reason,
                "source": record.source,
                "formula": record.formula,
                "raw_value": canonical_value(record.raw_value),
                "cached_value": canonical_value(record.cached_value),
                "has_formula": record.has_formula,
                "has_cached_value": record.has_cached_value,
                "history": list(record.history),
            }
        )
    payload = {"issues": issues}
    if deliveries is not None:
        payload["deliveries"] = [delivery.to_dict() for delivery in deliveries]
    return payload


def write_problem_receipt(records, path, deliveries=None):
    payload = problem_receipt(records, deliveries=deliveries)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(
            payload, stream, ensure_ascii=False, indent=2, sort_keys=True
        )
        stream.write("\n")
    return payload


def run_summary(run, deliveries=None):
    """A compact status summary suitable for a console or test."""

    deliveries = deliveries if deliveries is not None else run.deliveries
    summary = []
    for delivery in deliveries:
        counts = {}
        for record in delivery.records:
            counts[record.status] = counts.get(record.status, 0) + 1
        summary.append(
            {
                "name": delivery.name,
                "version": delivery.version,
                "kind": delivery.kind,
                "date_cells": len(delivery.records),
                "counts": counts,
            }
        )
    return summary
