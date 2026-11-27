"""
    test_ledger_planner
    ~~~~~~~~~~~~~~~~~~~

    Tests for reticket planning: minimum renames, reader-count tie-break, audit blocks.
"""
import pytest

from ulid.ledger import (
    BackfillRequest,
    ControlledClock,
    Ledger,
    LedgerService,
    NumberingService,
    ReaderService,
    ReaderSpec,
    ReticketPlanner,
    Window,
)
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def build_service(specs, registered=('a', 'b', 'c'), suffixes=(2, 4, 6)):
    ledger = Ledger()
    for item_id, suffix in zip(registered, suffixes):
        ledger.register(item_id, make_ulid(100, suffix))
    service = ReaderService(ledger)
    readers = [service.enroll(spec) for spec in specs]
    numbering = NumberingService(ControlledClock(1000))
    planner = ReticketPlanner(ledger, service, numbering)
    return ledger, service, numbering, planner, readers


def reticket_spec(reader_id, scope, order_map):
    return ReaderSpec(reader_id, frozenset(scope), order_key=lambda i: order_map[i], reticket_allowed=True)


def strict_spec(reader_id, scope, order_map):
    return ReaderSpec(reader_id, frozenset(scope), order_key=lambda i: order_map[i], reticket_allowed=False)


def test_backfill_in_open_range_needs_no_rename_and_one_reticket():
    order_map = {'a': 1, 'b': 2, 'x': 3, 'c': 4}
    ledger, service, numbering, planner, readers = build_service(
        [reticket_spec('A', ['a', 'b', 'x', 'c'], order_map)])
    page = service.next_page(readers[0], 2)
    service.sign(readers[0], page)
    service.deliver(readers[0], 'a')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.millisecond(100, 100))])
    assert plan.score == (0, 0)
    assert not plan.renames
    assert 'x' in plan.new_items
    ticket = plan.retickets['A']
    assert ticket.owed_segments == (('b', 'b'), ('x', 'x'), ('c', 'c'))
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    values = [ledger.current_ulid(i).int for i in ['a', 'b', 'x', 'c']]
    assert values == sorted(values)


def test_narrow_window_forces_minimum_renames():
    order_map = {'a': 1, 'b': 2, 'x': 3, 'y': 4, 'c': 5}
    ledger, service, numbering, planner, _readers = build_service(
        [reticket_spec('A', ['a', 'b', 'x', 'y', 'c'], order_map)],
        suffixes=(2, 4, 6))
    window = Window.suffix(100, 100, 1, 8)
    plan = planner.plan(1, 'r1', [BackfillRequest('x', window), BackfillRequest('y', window)])
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    ordered = sorted(
        (ledger.current_ulid(item_id).randomness().int, item_id)
        for item_id in ['a', 'b', 'x', 'y', 'c'])
    assert [item_id for _suffix, item_id in ordered] == ['a', 'b', 'x', 'y', 'c']
    assert len(set(suffix for suffix, _ in ordered)) == 5


def test_tie_break_prefers_fewer_reticketed_readers():
    order_a = {'a': 1, 'b': 2, 'x': 3, 'c': 4}
    order_b = {'a': 1, 'b': 2, 'c': 3}
    specs = [
        reticket_spec('A', ['a', 'b', 'x', 'c'], order_a),
        reticket_spec('B', ['a', 'b', 'c'], order_b),
    ]
    ledger, service, numbering, planner, readers = build_service(specs)
    for reader in readers:
        page = service.next_page(reader, 2)
        service.sign(reader, page)
    window = Window.suffix(100, 100, 1, 4)
    plan = planner.plan(1, 'r1', [BackfillRequest('x', window)])
    assert plan.score[0] >= 1
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    values = [ledger.current_ulid(i).int for i in ['a', 'b', 'x', 'c']]
    assert values == sorted(values)


def test_confirmed_item_is_anchor_and_never_renamed():
    order_map = {'a': 1, 'b': 2, 'x': 3, 'c': 4}
    ledger, service, numbering, planner, readers = build_service(
        [reticket_spec('A', ['a', 'b', 'x', 'c'], order_map)])
    ledger.confirm('b')
    frozen = ledger.current_ulid('b')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 1, 30))])
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    assert ledger.current_ulid('b') == frozen
    assert 'b' not in plan.renames


