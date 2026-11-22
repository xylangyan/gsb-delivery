from __future__ import annotations

import heapq
from collections.abc import Callable, ItemsView, Iterator, MutableMapping, ValuesView
from itertools import chain
from typing import Any, Generic, NamedTuple, cast

from zict.common import KT, VT, ZictBase, close, discard, flush, locked
from zict.lru import LRU


def _inventory_sort_key(key: Any) -> Any:
    """Sort keys in byte order; ``str`` keys are ordered by their UTF-8 encoding,
    which matches the byte order of their binary representation.
    """
    return key.encode() if isinstance(key, str) else key


def _iter_prefix(mapping: MutableMapping[KT, VT], prefix: KT) -> Iterator[KT]:
    """Iterate over the keys of *mapping* that start with *prefix*.

    If the mapping defines ``iter_prefix(prefix)`` (e.g. a partitioned or sorted
    store), use it so that partitions that cannot contain the prefix are never
    listed; otherwise fall back to scanning all keys and filtering by prefix.
    """
    iter_prefix = getattr(mapping, "iter_prefix", None)
    if iter_prefix is not None:
        return iter_prefix(prefix)
    return (key for key in mapping if cast(Any, key).startswith(prefix))


class InventoryPage(NamedTuple, Generic[KT, VT]):
    """One page of a :class:`BufferInventory`.

    Attributes
    ----------
    items: list[tuple[key, value]]
        Matching key/value pairs, sorted by key in byte order. Values reflect
        the state of the buffer when the inventory was opened.
    next_cursor:
        Opaque page credential to pass to :meth:`BufferInventory.page` in order
        to fetch the next page, or ``None`` if the inventory is exhausted.
        The credential that was used to obtain this page remains valid and may
        be reused to fetch this same page again (e.g. after a client
        disconnect); already confirmed pages are never reprocessed.
    """

    items: list[tuple[KT, VT]]
    next_cursor: KT | None


