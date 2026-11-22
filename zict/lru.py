from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    ItemsView,
    Iterator,
    KeysView,
    MutableMapping,
    ValuesView,
)
from typing import Any, Generic

from zict.common import KT, VT, NoDefault, ZictBase, close, flush, locked, nodefault
from zict.utils import InsertionSortedSet


class RetentionError(KeyError):
    """Raised when a retention cannot be granted or honoured.

    Subclasses :class:`KeyError`, so code treating a missing or unloadable key as a
    ``KeyError`` keeps working.
    """


class Retention(Generic[KT]):
    """Handle returned by :meth:`LRU.retain`.

    Every call to :meth:`LRU.retain` must be paired with exactly one :meth:`release`.
    Retentions are reentrant: the same key may be protected by several handles at the
    same time, and it stays protected until all of them have been released. Releasing a
    handle whose keys were deleted (and possibly re-inserted and re-retained) in the
    meantime is a no-op and can never undo another job's retention.

    This class can also be used as a context manager, in which case :meth:`release` is
    called automatically on exit, including if the enclosed code raises.
    """

    __slots__ = ("_lru", "_rid", "keys", "_released")

    _lru: LRU[KT, Any]
    _rid: int
    keys: frozenset[KT]
    _released: bool

    def __init__(self, lru: LRU[KT, Any], rid: int, keys: Collection[KT]):
        self._lru = lru
        self._rid = rid
        self.keys = frozenset(keys)
        self._released = False

    def release(self) -> None:
        """Release the retention. Idempotent: calling it more than once is a no-op."""
        if self._released:
            return
        self._released = True
        self._lru._release(self._rid, self.keys)

    def __enter__(self) -> Retention[KT]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class LRU(ZictBase[KT, VT]):
    """Evict Least Recently Used Elements.

    Parameters
    ----------
    n: int or float
        Number of elements to keep, or total weight if ``weight`` is used.
        Any individual key that is heavier than n will be automatically evicted as soon
        as it is inserted.

        It can be updated after initialization. See also: ``offset`` attribute.
    d: MutableMapping
        Dict-like in which to hold elements. There are no expectations on its internal
        ordering. Iteration on the LRU follows the order of the underlying mapping.
    on_evict: callable or list of callables
        Function:: k, v -> action to call on key/value pairs prior to eviction
        If an exception occurs during an on_evict callback (e.g a callback tried
        storing to disk and raised a disk full error) the key will remain in the LRU.
    on_cancel_evict: callable or list of callables
        Function:: k, v -> action to call on key/value pairs if they're deleted or
        updated from a thread while the on_evict callables are being executed in
        another.
        If you're not accessing the LRU from multiple threads, ignore this parameter.
    weight: callable
        Function:: k, v -> number to determine the size of keeping the item in
        the mapping.  Defaults to ``(k, v) -> 1``

    Notes
    -----
    If you call methods of this class from multiple threads, access will be fast as long
    as all methods of ``d`` are fast. Callbacks are not protected by locks and can be
    arbitrarily slow.

    A :meth:`retain` call atomically pins a group of keys so that the LRU won't evict
    any of them, returning a :class:`Retention` handle. All keys of the group must be
    present or the whole request fails atomically (nothing is retained) with a
    :class:`RetentionError`. Retained keys still occupy space according to their real
    ``weight``: if there isn't enough room, or not enough evictable keys, to fit a new
    entry, the insertion fails instead of evicting retained data. Call
    :meth:`Retention.release` (or use the handle as a context manager) to unpin.

    Retention is reentrant (a key may be held by several handles at once), neither
    :meth:`retain` nor :meth:`Retention.release` count as reads (the LRU order is left
    untouched), and releasing one handle never unpins keys still protected by another
    handle. A key whose eviction callback is still running, but which has not been
    removed from ``d`` yet, can be retained: when the callback returns the eviction is
    cancelled, the value stays in the LRU, and any stale copy left by the callback is
    cleaned up through ``on_cancel_evict``.

    Examples
    --------
    >>> lru = LRU(2, {}, on_evict=lambda k, v: print("Lost", k, v))
    >>> lru['x'] = 1
    >>> lru['y'] = 2
    >>> lru['z'] = 3
    Lost x 1
    """

    d: MutableMapping[KT, VT]
    order: InsertionSortedSet[KT]
    heavy: InsertionSortedSet[KT]
    on_evict: list[Callable[[KT, VT], None]]
    on_cancel_evict: list[Callable[[KT, VT], None]]
    weight: Callable[[KT, VT], float]
    #: Maximum weight before eviction is triggered, as set during initialization.
    #: Updating this attribute doesn't trigger eviction by itself; you should call
    #: :meth:`evict_until_below_target` explicitly afterwards.
    n: float
    #: Offset to add to ``total_weight`` to determine if key/value pairs should be
    #: evicted. It always starts at zero and can be updated afterwards. Updating this
    #: attribute doesn't trigger eviction by itself; you should call
    #: :meth:`evict_until_below_target` explicitly afterwards.
    #: Increasing ``offset`` is not the same as reducing ``n``, as the latter will also
    #: reduce the threshold below which a value is considered "heavy" and qualifies for
    #: immediate eviction.
    offset: float
    weights: dict[KT, float]
    closed: bool
    total_weight: float
    _cancel_evict: dict[KT, bool]
    #: Active retention handle IDs protecting each key. A key is protected from
    #: eviction while this set is non-empty, but it still occupies space according to
    #: its weight. Tracking IDs (rather than a single counter) keeps retentions
    #: independent across handles: deleting and re-inserting a key, or releasing a stale
    #: handle, can never undo another job's retention.
    _retentions: dict[KT, set[int]]
    _next_rid: int

    def __init__(
        self,
        n: float,
        d: MutableMapping[KT, VT],
        *,
        on_evict: Callable[[KT, VT], None]
        | list[Callable[[KT, VT], None]]
        | None = None,
        on_cancel_evict: Callable[[KT, VT], None]
        | list[Callable[[KT, VT], None]]
        | None = None,
        weight: Callable[[KT, VT], float] = lambda k, v: 1,
    ):
        super().__init__()
        self.d = d
        self.n = n
        self.offset = 0

        if callable(on_evict):
            on_evict = [on_evict]
        self.on_evict = on_evict or []
        if callable(on_cancel_evict):
            on_cancel_evict = [on_cancel_evict]
        self.on_cancel_evict = on_cancel_evict or []

        self.weight = weight
        self.weights = {k: weight(k, v) for k, v in d.items()}
        self.total_weight = sum(self.weights.values())
        self.order = InsertionSortedSet(d)
        self.heavy = InsertionSortedSet(k for k, v in self.weights.items() if v >= n)
        self.closed = False
        self._cancel_evict = {}
        self._retentions = {}
        self._next_rid = 0

    @locked
    def __getitem__(self, key: KT) -> VT:
        result = self.d[key]
        self.order.remove(key)
        self.order.add(key)
        return result

    @locked
    def get_all_or_nothing(self, keys: Collection[KT]) -> dict[KT, VT]:
        """If all keys exist in the LRU, update their FIFO priority and return their
        values; this would be the same as ``{k: lru[k] for k in keys}``.
        If any keys are missing, however, raise KeyError for the first one missing and
        do not bring any of the available keys to the top of the LRU.
        """
        result = {key: self.d[key] for key in keys}
        for key in keys:
            self.order.remove(key)
            self.order.add(key)
        return result

    @locked
    def retain(self, keys: Collection[KT]) -> Retention[KT]:
        """Atomically protect a group of keys from eviction.

        Every key in ``keys`` must currently be in the LRU - a key that is being evicted
        still counts as present as long as its eviction callbacks have not finished and
        the value has not been removed from the underlying mapping yet. If any key is
        missing, no key is retained and :class:`RetentionError` (a :class:`KeyError`
        subclass) is raised.

        Retained keys cannot be evicted, but they still take up space according to their
        real ``weight``. This call neither reads nor writes any value, so it does not
        change the LRU order; it is safe to call while eviction callbacks are running on
        other threads.

        The returned :class:`Retention` must be released exactly once, via
        :meth:`Retention.release` or by using it as a context manager. Retentions are
        reentrant: retaining the same key more than once keeps it protected until every
        handle has been released.
        """
        # Materialize once: we must not iterate the input collection twice and, if it
        # happens to be a generator, we'd get different results on the second pass.
        group = list(keys)
        for key in group:
            if key not in self.d:
                raise RetentionError(key)
        rid = self._next_rid
        self._next_rid += 1
        for key in group:
            self._retentions.setdefault(key, set()).add(rid)
        return Retention(self, rid, group)

    def _release(self, rid: int, keys: frozenset[KT]) -> None:
        with self.lock:
            for key in keys:
                handles = self._retentions.get(key)
                if handles is None:
                    continue
                handles.discard(rid)
                if not handles:
                    del self._retentions[key]

    def __setitem__(self, key: KT, value: VT) -> None:
        weight = self.weight(key, value)
        self._check_capacity(key, weight)
        self.set_noevict(key, value)
        try:
            self.evict_until_below_target()
        except Exception:
            if self.weights.get(key, 0) > self.n and key not in self.heavy:
                # weight(value) > n and evicting the key we just inserted failed.
                # Evict the rest of the LRU instead.
                try:
                    while len(self.d) > 1:
                        try:
                            evicted = self.evict()
                        except KeyError:
                            # No evictable key left, e.g. everything else is retained.
                            break
                        if evicted == (None, None, 0):
                            # Only keys whose eviction is already running remain.
                            break
                except Exception:
                    pass
            raise

    @locked
    def _check_capacity(self, key: KT, weight: float) -> None:
        """Make sure that ``key`` (an insertion or an overwrite) can be made to fit the
        target without evicting any retained key. Raise :class:`RetentionError`
        otherwise, without changing the LRU.

        When no retention is active this is a no-op, preserving the historical behaviour
        of attempting eviction (and re-raising any callback failure).
        """
        if not self._retentions:
            return
        # Weight freed by the operation itself when replacing an existing value of the
        # same key. set_noevict() always removes the old entry (even if its eviction
        # callbacks are still running), so the old weight is reclaimed regardless.
        freed = self.weights.get(key, 0.0)
        excess = self.total_weight - freed + weight + self.offset - self.n
        if excess <= 0:
            return

        # Weight that eviction must not touch: retained keys, plus keys whose eviction
        # callback is already running and whose space in d therefore can't be reclaimed
        # by starting a new eviction. The two sets may overlap (a key can be retained
        # while its callback is still running); count each key only once.
        pinned_weight = sum(
            weight_
            for pinned_key, weight_ in self.weights.items()
            if pinned_key != key
            and (pinned_key in self._retentions or pinned_key in self._cancel_evict)
        )
        # Everything else is reclaimable, except the incoming key (which we must keep)
        reclaimable = self.total_weight - pinned_weight - freed
        if excess > reclaimable:
            raise RetentionError(key)

    @locked
    def set_noevict(self, key: KT, value: VT) -> None:
        """Variant of ``__setitem__`` that does not evict if the total weight exceeds n.
        Unlike ``__setitem__``, this method does not depend on the ``on_evict``
        functions to be thread-safe for its own thread-safety. It also is not prone to
        re-raising exceptions from the ``on_evict`` callbacks.
        """
        # An overwrite must not silently drop a retention on the same key. discard()
        # goes through __delitem__(), which would otherwise remove the bookkeeping.
        retained_handles = self._retentions.get(key)
        self.discard(key)
        weight = self.weight(key, value)
        if key in self._cancel_evict:
            self._cancel_evict[key] = True
        self.d[key] = value
        self.order.add(key)
        if weight > self.n:
            self.heavy.add(key)  # Mark this key to be evicted first
        self.weights[key] = weight
        self.total_weight += weight
        if retained_handles:
            self._retentions[key] = retained_handles

    def evict_until_below_target(self, n: float | None = None) -> None:
        """Evict key/value pairs until the total weight falls below n

        Retained keys (see :meth:`retain`) are never evicted. If no evictable key can
        bring the total weight below n, the method returns leaving the retained keys in
        place.

        Parameters
        ----------
        n: float, optional
            Total weight threshold to achieve. Defaults to self.n.
        """
        if n is None:
            n = self.n
        while self.total_weight + self.offset > n and not self.closed:
            try:
                self._evict_one()
            except KeyError:
                return  # Nothing evictable left, or multithreaded race condition

    def _pick_evictable(self, include_evicting: bool) -> KT:
        """Return the next key to evict: a heavy key if any, otherwise the least
        recently used key. Retained keys are always skipped. Keys whose eviction
        callbacks are already running are skipped too, unless ``include_evicting`` is
        True (used by the public :meth:`evict`, which returns a no-op sentinel for
        them). Raise KeyError if no candidate remains.
        """

        def eligible(key: KT) -> bool:
            if key in self._retentions:
                return False
            if not include_evicting and key in self._cancel_evict:
                return False
            return True

        if self.heavy:
            for key in self.heavy:
                if eligible(key):
                    return key
        for key in self.order:
            if eligible(key):
                return key
        raise KeyError("evict(): no evictable keys")

    @locked
    def _evict_one(self) -> tuple[KT, VT, float] | tuple[None, None, float]:
        """Pick a key that is neither retained nor mid-eviction and evict it.
        Raises KeyError if no such key exists. The call may still return the
        (None, None, 0) sentinel if another thread retains or cancels the key while its
        eviction callbacks are running.
        """
        key = self._pick_evictable(include_evicting=False)
        return self.evict(key)

    @locked
    def evict(
        self, key: KT | NoDefault = nodefault
    ) -> tuple[KT, VT, float] | tuple[None, None, float]:
        """Evict least recently used key, or least recently inserted key with individual
        weight > n, if any. You may also evict a specific key.

        Retained keys are skipped when picking a victim and cannot be evicted
        explicitly; attempting to do so raises :class:`RetentionError`.

        This is typically called from internal use, but can be externally
        triggered as well.

        Returns
        -------
        Tuple of (key, value, weight)

        Or (None, None, 0) if the key that was being evicted was updated or deleted from
        another thread while the on_evict callbacks were being executed. This outcome is
        only possible in multithreaded access.
        """
        if key is nodefault:
            try:
                # Include mid-eviction keys so that, if one is the only candidate, we
                # preserve the historical behaviour of returning the no-op sentinel
                # below instead of raising.
                key = self._pick_evictable(include_evicting=True)
            except KeyError:
                raise KeyError("evict(): dictionary is empty")

        if key in self._retentions:
            raise RetentionError(key)

        if key in self._cancel_evict:
            return None, None, 0

        # For the purpose of multithreaded access, it's important that the value remains
        # in self.d until all callbacks are successful.
        # When this is used inside a Buffer, there must never be a moment when the key
        # is neither in fast nor in slow.
        value = self.d[key]

        # If we are evicting a heavy key we just inserted and one of the callbacks
        # fails, put it at the bottom of the LRU instead of the top. This way lighter
        # keys will have a chance to be evicted first and make space.
        self.heavy.discard(key)

        self._cancel_evict[key] = False
        try:
            with self.unlock():
                # This may raise; e.g. if a callback tries storing to a full disk
                for cb in self.on_evict:
                    cb(key, value)

                # The key may have been retained (or updated/deleted) from another
                # thread while the callbacks were running. In either case the value is
                # still in self.d, so it must not be removed now. Run the cancellation
                # callbacks so that any stale copy written by on_evict (e.g. a copy in
                # the slow layer of a Buffer) is cleaned up.
                if self._cancel_evict[key] or key in self._retentions:
                    for cb in self.on_cancel_evict:
                        cb(key, value)
                    return None, None, 0
        finally:
            del self._cancel_evict[key]

        del self.d[key]
        self.order.remove(key)
        weight = self.weights.pop(key)
        self.total_weight -= weight

        return key, value, weight

    @locked
    def __delitem__(self, key: KT) -> None:
        if key in self._cancel_evict:
            self._cancel_evict[key] = True
        del self.d[key]
        self.order.remove(key)
        self.heavy.discard(key)
        self.total_weight -= self.weights.pop(key)
        # An explicit delete also drops any retention bookkeeping, so that a later
        # re-insertion starts from a clean slate; outstanding handles release as no-ops.
        self._retentions.pop(key, None)

    def keys(self) -> KeysView[KT]:
        return self.d.keys()

    def values(self) -> ValuesView[VT]:
        return self.d.values()

    def items(self) -> ItemsView[KT, VT]:
        return self.d.items()

    def __len__(self) -> int:
        return len(self.d)

    def __iter__(self) -> Iterator[KT]:
        return iter(self.d)

    def __contains__(self, key: object) -> bool:
        return key in self.d

    def __str__(self) -> str:
        sub = str(self.d) if not isinstance(self.d, dict) else "dict"
        return f"<LRU: {self.total_weight + self.offset}/{self.n} on {sub}>"

    __repr__ = __str__

    def flush(self) -> None:
        flush(self.d)

    def close(self) -> None:
        self.closed = True
        close(self.d)
