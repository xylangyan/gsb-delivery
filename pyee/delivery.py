from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from threading import Lock
from typing import Any

from pyee.base import EventEmitter, PyeeError
from pyee.uplift import uplift_emit_chain

__all__ = [
    "DeliveryAttempt",
    "DeliveryFailure",
    "DeliveryQueue",
    "DeliveryReceipt",
]


@dataclass(frozen=True)
class DeliveryFailure:
    """A verification failure recorded on a delivery receipt.

    The failure is associated with the original receipt as well as the
    receiver and listener that actually produced it, so two receipts sharing
    the same unfinished `asyncio.Future` keep independent failure records.

    Attributes:
        receipt_id: Id of the receipt whose delivery produced the failure.
        target: Receiver emitter whose listener was invoked.
        listener: The listener registration that was actually called.
        error: The exception raised by the listener or its result.
    """

    receipt_id: int
    target: EventEmitter
    listener: Callable
    error: BaseException


class DeliveryAttempt:
    """A single planned listener invocation on a receipt.

    An attempt is snapshotted when a receipt is accepted. At dispatch time it
    either becomes `called`, when the same registration is still valid on the
    target emitter, or `skipped`, when the listener was removed or
    re-registered (for instance via `once`) in the meantime.

    Attributes:
        target: Receiver emitter the listener was registered on.
        listener: The registered listener function (the `once` wrapper's key).
        status: `"pending"` before dispatch, then `"called"` or `"skipped"`.
        error: Failure observed from the listener or its returned awaitable,
            if any. Waiting cancellation never clears this field.
        wait_cancelled: Whether waiting on this attempt was cancelled.
    """

    def __init__(
        self,
        target: EventEmitter,
        listener: Callable,
        wrapped: Callable,
    ) -> None:
        self.target: EventEmitter = target
        self.listener: Callable = listener
        self.status: str = "pending"
        self.error: BaseException | None = None
        self.wait_cancelled: bool = False
        self._wrapped: Callable = wrapped
        self._source: asyncio.Future | None = None
        self._waiter: asyncio.Future | None = None

    @property
    def skipped(self) -> bool:
        """Whether this attempt was skipped at dispatch time."""
        return self.status == "skipped"

    @property
    def called(self) -> bool:
        """Whether this attempt's listener was actually invoked."""
        return self.status == "called"

    @property
    def done(self) -> bool:
        """Whether the invoked listener's asynchronous result has settled."""
        return self._source is None or self._source.done()

    def _skip(self) -> None:
        self.status = "skipped"

    def _mark_called(self) -> None:
        self.status = "called"

    def _record_error(self, error: BaseException) -> None:
        self._mark_called()
        self.error = error
        self._resolve_waiter()

    def _observe_result(self, result: Any) -> None:
        self._mark_called()

        if asyncio.iscoroutine(result):
            result = asyncio.ensure_future(result)

        if isinstance(result, asyncio.Future):
            self._source = result
            if result.done():
                self._on_source_done(result)
            else:
                result.add_done_callback(self._on_source_done)
        else:
            self._resolve_waiter()

    def _on_source_done(self, future: asyncio.Future) -> None:
        if future.cancelled():
            self.error = asyncio.CancelledError()
        else:
            exception = future.exception()
            if exception is not None:
                self.error = exception
        self._resolve_waiter()

    def _resolve_waiter(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _get_waiter(self) -> asyncio.Future:
        if self._waiter is None:
            self._waiter = asyncio.get_running_loop().create_future()
            if self._source is None or self._source.done():
                self._waiter.set_result(None)
        return self._waiter

    async def wait(self) -> None:
        """Await this attempt's verification result.

        Cancelling the awaiting coroutine only cancels this waiter; the
        underlying listener result (which may be shared with other receipts)
        keeps running. The cancellation remains visible even if the shared
        result fails later, while that failure is still recorded separately.
        """
        waiter = self._get_waiter()

        if waiter.done():
            if waiter.cancelled():
                raise asyncio.CancelledError()
            waiter.result()
            return

        try:
            await waiter
        except asyncio.CancelledError:
            self.wait_cancelled = True
            if not waiter.cancelled():
                waiter.cancel()
            raise

    def cancel_wait(self) -> None:
        """Cancel a pending waiter without cancelling the listener result."""
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.cancel()
        self.wait_cancelled = True


class DeliveryReceipt:
    """An acknowledgement for an accepted delivery.

    The receiver is snapshotted when the receipt is accepted, so switching
    the queue's line afterwards only affects newly accepted receipts.

    Attributes:
        id: Unique receipt id, assigned in acceptance order.
        event: Event name delivered to the listeners.
        target: Receiver emitter the receipt was accepted against.
        status: `"queued"`, `"withdrawn"` or `"dispatched"`.
    """

    def __init__(
        self,
        receipt_id: int,
        event: str,
        target: EventEmitter,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        attempts: list[DeliveryAttempt],
    ) -> None:
        self.id: int = receipt_id
        self.event: str = event
        self.target: EventEmitter = target
        self.status: str = "queued"
        self._args: tuple[Any, ...] = args
        self._kwargs: dict[str, Any] = kwargs
        self._attempts: list[DeliveryAttempt] = attempts

    @property
    def withdrawn(self) -> bool:
        """Whether the receipt was withdrawn before dispatch."""
        return self.status == "withdrawn"

    @property
    def dispatched(self) -> bool:
        """Whether dispatch has started for this receipt."""
        return self.status == "dispatched"

    @property
    def attempts(self) -> list[DeliveryAttempt]:
        """All listener attempts captured at acceptance time."""
        return list(self._attempts)

    @property
    def skipped_attempts(self) -> list[DeliveryAttempt]:
        """Attempts whose registrations were no longer valid at dispatch."""
        return [attempt for attempt in self._attempts if attempt.skipped]

    @property
    def called_attempts(self) -> list[DeliveryAttempt]:
        """Attempts whose listeners were actually invoked."""
        return [attempt for attempt in self._attempts if attempt.called]

    @property
    def done(self) -> bool:
        """Whether every invoked listener's result has settled."""
        return all(attempt.done for attempt in self._attempts if attempt.called)

    @property
    def wait_cancelled(self) -> bool:
        """Whether waiting on this receipt was cancelled."""
        return any(attempt.wait_cancelled for attempt in self._attempts)

    @property
    def failures(self) -> list[DeliveryFailure]:
        """Failures recorded per actually invoked listener."""
        return [
            DeliveryFailure(
                receipt_id=self.id,
                target=attempt.target,
                listener=attempt.listener,
                error=attempt.error,
            )
            for attempt in self._attempts
            if attempt.error is not None
        ]

    async def wait(self) -> None:
        """Wait for every invoked listener's verification result.

        Cancelling the awaiting coroutine cancels waiting on this receipt
        only; shared listener futures keep running and their failures remain
        recorded on this receipt.
        """
        try:
            for attempt in self._attempts:
                if attempt.called:
                    await attempt.wait()
        except asyncio.CancelledError:
            for attempt in self._attempts:
                if attempt.called and not attempt.done and not attempt.wait_cancelled:
                    attempt.cancel_wait()
            raise

    def cancel_waits(self) -> None:
        """Cancel every pending waiter belonging to this receipt."""
        for attempt in self._attempts:
            if attempt.called and not attempt.done:
                attempt.cancel_wait()


class DeliveryQueue:
    """A queueable, withdrawable delivery entry point for an event emitter.

    Receipts accepted with `accept` are queued instead of being delivered
    immediately, and may be withdrawn with `withdraw` before `dispatch`
    releases them. Acceptance snapshots the receiver line, including both
    sides of an `uplift` pair and every listener registered at that moment,
    so changing the line later only affects newly accepted receipts.

    The regular `EventEmitter.emit` keeps calling listeners immediately and
    returning its original boolean; both entry points share the same listener
    registrations.

    Args:
        target: Default receiver line for accepted receipts. May be swapped
            later with `set_target`. Individual `accept` calls may override
            it.
    """

    def __init__(self, target: EventEmitter | None = None) -> None:
        self._target: EventEmitter | None = target
        self._receipts: dict[int, DeliveryReceipt] = {}
        self._queued_ids: list[int] = []
        self._ids: count = count(1)
        self._lock: Lock = Lock()

    @property
    def target(self) -> EventEmitter | None:
        """The receiver line new receipts are accepted against."""
        return self._target

    @property
    def queued(self) -> list[DeliveryReceipt]:
        """Receipts accepted but not yet dispatched or withdrawn."""
        with self._lock:
            return [self._receipts[receipt_id] for receipt_id in self._queued_ids]

    def set_target(self, target: EventEmitter) -> None:
        """Switch the receiver line.

        Only receipts accepted afterwards use the new line; receipts already
        in the queue keep the line captured at their acceptance.

        Args:
            target: New receiver emitter for subsequent acceptances.
        """
        self._target = target

    def accept(
        self,
        event: str,
        *args: Any,
        target: EventEmitter | None = None,
        **kwargs: Any,
    ) -> DeliveryReceipt:
        """Accept a receipt into the queue without delivering it.

        The receiver line and its current listener registrations are
        snapshotted, so the receiver that will handle the receipt can be
        inspected immediately and cannot change underneath it.

        Args:
            event: Event name to deliver.
            args: Positional arguments passed to listeners at dispatch.
            target: Receiver for this receipt. Defaults to the queue's
                current line.
            kwargs: Keyword arguments passed to listeners at dispatch.

        Returns:
            The queued delivery receipt.

        Raises:
            PyeeError: When no receiver line is configured.
        """
        receiver = target if target is not None else self._target
        if receiver is None:
            raise PyeeError("No target event emitter is configured")

        attempts: list[DeliveryAttempt] = []
        for emitter in uplift_emit_chain(receiver):
            with emitter._lock:
                entries = list(emitter._events.get(event, OrderedDict()).items())
            for listener, wrapped in entries:
                attempts.append(DeliveryAttempt(emitter, listener, wrapped))

        with self._lock:
            receipt_id = next(self._ids)
            receipt = DeliveryReceipt(
                receipt_id=receipt_id,
                event=event,
                target=receiver,
                args=args,
                kwargs=kwargs,
                attempts=attempts,
            )
            self._receipts[receipt_id] = receipt
            self._queued_ids.append(receipt_id)

        return receipt

    def withdraw(self, receipt: DeliveryReceipt) -> bool:
        """Withdraw a queued receipt before it is dispatched.

        Withdrawn receipts are never delivered, so no listener receives them.

        Args:
            receipt: Receipt to withdraw.

        Returns:
            `True` when the receipt was queued and is now withdrawn, otherwise
                `False` (for instance after dispatch already began).
        """
        with self._lock:
            if receipt.status != "queued":
                return False
            receipt.status = "withdrawn"
            self._queued_ids.remove(receipt.id)
            return True

    def dispatch(self, receipt: DeliveryReceipt | None = None) -> DeliveryReceipt:
        """Deliver a queued receipt to the listeners captured at acceptance.

        Each attempt is re-validated against its target emitter immediately
        before invocation, so `once` registrations removed or re-registered
        after acceptance are reported as skipped instead of being called.
        Dispatch runs without the queue lock, so listeners may accept further
        receipts reentrantly.

        Args:
            receipt: Receipt to release. `None` releases the oldest queued
                receipt.

        Returns:
            The dispatched receipt. Withdrawn receipts passed explicitly are
                returned untouched.

        Raises:
            PyeeError: When no receipt is given and the queue is empty, or
                the given receipt was already dispatched.
        """
        with self._lock:
            if receipt is None:
                if not self._queued_ids:
                    raise PyeeError("No receipts are queued for delivery")
                receipt = self._receipts[self._queued_ids[0]]

            if receipt.status == "withdrawn":
                return receipt
            if receipt.status != "queued":
                raise PyeeError(f"Receipt {receipt.id} is not queued")

            self._queued_ids.remove(receipt.id)
            receipt.status = "dispatched"

        for attempt in list(receipt._attempts):
            if not self._is_still_registered(receipt, attempt):
                attempt._skip()
                continue

            try:
                result = attempt._wrapped(*receipt._args, **receipt._kwargs)
            except Exception as error:  # noqa: BLE001
                attempt._record_error(error)
            else:
                attempt._observe_result(result)

        return receipt

    def _is_still_registered(
        self,
        receipt: DeliveryReceipt,
        attempt: DeliveryAttempt,
    ) -> bool:
        with attempt.target._lock:
            table = attempt.target._events.get(receipt.event)
            return table is not None and table.get(attempt.listener) is attempt._wrapped
