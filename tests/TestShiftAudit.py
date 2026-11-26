# -*- coding: utf-8 -*-
"""
Tests for the pre-work shift audit (shiftaudit).

The small schedules here are checked directly against the stock
parsedatetime ``evalRanges`` and the agreed timezone; the offset search is
then checked on top of those exact outputs.
"""

import datetime
import time
import unittest
import zoneinfo

import parsedatetime as pdt

from shiftaudit import (
    AnchorBatch,
    AuditConfig,
    Order,
    Route,
    ScheduleAuditor,
    TIE_EARLIER,
    TIE_LATER,
    TIE_NONE,
    REASON_CLOCK_FALLBACK,
    REASON_INVERTED,
    REASON_MISSING_CELL,
    REASON_NOT_A_RANGE,
    WALL_FOLD,
    WALL_GAP,
    WALL_UNIQUE,
    classify_wall_clock,
    local_to_utc,
)
from shiftaudit.engine import merge_ranges


NY = zoneinfo.ZoneInfo('America/New_York')
UTC = datetime.timezone.utc
MACHINE_CLOCK = datetime.datetime(2026, 11, 27, 4, 7, 26)
# Real UTC anchor that reads 2006-08-28 12:30:15 local time in New York.
ANCHOR_UTC = datetime.datetime(2006, 8, 28, 16, 30, 15)
LOCAL_ANCHOR = datetime.datetime(2006, 8, 28, 12, 30, 15)


def make_auditor(routes, offsets=(0,)):
    config = AuditConfig(
        routes=routes,
        zone=NY,
        candidate_offsets=offsets,
        machine_clock=MACHINE_CLOCK)
    return ScheduleAuditor(config), config


def single_route(orders, start=ANCHOR_UTC, step=86400, route_id='R'):
    batch = AnchorBatch(start, step, len(orders))
    return Route(route_id, orders, [batch])


class testClock(unittest.TestCase):

    def test_unique_wall_clock(self):
        wall = datetime.datetime(2006, 8, 29, 9, 0)
        self.assertEqual(classify_wall_clock(wall, NY), WALL_UNIQUE)
        self.assertEqual(
            local_to_utc(wall, NY, 0),
            datetime.datetime(2006, 8, 29, 13, 0, tzinfo=UTC))

    def test_repeated_fall_back_wall_clock(self):
        # 01:30 happens twice on 2006-10-29 in New York.
        wall = datetime.datetime(2006, 10, 29, 1, 30)
        self.assertEqual(classify_wall_clock(wall, NY), WALL_FOLD)
        self.assertEqual(
            local_to_utc(wall, NY, 0),
            datetime.datetime(2006, 10, 29, 5, 30, tzinfo=UTC))
        self.assertEqual(
            local_to_utc(wall, NY, 1),
            datetime.datetime(2006, 10, 29, 6, 30, tzinfo=UTC))

    def test_spring_forward_gap_wall_clock(self):
        # 02:30 never happens on 2006-04-02 in New York.
        wall = datetime.datetime(2006, 4, 2, 2, 30)
        self.assertEqual(classify_wall_clock(wall, NY), WALL_GAP)


class testAnchorBatch(unittest.TestCase):

    def test_anchors_cross_month_and_year_by_seconds(self):
        batch = AnchorBatch(
            datetime.datetime(2006, 12, 31, 23, 0), 3600, 3)
        self.assertEqual(
            batch.anchors(),
            (datetime.datetime(2006, 12, 31, 23, 0),
             datetime.datetime(2007, 1, 1, 0, 0),
             datetime.datetime(2007, 1, 1, 1, 0)))

    def test_validation(self):
        with self.assertRaises(ValueError):
            AnchorBatch(ANCHOR_UTC, 0, 2)
        with self.assertRaises(ValueError):
            AnchorBatch(ANCHOR_UTC, 60, 0)


