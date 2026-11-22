import random
import threading
import time
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from zict import Buffer, LRU, Retention, RetentionError
from zict.tests import utils_test


def test_retention_error_is_keyerror():
    assert issubclass(RetentionError, KeyError)
    lru = LRU(10, {})
    with pytest.raises(KeyError):
        lru.retain(["nope"])


def test_retain_empty():
    lru = LRU(10, {"x": 1})
    handle = lru.retain(())
    assert isinstance(handle, Retention)
    assert handle.keys == frozenset()
    handle.release()
    assert not lru._retentions


def test_retain_protects_group_from_eviction():
    lru = LRU(10, {}, weight=lambda k, v: v)
    lru.set_noevict("x", 6)
    lru.set_noevict("y", 4)
    handle = lru.retain(["x", "y"])

    # No evictable key can make room, so the insertion is rejected and existing data
    # is left untouched.
    with pytest.raises(RetentionError):
        lru["z"] = 1
    assert dict(lru) == {"x": 6, "y": 4}
    assert lru.total_weight == 10

    handle.release()
    lru["z"] = 1  # x (6) is now evictable and gets evicted first (LRU head)
    assert "x" not in lru
    assert dict(lru) == {"y": 4, "z": 1}


def test_insert_evicts_non_retained_to_make_room():
    lru = LRU(10, {}, weight=lambda k, v: v)
    lru.set_noevict("x", 6)
    lru.set_noevict("y", 4)
    handle = lru.retain(["y"])

    lru["z"] = 5  # evict x (6), keep the retained y
    assert "x" not in lru
    assert lru["y"] == 4
    assert lru["z"] == 5
    handle.release()


def test_retain_atomic_when_one_key_missing():
    lru = LRU(100, {"x": 1, "y": 2}, weight=lambda k, v: v)
    with pytest.raises(RetentionError) as exc:
        lru.retain(["x", "z"])
    assert exc.value.args[0] == "z"
    # Nothing from the group was retained: x is still freely evictable.
    assert not lru._retentions
    assert lru.evict()[0] == "x"


def test_retain_missing_after_delete():
    lru = LRU(100, {"x": 1})
    del lru["x"]
    with pytest.raises(RetentionError):
        lru.retain(["x"])
    assert not lru._retentions


def test_delete_while_retained_is_treated_as_missing():
    lru = LRU(100, {"x": 1, "y": 2})
    handle = lru.retain(["x", "y"])

    del lru["x"]
    assert "x" not in lru
    with pytest.raises(RetentionError):
        lru.retain(["x"])

    handle.release()  # must not resurrect the deleted key
    assert "x" not in lru
    assert not lru._retentions


def test_reentrant_retention():
    lru = LRU(2, {"x": 1, "y": 2})
    handle_a = lru.retain(["x", "y"])
    with pytest.raises(RetentionError):
        lru["z"] = 3  # nothing evictable
    assert set(lru) == {"x", "y"}

    handle_b = lru.retain(["x"])  # another job also protects x
    handle_a.release()  # y becomes free, x stays protected by handle_b

    lru["z"] = 3  # y is evicted; x survives
    assert set(lru) == {"x", "z"}

    # An evictable sibling (z) can still be reclaimed to fit w, but x cannot.
    lru["w"] = 4
    assert "x" in lru
    assert "z" not in lru
    with pytest.raises(RetentionError):
        lru.evict("x")  # x still pinned by handle_b

    handle_b.release()
    assert lru.evict("x")[0] == "x"  # x is evictable again


def test_retention_context_manager_releases_on_exception():
    lru = LRU(2, {"x": 1, "y": 2})
    with lru.retain(["x", "y"]) as handle:
        with pytest.raises(RetentionError):
            lru["z"] = 3
    assert not lru._retentions

    with pytest.raises(RuntimeError):
        with lru.retain(["x"]):
            raise RuntimeError("boom")
    assert not lru._retentions

    lru["z"] = 3
    assert "x" not in lru


def test_release_is_idempotent():
    lru = LRU(10, {"x": 1})
    handle = lru.retain(["x"])
    handle.release()
    handle.release()  # no-op, must not raise or corrupt the counts
    assert not lru._retentions


