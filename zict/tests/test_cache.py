import gc
import itertools
import threading
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from zict import Cache, LRU, WeakValueMapping
from zict.tests import utils_test


class VersionedBackend(dict):
    """Backend with a cheap get_version() and a counter of large value reads.

    Simulates a producer which can update values and atomically report the current
    version without reading the (potentially large) value itself.
    """

    def __init__(self):
        super().__init__()
        self._versions = {}
        self._counter = itertools.count(1)
        self.value_reads = 0
        self.version_reads = 0

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._versions[key] = next(self._counter)

    def __delitem__(self, key):
        super().__delitem__(key)
        self._versions.pop(key, None)

    def __getitem__(self, key):
        self.value_reads += 1
        return super().__getitem__(key)

    def get_version(self, key):
        self.version_reads += 1
        if key not in self._versions:
            raise KeyError(key)
        return self._versions[key]


class SlowVersionedBackend(VersionedBackend):
    """Versioned backend whose large value read can be parked from a test thread."""

    def __init__(self):
        super().__init__()
        self.in_get = threading.Event()
        self.block_get = threading.Event()

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.in_get.set()
        assert self.block_get.wait(timeout=5)
        return value


class Refable:
    """Minimal weak-referenceable value wrapper."""

    nbytes = 1


def byte_size(key, value):
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    return getattr(value, "nbytes", 1)


class WeakStoreBackend(VersionedBackend):
    """Backend which, like File, doesn't keep a strong reference to the objects it
    returns: each deserialized object is only held by its callers."""

    def __init__(self):
        super().__init__()
        self._store = WeakValueMapping()

    def __setitem__(self, key, value):
        self._store[key] = value
        self._versions[key] = next(self._counter)

    def __getitem__(self, key):
        self.value_reads += 1
        return self._store[key]



def test_cache_get_set_del():
    d = Cache({}, {})

    # getitem (bad key)
    with pytest.raises(KeyError):
        d[0]
    assert (d.data, d.cache) == ({}, {})

    # setitem(no update); cache is empty
    d[1] = 10
    assert (d.data, d.cache) == ({1: 10}, {1: 10})

    # getitem; cache is full
    assert d[1] == 10
    assert (d.data, d.cache) == ({1: 10}, {1: 10})

    # getitem; cache is empty
    d.cache.clear()
    assert d[1] == 10
    assert (d.data, d.cache) == ({1: 10}, {1: 10})

    # setitem(update); cache is full
    d[1] = 20
    assert (d.data, d.cache) == ({1: 20}, {1: 20})

    # setitem(update); cache is empty
    d.cache.clear()
    d[1] = 30
    assert (d.data, d.cache) == ({1: 30}, {1: 30})

    # delitem; cache is full
    del d[1]
    assert (d.data, d.cache) == ({}, {})

    # delitem; cache is empty
    d[1] = 10
    d.cache.clear()
    del d[1]
    assert (d.data, d.cache) == ({}, {})

    # delitem (bad key)
    with pytest.raises(KeyError):
        del d[0]
    assert (d.data, d.cache) == ({}, {})


def test_do_not_read_from_data():
    """__len__, __iter__, __contains__, and keys() do not populate the cache"""

    class D(UserDict):
        def __getitem__(self, key):
            raise AssertionError()

    d = Cache(D({1: 10, 2: 20}), {})
    assert len(d) == 2
    assert list(d) == [1, 2]
    assert 1 in d
    assert 3 not in d
    assert d.keys() == {1, 2}
    assert d.cache == {}


def test_no_update_on_set():
    d = Cache({}, {}, update_on_set=False)
    d[1] = 10
    assert (d.data, d.cache) == ({1: 10}, {})
    assert d[1] == 10
    assert (d.data, d.cache) == ({1: 10}, {1: 10})
    d[1] = 20
    assert (d.data, d.cache) == ({1: 20}, {})
    assert d[1] == 20
    assert (d.data, d.cache) == ({1: 20}, {1: 20})


