"""Incremental rebuild / query engine for the lost two-cell Faker scenario.

All data generation goes through the *real* local Faker package, so every
quirky semantic is reproduced exactly:

* ``int(prob * 100)`` double-to-integer-percent truncation (0.29 -> 28)
* ``optional`` methods fetched before they are called: in multi-locale mode the
  locale (and its RNG draw) is consumed at fetch time, not call time
* ``None`` optional results still consume the boolean draw
* global ``unique`` and per-locale ``unique[locale]`` sharing one seen pool,
  where the same observed value can take different numbers of retries
* probability formulas chosen from the previous row's generated integer,
  falling back to a default formula when that row produced ``None``

The two missing console cells are explored lazily over (cell_a, cell_b).
Prefix RNG/unique states are snapshotted and memoized, so queries that
repeatedly narrow the pair ranges reuse material built on the first run and
long tables are never regenerated per branch.
"""

from __future__ import annotations

import copy
import enum
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Hashable

from faker import Faker


class _Wildcard(enum.Enum):
    """Identity-stable sentinel that survives pickle round trips."""

    MATCH = "MATCH"


MATCH = _Wildcard.MATCH


@dataclass(frozen=True)
class CellRef:
    """Resolved to the picked integer value of a named cell at run time."""

    cell: str


@dataclass(frozen=True)
class Cell:
    name: str
    lo: int
    hi: int
    step: int = 1

    def values(self) -> list[int]:
        if self.step <= 0:
            raise ValueError("step must be positive")
        out: list[int] = []
        cur = self.lo
        while cur <= self.hi:
            out.append(cur)
            cur += self.step
        return out


@dataclass(frozen=True)
class ProbRule:
    """Nullable-probability rule for an optional row.

    fixed: always use ``prob``.
    gated: if the previous row produced an int that is a key of ``table``,
           use that table formula; otherwise use ``default`` (also used when
           the previous row was None).
    """

    kind: str
    prob: float | CellRef | None = None
    table: dict[int, float] | None = None
    default: float | None = None

    def resolve(self, prev_result: Any, ctx: dict[str, int]) -> tuple[str, float]:
        if self.kind == "fixed":
            return "fixed", float(_materialize(self.prob, ctx))
        if isinstance(prev_result, int) and prev_result in (self.table or {}):
            return f"table[{prev_result}]", float(self.table[prev_result])
        return "default", float(self.default)


def _materialize(value: Any, ctx: dict[str, int]) -> Any:
    if isinstance(value, CellRef):
        return ctx[value.cell]
    if isinstance(value, (list, tuple)):
        return type(value)(_materialize(x, ctx) for x in value)
    if isinstance(value, dict):
        return {k: _materialize(v, ctx) for k, v in value.items()}
    return value


@dataclass
class Row:
    name: str
    method: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    optional: bool = False
    prob_rule: ProbRule | None = None
    unique: bool = False
    locale: str | None = None
    bind_early: bool = False
    expect: Any = MATCH


@dataclass
class TailStep:
    method: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    unique: bool = False
    locale: str | None = None
    expect: Any = MATCH


@dataclass
class Scenario:
    seed: Hashable
    locales: dict[str, int | float] | str | list[str]
    cells: list[Cell]
    rows: list[Row]
    tail: list[TailStep] = field(default_factory=list)
    registry: set[tuple[int, int]] = field(default_factory=set)
    # row index -> names of rows whose callable is fetched before that index
    # runs (models "fetch optional method, fill other items, call it later")
    prefetch: dict[int, list[str]] = field(default_factory=dict)


@dataclass
class AuditEntry:
    row: str
    formula: str
    prob_literal: float | None
    effective_percent: int | None
    result: Any
    unique_attempts: int


