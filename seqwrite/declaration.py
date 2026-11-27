"""已登记的 Construct 声明（档案卷格式）与工单模型。

设计严格对应任务中登记的那部分 Construct 能力：

- 定长字段：``magic/version`` 与头部各固定宽槽位
- 长度包：外层 ``Prefixed(VarInt, ...)`` 与内层长度包（内层在独立
  ``BytesIO`` 里构建，拥有自己的局部坐标，不能当成整卷坐标）
- RawCopy 原件：默认走 ``data`` 实取口径；其中一份同时给原字节与业务值
- Pointer 回填的四种坐标来源：已定常量、给定字段(Tell)、当前位置、当时卷末
- 条件回填：补在原件内部的登记号，取决于另一联的选记
- Checksum：按声明取"原构建时刻"的字段内容（RawCopy 的 data），最终卷
  同一范围之后可能被条件回填改写
"""

import hashlib

from construct import (
    Struct, Const, Int8ub, Int32ub, Bytes, VarInt, Prefixed,
    RawCopy, Pointer, Tell, Padding, If, Adapter, Construct, this,
)

from .sources import SourceSpec

# --------------------------------------------------------------------------- #
# 已定常量
# --------------------------------------------------------------------------- #
MAGIC = b"ARC1"
VERSION = 1
SHA256_LEN = 32
SLOT4 = 4
# 登记号在原件 A 内部的固定偏移（补在原件内部）
REGISTRY_INSIDE_A = 6
# 短头槽位（长卷途中跳过去写的那一联短头）
SHORT_HEADER_LEN = 8
# 长原件在跳写短头前先写的固定小前导段长度。短头回填只阻断这一小段的
# 提交，长原件其余部分随后逐块交付，使峰值暂存与总卷长解耦。
LONG_LEAD = 8

# 各原件已证长度（小卷，便于逐字节核对）；长卷由工单另行给出
LEN_A = 16
LEN_B = 12
LEN_C = 4            # 同时带原字节与业务值的那份
LEN_INNER_PAYLOAD = 8
# 内层长度包 payload 的规范字节（参考构建与顺序构建共用，保证逐字节一致）
INNER_PAYLOAD_DATA = b"INNER123"


def _sha256(data):
    return hashlib.sha256(data).digest()


class _FromContext(Adapter):
    """回填专用：构建值不取自外部 obj，而由 context lambda 当场算出。

    这样所有 Pointer 回填都自包含，排程器不需要手工喂坐标/标记值，
    原读取工具也能直接 parse 出对应字段。
    """

    def __init__(self, subcon, valuefunc):
        super().__init__(subcon)
        self.valuefunc = valuefunc
        # 值完全由 context 决定，构建无需外部提供同名键
        self.flagbuildnone = True

    def _build(self, obj, stream, context, path):
        value = self.valuefunc(context)
        return self.subcon._build(value, stream, context, path)

    def _parse(self, stream, context, path):
        return self.subcon._parse(stream, context, path)


class BuildTimeChecksum(Construct):
    """只在构建时刻按声明取字段内容计算的校验值。

    与 :class:`construct.Checksum` 的关键区别：解析时**只读出存储的校验
    值、不就当前卷重算比对**。这正对应"校验取原构建时刻字段内容，而最终
    卷同一范围之后可能已被条件回填改写"——若在 parse 时对回填后的字节
    重算，必然与构建时刻的哈希不符而误报。原读取工具因此能直接打开卷；
    验收方要用构建时刻基线另行核对（见 planner 的 reference 比对）。
    """

    def __init__(self, checksumfield, hashfunc, bytesfunc):
        super().__init__()
        self.checksumfield = checksumfield
        self.hashfunc = hashfunc
        self.bytesfunc = bytesfunc
        self.flagbuildnone = True

    def _build(self, obj, stream, context, path):
        digest = self.hashfunc(self.bytesfunc(context))
        self.checksumfield._build(digest, stream, context, path)
        return digest

    def _parse(self, stream, context, path):
        return self.checksumfield._parsereport(stream, context, path)

    def _sizeof(self, context, path):
        return self.checksumfield._sizeof(context, path)


# --------------------------------------------------------------------------- #
# 内层长度包：独立 BytesIO 构建，局部坐标，Pointer 落点是局部坐标
# --------------------------------------------------------------------------- #
def _inner_packet():
    return Prefixed(VarInt, Struct(
        "inner_tail_pos" / Tell,                       # 局部坐标（内层内从 0 起）
        "inner_tail_slot" / Padding(SLOT4),            # 待回填的局部槽位
        "payload" / RawCopy(Bytes(LEN_INNER_PAYLOAD)),
        # 用局部坐标回填尾部槽位；该坐标与整卷全局坐标相差内层前缀偏移
        "inner_tail" / Pointer(
            this.inner_tail_pos,
            _FromContext(Int32ub, lambda ctx: LEN_INNER_PAYLOAD),
        ),
    ))