def test_slow_fails():
    """data.__setitem__ raises; e.g. disk full"""

    class D(UserDict):
        def __setitem__(self, key, value):
            if value == "fail":
                self.pop(key, None)
                raise ValueError()
            super().__setitem__(key, value)

    d = Cache(D(), {})

    # setitem(no update); cache is empty
    with pytest.raises(ValueError):
        d[1] = "fail"
    assert (d.data.data, d.cache) == ({}, {})

    # setitem(update); cache is empty
    d[1] = 10
    d.cache.clear()
    assert (d.data.data, d.cache) == ({1: 10}, {})
    with pytest.raises(ValueError):
        d[1] = "fail"
    assert (d.data.data, d.cache) == ({}, {})

    # setitem(update); cache is full
    d[1] = 10
    assert (d.data.data, d.cache) == ({1: 10}, {1: 10})
    with pytest.raises(ValueError):
        d[1] = "fail"
    assert (d.data.data, d.cache) == ({}, {})


def test_weakvaluemapping():
    class C:
        pass

    d = WeakValueMapping()
    a = C()
    d["a"] = a
    assert d["a"] is a
    del a
    gc.collect()  # Needed in pypy
    assert "a" not in d

    # str does not support weakrefs
    b = "bbb"
    d["b"] = b
    assert "b" not in d


def test_mapping():
    """
    Test mapping interface for Cache().
    """
    buff = Cache({}, {})
    utils_test.check_mapping(buff)
    utils_test.check_closing(buff)

    buff.clear()
    assert not buff.cache
    assert not buff.data
    assert not buff._last_updated


@pytest.mark.parametrize("get_when", ("before", "after"))
@pytest.mark.parametrize("set_when", ("before", "after"))
@pytest.mark.parametrize(
    "starts_first,seed,update_on_set",
    [
        ("get", True, False),
        ("set", False, False),
        ("set", False, True),
        ("set", True, False),
        ("set", True, True),
    ],
)
@pytest.mark.parametrize("ends_first", ("get", "set"))
def test_multithread_race_condition_set_get(
    get_when, set_when, starts_first, seed, update_on_set, ends_first
):
    """Test race conditions between __setitem__ and __getitem__ on the same key"""
    in_get = threading.Event()
    block_get = threading.Event()
    in_set = threading.Event()
    block_set = threading.Event()

    class Slow(UserDict):
        def __getitem__(self, k):
            if get_when == "before":
                try:
                    v = self.data[k]
                finally:
                    in_get.set()
                assert block_get.wait(timeout=5)
                return v
            else:
                in_get.set()
                assert block_get.wait(timeout=5)
                return self.data[k]

        def __setitem__(self, k, v):
            if set_when == "before":
                self.data[k] = v
                in_set.set()
                assert block_set.wait(timeout=5)
            else:
                in_set.set()
                assert block_set.wait(timeout=5)
                self.data[k] = v

    z = Cache(Slow(), {}, update_on_set=update_on_set)
    if seed:
        block_set.set()
        z["x"] = 1
        in_set.clear()
        block_set.clear()
        assert z.data.data == {"x": 1}
        assert set(z._last_updated) == {"x"}
        if update_on_set:
            assert z.cache == {"x": 1}
        else:
            assert z.cache == {}

    with ThreadPoolExecutor(2) as ex:
        if starts_first == "get":
            get_fut = ex.submit(z.__getitem__, "x")
            assert in_get.wait(timeout=5)
            set_fut = ex.submit(z.__setitem__, "x", 2)
            assert in_set.wait(timeout=5)
        else:
            set_fut = ex.submit(z.__setitem__, "x", 2)
            assert in_set.wait(timeout=5)
            get_fut = ex.submit(z.__getitem__, "x")
            assert in_get.wait(timeout=5)

        if ends_first == "get":
            block_get.set()
            try:
                assert get_fut.result() in (1, 2)
            except KeyError:
                pass
            block_set.set()
            set_fut.result()
        else:
            block_set.set()
            set_fut.result()
            block_get.set()
            try:
                assert get_fut.result() in (1, 2)
            except KeyError:
                pass

    assert z.data.data == {"x": 2}
    # The cache is either not populated or up to date
    assert z.cache in ({}, {"x": 2})
    assert set(z._last_updated) == {"x"}


