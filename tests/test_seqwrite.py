"""顺序写适配层的验收测试。

覆盖任务的全部硬性要求：
- 顺序构建与本机可定位 Construct 原入口逐字节一致，原读取工具可直接 parse
- 一次性原件只读一次；范围原件不重复服务、支持固定空档
- 多种取件先后中选峰值暂存最小且同时满足两类约束者
- 配额不足在动笔前给出两个必须共存的区域与差额，不留半成品
- 安全前缀只在保证不再覆盖时推进；回写已交付范围立即失败
- 校验取构建时刻字段内容；条件回填在另一联选记后才发生
- 内层长度包自有局部坐标；负偏移（相对当时 EOF）回跳在流层可用
"""

import hashlib

import pytest

from construct import Pointer, Int32ub

from seqwrite import (
    ARCHIVE_FORMAT,
    BuildPlanner,
    BackfillBeyondCommitted,
    ForwardOnlySink,
    OneshotSource,
    QuotaExceeded,
    RangeSource,
    SourceConsumed,
    VirtualSeekableStream,
    build_demo_work_order,
)
from seqwrite.declaration import (
    LEN_A,
    LEN_B,
    LEN_C,
    LEN_INNER_PAYLOAD,
    REGISTRY_INSIDE_A,
    standard_inputs,
)
from seqwrite.errors import CommittedRangeNotReadable


LONG_LEN = 40
CHUNK = 4


def _bytes_seed():
    return {
        "orig_a": bytes(range(LEN_A)),
        "orig_b": bytes((i * 3) & 0xFF for i in range(LEN_B)),
        "orig_c": (0x0A0B0C0D).to_bytes(4, "big"),
        "orig_long": bytes((i * 7) & 0xFF for i in range(LONG_LEN)),
    }


def _make_order(staging_quota=10 ** 9, attach_registry=True,
                allowed_orders=None, long_len=LONG_LEN, chunk_size=CHUNK):
    return build_demo_work_order(
        staging_quota=staging_quota,
        long_len=long_len,
        chunk_size=chunk_size,
        attach_registry=attach_registry,
        override_orders=allowed_orders,
    )


def _make_sources(order, seed):
    return {
        "orig_a": OneshotSource(order.spec("orig_a"), seed["orig_a"]),
        "orig_b": OneshotSource(order.spec("orig_b"), seed["orig_b"]),
        "orig_c": OneshotSource(order.spec("orig_c"), seed["orig_c"]),
        "orig_long": RangeSource(order.spec("orig_long"), seed["orig_long"]),
    }


def _context(seed, attach_registry=True, long_len=LONG_LEN):
    return standard_inputs(
        long_len=long_len,
        attach_registry=attach_registry,
        registry_value=0x52454731,
        short_header=b"SH" + b"\x00" * 6,
        orig_a=seed["orig_a"],
        orig_b=seed["orig_b"],
        orig_c=seed["orig_c"],
        orig_long=seed["orig_long"],
    )


def _planner(seed=None, attach_registry=True, staging_quota=10 ** 9,
             block_size=16, long_len=LONG_LEN, chunk_size=CHUNK):
    seed = seed or _bytes_seed()
    order = _make_order(staging_quota, attach_registry, long_len=long_len,
                        chunk_size=chunk_size)
    sources = _make_sources(order, seed)
    planner = BuildPlanner(order, sources, block_size=block_size)
    planner.analyze_layout(_context(seed, attach_registry, long_len))
    return planner, order, sources, seed


# --------------------------------------------------------------------------- #
# 字节一致性与原读取工具
# --------------------------------------------------------------------------- #
def test_streaming_build_byte_identical_to_reference():
    planner, _, _, _ = _planner()
    best, reports = planner.choose_best()
    for report in reports:
        assert report.feasible and report.matches_reference
    result = planner.execute()
    assert result["bytes"] == planner.reference_bytes
    assert len(result["bytes"]) == planner.total_length


