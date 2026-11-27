import enum
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from taskiq.utils import maybe_awaitable


@enum.unique
class AcknowledgeType(str, enum.Enum):
    """Enum with possible acknowledge times."""

    # The message is acknowledged right when it's received,
    # before it's executed.
    WHEN_RECEIVED = "when_received"
    # This option means that the message will be
    # acknowledged right after it's executed.
    WHEN_EXECUTED = "when_executed"
    # This option means that the message will be
    # acknowledged when the task will be saved
    # only after it's saved in the result backend.
    WHEN_SAVED = "when_saved"
    # This option means that the task is responsible
    # for acknowledging the message through Context.ack.
    MANUAL = "manual"
    # Two-coupon acknowledgement. The delivery is acknowledged only
    # after both the result (or continuation) coupon and the admission
    # list coupon are persisted under the fixed admission id.
    WHEN_SAVED_2PC = "when_saved_2pc"


def parse_acknowledge_type(value: Any) -> AcknowledgeType:
    """Parse acknowledge type from a task label value."""
    if isinstance(value, AcknowledgeType):
        return value
    if isinstance(value, str):
        try:
            return AcknowledgeType(value.lower())
        except ValueError as exc:
            raise ValueError(f"Unknown acknowledge type value: {value}.") from exc
    raise ValueError(
        f"Unsupported acknowledge type: {value} use str or AcknowledgeType",
    )


class AckableMessage(BaseModel):
    """
    Message that can be acknowledged.

    If your broker support message acknowledgement,
    please return this type of message, so we'll be
    able to mark this message as acknowledged after
    the function will be executed.

    It adds more reliability to brokers and system
    as a whole.

    The optional ``delivery_id`` distinguishes redeliveries of the same
    logical message: brokers that replace the message object on redelivery
    give every delivery its own id and its own ``ack`` handle.
    """

    data: bytes
    ack: Callable[[], Awaitable[None] | None]
    delivery_id: str | None = None


class AckController:
    """Controls acknowledgement state for a received message."""

    def __init__(
        self,
        ack: Callable[[], Awaitable[None] | None] | None,
        delivery_id: str | None = None,
    ) -> None:
        self._ack = ack
        self.delivery_id = delivery_id or uuid.uuid4().hex
        self.is_acked = False
        self._early_ack_requested = False

    @property
    def is_ackable(self) -> bool:
        """Whether the current message supports acknowledgement."""
        return self._ack is not None

    @property
    def early_ack_requested(self) -> bool:
        """Whether the task asked to be acknowledged before coupons were saved."""
        return self._early_ack_requested

    def mark_early_ack_requested(self) -> None:
        """Remember that the task declared itself done through Context.ack."""
        self._early_ack_requested = True

    async def ack(self) -> None:
        """Acknowledge the current message once."""
        if self._ack is None:
            raise RuntimeError("Current message is not ackable.")
        if self.is_acked:
            return
        await maybe_awaitable(self._ack())
        self.is_acked = True
