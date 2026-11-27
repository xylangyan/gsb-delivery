"""
    test_ledger_registry
    ~~~~~~~~~~~~~~~~~~~~

    Tests for the catalog registry: identity, freeze rules, aliases, generations.
"""
import pytest

from ulid.ledger import ItemState, Ledger, Window
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def test_register_keeps_original_and_current_ulid():
    ledger = Ledger()
    item = ledger.register('a', make_ulid(1, 2), library_code='L-a')
    assert item.original_ulid == item.current_ulid
    assert item.library_code == 'L-a'
    assert item.state is ItemState.DRAFT
    assert item.generation == 0


def test_register_rejects_duplicate_item_and_duplicate_ulid_in_generation():
    ledger = Ledger()
    ledger.register('a', make_ulid(1, 2))
    with pytest.raises(ValueError):
        ledger.register('a', make_ulid(1, 3))
    with pytest.raises(ValueError):
        ledger.register('b', make_ulid(1, 2))


def test_confirmed_items_are_frozen_and_cannot_renumber():
    ledger = Ledger()
    ledger.register('a', make_ulid(1, 2))
    ledger.confirm('a')
    assert ledger.get('a').is_frozen
    with pytest.raises(ValueError):
        ledger.reassign('a', make_ulid(1, 9), 1)


def test_delivered_unconfirmed_item_can_renumber_with_alias():
    ledger = Ledger()
    old = make_ulid(1, 2)
    ledger.register('a', old)
    ledger.deliver('a')
    assert not ledger.get('a').is_frozen
    new = make_ulid(1, 5)
    updated = ledger.reassign('a', new, 1)
    assert updated.current_ulid == new
    assert updated.original_ulid == old
    assert ledger.alias_history('a') == ((1, old, new),)
    assert ledger.resolve(old) == 'a'
    assert ledger.resolve(new) == 'a'


def test_reassign_respects_window():
    ledger = Ledger()
    ledger.register('a', make_ulid(1, 2))
    ledger.ordering.windows['a'] = Window.millisecond(10, 20)
    with pytest.raises(ValueError):
        ledger.reassign('a', make_ulid(30, 0), 1)


def test_catalog_filters_by_generation():
    ledger = Ledger()
    ledger.register('a', make_ulid(1, 2), generation=0)
    ledger.register('b', make_ulid(1, 3), generation=1)
    assert set(ledger.catalog(0)) == {'a'}
    assert set(ledger.catalog(1)) == {'b'}
    assert set(ledger.catalog()) == {'a', 'b'}


def test_resolve_understands_string_and_int_forms():
    ledger = Ledger()
    value = make_ulid(1, 2)
    ledger.register('a', value)
    assert ledger.resolve(value.str) == 'a'
    assert ledger.resolve(value.int) == 'a'
    assert ledger.resolve(make_ulid(9, 9)) is None