class Buffer(ZictBase[KT, VT]):
    """Buffer one dictionary on top of another

    This creates a MutableMapping by combining two MutableMappings, one that
    feeds into the other when it overflows, based on an LRU mechanism.  When
    the first evicts elements these get placed into the second. When an item
    is retrieved from the second it is placed back into the first.

    Parameters
    ----------
    fast: MutableMapping
    slow: MutableMapping
    n: float
        Number of elements to keep, or total weight if ``weight`` is used.
    weight: f(k, v) -> float, optional
        Weight of each key/value pair (default: 1)
    fast_to_slow_callbacks: list of callables
        These functions run every time data moves from the fast to the slow
        mapping. They take two arguments, a key and a value.
        If an exception occurs during a fast_to_slow_callbacks (e.g a callback tried
        storing to disk and raised a disk full error) the key will remain in the LRU.
    slow_to_fast_callbacks: list of callables
        These functions run every time data moves form the slow to the fast mapping.

    Notes
    -----
    If you call methods of this class from multiple threads, access will be fast as long
    as all methods of ``fast``, plus ``slow.__contains__`` and ``slow.__delitem__``, are
    fast. ``slow.__getitem__``, ``slow.__setitem__`` and callbacks are not protected
    by locks.

    Examples
    --------
    >>> fast = {}
    >>> slow = Func(dumps, loads, File('storage/'))  # doctest: +SKIP
    >>> def weight(k, v):
    ...     return sys.getsizeof(v)
    >>> buff = Buffer(fast, slow, 1e8, weight=weight)  # doctest: +SKIP

    See Also
    --------
    LRU
    """

    fast: LRU[KT, VT]
    slow: MutableMapping[KT, VT]
    weight: Callable[[KT, VT], float]
    fast_to_slow_callbacks: list[Callable[[KT, VT], None]]
    slow_to_fast_callbacks: list[Callable[[KT, VT], None]]
    _cancel_restore: dict[KT, bool]
    _inventories: dict[int, BufferInventory[KT, VT]]
    _inventory_token_counter: int

    def __init__(
        self,
        fast: MutableMapping[KT, VT],
        slow: MutableMapping[KT, VT],
        n: float,
        weight: Callable[[KT, VT], float] = lambda k, v: 1,
        fast_to_slow_callbacks: Callable[[KT, VT], None]
        | list[Callable[[KT, VT], None]]
        | None = None,
        slow_to_fast_callbacks: Callable[[KT, VT], None]
        | list[Callable[[KT, VT], None]]
        | None = None,
    ):
        super().__init__()
        self.fast = LRU(
            n,
            fast,
            weight=weight,
            on_evict=[self.fast_to_slow],
            on_cancel_evict=[self._cancel_evict],
        )
        self.slow = slow
        self.weight = weight
        if callable(fast_to_slow_callbacks):
            fast_to_slow_callbacks = [fast_to_slow_callbacks]
        if callable(slow_to_fast_callbacks):
            slow_to_fast_callbacks = [slow_to_fast_callbacks]
        self.fast_to_slow_callbacks = fast_to_slow_callbacks or []
        self.slow_to_fast_callbacks = slow_to_fast_callbacks or []
        self._cancel_restore = {}
        self._inventories = {}
        self._inventory_token_counter = 0

    @property
    def n(self) -> float:
        """Maximum weight in the fast mapping before eviction happens.
        Can be updated; this won't trigger eviction by itself; you should call
        :meth:`evict_until_below_target` afterwards.

        See also
        --------
        offset
        evict_until_below_target
        LRU.n
        LRU.offset
        """
        return self.fast.n

    @n.setter
    def n(self, value: float) -> None:
        self.fast.n = value

    @property
    def offset(self) -> float:
        """Offset to add to the total weight in the fast buffer to determine when
        eviction happens. Note that increasing offset is not the same as decreasing n,
        as the latter also changes what keys qualify as "heavy" and should not be stored
        in fast.

        Always starts at zero and can be updated; this won't trigger eviction by itself;
        you should call :meth:`evict_until_below_target` afterwards.

        See also
        --------
        n
        evict_until_below_target
        LRU.n
        LRU.offset
        """
        return self.fast.offset

    @offset.setter
    def offset(self, value: float) -> None:
        self.fast.offset = value

    def fast_to_slow(self, key: KT, value: VT) -> None:
        self.slow[key] = value
        try:
            for cb in self.fast_to_slow_callbacks:
                cb(key, value)
        # LRU catches exception, raises and makes sure keys are not lost and located in
        # fast.
        except Exception:
            del self.slow[key]
            raise

    def slow_to_fast(self, key: KT) -> VT:
        self._cancel_restore[key] = False
        try:
            with self.unlock():
                value = self.slow[key]
            if self._cancel_restore[key]:
                raise KeyError(key)
        finally:
            del self._cancel_restore[key]

        # Avoid useless movement for heavy values
        w = self.weight(key, value)
        if w <= self.n:
            # Multithreaded edge case:
            # - Thread 1 starts slow_to_fast(x) and puts it at the top of fast
            # - This causes the eviction of older key(s)
            # - While thread 1 is evicting older keys, thread 2 is loading fast with
            #   set_noevict()
            # - By the time the eviction of the older key(s) is done, there is
            #   enough weight in fast that thread 1 will spill x
            # - If the below code was just `self.fast[key] = value; del
            #   self.slow[key]` now the key would be in neither slow nor fast!
            self.fast.set_noevict(key, value)
            del self.slow[key]

        with self.unlock():
            self.fast.evict_until_below_target()
            for cb in self.slow_to_fast_callbacks:
                cb(key, value)

        return value

    @locked
    def __getitem__(self, key: KT) -> VT:
        try:
            return self.fast[key]
        except KeyError:
            return self.slow_to_fast(key)

    def __setitem__(self, key: KT, value: VT) -> None:
        with self.lock:
            self._inventory_retain(key)
            discard(self.slow, key)
            if key in self._cancel_restore:
                self._cancel_restore[key] = True
        self.fast[key] = value

    @locked
    def set_noevict(self, key: KT, value: VT) -> None:
        """Variant of ``__setitem__`` that does not move keys from fast to slow if the
        total weight exceeds n
        """
        self._inventory_retain(key)
        discard(self.slow, key)
        if key in self._cancel_restore:
            self._cancel_restore[key] = True
        self.fast.set_noevict(key, value)

    def evict_until_below_target(self, n: float | None = None) -> None:
        """Wrapper around :meth:`zict.LRU.evict_until_below_target`.
        Presented here to allow easier overriding.
        """
        self.fast.evict_until_below_target(n)

    @locked
    def __delitem__(self, key: KT) -> None:
        if key in self._cancel_restore:
            self._cancel_restore[key] = True
        self._inventory_retain(key)
        try:
            del self.fast[key]
        except KeyError:
            del self.slow[key]

    @locked
    def _cancel_evict(self, key: KT, value: VT) -> None:
        discard(self.slow, key)

    def open_inventory(self, prefix: KT) -> BufferInventory[KT, VT]:
        """Open a point-in-time inventory of the keys matching *prefix*.

        The returned :class:`BufferInventory` can be exported page by page, in
        byte order of the keys, and reflects the state of the buffer at the
        moment of this call:

        - keys inserted afterwards never appear in the inventory;
        - keys overwritten or deleted afterwards are exported with their old
          value. To this end, the buffer retains the old values of the matching
          keys affected by such changes - and only of those; an inventory never
          holds a full key table nor a copy of all values;
        - any number of inventories may be open at the same time on the same
          buffer; they progress independently and may stop at different pages.

        Opening an inventory is O(1): it does not list the keys of the fast or
        slow mappings, nor does it read any value. Regular access, movement of
        keys between the two mappings, and eviction remain available while
        inventories are open.

        Notes
        -----
        If the slow mapping (or the mapping wrapped by the fast LRU) defines
        ``iter_prefix(prefix)``, it is used to enumerate only the keys that may
        match the prefix, so that partitions of the slow mapping which cannot
        contain matching keys are never listed. Otherwise, keys are scanned and
        filtered by prefix on every page fetch.
        """
        with self.lock:
            inv = BufferInventory(self, self._inventory_token_counter, prefix)
            self._inventory_token_counter += 1
            self._inventories[inv.token] = inv
            return inv

    def close_inventory(self, inventory: BufferInventory[KT, VT] | int) -> None:
        """Close an inventory and release the history retained for it.
        Other inventories of this buffer are unaffected.
        """
        token = inventory.token if isinstance(inventory, BufferInventory) else inventory
        with self.lock:
            inv = self._inventories.get(token)
        if inv is not None:
            inv.close()

    def _inventory_retain(self, key: KT) -> None:
        """Record the pre-mutation state of *key* for every open inventory whose
        prefix matches. Must be called before the key is overwritten or deleted.
        """
        if not self._inventories:
            return
        for inv in self._inventories.values():
            if (
                key in inv.retained
                or key in inv.excluded
                or not cast(Any, key).startswith(inv.prefix)
            ):
                continue
            try:
                inv.retained[key] = self.fast.d[key]
            except KeyError:
                try:
                    inv.retained[key] = self.slow[key]
                except KeyError:
                    # The key does not exist yet; it was inserted after the
                    # inventory was opened and must never appear in it.
                    inv.excluded.add(key)

    def _read_inventory_page(
        self, inv: BufferInventory[KT, VT], cursor: KT | None, limit: int
    ) -> InventoryPage[KT, VT]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1; got {limit}")
        cursor_key = _inventory_sort_key(cursor) if cursor is not None else None

        def candidates(mapping: MutableMapping[KT, VT]) -> Iterator[KT]:
            for key in _iter_prefix(mapping, inv.prefix):
                if key in inv.excluded:
                    continue
                if cursor_key is not None and _inventory_sort_key(key) <= cursor_key:
                    continue
                yield key

        # Retry if a concurrent write mutates the underlying mappings or the
        # retained history while scanning them. Never changes the LRU order of
        # the fast mapping nor moves keys between fast and slow.
        while True:
            try:
                fast_keys = heapq.nsmallest(
                    limit, candidates(self.fast.d), key=_inventory_sort_key
                )
                slow_keys = heapq.nsmallest(
                    limit, candidates(self.slow), key=_inventory_sort_key
                )
                # Keys deleted after the inventory was opened are still owed
                # their old value, even if they were never re-inserted.
                retained_keys = [
                    key
                    for key in inv.retained
                    if cursor_key is None or _inventory_sort_key(key) > cursor_key
                ]
                break
            except RuntimeError:
                pass

        merged = sorted(
            set(fast_keys) | set(slow_keys) | set(retained_keys),
            key=_inventory_sort_key,
        )
        exhausted = (
            len(fast_keys) < limit
            and len(slow_keys) < limit
            and len(merged) <= limit
        )
        page_keys = merged[:limit]

        items: list[tuple[KT, VT]] = []
        for key in page_keys:
            if key in inv.retained:
                items.append((key, inv.retained[key]))
                continue
            try:
                value = self.fast.d[key]
            except KeyError:
                try:
                    value = self.slow[key]
                except KeyError:
                    # Key was deleted concurrently, bypassing the Buffer API
                    continue
            items.append((key, value))

        next_cursor = None if exhausted else page_keys[-1]
        return InventoryPage(items, next_cursor)

    def values(self) -> ValuesView[VT]:
        return BufferValuesView(self)

    def items(self) -> ItemsView[KT, VT]:
        return BufferItemsView(self)

    def __len__(self) -> int:
        with self.lock, self.fast.lock:
            return (
                len(self.fast)
                + len(self.slow)
                - sum(
                    k in self.fast and k in self.slow
                    for k in chain(self._cancel_restore, self.fast._cancel_evict)
                )
            )

    def __iter__(self) -> Iterator[KT]:
        """Make sure that the iteration is not disrupted if you evict/restore a key in
        the middle of it
        """
        seen = set()
        while True:
            try:
                for d in (self.fast, self.slow):
                    for key in d:
                        if key not in seen:
                            seen.add(key)
                            yield key
                return
            except RuntimeError:
                pass

    def __contains__(self, key: object) -> bool:
        return key in self.fast or key in self.slow

    def __str__(self) -> str:
        return f"Buffer<{self.fast}, {self.slow}>"

    __repr__ = __str__

    def flush(self) -> None:
        flush(self.fast, self.slow)

    def close(self) -> None:
        for inv in list(self._inventories.values()):
            inv.close()
        close(self.fast, self.slow)


