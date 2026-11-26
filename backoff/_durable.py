# coding:utf-8
"""Durable retry helpers used by the opt-in resumable retry driver.

Only decorated calls that opt into a journal use these helpers.  The original
retry loops remain untouched for ordinary calls.
"""
import contextlib
import contextvars
from typing import Any, Callable, Dict, List, Optional

from backoff._common import _init_wait_gen, _maybe_call
from backoff._journal import (
    Journal,
    TERMINAL_RECEIPT_KINDS,
    DurableResumeError,
    decode,
    decode_exception,
    encode,
    encode_exception,
    make_attempt_id,
)

NOT_FOUND = object()

_current_attempt_id = contextvars.ContextVar(
    "backoff_current_attempt_id", default=None)
_current_key = contextvars.ContextVar("backoff_current_key", default=None)
_current_layer = contextvars.ContextVar("backoff_current_layer", default=None)
_active_layers = contextvars.ContextVar("backoff_active_layers", default=())


def current_attempt_id() -> Optional[str]:
    """Attempt id of the target invocation currently in progress."""
    return _current_attempt_id.get()


attempt_id = current_attempt_id


def current_key() -> Optional[str]:
    """Journal key of the durable call currently in progress."""
    return _current_key.get()


class AttemptOutcome(object):
    """Structured answer from an attempt-id lookup hook."""

    __slots__ = ("value", "exception", "receipt")

    def __init__(self, value: Any = None, *,
                 exception: Optional[BaseException] = None,
                 receipt: Any = None) -> None:
        self.value = value
        self.exception = exception
        self.receipt = receipt


class ResumeContext(object):
    """Per-call options that turn a decorated call into a durable one."""

    __slots__ = ("journal", "lookup", "key", "layer", "resume",
                 "raise_on_giveup", "sleep", "clock")

    def __init__(self, journal=None, *, lookup=None, key=None, layer=None,
                 resume=None, raise_on_giveup=None, sleep=None,
                 clock=None) -> None:
        self.journal = journal
        self.lookup = lookup
        self.key = key
        self.layer = layer
        self.resume = resume
        self.raise_on_giveup = raise_on_giveup
        self.sleep = sleep
        self.clock = clock


_resume_context = contextvars.ContextVar("backoff_resume_context",
                                         default=None)


def current_resume_context() -> Optional[ResumeContext]:
    return _resume_context.get()


@contextlib.contextmanager
def resume(journal=None, *, lookup=None, key=None, layer=None, resume=None,
           raise_on_giveup=None, sleep=None, clock=None):
    """Enable resumable execution for decorated calls in this context.

    ``journal`` may be a :class:`Journal`, a path, or any ``os.PathLike``.
    ``lookup`` is called with an attempt id and may return a stored result, an
    :class:`AttemptOutcome`, or :data:`backoff.NOT_FOUND`.
    """
    context = ResumeContext(journal, lookup=lookup, key=key, layer=layer,
                            resume=resume, raise_on_giveup=raise_on_giveup,
                            sleep=sleep, clock=clock)
    token = _resume_context.set(context)
    try:
        yield context
    finally:
        _resume_context.reset(token)


def resolve_journal(journal) -> tuple:
    """Return ``(journal, opened)``.

    ``opened`` journals are closed by the caller.
    """
    if isinstance(journal, Journal):
        return journal, False
    if journal is None:
        return None, False
    return Journal(journal), True


def journal_key(context: ResumeContext, target, args, kwargs) -> str:
    """Stable key for a durable call.

    An explicit ``key`` wins, then an enclosing durable call's key, then the
    target's qualified name.
    """
    if context.key is not None:
        return str(context.key)
    inherited = current_key()
    if inherited is not None:
        return inherited
    name = getattr(target, "__qualname__", getattr(target, "__name__", "call"))
    module = getattr(target, "__module__", "")
    return "%s.%s" % (module, name) if module else str(name)


