"""虚拟可定位流：对 Construct 呈现可 seek 的随机访问流，对介质只做顺序交付。

核心映射
--------
Construct 的 Pointer/Seek/RawCopy 只会调用 ``seek/tell/write/read``。
本流在内存里维护一张稀疏的块表（逻辑地址空间），所有"回跳写入"都落在
尚未交付的暂存块上；真正的物理写只通过 :class:`ForwardOnlySink` 单调向前。

地址空间分三种状态：

- ``hole``     从未写过的逻辑位置（读取时按填充字节占位，绝不臆造提交）
- ``staged``   已写、仍可改可读的暂存块（占用暂存配额）
- ``committed`` 已顺序交付给介质，只读；再写即 :class:`BackfillBeyondCommitted`

安全前缀（可以发给移交人的连续、不会再被覆盖的前缀）严格等于
``committed_watermark``，只在 :meth:`VirtualSeekableStream.flush` 成功后推进。
"""

from dataclasses import dataclass
from .errors import BackfillBeyondCommitted, CommittedRangeNotReadable

DEFAULT_BLOCK_SIZE = 1 << 12  # 4 KiB，固定空档按块成段处理


@dataclass
class Event:
    """一次可回溯到声明的流动作。"""

    kind: str            # write_append | write_backfill | seek | read | commit | fill | register | release
    offset: int          # 动作针对的逻辑起点（绝对卷坐标）
    length: int = 0
    pos_after: int = 0
    reason: str = ""     # 关联到声明元素/字段路径与坐标来源
    detail: str = ""


class ForwardOnlySink:
    """只准前进的写端协议（验收介质的替身/真实驱动都实现这个接口）。

    提交动作天然单调：``delivered`` 只增不减。任何试图重写已交付区间的
    行为在上层 :class:`VirtualSeekableStream` 就会被拦下，到这里的永远是
    新的连续字节。
    """

    def __init__(self):
        self.delivered = 0
        self.chunks = []

    def write(self, chunk: bytes) -> int:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("只接受字节块")
        start = self.delivered
        self.chunks.append(bytes(chunk))
        self.delivered += len(chunk)
        return len(chunk)

    def mark_committed(self, end: int) -> None:
        # 真实介质可在此落"已封板"标记；替身只做一致性断言
        if end != self.delivered:
            raise AssertionError("封板位置 %d 与实际交付 %d 不一致" % (end, self.delivered))

    def getvalue(self) -> bytes:
        return b"".join(self.chunks)


