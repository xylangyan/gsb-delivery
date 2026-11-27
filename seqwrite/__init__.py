"""把可定位的 Construct 构建接到只准顺序写介质上的适配层。

子模块：
- errors:   适配层专用异常
- vstream:  虚拟可定位流（逻辑随机定位，物理顺序交付）
- sources:  一次性原件源与可按范围提供的长原件源
- planner:  静态排程、峰值暂存核算、安全前缀推进与配额预检
- declaration: 示范登记格式（Construct 声明）与工单
"""

from .errors import (
    SeqWriteError,
    BackfillBeyondCommitted,
    CommittedRangeNotReadable,
    QuotaExceeded,
    SourceConsumed,
    UnschedulableOrder,
)
from .vstream import VirtualSeekableStream, ForwardOnlySink, Event
from .sources import OneshotSource, RangeSource
from .fetchers import SourceBackedRawCopy, FeederRegistry
from .planner import BuildPlanner, StagingRequirement, BackfillSite
from .declaration import (
    ARCHIVE_FORMAT,
    build_format,
    WorkOrder,
    SourceSpec,
    build_demo_work_order,
)

__all__ = [
    "SeqWriteError",
    "BackfillBeyondCommitted",
    "CommittedRangeNotReadable",
    "QuotaExceeded",
    "SourceConsumed",
    "UnschedulableOrder",
    "VirtualSeekableStream",
    "ForwardOnlySink",
    "Event",
    "OneshotSource",
    "RangeSource",
    "SourceBackedRawCopy",
    "FeederRegistry",
    "BuildPlanner",
    "StagingRequirement",
    "BackfillSite",
    "ARCHIVE_FORMAT",
    "build_format",
    "WorkOrder",
    "SourceSpec",
    "build_demo_work_order",
]
