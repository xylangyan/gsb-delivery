"""Tests for coordinated handoff of paired document readers."""

import asyncio

import pytest

from aiostream.handoff import (
    DeliveryEntry,
    HandoffCoordinator,
    Parcel,
    ReaderPair,
    ReceiptRecord,
    ReceptionLedger,
    recover_lost_receipt,
)


class QueueReader:
    """Deterministic reader double backed by an asyncio queue."""

    _END = object()

    def __init__(self, reader_id, kind, *, close_delay=0.0, close_error=None):
        self.reader_id = reader_id
        self.kind = kind
        self._queue: asyncio.Queue = asyncio.Queue()
        self.close_count = 0
        self._close_delay = close_delay
        self._close_error = close_error

    def put(self, number, digest):
        self._queue.put_nowait(Parcel(number, self.kind, digest, self.reader_id))

    def end_feed(self):
        self._queue.put_nowait(self._END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is self._END:
            raise StopAsyncIteration
        return item

    async def aclose(self):
        self.close_count += 1
        if self._close_delay:
            await asyncio.sleep(self._close_delay)
        if self._close_error is not None:
            raise self._close_error


def make_pair(pair_id, *, close_delay=0.0, close_error=None):
    return ReaderPair(
        pair_id,
        QueueReader(
            f"{pair_id}-scale",
            "scale",
            close_delay=close_delay,
            close_error=close_error,
        ),
        QueueReader(
            f"{pair_id}-acceptance",
            "acceptance",
            close_delay=close_delay,
            close_error=close_error,
        ),
    )


def feed(pair, number, scale_digest, acceptance_digest):
    pair.scale.put(number, scale_digest)
    pair.acceptance.put(number, acceptance_digest)


def end_feed(pair):
    pair.scale.end_feed()
    pair.acceptance.end_feed()


class FakeWarehouse:
    """Idempotent in-memory warehouse gateway."""

    def __init__(self):
        self.stored = {}
        self.commit_calls = 0

    async def commit(self, entry: DeliveryEntry):
        self.commit_calls += 1
        record = self.stored.get(entry.receipt_number)
        if record is not None:
            return record
        record = ReceiptRecord(
            entry.receipt_number,
            entry.scale.digest,
            entry.acceptance.digest,
            entry.reader_pair_id,
        )
        self.stored[entry.receipt_number] = record
        return record

    async def find_receipt(self, number):
        return self.stored.get(number)


async def collect(coord, initial, announcements=None, stop_after=None):
    entries = []
    async for entry in coord.run(initial, announcements):
        entries.append(entry)
        if stop_after is not None and entry.receipt_number >= stop_after:
            coord.stop()
    return entries


@pytest.mark.asyncio
async def test_pairs_by_number_and_waits_for_both_halves():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair = make_pair("A")

    # Scale slip 1 arrives first and must not be stored on its own.
    pair.scale.put(1, "s1")

    async def driver():
        await asyncio.sleep(0.02)
        assert ledger.committed_numbers() == []
        pair.acceptance.put(1, "a1")
        feed(pair, 2, "s2", "a2")
        await asyncio.sleep(0.02)
        end_feed(pair)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair)

    assert [entry.receipt_number for entry in entries] == [1, 2]
    assert ledger.committed_numbers() == [1, 2]
    assert warehouse.commit_calls == 2


@pytest.mark.asyncio
async def test_two_lanes_are_read_in_parallel():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair = make_pair("A")

    # Acceptance leads, scale lags behind: pairing still works in order.
    for number in range(1, 4):
        pair.acceptance.put(number, f"a{number}")
    for number in range(1, 4):
        pair.scale.put(number, f"s{number}")
    end_feed(pair)

    entries = await collect(coord, pair)
    assert [entry.receipt_number for entry in entries] == [1, 2, 3]
    assert all(entry.reader_pair_id == "A" for entry in entries)


