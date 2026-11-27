"""Warehouse-entry reconciliation report (入仓核对).

The report is keyed by physical disk and time interval.  A segment
with a single feasible value is reported as an exact number together
with its original reporting source; genuinely ambiguous segments are
reported as the actually achievable [low, high] range.  Joint queries
(任选两段的指定量是否能同时对上) are answered by the solver inside
one consistent interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from .solver import SolveResult
from .solver import Solver
from .solver import describe_set


@dataclass
class SegmentReport:
    seg_id: str
    table: str
    disk_name: str
    t0: float
    t1: float
    status: str  # "exact" | "range"
    value: "int | None"
    low: int
    high: int
    source: str
    feasible: str


@dataclass
class PhysicalEntry:
    phys_disk_no: int
    t0: float
    t1: float
    status: str  # "exact" | "range"
    value: "int | None"
    low: int
    high: int
    basis: str


@dataclass
class JointResult:
    ok: bool
    witness: "dict | None" = None
    conflicts: list = field(default_factory=list)


class ReconReport:
    def __init__(self, handover, solver, result, segments):
        self.handover = handover
        self.solver = solver
        self.result: SolveResult = result
        self.segments_meta = {seg.seg_id: seg for seg in segments}
        self.ok = result.ok
        self.conflicts = result.conflicts
        self.segments: list = []
        if result.ok:
            for seg in segments:
                self.segments.append(self._segment_report(seg))

    # -- per-segment report ---------------------------------------------

    def _segment_report(self, seg) -> SegmentReport:
        fs = self.result.segment_sets[seg.seg_id]
        single = fs.singleton
        source = (
            f"样本#{seg.obs0.sample_no}->#{seg.obs1.sample_no} "
            f"({seg.obs0.origin}; {seg.obs1.origin})"
        )
        if single is not None:
            for st in self.handover.signed_totals:
                if (
                    st.table == seg.table
                    and st.disk_name == seg.disk_name
                    and st.start == seg.t0
                    and st.end == seg.t1
                    and st.amount == single
                ):
                    source += f"; 与已签合计[{st.label or '未命名'}]一致"
        return SegmentReport(
            seg_id=seg.seg_id,
            table=seg.table,
            disk_name=seg.disk_name,
            t0=seg.t0,
            t1=seg.t1,
            status="exact" if single is not None else "range",
            value=single,
            low=fs.min,
            high=fs.max,
            source=source,
            feasible=describe_set(fs),
        )

    # -- joint designated-amount query -----------------------------------

    def check_joint(self, *designated) -> JointResult:
        """Can the given (seg_id, amount) pairs hold simultaneously?

        Evaluated inside single consistent interpretations — never by
        stitching the low/high bounds of different ones.
        """
        for seg_id, _ in designated:
            if seg_id not in self.segments_meta:
                return JointResult(
                    ok=False, conflicts=[f"未知段号: {seg_id}"]
                )
        result = self.solver.solve(designated)
        return JointResult(
            ok=result.ok, witness=result.witness, conflicts=result.conflicts
        )

    # -- physical view -----------------------------------------------------

    def physical_view(self) -> list:
        """Per physical disk and interval, counted by actual coverage.

        When several views cover the same device (whole disk plus two
        partitions), each physical extent is counted once: partition
        views win where their extents do not overlap, otherwise the
        whole-disk view is used alone.
        """
        if not self.ok:
            return []
        groups = {}
        for rep in self.segments:
            seg = self.segments_meta[rep.seg_id]
            cov = self.handover.coverage_for(seg.table, seg.disk_name)
            if cov is not None:
                phys = cov.phys_disk_no
                extents = set(cov.extents)
            else:
                phys = seg.obs1.phys_disk_no
                if phys is None:
                    continue
                extents = {"whole"}
            groups.setdefault((phys, seg.t0, seg.t1), []).append(
                (rep, extents, seg)
            )
        entries = []
        for (phys, t0, t1), members in sorted(groups.items()):
            chosen, basis = self._choose_views(members)
            lows = sum(rep.low for rep, _, _ in chosen)
            highs = sum(rep.high for rep, _, _ in chosen)
            exact = all(rep.status == "exact" for rep, _, _ in chosen)
            entries.append(
                PhysicalEntry(
                    phys_disk_no=phys,
                    t0=t0,
                    t1=t1,
                    status="exact" if exact else "range",
                    value=lows if exact else None,
                    low=lows,
                    high=highs,
                    basis=basis,
                )
            )
        return entries

    @staticmethod
    def _choose_views(members):
        whole = [m for m in members if m[1] == {"whole"}]
        partial = [m for m in members if m[1] != {"whole"}]
        seen = set()
        overlap = False
        for _, extents, _ in partial:
            if seen & extents:
                overlap = True
            seen |= extents
        if partial and not overlap:
            basis = "; ".join(
                f"表{m[2].table} 覆盖{sorted(m[1])}" for m in partial
            )
            if whole:
                basis += f"; 整盘视图(表{whole[0][2].table})不重复计入"
            return partial, basis
        chosen = whole[:1] or partial[:1]
        basis = (
            f"表{chosen[0][2].table} 覆盖{sorted(chosen[0][1])}; "
            f"其余 {len(members) - 1} 个视图按实际覆盖不重复计入"
        )
        return chosen, basis

    # -- text summary ---------------------------------------------------

    def summary(self) -> str:
        lines = []
        if not self.ok:
            lines.append("入仓核对失败:")
            lines.extend(f"  {c}" for c in self.conflicts)
            return "\n".join(lines)
        lines.append("按段核对:")
        for rep in self.segments:
            if rep.status == "exact":
                amount = f"确定量 {rep.value}"
            else:
                amount = f"可达区间 [{rep.low}, {rep.high}]"
            lines.append(
                f"  {rep.seg_id} [{rep.t0},{rep.t1}] {amount} "
                f"(可行域 {rep.feasible}; 来源 {rep.source})"
            )
        lines.append("按物理盘与区间核对:")
        for entry in self.physical_view():
            if entry.status == "exact":
                amount = f"确定量 {entry.value}"
            else:
                amount = f"可达区间 [{entry.low}, {entry.high}]"
            lines.append(
                f"  物理盘#{entry.phys_disk_no} [{entry.t0},{entry.t1}] "
                f"{amount} ({entry.basis})"
            )
        return "\n".join(lines)


def reconcile(handover) -> ReconReport:
    """Run the full reconciliation over a handover."""
    solver = Solver(handover)
    result = solver.solve()
    # Segment metadata comes from the first transcript assignment
    # space (deterministic when every transcription is pinned by the
    # log); empty when the handover has unresolvable transcriptions.
    segments = []
    choices = list(solver.builder.transcript_assignments())
    if choices:
        segments = solver._segments_for(choices[0])
    return ReconReport(handover, solver, result, segments)
