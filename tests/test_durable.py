"""Durable execution: resume a run at the step it reached, with idempotent effects.

The scenario is a sampling job.  A ``StateChart`` runs two collection zones in parallel:
one zone waits for a child job and stores the sample it delivers, the other writes a
summary receipt on its own.  The data may only land once both zones are finished, so a
restart has to resume at the exact position it left — not from the initial state, and not
by skipping the failure it had already raised.

The tests drive two exit points:

- **Exit during failure handling.**  A sampling action lands the write, then raises; the
  ``after`` callback still leaves the same closing slip, and the error goes to the
  internal queue as ``error.execution``.  The run exits before that internal event is
  consumed.  A review had been queued earlier on the external queue.  The resume must
  settle the failure and the writes it left before the review can advance a round.
- **Exit right after the warehouse accepted.**  The sample warehouse received the stable
  id; the run exits before the chart records it as done.  The ticket written on the first
  attempt is reused, and the summary already written by the other zone is not lost.

A second group covers a declared invoke id reused by a re-entered waiting state: the new
round gets the next generation, a late ``done.invoke`` from the retired round is dropped,
and a delayed notification can still be attributed to the call that produced it.
"""

import asyncio
import threading
import time

import pytest
from statemachine.durable import Checkpoint
from statemachine.durable import DurableStateChart
from statemachine.durable import EffectReceipt
from statemachine.durable import FailureRecord
from statemachine.durable import Journal
from statemachine.durable import MemoryCheckpointStore
from statemachine.durable import PickleFileStore
from statemachine.durable import RestoredExecutionError
from statemachine.durable import load_checkpoint
from statemachine.event import BoundEvent
from statemachine.event_data import TriggerData

from statemachine import Event
from statemachine import State
from statemachine import StateChart


class Warehouse:
    """Sample warehouse: receives a stable id at most once and reports it."""

    def __init__(self):
        self.received: dict[str, object] = {}
        self.calls = 0

    def receive(self, key, ticket):
        self.calls += 1
        if key in self.received:
            return False, self.received[key]
        self.received[key] = ticket
        return True, ticket


class SamplingRun(DurableStateChart):
    """Two collection zones; the data lands once ``done.state.collect`` fires."""

    class collect(State.Parallel):
        class sample_zone(State.Compound):
            waiting = State(initial=True)
            landed = State(final=True)

            store = waiting.to(landed, on="store_sample", after="after_store")

        class summary_zone(State.Compound):
            idle = State(initial=True)
            posted = State(final=True)

            post = idle.to(posted, on="write_summary")

    stored = State(final=True)
    audited = State(final=True)

    # The engine emits `done.state.collect` only when every region is final, so that is
    # the only way the data lands.
    done_state_collect = Event(collect.to(stored), id="done.state.collect")
    error_execution = Event(collect.to(audited), id="error.execution")

    def __init__(self, warehouse=None, **kwargs):
        self.warehouse = warehouse
        self.sample_writes = []
        self.summary_writes = []
        self.slips = []
        self.armed = False
        self.fail_store = False
        super().__init__(**kwargs)

    # Exposed under the names the test asserts on; `landed`/`posted` are state IDs.
    @property
    def slip_count(self):
        return len(self.slips)

    def store_sample(self, key="sample:S-77", ticket="T-77", **kwargs):
        created, received = self.warehouse.receive(key, ticket)
        receipt = self.accept_effect(key, ticket=received)
        self.sample_writes.append((key, receipt.ticket, created))
        if self.fail_store:
            raise RuntimeError("warehouse hiccup")

    def after_store(self, **kwargs):
        receipt = self.accept_effect("slip:store", ticket="END-S77")
        self.slips.append((receipt.key, receipt.ticket))
        if self.armed:
            self.send("review")
            self.halt()

    def write_summary(self, key="summary:zone-b", ticket="R-ZB", **kwargs):
        receipt = self.accept_effect(key, ticket=ticket)
        self.summary_writes.append((receipt.key, receipt.ticket))


@pytest.fixture()
def run_factory():
    """Build a run sharing a checkpoint store, a journal and one warehouse."""

    def factory(cls, store, journal, warehouse, run_id="sampling"):
        return cls(
            warehouse=warehouse,
            checkpoint_store=store,
            journal=journal,
            run_id=run_id,
        )

    return factory


