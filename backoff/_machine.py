# coding:utf-8
"""Resumable retry state machine and sync/async drivers.

The machine emits small :class:`Step` objects; the drivers only perform the IO
a step asks for (invoke the target, call a handler, sleep until an absolute
timestamp).  Every decision is read from and written to the journal before the
step runs, so both drivers advance identical state.

Ordinary decorated calls never enter this module.
"""
import time
from typing import Any, Optional

from backoff._durable import (
    NOT_FOUND,
    DurableRun,
    WaitEngine,
    attempt_from_ack,
    begin_run,
    callbacks_done,
    concluded_outcome,
    deliver,
    find_attempt_start,
    find_backoff,
    find_decision,
    find_result,
    find_sleep_done,
    find_sleep_start,
    find_terminal,
    find_wait,
    lookup_outcome,
    record_attempt_start,
    record_backoff_receipt,
    record_callback,
    record_decision,
    record_result,
    record_sleep_done,
    record_sleep_start,
    record_terminal_receipt,
    record_wait,
    replay_wait_engine,
    resolve_config,
    resolve_journal,
)
from backoff._journal import decode, decode_exception

FAILED = object()
RETRY = object()


class Step(object):
    __slots__ = ("kind", "payload")

    def __init__(self, kind, payload=None) -> None:
        self.kind = kind
        self.payload = payload

    def __repr__(self) -> str:
        return "Step(%s)" % (self.kind,)


def _now(journal) -> float:
    return float(journal.clock())


def _stored_result(event):
    if event is None:
        return None, None
    if event.get("outcome") == "exception":
        return None, decode_exception(event["exception"])
    return decode(event.get("value")), None


def _raise_on_giveup(context, stored: bool) -> bool:
    if context.raise_on_giveup is not None:
        return bool(context.raise_on_giveup)
    return bool(stored)


