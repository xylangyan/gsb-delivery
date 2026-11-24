"""Occurrence-level provenance across a chain of archive versions.

Graphtage's ordinary diff answers one question: *one* cheapest way to transform one tree into another. An archive
audit needs a different answer. Every appearance of a row is registered separately, so two rows whose bodies are
byte-for-byte identical are still two different occurrences. A single cheapest diff inevitably picks one of them and
hides every other equally cheap explanation.

This module keeps Graphtage's own price for every base edit -- :class:`graphtage.Match`,
:class:`graphtage.Remove`, :class:`graphtage.Insert`, and whatever edit a pair of nodes returns from
:meth:`graphtage.TreeNode.edits` -- and sums those prices over the edits that actually occur. It then computes, for
every adjacent pair of versions, the minimum total price and every occurrence correspondence that occurs in at least
one minimum-price alignment. The two adjacent stages are composed through the actual intermediate occurrences, never
through equal-looking bodies, so one middle occurrence cannot be lent to two copies.

The entry point is :func:`audit`. It accepts three JSON-compatible versions (or paths), the list-valued sections that
carry registered occurrences, the reviewed identity evidence, and the third-version registration whose ancestry is
wanted.

Evidence model
--------------

The reviewed table is a set of :class:`IdentityEvidence` items:

``same``
    Two occurrences are one entity. Between adjacent versions this is a hard requirement: every minimum-price
    alignment of that stage must pair them. Between the first and third versions it directly certifies the
    ancestry, because the reviewer has already done the cross-version identification the diff might miss.

``different``
    Two occurrences must not be treated as one entity. Between adjacent versions the pair is forbidden. Across any
    versions, the two may not end up in the same entity class.

If honouring adjacent-version evidence requires paying more than that stage's unconstrained minimum, the result is a
:class:`AuditConflict`. The more expensive alignment is reported at its evidence-required price, never as the
original price.

Example:
    >>> first = {"rows": [{"sku": "A", "qty": 1}]}
    >>> second = {"rows": [{"sku": "A", "qty": 1}]}
    >>> third = {"rows": [{"sku": "A", "qty": 1}]}
    >>> result = audit([first, second, third], sections=["rows"], target=0)
    >>> result.confirmed
    True
    >>> result.first_version_registrations
    (0,)

"""

import io
import json
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from . import json as graphtage_json
from .bounds import Range
from .edits import Insert, Remove
from .graphtage import ListNode, UnorderedListNode
from .printer import Printer
from .sequences import SequenceEdit
from .tree import CompoundEdit, Edit, TreeNode

__all__ = [
    "AuditConflict",
    "AuditResult",
    "IdentityEvidence",
    "LineageError",
    "LineageLimitError",
    "Occurrence",
    "OccurrenceRef",
    "SectionSpec",
    "TraceExplanation",
    "audit",
    "different",
    "same",
]

_INFINITY = 10**12
"""A cost above any real edit price. Real prices are sums of node sizes, far below this."""

_ASSIGNMENT_INFINITY = 10**12
"""The forbidden-cell sentinel for an assignment matrix. It must dominate every real cell while staying in int64."""

_SUPPORT_PAIR_LIMIT = 20_000
"""The largest number of occurrence pairs for which per-edge all-optimal support is computed.

Beyond this, the stage minimum, the conflict verdict, and the reconstructed explanation are still exact, but
:attr:`AuditResult.exhaustive` is :const:`False` because the support set is not checked edge by edge.

"""

_DEFAULT_EXPLANATION_LIMIT = 2
"""The number of concrete explanations rendered when the evidence admits more than one attribution."""

_ENUMERATION_CAP = 20_000
"""The largest number of minimum-price alignments retained for one stage."""

_IDENTITY_SAME_KEYS = ("same", "identical", "same_entity", "same_entity_pairs")
_IDENTITY_DIFFERENT_KEYS = ("different", "distinct", "not_same", "cannot_be_same", "different_pairs")
_REGISTRATION_KEYS = ("registration", "registration_number", "登记号", "登记编号")


class LineageError(ValueError):
    """Raised when the versions, sections, or identity table cannot describe an archive."""


class LineageLimitError(LineageError):
    """Raised when a section is too large for exact all-optimal analysis."""


@dataclass(frozen=True)
class SectionSpec:
    """One list-valued section of the archive and how its occurrences are compared.

    Args:
        path: The path from the version root to the list. A string names a top-level key; a sequence names a
            nested path. ``()`` means the version root itself is the list of occurrences.
        ordered: :const:`True` if occurrences are compared as an ordered sequence, :const:`False` if they are
            compared as an unordered multiset. An unordered section still counts multiplicity: two identical rows
            are two occurrences, not one.
        registration_key: The member key holding the archive's registration number. If omitted, a key named
            ``registration``, ``registration_number``, ``登记号``, or ``登记编号`` is used when present; otherwise
            registrations are assigned in document order.
        strip_registration_key: Remove the registration member from a row before pricing it. Registration numbers
            are archive metadata, so this is usually what an audit wants when identifiers are stored inline.

    """

    path: tuple[str, ...]
    ordered: bool
    registration_key: str | None
    strip_registration_key: bool

    def __init__(
            self,
            path: str | Sequence[str] = (),
            ordered: bool = True,
            registration_key: str | None = None,
            strip_registration_key: bool = False,
    ):
        if isinstance(path, str):
            path = (path,)
        object.__setattr__(self, "path", tuple(path))
        object.__setattr__(self, "ordered", bool(ordered))
        object.__setattr__(self, "registration_key", registration_key)
        object.__setattr__(self, "strip_registration_key", bool(strip_registration_key))

    def __repr__(self):
        mode = "ordered" if self.ordered else "unordered"
        return f"SectionSpec(path={self.path!r}, {mode}, registration_key={self.registration_key!r})"


@dataclass(frozen=True)
class OccurrenceRef:
    """A reference to one registered appearance.

    ``version`` is the zero-based index of the version in the :func:`audit` call. ``registration`` is the archive's
    registration number for that appearance. Registrations identify appearances, not entities: one entity carried
    from one version to the next normally has a different registration in each version.

    """

    version: int
    registration: Any

    def __str__(self):
        return f"v{self.version + 1}:{self.registration}"


@dataclass(eq=False)
class Occurrence:
    """One appearance of a row in one section of one version."""

    version: int
    section: tuple[str, ...]
    position: int
    registration: Any
    value: Any
    node: TreeNode

    def __hash__(self):
        return id(self)

    @property
    def ref(self) -> OccurrenceRef:
        return OccurrenceRef(self.version, self.registration)

    def __repr__(self):
        return f"Occurrence(v{self.version + 1}:{self.registration!r}, section={self.section!r}, pos={self.position})"


@dataclass(frozen=True)
class IdentityEvidence:
    """One reviewed statement about two occurrences.

    ``same`` is :const:`True` when the reviewer confirmed that the two appearances are one entity, and
    :const:`False` when the reviewer confirmed that they must not be treated as one entity.

    """

    left: OccurrenceRef
    right: OccurrenceRef
    same: bool


def same(left, right) -> IdentityEvidence:
    """Builds evidence that two occurrences are the same entity."""
    return IdentityEvidence(_as_ref(left), _as_ref(right), True)


def different(left, right) -> IdentityEvidence:
    """Builds evidence that two occurrences must not be treated as one entity."""
    return IdentityEvidence(_as_ref(left), _as_ref(right), False)


