from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import requests.exceptions as requests_exceptions
import requests_mock
import requests_mock.exceptions as mock_exceptions


SIGNATURE_KEYS = (
    "step",
    "attempt",
    "method",
    "url",
    "status",
    "exception",
    "original_id",
)


@dataclass
class Divergence:
    schedule: str
    job: str
    ordinal: int
    global_index: int
    kind: str
    expected: Any
    actual: Any
    message: str
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PlanEvaluation:
    plan_id: str
    equivalent: bool
    schedule_count: int
    cost_bytes: int
    response_item_count: int
    divergence: Optional[Divergence] = None
    job_signatures: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    branches: Dict[str, str] = field(default_factory=dict)
    missing_required: List[str] = field(default_factory=list)
    state_count: Optional[int] = None


class CaseError(ValueError):
    pass


def load_case(path: str | Path) -> Dict[str, Any]:
    case_path = Path(path)
    with case_path.open("r", encoding="utf-8") as handle:
        case = json.load(handle)
    case["_path"] = str(case_path.resolve())
    case.setdefault("schedule", {})
    case["schedule"].setdefault("max_attempts_per_job", 6)
    if "jobs" not in case or len(case["jobs"]) != 2:
        raise CaseError("This regression entry currently expects exactly two jobs")
    return case


def job_ids(case: Dict[str, Any]) -> List[str]:
    return list(case["jobs"].keys())


def schedule_words(case: Dict[str, Any]) -> List[str]:
    ids = job_ids(case)
    attempts = case["schedule"].get("attempts_per_job", {})
    count_a = int(attempts.get(ids[0], case["schedule"].get("default_attempts", 4)))
    count_b = int(attempts.get(ids[1], case["schedule"].get("default_attempts", 4)))
    total = count_a + count_b
    words: List[str] = []
    for positions in itertools.combinations(range(total), count_a):
        chars = ["B"] * total
        for position in positions:
            chars[position] = "A"
        words.append("".join(chars))
    return words


def normalize_params(params: Any) -> List[Tuple[str, Any]]:
    if not params:
        return []
    if isinstance(params, dict):
        return [(str(key), value) for key, value in params.items()]
    return [(str(key), value) for key, value in params]


def canonical_request_url(url: str, params: Any = None) -> str:
    pairs = normalize_params(params)
    if not pairs:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, str(value)) for key, value in pairs)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def build_request_spec(job: Dict[str, Any], state: str, variables: Dict[str, Any]):
    spec = job["requests"][state]
    if spec.get("url_var"):
        url = variables[spec["url_var"]]
    else:
        url = spec["url"]
    method = spec.get("method", "GET").upper()
    params = spec.get("params")
    return method, url, params


def _json_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _json_matches(actual[key], value)
                   for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _json_matches(a, e) for a, e in zip(actual, expected)
        )
    return actual == expected


def select_transition(
    job: Dict[str, Any],
    state: str,
    outcome: Dict[str, Any],
    attempt: int,
) -> Optional[Dict[str, Any]]:
    for rule in job.get("transitions", []):
        if rule.get("state") is not None and rule["state"] != state:
            continue
        when = rule.get("when", {})
        if "status" in when and outcome.get("status") != when["status"]:
            continue
        if "exception" in when and outcome.get("exception") != when["exception"]:
            continue
        if "json" in when and not _json_matches(outcome.get("json"), when["json"]):
            continue
        if "attempt" in when and attempt != int(when["attempt"]):
            continue
        if "attempt_lt" in when and not attempt < int(when["attempt_lt"]):
            continue
        return rule
    return None


def _lookup_path(body: Any, path: str) -> Any:
    current = body
    for part in path.split("."):
        if isinstance(current, dict):
            current = current[part]
        else:
            current = getattr(current, part)
    return current


def apply_transition_vars(
    rule: Dict[str, Any],
    body: Any,
    variables: Dict[str, Any],
) -> Dict[str, Any]:
    updated = dict(variables)
    for target, source in rule.get("set_vars", {}).items():
        updated[target] = _lookup_path(body, source)
    return updated


def resolve_exception_type(name: str) -> type:
    if "." in name:
        module_name, class_name = name.rsplit(".", 1)
        if module_name == "builtins":
            return __builtins__[class_name] if isinstance(__builtins__, dict) else getattr(__builtins__, class_name)
        for module in (requests_exceptions, mock_exceptions):
            if getattr(module, "__name__", "") == module_name:
                return getattr(module, class_name)
    for module in (requests_exceptions, mock_exceptions, __builtins__):
        if isinstance(module, dict):
            if name in module and isinstance(module[name], type):
                return module[name]
        elif hasattr(module, name):
            found = getattr(module, name)
            if isinstance(found, type):
                return found
    raise CaseError("Unknown exception type: %s" % name)