@pytest.mark.asyncio
async def test_takeover_swaps_the_pair_together_at_next_number():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A", close_delay=0.02)
    pair_b = make_pair("B")
    for number in range(1, 18):  # A stored 1..17
        feed(pair_a, number, f"s{number}", f"a{number}")
    for number in range(1, 19):  # B replays history, then delivers 18
        feed(pair_b, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.05)
        assert ledger.last_receipt_number == 17
        coord.advertise(pair_b)
        await asyncio.sleep(0.05)
        assert coord.active_pair is pair_b
        feed(pair_b, 19, "s19", "a19")
        await asyncio.sleep(0.05)
        end_feed(pair_b)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == list(range(1, 20))
    assert ledger.origin_of(17) == "A"
    assert ledger.origin_of(18) == "B"
    assert ledger.origin_of(19) == "B"
    assert coord.switch_count == 1
    # Old pair closed exactly once, both readers.
    assert pair_a.scale.close_count == 1
    assert pair_a.acceptance.close_count == 1


@pytest.mark.asyncio
async def test_takeover_drains_items_buffered_before_the_switch():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")
    for number in range(1, 6):
        feed(pair_b, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.03)
        coord.advertise(pair_b)
        # B produces nothing more: numbers 4 and 5 are already buffered and
        # must be drained without a further reader event waking the loop.
        await asyncio.sleep(0.05)
        end_feed(pair_b)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await asyncio.wait_for(collect(coord, pair_a), timeout=2)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3, 4, 5]
    assert coord.switch_count == 1
    assert pair_a.closed and pair_b.closed


@pytest.mark.asyncio
async def test_candidate_stuck_on_first_batch_never_takes_over():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        coord.advertise(pair_b)  # B never produces anything
        await asyncio.sleep(0.05)
        assert coord.active_pair is pair_a
        assert coord.candidate_pair is pair_b
        end_feed(pair_a)
        end_feed(pair_b)  # stuck candidate gives up; it never took over

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3]
    assert coord.switch_count == 0
    assert pair_b.closed


@pytest.mark.asyncio
async def test_lost_receipt_is_queried_before_candidate_can_overwrite():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 18):  # A produces 1..17 through the coordinator
        feed(pair_a, number, f"s{number}", f"a{number}")
    # Number 18 is stored at the warehouse but its receipt was lost locally.
    await warehouse.commit(
        DeliveryEntry(
            18,
            Parcel(18, "scale", "s18", "A-scale"),
            Parcel(18, "acceptance", "a18", "A-acceptance"),
            "A",
        )
    )
    for number in range(1, 18):  # B only caught up to 17
        feed(pair_b, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.03)
        coord.advertise(pair_b)
        await asyncio.sleep(0.03)
        # B must not take over: it cannot pair number 19 yet.
        assert coord.active_pair is pair_a
        assert coord.candidate_pair is pair_b
        # Recover the lost receipt from the warehouse first.
        found = await recover_lost_receipt(ledger, 18)
        assert found is not None and found.receipt_number == 18
        assert ledger.origin_of(18) == "A"
        # B now completes the next pair (19); its own 18 is discarded.
        feed(pair_b, 18, "s18", "a18")
        feed(pair_b, 19, "s19", "a19")
        await asyncio.sleep(0.05)
        end_feed(pair_b)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    # Number 18 is recovered from the warehouse, not re-matched, so it does not
    # appear as a freshly produced entry; the ledger still holds it once.
    assert [entry.receipt_number for entry in entries] == [
        *range(1, 18),
        19,
    ]
    assert ledger.committed_numbers() == list(range(1, 20))
    assert ledger.origin_of(18) == "A"  # not overwritten by B
    assert ledger.origin_of(19) == "B"
    assert warehouse.commit_calls == 19  # 18 committed exactly once


@pytest.mark.asyncio
async def test_newer_candidate_retires_pending_pair_but_keeps_active():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    pair_c = make_pair("C")
    for number in range(1, 5):
        feed(pair_a, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        coord.advertise(pair_b)  # B stalls
        await asyncio.sleep(0.02)
        for number in range(1, 5):  # C replays stored history only
            feed(pair_c, number, f"s{number}", f"a{number}")
        coord.advertise(pair_c)  # C supersedes B
        await asyncio.sleep(0.05)
        assert pair_b.closed  # B retired and closed
        assert coord.candidate_pair is pair_c
        assert coord.active_pair is pair_a  # A keeps reading unchanged
        feed(pair_c, 5, "s5", "a5")  # C becomes eligible
        await asyncio.sleep(0.05)
        assert coord.active_pair is pair_c
        end_feed(pair_c)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3, 4, 5]
    assert pair_b.scale.close_count == 1
    assert pair_b.acceptance.close_count == 1
    assert ledger.origin_of(5) == "C"