def _parse_registration(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def _as_ref(value) -> OccurrenceRef:
    if isinstance(value, OccurrenceRef):
        return value
    if isinstance(value, Occurrence):
        return value.ref
    if isinstance(value, Mapping):
        if "version" not in value or "registration" not in value:
            raise LineageError(f"An occurrence reference needs 'version' and 'registration': {value!r}")
        return OccurrenceRef(int(value["version"]), value["registration"])
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return OccurrenceRef(int(value[0]), value[1])
    if isinstance(value, str) and ":" in value and value[:1] in ("v", "V"):
        version, _, registration = value[1:].partition(":")
        try:
            return OccurrenceRef(int(version) - 1, _parse_registration(registration))
        except ValueError:
            pass
    raise LineageError(f"Cannot interpret {value!r} as an occurrence reference")


def _normalize_evidence(evidence) -> list[IdentityEvidence]:
    """Accepts the forms of identity evidence that callers commonly hand an audit."""
    if evidence is None:
        return []
    if isinstance(evidence, IdentityEvidence):
        return [evidence]
    if isinstance(evidence, Mapping):
        pairs = []
        for key in _IDENTITY_SAME_KEYS:
            for entry in evidence.get(key, ()) or ():
                pairs.append(entry if isinstance(entry, IdentityEvidence) else same(entry[0], entry[1]))
        for key in _IDENTITY_DIFFERENT_KEYS:
            for entry in evidence.get(key, ()) or ():
                pairs.append(entry if isinstance(entry, IdentityEvidence) else different(entry[0], entry[1]))
        return pairs
    result = []
    for entry in evidence:
        if isinstance(entry, IdentityEvidence):
            result.append(entry)
        else:
            result.append(IdentityEvidence(_as_ref(entry[0]), _as_ref(entry[1]), bool(entry[2])))
    return result


def _normalize_sections(sections) -> list[SectionSpec]:
    if sections is None:
        return []
    result = []
    for section in sections:
        if isinstance(section, SectionSpec):
            result.append(section)
        elif isinstance(section, str):
            result.append(SectionSpec(section))
        elif isinstance(section, Mapping):
            result.append(SectionSpec(
                section.get("path", ()),
                ordered=section.get("ordered", True),
                registration_key=section.get("registration_key"),
                strip_registration_key=section.get("strip_registration_key", False),
            ))
        else:
            result.append(SectionSpec(section))
    return result


@dataclass(frozen=True)
class _Op:
    """One step of an alignment: pair two occurrences, remove one, or insert one."""

    kind: str
    left: int | None = None
    right: int | None = None


@dataclass(frozen=True)
class Alignment:
    """One concrete alignment between two occurrence lists."""

    ops: tuple[_Op, ...]
    cost: int

    def pairs(self) -> tuple[tuple[int, int], ...]:
        return tuple((op.left, op.right) for op in self.ops if op.kind == "pair")

    def removed(self) -> tuple[int, ...]:
        return tuple(op.left for op in self.ops if op.kind == "remove")

    def inserted(self) -> tuple[int, ...]:
        return tuple(op.right for op in self.ops if op.kind == "insert")


@dataclass
class StageSolution:
    """The minimum price and the all-optimal evidence of one section of one adjacent stage.

    ``cost`` is the stage's unconstrained minimum. ``evidence_cost`` is the cheapest alignment that honours the
    reviewed evidence for this stage, and ``evidence_conflict`` is true when the two differ.

    """

    cost: int
    pair_costs: list[list[int]]
    remove_costs: list[int]
    insert_costs: list[int]
    ordered: bool
    penalty: int
    pair_support: set[tuple[int, int]] = field(default_factory=set)
    removed_support: set[int] = field(default_factory=set)
    inserted_support: set[int] = field(default_factory=set)
    evidence_cost: int | None = None
    evidence_conflict: bool = False
    exhaustive: bool = True
    alignments: list["Alignment"] | None = None

    def __post_init__(self):
        if self.evidence_cost is None:
            self.evidence_cost = self.cost

    def alignment_cost(self, ops: Sequence[_Op]) -> int:
        """Returns the summed Graphtage price of a concrete alignment."""
        total = 0
        for op in ops:
            if op.kind == "pair":
                total += self.pair_costs[op.left][op.right]
            elif op.kind == "remove":
                total += self.remove_costs[op.left]
            else:
                total += self.insert_costs[op.right]
        return total

    def supported(self, op: _Op) -> bool:
        """Returns whether an op occurs in some unconstrained minimum-price alignment."""
        if op.kind == "pair":
            return (op.left, op.right) in self.pair_support
        if op.kind == "remove":
            return op.left in self.removed_support
        return op.right in self.inserted_support


@dataclass(frozen=True)
class TraceExplanation:
    """One evidence-consistent, minimum-price explanation for the target.

    ``origin`` is ``"first"`` when the target traces to a first-version registration, ``"midway"`` when it first
    appears in the second version, and ``"third"`` when it is inserted in the third version itself.

    """

    origin: str
    registration: Any | None
    first_version_registration: Any | None
    path: tuple[OccurrenceRef, ...]
    diffs: tuple[str, ...]
    stage_costs: tuple[int, ...]
    evidence_used: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "registration": self.registration,
            "first_version_registration": self.first_version_registration,
            "path": [str(ref) for ref in self.path],
            "path_refs": [[ref.version, ref.registration] for ref in self.path],
            "stage_costs": list(self.stage_costs),
            "diffs": list(self.diffs),
            "evidence_used": list(self.evidence_used),
        }


@dataclass(frozen=True)
class AuditConflict:
    """Why no original-price explanation honours the reviewed evidence."""

    kind: str
    message: str
    stage: int | None = None
    original_cost: int | None = None
    required_cost: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "stage": self.stage,
            "original_cost": self.original_cost,
            "required_cost": self.required_cost,
            "detail": self.detail,
        }


@dataclass
class AuditResult:
    """The provenance answer for one third-version registration."""

    target: OccurrenceRef
    section: tuple[str, ...]
    confirmed: bool
    first_version_registrations: tuple[Any, ...]
    introduced_midway: bool
    introduced_at_third: bool
    origins: tuple[tuple[str, Any | None], ...]
    explanations: tuple[TraceExplanation, ...]
    stage_costs: tuple[int, ...]
    exhaustive: bool
    conflict: AuditConflict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": {"version": self.target.version, "registration": self.target.registration},
            "section": list(self.section),
            "confirmed": self.confirmed,
            "first_version_registrations": list(self.first_version_registrations),
            "introduced_midway": self.introduced_midway,
            "introduced_at_third": self.introduced_at_third,
            "origins": [{"origin": kind, "registration": reg} for kind, reg in self.origins],
            "stage_costs": list(self.stage_costs),
            "exhaustive": self.exhaustive,
            "explanations": [explanation.to_dict() for explanation in self.explanations],
            "conflict": self.conflict.to_dict() if self.conflict is not None else None,
        }


def _exact_cost(edit: Edit) -> int:
    """Tightens an edit to a definitive bound and returns its Graphtage cost."""
    while getattr(edit, "valid", True) and not edit.bounds().definitive() and edit.tighten_bounds():
        pass
    bounds = edit.bounds()
    if not bounds.definitive():
        raise LineageError(f"Could not tighten {edit!r} to a definitive cost; got {bounds!r}")
    return int(bounds.upper_bound)


def _realized_cost(edit: Edit) -> int:
    """Returns the sum of the base edits an edit script actually performs.

    Graphtage's compound edits can report an aggregate bound that is lower than the base edits they contain. The
    audit must charge what actually happens, so a compound edit is expanded and its leaf edits are summed. Leaf
    edits are priced exactly as Graphtage itself prices them.

    """
    if isinstance(edit, CompoundEdit):
        total = 0
        for sub_edit in edit.edits():
            total += _realized_cost(sub_edit)
        return total
    return _exact_cost(edit)


def _node_cost(from_node: TreeNode, to_node: TreeNode) -> int:
    """Returns the summed base-edit price of editing one node into another."""
    return _realized_cost(from_node.edits(to_node))


def _resolve_path(version: Any, path: tuple[str, ...]) -> Any:
    """Follows a section path from a version root, treating a missing path as an empty section."""
    value = version
    for part in path:
        if not isinstance(value, Mapping) or part not in value or value[part] is None:
            return []
        value = value[part]
    if not isinstance(value, list):
        raise LineageError(f"Section {path!r} is not a list in this version")
    return value


def _registration_of(row: Any, spec: SectionSpec, position: int) -> Any:
    """Returns the registration number the archive assigned to one row."""
    if spec.registration_key is not None:
        if not isinstance(row, Mapping) or spec.registration_key not in row:
            raise LineageError(
                f"Row {position} in section {spec.path!r} has no registration key {spec.registration_key!r}"
            )
        return row[spec.registration_key]
    if isinstance(row, Mapping):
        for key in _REGISTRATION_KEYS:
            if key in row:
                return row[key]
    return position


def _body_of(row: Any, spec: SectionSpec) -> Any:
    """Returns a row's body, optionally without its registration member."""
    if not spec.strip_registration_key or not isinstance(row, Mapping):
        return row
    key = spec.registration_key
    if key is None:
        key = next((candidate for candidate in _REGISTRATION_KEYS if candidate in row), None)
    if key is None:
        return row
    return {k: v for k, v in row.items() if k != key}


def _load_version(version: Any) -> Any:
    """Loads a version from a path if it is not already a JSON-compatible value."""
    if isinstance(version, (str, os.PathLike)):
        with open(version, encoding="utf-8") as stream:
            return json.load(stream)
    return version


