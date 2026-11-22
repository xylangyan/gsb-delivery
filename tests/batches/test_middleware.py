from __future__ import annotations

import threading

import pytest

import dramatiq
from dramatiq import group
from dramatiq.batches import Batch, StubBatchStateBackend
from dramatiq.middleware import BatchSettlements, GroupCallbacks, stamp_message
from dramatiq.results import Results
from dramatiq.results.backends import StubBackend as StubResultBackend


@pytest.fixture()
def state_backend():
    return StubBatchStateBackend()


@pytest.fixture()
def broker(stub_broker, stub_rate_limiter_backend, state_backend):
    stub_broker.add_middleware(Results(backend=StubResultBackend()))
    stub_broker.add_middleware(BatchSettlements(state_backend))
    stub_broker.add_middleware(GroupCallbacks(stub_rate_limiter_backend))
    return stub_broker


def test_stamped_message_completes_its_obligation_on_success(broker, stub_worker, state_backend):
    # Given a batch with one obligation and an actor that reports a range
    @dramatiq.actor(store_results=True)
    def crawl(site, page):
        return {"range": {"site": site, "pages": [page]}}

    batch = Batch.open("b", backend=state_backend, obligations=[{"key": "E1", "site": "East"}])

    # When the stamped message is processed successfully
    broker.enqueue(stamp_message(crawl.message("East", 1), "b", "E1"))
    broker.join(crawl.queue_name)

    # Then its obligation completes with the reported range
    batch.reload()
    record = batch.get_obligation("E1")
    assert record["status"] == "completed"
    assert record["range"] == {"site": "East", "pages": [1]}
    assert batch.can_settle


def test_discovered_branches_are_accepted_and_enqueued_stamped(broker, stub_worker, state_backend):
    # Given an actor whose first page discovers its own continuation
    @dramatiq.actor(store_results=True)
    def crawl(site, page):
        if page == 1:
            child = crawl.message(site, page + 1).asdict()
            return {
                "range": {"site": site, "pages": [page]},
                "discovered": [{"key": f"{site}{page + 1}", "site": site, "message": child}],
            }
        return {"range": {"site": site, "pages": [page]}}

    Batch.open("b", backend=state_backend, obligations=[{"key": "E1", "site": "East"}])

    # When the first page is processed
    broker.enqueue(stamp_message(crawl.message("East", 1), "b", "E1"))
    broker.join(crawl.queue_name)

    # Then the continuation is accepted at submission and its message is
    # enqueued already stamped, so it completes and settles the batch
    batch = Batch("b", backend=state_backend)
    assert set(batch.state["obligations"]) == {"E1", "East2"}
    assert batch.get_obligation("East2")["status"] == "completed"
    assert batch.is_settled


def test_failed_message_does_not_complete_its_obligation(broker, stub_worker, state_backend):
    # Given an actor that fails without retrying
    @dramatiq.actor(throws=(RuntimeError,))
    def boom():
        raise RuntimeError("boom")

    @dramatiq.actor(store_results=True)
    def fine():
        return {"range": {"pages": [1]}}

    batch = Batch.open(
        "b",
        backend=state_backend,
        obligations=[{"key": "bad", "site": "s"}, {"key": "good", "site": "s"}],
    )

    # When one message fails and the other succeeds
    broker.enqueue(stamp_message(boom.message(), "b", "bad"))
    broker.enqueue(stamp_message(fine.message(), "b", "good"))
    broker.join(fine.queue_name, fail_fast=False)

    # Then only the successful obligation completes and settlement stays blocked
    batch.reload()
    assert batch.get_obligation("good")["status"] == "completed"
    assert batch.get_obligation("bad")["status"] == "pending"
    assert not batch.can_settle
    assert batch.try_settle() is None


