from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .model import Case, Fixture, TaskSpec
from .report import format_plan, pad_label
from .wire import BudgetViolation, PreparedMessage, WireReport, build_command_wire


@dataclass
class FixtureBudgetResult:
    fixture: str
    literal_plus: bool
    before_tier: int
    before_tier_name: str
    burst_limit: Optional[int]
    command_limit: Optional[int]
    total_bytes: int
    byte_tier: int
    category: str
    transition_rule: str
    after_tier: Optional[int]
    after_tier_name: Optional[str]
    ok: bool
    violation: Optional[BudgetViolation] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fixture": self.fixture,
            "literal_plus": self.literal_plus,
            "before_tier": self.before_tier,
            "before_tier_name": self.before_tier_name,
            "burst_limit": self.burst_limit,
            "command_limit": self.command_limit,
            "total_bytes": self.total_bytes,
            "byte_tier": self.byte_tier,
            "category": self.category,
            "transition_rule": self.transition_rule,
            "after_tier": self.after_tier,
            "after_tier_name": self.after_tier_name,
            "ok": self.ok,
            "violation": self.violation.to_dict() if self.violation else None,
            "provenance": self.provenance,
        }


@dataclass
class GroupEvaluation:
    case_ids: Tuple[Any, ...]
    folder: str
    cut_id: Any
    reports: Dict[str, WireReport]
    budgets: List[FixtureBudgetResult]
    next_tiers: Tuple[int, ...]
    ok: bool
    message_sources: List[Dict[str, Any]]

    def to_dict(self, include_length_fields: bool = True) -> Dict[str, Any]:
        return {
            "case_ids": list(self.case_ids),
            "folder": self.folder,
            "cut_id": self.cut_id,
            "ok": self.ok,
            "bytes_by_fixture": {
                name: report.total_bytes for name, report in self.reports.items()
            },
            "budgets": [item.to_dict() for item in self.budgets],
            "message_sources": self.message_sources,
            "length_fields": {
                name: report.length_fields for name, report in self.reports.items()
            }
            if include_length_fields
            else None,
        }


def flatten_messages(cases: Sequence[Case]) -> List[PreparedMessage]:
    messages: List[PreparedMessage] = []
    for case in cases:
        messages.extend(case.prepared_messages())
    return messages


