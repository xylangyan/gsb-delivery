from ics import Calendar

from ics.migration import MigrationManifest, migrate_calendar

from .conftest import NEW_TZ, WINDOW_END, WINDOW_START


def test_floating_events_stay_floating():
    old = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:feed",
            "BEGIN:VEVENT",
            "UID:float@venue",
            "SUMMARY:Floating weekly",
            "DTSTART:20261202T090000",
            "DTEND:20261202T100000",
            "RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=3",
            "DTSTAMP:20261101T000000Z",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    new = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:feed", NEW_TZ, "END:VCALENDAR"]
    )
    result = migrate_calendar(
        old, new, MigrationManifest(), WINDOW_START, WINDOW_END
    )
    assert result.unresolved == []
    events = Calendar(result.ics_text).events
    assert events, "floating occurrences must still be published"
    floating = [event for event in events if event.uid == "float@venue"]
    for event in floating:
        assert event.begin.tzinfo is None, "floating time must not gain a TZID"
        assert event.begin.strftime("%H%M") == "0900"
    # the emitted lines for the floating series carry no TZID parameter
    floating_lines = [
        line for line in result.ics_text.splitlines()
        if line.startswith("DTSTART") and "T090000" in line
    ]
    assert floating_lines and all("TZID" not in line for line in floating_lines)
