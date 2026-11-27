"""
    test_ledger_reader
    ~~~~~~~~~~~~~~~~~~

    Tests for reader paging, signing, delivery idempotency and reopen continuation.
"""
import pytest

from ulid.ledger import (
    ControlledClock,
    Ledger,
    Reader,
    ReaderService,
    ReaderSpec,
)
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def build_ledger():
    ledger = Ledger()
    for index, item_id in enumerate(['a', 'b', 'c', 'd'], start=1):
        ledger.register(item_id, make_ulid(100, index), library_code='L-{}'.format(item_id))
    return ledger


def test_next_page_advances_and_each_item_appears_once():
    service = ReaderService(build_ledger())
    reader = service.enroll(ReaderSpec('r', frozenset(['a', 'b', 'c']), order_key=lambda i: i))
    first = service.next_page(reader, 2)
    assert first.item_ids == ('a', 'b')
    assert reader.bound == first.lower_bound
    service.sign(reader, first)
    second = service.next_page(reader, 2)
    assert second.item_ids == ('c',)
    assert service.next_page(reader, 2) is None


def test_signed_pages_are_recorded_with_generation():
    service = ReaderService(build_ledger())
    reader = service.enroll(ReaderSpec('r', frozenset(['a', 'b']), order_key=lambda i: i))
    page = service.next_page(reader, 2)
    service.sign(reader, page)
    assert reader.signed_pages == [(page.generation, ('a', 'b'))]


def test_deliver_is_idempotent_and_marks_ledger_item():
    service = ReaderService(build_ledger())
    reader = service.enroll(ReaderSpec('r', frozenset(['a', 'b']), order_key=lambda i: i))
    assert service.deliver(reader, 'a') is True
    assert service.deliver(reader, 'a') is False
    from ulid.ledger import ItemState
    assert service.ledger.get('a').state is ItemState.DELIVERED


def test_deliver_rejects_item_outside_scope():
    service = ReaderService(build_ledger())
    reader = service.enroll(ReaderSpec('r', frozenset(['a']), order_key=lambda i: i))
    with pytest.raises(ValueError):
        service.deliver(reader, 'z')


def test_remaining_excludes_delivered_and_unregistered_backfills():
    service = ReaderService(build_ledger())
    reader = service.enroll(
        ReaderSpec('r', frozenset(['a', 'b', 'x']), order_key=lambda i: i))
    service.deliver(reader, 'a')
    assert service.remaining(reader) == ['b']


def test_reopen_pages_cover_each_undelivered_item_once_with_traceable_ids():
    service = ReaderService(build_ledger())
    reader = service.enroll(ReaderSpec('r', frozenset(['a', 'b', 'c']), order_key=lambda i: i))
    service.deliver(reader, 'a')
    pages = service.reopen_pages(reader, 2)
    flat = [item_id for page in pages for item_id in page.item_ids]
    assert flat == ['b', 'c']
    assert len(flat) == len(set(flat))
    for page in pages:
        for item_id, value in page.entries:
            assert service.ledger.current_ulid(item_id) == value


def test_library_view_orders_by_library_code_then_partial_order():
    ledger = build_ledger()
    service = ReaderService(ledger)
    view = service.library_view(['d', 'a', 'c'])
    assert view == ['a', 'c', 'd']


def test_resume_request_requires_a_bound():
    service = ReaderService(build_ledger())
    reader = Reader('r', ReaderSpec('r', frozenset(['a']), order_key=lambda i: i))
    with pytest.raises(ValueError):
        reader.resume_request()
    service.adopt(reader)
    page = service.next_page(reader, 1)
    service.sign(reader, page)
    request = reader.resume_request()
    assert request.old_bound == reader.bound
    assert request.signed_pages == tuple(reader.signed_pages)
