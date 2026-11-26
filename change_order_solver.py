#!/usr/bin/env python3
"""Shared-template selection engine for the change-order work order.

The concrete handoff manifest (old/new definitions, expanded words, source
positions, station licenses and replacement numbers) is not present in this
workspace.  This module therefore provides the batch-safe core and keeps all
provenance supplied by the caller.  It has no third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


Predicate = Callable[[tuple[int, ...]], bool]


@dataclass(frozen=True)
class Option:
    id: str
    replacement_number: int
    cost: int = 1


@dataclass(frozen=True)
class Choice:
    id: str
    options: tuple[Option, ...]
    tie_rank: int

    @property
    def max_cost(self) -> int:
        return max(option.cost for option in self.options)


@dataclass
class Constraint:
    case_id: str
    station: str
    file: str
    source: str
    scope: tuple[int, ...]
    predicate: Predicate
    replay: Mapping[str, Any] = field(default_factory=dict)


def find_with_cap(
    domains: Sequence[Sequence[Option]],
    constraints: Sequence[Constraint],
    cost_cap: int,
    fixed: Mapping[int, int] | None = None,
) -> tuple[int, ...] | None:
    """Find one assignment whose cost is at most ``cost_cap``.

    Search is MRV backtracking with immediate propagation of fully assigned
    scopes and memoized failed partial assignments.  It does not enumerate the
    full Cartesian product.
    """
    option_costs = [
        [option.cost for option in options] for options in domains
    ]
    min_cost = [min(costs) for costs in option_costs]
    assignment = [-1] * len(domains)

    for variable, value in (fixed or {}).items():
        assignment[variable] = value

    def scope_satisfied(constraint: Constraint) -> bool:
        if any(assignment[index] < 0 for index in constraint.scope):
            return True
        return constraint.predicate(tuple(assignment))

    def legal_value(variable: int, value: int) -> bool:
        assignment[variable] = value
        try:
            return all(scope_satisfied(item) for item in constraints)
        finally:
            assignment[variable] = -1

    failed: set[tuple[int, ...]] = set()

    def search(used_cost: int) -> tuple[int, ...] | None:
        lower_bound = used_cost + sum(
            min_cost[index]
            for index, value in enumerate(assignment)
            if value < 0
        )
        if lower_bound > cost_cap:
            return None

        state = tuple(assignment)
        if state in failed:
            return None

        candidates: list[tuple[int, int, list[int]]] = []
        for variable, current in enumerate(assignment):
            if current >= 0:
                continue
            values = [
                value
                for value in range(len(domains[variable]))
                if legal_value(variable, value)
            ]
            if not values:
                failed.add(state)
                return None
            candidates.append((len(values), variable, values))

        if not candidates:
            return state

        _, variable, values = min(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        for value in values:
            assignment[variable] = value
            result = search(used_cost + option_costs[variable][value])
            if result is not None:
                return result
            assignment[variable] = -1

        failed.add(state)
        return None

    initial_cost = sum(
        option_costs[index][value]
        for index, value in enumerate(assignment)
        if value >= 0
    )
    if not all(scope_satisfied(item) for item in constraints):
        return None
    return search(initial_cost)


def solve_common_order(
    choices: Sequence[Choice],
    constraints: Sequence[Constraint],
) -> dict[str, Any] | None:
    """Return minimum-cost assignment, tie-broken by ticket order."""
    domains = [choice.options for choice in choices]
    maximum_possible = sum(choice.max_cost for choice in choices)

    optimal: tuple[int, ...] | None = None
    optimal_cost: int | None = None
    for cost_cap in range(maximum_possible + 1):
        candidate = find_with_cap(domains, constraints, cost_cap)
        if candidate is not None:
            optimal = candidate
            optimal_cost = cost_cap
            break

    if optimal is None or optimal_cost is None:
        return None

    fixed: dict[int, int] = {}
    for choice_index, choice in sorted(
        enumerate(choices), key=lambda item: item[1].tie_rank
    ):
        for option_index, option in sorted(
            enumerate(choice.options),
            key=lambda item: item[1].replacement_number,
        ):
            trial = dict(fixed)
            trial[choice_index] = option_index
            if find_with_cap(
                domains,
                constraints,
                optimal_cost,
                fixed=trial,
            ) is not None:
                fixed = trial
                break
        else:  # pragma: no cover - guarded by the optimal assignment
            raise RuntimeError("tie-breaking lost the optimal solution")

    selected = [
        choices[index].options[value] for index, value in sorted(fixed.items())
    ]
    tie_key = tuple(
        choices[index].options[value].replacement_number
        for index, value in sorted(fixed.items())
    )
    return {
        "replacements": [
            {
                "choice_id": choices[index].id,
                "option_id": choices[index].options[value].id,
                "replacement_number": choices[index]
                .options[value]
                .replacement_number,
                "cost": choices[index].options[value].cost,
            }
            for index, value in sorted(fixed.items())
        ],
        "cost": optimal_cost,
        "tie_key": tie_key,
        "selected_options": selected,
    }


def conflict_core(
    choices: Sequence[Choice],
    constraints: Sequence[Constraint],
) -> list[Constraint]:
    """Return an inclusion-minimal replayable unsat core."""
    domains = [choice.options for choice in choices]
    maximum_cost = sum(choice.max_cost for choice in choices)
    if find_with_cap(domains, constraints, maximum_cost) is not None:
        return []

    core = list(constraints)
    for constraint in list(core):
        trial = [item for item in core if item is not constraint]
        if find_with_cap(domains, trial, maximum_cost) is None:
            core = trial
    return core


def diff_observation(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    """Classify a replay mismatch as preregistration/action/error."""
    differences: list[str] = []

    expected_prereg = expected.get("preregistrations")
    actual_prereg = actual.get("preregistrations")
    if (
        expected_prereg is not None
        and _normalize_prereg(expected_prereg)
        != _normalize_prereg(actual_prereg or [])
    ):
        differences.append("preregistration")

    expected_error = expected.get("error")
    actual_error = actual.get("error")
    if expected_error is not None or actual_error is not None:
        if not _error_matches(expected_error, actual_error):
            differences.append("error")

    expected_action = expected.get("action")
    if expected_action is not None:
        action_mismatch = expected_action != actual.get("action")
        expected_args = expected.get("args")
        if expected_args is not None and expected_args != actual.get("args"):
            action_mismatch = True
        expected_kwargs = expected.get("kwargs")
        if (
            expected_kwargs is not None
            and expected_kwargs != actual.get("kwargs")
        ):
            action_mismatch = True
        if action_mismatch and "error" not in differences:
            differences.append("action")

    return differences


def _normalize_prereg(records: Sequence[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            record.get("file"),
            record.get("pos0", record.get("position")),
            record.get("calibration"),
            record.get("channel"),
        )
        for record in records
    ]


def _error_matches(
    expected: Mapping[str, Any] | None,
    actual: Mapping[str, Any] | None,
) -> bool:
    if expected is None or actual is None:
        return expected is actual
    if expected.get("type") != actual.get("type"):
        return False
    expected_position = expected.get("original_position")
    if expected_position is not None:
        actual_position = actual.get("original_position")
        if expected_position == "end":
            if actual_position != "end":
                return False
        elif expected_position != actual_position:
            return False
    accepted_missing = expected.get("missing_variants")
    if accepted_missing is not None:
        actual_variants = actual.get("missing_variants")
        if actual_variants is None and actual.get("missing") is not None:
            actual_variants = [actual["missing"]]
        if actual_variants is None:
            return False
        accepted = {frozenset(variant) for variant in accepted_missing}
        observed = {frozenset(variant) for variant in actual_variants}
        if not (accepted & observed):
            return False
    return True
