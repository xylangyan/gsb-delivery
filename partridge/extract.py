"""Minimum-weight GTFS extraction that preserves transfer answers.

A dispatch office often needs a small package that still answers "may the
passengers of this arriving trip catch that departing trip?".  The publishing
department supplies the permit: which routes, trips, stops, files, and
individual rows may be copied.

:func:`extract_minimal_feed` searches the permitted original rows for the
cheapest selection whose written package, once read back with the ordinary
loader, reproduces the given survey of transfer answers.  Weight is charged
per carried ``stop_times.txt`` row, per carried parent-station row (a
necessary parent is carried once), and per carried ``transfers.txt`` rule row.

Rules are only carried when every reference they name survives.  A trip-level
rule that mentions a trip outside the permitted window is therefore dropped,
so no unauthorized trip can enter the transferable result.  A stop-pair rule
that touches a carried stop keeps both of its endpoints, because trimming a
platform out of it would change the scope an older reader saw.

If the permit does not cover a prohibition that a negative answer depends on,
that question is reported in ``unpreservable`` instead of being silently
weakened.
"""
import itertools
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from .gtfs import Feed
from .readers import load_feed, load_raw_feed
from .transfers import (
    _matches,
    _specificity,
    _types_for,
    can_transfer,
    stop_parents,
    transfer_type,
    value,
)
from .writers import write_feed_dangerously

RuleKey = Tuple[str, str, str, str, int]
StopTimeKey = Tuple[str, str]
Witness = Tuple[str, str]

ANALYSIS_FILES = frozenset(
    {"trips.txt", "stop_times.txt", "stops.txt", "transfers.txt"}
)
SUPPORT_FILES = frozenset(
    {"agency.txt", "routes.txt", "calendar.txt", "calendar_dates.txt"}
)

MAX_COMBINATIONS = 100_000


@dataclass
class Permit:
    """The original rows a publisher is willing to release.

    ``routes``, ``trips``, and ``stops`` bound which trips and stops may be
    copied; each is a hard limit when given.  ``files`` lists the files the
    package may contain.  ``rows`` optionally restricts individual rows:
    ``"stop_times.txt"`` entries are ``(trip_id, stop_id)`` pairs and
    ``"transfers.txt"`` entries are the five-column rule tuple.
    """

    routes: Optional[Iterable[str]] = None
    trips: Optional[Iterable[str]] = None
    stops: Optional[Iterable[str]] = None
    files: Optional[Iterable[str]] = None
    rows: Optional[Dict[str, Iterable[Any]]] = None


@dataclass
class Query:
    """One survey question about a trip pair."""

    from_trip_id: str
    to_trip_id: str
    transferable: bool


@dataclass
class Unpreservable:
    """A survey question the permit can no longer keep answerable."""

    from_trip_id: str
    to_trip_id: str
    transferable: bool
    reason: str


@dataclass
class ScopeLoss:
    """A source rule dropped although it still touches the carried subset.

    Unlike a lost prohibition, this does not flip a "no" to a "yes"; it
    narrows the scope an older reader would have seen.
    """

    rule: RuleKey
    transferable: bool
    reason: str


@dataclass
class ExtractionResult:
    """Outcome of :func:`extract_minimal_feed`."""

    outpath: str
    weight: int
    stop_time_rows: int
    parent_rows: int
    rule_rows: int
    stop_rows: int
    carried_trips: List[str] = field(default_factory=list)
    unpreservable: List[Unpreservable] = field(default_factory=list)
    scope_losses: List[ScopeLoss] = field(default_factory=list)
    verified: bool = True


def _s(item: Any) -> str:
    return value(item)


def _rule_key(row: Any) -> RuleKey:
    number = transfer_type(row.get("transfer_type"))
    return (
        _s(row.get("from_stop_id")),
        _s(row.get("to_stop_id")),
        _s(row.get("from_trip_id")),
        _s(row.get("to_trip_id")),
        0 if number is None else int(number),
    )


def _rule_trip_refs(key: RuleKey) -> Set[str]:
    return {ref for ref in (key[2], key[3]) if ref}


