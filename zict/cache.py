from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator, MutableMapping
from typing import Generic

from zict.common import KT, VT, ZictBase, close, discard, flush, locked

#: Attribute used to attach shared version bookkeeping to a backend mapping
STATE_ATTR = "_zict_cache_state"


class _WeakCacheSet:
    """Set of :class:`Cache` instances held by weak reference.

    Unlike :class:`weakref.WeakSet`, this doesn't require the elements to be hashable
    (``MutableMapping`` defines ``__eq__``, which makes caches unhashable).
    """

    __slots__ = ("_refs",)

    def __init__(self) -> None:
        self._refs: dict[int, weakref.ref[Cache[KT, VT]]] = {}

    def add(self, cache: Cache[KT, VT]) -> None:
        cache_id = id(cache)
        existing = self._refs.get(cache_id)
        if existing is not None and existing() is cache:
            return

        def remove(
            ref: weakref.ref[Cache[KT, VT]],
            cache_id: int = cache_id,
            owner: _WeakCacheSet = self,
        ) -> None:
            # Only drop the entry if it still points at this exact reference, so a
            # callback from a replaced ref can't delete a newer one.
            if owner._refs.get(cache_id) is ref:
                owner._refs.pop(cache_id, None)

        self._refs[cache_id] = weakref.ref(cache, remove)

    def __iter__(self) -> Iterator[Cache[KT, VT]]:
        for ref in list(self._refs.values()):
            cache = ref()
            if cache is not None:
                yield cache

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _BackendState(Generic[KT, VT]):
    """Shared bookkeeping for all :class:`Cache` instances wrapping the same backend.

    Holds the authoritative version of every key plus the set of caches known to hold
    that version. Nothing in here keeps a strong reference to any cached value.
    """

    __slots__ = ("versions", "tokens", "lock")

    def __init__(self) -> None:
        # Key -> (version number as reported by the backend, caches holding it)
        self.versions: dict[KT, tuple[int, _WeakCacheSet]] = {}
        # Per-key serial of cache-initiated writes; arbitrates concurrent set/del
        # operations while the backend lock is released.
        self.tokens: dict[KT, int] = {}
        # Serializes changes to the dictionaries above; never held while reading
        # from the backend.
        self.lock = threading.Lock()

    def reconcile(self, key: KT, version: int) -> tuple[int, _WeakCacheSet]:
        """Align the shared state with a version observed on the backend.

        A new version (e.g. published by a producer writing straight to the backend)
        resets the set of caches known to be up to date.
        """
        with self.lock:
            entry = self.versions.get(key)
            if entry is None or entry[0] != version:
                entry = (version, _WeakCacheSet())
                self.versions[key] = entry
            return entry

    def publish(
        self, key: KT, version: int, token: int
    ) -> tuple[int, _WeakCacheSet] | None:
        """Register a version written through one of the caches.

        Returns nothing if a concurrent cache operation on the same key superseded it.
        """
        with self.lock:
            if self.tokens.get(key) != token:
                return None
            entry = (version, _WeakCacheSet())
            self.versions[key] = entry
            return entry

    def next_token(self, key: KT) -> int:
        with self.lock:
            token = self.tokens.get(key, 0) + 1
            self.tokens[key] = token
            return token

    def retract(self, key: KT, token: int | None = None) -> None:
        """Remove a key from the shared state (backend deletion)."""
        with self.lock:
            if token is not None and self.tokens.get(key) != token:
                return
            self.versions.pop(key, None)

    def lookup(self, key: KT) -> tuple[int, _WeakCacheSet] | None:
        with self.lock:
            return self.versions.get(key)


def _backend_state(data: MutableMapping[KT, VT]) -> _BackendState[KT, VT] | None:
    state = getattr(data, STATE_ATTR, None)
    if isinstance(state, _BackendState):
        return state
    return None


def _get_version(data: MutableMapping[KT, VT], key: KT) -> int:
    """Cheaply query the backend for the current version of a key.

    Raises KeyError if the key doesn't exist. This never reads the (large) value.
    """
    try:
        get_version = data.get_version  # type: ignore[attr-defined]
    except AttributeError:
        raise KeyError(key)
    version = get_version(key)
    if not isinstance(version, int):
        raise TypeError(
            f"{type(data).__name__}.get_version() must return an int, got "
            f"{type(version).__name__}"
        )
    return version


