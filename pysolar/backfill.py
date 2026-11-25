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

"""Re-admission of fixed-interval solar samples after horizon revisions.

The archive only kept whether each original sampling segment was admitted at
the time.  When a station horizon profile is revised, this module re-evaluates
admission against the new profiles and reports the maximal runs of samples
that must be backfilled or withdrawn, without re-sampling long spans whose
verdict is unchanged -- even when the sampling step is only a few
microseconds.

Admission rule, evaluated per sample:

* the sun position is taken from :func:`pysolar.solar.get_position`, i.e.
  refracted altitude and azimuth computed with the station profile in effect
  at the sample instant;
* the blocking horizon height at the sun azimuth is obtained by linear
  interpolation between the measured (azimuth, altitude) points, moving in
  the positive azimuth direction, with the last point wrapping around to the
  first;
* a sample is admitted only when the refracted sun altitude is strictly
  greater than the interpolated horizon height.  Equality, including a sun
  sitting exactly on the horizon, counts as blocked.

The search works on continuous-time clearance rather than on individual
samples: horizon crossings are located adaptively and refined to the exact
sample index by bisection, so the number of ``get_position`` calls scales
with the number of crossings and profile switches, not with the (possibly
huge) sample count.
"""

import bisect
import datetime
from math import tan, radians

from . import solar


class StationProfile:
    """One station configuration, in effect from ``effective_from``.

    Parameters
    ----------
    effective_from : datetime.datetime
        timezone-aware UTC instant at which this profile starts being in
        effect.  A profile stays in effect until the next profile's
        ``effective_from``.
    latitude_deg, longitude_deg : float
        station coordinates in decimal degrees.
    elevation : float
        station elevation above sea level in metres.
    temperature : float
        atmospheric temperature in Kelvin, passed to ``get_position``.
    pressure : float
        atmospheric pressure in Pascal, passed to ``get_position``.
    horizon_points : sequence of (azimuth_deg, altitude_deg)
        measured points of the blocking horizon: azimuth in ``[0, 360)`` and
        horizon altitude in degrees.  At least two distinct azimuths are
        required; duplicate azimuths are rejected.
    label :
        identifier echoed back in the change reports so the effective
        profile version can be audited; defaults to the effective instant.
    """

    def __init__(self, effective_from, latitude_deg, longitude_deg,
                 elevation, temperature, pressure, horizon_points, label=None):
        if effective_from.tzinfo is None:
            raise ValueError("effective_from must be a timezone-aware datetime")
        points = [(float(az), float(alt)) for az, alt in horizon_points]
        if len(points) < 2:
            raise ValueError("at least two horizon points are required")
        points.sort()
        azimuths = [az for az, _ in points]
        for index in range(1, len(azimuths)):
            if azimuths[index] == azimuths[index - 1]:
                raise ValueError(
                    "duplicate horizon point at azimuth %r" % azimuths[index])
        for az in azimuths:
            if not 0.0 <= az < 360.0:
                raise ValueError("horizon azimuth %r is outside [0, 360)" % az)
        self.effective_from = effective_from
        self.latitude_deg = float(latitude_deg)
        self.longitude_deg = float(longitude_deg)
        self.elevation = float(elevation)
        self.temperature = float(temperature)
        self.pressure = float(pressure)
        self.azimuths = azimuths
        self.altitudes = [alt for _, alt in points]
        self.label = label if label is not None else effective_from
        self.max_horizon_slope = self._max_horizon_slope()
        self.max_horizon_height = max(self.altitudes)
        self.min_horizon_height = min(self.altitudes)
        self.refraction_jump = solar.get_refraction_correction(
            self.pressure, self.temperature,
            _REFRACTION_CUTOFF + 1e-7)

    def _max_horizon_slope(self):
        """Largest absolute horizon altitude change per azimuth degree."""
        azimuths = self.azimuths
        altitudes = self.altitudes
        slopes = []
        for index in range(len(azimuths)):
            next_index = (index + 1) % len(azimuths)
            az_span = azimuths[next_index] - azimuths[index]
            if next_index == 0:
                az_span += 360.0
            slopes.append(
                abs(altitudes[next_index] - altitudes[index]) / az_span)
        return max(slopes)

    def horizon_height(self, azimuth):
        """Blocking horizon altitude at ``azimuth`` degrees.

        Interpolation proceeds in the positive azimuth direction; the
        segment leaving the last measured point wraps through 360/0 to the
        first point.
        """
        azimuths = self.azimuths
        point = bisect.bisect_right(azimuths, azimuth) % len(azimuths)
        start = point - 1
        az0 = azimuths[start]
        alt0 = self.altitudes[start]
        az1 = azimuths[point]
        alt1 = self.altitudes[point]
        if point == 0:
            az1 += 360.0
            if azimuth < az0:
                azimuth += 360.0
        fraction = (azimuth - az0) / (az1 - az0)
        return alt0 + (alt1 - alt0) * fraction

    def clearance(self, when):
        """Signed angular clearance above the blocking horizon.

        Positive means direct sun reaches the station; zero means the sun
        sits exactly on the horizon and is treated as blocked.
        """
        azimuth, altitude = solar.get_position(
            self.latitude_deg, self.longitude_deg, when, self.elevation,
            self.temperature, self.pressure)
        return altitude - self.horizon_height(azimuth)

    def admitted(self, when):
        """Whether direct sun reaches the station at instant ``when``."""
        return self.clearance(when) > 0.0