def evaluate_group(
    cases: Sequence[Case],
    fixtures: Sequence[Fixture],
    tier_indices: Sequence[int],
    *,
    tag: bytes = b"tag",
    cut_id: Any = None,
    budget_origins: Optional[Sequence[Dict[str, Any]]] = None,
) -> GroupEvaluation:
    if not cases:
        raise ValueError("a command/group must contain at least one case")
    folders = {case.folder for case in cases}
    if len(folders) != 1:
        raise ValueError(f"a command must have one folder, got {sorted(folders)!r}")

    messages = flatten_messages(cases)
    category = str(cases[-1].category)
    reports: Dict[str, WireReport] = {}
    budgets: List[FixtureBudgetResult] = []
    next_tiers: List[int] = []
    all_ok = True

    for fixture_index, fixture in enumerate(fixtures):
        tier_index = int(tier_indices[fixture_index])
        tier = fixture.tiers[tier_index]
        report = build_command_wire(
            messages,
            cases[0].folder,
            tag=tag,
            literal_plus=fixture.literal_plus,
        )
        reports[fixture.name] = report
        violation = report.check_budget(tier.burst_limit, tier.command_limit)

        byte_tier = fixture.classify_byte_tier(report.total_bytes)
        after_tier: Optional[int] = tier_index
        rule = "budget-check-failed"
        if violation.ok:
            after_tier, rule = fixture.next_tier_index(
                tier_index, category, report.total_bytes
            )
            if after_tier is None:
                violation = BudgetViolation(
                    ok=False,
                    boundary_id="tier-transition",
                    limit_kind="transition",
                    sent_bytes=report.total_bytes,
                    message_number=len(messages),
                    case_id=cases[-1].case_id,
                )

        if not violation.ok:
            all_ok = False
            after_tier = None
        else:
            next_tiers.append(int(after_tier))

        origin: Dict[str, Any]
        if budget_origins is not None:
            origin = dict(budget_origins[fixture_index])
        else:
            origin = {"origin": "initial", "tier": tier_index}

        provenance = {
            "current_budget_origin": origin,
            "selected_because": {
                "last_case_id": cases[-1].case_id,
                "category": category,
                "total_bytes": report.total_bytes,
                "byte_tier": byte_tier,
                "rule": rule,
            },
        }
        budgets.append(
            FixtureBudgetResult(
                fixture=fixture.name,
                literal_plus=fixture.literal_plus,
                before_tier=tier_index,
                before_tier_name=tier.name,
                burst_limit=tier.burst_limit,
                command_limit=tier.command_limit,
                total_bytes=report.total_bytes,
                byte_tier=byte_tier,
                category=category,
                transition_rule=rule,
                after_tier=after_tier,
                after_tier_name=fixture.tiers[after_tier].name
                if after_tier is not None and violation.ok
                else None,
                ok=violation.ok,
                violation=violation if not violation.ok else None,
                provenance=provenance,
            )
        )

    sources = [
        {
            "message_number": i + 1,
            "case_id": message.case_id,
            "source": message.source,
            "length": message.length,
            "flags_token": message.flags.decode("ascii"),
            "date_token": message.date.decode("ascii") if message.date else None,
            "normalized_sha256": message.normalized_sha256,
        }
        for i, message in enumerate(messages)
    ]
    return GroupEvaluation(
        case_ids=tuple(case.case_id for case in cases),
        folder=cases[0].folder,
        cut_id=cut_id,
        reports=reports,
        budgets=budgets,
        next_tiers=tuple(next_tiers) if all_ok else tuple(),
        ok=all_ok,
        message_sources=sources,
    )


def initial_budget_origins(fixtures: Sequence[Fixture]) -> List[Dict[str, Any]]:
    origins = []
    for fixture in fixtures:
        tier = fixture.tiers[fixture.initial_tier]
        origins.append(
            {
                "origin": "initial_tier",
                "fixture": fixture.name,
                "tier": fixture.initial_tier,
                "tier_name": tier.name,
                "burst_limit": tier.burst_limit,
                "command_limit": tier.command_limit,
            }
        )
    return origins


def updated_origins(
    fixtures: Sequence[Fixture],
    evaluation: GroupEvaluation,
) -> List[Dict[str, Any]]:
    origins: List[Dict[str, Any]] = []
    for fixture, budget in zip(fixtures, evaluation.budgets):
        tier_index = budget.after_tier
        if tier_index is None:
            origins.append({})
            continue
        tier = fixture.tiers[tier_index]
        origins.append(
            {
                "origin": "after_command",
                "fixture": fixture.name,
                "tier": tier_index,
                "tier_name": tier.name,
                "burst_limit": tier.burst_limit,
                "command_limit": tier.command_limit,
                "from_tier": budget.before_tier,
                "last_case_id": budget.provenance["selected_because"]["last_case_id"],
                "category": budget.category,
                "total_bytes": budget.total_bytes,
                "byte_tier": budget.byte_tier,
                "rule": budget.transition_rule,
            }
        )
    return origins


@dataclass
class Parent:
    previous_state: Tuple[Any, ...]
    start: int
    end: int
    evaluation: GroupEvaluation
    origins: Tuple[Dict[str, Any], ...]


@dataclass
class LinearPlanResult:
    status: str
    sequence: List[Any]
    commands: List[GroupEvaluation] = field(default_factory=list)
    command_count: Optional[int] = None
    cuts: Tuple[Any, ...] = ()
    tie_key: Tuple[Any, ...] = ()
    failure: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "sequence": list(self.sequence),
            "command_count": self.command_count,
            "cuts": list(self.cuts),
            "tie_key": list(self.tie_key),
            "commands": [command.to_dict() for command in self.commands],
            "failure": self.failure,
        }


