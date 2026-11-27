"""
    test_ledger_ordering
    ~~~~~~~~~~~~~~~~~~~~

    Tests for partial order, windows and stable topo sorting.
"""
import pytest

from ulid import consts
from ulid.ledger import Ordering, PartialOrder, Window, WindowKind
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def test_open_window_contains_everything():
    window = Window.open()
    assert window.kind is WindowKind.OPEN
    assert window.contains(make_ulid(0, 0))
    assert window.contains(consts.MAX_ULID)


def test_millisecond_window_bounds_are_inclusive():
    window = Window.millisecond(100, 200)
    assert window.contains(make_ulid(100, consts.MAX_RANDOMNESS.int))
    assert window.contains(make_ulid(200, 0))
    assert not window.contains(make_ulid(99, 0))
    assert not window.contains(make_ulid(201, 0))


def test_suffix_window_restricts_both_parts():
    window = Window.suffix(100, 100, 5, 9)
    assert window.contains(make_ulid(100, 5))
    assert window.contains(make_ulid(100, 9))
    assert not window.contains(make_ulid(100, 4))
    assert not window.contains(make_ulid(101, 5))


def test_window_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        Window.millisecond(200, 100)
    with pytest.raises(ValueError):
        Window.suffix(100, 100, 9, 5)


def test_suffix_window_requires_all_bounds():
    with pytest.raises(ValueError):
        Window(WindowKind.SUFFIX, lower_millis=1, upper_millis=2)


def test_partial_order_transitive_before():
    order = PartialOrder()
    order.add_before('a', 'b')
    order.add_before('b', 'c')
    assert order.is_before('a', 'c')
    assert not order.is_before('c', 'a')
    assert order.successors('a') == frozenset({'b', 'c'})
    assert order.predecessors('c') == frozenset({'a', 'b'})


def test_partial_order_unrelated_items_are_incomparable():
    order = PartialOrder()
    order.add_before('a', 'b')
    assert not order.is_before('a', 'x')
    assert not order.is_before('x', 'a')


def test_topo_sort_respects_edges_and_stable_key():
    order = PartialOrder()
    order.add_before('x', 'a')
    result = order.topo_sort(['a', 'b', 'x'], key=lambda value: value)
    assert result.index('x') < result.index('a')
    assert result == ['b', 'x', 'a']


def test_topo_sort_detects_cycle():
    order = PartialOrder()
    order.add_before('a', 'b')
    order.add_before('b', 'a')
    with pytest.raises(ValueError):
        order.topo_sort(['a', 'b'])


def test_ordering_validate_uses_item_window():
    window = Window.millisecond(100, 200)
    ordering = Ordering(windows={'a': window})
    ordering.validate('a', make_ulid(150, 0))
    with pytest.raises(ValueError):
        ordering.validate('a', make_ulid(300, 0))
    ordering.validate('unknown', consts.MAX_ULID)