def _is_weakrefable(value: object) -> bool:
    try:
        weakref.ref(value)
    except TypeError:
        return False
    return True


def _weak_only(cache: MutableMapping[KT, VT]) -> bool:
    """True if the fast cache can only retain weak-referenceable values.

    This covers a bare :class:`WeakValueMapping` as well as an :class:`~zict.lru.LRU`
    (or another wrapper) backed by one.
    """
    seen: set[int] = set()
    while id(cache) not in seen:
        seen.add(id(cache))
        if isinstance(cache, WeakValueMapping):
            return True
        inner = getattr(cache, "d", None)
        if inner is None:
            return False
        cache = inner
    return False


class Cache(ZictBase[KT, VT]):
    """Transparent write-through cache around a MutableMapping with an expensive
    ``__getitem__`` method.

    Parameters
    ----------
    data: MutableMapping
        Persistent, slow to read mapping to be cached. Several caches may wrap the same
        backend. If the backend implements a cheap ``get_version(key) -> int`` method
        (raising ``KeyError`` for missing keys), the caches gain version awareness:

        - values updated or deleted directly through the backend are detected by
          version only, without reading the value itself;
        - a value is fetched from the backend only when the version held by the cache
          is stale, and a value fetched by one cache is reused by the others, so each
          version costs at most one large backend read across all caches;
        - a slow read that completes after the key changed still returns the value it
          read, but it is never loaded back into the cache;
        - deleting and re-inserting a key always produces a new version, even if the
          value is identical.

        Backends without ``get_version`` keep the classic write-through behaviour.
    cache: MutableMapping
        Fast cache for reads from data. This mapping may lose keys on its own; e.g. it
        could be a LRU or a :class:`WeakValueMapping`. Caches exposing ``n`` and a
        ``weight`` callable (like :class:`~zict.lru.LRU`) enforce a byte budget: values
        heavier than ``n`` are returned to the caller but never inserted, so they can't
        evict the hot items. :class:`WeakValueMapping` only retains weak-referenceable
        values, which therefore don't consume the strong-reference budget.
    update_on_set: bool, optional
        If True (default), the cache will be updated both when writing and reading.
        If False, update the cache when reading, but just invalidate it when writing.

    Notes
    -----
    If you call methods of this class from multiple threads, access will be fast as long
    as all methods of ``cache``, plus ``data.__delitem__``, are fast. Other methods of
    ``data`` are not protected by locks.

    Examples
    --------
    Keep the latest 100 accessed values in memory
    >>> from zict import Cache, File, LRU, WeakValueMapping
    >>> d = Cache(File('myfile'), LRU(100, {}))  # doctest: +SKIP

    Read data from disk every time, unless it was previously accessed and it's still in
    use somewhere else in the application
    >>> d = Cache(File('myfile'), WeakValueMapping())  # doctest: +SKIP
    """

    data: MutableMapping[KT, VT]
    cache: MutableMapping[KT, VT]
    update_on_set: bool
    _gen: int
    _last_updated: dict[KT, int]
    _state: _BackendState[KT, VT] | None
    _versioned: bool
    _versions: dict[KT, int]

    def __init__(
        self,
        data: MutableMapping[KT, VT],
        cache: MutableMapping[KT, VT],
        update_on_set: bool = True,
    ):
        super().__init__()
        self.data = data
        self.cache = cache
        self.update_on_set = update_on_set
        self._gen = 0
        self._last_updated = {}
        # Version of the value currently held in self.cache, keyed by key. An entry
        # only exists while the fast cache actually holds the matching value.
        self._versions = {}

        state = _backend_state(data)
        if state is None and hasattr(data, "get_version"):
            state = _BackendState()
            try:
                setattr(data, STATE_ATTR, state)
            except (AttributeError, TypeError):
                # The backend can't carry shared state (no __dict__). Keep private
                # bookkeeping; peer caches won't be able to share reads.
                pass
        # A backend without get_version keeps the classic write-through behaviour.
        self._state = state
        self._versioned = state is not None

    def _invalidate(self, key: KT) -> None:
        """Drop whatever this cache currently holds for a key."""
        self._versions.pop(key, None)
        discard(self.cache, key)

    def _admit(
        self,
        key: KT,
        value: VT,
        version: int,
        seen: _WeakCacheSet,
    ) -> bool:
        """Load a value of the given version into the fast cache.

        Returns True if the cache accepted the value, False if it was handed back to the
        caller without caching (oversized value, or value a weak cache can't reference).
        """
        cache = self.cache
        weak_only = _weak_only(cache)
        if weak_only and not _is_weakrefable(value):
            # bytes and friends can't be weak-referenced; inserting them would either
            # be silently dropped (corrupting an LRU's weight bookkeeping) or eat into
            # the strong-reference budget. Hand them back without caching.
            return False

        weight_fn = getattr(cache, "weight", None)
        capacity = getattr(cache, "n", None)
        if callable(weight_fn) and capacity is not None:
            if weight_fn(key, value) > capacity:
                # Too heavy for the reserved budget: hand the value to the caller but
                # leave the hot items untouched.
                return False

        self._invalidate(key)
        cache[key] = value
        if weak_only and key not in cache:
            # A weak-value cache that couldn't retain the value dropped it silently;
            # don't pretend the cache holds this version.
            self._versions.pop(key, None)
            return False
        self._versions[key] = version
        seen.add(self)
        return True

    # ------------------------------------------------------------------ read
    @locked
    def __getitem__(self, key: KT) -> VT:
        value, _ = self._getitem(key)
        return value

    @locked
    def get_with_version(self, key: KT) -> tuple[VT, int]:
        """Like ``self[key]``, but also return the version of the value read.

        The returned version identifies what the caller actually receives: a read that
        was already in flight when the key changed returns the old value together with
        the old version, even though the cache itself moves on to the new one.

        Only backends implementing ``get_version`` assign meaning to the returned
        number. On plain backends the cache falls back to its internal generation
        counter, which is only useful for telling two reads of the same key apart.
        """
        return self._getitem(key)

    def _getitem(self, key: KT) -> tuple[VT, int]:
        if not self._versioned:
            return self._getitem_legacy(key)

        state = self._state
        assert state is not None
        try:
            backend_version = _get_version(self.data, key)
        except KeyError:
            # Deleted from the backend (possibly by a producer bypassing the caches)
            state.retract(key)
            self._invalidate(key)
            raise

        state.reconcile(key, backend_version)
        if self._versions.get(key) == backend_version:
            try:
                return self.cache[key], backend_version
            except KeyError:
                # The fast cache dropped the value on its own (LRU eviction, death of
                # a weak reference). The version is still current; reload below.
                self._versions.pop(key, None)
        else:
            # Whatever the fast cache holds is older than the backend; it must never
            # be served again.
            self._invalidate(key)

        with self.unlock():
            value, read_version = self._fetch(key, backend_version)

        # The read may have taken arbitrarily long. A cheap version probe tells us
        # whether what we read is still current; this is not another large read.
        try:
            current_version = _get_version(self.data, key)
        except KeyError:
            current_version = None
        if (
            current_version == read_version
            and self._versions.get(key) != read_version
        ):
            current = state.reconcile(key, read_version)
            self._admit(key, value, read_version, current[1])
        return value, read_version

    def _fetch(self, key: KT, version: int) -> tuple[VT, int]:
        """Return the current value of a key, sharing the backend read across caches."""
        state = self._state
        assert state is not None

        # Another cache wrapping the same backend may already hold this version.
        # Acquire peer locks without blocking: waiting while holding our own lock could
        # deadlock against a peer probing us in turn. Sharing is best effort; a miss
        # just costs a backend read.
        entry = state.lookup(key)
        if entry is not None:
            for other in entry[1]:
                if other is self or not other.lock.acquire(blocking=False):
                    continue
                try:
                    if other._versions.get(key) == version:
                        try:
                            return other.cache[key], version
                        except KeyError:
                            pass
                finally:
                    other.lock.release()

        # Nobody has it: pay for the large read ourselves. The version recorded is the
        # one observed before the read; a post-read probe in the caller detects any
        # change happening meanwhile.
        value = self.data[key]
        return value, version

    # ----------------------------------------------------------------- write
    @locked
    def __setitem__(self, key: KT, value: VT) -> None:
        if not self._versioned:
            self._setitem_legacy(key, value)
            return

        state = self._state
        assert state is not None
        token = state.next_token(key)
        self._invalidate(key)

        with self.unlock():
            self.data[key] = value

        # The backend owns version numbering atomically; ask it which version we wrote.
        # Probe twice: a producer writing straight to the backend between our write and
        # the first probe would make us label our own value with somebody else's
        # version. A stable pair of probes rules that out (the residual sub-window is
        # re-validated by the cheap version check on every subsequent read anyway).
        try:
            version = _get_version(self.data, key)
            latest = _get_version(self.data, key)
        except KeyError:
            # Another thread deleted the key in the meantime
            return
        if latest != version:
            # The backend moved under us; let the next read reconcile instead of
            # pinning a potentially mismatched value/version pair.
            state.reconcile(key, latest)
            return

        entry = state.publish(key, version, token)
        if entry is None:
            # A concurrent set/del on the same key superseded this write; leave the
            # cache alone and let the next read reconcile against the backend.
            return
        if self.update_on_set:
            self._admit(key, value, version, entry[1])

    @locked
    def __delitem__(self, key: KT) -> None:
        if not self._versioned:
            self._delitem_legacy(key)
            return

        state = self._state
        assert state is not None
        token = state.next_token(key)
        del self.data[key]
        state.retract(key, token)
        self._last_updated.pop(key, None)
        self._invalidate(key)

    # --------------------------------------------------------- legacy behaviour
    def _getitem_legacy(self, key: KT) -> tuple[VT, int]:
        try:
            value = self.cache[key]
            return value, self._last_updated.get(key, 0)
        except KeyError:
            pass
        gen = self._last_updated[key]

        with self.unlock():
            value = self.data[key]

        # Could another thread have called __setitem__ or __delitem__ on the
        # same key in the meantime? If not, update the cache
        if gen == self._last_updated.get(key):
            self.cache[key] = value
            self._last_updated[key] += 1
        return value, gen

    def _setitem_legacy(self, key: KT, value: VT) -> None:
        # If the item was already in cache and data.__setitem__ fails, e.g. because
        # it's a File and the disk is full, make sure that the cache is invalidated.
        discard(self.cache, key)
        gen = self._gen
        gen += 1
        self._last_updated[key] = self._gen = gen

        with self.unlock():
            self.data[key] = value

        if key not in self._last_updated:
            # Another thread called __delitem__ in the meantime
            discard(self.data, key)
        elif gen != self._last_updated[key]:
            # Another thread called __setitem__ in the meantime. We have no idea which
            # of the two ended up actually setting self.data.
            # Case 1: the other thread did not enter this locked code block yet.
            #         Prevent it from setting the cache.
            self._last_updated[key] += 1
            # Case 2: the other thread already exited this locked code block and set the
            #         cache. Invalidate it.
            discard(self.cache, key)
        else:
            # No race condition
            self._last_updated[key] += 1
            if self.update_on_set:
                self.cache[key] = value

    def _delitem_legacy(self, key: KT) -> None:
        del self.data[key]
        del self._last_updated[key]
        discard(self.cache, key)

    # ------------------------------------------------------------- mapping API
    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self) -> Iterator[KT]:
        return iter(self.data)

    def __contains__(self, key: object) -> bool:
        # Do not let MutableMapping call self.data[key]
        return key in self.data

    def flush(self) -> None:
        flush(self.cache, self.data)

    def close(self) -> None:
        close(self.cache, self.data)


class WeakValueMapping(weakref.WeakValueDictionary[KT, VT]):
    """Variant of weakref.WeakValueDictionary which silently ignores objects that
    can't be referenced by a weakref.ref

    Values which can't be weak-referenced (e.g. ``bytes``) are not stored and therefore
    don't consume the strong-reference budget of a :class:`Cache`; they are still
    returned to the caller of ``Cache.__getitem__``. Weak-referenceable values are
    released as soon as the last external reference to them disappears.
    """

    def __setitem__(self, key: KT, value: VT) -> None:
        try:
            super().__setitem__(key, value)
        except TypeError:
            pass
