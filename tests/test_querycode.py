# -*- coding: utf-8 -*-
"""Tests for the query-code engine."""
import json
import random
import unittest

import querycode as qc
from querycode.search import Sheet
from querycode.verify import source_codes, verify_by_enumeration


def make(**overrides):
    spec = {
        'fragments': {'a': 'ab', 'b': 'bc'},
        'templates': {'T': {'seq': [{'frag': 'a'}, {'frag': 'b'}]}},
        'sources': {'A': {'templates': ['T']}, 'B': {'templates': ['T']}},
    }
    spec.update(overrides)
    return qc.Spec(spec)


class TransliterateTests(unittest.TestCase):
    def test_explicit_empty_stays_empty_under_replace(self):
        self.assertEqual('', qc.transliterate('­', 'replace', '_'))

    def test_missing_uses_replace_string(self):
        self.assertEqual('_', qc.transliterate('', 'replace', '_'))

    def test_missing_ignored(self):
        self.assertEqual('', qc.transliterate('', 'ignore'))

    def test_strict_reports_origin_index(self):
        with self.assertRaises(qc.StrictError) as caught:
            qc.transliterate('ab', 'strict')
        self.assertEqual(2, caught.exception.index)

    def test_strict_ignores_empty_mapping(self):
        self.assertEqual('ab', qc.transliterate('ab⁠', 'strict'))

    def test_matches_library(self):
        from unidecode import unidecode
        for text in ['ČŽŠčžš', 'ﬁs', 'Straße', '①Ⅰ', 'Ünïcode']:
            self.assertEqual(unidecode(text),
                             qc.transliterate(text, 'ignore'))

    def test_preserve_rejected(self):
        with self.assertRaises(qc.SpecError):
            qc.transliterate('a', 'preserve')


class SpecTests(unittest.TestCase):
    def test_cycle_rejected(self):
        with self.assertRaises(qc.SpecError):
            qc.Spec({
                'templates': {'T': {'ref': 'U'}, 'U': {'ref': 'T'}},
                'sources': {'A': {'templates': ['T']}},
            })

    def test_unbounded_rep_rejected(self):
        with self.assertRaises(qc.SpecError):
            qc.Spec({
                'fragments': {'f': 'a'},
                'templates': {'T': {'rep': {'expr': {'frag': 'f'}, 'min': 1}}},
                'sources': {'A': {'templates': ['T']}},
            })

    def test_unknown_fragment_rejected(self):
        with self.assertRaises(qc.SpecError):
            qc.Spec({'templates': {'T': {'frag': 'missing'}},
                     'sources': {'A': {'templates': ['T']}}})


class AlignmentTests(unittest.TestCase):
    def test_one_character_two_outputs_aligns_with_two_characters(self):
        spec = qc.Spec({
            'fragments': {'ligature': 'ﬁ', 'f': 'f', 'i': 'i', 's': 's'},
            'templates': {
                'left': {'seq': [{'frag': 'ligature'}, {'frag': 's'}]},
                'right': {'seq': [{'frag': 'f'}, {'frag': 'i'}, {'frag': 's'}]},
            },
            'sources': {'A': {'templates': ['left']},
                        'B': {'templates': ['right']}},
        })
        total, page = qc.compare_sources(spec, 'A', 'B')
        self.assertEqual(1, total)
        self.assertEqual('fis', page[0].code)
        self.assertEqual('different-name', page[0].kind)
        self.assertEqual('ﬁs', page[0].left.name)
        self.assertEqual('fis', page[0].right.name)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.spec = qc.load_spec('examples/querycode_demo.json')

    def test_different_names(self):
        result = qc.classify_code(self.spec, 'OLD_A', 'OLD_B', 'fis')
        self.assertEqual('different-name', result.kind)
        self.assertEqual('ﬁs', result.left.name)
        self.assertEqual('fis', result.right.name)

    def test_same_name_repeat(self):
        result = qc.classify_code(self.spec, 'OLD_A', 'OLD_B', '1')
        self.assertEqual('same-name-repeat', result.kind)
        self.assertEqual(result.left.name, result.right.name)

    def test_absent_on_one_side(self):
        absent_b = qc.classify_code(self.spec, 'OLD_A', 'OLD_B', 'Is')
        self.assertEqual('absent-b', absent_b.kind)
        absent_a = qc.classify_code(self.spec, 'OLD_A', 'OLD_B', 'fix_')
        self.assertEqual('absent-a', absent_a.kind)

    def test_every_witness_is_legal(self):
        total, page = qc.compare_sources(self.spec, 'OLD_A', 'OLD_B')
        for result in page:
            for source, witness in (('OLD_A', result.left),
                                    ('OLD_B', result.right)):
                sheet = self.spec.sources[source]
                self.assertEqual(
                    result.code,
                    qc.transliterate(witness.name, sheet['missing'],
                                     sheet['replace_str']))

    def test_page_is_byte_ordered(self):
        _total, page = qc.compare_sources(self.spec, 'OLD_A', 'OLD_B')
        codes = [result.code.encode('ascii') for result in page]
        self.assertEqual(sorted(codes), codes)

    def test_dedup_across_routes(self):
        # Two different template routes spelling the same name/code count once.
        spec = qc.Spec({
            'fragments': {'a': 'ab'},
            'templates': {
                'one': {'frag': 'a'},
                'two': {'seq': [{'lit': 'a'}, {'lit': 'b'}]},
            },
            'sources': {'A': {'templates': ['one']},
                        'B': {'templates': ['one', 'two']}},
        })
        total, page = qc.compare_sources(spec, 'A', 'B')
        self.assertEqual(1, total)
        self.assertEqual('ab', page[0].code)
        self.assertEqual('same-name-repeat', page[0].kind)


