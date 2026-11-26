# -*- coding: utf-8 -*-
"""Runnable reproduction of the table-switch drill from the brief.

Old side: one ISBN-10 bundle covering bodies ``999307588``..``999307589`` with
paper check characters ``2`` and ``4``.  New side: the same ISBN-10 bundle
plus an ISBN-13-only bundle on body ``978999307589`` with check ``9``.  Both
registry tables register only ``978-99930`` with the single seven-digit
reading ``7589900``; the old table uses publisher length 0, the new table
length 2.  One work order withdraws the new-side ISBN-10 bundle and resets the
new table's rule to length 0.
"""

from .bundles import Bundle, BundleSet
from .ledger import Correction, Ledger
from .registrytable import RegistryTable
from .results import ABSENT, UNSEGMENTED, Segmented

REG_PREFIX = '978-99930'
READING = 7589900


def build():
    old_bundles = BundleSet([
        Bundle('old-isbn10', 'isbn10', '999307588', '999307589', '24'),
    ])
    new_bundles = BundleSet([
        Bundle('new-isbn10', 'isbn10', '999307588', '999307589', '24'),
        Bundle('new-isbn13', 'isbn13', '978999307589', '978999307589', '9'),
    ])
    old_table = RegistryTable('old')
    old_table.set_reading_rule(REG_PREFIX, READING, 0)
    new_table = RegistryTable('new')
    new_table.set_reading_rule(REG_PREFIX, READING, 2)
    return Ledger(old_bundles, new_bundles, old_table, new_table)


def correction():
    order = Correction('withdraw-isbn10-and-reset-rule')
    order.withdraw_bundle('new', 'new-isbn10')
    order.replace_group_rules(REG_PREFIX, ((READING, READING, 0),))
    return order


def _format_result(result):
    if result is UNSEGMENTED:
        return '有凭据但未分段'
    if result is ABSENT:
        return '缺席'
    publisher = result.publisher if result.publisher else '∅'
    return '%s | 组号 %s | 出版者 %s' % (
        result.reg_prefix, result.group, publisher)


def _print_grid(title, grid):
    print(title)
    for (old_result, new_result), cell in sorted(
            grid.items(), key=lambda item: item[1].min_isbn13):
        print('  (%s, %s)  %d 本  最小 %s'
              % (_format_result(old_result), _format_result(new_result),
                 cell.count, cell.min_isbn13))


def main():
    ledger = build()
    _print_grid('更正前账格：', ledger.grid())

    review = ledger.review(correction())
    print('\n迁移：')
    for (before_pair, after_pair), cell in sorted(
            review.migrations.items(), key=lambda item: item[1].min_isbn13):
        print('  (%s, %s) -> (%s, %s)  %d 本  最小 %s'
              % (_format_result(before_pair[0]),
                 _format_result(before_pair[1]),
                 _format_result(after_pair[0]),
                 _format_result(after_pair[1]),
                 cell.count, cell.min_isbn13))

    _print_grid('\n更正后账格：', review.after_grid)

    print('\n样本凭据 9789993075899：')
    for side, evidence in sorted(review.evidence('9789993075899').items()):
        bundles = ', '.join(
            '%s[%s 纸面 %s]' % (record.bundle_id, record.printed,
                               ''.join(record.paper_chars))
            for record in evidence.records) or '无支持束'
        print('  %-9s %s | %s' % (side, _format_result(evidence.result),
                                  bundles))

    print('\n样本凭据 9789993075882：')
    for side, evidence in sorted(review.evidence('9789993075882').items()):
        bundles = ', '.join(
            '%s[%s 纸面 %s]' % (record.bundle_id, record.printed,
                               ''.join(record.paper_chars))
            for record in evidence.records) or '无支持束'
        print('  %-9s %s | %s' % (side, _format_result(evidence.result),
                                  bundles))


if __name__ == '__main__':
    main()
