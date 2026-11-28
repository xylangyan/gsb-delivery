"""Durable, per-origin request budgeting for the arXiv API.

The regular :class:`arxiv.Client` delay is intentionally local: two clients in
two processes can each wait three seconds and still issue requests back to back
from the server's point of view.  This module provides the missing shared piece:

* at most one *sent* request per origin is in flight at a time;
* a completed request starts a per-origin interval (three seconds by default);
* a ``Retry-After`` response is persisted, so a restarted process keeps the
  same deferral boundary;
* a cancelled wait never consumes an upstream request;
* an unconfirmed sent request stays in flight until an explicit resolution
  (an upstream receipt or a manual operator record) is supplied.

State is kept in SQLite.  Point several clients/processes at the same database
file to share a budget.  All time comes from an injectable :class:`Clock`, which
makes the state machine deterministic in tests.

This module deliberately does not perform HTTP requests.  It only coordinates
when a caller may issue one.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit


DEFAULT_INTERVAL_SECONDS = 3.0
"""arXiv asks clients to make no more than one request every three seconds."""

_ENV_BUDGET_PATH = "ARXIV_REQUEST_BUDGET"


@runtime_checkable
class Clock(Protocol):
    """The time source used by :class:`RequestBudget`."""

    def time(self) -> float:
        """Return the current time as seconds since the Unix epoch."""
        ...

    def sleep(self, seconds: float, *, wake: threading.Event | None = None) -> None:
        """Wait for ``seconds``, returning early if ``wake`` is set."""
        ...


class SystemClock:
    """A clock backed by wall time, suitable for sharing state across processes.

    Wall time (rather than a monotonic counter) is used because a deferral
    boundary stored in SQLite must still mean the same thing after a restart.
    """

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float, *, wake: threading.Event | None = None) -> None:
        if seconds <= 0:
            return
        if wake is None:
            time.sleep(seconds)
        else:
            wake.wait(seconds)


class ManualClock:
    """A deterministic, thread-safe clock for tests.

    Time does not move on its own.  Drive it explicitly with :meth:`advance`,
    :meth:`advance_to` or :meth:`set_time`; blocked callers are notified.  A
    waiting :class:`RequestBudget` polls this clock rather than forcing it
    forward, so cancellation at an exact instant is reliable.

    Set ``auto_advance=True`` to make :meth:`sleep` move the clock directly to
    the requested deadline.  That mode is convenient for a single-threaded
    timeline but should not be used when another thread must act (for example,
    cancel) before the deadline.
    """

    def __init__(self, start: float = 0.0, *, auto_advance: bool = False) -> None:
        self._now = float(start)
        self._auto_advance = auto_advance
        self._condition = threading.Condition()

    @property
    def auto_advance(self) -> bool:
        """Whether :meth:`sleep` moves this clock forward by itself."""
        return self._auto_advance

    @property
    def now(self) -> float:
        return self.time()

    def time(self) -> float:
        with self._condition:
            return self._now

    def sleep(self, seconds: float, *, wake: threading.Event | None = None) -> None:
        if seconds <= 0:
            return
        if wake is not None and wake.is_set():
            return
        if self._auto_advance:
            self.advance(seconds)
            return
        deadline = self.time() + seconds
        with self._condition:
            while self._now < deadline:
                if wake is not None and wake.is_set():
                    return
                remaining = deadline - self._now
                # A short real timeout lets an external cancellation token
                # wake this sleeper without requiring a clock advance.
                self._condition.wait(timeout=min(0.01, remaining) if wake else None)
                if wake is not None and wake.is_set():
                    return

    def sleep_until(self, deadline: float, *, wake: threading.Event | None = None) -> None:
        self.sleep(max(0.0, deadline - self.time()), wake=wake)

    def advance(self, seconds: float) -> float:
        """Move time forward by ``seconds`` and wake every sleeper."""
        with self._condition:
            self._now += max(0.0, float(seconds))
            self._condition.notify_all()
            return self._now

    def advance_to(self, when: float) -> float:
        """Move time to ``when`` (never backwards) and wake sleepers."""
        with self._condition:
            self._now = max(self._now, float(when))
            self._condition.notify_all()
            return self._now

    def set_time(self, when: float) -> float:
        """Set the clock to ``when`` and wake sleepers."""
        with self._condition:
            self._now = float(when)
            self._condition.notify_all()
            return self._now

    def wake(self) -> None:
        """Wake sleepers without changing the time (used for cancellation)."""
        with self._condition:
            self._condition.notify_all()

    tick = advance
    jump = set_time
    run_until = advance_to


# Friendly aliases: the class is a test double regardless of the name callers
# prefer.
TestClock = ManualClock
FakeClock = ManualClock
VirtualClock = ManualClock
ControlledClock = ManualClock
DeterministicClock = ManualClock


class CancelledError(RuntimeError):
    """A waiting page or retry was cancelled before it reached the network."""


class RequestCancelled(CancelledError):
    """Alias with a more explicit name for request-level cancellation."""


class BudgetTimeout(TimeoutError):
    """A caller stopped waiting before the origin granted it a request slot."""


class LeaseLostError(RuntimeError):
    """The lease is no longer the active request for its origin."""


class UnknownInFlightError(RuntimeError):
    """An unconfirmed sent request must be resolved before more requests."""


class CancellationToken:
    """A cooperative cancellation signal that can be shared with callers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()


