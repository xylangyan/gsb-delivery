# -*- coding: UTF-8 -*-
#
# Copyright 2011-2019 by Dirk Gorissen, Stephen Rauch and Contributors
# All rights reserved.
# This file is part of the Pycel Library, Licensed under GPLv3 (the 'License')
# You may not use this work except in compliance with the License.
# You may obtain a copy of the Licence at:
#   https://www.gnu.org/licenses/gpl-3.0.en.html

"""
Settlement (as-of) clocks for replaying historical workbook periods.

The Excel TODAY() and NOW() functions normally read the system clock.  When
a workbook is used to replay a historical settlement report it is often
necessary to make those functions see a caller supplied instant instead.  A
:class:`SettlementClock` keeps such an instant together with the timezone
the accounting period belongs to.

All values are still reported the Excel way: dates are day serial numbers
for the 1900 date system and times are fractions of a day.
"""

import ast
import datetime as dt
import math
import re
import threading

try:
    import contextvars
except ImportError:  # pragma: no cover - python 3.6
    contextvars = None

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = KeyError

try:
    import dateutil.parser
    import dateutil.tz
except ImportError:  # pragma: no cover - dateutil is a pycel requirement
    dateutil = None


DATE_ZERO = dt.datetime(1899, 12, 30)
SECONDS_PER_DAY = 24 * 60 * 60

# Active settlement clock for the current evaluation context.  Each top level
# evaluation pins its own clock, so formulas evaluated together all see the
# same instant even when several compilers are interleaved.
if contextvars is not None:
    _ACTIVE_CLOCK = contextvars.ContextVar(
        'pycel_settlement_clock', default=None)

    def get_clock():
        """Return the settlement clock active for this evaluation, if any."""
        return _ACTIVE_CLOCK.get()

    def set_clock(clock):
        """Pin a settlement clock for the current context."""
        return _ACTIVE_CLOCK.set(clock)

    def reset_clock(token):
        """Release a clock pinned with set_clock()."""
        _ACTIVE_CLOCK.reset(token)

else:  # pragma: no cover - python 3.6 fallback
    _ACTIVE_CLOCK = threading.local()

    def get_clock():
        return getattr(_ACTIVE_CLOCK, 'clock', None)

    def set_clock(clock):
        previous = get_clock()
        _ACTIVE_CLOCK.clock = clock
        return previous

    def reset_clock(token):
        _ACTIVE_CLOCK.clock = token


class ClockError(ValueError):
    """Base class for settlement clock errors."""


class NaiveDatetimeError(ClockError):
    """Raised when a settlement instant has no timezone information."""


class NaiveSettlementTimeError(NaiveDatetimeError):
    """A settlement time without a timezone was supplied."""


class TimezoneRequiredError(NaiveSettlementTimeError):
    """A settlement time requires a timezone."""


class TimezoneError(ClockError):
    """Raised when a timezone cannot be interpreted."""


class SettlementTimeError(ClockError):
    """Raised when a settlement time cannot be interpreted."""


def resolve_timezone(timezone):
    """Turn a timezone name or tzinfo into a tzinfo object."""
    if timezone is None:
        return None
    if isinstance(timezone, dt.tzinfo):
        return timezone
    if isinstance(timezone, str):
        offset = re.fullmatch(
            r'(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?',
            timezone.strip(), re.IGNORECASE)
        if offset:
            sign = -1 if offset.group(1) == '-' else 1
            hours = int(offset.group(2))
            minutes = int(offset.group(3) or 0)
            return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
        if ZoneInfo is not None:
            try:
                return ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        if dateutil is not None:
            tz = dateutil.tz.gettz(timezone)
            if tz is not None:
                return tz
        raise TimezoneError(f"Unknown timezone: {timezone!r}")
    raise TimezoneError(f"Invalid timezone: {timezone!r}")


def is_naive(value):
    """Does this value carry timezone information?"""
    if isinstance(value, dt.datetime):
        return value.tzinfo is None or value.utcoffset() is None
    if isinstance(value, str):
        try:
            value = dateutil.parser.isoparse(value)
        except (TypeError, ValueError):
            return True
        return value.tzinfo is None or value.utcoffset() is None
    return True


def _attach_timezone(value, tz):
    if isinstance(value, str):
        value = dateutil.parser.isoparse(value)
    # A named zone may already be attached; keep it when it has one.
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return value.replace(tzinfo=tz)


def coerce_datetime(value):
    """Return a timezone aware datetime or raise NaiveDatetimeError."""
    if isinstance(value, dt.datetime):
        when = value
    elif isinstance(value, str):
        if dateutil is None:  # pragma: no cover
            raise ClockError('String settlement times require python-dateutil')
        try:
            when = dateutil.parser.isoparse(value)
        except (TypeError, ValueError) as exc:
            raise SettlementTimeError(
                f"Invalid settlement time: {value!r}") from exc
    elif isinstance(value, SettlementClock):
        return value.instant
    else:
        raise SettlementTimeError(
            'Settlement time must be a timezone-aware datetime, '
            f'not {type(value).__name__}')

    if when.tzinfo is None or when.utcoffset() is None:
        raise NaiveSettlementTimeError(
            'Settlement time must be timezone-aware; '
            'a timezone offset or timezone name is required')
    return when


def local_datetime(when, timezone=None):
    """Return the wall clock datetime of an aware instant in a timezone."""
    when = coerce_datetime(when)
    tz = resolve_timezone(timezone) or when.tzinfo
    return when.astimezone(tz).replace(tzinfo=None)


SYSTEM_TIME = object()


def excel_date_serial(a_date):
    """Excel 1900 date serial for a date (including its 1900 leap bug)."""
    serial = (a_date - DATE_ZERO.date()).days
    if serial <= 60:
        serial -= 1
    return serial