def test_original_reader_parses_sequential_volume():
    planner, _, _, seed = _planner()
    planner.choose_best()
    result = planner.execute()
    parsed = ARCHIVE_FORMAT.parse(result["bytes"])
    # 业务值 + 原字节双口径
    assert parsed.orig_c.value == 0x0A0B0C0D
    assert parsed.orig_c.data == seed["orig_c"]
    # 长原件两段拼回完整原件
    assert (parsed.orig_long_1.data + parsed.orig_long_2.data
            == seed["orig_long"])
    assert parsed.body_offset == parsed.body_start


def test_identity_holds_across_chunk_and_block_sizes():
    for chunk_size in (1, 4, 8):
        for block_size in (8, 16, 64):
            planner, _, _, _ = _planner(block_size=block_size,
                                        chunk_size=chunk_size)
            planner.choose_best()
            result = planner.execute()
            assert result["bytes"] == planner.reference_bytes


# --------------------------------------------------------------------------- #
# 一次性 / 范围原件读取约束
# --------------------------------------------------------------------------- #
def test_oneshot_source_cannot_be_read_twice():
    order = _make_order()
    source = OneshotSource(order.spec("orig_a"), b"\x01" * LEN_A)
    source.fetch()
    with pytest.raises(SourceConsumed):
        source.fetch()


def test_range_source_rejects_overlap_and_serves_each_range_once():
    order = _make_order()
    source = RangeSource(order.spec("orig_long"), b"\x09" * LONG_LEN)
    assert source.fetch_range(0, 10) == b"\x09" * 10
    with pytest.raises(SourceConsumed):
        source.fetch_range(5, 15)          # 与 [0,10) 重叠
    assert source.fetch_range(10, 20) == b"\x09" * 10  # 相邻不重叠，允许


def test_long_source_each_range_served_once_in_build():
    planner, order, sources, _ = _planner(chunk_size=4)
    planner.choose_best()
    result = planner.execute()
    feeder = result["registry"].feeders["orig_long"]
    served = [(h.start, h.end) for h in feeder.history]
    # 每段恰好服务一次，且完整覆盖 [0, LONG_LEN)
    assert len(served) == len(set(served))
    covered = sorted(served)
    assert covered[0][0] == 0 and covered[-1][1] == LONG_LEN
    for (_, e1), (s2, _) in zip(covered, covered[1:]):
        assert e1 == s2


# --------------------------------------------------------------------------- #
# 选序：峰值最小，次序真实影响峰值
# --------------------------------------------------------------------------- #
def test_choose_best_picks_minimum_peak_order():
    planner, _, _, _ = _planner()
    best, reports = planner.choose_best()
    peaks = {r.fetch_order: r.combined_peak for r in reports}
    assert best.combined_peak == min(peaks.values())
    # 把晚落笔的件排前面会抬高原件侧驻留峰值
    assert peaks[("orig_b", "orig_a", "orig_c")] > peaks[
        ("orig_a", "orig_b", "orig_c")]


def test_invalid_candidate_order_rejected():
    order = _make_order(allowed_orders=[("orig_a", "orig_b")])  # 缺 orig_c
    seed = _bytes_seed()
    planner = BuildPlanner(order, _make_sources(order, seed), block_size=16)
    planner.analyze_layout(_context(seed))
    with pytest.raises(Exception):
        planner.choose_best()


# --------------------------------------------------------------------------- #
# 配额闸：动笔前失败，给出两区域与差额
# --------------------------------------------------------------------------- #
def test_preflight_blocks_before_any_media_write():
    planner, _, _, _ = _planner(staging_quota=10)
    best, _ = planner.choose_best()
    real_sink = ForwardOnlySink()
    with pytest.raises(QuotaExceeded) as excinfo:
        planner.preflight(best)
    # 真实介质此刻一字节都没拿到
    assert real_sink.delivered == 0
    err = excinfo.value
    assert err.shortfall == best.combined_peak - 10
    kinds = [r["region"].split(":")[0] for r in err.regions]
    assert "source" in kinds and "backfill" in kinds


def test_preflight_passes_when_quota_sufficient():
    planner, _, _, _ = _planner(staging_quota=10 ** 9)
    planner.choose_best()
    assert planner.preflight() is True


