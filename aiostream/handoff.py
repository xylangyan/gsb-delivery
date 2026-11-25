"""Coordinated handoff of paired document readers.

The front desk pairs a *scale slip* and an *acceptance slip* by receipt
number: a delivery is only stored once both halves of the same number have
been read. The two readers (one per document kind) are therefore always
operated as a :class:`ReaderPair`.

When a device update delivers a new pair of readers, the job must not switch
each lane independently on announcement (that used to stitch a new scale slip
onto an old acceptance slip under the same number). Instead the new pair is
advertised through :meth:`HandoffCoordinator.advertise` (or an announcement
stream) and only takes over once it has proven itself against the warehouse
state, while the two daily lanes keep being read in parallel.

A candidate pair becomes eligible when:

- the last receipt number that was *actually* stored, as established by a
  warehouse receipt, is known (``N``);
- both candidate readers have delivered the next number ``N + 1`` (a reader
  that stalls on its first batch therefore never takes over);
- every original the candidate already produced for a stored number
  (``<= N``) matches the digest recorded on the warehouse receipt.

The warehouse is queried through idempotent submit/lookup calls, so a lost
receipt (e.g. number 18 whose acknowledgement went missing) is recovered
before the candidate is allowed to overwrite it with its own number 18.

Retiring a pair (superseded candidate, failed reader or job shutdown) closes
its readers exactly once: a late cleanup or an error reported while closing
an old pair can never undo a takeover that already happened.
"""

from __future__ import annotations

import asyncio
import enum
from collections import deque
from dataclasses import dataclass, field
from typing import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Deque,
    Dict,
    Literal,
    Protocol,
    Set,
    Tuple,
    cast,
    runtime_checkable,
)

from .aiter_utils import aiter
from .manager import TaskGroup

__all__ = [
    "ParcelKind",
    "Parcel",
    "DeliveryEntry",
    "Receipt",
    "Warehouse",
    "ReceiptRecord",
    "ReceptionLedger",
    "Reader",
    "ReaderPair",
    "HandoffCoordinator",
    "recover_lost_receipt",
]

ParcelKind = Literal["scale", "acceptance"]
Kinds: Tuple[ParcelKind, ParcelKind] = ("scale", "acceptance")


class _AnnouncementState(enum.Enum):
    """Sentinel marking the end of the announcement stream."""

    DONE = enum.auto()


_ANNOUNCEMENT_DONE = _AnnouncementState.DONE


# Domain objects
@dataclass(frozen=True)
class Parcel:
    """One document (scale slip or acceptance slip) read by a reader."""

    receipt_number: int
    kind: ParcelKind
    digest: str
    reader_id: str


@dataclass(frozen=True)
class DeliveryEntry:
    """A matched pair of documents, ready to be stored in the warehouse."""

    receipt_number: int
    scale: Parcel
    acceptance: Parcel
    reader_pair_id: str


class Receipt(Protocol):
    """Warehouse acknowledgement for a stored receipt number."""

    @property
    def receipt_number(self) -> int: ...


@runtime_checkable
class Warehouse(Protocol):
    """Idempotent warehouse gateway.

    ``commit`` stores an entry at most once for a given receipt number and
    ``find_receipt`` reports whether a number was actually received, which
    makes it possible to recover a lost acknowledgement.
    """

    def commit(self, entry: DeliveryEntry) -> Awaitable[Receipt]: ...

    def find_receipt(self, receipt_number: int) -> Awaitable[Receipt | None]: ...


@dataclass(frozen=True)
class ReceiptRecord:
    """Local, inspectable view of a warehouse receipt."""

    receipt_number: int
    scale_digest: str
    acceptance_digest: str
    reader_pair_id: str
    committed: bool = True


