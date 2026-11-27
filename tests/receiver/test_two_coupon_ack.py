"""Tests for the two-coupon (when_saved_2pc) acknowledgement flow."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from taskiq_dependencies import Depends

from taskiq import (
    ADMISSION_ID_LABEL,
    ATTACHMENT_LABEL,
    AckableMessage,
    InMemoryAdmissionStorage,
    InMemoryBroker,
    TaskiqDepends,
    TaskiqMessage,
)
from taskiq.abc.middleware import TaskiqMiddleware
from taskiq.abc.result_backend import AsyncResultBackend
from taskiq.acks import AcknowledgeType
from taskiq.context import Context
from taskiq.exceptions import NoResultError
from taskiq.message import BrokerMessage
from taskiq.receiver import Receiver
from taskiq.result import TaskiqResult
from taskiq.storage import AdmissionPlan


class _CapturingBroker(InMemoryBroker):
    """InMemoryBroker that keeps kicked messages instead of auto-running them."""

    def __init__(self) -> None:
        super().__init__()
        self.kicked: list[bytes] = []

    async def kick(self, message: BrokerMessage) -> None:
        self.kicked.append(message.message)


class _RecordingResultBackend(AsyncResultBackend[Any]):
    def __init__(self) -> None:
        self.results: dict[str, TaskiqResult[Any]] = {}
        self.fail_keys: set[str] = set()
        self.calls = 0

    async def set_result(self, task_id: str, result: TaskiqResult[Any]) -> None:
        self.calls += 1
        if task_id in self.fail_keys:
            self.fail_keys.discard(task_id)
            raise RuntimeError("backend unavailable")
        self.results[task_id] = result

    async def is_result_ready(self, task_id: str) -> bool:
        return task_id in self.results

    async def get_result(
        self,
        task_id: str,
        with_logs: bool = False,
    ) -> TaskiqResult[Any]:
        return self.results[task_id]


class _AttachmentMiddleware(TaskiqMiddleware):
    def post_save(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        message.labels[ATTACHMENT_LABEL] = "attachment-bytes"


class _FailingPostSaveMiddleware(TaskiqMiddleware):
    def post_save(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        raise RuntimeError("post_save exploded")


class _CrashOnAdmissionStorage(InMemoryAdmissionStorage):
    """Storage that simulates a process stop while writing the admission coupon."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_admission = False

    async def save_admission(self, admission_id: str, entry: Any) -> None:
        if self.fail_next_admission:
            self.fail_next_admission = False
            raise RuntimeError("process stopped during admission coupon write")
        await super().save_admission(admission_id, entry)


def _receiver(broker: InMemoryBroker) -> Receiver:
    return Receiver(broker, executor=ThreadPoolExecutor(max_workers=4))


def _message(
    broker: InMemoryBroker,
    task: Any,
    admission_id: str,
    task_id: str,
    *,
    ack_type: str = AcknowledgeType.WHEN_SAVED_2PC.value,
) -> bytes:
    labels: dict[str, Any] = {}
    if ack_type is not None:
        labels["ack_type"] = ack_type
    if admission_id is not None:
        labels[ADMISSION_ID_LABEL] = admission_id
    return broker.formatter.dumps(
        TaskiqMessage(
            task_id=task_id,
            task_name=task.task_name,
            labels=labels,
            args=[],
            kwargs={},
        ),
    ).message


def _delivery(data: bytes, delivery_id: str, acks: list[str]) -> AckableMessage:
    return AckableMessage(
        data=data,
        ack=lambda: acks.append(delivery_id),
        delivery_id=delivery_id,
    )


def _broker(
    backend: _RecordingResultBackend | None = None,
    storage: InMemoryAdmissionStorage | None = None,
    *middlewares: TaskiqMiddleware,
) -> _CapturingBroker:
    broker = _CapturingBroker()
    if backend is not None:
        broker.with_result_backend(backend)
    broker.with_admission_storage(storage or InMemoryAdmissionStorage())
    if middlewares:
        broker.with_middlewares(*middlewares)
    return broker


async def test_happy_path_acks_only_after_both_coupons() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    acks: list[str] = []

    @broker.task
    async def my_task() -> int:
        return 42

    receiver = _receiver(broker)
    data = _message(broker, my_task, "adm-1", "task-1")
    await receiver.callback(_delivery(data, "del-1", acks))

    assert acks == ["del-1"]
    assert backend.results["task-1"].return_value == 42
    result_coupon = await storage.get_result("adm-1")
    assert result_coupon is not None and result_coupon.return_value == 42
    entry = await storage.get_admission("adm-1")
    assert entry is not None and entry.task_id == "task-1"
    plan = await storage.get_plan("adm-1")
    assert plan is not None and plan.result is not None


