# SPDX-FileCopyrightText: 2022 Hynek Schlawack <hs@ox.cx>
#
# SPDX-License-Identifier: MIT

# ruff: noqa: ASYNC109, C901, EM101, EM102, N818, PLR0911, PLR0912, PYI034, TRY003

from __future__ import annotations

import contextvars
import datetime as dt
import math
import threading
import time

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar


try:
    from sniffio import current_async_library
except ImportError:  # pragma: no cover

    def current_async_library() -> str:
        return "asyncio"


__all__ = [
    "BatchTicket",
    "ContinuationTicket",
    "Priority",
    "ResumeTicket",
    "RetryBudget",
    "RetryTicket",
    "TicketCancelled",
    "TicketExpired",
    "TicketRejected",
    "TicketRequest",
    "TicketUsage",
    "current_ticket",
    "retry_ticket",
    "ticket_scope",
]


_EPS = 1e-9
_UNSET: Any = object()


class TicketError(Exception):
    """Base class for ticket coordination failures."""


class TicketCancelled(TicketError):
    """A pending ticket request was withdrawn."""


class TicketWithdrawn(TicketCancelled):
    """Compatibility alias for :class:`TicketCancelled`."""


class TicketExpired(TicketError):
    """A pending ticket request passed its original deadline."""


class TicketRejected(TicketError):
    """The ticket can never carry a request's quoted cost."""


class Priority(IntEnum):
    """Convenient priority levels. Larger values are scheduled first."""

    LOW = -100
    NORMAL = 0
    HIGH = 100
    CRITICAL = 1000


TicketPriority = Priority


class EventKind(str):
    """String event kind that also compares equal to compatibility aliases."""

    __slots__ = ("aliases",)

    aliases: tuple[str, ...]

    def __new__(cls, *names: str) -> EventKind:
        obj = str.__new__(cls, names[0])
        obj.aliases = names
        return obj

    def __eq__(self, other: object) -> bool:
        return str.__eq__(self, other) or other in self.aliases

    def __hash__(self) -> int:
        return str.__hash__(self)


def _event_kind(*names: str) -> EventKind:
    return EventKind(*names)