def _rule_stop_refs(key: RuleKey) -> Set[str]:
    return {ref for ref in (key[0], key[1]) if ref}


class _Raw(object):
    """Unpruned source accessors used by the selector."""

    def __init__(self, feed: Feed) -> None:
        self.trips = feed.trips.copy()
        self.stop_times = feed.stop_times.copy()
        self.stops = feed.stops.copy()
        self.transfers = feed.transfers.copy()
        self.parents = stop_parents(self.stops)

        for frame, columns in (
            (self.stop_times, ("trip_id", "stop_id")),
            (self.stops, ("stop_id", "parent_station")),
            (
                self.transfers,
                ("from_stop_id", "to_stop_id", "from_trip_id", "to_trip_id"),
            ),
        ):
            for column in columns:
                if column not in frame.columns:
                    frame[column] = ""

        self.trip_ids: Set[str] = set(map(str, self.trips.trip_id))
        self.stop_ids: Set[str] = set(map(str, self.stops.stop_id))
        self.parent_ids: Set[str] = {
            parent for parent in self.parents.values() if parent in self.stop_ids
        }
        self.stops_by_trip: Dict[str, List[str]] = {}
        for trip_id, group in self.stop_times.groupby("trip_id"):
            self.stops_by_trip[str(trip_id)] = list(map(str, group.stop_id))

    def witness_pairs(self, from_trip: str, to_trip: str) -> List[Witness]:
        """Every (from_stop, to_stop) pair that forms a transfer point."""
        from_stops = self.stops_by_trip.get(from_trip, [])
        to_stops = self.stops_by_trip.get(to_trip, [])
        pairs: List[Witness] = []
        for from_stop in from_stops:
            for to_stop in to_stops:
                if from_stop == to_stop:
                    pairs.append((from_stop, to_stop))
                elif self.parents.get(from_stop) and self.parents.get(
                    from_stop
                ) == self.parents.get(to_stop):
                    pairs.append((from_stop, to_stop))
        return pairs

    def candidate(
        self, from_trip: str, to_trip: str, from_stop: str, to_stop: str
    ) -> Dict[str, str]:
        return {
            "from_trip": from_trip,
            "to_trip": to_trip,
            "from_stop": from_stop,
            "to_stop": to_stop,
            "from_parent": self.parents.get(from_stop, ""),
            "to_parent": self.parents.get(to_stop, ""),
        }

    def verdict(
        self, from_trip: str, to_trip: str, from_stop: str, to_stop: str
    ) -> bool:
        types = _types_for(
            self.transfers,
            self.candidate(from_trip, to_trip, from_stop, to_stop),
        )
        if types:
            return 3 not in types
        return True

    def decisive_prohibitions(
        self, from_trip: str, to_trip: str, from_stop: str, to_stop: str
    ) -> List[RuleKey]:
        """The most specific prohibition rules for one candidate."""
        candidate = self.candidate(from_trip, to_trip, from_stop, to_stop)
        best = -1
        rows: List[RuleKey] = []
        for _, row in self.transfers.iterrows():
            if transfer_type(row.get("transfer_type")) != 3:
                continue
            if not _matches(row, candidate):
                continue
            rank = _specificity(row, candidate)
            if rank > best:
                best = rank
                rows = [_rule_key(row)]
            elif rank == best:
                rows.append(_rule_key(row))
        return rows


def _files(permit: Permit) -> List[str]:
    if permit.files is None:
        return sorted(ANALYSIS_FILES | SUPPORT_FILES)
    return sorted(map(str, permit.files))


def _permit_rows(permit: Permit, filename: str) -> Optional[Set[Any]]:
    if not permit.rows or filename not in permit.rows:
        return None
    return set(permit.rows[filename])


def _row_allowed(permit: Permit, filename: str, key: Any) -> bool:
    rows = _permit_rows(permit, filename)
    return rows is None or key in rows


