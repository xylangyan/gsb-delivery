# This file is a part of Dramatiq.
#
# Copyright (C) 2017,2018,2019,2020 CLEARTYPE SRL <bogdan@cleartype.io>
#
# Dramatiq is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Dramatiq is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Per-source-group concurrency limits for a single :class:`Worker`.

A source group is an arbitrary label attached to an actor (by default
its queue name).  When a limit is configured for a group, only that
many *attempts* may be inside their actor at any given moment for the
whole worker, regardless of how many messages have been prefetched.
Messages waiting for their eta, sitting prefetched on the work queue or
waiting for their broker acknowledgement do not occupy a slot.  Only
attempts that have been admitted into the actor and have not yet
returned from it are counted.

Lowering a limit never interrupts running attempts; it only constrains
the next admission.  Groups without a configured limit are unaffected
and keep being scheduled purely by actor priority and runnable order.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from queue import Empty
from typing import TYPE_CHECKING, Callable, Optional

from .logging import get_logger

if TYPE_CHECKING:
    from queue import PriorityQueue

    from .broker import MessageProxy
    from .worker import _WorkQueueItem

#: The option actors may set to name their source group.  When it is
#: absent the actor's queue name is used instead.
SOURCE_GROUP_OPTION = "source_group"

#: The internal selection result: an item and its admission number.
Selection = tuple["_WorkQueueItem", int]

#: The result of a successful admission: a work queue item paired with
#: the sequence number uniquely identifying that actor entry and the
#: attempt number captured before any middleware can mutate the message.
Acquired = tuple["_WorkQueueItem", int, int]


class SourceGroupLimits:
    """Thread-safe registry of per-source-group concurrency limits.

    A group only becomes constrained once a limit has been set for it;
    removing its limit restores the default, unlimited behaviour.
    Limits may be adjusted at any time and take effect on the next
    admission without disturbing attempts already inside their actor.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._limits: dict[str, int] = {}
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Register a callable invoked after a limit is set or removed.
        The scheduler uses this to re-run admission whenever the
        available capacity changes.
        """
        with self._lock:
            self._listeners.append(listener)

    def set_limit(self, group: str, limit: int) -> None:
        """Set or replace the concurrency limit for ``group``.

        Parameters:
          group(str): The source group to constrain.
          limit(int): A positive maximum number of concurrent attempts.
        """
        if limit < 1:
            raise ValueError("source group limits must be positive, got %r for %r" % (limit, group))

        with self._lock:
            self._limits[group] = limit
        # Notify outside the registry lock: listeners acquire the
        # scheduler's condition lock while admission reads limits while
        # already holding that condition lock, so nesting the two would
        # risk an AB-BA deadlock.
        self._notify()

    def remove_limit(self, group: str) -> None:
        """Remove any limit configured for ``group``."""
        with self._lock:
            self._limits.pop(group, None)
        self._notify()

    def get_limit(self, group: str) -> Optional[int]:
        """Return the configured limit for ``group`` or ``None``."""
        with self._lock:
            return self._limits.get(group)

    def groups(self) -> dict[str, int]:
        """Return a snapshot copy of the configured limits."""
        with self._lock:
            return dict(self._limits)

    def _notify(self) -> None:
        # Snapshot under the lock, then invoke outside it so listener
        # callbacks (which take the scheduler's condition lock) never
        # run while the registry lock is held.
        with self._lock:
            listeners = list(self._listeners)

        for listener in listeners:
            listener()


@dataclasses.dataclass(frozen=True, slots=True)
class SourceGroupEvent:
    """A single scheduling observation for operational auditing.

    Each event is tied to a message and an attempt number so retries of
    the same message can be told apart.  ``started``/``exited`` pairs
    share an admission number.

    Parameters:
      event(str): One of ``prefetched``, ``delayed``, ``started``,
        ``exited``, ``acked`` or ``nacked``.
      message_id(str): The id of the underlying message.
      attempt(int): The attempt number within the message (0 for the
        first attempt, incremented for every retry).
      group(str): The resolved source group.
      at_ns(int): A monotonic timestamp.
      admission(Optional[int]): A per-worker admission sequence number.
    """

    event: str
    message_id: str
    attempt: int
    group: str
    at_ns: int
    admission: Optional[int] = None


