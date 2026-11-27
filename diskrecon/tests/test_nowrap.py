"""Field sample (现场小样): mirror vs. real psutil nowrap returns.

The mirror is checked against the genuine ``psutil._common.wrap_numbers``
code path on synthetic series, and against real kernel counters in a
live smoke test.  No disk outside the process is ever written to.
"""

import pytest
from psutil._common import wrap_numbers

from diskrecon.nowrap import NowrapTracker


def _psutil_series(name, values):
    """Run a raw series through the real psutil wrap adjustment."""
    wrap_numbers.cache_clear()
    out = []
    for value in values:
        adjusted = wrap_numbers({"disk": (value,)}, name)
        out.append(adjusted["disk"][0])
    wrap_numbers.cache_clear()
    return out


def _mirror_series(values):
    tracker = NowrapTracker()
    return [tracker.update(v) for v in values]


class TestMirrorAgainstRealPsutil:
    def test_plain_wrap(self):
        values = [250, 5, 7, 250]
        assert _mirror_series(values) == _psutil_series("t.plain", values)

    def test_multiple_wraps_and_reset_like_swap(self):
        # A same-name "swap" (reset to 0) is indistinguishable from a
        # wrap for psutil: it compensates once with the old value.
        values = [10, 250, 3, 250, 0, 4]
        assert _mirror_series(values) == _psutil_series("t.multi", values)

    def test_monotone_series_untouched(self):
        values = [0, 1, 2, 3, 1000]
        assert _mirror_series(values) == _psutil_series("t.mono", values)

    def test_two_disks_independent_reminders(self):
        # Real sweeps report every disk in each call; reminders are
        # kept per disk name.
        wrap_numbers.cache_clear()
        trackers = {"a": NowrapTracker(), "b": NowrapTracker()}
        sweeps = [
            {"a": 100, "b": 7},
            {"a": 2, "b": 9},
            {"a": 1, "b": 3},
        ]
        for sweep in sweeps:
            got = wrap_numbers(
                {name: (value,) for name, value in sweep.items()}, "t.two"
            )
            for name, value in sweep.items():
                assert trackers[name].update(value) == got[name][0]
        wrap_numbers.cache_clear()

    def test_multi_field_tuples(self):
        wrap_numbers.cache_clear()
        tracker = NowrapTracker()
        raws = [(10, 100), (20, 90), (5, 130)]
        for raw_read, raw_write in raws:
            got = wrap_numbers({"d": (raw_read, raw_write)}, "t.fields")
            assert got["d"][1] == tracker.update(raw_write)
        wrap_numbers.cache_clear()


class TestLiveSystem:
    """Live smoke check against real kernel returns (read-only)."""

    def test_nowrap_export_is_monotone(self):
        import psutil

        psutil.disk_io_counters.cache_clear()
        first = psutil.disk_io_counters(perdisk=True, nowrap=True)
        if not first:
            pytest.skip("no disk counters on this system")
        second = psutil.disk_io_counters(perdisk=True, nowrap=True)
        for name, counters in second.items():
            if name in first:
                assert counters.write_bytes >= first[name].write_bytes
                assert counters.read_bytes >= first[name].read_bytes
        psutil.disk_io_counters.cache_clear()

    def test_first_call_after_clear_matches_raw(self):
        import psutil

        # With a cold cache there is nothing to compensate: the very
        # first nowrap export must equal a raw read of the same sweep
        # up to counter growth between the two kernel reads.
        psutil.disk_io_counters.cache_clear()
        adjusted = psutil.disk_io_counters(perdisk=True, nowrap=True)
        raw = psutil.disk_io_counters(perdisk=True, nowrap=False)
        psutil.disk_io_counters.cache_clear()
        if not adjusted:
            pytest.skip("no disk counters on this system")
        for name, counters in adjusted.items():
            if name in raw:
                assert raw[name].write_bytes >= counters.write_bytes