def _auto_sections(versions: Sequence[Any]) -> list[SectionSpec]:
    """Discovers every list-valued path held by any version."""
    paths: list[tuple[str, ...]] = []

    def discover(value: Any, prefix: tuple[str, ...]):
        if isinstance(value, Mapping):
            for key, item in value.items():
                discover(item, (*prefix, str(key)))
        elif isinstance(value, list) and prefix and prefix not in paths:
            paths.append(prefix)

    for version in versions:
        discover(version, ())
    return [SectionSpec(path, ordered=True) for path in paths]


def _collect(versions: Sequence[Any], specs: Sequence[SectionSpec]
             ) -> list[dict[SectionSpec, list[Occurrence]]]:
    """Registers every occurrence of every section in every version.

    Sections are keyed by the :class:`SectionSpec` itself, not by path, so the same path may be audited once as an
    ordered section and once as an unordered one without either result overwriting the other.

    """
    collected = []
    for version_index, version in enumerate(versions):
        by_section: dict[SectionSpec, list[Occurrence]] = {}
        for spec in specs:
            occurrences = []
            for position, row in enumerate(_resolve_path(version, spec.path)):
                body = _body_of(row, spec)
                occurrences.append(Occurrence(
                    version=version_index,
                    section=spec.path,
                    position=position,
                    registration=_registration_of(row, spec, position),
                    value=body,
                    node=graphtage_json.build_tree(body),
                ))
            by_section[spec] = occurrences
        collected.append(by_section)
    return collected


def _penalty(occurrences: Sequence[Occurrence], ordered: bool) -> int:
    """Returns Graphtage's insertion/removal penalty for a section.

    An ordered list of leaves is compared with zero penalty; any other ordered list and every unordered multiset
    pays one. This mirrors :meth:`graphtage.ListNode.edits` and :class:`graphtage.multiset.MultiSetEdit`.

    """
    if not ordered:
        return 1
    if all(occurrence.node.is_leaf for occurrence in occurrences):
        return 0
    return 1


def _cost_matrix(left: Sequence[Occurrence], right: Sequence[Occurrence], penalty: int
                 ) -> tuple[list[list[int]], list[int], list[int]]:
    """Returns every pair price and every removal and insertion price for a section."""
    pair_costs = [[_node_cost(a.node, b.node) for b in right] for a in left]
    remove_costs = [a.node.total_size + penalty for a in left]
    insert_costs = [b.node.total_size + penalty for b in right]
    return pair_costs, remove_costs, insert_costs


def _check_ordered_requirements(required: set[tuple[int, int]]) -> None:
    """Rejects required pairs that cannot all occur in one ordered alignment."""
    ordered = sorted(required)
    for index, (left_a, right_a) in enumerate(ordered):
        for left_b, right_b in ordered[index + 1:]:
            if (left_a < left_b and right_a >= right_b) or (left_a == left_b and right_a != right_b):
                raise LineageError(
                    "The reviewed identity table requires an ordered alignment to pair "
                    f"{left_a}->{right_a} and {left_b}->{right_b}, which cross in order"
                )


def _band_cost(solution: "StageSolution", first_i: int, last_i: int, first_j: int, last_j: int) -> int:
    """Returns the cheapest price of an ordered rectangle with no required pair inside."""
    height, width = last_i - first_i, last_j - first_j
    if height == 0:
        return sum(solution.insert_costs[first_j:last_j])
    if width == 0:
        return sum(solution.remove_costs[first_i:last_i])
    dp = [[_INFINITY] * (width + 1) for _ in range(height + 1)]
    dp[0][0] = 0
    for i in range(1, height + 1):
        dp[i][0] = dp[i - 1][0] + solution.remove_costs[first_i + i - 1]
    for j in range(1, width + 1):
        dp[0][j] = dp[0][j - 1] + solution.insert_costs[first_j + j - 1]
    for i in range(1, height + 1):
        for j in range(1, width + 1):
            dp[i][j] = min(
                dp[i - 1][j] + solution.remove_costs[first_i + i - 1],
                dp[i][j - 1] + solution.insert_costs[first_j + j - 1],
                dp[i - 1][j - 1] + solution.pair_costs[first_i + i - 1][first_j + j - 1],
            )
    return dp[height][width]


def _enumerate_ordered_alignments(pair_costs, remove_costs, insert_costs, cap):
    """Enumerates every minimum-price ordered alignment, up to *cap*."""
    n, m = len(pair_costs), len(insert_costs)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + remove_costs[i - 1]
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + insert_costs[j - 1]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = min(
                dp[i - 1][j] + remove_costs[i - 1],
                dp[i][j - 1] + insert_costs[j - 1],
                dp[i - 1][j - 1] + pair_costs[i - 1][j - 1],
            )
    optimal = dp[n][m]
    results = []

    def walk(i, j, reversed_ops):
        if len(results) >= cap:
            return
        if i == 0 and j == 0:
            results.append(Alignment(tuple(reversed(reversed_ops)), optimal))
            return
        target = dp[i][j]
        if i and j and dp[i - 1][j - 1] + pair_costs[i - 1][j - 1] == target:
            reversed_ops.append(_Op("pair", i - 1, j - 1))
            walk(i - 1, j - 1, reversed_ops)
            reversed_ops.pop()
        if i and dp[i - 1][j] + remove_costs[i - 1] == target:
            reversed_ops.append(_Op("remove", i - 1, None))
            walk(i - 1, j, reversed_ops)
            reversed_ops.pop()
        if j and dp[i][j - 1] + insert_costs[j - 1] == target:
            reversed_ops.append(_Op("insert", None, j - 1))
            walk(i, j - 1, reversed_ops)
            reversed_ops.pop()

    walk(n, m, [])
    return optimal, results, len(results) < cap


def _enumerate_unordered_alignments(pair_costs, remove_costs, insert_costs, cap):
    """Enumerates every minimum-price unordered alignment, up to *cap*."""
    n, m = len(pair_costs), len(insert_costs)
    memo = {}

    def best(i, mask):
        key = (i, mask)
        if key in memo:
            return memo[key]
        if i == n:
            value = sum(insert_costs[j] for j in range(m) if not (mask >> j) & 1)
        else:
            value = remove_costs[i] + best(i + 1, mask)
            for j in range(m):
                if not (mask >> j) & 1:
                    value = min(value, pair_costs[i][j] + best(i + 1, mask | (1 << j)))
        memo[key] = value
        return value

    optimal = best(0, 0)
    results = []

    def walk(i, mask, ops):
        if len(results) >= cap:
            return
        if i == n:
            final = list(ops)
            for j in range(m):
                if not (mask >> j) & 1:
                    final.append(_Op("insert", None, j))
            results.append(Alignment(tuple(final), optimal))
            return
        if remove_costs[i] + best(i + 1, mask) == best(i, mask):
            walk(i + 1, mask, [*ops, _Op("remove", i, None)])
        for j in range(m):
            if not (mask >> j) & 1 and pair_costs[i][j] + best(i + 1, mask | (1 << j)) == best(i, mask):
                walk(i + 1, mask | (1 << j), [*ops, _Op("pair", i, j)])

    walk(0, 0, [])
    return optimal, results, len(results) < cap


def _filter_alignments(alignments, required, forbidden):
    """Returns the alignments that contain every required pair and no forbidden pair."""
    result = []
    for alignment in alignments:
        pairs = set(alignment.pairs())
        if required and not required.issubset(pairs):
            continue
        if forbidden and pairs & forbidden:
            continue
        result.append(alignment)
    return result