class SourceGroupEventLog:
    """An in-memory, thread-safe journal of scheduling events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[SourceGroupEvent] = []

    def record(
        self,
        event: str,
        message: "MessageProxy",
        group: str,
        *,
        admission: Optional[int] = None,
        attempt: Optional[int] = None,
    ) -> SourceGroupEvent:
        if attempt is None:
            attempt = message.options.get("retries", 0)

        entry = SourceGroupEvent(
            event=event,
            message_id=message.message_id,
            attempt=attempt,
            group=group,
            at_ns=time.monotonic_ns(),
            admission=admission,
        )
        with self._lock:
            self._events.append(entry)
        return entry

    def events(self) -> list[SourceGroupEvent]:
        """Return a snapshot copy of every recorded event."""
        with self._lock:
            return list(self._events)


class SourceGroupScheduler:
    """Admissions gate shared by a worker's consumer and worker threads.

    Messages become *runnable* once a consumer has fetched them off a
    normal queue (delayed messages must first go through the usual
    delay-queue re-enqueue).  Runnable messages are placed on the
    worker's priority queue.  Worker threads funnel through
    :meth:`acquire`, which pulls messages in priority/runnable order and
    admits the first one whose source group still has capacity.
    Messages belonging to saturated groups are parked in :attr:`parked`
    without occupying an execution thread and reconsidered whenever
    capacity frees up, a limit changes or new runnable work arrives.

    Every admitted attempt increments its group's in-flight counter and
    :meth:`release` decrements it once the actor call returns, before
    the broker acknowledgement.  Waiting for an acknowledgement
    therefore never occupies a source-group slot.
    """

    def __init__(
        self,
        limits: SourceGroupLimits,
        event_log: SourceGroupEventLog,
        work_queue: "PriorityQueue[_WorkQueueItem]",
    ) -> None:
        self.logger = get_logger(__name__, type(self))
        self._limits = limits
        self._event_log = event_log
        self._work_queue = work_queue
        self._condition = threading.Condition()
        # Runnable items pulled off the work queue but refused admission
        # because their source group is saturated.  Keyed by message id.
        self._parked: dict[str, _WorkQueueItem] = {}
        # Number of attempts currently inside their actor per group.
        self._in_flight: dict[str, int] = {}
        # Monotonic counters for runnable ordering and admission pairing.
        self._runnable_seq = 0
        self._admission_seq = 0
        limits.add_listener(self._on_limits_changed)

    @property
    def parked(self) -> dict[str, _WorkQueueItem]:
        return self._parked

    def source_group(self, message: "MessageProxy") -> str:
        """Resolve the source group label for ``message``."""
        group = message.options.get(SOURCE_GROUP_OPTION)
        if group is not None:
            return group
        return message.queue_name

    def next_runnable_seq(self) -> int:
        """Return the sequence number for a newly runnable attempt."""
        with self._condition:
            self._runnable_seq += 1
            return self._runnable_seq

    def on_runnable(self, item: _WorkQueueItem) -> None:
        """Observe a prefetched, runnable message and wake idle threads."""
        message = item.message
        group = self.source_group(message)
        self._event_log.record("prefetched", message, group)
        with self._condition:
            self._condition.notify_all()

    def record_delayed(self, message: "MessageProxy") -> None:
        """Record that a fetched message is parked on a delay queue and
        therefore is not runnable yet.
        """
        self._event_log.record("delayed", message, self.source_group(message))

    def record_exited(self, message: "MessageProxy", admission: int, attempt: int) -> None:
        """Record that an admitted attempt has returned from its actor."""
        self._event_log.record(
            "exited",
            message,
            self.source_group(message),
            admission=admission,
            attempt=attempt,
        )

    def record_acknowledgement(self, message: "MessageProxy", attempt: int) -> None:
        """Record the broker acknowledgement (or rejection) of an attempt.

        Only acknowledgements belonging to an attempt that entered an
        actor are journaled; internal acknowledgements (such as the one
        issued when an eta elapses and a delay-queue message is
        re-enqueued) never reach this method.
        """
        event = "nacked" if message.failed else "acked"
        self._event_log.record(event, message, self.source_group(message), attempt=attempt)

    def drain_parked(self) -> list[_WorkQueueItem]:
        """Remove and return every runnable item still awaiting admission.

        Used during worker shutdown to requeue prefetched messages that
        never entered an actor.
        """
        with self._condition:
            items = list(self._parked.values())
            self._parked.clear()
            self._condition.notify_all()
        return items

    def acquire(self, timeout: Optional[float] = None) -> Optional[Acquired]:
        """Block until an admissible runnable message is available.

        Returns a tuple of ``(item, admission, attempt)``, where
        ``admission`` is a sequence number uniquely identifying this
        actor entry and ``attempt`` is the attempt number captured at
        entry, or ``None`` when the timeout elapses.  Messages whose
        group is saturated are parked and never block the calling
        thread; the thread keeps scanning for an eligible message.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                selected = self._select()
                if selected is not None:
                    item, admission = selected
                    # Capture the attempt before any middleware (e.g.
                    # Retries) can mutate options, so retries journal
                    # independently of the attempt that spawned them.
                    attempt = item.message.options.get("retries", 0)
                    self._event_log.record(
                        "started",
                        item.message,
                        self.source_group(item.message),
                        admission=admission,
                        attempt=attempt,
                    )
                    return item, admission, attempt

                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                self._condition.wait(remaining)
                if deadline is not None and time.monotonic() >= deadline:
                    return None

    def release(self, admission: int, group: str) -> None:
        """Mark an admitted attempt as having left its actor."""
        with self._condition:
            current = self._in_flight.get(group, 0)
            if current > 0:
                self._in_flight[group] = current - 1
            else:  # pragma: no cover - releases always pair with acquires
                self.logger.warning("Released an unreserved source-group slot for %r.", group)
            self._condition.notify_all()

    def in_flight(self, group: str) -> int:
        with self._condition:
            return self._in_flight.get(group, 0)

    def _select(self) -> Optional[Selection]:
        """Pick the best admissible runnable item.

        Every runnable item (previously parked plus everything still on
        the priority work queue) is merged into one pool and ordered by
        ``(priority, runnable_seq)`` exactly like the work queue.  The
        first item whose source group has capacity is admitted; the rest
        become or stay parked so saturated groups can never block other
        work.  Each item leaves the work queue exactly once.
        """
        pool: list[_WorkQueueItem] = list(self._parked.values())
        while True:
            try:
                pool.append(self._work_queue.get_nowait())
            except Empty:
                break

        if not pool:
            return None

        pool.sort(key=lambda item: (item.priority, item.runnable_seq))
        selected: Optional[Selection] = None
        retained: dict[str, _WorkQueueItem] = {}
        for item in pool:
            if selected is None and self._has_capacity(item):
                selected = (item, self._admit(item))
            else:
                retained[item.message.message_id] = item

        self._parked = retained
        return selected

    def _has_capacity(self, item: _WorkQueueItem) -> bool:
        group = self.source_group(item.message)
        limit = self._limits.get_limit(group)
        return limit is None or self._in_flight.get(group, 0) < limit

    def _admit(self, item: _WorkQueueItem) -> int:
        group = self.source_group(item.message)
        self._in_flight[group] = self._in_flight.get(group, 0) + 1
        self._admission_seq += 1
        return self._admission_seq

    def _on_limits_changed(self) -> None:
        with self._condition:
            self._condition.notify_all()
