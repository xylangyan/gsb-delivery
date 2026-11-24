"""Versioned collection handoff example.

Run with ``python docs/examples/versioned_scheduling.py``.
"""

from arq.scheduling import PlanVersion, SampleRange, ScheduleLedger


def main() -> None:
    ledger = ScheduleLedger()

    ledger.publish_plan(PlanVersion.build(1, [('A', 'site-a', 'batch-7', 0, 100)]))
    ledger.claim('1', 'A', worker_id='old-a')

    ledger.publish_plan(
        PlanVersion.build(
            2,
            [
                ('B', 'site-a', 'batch-7', 0, 40),
                ('C', 'site-a', 'batch-7', 40, 100),
            ],
        )
    )

    ledger.record_receipt(
        'warehouse-1',
        start=0,
        end=65,
        site='site-a',
        source_batch='batch-7',
        plan_version=1,
        assignment_id='A',
    )

    batch_b = ledger.claim('2', 'B', worker_id='b')
    batch_c = ledger.claim('2', 'C', worker_id='c')

    assert batch_b.work_ranges == ()
    assert batch_c.work_ranges == (SampleRange(65, 100),)

    receipt = ledger.record_receipt(
        'warehouse-c',
        accepted=[(40, 100)],
        site='site-a',
        source_batch='batch-7',
        plan_version=2,
        assignment_id='C',
        claim_id=batch_c.claim_id,
    )

    assert receipt.new_ranges == (SampleRange(65, 100),)
    assert receipt.duplicate_ranges == (SampleRange(40, 65),)
    assert ledger.settled_ranges('site-a', 'batch-7') == [SampleRange(0, 100)]
    assert ledger.needs_reschedule() == []

    print(ledger.view().as_dict())


if __name__ == '__main__':
    main()
