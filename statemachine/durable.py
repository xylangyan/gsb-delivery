"""Durable execution for :class:`~statemachine.statemachine.StateChart`.

A ``StateChart`` normally lives in memory: its configuration and its two event queues are
gone when the process exits.  ``DurableStateChart`` records a checkpoint at every safe step
boundary, so a new instance can resume at the position the old one actually reached instead
of starting over at the initial state.

Three pieces work together:

- **Checkpoints** capture the configuration, the pending internal and external triggers, the
  live invocation generations and the effect receipts.  They are taken after each microstep
  and after each queued trigger, which is exactly the set of points where the machine is
  between two atomic steps.
- **Effects** are business writes with a stable id.  A durable chart keeps a
  :class:`Journal` of accepted receipts: the same id is accepted once, a repeated call
  reports ``replayed`` and returns the receipt (and ticket) already written.
- **Generations** tell the rounds of a reused invoke id apart.  When a state is exited and
  entered again, its declared invoke id starts the next round; a ``done.invoke`` left over
  from a retired round carries the old generation and is dropped rather than passing the
  new round.

Restoring follows the original run-to-completion order.  The pending *internal* triggers —
including the ``error.execution`` raised by an action that failed — are placed on the
internal queue and drained inside the first macrostep, before any pending external trigger
such as a review that was queued earlier can advance the chart to its next round.

Nothing here changes a plain ``StateChart``: the engine calls a no-op checkpoint hook on
every instance, and only ``DurableStateChart`` stores anything.
"""

import builtins
import logging
import pickle
import threading
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from time import monotonic
from typing import Any

from .event import BoundEvent
from .event_data import TriggerData
from .exceptions import InvalidDefinition
from .exceptions import InvalidStateValue
from .orderedset import OrderedSet
from .statemachine import StateChart

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1
"""The layout version of a checkpoint, so a restarted process can reject an old one."""


@dataclass(frozen=True)
class EffectReceipt:
    """A business write that a durable chart accepted once, keyed by its stable id."""

    key: str
    """The stable business id of the write; accepting it again is a replay."""

    ticket: Any = None
    """The ticket the write returned, reused on a replay instead of writing a second one."""

    generation: "int | None" = None
    """The invocation round that produced the write, when it belongs to one."""

    state_id: "str | None" = None
    """The state whose action accepted the write, for tracing it back to the call."""

    accepted: bool = True
    """Whether the write landed.  A receipt only exists for a landed write."""

    replayed: bool = False
    """Whether a repeated call returned this receipt instead of writing again."""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "ticket": self.ticket,
            "generation": self.generation,
            "state_id": self.state_id,
            "accepted": self.accepted,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EffectReceipt":
        return cls(
            key=data["key"],
            ticket=data.get("ticket"),
            generation=data.get("generation"),
            state_id=data.get("state_id"),
            accepted=data.get("accepted", True),
            replayed=data.get("replayed", False),
        )


