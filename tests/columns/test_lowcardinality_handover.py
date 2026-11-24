from unittest import TestCase

from clickhouse_driver.columns.lowcardinalityhandover import (
    HandoverPermissionError,
    HandoverVerificationError,
    LowCardinalityFragment,
    NULL,
    analyze_fragments,
    driver_key_width,
    encode_fragment,
    fragment_from_parts,
    fragment_from_rows,
    parse_fragment,
    plan_handover,
)


def driver_native_bytes(items, nullable=True):
    """Write a column with the driver itself and return its bytes."""
    from io import BytesIO

    from clickhouse_driver.bufferedwriter import CompressedBufferedWriter
    from clickhouse_driver.columns.service import write_column
    from clickhouse_driver.context import Context

    context = Context()
    context.client_settings = {
        'use_numpy': False,
        'strings_as_bytes': False,
        'strings_encoding': 'utf-8',
        'input_format_null_as_default': False,
    }
    context.settings = {}

    spec = ('LowCardinality(Nullable(String))' if nullable
            else 'LowCardinality(String)')
    sink = BytesIO()
    buf = CompressedBufferedWriter(sink, 1024)
    write_column(context, 'a', spec, list(items), buf)
    buf.flush()
    return sink.getvalue()


NORTH = '北站'
WEST = '西站'
EMPTY = ''
CENTRAL = '中央站'
DEPOT = '车辆段'

RAW_NORTH = NORTH.encode('utf-8')
RAW_WEST = WEST.encode('utf-8')
RAW_CENTRAL = CENTRAL.encode('utf-8')


def rows_of(plan):
    return plan.read()


class KeyWidthTestCase(TestCase):
    def test_matches_driver_formula(self):
        # int(log(n, 2) / 8) boundaries, n is the dictionary size.
        self.assertEqual(driver_key_width(1), 1)
        self.assertEqual(driver_key_width(255), 1)
        self.assertEqual(driver_key_width(256), 2)
        self.assertEqual(driver_key_width(65535), 2)
        self.assertEqual(driver_key_width(65536), 4)
        self.assertEqual(driver_key_width(2 ** 24 - 1), 4)
        self.assertEqual(driver_key_width(2 ** 24), 8)

    def test_rejects_empty_dictionary(self):
        with self.assertRaises(Exception):
            driver_key_width(0)


class FragmentParsingTestCase(TestCase):
    def test_nullable_round_trip_keeps_null_slot(self):
        fragment = fragment_from_rows(
            ['北站', '', None, '北站'], name='first')

        self.assertEqual(fragment.dictionary[0], NULL)
        self.assertIn(EMPTY, fragment.dictionary)
        self.assertEqual(
            fragment.values, (NORTH, EMPTY, None, NORTH))
        self.assertEqual(fragment.used_positions, (0, 1, 2))
        self.assertEqual(parse_fragment(encode_fragment(fragment)).values,
                         fragment.values)

    def test_null_and_empty_string_have_distinct_positions(self):
        fragment = fragment_from_rows(['', None, ''], name='f')

        self.assertNotEqual(fragment.keys[0], fragment.keys[1])
        self.assertEqual(fragment.keys[0], fragment.keys[2])
        self.assertEqual(fragment.values, (EMPTY, None, EMPTY))

    def test_strings_as_bytes_and_str_are_equivalent(self):
        from_bytes = fragment_from_rows([NORTH, EMPTY], name='a')
        from_str = fragment_from_rows(['北站', ''], name='a')

        self.assertEqual(from_bytes.dictionary, from_str.dictionary)
        self.assertEqual(from_bytes.keys, from_str.keys)

    def test_non_nullable_fragment(self):
        fragment = fragment_from_rows(
            ['北站', '西站', '北站'], nullable=False, name='f')

        self.assertFalse(fragment.nullable)
        self.assertEqual(fragment.dictionary, (NORTH, WEST))
        self.assertEqual(fragment.values, (NORTH, WEST, NORTH))

    def test_rejects_truncated_keys(self):
        fragment = fragment_from_rows(['a', 'b'], name='f')
        data = encode_fragment(fragment)
        with self.assertRaises(Exception):
            parse_fragment(data[:-1])

    def test_rejects_key_outside_dictionary(self):
        with self.assertRaises(Exception):
            LowCardinalityFragment(
                name='bad',
                nullable=True,
                dictionary=(NULL, b'a'),
                keys=(2, ),
                key_width=1,
                key_bytes=b'\x02')