async def test_admission_id_and_task_id_are_not_interchangeable() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)

    @broker.task
    async def my_task() -> str:
        return "ok"

    receiver = _receiver(broker)
    # Same task id, two unrelated admission ids: both admissions must finish.
    await receiver.callback(
        _delivery(_message(broker, my_task, "adm-a", "shared-task"), "d1", []),
    )
    acks_b: list[str] = []
    await receiver.callback(
        _delivery(_message(broker, my_task, "adm-b", "shared-task"), "d2", acks_b),
    )

    assert acks_b == ["d2"]
    assert await storage.get_result("adm-a") is not None
    assert await storage.get_result("adm-b") is not None
    assert await storage.get_admission("adm-a") is not None
    assert await storage.get_admission("adm-b") is not None
    # The result backend stays keyed by task id, never by admission id.
    assert "shared-task" in backend.results
    assert "adm-a" not in backend.results


async def test_set_result_failure_keeps_delivery_unacknowledged() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0
    acks: list[str] = []

    @broker.task
    async def flaky_task() -> int:
        nonlocal runs
        runs += 1
        return 7

    receiver = _receiver(broker)
    data = _message(broker, flaky_task, "adm-2", "task-2")
    backend.fail_keys.add("task-2")
    await receiver.callback(_delivery(data, "del-1", acks))

    assert runs == 1
    assert acks == []
    assert await storage.get_result("adm-2") is None
    assert await storage.get_admission("adm-2") is None
    # The computed result survives in the plan, so redelivery need not re-run.
    plan = await storage.get_plan("adm-2")
    assert plan is not None and plan.result is not None


async def test_redelivery_completes_coupons_without_rerunning_task() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0
    acks: list[str] = []

    @broker.task
    async def flaky_task() -> int:
        nonlocal runs
        runs += 1
        return 7

    receiver = _receiver(broker)
    data = _message(broker, flaky_task, "adm-3", "task-3")
    backend.fail_keys.add("task-3")
    await receiver.callback(_delivery(data, "del-1", acks))
    # The broker redelivers the same bytes with a fresh ack handle.
    await receiver.callback(_delivery(data, "del-2", acks))

    assert runs == 1
    assert acks == ["del-2"]
    assert backend.results["task-3"].return_value == 7
    assert (await storage.get_result("adm-3")).return_value == 7
    assert await storage.get_admission("adm-3") is not None


async def test_redelivery_after_both_coupons_only_acks_new_delivery() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0
    acks: list[str] = []

    @broker.task
    async def my_task() -> int:
        nonlocal runs
        runs += 1
        return 1

    receiver = _receiver(broker)
    data = _message(broker, my_task, "adm-4", "task-4")
    await receiver.callback(_delivery(data, "del-1", acks))
    await receiver.callback(_delivery(data, "del-2", acks))

    assert runs == 1
    assert acks == ["del-1", "del-2"]


async def test_early_context_ack_is_deferred_until_coupons_exist() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage)
    acks: list[str] = []

    @broker.task
    async def early_task(context: Context = TaskiqDepends()) -> str:
        await context.ack()
        assert not context.is_acked
        return "done"

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, early_task, "adm-5", "task-5"), "del-1", acks),
    )

    assert acks == ["del-1"]
    plan = await storage.get_plan("adm-5")
    assert plan is not None and plan.early_ack is True
    assert await storage.get_result("adm-5") is not None
    assert await storage.get_admission("adm-5") is not None


async def test_requeue_waits_for_next_round_credential() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage)
    acks: list[str] = []
    runs = 0

    @broker.task
    async def requeue_task(context: Context = Depends()) -> str:
        nonlocal runs
        runs += 1
        if runs == 1:
            await context.requeue()
        return "second-round"

    receiver = _receiver(broker)
    first = _message(broker, requeue_task, "adm-6", "task-6")
    await receiver.callback(_delivery(first, "del-1", acks))

    assert runs == 1
    assert acks == []
    assert await storage.get_result("adm-6") is None
    continuation = await storage.get_continuation("adm-6")
    assert continuation is not None and continuation.requeue_count == 1
    assert len(broker.kicked) == 1

    # A redelivery of the first-round message still cannot settle anything.
    await receiver.callback(_delivery(first, "del-1b", acks))
    assert runs == 1
    assert acks == []

    # The next-round message carries its own delivery credential.
    await receiver.callback(_delivery(broker.kicked[0], "del-2", acks))
    assert runs == 2
    assert acks == ["del-2"]
    assert (await storage.get_result("adm-6")).return_value == "second-round"
    entry = await storage.get_admission("adm-6")
    assert entry is not None and entry.continuation is not None
    assert (await storage.get_continuation("adm-6")).delivered_delivery_id == "del-2"


