'''
HdrHistogram 交接规划器（forensic planner）

同一批监测原值分别用两种期望间隔（旧设备节拍 I1、值班台间隔 I2）调用
HdrHistogram.record_corrected_value，形成两份同配置的旧桶快照 A、B。
逐笔日志退役后仅留下：

  - 两份旧材料的桶计数（尾部零桶可能被物理截断，缺省一律按零读）；
  - 已核实的真实请求总数 N（补点不计真实请求；两档补点链长度不同，
    两份总计数天然不相等，但每份都 >= N）；
  - 交接单上可直接确认的少量原值及重数；
  - 新版期望间隔 I_new。

对指定目标桶 t，求在两份旧材料与全部确认记录同时成立的前提下，用 I_new
重录后 t 内真实请求计数的最小值/最大值：

  - min == max：唯一可定，直接出值；
  - 否则用两趟分支定界分别求出最小、最大可达计数，各给一份原入口
    重录方案（原值 + 重数）；方案必须能在旧两档中逐桶核回
    （replay 用真实 HdrHistogram 核验），即两结果都有可用原入口；
  - 证据矛盾时抛 ForensicConflict，指到具体桶索引或确认记录；
  - 组合空间在节点护栏内仍无法定界时抛 ForensicConflict，指出需要
    追加确认记录的区间，而不是返回未经核验的松弛界。

可扩展性（桶内上亿原值、补点链超长时不得逐个展开）：

  - 补点链 v, v-I, v-2I, ... 落在任意桶区间的点数全部走闭式
    floor/ceil 计数（chain_count_in_range），不展开补点序列；
  - 位置类按链计数断点切分：补点链在低桶 j 的点数仅当链点跨过桶
    下界 L_j 时变化，即 v ≡ L_j (mod I)。断点只由活动桶边界和
    目标桶边界的同余式生成，类数与桶内原值绝对个数、补点链长度
    解耦；超护栏时抛 ForensicConflict 指到具体源桶；
  - 重数是类上的整数变量，不按请求个数展开，上亿重数只做一次乘法；
  - 方程呈三角结构：高桶原值的链只向低桶传播，桶自高而低处理。
    每类重数的上界由其完整链触及桶的最小残余当场定死（cap 剪枝），
    末位可用类被自身桶两余数强制整除直解，分支定界原地改值/撤销。
'''
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from hdrh.histogram import HdrHistogram


# ---------------------------------------------------------------------------
# 桶几何（与 hdrh.histogram.HdrHistogram 同构，纯算术）
# ---------------------------------------------------------------------------

def _clz64(value: int) -> int:
    if value <= 0:
        raise ValueError("clz is only defined for positive values")
    return 64 - value.bit_length()


class BucketGeometry:
    '''与 HdrHistogram 同构的桶几何（旧设备整纳秒记，lowest=1）。'''

    def __init__(self, significant_figures: int,
                 highest_trackable_value: int):
        largest_value_single_unit_res = 2 * (10 ** significant_figures)
        subb_count_mag = int(math.ceil(
            math.log(largest_value_single_unit_res) / math.log(2)))
        self.sub_bucket_half_count_magnitude = \
            subb_count_mag - 1 if subb_count_mag > 1 else 0
        self.sub_bucket_half_count = 1 << self.sub_bucket_half_count_magnitude
        self.sub_bucket_count = self.sub_bucket_half_count << 1
        self.sub_bucket_mask = self.sub_bucket_count - 1
        smallest = self.sub_bucket_count
        bucket_count = 1
        while smallest <= int(highest_trackable_value):
            smallest <<= 1
            bucket_count += 1
        self.bucket_count = bucket_count
        self.significant_figures = significant_figures
        self.highest_trackable_value = int(highest_trackable_value)
        self.counts_len = (bucket_count + 1) * self.sub_bucket_half_count

    def bucket_index_for(self, value: int) -> int:
        pow2ceiling = 64 - _clz64(int(value) | self.sub_bucket_mask)
        return pow2ceiling - (self.sub_bucket_half_count_magnitude + 1)

    def sub_bucket_index_for(self, value: int, bucket_index: int) -> int:
        return int(value) >> bucket_index

    def index_for(self, value: int) -> int:
        bi = self.bucket_index_for(value)
        si = self.sub_bucket_index_for(value, bi)
        return (((bi + 1) << self.sub_bucket_half_count_magnitude)
                + si - self.sub_bucket_half_count)

    def value_from_index(self, index: int) -> int:
        '''索引 -> 桶内最低等值原值（与真实 record 落点一致）。

        低半区 index < half 属于 bucket 0 的单位分辨率段，值就是 index
        本身（库自带的 get_value_from_index 对该段有已知 quirk，这里
        按 record_value 的真实映射纠正）。
        '''
        if index < self.sub_bucket_half_count:
            return index
        bucket_index = (index >> self.sub_bucket_half_count_magnitude) - 1
        sub_bucket_index = ((index & (self.sub_bucket_half_count - 1))
                            + self.sub_bucket_half_count)
        return sub_bucket_index << bucket_index

    def range_for_index(self, index: int) -> Tuple[int, int]:
        '''桶 index 覆盖的整数值闭区间 [lo, hi]。'''
        if index < self.sub_bucket_half_count:
            return index, index
        low = self.value_from_index(index)
        bucket_index = (index >> self.sub_bucket_half_count_magnitude) - 1
        return low, low + (1 << bucket_index) - 1

    @classmethod
    def from_hdr(cls, hist: HdrHistogram) -> 'BucketGeometry':
        return cls(hist.significant_figures, hist.highest_trackable_value)


