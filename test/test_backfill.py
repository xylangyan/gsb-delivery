#!/usr/bin/python3

#    Library for calculating location of the sun

#    Copyright Brandon Stafford
#
#    This file is part of Pysolar.
#
#    Pysolar is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#
#    Pysolar is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with Pysolar. If not, see <http://www.gnu.org/licenses/>.

import datetime
import math
import unittest
from unittest import mock

from pysolar import backfill, solar
from pysolar.backfill import ChangeSegment, StationProfile, reevaluate_admission

UTC = datetime.timezone.utc


def profile(horizon_points, effective_from=None, label=None,
            latitude=39.742476, longitude=-105.1786, elevation=1830.14,
            temperature=284.15, pressure=82000.0):
    if effective_from is None:
        effective_from = datetime.datetime(2000, 1, 1, tzinfo=UTC)
    return StationProfile(effective_from, latitude, longitude, elevation,
                          temperature, pressure, horizon_points, label=label)


class TestHorizonInterpolation(unittest.TestCase):

    def test_linear_interpolation_between_measured_points(self):
        station = profile([(0.0, 0.0), (90.0, 10.0),
                           (180.0, 0.0), (270.0, 10.0)])
        self.assertEqual(station.horizon_height(45.0), 5.0)
        self.assertEqual(station.horizon_height(135.0), 5.0)

    def test_interpolation_wraps_through_north(self):
        station = profile([(359.0, 0.0), (1.0, 10.0)])
        self.assertEqual(station.horizon_height(359.5), 2.5)
        self.assertEqual(station.horizon_height(0.0), 5.0)
        self.assertEqual(station.horizon_height(0.5), 7.5)
        self.assertEqual(station.horizon_height(359.0), 0.0)
        self.assertEqual(station.horizon_height(1.0), 10.0)

    def test_wraparound_with_unsorted_input(self):
        station = profile([(1.0, 10.0), (359.0, 0.0)])
        self.assertEqual(station.horizon_height(0.0), 5.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            profile([(0.0, 1.0)])
        with self.assertRaises(ValueError):
            profile([(0.0, 1.0), (0.0, 2.0)])
        with self.assertRaises(ValueError):
            profile([(-1.0, 1.0), (10.0, 2.0)])
        with self.assertRaises(ValueError):
            profile([(0.0, 1.0), (360.0, 2.0)])
        with self.assertRaises(ValueError):
            StationProfile(
                datetime.datetime(2020, 1, 1), 0.0, 0.0, 0.0,
                288.15, 101325.0, [(0.0, 0.0), (90.0, 0.0)])


class TestEqualityIsBlocked(unittest.TestCase):

    def test_sun_exactly_on_horizon_is_blocked(self):
        when = datetime.datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
        station = profile([(0.0, 0.0), (90.0, 0.0),
                           (180.0, 0.0), (270.0, 0.0)])
        azimuth, altitude = solar.get_position(
            station.latitude_deg, station.longitude_deg, when,
            station.elevation, station.temperature, station.pressure)
        station = profile([
            (azimuth - 1.0, altitude + 5.0),
            (azimuth, altitude),
            (azimuth + 1.0, altitude + 5.0),
        ])
        self.assertEqual(station.horizon_height(azimuth), altitude)
        self.assertFalse(station.admitted(when))

    def test_just_above_horizon_is_admitted(self):
        when = datetime.datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
        station = profile([(0.0, 0.0), (90.0, 0.0),
                           (180.0, 0.0), (270.0, 0.0)])
        azimuth, altitude = solar.get_position(
            station.latitude_deg, station.longitude_deg, when,
            station.elevation, station.temperature, station.pressure)
        station = profile([
            (azimuth - 1.0, altitude - 0.01),
            (azimuth + 1.0, altitude - 0.01),
        ])
        self.assertTrue(station.admitted(when))


class _FakeSolar:
    """Deterministic solar stand-in respecting the real angular-rate bounds.

    The azimuth stays fixed inside the horizon slit while the altitude
    oscillates around zero with a peak rate below the sidereal hour-angle
    rate, so the Lipschitz envelopes used for crossing-free certification
    remain valid while the trajectory crosses the slit horizon repeatedly.
    """

    period_seconds = 1.0
    amplitude = 0.0006
    fixed_azimuth = 180.0

    def __init__(self, station, altitude0=0.0, quantize=None):
        self.station = station
        self.altitude0 = altitude0
        self.quantize = quantize
        self.epoch = None

    def _angles(self, when):
        if self.epoch is None:
            self.epoch = when
        seconds = (when - self.epoch).total_seconds()
        topo = self.altitude0 + self.amplitude * math.sin(
            2.0 * math.pi * seconds / self.period_seconds)
        if self.quantize is not None:
            topo = float(int(topo / self.quantize)) * self.quantize
        return topo, self.fixed_azimuth

    def get_topocentric_position(self, latitude, longitude, when, elevation=0):
        topo, azimuth = self._angles(when)
        return topo, azimuth

    def get_topocentric_elevation_angle(self, latitude, declination, hour_angle):
        return declination

    def get_refraction_correction(self, pressure, temperature, topo):
        return 0.0

    def get_topocentric_azimuth_angle(self, hour_angle, latitude, declination):
        return hour_angle


class _ConstantHighSun(_FakeSolar):
    """Sun fixed above every measured horizon point: no crossings at all."""

    def _angles(self, when):
        return 85.0, self.fixed_azimuth


def fake_admitted(station, when, fake_class=_FakeSolar):
    fake = fake_class(station)
    topo, azimuth = fake._angles(when)
    return topo > station.horizon_height(azimuth)


class TestSearchAgainstBruteForce(unittest.TestCase):

    def setUp(self):
        self.start = datetime.datetime(2024, 3, 31, 23, 0, tzinfo=UTC)
        self.step_us = 100
        self.count = 20000
        # Tall wall with a narrow slit around azimuth 180 degrees; the slit
        # floor sits inside the fake sun's oscillation band so the beam
        # enters and leaves the slit repeatedly.
        self.station = profile([
            (0.0, 80.0), (170.0, 80.0), (176.0, -0.001),
            (184.0, -0.001), (190.0, 80.0), (359.999, 80.0),
        ], label="v1")

    def _brute_force(self):
        step = datetime.timedelta(microseconds=self.step_us)
        return [
            fake_admitted(self.station, self.start + step * index)
            for index in range(self.count)
        ]

    def test_matches_brute_force_with_narrow_slit(self):
        expected = self._brute_force()
        self.assertTrue(any(expected))
        with mock.patch.object(backfill, "solar", _FakeSolar(self.station)):
            segments = reevaluate_admission(
                self.start, self.step_us, self.count,
                [(0, self.count, False)], [self.station])
        admitted_indices = set()
        for segment in segments:
            self.assertEqual(segment.action, "backfill")
            admitted_indices.update(
                range(segment.start_index, segment.end_index + 1))
        self.assertEqual(
            admitted_indices,
            {index for index, value in enumerate(expected) if value})

    def test_blocked_neighbors_are_not_backfilled(self):
        expected = self._brute_force()
        with mock.patch.object(backfill, "solar", _FakeSolar(self.station)):
            segments = reevaluate_admission(
                self.start, self.step_us, self.count,
                [(0, self.count, False)], [self.station])
        for segment in segments:
            if segment.start_index > 0:
                self.assertFalse(expected[segment.start_index - 1])
            if segment.end_index + 1 < self.count:
                self.assertFalse(expected[segment.end_index + 1])
            self.assertEqual(
                segment.start_time,
                self.start + datetime.timedelta(
                    microseconds=self.step_us * segment.start_index))
            self.assertEqual(
                segment.end_time,
                self.start + datetime.timedelta(
                    microseconds=self.step_us * segment.end_index))

    def test_withdraw_runs(self):
        expected = self._brute_force()
        with mock.patch.object(backfill, "solar", _FakeSolar(self.station)):
            segments = reevaluate_admission(
                self.start, self.step_us, self.count,
                [(0, self.count, True)], [self.station])
        withdrawn = set()
        for segment in segments:
            self.assertEqual(segment.action, "withdraw")
            withdrawn.update(
                range(segment.start_index, segment.end_index + 1))
        self.assertEqual(
            withdrawn,
            {index for index, value in enumerate(expected) if not value})

    def test_mixed_original_intervals_merge_into_maximal_runs(self):
        expected = self._brute_force()
        old_intervals = [
            (0, self.count // 3, True),
            (self.count // 3, 2 * self.count // 3, False),
            (2 * self.count // 3, self.count, True),
        ]
        with mock.patch.object(backfill, "solar", _FakeSolar(self.station)):
            segments = reevaluate_admission(
                self.start, self.step_us, self.count,
                old_intervals, [self.station])
        for segment in segments:
            for index in range(segment.start_index, segment.end_index + 1):
                if segment.action == "backfill":
                    self.assertFalse(
                        old_admitted_at(old_intervals, index))
                    self.assertTrue(expected[index])
                else:
                    self.assertTrue(
                        old_admitted_at(old_intervals, index))
                    self.assertFalse(expected[index])

    def test_identical_float_coordinates_share_verdict(self):
        plateau_station = profile([
            (0.0, 80.0), (179.0, 80.0), (179.5, -0.001),
            (180.5, -0.001), (181.0, 80.0), (359.999, 80.0),
        ], label="plateau")
        step = datetime.timedelta(microseconds=self.step_us)
        fake = _FakeSolar(plateau_station, quantize=2e-4)
        expected = []
        for index in range(self.count):
            topo, azimuth = fake._angles(self.start + step * index)
            expected.append(topo > plateau_station.horizon_height(azimuth))
        self.assertGreater(
            sum(1 for index in range(1, self.count)
                if fake._angles(self.start + step * index)
                == fake._angles(self.start + step * (index - 1))),
            self.count // 2)
        self.assertTrue(any(expected))
        with mock.patch.object(backfill, "solar",
                               _FakeSolar(plateau_station, quantize=2e-4)):
            segments = reevaluate_admission(
                self.start, self.step_us, self.count,
                [(0, self.count, False)], [plateau_station])
        admitted = set()
        for segment in segments:
            admitted.update(range(segment.start_index, segment.end_index + 1))
        self.assertEqual(
            admitted,
            {index for index, value in enumerate(expected) if value})

    def test_large_archive_is_not_resampled_point_by_point(self):
        count = 5_000_000
        with mock.patch.object(backfill, "solar",
                               _ConstantHighSun(self.station)):
            evaluator = backfill._Evaluator(
                datetime.timedelta(microseconds=self.step_us),
                lambda index: self.start + datetime.timedelta(
                    microseconds=self.step_us * index),
                [self.station.effective_from], [self.station])
            intervals = evaluator.resolve_block(0, count)
        self.assertLess(evaluator.calls, 50)
        self.assertEqual(intervals, [(0, count, True)])


def old_admitted_at(intervals, index):
    for start, end, admitted in intervals:
        if start <= index < end:
            return admitted
    raise IndexError(index)


class TestProfileSwitching(unittest.TestCase):

    def test_switch_across_utc_calendar_boundary(self):
        switch = datetime.datetime(2024, 4, 1, 0, 0, tzinfo=UTC)
        start = switch - datetime.timedelta(minutes=30)
        step_us = 60_000_000
        count = 60
        blocking = profile([
            (0.0, 85.0), (90.0, 85.0), (180.0, 85.0), (270.0, 85.0),
        ], effective_from=datetime.datetime(2000, 1, 1, tzinfo=UTC),
            label="old-wall")
        open_sky = profile([
            (0.0, -90.0), (90.0, -90.0),
            (180.0, -90.0), (270.0, -90.0),
        ], effective_from=switch, label="new-open")
        segments = reevaluate_admission(
            start, step_us, count, [(0, count, True)],
            [blocking, open_sky])
        switch_index = (switch - start) // datetime.timedelta(
            microseconds=step_us)
        self.assertTrue(segments)
        withdraw = next(
            segment for segment in segments if segment.action == "withdraw")
        self.assertEqual(withdraw.start_index, 0)
        self.assertEqual(withdraw.end_index, switch_index - 1)
        self.assertEqual(withdraw.profile_versions, ["old-wall"])
        self.assertEqual(withdraw.effective_points, [])
        # Samples at and after the switch follow the new profile and are
        # admitted whenever the sun is above the open horizon.
        step = datetime.timedelta(microseconds=step_us)
        for segment in segments:
            if segment.action == "backfill":
                self.assertGreaterEqual(segment.start_index, switch_index)
                for index in (segment.start_index, segment.end_index):
                    self.assertTrue(
                        open_sky.admitted(start + step * index))

    def test_segment_reports_all_covering_versions_and_switch_points(self):
        switch = datetime.datetime(2024, 4, 1, 0, 0, tzinfo=UTC)
        start = switch - datetime.timedelta(minutes=10)
        step_us = 60_000_000
        count = 20
        wall = profile([
            (0.0, 85.0), (90.0, 85.0), (180.0, 85.0), (270.0, 85.0),
        ], effective_from=datetime.datetime(2000, 1, 1, tzinfo=UTC),
            label="v1")
        open_sky = profile([
            (0.0, -90.0), (90.0, -90.0),
            (180.0, -90.0), (270.0, -90.0),
        ], effective_from=switch, label="v2")
        segments = reevaluate_admission(
            start, step_us, count, [(0, count, True)], [wall, open_sky])
        touched = [
            segment for segment in segments
            if any(index < (switch - start) // datetime.timedelta(
                    microseconds=step_us)
                   for index in (segment.start_index, segment.end_index))
            and segment.end_index >= (switch - start) // datetime.timedelta(
                    microseconds=step_us)]
        # No change segment may straddle the boundary with a single profile;
        # every reported version list must correspond to the actual instants.
        for segment in segments:
            self.assertTrue(segment.profile_versions)
            for point in segment.effective_points:
                self.assertGreaterEqual(point, segment.start_time)
                self.assertLessEqual(point, segment.end_time)


class TestInputValidation(unittest.TestCase):

    def setUp(self):
        self.start = datetime.datetime(2024, 1, 1, tzinfo=UTC)
        self.station = profile([(0.0, 0.0), (90.0, 0.0),
                                (180.0, 0.0), (270.0, 0.0)])

    def test_basic_types(self):
        with self.assertRaises(TypeError):
            reevaluate_admission(
                self.start, 1.5, 10, [(0, 10, False)], [self.station])
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 0, 10, [(0, 10, False)], [self.station])
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 10, -1, [(0, 0, False)], [self.station])

    def test_naive_datetime_rejected(self):
        naive = datetime.datetime(2024, 1, 1)
        with self.assertRaises(ValueError):
            reevaluate_admission(
                naive, 10, 10, [(0, 10, False)], [self.station])

    def test_interval_coverage(self):
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 10, 10, [(0, 9, False)], [self.station])
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 10, 10, [(0, 5, False), (6, 10, False)],
                [self.station])
        with self.assertRaises(TypeError):
            reevaluate_admission(
                self.start, 10, 3, [False, False, True], [self.station])

    def test_profile_rules(self):
        late = profile(
            [(0.0, 0.0), (90.0, 0.0)],
            effective_from=datetime.datetime(2025, 1, 1, tzinfo=UTC))
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 10, 10, [(0, 10, False)], [late])
        twin = profile(
            [(0.0, 0.0), (90.0, 0.0)],
            effective_from=datetime.datetime(2000, 1, 1, tzinfo=UTC),
            label="twin")
        with self.assertRaises(ValueError):
            reevaluate_admission(
                self.start, 10, 10, [(0, 10, False)],
                [self.station, twin])

    def test_empty_run(self):
        self.assertEqual(
            reevaluate_admission(
                self.start, 10, 0, [(0, 0, False)], [self.station]),
            [])


