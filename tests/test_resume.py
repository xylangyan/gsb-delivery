# coding:utf-8
"""Tests for resumable (journal backed) retry execution."""
import functools
import json
import os

import pytest

import backoff
from backoff._journal import Journal


class Crash(BaseException):
    """Simulates a process stopping in the middle of a retry loop."""


class ManualClock(object):
    def __init__(self, start=1000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _crash_on(kind, **match):
    def hook(*args):
        event = args[-1]
        if event.get("kind") != kind:
            return
        if any(event.get(key) != value for key, value in match.items()):
            return
        raise Crash()
    return hook


def _resume(journal, **kwargs):
    return backoff.resume(journal, **kwargs)

def test_plain_calls_do_not_touch_a_journal(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    journal = Journal()

    @backoff.on_predicate(backoff.constant, jitter=None, interval=0,
                          max_tries=3)
    def poll():
        return False

    assert poll() is False
    assert journal.events == []


def test_durable_success_matches_plain_run():
    clock = ManualClock()
    sleeps = []

    @backoff.on_predicate(backoff.expo, jitter=None, max_tries=3,
                          on_backoff=lambda d: None)
    def poll(state):
        state["calls"] += 1
        return state["calls"] >= 2

    journal = Journal(clock=clock)
    with _resume(journal, key="demo"):
        result = poll({"calls": 0})

    assert result is True
    report = journal.report("demo")
    assert [attempt["attempt"] for attempt in report["attempts"]] == [1, 2]
    assert [attempt["decision"] for attempt in report["attempts"]] == [
        "backoff", "success"]
    first = report["attempts"][0]
    assert first["wait"] == 1
    assert first["confirmed"] is True
    assert first["receipt_phase"] == "after"
    assert first["callbacks"]["backoff"] == ["done", "done"]


def test_restart_resumes_started_wait_at_absolute_wake_up(tmp_path):
    clock = ManualClock()
    path = str(tmp_path / "journal.jsonl")
    journal = Journal(path, clock=clock,
                      on_append=_crash_on("backoff_receipt", attempt=2))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    @backoff.on_predicate(backoff.constant, jitter=None, interval=5,
                          max_tries=4)
    def poll(state):
        state["calls"] += 1
        return state["calls"] >= 3

    with pytest.raises(Crash):
        with _resume(journal, key="demo", sleep=sleep):
            poll({"calls": 0})
    journal.close()

    clock.advance(3)

    restarted = Journal(path, clock=clock)
    with _resume(restarted, key="demo", sleep=sleep):
        assert poll({"calls": 2}) is True
    restarted.close()

    attempts = Journal(path).reconcile("demo").attempts
    assert [attempt.wake_at for attempt in attempts
            if attempt.wake_at is not None] == [1005.0, 1010.0]
    assert attempts[1].waited == 2
    assert attempts[0].waited == 5
    assert [attempt.number for attempt in attempts] == [1, 2, 3]


def test_restart_before_receipt_starts_at_original_stage(tmp_path):
    clock = ManualClock()
    path = str(tmp_path / "journal.jsonl")
    sleeps = []
    journal = Journal(path, clock=clock,
                      on_append=_crash_on("wait_planned", attempt=1))

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    @backoff.on_predicate(backoff.constant, jitter=None, interval=5,
                          max_tries=2)
    def poll(state):
        state["calls"] += 1
        return state["calls"] >= 2

    with pytest.raises(Crash):
        with _resume(journal, key="demo", sleep=sleep):
            poll({"calls": 0})
    journal.close()

    # One second of downtime must not shorten the planned 5s interval.
    clock.advance(1)
    restarted = Journal(path, clock=clock)
    with _resume(restarted, key="demo", sleep=sleep):
        assert poll({"calls": 1}) is True
    restarted.close()

    attempts = Journal(path).reconcile("demo").attempts
    first = attempts[0]
    assert first.receipt_phase == "after"
    assert first.confirmed is True
    # The wait should have started when the attempt decided to wait, so the
    # one second of downtime counts against it.
    assert sleeps == [4.0]
    assert first.intended_wake_at == pytest.approx(1005.0)
    assert first.wake_at == pytest.approx(1005.0)


def test_unconfirmed_receipt_is_not_success():
    journal = Journal(on_append=_crash_on("callback", phase="backoff",
                                          status="start", attempt=1))

    @backoff.on_predicate(backoff.constant, jitter=None, interval=0,
                          max_tries=2)
    def poll():
        return False

    with pytest.raises(Crash):
        with _resume(journal, key="demo"):
            poll()

    attempt = journal.reconcile("demo").attempts[0]
    assert attempt.decision == "backoff"
    assert attempt.receipt is None
    assert attempt.confirmed is False
    assert attempt.pending_callbacks == ["backoff[0]"]
    assert attempt.status == "receipt_pending"


def test_late_ack_is_attached_to_original_attempt():
    clock = ManualClock()
    journal = Journal(clock=clock)
    ids = []

    @backoff.on_exception(backoff.constant, ValueError, jitter=None,
                          interval=1, max_tries=2)
    def fetch():
        ids.append(backoff.current_attempt_id())
        raise ValueError("boom")

    with _resume(journal, key="demo"):
        with pytest.raises(ValueError):
            fetch()

    first_id = ids[0]
    journal.acknowledge(first_id, value="stored", key="demo")
    attempts = journal.reconcile("demo").attempts
    assert attempts[0].acknowledged is True
    assert attempts[0].ack_late is True
    assert attempts[0].number == 1


def test_config_change_between_layers_uses_fresh_snapshot():
    clock = ManualClock()
    journal = Journal(clock=clock)
    config = {"max_tries": 1}

    def lookup_max_tries():
        return config["max_tries"]

    @backoff.on_predicate(backoff.constant, jitter=None, interval=1,
                          max_tries=lookup_max_tries)
    def inner(state):
        return False

    @backoff.on_predicate(backoff.constant, jitter=None, interval=1,
                          max_tries=5)
    def outer(state):
        state["outer"] += 1
        if state["outer"] == 2:
            # New configuration takes effect between an outer wake-up and the
            # next inner call.
            config["max_tries"] = 3
        if state["outer"] <= 2:
            inner(state)
        return state["outer"] >= 3

    with _resume(journal, key="demo"):
        assert outer({"outer": 0}) is True

    runs = journal.reconcile("demo").runs
    outer_name = "%s.%s" % (outer.__module__, outer.__qualname__)
    inner_name = "%s.%s" % (inner.__module__, inner.__qualname__)
    assert [(run.layer, run.number, run.config["max_tries"])
            for run in runs] == [
        (outer_name, 0, 5), (inner_name, 0, 1), (inner_name, 1, 3)]


def test_lookup_by_attempt_id_avoids_resending(tmp_path):
    clock = ManualClock()
    path = str(tmp_path / "journal.jsonl")
    sent = []
    stored = {}

    @backoff.on_exception(backoff.constant, ValueError, jitter=None,
                          interval=1, max_tries=1)
    def send():
        attempt = backoff.current_attempt_id()
        sent.append(attempt)
        stored[str(attempt)] = "ok"
        return "ok"

    journal = Journal(path, clock=clock,
                      on_before_append=_crash_on("attempt_result",
                                                 attempt=1))
    with pytest.raises(Crash):
        with _resume(journal, key="demo"):
            send()
    journal.close()

    assert len(sent) == 1

    def lookup(attempt):
        return stored.get(str(attempt), backoff.NOT_FOUND)

    restarted = Journal(path, clock=clock)
    with _resume(restarted, key="demo", lookup=lookup):
        assert send() == "ok"
    restarted.close()

    attempts = Journal(path).reconcile("demo").attempts
    assert len(attempts) == 1
    assert attempts[0].status == "success"
    assert attempts[0].confirmed is True
    assert len(sent) == 1


def test_persisted_journal_ignores_torn_trailing_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(str(path))
    with _resume(journal, key="demo"):
        @backoff.on_predicate(backoff.constant, jitter=None, interval=0,
                              max_tries=1)
        def poll():
            return True
        assert poll() is True
    journal.close()

    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 9, "kind": "partial"')

    reloaded = Journal(str(path), fsync=False)
    assert reloaded.reconcile("demo").attempts[0].status == "success"
    reloaded.close()


@pytest.mark.asyncio
async def test_async_durable_resume(tmp_path):
    clock = ManualClock()
    path = str(tmp_path / "journal.jsonl")
    sleeps = []
    journal = Journal(path, clock=clock,
                      on_append=_crash_on("backoff_receipt", attempt=1))

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    @backoff.on_predicate(backoff.constant, jitter=None, interval=5,
                          max_tries=3)
    async def poll(state):
        state["calls"] += 1
        return state["calls"] >= 2

    with pytest.raises(Crash):
        with _resume(journal, key="demo", sleep=sleep):
            await poll({"calls": 0})
    journal.close()

    clock.advance(2)
    restarted = Journal(path, clock=clock)
    with _resume(restarted, key="demo", sleep=sleep):
        assert await poll({"calls": 1}) is True
    restarted.close()

    attempts = Journal(path).reconcile("demo").attempts
    assert attempts[0].waited == 3
    assert [attempt.number for attempt in attempts] == [1, 2]


def test_inner_giveup_becomes_empty_and_outer_fetches_next_batch():
    clock = ManualClock()
    journal = Journal(clock=clock)
    calls = []

    @backoff.on_exception(backoff.constant, ValueError, jitter=None,
                          interval=1, max_tries=2, raise_on_giveup=False)
    def fetch_batch(cursor):
        calls.append(("inner", backoff.current_attempt_id()))
        raise ValueError("unavailable")

    @backoff.on_predicate(backoff.constant, jitter=None, interval=1,
                          max_tries=2)
    def collect(cursor):
        batch = fetch_batch(cursor["cursor"])
        calls.append(("outer", backoff.current_attempt_id()))
        if batch is None:
            cursor["cursor"] += 100
            return False
        return True

    with _resume(journal, key="demo"):
        assert collect({"cursor": 0}) is False

    report = journal.report("demo")
    inner = [attempt for attempt in report["attempts"]
             if "fetch_batch" in attempt["layer"]]
    outer = [attempt for attempt in report["attempts"]
             if "collect" in attempt["layer"]]
    seen = {(attempt["run"], attempt["attempt"]) for attempt in inner}
    assert sorted(seen) == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert [(attempt["run"], attempt["attempt"], attempt["decision"])
            for attempt in outer] == [(0, 1, "backoff"),
                                      (0, 2, "giveup")]
    assert [attempt["decision"] for attempt in inner] == [
        "backoff", "giveup", "backoff", "giveup"]
    assert inner[0]["confirmed"] is True
    assert inner[1]["confirmed"] is True
    # The outer layer received the visual empty list from a giveup, which is
    # recorded as a value, not mistaken for a successful fetch.
    assert outer[0]["outcome"] == "value"
    assert outer[0]["value"] is False


def test_resumed_and_uninterrupted_traces_match():
    def run(journal, state, crash=False):
        if crash:
            journal = Journal(journal.path, clock=ManualClock(),
                              on_append=_crash_on("backoff_receipt"))
        with _resume(journal, key="demo"):
            if crash:
                with pytest.raises(Crash):
                    _collect(state)
                journal.close()
                restarted = Journal(journal.path, clock=ManualClock())
                with _resume(restarted, key="demo"):
                    return _collect(state)
            return _collect(state)

    @backoff.on_predicate(backoff.constant, jitter=None, interval=2,
                          max_tries=3)
    def _collect(state):
        state["n"] += 1
        return state["n"] >= 3

    uninterrupted = run(Journal(clock=ManualClock()), {"n": 0})
    with __import__("tempfile").TemporaryDirectory() as directory:
        import os
        path = os.path.join(directory, "resumed.jsonl")
        journal = Journal(path, clock=ManualClock())
        resumed = run(journal, {"n": 0}, crash=True)
        a = backoff.reconcile(path, key="demo")
    assert resumed == uninterrupted
    expected = Journal(clock=ManualClock())
    with _resume(expected, key="demo"):
        state = {"n": 0}
        _collect(state)
    diff = a.compare(expected.reconcile("demo"))
    assert diff.matches, diff.to_dict()


def test_run_config_snapshot_survives_restart(tmp_path):
    clock = ManualClock()
    path = str(tmp_path / "journal.jsonl")
    journal = Journal(path, clock=clock)
    state = {"calls": 0}

    @backoff.on_predicate(backoff.constant, jitter=None, interval=1,
                          max_tries=2)
    def poll():
        state["calls"] += 1
        return state["calls"] >= 2

    with _resume(journal, key="demo"):
        assert poll() is True
    journal.close()

    # A later entry starts a new run and reads fresh configuration.
    journal2 = Journal(path, clock=clock)
    with _resume(journal2, key="demo"):
        assert poll() is True
    journal2.close()

    runs = Journal(path).reconcile("demo").runs
    assert len(runs) == 2
    assert runs[0].config["max_tries"] == runs[1].config["max_tries"] == 2


def test_sleep_until_absolute_does_not_double_count_downtime(tmp_path):
    clock = ManualClock(start=0.0)
    path = str(tmp_path / "journal.jsonl")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    @backoff.on_predicate(backoff.constant, jitter=None, interval=4,
                          max_tries=2)
    def poll(state):
        state["calls"] += 1
        return state["calls"] >= 2

    journal = Journal(path, clock=clock,
                      on_append=_crash_on("sleep_start", attempt=1))
    with pytest.raises(Crash):
        with _resume(journal, key="demo", sleep=sleep):
            poll({"calls": 0})
    journal.close()

    clock.advance(3)
    restarted = Journal(path, clock=clock)
    with _resume(restarted, key="demo", sleep=sleep):
        assert poll({"calls": 1}) is True
    restarted.close()

    attempt = Journal(path).reconcile("demo").attempts[0]
    assert attempt.wake_at == 4.0
    assert attempt.sleep_started_at == 0.0
    assert attempt.waited == 1.0