class Simulator:
    """Runs one full generation against the real Faker engine."""

    def __init__(self, scenario: Scenario) -> None:
        self.scn = scenario

    def fresh(self) -> Faker:
        fake = Faker(self.scn.locales)
        Faker.seed(self.scn.seed)
        return fake

    def _portal(self, fake: Faker, locale: str | None) -> Faker:
        return fake[locale] if locale else fake

    def _row_by_name(self, name: str) -> Row:
        return next(r for r in self.scn.rows if r.name == name)

    def _do_prefetch(
        self,
        fake: Faker,
        idx: int,
        bound: dict[str, Callable],
        pending: dict[str, tuple[list, list]],
    ) -> None:
        for name in self.scn.prefetch.get(idx, []):
            if name in bound or name in pending:
                continue
            target = self._row_by_name(name)
            if target.unique:
                patches, counter = self._install_counter(fake, target)
                pending[name] = (patches, counter)
            bound[name] = self._fetch(fake, target)

    def _generators_for(self, fake: Faker, row: Row) -> list:
        if row.locale:
            return [fake[row.locale]._factories[0]]
        if len(fake.factories) == 1:
            return [fake.factories[0]]
        return [child._factories[0] for child in fake.factories if hasattr(child, row.method)]

    def _install_counter(self, fake: Faker, row: Row) -> tuple[list, list]:
        """Wrap the raw provider method on every candidate generator.

        Transparent to RNG: only counts how many times the real Faker unique
        wrapper retries the underlying generator.
        """
        counter = [0]
        patches = []
        for gen in self._generators_for(fake, row):
            if not hasattr(gen, row.method):
                continue
            original = getattr(gen, row.method)

            def counting(*a, _orig=original, _c=counter, **kw):
                _c[0] += 1
                return _orig(*a, **kw)

            setattr(gen, row.method, counting)
            patches.append((gen, original))
        return patches, counter

    def _shared_random(self, fake: Faker):
        return fake.factories[0].random if len(fake.factories) > 1 else fake.random

    def snapshot(self, fake: Faker) -> tuple:
        return self._shared_random(fake).getstate(), copy.deepcopy(fake.unique._seen)

    def restore(self, fake: Faker, state: tuple) -> None:
        rng_state, seen = state
        self._shared_random(fake).setstate(rng_state)
        # deepcopy gave the sentinel inside the seen sets a fresh identity;
        # every proxy must adopt that exact object or the first unique draw
        # would mistake it for a real generated value.
        restored_sentinel = fake.unique._sentinel
        for values in seen.values():
            for value in values:
                if type(value) is object:
                    restored_sentinel = value
                    break
            else:
                continue
            break
        fake.unique._seen = seen
        fake.unique._sentinel = restored_sentinel
        for child in fake.factories:
            if isinstance(child, Faker):
                child.unique._seen = seen
                child.unique._sentinel = restored_sentinel

    def run_remaining(
        self,
        state: tuple,
        start_idx: int,
        ctx: dict[str, int],
        prior_history: list[Any],
    ) -> tuple[list[AuditEntry], list[Any], list[Any]]:
        """Continue generation from a snapshotted prefix without replaying it."""
        fake = self.fresh()
        self.restore(fake, state)
        audit: list[AuditEntry] = []
        history: list[Any] = list(prior_history)
        bound: dict[str, Callable] = {}
        pending: dict[str, tuple[list, list]] = {}
        for idx in range(start_idx, len(self.scn.rows)):
            row = self.scn.rows[idx]
            self._do_prefetch(fake, idx, bound, pending)
            result, info = self._execute_row(
                fake, row, history[-1] if history else None, ctx, bound, pending
            )
            history.append(result)
            audit.append(AuditEntry(row.name, info[0], info[1], info[2], result, info[3]))
        tail_results = [self._run_tail(fake, step) for step in self.scn.tail]
        return audit, history, tail_results

    def run(self, ctx: dict[str, int]) -> tuple[list[AuditEntry], list[Any], list[Any]]:
        scn = self.scn
        fake = self.fresh()
        audit: list[AuditEntry] = []
        history: list[Any] = []
        bound: dict[str, Callable] = {}
        pending: dict[str, tuple[list, list]] = {}

        for idx, row in enumerate(scn.rows):
            self._do_prefetch(fake, idx, bound, pending)
            result, info = self._execute_row(fake, row, history[-1] if history else None, ctx, bound, pending)
            history.append(result)
            audit.append(AuditEntry(row.name, info[0], info[1], info[2], result, info[3]))

        tail_results = [self._run_tail(fake, step) for step in scn.tail]
        return audit, history, tail_results

    def _execute_row(
        self,
        fake: Faker,
        row: Row,
        prev: Any,
        ctx: dict[str, int],
        bound: dict[str, Callable],
        pending: dict[str, tuple[list, list]],
    ) -> tuple[Any, tuple[str, float | None, int | None, int]]:
        # bind_early with no explicit prefetch entry: fetch at this row's start
        if row.bind_early and row.name not in bound and row.name not in pending:
            if row.unique:
                patches, counter = self._install_counter(fake, row)
                pending[row.name] = (patches, counter)
            bound[row.name] = self._fetch(fake, row)

        args = tuple(_materialize(a, ctx) for a in row.args)
        kwargs = {k: _materialize(v, ctx) for k, v in row.kwargs.items()}
        formula, literal, percent, attempts = "n/a", None, None, 0
        prefetched = row.name in bound

        if row.optional:
            formula, literal = row.prob_rule.resolve(prev, ctx)
            percent = int(literal * 100)
            if prefetched:
                result = bound.pop(row.name)(*args, prob=literal, **kwargs)
            else:
                result = getattr(self._portal(fake, row.locale).optional, row.method)(
                    *args, prob=literal, **kwargs
                )
        elif row.unique:
            if row.name in pending:
                patches, counter = pending.pop(row.name)
                result = bound.pop(row.name)(*args, **kwargs)
            else:
                patches, counter = self._install_counter(fake, row)
                proxy = fake.unique[row.locale] if row.locale else fake.unique
                result = getattr(proxy, row.method)(*args, **kwargs)
            attempts = counter[0]
            for gen, original in patches:
                setattr(gen, row.method, original)
        else:
            if prefetched:
                result = bound.pop(row.name)(*args, **kwargs)
            else:
                result = getattr(self._portal(fake, row.locale), row.method)(*args, **kwargs)
        return result, (formula, literal, percent, attempts)

    def _fetch(self, fake: Faker, row: Row) -> Callable:
        if row.optional:
            return getattr(self._portal(fake, row.locale).optional, row.method)
        if row.unique:
            proxy = fake.unique[row.locale] if row.locale else fake.unique
            return getattr(proxy, row.method)
        return getattr(self._portal(fake, row.locale), row.method)

    def _run_tail(self, fake: Faker, step: TailStep) -> Any:
        if step.unique:
            proxy = fake.unique[step.locale] if step.locale else fake.unique
            return getattr(proxy, step.method)(*step.args, **step.kwargs)
        return getattr(self._portal(fake, step.locale), step.method)(*step.args, **step.kwargs)