def _ordered_solution(left: Sequence[Occurrence], right: Sequence[Occurrence], penalty: int,
                      required: set[tuple[int, int]] | None = None,
                      forbidden: set[tuple[int, int]] | None = None) -> StageSolution:
    """Solves an ordered section exactly and records every edge in some unconstrained optimum.

    A forward matrix gives the cheapest price of every prefix pair and a backward matrix the cheapest price of every
    suffix pair. A pair ``(i, j)`` occurs in some unconstrained optimum exactly when

    ``forward[i][j] + pair_cost[i][j] + backward[i + 1][j + 1] == minimum``,

    and the analogous identity holds for a removal or an insertion. That is the all-optimal guarantee: every
    optimal script decomposes uniquely around each of its steps.

    """
    n, m = len(left), len(right)
    pair_costs, remove_costs, insert_costs = _cost_matrix(left, right, penalty)
    required = required or set()
    forbidden = forbidden or set()
    _check_ordered_requirements(required)
    solution = StageSolution(
        cost=0,
        pair_costs=pair_costs,
        remove_costs=remove_costs,
        insert_costs=insert_costs,
        ordered=True,
        penalty=penalty,
    )

    forward = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        forward[i][0] = forward[i - 1][0] + remove_costs[i - 1]
    for j in range(1, m + 1):
        forward[0][j] = forward[0][j - 1] + insert_costs[j - 1]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            forward[i][j] = min(
                forward[i - 1][j] + remove_costs[i - 1],
                forward[i][j - 1] + insert_costs[j - 1],
                forward[i - 1][j - 1] + pair_costs[i - 1][j - 1],
            )

    backward = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        backward[i][m] = backward[i + 1][m] + remove_costs[i]
    for j in range(m - 1, -1, -1):
        backward[n][j] = backward[n][j + 1] + insert_costs[j]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            backward[i][j] = min(
                backward[i + 1][j] + remove_costs[i],
                backward[i][j + 1] + insert_costs[j],
                backward[i + 1][j + 1] + pair_costs[i][j],
            )

    optimal = forward[n][m]
    solution.cost = optimal
    if n * m <= _SUPPORT_PAIR_LIMIT:
        enumerated_cost, alignments, complete = _enumerate_ordered_alignments(
            pair_costs, remove_costs, insert_costs, _ENUMERATION_CAP,
        )
        if enumerated_cost != optimal:
            raise LineageError(
                f"The ordered support search found {optimal} but exhaustive enumeration found {enumerated_cost}"
            )
        solution.alignments = alignments
        solution.exhaustive = complete
    else:
        solution.exhaustive = False
    for i in range(n):
        for j in range(m):
            if (i, j) not in forbidden and forward[i][j] + pair_costs[i][j] + backward[i + 1][j + 1] == optimal:
                solution.pair_support.add((i, j))
    for i in range(n):
        if forward[i][m] + remove_costs[i] + backward[i + 1][m] == optimal:
            solution.removed_support.add(i)
    for j in range(m):
        if forward[n][j] + insert_costs[j] + backward[n][j + 1] == optimal:
            solution.inserted_support.add(j)
    if solution.alignments is not None:
        consistent = _filter_alignments(solution.alignments, required, forbidden)
        if consistent:
            solution.evidence_cost = optimal
            solution.evidence_conflict = False
        else:
            solution.evidence_cost = _ordered_best_with_evidence(
                pair_costs, remove_costs, insert_costs, required, forbidden,
            )
            solution.evidence_conflict = True
    else:
        # Enumeration was skipped because the section is large. The evidence-aware band DP still gives the exact
        # required minimum, so it decides the conflict verdict without materializing every alignment.
        solution.evidence_cost = _ordered_best_with_evidence(
            pair_costs, remove_costs, insert_costs, required, forbidden,
        )
        solution.evidence_conflict = solution.evidence_cost != optimal
    return solution


def _assignment_matrix(pair_costs: Sequence[Sequence[int]], remove_costs: Sequence[int],
                       insert_costs: Sequence[int]) -> np.ndarray:
    """Builds the rectangular assignment that prices an unordered multiset alignment.

    Rows are the left occurrences plus one dummy row per right occurrence; columns are the right occurrences plus
    one dummy column per left occurrence. A real pair costs its Graphtage edit price, removing a left occurrence
    costs its removal price through its dummy column, inserting a right occurrence costs its insertion price
    through its dummy row, and every dummy-to-dummy cell costs zero. Every row and every column is assigned, so the
    minimum of this matrix is exactly the minimum of the summed edit script, including the choice of which
    occurrences to leave unpaired.

    """
    n, m = len(pair_costs), len(insert_costs)
    matrix = np.full((n + m, n + m), _ASSIGNMENT_INFINITY, dtype=np.int64)
    for i in range(n):
        for j in range(m):
            matrix[i][j] = pair_costs[i][j]
        matrix[i][m + i] = remove_costs[i]
    for j in range(m):
        matrix[n + j][j] = insert_costs[j]
        for i in range(n):
            matrix[n + j][m + i] = 0
    return matrix


def _assignment_value(matrix: np.ndarray) -> int:
    """Returns the minimum assignment cost of a matrix."""
    rows, columns = linear_sum_assignment(matrix)
    return int(matrix[rows, columns].sum())


def _restricted_optimum(matrix: np.ndarray, forced_rows: set[int], forced_columns: set[int]) -> int:
    """Returns the optimum of an assignment with some rows and columns already consumed."""
    rows = [row for row in range(matrix.shape[0]) if row not in forced_rows]
    columns = [column for column in range(matrix.shape[1]) if column not in forced_columns]
    if not rows:
        return 0
    return _assignment_value(matrix[np.ix_(rows, columns)])


def _check_unordered_requirements(required: set[tuple[int, int]]) -> None:
    """Rejects required pairs that share a row or a column."""
    rows: dict[int, int] = {}
    columns: dict[int, int] = {}
    for left_index, right_index in sorted(required):
        if left_index in rows:
            raise LineageError(
                f"Identity evidence requires {left_index} to pair with both {rows[left_index]} and {right_index}"
            )
        if right_index in columns:
            raise LineageError(
                f"Identity evidence requires {right_index} to pair with both {columns[right_index]} and {left_index}"
            )
        rows[left_index] = right_index
        columns[right_index] = left_index


_CONFLICT_SEARCH_LIMIT = 20_000
"""How many evidence-constrained alignments to search when no stage-minimum one satisfies the review."""


def _ordered_best_with_evidence(pair_costs, remove_costs, insert_costs,
                                required: set[tuple[int, int]],
                                forbidden: set[tuple[int, int]]) -> int:
    """Returns the cheapest ordered alignment containing every required pair and no forbidden pair.

    Required pairs form an increasing chain, so they split the problem into independent bands. Within each band the
    usual recurrence applies with forbidden pair transitions priced out. This is exact and does not enumerate
    alignments, so it also serves sections whose alignment count exceeds the enumeration cap.

    Raises:
        LineageError: If a pair is both required and forbidden, or if the review admits no ordered alignment.

    """
    if required & forbidden:
        raise LineageError(
            f"Identity evidence both requires and forbids the correspondence(s) {sorted(required & forbidden)}"
        )
    n, m = len(pair_costs), len(insert_costs)
    total = 0
    i = j = 0
    for left_index, right_index in sorted(required):
        total += _band_cost_forbidden(pair_costs, remove_costs, insert_costs, i, left_index, j, right_index,
                                      forbidden)
        total += pair_costs[left_index][right_index]
        i, j = left_index + 1, right_index + 1
    total += _band_cost_forbidden(pair_costs, remove_costs, insert_costs, i, n, j, m, forbidden)
    if total >= _INFINITY // 2:
        raise LineageError("The reviewed identity table forbids every ordered alignment of this section")
    return total


def _band_cost_forbidden(pair_costs, remove_costs, insert_costs, first_i, last_i, first_j, last_j,
                         forbidden: set[tuple[int, int]]) -> int:
    """Returns the cheapest price of an ordered rectangle with specified pair transitions forbidden."""
    height, width = last_i - first_i, last_j - first_j
    if height == 0:
        return sum(insert_costs[first_j:last_j])
    if width == 0:
        return sum(remove_costs[first_i:last_i])
    dp = [[_INFINITY] * (width + 1) for _ in range(height + 1)]
    dp[0][0] = 0
    for a in range(1, height + 1):
        dp[a][0] = dp[a - 1][0] + remove_costs[first_i + a - 1]
    for b in range(1, width + 1):
        dp[0][b] = dp[0][b - 1] + insert_costs[first_j + b - 1]
    for a in range(1, height + 1):
        for b in range(1, width + 1):
            pair = (first_i + a - 1, first_j + b - 1)
            best = min(
                dp[a - 1][b] + remove_costs[first_i + a - 1],
                dp[a][b - 1] + insert_costs[first_j + b - 1],
            )
            if pair not in forbidden:
                best = min(best, dp[a - 1][b - 1] + pair_costs[pair[0]][pair[1]])
            dp[a][b] = best
    return dp[height][width]


def _unordered_evidence_cost(pair_costs, remove_costs, insert_costs, required: set[tuple[int, int]],
                             forbidden: set[tuple[int, int]]) -> int:
    """Returns the cheapest unordered alignment that contains every required pair."""
    n, m = len(pair_costs), len(insert_costs)
    matrix = _assignment_matrix(pair_costs, remove_costs, insert_costs)
    total = 0
    forced_rows: set[int] = set()
    forced_columns: set[int] = set()
    for left_index, right_index in required:
        total += pair_costs[left_index][right_index]
        forced_rows.add(left_index)
        forced_columns.add(right_index)
    rows = [row for row in range(n + m) if row not in forced_rows]
    columns = [column for column in range(n + m) if column not in forced_columns]
    if rows:
        submatrix = matrix[np.ix_(rows, columns)].copy()
        for position_row, row in enumerate(rows):
            for position_column, column in enumerate(columns):
                if row < n and column < m and (row, column) in forbidden:
                    submatrix[position_row, position_column] = _ASSIGNMENT_INFINITY
        total += _assignment_value(submatrix)
    return total


