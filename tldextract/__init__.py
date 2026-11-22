"""Export tldextract's public interface."""

from . import _version
from .rule_updates import (
    ActiveRules,
    Component,
    PublishReceipt,
    SharedRuleCache,
    UpdateReceipt,
    UpdateStatus,
)
from .tldextract import ExtractResult, TLDExtract, extract, update

__version__: str = _version.version

__all__ = [
    "__version__",
    "extract",
    "ExtractResult",
    "TLDExtract",
    "update",
    "ActiveRules",
    "Component",
    "PublishReceipt",
    "SharedRuleCache",
    "UpdateReceipt",
    "UpdateStatus",
]
