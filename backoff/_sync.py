# coding:utf-8
import datetime
import functools
import time
from datetime import timedelta

from backoff._common import (_init_wait_gen, _maybe_call, _next_wait)
from backoff._durable import current_resume_context
from backoff._machine import run_sync


def _call_handlers(hdlrs, target, args, kwargs, tries, elapsed, **extra):
    details = {
        'target': target,
        'args': args,
        'kwargs': kwargs,
        'tries': tries,
        'elapsed': elapsed,
    }
    details.update(extra)
    for hdlr in hdlrs:
        hdlr(details)


def _durable(target, args, kwargs, context, *, wait_gen, max_tries, max_time,
             jitter, giveup=None, predicate=None, on_success=(), on_backoff=(),
             on_giveup=(), raise_on_giveup=True, wait_gen_kwargs=None,
             exception_types=None):
    return run_sync(
        journal=context.journal,
        context=context,
        target=target,
        args=args,
        kwargs=kwargs,
        wait_gen=wait_gen,
        max_tries=max_tries,
        max_time=max_time,
        jitter=jitter,
        giveup=giveup,
        predicate=predicate,
        on_success=on_success,
        on_backoff=on_backoff,
        on_giveup=on_giveup,
        raise_on_giveup=raise_on_giveup,
        wait_gen_kwargs=wait_gen_kwargs or {},
        exception_types=exception_types,
    )


def retry_predicate(target, wait_gen, predicate,
                    *,
                    max_tries, max_time, jitter,
                    on_success, on_backoff, on_giveup,
                    wait_gen_kwargs):

    @functools.wraps(target)
    def retry(*args, **kwargs):
        context = current_resume_context()
        if context is not None:
            return _durable(
                target, args, kwargs, context, wait_gen=wait_gen,
                max_tries=max_tries, max_time=max_time, jitter=jitter,
                predicate=predicate, on_success=on_success,
                on_backoff=on_backoff, on_giveup=on_giveup,
                raise_on_giveup=True, wait_gen_kwargs=wait_gen_kwargs,
            )

        max_tries_value = _maybe_call(max_tries)
        max_time_value = _maybe_call(max_time)

        tries = 0
        start = datetime.datetime.now()
        wait = _init_wait_gen(wait_gen, wait_gen_kwargs)
        while True:
            tries += 1
            elapsed = timedelta.total_seconds(datetime.datetime.now() - start)
            details = {
                "target": target,
                "args": args,
                "kwargs": kwargs,
                "tries": tries,
                "elapsed": elapsed,
            }

            ret = target(*args, **kwargs)
            if predicate(ret):
                max_tries_exceeded = (tries == max_tries_value)
                max_time_exceeded = (max_time_value is not None and
                                     elapsed >= max_time_value)

                if max_tries_exceeded or max_time_exceeded:
                    _call_handlers(on_giveup, **details, value=ret)
                    break

                try:
                    seconds = _next_wait(wait, ret, jitter, elapsed,
                                         max_time_value)
                except StopIteration:
                    _call_handlers(on_giveup, **details)
                    break

                _call_handlers(on_backoff, **details,
                               value=ret, wait=seconds)

                time.sleep(seconds)
                continue
            else:
                _call_handlers(on_success, **details, value=ret)
                break

        return ret

    return retry


def retry_exception(target, wait_gen, exception,
                    *,
                    max_tries, max_time, jitter, giveup,
                    on_success, on_backoff, on_giveup, raise_on_giveup,
                    wait_gen_kwargs):

    @functools.wraps(target)
    def retry(*args, **kwargs):
        context = current_resume_context()
        if context is not None:
            return _durable(
                target, args, kwargs, context, wait_gen=wait_gen,
                max_tries=max_tries, max_time=max_time, jitter=jitter,
                giveup=giveup, on_success=on_success,
                on_backoff=on_backoff, on_giveup=on_giveup,
                raise_on_giveup=raise_on_giveup,
                wait_gen_kwargs=wait_gen_kwargs,
                exception_types=exception,
            )

        max_tries_value = _maybe_call(max_tries)
        max_time_value = _maybe_call(max_time)

        tries = 0
        start = datetime.datetime.now()
        wait = _init_wait_gen(wait_gen, wait_gen_kwargs)
        while True:
            tries += 1
            elapsed = timedelta.total_seconds(datetime.datetime.now() - start)
            details = {
                "target": target,
                "args": args,
                "kwargs": kwargs,
                "tries": tries,
                "elapsed": elapsed,
            }

            try:
                ret = target(*args, **kwargs)
            except exception as e:
                max_tries_exceeded = (tries == max_tries_value)
                max_time_exceeded = (max_time_value is not None and
                                     elapsed >= max_time_value)

                if giveup(e) or max_tries_exceeded or max_time_exceeded:
                    _call_handlers(on_giveup, **details, exception=e)
                    if raise_on_giveup:
                        raise
                    return None

                try:
                    seconds = _next_wait(wait, e, jitter, elapsed,
                                         max_time_value)
                except StopIteration:
                    _call_handlers(on_giveup, **details, exception=e)
                    raise e

                _call_handlers(on_backoff, **details, wait=seconds,
                               exception=e)

                time.sleep(seconds)
            else:
                _call_handlers(on_success, **details)

                return ret
    return retry
