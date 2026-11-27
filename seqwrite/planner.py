"""静态排程、峰值暂存核算、安全前缀推进与配额预检。

排程器不臆测字节布局：先用本机可定位的 Construct 原入口（标准格式 build
进 ``BytesIO``）跑出规范字节与全部逻辑偏移，再让每个候选取件次序用顺序
格式在 :class:`~seqwrite.vstream.VirtualSeekableStream` 上做一次全真构建，
在每个取数/回填事件点采样"原件侧持有 + 输出侧暂存"的组合占用，取峰值。

选序准则：候选必须同时满足工单取件排列与声明原生落笔次序，再取组合峰值
最小者。配额闸在任何字节落介质前生效。
"""

from dataclasses import dataclass, field

from .errors import QuotaExceeded, UnschedulableOrder
from .vstream import VirtualSeekableStream
from .sources import OneshotSource, RangeSource
from .fetchers import FeederRegistry
from .declaration import (
    ARCHIVE_FORMAT, build_format, SHORT_HEADER_LEN, REGISTRY_INSIDE_A,
)


@dataclass
class BackfillSite:
    """一个回填站点及其坐标来源（可回溯到声明字段）。"""

    key: str
    start: int
    length: int
    coordinate_basis: str      # constant | tell_field | current_pos | eof_then
    reason: str
    conditional: bool = False


@dataclass
class StagingRequirement:
    """某个候选次序的暂存需求结论。"""

    fetch_order: tuple
    output_peak: int           # 输出侧已写未交付峰值
    held_peak: int             # 原件侧已取未落笔峰值
    combined_peak: int         # 同时刻两者之和的最大值
    feasible: bool
    matches_reference: bool
    note: str = ""
    peak_instant: dict = field(default_factory=dict)
    co_resident_at_peak: list = field(default_factory=list)


@dataclass
class ProgressRecord:
    """开工后每个可回溯检查点：推过的位置、暂存原件、未决回填原因。"""

    step: int
    watermark_before: int
    watermark_after: int
    delivered_ranges: tuple
    held_originals: list
    held_bytes: int
    open_backfills: list
    event_index: int


