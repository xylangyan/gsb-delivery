from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from arq.cron import next_cron
from arq.scheduling import (
    ClaimState,
    CollectionScheduler,
    PlanVersion,
    SampleRange,
    ScheduleLedger,
    StalePlanVersion,
    collection_job_id,
    merge_ranges,
    progress_key,
    trigger_identity,
)


def publish_handoff_plan(ledger: ScheduleLedger) -> None:
    ledger.publish_plan(
        PlanVersion.build(
            1,
            [
                {'id': 'A', 'site': 'site-a', 'source_batch': 'batch-7', 'start_sample': 0, 'end_sample': 100},
            ],
        )
    )
    ledger.publish_plan(
        PlanVersion.build(
            2,
            [
                {'id': 'B', 'site': 'site-a', 'source_batch': 'batch-7', 'start_sample': 0, 'end_sample': 40},
                {'id': 'C', 'site': 'site-a', 'source_batch': 'batch-7', 'start_sample': 40, 'end_sample': 100},
            ],
        )
    )


def test_handoff_after_receipt_removes_finished_prefix_and_trims_c() -> None:
    ledger = CollectionScheduler()
    ledger.publish_plan(PlanVersion.build(1, [('A', 'site-a', 'batch-7', 0, 100)]))
    first_a = ledger.claim('1', 'A', worker_id='old-a')
    assert first_a.work_ranges == (SampleRange(0, 100),)

    ledger.publish_plan(PlanVersion.build(2, [('B', 'site-a', 'batch-7', 0, 40), ('C', 'site-a', 'batch-7', 40, 100)]))

    with pytest.raises(StalePlanVersion):
        ledger.claim('1', 'A', worker_id='another-old-a')

    receipt = ledger.record_receipt(
        'warehouse-1',
        start=0,
        end=65,
        site='site-a',
        source_batch='batch-7',
        plan_version=1,
        assignment_id='A',
    )
    assert receipt.new_ranges == (SampleRange(0, 65),)

    b = ledger.claim('2', 'B', worker_id='b')
    c = ledger.claim('2', 'C', worker_id='c')
    assert b.work_ranges == ()
    assert c.work_ranges == (SampleRange(65, 100),)
    assert ledger.needs_reschedule() == []

    c_receipt = ledger.record_receipt(
        'warehouse-c',
        accepted=[(40, 100)],
        site='site-a',
        source_batch='batch-7',
        plan_version=2,
        assignment_id='C',
        claim_id=c.claim_id,
    )
    assert c_receipt.new_ranges == (SampleRange(65, 100),)
    assert c_receipt.duplicate_ranges == (SampleRange(40, 65),)
    assert ledger.settled_ranges('site-a', 'batch-7') == [SampleRange(0, 100)]
    assert ledger.needs_reschedule() == []


def test_existing_c_claim_still_accepts_tail_after_late_a_receipt() -> None:
    ledger = ScheduleLedger()
    ledger.publish_plan(PlanVersion.build('old', [('A', 'site-a', 'batch-7', 0, 100)]))
    ledger.claim('old', 'A', worker_id='a')
    ledger.publish_plan(
        PlanVersion.build('new', [('B', 'site-a', 'batch-7', 0, 40), ('C', 'site-a', 'batch-7', 40, 100)])
    )

    c = ledger.claim('new', 'C', worker_id='c')
    assert c.work_ranges == (SampleRange(40, 100),)

    ledger.record_receipt(
        'a-receipt', start=0, end=65, site='site-a', source_batch='batch-7', plan_version='old'
    )
    c_result = ledger.record_receipt(
        'c-receipt',
        accepted=[(40, 100)],
        site='site-a',
        source_batch='batch-7',
        plan_version='new',
        claim_id=c.claim_id,
    )

    assert c_result.new_ranges == (SampleRange(65, 100),)
    assert c_result.duplicate_ranges == (SampleRange(40, 65),)
    assert ledger.coverage() == {('site-a', 'batch-7'): [SampleRange(0, 100)]}


def test_repeated_receipt_is_idempotent_even_when_report_is_redelivered() -> None:
    ledger = ScheduleLedger()
    first = ledger.record_receipt('r1', accepted=[(0, 65)], site='s', source_batch='b')
    second = ledger.record_receipt('r1', accepted=[(0, 65)], site='s', source_batch='b')

    assert first.new_ranges == (SampleRange(0, 65),)
    assert second.new_ranges == ()
    assert second.duplicate_delivery is True
    assert ledger.settled_ranges('s', 'b') == [SampleRange(0, 65)]