class testRouteModel(unittest.TestCase):

    def test_anchor_count_must_match_orders(self):
        batch = AnchorBatch(ANCHOR_UTC, 86400, 2)
        with self.assertRaises(ValueError):
            Route('R', [Order('A', '09:00-10:00', 'M1')], [batch])

    def test_duplicate_order_id_rejected_within_route(self):
        orders = [Order('A', '09:00-10:00', 'M1'),
                  Order('A', '11:00-12:00', 'M1')]
        with self.assertRaises(ValueError):
            single_route(orders)

    def test_same_id_on_two_routes_stays_distinct(self):
        r1 = single_route([Order('A', '09:00-10:00', 'M1')], route_id='R1')
        r2 = single_route([Order('A', '11:00-12:00', 'M2')], route_id='R2')
        auditor, _ = make_auditor([r1, r2])
        result = auditor.run()[0]
        self.assertTrue(result.result_for('R1', 'A').bookable)
        self.assertTrue(result.result_for('R2', 'A').bookable)


class testStockLibraryQuirks(unittest.TestCase):
    """Direct checks against evalRanges and the agreed timezone."""

    def setUp(self):
        self.cal = pdt.Calendar()

    def _range(self, note):
        return self.cal.evalRanges(note, LOCAL_ANCHOR.timetuple())

    def test_tomorrow_then_morning_window_wins(self):
        # The collector actually books the next-morning window, not the
        # evening shift written in front of it.
        start, end, flag = self._range(
            'tomorrow 9-11pm then 7:45-8:15')
        self.assertEqual(flag, 2)
        self.assertEqual(start[:6], (2006, 8, 29, 7, 45, 0))
        self.assertEqual(end[:6], (2006, 8, 29, 8, 15, 0))

    def test_evening_window_alone_books_the_evening(self):
        start, end, flag = self._range('tomorrow 9-11pm')
        self.assertEqual(flag, 2)
        self.assertEqual(start[:6], (2006, 8, 29, 21, 0, 0))
        self.assertEqual(end[:6], (2006, 8, 29, 23, 0, 0))

    def test_date_range_inherits_anchor_hms(self):
        start, end, flag = self._range(
            'August 29 - September 2, 2006')
        self.assertEqual(flag, 1)
        self.assertEqual(start[:6], (2006, 8, 29, 12, 30, 15))
        self.assertEqual(end[:6], (2006, 9, 2, 12, 30, 15))

    def test_inverted_time_window_returned_as_is(self):
        start, end, flag = self._range('5-3pm')
        self.assertEqual(flag, 2)
        self.assertGreater(start[:6], end[:6])

    def test_inverted_date_window_returned_as_is(self):
        start, end, flag = self._range(
            'September 5 - August 3, 2006')
        self.assertEqual(flag, 1)
        self.assertGreater(start[:6], end[:6])

    def test_no_overnight_rollover(self):
        # Old orders have no automatic cross-midnight rule: the end stays
        # on the same calendar day, producing a reversed window.
        start, end, flag = self._range('11:30pm-12:30am')
        self.assertEqual(flag, 2)
        self.assertEqual(start[:3], end[:3])
        self.assertGreater(start[:6], end[:6])

    def test_leftover_note_reads_machine_clock(self):
        frozen = time.localtime()
        original = time.localtime
        time.localtime = lambda *args: MACHINE_CLOCK.timetuple()
        try:
            start, end, flag = self.cal.evalRanges(
                '9-11pm foo bar', LOCAL_ANCHOR.timetuple())
        finally:
            time.localtime = original
        self.assertEqual(flag, 2)
        self.assertEqual(start[:6], (2026, 11, 27, 21, 0, 0))
        self.assertEqual(end[:6], (2026, 11, 27, 23, 0, 0))
        self.assertIsNot(frozen, None)


