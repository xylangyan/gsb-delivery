# -*- coding: utf-8 -*-
# MinIO Python Library for Amazon S3 Compatible Cloud Storage, (C)
# [2014] - [2025] MinIO, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Identity based notification event reading.

``EventIterable`` exposes the raw ``Records`` envelopes sent by the server.
An envelope is a transport unit, not an event unit: a reconnect can split the
same events across different envelopes, replay them in another order, or omit
history the queue no longer retains.

:class:`NotificationReader` keeps one ledger entry per real event identity and
lets the caller acknowledge each identity separately.  Envelopes are never the
unit of acknowledgement, and a sequencer is never treated as a global order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterator, Optional, Union
from urllib.parse import unquote_plus

from typing_extensions import Protocol

__all__ = [
    "EventEvidence",
    "EventEvidenceProvider",
    "EventIdentity",
    "EventState",
    "NotificationEvent",
    "NotificationReader",
    "ObjectGeneration",
    "ReconcileReport",
    "ReplayGapReport",
]

_DELETE_EVENT_MARKERS = ("removed", "delete")


class EventState(str, Enum):
    """Delivery state of one event identity."""
    DELIVERED = "delivered"
    PENDING = "pending"
    ACKED = "acked"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


def _as_generation(value: Any) -> Optional[int]:
    """Return a non-negative integer generation or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            return int(value)
    return None


def _first(*values: Any) -> Any:
    """Return the first value which is not ``None``."""
    for value in values:
        if value is not None:
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return value as a mapping or an empty mapping."""
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class EventIdentity:
    """Stable identity of one real notification event.

    A service event id, when present, is the strongest identity.  Otherwise the
    identity is the object plus the event name plus the most specific marker
    available: generation, version id, sequencer, or event time.  Object name
    is always part of the key, so two objects which happen to share a
    sequencer value never share an identity.
    """
    bucket_name: str
    object_name: str
    event_name: str
    generation: Optional[int] = None
    version_id: Optional[str] = None
    sequencer: Optional[str] = None
    event_id: Optional[str] = None
    event_time: Optional[str] = None

    @property
    def key(self) -> tuple[Any, ...]:
        """Key used by the ledger for duplicate detection.

        All available markers are part of the key.  A replay which carries a
        different subset of markers is matched by :meth:`_equivalent_key`
        instead of being treated as a new event.
        """
        if self.event_id:
            return ("event-id", self.bucket_name, self.event_id)
        return (
            "record",
            self.bucket_name,
            self.object_name,
            self.event_name,
            self.generation,
            self.version_id,
            self.sequencer,
            self.event_time,
        )

    def describe(self) -> str:
        """Human readable form used in evidence and gap reports."""
        location = f"{self.bucket_name}/{self.object_name}"
        if self.generation is not None:
            location += f"@{self.generation}"
        description = f"{location} ({self.event_name})"
        if self.event_id:
            description += f" event-id={self.event_id}"
        return description


