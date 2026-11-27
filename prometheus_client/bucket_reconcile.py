"""Reconciliation of histogram bucket counts across a bucket-layout rollout.

During a rolling deployment an old and a new workshop can expose the same
latency histogram with different bucket boundaries.  In the motivating case the
old layout only carries the ``500us`` and ``1000us`` publication lines while the
new layout adds a ``750us`` line in between.  One scrape window can therefore be
described by several ledgers (e.g. an "old" and a "new" scrape) that partially
measure the same physical observations.

The module never assumes that equal labels or equal values mean the same
samples.  Two ledger statements only describe the same observations when they
reference the same registered batch (or the same per-process block); such
overlaps are joined onto shared strip variables instead of being deduplicated by
label or value.

Within a process block the observations are partitioned into coverage classes:
the items several ledgers count in common are decision variables (one integer
count per half-open strip), while the items only one ledger ever sees are
*residuals*.  Residuals are never search variables: a ledger band's residual is
its fixed raw increment minus the shared items placed in that band, so it is a
linear expression bounded below by zero.  This keeps the number of decision
variables equal to the (small) number of strips, regardless of how large the
counts get.

Three figures carried by a bucket file are kept distinct on purpose:

* the raw per-bucket increments,
* the cumulative ``le`` displays a receiver (upper bound inclusive) sees,
* the known integer sum of the observed latencies.

The solver works on half-open strips ``(lo, hi]`` (the first strip has no lower
bound) so the microsecond value domain is never enumerated, and it works on
strip counts rather than on one variable per observed item, so long ledgers do
not require expanding every item or every possible microsecond.
"""

from dataclasses import dataclass, field, asdict
import itertools
import json
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .mmap_dict import MmapedDict

INF = math.inf
Number = Union[int, float]


class ReconcileError(ValueError):
    """Raised when ledger statements contradict each other or fail validation."""


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundLine:
    """One publication line (bucket upper bound) of one or more layouts."""

    bound: float
    layouts: Tuple[str, ...]
    introduced: str = ""

    def is_inf(self) -> bool:
        return self.bound == INF


@dataclass(frozen=True)
class Manifest:
    """The big ledger: value domain, batch id ranges and fixed boundary lines."""

    metric_name: str
    lines: Tuple[BoundLine, ...]
    unit: str = ""
    value_domain: Optional[Tuple[Optional[int], Optional[int]]] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Manifest":
        lines = []
        for item in data["lines"]:
            if "layouts" in item:
                layouts = tuple(str(layout) for layout in item["layouts"])
            else:
                layouts = (str(item["layout"]),)
            lines.append(BoundLine(
                bound=_parse_bound(item["bound"]),
                layouts=layouts,
                introduced=item.get("introduced", ""),
            ))
        domain = data.get("value_domain")
        value_domain = None
        if domain is not None:
            value_domain = (
                None if domain[0] is None else int(domain[0]),
                None if domain[1] is None else int(domain[1]),
            )
        return Manifest(
            metric_name=data["metric_name"],
            lines=tuple(lines),
            unit=data.get("unit", ""),
            value_domain=value_domain,
        )

    def layouts(self) -> Tuple[str, ...]:
        seen: Dict[str, None] = {}
        for line in self.lines:
            for layout in line.layouts:
                seen.setdefault(layout, None)
        return tuple(seen)

    def bounds(self, layout: str) -> Tuple[float, ...]:
        bounds = sorted(line.bound for line in self.lines if layout in line.layouts)
        if not bounds or bounds[-1] != INF:
            bounds.append(INF)
        return tuple(bounds)

    def line_for(self, bound: float) -> Optional[BoundLine]:
        for line in self.lines:
            if line.bound == bound:
                return line
        return None


@dataclass(frozen=True)
class Snapshot:
    """Pointer to the raw scrape snapshot backing a ledger statement."""

    snapshot_id: str
    path: str = ""
    kind: str = ""


@dataclass(frozen=True)
class BatchCoverage:
    """Statement that a ledger's cumulative ``le <= bound`` display holds a batch."""

    batch_id: str
    bound: float


@dataclass
class LedgerBlock:
    """One per-process chunk of a ledger.

    A process id reused after a restart must carry a distinct ``generation`` so
    the post-restart subtotal is never subtracted from the old counter: each
    (process_id, generation) pair is its own variable universe.
    """

    process_id: int
    increments: Dict[float, int]
    generation: int = 0
    labels: Dict[str, str] = field(default_factory=dict)
    cumulative: Optional[Dict[float, int]] = None
    count: Optional[int] = None
    latency_sum: Optional[int] = None
    batches: List[BatchCoverage] = field(default_factory=list)

    @property
    def key(self) -> "BlockKey":
        return BlockKey(
            self.process_id,
            self.generation,
            tuple(sorted(self.labels.items())),
        )


@dataclass
class Ledger:
    """One scrape, expressed in one layout."""

    ledger_id: str
    layout: str
    blocks: List[LedgerBlock]
    snapshot: Optional[Snapshot] = None
    source_files: List[str] = field(default_factory=list)


@dataclass
class Batch:
    """Small-ledger detail: the few integer latency values a batch may take."""

    batch_id: str
    allowed_values: Tuple[int, ...]
    size: Optional[int] = None