def _allowed_trips(raw: _Raw, permit: Permit) -> Set[str]:
    allowed = set(raw.trip_ids)
    if permit.trips is not None:
        allowed &= set(map(str, permit.trips))
    if permit.routes is not None:
        routes = set(map(str, permit.routes))
        by_route = set(
            map(str, raw.trips[raw.trips.route_id.isin(routes)].trip_id)
        )
        allowed &= by_route
    return allowed


def _allowed_stops(raw: _Raw, permit: Permit) -> Set[str]:
    if permit.stops is None:
        return set(raw.stop_ids)
    return set(map(str, permit.stops)) & set(raw.stop_ids)


def _witness_options(
    raw: _Raw,
    permit: Permit,
    query: Query,
    allowed_trips: Set[str],
    allowed_stops: Set[str],
    files: Sequence[str],
    unpreservable: List[Unpreservable],
) -> List[Witness]:
    """Acceptable witness pairs for one survey question."""
    from_trip = str(query.from_trip_id)
    to_trip = str(query.to_trip_id)

    required_files = {"trips.txt", "stop_times.txt", "stops.txt", "transfers.txt"}
    if not required_files <= set(files):
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the permit omits files needed to answer transfers: "
                + ", ".join(sorted(required_files - set(files))),
            )
        )
        return []

    if from_trip not in raw.trip_ids or to_trip not in raw.trip_ids:
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the trip pair does not exist in the source feed",
            )
        )
        return []
    if from_trip not in allowed_trips or to_trip not in allowed_trips:
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the trip pair is outside the permitted window",
            )
        )
        return []

    pairs = [
        (from_stop, to_stop)
        for from_stop, to_stop in raw.witness_pairs(from_trip, to_trip)
        if from_stop in allowed_stops
        and to_stop in allowed_stops
        and _row_allowed(permit, "stop_times.txt", (from_trip, from_stop))
        and _row_allowed(permit, "stop_times.txt", (to_trip, to_stop))
    ]
    if not pairs:
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the permitted stops hold no shared stop or parent station",
            )
        )
        return []

    matching = [
        pair
        for pair in pairs
        if raw.verdict(from_trip, to_trip, pair[0], pair[1])
        == query.transferable
    ]
    if not matching:
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the source feed cannot reproduce this answer at a permitted stop",
            )
        )
        return []

    if query.transferable:
        return matching

    viable: List[Witness] = []
    for from_stop, to_stop in matching:
        prohibitions = raw.decisive_prohibitions(
            from_trip, to_trip, from_stop, to_stop
        )
        # One permitted prohibition at the decisive specificity is enough to
        # keep the negative answer.
        if any(_row_allowed(permit, "transfers.txt", key) for key in prohibitions):
            viable.append((from_stop, to_stop))

    if not viable:
        needed = sorted(
            {
                key
                for from_stop, to_stop in matching
                for key in raw.decisive_prohibitions(
                    from_trip, to_trip, from_stop, to_stop
                )
            }
        )
        detail = ""
        if needed:
            detail = "; needed rule(s): " + ", ".join(
                "({}, {}, {}, {}, {})".format(*key) for key in needed
            )
        unpreservable.append(
            Unpreservable(
                from_trip,
                to_trip,
                query.transferable,
                "the permit does not cover the prohibition this answer needs"
                + detail,
            )
        )
        return []

    return viable


class _Plan(object):
    """A candidate selection and the rows it would carry."""

    def __init__(
        self,
        raw: "_Raw",
        visits: Set[StopTimeKey],
        stops: Set[str],
        trips: Set[str],
        rules: Sequence[RuleKey],
        dropped: Sequence[RuleKey] = (),
    ) -> None:
        self.raw = raw
        self.visits = set(visits)
        self.stops = set(stops)
        self.trips = set(trips)
        self.rules = sorted(set(rules))
        self.dropped = sorted(set(dropped))

    def weight(self) -> Tuple[int, int, int, int]:
        return _weight(self.raw, self.visits, self.stops, self.rules)

    def frames(self) -> Dict[str, pd.DataFrame]:
        """The four frames a transfer answer actually depends on."""
        return _analysis_frames(
            self.raw, self.visits, self.stops, self.trips, self.rules
        )

    def answers(self, pairs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], bool]:
        """Evaluate the plan the way a re-read would see it."""
        frames = self.frames()
        return {
            (from_trip, to_trip): can_transfer(
                from_trip,
                to_trip,
                frames["trips.txt"],
                frames["stop_times.txt"],
                frames["stops.txt"],
                frames["transfers.txt"],
            )
            for from_trip, to_trip in pairs
        }


