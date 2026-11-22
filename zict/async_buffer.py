from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable, Collection
from concurrent.futures import (
    Executor,
    Future as ConcurrentFuture,
    ThreadPoolExecutor,
)
from functools import wraps
from itertools import chain
from typing import Any, Literal

from zict.buffer import Buffer
from zict.common import KT, VT, T, locked


class AsyncBuffer(Buffer[KT, VT]):
    """Extension of :class:`~zict.Buffer` that allows offloading all reads and writes
    from/to slow to a separate worker thread.

    This requires ``fast`` to be fully thread-safe (e.g. a plain dict).

    ``slow.__setitem__`` and ``slow.__getitem__`` will be called from the offloaded
    thread, while all of its other methods (including, notably for the purpose of
    thread-safety consideration, ``__contains__`` and ``__delitem__``) will be called
    from the main thread.

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
    #: Set as soon as :meth:`close` (or :meth:`async_close`) is invoked for the first
    #: time, from any thread. Once set, no new work is offloaded anymore.
    _closing: bool
    #: Shared outcome of the shutdown. Every caller of close/async_close gets this very
    #: same future, so a failure while flushing is re-raised to all of them and a
    #: successful close cannot be observed as such only by some callers.
    _close_future: ConcurrentFuture[None] | None
    #: Offloaded work that has been accepted and not yet settled, keyed by the
    #: underlying concurrent future. Unlike the asyncio wrappers in :attr:`futures`,
    #: these can be waited upon (``.result()``) from any thread.
    _pending: set[ConcurrentFuture[Any]]
    #: Maps each accepted concurrent future to the asyncio wrapper exposed to callers.
    #: It lets close() flip the wrappers to cancelled synchronously when invoked from
    #: the event loop thread, mirroring the historical run_in_executor behaviour.
    _wrappers: dict[ConcurrentFuture[Any], asyncio.Future[Any]]

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
        self._closing = False
        self._close_future = None
        self._pending = set()
        self._wrappers = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None

    def close(self) -> None:
        """Synchronously close the buffer and block until the shutdown completes.

        In-flight offloaded work is allowed to finish instead of being cancelled, so
        that data already accepted for writing to slow is either fully persisted or
        cleaned up by the underlying layers. Work submitted after the close has started
        is rejected with :class:`asyncio.CancelledError`.

        Repeated calls retrieve the very same outcome. Do not invoke this blocking
        method from within an offloaded callback running on a single-worker executor:
        the shutdown has to wait for that callback to return, which would deadlock.
        Call :meth:`async_close` from a callback instead and await its result from the
        event loop.
        """
        close_future = self.async_close()
        try:
            close_future.result()
        finally:
            # Make sure the helper thread is fully gone before returning, so that no
            # off-thread outlives the (synchronous) close() call.
            shutdown_thread = self._shutdown_thread
            if (
                shutdown_thread is not None
                and shutdown_thread is not threading.current_thread()
            ):
                shutdown_thread.join()

    def __del__(self) -> None:
        # Best effort only. If the shutdown failed, its exception was already delivered
        # to whoever awaited close()/async_close(); re-raising it from the garbage
        # collector would just produce an unraisable exception.
        try:
            self.close()
        except BaseException:
            pass

    def async_close(self) -> ConcurrentFuture[None]:
        """Request a close without waiting for it to complete.

        This is the entry point meant to be invoked either from the event loop or from
        within a running offloaded callback. The returned future resolves once all
        accepted writes have settled and the resources have been released; a callback
        may keep it and hand it back to the event loop, without waiting on it itself
        (which would deadlock a single-worker executor).

        Repeated calls return the same future.
        """
        with self.lock:
            if self._close_future is not None:
                return self._close_future
            self._closing = True
            # Note: LRU.closed is deliberately NOT flipped here. It would make an
            # already-accepted eviction stop halfway through, dropping writes that were
            # accepted before the close. New work is rejected through self._closing
            # instead, while accepted evictions run to completion; Buffer.close(),
            # which flips LRU.closed and releases fast/slow, runs in the shutdown
            # thread once everything has settled.

            close_future: ConcurrentFuture[None] = ConcurrentFuture()
            self._close_future = close_future
            executor = self.executor
            internal = self._internal_executor
            # No further work can be accepted past this point, so this snapshot covers
            # every write the close has to wait for.
            pending = list(self._pending)
            wrappers = list(self._wrappers.values())
            loop = self._loop

        # Mirror the historical run_in_executor lifecycle: when close is requested from
        # the event loop thread, flip the exposed asyncio wrappers to cancelled right
        # away, so that awaiting them raises CancelledError without yielding and they
        # stay in self.futures until the loop gets to run their callbacks. The accepted
        # work itself keeps running and is independently awaited by the shutdown thread.
        # From a worker thread, defer the cancellation to the loop instead of touching
        # asyncio objects from the wrong thread.
        if loop is not None and loop.is_running():
            if threading.current_thread() is self._loop_thread:
                for wrapper in wrappers:
                    if not wrapper.done():
                        wrapper.cancel()
            else:
                loop.call_soon_threadsafe(_cancel_all, wrappers)

        if executor is None:
            # Internal executor that was never used: nothing was ever offloaded, so the
            # underlying mappings can be closed straight away.
            assert internal and not pending
            try:
                Buffer.close(self)
            except BaseException as exc:
                close_future.set_exception(exc)
            else:
                close_future.set_result(None)
            return close_future

        def _shutdown() -> None:
            # Runs on a dedicated thread (never on the offload executor itself, which
            # would deadlock a single-worker pool) and waits for every accepted write,
            # including the callback that requested the close and anything queued
            # behind it. The first failure is remembered and broadcast through the
            # shared future, but all work is still awaited and the executor is shut
            # down exactly once.
            error: BaseException | None = None
            for future in pending:
                try:
                    future.result()
                except asyncio.CancelledError:
                    # The work bailed out on its own because LRU.closed is set (see
                    # async_get()). This is the expected outcome of a close, not a
                    # failure of the shutdown.
                    continue
                except BaseException as exc:
                    if error is None:
                        error = exc

            # Only now that the last accepted write and its cleanup have settled is it
            # safe to release the underlying fast/slow resources.
            try:
                Buffer.close(self)
            except BaseException as exc:
                if error is None:
                    error = exc

            try:
                if internal:
                    executor.shutdown(wait=True)
                    with self.lock:
                        if self.executor is executor:
                            self.executor = None
            except BaseException as exc:  # pragma: no cover - defensive
                if not close_future.done():
                    close_future.set_exception(exc)
                return
            if not close_future.done():
                if error is not None:
                    close_future.set_exception(error)
                else:
                    close_future.set_result(None)

        shutdown_thread = threading.Thread(
            target=_shutdown,
            name="zict.AsyncBuffer shutdown",
            daemon=True,
        )
        self._shutdown_thread = shutdown_thread
        shutdown_thread.start()
        return close_future

    def _offload(self, func: Callable[..., T], *args: Any) -> asyncio.Future[T]:
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        with self.lock:
            if self._closing:
                # The close has already been requested (possibly from within an
                # offloaded callback): don't accept any new work.
                rejected: asyncio.Future[T] = loop.create_future()
                rejected.cancel()
                return rejected
            if self.executor is None:
                assert self.nthreads
                self.executor = ThreadPoolExecutor(
                    self.nthreads, thread_name_prefix="zict.AsyncBuffer offloader"
                )
            if self._loop is None:
                self._loop = loop
                self._loop_thread = threading.current_thread()
            executor = self.executor
            # Submit and register under the same lock, so async_close() cannot start in
            # between the rejection check and the registration: anything registered
            # here is guaranteed to be awaited by the shutdown thread.
            concurrent_future = executor.submit(context.run, func, *args)
            self._pending.add(concurrent_future)

        async_future: asyncio.Future[T] = loop.create_future()

        def _propagate(_: ConcurrentFuture[Any]) -> None:
            # Hand the outcome over to the event loop. Note that cancelling
            # async_future (a waiter giving up, or close() flipping it) must not cancel
            # the accepted work: the close still waits on concurrent_future
            # independently. The *_if_not_done helpers make propagation a no-op if the
            # wrapper was cancelled in the meantime.
            if concurrent_future.cancelled():
                loop.call_soon_threadsafe(_cancel_if_not_done, async_future)
                return
            exc = concurrent_future.exception()
            if exc is not None:
                loop.call_soon_threadsafe(
                    _set_exception_if_not_done, async_future, exc
                )
            else:
                loop.call_soon_threadsafe(
                    _set_result_if_not_done, async_future, concurrent_future.result()
                )

        def _cleanup(_: ConcurrentFuture[Any]) -> None:
            # Runs on an offload thread, while async_close() may snapshot the same
            # collections from another thread: take the lock to stay consistent.
            with self.lock:
                self._pending.discard(concurrent_future)
                self._wrappers.pop(concurrent_future, None)

        concurrent_future.add_done_callback(_cleanup)
        concurrent_future.add_done_callback(_propagate)

        def _discard_async_future(_: asyncio.Future[Any]) -> None:
            self.futures.discard(async_future)

        self.futures.add(async_future)
        self._wrappers[concurrent_future] = async_future
        async_future.add_done_callback(_discard_async_future)
        return async_future

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

        Parameters
        ----------
        keys:
            collection of zero or more keys to get
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

        try:
            # Do not pull keys towards the top of the LRU unless they are all available.
            # This matters when there is a very long queue of async_get futures.
            d = self.fast.get_all_or_nothing(keys)
        except KeyError:
            pass
        else:
            f: asyncio.Future[dict[KT, VT]] = asyncio.Future()
            f.set_result(d)
            return f

        def _async_get() -> dict[KT, VT]:
            d = {}
            for k in keys:
                if self._closing or self.fast.closed:
                    raise asyncio.CancelledError()
                try:
                    # This can cause keys to be restored and older keys to be evicted
                    d[k] = self[k]
                except KeyError:
                    # Race condition: key was there when async_get was called, but got
                    # deleted afterwards.
                    if missing == "raise":
                        raise
            return d

        return self._offload(_async_get)

    def __setitem__(self, key: KT, value: VT) -> None:
        """Immediately set a key in fast. If this causes the total weight to exceed n,
        asynchronously start moving keys from fast to slow in a worker thread.
        """
        self.set_noevict(key, value)
        self.async_evict_until_below_target()

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

        # This gets rejected with a cancelled future if close()/async_close() has
        # already started. Any eviction accepted beforehand still runs to completion
        # and is awaited by the shutdown before the underlying mappings are closed.
        future = self._offload(self.evict_until_below_target, n)
        if future.cancelled():
            return
        self.evicting[future] = n
        future.add_done_callback(self.evicting.__delitem__)


def _set_result_if_not_done(future: asyncio.Future[T], result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception_if_not_done(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _cancel_if_not_done(future: asyncio.Future[Any]) -> None:
    if not future.done():
        future.cancel()


def _cancel_all(futures: Collection[asyncio.Future[Any]]) -> None:
    for future in futures:
        _cancel_if_not_done(future)
