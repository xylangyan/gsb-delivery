from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Optional, Union

from redis.exceptions import WatchError

from .scheduling import ScheduleLedger, Segment, collection_job_id


class RedisScheduleStore:
    """Optimistic JSON snapshot store for a :class:`ScheduleLedger`."""

    def __init__(self, redis: Any, key: str = 'arq:schedule-ledger') -> None:
        self.redis = redis
        self.key = key

    async def load(self) -> ScheduleLedger:
        raw = await self.redis.get(self.key)
        return self._from_raw(raw)

    @staticmethod
    def _from_raw(raw: Any) -> ScheduleLedger:
        if raw is None:
            return ScheduleLedger()
        if isinstance(raw, bytes):
            raw = raw.decode()
        return ScheduleLedger.from_dict(json.loads(raw))

    async def save(self, ledger: ScheduleLedger) -> None:
        payload = json.dumps(ledger.to_dict(), default=str, ensure_ascii=False)
        await self.redis.set(self.key, payload)

    async def update(
        self,
        mutation: Union[Callable[[ScheduleLedger], Any], Callable[[ScheduleLedger], Awaitable[Any]]],
        *,
        max_retries: int = 10,
    ) -> Any:
        for _ in range(max_retries):
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(self.key)
                    ledger = self._from_raw(await pipe.get(self.key))
                    result = mutation(ledger)
                    if hasattr(result, '__await__'):
                        result = await result
                    payload = json.dumps(ledger.to_dict(), default=str, ensure_ascii=False)
                    pipe.multi()
                    pipe.set(self.key, payload)
                    await pipe.execute()
                    return result
                except WatchError:
                    continue
        raise WatchError(f'could not update schedule ledger after {max_retries} retries')


class RedisScheduleLedger:
    """Async facade that applies each ledger mutation through :class:`RedisScheduleStore`."""

    def __init__(self, redis: Any, key: str = 'arq:schedule-ledger') -> None:
        self.store = RedisScheduleStore(redis, key)

    async def load(self) -> ScheduleLedger:
        return await self.store.load()

    async def publish_plan(self, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.publish_plan(*args, **kwargs))

    async def claim(self, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.claim(*args, **kwargs))

    async def record_job_report(self, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.record_job_report(*args, **kwargs))

    async def record_delivery(self, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.record_delivery(*args, **kwargs))

    async def record_receipt(self, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.record_receipt(*args, **kwargs))

    acknowledge = record_receipt
    ack = record_receipt

    async def release_claim(self, claim_id: str, *args: Any, **kwargs: Any) -> Any:
        return await self.store.update(lambda ledger: ledger.release_claim(claim_id, *args, **kwargs))

    async def view(self, *args: Any, **kwargs: Any) -> Any:
        ledger = await self.load()
        return ledger.view(*args, **kwargs)

    async def needs_reschedule(self, *args: Any, **kwargs: Any) -> Any:
        ledger = await self.load()
        return ledger.needs_reschedule(*args, **kwargs)

    async def settled_segments(self, *args: Any, **kwargs: Any) -> Any:
        ledger = await self.load()
        return ledger.settled_segments(*args, **kwargs)

    async def held_segments(self, *args: Any, **kwargs: Any) -> Any:
        ledger = await self.load()
        return ledger.held_segments(*args, **kwargs)


AsyncScheduleLedger = RedisScheduleLedger


async def enqueue_pending_windows(
    redis: Any,
    ledger: ScheduleLedger,
    function: str,
    *,
    plan_version: Optional[Union[str, int]] = None,
    queue_name: Optional[str] = None,
    max_length: Optional[int] = None,
    prefix: str = 'collect',
    enqueue_kwargs: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """Enqueue unsettled intervals that are not already covered by a live claim."""

    plan = ledger.current_plan() if plan_version is None else ledger.plans[str(plan_version)]
    pending = ledger.pending_segments(plan, include_held=False)
    jobs = []
    extra = dict(enqueue_kwargs or {})
    if queue_name is not None:
        extra['_queue_name'] = queue_name

    for segment in pending:
        for window in _chunk(segment, max_length):
            job_id = collection_job_id(
                segment.source_batch,
                window.start,
                window.end,
                site=segment.site,
                prefix=prefix,
            )
            jobs.append(
                await redis.enqueue_job(
                    function,
                    segment.site,
                    segment.source_batch,
                    window.start,
                    window.end,
                    _job_id=job_id,
                    **extra,
                )
            )
    return jobs


def _chunk(segment: Segment, max_length: Optional[int]) -> list:
    if max_length is None or segment.range.length <= max_length:
        return [segment.range]
    return [
        type(segment.range)(start, min(start + max_length, segment.end))
        for start in range(segment.start, segment.end, max_length)
    ]
