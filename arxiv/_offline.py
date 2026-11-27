"""Offline reconstruction of a weekly arXiv ranking from stored Atom responses.

A collection run issues deliberately overlapping paged requests against the
same `Search`, and the same request window may have several successful raw
responses retained (retries, repeated collection runs). This module rebuilds
a ranking over a zero-based rank range without any network access.

A usable ranking picks exactly one complete response per request window. A
response is complete for a window when its ``startIndex``, ``itemsPerPage``
and the number of retained ``<entry>`` elements all match the submitted
window, and the selected responses share one ``totalResults`` value that
covers the requested rank range.

Entries are identified by their versioned arXiv short id (e.g.
``2107.05580v1`` or ``quant-ph/0201082v1``). Overlapping responses must carry
the same version at a given rank; records that parse into `Result` objects
must additionally agree on their UTC ``updated`` timestamp and normalized
title. A versioned short id may occupy only one rank. An entry with a
recognizable version id but no readable timestamp still holds its rank; it
can only be completed by a readable record for the same version at the same
rank coming from another selected overlapping response. Entries without a
reliable version id can never serve as that kind of evidence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import TYPE_CHECKING, Any

from ._feed import _build_result, _parse_datetime, _parse_xml, _text

if TYPE_CHECKING:
    from . import Result, Search

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

_MODERN_VERSION = re.compile(r"\d{4}\.\d{4,5}v\d+$")
_LEGACY_VERSION = re.compile(r"[a-z]+(?:-[a-z]+)*/\d{7}v\d+$")
_WHITESPACE = re.compile(r"\s+")


class RequestWindow:
    """A half-open zero-based rank range ``[start, end)`` of one API request."""

    start: int
    """The zero-based rank of the first entry in the window (inclusive)."""
    end: int
    """The zero-based rank just past the last entry in the window."""

    def __init__(self, start: int, end: int) -> None:
        if not isinstance(start, int) or not isinstance(end, int):
            raise TypeError("RequestWindow bounds must be integers")
        if start < 0 or end <= start:
            raise ValueError("RequestWindow requires 0 <= start < end")
        self.start = start
        self.end = end

    @property
    def length(self) -> int:
        """The number of ranks covered by this window."""
        return self.end - self.start

    def contains(self, rank: int) -> bool:
        """Whether `rank` falls inside the window."""
        return self.start <= rank < self.end

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, RequestWindow) and self.start == other.start and self.end == other.end
        )

    def __lt__(self, other: RequestWindow) -> bool:
        return (self.start, self.end) < (other.start, other.end)

    def __hash__(self) -> int:
        return hash((self.start, self.end))

    def __repr__(self) -> str:
        return f"RequestWindow(start={self.start}, end={self.end})"


@dataclass(frozen=True)
class Evidence:
    """A pointer to the entry supporting a board entry at a rank."""

    window_index: int
    """Index of the request window in the canonical (sorted) window order."""
    response_index: int
    """Index of the response within its window, ordered by content hash."""
    position: int
    """Zero-based position of the ``<entry>`` inside that response."""
    readable: bool
    """Whether this entry parsed into a fully readable `Result`."""
    version_id: str | None
    """The recognized versioned short id, if any."""


@dataclass(frozen=True)
class RankCandidate:
    """One record observed at a rank across one or more complete boards."""

    rank: int
    version_id: str
    title: str | None
    updated: datetime | None
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass
class BoardEntry:
    """A single published rank: an optional `Result` plus supporting evidence."""

    rank: int
    version_id: str
    result: Result | None
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def title(self) -> str | None:
        """The normalized title of the backing `Result`, if readable."""
        return None if self.result is None else self.result.title

    @property
    def updated(self) -> datetime | None:
        """The UTC updated timestamp of the backing `Result`, if readable."""
        return None if self.result is None else self.result.updated


@dataclass
class Reconstruction:
    """The outcome of an offline board reconstruction attempt."""

    search: Search
    rank_start: int
    rank_end: int
    published: bool
    """Whether a new board could be published; otherwise the previous board is kept."""
    board: list[BoardEntry] | None
    """The active board: the new one when published, otherwise the retained previous."""
    total_results: int | None
    """The shared ``totalResults`` of the selected responses, when known."""
    uncertain_ranks: list[int]
    """Ranks with more than one viable record; empty when the board is unique."""
    candidates: dict[int, list[RankCandidate]]
    """Per-rank candidates; every candidate occurs in at least one complete board."""
    reasons: list[str]
    """Deterministic, human-readable diagnostics explaining the decision."""
    window_indices: dict[RequestWindow, int]
    """Canonical window ordering, so evidence indices can be traced back to windows."""


@dataclass
class _StoredEntry:
    position: int
    entry_id: str | None
    version_id: str | None
    result: Result | None

    @property
    def readable(self) -> bool:
        return self.result is not None


@dataclass
class _StoredFeed:
    body_hash: str
    total_results: int
    items_per_page: int
    start_index: int
    entries: list[_StoredEntry]
    malformed: bool


def _normalize_title(title: str) -> str:
    return _WHITESPACE.sub(" ", title).strip()


def _extract_version_id(entry_id: str | None) -> str | None:
    """Return the versioned arXiv short id, or None when it is not reliable."""
    if not entry_id:
        return None
    tail = entry_id.split("arxiv.org/abs/")[-1].strip().rstrip("/")
    if _MODERN_VERSION.search(tail) or _LEGACY_VERSION.search(tail):
        return tail
    return None


def _parse_stored_response(body: bytes) -> _StoredFeed:
    """Parse a retained Atom response without dropping unreadable entries."""
    body_hash = hashlib.sha256(bytes(body)).hexdigest()
    root = _parse_xml(bytes(body))
    if root is None:
        return _StoredFeed(body_hash, 0, 0, 0, [], malformed=True)

    def _int(path: str) -> int:
        text = _text(root, path)
        if text is None:
            return 0
        try:
            return int(text.strip())
        except ValueError:
            return 0

    entries: list[_StoredEntry] = []
    for position, elem in enumerate(root.iterfind("atom:entry", _NS)):
        entry_id = _text(elem, "atom:id")
        updated = _parse_datetime(_text(elem, "atom:updated"))
        published = _parse_datetime(_text(elem, "atom:published"))
        # Only invoke the full builder when the required fields are present so
        # deliberately unreadable entries do not emit skip warnings.
        result = (
            _build_result(elem)
            if entry_id and updated is not None and published is not None
            else None
        )
        entries.append(
            _StoredEntry(
                position=position,
                entry_id=entry_id,
                version_id=_extract_version_id(entry_id),
                result=result,
            )
        )

    return _StoredFeed(
        body_hash=body_hash,
        total_results=_int("opensearch:totalResults"),
        items_per_page=_int("opensearch:itemsPerPage"),
        start_index=_int("opensearch:startIndex"),
        entries=entries,
        malformed=False,
    )


def _canonical_responses(bodies: list[bytes]) -> list[tuple[int, _StoredFeed]]:
    """Parse, de-duplicate and order responses so output is input-order independent."""
    unique: dict[str, _StoredFeed] = {}
    for body in bodies:
        feed = _parse_stored_response(body)
        unique.setdefault(feed.body_hash, feed)
    ordered = sorted(unique.values(), key=lambda feed: feed.body_hash)
    return list(enumerate(ordered))


def _matches_window(feed: _StoredFeed, window: RequestWindow) -> bool:
    return (
        not feed.malformed
        and feed.start_index == window.start
        and feed.items_per_page == window.length
        and len(feed.entries) == window.length
    )


def _coerce_window(window: RequestWindow | tuple[int, int]) -> RequestWindow:
    if isinstance(window, RequestWindow):
        return window
    if isinstance(window, tuple) and len(window) == 2:
        return RequestWindow(window[0], window[1])
    raise TypeError("windows must be RequestWindow instances or (start, end) tuples")


def reconstruct_board(
    search: Search,
    rank_start: int,
    rank_end: int,
    windows: list[RequestWindow | tuple[int, int]],
    responses: list[list[bytes]],
    previous: Reconstruction | None = None,
) -> Reconstruction:
    """Rebuild an offline ranking from retained Atom responses.

    `windows` and `responses` are aligned: ``responses[i]`` holds the raw
    successful Atom response bodies collected for ``windows[i]``. The result
    is independent of the order in which windows and responses are supplied.
    """
    if not isinstance(rank_start, int) or not isinstance(rank_end, int):
        raise TypeError("rank bounds must be integers")
    if rank_start < 0 or rank_end <= rank_start:
        raise ValueError("rank range requires 0 <= rank_start < rank_end")
    if len(windows) != len(responses):
        raise ValueError("windows and responses must be aligned one-to-one")

    coerced_windows = [_coerce_window(window) for window in windows]
    canonical_windows = sorted(set(coerced_windows))
    window_indices = {window: index for index, window in enumerate(canonical_windows)}
    ranks = list(range(rank_start, rank_end))
    reasons: list[str] = []
    hard_failure = False

    out_of_range = [
        window for window in canonical_windows if window.start < rank_start or window.end > rank_end
    ]
    if out_of_range:
        hard_failure = True
        reasons.append(
            "One or more windows extend outside the requested rank range "
            f"[{rank_start},{rank_end}): " + ", ".join(f"[{w.start},{w.end})" for w in out_of_range)
        )

    covered = [False] * len(ranks)
    for window in canonical_windows:
        for rank in range(window.start, window.end):
            if rank_start <= rank < rank_end:
                covered[rank - rank_start] = True
    missing_ranks = [rank for rank, ok in zip(ranks, covered) if not ok]
    if missing_ranks:
        hard_failure = True
        reasons.append(f"Windows do not cover ranks {missing_ranks}")

    # Merge responses when the same window was supplied more than once.
    bodies_by_window: dict[RequestWindow, list[bytes]] = {}
    for window, bodies in zip(coerced_windows, responses):
        bodies_by_window.setdefault(window, []).extend(bodies)

    eligible: dict[RequestWindow, list[tuple[int, _StoredFeed]]] = {}
    feed_lookup: dict[tuple[int, int], _StoredFeed] = {}
    for window_index, window in enumerate(canonical_windows):
        candidates = _canonical_responses(bodies_by_window.get(window, []))
        for response_index, feed in candidates:
            feed_lookup[(window_index, response_index)] = feed
        matching = [
            (response_index, feed)
            for response_index, feed in candidates
            if _matches_window(feed, window)
        ]
        if not matching:
            hard_failure = True
            reasons.append(
                f"Window [{window.start},{window.end}) has no complete response: "
                "startIndex, itemsPerPage and entry count must match the window"
            )
        for response_index, feed in candidates:
            if not _matches_window(feed, window):
                reasons.append(
                    f"Response {response_index} of window [{window.start},{window.end}) "
                    "is not complete for that window and was excluded"
                )
        eligible[window] = matching

    totals = {feed.total_results for feeds in eligible.values() for _index, feed in feeds}
    if len(totals) > 1:
        reasons.append(f"Responses disagree on totalResults: {sorted(totals)}")

    identities: dict[tuple[Any, ...], dict[str, Any]] = {}
    incomplete = 0
    structural_reasons: set[str] = set()

    if not hard_failure:
        for total in sorted(totals):
            if total < rank_end:
                reasons.append(f"totalResults={total} does not cover rank end {rank_end}")
                continue
            pools = [
                [
                    (response_index, feed)
                    for response_index, feed in eligible[window]
                    if feed.total_results == total
                ]
                for window in canonical_windows
            ]
            if any(not pool for pool in pools):
                continue
            for combination in product(*pools):
                evaluation = _evaluate_combination(combination, canonical_windows, ranks)
                structural_reasons.update(evaluation.reasons)
                if not evaluation.valid:
                    continue
                identity_key, board_records, evidence_by_rank = (
                    evaluation.identity_key,
                    evaluation.records,
                    evaluation.evidence,
                )
                if not evaluation.complete:
                    incomplete += 1
                    continue
                stored = identities.setdefault(
                    identity_key,
                    {
                        "totals": set(),
                        "records": board_records,
                        "evidence": {},
                    },
                )
                stored["totals"].add(total)
                for rank, evidences in evidence_by_rank.items():
                    stored["evidence"].setdefault(rank, set()).update(evidences)

    if incomplete:
        reasons.append(f"{incomplete} response selection(s) left a rank without a readable Result")
    if not identities:
        reasons.extend(sorted(structural_reasons))

    return _decide(
        search=search,
        rank_start=rank_start,
        rank_end=rank_end,
        canonical_windows=canonical_windows,
        window_indices=window_indices,
        feed_lookup=feed_lookup,
        identities=identities,
        reasons=reasons,
        previous=previous,
    )


@dataclass
class _Evaluation:
    """The verdict for one response-per-window selection."""

    valid: bool
    complete: bool
    identity_key: tuple[Any, ...]
    records: list[tuple[str, str | None, datetime | None]]
    evidence: dict[int, set[Evidence]]
    reasons: list[str]


def _evaluate_combination(
    combination: tuple[tuple[int, _StoredFeed], ...],
    canonical_windows: list[RequestWindow],
    ranks: list[int],
) -> _Evaluation:
    """Validate one response-per-window selection and derive its board identity."""
    occurrences: dict[int, list[tuple[int, int, _StoredEntry]]] = {rank: [] for rank in ranks}
    for window_index, (window, (response_index, feed)) in enumerate(
        zip(canonical_windows, combination)
    ):
        for entry in feed.entries:
            rank = window.start + entry.position
            if rank in occurrences:
                occurrences[rank].append((window_index, response_index, entry))

    board_records: list[tuple[str, str | None, datetime | None]] = []
    evidence_by_rank: dict[int, set[Evidence]] = {rank: set() for rank in ranks}
    seen_versions: dict[str, int] = {}
    reasons: list[str] = []
    valid = True
    complete = True

    for rank in ranks:
        entries = occurrences[rank]
        # Entries without a reliable version id cannot confirm or complete a
        # rank, so they never contribute evidence or consistency checks.
        versioned = [
            (window_index, response_index, entry)
            for window_index, response_index, entry in entries
            if entry.version_id is not None
        ]
        version_ids = {entry.version_id for _w, _r, entry in versioned}
        if not version_ids:
            valid = False
            reasons.append(f"rank {rank}: no entry with a reliable version id")
            continue
        if len(version_ids) > 1:
            valid = False
            reasons.append(
                f"rank {rank}: overlapping responses disagree on version {sorted(version_ids)}"
            )
            continue
        version_id = next(iter(version_ids))
        if version_id in seen_versions:
            valid = False
            reasons.append(
                f"version {version_id} occupies more than one rank: "
                f"{seen_versions[version_id]} and {rank}"
            )
            continue
        seen_versions[version_id] = rank

        matching = [entry for _w, _r, entry in versioned if entry.version_id == version_id]
        readable_results = [entry.result for entry in matching if entry.result is not None]
        titles = {_normalize_title(result.title) for result in readable_results}
        updated_values = {result.updated for result in readable_results}
        if len(titles) > 1:
            valid = False
            reasons.append(f"rank {rank}: readable records disagree on normalized title")
        if len(updated_values) > 1:
            valid = False
            reasons.append(f"rank {rank}: readable records disagree on UTC updated")
        if not readable_results:
            complete = False
            reasons.append(f"rank {rank}: no readable Result for version {version_id}")

        for window_index, response_index, entry in versioned:
            if entry.version_id == version_id:
                evidence_by_rank[rank].add(
                    Evidence(
                        window_index=window_index,
                        response_index=response_index,
                        position=entry.position,
                        readable=entry.readable,
                        version_id=entry.version_id,
                    )
                )
        title = next(iter(titles)) if titles else None
        updated = next(iter(updated_values)) if updated_values else None
        board_records.append((version_id, title, updated))

    return _Evaluation(
        valid=valid,
        complete=complete,
        identity_key=tuple(board_records),
        records=board_records,
        evidence=evidence_by_rank,
        reasons=reasons,
    )


def _build_board(
    records: list[tuple[str, str | None, datetime | None]],
    evidence_by_rank: dict[int, set[Evidence]],
    rank_start: int,
    feed_lookup: dict[tuple[int, int], _StoredFeed],
) -> list[BoardEntry]:
    board: list[BoardEntry] = []
    for offset, (version_id, _title, _updated) in enumerate(records):
        rank = rank_start + offset
        evidences = sorted(
            evidence_by_rank.get(rank, set()),
            key=lambda evidence: (
                evidence.window_index,
                evidence.response_index,
                evidence.position,
            ),
        )
        result = None
        for evidence in evidences:
            if not evidence.readable:
                continue
            feed = feed_lookup[(evidence.window_index, evidence.response_index)]
            entry = feed.entries[evidence.position]
            if entry.version_id == version_id and entry.result is not None:
                result = entry.result
                break
        board.append(
            BoardEntry(rank=rank, version_id=version_id, result=result, evidence=evidences)
        )
    return board


def _decide(
    search: Search,
    rank_start: int,
    rank_end: int,
    canonical_windows: list[RequestWindow],
    window_indices: dict[RequestWindow, int],
    feed_lookup: dict[tuple[int, int], _StoredFeed],
    identities: dict[tuple[Any, ...], dict[str, Any]],
    reasons: list[str],
    previous: Reconstruction | None,
) -> Reconstruction:
    uncertain_ranks: list[int] = []
    candidates: dict[int, list[RankCandidate]] = {}
    total_results: int | None = None
    board: list[BoardEntry] | None = None
    published = False

    if not identities:
        reasons.append("No selection of one complete response per window forms a valid board")
    else:
        keys = sorted(identities, key=lambda key: tuple(record[0] for record in key))
        total_values: set[int] = set()
        for key in keys:
            total_values.update(identities[key]["totals"])
        if len(total_values) > 1:
            reasons.append(
                f"Valid boards use different totalResults values: {sorted(total_values)}"
            )
        else:
            total_results = next(iter(total_values))

        for offset in range(rank_end - rank_start):
            records_at_rank = {key[offset] for key in keys}
            if len(records_at_rank) <= 1:
                continue
            rank = rank_start + offset
            uncertain_ranks.append(rank)
            rank_candidates: list[RankCandidate] = []
            for record in sorted(records_at_rank, key=lambda rec: rec[0]):
                evidence_sets = [
                    identities[key]["evidence"].get(rank, set())
                    for key in keys
                    if key[offset] == record
                ]
                merged: set[Evidence] = set().union(*evidence_sets)
                rank_candidates.append(
                    RankCandidate(
                        rank=rank,
                        version_id=record[0],
                        title=record[1],
                        updated=record[2],
                        evidence=tuple(
                            sorted(
                                merged,
                                key=lambda evidence: (
                                    evidence.window_index,
                                    evidence.response_index,
                                    evidence.position,
                                ),
                            )
                        ),
                    )
                )
            candidates[rank] = rank_candidates

        if not uncertain_ranks and len(total_values) == 1:
            chosen = identities[keys[0]]
            board = _build_board(
                records=chosen["records"],
                evidence_by_rank=chosen["evidence"],
                rank_start=rank_start,
                feed_lookup=feed_lookup,
            )
            published = True
            if len(keys) > 1:
                reasons.append(
                    "Multiple response selections yield identical ranks, titles and "
                    "updated timestamps; publishing the shared board"
                )
        else:
            reasons.append(
                "Multiple distinct complete boards exist; retaining the previously "
                f"published board and flagging ranks {uncertain_ranks}"
            )

    if not published:
        board = previous.board if previous is not None else None

    return Reconstruction(
        search=search,
        rank_start=rank_start,
        rank_end=rank_end,
        published=published,
        board=board,
        total_results=total_results,
        uncertain_ranks=uncertain_ranks,
        candidates=candidates,
        reasons=reasons,
        window_indices=window_indices,
    )
