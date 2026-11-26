# coding:utf-8
"""Append-only attempt journal and reconciliation.

The journal stores facts, never decisions that can be recomputed from a
different configuration.  In particular it records the configuration snapshot
of every retry "run", the value/exception seen by each attempt, the wait that
was actually derived from it, the point in time at which each event handler
completed and the absolute wake-up time of a sleep that has really started.

Records are newline delimited JSON and are flushed before a retry loop is
allowed to continue.  A torn trailing line (a crash in the middle of an
append) is ignored on reload: it was never a durable receipt.

All times are taken from ``Journal.clock``.  The default is ``time.time`` but
tests and replay tooling can inject a manual clock.
"""
import datetime
import importlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

_REPR_MARKER = "__backoff_repr__"
_EXC_MARKER = "__backoff_exception__"
_BYTES_MARKER = "__backoff_bytes__"
_DATETIME_MARKER = "__backoff_datetime__"
_ITEMS_MARKER = "__backoff_items__"

BACKOFF_RECEIPT_KINDS = ("backoff_receipt",)
TERMINAL_RECEIPT_KINDS = ("success_receipt", "giveup_receipt")


class DurableResumeError(RuntimeError):
    """A recorded value could not be reconstructed faithfully."""


class LossyValue(object):
    """Placeholder for a value that was only stored as ``repr``."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    @property
    def lossy(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "LossyValue(%r)" % (self.value,)


def encode(value: Any) -> Any:
    """Convert ``value`` into something json can represent."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {_BYTES_MARKER: value.decode("latin-1")}
    if isinstance(value, datetime.datetime):
        return {_DATETIME_MARKER: value.isoformat()}
    if isinstance(value, BaseException):
        return encode_exception(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: encode(item) for key, item in value.items()}
        items = [[encode(k), encode(v)] for k, v in value.items()]
        return {_ITEMS_MARKER: items}
    return {_REPR_MARKER: repr(value)}


