from datetime import datetime, timedelta

from ics import Calendar
from ics.contentline import ContentLine
from ics.migration import (
    BASIC_READER,
    ReaderIntersection,
    migrate_calendar,
)
from ics.timezone import UTC

from .conftest import (
    WINDOW_END,
    WINDOW_START,
    build_biweekly_calendar,
    build_new_calendar,
    build_old_calendar,
)


def _run(feeds, manifest):
    old_text, new_text = feeds
    return migrate_calendar(
        old_text, new_text, manifest, WINDOW_START, WINDOW_END
    )


def _output_events(result):
    calendar = Calendar(result.ics_text)
    return calendar.events


def _lines(event, name):
    return [line for line in event.extra if line.name == name]


def test_output_is_rereadable_by_library(feeds, manifest):
    result = _run(feeds, manifest)
    assert result.unresolved == []
    # the emitted document must parse cleanly with the same library
    calendar = Calendar(result.ics_text)
    assert calendar.events
    # both VTIMEZONE definitions are embedded
    text = result.ics_text
    assert "TZID:Site/Custom\r\n" in text
    assert "TZID:Site/Custom@legacy\r\n" in text


def test_notified_occurrence_keeps_absolute_instant_and_legacy_zone(
    feeds, manifest, weekly_identities
):
    result = _run(feeds, manifest)
    dec8 = weekly_identities["dec8"]

    locked = [
        event
        for event in _output_events(result)
        if event.uid == "weekly@venue"
        and event.begin is not None
        and event.begin.astimezone(UTC) == dec8.start.astimezone(UTC)
    ]
    assert locked, "locked Dec 8 occurrence must still be published"
    event = locked[0]
    assert event.begin.tzinfo.tzid == "Site/Custom@legacy"
    assert event.begin.utcoffset() == timedelta(hours=1)
    assert event.begin.strftime("%H%M") == "0900"


def test_new_zone_applies_only_to_unnotified_part(feeds, manifest):
    result = _run(feeds, manifest)
    text = result.ics_text
    # Dec 29 onward uses the upgraded (+02:00) zone at civil 10:00, which is
    # the same absolute instant as the old 09:00 (+01:00)
    assert "DTSTART;TZID=Site/Custom:20261229T100000" in text
    assert "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20270126T100000" in text


def test_absolute_instants_are_preserved(feeds, manifest, weekly_identities):
    result = _run(feeds, manifest)
    by_identity = {
        (entry.uid, entry.rid.astimezone(UTC) if entry.rid else None): entry
        for entry in result.audit
    }
    for occ_id, entry in by_identity.items():
        if entry.kind == "deleted" or entry.new_start is None:
            continue
        original = next(
            o
            for o in [weekly_identities["dec1"], weekly_identities["dec8"]]
            if (o.uid, o.rid) == occ_id
        ) if occ_id in {
            (weekly_identities["dec1"].uid, weekly_identities["dec1"].rid),
            (weekly_identities["dec8"].uid, weekly_identities["dec8"].rid),
        } else None
        if original is not None:
            assert entry.new_start.astimezone(UTC) == original.start.astimezone(UTC)


def test_coincident_moves_keep_two_distinct_bookings(feeds, manifest):
    result = _run(feeds, manifest)
    moved = [
        event
        for event in _output_events(result)
        if event.begin is not None
        and event.begin.astimezone(UTC) == datetime(2026, 12, 22, 10, tzinfo=UTC)
    ]
    uids = {event.uid for event in moved}
    assert uids == {"weekly@venue", "clinic@venue"}
    # both sessions still occupy a slot, so there must be two components
    assert len(moved) == 2
    for event in moved:
        assert _lines(event, "RECURRENCE-ID")


def test_cancelled_occurrence_is_a_tombstone(feeds, manifest):
    result = _run(feeds, manifest)
    tombstones = [
        event
        for event in _output_events(result)
        if event.status == "CANCELLED"
    ]
    assert len(tombstones) == 1
    tombstone = tombstones[0]
    assert tombstone.uid == "weekly@venue"
    rid = _lines(tombstone, "RECURRENCE-ID")
    assert rid and rid[0].value == "20270105T100000"
    assert any(entry.kind == "deleted" for entry in result.audit)


