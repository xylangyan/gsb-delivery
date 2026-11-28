"""Deterministic tests for shared open and segment finalization.

The controllable backend below performs real pread/pwrite against the actual
file descriptor, but lets a test hold an individual write in flight, fail it,
and observe the exact ordering of writes, durability flushes and closes.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aiofile import (
    AIOFile, RecordState, SegmentRegistry,
)


class GatedExecutor(ThreadPoolExecutor):
    """Executor whose jobs do not run until the gate is opened."""

    _instances: "list[GatedExecutor]" = []

    def __init__(self) -> None:
        super().__init__(max_workers=4)
        self._event = threading.Event()
        self.calls = 0
        self._instances.append(self)

    def submit(self, fn, /, *args, **kwargs):
        self.calls += 1

        def gated():
            self._event.wait()
            return fn(*args, **kwargs)

        return super().submit(gated)

    def open_gate(self) -> None:
        self._event.set()


@pytest.fixture(autouse=True)
async def _release_gated_executors():
    yield
    for executor in GatedExecutor._instances:
        executor.open_gate()
        executor.shutdown(wait=False, cancel_futures=True)
    GatedExecutor._instances.clear()


class ControllableContext:
    """caio-like context with real positional I/O and holdable writes."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.events: list = []
        self.submissions = 0
        # offset -> number of bytes the next write at that offset is cut to
        self.short_writes: dict[int, int] = {}
        # offsets whose write must stay in flight until explicitly released
        self.held_offsets: set[int] = set()
        self._gates: dict[
            int, tuple[asyncio.Future, int, bytes, int],
        ] = {}

    async def write(self, payload: bytes, fd: int, offset: int) -> int:
        self.submissions += 1
        payload = bytes(payload)

        if offset in self.short_writes:
            cut = self.short_writes.pop(offset)
            written = os.pwrite(fd, payload[:cut], offset)
            self.events.append(("write", offset, payload[:cut], written))
            return written

        if offset in self.held_offsets:
            future = self.loop.create_future()
            self._gates[offset] = (future, fd, payload, offset)
            self.events.append(("write_held", offset, payload))
            return await future

        written = os.pwrite(fd, payload, offset)
        self.events.append(("write", offset, payload, written))
        return written

    def release_write(
        self, offset: int, exc: BaseException | None = None,
    ) -> None:
        future, fd, payload, held_offset = self._gates.pop(offset)
        if exc is not None:
            self.loop.call_soon_threadsafe(
                lambda: future.done() or future.set_exception(exc),
            )
            return

        written = os.pwrite(fd, payload, held_offset)
        self.events.append(("write", held_offset, payload, written))
        self.loop.call_soon_threadsafe(
            lambda: future.done() or future.set_result(written),
        )

    def is_holding(self, offset: int) -> bool:
        return offset in self._gates

    async def read(self, size: int, fd: int, offset: int) -> bytes:
        data = os.pread(fd, size, offset)
        self.events.append(("read", offset, data))
        return data

    async def fsync(self, fd: int) -> None:
        os.fsync(fd)
        self.events.append(("fsync", fd))

    async def fdsync(self, fd: int) -> None:
        os.fsync(fd)
        self.events.append(("fdsync", fd))


@pytest.fixture
async def ctx():
    return ControllableContext(asyncio.get_running_loop())


def _track_close(afp: AIOFile, events: list) -> AIOFile:
    file_obj = afp._file_obj
    original_close = file_obj.close

    def logged_close():
        original_close()
        events.append(("close", afp.name))

    file_obj.close = logged_close
    return afp


async def test_parallel_open_first_waiter_cancelled(tmp_path, ctx):
    """Two waiters share one open; cancelling the first must not strand it."""
    path = tmp_path / "segment.bin"
    path.write_bytes(b"")
    executor = GatedExecutor()

    afp = AIOFile(path, "rb", context=ctx, executor=executor)
    first = asyncio.ensure_future(afp.open())
    second = asyncio.ensure_future(afp.open())
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()

    executor.open_gate()
    assert await second is None

    # The single background open was adopted and the file is usable.
    assert executor.calls == 1
    assert not afp.closed
    assert await afp.read(3, offset=0) == b""
    await afp.close()