def test_stale_handle_release_does_not_unpin_new_retention():
    lru = LRU(10, {"x": 1, "y": 2}, weight=lambda k, v: v)
    old_handle = lru.retain(["x"])

    del lru["x"]  # the key disappears while job A still holds a handle
    lru.set_noevict("x", 1)  # ...and comes back
    new_handle = lru.retain(["x"])  # protected by a different job

    old_handle.release()  # job A unwinds; it must not undo the new retention
    with pytest.raises(RetentionError):
        lru.evict("x")

    new_handle.release()
    assert lru.evict("x")[0] == "x"
    assert not lru._retentions


def test_retain_and_release_are_not_reads():
    lru = LRU(100, {"a": 1, "b": 2, "c": 3}, weight=lambda k, v: v)
    before = list(lru.order)
    handle = lru.retain(["b"])
    assert list(lru.order) == before
    handle.release()
    assert list(lru.order) == before
    # Eviction still follows the original LRU order.
    assert lru.evict()[0] == "a"


def test_evict_retained_raises_and_is_skipped_automatically():
    lru = LRU(10, {"x": 1, "y": 2, "z": 3}, weight=lambda k, v: v)
    handle = lru.retain(["x"])

    with pytest.raises(RetentionError):
        lru.evict("x")
    # Automatic victim selection skips the retained x and takes the next LRU key.
    assert lru.evict()[0] == "y"

    handle.release()
    assert lru.evict()[0] == "x"


def test_overwrite_retained_key_keeps_retention():
    lru = LRU(10, {}, weight=lambda k, v: v)
    lru.set_noevict("x", 4)
    lru.set_noevict("y", 4)
    handle = lru.retain(["x"])

    lru.set_noevict("x", 6)  # overwrite: retention must survive
    with pytest.raises(RetentionError):
        lru.evict("x")
    assert lru["x"] == 6
    handle.release()
    assert lru.evict("x")[0] == "x"  # x is evictable again after release


def test_retain_mid_eviction_cancels_and_cleans_stale_copy():
    """LRU-level version of the headline scenario: a retention acquired while the
    eviction callback is still running cancels the eviction and triggers the cleanup
    callbacks, without waiting for the slow layer.
    """
    ev_started = threading.Event()
    release_ev = threading.Event()
    copies = []
    cancelled = []

    def on_evict(k, v):
        copies.append((k, v))  # hand a copy to the slow layer
        if k == "x":
            ev_started.set()
            assert release_ev.wait(timeout=5)

    def on_cancel_evict(k, v):
        cancelled.append((k, v))
        copies.remove((k, v))  # drop the stale migration copy

    lru = LRU(
        10,
        {},
        on_evict=on_evict,
        on_cancel_evict=on_cancel_evict,
        weight=lambda k, v: v,
    )
    lru.set_noevict("x", 5)
    lru.set_noevict("y", 5)

    with ThreadPoolExecutor(1) as ex:
        future = ex.submit(lru.evict)  # starts evicting x
        assert ev_started.wait(timeout=5)
        # The callback is still hanging, but x is still readable from the fast layer.
        assert lru["x"] == 5

        handle_a = lru.retain(["x", "y"])
        handle_b = lru.retain(["x"])
        handle_a.release()  # job A exits; x stays protected by the other job

        lru["z"] = 5  # pressure evicts the now-unprotected y, never x
        assert "x" in lru
        assert "y" not in lru

        release_ev.set()  # let the old, hanging eviction callback return
        assert future.result() == (None, None, 0)

    # x was never deleted and its stale slow-layer copy was cleaned up.
    assert lru.d == {"x": 5, "z": 5}
    assert cancelled == [("x", 5)]
    assert ("x", 5) not in copies
    handle_b.release()
    assert lru.evict()[0] == "x"


def test_capacity_rejects_insert_with_inflight_eviction():
    ev_started = threading.Event()
    release_ev = threading.Event()

    def on_evict(k, v):
        ev_started.set()
        assert release_ev.wait(timeout=5)

    lru = LRU(10, {}, on_evict=on_evict, weight=lambda k, v: v)
    lru.set_noevict("x", 6)
    lru.set_noevict("y", 4)

    with ThreadPoolExecutor(1) as ex:
        future = ex.submit(lru.evict)  # evicting x (6); its space is still occupied
        assert ev_started.wait(timeout=5)
        handle = lru.retain(["y"])  # pin y; nothing is safely reclaimable now

        with pytest.raises(RetentionError):
            lru["z"] = 5
        assert dict(lru) == {"x": 6, "y": 4}

        release_ev.set()
        assert future.result() == ("x", 6, 6)
        handle.release()


