# -*- coding: utf-8 -*-
import random
import unittest

from isbnrecon import Bundle, BundleSet, Correction, Ledger, RegistryTable
from isbnrecon.counting import PREFIX_978
from tests._oracle import brute_grid, brute_migrations


def random_table(rng, name, group_keys):
    table = RegistryTable(name)
    for group_key in group_keys:
        rule_count = rng.randint(1, 4)
        cuts = sorted(rng.sample(range(0, 10_000_000, 250_000),
                                 rule_count + 1))
        rules = []
        for idx in range(rule_count):
            length = rng.randint(0, 7)
            rules.append((cuts[idx], cuts[idx + 1] - 1, length))
        rules[-1] = (rules[-1][0], 9_999_999, rules[-1][2])
        table.set_group_rules(group_key, rules)
    return table


def random_bundles(rng, prefix, kind, lo, hi, count):
    width = 12 if kind == 'isbn13' else 9
    bundles = BundleSet()
    for idx in range(count):
        span = rng.randint(1, max(1, (hi - lo) // 2))
        start = rng.randint(lo, hi - span)
        alphabet = '0123456789X' if kind == 'isbn10' else '0123456789'
        chars = ''.join(rng.sample(alphabet, rng.randint(1, 4)))
        bundles.add(Bundle('%s-%d' % (prefix, idx), kind,
                           str(start).zfill(width), str(start + span).zfill(width),
                           chars))
    return bundles


class FuzzTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(99173)

    def _assert_grid(self, ledger, views, region):
        got = ledger.grid(views, region)
        expected = brute_grid(views, region)
        self.assertEqual(set(got), set(expected))
        for label, (count, min_key) in expected.items():
            self.assertEqual(got[label].count, count, label)
            self.assertEqual(got[label].min_isbn13, min_key, label)

    def test_grid_fuzz_across_group_shapes(self):
        for seed in range(24):
            rng = random.Random(seed * 131 + 7)
            lo13 = 978999300000
            hi13 = lo13 + 500
            old_bundles = random_bundles(rng, 'o', 'isbn13', lo13, hi13, 4)
            new_bundles = random_bundles(rng, 'n', 'isbn13', lo13, hi13, 4)
            old_table = random_table(rng, 'old', ('978-99930',))
            new_groups = ('978-99930',) if seed % 2 else ('978-999',)
            new_table = random_table(rng, 'new', new_groups)
            ledger = Ledger(old_bundles, new_bundles, old_table, new_table)
            self._assert_grid(ledger, ledger.before, (lo13, hi13))

    def test_mixed_kind_and_979_fuzz(self):
        for seed in range(12):
            rng = random.Random(seed * 977 + 3)
            lo10, hi10 = 999300000, 999300400
            old10 = random_bundles(rng, 'o10', 'isbn10', lo10, hi10, 3)
            new10 = random_bundles(rng, 'n10', 'isbn10', lo10, hi10, 2)
            new13 = random_bundles(
                rng, 'n13', 'isbn13',
                PREFIX_978 + lo10, PREFIX_978 + hi10, 3)
            for bundle in new13:
                new10.add(bundle)
            old_table = random_table(rng, 'old', ('978-99930',))
            new_table = random_table(rng, 'new', ('978-99930',))
            ledger = Ledger(old10, new10, old_table, new_table)
            region = (PREFIX_978 + lo10, PREFIX_978 + hi10)
            self._assert_grid(ledger, ledger.before, region)

    def test_correction_fuzz(self):
        for seed in range(18):
            rng = random.Random(seed * 613 + 11)
            lo13, hi13 = 978999306000, 978999308000
            old_bundles = random_bundles(rng, 'o', 'isbn13', lo13, hi13, 4)
            new_bundles = random_bundles(rng, 'n', 'isbn13', lo13, hi13, 5)
            old_table = random_table(rng, 'old', ('978-99930',))
            new_table = random_table(rng, 'new', ('978-99930',))
            ledger = Ledger(old_bundles, new_bundles, old_table, new_table)

            order = Correction('fuzz-%d' % seed)
            new_ids = list(new_bundles.ids())
            for bundle_id in rng.sample(new_ids, rng.randint(1, 2)):
                order.withdraw_bundle('new', bundle_id)
            if rng.random() < 0.7:
                start = rng.randint(lo13, hi13 - 20)
                order.replace_bundle('new', Bundle(
                    'repl', 'isbn13', str(start), str(start + 19),
                    ''.join(rng.sample('0123456789', 2))))
            if rng.random() < 0.8:
                order.replace_group_rules(
                    '978-99930',
                    random_table(rng, 'swap', ('978-99930',))
                    ._ranges['978-99930'])

            after_views = ledger.apply(order)
            region = ledger.affected_region(order, after_views)
            got = ledger.review(order).migrations
            expected = brute_migrations(ledger.before, after_views, region)
            self.assertEqual(set(got), set(expected), seed)
            for pair, (count, min_key) in expected.items():
                self.assertEqual(got[pair].count, count, (seed, pair))
                self.assertEqual(got[pair].min_isbn13, min_key, pair)

    def test_large_unrelated_bundle_locality(self):
        huge_lo, huge_hi = 978000000000, 978000000000 + 200_000_000 - 1
        small_lo, small_hi = 978999307588, 978999307589
        old_bundles = BundleSet([
            Bundle('huge', 'isbn13', str(huge_lo), str(huge_hi),
                   '0123456789'),
            Bundle('small-old', 'isbn10', '999307588', '999307589', '24'),
        ])
        new_bundles = BundleSet([
            Bundle('huge', 'isbn13', str(huge_lo), str(huge_hi),
                   '0123456789'),
            Bundle('small-new', 'isbn10', '999307588', '999307589', '24'),
        ])
        old_table = RegistryTable('old')
        old_table.set_reading_rule('978-99930', 7589900, 0)
        new_table = RegistryTable('new')
        new_table.set_reading_rule('978-99930', 7589900, 2)
        ledger = Ledger(old_bundles, new_bundles, old_table, new_table)
        order = Correction('local').withdraw_bundle('new', 'small-new')

        import time
        start = time.perf_counter()
        review = ledger.review(order)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0)

        # The huge bundle still supports its own range untouched.
        after_new = ledger.apply(order)[1]
        self.assertTrue(
            after_new.supports('isbn13', huge_lo + 123_456_789))
        # Migration cells only mention the small-bundle region.
        for (_before, _after), cell in review.migrations.items():
            self.assertTrue(cell.min_isbn13.startswith('97899930758'))


if __name__ == '__main__':
    unittest.main()
