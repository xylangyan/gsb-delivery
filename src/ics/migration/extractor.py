"""Turn the two source calendars into absolute, identity-keyed occurrences.

The two feeds may spell a TZID identically while defining it differently, so
timezone objects are always taken from the feed that actually carries the
VTIMEZONE block (``Timezone`` instances are attached to the parsed datetimes).
Recurrence expansion works on civil wall-clock time and the source timezone is
re-attached afterwards, which preserves the supplier's intended absolute
instant even after the TZID table is upgraded.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import dateutil.rrule

from ics import Calendar, Event
from ics.contentline import ContentLine, string_to_containers
from ics.migration.model import Occurrence
from ics.timezone import Timezone, UTC, is_utc
from ics.valuetype.datetime import DatetimeConverter

RECURRENCE_NAMES = ("RRULE", "RDATE", "EXRULE", "EXDATE")


@dataclass(frozen=True)
class RuleSpec:
    """The source master's RRULE parts, used as the cadence ground truth."""

    freq: str
    interval: int
    byday: Tuple[str, ...]


@dataclass
class ExtractedFeed:
    name: str
    calendar: Calendar
    timezones: Dict[str, Timezone]
    occurrences: List[Occurrence] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    rule_specs: Dict[str, RuleSpec] = field(default_factory=dict)


def _parse_rule_spec(master: Event) -> Optional[RuleSpec]:
    rrule_line = next((line for line in master.extra if line.name == "RRULE"), None)
    if rrule_line is None:
        return None
    tokens = dict(
        part.split("=", 1)
        for part in rrule_line.value.split(";")
        if "=" in part
    )
    freq = tokens.get("FREQ")
    if freq is None:
        return None
    interval = int(tokens.get("INTERVAL", "1"))
    byday = tuple(tokens["BYDAY"].split(",")) if "BYDAY" in tokens else ()
    return RuleSpec(freq=freq, interval=interval, byday=byday)


def _feed_timezones(ics_text: str) -> Dict[str, Timezone]:
    tzmap: Dict[str, Timezone] = {}
    for container in string_to_containers(ics_text):
        for child in container:
            if child.name == Timezone.NAME:
                tz = Timezone.from_container(child)
                # keep the first definition: each feed owns its own rules
                tzmap.setdefault(tz.tzid, tz)
    return tzmap


def _parse_datetime_line(line: ContentLine, tzmap: Dict[str, Timezone]) -> datetime:
    params = {name: list(values) for name, values in line.params.items()}
    context = {DatetimeConverter.CONTEXT_KEY_AVAILABLE_TZ: dict(tzmap)}
    return DatetimeConverter.parse(line.value, params, context)


