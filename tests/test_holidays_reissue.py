#  holidays
#  --------
#  A fast, efficient Python library for generating country, province and state
#  specific sets of holidays on the fly. It aims to make determining whether a
#  specific date is a holiday as fast and flexible as possible.
#
#  Authors: Vacanza Team and individual contributors (see CONTRIBUTORS file)
#           dr-prodigy <dr.prodigy.github@gmail.com> (c) 2017-2023
#           ryanss <ryanssdev@icloud.com> (c) 2014-2017
#  Website: https://github.com/vacanza/holidays
#  License: MIT (see LICENSE file)

"""Local verification ledger for overseas quote re-issue booking and dispatch.

Scenario outline:
  * The exchange side uses XNAS and the custody side uses XNYS. Both composite
    calendars share the same NewYorkStockExchange (HolidayBase) source calendar
    but run on independent instances.
  * Two explicitly permitted announcement candidates A/B show an identically
    named ad-hoc closure on 2027-01-15. They cannot be treated separately until a
    distinguishing announcement arrives (a 2027-01-14 suspension and a
    2027-01-16 makeup workday for A; a 2027-01-20 suspension for B).
  * The same jobs are run under both announcement visibility orders; the signed
    slots must stay identical.
  * A minimal synthetic calendar additionally checks cross-midnight windows,
    fall-back fold endpoints, fee tie-breaks and the no-uniform-booking report.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest import TestCase

from holidays.calendars.gregorian import SAT, SUN
from holidays.financial.ny_stock_exchange import NewYorkStockExchange
from holidays.holiday_base import HolidayBase
from holidays_reissue import (
    AnnouncementCandidate,
    Booking,
    BookingLedger,
    CalendarSide,
    DispatchDecision,
    FeasibleInterval,
    IndistinguishableAnnouncementsError,
    ReissueJob,
    ScheduleAmendment,
    WindowSpec,
    compatible_booking,
    feasible_intervals,
    inspect_moment,
    plan_robust_bookings,
    run_dispatch,
)


class _MiniCalendar(HolidayBase):
    """Minimal synthetic calendar with fixed weekends and a few closure days."""

    market = "MINI"
    weekend = {SAT, SUN}
    start_year = 2000
    end_year = 2100

    def __init__(self, closed_days: dict[int, list[tuple[int, int, str]]] | None = None):
        self._closed_days = closed_days or {}
        super().__init__(expand=False)

    def _populate(self, year: int) -> None:
        if year < self.start_year or year > self.end_year:
            return
        self._year = year
        for month, day, name in self._closed_days.get(year, ()):
            self._add_holiday(name, month, day)


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


class SharedCalendarLedgerTest(TestCase):
    """Local ledger: two regions sharing the NYSE source calendar, under both
    announcement visibility orders."""

    def setUp(self) -> None:
        # Exchange side: the NASDAQ composite calendar, New York time, intraday.
        self.exchange = CalendarSide.from_financial_market(
            "XNAS",
            "America/New_York",
            WindowSpec(time(9, 30), time(16, 0)),
            label="NASDAQ",
        )
        # Custody side: the NYSE composite calendar (an independent instance
        # sharing the same HolidayBase class as XNAS), Chicago time, intraday.
        self.warehouse = CalendarSide.from_financial_market(
            "XNYS",
            "America/Chicago",
            WindowSpec(time(8, 0), time(19, 0)),
            label="NYSE-custody",
        )

        self.same_name_closure = date(2027, 1, 15)  # Same name in A and B.
        self.distinguishing_closure = date(2027, 1, 14)  # Suspended under A only.
        self.makeup_saturday = date(2027, 1, 16)  # Makeup workday under A only.
        self.candidate_b_closure = date(2027, 1, 20)  # Suspended under B only.

        self.candidate_a = AnnouncementCandidate(
            "A",
            announced_dates=frozenset(
                {self.same_name_closure, self.distinguishing_closure, self.makeup_saturday}
            ),
            amendments=ScheduleAmendment(
                suspended_dates=frozenset({self.same_name_closure, self.distinguishing_closure}),
                makeup_workdates=frozenset({self.makeup_saturday}),
            ),
        )
        self.candidate_b = AnnouncementCandidate(
            "B",
            announced_dates=frozenset({self.same_name_closure, self.candidate_b_closure}),
            amendments=ScheduleAmendment(
                suspended_dates=frozenset({self.same_name_closure, self.candidate_b_closure}),
            ),
        )
        self.candidates = [self.candidate_a, self.candidate_b]

        # Long-dated ledger: continuous segment 1..6 at fixed UTC instants, with
        # varied fees including a tie.
        self.bookings = [
            Booking(1, _utc(2027, 1, 4, 15, 0), _utc(2027, 1, 4, 21, 0), 10),
            Booking(2, _utc(2027, 1, 5, 15, 0), _utc(2027, 1, 5, 21, 0), 8),
            Booking(3, _utc(2027, 1, 6, 15, 0), _utc(2027, 1, 6, 21, 0), 8),
            Booking(4, _utc(2027, 1, 11, 15, 0), _utc(2027, 1, 11, 21, 0), 12),
            Booking(5, _utc(2027, 1, 12, 15, 0), _utc(2027, 1, 12, 21, 0), 6),
            Booking(6, _utc(2027, 1, 13, 15, 0), _utc(2027, 1, 13, 21, 0), 6),
        ]
        self.candidate_bookings = {
            "A": self.bookings,
            "B": self.bookings,
        }

        self.jobs = [
            ReissueJob(
                "J1",
                timedelta(hours=2),
                _utc(2027, 1, 4, 0),
                _utc(2027, 1, 22, 0),
            ),
            ReissueJob(
                "J2",
                timedelta(hours=3),
                _utc(2027, 1, 4, 0),
                _utc(2027, 1, 22, 0),
            ),
        ]

    def test_shared_base_independent_instances(self):
        self.assertIsNot(self.exchange.calendar, self.warehouse.calendar)
        # Both composite calendars share the exchange HolidayBase base class.
        self.assertIsInstance(self.exchange.calendar, NewYorkStockExchange)
        self.assertIsInstance(self.warehouse.calendar, NewYorkStockExchange)
        # Claim the 2027 version on XNAS first and record an operating date.
        self.exchange.holiday_source(date(2027, 1, 18))
        snapshot_years = set(self.exchange.calendar.years)
        mlk_name = self.exchange.holiday_source(date(2027, 1, 18))

        # Querying a different (future) year on the other composite calendar only
        # expands its own source calendar.
        self.warehouse.holiday_source(date(2031, 1, 1))
        self.assertIn(2031, self.warehouse.calendar.years)
        self.assertNotIn(2031, self.exchange.calendar.years)
        self.assertEqual(snapshot_years, set(self.exchange.calendar.years))
        # The claimed holiday version stays readable; its operating date is not
        # changed by the query on the other composite calendar.
        self.assertEqual(mlk_name, self.exchange.holiday_source(date(2027, 1, 18)))
        self.assertEqual("Martin Luther King Jr. Day", mlk_name)

    def test_same_named_closure_indistinguishable_before_source_announcement(self):
        # The identically named closure shuts both sides under both candidates;
        # neither source calendar originally contains that date, so it is an
        # ad-hoc candidate change.
        for side in (self.exchange, self.warehouse):
            self.assertIsNone(side.holiday_source(self.same_name_closure))
            for candidate in self.candidates:
                self.assertFalse(side.is_open_day(self.same_name_closure, candidate.amendments))

    def test_plan_robust_bookings_min_fee_and_tie_break(self):
        total_fee, selected = plan_robust_bookings(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            self.candidate_bookings,
        )
        selected_numbers = tuple(booking.booking_no for booking in selected)
        # Two jobs, one booking each, feasible under both A and B.
        self.assertEqual(2, len(selected))
        for candidate in self.candidates:
            assignment = compatible_booking(
                self.jobs, self.exchange, self.warehouse, candidate, selected
            )
            self.assertIsNotNone(assignment)
            self.assertEqual({"J1", "J2"}, set(assignment))
        # Minimum fee; on the fee tie (5/6 both cost 6) the earlier number wins.
        self.assertEqual(12, total_fee)
        self.assertEqual((5, 6), selected_numbers)

    def test_dispatch_both_reveal_orders_keep_signed_slots_identical(self):
        _, selected = plan_robust_bookings(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            self.candidate_bookings,
        )

        # Visibility order one: the identically named closure first (both
        # candidates stay consistent), then the distinguishing announcement.
        ledger_one = BookingLedger(selected)
        decisions_one = run_dispatch(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            ledger_one,
            [
                frozenset({self.same_name_closure}),
                frozenset({self.distinguishing_closure, self.makeup_saturday}),
            ],
        )

        # Visibility order two: the distinguishing announcement arrives at once,
        # so candidate A is identified immediately.
        ledger_two = BookingLedger(selected)
        decisions_two = run_dispatch(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            ledger_two,
            [
                frozenset(
                    {
                        self.same_name_closure,
                        self.distinguishing_closure,
                        self.makeup_saturday,
                    }
                )
            ],
        )

        def slot_map(decisions):
            return {
                decision.job_id: (
                    decision.booking.booking_no,
                    decision.start_utc,
                    decision.end_utc,
                )
                for decision in decisions
            }

        # Bookings and UTC instants assigned are identical across both orders
        # (signed slots cannot be changed).
        self.assertEqual(slot_map(decisions_one), slot_map(decisions_two))
        self.assertTrue(
            all(
                decision.used_candidate_code.startswith("UNRESOLVED:")
                for decision in decisions_one
            )
        )
        self.assertEqual({"A"}, {decision.used_candidate_code for decision in decisions_two})
        # Every decision carries both sides' local dates and source evidence.
        for decision in decisions_one:
            self.assertEqual(2, len(decision.evidence_sources))
            self.assertEqual(2, len(decision.local_dates))
            for side_code, local_day, source in decision.evidence_sources:
                self.assertIn(side_code, {"XNAS", "XNYS"})
                self.assertIsInstance(date.fromisoformat(local_day), date)
                self.assertTrue(source is None or isinstance(source, str))

    def test_dispatch_continuous_duration_inside_both_real_windows(self):
        _, selected = plan_robust_bookings(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            self.candidate_bookings,
        )
        ledger = BookingLedger(selected)
        decisions = run_dispatch(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            ledger,
            [
                frozenset(
                    {
                        self.same_name_closure,
                        self.distinguishing_closure,
                        self.makeup_saturday,
                    }
                )
            ],
        )
        for decision in decisions:
            job = next(item for item in self.jobs if item.job_id == decision.job_id)
            for side in (self.exchange, self.warehouse):
                day = side.local_date(decision.start_utc)
                start, end = side._window_bounds(day)
                # The work interval sits fully inside that side's real window
                # (the window may extend into the next day).
                self.assertLessEqual(start, decision.start_utc)
                self.assertGreaterEqual(end, decision.end_utc)
                self.assertTrue(side.is_open_day(day, self.candidate_a.amendments))
            self.assertEqual(job.duration, decision.end_utc - decision.start_utc)

    def test_inspect_moment_reports_local_dates_and_sources(self):
        moment = _utc(2027, 1, 4, 16, 0)
        report = inspect_moment(moment, (self.exchange, self.warehouse), self.candidates)
        self.assertEqual(moment.isoformat(), report["moment_utc"])
        self.assertEqual({"XNAS", "XNYS"}, set(report["sides"]))
        nas = report["sides"]["XNAS"]
        self.assertEqual("2027-01-04", nas["local_date"])
        self.assertIsNone(nas["holiday_source"])
        self.assertEqual({"A": True, "B": True}, nas["open_by_candidate"])
        # On the identically named closure day both candidates show shut.
        closed_moment = _utc(2027, 1, 15, 16, 0)
        closed_report = inspect_moment(
            closed_moment, (self.exchange, self.warehouse), self.candidates
        )
        for side_report in closed_report["sides"].values():
            self.assertEqual({"A": False, "B": False}, side_report["open_by_candidate"])

    def test_no_uniform_booking_reports_indistinguishable_pair_and_jobs(self):
        # Each candidate only gets its own exclusive booking (no common number),
        # so a uniform booking cannot exist.
        only_a = [Booking(1, _utc(2027, 1, 4, 15), _utc(2027, 1, 4, 21), 5)]
        only_b = [Booking(2, _utc(2027, 1, 5, 15), _utc(2027, 1, 5, 21), 5)]
        with self.assertRaises(IndistinguishableAnnouncementsError) as context:
            plan_robust_bookings(
                self.jobs,
                self.exchange,
                self.warehouse,
                self.candidates,
                {"A": only_a, "B": only_b},
            )
        details = context.exception.details
        self.assertEqual(["A", "B"], details["candidate_codes"])
        pairs = details["undifferentiated_pairs"]
        self.assertIn(("A", "B", self.same_name_closure), pairs)
        self.assertEqual(["J1", "J2"], details["unbookable_jobs"])

    def test_dispatch_resolves_candidate_b_from_distinguishing_announcement(self):
        _, selected = plan_robust_bookings(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            self.candidate_bookings,
        )
        ledger = BookingLedger(selected)
        decisions = run_dispatch(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            ledger,
            [frozenset({self.same_name_closure, self.candidate_b_closure})],
        )
        # The single announcement includes 1/20, predicted only by B: A is ruled
        # out and every job is fulfilled under B.
        self.assertEqual({"B"}, {decision.used_candidate_code for decision in decisions})
        self.assertEqual(2, len({decision.booking.booking_no for decision in decisions}))

    def test_dispatch_blocked_when_announcements_never_distinguish_candidates(self):
        # The two bookings land on dates where the candidates differ: 1/14 is shut
        # under A only and 1/20 under B only. The identically named 1/15 closure
        # never distinguishes them, so no booking works under both at the
        # decision point.
        only_b_open = [Booking(1, _utc(2027, 1, 14, 15), _utc(2027, 1, 14, 21), 5)]
        only_a_open = [Booking(2, _utc(2027, 1, 20, 15), _utc(2027, 1, 20, 21), 5)]
        # Build a ledger holding both candidate-specific bookings directly (the
        # robust planner itself would reject this combination).
        ledger = BookingLedger(only_b_open + only_a_open)
        with self.assertRaises(IndistinguishableAnnouncementsError) as context:
            run_dispatch(
                self.jobs,
                self.exchange,
                self.warehouse,
                self.candidates,
                ledger,
                [frozenset({self.same_name_closure})],
            )
        details = context.exception.details
        self.assertEqual(1, details["sequence"])
        self.assertEqual(["A", "B"], details["candidate_codes"])
        self.assertIn(("A", "B", self.same_name_closure), details["undifferentiated_pairs"])
        self.assertEqual(["J1", "J2"], details["unbookable_jobs"])
        # Nothing is signed while the decision point is stuck.
        self.assertEqual((), ledger.signed_numbers)

    def test_long_ledger_only_local_segments_touch_announcement_years(self):
        # A long-dated ledger across several months; most entries do not
        # intersect this batch's time bounds.
        long_bookings: list[Booking] = []
        number = 1
        for month in range(1, 6):
            for day in (4, 5, 6):
                long_bookings.append(
                    Booking(
                        number,
                        _utc(2027, month, day, 15),
                        _utc(2027, month, day, 21),
                        number,
                    )
                )
                number += 1
        total_fee, selected = plan_robust_bookings(
            self.jobs,
            self.exchange,
            self.warehouse,
            self.candidates,
            {"A": long_bookings, "B": long_bookings},
        )
        selected_numbers = [booking.booking_no for booking in selected]
        # Only the January segment is selected (job bounds end on 1/22); later
        # monthly segments never enter the search.
        self.assertTrue(all(booking.start_utc.month == 1 for booking in selected))
        self.assertEqual(2, len(selected_numbers))
        self.assertEqual(total_fee, sum(booking.fee for booking in selected))
        # Planning never reshuffles far-dated segments: numbering stays continuous.
        BookingLedger(long_bookings)


class WindowEdgeTest(TestCase):
    """Cross-midnight windows and DST fall-back repeated-hour endpoints."""

    def setUp(self) -> None:
        calendar = _MiniCalendar(
            {2026: [(11, 2, "Fixed Closure")], 2027: [(1, 4, "New Year Brief Closure")]}
        )
        self.night_side = CalendarSide(
            "NIGHT",
            "America/New_York",
            WindowSpec(time(22, 0), time(6, 0), fold=1),
            calendar,
        )
        self.day_side = CalendarSide(
            "DAY",
            "UTC",
            WindowSpec(time(8, 0), time(20, 0)),
            _MiniCalendar(),
        )

    def test_cross_midnight_window_maps_to_next_local_day(self):
        windows = self.night_side.open_windows_utc([date(2027, 1, 5)], ScheduleAmendment())
        self.assertEqual(1, len(windows))
        start, end, local_day = windows[0]
        self.assertEqual(date(2027, 1, 5), local_day)
        # New York in January is UTC-5: local 22:00 maps to 03:00 UTC the next
        # day; the cross-midnight close at local 06:00 maps to 11:00 UTC.
        self.assertEqual(_utc(2027, 1, 6, 3, 0), start)
        self.assertEqual(_utc(2027, 1, 6, 11, 0), end)

    def test_fallback_repeated_local_hour_uses_marked_fold_endpoint(self):
        # North American fall-back on 2026-11-01 repeats local 01:xx. fold=1
        # picks the second (standard-time) occurrence.
        fold_one = CalendarSide(
            "F1",
            "America/New_York",
            WindowSpec(time(1, 0), time(3, 0), fold=1),
            _MiniCalendar(),
        )
        fold_zero = CalendarSide(
            "F0",
            "America/New_York",
            WindowSpec(time(1, 0), time(3, 0), fold=0),
            _MiniCalendar(),
        )
        start_one, end_one = fold_one._window_bounds(date(2026, 11, 1))
        start_zero, end_zero = fold_zero._window_bounds(date(2026, 11, 1))
        self.assertEqual(_utc(2026, 11, 1, 6, 0), start_one)  # 01:00 EST (UTC-5)
        self.assertEqual(_utc(2026, 11, 1, 5, 0), start_zero)  # 01:00 EDT (UTC-4)
        # 03:00 exists only in standard time after the fall-back, so both marked
        # closes equal 08:00 UTC; fold only changes the repeated 01:00 endpoint.
        self.assertEqual(_utc(2026, 11, 1, 8, 0), end_one)
        self.assertEqual(_utc(2026, 11, 1, 8, 0), end_zero)

    def test_job_spanning_midnight_must_fit_continuously(self):
        job = ReissueJob(
            "N1",
            timedelta(hours=2),
            _utc(2027, 1, 1, 0),
            _utc(2027, 1, 31, 0),
        )
        # The local 1/5 night window (UTC 1/6 03:00-11:00) intersects the UTC day
        # window (08:00-20:00) over UTC 1/6 08:00-11:00; a 2-hour job therefore
        # has the start range 08:00-09:00.
        bookings = [Booking(7, _utc(2027, 1, 6, 6), _utc(2027, 1, 6, 14), 3)]
        candidate = AnnouncementCandidate("C")
        intervals = feasible_intervals(job, self.night_side, self.day_side, candidate, bookings)
        self.assertEqual({7}, set(intervals))
        interval = intervals[7][0]
        self.assertEqual(_utc(2027, 1, 6, 8, 0), interval.lower)
        self.assertEqual(_utc(2027, 1, 6, 9, 0), interval.upper)

    def test_amendment_suspension_and_makeup_change_open_days(self):
        closure = date(2027, 1, 5)  # Tuesday, originally open.
        saturday = date(2027, 1, 9)  # Saturday, originally closed.
        candidate = AnnouncementCandidate(
            "C",
            announced_dates=frozenset({closure, saturday}),
            amendments=ScheduleAmendment(
                suspended_dates=frozenset({closure}),
                makeup_workdates=frozenset({saturday}),
            ),
        )
        self.assertTrue(self.day_side.is_open_day(closure, ScheduleAmendment()))
        self.assertFalse(self.day_side.is_open_day(closure, candidate.amendments))
        self.assertFalse(self.day_side.is_open_day(saturday, ScheduleAmendment()))
        self.assertTrue(self.day_side.is_open_day(saturday, candidate.amendments))


class MatchingAndLedgerTest(TestCase):
    """One booking per job, one run per job, fee tie-break and immutable ledger."""

    def setUp(self) -> None:
        self.side = CalendarSide(
            "S",
            "UTC",
            WindowSpec(time(0, 0), time(23, 59)),
            _MiniCalendar(),
        )
        self.candidate = AnnouncementCandidate("C")
        self.jobs = [
            ReissueJob("J1", timedelta(hours=1), _utc(2027, 1, 4, 0), _utc(2027, 1, 8, 0)),
            ReissueJob("J2", timedelta(hours=1), _utc(2027, 1, 4, 0), _utc(2027, 1, 8, 0)),
        ]

    def test_one_booking_cannot_serve_two_jobs(self):
        bookings = [Booking(1, _utc(2027, 1, 4, 0), _utc(2027, 1, 4, 12), 1)]
        self.assertIsNone(
            compatible_booking(self.jobs, self.side, self.side, self.candidate, bookings)
        )
        two_bookings = [
            Booking(1, _utc(2027, 1, 4, 0), _utc(2027, 1, 4, 12), 1),
            Booking(2, _utc(2027, 1, 5, 0), _utc(2027, 1, 5, 12), 1),
        ]
        assignment = compatible_booking(
            self.jobs, self.side, self.side, self.candidate, two_bookings
        )
        self.assertEqual({"J1", "J2"}, set(assignment))

    def test_min_fee_prefers_cheapest_and_tie_uses_booking_order(self):
        bookings = [
            Booking(1, _utc(2027, 1, 4, 0), _utc(2027, 1, 4, 12), 9),
            Booking(2, _utc(2027, 1, 5, 0), _utc(2027, 1, 5, 12), 5),
            Booking(3, _utc(2027, 1, 6, 0), _utc(2027, 1, 6, 12), 5),
            Booking(4, _utc(2027, 1, 7, 0), _utc(2027, 1, 7, 12), 5),
        ]
        total_fee, selected = plan_robust_bookings(
            self.jobs, self.side, self.side, [self.candidate], {"C": bookings}
        )
        self.assertEqual(10, total_fee)
        self.assertEqual((2, 3), tuple(booking.booking_no for booking in selected))

    def test_ledger_rejects_reassigning_signed_slot(self):
        bookings = [
            Booking(1, _utc(2027, 1, 4, 0), _utc(2027, 1, 4, 12), 1),
            Booking(2, _utc(2027, 1, 5, 0), _utc(2027, 1, 5, 12), 1),
        ]
        ledger = BookingLedger(bookings)

        first = DispatchDecision(
            sequence=1,
            job_id="J1",
            booking=bookings[0],
            start_utc=bookings[0].start_utc,
            end_utc=bookings[0].start_utc + timedelta(hours=1),
            used_candidate_code="C",
            evidence_sources=(),
            local_dates=(),
        )
        ledger.sign(first)
        conflicting = replace(first, job_id="J2")
        with self.assertRaises(ValueError):
            ledger.sign(conflicting)
        # Replaying the same decision is allowed (idempotent); the signed instant
        # is never rewritten later.
        ledger.sign(first)
        self.assertEqual((1,), ledger.signed_numbers)

    def test_ledger_requires_continuous_number_segment(self):
        with self.assertRaises(ValueError):
            BookingLedger(
                [
                    Booking(1, _utc(2027, 1, 4, 0), _utc(2027, 1, 4, 12), 1),
                    Booking(3, _utc(2027, 1, 5, 0), _utc(2027, 1, 5, 12), 1),
                ]
            )

    def test_feasible_interval_type(self):
        bookings = [Booking(1, _utc(2027, 1, 4, 2), _utc(2027, 1, 4, 10), 1)]
        intervals = feasible_intervals(
            self.jobs[0], self.side, self.side, self.candidate, bookings
        )
        self.assertIsInstance(intervals[1][0], FeasibleInterval)
        self.assertEqual(_utc(2027, 1, 4, 2), intervals[1][0].lower)
        self.assertEqual(_utc(2027, 1, 4, 9), intervals[1][0].upper)