def journal_layer(context: ResumeContext, name: Optional[str] = None) -> str:
    """Stable layer name for one decorated function in a durable call."""
    if name is not None:
        return str(name)
    if context.layer is not None:
        return str(context.layer)
    return _current_layer.get()


def target_layer_name(target) -> str:
    name = getattr(target, "__qualname__", getattr(target, "__name__", "call"))
    module = getattr(target, "__module__", "")
    return "%s.%s" % (module, name) if module else str(name)


def make_details(target, args, kwargs, attempt, elapsed, run,
                 attempt_id_value, **extra):
    details = {
        "target": target,
        "args": args,
        "kwargs": kwargs,
        "tries": attempt,
        "elapsed": elapsed,
        "key": run.key,
        "layer": run.layer,
        "run": run.run,
        "attempt": attempt,
        "attempt_id": attempt_id_value,
    }
    details.update(extra)
    return details


def resolve_config(max_tries, max_time, wait_gen_kwargs) -> Dict[str, Any]:
    return {
        "max_tries": _maybe_call(max_tries),
        "max_time": _maybe_call(max_time),
        "wait_gen_kwargs": {key: _maybe_call(value)
                            for key, value in wait_gen_kwargs.items()},
    }


def _decode_config(config) -> Dict[str, Any]:
    decoded = decode(config)
    if not isinstance(decoded, dict):
        raise DurableResumeError("malformed run configuration: %r" % (config,))
    kwargs = decoded.get("wait_gen_kwargs") or {}
    if not isinstance(kwargs, dict):
        raise DurableResumeError("malformed wait generator arguments: %r"
                                 % (kwargs,))
    return {
        "max_tries": decoded.get("max_tries"),
        "max_time": decoded.get("max_time"),
        "wait_gen_kwargs": kwargs,
    }


class DurableRun(object):
    """One run (one uninterrupted layer entry) and its config snapshot."""

    __slots__ = ("journal", "key", "layer", "run", "config", "started_at",
                 "resumed")

    def __init__(self, journal: Journal, key: str, layer: str, run: int,
                 config: Dict[str, Any], started_at: float,
                 resumed: bool) -> None:
        self.journal = journal
        self.key = key
        self.layer = layer
        self.run = run
        self.config = config
        self.started_at = started_at
        self.resumed = resumed

    @property
    def max_tries(self):
        return self.config.get("max_tries")

    @property
    def max_time(self):
        return self.config.get("max_time")

    @property
    def wait_gen_kwargs(self) -> Dict[str, Any]:
        return self.config.get("wait_gen_kwargs", {})

    def attempt_id(self, attempt: int):
        return make_attempt_id(self.key, self.layer, self.run, attempt)

    def elapsed(self) -> float:
        return float(self.journal.clock()) - self.started_at


def _run_delivered(journal: Journal, key: str, layer: str, run: int) -> bool:
    return bool(journal.records(key, layer=layer, run=run,
                                kind="run_delivered"))


def concluded_outcome(journal: Journal, run: DurableRun):
    """Return ``(value, exception, raise_on_giveup)`` or ``NOT_FOUND``."""
    terminal = None
    for event in journal.records(run.key, layer=run.layer, run=run.run):
        if event.get("kind") in TERMINAL_RECEIPT_KINDS:
            terminal = event
    if terminal is None:
        return NOT_FOUND
    value = (decode(terminal.get("value"))
             if terminal.get("value") is not None else None)
    exception = (decode_exception(terminal["exception"])
                 if terminal.get("exception") is not None else None)
    return value, exception, terminal.get("raise_on_giveup", True)


