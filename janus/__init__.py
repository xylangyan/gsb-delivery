import asyncio
import importlib.metadata
import sys
import threading
from asyncio import QueueEmpty as AsyncQueueEmpty
from asyncio import QueueFull as AsyncQueueFull
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from heapq import heappop, heappush
from queue import Empty as SyncQueueEmpty
from queue import Full as SyncQueueFull
from time import monotonic
from typing import Any, Generic, Protocol, TypeVar

if sys.version_info >= (3, 13):
    from asyncio import QueueShutDown as AsyncQueueShutDown
    from queue import ShutDown as SyncQueueShutDown
else:
    # Python 3.11-3.12 stdlib queues have no shutdown support,
    # provide compatible replacements.

    class AsyncQueueShutDown(Exception):
        """Raised when put/get is called on a shut-down async queue."""

    class SyncQueueShutDown(Exception):
        """Raised when put/get is called on a shut-down sync queue."""


__version__ = importlib.metadata.version("janus")
__all__ = (
    "Queue",
    "PriorityQueue",
    "LifoQueue",
    "SyncQueue",
    "SyncQueueEmpty",
    "SyncQueueFull",
    "SyncQueueShutDown",
    "AsyncQueue",
    "AsyncQueueEmpty",
    "AsyncQueueFull",
    "AsyncQueueShutDown",
    "BaseQueue",
    "QueueRebindError",
    "ItemNotDeliveredError",
    "RebindReport",
)


class SupportsLT(Protocol):
    def __lt__(self, other: Any) -> bool: ...


class SupportsGT(Protocol):
    def __gt__(self, other: Any) -> bool: ...


RichComparableT = TypeVar("RichComparableT", bound=SupportsGT | SupportsLT)

T = TypeVar("T")
OptFloat = float | None


class QueueRebindError(RuntimeError):
    """Raised on the old event loop after the queue was handed over.

    A coroutine waiting on the old loop when the handover happened is
    resolved with this exception. It never took or enqueued an item, so
    the queue state is untouched; the caller may retry against the queue
    bound to the new event loop.
    """


class ItemNotDeliveredError(QueueRebindError):
    """Raised when a blocked async ``put()`` loses the race with a handover.

    The item was never admitted to the queue; it is reported in
    :attr:`RebindReport.not_enqueued` and stays owned by the caller.
    """


@dataclass(frozen=True)
class RebindReport(Generic[T]):
    """Item-by-item accounting of one event loop handover.

    * ``not_enqueued`` -- items rejected by old-loop putters that never
      made it into the queue.
    * ``pending_in_queue`` -- items still waiting in the queue, handed
      over to the new writer untouched and in their original order.
    * ``taken_unconfirmed`` -- items already fetched by a consumer
      (old or new) whose ``task_done()`` had not arrived at handover.
    * ``confirmed`` -- items acknowledged with ``task_done()`` since the
      previous handover.
    * ``stale_notifications`` -- wake-up callbacks queued on the old
      event loop that were drained during handover without handing out
      another item.
    """

    generation: int
    not_enqueued: tuple[T, ...]
    pending_in_queue: tuple[T, ...]
    taken_unconfirmed: tuple[T, ...]
    confirmed: tuple[T, ...]
    stale_notifications: int


class BaseQueue(Protocol[T]):
    @property
    def maxsize(self) -> int: ...

    @property
    def closed(self) -> bool: ...

    def task_done(self) -> None: ...

    def qsize(self) -> int: ...

    @property
    def unfinished_tasks(self) -> int: ...

    def empty(self) -> bool: ...

    def full(self) -> bool: ...

    def put_nowait(self, item: T) -> None: ...

    def get_nowait(self) -> T: ...

    def shutdown(self, immediate: bool = False) -> None: ...


class SyncQueue(BaseQueue[T], Protocol[T]):
    def put(self, item: T, block: bool = True, timeout: OptFloat = None) -> None: ...

    def get(self, block: bool = True, timeout: OptFloat = None) -> T: ...

    def join(self) -> None: ...


class AsyncQueue(BaseQueue[T], Protocol[T]):
    async def put(self, item: T) -> None: ...

    async def get(self) -> T: ...

    async def join(self) -> None: ...


