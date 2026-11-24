'''
交接规划器（hdrh.forensics）测试。

覆盖：
  - 补点链闭式计数与逐点枚举、与库 record_corrected_value 语义一致
    （含题面 35/10 -> 35,25,15,5 与 35/20 -> 35,15）；
  - BucketGeometry 的桶区间与真实 HdrHistogram 索引映射一致；
  - 精确场景：目标桶计数唯一可定，且方案可在旧两档逐桶回放核回；
  - 两结果歧义：min/max 差 1，两份重录方案分别达到两端且都核回；
  - 证据矛盾、尾部缺省零、配置不一致等冲突精确指到桶/确认记录；
  - 上亿重数不逐个展开，规划仍在常数级算术内完成。
'''
import time

import pytest

from hdrh.histogram import HdrHistogram
from hdrh.forensics import BucketGeometry
from hdrh.forensics import ForensicConflict
from hdrh.forensics import ForensicPlanner
from hdrh.forensics import Snapshot
from hdrh.forensics import chain_count_in_range
from hdrh.forensics import corrected_chain_length


HIGHEST = 4095
SIG = 2
I1, I2, INEW = 10, 20, 15


def _explicit_chain(value, interval):
    if interval <= 0 or value <= 0:
        return [value]
    return [value - q * interval for q in range(corrected_chain_length(value, interval))]


def _snapshots(originals, interval_a=I1, interval_b=I2,
               interval_new=INEW, highest=HIGHEST, sig=SIG):
    def make(interval):
        hist = HdrHistogram(1, highest, sig)
        for value, mult in originals.items():
            hist.record_corrected_value(value, interval, mult)
        return hist
    hist_a = make(interval_a)
    hist_b = make(interval_b)
    hist_new = make(interval_new)
    snap_a = Snapshot.from_encoded(hist_a.encode(), interval_a, "device")
    snap_b = Snapshot.from_encoded(hist_b.encode(), interval_b, "desk")
    return snap_a, snap_b, hist_new


# ---------------------------------------------------------------------------
# 补点链语义
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_corrected_chain_matches_prompt_example():
    # 题面短例：原值 35、间隔 10 记录 35/25/15/5；间隔 20 只再记 15。
    assert _explicit_chain(35, 10) == [35, 25, 15, 5]
    assert _explicit_chain(35, 20) == [35, 15]
    assert corrected_chain_length(35, 10) == 4
    assert corrected_chain_length(35, 20) == 2


@pytest.mark.basic
@pytest.mark.parametrize("value,interval",
                         [(35, 10), (35, 20), (100, 7), (1000, 3),
                          (10, 10), (7, 20), (1, 10)])
def test_chain_closed_form_matches_enumeration(value, interval):
    chain = _explicit_chain(value, interval)
    for lo, hi in [(0, 9), (10, 19), (0, value), (20, 34),
                   (value, value), (5, 25), (1, interval)]:
        got = chain_count_in_range(value, interval, lo, hi)
        want = sum(1 for point in chain if lo <= point <= hi)
        assert got == want


@pytest.mark.basic
def test_chain_matches_library_total_counts():
    hist = HdrHistogram(1, 1_000_000, 3)
    for value, interval in [(35, 10), (35, 20), (100, 10), (0, 10),
                            (1000, 3), (99, 7)]:
        hist.reset()
        hist.record_corrected_value(value, interval)
        assert hist.get_total_count() == corrected_chain_length(value, interval)
        assert chain_count_in_range(value, interval, 0, value) == \
            hist.get_total_count()


# ---------------------------------------------------------------------------
# 桶几何
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_geometry_ranges_match_real_indices():
    hist = HdrHistogram(1, HIGHEST, SIG)
    geom = BucketGeometry.from_hdr(hist)
    for value in range(HIGHEST + 1):
        index = hist.get_counts_array_index(value)
        lo, hi = geom.range_for_index(index)
        assert lo <= value <= hi
        assert geom.value_from_index(index) == \
            hist.get_lowest_equivalent_value(value)


# ---------------------------------------------------------------------------
# 精确场景
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_exact_verdicts_bracket_true_counts_and_replay():
    originals = {35: 1, 100: 2, 7: 3}
    snap_a, snap_b, hist_new = _snapshots(originals)
    planner = ForensicPlanner(snap_a, snap_b, sum(originals.values()), INEW)
    for target in range(snap_a.stored_len):
        verdict = planner.analyze_target(target)
        actual = hist_new.get_count_at_index(target)
        assert verdict.minimum <= actual <= verdict.maximum
        for recording in verdict.outcomes:
            planner.verify_recording(recording)
            assert sum(recording.entries.values()) == sum(originals.values())