def _rawcopy(name, source_id, length, streaming, parse_subcon=None,
             ranged=False, chunk_size=4, source_offset=0):
    """原件字段：标准模式用 RawCopy；顺序模式用 data 实取的 SourceBackedRawCopy。"""
    if not streaming:
        inner = parse_subcon if parse_subcon is not None else Bytes(length)
        return RawCopy(inner)
    from .fetchers import SourceBackedRawCopy
    inner = parse_subcon if parse_subcon is not None else Bytes(length)
    return SourceBackedRawCopy(inner, source_id, length,
                               ranged=ranged, chunk_size=chunk_size,
                               source_offset=source_offset)


def build_format(streaming=False, chunk_size=4):
    """构造登记格式。

    streaming=False：纯标准 Construct，原读取工具直接 parse 的权威格式。
    streaming=True：原件替换为 :class:`~seqwrite.fetchers.SourceBackedRawCopy`，
        字节布局与标准模式完全一致，仅取数被延迟到落笔时刻。
    """
    return Struct(
        "magic" / Const(MAGIC),
        "version" / Int8ub,
        # 显式给定字段（构建/解析自描述）：长原件长度与条件回填选记
        "long_len" / VarInt,
        "attach_registry" / Int8ub,

        # ---- 头部：一组固定宽槽位，位置由当前位置(Tell)与常量确定 ---- #
        "body_offset_pos" / Tell,
        "body_offset_slot" / Padding(SLOT4),
        "checksum_pos" / Tell,
        "checksum_slot" / Padding(SHA256_LEN),
        "eof_mark_pos" / Tell,
        "eof_mark_slot" / Padding(SLOT4),
        "short_header_pos" / Tell,
        "short_header_slot" / Padding(SHORT_HEADER_LEN),

        # ---- 内层长度包（自有坐标）---- #
        "inner" / _inner_packet(),

        # ---- 原件区 ---- #
        "body_start" / Tell,

        # 回填 1（最早时机）：body 起始坐标此刻已知，立即回填头部槽位，
        # 让安全前缀得以尽早越过该槽位继续推进。
        "body_offset" / Pointer(
            this.body_offset_pos,
            _FromContext(Int32ub, lambda ctx: ctx.body_start),
        ),

        "orig_a_start" / Tell,
        "orig_a" / _rawcopy("orig_a", "orig_a", LEN_A, streaming),

        # 回填 2：校验只能在实际读取 orig_a 后算出；取构建时刻的原始字节。
        # 必须在登记号回填之前完成，以锁定未被改写的 orig_a 原字节。
        "checksum" / Pointer(this.checksum_pos, BuildTimeChecksum(
            Bytes(SHA256_LEN), _sha256, this.orig_a.data,
        )),

        # 回填 3（条件）：补在原件 A 内部固定偏移的登记号，取决于选记。
        # 在校验锁定原字节之后才写，因此同一范围最终卷与构建时刻不同。
        "registry" / If(this.attach_registry, Pointer(
            this.orig_a_start + REGISTRY_INSIDE_A,
            _FromContext(Int32ub, lambda ctx: ctx.registry_value),
        )),

        "orig_b" / _rawcopy("orig_b", "orig_b", LEN_B, streaming),
        # 同时带原字节 data 与业务值 value：RawCopy 以 data 为准
        "orig_c" / _rawcopy("orig_c", "orig_c", LEN_C, streaming,
                            parse_subcon=Int32ub),

        # 回填 4：按"当时卷末"往回改的标记。这里有一个 construct 的固有限制：
        # 负偏移 Pointer(offset<0) 用 whence=2，基准是"执行时刻 EOF"；构建到
        # 此处时长原件尚未写、EOF=eof_now，而解析面对完整卷、EOF 是最终卷末，
        # 同一负偏移在两个时刻落点不同，无法被原读取工具稳定复现。
        # 因此登记格式用绝对坐标（头部槽 eof_mark_pos）落点，写入的*值*仍是
        # 当时卷末 eof_now——落点远离最终卷末，语义等价且可解析。负偏移相对
        # EOF 的回跳能力在 VirtualSeekableStream 层另有专测（seek whence=2）。
        "eof_now" / Tell,
        "eof_mark" / Pointer(
            this.eof_mark_pos,
            _FromContext(Int32ub, lambda ctx: ctx.eof_now),
        ),

        # ---- 长原件：先写固定小前导，途中跳写短头，再逐块写其余 ---- #
        # 短头槽位只阻断这一小前导的提交；后半段写完即可逐块交付，
        # 因此无论长原件多大，输出侧暂存都被钳在常数级别。
        "orig_long_start" / Tell,
        "orig_long_1" / _rawcopy(
            "orig_long", "orig_long", LONG_LEAD, streaming,
            ranged=True, chunk_size=chunk_size, source_offset=0),
        # 回填 5（长卷途中）：跳去写一联短头，Pointer 写完自动回到原位置
        "short_header" / Pointer(
            this.short_header_pos,
            _FromContext(Bytes(SHORT_HEADER_LEN), lambda ctx: ctx.short_header),
        ),
        "orig_long_2" / _rawcopy(
            "orig_long", "orig_long",
            this.long_len - LONG_LEAD, streaming,
            ranged=True, chunk_size=chunk_size,
            source_offset=LONG_LEAD),
    )


