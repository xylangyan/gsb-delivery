"""Per-publication tracking of publisher confirms and mandatory returns.

A `Basic.Return` frame carries no delivery tag, so an unroutable publish can
only be correlated with its publication through an identifier carried in the
message itself. `PublishTracker` stamps each publish with a unique message id
in a headers field, then reconciles three broker signals against that id:

* the delivery tag handed to `on_publish`, which links the publication to the
  broker's confirm sequence;
* `Basic.Ack` / `Basic.Nack`, addressed by delivery tag, possibly with
  `multiple=True` covering every earlier tag;
* `Basic.Return`, addressed by the message id and always arriving before the
  `Basic.Ack` for the same publish.

The tracker is channel scoped. A channel reopened with the same channel number
restarts the broker's delivery-tag counter at 1, so publications on the new
channel need a new tracker; the old one stays readable for its own records and
can report what was still outstanding when the channel went away.
"""

from __future__ import annotations

import functools
import logging
import threading
import uuid
from collections import namedtuple
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from pika import spec
from pika._utils import override

if TYPE_CHECKING:
    from pika import frame

LOGGER = logging.getLogger(__name__)

#: Headers field the tracker stores each publication's message id under. RabbitMQ
#: accepts arbitrary string keys in the field table, and the `x-` prefix marks
#: it as application defined rather than broker defined.
DEFAULT_MESSAGE_ID_HEADER = 'x-pika-message-id'


class PublishOutcome(IntEnum):
    """
    Terminal state of a tracked publication.

    Attributes:
        Pending: No terminal frame has been processed yet.
        Acked: A `Basic.Ack` covered the delivery tag and no return was seen.
        Returned: A `Basic.Return` was seen; the confirm frame, if any, is kept
            on the publication as well.
        Nacked: A `Basic.Nack` covered the delivery tag and no return was seen.
        Uncertain: The channel went away while the publication was still
            pending; no broker verdict was ever observed.
    """

    Pending = 0
    Acked = 1
    Returned = 2
    Nacked = 3
    Uncertain = 4

    @property
    def is_terminal(self) -> bool:
        """Whether the outcome can no longer change while the channel lives."""
        return self is not PublishOutcome.Pending


class ReturnedMessage:
    """A message handed back by the broker via `Basic.Return`."""

    __slots__ = ('body', 'method', 'properties')

    def __init__(self, method: spec.Basic.Return,
                 properties: spec.BasicProperties, body: bytes) -> None:
        """
        :param method: The `Basic.Return` method, carrying reply code and text.
        :param properties: The properties the message was published with.
        :param body: The published body, unchanged.
        """
        self.method = method
        self.properties = properties
        self.body = body


class Publication:
    """
    One tracked publish, mutated only under `PublishTracker`'s condition.

    Attributes:
        message_id: The unique id stamped into the message headers.
        delivery_tag: The broker delivery tag reported by `on_publish`, or
            ``None`` until that callback runs.
        outcome: The current verdict.
        returned: The returned message, or ``None`` when the broker did not
            return this publication.
        confirm_method: The `Basic.Ack` or `Basic.Nack` method that covered
            the delivery tag, or ``None`` before a confirm arrives.
    """

    __slots__ = (
        'confirm_method',
        'delivery_tag',
        'message_id',
        'outcome',
        'returned',
    )

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.delivery_tag: int | None = None
        self.outcome = PublishOutcome.Pending
        self.returned: ReturnedMessage | None = None
        self.confirm_method: spec.Basic.Ack | spec.Basic.Nack | None = None

    @override
    def __repr__(self) -> str:
        return (f'<Publication message_id={self.message_id!r} '
                f'delivery_tag={self.delivery_tag} outcome={self.outcome.name}>')


#: Frozen report of everything the tracker knew at one instant.
#:
#: ``last_delivery_tag`` is the highest tag an `on_publish` reported, and
#: ``last_confirm_tag`` the highest tag any Ack/Nack frame covered (including
#: via ``multiple``). Either can be ``None``. The gap between them, joined with
#: ``pending`` and ``uncertain``, is the boundary a caller cannot see past.
TrackerSnapshot = namedtuple(
    'TrackerSnapshot',
    ('last_delivery_tag', 'last_confirm_tag', 'resolved', 'pending',
     'uncertain', 'unmatched_returns'))