def _unordered_solution(left: Sequence[Occurrence], right: Sequence[Occurrence], penalty: int,
                        required: set[tuple[int, int]] | None = None,
                        forbidden: set[tuple[int, int]] | None = None) -> StageSolution:
    """Solves an unordered multiset section exactly and records every edge in some unconstrained optimum.

    The assignment optimum is the true summed edit price. Per-edge support is found by forcing a cell and solving
    the remainder: a pair is supported exactly when fixing it does not raise the optimum. A removal or insertion is
    tested the same way through its dummy cell. This correctly leaves an occurrence unmatched when pairing it costs
    more than removing it.

    """
    pair_costs, remove_costs, insert_costs = _cost_matrix(left, right, penalty)
    required = required or set()
    forbidden = forbidden or set()
    _check_unordered_requirements(required)
    solution = StageSolution(
        cost=0,
        pair_costs=pair_costs,
        remove_costs=remove_costs,
        insert_costs=insert_costs,
        ordered=False,
        penalty=penalty,
    )
    n, m = len(left), len(right)
    if n == 0 and m == 0:
        return solution

    matrix = _assignment_matrix(pair_costs, remove_costs, insert_costs)
    optimal = _assignment_value(matrix)
    solution.cost = optimal
    if n * m <= _SUPPORT_PAIR_LIMIT:
        enumerated_cost, alignments, complete = _enumerate_unordered_alignments(
            pair_costs, remove_costs, insert_costs, _ENUMERATION_CAP,
        )
        if enumerated_cost != optimal:
            raise LineageError(
                f"The unordered assignment found {optimal} but exhaustive enumeration found {enumerated_cost}"
            )
        solution.alignments = alignments
        solution.exhaustive = complete
        for i in range(n):
            for j in range(m):
                if (i, j) in forbidden:
                    continue
                if matrix[i][j] + _restricted_optimum(matrix, {i}, {j}) == optimal:
                    solution.pair_support.add((i, j))
            if matrix[i][m + i] + _restricted_optimum(matrix, {i}, {m + i}) == optimal:
                solution.removed_support.add(i)
        for j in range(m):
            if matrix[n + j][j] + _restricted_optimum(matrix, {n + j}, {j}) == optimal:
                solution.inserted_support.add(j)
    else:
        solution.exhaustive = False
    if solution.alignments is not None:
        consistent = _filter_alignments(solution.alignments, required, forbidden)
        if consistent:
            solution.evidence_cost = optimal
            solution.evidence_conflict = False
        else:
            solution.evidence_cost = _unordered_evidence_cost(
                pair_costs, remove_costs, insert_costs, required, forbidden,
            )
            solution.evidence_conflict = True
    else:
        solution.evidence_cost = (
            _unordered_evidence_cost(pair_costs, remove_costs, insert_costs, required, forbidden)
            if required else optimal
        )
        solution.evidence_conflict = solution.evidence_cost != optimal
    return solution


def _solve(left: Sequence[Occurrence], right: Sequence[Occurrence], ordered: bool, penalty: int,
           required: set[tuple[int, int]] | None = None,
           forbidden: set[tuple[int, int]] | None = None) -> StageSolution:
    """Solves one section of one stage."""
    if ordered:
        return _ordered_solution(left, right, penalty, required, forbidden)
    return _unordered_solution(left, right, penalty, required, forbidden)


class _AlignmentEdit(SequenceEdit):
    """A concrete, fully priced alignment used only to render one explanation."""

    __slots__ = ("_cost", "_sub_edits")

    def __init__(self, from_node: TreeNode, sub_edits: Sequence[Edit], cost: int):
        self._sub_edits = list(sub_edits)
        self._cost = int(cost)
        super().__init__(from_node=from_node, to_node=None)

    def edits(self):
        return iter(self._sub_edits)

    def bounds(self) -> Range:
        return Range(self._cost, self._cost)

    def tighten_bounds(self) -> bool:
        return False

    def is_complete(self) -> bool:
        return True


def _container_for(occurrences: Sequence[Occurrence], ordered: bool) -> TreeNode:
    """Builds a fresh container over copies of a section's nodes."""
    children = [occurrence.node.copy() for occurrence in occurrences]
    return ListNode(children) if ordered else UnorderedListNode(children)


def _ordered_ops(solution: StageSolution, alignment: Alignment) -> list[_Op]:
    """Returns an ordered alignment's ops in order."""
    return list(alignment.ops)


def _unordered_ops(solution: StageSolution, alignment: Alignment) -> list[_Op]:
    """Returns an unordered alignment's ops in a stable order."""
    paired = [op for op in alignment.ops if op.kind == "pair"]
    removed = [op for op in alignment.ops if op.kind == "remove"]
    inserted = [op for op in alignment.ops if op.kind == "insert"]
    return paired + removed + inserted


def _ops_for(alignment: Alignment, solution: StageSolution) -> list[_Op]:
    """Returns a concrete alignment's ops in a stable rendering order."""
    if solution.ordered:
        return _ordered_ops(solution, alignment)
    return _unordered_ops(solution, alignment)


def _render_alignment(left: Sequence[Occurrence], right: Sequence[Occurrence], ops: Sequence[_Op],
                      ordered: bool, solution: StageSolution) -> str:
    """Renders one concrete alignment through Graphtage's own JSON formatter.

    The rendered diff's :meth:`graphtage.EditedTreeNode.edited_cost` is checked against the summed price of the ops,
    so no diff is presented at a price its own edits do not add up to.

    """
    left_container = _container_for(left, ordered).make_edited()
    right_container = _container_for(right, ordered)
    left_children = list(left_container.children())
    right_children = list(right_container.children())
    sub_edits: list[Edit] = []
    for op in ops:
        if op.kind == "pair":
            sub_edits.append(left_children[op.left].edits(right_children[op.right]))
        elif op.kind == "remove":
            sub_edits.append(Remove(left_children[op.left], left_container))
        else:
            sub_edits.append(Insert(right_children[op.right], left_container))
    cost = solution.alignment_cost(ops)
    # Verify against the concrete edits' own Graphtage prices before wrapping them in a constant-cost carrier.
    actual = sum(_realized_cost(sub_edit) for sub_edit in sub_edits)
    if actual != cost:
        raise LineageError(
            f"The concrete edits sum to {actual}, but the search reported {cost}; "
            "refusing to present a diff whose accounting disagrees with itself"
        )
    edit = _AlignmentEdit(left_container, sub_edits, actual)
    edit.on_diff(left_container)
    output = io.StringIO()
    printer = Printer(ansi_color=False, out_stream=output)
    graphtage_json.JSONFormatter.DEFAULT_INSTANCE.print(printer, left_container)
    return output.getvalue()




def _positions(occurrences: Sequence[Occurrence]) -> dict[OccurrenceRef, int]:
    return {occurrence.ref: index for index, occurrence in enumerate(occurrences)}


