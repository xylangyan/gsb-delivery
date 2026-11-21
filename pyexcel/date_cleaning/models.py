"""
pyexcel.date_cleaning.models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Data structures used by the receiving-ledger date cleaner.
"""

import hashlib
import os

from .dates import canonical_value


ACCEPTED = "accepted"
UNCONFIRMED_FORMULA_CACHE = "unconfirmed_formula_cache"
FORMULA_WITHOUT_CACHE = "formula_without_cache"
BLANK_CELL = "blank_cell"
PLAIN_NUMBER = "plain_number"
INVALID_CALENDAR_DATE = "invalid_calendar_date"
INVALID_DATE_TEXT = "invalid_date_text"
INVALID_CACHED_VALUE = "invalid_cached_value"
INVALID_VALUE = "invalid_value"

SOURCE_FORMULA = "formula"
SOURCE_XLSX_LITERAL = "xlsx_literal"
SOURCE_CSV_TEXT = "csv_text"

CONFIRMATION_APPLIED = "applied"
CONFIRMATION_REJECTED = "rejected"
CONFIRMATION_NO_CHANGE = "no_change"


class DateCleaningError(ValueError):
    """Raised when a ledger cannot be read as a receiving ledger."""


def ledger_version_of(path):
    """Return a stable content identifier for a ledger file."""

    digest = hashlib.sha256()
    with open(path, "rb") as ledger_file:
        for chunk in iter(lambda: ledger_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def ledger_name_of(path):
    return os.path.basename(path)


class DateRecord:
    """One date cell in one ledger page."""

    def __init__(
        self,
        ledger_version,
        ledger_name,
        sheet,
        cell,
        row,
        column,
        shipment_no,
        source,
        formula,
        raw_value,
        cached_value,
        has_formula,
        has_cached_value,
        number_format,
        headers,
        row_values,
        date_column_index,
        shipment_column_index,
        epoch=None,
    ):
        self.record_id = "%s:%s:%s" % (ledger_version, sheet, cell)
        self.ledger_version = ledger_version
        self.ledger_name = ledger_name
        self.sheet = sheet
        self.cell = cell
        self.row = row
        self.column = column
        self.shipment_no = shipment_no
        self.source = source
        self.formula = formula
        self.raw_value = raw_value
        self.cached_value = cached_value
        self.has_formula = has_formula
        self.has_cached_value = has_cached_value
        self.number_format = number_format
        self.headers = tuple(headers)
        self.row_values = tuple(row_values)
        self.date_column_index = date_column_index
        self.shipment_column_index = shipment_column_index
        self.epoch = epoch
        self.date_value = None
        self.status = None
        self.reason = None
        self.accepted = False
        self.confirmation_id = None
        self.history = []

    def add_history(self, event, status, reason, **details):
        entry = {
            "event": event,
            "status": status,
            "reason": reason,
        }
        entry.update(details)
        self.history.append(entry)

    @property
    def cached_date(self):
        """The calendar date read from this formula's cache, if any."""
        if self.has_formula and self.date_value is not None:
            return self.date_value
        return None

    def set_status(
        self, status, reason, accepted, event="classified", **details
    ):
        self.status = status
        self.reason = reason
        self.accepted = accepted
        self.add_history(event, status, reason, **details)

    def to_dict(self, include_rows=False):
        payload = {
            "record_id": self.record_id,
            "ledger_version": self.ledger_version,
            "ledger_name": self.ledger_name,
            "sheet": self.sheet,
            "cell": self.cell,
            "row": self.row,
            "shipment_no": self.shipment_no,
            "source": self.source,
            "formula": self.formula,
            "raw_value": canonical_value(self.raw_value),
            "cached_value": canonical_value(self.cached_value),
            "has_formula": self.has_formula,
            "has_cached_value": self.has_cached_value,
            "number_format": self.number_format,
            "date_value": (
                self.date_value.isoformat() if self.date_value else None
            ),
            "status": self.status,
            "reason": self.reason,
            "accepted": self.accepted,
            "confirmation_id": self.confirmation_id,
            "history": list(self.history),
        }
        if include_rows:
            payload["headers"] = list(self.headers)
            payload["row_values"] = [
                canonical_value(value) for value in self.row_values
            ]
            payload["date_column_index"] = self.date_column_index
            payload["shipment_column_index"] = self.shipment_column_index
        return payload


class Delivery:
    """A single supplied ledger file and the rows read from it."""

    def __init__(self, path, version, kind, sheets):
        self.path = path
        self.name = ledger_name_of(path)
        self.version = version
        self.kind = kind
        self.sheets = sheets
        self.records = []

    def add_record(self, record):
        self.records.append(record)

    def to_dict(self):
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "kind": self.kind,
            "sheets": list(self.sheets),
            "date_cells": len(self.records),
        }


class Confirmation:
    """A finance confirmation for one cached formula cell."""

    def __init__(
        self,
        confirmation_id,
        ledger_version,
        sheet,
        cell,
        cached_value,
        shipment_no=None,
        note=None,
        target_ledger_version=None,
        target_delivery=None,
    ):
        self.confirmation_id = confirmation_id
        self.ledger_version = ledger_version
        self.sheet = sheet
        self.cell = str(cell).replace("$", "").upper()
        self.cached_value = cached_value
        self.shipment_no = shipment_no
        self.note = note
        self.target_ledger_version = target_ledger_version
        self.target_delivery = target_delivery

    @classmethod
    def from_dict(cls, payload):
        return cls(
            confirmation_id=payload.get(
                "confirmation_id", payload.get("id")
            ),
            ledger_version=payload.get(
                "ledger_version", payload.get("version")
            ),
            sheet=payload.get("sheet", payload.get("page")),
            cell=payload.get("cell"),
            cached_value=payload.get(
                "cached_value", payload.get("cache")
            ),
            shipment_no=payload.get(
                "shipment_no", payload.get("shipment_number")
            ),
            note=payload.get("note"),
            target_ledger_version=payload.get(
                "target_ledger_version", payload.get("target_version")
            ),
            target_delivery=payload.get("target_delivery"),
        )

    def to_dict(self):
        return {
            "confirmation_id": self.confirmation_id,
            "ledger_version": self.ledger_version,
            "sheet": self.sheet,
            "cell": self.cell,
            "cached_value": canonical_value(self.cached_value),
            "shipment_no": self.shipment_no,
            "note": self.note,
            "target_ledger_version": self.target_ledger_version,
            "target_delivery": self.target_delivery,
        }


class ConfirmationResult:
    """The auditable outcome of offering a confirmation to a ledger."""

    def __init__(
        self,
        confirmation,
        outcome,
        reason,
        target_version=None,
        record=None,
        affected_record_ids=None,
    ):
        self.confirmation = confirmation
        self.outcome = outcome
        self.reason = reason
        self.target_version = target_version
        self.record = record
        self.affected_record_ids = list(affected_record_ids or [])

    def to_dict(self):
        return {
            "confirmation": (
                self.confirmation.to_dict()
                if self.confirmation is not None
                else None
            ),
            "outcome": self.outcome,
            "reason": self.reason,
            "target_version": self.target_version,
            "record_id": (
                self.record.record_id if self.record is not None else None
            ),
            "affected_record_ids": list(self.affected_record_ids),
        }
