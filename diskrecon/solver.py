"""Global consistency solver.

A swap event referenced by several tables is one shared boolean
variable: every table must use the same interpretation of it.  The
solver branches only on those shared events and on ambiguous
transcription sources; counter turnovers stay symbolic inside the
arithmetic-progression feasible sets, so no per-period or
per-combination archives are ever generated.

Designated amounts (任选段的指定量) are checked jointly inside a
single assignment — bounds from different interpretations are never
stitched together.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from itertools import product

from .feasible import AP
from .feasible import FeasibleSet
from .feasible import pick_sum
from .segments import SegmentBuilder
from .segments import segment_feasible


@dataclass
class SolveResult:
    ok: bool
    segment_sets: dict = field(default_factory=dict)  # seg_id -> FeasibleSet
    conflicts: list = field(default_factory=list)
    assignments: int = 0  # satisfying assignments found
    witness: "dict | None" = None


class Solver:
    def __init__(self, handover):
        self.handover = handover
        self.builder = SegmentBuilder(handover)
        self.events = sorted({c.event_id for c in handover.swap_claims})
        self._segments_cache = {}

    # -- interpretation space ------------------------------------------

    def _segments_for(self, transcript_choice):
        key = tuple(sorted(transcript_choice.items()))
        if key not in self._segments_cache:
            self._segments_cache[key] = self.builder.build_segments(
                transcript_choice
            )
        return self._segments_cache[key]

    def _event_assignments(self):
        if not self.events:
            yield {}
            return
        for combo in product((False, True), repeat=len(self.events)):
            yield dict(zip(self.events, combo))

    def _assignments(self):
        for tchoice in self.builder.transcript_assignments():
            segments = self._segments_for(tchoice)
            for event_map in self._event_assignments():
                yield tchoice, event_map, segments

    # -- constraints ----------------------------------------------------

    def _signed_total_chains(self, segments, st):
        chain = [
            seg
            for seg in segments
            if seg.table == st.table
            and seg.disk_name == st.disk_name
            and seg.t0 >= st.start
            and seg.t1 <= st.end
        ]
        chain.sort(key=lambda s: s.t0)
        covered = (
            chain
            and chain[0].t0 == st.start
            and chain[-1].t1 == st.end
            and all(
                chain[i].t1 == chain[i + 1].t0 for i in range(len(chain) - 1)
            )
        )
        return chain if covered else None

    def _signed_total_failures(self, segments, sets):
        failures = []
        for st in self.handover.signed_totals:
            label = st.label or f"{st.table}:{st.disk_name}"
            chain = self._signed_total_chains(segments, st)
            if chain is None:
                failures.append(
                    f"已签合计[{label}] 窗口[{st.start},{st.end}] "
                    f"未被原观测完整覆盖, 无法核对"
                )
                continue
            total = FeasibleSet.sum_all([sets[seg.seg_id] for seg in chain])
            if not total.contains(st.amount):
                samples_involved = ", ".join(
                    f"#{no}"
                    for no in sorted(
                        {seg.obs0.sample_no for seg in chain}
                        | {seg.obs1.sample_no for seg in chain}
                    )
                )
                caps = "; ".join(
                    f"{seg.seg_id}≤{seg.cap_same}(同盘)/"
                    f"{seg.cap_old}+{seg.cap_new}(换盘)"
                    for seg in chain
                )
                failures.append(
                    f"已签合计[{label}] 金额={st.amount} 对不上: "
                    f"该窗口实际可达 [{total.min},{total.max}] "
                    f"(原观测样本号 {samples_involved}; 容量限制 {caps})"
                )
        return failures

    def _propagate_signed_totals(self, segments, sets):
        """Arc-consistency: keep only values supported by the signed totals.

        A segment covered by a signed total may only keep values that
        take part in some combination summing to the signed amount;
        this is how an exact total pins a segment down to a definite
        number.  Returns None when the totals are mutually inconsistent.
        """
        sets = dict(sets)
        for _ in range(10):  # fixpoint over possibly overlapping windows
            changed = False
            for st in self.handover.signed_totals:
                chain = self._signed_total_chains(segments, st)
                if chain is None:
                    continue
                for seg in chain:
                    rest = FeasibleSet.sum_all(
                        [
                            sets[other.seg_id]
                            for other in chain
                            if other.seg_id != seg.seg_id
                        ]
                    )
                    supported = sets[seg.seg_id].intersect(
                        _reflect(rest, st.amount)
                    )
                    if supported.is_empty:
                        return None
                    if supported != sets[seg.seg_id]:
                        sets[seg.seg_id] = supported
                        changed = True
            if not changed:
                return sets
        return sets

    # -- solving ----------------------------------------------------------

    def solve(self, designated=()) -> SolveResult:
        """Check every interpretation; ``designated`` pins segment amounts."""
        designated = dict(designated)
        if self.builder.static_conflicts:
            return SolveResult(
                ok=False, conflicts=list(self.builder.static_conflicts)
            )
        satisfying = []
        failure_log = []
        for tchoice, event_map, segments in self._assignments():
            sets = {}
            failures = []
            for seg in segments:
                swap_happened = (
                    event_map.get(seg.swap_event)
                    if seg.swap_event is not None
                    else None
                )
                fs = segment_feasible(seg, swap_happened)
                if fs.is_empty:
                    failures.append(
                        f"段 {seg.seg_id} 无可行解释: 原观测样本号 "
                        f"#{seg.obs0.sample_no}->#{seg.obs1.sample_no} "
                        f"({seg.obs0.origin}; {seg.obs1.origin})"
                    )
                    continue
                if seg.seg_id in designated:
                    amount = designated[seg.seg_id]
                    if not fs.contains(amount):
                        failures.append(
                            f"段 {seg.seg_id} 指定量 {amount} 不可达: "
                            f"该解释下可行域为 {describe_set(fs)} "
                            f"(样本号 #{seg.obs0.sample_no}->#{seg.obs1.sample_no}; "
                            f"{seg.cap_source})"
                        )
                        continue
                    fs = FeasibleSet.progression(amount, 1, 1)
                sets[seg.seg_id] = fs
            if not failures:
                failures = self._signed_total_failures(segments, sets)
            if not failures:
                refined = self._propagate_signed_totals(segments, sets)
                if refined is None:
                    failures = [
                        "已签合计之间互相矛盾, 无可行解释 "
                        f"(事件解释 {event_map})"
                    ]
                else:
                    sets = refined
            if failures:
                failure_log.append(failures)
            else:
                satisfying.append((tchoice, event_map, segments, sets))

        if not satisfying:
            best = min(failure_log, key=len) if failure_log else []
            conflicts = [
                "所有解释均无法满足全部票据, 失败原因:"
            ] + best
            return SolveResult(ok=False, conflicts=conflicts)

        seg_sets = {}
        for _, _, segments, sets in satisfying:
            for seg in segments:
                fs = sets[seg.seg_id]
                if seg.seg_id in seg_sets:
                    fs = seg_sets[seg.seg_id].union(fs)
                seg_sets[seg.seg_id] = fs

        witness = self._witness(satisfying[0], designated)
        return SolveResult(
            ok=True,
            segment_sets=seg_sets,
            assignments=len(satisfying),
            witness=witness,
        )

    def _witness(self, sat, designated):
        tchoice, event_map, segments, sets = sat
        witness = {
            "swap_events": dict(event_map),
            "transcriptions": dict(tchoice),
            "designated": dict(designated),
            "signed_totals": {},
        }
        for st in self.handover.signed_totals:
            chain = [
                seg
                for seg in segments
                if seg.table == st.table
                and seg.disk_name == st.disk_name
                and seg.t0 >= st.start
                and seg.t1 <= st.end
            ]
            chain.sort(key=lambda s: s.t0)
            values = pick_sum(
                [sets[seg.seg_id] for seg in chain], st.amount
            )
            if values is not None:
                witness["signed_totals"][st.label or st.table] = {
                    seg.seg_id: val for seg, val in zip(chain, values)
                }
        return witness


def _reflect(fs: FeasibleSet, target: int) -> FeasibleSet:
    """All ``v`` such that ``target - v`` is in ``fs``."""
    return FeasibleSet(
        [AP(target - p.last, p.step, p.count) for p in fs.pieces]
    )


def describe_set(fs: FeasibleSet) -> str:
    parts = []
    for piece in fs.pieces:
        if piece.count == 1:
            parts.append(str(piece.start))
        elif piece.step == 1:
            parts.append(f"[{piece.start}..{piece.last}]")
        else:
            parts.append(
                f"{piece.start}+{f'k*{piece.step}'} (k=0..{piece.count - 1})"
            )
    return " ∪ ".join(parts) if parts else "∅"
