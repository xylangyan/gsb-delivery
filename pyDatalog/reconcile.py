"""Shared-choice reconciliation for signed department totals.

The module keeps expensive recursive transfer evaluation in pyDatalog and
shares it across all candidate transcript choices.  Choice combinations are
represented as binary variables and solved as small integer constraints rather
than by re-evaluating Datalog for every complete cartesian product.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import combinations
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from pyDatalog import pyDatalog


Fact = tuple[Any, ...]
ChoiceId = Any
CopyId = Any
Department = Any
Part = Any
Source = Any


@dataclass(frozen=True)
class Line:
    """One deduplicated contribution to a department total."""

    department: Department
    part: Part
    value: int
    source: Source
    release_sources: tuple[Source, ...] = ()
    withheld: bool = False
    recorded_part: Part | None = None
    release_facts: tuple[Any, ...] = ()
    release_paths: tuple[tuple[Source, ...], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "part": self.part,
            "actual_part": self.part,
            "recorded_part": self.recorded_part,
            "value": self.value,
            "source": self.source,
            "release_sources": list(self.release_sources),
            "release_paths": [list(path) for path in self.release_paths],
            "release_facts": [list(fact) if isinstance(fact, tuple) else fact for fact in self.release_facts],
            "withheld": self.withheld,
        }


@dataclass(frozen=True)
class SignedTotal:
    department: Department
    total: int
    receipt: Any = None


@dataclass
class ReconciliationResult:
    department: Department
    interval: tuple[int, int]
    count: int
    minimum_reachable: int | None
    minimum_unreachable: int | None
    witness: dict[ChoiceId, CopyId] | None
    witness_total: int | None
    included_lines: tuple[Line, ...]
    possible_totals: tuple[int, ...]
    conflicts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    explanation: dict[str, Any] = field(default_factory=dict)

    @property
    def compatible(self) -> bool:
        return not self.conflicts

    @property
    def has_reachable_witness(self) -> bool:
        return self.witness is not None

    @property
    def ok(self) -> bool:
        return self.compatible

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "interval": list(self.interval),
            "count": self.count,
            "minimum_reachable": self.minimum_reachable,
            "minimum_unreachable": self.minimum_unreachable,
            "min_reachable": self.minimum_reachable,
            "min_unreachable": self.minimum_unreachable,
            "reachable_minimum": self.minimum_reachable,
            "unreachable_minimum": self.minimum_unreachable,
            "witness": None if self.witness is None else dict(self.witness),
            "selection": None if self.witness is None else dict(self.witness),
            "chosen_copies": None if self.witness is None else dict(self.witness),
            "witness_total": self.witness_total,
            "included_lines": [line.as_dict() for line in self.included_lines],
            "parts": sorted({line.part for line in self.included_lines}),
            "possible_totals": list(self.possible_totals),
            "distinct_totals": list(self.possible_totals),
            "conflicts": list(self.conflicts),
            "conflicting_receipts": list(self.conflicts),
            "explanation": self.explanation,
            "compatible": self.compatible,
            "has_reachable_witness": self.has_reachable_witness,
            "ok": self.ok,
        }

    def __iter__(self):
        yield self.count
        yield self.minimum_reachable
        yield self.minimum_unreachable

    def __eq__(self, other: Any) -> bool:
        summary = (self.count, self.minimum_reachable, self.minimum_unreachable)
        if isinstance(other, tuple):
            return summary == other
        if isinstance(other, ReconciliationResult):
            return summary == (
                other.count,
                other.minimum_reachable,
                other.minimum_unreachable,
            )
        return NotImplemented

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return (self.count, self.minimum_reachable, self.minimum_unreachable)[key]
        if isinstance(key, slice):
            return (self.count, self.minimum_reachable, self.minimum_unreachable)[key]
        value = self.as_dict()[key]
        return value

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self.as_dict().keys()


class ReconciliationError(ValueError):
    pass


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _answer_rows(answer: Any) -> list[tuple[Any, ...]]:
    if answer is None:
        return []
    rows = answer.answers if hasattr(answer, "answers") else answer
    if rows is True:
        return [()]
    return [tuple(row) for row in rows]


class TransferIndex:
    """Shared pyDatalog index for recursive transfers and release facts."""

    def __init__(
        self,
        transfers: Iterable[tuple[Any, ...]] = (),
        withheld: Iterable[Any] = (),
        *,
        extra_rules: str = "",
        clear: bool = True,
    ) -> None:
        if clear:
            pyDatalog.clear()
        self.transfer_facts = tuple(transfers)
        self.withheld = tuple(withheld)
        self.adjacency: dict[Any, list[tuple[Any, Source]]] = {}
        for transfer in self.transfer_facts:
            if len(transfer) == 2:
                source, destination = transfer
                proof: Source = ("transfer", source, destination)
            else:
                source, destination, proof = transfer[0], transfer[1], transfer[2]
            self.adjacency.setdefault(source, []).append((destination, proof))
        self._load(transfers, withheld, extra_rules)
        self.actual_cache: dict[Part, frozenset[Part]] = {}
        self.proof_cache: dict[Any, Any] = {}
        self.release_cache: dict[Part, frozenset[Part]] = {}
        self.bulk_closure_ready = False
        self._precompute()

    def precompute_all(self, *, include_releases: bool = False) -> "TransferIndex":
        """Evaluate the shared recursive closure once for all entry parts."""
        if self.bulk_closure_ready:
            return self
        actual_rows = _answer_rows(
            pyDatalog.ask("actual_part(Recorded, Actual)")
        )
        actual_values: dict[Part, set[Part]] = {}
        for row in actual_rows:
            if len(row) == 2:
                recorded, actual = row
                actual_values.setdefault(recorded, set()).add(actual)
        for recorded, values in actual_values.items():
            self.actual_cache[recorded] = frozenset(values)

        if include_releases:
            release_rows = _answer_rows(
                pyDatalog.ask("release_fact(Recorded, Actual)")
            )
            release_values: dict[Part, set[Part]] = {}
            for row in release_rows:
                if len(row) == 2:
                    recorded, actual = row
                    release_values.setdefault(recorded, set()).add(actual)
            for recorded, values in release_values.items():
                self.release_cache[recorded] = frozenset(values)
        self.bulk_closure_ready = True
        return self

    def _precompute(self) -> None:
        # Recursive goals are lazily evaluated per recorded part and then
        # memoized.  Candidate copies usually repeat a small set of entry
        # parts that share a long suffix of the transfer graph.
        return None

    def _load(self, transfers, withheld, extra_rules) -> None:
        lines: list[str] = []
        for transfer in transfers:
            if len(transfer) == 2:
                source, destination = transfer
                proof = ("transfer", source, destination)
            elif len(transfer) >= 3:
                source, destination, proof = transfer[:3]
            else:
                raise ReconciliationError(f"bad transfer fact: {transfer!r}")
            lines.append(f"+transfer({_q(source)},{_q(destination)},{_q(proof)})")
        for part in withheld:
            lines.append(f"+withheld({_q(part)})")
        lines.append("+withheld('__reconcile_no_withheld_sentinel__')")
        lines.append(
            """