class AnalyzeTestCase(TestCase):
    def test_reports_used_positions_and_cost(self):
        first = fragment_from_parts(
            [None, NORTH, WEST], [1, 1, 2], name='first')
        # The second fragment still carries '西站' in its dictionary but
        # its keys only reference position 1 ('北站' there).
        second = fragment_from_parts(
            [None, NORTH, WEST], [1, 1], name='second')

        analysis = analyze_fragments([first, second])
        by_name = {s['name']: s for s in analysis['segments']}

        self.assertEqual(by_name['first']['used_positions'], (1, 2))
        self.assertEqual(by_name['first']['unused_positions'], (0, ))
        self.assertEqual(by_name['first']['rewrite_cost'], 3)
        self.assertEqual(by_name['second']['used_positions'], (1, ))
        self.assertIn(2, by_name['second']['unused_positions'])


class PositionRemappingTestCase(TestCase):
    def test_key_one_means_different_stations(self):
        north = fragment_from_parts(
            [None, NORTH], [1, 1], name='north')
        west = fragment_from_parts(
            [None, WEST], [1, 1], name='west')

        plan = plan_handover([north, west])
        report = {s['name']: s for s in plan.report()['segments']}

        # Both segments used position 1, so only one can keep it.
        self.assertEqual(report['north']['action'], 'handover')
        self.assertEqual(report['west']['action'], 'rewrite')
        self.assertEqual(report['north']['position_map'], {1: 1})
        self.assertEqual(report['west']['position_map'], {1: 2})
        self.assertEqual(report['west']['cost'], 2)
        self.assertEqual(plan.total_rewrite_cost, 2)

        # The handover kept the original dict slot and the original bytes.
        self.assertEqual(plan.dictionary[1], NORTH)
        self.assertEqual(plan.dictionary[2], WEST)
        self.assertEqual(north.key_bytes, b'\x01\x01')
        self.assertEqual(west.key_bytes, b'\x01\x01')
        self.assertEqual(
            [s.key_bytes for s in plan.handovers], [b'\x01\x01'])
        self.assertEqual(
            [s.key_bytes for s in plan.rewrites], [b'\x02\x02'])

    def test_unused_distinct_value_does_not_block_verbatim(self):
        # Both dictionaries contain '西站'.  The north segment only uses
        # position 1 ('北站') and never references its 西站 slot, so the
        # west segment's use of that value must not block either one.
        north = fragment_from_parts(
            [None, NORTH, WEST], [1, 1], name='north')
        west = fragment_from_parts(
            [None, NORTH, WEST], [2, 2], name='west')

        plan = plan_handover([north, west])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['north']['action'], 'handover')
        self.assertEqual(report['west']['action'], 'handover')
        self.assertEqual(plan.total_rewrite_cost, 0)
        self.assertEqual(plan.dictionary, (NULL, NORTH, WEST))
        self.assertEqual(
            rows_of(plan), ('北站', '北站', '西站', '西站'))

    def test_chooses_the_cheaper_segment_to_rewrite(self):
        short = fragment_from_parts(
            [None, NORTH], [1], name='short')
        long = fragment_from_parts(
            [None, WEST], [1] * 5, name='long')

        plan = plan_handover([short, long])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['long']['action'], 'handover')
        self.assertEqual(report['short']['action'], 'rewrite')
        self.assertEqual(plan.total_rewrite_cost, 1)

    def test_shared_positions_can_all_be_kept_when_values_agree(self):
        a = fragment_from_parts(
            [None, NORTH, EMPTY], [1, 2, 1], name='a')
        b = fragment_from_parts(
            [None, NORTH], [1, 1], name='b')

        plan = plan_handover([a, b])
        self.assertEqual(plan.total_rewrite_cost, 0)
        self.assertEqual(len(plan.handovers), 2)

    def test_verbatim_segment_that_uses_position_zero_is_rejected(self):
        # A non-nullable source may use position 0 for a real value.
        plain = fragment_from_parts(
            [NORTH, WEST], [0, 1], nullable=False, name='plain',
            verbatim_only=True)

        with self.assertRaises(HandoverPermissionError) as ctx:
            plan_handover([plain])

        self.assertIn('plain', ctx.exception.segments)


