from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable, Collection
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from functools import wraps
from itertools import chain
from typing import Any, Literal

from zict.buffer import Buffer
from zict.common import KT, VT, T, locked


class KeyVersionChangedError(Exception):
    """Raised on the future returned by :meth:`AsyncBuffer.async_get` when the
    requested key is overwritten while its value is still being fetched from slow
    storage.

    This is deliberately *not* a :class:`KeyError` and also *not* a
    :class:`asyncio.CancelledError`: :class:`KeyError` signals that the key was
    deleted, while this exception signals that the in-flight read was fetching a stale
    version of the key and has been superseded by a new value. It must remain catchable
    by type after ``await``, which rules out subclassing :class:`asyncio.CancelledError`
    (asyncio flattens every cancelled future to a plain ``CancelledError`` once it
    crosses a task boundary).
    """

    def __init__(self, key: Any) -> None:
        super().__init__(key)
        self.key = key


class _InFlightGet:
    """Bookkeeping for a single key version currently being restored from slow.
    Several waiters, possibly running on different event loops in different threads,
    may share the same restore.
    """

    __slots__ = ("key", "waiters", "cancelled")

    def __init__(self, key: Any) -> None:
        self.key = key
        #: One asyncio future per event loop waiting on this restore
        self.waiters: dict[asyncio.AbstractEventLoop, asyncio.Future[Any]] = {}
        #: Set when the key is overwritten/deleted while the restore is running
        self.cancelled = False


