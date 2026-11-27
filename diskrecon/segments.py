"""Segment construction and per-segment feasible write deltas.

Observations come from actual kernel readings plus transcribed
records unified with the actual reading the log allows.  Consecutive
observations of the same (table, disk name) form a segment; each
segment gets a feasible set of write deltas built from

* the counter modulus (original field width from the duty record),
* the device capability records (a finite upper bound per segment,
  per physical disk when a swap is involved),
* the identity evidence (recorded physical disk numbers, device
  serials, or merely "restart allowed" for unclear duty records).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .feasible import FeasibleSet
from .model import Sample


class HandoverError(Exception):
    """The handover material is internally inconsistent."""


@dataclass
class Observation:
    table: str
    disk_name: str
    timestamp: float
    sample_no: int
    nowrap_value: int
    raw_value: "int | None"
    reader: str
    phys_disk_no: "int | None"
    origin: str


@dataclass
class Segment:
    """The interval between two consecutive observations."""

    seg_id: str
    table: str
    disk_name: str
    t0: float
    t1: float
    obs0: Observation
    obs1: Observation
    modulus: int
    cap_same: int
    cap_old: int
    cap_new: int
    swap_possible: bool
    definite_swap: bool
    swap_event: "str | None"
    cap_source: str

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


def _obs_from_sample(sample: Sample) -> Observation:
    return Observation(
        table=sample.table,
        disk_name=sample.disk_name,
        timestamp=sample.timestamp,
        sample_no=sample.sample_no,
        nowrap_value=sample.nowrap_value,
        raw_value=sample.raw_value,
        reader=sample.reader,
        phys_disk_no=sample.phys_disk_no,
        origin=f"内核取数#{sample.sample_no} 读表进程={sample.reader}",
    )


class SegmentBuilder:
    """Turns a handover into observations, segments and transcript options."""

    def __init__(self, handover):
        self.handover = handover
        samples = handover.samples
        if handover.archive is not None:
            samples = handover.archive.filter_samples(samples)
        self.samples = sorted(samples, key=lambda s: (s.timestamp, s.sample_no))
        self.by_sample_no = {}
        for sample in self.samples:
            self.by_sample_no.setdefault(sample.sample_no, []).append(sample)
        self.transcript_options = []  # list of list[sample_no], per transcription
        self.static_conflicts = []
        self._resolve_transcriptions()

    def _resolve_transcriptions(self):
        for idx, tr in enumerate(self.handover.transcriptions):
            valid = []
            for no in tr.allowed_sources:
                for sample in self.by_sample_no.get(no, []):
                    if sample.nowrap_value == tr.value:
                        valid.append(no)
            valid = sorted(set(valid))
            if not valid:
                self.static_conflicts.append(
                    f"转抄记录[{idx}] 表={tr.table} 盘={tr.disk_name} 值={tr.value} "
                    f"与任何允许的实际取数 {sorted(tr.allowed_sources)} 对不上"
                )
            self.transcript_options.append(valid)

    def transcript_assignments(self):
        """Every allowed way to unify transcriptions with actual readings."""
        if not self.transcript_options:
            yield {}
            return
        if any(not opts for opts in self.transcript_options):
            return  # hard conflict; reported via static_conflicts
        keys = range(len(self.transcript_options))
        for combo in product(*self.transcript_options):
            yield dict(zip(keys, combo))

    def _transcription_observations(self, choice):
        out = []
        for idx, tr in enumerate(self.handover.transcriptions):
            source_no = choice[idx]
            source = self.by_sample_no[source_no][0]
            out.append(
                Observation(
                    table=tr.table,
                    disk_name=tr.disk_name,
                    timestamp=source.timestamp,
                    sample_no=source.sample_no,
                    nowrap_value=tr.value,
                    raw_value=source.raw_value,
                    reader=source.reader,
                    phys_disk_no=source.phys_disk_no,
                    origin=f"转抄[{idx}]→内核取数#{source_no}",
                )
            )
        return out

    def build_segments(self, transcript_choice) -> list:
        observations = [_obs_from_sample(s) for s in self.samples]
        observations.extend(self._transcription_observations(transcript_choice))
        groups = {}
        for obs in observations:
            groups.setdefault((obs.table, obs.disk_name), []).append(obs)
        segments = []
        for (table, disk_name), obs_list in sorted(groups.items()):
            obs_list.sort(key=lambda o: (o.timestamp, o.sample_no))
            for i in range(len(obs_list) - 1):
                seg = self._make_segment(table, disk_name, i, obs_list[i], obs_list[i + 1])
                segments.append(seg)
        return segments

    def _make_segment(self, table, disk_name, index, obs0, obs1) -> Segment:
        handover = self.handover
        duty = handover.duty_for(disk_name, obs0.timestamp, obs1.timestamp)
        bits = duty.counter_bits if duty else 64
        modulus = 1 << bits
        duration = obs1.timestamp - obs0.timestamp

        phys0, phys1 = obs0.phys_disk_no, obs1.phys_disk_no
        definite_same = False
        definite_swap = False
        if phys0 is not None and phys1 is not None:
            definite_same = phys0 == phys1
            definite_swap = phys0 != phys1
        elif duty is not None and duty.serial is not None:
            definite_same = True  # 设备序号把身份钉住了

        allowed = set(duty.allowed_disk_nos) if duty else set()
        swap_possible = (
            not definite_same
            and duty is not None
            and duty.restart_allowed
        )

        def cap_for(phys, fallback_pool):
            candidates = [phys] if phys is not None else sorted(fallback_pool)
            if not candidates:
                raise HandoverError(
                    f"段 {table}:{disk_name} 缺少盘号信息, 无法套用设备能力记录"
                )
            best, best_no = None, None
            for no in candidates:
                cap = handover.capability_for(no)
                if cap is None:
                    raise HandoverError(
                        f"设备能力记录缺失: 物理盘#{no} "
                        f"(段 {table}:{disk_name})"
                    )
                bound = cap.bound(duration)
                if best is None or bound > best:
                    best, best_no = bound, no
            return best, best_no

        cap_same, same_no = cap_for(phys0, allowed)
        cap_old, old_no = cap_for(phys0, allowed)
        cap_new, new_no = cap_for(phys1, allowed)
        cap_source = (
            f"同盘上限←盘#{same_no}; 换盘上限←旧盘#{old_no}/新盘#{new_no}"
        )

        swap_event = None
        for claim in handover.swap_claims:
            if (
                claim.disk_name == disk_name
                and claim.start < obs1.timestamp
                and obs0.timestamp < claim.end
            ):
                swap_event = claim.event_id
                break

        return Segment(
            seg_id=f"{table}:{disk_name}:{index}",
            table=table,
            disk_name=disk_name,
            t0=obs0.timestamp,
            t1=obs1.timestamp,
            obs0=obs0,
            obs1=obs1,
            modulus=modulus,
            cap_same=cap_same,
            cap_old=cap_old,
            cap_new=cap_new,
            swap_possible=swap_possible,
            definite_swap=definite_swap,
            swap_event=swap_event,
            cap_source=cap_source,
        )


def feasible_same(seg: Segment) -> FeasibleSet:
    """Same-disk continuation: ``base + k*modulus`` within capability."""
    obs0, obs1 = seg.obs0, seg.obs1
    if obs0.raw_value is not None and obs1.raw_value is not None:
        base = (obs1.raw_value - obs0.raw_value) % seg.modulus
    else:
        # Only the exported nowrap counters survive.  The old exporter
        # already compensated once per observed decrease under the same
        # name; any further turnovers add whole periods on top of the
        # exported delta.
        base = obs1.nowrap_value - obs0.nowrap_value
        if base < 0:
            # Exported nowrap counters must be monotone: the original
            # observations themselves are inconsistent.
            return FeasibleSet.empty()
    return FeasibleSet.wrapped_deltas(base, seg.modulus, seg.cap_same)


def feasible_swap(seg: Segment) -> FeasibleSet:
    """Disk swap: old disk up to its own bound, new disk from zero."""
    obs1 = seg.obs1
    if obs1.raw_value is not None:
        new_disk = FeasibleSet.wrapped_deltas(
            obs1.raw_value, seg.modulus, seg.cap_new
        )
        old_disk = FeasibleSet.interval(0, seg.cap_old)
        return old_disk.minkowski(new_disk)
    # nowrap-only: the exported delta cannot be split between the two
    # disks, so the honest envelope is the sum of both capability bounds.
    return FeasibleSet.interval(0, seg.cap_old + seg.cap_new)


def segment_feasible(seg: Segment, swap_happened: "bool | None") -> FeasibleSet:
    """Feasible write deltas under a given swap interpretation.

    ``swap_happened`` is None for segments not tied to a shared swap
    event: both interpretations are then unioned directly.
    """
    if seg.definite_swap:
        return feasible_swap(seg)
    if not seg.swap_possible:
        return feasible_same(seg)
    if swap_happened is None:
        return feasible_same(seg).union(feasible_swap(seg))
    return feasible_swap(seg) if swap_happened else feasible_same(seg)
