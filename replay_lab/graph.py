from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlsplit

from replay_lab.core import (
    apply_transition_vars,
    build_request_spec,
    canonical_request_url,
    evaluate_condition,
    job_ids,
    select_transition,
)


def item_identity(registration_id: str, position: int, item: Dict[str, Any]) -> str:
    return str(item.get("id") or "%s:%d" % (registration_id, position))


def initial_registry_state(plan: Dict[str, Any]) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    state = []
    for registration in plan.get("registrations", []):
        items = tuple(
            item_identity(registration["id"], position, item)
            for position, item in enumerate(registration.get("responses", []))
        )
        state.append((registration["id"], items))
    return tuple(state)


def _item_lookup(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    lookup = {}
    for registration in plan.get("registrations", []):
        for position, item in enumerate(registration.get("responses", [])):
            lookup[item_identity(registration["id"], position, item)] = item
    return lookup


def _registry_map(state: Tuple[Tuple[str, Tuple[str, ...]], ...]) -> Dict[str, List[str]]:
    return {registration_id: list(items) for registration_id, items in state}


def _registry_key(registry: Dict[str, List[str]]) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    return tuple(sorted(
        (registration_id, tuple(items))
        for registration_id, items in registry.items()
    ))


def _query_matches(registration: Dict[str, Any], request_query: str) -> bool:
    registration_url = registration.get("url")
    if not registration_url or registration_url == "@any":
        return True

    registration_parts = urlsplit(registration_url)
    request_qs = parse_qs(request_query, keep_blank_values=True)
    matcher_qs = parse_qs(registration_parts.query, keep_blank_values=True)

    for key, expected_values in matcher_qs.items():
        for value in expected_values:
            try:
                request_qs.get(key, []).remove(value)
            except ValueError:
                return False

    if registration.get("complete_qs", False):
        for values in request_qs.values():
            if values:
                return False
    return True


def pure_match(
    plan: Dict[str, Any],
    method: str,
    url: str,
    params: Any,
    registry_state: Tuple[Tuple[str, Tuple[str, ...]], ...],
):
    actual_url = canonical_request_url(url, params)
    request_parts = urlsplit(actual_url)
    registry = _registry_map(registry_state)

    for registration in reversed(plan.get("registrations", [])):
        if registration.get("method", "GET").upper() != method:
            continue

        registration_url = registration.get("url")
        if registration_url and registration_url != "@any":
            registration_parts = urlsplit(registration_url)
            if registration_parts.path and registration_parts.path != request_parts.path:
                continue
            if registration_parts.netloc:
                if registration_parts.scheme and registration_parts.scheme != request_parts.scheme:
                    continue
                if registration_parts.netloc.lower() != request_parts.netloc.lower():
                    continue

        if not _query_matches(registration, request_parts.query):
            continue

        query = parse_qs(request_parts.query, keep_blank_values=True)
        if not evaluate_condition(registration.get("additional_matcher"), qs=query):
            continue

        if registration.get("request_headers"):
            continue

        return registration, registry

    return None, registry


def _job_key(state: str, attempt: int, variables: Dict[str, Any], done: bool = False, branch: str = ""):
    if done:
        return ("DONE", branch, ())
    return (
        state,
        int(attempt),
        tuple(sorted((key, str(value)) for key, value in variables.items())),
    )


def explore_canonical_graph(case: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    ids = job_ids(case)
    initial_registry = initial_registry_state(plan)
    item_lookup = _item_lookup(plan)

    initial_jobs = tuple(
        _job_key(case["jobs"][job_id]["initial"], 1, {})
        for job_id in ids
    )
    root = (initial_jobs, initial_registry)
    seen = {root}
    stack = [root]
    edge_count = 0
    terminal_states = set()

    while stack:
        job_states, registry_state = stack.pop()

        for job_index, job_id in enumerate(ids):
            job_state = job_states[job_index]
            if job_state[0] == "DONE":
                continue

            state_name, attempt, variable_items = job_state
            variables = {key: value for key, value in variable_items}
            job = case["jobs"][job_id]
            method, url, params = build_request_spec(job, state_name, variables)
            registration, registry = pure_match(
                plan, method, url, params, registry_state
            )
            edge_count += 1

            if registration is None:
                outcome = {
                    "status": None,
                    "exception": "NoMockAddress",
                    "json": None,
                }
                original_id = None
            else:
                registration_id = registration["id"]
                remaining = registry[registration_id]
                if len(remaining) > 1:
                    consumed_id = remaining.pop(0)
                else:
                    consumed_id = remaining[0]
                item = item_lookup[consumed_id]
                original_id = item.get("original")
                if item.get("exception"):
                    exception_name = str(item["exception"]).rsplit(".", 1)[-1]
                    outcome = {
                        "status": None,
                        "exception": exception_name,
                        "json": None,
                    }
                else:
                    outcome = {
                        "status": int(item.get("status", 200)),
                        "exception": None,
                        "json": item.get("json"),
                    }

            rule = select_transition(job, state_name, outcome, attempt)
            if rule is None:
                next_job_state = _job_key(
                    state_name,
                    attempt,
                    variables,
                    done=True,
                    branch="unhandled",
                )
            elif rule.get("terminal"):
                next_job_state = _job_key(
                    state_name,
                    attempt,
                    variables,
                    done=True,
                    branch=str(rule["terminal"]),
                )
            else:
                if rule.get("set_vars") and outcome.get("json") is not None:
                    variables = apply_transition_vars(
                        rule, outcome["json"], variables
                    )
                next_state = rule.get("next") or state_name
                next_attempt = attempt + 1 if next_state == state_name else 1
                next_job_state = _job_key(next_state, next_attempt, variables)

            next_job_states = list(job_states)
            next_job_states[job_index] = next_job_state
            next_registry = _registry_key(registry)
            next_key = (tuple(next_job_states), next_registry)

            if next_job_state[0] == "DONE":
                terminal_states.add(next_job_state[1])
            if next_key not in seen:
                seen.add(next_key)
                stack.append(next_key)

    return {
        "state_count": len(seen),
        "edge_count": edge_count,
        "terminal_branches": sorted(terminal_states),
    }