def _civil(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _as_aware(value: datetime, tz: Optional[Timezone]) -> datetime:
    if value.tzinfo is not None or tz is None:
        return value
    return value.replace(tzinfo=tz)


def _build_rruleset(
    master: Event, tzmap: Dict[str, Timezone]
) -> Optional[dateutil.rrule.rruleset]:
    recur_lines = [line for line in master.extra if line.name in RECURRENCE_NAMES]
    if not any(line.name in ("RRULE", "EXRULE") for line in recur_lines):
        return None

    begin = master.begin
    tz = begin.tzinfo
    if is_utc(begin):
        dtstart = ContentLine(name="DTSTART", value=begin.strftime("%Y%m%dT%H%M%SZ"))
    elif tz is None:
        dtstart = ContentLine(name="DTSTART", value=begin.strftime("%Y%m%dT%H%M%S"))
    else:
        dtstart = ContentLine(
            name="DTSTART",
            params={"TZID": [tz.tzid]},
            value=begin.strftime("%Y%m%dT%H%M%S"),
        )

    normalized = [dtstart]
    for line in recur_lines:
        line = line.clone()
        # dateutil rejects a UTC UNTIL next to a TZID-aware DTSTART and only
        # accepts the rule's local time; convert it into source civil time.
        if line.name in ("RRULE", "EXRULE") and "UNTIL=" in line.value and tz is not None:
            line.value = _normalize_until(line.value, tz)
        normalized.append(line)

    lines_str = "".join(line.serialize(newline=True) for line in normalized)
    # ignoretz keeps every generated instance in naive source civil time; the
    # source timezone is re-attached by the caller, so absolute instants and
    # EXDATE comparisons stay consistent with the feed's own VTIMEZONE.
    return dateutil.rrule.rrulestr(
        lines_str, tzinfos=dict(tzmap), compatible=True, ignoretz=True
    )


def _normalize_until(rule_value: str, tz: Timezone) -> str:
    parts = []
    for token in rule_value.split(";"):
        if token.startswith("UNTIL="):
            raw = token[len("UNTIL="):]
            if raw.endswith(("Z", "z")):
                naive = datetime.strptime(raw[:-1], "%Y%m%dT%H%M%S")
                from ics.timezone import UTC

                aware = naive.replace(tzinfo=UTC).astimezone(tz)
                token = "UNTIL=" + aware.strftime("%Y%m%dT%H%M%S")
        parts.append(token)
    return ";".join(parts)


def _expand_recurring(
    master: Event,
    rrset: dateutil.rrule.rruleset,
    window_start: datetime,
    window_end: datetime,
) -> List[Tuple[datetime, datetime, datetime]]:
    """Return (aware start, aware end, recurrence-id) tuples in the window."""
    tz = master.begin.tzinfo
    duration = master.end - master.begin if master.end is not None else timedelta(0)

    if tz is not None:
        naive_start = window_start.astimezone(tz).replace(tzinfo=None)
        naive_end = window_end.astimezone(tz).replace(tzinfo=None)
    else:
        # floating rules expand in naive civil time, so the window must be
        # naive too (the absolute instant of a floating event is undefined)
        naive_start = window_start.replace(tzinfo=None)
        naive_end = window_end.replace(tzinfo=None)

    expanded: List[Tuple[datetime, datetime, datetime]] = []
    for civil in rrset.between(naive_start, naive_end, inc=True):
        aware = _as_aware(civil, tz)
        expanded.append((aware, aware + duration, aware))
    return expanded


def _has_recurrence_id(event: Event) -> bool:
    return any(line.name == "RECURRENCE-ID" for line in event.extra)


def _override_rid(event: Event, tzmap: Dict[str, Timezone]) -> datetime:
    rid_line = next(line for line in event.extra if line.name == "RECURRENCE-ID")
    return _parse_datetime_line(rid_line, tzmap)


def _sequence(event: Event) -> Optional[int]:
    for line in event.extra:
        if line.name == "SEQUENCE":
            try:
                return int(line.value)
            except ValueError:
                return None
    return None


def _in_window(value: datetime, start: datetime, end: datetime) -> bool:
    if value.tzinfo is not None:
        return start <= value.astimezone(UTC) <= end
    return start.replace(tzinfo=None) <= value <= end.replace(tzinfo=None)


def _occurrence(
    uid: str,
    rid: Optional[datetime],
    event: Event,
    fallback_tz: Optional[Timezone],
    origin: str,
    summary: Optional[str] = None,
    source_override: bool = False,
) -> Occurrence:
    start = event.begin
    end = event.end or event.begin
    tz = start.tzinfo if start.tzinfo is not None else fallback_tz
    return Occurrence(
        uid=uid,
        rid=rid,
        start=start,
        end=end,
        civil_start=_civil(start),
        civil_end=_civil(end),
        source_tz=tz,
        summary=summary if summary is not None else event.summary,
        status=event.status,
        cancelled=event.status == "CANCELLED",
        sequence=_sequence(event),
        source_override=source_override,
        origin=origin,
    )


def extract_feed(
    name: str,
    ics_text: str,
    window_start: datetime,
    window_end: datetime,
) -> ExtractedFeed:
    calendar = Calendar(ics_text)
    tzmap = _feed_timezones(ics_text)
    feed = ExtractedFeed(
        name=name,
        calendar=calendar,
        timezones=tzmap,
        events=list(calendar.events),
    )

    overrides: Dict[str, List[Event]] = {}
    for event in feed.events:
        if _has_recurrence_id(event):
            overrides.setdefault(event.uid, []).append(event)
    consumed: set = set()

    for index, master in enumerate(feed.events):
        if _has_recurrence_id(master):
            continue  # handled together with its master

        tz = master.begin.tzinfo
        rrset = _build_rruleset(master, tzmap)
        origin = f"{name}#event{index}"
        rule_spec = _parse_rule_spec(master)
        if rule_spec is not None:
            feed.rule_specs.setdefault(master.uid, rule_spec)

        if rrset is None:
            if _in_window(master.begin, window_start, window_end):
                feed.occurrences.append(
                    _occurrence(master.uid, None, master, tz, origin)
                )
            continue

        rid_to_override: Dict[datetime, Event] = {}
        for override in overrides.get(master.uid, []):
            rid_aware = _override_rid(override, tzmap)
            rid_to_override[rid_aware.astimezone(UTC)] = override

        for start, end, rid in _expand_recurring(
            master, rrset, window_start, window_end
        ):
            override = rid_to_override.get(rid.astimezone(UTC))
            if override is None:
                feed.occurrences.append(
                    Occurrence(
                        uid=master.uid,
                        rid=rid,
                        start=start,
                        end=end,
                        civil_start=_civil(start),
                        civil_end=_civil(end),
                        source_tz=tz,
                        summary=master.summary,
                        status=master.status,
                        sequence=_sequence(master),
                        origin=origin,
                    )
                )
                continue

            consumed.add(id(override))
            feed.occurrences.append(
                _occurrence(
                    master.uid,
                    rid,
                    override,
                    tz,
                    f"{name}#event{feed.events.index(override)}",
                    summary=override.summary or master.summary,
                    source_override=True,
                )
            )

    # overrides whose master (or that occurrence) sits outside the expanded window
    for override in feed.events:
        if id(override) in consumed or not _has_recurrence_id(override):
            continue
        rid = _override_rid(override, tzmap)
        if not _in_window(override.begin, window_start, window_end):
            continue
        feed.occurrences.append(
            _occurrence(
                override.uid,
                rid,
                override,
                override.begin.tzinfo,
                f"{name}#event{feed.events.index(override)}",
                source_override=True,
            )
        )

    return feed