def _plan_for(
    raw: "_Raw",
    permit: Permit,
    entries: Sequence[Tuple[Query, List[Witness]]],
    choice: Sequence[Witness],
    allowed_stops: Set[str],
) -> _Plan:
    """Build the plan for one witness selection.

    A permitted rule is carried whenever every reference it names survives,
    which is what keeps an older reader's applicability unchanged.  In
    particular a stop-pair permission that touches a carried platform drags
    in its other endpoint: trimming the rule down to one platform would
    narrow the scope it used to have.  Only prohibition rows can forbid an
    answer, but rules that merely permit still count toward weight when they
    are carried.
    """
    visits: Set[StopTimeKey] = set()
    stops: Set[str] = set()
    trips: Set[str] = set()
    for (query, _options), (from_stop, to_stop) in zip(entries, choice):
        from_trip = str(query.from_trip_id)
        to_trip = str(query.to_trip_id)
        visits.add((from_trip, from_stop))
        visits.add((to_trip, to_stop))
        stops.update((from_stop, to_stop))
        trips.update((from_trip, to_trip))

    stops, rules = _carried_rules(raw, permit, stops, trips, allowed_stops)
    dropped: Set[RuleKey] = set()
    changed = True
    while changed:
        changed = False
        for key in list(rules):
            extra = _cover_rule_stops(raw, key, visits, trips, permit)
            if extra is None:
                rules.discard(key)
                dropped.add(key)
                changed = True
                break
            if not extra <= visits:
                visits.update(extra)
                changed = True
                break

    # Only stops that will still be reachable survive any other rule's needs.
    witnessed = {stop for _trip, stop in visits}
    stops = {
        stop_id
        for stop_id in stops
        if stop_id in witnessed
        or (
            stop_id in raw.parent_ids
            and any(raw.parents.get(child) == stop_id for child in witnessed)
        )
    }

    return _Plan(raw, visits, stops, trips, sorted(rules), sorted(dropped))


def _carried_rules(
    raw: "_Raw",
    permit: Permit,
    stops: Set[str],
    trips: Set[str],
    allowed_stops: Set[str],
) -> Tuple[Set[str], Set[RuleKey]]:
    """Carried stops and rules, closing over parent and stop-pair scope."""
    stops = set(stops)
    changed = True
    while changed:
        changed = False

        # A necessary parent is carried once, no matter how many platforms
        # point at it.
        for stop_id in list(stops):
            parent = raw.parents.get(stop_id)
            if parent and parent in raw.stop_ids and parent not in stops:
                stops.add(parent)
                changed = True

        # A stop-pair rule that touches a carried platform keeps both of its
        # endpoints.  This is the rule that forbids trimming to one platform.
        for key in _permitted_stop_pair_rules(raw, permit):
            endpoints = _rule_stop_refs(key)
            if not endpoints & stops or not endpoints <= allowed_stops:
                continue
            if not endpoints <= stops:
                stops.update(endpoints)
                changed = True

    rules: Set[RuleKey] = set()
    for key in _permitted_rules(raw, permit):
        if not _rule_stop_refs(key) <= stops:
            continue
        if not _rule_trip_refs(key) <= trips:
            continue
        rules.add(key)
    return stops, rules


def _permitted_rules(raw: "_Raw", permit: Permit) -> List[RuleKey]:
    rules: List[RuleKey] = []
    for _, row in raw.transfers.iterrows():
        key = _rule_key(row)
        if not (key[0] or key[1] or key[2] or key[3]):
            continue
        if not _row_allowed(permit, "transfers.txt", key):
            continue
        rules.append(key)
    return rules


def _permitted_stop_pair_rules(raw: "_Raw", permit: Permit) -> List[RuleKey]:
    """Stop-pair level rules (both endpoints, no trip references)."""
    return [
        key
        for key in _permitted_rules(raw, permit)
        if key[0] and key[1] and not key[2] and not key[3]
    ]