def datetime_to_serial(when):
    """Convert a naive/local datetime to an Excel 1900 date serial."""
    delta = when - DATE_ZERO
    serial = (delta.days +
              (delta.seconds + delta.microseconds / 1000000) / SECONDS_PER_DAY)
    if delta.days <= 60:
        serial -= 1
    return serial


def serial_to_datetime(serial):
    """Convert an Excel 1900 date serial to a naive datetime."""
    return DATE_ZERO + dt.timedelta(days=serial)


def serial_to_today(serial):
    """Return the integer day serial for an Excel date serial."""
    return int(math.floor(serial))


class SettlementClock:
    """A fixed, timezone aware instant used for TODAY() and NOW()."""

    __slots__ = ('_instant', '_timezone', '_tokens', '_system')

    # Tolerate the same instant expressed twice, e.g. once with a named zone
    # and once with a fixed offset, without refreshing cached clock values.
    _SERIAL_TOLERANCE = 1e-9

    def __init__(self, when, timezone=None):
        self._system = False
        tz = resolve_timezone(timezone)
        if when is SYSTEM_TIME:
            when = dt.datetime.now().astimezone()
            if tz is not None:
                when = when.astimezone(tz)
            self._system = True
        elif tz is not None and is_naive(when):
            # A wall clock time is unambiguous once its timezone is given.
            when = _attach_timezone(when, tz)
        instant = coerce_datetime(when)
        if tz is None:
            tz = instant.tzinfo
        self._instant = instant
        self._timezone = tz
        self._tokens = []

    def __getstate__(self):
        return dict(
            _instant=self._instant,
            _timezone=self._timezone,
            _system=self._system,
        )

    def __setstate__(self, state):
        self._instant = state['_instant']
        self._timezone = state['_timezone']
        self._system = state.get('_system', False)
        self._tokens = []

    def __enter__(self):
        """Pin this clock for all formulas evaluated in the block."""
        self._tokens.append(set_clock(self))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._tokens:
            reset_clock(self._tokens.pop())
        return False

    @classmethod
    def system(cls, timezone=None):
        """Capture the system time once, as a fixed clock."""
        clock = cls(SYSTEM_TIME, timezone)
        return clock

    @property
    def instant(self):
        """The absolute (aware) settlement instant."""
        return self._instant

    @property
    def when(self):
        return self._instant

    @property
    def timezone(self):
        """The timezone of the accounting period."""
        return self._timezone

    @property
    def tzinfo(self):
        return self._timezone

    @property
    def local(self):
        """The settlement wall clock time in the accounting timezone."""
        return self._instant.astimezone(self._timezone).replace(tzinfo=None)

    @property
    def local_datetime(self):
        return self.local

    @property
    def datetime(self):
        return self.local

    @property
    def serial(self):
        """The Excel 1900 date serial, including the time of day."""
        return datetime_to_serial(self.local)

    @property
    def today(self):
        """The Excel 1900 serial number for the local date."""
        return excel_date_serial(self.local.date())

    @property
    def date_serial(self):
        return self.today

    @property
    def serial_number(self):
        return self.serial

    def replace(self, when=None, timezone=None):
        """Return a clock for another instant and/or timezone."""
        return SettlementClock(
            self._instant if when is None else when,
            self._timezone if timezone is None else timezone)

    def astimezone(self, timezone):
        """Return the same instant viewed in another timezone."""
        return SettlementClock(self._instant, timezone)

    def advance(self, delta):
        """Return a clock moved by a timedelta from this instant."""
        if isinstance(delta, (int, float)):
            delta = dt.timedelta(days=delta)
        if not isinstance(delta, dt.timedelta):
            raise SettlementTimeError('advance() requires a timedelta')
        return SettlementClock(self._instant + delta, self._timezone)

    def isoformat(self):
        """The settlement instant in ISO 8601 form."""
        return self._instant.isoformat()

    def close_enough(self, other, tolerance=1e-9):
        """Compare the effective local serial values of two clocks."""
        try:
            other = other if isinstance(other, SettlementClock) else \
                SettlementClock(other)
        except ClockError:
            return False
        return abs(self.serial - other.serial) <= max(
            tolerance, self._SERIAL_TOLERANCE)

    def __eq__(self, other):
        if isinstance(other, SettlementClock):
            return self._instant == other._instant
        if isinstance(other, dt.datetime):
            if other.tzinfo is None or other.utcoffset() is None:
                return False
            return self._instant == other
        return NotImplemented

    def __hash__(self):
        return hash(self._instant)

    def __repr__(self):
        return (f'SettlementClock({self._instant.isoformat()!r}, '
                f'timezone={self._timezone!r})')

    def __str__(self):
        return self._instant.isoformat()


# Common alternative names for the same concept.
AsOfClock = SettlementClock
EvaluationClock = SettlementClock
ExcelClock = SettlementClock
Clock = SettlementClock


def is_clock_name(name):
    """Is the given Python name one of the clock functions in a formula?"""
    return name in ('now', 'today')


def clock_dependencies(python_code):
    """Return the set of clock functions a compiled formula calls.

    The compiled code is Python source, so use the AST rather than a regular
    expression.  This avoids treating the text inside a string literal (e.g.
    a label) as a clock dependency.
    """
    if not python_code:
        return frozenset()
    try:
        tree = ast.parse(python_code, mode='eval')
    except (SyntaxError, ValueError):
        return frozenset(
            name for name in ('now', 'today')
            if re.search(rf'(?<![\w.]){name}\s*\(', python_code))

    return frozenset(
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
        is_clock_name(node.func.id)
    )