async def test_requeue_does_not_fabricate_a_result() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(_RecordingResultBackend(), storage)

    @broker.task
    async def requeue_task(context: Context = TaskiqDepends()) -> None:
        await context.requeue()

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, requeue_task, "adm-7", "task-7"), "del-1", []),
    )

    assert await storage.get_result("adm-7") is None
    assert "task-7" not in broker.result_backend.results


async def test_plain_no_result_error_is_still_discarded() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage)
    acks: list[str] = []

    @broker.task
    async def skip_task() -> None:
        raise NoResultError

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, skip_task, "adm-8", "task-8"), "del-1", acks),
    )

    assert acks == ["del-1"]
    assert await storage.get_result("adm-8") is None
    assert await storage.get_admission("adm-8") is None
    assert await storage.get_continuation("adm-8") is None


async def test_normal_failure_is_saved_as_error_result() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    acks: list[str] = []

    @broker.task
    async def broken_task() -> None:
        raise ValueError("nope")

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, broken_task, "adm-9", "task-9"), "del-1", acks),
    )

    assert acks == ["del-1"]
    saved = backend.results["task-9"]
    assert saved.is_err and isinstance(saved.error, ValueError)


async def test_post_save_attachment_lands_on_admission_coupon() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage).with_middlewares(_AttachmentMiddleware())

    @broker.task
    async def my_task() -> int:
        return 1

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, my_task, "adm-10", "task-10"), "del-1", []),
    )

    entry = await storage.get_admission("adm-10")
    assert entry is not None and entry.attachment == b"attachment-bytes"


async def test_failing_post_save_does_not_block_coupons() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage).with_middlewares(_FailingPostSaveMiddleware())
    acks: list[str] = []

    @broker.task
    async def my_task() -> int:
        return 1

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, my_task, "adm-11", "task-11"), "del-1", acks),
    )

    assert acks == ["del-1"]
    assert await storage.get_result("adm-11") is not None
    assert await storage.get_admission("adm-11") is not None


async def test_missing_storage_raises_clear_error() -> None:
    broker = InMemoryBroker()

    @broker.task
    async def my_task() -> None:
        return None

    receiver = _receiver(broker)
    with pytest.raises(RuntimeError, match="admission storage"):
        await receiver.callback(
            _delivery(_message(broker, my_task, "adm-12", "task-12"), "del-1", []),
        )


async def test_concurrent_deliveries_keep_distinct_ack_handles() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0
    acks: list[str] = []
    started = asyncio.Event()

    @broker.task
    async def slow_task() -> int:
        nonlocal runs
        runs += 1
        started.set()
        await asyncio.sleep(0.05)
        return 3

    receiver = _receiver(broker)
    data = _message(broker, slow_task, "adm-13", "task-13")
    backend.fail_keys.add("task-13")
    first = asyncio.create_task(
        receiver.callback(_delivery(data, "del-1", acks)),
    )
    await started.wait()
    second = asyncio.create_task(
        receiver.callback(_delivery(data, "del-2", acks)),
    )
    await asyncio.gather(first, second)

    assert runs == 1
    assert acks == ["del-2"]


async def test_storage_writes_are_idempotent_per_key() -> None:
    storage = InMemoryAdmissionStorage()
    plan = AdmissionPlan(
        task_id="t",
        task_name="name",
        labels={},
        args=[],
        kwargs={},
    )
    await storage.save_plan("adm", plan)
    plan.early_ack = True
    await storage.save_plan("adm", plan)

    fetched = await storage.get_plan("adm")
    assert fetched is not None and fetched.early_ack is True


async def test_legacy_ack_timing_is_unchanged_without_opt_in() -> None:
    events: list[str] = []
    backend = _RecordingResultBackend()
    broker = _CapturingBroker().with_result_backend(backend)

    @broker.task
    async def my_task() -> int:
        events.append("task")
        return 1

    receiver = Receiver(
        broker,
        executor=ThreadPoolExecutor(max_workers=2),
        ack_type=AcknowledgeType.WHEN_RECEIVED,
    )
    data = _message(
        broker,
        my_task,
        admission_id=None,
        task_id="legacy-1",
        ack_type=AcknowledgeType.WHEN_RECEIVED.value,
    )
    await receiver.callback(_delivery(data, "del-1", events))

    assert events[0] == "del-1"
    assert "task" in events


async def test_recovery_finishes_missing_admission_coupon_in_new_receiver() -> None:
    backend = _RecordingResultBackend()
    storage = _CrashOnAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0

    @broker.task
    async def my_task() -> int:
        nonlocal runs
        runs += 1
        return 9

    data = _message(broker, my_task, "adm-r1", "task-r1")
    first_receiver = _receiver(broker)
    storage.fail_next_admission = True
    await first_receiver.callback(_delivery(data, "del-1", []))

    # First coupon exists, the admission coupon is still missing.
    assert await storage.get_result("adm-r1") is not None
    assert await storage.get_admission("adm-r1") is None

    # A fresh receiver resumes the admission from the persisted plan.
    acks: list[str] = []
    recovered_receiver = _receiver(broker)
    await recovered_receiver.callback(_delivery(data, "del-2", acks))

    assert runs == 1
    assert acks == ["del-2"]
    assert await storage.get_admission("adm-r1") is not None