class NullAndEmptyTestCase(TestCase):
    def test_null_is_kept_separate_from_empty_string(self):
        first = fragment_from_rows(['', None, '北站', None], name='first')
        second = fragment_from_rows([None, ''], name='second')

        plan = plan_handover([first, second])
        values = rows_of(plan)

        self.assertEqual(
            values, ('', None, '北站', None, None, ''))
        self.assertEqual(plan.dictionary.count(NULL), 1)
        self.assertEqual(plan.total_rewrite_cost, 0)

    def test_null_and_empty_string_are_not_conflated(self):
        # The nullable segment stores the empty string at position 1.
        # The non-nullable segment stores it at position 0, which is the
        # reserved NULL slot of the target.  Keeping position 0 would
        # turn the empty string into NULL, so that segment is rewritten.
        nullable_empty = fragment_from_parts(
            [None, EMPTY], [1, 1], name='nullable-empty')
        plain_empty = fragment_from_parts(
            [EMPTY, EMPTY], [0, 0], nullable=False, name='plain-empty')

        plan = plan_handover([nullable_empty, plain_empty])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['nullable-empty']['action'], 'handover')
        self.assertEqual(report['plain-empty']['action'], 'rewrite')
        self.assertEqual(report['plain-empty']['position_map'], {0: 1})
        self.assertEqual(report['plain-empty']['cost'], 2)
        self.assertEqual(plan.total_rewrite_cost, 2)
        self.assertEqual(rows_of(plan), ('', '', '', ''))
        self.assertIs(plan.dictionary[0], NULL)
        self.assertEqual(plan.dictionary[1], EMPTY)


class KeyWidthBoundaryTestCase(TestCase):
    def test_growing_dictionary_invalidates_original_wide_keys(self):
        # 255 real values fit into one byte keys by the driver formula.
        values = ['v{}'.format(i).encode('utf-8') for i in range(254)]
        big = fragment_from_parts(
            [None] + values, list(range(1, 255)), name='big')
        self.assertEqual(big.key_width, 1)
        self.assertEqual(len(big.dictionary), 255)
        self.assertEqual(driver_key_width(255), 1)

        # One more distinct value pushes the target dictionary from 255
        # to 256 entries.  Positions 1..254 are unchanged but the driver
        # writes two-byte keys, so the original key bytes cannot be used.
        small = fragment_from_rows(
            ['v0', 'brand-new'], name='small')
        plan = plan_handover([big, small])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(plan.dictionary_size, 256)
        self.assertEqual(plan.key_width, 2)
        self.assertEqual(report['big']['action'], 'rewrite')
        self.assertEqual(report['big']['cost'], 254)
        self.assertEqual(
            report['big']['position_map'],
            {i: i for i in range(1, 255)})
        self.assertEqual(len(big.key_bytes), 254)
        self.assertEqual(len(plan.segments[0].key_bytes), 254 * 2)
        self.assertEqual(rows_of(plan), big.values + ('v0', 'brand-new'))

    def test_wide_segment_is_kept_when_target_stays_wide(self):
        values = ['v{}'.format(i).encode('utf-8') for i in range(255)]
        big = fragment_from_parts(
            [None] + values, list(range(1, 256)), name='big')
        # 256 entries are past the driver's 1-byte boundary.
        self.assertEqual(big.key_width, 2)

        plan = plan_handover([big])
        self.assertEqual(plan.key_width, 2)
        self.assertEqual(plan.total_rewrite_cost, 0)