async def test_open_failure_can_be_retried(tmp_path, ctx):
    """A failed shared open must not poison later open() attempts."""
    missing = tmp_path / "missing.bin"
    afp = AIOFile(missing, "rb", context=ctx)

    with pytest.raises(FileNotFoundError):
        await afp.open()

    missing.write_bytes(b"retry")
    assert isinstance(await afp.open(), int)
    assert await afp.read(5, offset=0) == b"retry"
    await afp.close()


async def test_two_registry_waiters_share_one_open(tmp_path, ctx):
    executor = GatedExecutor()
    path = tmp_path / "segment.bin"
    path.write_bytes(b"")

    registry = SegmentRegistry()
    afp = AIOFile(path, "wb", context=ctx, executor=executor)

    first = asyncio.ensure_future(
        registry.open_segment("logical", afp),
    )
    second = asyncio.ensure_future(
        registry.open_segment("logical", afp),
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    executor.open_gate()
    generation_b, writer_b = await second
    generation_c, writer_c = await registry.open_segment("logical", afp)

    assert executor.calls == 1
    assert writer_b is writer_c
    assert generation_b == generation_c


def _prepare_old_segment(ctx: ControllableContext, path: Path) -> AIOFile:
    path.write_bytes(b"")
    afp = AIOFile(path, "wb", context=ctx)
    return afp


async def _pump_until(predicate, description: str) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Timed out waiting for: %s" % description)


async def test_inflight_write_success_across_rotation(tmp_path, ctx):
    old_path = tmp_path / "seg-0.bin"
    new_path = tmp_path / "seg-1.bin"
    afp0 = _prepare_old_segment(ctx, old_path)
    await afp0.open()
    _track_close(afp0, ctx.events)

    registry = SegmentRegistry()
    generation0, writer0 = await registry.open_segment("logical", afp0)
    assert generation0.generation == 0

    # An ordinary reader keeps its own descriptor on the old segment.
    reader = AIOFile(old_path, "rb", context=ctx)
    await reader.open()

    # Working copy borrowed by the worker coroutine.
    working_copy = await afp0.clone()

    # First three bytes land immediately; the continuation stays in flight.
    ctx.short_writes[10] = 3
    ctx.held_offsets.add(13)
    receipt = await writer0.write_record(b"ABCDEF", offset=10)
    await _pump_until(
        lambda: old_path.stat().st_size == 13 and ctx.is_holding(13),
        "first three bytes land and the continuation is held",
    )

    assert receipt.state is RecordState.IN_FLIGHT
    assert old_path.stat().st_size == 13
    assert old_path.read_bytes()[10:13] == b"ABC"

    # Caller cancels its wait while the continuation is still in flight.
    waiter = asyncio.ensure_future(receipt.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert receipt.state is RecordState.IN_FLIGHT

    # Working copy released first, then the original holder starts releasing.
    await working_copy.close()
    close_task = asyncio.ensure_future(afp0.close())
    await _pump_until(
        lambda: close_task.done() is False and afp0._closing,
        "final release starts and blocks on the in-flight write",
    )
    assert not close_task.done()

    # Rotation seals the old generation and blocks on the same in-flight write.
    afp1 = AIOFile(new_path, "wb", context=ctx)
    rotate_task = asyncio.ensure_future(
        registry.rotate("logical", afp1),
    )
    await _pump_until(
        lambda: writer0.sealed and not rotate_task.done(),
        "rotation seals and waits for the in-flight write",
    )
    assert not rotate_task.done()

    # The in-flight continuation succeeds: everything drains and finalizes.
    ctx.release_write(13)
    generation1, writer1 = await rotate_task
    await close_task

    assert generation1.generation == 1
    assert generation1 != generation0

    # Persistence happened before the descriptor was closed.
    event_names = [event[0] for event in ctx.events]
    assert event_names.index("fdsync") < event_names.index("close")
    assert ("write", 13, b"DEF", 3) in ctx.events

    # The old record is only confirmed over the full 10..16 range now.
    assert await receipt.wait() == (10, 16)
    assert receipt.complete_range == (10, 16)
    seal_result = registry.seal_result(generation0)
    assert seal_result is not None
    assert seal_result.durable_range(10, 6) == (10, 16)
    assert seal_result.durable_end == 16

    # Real readback from the physical old segment.
    assert old_path.stat().st_size == 16
    assert old_path.read_bytes()[10:16] == b"ABCDEF"

    # The new segment's record at the same offset gets only its own result.
    new_receipt = await writer1.write_record(b"123456", offset=10)
    assert new_receipt.generation == generation1
    assert await new_receipt.wait() == (10, 16)
    await writer1.close()

    assert new_path.read_bytes()[10:16] == b"123456"
    assert old_path.read_bytes()[10:16] == b"ABCDEF"

    # The unrelated reader can still read the old segment.
    assert await reader.read(16, offset=0) == b"\x00" * 10 + b"ABCDEF"
    await reader.close()

async def test_inflight_write_failure_is_not_committed(tmp_path, ctx):
    old_path = tmp_path / "seg-0.bin"
    new_path = tmp_path / "seg-1.bin"
    afp0 = _prepare_old_segment(ctx, old_path)

    registry = SegmentRegistry()
    generation0, writer0 = await registry.open_segment("logical", afp0)

    ctx.short_writes[10] = 3
    ctx.held_offsets.add(13)
    receipt = await writer0.write_record(b"ABCDEF", offset=10)
    await _pump_until(
        lambda: old_path.stat().st_size == 13 and ctx.is_holding(13),
        "first three bytes land and the continuation is held",
    )

    waiter = asyncio.ensure_future(receipt.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    close_task = asyncio.ensure_future(afp0.close())
    afp1 = AIOFile(new_path, "wb", context=ctx)
    rotate_task = asyncio.ensure_future(
        registry.rotate("logical", afp1),
    )
    await _pump_until(
        lambda: writer0.sealed and not rotate_task.done(),
        "rotation seals and waits for the in-flight write",
    )

    # The continuation fails: the half-written record stays unfinished.
    ctx.release_write(13, exc=OSError(5, "I/O error"))
    generation1, writer1 = await rotate_task
    await close_task

    assert receipt.state is RecordState.FAILED
    assert receipt.complete_range is None
    with pytest.raises(Exception):
        await receipt.wait()

    # The seal result exposes the unfinished record rather than guessing from
    # the 13-byte file length that six bytes were committed.
    seal_result = registry.seal_result(generation0)
    assert seal_result is not None
    assert seal_result.durable_range(10, 6) is None
    sealed_receipt = seal_result.receipt(10)
    assert sealed_receipt is receipt
    assert sealed_receipt.state is RecordState.FAILED
    assert old_path.stat().st_size == 13

    # The next generation still works and stays independent.
    new_receipt = await writer1.write_record(b"XYZABC", offset=10)
    assert new_receipt.generation == generation1
    assert await new_receipt.wait() == (10, 16)
    await writer1.close()
    assert new_path.read_bytes()[10:16] == b"XYZABC"


async def test_no_new_batch_write_after_final_release_begins(tmp_path, ctx):
    path = tmp_path / "segment.bin"
    afp = _prepare_old_segment(ctx, path)

    registry = SegmentRegistry()
    generation, writer = await registry.open_segment("logical", afp)

    ctx.held_offsets.add(0)
    receipt = await writer.write_record(b"pending", offset=0)
    await _pump_until(
        lambda: ctx.is_holding(0), "write at offset 0 is held",
    )

    close_task = asyncio.ensure_future(afp.close())
    await _pump_until(
        lambda: afp._closing and not close_task.done(),
        "final release starts and blocks on the held write",
    )

    submissions_at_close = ctx.submissions

    # Once final release started, new operations are refused and therefore
    # submit nothing; settling the late receipt adds no operation either.
    with pytest.raises(asyncio.InvalidStateError):
        await afp.write(b"late", offset=100)
    with pytest.raises(asyncio.InvalidStateError):
        await afp.read(1, offset=0)

    ctx.release_write(0)
    await close_task
    assert await receipt.wait() == (0, 7)
    assert ctx.submissions == submissions_at_close


async def test_borrowed_file_object_is_not_closed(tmp_path, ctx):
    path = tmp_path / "borrowed.bin"
    with open(path, "wb+") as fp:
        afp = AIOFile.from_fp(fp, context=ctx)
        assert afp._file_obj_owner is False

        await afp.write(b"hello", offset=0)
        await afp.close()

        # Releasing the wrapper must not close the caller's file.
        assert not fp.closed
        fp.seek(0)
        assert fp.read() == b"hello"


async def test_plain_sequential_write_read_still_works(tmp_path, ctx):
    path = tmp_path / "plain.bin"
    afp = AIOFile(path, "wb+", context=ctx)
    await afp.open()
    await afp.write(b"header", offset=0)
    await afp.write(b"body", offset=8)
    await afp.fdsync()
    assert await afp.read(12, offset=0) == b"header\x00\x00body"
    await afp.close()
    assert path.read_bytes() == b"header\x00\x00body"


async def test_receipt_reawait_after_caller_cancel(tmp_path, ctx):
    path = tmp_path / "receipt.bin"
    afp = _prepare_old_segment(ctx, path)
    registry = SegmentRegistry()
    _, writer = await registry.open_segment("logical", afp)

    ctx.held_offsets.add(0)
    receipt = await writer.write_record(b"abc", offset=0)
    await _pump_until(lambda: ctx.is_holding(0), "write is held in flight")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(receipt.wait(), timeout=0.01)
    assert receipt.state is RecordState.IN_FLIGHT

    ctx.release_write(0)
    assert await receipt.wait() == (0, 3)
    assert receipt.state is RecordState.DURABLE
    await writer.close()


async def test_segment_roundtrip_with_real_backend(
    aio_file_maker, tmp_path,
):
    old_path = tmp_path / "seg-0.bin"
    new_path = tmp_path / "seg-1.bin"
    registry = SegmentRegistry()

    afp0 = aio_file_maker(old_path, "wb")
    generation0, writer0 = await registry.open_segment("logical", afp0)

    receipt = await writer0.write_record(b"ABCDEF", offset=10)
    assert await receipt.wait() == (10, 16)

    seal0 = await writer0.seal()
    assert seal0.durable_range(10, 6) == (10, 16)
    assert seal0.durable_end == 16

    afp1 = aio_file_maker(new_path, "wb")
    generation1, writer1 = await registry.rotate("logical", afp1)
    assert generation1 != generation0

    new_receipt = await writer1.write_record(b"123456", offset=10)
    assert new_receipt.generation == generation1
    assert await new_receipt.wait() == (10, 16)
    await registry.close_generation(generation1)
    await afp0.close()

    assert old_path.read_bytes()[10:16] == b"ABCDEF"
    assert new_path.read_bytes()[10:16] == b"123456"


async def test_shared_segment_open_with_real_backend(
    aio_file_maker, tmp_path,
):
    path = tmp_path / "shared.bin"
    path.write_bytes(b"")
    registry = SegmentRegistry()
    afp = aio_file_maker(path, "wb")

    results = await asyncio.gather(
        registry.open_segment("logical", afp),
        registry.open_segment("logical", afp),
    )

    assert results[0][0] == results[1][0]
    assert results[0][1] is results[1][1]
    await afp.close()