def test_batch_settles_automatically_and_fires_its_callback_once(broker, stub_worker, state_backend):
    # Given actors and a settlement callback
    invocations = []

    @dramatiq.actor(store_results=True)
    def crawl(site, page):
        return {"range": {"site": site, "pages": [page]}}

    @dramatiq.actor
    def settle_hook(batch_id):
        invocations.append(batch_id)

    Batch.open(
        "b",
        backend=state_backend,
        obligations=[{"key": "E1", "site": "East"}, {"key": "W1", "site": "West"}],
        settlement_callbacks=[settle_hook.message("b").asdict()],
    )
    broker.enqueue(stamp_message(crawl.message("East", 1), "b", "E1"))
    broker.enqueue(stamp_message(crawl.message("West", 1), "b", "W1"))

    # When every obligation completes
    broker.join(crawl.queue_name)
    broker.join(settle_hook.queue_name)

    # Then the batch settles and the callback runs exactly once
    batch = Batch("b", backend=state_backend)
    assert batch.is_settled
    assert invocations == ["b"]


def test_group_callbacks_keep_working_alongside_batch_settlements(broker, stub_worker, state_backend):
    # Given a fixed group with its own completion callback
    group_events = []

    @dramatiq.actor(store_results=True)
    def crawl(site, page):
        return {"range": {"site": site, "pages": [page]}}

    @dramatiq.actor
    def group_hook():
        group_events.append("done")

    Batch.open("b", backend=state_backend, obligations=[{"key": "E1", "site": "East"}])
    broker.enqueue(stamp_message(crawl.message("East", 1), "b", "E1"))

    composed = group([crawl.message("G", 1), crawl.message("G", 2)])
    composed.add_completion_callback(group_hook.message())
    composed.run()

    # When all messages drain
    broker.join(crawl.queue_name)
    broker.join(group_hook.queue_name)

    # Then both the batch settlement and the fixed-group callback happen
    batch = Batch("b", backend=state_backend)
    assert batch.is_settled
    assert group_events == ["done"]


def test_late_result_after_cancellation_is_kept_without_reopening_work(broker, stub_worker, state_backend):
    # Given an actor that blocks until the test releases it
    release = threading.Event()

    @dramatiq.actor(store_results=True)
    def crawl():
        release.wait(timeout=5)
        return {"range": {"pages": [2]}, "evidence": {"late": True}}

    batch = Batch.open("b", backend=state_backend, obligations=[{"key": "E2", "site": "East"}])
    broker.enqueue(stamp_message(crawl.message(), "b", "E2"))

    # When the obligation is cancelled before the worker finishes it
    version = batch.version
    batch.cancel_obligation("E2", effective_version=version)
    release.set()
    broker.join(crawl.queue_name)

    # Then the late result is attached but the cancellation stands
    batch.reload()
    record = batch.get_obligation("E2")
    assert record["status"] == "cancelled"
    assert record["late"] == [{"range": {"pages": [2]}, "evidence": {"late": True}}]


def test_unstamped_messages_are_ignored(broker, stub_worker, state_backend):
    # Given an actor and an empty backend
    @dramatiq.actor(store_results=True)
    def plain():
        return {"range": {"pages": [1]}}

    # When an unstamped message runs
    plain.send()
    broker.join(plain.queue_name)

    # Then no batch state is created and nothing is enqueued afterwards
    assert state_backend.read("b") is None


def test_settling_one_batch_does_not_wait_for_an_independent_batch(broker, stub_worker, state_backend):
    # Given two independent batches that happen to run on the same group
    @dramatiq.actor(store_results=True)
    def crawl(site, page):
        return {"range": {"site": site, "pages": [page]}}

    Batch.open("first", backend=state_backend, obligations=[{"key": "A", "site": "s"}])
    second = Batch.open(
        "second",
        backend=state_backend,
        obligations=[{"key": "B", "site": "s"}, {"key": "C", "site": "s"}],
    )

    # When only the first batch's obligation finishes
    broker.enqueue(stamp_message(crawl.message("s", 1), "first", "A"))
    broker.join(crawl.queue_name)

    # Then the first batch settles on its own while the second stays open
    assert Batch("first", backend=state_backend).is_settled
    second.reload()
    assert not second.is_settled
    assert [o["key"] for o in second.pending_obligations] == ["B", "C"]