class PermissionTestCase(TestCase):
    def test_two_verbatim_only_segments_that_conflict(self):
        north = fragment_from_parts(
            [None, NORTH], [1], name='north', verbatim_only=True)
        west = fragment_from_parts(
            [None, WEST], [1], name='west', verbatim_only=True)

        with self.assertRaises(HandoverPermissionError) as ctx:
            plan_handover([north, west])

        error = ctx.exception
        self.assertEqual(set(error.segments), {'north', 'west'})
        self.assertEqual(len(error.conflicts), 1)
        self.assertEqual(error.conflicts[0]['kind'], 'position')
        self.assertEqual(error.conflicts[0]['position'], 1)

    def test_compatible_verbatim_only_segments_are_allowed(self):
        north = fragment_from_parts(
            [None, NORTH], [1], name='north', verbatim_only=True)
        west = fragment_from_parts(
            [None, NORTH, WEST], [2], name='west',
            verbatim_only=True)

        plan = plan_handover([north, west])
        self.assertEqual(len(plan.handovers), 2)
        self.assertEqual(plan.total_rewrite_cost, 0)

    def test_verbatim_only_segment_crossing_key_width(self):
        big = fragment_from_parts(
            [None] + ['v{}'.format(i).encode('utf-8')
                      for i in range(254)],
            list(range(1, 255)), name='big', verbatim_only=True)
        small = fragment_from_rows(['extra'], name='small')

        with self.assertRaises(HandoverPermissionError) as ctx:
            plan_handover([big, small])

        self.assertIn('big', ctx.exception.segments)
        self.assertIn('key width', str(ctx.exception))

    def test_rewrite_allowed_segment_yields_to_mandatory_one(self):
        north = fragment_from_parts(
            [None, NORTH], [1], name='north', verbatim_only=True)
        west = fragment_from_parts(
            [None, WEST], [1], name='west')

        plan = plan_handover([west, north])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['north']['action'], 'handover')
        self.assertEqual(report['west']['action'], 'rewrite')
        self.assertEqual(plan.total_rewrite_cost, 1)
        self.assertEqual(rows_of(plan), ('西站', '北站'))


