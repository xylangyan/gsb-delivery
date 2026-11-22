"""Export tldextract's public interface."""

from . import _version
from .tldextract import ExtractResult, TLDExtract, extract, update
from .zones import (
    PUBLIC_PRIORITY,
    PUBLIC_SOURCE,
    RuleKind,
    ZoneEvaluator,
    ZoneList,
    ZoneMatch,
    ZoneRule,
    ZoneValidationReport,
    ZoneViolation,
)

__version__: str = _version.version

__all__ = [
    "__version__",
    "extract",
    "ExtractResult",
    "PUBLIC_PRIORITY",
    "PUBLIC_SOURCE",
    "RuleKind",
    "TLDExtract",
    "update",
    "ZoneEvaluator",
    "ZoneList",
    "ZoneMatch",
    "ZoneRule",
    "ZoneValidationReport",
    "ZoneViolation",
]