# ---------------------------------------------------------------------------
# 补点链闭式算术
# ---------------------------------------------------------------------------

def corrected_chain_length(value: int, interval: int) -> int:
    '''record_corrected_value 实际写入点数（含原值自身）。

    库语义：记录 value 后，仅当 value > interval 才继续减 interval，
    故链为 v, v-I, ..., r_min（1 <= r_min <= I），点数 = ceil(v/I)；
    v=0 时只记录自身一次。与题面 35/10 -> 35,25,15,5（4 点）、
    35/20 -> 35,15（2 点）一致。
    '''
    if interval <= 0 or value <= 0:
        return 1
    return (value + interval - 1) // interval


def chain_count_in_range(value: int, interval: int,
                         lo: int, hi: int) -> int:
    '''补点链落在闭区间 [lo, hi] 的点数，闭式计数（不展开链）。

    链上的值为 r_min + m*I，m = 0 .. L-1，
    L = ceil(v/I)，r_min = v - (L-1)*I（1 <= r_min <= I）。
    '''
    if interval <= 0 or value <= 0:
        return 1 if lo <= value <= hi else 0
    if lo > hi or lo > value:
        return 0
    length = (value + interval - 1) // interval
    r_min = value - (length - 1) * interval
    m_lo = (lo - r_min + interval - 1) // interval
    if m_lo < 0:
        m_lo = 0
    m_hi = (min(hi, value) - r_min) // interval
    if m_hi < m_lo:
        return 0
    return min(m_hi, length - 1) - m_lo + 1


def chain_bucket_vector(geom: BucketGeometry, value: int, interval: int,
                        max_index: int) -> Dict[int, int]:
    '''补点链在各旧桶上的落点计数（只遍历被链碰到的桶边界）。'''
    vector: Dict[int, int] = {}
    index = min(geom.index_for(value), max_index - 1)
    while index >= 0:
        lo, hi = geom.range_for_index(index)
        if lo > value:
            index -= 1
            continue
        count = chain_count_in_range(value, interval, lo, min(hi, value))
        if count:
            vector[index] = count
        index -= 1
    return vector


# ---------------------------------------------------------------------------
# 旧快照
# ---------------------------------------------------------------------------

class ForensicConflict(ValueError):
    '''两份旧材料或确认记录之间存在矛盾。'''


@dataclass
class Snapshot:
    '''一份旧桶快照。

    stored_len: 物理存下的桶数；索引 >= stored_len 属于尾部缺省零，
                照零读且不能承接任何请求（原值自身桶必须在该范围内）。
    '''
    geometry: BucketGeometry
    counts: Dict[int, int]
    stored_len: int
    interval: int
    label: str = "snapshot"

    @classmethod
    def from_encoded(cls, blob: str, interval: int,
                     label: str = "snapshot") -> 'Snapshot':
        hist = HdrHistogram.decode(blob)
        geom = BucketGeometry.from_hdr(hist)
        sparse: Dict[int, int] = {}
        max_nonzero = -1
        for index in range(hist.counts_len):
            count = hist.get_count_at_index(index)
            if count:
                sparse[index] = int(count)
                max_nonzero = index
        return cls(geom, sparse, max_nonzero + 1, interval, label)

    @classmethod
    def from_dense(cls, values: Sequence[int], geometry: BucketGeometry,
                   interval: int, label: str = "snapshot",
                   stored_len: Optional[int] = None) -> 'Snapshot':
        sparse: Dict[int, int] = {}
        max_nonzero = -1
        for index, count in enumerate(values):
            if count:
                sparse[index] = int(count)
                max_nonzero = index
        if stored_len is None:
            stored_len = max_nonzero + 1
        return cls(geometry, sparse, stored_len, interval, label)

    def count_at(self, index: int) -> int:
        if index >= self.stored_len:
            return 0
        return self.counts.get(index, 0)

    def total(self) -> int:
        return sum(self.counts.values())