class ChangeSegment:
    """A maximal run of consecutive samples whose verdict changes.

    Attributes
    ----------
    action : str
        ``"backfill"`` when the samples were originally blocked and are now
        admitted, or ``"withdraw"`` when they were originally admitted and
        are now blocked.
    start_index, end_index : int
        inclusive sample indices, numbered from zero at the supplied UTC
        start of the original sampling run.
    start_time, end_time : datetime.datetime
        UTC instants of the first and last samples of the run.
    profile_versions : list
        ``label`` of every station profile covering any sample in the run,
        in chronological order.
    effective_points : list of datetime.datetime
        effective-from instants of profile switches that fall between the
        run's first and last sample.
    """

    def __init__(self, action, start_index, end_index, start_time, end_time,
                 profile_versions, effective_points):
        self.action = action
        self.start_index = start_index
        self.end_index = end_index
        self.start_time = start_time
        self.end_time = end_time
        self.profile_versions = profile_versions
        self.effective_points = effective_points

    def __repr__(self):
        return ("ChangeSegment(action=%r, start_index=%d, end_index=%d, "
                "start_time=%r, end_time=%r, profile_versions=%r, "
                "effective_points=%r)" % (
                    self.action, self.start_index, self.end_index,
                    self.start_time, self.end_time, self.profile_versions,
                    self.effective_points))

    def __eq__(self, other):
        if not isinstance(other, ChangeSegment):
            return NotImplemented
        return self.__dict__ == other.__dict__


# Sidereal hour-angle rate in degrees per second (360.985647 deg/day).  The
# horizon-frame angular velocity identities give exact envelopes
# |dh/dt| <= omega and |dA/dt| <= omega (1 + |tan h|) for the topocentric
# angles; the safety factor covers the smooth refraction derivative (measured
# below 1.23) and numerical slack.  The envelopes only certify crossing-free
# spans, and any span that cannot be certified is subdivided instead.
_HOUR_ANGLE_RATE = 360.985647 / 86400.0
_RATE_SAFETY_FACTOR = 4.0

# The refraction correction in pysolar.solar jumps to zero below this
# topocentric altitude; certification carries the jump as an explicit margin
# instead of refusing spans near the horizon.
_REFRACTION_CUTOFF = -(0.26667 + 0.5667)

# Above this topocentric altitude the azimuth envelope would grow without
# bound near the zenith; such spans are certified geometrically instead,
# since no measured horizon point reaches that high.
_GEOMETRIC_ANGLE = 80.0


def _require_utc(when):
    offset = when.utcoffset()
    if offset is None or offset != datetime.timedelta(0):
        raise ValueError("datetimes must be timezone-aware UTC")
    return when