class VerificationTestCase(TestCase):
    def test_detects_wrong_values_in_source_segment(self):
        fragment = fragment_from_rows(['北站', '西站'], name='f')
        wrong = LowCardinalityFragment(
            name='f',
            nullable=True,
            dictionary=(NULL, NORTH, CENTRAL),
            keys=(1, 2),
            key_width=1,
            key_bytes=b'\x01\x02',
            source_bytes=encode_fragment(fragment))

        with self.assertRaises(HandoverVerificationError) as ctx:
            plan_handover([wrong])

        self.assertEqual(ctx.exception.segment, 'f')
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.expected, CENTRAL)
        self.assertEqual(ctx.exception.got, WEST)

    def test_detects_null_turned_into_empty_string(self):
        # The segment declares the empty string where re-reading the
        # source yields NULL.
        fragment = fragment_from_rows([None, '北站'], name='f')
        wrong = LowCardinalityFragment(
            name='f',
            nullable=True,
            dictionary=(NULL, EMPTY, NORTH),
            keys=(1, 2),
            key_width=1,
            key_bytes=b'\x01\x02',
            source_bytes=encode_fragment(fragment))

        with self.assertRaises(HandoverVerificationError) as ctx:
            plan_handover([wrong])

        self.assertEqual(ctx.exception.index, 0)
        self.assertEqual(ctx.exception.expected, EMPTY)
        self.assertIsNone(ctx.exception.got)

    def test_detects_empty_string_turned_into_null(self):
        # The segment declares NULL where re-reading the source yields
        # the empty string.
        fragment = fragment_from_rows(['', '西站'], name='f')
        wrong = LowCardinalityFragment(
            name='f',
            nullable=True,
            dictionary=(NULL, WEST),
            keys=(0, 1),
            key_width=1,
            key_bytes=b'\x00\x01',
            source_bytes=encode_fragment(fragment))

        with self.assertRaises(HandoverVerificationError) as ctx:
            plan_handover([wrong])

        self.assertEqual(ctx.exception.index, 0)
        self.assertIsNone(ctx.exception.expected)
        self.assertEqual(ctx.exception.got, EMPTY)

    def test_failed_plan_is_not_delivered(self):
        fragment = fragment_from_rows(['北站'], name='f')
        wrong = LowCardinalityFragment(
            name='f',
            nullable=True,
            dictionary=(NULL, CENTRAL),
            keys=(1, ),
            key_width=1,
            key_bytes=b'\x01',
            source_bytes=encode_fragment(fragment))

        with self.assertRaises(HandoverVerificationError):
            plan_handover([wrong])

    def test_verified_plan_is_delivered(self):
        plan = plan_handover([
            fragment_from_rows(['北站', '', None], name='a'),
            fragment_from_rows(['西站', None], name='b'),
        ])

        self.assertTrue(plan.verified)
        self.assertTrue(plan.delivered)
        self.assertTrue(all(s.verified for s in plan.segments))

    def test_unverified_plan_is_not_marked_delivered(self):
        plan = plan_handover(
            [fragment_from_rows(['北站'], name='a')], verify=False)

        self.assertFalse(plan.verified)
        self.assertFalse(plan.delivered)

    def test_verify_can_be_rerun_on_a_plan(self):
        plan = plan_handover(
            [fragment_from_rows(['北站', '', None], name='a')],
            verify=False)

        self.assertEqual(plan.verify().read(), ('北站', '', None))
        self.assertTrue(plan.delivered)

    def test_verify_is_repeatable(self):
        plan = plan_handover(
            [fragment_from_rows(['西站'], name='a')])

        plan.verify()
        self.assertTrue(plan.verify().verified)
        self.assertTrue(plan.delivered)


class OrderPreservationTestCase(TestCase):
    def test_rows_keep_fragment_and_row_order(self):
        first = fragment_from_rows(
            ['北站', None, '', '西站'], name='first')
        second = fragment_from_rows(
            [None, '西站', '北站', ''], name='second')
        third = fragment_from_rows(['中央站'], name='third')

        plan = plan_handover([first, second, third])

        expected = (
            '北站', None, '', '西站',
            None, '西站', '北站', '',
            '中央站')
        self.assertEqual(rows_of(plan), expected)

    def test_target_dictionary_has_one_position_per_real_value(self):
        plan = plan_handover([
            fragment_from_rows(['北站', '西站', ''], name='a'),
            fragment_from_rows(['西站', '', '北站'], name='b'),
        ])

        real = [value for value in plan.dictionary if value is not NULL]
        self.assertEqual(len(real), len(set(real)))
        self.assertEqual(set(real), {NORTH, WEST, EMPTY})
        self.assertIs(plan.dictionary[0], NULL)


