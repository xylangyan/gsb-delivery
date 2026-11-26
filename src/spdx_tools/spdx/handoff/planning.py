# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Placement planning for a JSON to TagValue handoff.

A revision is a JSON catalog already parsed by ``spdx_tools`` into a
:class:`~spdx_tools.spdx.model.Document`.  When such a document is written to
TagValue and read back, the reader invents a ``CONTAINS`` relationship for every
file that directly follows a package, and it de-duplicates that inferred
relationship against an identical explicit one.  To end up with *exactly* the
relationship multiset of the JSON, the writer must therefore either:

* give the reader back a relationship it would infer anyway (a free
  ``CONTAINS pkg file``, with no comment), or
* write the relationship explicitly in the trailing ``Relationships`` section,
  and make sure no file placement would introduce an unwanted inferred copy.

This module decides, for a fixed package order shared by every revision, where
packages are placed among the locked file order so that the most expensive
revision writes the fewest explicit relationship bytes.
"""

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from spdx_tools.spdx.handoff.encoding import (
    classify_relationship,
    describe_relationship,
    relationship_byte_cost,
    relationship_key,
)
from spdx_tools.spdx.model import Document, Relationship, RelationshipType

# Placement of a package relative to the locked file order: gap ``i`` means the
# package is emitted after the first ``i`` files (gap ``len(files)`` trails all
# files).  Several packages may share a gap and keep their relative order.
Gap = int


@dataclass
class Revision:
    """One JSON catalog and the constraints carried by its revision order."""

    name: str
    document: Document
    # File order is locked by the revision order.  ``None`` keeps the document's
    # own order.
    file_order: Optional[List[str]] = None
    # Only the interactions named by the handoff order are effective.  ``None``
    # means "use the document as parsed".
    enabled: bool = True
    note: Optional[str] = None
    # Allowed file gaps for each package.  Gap ``i`` is the position after the
    # first ``i`` files.  ``None`` allows every gap; an empty list means the
    # revision order does not permit that package at all.
    allowed_gaps: Optional[Dict[str, List[Gap]]] = None

    def resolved_file_order(self) -> List[str]:
        if self.file_order is None:
            return [file.spdx_id for file in self.document.files]
        return list(self.file_order)

    def gaps_for(self, package_id: str, file_count: int) -> List[Gap]:
        if self.allowed_gaps is None or package_id not in self.allowed_gaps:
            return list(range(file_count + 1))
        return [gap for gap in self.allowed_gaps[package_id] if 0 <= gap <= file_count]


@dataclass
class Placement:
    """An assignment of packages to gaps for a single revision."""

    packages_at_gap: Dict[Gap, List[str]] = field(default_factory=dict)

    def gap_of(self, package_id: str) -> Optional[Gap]:
        for gap, package_ids in self.packages_at_gap.items():
            if package_id in package_ids:
                return gap
        return None


@dataclass
class RevisionAnalysis:
    revision: Revision
    file_order: List[str]
    packages: List[str]
    relationships: List[Relationship]
    writable: List[Relationship]
    diagnostics: List[Dict]
    explicit_cost: int
    inferred: List[Relationship]


@dataclass
class PackageOrderDecision:
    order: List[str]
    missing_packages: List[str]
    scoring_revision: Optional[str]
    explicit_cost: int
    per_revision_cost: Dict[str, int]


@dataclass
class DiagnosedRevision:
    revision: Revision
    analysis: RevisionAnalysis
    diagnostic: Dict


def _document_package_ids(document: Document) -> List[str]:
    return [package.spdx_id for package in document.packages]


def _document_file_ids(document: Document) -> List[str]:
    return [file.spdx_id for file in document.files]


def analyze_revision(revision: Revision) -> RevisionAnalysis:
    """Split a revision's relationships into explicit and inferred parts."""
    document = revision.document
    file_ids = _document_file_ids(document)
    package_ids = _document_package_ids(document)
    file_order = revision.resolved_file_order()
    missing = [file_id for file_id in file_order if file_id not in set(file_ids)]
    if missing:
        raise ValueError(f"revision {revision.name!r} names unknown files in its locked order: {missing}")

    writable: List[Relationship] = []
    diagnostics: List[Dict] = []
    for relationship in document.relationships:
        ok, reason = classify_relationship(relationship)
        if ok:
            writable.append(relationship)
        else:
            diagnostics.append(
                {
                    "kind": "unwritable-relationship",
                    "reason": reason,
                    "relationship": describe_relationship(relationship),
                }
            )

    return RevisionAnalysis(
        revision=revision,
        file_order=file_order,
        packages=package_ids,
        relationships=list(document.relationships),
        writable=writable,
        diagnostics=diagnostics,
        explicit_cost=0,
        inferred=[],
    )