class BufferItemsView(ItemsView[KT, VT]):
    _mapping: Buffer  # FIXME CPython implementation detail
    __slots__ = ()

    def __iter__(self) -> Iterator[tuple[KT, VT]]:
        # Avoid changing the LRU
        return chain(self._mapping.fast.items(), self._mapping.slow.items())


class BufferValuesView(ValuesView[VT]):
    _mapping: Buffer  # FIXME CPython implementation detail
    __slots__ = ()

    def __contains__(self, value: object) -> bool:
        # Avoid changing the LRU
        return any(value == v for v in self)

    def __iter__(self) -> Iterator[VT]:
        # Avoid changing the LRU
        return chain(self._mapping.fast.values(), self._mapping.slow.values())


class BufferInventory(Generic[KT, VT]):
    """Point-in-time, paginated inventory of the keys of a :class:`Buffer` that
    match a given prefix. Create with :meth:`Buffer.open_inventory`.

    Pages are fetched with :meth:`page` and are always sorted by key in byte
    order. Every page returns an opaque credential (``next_cursor``) that
    identifies the following page; fetching a page does not change the LRU
    order of the fast mapping, nor does it move keys between the fast and slow
    mappings.

    See Also
    --------
    Buffer.open_inventory
    InventoryPage
    """

    buffer: Buffer[KT, VT]
    token: int
    prefix: KT
    #: Old values of matching keys overwritten or deleted since the inventory
    #: was opened. Never holds more than the keys affected by such changes.
    retained: dict[KT, VT]
    #: Matching keys inserted after the inventory was opened; never exported.
    excluded: set[KT]
    closed: bool

    def __init__(self, buffer: Buffer[KT, VT], token: int, prefix: KT):
        self.buffer = buffer
        self.token = token
        self.prefix = prefix
        self.retained = {}
        self.excluded = set()
        self.closed = False

    def page(self, cursor: KT | None = None, limit: int = 100) -> InventoryPage[KT, VT]:
        """Fetch one page of the inventory.

        Parameters
        ----------
        cursor:
            Page credential previously returned as ``next_cursor``, or ``None``
            to fetch the first page. Passing the same cursor again always
            returns the same page, so a client that disconnects before
            receiving the acknowledgement of a page can retry with the
            credential it already holds, and then keep turning pages without
            reprocessing confirmed ones.
        limit: int
            Maximum number of items per page.

        Returns
        -------
        :class:`InventoryPage` with up to ``limit`` items and the credential
        for the next page (``None`` when the inventory is exhausted).

        Notes
        -----
        If reading a value from the slow mapping fails (e.g. a transient
        error), the exception is propagated and no state is consumed: retrying
        with the same cursor returns the same batch of keys and values.
        """
        if self.closed:
            raise ValueError("Inventory is closed")
        return self.buffer._read_inventory_page(self, cursor, limit)

    def close(self) -> None:
        """Close the inventory and release the history retained for it.
        Idempotent; does not affect other inventories of the same buffer.
        """
        if self.closed:
            return
        self.closed = True
        with self.buffer.lock:
            self.buffer._inventories.pop(self.token, None)
        self.retained.clear()
        self.excluded.clear()

    def __enter__(self) -> BufferInventory[KT, VT]:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
