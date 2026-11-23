"""
Module for the jsonlines data format.
"""

# expose only public api
from .jsonlines import (
    Error,
    InvalidLineError,
    Reader,
    Writer,
    open,
)
from .recovery import (
    Alternative,
    BodyCandidate,
    ByteRange,
    ConfirmedRecord,
    IncompatibleFact,
    JsonType,
    OrphanBodyReceipt,
    Receipt,
    RecordReceipt,
    RecoveryResult,
    SourceSpec,
    UncertainRegion,
    receipt_from_dict,
    recover,
    recover_file,
)

__all__ = [
    "Alternative",
    "BodyCandidate",
    "ByteRange",
    "ConfirmedRecord",
    "Error",
    "IncompatibleFact",
    "InvalidLineError",
    "JsonType",
    "OrphanBodyReceipt",
    "Reader",
    "Receipt",
    "RecordReceipt",
    "RecoveryResult",
    "SourceSpec",
    "UncertainRegion",
    "Writer",
    "open",
    "recover",
    "recover_file",
    "receipt_from_dict",
]
