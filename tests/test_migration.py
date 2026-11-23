from __future__ import annotations

from decimal import Decimal

import pytest

import dramatiq
from dramatiq.migration import (
    InMemoryReceiptStore,
    MigrationMiddleware,
    MigrationPipeline,
    MigrationRegistry,
    PendingMigration,
    Receipt,
    Stage,
    StageStatus,
    canonical_version,
    cents_to_yuan,
    get_field,
    get_pipe_target,
    has_field,
    is_missing,
    pipe_target_payload,
    resolve_currency,
    resolve_default,
    with_defaults,
    yuan_to_cents,
)


@pytest.fixture
def parse_and_validate():
    registry = MigrationRegistry()
    registry.register_stage(
        "parse",
        versions=("v1", "v2"),
        queues={"v1": "parse-old", "v2": "parse-new"},
        actors={"v1": "parse_v1", "v2": "parse_v2"},
        successor="validate",
    )
    registry.register_stage(
        "validate",
        versions=("v1", "v2"),
        queues={"v1": "validate-old", "v2": "validate-new"},
        actors={"v1": "validate_v1", "v2": "validate_v2"},
    )
    return MigrationPipeline(registry=registry, first_stage="parse")


def register_old_workers(pipeline):
    pipeline.registry.register_worker("parse-old", ("v1",), stage="parse", worker_id="old-parse")
    pipeline.registry.register_worker("validate-old", ("v1",), stage="validate", worker_id="old-validate")


def register_new_workers(pipeline):
    pipeline.registry.register_worker("parse-new", ("v2",), stage="parse", worker_id="new-parse")
    pipeline.registry.register_worker("validate-new", ("v2",), stage="validate", worker_id="new-validate")


class TestAmounts:
    def test_yuan_amounts_convert_to_integer_cents(self):
        assert yuan_to_cents("10.01") == 1001
        assert yuan_to_cents(10.01) == 1001
        assert yuan_to_cents("0.10") == 10
        assert yuan_to_cents(0) == 0

    def test_cents_convert_back_to_yuan(self):
        assert cents_to_yuan(1001) == Decimal("10.01")
        assert str(cents_to_yuan(1001)) == "10.01"

    def test_sub_cent_amounts_round_half_up(self):
        assert yuan_to_cents("10.005") == 1001
        assert yuan_to_cents("10.004") == 1000

    def test_booleans_are_not_amounts(self):
        with pytest.raises(TypeError):
            yuan_to_cents(True)


class TestNullVersusMissing:
    def test_null_is_not_missing(self):
        payload = {"amount": 10.01, "currency": None}

        assert has_field(payload, "currency")
        assert not is_missing(get_field(payload, "currency"))
        assert get_field(payload, "currency") is None

    def test_absent_field_is_missing(self):
        payload = {"amount": 10.01}

        assert not has_field(payload, "currency")
        assert is_missing(get_field(payload, "currency"))
        assert get_field(payload, "currency", "CNY") == "CNY"

    def test_defaults_only_fill_absent_fields(self):
        payload = {"currency": None}

        assert with_defaults(payload, {"currency": "CNY"}) == {"currency": None}
        assert with_defaults(payload, {"currency": "CNY", "scale": 2}) == {"currency": None, "scale": 2}

    def test_run_default_is_used_only_for_absent_fields(self):
        payload = {"amount": 10.01}
        defaults = {"currency": "EUR"}

        assert resolve_currency(payload, defaults=defaults) == "EUR"
        assert resolve_currency({"currency": None}, defaults=defaults) is None
        assert resolve_currency({"currency": "USD"}, defaults=defaults) == "USD"
        assert resolve_default(payload, "currency", defaults=defaults, default="CNY") == "EUR"
        assert resolve_default({}, "currency", default="CNY") == "CNY"
        assert is_missing(resolve_default({}, "currency"))