class Machine(object):
    """Advance one layer run, reading every fact back from the journal."""

    def __init__(self, *, journal, context, target, args, kwargs, wait_gen,
                 max_tries, max_time, jitter, giveup, predicate,
                 on_success, on_backoff, on_giveup, raise_on_giveup,
                 wait_gen_kwargs, exception_types=None,
                 layer_name=None) -> None:
        self.journal = journal
        self.context = context
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.wait_gen = wait_gen
        self.max_tries = max_tries
        self.max_time = max_time
        self.jitter = jitter
        self.giveup = giveup
        self.predicate = predicate
        self.on_success = on_success or []
        self.on_backoff = on_backoff or []
        self.on_giveup = on_giveup or []
        self._raise_default = raise_on_giveup
        self.wait_gen_kwargs = wait_gen_kwargs
        self.exception_types = exception_types
        self.layer_name = layer_name
        self.key = ""
        self.layer = ""
        self.run: Optional[DurableRun] = None
        self.wait_engine: Optional[WaitEngine] = None

    # -- setup -----------------------------------------------------------
    def _resolve(self):
        return resolve_config(self.max_tries, self.max_time,
                              self.wait_gen_kwargs)

    def _engine(self) -> WaitEngine:
        if self.wait_engine is None:
            self.wait_engine = replay_wait_engine(
                self.journal, self.run, self.wait_gen, self.jitter)
        return self.wait_engine

    @property
    def raise_on_giveup(self) -> bool:
        return _raise_on_giveup(self.context, self._raise_default)

    def _entry_elapsed(self, tid) -> float:
        start = find_attempt_start(self.journal, tid)
        if start is not None and start.get("elapsed") is not None:
            return float(start["elapsed"])
        result = find_result(self.journal, tid)
        if result is not None and result.get("elapsed") is not None:
            return float(result["elapsed"])
        return self.run.elapsed()

    def _details(self, attempt, elapsed, tid, **extra):
        details = {
            "target": self.target,
            "args": self.args,
            "kwargs": self.kwargs,
            "tries": attempt,
            "elapsed": elapsed,
            "key": self.run.key,
            "layer": self.run.layer,
            "run": self.run.run,
            "attempt": attempt,
            "attempt_id": tid,
        }
        details.update(extra)
        return details

    def _attempt_starts(self):
        return self.journal.records(self.run.key, layer=self.run.layer,
                                    run=self.run.run, kind="attempt_start")

    # -- generation ------------------------------------------------------
    def steps(self):
        from backoff._durable import (
            _active_layers,
            _current_layer,
            journal_key,
            journal_layer,
            target_layer_name,
        )

        self.key = journal_key(self.context, self.target, self.args,
                               self.kwargs)
        name = self.layer_name
        if name is None:
            if self.context.layer is not None and _current_layer.get() is None:
                name = self.context.layer
            else:
                name = target_layer_name(self.target)
        self.layer = journal_layer(self.context, name)
        active = _active_layers.get()
        token = _active_layers.set(active + (self.layer,))
        try:
            yield from self._run_steps()
        finally:
            _active_layers.reset(token)

    def _run_steps(self):
        self.run = begin_run(self.journal, self.key, self.layer,
                             self._resolve, resume=self.context.resume)

        concluded = concluded_outcome(self.journal, self.run)
        if concluded is not NOT_FOUND:
            value, exception, stored_raise = concluded
            raise_on = _raise_on_giveup(self.context, stored_raise)
            yield Step("RETURN", (value, exception, raise_on))
            return

        starts = self._attempt_starts()
        attempt = int(starts[-1]["attempt"]) if starts else 1
        if not starts:
            tid = self.run.attempt_id(attempt)
            record_attempt_start(self.journal, self.run, attempt, tid,
                                 self.run.elapsed())
        while True:
            tid = self.run.attempt_id(attempt)
            outcome = yield from self._advance(attempt, tid)
            if outcome is not RETRY:
                return
            attempt += 1

    def _advance(self, attempt, tid):
        result = find_result(self.journal, tid)
        if result is None:
            yield from self._ensure_result(attempt, tid)
            result = find_result(self.journal, tid)
            if result is None:
                yield Step("RETURN", (FAILED, None, True))
                return None

        value, exception = _stored_result(result)
        elapsed = self._entry_elapsed(tid)

        decision = find_decision(self.journal, tid)
        if decision is None:
            selected = self._compute_decision(attempt, value, exception,
                                              elapsed)
            record_decision(self.journal, self.run, attempt, tid, selected,
                            elapsed=elapsed,
                            raise_on_giveup=self.raise_on_giveup)
            decision = find_decision(self.journal, tid)
        selected = decision.get("decision")
        if selected == "retry":
            selected = "backoff"

        if selected == "success":
            yield from self._finish("success", attempt, tid, value, exception)
            return None
        if selected == "giveup":
            yield from self._finish("giveup", attempt, tid, value, exception)
            return None
        yield from self._backoff(attempt, tid, value, exception)
        return RETRY

    def _ensure_result(self, attempt, tid):
        if find_attempt_start(self.journal, tid) is None:
            record_attempt_start(self.journal, self.run, attempt, tid,
                                 self.run.elapsed())

        value, exception, found = attempt_from_ack(self.journal, tid)
        if not found:
            value, exception, found = lookup_outcome(self.context.lookup, tid)
        if found:
            record_result(self.journal, self.run, attempt, tid,
                          value=value, exception=exception,
                          elapsed=self._entry_elapsed(tid), recovered=True)
            return
        yield Step("INVOKE", (attempt, tid))
        if find_result(self.journal, tid) is None:
            raise RuntimeError("target invocation did not record a result")

    def _compute_decision(self, attempt, value, exception, elapsed):
        max_tries = self.run.max_tries
        max_time = self.run.max_time
        max_tries_exceeded = (max_tries is not None
                              and attempt == int(max_tries))
        max_time_exceeded = (max_time is not None
                             and elapsed >= float(max_time))
        if exception is not None:
            giveup_result = False
            if self.giveup:
                giveup_result = bool(self.giveup(exception))
            if giveup_result or max_tries_exceeded or max_time_exceeded:
                return "giveup"
            return "backoff"
        if self.predicate is None or not self.predicate(value):
            return "success"
        if max_tries_exceeded or max_time_exceeded:
            return "giveup"
        return "backoff"

    def _finish(self, decision, attempt, tid, value, exception, *,
                include_value=True):
        elapsed = self._entry_elapsed(tid)
        details = self._details(attempt, elapsed, tid)
        if include_value:
            details["value"] = None if exception is not None else value
        if exception is not None:
            details["exception"] = exception
        handlers = self.on_success if decision == "success" else self.on_giveup
        yield from self._call_handlers(decision, handlers, attempt, tid,
                                       details)

        if find_terminal(self.journal, tid) is None:
            ack_value, ack_exception, ack_found = attempt_from_ack(
                self.journal, tid)
            if ack_found:
                value, exception = ack_value, ack_exception
            record_terminal_receipt(
                self.journal, self.run, attempt, tid, decision,
                value=value, exception=exception,
                raise_on_giveup=(decision == "giveup"
                                 and self.raise_on_giveup))
        deliver(self.run)
        if decision == "giveup" and exception is not None \
                and self.raise_on_giveup:
            yield Step("RETURN", (FAILED, exception, True))
        else:
            yield Step("RETURN", (value, exception, False))

    def _call_handlers(self, phase, handlers, attempt, tid, details):
        states = callbacks_done(self.journal, tid).get(phase, [])
        for index, handler in enumerate(handlers or ()):
            if index < len(states) and states[index]:
                continue
            record_callback(self.journal, self.run, attempt, tid, phase,
                            index, "start")
            yield Step("CALLBACK", (handler, details, phase, attempt, tid,
                                    index))
            record_callback(self.journal, self.run, attempt, tid, phase,
                            index, "done")

    def _backoff(self, attempt, tid, value, exception):
        while True:
            planned = find_wait(self.journal, tid)
            if planned is None or planned.get("wait") is None:
                elapsed = self._entry_elapsed(tid)
                outcome = self._engine().derive(
                    tid, exception if exception is not None else value,
                    elapsed)
                if outcome is None:
                    record_decision(self.journal, self.run, attempt, tid,
                                    "giveup", reason="wait-exhausted",
                                    elapsed=elapsed, raise_on_giveup=True)
                    yield from self._finish(
                        "giveup", attempt, tid, value, exception,
                        include_value=exception is not None)
                    return
                seconds, raw = outcome
                record_wait(self.journal, self.run, attempt, tid, seconds,
                            raw, elapsed)
                continue

            seconds = float(planned["wait"])
            elapsed = self._entry_elapsed(tid)
            receipt = find_backoff(self.journal, tid)
            if receipt is None:
                anchor = self._anchor_before_receipt(planned, attempt, tid)
                details = self._details(attempt, elapsed, tid)
                details["wait"] = seconds
                details["value"] = None if exception is not None else value
                if exception is not None:
                    details["exception"] = exception
                yield from self._call_handlers("backoff", self.on_backoff,
                                               attempt, tid, details)
                receipt = record_backoff_receipt(
                    self.journal, self.run, attempt, tid, seconds, elapsed,
                    value=value, exception=exception, anchor_at=anchor)
                continue

            yield from self._wait(attempt, tid, receipt, seconds)
            return

    def _anchor_before_receipt(self, planned, attempt, tid):
        """Intended start of a wait whose receipt has not landed yet.

        The waiting stage is defined by the decision that produced the wait,
        not by the time a later attempt re-enters the loop.  Callback
        completion is already durable and is replayed separately, so the
        anchor stays at the decision.
        """
        return float(planned["at"])

    def _wait(self, attempt, tid, receipt, seconds):
        start = find_sleep_start(self.journal, tid)
        done = find_sleep_done(self.journal, tid)
        if done is not None:
            return
        if start is not None:
            wake_at = float(start["wake_at"])
            now = _now(self.journal)
            record_sleep_done(
                self.journal, self.run, attempt, tid,
                max(0.0, wake_at - float(start["at"]),
                    now - float(start["at"])))
            if now >= wake_at:
                return
            yield Step("SLEEP", (wake_at, attempt, tid, float(start["at"])))
            return

        anchor = float(receipt.get("anchor_at", receipt["at"]))
        now = _now(self.journal)
        wake_at = anchor + seconds
        if now >= wake_at:
            record_sleep_done(self.journal, self.run, attempt, tid, 0.0)
            return
        record_sleep_start(self.journal, self.run, attempt, tid, wake_at)
        yield Step("SLEEP", (wake_at, attempt, tid, anchor))