def _normalize_original_intervals(original_admission, count):
    """Return old admission as sorted, fully covering (start, end, admitted)."""
    intervals = list(original_admission)
    normalized = []
    for position, entry in enumerate(intervals):
        if isinstance(entry, (bool, int)):
            raise TypeError(
                "original admission must be given as (start_index, "
                "end_index, admitted) intervals, not per-sample booleans")
        start_index, end_index, admitted = entry
        if not (isinstance(start_index, int) and isinstance(end_index, int)
                and isinstance(admitted, bool)):
            raise TypeError(
                "admission interval %r must be (int, int, bool)" % (entry,))
        if start_index < 0 or end_index > count or end_index <= start_index:
            raise ValueError("admission interval %r is out of range" % (entry,))
        if normalized and start_index != normalized[-1][1]:
            raise ValueError(
                "admission intervals must be contiguous; gap or overlap at "
                "index %d" % start_index)
        normalized.append((start_index, end_index, admitted))
    if not normalized:
        if count != 0:
            raise ValueError("admission intervals do not cover any samples")
        return []
    if normalized[0][0] != 0 or normalized[-1][1] != count:
        raise ValueError("admission intervals must cover indices 0..%d" % count)
    return normalized


def _validate_profiles(profiles):
    profiles = list(profiles)
    if not profiles:
        raise ValueError("at least one station profile is required")
    for profile in profiles:
        if not isinstance(profile, StationProfile):
            raise TypeError("station profiles must be StationProfile instances")
        _require_utc(profile.effective_from)
    profiles.sort(key=lambda profile: profile.effective_from)
    for index in range(1, len(profiles)):
        if profiles[index].effective_from == profiles[index - 1].effective_from:
            raise ValueError("two profiles take effect at the same instant")
    return profiles


def reevaluate_admission(start, step_microseconds, count, original_admission,
                         station_profiles):
    """Re-evaluate fixed-interval sample admission under revised horizons.

    Parameters
    ----------
    start : datetime.datetime
        timezone-aware UTC instant of the original sample with index zero.
    step_microseconds : int
        integer, strictly positive spacing between consecutive samples, in
        microseconds.
    count : int
        number of samples in the original run, indexed ``0 .. count-1``.
    original_admission : sequence of (start_index, end_index, admitted)
        the archived admission intervals in sample indices (end exclusive,
        as computed from the original start).  The intervals must partition
        ``range(count)`` contiguously.
    station_profiles : sequence of StationProfile
        revised station profiles, each with its effective-from instant.  At
        any sample instant the profile most recently in effect at that
        instant governs the verdict, including across UTC calendar
        boundaries.

    Returns
    -------
    list of ChangeSegment
        maximal consecutive runs where the new verdict differs from the
        archived one.  ``backfill`` runs mark samples to add and
        ``withdraw`` runs mark samples to remove; adjacent samples whose
        verdict is unchanged are never included.

    Notes
    -----
    Sample timing uses exact integer microsecond arithmetic
    (``start + timedelta(microseconds=step_microseconds * index)``).  Every
    verdict comes from the native floating-point values returned by
    :func:`pysolar.solar.get_position`; consecutive microsecond samples that
    collapse to identical float coordinates therefore share one verdict
    automatically.  Long unchanged spans are certified in bulk and never
    re-sampled point by point.
    """
    if not isinstance(step_microseconds, int) or isinstance(
            step_microseconds, bool):
        raise TypeError("step_microseconds must be an integer")
    if step_microseconds <= 0:
        raise ValueError("step_microseconds must be positive")
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    _require_utc(start)
    profiles = _validate_profiles(station_profiles)
    if profiles[0].effective_from > start:
        raise ValueError(
            "no station profile is in effect at the first sample instant")
    if count == 0:
        return []
    old_intervals = _normalize_original_intervals(original_admission, count)

    step = datetime.timedelta(microseconds=step_microseconds)

    def sample_time(index):
        return start + step * index

    effective = [profile.effective_from for profile in profiles]
    block_edges = [0]
    for when in effective:
        if when <= start:
            continue
        offset = (when - start) // step
        if 0 < offset < count:
            insertion = bisect.bisect_left(block_edges, offset)
            if insertion == len(block_edges) or block_edges[insertion] != offset:
                block_edges.insert(insertion, offset)
    block_edges.append(count)

    evaluator = _Evaluator(step, sample_time, effective, profiles)
    new_intervals = []
    for block in range(len(block_edges) - 1):
        first = block_edges[block]
        after = block_edges[block + 1]
        new_intervals.extend(
            evaluator.resolve_block(first, after))

    return _collect_segments(old_intervals, new_intervals, sample_time,
                             effective, profiles)