def _cover_rule_stops(
    raw: "_Raw",
    key: RuleKey,
    visits: Set[StopTimeKey],
    trips: Set[str],
    permit: Permit,
) -> Optional[Set[StopTimeKey]]:
    """Visits that give every stop reference of one rule a carried row."""
    covered: Set[StopTimeKey] = set()
    for stop_id in _rule_stop_refs(key):
        if any(stop_id == visited for _trip, visited in visits):
            continue
        owner = _trip_visiting(raw, stop_id, trips, permit)
        if owner is None:
            return None
        covered.add((owner, stop_id))
    return covered


def _trip_visiting(
    raw: "_Raw", stop_id: str, trips: Set[str], permit: Permit
) -> Optional[str]:
    """Pick a carried trip that visits ``stop_id`` and may carry the row."""
    candidates = [
        trip_id
        for trip_id, stops in raw.stops_by_trip.items()
        if trip_id in trips and stop_id in stops
    ]
    candidates = [
        trip_id
        for trip_id in candidates
        if _row_allowed(permit, "stop_times.txt", (trip_id, stop_id))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda trip_id: (len(trip_id), trip_id))


def _weight(
    raw: "_Raw", visits: Set[StopTimeKey], stops: Set[str], rules: Sequence[RuleKey]
) -> Tuple[int, int, int, int]:
    parent_rows = len(
        {
            stop_id
            for stop_id in stops
            if stop_id in raw.parent_ids and stop_id not in {s for _t, s in visits}
        }
    )
    stop_time_rows = len(visits)
    rule_rows = len(rules)
    return (
        stop_time_rows + parent_rows + rule_rows,
        stop_time_rows,
        parent_rows,
        rule_rows,
    )


def _choose(
    raw: "_Raw",
    permit: Permit,
    entries: Sequence[Tuple[Query, List[Witness]]],
    allowed_stops: Set[str],
) -> List[Witness]:
    """Return the witness selection whose written package weighs least."""
    if not entries:
        return []

    total = 1
    for _query, options in entries:
        total *= max(1, len(options))

    if total <= MAX_COMBINATIONS:
        best_choice: Optional[Tuple[Witness, ...]] = None
        best_key: Optional[Tuple[int, int, Tuple[str, ...]]] = None
        for choice in itertools.product(*[options for _q, options in entries]):
            plan = _plan_for(raw, permit, entries, choice, allowed_stops)
            pairs = [
                (str(query.from_trip_id), str(query.to_trip_id))
                for query, _options in entries
            ]
            produced = plan.answers(pairs)
            if produced != {
                (str(query.from_trip_id), str(query.to_trip_id)): query.transferable
                for query, _options in entries
            }:
                continue
            weight = plan.weight()[0]
            # Fewer dropped rules is better; it means less scope was lost.
            key = (weight, len(plan.dropped), tuple(f"{a}>{b}" for a, b in choice))
            if best_key is None or key < best_key:
                best_key = key
                best_choice = choice
        return list(best_choice or ())

    # Above the enumeration cap, choose each witness greedily against the
    # union of the ones already chosen, then confirm the finished plan.
    choice: List[Witness] = []
    for query, options in entries:
        prefix = list(choice)

        def marginal(
            pair: Witness,
            prefix: Sequence[Witness] = prefix,
        ) -> Tuple[int, int, str, str]:
            candidate = list(prefix) + [pair]
            visits: Set[StopTimeKey] = set()
            stops: Set[str] = set()
            for other, (from_stop, to_stop) in zip(entries, candidate):
                other_query = other[0]
                visits.add((str(other_query.from_trip_id), from_stop))
                visits.add((str(other_query.to_trip_id), to_stop))
                stops.update((from_stop, to_stop))
            parent_rows = len(
                {
                    stop_id
                    for stop_id in stops
                    if stop_id in raw.parent_ids
                    and stop_id not in {s for _t, s in visits}
                }
            )
            return (
                len(visits) + parent_rows,
                len(pair[0]) + len(pair[1]),
                pair[0],
                pair[1],
            )

        choice.append(min(options, key=marginal))

    return choice


