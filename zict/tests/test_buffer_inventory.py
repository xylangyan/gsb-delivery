from __future__ import annotations

import threading
from collections.abc import Iterator, MutableMapping

import pytest

from zict import Buffer, BufferInventory, InventoryPage


class LoggingDict(dict):
    """dict that logs iteration and value reads"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iter_count = 0
        self.reads = []

    def __iter__(self):
        self.iter_count += 1
        return super().__iter__()

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)


class PartitionedSlow(MutableMapping):
    """Slow mapping sharded into partitions by the first character of the key.

    Implements ``iter_prefix`` so that a prefix inventory never has to list
    partitions that cannot match. Logs partition listings and value reads, and
    can be told to fail once on the read of a given key.
    """

    def __init__(self):
        self.partitions: dict[str, LoggingDict] = {}
        self.listed_partitions: list[str] = []
        self.value_reads: list[str] = []
        self.fail_once: set[str] = set()

    def _partition(self, key: str) -> LoggingDict:
        return self.partitions.setdefault(key[0], LoggingDict())

    def __getitem__(self, key: str):
        self.value_reads.append(key)
        if key in self.fail_once:
            self.fail_once.discard(key)
            raise RuntimeError("transient slow-layer failure")
        return self.partitions[key[0]][key]

    def __setitem__(self, key: str, value) -> None:
        self._partition(key)[key] = value

    def __delitem__(self, key: str) -> None:
        del self.partitions[key[0]][key]

    def __iter__(self) -> Iterator[str]:
        for name in sorted(self.partitions):
            self.listed_partitions.append(name)
            yield from self.partitions[name]

    def __len__(self) -> int:
        return sum(len(p) for p in self.partitions.values())

    def __contains__(self, key: object) -> bool:
        try:
            return key in self.partitions[key[0]]  # type: ignore[index]
        except KeyError:
            return False

    def iter_prefix(self, prefix: str) -> Iterator[str]:
        self.listed_partitions.append(prefix[0])
        return iter(self.partitions.get(prefix[0], ()))


def collect(inv: BufferInventory, limit: int = 100) -> list[tuple[str, str]]:
    """Fetch all remaining pages of an inventory"""
    items = []
    cursor = None
    while True:
        page = inv.page(cursor, limit=limit)
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def test_inventory_basic_pagination():
    buff = Buffer({}, {}, n=100)
    for i in range(10):
        buff[f"k{i:02d}"] = i
    buff["other"] = -1

    inv = buff.open_inventory("k")
    page0 = inv.page(limit=4)
    assert page0 == InventoryPage([(f"k{i:02d}", i) for i in range(4)], "k03")
    page1 = inv.page(page0.next_cursor, limit=4)
    assert page1 == InventoryPage([(f"k{i:02d}", i) for i in range(4, 8)], "k07")
    page2 = inv.page(page1.next_cursor, limit=4)
    assert page2 == InventoryPage([(f"k{i:02d}", i) for i in range(8, 10)], None)

    # Retrying with the same credential returns the same page
    assert inv.page(limit=4) == page0
    assert inv.page("k03", limit=4) == page1
    assert inv.page("k07", limit=4) == page2

    # The irrelevant key never shows up
    assert "other" not in dict(collect(inv))
    inv.close()


def test_inventory_byte_order():
    buff = Buffer({}, {}, n=100)
    buff["b"] = 1
    buff["a"] = 2
    buff["a\x7f"] = 3
    buff["aé"] = 4  # UTF-8: b"a\xc3\xa9", sorts after b"a\x7f"
    inv = buff.open_inventory("a")
    assert [k for k, _ in collect(inv)] == ["a", "a\x7f", "aé"]
    inv.close()


def test_inventory_retains_deleted_key_without_reinsert():
    buff = Buffer({}, {}, n=100)
    buff["a1"] = "old-a1"
    buff["a2"] = "old-a2"
    inv = buff.open_inventory("a")
    del buff["a2"]
    assert collect(inv) == [("a1", "old-a1"), ("a2", "old-a2")]
    inv.close()


def test_inventory_overwrite_and_insert_after_open():
    buff = Buffer({}, {}, n=100)
    buff["a1"] = "old"
    inv = buff.open_inventory("a")
    buff["a1"] = "new"  # overwritten after open: old value retained
    buff["a2"] = "new"  # inserted after open: excluded
    assert collect(inv) == [("a1", "old")]
    # Only the affected keys are tracked; no full key table or value copy
    assert inv.retained == {"a1": "old"}
    assert inv.excluded == {"a2"}
    inv.close()


def test_inventory_page_validation_and_close():
    buff = Buffer({}, {}, n=100)
    buff["a"] = 1
    inv = buff.open_inventory("a")
    with pytest.raises(ValueError, match="limit"):
        inv.page(limit=0)
    inv.close()
    inv.close()  # idempotent
    with pytest.raises(ValueError, match="closed"):
        inv.page()
    assert not buff._inventories


def test_buffer_close_closes_inventories():
    buff = Buffer({}, {}, n=100)
    buff["a"] = 1
    inv = buff.open_inventory("a")
    buff.close()
    assert inv.closed
    assert not buff._inventories


def test_inventory_does_not_change_lru_order():
    fast, slow = {}, {}
    buff = Buffer(fast, slow, n=2)
    buff["a"] = 1
    buff["b"] = 2
    buff["c"] = 3  # evicts 'a' to slow
    assert set(fast) == {"b", "c"}
    assert set(slow) == {"a"}

    order_before = list(buff.fast.order)
    inv = buff.open_inventory("")
    assert collect(inv, limit=1) == [("a", 1), ("b", 2), ("c", 3)]
    inv.close()
    # Fetching pages did not change the LRU order nor move keys around
    assert list(buff.fast.order) == order_before
    assert set(fast) == {"b", "c"}
    assert set(slow) == {"a"}


def test_inventory_scenario():
    """Two inventories over the same prefix, interleaved with overwrites,
    deletes, re-inserts, an in-flight eviction that gets cancelled, and a
    transient slow-layer failure.
    """
    slow = PartitionedSlow()
    # Junk partitions that no inventory of prefix "a" may ever list
    for p in "xyz":
        for i in range(5):
            slow[f"{p}{i}"] = f"junk-{p}{i}"
    fast = LoggingDict()

    eviction_started = threading.Event()
    eviction_release = threading.Event()
    block_once = []

    def fast_to_slow_cb(key, value):
        if block_once and block_once[0] == key:
            block_once.pop(0)
            eviction_started.set()
            eviction_release.wait(30)

    buff = Buffer(fast, slow, n=3, fast_to_slow_callbacks=fast_to_slow_cb)
    # Seed prefix "a" with a10, a20, a30, a40 plus one irrelevant key.
    # Land a10 and a40 in slow; keep a20 in fast.
    buff["a10"] = "old-a10"
    buff["a40"] = "old-a40"
    buff["b00"] = "irrelevant"
    buff["a30"] = "old-a30"
    buff["a20"] = "old-a20"
    assert set(fast) == {"b00", "a30", "a20"}
    assert set(slow.partitions["a"]) == {"a10", "a40"}

    # Reset instrumentation after seeding
    fast.iter_count = 0
    fast.reads.clear()
    slow.listed_partitions.clear()
    slow.value_reads.clear()

    # Opening an inventory must not list the key tables nor read any value
    inv_a = buff.open_inventory("a")
    assert fast.iter_count == 0
    assert slow.listed_partitions == []
    assert slow.value_reads == []

    # 甲 (A) fetches its first two-item page; the client never receives the
    # receipt and will retry with the same credential later
    page_a1 = inv_a.page(limit=2)
    assert page_a1.items == [("a10", "old-a10"), ("a20", "old-a20")]
    assert page_a1.next_cursor == "a20"
    # Only the matching partition of the slow layer was listed
    assert slow.listed_partitions == ["a"]
    assert slow.value_reads == ["a10"]

    # Move a20 from fast to slow; the callback has not completed yet
    block_once.append("a20")
    t = threading.Thread(target=buff.fast.evict, args=("a20",))
    t.start()
    assert eviction_started.wait(30)
    assert "a20" in fast and "a20" in slow.partitions["a"]  # in flight

    # Overwrite a20 through the regular write entry, then delete a30
    buff["a20"] = "new-a20"
    del buff["a30"]

    # 乙 (B) opens its own inventory; the two may stop at different pages
    inv_b = buff.open_inventory("a")

    # Re-insert a30 with a new value; insert a25 between a20 and a30
    buff["a30"] = "new-a30"
    buff["a25"] = "new-a25"

    # The in-flight eviction of a20 comes back and is cancelled
    eviction_release.set()
    t.join(30)
    assert not t.is_alive()
    assert fast["a20"] == "new-a20"
    assert "a20" not in slow.partitions["a"]

    # Only the keys affected by post-open changes are tracked per inventory
    assert inv_a.retained == {"a20": "old-a20", "a30": "old-a30"}
    assert inv_a.excluded == {"a25"}
    assert inv_b.retained == {}
    assert inv_b.excluded == {"a25", "a30"}

    # Inventory pages must not change the LRU order nor move keys
    order_before = list(buff.fast.order)
    fast_snapshot = dict(fast)
    slow_snapshot = {k: v for p in slow.partitions.values() for k, v in p.items()}

    # 乙 fetches its first page, then re-reads the same page
    page_b1 = inv_b.page(limit=2)
    assert page_b1.items == [("a10", "old-a10"), ("a20", "new-a20")]
    assert page_b1.next_cursor == "a20"
    assert inv_b.page(limit=2) == page_b1

    # 甲 retries with its original page credential and gets the same page
    assert inv_a.page(limit=2) == page_a1

    assert list(buff.fast.order) == order_before
    assert dict(fast) == fast_snapshot
    assert {k: v for p in slow.partitions.values() for k, v in p.items()} == slow_snapshot

    # Regular access, movement and eviction stay online while inventories run
    assert buff["a10"] == "old-a10"  # moves a10 back to fast; evicts a20 to slow
    assert "a10" in fast
    assert slow.partitions["a"]["a20"] == "new-a20"

    # From now on, the already confirmed first pages must not be re-read
    slow.value_reads.clear()

    # A subsequent page hits a transient failure of the independent slow layer
    slow.fail_once.add("a40")
    with pytest.raises(RuntimeError, match="transient slow-layer failure"):
        inv_a.page("a20", limit=2)
    # Retrying with the same credential returns the same batch of keys and values
    page_a2 = inv_a.page("a20", limit=2)
    assert page_a2.items == [("a30", "old-a30"), ("a40", "old-a40")]
    assert page_a2.next_cursor is None

    # 乙 sees the new a20, misses a30, and neither inventory absorbs the
    # late-inserted a25
    page_b2 = inv_b.page("a20", limit=2)
    assert page_b2.items == [("a40", "old-a40")]
    assert page_b2.next_cursor is None

    # The confirmed previous page was not re-checked on the slow layer
    assert "a10" not in slow.value_reads

    # Full byte-ordered results of the two chains
    chain_a = page_a1.items + page_a2.items
    chain_b = page_b1.items + page_b2.items
    assert chain_a == [
        ("a10", "old-a10"),
        ("a20", "old-a20"),
        ("a30", "old-a30"),
        ("a40", "old-a40"),
    ]
    assert chain_b == [
        ("a10", "old-a10"),
        ("a20", "new-a20"),
        ("a40", "old-a40"),
    ]
    for chain in (chain_a, chain_b):
        assert [k for k, _ in chain] == sorted((k for k, _ in chain), key=str.encode)

    # Irrelevant partitions of the slow layer were never listed
    assert set(slow.listed_partitions) == {"a"}

    # Closing 甲 only reclaims the history used by 甲
    inv_a.close()
    assert inv_a.retained == {}
    assert set(buff._inventories) == {inv_b.token}
    with pytest.raises(ValueError, match="closed"):
        inv_a.page()

    # 乙 still sees its own snapshot of further changes
    buff["a40"] = "newer-a40"
    assert inv_b.retained == {"a40": "old-a40"}
    assert inv_b.page("a20", limit=2).items == [("a40", "old-a40")]

    inv_b.close()
    assert not buff._inventories
