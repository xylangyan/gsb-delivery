"""
    ulid.ledger
    ~~~~~~~~~~~

    离线柜发货清单的 ULID 排号、翻页续读与改签子系统。

    领域映射:
        物理原件 (physical item) -> :class:`~ulid.ledger.model.PhysicalItem`
        原件号/ULID             -> 可补号、可改签, 但已对外确认或已交付者冻结
        读者 (reader)           -> 按自己的范围与排序条件翻页, 进度彼此独立
        代 (generation)         -> 一次成功签发的对照目录, 重开后只承认已签发的代
"""
from .clock import ControlledClock
from .model import (
    AuditBlock,
    ExportBook,
    ItemState,
    Page,
    PhysicalItem,
    Publication,
    ReservationState,
    ResumeRequest,
    Reticket,
)
from .numbering import NumberingService, Reservation
from .ordering import Ordering, PartialOrder, Window, WindowKind
from .planner import BackfillRequest, RenumberPlan, ReticketPlanner
from .reader import Reader, ReaderService, ReaderSpec
from .registry import Ledger
from .service import LedgerService

__all__ = [
    'AuditBlock',
    'BackfillRequest',
    'ControlledClock',
    'ExportBook',
    'ItemState',
    'Ledger',
    'LedgerService',
    'NumberingService',
    'Ordering',
    'Page',
    'PartialOrder',
    'PhysicalItem',
    'Publication',
    'Reader',
    'ReaderService',
    'ReaderSpec',
    'RenumberPlan',
    'Reservation',
    'ReservationState',
    'ResumeRequest',
    'Reticket',
    'ReticketPlanner',
    'Window',
    'WindowKind',
]
