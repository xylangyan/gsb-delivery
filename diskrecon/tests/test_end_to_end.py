"""Full reproduction on fake reader data (整套复现用假读表数据).

A FakeKernel plays the cabinet: 8-bit wrapping counters, a same-name
disk swap mid-shift.  The OldExporter replays the old process and its
nowrap export is cross-checked against the genuine psutil code path.
No disk outside the process is touched.
"""

from psutil._common import wrap_numbers

from diskrecon import Capability
from diskrecon import DutyWindow
from diskrecon import FakeKernel
from diskrecon import Handover
from diskrecon import OldExporter
from diskrecon import SignedTotal
from diskrecon import reconcile


def night_shift():
    """The night shift: 300 units, a swap, then 30 more units."""
    kernel = FakeKernel(counter_bits=8)
    kernel.install("sda", phys_disk_no=1)
    exporter = OldExporter(kernel, reader="night-export")

    samples = []
    samples += exporter.sweep("main", sample_no=1, timestamp=0.0)
    kernel.write("sda", 300)  # wraps the 8-bit counter: raw 44
    samples += exporter.sweep("main", sample_no=2, timestamp=60.0)
    kernel.swap("sda", phys_disk_no=2)  # new disk, counter restarts
    kernel.write("sda", 30)
    samples += exporter.sweep("main", sample_no=3, timestamp=120.0)

    handover = Handover(
        samples=samples,
        duty=[
            DutyWindow(
                disk_name="sda",
                start=0.0,
                end=120.0,
                allowed_disk_nos=frozenset({1, 2}),
                restart_allowed=True,
                counter_bits=8,
            )
        ],
        capabilities=[Capability(1, 10.0), Capability(2, 10.0)],
        signed_totals=[
            SignedTotal("main", "sda", 0.0, 60.0, 300, label="夜班前段")
        ],
    )
    return kernel, handover


class TestExporterMatchesRealPsutil:
    def test_exported_nowrap_equals_psutil_wrap_numbers(self):
        kernel, handover = night_shift()
        raws = [s.raw_value for s in handover.samples]
        assert raws == [0, 44, 30]
        # The swap makes the counter drop 44 -> 30 under the same name;
        # psutil compensates once with the old value: 30 + 44 = 74.
        wrap_numbers.cache_clear()
        try:
            expected = [
                wrap_numbers({"sda": (raw,)}, "e2e.check")["sda"][0]
                for raw in raws
            ]
        finally:
            wrap_numbers.cache_clear()
        exported = [s.nowrap_value for s in handover.samples]
        assert exported == expected == [0, 44, 74]


class TestNightShiftReconciliation:
    def test_signed_total_pins_first_segment(self):
        kernel, handover = night_shift()
        report = reconcile(handover)
        assert report.ok, report.conflicts
        seg1, seg2 = report.segments
        # The signed total pins the first segment to exactly 300
        # (one full counter period plus the raw delta of 44).
        assert seg1.status == "exact"
        assert seg1.value == 300
        assert kernel.written[1] == 300
        # The swap is certain (recorded phys numbers 1 -> 2): the new
        # disk read 30, the old one may have written up to its bound
        # of 600 and the new one up to its own (30 + 2 turnovers).
        assert seg2.status == "range"
        assert seg2.low <= kernel.written[2] <= seg2.high
        assert (seg2.low, seg2.high) == (30, 1142)

    def test_joint_queries(self):
        _, handover = night_shift()
        report = reconcile(handover)
        # The true amounts hold simultaneously.
        assert report.check_joint(("main:sda:0", 300), ("main:sda:1", 30)).ok
        # 44 alone contradicts the signed total.
        assert not report.check_joint(("main:sda:0", 44)).ok
        # Above the combined capability bounds of both disks.
        assert not report.check_joint(("main:sda:1", 1143)).ok

    def test_physical_view(self):
        kernel, handover = night_shift()
        report = reconcile(handover)
        entries = {(e.phys_disk_no, e.t0, e.t1): e for e in report.physical_view()}
        first = entries[(1, 0.0, 60.0)]
        assert first.status == "exact"
        assert first.value == kernel.written[1] == 300
        second = entries[(2, 60.0, 120.0)]
        assert second.low <= kernel.written[2] <= second.high

    def test_summary_mentions_sources(self):
        _, handover = night_shift()
        report = reconcile(handover)
        text = report.summary()
        assert "night-export" in text
        assert "已签合计" in text
