"""Minimum-cost segmentation and emission planning.

The planner turns identity-keyed, absolute occurrences into concrete VEVENT
components.  A series may be split at the boundaries the manifest allows;
a dynamic program scores every feasible segmentation against the per-record
cost schedule so the cheapest publishable plan wins.  Anything that cannot be
expressed inside both reader profiles is returned as an
:class:`Unrepresentable` entry together with the blocking reader and the exact
limitation, instead of being emitted in a form one reader would misinterpret.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import dateutil.rrule

from ics.migration.extractor import ExtractedFeed
from ics.migration.model import (
    AuditEntry,
    MigrationManifest,
    MigrationResult,
    NewSlot,
    Occurrence,
    Unrepresentable,
)
from ics.migration.readers import (
    FORM_EXDATE,
    FORM_RECURRENCE_ID,
    FORM_RECURRENCE_ID_TIME_SHIFT,
    ReaderIntersection,
)
from ics.timezone import Timezone, UTC

LEGACY_TZID_SUFFIX = "@legacy"
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass(frozen=True)
class CostSchedule:
    """Per-record fees used to rank publishable plans."""

    per_component: int = 5
    per_exdate: int = 1
    per_override: int = 3
    per_tombstone: int = 2
    per_vtimezone: int = 4
    per_new_slot: int = 1


DEFAULT_COST = CostSchedule()


@dataclass
class TargetOccurrence:
    occurrence: Occurrence
    locked: bool
    target_tz: Optional[Timezone]
    cancelled: bool = False
    moved_to: Optional[datetime] = None

    @property
    def uid(self) -> str:
        return self.occurrence.uid

    @property
    def rid(self) -> Optional[datetime]:
        return self.occurrence.rid

    @property
    def target_start(self) -> datetime:
        start = self.moved_to if self.moved_to is not None else self.occurrence.start
        return self._into_target_tz(start)

    @property
    def target_end(self) -> datetime:
        if self.moved_to is not None:
            end = self.moved_to + (
                self.occurrence.end - self.occurrence.start
            )
        else:
            end = self.occurrence.end
        return self._into_target_tz(end)

    def _into_target_tz(self, value: datetime) -> datetime:
        if self.target_tz is None:
            # preserve floating time verbatim
            return value.replace(tzinfo=None)
        if value.tzinfo is None:
            return value.replace(tzinfo=self.target_tz)
        return value.astimezone(self.target_tz)

    @property
    def civil_target_start(self) -> datetime:
        if self.target_tz is None:
            return self.target_start
        return self.target_start.astimezone(self.target_tz).replace(tzinfo=None)


@dataclass
class Segment:
    uid: str
    targets: List[TargetOccurrence]
    tz: Optional[Timezone]
    freq: Optional[str]
    interval: int
    byday: Tuple[str, ...]
    recurring: bool = False
    until: Optional[datetime] = None
    exdates: List[datetime] = field(default_factory=list)
    overrides: List[TargetOccurrence] = field(default_factory=list)
    tombstones: List[TargetOccurrence] = field(default_factory=list)


def _identity_set(identities):
    return {
        (uid, rid.astimezone(UTC) if rid is not None else None)
        for uid, rid in identities
    }


def _gcd(values: Sequence[int]) -> int:
    from math import gcd
    from functools import reduce

    return reduce(gcd, values)


class MigrationPlanner:
    def __init__(
        self,
        old_feed: ExtractedFeed,
        new_feed: ExtractedFeed,
        manifest: MigrationManifest,
        window_start: datetime,
        window_end: datetime,
        readers: Optional[ReaderIntersection] = None,
        cost: CostSchedule = DEFAULT_COST,
        legacy_suffix: str = LEGACY_TZID_SUFFIX,
    ):
        self.old_feed = old_feed
        self.new_feed = new_feed
        self.manifest = manifest
        self.window_start = window_start
        self.window_end = window_end
        self.readers = readers or ReaderIntersection()
        self.cost = cost
        self.legacy_suffix = legacy_suffix
        self.unresolved: List[Unrepresentable] = []
        self._tz_registry = self._build_tz_registry()

    @property
    def tz_registry(self) -> Dict[str, Tuple[Timezone, Timezone]]:
        return self._tz_registry

    # ------------------------------------------------------------------ timezones
    def _build_tz_registry(self) -> Dict[str, Tuple[Timezone, Timezone]]:
        """Map each shared TZID to distinct (legacy, new) Timezone objects."""
        registry: Dict[str, Tuple[Timezone, Timezone]] = {}
        for tzid, legacy in self.old_feed.timezones.items():
            renamed = self._rename(legacy, tzid + self.legacy_suffix)
            new = self.new_feed.timezones.get(tzid, renamed)
            registry[tzid] = (renamed, new)
        for tzid, new in self.new_feed.timezones.items():
            registry.setdefault(tzid, (new, new))
        return registry

    @staticmethod
    def _rename(tz: Timezone, tzid: str) -> Timezone:
        if tz.tzid == tzid:
            return tz
        return Timezone(tzid, list(tz.observances), tz.tzurl, tz.last_modified)

    def _target_timezone(self, occ: Occurrence, locked: bool) -> Optional[Timezone]:
        # floating (timezone-naive) occurrences stay floating: there is no
        # TZID to reinterpret and assigning one would invent an absolute instant
        if occ.source_tz is None:
            return None
        source_tzid = occ.source_tz.tzid
        if source_tzid and source_tzid in self._tz_registry:
            legacy, new = self._tz_registry[source_tzid]
            return legacy if locked else new
        return occ.source_tz

    # ------------------------------------------------------------- target build
    def build_targets(self) -> Dict[str, List[TargetOccurrence]]:
        notified = _identity_set(self.manifest.notified)
        cancelled = _identity_set(self.manifest.cancelled)
        moved = {
            (uid, rid.astimezone(UTC) if rid is not None else None): new_start
            for uid, rid, new_start in self.manifest.moves
        }

        grouped: Dict[str, List[TargetOccurrence]] = {}
        for occ in self.old_feed.occurrences:
            key = occ.key()
            locked = key in notified or occ.uid in self.manifest.frozen_uids
            target_tz = self._target_timezone(occ, locked)
            grouped.setdefault(occ.uid, []).append(
                TargetOccurrence(
                    occurrence=occ,
                    locked=locked,
                    target_tz=target_tz,
                    cancelled=key in cancelled or occ.cancelled,
                    moved_to=moved.get(key),
                )
            )

        for slot in self.manifest.new_slots:
            tz = self._slot_timezone(slot)
            uid = slot.uid or f"admin-{abs(hash((slot.start, slot.end))) % 10**8}@site"
            if tz is None:
                civil_start = slot.start.replace(tzinfo=None)
                civil_end = slot.end.replace(tzinfo=None)
            else:
                civil_start = slot.start.astimezone(tz).replace(tzinfo=None)
                civil_end = slot.end.astimezone(tz).replace(tzinfo=None)
            pseudo = Occurrence(
                uid=uid,
                rid=None,
                start=slot.start,
                end=slot.end,
                civil_start=civil_start,
                civil_end=civil_end,
                source_tz=tz,
                summary=slot.summary,
                origin="manifest.new_slot",
            )
            grouped.setdefault(uid, []).append(
                TargetOccurrence(pseudo, locked=False, target_tz=tz)
            )

        def sort_key(target: TargetOccurrence):
            start = target.target_start
            if start.tzinfo is None:
                # floating instants have no absolute position; order them last
                return (1, start)
            return (0, start.astimezone(UTC))

        for targets in grouped.values():
            targets.sort(key=sort_key)
        return grouped

    def _slot_timezone(self, slot: NewSlot) -> Optional[Timezone]:
        if slot.start.tzinfo is None:
            return None
        tzid = getattr(slot.start.tzinfo, "tzid", None)
        if tzid and tzid in self._tz_registry:
            return self._tz_registry[tzid][1]
        if isinstance(slot.start.tzinfo, Timezone):
            return slot.start.tzinfo
        if self.new_feed.timezones:
            return next(iter(self.new_feed.timezones.values()))
        return None

    # ------------------------------------------------------------- segmentation
    @staticmethod
    def _is_regular(target: TargetOccurrence) -> bool:
        occ = target.occurrence
        return (
            not target.cancelled
            and target.moved_to is None
            and not occ.source_override
        )

    def _original_slot(self, target: TargetOccurrence) -> datetime:
        """Absolute instant of the identity slot the occurrence descends from."""
        if target.occurrence.rid is not None:
            return target.occurrence.rid
        return target.occurrence.start

    def _source_civil_slot(self, target: TargetOccurrence) -> datetime:
        slot = self._original_slot(target)
        return slot.replace(tzinfo=None) if slot.tzinfo is not None else slot

    def _allowed_cut(self, left: TargetOccurrence, right: TargetOccurrence) -> bool:
        gap_start = left.target_end
        gap_end = right.target_start
        if gap_start.tzinfo is None or gap_end.tzinfo is None:
            return False  # floating series are never split on zone boundaries
        gap_start = gap_start.astimezone(UTC)
        gap_end = gap_end.astimezone(UTC)
        return any(
            gap_start <= boundary.astimezone(UTC) <= gap_end
            for boundary in self.manifest.allowed_boundaries
        )

    def _source_cadence(
        self, uid: str, regular: List[TargetOccurrence]
    ) -> Optional[Tuple[str, int, Tuple[str, ...]]]:
        spec = self.old_feed.rule_specs.get(uid)
        if spec is None:
            return None
        byday = spec.byday if spec.freq == "WEEKLY" else ()
        return spec.freq, spec.interval, byday

    def _detect_cadence(
        self, regular: Sequence[TargetOccurrence]
    ) -> Optional[Tuple[str, int, Tuple[str, ...]]]:
        if len(regular) < 2:
            return None
        # use source civil time: it is unaffected by the target-zone rewrite
        # so a 09:00 weekly series stays weekly even across a zone split.
        deltas = [
            (regular[i + 1].occurrence.civil_start - regular[i].occurrence.civil_start).days
            for i in range(len(regular) - 1)
        ]
        if any(d <= 0 for d in deltas):
            return None
        if all(27 <= d <= 31 for d in deltas):
            freq, interval = "MONTHLY", 1
        else:
            step = _gcd(deltas)
            if step == 1:
                freq, interval = "DAILY", 1
            elif step % 7 == 0:
                freq, interval = "WEEKLY", step // 7
            else:
                return None

        byday: Tuple[str, ...] = ()
        if freq == "WEEKLY":
            weekdays = sorted({t.occurrence.civil_start.weekday() for t in regular})
            byday = tuple(_WEEKDAYS[d] for d in weekdays)
        return freq, interval, byday

    def plan_series(
        self, uid: str, targets: List[TargetOccurrence]
    ) -> List[Segment]:
        # floating (naive) series are emitted one component per occurrence so
        # their wall-clock time is preserved without inventing a timezone
        if all(target.target_tz is None for target in targets):
            return self._singleton_segments(targets)

        regular = [t for t in targets if self._is_regular(t)]
        irregular = [t for t in targets if not self._is_regular(t)]

        if len(regular) < 2:
            return self._singleton_segments(targets)

        cadence = self._source_cadence(uid, regular)
        if cadence is None:
            cadence = self._detect_cadence(regular)
        if cadence is None:
            return self._singleton_segments(targets)
        freq, interval, byday = cadence

        limitation = self.readers.rule_supported(
            freq=freq, interval=interval, byday=byday, count=len(regular)
        )
        if limitation is not None:
            self._flag_rule(uid, regular, limitation)
            return self._singleton_segments(targets)

        source_grid = self._source_grid(regular, freq, interval, byday)
        if source_grid is None:
            return self._singleton_segments(targets)

        ordered = sorted(
            regular, key=lambda t: source_grid.index(self._source_civil_slot(t))
        )
        ranges = self._cheapest_ranges(
            uid, ordered, freq, interval, byday, irregular
        )

        segments: List[Segment] = []
        attached: set = set()
        for start, end in ranges:
            group = ordered[start:end]
            segment = self._build_segment(
                uid, group, freq, interval, byday, irregular, attached
            )
            self._check_exception_forms(segment)
            segments.append(segment)

        for target in irregular:
            if id(target) not in attached:
                segments.extend(self._singleton_segments([target]))
        return segments

    def _cheapest_ranges(
        self,
        uid: str,
        ordered: List[TargetOccurrence],
        freq: str,
        interval: int,
        byday: Tuple[str, ...],
        irregular: List[TargetOccurrence],
    ) -> List[Tuple[int, int]]:
        """DP over every legal cut, minimizing the per-record fee total.

        Mandatory cuts are target-zone changes (two zones may never share one
        RRULE definition).  Optional cuts are the manifest's allowed publish
        boundaries inside an otherwise homogeneous zone run.
        """
        n = len(ordered)
        mandatory = {0, n}
        optional = set()
        for i in range(1, n):
            if ordered[i].target_tz is not ordered[i - 1].target_tz:
                if not self._allowed_cut(ordered[i - 1], ordered[i]):
                    self._flag_boundary(uid, ordered[i - 1], ordered[i])
                mandatory.add(i)
            elif self._allowed_cut(ordered[i - 1], ordered[i]):
                optional.add(i)

        nodes = sorted(mandatory | optional)
        inf = float("inf")
        dp: Dict[int, float] = {0: 0.0}
        predecessor: Dict[int, int] = {}
        for s in nodes:
            if s not in dp:
                continue
            for e in nodes:
                if e <= s:
                    continue
                # a range may not straddle an unchosen mandatory cut
                if any(s < m < e for m in mandatory):
                    continue
                candidate = dp[s] + self._range_cost(
                    ordered[s:e], freq, interval, byday, irregular
                )
                if candidate < dp.get(e, inf):
                    dp[e] = candidate
                    predecessor[e] = s

        path: List[int] = []
        current = n
        while current != 0:
            path.append(current)
            current = predecessor[current]
        path.append(0)
        path.reverse()
        return list(zip(path[:-1], path[1:]))

    def _range_cost(
        self,
        group: List[TargetOccurrence],
        freq: str,
        interval: int,
        byday: Tuple[str, ...],
        irregular: List[TargetOccurrence],
    ) -> float:
        if len(group) == 1 and not self._group_has_exception(
            group, irregular, group[0].target_tz
        ):
            return self.cost.per_component

        tz = group[0].target_tz
        first = group[0].civil_target_start
        last = group[-1].civil_target_start
        kwargs = {
            "freq": getattr(dateutil.rrule, freq),
            "interval": interval,
            "dtstart": first,
            "until": last + timedelta(days=1),
        }
        if freq == "WEEKLY" and byday:
            kwargs["byweekday"] = tuple(
                dateutil.rrule.weekday(_WEEKDAYS.index(name)) for name in byday
            )
        grid = [dt for dt in dateutil.rrule.rrule(**kwargs) if first <= dt <= last]
        regular_civil = {target.civil_target_start for target in group}
        irregular_by_civil = {
            self._original_slot(target).astimezone(tz).replace(tzinfo=None): target
            for target in irregular
        }
        exdates = overrides = tombstones = 0
        for slot in grid:
            if slot in regular_civil:
                continue
            target = irregular_by_civil.get(slot)
            if target is None:
                exdates += 1
            elif target.cancelled:
                tombstones += 1
            else:
                overrides += 1
        return (
            self.cost.per_component
            + exdates * self.cost.per_exdate
            + overrides * self.cost.per_override
            + tombstones * self.cost.per_tombstone
        )

    def _source_grid(
        self,
        regular: List[TargetOccurrence],
        freq: str,
        interval: int,
        byday: Tuple[str, ...],
    ) -> Optional[List[datetime]]:
        """Ordering grid built in the source frame (slot identity only)."""
        first = self._source_civil_slot(regular[0])
        last = self._source_civil_slot(regular[-1])
        kwargs = {
            "freq": getattr(dateutil.rrule, freq),
            "interval": interval,
            "dtstart": first,
            "until": last + timedelta(days=1),
        }
        if freq == "WEEKLY" and byday:
            kwargs["byweekday"] = tuple(
                dateutil.rrule.weekday(_WEEKDAYS.index(name)) for name in byday
            )
        rule = dateutil.rrule.rrule(**kwargs)
        return [dt for dt in rule if first <= dt <= last]

    def _build_segment(
        self,
        uid: str,
        group: List[TargetOccurrence],
        freq: str,
        interval: int,
        byday: Tuple[str, ...],
        irregular: List[TargetOccurrence],
        attached: set,
    ) -> Segment:
        tz = group[0].target_tz

        # a zone run that contains only one regular slot is published as a
        # plain single VEVENT rather than as a degenerate one-instance rule
        if len(group) == 1 and not self._group_has_exception(group, irregular, tz):
            return Segment(
                uid=uid,
                targets=group,
                tz=tz,
                freq=None,
                interval=1,
                byday=(),
                recurring=False,
            )

        segment = Segment(
            uid=uid,
            targets=group,
            tz=tz,
            freq=freq,
            interval=interval,
            byday=byday,
            recurring=True,
        )

        # target-frame grid: every value is civil time expressed in segment.tz
        first = group[0].civil_target_start
        last = group[-1].civil_target_start
        kwargs = {
            "freq": getattr(dateutil.rrule, freq),
            "interval": interval,
            "dtstart": first,
            "until": last + timedelta(days=1),
        }
        if freq == "WEEKLY" and byday:
            kwargs["byweekday"] = tuple(
                dateutil.rrule.weekday(_WEEKDAYS.index(name)) for name in byday
            )
        grid = [
            dt for dt in dateutil.rrule.rrule(**kwargs) if first <= dt <= last
        ]
        segment.until = grid[-1]
        regular_civil = {target.civil_target_start for target in group}

        # exceptions keyed by the original slot's civil time in this segment's
        # zone (the RFC 5545 RECURRENCE-ID matching convention)
        irregular_by_civil: Dict[datetime, TargetOccurrence] = {}
        for target in irregular:
            original = self._original_slot(target)
            irregular_by_civil[
                original.astimezone(tz).replace(tzinfo=None)
            ] = target

        for slot in grid:
            if slot in regular_civil:
                continue
            irregular_target = irregular_by_civil.get(slot)
            if irregular_target is not None:
                attached.add(id(irregular_target))
                if irregular_target.cancelled:
                    segment.tombstones.append(irregular_target)
                else:
                    segment.overrides.append(irregular_target)
            else:
                segment.exdates.append(slot)
        segment.exdates = sorted(set(segment.exdates))
        return segment

    def _group_has_exception(
        self,
        group: List[TargetOccurrence],
        irregular: List[TargetOccurrence],
        tz: Timezone,
    ) -> bool:
        span = {
            target.civil_target_start for target in group
        }
        first = min(span)
        last = max(span)
        for target in irregular:
            original_civil = self._original_slot(target).astimezone(tz).replace(
                tzinfo=None
            )
            if first <= original_civil <= last:
                return True
        return False

    def _singleton_segments(
        self, targets: List[TargetOccurrence]
    ) -> List[Segment]:
        segments: List[Segment] = []
        for target in targets:
            segment = Segment(
                uid=target.uid,
                targets=[target],
                tz=target.target_tz,
                freq=None,
                interval=1,
                byday=(),
                tombstones=[target] if target.cancelled else [],
            )
            if target.cancelled:
                self._check_exception_forms(segment)
            segments.append(segment)
        return segments

    def _flag_rule(self, uid: str, chunk: List[TargetOccurrence], limitation: str) -> None:
        reader = limitation.split(" does not")[0].strip()
        for target in chunk:
            self.unresolved.append(
                Unrepresentable(
                    uid, target.rid, target.target_start,
                    blocking_reader=reader, limitation=limitation,
                )
            )

    def _flag_boundary(
        self, uid: str, left: TargetOccurrence, right: TargetOccurrence
    ) -> None:
        self.unresolved.append(
            Unrepresentable(
                uid,
                right.rid,
                right.target_start,
                blocking_reader="segmentation",
                limitation=(
                    "no allowed publish boundary between "
                    f"{left.target_start.astimezone(UTC)} (locked) and "
                    f"{right.target_start.astimezone(UTC)}; the legacy and new "
                    "zone cannot share one RRULE definition"
                ),
            )
        )

    def _check_exception_forms(self, segment: Segment) -> None:
        if segment.exdates:
            limitation = self.readers.form_supported(FORM_EXDATE)
            if limitation:
                reader = limitation.split(" does not")[0].strip()
                for target in segment.targets:
                    self.unresolved.append(
                        Unrepresentable(
                            segment.uid, target.rid, target.target_start,
                            blocking_reader=reader, limitation=limitation,
                        )
                    )
        for target in segment.overrides:
            form = (
                FORM_RECURRENCE_ID_TIME_SHIFT
                if target.moved_to is not None
                else FORM_RECURRENCE_ID
            )
            limitation = self.readers.form_supported(form)
            if limitation:
                reader = limitation.split(" does not")[0].strip()
                self.unresolved.append(
                    Unrepresentable(
                        segment.uid, target.rid, target.target_start,
                        blocking_reader=reader, limitation=limitation,
                    )
                )

    # ------------------------------------------------------------------- driver
    def plan(self) -> MigrationResult:
        from ics.migration.emitter import build_calendar

        grouped = self.build_targets()
        segments: List[Segment] = []
        for uid, targets in grouped.items():
            segments.extend(self.plan_series(uid, targets))

        ics_text, used_timezones = build_calendar(segments, self._tz_registry)
        audit = self._audit(segments)
        cost = self._score(segments, used_timezones)
        return MigrationResult(
            ics_text=ics_text,
            audit=audit,
            unresolved=self.unresolved,
            cost=cost,
            component_count=len(segments),
        )

    def _score(self, segments: List[Segment], used_timezones: List[Timezone]) -> int:
        total = len(used_timezones) * self.cost.per_vtimezone
        for segment in segments:
            total += self.cost.per_component
            total += len(segment.exdates) * self.cost.per_exdate
            total += len(segment.overrides) * self.cost.per_override
            total += len(segment.tombstones) * self.cost.per_tombstone
        total += len(self.manifest.new_slots) * self.cost.per_new_slot
        return total

    def _audit(self, segments: List[Segment]) -> List[AuditEntry]:
        entries: List[AuditEntry] = []
        seen: set = set()

        def add(target: TargetOccurrence) -> None:
            if id(target) in seen:
                return
            seen.add(id(target))
            occ = target.occurrence
            if target.cancelled:
                kind = "deleted"
            elif occ.origin == "manifest.new_slot":
                kind = "added"
            elif target.moved_to is not None or self._absolute_moved(target):
                kind = "moved"
            else:
                kind = "kept"
            entries.append(
                AuditEntry(
                    uid=occ.uid,
                    rid=occ.rid,
                    kind=kind,
                    old_start=occ.start,
                    new_start=None if target.cancelled else target.target_start,
                )
            )

        for segment in segments:
            for target in segment.targets:
                add(target)
            for target in segment.overrides:
                add(target)
            for target in segment.tombstones:
                add(target)

        entries.sort(
            key=lambda entry: (
                entry.uid,
                entry.rid.astimezone(UTC)
                if entry.rid is not None
                else datetime.min.replace(tzinfo=UTC),
            )
        )
        return entries

    @staticmethod
    def _absolute_moved(target: TargetOccurrence) -> bool:
        occ = target.occurrence
        # a source override moved relative to the rule slot it replaces
        if occ.source_override and occ.rid is not None:
            if occ.start.tzinfo is not None and occ.rid.tzinfo is not None:
                return occ.start.astimezone(UTC) != occ.rid.astimezone(UTC)
            return occ.start != occ.rid
        if occ.start.tzinfo is None or target.target_start.tzinfo is None:
            return occ.start != target.target_start
        return (
            occ.start.astimezone(UTC) != target.target_start.astimezone(UTC)
        )
