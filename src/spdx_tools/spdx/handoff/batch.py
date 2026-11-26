# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Batch driver for a JSON to TagValue handoff.

A handoff manifest names a shared package order and a list of revisions.  Each
revision points at a JSON catalog (read by ``spdx_tools``), optionally pins the
file order, and optionally names the interactions that the handoff order makes
effective.  The driver returns a plan (the package order, the scoring revision
and the per-revision explicit byte costs), the rendered TagValue texts, a set of
diagnostics for rows that cannot be written, and reread verification records for
the small cases.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from spdx_tools.spdx.handoff.encoding import classify_relationship, describe_relationship
from spdx_tools.spdx.handoff.emitter import verify_reread, write_revision
from spdx_tools.spdx.handoff.planning import (
    NoCommonPackageOrder,
    Revision,
    RevisionAnalysis,
    analyze_revision,
    best_placement,
    choose_package_order,
    diagnose_stuck_revisions,
)
from spdx_tools.spdx.parser.json.json_parser import parse_from_file as parse_json_file


@dataclass
class HandoffResult:
    package_order: List[str]
    scoring_revision: Optional[str]
    explicit_cost: int
    per_revision_cost: Dict[str, int]
    documents: Dict[str, str] = field(default_factory=dict)
    diagnostics: List[Dict] = field(default_factory=list)
    verifications: List[Dict] = field(default_factory=list)
    stuck: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packageOrder": self.package_order,
            "scoringRevision": self.scoring_revision,
            "explicitByteCost": self.explicit_cost,
            "perRevisionCost": self.per_revision_cost,
            "documents": self.documents,
            "diagnostics": self.diagnostics,
            "verifications": self.verifications,
            "stuck": self.stuck,
        }


def load_json_catalog(path: str) -> Any:
    return parse_json_file(path)


def revisions_from_manifest(manifest: Dict[str, Any]) -> List[Revision]:
    """Build revisions from a manifest dictionary.

    ``manifest`` keys:

    * ``packageOrder`` - optional sequence fixing the shared order.
    * ``revisions`` - list of ``{"name", "json", "fileOrder"?, "enabled"?,
      "note"?}`` entries.
    """
    revisions: List[Revision] = []
    for entry in manifest.get("revisions", []):
        document = load_json_catalog(entry["json"])
        revisions.append(
            Revision(
                name=entry.get("name", entry["json"]),
                document=document,
                file_order=entry.get("fileOrder"),
                enabled=entry.get("enabled", True),
                note=entry.get("note"),
            )
        )
    return revisions


def run_handoff(
    revisions: Sequence[Revision],
    package_order: Optional[Sequence[str]] = None,
    verify_small_cases: bool = True,
    output_directory: Optional[str] = None,
) -> HandoffResult:
    """Plan and render a handoff for ``revisions`` under ``package_order``.

    If the revisions individually place but share no total order, nothing is
    rendered; the result names the stuck revisions and their cycle instead.
    """
    included = [revision for revision in revisions if revision.enabled]
    try:
        if package_order is not None:
            decision = choose_package_order(revisions, candidate_order=list(package_order))
        else:
            decision = choose_package_order(revisions)
    except NoCommonPackageOrder as error:
        result = HandoffResult(package_order=[], scoring_revision=None, explicit_cost=0, per_revision_cost={})
        result.stuck = [_stuck_record(item) for item in error.diagnosed]
        return result

    result = HandoffResult(
        package_order=decision.order,
        scoring_revision=decision.scoring_revision,
        explicit_cost=decision.explicit_cost,
        per_revision_cost=dict(decision.per_revision_cost),
    )
    if decision.missing_packages:
        result.diagnostics.append(
            {
                "kind": "packages-not-in-order",
                "reason": "packages skipped because the shared order does not mention them",
                "packages": decision.missing_packages,
            }
        )

    for revision in included:
        analysis = analyze_revision(revision)
        result.diagnostics.extend(_relationship_diagnostics(analysis))
        try:
            placement, explicit, cost = best_placement(analysis, decision.order)
        except ValueError as error:
            result.diagnostics.append(
                {"kind": "unplaceable-revision", "revision": revision.name, "reason": str(error)}
            )
            continue
        text = write_revision(analysis, placement)
        result.documents[revision.name] = text
        if verify_small_cases and output_directory is not None:
            path = _verification_path(output_directory, revision.name)
            verification = verify_reread(analysis, placement, path)
            verification["cost"] = cost
            result.verifications.append(verification)

    for diagnosed in diagnose_stuck_revisions(included):
        result.stuck.append(_stuck_record(diagnosed))
    return result


def _stuck_record(diagnosed) -> Dict:
    return {
        "revision": diagnosed.revision.name,
        "kind": diagnosed.diagnostic["kind"],
        "reason": diagnosed.diagnostic["reason"],
        "cycle": diagnosed.diagnostic.get("cycle", []),
    }


def _relationship_diagnostics(analysis: RevisionAnalysis) -> List[Dict]:
    diagnostics: List[Dict] = []
    for relationship in analysis.relationships:
        ok, reason = classify_relationship(relationship)
        if not ok:
            diagnostics.append(
                {
                    "kind": "unwritable-relationship",
                    "revision": analysis.revision.name,
                    "reason": reason,
                    "relationship": describe_relationship(relationship),
                }
            )
    return diagnostics


def _verification_path(directory: str, name: str) -> str:
    return os.path.join(directory, safe_name(name) + ".spdx")


def safe_name(name: str) -> str:
    """Turn a revision name into a portable file stem."""
    return "".join(character if character.isalnum() or character in "-_." else "_" for character in name)


def handoff_from_manifest_file(manifest_path: str, output_directory: Optional[str] = None) -> HandoffResult:
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    revisions = revisions_from_manifest(manifest)
    result = run_handoff(
        revisions,
        package_order=manifest.get("packageOrder"),
        verify_small_cases=manifest.get("verifySmallCases", True),
        output_directory=output_directory,
    )
    return result