# ---------------------------------------------------------------------------
# 位置类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionClass:
    '''桶内补点行为完全同构的一组原值。

    representative: 代表原值（重录方案里用它承接整类重数）；
    size: 该类覆盖的桶内合法原值个数（信息性）；
    vector_a / vector_b: 旧两档补点链在各桶的落点计数；
    target_count: 新档补点链落在目标桶的点数。
    '''
    representative: int
    size: int
    vector_a: Dict[int, int]
    vector_b: Dict[int, int]
    target_count: int


def _residue_points(lo: int, hi: int, interval: int,
                    bases: Sequence[int]) -> List[int]:
    '''桶区间 [lo, hi] 内所有满足 v ≡ base (mod interval) 的断点。'''
    points: List[int] = []
    for base in bases:
        residue = base % interval
        first = lo + ((residue - lo) % interval)
        if first <= hi:
            count = (hi - first) // interval + 1
            points.extend(first + k * interval for k in range(count))
    return points


def build_position_classes(geom: BucketGeometry, source_index: int,
                           interval_a: int, interval_b: int,
                           interval_new: int, target_index: int,
                           active_indices: Sequence[int],
                           class_guard: int = 200000) -> List[PositionClass]:
    '''按链计数断点把源桶切成位置类。

    补点链在低桶 j 的点数 c_j(v) 仅当某个链点跨过桶下界 L_j 时变化，
    即 v ≡ L_j (mod I)。故对每个间隔，断点只由“活动桶”（残余非零、
    链真正需要核回的桶）的下界生成；新档再加目标桶上下界。
    块内旧两档链向量与新档目标点数恒定，每块一个位置类。
    类数与补点链长度、桶内原值绝对个数解耦；超护栏抛 ForensicConflict。
    '''
    lo, hi = geom.range_for_index(source_index)
    target_lo, target_hi = geom.range_for_index(target_index)
    active_bounds = [geom.range_for_index(j)[0]
                     for j in active_indices if j <= source_index]
    breakpoints = set(_residue_points(lo, hi, interval_a, active_bounds))
    breakpoints.update(_residue_points(lo, hi, interval_b, active_bounds))
    breakpoints.update(_residue_points(
        lo, hi, interval_new, [target_lo, target_hi + 1]))
    if len(breakpoints) > class_guard:
        raise ForensicConflict(
            "源桶 %d 的位置类断点 %d 超过护栏 %d（桶宽 %d，"
            "间隔 %d/%d/%d，活动桶 %d 个）：需追加该桶的交接单确认记录"
            % (source_index, len(breakpoints), class_guard,
               hi - lo + 1, interval_a, interval_b, interval_new,
               len(active_indices)))

    classes: List[PositionClass] = []
    cursor = lo
    for point in sorted(breakpoints):
        if point > cursor:
            classes.append(_make_class(
                geom, source_index, cursor, point - 1,
                interval_a, interval_b, interval_new,
                target_lo, target_hi))
        cursor = point
    classes.append(_make_class(
        geom, source_index, cursor, hi,
        interval_a, interval_b, interval_new,
        target_lo, target_hi))
    return classes


def _make_class(geom: BucketGeometry, source_index: int,
                block_lo: int, block_hi: int,
                interval_a: int, interval_b: int, interval_new: int,
                target_lo: int, target_hi: int) -> PositionClass:
    representative = block_lo
    return PositionClass(
        representative, block_hi - block_lo + 1,
        chain_bucket_vector(geom, representative, interval_a,
                            source_index + 1),
        chain_bucket_vector(geom, representative, interval_b,
                            source_index + 1),
        chain_count_in_range(representative, interval_new, target_lo,
                             min(target_hi, representative)))