class _LoopBinding(Generic[T]):
    """Everything bound to a particular asyncio event loop.

    A handover detaches the current binding and attaches a fresh one for
    the new loop without touching the thread-side primitives or the
    shared item storage.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.mutex = asyncio.Lock()
        self.not_empty = asyncio.Condition(self.mutex)
        self.not_full = asyncio.Condition(self.mutex)
        self.tasks_done = asyncio.Condition(self.mutex)
        self.not_empty_waiting = 0
        self.not_full_waiting = 0
        self.tasks_done_waiting = 0
        # Coroutines parked inside a condition.wait() section.
        self.active_waits = 0
        # Every put/get/join call that resolved against this binding and
        # has not returned yet, including ones queued at mutex acquisition.
        self.active_callers = 0
        # Detached bindings keep serving only the finalization drain.
        self.detached = False
        # Notifications scheduled on the loop whose notifier task has
        # not completed yet, including callbacks still queued by
        # call_soon_threadsafe.
        self.queued_wakeups = 0
        # Notifier tasks created via call_soon_threadsafe, see _notify_async.
        self.pending: deque[asyncio.Task[None]] = deque()


class Queue(Generic[T]):
    def __init__(self, maxsize: int = 0) -> None:
        self._maxsize = maxsize
        self._is_shutdown = False

        self._init(maxsize)

        self._unfinished_tasks = 0

        self._sync_mutex = threading.Lock()
        self._sync_not_empty = threading.Condition(self._sync_mutex)
        self._sync_not_empty_waiting = 0
        self._sync_not_full = threading.Condition(self._sync_mutex)
        self._sync_not_full_waiting = 0
        self._sync_tasks_done = threading.Condition(self._sync_mutex)
        self._sync_tasks_done_waiting = 0

        self._binding: _LoopBinding[T] | None = None

        # Item-by-item bookkeeping for handover reports.
        self._in_flight: deque[T] = deque()
        self._confirmed: deque[T] = deque()
        self._rejected_items: deque[T] = deque()

        self._rebind_generation = 0
        self._rebind_barrier: threading.Event | None = None
        self._retired_bindings: list[_LoopBinding[T]] = []

        self._sync_queue = _SyncQueueProxy(self)
        self._async_queue = _AsyncQueueProxy(self)

    @property
    def _loop(self) -> asyncio.AbstractEventLoop | None:
        if self._binding is None:
            return None
        return self._binding.loop

    @_loop.setter
    def _loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        if loop is None:
            self._binding = None
        elif self._binding is None or self._binding.loop is not loop:
            self._binding = _LoopBinding(loop)

    @property
    def _pending(self) -> deque[asyncio.Task[None]]:
        binding = self._binding
        if binding is None:
            return deque()
        return binding.pending

    @property
    def _async_not_empty_waiting(self) -> int:
        binding = self._binding
        return binding.not_empty_waiting if binding is not None else 0

    @property
    def _async_not_full_waiting(self) -> int:
        binding = self._binding
        return binding.not_full_waiting if binding is not None else 0

    @property
    def _async_tasks_done_waiting(self) -> int:
        binding = self._binding
        return binding.tasks_done_waiting if binding is not None else 0

    def _get_binding_locked(self) -> _LoopBinding[T]:
        # Warning!
        # The function should be called when self._sync_mutex is locked,
        # otherwise the code is not thread-safe
        loop = asyncio.get_running_loop()
        binding = self._binding

        if binding is None:
            binding = _LoopBinding(loop)
            self._binding = binding
            return binding
        if loop is binding.loop:
            return binding

        for retired in self._retired_bindings:
            if loop is retired.loop:
                raise QueueRebindError(
                    f"{self!r} was handed over to another event loop; "
                    "this coroutine on the old loop must give up without "
                    "touching the queue"
                )
        raise RuntimeError(f"{self!r} is bound to a different event loop")

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        # Warning!
        # The function should be called when self._sync_mutex is locked,
        # otherwise the code is not thread-safe
        return self._get_binding_locked().loop

    def _validate_binding(self, binding: _LoopBinding[T]) -> None:
        # Called under self._sync_mutex after acquiring the async lock.
        if binding.detached:
            raise QueueRebindError(
                f"{self!r} was handed over to another event loop while "
                "this coroutine was waiting"
            )

    def _register_taken(self, item: T) -> None:
        # Called under self._sync_mutex (or the tasks_done condition
        # wrapping it) whenever an item leaves the queue.
        self._in_flight.append(item)

    def _register_done(self) -> None:
        if self._in_flight:
            self._confirmed.append(self._in_flight.popleft())

    def _reject_item(self, item: T) -> None:
        self._rejected_items.append(item)

    async def rebind_event_loop(self) -> RebindReport[T]:
        """Hand the queue over to the currently running event loop.

        Awaitable from the *new* writer loop while the producer threads
        keep using :attr:`sync_q`. The call:

        * detaches the current loop binding and attaches a fresh one for
          the running loop, keeping the same underlying queue;
        * resolves every waiter still parked on the old loop with
          :class:`QueueRebindError` (or :class:`ItemNotDeliveredError`
          for putters whose item was never admitted) without moving any
          item;
        * drains stale wake-up notifications on the old loop;
        * returns only after the old loop has confirmed the handover, so
          it is safe to stop that loop afterwards.

        The returned :class:`RebindReport` accounts for every item:
        rejected, still queued, taken but unconfirmed, and confirmed.
        """
        new_loop = asyncio.get_running_loop()

        with self._sync_mutex:
            if self._is_shutdown:
                raise RuntimeError("cannot rebind a shut-down queue")
            if self._rebind_barrier is not None:
                raise RuntimeError("event loop handover is already in progress")

            old_binding = self._binding
            if old_binding is not None and new_loop is old_binding.loop:
                raise RuntimeError("queue is already bound to this event loop")

            # A stopped loop can no longer issue calls, so its retired
            # binding cannot receive late waiters and may be dropped.
            self._retired_bindings = [
                binding
                for binding in self._retired_bindings
                if not binding.loop.is_closed()
            ]

            barrier = threading.Event()
            self._rebind_barrier = barrier
            self._rebind_generation += 1
            generation = self._rebind_generation

            stale_notifications = 0
            if old_binding is not None:
                stale_notifications = old_binding.queued_wakeups
                self._retired_bindings.append(old_binding)

            self._binding = _LoopBinding(new_loop)

        if old_binding is not None and not old_binding.loop.is_closed():
            try:
                old_binding.loop.call_soon_threadsafe(
                    self._spawn_detach_finalization, old_binding
                )
            except RuntimeError:
                # The old loop stopped before it could accept the
                # finalization job; nothing on it can be waiting anymore.
                barrier.set()
        else:
            barrier.set()

        await asyncio.to_thread(barrier.wait)

        with self._sync_mutex:
            report: RebindReport[T] = RebindReport(
                generation=generation,
                not_enqueued=tuple(self._rejected_items),
                pending_in_queue=self._snapshot(),
                taken_unconfirmed=tuple(self._in_flight),
                confirmed=tuple(self._confirmed),
                stale_notifications=stale_notifications,
            )
            self._rejected_items.clear()
            self._confirmed.clear()
            self._rebind_barrier = None
        return report

    async def wait_rebind(self) -> None:
        """Wait until an in-progress handover has been confirmed."""
        barrier = self._rebind_barrier
        if barrier is not None:
            await asyncio.to_thread(barrier.wait)

    def _spawn_detach_finalization(self, old_binding: _LoopBinding[T]) -> None:
        # Runs on the old event loop.
        task = old_binding.loop.create_task(
            self._finalize_detached_binding(old_binding)
        )
        task.add_done_callback(self._on_detach_finalized)

    def _on_detach_finalized(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            # Retrieve the exception so it is not reported as an
            # unretrieved task exception when the old loop keeps running.
            task.exception()
        barrier = self._rebind_barrier
        if barrier is not None:
            barrier.set()

    async def _finalize_detached_binding(self, old_binding: _LoopBinding[T]) -> None:
        # Flip the detached flag while holding the async mutex so a
        # caller queued at mutex acquisition is still counted and will
        # observe the flag as soon as it enters.
        async with old_binding.mutex:
            with self._sync_mutex:
                old_binding.detached = True

        # Drive every old-loop waiter into its detached check. A parked
        # waiter only wakes when its condition is notified, so keep
        # notifying until every resolved caller has returned.
        while True:
            async with old_binding.mutex:
                old_binding.not_empty.notify_all()
                old_binding.not_full.notify_all()
                old_binding.tasks_done.notify_all()
            await asyncio.sleep(0)
            with self._sync_mutex:
                if old_binding.active_callers == 0:
                    break

        # Drain wake-up tasks that were queued but had not run yet.
        pending = tuple(old_binding.pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def shutdown(self, immediate: bool = False) -> None:
        """Shut-down the queue, making queue gets and puts raise an exception.

        By default, gets will only raise once the queue is empty. Set
        'immediate' to True to make gets raise immediately instead.

        All blocked callers of put() and get() will be unblocked. If
        'immediate', a task is marked as done for each item remaining in
        the queue, which may unblock callers of join().

        The raise exception is SyncQueueShutDown for sync api and AsyncQueueShutDown
        for async one.
        """
        with self._sync_mutex:
            self._is_shutdown = True
            binding = self._binding
            if immediate:
                while self._qsize():
                    self._get()
                    if self._unfinished_tasks > 0:
                        self._unfinished_tasks -= 1
                # release all blocked threads in `join()`
                if self._sync_tasks_done_waiting:
                    self._sync_tasks_done.notify_all()
                if binding is not None and binding.tasks_done_waiting:
                    self._notify_async(binding, binding.tasks_done.notify_all)
            # All getters need to re-check queue-empty to raise ShutDown
            if self._sync_not_empty_waiting:
                self._sync_not_empty.notify_all()
            if self._sync_not_full_waiting:
                self._sync_not_full.notify_all()
            if binding is not None:
                if binding.not_empty_waiting:
                    self._notify_async(binding, binding.not_empty.notify_all)
                if binding.not_full_waiting:
                    self._notify_async(binding, binding.not_full.notify_all)

    def close(self) -> None:
        """Close the queue.

        The method is a shortcut for .shutdown(immediate=True)
        """
        self.shutdown(immediate=True)

    async def wait_closed(self) -> None:
        """Wait for finishing all pending activities"""
        # should be called from loop after close().
        # Nobody should put/get at this point,
        # so lock acquiring is not required
        if not self._is_shutdown:
            raise RuntimeError("Waiting for non-closed queue")
        barrier = self._rebind_barrier
        if barrier is not None:
            await asyncio.to_thread(barrier.wait)
        # give a chance for the task-done callbacks
        # of async tasks created inside
        # _notify_async()
        # methods to be executed.
        await asyncio.sleep(0)
        binding = self._binding
        if binding is not None and binding.pending:
            await asyncio.wait(binding.pending)

    async def aclose(self) -> None:
        """Shutdown the queue and wait for actual shutting down"""
        self.close()
        await self.wait_closed()

    @property
    def closed(self) -> bool:
        binding = self._binding
        return self._is_shutdown and (binding is None or not binding.pending)

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def sync_q(self) -> "_SyncQueueProxy[T]":
        return self._sync_queue

    @property
    def async_q(self) -> "_AsyncQueueProxy[T]":
        return self._async_queue

    # Override these methods to implement other queue organizations
    # (e.g. stack or priority queue).
    # These will only be called with appropriate locks held

    def _init(self, maxsize: int) -> None:
        self._queue: deque[T] = deque()

    def _qsize(self) -> int:
        return len(self._queue)

    def _snapshot(self) -> tuple[T, ...]:
        # Items in their dequeue order; used for handover reports.
        return tuple(self._queue)

    # Put a new item in the queue
    def _put(self, item: T) -> None:
        self._queue.append(item)

    # Get an item from the queue
    def _get(self) -> T:
        return self._queue.popleft()

    def _put_internal(self, item: T) -> None:
        self._put(item)
        self._unfinished_tasks += 1

    async def _do_async_notifier(
        self, binding: _LoopBinding[T], method: Callable[[], None]
    ) -> None:
        with self._sync_mutex:
            binding.queued_wakeups -= 1
        async with binding.mutex:
            method()

    def _setup_async_notifier(
        self, binding: _LoopBinding[T], method: Callable[[], None]
    ) -> None:
        task = binding.loop.create_task(self._do_async_notifier(binding, method))

        def remove(
            _task: asyncio.Task[None],
            target: _LoopBinding[T] = binding,
        ) -> None:
            try:
                target.pending.remove(_task)
            except ValueError:
                pass

        task.add_done_callback(remove)
        binding.pending.append(task)

    def _notify_async(
        self, binding: _LoopBinding[T], method: Callable[[], None]
    ) -> None:
        # Warning!
        # The function should be called when self._sync_mutex is locked,
        # otherwise the code is not thread-safe
        loop = binding.loop
        if loop.is_closed():
            # async API is not available, nothing to notify
            return
        binding.queued_wakeups += 1
        try:
            loop.call_soon_threadsafe(self._setup_async_notifier, binding, method)
        except RuntimeError:
            # The loop stopped right after the is_closed() check; the
            # notification cannot be delivered or accounted for anymore.
            binding.queued_wakeups -= 1


class _SyncQueueProxy(SyncQueue[T]):
    """Create a queue object with a given maximum size.

    If maxsize is <= 0, the queue size is infinite.
    """

    def __init__(self, parent: Queue[T]):
        self._parent = parent

    @property
    def maxsize(self) -> int:
        return self._parent._maxsize

    @property
    def closed(self) -> bool:
        return self._parent.closed

    def task_done(self) -> None:
        """Indicate that a formerly enqueued task is complete.

        Used by Queue consumer threads.  For each get() used to fetch a task,
        a subsequent call to task_done() tells the queue that the processing
        on the task is complete.

        If a join() is currently blocking, it will resume when all items
        have been processed (meaning that a task_done() call was received
        for every item that had been put() into the queue).

        Raises a ValueError if called more times than there were items
        placed in the queue.
        """
        parent = self._parent
        with parent._sync_tasks_done:
            unfinished = parent._unfinished_tasks - 1
            if unfinished <= 0:
                if unfinished < 0:
                    raise ValueError("task_done() called too many times")
                if parent._sync_tasks_done_waiting:
                    parent._sync_tasks_done.notify_all()
                binding = parent._binding
                if binding is not None and binding.tasks_done_waiting:
                    parent._notify_async(binding, binding.tasks_done.notify_all)
            parent._unfinished_tasks = unfinished
            parent._register_done()

    def join(self) -> None:
        """Blocks until all items in the Queue have been gotten and processed.

        The count of unfinished tasks goes up whenever an item is added to the
        queue. The count goes down whenever a consumer thread calls task_done()
        to indicate the item was retrieved and all work on it is complete.

        When the count of unfinished tasks drops to zero, join() unblocks.
        """
        parent = self._parent
        with parent._sync_tasks_done:
            while parent._unfinished_tasks:
                parent._sync_tasks_done_waiting += 1
                try:
                    parent._sync_tasks_done.wait()
                finally:
                    parent._sync_tasks_done_waiting -= 1

    def qsize(self) -> int:
        """Return the approximate size of the queue (not reliable!)."""
        return self._parent._qsize()

    @property
    def unfinished_tasks(self) -> int:
        """Return the number of unfinished tasks."""
        return self._parent._unfinished_tasks

    def empty(self) -> bool:
        """Return True if the queue is empty, False otherwise (not reliable!).

        This method is likely to be removed at some point.  Use qsize() == 0
        as a direct substitute, but be aware that either approach risks a race
        condition where a queue can grow before the result of empty() or
        qsize() can be used.

        To create code that needs to wait for all queued tasks to be
        completed, the preferred technique is to use the join() method.
        """
        return not self._parent._qsize()

    def full(self) -> bool:
        """Return True if the queue is full, False otherwise (not reliable!).

        This method is likely to be removed at some point.  Use qsize() >= n
        as a direct substitute, but be aware that either approach risks a race
        condition where a queue can shrink before the result of empty() or
        qsize() can be used.
        """
        parent = self._parent
        return 0 < parent._maxsize <= parent._qsize()

    def put(self, item: T, block: bool = True, timeout: OptFloat = None) -> None:
        """Put an item into the queue.

        If optional args 'block' is true and 'timeout' is None (the default),
        block if necessary until a free slot is available. If 'timeout' is
        a non-negative number, it blocks at most 'timeout' seconds and raises
        the Full exception if no free slot was not available within that
        time. Otherwise ('block' is false), put an item on the queue if a
        free slot is immediately available, else raise the Full exception
        ('timeout' is ignored in that case).
        """
        parent = self._parent
        with parent._sync_not_full:
            if parent._is_shutdown:
                raise SyncQueueShutDown
            if parent._maxsize > 0:
                if not block:
                    if parent._qsize() >= parent._maxsize:
                        raise SyncQueueFull
                elif timeout is None:
                    while parent._qsize() >= parent._maxsize:
                        parent._sync_not_full_waiting += 1
                        try:
                            parent._sync_not_full.wait()
                        finally:
                            parent._sync_not_full_waiting -= 1
                        if parent._is_shutdown:
                            raise SyncQueueShutDown
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = monotonic() + timeout
                    while parent._qsize() >= parent._maxsize:
                        remaining = endtime - monotonic()
                        if remaining <= 0.0:
                            raise SyncQueueFull
                        parent._sync_not_full_waiting += 1
                        try:
                            parent._sync_not_full.wait(remaining)
                        finally:
                            parent._sync_not_full_waiting -= 1
                        if parent._is_shutdown:
                            raise SyncQueueShutDown
            parent._put_internal(item)
            if parent._sync_not_empty_waiting:
                parent._sync_not_empty.notify()
            binding = parent._binding
            if binding is not None and binding.not_empty_waiting:
                parent._notify_async(binding, binding.not_empty.notify)

    def get(self, block: bool = True, timeout: OptFloat = None) -> T:
        """Remove and return an item from the queue.

        If optional args 'block' is true and 'timeout' is None (the default),
        block if necessary until an item is available. If 'timeout' is
        a non-negative number, it blocks at most 'timeout' seconds and raises
        the Empty exception if no item was not available within that
        timeout. Otherwise ('block' is false), return an item if one is
        immediately available, else raise the Empty exception ('timeout' is
        ignored in that case).
        """
        parent = self._parent
        with parent._sync_not_empty:
            if parent._is_shutdown and not parent._qsize():
                raise SyncQueueShutDown
            if not block:
                if not parent._qsize():
                    raise SyncQueueEmpty
            elif timeout is None:
                while not parent._qsize():
                    parent._sync_not_empty_waiting += 1
                    try:
                        parent._sync_not_empty.wait()
                    finally:
                        parent._sync_not_empty_waiting -= 1
                    if parent._is_shutdown and not parent._qsize():
                        raise SyncQueueShutDown
            elif timeout < 0:
                raise ValueError("'timeout' must be a non-negative number")
            else:
                endtime = monotonic() + timeout
                while not parent._qsize():
                    remaining = endtime - monotonic()
                    if remaining <= 0.0:
                        raise SyncQueueEmpty
                    parent._sync_not_empty_waiting += 1
                    try:
                        parent._sync_not_empty.wait(remaining)
                    finally:
                        parent._sync_not_empty_waiting -= 1
                    if parent._is_shutdown and not parent._qsize():
                        raise SyncQueueShutDown
            item = parent._get()
            parent._register_taken(item)
            if parent._sync_not_full_waiting:
                parent._sync_not_full.notify()
            binding = parent._binding
            if binding is not None and binding.not_full_waiting:
                parent._notify_async(binding, binding.not_full.notify)
            return item

    def put_nowait(self, item: T) -> None:
        """Put an item into the queue without blocking.

        Only enqueue the item if a free slot is immediately available.
        Otherwise raise the Full exception.
        """
        return self.put(item, block=False)

    def get_nowait(self) -> T:
        """Remove and return an item from the queue without blocking.

        Only get an item if one is immediately available. Otherwise
        raise the Empty exception.
        """
        return self.get(block=False)

    def shutdown(self, immediate: bool = False) -> None:
        """Shut-down the queue, making queue gets and puts raise an exception.

        By default, gets will only raise once the queue is empty. Set
        'immediate' to True to make gets raise immediately instead.

        All blocked callers of put() and get() will be unblocked. If
        'immediate', a task is marked as done for each item remaining in
        the queue, which may unblock callers of join().

        The raise exception is SyncQueueShutDown for sync api and AsyncQueueShutDown
        for async one.
        """
        self._parent.shutdown(immediate)


class _AsyncQueueProxy(AsyncQueue[T]):
    """Create a queue object with a given maximum size.

    If maxsize is <= 0, the queue size is infinite.
    """

    def __init__(self, parent: Queue[T]):
        self._parent = parent

    @property
    def closed(self) -> bool:
        parent = self._parent
        return parent.closed

    def qsize(self) -> int:
        """Number of items in the queue."""
        parent = self._parent
        return parent._qsize()

    @property
    def unfinished_tasks(self) -> int:
        """Return the number of unfinished tasks."""
        parent = self._parent
        return parent._unfinished_tasks

    @property
    def maxsize(self) -> int:
        """Number of items allowed in the queue."""
        parent = self._parent
        return parent._maxsize

    def empty(self) -> bool:
        """Return True if the queue is empty, False otherwise."""
        return self.qsize() == 0

    def full(self) -> bool:
        """Return True if there are maxsize items in the queue.

        Note: if the Queue was initialized with maxsize=0 (the default),
        then full() is never True.
        """
        parent = self._parent
        if parent._maxsize <= 0:
            return False
        else:
            return parent._qsize() >= parent._maxsize

    async def put(self, item: T) -> None:
        """Put an item into the queue.

        Put an item into the queue. If the queue is full, wait until a free
        slot is available before adding item.

        This method is a coroutine.
        """
        parent = self._parent
        with parent._sync_mutex:
            try:
                binding = parent._get_binding_locked()
            except QueueRebindError:
                parent._reject_item(item)
                raise ItemNotDeliveredError(
                    f"{parent!r} was handed over before put({item!r}) could run; "
                    "the item was never enqueued"
                ) from None
            binding.active_callers += 1
        try:
            async with binding.mutex:
                with parent._sync_mutex:
                    if parent._is_shutdown:
                        raise AsyncQueueShutDown
                    parent._validate_binding(binding)
                    binding.active_waits += 1
                    try:
                        while True:
                            if binding.detached:
                                parent._reject_item(item)
                                raise ItemNotDeliveredError(
                                    f"{parent!r} was handed over while "
                                    f"put({item!r}) was waiting; "
                                    "the item was never enqueued"
                                )
                            if parent._is_shutdown:
                                raise AsyncQueueShutDown
                            if not (0 < parent._maxsize <= parent._qsize()):
                                break
                            binding.not_full_waiting += 1
                            parent._sync_mutex.release()
                            try:
                                await binding.not_full.wait()
                            finally:
                                parent._sync_mutex.acquire()
                                binding.not_full_waiting -= 1

                        parent._put_internal(item)
                        if binding.not_empty_waiting:
                            binding.not_empty.notify()
                        if parent._sync_not_empty_waiting:
                            parent._sync_not_empty.notify()
                    finally:
                        binding.active_waits -= 1
        finally:
            with parent._sync_mutex:
                binding.active_callers -= 1

    def put_nowait(self, item: T) -> None:
        """Put an item into the queue without blocking.

        If no free slot is immediately available, raise QueueFull.
        """
        parent = self._parent
        with parent._sync_mutex:
            if parent._is_shutdown:
                raise AsyncQueueShutDown

            binding = parent._get_binding_locked()
            parent._validate_binding(binding)
            if 0 < parent._maxsize <= parent._qsize():
                raise AsyncQueueFull

            parent._put_internal(item)
            if binding.not_empty_waiting:
                parent._notify_async(binding, binding.not_empty.notify)
            if parent._sync_not_empty_waiting:
                parent._sync_not_empty.notify()

    async def get(self) -> T:
        """Remove and return an item from the queue.

        If queue is empty, wait until an item is available.

        This method is a coroutine.
        """
        parent = self._parent
        with parent._sync_mutex:
            binding = parent._get_binding_locked()
            binding.active_callers += 1
        try:
            async with binding.mutex:
                with parent._sync_mutex:
                    if parent._is_shutdown and not parent._qsize():
                        raise AsyncQueueShutDown
                    parent._validate_binding(binding)
                    binding.active_waits += 1
                    try:
                        while True:
                            if binding.detached:
                                raise QueueRebindError(
                                    f"{parent!r} was handed over while get() was "
                                    "waiting; no item was taken"
                                )
                            if parent._is_shutdown and not parent._qsize():
                                raise AsyncQueueShutDown
                            if parent._qsize():
                                break
                            binding.not_empty_waiting += 1
                            parent._sync_mutex.release()
                            try:
                                await binding.not_empty.wait()
                            finally:
                                parent._sync_mutex.acquire()
                                binding.not_empty_waiting -= 1

                        item = parent._get()
                        parent._register_taken(item)
                        if binding.not_full_waiting:
                            binding.not_full.notify()
                        if parent._sync_not_full_waiting:
                            parent._sync_not_full.notify()
                        return item
                    finally:
                        binding.active_waits -= 1
        finally:
            with parent._sync_mutex:
                binding.active_callers -= 1

    def get_nowait(self) -> T:
        """Remove and return an item from the queue.

        Return an item if one is immediately available, else raise QueueEmpty.
        """
        parent = self._parent
        with parent._sync_mutex:
            if parent._is_shutdown and not parent._qsize():
                raise AsyncQueueShutDown
            if not parent._qsize():
                raise AsyncQueueEmpty

            binding = parent._get_binding_locked()
            parent._validate_binding(binding)
            item = parent._get()
            parent._register_taken(item)
            if binding.not_full_waiting:
                parent._notify_async(binding, binding.not_full.notify)
            if parent._sync_not_full_waiting:
                parent._sync_not_full.notify()
            return item

    def task_done(self) -> None:
        """Indicate that a formerly enqueued task is complete.

        Used by queue consumers. For each get() used to fetch a task,
        a subsequent call to task_done() tells the queue that the processing
        on the task is complete.

        If a join() is currently blocking, it will resume when all items have
        been processed (meaning that a task_done() call was received for every
        item that had been put() into the queue).

        Raises ValueError if called more times than there were items placed in
        the queue.
        """
        parent = self._parent
        with parent._sync_tasks_done:
            if parent._unfinished_tasks <= 0:
                raise ValueError("task_done() called too many times")
            parent._unfinished_tasks -= 1
            parent._register_done()
            if parent._unfinished_tasks == 0:
                binding = parent._binding
                if binding is not None and binding.tasks_done_waiting:
                    parent._notify_async(binding, binding.tasks_done.notify_all)
                if parent._sync_tasks_done_waiting:
                    parent._sync_tasks_done.notify_all()

    async def join(self) -> None:
        """Block until all items in the queue have been gotten and processed.

        The count of unfinished tasks goes up whenever an item is added to the
        queue. The count goes down whenever a consumer calls task_done() to
        indicate that the item was retrieved and all work on it is complete.
        When the count of unfinished tasks drops to zero, join() unblocks.
        """
        parent = self._parent
        with parent._sync_mutex:
            binding = parent._get_binding_locked()
            binding.active_callers += 1
        try:
            async with binding.mutex:
                with parent._sync_mutex:
                    parent._validate_binding(binding)
                    binding.active_waits += 1
                    try:
                        while True:
                            if binding.detached:
                                raise QueueRebindError(
                                    f"{parent!r} was handed over while join() was "
                                    "waiting"
                                )
                            if not parent._unfinished_tasks:
                                break
                            binding.tasks_done_waiting += 1
                            parent._sync_mutex.release()
                            try:
                                await binding.tasks_done.wait()
                            finally:
                                parent._sync_mutex.acquire()
                                binding.tasks_done_waiting -= 1
                    finally:
                        binding.active_waits -= 1
        finally:
            with parent._sync_mutex:
                binding.active_callers -= 1

    def shutdown(self, immediate: bool = False) -> None:
        """Shut-down the queue, making queue gets and puts raise an exception.

        By default, gets will only raise once the queue is empty. Set
        'immediate' to True to make gets raise immediately instead.

        All blocked callers of put() and get() will be unblocked. If
        'immediate', a task is marked as done for each item remaining in
        the queue, which may unblock callers of join().

        The raise exception is SyncQueueShutDown for sync api and AsyncQueueShutDown
        for async one.
        """
        self._parent.shutdown(immediate)


class PriorityQueue(Queue[RichComparableT]):
    """Variant of Queue that retrieves open entries in priority order
    (lowest first).

    Entries are typically tuples of the form:  (priority number, data).

    """

    def _init(self, maxsize: int) -> None:
        self._heap_queue: list[RichComparableT] = []

    def _qsize(self) -> int:
        return len(self._heap_queue)

    def _put(self, item: RichComparableT) -> None:
        heappush(self._heap_queue, item)

    def _get(self) -> RichComparableT:
        return heappop(self._heap_queue)

    def _snapshot(self) -> tuple[RichComparableT, ...]:
        # A heap's ascending order is the order heappop() yields.
        return tuple(sorted(self._heap_queue))


class LifoQueue(Queue[T]):
    """Variant of Queue that retrieves most recently added entries first."""

    def _qsize(self) -> int:
        return len(self._queue)

    def _put(self, item: T) -> None:
        self._queue.append(item)

    def _get(self) -> T:
        return self._queue.pop()

    def _snapshot(self) -> tuple[T, ...]:
        # Dequeue order is the reverse of the underlying deque.
        return tuple(reversed(self._queue))