def test_non_reticket_reader_blocked_behind_bound_gets_audit_and_no_force_renumber():
    order_map = {'a': 1, 'b': 2, 'x': 3, 'c': 4}
    ledger, service, numbering, planner, readers = build_service(
        [strict_spec('R', ['a', 'b', 'x', 'c'], order_map)])
    ledger.confirm('a')
    ledger.confirm('b')
    page = service.next_page(readers[0], 2)
    service.sign(readers[0], page)
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 3, 3))])
    publication = LedgerService(ledger, numbering, service).publish(plan, 'r1')
    assert 'x' not in publication.catalog
    assert plan.audits
    audit = plan.audits[0]
    assert audit.reader_id == 'R'
    assert audit.pending_items == ('x',)
    assert audit.bound == readers[0].bound


def test_delivered_renamed_item_emits_alias_and_is_not_redelivered():
    # x 被窗口钉在后缀 2, 又必须排在 a(后缀2) 之后, 只能把已送达的 a 改到后缀 1。
    order_map = {'a': 1, 'x': 2, 'b': 3, 'c': 4}
    ledger, service, numbering, planner, readers = build_service(
        [reticket_spec('A', ['a', 'x', 'b', 'c'], order_map)])
    service.deliver(readers[0], 'a')
    original_a = ledger.current_ulid('a')
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 2, 2))])
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    assert readers[0].delivered == frozenset({'a'})
    assert 'a' in plan.renames
    ticket = plan.retickets['A']
    assert any(item_id == 'a' and old == original_a for item_id, old, _new in ticket.aliases)
    ordered = sorted(
        (ledger.current_ulid(item_id).randomness().int, item_id)
        for item_id in ['a', 'x', 'b', 'c'])
    assert [item_id for _suffix, item_id in ordered] == ['a', 'x', 'b', 'c']


def test_plan_is_deterministic_across_runs():
    order_map = {'a': 1, 'b': 2, 'x': 3, 'c': 4}
    results = []
    for _ in range(3):
        ledger, service, numbering, planner, _readers = build_service(
            [reticket_spec('A', ['a', 'b', 'x', 'c'], order_map)])
        plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 1, 12))])
        results.append((plan.score, sorted(plan.renames), sorted(plan.new_items)))
    assert results[0] == results[1] == results[2]


def test_shared_physical_item_is_a_single_catalog_entry():
    ledger = Ledger()
    for index, item_id in enumerate(['a', 'b', 'c'], start=1):
        ledger.register(item_id, make_ulid(100, 2 * index))
    service = ReaderService(ledger)
    reader_a = service.enroll(ReaderSpec('A', frozenset(['a', 'b']), order_key=lambda i: i, reticket_allowed=True))
    reader_b = service.enroll(ReaderSpec('B', frozenset(['a', 'c']), order_key=lambda i: i, reticket_allowed=True))
    service.deliver(reader_a, 'a')
    service.deliver(reader_b, 'a')
    assert ledger.ids().count('a') == 1
    assert service.remaining(reader_a) == ['b']
    assert service.remaining(reader_b) == ['c']
    assert ledger.resolve(make_ulid(100, 2)) == 'a'


def test_renamed_bound_item_remaps_new_bound_by_identity():
    order_map = {'a': 1, 'x': 2, 'b': 3}
    ledger, service, numbering, planner, readers = build_service(
        [reticket_spec('A', ['a', 'x', 'b'], order_map)],
        registered=('a', 'b'), suffixes=(2, 4))
    page = service.next_page(readers[0], 2)
    service.sign(readers[0], page)
    old_bound = readers[0].bound
    plan = planner.plan(1, 'r1', [BackfillRequest('x', Window.suffix(100, 100, 4, 4))])
    LedgerService(ledger, numbering, service).publish(plan, 'r1')
    ticket = plan.retickets['A']
    assert 'b' in plan.renames
    assert ticket.new_bound != old_bound
    assert ticket.new_bound == ledger.current_ulid('b')