def _stop_time_rows(raw: _Raw, visits: Set[StopTimeKey]) -> pd.DataFrame:
    """One carriage row per needed (trip, stop) visit."""
    keep: List[bool] = []
    seen: Set[StopTimeKey] = set()
    wanted = set(visits)
    for row in raw.stop_times.itertuples():
        key = (str(row.trip_id), str(row.stop_id))
        keep.append(key in wanted and key not in seen)
        if keep[-1]:
            seen.add(key)
    return raw.stop_times[pd.Series(keep, index=raw.stop_times.index, dtype=bool)].copy()


def _analysis_frames(
    raw: "_Raw",
    visits: Set[StopTimeKey],
    stops: Set[str],
    trips: Set[str],
    rules: Sequence[RuleKey],
) -> Dict[str, pd.DataFrame]:
    """The four frames a transfer answer depends on."""
    keys = set(rules)
    return {
        "stop_times.txt": _stop_time_rows(raw, visits),
        "stops.txt": raw.stops[
            raw.stops.stop_id.map(lambda v: _s(v) in stops)
        ].copy(),
        "transfers.txt": raw.transfers[
            pd.Series(
                [_rule_key(row) in keys for _, row in raw.transfers.iterrows()],
                index=raw.transfers.index,
                dtype=bool,
            )
        ].copy(),
        "trips.txt": raw.trips[
            raw.trips.trip_id.map(lambda v: _s(v) in trips)
        ].copy(),
    }


def _build_frames(
    raw: "_Raw",
    raw_feed: Feed,
    permit: Permit,
    visits: Set[StopTimeKey],
    stops: Set[str],
    trips: Set[str],
    rules: Sequence[RuleKey],
    files: Sequence[str],
) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}

    for name, frame in _analysis_frames(raw, visits, stops, trips, rules).items():
        if name in files:
            frames[name] = frame

    trips_frame = frames.get("trips.txt")
    if trips_frame is None or trips_frame.empty:
        return frames

    if "routes.txt" in files:
        routes = raw_feed.routes
        wanted = set(trips_frame.route_id.dropna()) if "route_id" in trips_frame else set()
        if "route_id" in routes.columns:
            frames["routes.txt"] = routes[routes.route_id.isin(wanted)].copy()
        else:
            frames["routes.txt"] = routes.copy()

        if "agency.txt" in files:
            agency = raw_feed.agency
            routes_frame = frames["routes.txt"]
            if "agency_id" in agency.columns and "agency_id" in routes_frame:
                wanted_agency = set(routes_frame.agency_id.dropna())
                frames["agency.txt"] = agency[agency.agency_id.isin(wanted_agency)].copy()
            else:
                frames["agency.txt"] = agency.copy()

    if "service_id" in trips_frame:
        services = set(trips_frame.service_id.dropna())
        for name, frame in (
            ("calendar.txt", raw_feed.calendar),
            ("calendar_dates.txt", raw_feed.calendar_dates),
        ):
            if name in files and "service_id" in frame.columns:
                frames[name] = frame[frame.service_id.isin(services)].copy()

    return frames


def _write(
    inpath: str, outpath: str, frames: Dict[str, pd.DataFrame], files: Sequence[str]
) -> str:
    """Write selected frames to a zip, accepting either a folder or a zip."""
    temporary = None
    if os.path.isdir(inpath):
        source_dir = inpath
    else:
        temporary = tempfile.mkdtemp()
        shutil.unpack_archive(inpath, temporary)
        source_dir = temporary

    try:
        source = Feed(source_dir)
        for name in files:
            source.set(name, frames.get(name, pd.DataFrame()))
        return write_feed_dangerously(source, outpath, nodes=list(files))
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)