def with_message_id(properties: spec.BasicProperties | None,
                    message_id: str,
                    header_name: str = DEFAULT_MESSAGE_ID_HEADER
                    ) -> spec.BasicProperties:
    """
    Return properties carrying *message_id* in their headers.

    The input properties are copied rather than mutated, so a properties object
    shared between publishes keeps its original headers. Every other field is
    preserved verbatim.

    :param properties: The source properties, or ``None`` for defaults.
    :param message_id: The id to store.
    :param header_name: The headers field to store it under.
    :returns: A new `pika.spec.BasicProperties` instance.
    :raises ValueError: if *header_name* is already set in the source headers.
    """
    source = properties if properties is not None else spec.BasicProperties()
    headers = dict(source.headers or {})
    if header_name in headers:
        raise ValueError(
            f'header {header_name!r} is reserved for the publish tracker but '
            f'already holds {headers[header_name]!r}')
    headers[header_name] = message_id
    return spec.BasicProperties(
        content_type=source.content_type,
        content_encoding=source.content_encoding,
        headers=headers,
        delivery_mode=source.delivery_mode,
        priority=source.priority,
        correlation_id=source.correlation_id,
        reply_to=source.reply_to,
        expiration=source.expiration,
        message_id=source.message_id,
        timestamp=source.timestamp,
        type=source.type,
        user_id=source.user_id,
        app_id=source.app_id,
        cluster_id=source.cluster_id)


def message_id_of(properties: spec.BasicProperties,
                  header_name: str = DEFAULT_MESSAGE_ID_HEADER) -> str | None:
    """
    Read the tracker message id from returned message properties.

    :param properties: Properties from a `Basic.Return` frame.
    :param header_name: The headers field the id is stored under.
    :returns: The id, or ``None`` when the field is absent or not a string.
    """
    headers = properties.headers or {}
    value = headers.get(header_name)
    return value if isinstance(value, str) else None


