"""构建侧取数构件与取件注册表。

登记格式 :data:`~seqwrite.declaration.ARCHIVE_FORMAT` 保持纯标准 Construct
（原读取工具直接 parse 的权威格式）。顺序构建时，把原件的 :class:`RawCopy`
替换为 :class:`SourceBackedRawCopy`：它严格走 RawCopy 的 **data 实取口径**
（取原始字节直写、不回读），只是数据来源换成受控的原件源，从而：

- 写出的仍是原件原字节、定长同位，产物与标准格式逐字节一致；
- 不触发 RawCopy value 模式的"写完再 seek 回读"，因此不会触碰已交付、
  不可读的介质区间；
- 取数被精确延迟到 RawCopy 落笔时刻（JIT），一次性原件整段只取一次。

两类暂存分开记账，峰值取二者之和的最大值：

- 输出侧暂存：已写但未交付的块（:class:`~seqwrite.vstream.VirtualSeekableStream`）
- 原件侧暂存：已从一次性原件取得、尚未落笔的字节（:class:`FeederRegistry`）

取件排列如何影响峰值
--------------------
落笔顺序由声明固定（a→b→c→long）。落笔原件 X 前，注册表必须先取得
"所选排列中排在 X 之前（含 X）"的全部件。把晚落笔的件排得越靠前，它
越早进原件侧暂存、驻留越久，组合峰值越高。
"""

from construct import RawCopy, Container, stream_write


def find_registry(context):
    """沿 Struct 嵌套链（``_``）与根 ``_params`` 查找取件注册表。

    顶层输入键被 construct 放进最外层 Struct 的 context，而非 build_stream
    的根 context；内层 Prefixed 等又各自开新 context，故需逐层上溯。
    """
    seen = set()
    cur = context
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        registry = cur.get("feeder_registry") if hasattr(cur, "get") else None
        if registry is not None:
            return registry
        params = getattr(cur, "_params", None)
        if params is not None and params is not cur:
            registry = params.get("feeder_registry") if hasattr(params, "get") else None
            if registry is not None:
                return registry
        cur = getattr(cur, "_", None)
    return None


class SourceBackedRawCopy(RawCopy):
    """RawCopy 的顺序构建版：data 实取口径，数据源是受控原件。

    :param subcon: 标准模式下的内件（parse/value 语义沿用，如 Int32ub）
    :param source_id: 工单原件 id
    :param length: 该原件定长（int 或 context lambda）
    :param ranged: True 表示长原件，按 chunk 从 RangeSource 分段取
    :param chunk_size: 长原件分段粒度（固定空档可成段处理）
    """

    def __init__(self, subcon, source_id, length, ranged=False, chunk_size=4,
                 source_offset=0):
        super().__init__(subcon)
        self.source_id = source_id
        self.length = length
        self.ranged = ranged
        self.chunk_size = chunk_size
        # 长原件拆段时，本字段对应源内的绝对起始偏移
        self.source_offset = source_offset

    def _length(self, context):
        return self.length(context) if callable(self.length) else self.length

    def _source_offset(self, context):
        return (self.source_offset(context)
                if callable(self.source_offset) else self.source_offset)

    def _build(self, obj, stream, context, path):
        registry = find_registry(context)
        if registry is None:
            # 无注册表 => 标准可定位构建，完全委托原 RawCopy
            return super()._build(obj, stream, context, path)

        length = self._length(context)
        registry.before_fetch(self.source_id, stream)
        offset1 = stream.tell()

        if self.ranged:
            feeder = registry.feeders[self.source_id]
            base = self._source_offset(context)
            written = 0
            while written < length:
                take = min(self.chunk_size, length - written)
                chunk = feeder.fetch_range(
                    base + written, base + written + take,
                    note="长原件成段喂入",
                )
                stream_write(stream, chunk, take, path)
                written += take
                # 每块写完即尝试交付连续前缀，长卷峰值不再随卷长膨胀
                registry.after_chunk(self.source_id, stream)
            registry.after_fetch(self.source_id, length, stream)
        else:
            data = registry.fetch_oneshot(self.source_id, stream)
            stream_write(stream, data, len(data), path)
            registry.after_write(self.source_id, len(data), stream)

        offset2 = stream.tell()
        actual_len = offset2 - offset1
        # data 容器：oneshot 直接持原字节；ranged 不缓存整卷，用占位标记
        # （本格式的 checksum 只引用 oneshot 原件 orig_a，不引用长原件）
        data_value = data if not self.ranged else b""
        return Container(
            data=data_value, offset1=offset1, offset2=offset2,
            length=actual_len,
        )


class FeederRegistry:
    """经 context 注入的取件编排器（一次顺序构建一个）。"""

    def __init__(self, sources, feeders, fetch_order, sampler=None,
                 checkpoint=None):
        self.sources = sources              # id -> OneshotSource
        self.feeders = feeders              # id -> RangeSource
        self.fetch_order = tuple(fetch_order)
        self._rank = {sid: i for i, sid in enumerate(self.fetch_order)}
        self._fetched = set()
        self._held = {}                     # sid -> bytes（原件侧暂存）
        self.peak_held = 0
        self.held_log = []
        self.sampler = sampler
        self.checkpoint = checkpoint or (lambda kind, sid, stream: None)

    def held_bytes(self):
        return sum(len(d) for d in self._held.values())

    def held_snapshot(self):
        return sorted(self._held.keys())

    def _sample(self, stream):
        if self.sampler is not None:
            self.sampler.sample(self, stream)

    def before_fetch(self, source_id, stream):
        self.checkpoint("before_fetch", source_id, stream)
        self._sample(stream)

    def after_fetch(self, source_id, length, stream):
        self.checkpoint("after_fetch", source_id, stream)
        self._sample(stream)

    def after_write(self, source_id, length, stream):
        self.checkpoint("after_write", source_id, stream)
        self._sample(stream)

    def after_chunk(self, source_id, stream):
        """长原件每写一块就推进一次安全前缀，使峰值与卷长解耦。"""
        self.checkpoint("after_chunk", source_id, stream)
        self._sample(stream)

    def _acquire_through(self, source_id, stream):
        """落实取件次序：取得排列中排在 source_id 之前（含）的未取件。"""
        idx = self._rank[source_id]
        for sid in self.fetch_order[:idx + 1]:
            if sid in self._fetched or sid not in self.sources:
                continue
            data = self.sources[sid].fetch(
                note="按排列 %r 取得（落笔 %s 前）" % (self.fetch_order, source_id)
            )
            self._fetched.add(sid)
            self._held[sid] = data
            self.held_log.append((sid, len(data), self.held_bytes()))
            self.peak_held = max(self.peak_held, self.held_bytes())
            self.checkpoint("acquired", sid, stream)
            self._sample(stream)

    def fetch_oneshot(self, source_id, stream):
        self._acquire_through(source_id, stream)
        data = self._held.pop(source_id)     # 落笔即释放原件侧暂存
        self.checkpoint("fetched_to_write", source_id, stream)
        self._sample(stream)
        return data