def _stage_evidence(evidence: Sequence[IdentityEvidence], version_a: int, version_b: int,
                    left: Sequence[Occurrence], right: Sequence[Occurrence],
                    entities=None
                    ) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Projects the review onto one adjacent stage as required and forbidden index pairs.

    Same-entity evidence is expanded through its transitive closure before projection, so a chain such as
    ``v1:r3 == v1:r2`` plus ``v1:r2 == v2:m1`` requires both ``r3->m1`` and ``r2->m1``. Different-entity
    evidence likewise forbids every pair drawn from the two entity classes, not merely the two references named
    in the table.

    """
    left_index, right_index = _positions(left), _positions(right)
    required: set[tuple[int, int]] = set()
    forbidden: set[tuple[int, int]] = set()
    if entities is None:
        entities = _EntityUnion(evidence)

    left_classes: dict[OccurrenceRef, set[OccurrenceRef]] = defaultdict(set)
    right_classes: dict[OccurrenceRef, set[OccurrenceRef]] = defaultdict(set)
    for ref in left_index:
        left_classes[entities.find(ref)].add(ref)
    for ref in right_index:
        right_classes[entities.find(ref)].add(ref)

    for item in evidence:
        if item.same:
            if item.left.version == version_a and item.right.version == version_b:
                same_left, same_right = item.left, item.right
                for left_member in left_classes.get(entities.find(same_left), {same_left}):
                    for right_member in right_classes.get(entities.find(same_right), {same_right}):
                        if left_member in left_index and right_member in right_index:
                            required.add((left_index[left_member], right_index[right_member]))
            elif item.left.version == version_b and item.right.version == version_a:
                same_left, same_right = item.right, item.left
                for left_member in left_classes.get(entities.find(same_left), {same_left}):
                    for right_member in right_classes.get(entities.find(same_right), {same_right}):
                        if left_member in left_index and right_member in right_index:
                            required.add((left_index[left_member], right_index[right_member]))
        else:
            if {item.left.version, item.right.version} != {version_a, version_b}:
                continue
            left, right = (item.left, item.right) if item.left.version == version_a else \
                (item.right, item.left)
            for left_member in left_classes.get(entities.find(left), {left}):
                for right_member in right_classes.get(entities.find(right), {right}):
                    if left_member in left_index and right_member in right_index:
                        forbidden.add((left_index[left_member], right_index[right_member]))
    return required, forbidden


class _EntityUnion:
    """Closure of same-entity evidence, with explicit different-entity constraints."""

    def __init__(self, evidence: Sequence[IdentityEvidence]):
        self.parent: dict[OccurrenceRef, OccurrenceRef] = {}
        self.rank: dict[OccurrenceRef, int] = {}
        self.distinct: set[frozenset] = set()
        for item in evidence:
            self.add(item.left)
            self.add(item.right)
            if item.same:
                self.union(item.left, item.right)
            else:
                self.distinct.add(frozenset((item.left, item.right)))
        for pair in self.distinct:
            a, b = tuple(pair)
            if self.same(a, b):
                raise LineageError(
                    f"Identity evidence is contradictory: {a} and {b} are both confirmed the same and declared "
                    "different"
                )

    def add(self, ref: OccurrenceRef):
        if ref not in self.parent:
            self.parent[ref] = ref
            self.rank[ref] = 0

    def find(self, ref: OccurrenceRef) -> OccurrenceRef:
        self.add(ref)
        root = ref
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[ref] != root:
            self.parent[ref], ref = root, self.parent[ref]
        return root

    def union(self, left: OccurrenceRef, right: OccurrenceRef):
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1

    def same(self, left: OccurrenceRef, right: OccurrenceRef) -> bool:
        return self.find(left) == self.find(right)

    def may_coincide(self, left: OccurrenceRef, right: OccurrenceRef) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return True
        for pair in self.distinct:
            a, b = tuple(pair)
            if (self.find(a) == left_root and self.find(b) == right_root) or \
                    (self.find(a) == right_root and self.find(b) == left_root):
                return False
        return True


def _alignment_entity_ok(alignment: Alignment,
                         left_refs: Sequence[OccurrenceRef],
                         right_refs: Sequence[OccurrenceRef],
                         evidence: Sequence[IdentityEvidence],
                         entities: _EntityUnion) -> bool:
    """Returns whether an alignment's partner choices honour same- and different-entity evidence."""
    left_partner: dict[OccurrenceRef, OccurrenceRef] = {}
    right_partner: dict[OccurrenceRef, OccurrenceRef] = {}
    for left_index, right_index in alignment.pairs():
        left_ref, right_ref = left_refs[left_index], right_refs[right_index]
        left_partner[left_ref] = right_ref
        right_partner[right_ref] = left_ref

    for item in evidence:
        left_ref, right_ref = item.left, item.right
        left_is_left = left_ref in left_partner
        left_is_right = left_ref in right_partner
        right_is_left = right_ref in left_partner
        right_is_right = right_ref in right_partner
        if not (left_is_left or left_is_right) or not (right_is_left or right_is_right):
            continue
        if item.same:
            if left_is_left and right_is_left:
                if not entities.same(left_partner[left_ref], left_partner[right_ref]):
                    return False
            elif left_is_right and right_is_right:
                if not entities.same(right_partner[left_ref], right_partner[right_ref]):
                    return False
            elif left_is_left and right_is_right and left_partner.get(left_ref) != right_ref:
                return False
            elif left_is_right and right_is_left and right_partner.get(left_ref) != right_ref:
                return False
        else:
            if left_is_left and right_is_left:
                if entities.same(left_partner[left_ref], left_partner[right_ref]):
                    return False
            elif left_is_right and right_is_right:
                if entities.same(right_partner[left_ref], right_partner[right_ref]):
                    return False
            elif left_is_left and right_is_right and left_partner.get(left_ref) == right_ref:
                return False
            elif left_is_right and right_is_left and right_partner.get(left_ref) == right_ref:
                return False
    return True


def _consistent_alignments(solution: StageSolution,
                           left: Sequence[Occurrence],
                           right: Sequence[Occurrence],
                           required: set[tuple[int, int]],
                           forbidden: set[tuple[int, int]],
                           evidence: Sequence[IdentityEvidence],
                           entities=None) -> list[Alignment]:
    """Returns every stage-minimum alignment that honours the reviewed evidence."""
    if solution.alignments is None:
        raise LineageLimitError(
            "This section is too large to enumerate every minimum-price alignment, so the audit cannot decide "
            "whether the reviewed evidence is satisfiable at the original price"
        )
    left_refs = [occurrence.ref for occurrence in left]
    right_refs = [occurrence.ref for occurrence in right]
    relevant = [
        item for item in evidence
        if (item.left in left_refs and item.right in right_refs)
        or (item.right in left_refs and item.left in right_refs)
        or (item.left in left_refs and item.right in left_refs)
        or (item.left in right_refs and item.right in right_refs)
    ]
    consistent = []
    for alignment in solution.alignments:
        pairs = set(alignment.pairs())
        if not required.issubset(pairs):
            continue
        if pairs & forbidden:
            continue
        if not _alignment_entity_ok(alignment, left_refs, right_refs, relevant, entities):
            continue
        consistent.append(alignment)
    return consistent


@dataclass
class _Section:
    """Both adjacent stages of one section, with their evidence-filtered optimal alignments."""

    spec: SectionSpec
    first: StageSolution
    second: StageSolution
    first_occurrences: Sequence[Occurrence]
    middle_occurrences: Sequence[Occurrence]
    third_occurrences: Sequence[Occurrence]
    first_consistent: list[Alignment]
    second_consistent: list[Alignment]
    penalty: int


def _solve_section(spec: SectionSpec, collected: Sequence[dict[SectionSpec, list[Occurrence]]],
                   evidence: Sequence[IdentityEvidence], entities: _EntityUnion) -> _Section:
    """Solves both adjacent stages of one section and filters them by the review."""
    first_occurrences = collected[0][spec]
    middle_occurrences = collected[1][spec]
    third_occurrences = collected[2][spec]
    first_penalty = _penalty(list(first_occurrences) + list(middle_occurrences), spec.ordered)
    second_penalty = _penalty(list(middle_occurrences) + list(third_occurrences), spec.ordered)

    first_required, first_forbidden = _stage_evidence(
        evidence, 0, 1, first_occurrences, middle_occurrences, entities,
    )
    second_required, second_forbidden = _stage_evidence(
        evidence, 1, 2, middle_occurrences, third_occurrences, entities,
    )
    first_solution = _solve(first_occurrences, middle_occurrences, spec.ordered, first_penalty,
                            first_required, first_forbidden)
    second_solution = _solve(middle_occurrences, third_occurrences, spec.ordered, second_penalty,
                             second_required, second_forbidden)

    return _Section(
        spec=spec,
        first=first_solution,
        second=second_solution,
        first_occurrences=first_occurrences,
        middle_occurrences=middle_occurrences,
        third_occurrences=third_occurrences,
        first_consistent=_consistent_alignments(
            first_solution, first_occurrences, middle_occurrences,
            first_required, first_forbidden, evidence, entities,
        ),
        second_consistent=_consistent_alignments(
            second_solution, middle_occurrences, third_occurrences,
            second_required, second_forbidden, evidence, entities,
        ),
        penalty=first_penalty,
    )