class TestLegacyPipeTarget:
    def test_pipe_target_can_be_read_without_running_the_old_chain(self):
        @dramatiq.actor(queue_name="parse-old", actor_name="legacy_parse")
        def legacy_parse(amount):
            pass

        @dramatiq.actor(queue_name="validate-old", actor_name="legacy_validate")
        def legacy_validate(payload):
            pass

        message = legacy_parse.message(10.01) | legacy_validate.message({"amount": 10.01, "currency": None})
        first = message.messages[0]

        target = get_pipe_target(first)
        assert target is not None
        assert target["actor_name"] == "legacy_validate"
        assert target["queue_name"] == "validate-old"
        assert pipe_target_payload(first) == ({"amount": 10.01, "currency": None},)

    def test_pipe_target_helper_reports_a_missing_target(self):
        @dramatiq.actor(actor_name="parse_v1_alone")
        def parse_v1_alone(amount):
            pass

        message = parse_v1_alone.message(10.01)

        assert get_pipe_target(message) is None
        assert is_missing(pipe_target_payload(message))


class TestRegistryValidation:
    def test_worker_versions_are_required(self):
        registry = MigrationRegistry()

        with pytest.raises(TypeError):
            registry.register_worker("parse-old")

    def test_unknown_stage_lookups_raise(self):
        registry = MigrationRegistry()

        with pytest.raises(KeyError):
            registry.stage("missing")
        assert registry.get_stage("missing") is None

    def test_workers_can_be_unregistered(self):
        registry = MigrationRegistry()
        worker = registry.register_worker("parse-old", ("v1",))
        registry.register_worker("parse-new", ("v2",))

        assert registry.unregister_worker(worker) is True
        assert registry.workers_for("parse") == (registry.workers[0],)
        assert registry.unregister_worker(worker) is False

    def test_duplicate_versions_collapse_in_declaration_order(self):
        stage = Stage("parse", versions=("v2", "v1", "v2"))

        assert stage.versions == ("v2", "v1")
        assert stage.newest_version() == "v1"


class TestRegistrationAndRouting:
    def test_stage_versions_are_canonicalized(self):
        stage = Stage("parse", versions=(1, "v2", 2))

        assert stage.versions == ("v1", "v2")
        assert canonical_version(1) == canonical_version("1") == canonical_version("v1")

    def test_workers_register_queue_and_supported_versions(self):
        registry = MigrationRegistry()
        registry.register_stage(
            "parse",
            versions=("v1", "v2"),
            queues={"v1": "parse-old", "v2": "parse-new"},
        )
        old_worker = registry.register_worker("parse-old", ("v1",))
        new_worker = registry.register_worker("parse-new", supported_versions=("v1", "v2"))

        assert old_worker.supported_versions == frozenset({"v1"})
        assert new_worker.supported_versions == frozenset({"v1", "v2"})

        route = registry.route("parse", version="v2")
        assert route.worker_id == new_worker.worker_id
        assert route.queue_name == "parse-new"

    def test_routing_prefers_the_newest_compatible_version(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        register_new_workers(parse_and_validate)

        route = parse_and_validate.registry.route("parse")

        assert route.version == "v2"
        assert route.queue_name == "parse-new"

    def test_unregistered_workers_do_not_accept_stage_versions(self, parse_and_validate):
        parse_and_validate.registry.register_worker("parse-old", ("v1",), stage="parse")

        assert parse_and_validate.registry.route("parse", version="v2") is None
        assert parse_and_validate.registry.route("parse", version="v1") is not None

    def test_old_implementation_does_not_accept_new_payloads(self):
        stage = Stage("validate", versions=("v1", "v2"))

        assert stage.accepts_payload("v1", "v1")
        assert stage.accepts_payload("v2", "v1")
        assert stage.accepts_payload("v2", "v2")
        assert not stage.accepts_payload("v1", "v2")

    def test_compatibility_can_be_narrowed_explicitly(self):
        stage = Stage(
            "validate",
            versions=("v1", "v2"),
            accepts={"v2": ("v1",)},
        )

        assert stage.accepts_payload("v2", "v1")
        assert not stage.accepts_payload("v2", "v2")


class TestCallStyles:
    def test_register_stage_accepts_a_version_to_queue_mapping(self):
        registry = MigrationRegistry()
        stage = registry.register_stage("parse", versions={"v1": "parse-old", "v2": "parse-new"})

        assert stage.versions == ("v1", "v2")
        assert stage.queues == {"v1": "parse-old", "v2": "parse-new"}

    def test_register_workers_accepts_a_queue_mapping(self):
        registry = MigrationRegistry()
        workers = registry.register_workers({"parse-old": ("v1",), "validate-old": ("v1",)}, stage="parse")

        assert {worker.queue_name for worker in workers} == {"parse-old", "validate-old"}
        assert all(worker.stage == "parse" for worker in workers)

    def test_submit_supports_keyword_and_positional_forms(self, parse_and_validate):
        register_new_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})

        keyword = parse_and_validate.submit(
            result={"amount_cents": 1001},
            run_id="R2",
            stage="parse",
            version="v2",
        )
        assert keyword.committed
        assert keyword.dispatch.queue_name == "validate-new"

    def test_submit_supports_the_positional_form(self, parse_and_validate):
        register_new_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R3", defaults={"currency": "CNY"})

        positional = parse_and_validate.submit("R3", "parse", "v2", {"amount_cents": 1001})

        assert positional.committed
        assert positional.receipt.payload == {"amount_cents": 1001}


