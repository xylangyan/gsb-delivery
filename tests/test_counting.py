# -*- coding: utf-8 -*-
import time
import unittest

from isbnlib._core import (
    check_digit10,
    check_digit13,
    is_isbn10,
    is_isbn13,
    to_isbn10,
    to_isbn13,
)

from isbnrecon import Bundle, count_realized, min_realized
from isbnrecon.counting import (
    PREFIX_978,
    min_match,
    tally_with_min,
)


def brute_count(bundles, lo, hi):
    kind = bundles[0].kind
    width = bundles[0].width
    chars = frozenset().union(*(b.check_chars for b in bundles))
    total = 0
    minimum = None
    for body in range(lo, hi + 1):
        body_str = str(body).zfill(width)
        if kind == 'isbn13':
            check = check_digit13(body_str)
            key = body_str + check
            ok = check in chars and is_isbn13(key)
        else:
            check = check_digit10(body_str)
            printed = body_str + check
            ok = check in chars and is_isbn10(printed)
            key = to_isbn13(printed)
        if ok:
            total += 1
            minimum = key if minimum is None else min(minimum, key)
    return total, minimum


class CountingTests(unittest.TestCase):
    def test_drill_intervals(self):
        bundles = [Bundle('b', 'isbn10', '999307588', '999307589', '24')]
        self.assertEqual(count_realized(bundles, 999307588, 999307589), 2)
        self.assertEqual(min_realized(bundles, 999307588, 999307589),
                         '9789993075882')

    def test_brute_force_small_intervals(self):
        scenarios = [
            ('isbn13', 978000000000, 978000000047, '037'),
            ('isbn13', 978000001023, 978000001199, '159'),
            ('isbn10', 5, 83, '24'),
            ('isbn10', 100, 260, '0X'),
        ]
        for kind, lo, hi, chars in scenarios:
            width = 12 if kind == 'isbn13' else 9
            start = str(lo).zfill(width)
            end = str(hi).zfill(width)
            bundles = [Bundle('b', kind, start, end, chars)]
            count = count_realized(bundles, lo, hi)
            expected_count, expected_min = brute_count(bundles, lo, hi)
            self.assertEqual(count, expected_count, (kind, lo, hi, chars))
            self.assertEqual(min_realized(bundles, lo, hi), expected_min)

    def test_overlapping_bundles_count_once(self):
        base = 978000001000
        bundles = [
            Bundle('a', 'isbn13', str(base), str(base + 200), '01'),
            Bundle('b', 'isbn13', str(base + 100), str(base + 300), '12'),
        ]
        count = count_realized(bundles, base, base + 300)
        expected, minimum = brute_count(bundles, base, base + 300)
        self.assertEqual(count, expected)
        self.assertEqual(min_realized(bundles, base, base + 300), minimum)

    def test_huge_interval_performance(self):
        lo = 978000000000
        hi = lo + 200_000_000 - 1
        bundle = Bundle('huge', 'isbn13', str(lo), str(hi), '0123456789')
        start = time.perf_counter()
        count = count_realized([bundle], lo, hi)
        minimum = min_realized([bundle], lo, hi)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0)
        # Every body realizes exactly one valid ISBN-13 with all checks open.
        self.assertEqual(count, 200_000_000)
        self.assertEqual(minimum, str(lo) + check_digit13(str(lo)))

    def test_isbn10_huge_interval_maps_under_978(self):
        lo, hi = 0, 9_999_999
        bundle = Bundle('ten', 'isbn10', str(lo).zfill(9),
                        str(hi).zfill(9), 'X')
        start = time.perf_counter()
        count = count_realized([bundle], lo, hi)
        self.assertLess(time.perf_counter() - start, 5.0)
        self.assertGreater(count, 0)
        key = min_realized([bundle], lo, hi)
        self.assertTrue(key.startswith('978') and is_isbn13(key))
        # The realized 978 key must correspond to an ISBN-10 ending in X.
        printed10 = to_isbn10(key)
        self.assertTrue(is_isbn10(printed10))
        self.assertEqual(printed10[-1], 'X')
        self.assertEqual(to_isbn13(printed10), key)

    def test_engine_leaf_labels_and_min(self):
        lo, hi = 978999307588, 978999307599

        def leaf(c13, c10, trace):
            return c13
        result = tally_with_min(lo, hi, (), leaf)
        self.assertEqual(sum(count for count, _ in result.values()),
                         hi - lo + 1)
        smallest = min(result, key=lambda label: result[label][1])
        self.assertEqual(min_match(lo, hi, lambda c13, c10: True),
                         result[smallest][1])
        self.assertEqual(PREFIX_978, 978 * 10 ** 9)


if __name__ == '__main__':
    unittest.main()
