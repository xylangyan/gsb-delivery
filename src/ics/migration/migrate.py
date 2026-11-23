"""Public entry point for the two-feed calendar migration.

``migrate_calendar`` consumes the old and the supplier's re-issued feed plus
a :class:`MigrationManifest`, and returns a :class:`MigrationResult` whose
``ics_text`` is re-readable by both supported reader classes.  Components of
the old calendar that do not intersect the two-month window are preserved
verbatim (with their TZID references moved onto the legacy VTIMEZONE block so
they keep the source-specified absolute instant).
"""
from datetime import datetime
from typing import List, Optional

from ics.contentline import (
    Container,
    ContentLine,
    string_to_containers,
)
from ics.migration.extractor import extract_feed
from ics.migration.model import (
    MigrationManifest,
    MigrationResult,
)
from ics.migration.planner import (
    DEFAULT_COST,
    CostSchedule,
    LEGACY_TZID_SUFFIX,
    MigrationPlanner,
)
from ics.migration.readers import ReaderIntersection
from ics.timezone import Timezone, UTC


def _as_utc_bound(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _component_start(container: Container, tzmap) -> Optional[datetime]:
    for line in container:
        if isinstance(line, ContentLine) and line.name == "DTSTART":
            params = {name: list(values) for name, values in line.params.items()}
            from ics.valuetype.datetime import DatetimeConverter

            context = {DatetimeConverter.CONTEXT_KEY_AVAILABLE_TZ: dict(tzmap)}
            return DatetimeConverter.parse(line.value, params, context)
    return None


def _rewrite_tzid(container: Container, renamed: dict) -> None:
    for line in container:
        if isinstance(line, Container):
            _rewrite_tzid(line, renamed)
            continue
        tzid = line.params.get("TZID")
        if tzid and str(tzid[0]) in renamed:
            line.params["TZID"] = [renamed[str(tzid[0])]]


def _preserve_outside_window(
    old_ics_text: str,
    result: MigrationResult,
    window_start: datetime,
    window_end: datetime,
    tz_registry: dict,
) -> str:
    renamed = {
        tzid: tzid + LEGACY_TZID_SUFFIX
        for tzid in tz_registry
        if tz_registry[tzid][0].tzid.endswith(LEGACY_TZID_SUFFIX)
    }
    old_containers = list(string_to_containers(old_ics_text))
    tzmap = {}
    for cal_container in old_containers:
        for child in cal_container:
            if child.name == Timezone.NAME:
                tz = Timezone.from_container(child)
                tzmap.setdefault(tz.tzid, tz)

    kept_components: List[Container] = []
    for cal_container in old_containers:
        for child in cal_container:
            if child.name != "VEVENT":
                continue
            start = _component_start(child, tzmap)
            if start is None:
                continue
            if start.tzinfo is not None:
                outside = (
                    start.astimezone(UTC) < window_start
                    or start.astimezone(UTC) > window_end
                )
            else:
                # floating: compare civil time against naive window bounds
                outside = (
                    start < window_start.replace(tzinfo=None)
                    or start > window_end.replace(tzinfo=None)
                )
            if outside:
                _rewrite_tzid(child, renamed)
                kept_components.append(child)

    if not kept_components:
        return result.ics_text

    output_containers = list(string_to_containers(result.ics_text))
    target = output_containers[0]

    referenced_tzids = set()
    for component in kept_components:
        for line in component:
            tzid = line.params.get("TZID") if isinstance(line, ContentLine) else None
            if tzid:
                referenced_tzids.add(str(tzid[0]))
    present_tzids = {
        child[0].value
        for child in target
        if child.name == Timezone.NAME and len(child) > 0
    }
    available_tzs = {
        tz.tzid: tz for pair in tz_registry.values() for tz in pair
    }
    missing = [
        available_tzs[tzid]
        for tzid in referenced_tzids
        if tzid not in present_tzids and tzid in available_tzs
    ]
    if missing:
        insert_at = next(
            (index for index, child in enumerate(target) if child.name == "VEVENT"),
            len(target),
        )
        for offset, tz in enumerate(missing):
            target.data.insert(insert_at + offset, tz.to_container())

    existing_uids = {
        next((l.value for l in c if isinstance(l, ContentLine) and l.name == "UID"), None)
        for c in target if c.name == "VEVENT"
    }
    for component in kept_components:
        uid = next((l.value for l in component if isinstance(l, ContentLine) and l.name == "UID"), None)
        if uid not in existing_uids:
            target.append(component)
    return target.serialize()


def migrate_calendar(
    old_ics_text: str,
    new_ics_text: str,
    manifest: MigrationManifest,
    window_start: datetime,
    window_end: datetime,
    readers: Optional[ReaderIntersection] = None,
    cost: CostSchedule = DEFAULT_COST,
    preserve_history: bool = True,
) -> MigrationResult:
    """Run the full migration and return the publishable result plus audit."""
    window_start = _as_utc_bound(window_start)
    window_end = _as_utc_bound(window_end)

    old_feed = extract_feed("old", old_ics_text, window_start, window_end)
    new_feed = extract_feed("new", new_ics_text, window_start, window_end)

    planner = MigrationPlanner(
        old_feed=old_feed,
        new_feed=new_feed,
        manifest=manifest,
        window_start=window_start,
        window_end=window_end,
        readers=readers,
        cost=cost,
    )
    result = planner.plan()
    if preserve_history:
        result.ics_text = _preserve_outside_window(
            old_ics_text, result, window_start, window_end, planner.tz_registry
        )
    return result