class ReceptionLedger:
    """Reception table tracking stored numbers and their originating pair.

    Every submission goes through the idempotent warehouse gateway: a receipt
    number is never stored twice, and the table always records which reader
    pair produced each accepted number.
    """

    def __init__(self, warehouse: Warehouse) -> None:
        self._warehouse = warehouse
        self._records: Dict[int, ReceiptRecord] = {}
        self._origin: Dict[int, str] = {}
        self._last_receipt_number: int | None = None

    @property
    def last_receipt_number(self) -> int | None:
        """Last receipt number actually stored, per warehouse receipts."""
        return self._last_receipt_number

    def is_committed(self, receipt_number: int) -> bool:
        return receipt_number in self._records

    def record_for(self, receipt_number: int) -> ReceiptRecord | None:
        return self._records.get(receipt_number)

    def origin_of(self, receipt_number: int) -> str | None:
        """Which reader pair a given number came from."""
        return self._origin.get(receipt_number)

    def committed_numbers(self) -> list[int]:
        return sorted(self._records)

    async def commit(self, entry: DeliveryEntry) -> ReceiptRecord:
        """Idempotently store an entry and return its recorded receipt."""
        existing = self._records.get(entry.receipt_number)
        if existing is not None:
            self._check_consistency(existing, entry)
            return existing

        await self._warehouse.commit(entry)
        record = ReceiptRecord(
            receipt_number=entry.receipt_number,
            scale_digest=entry.scale.digest,
            acceptance_digest=entry.acceptance.digest,
            reader_pair_id=entry.reader_pair_id,
        )
        self._register(record)
        return record

    async def find_receipt(self, receipt_number: int) -> ReceiptRecord | None:
        """Return a stored receipt, querying the warehouse if unknown locally."""
        local = self._records.get(receipt_number)
        if local is not None:
            return local
        receipt = await self._warehouse.find_receipt(receipt_number)
        if receipt is None:
            return None
        record = self._record_from_receipt(receipt)
        self._register(record)
        return record

    def _register(self, record: ReceiptRecord) -> None:
        self._records[record.receipt_number] = record
        self._origin[record.receipt_number] = record.reader_pair_id
        if (
            self._last_receipt_number is None
            or record.receipt_number > self._last_receipt_number
        ):
            self._last_receipt_number = record.receipt_number

    @staticmethod
    def _check_consistency(record: ReceiptRecord, entry: DeliveryEntry) -> None:
        if (
            record.scale_digest != entry.scale.digest
            or record.acceptance_digest != entry.acceptance.digest
        ):
            raise RuntimeError(
                f"Receipt {entry.receipt_number} was already stored "
                "with different original documents"
            )

    @staticmethod
    def _record_from_receipt(receipt: Receipt) -> ReceiptRecord:
        if isinstance(receipt, ReceiptRecord):
            return receipt
        return ReceiptRecord(
            receipt_number=receipt.receipt_number,
            scale_digest="",
            acceptance_digest="",
            reader_pair_id="",
        )


async def recover_lost_receipt(
    ledger: ReceptionLedger, receipt_number: int
) -> ReceiptRecord | None:
    """Resolve whether a number whose acknowledgement was lost was stored."""
    return await ledger.find_receipt(receipt_number)


# Reader side
@runtime_checkable
class Reader(Protocol):
    """An asynchronous source of documents for one document kind."""

    reader_id: str

    def __aiter__(self) -> AsyncIterator[Parcel]: ...

    def aclose(self) -> Awaitable[None]: ...


@dataclass
class ReaderPair:
    """A scale reader and an acceptance reader that must be swapped together."""

    pair_id: str
    scale: Reader
    acceptance: Reader
    closed: bool = False
    close_error: Exception | None = field(default=None, repr=False)
    failure: Exception | None = field(default=None, repr=False)

    def reader(self, kind: ParcelKind) -> Reader:
        return self.scale if kind == "scale" else self.acceptance

    async def aclose(self) -> None:
        """Close both readers once, never raising on late cleanup errors."""
        if self.closed:
            return
        self.closed = True
        # Both closures run concurrently and are awaited in full: a reader
        # whose cleanup is slow must not have its close cancelled underneath it.
        await asyncio.gather(
            *(self._close_reader(kind) for kind in Kinds),
            return_exceptions=True,
        )

    async def _close_reader(self, kind: ParcelKind) -> None:
        reader = self.reader(kind)
        try:
            await reader.aclose()
        except Exception as exc:  # late errors must not undo a takeover
            self.close_error = exc


