"""
pyexcel.date_cleaning.dates
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Strict date handling for receiving ledgers.

The cleaner deliberately does not evaluate Excel formulas.  It only
interprets values that are already present in the workbook: literal
dates and the cached results of formulas.
"""

import datetime
import re

try:
    from openpyxl.utils.datetime import from_excel
except ImportError:  # pragma: no cover - openpyxl is optional at runtime
    from_excel = None


BLANK = "blank"
VALID_DATE = "valid_date"
PLAIN_NUMBER = "plain_number"
INVALID_CALENDAR_DATE = "invalid_calendar_date"
INVALID_DATE_TEXT = "invalid_date_text"
INVALID_VALUE = "invalid_value"

_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_DATE_LIKE_RE = re.compile(
    r"^\d{1,4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,4}\s*日?$"
)

DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
)


def parse_date_text(value, date_formats=None):
    """Parse text as a calendar date.

    Returns ``(date, kind)`` where ``kind`` is one of :data:`BLANK`,
    :data:`VALID_DATE`, :data:`PLAIN_NUMBER`,
    :data:`INVALID_CALENDAR_DATE` or :data:`INVALID_DATE_TEXT`.

    A bare number is never accepted as a date.  This is the guard against
    an Excel serial number or a plain CSV number being mistaken for a
    calendar date.
    """

    if value is None:
        return None, BLANK
    if isinstance(value, datetime.datetime):
        return value.date(), VALID_DATE
    if isinstance(value, datetime.date):
        return value, VALID_DATE
    if isinstance(value, bool):
        return None, INVALID_VALUE

    text = str(value).strip()
    if text == "":
        return None, BLANK

    if _PLAIN_NUMBER_RE.match(text):
        return None, PLAIN_NUMBER

    formats = date_formats or DEFAULT_DATE_FORMATS
    for date_format in formats:
        try:
            parsed = datetime.datetime.strptime(text, date_format)
        except ValueError:
            continue
        return parsed.date(), VALID_DATE

    if _DATE_LIKE_RE.match(text):
        return None, INVALID_CALENDAR_DATE
    return None, INVALID_DATE_TEXT


def interpret_excel_value(
    value, number_format, date_formats=None, epoch=None
):
    """Interpret a literal Excel value in a date column.

    Numeric values are dates only when the cell carries a date number
    format.  A number without a date format is an ordinary number.
    """

    if value is None:
        return None, BLANK
    if isinstance(value, datetime.datetime):
        return value.date(), VALID_DATE
    if isinstance(value, datetime.date):
        return value, VALID_DATE
    if isinstance(value, bool):
        return None, INVALID_VALUE
    if isinstance(value, (int, float)):
        if _is_date_format(number_format):
            return _from_serial(value, epoch=epoch), VALID_DATE
        return None, PLAIN_NUMBER
    return parse_date_text(value, date_formats=date_formats)


def interpret_cached_formula_value(
    value, number_format, date_formats=None, epoch=None
):
    """Interpret a cached formula result in a date column.

    The cache is used as-is.  No formula is recalculated.
    """

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if _is_date_format(number_format):
            return _from_serial(value, epoch=epoch), VALID_DATE
        return None, PLAIN_NUMBER
    return interpret_excel_value(
        value, number_format, date_formats=date_formats, epoch=epoch
    )


def _is_date_format(number_format):
    if not number_format:
        return False
    try:
        from openpyxl.styles.numbers import is_date_format
    except ImportError:  # pragma: no cover
        return False
    return is_date_format(number_format)


def _from_serial(value, epoch=None):
    if from_excel is None:  # pragma: no cover
        raise RuntimeError("openpyxl is required to read Excel dates")
    converted = from_excel(value, epoch=epoch)
    if isinstance(converted, datetime.datetime):
        return converted.date()
    if isinstance(converted, datetime.date):
        return converted
    return converted


def canonical_value(value):
    """Return a stable, JSON-friendly representation of a raw value."""

    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def coerce_confirmation_date(value, date_formats=None):
    """Try to read a confirmation's cached value as a calendar date."""

    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    if value is None:
        return None
    parsed, kind = parse_date_text(value, date_formats=date_formats)
    if kind == VALID_DATE:
        return parsed
    return None


def confirmation_value_matches(confirmation_value, record):
    """Check a confirmation's cached value against a record's cache."""

    if isinstance(confirmation_value, (int, float)) and not isinstance(
        confirmation_value, bool
    ):
        parsed, kind = interpret_cached_formula_value(
            confirmation_value, record.number_format
        )
        if kind == VALID_DATE:
            return (
                record.cached_date is not None
                and parsed == record.cached_date
            )
        return False

    confirmed_date = coerce_confirmation_date(confirmation_value)
    if confirmed_date is not None and record.cached_date is not None:
        return confirmed_date == record.cached_date

    expected = canonical_value(confirmation_value)
    actual = canonical_value(record.cached_value)
    if expected == actual:
        return True

    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip() == actual.strip()

    return False
