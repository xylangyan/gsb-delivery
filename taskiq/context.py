from typing import TYPE_CHECKING, Any

from taskiq.abc.broker import AsyncBroker
from taskiq.acks import (
    AckController,
    AcknowledgeType,
    parse_acknowledge_type,
)
from taskiq.exceptions import NoResultError, TaskRejectedError
from taskiq.message import TaskiqMessage
from taskiq.storage import ADMISSION_ID_LABEL, AdmissionStorage, ContinuationRecord

if TYPE_CHECKING:  # pragma: no cover
    from taskiq.state import TaskiqState


class Context:
    """Context class."""

    def __init__(
        self,
        message: TaskiqMessage,
        broker: AsyncBroker,
        ack_controller: AckController | None = None,
        admission_storage: AdmissionStorage[Any] | None = None,
    ) -> None:
        self.message = message
        self.broker = broker
        self._ack_controller = ack_controller
        self._admission_storage = admission_storage
        self.state: TaskiqState = None  # type: ignore
        self.state = broker.state

    @property
    def is_ackable(self) -> bool:
        """Whether the current message supports acknowledgement."""
        return self._ack_controller is not None and self._ack_controller.is_ackable

    @property
    def is_acked(self) -> bool:
        """Whether the current message has already been acknowledged."""
        return self._ack_controller is not None and self._ack_controller.is_acked

    @property
    def admission_id(self) -> str | None:
        """Fixed admission id (入仓号) of the current delivery, if assigned."""
        value = self.message.labels.get(ADMISSION_ID_LABEL)
        if value is None:
            return None
        return str(value)

    @property
    def _uses_two_coupon_ack(self) -> bool:
        ack_type = self.message.labels.get("ack_type")
        if ack_type is None:
            return False
        try:
            return parse_acknowledge_type(ack_type) == AcknowledgeType.WHEN_SAVED_2PC
        except ValueError:
            return False

    async def ack(self) -> None:
        """
        Acknowledge current message.

        For two-coupon tasks the acknowledgement is deferred until both
        coupons are persisted; calling this method only records that the
        task considers itself done. Other acknowledge types acknowledge
        the delivery immediately.

        :raises RuntimeError: if current broker message is not ackable.
        """
        if self._ack_controller is None:
            raise RuntimeError("Current message is not ackable.")
        if self._uses_two_coupon_ack:
            self._ack_controller.mark_early_ack_requested()
            return
        await self._ack_controller.ack()

    async def requeue(self) -> None:
        """
        Requeue task.

        This function creates a task with
        the same message and sends it using
        current broker.

        For two-coupon deliveries the continuation request is persisted
        next to the admission plan before the next-round message is
        kicked, so the receiver can resume it after a crash.

        :raises NoResultError: to not store result for current task.
        """
        requeue_count = int(self.message.labels.get("X-Taskiq-requeue", 0))
        requeue_count += 1
        self.message.labels["X-Taskiq-requeue"] = str(requeue_count)
        broker_message = self.broker.formatter.dumps(self.message)
        if self._uses_two_coupon_ack and self._admission_storage is not None:
            admission_id = self.admission_id
            if admission_id is not None:
                continuation = ContinuationRecord(
                    task_id=self.message.task_id,
                    message=broker_message.message,
                    requeue_count=requeue_count,
                )
                plan = await self._admission_storage.get_plan(admission_id)
                if plan is not None:
                    plan.continuation = continuation
                    await self._admission_storage.save_plan(admission_id, plan)
                await self._admission_storage.save_continuation(
                    admission_id,
                    continuation,
                )
        await self.broker.kick(broker_message)
        raise NoResultError

    def reject(self) -> None:
        """
        Raise reject error.

        :raises TaskRejectedError: to reject current message.
        """
        raise TaskRejectedError
