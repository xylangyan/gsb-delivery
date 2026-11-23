"""Transfer-aware helpers for GTFS feeds.

``transfers.txt`` can describe rules at several levels: per stop pair, per
origin trip, per destination trip, or per trip pair.  A naive dependency-graph
prune drops every rule whose stop references leave the subset.  That silently
changes the answer to "can I transfer from this arriving trip to that
departing trip?".  The helpers here evaluate that question and report which
rules a trimmed feed can no longer answer.
"""
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import pandas as pd

STOP_FIELDS = ("from_stop_id", "to_stop_id")
TRIP_FIELDS = ("from_trip_id", "to_trip_id")
REFERENCE_FIELDS = STOP_FIELDS + TRIP_FIELDS


class DroppedRule(NamedTuple):
    """A transfer rule the extracted feed cannot carry."""

    kind: str
    from_trip_id: str
    to_trip_id: str
    from_stop_id: str
    to_stop_id: str
    transfer_type: int
    reason: str


def value(item: Any) -> str:
    """Return a stripped string, treating pandas nulls as empty."""
    if item is None or pd.isna(item):
        return ""
    return str(item).strip()


def transfer_type(item: Any) -> Optional[int]:
    text = value(item)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def stop_parents(stops: pd.DataFrame) -> Dict[str, str]:
    """Map each stop id to its parent station id."""
    parents: Dict[str, str] = {}
    if {"stop_id", "parent_station"}.issubset(stops.columns):
        for stop_id, parent in zip(stops.stop_id, stops.parent_station):
            stop_id = value(stop_id)
            parent = value(parent)
            if stop_id and parent and stop_id != parent:
                parents[stop_id] = parent
    return parents


def parent_closure(
    stops: pd.DataFrame, stop_ids: Iterable[str]
) -> Tuple[Set[str], Set[str]]:
    """Return ``stop_ids`` plus every ancestor, and the ancestors that were added.

    A necessary parent is returned at most once no matter how many of its
    platforms reference it.
    """
    present = set(map(str, stops.stop_id)) if "stop_id" in stops.columns else set()
    parents = stop_parents(stops)

    selected = set(map(str, stop_ids))
    added: Set[str] = set()
    frontier = set(selected)
    while frontier:
        ancestors: Set[str] = set()
        for stop_id in frontier:
            parent = parents.get(stop_id)
            if parent and parent not in selected and parent in present:
                selected.add(parent)
                added.add(parent)
                ancestors.add(parent)
        frontier = ancestors

    return selected, added


def _row_refs(row: Any) -> Tuple[str, str, str, str]:
    return (
        value(row.get("from_stop_id")),
        value(row.get("to_stop_id")),
        value(row.get("from_trip_id")),
        value(row.get("to_trip_id")),
    )


def _survives(row: Any, trips: Set[str], stops: Set[str]) -> bool:
    from_stop, to_stop, from_trip, to_trip = _row_refs(row)
    stop_refs = [ref for ref in (from_stop, to_stop) if ref]
    trip_refs = [ref for ref in (from_trip, to_trip) if ref]
    if not stop_refs and not trip_refs:
        return False
    return all(ref in stops for ref in stop_refs) and all(
        ref in trips for ref in trip_refs
    )


def _touches(
    row: Any, trips: Set[str], stops: Set[str]
) -> List[str]:
    from_stop, to_stop, from_trip, to_trip = _row_refs(row)
    missing: List[str] = []
    for ref in (from_trip, to_trip):
        if ref and ref not in trips:
            missing.append(ref)
    for ref in (from_stop, to_stop):
        if ref and ref not in stops:
            missing.append(ref)
    return missing


def select_transfer_rows(
    transfers: pd.DataFrame, trip_ids: Iterable[str], stop_ids: Iterable[str]
) -> pd.DataFrame:
    """Return only rules whose every reference survives the subset.

    Empty reference fields are wildcards and do not need a counterpart.
    """
    trips = set(map(str, trip_ids))
    stops = set(map(str, stop_ids))
    if transfers.empty:
        return transfers

    keep = [_survives(row, trips, stops) for _, row in transfers.iterrows()]
    return transfers[pd.Series(keep, index=transfers.index, dtype=bool)].copy()