@dataclass(frozen=True)
class OriginState:
    """A snapshot of the durable budget for one origin."""

    origin: str
    not_before: float
    last_completed: float | None
    in_flight_id: str | None
    in_flight_job: str | None
    in_flight_page: str | None
    in_flight_started: float | None
    updated: float | None


@dataclass(frozen=True)
class RequestRecord:
    """One auditable acquisition attempt/page request.

    ``status`` is one of ``waiting``, ``granted``, ``sent``, ``completed``,
    ``failed``, ``uncertain``, ``cancelled``, ``timed_out`` or ``resolved``.
    Only records with a ``sent_at`` actually reached the upstream service.
    ``result_ids`` are the entries successfully parsed from that response.
    """

    request_id: str
    origin: str
    job_id: str
    page_key: str | None
    url: str | None
    status: str
    requested_at: float
    granted_at: float | None
    sent_at: float | None
    finished_at: float | None
    not_before: float | None
    retry_after: float | None
    status_code: int | None
    result_count: int
    result_ids: tuple[str, ...]
    detail: str | None

    @property
    def was_sent(self) -> bool:
        return self.sent_at is not None

    @property
    def was_fetched(self) -> bool:
        return self.status == "completed" and self.was_sent


@dataclass(frozen=True)
class Lease:
    """Permission to issue exactly one request for one origin.

    The lease is persisted as the origin's in-flight request as soon as it is
    granted.  Call :meth:`mark_sent` immediately before the network call, then
    :meth:`complete` once a response (of any status) has been received.  If the
    process dies after ``mark_sent`` and before ``complete``, the request stays
    in flight and blocks the origin until :meth:`RequestBudget.resolve_unknown`
    is given a credential or a manual record.
    """

    budget: "RequestBudget"
    request_id: str
    origin: str
    job_id: str
    page_key: str | None
    url: str | None
    granted_at: float

    def mark_sent(self) -> None:
        self.budget.mark_sent(self)

    def complete(
        self,
        *,
        status_code: int | None = None,
        retry_after: float | str | None = None,
        defer_until: float | None = None,
        result_count: int | None = None,
        result_ids: list[str] | tuple[str, ...] | None = None,
        detail: str | None = None,
        finished_at: float | None = None,
        failed: bool = False,
    ) -> None:
        self.budget.complete(
            self,
            status_code=status_code,
            retry_after=retry_after,
            defer_until=defer_until,
            result_count=result_count,
            result_ids=result_ids,
            detail=detail,
            finished_at=finished_at,
            failed=failed,
        )

    def fail(
        self,
        error: BaseException | str | None = None,
        *,
        retry_after: float | str | None = None,
        defer_until: float | None = None,
        status_code: int | None = None,
    ) -> None:
        self.budget.complete(
            self,
            status_code=status_code,
            retry_after=retry_after,
            defer_until=defer_until,
            detail=str(error) if error is not None else None,
            failed=True,
        )

    def mark_uncertain(self, reason: BaseException | str | None = None) -> None:
        self.budget.mark_uncertain(self, reason=reason)

    def abandon(self, reason: BaseException | str | None = None) -> None:
        self.budget.mark_uncertain(self, reason=reason)

    def release(self, reason: str | None = None) -> None:
        self.budget.release(self, reason=reason)

    def record_results(
        self,
        result_ids: list[str] | tuple[str, ...],
        *,
        result_count: int | None = None,
    ) -> None:
        self.budget.record_results(self, result_ids, result_count=result_count)


def origin_key(value: str) -> str:
    """Normalize a URL (or an explicit origin label) to a budget key."""
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        return parsed.netloc.lower() or value.lower()
    return value.rstrip("/").lower()


def parse_retry_after(value: str | int | float | None, *, now: float) -> float | None:
    """Convert a ``Retry-After`` header value into an absolute epoch deadline.

    Both forms from RFC 9110 are supported: a number of seconds and an
    HTTP-date.  A missing value returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return now + max(0.0, seconds)
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(now, when.timestamp())
    return now + max(0.0, seconds)


class HeaderMapping(Protocol):
    """The response-header interface needed by :func:`retry_after_from_headers`."""

    def get(self, name: str, default: str | None = None) -> str | None: ...


def _parse_absolute_or_seconds(value: str | int | float) -> float | None:
    """Parse a rate-limit reset value: epoch seconds, or an HTTP date."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()


def retry_after_from_headers(headers: HeaderMapping | None, *, now: float) -> float | None:
    """Extract the strongest known deferral deadline from response headers.

    ``Retry-After`` is relative (seconds or HTTP-date); ``X-RateLimit-Reset`` is
    an absolute epoch timestamp (or HTTP-date).  The latest deadline wins.
    """
    if headers is None:
        return None
    candidates: list[float] = []
    for name in ("Retry-After", "retry-after"):
        raw = headers.get(name)
        if raw is not None:
            parsed = parse_retry_after(raw, now=now)
            if parsed is not None:
                candidates.append(parsed)
    for name in ("X-RateLimit-Reset", "x-ratelimit-reset"):
        raw = headers.get(name)
        if raw is not None:
            parsed = _parse_absolute_or_seconds(raw)
            if parsed is not None:
                candidates.append(max(now, parsed))
    return max(candidates) if candidates else None