def evaluate_condition(
    condition: Optional[Dict[str, Any]],
    qs: Any = None,
    headers: Any = None,
    text: Optional[str] = None,
) -> bool:
    if condition is None:
        return True

    if "all" in condition:
        return all(evaluate_condition(child, qs, headers, text)
                   for child in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(child, qs, headers, text)
                   for child in condition["any"])
    if "not" in condition:
        return not evaluate_condition(condition["not"], qs, headers, text)

    qs = qs or {}
    if "query_contains" in condition:
        for key, expected in condition["query_contains"].items():
            values = expected if isinstance(expected, list) else [expected]
            actual_values = qs.get(key, [])
            for value in values:
                if str(value) not in actual_values:
                    return False

    if "query_equals" in condition:
        for key, expected in condition["query_equals"].items():
            values = expected if isinstance(expected, list) else [expected]
            if qs.get(key, []) != [str(value) for value in values]:
                return False

    if "header_equals" in condition:
        for key, expected in condition["header_equals"].items():
            if headers is None or str(headers.get(key, "")) != str(expected):
                return False

    if "body_contains" in condition:
        if text is None or condition["body_contains"] not in text:
            return False

    return True


def plan_cost(case: Dict[str, Any], plan: Dict[str, Any]) -> Tuple[int, int]:
    originals = case.get("originals", {})
    total = 0
    item_count = 0
    for registration in plan.get("registrations", []):
        for item in registration.get("responses", []):
            item_count += 1
            if item.get("cost_bytes") is not None:
                total += int(item["cost_bytes"])
            elif item.get("original") in originals:
                original = originals[item["original"]]
                total += int(original.get("raw_bytes", original.get("byte_length", 0)))
            else:
                body = item.get("json", item.get("text", ""))
                if not isinstance(body, (str, bytes)):
                    body = json.dumps(body, ensure_ascii=False, sort_keys=True)
                total += len(body.encode("utf-8")) if isinstance(body, str) else len(body)
    return total, item_count


def _make_response_callback(
    registration: Dict[str, Any],
    item: Dict[str, Any],
    position: int,
    ledger: List[Dict[str, Any]],
    registry_items: List[str],
):
    registration_id = registration["id"]
    item_id = item.get("id", "%s[%d]" % (registration_id, position))
    original_id = item.get("original")

    def callback(request, context):
        if len(registry_items) > 1:
            registry_items.pop(0)
        entry = {
            "registration": registration_id,
            "position": position,
            "item_id": item_id,
            "original_id": original_id,
            "remaining_item_ids": list(registry_items),
        }
        ledger.append(entry)
        context.status_code = int(item.get("status", 200))
        context.headers.update(item.get("response_headers", {}))
        if item.get("exception"):
            exception_type = resolve_exception_type(item["exception"])
            raise exception_type(item.get("exception_message", "synthetic replay failure"))
        return item.get("json", item.get("text", ""))

    return callback


def _register_plan(mocker: requests_mock.Mocker, plan: Dict[str, Any], ledger: List[Dict[str, Any]]):
    for registration in plan.get("registrations", []):
        registry_items = [
            str(item.get("id", "%s[%d]" % (registration["id"], position)))
            for position, item in enumerate(registration.get("responses", []))
        ]
        response_list = []
        for position, item in enumerate(registration.get("responses", [])):
            callback = _make_response_callback(
                registration,
                item,
                position,
                ledger,
                registry_items,
            )
            response_kwargs: Dict[str, Any] = {
                "status_code": int(item.get("status", 200)),
                "headers": {
                    "X-Replay-Original-Id": item.get("original", ""),
                    **item.get("response_headers", {}),
                },
            }
            if item.get("format") == "text" or "text" in item:
                response_kwargs["text"] = callback
            else:
                response_kwargs["json"] = callback
            response_list.append(response_kwargs)

        condition = registration.get("additional_matcher")

        def additional_matcher(request, condition=condition):
            return evaluate_condition(
                condition,
                qs=getattr(request, "qs", {}),
                headers=getattr(request, "headers", {}),
                text=getattr(request, "text", None),
            )

        url = requests_mock.ANY if registration.get("url") == "@any" else registration.get("url")
        mocker.register_uri(
            registration.get("method", "GET").upper(),
            url,
            response_list=response_list,
            complete_qs=bool(registration.get("complete_qs", False)),
            additional_matcher=additional_matcher if condition else None,
            request_headers=registration.get("request_headers", {}),
        )


