import asyncio
import threading

import pytest

import janus


class LoopThread:
    """An event loop driven by its own dedicated thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> "LoopThread":
        self._thread.start()
        self._ready.wait()
        return self

    def submit(self, coro: object) -> object:
        return asyncio.run_coroutine_threadsafe(
            coro,
            self.loop,  # type: ignore[arg-type]
        )

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()
        self.loop.close()


class hold_wakeups:
    """Capture (and withhold) call_soon_threadsafe wake-up callbacks.

    The loop thread keeps running; only the selected callbacks are kept
    undelivered, which deterministically models a wake-up that has been
    scheduled but has not had its turn to execute.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._original = loop.call_soon_threadsafe
        self.captured: list[tuple[object, tuple]] = []

    def __enter__(self) -> "hold_wakeups":
        original = self._original

        def capture(callback: object, *args: object) -> None:
            if getattr(callback, "__name__", "") == "_setup_async_notifier":
                self.captured.append((callback, args))
            else:
                original(callback, *args)

        self._loop.call_soon_threadsafe = capture  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.call_soon_threadsafe = self._original  # type: ignore[assignment]


class freeze_old_loop:
    """Withhold every cross-thread callback until the context exits.

    This freezes handover finalization at a stable point so the test can
    intervene (e.g. shut the queue down) mid-handover.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._original = loop.call_soon_threadsafe
        self.captured: list[tuple[object, tuple]] = []

    def __enter__(self) -> "freeze_old_loop":
        self._loop.call_soon_threadsafe = self._capture  # type: ignore[assignment]
        return self

    def _capture(self, callback: object, *args: object) -> None:
        self.captured.append((callback, args))

    def __exit__(self, *exc: object) -> None:
        self._loop.call_soon_threadsafe = self._original  # type: ignore[assignment]
        for callback, args in self.captured:
            self._original(callback, *args)
        self.captured.clear()


class TestRebindEventLoop:
    def test_rebind_preserves_queue_and_sync_proxy(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue()).result(timeout=5)

            for item in range(5):
                q.sync_q.put(item)

            report = new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            assert report.pending_in_queue == (0, 1, 2, 3, 4)
            assert q.sync_q.qsize() == 5

            items = new_loop.submit(_drain(q)).result(timeout=5)
            assert items == [0, 1, 2, 3, 4]
            assert q.sync_q.unfinished_tasks == 0
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_rebind_without_prior_binding(self):
        new_loop = LoopThread().start()
        try:
            q = janus.Queue()
            q.sync_q.put("keep-me")
            report = new_loop.submit(q.rebind_event_loop()).result(timeout=5)
            assert report.generation == 1
            assert report.not_enqueued == ()
            assert report.pending_in_queue == ("keep-me",)
            assert report.taken_unconfirmed == ()
            assert report.confirmed == ()

            assert new_loop.submit(_read_one(q)).result(timeout=5) == "keep-me"
        finally:
            new_loop.stop()

    def test_abc_handover_full_scenario(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue(maxsize=1)).result(timeout=5)

            # Order A is taken by the old writer but never confirmed.
            q.sync_q.put("A")
            item_a = old_loop.submit(q.async_q.get()).result(timeout=5)
            assert item_a == "A"

            # An old-loop async getter is parked while the queue is empty.
            stale_getter = old_loop.submit(q.async_q.get())
            old_loop.submit(_wait_for_not_empty(q, 1)).result(timeout=5)

            # Withhold B's wake-up so it never reaches the parked getter.
            with hold_wakeups(old_loop.loop) as held:
                q.sync_q.put("B")
                assert len(held.captured) == 1

                # Another thread is blocked trying to admit C.
                put_c_done = threading.Event()

                def put_c() -> None:
                    q.sync_q.put("C")
                    put_c_done.set()

                putter_thread = threading.Thread(target=put_c)
                putter_thread.start()
                assert not put_c_done.wait(timeout=0.2)

                report = new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            # The withheld wake-up must not hand B to the old getter.
            with pytest.raises(janus.QueueRebindError):
                stale_getter.result(timeout=5)
            assert q.sync_q.qsize() == 1
            assert report.stale_notifications >= 1
            assert report.taken_unconfirmed == ("A",)
            assert report.pending_in_queue == ("B",)
            assert report.confirmed == ()
            assert report.not_enqueued == ()

            # B must reach the new writer before the admitted C.
            item_b = new_loop.submit(q.async_q.get()).result(timeout=5)
            assert item_b == "B"
            assert put_c_done.wait(timeout=5)
            item_c = new_loop.submit(q.async_q.get()).result(timeout=5)
            assert item_c == "C"

            # A's late acknowledgement only settles A, not B or C.
            q.sync_q.task_done()
            assert q.sync_q.unfinished_tasks == 2
            q.sync_q.task_done()
            assert q.sync_q.unfinished_tasks == 1
            q.sync_q.task_done()
            assert q.sync_q.unfinished_tasks == 0

            putter_thread.join(timeout=5)
            assert not putter_thread.is_alive()
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_blocked_async_putter_item_is_rejected(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue(maxsize=1)).result(timeout=5)
            q.sync_q.put("X")

            putter = old_loop.submit(q.async_q.put("D"))
            old_loop.submit(_wait_for_not_full(q, 1)).result(timeout=5)

            report = new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            with pytest.raises(janus.ItemNotDeliveredError):
                putter.result(timeout=5)
            assert report.not_enqueued == ("D",)
            assert q.sync_q.qsize() == 1

            assert new_loop.submit(_read_one(q)).result(timeout=5) == "X"
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_report_confirmed_and_pending_categories(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue(maxsize=1)).result(timeout=5)
            q.sync_q.put("Q1")
            q1 = old_loop.submit(q.async_q.get()).result(timeout=5)
            assert q1 == "Q1"
            q.sync_q.put("Q2")
            rejected_putter = old_loop.submit(q.async_q.put("R"))
            old_loop.submit(_wait_for_not_full(q, 1)).result(timeout=5)
            q.sync_q.task_done()  # confirm Q1 before handover

            report = new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            with pytest.raises(janus.ItemNotDeliveredError):
                rejected_putter.result(timeout=5)
            assert report.confirmed == ("Q1",)
            assert report.pending_in_queue == ("Q2",)
            assert report.not_enqueued == ("R",)
            assert report.taken_unconfirmed == ()
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_sync_getter_keeps_working_across_handover(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue()).result(timeout=5)

            got: list[str] = []
            getter_done = threading.Event()

            def sync_getter() -> None:
                got.append(q.sync_q.get())
                getter_done.set()

            thread = threading.Thread(target=sync_getter)
            thread.start()

            new_loop.submit(q.rebind_event_loop()).result(timeout=5)
            new_loop.submit(_put_one(q, "from-new-loop")).result(timeout=5)

            assert getter_done.wait(timeout=5)
            assert got == ["from-new-loop"]
            thread.join(timeout=5)
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_old_loop_calls_raise_after_handover(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue()).result(timeout=5)
            q.sync_q.put("still-here")
            new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            with pytest.raises(janus.QueueRebindError):
                old_loop.submit(_get_nowait(q)).result(timeout=5)
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_two_loop_runs_preserve_order(self):
        first_loop = LoopThread().start()
        second_loop = LoopThread().start()
        try:
            q = first_loop.submit(_make_queue()).result(timeout=5)
            total = 10
            consumed: list[int] = []
            consumed_lock = threading.Lock()

            def produce() -> None:
                for item in range(total):
                    q.sync_q.put(item)

            producer = threading.Thread(target=produce)
            producer.start()

            first_loop.submit(_consume(q, 4, consumed, consumed_lock)).result(5)

            handover = second_loop.submit(q.rebind_event_loop())
            awaitable = second_loop.submit(q.wait_rebind())
            report = handover.result(timeout=5)
            awaitable.result(timeout=5)
            assert report.generation == 1

            second_loop.submit(_consume(q, 6, consumed, consumed_lock)).result(5)
            producer.join(timeout=5)

            assert consumed == list(range(total))
            assert q.sync_q.unfinished_tasks == 0
            second_loop.submit(q.aclose()).result(timeout=5)
            assert q.closed
        finally:
            first_loop.stop()
            second_loop.stop()

    def test_shutdown_during_handover(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue(maxsize=1)).result(timeout=5)

            # Fill the single slot so the putter blocks on not_full.
            q.sync_q.put("queued")
            waiting_putter = old_loop.submit(q.async_q.put("extra"))
            old_loop.submit(_wait_for_not_full(q, 1)).result(timeout=5)

            with freeze_old_loop(old_loop.loop):
                handover = new_loop.submit(q.rebind_event_loop())
                _wait_until(q, lambda queue: queue._rebind_barrier is not None)
                # Stop work while the handover is still in progress.
                q.shutdown(immediate=True)

            report = handover.result(timeout=5)
            with pytest.raises(janus.ItemNotDeliveredError):
                waiting_putter.result(timeout=5)

            binding = q._retired_bindings[-1]
            assert binding.active_callers == 0
            assert binding.active_waits == 0
            assert len(binding.pending) == 0
            assert binding.queued_wakeups == 0
            assert binding.not_full_waiting == 0
            assert report.pending_in_queue == ()

            with pytest.raises(janus.AsyncQueueShutDown):
                new_loop.submit(_get_nowait(q)).result(timeout=5)
            new_loop.submit(q.wait_closed()).result(timeout=5)
            assert q.closed
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_no_hanging_state_after_old_loop_stops(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue()).result(timeout=5)
            q.sync_q.put("a")
            q.sync_q.put("b")

            old_binding = q._binding
            new_loop.submit(q.rebind_event_loop()).result(timeout=5)
            old_loop.stop()

            assert old_binding.active_callers == 0
            assert len(old_binding.pending) == 0
            assert old_binding.queued_wakeups == 0

            new_loop.submit(_drain(q)).result(timeout=5)
            new_loop.submit(q.aclose()).result(timeout=5)
            assert q.closed
            assert q.sync_q.unfinished_tasks == 0
        finally:
            new_loop.stop()

    def test_rebind_validation_errors(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            q = old_loop.submit(_make_queue()).result(timeout=5)

            with pytest.raises(RuntimeError, match="already bound"):
                old_loop.submit(q.rebind_event_loop()).result(timeout=5)

            new_loop.submit(q.rebind_event_loop()).result(timeout=5)
            with pytest.raises(RuntimeError, match="already bound"):
                new_loop.submit(q.rebind_event_loop()).result(timeout=5)

            q.shutdown()
            with pytest.raises(RuntimeError, match="shut-down queue"):
                new_loop.submit(q.rebind_event_loop()).result(timeout=5)
        finally:
            old_loop.stop()
            new_loop.stop()

    def test_successive_rebinds_keep_data_and_drop_dead_bindings(self):
        loop_one = LoopThread().start()
        loop_two = LoopThread().start()
        loop_three = LoopThread().start()
        try:
            q = loop_one.submit(_make_queue()).result(timeout=5)
            q.sync_q.put("persisted")

            report_one = loop_two.submit(q.rebind_event_loop()).result(5)
            assert report_one.generation == 1
            loop_one.stop()

            report_two = loop_three.submit(q.rebind_event_loop()).result(5)
            assert report_two.generation == 2
            assert report_two.pending_in_queue == ("persisted",)
            # The first loop is stopped, so its retired binding is reclaimed.
            assert all(not binding.loop.is_closed() for binding in q._retired_bindings)

            assert loop_three.submit(_read_one(q)).result(5) == "persisted"
            q.sync_q.task_done()
            loop_three.submit(q.aclose()).result(5)
        finally:
            loop_two.stop()
            loop_three.stop()

    def test_lifo_and_priority_queues_keep_organization(self):
        old_loop = LoopThread().start()
        new_loop = LoopThread().start()
        try:
            lifo = janus.LifoQueue()
            old_loop.submit(_bind(lifo)).result(5)
            for item in (1, 2, 3):
                lifo.sync_q.put(item)
            report = new_loop.submit(lifo.rebind_event_loop()).result(5)
            assert report.pending_in_queue == (3, 2, 1)
            assert new_loop.submit(_drain(lifo)).result(5) == [3, 2, 1]
            new_loop.submit(lifo.aclose()).result(5)

            priority = janus.PriorityQueue()
            old_loop.submit(_bind(priority)).result(5)
            for item in (3, 1, 2):
                priority.sync_q.put(item)
            report = new_loop.submit(priority.rebind_event_loop()).result(5)
            assert report.pending_in_queue == (1, 2, 3)
            assert new_loop.submit(_drain(priority)).result(5) == [1, 2, 3]
            new_loop.submit(priority.aclose()).result(5)
        finally:
            old_loop.stop()
            new_loop.stop()


async def _make_queue(maxsize: int = 0) -> janus.Queue:
    q = janus.Queue(maxsize)
    q._get_loop()
    return q


async def _bind(q: janus.Queue) -> None:
    q._get_loop()


async def _drain(q: janus.Queue) -> list:
    items = []
    while not q.async_q.empty():
        items.append(await q.async_q.get())
        q.async_q.task_done()
    return items


async def _consume(q: janus.Queue, limit: int, out: list, lock) -> None:
    for _ in range(limit):
        item = await q.async_q.get()
        with lock:
            out.append(item)
        q.async_q.task_done()


async def _read_one(q: janus.Queue) -> object:
    return await q.async_q.get()


async def _put_one(q: janus.Queue, item: object) -> None:
    await q.async_q.put(item)


async def _get_nowait(q: janus.Queue) -> object:
    return q.async_q.get_nowait()


async def _wait_for_not_empty(q: janus.Queue, count: int) -> None:
    binding = q._binding
    while binding.not_empty_waiting != count:
        await asyncio.sleep(0.001)


async def _wait_for_not_full(q: janus.Queue, count: int) -> None:
    binding = q._binding
    while binding.not_full_waiting != count:
        await asyncio.sleep(0.001)


def _wait_until(q: janus.Queue, predicate) -> None:
    import time

    deadline = time.monotonic() + 5
    while not predicate(q):
        if time.monotonic() > deadline:
            raise TimeoutError("condition was not met in time")
        time.sleep(0.001)
