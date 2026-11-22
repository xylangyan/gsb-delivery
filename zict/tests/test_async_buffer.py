import asyncio
import contextlib
import contextvars
import threading
import time
from collections import UserDict
from concurrent.futures import Executor, Future, ThreadPoolExecutor

import pytest

from zict import AsyncBuffer, Func
from zict.async_buffer import KeyVersionChangedError
from zict.tests import utils_test


class _BlockingSlowDict(UserDict):
    """Slow storage whose __getitem__ blocks until ``block`` is set."""

    def __init__(self):
        super().__init__()
        self.in_get = threading.Event()
        self.block = threading.Event()
        self.n_getitem = 0

    def __getitem__(self, key):
        self.n_getitem += 1
        snapshot = self.data.get(key)
        self.in_get.set()
        assert self.block.wait(timeout=10)
        if key not in self.data:
            raise KeyError(key)
        return snapshot


class _CountingExecutor(ThreadPoolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_submit = 0

    def submit(self, fn, /, *args, **kwargs):
        self.n_submit += 1
        return super().submit(fn, *args, **kwargs)


def _run_in_new_loop(coro_factory):
    """Run ``coro_factory()`` in a brand-new event loop, in a dedicated thread."""
    outcome = {}

    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            outcome["value"] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # captured, re-raised by the caller via join
            outcome["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


@pytest.mark.asyncio
async def test_simple(check_thread_leaks):
    with AsyncBuffer({}, utils_test.SlowDict(0.01), n=3) as buff:
        buff["a"] = 1
        buff["b"] = 2
        buff["c"] = 3
        assert set(buff.fast) == {"a", "b", "c"}
        assert not buff.slow
        assert not buff.futures

        buff["d"] = 4
        assert set(buff.fast) == {"a", "b", "c", "d"}
        assert not buff.slow
        assert buff.futures
        await asyncio.wait(buff.futures)
        assert set(buff.fast) == {"b", "c", "d"}
        assert set(buff.slow) == {"a"}

        buff.async_evict_until_below_target()
        assert not buff.futures
        buff.async_evict_until_below_target(10)
        assert not buff.futures
        buff.async_evict_until_below_target(2)
        assert buff.futures
        await asyncio.wait(buff.futures)
        assert set(buff.fast) == {"c", "d"}
        assert set(buff.slow) == {"a", "b"}

        # Do not incur in threading sync cost if everything is in fast
        assert list(buff.fast.order) == ["c", "d"]
        future = buff.async_get(["c"])
        assert future.done()
        assert await future == {"c": 3}
        assert list(buff.fast.order) == ["d", "c"]

        # Do not disturb LRU order in case of missing keys
        with pytest.raises(KeyError, match="m"):
            _ = buff.async_get(["d", "m"], missing="raise")
        assert list(buff.fast.order) == ["d", "c"]

        future = buff.async_get(["d", "m"], missing="omit")
        assert future.done()
        assert await future == {"d": 4}
        assert list(buff.fast.order) == ["c", "d"]

        with pytest.raises(ValueError):
            _ = buff.async_get(["a"], missing="misspell")

        # Asynchronously retrieve from slow
        future = buff.async_get(["a", "b"])
        assert not future.done()
        assert future in buff.futures
        assert await future == {"a": 1, "b": 2}
        assert not buff.futures
        assert set(buff.fast) == {"d", "a", "b"}
        assert set(buff.slow) == {"c"}


@pytest.mark.asyncio
async def test_double_evict(check_thread_leaks):
    """User calls async_evict_until_below_target() while the same is already running"""
    with AsyncBuffer({}, utils_test.SlowDict(0.01), n=3) as buff:
        buff["x"] = 1
        buff["y"] = 2
        buff["z"] = 3
        assert len(buff.fast) == 3
        assert not buff.futures

        buff.async_evict_until_below_target(2)
        assert len(buff.futures) == 1
        assert list(buff.evicting.values()) == [2]

        # Evicting to the same n is a no-op
        buff.async_evict_until_below_target(2)
        assert len(buff.futures) == 1
        assert list(buff.evicting.values()) == [2]

        # Evicting to a lower n while a previous eviction is still running does not
        # cancel the previous eviction
        buff.async_evict_until_below_target(1)
        assert len(buff.futures) == 2
        assert list(buff.evicting.values()) == [2, 1]
        await asyncio.wait(buff.futures, return_when=asyncio.FIRST_COMPLETED)
        assert len(buff.futures) == 1
        assert list(buff.evicting.values()) == [1]
        await asyncio.wait(buff.futures)
        assert not buff.futures
        assert not buff.evicting

        assert buff.fast == {"z": 3}
        assert buff.slow.data == {"x": 1, "y": 2}

        # Evicting to negative n while fast is empty does nothing
        buff.evict_until_below_target(0)
        buff.async_evict_until_below_target(-1)
        assert not buff.futures
        assert not buff.evicting


@pytest.mark.asyncio
async def test_close_during_evict(check_thread_leaks):
    buff = AsyncBuffer({}, utils_test.SlowDict(0.01), n=100)
    buff.update({i: i for i in range(100)})
    assert not buff.futures
    assert len(buff.fast) == 100

    buff.async_evict_until_below_target(0)
    while not buff.slow:
        await asyncio.sleep(0.01)
    assert buff.fast
    assert buff.futures

    buff.close()
    await asyncio.wait(buff.futures)
    assert not buff.futures
    assert buff.fast
    assert buff.slow


@pytest.mark.asyncio
async def test_close_during_get(check_thread_leaks):
    buff = AsyncBuffer({}, utils_test.SlowDict(0.01), n=100)
    buff.slow.data.update({i: i for i in range(100)})
    assert len(buff) == 100
    assert not buff.fast

    future = buff.async_get(list(range(100)))
    assert buff.futures
    while not buff.fast:
        await asyncio.sleep(0.01)

    buff.close()
    with pytest.raises(asyncio.CancelledError):
        await future
    await asyncio.wait(buff.futures)
    assert not buff.futures

    assert buff.fast
    assert buff.slow


@pytest.mark.asyncio
async def test_contextvars(check_thread_leaks):
    ctx = contextvars.ContextVar("v", default=0)
    in_dump = threading.Event()
    in_load = threading.Event()
    block_dump = threading.Event()
    block_load = threading.Event()

    def dump(v):
        in_dump.set()
        assert block_dump.wait(timeout=5)
        return v + ctx.get()

    def load(v):
        in_load.set()
        assert block_load.wait(timeout=5)
        return v + ctx.get()

    with AsyncBuffer({}, Func(dump, load, {}), n=0.1) as buff:
        ctx.set(20)  # Picked up by dump
        buff["x"] = 1
        assert buff.futures
        assert in_dump.wait(timeout=5)
        ctx.set(300)  # Changed while dump runs. Won't be picked up until load.
        block_dump.set()
        await asyncio.wait(buff.futures)
        assert buff.slow.d == {"x": 21}
        fut = buff.async_get(["x"])
        assert in_load.wait(timeout=5)
        ctx.set(4000)  # Changed while load runs. Won't be picked up.
        block_load.set()
        assert await fut == {"x": 321}  # 1 + 20 (added by dump) + 300 (added by load)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["raise", "omit"])
async def test_race_condition_get_async_delitem(check_thread_leaks, missing):
    """All required keys exist in slow when you call get_async(); however some are
    deleted by the time the offloaded thread retrieves their values.
    """

    class Slow(UserDict):
        def __getitem__(self, key):
            if key in self:
                time.sleep(0.01)
            return super().__getitem__(key)

    with AsyncBuffer({}, Slow(), n=100) as buff:
        buff.slow.update({i: i for i in range(100)})
        assert len(buff) == 100

        future = buff.async_get(list(range(100)), missing=missing)
        while not buff.fast:
            await asyncio.sleep(0.01)
        assert buff.slow
        # Don't use clear(); it uses __iter__ which would not return until restore is
        # completed
        for i in range(100):
            del buff[i]
        assert not buff.fast
        assert not buff.slow
        assert not future.done()

        if missing == "raise":
            with pytest.raises(KeyError):
                await future
        else:
            out = await future
            assert 0 < len(out) < 100


@pytest.mark.asyncio
async def test_multiple_offload_threads():
    barrier = threading.Barrier(2)

    class Slow(UserDict):
        def __getitem__(self, key):
            barrier.wait(timeout=5)
            return super().__getitem__(key)

    with AsyncBuffer({}, Slow(), n=100, nthreads=2) as buff:
        buff["x"] = 1
        buff["y"] = 2
        buff.evict_until_below_target(0)
        assert not buff.fast
        assert set(buff.slow) == {"x", "y"}

        out = await asyncio.gather(buff.async_get(["x"]), buff.async_get(["y"]))
        assert out == [{"x": 1}, {"y": 2}]


@pytest.mark.asyncio
async def test_external_executor():
    n_submit = 0

    class MyExecutor(Executor):
        def submit(self, fn, /, *args, **kwargs):
            nonlocal n_submit
            n_submit += 1
            out = fn(*args, **kwargs)
            f = Future()
            f.set_result(out)
            return f

        def shutdown(self, *args, **kwargs):
            raise AssertionError("AsyncBuffer.close() called executor.shutdown()")

    ex = MyExecutor()
    buff = AsyncBuffer({}, {}, n=1, executor=ex)
    buff["x"] = 1
    buff["y"] = 2  # Evict x
    assert buff.fast.d == {"y": 2}
    assert buff.slow == {"x": 1}
    assert await buff.async_get(["x"]) == {"x": 1}  # Restore x, evict y
    assert buff.fast.d == {"x": 1}
    assert buff.slow == {"y": 2}
    assert n_submit == 2
    buff.close()


@pytest.mark.asyncio
async def test_coalesce_same_loop(check_thread_leaks):
    """Two overlapping requests from the same loop share a single slow read."""
    slow = _BlockingSlowDict()
    slow["x"] = 1
    with AsyncBuffer({}, slow, n=100) as buff:
        first = buff.async_get(["x"])
        assert slow.in_get.wait(timeout=5)

        second = buff.async_get(["x"])
        assert not second.done()
        # Both requests are attached to the same in-flight restore
        with buff.lock:
            assert len(buff._gets["x"].waiters) == 1

        slow.block.set()
        assert await first == {"x": 1}
        assert await second == {"x": 1}
        assert slow.n_getitem == 1


def test_coalesce_two_event_loops(check_thread_leaks):
    """Two partitions, each with its own event loop, share one AsyncBuffer and one
    thread executor. Overlapping requests for the same key version trigger a single
    slow read. One side times out and closes its loop; the other is unaffected.
    """
    slow = _BlockingSlowDict()
    slow["x"] = "old-x"
    executor = _CountingExecutor(1)
    buff = AsyncBuffer({}, slow, n=100, executor=executor)
    # A key already resident in fast
    buff["h"] = "hot"
    try:
        async def side_a():
            future = buff.async_get(["x", "h"])
            # shield(): the timeout must not cancel the shared restore
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=0.3)
            except asyncio.TimeoutError:
                # Give up on this side and wind the local future down before the loop
                # closes. The shared restore and the other loop's waiter keep running.
                future.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await future
                raise  # Propagated to the main thread through _run_in_new_loop

        thread_a, outcome_a = _run_in_new_loop(side_a)
        assert slow.in_get.wait(timeout=5)
        # The first side owns the single read of the slow block
        assert slow.n_getitem == 1
        assert executor.n_submit == 1

        async def side_b():
            # "x" appears twice, "h" is a hot hit, "m" does not exist
            future = buff.async_get(["x", "x", "h", "m"], missing="omit")
            assert not future.done()
            return await future

        thread_b, outcome_b = _run_in_new_loop(side_b)

        # Both event loops are now waiting on the same restore
        while True:
            with buff.lock:
                state = buff._gets.get("x")
                if state is not None and len(state.waiters) == 2:
                    break
            time.sleep(0.01)

        thread_a.join(timeout=5)
        assert not thread_a.is_alive()
        assert isinstance(outcome_a.get("error"), asyncio.TimeoutError)
        # Side A's event loop is gone, but side B keeps running
        assert thread_b.is_alive()

        slow.block.set()
        thread_b.join(timeout=5)
        assert not thread_b.is_alive()
        assert "error" not in outcome_b
        result = outcome_b["value"]
        # Both duplicate positions for x are filled, the missing slot m is omitted and
        # the batch ordering is preserved
        assert list(result) == ["x", "h"]
        assert result == {"x": "old-x", "h": "hot"}
        # The same version was read from slow exactly once
        assert slow.n_getitem == 1

        # A batch fully served from fast requires no executor work at all
        async def hot_only():
            future = buff.async_get(["h"])
            assert future.done()
            return await future

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(hot_only()) == {"h": "hot"}
        finally:
            loop.close()
        assert executor.n_submit == 1
    finally:
        slow.block.set()
        buff.close()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_version_change_during_get(check_thread_leaks):
    """An old-version slow read stays in the executor while the key is replaced.
    The old waiter receives a version-change result (distinct from deletion), later
    requests cannot join the obsolete read, and the stale return must not overwrite or
    resurrect the current key.
    """
    slow = _BlockingSlowDict()
    slow["x"] = "old-x"
    executor = _CountingExecutor(1)
    try:
        with AsyncBuffer({}, slow, n=100, executor=executor) as buff:
            old_future = buff.async_get(["x"])
            assert slow.in_get.wait(timeout=5)
            assert not old_future.done()
            assert executor.n_submit == 1

            # Replace the key while the old-version read is stuck in the worker thread
            buff["x"] = "new-x"
            with pytest.raises(KeyVersionChangedError, match="x") as info:
                await old_future
            assert info.value.key == "x"
            assert not buff._gets

            # A later request must not attach to the obsolete read; the new value is
            # already in fast, so no additional thread read is scheduled
            later = buff.async_get(["x"])
            assert later.done()
            assert await later == {"x": "new-x"}
            assert executor.n_submit == 1

            # Release the old slow read: nothing stale may overwrite or resurrect the
            # key
            slow.block.set()
            await asyncio.sleep(0.1)
            assert buff.fast["x"] == "new-x"
            assert "x" not in buff.slow
            assert slow.n_getitem == 1
    finally:
        # An externally provided executor is not owned/shut down by AsyncBuffer.close()
        slow.block.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["set", "delete"])
async def test_get_invalidation_set_vs_delete(check_thread_leaks, mode):
    """Overwriting a key mid-read is reported differently from deleting it."""
    slow = _BlockingSlowDict()
    slow["x"] = 1
    with AsyncBuffer({}, slow, n=100) as buff:
        raise_future = buff.async_get(["x"])
        omit_future = buff.async_get(["x"], missing="omit")
        assert slow.in_get.wait(timeout=5)

        if mode == "set":
            buff["x"] = 2
            with pytest.raises(KeyVersionChangedError):
                await raise_future
            with pytest.raises(KeyVersionChangedError):
                await omit_future
            assert buff.fast["x"] == 2
        else:
            del buff["x"]
            with pytest.raises(KeyError):
                await raise_future
            assert await omit_future == {}
            assert "x" not in buff

        slow.block.set()
        await asyncio.sleep(0.05)