class BuildPlanner:
    """对一张工单做布局分析、选序、配额预检并驱动实际顺序构建。"""

    def __init__(self, work_order, sources, block_size=1 << 12):
        self.order = work_order
        self.block_size = block_size
        # 快照不可变原件数据；每次评估/执行都据此造全新源，
        # 因此同一份原件在任意一次真实构建里仍然只被读取一次。
        self._data = {sid: src.data for sid, src in sources.items()}
        self._specs = {sid: src.spec for sid, src in sources.items()}
        self._is_ranged = {
            sid: isinstance(src, RangeSource) for sid, src in sources.items()
        }
        self._layout = None
        self._reference_bytes = None
        self._total_length = None
        self._backfill_sites = None
        self._reports = None
        self._best = None

    def _fresh_sources(self):
        """每轮构建用的全新一次性/范围源（读取计数归零）。"""
        oneshots, feeders = {}, {}
        for sid, data in self._data.items():
            if self._is_ranged[sid]:
                feeders[sid] = RangeSource(self._specs[sid], data)
            else:
                oneshots[sid] = OneshotSource(self._specs[sid], data)
        return oneshots, feeders

    # ------------------------------------------------------------------ #
    # 布局：本机可定位 Construct 原入口，取规范字节与偏移
    # ------------------------------------------------------------------ #
    def analyze_layout(self, context_inputs):
        ref = ARCHIVE_FORMAT.build(context_inputs)
        # 头部字段自描述，解析无需额外 context
        parsed = ARCHIVE_FORMAT.parse(ref)
        self._reference_bytes = ref
        self._total_length = len(ref)
        self._layout = self._extract_layout(parsed)
        self._backfill_sites = self._extract_backfill_sites(parsed)
        return self._layout

    @staticmethod
    def _extract_layout(parsed):
        layout = {"fields": {}, "rawcopy": {}}
        for key, value in parsed.items():
            if isinstance(value, int):
                layout["fields"][key] = value
            elif isinstance(value, dict) and "offset1" in value:
                layout["rawcopy"][key] = {
                    "offset1": value.offset1,
                    "offset2": value.offset2,
                    "length": value.length,
                }
        return layout

    def _extract_backfill_sites(self, parsed):
        sites = [
            BackfillSite("body_offset", parsed.body_offset_pos, 4,
                         "tell_field", "Pointer(body_offset_pos) 回填 body 起始"),
            BackfillSite("checksum", parsed.checksum_pos, 32,
                         "tell_field", "Checksum 取构建时刻 orig_a.data"),
            BackfillSite("registry",
                         parsed.orig_a_start + REGISTRY_INSIDE_A, 4,
                         "constant", "补在原件 A 内部固定偏移的登记号",
                         conditional=True),
            BackfillSite("eof_mark", parsed.eof_mark_pos, 4,
                         "tell_field",
                         "落点为头部绝对槽位；写入值=当时卷末 eof_now（负偏移回跳见流层专测）"),
            BackfillSite("short_header", parsed.short_header_pos,
                         SHORT_HEADER_LEN, "tell_field",
                         "长卷途中跳写的短头"),
        ]
        return tuple(sites)

    @property
    def reference_bytes(self):
        return self._reference_bytes

    @property
    def total_length(self):
        return self._total_length

    @property
    def layout(self):
        return self._layout

    @property
    def backfill_sites(self):
        return tuple(self._backfill_sites)

    # ------------------------------------------------------------------ #
    # 一次全真顺序构建（评估与执行共用）
    # ------------------------------------------------------------------ #
    def _register_sites(self, stream):
        for site in self._backfill_sites:
            if site.conditional and not self.order.attach_registry:
                continue
            stream.register_pending(
                site.key, site.start, site.length,
                "%s|%s" % (site.coordinate_basis, site.reason),
            )

    def _make_inputs(self):
        from .declaration import INNER_PAYLOAD_DATA, VERSION
        return {
            "version": VERSION,
            "long_len": self.order.long_len,
            "attach_registry": 1 if self.order.attach_registry else 0,
            "registry_value": 0x52454731,          # "REG1"
            "short_header": b"SH" + bytes(SHORT_HEADER_LEN - 2),
            # 内层长度包仍是标准构件，喂同一份规范 payload
            "inner": {"payload": {"data": INNER_PAYLOAD_DATA}},
            # 原件 RawCopy 走 data 实取（SourceBackedRawCopy 忽略外部值，
            # 数据从受控源 JIT 取得）；长原件拆两段
            "orig_a": {"value": None},
            "orig_b": {"value": None},
            "orig_c": {"value": None},
            "orig_long_1": {"value": None},
            "orig_long_2": {"value": None},
        }

    def _run(self, fetch_order, trace=False):
        streaming = build_format(
            streaming=True, chunk_size=self.order.chunk_size
        )
        stream = VirtualSeekableStream(block_size=self.block_size)
        self._register_sites(stream)

        oneshots, feeders = self._fresh_sources()
        sampler = _PeakSampler(self)
        progress = []
        holder = {}

        def checkpoint(kind, sid, evt_stream):
            self._on_event(kind, stream, holder.get("registry"),
                           sampler, progress, trace)

        registry = FeederRegistry(
            oneshots, feeders, fetch_order, sampler=sampler,
            checkpoint=checkpoint,
        )
        holder["registry"] = registry

        # 回填站点被覆盖时采样；trace 时补一条检查点
        def on_write(start, end, is_backfill):
            sampler.sample(registry, stream)
            if trace:
                self._append_progress(stream, registry, progress, ())
        stream.on_write = on_write

        inputs = self._make_inputs()
        inputs["feeder_registry"] = registry

        streaming.build_stream(inputs, stream)
        final_ranges = stream.finalize()
        if trace:
            self._append_progress(stream, registry, progress, final_ranges)

        produced = stream.sink.getvalue()
        matches = produced == self._reference_bytes
        return {
            "stream": stream,
            "registry": registry,
            "sampler": sampler,
            "produced": produced,
            "matches_reference": matches,
            "progress": tuple(progress),
            "fresh_oneshots": oneshots,
            "fresh_feeders": feeders,
        }

    def _on_event(self, kind, stream, registry, sampler, progress, trace):
        # 每次取数/回填事件都尝试推进安全前缀，得到真实输出侧暂存
        ranges = stream.flush()
        if registry is not None:
            sampler.sample(registry, stream)
        if trace and (ranges or kind in ("acquired", "backfill_done")):
            self._append_progress(stream, registry, progress, ranges)

    # ------------------------------------------------------------------ #
    # 选序
    # ------------------------------------------------------------------ #
    def evaluate_order(self, fetch_order):
        expected = sorted(s.source_id for s in self.order.oneshot_specs())
        if sorted(fetch_order) != expected:
            raise UnschedulableOrder("候选次序未恰好覆盖全部一次性原件",
                                     fetch_order)
        try:
            run = self._run(fetch_order)
        except Exception as exc:
            return StagingRequirement(tuple(fetch_order), 0, 0, 0, False,
                                      False, note="构建失败：%r" % exc)
        sampler = run["sampler"]
        return StagingRequirement(
            fetch_order=tuple(fetch_order),
            output_peak=run["stream"].peak_staged,
            held_peak=run["registry"].peak_held,
            combined_peak=sampler.combined_peak,
            feasible=True,
            matches_reference=run["matches_reference"],
            peak_instant=sampler.peak_snapshot,
            co_resident_at_peak=sampler.peak_regions,
        )

    def choose_best(self):
        reports = [self.evaluate_order(o) for o in self.order.allowed_fetch_orders]
        feasible = [r for r in reports
                    if r.feasible and r.matches_reference]
        if not feasible:
            raise UnschedulableOrder(
                "没有任何候选次序能产出与标准格式一致的卷",
                self.order.allowed_fetch_orders,
            )
        best = min(feasible,
                   key=lambda r: (r.combined_peak, r.fetch_order))
        self._reports = reports
        self._best = best
        return best, reports

    # ------------------------------------------------------------------ #
    # 配额闸：动笔前生效，明确两个必须同时放下的区域与差额
    # ------------------------------------------------------------------ #
    def preflight(self, best=None):
        best = best or self._best
        quota = self.order.staging_quota
        if best.combined_peak <= quota:
            return True

        def region_size(reg):
            if reg["region"].startswith("source:"):
                return reg.get("held_bytes", 0)
            s, e = reg["span"]
            return e - s

        source_regions = [r for r in best.co_resident_at_peak
                          if r["region"].startswith("source:")]
        backfill_regions = [r for r in best.co_resident_at_peak
                            if r["region"].startswith("backfill:")]
        two = []
        if source_regions:
            two.append(max(source_regions, key=region_size))
        if backfill_regions:
            two.append(max(backfill_regions, key=region_size))
        # 若某一类缺失，用剩余区域补齐到两个
        if len(two) < 2:
            for reg in sorted(best.co_resident_at_peak,
                              key=region_size, reverse=True):
                if reg not in two:
                    two.append(reg)
                    if len(two) == 2:
                        break
        raise QuotaExceeded(
            best.combined_peak, quota, two,
            detail="取件次序=%r；峰值瞬间一个原件持有区与一个阻断水线的"
                   "回填区必须同时驻留。" % (best.fetch_order,),
        )

    # ------------------------------------------------------------------ #
    # 实际执行：可回溯推进台账 + 最终顺序字节
    # ------------------------------------------------------------------ #
    def execute(self):
        if self._best is None:
            self.choose_best()
        self.preflight()
        run = self._run(self._best.fetch_order, trace=True)
        if not run["matches_reference"]:
            raise AssertionError("顺序构建产物与标准格式不一致，拒绝交付")
        return {
            "bytes": run["produced"],
            "stream": run["stream"],
            "registry": run["registry"],
            "progress": run["progress"],
            "events": run["stream"].events,
            "best": self._best,
            "reports": self._reports,
        }

    def _append_progress(self, stream, registry, progress, extra_ranges):
        after = stream.committed_watermark
        held = registry.held_snapshot()
        held_bytes = registry.held_bytes()
        open_backfills = [site[0] for site in stream.pending_sites()]
        delivered = tuple(extra_ranges)
        # 去重：水线、持有原件、未决回填、交付区间都没变化则不入账
        if progress:
            last = progress[-1]
            if (last.watermark_after == after
                    and last.held_originals == held
                    and last.open_backfills == open_backfills
                    and not delivered):
                return
        before = progress[-1].watermark_after if progress else 0
        progress.append(ProgressRecord(
            step=len(progress),
            watermark_before=before,
            watermark_after=after,
            delivered_ranges=delivered,
            held_originals=held,
            held_bytes=held_bytes,
            open_backfills=open_backfills,
            event_index=len(stream.events),
        ))