class testOrderEvaluation(unittest.TestCase):

    def test_headline_note_books_morning_window(self):
        route = single_route(
            [Order('Q', 'tomorrow 9-11pm then 7:45-8:15', 'M1')])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0].order_results[0]
        self.assertTrue(result.bookable)
        booked = result.ranges[0]
        self.assertEqual(
            booked.start_local, datetime.datetime(2006, 8, 29, 7, 45))
        self.assertEqual(
            booked.end_local, datetime.datetime(2006, 8, 29, 8, 15))
        self.assertEqual(
            booked.start_utc,
            datetime.datetime(2006, 8, 29, 11, 45, tzinfo=UTC))

    def test_date_range_carries_anchor_time_of_day(self):
        route = single_route(
            [Order('Q', 'August 29 - September 2, 2006', 'M1')])
        auditor, _ = make_auditor([route])
        booked = auditor.run()[0].order_results[0].ranges[0]
        self.assertEqual(
            booked.start_utc,
            datetime.datetime(2006, 8, 29, 16, 30, 15, tzinfo=UTC))
        self.assertEqual(
            booked.end_utc,
            datetime.datetime(2006, 9, 2, 16, 30, 15, tzinfo=UTC))

    def test_flag_zero_note_is_clock_fallback(self):
        route = single_route([Order('Q', 'noon-midnight', 'M1')])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0].order_results[0]
        self.assertFalse(result.bookable)
        self.assertEqual(result.reason, REASON_CLOCK_FALLBACK)

    def test_leftover_note_is_clock_fallback(self):
        route = single_route([Order('Q', '9-11pm foo bar', 'M1')])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0].order_results[0]
        self.assertFalse(result.bookable)
        self.assertEqual(result.reason, REASON_CLOCK_FALLBACK)

    def test_unparseable_window_keeps_anchor_not_machine_clock(self):
        # A recognised but impossible range returns flag 0 with the source
        # anchor untouched, which is distinct from a host-clock fallback.
        route = single_route([Order('Q', '25:00-26:00', 'M1')])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0].order_results[0]
        self.assertFalse(result.bookable)
        self.assertEqual(result.reason, REASON_NOT_A_RANGE)

    def test_machine_clock_is_restored_after_audit(self):
        route = single_route([Order('Q', 'noon-midnight', 'M1')])
        auditor, _ = make_auditor([route])
        auditor.run()
        self.assertEqual(time.localtime().tm_year,
                         datetime.datetime.now().year)

    def test_inverted_time_and_date_are_unbookable(self):
        for note in ('5-3pm', 'September 5 - August 3, 2006'):
            route = single_route([Order('Q', note, 'M1')])
            auditor, _ = make_auditor([route])
            result = auditor.run()[0].order_results[0]
            self.assertFalse(result.bookable, note)
            self.assertEqual(result.reason, REASON_INVERTED, note)

    def test_spring_forward_gap_is_missing_cell(self):
        # 2006-04-02 11:00 local, inside EDT after the jump.
        batch = AnchorBatch(datetime.datetime(2006, 4, 2, 15, 0), 3600, 1)
        route = Route('R', [Order('Q', '2:30-2:45', 'M1')], [batch])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0].order_results[0]
        self.assertFalse(result.bookable)
        self.assertEqual(result.reason, REASON_MISSING_CELL)

    def test_fall_back_fold_tie_rules(self):
        # 2006-10-29 11:00 local; 01:30 is a repeated wall clock.
        batch = AnchorBatch(datetime.datetime(2006, 10, 29, 16, 0), 3600, 1)

        route = Route(
            'R',
            [Order('E', '1:30-1:45', 'M1', TIE_EARLIER)],
            [batch])
        booked = make_auditor([route])[0].run()[0].order_results[0].ranges[0]
        self.assertEqual(booked.start_wall, WALL_FOLD)
        self.assertEqual(
            booked.start_utc,
            datetime.datetime(2006, 10, 29, 5, 30, tzinfo=UTC))

        route = Route(
            'R',
            [Order('L', '1:30-1:45', 'M1', TIE_LATER)],
            [batch])
        booked = make_auditor([route])[0].run()[0].order_results[0].ranges[0]
        self.assertEqual(
            booked.start_utc,
            datetime.datetime(2006, 10, 29, 6, 30, tzinfo=UTC))

        route = Route(
            'R',
            [Order('N', '1:30-1:45', 'M1', TIE_NONE)],
            [batch])
        result = make_auditor([route])[0].run()[0].order_results[0]
        self.assertFalse(result.bookable)
        self.assertEqual(result.reason, REASON_MISSING_CELL)