@dataclass(frozen=True)
class NotificationEvent:
    """One real event parsed from a ``Records`` entry."""
    bucket_name: str
    object_name: str
    event_name: str
    generation: Optional[int] = None
    version_id: Optional[str] = None
    sequencer: Optional[str] = None
    event_id: Optional[str] = None
    event_time: Optional[str] = None
    raw: Optional[Mapping[str, Any]] = field(
        default=None, compare=False, repr=False,
    )

    @property
    def identity(self) -> EventIdentity:
        """Stable identity of this event."""
        return EventIdentity(
            bucket_name=self.bucket_name,
            object_name=self.object_name,
            event_name=self.event_name,
            generation=self.generation,
            version_id=self.version_id,
            sequencer=self.sequencer,
            event_id=self.event_id,
            event_time=self.event_time,
        )

    @property
    def is_delete(self) -> bool:
        """Whether this record represents a removal."""
        name = self.event_name.lower()
        return any(marker in name for marker in _DELETE_EVENT_MARKERS)

    @classmethod
    def from_record(
            cls,
            record: Mapping[str, Any],
            *,
            decode_key: bool = True,
    ) -> NotificationEvent:
        """Build an event from one MinIO notification record."""
        s3_info = _mapping(record.get("s3"))
        bucket_info = _mapping(s3_info.get("bucket"))
        object_info = _mapping(s3_info.get("object"))

        bucket_name = str(_first(
            bucket_info.get("name"),
            record.get("bucketName"),
            record.get("bucket"),
            "",
        ))
        object_name = str(_first(
            object_info.get("key"),
            record.get("objectName"),
            record.get("key"),
            "",
        ))
        if decode_key:
            object_name = unquote_plus(object_name)

        event_name = str(_first(
            record.get("eventName"),
            record.get("event"),
            record.get("eventType"),
            "",
        ))
        generation_value = _first(
            record.get("generation"),
            record.get("objectGeneration"),
            record.get("generationNumber"),
            object_info.get("generation"),
            object_info.get("objectGeneration"),
            object_info.get("generationNumber"),
        )
        generation = _as_generation(generation_value)
        version_id = _first(
            object_info.get("versionId"),
            record.get("versionId"),
        )
        sequencer = _first(
            object_info.get("sequencer"),
            record.get("sequencer"),
        )
        event_id = _first(
            record.get("eventId"),
            record.get("eventID"),
            s3_info.get("eventId"),
        )
        event_time = _first(
            record.get("eventTime"),
            record.get("time"),
        )

        return cls(
            bucket_name=bucket_name,
            object_name=object_name,
            event_name=event_name,
            generation=generation,
            version_id=(
                str(version_id) if version_id is not None else None
            ),
            sequencer=str(sequencer) if sequencer is not None else None,
            event_id=str(event_id) if event_id is not None else None,
            event_time=str(event_time) if event_time is not None else None,
            raw=record,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the raw record or a reconstructed S3 notification."""
        if self.raw is not None:
            return dict(self.raw)
        object_info: dict[str, Any] = {"key": self.object_name}
        if self.generation is not None:
            object_info["generation"] = str(self.generation)
        if self.version_id is not None:
            object_info["versionId"] = self.version_id
        if self.sequencer is not None:
            object_info["sequencer"] = self.sequencer
        result: dict[str, Any] = {
            "eventName": self.event_name,
            "s3": {
                "bucket": {"name": self.bucket_name},
                "object": object_info,
            },
        }
        if self.event_time is not None:
            result["eventTime"] = self.event_time
        if self.event_id is not None:
            result["eventId"] = self.event_id
        return result


@dataclass(frozen=True)
class EventEvidence:
    """Service-side result of one bucket event id lookup."""
    event_id: str
    applied: bool = False
    generation: Optional[int] = None
    deleted: Optional[bool] = None
    superseded_by: Optional[int] = None


@dataclass(frozen=True)
class ObjectGeneration:
    """One entry of an object's generation order."""
    generation: int
    version_id: Optional[str] = None
    is_delete_marker: bool = False
    sequencer: Optional[str] = None
    event_id: Optional[str] = None


class EventEvidenceProvider(Protocol):
    """Evidence source for event ids and object generation orders."""

    def event_evidence(
            self, *, bucket_name: str, event_id: str,
    ) -> Optional[EventEvidence]:
        """Return the service result for one bucket event id."""

    def object_generations(
            self, *, bucket_name: str, object_name: str,
    ) -> Sequence[ObjectGeneration]:
        """Return the object's generation order, tombstones included."""


@dataclass
class _Record:
    """Ledger entry for one event identity."""
    event: NotificationEvent
    state: EventState = EventState.DELIVERED
    reason: Optional[str] = None
    deliveries: int = 0
    acknowledgements: int = 0


@dataclass
class _ObjectState:
    """Per-object generation state."""
    current_generation: Optional[int] = None
    current_deleted: bool = False
    highest_generation: Optional[int] = None
    highest_deleted: bool = False


@dataclass(frozen=True)
class ReconcileReport:
    """Outcome of reconciling unacknowledged events with evidence."""
    confirmed: tuple[EventIdentity, ...] = ()
    superseded: tuple[EventIdentity, ...] = ()
    unconfirmed: tuple[EventIdentity, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.confirmed or self.superseded or self.unconfirmed)


@dataclass(frozen=True)
class ReplayGapReport:
    """What replay can and cannot recover once queue history is gone.

    ``rescan_objects`` can be inspected for their current generation or
    presence.  ``generation_history_required`` names objects where an
    unresolved deletion needs the per-object generation order.  Final state
    alone cannot prove that an intermediate deletion was delivered, so such
    events stay listed in ``cannot_confirm_by_final_state``.
    """
    reason: str
    affected_objects: tuple[tuple[str, str], ...]
    unresolved_events: tuple[EventIdentity, ...]
    rescan_objects: tuple[tuple[str, str], ...]
    final_state_can_confirm: tuple[tuple[str, str], ...]
    generation_history_required: tuple[tuple[str, str], ...]
    cannot_confirm_by_final_state: tuple[EventIdentity, ...]

    def explain(self) -> str:
        """Explain the boundary between rescan and generation evidence."""
        parts = [f"Replay history is unavailable: {self.reason}."]
        if self.rescan_objects:
            names = ", ".join(
                f"{bucket}/{obj}" for bucket, obj in self.rescan_objects
            )
            parts.append(f"Objects requiring rescan: {names}.")
        if self.final_state_can_confirm:
            parts.append(
                "Rescan can confirm only current generation or current "
                "absence; it cannot confirm intermediate history.",
            )
        if self.cannot_confirm_by_final_state:
            names = "; ".join(
                identity.describe()
                for identity in self.cannot_confirm_by_final_state
            )
            parts.append(
                "Final-state rescan cannot prove that these deletion events "
                f"were delivered: {names}.",
            )
        if self.generation_history_required:
            names = ", ".join(
                f"{bucket}/{obj}"
                for bucket, obj in self.generation_history_required
            )
            parts.append(
                "Per-object generation order is required for: "
                f"{names}.",
            )
        return " ".join(parts)


RecordLike = Union[NotificationEvent, Mapping[str, Any]]
Batch = Union[RecordLike, Iterable[RecordLike]]


class NotificationReader:
    """Identity based, per-object-generation notification reader.

    ``source`` is anything which yields ``{"Records": [...]}`` envelopes, such
    as :class:`minio.models.EventIterable`, or it can be ``None`` when the
    caller drives the reader through :meth:`feed`.

    Duplicate identities are retained once.  A duplicate delivery never returns
    a second event to the caller and never resets an acknowledgement.  Pending
    events are scoped to their own object, so an unresolved deletion cannot
    hold back an event which has independent evidence.
    """

    def __init__(
            self,
            source: Optional[Iterable[Any]] = None,
            *,
            evidence: Optional[EventEvidenceProvider] = None,
    ):
        self._source = source
        self._evidence = evidence
        self._records: dict[tuple[Any, ...], _Record] = {}
        self._aliases: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        self._objects: dict[tuple[str, str], _ObjectState] = {}
        self._gaps: list[ReplayGapReport] = []

    def __iter__(self) -> Iterator[NotificationEvent]:
        return self.iter_events()

    def iter_events(self) -> Iterator[NotificationEvent]:
        """Yield each real event once as the source produces envelopes."""
        if self._source is None:
            return
        for batch in self._source:
            yield from self.feed(batch)

    def feed(self, batch: Batch) -> list[NotificationEvent]:
        """Consume one envelope, one record, or a group of records."""
        if isinstance(batch, NotificationEvent):
            records: Iterable[RecordLike] = [batch]
        elif isinstance(batch, Mapping):
            envelope = batch.get("Records")
            if isinstance(envelope, Mapping):
                records = [envelope]
            elif (
                    isinstance(envelope, Iterable)
                    and not isinstance(envelope, (str, bytes))
            ):
                records = envelope
            else:
                records = [batch]
        elif isinstance(batch, (str, bytes)):
            raise TypeError("event batch must not be a string")
        else:
            records = batch

        delivered = []
        for record in records:
            event = (
                record if isinstance(record, NotificationEvent)
                else NotificationEvent.from_record(record)
            )
            accepted = self._offer(event)
            if accepted is not None:
                delivered.append(accepted)
        return delivered

    def _object_state(self, event: NotificationEvent) -> _ObjectState:
        return self._objects.setdefault(
            (event.bucket_name, event.object_name),
            _ObjectState(),
        )

    def _offer(self, event: NotificationEvent) -> Optional[NotificationEvent]:
        key = event.identity.key
        existing = self._records.get(self._aliases.get(key, key))
        if existing is not None:
            existing.event = self._merge_event(existing.event, event)
            existing.deliveries += 1
            return None

        equivalent = self._equivalent_key(event)
        if equivalent is not None:
            existing = self._records[equivalent]
            existing.event = self._merge_event(existing.event, event)
            existing.deliveries += 1
            self._aliases[key] = equivalent
            return None

        self._records[key] = _Record(event=event, deliveries=1)
        state = self._object_state(event)
        generation = event.generation
        if generation is not None and (
                state.highest_generation is None
                or generation > state.highest_generation
        ):
            state.highest_generation = generation
            state.highest_deleted = event.is_delete
        return event

    def _equivalent_key(
            self, event: NotificationEvent,
    ) -> Optional[tuple[Any, ...]]:
        """Find an already known record for the same real event.

        A reconnect can change which optional marker a record carries, for
        example generation on the first delivery and only version id on the
        replay.  Equality of any object-scoped marker is enough, provided the
        object and event name match.
        """
        if event.event_id:
            return None
        for key, record in self._records.items():
            other = record.event
            if (
                    other.bucket_name != event.bucket_name
                    or other.object_name != event.object_name
                    or other.event_name != event.event_name
            ):
                continue
            for marker in ("generation", "version_id", "sequencer"):
                left = getattr(event, marker)
                right = getattr(other, marker)
                if left is None or right is None:
                    continue
                if left == right:
                    return key
                break
            else:
                if (
                        event.event_time
                        and other.event_time
                        and event.event_time == other.event_time
                ):
                    return key
        return None

    @staticmethod
    def _merge_event(
            known: NotificationEvent, incoming: NotificationEvent,
    ) -> NotificationEvent:
        """Fill missing markers on the known event from the replay."""
        return replace(
            known,
            generation=_first(known.generation, incoming.generation),
            version_id=_first(known.version_id, incoming.version_id),
            sequencer=_first(known.sequencer, incoming.sequencer),
            event_id=_first(known.event_id, incoming.event_id),
            event_time=_first(known.event_time, incoming.event_time),
        )

    def _find(self, identity: EventIdentity) -> Optional[_Record]:
        """Find a record by exact key, alias, or merged event identity."""
        key = identity.key
        record = self._records.get(key)
        if record is not None:
            return record
        key = self._aliases.get(key, key)
        record = self._records.get(key)
        if record is not None:
            return record
        for candidate in self._records.values():
            if candidate.event.identity.key == identity.key:
                return candidate
        equivalent = self._equivalent_key(self._event_from_identity(identity))
        if equivalent is not None:
            return self._records.get(equivalent)
        return None

    def ack(self, event: Union[NotificationEvent, EventIdentity]) -> bool:
        """Acknowledge one real event.

        Returns ``True`` when the acknowledgement advances the object's current
        generation.  Late events are still acknowledgable, but an older or
        equal generation never moves the current generation backwards.
        """
        identity = self._identity(event)
        record = self._find(identity)
        if record is None:
            record = _Record(
                event=self._event_from_identity(identity),
                deliveries=0,
            )
            self._records[identity.key] = record
        if record.state in (
                EventState.ACKED,
                EventState.SUPERSEDED,
                EventState.CONFLICT,
        ):
            return False
        record.acknowledgements += 1
        record.state = EventState.ACKED
        record.reason = None
        return self._advance_current(record)

    def defer(
            self,
            event: Union[NotificationEvent, EventIdentity],
            reason: str = "evidence required",
    ) -> None:
        """Mark an event pending without affecting any other object."""
        identity = self._identity(event)
        record = self._find(identity)
        if record is None:
            record = _Record(
                event=self._event_from_identity(identity),
                deliveries=0,
            )
            self._records[identity.key] = record
        if record.state in (EventState.ACKED, EventState.SUPERSEDED):
            return
        record.state = EventState.PENDING
        record.reason = reason
        self._object_state(record.event)

    def status(
            self, event: Union[NotificationEvent, EventIdentity],
    ) -> Optional[EventState]:
        """Return the ledger state of an identity, or ``None`` if unknown."""
        record = self._find(self._identity(event))
        return record.state if record is not None else None

    def reason(
            self, event: Union[NotificationEvent, EventIdentity],
    ) -> Optional[str]:
        """Return the reason recorded for a pending/superseded event."""
        record = self._find(self._identity(event))
        return record.reason if record is not None else None

    def delivery_count(
            self, event: Union[NotificationEvent, EventIdentity],
    ) -> int:
        """How many envelopes delivered this identity."""
        record = self._find(self._identity(event))
        return record.deliveries if record is not None else 0

    def acknowledgement_count(
            self, event: Union[NotificationEvent, EventIdentity],
    ) -> int:
        """How many times this identity was acknowledged downstream."""
        record = self._find(self._identity(event))
        return record.acknowledgements if record is not None else 0

    @property
    def pending_events(self) -> tuple[NotificationEvent, ...]:
        """Events waiting for evidence."""
        return tuple(
            record.event for record in self._records.values()
            if record.state == EventState.PENDING
        )

    @property
    def unacknowledged_events(self) -> tuple[NotificationEvent, ...]:
        """Events delivered or pending which have not been acknowledged."""
        return tuple(
            record.event for record in self._records.values()
            if record.state in (EventState.DELIVERED, EventState.PENDING)
        )

    @property
    def acknowledged_events(self) -> tuple[NotificationEvent, ...]:
        """Events acknowledged by downstream."""
        return tuple(
            record.event for record in self._records.values()
            if record.state == EventState.ACKED
        )

    @property
    def gaps(self) -> tuple[ReplayGapReport, ...]:
        """Recorded replay gaps."""
        return tuple(self._gaps)

    def current_generation(
            self, bucket_name: str, object_name: str,
    ) -> Optional[int]:
        """Current acknowledged generation of an object."""
        state = self._objects.get((bucket_name, object_name))
        return state.current_generation if state is not None else None

    def highest_generation(
            self, bucket_name: str, object_name: str,
    ) -> Optional[int]:
        """Highest generation observed, acknowledged or not."""
        state = self._objects.get((bucket_name, object_name))
        return state.highest_generation if state is not None else None

    def is_deleted(self, bucket_name: str, object_name: str) -> bool:
        """Whether the current acknowledged generation is a tombstone."""
        state = self._objects.get((bucket_name, object_name))
        return state.current_deleted if state is not None else False

    def _advance_current(
            self,
            record: _Record,
            *,
            generation: Optional[int] = None,
            deleted: Optional[bool] = None,
    ) -> bool:
        event = record.event
        generation = event.generation if generation is None else generation
        if generation is None:
            return False
        deleted = event.is_delete if deleted is None else deleted
        state = self._object_state(event)
        if state.current_generation is None:
            state.current_generation = generation
            state.current_deleted = deleted
            return True
        if generation > state.current_generation:
            state.current_generation = generation
            state.current_deleted = deleted
            return True
        if generation == state.current_generation:
            if state.current_deleted == deleted:
                return False
            record.state = EventState.CONFLICT
            record.reason = (
                "create and delete share the current generation "
                f"{generation}; current state is unchanged"
            )
        return False

    def reconcile(
            self,
            *,
            events: Optional[Iterable[
                Union[NotificationEvent, EventIdentity]]] = None,
    ) -> ReconcileReport:
        """Reconcile unacknowledged events with service evidence.

        Event-id evidence is authoritative when available.  Otherwise the
        object's generation order decides, per object.  Failure or absence of
        evidence for one object never prevents another object from being
        reconciled.
        """
        if events is None:
            targets = list(self.unacknowledged_events)
        else:
            targets = [
                self._event_from_identity(self._identity(event))
                if isinstance(event, EventIdentity) else event
                for event in events
            ]

        confirmed: list[EventIdentity] = []
        superseded: list[EventIdentity] = []
        unconfirmed: list[EventIdentity] = []

        for event in targets:
            outcome = self._reconcile_event(event)
            if outcome == EventState.ACKED:
                confirmed.append(event.identity)
            elif outcome == EventState.SUPERSEDED:
                superseded.append(event.identity)
            else:
                unconfirmed.append(event.identity)

        return ReconcileReport(
            confirmed=tuple(confirmed),
            superseded=tuple(superseded),
            unconfirmed=tuple(unconfirmed),
        )

    def _reconcile_event(self, event: NotificationEvent) -> EventState:
        record = self._find(event.identity)
        if record is None:
            record = _Record(event=event, deliveries=0)
            self._records[event.identity.key] = record
        if record.state in (EventState.ACKED, EventState.SUPERSEDED):
            return record.state

        evidence = self._event_evidence(event)
        if evidence is not None:
            if evidence.applied:
                generation = _first(
                    evidence.generation,
                    event.generation,
                )
                deleted = (
                    event.is_delete
                    if evidence.deleted is None else evidence.deleted
                )
                self._confirm(record, _as_generation(generation), deleted)
                return EventState.ACKED
            if evidence.superseded_by is not None:
                self._mark_superseded(record, evidence.superseded_by)
                return EventState.SUPERSEDED
            if (
                    evidence.generation is not None
                    and evidence.generation != event.generation
            ):
                self._mark_superseded(record, evidence.generation)
                return EventState.SUPERSEDED

        history = self._object_history(event)
        if history is None:
            return EventState.PENDING

        resolved = self._resolve_event(event, history)
        if resolved.generation is None:
            return EventState.PENDING

        newest = max(
            (entry.generation for entry in history),
            default=None,
        )
        if newest is not None and resolved.generation < newest:
            self._mark_superseded(record, newest)
            return EventState.SUPERSEDED

        matches = [
            entry for entry in history
            if entry.generation == resolved.generation
        ]
        if not matches:
            return EventState.PENDING

        if resolved.is_delete:
            if any(entry.is_delete_marker for entry in matches):
                self._confirm(record, resolved.generation, True)
                return EventState.ACKED
            return EventState.PENDING
        if any(not entry.is_delete_marker for entry in matches):
            self._confirm(record, resolved.generation, False)
            return EventState.ACKED
        return EventState.PENDING

    def _event_evidence(
            self, event: NotificationEvent,
    ) -> Optional[EventEvidence]:
        if self._evidence is None or not event.event_id:
            return None
        try:
            return self._evidence.event_evidence(
                bucket_name=event.bucket_name,
                event_id=event.event_id,
            )
        except Exception:  # pylint: disable=broad-except
            return None

    def _object_history(
            self, event: NotificationEvent,
    ) -> Optional[Sequence[ObjectGeneration]]:
        if self._evidence is None:
            return None
        try:
            return self._evidence.object_generations(
                bucket_name=event.bucket_name,
                object_name=event.object_name,
            )
        except Exception:  # pylint: disable=broad-except
            return None

    @staticmethod
    def _resolve_event(
            event: NotificationEvent,
            history: Sequence[ObjectGeneration],
    ) -> NotificationEvent:
        if event.generation is not None:
            return event
        for entry in history:
            if (
                    event.event_id
                    and entry.event_id == event.event_id
            ) or (
                    event.version_id
                    and entry.version_id == event.version_id
            ) or (
                    event.sequencer
                    and entry.sequencer == event.sequencer
            ):
                return replace(event, generation=entry.generation)
        return event

    def _confirm(
            self,
            record: _Record,
            generation: Optional[int],
            deleted: bool,
    ) -> None:
        was_acked = record.state == EventState.ACKED
        record.state = EventState.ACKED
        record.reason = None
        if not was_acked:
            record.acknowledgements += 1
        if generation is None:
            return
        if record.event.generation != generation:
            record.event = replace(record.event, generation=generation)
        state = self._object_state(record.event)
        if (
                state.highest_generation is None
                or generation > state.highest_generation
        ):
            state.highest_generation = generation
            state.highest_deleted = deleted
        self._advance_current(record, generation=generation, deleted=deleted)

    def _mark_superseded(
            self, record: _Record, by_generation: int,
    ) -> None:
        record.state = EventState.SUPERSEDED
        record.reason = f"superseded by generation {by_generation}"
        state = self._object_state(record.event)
        if (
                state.highest_generation is None
                or by_generation > state.highest_generation
        ):
            state.highest_generation = by_generation

    def note_replay_gap(
            self,
            *,
            reason: str = "the queue no longer retains replay history",
            objects: Optional[Iterable[tuple[str, str]]] = None,
    ) -> ReplayGapReport:
        """Record that replay history is unavailable and classify recovery.

        A final-state rescan can establish the current generation/presence of
        an object.  It cannot establish that an intermediate deletion was
        delivered; that needs the per-object generation order, tombstones
        included.
        """
        affected: set[tuple[str, str]] = set()
        if objects is not None:
            affected.update(objects)
        for record in self._records.values():
            if record.state in (EventState.DELIVERED, EventState.PENDING):
                affected.add((
                    record.event.bucket_name,
                    record.event.object_name,
                ))

        unresolved = self.unacknowledged_events
        deletion_objects = {
            (event.bucket_name, event.object_name)
            for event in unresolved if event.is_delete
        }
        deletions = tuple(
            event.identity for event in unresolved if event.is_delete
        )
        rescan_objects = tuple(sorted(affected))
        report = ReplayGapReport(
            reason=reason,
            affected_objects=rescan_objects,
            unresolved_events=tuple(event.identity for event in unresolved),
            rescan_objects=rescan_objects,
            final_state_can_confirm=rescan_objects,
            generation_history_required=tuple(sorted(deletion_objects)),
            cannot_confirm_by_final_state=deletions,
        )
        self._gaps.append(report)
        return report

    @staticmethod
    def _identity(
            event: Union[NotificationEvent, EventIdentity],
    ) -> EventIdentity:
        return event.identity if isinstance(
            event, NotificationEvent,
        ) else event

    @staticmethod
    def _event_from_identity(identity: EventIdentity) -> NotificationEvent:
        return NotificationEvent(
            bucket_name=identity.bucket_name,
            object_name=identity.object_name,
            event_name=identity.event_name,
            generation=identity.generation,
            version_id=identity.version_id,
            sequencer=identity.sequencer,
            event_id=identity.event_id,
            event_time=identity.event_time,
        )
