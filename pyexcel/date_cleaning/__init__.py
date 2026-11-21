"""
pyexcel.date_cleaning
~~~~~~~~~~~~~~~~~~~~~

Date cleaning for receiving ledgers in XLSX and CSV.

The module separates literal dates from formula-driven dates.  Literal
dates may be accepted directly.  A formula date is accepted only when a
finance confirmation for the same ledger version, page, cell and cached
value accompanies that delivery.  Excel formulas are never recalculated.
"""

from .dates import (
    canonical_value,
    confirmation_value_matches,
    coerce_confirmation_date,
    interpret_cached_formula_value,
    interpret_excel_value,
    parse_date_text,
)
from .engine import CleaningRun
from .loader import load_confirmations, load_delivery
from .models import (
    ACCEPTED,
    BLANK_CELL,
    CONFIRMATION_APPLIED,
    CONFIRMATION_NO_CHANGE,
    CONFIRMATION_REJECTED,
    FORMULA_WITHOUT_CACHE,
    INVALID_CACHED_VALUE,
    INVALID_CALENDAR_DATE,
    INVALID_DATE_TEXT,
    INVALID_VALUE,
    PLAIN_NUMBER,
    Confirmation,
    ConfirmationResult,
    DateCleaningError,
    DateRecord,
    Delivery,
    UNCONFIRMED_FORMULA_CACHE,
)
from .output import (
    problem_receipt,
    run_summary,
    select_records,
    write_csv,
    write_problem_receipt,
    write_xlsx,
)

__all__ = [
    "ACCEPTED",
    "BLANK_CELL",
    "CONFIRMATION_APPLIED",
    "CONFIRMATION_NO_CHANGE",
    "CONFIRMATION_REJECTED",
    "FORMULA_WITHOUT_CACHE",
    "INVALID_CACHED_VALUE",
    "INVALID_CALENDAR_DATE",
    "INVALID_DATE_TEXT",
    "INVALID_VALUE",
    "PLAIN_NUMBER",
    "UNCONFIRMED_FORMULA_CACHE",
    "CleaningRun",
    "Confirmation",
    "ConfirmationResult",
    "DateCleaningError",
    "DateRecord",
    "Delivery",
    "canonical_value",
    "confirmation_value_matches",
    "coerce_confirmation_date",
    "interpret_cached_formula_value",
    "interpret_excel_value",
    "load_confirmations",
    "load_delivery",
    "parse_date_text",
    "problem_receipt",
    "run_summary",
    "select_records",
    "write_csv",
    "write_problem_receipt",
    "write_xlsx",
]