class testOffsetSearch(unittest.TestCase):

    def _two_routes(self):
        # Both orders land on the same machine M1.
        batch = AnchorBatch(ANCHOR_UTC, 86400, 1)
        r1 = Route('R1', [Order('A', 'tomorrow 9-11pm', 'M1')], [batch])
        r2 = Route(
            'R2',
            [Order('B', 'August 29 - September 2, 2006', 'M1')],
            [batch])
        return r1, r2

    def test_bookable_offsets_avoid_same_device_clash(self):
        r1, r2 = self._two_routes()
        # -1 day: evening shift lands on Aug 28, outside B's window.
        # 0 and +1 day: the evening shift falls inside B's multi-day window.
        auditor, _ = make_auditor([r1, r2], offsets=(-86400, 0, 86400))
        self.assertEqual(auditor.bookable_offsets(), [-86400])

    def test_earliest_collision_and_sorting(self):
        r1, r2 = self._two_routes()
        auditor, _ = make_auditor([r1, r2], offsets=(0, 86400))
        zero = auditor.run()[0]
        self.assertFalse(zero.bookable)
        collision = zero.earliest_collision
        self.assertEqual(collision.order_a.order_id, 'A')
        self.assertEqual(collision.order_b.order_id, 'B')
        self.assertEqual(
            collision.collision_start_utc,
            datetime.datetime(2006, 8, 30, 1, 0, tzinfo=UTC))
        self.assertEqual(
            collision.overlap_end_utc,
            datetime.datetime(2006, 8, 30, 3, 0, tzinfo=UTC))

        plus_day = auditor.run()[1]
        self.assertEqual(
            plus_day.earliest_collision.collision_start_utc,
            datetime.datetime(2006, 8, 31, 1, 0, tzinfo=UTC))

    def test_different_devices_never_collide(self):
        batch = AnchorBatch(ANCHOR_UTC, 86400, 1)
        r1 = Route('R1', [Order('A', 'tomorrow 9-11pm', 'M1')], [batch])
        r2 = Route(
            'R2',
            [Order('B', 'August 29 - September 2, 2006', 'M2')],
            [batch])
        auditor, _ = make_auditor([r1, r2], offsets=(0,))
        self.assertEqual(auditor.bookable_offsets(), [0])

    def test_half_open_boundary_does_not_collide(self):
        # Three same-machine windows on one day; B starts exactly when A
        # ends, C overlaps B.
        batch = AnchorBatch(
            datetime.datetime(2006, 8, 28, 10, 0), 3600, 3)
        route = Route('R', [
            Order('A', '09:00-10:00', 'M1'),
            Order('B', '10:00-11:00', 'M1'),
            Order('C', '10:30-11:30', 'M1')], [batch])
        auditor, _ = make_auditor([route])
        result = auditor.run()[0]
        self.assertFalse(result.bookable)
        self.assertEqual(
            [(c.order_a.order_id, c.order_b.order_id)
             for c in result.collisions],
            [('B', 'C')])

    def test_collision_order_breaks_ties_by_order_id(self):
        # Three identical same-device windows all overlap at the same
        # real instant; the original order number breaks the ordering.
        batch = AnchorBatch(
            datetime.datetime(2006, 8, 28, 10, 0), 3600, 3)
        route = Route('R', [
            Order('A', '09:00-10:00', 'M1'),
            Order('B', '09:00-10:00', 'M1'),
            Order('C', '09:00-10:00', 'M1')], [batch])
        auditor, _ = make_auditor([route])
        pairs = [(c.order_a.order_id, c.order_b.order_id)
                 for c in auditor.run()[0].collisions]
        self.assertEqual(pairs, [('A', 'B'), ('A', 'C'), ('B', 'C')])
        self.assertTrue(all(
            c.collision_start_utc
            == datetime.datetime(2006, 8, 28, 13, 0, tzinfo=UTC)
            for c in auditor.run()[0].collisions))


