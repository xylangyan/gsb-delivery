"""Sealable write segments with generation-scoped write receipts.

A :class:`SegmentWriter` owns an :class:`~aiofile.AIOFile` opened for a path
and hands out :class:`RecordReceipt` objects for fixed-offset writes.  The
receipts are the *only* authoritative answer about whether a record reached
durable storage: a file's length on disk never is, because an in-flight write
may land after a segment was sealed or after the path was rotated to point at
the next segment.

Each rotation of a path gets a fresh :class:`SegmentGeneration`, so receipts
from the old segment can never be confused with results of writes issued
against the new file that reused the same path and offsets.
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple, Union

from .aio import AIOFile


class RecordState(Enum):
    """Lifecycle of a single record write."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DURABLE = "durable"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SegmentGeneration:
    """Identity of one physical file behind a (possibly rotated) path."""

    path: str
    generation: int

    def __str__(self) -> str:
        return "%s#%d" % (self.path, self.generation)


class RecordError(RuntimeError):
    """Raised when a receipt is awaited after its record failed."""


class RecordCancelled(RecordError):
    """Raised when a receipt is awaited after caller cancellation and the
    in-flight write never reached a durable state."""


@dataclass
class RecordReceipt:
    """Receipt for one record (header and body written together).

    The receipt survives caller cancellation: cancelling the awaiter does not
    cancel the underlying write once it has been submitted to the I/O
    backend.  Awaiting the receipt again (or polling :meth:`state`) later
    reports the real outcome of that write.
    """

    generation: SegmentGeneration
    offset: int
    length: int
    state: RecordState = RecordState.PENDING
    error: Optional[BaseException] = None
    _future: "Optional[asyncio.Future[int]]" = None

    async def wait(self) -> Tuple[int, int]:
        """Wait until the record reaches a terminal state.

        Returns the confirmed durable ``(offset, end_offset)`` range.
        Raises :class:`RecordError` (or :class:`RecordCancelled`) if the
        record did not reach durable storage.
        """
        if self._future is not None and not self._future.done():
            try:
                # Shield the actual write task: cancelling this waiter must
                # not cancel a write already submitted to the I/O backend.
                await asyncio.shield(self._future)
            except asyncio.CancelledError:
                if not self._future.done():
                    raise

        if self.state is RecordState.DURABLE:
            return self.offset, self.offset + self.length

        if self.state is RecordState.CANCELLED:
            raise RecordCancelled(
                "Record at offset %d length %d of %s never became durable: "
                "the wait was cancelled while the write was still in flight"
                % (self.offset, self.length, self.generation),
            )

        raise RecordError(
            "Record at offset %d length %d of %s is not durable: %s"
            % (self.offset, self.length, self.generation, self.state),
        ) from self.error

    def __await__(self):
        return self.wait().__await__()

    @property
    def durable(self) -> bool:
        return self.state is RecordState.DURABLE

    @property
    def complete_range(self) -> Optional[Tuple[int, int]]:
        """The confirmed ``[offset, end)`` range, or ``None`` unless durable."""
        if self.state is RecordState.DURABLE:
            return self.offset, self.offset + self.length
        return None


@dataclass
class SealResult:
    """Result of sealing a segment."""

    generation: SegmentGeneration
    records: Dict[int, RecordReceipt] = field(default_factory=dict)
    durable_end: Optional[int] = None
    error: Optional[BaseException] = None

    @property
    def sealed(self) -> bool:
        return self.error is None

    def receipt(self, offset: int) -> Optional[RecordReceipt]:
        return self.records.get(offset)

    def durable_range(
        self, offset: int, length: int,
    ) -> Optional[Tuple[int, int]]:
        """Return the confirmed range for a record, or ``None`` if the seal
        result has no durable proof for it (a half-written record is never
        inferred from file length)."""
        receipt = self.records.get(offset)
        if receipt is None or receipt.state is not RecordState.DURABLE:
            return None
        if receipt.length != length:
            return None
        return receipt.complete_range