class Journal:
    """Durable receipts of business writes, each accepted once by a stable id.

    The journal can live in the process that writes the effects (an in-memory instance
    shared by every machine of a run), or be restored from a checkpoint.  It is the
    authority for "was this write already received?".
    """

    def __init__(self, receipts: "dict[str, EffectReceipt] | None" = None):
        self._receipts: dict[str, EffectReceipt] = dict(receipts or {})
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        return key in self._receipts

    def __len__(self) -> int:
        return len(self._receipts)

    def received(self, key: str) -> bool:
        """Whether the write with this id has landed."""
        return key in self._receipts

    def get(self, key: str) -> "EffectReceipt | None":
        """The receipt of a landed write, or ``None`` when it never landed."""
        return self._receipts.get(key)

    def keys(self) -> "list[str]":
        return list(self._receipts)

    def accept(
        self,
        key: str,
        ticket: Any = None,
        generation: "int | None" = None,
        state_id: "str | None" = None,
    ) -> "tuple[EffectReceipt, bool]":
        """Accept a write once.

        Returns:
            The receipt and whether this call created it.  ``False`` means the write had
            already landed: the returned receipt keeps the original ticket, so the caller
            reuses it instead of writing again.
        """
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                return existing, False
            receipt = EffectReceipt(
                key=key,
                ticket=ticket,
                generation=generation,
                state_id=state_id,
            )
            self._receipts[key] = receipt
            return receipt, True

    def merge_missing(self, other: "dict[str, EffectReceipt]"):
        """Adopt receipts this journal does not have, without overwriting its own.

        A checkpoint is a fallback: a journal that already holds a write is the authority.
        """
        with self._lock:
            for key, receipt in other.items():
                self._receipts.setdefault(key, receipt)

    def to_dict(self) -> dict:
        with self._lock:
            return {key: receipt.to_dict() for key, receipt in self._receipts.items()}

    @classmethod
    def from_dict(cls, data: "dict[str, dict] | None") -> "Journal":
        return cls({key: EffectReceipt.from_dict(value) for key, value in (data or {}).items()})


class CheckpointStore:
    """Keeps the latest checkpoint of each durable run.

    An in-memory implementation is enough for a restart simulated in one process; pass a
    :class:`PickleFileStore` to survive an actual process exit.
    """

    def save(self, run_id: str, checkpoint: dict):
        raise NotImplementedError

    def load(self, run_id: str) -> "dict | None":
        raise NotImplementedError

    def clear(self, run_id: str):
        raise NotImplementedError


class MemoryCheckpointStore(CheckpointStore):
    """In-memory store, keyed by run id."""

    def __init__(self):
        self._checkpoints: dict[str, dict] = {}

    def save(self, run_id: str, checkpoint: dict):
        self._checkpoints[run_id] = checkpoint

    def load(self, run_id: str) -> "dict | None":
        return self._checkpoints.get(run_id)

    def clear(self, run_id: str):
        self._checkpoints.pop(run_id, None)


class PickleFileStore(CheckpointStore):
    """One pickle file per run, written atomically, so an exit loses at most the live step."""

    def __init__(self, directory: "str | Path"):
        self.directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in run_id)
        return self.directory / f"{safe}.checkpoint"

    def save(self, run_id: str, checkpoint: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(checkpoint))
        tmp.replace(path)

    def load(self, run_id: str) -> "dict | None":
        path = self._path(run_id)
        if not path.exists():
            return None
        loaded: dict = pickle.loads(path.read_bytes())
        return loaded

    def clear(self, run_id: str):
        self._path(run_id).unlink(missing_ok=True)


@dataclass
class FailureRecord:
    """What an ``error.execution`` trigger carried, kept across a restart.

    An exception object cannot be pickled reliably, so the checkpoint keeps its type and
    message and rebuilds it on resume.  A handler can inspect ``error_type`` or ``message``.
    """

    type_name: str
    message: str

    def to_dict(self) -> dict:
        return {"type": self.type_name, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict) -> "FailureRecord":
        return cls(type_name=data["type"], message=data["message"])


class RestoredExecutionError(Exception):
    """The error rebuilt from a checkpoint when the original exception cannot be restored.

    ``error_type`` and ``message`` preserve what the failed action reported.
    """

    def __init__(self, type_name: str, message: str):
        super().__init__(message)
        self.type_name = type_name
        self.error_type = type_name
        self.message = message


_BUILTIN_ERRORS: dict[str, type[Exception]] = {
    name: value
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, Exception)
}


def _rebuild_error(record: FailureRecord) -> Exception:
    cls = _BUILTIN_ERRORS.get(record.type_name)
    if cls is not None:
        return cls(record.message)
    return RestoredExecutionError(record.type_name, record.message)


def _record_error(error: BaseException) -> FailureRecord:
    return FailureRecord(type_name=type(error).__name__, message=str(error))


