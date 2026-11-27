"""原件源：对"各只让读一次"与"可按范围提供"两类原件的统一抽象。

- :class:`OneshotSource`：整卷原件只能整体取一次，取过即焚；用于工单里
  取得次序受控、且必须整段进暂存的 RawCopy 原件。
- :class:`RangeSource`：长原件支持按 ``[start, end)`` 多次取段，每一段
  同样只给一次；固定空档（跳过区间）由调用方按块成段处理，不会触发
  对原件的无谓读取。

两类源都记录真实读取轨迹，验收时可与工单的"取得次序/已证长度"逐笔对账。
"""

from dataclasses import dataclass
from .errors import SourceConsumed


@dataclass
class SourceSpec:
    """工单对一份原件的登记信息（声明侧）。"""

    source_id: str
    proven_length: int          # 已证长度（构建前已知），RawCopy 实取口径以此为准
    acquisition_rank: int       # 工单给定的取得次序（越小越早）
    kind: str = "oneshot"       # oneshot | range
    # range 源允许的取段约束：None 表示任意范围；否则为允许的 (start,end) 列表
    allowed_ranges: object = None
    note: str = ""


@dataclass
class _FetchRecord:
    source_id: str
    start: int
    end: int
    order: int
    note: str = ""


class OneshotSource:
    """整段只能读一次的原件。"""

    def __init__(self, spec: SourceSpec, data: bytes):
        self.spec = spec
        self._data = bytes(data)
        if len(self._data) != spec.proven_length:
            raise ValueError(
                "原件 %r 实际长度 %d 与工单已证长度 %d 不符"
                % (spec.source_id, len(self._data), spec.proven_length)
            )
        self._consumed = False
        self.history = []
        self._order = 0

    @property
    def source_id(self):
        return self.spec.source_id

    @property
    def length(self):
        return self.spec.proven_length

    @property
    def data(self):
        """不可变原件字节（仅供排程器重放时造新源，不构成第二次读取）。"""
        return self._data

    def fetch(self, note=""):
        """整体取回原件；第二次调用直接失败。"""
        if self._consumed:
            raise SourceConsumed(self.spec.source_id)
        self._consumed = True
        self._order += 1
        self.history.append(
            _FetchRecord(self.spec.source_id, 0, len(self._data), self._order, note)
        )
        return self._data

    def as_rawcopy_obj(self, note=""):
        """按 RawCopy 的实取口径产出 data 容器（value 另算时也以 data 为准）。"""
        return {"data": self.fetch(note)}


class RangeSource:
    """可按范围提供的长原件，每个范围只给一次。

    用于把"另用可按范围提供的长原件喂只准前进的写端"：构建端不必持有
    整卷，只在写到对应逻辑区间时拉取所需范围。固定空档（源中需要跳过
    的部分）通过 :meth:`gap` 登记，不计入读取、由虚拟流填充。
    """

    def __init__(self, spec: SourceSpec, data: bytes):
        if spec.kind != "range":
            raise ValueError("RangeSource 需要 kind='range' 的 SourceSpec")
        self.spec = spec
        self._data = bytes(data)
        if len(self._data) != spec.proven_length:
            raise ValueError(
                "长原件 %r 实际长度 %d 与已证长度 %d 不符"
                % (spec.source_id, len(self._data), spec.proven_length)
            )
        self._served_ranges = []
        self.history = []
        self._order = 0

    @property
    def source_id(self):
        return self.spec.source_id

    @property
    def length(self):
        return self.spec.proven_length

    @property
    def data(self):
        """不可变原件字节（供重放造新源；真实读取仍只经 fetch_range）。"""
        return self._data

    def _check_allowed(self, start, end):
        if self.spec.allowed_ranges is None:
            return
        for (a, b) in self.spec.allowed_ranges:
            if a <= start and end <= b:
                return
        raise ValueError(
            "范围 [%d,%d) 不在原件 %r 允许的取段集合内"
            % (start, end, self.spec.source_id)
        )

    def fetch_range(self, start, end, note=""):
        if start < 0 or end > len(self._data) or start >= end:
            raise ValueError("非法取段 [%d,%d)" % (start, end))
        self._check_allowed(start, end)
        # 同一字节不许给两次（原件各只让读一次的范围版）
        for (a, b) in self._served_ranges:
            if start < b and a < end:
                raise SourceConsumed(
                    self.spec.source_id,
                    "范围 [%d,%d) 与已服务范围 [%d,%d) 重叠" % (start, end, a, b),
                )
        self._served_ranges.append((start, end))
        self._order += 1
        self.history.append(
            _FetchRecord(self.spec.source_id, start, end, self._order, note)
        )
        return self._data[start:end]

    def gap(self, start, end, note="固定空档"):
        """登记一段不取的固定空档（不读原件，交由虚拟流成段填充）。"""
        self.history.append(
            _FetchRecord(self.spec.source_id, start, end, 0, note="GAP " + note)
        )
