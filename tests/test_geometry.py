# -*- coding: utf-8 -*-
import unittest

from isbnrecon import (
    ABSENT,
    UNSEGMENTED,
    Bundle,
    BundleSet,
    Ledger,
    RegistryTable,
    Segmented,
)
from tests._oracle import brute_grid


class ShortGroupTests(unittest.TestCase):
    def test_978_0_multiple_rules_brute_force(self):
        # Group 978-0 ends at cur=3, so the seven-digit reading is made of
        # body digits only: this exercises the check-independent geometry.
        lo, hi = 978000000100, 978000000400
        old_bundles = BundleSet([
            Bundle('o1', 'isbn13', str(lo), str(hi), '01234'),
            Bundle('o2', 'isbn13', str(lo + 50), str(hi + 100), '56789'),
        ])
        new_bundles = BundleSet([
            Bundle('n1', 'isbn13', str(lo), str(lo + 150), '0123456789'),
        ])
        rules = (
            (0, 1_999_999, 2),
            (2_000_000, 2_279_999, 3),
            (2_280_000, 2_289_999, 4),
        )
        old_table = RegistryTable('old')
        old_table.set_group_rules('978-0', rules)
        new_table = RegistryTable('new')
        new_table.set_group_rules('978-0', ((0, 9_999_999, 1),))
        ledger = Ledger(old_bundles, new_bundles, old_table, new_table)
        got = ledger.grid(region=(lo, hi))
        expected = brute_grid(ledger.before, (lo, hi))
        self.assertEqual(set(got), set(expected))
        for label, (count, min_key) in expected.items():
            self.assertEqual(got[label].count, count, label)
            self.assertEqual(got[label].min_isbn13, min_key, label)

    def test_point_semantics_absent_unsegmented_segmented(self):
        lo = 978999307589
        bundles = BundleSet([
            Bundle('b13', 'isbn13', str(lo), str(lo), '9'),
        ])
        table = RegistryTable('t')
        table.set_reading_rule('978-99930', 7589900, 2)
        ledger = Ledger(bundles, BundleSet(), table, RegistryTable('empty'))
        old_view, new_view = ledger.before
        self.assertEqual(
            old_view.result_for(lo),
            Segmented('978-99930', '99930', '75'))
        self.assertIs(new_view.result_for(lo), ABSENT)

        slim = RegistryTable('slim')
        slim.set_reading_rule('978-1', 1, 1)
        view = type(old_view)('v', bundles, slim)
        self.assertIs(view.result_for(lo), UNSEGMENTED)

    def test_979_has_no_isbn10_fallback(self):
        lo = 979000000005
        bundles13 = BundleSet([Bundle('p', 'isbn13', str(lo), str(lo), '0123456789')])
        bundles10 = BundleSet([Bundle('ten', 'isbn10', '000000005', '000000005', 'X')])
        table = RegistryTable('t')
        table.set_reading_rule('979-0', 50, 1)
        ledger = Ledger(bundles13, bundles10, table, table.copy())
        # An ISBN-10 bundle must never support a 979-prefixed key.
        _, new_view = ledger.before
        self.assertFalse(new_view.side_supports(lo))
        evidence = new_view.evidence_for(lo)
        self.assertEqual(evidence.records, ())
        self.assertIs(evidence.result, ABSENT)


if __name__ == '__main__':
    unittest.main()