def begin_run(journal: Journal, key: str, layer: str,
              resolve: Callable[[], Dict[str, Any],],
              *, resume: Optional[bool] = None) -> DurableRun:
    """Continue the latest unfinished run or start a new one.

    A concluded run whose outcome was never delivered is also continued so
    callers cannot lose an already committed decision.  ``resume=False``
    forces a new run.
    """
    starts = journal.records(key, layer=layer, kind="run_start")
    if resume is not False and starts:
        latest = starts[-1]
        run_no = latest["run"]
        delivered = _run_delivered(journal, key, layer, run_no)
        # Continue an unfinished run.  A concluded but undelivered run is
        # replayed so a crash between the receipt and returning its outcome
        # cannot lose an already committed decision.  A delivered run is
        # complete: entering the layer again starts a new run.
        if not delivered:
            return DurableRun(journal, key, layer, run_no,
                              _decode_config(latest.get("config")),
                              latest["at"], True)
    run_no = starts[-1]["run"] + 1 if starts else 0
    config = resolve()
    event = journal.append("run_start", key=key, layer=layer, run=run_no,
                           config=encode(config))
    return DurableRun(journal, key, layer, run_no, config, event["at"], False)


def deliver(run: DurableRun) -> None:
    run.journal.append("run_delivered", key=run.key, layer=run.layer,
                       run=run.run)


class WaitEngine(object):
    """Wait generator positioned according to the journal.

    ``derive`` advances the generator for an attempt that has no recorded
    wait.  ``replay`` advances it for a completed attempt without applying
    jitter again, so resumed and uninterrupted runs share the same sequence.
    """

    def __init__(self, wait_gen, wait_gen_kwargs, jitter, max_time) -> None:
        self.jitter = jitter
        self.max_time = max_time
        self._wait = _init_wait_gen(wait_gen, wait_gen_kwargs)
        self._advanced: List[str] = []

    def _jittered(self, raw):
        if self.jitter is None:
            return raw
        try:
            return self.jitter(raw)
        except TypeError:
            return raw + self.jitter()

    def derive(self, attempt_id_value, send_value, elapsed):
        """Return ``(wait, raw_wait)`` or ``None`` when exhausted."""
        if attempt_id_value in self._advanced:
            return None
        self._advanced.append(attempt_id_value)
        try:
            raw = self._wait.send(send_value)
        except StopIteration:
            return None
        seconds = self._jittered(raw)
        if self.max_time is not None:
            seconds = min(seconds, self.max_time - elapsed)
        return seconds, raw

    def replay(self, attempt_id_value, send_value) -> None:
        if attempt_id_value in self._advanced:
            return
        self._advanced.append(attempt_id_value)
        self._wait.send(send_value)

    def advanced(self, attempt_id_value) -> bool:
        return attempt_id_value in self._advanced


_NoSend = object()


def _send_value(result_event):
    exception = result_event.get("exception")
    if exception is not None:
        return decode_exception(exception)
    return decode(result_event.get("value"))


def replay_wait_engine(journal: Journal, run: DurableRun, wait_gen,
                       jitter) -> WaitEngine:
    engine = WaitEngine(wait_gen, run.wait_gen_kwargs, jitter, run.max_time)
    for start in journal.records(run.key, layer=run.layer, run=run.run,
                                 kind="attempt_start"):
        tid = start["attempt_id"]
        result = find_result(journal, tid)
        planned = find_wait(journal, tid)
        if result is not None and planned is not None:
            engine.replay(tid, _send_value(result))
    return engine


# -- record helpers ------------------------------------------------------
def record_attempt_start(journal, run, attempt, attempt_id_value, elapsed):
    return journal.append(
        "attempt_start", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value, elapsed=elapsed,
    )


def record_result(journal, run, attempt, attempt_id_value, *, value=None,
                  exception=None, elapsed=None, recovered=False):
    return journal.append(
        "attempt_result", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value,
        outcome="exception" if exception is not None else "value",
        value=encode(value) if exception is None else None,
        exception=(encode_exception(exception)
                   if exception is not None else None),
        elapsed=elapsed, recovered=recovered,
    )


