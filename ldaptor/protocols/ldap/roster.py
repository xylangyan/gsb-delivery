"""Versioned, ascending roster paging for merged LDAP directories.

The merger serves several directories behind one LDAP endpoint.  Roster
clients need a stronger contract than a plain merged search:

* every directory answers in ascending personnel-number order;
* a query is pinned to one version (snapshot) of every directory;
* a page is only released once every live directory has moved strictly
  past every personnel number in it, so a later response cannot add a
  field or a source to an already confirmed page;
* duplicate personnel numbers are merged with fields from the primary
  directory taking precedence, while the result records every directory
  in which the person actually occurred and how many original
  registrations that directory supplied;
* a continuation cookie belongs to one query and one client connection;
  it is not valid on a new connection;
* a query can be resumed on a new connection, on the same version,
  after the last number the client confirmed;
* if a directory loses the pinned version, the personnel-number range it
  can no longer settle is reported instead of guessed.

This module contains the state machine.  ``merger`` adapts it to LDAP
searches and the RFC 2696 paged-results control.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ldaptor._encoder import to_bytes
from ldaptor.protocols import pureber


PAGED_RESULTS_OID = b"1.2.840.113556.1.4.319"
"""RFC 2696 paged-results control."""

ROSTER_RESUME_OID = b"1.3.6.1.4.1.22024.1.2.2"
"""Request control: query id, pinned version, last confirmed number."""

ROSTER_STATE_OID = b"1.3.6.1.4.1.22024.1.2.5"
"""Response control: query id, pinned version, last confirmed number."""

ROSTER_UNSETTLED_OID = b"1.3.6.1.4.1.22024.1.2.1"
"""Response control naming roster ranges that cannot be settled."""

ROSTER_SOURCE_REQUEST_OID = b"1.3.6.1.4.1.22024.1.2.3"
"""Per-directory request: version, inclusive cursor, page limit."""

ROSTER_SOURCE_RESPONSE_OID = b"1.3.6.1.4.1.22024.1.2.4"
"""Per-directory response: version, next-number watermark, ended."""

DEFAULT_NUMBER_ATTRIBUTE = b"employeeNumber"

SOURCE_ATTRIBUTE = b"rosterSource"
OCCURRENCE_ATTRIBUTE = b"rosterOccurrence"
PRIMARY_ATTRIBUTE = b"rosterPrimary"

RESERVED_ATTRIBUTES = frozenset(
    name.lower()
    for name in (SOURCE_ATTRIBUTE, OCCURRENCE_ATTRIBUTE, PRIMARY_ATTRIBUTE)
)


class RosterError(Exception):
    """Base class for roster paging errors."""


class InvalidContinuationToken(RosterError):
    """A continuation cookie is unknown, reused, or from another connection."""


class SnapshotLost(RosterError):
    """A directory no longer has the snapshot a query was pinned to."""


class RosterProtocolError(RosterError):
    """A directory or client violated the roster paging contract."""


# ---------------------------------------------------------------------------
# Personnel numbers
# ---------------------------------------------------------------------------


def number_key(value: Any) -> tuple[int, Any]:
    """Return a sortable key for a personnel number.

    Purely numeric numbers compare numerically, so ``"84"`` and ``"084"``
    are the same person.  Anything else compares as bytes.  Numeric
    numbers sort before non-numeric ones so mixed directories stay
    deterministic instead of failing part-way through a page.
    """
    raw = to_bytes(value).strip()
    try:
        return (0, int(raw))
    except (TypeError, ValueError):
        return (1, raw)


def number_text(value: Any) -> bytes:
    """Return the wire form of a personnel number."""
    return to_bytes(value).strip()


def number_successor(value: Any) -> bytes:
    """Return a number strictly greater than *value* (diagnostics only)."""
    key = number_key(value)
    if key[0] == 0:
        return str(key[1] + 1).encode("ascii")
    return key[1] + b"\x00"


# ---------------------------------------------------------------------------
# Records and pages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterRecord:
    """One original registration returned by one directory."""

    number: bytes
    object_name: bytes
    attributes: tuple[tuple[bytes, tuple[bytes, ...]], ...]
    source: str
    key: tuple[int, Any] = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "key", number_key(self.number))

    def attribute_dict(self) -> dict[bytes, tuple[bytes, ...]]:
        return dict(self.attributes)

    def identity(self) -> tuple:
        return (self.object_name, self.attributes)


@dataclass(frozen=True)
class SourcePage:
    """A batch returned by one directory.

    ``next_number`` is a *lower bound* for the next response, not an
    exclusive cursor.  The next response from this directory is
    guaranteed not to contain a smaller number.  A batch ending at
    ``084`` may therefore report ``next_number == 084`` when more
    registrations for ``084`` can still follow.  Such a number stays
    open until the directory reports a strictly larger bound or
    ``ended``.
    """

    source: str
    records: tuple[RosterRecord, ...] = ()
    next_number: Optional[bytes] = None
    ended: bool = False
    version: Any = None

    def __post_init__(self):
        if not self.ended and self.next_number is None:
            raise RosterProtocolError(
                "source %s returned a page with neither a next number nor an end"
                % self.source
            )
        keys = [record.key for record in self.records]
        if keys != sorted(keys):
            raise RosterProtocolError(
                "source %s returned personnel numbers out of order"
                % self.source
            )
        if self.records and self.next_number is not None:
            if number_key(self.next_number) < self.records[-1].key:
                raise RosterProtocolError(
                    "source %s returned a next number before its last record"
                    % self.source
                )
        for record in self.records:
            if record.source != self.source:
                raise RosterProtocolError(
                    "record source %r does not match page source %r"
                    % (record.source, self.source)
                )


@dataclass(frozen=True)
class MergedRosterEntry:
    """One personnel number after all directories are reconciled."""

    number: bytes
    object_name: bytes
    attributes: tuple[tuple[bytes, tuple[bytes, ...]], ...]
    sources: tuple[str, ...]
    primary_source: Optional[str]
    occurrences: tuple[tuple[str, int], ...]
    key: tuple[int, Any] = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "key", number_key(self.number))

    def attribute_dict(self) -> dict[bytes, tuple[bytes, ...]]:
        return dict(self.attributes)


@dataclass(frozen=True)
class UnsettledRange:
    """A personnel-number range a lost snapshot can no longer settle."""

    query_id: str
    start: Optional[bytes]
    end: Optional[bytes] = None
    reason: str = "snapshot lost"

    def __str__(self) -> str:
        start = (
            "start"
            if self.start is None
            else self.start.decode("utf-8", "replace")
        )
        if self.end is None:
            span = "%s..end" % start
        else:
            span = "%s..%s" % (
                start,
                self.end.decode("utf-8", "replace"),
            )
        return "query %s: %s (%s)" % (self.query_id, span, self.reason)


@dataclass
class PageResult:
    """The outcome of trying to build one client page."""

    entries: list[MergedRosterEntry] = field(default_factory=list)
    need: list[str] = field(default_factory=list)
    complete: bool = False
    blocked: bool = False
    unsettled: list[UnsettledRange] = field(default_factory=list)
    replay: bool = False
    more: bool = False

    @property
    def last_number(self) -> Optional[bytes]:
        return self.entries[-1].number if self.entries else None


# ---------------------------------------------------------------------------
# Per-source state
# ---------------------------------------------------------------------------


class SourceState:
    """State one directory contributes to one query.

    ``cursor`` is the inclusive lower bound for the next request to this
    directory.  A number ``N`` is settled only when ``N < cursor`` (or
    the source ended): a response may be followed by another response
    that still starts at ``N`` and adds another registration for it.

    ``lost_from`` is the first number this source can no longer settle
    because it lost the pinned version.  Numbers before it remain
    confirmable from the other sources and from data already received.
    """

    def __init__(self, name: str, order: int):
        self.name = name
        self.order = order
        self.version: Optional[bytes] = None
        self.cursor: Optional[bytes] = None
        self.ended = False
        self.lost = False
        self.lost_from: Optional[bytes] = None
        self.lost_reason: Optional[str] = None
        self.cookie: bytes = b""
        # "native" follows RFC 2696 cookies from the directory itself;
        # "custom" uses this module's source response control; "plain"
        # uses short-page detection.  None means not yet known.
        self.mode: Optional[str] = None
        self.buffer: dict[tuple[int, Any], list[RosterRecord]] = {}
        self._seen: set[tuple] = set()
        self.last_number: Optional[bytes] = None
        self.records_seen = 0
        self.pages_seen = 0

    @property
    def finished(self) -> bool:
        return self.ended or self.lost

    def settled(self, key: tuple[int, Any]) -> bool:
        if self.ended:
            return True
        if self.lost:
            if self.lost_from is None:
                return False
            return key < number_key(self.lost_from)
        return self.cursor is not None and key < number_key(self.cursor)

    def needs(self, key: tuple[int, Any]) -> bool:
        if self.finished:
            return False
        return self.cursor is None or key >= number_key(self.cursor)

    def apply(self, page: SourcePage) -> None:
        page_version = (
            to_bytes(page.version) if page.version is not None else None
        )
        if page_version is not None:
            if self.version is None:
                self.version = page_version
            elif self.version != page_version:
                self.mark_lost(
                    self.cursor,
                    "directory no longer has version %r"
                    % self.version.decode("utf-8", "replace"),
                )
                return

        if page.records and self.cursor is not None:
            first = page.records[0].key
            if first < number_key(self.cursor):
                self.mark_lost(
                    self.cursor,
                    "directory returned a number below its cursor",
                )
                return
        if (
            page.records
            and self.last_number is not None
            and self.cursor is None
        ):
            # Without an explicit watermark the inclusive cursor is the
            # last number, so a batch may legitimately start there.  Only
            # a strictly smaller number is a real regression.
            first = page.records[0].key
            if first < number_key(self.last_number):
                self.mark_lost(
                    self.last_number,
                    "directory moved backwards below its last record",
                )
                return

        previous_cursor = self.cursor
        previously_seen = set(self._seen)
        added = 0

        for record in page.records:
            if record.identity() in previously_seen:
                # An inclusive cursor can legitimately repeat the last
                # number.  Identical records inside one response are
                # still kept, so real duplicate registrations count.
                continue
            self.buffer.setdefault(record.key, []).append(record)
            self.last_number = record.number
            self.records_seen += 1
            added += 1

        self._seen.update(record.identity() for record in page.records)
        self.pages_seen += 1

        if page.ended:
            self.ended = True
            self.cursor = None
            self.cookie = b""
            return

        if page.next_number is not None:
            bound = number_text(page.next_number)
            if self.cursor is None or number_key(bound) >= number_key(self.cursor):
                self.cursor = bound
        elif self.last_number is not None:
            self.cursor = self.last_number

        if added == 0 and not page.ended:
            if (
                previous_cursor is not None
                and self.cursor is not None
                and number_key(self.cursor) <= number_key(previous_cursor)
            ):
                self.mark_lost(
                    self.cursor,
                    "directory repeated its page without advancing",
                )

    def mark_lost(self, start: Optional[bytes], reason: str) -> None:
        if self.ended or self.lost:
            return
        self.lost = True
        self.lost_from = number_text(start) if start is not None else None
        self.lost_reason = reason
        self.cookie = b""
        # Keep ``cursor``: it is the watermark required to resume the
        # same version on a new connection.  Settlement while lost is
        # governed by ``lost_from``, not by this cursor.

    def rebind(self, after: Optional[bytes]) -> None:
        """Prepare this source for a fresh directory connection.

        Native paging cookies are connection-scoped.  The cursor already
        points beyond every confirmed number; if it does not, move it to
        just after the last confirmed number.
        """
        self.cookie = b""
        self.mode = None
        if self.ended or self.lost:
            return
        if after is not None:
            successor = number_successor(after)
            if self.cursor is None or number_key(self.cursor) <= number_key(after):
                self.cursor = successor

    def peek(self, key: tuple[int, Any]) -> list[RosterRecord]:
        return list(self.buffer.get(key, ()))

    def discard(self, key: tuple[int, Any]) -> None:
        self.buffer.pop(key, None)

    def minimum(self) -> Optional[tuple[int, Any]]:
        return min(self.buffer) if self.buffer else None


# ---------------------------------------------------------------------------
# Query state
# ---------------------------------------------------------------------------


class RosterQuery:
    """State for one paged roster query across several directories.

    Every directory has its own state, so a failure or cursor in one
    query never leaks into another query or another shift's page.
    """

    def __init__(
        self,
        query_id: str,
        key: Any,
        page_size: int,
        source_names: Iterable[str],
        primary_source: Optional[str] = None,
        version: Optional[bytes] = None,
        resume_after: Optional[bytes] = None,
    ):
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.query_id = query_id
        self.key = key
        self.page_size = page_size
        self.version = (
            to_bytes(version)
            if version is not None
            else secrets.token_bytes(16)
        )
        self.primary_source = primary_source
        self.states = [
            SourceState(name, order) for order, name in enumerate(source_names)
        ]
        self.by_name = {state.name: state for state in self.states}
        if primary_source is not None and primary_source not in self.by_name:
            raise ValueError("primary_source must be one of source_names")
        self.confirmed_after = (
            number_text(resume_after) if resume_after is not None else None
        )
        self.served_after = self.confirmed_after
        self.pages_served = 0
        self._pending: Optional[list[MergedRosterEntry]] = None
        self._pending_keys: list[tuple[int, Any]] = []

    # -- feeding ---------------------------------------------------------

    def feed(self, page: SourcePage) -> None:
        state = self.by_name.get(page.source)
        if state is None:
            raise RosterProtocolError(
                "unknown roster source %r" % (page.source,)
            )
        state.apply(page)

    def mark_source_lost(
        self,
        source: str,
        start: Optional[bytes] = None,
        reason: str = "source lost",
    ) -> None:
        state = self.by_name.get(source)
        if state is None:
            return
        state.mark_lost(state.cursor if start is None else start, reason)

    def source_state(self, source: str) -> SourceState:
        return self.by_name[source]

    # -- status ----------------------------------------------------------

    def _all_ended(self) -> bool:
        return all(state.ended for state in self.states)

    def _all_finished(self) -> bool:
        return all(state.finished for state in self.states)

    def _buffered(self, exclude: Optional[set] = None) -> bool:
        exclude = exclude or set()
        return any(
            records and key not in exclude
            for state in self.states
            for key, records in state.buffer.items()
        )

    @property
    def complete(self) -> bool:
        return self._all_ended() and not self._buffered()

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    @property
    def exhausted(self) -> bool:
        if self.has_pending:
            return False
        return self.complete or (
            self._all_finished() and not self._has_settled_buffered()
        )

    # -- page construction ----------------------------------------------

    def _settled(self, key: tuple[int, Any]) -> bool:
        return all(state.settled(key) for state in self.states)

    def _sources_needed(self, key: tuple[int, Any]) -> list[str]:
        return [
            state.name for state in self.states if state.needs(key)
        ]

    def _merge(self, key: tuple[int, Any]) -> MergedRosterEntry:
        records: list[RosterRecord] = []
        for state in sorted(self.states, key=lambda item: item.order):
            records.extend(state.peek(key))
        if not records:
            raise RosterProtocolError(
                "cannot merge an empty personnel number"
            )

        by_source: dict[str, list[RosterRecord]] = {}
        for record in records:
            by_source.setdefault(record.source, []).append(record)

        sources = tuple(
            state.name for state in self.states if state.name in by_source
        )
        ordered = list(sources)
        if self.primary_source in ordered:
            ordered.remove(self.primary_source)
            ordered.insert(0, self.primary_source)

        attributes: list[tuple[bytes, tuple[bytes, ...]]] = []
        for attr in _attribute_order(ordered, by_source):
            if attr.lower() in RESERVED_ATTRIBUTES:
                continue
            if (
                self.primary_source in by_source
                and _has_attribute(by_source[self.primary_source], attr)
            ):
                # The primary directory's field wins outright.
                suppliers = [self.primary_source]
            else:
                suppliers = ordered
            values: list[bytes] = []
            for name in suppliers:
                for record in by_source[name]:
                    values.extend(record.attribute_dict().get(attr, ()))
            attributes.append((attr, tuple(_unique(values))))

        attributes.append(
            (
                SOURCE_ATTRIBUTE,
                tuple(name.encode("utf-8") for name in sources),
            )
        )
        attributes.append(
            (
                OCCURRENCE_ATTRIBUTE,
                tuple(
                    ("%s:%d" % (name, len(by_source[name]))).encode("utf-8")
                    for name in sources
                ),
            )
        )
        if self.primary_source in by_source:
            attributes.append((PRIMARY_ATTRIBUTE, (b"TRUE",)))

        object_name = records[0].object_name
        primary_source = None
        if self.primary_source in by_source:
            object_name = by_source[self.primary_source][0].object_name
            primary_source = self.primary_source

        return MergedRosterEntry(
            number=records[0].number,
            object_name=object_name,
            attributes=tuple(attributes),
            sources=sources,
            primary_source=primary_source,
            occurrences=tuple(
                (name, len(by_source[name])) for name in sources
            ),
        )

    def build_page(self) -> PageResult:
        """Build the next page without consuming source state.

        A page is not committed until :meth:`mark_served`; until the
        client asks for the following page, it remains replayable after
        a reconnect.  Entries before a blocked number are already
        settled and are returned even when a lost directory prevents
        the rest.
        """
        if self._pending is not None:
            return PageResult(
                entries=list(self._pending),
                complete=self.complete,
                replay=True,
                more=not self.complete,
            )

        entries: list[MergedRosterEntry] = []
        need: list[str] = []
        unsettled: list[UnsettledRange] = []
        used: set[tuple[int, Any]] = set()

        while len(entries) < self.page_size:
            # A source with no buffered records may still hold numbers
            # below the smallest buffered one.  Fetch it before emitting
            # anything out of order.
            buffered = sorted(
                {
                    key
                    for state in self.states
                    for key in state.buffer
                    if state.buffer.get(key) and key not in used
                }
            )

            waiting = []
            for state in self.states:
                if state.finished:
                    continue
                if state.cursor is None:
                    waiting.append(state.name)
                elif buffered and number_key(state.cursor) < buffered[0]:
                    waiting.append(state.name)
            if waiting:
                need = waiting
                break

            if not buffered:
                if not self._all_finished():
                    need = [
                        state.name
                        for state in self.states
                        if not state.finished
                    ]
                break

            key = buffered[0]
            if self._settled(key):
                entries.append(self._merge(key))
                used.add(key)
                continue

            needed = self._sources_needed(key)
            if needed:
                need = needed
                break

            # Only a lost source can prevent this number from settling.
            unsettled = self.unsettled_ranges()
            break

        remaining = self._buffered(exclude=used)
        complete = self._all_ended() and not remaining
        blocked = (
            not complete
            and not need
            and self._all_finished()
            and not self._has_settled_buffered(exclude=used)
        )
        if blocked and not unsettled:
            unsettled = self.unsettled_ranges()

        more = (
            not complete
            and not need
            and self._has_settled_buffered(exclude=used)
        )

        return PageResult(
            entries=entries,
            need=need,
            complete=complete,
            blocked=blocked,
            unsettled=unsettled,
            more=more,
        )

    def _has_settled_buffered(self, exclude: Optional[set] = None) -> bool:
        exclude = exclude or set()
        for state in self.states:
            for key, records in state.buffer.items():
                if records and key not in exclude and self._settled(key):
                    return True
        return False

    def unsettled_ranges(self) -> list[UnsettledRange]:
        """Return one open range per source that lost its snapshot."""
        ranges = []
        for state in self.states:
            if not state.lost:
                continue
            start = state.lost_from
            if self.confirmed_after is not None:
                successor = number_successor(self.confirmed_after)
                if start is None or number_key(start) <= number_key(
                    self.confirmed_after
                ):
                    start = successor
            ranges.append(
                UnsettledRange(
                    query_id=self.query_id,
                    start=start,
                    end=None,
                    reason=state.lost_reason or "snapshot lost",
                )
            )
        return ranges

    # -- confirmation and reconnect -------------------------------------

    def mark_served(
        self, entries: Optional[list[MergedRosterEntry]] = None
    ) -> None:
        """Remember the page just handed to the client."""
        entries = list(entries or ())
        self._pending = entries
        self._pending_keys = [entry.key for entry in entries]
        if entries:
            self.served_after = entries[-1].number
        self.pages_served += 1

    def confirm_served(self) -> None:
        """The next page request confirms the previously served page."""
        if self._pending is None:
            return
        for key in self._pending_keys:
            for state in self.states:
                state.discard(key)
        if self._pending:
            self.confirmed_after = self._pending[-1].number
            self.served_after = self.confirmed_after
        self._pending = None
        self._pending_keys = []

    def prepare_resume(self) -> None:
        """Resume on a new connection without confirming the last page."""
        self._pending = None
        self._pending_keys = []

    def rebind_sources(self) -> None:
        """Prepare every source for a fresh connection.

        A connection loss is not proof that the directory lost the
        pinned version; it only invalidates connection-scoped cookies.
        Clear the loss marker and ask again for the same version.  If
        the directory really cannot provide it, its next response marks
        the range unsettled again.
        """
        for state in self.states:
            if state.ended:
                continue
            state.lost = False
            state.lost_from = None
            state.lost_reason = None
            state.rebind(self.confirmed_after)

    def state_payload(self) -> tuple[str, bytes, Optional[bytes]]:
        return self.query_id, self.version, self.confirmed_after


def _has_attribute(
    records: Iterable[RosterRecord], name: bytes
) -> bool:
    return any(name in record.attribute_dict() for record in records)


def _attribute_order(
    ordered_sources: Iterable[str],
    by_source: dict[str, list[RosterRecord]],
) -> list[bytes]:
    order: list[bytes] = []
    seen: set[bytes] = set()
    for name in ordered_sources:
        for record in by_source.get(name, ()):
            for attr in record.attribute_dict():
                if attr in seen:
                    continue
                seen.add(attr)
                order.append(attr)
    return order


def _unique(values: Iterable[bytes]) -> list[bytes]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# Pager / connection-scoped continuations
# ---------------------------------------------------------------------------


@dataclass
class _Continuation:
    query: RosterQuery
    connection_id: Any


class RosterPager:
    """Owns roster queries and connection-scoped continuation cookies."""

    def __init__(self):
        self.queries: dict[str, RosterQuery] = {}
        self._by_key: dict[Any, list[str]] = {}
        self._tokens: dict[bytes, _Continuation] = {}

    def new_query(
        self,
        key: Any,
        page_size: int,
        source_names: Iterable[str],
        primary_source: Optional[str] = None,
        version: Optional[bytes] = None,
        query_id: Optional[str] = None,
        resume_after: Optional[bytes] = None,
    ) -> RosterQuery:
        query_id = query_id or secrets.token_hex(12)
        query = RosterQuery(
            query_id=query_id,
            key=key,
            page_size=page_size,
            source_names=source_names,
            primary_source=primary_source,
            version=version,
            resume_after=resume_after,
        )
        self.queries[query.query_id] = query
        self._by_key.setdefault(key, []).append(query.query_id)
        return query

    def get(self, query_id: str) -> RosterQuery:
        try:
            return self.queries[query_id]
        except KeyError:
            raise InvalidContinuationToken("unknown roster query")

    def find_resumable(
        self, key: Any, version: Optional[bytes] = None
    ) -> Optional["RosterQuery"]:
        """Find a still-open query with the same search identity and version."""
        for query_id in reversed(self._by_key.get(key, [])):
            query = self.queries.get(query_id)
            if query is None or query.exhausted:
                continue
            if version is not None and query.version != to_bytes(version):
                continue
            return query
        return None

    def connection_lost(self, connection_id: Any) -> None:
        """Invalidate every continuation cookie issued on a connection.

        Queries survive so the same search can resume on a new
        connection at the last confirmed number.  No old cookie does.
        """
        for token, continuation in list(self._tokens.items()):
            if continuation.connection_id == connection_id:
                del self._tokens[token]

    def issue_token(
        self, query: RosterQuery, connection_id: Any
    ) -> bytes:
        token = secrets.token_bytes(24)
        self._tokens[token] = _Continuation(
            query=query, connection_id=connection_id
        )
        return token

    def consume_token(
        self, token: bytes, connection_id: Any
    ) -> RosterQuery:
        if not token:
            raise InvalidContinuationToken("empty continuation token")
        try:
            continuation = self._tokens.pop(token)
        except KeyError:
            raise InvalidContinuationToken(
                "continuation token is unknown or was already used"
            )
        if continuation.connection_id != connection_id:
            raise InvalidContinuationToken(
                "continuation token belongs to another connection"
            )
        query = continuation.query
        if self.queries.get(query.query_id) is not query:
            raise InvalidContinuationToken(
                "roster query is no longer available"
            )
        query.confirm_served()
        return query

    def drop_tokens(self, query: RosterQuery) -> None:
        for token, continuation in list(self._tokens.items()):
            if continuation.query is query:
                del self._tokens[token]

    def discard_query(self, query: RosterQuery) -> None:
        self.queries.pop(query.query_id, None)
        ids = self._by_key.get(query.key, [])
        if query.query_id in ids:
            ids.remove(query.query_id)
        if not ids:
            self._by_key.pop(query.key, None)
        self.drop_tokens(query)


# ---------------------------------------------------------------------------
# BER helpers for the LDAP controls
# ---------------------------------------------------------------------------


def _decode_sequence(control_value, allowed_lengths):
    raw = to_bytes(control_value)
    value, used = pureber.berDecodeObject(
        pureber.BERDecoderContext(), raw
    )
    if used != len(raw) or not isinstance(value, pureber.BERSequence):
        raise ValueError
    if len(value.data) not in allowed_lengths:
        raise ValueError
    return value


def decode_paged_results_control(
    controls,
) -> Optional[tuple[int, bytes]]:
    """Return ``(page_size, cookie)`` for an RFC 2696 request control."""
    if not controls:
        return None
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != PAGED_RESULTS_OID:
            continue
        try:
            value = _decode_sequence(control_value, (2,))
            size = value[0].value
            cookie = to_bytes(value[1].value)
            if not isinstance(size, int) or size < 0:
                raise ValueError
        except Exception:
            raise RosterProtocolError("malformed paged-results control")
        return size, cookie
    return None


def encode_paged_results_control(
    page_size: int, cookie: bytes = b""
) -> tuple:
    value = pureber.BERSequence(
        [pureber.BERInteger(page_size), pureber.BEROctetString(cookie)]
    )
    return (PAGED_RESULTS_OID, None, value.toWire())


def encode_resume_control(
    query_id: str, version: bytes, confirmed_after: Optional[bytes] = None
) -> tuple:
    value = pureber.BERSequence(
        [
            pureber.BEROctetString(query_id.encode("utf-8")),
            pureber.BEROctetString(version),
            pureber.BEROctetString(
                confirmed_after if confirmed_after is not None else b""
            ),
        ]
    )
    return (ROSTER_RESUME_OID, None, value.toWire())


def decode_resume_control(
    controls,
) -> Optional[tuple[str, bytes, Optional[bytes]]]:
    """Return ``(query_id, version, confirmed_after)`` if present."""
    if not controls:
        return None
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != ROSTER_RESUME_OID:
            continue
        try:
            value = _decode_sequence(control_value, (3,))
            return (
                to_bytes(value[0].value).decode("utf-8"),
                to_bytes(value[1].value),
                to_bytes(value[2].value) or None,
            )
        except Exception:
            raise RosterProtocolError("malformed roster resume control")
    return None


def encode_state_control(query: RosterQuery) -> tuple:
    query_id, version, confirmed_after = query.state_payload()
    value = pureber.BERSequence(
        [
            pureber.BEROctetString(query_id.encode("utf-8")),
            pureber.BEROctetString(version),
            pureber.BEROctetString(
                confirmed_after if confirmed_after is not None else b""
            ),
        ]
    )
    return (ROSTER_STATE_OID, None, value.toWire())


def decode_state_control(
    controls,
) -> Optional[tuple[str, bytes, Optional[bytes]]]:
    if not controls:
        return None
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != ROSTER_STATE_OID:
            continue
        try:
            value = _decode_sequence(control_value, (3,))
            return (
                to_bytes(value[0].value).decode("utf-8"),
                to_bytes(value[1].value),
                to_bytes(value[2].value) or None,
            )
        except Exception:
            raise RosterProtocolError("malformed roster state control")
    return None


def encode_unsettled_control(ranges: Iterable[UnsettledRange]) -> tuple:
    items = []
    for item in ranges:
        items.append(
            pureber.BERSequence(
                [
                    pureber.BEROctetString(query_id_bytes(item.query_id)),
                    pureber.BEROctetString(
                        item.start if item.start is not None else b""
                    ),
                    pureber.BEROctetString(
                        item.end if item.end is not None else b""
                    ),
                    pureber.BEROctetString(item.reason.encode("utf-8")),
                ]
            )
        )
    return (
        ROSTER_UNSETTLED_OID,
        None,
        pureber.BERSequence(items).toWire(),
    )


def decode_unsettled_control(controls) -> list[dict[str, bytes]]:
    if not controls:
        return []
    result = []
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != ROSTER_UNSETTLED_OID:
            continue
        try:
            value = _decode_sequence(control_value, (1,))
            for item in value.data:
                values = item.data
                result.append(
                    {
                        "query_id": to_bytes(values[0].value),
                        "start": to_bytes(values[1].value),
                        "end": to_bytes(values[2].value),
                        "reason": to_bytes(values[3].value),
                    }
                )
        except Exception:
            raise RosterProtocolError("malformed unsettled control")
    return result


def query_id_bytes(query_id: str) -> bytes:
    return query_id.encode("utf-8")


def encode_source_request_control(
    version: Optional[bytes], after: Optional[bytes], limit: int
) -> tuple:
    value = pureber.BERSequence(
        [
            pureber.BEROctetString(version if version is not None else b""),
            pureber.BEROctetString(after if after is not None else b""),
            pureber.BERInteger(limit),
        ]
    )
    return (ROSTER_SOURCE_REQUEST_OID, None, value.toWire())


def decode_source_request_control(controls):
    if not controls:
        return None
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != ROSTER_SOURCE_REQUEST_OID:
            continue
        try:
            value = _decode_sequence(control_value, (3,))
            return (
                to_bytes(value[0].value) or None,
                to_bytes(value[1].value) or None,
                value[2].value,
            )
        except Exception:
            raise RosterProtocolError(
                "malformed roster source request control"
            )
    return None


def encode_source_response_control(
    version: Optional[bytes], next_number: Optional[bytes], ended: bool
) -> tuple:
    value = pureber.BERSequence(
        [
            pureber.BEROctetString(version if version is not None else b""),
            pureber.BEROctetString(
                next_number if next_number is not None else b""
            ),
            pureber.BERBoolean(ended),
        ]
    )
    return (ROSTER_SOURCE_RESPONSE_OID, None, value.toWire())


def decode_source_response_control(controls):
    if not controls:
        return None
    for control in controls:
        control_type, _criticality, control_value = control
        if to_bytes(control_type) != ROSTER_SOURCE_RESPONSE_OID:
            continue
        try:
            value = _decode_sequence(control_value, (3,))
            return (
                to_bytes(value[0].value) or None,
                to_bytes(value[1].value) or None,
                bool(value[2].value),
            )
        except Exception:
            raise RosterProtocolError(
                "malformed roster source response control"
            )
    return None


def make_query_key(request) -> tuple:
    """Build the stable identity of a roster search.

    Page size and continuation state are excluded: they are negotiated
    separately.  Night shift and another shift therefore never share a
    cursor, even when they run at the same time.
    """
    attributes = tuple(
        sorted(to_bytes(attr).lower() for attr in request.attributes)
    )
    return (
        to_bytes(request.baseObject),
        request.scope,
        request.derefAliases,
        request.sizeLimit,
        request.timeLimit,
        request.typesOnly,
        request.filter.toWire(),
        attributes,
    )
