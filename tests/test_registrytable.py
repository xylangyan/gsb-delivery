# -*- coding: utf-8 -*-
import unittest

from isbnlib._core import check_digit13
from isbnlib._data.data4mask import ranges
from isbnlib._msk import msk

from isbnrecon import RegistryTable
from isbnrecon.results import UNSEGMENTED, Segmented


def table_from_isbnlib(group_keys):
    table = RegistryTable('mirror')
    for key in group_keys:
        table.set_group_rules(key, ranges[key])
    return table


class RegistryTableTests(unittest.TestCase):
    def test_mirrors_isbnlib_msk_on_real_groups(self):
        keys = ('978-0', '978-1', '978-3', '978-99930')
        table = table_from_isbnlib(keys)
        bodies = (
            '978030640615', '978019922666', '978140289456',
            '978316148410', '978999307589', '978999307588',
        )
        samples = tuple(body + check_digit13(body) for body in bodies)
        for isbn13 in samples:
            expected = msk(isbn13)
            result = table.segment(isbn13)
            if expected == '':
                self.assertIs(result, UNSEGMENTED)
                continue
            parts = expected.split('-')
            reg_prefix = parts[0] + '-' + parts[1]
            self.assertEqual(
                result,
                Segmented(reg_prefix, parts[1], parts[2]),
                isbn13)

    def test_unsegmented_when_group_missing(self):
        table = RegistryTable('slim')
        table.set_reading_rule('978-99930', 7589900, 2)
        self.assertIs(table.segment('9780306406157'), UNSEGMENTED)

    def test_unsegmented_when_reading_outside_rules(self):
        table = RegistryTable('slim')
        table.set_reading_rule('978-99930', 7589900, 2)
        self.assertIs(table.segment('97899930123456'), UNSEGMENTED)

    def test_empty_publisher_segment(self):
        table = RegistryTable('slim')
        table.set_reading_rule('978-99930', 7589900, 0)
        result = table.segment('9789993075899')
        self.assertEqual(result, Segmented('978-99930', '99930', ''))

    def test_group_rule_replacement_and_drop(self):
        table = RegistryTable('slim')
        table.set_reading_rule('978-99930', 7589900, 2)
        self.assertEqual(len(table.segment('9789993075899').publisher), 2)
        table.set_group_rules('978-99930', ((7589900, 7589900, 0),))
        self.assertEqual(table.segment('9789993075899').publisher, '')
        table.drop_group('978-99930')
        self.assertIs(table.segment('9789993075899'), UNSEGMENTED)

    def test_independent_tables(self):
        old = RegistryTable('old')
        old.set_reading_rule('978-99930', 7589900, 0)
        new = old.copy()
        new.set_reading_rule('978-99930', 7589900, 2)
        self.assertEqual(old.segment('9789993075899').publisher, '')
        self.assertEqual(new.segment('9789993075899').publisher, '75')


if __name__ == '__main__':
    unittest.main()
