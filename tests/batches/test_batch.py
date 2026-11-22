from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from dramatiq.batches import (
    CANCELLED,
    COMPLETED,
    PENDING,
    TERMINATED,
    Batch,
    StubBatchStateBackend,
)
from dramatiq.batches.errors import (
    BatchAlreadyExists,
    BatchNotFound,
    BatchOpen,
    BatchSettled,
    BatchVersionConflict,
    DuplicateObligation,
    ObligationClosed,
    ObligationNotFound,
)


@pytest.fixture()
def backend():
    return StubBatchStateBackend()


def make_batch(backend, batch_id="nightly"):
    # The initial registration only knows East1 and West1.
    return Batch.open(
        batch_id,
        backend=backend,
        obligations=[
            {"key": "East1", "site": "East", "detail": {"page": 1}},
            {"key": "West1", "site": "West", "detail": {"page": 1}},
        ],
    )


def test_batch_opens_with_only_the_registered_obligations(backend):
    # Given a batch opened with two obligations
    batch = make_batch(backend)

    # Then both are pending and the batch cannot settle
    assert [o["key"] for o in batch.pending_obligations] == ["East1", "West1"]
    assert not batch.can_settle
    assert batch.version == 3


def test_unknown_batch_cannot_be_reloaded(backend):
    # Given a backend without any batch
    # Then reloading an unknown id raises
    with pytest.raises(BatchNotFound):
        Batch("missing", backend=backend).reload()


def test_opening_the_same_batch_twice_is_rejected(backend):
    # Given an existing batch
    make_batch(backend)

    # When I open the same id again
    # Then that is rejected
    with pytest.raises(BatchAlreadyExists):
        Batch.open("nightly", backend=backend)


def test_cannot_settle_while_the_east1_submission_has_not_committed(backend):
    # Given West1 confirmed it has no continuation pages and completed
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})

    # And East1's success notification arrived but its page submission
    # (which carries the promised East2) has not committed yet, e.g. the
    # broker restarted before the registration receipt was persisted
    # Then East1 itself is still unresolved and the batch cannot settle
    assert not batch.can_settle
    with pytest.raises(BatchOpen):
        batch.settle()

    # When the notification is replayed after recovery, the submission
    # commits East1's completion and East2's registration atomically
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East", "detail": {"page": 2}}],
    )

    # Then there is still no settleable window: East2 is now pending
    assert not batch.can_settle
    with pytest.raises(BatchOpen) as exc_info:
        batch.settle()
    assert "East2" in str(exc_info.value)


def test_page_submission_atomically_completes_and_registers_discovered_branch(backend):
    # Given the initial batch
    batch = make_batch(backend)

    # When East1 submits its page together with the East2 continuation it found
    newly_accepted = batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East", "detail": {"page": 2}}],
    )

    # Then East1 is complete and East2 is pending in the same committed state
    assert batch.get_obligation("East1")["status"] == COMPLETED
    assert batch.get_obligation("East2")["status"] == PENDING
    assert [o["key"] for o in newly_accepted] == ["East2"]
    assert not batch.can_settle


def test_lost_registration_receipt_never_creates_a_second_obligation(backend):
    # Given East1 already submitted and registered East2
    batch = make_batch(backend)
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East", "detail": {"page": 2}}],
    )
    version_after_submission = batch.version

    # When the registration is retried after the broker restart
    batch.accept_obligation("East2", "East", detail={"page": 2})
    # And the whole submission is redelivered
    redelivered = batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East", "detail": {"page": 2}}],
    )

    # Then exactly one East2 obligation exists and no extra version bump happened
    assert list(batch.state["obligations"]).count("East2") == 1
    assert redelivered == []
    assert batch.version == version_after_submission


def test_reusing_an_obligation_key_for_another_site_is_rejected(backend):
    # Given an East2 obligation registered to East
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")

    # When something tries to register the same key against another site
    # Then that is rejected rather than silently re-homed
    with pytest.raises(DuplicateObligation):
        batch.accept_obligation("East2", "South")


def test_administrative_stop_requires_the_effective_batch_version(backend):
    # Given a batch with an East2 obligation
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")
    current_version = batch.version

    # When an admin terminates East2's follow-up crawl with the right version
    batch.terminate_obligation("East2", effective_version=current_version, range={"pages": [2]})

    # Then the obligation is terminated and keeps its final range
    record = batch.get_obligation("East2")
    assert record["status"] == TERMINATED
    assert record["range"] == {"pages": [2]}

    # And a stop command carrying an older version is rejected
    with pytest.raises(BatchVersionConflict):
        batch.cancel_obligation("East2", effective_version=current_version - 1)