def plan_linear(
    ordered_cases: Sequence[Case],
    fixtures: Sequence[Fixture],
    *,
    tag: bytes = b"tag",
    max_group_cases: Optional[int] = None,
    sequence_ids: Optional[Sequence[Any]] = None,
    initial_tiers: Optional[Sequence[int]] = None,
    initial_origins: Optional[Sequence[Dict[str, Any]]] = None,
) -> LinearPlanResult:
    n = len(ordered_cases)
    if n == 0:
        return LinearPlanResult("feasible", [], command_count=0, tie_key=())

    tier_count = len(fixtures)
    if initial_tiers is None:
        initial_tiers = tuple(fixture.initial_tier for fixture in fixtures)
    else:
        initial_tiers = tuple(int(x) for x in initial_tiers)
    initial_state: Tuple[Any, ...] = (0,) + initial_tiers
    dp: Dict[Tuple[Any, ...], Tuple[int, Tuple[Any, ...]]] = {
        initial_state: (0, ())
    }
    parents: Dict[Tuple[Any, ...], Parent] = {}
    best_failure: Optional[Dict[str, Any]] = None

    def remember_failure(
        state: Tuple[Any, ...],
        start: int,
        end: int,
        evaluation: GroupEvaluation,
        path_commands: int,
        origins: Sequence[Dict[str, Any]],
    ) -> None:
        nonlocal best_failure
        candidate = {
            "reachable_command_count": path_commands,
            "state": list(state),
            "start": start,
            "end": end,
            "cut_id": end,
            "group": evaluation.to_dict(),
            "budget_origins": list(origins),
        }
        rank = (path_commands, end, -start)
        if best_failure is None or rank > best_failure["_rank"]:
            candidate["_rank"] = rank
            best_failure = candidate

    for start in range(n):
        states_here = [state for state in dp if state[0] == start]
        for state in states_here:
            tiers = tuple(int(x) for x in state[1:])
            cost, cuts = dp[state]
            parent = parents.get(state)
            origins = (
                list(parent.origins)
                if parent is not None
                else list(initial_origins)
                if initial_origins is not None
                else initial_budget_origins(fixtures)
            )

            end_limit = n + 1
            if max_group_cases is not None:
                end_limit = min(end_limit, start + int(max_group_cases) + 1)

            folder = ordered_cases[start].folder
            for end in range(start + 1, end_limit):
                group = ordered_cases[start:end]
                if any(case.folder != folder for case in group):
                    break

                evaluation = evaluate_group(
                    group,
                    fixtures,
                    tiers,
                    tag=tag,
                    cut_id=end,
                    budget_origins=origins,
                )
                if not evaluation.ok:
                    remember_failure(state, start, end, evaluation, cost, origins)
                    # Total byte size is monotonic as cases are appended.
                    if any(
                        budget.violation is not None
                        and budget.violation.limit_kind == "command"
                        for budget in evaluation.budgets
                    ):
                        break
                    continue

                new_origins = updated_origins(fixtures, evaluation)
                new_state: Tuple[Any, ...] = (end,) + evaluation.next_tiers
                new_cuts = cuts + (end,)
                candidate_cost = (cost + 1, new_cuts)
                old = dp.get(new_state)
                if old is None or candidate_cost < old:
                    dp[new_state] = candidate_cost
                    parents[new_state] = Parent(
                        previous_state=state,
                        start=start,
                        end=end,
                        evaluation=evaluation,
                        origins=tuple(new_origins),
                    )

    final_states = [state for state in dp if state[0] == n]
    sequence = list(sequence_ids) if sequence_ids is not None else [c.case_id for c in ordered_cases]
    if not final_states:
        failure = dict(best_failure) if best_failure else None
        if failure is not None:
            failure.pop("_rank", None)
        return LinearPlanResult(
            "infeasible",
            sequence,
            failure=failure
            or {"reason": "no legal command partition", "budget_origins": initial_budget_origins(fixtures)},
        )

    best_state = min(final_states, key=lambda state: dp[state])
    command_cost, cuts = dp[best_state]
    evaluations_reversed: List[GroupEvaluation] = []
    state = best_state
    while state in parents:
        parent = parents[state]
        evaluations_reversed.append(parent.evaluation)
        state = parent.previous_state
    commands = list(reversed(evaluations_reversed))
    return LinearPlanResult(
        "feasible",
        sequence,
        commands=commands,
        command_count=command_cost,
        cuts=tuple(cuts),
        tie_key=(tuple(sequence), tuple(cuts)),
    )