class _ReplacingMiddleware(TaskiqMiddleware):
    """Middleware that swaps the message object and later attaches a payload."""

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        return message.model_copy(deep=True)

    def post_save(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        message.labels[ATTACHMENT_LABEL] = "from-replaced-message"


class _CountingPostSaveMiddleware(TaskiqMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def post_save(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        self.calls += 1
        message.labels[ATTACHMENT_LABEL] = f"attachment-{self.calls}"


async def test_recovery_with_both_coupons_only_acks_redelivery() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)
    runs = 0

    @broker.task
    async def my_task() -> int:
        nonlocal runs
        runs += 1
        return 9

    data = _message(broker, my_task, "adm-r2", "task-r2")
    await _receiver(broker).callback(_delivery(data, "del-1", []))
    assert runs == 1

    # Process restarts: a brand-new receiver sees the complete admission.
    acks: list[str] = []
    recovered = _receiver(broker)
    await recovered.callback(_delivery(data, "del-2", acks))
    await recovered.callback(_delivery(data, "del-3", acks))

    assert runs == 1
    assert acks == ["del-2", "del-3"]


async def test_middleware_replaces_message_and_attaches_after_save() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage).with_middlewares(_ReplacingMiddleware())
    acks: list[str] = []

    @broker.task
    async def my_task() -> int:
        return 5

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(_message(broker, my_task, "adm-mw", "task-mw"), "del-1", acks),
    )

    assert acks == ["del-1"]
    entry = await storage.get_admission("adm-mw")
    assert entry is not None and entry.attachment == b"from-replaced-message"


async def test_decorator_label_enables_two_coupon_ack() -> None:
    backend = _RecordingResultBackend()
    storage = InMemoryAdmissionStorage()
    broker = _broker(backend, storage)

    @broker.task(ack_type=AcknowledgeType.WHEN_SAVED_2PC.value)
    async def my_task() -> int:
        return 11

    receiver = _receiver(broker)
    prepared = my_task.kicker()._prepare_message()
    broker_message = broker.formatter.dumps(prepared).message
    acks: list[str] = []
    await receiver.callback(_delivery(broker_message, "del-1", acks))

    assert acks == ["del-1"]
    # Without an explicit admission label the task id becomes the admission id.
    assert await storage.get_admission(prepared.task_id) is not None
    assert await storage.get_result(prepared.task_id) is not None


async def test_explicit_admission_id_overrides_task_id() -> None:
    storage = InMemoryAdmissionStorage()
    broker = _broker(storage=storage)

    @broker.task
    async def my_task() -> None:
        return None

    receiver = _receiver(broker)
    await receiver.callback(
        _delivery(
            _message(broker, my_task, "fixed-admission", "different-task-id"),
            "del-1",
            [],
        ),
    )

    assert await storage.get_admission("fixed-admission") is not None
    assert await storage.get_admission("different-task-id") is None


async def test_recovery_does_not_rerun_post_save_after_crash() -> None:
    storage = _CrashOnAdmissionStorage()
    middleware = _CountingPostSaveMiddleware()
    broker = _broker(storage=storage).with_middlewares(middleware)
    runs = 0

    @broker.task
    async def my_task() -> int:
        nonlocal runs
        runs += 1
        return 9

    data = _message(broker, my_task, "adm-ps", "task-ps")
    storage.fail_next_admission = True
    await _receiver(broker).callback(_delivery(data, "del-1", []))
    assert middleware.calls == 1
    assert await storage.get_admission("adm-ps") is None
    assert (await storage.get_plan("adm-ps")).post_save_done is True

    await _receiver(broker).callback(_delivery(data, "del-2", []))
    assert middleware.calls == 1
    assert runs == 1
    entry = await storage.get_admission("adm-ps")
    assert entry is not None and entry.attachment == b"attachment-1"


async def test_end_to_end_through_inmemory_broker_kiq() -> None:
    storage = InMemoryAdmissionStorage()
    broker = InMemoryBroker().with_admission_storage(storage)

    @broker.task(ack_type=AcknowledgeType.WHEN_SAVED_2PC.value)
    async def admitted_task() -> str:
        return "persisted"

    kicked = await admitted_task.kicker().with_task_id("e2e-task").kiq()
    result = await kicked.wait_result(timeout=2)

    assert result.return_value == "persisted"
    assert await storage.get_result("e2e-task") is not None
    assert await storage.get_admission("e2e-task") is not None
