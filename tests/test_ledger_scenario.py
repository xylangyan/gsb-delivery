"""
    test_ledger_scenario
    ~~~~~~~~~~~~~~~~~~~~

    End-to-end small sample required by the acceptance narrative:
    controlled clock with full number rollback, two old readers paging in
    interleaved order, a late release arriving in the middle, then a reopen
    where each reader finishes its own resume independently.
"""
from ulid.ledger import (
    BackfillRequest,
    ControlledClock,
    ItemState,
    Ledger,
    LedgerService,
    NumberingService,
    ReaderService,
    ReaderSpec,
    ReservationState,
    ReticketPlanner,
    Window,
)
from ulid.ulid import ULID


def make_ulid(millis, suffix):
    return ULID(((millis << 80) | suffix).to_bytes(16, byteorder='big'))


def test_full_rollback_interleaved_resume_and_reopen():
    clock = ControlledClock(1000)
    ledger = Ledger()
    initial = {'p1': 10, 'p2': 20, 'p3': 30, 'p4': 40, 'p5': 45}
    for item_id, suffix in initial.items():
        ledger.register(item_id, make_ulid(100, suffix), library_code='LIB-{}'.format(item_id))

    service = ReaderService(ledger)
    order_a = {'p1': 1, 'p2': 2, 'q1': 3, 'p3': 4, 'p4': 5, 'q2': 6, 'p5': 7}
    order_b = {'p1': 1, 'p2': 2, 'p3': 3, 'p4': 4}
    reader_a = service.enroll(
        ReaderSpec('jia', frozenset(order_a), order_key=lambda i: order_a[i], reticket_allowed=True))
    reader_b = service.enroll(
        ReaderSpec('yi', frozenset(order_b), order_key=lambda i: order_b[i], reticket_allowed=False))

    # 两个旧客户端交错翻页、签页。
    page_a1 = service.next_page(reader_a, 2)
    assert page_a1.item_ids == ('p1', 'p2')
    service.sign(reader_a, page_a1)

    page_b1 = service.next_page(reader_b, 2)
    assert page_b1.item_ids == ('p1', 'p2')
    service.sign(reader_b, page_b1)
    assert service.deliver(reader_b, 'p1') is True

    page_a2 = service.next_page(reader_a, 2)
    assert page_a2.item_ids == ('p3', 'p4')
    service.sign(reader_a, page_a2)
    service.deliver(reader_a, 'p1')
    assert service.deliver(reader_a, 'p3') is True
    service.deliver(reader_a, 'p5')

    page_b2 = service.next_page(reader_b, 2)
    assert page_b2.item_ids == ('p3', 'p4')
    service.sign(reader_b, page_b2)

    # 旧钟回拨: 之后新领的号时间戳可以早于此前签发的号。
    clock.rollback(500)
    assert clock.millis == 500
    numbering = NumberingService(clock)
    rolled_token, rolled_value = numbering.hold('rollback-era')
    assert rolled_value.timestamp().int == 500
    numbering.release(rolled_token)

    # 中间迟到一个释号: 新一轮接管位置后, 旧轮晚到的释号不能释放当前占用。
    late_token, _late_value = numbering.hold_position('pre-round', 1 << 90, 'late')
    new_token, _new_value = numbering.hold_position('next-round', 1 << 90, 'late')
    assert numbering.reservation(late_token).state is ReservationState.SUPERSEDED
    assert numbering.release(late_token) is False
    assert numbering.reservation(new_token).state is ReservationState.HELD
    numbering.release(new_token)

    planner = ReticketPlanner(ledger, service, numbering)
    backfills = [
        BackfillRequest('q1', Window.suffix(100, 100, 21, 29), 'LIB-q1'),
        BackfillRequest('q2', Window.suffix(100, 100, 45, 49), 'LIB-q2'),
    ]
    plan = planner.plan(1, 'plan-round', backfills)

    # p5 已送达但未确认, 与 q2 顶撞时允许换号; 不能改签的乙范围内 p1..p4 全部保持原号。
    assert 'p5' in plan.renames
    assert not {'p1', 'p2', 'p3', 'p4'}.intersection(plan.renames)
    assert plan.score == (1, 1)

    facade = LedgerService(ledger, numbering, service)
    publication = facade.publish(plan, 'plan-round')

    # 对照目录与原进度映射必须一起发布。
    assert publication.generation == 1
    assert set(publication.catalog) == {'p1', 'p2', 'p3', 'p4', 'p5', 'q1', 'q2'}
    assert ('jia', 'bound') in publication.progress_map
    ticket_a = plan.retickets['jia']
    alias_items = {item_id for item_id, _old, _new in ticket_a.aliases}
    assert 'p5' in alias_items

    # 已送达实物不因换号再交一回。
    assert reader_a.delivered == frozenset({'p1', 'p3', 'p5'})
    assert ledger.get('p5').state is ItemState.DELIVERED
    assert ledger.resolve(make_ulid(100, 45)) == 'p5'

    # 重开后只承认已成功签发的一代, 临时保留全部消失。
    reopened = facade.reopen()
    assert reopened.numbering.issued_generations() == (1,)
    assert not reopened.numbering.has_round('plan-round')

    # 甲先完成自己的旧续读。
    resumed_a = reopened.continue_resume(reopened.reader_service.get('jia'), 2)
    flat_a = [item_id for page in resumed_a for item_id in page.item_ids]
    assert flat_a == ['p2', 'q1', 'p4', 'q2']
    assert len(flat_a) == len(set(flat_a))
    for page in resumed_a:
        for item_id, value in page.entries:
            assert reopened.ledger.current_ulid(item_id) == value

    # 乙随后完成自己的旧续读, 继续交相同范围的原格式号码, 已签原号始终有效。
    resumed_b = reopened.continue_resume(reopened.reader_service.get('yi'), 2)
    flat_b = [item_id for page in resumed_b for item_id in page.item_ids]
    assert flat_b == ['p2', 'p3', 'p4']
    for item_id in flat_b:
        assert reopened.ledger.current_ulid(item_id) == make_ulid(100, initial[item_id])

    # 新读者按原库编码和已定序查看。
    new_reader_service = ReaderService(reopened.ledger)
    view = new_reader_service.library_view(reopened.ledger.ids())
    assert view[0] == 'p1'
    assert 'q1' in view and 'q2' in view
    assert view.index('q1') < view.index('q2')