class HandoffCoordinator:
    """Run the two reading lanes with coordinated reader-pair handoff.

    The active pair feeds the two daily lanes in parallel. New pairs are
    advertised with :meth:`advertise` (or an announcement stream) and vetted
    before they may take over; a pair that is retired before taking over is
    closed and discarded while the active pair keeps reading unchanged.
    """

    def __init__(self, ledger: ReceptionLedger) -> None:
        self._ledger = ledger
        self._active_id: str | None = None
        self._candidate_id: str | None = None
        self._pairs: Dict[str, ReaderPair] = {}
        self._buffers: Dict[str, Dict[str, Deque[Parcel]]] = {}
        self._sequences: Dict[str, Dict[str, int]] = {}
        self._ended: Dict[str, Set[str]] = {}
        self._iterators: Dict[Tuple[str, str], AsyncIterator[Parcel]] = {}
        self._tasks: Dict[Tuple[str, str], asyncio.Task[Parcel]] = {}
        self._task_keys: Dict[asyncio.Task[Parcel], Tuple[str, str]] = {}
        self._closed_pairs: Set[str] = set()
        self._announcements: asyncio.Queue[ReaderPair | _AnnouncementState] = (
            asyncio.Queue()
        )
        self._stop_event = asyncio.Event()
        self._group: TaskGroup[object] | None = None
        self._switch_count = 0
        self.verification_failures: list[tuple[str, int]] = []

    @property
    def active_pair(self) -> ReaderPair | None:
        return None if self._active_id is None else self._pairs.get(self._active_id)

    @property
    def candidate_pair(self) -> ReaderPair | None:
        return (
            None if self._candidate_id is None else self._pairs.get(self._candidate_id)
        )

    @property
    def switch_count(self) -> int:
        return self._switch_count

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def advertise(self, pair: ReaderPair) -> None:
        """Entry point for swapping a pair of readers in together."""
        self._announcements.put_nowait(pair)

    def stop(self) -> None:
        """Request outer shutdown: no further documents are produced."""
        self._stop_event.set()

    async def run(
        self,
        initial: ReaderPair,
        announcements: AsyncIterable[ReaderPair] | None = None,
    ) -> AsyncIterator[DeliveryEntry]:
        """Yield matched entries, switching pairs only as a coordinated unit."""
        self._active_id = initial.pair_id
        self._register_pair(initial)
        async with TaskGroup[object]() as group:
            self._group = group
            stop_waiter = cast(
                "asyncio.Task[bool]", group.create_task(self._stop_event.wait())
            )
            pump: asyncio.Task[None] | None = None
            if announcements is not None:
                pump = cast(
                    "asyncio.Task[None]",
                    group.create_task(self._pump_announcements(announcements)),
                )
            ann_waiter: asyncio.Task[ReaderPair | _AnnouncementState] | None = cast(
                "asyncio.Task[ReaderPair | _AnnouncementState]",
                group.create_task(self._announcements.get()),
            )
            try:
                for kind in Kinds:
                    self._schedule(initial.pair_id, kind)

                while True:
                    # Flush anything already pairable and run a pending handoff
                    # before blocking. This also drains items a pair inherited
                    # in its buffers when it took over, without waiting for the
                    # next reader event.
                    async for entry in self._store_active_matches():
                        yield entry
                    await self._maybe_takeover()
                    self._reschedule_readers()

                    # Nothing left to read and no further announcements can
                    # arrive: the job drains to completion.
                    if not self._tasks and ann_waiter is None:
                        break
                    # The active pair is exhausted or gone, and no candidate is
                    # waiting to take over: stop rather than block on a queue.
                    if self._active_id is None or (
                        not self._tasks.get((self._active_id, "scale"))
                        and not self._tasks.get((self._active_id, "acceptance"))
                        and self._candidate_id is None
                    ):
                        break

                    waitables: list[asyncio.Task[object]] = [
                        *self._tasks.values(),
                        stop_waiter,
                    ]
                    if ann_waiter is not None:
                        waitables.append(ann_waiter)

                    done, _ = await asyncio.wait(
                        waitables, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_waiter in done:
                        break

                    # Announcements are handled first: a ready advertisement
                    # always preempts reader events from the same round.
                    if ann_waiter is not None and ann_waiter in done:
                        ann_waiter = await self._drain_announcements(ann_waiter)

                    for task in list(done):
                        if task is stop_waiter:
                            continue
                        await self._handle_reader_task(
                            cast("asyncio.Task[Parcel]", task)
                        )
            finally:
                await group.cancel_task(stop_waiter)
                if pump is not None:
                    await group.cancel_task(pump)
                if ann_waiter is not None:
                    await group.cancel_task(ann_waiter)
                await self._shutdown()
                self._group = None

    async def _pump_announcements(
        self, announcements: AsyncIterable[ReaderPair]
    ) -> None:
        async for pair in announcements:
            await self._announcements.put(pair)
        await self._announcements.put(_ANNOUNCEMENT_DONE)

    async def _drain_announcements(
        self,
        waiter: asyncio.Task[ReaderPair | _AnnouncementState],
    ) -> asyncio.Task[ReaderPair | _AnnouncementState] | None:
        assert self._group is not None
        item = waiter.result()
        if item is _ANNOUNCEMENT_DONE:
            return None
        await self._adopt_candidate(item)
        while not self._announcements.empty():
            queued = self._announcements.get_nowait()
            if queued is _ANNOUNCEMENT_DONE:
                return None
            await self._adopt_candidate(queued)
        return cast(
            "asyncio.Task[ReaderPair | _AnnouncementState]",
            self._group.create_task(self._announcements.get()),
        )

    def _register_pair(self, pair: ReaderPair) -> None:
        self._pairs[pair.pair_id] = pair
        self._buffers[pair.pair_id] = {kind: deque() for kind in Kinds}
        self._sequences[pair.pair_id] = {}
        self._ended[pair.pair_id] = set()

    async def _adopt_candidate(self, pair: ReaderPair) -> None:
        if pair.pair_id == self._active_id:
            raise ValueError(f"Pair {pair.pair_id!r} is already the active pair")
        if pair.pair_id in self._pairs:
            raise ValueError(f"Pair {pair.pair_id!r} was already advertised")
        if self._candidate_id is not None:
            # A newer version (C) supersedes the pending one (B): B is retired
            # and closed while the active pair (A) keeps reading unchanged.
            await self._retire_pair(self._candidate_id)
        self._register_pair(pair)
        self._candidate_id = pair.pair_id
        for kind in Kinds:
            self._schedule(pair.pair_id, kind)

    async def _retire_pair(self, pair_id: str) -> None:
        assert self._group is not None
        for kind in Kinds:
            task = self._tasks.pop((pair_id, kind), None)
            if task is not None:
                self._task_keys.pop(task, None)
                await self._group.cancel_task(task)
            self._iterators.pop((pair_id, kind), None)
        pair = self._pairs.pop(pair_id, None)
        self._buffers.pop(pair_id, None)
        self._sequences.pop(pair_id, None)
        self._ended.pop(pair_id, None)
        if self._candidate_id == pair_id:
            self._candidate_id = None
        if self._active_id == pair_id:
            self._active_id = None
        if pair is not None:
            await self._close_pair(pair)

    async def _close_pair(self, pair: ReaderPair) -> None:
        # Closing happens exactly once; late cleanup cannot reverse a takeover.
        if pair.pair_id in self._closed_pairs:
            return
        self._closed_pairs.add(pair.pair_id)
        await pair.aclose()

    async def _shutdown(self) -> None:
        for pair_id in list(self._pairs):
            await self._retire_pair(pair_id)

    def _schedule(self, pair_id: str, kind: ParcelKind) -> None:
        assert self._group is not None
        if (pair_id, kind) in self._tasks:
            return
        if kind in self._ended.get(pair_id, set()):
            return
        task = cast(
            "asyncio.Task[Parcel]",
            self._group.create_task(self._read_next(pair_id, kind)),
        )
        self._tasks[(pair_id, kind)] = task
        self._task_keys[task] = (pair_id, kind)

    async def _read_next(self, pair_id: str, kind: ParcelKind) -> Parcel:
        key = (pair_id, kind)
        iterator = self._iterators.get(key)
        if iterator is None:
            pair = self._pairs[pair_id]
            iterator = aiter(pair.reader(kind))
            self._iterators[key] = iterator
        return await iterator.__anext__()

    async def _handle_reader_task(self, task: asyncio.Task[Parcel]) -> None:
        key = self._task_keys.pop(task, None)
        if key is None or self._tasks.get(key) is not task:
            return
        pair_id, kind = key
        self._tasks.pop(key, None)
        try:
            parcel = task.result()
        except StopAsyncIteration:
            self._ended[pair_id].add(kind)
            await self._on_lane_exit(pair_id)
            return
        except Exception as exc:
            if pair_id == self._candidate_id:
                # A candidate that loses a lane can never take over: retire the
                # whole pair, including the partner still held by the job.
                self._pairs[pair_id].failure = exc
                await self._retire_pair(pair_id)
                return
            raise
        self._sequences[pair_id][kind] = parcel.receipt_number
        self._buffers[pair_id][kind].append(parcel)

    async def _on_lane_exit(self, pair_id: str) -> None:
        if pair_id == self._candidate_id:
            await self._retire_pair(pair_id)

    async def _store_active_matches(self) -> AsyncIterator[DeliveryEntry]:
        active_id = self._active_id
        if active_id is None:
            return
        buffers = self._buffers[active_id]
        while buffers["scale"] and buffers["acceptance"]:
            scale = buffers["scale"][0]
            acceptance = buffers["acceptance"][0]
            if scale.receipt_number != acceptance.receipt_number:
                raise RuntimeError(
                    f"Reader pair {active_id!r} produced mismatched numbers "
                    f"{scale.receipt_number} and {acceptance.receipt_number}"
                )
            buffers["scale"].popleft()
            buffers["acceptance"].popleft()
            entry = DeliveryEntry(
                receipt_number=scale.receipt_number,
                scale=scale,
                acceptance=acceptance,
                reader_pair_id=active_id,
            )
            await self._store(entry)
            yield entry

    async def _store(self, entry: DeliveryEntry) -> None:
        # Recover a lost acknowledgement before doing anything else: the
        # warehouse, not the reader, is the source of truth for stored numbers.
        stored = await self._ledger.find_receipt(entry.receipt_number)
        if stored is not None:
            return
        await self._ledger.commit(entry)

    async def _maybe_takeover(self) -> None:
        candidate_id = self._candidate_id
        active_id = self._active_id
        if candidate_id is None or active_id is None:
            return
        last = self._ledger.last_receipt_number
        if last is None:
            return
        next_number = last + 1
        if any(
            self._find_parcel(candidate_id, kind, next_number) is None for kind in Kinds
        ):
            return
        if not await self._verify_candidate(candidate_id, last):
            await self._retire_pair(candidate_id)
            return
        # Local handoff: the candidate now owns both lanes as a single unit.
        self._active_id = candidate_id
        self._candidate_id = None
        self._switch_count += 1
        self._discard_stored_parcels(candidate_id, last)
        await self._retire_pair(active_id)

    async def _verify_candidate(self, candidate_id: str, last: int) -> bool:
        for kind in Kinds:
            for parcel in list(self._buffers[candidate_id][kind]):
                if parcel.receipt_number > last:
                    break
                record = self._ledger.record_for(parcel.receipt_number)
                if record is None:
                    continue
                expected = (
                    record.scale_digest if kind == "scale" else record.acceptance_digest
                )
                if expected and parcel.digest != expected:
                    self.verification_failures.append(
                        (candidate_id, parcel.receipt_number)
                    )
                    return False
        return True

    def _discard_stored_parcels(self, pair_id: str, last: int) -> None:
        for kind in Kinds:
            buffer = self._buffers[pair_id][kind]
            while buffer and buffer[0].receipt_number <= last:
                buffer.popleft()

    def _find_parcel(
        self, pair_id: str, kind: ParcelKind, number: int
    ) -> Parcel | None:
        for parcel in self._buffers[pair_id][kind]:
            if parcel.receipt_number == number:
                return parcel
        return None

    def _reschedule_readers(self) -> None:
        for pair_id in (self._active_id, self._candidate_id):
            if pair_id is None or pair_id not in self._pairs:
                continue
            for kind in Kinds:
                self._schedule(pair_id, kind)
