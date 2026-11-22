from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Hashable,
    ItemsView,
    Iterator,
    KeysView,
    MutableMapping,
    ValuesView,
)

from zict.common import KT, VT, NoDefault, ZictBase, close, flush, locked, nodefault
from zict.utils import InsertionSortedSet


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
    payload: callable, optional
        Function:: k, v -> hashable identifying the real payload backing the value, or
        a ``(identity, size)`` tuple. Multiple keys may return the same identity; they
        will then be charged to the total weight only once.
        When it returns just an identity, ``weight`` supplies the payload size; it must
        return the same size for every key that references the same payload.
        When omitted, every key is its own payload and the LRU behaves exactly like
        the classic per-key weighted LRU.

    Notes
    -----
    If you call methods of this class from multiple threads, access will be fast as long
    as all methods of ``d`` are fast. Callbacks are not protected by locks and can be
    arbitrarily slow.

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
    payload: Callable[[KT, VT], Hashable | tuple[Hashable, float]] | None
    #: Payload identity referenced by each key in ``d``
    payloads: dict[KT, Hashable]
    #: Number of keys in ``d`` that reference each payload
    payload_refs: dict[Hashable, int]
    #: Size charged to the total weight for each payload with at least one reference
    payload_sizes: dict[Hashable, float]
    #: Number of payloads currently referenced by two or more keys
    shared_payloads: int
    closed: bool
    total_weight: float
    _cancel_evict: dict[KT, bool]
    #: Evictions currently running their on_evict callbacks, keyed by a unique token
    #: so that a late callback for a since-replaced key cannot remove a newer
    #: in-flight entry. The key is still in ``d`` and charged to ``total_weight`` until
    #: the callbacks succeed; these attempts are only used to avoid selecting a key
    #: twice and to wait for a shared payload's pending release.
    _evicting: dict[object, KT]
    _evict_token: int

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
        payload: Callable[[KT, VT], Hashable | tuple[Hashable, float]] | None = None,
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

        self.payload = payload
        self.weight = weight
        self.weights = {}
        self.payloads = {}
        self.payload_refs = {}
        self.payload_sizes = {}
        self.shared_payloads = 0
        self.total_weight = 0
        for k, v in d.items():
            self._register(k, v)
        self.order = InsertionSortedSet(d)
        self.heavy = InsertionSortedSet(k for k, v in self.weights.items() if v >= n)
        self.closed = False
        self._cancel_evict = {}
        self._evicting = {}
        self._evict_token = 0

    def _payload_id_and_size(self, key: KT, value: VT) -> tuple[Hashable, float]:
        if self.payload is not None:
            info = self.payload(key, value)
            pair = self._as_id_size(info)
            if pair is not None:
                return pair
            return info, self.weight(key, value)

        weight = self.weight(key, value)
        pair = self._as_id_size(weight)
        if pair is not None:
            # A single callback may supply both the shared payload identity and its
            # size, e.g. ``lambda k, v: (v.buffer_id, v.nbytes)``
            return pair

        # No sharing: every key is its own payload
        return key, weight

    @staticmethod
    def _as_id_size(info: object) -> tuple[Hashable, float] | None:
        """Interpret ``(identity, size)`` unambiguously.

        A two-element tuple whose second item is a number is treated as an
        ``(identity, size)`` pair. Any other value - including a tuple identity such
        as ``("a", "b")`` - is a plain identity.
        """
        if (
            isinstance(info, tuple)
            and len(info) == 2
            and isinstance(info[1], (int, float))
        ):
            return info[0], info[1]
        return None

    def _register(self, key: KT, value: VT) -> float:
        """Book *key* as a reference to its payload and return its nominal weight.

        The payload is charged to ``total_weight`` only on its first reference.
        """
        pid, size = self._payload_id_and_size(key, value)
        self.weights[key] = size
        self.payloads[key] = pid
        nrefs = self.payload_refs.get(pid, 0)
        self.payload_refs[pid] = nrefs + 1
        if not nrefs:
            self.payload_sizes[pid] = size
            self.total_weight += size
        elif nrefs == 1:
            self.shared_payloads += 1
        return size

    def _unregister(self, key: KT) -> float:
        """Remove *key*'s reference to its payload and return the freed weight.

        The weight is only freed when the last fast reference to the payload is gone.
        """
        weight = self.weights.pop(key)
        pid = self.payloads.pop(key)
        nrefs = self.payload_refs[pid] - 1
        if nrefs:
            self.payload_refs[pid] = nrefs
            if nrefs == 1:
                self.shared_payloads -= 1
            return 0

        del self.payload_refs[pid]
        size = self.payload_sizes.pop(pid)
        self.total_weight -= size
        return size

    def _evict_key(self) -> KT:
        """Choose the next key to evict, without reading any value.

        The least recently used key is chosen, as in the classic LRU. A key whose
        payload is also referenced by another fast key, however, frees no memory when
        it is evicted on its own; in that case the least recently used key whose
        payload is exclusive is preferred, so that spilling the remaining references
        to a shared payload isn't required just to reclaim the shared memory.

        If every remaining payload is shared, any single eviction frees nothing; the
        oldest references are then spilled. While such an attempt is already in
        flight, raise ``KeyError`` instead of spilling the other references, so that a
        payload whose last reference is still being decided isn't treated as freed.
        """
        try:
            first = next(iter(self.heavy or self.order))
        except StopIteration:
            raise KeyError("evict(): dictionary is empty")

        if not self.shared_payloads:
            # No payload is referenced by more than one key, so the classic O(1)
            # selection is always correct.
            return first

        # Individual over-weight keys keep precedence over the LRU order. The classic
        # order is also scanned as a fallback in case the heavy keys are all shared
        # and cannot free memory on their own.
        groups = (self.heavy, self.order)

        # 1. Prefer a key whose eviction actually frees its payload.
        for group in groups:
            for key in group:
                if self._prospective_free(key) > 0:
                    return key

        if self._evicting_keys():
            # Every remaining payload is shared, so evicting one of its references
            # alone would free nothing. Some of those references are already being
            # evicted; wait for those attempts to be committed or cancelled instead of
            # spilling the remaining ones now, which would free nothing yet and could
            # treat a still-referenced payload as if it were down to its last owner.
            raise KeyError("evict(): all remaining keys are being evicted")

        # 2. Nothing is in flight: spill references, oldest first, until the shared
        # payload is fully released. Each individual spill frees no memory, but the
        # last one does; the outer loop keeps going until the target is reached.
        for group in groups:
            for key in group:
                if key not in self._cancel_evict:
                    return key

        raise KeyError("evict(): dictionary is empty")

    def _prospective_free(self, key: KT) -> float:
        """Weight that evicting *key* would free once the callbacks have succeeded."""
        if key in self._cancel_evict or self._is_evicting(key):
            return 0.0
        pid = self.payloads[key]
        if self.payload_refs[pid] > 1:
            # Another fast key still references the payload; evicting this one alone
            # frees nothing until the last reference leaves fast.
            return 0.0
        return self.payload_sizes[pid]

    def _is_evicting(self, key: KT) -> bool:
        return key in self._evicting.values()

    def _evicting_keys(self) -> set[KT]:
        with self.lock:
            return set(self._evicting.values())

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

    def __setitem__(self, key: KT, value: VT) -> None:
        self.set_noevict(key, value)
        try:
            self.evict_until_below_target()
        except Exception:
            if self.weights.get(key, 0) > self.n and key not in self.heavy:
                # weight(value) > n and evicting the key we just inserted failed.
                # Evict the rest of the LRU instead.
                try:
                    while len(self.d) > 1:
                        self.evict()
                except Exception:
                    pass
            raise

    @locked
    def set_noevict(self, key: KT, value: VT) -> None:
        """Variant of ``__setitem__`` that does not evict if the total weight exceeds n.
        Unlike ``__setitem__``, this method does not depend on the ``on_evict``
        functions to be thread-safe for its own thread-safety. It also is not prone to
        re-raising exceptions from the ``on_evict`` callbacks.
        """
        self.discard(key)
        weight = self._register(key, value)
        if key in self._cancel_evict:
            self._cancel_evict[key] = True
        self.d[key] = value
        self.order.add(key)
        if weight > self.n:
            self.heavy.add(key)  # Mark this key to be evicted first

    def evict_until_below_target(self, n: float | None = None) -> None:
        """Evict key/value pairs until the total weight falls below n

        Parameters
        ----------
        n: float, optional
            Total weight threshold to achieve. Defaults to self.n.
        """
        if n is None:
            n = self.n
        while self.total_weight + self.offset > n and not self.closed:
            try:
                self.evict()
            except KeyError:
                return  # Multithreaded race condition

    @locked
    def evict(
        self, key: KT | NoDefault = nodefault
    ) -> tuple[KT, VT, float] | tuple[None, None, float]:
        """Evict least recently used key, or least recently inserted key with individual
        weight > n, if any. You may also evict a specific key.

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
            key = self._evict_key()

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

        # Until this eviction is committed, its payload is still charged to
        # total_weight and the key is still in fast. Record the attempt so that a
        # concurrent reduction doesn't treat a shared payload as down to its last
        # reference, and doesn't select the same key twice.
        token = self._evict_token
        self._evict_token += 1
        self._evicting[token] = key

        self._cancel_evict[key] = False
        try:
            with self.unlock():
                # This may raise; e.g. if a callback tries storing to a full disk
                for cb in self.on_evict:
                    cb(key, value)

                if self._cancel_evict[key]:
                    for cb in self.on_cancel_evict:
                        cb(key, value)
                    return None, None, 0
        finally:
            del self._cancel_evict[key]
            self._evicting.pop(token, None)

        del self.d[key]
        self.order.remove(key)
        weight = self.weights[key]
        self._unregister(key)

        return key, value, weight

    @locked
    def __delitem__(self, key: KT) -> None:
        if key in self._cancel_evict:
            self._cancel_evict[key] = True
        del self.d[key]
        self.order.remove(key)
        self.heavy.discard(key)
        self._unregister(key)

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