class VirtualSeekableStream:
    """对 Construct 表现为可 seek 流，物理上只顺序写。

    :param sink: 顺序写端
    :param block_size: 暂存块大小，固定空档按此粒度成段
    :param fill: hole/占位读取与空档填充字节
    :param strict_committed_read: 已交付范围是否禁止回读（默认禁止，
        与"原件各只让读一次、交付即不可读"对齐）
    """

    def __init__(self, sink=None, block_size=DEFAULT_BLOCK_SIZE, fill=b"\x00",
                 strict_committed_read=True):
        self.sink = sink if sink is not None else ForwardOnlySink()
        self.block_size = block_size
        self.fill_byte = fill[:1] or b"\x00"
        self.strict_committed_read = strict_committed_read

        self._blocks = {}          # block_index -> bytearray（仅保留未交付块）
        self._committed = set()    # 已交付的 block_index
        self._pos = 0
        self._eof = 0              # 当时卷末（逻辑）
        # 从 0 起连续已写字节上界（O(1) 洞检测，避免逐块扫描导致 O(n^2)）
        self._contiguous = 0
        self._watermark = 0        # 安全前缀末端
        self._peak_staged = 0
        self._events = []
        # key(字段路径) -> (start, end, reason)
        self._pending = {}
        # 写回调：(start, end, is_backfill) -> None，用于自动释放回填站点
        self.on_write = None

    # ------------------------------------------------------------------ #
    # 事件与暂存记账
    # ------------------------------------------------------------------ #
    @property
    def events(self):
        return tuple(self._events)

    @property
    def committed_watermark(self):
        return self._watermark

    @property
    def logical_eof(self):
        return self._eof

    def _log(self, kind, offset, length=0, reason="", detail=""):
        self._events.append(Event(kind, offset, length, self._pos, reason, detail))

    def _block_index(self, abs_offset):
        return abs_offset // self.block_size

    def _staged_bytes(self):
        """暂存区当前占用的精确字节数（已写但未交付区间的并集长度）。"""
        if not self._blocks:
            return 0
        total = 0
        for index, buf in self._blocks.items():
            if index in self._committed:
                continue
            total += len(buf)
        return total

    @property
    def staged_bytes(self):
        return self._staged_bytes()

    @property
    def peak_staged(self):
        return self._peak_staged

    def _note_staged(self):
        cur = self._staged_bytes()
        if cur > self._peak_staged:
            self._peak_staged = cur

    # ------------------------------------------------------------------ #
    # 回填站点登记（静态排程结果在动笔前注册）
    # ------------------------------------------------------------------ #
    def register_pending(self, key, start, length, reason):
        """登记一个将来要回写的站点，落在其范围内的前缀不得提前交付。"""
        self._pending[key] = (start, start + length, reason)
        self._log("register", start, length, reason=reason, detail=key)

    def release_pending(self, key):
        site = self._pending.pop(key, None)
        if site is not None:
            self._log("release", site[0], site[1] - site[0], reason=site[2], detail=key)

    def pending_sites(self):
        return tuple(
            (k, s, e, r) for k, (s, e, r) in sorted(self._pending.items(), key=lambda kv: kv[1][0])
        )

    # ------------------------------------------------------------------ #
    # 类文件接口（Construct 只依赖这四个方法 + 返回值约定）
    # ------------------------------------------------------------------ #
    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self._pos + offset
        elif whence == 2:
            target = self._eof + offset
        else:
            raise ValueError("非法 whence %r" % whence)
        if target < 0:
            raise ValueError("seek 到负位置 %d" % target)
        self._log("seek", target, reason="whence=%d offset=%d" % (whence, offset))
        self._pos = target
        return target

    def write(self, data):
        data = bytes(data)
        start = self._pos
        end = start + len(data)
        eof_before = self._eof

        # 安全铁律：落点若已交付，介质不可改，立即失败而不是制造半成品
        if start < self._watermark:
            site = self._find_site(start)
            raise BackfillBeyondCommitted(start, self._watermark, site)

        # 真回跳 = 写入起点落在当时卷末之前（Pointer/Seek 回去写）。
        # 自然追加（含头部 Padding 占位，即使覆盖了待回填站点）不算回跳，
        # 因而不能释放站点——占位不等于该字段已回填。
        is_backfill = start < eof_before
        self._write_into_blocks(start, data)
        self._eof = max(self._eof, end)
        # 仅当写入衔接当前连续前沿时推进连续上界；向前 seek 造出的洞之后，
        # 连续上界停在洞起点，回跳写不会破坏既有连续性。
        if start <= self._contiguous < end:
            self._contiguous = end
        self._pos = end
        self._note_staged()
        self._log(
            "write_backfill" if is_backfill else "write_append",
            start, len(data),
            reason=self._find_site(start) or ("顺序追加" if not is_backfill else "回跳写入"),
        )
        # 仅真回跳写入完整覆盖站点时才释放；占位写入永不释放待回填字段
        if is_backfill:
            for key in [k for k, (s, e, _r) in self._pending.items()
                        if s >= start and e <= end]:
                self.release_pending(key)
        if self.on_write is not None:
            self.on_write(start, end, is_backfill)
        return len(data)

    def read(self, length):
        if length < 0:
            raise ValueError("读取长度不能为负")
        start = self._pos
        end = start + length
        out = bytearray()
        cursor = start
        while cursor < end:
            index = self._block_index(cursor)
            block_start = index * self.block_size
            local = cursor - block_start
            take = min(end - cursor, self.block_size - local)
            if index in self._committed:
                if self.strict_committed_read:
                    raise CommittedRangeNotReadable(
                        cursor, take, "已交付前缀不可回读；RawCopy 应走 data 实取口径"
                    )
                out.extend(self.fill_byte * take)
            elif index in self._blocks:
                buf = self._blocks[index]
                avail = min(take, len(buf) - local)
                if avail <= 0:
                    out.extend(self.fill_byte * take)
                else:
                    out.extend(buf[local:local + min(take, avail)])
                    if avail < take:
                        out.extend(self.fill_byte * (take - avail))
            else:
                out.extend(self.fill_byte * take)  # hole 占位
            cursor += take
        self._log("read", start, length, reason="回读（应仅来自未交付暂存）")
        self._pos = end
        return bytes(out)

    # ------------------------------------------------------------------ #
    # 块表内部操作
    # ------------------------------------------------------------------ #
    def _ensure_block(self, index):
        buf = self._blocks.get(index)
        if buf is None:
            buf = bytearray(self.fill_byte * self.block_size)
            self._blocks[index] = buf
        return buf

    def _write_into_blocks(self, start, data):
        cursor = start
        view = memoryview(data)
        while cursor < start + len(data):
            index = self._block_index(cursor)
            block_start = index * self.block_size
            local = cursor - block_start
            space = self.block_size - local
            take = min(space, len(data) - (cursor - start))
            buf = self._ensure_block(index)
            buf[local:local + take] = view[cursor - start:cursor - start + take]
            cursor += take

    def _find_site(self, offset):
        for key, (s, e, reason) in self._pending.items():
            if s <= offset < e:
                return "%s[%s]" % (key, reason)
        return ""

    def _first_hole(self, from_offset):
        """从 from_offset 起第一个未写字节的绝对地址（O(1)）。"""
        return max(from_offset, self._contiguous)

    # ------------------------------------------------------------------ #
    # 顺序提交：推进安全前缀
    # ------------------------------------------------------------------ #
    def fill_to(self, offset, pattern=None):
        """把 [当前 eof, offset) 的固定空档写成填充段（成块处理）。

        填补的是卷末空档，填充后逻辑卷末与光标都推进到 offset，后续追加
        自然衔接；不会退回旧位置而覆盖刚填好的段。
        """
        if offset <= self._eof:
            return
        pattern = self.fill_byte if pattern is None else bytes(pattern)[:1]
        total = offset - self._eof
        start = self._eof
        self.seek(start)
        written = 0
        while written < total:
            chunk = pattern * min(self.block_size, total - written)
            self.write(chunk)
            written += len(chunk)
        self._log("fill", start, total, reason="显式固定空档填充")

    def flush(self, fill_holes=False):
        """把满足条件的连续前缀交付给顺序介质，返回新提交的区间列表。

        可提交到的上界 = min(第一个 hole, 任一未决回填站点的起点)。
        未决站点覆盖的范围一律不交付，即便字节已写好——"暂时不变"不等于
        "可以保证后面不再覆盖"。
        """
        committed_ranges = []
        while True:
            limit = self._eof
            # 限制 1：不能越过第一个洞
            hole = self._first_hole(self._watermark)
            limit = min(limit, hole)
            # 限制 2：不能卷入任何未决回填站点（含起点恰在水线处的站点）
            for s, e, reason in self._pending.values():
                if e <= self._watermark:
                    continue  # 站点整体已在水线之后（已交付侧），无需再拦
                if s < self._watermark < e:
                    # 水线已压进站点内部：说明此前误交付了待回填范围
                    raise BackfillBeyondCommitted(
                        s, self._watermark, (s, e, reason))
                # s >= watermark：提交至多到站点起点；s == watermark 时自然停住
                limit = min(limit, s)

            if limit <= self._watermark:
                if fill_holes and hole < self._eof and not self._pending_blocking(hole):
                    self.fill_to(min(hole + self.block_size, self._eof))
                    continue
                break

            self._commit_range(self._watermark, limit)
            committed_ranges.append((self._watermark, limit))
            self._watermark = limit
            self.sink.mark_committed(limit)

        self._note_staged()
        return committed_ranges

    def _pending_blocking(self, offset):
        return any(s <= offset < e for s, e, _r in self._pending.values())

    def _commit_range(self, start, end):
        cursor = start
        pieces = []
        reclaimed = []
        while cursor < end:
            index = self._block_index(cursor)
            block_start = index * self.block_size
            local = cursor - block_start
            take = min(end - cursor, self.block_size - local)
            buf = self._blocks.get(index)
            if buf is None:  # 防御：洞不应进入提交区间
                pieces.append(self.fill_byte * take)
            else:
                pieces.append(bytes(buf[local:local + take]))
            # 整块交付完：标记并回收，块表只保留未交付内容
            if (local + take == self.block_size
                    and block_start + self.block_size <= end):
                self._committed.add(index)
                reclaimed.append(index)
            cursor += take
        for index in reclaimed:
            self._blocks.pop(index, None)
        chunk = b"".join(pieces)
        self.sink.write(chunk)
        self._log("commit", start, len(chunk), reason="安全前缀推进")

    def finalize(self, fill_pattern=None):
        """构建结束后的收口：补固定空档、释放全部站点并交付到卷末。"""
        if self._eof > self._watermark:
            self.fill_to(self._eof, fill_pattern)
        # 此刻所有回填都应已完成
        leftover = self.pending_sites()
        if leftover:
            raise BackfillBeyondCommitted(
                leftover[0][1], self._watermark, leftover
            )
        ranges = self.flush(fill_holes=True)
        return ranges

    # ------------------------------------------------------------------ #
    # 诊断快照（供计划/验收报告引用）
    # ------------------------------------------------------------------ #
    def snapshot(self):
        return {
            "watermark": self._watermark,
            "logical_eof": self._eof,
            "staged_bytes": self.staged_bytes,
            "peak_staged": self.peak_staged,
            "delivered": self.sink.delivered,
            "pending_sites": self.pending_sites(),
        }