class DifferentialTests(unittest.TestCase):
    def test_random_specs_agree_with_brute_force(self):
        pool = ['a', 'b', 's', 'x', 'ﬁ', 'ß', '①', '­',
                '⁠', '', '͸', '']
        for seed in range(120):
            random.seed(seed)
            fragments = {
                'f%d' % i: ''.join(random.choice(pool)
                                   for _ in range(random.randint(0, 2)))
                for i in range(random.randint(1, 5))
            }

            def expr(depth=0):
                if depth > 2 or random.random() < 0.5:
                    if random.random() < 0.65:
                        return {'frag': random.choice(list(fragments))}
                    return {'lit': ''.join(
                        random.choice(pool)
                        for _ in range(random.randint(0, 2)))}
                op = random.choice(['seq', 'alt', 'rep'])
                if op == 'seq':
                    return {'seq': [expr(depth + 1)
                                    for _ in range(random.randint(1, 3))]}
                if op == 'alt':
                    return {'alt': [expr(depth + 1)
                                    for _ in range(random.randint(1, 3))]}
                min_count = random.randint(0, 2)
                max_count = min_count + random.randint(0, 1)
                return {'rep': {'expr': expr(depth + 1),
                                'min': min_count, 'max': max_count}}

            templates = {'t%d' % i: expr()
                         for i in range(random.randint(1, 4))}
            sources = {}
            for name in ('A', 'B'):
                sources[name] = {
                    'templates': [random.choice(list(templates))
                                  for _ in range(random.randint(1, 2))],
                    'missing': random.choice(['ignore', 'replace', 'strict']),
                    'replace_str': random.choice(['?', '_']),
                }
            spec = qc.Spec({'fragments': fragments,
                            'templates': templates,
                            'sources': sources})
            total, page = qc.compare_sources(spec, 'A', 'B', limit=500)
            reference_total, reference_kinds = verify_by_enumeration(
                spec, 'A', 'B')
            self.assertEqual(reference_total, total, 'seed %d' % seed)
            for result in page:
                expected = ('different-name'
                            if reference_kinds[result.code] == 'different-name'
                            else 'same-name-repeat')
                self.assertEqual(expected, result.kind, 'seed %d' % seed)


class DemoReportTests(unittest.TestCase):
    def test_report_page_counts(self):
        spec = qc.load_spec('examples/querycode_demo.json')
        total, page = qc.compare_sources(spec, 'OLD_A', 'OLD_B', limit=2)
        self.assertEqual(3, total)
        self.assertEqual(2, len(page))
        distinct, distinct_page = qc.count_distinct_name_codes(
            spec, 'OLD_A', 'OLD_B', limit=1)
        self.assertEqual(2, distinct)
        self.assertEqual(1, len(distinct_page))

    def test_verify_by_enumeration_matches(self):
        spec = qc.load_spec('examples/querycode_demo.json')
        total = qc.compare_sources(spec, 'OLD_A', 'OLD_B')[0]
        reference_total, _kinds = verify_by_enumeration(spec, 'OLD_A', 'OLD_B')
        self.assertEqual(reference_total, total)


if __name__ == '__main__':
    unittest.main()