def topological_orders(cases: Sequence[Case], limit: Optional[int] = None) -> Iterable[List[Case]]:
    by_id = {case.case_id: case for case in cases}
    ids = [case.case_id for case in cases]
    predecessors: Dict[Any, set] = {case_id: set() for case_id in ids}
    for case in cases:
        for predecessor in case.predecessors:
            if predecessor in by_id:
                predecessors[case.case_id].add(predecessor)

    remaining = set(ids)
    available = [case_id for case_id in ids if not predecessors[case_id]]
    heapq.heapify(available)
    count = 0

    def generate(heap: List[Any], chosen: List[Case], pending: set) -> Iterable[List[Case]]:
        nonlocal count
        if limit is not None and count >= limit:
            return
        if not pending:
            count += 1
            yield list(chosen)
            return

        for index, case_id in enumerate(list(heap)):
            if limit is not None and count >= limit:
                return
            rest = heap[:index] + heap[index + 1 :]
            heapq.heapify(rest)
            newly_available = []
            for other_id in pending:
                preds = predecessors[other_id]
                if case_id in preds:
                    preds = preds - {case_id}
                    predecessors[other_id] = preds
                    if not preds:
                        newly_available.append(other_id)
            for new_id in newly_available:
                heapq.heappush(rest, new_id)
            next_pending = pending - {case_id}
            chosen.append(by_id[case_id])
            yield from generate(rest, chosen, next_pending)
            chosen.pop()
            for other_id in newly_available:
                predecessors[other_id].add(case_id)

    yield from generate(available, [], remaining)


def plan_task(spec: TaskSpec, *, topo_order_limit: int = 20000) -> Dict[str, Any]:
    cases_by_id = spec.case_map()
    if spec.order is not None:
        missing = [case_id for case_id in spec.order if case_id not in cases_by_id]
        if missing:
            raise ValueError(f"order contains unknown case ids: {missing!r}")
        base_order = [cases_by_id[case_id] for case_id in spec.order]
        for case in spec.cases:
            if case.case_id not in spec.order:
                base_order.append(case)
    else:
        base_order = list(sorted(spec.cases, key=lambda case: (case.order_index, str(case.case_id))))

    if not spec.allow_reorder:
        result = plan_linear(
            base_order,
            spec.fixtures,
            tag=spec.tag,
            max_group_cases=spec.max_group_cases,
        )
        return _finalize_result(spec, result, orders_considered=1)

    best: Optional[LinearPlanResult] = None
    considered = 0
    for order in topological_orders(spec.cases, limit=topo_order_limit):
        considered += 1
        result = plan_linear(
            order,
            spec.fixtures,
            tag=spec.tag,
            max_group_cases=spec.max_group_cases,
        )
        if result.status != "feasible":
            if best is None or result.failure is not None:
                best = result
            continue
        if best is None or result.tie_key < best.tie_key:
            best = result

    if best is None:
        return {
            "status": "infeasible",
            "command_count": None,
            "orders_considered": considered,
            "failure": {
                "reason": "precedence constraints contain a cycle or no topological order",
                "budget_origins": initial_budget_origins(spec.fixtures),
            },
            "fixture_labels": [
                _fixed_label(fixture.name, max(10, fixture.label_width))
                for fixture in spec.fixtures
            ],
        }
    return _finalize_result(spec, best, orders_considered=considered)