@pytest.mark.asyncio
async def test_ready_announcement_is_handled_before_reader_events():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")  # fully eligible on arrival
    pair_c = make_pair("C")  # only history, not eligible yet
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")
    for number in range(1, 5):
        feed(pair_b, number, f"s{number}", f"a{number}")
    for number in range(1, 4):
        feed(pair_c, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        # Both announcements are queued before the coordinator processes them:
        # B would take over immediately, but C must be adopted first.
        coord.advertise(pair_b)
        coord.advertise(pair_c)
        await asyncio.sleep(0.05)
        assert pair_b.closed
        assert coord.candidate_pair is pair_c
        assert coord.active_pair is pair_a
        end_feed(pair_a)
        end_feed(pair_c)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3]


@pytest.mark.asyncio
async def test_digest_mismatch_retires_candidate():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        for number in range(1, 5):
            scale_digest = "WRONG" if number == 2 else f"s{number}"
            feed(pair_b, number, scale_digest, f"a{number}")
        coord.advertise(pair_b)
        await asyncio.sleep(0.05)
        assert pair_b.closed
        assert coord.active_pair is pair_a
        assert coord.verification_failures == [("B", 2)]
        end_feed(pair_a)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3]


@pytest.mark.asyncio
async def test_lane_exit_closes_the_partner_reader_and_retires_pair():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        pair_b.scale.end_feed()  # one lane exits; acceptance stays open
        coord.advertise(pair_b)
        await asyncio.sleep(0.05)
        assert pair_b.closed
        assert pair_b.scale.close_count == 1
        # The partner still held by the job is closed as well.
        assert pair_b.acceptance.close_count == 1
        assert coord.active_pair is pair_a
        end_feed(pair_a)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3]


@pytest.mark.asyncio
async def test_close_once_and_late_close_error_keeps_takeover():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A", close_error=RuntimeError("late cleanup boom"))
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")
    for number in range(1, 6):
        feed(pair_b, number, f"s{number}", f"a{number}")

    async def driver():
        await asyncio.sleep(0.02)
        coord.advertise(pair_b)
        await asyncio.sleep(0.05)
        assert coord.active_pair is pair_b  # takeover happened
        # A late/duplicate cleanup must not undo it nor raise.
        await pair_a.aclose()
        assert pair_a.scale.close_count == 1
        assert pair_a.acceptance.close_count == 1
        assert pair_a.close_error is not None
        end_feed(pair_b)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3, 4, 5]
    assert coord.switch_count == 1
    assert ledger.origin_of(5) == "B"  # B really did take over


@pytest.mark.asyncio
async def test_stop_leaves_no_pending_task_or_orphan_handle():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 10):
        feed(pair_a, number, f"s{number}", f"a{number}")
    coord.advertise(pair_b)  # held candidate, neither lane producing yet

    entries = await collect(coord, pair_a, stop_after=3)

    assert [entry.receipt_number for entry in entries] == [1, 2, 3]
    assert coord.is_stopped
    assert pair_a.closed and pair_b.closed
    assert not coord._tasks  # no pending substreams
    assert not coord._pairs  # every opened handle is owned and closed


class FailingReader(QueueReader):
    """Reader that raises after producing an optional first item."""

    def __init__(self, reader_id, kind, error):
        super().__init__(reader_id, kind)
        self._error = error

    async def __anext__(self):
        item = await self._queue.get()
        if item is self._END:
            raise self._error
        return item


@pytest.mark.asyncio
async def test_active_reader_failure_propagates_and_closes_everything():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    failure = RuntimeError("scale reader broke")
    pair_a = ReaderPair(
        "A",
        FailingReader("A-scale", "scale", failure),
        QueueReader("A-acceptance", "acceptance"),
    )
    pair_a.scale.put(1, "s1")
    pair_a.acceptance.put(1, "a1")
    pair_a.scale.end_feed()  # next read raises
    pair_a.acceptance.end_feed()

    with pytest.raises(RuntimeError, match="scale reader broke"):
        await collect(coord, pair_a)

    assert pair_a.closed
    assert not coord._pairs
    assert not coord._tasks


class RaisingWarehouse(FakeWarehouse):
    async def commit(self, entry: DeliveryEntry):
        raise RuntimeError("warehouse unavailable")