class CostTestCase(TestCase):
    def test_explicit_rewrite_cost_is_summed(self):
        a = fragment_from_parts(
            [None, NORTH], [1], name='a', rewrite_cost=10)
        b = fragment_from_parts(
            [None, WEST], [1], name='b', rewrite_cost=3)

        plan = plan_handover([a, b])
        # 'b' is cheaper to rewrite, so 'a' is handed over.
        self.assertEqual(plan.total_rewrite_cost, 3)

    def test_total_cost_is_minimal_among_compatible_sets(self):
        # a (cost 1) conflicts with b at position 1; b and c are
        # compatible.  Handing b and c over and rewriting a costs 1,
        # while handing a over and rewriting b and c costs 6.
        a = fragment_from_parts(
            [None, NORTH], [1], name='a')
        b = fragment_from_parts(
            [None, WEST], [1, 1, 1], name='b')
        c = fragment_from_parts(
            [None, NORTH, CENTRAL], [2, 2, 2], name='c')

        plan = plan_handover([a, b, c])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['a']['action'], 'rewrite')
        self.assertEqual(report['b']['action'], 'handover')
        self.assertEqual(report['c']['action'], 'handover')
        self.assertEqual(plan.total_rewrite_cost, 1)

    def test_total_cost_prefers_the_cheapest_full_set(self):
        # a (cost 5) conflicts with b (cost 1); c (cost 1) is
        # unconstrained.  Rewriting a costs 5, rewriting b and c costs 2.
        a = fragment_from_parts(
            [None, NORTH], [1] * 5, name='a')
        b = fragment_from_parts(
            [None, WEST], [1], name='b')
        c = fragment_from_parts(
            [None, NORTH, CENTRAL], [2], name='c')

        plan = plan_handover([a, b, c])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['a']['action'], 'handover')
        self.assertEqual(report['b']['action'], 'rewrite')
        self.assertEqual(report['c']['action'], 'handover')
        self.assertEqual(plan.total_rewrite_cost, 1)

    def test_conflicting_optional_segments_do_not_force_a_rewrite(self):
        # Both use position 1 for different values, but they also have a
        # free slot agreeing with the other.  One handover and one
        # rewrite is already optimal.
        a = fragment_from_parts(
            [None, NORTH, WEST], [1, 2], name='a')
        b = fragment_from_parts(
            [None, WEST], [1], name='b')

        plan = plan_handover([a, b])
        report = {s['name']: s for s in plan.report()['segments']}

        self.assertEqual(report['a']['action'], 'handover')
        self.assertEqual(report['b']['action'], 'rewrite')
        self.assertEqual(plan.total_rewrite_cost, 1)
        self.assertEqual(rows_of(plan), ('北站', '西站', '西站'))


class ReportTestCase(TestCase):
    def test_report_lists_positions_actions_and_costs(self):
        first = fragment_from_parts(
            [None, NORTH, WEST], [1, 1, 2], name='first')
        second = fragment_from_parts(
            [None, WEST], [1, 1], name='second',
            verbatim_only=False)

        plan = plan_handover([first, second])
        report = plan.report()

        self.assertEqual(report['key_width'], 1)
        self.assertEqual(report['dictionary_size'], 3)
        self.assertTrue(report['verified'])
        self.assertTrue(report['delivered'])
        self.assertEqual(
            [(s['name'], s['action'], s['cost'])
             for s in report['segments']],
            [('first', 'handover', 0), ('second', 'rewrite', 2)])
        self.assertEqual(
            report['segments'][0]['used_positions'], (1, 2))
        self.assertEqual(
            report['segments'][1]['position_map'], {1: 2})

        self.assertEqual(plan.handover_list, ('first', ))
        self.assertEqual(plan.rewrite_list, ('second', ))
        self.assertEqual(plan.target_dictionary, (None, NORTH, WEST))
        self.assertEqual(report['dictionary'], (None, NORTH, WEST))
        self.assertEqual(
            report['dictionary_display'], ['NULL', NORTH, WEST])


class EmptyInputTestCase(TestCase):
    def test_empty_handover_has_only_the_null_slot(self):
        plan = plan_handover([])

        self.assertEqual(plan.dictionary, (NULL, ))
        self.assertEqual(plan.segments, [])
        self.assertTrue(plan.delivered)
        self.assertEqual(rows_of(plan), ())