def test_obligation_that_has_not_started_can_be_cancelled(backend):
    # Given a freshly registered obligation that no worker has started
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")

    # When the admin cancels it against the current version
    batch.cancel_obligation("East2", effective_version=batch.version, reason="superseded")

    # Then the obligation is explicitly cancelled
    record = batch.get_obligation("East2")
    assert record["status"] == CANCELLED
    assert record["reason"] == "superseded"


def test_cannot_drive_a_terminal_obligation_into_another_terminal_state(backend):
    # Given a cancelled obligation
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")
    batch.cancel_obligation("East2", effective_version=batch.version)

    # When an explicit terminate arrives afterwards
    # Then it is rejected instead of overwriting the cancellation
    with pytest.raises(ObligationClosed):
        batch.terminate_obligation("East2", effective_version=batch.version)


def test_late_result_after_cancellation_is_kept_as_late_material(backend):
    # Given an obligation that was cancelled before it started
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")
    batch.cancel_obligation("East2", effective_version=batch.version)

    # When its result nonetheless comes back after the cancellation
    batch.complete_obligation("East2", range={"pages": [2]}, evidence={"late": True})

    # Then the obligation stays cancelled, the material is recorded, and
    # the old promise is not turned into new work
    record = batch.get_obligation("East2")
    assert record["status"] == CANCELLED
    assert record["late"] == [{"range": {"pages": [2]}, "evidence": {"late": True}}]


def test_late_result_after_termination_does_not_reopen_the_obligation(backend):
    # Given a terminated obligation whose worker had already started
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")
    batch.terminate_obligation("East2", effective_version=batch.version, range={"pages": [2]})

    # When that worker sends its result back after the stop
    batch.complete_obligation("East2", range={"pages": [2]}, evidence={"finished": True})

    # Then the termination stands and the result is attached as late material
    record = batch.get_obligation("East2")
    assert record["status"] == TERMINATED
    assert record["late"] == [{"range": {"pages": [2]}, "evidence": {"finished": True}}]


def test_late_submission_never_turns_discovered_branches_into_new_work(backend):
    # Given an obligation that was cancelled before its worker finished
    batch = make_batch(backend)
    batch.accept_obligation("East2", "East")
    batch.cancel_obligation("East2", effective_version=batch.version)
    keys_before = list(batch.state["obligations"])

    # When the late worker submits its page together with branches it found
    newly_accepted = batch.submit_page(
        "East2",
        range={"pages": [2]},
        evidence={"late": True},
        discovered=[{"key": "East3", "site": "East"}, {"key": "East4", "site": "East"}],
    )

    # Then no new obligations are accepted and the whole submission is late material
    assert newly_accepted == []
    assert list(batch.state["obligations"]) == keys_before
    record = batch.get_obligation("East2")
    assert record["status"] == CANCELLED
    assert record["late"] == [
        {
            "range": {"pages": [2]},
            "evidence": {"late": True},
            "discovered": [
                {"key": "East3", "site": "East"},
                {"key": "East4", "site": "East"},
            ],
        }
    ]


def test_new_site_and_its_discovered_followup_belong_to_the_open_batch(backend):
    # Given the original batch is still unfinished because East2 is pending
    batch = make_batch(backend)
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East"}],
    )
    batch.complete_obligation("West1", range={"pages": [1]})

    # When an admin registers a brand new site South1
    batch.accept_obligation("South1", "South")
    # And South1's submission discovers its own continuation South2
    newly_accepted = batch.submit_page(
        "South1",
        range={"pages": [1]},
        discovered=[{"key": "South2", "site": "South"}],
    )

    # Then both South obligations belong to this still-open batch
    assert [o["key"] for o in newly_accepted] == ["South2"]
    assert batch.get_obligation("South1")["status"] == COMPLETED
    assert batch.get_obligation("South2")["status"] == PENDING
    assert not batch.can_settle


def test_settlement_needs_explicit_evidence_for_every_obligation(backend):
    # Given every obligation has terminal evidence
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East"}],
    )
    batch.cancel_obligation("East2", effective_version=batch.version)

    # When the batch settles
    settlement = batch.settle()

    # Then it reports one settlement version with per-site completion ranges
    assert batch.is_settled
    assert settlement["version"] == batch.version
    assert [r["key"] for r in settlement["ranges"]["East"]] == ["East1", "East2"]
    assert settlement["ranges"]["East"][0]["status"] == COMPLETED
    assert settlement["ranges"]["East"][1]["status"] == CANCELLED
    assert settlement["ranges"]["West"][0]["range"] == {"pages": [1]}


