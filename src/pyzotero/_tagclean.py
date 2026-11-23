"""Idempotent batch tag cleanup.

A cleanup request names an item, the tags to remove, and the tags to add.
Planning the write is a pure function of four inputs:

1. the requested intent (:class:`TagCleanupRequest`),
2. the item's current tag names,
3. the per-item journal of intents that were dispatched and cleanups that
   completed (:class:`TagCleanupJournal`), and
4. any server-side tag events ingested for the item.

Because the decision never depends on transport details, re-opening the
client or moving an item across a 50-object request chunk yields the same
classification. The journal is what makes a request safe to repeat after the
acknowledgement of a completed write has been lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, TypeAlias

TagCleanupStatus: TypeAlias = Literal[
    "applied",
    "backfill_applied",
    "already_applied",
    "conflict",
    "needs_review",
]

TagEventAction: TypeAlias = Literal["added", "removed"]


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True)
class TagCleanupRequest:
    """One entry of a batch tag cleanup.

    Args:
        key: The item key the intent applies to.
        remove: Tag names to remove from the item.
        add: Tag names to add to the item, appended after the surviving tags.
        version: Optional item version the caller based the intent on. The
            live item version is used for the write precondition when given.

    """

    key: str
    remove: tuple[str, ...] = ()
    add: tuple[str, ...] = ()
    version: int | None = None

    @property
    def remove_set(self) -> frozenset[str]:
        """The removal intent as a set."""
        return frozenset(self.remove)

    @property
    def add_set(self) -> frozenset[str]:
        """The addition intent as a set."""
        return frozenset(self.add)


@dataclass(frozen=True)
class TagCleanupDispatch:
    """An intent that was sent to the server but not yet confirmed.

    A dispatch survives a lost acknowledgement or a failed process. It is the
    evidence that turns an otherwise ordinary removal into a manual-review
    case on a later run.
    """

    key: str
    remove: frozenset[str]
    add: frozenset[str]
    at: str


@dataclass(frozen=True)
class TagCleanupRecord:
    """A cleanup that this client applied to an item."""

    key: str
    remove: frozenset[str]
    add: frozenset[str]
    applied_tags: tuple[str, ...]
    version: int | None
    at: str


@dataclass(frozen=True)
class TagEvent:
    """A tag change observed in the server's per-item edit history.

    Ingest events for an item when another editor may have changed its tags.
    They let the planner distinguish a tag that was deliberately re-created by
    someone else from a state it cannot account for.
    """

    tag: str
    action: TagEventAction
    at: str | None = None
    actor: str = "server"


@dataclass(frozen=True)
class TagConflict:
    """A requested removal that cannot be applied safely."""

    tag: str
    reason: str
    actor: str | None = None


@dataclass
class TagCleanupResult:
    """The outcome for one cleanup request, keyed by its input position."""

    index: int
    key: str
    status: TagCleanupStatus
    remove: tuple[str, ...]
    add: tuple[str, ...]
    current_tags: list[str]
    target_tags: list[str] | None = None
    conflicts: list[TagConflict] = field(default_factory=list)
    events: list[TagEvent] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    record: TagCleanupRecord | None = None

    @property
    def writable(self) -> bool:
        """Whether a write was (or, pre-send, should be) performed."""
        return self.status in {"applied", "backfill_applied"}


@dataclass
class TagCleanupReport:
    """All per-item outcomes, in the same order as the submitted requests."""

    results: list[TagCleanupResult]

    def by_status(self, status: TagCleanupStatus) -> list[TagCleanupResult]:
        """Return the results carrying ``status``."""
        return [result for result in self.results if result.status == status]

    @property
    def applied(self) -> list[TagCleanupResult]:
        """Results whose intent was written during this or an earlier run."""
        return [
            result
            for result in self.results
            if result.status in {"applied", "backfill_applied", "already_applied"}
        ]

    @property
    def backfills(self) -> list[TagCleanupResult]:
        """Results that only needed the missing additions written."""
        return self.by_status("backfill_applied")

    @property
    def conflicts(self) -> list[TagCleanupResult]:
        """Results blocked because a removed tag was re-created."""
        return self.by_status("conflict")

    @property
    def needs_review(self) -> list[TagCleanupResult]:
        """Results that could not be classified safely."""
        return self.by_status("needs_review")


class TagCleanupJournal:
    """Append-only, per-item history of tag cleanups and server events.

    One instance can be shared between calls and between client instances:
    pass it to ``Zotero.cleanup_tags()`` to carry the history across a client
    restart. The default journal lives on the client and covers its lifetime.

    Two kinds of entries exist per item:

    * dispatches: intents that were sent without a confirmed outcome, written
      immediately before the HTTP request so a lost acknowledgement leaves
      evidence;
    * records: cleanups confirmed as applied.
    """

    def __init__(self) -> None:
        self._dispatches: dict[str, list[TagCleanupDispatch]] = {}
        self._records: dict[str, list[TagCleanupRecord]] = {}
        self._events: dict[str, list[TagEvent]] = {}

    def mark_dispatched(self, request: TagCleanupRequest) -> TagCleanupDispatch:
        """Record that ``request`` is about to be sent."""
        dispatch = TagCleanupDispatch(
            key=request.key,
            remove=request.remove_set,
            add=request.add_set,
            at=_now_iso(),
        )
        self._dispatches.setdefault(request.key, []).append(dispatch)
        return dispatch

    def append(self, record: TagCleanupRecord) -> None:
        """Record a cleanup that was applied successfully."""
        self._records.setdefault(record.key, []).append(record)
        # A confirmed write settles every earlier dispatch for the item.
        self._dispatches.pop(record.key, None)

    def record_events(
        self, key: str, events: list[TagEvent] | tuple[TagEvent, ...]
    ) -> None:
        """Ingest server-side tag events for an item, oldest first."""
        self._events.setdefault(key, []).extend(events)

    def dispatches_for(self, key: str) -> list[TagCleanupDispatch]:
        """Return the unconfirmed dispatches for an item, oldest first."""
        return list(self._dispatches.get(key, ()))

    def records_for(self, key: str) -> list[TagCleanupRecord]:
        """Return the completed cleanup records for an item, oldest first."""
        return list(self._records.get(key, ()))

    def events_for(self, key: str) -> list[TagEvent]:
        """Return the ingested server events for an item, oldest first."""
        return list(self._events.get(key, ()))

    def item_keys(self) -> list[str]:
        """Return the keys carrying at least one dispatch, record or event."""
        return list(
            self._dispatches.keys() | self._records.keys() | self._events.keys()
        )

    def __len__(self) -> int:
        """Return the number of completed cleanup records in the journal."""
        return sum(len(records) for records in self._records.values())


def _completed_record(
    request: TagCleanupRequest, records: list[TagCleanupRecord]
) -> TagCleanupRecord | None:
    """Find a record proving the whole requested intent was already applied."""
    for record in reversed(records):
        if request.remove_set <= record.remove and request.add_set <= record.add:
            return record
    return None


def plan_tag_cleanup(
    request: TagCleanupRequest,
    current_tags: list[str] | tuple[str, ...],
    records: list[TagCleanupRecord],
    events: list[TagEvent],
    dispatches: list[TagCleanupDispatch] | None = None,
    index: int = 0,
) -> TagCleanupResult:
    """Decide what to do with one cleanup request.

    The function performs no I/O and never mutates its inputs.

    Statuses:

    * ``already_applied``: the journal proves the full intent completed, so no
      second write is made even if the acknowledgement was lost.
    * ``conflict``: a tag slated for removal is present again and the history
      shows it was removed and re-created (by this client or another editor).
      The re-created tag is preserved and nothing is written for the item.
    * ``needs_review``: a tag slated for removal is present again after an
      unconfirmed dispatch, without history showing whether the original
      removal applied or the tag was rebuilt.
    * ``backfill_applied``: nothing remains to remove; only missing additions
      need writing. Other fields are not part of the write and are preserved.
    * ``applied``: at least one present tag has to be removed, after which the
      missing additions are appended.
    """
    dispatches = dispatches or []
    names = list(dict.fromkeys(current_tags))
    present = set(names)
    completed = _completed_record(request, records)

    conflicts: list[TagConflict] = []
    ambiguous: list[str] = []
    for name in request.remove:
        if name not in present:
            continue
        client_removed = any(name in record.remove for record in records)
        readded_events = [
            event for event in events if event.tag == name and event.action == "added"
        ]
        if client_removed:
            conflicts.append(
                TagConflict(
                    tag=name,
                    reason=(
                        "The tag was removed by an earlier recorded cleanup and "
                        "has since been re-created."
                    ),
                )
            )
        elif readded_events:
            event = readded_events[-1]
            was_removed_in_history = any(
                event.tag == name and event.action == "removed" for event in events
            )
            detail = (
                "the tag was removed and then re-created"
                if was_removed_in_history
                else "a later edit re-added the tag"
            )
            conflicts.append(
                TagConflict(
                    tag=name,
                    reason=(
                        f"Server history shows {detail}; removing it again would "
                        "discard that edit."
                    ),
                    actor=event.actor,
                )
            )
        elif dispatches:
            ambiguous.append(name)

    if conflicts:
        relevant = [
            event
            for event in events
            if event.tag in {conflict.tag for conflict in conflicts}
        ]
        return TagCleanupResult(
            index=index,
            key=request.key,
            status="conflict",
            remove=request.remove,
            add=request.add,
            current_tags=names,
            conflicts=conflicts,
            events=relevant,
            reasons=[
                f"Requested removal of {conflict.tag!r} conflicts with newer "
                f"history: {conflict.reason}"
                for conflict in conflicts
            ],
        )

    if ambiguous:
        return TagCleanupResult(
            index=index,
            key=request.key,
            status="needs_review",
            remove=request.remove,
            add=request.add,
            current_tags=names,
            reasons=[
                f"Tag {name!r} is present again after an unconfirmed dispatch, "
                "but no journal record or server event shows whether the "
                "original removal applied or the tag was re-created."
                for name in ambiguous
            ],
        )

    surviving = [name for name in names if name not in request.remove_set]
    additions = [name for name in request.add if name not in set(surviving)]
    target = surviving + additions

    if target == names:
        reasons = ["The item already carries exactly the requested tag set."]
        if completed is not None:
            reasons = [
                "A journal record proves the requested cleanup already completed."
            ]
        return TagCleanupResult(
            index=index,
            key=request.key,
            status="already_applied",
            remove=request.remove,
            add=request.add,
            current_tags=names,
            record=completed,
            reasons=reasons,
        )

    removed_present = [name for name in names if name in request.remove_set]
    status: TagCleanupStatus = "applied" if removed_present else "backfill_applied"
    return TagCleanupResult(
        index=index,
        key=request.key,
        status=status,
        remove=request.remove,
        add=request.add,
        current_tags=names,
        target_tags=target,
    )


def make_record(
    request: TagCleanupRequest, applied_tags: list[str]
) -> TagCleanupRecord:
    """Build the journal record for a successfully applied cleanup."""
    return TagCleanupRecord(
        key=request.key,
        remove=request.remove_set,
        add=request.add_set,
        applied_tags=tuple(applied_tags),
        version=request.version,
        at=_now_iso(),
    )


__all__ = [
    "TagCleanupDispatch",
    "TagCleanupJournal",
    "TagCleanupRecord",
    "TagCleanupReport",
    "TagCleanupRequest",
    "TagCleanupResult",
    "TagConflict",
    "TagEvent",
    "make_record",
    "plan_tag_cleanup",
]