class TestResumeDuringFailureHandling:
    """Exit after a write raised and before ``error.execution`` was consumed."""

    def test_resume_settles_the_failure_before_a_queued_review(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")  # summary zone writes its own receipt first
        assert set(sm.configuration_values) == {
            "collect",
            "posted",
            "sample_zone",
            "summary_zone",
            "waiting",
        }

        sm.fail_store = True
        sm.armed = True
        sm.send("store")

        # The action landed the sample, then raised.  after_store still left its slip and
        # queued the review; the exit came before error.execution was consumed.
        assert sm.sample_writes == [("sample:S-77", "T-77", True)]
        assert sm.slips == [("slip:store", "END-S77")]

        checkpoint = load_checkpoint(store, "sampling")
        assert [item.to_dict() for item in checkpoint.failures] == [
            {"type": "RuntimeError", "message": "warehouse hiccup"}
        ]
        assert [item.event for item in checkpoint.internal] == [
            "error.execution",
            "done.state.sample_zone",
            "done.state.collect",
        ]
        assert [item.event for item in checkpoint.external] == ["review"]
        assert "summary:zone-b" in checkpoint.receipts
        assert "sample:S-77" in checkpoint.receipts
        assert "slip:store" in checkpoint.receipts

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        assert resumed.is_restored
        assert set(resumed.configuration_values) == {
            "collect",
            "landed",
            "posted",
            "sample_zone",
            "summary_zone",
        }
        # The warehouse accepted the stable id only once.
        assert warehouse.calls == 1
        assert warehouse.received == {"sample:S-77": "T-77"}

        resumed.resume()

        # The pending error was handled first: the run moved to the audit branch, not to
        # the next round that the queued review would have started.
        assert set(resumed.configuration_values) == {"audited"}
        assert resumed.resume_report["pending_failures"] == [
            {"type": "RuntimeError", "message": "warehouse hiccup"}
        ]

    def test_resumed_error_handler_receives_rebuilt_error(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        class RecordingRun(SamplingRun):
            def on_enter_audited(self, error=None, **kwargs):
                self.seen_error = error

        sm = run_factory(RecordingRun, store, journal, warehouse)
        sm.fail_store = True
        sm.armed = True
        sm.send("store")
        assert sm.slips == [("slip:store", "END-S77")]

        resumed = run_factory(RecordingRun, store, journal, warehouse)
        resumed.resume()
        # A builtin exception is rebuilt as itself; the type and message survive.
        assert isinstance(resumed.seen_error, RuntimeError)
        assert str(resumed.seen_error) == "warehouse hiccup"

    def test_other_zone_receipt_is_already_landed_when_the_failure_resumes(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")
        sm.fail_store = True
        sm.armed = True
        sm.send("store")

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        # The summary zone already handed in its receipt; it is in the checkpoint, not lost.
        assert resumed.has_effect("summary:zone-b")
        resumed.resume()
        assert set(resumed.configuration_values) == {"audited"}


class TestResumeAfterWarehouseAccepted:
    """Exit after the sample warehouse took the stable id, before the chart recorded it."""

    def test_reused_ticket_and_summary_of_the_other_zone(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")  # summary zone done
        sm.send("store")
        assert sm.sample_writes == [("sample:S-77", "T-77", True)]
        assert sm.summary_writes == [("summary:zone-b", "R-ZB")]

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        assert warehouse.calls == 1
        assert resumed.has_effect("sample:S-77")
        assert resumed.has_effect("summary:zone-b")
        # Both zones had already reached their final state, so the run had landed the data
        # before the exit: that is the position the resume continues from.
        assert "stored" in resumed.configuration_values

        # A repeated attempt at the same stable id is a replay: the first ticket is used
        # and the warehouse is not asked a second time.
        receipt, created = resumed.receive_effect("sample:S-77", ticket="T-77-NEW")
        assert created is False
        assert receipt == EffectReceipt(key="sample:S-77", ticket="T-77", accepted=True)

    def test_both_zones_must_be_done_before_the_data_lands(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("store")
        assert "landed" in sm.configuration_values
        assert "posted" not in sm.configuration_values
        assert "stored" not in sm.configuration_values

        # The parallel closes only when every region is final; the data lands then.
        sm.send("post")
        assert "stored" in sm.configuration_values


class TestSameLookingWaitingStates:
    """Two waiting states with the same look but different receipts."""

    def test_visual_equality_is_not_receipt_equality(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)

        # Both zones are waiting states; a caller reading configuration values sees the
        # same label whichever zone it is asking about.
        assert "waiting" in sm.configuration_values
        assert "idle" in sm.configuration_values
        assert sm.sample_zone.waiting.is_active

        # Their receipts are keyed by what the zone did, not by the state's label, so a
        # replay of one zone cannot satisfy the other.
        sm.accept_effect("alpha:sample", ticket="A-1", state_id="sample_zone.waiting")
        sm.accept_effect("beta:sample", ticket="B-1", state_id="summary_zone.idle")
        assert sm.journal.get("alpha:sample").state_id == "sample_zone.waiting"
        assert sm.journal.get("beta:sample").state_id == "summary_zone.idle"
        assert sm.journal.get("alpha:sample").ticket != sm.journal.get("beta:sample").ticket


class TestInvokeIdReuseAcrossGenerations:
    """A re-entered waiting state reuses its declared invoke id, but not its round."""

    def _make_round(self):
        release = {1: threading.Event(), 2: threading.Event()}

        class Job:
            invoke_id = "sample-job"

            def run(self, ctx):
                release[ctx.generation].wait(2.0)
                return f"sample-{ctx.generation}"

            def on_cancel(self):
                pass

        class Round(DurableStateChart):
            idle = State(initial=True)
            waiting = State(invoke=Job)
            landed = State(final=True)

            start = idle.to(waiting)
            leave = waiting.to(idle)
            reenter = idle.to(waiting)
            # A declared invoke id names its own completion event; the
            # `done_invoke_<state>` convention would only match `done.invoke.waiting`.
            done_invoke_waiting = Event(waiting.to(landed), id="done.invoke.sample-job")

        return Round, release

    def test_reused_id_gets_a_new_generation(self):
        Round, release = self._make_round()
        sm = Round(checkpoint_store=MemoryCheckpointStore(), journal=Journal())
        sm.send("start")
        assert sm._engine._invoke_manager._generations["sample-job"] == 1

        sm.send("leave")
        assert "idle" in sm.configuration_values
        sm.send("reenter")
        assert sm._engine._invoke_manager._generations["sample-job"] == 2

        # A late completion of the retired round carries generation 1 and is dropped.
        sm.send("done.invoke.sample-job", data="late", _generation=1)
        assert "waiting" in sm.configuration_values

        release[2].set()
        for _ in range(50):
            time.sleep(0.02)
            sm._processing_loop()
            if "landed" in sm.configuration_values:
                break
        assert "landed" in sm.configuration_values
        release[1].set()

    def test_generation_survives_a_checkpoint(self):
        Round, release = self._make_round()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = Round(checkpoint_store=store, journal=journal, run_id="round")
        sm.send("start")
        sm.send("leave")
        sm.send("reenter")

        checkpoint = sm.checkpoint()
        assert checkpoint.invoke_generations == {"sample-job": 2}

        resumed = Round(checkpoint_store=store, journal=journal, run_id="round")
        # A completion of the previous round no longer passes: the checkpoint kept the
        # live id at generation 2.
        resumed.send("done.invoke.sample-job", data="late", _generation=1)
        assert "waiting" in resumed.configuration_values

        # A completion of the current round does pass.
        resumed.send("done.invoke.sample-job", data="fresh", _generation=2)
        assert "landed" in resumed.configuration_values
        release[1].set()
        release[2].set()

    def test_delayed_notification_is_attributable_to_its_call(self):
        sm = SamplingRun(
            warehouse=Warehouse(),
            checkpoint_store=MemoryCheckpointStore(),
            journal=Journal(),
        )
        receipt = sm.accept_effect(
            "lane:sample", ticket="L-1", generation=4, state_id="sample-job"
        )
        assert receipt.generation == 4
        assert receipt.state_id == "sample-job"

        # A later replay of the same id keeps the round of the call that produced it.
        replayed = sm.accept_effect("lane:sample", ticket="L-2")
        assert replayed.generation == 4
        assert replayed.ticket == "L-1"


class TestCheckpointRoundTrip:
    """The checkpoint survives an actual process boundary through a file store."""

    def test_pickle_file_store_round_trip(self, tmp_path):
        warehouse = Warehouse()
        store = PickleFileStore(tmp_path)
        journal = Journal()

        sm = SamplingRun(
            warehouse=warehouse, checkpoint_store=store, journal=journal, run_id="file-run"
        )
        sm.send("post")
        sm.send("store")
        assert "stored" in sm.configuration_values

        resumed = SamplingRun(
            warehouse=warehouse, checkpoint_store=store, journal=journal, run_id="file-run"
        )
        assert resumed.is_restored
        assert "stored" in resumed.configuration_values
        assert resumed.has_effect("summary:zone-b")
        assert warehouse.calls == 1

    def test_unsupported_version_is_rejected(self):
        store = MemoryCheckpointStore()
        store.save("x", {"version": 99, "configuration": []})
        with pytest.raises(Exception, match="Unsupported checkpoint version"):
            load_checkpoint(store, "x")

    def test_unknown_state_is_rejected(self):
        store = MemoryCheckpointStore()
        store.save("x", {"version": 1, "configuration": ["nowhere"]})

        class Tiny(DurableStateChart):
            a = State(initial=True)
            b = State(final=True)
            go = a.to(b)

        with pytest.raises(Exception, match="nowhere"):
            Tiny(checkpoint_store=store, journal=Journal(), run_id="x")

    def test_resume_without_checkpoint_is_rejected(self):
        class Tiny(DurableStateChart):
            a = State(initial=True)
            b = State(final=True)
            go = a.to(b)

        sm = Tiny(checkpoint_store=MemoryCheckpointStore(), journal=Journal())
        with pytest.raises(Exception, match="no checkpoint"):
            sm.resume()

    def test_checkpoint_round_trips_through_its_dict(self):
        checkpoint = Checkpoint(
            configuration=["a"],
            internal=[],
            external=[],
            invoke_generations={"job": 2},
            receipts={"k": EffectReceipt(key="k", ticket="t").to_dict()},
        )
        restored = Checkpoint.from_dict(checkpoint.to_dict())
        assert restored.configuration == ["a"]
        assert restored.invoke_generations == {"job": 2}
        assert restored.receipts == {"k": EffectReceipt(key="k", ticket="t").to_dict()}


class _AsyncListener:
    """No-op async listener that makes the engine async."""

    async def on_enter_state(self, **kwargs): ...


class TestRemainingEventsAfterResume:
    """After a resume, the triggers left are claimable in their original order."""

    def test_pending_external_triggers_keep_their_order(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")

        # Queue two events the chart has no transition for, so the loop leaves them and
        # they stay claimable in the order they were queued.
        for event_name in ("audit", "review"):
            sm._engine.external_queue.put(
                TriggerData(machine=sm, event=BoundEvent(event_name, _sm=sm))
            )

        checkpoint = sm.checkpoint()
        assert [item.event for item in checkpoint.external] == ["audit", "review"]

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        assert [str(t.event) for t in resumed._engine.external_queue.snapshot()] == [
            "audit",
            "review",
        ]
        assert [item.event for item in resumed.checkpoint().external] == ["audit", "review"]

    def test_async_pending_failure_is_settled_before_external_triggers(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        class AsyncRun(SamplingRun):
            async def on_enter_audited(self, **kwargs):
                self.audited_async = True

        async def drive():
            listener = _AsyncListener()
            sm = AsyncRun(
                warehouse=warehouse,
                checkpoint_store=store,
                journal=journal,
                run_id="async",
                listeners=[listener],
            )
            await sm.activate_initial_state()
            sm.fail_store = True
            sm.armed = True
            await sm.send("store")

            resumed = AsyncRun(
                warehouse=warehouse,
                checkpoint_store=store,
                journal=journal,
                run_id="async",
                listeners=[listener],
            )
            assert resumed.is_restored
            await resumed.resume()
            assert set(resumed.configuration_values) == {"audited"}
            assert resumed.audited_async is True

        asyncio.run(drive())


class TestPlainStateChartIsUnaffected:
    """Without durable opt-in, the engine's new seams change nothing."""

    def test_plain_chart_never_writes_a_checkpoint(self):
        class Plain(StateChart):
            class collect(State.Parallel):
                class sample_zone(State.Compound):
                    waiting = State(initial=True)
                    landed = State(final=True)

                    store = waiting.to(landed)

                class summary_zone(State.Compound):
                    idle = State(initial=True)
                    posted = State(final=True)

                    post = idle.to(posted)

            stored = State(final=True)
            done_state_collect = Event(collect.to(stored), id="done.state.collect")

        sm = Plain()
        sm.send("post")
        sm.send("store")
        assert set(sm.configuration_values) == {"stored"}
        assert not hasattr(sm, "checkpoint_store")


class TestEngineSeams:
    """The step-boundary seams a durable chart uses, exercised on their own."""

    def test_plain_chart_does_not_treat_a_generationed_event_as_stale(self):
        class Plain(StateChart):
            waiting = State(initial=True)
            landed = State(final=True)
            go = waiting.to(landed)

        sm = Plain()
        # A plain chart assigns no generation and tracks no live round, so an event that
        # happens to carry a generation is still taken; only a durable chart drops it.
        sm.send("go", _generation=1)
        assert "landed" in sm.configuration_values

    def test_settle_internal_leaves_the_external_trigger_queued(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")

        # Queue an external trigger the chart cannot claim, then settle the internal step.
        sm._engine.external_queue.put(TriggerData(machine=sm, event=BoundEvent("review", _sm=sm)))
        sm.settle_internal()
        assert [str(t.event) for t in sm._engine.external_queue.snapshot()] == ["review"]

    def test_advancing_the_macrostep_count_is_resumed(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")
        counts = sm.snapshot()
        assert counts["macrostep"] >= 1

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        assert resumed.snapshot()["macrostep"] >= counts["macrostep"]


class TestEffectBranches:
    """The small branches of effect receipts and journal merging."""

    def test_journal_merge_never_overwrites_its_own_receipt(self):
        journal = Journal()
        journal.accept("k", ticket="first")
        journal.merge_missing(
            {"k": EffectReceipt(key="k", ticket="second"), "j": EffectReceipt("j")}
        )
        assert journal.get("k").ticket == "first"
        assert journal.get("j") is not None
        assert "j" in journal
        assert len(journal) == 2

    def test_rebuilt_error_falls_back_for_a_non_builtin_type(self):
        from statemachine.durable import _rebuild_error

        record = FailureRecord(type_name="CustomBoom", message="boom")
        error = _rebuild_error(record)
        assert isinstance(error, RestoredExecutionError)
        assert error.type_name == "CustomBoom"

    def test_restored_error_keeps_type_and_message_in_a_dict_form(self):
        record = FailureRecord(type_name="ValueError", message="bad")
        assert FailureRecord.from_dict(record.to_dict()) == record
        from statemachine.durable import _rebuild_error as rebuild

        error = rebuild(record)
        assert isinstance(error, ValueError)
        assert str(error) == "bad"

    def test_skip_effect_returns_same_receipt_on_replay(self):
        class Simple(DurableStateChart):
            a = State(initial=True)
            b = State(final=True)
            go = a.to(b)

        sm = Simple(checkpoint_store=MemoryCheckpointStore(), journal=Journal())
        first, created = sm.receive_effect("k", ticket="t1")
        assert created is True
        second, created = sm.receive_effect("k", ticket="t2")
        assert created is False
        assert second.ticket == "t1"


class TestDurableHelpers:
    """Small helpers and branches around the durable layer."""

    def test_journal_keys_and_from_dict(self):
        journal = Journal.from_dict({"a": {"key": "a", "ticket": 1}})
        assert journal.keys() == ["a"]
        assert Journal.from_dict(None).keys() == []

    def test_clear_checkpoint_and_store_clear(self):
        class Tiny(DurableStateChart):
            a = State(initial=True)
            b = State(final=True)
            go = a.to(b)

        store = MemoryCheckpointStore()
        sm = Tiny(checkpoint_store=store, journal=Journal(), run_id="tiny")
        sm.send("go")
        assert store.load("tiny") is not None
        sm.clear_checkpoint()
        assert store.load("tiny") is None

    def test_memory_store_clear_of_unknown_run_is_silent(self):
        MemoryCheckpointStore().clear("never-saved")

    def test_pickle_store_clear_removes_the_file(self, tmp_path):
        store = PickleFileStore(tmp_path)
        store.save("run", {"version": 1, "configuration": []})
        assert store.load("run") is not None
        store.clear("run")
        assert store.load("run") is None

    def test_journal_to_dict_round_trip(self):
        journal = Journal()
        journal.accept("k", ticket="t", generation=2, state_id="s")
        rebuilt = Journal.from_dict(journal.to_dict())
        assert rebuilt.get("k").generation == 2
        assert rebuilt.get("k").state_id == "s"

    def test_queue_record_round_trip_and_error_encoding(self):
        from statemachine.durable import QueueRecord
        from statemachine.durable import _decode_kwargs
        from statemachine.durable import _encode_kwargs

        encoded = _encode_kwargs({"error": ValueError("bad"), "other": 1})
        assert encoded["error"] == {"__durable_error__": {"type": "ValueError", "message": "bad"}}
        decoded = _decode_kwargs(encoded)
        assert isinstance(decoded["error"], ValueError)
        assert decoded["other"] == 1

        record = QueueRecord(
            event="error.execution",
            args=(),
            kwargs=encoded,
            generation=None,
            send_id="s",
            remaining=0.5,
            sequence=3,
        )
        assert QueueRecord.from_dict(record.to_dict()) == record
        assert QueueRecord.from_dict(record.to_dict()).args == ()

    def test_restore_uses_a_declared_event_definition(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")
        sm.checkpoint()

        # Restored triggers refer to the declared event objects where there is one.
        resumed = run_factory(SamplingRun, store, journal, warehouse)
        trigger = TriggerData(machine=resumed, event=BoundEvent("store", _sm=resumed))
        assert resumed._restore_event("store").id == "store"
        assert resumed._restore_event(None) is None
        assert trigger.event is not None

    def test_checkpointing_is_not_reentrant(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        calls = []
        original = sm.checkpoint

        def counting():
            calls.append(1)
            return original()

        # A nested call from the checkpoint hook is dropped instead of recursing.
        sm._checkpointing = True
        sm._checkpoint_after_step()
        sm._checkpointing = False
        sm._checkpoint_after_step()
        assert calls == []


class TestAttributionFromTheRunningTrigger:
    """A receipt takes its round and call from the trigger being processed."""

    def test_round_and_call_are_taken_from_the_running_trigger(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        calling = TriggerData(
            machine=sm,
            event=BoundEvent("done.invoke.sample-job", _sm=sm),
            generation=5,
            kwargs={"_invokeid": "sample-job"},
        )
        sm._engine._current_trigger = calling

        receipt_receive = sm.receive_effect("from:receive", ticket="T")
        assert receipt_receive[0].generation == 5
        assert receipt_receive[0].state_id == "sample-job"

        receipt_accept = sm.accept_effect("from:accept", ticket="A")
        assert receipt_accept.generation == 5
        assert receipt_accept.state_id == "sample-job"

    def test_explicit_attribution_wins_over_the_running_trigger(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm._engine._current_trigger = TriggerData(
            machine=sm, event=BoundEvent("x", _sm=sm), generation=9, kwargs={"_invokeid": "i9"}
        )
        receipt, created = sm.receive_effect("explicit", ticket="T", generation=1, state_id="s1")
        assert receipt.generation == 1
        assert receipt.state_id == "s1"
        assert created is True
        sm._engine._current_trigger = None


class TestPendingCompletionAcrossRestart:
    """A completion queued before the exit is still claimed by its round after it."""

    def _chart(self):
        release = threading.Event()

        class Job:
            invoke_id = "sample-job"

            def run(self, ctx):
                release.wait(2.0)
                return "sample"

            def on_cancel(self):
                pass

        class WaitingRound(DurableStateChart):
            idle = State(initial=True)
            waiting = State(invoke=Job, enter="note_enter")
            landed = State(final=True)

            start = idle.to(waiting)
            done_invoke_waiting = Event(waiting.to(landed), id="done.invoke.sample-job")

            entries = 0

            def note_enter(self, **kwargs):
                type(self).entries += 1

        return WaitingRound, release

    def test_queued_completion_is_taken_after_the_restart(self):
        WaitingRound, release = self._chart()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = WaitingRound(checkpoint_store=store, journal=journal, run_id="pending")
        sm.send("start")
        assert WaitingRound.entries == 1

        # Queue the completion the child will send, then exit before it is claimed.
        sm._engine.external_queue.put(
            TriggerData(
                machine=sm,
                event=BoundEvent("done.invoke.sample-job", _sm=sm),
                generation=1,
                kwargs={"_invokeid": "sample-job"},
            )
        )
        sm.halt()
        checkpoint = load_checkpoint(store, "pending")
        assert [item.event for item in checkpoint.external] == ["done.invoke.sample-job"]
        assert checkpoint.invoke_generations == {"sample-job": 1}

        resumed = WaitingRound(checkpoint_store=store, journal=journal, run_id="pending")
        # The restart did not re-enter the waiting state.
        assert WaitingRound.entries == 1
        assert "waiting" in resumed.configuration_values

        # The completion queued before the exit is still claimable, by the round it names.
        resumed._processing_loop()
        assert "landed" in resumed.configuration_values
        assert WaitingRound.entries == 1
        release.set()

    def test_stale_completion_in_the_queue_does_not_land_the_new_round(self):
        WaitingRound, release = self._chart()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = WaitingRound(checkpoint_store=store, journal=journal, run_id="pending2")
        sm.send("start")
        # A completion from a round that never existed here carries generation 0.
        sm._engine.external_queue.put(
            TriggerData(
                machine=sm,
                event=BoundEvent("done.invoke.sample-job", _sm=sm),
                generation=0,
                kwargs={"_invokeid": "sample-job"},
            )
        )
        sm.halt()

        resumed = WaitingRound(checkpoint_store=store, journal=journal, run_id="pending2")
        resumed.resume()
        assert "waiting" in resumed.configuration_values
        release.set()


class _NoEventHandler:
    """Handler without ``on_event``; ``send_to_child`` has nothing to forward to."""

    def run(self, ctx):
        return None


class _RecordingHandler:
    """Handler that records the events forwarded to its child."""

    def __init__(self):
        self.events = []
        self.autoforward = True

    def run(self, ctx):
        return None

    def on_event(self, name, **data):
        self.events.append(name)


class TestInvokeManagerSeams:
    """The narrow paths a durable run relies on in the invoke manager."""

    def test_completion_of_an_unknown_id_is_not_stale(self, run_factory):
        from statemachine.event import BoundEvent as B

        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        unknown = TriggerData(
            machine=sm,
            event=B("done.invoke.nobody", _sm=sm),
            generation=3,
            kwargs={},
        )
        assert sm._engine._invoke_manager.is_stale_completion(unknown) is False

    def test_non_completion_with_a_generation_is_not_stale(self, run_factory):
        from statemachine.event import BoundEvent as B

        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        other = TriggerData(machine=sm, event=B("post", _sm=sm), generation=3, kwargs={})
        assert sm._engine._invoke_manager.is_stale_completion(other) is False

    def test_send_to_child_skips_a_cancelled_round(self):
        from statemachine.invoke import Invocation
        from statemachine.invoke import InvokeContext

        sm = SamplingRun(
            warehouse=Warehouse(),
            checkpoint_store=MemoryCheckpointStore(),
            journal=Journal(),
        )
        ctx = InvokeContext(invokeid="i", state_id="s", send=sm.send, machine=sm)
        ctx.cancelled.set()
        sm._engine._invoke_manager._active["i"] = Invocation(invokeid="i", state_id="s", ctx=ctx)
        assert sm._engine._invoke_manager.send_to_child("i", "go") is False

    def test_send_to_child_with_a_handler_that_does_not_forward(self):
        from statemachine.invoke import Invocation
        from statemachine.invoke import InvokeContext

        sm = SamplingRun(
            warehouse=Warehouse(),
            checkpoint_store=MemoryCheckpointStore(),
            journal=Journal(),
        )
        ctx = InvokeContext(invokeid="i", state_id="s", send=sm.send, machine=sm)
        sm._engine._invoke_manager._active["i"] = Invocation(
            invokeid="i", state_id="s", ctx=ctx, _handler=_NoEventHandler()
        )
        assert sm._engine._invoke_manager.send_to_child("i", "go") is False
        del sm._engine._invoke_manager._active["i"]

    def test_send_to_child_forwards_to_the_live_handler(self):
        from statemachine.invoke import Invocation
        from statemachine.invoke import InvokeContext

        sm = SamplingRun(
            warehouse=Warehouse(),
            checkpoint_store=MemoryCheckpointStore(),
            journal=Journal(),
        )
        handler = _RecordingHandler()
        ctx = InvokeContext(invokeid="i", state_id="s", send=sm.send, machine=sm)
        sm._engine._invoke_manager._active["i"] = Invocation(
            invokeid="i", state_id="s", ctx=ctx, _handler=handler
        )
        assert sm._engine._invoke_manager.send_to_child("i", "go") is True
        assert handler.events == ["go"]
        del sm._engine._invoke_manager._active["i"]

    def test_autoforward_sends_a_parent_event_to_the_child(self):
        from statemachine.invoke import Invocation
        from statemachine.invoke import InvokeContext

        sm = SamplingRun(
            warehouse=Warehouse(),
            checkpoint_store=MemoryCheckpointStore(),
            journal=Journal(),
        )
        handler = _RecordingHandler()
        ctx = InvokeContext(invokeid="i", state_id="s", send=sm.send, machine=sm)
        sm._engine._invoke_manager._active["i"] = Invocation(
            invokeid="i", state_id="s", ctx=ctx, _handler=handler
        )
        trigger = TriggerData(machine=sm, event=BoundEvent("post", _sm=sm), kwargs={})
        sm._engine._invoke_manager.handle_external_event(trigger)
        assert handler.events == ["post"]
        del sm._engine._invoke_manager._active["i"]


class TestDueTimeReanchoring:
    """A delayed trigger keeps the wait it had left, not an absolute clock reading."""

    def test_delay_is_reanchored_on_the_restored_process_clock(self, run_factory):
        from time import monotonic

        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")

        # Queue a review with a delay that has not elapsed yet, without driving the loop
        # (which would wait for it).
        delayed = BoundEvent("review", delay=60_000, _sm=sm)
        sm._engine.external_queue.put(delayed.build_trigger(machine=sm))
        assert sm._engine.external_queue.snapshot()[0].execution_time > monotonic()
        checkpoint = sm.checkpoint()
        assert [item.event for item in checkpoint.external] == ["review"]
        recorded = checkpoint.external[0].remaining
        assert recorded > 0

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        trigger = resumed._engine.external_queue.snapshot()[0]
        delay_left = trigger.execution_time - monotonic()
        # The wait left is the recorded one, re-anchored on this process's clock, so a
        # reboot that reset the monotonic origin cannot make the trigger due at once.
        assert 0 < delay_left <= recorded + 1


class TestLiveRounds:
    """The round of a reused invoke id can be read from the chart."""

    def test_live_rounds_lists_the_current_generation(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        rounds = sm.live_rounds()
        assert isinstance(rounds, dict)
        # The zones hold no invocation, so no round is live here.
        assert rounds == {}


class TestCompletedRunStaysCompleted:
    """A run that reached a final state is not revived by a restart."""

    def test_restored_completed_run_is_terminated(self, run_factory):
        warehouse = Warehouse()
        store = MemoryCheckpointStore()
        journal = Journal()

        sm = run_factory(SamplingRun, store, journal, warehouse)
        sm.send("post")
        sm.send("store")
        assert "stored" in sm.configuration_values
        assert sm.is_terminated is True
        assert sm.checkpoint().running is False

        resumed = run_factory(SamplingRun, store, journal, warehouse)
        assert "stored" in resumed.configuration_values
        assert resumed.is_terminated is True
        assert resumed.snapshot()["running"] is False
