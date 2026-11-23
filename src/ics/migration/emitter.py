"""Render planned segments into a re-readable iCalendar document.

The output stays inside plain RFC 5545 building blocks that both reader
profiles understand: one RRULE per VEVENT, EXDATE for removed slots, and
RECURRENCE-ID override components for moved or cancelled single occurrences.
Timezone-aware datetimes carry an explicit :class:`~ics.timezone.Timezone` so
the library emits the matching VTIMEZONE block and the absolute instant is
preserved end to end; timezone-naive (floating) datetimes stay floating.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ics import Calendar, Event
from ics.contentline import ContentLine
from ics.migration.planner import Segment, TargetOccurrence
from ics.timezone import Timezone


def _civil_string(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def _tzid_param(tz: Optional[Timezone]) -> Dict[str, List[str]]:
    return {} if tz is None else {"TZID": [tz.tzid]}


def _to_civil(value: datetime, tz: Optional[Timezone]) -> datetime:
    if tz is None:
        return value.replace(tzinfo=None)
    return value.astimezone(tz).replace(tzinfo=None)


def _recurrence_id_line(segment: Segment, target: TargetOccurrence) -> ContentLine:
    rid = target.occurrence.rid
    if rid is not None:
        rid_civil = _to_civil(rid, segment.tz)
    else:
        rid_civil = _to_civil(target.occurrence.start, segment.tz)
    return ContentLine(
        name="RECURRENCE-ID",
        params=_tzid_param(segment.tz),
        value=_civil_string(rid_civil),
    )


def _master_event(segment: Segment) -> Event:
    first = segment.targets[0]
    event = Event(
        uid=segment.uid,
        summary=first.occurrence.summary,
        begin=first.target_start,
        end=first.target_end,
    )
    if segment.recurring and segment.freq is not None:
        parts = [f"FREQ={segment.freq}"]
        if segment.interval != 1:
            parts.append(f"INTERVAL={segment.interval}")
        if segment.byday:
            parts.append("BYDAY=" + ",".join(segment.byday))
        if segment.until is not None:
            parts.append("UNTIL=" + _civil_string(segment.until))
        event.extra.append(ContentLine(name="RRULE", value=";".join(parts)))

    # a detached component that originated as a source override keeps its
    # RECURRENCE-ID so its original occurrence identity stays traceable;
    # regular instances promoted to standalone components do not get one
    if not segment.recurring and first.occurrence.source_override:
        event.extra.append(_recurrence_id_line(segment, first))

    for exdate in sorted(set(segment.exdates)):
        event.extra.append(
            ContentLine(
                name="EXDATE",
                params=_tzid_param(segment.tz),
                value=_civil_string(exdate),
            )
        )
    return event


def _override_event(segment: Segment, target: TargetOccurrence) -> Event:
    # express the moved instant in the master segment's zone so the whole
    # component family shares one TZID (absolute instant is unchanged)
    if segment.tz is None:
        begin, end = target.target_start, target.target_end
    else:
        begin = target.target_start.astimezone(segment.tz)
        end = target.target_end.astimezone(segment.tz)
    event = Event(
        uid=segment.uid,
        summary=target.occurrence.summary,
        begin=begin,
        end=end,
    )
    event.extra.append(_recurrence_id_line(segment, target))
    if target.occurrence.sequence is not None:
        event.extra.append(
            ContentLine(name="SEQUENCE", value=str(target.occurrence.sequence))
        )
    return event


def _tombstone_event(segment: Segment, target: TargetOccurrence) -> Event:
    duration = target.occurrence.end - target.occurrence.start
    rid = target.occurrence.rid
    rid_civil = _to_civil(rid if rid is not None else target.occurrence.start, segment.tz)
    begin = rid_civil if segment.tz is None else rid_civil.replace(tzinfo=segment.tz)
    event = Event(
        uid=segment.uid,
        summary=target.occurrence.summary,
        begin=begin,
        end=begin + duration,
        status="CANCELLED",
    )
    if target.occurrence.rid is not None:
        # a recurring occurrence needs its identity; a standalone CANCELLED
        # VEVENT is identified by its UID alone
        event.extra.append(
            ContentLine(
                name="RECURRENCE-ID",
                params=_tzid_param(segment.tz),
                value=_civil_string(rid_civil),
            )
        )
    return event


def build_calendar(
    segments: List[Segment],
    tz_registry: Dict[str, Tuple[Timezone, Timezone]],
) -> Tuple[str, List[Timezone]]:
    events: List[Event] = []
    used: Dict[str, Timezone] = {}

    for segment in segments:
        if segment.tz is not None:
            used[segment.tz.tzid] = segment.tz
        events.append(_master_event(segment))
        for target in segment.overrides:
            if segment.tz is not None:
                used[segment.tz.tzid] = segment.tz
            events.append(_override_event(segment, target))
        for target in segment.tombstones:
            if segment.tz is not None:
                used[segment.tz.tzid] = segment.tz
            events.append(_tombstone_event(segment, target))

    calendar = Calendar(events=events)
    return calendar.serialize(), list(used.values())
