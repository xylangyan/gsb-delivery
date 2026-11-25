"""Choose one label decision per marked position for the whole batch.

A decision is global per original-manual position: a change that is signed
for one position is used by every release that includes it.  Decisions are
therefore solved jointly, but positions only interact through the releases
that include them, so the problem decomposes into connected components
(positions linked when they share a release, or when a reference is coupled
to its definition position).  Components are solved independently by
backtracking search, optimizing lexicographically:

1. fewest changed original positions (each position counted once, however
   many releases use it);
2. fewest definition lines brought into releases in total.

The search respects a time budget.  If a component cannot be decided in
time, its releases are reported as *pending* -- the preview explicitly says
the result is not computed yet, rather than claiming the batch cannot be
published.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .manual import Manual
from .plan import DefKey, MarkId, ReleaseModel

KEEP = None  # decision value meaning "keep the original label"


@dataclass
class Variable:
    mark_id: MarkId
    manual: str
    offset: int
    kind: str  # "ref" | "def"
    orig_label: str  # normalized original label
    candidates: list[str]  # normalized approved replacements
    candidate_raw: dict[str, str]  # normalized -> as written in the table
    def_key: DefKey
    releases: set[str] = field(default_factory=set)
    # releases where the position's own text appears (for definitions: where
    # the definition line travels physically inside a selected segment)
    physical_releases: set[str] = field(default_factory=set)

    def domain(self) -> list[str | None]:
        return [KEEP, *self.candidates]


@dataclass
class Blocker:
    release: str
    label: str
    kind: str  # "literal" | "definition" | "coupling"
    text: str
    manual: str | None = None
    offset: int | None = None
    blocked_marks: list[MarkId] = field(default_factory=list)


@dataclass
class ComponentResult:
    status: str  # "ok" | "infeasible" | "timeout"
    assignment: dict[MarkId, str] = field(default_factory=dict)  # changed only
    changes: int = 0
    appends: int = 0


@dataclass
class SolveResult:
    assignment: dict[MarkId, str]  # mark_id -> chosen new label (changed only)
    release_status: dict[str, str]  # "ok" | "blocked" | "pending"
    blockers: list[Blocker]
    fixed_conflicts: dict[str, list[str]]
    elapsed: float


class _BudgetExceeded(Exception):
    pass


class Solver:
    def __init__(
        self,
        variables: dict[MarkId, Variable],
        releases: dict[str, ReleaseModel],
        manuals: dict[str, Manual],
        budget: float,
        check_interval: int = 1024,
    ) -> None:
        self.variables = variables
        self.releases = releases
        self.manuals = manuals
        self.budget = budget
        self.check_interval = check_interval
        self.deadline = 0.0
        self._steps = 0

    # ------------------------------------------------------------- solving

    def solve(self) -> SolveResult:
        started = time.monotonic()
        self.deadline = started + max(self.budget, 0.0)
        components = self._components()
        assignment: dict[MarkId, str] = {}
        release_status = {name: "ok" for name in self.releases}
        blockers: list[Blocker] = []
        fixed_conflicts = {
            name: list(model.fixed_conflicts)
            for name, model in self.releases.items()
            if model.fixed_conflicts
        }

        for name, conflicts in fixed_conflicts.items():
            if conflicts:
                release_status[name] = "blocked"

        for comp in components:
            comp_releases = sorted(
                {r for mark in comp for r in self.variables[mark].releases}
            )
            # Releases with decision-independent conflicts are doomed on
            # their own; they no longer constrain the shared decisions.
            open_releases = [
                r for r in comp_releases if release_status[r] != "blocked"
            ]
            if not open_releases:
                continue
            try:
                result = self._solve_component(comp, open_releases)
            except _BudgetExceeded:
                for r in open_releases:
                    release_status[r] = "pending"
                continue
            if result.status == "infeasible":
                blockers.extend(self._diagnose(comp, open_releases))
                for r in open_releases:
                    release_status[r] = "blocked"
            else:
                assignment.update(result.assignment)

        # Releases with fixed conflicts but no variables stay blocked.
        return SolveResult(
            assignment=assignment,
            release_status=release_status,
            blockers=blockers,
            fixed_conflicts=fixed_conflicts,
            elapsed=time.monotonic() - started,
        )

    def _components(self) -> list[list[MarkId]]:
        parent = {m: m for m in self.variables}

        def find(x: MarkId) -> MarkId:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: MarkId, b: MarkId) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        by_def: dict[DefKey, MarkId] = {
            v.def_key: m for m, v in self.variables.items() if v.kind == "def"
        }
        for m, v in self.variables.items():
            if v.kind == "ref" and v.def_key in by_def:
                union(m, by_def[v.def_key])
        by_release: dict[str, list[MarkId]] = {}
        for m, v in self.variables.items():
            for r in v.releases:
                by_release.setdefault(r, []).append(m)
        for marks in by_release.values():
            for other in marks[1:]:
                union(marks[0], other)

        groups: dict[MarkId, list[MarkId]] = {}
        for m in self.variables:
            groups.setdefault(find(m), []).append(m)
        return [sorted(g) for g in groups.values()]

    # --------------------------------------------------------- constraints

    def _release_lines(
        self, model: ReleaseModel, assignment: dict[MarkId, str | None]
    ) -> dict[str, DefKey] | None:
        """Label -> definition identity for one release, or None on conflict."""
        lines: dict[str, DefKey] = {}

        def add(label: str, key: DefKey) -> bool:
            if label in model.forbidden:
                return False
            other = lines.get(label)
            if other is not None and other != key:
                return False
            lines[label] = key
            return True

        for req in model.ref_reqs:
            label = req.orig_label
            if req.mark_id is not None:
                decision = assignment.get(req.mark_id, KEEP)
                if decision is not KEEP:
                    label = decision
            if not add(label, req.def_key):
                return None
        for pd in model.phys_defs:
            label = pd.orig_label
            if pd.mark_id is not None:
                decision = assignment.get(pd.mark_id, KEEP)
                if decision is not KEEP:
                    label = decision
            if not add(label, pd.def_key):
                return None
        return lines

    def _coupling_ok(
        self, model: ReleaseModel, assignment: dict[MarkId, str | None]
    ) -> bool:
        for req in model.ref_reqs:
            if req.mark_id is None:
                continue
            decision = assignment.get(req.mark_id, KEEP)
            if decision is KEEP:
                continue
            def_mark = self._def_marks.get(req.def_key)
            if def_mark is None:
                return False
            if assignment.get(def_mark, KEEP) != decision:
                return False
        return True

    def _check_release(
        self, name: str, assignment: dict[MarkId, str | None]
    ) -> tuple[bool, int]:
        """Full check of one release; returns (feasible, appended def count)."""
        model = self.releases[name]
        lines = self._release_lines(model, assignment)
        if lines is None or not self._coupling_ok(model, assignment):
            return False, 0
        physical: set[tuple[DefKey, str]] = set()
        for pd in model.phys_defs:
            label = pd.orig_label
            if pd.mark_id is not None:
                decision = assignment.get(pd.mark_id, KEEP)
                if decision is not KEEP:
                    label = decision
            physical.add((pd.def_key, label))
        required = {(key, label) for label, key in lines.items()}
        return True, len(required - physical)

    def _solve_component(
        self, comp: list[MarkId], comp_releases: list[str]
    ) -> ComponentResult:
        variables = [self.variables[m] for m in comp]
        open_releases = set(comp_releases)
        # References and their definitions must stay consistent; order
        # definitions right after their references for early propagation.
        variables.sort(key=lambda v: (v.kind != "ref", v.mark_id))
        best: dict[str, object] = {"key": None, "assignment": None}
        current: dict[MarkId, str | None] = {}

        self._def_marks = {
            v.def_key: v.mark_id for v in self.variables.values() if v.kind == "def"
        }
        refs_by_def: dict[DefKey, list[MarkId]] = {}
        for v in self.variables.values():
            if v.kind == "ref":
                refs_by_def.setdefault(v.def_key, []).append(v.mark_id)

        def partial_ok(var: Variable, value: str | None) -> bool:
            if value is not KEEP and var.kind == "ref":
                def_mark = self._def_marks.get(var.def_key)
                if def_mark is None:
                    return False
                if def_mark in current and current[def_mark] != value:
                    return False
                if value not in self.variables[def_mark].candidates:
                    return False
            if var.kind == "def":
                for ref_mark in refs_by_def.get(var.def_key, []):
                    decision = current.get(ref_mark, KEEP)
                    if ref_mark in current and decision is not KEEP:
                        if decision != value:
                            return False
            label = var.orig_label if value is KEEP else value
            # Label conflicts against already-decided positions and
            # unchangeable text, but only in releases where this decision
            # actually introduces the label (a relabeled definition only
            # appears in releases containing its line or a matching
            # reference) and only in releases still in play.
            if var.kind == "def":
                touched = var.physical_releases
            else:
                touched = var.releases
            for release_name in touched & open_releases:
                model = self.releases[release_name]
                if label in model.forbidden:
                    return False
                trial = dict(current)
                trial[var.mark_id] = value
                if not self._release_lines_partial(
                    model, trial, var.def_key, label
                ):
                    return False
            return True

        def dfs(index: int, changes: int) -> None:
            self._steps += 1
            if (
                self._steps % self.check_interval == 0
                and time.monotonic() > self.deadline
            ):
                raise _BudgetExceeded
            if best["key"] is not None and changes > best["key"][0]:
                return
            if index == len(variables):
                appends = 0
                for release_name in comp_releases:
                    ok, count = self._check_release(release_name, current)
                    if not ok:
                        return
                    appends += count
                key = (changes, appends)
                if best["key"] is None or key < best["key"]:
                    best["key"] = key
                    best["assignment"] = {
                        m: v for m, v in current.items() if v is not KEEP
                    }
                return
            var = variables[index]
            for value in var.domain():
                current[var.mark_id] = value
                if partial_ok(var, value):
                    dfs(index + 1, changes + (value is not KEEP))
                del current[var.mark_id]

        dfs(0, 0)
        if best["assignment"] is None:
            return ComponentResult(status="infeasible")
        return ComponentResult(
            status="ok",
            assignment=best["assignment"],
            changes=best["key"][0],
            appends=best["key"][1],
        )

    def _release_lines_partial(
        self, model: ReleaseModel, assignment: dict[MarkId, str | None], key, label
    ) -> bool:
        """Cheap clash check for one newly assigned label in one release."""
        for req in model.ref_reqs:
            req_label = req.orig_label
            if req.mark_id is not None and req.mark_id in assignment:
                decision = assignment[req.mark_id]
                if decision is not KEEP:
                    req_label = decision
            elif req.mark_id is not None:
                continue  # undecided positions are checked at the leaf
            if req_label == label and req.def_key != key:
                return False
        for pd in model.phys_defs:
            pd_label = pd.orig_label
            if pd.mark_id is not None and pd.mark_id in assignment:
                decision = assignment[pd.mark_id]
                if decision is not KEEP:
                    pd_label = decision
            elif pd.mark_id is not None:
                continue
            if pd_label == label and pd.def_key != key:
                return False
        return True

    # ---------------------------------------------------------- diagnostics

    def _diagnose(self, comp: list[MarkId], comp_releases: list[str]) -> list[Blocker]:
        """Explain why a component cannot be published: for every candidate
        label of every marked position, find the unchangeable text (literal
        bracket or fixed definition) that blocks it in each release."""
        blockers: list[Blocker] = []
        self._def_marks = {
            v.def_key: v.mark_id for v in self.variables.values() if v.kind == "def"
        }
        for release_name in comp_releases:
            model = self.releases[release_name]
            fixed: dict[str, tuple[DefKey, str]] = {}
            for req in model.ref_reqs:
                if req.mark_id is None:
                    fixed.setdefault(req.orig_label, (req.def_key, req.ref.text))
            for pd in model.phys_defs:
                if pd.mark_id is None:
                    fixed.setdefault(pd.orig_label, (pd.def_key, pd.dinfo.label_raw))

            for mark in comp:
                var = self.variables[mark]
                if release_name not in var.releases:
                    continue
                for label in [var.orig_label, *var.candidates]:
                    display = var.candidate_raw.get(label, label)
                    literal = model.forbidden.get(label)
                    if literal is not None:
                        blockers.append(
                            Blocker(
                                release=release_name,
                                label=display,
                                kind="literal",
                                text=literal.raw,
                                manual=literal.manual,
                                offset=literal.start,
                                blocked_marks=[mark],
                            )
                        )
                        continue
                    claimant = fixed.get(label)
                    if claimant is not None and claimant[0] != var.def_key:
                        blockers.append(
                            Blocker(
                                release=release_name,
                                label=display,
                                kind="definition",
                                text=claimant[1],
                                blocked_marks=[mark],
                            )
                        )
                if var.kind == "ref":
                    def_mark = self._def_marks.get(var.def_key)
                    for candidate in var.candidates:
                        if def_mark is None or candidate not in self.variables[
                            def_mark
                        ].candidates:
                            blockers.append(
                                Blocker(
                                    release=release_name,
                                    label=candidate,
                                    kind="coupling",
                                    text="对应的定义位置未核准该标签",
                                    blocked_marks=[mark],
                                )
                            )
        return self._merge_blockers(blockers)

    @staticmethod
    def _merge_blockers(blockers: list[Blocker]) -> list[Blocker]:
        merged: dict[tuple, Blocker] = {}
        for b in blockers:
            key = (b.release, b.label, b.kind, b.text, b.offset)
            if key in merged:
                for m in b.blocked_marks:
                    if m not in merged[key].blocked_marks:
                        merged[key].blocked_marks.append(m)
            else:
                merged[key] = b
        return list(merged.values())