@pytest.mark.parametrize("get_when", ("before", "after"))
def test_multithread_race_condition_del_get(get_when):
    """Test race conditions between __delitem__ and __getitem__ on the same key"""
    in_get = threading.Event()
    block_get = threading.Event()

    class Slow(UserDict):
        def __getitem__(self, k):
            if get_when == "before":
                v = self.data[k]
                in_get.set()
                assert block_get.wait(timeout=5)
                return v
            else:
                in_get.set()
                assert block_get.wait(timeout=5)
                return self.data[k]

    z = Cache(Slow(), {}, update_on_set=False)
    z["x"] = 1
    assert z.data.data == {"x": 1}
    assert set(z._last_updated) == {"x"}
    assert z.cache == {}

    with ThreadPoolExecutor(1) as ex:
        get_fut = ex.submit(z.__getitem__, "x")
        assert in_get.wait(timeout=5)
        del z["x"]

        block_get.set()
        if get_when == "before":
            assert get_fut.result() == 1
        else:
            with pytest.raises(KeyError):
                get_fut.result()

    assert not z.data.data
    assert not z.cache
    assert not z._last_updated


@pytest.mark.parametrize("set_when", ("before", "after"))
@pytest.mark.parametrize(
    "seed_data,seed_cache", [(False, False), (True, False), (True, True)]
)
@pytest.mark.parametrize("update_on_set", [False, True])
def test_multithread_race_condition_del_set(
    set_when, seed_data, seed_cache, update_on_set
):
    """Test race conditions between __delitem__ and __setitem__ on the same key"""
    in_set = threading.Event()
    block_set = threading.Event()

    class Slow(UserDict):
        def __setitem__(self, k, v):
            if set_when == "before":
                self.data[k] = v
                in_set.set()
                assert block_set.wait(timeout=5)
            else:
                in_set.set()
                assert block_set.wait(timeout=5)
                self.data[k] = v

    z = Cache(Slow(), {}, update_on_set=update_on_set)
    if seed_data:
        block_set.set()
        z["x"] = 1
        in_set.clear()
        block_set.clear()
        if seed_cache and not update_on_set:
            _ = z["x"]

    with ThreadPoolExecutor(1) as ex:
        set_fut = ex.submit(z.__setitem__, "x", 2)
        assert in_set.wait(timeout=5)
        try:
            del z["x"]
        except KeyError:
            pass
        block_set.set()
        set_fut.result()

    assert z.data.data in ({"x": 2}, {})
    assert z.cache in (z.data.data, {})
    assert z._last_updated.keys() == z.data.data.keys()


@pytest.mark.parametrize("set1_when", ("before", "after"))
@pytest.mark.parametrize("set2_when", ("before", "after"))
@pytest.mark.parametrize("starts_first", (1, 2))
@pytest.mark.parametrize("ends_first", (1, 2))
@pytest.mark.parametrize(
    "seed_data,seed_cache", [(False, False), (True, False), (True, True)]
)
@pytest.mark.parametrize("update_on_set", [False, True])
def test_multithread_race_condition_set_set(
    set1_when, set2_when, starts_first, ends_first, seed_data, seed_cache, update_on_set
):
    """Test __setitem__ in race condition with itself"""
    when = {1: set1_when, 2: set2_when}
    in_set = {1: threading.Event(), 2: threading.Event()}
    block_set = {1: threading.Event(), 2: threading.Event()}

    class Slow(UserDict):
        def __setitem__(self, k, v):
            if v == 0:
                # seed
                self.data[k] = v
                return

            if when[v] == "before":
                self.data[k] = v
                in_set[v].set()
                assert block_set[v].wait(timeout=5)
            else:
                in_set[v].set()
                assert block_set[v].wait(timeout=5)
                self.data[k] = v

    z = Cache(Slow(), {}, update_on_set=update_on_set)
    if seed_data:
        z["x"] = 0
        if seed_cache and not update_on_set:
            _ = z["x"]

    with ThreadPoolExecutor(2) as ex:
        futures = {}
        futures[starts_first] = ex.submit(z.__setitem__, "x", starts_first)
        assert in_set[starts_first].wait(timeout=5)
        starts_second = 2 if starts_first == 1 else 1
        futures[starts_second] = ex.submit(z.__setitem__, "x", starts_second)
        assert in_set[starts_second].wait(timeout=5)

        block_set[ends_first].set()
        futures[ends_first].result()
        ends_second = 2 if ends_first == 1 else 1
        block_set[ends_second].set()
        futures[ends_second].result()

    assert z.data.data in ({"x": 1}, {"x": 2})
    assert z.cache in (z.data.data, {})
    assert set(z._last_updated) == {"x"}