class TestConditionalReceipts:
    def test_receipts_are_keyed_by_run_and_stage(self):
        store = InMemoryReceiptStore()

        first = store.commit(Receipt(run_id="R1", stage="parse", version="v1", payload={"amount": 10.01}))
        second = store.commit(Receipt(run_id="R2", stage="parse", version="v1", payload={"amount": 20.02}))

        assert first.committed and second.committed
        assert store.get("R1", "parse").payload == {"amount": 10.01}
        assert store.get("R2", "parse").payload == {"amount": 20.02}

    def test_newer_versions_replace_older_receipts(self):
        store = InMemoryReceiptStore()
        store.commit(Receipt(run_id="R1", stage="parse", version="v1", payload=10.01, rank=0))

        result = store.commit(Receipt(run_id="R1", stage="parse", version="v2", payload=1001, rank=1))

        assert result.committed
        assert result.receipt.payload == 1001
        assert result.receipt.sequence == 2
        assert store.get("R1", "parse").version == "v2"

    def test_older_versions_cannot_overwrite_newer_receipts(self):
        store = InMemoryReceiptStore()
        store.commit(Receipt(run_id="R2", stage="parse", version="v2", payload=1001, rank=1))

        result = store.commit(Receipt(run_id="R2", stage="parse", version="v1", payload=10.01, rank=0))

        assert not result.committed
        assert result.reason == "stale_version"
        assert result.receipt.payload == 1001

    def test_duplicate_commits_are_idempotent(self):
        store = InMemoryReceiptStore()
        store.commit(Receipt(run_id="R1", stage="parse", version="v2", payload=1001, rank=1))

        result = store.commit(Receipt(run_id="R1", stage="parse", version="v2", payload=1001, rank=1))

        assert not result.committed
        assert result.duplicate
        assert result.receipt.sequence == 1

    def test_expected_sequence_guards_against_lost_updates(self):
        store = InMemoryReceiptStore()
        first = store.commit(Receipt(run_id="R1", stage="parse", version="v1", payload=10.01, rank=0))
        store.commit(Receipt(run_id="R1", stage="parse", version="v2", payload=1001, rank=1))

        result = store.commit(
            Receipt(run_id="R1", stage="parse", version="v2", payload=2002, rank=1),
            expected=first.receipt,
        )

        assert not result.committed
        assert result.reason == "precondition_failed"

    def test_force_allows_an_explicit_override(self):
        store = InMemoryReceiptStore()
        store.commit(Receipt(run_id="R1", stage="parse", version="v2", payload=1001, rank=1))

        result = store.commit(
            Receipt(run_id="R1", stage="parse", version="v1", payload=10.01, rank=0),
            force=True,
        )

        assert result.committed
        assert result.receipt.version == "v1"