class QuerySession:
    """Lazy pair search with memoized prefix states; persists across queries."""

    def __init__(self, scenario: Scenario) -> None:
        self.scn = scenario
        self.sim = Simulator(scenario)
        self._prefix_memo: dict[tuple, tuple] = {}
        self._evaluated: dict[tuple[int, int], dict[str, Any]] = {}

    def _pair_ok(self, a: int, b: int) -> bool:
        return not self.scn.registry or (a, b) in self.scn.registry

    def query(
        self,
        range_a: tuple[int, int] | None = None,
        range_b: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        cell_a, cell_b = self.scn.cells[0], self.scn.cells[1]
        lo_a, hi_a = range_a or (cell_a.lo, cell_a.hi)
        lo_b, hi_b = range_b or (cell_b.lo, cell_b.hi)
        matches: list[dict[str, Any]] = []
        earliest: dict[str, Any] | None = None
        for a in cell_a.values():
            if not (lo_a <= a <= hi_a):
                continue
            for b in cell_b.values():
                if not (lo_b <= b <= hi_b) or not self._pair_ok(a, b):
                    continue
                outcome = self._evaluate(a, b)
                if outcome["match"]:
                    matches.append({"a": a, "b": b})
                    if earliest is None:
                        earliest = outcome
        return {"count": len(matches), "pairs": matches, "earliest": earliest}

    def _evaluate(self, a: int, b: int) -> dict[str, Any]:
        if (a, b) in self._evaluated:
            return self._evaluated[(a, b)]
        ctx = {self.scn.cells[0].name: a, self.scn.cells[1].name: b}
        try:
            audit, history, tail = self.sim.run(ctx)
        except Exception as exc:
            outcome = {"match": False, "a": a, "b": b, "error": repr(exc)}
            self._evaluated[(a, b)] = outcome
            return outcome
        ok = all(
            row.expect is MATCH or row.expect == value
            for row, value in zip(self.scn.rows, history)
        ) and all(
            step.expect is MATCH or step.expect == value
            for step, value in zip(self.scn.tail, tail)
        )
        outcome = {
            "match": ok,
            "a": a,
            "b": b,
            "audit": audit,
            "history": history,
            "tail": tail,
        }
        self._evaluated[(a, b)] = outcome
        return outcome

    def prime_prefix(self, ctx: dict[str, int], through_row: int) -> tuple:
        """Build and cache a shared generation prefix (RNG state + seen pool).

        The cut must sit on a row boundary with no still-open prefetched
        callable: a bound method cannot be carried across a serialized state.
        """
        open_bindings = {
            name
            for idx, names in self.scn.prefetch.items()
            if idx <= through_row
            for name in names
            if self._row_index(name) > through_row
        }
        if open_bindings:
            raise ValueError(
                f"prefix cut at row {through_row} would split open prefetched callables "
                f"{sorted(open_bindings)}; choose a boundary after they are called"
            )
        key = (through_row, tuple(sorted(ctx.items())))
        if key in self._prefix_memo:
            return self._prefix_memo[key]
        fake = self.sim.fresh()
        bound: dict[str, Callable] = {}
        pending: dict[str, tuple[list, list]] = {}
        history: list[Any] = []
        for idx in range(through_row + 1):
            row = self.scn.rows[idx]
            self.sim._do_prefetch(fake, idx, bound, pending)
            result, _ = self.sim._execute_row(
                fake, row, history[-1] if history else None, ctx, bound, pending
            )
            history.append(result)
        state = self.sim.snapshot(fake)
        self._prefix_memo[key] = state
        return state

    def _row_index(self, name: str) -> int:
        return next(i for i, r in enumerate(self.scn.rows) if r.name == name)

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(
            pickle.dumps(
                {"scenario": self.scn, "prefix_memo": self._prefix_memo, "evaluated": self._evaluated}
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> "QuerySession":
        payload = pickle.loads(Path(path).read_bytes())
        session = cls(payload["scenario"])
        session._prefix_memo = payload["prefix_memo"]
        session._evaluated = payload["evaluated"]
        return session
