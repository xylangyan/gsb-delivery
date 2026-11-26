import asyncio
from asyncio import Future

import pytest
import pytest_asyncio.plugin  # noqa: F401

from pyee import EventEmitter
from pyee.asyncio import AsyncIOEventEmitter
from pyee.delivery import (
    DeliveryFailure,
    DeliveryQueue,
)
from pyee.uplift import uplift, uplift_emit_chain, uplift_partner, unwrap


class QueryError(Exception):
    pass


def test_emit_still_calls_listeners_immediately():
    """The classic emit entry point keeps its boolean semantics."""
    calls = []
    ee = EventEmitter()
    queue = DeliveryQueue(ee)

    @ee.on("check")
    def listener(value):
        calls.append(value)

    assert ee.emit("check", 1) is True
    assert ee.emit("missing") is False

    # Accepting a receipt does not deliver it yet
    receipt = queue.accept("check", 2)
    assert calls == [1]
    assert receipt.target is ee

    queue.dispatch(receipt)
    assert calls == [1, 2]


def test_accept_snapshots_listeners_at_acceptance():
    """Listeners registered after acceptance are not on the receipt."""
    ee = EventEmitter()
    queue = DeliveryQueue(ee)

    @ee.on("check")
    def first(value):
        calls.append(("first", value))

    calls = []
    receipt = queue.accept("check", 1)

    @ee.on("check")
    def second(value):
        calls.append(("second", value))

    queue.dispatch(receipt)
    assert calls == [("first", 1)]

    later = queue.accept("check", 2)
    queue.dispatch(later)
    assert calls == [("first", 1), ("first", 2), ("second", 2)]


def test_switching_line_only_affects_new_receipts():
    jia = EventEmitter()
    yi = EventEmitter()
    queue = DeliveryQueue(jia)

    jia_calls: list[int] = []
    yi_calls: list[int] = []
    jia.on("check", jia_calls.append)
    yi.on("check", yi_calls.append)

    jia_receipt = queue.accept("check", 1)
    assert jia_receipt.target is jia

    queue.set_target(yi)
    yi_receipt = queue.accept("check", 2)
    assert yi_receipt.target is yi

    queue.dispatch(jia_receipt)
    queue.dispatch(yi_receipt)

    assert jia_calls == [1]
    assert yi_calls == [2]


def test_withdrawn_receipt_never_reaches_listeners():
    ee = EventEmitter()
    queue = DeliveryQueue(ee)
    calls: list[int] = []
    ee.on("check", calls.append)

    receipt = queue.accept("check", 41)
    assert queue.withdraw(receipt) is True
    assert receipt.withdrawn

    queue.dispatch(receipt)

    assert calls == []
    assert queue.withdraw(receipt) is False


def test_dispatch_without_receipts_raises():
    ee = EventEmitter()
    queue = DeliveryQueue(ee)

    with pytest.raises(Exception):
        queue.dispatch()


def test_once_is_consumed_by_first_actual_dispatch():
    """Acceptance and waiting do not consume a once registration."""
    ee = EventEmitter()
    queue = DeliveryQueue(ee)
    calls = []

    def once_handler(value):
        calls.append(value)

    ee.once("check", once_handler)

    first = queue.accept("check", 1)
    second = queue.accept("check", 2)

    assert ee.listeners("check") == [once_handler]

    queue.dispatch(first)
    assert calls == [1]
    assert "check" not in ee._events

    queue.dispatch(second)
    assert calls == [1]
    assert second.skipped_attempts[0].listener is once_handler


def test_synchronous_failure_is_recorded_on_receipt():
    ee = EventEmitter()
    queue = DeliveryQueue(ee)
    error = QueryError("boom")

    def failing_listener(value):
        raise error

    ee.on("check", failing_listener)
    receipt = queue.dispatch(queue.accept("check", 1))

    failures = receipt.failures
    assert len(failures) == 1
    failure = failures[0]
    assert isinstance(failure, DeliveryFailure)
    assert failure.receipt_id == receipt.id
    assert failure.target is ee
    assert failure.listener is failing_listener
    assert failure.error is error


@pytest.mark.asyncio
async def test_coroutine_failure_is_recorded():
    ee = AsyncIOEventEmitter()
    queue = DeliveryQueue(ee)
    error = QueryError("async boom")

    @ee.on("check")
    async def failing_listener(value):
        raise error

    receipt = queue.dispatch(queue.accept("check", 1))
    await receipt.wait()

    failures = receipt.failures
    assert len(failures) == 1
    assert failures[0].error is error
    assert failures[0].listener is failing_listener


def test_uplift_chain_and_unwrap():
    jia = EventEmitter()
    verify = uplift(AsyncIOEventEmitter, jia)

    assert uplift_partner(jia) is verify
    assert uplift_partner(verify) is jia
    assert uplift_emit_chain(jia) == [jia, verify]

    unwrap(verify)

    assert uplift_partner(jia) is None
    assert uplift_emit_chain(jia) == [jia]