def _candidate_paths(section: _Section, target: Occurrence):
    """Yields every distinct (origin, first registration, first alignment, second alignment, middle index).

    This is the complete enumeration of evidence-consistent two-stage paths for the target, before any rendering
    limit is applied. Two paths that produce the same attribution are collapsed only at the very end.

    """
    seen_second = set()
    for second_alignment in section.second_consistent:
        inserted = target.position in {op.right for op in second_alignment.ops if op.kind == "insert"}
        if inserted:
            middle_indices = [None]
        else:
            middle_indices = [middle for middle, third in second_alignment.pairs() if third == target.position]
        for middle_index in middle_indices:
            signature = (middle_index, tuple(second_alignment.ops))
            if signature in seen_second:
                continue
            seen_second.add(signature)
            if middle_index is None:
                for first_alignment in section.first_consistent:
                    yield "third", None, first_alignment, second_alignment, None
                continue
            any_origin = False
            for first_alignment in section.first_consistent:
                if middle_index in {op.right for op in first_alignment.ops if op.kind == "insert"}:
                    yield "midway", None, first_alignment, second_alignment, middle_index
                    any_origin = True
                for first_index, paired_middle in first_alignment.pairs():
                    if paired_middle == middle_index:
                        any_origin = True
                        yield (
                            "first", section.first_occurrences[first_index].registration,
                            first_alignment, second_alignment, middle_index,
                        )
            if not any_origin and section.first_consistent:
                yield "midway", None, section.first_consistent[0], second_alignment, middle_index


def _explain(section: _Section, target: Occurrence, evidence: Sequence[IdentityEvidence],
             limit: int) -> tuple[list[TraceExplanation], set[tuple[str, Any]]]:
    """Renders up to *limit* concrete explanations and returns every possible attribution.

    Returns:
        tuple: The rendered explanations, and the complete set of distinct origins. The second value is what the
        audit's confirmed/ambiguous verdict uses; the first is only the evidence presented to the reader.

    """
    origins: set[tuple[str, Any]] = set()
    rendered: list[TraceExplanation] = []
    rendered_keys: set[tuple] = set()
    candidates = list(_candidate_paths(section, target))
    candidates.sort(key=lambda candidate: (candidate[0], repr(candidate[1]), candidate[4] if candidate[4] is not None else -1))
    for origin, registration, first_alignment, second_alignment, middle_index in candidates:
        origins.add((origin, registration))
        key = (origin, registration, middle_index)
        if key in rendered_keys or len(rendered) >= limit:
            continue
        rendered_keys.add(key)
        first_ops = _ops_for(first_alignment, section.first)
        second_ops = _ops_for(second_alignment, section.second)
        diffs = (
            _render_alignment(section.first_occurrences, section.middle_occurrences, first_ops,
                              section.spec.ordered, section.first),
            _render_alignment(section.middle_occurrences, section.third_occurrences, second_ops,
                              section.spec.ordered, section.second),
        )
        if middle_index is None:
            path = (target.ref,)
        elif origin == "midway" and registration is None:
            path = (section.middle_occurrences[middle_index].ref, target.ref)
        else:
            first_ref = next(
                occurrence.ref for index, occurrence in enumerate(section.first_occurrences)
                if occurrence.registration == registration
                and any(op.kind == "pair" and op.left == index and op.right == middle_index
                        for op in first_alignment.ops)
            ) if registration is not None else None
            path = (first_ref, section.middle_occurrences[middle_index].ref, target.ref) if first_ref is not None \
                else (section.middle_occurrences[middle_index].ref, target.ref)
        rendered.append(TraceExplanation(
            origin=origin,
            registration=target.registration,
            first_version_registration=registration if origin == "first" else None,
            path=path,
            diffs=diffs,
            stage_costs=(section.first.cost, section.second.cost),
        ))
    return rendered, origins


def _direct_first_registration(evidence: Sequence[IdentityEvidence], target: Occurrence) -> Any | None:
    """Returns a first-version registration the reviewer directly tied to the target."""
    for item in evidence:
        if not item.same:
            continue
        if item.left == target.ref and item.right.version == 0:
            return item.right.registration
        if item.right == target.ref and item.left.version == 0:
            return item.left.registration
    return None


def _find_target(collected: Sequence[dict[SectionSpec, list[Occurrence]]], target: Any,
                 section: tuple[str, ...] | str | SectionSpec | None) -> tuple[SectionSpec, Occurrence]:
    """Resolves the third-version occurrence the audit is about."""
    if isinstance(target, Occurrence):
        if target.version != 2:
            raise LineageError(f"The target must be in the third version, not {target!r}")
        for spec, occurrences in collected[2].items():
            if target in occurrences:
                return spec, target
        raise LineageError(f"The target {target!r} is not registered in this audit")
    version = 2
    registration = target
    if isinstance(target, OccurrenceRef):
        version, registration = target.version, target.registration
        if version != 2:
            raise LineageError(f"The target must be in the third version (index 2), not version {version}")
    if isinstance(section, SectionSpec):
        for spec, occurrences in collected[2].items():
            if spec == section:
                for occurrence in occurrences:
                    if occurrence.registration == registration:
                        return spec, occurrence
        raise LineageError(f"No third-version occurrence with registration {registration!r} in {section!r}")
    if isinstance(section, str):
        section = (section,)
    candidates = []
    for spec, occurrences in collected[2].items():
        if section is not None and spec.path != tuple(section):
            continue
        for occurrence in occurrences:
            if occurrence.registration == registration:
                candidates.append((spec, occurrence))
    if not candidates:
        scope = f" in section {section!r}" if section is not None else ""
        raise LineageError(f"No third-version occurrence with registration {registration!r}{scope}")
    if len(candidates) > 1:
        raise LineageError(
            f"Registration {registration!r} occurs in more than one section; pass section= to disambiguate"
        )
    return candidates[0]


def _first_class_registrations(evidence: Sequence[IdentityEvidence], section: _Section,
                               registrations: Iterable[Any]) -> set[Any]:
    """Expands first-version registrations through confirmed same-entity links in the same section."""
    first_by_ref = {occurrence.ref: occurrence for occurrence in section.first_occurrences}
    classes: dict[OccurrenceRef, set[OccurrenceRef]] = defaultdict(set)
    union = _EntityUnion(evidence)
    for ref in first_by_ref:
        classes[union.find(ref)].add(ref)
    expanded: set[Any] = set(registrations)
    for registration in registrations:
        ref = OccurrenceRef(0, registration)
        for member in classes.get(union.find(ref), set()):
            occurrence = first_by_ref.get(member)
            if occurrence is not None:
                expanded.add(occurrence.registration)
    return expanded


def _direct_realizable(evidence: Sequence[IdentityEvidence], target: Occurrence, section: _Section) -> bool:
    """Returns whether a direct first-to-third same assertion has a two-stage witness at the current minima."""
    directs = _direct_first_registrations(evidence, target, section)
    if not directs:
        return True
    paired = set()
    for second_alignment in section.second_consistent:
        for middle, third in second_alignment.pairs():
            if third != target.position:
                continue
            for first_alignment in section.first_consistent:
                for first_index, paired_middle in first_alignment.pairs():
                    if paired_middle == middle:
                        paired.add(section.first_occurrences[first_index].registration)
    return bool(directs & paired)


