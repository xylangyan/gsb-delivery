"""
    test_ledger_service
    ~~~~~~~~~~~~~~~~~~~

    Tests for the facade: joint publication of catalog and progress map,
    export book contents, and reopen semantics.
"""
from ulid.ledger import (
    BackfillRequest,
    ControlledClock,
    ItemState,
    Ledger,
    LedgerService,
    NumberingService,
    Ordering,
    PartialOrder,
    ReaderService,
    ReaderSpec,
    ReticketPlanner,
    Window,
)
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def build_facade():
    ledger = Ledger()
    for index, item_id in enumerate(['a', 'b', 'c'], start=1):
        ledger.register(item_id, make_ulid(100, 2 * index))
    service = ReaderService(ledger)
    reader = service.enroll(
        ReaderSpec('A', frozenset(['a', 'b', 'x', 'c']),
                   order_key=lambda i: {'a': 1, 'b': 2, 'x': 3, 'c': 4}[i],
                   reticket_allowed=True))
    numbering = NumberingService(ControlledClock(1000))
    planner = ReticketPlanner(ledger, service, numbering)
    return LedgerService(ledger, numbering, service), planner, reader


def test_publication_carries_catalog_progress_and_aliases_together():
    facade, planner, reader = build_facade()
    facade.reader_service.deliver(reader, 'a')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 4, 4))])
    publication = facade.publish(plan, 'r1')
    assert set(publication.catalog) == {'a', 'b', 'c', 'x'}
    assert ('A', 'bound') in publication.progress_map
    assert publication.generation == 1
    if plan.renames:
        assert publication.alias_map
    assert facade.publications[1] is publication


def test_export_book_records_delivered_previewed_and_specs():
    facade, _planner, reader = build_facade()
    first = facade.reader_service.next_page(reader, 1)
    facade.reader_service.sign(reader, first)
    facade.reader_service.deliver(reader, 'a')
    second = facade.reader_service.next_page(reader, 1)
    assert second.item_ids == ('b',)
    book = facade.export_book()
    assert set(book.items) == {'a', 'b', 'c'}
    assert book.delivered['A'] == frozenset({'a'})
    assert book.reader_specs['A'].reticket_allowed is True
    assert 'b' in book.previewed['A']


def test_reopen_keeps_only_issued_generation_and_reader_progress():
    facade, planner, reader = build_facade()
    facade.reader_service.deliver(reader, 'a')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.millisecond(100, 100))])
    facade.publish(plan, 'r1')
    reopened = facade.reopen()
    assert reopened.numbering.issued_generations() == (1,)
    resumed_reader = reopened.reader_service.get('A')
    assert resumed_reader.delivered == frozenset({'a'})
    pages = reopened.continue_resume(resumed_reader, 2)
    flat = [item_id for page in pages for item_id in page.item_ids]
    assert flat == ['b', 'x', 'c']
    assert len(flat) == len(set(flat))


def test_partial_order_overrides_reader_key_while_staying_stable():
    partial = PartialOrder()
    partial.add_before('c', 'a')
    ordering = Ordering(partial)
    ledger = Ledger(ordering)
    for index, item_id in enumerate(['a', 'b', 'c'], start=1):
        ledger.register(item_id, make_ulid(100, 2 * index))
    service = ReaderService(ledger, ordering)
    reader = service.enroll(ReaderSpec('R', frozenset(['a', 'b', 'c']), order_key=lambda i: i))
    ordered = service.ordered_scope(reader)
    assert ordered.index('c') < ordered.index('a')


def test_confirmed_item_state_survives_reopen():
    facade, planner, _reader = build_facade()
    facade.ledger.confirm('a')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.millisecond(100, 100))])
    facade.publish(plan, 'r1')
    reopened = facade.reopen()
    assert reopened.ledger.get('a').state is ItemState.CONFIRMED