class _Evaluator:
    """Memoized clearance evaluations and adaptive crossing location."""

    def __init__(self, step, sample_time, effective, profiles):
        self._step = step
        self._sample_time = sample_time
        self._effective = effective
        self._profiles = profiles
        self._cache = {}
        self.calls = 0

    def _profile_at(self, index):
        when = self._sample_time(index)
        return self._profiles[bisect.bisect_right(self._effective, when) - 1]

    def evaluate(self, index):
        """Return (admitted, signed clearance, sun altitude) for a sample."""
        cached = self._cache.get(index)
        if cached is not None:
            return cached
        profile = self._profile_at(index)
        when = self._sample_time(index)
        declination, hour_angle = solar.get_topocentric_position(
            profile.latitude_deg, profile.longitude_deg, when,
            profile.elevation)
        topocentric_altitude = solar.get_topocentric_elevation_angle(
            profile.latitude_deg, declination, hour_angle)
        refraction = solar.get_refraction_correction(
            profile.pressure, profile.temperature, topocentric_altitude)
        altitude = topocentric_altitude + refraction
        azimuth = solar.get_topocentric_azimuth_angle(
            hour_angle, profile.latitude_deg, declination)
        self.calls += 1
        clearance = altitude - profile.horizon_height(azimuth)
        result = (bool(clearance > 0.0), clearance, altitude,
                  topocentric_altitude)
        self._cache[index] = result
        return result

    def resolve_block(self, first, after):
        """Return new admission intervals over one constant-profile block."""
        flips = [first]
        self._find_flips(first, after - 1, flips)
        flips = sorted(set(flips))
        flips.append(after)
        intervals = []
        for position in range(len(flips) - 1):
            run_start = flips[position]
            run_end = flips[position + 1]
            admitted, _, _, _ = self.evaluate(run_start)
            intervals.append((run_start, run_end, admitted))
        return intervals

    def _find_flips(self, left, right, flips):
        """Locate every admission flip with index in ``(left, right]``.

        ``left`` and ``right`` are already evaluated.  Spans certified to
        contain no horizon crossing from a Lipschitz bound on the clearance
        are skipped as units; everything else is subdivided, and genuine
        flips are refined to the exact sample index by bisection.  Every
        recursive call shrinks the span, so the search always terminates
        even when many consecutive samples share identical float
        coordinates.
        """
        if right - left <= 1:
            return
        left_admitted, left_clearance, _, _ = self.evaluate(left)
        right_admitted, right_clearance, _, _ = self.evaluate(right)

        if left_admitted == right_admitted and self._no_crossing_certified(
                left, right, left_clearance, right_clearance):
            return

        if left_admitted != right_admitted:
            crossing = self._bisect_flip(left, right, left_admitted)
            flips.append(crossing)
            self._find_flips(left, crossing - 1, flips)
            self._find_flips(crossing, right, flips)
            return

        middle = (left + right) // 2
        self._find_flips(left, middle, flips)
        self._find_flips(middle, right, flips)

    def _no_crossing_certified(self, left, right, left_clearance,
                               right_clearance):
        """Certify that the clearance cannot reach zero inside the span.

        Two independent arguments are accepted:

        * geometric: the exact altitude-rate bound keeps the whole span
          above the highest measured horizon point (certainly admitted) or
          below the lowest one (certainly blocked), which also covers the
          zenith region where the azimuth envelope diverges;
        * Lipschitz: the span is first shown, again from the altitude-rate
          bound, to stay on one side of the refraction cutoff, after which
          the clearance rate is bounded from the exact altitude and
          azimuth angular-rate envelopes plus the steepest measured
          horizon segment.  If the smaller endpoint clearance exceeds that
          bound over the span, no crossing can fit -- not even one inside a
          narrow opening the endpoints miss.

        A refused certification only forces more subdivisions; it never
        changes an individual verdict.
        """
        if left_clearance == 0.0 or right_clearance == 0.0:
            return False
        if (left_clearance > 0.0) != (right_clearance > 0.0):
            return False
        _, _, _, left_topo = self.evaluate(left)
        _, _, _, right_topo = self.evaluate(right)
        profile = self._profile_at(left)
        span_seconds = (self._step * (right - left)).total_seconds()
        excursion = _HOUR_ANGLE_RATE * span_seconds
        low_topo = min(left_topo, right_topo) - excursion
        high_topo = max(left_topo, right_topo) + excursion

        if low_topo > profile.max_horizon_height:
            return True
        if high_topo + profile.refraction_jump < profile.min_horizon_height:
            return True

        if not (high_topo < _REFRACTION_CUTOFF
                or low_topo >= _REFRACTION_CUTOFF):
            return False
        if max(abs(low_topo), abs(high_topo)) > _GEOMETRIC_ANGLE:
            return False
        # |tan h| is monotone in |h| below the zenith singularity, so the
        # tightest bound over the reachable interval uses its steepest end.
        steepest = max(abs(low_topo), abs(high_topo))
        azimuth_rate = _HOUR_ANGLE_RATE * (
            1.0 + abs(tan(radians(steepest))))
        clearance_rate = _RATE_SAFETY_FACTOR * (
            _HOUR_ANGLE_RATE
            + profile.max_horizon_slope * azimuth_rate)
        nearest = min(abs(left_clearance), abs(right_clearance))
        return nearest > clearance_rate * span_seconds

    def _bisect_flip(self, left, right, left_admitted):
        """First index in ``(left, right]`` whose admission differs.

        The state at ``left`` is ``left_admitted`` and the state at ``right``
        is known to differ.  Bisection evaluates exact sample readings, so
        repeated float coordinates at microsecond spacing are handled
        naturally.
        """
        while right - left > 1:
            middle = (left + right) // 2
            middle_admitted, _, _, _ = self.evaluate(middle)
            if middle_admitted == left_admitted:
                left = middle
            else:
                right = middle
        return right