class TestEndToEndRealSolar(unittest.TestCase):
    """Brute-force comparison using the unmodified solar.get_position."""

    STATION_KWARGS = dict(
        latitude=39.742476, longitude=-105.1786, elevation=1830.14,
        temperature=284.15, pressure=82000.0)

    def _brute_force_runs(self, start, step_us, count, station):
        step = datetime.timedelta(microseconds=step_us)
        states = [
            station.admitted(start + step * index)
            for index in range(count)
        ]
        runs = []
        index = 0
        while index < count:
            end = index + 1
            while end < count and states[end] == states[index]:
                end += 1
            runs.append((index, end, bool(states[index])))
            index = end
        return runs, states

    def _assert_matches_brute_force(self, start, step_us, count,
                                    horizon_points, old_intervals):
        station = profile(horizon_points, label="v1", **self.STATION_KWARGS)
        expected_runs, states = self._brute_force_runs(
            start, step_us, count, station)
        segments = reevaluate_admission(
            start, step_us, count, old_intervals, [station])
        expected_changes = set()
        old_lookup = [
            admitted for interval_start, interval_end, admitted
            in old_intervals
            for _ in range(interval_end - interval_start)
        ]
        for index in range(count):
            if old_lookup[index] != states[index]:
                expected_changes.add(index)
        actual_changes = set()
        for segment in segments:
            actual_changes.update(
                range(segment.start_index, segment.end_index + 1))
        self.assertEqual(actual_changes, expected_changes)
        for segment in segments:
            self.assertEqual(
                segment.action,
                "backfill" if states[segment.start_index] else "withdraw")
        return segments, expected_runs

    def test_day_long_minute_cadence(self):
        start = datetime.datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
        horizon = [(0.0, 2.0), (90.0, 5.0), (180.0, 3.0), (270.0, 1.0)]
        self._assert_matches_brute_force(
            start, 60_000_000, 24 * 60, horizon, [(0, 24 * 60, False)])

    def test_microsecond_cadence_with_repeated_coordinates(self):
        start = datetime.datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
        horizon = [(0.0, 0.0), (90.0, 0.0),
                   (180.0, 0.0), (270.0, 0.0)]
        segments, _ = self._assert_matches_brute_force(
            start, 1, 50_000, horizon, [(0, 50_000, False)])
        # With microsecond spacing many samples must share exact coordinates.
        step = datetime.timedelta(microseconds=1)
        coordinates = [
            solar.get_position(
                self.STATION_KWARGS["latitude"],
                self.STATION_KWARGS["longitude"], start + step * index,
                self.STATION_KWARGS["elevation"],
                self.STATION_KWARGS["temperature"],
                self.STATION_KWARGS["pressure"])
            for index in range(1000)
        ]
        repeated = sum(
            coordinates[index] == coordinates[index - 1]
            for index in range(1, len(coordinates)))
        self.assertGreater(repeated, 900)
        self.assertTrue(segments)

    def test_switch_exactly_on_a_sample_instant(self):
        switch = datetime.datetime(2024, 4, 1, 0, 0, tzinfo=UTC)
        start = switch - datetime.timedelta(minutes=20)
        step_us = 60_000_000
        count = 40
        wall = profile([
            (0.0, 85.0), (90.0, 85.0), (180.0, 85.0), (270.0, 85.0),
        ], effective_from=datetime.datetime(2000, 1, 1, tzinfo=UTC),
            label="v1", **self.STATION_KWARGS)
        open_sky = profile([
            (0.0, -90.0), (90.0, -90.0),
            (180.0, -90.0), (270.0, -90.0),
        ], effective_from=switch, label="v2", **self.STATION_KWARGS)
        segments = reevaluate_admission(
            start, step_us, count, [(0, count, False)], [wall, open_sky])
        switch_index = (switch - start) // datetime.timedelta(
            microseconds=step_us)
        step = datetime.timedelta(microseconds=step_us)
        for segment in segments:
            self.assertEqual(segment.action, "backfill")
            self.assertGreaterEqual(segment.start_index, switch_index)
            for index in (segment.start_index, segment.end_index):
                self.assertTrue(open_sky.admitted(start + step * index))
            self.assertIn("v2", segment.profile_versions)

    def test_math_and_numpy_backends_agree(self):
        from pysolar import numeric
        start = datetime.datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
        horizon = [(0.0, 1.0), (90.0, 4.0), (180.0, 2.0), (270.0, 0.5)]
        count = 720
        old_intervals = [(0, count, False)]
        try:
            numeric.use_math()
            math_segments = reevaluate_admission(
                start, 60_000_000, count, old_intervals,
                [profile(horizon, label="v1", **self.STATION_KWARGS)])
            numeric.use_numpy()
            numpy_segments = reevaluate_admission(
                start, 60_000_000, count, old_intervals,
                [profile(horizon, label="v1", **self.STATION_KWARGS)])
        finally:
            numeric.use_numpy()
        self.assertEqual(
            [(segment.action, segment.start_index, segment.end_index)
             for segment in math_segments],
            [(segment.action, segment.start_index, segment.end_index)
             for segment in numpy_segments])


class TestChangeSegmentEquality(unittest.TestCase):

    def test_repr_and_equality(self):
        when = datetime.datetime(2024, 1, 1, tzinfo=UTC)
        segment = ChangeSegment("backfill", 2, 5, when, when, ["v1"], [])
        same = ChangeSegment("backfill", 2, 5, when, when, ["v1"], [])
        other = ChangeSegment("withdraw", 2, 5, when, when, ["v1"], [])
        self.assertEqual(segment, same)
        self.assertNotEqual(segment, other)
        self.assertIn("backfill", repr(segment))


if __name__ == "__main__":
    unittest.main()