def dropped_rules(
    transfers: pd.DataFrame, trip_ids: Iterable[str], stop_ids: Iterable[str]
) -> List[DroppedRule]:
    """List rules touching the subset that cannot be carried.

    Prohibitions (``transfer_type`` 3) are the dangerous ones: losing them
    flips an answer from "no" to "yes".  Permissions are reported too because
    losing them changes the scope an older reader would have seen.
    """
    trips = set(map(str, trip_ids))
    stops = set(map(str, stop_ids))
    results: List[DroppedRule] = []

    if transfers.empty:
        return results

    for _, row in transfers.iterrows():
        if _survives(row, trips, stops):
            continue
        refs = _row_refs(row)
        missing = _touches(row, trips, stops)
        # A rule that mentions nothing from the subset is simply out of scope.
        if not any(ref in trips for ref in refs[2:]) and not any(
            ref in stops for ref in refs[:2]
        ):
            continue

        fallback_type = transfer_type(row.get("transfer_type"))
        result_type = 0 if fallback_type is None else fallback_type
        kind = "trip" if refs[2] or refs[3] else "stop"
        results.append(
            DroppedRule(
                kind=kind,
                from_trip_id=refs[2],
                to_trip_id=refs[3],
                from_stop_id=refs[0],
                to_stop_id=refs[1],
                transfer_type=result_type,
                reason="missing reference(s): " + ", ".join(missing),
            )
        )

    return results


def _matches(row: Any, candidate: Dict[str, str]) -> bool:
    """Test whether a rule addresses the candidate trip/stop combination."""
    from_stop, to_stop, from_trip, to_trip = _row_refs(row)
    if from_trip and from_trip != candidate["from_trip"]:
        return False
    if to_trip and to_trip != candidate["to_trip"]:
        return False
    if from_stop and from_stop not in (
        candidate["from_stop"],
        candidate["from_parent"],
    ):
        return False
    if to_stop and to_stop not in (candidate["to_stop"], candidate["to_parent"]):
        return False
    return True


def _specificity(row: Any, candidate: Dict[str, str]) -> int:
    """Rank a matching rule; more specific rules override broader ones."""
    from_stop, to_stop, from_trip, to_trip = _row_refs(row)
    trip_level = (1 if from_trip else 0) + (1 if to_trip else 0)
    stop_level = (1 if from_stop else 0) + (1 if to_stop else 0)
    parent_level = 0
    if from_stop == candidate["from_parent"] or to_stop == candidate["to_parent"]:
        parent_level = 1
    return trip_level * 10 + stop_level * 2 + parent_level


def _types_for(
    transfers: pd.DataFrame, candidate: Dict[str, str]
) -> List[int]:
    """Return the transfer types of the most specific rules for a candidate."""
    best: int = -1
    types: List[int] = []
    if transfers.empty:
        return types

    for _, row in transfers.iterrows():
        rule_type = transfer_type(row.get("transfer_type"))
        if rule_type is None or not _matches(row, candidate):
            continue
        rank = _specificity(row, candidate)
        if rank > best:
            best = rank
            types = [rule_type]
        elif rank == best:
            types.append(rule_type)
    return types


def _stop_rows(stop_times: pd.DataFrame, trip_id: str) -> pd.DataFrame:
    if stop_times.empty:
        return stop_times
    return stop_times[stop_times.trip_id == trip_id]


def can_transfer(
    from_trip_id: str,
    to_trip_id: str,
    trips: pd.DataFrame,
    stop_times: pd.DataFrame,
    stops: pd.DataFrame,
    transfers: pd.DataFrame,
) -> bool:
    """Answer whether an arriving trip may connect to a departing trip."""
    from_trip_id = str(from_trip_id)
    to_trip_id = str(to_trip_id)

    if trips.empty:
        return False
    trip_ids = set(map(str, trips.trip_id))
    if from_trip_id not in trip_ids or to_trip_id not in trip_ids:
        return False

    from_rows = _stop_rows(stop_times, from_trip_id)
    to_rows = _stop_rows(stop_times, to_trip_id)
    if from_rows.empty or to_rows.empty:
        return False

    parents = stop_parents(stops)
    for _, arriving in from_rows.iterrows():
        from_stop = value(arriving.stop_id)
        for _, departing in to_rows.iterrows():
            to_stop = value(departing.stop_id)
            same_stop = from_stop == to_stop
            from_parent = parents.get(from_stop, "")
            same_parent = bool(from_parent and from_parent == parents.get(to_stop))
            if not (same_stop or same_parent):
                continue

            candidate = {
                "from_trip": from_trip_id,
                "to_trip": to_trip_id,
                "from_stop": from_stop,
                "to_stop": to_stop,
                "from_parent": from_parent,
                "to_parent": parents.get(to_stop, ""),
            }
            types = _types_for(transfers, candidate)
            if types:
                # A prohibition wins any tie at the same specificity.
                if 3 not in types:
                    return True
                continue
            return True

    return False


def can_transfer_feed(feed: Any, from_trip_id: str, to_trip_id: str) -> bool:
    """Convenience wrapper around :func:`can_transfer` for a loaded feed."""
    return can_transfer(
        from_trip_id,
        to_trip_id,
        feed.trips,
        feed.stop_times,
        feed.stops,
        feed.transfers,
    )
