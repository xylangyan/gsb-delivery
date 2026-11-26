# -*- coding: utf-8 -*-
import random
import unittest

from isbnlib._core import check_digit13

from isbnrecon import (
    ABSENT,
    UNSEGMENTED,
    Bundle,
    BundleSet,
    Correction,
    Ledger,
    RegistryTable,
    Segmented,
)
from isbnrecon.counting import PREFIX_978
from tests._oracle import brute_grid, brute_migrations


def make_table(name, group_key, rules):
    table = RegistryTable(name)
    table.set_group_rules(group_key, rules)
    return table


class LedgerBruteForceTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(20261127)

    def _random_bundles(self, prefix, count, width_kind, lo, hi):
        kind, width = width_kind
        bundles = []
        for idx in range(count):
            span = self.rng.randint(1, max(1, (hi - lo) // 3))
            start = self.rng.randint(lo, hi - span)
            end = start + span
            chars = ''.join(self.rng.sample(
                '0123456789X' if kind == 'isbn10' else '0123456789',
                self.rng.randint(1, 3)))
            bundles.append(Bundle(
                '%s-%d' % (prefix, idx), kind,
                str(start).zfill(width), str(end).zfill(width), chars))
        return BundleSet(bundles)

    def _assert_region(self, ledger, views, region):
        got = ledger.grid(views, region)
        expected = brute_grid(views, region)
        self.assertEqual(set(got), set(expected))
        for label, (count, min_key) in expected.items():
            cell = got[label]
            self.assertEqual(cell.count, count, label)
            self.assertEqual(cell.min_isbn13, min_key, label)

    def test_overlapping_bundles_single_group(self):
        lo13, hi13 = 978999300000, 978999300400
        old_bundles = self._random_bundles(
            'o', 4, ('isbn13', 12), lo13, hi13)
        new_bundles = self._random_bundles(
            'n', 5, ('isbn13', 12), lo13, hi13)
        rules = ((0, 2000000, 2), (2000001, 5000000, 0),
                 (5000001, 9999999, 3))
        old_table = make_table('old', '978-99930', rules)
        new_table = make_table('new', '978-99930',
                               ((0, 3000000, 1), (3000001, 9999999, 4)))
        ledger = Ledger(old_bundles, new_bundles, old_table, new_table)
        self._assert_region(ledger, ledger.before, (lo13, hi13))

    def test_mixed_isbn10_and_isbn13_bundles(self):
        lo10, hi10 = 999300000, 999300250
        old_bundles = self._random_bundles(
            'o', 4, ('isbn10', 9), lo10, hi10)
        new13 = self._random_bundles(
            'n13', 3, ('isbn13', 12),
            PREFIX_978 + lo10, PREFIX_978 + hi10)
        new10 = self._random_bundles(
            'n10', 3, ('isbn10', 9), lo10, hi10)
        for bundle in new10:
            new13.add(bundle)
        rules = ((0, 9999999, 2),)
        ledger = Ledger(old_bundles, new13,
                        make_table('old', '978-99930', rules),
                        make_table('new', '978-99930', ((0, 9999999, 0),)))
        region = (PREFIX_978 + lo10, PREFIX_978 + hi10)
        self._assert_region(ledger, ledger.before, region)

    def test_979_prefix_stays_independent(self):
        lo13, hi13 = 979000000000, 979000000200
        bundles = self._random_bundles(
            'p', 4, ('isbn13', 12), lo13, hi13)
        old_table = make_table('old', '979-0', ((0, 9999999, 1),))
        new_table = RegistryTable('new')
        ledger = Ledger(bundles, BundleSet(), old_table, new_table)
        got = ledger.grid(ledger.before, (lo13, hi13))
        expected = brute_grid(ledger.before, (lo13, hi13))
        self.assertEqual(set(got), set(expected))
        for label, (count, min_key) in expected.items():
            self.assertEqual(got[label].count, count)
            self.assertEqual(got[label].min_isbn13, min_key)
            self.assertTrue(label[0].reg_prefix.startswith('979-'))

    def test_correction_review_matches_brute_force(self):
        lo13, hi13 = 978999307000, 978999307900
        old_bundles = self._random_bundles(
            'o', 3, ('isbn13', 12), lo13, hi13)
        new_bundles = self._random_bundles(
            'n', 4, ('isbn13', 12), lo13, hi13)
        old_table = make_table('old', '978-99930', ((0, 9999999, 2),))
        new_table = make_table('new', '978-99930', ((0, 9999999, 3),))
        ledger = Ledger(old_bundles, new_bundles, old_table, new_table)

        withdrawn = list(new_bundles.ids())[0]
        replacement = Bundle(
            'repl', 'isbn13', str(lo13 + 10), str(lo13 + 80), '5')
        order = Correction('mixed').withdraw_bundle('new', withdrawn)
        order.replace_bundle('new', replacement)
        order.replace_group_rules('978-99930', ((0, 9999999, 0),))

        review = ledger.review(order)
        after_views = ledger.apply(order)
        region = ledger.affected_region(order, after_views)
        expected_migrations = brute_migrations(
            ledger.before, after_views, region)
        self.assertEqual(set(review.migrations), set(expected_migrations))
        for pair, (count, min_key) in expected_migrations.items():
            cell = review.migrations[pair]
            self.assertEqual(cell.count, count, pair)
            self.assertEqual(cell.min_isbn13, min_key, pair)

    def test_drill_scenario_cell_and_migration(self):
        from isbnrecon.drill import build, correction
        ledger = build()
        grid = ledger.grid()
        unseg = (UNSEGMENTED, UNSEGMENTED)
        self.assertEqual(grid[unseg].count, 1)
        self.assertEqual(grid[unseg].min_isbn13, '9789993075882')
        before_cell = (
            Segmented('978-99930', '99930', ''),
            Segmented('978-99930', '99930', '75'))
        self.assertEqual(grid[before_cell].count, 1)
        self.assertEqual(grid[before_cell].min_isbn13, '9789993075899')

        review = ledger.review(correction())
        after_pair = (
            Segmented('978-99930', '99930', ''),
            Segmented('978-99930', '99930', ''))
        self.assertEqual(review.migrations[(before_cell, after_pair)].count, 1)
        moved_absent = (unseg, (UNSEGMENTED, ABSENT))
        self.assertEqual(review.migrations[moved_absent].count, 1)
        self.assertNotIn(
            (Segmented('978-99930', '99930', '75'),
             Segmented('978-99930', '99930', '75')),
            review.after_grid)

    def test_evidence_is_traceable_and_deduped(self):
        from isbnrecon.drill import build, correction
        review = build().review(correction())
        evidence = review.evidence('9789993075899')
        self.assertEqual(
            {record.bundle_id for record in evidence['after_new'].records},
            {'new-isbn13'})
        before_new_ids = {
            record.bundle_id for record in evidence['before_new'].records}
        self.assertEqual(before_new_ids, {'new-isbn10', 'new-isbn13'})
        self.assertEqual(evidence['after_new'].result.publisher, '')

    def test_absent_cell_semantics(self):
        # A book old=A new=B must not be counted as old-only.
        lo = 978999300000
        old_bundles = BundleSet([Bundle('o', 'isbn13', str(lo), str(lo + 9),
                                        '0123456789')])
        new_bundles = BundleSet([Bundle('n', 'isbn13', str(lo), str(lo + 4),
                                        '0123456789')])
        table = make_table('t', '978-99930', ((0, 9999999, 1),))
        ledger = Ledger(old_bundles, new_bundles, table, table.copy())
        grid = ledger.grid(region=(lo, lo + 9))
        old_only = grid.get((Segmented('978-99930', '99930', '0'), ABSENT))
        self.assertIsNotNone(old_only)
        # Bodies 5..9 have no new-side support: five titles, each counted once.
        self.assertEqual(old_only.count, 5)


if __name__ == '__main__':
    unittest.main()