def decode(value: Any) -> Any:
    """Inverse of :func:`encode`, preferring explicit failure to pretending."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if _REPR_MARKER in value:
            return LossyValue(value[_REPR_MARKER])
        if _BYTES_MARKER in value:
            return value[_BYTES_MARKER].encode("latin-1")
        if _DATETIME_MARKER in value:
            return datetime.datetime.fromisoformat(value[_DATETIME_MARKER])
        if _ITEMS_MARKER in value:
            return {decode(k): decode(v) for k, v in value[_ITEMS_MARKER]}
        if _EXC_MARKER in value:
            return decode_exception(value)
        return {key: decode(item) for key, item in value.items()}
    return value


def encode_exception(exc: BaseException) -> Dict[str, Any]:
    return {
        _EXC_MARKER: True,
        "module": type(exc).__module__,
        "qualname": type(exc).__qualname__,
        "args": encode(exc.args),
        "repr": repr(exc),
    }


def decode_exception(record: Dict[str, Any]) -> BaseException:
    module_name = record.get("module", "builtins")
    qualname = record.get("qualname", "Exception")
    try:
        module = importlib.import_module(module_name)
        cls: Any = module
        for part in qualname.split("."):
            cls = getattr(cls, part)
        args = decode(record.get("args", ()))
        if isinstance(args, list):
            args = tuple(args)
        if not isinstance(args, tuple):
            args = (args,)
        return cls(*args)
    except Exception:
        return DurableResumeError(
            "could not reconstruct %s.%s from %s"
            % (module_name, qualname, record.get("repr"))
        )


def is_lossy(value: Any) -> bool:
    if isinstance(value, LossyValue):
        return True
    if isinstance(value, list):
        return any(is_lossy(item) for item in value)
    if isinstance(value, dict):
        return any(is_lossy(item) for item in value.values())
    return False


class AttemptId(str):
    """A stable attempt id string that also exposes its components.

    It behaves exactly like a string (and can therefore be used as a server
    lookup key), while ``key``, ``layer``, ``run`` and ``number`` remain
    available to hooks.
    """

    def __new__(cls, key: str, layer: str, run: int, number: int):
        value = "%s::%s::%s::%s" % (key, layer, run, number)
        obj = str.__new__(cls, value)
        obj.key = key
        obj.layer = layer
        obj.run_number = run
        obj.number = number
        return obj


def make_attempt_id(key: str, layer: str, run: int, attempt: int) -> AttemptId:
    return AttemptId(key, layer, run, attempt)


class Journal(object):
    """Durable local journal.

    Args:
        path: File to append to.  ``None`` keeps the journal in memory.
        clock: Zero-argument callable returning the current time in seconds.
        fsync: Force each record to disk before :meth:`append` returns.
        on_append: Hook called with the record after it was made durable.
        on_before_append: Hook called with ``(kind, event)`` just before the
            record is written.  Raising from either hook simulates a crash.
    """

    def __init__(self, path: Optional[str] = None, *,
                 clock=None, fsync: bool = True,
                 on_append=None, on_before_append=None) -> None:
        self.path = os.fspath(path) if path is not None else None
        self.clock = clock or time.time
        self.fsync = fsync
        self.on_append = on_append
        self.on_before_append = on_before_append
        self._lock = threading.RLock()
        self._events: List[Dict[str, Any]] = []
        self._seq = 0
        self._fh = None
        if self.path is not None:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            self._load()
            self._fh = open(self.path, "a", encoding="utf-8")

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or "seq" not in event:
                    continue
                self._events.append(event)
                self._seq = max(self._seq, int(event["seq"]) + 1)

    def append(self, kind: str, *, key: Optional[str] = None,
               layer: Optional[str] = None, run: Optional[int] = None,
               attempt: Optional[int] = None,
               attempt_id: Optional[str] = None,
               **fields: Any) -> Dict[str, Any]:
        with self._lock:
            event = {
                "seq": self._seq,
                "at": float(self.clock()),
                "kind": kind,
                "key": key,
                "layer": layer,
                "run": run,
                "attempt": attempt,
                "attempt_id": attempt_id,
            }
            event.update(fields)
            if self.on_before_append is not None:
                self.on_before_append(kind, event)
            self._seq += 1
            if self._fh is not None:
                self._fh.write(json.dumps(event, sort_keys=True) + "\n")
                self._fh.flush()
                if self.fsync:
                    os.fsync(self._fh.fileno())
            self._events.append(event)
        if self.on_append is not None:
            self.on_append(event)
        return event

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- reading ---------------------------------------------------------
    @property
    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def records(self, key: Optional[str] = None, *,
                layer: Optional[str] = None,
                run: Optional[int] = None,
                kind: Optional[str] = None,
                attempt_id: Optional[str] = None,
                attempt: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        return [
            event for event in events
            if (key is None or event.get("key") == key)
            and (layer is None or event.get("layer") == layer)
            and (run is None or event.get("run") == run)
            and (kind is None or event.get("kind") == kind)
            and (attempt_id is None or event.get("attempt_id") == attempt_id)
            and (attempt is None or event.get("attempt") == attempt)
        ]

    def keys(self) -> List[str]:
        return sorted({event["key"] for event in self.events
                       if event.get("key") is not None})

    def layers(self, key: Optional[str] = None) -> List[str]:
        return sorted({event["layer"] for event in self.records(key)
                       if event.get("layer") is not None})

    def latest_run(self, key: str, layer: str) -> Optional[int]:
        runs = [event.get("run") for event in self.records(key, layer=layer,
                                                            kind="run_start")
                if event.get("run") is not None]
        return max(runs) if runs else None

    # -- server acceptance receipts --------------------------------------
    def acknowledge(self, attempt_id: str, *, value: Any = None,
                    exception: Optional[BaseException] = None,
                    receipt: Any = None,
                    has_value: Optional[bool] = None,
                    key: Optional[str] = None,
                    layer: Optional[str] = None,
                    run: Optional[int] = None,
                    attempt: Optional[int] = None) -> Dict[str, Any]:
        """Record an acceptance receipt keyed by attempt id.

        This may be called at any time, including after later attempts have
        already run.  Reconciliation attaches it to the original attempt by
        id and flags it as late when applicable.

        ``has_value`` distinguishes "accepted with a known result of None"
        from "accepted, result must be looked up".  It defaults to
        ``value is not None``.
        """
        if exception is not None:
            has_value = False
        elif has_value is None:
            has_value = value is not None
        return self.append(
            "ack", key=key, layer=layer, run=run, attempt=attempt,
            attempt_id=attempt_id,
            has_value=bool(has_value),
            value=encode(value) if has_value else None,
            exception=(encode_exception(exception)
                       if exception is not None else None),
            receipt=encode(receipt),
        )

    def acknowledgement(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        for event in reversed(self.records(attempt_id=attempt_id, kind="ack")):
            return event
        return None

    def attempts(self, key: Optional[str] = None,
                 layer: Optional[str] = None,
                 run: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.records(key, layer=layer, run=run, kind="attempt_start")

    # -- reconciliation --------------------------------------------------
    def reconcile(self, key: Optional[str] = None,
                  layer: Optional[str] = None) -> "Reconciliation":
        return Reconciliation(self, key=key, layer=layer)

    def report(self, key: Optional[str] = None,
               layer: Optional[str] = None) -> Dict[str, Any]:
        return self.reconcile(key=key, layer=layer).to_dict()


def reconcile(journal, key: Optional[str] = None,
              layer: Optional[str] = None) -> "Reconciliation":
    """Convenience wrapper accepting a :class:`Journal` or a path."""
    opened = False
    if not isinstance(journal, Journal):
        journal = Journal(journal, fsync=False)
        opened = True
    try:
        return journal.reconcile(key=key, layer=layer)
    finally:
        if opened:
            journal.close()


class AttemptTrace(object):
    """Reconciled facts for one attempt."""

    def __init__(self, run: "RunTrace", number: int, attempt_id: str) -> None:
        self.run = run
        self.number = number
        self.attempt_id = attempt_id
        self.started_at: Optional[float] = None
        self.entry_elapsed: Optional[float] = None
        self.result_at: Optional[float] = None
        self.outcome: Optional[str] = None
        self.value: Any = None
        self.exception: Any = None
        self.raise_on_giveup: Optional[bool] = None
        self.decision: Optional[str] = None
        self.decision_reason: Optional[str] = None
        self.wait: Optional[float] = None
        self.raw_wait: Optional[float] = None
        self.wait_at: Optional[float] = None
        self.receipt: Optional[Dict[str, Any]] = None
        self.sleep_started_at: Optional[float] = None
        self.wake_at: Optional[float] = None
        self.intended_wake_at: Optional[float] = None
        self.sleep_done_at: Optional[float] = None
        self.waited: Optional[float] = None
        self.terminal: Optional[Dict[str, Any]] = None
        self.callbacks: Dict[str, List[str]] = {}
        self.callback_events: List[Dict[str, Any]] = []
        self.ack: Optional[Dict[str, Any]] = None
        self.ack_late = False

    @property
    def layer(self) -> str:
        return self.run.layer

    @property
    def key(self) -> str:
        return self.run.key

    @property
    def run_number(self) -> int:
        return self.run.number

    @property
    def tries(self) -> int:
        return self.number

    @property
    def receipt_phase(self) -> str:
        """Where the attempt stopped relative to its durable backoff receipt.

        One of ``none`` (no wait decided), ``before`` (wait decided but the
        backoff receipt is missing), ``after`` (receipt committed) or
        ``terminal`` (outcome receipt committed).
        """
        if self.terminal is not None:
            return "terminal"
        if self.receipt is not None:
            return "after"
        if self.wait is not None or self.decision in ("backoff", "retry"):
            return "before"
        return "none"

    @property
    def confirmed(self) -> bool:
        """The decision for this attempt is backed by a durable receipt."""
        if self.decision == "backoff" or self.decision == "retry":
            return self.receipt is not None
        if self.decision in ("success", "giveup"):
            return self.terminal is not None
        return False

    @property
    def completed(self) -> bool:
        """Every callback that was started also recorded completion."""
        return all(state == "done"
                   for states in self.callbacks.values()
                   for state in states)

    @property
    def pending_callbacks(self) -> List[str]:
        return ["%s[%d]" % (phase, index)
                for phase, states in self.callbacks.items()
                for index, state in enumerate(states)
                if state != "done"]

    @property
    def acknowledged(self) -> bool:
        return self.ack is not None

    @property
    def status(self) -> str:
        if self.terminal is not None:
            return "success" if self.decision == "success" else "giveup"
        if self.receipt is not None:
            if self.sleep_done_at is not None:
                return "continued"
            if self.sleep_started_at is not None:
                return "sleeping"
            return "receipted"
        if self.wait is not None:
            return "receipt_pending"
        if self.decision is not None:
            return "wait_pending"
        if self.result_at is not None:
            return "decision_pending"
        if self.started_at is not None:
            return "sent"
        return "unknown"

    def callback_state(self, phase: str) -> List[str]:
        return list(self.callbacks.get(phase, []))

    def to_dict(self, include_run_config: bool = True) -> Dict[str, Any]:
        data = {
            "layer": self.layer,
            "run": self.run_number,
            "attempt": self.number,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "started_at": self.started_at,
            "entry_elapsed": self.entry_elapsed,
            "result_at": self.result_at,
            "outcome": self.outcome,
            "value": decode(self.value) if self.value is not None else None,
            "exception": (decode_exception(self.exception)
                          if self.exception is not None else None),
            "raise_on_giveup": self.raise_on_giveup,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "receipt_phase": self.receipt_phase,
            "completed": self.completed,
            "pending_callbacks": self.pending_callbacks,
            "wait": self.wait,
            "raw_wait": self.raw_wait,
            "wait_at": self.wait_at,
            "confirmed": self.confirmed,
            "receipt_at": self.receipt.get("at") if self.receipt else None,
            "sleep_started_at": self.sleep_started_at,
            "wake_at": self.wake_at,
            "sleep_done_at": self.sleep_done_at,
            "waited": self.waited,
            "callbacks": {phase: list(state)
                          for phase, state in self.callbacks.items()},
            "acknowledged": self.ack is not None,
            "ack_at": self.ack.get("at") if self.ack else None,
            "ack_late": self.ack_late,
        }
        if include_run_config:
            data["config"] = self.run.config
        return data


class RunTrace(object):
    """Reconciled facts for one layer run (one config snapshot)."""

    def __init__(self, key: str, layer: str, number: int,
                 config: Optional[Dict[str, Any]],
                 started_at: Optional[float]) -> None:
        self.key = key
        self.layer = layer
        self.number = number
        self.config = config
        self.started_at = started_at
        self.attempts: List[AttemptTrace] = []
        self.terminal: Optional[Dict[str, Any]] = None

    @property
    def concluded(self) -> bool:
        return self.terminal is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "layer": self.layer,
            "run": self.number,
            "started_at": self.started_at,
            "config": self.config,
            "concluded": self.concluded,
            "attempts": [attempt.to_dict(include_run_config=False)
                         for attempt in self.attempts],
        }


class Reconciliation(object):
    """Stable view of what a journal proves happened."""

    def __init__(self, journal: Journal, key: Optional[str] = None,
                 layer: Optional[str] = None) -> None:
        self.journal = journal
        self.key = key
        self.layer = layer
        self.runs: List[RunTrace] = []
        self._attempts: List[AttemptTrace] = []
        self._build()

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        events = self.journal.records(self.key, layer=self.layer)
        runs: Dict[tuple, RunTrace] = {}
        attempts: Dict[str, AttemptTrace] = {}
        attempt_seq: Dict[str, int] = {}
        pending_acks: List[Dict[str, Any]] = []

        for event in events:
            kind = event.get("kind")
            key = event.get("key")
            layer = event.get("layer")
            run_no = event.get("run")
            if kind == "ack":
                pending_acks.append(event)
                continue
            if key is None or layer is None or run_no is None:
                continue
            run_key = (key, layer, run_no)
            if kind == "run_start":
                run = RunTrace(key, layer, run_no, event.get("config"),
                               event.get("at"))
                runs[run_key] = run
                self.runs.append(run)
                continue
            run = runs.get(run_key)
            if run is None:
                continue
            attempt_id = event.get("attempt_id")
            if kind == "success_receipt" or kind == "giveup_receipt":
                run.terminal = event
            if attempt_id is None:
                continue
            if kind == "attempt_start":
                attempt = AttemptTrace(run, event.get("attempt"), attempt_id)
                attempt.started_at = event.get("at")
                attempt.entry_elapsed = event.get("elapsed")
                run.attempts.append(attempt)
                attempts[attempt_id] = attempt
                attempt_seq[attempt_id] = event.get("seq", 0)
                continue
            attempt = attempts.get(attempt_id)
            if attempt is None:
                continue
            self._apply(attempt, event)

        for ack in pending_acks:
            attempt = attempts.get(ack.get("attempt_id"))
            if attempt is None:
                continue
            attempt.ack = ack
            ack_seq = ack.get("seq", 0)
            attempt.ack_late = any(
                ack_seq > seq
                for other_id, seq in attempt_seq.items()
                if other_id != attempt.attempt_id
                and attempts[other_id].run is attempt.run
                and seq > attempt_seq[attempt.attempt_id]
            )
        self._attempts = sorted(
            attempts.values(), key=lambda item: attempt_seq[item.attempt_id]
        )

    @staticmethod
    def _apply(attempt: AttemptTrace, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "attempt_result":
            attempt.result_at = event.get("at")
            attempt.outcome = event.get("outcome")
            attempt.value = event.get("value")
            attempt.exception = event.get("exception")
            attempt.entry_elapsed = event.get("elapsed",
                                              attempt.entry_elapsed)
            attempt.raise_on_giveup = event.get("raise_on_giveup")
        elif kind == "attempt_decision":
            attempt.decision = event.get("decision")
            attempt.decision_reason = event.get("reason")
            attempt.entry_elapsed = event.get("elapsed",
                                              attempt.entry_elapsed)
        elif kind == "wait_planned":
            attempt.wait = event.get("wait")
            attempt.raw_wait = event.get("raw_wait")
            attempt.wait_at = event.get("at")
            if attempt.wait is not None and attempt.wait_at is not None:
                attempt.intended_wake_at = attempt.wait_at + attempt.wait
        elif kind == "backoff_receipt":
            attempt.receipt = event
            anchor = event.get("anchor_at")
            if anchor is None:
                anchor = event.get("at")
            if anchor is not None and attempt.wait is not None:
                attempt.intended_wake_at = float(anchor) + attempt.wait
        elif kind == "sleep_start":
            attempt.sleep_started_at = event.get("at")
            attempt.wake_at = event.get("wake_at")
        elif kind == "sleep_done":
            attempt.sleep_done_at = event.get("at")
            attempt.waited = event.get("waited")
        elif kind in TERMINAL_RECEIPT_KINDS:
            attempt.terminal = event
            attempt.decision = ("success" if kind == "success_receipt"
                                else "giveup")
        elif kind == "callback":
            phase = event.get("phase")
            index = event.get("index")
            if phase is not None and index is not None:
                states = attempt.callbacks.setdefault(phase, [])
                while len(states) <= index:
                    states.append("missing")
                states[index] = event.get("status", "unknown")
            attempt.callback_events.append(event)
        elif kind == "ack":
            attempt.ack = event

    # -- access ----------------------------------------------------------
    @property
    def attempts(self) -> List[AttemptTrace]:
        return list(self._attempts)

    def trace(self) -> List[Dict[str, Any]]:
        return [attempt.to_dict() for attempt in self._attempts]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "layer": self.layer,
            "runs": [run.to_dict() for run in self.runs],
            "attempts": self.trace(),
        }

    def compare(self, other: "Reconciliation") -> "TraceDiff":
        return TraceDiff(self, other)


class TraceDiff(object):
    """Difference between a resumed trace and an uninterrupted trace."""

    FIELDS = (
        "outcome", "decision", "receipt_phase", "wait", "intended_wake_at",
        "wake_at", "waited", "status", "confirmed", "completed",
        "acknowledged", "ack_late",
    )

    def __init__(self, actual: Reconciliation,
                 expected: Reconciliation) -> None:
        self.actual = actual
        self.expected = expected
        self.differences: List[Dict[str, Any]] = []
        self.missing: List[str] = []
        self.extra: List[str] = []
        self._compare()

    @staticmethod
    def _identity(attempt: AttemptTrace):
        return (attempt.layer, attempt.run_number, attempt.number)

    @staticmethod
    def _label(identity) -> str:
        return "%s/run-%s/attempt-%s" % identity

    def _compare(self) -> None:
        actual_ids = {self._identity(a): a for a in self.actual.attempts}
        expected_ids = {self._identity(a): a for a in self.expected.attempts}
        for identity, expected in expected_ids.items():
            actual = actual_ids.get(identity)
            if actual is None:
                self.missing.append(self._label(identity))
                continue
            self._compare_attempt(actual, expected)
        for identity in actual_ids:
            if identity not in expected_ids:
                self.extra.append(self._label(identity))
        actual_order = [self._identity(a) for a in self.actual.attempts]
        expected_order = [self._identity(a) for a in self.expected.attempts]
        if actual_order != expected_order:
            self.differences.append({
                "attempt": "*",
                "field": "order",
                "expected": expected_order,
                "actual": actual_order,
            })
        actual_runs = [(r.layer, r.number, r.config)
                       for r in self.actual.runs]
        expected_runs = [(r.layer, r.number, r.config)
                         for r in self.expected.runs]
        if actual_runs != expected_runs:
            self.differences.append({
                "attempt": "*",
                "field": "runs",
                "expected": expected_runs,
                "actual": actual_runs,
            })

    def _compare_attempt(self, actual: AttemptTrace,
                         expected: AttemptTrace) -> None:
        label = self._label(self._identity(expected))
        for field in self.FIELDS:
            actual_value = getattr(actual, field)
            expected_value = getattr(expected, field)
            if field == "confirmed":
                actual_value = actual.confirmed
                expected_value = expected.confirmed
            if actual_value != expected_value:
                self.differences.append({
                    "attempt": label,
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                })
        actual_phases = set(actual.callbacks)
        expected_phases = set(expected.callbacks)
        for phase in sorted(actual_phases | expected_phases):
            actual_state = actual.callback_state(phase)
            expected_state = expected.callback_state(phase)
            if actual_state != expected_state:
                self.differences.append({
                    "attempt": label,
                    "field": "callbacks.%s" % phase,
                    "expected": expected_state,
                    "actual": actual_state,
                })

    @property
    def matches(self) -> bool:
        return not (self.differences or self.missing or self.extra)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matches": self.matches,
            "differences": self.differences,
            "missing": self.missing,
            "extra": self.extra,
        }

    def __bool__(self) -> bool:
        return self.matches
