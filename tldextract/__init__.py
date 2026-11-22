"""Export tldextract's public interface."""

from . import _version
from .site_key import canonical_host, legacy_host
from .site_migration import (
    DetailRecord,
    MigrationLedger,
    MigrationReport,
    SiteKey,
    UncertainRecord,
    derive_site_key,
    reconcile,
)
from .tldextract import ExtractResult, TLDExtract, extract, update

__version__: str = _version.version

__all__ = [
    "__version__",
    "canonical_host",
    "DetailRecord",
    "derive_site_key",
    "ExtractResult",
    "extract",
    "legacy_host",
    "MigrationLedger",
    "MigrationReport",
    "reconcile",
    "SiteKey",
    "TLDExtract",
    "UncertainRecord",
    "update",
]