# -- drivers -------------------------------------------------------------
def _is_retryable_exception(exc, exception_types) -> bool:
    if exception_types is None:
        return False
    return isinstance(exc, exception_types)


def _return_payload(payload):
    value, exception, raise_on = payload
    if value is FAILED:
        if exception is not None and raise_on:
            raise exception
        return None
    if exception is not None and raise_on:
        raise exception
    return value


def _invoke_target_sync(target, args, kwargs, run, tid):
    from backoff._durable import (
        _current_attempt_id,
        _current_key,
        _current_layer,
    )
    key_token = _current_key.set(run.key)
    layer_token = _current_layer.set(run.layer)
    attempt_token = _current_attempt_id.set(tid)
    try:
        return target(*args, **kwargs)
    finally:
        _current_attempt_id.reset(attempt_token)
        _current_layer.reset(layer_token)
        _current_key.reset(key_token)


async def _invoke_target_async(target, args, kwargs, run, tid):
    from backoff._durable import (
        _current_attempt_id,
        _current_key,
        _current_layer,
    )
    key_token = _current_key.set(run.key)
    layer_token = _current_layer.set(run.layer)
    attempt_token = _current_attempt_id.set(tid)
    try:
        return await target(*args, **kwargs)
    finally:
        _current_attempt_id.reset(attempt_token)
        _current_layer.reset(layer_token)
        _current_key.reset(key_token)


