from datetime import datetime, timedelta

from ics import Calendar
from ics.migration import (
    BASIC_READER,
    RFC5545_READER,
    ReaderIntersection,
    migrate_calendar,
)
from ics.migration.extractor import extract_feed
from ics.migration.readers import (
    FORM_EXRULE,
    FORM_MULTI_RRULE_COMPONENT,
    FORM_OVERLAPPING_SEGMENTS,
)
from ics.timezone import UTC

from .conftest import WINDOW_END, WINDOW_START


def test_reader_rule_gate_detects_interval_limit():
    assert BASIC_READER.rule_supported("WEEKLY", 1) is None
    limitation = BASIC_READER.rule_supported("WEEKLY", 2)
    assert limitation and "INTERVAL" in limitation
    # the full RFC reader accepts the same rule
    assert RFC5545_READER.rule_supported("WEEKLY", 2) is None


def test_reader_form_gate_detects_missing_exception_forms():
    assert BASIC_READER.form_supported(FORM_EXRULE) is False
    assert BASIC_READER.form_supported(FORM_MULTI_RRULE_COMPONENT) is False
    assert BASIC_READER.form_supported(FORM_OVERLAPPING_SEGMENTS) is False
    intersection = ReaderIntersection()
    assert intersection.form_supported(FORM_EXRULE) is not None
    assert intersection.form_supported(FORM_MULTI_RRULE_COMPONENT) is not None


def test_round_trip_preserves_every_absolute_instant(feeds, manifest):
    old_text, new_text = feeds
    result = migrate_calendar(
        old_text, new_text, manifest, WINDOW_START, WINDOW_END
    )
    assert result.unresolved == []

    original = {
        (occ.uid, occ.rid.astimezone(UTC) if occ.rid is not None else None):
        occ.start.astimezone(UTC)
        for occ in extract_feed("old", old_text, WINDOW_START, WINDOW_END).occurrences
        if not occ.cancelled
    }

    # expand the emitted document the same way the input was expanded
    emitted_feed = extract_feed(
        "out", result.ics_text, WINDOW_START, WINDOW_END
    )
    emitted_starts = {
        occ.start.astimezone(UTC)
        for occ in emitted_feed.occurrences
        if not occ.cancelled
    }

    # every surviving original absolute instant is still represented
    for (uid, rid), instant in original.items():
        assert instant in emitted_starts, f"missing {uid} {rid} at {instant}"

    # the two co-instant Dec 22 bookings survive as distinct components
    dec22 = datetime(2026, 12, 22, 10, tzinfo=UTC)
    coincident = [
        occ for occ in emitted_feed.occurrences
        if occ.start.astimezone(UTC) == dec22 and not occ.cancelled
    ]
    assert len(coincident) == 2
    assert {occ.uid for occ in coincident} == {
        "weekly@venue",
        "clinic@venue",
    }


def test_locked_and_future_use_distinct_timezone_blocks(feeds, manifest):
    old_text, new_text = feeds
    result = migrate_calendar(
        old_text, new_text, manifest, WINDOW_START, WINDOW_END
    )
    calendar = Calendar(result.ics_text)
    locked = next(
        event.begin.utcoffset()
        for event in calendar.events
        if event.uid == "weekly@venue"
        and event.begin is not None
        and event.begin.strftime("%Y%m%d") == "20261208"
    )
    future = next(
        event.begin.utcoffset()
        for event in calendar.events
        if event.uid == "weekly@venue"
        and event.begin is not None
        and event.begin.strftime("%Y%m%d") == "20261229"
    )
    assert locked == timedelta(hours=1)
    assert future == timedelta(hours=2)
    # both VTIMEZONE blocks are embedded with distinct TZIDs
    assert "TZID:Site/Custom@legacy\r\n" in result.ics_text
    assert "TZID:Site/Custom\r\n" in result.ics_text