def _encode_kwargs(kwargs: dict) -> dict:
    """Replace exception values with records that survive a checkpoint."""
    encoded = {}
    for key, value in kwargs.items():
        if isinstance(value, BaseException):
            encoded[key] = {"__durable_error__": _record_error(value).to_dict()}
        else:
            encoded[key] = value
    return encoded


def _decode_kwargs(kwargs: dict) -> dict:
    decoded = {}
    for key, value in kwargs.items():
        if isinstance(value, dict) and "__durable_error__" in value:
            record = FailureRecord.from_dict(value["__durable_error__"])
            decoded[key] = _rebuild_error(record)
        else:
            decoded[key] = value
    return decoded


@dataclass
class QueueRecord:
    """One trigger waiting in a queue when the checkpoint was taken."""

    event: "str | None"
    args: tuple
    kwargs: dict
    generation: "int | None"
    send_id: "str | None"
    remaining: float
    """Seconds the trigger still had to wait when the checkpoint was taken.

    A ``TriggerData`` due time is on the ``monotonic`` clock, which is relative to this
    process (and to the boot).  Recording the remaining delay instead of the absolute time
    lets a restarted process re-anchor it on its own clock.
    """
    sequence: int

    @classmethod
    def from_trigger(cls, trigger_data: TriggerData) -> "QueueRecord":
        return cls(
            event=str(trigger_data.event) if trigger_data.event is not None else None,
            args=tuple(trigger_data.args),
            kwargs=_encode_kwargs(dict(trigger_data.kwargs)),
            generation=trigger_data.generation,
            send_id=trigger_data.send_id,
            remaining=max(0.0, trigger_data.execution_time - monotonic()),
            sequence=trigger_data.sequence,
        )

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "args": list(self.args),
            "kwargs": self.kwargs,
            "generation": self.generation,
            "send_id": self.send_id,
            "remaining": self.remaining,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueRecord":
        return cls(
            event=data["event"],
            args=tuple(data.get("args", ())),
            kwargs=dict(data.get("kwargs", {})),
            generation=data.get("generation"),
            send_id=data.get("send_id"),
            remaining=data.get("remaining", 0.0),
            sequence=data.get("sequence", 0),
        )


@dataclass
class Checkpoint:
    """Everything a restarted chart needs to continue at the recorded step."""

    configuration: list
    history_values: dict = field(default_factory=dict)
    internal: list = field(default_factory=list)
    external: list = field(default_factory=list)
    invoke_generations: dict = field(default_factory=dict)
    """Current generation of each live invocation id.

    The handlers themselves are not resumable, so a restored run knows only their ids and
    generations.  A completion carrying a live id and generation is for the restored round;
    one carrying a retired generation is dropped.
    """
    receipts: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    macrostep: int = 0
    microstep: int = 0
    running: bool = True
    """Whether the run had not yet reached a final state when the checkpoint was taken."""
    version: int = CHECKPOINT_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "configuration": list(self.configuration),
            "history_values": {key: list(value) for key, value in self.history_values.items()},
            "internal": [item.to_dict() for item in self.internal],
            "external": [item.to_dict() for item in self.external],
            "invoke_generations": dict(self.invoke_generations),
            "receipts": dict(self.receipts),
            "failures": [item.to_dict() for item in self.failures],
            "macrostep": self.macrostep,
            "microstep": self.microstep,
            "running": self.running,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        version = data.get("version", CHECKPOINT_VERSION)
        if version != CHECKPOINT_VERSION:
            raise InvalidStateValue(
                data.get("version"),
                f"Unsupported checkpoint version {version}; expected {CHECKPOINT_VERSION}.",
            )
        return cls(
            configuration=list(data.get("configuration", [])),
            history_values={
                key: list(value) for key, value in data.get("history_values", {}).items()
            },
            internal=[QueueRecord.from_dict(item) for item in data.get("internal", [])],
            external=[QueueRecord.from_dict(item) for item in data.get("external", [])],
            invoke_generations=dict(data.get("invoke_generations", {})),
            receipts=dict(data.get("receipts", {})),
            failures=[FailureRecord.from_dict(item) for item in data.get("failures", [])],
            macrostep=data.get("macrostep", 0),
            microstep=data.get("microstep", 0),
            running=data.get("running", True),
            version=version,
        )