class DriverNativeBytesTestCase(TestCase):
    def test_column_class_exposes_the_planner(self):
        from clickhouse_driver.columns.lowcardinalitycolumn import (
            LowCardinalityColumn)

        plan = LowCardinalityColumn.plan_handover([
            fragment_from_rows(['北站'], name='a')])

        self.assertEqual(plan.read(), ('北站', ))
        self.assertTrue(plan.delivered)

    def test_parses_column_written_by_the_driver(self):
        items = ['北站', '', None, '北站', '西站']
        fragment = parse_fragment(
            driver_native_bytes(items), nullable=True, name='native')

        self.assertEqual(fragment.values, (
            NORTH, EMPTY, None, NORTH, WEST))
        self.assertEqual(fragment.dictionary[0], NULL)

        plan = plan_handover([fragment])
        self.assertEqual(plan.total_rewrite_cost, 0)
        self.assertEqual(plan.read(), tuple(items))

    def test_mixed_native_fragments_reassemble_in_order(self):
        first_items = ['北站', None, '']
        second_items = ['西站', '', '北站', None]

        first = parse_fragment(
            driver_native_bytes(first_items), name='first')
        second = parse_fragment(
            driver_native_bytes(second_items), name='second')

        plan = plan_handover([first, second])

        self.assertEqual(
            plan.read(), tuple(first_items) + tuple(second_items))
        # Position 1 means '北站' in the first fragment and '西站' in the
        # second, so one of them must be rewritten.  The first is shorter.
        self.assertEqual(plan.total_rewrite_cost, 3)
        self.assertEqual(len(plan.handovers), 1)

    def test_non_nullable_native_fragment(self):
        items = ['北站', '西站', '北站']
        fragment = parse_fragment(
            driver_native_bytes(items, nullable=False),
            nullable=False, name='plain')

        self.assertFalse(fragment.nullable)
        self.assertEqual(fragment.dictionary, (NORTH, WEST))
        # Position 0 holds a real value in the source but is the reserved
        # NULL slot in the target, so the whole segment is rewritten.
        self.assertEqual(fragment.used_positions, (0, 1))

        plan = plan_handover([fragment])
        self.assertEqual(plan.total_rewrite_cost, 3)
        self.assertEqual(plan.read(), tuple(items))
        self.assertEqual(plan.dictionary, (NULL, NORTH, WEST))


class RandomizedPlannerTestCase(TestCase):
    """Cross-check the planner against brute force on small inputs."""

    def _build(self, rng, name):
        alphabet = ['北站', '西站', '', '中央站', '车辆段']
        nullable = rng.random() < 0.8
        size = rng.randint(1, 4)
        values = [None] if nullable else []
        values += rng.sample(alphabet, size)
        keys = [
            rng.randrange(len(values))
            for _ in range(rng.randint(0, 5))]
        width = driver_key_width(max(1, len(values)))
        return fragment_from_parts(
            values, keys, nullable=nullable, name=name, key_width=width)

    def test_matches_brute_force(self):
        import itertools
        import random

        from clickhouse_driver.columns.lowcardinalityhandover import (
            _candidate_conflict, _ineligible_reasons)

        rng = random.Random(20261124)

        for _ in range(200):
            fragments = [
                self._build(rng, 'f{}'.format(i))
                for i in range(rng.randint(1, 5))]
            expected_rows = []
            for fragment in fragments:
                expected_rows.extend(fragment.values)

            plan = plan_handover(fragments)
            self.assertEqual(plan.read(), tuple(expected_rows))
            self.assertTrue(plan.delivered)

            analysis = analyze_fragments(fragments)
            size = analysis['dictionary_size']
            width = analysis['key_width']
            eligible = [
                fragment for fragment in fragments
                if not _ineligible_reasons(fragment, size, width)]

            best = 0
            for r in range(len(eligible) + 1):
                for subset in itertools.combinations(eligible, r):
                    ok = all(
                        _candidate_conflict(a, b) is None
                        for a, b in itertools.combinations(subset, 2))
                    if ok:
                        saved = sum(f.rewrite_cost for f in subset)
                        best = max(best, saved)

            total = sum(f.rewrite_cost for f in fragments)
            self.assertEqual(plan.total_rewrite_cost, total - best)