class _PeakSampler:
    """在每个取数/回填事件点采样组合暂存，并定格峰值瞬间的共存区域。"""

    def __init__(self, planner):
        self.planner = planner
        self.combined_peak = 0
        self.peak_snapshot = {}
        self.peak_regions = []

    def _source_span(self, sid):
        rc = self.planner._layout["rawcopy"].get(sid)
        if rc:
            return (rc["offset1"], rc["offset2"])
        return None

    def _regions(self, registry, stream):
        regions = []
        for sid, data in sorted(registry._held.items()):
            regions.append({
                "region": "source:%s" % sid,
                "held_bytes": len(data),
                "span": self._source_span(sid),
            })
        for key, (s, e, reason) in sorted(
                stream._pending.items(), key=lambda kv: kv[1][0]):
            regions.append({
                "region": "backfill:%s" % key,
                "span": (s, e),
                "reason": reason,
            })
        return regions

    def sample(self, registry, stream):
        combined = registry.held_bytes() + stream.staged_bytes
        if combined > self.combined_peak:
            self.combined_peak = combined
            self.peak_snapshot = {
                "held_bytes": registry.held_bytes(),
                "output_staged": stream.staged_bytes,
                "watermark": stream.committed_watermark,
                "logical_eof": stream.logical_eof,
            }
            self.peak_regions = self._regions(registry, stream)