def test_successful_job_report_does_not_settle_samples() -> None:
    ledger = ScheduleLedger()
    ledger.record_job_report('job-1', success=True, site='s', source_batch='b', start=0, end=100)

    assert ledger.settled_ranges('s', 'b') == []
    assert ledger.delivered_segments()
    ledger.record_receipt('receipt-1', accepted=[(0, 65)], site='s', source_batch='b')
    assert ledger.settled_ranges('s', 'b') == [SampleRange(0, 65)]


def test_publish_marks_old_claim_superseded_but_late_receipt_still_settles() -> None:
    ledger = ScheduleLedger()
    ledger.publish_plan('v1', [('A', 's', 'b', 0, 100)])
    claim = ledger.claim('v1', 'A').claim
    ledger.publish_plan('v2', [('C', 's', 'b', 65, 100)])

    assert ledger.claims[claim.claim_id].state == ClaimState.superseded
    held = ledger.held_segments()
    assert [segment.range for segment in held] == [SampleRange(0, 100)]
    assert ledger.active_held_segments() == []
    assert ledger.stale_held_segments() == [
        __import__('arq.scheduling', fromlist=['Segment']).Segment('s', 'b', SampleRange(0, 100))
    ]

    result = ledger.record_receipt('late-a', accepted=[(0, 65)], site='s', source_batch='b', plan_version='v1')
    assert result.new_ranges == (SampleRange(0, 65),)


def test_view_partitions_settled_held_and_reschedule() -> None:
    ledger = ScheduleLedger()
    publish_handoff_plan(ledger)
    ledger.record_receipt('r', accepted=[(0, 40)], site='site-a', source_batch='batch-7')
    ledger.claim('2', 'C', worker_id='c', lease_expires_at=datetime(2099, 1, 1))

    view = ledger.view()
    assert [segment.range for segment in view.settled] == [SampleRange(0, 40)]
    assert [segment.range for segment in view.held] == [SampleRange(40, 100)]
    assert view.needs_reschedule == []

    ledger.expire_due_claims(datetime(2100, 1, 1))
    view_after_expiry = ledger.view(now=datetime(2100, 1, 1))
    assert view_after_expiry.held == []
    assert [segment.range for segment in view_after_expiry.needs_reschedule] == [SampleRange(40, 100)]


def test_dst_wall_time_has_two_triggers_but_progress_identity_uses_batch_and_range() -> None:
    berlin = ZoneInfo('Europe/Berlin')
    first = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0)
    second = first.replace(fold=1)

    first_trigger = trigger_identity(first)
    second_trigger = trigger_identity(second)
    assert first_trigger != second_trigger
    assert first_trigger['utc_offset_s'] != second_trigger['utc_offset_s']

    repeated = next_cron(first, hour=2, minute=30, second=0, microsecond=0)
    assert repeated.fold == 1
    assert repeated.utcoffset() != first.utcoffset()
    assert next_cron(repeated, hour=2, minute=30, second=0, microsecond=0).day == 26

    same_progress_1 = progress_key('batch-7', 40, 100, site='site-a')
    same_progress_2 = progress_key('batch-7', 40, 100, site='site-a')
    other_segment = progress_key('batch-7', 65, 100, site='site-a')
    other_batch = progress_key('batch-8', 40, 100, site='site-a')

    assert same_progress_1 == same_progress_2
    assert same_progress_1 != other_segment
    assert same_progress_1 != other_batch
    assert collection_job_id('batch-7', 40, 100, site='site-a').count('batch-7') >= 1


def test_merge_and_subtract_half_open_ranges() -> None:
    assert merge_ranges([SampleRange(40, 65), SampleRange(0, 40), SampleRange(50, 100)]) == [SampleRange(0, 100)]
    assert SampleRange(0, 100).subtract([(0, 65), (40, 70)]) == [SampleRange(70, 100)]
    assert SampleRange(0, 40).subtract([(0, 65)]) == []


def test_ledger_round_trip_preserves_settlement_and_claims() -> None:
    ledger = ScheduleLedger()
    publish_handoff_plan(ledger)
    claim = ledger.claim('2', 'C', worker_id='c').claim
    ledger.record_receipt('r', accepted=[(0, 65)], site='site-a', source_batch='batch-7')

    restored = ScheduleLedger.from_dict(ledger.to_dict())
    assert restored.current_version == '2'
    assert restored.claims[claim.claim_id].assignment_id == 'C'
    assert restored.settled_ranges('site-a', 'batch-7') == [SampleRange(0, 65)]
    assert restored.claim('2', 'C', worker_id='c').work_ranges == (SampleRange(65, 100),)
