# -*- coding: utf-8 -*-
import unittest

from isbnrecon import Bundle, BundleSet


class BundleTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            Bundle('b', 'isbn13', '12', '123', '0')
        with self.assertRaises(ValueError):
            Bundle('b', 'isbn13', 'abc', 'abc', '0')
        with self.assertRaises(ValueError):
            Bundle('b', 'isbn13', '999', '998', '0')
        with self.assertRaises(ValueError):
            Bundle('b', 'isbn13', '999', '999', '')
        with self.assertRaises(ValueError):
            Bundle('b', 'isbn13', '999', '999', 'X')
        Bundle('b', 'isbn10', '999', '999', 'X')

    def test_covering_and_intersecting(self):
        bundle_set = BundleSet([
            Bundle('a', 'isbn13', '100', '200', '0'),
            Bundle('b', 'isbn13', '150', '300', '1'),
            Bundle('c', 'isbn13', '900', '950', '2'),
        ])
        self.assertEqual({b.id for b in bundle_set.covering(170)}, {'a', 'b'})
        self.assertEqual({b.id for b in bundle_set.covering(300)}, {'b'})
        self.assertEqual(bundle_set.covering(400), ())
        self.assertEqual(
            {b.id for b in bundle_set.intersecting(250, 1000)},
            {'b', 'c'})
        self.assertEqual(
            {b.id for b in bundle_set.intersecting(0, 99)}, set())

    def test_union_segments_and_overlap_dedup(self):
        bundle_set = BundleSet([
            Bundle('a', 'isbn13', '100', '200', '0'),
            Bundle('b', 'isbn13', '150', '300', '1'),
            Bundle('c', 'isbn13', '301', '400', '2'),
        ])
        self.assertEqual(bundle_set.union_segments(), [(100, 300), (301, 400)])

    def test_mutable_ids_and_copy(self):
        bundle_set = BundleSet([Bundle('a', 'isbn13', '1', '2', '0')])
        clone = bundle_set.copy()
        clone.remove('a')
        self.assertIn('a', bundle_set)
        self.assertNotIn('a', clone)
        with self.assertRaises(ValueError):
            bundle_set.add(Bundle('a', 'isbn13', '3', '4', '0'))


if __name__ == '__main__':
    unittest.main()