class SegmentWriter:
    """Write records into one open segment and seal it.

    Writes are issued against an :class:`~aiofile.AIOFile`.  Every write gets
    a :class:`RecordReceipt` bound to the writer's :class:`SegmentGeneration`;
    the generation is stamped into the receipt so a rotated path can never
    attribute a new segment's result to an old in-flight write.
    """

    def __init__(self, aio_file: AIOFile, generation: SegmentGeneration):
        self._file = aio_file
        self.generation = generation
        self._receipts: Dict[int, RecordReceipt] = {}
        self._inflight: Dict[asyncio.Future, RecordReceipt] = {}
        self._sealed = False
        self._closing: Optional[asyncio.Future] = None
        self._lock = asyncio.Lock()

    @property
    def file(self) -> AIOFile:
        return self._file

    @property
    def sealed(self) -> bool:
        return self._sealed

    async def write_record(
        self, data: Union[bytes, str], offset: int,
    ) -> RecordReceipt:
        """Write ``data`` at ``offset`` and return its receipt.

        The write is launched as a tracked task: cancelling the caller does
        not cancel the write once submitted, and the receipt keeps the final
        state.  Re-opening or sealing consults receipts rather than file size.
        """
        async with self._lock:
            if self._sealed:
                raise asyncio.InvalidStateError(
                    "Segment %s is already sealed" % self.generation,
                )

            existing = self._receipts.get(offset)
            if existing is not None and existing.state in (
                RecordState.PENDING, RecordState.IN_FLIGHT,
            ):
                raise asyncio.InvalidStateError(
                    "Write at offset %d is already in progress" % offset,
                )

            receipt = RecordReceipt(
                generation=self.generation,
                offset=offset,
                length=len(data),
                state=RecordState.IN_FLIGHT,
            )
            future = asyncio.ensure_future(
                self._file.write(data, offset=offset),
            )
            receipt._future = future
            self._receipts[offset] = receipt
            self._inflight[future] = receipt
            future.add_done_callback(self._on_write_done)
            return receipt

    def _on_write_done(self, future: "asyncio.Future[int]") -> None:
        receipt = self._inflight.pop(future, None)
        if receipt is None:
            return

        if future.cancelled():
            receipt.state = RecordState.CANCELLED
            return

        error = future.exception()
        if error is not None:
            receipt.state = RecordState.FAILED
            receipt.error = error
            return

        receipt.state = RecordState.DURABLE

    async def _drain(self) -> None:
        """Wait for every submitted write to settle, suppressing nothing:
        outcomes are recorded on the receipts, not raised here."""
        pending = [
            future for future, receipt in self._inflight.items()
            if not future.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def seal(self) -> SealResult:
        """Finish the segment.

        All in-flight writes are awaited first.  A write that succeeds is only
        acknowledged after ``fdsync`` makes it durable; a failed or cancelled
        record stays visible as unfinished on the returned :class:`SealResult`
        — its partial bytes on disk never count as a committed record.
        """
        async with self._lock:
            if self._sealed:
                raise asyncio.InvalidStateError(
                    "Segment %s is already sealed" % self.generation,
                )
            self._sealed = True

        await self._drain()

        result = SealResult(
            generation=self.generation,
            records=dict(self._receipts),
        )

        all_durable = all(
            receipt.state is RecordState.DURABLE
            for receipt in self._receipts.values()
        )

        if self._file.mode.writable:
            try:
                await self._file.fdsync()
            except BaseException as exc:    # noqa: BLE001
                result.error = exc
                all_durable = False

        if all_durable and result.error is None:
            result.durable_end = max(
                (
                    receipt.offset + receipt.length
                    for receipt in self._receipts.values()
                ),
                default=0,
            )

        return result

    async def close(self) -> None:
        """Release the underlying file.

        Pending writes are settled first (their receipts keep the outcome).
        A file object borrowed via :meth:`AIOFile.from_fp` is never closed by
        the writer; only the release bookkeeping runs.
        """
        if self._closing is not None:
            await self._closing
            return

        self._closing = asyncio.ensure_future(self._close())
        try:
            await self._closing
        finally:
            self._closing = None

    async def _close(self) -> None:
        self._sealed = True
        await self._drain()
        await self._file.close()


class SegmentRegistry:
    """Coordinates writers sharing one path across generations.

    Multiple workers open the same path through :meth:`open_segment`; only the
    first call opens the file, the rest share that open result even if a
    preceding waiter is cancelled.  Rotating the path with :meth:`rotate`
    starts a new generation, and a late receipt from the old generation can
    never start or confirm a write on the new one.
    """

    def __init__(self) -> None:
        self._generations: Dict[str, int] = {}
        self._writers: Dict[SegmentGeneration, SegmentWriter] = {}
        self._opens: Dict[
            str, "asyncio.Future[Tuple[SegmentGeneration, SegmentWriter]]",
        ] = {}
        self._sealed: Dict[SegmentGeneration, SealResult] = {}
        self._lock = asyncio.Lock()

    def _bind(
        self, path: str, aio_file: AIOFile,
    ) -> Tuple[SegmentGeneration, SegmentWriter]:
        index = self._generations.get(path, 0)
        generation = SegmentGeneration(path=path, generation=index)
        writer = SegmentWriter(aio_file, generation)
        self._generations[path] = index
        self._writers[generation] = writer
        return generation, writer

    async def open_segment(
        self, path: str, aio_file: AIOFile,
    ) -> Tuple[SegmentGeneration, SegmentWriter]:
        """Open (or attach to) the current segment for ``path``.

        ``aio_file`` must target ``path`` and is opened lazily on first use.
        Concurrent callers share one open; cancelling a caller only detaches
        that waiter, the shared open keeps running and later callers receive
        the same writer.
        """
        async with self._lock:
            pending = self._opens.get(path)
            if pending is None:
                pending = asyncio.ensure_future(self._open(path, aio_file))
                self._opens[path] = pending

                def _drop(
                    future: "asyncio.Future[Any]",
                    path: str = path,
                ) -> None:
                    if self._opens.get(path) is future:
                        self._opens.pop(path, None)

                pending.add_done_callback(_drop)

        # Shield the shared task: a cancelled waiter cancels only its own
        # shield, never the open the other waiters are attached to.
        return await asyncio.shield(pending)

    async def _open(
        self, path: str, aio_file: AIOFile,
    ) -> Tuple[SegmentGeneration, SegmentWriter]:
        await aio_file.open()
        async with self._lock:
            existing = self._current_generation(path)
            if existing is not None:
                return existing, self._writers[existing]
            return self._bind(path, aio_file)

    def _current_generation(self, path: str) -> Optional[SegmentGeneration]:
        generation = self._generations.get(path)
        if generation is None:
            return None
        candidate = SegmentGeneration(path, generation)
        if candidate in self._writers:
            return candidate
        return None

    async def rotate(
        self, path: str, next_file: AIOFile,
    ) -> Tuple[SegmentGeneration, SegmentWriter]:
        """Point ``path`` at a new physical file and return its writer.

        The old generation's writer is sealed first, so no new writes can be
        started against it; late old-generation receipts only settle against
        their own records and can never confirm a write on the new file.
        """
        old_generation = self._current_generation(path)
        if old_generation is not None:
            old_writer = self._writers[old_generation]
            if not old_writer.sealed:
                self._sealed[old_generation] = await old_writer.seal()

        next_index = self._generations.get(path, -1) + 1
        next_generation = SegmentGeneration(path, next_index)

        await next_file.open()

        async with self._lock:
            # A concurrent rotate may have advanced the path already.
            current = self._current_generation(path)
            if current is not None and current.generation >= next_index:
                return current, self._writers[current]

            self._generations[path] = next_index
            self._opens.pop(path, None)
            writer = SegmentWriter(next_file, next_generation)
            self._writers[next_generation] = writer
            return next_generation, writer

    def writer_for(
        self, generation: SegmentGeneration,
    ) -> Optional[SegmentWriter]:
        return self._writers.get(generation)

    def seal_result(
        self, generation: SegmentGeneration,
    ) -> Optional[SealResult]:
        return self._sealed.get(generation)

    async def close_generation(self, generation: SegmentGeneration) -> None:
        writer = self._writers.get(generation)
        if writer is None:
            return
        if not writer.sealed:
            self._sealed[generation] = await writer.seal()
        await writer.close()


__all__ = (
    "RecordCancelled",
    "RecordError",
    "RecordReceipt",
    "RecordState",
    "SealResult",
    "SegmentGeneration",
    "SegmentRegistry",
    "SegmentWriter",
)
