"""Global consistency: signed totals, shared swaps, joint queries,
transcriptions and failure diagnostics."""

from diskrecon import Capability
from diskrecon import DutyWindow
from diskrecon import Handover
from diskrecon import Sample
from diskrecon import SignedTotal
from diskrecon import SwapClaim
from diskrecon import Transcription
from diskrecon import reconcile


def sample(no, t, nowrap, raw, phys=None, table="main", disk="sda"):
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


def two_segment_handover(signed_amount):
    """Two 8-bit segments, each {11, 267}; optional signed total."""
    return Handover(
        samples=[
            sample(1, 0.0, 239, 239, 1),
            sample(2, 60.0, 250, 250, 1),
            sample(3, 120.0, 255, 5, 1),
        ],
        duty=[
            DutyWindow(
                disk_name="sda",
                start=0.0,
                end=120.0,
                allowed_disk_nos=frozenset({1}),
                counter_bits=8,
            )
        ],
        capabilities=[Capability(1, 5.0)],  # 300 per 60s segment
        signed_totals=(
            [SignedTotal("main", "sda", 0.0, 120.0, signed_amount, label="夜班")]
            if signed_amount is not None
            else []
        ),
    )


class TestSignedTotal:
    def test_total_splits_across_segments(self):
        report = reconcile(two_segment_handover(278))  # 11 + 267
        assert report.ok
        seg0, seg1 = report.segments
        # Neither segment is pinned alone: (11, 267) and (267, 11)
        # both satisfy the signed total.
        assert (seg0.low, seg0.high) == (11, 267)
        assert (seg1.low, seg1.high) == (11, 267)

    def test_total_pins_both_segments(self):
        report = reconcile(two_segment_handover(22))  # 11 + 11 only
        assert all(s.status == "exact" and s.value == 11 for s in report.segments)

    def test_joint_query_inside_one_interpretation(self):
        report = reconcile(two_segment_handover(278))
        ok = report.check_joint(("main:sda:0", 11), ("main:sda:1", 267))
        assert ok.ok
        assert ok.witness["signed_totals"]["夜班"] == {
            "main:sda:0": 11,
            "main:sda:1": 267,
        }
        # 267 is inside each segment's own range, yet the pair cannot
        # hold simultaneously: bounds of different interpretations
        # cannot be stitched together.
        assert not report.check_joint(("main:sda:0", 267), ("main:sda:1", 267)).ok
        assert not report.check_joint(("main:sda:0", 11), ("main:sda:1", 11)).ok

    def test_infeasible_total_points_to_observations_and_capacity(self):
        report = reconcile(two_segment_handover(1000))
        assert not report.ok
        text = "\n".join(report.conflicts)
        assert "已签合计" in text
        assert "1000" in text
        assert "容量限制" in text  # capability bounds
        assert "#1" in text and "#3" in text  # original sample numbers


class TestSharedSwapAcrossTables:
    def handover(self, signed=None):
        # The old disk is known to be phys 1 (bound 6); the post-swap
        # disk is unclear, so its bound is the best of {1, 2} = 60.
        return Handover(
            samples=[
                sample(1, 0.0, 250, 250, 1, table="A"),
                sample(2, 60.0, 255, 5, None, table="A"),
                sample(3, 0.0, 250, 250, 1, table="B"),
                sample(4, 60.0, 255, 5, None, table="B"),
            ],
            duty=[
                DutyWindow(
                    disk_name="sda",
                    start=0.0,
                    end=60.0,
                    allowed_disk_nos=frozenset({1, 2}),
                    restart_allowed=True,
                    counter_bits=8,
                )
            ],
            capabilities=[Capability(1, 0.1), Capability(2, 1.0)],
            swap_claims=[SwapClaim("E1", "sda", 0.0, 60.0)],
            signed_totals=(
                [SignedTotal("A", "sda", 0.0, 60.0, signed, label="A窗")]
                if signed is not None
                else []
            ),
        )

    def test_same_interpretation_in_all_tables(self):
        # Same-disk continuation is impossible (base 11 > bound 6), so
        # the only consistent reading of E1 is "swap happened"; both
        # tables must report the swap range [5, 11].
        report = reconcile(self.handover())
        assert report.ok
        assert report.result.assignments == 1
        assert report.result.witness["swap_events"] == {"E1": True}
        for seg in report.segments:
            assert (seg.low, seg.high) == (5, 11)

    def test_signed_total_splits_swap_between_disks(self):
        # Signed total 8 in table A: new disk read 5, so the old disk
        # wrote 3 (within its bound of 6).
        report = reconcile(self.handover(signed=8))
        assert report.ok
        seg_a = next(s for s in report.segments if s.table == "A")
        assert seg_a.status == "exact"
        assert seg_a.value == 8

    def test_conflicting_total_fails_everywhere(self):
        report = reconcile(self.handover(signed=4))  # below new disk's 5
        assert not report.ok
        assert "已签合计" in "\n".join(report.conflicts)


class TestTranscription:
    def handover(self, transcribed_value):
        return Handover(
            samples=[
                sample(1, 0.0, 100, 100, 1),
                sample(2, 60.0, 130, 130, 1),
                sample(5, 0.0, 100, 100, 1, table="audit"),
            ],
            transcriptions=[
                Transcription(
                    table="audit",
                    disk_name="sda",
                    value=transcribed_value,
                    allowed_sources=(2,),
                    note="转抄自值班日志",
                )
            ],
            duty=[
                DutyWindow(
                    disk_name="sda",
                    start=0.0,
                    end=60.0,
                    allowed_disk_nos=frozenset({1}),
                    counter_bits=8,
                )
            ],
            capabilities=[Capability(1, 1.0)],
        )

    def test_transcription_unified_with_actual_reading(self):
        report = reconcile(self.handover(130))
        assert report.ok
        seg = next(s for s in report.segments if s.table == "audit")
        assert seg.status == "exact"
        assert seg.value == 30
        assert "转抄" in seg.source and "#2" in seg.source

    def test_transcription_matching_no_reading_fails(self):
        report = reconcile(self.handover(131))
        assert not report.ok
        text = "\n".join(report.conflicts)
        assert "转抄" in text and "131" in text