def run_sync(*, journal, context, target, args, kwargs, **machine_kwargs):
    jnl, opened = resolve_journal(journal if journal is not None
                                  else context.journal)
    if jnl is None:
        raise ValueError("resumable execution requires a journal")
    machine = Machine(journal=jnl, context=context, target=target, args=args,
                      kwargs=kwargs, **machine_kwargs)
    try:
        steps = machine.steps()
        while True:
            try:
                step = next(steps)
            except StopIteration:
                return None
            if step.kind == "INVOKE":
                attempt, tid = step.payload
                try:
                    value = _invoke_target_sync(target, args, kwargs,
                                                machine.run, tid)
                    exception = None
                except BaseException as exc:
                    if not _is_retryable_exception(
                            exc, machine.exception_types):
                        raise
                    value, exception = None, exc
                record_result(jnl, machine.run, attempt, tid, value=value,
                              exception=exception,
                              elapsed=machine._entry_elapsed(tid))
            elif step.kind == "CALLBACK":
                handler, details, phase, attempt, tid, index = step.payload
                handler(details)
            elif step.kind == "SLEEP":
                wake_at, attempt, tid, anchor = step.payload
                clock = context.clock or jnl.clock
                sleep = context.sleep or time.sleep
                started = float(clock())
                remaining = wake_at - started
                if remaining > 0:
                    sleep(remaining)
                record_sleep_done(jnl, machine.run, attempt, tid,
                                  max(0.0, float(clock()) - started))
            elif step.kind == "RETURN":
                return _return_payload(step.payload)
    finally:
        if opened:
            jnl.close()


async def run_async(*, journal, context, target, args, kwargs,
                    **machine_kwargs):
    import asyncio

    jnl, opened = resolve_journal(journal if journal is not None
                                  else context.journal)
    if jnl is None:
        raise ValueError("resumable execution requires a journal")
    machine = Machine(journal=jnl, context=context, target=target, args=args,
                      kwargs=kwargs, **machine_kwargs)
    try:
        steps = machine.steps()
        while True:
            try:
                step = next(steps)
            except StopIteration:
                return None
            if step.kind == "INVOKE":
                attempt, tid = step.payload
                try:
                    value = await _invoke_target_async(target, args, kwargs,
                                                       machine.run, tid)
                    exception = None
                except BaseException as exc:
                    if not _is_retryable_exception(
                            exc, machine.exception_types):
                        raise
                    value, exception = None, exc
                record_result(jnl, machine.run, attempt, tid, value=value,
                              exception=exception,
                              elapsed=machine._entry_elapsed(tid))
            elif step.kind == "CALLBACK":
                handler, details, phase, attempt, tid, index = step.payload
                result = handler(details)
                if hasattr(result, "__await__"):
                    await result
            elif step.kind == "SLEEP":
                wake_at, attempt, tid, anchor = step.payload
                clock = context.clock or jnl.clock
                sleep = context.sleep or asyncio.sleep
                started = float(clock())
                remaining = wake_at - started
                if remaining > 0:
                    await sleep(remaining)
                record_sleep_done(jnl, machine.run, attempt, tid,
                                  max(0.0, float(clock()) - started))
            elif step.kind == "RETURN":
                return _return_payload(step.payload)
    finally:
        if opened:
            jnl.close()