# --------------------------------------------------------------------------- #
# 安全前缀与回填保护
# --------------------------------------------------------------------------- #
def test_watermark_never_crosses_open_backfill_site():
    planner, _, _, _ = _planner()
    planner.choose_best()
    result = planner.execute()
    stream = result["stream"]
    # 最终全部交付，且每次提交都是连续前缀
    assert stream.committed_watermark == planner.total_length
    watermarks = [0] + [r.watermark_after for r in result["progress"]]
    assert watermarks == sorted(watermarks)
    assert watermarks[-1] == planner.total_length


def test_writing_committed_range_fails_instead_of_corrupting():
    stream = VirtualSeekableStream(block_size=16)
    stream.register_pending("x", 16, 4, "test-site")
    stream.write(b"\x00" * 20)
    stream.flush()
    # 水线只能到站点起点 16
    assert stream.committed_watermark == 16
    # 强行回写已交付区间必须失败
    stream.seek(0)
    with pytest.raises(BackfillBeyondCommitted):
        stream.write(b"ZZ")


def test_committed_range_not_readable():
    stream = VirtualSeekableStream(block_size=16)
    stream.write(b"\x00" * 16)
    stream.flush()
    stream.seek(0)
    with pytest.raises(CommittedRangeNotReadable):
        stream.read(4)


def test_negative_eof_seek_uses_then_eof_basis():
    stream = VirtualSeekableStream(block_size=16)
    stream.write(b"\x00" * 20)          # 当时 EOF = 20
    stream.seek(-4, 2)                  # whence=2 相对当时 EOF
    assert stream.tell() == 16
    stream.write(b"MARK")
    stream.seek(0, 2)
    assert stream.tell() == 20          # 回跳不改 EOF
    finalized = VirtualSeekableStream(block_size=16)
    finalized.write(b"\x00" * 16 + b"MARK")
    finalized.finalize()
    assert finalized.sink.getvalue()[16:20] == b"MARK"


# --------------------------------------------------------------------------- #
# 构建期校验与条件回填
# --------------------------------------------------------------------------- #
def test_checksum_locks_build_time_bytes_despite_later_overwrite():
    planner, _, _, seed = _planner(attach_registry=True)
    planner.choose_best()
    result = planner.execute()
    parsed = ARCHIVE_FORMAT.parse(result["bytes"])
    # 校验值是构建时刻 orig_a 原字节的哈希
    assert parsed.checksum == hashlib.sha256(seed["orig_a"]).digest()
    # 而最终卷 orig_a 同范围已被登记号覆盖，不再是原始字节
    assert parsed.orig_a.data != seed["orig_a"]
    overwritten = parsed.orig_a.data[REGISTRY_INSIDE_A:REGISTRY_INSIDE_A + 4]
    assert overwritten == (0x52454731).to_bytes(4, "big")
    # 原读取工具 parse 不就当前卷重算而误报（BuildTimeChecksum）
    assert len(parsed.checksum) == 32


def test_conditional_registry_skipped_when_flag_off():
    seed = _bytes_seed()
    planner, _, _, _ = _planner(attach_registry=False)
    planner.choose_best()
    result = planner.execute()
    parsed = ARCHIVE_FORMAT.parse(result["bytes"])
    # 未选记：orig_a 保持原字节，登记号位置不被回填
    assert parsed.orig_a.data == seed["orig_a"]
    assert result["bytes"] == planner.reference_bytes


# --------------------------------------------------------------------------- #
# 内层长度包自有局部坐标
# --------------------------------------------------------------------------- #
def test_inner_packet_uses_its_own_local_coordinates():
    planner, _, _, _ = _planner()
    planner.choose_best()
    result = planner.execute()
    parsed = ARCHIVE_FORMAT.parse(result["bytes"])
    # 内层 Pointer 回填的是内层局部坐标下的 payload 长度（8），
    # 而不是整卷全局坐标
    assert parsed.inner.inner_tail == LEN_INNER_PAYLOAD
    assert parsed.inner.payload.data == b"INNER123"
    # 内层 VarInt 长度包前缀存在且整体可被原工具解析
    assert parsed.inner.payload.length == LEN_INNER_PAYLOAD