def record_decision(journal, run, attempt, attempt_id_value, decision,
                    *, reason=None, elapsed=None, raise_on_giveup=None):
    return journal.append(
        "attempt_decision", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value,
        decision=decision, reason=reason, elapsed=elapsed,
        raise_on_giveup=raise_on_giveup,
    )


def record_wait(journal, run, attempt, attempt_id_value, wait, raw_wait,
                elapsed):
    return journal.append(
        "wait_planned", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value,
        wait=wait, raw_wait=raw_wait, elapsed=elapsed,
    )


def record_backoff_receipt(journal, run, attempt, attempt_id_value, wait,
                           elapsed, *, value=None, exception=None,
                           anchor_at=None):
    return journal.append(
        "backoff_receipt", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value, wait=wait,
        elapsed=elapsed, anchor_at=anchor_at,
        value=(encode(value) if exception is None else None),
        exception=(encode_exception(exception)
                   if exception is not None else None),
    )


def record_terminal_receipt(journal, run, attempt, attempt_id_value, decision,
                            *, value=None, exception=None,
                            raise_on_giveup=False):
    kind = "success_receipt" if decision == "success" else "giveup_receipt"
    return journal.append(
        kind, key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value,
        value=(encode(value) if exception is None else None),
        exception=(encode_exception(exception)
                   if exception is not None else None),
        raise_on_giveup=raise_on_giveup,
    )


def record_sleep_start(journal, run, attempt, attempt_id_value, wake_at):
    return journal.append(
        "sleep_start", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value, wake_at=wake_at,
    )


def record_sleep_done(journal, run, attempt, attempt_id_value, waited):
    return journal.append(
        "sleep_done", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value, waited=waited,
    )


def record_callback(journal, run, attempt, attempt_id_value, phase, index,
                    status):
    return journal.append(
        "callback", key=run.key, layer=run.layer, run=run.run,
        attempt=attempt, attempt_id=attempt_id_value,
        phase=phase, index=index, status=status,
    )


# -- lookups -------------------------------------------------------------
def _last(journal, attempt_id_value, kind):
    records = journal.records(attempt_id=attempt_id_value, kind=kind)
    return records[-1] if records else None


def find_attempt_start(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "attempt_start")


def find_result(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "attempt_result")


def find_decision(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "attempt_decision")


def find_wait(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "wait_planned")


def find_backoff(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "backoff_receipt")


def find_sleep_start(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "sleep_start")


def find_sleep_done(journal, attempt_id_value):
    return _last(journal, attempt_id_value, "sleep_done")


def find_terminal(journal, attempt_id_value):
    for event in journal.records(attempt_id=attempt_id_value):
        if event.get("kind") in TERMINAL_RECEIPT_KINDS:
            return event
    return None


def callbacks_done(journal, attempt_id_value) -> Dict[str, List[bool]]:
    result: Dict[str, List[bool]] = {}
    for event in journal.records(attempt_id=attempt_id_value, kind="callback"):
        phase = event.get("phase")
        index = event.get("index")
        if phase is None or index is None:
            continue
        states = result.setdefault(phase, [])
        while len(states) <= index:
            states.append(False)
        if event.get("status") == "done":
            states[index] = True
    return result


def attempt_from_ack(journal, attempt_id_value):
    """Return ``(value, exception, found)`` from an acceptance receipt."""
    ack = journal.acknowledgement(attempt_id_value)
    if ack is None:
        return None, None, False
    exception = (decode_exception(ack["exception"])
                 if ack.get("exception") is not None else None)
    value = decode(ack.get("value")) if ack.get("has_value") else None
    return value, exception, True


def lookup_outcome(lookup, attempt_id_value):
    """Call the lookup hook and normalise its answer."""
    if lookup is None:
        return None, None, False
    result = lookup(attempt_id_value)
    if result is NOT_FOUND or result is None:
        return None, None, False
    if isinstance(result, AttemptOutcome):
        return result.value, result.exception, True
    return result, None, True