# 权威登记格式（读取工具直接打开用）
ARCHIVE_FORMAT = build_format(streaming=False)


def standard_inputs(*, long_len, attach_registry, registry_value,
                    short_header, orig_a, orig_b, orig_c, orig_long,
                    version=VERSION):
    """标准（可定位）构建所需的完整输入；原件走 RawCopy 的 data 实取口径。"""
    lead = LONG_LEAD
    if long_len < lead:
        raise ValueError("long_len=%d 小于固定前导 LONG_LEAD=%d" % (long_len, lead))
    return {
        "version": version,
        "long_len": long_len,
        "attach_registry": 1 if attach_registry else 0,
        "registry_value": registry_value,
        "short_header": short_header,
        "inner": {"payload": {"data": INNER_PAYLOAD_DATA}},
        "orig_a": {"data": orig_a},
        "orig_b": {"data": orig_b},
        "orig_c": {"data": orig_c},
        # 长原件在声明里拆成"固定前导 + 其余"，参考构建按同一字节切片
        "orig_long_1": {"data": orig_long[:lead]},
        "orig_long_2": {"data": orig_long[lead:]},
    }


# --------------------------------------------------------------------------- #
# 工单
# --------------------------------------------------------------------------- #
class WorkOrder:
    """一张可验收的工单：原件登记 + 允许的取件先后 + 暂存配额。"""

    def __init__(self, order_id, source_specs, allowed_fetch_orders,
                 staging_quota, long_source_id=None, gap_ranges=(),
                 attach_registry=False, long_len=0, chunk_size=4):
        self.order_id = order_id
        self.source_specs = tuple(source_specs)
        self.allowed_fetch_orders = tuple(allowed_fetch_orders)
        self.staging_quota = staging_quota
        self.long_source_id = long_source_id
        self.gap_ranges = tuple(gap_ranges)
        self.attach_registry = attach_registry
        self.long_len = long_len
        self.chunk_size = chunk_size
        self._by_id = {s.source_id: s for s in source_specs}

    def spec(self, source_id):
        return self._by_id[source_id]

    def oneshot_specs(self):
        return tuple(s for s in self.source_specs if s.kind == "oneshot")

    def range_specs(self):
        return tuple(s for s in self.source_specs if s.kind == "range")


def build_demo_work_order(staging_quota=10 ** 9, long_len=40, chunk_size=4,
                          attach_registry=True, override_orders=None):
    """构造示范工单（小卷原件 + 一份可按范围取的长原件）。"""
    specs = [
        SourceSpec("orig_a", LEN_A, acquisition_rank=1),
        SourceSpec("orig_b", LEN_B, acquisition_rank=2),
        SourceSpec("orig_c", LEN_C, acquisition_rank=3),
        SourceSpec(
            "orig_long", long_len, acquisition_rank=4, kind="range",
            note="可按范围提供；固定空档成段处理",
        ),
    ]
    base = ["orig_a", "orig_b", "orig_c"]
    if override_orders is None:
        # 工单允许的几种取件先后（长原件按写到的范围即时取，不参与整体置换）
        allowed = [
            tuple(base),
            ("orig_b", "orig_a", "orig_c"),
            ("orig_c", "orig_a", "orig_b"),
        ]
    else:
        allowed = override_orders
    return WorkOrder(
        "WO-DEMO", specs, allowed, staging_quota,
        long_source_id="orig_long", attach_registry=attach_registry,
        long_len=long_len, chunk_size=chunk_size,
    )