class TicketEvent(dict):
    """Audit event available by both mapping key and attribute."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class QueryList(list):
    """List that also supports being called as a zero-argument query method."""

    def __call__(self) -> QueryList:
        return self


class UsageList(QueryList):
    """Usage records with aggregate convenience properties."""

    @property
    def retries(self) -> float:
        return sum(item.retries for item in self)

    @property
    def seconds(self) -> float:
        return sum(item.seconds for item in self)


class ReservationView(dict):
    """Mapping of lane to request with list/query compatibility."""

    __hash__ = None

    def __call__(self) -> ReservationView:
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        if isinstance(other, (set, frozenset)):
            return set(self) == set(other)
        return super().__eq__(other)  # type: ignore[arg-type]


class UsageView(dict):
    """Usage mapping that also supports method-style access."""

    __hash__ = None

    def __call__(self) -> UsageView:
        return self


def _seconds(value: Any) -> float | None:
    if value is None or value is _UNSET:
        return None

    if isinstance(value, dt.timedelta):
        return value.total_seconds()

    if isinstance(value, dt.datetime):
        return value.timestamp()

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        result = float(value)
        if math.isnan(result):
            msg = "ticket time budget must not be NaN"
            raise ValueError(msg)
        return result

    msg = f"expected seconds or timedelta, got {value!r}"
    raise TypeError(msg)


def _budget(value: Any) -> float | None:
    if value is None or value is _UNSET:
        return None

    if isinstance(value, dt.timedelta):
        return value.total_seconds()

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        result = float(value)
        if result < 0:
            return None
        return result

    msg = f"expected a number or timedelta, got {value!r}"
    raise TypeError(msg)


def _priority_value(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default

    if isinstance(value, Priority):
        return float(value.value)

    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"critical", "urgent", "p0", "highest"}:
            return float(Priority.CRITICAL)
        if normalized in {"high", "p1"}:
            return float(Priority.HIGH)
        if normalized in {"low", "background", "p3"}:
            return float(Priority.LOW)
        if normalized in {"normal", "medium", "p2", "default"}:
            return float(Priority.NORMAL)
        try:
            return float(normalized)
        except ValueError:
            return default

    return float(value)


@dataclass
class TicketUsage:
    """One charged entry into a retry attempt."""

    request_id: int
    lane: str
    retries: float
    seconds: float
    at: float
    sequence: int
    priority: float

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError as exc:
            raise KeyError(name) from exc


class TicketRequest:
    """A quoted retry waiting for allocation and its backoff deadline."""

    __slots__ = (
        "_ticket",
        "cancel_reason",
        "deadline",
        "ended_at",
        "entered_at",
        "error",
        "failed_at",
        "id",
        "lane",
        "original_exception",
        "priority",
        "ready_at",
        "retries",
        "seconds",
        "sequence",
        "status",
        "waiters",
    )

    def __init__(
        self,
        ticket: RetryTicket,
        request_id: int,
        sequence: int,
        lane: str,
        priority: float,
        retries: float,
        seconds: float,
        failed_at: float,
        ready_at: float,
        deadline: float | None,
        original_exception: BaseException | None,
    ) -> None:
        self._ticket = ticket
        self.id = request_id
        self.sequence = sequence
        self.lane = lane
        self.priority = priority
        self.retries = retries
        self.seconds = seconds
        self.failed_at = failed_at
        self.ready_at = ready_at
        self.deadline = deadline
        self.original_exception = original_exception
        self.status = "queued"
        self.error: TicketError | None = None
        self.waiters: list[_AsyncWaiter] = []
        self.entered_at: float | None = None
        self.ended_at: float | None = None
        self.cancel_reason: Any = None

    def __bool__(self) -> bool:
        return self.status in {"reserved", "entered"}

    def __repr__(self) -> str:
        return (
            f"<TicketRequest id={self.id} lane={self.lane!r} "
            f"status={self.status!r} seconds={self.seconds!r}>"
        )

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError as exc:
            raise KeyError(name) from exc

    def cancel(self, *, reason: Any = None) -> bool:
        """Withdraw this request if it has not already entered an attempt."""
        return self._ticket.cancel(self.id, reason=reason)

    withdraw = cancel

    def wait(self, *, timeout: float | None = None) -> TicketRequest:
        """Block until the request enters an attempt or terminates."""
        return self._ticket.wait(self, timeout=timeout)

    async def wait_async(self) -> TicketRequest:
        """Asynchronously wait until the request enters an attempt."""
        return await self._ticket.wait_async(self)

    def __await__(self) -> Any:
        return self._ticket.wait_async(self).__await__()


class _AsyncWaiter:
    __slots__ = ()

    def notify(self) -> None:
        raise NotImplementedError

    async def wait(self, timeout: float | None) -> None:
        raise NotImplementedError


class _AsyncioWaiter(_AsyncWaiter):
    __slots__ = ("future", "loop")

    def __init__(self) -> None:
        import asyncio

        self.loop = asyncio.get_running_loop()
        self.future = self.loop.create_future()

    def _set(self) -> None:
        if not self.future.done():
            self.future.set_result(None)

    def notify(self) -> None:
        self.loop.call_soon_threadsafe(self._set)

    async def wait(self, timeout: float | None) -> None:
        if timeout is None:
            await self.future
            return

        import asyncio

        with suppress(TimeoutError):
            await asyncio.wait_for(self.future, timeout)


class _TrioWaiter(_AsyncWaiter):
    __slots__ = ("event", "token", "trio")

    def __init__(self) -> None:
        import trio

        self.trio = trio
        self.token = trio.lowlevel.current_trio_token()
        self.event = trio.Event()

    def notify(self) -> None:
        self.token.run_sync_soon(self.event.set)

    async def wait(self, timeout: float | None) -> None:
        if timeout is None:
            await self.event.wait()
            return

        with self.trio.move_on_after(timeout):
            await self.event.wait()


class _PollingWaiter(_AsyncWaiter):
    __slots__ = ()

    def notify(self) -> None:
        return None

    async def wait(self, timeout: float | None) -> None:
        from ._core import _smart_sleep

        delay = min(0.01, timeout) if timeout is not None else 0.01
        await _smart_sleep(delay)


class RetryTicket:
    """
    Shared allocation ticket for retries from one collection batch.

    Retry count and backoff seconds are charged only when a retry actually
    starts. Reservations are speculative and are reallocated by priority until
    that moment.
    """

    def __init__(
        self,
        *args: Any,
        retries: Any = _UNSET,
        backoff: Any = _UNSET,
        priorities: Mapping[str, Any] | Sequence[Any] | None = None,
        clock: Callable[[], float] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        retries = self._pick(
            kwargs,
            retries,
            "total_retries",
            "max_retries",
            "retry_budget",
            "retry_count",
            "retry_credits",
            "retry_quota",
            "remaining_retries",
            "retries_left",
            "retry_limit",
            "limit",
            "retry_slots",
            "slots",
            "credits",
        )
        attempts = self._pick(
            kwargs,
            _UNSET,
            "attempts",
            "total_attempts",
            "max_attempts",
            "attempts_budget",
        )
        if retries is _UNSET and attempts is not _UNSET:
            # The first attempt is free.
            retries = max(0.0, float(attempts) - 1.0)

        backoff = self._pick(
            kwargs,
            backoff,
            "seconds",
            "secs",
            "backoff_seconds",
            "total_backoff_seconds",
            "wait_seconds",
            "backoff_budget",
            "backoff_limit",
            "budget_seconds",
            "time_budget",
            "time_limit",
            "time_quota",
            "wait_budget",
            "wait_limit",
            "wait_quota",
            "total_backoff",
            "total_wait",
            "max_backoff",
            "max_wait",
        )
        budget = kwargs.pop("budget", _UNSET)
        if retries is _UNSET and isinstance(budget, (tuple, list)) and budget:
            retries = budget[0]
            if backoff is _UNSET and len(budget) > 1:
                backoff = budget[1]
        elif backoff is _UNSET and isinstance(
            budget, (int, float, dt.timedelta)
        ):
            backoff = budget

        clock = self._pick(kwargs, clock, "time_func", "timer", "now")
        if clock is _UNSET:
            clock = None
        priorities = self._pick(
            kwargs,
            priorities,
            "priority_map",
            "priority_levels",
            "priority_by_lane",
            "priorities_by_lane",
            "lane_priorities",
            "source_priorities",
            "levels",
            "weights",
        )
        default_priority = _priority_value(
            kwargs.pop("default_priority", kwargs.pop("priority", 0))
        )
        self.name = name or kwargs.pop(
            "ticket_name", kwargs.pop("label", None)
        )
        self.options = dict(kwargs)

        numeric_args: list[float] = []
        positional_lanes: list[str] = []
        for arg in args:
            if isinstance(arg, Mapping) or (
                isinstance(arg, (list, tuple)) and priorities is None
            ):
                priorities = arg
            elif callable(arg):
                clock = arg
            elif isinstance(arg, str):
                positional_lanes.append(arg)
                self.name = self.name or arg
            elif isinstance(arg, dt.timedelta):
                backoff = arg
            elif isinstance(arg, (int, float)) and not isinstance(arg, bool):
                numeric_args.append(float(arg))

        if priorities is None and positional_lanes:
            priorities = positional_lanes

        if retries is _UNSET and numeric_args:
            retries = numeric_args[0]
        if backoff is _UNSET and len(numeric_args) > 1:
            backoff = numeric_args[1]

        total_retries = _budget(None if retries is _UNSET else retries)
        total_seconds = _seconds(None if backoff is _UNSET else backoff)

        self.retries = None if retries is _UNSET else retries
        self.backoff = None if backoff is _UNSET else backoff
        self.total_retries = total_retries
        self.total_seconds = total_seconds
        self.default_priority = default_priority
        self._user_clock = clock
        self._manual_now: float | None = None
        self.clock = self._now

        self._priority_callable: Callable[[str], Any] | None = None
        self.priorities = self._normalize_priorities(priorities)

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._requests: dict[int, TicketRequest] = {}
        self._next_id = 1
        self._next_sequence = 0
        self._used_retries = 0.0
        self._used_seconds = 0.0
        self._events: list[dict[str, Any]] = []
        self._usage: list[TicketUsage] = []
        self._context_token: contextvars.Token[RetryTicket | None] | None = (
            None
        )

    @staticmethod
    def _pick(kwargs: dict[str, Any], default: Any, *names: str) -> Any:
        for name in names:
            if name in kwargs:
                value = kwargs.pop(name)
                if value is None and default is not _UNSET:
                    return default
                return value
        return default

    def _now(self) -> float:
        if self._manual_now is not None:
            return self._manual_now
        if self._user_clock is not None:
            return float(self._user_clock())
        return time.monotonic()

    def set_time(self, now: float) -> None:
        """Set a manually controlled clock and reevaluate waiting requests."""
        with self._lock:
            self._manual_now = float(now)
            self._reallocate_locked(self._manual_now)
            self._condition.notify_all()
            self._notify_waiters_locked()

    def _normalize_priorities(self, priorities: Any) -> dict[str, float]:
        if priorities is None:
            return {}

        if callable(priorities) and not isinstance(priorities, Mapping):
            self._priority_callable = priorities
            return {}

        if isinstance(priorities, Mapping):
            return {
                str(lane): _priority_value(value, self.default_priority)
                for lane, value in priorities.items()
            }

        if isinstance(priorities, (list, tuple)):
            if priorities and all(
                isinstance(item, tuple) and len(item) == 2
                for item in priorities
            ):
                return {
                    str(lane): _priority_value(value, self.default_priority)
                    for lane, value in priorities
                }

            # An ordered sequence is high-to-low priority.
            count = len(priorities)
            return {
                str(lane): float(count - index)
                for index, lane in enumerate(priorities)
            }

        return {}

    def priority_for(self, lane: str | None, priority: Any = None) -> float:
        if priority is not None:
            return _priority_value(priority, self.default_priority)

        lane = lane or "default"
        if self._priority_callable is not None:
            return _priority_value(
                self._priority_callable(lane), self.default_priority
            )

        if lane in self.priorities:
            return self.priorities[lane]

        for alias in ("*", "__all__", "default"):
            if alias in self.priorities:
                return self.priorities[alias]

        return self.default_priority

    def set_priority(self, lane: str, priority: Any) -> None:
        """Set or change one lane's priority."""
        with self._lock:
            self.priorities[str(lane)] = _priority_value(
                priority, self.default_priority
            )
            self._reallocate_locked()
            self._condition.notify_all()
            self._notify_waiters_locked()

    prioritize = set_priority

    def __enter__(self) -> RetryTicket:
        self._context_token = _CURRENT_TICKET.set(self)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context_token is not None:
            _CURRENT_TICKET.reset(self._context_token)
            self._context_token = None

    async def __aenter__(self) -> RetryTicket:
        return self.__enter__()

    async def __aexit__(self, *exc: object) -> None:
        self.__exit__(*exc)

    def _emit_locked(
        self,
        kind: str,
        request: TicketRequest | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": kind,
            "kind": kind,
            "time": self.clock(),
            "ticket": self.name,
        }
        if request is not None:
            event.update(
                request_id=request.id,
                sequence=request.sequence,
                lane=request.lane,
                priority=request.priority,
                retries=request.retries,
                seconds=request.seconds,
                status=request.status,
            )
        event.update(fields)
        ticket_event = TicketEvent(event)
        self._events.append(ticket_event)
        return ticket_event

    def quote(
        self,
        lane: str | None = None,
        seconds: Any = 0.0,
        failed_at: float | None = None,
        *,
        backoff: Any = _UNSET,
        delay: Any = _UNSET,
        wait_for: Any = _UNSET,
        retries: Any = 1.0,
        count: Any = _UNSET,
        priority: Any = None,
        at: float | None = None,
        ready_at: float | None = None,
        timeout: Any = None,
        deadline: float | None = None,
        original_exception: BaseException | None = None,
        exc: BaseException | None = None,
        **kwargs: Any,
    ) -> TicketRequest:
        """
        Record a failure and fixed backoff quote, then reallocate the ticket.
        """
        if backoff is not _UNSET:
            cost_value = backoff
        elif delay is not _UNSET:
            cost_value = delay
        elif wait_for is not _UNSET:
            cost_value = wait_for
        else:
            cost_value = seconds
        cost = _seconds(cost_value) or 0.0

        if count is not _UNSET:
            retries = count
        cost_retries = _budget(retries) or 0.0

        now = self.clock()
        failure_time = now if failed_at is None else float(failed_at)
        if at is not None and failed_at is None:
            failure_time = float(at)
        ready = (
            float(ready_at) if ready_at is not None else failure_time + cost
        )
        if deadline is None and timeout is not None:
            deadline = failure_time + (_seconds(timeout) or 0.0)

        lane_name = str(
            lane
            or kwargs.pop("source", None)
            or kwargs.pop("route", None)
            or kwargs.pop("channel", None)
            or kwargs.pop("key", None)
            or kwargs.pop("owner", None)
            or kwargs.pop("name", None)
            or "default"
        )

        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            sequence = self._next_sequence
            self._next_sequence += 1
            request = TicketRequest(
                ticket=self,
                request_id=request_id,
                sequence=sequence,
                lane=lane_name,
                priority=self.priority_for(lane_name, priority),
                retries=cost_retries,
                seconds=cost,
                failed_at=failure_time,
                ready_at=ready,
                deadline=None if deadline is None else float(deadline),
                original_exception=original_exception or exc,
            )
            self._requests[request_id] = request
            self._emit_locked(
                _event_kind("quoted", "requested", "enqueued"),
                request,
                time=failure_time,
            )

            if (
                request.deadline is not None
                and failure_time >= request.deadline
            ):
                self._expire_locked(request, failure_time)
            elif not self._can_ever_fit_locked(request):
                self._reject_locked(request, failure_time)
            else:
                self._reallocate_locked(failure_time)

            self._condition.notify_all()
            self._notify_waiters_locked()
            return request

    reserve = quote
    request_retry = quote
    submit = quote
    enqueue = quote
    apply = quote

    def _can_ever_fit_locked(self, request: TicketRequest) -> bool:
        remaining_retries = (
            math.inf
            if self.total_retries is None
            else self.total_retries - self._used_retries
        )
        remaining_seconds = (
            math.inf
            if self.total_seconds is None
            else self.total_seconds - self._used_seconds
        )
        return (
            request.retries <= remaining_retries + _EPS
            and request.seconds <= remaining_seconds + _EPS
        )

    def _reject_locked(
        self, request: TicketRequest, now: float | None = None
    ) -> None:
        if request.status not in {"queued", "reserved"}:
            return
        request.status = "rejected"
        request.ended_at = now if now is not None else self.clock()
        request.error = TicketRejected(
            f"retry quote {request.retries:g} retry/"
            f"{request.seconds:g}s does not fit ticket"
        )
        self._emit_locked(
            _event_kind("rejected", "denied", "exhausted"), request
        )

    def _expire_locked(
        self, request: TicketRequest, now: float | None = None
    ) -> None:
        if request.status not in {"queued", "reserved"}:
            return
        current = now if now is not None else self.clock()
        request.status = "expired"
        request.ended_at = current
        request.error = TicketExpired("original retry deadline expired")
        self._emit_locked(
            _event_kind("expired", "timeout", "timed_out"), request
        )

    def _cancel_locked(
        self,
        request: TicketRequest,
        reason: Any = None,
        now: float | None = None,
    ) -> bool:
        if request.status not in {"queued", "reserved"}:
            return False
        old_status = request.status
        ended_at = self.clock() if now is None else float(now)
        request.status = "cancelled"
        request.cancel_reason = reason
        request.ended_at = ended_at
        request.error = TicketCancelled(
            "pending retry ticket request was withdrawn"
        )
        self._emit_locked(
            _event_kind("cancelled", "withdrawn", "withdraw"),
            request,
            time=ended_at,
            previous_status=old_status,
            reason=str(reason) if reason is not None else None,
        )
        return True

    def cancel(
        self,
        request_or_lane: int | TicketRequest | str | None = None,
        at: float | str | None = None,
        *,
        now: float | None = None,
        lane: str | None = None,
        request_id: int | None = None,
        reason: Any = None,
    ) -> bool:
        """Withdraw one pending request; false if it already started."""
        if isinstance(at, str) and reason is None:
            reason = at
            current_time = now
        else:
            current_time = now if now is not None else at

        with self._lock:
            request = self._resolve_locked(
                request_or_lane, lane=lane, request_id=request_id
            )
            if request is None:
                return False
            changed = self._cancel_locked(request, reason, current_time)
            if changed:
                self._reallocate_locked(current_time)
                self._condition.notify_all()
                self._notify_waiters_locked()
            return changed

    withdraw = cancel
    revoke = cancel

    def _resolve_locked(
        self,
        request_or_lane: int | TicketRequest | str | None = None,
        *,
        lane: str | None = None,
        request_id: int | None = None,
    ) -> TicketRequest | None:
        if isinstance(request_or_lane, TicketRequest):
            return request_or_lane
        if isinstance(request_or_lane, int) and not isinstance(
            request_or_lane, bool
        ):
            return self._requests.get(request_or_lane)
        if request_id is not None:
            return self._requests.get(request_id)

        lane_name = lane
        if lane_name is None and isinstance(request_or_lane, str):
            lane_name = request_or_lane
        if lane_name is None:
            return None

        active = [
            request
            for request in self._requests.values()
            if request.lane == lane_name
            and request.status in {"queued", "reserved"}
        ]
        if active:
            return max(active, key=lambda request: request.sequence)

        matching = [
            request
            for request in self._requests.values()
            if request.lane == lane_name
        ]
        if not matching:
            return None
        return max(matching, key=lambda request: request.sequence)

    def _reallocate_locked(self, now: float | None = None) -> None:
        current = now if now is not None else self.clock()
        active = [
            request
            for request in self._requests.values()
            if request.status in {"queued", "reserved"}
        ]

        for request in active:
            if request.deadline is not None and current >= request.deadline:
                self._expire_locked(request, current)
            elif not self._can_ever_fit_locked(request):
                self._reject_locked(request, current)

        candidates = [
            request
            for request in self._requests.values()
            if request.status in {"queued", "reserved"}
        ]
        candidates.sort(
            key=lambda request: (-request.priority, request.sequence)
        )

        available_retries = (
            math.inf
            if self.total_retries is None
            else self.total_retries - self._used_retries
        )
        available_seconds = (
            math.inf
            if self.total_seconds is None
            else self.total_seconds - self._used_seconds
        )

        for request in candidates:
            old_status = request.status
            fits = (
                request.retries <= available_retries + _EPS
                and request.seconds <= available_seconds + _EPS
            )
            if fits:
                available_retries -= request.retries
                available_seconds -= request.seconds
                request.status = "reserved"
                if old_status == "queued":
                    self._emit_locked(
                        _event_kind(
                            "reserved", "granted", "held", "allocated"
                        ),
                        request,
                    )
            else:
                request.status = "queued"
                if old_status == "reserved":
                    self._emit_locked(
                        _event_kind(
                            "preempted",
                            "unreserved",
                            "requeued",
                            "revoked_reservation",
                        ),
                        request,
                    )

    def _enter_locked(
        self, request: TicketRequest, now: float
    ) -> TicketRequest | None:
        if request.status != "reserved":
            return None
        if request.deadline is not None and now >= request.deadline:
            self._expire_locked(request, now)
            return None
        if not self._can_ever_fit_locked(request):
            self._reject_locked(request, now)
            return None

        request.status = "entered"
        request.entered_at = now
        self._used_retries += request.retries
        self._used_seconds += request.seconds
        usage = TicketUsage(
            request_id=request.id,
            lane=request.lane,
            retries=request.retries,
            seconds=request.seconds,
            at=now,
            sequence=request.sequence,
            priority=request.priority,
        )
        self._usage.append(usage)
        self._emit_locked(
            _event_kind("used", "entered", "charged"),
            request,
            usage_seconds=request.seconds,
        )

        self._reallocate_locked(now)
        self._condition.notify_all()
        self._notify_waiters_locked()
        return request

    def enter(
        self,
        request_or_lane: TicketRequest | int | str | None = None,
        at: float | None = None,
        *,
        now: float | None = None,
        lane: str | None = None,
        request_id: int | None = None,
    ) -> TicketRequest | None:
        """
        Atomically charge and enter a reserved request whose backoff is due.

        This is the deterministic-clock counterpart to :meth:`wait`.
        """
        with self._lock:
            request = self._resolve_locked(
                request_or_lane, lane=lane, request_id=request_id
            )
            if request is None:
                raise KeyError(request_or_lane)
            current = (
                self.clock()
                if at is None and now is None
                else float(at if at is not None else now)
            )
            self._reallocate_locked(current)
            if request.status != "reserved" or current < request.ready_at:
                return None
            return self._enter_locked(request, current)

    start = enter
    begin = enter
    charge = enter
    commit = enter

    async def acquire_async(
        self, request_or_id: TicketRequest | int
    ) -> TicketRequest:
        return await self.wait_async(request_or_id)

    def _notify_waiters_locked(self) -> None:
        for request in self._requests.values():
            for waiter in tuple(request.waiters):
                with suppress(Exception):  # pragma: no cover - teardown race
                    waiter.notify()

    def _wait_timeout_locked(
        self, request: TicketRequest, now: float
    ) -> float | None:
        timeout: float | None = None
        if request.status == "reserved":
            timeout = max(0.0, request.ready_at - now)
        if request.deadline is not None:
            deadline_timeout = max(0.0, request.deadline - now)
            timeout = (
                deadline_timeout
                if timeout is None
                else min(timeout, deadline_timeout)
            )
        return timeout

    def _raise_terminal(self, request: TicketRequest) -> None:
        if request.error is not None:
            raise request.error
        if request.status == "cancelled":
            raise TicketCancelled("pending retry ticket request was withdrawn")
        if request.status == "expired":
            raise TicketExpired("original retry deadline expired")
        if request.status == "rejected":
            raise TicketRejected("retry quote does not fit ticket")
        raise TicketError(f"ticket request {request.id} did not start")

    def wait(
        self,
        request_or_id: TicketRequest | int,
        *,
        timeout: float | None = None,
    ) -> TicketRequest:
        """Wait for allocation/backoff, then atomically charge and enter."""
        request = self._resolve_wait_request(request_or_id)
        wait_started = self.clock()

        with self._condition:
            while True:
                now = self.clock()
                if request.status in {"cancelled", "expired", "rejected"}:
                    self._raise_terminal(request)
                if request.deadline is not None and now >= request.deadline:
                    self._expire_locked(request, now)
                    self._condition.notify_all()
                    self._notify_waiters_locked()
                    self._raise_terminal(request)

                if request.status == "reserved" and now >= request.ready_at:
                    entered = self._enter_locked(request, now)
                    if entered is not None:
                        return entered
                    self._raise_terminal(request)

                wait_timeout = self._wait_timeout_locked(request, now)
                if timeout is not None:
                    elapsed = now - wait_started
                    remaining_call_timeout = max(0.0, timeout - elapsed)
                    wait_timeout = (
                        remaining_call_timeout
                        if wait_timeout is None
                        else min(wait_timeout, remaining_call_timeout)
                    )

                if wait_timeout == 0:
                    continue

                self._condition.wait(wait_timeout)

    acquire = wait

    def _resolve_wait_request(
        self, request_or_id: TicketRequest | int
    ) -> TicketRequest:
        if isinstance(request_or_id, TicketRequest):
            return request_or_id
        request = self._requests.get(int(request_or_id))
        if request is None:
            raise KeyError(request_or_id)
        return request

    async def wait_async(
        self, request_or_id: TicketRequest | int
    ) -> TicketRequest:
        """Async variant of :meth:`wait` that does not block an event loop."""
        request = self._resolve_wait_request(request_or_id)

        while True:
            waiter: _AsyncWaiter | None = None
            with self._lock:
                now = self.clock()
                if request.status in {"cancelled", "expired", "rejected"}:
                    self._raise_terminal(request)
                if request.deadline is not None and now >= request.deadline:
                    self._expire_locked(request, now)
                    self._condition.notify_all()
                    self._notify_waiters_locked()
                    self._raise_terminal(request)
                if request.status == "reserved" and now >= request.ready_at:
                    entered = self._enter_locked(request, now)
                    if entered is not None:
                        return entered
                    self._raise_terminal(request)

                wait_timeout = self._wait_timeout_locked(request, now)
                io = current_async_library()
                if io == "asyncio":
                    waiter = _AsyncioWaiter()
                elif io == "trio":
                    waiter = _TrioWaiter()
                else:  # pragma: no cover
                    waiter = _PollingWaiter()
                request.waiters.append(waiter)

            try:
                await waiter.wait(wait_timeout)
            except BaseException:
                self.cancel(request.id, reason="async-task-cancelled")
                raise
            finally:
                with self._lock:
                    if waiter is not None and waiter in request.waiters:
                        request.waiters.remove(waiter)

    def advance(self, seconds: float | None = None) -> None:
        """Advance a manual clock and wake/reevaluate waiting requests."""
        with self._lock:
            if seconds is not None:
                delta = float(seconds)
                if self._manual_now is None:
                    self._manual_now = max(0.0, delta)
                else:
                    self._manual_now += delta
            now = self.clock()
            self._reallocate_locked(now)
            self._condition.notify_all()
            self._notify_waiters_locked()

    tick = advance

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    history = events
    log = events

    @property
    def usage(self) -> UsageList:
        return UsageList(self._usage)

    ledger = usage
    charges = usage

    def usage_by_lane(self) -> dict[str, TicketLaneUsage]:
        """Return aggregate charged usage keyed by lane."""
        with self._lock:
            lanes = {item.lane for item in self._usage}
            return {lane: self.usage_for(lane) for lane in sorted(lanes)}

    @property
    def used(self) -> UsageView:
        return UsageView(self.usage_by_lane())

    spent = used
    consumed = used
    usage_totals = used

    @property
    def used_retries(self) -> float:
        return self._used_retries

    @property
    def used_seconds(self) -> float:
        return self._used_seconds

    spent_retries = used_retries
    spent_seconds = used_seconds

    @property
    def remaining_retries(self) -> float | None:
        if self.total_retries is None:
            return None
        return max(0.0, self.total_retries - self._used_retries)

    @property
    def remaining_seconds(self) -> float | None:
        if self.total_seconds is None:
            return None
        return max(0.0, self.total_seconds - self._used_seconds)

    remaining_backoff = remaining_seconds
    available_retries = remaining_retries
    available_seconds = remaining_seconds

    def _lanes_with_status(self, *statuses: str) -> QueryList:
        with self._lock:
            return QueryList(
                request.lane
                for request in sorted(
                    self._requests.values(), key=lambda item: item.sequence
                )
                if request.status in statuses
            )

    @property
    def reservations(self) -> ReservationView:
        with self._lock:
            reserved = [
                request
                for request in self._requests.values()
                if request.status == "reserved"
            ]
            reserved.sort(
                key=lambda request: (-request.priority, request.sequence)
            )
            return ReservationView(
                (request.lane, request) for request in reserved
            )

    held = reservations
    holders = reservations
    occupied = reservations
    allocations = reservations
    claims = reservations

    @property
    def queued(self) -> list[str]:
        return self._lanes_with_status("queued")

    waiting = queued
    queue = queued

    @property
    def withdrawn(self) -> list[str]:
        return self._lanes_with_status("cancelled")

    cancelled = withdrawn
    canceled = withdrawn

    @property
    def expired(self) -> list[str]:
        return self._lanes_with_status("expired")

    @property
    def rejected(self) -> list[str]:
        return self._lanes_with_status("rejected")

    @property
    def used_lanes(self) -> list[str]:
        return self._lanes_with_status("entered")

    entered = used_lanes

    def status_of(self, lane_or_id: str | int) -> str | None:
        with self._lock:
            request = self._resolve_locked(lane_or_id)
            return None if request is None else request.status

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-ish point-in-time view of ticket allocation."""
        with self._lock:
            return {
                "total_retries": self.total_retries,
                "total_seconds": self.total_seconds,
                "used_retries": self.used_retries,
                "used_seconds": self.used_seconds,
                "remaining_retries": self.remaining_retries,
                "remaining_seconds": self.remaining_seconds,
                "reservations": {
                    lane: request.id
                    for lane, request in self.reservations.items()
                },
                "queued": self.queued,
                "withdrawn": self.withdrawn,
                "cancelled": self.cancelled,
                "expired": self.expired,
                "rejected": self.rejected,
                "used_lanes": self.used_lanes,
                "usage": [
                    {
                        "request_id": item.request_id,
                        "lane": item.lane,
                        "retries": item.retries,
                        "seconds": item.seconds,
                        "at": item.at,
                    }
                    for item in self.usage
                ],
            }

    status = status_of

    _QUERY_ALIASES: ClassVar[dict[str, str]] = {
        "reserved": "reservations",
        "reserved_lanes": "reservations",
        "occupants": "reservations",
        "active_reservations": "reservations",
        "current_reservations": "reservations",
        "withdrawals": "withdrawn",
        "cancellations": "cancelled",
        "canceled_lanes": "cancelled",
        "expirations": "expired",
        "timeouts": "expired",
        "rejections": "rejected",
        "uses": "used_lanes",
        "started": "used_lanes",
        "started_lanes": "used_lanes",
    }

    def __getattr__(self, name: str) -> Any:
        target = self._QUERY_ALIASES.get(name)
        if target is not None:
            return getattr(self, target)
        raise AttributeError(
            f"{type(self).__name__!s} has no attribute {name!r}"
        )

    def events_for(self, lane: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                event for event in self._events if event.get("lane") == lane
            ]

    def usage_for(self, lane: str) -> TicketLaneUsage:
        with self._lock:
            records = [item for item in self._usage if item.lane == lane]
            return TicketLaneUsage(
                lane=lane,
                retries=sum(item.retries for item in records),
                seconds=sum(item.seconds for item in records),
                count=len(records),
            )


@dataclass
class TicketLaneUsage:
    """Aggregate usage for one collection lane."""

    lane: str
    retries: float
    seconds: float
    count: int

    def __float__(self) -> float:
        return self.seconds

    def __int__(self) -> int:
        return int(self.retries)

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError as exc:
            raise KeyError(name) from exc


_CURRENT_TICKET: contextvars.ContextVar[RetryTicket | None] = (
    contextvars.ContextVar("stamina_current_ticket", default=None)
)


def current_ticket() -> RetryTicket | None:
    """Return the ticket installed in the current context, if any."""
    return _CURRENT_TICKET.get()


@contextmanager
def ticket_scope(
    ticket: RetryTicket | None,
) -> Iterator[RetryTicket | None]:
    """Install *ticket* as the ambient ticket for the current context."""
    token = _CURRENT_TICKET.set(ticket)
    try:
        yield ticket
    finally:
        _CURRENT_TICKET.reset(token)


def retry_ticket(*args: Any, **kwargs: Any) -> RetryTicket:
    """Create a shared retry ticket."""
    return RetryTicket(*args, **kwargs)


retry_budget = retry_ticket
continuation_ticket = retry_ticket
resume_ticket = retry_ticket
batch_ticket = retry_ticket


ContinuationTicket = RetryTicket
ContinueTicket = RetryTicket
ResumeTicket = RetryTicket
RunTicket = RetryTicket
BatchTicket = RetryTicket
RetryBudget = RetryTicket
RetryQuota = RetryTicket
RetryToken = RetryTicket
RetryCoordinator = RetryTicket
RetryScheduler = RetryTicket
Ticket = RetryTicket

TicketCancellation = TicketCancelled
TicketUnavailable = TicketRejected