@pytest.mark.asyncio
async def test_warehouse_failure_propagates_and_closes_handles():
    ledger = ReceptionLedger(RaisingWarehouse())
    coord = HandoffCoordinator(ledger)
    pair = make_pair("A")
    feed(pair, 1, "s1", "a1")
    pair.scale.end_feed()
    pair.acceptance.end_feed()

    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        await collect(coord, pair)

    assert pair.closed
    assert not coord._pairs


@pytest.mark.asyncio
async def test_mismatched_numbers_raise_and_shut_down():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair = make_pair("A")
    pair.scale.put(1, "s1")
    pair.acceptance.put(2, "a2")  # different receipt number under one pair
    pair.scale.end_feed()
    pair.acceptance.end_feed()

    with pytest.raises(RuntimeError, match="mismatched numbers"):
        await collect(coord, pair)

    assert pair.closed
    assert not coord._pairs


@pytest.mark.asyncio
async def test_handoff_is_stable_under_repetition():
    for _ in range(50):
        warehouse = FakeWarehouse()
        ledger = ReceptionLedger(warehouse)
        coord = HandoffCoordinator(ledger)
        pair_a = make_pair("A")
        pair_b = make_pair("B")
        for number in range(1, 11):
            feed(pair_a, number, f"s{number}", f"a{number}")
        for number in range(1, 14):
            feed(pair_b, number, f"s{number}", f"a{number}")

        async def driver():
            await asyncio.sleep(0)
            coord.advertise(pair_b)
            await asyncio.sleep(0)
            end_feed(pair_b)

        async with asyncio.TaskGroup() as group:
            group.create_task(driver())
            entries = await collect(coord, pair_a)

        assert [entry.receipt_number for entry in entries] == list(range(1, 14))
        assert ledger.committed_numbers() == list(range(1, 14))
        assert pair_a.closed and pair_b.closed
        assert not coord._tasks and not coord._pairs


@pytest.mark.asyncio
async def test_active_lane_exit_holds_partner_until_shutdown():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair = make_pair("A")
    pair.scale.put(1, "s1")
    pair.acceptance.put(1, "a1")
    pair.scale.end_feed()  # scale lane exits first

    async def driver():
        await asyncio.sleep(0.02)
        pair.acceptance.put(2, "a2")  # unmatched: scale can never pair it
        pair.acceptance.end_feed()

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair)

    # Number 1 was paired; number 2 had no scale counterpart. The acceptance
    # reader stayed held by the job until shutdown, which closed both once.
    assert [entry.receipt_number for entry in entries] == [1]
    assert pair.scale.close_count == 1
    assert pair.acceptance.close_count == 1
    assert not coord._tasks and not coord._pairs


@pytest.mark.asyncio
async def test_ledger_origin_and_idempotency():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    entry = DeliveryEntry(
        7,
        Parcel(7, "scale", "s7", "A-scale"),
        Parcel(7, "acceptance", "a7", "A-acceptance"),
        "A",
    )
    first = await ledger.commit(entry)
    second = await ledger.commit(entry)  # idempotent resubmission
    assert first is second
    assert warehouse.commit_calls == 1
    assert ledger.is_committed(7)
    assert ledger.origin_of(7) == "A"
    assert await ledger.find_receipt(99) is None


@pytest.mark.asyncio
async def test_announcement_stream_drives_handoff():
    warehouse = FakeWarehouse()
    ledger = ReceptionLedger(warehouse)
    coord = HandoffCoordinator(ledger)
    pair_a = make_pair("A")
    pair_b = make_pair("B")
    for number in range(1, 4):
        feed(pair_a, number, f"s{number}", f"a{number}")
    for number in range(1, 6):
        feed(pair_b, number, f"s{number}", f"a{number}")
    queue: asyncio.Queue = asyncio.Queue()

    async def announcements():
        yield await queue.get()

    async def driver():
        await asyncio.sleep(0.02)
        await queue.put(pair_b)
        await asyncio.sleep(0.05)
        end_feed(pair_b)

    async with asyncio.TaskGroup() as group:
        group.create_task(driver())
        entries = await collect(coord, pair_a, announcements=announcements())

    assert [entry.receipt_number for entry in entries] == [1, 2, 3, 4, 5]
    assert ledger.origin_of(5) == "B"
    assert pair_a.closed