@pytest.mark.basic
def test_determined_bucket_emits_single_value():
    originals = {7: 3}
    snap_a, snap_b, hist_new = _snapshots(originals)
    planner = ForensicPlanner(snap_a, snap_b, 3, INEW)
    verdict = planner.analyze_target(7)
    assert verdict.determined
    assert verdict.minimum == verdict.maximum == 3
    assert len(verdict.outcomes) == 1
    planner.verify_recording(verdict.outcomes[0])


# ---------------------------------------------------------------------------
# 两结果歧义
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_two_outcome_ambiguity_is_replay_verified():
    # 旧两档无法区分 {152,203} 与 {155,200}，但新档 I=20 在桶 3 上
    # 分别给出 1 与 0，形成 min=0/max=1 的两结果。
    originals = {152: 1, 203: 1}
    snap_a, snap_b, hist_new = _snapshots(
        originals, interval_a=8, interval_b=12, interval_new=20,
        highest=255, sig=1)
    planner = ForensicPlanner(snap_a, snap_b, 2, 20)
    verdict = planner.analyze_target(3)
    assert (verdict.minimum, verdict.maximum) == (0, 1)
    assert not verdict.determined
    counts = sorted(r.target_count for r in verdict.outcomes)
    assert counts == [0, 1]
    assert hist_new.get_count_at_index(3) == 1
    for recording in verdict.outcomes:
        planner.verify_recording(recording)
        # 两份方案都必须在旧两档逐桶核回同样的桶计数。
        for snap in (snap_a, snap_b):
            replayed = planner.replay(recording.entries, snap.interval)
            for index in range(snap.stored_len):
                assert replayed.get(index, 0) == snap.count_at(index)


# ---------------------------------------------------------------------------
# 冲突与尾部零桶
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_conflicting_evidence_points_to_bucket():
    originals = {35: 1}
    snap_a, snap_b, _ = _snapshots(originals)
    with pytest.raises(ForensicConflict) as exc:
        ForensicPlanner(snap_a, snap_b, 1, INEW, evidence=[(30, 1)])
    assert "桶 30" in str(exc.value)


@pytest.mark.basic
def test_real_count_cannot_exceed_snapshot_totals():
    snap_a, snap_b, _ = _snapshots({35: 1})
    with pytest.raises(ForensicConflict):
        ForensicPlanner(snap_a, snap_b, 99, INEW)


@pytest.mark.basic
def test_tampered_snapshot_has_no_feasible_recording():
    snap_a, snap_b, _ = _snapshots({35: 1})
    tampered = dict(snap_a.counts)
    tampered[5] = 5
    bad = Snapshot(snap_a.geometry, tampered, snap_a.stored_len,
                   snap_a.interval, "tampered")
    planner = ForensicPlanner(bad, snap_b, 1, INEW)
    with pytest.raises(ForensicConflict):
        planner.analyze_target(5)


@pytest.mark.basic
def test_tail_truncated_zero_cannot_host_request():
    snap_a, snap_b, _ = _snapshots({35: 1})
    truncated = Snapshot(snap_a.geometry, dict(snap_a.counts),
                         stored_len=20, interval=snap_a.interval,
                         label="truncated")
    assert truncated.count_at(50) == 0
    with pytest.raises(ForensicConflict) as exc:
        ForensicPlanner(truncated, snap_b, 1, INEW, evidence=[(35, 1)])
    assert "尾部缺省零" in str(exc.value)


@pytest.mark.basic
def test_geometry_mismatch_is_rejected():
    geom_a = BucketGeometry(SIG, HIGHEST)
    geom_b = BucketGeometry(1, 2047)
    snap_a = Snapshot.from_dense([1], geom_a, I1, "a")
    snap_b = Snapshot.from_dense([1], geom_b, I2, "b")
    with pytest.raises(ForensicConflict):
        ForensicPlanner(snap_a, snap_b, 1, INEW)


# ---------------------------------------------------------------------------
# 可扩展性：大重数、大补点链不逐个展开
# ---------------------------------------------------------------------------

@pytest.mark.basic
def test_large_multiplicity_is_constant_time_per_bucket():
    big = 50_000_000
    originals = {3000: big}
    snap_a, snap_b, hist_new = _snapshots(originals)
    planner = ForensicPlanner(snap_a, snap_b, big, INEW)
    start = time.time()
    verdict = planner.analyze_target(planner.geom.index_for(3000))
    elapsed = time.time() - start
    assert elapsed < 5
    assert verdict.determined
    assert verdict.minimum == big
    planner.verify_recording(verdict.outcomes[0])
    assert hist_new.get_count_at_value(3000) == big
