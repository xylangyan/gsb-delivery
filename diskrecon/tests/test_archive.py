"""Historical archives handed over by omitted spans and stride."""

from diskrecon import ArchiveIndex
from diskrecon import Capability
from diskrecon import DutyWindow
from diskrecon import Handover
from diskrecon import OmittedSpan
from diskrecon import Sample
from diskrecon import reconcile


class TestArchiveIndex:
    def test_whole_span_omitted(self):
        span = OmittedSpan(3, 8)
        assert all(span.is_omitted(no) for no in range(3, 9))
        assert not span.is_omitted(2)

    def test_stride_keeps_every_nth(self):
        span = OmittedSpan(3, 9, stride=3)  # keeps 3, 6, 9
        kept = [no for no in range(3, 10) if not span.is_omitted(no)]
        assert kept == [3, 6, 9]

    def test_retained_filter(self):
        index = ArchiveIndex([OmittedSpan(3, 8), OmittedSpan(20, 30, stride=5)])
        retained = list(index.retained(range(0, 31)))
        assert retained == [0, 1, 2, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                            20, 25, 30]


class TestReconcileAcrossOmittedSpans:
    def handover(self, rate):
        samples = [
            Sample(
                table="main",
                sample_no=no,
                timestamp=no * 60.0,
                disk_name="sda",
                nowrap_value=no * 10,
                raw_value=no * 10,
                reader="procA",
                phys_disk_no=1,
            )
            for no in range(5)
        ]
        return Handover(
            samples=samples,
            duty=[
                DutyWindow(
                    disk_name="sda",
                    start=0.0,
                    end=240.0,
                    allowed_disk_nos=frozenset({1}),
                    counter_bits=8,
                )
            ],
            capabilities=[Capability(1, rate)],
            archive=ArchiveIndex([OmittedSpan(1, 3)]),  # 中间三笔省略
        )

    def test_gap_becomes_one_segment(self):
        report = reconcile(self.handover(rate=1.0))  # cap 240 >= 40
        assert report.ok
        (seg,) = report.segments
        assert (seg.t0, seg.t1) == (0.0, 240.0)
        assert seg.status == "exact"
        assert seg.value == 40

    def test_gap_allows_legal_turnovers(self):
        # cap 480: the unmeasured window may legally have passed one
        # extra counter period — the archive is not expanded per period.
        report = reconcile(self.handover(rate=2.0))
        (seg,) = report.segments
        assert seg.status == "range"
        assert (seg.low, seg.high) == (40, 296)
