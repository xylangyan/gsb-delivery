import asyncio
import contextvars
import threading
import time
from collections import UserDict
from concurrent.futures import Executor, Future, ThreadPoolExecutor

import pytest

from zict import AsyncBuffer, Func
from zict.tests import utils_test


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
async def test_shared_payload_reduction_and_replacement(check_thread_leaks):
    """A blocked eviction of one reference to a shared payload must not make a
    concurrent reduction spill the other reference, and its late success must be
    undone if the key was replaced meanwhile."""
    mib = 1024 * 1024

    class Slow(UserDict):
        def __init__(self):
            super().__init__()
            self.enter = threading.Event()
            self.release = threading.Event()

        def __setitem__(self, k, v):
            if k == "B" and not self.enter.is_set():
                self.enter.set()
                assert self.release.wait(timeout=5)
            super().__setitem__(k, v)

    slow = Slow()
    ex = ThreadPoolExecutor(1)
    buff = AsyncBuffer(
        {},
        slow,
        n=10 * mib,
        executor=ex,
        weight=lambda k, v: v[1],
        payload=lambda k, v: v[0],
    )
    loop = asyncio.get_running_loop()
    try:
        buff.set_noevict("A", ("p", 6 * mib))
        buff.set_noevict("B", ("p", 6 * mib))
        buff.set_noevict("C", ("c", 4 * mib))
        assert buff.fast.total_weight == 10 * mib

        # Block B's on_evict callback on the sole writer thread
        fut = loop.run_in_executor(ex, buff.fast.evict, "B")
        assert await loop.run_in_executor(None, slow.enter.wait, 5)
        assert "B" in buff.fast
        assert buff.fast.total_weight == 10 * mib
        assert buff.fast.payload_refs["p"] == 2

        # Accept another reduction while B is still in fast
        buff.async_evict_until_below_target(6 * mib)
        await asyncio.sleep(0.05)
        # A must not be treated as the last reference before B is decided
        assert buff.fast.payload_refs["p"] == 2
        assert {"A", "B", "C"} <= set(buff.fast)
        assert buff.fast.total_weight == 10 * mib

        # Replace B with a new 2 MiB payload through the normal write entry
        buff["B"] = ("q", 2 * mib)
        assert buff.fast.payload_refs["p"] == 1

        # The old callback arrives late and succeeds
        slow.release.set()
        assert await fut == (None, None, 0)
        while buff.futures:
            await asyncio.wait(buff.futures)

        assert buff.fast["B"] == ("q", 2 * mib)
        assert "B" not in buff.slow
        # A became the sole fast reference of the old payload and was spilled;
        # only new B (2 MiB) and C (4 MiB) remain
        assert set(buff.fast) == {"B", "C"}
        assert buff.fast.total_weight == 6 * mib
        assert set(buff.slow) == {"A"}
    finally:
        slow.release.set()
        buff.close()
        ex.shutdown(wait=True)

