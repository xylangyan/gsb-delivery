"""Reconciliation report: source attribution and physical coverage."""

from diskrecon import Capability
from diskrecon import Coverage
from diskrecon import DutyWindow
from diskrecon import Handover
from diskrecon import Sample
from diskrecon import SignedTotal
from diskrecon import reconcile


def sample(no, t, nowrap, raw, phys, table, disk):
    return Sample(
        table=table,
        sample_no=no,
        timestamp=t,
        disk_name=disk,
        nowrap_value=nowrap,
        raw_value=raw,
        reader="procA",
        phys_disk_no=phys,
    )


def duty_for(disk, end=60.0):
    return DutyWindow(
        disk_name=disk,
        start=0.0,
        end=end,
        allowed_disk_nos=frozenset({7}),
        counter_bits=32,
    )


class TestSourceAttribution:
    def test_exact_value_carries_signed_total_note(self):
        handover = Handover(
            samples=[
                sample(1, 0.0, 0, 0, 7, "main", "sda"),
                sample(2, 60.0, 40, 40, 7, "main", "sda"),
            ],
            duty=[duty_for("sda")],
            capabilities=[Capability(7, 100.0)],
            signed_totals=[
                SignedTotal("main", "sda", 0.0, 60.0, 40, label="夜班窗")
            ],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "exact"
        assert seg.value == 40
        assert "已签合计" in seg.source
        assert "样本#1->#2" in seg.source


class TestPhysicalCoverage:
    def handover(self, with_partitions=True):
        samples = [
            sample(1, 0.0, 0, 0, 7, "whole", "sdb"),
            sample(2, 60.0, 30, 30, 7, "whole", "sdb"),
        ]
        coverage = [Coverage("whole", "sdb", 7, frozenset({"whole"}))]
        duty = [duty_for("sdb")]
        if with_partitions:
            samples += [
                sample(3, 0.0, 0, 0, 7, "part", "sdb1"),
                sample(4, 60.0, 10, 10, 7, "part", "sdb1"),
                sample(5, 0.0, 0, 0, 7, "part", "sdb2"),
                sample(6, 60.0, 20, 20, 7, "part", "sdb2"),
            ]
            coverage += [
                Coverage("part", "sdb1", 7, frozenset({"p1"})),
                Coverage("part", "sdb2", 7, frozenset({"p2"})),
            ]
            duty += [duty_for("sdb1"), duty_for("sdb2")]
        return Handover(
            samples=samples,
            duty=duty,
            capabilities=[Capability(7, 100.0)],
            coverage=coverage,
        )

    def test_whole_and_partitions_counted_once(self):
        # The whole-disk view says 30 and the two partitions say
        # 10 + 20: the physical total is 30, never 60.
        report = reconcile(self.handover(with_partitions=True))
        assert report.ok
        (entry,) = report.physical_view()
        assert entry.phys_disk_no == 7
        assert entry.status == "exact"
        assert entry.value == 30
        assert "不重复计入" in entry.basis

    def test_whole_view_used_when_no_partitions(self):
        report = reconcile(self.handover(with_partitions=False))
        (entry,) = report.physical_view()
        assert entry.status == "exact"
        assert entry.value == 30

    def test_summary_renders(self):
        report = reconcile(self.handover())
        text = report.summary()
        assert "物理盘#7" in text
        assert "确定量 30" in text