# ---------------------------------------------------------------------------
# 结论数据结构
# ---------------------------------------------------------------------------

@dataclass
class ReRecording:
    '''一份可在旧两档逐桶核回的原入口重录方案。'''
    target_index: int
    entries: Dict[int, int]
    target_count: int


@dataclass
class BucketVerdict:
    '''目标桶结论。outcomes 至多含最小、最大各一份见证重录方案。'''
    target_index: int
    minimum: int
    maximum: int
    outcomes: List[ReRecording] = field(default_factory=list)

    @property
    def determined(self) -> bool:
        return self.minimum == self.maximum


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------

class ForensicPlanner:

    def __init__(self,
                 snapshot_a: Snapshot,
                 snapshot_b: Snapshot,
                 real_request_count: int,
                 new_interval: int,
                 evidence: Optional[Sequence[Tuple[int, int]]] = None,
                 class_guard: int = 200000,
                 node_guard: int = 2_000_000):
        self.a = snapshot_a
        self.b = snapshot_b
        self.n = int(real_request_count)
        self.i_new = int(new_interval)
        self.class_guard = class_guard
        self.node_guard = node_guard
        self._current_target = -1
        self._active: List[int] = []
        self._nodes = 0
        self._validate_basics()
        self.geom = self.a.geometry
        self.stored_len = max(self.a.stored_len, self.b.stored_len)
        self.evidence_map = self._collect_evidence(evidence or [])
        self.residual_a: Dict[int, int] = {
            i: self.a.count_at(i) for i in range(self.a.stored_len)}
        self.residual_b: Dict[int, int] = {
            i: self.b.count_at(i) for i in range(self.b.stored_len)}
        self._pre_subtract_evidence()

    # -- 校验 ----------------------------------------------------------------

    def _validate_basics(self) -> None:
        ga, gb = self.a.geometry, self.b.geometry
        if (ga.significant_figures != gb.significant_figures
                or ga.highest_trackable_value != gb.highest_trackable_value
                or ga.counts_len != gb.counts_len):
            raise ForensicConflict("两份快照分辨率/量程配置不一致，无法同桶核对")
        if min(self.a.interval, self.b.interval, self.i_new) <= 0:
            raise ForensicConflict("期望间隔必须为正整数")
        if self.n <= 0:
            raise ForensicConflict("真实请求总数必须为正")
        # 两档间隔不同 => 补点链长度不同 => 两份总计数天然不相等，
        # 唯一硬关系是每份总计数 >= 真实请求数（每请求至少写自身 1 点）。
        if self.n > min(self.a.total(), self.b.total()):
            raise ForensicConflict(
                "真实请求总数 %d 大于旧材料总计数（%s=%d, %s=%d），"
                "补点只可能让总计数更多"
                % (self.n, self.a.label, self.a.total(),
                   self.b.label, self.b.total()))

    def _collect_evidence(self, evidence: Sequence[Tuple[int, int]]
                          ) -> Dict[int, int]:
        fixed: Dict[int, int] = {}
        total = 0
        for value, mult in evidence:
            if value < 0 or mult <= 0:
                raise ForensicConflict(
                    "确认记录非法：value=%d multiplicity=%d" % (value, mult))
            if value > self.geom.highest_trackable_value:
                raise ForensicConflict(
                    "确认原值 %d 超出 histogram 量程 %d"
                    % (value, self.geom.highest_trackable_value))
            own_index = self.geom.index_for(value)
            for snap in (self.a, self.b):
                if own_index >= snap.stored_len:
                    raise ForensicConflict(
                        "确认原值 %d 的自身桶 %d 落在 %s 尾部缺省零区，"
                        "格式零照零读，不能承接该请求"
                        % (value, own_index, snap.label))
            fixed[value] = fixed.get(value, 0) + mult
            total += mult
        if total > self.n:
            raise ForensicConflict(
                "确认记录重数合计 %d 超过真实请求总数 %d" % (total, self.n))
        return fixed

    def _pre_subtract_evidence(self) -> None:
        for value, mult in self.evidence_map.items():
            for snap, residuals in (
                    (self.a, self.residual_a),
                    (self.b, self.residual_b)):
                vector = chain_bucket_vector(
                    self.geom, value, snap.interval, snap.stored_len)
                for index, per_entry in vector.items():
                    need = per_entry * mult
                    if residuals.get(index, 0) < need:
                        raise ForensicConflict(
                            "确认原值 %d（重数 %d）在 %s 桶 %d 核不回："
                            "需 %d 点，该桶仅剩 %d"
                            % (value, mult, snap.label, index, need,
                               residuals.get(index, 0)))
                    residuals[index] -= need

    # -- 目标桶分析 -----------------------------------------------------------

    def analyze_target(self, target_index: int) -> BucketVerdict:
        if not 0 <= target_index < self.geom.counts_len:
            raise ForensicConflict("目标桶 %d 越界" % target_index)
        target_lo, target_hi = self.geom.range_for_index(target_index)
        evidence_target = 0
        for value, mult in self.evidence_map.items():
            evidence_target += mult * chain_count_in_range(
                value, self.i_new, target_lo, min(target_hi, value))

        active_indices = [
            j for j in range(self.stored_len)
            if self.residual_a.get(j, 0) > 0
            or self.residual_b.get(j, 0) > 0]
        classes_by_bucket: Dict[int, List[PositionClass]] = {}
        for source in active_indices:
            classes_by_bucket[source] = build_position_classes(
                self.geom, source,
                self.a.interval, self.b.interval, self.i_new,
                target_index, active_indices, self.class_guard)

        base_requests = sum(self.evidence_map.values())
        recordings: List[ReRecording] = []
        found: Dict[int, Dict[int, int]] = {}
        # 两趟分支定界：先求最小、再求最大，每趟只保留一份见证方案。
        for sense in (-1, 1):
            self._current_target = target_index
            self._nodes = 0
            self._active = active_indices
            self._sense = sense
            self._incumbent = None
            self._optimize(
                len(active_indices) - 1,
                dict(self.residual_a), dict(self.residual_b),
                base_requests, evidence_target,
                dict(self.evidence_map), classes_by_bucket, found)
            if self._incumbent is None:
                raise ForensicConflict(
                    "目标桶 %d：不存在同时满足两份旧材料的重录方案"
                    % target_index)
        for count, entries in sorted(found.items()):
            recordings.append(ReRecording(
                target_index=target_index,
                entries=dict(entries), target_count=count))
        return BucketVerdict(target_index,
                             min(found), max(found), recordings)

    # -- 分支定界 -------------------------------------------------------------

    def _optimize(self, pos: int,
                  residual_a: Dict[int, int],
                  residual_b: Dict[int, int],
                  used_requests: int, target_count: int,
                  allocation: Dict[int, int],
                  classes_by_bucket: Dict[int, List[PositionClass]],
                  found: Dict[int, Dict[int, int]]) -> None:
        self._nodes += 1
        if self._nodes > self.node_guard:
            raise ForensicConflict(
                "目标桶 %d 的可行重录组合超过节点护栏 %d："
                "现有证据不足以在预算内定界，请追加该区间原值的确认记录"
                % (self._current_target, self.node_guard))
        if pos < 0:
            if used_requests == self.n \
                    and all(v == 0 for v in residual_a.values()) \
                    and all(v == 0 for v in residual_b.values()):
                self._accept(target_count, allocation, found)
            return
        if used_requests > self.n:
            return
        # 每请求目标贡献界：用于剪枝（target_count 可 >1，取剩余类极值）。
        if not self._bound_ok(pos, target_count, used_requests,
                              classes_by_bucket):
            return
        bucket = self._active[pos]
        if residual_a.get(bucket, 0) <= 0 and residual_b.get(bucket, 0) <= 0:
            self._optimize(pos - 1, residual_a, residual_b,
                           used_requests, target_count, allocation,
                           classes_by_bucket, found)
            return
        classes = classes_by_bucket[bucket]
        self._optimize_classes(pos, 0, classes, residual_a, residual_b,
                               used_requests, target_count, allocation,
                               classes_by_bucket, found)

    def _bound_ok(self, pos: int, target_count: int,
                  used_requests: int,
                  classes_by_bucket: Dict[int, List[PositionClass]]) -> bool:
        if self._incumbent is None:
            return True
        remaining = self.n - used_requests
        if remaining < 0:
            return False
        extremes = [pc.target_count
                    for q in range(pos + 1)
                    for pc in classes_by_bucket[self._active[q]]]
        if not extremes:
            optimistic = target_count
        elif self._sense > 0:
            optimistic = target_count + remaining * max(extremes)
            if optimistic <= self._incumbent:
                return False
        else:
            optimistic = target_count + remaining * min(extremes)
            if optimistic >= self._incumbent:
                return False
        return True

    def _accept(self, target_count: int,
                allocation: Dict[int, int],
                found: Dict[int, Dict[int, int]]) -> None:
        better = self._incumbent is None or (
            target_count > self._incumbent if self._sense > 0
            else target_count < self._incumbent)
        if better:
            self._incumbent = target_count
            found[target_count] = dict(allocation)

    def _optimize_classes(self, bucket_pos: int, class_pos: int,
                          classes: List[PositionClass],
                          residual_a: Dict[int, int],
                          residual_b: Dict[int, int],
                          used_requests: int, target_count: int,
                          allocation: Dict[int, int],
                          classes_by_bucket: Dict[int, List[PositionClass]],
                          found: Dict[int, Dict[int, int]]) -> None:
        self._nodes += 1
        if self._nodes > self.node_guard:
            raise ForensicConflict(
                "目标桶 %d 的可行重录组合超过节点护栏 %d："
                "现有证据不足以在预算内定界，请追加该区间原值的确认记录"
                % (self._current_target, self.node_guard))
        bucket = self._active[bucket_pos]
        if class_pos == len(classes):
            if residual_a.get(bucket, 0) == 0 \
                    and residual_b.get(bucket, 0) == 0:
                self._optimize(bucket_pos - 1, residual_a, residual_b,
                               used_requests, target_count, allocation,
                               classes_by_bucket, found)
            return
        pc = classes[class_pos]
        own_a = pc.vector_a.get(bucket, 1)
        own_b = pc.vector_b.get(bucket, 1)
        cap = min(residual_a.get(bucket, 0) // own_a,
                  residual_b.get(bucket, 0) // own_b)
        for index, per_entry in pc.vector_a.items():
            cap = min(cap, residual_a.get(index, 0) // per_entry)
        for index, per_entry in pc.vector_b.items():
            cap = min(cap, residual_b.get(index, 0) // per_entry)
        cap = min(cap, self.n - used_requests)
        # cap==0 的类被其链上某个零容量桶强制为 0，直接跳过，
        # 避免它把真正可用的类顶离“末位整除直解”位置。
        if cap == 0:
            self._optimize_classes(
                bucket_pos, class_pos + 1, classes,
                residual_a, residual_b, used_requests, target_count,
                allocation, classes_by_bucket, found)
            return
        # 末位（最后一个 cap>0 的）类被自身桶两余数强制决定。
        last_usable = self._last_usable_pos(classes, class_pos + 1,
                                            residual_a, residual_b,
                                            used_requests, bucket)
        if class_pos == last_usable:
            left_a = residual_a.get(bucket, 0)
            left_b = residual_b.get(bucket, 0)
            if left_a % own_a == 0 and left_b % own_b == 0:
                mult = left_a // own_a
                if mult == left_b // own_b and 0 <= mult <= cap:
                    self._apply_class(pc, mult, residual_a, residual_b,
                                      allocation)
                    self._optimize_classes(
                        bucket_pos, class_pos + 1, classes,
                        residual_a, residual_b,
                        used_requests + mult,
                        target_count + mult * pc.target_count,
                        allocation, classes_by_bucket, found)
                    self._undo_class(pc, mult, residual_a, residual_b,
                                     allocation)
            return
        rest_a = [c.vector_a.get(bucket, 1) for c in classes[class_pos + 1:]
                  if self._class_cap(c, bucket, residual_a, residual_b,
                                     used_requests) > 0]
        rest_b = [c.vector_b.get(bucket, 1) for c in classes[class_pos + 1:]
                  if self._class_cap(c, bucket, residual_a, residual_b,
                                     used_requests) > 0]
        gcd_a = math.gcd(*rest_a) if rest_a else 1
        gcd_b = math.gcd(*rest_b) if rest_b else 1
        options = range(cap, -1, -1) if self._sense > 0 else range(cap + 1)
        for mult in options:
            rem_a = residual_a.get(bucket, 0) - mult * own_a
            rem_b = residual_b.get(bucket, 0) - mult * own_b
            if rem_a % gcd_a or rem_b % gcd_b:
                continue
            self._apply_class(pc, mult, residual_a, residual_b, allocation)
            self._optimize_classes(
                bucket_pos, class_pos + 1, classes,
                residual_a, residual_b,
                used_requests + mult,
                target_count + mult * pc.target_count,
                allocation, classes_by_bucket, found)
            self._undo_class(pc, mult, residual_a, residual_b, allocation)
            if self._incumbent is not None and not self._bound_ok(
                    bucket_pos, target_count, used_requests,
                    classes_by_bucket):
                break

    def _class_cap(self, pc: PositionClass, bucket: int,
                   residual_a: Dict[int, int],
                   residual_b: Dict[int, int],
                   used_requests: int) -> int:
        own_a = pc.vector_a.get(bucket, 1)
        own_b = pc.vector_b.get(bucket, 1)
        cap = min(residual_a.get(bucket, 0) // own_a,
                  residual_b.get(bucket, 0) // own_b)
        for index, per_entry in pc.vector_a.items():
            cap = min(cap, residual_a.get(index, 0) // per_entry)
        for index, per_entry in pc.vector_b.items():
            cap = min(cap, residual_b.get(index, 0) // per_entry)
        return min(cap, self.n - used_requests)

    def _last_usable_pos(self, classes: List[PositionClass],
                         start: int, residual_a: Dict[int, int],
                         residual_b: Dict[int, int],
                         used_requests: int, bucket: int) -> int:
        last = start - 1
        for k in range(start, len(classes)):
            if self._class_cap(classes[k], bucket, residual_a,
                               residual_b, used_requests) > 0:
                last = k
        return last

    @staticmethod
    def _apply_class(pc: PositionClass, mult: int,
                     residual_a: Dict[int, int],
                     residual_b: Dict[int, int],
                     allocation: Dict[int, int]) -> None:
        if mult == 0:
            return
        allocation[pc.representative] = \
            allocation.get(pc.representative, 0) + mult
        for index, per_entry in pc.vector_a.items():
            residual_a[index] = residual_a.get(index, 0) - per_entry * mult
        for index, per_entry in pc.vector_b.items():
            residual_b[index] = residual_b.get(index, 0) - per_entry * mult

    @staticmethod
    def _undo_class(pc: PositionClass, mult: int,
                    residual_a: Dict[int, int],
                    residual_b: Dict[int, int],
                    allocation: Dict[int, int]) -> None:
        if mult == 0:
            return
        allocation[pc.representative] -= mult
        if allocation[pc.representative] == 0:
            del allocation[pc.representative]
        for index, per_entry in pc.vector_a.items():
            residual_a[index] = residual_a.get(index, 0) + per_entry * mult
        for index, per_entry in pc.vector_b.items():
            residual_b[index] = residual_b.get(index, 0) + per_entry * mult

    # -- 真实回放核验 ---------------------------------------------------------

    def replay(self, entries: Dict[int, int], interval: int) -> Dict[int, int]:
        '''用真实 HdrHistogram.record_corrected_value 回放，返回稀疏桶计数。'''
        hist = HdrHistogram(1, self.geom.highest_trackable_value,
                            self.geom.significant_figures)
        for value, mult in entries.items():
            hist.record_corrected_value(value, interval, mult)
        return {i: hist.get_count_at_index(i)
                for i in range(hist.counts_len)
                if hist.get_count_at_index(i)}

    def verify_recording(self, recording: ReRecording) -> None:
        '''方案必须在旧两档逐桶核回，且新档目标桶计数与结论一致。'''
        for snap in (self.a, self.b):
            replayed = self.replay(recording.entries, snap.interval)
            for index in range(snap.stored_len):
                if replayed.get(index, 0) != snap.count_at(index):
                    raise ForensicConflict(
                        "回放与 %s 桶 %d 不符：回放 %d vs 旧桶 %d"
                        % (snap.label, index, replayed.get(index, 0),
                           snap.count_at(index)))
            for index, count in replayed.items():
                if index >= snap.stored_len and count:
                    raise ForensicConflict(
                        "回放把 %d 点写进 %s 尾部缺失零桶 %d"
                        % (count, snap.label, index))
        replayed_new = self.replay(recording.entries, self.i_new)
        actual = replayed_new.get(recording.target_index, 0)
        if actual != recording.target_count:
            raise ForensicConflict(
                "新档回放目标桶 %d 计数 %d 与方案声明 %d 不符"
                % (recording.target_index, actual,
                   recording.target_count))
