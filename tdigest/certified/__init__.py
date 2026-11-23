"""
Certified, mergeable quantile summaries.

Public entry points:

* :class:`CertifiedDigest` -- deterministic GK summary with a provable rank
  error, canonical byte serialization and hierarchical merging.
* :class:`AlternativeSummary` -- same-precision bulk-construction summary
  sent by collection agents as an alternative delivery.
* :class:`LegacyWindow` / :meth:`CertifiedDigest.from_legacy_dict` -- read
  old windows that only have ``TDigest.to_dict`` centroids and weights.
* :func:`validate_two_trees` -- verify that two differently shaped
  aggregation trees produce identical canonical summaries and certificates.
* :class:`CertificationError` -- raised (or recorded) when an error bound
  cannot be proven; names every gap source.
"""

from .certified_digest import (
    SCHEMA,
    CertificationError,
    CertifiedDigest,
    LegacyWindow,
    SourceManifest,
    WindowRecord,
)
from .alternative import ALT_SCHEMA, AlternativeSummary
from .tree_validation import TreeValidationResult, validate_two_trees

__all__ = [
    "SCHEMA",
    "ALT_SCHEMA",
    "CertificationError",
    "CertifiedDigest",
    "LegacyWindow",
    "SourceManifest",
    "WindowRecord",
    "AlternativeSummary",
    "TreeValidationResult",
    "validate_two_trees",
]