class DurableStateChart(StateChart):
    """A :class:`StateChart` that can resume from the step it actually reached.

    Args:
        checkpoint_store: Where the latest checkpoint is kept.  Default: a fresh
            :class:`MemoryCheckpointStore` (only useful within one process).
        journal: The durable receipts of business writes.  Default: a fresh
            :class:`Journal`.
        run_id: Names the checkpoint inside the store.  Default: the machine class name.

    Every other argument is forwarded to ``StateChart``.

    On construction the chart looks for a checkpoint of ``run_id``.  Without one it enters
    the initial state exactly as a plain ``StateChart`` does.  With one it adopts the
    recorded configuration, generation and queues, skips the initial entry (so entry
    effects do not run twice) and lets the engine continue the run-to-completion loop.
    """

    def __init__(
        self,
        model: Any = None,
        state_field: str = "state",
        start_value: Any = None,
        listeners: "list[object] | None" = None,
        *,
        checkpoint_store: "CheckpointStore | None" = None,
        journal: "Journal | None" = None,
        run_id: "str | None" = None,
        **kwargs: Any,
    ):
        self.checkpoint_store = checkpoint_store or MemoryCheckpointStore()
        self.journal = journal or Journal()
        self.run_id = run_id or type(self).__name__
        self._durable_lock = threading.RLock()
        self._checkpointing = False
        self._restored = False
        self._last_checkpoint: "Checkpoint | None" = None
        self.resume_report: dict[str, Any] = {}
        """What the last restore found: pending failures, landed receipts and queues."""
        super().__init__(
            model=model,
            state_field=state_field,
            start_value=start_value,
            listeners=listeners,
            **kwargs,
        )

    # -- Effects ---------------------------------------------------------------

    def _resolve_attribution(
        self, generation: "int | None", state_id: "str | None"
    ) -> "tuple[int | None, str | None]":
        """Fill in missing attribution from the trigger whose callback is running."""
        trigger = getattr(self._engine, "_current_trigger", None)
        if trigger is None:
            return generation, state_id
        if generation is None:
            generation = trigger.generation
        if state_id is None:
            state_id = trigger.kwargs.get("_invokeid")
        return generation, state_id

    def accept_effect(
        self,
        key: str,
        ticket: Any = None,
        *,
        generation: "int | None" = None,
        state_id: "str | None" = None,
    ) -> EffectReceipt:
        """Accept a business write once by its stable id.

        The first call records the receipt and the ticket.  A call with an id already in the
        journal does not write again: it returns the original receipt and ticket, so a
        retried ``after`` callback leaves the same slip as the first attempt.

        Without explicit ``generation``/``state_id`` the attribution is taken from the
        trigger whose callback is running, so a receipt can be traced back to the call —
        and to the invocation round — that produced it.
        """
        generation, state_id = self._resolve_attribution(generation, state_id)
        receipt, created = self.journal.accept(
            key, ticket=ticket, generation=generation, state_id=state_id
        )
        if not created:
            logger.debug("Durable effect %r replayed; ticket %r reused", key, receipt.ticket)
        return receipt

    def receive_effect(
        self,
        key: str,
        ticket: Any = None,
        *,
        generation: "int | None" = None,
        state_id: "str | None" = None,
    ) -> "tuple[EffectReceipt, bool]":
        """Accept a write and report whether this call created the receipt.

        ``(receipt, True)`` means the write landed now; ``(receipt, False)`` means it had
        already landed and the original ticket is being reused.
        """
        generation, state_id = self._resolve_attribution(generation, state_id)
        return self.journal.accept(key, ticket=ticket, generation=generation, state_id=state_id)

    def has_effect(self, key: str) -> bool:
        """Whether the business write with this stable id already landed."""
        return self.journal.received(key)

    def live_rounds(self) -> "dict[str, int]":
        """The generation of each live invocation, keyed by its invoke id.

        The round tells a write, and a ``done.invoke`` notification, which call of a reused
        invoke id it belongs to.  A round whose state has been exited is not listed.
        """
        return self._engine._invoke_manager.live_generations()

    # -- Checkpointing ---------------------------------------------------------

    def halt(self):
        """Ask the engine to stop at the next step boundary, keeping both queues.

        A restart from the checkpoint continues at exactly that boundary: the microstep
        already finished, the triggers it queued still pending.
        """
        self._engine.halt_requested = True
        self.checkpoint()

    def checkpoint(self) -> Checkpoint:
        """Record the current step.  Safe to call from a callback, too."""
        with self._durable_lock:
            checkpoint = self._build_checkpoint()
            self.checkpoint_store.save(self.run_id, checkpoint.to_dict())
            self._last_checkpoint = checkpoint
            return checkpoint

    def _checkpoint_after_step(self):
        """Persist the step boundary the engine just reached.

        Reentrant calls are dropped: a checkpoint runs no callbacks, but setting the model
        state can reach user code that sends an event, and a second checkpoint is not
        needed for a step that already recorded itself.
        """
        if self._checkpointing:
            return
        self._checkpointing = True
        try:
            self.checkpoint()
        finally:
            self._checkpointing = False

    def _restore_pending_checkpoint(self) -> bool:
        raw = self.checkpoint_store.load(self.run_id)
        if raw is None:
            return False
        self._apply_checkpoint(Checkpoint.from_dict(raw))
        return True

    def resume(self) -> Any:
        """Continue the run from the checkpoint, settling the pending internal events first.

        The configuration, the invocation generations, the landed receipts and the queues
        are already in place.  Running the macrostep without taking an external trigger
        processes the failure the exit interrupted (``error.execution`` and the eventless
        transitions it enables) before any external trigger queued earlier — a review, say —
        can advance the run to its next round.  The remaining external triggers stay in the
        queue, in the order they were sent, for the caller to take with ``send()`` or another
        normal loop turn.

        Raises:
            InvalidDefinition: When there is no checkpoint to resume from.
        """
        if not self._restored:
            raise InvalidDefinition(f"Durable run {self.run_id!r} has no checkpoint to resume.")
        return self.settle_internal()

    def _build_checkpoint(self) -> Checkpoint:
        engine = self._engine
        configuration = list(self._config.values)
        history_values = {
            key: [state.value for state in states] for key, states in self.history_values.items()
        }
        internal = [
            QueueRecord.from_trigger(trigger) for trigger in engine.internal_queue.snapshot()
        ]
        # A run that has reached a final state drops what its external queue held, so the
        # checkpoint records none of it; restoring it would let a closed run take it.
        external = (
            [QueueRecord.from_trigger(trigger) for trigger in engine.external_queue.snapshot()]
            if engine.running
            else []
        )

        failures = [
            FailureRecord.from_dict(record.kwargs["error"]["__durable_error__"])
            for record in internal
            if isinstance(record.kwargs.get("error"), dict)
            and "__durable_error__" in record.kwargs["error"]
        ]

        return Checkpoint(
            configuration=configuration,
            history_values=history_values,
            internal=internal,
            external=external,
            invoke_generations=engine._invoke_manager.live_generations(),
            receipts=self.journal.to_dict(),
            failures=failures,
            macrostep=engine._macrostep_count,
            microstep=engine._microstep_count,
            running=engine.running,
        )

    # -- Restore ---------------------------------------------------------------

    def _apply_checkpoint(self, checkpoint: Checkpoint):
        self._checkpointing = True
        try:
            states = []
            for value in checkpoint.configuration:
                state = self.states_map.get(value)
                if state is None:
                    raise InvalidStateValue(value)
                states.append(state)
            self.configuration = OrderedSet(states)

            self.history_values = {
                key: [self.states_map[value] for value in values]
                for key, values in checkpoint.history_values.items()
            }

            self.journal.merge_missing(
                {key: EffectReceipt.from_dict(value) for key, value in checkpoint.receipts.items()}
            )
            self._engine._invoke_manager.restore_generations(checkpoint.invoke_generations)

            internal = [
                self._restore_trigger(record, internal=True) for record in checkpoint.internal
            ]
            external = [
                self._restore_trigger(record, internal=False) for record in checkpoint.external
            ]
            for trigger in internal:
                self._engine.internal_queue.put(trigger)
            for trigger in external:
                self._engine.external_queue.put(trigger)

            self._engine._macrostep_count = checkpoint.macrostep
            self._engine._microstep_count = checkpoint.microstep
            if not checkpoint.running:
                # A run that had already completed stays completed; a new instance must not
                # claim it can take another trigger.
                self._engine.running = False
            self._restored = True
            self._last_checkpoint = checkpoint
            self.resume_report = {
                "pending_failures": [record.to_dict() for record in checkpoint.failures],
                "landed_effects": list(checkpoint.receipts),
                "internal_pending": [record.event for record in checkpoint.internal],
                "external_pending": [record.event for record in checkpoint.external],
            }
            logger.debug(
                "Durable run %r resumed: failures=%s internal=%s external=%s",
                self.run_id,
                self.resume_report["pending_failures"],
                self.resume_report["internal_pending"],
                self.resume_report["external_pending"],
            )
        finally:
            self._checkpointing = False

    def _restore_trigger(self, record: QueueRecord, internal: bool) -> TriggerData:
        event = self._restore_event(record.event)
        trigger = TriggerData(
            machine=self,
            event=event,
            send_id=record.send_id,
            generation=record.generation,
            args=record.args,
            kwargs=_decode_kwargs(record.kwargs),
        )
        # The trigger keeps the delay it had left, re-anchored on this process's clock;
        # its creation order still breaks ties with the triggers queued around it.
        trigger.execution_time = monotonic() + record.remaining
        trigger.sequence = record.sequence
        return trigger

    def _restore_event(self, event_id: "str | None"):
        if event_id is None:
            return None
        for declared in self.__class__._events:
            if declared.id == event_id:
                return BoundEvent(
                    id=declared.id,
                    name=declared.name,
                    delay=declared.delay,
                    internal=declared.internal,
                    _sm=self,
                )
        return BoundEvent(id=event_id, name=event_id, internal=False, _sm=self)

    # -- Introspection ---------------------------------------------------------

    @property
    def is_restored(self) -> bool:
        """Whether this instance resumed from a checkpoint at construction."""
        return self._restored

    def snapshot(self) -> dict:
        """The current checkpoint as a plain dict, for inspection or manual storage."""
        return self._build_checkpoint().to_dict()

    def clear_checkpoint(self):
        """Forget the stored checkpoint of this run, so a new instance starts fresh."""
        self.checkpoint_store.clear(self.run_id)


def load_checkpoint(store: CheckpointStore, run_id: str) -> "Checkpoint | None":
    """Load a checkpoint from a store, without instantiating a machine."""
    raw = store.load(run_id)
    return Checkpoint.from_dict(raw) if raw is not None else None


__all__ = [
    "CHECKPOINT_VERSION",
    "Checkpoint",
    "CheckpointStore",
    "DurableStateChart",
    "EffectReceipt",
    "FailureRecord",
    "Journal",
    "MemoryCheckpointStore",
    "PickleFileStore",
    "QueueRecord",
    "RestoredExecutionError",
    "load_checkpoint",
]