class PublishTracker:
    """
    Reconcile publisher confirms and mandatory returns per publication.

    One instance serves one channel. Wire it to a `pika.Connection` channel with
    `arm`, publish through `publish`, and block on individual verdicts with
    `wait` or `wait_all`. Every method is safe to call from any thread; the
    confirm and return callbacks run on the channel's worker thread while
    `on_publish` runs on the IOLoop thread.
    """

    def __init__(self,
                 header_name: str = DEFAULT_MESSAGE_ID_HEADER) -> None:
        """
        :param header_name: Headers field to carry each publication's id.
        """
        self._header_name = header_name
        self._condition = threading.Condition()
        self._by_id: dict[str, Publication] = {}
        self._by_tag: dict[int, Publication] = {}
        self._unmatched_returns: list[ReturnedMessage] = []
        self._last_delivery_tag: int | None = None
        self._last_confirm_tag: int | None = None

    @property
    def header_name(self) -> str:
        """The headers field this tracker stores publication ids under."""
        return self._header_name

    @property
    def last_delivery_tag(self) -> int | None:
        """Highest delivery tag assigned on this channel, if any."""
        with self._condition:
            return self._last_delivery_tag

    @property
    def last_confirm_tag(self) -> int | None:
        """Highest delivery tag covered by an Ack/Nack frame, if any."""
        with self._condition:
            return self._last_confirm_tag

    def arm(self, channel: Any) -> None:
        """
        Enable publisher confirms on *channel* and register the tracker's frames.

        :param channel: A `pika.adapters.thread_safe_connection.Channel`; the
            method relies on its `confirm_delivery`, `add_on_return_callback`
            and `basic_publish(on_publish=...)` APIs.
        """
        channel.confirm_delivery(self.on_confirm)
        channel.add_on_return_callback(self.on_return)

    def publish(self,
                channel: Any,
                exchange: str,
                routing_key: str,
                body: bytes | str,
                properties: spec.BasicProperties | None = None,
                *,
                mandatory: bool = True,
                message_id: str | None = None) -> Publication:
        """
        Stamp *body* with a unique id and publish it on *channel*.

        The publication is registered before the publish frame is written, so a
        frame the broker answers immediately cannot outrun registration.

        :param channel: The channel to publish on.
        :param exchange: The exchange to publish to.
        :param routing_key: The routing key.
        :param body: The message body.
        :param properties: Optional properties; copied, not mutated.
        :param mandatory: The mandatory flag; defaults to ``True`` because
            tracking returns is pointless without it.
        :param message_id: An explicit id, or ``None`` to generate a uuid hex.
        :returns: The tracked `Publication`.
        :raises ValueError: if *message_id* is already tracked, or its header is
            preset on *properties*.
        """
        message_id = message_id or uuid.uuid4().hex
        stamped = with_message_id(properties, message_id, self._header_name)
        record = self._register_pending(message_id)
        try:
            channel.basic_publish(exchange=exchange,
                                  routing_key=routing_key,
                                  body=body,
                                  properties=stamped,
                                  mandatory=mandatory,
                                  on_publish=functools.partial(
                                      self._bind_delivery_tag, record))
        except BaseException:
            self._discard(record)
            raise
        return record

    def on_confirm(self, method_frame: frame.Method[spec.Basic.Ack |
                                                    spec.Basic.Nack]) -> None:
        """
        Handle a `Basic.Ack` or `Basic.Nack` frame.

        Suitable as the `confirm_delivery` callback. A `multiple` frame resolves
        every tracked publication whose tag is at most the frame's tag.

        :param method_frame: The confirm frame from the broker.
        """
        method = method_frame.method
        tag = method.delivery_tag
        is_ack = isinstance(method, spec.Basic.Ack)
        with self._condition:
            self._remember_confirm_tag(tag)
            if method.multiple:
                records = [
                    record for record in self._by_id.values()
                    if record.delivery_tag is not None
                    and record.delivery_tag <= tag
                ]
            else:
                record = self._by_tag.get(tag)
                records = [record] if record is not None else []
            for record in records:
                self._apply_confirm(record, method, is_ack)
            self._condition.notify_all()

    def on_return(self, channel: Any, method: spec.Basic.Return,
                  properties: spec.BasicProperties, body: bytes) -> None:
        """
        Handle a `Basic.Return` frame.

        Suitable as an `add_on_return_callback` callback. The frame has no
        delivery tag, so correlation is by the stamped message id. A return that
        cannot be matched, including one whose id header is missing, is retained
        on `snapshot` rather than dropped.

        :param channel: The channel the frame arrived on; unused but part of the
            callback contract.
        :param method: The `Basic.Return` method.
        :param properties: The returned message's properties.
        :param body: The returned message's body.
        """
        message_id = message_id_of(properties, self._header_name)
        returned = ReturnedMessage(method, properties, body)
        with self._condition:
            record = (self._by_id.get(message_id)
                      if message_id is not None else None)
            if record is None:
                LOGGER.warning(
                    'Basic.Return could not be matched to a tracked '
                    'publication (message_id=%r, reply_code=%r); retaining it '
                    'on the tracker snapshot', message_id, method.reply_code)
                self._unmatched_returns.append(returned)
                return
            record.returned = returned
            record.outcome = PublishOutcome.Returned
            self._condition.notify_all()

    def wait(self, message_id: str, timeout: float | None = None) -> Publication:
        """
        Block until the publication with *message_id* reaches a terminal outcome.

        :param message_id: The id returned by `publish`.
        :param timeout: Seconds to wait, or ``None`` to wait indefinitely.
        :returns: The publication; still `PublishOutcome.Pending` only when
            *timeout* elapsed, `PublishOutcome.Uncertain` after a disconnect.
        :raises KeyError: if *message_id* was never tracked.
        """
        with self._condition:
            record = self._by_id[message_id]
            self._condition.wait_for(
                lambda: record.outcome.is_terminal, timeout=timeout)
            return record

    def wait_all(self, timeout: float | None = None) -> bool:
        """
        Block until every tracked publication reaches a terminal outcome.

        :param timeout: Seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` if all publications are terminal, ``False`` on timeout.
        """
        with self._condition:
            return self._condition.wait_for(
                lambda: all(record.outcome.is_terminal
                            for record in self._by_id.values()),
                timeout=timeout)

    def get(self, message_id: str) -> Publication:
        """Return the publication with *message_id* without blocking."""
        with self._condition:
            return self._by_id[message_id]

    def wait_for_delivery_tag(self,
                              message_id: str,
                              timeout: float | None = None) -> int:
        """
        Block until `on_publish` reports the publication's delivery tag.

        The thread-safe connection writes the publish frame on its IOLoop
        thread, so the tag is not assigned when `publish` returns. Read it
        through this method rather than polling the publication.

        :param message_id: The id returned by `publish`.
        :param timeout: Seconds to wait, or ``None`` to wait indefinitely.
        :returns: The broker delivery tag.
        :raises KeyError: if *message_id* was never tracked.
        :raises TimeoutError: if *timeout* elapses before the tag is bound.
        """
        with self._condition:
            record = self._by_id[message_id]
            ready = self._condition.wait_for(
                lambda: record.delivery_tag is not None, timeout=timeout)
            if not ready:
                raise TimeoutError(
                    f'no delivery tag for message_id {message_id!r} within '
                    f'{timeout}s')
            assert record.delivery_tag is not None
            return record.delivery_tag

    def mark_disconnected(self) -> None:
        """
        Mark every still-pending publication uncertain and wake waiters.

        Call when the channel or connection is lost: a pending publication has
        no broker verdict, and waiting on it would otherwise block past the
        loss. Returns and confirms already processed are left untouched.
        """
        with self._condition:
            for record in self._by_id.values():
                if record.outcome is PublishOutcome.Pending:
                    record.outcome = PublishOutcome.Uncertain
            self._condition.notify_all()

    def snapshot(self) -> TrackerSnapshot:
        """
        Capture the tracker's known state and the observable confirm boundary.
        """
        with self._condition:
            resolved = tuple(
                record for record in self._by_id.values()
                if record.outcome in (PublishOutcome.Acked,
                                      PublishOutcome.Returned,
                                      PublishOutcome.Nacked))
            pending = tuple(record for record in self._by_id.values()
                            if record.outcome is PublishOutcome.Pending)
            uncertain = tuple(record for record in self._by_id.values()
                              if record.outcome is PublishOutcome.Uncertain)
            return TrackerSnapshot(
                last_delivery_tag=self._last_delivery_tag,
                last_confirm_tag=self._last_confirm_tag,
                resolved=resolved,
                pending=pending,
                uncertain=uncertain,
                unmatched_returns=tuple(self._unmatched_returns))

    def _register_pending(self, message_id: str) -> Publication:
        with self._condition:
            if message_id in self._by_id:
                raise ValueError(
                    f'message_id {message_id!r} is already tracked on this '
                    'channel; ids must be unique per channel')
            record = Publication(message_id)
            self._by_id[message_id] = record
            return record

    def _bind_delivery_tag(self, record: Publication, delivery_tag: int) -> None:
        with self._condition:
            record.delivery_tag = delivery_tag
            self._by_tag[delivery_tag] = record
            if (self._last_delivery_tag is None
                    or delivery_tag > self._last_delivery_tag):
                self._last_delivery_tag = delivery_tag
            self._condition.notify_all()

    def _discard(self, record: Publication) -> None:
        with self._condition:
            self._by_id.pop(record.message_id, None)
            if record.delivery_tag is not None:
                self._by_tag.pop(record.delivery_tag, None)
            self._condition.notify_all()

    def _remember_confirm_tag(self, tag: int) -> None:
        if self._last_confirm_tag is None or tag > self._last_confirm_tag:
            self._last_confirm_tag = tag

    @staticmethod
    def _apply_confirm(record: Publication,
                       method: spec.Basic.Ack | spec.Basic.Nack,
                       is_ack: bool) -> None:
        record.confirm_method = method
        if record.outcome is PublishOutcome.Returned:
            # The broker returns an unroutable publish and then confirms the
            # same tag; the return is the routing verdict and stays primary.
            return
        record.outcome = (PublishOutcome.Acked
                          if is_ack else PublishOutcome.Nacked)