def _finalize_result(spec: TaskSpec, result: LinearPlanResult, *, orders_considered: int) -> Dict[str, Any]:
    label_width = max([10] + [fixture.label_width for fixture in spec.fixtures])
    if result.status != "feasible":
        payload = {
            "status": "infeasible",
            "command_count": None,
            "orders_considered": orders_considered,
            "failure": result.failure,
            "fixture_labels": [_fixed_label(fixture.name, label_width) for fixture in spec.fixtures],
        }
        payload["text_sheet"] = format_plan(payload, label_width=label_width)
        return payload

    commands = []
    for index, evaluation in enumerate(result.commands, start=1):
        item = evaluation.to_dict()
        item["command_index"] = index
        commands.append(item)

    verification = verify_plan_receipts(spec, result.commands)
    payload = {
        "status": "feasible",
        "command_count": result.command_count,
        "tie_key": list(result.tie_key),
        "orders_considered": orders_considered,
        "cuts": list(result.cuts),
        "commands": commands,
        "verification": verification,
        "fixture_labels": [_fixed_label(fixture.name, label_width) for fixture in spec.fixtures],
    }
    payload["text_sheet"] = format_plan(payload, label_width=label_width)
    return payload


def _fixed_label(name: str, width: int) -> str:
    return pad_label(name, width)


def verify_plan_receipts(spec: TaskSpec, evaluations: Sequence[GroupEvaluation]) -> Dict[str, Any]:
    cases = spec.case_map()
    issues: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    checked = 0
    for evaluation in evaluations:
        for case_id in evaluation.case_ids:
            case = cases[case_id]
            for message in case.messages:
                checked += 1
                message_issues: List[str] = []
                wire_flags = message.prepared(case_id).flags.decode("ascii")
                if message.date is None or message.date.utcoffset() is None:
                    issue = "missing explicit timezone"
                    issues.append({"source": message.source, "issue": issue})
                    message_issues.append(issue)
                if message.flags is None:
                    issue = "flags missing; empty list required"
                    issues.append({"source": message.source, "issue": issue})
                    message_issues.append(issue)
                elif not message.flags and wire_flags != "()":
                    issue = "empty flags must be sent as ()"
                    issues.append({"source": message.source, "issue": issue})
                    message_issues.append(issue)

                receipt_flags = message.receipt.get("flags")
                if receipt_flags is not None and tuple(receipt_flags) != tuple(message.flags):
                    issue = "flags differ from old receipt"
                    issues.append(
                        {
                            "source": message.source,
                            "issue": issue,
                            "expected": list(receipt_flags),
                            "actual": list(message.flags),
                        }
                    )
                    message_issues.append(issue)

                expected_digest = message.digest or message.receipt.get("digest")
                if expected_digest and message.body is not None:
                    actual = message.sha256()
                    expected_hex = (
                        expected_digest.split(":", 1)[1]
                        if str(expected_digest).startswith("sha256:")
                        else expected_digest
                    )
                    if actual != expected_hex:
                        issue = "digest mismatch"
                        issues.append(
                            {
                                "source": message.source,
                                "issue": issue,
                                "expected": expected_digest,
                                "actual": actual,
                            }
                        )
                        message_issues.append(issue)

                if message.attribution is None and not message.receipt.get("attribution"):
                    issue = "original attribution missing"
                    issues.append({"source": message.source, "issue": issue})
                    message_issues.append(issue)

                results.append(
                    {
                        "source": message.source,
                        "case_id": case_id,
                        "ok": not message_issues,
                        "issues": message_issues,
                        "flags_token": wire_flags,
                        "date_token": message.date_token().decode("ascii")
                        if message.date is not None and message.date.utcoffset() is not None
                        else None,
                    }
                )
    return {
        "checked_messages": checked,
        "ok": not issues,
        "issues": issues,
        "results": results,
    }