def test_settlement_includes_late_material_in_the_site_ranges(backend):
    # Given a terminated obligation with a late result
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East"}],
    )
    batch.terminate_obligation("East2", effective_version=batch.version, range={"pages": [2]})
    batch.complete_obligation("East2", range={"pages": [2]}, evidence={"late": True})

    # When the batch settles
    settlement = batch.settle()

    # Then the late material is preserved on the obligation's range entry
    east2 = next(r for r in settlement["ranges"]["East"] if r["key"] == "East2")
    assert east2["status"] == TERMINATED
    assert east2["late"] == [{"range": {"pages": [2]}, "evidence": {"late": True}}]


def test_settling_twice_keeps_a_single_settlement_version(backend):
    # Given a fully resolved batch
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})
    batch.complete_obligation("East1", range={"pages": [1]})
    first = batch.settle()

    # When settlement is requested again
    second = batch.settle()

    # Then the same settlement version is returned without further bumps
    assert first["version"] == second["version"]
    assert batch.version == first["version"]


def test_try_settle_only_returns_a_settlement_when_it_performs_one(backend):
    # Given a batch with a pending obligation
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})

    # Then try_settle returns None while work remains
    assert batch.try_settle() is None
    assert not batch.is_settled

    # When the last obligation completes
    batch.complete_obligation("East1", range={"pages": [1]})
    settlement = batch.try_settle()

    # Then the first call settles and subsequent calls return None so
    # callbacks fire exactly once
    assert settlement is not None
    assert batch.try_settle() is None


def test_mutations_are_rejected_after_settlement(backend):
    # Given a settled batch
    batch = make_batch(backend)
    batch.complete_obligation("West1", range={"pages": [1]})
    batch.complete_obligation("East1", range={"pages": [1]})
    batch.settle()

    # Then new registrations and resolutions are rejected
    with pytest.raises(BatchSettled):
        batch.accept_obligation("South1", "South")
    with pytest.raises(BatchSettled):
        batch.complete_obligation("East1", range={"pages": [1, 2]})


def test_unknown_obligation_keys_are_rejected(backend):
    # Given a batch
    batch = make_batch(backend)

    # Then operating on an unknown key raises
    with pytest.raises(ObligationNotFound):
        batch.complete_obligation("nope")
    with pytest.raises(ObligationNotFound):
        batch.record_late("nope", {"range": {}})


def test_a_fresh_handle_sees_state_committed_by_another_handle(backend):
    # Given a batch opened through one handle
    writer = make_batch(backend)
    writer.complete_obligation("West1", range={"pages": [1]})

    # When a separate handle reads the same batch id
    reader = Batch("nightly", backend=backend)

    # Then it observes the committed state
    assert reader.get_obligation("West1")["status"] == COMPLETED
    assert reader.pending_obligations[0]["key"] == "East1"


def test_concurrent_completions_all_settle_under_compare_and_swap(backend):
    # Given a batch with many obligations handled by separate clients
    batch = Batch.open(
        "parallel",
        backend=backend,
        obligations=[{"key": f"P{i}", "site": "s"} for i in range(20)],
    )

    def complete(index):
        Batch("parallel", backend=backend).complete_obligation(f"P{index}", range={"i": index})

    # When they all complete concurrently
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(complete, range(20)))

    # Then every completion lands exactly once and the batch settles
    batch.reload()
    assert batch.can_settle
    settlement = batch.settle()
    assert len(settlement["ranges"]["s"]) == 20
    assert all(r["status"] == COMPLETED for r in settlement["ranges"]["s"])


def test_concurrent_discovery_registers_each_key_once(backend):
    # Given a batch with a root obligation
    Batch.open("discovery", backend=backend, obligations=[{"key": "root", "site": "s"}])

    def discover(_):
        Batch("discovery", backend=backend).accept_obligation("shared", "s")

    # When many clients register the same discovered key concurrently
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(discover, range(10)))

    # Then only one obligation is accepted
    batch = Batch("discovery", backend=backend)
    assert list(batch.state["obligations"]).count("shared") == 1


def test_settlement_callbacks_are_registered_on_the_batch(backend):
    # Given a batch with a settlement callback
    batch = Batch.open("cb", backend=backend, obligations=[{"key": "A", "site": "s"}])

    class FakeMessage:
        def asdict(self):
            return {"queue_name": "q", "actor_name": "hook", "args": (), "kwargs": {}, "options": {}}

    batch.add_settlement_callback(FakeMessage())

    # Then it is persisted with the batch and survives a reload
    reloaded = Batch("cb", backend=backend)
    assert reloaded.state["settlement_callbacks"] == [
        {"queue_name": "q", "actor_name": "hook", "args": [], "kwargs": {}, "options": {}}
    ]