def retry_after_from_response(response: object, *, now: float) -> float | None:
    """Extract a deferral deadline from a requests-like response object.

    Header handling is primary.  A few ``requests`` test doubles attach the
    deadline directly (``retry_after``/``retry_at``), which is also honored.
    """
    headers = getattr(response, "headers", None)
    deadline = retry_after_from_headers(headers, now=now)
    direct = getattr(response, "retry_after", None)
    if direct is None:
        direct = getattr(response, "retry_at", None)
    if direct is not None:
        parsed = parse_retry_after(direct, now=now)
        if parsed is not None:
            deadline = parsed if deadline is None else max(deadline, parsed)
    return deadline


def _default_path() -> str:
    configured = os.environ.get(_ENV_BUDGET_PATH)
    if configured:
        return configured
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return str(base / "arxiv" / "request-budget.sqlite3")


class RequestBudget:
    """A durable per-origin request budget shared by clients and processes.

    Parameters
    ----------
    path:
        SQLite database path.  Defaults to ``$ARXIV_REQUEST_BUDGET`` or
        ``~/.cache/arxiv/request-budget.sqlite3``.  ``":memory:"`` creates an
        isolated in-memory budget for a single instance.
    clock:
        Injectable time source.  Defaults to :class:`SystemClock`.
    min_interval:
        Minimum seconds between the *end* of one request and the start of the
        next request for the same origin.  Defaults to three seconds, arXiv's
        published limit.
    poll_interval:
        How often a waiter rechecks durable state while another process owns
        the in-flight request.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Clock | None = None,
        min_interval: float = DEFAULT_INTERVAL_SECONDS,
        poll_interval: float = 0.05,
        grant_timeout: float = 30.0,
        interval_seconds: float | None = None,
        store_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if store_path is not None:
            path = store_path
        if interval_seconds is not None:
            min_interval = interval_seconds
        if min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        if grant_timeout < 0:
            raise ValueError("grant_timeout must be non-negative")
        self.clock: Clock = clock or SystemClock()
        self.min_interval = float(min_interval)
        self.poll_interval = max(0.001, float(poll_interval))
        self.grant_timeout = float(grant_timeout)
        self._condition = threading.Condition()
        self._waiters: dict[str, tuple[str, threading.Event]] = {}
        self._keepalive: sqlite3.Connection | None = None

        if path is None:
            path = _default_path()
        if str(path) == ":memory:":
            database = f"file:arxiv-budget-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self.path = database
            self._uri = True
            self._keepalive = self._connect()
        else:
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(path)
            self._uri = False
        self._initialize()

    def __repr__(self) -> str:
        return "{}(path={!r}, min_interval={!r})".format(
            _classname(self), self.path, self.min_interval
        )

    @classmethod
    def shared(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Clock | None = None,
        min_interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> "RequestBudget":
        """Construct a budget at the default shared location."""
        return cls(path, clock=clock, min_interval=min_interval)

    # ------------------------------------------------------------------
    # SQLite plumbing
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            uri=self._uri,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS origins (
                    origin TEXT PRIMARY KEY,
                    not_before REAL NOT NULL DEFAULT 0,
                    last_completed REAL,
                    in_flight_id TEXT,
                    in_flight_job TEXT,
                    in_flight_page TEXT,
                    in_flight_started REAL,
                    updated REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    page_key TEXT,
                    url TEXT,
                    status TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    granted_at REAL,
                    sent_at REAL,
                    finished_at REAL,
                    not_before REAL,
                    retry_after REAL,
                    status_code INTEGER,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    result_ids TEXT,
                    detail TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS requests_origin_idx ON requests(origin)")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_job_idx ON requests(job_id)")

    def close(self) -> None:
        """Close the in-memory keepalive connection, if any."""
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    def __enter__(self) -> "RequestBudget":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def now(self) -> float:
        """Return the budget clock's current time."""
        clock = self.clock
        reader = getattr(clock, "time", None) or getattr(clock, "now", None)
        if reader is None:
            raise TypeError("clock must provide time() or now")
        value = reader() if callable(reader) else reader
        return float(value)

    def state(self, origin: str) -> OriginState:
        """Return the current durable state for ``origin``."""
        key = origin_key(origin)
        rows = self._query("SELECT * FROM origins WHERE origin = ?", (key,))
        if not rows:
            return OriginState(key, 0.0, None, None, None, None, None, None)
        row = rows[0]
        return OriginState(
            origin=key,
            not_before=float(row["not_before"] or 0.0),
            last_completed=row["last_completed"],
            in_flight_id=row["in_flight_id"],
            in_flight_job=row["in_flight_job"],
            in_flight_page=row["in_flight_page"],
            in_flight_started=row["in_flight_started"],
            updated=row["updated"],
        )

    def next_allowed(self, origin: str) -> float:
        """Earliest time at which a new request could be granted."""
        state = self.state(origin)
        allowed = max(0.0, state.not_before)
        if state.last_completed is not None:
            allowed = max(allowed, float(state.last_completed) + self.min_interval)
        return allowed

    def in_flight(self, origin: str | None = None) -> list[RequestRecord]:
        """Return sent-but-unfinished request records.

        This includes requests left behind by a crashed process (``sent``) and
        requests whose outcome is explicitly unknown (``uncertain``).  Both
        continue to block their origin until resolved.
        """
        if origin is not None:
            rows = self._query(
                "SELECT * FROM requests WHERE origin = ? "
                "AND status IN ('sent', 'uncertain') ORDER BY requested_at",
                (origin_key(origin),),
            )
        else:
            rows = self._query(
                "SELECT * FROM requests WHERE status IN ('sent', 'uncertain') ORDER BY requested_at"
            )
        return [_record(row) for row in rows]

    def unresolved(self, origin: str | None = None) -> list[RequestRecord]:
        """Alias for :meth:`in_flight`."""
        return self.in_flight(origin)

    def records(
        self,
        *,
        origin: str | None = None,
        job_id: str | None = None,
        status: str | None = None,
        sent_only: bool = False,
        fetched_only: bool = False,
    ) -> list[RequestRecord]:
        """Query the audit log.

        ``fetched_only=True`` returns only responses whose entries were
        successfully parsed and yielded.  Filtering by ``job_id`` answers
        "which pages did this job actually retrieve?"; the returned records
        also carry the lease/request id that was granted for each page.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin_key(origin))
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if sent_only:
            clauses.append("sent_at IS NOT NULL")
        if fetched_only:
            # A record counts as fetched only when a request was actually sent
            # and at least one parsed entry came back. Failed / empty / non-2xx
            # responses are auditable but were not fetched records.
            clauses.append("status = 'completed' AND sent_at IS NOT NULL AND result_count > 0")
        sql = "SELECT * FROM requests"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid"
        return [_record(row) for row in self._query(sql, tuple(params))]

    def history(
        self,
        *,
        origin: str | None = None,
        job_id: str | None = None,
        status: str | None = None,
        sent_only: bool = False,
        fetched_only: bool = False,
    ) -> list[RequestRecord]:
        """Alias for :meth:`records`."""
        return self.records(
            origin=origin,
            job_id=job_id,
            status=status,
            sent_only=sent_only,
            fetched_only=fetched_only,
        )

    def request(self, request_id: str) -> RequestRecord | None:
        rows = self._query("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        return _record(rows[0]) if rows else None

    def used_credentials(
        self, *, origin: str | None = None, job_id: str | None = None
    ) -> list[RequestRecord]:
        """Return requests that were granted a slot (and their lease ids).

        This answers "which request/budget credential did this job use?".
        Cancelled-before-send and timed-out waits are excluded because they
        never consumed an upstream slot.
        """
        records = self.records(origin=origin, job_id=job_id)
        return [
            r
            for r in records
            if r.granted_at is not None and r.status not in ("cancelled", "timed_out")
        ]

    def leases(
        self, *, origin: str | None = None, job_id: str | None = None
    ) -> list[RequestRecord]:
        """Alias for :meth:`used_credentials`."""
        return self.used_credentials(origin=origin, job_id=job_id)

    def fetched(
        self, *, origin: str | None = None, job_id: str | None = None
    ) -> list[RequestRecord]:
        """Records actually retrieved from upstream (non-empty, successful)."""
        return self.records(origin=origin, job_id=job_id, fetched_only=True)

    def job_records(self, job_id: str, *, fetched_only: bool = False) -> list[RequestRecord]:
        """All audit records for ``job_id``."""
        return self.records(job_id=job_id, fetched_only=fetched_only)

    def records_for_job(self, job_id: str, *, fetched_only: bool = False) -> list[RequestRecord]:
        """Alias for :meth:`job_records`."""
        return self.job_records(job_id, fetched_only=fetched_only)

    def jobs(self, *, origin: str | None = None) -> list[str]:
        """Return distinct job ids present in the audit log."""
        if origin is None:
            rows = self._query("SELECT DISTINCT job_id FROM requests ORDER BY job_id")
        else:
            rows = self._query(
                "SELECT DISTINCT job_id FROM requests WHERE origin = ? ORDER BY job_id",
                (origin_key(origin),),
            )
        return [str(row["job_id"]) for row in rows]

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def acquire(
        self,
        origin: str,
        *,
        job_id: str | None = None,
        page_key: str | None = None,
        url: str | None = None,
        token: CancellationToken | None = None,
        timeout: float | None = None,
        wait: bool = True,
    ) -> Lease:
        """Wait for and atomically claim the origin's request slot.

        Cancellation and timeout only discard a *waiting* attempt; they never
        turn into an upstream request.  Set ``wait=False`` for a non-blocking
        probe, in which case :class:`BudgetTimeout` is raised when the origin is
        not currently grantable.
        """
        key = origin_key(origin)
        job = job_id or f"job-{uuid.uuid4().hex}"
        request_id = uuid.uuid4().hex
        started = self.now()
        deadline = started + timeout if timeout is not None else None

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO requests
                    (request_id, origin, job_id, page_key, url, status, requested_at)
                VALUES (?, ?, ?, ?, ?, 'waiting', ?)
                """,
                (request_id, key, job, page_key, url, started),
            )
            conn.execute(
                "INSERT OR IGNORE INTO origins (origin, not_before, updated) VALUES (?, 0, ?)",
                (key, started),
            )

        wake = threading.Event()
        with self._condition:
            self._waiters[request_id] = (job, wake)

        try:
            while True:
                cancelled = (token is not None and token.cancelled) or (
                    token is not None and token.event.is_set()
                )
                if cancelled:
                    self._cancel_request(request_id)
                    raise RequestCancelled(f"request for {key} was cancelled")

                lease, retry_at, blocked = self._try_grant(
                    request_id=request_id,
                    origin=key,
                    job_id=job,
                    page_key=page_key,
                    url=url,
                )
                if lease is not None:
                    return lease

                if not wait:
                    self._finish_waiting(request_id, "timed_out", "would block")
                    raise BudgetTimeout(f"origin {key} is not currently grantable")

                if deadline is not None and self.now() >= deadline:
                    self._finish_waiting(request_id, "timed_out", "wait timeout")
                    raise BudgetTimeout(f"origin {key} did not grant a request in time")

                if blocked:
                    # Another request is in flight.  A local completion wakes
                    # the waiter immediately; durable polling catches changes
                    # made by other processes.  This wait must not move a test
                    # clock that advances on sleep, unless a caller-supplied
                    # timeout requires time to pass.
                    wake.clear()
                    self._pause(
                        self.poll_interval,
                        token=token,
                        wake=wake,
                        advance_clock=deadline is not None,
                    )
                else:
                    assert retry_at is not None
                    if deadline is not None:
                        retry_at = min(retry_at, deadline)
                    wake.clear()
                    self._sleep(max(0.0, retry_at - self.now()), token=token, wake=wake)
        finally:
            with self._condition:
                self._waiters.pop(request_id, None)

    def try_acquire(
        self,
        origin: str,
        *,
        job_id: str | None = None,
        page_key: str | None = None,
        url: str | None = None,
        token: CancellationToken | None = None,
    ) -> Lease | None:
        """Non-blocking variant of :meth:`acquire`; returns ``None`` if denied."""
        try:
            return self.acquire(
                origin,
                job_id=job_id,
                page_key=page_key,
                url=url,
                token=token,
                wait=False,
            )
        except BudgetTimeout:
            return None

    def can_acquire(self, origin: str) -> bool:
        """Whether a slot is available at the current clock time.

        This accounts for reclaimable grants that were never sent (after
        ``grant_timeout``); it does not mutate state.
        """
        state = self.state(origin)
        in_flight = state.in_flight_id
        if in_flight is not None:
            holder = self.request(in_flight)
            reclaimable = (
                holder is not None
                and holder.sent_at is None
                and holder.status == "granted"
                and holder.granted_at is not None
                and self.now() - float(holder.granted_at) >= self.grant_timeout
            )
            if not reclaimable:
                return False
        return self.now() + 1e-9 >= self.next_allowed(origin)

    def _try_grant(
        self,
        *,
        request_id: str,
        origin: str,
        job_id: str,
        page_key: str | None,
        url: str | None,
    ) -> tuple[Lease | None, float | None, bool]:
        now = self.now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None or row["status"] != "waiting":
                status = row["status"] if row is not None else "missing"
                raise RequestCancelled(f"request {request_id} is {status}")

            state = conn.execute("SELECT * FROM origins WHERE origin = ?", (origin,)).fetchone()
            assert state is not None

            if state["in_flight_id"] is not None:
                # A grant that provably never reached the network can be
                # reclaimed after grant_timeout. A grant with sent_at set must
                # never be reclaimed by time alone: only a response or an
                # explicit credential/manual record may clear it.
                holder = conn.execute(
                    "SELECT * FROM requests WHERE request_id = ?",
                    (state["in_flight_id"],),
                ).fetchone()
                if (
                    holder is not None
                    and holder["sent_at"] is None
                    and holder["status"] == "granted"
                    and holder["granted_at"] is not None
                    and now - float(holder["granted_at"]) >= self.grant_timeout
                ):
                    conn.execute(
                        "UPDATE requests SET status = 'cancelled', finished_at = ?, "
                        "detail = 'abandoned unsent grant' WHERE request_id = ?",
                        (now, holder["request_id"]),
                    )
                    conn.execute(
                        "UPDATE origins SET in_flight_id = NULL, in_flight_job = NULL, "
                        "in_flight_page = NULL, in_flight_started = NULL, updated = ? "
                        "WHERE origin = ? AND in_flight_id = ?",
                        (now, origin, holder["request_id"]),
                    )
                    state = conn.execute(
                        "SELECT * FROM origins WHERE origin = ?", (origin,)
                    ).fetchone()
                    assert state is not None
                else:
                    return None, None, True

            allowed = max(0.0, float(state["not_before"] or 0.0))
            last_completed = state["last_completed"]
            if last_completed is not None:
                allowed = max(allowed, float(last_completed) + self.min_interval)

            if now + 1e-9 < allowed:
                return None, allowed, False

            conn.execute(
                """
                UPDATE requests
                   SET status = 'granted', granted_at = ?
                 WHERE request_id = ?
                """,
                (now, request_id),
            )
            conn.execute(
                """
                UPDATE origins
                   SET in_flight_id = ?, in_flight_job = ?, in_flight_page = ?,
                       in_flight_started = ?, updated = ?
                 WHERE origin = ?
                """,
                (request_id, job_id, page_key, now, now, origin),
            )
            return Lease(self, request_id, origin, job_id, page_key, url, now), None, False

    def _sleep(
        self,
        seconds: float,
        *,
        token: CancellationToken | None,
        wake: threading.Event,
    ) -> None:
        if seconds <= 0:
            return
        # Wake on either local budget changes or caller cancellation.
        combined = _CombinedEvent(wake, token.event) if token is not None else wake
        if isinstance(self.clock, ManualClock) and not self.clock.auto_advance:
            # Pure manual mode: do not move time. Poll in short real slices so
            # another thread can call `clock.advance(...)`, set the token, or
            # set `wake` and be observed promptly.
            deadline = self.now() + seconds
            while self.now() < deadline:
                if combined.is_set():
                    return
                combined.wait(min(0.01, max(0.0, deadline - self.now())))
            return
        self.clock.sleep(seconds, wake=combined)

    def _pause(
        self,
        seconds: float,
        *,
        token: CancellationToken | None,
        wake: threading.Event,
        advance_clock: bool = False,
    ) -> None:
        """Poll durable state without consuming/skipping test-clock time.

        A waiting acquisition is not itself a request, so a manual test clock
        should not jump forward merely because a competitor is in flight. Use
        a short wall-clock sleep here. ``SystemClock`` behaves identically
        because the system clock does not care how the wait is implemented.
        Callers with an explicit timeout pass ``advance_clock=True`` so the
        deadline is actually reachable in deterministic tests.
        """
        combined = _CombinedEvent(wake, token.event) if token is not None else wake
        if isinstance(self.clock, ManualClock):
            if advance_clock and self.clock.auto_advance:
                self.clock.sleep(seconds, wake=combined)
            else:
                # Pure manual mode: poll briefly so an external
                # `clock.advance(...)`, `token.cancel()` or `wake` is observed.
                combined.wait(seconds)
        else:
            self.clock.sleep(seconds, wake=combined)

    def _wake_local(self) -> None:
        with self._condition:
            for _, wake in self._waiters.values():
                wake.set()

    def _finish_waiting(self, request_id: str, status: str, detail: str | None) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE requests SET status = ?, finished_at = ?, detail = ? "
                "WHERE request_id = ? AND status = 'waiting'",
                (status, self.now(), detail, request_id),
            )

    def _cancel_request(self, request_id: str, detail: str | None = None) -> None:
        detail = detail or "cancelled"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return
            if row["status"] == "waiting":
                conn.execute(
                    "UPDATE requests SET status = 'cancelled', finished_at = ?, "
                    "detail = ? WHERE request_id = ?",
                    (self.now(), f"{detail} while waiting", request_id),
                )
            elif row["status"] == "granted" and row["sent_at"] is None:
                # Never sent: safe to return the slot without consuming it.
                conn.execute(
                    "UPDATE requests SET status = 'cancelled', finished_at = ?, "
                    "detail = ? WHERE request_id = ?",
                    (self.now(), f"{detail} before send", request_id),
                )
                conn.execute(
                    "UPDATE origins SET in_flight_id = NULL, in_flight_job = NULL, "
                    "in_flight_page = NULL, in_flight_started = NULL, updated = ? "
                    "WHERE origin = ? AND in_flight_id = ?",
                    (self.now(), row["origin"], request_id),
                )
        self._wake_local()

    # ------------------------------------------------------------------
    # Lease transitions
    # ------------------------------------------------------------------

    def mark_sent(self, lease: Lease, *, at: float | None = None) -> None:
        """Durably record that the request has been handed to the network."""
        when = self.now() if at is None else at
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (lease.request_id,)
            ).fetchone()
            if row is None or row["status"] not in ("granted", "sent"):
                raise LeaseLostError(f"lease {lease.request_id} is no longer active")
            conn.execute(
                "UPDATE requests SET status = 'sent', sent_at = COALESCE(sent_at, ?) "
                "WHERE request_id = ?",
                (when, lease.request_id),
            )
            conn.execute(
                "UPDATE origins SET updated = ? WHERE origin = ? AND in_flight_id = ?",
                (when, lease.origin, lease.request_id),
            )
        self._wake_local()

    def complete(
        self,
        lease: Lease,
        *,
        status_code: int | None = None,
        retry_after: float | str | None = None,
        defer_until: float | None = None,
        result_count: int | None = None,
        result_ids: list[str] | tuple[str, ...] | None = None,
        detail: str | None = None,
        finished_at: float | None = None,
        failed: bool = False,
    ) -> None:
        """Record a received response and persist any deferral boundary.

        Completing a stale callback is safe: the new boundary is combined with
        the durable one using ``max``, so it can never shorten a later,
        already-confirmed pause.
        """
        when = self.now() if finished_at is None else finished_at
        candidate: float | None = None
        if defer_until is not None:
            candidate = max(candidate or 0.0, float(defer_until))
        if retry_after is not None:
            parsed = parse_retry_after(retry_after, now=when)
            # "Retry-After: 0" (or an already-past HTTP date) means "no further
            # deferral"; it must not manufacture a boundary at the callback's
            # own arrival time.
            if parsed is not None and parsed > when:
                candidate = max(candidate or 0.0, parsed)
        retry_seconds = max(0.0, candidate - when) if candidate is not None else None
        ids = tuple(result_ids or ())
        if result_count is None and result_ids is not None:
            result_count = len(ids)
        outcome = "failed" if failed else "completed"

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (lease.request_id,)
            ).fetchone()

            if row is not None and row["status"] != "cancelled":
                conn.execute(
                    """
                    UPDATE requests
                       SET status = ?,
                           sent_at = COALESCE(sent_at, ?),
                           finished_at = ?,
                           not_before = CASE
                               WHEN ? IS NULL THEN not_before
                               WHEN not_before IS NULL THEN ?
                               ELSE MAX(not_before, ?)
                           END,
                           retry_after = CASE
                               WHEN ? IS NULL THEN retry_after
                               WHEN retry_after IS NULL THEN ?
                               ELSE MAX(retry_after, ?)
                           END,
                           status_code = COALESCE(?, status_code),
                           result_count = CASE
                               WHEN ? IS NULL THEN result_count ELSE ?
                           END,
                           result_ids = CASE
                               WHEN ? = '' THEN result_ids ELSE ?
                           END,
                           detail = COALESCE(?, detail)
                     WHERE request_id = ?
                    """,
                    (
                        outcome,
                        when,
                        when,
                        candidate,
                        candidate,
                        candidate,
                        retry_seconds,
                        retry_seconds,
                        retry_seconds,
                        status_code,
                        result_count,
                        result_count,
                        ",".join(ids),
                        ",".join(ids),
                        detail,
                        lease.request_id,
                    ),
                )

            state = conn.execute(
                "SELECT * FROM origins WHERE origin = ?", (lease.origin,)
            ).fetchone()
            if state is not None:
                durable_not_before = float(state["not_before"] or 0.0)
                if candidate is not None:
                    durable_not_before = max(durable_not_before, candidate)
                if state["in_flight_id"] == lease.request_id:
                    # Only the request that still owns the slot may complete it.
                    conn.execute(
                        """
                        UPDATE origins
                           SET not_before = ?,
                               last_completed = ?,
                               in_flight_id = NULL,
                               in_flight_job = NULL,
                               in_flight_page = NULL,
                               in_flight_started = NULL,
                               updated = ?
                         WHERE origin = ?
                        """,
                        (durable_not_before, when, when, lease.origin),
                    )
                else:
                    # A late callback from an older request may only extend the
                    # pause, never clear a newer in-flight request or shorten a
                    # newer deadline.
                    conn.execute(
                        "UPDATE origins SET not_before = ?, updated = ? WHERE origin = ?",
                        (durable_not_before, when, lease.origin),
                    )
        self._wake_local()

    def release(self, lease: Lease, *, reason: str | None = None) -> None:
        """Release a granted-but-never-sent slot."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (lease.request_id,)
            ).fetchone()
            if row is None:
                return
            if row["sent_at"] is not None:
                # Once sent, only a response or an explicit resolution may
                # close the request.
                raise UnknownInFlightError(
                    f"request {lease.request_id} was sent and must be completed "
                    "or resolved explicitly"
                )
            if row["status"] != "cancelled":
                conn.execute(
                    "UPDATE requests SET status = 'cancelled', finished_at = ?, "
                    "detail = COALESCE(?, detail) WHERE request_id = ?",
                    (self.now(), reason, lease.request_id),
                )
            conn.execute(
                "UPDATE origins SET in_flight_id = NULL, in_flight_job = NULL, "
                "in_flight_page = NULL, in_flight_started = NULL, updated = ? "
                "WHERE origin = ? AND in_flight_id = ?",
                (self.now(), lease.origin, lease.request_id),
            )
        self._wake_local()

    def mark_uncertain(self, lease: Lease, *, reason: BaseException | str | None = None) -> None:
        """Leave a sent request in flight after losing its outcome.

        This is the crash/connection-loss path.  No timeout ever calls it
        automatically, and it never frees the origin's slot.
        """
        detail = str(reason) if reason is not None else "outcome unknown"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (lease.request_id,)
            ).fetchone()
            if row is None:
                return
            if row["sent_at"] is None:
                raise UnknownInFlightError(
                    f"request {lease.request_id} was never sent; use release()"
                )
            conn.execute(
                "UPDATE requests SET status = 'uncertain', detail = ? WHERE request_id = ?",
                (detail, lease.request_id),
            )
            conn.execute(
                "UPDATE origins SET updated = ? WHERE origin = ? AND in_flight_id = ?",
                (self.now(), lease.origin, lease.request_id),
            )
        self._wake_local()

    def record_results(
        self,
        lease: Lease,
        result_ids: list[str] | tuple[str, ...],
        *,
        result_count: int | None = None,
    ) -> None:
        """Record which entries a completed page actually returned."""
        ids = tuple(result_ids)
        count = len(ids) if result_count is None else result_count
        with self._transaction() as conn:
            conn.execute(
                "UPDATE requests SET result_count = ?, result_ids = ? WHERE request_id = ?",
                (count, ",".join(ids), lease.request_id),
            )

    # ------------------------------------------------------------------
    # Cancellation and manual resolution
    # ------------------------------------------------------------------

    def cancel(
        self,
        *,
        job_id: str | None = None,
        request_id: str | None = None,
        origin: str | None = None,
        reason: str = "cancelled",
    ) -> int:
        """Cancel waiting requests without treating them as upstream traffic.

        Returns the number of waiting/granted-unsent requests cancelled.  An
        already-sent request is never silently discarded; use
        :meth:`resolve_unknown` once its outcome is known.
        """
        clauses = ["status IN ('waiting', 'granted')", "sent_at IS NULL"]
        params: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if request_id is not None:
            clauses.append("request_id = ?")
            params.append(request_id)
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin_key(origin))
        sql = "SELECT * FROM requests WHERE " + " AND ".join(clauses)
        rows = self._query(sql, tuple(params))
        count = 0
        for row in rows:
            self._cancel_request(str(row["request_id"]), reason)
            count += 1
        self._wake_local()
        return count

    cancel_job = cancel
    cancel_request = cancel

    def resolve_unknown(
        self,
        origin: str,
        *,
        request_id: str | None = None,
        credential: str | None = None,
        manual_record: str | bool | None = None,
        completed_at: float | None = None,
        not_before: float | None = None,
        detail: str | None = None,
    ) -> RequestRecord:
        """Explicitly close a sent request whose outcome was lost.

        Time alone is never enough: callers must provide an upstream
        ``credential``/receipt, or a ``manual_record`` from an operator.  This
        is the only path that frees an ``uncertain`` in-flight slot.
        """
        if not credential and not manual_record:
            raise UnknownInFlightError(
                "an upstream credential or manual record is required to resolve "
                "an unknown in-flight request"
            )
        key = origin_key(origin)
        clauses = ["origin = ?", "status IN ('sent', 'uncertain')"]
        params: list[Any] = [key]
        if request_id is not None:
            clauses.append("request_id = ?")
            params.append(request_id)
        rows = self._query(
            "SELECT * FROM requests WHERE " + " AND ".join(clauses) + " ORDER BY requested_at",
            tuple(params),
        )
        if not rows:
            raise UnknownInFlightError(f"no unresolved sent request for origin {key}")
        row = rows[0]
        rid = str(row["request_id"])
        when = self.now() if completed_at is None else float(completed_at)
        evidence = credential or str(manual_record)
        with self._transaction() as conn:
            conn.execute(
                "UPDATE requests SET status = 'resolved', finished_at = ?, detail = ? "
                "WHERE request_id = ?",
                (when, detail or f"resolved by {evidence}", rid),
            )
            state = conn.execute("SELECT * FROM origins WHERE origin = ?", (key,)).fetchone()
            if state is not None:
                durable = max(
                    float(state["not_before"] or 0.0),
                    float(not_before) if not_before is not None else 0.0,
                )
                if state["in_flight_id"] == rid:
                    conn.execute(
                        "UPDATE origins SET not_before = ?, last_completed = ?, "
                        "in_flight_id = NULL, in_flight_job = NULL, "
                        "in_flight_page = NULL, in_flight_started = NULL, updated = ? "
                        "WHERE origin = ?",
                        (durable, when, when, key),
                    )
                else:
                    conn.execute(
                        "UPDATE origins SET not_before = ?, updated = ? WHERE origin = ?",
                        (durable, when, key),
                    )
        self._wake_local()
        return self.request(rid)  # type: ignore[return-value]

    confirm_unknown = resolve_unknown
    recover_unknown = resolve_unknown
    release_unknown = resolve_unknown
    resolve_in_flight = resolve_unknown
    resolve = resolve_unknown
    lease = acquire
    wait_for_lease = acquire
    claim = try_acquire

    def pause(
        self,
        origin: str,
        *,
        until: float | None = None,
        retry_after: float | str | None = None,
        now: float | None = None,
    ) -> float:
        """Persist a deferral boundary directly (for operator/test use).

        The stored boundary only ever extends: a shorter request can never
        shrink a later pause.  Returns the resulting absolute deadline.
        """
        key = origin_key(origin)
        when = self.now() if now is None else float(now)
        candidate = 0.0
        if until is not None:
            candidate = max(candidate, float(until))
        if retry_after is not None:
            parsed = parse_retry_after(retry_after, now=when)
            if parsed is not None and parsed > when:
                candidate = max(candidate, parsed)
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO origins (origin, not_before, updated) VALUES (?, 0, ?)",
                (key, when),
            )
            state = conn.execute(
                "SELECT not_before FROM origins WHERE origin = ?", (key,)
            ).fetchone()
            assert state is not None
            durable = max(float(state["not_before"] or 0.0), candidate)
            conn.execute(
                "UPDATE origins SET not_before = ?, updated = ? WHERE origin = ?",
                (durable, when, key),
            )
        self._wake_local()
        return durable

    defer = pause
    set_not_before = pause

    def note_response(
        self,
        origin: str,
        *,
        retry_after: float | str | None = None,
        defer_until: float | None = None,
        now: float | None = None,
    ) -> float:
        """Alias for :meth:`pause` using response-style arguments."""
        return self.pause(
            origin,
            until=defer_until,
            retry_after=retry_after,
            now=now,
        )


class _CombinedEvent:
    """A cancellation-aware stand-in for ``threading.Event``.

    ``Clock.sleep`` implementations use either ``wait`` (system clock) or
    ``is_set`` (manual clock), so both are provided.  No background thread is
    needed: the underlying waits are already bounded.
    """

    def __init__(self, first: threading.Event, second: threading.Event) -> None:
        self._first = first
        self._second = second

    def is_set(self) -> bool:
        return self._first.is_set() or self._second.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        # Poll in short slices so either source event can interrupt promptly.
        remaining = timeout
        while remaining is None or remaining > 0:
            slice_timeout = 0.01 if remaining is None else min(0.01, remaining)
            if self._first.wait(slice_timeout):
                return True
            if self._second.is_set():
                return True
            if remaining is not None:
                remaining -= slice_timeout
        return self.is_set()


def _record(row: Mapping[str, Any]) -> RequestRecord:
    ids = str(row["result_ids"] or "")
    return RequestRecord(
        request_id=str(row["request_id"]),
        origin=str(row["origin"]),
        job_id=str(row["job_id"]),
        page_key=row["page_key"],
        url=row["url"],
        status=str(row["status"]),
        requested_at=float(row["requested_at"]),
        granted_at=row["granted_at"],
        sent_at=row["sent_at"],
        finished_at=row["finished_at"],
        not_before=row["not_before"],
        retry_after=row["retry_after"],
        status_code=row["status_code"],
        result_count=int(row["result_count"] or 0),
        result_ids=tuple(item for item in ids.split(",") if item),
        detail=row["detail"],
    )


def _classname(o: object) -> str:
    return "arxiv.{}".format(o.__class__.__qualname__)