def _count_relationships(relationships: Sequence[Relationship]) -> Dict[Tuple, int]:
    counts: Dict[Tuple, int] = {}
    for relationship in relationships:
        key = relationship_key(relationship)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _key_for(origin: str, relationship_type: RelationshipType, target: str) -> Tuple:
    return (origin, relationship_type, target, None)


def _inferred_for_placement(
    analysis: RevisionAnalysis, placement: Placement
) -> Tuple[List[Tuple], List[Dict]]:
    """Return inferred relationship keys and diagnostics for a placement.

    A file placed directly after a package yields one inferred
    ``CONTAINS package file``.  Two adjacent packages must not both claim the
    same file: the reader only remembers the most recent package, so a package
    placed immediately before another package can never own the following file.
    """
    keys: List[Tuple] = []
    diagnostics: List[Dict] = []
    current_package: Optional[str] = None
    packages_by_gap = placement.packages_at_gap
    file_ids = analysis.file_order

    # Walk files and the packages attached before each one.
    for index, file_id in enumerate(file_ids):
        for package_id in packages_by_gap.get(index, []):
            current_package = package_id
        if current_package is not None:
            keys.append(_key_for(current_package, RelationshipType.CONTAINS, file_id))
    return keys, diagnostics


def _explicit_relationships(
    analysis: RevisionAnalysis, placement: Placement
) -> Tuple[List[Relationship], List[Dict]]:
    """Relationships that must still be written explicitly for a placement."""
    inferred_keys, diagnostics = _inferred_for_placement(analysis, placement)
    inferred_counts = _count_relationships_keys(inferred_keys)

    wanted_counts = _count_relationships(analysis.writable)
    # A file placement can only reproduce a relationship the JSON actually has,
    # and it can never reproduce more copies than the JSON has.  Any other
    # inferred CONTAINS would add a relationship the reread would show as extra.
    for key, count in inferred_counts.items():
        if key not in wanted_counts or count > wanted_counts[key]:
            diagnostics.append(_conflict_for_key(key))
    if any(diagnostic["kind"] == "placement-conflict" for diagnostic in diagnostics):
        return [], diagnostics

    remaining = dict(wanted_counts)
    for key, count in inferred_counts.items():
        remaining[key] -= count

    explicit: List[Relationship] = []
    by_key: Dict[Tuple, List[Relationship]] = {}
    for relationship in analysis.writable:
        by_key.setdefault(relationship_key(relationship), []).append(relationship)
    for key, count in remaining.items():
        if count > 0:
            explicit.extend(by_key[key][:count])
    return explicit, diagnostics