class TestR1OldParseThenNewValidate:
    def test_r1_reuses_the_old_parse_result_including_null_fields(self, parse_and_validate):
        register_old_workers(parse_and_validate)

        old_parse_result = {"amount": 10.01, "currency": None, "note": None}
        run = parse_and_validate.start(10.01, run_id="R1", defaults={"currency": "CNY"})
        assert run.last_submission.dispatch.queue_name == "parse-old"

        adopted = parse_and_validate.adopt("R1", "parse", "v1", old_parse_result)
        assert adopted.committed
        assert adopted.dispatch.queue_name == "validate-old"

        register_new_workers(parse_and_validate)
        switched = parse_and_validate.switch_stage("R1", "validate", version="v2")

        assert switched.dispatch.queue_name == "validate-new"
        assert switched.dispatch.version == "v2"
        assert switched.dispatch.payload == old_parse_result
        assert has_field(switched.dispatch.payload, "currency")
        assert get_field(switched.dispatch.payload, "currency") is None

    def test_r1_receipts_are_preserved_when_rerouting(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R1")
        parse_and_validate.adopt("R1", "parse", "v1", {"amount": 10.01, "currency": None})

        register_new_workers(parse_and_validate)
        parse_and_validate.switch_stage("R1", "validate", version="v2")

        receipt = parse_and_validate.get_receipt("R1", "parse")
        assert receipt.version == "v1"
        assert receipt.payload["currency"] is None
        assert parse_and_validate.get_receipt("R1", "validate") is None


class TestR2NewVersionAndLateOldResult:
    def test_r2_reroutes_a_waiting_old_parse_to_a_new_worker(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        run = parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        assert run.last_submission.dispatch.queue_name == "parse-old"

        register_new_workers(parse_and_validate)
        switched = parse_and_validate.switch_stage("R2", "parse", version="v2")

        assert switched.dispatch.queue_name == "parse-new"
        assert switched.dispatch.version == "v2"
        assert switched.dispatch.payload == 10.01

    def test_r2_new_parse_produces_cents_and_uses_the_run_default_currency(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        register_new_workers(parse_and_validate)
        parse_and_validate.switch_stage("R2", "parse", version="v2")

        incoming = {"amount": 10.01}
        new_result = {
            "amount_cents": yuan_to_cents(get_field(incoming, "amount")),
            "currency": resolve_currency(incoming, defaults={"currency": "CNY"}),
        }

        submission = parse_and_validate.submit_result("R2", "parse", "v2", new_result, worker_id="new-parse")

        assert submission.committed
        assert submission.receipt.payload == {"amount_cents": 1001, "currency": "CNY"}
        assert submission.dispatch.queue_name == "validate-new"

    def test_late_old_result_cannot_overwrite_the_new_result(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        register_new_workers(parse_and_validate)
        parse_and_validate.switch_stage("R2", "parse", version="v2")
        parse_and_validate.submit_result(
            "R2",
            "parse",
            "v2",
            {"amount_cents": 1001, "currency": "CNY"},
            worker_id="new-parse",
        )

        late = parse_and_validate.submit_result(
            "R2",
            "parse",
            "v1",
            {"amount": 10.01, "currency": None},
            worker_id="old-parse",
        )

        assert not late.committed
        assert late.stale
        assert late.dispatch is None
        assert late.receipt.version == "v2"
        assert late.receipt.payload == {"amount_cents": 1001, "currency": "CNY"}

    def test_late_old_result_is_stale_even_before_the_new_version_commits(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        register_new_workers(parse_and_validate)
        parse_and_validate.switch_stage("R2", "parse", version="v2")

        late = parse_and_validate.submit_result(
            "R2",
            "parse",
            "v1",
            {"amount": 10.01, "currency": None},
            worker_id="old-parse",
        )

        assert not late.committed
        assert late.stale
        assert late.dispatch is None
        assert parse_and_validate.get_receipt("R2", "parse") is None

    def test_late_old_result_is_not_sent_to_the_old_validate_queue(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        register_new_workers(parse_and_validate)
        parse_and_validate.switch_stage("R2", "parse", version="v2")
        parse_and_validate.submit_result(
            "R2",
            "parse",
            "v2",
            {"amount_cents": 1001, "currency": "CNY"},
        )

        late = parse_and_validate.submit_result(
            "R2",
            "parse",
            "v1",
            {"amount": 10.01, "currency": None},
        )

        assert late.dispatch is None
        validate_state = parse_and_validate.get_run("R2").stage("validate")
        assert validate_state.queue_name != "validate-old"

    def test_legacy_pipe_target_is_not_followed_for_migration_messages(self, stub_broker, stub_worker):
        validated = []

        @dramatiq.actor(queue_name="parse-old", actor_name="late_parse_v1")
        def late_parse_v1(amount):
            return {"amount": amount, "currency": None}

        @dramatiq.actor(queue_name="validate-old", actor_name="stale_validate_v1")
        def stale_validate_v1(payload):
            validated.append(payload)

        message = late_parse_v1.message(10.01).copy(
            options={
                "migration_run_id": "R2",
                "migration_stage": "parse",
                "migration_version": "v1",
                "pipe_target": stale_validate_v1.message().asdict(),
            }
        )
        stub_broker.enqueue(message)

        stub_broker.join("parse-old")
        stub_broker.join("validate-old")
        stub_worker.join()

        assert validated == []


class TestPendingMigration:
    def test_missing_successor_is_reported_as_pending_migration(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R1", defaults={"currency": "CNY"})

        submission = parse_and_validate.adopt("R1", "parse", "v1", {"amount": 10.01, "currency": None})

        assert submission.committed
        assert submission.dispatch.queue_name == "validate-old"

        # The old validate worker goes away before it can take the message.
        parse_and_validate.registry.unregister_worker("old-validate")
        parse_and_validate.registry.unregister_worker("old-parse")

        parse_and_validate.switch_stage("R1", "validate", version="v2")

        # No v2 validate worker exists yet.
        pending = parse_and_validate.resume("R1")
        assert pending is not None
        assert pending.pending is not None
        assert pending.pending.stage == "validate"

        state = parse_and_validate.get_run("R1").stage("validate")
        assert state.status == StageStatus.PENDING_MIGRATION
        assert state.pending_migration
        assert parse_and_validate.pending_stages("R1") == ["validate"]
        assert parse_and_validate.get_run("R1").pending_stage == "validate"
        assert "validate: pending migration" in parse_and_validate.describe("R1")
        with pytest.raises(PendingMigration):
            pending.raise_if_pending()

        # The old parse result is still available for a later continuation.
        assert parse_and_validate.get_receipt("R1", "parse").payload["currency"] is None
        assert parse_and_validate.get_run("R1").stage("parse").status == StageStatus.SUCCEEDED

    def test_a_late_result_is_stale_while_the_new_route_is_pending(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R1")
        parse_and_validate.adopt("R1", "parse", "v1", {"amount": 10.01, "currency": None})
        parse_and_validate.registry.unregister_worker("old-validate")
        parse_and_validate.registry.unregister_worker("old-parse")

        pending = parse_and_validate.switch_stage("R1", "validate", version="v2")
        assert pending.pending is not None

        late = parse_and_validate.submit_result(
            "R1",
            "validate",
            "v1",
            {"amount": 10.01, "currency": None},
        )

        assert not late.committed
        assert late.stale
        assert parse_and_validate.get_receipt("R1", "validate") is None

    def test_pending_migration_can_be_resumed_after_a_compatible_worker_registers(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R1")
        parse_and_validate.adopt("R1", "parse", "v1", {"amount": 10.01, "currency": None})
        parse_and_validate.registry.unregister_worker("old-validate")
        parse_and_validate.registry.unregister_worker("old-parse")

        submission = parse_and_validate.switch_stage("R1", "validate", version="v2")
        assert submission.pending is not None

        parse_and_validate.registry.register_worker("validate-new", ("v2",), stage="validate")
        resumed = parse_and_validate.resume("R1")

        assert resumed.dispatch is not None
        assert resumed.dispatch.queue_name == "validate-new"
        assert resumed.dispatch.payload == {"amount": 10.01, "currency": None}
        assert parse_and_validate.get_run("R1").pending is None

    def test_resume_does_not_duplicate_a_queued_stage(self, parse_and_validate):
        register_new_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R5")
        queued = parse_and_validate.get_run("R5").last_submission

        assert parse_and_validate.resume("R5") is queued
        assert parse_and_validate.resume("R5", stage="parse") is queued

    def test_pending_stages_can_be_parked_and_completed_explicitly(self, parse_and_validate):
        register_new_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R4", defaults={"currency": "CNY"})
        parse_and_validate.submit_result("R4", "parse", "v2", {"amount_cents": 1001, "currency": "CNY"})

        parked = parse_and_validate.mark_pending("R4", "validate", reason="awaiting bilingual validator")
        assert parked.pending is not None
        assert parse_and_validate.get_run("R4").pending_stage == "validate"
        assert parse_and_validate.receipt("R4", "parse").payload == {"amount_cents": 1001, "currency": "CNY"}
        assert set(parse_and_validate.receipts_for("R4")) == {"parse"}

        completed = parse_and_validate.complete_pending("R4", "validate", "v2", {"ok": True})
        assert completed.committed
        assert parse_and_validate.is_stage_done("R4", "validate")
        assert parse_and_validate.get_run("R4").status.value == "completed"

    def test_pending_migration_preserves_the_upstream_receipts(self, parse_and_validate):
        register_old_workers(parse_and_validate)
        parse_and_validate.start(10.01, run_id="R1")
        old_payload = {"amount": 10.01, "currency": None, "note": None}
        parse_and_validate.adopt("R1", "parse", "v1", old_payload)
        before = parse_and_validate.get_receipt("R1", "parse")

        parse_and_validate.registry.unregister_worker("old-validate")
        parse_and_validate.switch_stage("R1", "validate", version="v2")

        after = parse_and_validate.get_receipt("R1", "parse")
        assert after.sequence == before.sequence
        assert after.payload == old_payload


class TestMiddlewareIntegration:
    def test_middleware_commits_results_and_routes_the_successor(self, stub_broker):
        stub_broker.declare_queue("parse-new")
        stub_broker.declare_queue("validate-new")
        registry = MigrationRegistry()
        registry.register_stage("parse", versions=("v2",), queues={"v2": "parse-new"}, actors={"v2": "parse_v2"})
        registry.register_stage(
            "validate",
            versions=("v2",),
            queues={"v2": "validate-new"},
            actors={"v2": "validate_v2"},
        )
        pipeline = MigrationPipeline(registry=registry, first_stage="parse")
        registry.register_worker("parse-new", ("v2",), stage="parse", worker_id="new-parse")
        registry.register_worker("validate-new", ("v2",), stage="validate", worker_id="new-validate")
        pipeline.start(10.01, run_id="R2", defaults={"currency": "CNY"})

        middleware = MigrationMiddleware(pipeline, worker_id="new-parse")
        message = pipeline.build_message(pipeline.get_run("R2").last_submission.dispatch)
        assert message.queue_name == "parse-new"
        assert message.actor_name == "parse_v2"

        middleware.after_process_message(stub_broker, message, result={"amount_cents": 1001, "currency": "CNY"})

        assert pipeline.get_receipt("R2", "parse").payload == {"amount_cents": 1001, "currency": "CNY"}
        validate_message = stub_broker.queues["validate-new"].get_nowait()
        decoded = dramatiq.Message.decode(validate_message)
        assert decoded.actor_name == "validate_v2"
        assert decoded.args == ({"amount_cents": 1001, "currency": "CNY"},)

    def test_middleware_registers_each_queue_with_its_own_versions(self, stub_broker, stub_worker):
        registry = MigrationRegistry()
        registry.register_stage("parse", versions=("v1", "v2"), queues={"v1": "parse-old", "v2": "parse-new"})
        registry.register_stage("validate", versions=("v1",), queues={"v1": "validate-old"})
        pipeline = MigrationPipeline(registry=registry, first_stage="parse")
        stub_broker.declare_queue("parse-old")
        stub_broker.declare_queue("validate-old")

        middleware = MigrationMiddleware(
            pipeline,
            worker_id="mixed",
            registrations={"parse-old": ("v1",), "validate-old": ("v1",)},
        )
        stub_broker.add_middleware(middleware)
        middleware.after_worker_boot(stub_broker, stub_worker)

        workers = {worker.queue_name: worker for worker in registry.workers}
        assert workers["parse-old"].stage == "parse"
        assert workers["validate-old"].stage == "validate"
        assert workers["parse-old"].worker_id != workers["validate-old"].worker_id

        run = pipeline.start(10.01, run_id="R1")
        assert run.last_submission.dispatch.queue_name == "parse-old"

    def test_middleware_leaves_non_migration_messages_alone(self, stub_broker):
        registry = MigrationRegistry()
        pipeline = MigrationPipeline(registry=registry)
        middleware = MigrationMiddleware(pipeline, worker_id="plain")

        @dramatiq.actor
        def do_nothing():
            pass

        message = do_nothing.message()
        middleware.after_process_message(stub_broker, message, result=42)

        assert pipeline.runs == {}


class TestEndToEnd:
    def test_a_new_version_run_flows_through_real_workers(self, stub_broker, stub_worker):
        stub_broker.declare_queue("parse-new")
        stub_broker.declare_queue("validate-new")

        registry = MigrationRegistry()
        registry.register_stage(
            "parse",
            versions=("v2",),
            queues={"v2": "parse-new"},
            actors={"v2": "parse_v2"},
            successor="validate",
        )
        registry.register_stage(
            "validate",
            versions=("v2",),
            queues={"v2": "validate-new"},
            actors={"v2": "validate_v2"},
        )
        pipeline = MigrationPipeline(registry=registry, first_stage="parse")
        middleware = MigrationMiddleware(
            pipeline,
            worker_id="new-worker",
            registrations={"parse-new": ("v2",), "validate-new": ("v2",)},
        )
        stub_broker.add_middleware(middleware)
        middleware.after_worker_boot(stub_broker, stub_worker)
        validated = []

        @dramatiq.actor(queue_name="parse-new", actor_name="parse_v2")
        def parse_v2(amount):
            return {
                "amount_cents": yuan_to_cents(amount),
                "currency": resolve_default({"amount": amount}, "currency", defaults={"currency": "CNY"}),
            }

        @dramatiq.actor(queue_name="validate-new", actor_name="validate_v2")
        def validate_v2(payload):
            validated.append(payload)
            return payload

        run = pipeline.start(10.01, run_id="R2", defaults={"currency": "CNY"})
        stub_broker.enqueue(pipeline.build_message(run.last_submission.dispatch))

        stub_broker.join("parse-new")
        stub_broker.join("validate-new")
        stub_worker.join()

        assert validated == [{"amount_cents": 1001, "currency": "CNY"}]
        assert pipeline.get_receipt("R2", "parse").payload == {"amount_cents": 1001, "currency": "CNY"}
        assert pipeline.get_receipt("R2", "validate").payload == {"amount_cents": 1001, "currency": "CNY"}
        assert pipeline.get_run("R2").status.value == "completed"


class TestLegacyPipelinesUnchanged:
    def test_unregistered_pipelines_still_use_pipe_target(self, stub_broker, stub_worker):
        @dramatiq.actor(actor_name="legacy_add")
        def legacy_add(x, y):
            return x + y

        @dramatiq.actor(actor_name="legacy_collect")
        def legacy_collect(value):
            collected.append(value)

        collected = []
        pipe = legacy_add.message(1, 2) | legacy_collect.message()
        pipe.run()

        stub_broker.join(legacy_add.queue_name)
        stub_broker.join(legacy_collect.queue_name)
        stub_worker.join()

        assert collected == [3]

    def test_unregistered_pipeline_options_are_untouched(self, stub_broker):
        @dramatiq.actor
        def add(x, y):
            return x + y

        pipe = add.message(1, 2) | add.message(3)

        assert pipe.messages[0].options["pipe_target"] == pipe.messages[1].asdict()