class testTraceAndMerge(unittest.TestCase):

    def test_explain_offset_reports_anchor_and_wall_conversion(self):
        batch = AnchorBatch(ANCHOR_UTC, 86400, 1)
        route = Route(
            'R1', [Order('A', 'August 29 - September 2, 2006', 'M1')],
            [batch])
        auditor, _ = make_auditor([route], offsets=(3600,))
        result, trace = auditor.explain_offset(3600)
        entry = trace[0]
        self.assertEqual(
            entry['anchor_utc'], ANCHOR_UTC + datetime.timedelta(seconds=3600))
        self.assertEqual(
            entry['local_anchor'],
            datetime.datetime(2006, 8, 28, 13, 30, 15))
        self.assertEqual(entry['ranges'][0]['start_wall'], WALL_UNIQUE)
        self.assertEqual(
            result.anchor_for('R1', 'A'),
            ANCHOR_UTC + datetime.timedelta(seconds=3600))
        self.assertEqual(len(result.ranges_for('R1', 'A')), 1)

    def test_merge_joins_touching_segments(self):
        batch = AnchorBatch(
            datetime.datetime(2006, 8, 28, 10, 0), 3600, 3)
        route = Route('R', [
            Order('A', '09:00-10:00', 'M1'),
            Order('B', '10:00-11:00', 'M1'),
            Order('C', '10:30-11:30', 'M1')], [batch])
        auditor, _ = make_auditor([route])
        merged = auditor.merged_device_ranges(auditor.run()[0])['M1']
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0].start_utc,
            datetime.datetime(2006, 8, 28, 13, 0, tzinfo=UTC))
        self.assertEqual(
            merged[0].end_utc,
            datetime.datetime(2006, 8, 28, 15, 30, tzinfo=UTC))

    def test_merge_keeps_disjoint_segments_apart(self):
        first = Order('A', '09:00-10:00', 'M1')
        batch = AnchorBatch(
            datetime.datetime(2006, 8, 28, 10, 0), 3600, 1)
        route = Route('R', [first], [batch])
        auditor, _ = make_auditor([route])
        booked = auditor.run()[0].order_results[0].ranges[0]
        later = booked.__class__(
            booked.start_utc + datetime.timedelta(hours=2),
            booked.end_utc + datetime.timedelta(hours=2),
            booked.start_local, booked.end_local,
            WALL_UNIQUE, WALL_UNIQUE, booked.range_flag)
        self.assertEqual(len(merge_ranges((booked, later))), 2)
        self.assertEqual(len(merge_ranges((booked,))), 1)


class testMemoization(unittest.TestCase):

    def test_shared_note_anchor_pair_parsed_once_across_routes(self):
        # Both routes carry the same note on the same anchor batch, so
        # each offset yields one (note, local anchor) pair that is parsed
        # once rather than once per route.
        batch = AnchorBatch(ANCHOR_UTC, 86400, 1)
        r1 = Route('R1', [Order('A', '09:00-10:00', 'M1')], [batch])
        r2 = Route('R2', [Order('B', '09:00-10:00', 'M2')], [batch])
        auditor, _ = make_auditor([r1, r2], offsets=(0, 3600))

        calls = []
        original = auditor._calendar.evalRanges

        def spy(note, source):
            calls.append((note, source))
            return original(note, source)

        auditor._calendar.evalRanges = spy
        auditor.run()
        # Two offsets, two distinct local anchors, one parse per pair.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(auditor._parse_cache), 2)

    def test_fold_collapses_distinct_utc_instants_to_one_wall_clock(self):
        # 05:30Z and 06:30Z on the fall-back day both read 01:30 local.
        batch = AnchorBatch(
            datetime.datetime(2006, 10, 29, 5, 30), 3600, 2)
        route = Route('R', [
            Order('A', '02:00-03:00', 'M1'),
            Order('B', '02:00-03:00', 'M1')], [batch])
        auditor, _ = make_auditor([route])
        self.assertEqual(
            auditor.distinct_local_anchors('R'),
            (datetime.datetime(2006, 10, 29, 1, 30),))


if __name__ == '__main__':
    unittest.main()
