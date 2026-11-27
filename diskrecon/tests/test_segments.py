"""Segment-level interpretation: wraps, swaps, capability bounds."""

from diskrecon import Capability
from diskrecon import DutyWindow
from diskrecon import Handover
from diskrecon import Sample
from diskrecon import reconcile


def sample(no, t, nowrap, raw, phys, table="main", disk="sda", reader="procA"):
    return Sample(
        table=table,
        sample_no=no,
        timestamp=t,
        disk_name=disk,
        nowrap_value=nowrap,
        raw_value=raw,
        reader=reader,
        phys_disk_no=phys,
    )


def duty(bits=8, restart=False, allowed=(1,), serial=None, end=100.0):
    return DutyWindow(
        disk_name="sda",
        start=0.0,
        end=end,
        serial=serial,
        allowed_disk_nos=frozenset(allowed),
        restart_allowed=restart,
        counter_bits=bits,
    )


class TestSameDiskWrap:
    def test_8bit_250_to_5_exact(self):
        # 250 -> 5 on an 8-bit counter: same disk wrote 11 units.
        # Capability bound (0.2/s over 60s = 12) rules out any
        # further turnover, so the amount is exact.
        handover = Handover(
            samples=[sample(1, 0.0, 250, 250, 1), sample(2, 60.0, 255, 5, 1)],
            duty=[duty()],
            capabilities=[Capability(1, 0.2)],
        )
        report = reconcile(handover)
        assert report.ok
        (seg,) = report.segments
        assert seg.status == "exact"
        assert seg.value == 11
        assert "样本#1->#2" in seg.source
        assert "procA" in seg.source

    def test_unmeasured_window_may_turn_over_legally(self):
        # Same readings, but the capability record allows 600 units:
        # k = 0, 1, 2 turnovers are all legal — never assume one period.
        handover = Handover(
            samples=[sample(1, 0.0, 250, 250, 1), sample(2, 60.0, 255, 5, 1)],
            duty=[duty()],
            capabilities=[Capability(1, 10.0)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "range"
        assert (seg.low, seg.high) == (11, 523)

    def test_serial_pins_identity(self):
        handover = Handover(
            samples=[sample(1, 0.0, 250, 250, None), sample(2, 60.0, 255, 5, None)],
            duty=[duty(serial="SN-001", restart=True, allowed=(1, 2))],
            capabilities=[Capability(1, 0.2), Capability(2, 0.2)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "exact"
        assert seg.value == 11  # 有序号, 不允许换盘解释

    def test_nowrap_only_fallback(self):
        # Raw counter not retained: the exported nowrap delta (already
        # compensated once by the old process) is the base; further
        # turnovers add whole periods.
        handover = Handover(
            samples=[sample(1, 0.0, 250, None, 1), sample(2, 60.0, 255, None, 1)],
            duty=[duty()],
            capabilities=[Capability(1, 0.2)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "exact"
        assert seg.value == 5


class TestSwapInterpretation:
    def test_ambiguous_restart_allowed(self):
        # Unclear duty record: restart allowed, disks 1 and 2 both
        # allowed.  Same-disk gives 11; a swap gives [5, 65] (new disk
        # restarted at 0 and read 5, old disk up to its own 60 bound).
        handover = Handover(
            samples=[
                sample(1, 0.0, 250, 250, None),
                sample(2, 60.0, 255, 5, None),
            ],
            duty=[duty(restart=True, allowed=(1, 2))],
            capabilities=[Capability(1, 1.0), Capability(2, 0.5)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "range"
        assert (seg.low, seg.high) == (5, 65)

    def test_definite_swap_from_recorded_phys_numbers(self):
        # The old process recorded different physical disk numbers:
        # the swap is certain, each disk bounded by its own capability.
        handover = Handover(
            samples=[sample(1, 0.0, 250, 250, 1), sample(2, 60.0, 255, 5, 2)],
            duty=[duty(allowed=(1, 2))],
            capabilities=[Capability(1, 1.0), Capability(2, 0.5)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert seg.status == "range"
        assert (seg.low, seg.high) == (5, 65)

    def test_swap_per_disk_bounds(self):
        # Tighten the old disk's capability: the swap range shrinks to
        # [5, 5+6]; the new disk is still bounded by its own record.
        handover = Handover(
            samples=[sample(1, 0.0, 250, 250, 1), sample(2, 60.0, 255, 5, 2)],
            duty=[duty(allowed=(1, 2))],
            capabilities=[Capability(1, 0.1), Capability(2, 0.5)],
        )
        report = reconcile(handover)
        (seg,) = report.segments
        assert (seg.low, seg.high) == (5, 11)
