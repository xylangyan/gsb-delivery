#!/usr/bin/env python3
"""Print the three-way merge audit for the registration described in the task.

Run:  .venv/bin/python -m merge_audit.report
"""
from __future__ import annotations

# built-in

# project
from merge_audit.engine import (
    BOTH,
    BUCKETS,
    NEW_ONLY,
    OLD_ONLY,
    Entry,
    MergeRule,
    audit_band,
    audit_long_band,
    boundary_family_pair,
    family_parity_table,
    score_pair,
)


LABELS = {
    BOTH: 'both heads release',
    OLD_ONLY: 'old head only',
    NEW_ONLY: 'new head only',
}


def _print_pair(pair) -> None:
    print(
        f'      first: scan #{pair.left_index} vs #{pair.right_index} '
        f'{pair.left_token!r} / {pair.right_token!r} '
        f'[{pair.direction}] R={pair.restricted} U={pair.unrestricted}',
    )


def main() -> None:
    rule = MergeRule(threshold=1)

    print('== family parity (left head ...b vs right head ...bA) ==')
    print(' n  left        right       R  U  U(lower)  bucket@1')
    for length, left, right, restricted, unrestricted, lowered in family_parity_table(10):
        bucket = BOTH if unrestricted <= 1 else OLD_ONLY
        print(f'{length:>2}  {left:<10}  {right:<10}  {restricted}  {unrestricted}  '
              f'{lowered:>8}  {LABELS[bucket]}')

    print('\n== small band, stored scan order, coupled length step ==')
    entries = [
        Entry('bA', code='x1', group='g', lengths=(2, 3, 4)),
        Entry('AB', code='x1', group='g', lengths=(2, 3, 4)),
        Entry('ab', code='dup'),
        Entry('ab', code='dup'),
    ]
    for report in audit_band(entries, rule):
        print(f' step {report.step}, group lengths {report.lengths}')
        for bucket in BUCKETS:
            if bucket == 'neither':
                continue
            print(f'  {LABELS[bucket]:<20} count={report.counts[bucket]}')
            if report.first[bucket] is not None:
                _print_pair(report.first[bucket])

    print('\n== long band, legal length 2 .. 300000003, fragment probes only ==')
    verdict = audit_long_band(2, 300_000_003, rule)
    print(f" even lengths -> {LABELS[verdict.even_length_bucket]}")
    print(f" odd lengths  -> {LABELS[verdict.odd_length_bucket]}")
    for pair in verdict.boundary_pairs + verdict.spot_checks:
        print(
            f'  {pair.direction}: {pair.left_token!r} / {pair.right_token!r} '
            f'R={pair.restricted} U={pair.unrestricted} -> {LABELS[pair.bucket]}',
        )

    print('\n== threshold-2 preview (new-only bucket reopens) ==')
    rule2 = MergeRule(threshold=2)
    pair = score_pair('ab', 'bca', rule2)
    print(f"  'ab' / 'bca' R={pair.restricted} U={pair.unrestricted} "
          f'-> {LABELS[pair.bucket]}')


if __name__ == '__main__':
    main()