def _set_result_if_pending(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _set_exception_if_pending(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _cancel_if_pending(future: asyncio.Future[Any]) -> None:
    if not future.done():
        future.cancel()


def _retrieve_exception(future: asyncio.Future[Any]) -> None:
    # Suppress "Future exception was never retrieved" for waiters whose consumer task
    # was cancelled before the restore finished (the exception state itself is kept and
    # is still raised for any active awaiter).
    if not future.cancelled():
        future.exception()


def _safe_cancel(future: asyncio.Future[Any]) -> None:
    """Cancel a future possibly bound to a different (and by now closed) event loop."""
    if future.done():
        return
    loop = future.get_loop()
    if loop.is_closed():
        return
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        # Direct cancel: takes effect synchronously, while the done callbacks (notably
        # the removal from the futures set) are deferred to the next loop iteration.
        future.cancel()
    else:
        loop.call_soon_threadsafe(_cancel_if_pending, future)


class AsyncBuffer(Buffer[KT, VT]):
    """Extension of :class:`~zict.Buffer` that allows offloading all reads and writes
    from/to slow to a separate worker thread.

    This requires ``fast`` to be fully thread-safe (e.g. a plain dict).

    ``slow.__setitem__`` and ``slow.__getitem__`` will be called from the offloaded
    thread, while all of its other methods (including, notably for the purpose of
    thread-safety consideration, ``__contains__`` and ``__delitem__``) will be called
    from the main thread.

    Overlapping :meth:`async_get` calls for the same key are coalesced, even if they
    originate from different event loops running in different threads: as long as they
    target the same version of the key, the slow storage is read only once and every
    waiter is notified when the single offloaded restore completes.

    See Also
    --------
    Buffer

    Parameters
    ----------
    Same as in Buffer, plus:

    executor: concurrent.futures.Executor, optional
        An Executor instance to use for offloading. It must not pickle/unpickle.
        Defaults to an internal ThreadPoolExecutor.
    nthreads: int, optional
        Number of offloaded threads to run in parallel. Defaults to 1.
        Mutually exclusive with executor parameter.
    """

    executor: Executor | None
    nthreads: int | None
    futures: set[asyncio.Future]
    evicting: dict[asyncio.Future, float]

    @wraps(Buffer.__init__)
    def __init__(
        self,
        *args: Any,
        executor: Executor | None = None,
        nthreads: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.executor = executor
        self.nthreads = None if executor else nthreads
        self._internal_executor = executor is None
        self.futures = set()
        self.evicting = {}
        #: In-flight restores, one entry per key version
        self._gets: dict[KT, _InFlightGet] = {}

    def close(self) -> None:
        # Call LRU.close(), which stops LRU.evict_until_below_target() halfway through
        super().close()
        with self.lock:
            for state in list(self._gets.values()):
                self._gets.pop(state.key, None)
                state.cancelled = True
                for loop, future in state.waiters.items():
                    if future.done() or loop.is_closed():
                        continue
                    loop.call_soon_threadsafe(_cancel_if_pending, future)
            futures = list(self.futures)
        for future in futures:
            _safe_cancel(future)
        if self.executor is not None and self.nthreads is not None:
            self.executor.shutdown(wait=True)
            self.executor = None

    def _track(self, future: asyncio.Future[Any]) -> asyncio.Future[Any]:
        """Register *future* so that close() can cancel it. Safe to invoke from
        different event-loop threads sharing this buffer.
        """
        # Retrieving (but not clearing) the exception keeps "never retrieved" warnings
        # quiet without affecting any real awaiter.
        future.add_done_callback(_retrieve_exception)
        with self.lock:
            self.futures.add(future)
        future.add_done_callback(self._untrack)
        return future

    def _untrack(self, future: asyncio.Future[Any]) -> None:
        with self.lock:
            self.futures.discard(future)

    def _submit(self, func: Callable[..., T], *args: Any) -> Future[T]:
        """Submit *func* to the shared executor, independent of any event loop."""
        if self.executor is None:
            assert self.nthreads
            self.executor = ThreadPoolExecutor(
                self.nthreads, thread_name_prefix="zict.AsyncBuffer offloader"
            )

        context = contextvars.copy_context()
        return self.executor.submit(context.run, func, *args)

    def _offload(self, func: Callable[..., T], *args: Any) -> asyncio.Future[T]:
        return self._track(  # type: ignore[return-value]
            asyncio.wrap_future(self._submit(func, *args))
        )

    def _run_gets(self, owned: list[tuple[KT, _InFlightGet]]) -> None:
        """Worker-thread function restoring the keys owned by a single async_get call.
        Keys that other concurrent calls are already restoring are not included, so each
        key version is read from slow at most once.
        """
        for key, state in owned:
            if self.fast.closed:
                exc: BaseException | None = asyncio.CancelledError()
                value: VT | None = None
                ok = False
            else:
                # slow_to_fast() relies on the buffer RLock being held: it calls
                # unlock() around the slow read and re-acquires it afterwards, which
                # serializes the restore against concurrent set/del of the same key.
                try:
                    with self.lock:
                        # Buffer.slow_to_fast raises KeyError if the key is
                        # deleted/overwritten mid-read (see _cancel_restore), so a stale
                        # value never lands in fast.
                        value = self.slow_to_fast(key)
                except (Exception, asyncio.CancelledError) as e:
                    exc = e
                    value = None
                    ok = False
                else:
                    exc = None
                    ok = True

            with self.lock:
                # The key version changed while reading, or close() happened: waiters
                # have already been notified and this restore must not touch the
                # current state of the key.
                if state.cancelled or self._gets.get(key) is not state:
                    continue
                self._gets.pop(key, None)
                if ok:
                    for loop, future in state.waiters.items():
                        if future.done() or loop.is_closed():
                            continue
                        loop.call_soon_threadsafe(
                            _set_result_if_pending, future, value
                        )
                else:
                    assert exc is not None
                    for loop, future in state.waiters.items():
                        if future.done() or loop.is_closed():
                            continue
                        loop.call_soon_threadsafe(
                            _set_exception_if_pending, future, exc
                        )

    def _cancel_get(self, key: KT, exc: BaseException) -> None:
        """Notify every waiter on *key* that its in-flight read is obsolete.

        Must be called while holding ``self.lock``.
        """
        state = self._gets.pop(key, None)
        if state is None:
            return
        state.cancelled = True
        for loop, future in state.waiters.items():
            if future.done() or loop.is_closed():
                continue
            loop.call_soon_threadsafe(_set_exception_if_pending, future, exc)

    # Return an asyncio.Future, instead of just writing it as an async function, to make
    # it easier for overriders to tell apart the use case when all keys were already
    # in fast
    @locked
    def async_get(
        self, keys: Collection[KT], missing: Literal["raise", "omit"] = "raise"
    ) -> asyncio.Future[dict[KT, VT]]:
        """Fetch one or more key/value pairs. If not all keys are available in fast,
        offload to a worker thread moving keys from slow to fast, as well as possibly
        moving older keys from fast to slow.

        Overlapping requests for the same key, including requests issued from different
        event loops (and thus different threads) sharing this buffer, are served by a
        single read from slow storage. If a key is overwritten while a restore is in
        flight, the waiting futures fail with :class:`KeyVersionChangedError`; if it is
        deleted instead, they fail with :class:`KeyError` (subject to *missing*). The
        two outcomes are catchable separately.

        Parameters
        ----------
        keys:
            collection of zero or more keys to get. Duplicate keys are fetched once and
            appear once in the returned dict; the input ordering is preserved.
        missing: raise or omit, optional
            raise (default)
                If any key is missing, raise KeyError.
            omit
                If a key is missing, return a dict with less keys than those requested.

        Notes
        -----
        All keys may be present when you call ``async_get``, but ``__delitem__`` may be
        called on one of them before the actual data is fetched. ``__setitem__`` also
        internally calls ``__delitem__`` in a non-atomic way, so you may get
        ``KeyError`` when updating a value too.
        """
        # This block avoids spawning a thread if keys are missing from both fast and
        # slow. It is otherwise just a performance optimization.
        if missing == "omit":
            keys = [key for key in keys if key in self]
        elif missing == "raise":
            for key in keys:
                if key not in self:
                    raise KeyError(key)
        else:
            raise ValueError(f"missing: expected raise or omit; got {missing}")
        # End performance optimization

        # Fetch each key only once, but remember the order of the batch
        unique_keys = list(dict.fromkeys(keys))

        # Fast path: nothing to offload, so do not involve the executor at all
        try:
            # Do not pull keys towards the top of the LRU unless they are all available.
            # This matters when there is a very long queue of async_get futures.
            d = self.fast.get_all_or_nothing(unique_keys)
        except KeyError:
            pass
        else:
            f: asyncio.Future[dict[KT, VT]] = asyncio.Future()
            f.set_result(d)
            return f

        loop = asyncio.get_running_loop()
        waiters: dict[KT, asyncio.Future[VT]] = {}
        owned: list[tuple[KT, _InFlightGet]] = []
        for key in unique_keys:
            if key in self.fast:
                continue
            state = self._gets.get(key)
            if state is not None and loop in state.waiters:
                # Same loop already waiting on the same key version: join its future
                waiters[key] = state.waiters[loop]
                continue

            future: asyncio.Future[VT] = loop.create_future()
            future.add_done_callback(_retrieve_exception)
            if state is None:
                # First request for this key version: this batch owns the single slow
                # read; later waiters just attach to it
                state = _InFlightGet(key)
                self._gets[key] = state
                owned.append((key, state))
            state.waiters[loop] = future
            waiters[key] = future

        if owned:
            self._submit(self._run_gets, owned)

        # Plain future (no wrapping Task): cancellation takes effect synchronously and
        # there is no task that can be "destroyed pending" if the loop closes early.
        # It is driven to completion by callbacks on the per-key waiter futures.
        result_future: asyncio.Future[dict[KT, VT]] = loop.create_future()

        def _on_waiter_done(_: asyncio.Future[Any]) -> None:
            if result_future.done():
                return
            # First, wait for every slow-key restore to settle
            for waiter in waiters.values():
                if not waiter.done():
                    return
            result: dict[KT, VT] = {}
            for key in unique_keys:
                waiter = waiters.get(key)
                if waiter is not None:
                    exc = waiter.exception()
                    if exc is not None:
                        # KeyError (deleted) is swallowed in omit mode; version-change
                        # and every other exception propagate
                        if isinstance(exc, KeyError) and missing == "omit":
                            continue
                        result_future.set_exception(exc)
                        return
                    # Use the restored value directly: a heavy value may never be
                    # placed in fast (see Buffer.slow_to_fast)
                    result[key] = waiter.result()
                else:
                    # Key was already in fast when async_get was called
                    try:
                        result[key] = self.fast[key]
                    except KeyError:
                        if missing == "raise":
                            result_future.set_exception(KeyError(key))
                            return
            result_future.set_result(result)

        for waiter in waiters.values():
            waiter.add_done_callback(_on_waiter_done)
        if not waiters:
            # All keys were in fast; resolve immediately on the next loop iteration
            loop.call_soon(_on_waiter_done, result_future)

        self._track(result_future)
        return result_future

    def __setitem__(self, key: KT, value: VT) -> None:
        """Immediately set a key in fast. If this causes the total weight to exceed n,
        asynchronously start moving keys from fast to slow in a worker thread.
        """
        self.set_noevict(key, value)
        self.async_evict_until_below_target()

    @locked
    def set_noevict(self, key: KT, value: VT) -> None:
        self._cancel_get(key, KeyVersionChangedError(key))
        super().set_noevict(key, value)

    @locked
    def __delitem__(self, key: KT) -> None:
        self._cancel_get(key, KeyError(key))
        super().__delitem__(key)

    @locked
    def async_evict_until_below_target(self, n: float | None = None) -> None:
        """If the total weight exceeds n, asynchronously start moving keys from fast to
        slow in a worker thread.
        """
        if n is None:
            n = self.n
        n = max(0.0, n)
        weight = min(chain([self.fast.total_weight], self.evicting.values()))
        if weight <= n:
            return

        # Note: this can get cancelled by LRU.close(), which in turn is
        # triggered by Buffer.close()
        future = self._offload(self.evict_until_below_target, n)
        self.evicting[future] = n
        future.add_done_callback(self._remove_evicting)

    def _remove_evicting(self, future: asyncio.Future[Any]) -> None:
        # The done callback may fire on any of the event loops sharing this buffer
        with self.lock:
            self.evicting.pop(future, None)