def _collect_segments(old_intervals, new_intervals, sample_time,
                      effective, profiles):
    """Merge two run-length partitions and keep the differing runs."""
    pieces = []
    old_position = 0
    new_position = 0
    cursor = 0
    total = old_intervals[-1][1]
    while cursor < total:
        _, old_end, old_admitted = old_intervals[old_position]
        _, new_end, new_admitted = new_intervals[new_position]
        run_end = min(old_end, new_end)
        if old_admitted != new_admitted:
            if pieces and pieces[-1][2] == new_admitted \
                    and pieces[-1][1] == cursor:
                pieces[-1][1] = run_end
            else:
                pieces.append([cursor, run_end, new_admitted])
        cursor = run_end
        if old_end == run_end:
            old_position += 1
        if new_end == run_end:
            new_position += 1

    change_segments = []
    for run_start, run_end, new_admitted in pieces:
        start_time = sample_time(run_start)
        end_time = sample_time(run_end - 1)
        first_profile = bisect.bisect_right(effective, start_time) - 1
        last_profile = bisect.bisect_right(effective, end_time) - 1
        versions = [
            profiles[index].label
            for index in range(first_profile, last_profile + 1)
        ]
        switch_points = [
            when for when in effective
            if start_time <= when <= end_time
        ]
        action = "backfill" if new_admitted else "withdraw"
        change_segments.append(ChangeSegment(
            action, run_start, run_end - 1, start_time, end_time,
            versions, switch_points))
    return change_segments