@pytest.mark.asyncio
async def test_queued_delivery_across_uplifted_lines():
    """The full 41/42/43/44 scenario."""
    loop = asyncio.get_running_loop()

    jia = EventEmitter()
    verify = uplift(AsyncIOEventEmitter, jia, loop=loop)
    yi = AsyncIOEventEmitter(loop=loop)

    queue = DeliveryQueue(jia)

    shared_query: Future = loop.create_future()
    received = []
    movable_calls = []

    def bottom_listener(receipt_id):
        received.append(("bottom", receipt_id))
        if receipt_id != 42:
            return

        # Switch the underlying line before accepting the next receipt.
        queue.set_target(yi)

        # Remove a once that has not fired, then re-register the same
        # function as once again.
        jia.remove_listener("check", movable)
        jia.once("check", movable)

        # Accepting 43 reentrantly must not disturb receipt 42's dispatch.
        queue.accept("check", 43)

    def movable(receipt_id):
        movable_calls.append(receipt_id)

    def verify_jia(receipt_id):
        received.append(("verify-jia", receipt_id))
        if receipt_id == 42:
            return shared_query
        return None

    def verify_yi(receipt_id):
        received.append(("verify-yi", receipt_id))
        if receipt_id == 43:
            return shared_query
        return None

    jia.on("check", bottom_listener)
    jia.once("check", movable)
    verify.on("check", verify_jia)
    yi.on("check", verify_yi)

    # Receipts 41 and 42 are both accepted against line 甲.
    receipt_41 = queue.accept("check", 41)
    receipt_42 = queue.accept("check", 42)

    # Waiting in the queue must not consume the once.
    assert jia.listeners("check") == [bottom_listener, movable]
    assert receipt_41.target is jia
    assert receipt_42.target is jia

    # 41 is withdrawn before dispatch ever starts.
    assert queue.withdraw(receipt_41) is True

    receipt_42 = queue.dispatch(receipt_42)
    assert receipt_42.dispatched

    # Bottom listener ran first, accepted 43 against line 乙.
    [receipt_43] = queue.queued
    assert receipt_43.id != receipt_42.id
    assert receipt_43.target is yi

    # The removed once registration shows as skipped on receipt 42...
    skipped = receipt_42.skipped_attempts
    assert len(skipped) == 1
    assert skipped[0].listener is movable
    assert skipped[0].target is jia

    # ...while the still-valid verification listener on 甲 did run.
    called_listeners = {
        (attempt.target, attempt.listener) for attempt in receipt_42.called_attempts
    }
    assert (jia, bottom_listener) in called_listeners
    assert (verify, verify_jia) in called_listeners

    # The re-registered once is left for a later receipt.
    assert jia.listeners("check") == [bottom_listener, movable]

    # Cancel waiting on 42 after its verification started.
    waiter_42 = asyncio.create_task(receipt_42.wait())
    await asyncio.sleep(0)
    waiter_42.cancel()
    cancel_result = await asyncio.gather(waiter_42, return_exceptions=True)
    assert isinstance(cancel_result[0], asyncio.CancelledError)
    assert receipt_42.wait_cancelled is True
    assert not shared_query.cancelled()

    # Release 43 onto line 乙 and start waiting for its result.
    receipt_43 = queue.dispatch(receipt_43)
    assert receipt_43.target is yi
    waiter_43 = asyncio.create_task(receipt_43.wait())
    await asyncio.sleep(0)
    assert not waiter_43.done()

    # 乙 only ever saw receipt 43; the 甲 side never saw it.
    yi_entries = [rid for name, rid in received if name == "verify-yi"]
    assert yi_entries == [43]
    jia_verify_entries = [rid for name, rid in received if name == "verify-jia"]
    assert 43 not in jia_verify_entries

    # Detach 乙, restore 甲, then accept and release 44.
    queue.set_target(jia)
    receipt_44 = queue.dispatch(queue.accept("check", 44))
    assert receipt_44.target is jia

    # The re-registered once is consumed exactly once, by receipt 44.
    assert movable_calls == [44]
    assert "check" in jia._events
    assert movable not in jia.listeners("check")

    # Now let the shared query fail.
    query_error = QueryError("shared query failed")
    shared_query.set_exception(query_error)

    # 43 still waits for and observes the result.
    await waiter_43

    # 42's waiting remains cancelled, yet its delivery failure is retained.
    assert receipt_42.wait_cancelled is True
    failures_42 = receipt_42.failures
    assert len(failures_42) == 1
    assert failures_42[0].receipt_id == receipt_42.id
    assert failures_42[0].target is verify
    assert failures_42[0].listener is verify_jia
    assert failures_42[0].error is query_error

    # 43 keeps its own failure tied to the 乙 listener.
    failures_43 = receipt_43.failures
    assert len(failures_43) == 1
    assert failures_43[0].receipt_id == receipt_43.id
    assert failures_43[0].target is yi
    assert failures_43[0].listener is verify_yi
    assert failures_43[0].error is query_error

    # 44 has nothing to do with the failed shared query.
    assert receipt_44.failures == []

    # No listener ever received the withdrawn receipt 41.
    assert all(rid != 41 for _, rid in received)
    assert movable_calls == [44]
    assert receipt_41.withdrawn