def test_source_exdate_slot_is_not_published(feeds, manifest):
    result = _run(feeds, manifest)
    text = result.ics_text
    # Dec 15 was excluded on the master rule; no component claims that instant
    assert "20261215T090000" not in text
    assert "20261215T100000" not in text


def test_history_outside_window_is_preserved_verbatim(feeds, manifest):
    result = _run(feeds, manifest)
    history = [
        event
        for event in _output_events(result)
        if event.uid == "history@venue"
    ]
    assert len(history) == 1
    event = history[0]
    assert event.begin.tzinfo.tzid == "Site/Custom@legacy"
    assert event.begin.astimezone(UTC) == datetime(2026, 11, 3, 8, tzinfo=UTC)


def test_audit_classifies_moves_deletes_and_additions(feeds, manifest):
    result = _run(feeds, manifest)
    kinds = {(entry.uid, entry.kind) for entry in result.audit}
    assert ("weekly@venue", "moved") in kinds
    assert ("weekly@venue", "deleted") in kinds
    assert ("clinic@venue", "moved") in kinds
    assert ("admin-maint@venue", "added") in kinds
    # unchanged regular sessions are still reported as kept
    assert ("weekly@venue", "kept") in kinds


def test_new_admin_slot_is_published(feeds, manifest):
    result = _run(feeds, manifest)
    admin = [
        event
        for event in _output_events(result)
        if event.uid == "admin-maint@venue"
    ]
    assert len(admin) == 1
    assert admin[0].begin == datetime(2027, 1, 20, 14, tzinfo=admin[0].begin.tzinfo)


def test_unrepresentable_when_zone_cut_lacks_allowed_boundary(
    feeds, weekly_identities
):
    from ics.migration import MigrationManifest

    manifest = MigrationManifest(
        notified=[weekly_identities["dec8"].key()],
        allowed_boundaries=[],  # no legal cut around the locked occurrence
    )
    result = _run(feeds, manifest)
    assert result.unresolved
    assert all(item.blocking_reader == "segmentation" for item in result.unresolved)
    message = " ".join(item.limitation for item in result.unresolved)
    assert "no allowed publish boundary" in message


def test_basic_reader_rejects_intervals_above_one(feeds):
    from ics.migration import MigrationManifest

    old_text = build_biweekly_calendar()
    new_text = build_new_calendar()
    result = migrate_calendar(
        old_text,
        new_text,
        MigrationManifest(),
        WINDOW_START,
        WINDOW_END,
        readers=ReaderIntersection(readers=(BASIC_READER,)),
    )
    assert result.unresolved
    assert any("INTERVAL" in item.limitation for item in result.unresolved)


def test_homogeneous_series_stays_single_component(feeds, weekly_identities):
    from ics.migration import MigrationManifest

    # optional boundaries inside a homogeneous (all-new-zone) series must not
    # trigger extra components because each extra component costs more
    manifest = MigrationManifest(
        allowed_boundaries=[
            datetime(2026, 12, 10, tzinfo=UTC),
            datetime(2026, 12, 24, tzinfo=UTC),
            datetime(2027, 1, 7, tzinfo=UTC),
        ]
    )
    result = _run(feeds, manifest)
    clinic_masters = [
        event
        for event in _output_events(result)
        if event.uid == "clinic@venue" and _lines(event, "RRULE")
    ]
    assert len(clinic_masters) == 1
    rule = _lines(clinic_masters[0], "RRULE")[0].value
    assert rule.startswith("FREQ=WEEKLY")


def test_cost_increases_with_extra_records(feeds, manifest):
    cheap = _run(feeds, manifest)
    # forcing every slot apart must never be cheaper than the compact plan
    from ics.migration import MigrationManifest

    split_manifest = MigrationManifest(
        notified=manifest.notified,
        cancelled=manifest.cancelled,
        allowed_boundaries=[
            datetime(2026, 12, d, tzinfo=UTC) for d in range(2, 31)
        ]
        + [datetime(2027, 1, d, tzinfo=UTC) for d in range(2, 30)],
        new_slots=manifest.new_slots,
    )
    split = _run(feeds, split_manifest)
    assert split.cost >= cheap.cost
