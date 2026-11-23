from datetime import datetime, timedelta, timezone

import pytest

from ics.migration import NewSlot
from ics.migration.extractor import extract_feed
from ics.timezone import UTC

OLD_TZ = """BEGIN:VTIMEZONE
TZID:Site/Custom
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0100
TZOFFSETTO:+0100
TZNAME:CEU
RRULE:FREQ=YEARLY
END:STANDARD
END:VTIMEZONE
"""

NEW_TZ = """BEGIN:VTIMEZONE
TZID:Site/Custom
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0200
TZOFFSETTO:+0200
TZNAME:CEU2
RRULE:FREQ=YEARLY
END:STANDARD
END:VTIMEZONE
"""

WINDOW_START = datetime(2026, 12, 1, tzinfo=UTC)
WINDOW_END = datetime(2027, 1, 31, 23, 59, 59, tzinfo=UTC)


def build_old_calendar() -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:old-feed",
            OLD_TZ,
            # out-of-window history: must be kept verbatim on the legacy zone
            "BEGIN:VEVENT",
            "UID:history@venue",
            "SUMMARY:Old November session",
            "DTSTART;TZID=Site/Custom:20261103T090000",
            "DTEND;TZID=Site/Custom:20261103T100000",
            "DTSTAMP:20261001T000000Z",
            "END:VEVENT",
            # weekly Tuesday series with one EXDATE
            "BEGIN:VEVENT",
            "UID:weekly@venue",
            "SUMMARY:Weekly court",
            "DTSTART;TZID=Site/Custom:20261201T090000",
            "DTEND;TZID=Site/Custom:20261201T100000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20270127T235959Z",
            "EXDATE;TZID=Site/Custom:20261215T090000",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            # Dec 22 delayed by two hours
            "BEGIN:VEVENT",
            "UID:weekly@venue",
            "RECURRENCE-ID;TZID=Site/Custom:20261222T090000",
            "DTSTART;TZID=Site/Custom:20261222T110000",
            "DTEND;TZID=Site/Custom:20261222T120000",
            "SUMMARY:Weekly court delayed",
            "SEQUENCE:1",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            # Jan 5 cancelled
            "BEGIN:VEVENT",
            "UID:weekly@venue",
            "RECURRENCE-ID;TZID=Site/Custom:20270105T090000",
            "STATUS:CANCELLED",
            "DTSTART;TZID=Site/Custom:20270105T090000",
            "DTEND;TZID=Site/Custom:20270105T100000",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            # clinic series whose Dec 22 move lands on the same UTC instant
            "BEGIN:VEVENT",
            "UID:clinic@venue",
            "SUMMARY:Skills clinic",
            "DTSTART;TZID=Site/Custom:20261201T140000",
            "DTEND;TZID=Site/Custom:20261201T150000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=4",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:clinic@venue",
            "RECURRENCE-ID;TZID=Site/Custom:20261222T140000",
            "DTSTART;TZID=Site/Custom:20261222T110000",
            "DTEND;TZID=Site/Custom:20261222T120000",
            "SUMMARY:Skills clinic rescheduled",
            "SEQUENCE:1",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )


def build_new_calendar() -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:new-feed",
            NEW_TZ,
            "END:VCALENDAR",
        ]
    )


def build_biweekly_calendar() -> str:
    # every second Tuesday -> INTERVAL=2, unsupported by the basic reader
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:old-feed",
            OLD_TZ,
            "BEGIN:VEVENT",
            "UID:biweekly@venue",
            "DTSTART;TZID=Site/Custom:20261201T090000",
            "DTEND;TZID=Site/Custom:20261201T100000",
            "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=4",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )


@pytest.fixture
def feeds():
    return build_old_calendar(), build_new_calendar()


@pytest.fixture
def weekly_identities(feeds):
    old_text, _ = feeds
    feed = extract_feed("old", old_text, WINDOW_START, WINDOW_END)

    def find(day, month=12):
        return next(
            o
            for o in feed.occurrences
            if o.uid == "weekly@venue"
            and o.civil_start.day == day
            and o.civil_start.month == month
        )

    return {"dec1": find(1), "dec8": find(8), "jan5": find(5, 1)}


@pytest.fixture
def manifest(weekly_identities):
    from ics.migration import MigrationManifest

    return MigrationManifest(
        notified=[weekly_identities["dec8"].key()],
        cancelled=[weekly_identities["jan5"].key()],
        allowed_boundaries=[
            datetime(2026, 12, 3, tzinfo=UTC),
            datetime(2026, 12, 18, tzinfo=UTC),
        ],
        new_slots=[
            NewSlot(
                start=datetime(2027, 1, 20, 14, tzinfo=timezone(timedelta(hours=2))),
                end=datetime(2027, 1, 20, 15, tzinfo=timezone(timedelta(hours=2))),
                summary="Admin maintenance window",
                uid="admin-maint@venue",
            )
        ],
    )