def _count_relationships_keys(keys: Sequence[Tuple]) -> Dict[Tuple, int]:
    counts: Dict[Tuple, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return counts


def _conflict_for_key(key: Tuple) -> Dict:
    return {
        "kind": "placement-conflict",
        "reason": "file placement would infer a CONTAINS relationship the JSON does not contain",
        "relationship": {
            "spdxElementId": key[0],
            "relationshipType": key[1].name,
            "relatedSpdxElement": key[2],
            "comment": key[3],
        },
    }


def candidate_placements(
    analysis: RevisionAnalysis, order: Sequence[str]
) -> List[Placement]:
    """All placements that put packages in ``order`` among the locked files.

    ``order`` is the shared package declaration order.  Packages absent from a
    revision are skipped.  The gaps are non-decreasing in that order, so the
    emitted package declaration order is exactly the shared order.  The number
    of placements is ``C(files + packages, packages)`` at worst; revisions in a
    handoff are small enough for exhaustive search.
    """
    packages = [package_id for package_id in order if package_id in analysis.packages]
    extra = [package_id for package_id in analysis.packages if package_id not in packages]
    if extra:
        # Packages the shared order does not mention still have to be declared;
        # keep them after the shared ones in their original relative order.
        packages = packages + extra
    file_count = len(analysis.file_order)
    allowed = [revision_allowed_gaps(analysis, package_id, file_count) for package_id in packages]
    placements: List[Placement] = []
    # Each package takes one allowed gap, and gaps must not decrease so that the
    # declaration order stays the shared order.
    for gaps in itertools.product(*allowed):
        concrete = [gap for gap in gaps if gap is not None]
        if any(earlier > later for earlier, later in zip(concrete, concrete[1:])):
            continue
        assignment: Dict[int, List[str]] = {}
        for package_id, gap in zip(packages, gaps):
            if gap is None:
                continue
            assignment.setdefault(gap, []).append(package_id)
        placements.append(Placement(assignment))
    return placements


def revision_allowed_gaps(analysis: RevisionAnalysis, package_id: str, file_count: int) -> List[Optional[Gap]]:
    """Allowed gaps for one package, or ``[None]`` if the revision forbids it."""
    gaps = analysis.revision.gaps_for(package_id, file_count)
    return list(gaps) if gaps else [None]


def _score_placement(analysis: RevisionAnalysis, placement: Placement) -> Optional[Tuple[int, int]]:
    """Score a placement as ``(explicit cost, inferred count)`` or ``None``."""
    explicit, diagnostics = _explicit_relationships(analysis, placement)
    if any(diagnostic["kind"] == "placement-conflict" for diagnostic in diagnostics):
        return None
    try:
        cost = sum(relationship_byte_cost(relationship) for relationship in explicit)
    except ValueError:
        return None
    return cost, len(_inferred_for_placement(analysis, placement)[0])


def best_placement(analysis: RevisionAnalysis, order: Sequence[str]) -> Tuple[Placement, List[Relationship], int]:
    """Pick the placement with the fewest explicit relationship bytes."""
    best: Optional[Tuple[Tuple[int, int], Placement, List[Relationship]]] = None
    for placement in candidate_placements(analysis, order):
        if not _placement_declares_every_package(analysis, placement):
            continue
        score = _score_placement(analysis, placement)
        if score is None:
            continue
        explicit, _ = _explicit_relationships(analysis, placement)
        if best is None or score < best[0]:
            best = (score, placement, explicit)
    if best is None:
        raise ValueError(f"no valid placement for revision {analysis.revision.name!r}")
    score, placement, explicit = best
    return placement, explicit, score[0]


def _placement_declares_every_package(analysis: RevisionAnalysis, placement: Placement) -> bool:
    declared = {package_id for package_ids in placement.packages_at_gap.values() for package_id in package_ids}
    return all(package_id in declared for package_id in analysis.packages)


def choose_package_order(
    revisions: Sequence[Revision],
    candidate_order: Optional[Sequence[str]] = None,
) -> PackageOrderDecision:
    """Choose a common package declaration order that minimises the worst case.

    ``candidate_order`` restricts and fixes the order (handy for the "pre
    determined order" delivered up front).  Otherwise every order consistent
    with the union of the revisions' own package orders is tried.
    """
    analyses = [analyze_revision(revision) for revision in revisions if revision.enabled]
    if not analyses:
        return PackageOrderDecision([], [], None, 0, {})

    universe: List[str] = []
    for analysis in analyses:
        for package_id in analysis.packages:
            if package_id not in universe:
                universe.append(package_id)

    if candidate_order is not None:
        orders = [list(candidate_order)]
    else:
        orders = _orders_consistent_with_revisions(analyses, universe)

    best: Optional[PackageOrderDecision] = None
    for order in orders:
        per_revision: Dict[str, int] = {}
        feasible = True
        for analysis in analyses:
            try:
                _, _, cost = best_placement(analysis, order)
            except ValueError:
                feasible = False
                break
            per_revision[analysis.revision.name] = cost
        if not feasible:
            continue
        worst = max(per_revision.values()) if per_revision else 0
        scoring = max(per_revision, key=lambda name: per_revision[name]) if per_revision else None
        decision = PackageOrderDecision(
            order=[package_id for package_id in order if package_id in universe],
            missing_packages=[package_id for package_id in universe if package_id not in order],
            scoring_revision=scoring,
            explicit_cost=worst,
            per_revision_cost=per_revision,
        )
        if best is None or decision.explicit_cost < best.explicit_cost:
            best = decision
    if best is None:
        stuck = diagnose_stuck_revisions(revisions)
        if stuck:
            names = ", ".join(sorted(item.revision.name for item in stuck))
            raise NoCommonPackageOrder(
                f"no common package order exists; stuck revisions: {names}", stuck
            )
        raise ValueError("no common package order satisfies every revision")
    return best


class NoCommonPackageOrder(ValueError):
    """Raised when the revisions individually place but share no total order."""

    def __init__(self, message: str, diagnosed: List["DiagnosedRevision"]):
        super().__init__(message)
        self.diagnosed = diagnosed


def _orders_consistent_with_revisions(
    analyses: Sequence[RevisionAnalysis], universe: Sequence[str]
) -> List[List[str]]:
    """Orders that respect every revision's own package order (a topological sort)."""
    precedence: Dict[str, set] = {package_id: set() for package_id in universe}
    for analysis in analyses:
        packages = analysis.packages
        for earlier, later in zip(packages, packages[1:]):
            precedence[later].add(earlier)

    orders: List[List[str]] = []

    def visit(prefix: List[str], remaining: List[str]):
        if not remaining:
            orders.append(list(prefix))
            return
        for candidate in remaining:
            if all(predecessor in prefix for predecessor in precedence[candidate]):
                visit(prefix + [candidate], [item for item in remaining if item != candidate])

    visit([], [package_id for package_id in universe])
    return orders


def diagnose_stuck_revisions(revisions: Sequence[Revision]) -> List[DiagnosedRevision]:
    """Return revisions that individually place but share no common order.

    Compatibility is decided by building one precedence graph over every
    revision's own package order; a cycle means no shared total order exists.
    Every revision that contributes an edge of the cycle is reported, so a
    disagreement that only appears through a chain of three or more revisions is
    still found (a purely pairwise check would miss it).
    """
    analyses = [analyze_revision(revision) for revision in revisions if revision.enabled]
    edges: List[Tuple[str, str, RevisionAnalysis]] = []
    for analysis in analyses:
        packages = analysis.packages
        for earlier, later in zip(packages, packages[1:]):
            edges.append((earlier, later, analysis))

    cycle = _cycle_in_edges(edges)
    if not cycle:
        return []
    cycle_pairs = set(zip(cycle, cycle[1:] + cycle[:1]))
    reported: Dict[str, DiagnosedRevision] = {}
    for earlier, later, analysis in edges:
        if (earlier, later) in cycle_pairs and analysis.revision.name not in reported:
            reported[analysis.revision.name] = DiagnosedRevision(
                revision=analysis.revision,
                analysis=analysis,
                diagnostic={
                    "kind": "incompatible-package-order",
                    "reason": "the revisions' package orders form a cycle with no common total order",
                    "cycle": list(cycle),
                },
            )
    return list(reported.values())


def _cycle_in_edges(edges: Sequence[Tuple[str, str, object]]) -> Optional[List[str]]:
    """Return a precedence cycle as a package list, or ``None`` if acyclic."""
    successors: Dict[str, List[str]] = {}
    for earlier, later, _ in edges:
        successors.setdefault(earlier, [])
        if later not in successors[earlier]:
            successors[earlier].append(later)
        successors.setdefault(later, [])

    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {node: WHITE for node in successors}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        colour[node] = GREY
        stack.append(node)
        for successor in successors[node]:
            if colour[successor] == GREY:
                start = stack.index(successor)
                return stack[start:] + [successor]
            if colour[successor] == WHITE:
                cycle = visit(successor)
                if cycle:
                    return cycle
        stack.pop()
        colour[node] = BLACK
        return None

    for node in successors:
        if colour[node] == WHITE:
            cycle = visit(node)
            if cycle:
                return cycle
    return None