def extract_minimal_feed(
    inpath: str,
    outpath: str,
    permit: Permit,
    survey: Sequence[Query],
    verify: bool = True,
) -> ExtractionResult:
    """Write the cheapest permitted package that keeps the survey answers."""
    if not isinstance(permit, Permit):
        permit = Permit(**permit)
    survey = [item if isinstance(item, Query) else Query(**item) for item in survey]

    raw_feed = load_raw_feed(inpath)
    raw = _Raw(raw_feed)
    files = _files(permit)

    allowed_trips = _allowed_trips(raw, permit)
    allowed_stops = _allowed_stops(raw, permit)

    unpreservable: List[Unpreservable] = []
    entries: List[Tuple[Query, List[Witness]]] = []
    for query in survey:
        options = _witness_options(
            raw, permit, query, allowed_trips, allowed_stops, files, unpreservable
        )
        if options:
            entries.append((query, options))

    choice = _choose(raw, permit, entries, allowed_stops)
    plan = _plan_for(raw, permit, entries, choice, allowed_stops)

    scope_losses = _scope_losses(
        raw, permit, plan.stops, plan.trips, plan.rules, files
    )
    scope_losses.extend(_dropped_losses(plan))

    frames = _build_frames(
        raw, raw_feed, permit, plan.visits, plan.stops, plan.trips, plan.rules, files
    )
    outpath = _write(inpath, outpath, frames, files)

    weight, stop_time_rows, parent_rows, rule_rows = _weight(
        raw, plan.visits, plan.stops, plan.rules
    )
    result = ExtractionResult(
        outpath=outpath,
        weight=weight,
        stop_time_rows=stop_time_rows,
        parent_rows=parent_rows,
        rule_rows=rule_rows,
        stop_rows=len(frames.get("stops.txt", [])),
        carried_trips=sorted(plan.trips),
        unpreservable=unpreservable,
        scope_losses=scope_losses,
    )

    if verify:
        _verify(outpath, entries, result)

    return result


def _dropped_losses(plan: _Plan) -> List[ScopeLoss]:
    """Rules the plan had to drop because no carried row could support them."""
    return [
        ScopeLoss(
            rule=key,
            transferable=int(key[4]) != 3,
            reason="no carried stop-time row can keep this rule readable",
        )
        for key in plan.dropped
    ]


def _scope_losses(
    raw: _Raw,
    permit: Permit,
    stops: Set[str],
    trips: Set[str],
    rules: Sequence[RuleKey],
    files: Sequence[str],
) -> List[ScopeLoss]:
    """Source rules that touch the subset yet cannot be carried as written."""
    if "transfers.txt" not in files:
        return []

    kept = set(rules)
    losses: List[ScopeLoss] = []
    for _, row in raw.transfers.iterrows():
        key = _rule_key(row)
        if key in kept:
            continue
        if not (key[0] or key[1] or key[2] or key[3]):
            continue
        # A rule whose references leave the subset is out of window, not a
        # narrowed scope.
        if not _rule_stop_refs(key) <= stops:
            continue
        if not _rule_trip_refs(key) <= trips:
            continue
        if not _row_allowed(permit, "transfers.txt", key):
            continue
        number = int(key[4])
        losses.append(
            ScopeLoss(
                rule=key,
                transferable=number != 3,
                reason="carrying it would need rows the permit or subset cannot supply",
            )
        )
    return losses


def _verify(
    outpath: str,
    entries: Sequence[Tuple[Query, List[Witness]]],
    result: ExtractionResult,
) -> None:
    feed = load_feed(outpath)
    failures: List[Tuple[str, str, bool, bool]] = []
    for query, _options in entries:
        actual = can_transfer(
            query.from_trip_id,
            query.to_trip_id,
            feed.trips,
            feed.stop_times,
            feed.stops,
            feed.transfers,
        )
        if actual != query.transferable:
            failures.append(
                (
                    str(query.from_trip_id),
                    str(query.to_trip_id),
                    query.transferable,
                    actual,
                )
            )

    if failures:
        result.verified = False
        for from_trip, to_trip, expected, actual in failures:
            result.unpreservable.append(
                Unpreservable(
                    from_trip,
                    to_trip,
                    expected,
                    "the re-read package answers {} instead of {}".format(
                        actual, expected
                    ),
                )
            )