def _job_generator(case: Dict[str, Any], job_id: str, ledger: List[Dict[str, Any]]):
    job = case["jobs"][job_id]
    state = job["initial"]
    attempt = 1
    variables: Dict[str, Any] = {}

    while True:
        method, url, params = build_request_spec(job, state, variables)
        event: Dict[str, Any] = {
            "job": job_id,
            "step": state,
            "attempt": attempt,
            "method": method,
            "requested_url": canonical_request_url(url, params),
            "url": None,
            "status": None,
            "exception": None,
            "json": None,
            "text": None,
            "original_id": None,
            "consumed": [],
        }
        ledger_start = len(ledger)
        try:
            response = requests.request(method, url, params=params)
            event["url"] = response.request.url
            event["status"] = response.status_code
            event["original_id"] = response.headers.get("X-Replay-Original-Id")
            try:
                event["json"] = response.json()
            except ValueError:
                event["text"] = response.text
        except Exception as exc:  # outcome is part of the regression trace
            request_object = getattr(exc, "request", None)
            event["url"] = getattr(request_object, "url", event["requested_url"])
            event["exception"] = type(exc).__name__
        event["consumed"] = [dict(entry) for entry in ledger[ledger_start:]]
        if event["consumed"] and not event["original_id"]:
            event["original_id"] = event["consumed"][-1].get("original_id")

        yield event

        outcome = {
            "status": event["status"],
            "exception": event["exception"],
            "json": event["json"],
        }
        rule = select_transition(job, state, outcome, attempt)
        if rule is None:
            branch = "unhandled:%s:%s:%s" % (
                state,
                event["status"] or "",
                event["exception"] or "",
            )
            return branch

        if rule.get("set_vars"):
            variables = apply_transition_vars(rule, event["json"], variables)

        if rule.get("terminal"):
            return str(rule["terminal"])

        next_state = rule.get("next")
        if not next_state:
            return "terminal:%s" % state
        if next_state == state:
            attempt += 1
        else:
            state = next_state
            attempt = 1