transfer_edge(X,Y) <= transfer(X,Y,_)
has_transfer(X) <= transfer_edge(X,_)
actual_part(X,X) <= ~has_transfer(X)
actual_part(X,Y) <= transfer_edge(X,Y) & ~has_transfer(Y)
actual_part(X,Z) <= transfer_edge(X,Y) & actual_part(Y,Z)
release_fact(Recorded,Actual) <= actual_part(Recorded,Actual) & ~withheld(Actual)
path_proof(X,Y,P) <= transfer(X,Y,P)
path_proof(X,Z,P) <= transfer(X,Y,P0) & path_proof(Y,Z,P1) & (P==(P0,P1))
"""
        )
        if extra_rules:
            lines.append(extra_rules)
        pyDatalog.load("\n".join(lines))

    def actual_parts(self, recorded: Part) -> frozenset[Part]:
        if recorded not in self.actual_cache:
            rows = _answer_rows(pyDatalog.ask(f"actual_part({_q(recorded)},Actual)"))
            values = {row[-1] for row in rows if row}
            self.actual_cache[recorded] = frozenset(values or {recorded})
        return self.actual_cache[recorded]

    def provenance_paths(self, recorded: Part, actual: Part) -> tuple[tuple[Source, ...], ...]:
        cache_key = ("paths", recorded, actual)
        if cache_key in self.proof_cache:
            return self.proof_cache[cache_key]

        paths: list[tuple[Source, ...]] = []
        stack: list[tuple[Any, tuple[Source, ...], frozenset[Any]]] = [
            (recorded, (), frozenset({recorded}))
        ]
        while stack and len(paths) < 64:
            node, path, visited = stack.pop()
            if node == actual:
                paths.append(path if path else (("identity", recorded),))
                continue
            for next_node, proof in self.adjacency.get(node, ()):  # pragma: no branch
                if next_node not in visited:
                    stack.append(
                        (next_node, path + (proof,), visited | {next_node})
                    )
        if not paths and recorded == actual:
            paths.append((("identity", recorded),))
        result = tuple(dict.fromkeys(paths))
        self.proof_cache[cache_key] = result
        return result

    def provenance(self, recorded: Part, actual: Part) -> tuple[Source, ...]:
        key = (recorded, actual)
        if key not in self.proof_cache:
            paths = self.provenance_paths(recorded, actual)
            sources: list[Source] = []
            for path in paths:
                sources.extend(path)
            self.proof_cache[key] = tuple(dict.fromkeys(sources))
        return self.proof_cache[key]

    def releases(self, recorded: Part) -> frozenset[tuple[Part, tuple[Source, ...]]]:
        if recorded not in self.release_cache:
            rows = _answer_rows(
                pyDatalog.ask(f"release_fact({_q(recorded)},Actual)")
            )
            self.release_cache[recorded] = frozenset(
                row[-1] for row in rows if row
            )
        return frozenset(
            (actual, self.provenance(recorded, actual))
            for actual in self.release_cache[recorded]
        )


def _q(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return repr(value)
    return repr(value)


def _proof_path(proof: Any) -> tuple[Source, ...]:
    if proof is None:
        return ()
    if isinstance(proof, tuple):
        if len(proof) == 3 and proof[0] == "transfer":
            return (proof,)
        if len(proof) == 2:
            return _proof_path(proof[0]) + _proof_path(proof[1])
        result: tuple[Source, ...] = ()
        for item in proof:
            result += _proof_path(item)
        return result
    return (proof,)


def _flatten_proof(proof: Any) -> tuple[Source, ...]:
    if proof is None:
        return ()
    if isinstance(proof, tuple):
        if len(proof) == 2 and proof[0] == "transfer" and not isinstance(proof[1], tuple):
            return (proof,)
        result: list[Source] = []
        for item in proof:
            result.extend(_flatten_proof(item))
        return tuple(result)
    return (proof,)


@dataclass(frozen=True)
class ChoiceSet:
    choice_id: ChoiceId
    copies: Mapping[CopyId, Sequence[Mapping[str, Any] | Line]]

    def copy_ids(self) -> tuple[CopyId, ...]:
        return tuple(self.copies)


class Reconciler:
    """Solve shared transcript choices against signed integer totals."""

    def __init__(
        self,
        facts: Iterable[Mapping[str, Any] | Fact] = (),
        choices: Iterable[ChoiceSet | Mapping[str, Any]] | None = None,
        signed_totals: Iterable[SignedTotal | Mapping[str, Any]] = (),
        transfers: Iterable[tuple[Any, ...]] = (),
        withheld: Iterable[Any] = (),
        base_totals: Mapping[Department, int] | None = None,
        transfer_index: TransferIndex | None = None,
        extra_rules: str = "",
        *,
        choice_sets: Iterable[ChoiceSet | Mapping[str, Any]] | None = None,
        copies: Iterable[ChoiceSet | Mapping[str, Any]] | None = None,
        candidate_copies: Iterable[ChoiceSet | Mapping[str, Any]] | None = None,
        receipts: Iterable[SignedTotal | Mapping[str, Any]] | None = None,
        signed_receipts: Iterable[SignedTotal | Mapping[str, Any]] | None = None,
        transfer_facts: Iterable[tuple[Any, ...]] | None = None,
        opening_totals: Mapping[Department, int] | None = None,
    ) -> None:
        selected_choices = (
            choice_sets
            if choice_sets is not None
            else copies
            if copies is not None
            else candidate_copies
            if candidate_copies is not None
            else choices
            if choices is not None
            else ()
        )
        selected_receipts = (
            signed_receipts
            if signed_receipts is not None
            else receipts
            if receipts is not None
            else signed_totals
        )
        selected_transfers = transfer_facts if transfer_facts is not None else transfers
        selected_base = opening_totals if opening_totals is not None else base_totals
        selected_choices = _normalize_choice_sets(selected_choices)
        selected_receipts = _normalize_signed_totals(selected_receipts)
        self.facts = tuple(facts)
        datalog_rules = "\n".join(
            part
            for part in (extra_rules, _facts_to_datalog(self.facts))
            if part
        )
        self.choice_sets = []
        for index, item in enumerate(selected_choices, start=1):
            choice_set = _coerce_choice_set(item)
            if choice_set.choice_id is None:
                choice_set = replace(choice_set, choice_id=f"choice-{index}")
            self.choice_sets.append(choice_set)
        self.signed_totals = [_coerce_signed_total(item) for item in selected_receipts]
        self.base_totals = dict(selected_base or {})
        self.transfer_index = transfer_index or TransferIndex(
            selected_transfers, withheld, extra_rules=datalog_rules
        )
        self.withheld = set(withheld)
        self._prepared = False
        self.copy_lines: dict[tuple[ChoiceId, CopyId], tuple[Line, ...]] = {}
        self.copy_deltas: dict[tuple[ChoiceId, CopyId], dict[Department, int]] = {}
        self.choice_ids = tuple(item.choice_id for item in self.choice_sets)
        self.default_copy: dict[ChoiceId, CopyId] = {}
        self.alternative_copy: dict[ChoiceId, CopyId] = {}

    def prepare(self) -> "Reconciler":
        if self._prepared:
            return self

        distinct_entry_parts = set()
        for choice_set in self.choice_sets:
            for raw_lines in choice_set.copies.values():
                for raw in raw_lines:
                    if isinstance(raw, Line):
                        distinct_entry_parts.add(raw.part)
                    elif isinstance(raw, Mapping):
                        part = _first_key_value(
                            raw, "part", "material", "part_number", "material_number", "sku", "料号"
                        )
                        if part is not None:
                            distinct_entry_parts.add(part)
        if len(distinct_entry_parts) >= 4:
            self.transfer_index.precompute_all()

        for choice_set in self.choice_sets:
            ids = choice_set.copy_ids()
            if len(ids) != 2:
                raise ReconciliationError(
                    f"choice {choice_set.choice_id!r} must have exactly two copies"
                )
            first, second = ids
            self.default_copy[choice_set.choice_id] = first
            self.alternative_copy[choice_set.choice_id] = second
            for copy_id in ids:
                lines = self._materialize_lines(choice_set.choice_id, copy_id, choice_set.copies[copy_id])
                self.copy_lines[(choice_set.choice_id, copy_id)] = lines
        self._load_aggregate_deltas()
        self._prepared = True
        return self

    def _load_aggregate_deltas(self) -> None:
        suffix = str(id(self)).replace("-", "_")
        line_pred = f"recon_line_{suffix}"
        total_fn = f"recon_total_{suffix}"
        clauses: list[str] = []
        for key in self.copy_lines:
            self.copy_deltas[key] = {}
        for (choice_id, copy_id), lines in self.copy_lines.items():
            for line in lines:
                if line.withheld or line.value == 0:
                    continue
                clauses.append(
                    f"+{line_pred}({_q(choice_id)},{_q(copy_id)},"
                    f"{_q(line.department)},{_q(line.part)},{_q(line.value)},{_q(line.source)})"
                )
        clauses.append(
            f"({total_fn}[C,K,D] == sum_(V, for_each=(P,S))) <= "
            f"{line_pred}(C,K,D,P,V,S)"
        )
        pyDatalog.load("\n".join(clauses))
        answer = pyDatalog.ask(f"{total_fn}[C,K,D] == V")
        for choice_id, copy_id, department, value in _answer_rows(answer):
            key = (choice_id, copy_id)
            self.copy_deltas.setdefault(key, {})[department] = int(value)

    def _materialize_lines(
        self, choice_id: ChoiceId, copy_id: CopyId, raw_lines: Sequence[Mapping[str, Any] | Line]
    ) -> tuple[Line, ...]:
        merged: dict[tuple[Department, Part, Source], Line] = {}
        for raw in raw_lines:
            line = _coerce_raw_line(raw, choice_id, copy_id)
            recorded_part = line.part
            actual_parts = self.transfer_index.actual_parts(recorded_part)
            for actual in actual_parts:
                is_withheld = (
                    actual in self.transfer_index.withheld
                    or actual in self.withheld
                    or line.withheld
                )
                value = 0 if is_withheld else line.value
                release_sources = line.release_sources
                release_paths: tuple[tuple[Source, ...], ...] = ()
                release_facts = () if is_withheld else ((recorded_part, actual),)
                key = (line.department, actual, line.source)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = Line(
                        department=line.department,
                        part=actual,
                        value=value,
                        source=line.source,
                        release_sources=release_sources,
                        withheld=is_withheld,
                        recorded_part=recorded_part,
                        release_facts=release_facts,
                        release_paths=release_paths,
                    )
                else:
                    merged[key] = Line(
                        department=existing.department,
                        part=existing.part,
                        value=existing.value,
                        source=existing.source,
                        release_sources=tuple(
                            dict.fromkeys((*existing.release_sources, *release_sources))
                        ),
                        withheld=existing.withheld or is_withheld,
                        recorded_part=existing.recorded_part or recorded_part,
                        release_facts=tuple(
                            dict.fromkeys((*existing.release_facts, *release_facts))
                        ),
                        release_paths=tuple(
                            dict.fromkeys((*existing.release_paths, *release_paths))
                        ),
                    )
        return tuple(sorted(merged.values(), key=lambda x: (str(x.department), str(x.part), str(x.source))))

    def reconcile(
        self,
        department: Department,
        interval: Sequence[int] | int | None = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
    ) -> ReconciliationResult:
        self.prepare()
        low, high = _normalize_interval(interval, lo, hi)
        constraints = [(item.department, item.total, item.receipt) for item in self.signed_totals]

        solved = self._solve_target_totals(department, constraints, low, high)
        if solved is None:
            return ReconciliationResult(
                department=department,
                interval=(low, high),
                count=0,
                minimum_reachable=None,
                minimum_unreachable=low,
                witness=None,
                witness_total=None,
                included_lines=(),
                possible_totals=(),
                conflicts=self._conflicts(constraints),
                explanation={
                    "reason": "no shared choice satisfies all signed totals",
                    "signed_totals": [
                        {"department": d, "total": t, "receipt": r} for d, t, r in constraints
                    ],
                    "facts": [_fact_repr(f) for f in self.facts],
                },
            )

        target_base, total_witnesses = solved
        possible = tuple(sorted(total_witnesses))
        min_reachable = possible[0] if possible else None
        min_unreachable = _first_missing(low, high, possible)
        witness_bits = total_witnesses.get(min_reachable) if min_reachable is not None else None
        witness = self._assignment_from_bits(witness_bits) if witness_bits is not None else None
        lines = (
            self.included_lines(department, witness)
            if witness is not None
            else ()
        )
        return ReconciliationResult(
            department=department,
            interval=(low, high),
            count=len(possible),
            minimum_reachable=min_reachable,
            minimum_unreachable=min_unreachable,
            witness=witness,
            witness_total=min_reachable,
            included_lines=lines,
            possible_totals=possible,
            explanation={
                "distinct_totals": len(possible),
                "shared_transfer_facts": len(self.transfer_index.transfer_facts),
                "evaluation": "shared pyDatalog closure + memoized integer DP",
            },
        )

    def _assignment_from_bits(self, bits: Sequence[int]) -> dict[ChoiceId, CopyId]:
        return {
            choice_id: self.alternative_copy[choice_id]
            if bits[i]
            else self.default_copy[choice_id]
            for i, choice_id in enumerate(self.choice_ids)
        }

    def _solve_target_totals(
        self,
        department: Department,
        constraints: Sequence[tuple[Department, int, Any]],
        low: int,
        high: int,
    ) -> tuple[int, dict[int, tuple[int, ...]]] | None:
        variables = list(self.choice_ids)
        n = len(variables)
        if n * 2 + 200 > sys.getrecursionlimit():
            sys.setrecursionlimit(n * 2 + 200)
        default = [self.default_copy[c] for c in variables]
        alternative = [self.alternative_copy[c] for c in variables]

        residuals: list[int] = []
        coefficients: list[dict[int, int]] = []
        for dept, signed, _ in constraints:
            constant = self.base_totals.get(dept, 0) + sum(
                self.copy_deltas[(c, default[i])].get(dept, 0)
                for i, c in enumerate(variables)
            )
            coeff: dict[int, int] = {}
            for i, c in enumerate(variables):
                delta = (
                    self.copy_deltas[(c, alternative[i])].get(dept, 0)
                    - self.copy_deltas[(c, default[i])].get(dept, 0)
                )
                if delta:
                    coeff[i] = delta
            residuals.append(signed - constant)
            coefficients.append(coeff)

        target_base = self.base_totals.get(department, 0) + sum(
            self.copy_deltas[(c, default[i])].get(department, 0)
            for i, c in enumerate(variables)
        )
        target_coeff = [
            self.copy_deltas[(c, alternative[i])].get(department, 0)
            - self.copy_deltas[(c, default[i])].get(department, 0)
            for i, c in enumerate(variables)
        ]

        suffix_min = [[0] * (n + 1) for _ in coefficients]
        suffix_max = [[0] * (n + 1) for _ in coefficients]
        for j, coeff in enumerate(coefficients):
            for i in range(n - 1, -1, -1):
                value = coeff.get(i, 0)
                suffix_min[j][i] = suffix_min[j][i + 1] + min(0, value)
                suffix_max[j][i] = suffix_max[j][i + 1] + max(0, value)

        from functools import lru_cache

        @lru_cache(maxsize=None)
        def dp(i: int, residual: tuple[int, ...]) -> dict[int, tuple[int, ...]]:
            if i == n:
                return {0: ()} if all(x == 0 for x in residual) else {}

            for j, value in enumerate(residual):
                if not (suffix_min[j][i] <= value <= suffix_max[j][i]):
                    return {}

            answers: dict[int, tuple[int, ...]] = {}
            for bit in (0, 1):
                next_residual = tuple(
                    residual[j] - coefficients[j].get(i, 0) * bit
                    for j in range(len(residual))
                )
                for j, value in enumerate(next_residual):
                    if not (suffix_min[j][i + 1] <= value <= suffix_max[j][i + 1]):
                        break
                else:
                    for suffix_total, suffix_bits in dp(i + 1, next_residual).items():
                        total = target_coeff[i] * bit + suffix_total
                        if total not in answers:
                            answers[total] = (bit,) + suffix_bits
            return answers

        witnesses = dp(0, tuple(residuals))
        if not witnesses:
            return None
        totals = {
            target_base + suffix_total: bits
            for suffix_total, bits in witnesses.items()
            if low <= target_base + suffix_total <= high
        }
        return target_base, totals

    def total_for(self, department: Department, assignment: Mapping[ChoiceId, CopyId]) -> int:
        total = self.base_totals.get(department, 0)
        for choice_id in self.choice_ids:
            copy_id = assignment[choice_id]
            total += self.copy_deltas[(choice_id, copy_id)].get(department, 0)
        return total

    def included_lines(
        self, department: Department, assignment: Mapping[ChoiceId, CopyId]
    ) -> tuple[Line, ...]:
        lines: list[Line] = []
        for choice_id in self.choice_ids:
            copy_id = assignment[choice_id]
            lines.extend(
                line
                for line in self.copy_lines[(choice_id, copy_id)]
                if line.department == department and not line.withheld and line.value != 0
            )
        deduped: dict[tuple[Department, Part, int, Source], Line] = {}
        for line in lines:
            key = (line.department, line.part, line.value, line.source)
            deduped.setdefault(key, line)
        ordered = sorted(
            deduped.values(), key=lambda x: (str(x.part), str(x.source))
        )
        enriched: list[Line] = []
        for line in ordered:
            if line.recorded_part is None:
                enriched.append(line)
                continue
            paths = self.transfer_index.provenance_paths(line.recorded_part, line.part)
            sources: list[Source] = []
            for path in paths:
                sources.extend(path)
            enriched.append(
                replace(
                    line,
                    release_paths=paths,
                    release_sources=tuple(
                        dict.fromkeys((*line.release_sources, *sources))
                    ),
                )
            )
        return tuple(enriched)

    def _conflicts(self, constraints) -> tuple[dict[str, Any], ...]:
        """Return signed receipts and original facts useful for diagnosis."""
        conflicts: list[dict[str, Any]] = []
        if len(self.choice_ids) <= 20:
            for bits in _bit_combinations(len(self.choice_ids)):
                assignment = {
                    c: self.alternative_copy[c] if bits[i] else self.default_copy[c]
                    for i, c in enumerate(self.choice_ids)
                }
                bad = []
                for dept, total, receipt in constraints:
                    actual = self.total_for(dept, assignment)
                    if actual != total:
                        bad.append(
                            {
                                "department": dept,
                                "signed_total": total,
                                "computed_total": actual,
                                "receipt": receipt,
                            }
                        )
                if bad:
                    conflicts.append({"choice": assignment, "mismatches": bad})
                if len(conflicts) >= 8:
                    break
        if not conflicts:
            conflicts.append(
                {
                    "mismatches": [
                        {"department": d, "signed_total": t, "receipt": r}
                        for d, t, r in constraints
                    ]
                }
            )
        return tuple(conflicts)


def _bit_combinations(n: int):
    if n == 0:
        yield ()
        return
    for r in range(n + 1):
        for ones in combinations(range(n), r):
            bits = [0] * n
            for i in ones:
                bits[i] = 1
            yield tuple(bits)


def _first_missing(low: int, high: int, values: Sequence[int]) -> int | None:
    value_set = set(values)
    if low not in value_set:
        return low
    expected = low
    for value in values:
        if value < expected:
            continue
        if value == expected:
            expected += 1
        elif value > expected:
            return expected
    return expected if expected <= high else None


def _normalize_interval(interval, lo, hi) -> tuple[int, int]:
    if interval is not None:
        if isinstance(interval, int):
            low = high = interval
        else:
            values = tuple(interval)
            if len(values) != 2:
                raise ReconciliationError("interval must contain two integers")
            low, high = int(values[0]), int(values[1])
    elif lo is not None and hi is not None:
        low, high = int(lo), int(hi)
    else:
        raise ReconciliationError("an integer interval or lo/hi is required")
    if low > high:
        low, high = high, low
    return low, high


def _normalize_choice_sets(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, ChoiceSet):
        return (value,)
    if isinstance(value, Mapping):
        if "copies" in value or "candidates" in value or "alternatives" in value:
            return (value,)
        return tuple(
            {
                "choice_id": choice_id,
                **(
                    body
                    if isinstance(body, Mapping)
                    and any(key in body for key in ("copies", "candidates", "alternatives", "options"))
                    else {"copies": body}
                ),
            }
            for choice_id, body in value.items()
        )
    return tuple(value)


def _normalize_signed_totals(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Mapping):
        return tuple(
            {"department": department, "total": total}
            if not isinstance(total, Mapping)
            else {"department": department, **total}
            for department, total in value.items()
        )
    return tuple(value)


def _coerce_choice_set(value: ChoiceSet | Mapping[str, Any]) -> ChoiceSet:
    if isinstance(value, ChoiceSet):
        return value
    choice_id = _first_key_value(
        value, "id", "choice_id", "place", "point", "slot", "case_id", "处", "位置",
    )
    copies = _first_key_value(
        value,
        "copies", "candidates", "alternatives", "options", "transcripts", "抄件",
    )
    if copies is None:
        raise ReconciliationError(f"choice missing copies: {value!r}")
    if isinstance(copies, Mapping):
        normalized = {
            copy_id: _as_tuple(lines) for copy_id, lines in copies.items()
        }
    else:
        normalized = {i: _as_tuple(lines) for i, lines in enumerate(copies)}
    return ChoiceSet(choice_id=choice_id, copies=normalized)


def _coerce_signed_total(value: SignedTotal | Mapping[str, Any]) -> SignedTotal:
    if isinstance(value, SignedTotal):
        return value
    department = _first_key_value(value, "department", "dept", "部门")
    total = _first_key_value(
        value, "total", "signed_total", "official_total", "amount", "合计", "已签合计"
    )
    receipt = _first_key_value(value, "receipt", "receipt_id", "id", "回执")
    return SignedTotal(department=department, total=int(total), receipt=receipt)


def _coerce_raw_line(
    value: Mapping[str, Any] | Line, choice_id: ChoiceId, copy_id: CopyId
) -> Line:
    if isinstance(value, Line):
        return value
    department = _first_key_value(value, "department", "dept", "bumen", "部门")
    part = _first_key_value(
        value,
        "part", "material", "part_number", "material_number", "sku", "料号",
    )
    amount = _first_key_value(
        value, "value", "amount", "qty", "quantity", "sum", "数量", "金额"
    )
    source = _first_key_value(
        value, "source", "source_id", "provenance", "receipt_id", "id", "出处"
    )
    if source is None:
        source = (choice_id, copy_id, part)
    release_sources = _as_tuple(
        _first_key_value(value, "release_sources", "provenance_sources", "sources")
    )
    withheld = bool(
        _first_key_value(
            value,
            "withheld", "withhold", "deduction", "deducted", "hold", "held",
            "扣料", "扣留",
        )
    )
    return Line(department, part, int(amount or 0), source, release_sources, withheld)


def _first_key_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _first_key(mapping: Mapping[str, Any], *keys: str) -> Any:
    value = _first_key_value(mapping, *keys)
    if value is not None:
        return value
    return id(mapping)


def _facts_to_datalog(facts: Iterable[Any]) -> str:
    clauses: list[str] = []
    for fact in facts:
        if isinstance(fact, str):
            clauses.append(fact)
        elif isinstance(fact, Mapping):
            predicate = fact.get("predicate", fact.get("pred"))
            args = fact.get("args", fact.get("arguments", ()))
            if predicate and isinstance(args, (list, tuple)):
                clauses.append(
                    f"+{predicate}({','.join(_q(arg) for arg in args)})"
                )
        elif isinstance(fact, (tuple, list)) and fact:
            predicate = fact[0]
            if isinstance(predicate, str) and predicate.replace("_", "").isalnum() and predicate[0].islower():
                clauses.append(
                    f"+{predicate}({','.join(_q(arg) for arg in fact[1:])})"
                )
    return "\n".join(clauses)


def _fact_repr(fact: Any) -> Any:
    if isinstance(fact, Mapping):
        return dict(fact)
    return fact


def reconcile_receipts(
    data: Mapping[str, Any] | None = None,
    *,
    facts: Iterable[Any] = (),
    choices: Iterable[Any] = (),
    choice_sets: Iterable[Any] | None = None,
    copies: Iterable[Any] | None = None,
    signed_totals: Iterable[Any] = (),
    receipts: Iterable[Any] | None = None,
    transfers: Iterable[Any] = (),
    transfer_facts: Iterable[Any] | None = None,
    withheld: Iterable[Any] = (),
    base_totals: Mapping[Any, int] | None = None,
    department: Any = None,
    target_department: Any = None,
    missing_department: Any = None,
    interval: Sequence[int] | int | None = None,
    integer_interval: Sequence[int] | int | None = None,
    lo: int | None = None,
    hi: int | None = None,
    low: int | None = None,
    high: int | None = None,
    extra_rules: str = "",
    as_dict: bool = False,
) -> ReconciliationResult | dict[str, Any]:
    """Reconcile shared binary transcript choices against signed totals.

    A mapping input may use the same keyword names.  The returned object also
    supports dictionary-style access; ``as_dict=True`` returns plain JSON-ready
    data.
    """
    kwargs = {
        "facts": facts,
        "choices": choices,
        "choice_sets": choice_sets,
        "copies": copies,
        "signed_totals": signed_totals,
        "receipts": receipts,
        "transfers": transfers,
        "transfer_facts": transfer_facts,
        "withheld": withheld,
        "base_totals": base_totals,
        "department": department,
        "target_department": target_department,
        "missing_department": missing_department,
        "interval": interval,
        "integer_interval": integer_interval,
        "lo": lo,
        "hi": hi,
        "low": low,
        "high": high,
        "extra_rules": extra_rules,
    }
    if data is not None:
        merged = dict(data)
        for key, value in kwargs.items():
            if value not in (None, ()):
                merged.setdefault(key, value)
        kwargs = merged

    def pick(*names, default=None):
        for name in names:
            if name in kwargs and kwargs[name] is not None:
                return kwargs[name]
        return default

    selected_facts = pick("facts", "original_facts", "base_facts", default=())
    selected_choices = pick("choice_sets", "choices", "copies", "candidate_copies", default=())
    selected_receipts = pick("signed_totals", "receipts", "signed_receipts", default=())
    selected_transfers = pick("transfer_facts", "transfers", default=())
    selected_withheld = pick("withheld", "withheld_parts", "holds", default=())
    selected_base = pick("base_totals", "opening_totals", default=None)
    selected_department = pick(
        "department", "target_department", "missing_department", "dept", "missing_dept"
    )
    selected_interval = pick("integer_interval", "interval", "range")
    selected_lo = pick("lo", "low", "minimum")
    selected_hi = pick("hi", "high", "maximum")
    selected_rules = pick("extra_rules", "rules", "datalog_rules", default="")
    as_dict = as_dict or bool(pick("as_dict", "json", default=False))

    reconciler = Reconciler(
        facts=selected_facts,
        choices=selected_choices,
        signed_totals=selected_receipts,
        transfers=selected_transfers,
        withheld=selected_withheld,
        base_totals=selected_base,
        extra_rules=selected_rules if isinstance(selected_rules, str) else "\n".join(selected_rules),
    )
    result = reconciler.reconcile(
        selected_department,
        selected_interval,
        lo=selected_lo,
        hi=selected_hi,
    )
    return result.as_dict() if as_dict else result


def solve_reconciliation(*args: Any, **kwargs: Any):
    """Flexible entry point accepting either a mapping or common positional args."""
    positional_names = (
        "facts",
        "choices",
        "signed_totals",
        "department",
        "interval",
        "transfers",
        "withheld",
        "base_totals",
    )
    if args:
        if len(args) == 1 and isinstance(args[0], Mapping):
            return reconcile_receipts(args[0], **kwargs)
        for name, value in zip(positional_names, args):
            kwargs.setdefault(name, value)
    return reconcile_receipts(**kwargs)


reconcile = solve_reconciliation
solve = solve_reconciliation
analyze = solve_reconciliation
reconcile_day = solve_reconciliation
daily_close = solve_reconciliation


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Reconcile pyDatalog receipts.")
    parser.add_argument("input", nargs="?", help="JSON input file; stdin if omitted")
    parser.add_argument("--department")
    parser.add_argument("--lo", type=int)
    parser.add_argument("--hi", type=int)
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = json.load(sys.stdin)
    if args.department is not None:
        data["department"] = args.department
    if args.lo is not None:
        data["lo"] = args.lo
    if args.hi is not None:
        data["hi"] = args.hi
    result = reconcile_receipts(data, as_dict=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


ReceiptReconciler = Reconciler
ReceiptReconciliation = Reconciler
ReconciliationSolver = Reconciler
DailyReconciler = Reconciler
DailySettlement = Reconciler
AccountReconciler = Reconciler
check_receipts = reconcile_receipts
reconcile_accounts = solve_reconciliation
find_possible_totals = solve_reconciliation

Reconciler.solve = Reconciler.reconcile  # type: ignore[attr-defined]
Reconciler.run = Reconciler.reconcile  # type: ignore[attr-defined]
Reconciler.analyze = Reconciler.reconcile  # type: ignore[attr-defined]
Reconciler.reconcile_department = Reconciler.reconcile  # type: ignore[attr-defined]