@pytest.mark.stress
@pytest.mark.repeat(utils_test.REPEAT_STRESS_TESTS)
@pytest.mark.parametrize("update_on_set", [False, True])
def test_stress_different_keys_threadsafe(update_on_set):
    buff = Cache(utils_test.SlowDict(0.001), {}, update_on_set=update_on_set)
    utils_test.check_different_keys_threadsafe(buff)
    assert not buff.cache
    assert not buff.data
    assert not buff._last_updated
    utils_test.check_mapping(buff)


@pytest.mark.stress
@pytest.mark.repeat(utils_test.REPEAT_STRESS_TESTS)
@pytest.mark.parametrize("update_on_set", [False, True])
def test_stress_same_key_threadsafe(update_on_set):
    buff = Cache(utils_test.SlowDict(0.001), {}, update_on_set=update_on_set)
    utils_test.check_same_key_threadsafe(buff)
    assert not buff.cache
    assert not buff.data
    assert not buff._last_updated
    utils_test.check_mapping(buff)


# --------------------------------------------------------------- versioning
def test_versioned_backend_shared_state():
    """Two caches on the same versioned backend share bookkeeping and reads."""
    data = VersionedBackend()
    c1 = Cache(data, LRU(1000, {}, weight=byte_size))
    c2 = Cache(data, WeakValueMapping())

    assert data._zict_cache_state is c1._state is c2._state
    assert c1._versioned and c2._versioned

    # Producer publishes directly through the backend
    data["k"] = b"v1"
    version1 = data.get_version("k")

    assert c1["k"] == b"v1"
    assert data.value_reads == 1
    # The second reader reuses the value loaded by the first: no extra large read
    assert c2["k"] == b"v1"
    assert data.value_reads == 1
    # bytes cannot be weak-referenced, so they only live in the LRU
    assert c2.cache == {}
    assert c1.cache == {"k": b"v1"}
    assert c1._versions == {"k": version1}

    # A version-only query does not count as a large value read
    version_reads = data.version_reads
    assert data.get_version("k") == version1
    assert data.version_reads == version_reads + 1
    assert data.value_reads == 1


def test_versioned_backend_detects_backend_changes_without_reading():
    """The cache follows producer changes, invalidating old bytes, without
    unnecessarily reading the value."""
    data = VersionedBackend()
    c1 = Cache(data, LRU(1000, {}, weight=byte_size))
    c2 = Cache(data, WeakValueMapping())

    data["k"] = b"v1"
    version1 = data.get_version("k")
    assert c1["k"] == b"v1"
    assert data.value_reads == 1

    # Producer publishes a weak-referenceable new version
    obj = Refable()
    data["k"] = obj
    assert data.get_version("k") != version1

    # The old hot bytes are still in the LRU but must not be served anymore
    assert c1.cache["k"] == b"v1"
    assert c1["k"] is obj
    assert b"v1" not in c1.cache.values()
    assert data.value_reads == 2  # obj fetched once

    # Then the key changes to yet another bytes payload
    data["k"] = b"v3"
    assert c1["k"] == b"v3"
    assert b"v1" not in c1.cache.values()
    assert obj not in c1.cache.values()
    assert data.value_reads == 3


def test_versioned_backend_delete_and_reinsert_is_new_version():
    data = VersionedBackend()
    c = Cache(data, LRU(1000, {}, weight=byte_size))

    data["j"] = b"same"
    version_before = data.get_version("j")
    assert c["j"] == b"same"

    del data["j"]
    with pytest.raises(KeyError):
        c["j"]
    assert "j" not in c.cache
    assert c._versions == {}

    data["j"] = b"same"  # identical content, but a new version
    assert data.get_version("j") != version_before
    assert c["j"] == b"same"
    assert c._versions["j"] == data.get_version("j")


def test_oversized_value_does_not_evict_hot_items():
    data = VersionedBackend()
    c = Cache(data, LRU(100, {}, weight=byte_size))

    data["hot"] = b"h" * 50
    assert c["hot"] == b"h" * 50
    hot = dict(c.cache)
    total_weight = c.cache.total_weight

    data["big"] = b"x" * 500  # heavier than the whole budget
    assert c["big"] == b"x" * 500  # handed to the caller
    assert "big" not in c.cache  # never inserted
    assert "big" not in c._versions
    assert dict(c.cache) == hot  # hot items untouched
    assert c.cache.total_weight == total_weight


