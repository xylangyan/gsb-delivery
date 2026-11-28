from __future__ import annotations

import threading
import time
from collections import defaultdict

import pytest

import dramatiq
from dramatiq.brokers.stub import StubBroker, _StubConsumer
from dramatiq.errors import Retry
from dramatiq.source_limits import SourceGroupLimits

from .common import worker

RETRY_DELAY = 300
INITIAL_DELAY = 250


class AckGate:
    """Blocks specific broker acknowledgements so tests can hold them."""

    def __init__(self) -> None:
        self._holds: dict[str, list] = {}
        self._lock = threading.Lock()

    def hold_once(self, message_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._holds[message_id] = [1, event]
        return event

    def maybe_wait(self, message_id: str) -> None:
        with self._lock:
            hold = self._holds.get(message_id)
            if hold is not None and hold[0] > 0:
                hold[0] -= 1
                event = hold[1]
            else:
                return
        event.wait(10)


class ScenarioController:
    """Coordinates the actors: entry journal, per-label gates and events."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: list[str] = []
        self.gates: dict[str, threading.Event] = defaultdict(threading.Event)
        self.b1_attempts = 0

    def enter(self, label: str) -> None:
        with self.lock:
            self.entries.append(label)

    def wait(self, label: str) -> None:
        self.gates[label].wait(10)

    def release(self, label: str) -> None:
        self.gates[label].set()

    def release_all(self) -> None:
        for event in self.gates.values():
            event.set()


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def make_actors(ctrl: ScenarioController):
    @dramatiq.actor(queue_name="a")
    def a_actor(label):
        ctrl.enter(label)
        ctrl.wait(label)

    @dramatiq.actor(queue_name="b", max_retries=5)
    def b_actor(label):
        ctrl.enter(label)
        if label == "B1":
            with ctrl.lock:
                ctrl.b1_attempts += 1
                attempt = ctrl.b1_attempts
            if attempt == 1:
                ctrl.wait("B1-0-raise")
                raise Retry(delay=RETRY_DELAY)
            ctrl.wait("B1-1-done")
            return
        ctrl.wait(label)

    @dramatiq.actor(queue_name="c")
    def c_actor(label):
        ctrl.enter(label)

    return a_actor, b_actor, c_actor


def install_ack_gate(monkeypatch, gate: AckGate) -> None:
    real_ack = _StubConsumer.ack
    real_nack = _StubConsumer.nack

    def gated_ack(self, message):
        gate.maybe_wait(message.message_id)
        return real_ack(self, message)

    def gated_nack(self, message):
        gate.maybe_wait(message.message_id)
        return real_nack(self, message)

    monkeypatch.setattr(_StubConsumer, "ack", gated_ack)
    monkeypatch.setattr(_StubConsumer, "nack", gated_nack)


def events_for(stub_worker, message_id: str):
    return [event for event in stub_worker.source_group_events.events() if event.message_id == message_id]


def has_event(stub_worker, message_id: str, event: str, attempt: int) -> bool:
    return any(
        entry.event == event and entry.attempt == attempt
        for entry in events_for(stub_worker, message_id)
    )


def test_source_group_limits_validate_values() -> None:
    limits = SourceGroupLimits()
    limits.set_limit("a", 1)
    assert limits.get_limit("a") == 1

    limits.set_limit("a", 3)
    assert limits.get_limit("a") == 3

    with pytest.raises(ValueError):
        limits.set_limit("b", 0)

    limits.remove_limit("a")
    assert limits.get_limit("a") is None


def test_workers_without_source_group_limits_keep_priority_and_delay_rules(stub_broker, monkeypatch) -> None:
    gate = AckGate()
    install_ack_gate(monkeypatch, gate)
    ctrl = ScenarioController()
    a_actor, b_actor, c_actor = make_actors(ctrl)

    with worker(stub_broker, worker_timeout=50, worker_threads=3) as stub_worker:
        # No source group limits are configured at all.
        assert stub_worker.source_group_limits.groups() == {}

        c_actor.send("C1")
        delayed = b_actor.send_with_options(args=("BX",), delay=100)
        a_actor.send("A0")

        stub_broker.join(a_actor.queue_name, fail_fast=False)
        stub_broker.join(b_actor.queue_name, fail_fast=False)
        stub_broker.join(c_actor.queue_name, fail_fast=False)
        stub_worker.join()

    # The delayed message still went through the delay queue, a normal
    # prefetch, actor entry/exit and exactly one acknowledgement (the
    # internal delay-queue ack must not be journaled as an attempt ack).
    journal = stub_worker.source_group_events.events()
    delayed_events = [entry for entry in journal if entry.message_id == delayed.message_id]
    counts: dict[str, int] = {}
    for entry in delayed_events:
        counts[entry.event] = counts.get(entry.event, 0) + 1
    assert counts == {
        "delayed": 1,
        "prefetched": 1,
        "started": 1,
        "exited": 1,
        "acked": 1,
    }

    # Nothing is left waiting for an admission slot.
    assert stub_worker.source_group_scheduler.parked == {}


def test_source_group_limit_scenario(stub_broker: StubBroker, monkeypatch) -> None:
    gate = AckGate()
    install_ack_gate(monkeypatch, gate)
    ctrl = ScenarioController()
    a_actor, b_actor, c_actor = make_actors(ctrl)

    stub_worker = dramatiq.Worker(stub_broker, worker_timeout=50, worker_threads=3)
    stub_worker.set_source_group_limit("a", 2)
    stub_worker.set_source_group_limit("b", 1)
    stub_worker.start()

    try:
        # A0 is admitted under the original limit of 2 and stays in its
        # actor (its gate is never released until the end of the run).
        m_a0 = a_actor.send("A0")
        wait_for(lambda: has_event(stub_worker, m_a0.message_id, "started", 0))
        assert stub_worker.source_group_scheduler.in_flight("a") == 1

        # Lower A from 2 to 1 while A0 is still in the actor.  The
        # running attempt is left untouched, but the new limit must
        # constrain every later admission.
        stub_worker.set_source_group_limit("a", 1)

        # A1 and A2 are then prefetched in order and must both wait:
        # A0 keeps holding the single available slot.
        m_a1 = a_actor.send("A1")
        m_a2 = a_actor.send("A2")
        wait_for(
            lambda: set(stub_worker.source_group_scheduler.parked)
            == {m_a1.message_id, m_a2.message_id}
        )
        assert not has_event(stub_worker, m_a1.message_id, "started", 0)
        assert not has_event(stub_worker, m_a2.message_id, "started", 0)
        assert stub_worker.source_group_scheduler.in_flight("a") == 1

        # B1 becomes due after the downgrade.  A free thread must pick
        # it even though A1/A2 were prefetched first.
        m_b1 = b_actor.send_with_options(args=("B1",), delay=INITIAL_DELAY)
        wait_for(lambda: has_event(stub_worker, m_b1.message_id, "started", 0))

        # B2 is prefetched while B1's first attempt is still in the actor.
        m_b2 = b_actor.send("B2")
        wait_for(lambda: has_event(stub_worker, m_b2.message_id, "prefetched", 0))
        assert not has_event(stub_worker, m_b2.message_id, "started", 0)
        assert stub_worker.source_group_scheduler.in_flight("b") == 1

        # Hold B1's first acknowledgement, then let the first attempt fail.
        b1_first_ack = gate.hold_once(m_b1.message_id)
        ctrl.release("B1-0-raise")
        wait_for(lambda: has_event(stub_worker, m_b1.message_id, "exited", 0))
        # The slot is released even though the ack is still held; B2
        # immediately occupies it.
        wait_for(lambda: has_event(stub_worker, m_b2.message_id, "started", 0))
        time.sleep(0.05)
        assert not has_event(stub_worker, m_b1.message_id, "acked", 0)
        assert stub_worker.source_group_scheduler.in_flight("b") == 1

        # The B1 retry goes through the delay queue and is prefetched,
        # but it must wait for B2 to leave the actor.
        wait_for(lambda: has_event(stub_worker, m_b1.message_id, "prefetched", 1))
        assert not has_event(stub_worker, m_b1.message_id, "started", 1)

        ctrl.release("B2")
        # B1's retry starts while the first acknowledgement is still held.
        wait_for(lambda: has_event(stub_worker, m_b1.message_id, "started", 1))
        assert not has_event(stub_worker, m_b1.message_id, "acked", 0)

        # With the retry in the actor, send B3 and an unrelated C1.
        m_b3 = b_actor.send("B3")
        m_c1 = c_actor.send("C1")
        wait_for(lambda: has_event(stub_worker, m_b3.message_id, "prefetched", 0))
        assert not has_event(stub_worker, m_b3.message_id, "started", 0)

        # Releasing the held ack frees a thread; it must run C1 while B3
        # keeps waiting for the B1 retry to finish.
        b1_first_ack.set()
        wait_for(lambda: has_event(stub_worker, m_c1.message_id, "exited", 0))
        assert not has_event(stub_worker, m_b3.message_id, "started", 0)
        assert has_event(stub_worker, m_b1.message_id, "started", 1)

        # Let A0 return but keep its acknowledgement held.
        a0_ack = gate.hold_once(m_a0.message_id)
        ctrl.release("A0")
        wait_for(lambda: has_event(stub_worker, m_a1.message_id, "started", 0))
        assert not has_event(stub_worker, m_a2.message_id, "started", 0)
        time.sleep(0.05)
        assert not has_event(stub_worker, m_a0.message_id, "acked", 0)

        # Drain the remaining work and shut down cleanly.
        a0_ack.set()
        ctrl.release_all()
        stub_broker.join(a_actor.queue_name, fail_fast=False)
        stub_broker.join(b_actor.queue_name, fail_fast=False)
        stub_broker.join(c_actor.queue_name, fail_fast=False)
        stub_worker.join()
    finally:
        ctrl.release_all()
        stub_worker.stop()

    journal = stub_worker.source_group_events.events()

    def entries(message_id: str, event: str):
        return [entry for entry in journal if entry.message_id == message_id and entry.event == event]

    # Every attempt keeps its own start/exit pair and acknowledgement.
    for message_id in (m_a0.message_id, m_a1.message_id, m_a2.message_id, m_b2.message_id, m_b3.message_id):
        started = entries(message_id, "started")
        exited = entries(message_id, "exited")
        acked = entries(message_id, "acked")
        assert len(started) == len(exited) == len(acked) == 1
        assert started[0].admission == exited[0].admission
        assert started[0].attempt == exited[0].attempt == acked[0].attempt == 0

    # B1 has two attempts with distinct admissions and full event sets.
    b1_starts = entries(m_b1.message_id, "started")
    b1_exits = entries(m_b1.message_id, "exited")
    b1_acks = entries(m_b1.message_id, "acked")
    assert [entry.attempt for entry in b1_starts] == [0, 1]
    assert [entry.attempt for entry in b1_exits] == [0, 1]
    assert [entry.attempt for entry in b1_acks] == [0, 1]
    assert {entry.admission for entry in b1_starts} == {entry.admission for entry in b1_exits}
    assert len({entry.admission for entry in b1_starts}) == 2

    # Scheduling order constraints observed during the run.
    order = [label for label in ctrl.entries]
    assert order.count("B1") == 2
    assert order.index("C1") < order.index("B3")
    assert order.index("A1") < order.index("A2")
    # B2 entered before the second (retry) attempt of B1.
    assert order.index("B2") < order.index("B1", order.index("B1") + 1)
