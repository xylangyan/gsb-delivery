"""Sealed-archive review support for two-area imagery handoffs.

This subpackage adds an offline, read-only review workflow on top of the
normal pystac-client search machinery.  A delivery manifest describes two
lon/lat rectangles, the imagery categories and the number ``k`` of newest
records required per area.  Sealed volumes provide read-only copies of the
search pages and per-page receipts; the planner:

* replays each volume's paging chains using the original STAC Link semantics,
  including POST ``merge`` links and links that replace the request body,
* filters Items by their actual geometry against each rectangle (boundary
  contact counts, antimeridian rectangles supported),
* dedupes by ``(collection, id)`` and orders by UTC time (newest first),
  collection and id,
* verifies every handed Item on a reached page and every required cutoff key
  against fully-swept receipt intervals,
* selects an exact minimum-fee set of volumes, charging each volume once even
  when it serves both areas.

The normal online :class:`~pystac_client.ItemSearch` flow is untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .geometry import geometry_intersects_bbox, normalize_bbox
from .keys import (
    MAX_KEY,
    MIN_KEY,
    KeyInterval,
    SortKey,
    item_sort_key,
    parse_datetime,
    parse_sort_key,
)
from .planner import (
    AreaSpec,
    AuditPlanner,
    AuditResult,
    CategorySpec,
    Gap,
    Manifest,
    Requirement,
    RequirementProof,
    RequirementResult,
    run_audit,
)
from .replay import SealedRequest, request_key, requests_match, resolve_link
from .vault import Archive, Receipt, SealedPage, Volume

__all__ = [
    "MAX_KEY",
    "MIN_KEY",
    "Archive",
    "AreaSpec",
    "AuditPlanner",
    "AuditResult",
    "CategorySpec",
    "Gap",
    "KeyInterval",
    "Manifest",
    "Receipt",
    "Requirement",
    "RequirementProof",
    "RequirementResult",
    "SealedPage",
    "SealedRequest",
    "SortKey",
    "Volume",
    "audit_handoff",
    "geometry_intersects_bbox",
    "item_sort_key",
    "normalize_bbox",
    "parse_datetime",
    "parse_sort_key",
    "request_key",
    "requests_match",
    "resolve_link",
    "run_audit",
]


def audit_handoff(
    archive: Archive | str | Path | dict[str, Any],
    manifest: Manifest | dict[str, Any],
) -> AuditResult:
    """Run a two-area handoff review against a sealed archive.

    Args:
        archive: An :class:`Archive`, a path to an archive JSON document, or
            its parsed dictionary.
        manifest: A :class:`Manifest` or its parsed dictionary.

    Returns:
        AuditResult: The minimum-fee volume selection, the records that
            establish each requirement, and any unprovable key ranges.
    """
    if isinstance(archive, Archive):
        parsed_archive = archive
    elif isinstance(archive, dict):
        parsed_archive = Archive.from_dict(archive)
    else:
        parsed_archive = Archive.from_file(archive)
    parsed_manifest = (
        manifest if isinstance(manifest, Manifest) else Manifest.from_dict(manifest)
    )
    return run_audit(parsed_archive, parsed_manifest)