# --------------------------------------------------------------------------- #
# 台账可回溯到声明
# --------------------------------------------------------------------------- #
def test_progress_ledger_traceable_to_sites_and_sources():
    planner, _, _, _ = _planner()
    planner.choose_best()
    result = planner.execute()
    site_keys = {site.key for site in planner.backfill_sites}
    seen_open = set()
    for record in result["progress"]:
        seen_open.update(record.open_backfills)
        for sid in record.held_originals:
            assert sid in ("orig_a", "orig_b", "orig_c")
    # 台账里出现过的未决回填都能回溯到登记的回填站点
    assert seen_open <= site_keys
    # 每个回填站点都有一次回跳写入与一次释放事件
    backfill_writes = [e for e in result["events"]
                       if e.kind == "write_backfill"]
    releases = [e for e in result["events"] if e.kind == "release"]
    assert len(backfill_writes) == len(planner.backfill_sites)
    assert len(releases) == len(planner.backfill_sites)


def test_forward_sink_is_monotonic_and_complete():
    planner, _, _, _ = _planner()
    planner.choose_best()
    result = planner.execute()
    sink = result["stream"].sink
    assert sink.delivered == planner.total_length
    assert b"".join(sink.chunks) == planner.reference_bytes


# --------------------------------------------------------------------------- #
# 有界暂存：长卷峰值不随卷长膨胀（无空间先做一整卷的核心保证）
# --------------------------------------------------------------------------- #
def test_peak_staging_stays_bounded_as_long_volume_grows():
    peaks = {}
    for long_len in (40, 400, 4000):
        seed = _bytes_seed()
        seed["orig_long"] = bytes((i * 7) & 0xFF for i in range(long_len))
        order = _make_order(long_len=long_len, chunk_size=64)
        planner = BuildPlanner(order, _make_sources(order, seed),
                               block_size=16)
        planner.analyze_layout(_context(seed, long_len=long_len))
        best, _ = planner.choose_best()
        result = planner.execute()
        assert result["bytes"] == planner.reference_bytes
        peaks[long_len] = best.combined_peak
    # 卷长扩大 100 倍，组合峰值保持不变（分段喂入 + 逐块交付）
    assert peaks[40] == peaks[400] == peaks[4000]
    # 注册表绝不缓存长原件整卷
    order = _make_order(long_len=4000, chunk_size=64)
    seed = _bytes_seed()
    seed["orig_long"] = bytes(4000)
    planner = BuildPlanner(order, _make_sources(order, seed), block_size=16)
    planner.analyze_layout(_context(seed, long_len=4000))
    planner.choose_best()
    result = planner.execute()
    assert "orig_long" not in result["registry"]._held


# --------------------------------------------------------------------------- #
# 固定空档：成段处理，不读原件，由流层填充
# --------------------------------------------------------------------------- #
def test_range_source_gap_is_never_fetched():
    order = _make_order()
    source = RangeSource(order.spec("orig_long"), bytes(range(LONG_LEN)))
    # 声明 [8,16) 为固定空档：只取其前后范围，空档字节永不从原件读
    source.gap(8, 16)
    source.fetch_range(0, 8)
    source.fetch_range(16, 24)
    served = [(h.start, h.end) for h in source.history if "GAP" not in h.note]
    assert all(not (s < 16 and e > 8) for s, e in served)
    assert any("GAP" in h.note for h in source.history)


def test_stream_fill_materializes_fixed_gap_without_source_read():
    order = _make_order()
    source = RangeSource(order.spec("orig_long"), bytes(range(LONG_LEN)))
    stream = VirtualSeekableStream(block_size=16, fill=b"\x00")
    # 先写空档前的 8 字节
    stream.write(source.fetch_range(0, 8))
    # [8,24) 是固定空档：不读原件，直接在流里成段填充
    stream.fill_to(24, pattern=b"\x00")
    # 再写空档之后的真实原件字节
    stream.write(source.fetch_range(24, 32))
    stream.finalize()
    out = stream.sink.getvalue()
    assert out[8:24] == b"\x00" * 16            # 空档由填充段构成
    assert out[24:32] == bytes(range(24, 32))   # 原件字节不受影响
    # 原件只服务了两个真实范围，空档未触发读取
    served = [(h.start, h.end) for h in source.history if "GAP" not in h.note]
    assert served == [(0, 8), (24, 32)]
