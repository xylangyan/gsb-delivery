"""顺序写适配层异常。

所有异常都带可回溯到声明的位置/字段信息，便于验收人核对工单。
"""


class SeqWriteError(Exception):
    """适配层基类异常。"""


class BackfillBeyondCommitted(SeqWriteError):
    """回跳落点已越过安全前缀（介质上该范围不可改）。

    这通常意味着排程错误：某个 Pointer/Checksum 的落点在它真正被回填
    之前就被当成稳定前缀送出去了。
    """

    def __init__(self, target, committed, site=None):
        self.target = target
        self.committed = committed
        self.site = site
        super().__init__(
            "回跳落点 %d 已越过安全前缀 %d（站点=%r），该范围在介质上不可改"
            % (target, committed, site)
        )


class CommittedRangeNotReadable(SeqWriteError):
    """构建过程试图回读已交付（不可读）的范围。

    顺序介质一旦交付即不可读；出现此异常说明声明里混入了无法靠
    暂存满足的回读需求（例如 value 模式 RawCopy 的回读）。
    """

    def __init__(self, offset, length, reason=""):
        self.offset = offset
        self.length = length
        super().__init__(
            "试图回读已交付范围 offset=%d length=%d %s" % (offset, length, reason)
        )


class QuotaExceeded(SeqWriteError):
    """暂存配额不足。

    在真正动笔前由预检抛出，明确指出必须同时放下的两个区域及差额，
    避免写出半卷后才失败、让介质上的半成品冒充可用卷。
    """

    def __init__(self, required, quota, regions, detail=""):
        self.required = required
        self.quota = quota
        self.regions = tuple(regions)
        self.shortfall = required - quota
        message = (
            "暂存配额不足：需要同时容纳 %d 字节，配额仅 %d 字节，还差 %d 字节。"
            "必须同时放下的区域：%s。%s"
            % (required, quota, self.shortfall, list(self.regions), detail)
        )
        super().__init__(message)


class SourceConsumed(SeqWriteError):
    """一次性原件被读取第二次。"""

    def __init__(self, source_id, path=""):
        self.source_id = source_id
        super().__init__("一次性原件 %r 已被读取，不能再读（%s）" % (source_id, path))


class UnschedulableOrder(SeqWriteError):
    """工单给出的取件先后没有一种能同时满足取件与原生写入约束。"""

    def __init__(self, reason, candidates=None):
        self.candidates = candidates
        super().__init__("无法排程：%s（候选次序=%r）" % (reason, candidates))