def test_buffer_retain_during_inflight_eviction():
    """End-to-end Buffer scenario.

    Job A retains x and y while x's fast-to-slow callback is hanging. A second job also
    retains x. After A exits and capacity pressure is applied, the old callback returns:
    x must survive in fast, its stale slow copy must be removed, and releasing the last
    handle restores ordinary LRU eviction.
    """
    ev_started = threading.Event()
    release_ev = threading.Event()

    class Slow(UserDict):
        def __setitem__(self, k, v):
            super().__setitem__(k, v)
            if k == "x":
                ev_started.set()
                assert release_ev.wait(timeout=5)

    buff = Buffer({}, Slow(), n=10, weight=lambda k, v: v)
    buff.set_noevict("x", 5)
    buff.set_noevict("y", 5)

    with ThreadPoolExecutor(1) as ex:
        future = ex.submit(buff.fast.evict)
        assert ev_started.wait(timeout=5)
        # x has been copied to slow, the callback is hanging, x is still in fast.
        assert "x" in buff.fast
        assert set(buff.slow) == {"x"}

        handle_a = buff.fast.retain(["x", "y"])
        handle_b = buff.fast.retain(["x"])
        handle_a.release()  # job A exits; x is still protected by the other job

        # Capacity pressure: y is now evictable, x is pinned. z fits by evicting y.
        buff["z"] = 5
        assert "x" in buff.fast
        assert "y" not in buff.fast
        assert set(buff.slow) == {"x", "y"}

        release_ev.set()  # let the old x-eviction callback finish
        assert future.result() == (None, None, 0)

    # The old eviction neither deleted the protected x nor left a stale slow copy.
    assert "x" in buff.fast
    assert "x" not in buff.slow
    assert buff["x"] == 5  # served from the fast layer
    assert set(buff.slow) == {"y"}

    handle_b.release()
    # The next eviction follows the current weights and LRU order; x is movable again.
    buff.fast.evict("x")
    assert "x" not in buff.fast
    assert "x" in buff.slow


def test_eviction_after_release_uses_current_lru_order():
    lru = LRU(10, {}, weight=lambda k, v: v)
    lru.set_noevict("a", 2)
    lru.set_noevict("b", 2)
    lru.set_noevict("c", 2)
    handle = lru.retain(["a", "b", "c"])

    _ = lru["a"]  # refresh a while retained; reads still reorder the LRU
    handle.release()

    # b is now the least recently used key, so it is the first to go.
    assert lru.evict()[0] == "b"
    assert lru.evict()[0] == "c"
    assert lru.evict()[0] == "a"


@pytest.mark.stress
@pytest.mark.repeat(utils_test.REPEAT_STRESS_TESTS)
def test_stress_retention_threadsafe():
    """Retained keys must never be evicted and the LRU must stay internally
    consistent while retentions, releases, inserts and evictions interleave.
    """
    lru = LRU(
        5,
        {},
        weight=lambda k, v: v,
        on_evict=lambda k, v: time.sleep(0.001),
        on_cancel_evict=lambda k, v: None,
    )
    for i in range(5):
        lru.set_noevict(i, 1)

    active: list[Retention] = []
    barrier = threading.Barrier(2)
    stop = threading.Event()

    def retainer():
        local_rng = random.Random(1)
        barrier.wait()
        while not stop.is_set():
            keys = local_rng.sample(range(5), local_rng.randint(1, 3))
            try:
                handle = lru.retain(keys)
            except RetentionError:
                continue
            active.append(handle)
            time.sleep(0.001)
            handle.release()

    def writer():
        local_rng = random.Random(2)
        barrier.wait()
        while not stop.is_set():
            key = local_rng.randrange(5)
            value = local_rng.choice([1, 2])
            try:
                lru[key] = value
            except RetentionError:
                continue
            time.sleep(0.001)

    with ThreadPoolExecutor(2) as ex:
        futures = [ex.submit(retainer), ex.submit(writer)]
        time.sleep(1.0)
        stop.set()
        for future in futures:
            future.result()

    # Every retained key is still present; bookkeeping has been fully cleaned up.
    for handle in active:
        for key in handle.keys:
            if key in lru:
                assert key in lru.order and key in lru.weights
    assert not lru._retentions
    assert lru.total_weight == sum(lru.weights.values())
    assert set(lru.order) == set(lru.d)
