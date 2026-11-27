"""Persistence primitives for the two-coupon acknowledgement flow.

When a task opts into the :attr:`~taskiq.acks.AcknowledgeType.WHEN_SAVED_2PC`
acknowledge type, the receiver treats every delivery as an *admission*
(入仓) identified by a fixed admission id. It persists an admission plan
before running the task and requires two independently written receipts
(coupons) before the broker delivery is acknowledged:

1. the result coupon (or a continuation coupon for requeued messages);
2. the admission-list entry describing the admitted delivery.

The storage backends deliberately expose only idempotent, key-based
writes and lookups, so an interrupted process can resume the missing
coupon when the broker redelivers the message.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult

_ReturnType = TypeVar("_ReturnType")

ADMISSION_ID_LABEL = "X-Taskiq-admission-id"
ATTACHMENT_LABEL = "X-Taskiq-attachment"


@dataclass(slots=True)
class AdmissionPlan:
    """Everything required to finish an admission, saved before execution."""

    task_id: str
    task_name: str
    labels: dict[str, Any]
    args: list[Any]
    kwargs: dict[str, Any]
    result: TaskiqResult[Any] | None = None
    attachment: bytes | None = None
    continuation: "ContinuationRecord | None" = None
    early_ack: bool = False
    post_save_done: bool = False

    @classmethod
    def from_message(cls, message: TaskiqMessage) -> "AdmissionPlan":
        """Build a plan from a received :class:`TaskiqMessage`."""
        return cls(
            task_id=message.task_id,
            task_name=message.task_name,
            labels=dict(message.labels),
            args=list(message.args),
            kwargs=dict(message.kwargs),
        )


@dataclass(slots=True)
class AdmissionEntry:
    """The admission-list coupon, written only after the first coupon."""

    admission_id: str
    task_id: str
    attachment: bytes | None = None
    continuation: "ContinuationRecord | None" = None


@dataclass(slots=True)
class ContinuationRecord:
    """A persisted request to hand the work over to the next round."""

    task_id: str
    message: bytes
    requeue_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Delivery id of the next-round message once it is received.
    delivered_delivery_id: str | None = None


class AdmissionStorage(ABC, Generic[_ReturnType]):
    """Persistence backend for admission plans and the two coupons."""

    async def startup(self) -> None:
        """Prepare the storage, e.g. open a database connection."""

    async def shutdown(self) -> None:
        """Release resources held by the storage."""

    @abstractmethod
    async def save_plan(self, admission_id: str, plan: AdmissionPlan) -> None:
        """Persist an admission plan under the admission id.

        Repeated writes with the same key replace the previous plan
        instead of duplicating it.
        """

    @abstractmethod
    async def get_plan(self, admission_id: str) -> AdmissionPlan | None:
        """Return the saved plan, or ``None`` when the key is missing."""

    @abstractmethod
    async def save_result(
        self,
        admission_id: str,
        result: TaskiqResult[_ReturnType],
    ) -> None:
        """Write the result coupon under the admission id.

        Repeated writes with the same key replace the previous result.
        """

    @abstractmethod
    async def get_result(
        self,
        admission_id: str,
    ) -> TaskiqResult[_ReturnType] | None:
        """Return the result coupon, or ``None`` when it is missing."""

    @abstractmethod
    async def save_admission(
        self,
        admission_id: str,
        entry: AdmissionEntry,
    ) -> None:
        """Write the admission-list coupon under the admission id."""

    @abstractmethod
    async def get_admission(self, admission_id: str) -> AdmissionEntry | None:
        """Return the admission-list coupon, or ``None`` when missing."""

    @abstractmethod
    async def save_continuation(
        self,
        admission_id: str,
        continuation: ContinuationRecord,
    ) -> None:
        """Persist a continuation request under the admission id."""

    @abstractmethod
    async def get_continuation(
        self,
        admission_id: str,
    ) -> ContinuationRecord | None:
        """Return the continuation request, or ``None`` when missing."""


class InMemoryAdmissionStorage(AdmissionStorage[Any]):
    """Dict-backed admission storage intended for tests and local runs."""

    def __init__(self) -> None:
        self._plans: dict[str, AdmissionPlan] = {}
        self._results: dict[str, TaskiqResult[Any]] = {}
        self._admissions: dict[str, AdmissionEntry] = {}
        self._continuations: dict[str, ContinuationRecord] = {}

    async def save_plan(self, admission_id: str, plan: AdmissionPlan) -> None:
        """Persist the plan, replacing any plan with the same admission id."""
        self._plans[admission_id] = plan

    async def get_plan(self, admission_id: str) -> AdmissionPlan | None:
        """Look up a plan by admission id."""
        return self._plans.get(admission_id)

    async def save_result(
        self,
        admission_id: str,
        result: TaskiqResult[Any],
    ) -> None:
        """Persist the result coupon, replacing any previous value."""
        self._results[admission_id] = result

    async def get_result(self, admission_id: str) -> TaskiqResult[Any] | None:
        """Look up the result coupon by admission id."""
        return self._results.get(admission_id)

    async def save_admission(
        self,
        admission_id: str,
        entry: AdmissionEntry,
    ) -> None:
        """Persist the admission-list coupon."""
        self._admissions[admission_id] = entry

    async def get_admission(self, admission_id: str) -> AdmissionEntry | None:
        """Look up the admission-list coupon by admission id."""
        return self._admissions.get(admission_id)

    async def save_continuation(
        self,
        admission_id: str,
        continuation: ContinuationRecord,
    ) -> None:
        """Persist a continuation request."""
        self._continuations[admission_id] = continuation

    async def get_continuation(
        self,
        admission_id: str,
    ) -> ContinuationRecord | None:
        """Look up a continuation request by admission id."""
        return self._continuations.get(admission_id)