def _audit_section(section: _Section, target: Occurrence, evidence: Sequence[IdentityEvidence],
                   limit: int) -> AuditResult:
    """Builds the audit result for one section."""
    stage_costs = (section.first.cost, section.second.cost)
    conflict = None
    if section.first.evidence_conflict:
        conflict = AuditConflict(
            kind="evidence_requires_higher_cost",
            message=(
                "No first-to-second minimum-price alignment satisfies the reviewed identity table; honouring it "
                "requires paying more than that stage's original price"
            ),
            stage=0,
            original_cost=section.first.cost,
            required_cost=section.first.evidence_cost,
            detail=f"Section {section.spec.path!r}",
        )
    elif section.second.evidence_conflict:
        conflict = AuditConflict(
            kind="evidence_requires_higher_cost",
            message=(
                "No second-to-third minimum-price alignment satisfies the reviewed identity table; honouring it "
                "requires paying more than that stage's original price"
            ),
            stage=1,
            original_cost=section.second.cost,
            required_cost=section.second.evidence_cost,
            detail=f"Section {section.spec.path!r}",
        )
    elif not section.first_consistent or not section.second_consistent:
        conflict = AuditConflict(
            kind="evidence_inconsistent_with_injective_alignment",
            message=(
                "The reviewed identity table cannot be satisfied by any minimum-price alignment of one or both "
                "stages; no more expensive alignment is substituted for the original price"
            ),
            stage=0 if not section.first_consistent else 1,
            original_cost=section.first.cost if not section.first_consistent else section.second.cost,
            required_cost=None,
            detail=f"Section {section.spec.path!r}",
        )
    elif not _direct_realizable(evidence, target, section):
        conflict = AuditConflict(
            kind="evidence_requires_different_path",
            message=(
                "The reviewed first-to-third identity cannot be carried by any pair of minimum-price adjacent "
                "alignments; honouring it would require giving up a stage's original minimum"
            ),
            stage=None,
            original_cost=section.first.cost + section.second.cost,
            required_cost=None,
            detail=f"Direct evidence for {target.ref}",
        )

    if conflict is not None:
        return AuditResult(
            target=target.ref,
            section=section.spec.path,
            confirmed=False,
            first_version_registrations=(),
            introduced_midway=False,
            introduced_at_third=False,
            origins=(),
            explanations=(),
            stage_costs=stage_costs,
            exhaustive=section.first.exhaustive and section.second.exhaustive,
            conflict=conflict,
        )

    explanations, origins = _explain(section, target, evidence, limit)

    excluded = _cross_version_exclusions(evidence, target, section)
    suppressed: set[Any] = set()
    if excluded:
        # A cross-version 'different' assertion removes an attribution the adjacent minimum-price alignments
        # support. Attributions the review already rules out upstream are not 'suppressed' again here; they were
        # never live candidates.
        suppressed = {registration for registration in excluded if registration in {
            origin[1] for origin in origins if origin[0] == "first"
        }}
        if suppressed:
            origins = {origin for origin in origins
                       if not (origin[0] == "first" and origin[1] in suppressed)}
            explanations = [
                explanation for explanation in explanations
                if not (explanation.origin == "first" and explanation.first_version_registration in suppressed)
            ]

    direct_all = _direct_first_registrations(evidence, target, section)
    if direct_all:
        direct_set = _first_class_registrations(evidence, section, direct_all)
        origins = {("first", registration) for registration in direct_set}
        matching = [e for e in explanations
                    if e.origin == "first" and e.first_version_registration in direct_set]
        if not matching:
            matching = [
                explanation for explanation in _explain(section, target, evidence, limit=10**6)[0]
                if explanation.origin == "first" and explanation.first_version_registration in direct_set
            ][:limit]
        explanations = matching or explanations

    entity_classes = _EntityUnion(evidence)
    class_members = _first_class_members(entity_classes, section)
    canonical: dict[Any, Any] = {}
    for members in class_members.values():
        representative = sorted(members, key=repr)[0]
        for member in members:
            canonical[member] = representative

    origins = {
        (origin, canonical.get(registration, registration) if origin == "first" else registration)
        for origin, registration in origins
    }

    first_registrations: set[Any] = set()
    for origin, registration in origins:
        if origin == "first" and registration is not None:
            first_registrations.update(class_members.get(registration, {registration}))
    if direct_all:
        first_registrations = _first_class_registrations(evidence, section, direct_all)
        origins = {("first", canonical.get(registration, registration)) for registration in direct_all}

    if not origins and suppressed and not direct_all:
        # Every attribution the adjacent minima supported is excluded, but the evidence table may still name a
        # first-version occurrence that the minima could have used. Report that candidate as excluded, not as a
        # confirmed origin.
        survivors = {("first", registration) for registration in
                     _first_class_registrations(evidence, section, excluded) - suppressed}
        if survivors:
            origins = survivors
        return AuditResult(
            target=target.ref,
            section=section.spec.path,
            confirmed=False,
            first_version_registrations=(),
            introduced_midway=False,
            introduced_at_third=False,
            origins=(),
            explanations=(),
            stage_costs=stage_costs,
            exhaustive=section.first.exhaustive and section.second.exhaustive,
            conflict=AuditConflict(
                kind="no_explanation",
                message="No evidence-consistent minimum-price explanation reaches the target",
                detail=f"Section {section.spec.path!r}",
            ),
        )

    introduced_midway = ("midway", None) in origins
    introduced_at_third = ("third", None) in origins
    distinct_origins = tuple(sorted(origins, key=repr))
    confirmed = (
        len(distinct_origins) == 1
        and distinct_origins[0][0] == "first"
        and bool(first_registrations)
        and not introduced_midway
        and not introduced_at_third
    )
    return AuditResult(
        target=target.ref,
        section=section.spec.path,
        confirmed=confirmed,
        first_version_registrations=tuple(sorted(first_registrations, key=repr)),
        introduced_midway=introduced_midway,
        introduced_at_third=introduced_at_third,
        origins=distinct_origins,
        explanations=tuple(explanations),
        stage_costs=stage_costs,
        exhaustive=section.first.exhaustive and section.second.exhaustive,
        conflict=None,
    )


def _direct_first_registrations(evidence: Sequence[IdentityEvidence], target: Occurrence,
                                section: _Section) -> set[Any]:
    """Returns every first-version registration the review directly tied to the target in this section."""
    registrations = {occurrence.registration for occurrence in section.first_occurrences}
    result: set[Any] = set()
    for item in evidence:
        if not item.same:
            continue
        if item.left == target.ref and item.right.version == 0 and item.right.registration in registrations:
            result.add(item.right.registration)
        if item.right == target.ref and item.left.version == 0 and item.left.registration in registrations:
            result.add(item.left.registration)
    return result


def audit(versions: Sequence[Any], sections: Any = None, target: Any = None, *,
          identities: Any = None, evidence: Any = None,
          section: tuple[str, ...] | str | SectionSpec | None = None,
          explanation_limit: int = _DEFAULT_EXPLANATION_LIMIT) -> AuditResult:
    """Traces one third-version registration back through two adjacent archive versions.

    Args:
        versions: Three JSON-compatible versions (mappings, lists, or paths), oldest first.
        sections: The :class:`SectionSpec` values (or section paths) that carry registered occurrences. If omitted,
            every list-valued path found in any version is used, in ordered mode.
        target: The third-version registration number whose ancestry is wanted, or an :class:`Occurrence` from the
            third version.
        identities: The reviewed identity table. Accepted forms are a sequence of :class:`IdentityEvidence`, a
            sequence of ``(left, right, same)`` triples, or a mapping with ``same`` and ``different`` lists.
        evidence: An alias for :class:`identities`.
        section: Restrict the lookup to one section path, or to one exact :class:`SectionSpec` when the same path is
            audited in both ordered and unordered mode. Required only when the registration number is not unique
            among the selected sections.
        explanation_limit: The number of concrete explanations to render when the evidence admits more than one.

    Returns:
        AuditResult: The confirmed origin, the alternative explanations, or the evidence cost conflict.

    Raises:
        LineageError: If the versions, sections, or evidence cannot describe an archive, or if the evidence is
            self-contradictory.
        LineageLimitError: If a section is too large to enumerate every minimum-price alignment.

    """
    if len(versions) != 3:
        raise LineageError(f"An audit needs exactly three versions, not {len(versions)}")
    versions = [_load_version(version) for version in versions]
    specs = _normalize_sections(sections) or _auto_sections(versions)
    if not specs:
        raise LineageError("No sections to audit; pass sections= or provide at least one list-valued path")
    evidence_items = _normalize_evidence(evidence if evidence is not None else identities)
    entities = _EntityUnion(evidence_items)
    collected = _collect(versions, specs)
    registered = {occurrence.ref for version in collected for occurrences in version.values()
                  for occurrence in occurrences}
    for item in evidence_items:
        for ref in (item.left, item.right):
            if ref not in registered:
                raise LineageError(
                    f"Identity evidence names {ref}, which is not a registered occurrence in any selected section"
                )
    spec, target_occurrence = _find_target(collected, target, section)
    section_state = _solve_section(spec, collected, evidence_items, entities)

    return _audit_section(section_state, target_occurrence, evidence_items, explanation_limit)


def _cross_version_exclusions(evidence: Sequence[IdentityEvidence], target: Occurrence,
                              section: _Section) -> set[Any]:
    """Returns first-version registrations the review says are not the target's entity."""
    excluded: set[Any] = set()
    for item in evidence:
        if item.same:
            continue
        pair = {item.left, item.right}
        if target.ref not in pair:
            continue
        other = next(iter(pair - {target.ref}))
        if other.version == 0 and other.registration in {
            occurrence.registration for occurrence in section.first_occurrences
        }:
            excluded.add(other.registration)
    return excluded


def _first_class_members(entities: _EntityUnion, section: _Section) -> dict[Any, set[Any]]:
    """Groups first-version registrations by confirmed same-entity class."""
    by_registration = {occurrence.registration: occurrence.ref for occurrence in section.first_occurrences}
    classes: dict[OccurrenceRef, set[Any]] = defaultdict(set)
    for registration, ref in by_registration.items():
        classes[entities.find(ref)].add(registration)
    return {registration: members for members in classes.values() for registration in members}