def run_schedule(
    case: Dict[str, Any],
    plan: Dict[str, Any],
    word: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    ids = job_ids(case)
    ledger: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    branches: Dict[str, str] = {}

    with requests_mock.Mocker(case_sensitive=True) as mocker:
        _register_plan(mocker, plan, ledger)
        generators = {job_id: _job_generator(case, job_id, ledger) for job_id in ids}

        for symbol in word:
            job_id = ids[0] if symbol == "A" else ids[1]
            if job_id in branches:
                continue
            try:
                events.append(next(generators[job_id]))
            except StopIteration as stop:
                branches[job_id] = str(stop.value)

        cap = int(case["schedule"].get("max_attempts_per_job", 6))
        for job_id in ids:
            while job_id not in branches:
                job_events = [event for event in events if event["job"] == job_id]
                if len(job_events) >= cap:
                    branches[job_id] = "cap-exceeded"
                    break
                try:
                    events.append(next(generators[job_id]))
                except StopIteration as stop:
                    branches[job_id] = str(stop.value)

    for index, event in enumerate(events, start=1):
        event["global_index"] = index
    return events, branches


def _signature(event: Dict[str, Any]) -> Dict[str, Any]:
    return {key: event.get(key) for key in SIGNATURE_KEYS}


def _event_list(events: List[Dict[str, Any]], job_id: str) -> List[Dict[str, Any]]:
    return [event for event in events if event["job"] == job_id]


def _find_divergence(
    schedule: str,
    events: List[Dict[str, Any]],
    branches: Dict[str, str],
    expected_signatures: Dict[str, List[Dict[str, Any]]],
    expected_branches: Dict[str, str],
) -> Optional[Divergence]:
    best: Optional[Divergence] = None

    def consider(candidate: Divergence):
        nonlocal best
        if best is None or candidate.global_index < best.global_index:
            best = candidate

    for job_id, expected in expected_signatures.items():
        actual_events = _event_list(events, job_id)
        actual = [_signature(event) for event in actual_events]
        limit = max(len(expected), len(actual))
        for ordinal in range(limit):
            exp = expected[ordinal] if ordinal < len(expected) else None
            act = actual[ordinal] if ordinal < len(actual) else None
            if exp == act:
                continue
            actual_event = actual_events[ordinal] if ordinal < len(actual_events) else None
            global_index = actual_event["global_index"] if actual_event else len(events) + 1
            message = "job %s step #%d differs" % (job_id, ordinal + 1)
            consider(Divergence(
                schedule=schedule,
                job=job_id,
                ordinal=ordinal,
                global_index=global_index,
                kind="trace",
                expected=exp,
                actual=act,
                message=message,
                events=events,
            ))
            break

        expected_branch = expected_branches.get(job_id)
        actual_branch = branches.get(job_id)
        if expected_branch != actual_branch:
            consider(Divergence(
                schedule=schedule,
                job=job_id,
                ordinal=len(expected),
                global_index=len(events) + 1,
                kind="branch",
                expected=expected_branch,
                actual=actual_branch,
                message="job %s ends in a different branch" % job_id,
                events=events,
            ))

    return best


def evaluate_plan(
    case: Dict[str, Any],
    plan: Dict[str, Any],
    expected_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    expected_branches: Optional[Dict[str, str]] = None,
) -> PlanEvaluation:
    words = schedule_words(case)
    first_events, first_branches = run_schedule(case, plan, words[0])
    first_signatures = {
        job_id: [_signature(event) for event in _event_list(first_events, job_id)]
        for job_id in job_ids(case)
    }

    reference_signatures = expected_signatures or first_signatures
    reference_branches = expected_branches or first_branches
    earliest: Optional[Divergence] = None

    for word in words:
        if word == words[0]:
            events, branches = first_events, first_branches
        else:
            events, branches = run_schedule(case, plan, word)
        divergence = _find_divergence(
            word,
            events,
            branches,
            reference_signatures,
            reference_branches,
        )
        if divergence is not None and (
            earliest is None or divergence.global_index < earliest.global_index
        ):
            earliest = divergence

    cost, item_count = plan_cost(case, plan)
    required = case.get("required_receipts", [])
    observed_originals = {
        event.get("original_id")
        for event in first_events
        if event.get("original_id")
    }
    missing_required = [item for item in required if item not in observed_originals]

    equivalent = earliest is None and not missing_required
    return PlanEvaluation(
        plan_id=plan["id"],
        equivalent=equivalent,
        schedule_count=len(words),
        cost_bytes=cost,
        response_item_count=item_count,
        divergence=earliest,
        job_signatures=reference_signatures,
        branches=reference_branches,
        missing_required=missing_required,
    )


def evaluate_all(case: Dict[str, Any]) -> Tuple[PlanEvaluation, Dict[str, Any], List[PlanEvaluation]]:
    baseline_plan = next(
        plan for plan in case["plans"] if plan["id"] == case["baseline_plan_id"]
    )
    baseline = evaluate_plan(case, baseline_plan)
    if baseline.divergence is not None:
        raise CaseError(
            "Baseline case is not schedule-stable; first divergence: %s"
            % baseline.divergence.message
        )

    expected_signatures = baseline.job_signatures
    expected_branches = baseline.branches
    results = [baseline]
    for plan in case["plans"]:
        if plan["id"] == baseline.plan_id:
            continue
        results.append(evaluate_plan(
            case,
            plan,
            expected_signatures=expected_signatures,
            expected_branches=expected_branches,
        ))

    safe = [result for result in results if result.equivalent]
    safe.sort(key=lambda result: (
        result.cost_bytes,
        result.response_item_count,
        result.plan_id,
    ))
    selection = {
        "selected_plan_id": safe[0].plan_id if safe else None,
        "candidate_count": len(results),
        "safe_count": len(safe),
        "baseline_plan_id": baseline.plan_id,
        "cost_bytes": safe[0].cost_bytes if safe else None,
    }
    return baseline, selection, results


def certify_minimal(
    case: Dict[str, Any],
    plan: Dict[str, Any],
    expected_signatures: Dict[str, List[Dict[str, Any]]],
    expected_branches: Dict[str, str],
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []

    for registration_index, registration in enumerate(plan.get("registrations", [])):
        responses = registration.get("responses", [])
        for item_index, item in enumerate(responses):
            if len(responses) <= 1:
                continue
            candidate = copy.deepcopy(plan)
            removed = candidate["registrations"][registration_index]["responses"].pop(item_index)
            evaluation = evaluate_plan(
                case,
                candidate,
                expected_signatures,
                expected_branches,
            )
            checks.append({
                "kind": "response_item",
                "registration": registration["id"],
                "item_id": removed.get("id", item_index),
                "necessary": not evaluation.equivalent,
                "first_divergence": (
                    evaluation.divergence.message
                    if evaluation.divergence is not None
                    else None
                ),
            })

    for registration_index, registration in enumerate(plan.get("registrations", [])):
        candidate = copy.deepcopy(plan)
        candidate["registrations"].pop(registration_index)
        evaluation = evaluate_plan(
            case,
            candidate,
            expected_signatures,
            expected_branches,
        )
        checks.append({
            "kind": "registration",
            "registration": registration["id"],
            "necessary": not evaluation.equivalent,
            "first_divergence": (
                evaluation.divergence.message
                if evaluation.divergence is not None
                else None
            ),
        })

    return checks


def select_plan(case: Dict[str, Any]) -> Tuple[PlanEvaluation, Dict[str, Any], List[PlanEvaluation]]:
    return evaluate_all(case)