@dataclass(frozen=True)
class BlockKey:
    process_id: int
    generation: int
    labels: Tuple[Tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass
class TargetResult:
    """Window count for one target line."""

    bound: float
    minimum: int
    maximum: int
    exact: bool
    value: Optional[int]
    evidence: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PairResult:
    """Feasibility of a requested (count at bound_a, count at bound_b) pair."""

    bounds: Tuple[float, float]
    requested: Tuple[int, int]
    feasible: bool
    witness: Optional[Dict[str, Any]] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    achievable: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None


@dataclass
class ReconciliationReport:
    metric_name: str
    labels_group: Dict[str, str]
    total_min: int
    total_max: int
    total_exact: bool
    targets: List[TargetResult]
    pair_results: List[PairResult]
    overlaps: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        """The window total when it is pinned, otherwise the lower bound."""
        return self.total_min

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small constraint solver over strip counts (integers, no value enumeration)
# ---------------------------------------------------------------------------


class _Linear:
    __slots__ = ("coeffs", "op", "rhs", "note")

    def __init__(self, coeffs: Mapping[str, int], op: str, rhs: int,
                 note: Optional[Mapping[str, Any]] = None):
        self.coeffs = dict(coeffs)
        self.op = op
        self.rhs = rhs
        self.note = dict(note or {})

    def _bounds(self, domains: Mapping[str, Tuple[int, int]]) -> Tuple[int, int]:
        low = high = 0
        for name, coeff in self.coeffs.items():
            lo, hi = domains[name]
            if coeff >= 0:
                low += coeff * lo
                high += coeff * hi
            else:
                low += coeff * hi
                high += coeff * lo
        return low, high

    def feasible(self, domains: Mapping[str, Tuple[int, int]]) -> bool:
        low, high = self._bounds(domains)
        if self.op == "=":
            return low <= self.rhs <= high
        if self.op == ">=":
            return high >= self.rhs
        return low <= self.rhs

    def satisfied(self, assignment: Mapping[str, int]) -> bool:
        value = sum(coeff * assignment[name] for name, coeff in self.coeffs.items())
        if self.op == "=":
            return value == self.rhs
        if self.op == ">=":
            return value >= self.rhs
        return value <= self.rhs


class _Solver:
    def __init__(self, domains: Mapping[str, Tuple[int, int]],
                 constraints: Sequence[_Linear]):
        self._domains = dict(domains)
        self._constraints = list(constraints)
        self._linear = list(self._constraints)

    def _propagate(self, domains: Dict[str, Tuple[int, int]],
                   extra_linear: Sequence[_Linear] = ()) -> bool:
        """Interval (bounds) propagation over the linear sum constraints."""
        linear = self._linear + list(extra_linear)
        changed = True
        iterations = 0
        max_iterations = len(domains) * max(1, len(linear)) + 1
        while changed:
            changed = False
            iterations += 1
            if iterations > max_iterations:
                break
            for constraint in linear:
                for target, coeff in constraint.coeffs.items():
                    if coeff == 0:
                        continue
                    low_other = high_other = 0
                    for name, other_coeff in constraint.coeffs.items():
                        if name == target:
                            continue
                        lo, hi = domains[name]
                        if other_coeff >= 0:
                            low_other += other_coeff * lo
                            high_other += other_coeff * hi
                        else:
                            low_other += other_coeff * hi
                            high_other += other_coeff * lo
                    lo, hi = domains[target]
                    if constraint.op in ("=", ">="):
                        bound = constraint.rhs - high_other
                        if coeff > 0:
                            lo = max(lo, math.ceil(bound / coeff))
                        else:
                            hi = min(hi, math.floor(bound / coeff))
                    if constraint.op in ("=", "<="):
                        bound = constraint.rhs - low_other
                        if coeff > 0:
                            hi = min(hi, math.floor(bound / coeff))
                        else:
                            lo = max(lo, math.ceil(bound / coeff))
                    if lo > hi:
                        return False
                    if (lo, hi) != domains[target]:
                        domains[target] = (lo, hi)
                        changed = True
        return all(constraint.feasible(domains) for constraint in self._constraints)

    def _search(self, domains: Dict[str, Tuple[int, int]],
                extra: Sequence[_Linear] = ()) -> Optional[Dict[str, int]]:
        if not self._propagate(domains, extra):
            return None
        if not all(c.feasible(domains) for c in extra):
            return None
        undecided = [name for name, (lo, hi) in domains.items() if lo != hi]
        if not undecided:
            assignment = {name: lo for name, (lo, _hi) in domains.items()}
            if all(c.satisfied(assignment) for c in self._constraints) and \
                    all(c.satisfied(assignment) for c in extra):
                return assignment
            return None
        name = min(undecided, key=lambda candidate: domains[candidate][1] - domains[candidate][0])
        lo, hi = domains[name]
        # Steer toward feasibility of the probe: a >= objective wants high
        # values, a <= objective wants low values.
        prefer_high = any(
            constraint.op == ">=" and name in constraint.coeffs
            for constraint in extra
        )
        ordered_values = (hi, lo) if prefer_high else (lo, hi)
        for value in ordered_values:
            child_domains = dict(domains)
            child_domains[name] = (value, value)
            found = self._search(child_domains, extra)
            if found is not None:
                return found
        mid = (lo + hi) // 2
        for child_lo, child_hi in ((lo + 1, mid), (mid + 1, hi - 1)):
            if child_lo > child_hi:
                continue
            child_domains = dict(domains)
            child_domains[name] = (child_lo, child_hi)
            found = self._search(child_domains, extra)
            if found is not None:
                return found
        return None

    def witness(self, extra: Sequence[_Linear] = ()) -> Optional[Dict[str, int]]:
        return self._search(dict(self._domains), extra)

    def optimize(self, expression: Mapping[str, int],
                 maximize: bool = False) -> int:
        def node_bound(domains: Mapping[str, Tuple[int, int]]) -> Tuple[int, int]:
            low = high = 0
            for name, coeff in expression.items():
                lo, hi = domains[name]
                if coeff >= 0:
                    low += coeff * lo
                    high += coeff * hi
                else:
                    low += coeff * hi
                    high += coeff * lo
            return low, high

        root_low, root_high = node_bound(self._domains)
        if root_low == root_high:
            if self.witness() is None:
                raise ReconcileError("constraints have no feasible allocation")
            return root_low

        # Monotone models (box domains plus capacity inequalities) attain their
        # extrema at domain endpoints: test those assignments directly before
        # paying for a binary search.
        propagated = dict(self._domains)
        if self._propagate(propagated):
            endpoint = {
                name: (dom[0] if not maximize else dom[1])
                for name, dom in propagated.items()
            }
            if all(c.satisfied(endpoint) for c in self._constraints):
                value = sum(coeff * endpoint[name] for name, coeff in expression.items())
                if (not maximize and value == root_low) or (maximize and value == node_bound(propagated)[1]):
                    return value

        if not maximize:
            low, high = root_low, root_high
            while low < high:
                middle = (low + high) // 2
                if self.witness([_Linear(expression, "<=", middle)]) is not None:
                    high = middle
                else:
                    low = middle + 1
            return low

        low, high = root_low, root_high
        while low < high:
            middle = (low + high + 1) // 2
            if self.witness([_Linear(expression, ">=", middle)]) is not None:
                low = middle
            else:
                high = middle - 1
        return low


# A linear expression with a constant term: value == const + sum(coeffs * var).
class _Expr:
    __slots__ = ("const", "coeffs")

    def __init__(self, const: int = 0, coeffs: Optional[Mapping[str, int]] = None):
        self.const = const
        self.coeffs: Dict[str, int] = dict(coeffs or {})

    def add(self, other: "_Expr", sign: int = 1) -> "_Expr":
        result = _Expr(self.const + sign * other.const, dict(self.coeffs))
        for name, coeff in other.coeffs.items():
            result.coeffs[name] = result.coeffs.get(name, 0) + sign * coeff
        return result

    def add_var(self, name: str, sign: int = 1) -> "_Expr":
        result = _Expr(self.const, dict(self.coeffs))
        result.coeffs[name] = result.coeffs.get(name, 0) + sign
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bound(value: Any) -> float:
    if isinstance(value, str) and value.strip() in ("+Inf", "Inf", "inf", "-Inf"):
        return INF
    number = float(value)
    if number != INF and not float(number).is_integer():
        raise ReconcileError(f"bucket bound must be an integer microsecond value, got {value!r}")
    return int(number) if number != INF else INF


def _bound_token(bound: float) -> str:
    return "+Inf" if bound == INF else str(int(bound))


def _normalize_counts(raw: Mapping[Any, Any]) -> Dict[float, int]:
    normalized: Dict[float, int] = {}
    for key, value in raw.items():
        bound = _parse_bound(key)
        count = int(value)
        if count < 0:
            raise ReconcileError(f"count for bound {_bound_token(bound)} is negative: {count}")
        normalized[bound] = count
    return normalized


@dataclass
class _Strip:
    lo: Optional[int]
    hi: float

    def contains_value(self, value: int) -> bool:
        if self.lo is not None and value <= self.lo:
            return False
        return value <= self.hi

    def value_domain(self,
                     global_domain: Tuple[Optional[int], Optional[int]]
                     ) -> Tuple[Optional[int], Optional[int]]:
        glob_min, glob_max = global_domain
        vmin = glob_min
        if self.lo is not None:
            floor = self.lo + 1
            vmin = floor if vmin is None else max(vmin, floor)
        if self.hi == INF:
            vmax = glob_max
        else:
            cap = int(self.hi)
            vmax = cap if glob_max is None else min(glob_max, cap)
        if vmin is not None and vmax is not None and vmin > vmax:
            raise ReconcileError(
                f"strip ({self.lo}, {_bound_token(self.hi)}] has no integer value in the "
                f"declared value domain {global_domain}"
            )
        return vmin, vmax

    def __str__(self) -> str:
        return f"({self.lo if self.lo is not None else '-inf'}, {_bound_token(self.hi)}]"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class Reconciler:
    """Builds the strip/coverage model and answers window / pair questions."""

    def __init__(self, manifest: Manifest, ledgers: Sequence[Ledger],
                 batches: Optional[Mapping[str, Batch]] = None):
        self.manifest = manifest
        self.ledgers = list(ledgers)
        self.batches = dict(batches or {})
        self._validate_ledgers()
        self.strips = self._build_strips()
        self.strip_count = len(self.strips)
        self.block_keys = self._collect_block_keys()
        self.domains: Dict[str, Tuple[int, int]] = {}
        self.constraints: List[_Linear] = []
        self.block_meta: Dict[BlockKey, Dict[str, Any]] = {}
        self.overlaps: List[Dict[str, Any]] = []
        self._batch_blocks: Dict[str, BlockKey] = {}
        self._build_model()

    # -- validation --------------------------------------------------------

    def _validate_ledgers(self) -> None:
        known_layouts = set(self.manifest.layouts())
        for ledger in self.ledgers:
            if ledger.layout not in known_layouts:
                raise ReconcileError(
                    f"ledger {ledger.ledger_id!r} uses unknown layout {ledger.layout!r}; "
                    f"manifest declares {sorted(known_layouts)}"
                )
            layout_bounds = set(self.manifest.bounds(ledger.layout))
            seen_blocks: Dict[BlockKey, LedgerBlock] = {}
            for block in ledger.blocks:
                if block.key in seen_blocks:
                    raise ReconcileError(
                        f"ledger {ledger.ledger_id!r} contains two blocks for {block.key}"
                    )
                seen_blocks[block.key] = block
                increments = _normalize_counts(block.increments)
                unknown = set(increments) - layout_bounds
                if unknown:
                    raise ReconcileError(
                        f"ledger {ledger.ledger_id!r} layout {ledger.layout!r} has no line at "
                        f"{sorted(_bound_token(b) for b in unknown)} for process {block.process_id}"
                    )
                derived = self._cumulative_from_increments(ledger.layout, increments)
                if block.cumulative is not None:
                    displayed = _normalize_counts(block.cumulative)
                    unknown = set(displayed) - layout_bounds
                    if unknown:
                        raise ReconcileError(
                            f"cumulative display in ledger {ledger.ledger_id!r} uses unknown "
                            f"bounds {sorted(_bound_token(b) for b in unknown)}"
                        )
                    for bound, displayed_value in displayed.items():
                        if displayed_value != derived[bound]:
                            raise ReconcileError(
                                f"increment/cumulative mismatch in ledger {ledger.ledger_id!r} "
                                f"process {block.process_id} gen {block.generation} at "
                                f"le<={_bound_token(bound)}: raw increments imply {derived[bound]}, "
                                f"file displays {displayed_value} "
                                f"(snapshot {self._snapshot_id(ledger)})"
                            )
                if block.count is not None and int(block.count) != derived[INF]:
                    raise ReconcileError(
                        f"count {block.count} disagrees with bucket total {derived[INF]} "
                        f"in ledger {ledger.ledger_id!r} process {block.process_id}"
                    )
                for coverage in block.batches:
                    if coverage.bound not in layout_bounds:
                        raise ReconcileError(
                            f"batch {coverage.batch_id!r} is claimed under le<="
                            f"{_bound_token(coverage.bound)} but ledger {ledger.ledger_id!r} "
                            f"layout {ledger.layout!r} has no such line"
                        )
                    if self.batches and coverage.batch_id not in self.batches:
                        raise ReconcileError(
                            f"ledger {ledger.ledger_id!r} references unregistered batch "
                            f"{coverage.batch_id!r}"
                        )

    def _cumulative_from_increments(self, layout: str,
                                    increments: Mapping[float, int]) -> Dict[float, int]:
        cumulative: Dict[float, int] = {}
        acc = 0
        for bound in self.manifest.bounds(layout):
            acc += increments.get(bound, 0)
            cumulative[bound] = acc
        return cumulative

    @staticmethod
    def _snapshot_id(ledger: Ledger) -> str:
        return ledger.snapshot.snapshot_id if ledger.snapshot else "<no snapshot>"

    # -- geometry ----------------------------------------------------------

    def _build_strips(self) -> List[_Strip]:
        finite = sorted({
            int(line.bound)
            for line in self.manifest.lines
            if line.bound != INF
        })
        strips: List[_Strip] = []
        previous: Optional[int] = None
        for bound in finite:
            strips.append(_Strip(previous, float(bound)))
            previous = bound
        strips.append(_Strip(previous, INF))
        return strips

    def _collect_block_keys(self) -> List[BlockKey]:
        keys: Dict[BlockKey, None] = {}
        for ledger in self.ledgers:
            for block in ledger.blocks:
                keys.setdefault(block.key, None)
        return list(keys)

    def _var(self, key: BlockKey, class_id: frozenset, strip_index: int) -> str:
        labels = ",".join(f"{name}={value}" for name, value in key.labels)
        return (
            f"p{key.process_id}g{key.generation}[{labels}]"
            f"|c{'+'.join(sorted(class_id))}|s{strip_index}"
        )

    def _evidence(self, ledger: Ledger, block: LedgerBlock, bound: Optional[float] = None,
                  batch_id: Optional[str] = None, kind: str = "") -> Dict[str, Any]:
        note: Dict[str, Any] = {
            "kind": kind,
            "ledger": ledger.ledger_id,
            "layout": ledger.layout,
            "snapshot": self._snapshot_id(ledger),
            "source_files": list(ledger.source_files),
            "process_id": block.process_id,
            "generation": block.generation,
            "labels": dict(block.key.labels),
        }
        if bound is not None:
            note["bound"] = _bound_token(bound)
            line = self.manifest.line_for(bound)
            if line is not None:
                note["introduced"] = line.introduced
        if batch_id is not None:
            note["batch_id"] = batch_id
        return note

    # -- model construction ------------------------------------------------

    def _build_model(self) -> None:
        global_domain = self.manifest.value_domain or (None, None)
        per_key_blocks: Dict[BlockKey, List[Tuple[Ledger, LedgerBlock]]] = {}
        for ledger in self.ledgers:
            for block in ledger.blocks:
                per_key_blocks.setdefault(block.key, []).append((ledger, block))
        for key, occurrences in per_key_blocks.items():
            self._build_block(key, occurrences, global_domain)

    def _ledger_bands(self, ledger: Ledger, block: LedgerBlock
                      ) -> List[Tuple[frozenset, int]]:
        increments = _normalize_counts(block.increments)
        bounds = self.manifest.bounds(ledger.layout)
        total = self._cumulative_from_increments(ledger.layout, increments)[INF]
        finite_sum = sum(increments.get(bound, 0) for bound in bounds if bound != INF)
        increments[INF] = total - finite_sum
        bands: List[Tuple[frozenset, int]] = []
        previous: Optional[float] = None
        for bound in bounds:
            indices = frozenset(
                index for index, strip in enumerate(self.strips)
                if (previous is None or strip.hi > previous) and strip.hi <= bound
            )
            bands.append((indices, increments.get(bound, 0)))
            previous = bound
        return bands

    def _build_block(self, key: BlockKey,
                     occurrences: Sequence[Tuple[Ledger, LedgerBlock]],
                     global_domain: Tuple[Optional[int], Optional[int]]) -> None:
        covering_ledgers = sorted(ledger.ledger_id for ledger, _ in occurrences)
        ledger_lookup = {ledger.ledger_id: (ledger, block) for ledger, block in occurrences}
        value_bounds = [strip.value_domain(global_domain) for strip in self.strips]

        bands_by_ledger: Dict[str, List[Tuple[frozenset, int]]] = {
            ledger.ledger_id: self._ledger_bands(ledger, block)
            for ledger, block in occurrences
        }

        def band_increment(ledger_id: str, strip_index: int) -> int:
            for indices, increment in bands_by_ledger[ledger_id]:
                if strip_index in indices:
                    return increment
            raise ReconcileError(
                f"internal: strip {strip_index} covered by no band of ledger {ledger_id!r}"
            )

        # Membership of every registered batch mentioned in this block.
        batch_membership: Dict[str, frozenset] = {}
        for ledger, block in occurrences:
            for coverage in block.batches:
                membership = batch_membership.setdefault(
                    coverage.batch_id, frozenset((ledger.ledger_id,))
                )
                if ledger.ledger_id not in membership:
                    batch_membership[coverage.batch_id] = \
                        membership | frozenset((ledger.ledger_id,))

        # Decision variables only for classes several ledgers share.
        multi_classes: List[frozenset] = [
            frozenset(subset)
            for size in range(2, len(covering_ledgers) + 1)
            for subset in itertools.combinations(covering_ledgers, size)
        ]
        class_vars: Dict[frozenset, List[str]] = {}
        for class_id in multi_classes:
            variables = [self._var(key, class_id, index) for index in range(self.strip_count)]
            class_vars[class_id] = variables
            for index, name in enumerate(variables):
                cap = min(band_increment(ledger_id, index) for ledger_id in class_id)
                self.domains[name] = (0, cap)

        # Band capacity: the shared items a ledger sees in a band cannot exceed
        # the band's raw increment; the non-negative remainder is its residual.
        for ledger_id, bands in bands_by_ledger.items():
            ledger, block = ledger_lookup[ledger_id]
            own_classes = [class_id for class_id in multi_classes if ledger_id in class_id]
            for indices, increment in bands:
                expr = {
                    class_vars[class_id][index]: 1
                    for class_id in own_classes
                    for index in indices
                }
                bound = self.strips[max(indices)].hi
                self.constraints.append(_Linear(
                    expr, "<=", increment,
                    self._evidence(ledger, block, bound=bound, kind="band_capacity"),
                ))

        self._apply_batch_constraints(key, occurrences, ledger_lookup,
                                      bands_by_ledger, class_vars, batch_membership)
        self._check_latency_sums(occurrences, bands_by_ledger, value_bounds)

        for batch_id, membership in batch_membership.items():
            if len(membership) > 1:
                self.overlaps.append({
                    "batch_id": batch_id,
                    "process_id": key.process_id,
                    "generation": key.generation,
                    "labels": dict(key.labels),
                    "ledgers": sorted(membership),
                    "note": "same registered batch constrained by several ledgers; joined as "
                            "one set of observations, not deduped by label or value",
                })
            self._register_batch_block(batch_id, key)

        self.block_meta[key] = {
            "multi_classes": [sorted(class_id) for class_id in multi_classes],
            "class_vars": {
                ",".join(sorted(class_id)): vars_ for class_id, vars_ in class_vars.items()
            },
            "value_bounds": value_bounds,
            "covering_ledgers": covering_ledgers,
            "bands_by_ledger": {
                ledger_id: [(sorted(indices), increment) for indices, increment in bands]
                for ledger_id, bands in bands_by_ledger.items()
            },
            "snapshots": [
                {"snapshot": self._snapshot_id(ledger), "ledger": ledger.ledger_id,
                 "layout": ledger.layout, "source_files": list(ledger.source_files)}
                for ledger, _ in occurrences
            ],
            "batches": sorted(batch_membership),
        }

    def _apply_batch_constraints(self, key: BlockKey,
                                 occurrences: Sequence[Tuple[Ledger, LedgerBlock]],
                                 ledger_lookup: Mapping[str, Tuple[Ledger, LedgerBlock]],
                                 bands_by_ledger: Mapping[str, List[Tuple[frozenset, int]]],
                                 class_vars: Mapping[frozenset, List[str]],
                                 batch_membership: Mapping[str, frozenset]) -> None:
        for batch_id, membership in batch_membership.items():
            batch = self.batches.get(batch_id)
            if batch is None:
                continue
            need = batch.size if batch.size is not None else 1
            feasible_strips = [
                index for index, strip in enumerate(self.strips)
                if any(strip.contains_value(value) for value in batch.allowed_values)
            ]
            if not feasible_strips:
                raise ReconcileError(
                    f"batch {batch_id!r} allowed values {list(batch.allowed_values)} fall in no "
                    f"strip of process {key.process_id} gen {key.generation}"
                )
            if membership in class_vars:
                expr = {class_vars[membership][index]: 1 for index in feasible_strips}
                ledger_id = next(iter(membership))
                ledger, block = ledger_lookup[ledger_id]
                self.constraints.append(_Linear(
                    expr, ">=", need,
                    self._evidence(ledger, block, batch_id=batch_id,
                                   kind="batch_value_domain"),
                ))
            else:
                # Single-ledger batch: it lives in that ledger's residual. The
                # residual room spread over every eligible band must hold the
                # whole batch, so constrain it once as an aggregate rather than
                # forcing each band to reserve room it may not need.
                ledger_id = next(iter(membership))
                ledger, block = ledger_lookup[ledger_id]
                own = [c for c in class_vars if ledger_id in c]
                expr: Dict[str, int] = {}
                eligible_capacity = 0
                for coverage in block.batches:
                    if coverage.batch_id != batch_id:
                        continue
                    for indices, increment in bands_by_ledger[ledger_id]:
                        if not (indices & frozenset(feasible_strips)):
                            continue
                        eligible_capacity += increment
                        for klass in own:
                            for index in indices:
                                name = class_vars[klass][index]
                                expr[name] = expr.get(name, 0) + 1
                if expr:
                    self.constraints.append(_Linear(
                        expr, "<=", max(0, eligible_capacity - need),
                        self._evidence(ledger, block, batch_id=batch_id,
                                       kind="batch_residual_capacity"),
                    ))

    def _check_latency_sums(self,
                            occurrences: Sequence[Tuple[Ledger, LedgerBlock]],
                            bands_by_ledger: Mapping[str, List[Tuple[frozenset, int]]],
                            value_bounds: Sequence[Tuple[Optional[int], Optional[int]]]
                            ) -> None:
        for ledger, block in occurrences:
            if block.latency_sum is None:
                continue
            low: Optional[int] = 0
            high: Optional[int] = 0
            for indices, increment in bands_by_ledger[ledger.ledger_id]:
                mins = [value_bounds[index][0] for index in indices
                        if value_bounds[index][0] is not None]
                maxs = [value_bounds[index][1] for index in indices
                        if value_bounds[index][1] is not None]
                band_min = min(mins) if mins else None
                band_max = max(maxs) if maxs else None
                if band_min is None:
                    if increment:
                        low = None
                elif low is not None:
                    low += increment * band_min
                if band_max is None:
                    if increment:
                        high = None
                elif high is not None:
                    high += increment * band_max
            total = int(block.latency_sum)
            if (low is not None and total < low) or (high is not None and total > high):
                raise ReconcileError(
                    f"latency sum {total} in ledger {ledger.ledger_id!r} process "
                    f"{block.process_id} gen {block.generation} is outside the reachable "
                    f"integer interval [{low}, {high}] given the bucket band increments "
                    f"(snapshot {self._snapshot_id(ledger)})"
                )

    def _register_batch_block(self, batch_id: str, key: BlockKey) -> None:
        existing = self._batch_blocks.get(batch_id)
        if existing is not None and existing != key:
            raise ReconcileError(
                f"batch {batch_id!r} appears under different process blocks "
                f"{existing} and {key}; a registered batch must stay in one block"
            )
        self._batch_blocks[batch_id] = key

    # -- connected-component decomposition --------------------------------

    @staticmethod
    def _components(domains: Mapping[str, Tuple[int, int]],
                    constraints: Sequence[_Linear]) -> List[List[str]]:
        parent = {name: name for name in domains}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for constraint in constraints:
            names = [name for name in constraint.coeffs if name in parent]
            for name in names[1:]:
                union(names[0], name)
        grouped: Dict[str, List[str]] = {}
        for name in parent:
            grouped.setdefault(find(name), []).append(name)
        return list(grouped.values())

    def _feasible_witness(self, domains: Mapping[str, Tuple[int, int]],
                          extra: Sequence[_Linear] = ()) -> Optional[Dict[str, int]]:
        constraints = list(self.constraints) + list(extra)
        components = self._components(domains, constraints)
        assignment: Dict[str, int] = {}
        for component in components:
            members = set(component)
            sub_domains = {name: domains[name] for name in component}
            sub_constraints = [
                constraint for constraint in constraints
                if all(name in members for name in constraint.coeffs)
            ]
            solution = _Solver(sub_domains, sub_constraints).witness()
            if solution is None:
                return None
            assignment.update(solution)
        # Constant-only constraints reference no decision variables.
        for constraint in constraints:
            if not any(name in domains for name in constraint.coeffs):
                if not constraint.satisfied({}):
                    return None
        for name, (low, _high) in domains.items():
            assignment.setdefault(name, low)
        return assignment

    def _optimize(self, expression: Mapping[str, int], maximize: bool,
                  domains: Optional[Mapping[str, Tuple[int, int]]] = None,
                  extra: Sequence[_Linear] = ()) -> int:
        domains = dict(self.domains if domains is None else domains)
        constraints = list(self.constraints) + list(extra)
        components = self._components(domains, constraints)
        total = 0
        for component in components:
            members = set(component)
            sub_expr = {
                name: coeff for name, coeff in expression.items() if name in members
            }
            sub_domains = {name: domains[name] for name in component}
            sub_constraints = [
                constraint for constraint in constraints
                if all(name in members for name in constraint.coeffs)
            ]
            solver = _Solver(sub_domains, sub_constraints)
            if sub_expr:
                total += solver.optimize(sub_expr, maximize)
        return total

    # -- window expressions ------------------------------------------------

    def _labels_groups(self) -> List[Dict[str, str]]:
        groups: Dict[Tuple[Tuple[str, str], ...], Dict[str, str]] = {}
        for key in self.block_keys:
            groups.setdefault(key.labels, dict(key.labels))
        return list(groups.values())

    def _group_keys(self, labels: Mapping[str, str]) -> List[BlockKey]:
        wanted = tuple(sorted(labels.items()))
        return [key for key in self.block_keys if key.labels == wanted]

    def _shared_expr(self, key: BlockKey, bound: float) -> Dict[str, int]:
        expr: Dict[str, int] = {}
        for vars_ in self.block_meta[key]["class_vars"].values():
            for index, strip in enumerate(self.strips):
                if strip.hi <= bound:
                    name = vars_[index]
                    expr[name] = expr.get(name, 0) + 1
        return expr

    def _residual_window(self, key: BlockKey, ledger_id: str, bound: float) -> _Expr:
        """This ledger's single-ledger residual count not above ``bound``."""
        expr = _Expr()
        for indices, increment in self.block_meta[key]["bands_by_ledger"][ledger_id]:
            band_strips = sorted(indices)
            band_hi = self.strips[band_strips[-1]].hi
            band_lo = self.strips[band_strips[0]].lo
            if band_hi <= bound:
                # Whole band is inside the window.
                band = _Expr(increment)
                for class_id, vars_ in self.block_meta[key]["class_vars"].items():
                    if ledger_id in class_id.split(","):
                        for index in indices:
                            band = band.add_var(vars_[index], sign=-1)
                expr = expr.add(band)
            # A band the window cuts through contributes a free residual pool;
            # that pool is accounted for separately by _window_pool_expr.
        return expr

    def _window_fixed_expr(self, labels: Mapping[str, str], bound: float) -> _Expr:
        expr = _Expr()
        for key in self._group_keys(labels):
            for name in self._shared_expr(key, bound):
                expr = expr.add_var(name)
            for ledger_id in self.block_meta[key]["covering_ledgers"]:
                expr = expr.add(self._residual_window(key, ledger_id, bound))
        return expr

    def _window_pool_expr(self, labels: Mapping[str, str], bound: float) -> _Expr:
        """Maximum extra residual hidden in bands the window cuts through."""
        expr = _Expr()
        for key in self._group_keys(labels):
            for ledger_id in self.block_meta[key]["covering_ledgers"]:
                for indices, increment in self.block_meta[key]["bands_by_ledger"][ledger_id]:
                    band_strips = sorted(indices)
                    band_hi = self.strips[band_strips[-1]].hi
                    band_lo = self.strips[band_strips[0]].lo
                    if not (band_hi > bound and (band_lo is None or bound > band_lo)):
                        continue
                    residual = _Expr(increment)
                    for class_id, vars_ in self.block_meta[key]["class_vars"].items():
                        if ledger_id in class_id.split(","):
                            for index in indices:
                                residual = residual.add_var(vars_[index], sign=-1)
                    expr = expr.add(residual)
        return expr

    # -- queries -----------------------------------------------------------

    def window_report(self, target_bounds: Sequence[Number],
                      pair_queries: Sequence[Mapping[str, Any]] = ()) -> List[ReconciliationReport]:
        target_bounds = [_parse_bound(bound) for bound in target_bounds]
        reports: List[ReconciliationReport] = []
        for labels in self._labels_groups():
            keys = self._group_keys(labels)

            fixed_total = self._window_fixed_expr(labels, INF)
            pool_total = self._window_pool_expr(labels, INF)
            total_min = fixed_total.const + self._optimize(fixed_total.coeffs, False)
            total_max_expr = fixed_total.add(pool_total)
            total_max = total_max_expr.const + self._optimize(
                total_max_expr.coeffs, True
            )

            targets: List[TargetResult] = []
            for bound in target_bounds:
                fixed = self._window_fixed_expr(labels, bound)
                pool = self._window_pool_expr(labels, bound)
                minimum = fixed.const + self._optimize(fixed.coeffs, False)
                upper = fixed.add(pool)
                maximum = upper.const + self._optimize(upper.coeffs, True)
                exact = minimum == maximum
                evidence: List[Dict[str, Any]] = []
                if not exact:
                    evidence = self._inexact_evidence(keys, bound)
                targets.append(TargetResult(
                    bound=bound,
                    minimum=minimum,
                    maximum=maximum,
                    exact=exact,
                    value=minimum if exact else None,
                    evidence=evidence,
                ))

            pair_results = [
                self._pair_result(labels, query) for query in pair_queries
            ]
            reports.append(ReconciliationReport(
                metric_name=self.manifest.metric_name,
                labels_group=labels,
                total_min=total_min,
                total_max=total_max,
                total_exact=total_min == total_max,
                targets=targets,
                pair_results=pair_results,
                overlaps=[item for item in self.overlaps
                          if dict(item["labels"]) == labels],
            ))
        return reports

    def _inexact_evidence(self, keys: Sequence[BlockKey], bound: float) -> List[Dict[str, Any]]:
        line = self.manifest.line_for(bound)
        evidence: List[Dict[str, Any]] = []
        for key in keys:
            meta = self.block_meta[key]
            open_classes: List[Dict[str, Any]] = []
            for class_id, vars_ in meta["class_vars"].items():
                open_strips = [
                    str(self.strips[index])
                    for index, name in enumerate(vars_)
                    if self.strips[index].hi <= bound
                    and self.domains[name][0] != self.domains[name][1]
                ]
                if open_strips:
                    ledger_ids = class_id.split(",")
                    layouts = sorted({
                        snap["layout"] for snap in meta["snapshots"]
                        if snap["ledger"] in ledger_ids
                    })
                    has_line = any(bound in set(self.manifest.bounds(layout)) for layout in layouts)
                    open_classes.append({
                        "coverage_class": ledger_ids,
                        "layouts": layouts,
                        "open_strips": open_strips,
                        "reason": (
                            f"none of {layouts} carries a le<={_bound_token(bound)} line"
                            if not has_line else
                            "strip counts remain open after applying every covering ledger"
                        ),
                    })
            straddle: List[Dict[str, Any]] = []
            for ledger_id in meta["covering_ledgers"]:
                for indices, _increment in meta["bands_by_ledger"][ledger_id]:
                    band_strips = sorted(indices)
                    band_hi = self.strips[band_strips[-1]].hi
                    band_lo = self.strips[band_strips[0]].lo
                    if band_hi > bound and (band_lo is None or bound > band_lo):
                        straddle.append({
                            "ledger": ledger_id,
                            "bound": _bound_token(bound),
                            "strips": [str(self.strips[index]) for index in band_strips],
                        })
            if open_classes or straddle:
                evidence.append({
                    "kind": "unpinned_split",
                    "process_id": key.process_id,
                    "generation": key.generation,
                    "labels": dict(key.labels),
                    "bound_line": None if line is None else {
                        "bound": _bound_token(line.bound),
                        "layouts": list(line.layouts),
                        "introduced": line.introduced,
                    },
                    "open_classes": open_classes,
                    "residual_pools": straddle,
                    "snapshots": meta["snapshots"],
                    "registered_batches": meta["batches"],
                })
        return evidence

    def _pair_result(self, labels: Mapping[str, str],
                     query: Mapping[str, Any]) -> PairResult:
        bounds = query["bounds"]
        if len(bounds) != 2:
            raise ReconcileError("pair query must name exactly two bounds")
        counts = query["counts"]
        if len(counts) != 2:
            raise ReconcileError("pair query must give exactly two counts")
        bound_a, bound_b = _parse_bound(bounds[0]), _parse_bound(bounds[1])
        count_a, count_b = int(counts[0]), int(counts[1])

        ranges = {}
        for bound in (bound_a, bound_b):
            fixed = self._window_fixed_expr(labels, bound)
            pool = self._window_pool_expr(labels, bound)
            minimum = fixed.const + self._optimize(fixed.coeffs, False)
            upper = fixed.add(pool)
            maximum = upper.const + self._optimize(upper.coeffs, True)
            ranges[bound] = (minimum, maximum)
        achievable = (
            (ranges[bound_a][0], ranges[bound_b][0]),
            (ranges[bound_a][1], ranges[bound_b][1]),
        )
        if not (ranges[bound_a][0] <= count_a <= ranges[bound_a][1]) or \
                not (ranges[bound_b][0] <= count_b <= ranges[bound_b][1]):
            return PairResult(
                bounds=(bound_a, bound_b),
                requested=(count_a, count_b),
                feasible=False,
                evidence=self._conflict_evidence(labels, bound_a, bound_b),
                achievable=achievable,
            )

        assignment = self._pair_witness(labels, (bound_a, bound_b), (count_a, count_b))
        if assignment is None:
            return PairResult(
                bounds=(bound_a, bound_b),
                requested=(count_a, count_b),
                feasible=False,
                evidence=self._conflict_evidence(labels, bound_a, bound_b),
                achievable=achievable,
            )
        return PairResult(
            bounds=(bound_a, bound_b),
            requested=(count_a, count_b),
            feasible=True,
            witness=self._witness_allocation(labels, assignment),
        )

    def _pair_witness(self, labels: Mapping[str, str],
                      bounds: Tuple[float, float],
                      counts: Tuple[int, int]) -> Optional[Dict[str, int]]:
        # Add one pool variable per (ledger, bound) for the residual portion of a
        # band the window cuts through, then pin each window to its target.
        domains = dict(self.domains)
        extra: List[_Linear] = []
        pool_counter = itertools.count()
        for bound, wanted in zip(bounds, counts):
            fixed = self._window_fixed_expr(labels, bound)
            coeffs = dict(fixed.coeffs)
            const = fixed.const
            for key in self._group_keys(labels):
                for ledger_id in self.block_meta[key]["covering_ledgers"]:
                    for indices, increment in self.block_meta[key]["bands_by_ledger"][ledger_id]:
                        band_strips = sorted(indices)
                        band_hi = self.strips[band_strips[-1]].hi
                        band_lo = self.strips[band_strips[0]].lo
                        if not (band_hi > bound and (band_lo is None or bound > band_lo)):
                            continue
                        pool_name = f"pool{next(pool_counter)}"
                        domains[pool_name] = (0, increment)
                        # pool <= band increment - shared count
                        # <=> pool + sum(shared) <= increment
                        cap_coeffs = {pool_name: 1}
                        for class_id, vars_ in self.block_meta[key]["class_vars"].items():
                            if ledger_id in class_id.split(","):
                                for index in indices:
                                    name = vars_[index]
                                    cap_coeffs[name] = cap_coeffs.get(name, 0) + 1
                        extra.append(_Linear(cap_coeffs, "<=", increment,
                                             {"kind": "residual_pool_cap"}))
                        coeffs[pool_name] = coeffs.get(pool_name, 0) + 1
            # const + sum coeffs*x = wanted  <=>  sum coeffs*x = wanted - const
            extra.append(_Linear(coeffs, "=", wanted - const,
                                 {"kind": "pair_window", "bound": _bound_token(bound)}))
        return self._feasible_witness(domains, extra)

    def _conflict_evidence(self, labels: Mapping[str, str],
                           bound_a: float, bound_b: float) -> List[Dict[str, Any]]:
        evidence: List[Dict[str, Any]] = []
        for key in self._group_keys(labels):
            meta = self.block_meta[key]
            evidence.append({
                "kind": "blocking_snapshot",
                "process_id": key.process_id,
                "generation": key.generation,
                "labels": dict(key.labels),
                "snapshots": meta["snapshots"],
                "registered_batches": meta["batches"],
                "pinning_constraints": [
                    c.note for c in self.constraints
                    if c.note.get("process_id") == key.process_id
                    and c.note.get("generation") == key.generation
                ],
                "queried_bounds": [_bound_token(bound_a), _bound_token(bound_b)],
            })
        return evidence

    def _witness_allocation(self, labels: Mapping[str, str],
                            assignment: Mapping[str, int]) -> Dict[str, Any]:
        blocks: List[Dict[str, Any]] = []
        for key in self._group_keys(labels):
            meta = self.block_meta[key]
            class_detail = []
            for class_id, vars_ in meta["class_vars"].items():
                strip_detail = []
                for index, name in enumerate(vars_):
                    vmin, vmax = meta["value_bounds"][index]
                    strip_detail.append({
                        "strip": str(self.strips[index]),
                        "shared_count": assignment.get(name, 0),
                        "value_domain": [vmin, vmax],
                    })
                class_detail.append({"coverage_class": class_id.split(","),
                                     "strips": strip_detail})
            residual_detail = []
            for ledger_id, bands in meta["bands_by_ledger"].items():
                for indices, increment in bands:
                    shared = sum(
                        assignment.get(vars_[index], 0)
                        for class_id, vars_ in meta["class_vars"].items()
                        if ledger_id in class_id.split(",")
                        for index in indices
                    )
                    residual_detail.append({
                        "ledger": ledger_id,
                        "band_strips": [str(self.strips[index]) for index in sorted(indices)],
                        "raw_increment": increment,
                        "residual_count": increment - shared,
                    })
            blocks.append({
                "process_id": key.process_id,
                "generation": key.generation,
                "labels": dict(key.labels),
                "shared_classes": class_detail,
                "ledger_residuals": residual_detail,
            })
        return {"blocks": blocks}


# ---------------------------------------------------------------------------
# JSON loading and mmap ingestion
# ---------------------------------------------------------------------------


def _coerce_coverage(raw: Mapping[str, Any]) -> BatchCoverage:
    return BatchCoverage(batch_id=raw["batch_id"], bound=_parse_bound(raw["bound"]))


def _coerce_block(raw: Mapping[str, Any]) -> LedgerBlock:
    return LedgerBlock(
        process_id=int(raw["process_id"]),
        generation=int(raw.get("generation", 0)),
        labels=dict(raw.get("labels", {})),
        increments=_normalize_counts(raw["increments"]),
        cumulative=None if raw.get("cumulative") is None else _normalize_counts(raw["cumulative"]),
        count=None if raw.get("count") is None else int(raw["count"]),
        latency_sum=None if raw.get("sum") is None else int(raw["sum"]),
        batches=[_coerce_coverage(item) for item in raw.get("batches", ())],
    )


def _coerce_ledger(raw: Mapping[str, Any]) -> Ledger:
    snapshot_raw = raw.get("snapshot")
    snapshot = None if snapshot_raw is None else Snapshot(
        snapshot_id=snapshot_raw["snapshot_id"],
        path=snapshot_raw.get("path", ""),
        kind=snapshot_raw.get("kind", ""),
    )
    return Ledger(
        ledger_id=raw["ledger_id"],
        layout=raw["layout"],
        blocks=[_coerce_block(item) for item in raw.get("blocks", ())],
        snapshot=snapshot,
        source_files=list(raw.get("source_files", ())),
    )


def load_input(path: str) -> Tuple[Manifest, List[Ledger], Dict[str, Batch], List[Any], List[Any]]:
    """Load a reconciliation input document from a local JSON file."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    manifest = Manifest.from_dict(data["manifest"])
    ledgers = [_coerce_ledger(item) for item in data.get("ledgers", ())]
    batches = {
        item["batch_id"]: Batch(
            batch_id=item["batch_id"],
            allowed_values=tuple(int(value) for value in item["allowed_values"]),
            size=None if item.get("size") is None else int(item["size"]),
        )
        for item in data.get("batches", ())
    }
    return (
        manifest,
        ledgers,
        batches,
        data.get("target_bounds", ()),
        data.get("pair_queries", ()),
    )


def reconcile_file(path: str) -> List[ReconciliationReport]:
    """Convenience entry point: load a JSON document and reconcile it."""
    manifest, ledgers, batches, targets, pair_queries = load_input(path)
    reconciler = Reconciler(manifest, ledgers, batches)
    return reconciler.window_report(targets, pair_queries)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if value == INF:
            return "+Inf"
        if value == -INF:
            return "-Inf"
        if math.isnan(value):
            return "NaN"
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line entry point: reconcile a local JSON scrape document."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Reconcile histogram bucket counts across a bucket-layout rollout.",
    )
    parser.add_argument("input", help="path to the JSON scrape document")
    parser.add_argument(
        "--target", action="append", default=[],
        help="target publication line (microsecond bound, or +Inf); may be repeated",
    )
    parser.add_argument(
        "--pair", action="append", default=[],
        help="feasibility query as bound:count,bound:count, e.g. 750:4,1000:6",
    )
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    manifest, ledgers, batches, file_targets, file_pairs = load_input(args.input)
    target_bounds = args.target or list(file_targets)
    pair_queries = list(file_pairs)
    for raw in args.pair:
        parts = raw.split(",")
        if len(parts) != 2:
            parser.error(f"--pair expects bound:count,bound:count, got {raw!r}")
        bound_pair, count_pair = [], []
        for part in parts:
            bound_token, count_token = part.split(":", 1)
            bound_pair.append(bound_token)
            count_pair.append(int(count_token))
        pair_queries.append({"bounds": bound_pair, "counts": count_pair})

    reconciler = Reconciler(manifest, ledgers, batches)
    reports = reconciler.window_report(target_bounds, pair_queries)
    print(json.dumps(_json_safe([report.to_dict() for report in reports]), indent=args.indent))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def ledger_from_mmap_files(ledger_id: str, layout: str, files: Sequence[str],
                           snapshot: Optional[Snapshot] = None,
                           generations: Optional[Mapping[int, int]] = None) -> Ledger:
    """Turn ordinary per-process ``histogram_<pid>.db`` files into a ledger.

    The mmap files hold per-process cumulative bucket displays, which this
    helper keeps separate from the raw increments (it derives the increments
    itself).  A reused pid can be disambiguated through ``generations``.
    """
    generations = dict(generations or {})
    grouped: Dict[BlockKey, Dict[str, Any]] = {}
    for path in files:
        base = os.path.basename(path)
        if not base.startswith("histogram_") or not base.endswith(".db"):
            raise ReconcileError(f"not a histogram mmap file: {path}")
        pid = int(base[len("histogram_"):-len(".db")])
        generation = generations.get(pid, 0)
        for key, value, _timestamp, _pos in MmapedDict.read_all_values_from_file(path):
            metric_name, sample_name, labels, _help = json.loads(key)
            label_dict = {
                name: str(one) for name, one in labels.items() if name != "le"
            }
            block_key = BlockKey(pid, generation, tuple(sorted(label_dict.items())))
            record = grouped.setdefault(
                block_key, {"labels": label_dict, "buckets": {}, "sum": None}
            )
            if sample_name == metric_name + "_sum":
                record["sum"] = int(value)
            elif sample_name == metric_name + "_bucket":
                record["buckets"][_parse_bound(labels["le"])] = int(value)

    blocks: List[LedgerBlock] = []
    for key, record in sorted(grouped.items(),
                              key=lambda item: (item[0].process_id, item[0].generation)):
        # The mmap file stores one ValueClass per histogram band, and an
        # observation increments exactly one band, so the file values are the
        # raw increments. The cumulative ``le`` display is only produced later
        # (e.g. by MultiProcessCollector.merge); derive it here but keep it
        # separate from the increments it came from.
        increments = {
            bound: value for bound, value in record["buckets"].items() if bound != INF
        }
        cumulative: Dict[float, int] = {}
        running = 0
        for bound in sorted(increments):
            running += increments[bound]
            cumulative[bound] = running
        blocks.append(LedgerBlock(
            process_id=key.process_id,
            generation=key.generation,
            labels=record["labels"],
            increments=increments,
            cumulative=cumulative,
            latency_sum=record["sum"],
        ))
    return Ledger(
        ledger_id=ledger_id,
        layout=layout,
        blocks=blocks,
        snapshot=snapshot,
        source_files=list(files),
    )
