# -*- coding: utf-8 -*-
"""shiftaudit

Pre-work conflict audit for old paper shift orders parsed by the stock
parsedatetime English locale.

Two schedule routes are audited against shared collection machines.  For
every candidate integer-second dial offset, each order note is parsed with
``parsedatetime.Calendar.evalRanges`` using a local anchor derived by:

    real UTC anchor + offset  ->  local wall clock (via the given zone)

The parsed local window is converted back to a real UTC interval.  An
offset is bookable only when every order on both routes is bookable and no
two orders on the same device overlap in real time (left-closed,
right-open).
"""

from .model import (
    Order,
    Route,
    AnchorBatch,
    AuditConfig,
    BookedRange,
    OrderResult,
    Collision,
    OffsetResult,
    TIE_EARLIER,
    TIE_LATER,
    TIE_NONE,
    REASON_NOT_A_RANGE,
    REASON_CLOCK_FALLBACK,
    REASON_MISSING_CELL,
    REASON_INVERTED,
)
from .clock import (
    WALL_UNIQUE,
    WALL_FOLD,
    WALL_GAP,
    utc_to_local,
    local_to_utc,
    local_naive,
    utc_naive,
    classify_wall_clock,
)
from .engine import ScheduleAuditor, merge_ranges

__all__ = [
    'Order',
    'Route',
    'AnchorBatch',
    'AuditConfig',
    'BookedRange',
    'OrderResult',
    'Collision',
    'OffsetResult',
    'ScheduleAuditor',
    'merge_ranges',
    'utc_to_local',
    'local_to_utc',
    'local_naive',
    'utc_naive',
    'classify_wall_clock',
    'WALL_UNIQUE',
    'WALL_FOLD',
    'WALL_GAP',
    'TIE_EARLIER',
    'TIE_LATER',
    'TIE_NONE',
    'REASON_NOT_A_RANGE',
    'REASON_CLOCK_FALLBACK',
    'REASON_MISSING_CELL',
    'REASON_INVERTED',
]