def test_weak_cache_keeps_refable_values_and_drops_others():
    data = WeakStoreBackend()
    weak_cache = WeakValueMapping()
    c = Cache(data, weak_cache)

    obj = Refable()
    data["w"] = obj
    assert c["w"] is obj
    assert "w" in weak_cache  # weak-referenceable values are retained

    # bytes aren't weak-referenceable; check admission directly against a backend
    # that can store them
    strong_data = VersionedBackend()
    c_strong = Cache(strong_data, weak_cache)
    strong_data["b"] = b"bytes"
    assert c_strong["b"] == b"bytes"
    assert "b" not in weak_cache  # bytes don't consume the strong budget

    # Once the last external reference disappears, the weak value is released
    del obj
    gc.collect()
    assert "w" not in weak_cache


def test_lru_over_weak_cache_skips_bytes_without_corrupting_bookkeeping():
    data = VersionedBackend()
    fast = LRU(1000, WeakValueMapping(), weight=byte_size)
    c = Cache(data, fast)

    data["b"] = b"bytes"
    assert c["b"] == b"bytes"  # returned to the caller
    assert "b" not in fast  # not retained by the weak-only cache
    assert fast.weights == {} and fast.total_weight == 0  # no stale bookkeeping

    obj = Refable()
    data["o"] = obj
    assert c["o"] is obj
    assert "o" in fast  # weak-referenceable values are admitted
    assert fast.weights == {"o": 1}


def test_late_weakref_collection_does_not_delete_current_bytes():
    data = VersionedBackend()
    c1 = Cache(data, LRU(1000, {}, weight=byte_size))
    c2 = Cache(data, WeakValueMapping())

    obj = Refable()
    data["w"] = obj
    assert c2["w"] is obj

    # A bytes value becomes current while c2 still holds a weakref to the old one
    data["w"] = b"current"
    assert c1["w"] == b"current"
    del obj
    gc.collect()  # late collection of the stale weak reference
    assert c1.cache["w"] == b"current"
    assert c1["w"] == b"current"


def test_slow_stale_read_is_returned_but_not_reloaded():
    data = SlowVersionedBackend()
    c = Cache(data, LRU(1000, {}, weight=byte_size))

    data["x"] = b"old"
    old_version = data.get_version("x")
    c.cache.clear()
    c._versions.clear()

    with ThreadPoolExecutor(1) as ex:
        future = ex.submit(c.get_with_version, "x")
        assert data.in_get.wait(timeout=5)
        # Producer publishes a new version while the read is parked
        data["x"] = b"new"
        data.block_get.set()
        value, version = future.result()

    # The in-flight read explicitly reports the version it actually read
    assert value == b"old"
    assert version == old_version
    # ... and it must not be loaded back into the cache
    assert "x" not in c.cache
    assert c._versions == {}
    assert c["x"] == b"new"
    assert c._versions["x"] == data.get_version("x")


def test_get_with_version_tracks_current_version():
    data = VersionedBackend()
    c = Cache(data, {})

    data["x"] = b"a"
    value, version = c.get_with_version("x")
    assert value == b"a" and version == data.get_version("x")
    data["x"] = b"b"
    value, version = c.get_with_version("x")
    assert value == b"b" and version == data.get_version("x")


def test_versioned_backend_deletion_is_detected_by_version():
    data = VersionedBackend()
    c = Cache(data, LRU(1000, {}, weight=byte_size))
    data["x"] = b"a"
    assert c["x"] == b"a"

    del data["x"]  # producer deletes straight through the backend
    with pytest.raises(KeyError):
        c["x"]
    assert "x" not in c.cache
    assert c._versions == {}


def test_legacy_backend_keeps_write_through_behavior():
    data = {}
    c = Cache(data, {})
    assert c._versioned is False

    c["a"] = 1
    assert c["a"] == 1
    assert (data, c.cache) == ({"a": 1}, {"a": 1})
    c.cache.clear()
    assert c["a"] == 1
    assert c.cache == {"a": 1}
    del c["a"]
    assert (data, c.cache) == ({}, {})


def test_versioned_cache_mapping_interface():
    data = VersionedBackend()
    c = Cache(data, LRU(10_000, {}, weight=byte_size))
    utils_test.check_mapping(c)
    utils_test.check_closing(c)


def test_versioned_cache_same_key_threadsafe():
    data = VersionedBackend()
    buff = Cache(data, LRU(10_000, {}, weight=byte_size))
    utils_test.check_same_key_threadsafe(buff)
    assert not buff._versions
    utils_test.check_mapping(buff)

