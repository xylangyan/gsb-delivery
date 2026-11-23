from datetime import datetime, timedelta, timezone

from ics import Calendar
from ics.contentline import ContentLine
from ics.migration import MigrationManifest, migrate_calendar
from ics.migration.extractor import extract_feed
from ics.timezone import UTC

from .conftest import NEW_TZ, OLD_TZ, WINDOW_END, WINDOW_START


def _calendar(events: str) -> str:
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:feed", OLD_TZ, events, "END:VCALENDAR"]
    )


def test_internal_exdate_is_carried_into_new_zone():
    old = _calendar(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:plain@venue",
                "DTSTART;TZID=Site/Custom:20261201T090000",
                "DTEND;TZID=Site/Custom:20261201T100000",
                "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=4",
                "EXDATE;TZID=Site/Custom:20261208T090000",
                "DTSTAMP:20261101T000000Z",
                "END:VEVENT",
            ]
        )
    )
    new = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:feed", NEW_TZ, "END:VCALENDAR"]
    )
    # no locked occurrences: the whole series moves to the new zone
    result = migrate_calendar(old, new, MigrationManifest(), WINDOW_START, WINDOW_END)
    assert result.unresolved == []
    text = result.ics_text
    # rule now runs at 10:00 (+02:00) and the removed slot follows it
    assert "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261222T100000" in text
    assert "EXDATE;TZID=Site/Custom:20261208T100000" in text
    # nothing is published on the original Dec 8 absolute instant
    feed = extract_feed("out", result.ics_text, WINDOW_START, WINDOW_END)
    dec8 = datetime(2026, 12, 8, 8, tzinfo=UTC)
    assert all(
        occ.start.astimezone(UTC) != dec8 for occ in feed.occurrences
    )


def test_manifest_move_emits_recurrence_override():
    old = _calendar(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:plain@venue",
                "DTSTART;TZID=Site/Custom:20261201T090000",
                "DTEND;TZID=Site/Custom:20261201T100000",
                "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=3",
                "DTSTAMP:20261101T000000Z",
                "END:VEVENT",
            ]
        )
    )
    new = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:feed", NEW_TZ, "END:VCALENDAR"]
    )
    feed = extract_feed("old", old, WINDOW_START, WINDOW_END)
    dec8 = next(o for o in feed.occurrences if o.civil_start.day == 8)
    new_start = datetime(2026, 12, 9, 11, tzinfo=timezone(timedelta(hours=2)))
    manifest = MigrationManifest(moves=[(dec8.uid, dec8.rid, new_start)])

    result = migrate_calendar(old, new, manifest, WINDOW_START, WINDOW_END)
    assert result.unresolved == []

    override = [
        event
        for event in Calendar(result.ics_text).events
        if any(line.name == "RECURRENCE-ID" for line in event.extra)
    ]
    assert len(override) == 1
    rid = next(line for line in override[0].extra if line.name == "RECURRENCE-ID")
    # identity stays on the original slot (Dec 8 10:00 in the new civil frame)
    assert rid.value == "20261208T100000"
    assert override[0].begin.astimezone(UTC) == new_start.astimezone(UTC)
    assert any(
        entry.kind == "moved" and entry.rid == dec8.rid for entry in result.audit
    )


def test_frozen_uid_keeps_legacy_zone_for_every_occurrence():
    old = _calendar(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:frozen@venue",
                "DTSTART;TZID=Site/Custom:20261201T090000",
                "DTEND;TZID=Site/Custom:20261201T100000",
                "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=2",
                "DTSTAMP:20261101T000000Z",
                "END:VEVENT",
            ]
        )
    )
    new = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:feed", NEW_TZ, "END:VCALENDAR"]
    )
    manifest = MigrationManifest(frozen_uids=["frozen@venue"])
    result = migrate_calendar(old, new, manifest, WINDOW_START, WINDOW_END)
    assert result.unresolved == []
    for event in Calendar(result.ics_text).events:
        if event.uid == "frozen@venue":
            assert event.begin.tzinfo.tzid == "Site/Custom@legacy"
            assert event.begin.utcoffset() == timedelta(hours=1)
