"""Archive loan daily-report solver.

The input registry is JSON-compatible.  Dates are ISO calendar dates; EDTF
values are expanded through the installed ``edtf`` package.  Receipt copies
are selected whole, so correlations between endpoints on one copy are kept.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

MISSING = object()


class RegistryError(ValueError):
    pass


def first_key(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def to_ordinal(value: Any) -> int:
    if isinstance(value, date):
        return value.toordinal()
    if isinstance(value, int):
        raise RegistryError(
            f"bare integer {value!r} is not a date; use ISO YYYY-MM-DD or provide a registry domain"
        )
    text = str(value).strip()
    try:
        return date.fromisoformat(text).toordinal()
    except ValueError:
        try:
            struct = _parse_edtf_object(text).lower_strict()
            return date(struct.tm_year, struct.tm_mon or 1, struct.tm_mday or 1).toordinal()
        except Exception as exc:  # noqa: BLE001 - boundary conversion is reported as input error
            raise RegistryError(f"invalid date {value!r}: {exc}") from exc


def iso_date(ordinal: int) -> str:
    return date.fromordinal(ordinal).isoformat()


def _parse_edtf_object(value: str) -> Any:
    from edtf import parse_edtf

    return parse_edtf(value)


def _struct_to_ordinal(value: Any) -> int:
    return date(value.tm_year, value.tm_mon or 1, value.tm_mday or 1).toordinal()


def _bounds_to_domain(lower: Any, upper: Any) -> tuple[int, ...]:
    if not hasattr(lower, "tm_year"):
        lower = lower.lower_strict()
    if not hasattr(upper, "tm_year"):
        upper = upper.upper_strict()
    start = _struct_to_ordinal(lower)
    if isinstance(upper, float) and not math.isfinite(upper):
        raise RegistryError("open EDTF bound needs an explicit registry date domain")
    end = _struct_to_ordinal(upper)
    if end < start:
        start, end = end, start
    return tuple(range(start, end + 1))


def edtf_domain(value: str) -> tuple[int, ...]:
    obj = _parse_edtf_object(value)
    return tuple(sorted(set(_edtf_candidates(obj))))


def _edtf_candidates(obj: Any) -> Iterable[int]:
    if hasattr(obj, "objects"):
        for child in obj.objects:
            yield from _edtf_candidates(child)
        return

    lower = obj.lower_strict()
    upper = obj.upper_strict()

    if hasattr(obj, "lower") and hasattr(obj, "upper") and obj.lower is not None:
        lower = obj.lower.lower_strict()
    if hasattr(obj, "upper") and obj.upper is not None and hasattr(obj.upper, "upper_strict"):
        upper_value = obj.upper.upper_strict()
        if hasattr(upper_value, "tm_year"):
            upper = upper_value

    if isinstance(upper, float) and not math.isfinite(upper):
        raise RegistryError("open EDTF bound needs an explicit registry date domain")

    yield from _bounds_to_domain(lower, upper)


def domain_from_spec(
    spec: Any,
    named_domains: dict[str, Any] | None = None,
    symbol_ranges: dict[str, Any] | None = None,
) -> tuple[int, ...]:
    named_domains = named_domains or {}
    symbol_ranges = symbol_ranges or {}

    if spec is None:
        raise RegistryError("date specification is missing")

    if isinstance(spec, (list, tuple, set)):
        values: set[int] = set()
        for item in spec:
            values.update(domain_from_spec(item, named_domains, symbol_ranges))
        return tuple(sorted(values))

    if isinstance(spec, dict):
        if "dates" in spec:
            return tuple(sorted({to_ordinal(item) for item in spec["dates"]}))
        if "start" in spec and "end" in spec:
            start = to_ordinal(spec["start"])
            end = to_ordinal(spec["end"])
            return tuple(range(min(start, end), max(start, end) + 1))
        if "range" in spec:
            return domain_from_spec(spec["range"], named_domains, symbol_ranges)
        raise RegistryError(f"unsupported date domain object: {spec!r}")

    text = str(spec)
    if text in named_domains:
        return domain_from_spec(named_domains[text], named_domains, symbol_ranges)

    if text.startswith("domain:"):
        name = text.split(":", 1)[1]
        if name not in named_domains:
            raise RegistryError(f"unknown named date domain {name!r}")
        return domain_from_spec(named_domains[name], named_domains, symbol_ranges)

    try:
        date.fromisoformat(text)
        return (to_ordinal(text),)
    except ValueError:
        pass

    try:
        base_domain = edtf_domain(text)
    except Exception as exc:
        raise RegistryError(f"invalid EDTF date specification {text!r}: {exc}") from exc
    return apply_symbol_ranges(text, base_domain, symbol_ranges)


def apply_symbol_ranges(
    text: str,
    base_domain: tuple[int, ...],
    symbol_ranges: dict[str, Any],
) -> tuple[int, ...]:
    marker = next((token for token in ("%", "?", "~") if token in text and token in symbol_ranges), None)
    if marker is None:
        return base_domain

    policy = symbol_ranges[marker]
    if not isinstance(policy, dict):
        return domain_from_spec(policy)

    before = int(first_key(policy, "before", "minus", "previous") or 0)
    after = int(first_key(policy, "after", "plus", "next") or 0)
    symmetric = first_key(policy, "days", "plus_minus", "radius")
    if symmetric is not None:
        before = after = int(symmetric)

    expanded: set[int] = set()
    for value in base_domain:
        expanded.update(range(value - before, value + after + 1))
    return tuple(sorted(expanded))


@dataclass(frozen=True)
class Loan:
    id: str
    item: str
    order: int
    checkout_endpoint: str
    return_endpoint: str


@dataclass(frozen=True)
class ReceiptCopy:
    id: str
    claims: dict[str, tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    id: str
    copies: tuple[ReceiptCopy, ...]


@dataclass
class Registry:
    window_start: int | None
    window_end: int | None
    loans: dict[str, Loan]
    endpoint_loan: dict[str, str]
    receipts: dict[str, Receipt]
    base_domains: dict[str, tuple[int, ...]]
    named_domains: dict[str, Any]
    symbol_ranges: dict[str, Any]

    def loan_endpoints(self, loan_id: str) -> tuple[str, str]:
        loan = self.loans[loan_id]
        return loan.checkout_endpoint, loan.return_endpoint

    def component_endpoints(self, loan_ids: Iterable[str]) -> list[str]:
        endpoints: list[str] = []
        for loan_id in loan_ids:
            checkout, returned = self.loan_endpoints(loan_id)
            endpoints.extend((checkout, returned))
        return endpoints


class DSU:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> None:
        self.parent.setdefault(node, node)

    def find(self, node: str) -> str:
        self.add(node)
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def parse_window(raw_window: Any) -> tuple[int | None, int | None]:
    if raw_window is None:
        return None, None
    if isinstance(raw_window, dict):
        start = first_key(raw_window, "start", "from", "开始", "起")
        end = first_key(raw_window, "end", "to", "结束", "止")
        return (to_ordinal(start) if start is not None else None, to_ordinal(end) if end is not None else None)
    if isinstance(raw_window, (list, tuple)) and len(raw_window) == 2:
        return to_ordinal(raw_window[0]), to_ordinal(raw_window[1])
    if isinstance(raw_window, str) and ":" in raw_window:
        start, end = raw_window.split(":", 1)
        return to_ordinal(start), to_ordinal(end)
    raise RegistryError(f"invalid window {raw_window!r}")


def load_registry(case: dict[str, Any]) -> Registry:
    named_domains = first_key(case, "date_domains", "domains", "calendar_domains", "日历范围", "认可范围") or {}
    symbol_ranges = first_key(case, "symbol_ranges", "imprecise_symbols", "符号范围", "不精确符号") or {}
    window_start, window_end = parse_window(first_key(case, "window", "date_window", "日期窗口", "窗口"))

    raw_loans = first_key(case, "loans", "借件", "loans_registry") or []
    raw_endpoints = first_key(case, "endpoints", "端点") or []
    raw_receipts = first_key(case, "receipts", "回执") or []

    loans: dict[str, Loan] = {}
    endpoint_loan: dict[str, str] = {}
    base_domains: dict[str, tuple[int, ...]] = {}

    for index, raw_loan in enumerate(raw_loans):
        loan_id = str(first_key(raw_loan, "id", "loan_id", "编号", "借件编号") or f"L{index + 1}")
        if loan_id in loans:
            raise RegistryError(f"duplicate loan id {loan_id!r}")
        item = str(first_key(raw_loan, "item", "item_id", "original_id", "original", "原件编号", "原件"))
        order_value = first_key(raw_loan, "order", "sequence", "seq", "先后", "次序")
        order = int(order_value) if order_value is not None else index

        checkout_value = first_key(
            raw_loan,
            "checkout_endpoint",
            "checkout_ep",
            "checkout_date",
            "checkout_edtf",
            "checkout",
            "借出端点",
            "借出日期",
            "借出",
        )
        return_value = first_key(
            raw_loan,
            "return_endpoint",
            "return_ep",
            "return_date",
            "return_edtf",
            "return",
            "归还端点",
            "归还日期",
            "归还",
        )

        checkout_endpoint = f"{loan_id}:checkout"
        return_endpoint = f"{loan_id}:return"
        checkout_domain = None
        return_domain = None

        if checkout_value is not None:
            if isinstance(checkout_value, str) and not _looks_like_date_spec(checkout_value):
                checkout_endpoint = checkout_value
            else:
                checkout_domain = domain_from_spec(checkout_value, named_domains, symbol_ranges)

        if return_value is not None:
            if isinstance(return_value, str) and not _looks_like_date_spec(return_value):
                return_endpoint = return_value
            else:
                return_domain = domain_from_spec(return_value, named_domains, symbol_ranges)

        loans[loan_id] = Loan(loan_id, item, order, checkout_endpoint, return_endpoint)
        for endpoint in (checkout_endpoint, return_endpoint):
            if endpoint in endpoint_loan and endpoint_loan[endpoint] != loan_id:
                raise RegistryError(f"endpoint {endpoint!r} is assigned to multiple loans")
            endpoint_loan[endpoint] = loan_id

        if checkout_domain is not None:
            base_domains[checkout_endpoint] = checkout_domain
        if return_domain is not None:
            base_domains[return_endpoint] = return_domain

    for raw_endpoint in raw_endpoints:
        endpoint_id = str(first_key(raw_endpoint, "id", "endpoint", "端点编号"))
        loan_id = str(first_key(raw_endpoint, "loan", "loan_id", "借件"))
        if loan_id not in loans:
            raise RegistryError(f"endpoint {endpoint_id!r} refers to unknown loan {loan_id!r}")
        endpoint_loan[endpoint_id] = loan_id
        value = first_key(raw_endpoint, "value", "edtf", "date", "dates", "日期")
        if value is not None:
            base_domains[endpoint_id] = domain_from_spec(value, named_domains, symbol_ranges)

    receipts: dict[str, Receipt] = {}
    for r_index, raw_receipt in enumerate(raw_receipts):
        receipt_id = str(first_key(raw_receipt, "id", "receipt_id", "编号", "回执编号") or f"R{r_index + 1}")
        if receipt_id in receipts:
            raise RegistryError(f"duplicate receipt id {receipt_id!r}")

        raw_copies = first_key(raw_receipt, "copies", "transcripts", "抄件", "copies_raw")
        if raw_copies is None:
            raw_copies = [raw_receipt]

        copies: list[ReceiptCopy] = []
        for c_index, raw_copy in enumerate(raw_copies):
            copy_id = str(
                first_key(raw_copy, "id", "copy_id", "编号", "抄件编号")
                or f"{receipt_id}-copy-{c_index + 1}"
            )
            raw_claims = first_key(raw_copy, "dates", "date", "claims", "assertions", "日期", "主张") or {}
            claims: dict[str, tuple[int, ...]] = {}

            if isinstance(raw_claims, dict):
                claim_items = raw_claims.items()
                for endpoint, spec in claim_items:
                    claims[str(endpoint)] = domain_from_spec(spec, named_domains, symbol_ranges)
            elif isinstance(raw_claims, list):
                for raw_claim in raw_claims:
                    endpoint = str(first_key(raw_claim, "endpoint", "endpoint_id", "端点"))
                    spec = first_key(raw_claim, "value", "edtf", "date", "dates", "spec", "日期")
                    claims[endpoint] = domain_from_spec(spec, named_domains, symbol_ranges)
            else:
                raise RegistryError(f"copy {copy_id!r} has unsupported claims {raw_claims!r}")

            for endpoint in claims:
                if endpoint not in endpoint_loan:
                    raise RegistryError(f"receipt {receipt_id!r} refers to unknown endpoint {endpoint!r}")
            copies.append(ReceiptCopy(copy_id, claims))

        receipts[receipt_id] = Receipt(receipt_id, tuple(copies))

    return Registry(window_start, window_end, loans, endpoint_loan, receipts, base_domains, named_domains, symbol_ranges)


def _looks_like_date_spec(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    text = value.strip()
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        pass
    if text.startswith(("[", "{", "Y")) or "XX" in text or any(token in text for token in "?~%"):
        return True
    if "/" in text or ".." in text:
        return True
    return re.match(r"^-?\d{3,5}(?:-\d{2})?(?:-\d{2})?", text) is not None


def build_components(registry: Registry) -> list[dict[str, Any]]:
    dsu = DSU()
    for loan_id, loan in registry.loans.items():
        dsu.union(f"L:{loan_id}", f"I:{loan.item}")

    for receipt_id, receipt in registry.receipts.items():
        dsu.add(f"R:{receipt_id}")
        for copy in receipt.copies:
            for endpoint in copy.claims:
                loan_id = registry.endpoint_loan[endpoint]
                dsu.union(f"R:{receipt_id}", f"L:{loan_id}")

    roots: dict[str, dict[str, Any]] = {}

    def bucket(node: str) -> dict[str, Any]:
        root = dsu.find(node)
        if root not in roots:
            roots[root] = {"receipts": [], "loans": set(), "items": set()}
        return roots[root]

    for loan_id, loan in registry.loans.items():
        group = bucket(f"L:{loan_id}")
        group["loans"].add(loan_id)
        group["items"].add(loan.item)

    for receipt_id in registry.receipts:
        group = bucket(f"R:{receipt_id}")
        group["receipts"].append(receipt_id)

    components = []
    for index, group in enumerate(roots.values(), start=1):
        receipt_ids = sorted(group["receipts"])
        loan_ids = sorted(group["loans"], key=lambda loan_id: registry.loans[loan_id].order)
        item_ids = sorted(group["items"])
        component_id = "B" + str(index)
        components.append(
            {
                "id": component_id,
                "receipts": receipt_ids,
                "loans": loan_ids,
                "items": item_ids,
            }
        )
    return components


def intersect_domains(left: tuple[int, ...] | object, right: tuple[int, ...]) -> tuple[int, ...] | object:
    if left is MISSING:
        return right
    right_set = set(right)
    result = tuple(value for value in left if value in right_set)  # type: ignore[union-attr]
    return result if result else MISSING


def selection_iter(
    registry: Registry,
    receipt_ids: list[str],
    loan_ids: list[str],
    allow_fallback: bool = False,
    all_known_domains: dict[str, tuple[int, ...]] | None = None,
):
    all_known_domains = all_known_domains or {}
    component_loan_set = set(loan_ids)

    def visit(
        index: int,
        chosen: dict[str, ReceiptCopy],
        intersections: dict[str, tuple[int, ...] | object],
    ):
        if index == len(receipt_ids):
            domains: dict[str, tuple[int, ...]] = {}
            missing: list[str] = []
            for endpoint, loan_id in registry.endpoint_loan.items():
                if loan_id not in component_loan_set:
                    continue
                domain = intersections.get(endpoint, MISSING)
                if domain is MISSING:
                    domain = registry.base_domains.get(endpoint, MISSING)
                if domain is MISSING and allow_fallback:
                    domain = all_known_domains.get(endpoint, MISSING)
                if domain is MISSING or len(domain) == 0:  # type: ignore[arg-type]
                    missing.append(endpoint)
                else:
                    domains[endpoint] = domain  # type: ignore[assignment]
            if missing:
                yield chosen, domains, missing
            else:
                yield chosen, domains, []
            return

        receipt = registry.receipts[receipt_ids[index]]
        for copy in receipt.copies:
            next_intersections = dict(intersections)
            compatible = True
            for endpoint, claim_domain in copy.claims.items():
                merged = intersect_domains(next_intersections.get(endpoint, MISSING), claim_domain)
                if merged is MISSING:
                    compatible = False
                    break
                next_intersections[endpoint] = merged
            if compatible:
                chosen[receipt.id] = copy
                yield from visit(index + 1, chosen, next_intersections)
                chosen.pop(receipt.id, None)

    yield from visit(0, {}, {})


def item_loans(registry: Registry, loan_ids: Iterable[str], item: str) -> list[Loan]:
    loans = [registry.loans[loan_id] for loan_id in loan_ids if registry.loans[loan_id].item == item]
    return sorted(loans, key=lambda loan: loan.order)


def search_item_schedule(
    loans: list[Loan],
    domains: dict[str, tuple[int, ...]],
    day: int | None = None,
    mode: str = "any",
) -> tuple[tuple[tuple[int, int], ...], ...] | None:
    endpoint_domains: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for loan in loans:
        checkout_domain = domains.get(loan.checkout_endpoint, ())
        return_domain = domains.get(loan.return_endpoint, ())
        if not checkout_domain or not return_domain:
            return None
        endpoint_domains.append((checkout_domain, return_domain))

    @lru_cache(maxsize=None)
    def dfs(index: int, previous_return: int, occupied: bool):
        if index == len(loans):
            if mode == "out" and not occupied:
                return None
            return ()

        checkout_domain, return_domain = endpoint_domains[index]
        checkout_start = bisect_left(checkout_domain, previous_return)
        for checkout in checkout_domain[checkout_start:]:
            return_start = bisect_left(return_domain, checkout + 1)
            for returned in return_domain[return_start:]:
                occupies = day is not None and checkout <= day < returned
                if mode == "home" and occupies:
                    continue
                suffix = dfs(index + 1, returned, occupied or occupies)
                if suffix is not None:
                    return ((checkout, returned),) + suffix
        return None

    lower_bound = date.min.toordinal()
    return dfs(0, lower_bound, False)


def schedule_witness(
    registry: Registry,
    loans: list[Loan],
    assignment: tuple[tuple[int, int], ...] | None,
    day: int | None,
) -> list[dict[str, Any]]:
    if assignment is None:
        return []
    rows = []
    for loan, (checkout, returned) in zip(loans, assignment):
        rows.append(
            {
                "loan_id": loan.id,
                "item_id": loan.item,
                "order": loan.order,
                "checkout": iso_date(checkout),
                "return": iso_date(returned),
                "out_on_witness_day": None if day is None else checkout <= day < returned,
            }
        )
    return rows


def receipt_witness(
    registry: Registry,
    chosen: dict[str, ReceiptCopy],
    domains: dict[str, tuple[int, ...]],
    loan_ids: list[str],
    item_results: dict[str, dict[str, Any]],
    day: int,
    bound: str,
) -> dict[str, Any]:
    selected_receipts = sorted(chosen)
    endpoint_values: dict[str, str] = {}
    supporting_receipts: dict[str, list[str]] = {}

    for loan_id in loan_ids:
        for endpoint in registry.loan_endpoints(loan_id):
            if endpoint not in domains:
                continue
            candidates = domains[endpoint]
            chosen_value = None
            for item_id, result in item_results.items():
                for row in result.get(bound + "_rows", []):
                    if endpoint.endswith(":checkout") and row["loan_id"] == loan_id:
                        chosen_value = row["checkout"]
                    if endpoint.endswith(":return") and row["loan_id"] == loan_id:
                        chosen_value = row["return"]
            if chosen_value is None:
                chosen_value = iso_date(candidates[0])
            endpoint_values[endpoint] = chosen_value

    for receipt_id in selected_receipts:
        for endpoint in chosen[receipt_id].claims:
            supporting_receipts.setdefault(endpoint, []).append(receipt_id)

    loans_rows: list[dict[str, Any]] = []
    for item_id in sorted(item_results):
        loans_rows.extend(item_results[item_id].get(bound + "_rows", []))
    loans_rows.sort(key=lambda row: row["order"])

    for row in loans_rows:
        loan = registry.loans[row["loan_id"]]
        endpoint_values[loan.checkout_endpoint] = row["checkout"]
        endpoint_values[loan.return_endpoint] = row["return"]

    return {
        "witness_day": iso_date(day),
        "bound": bound,
        "receipt_set": selected_receipts,
        "receipt_copies": {receipt_id: chosen[receipt_id].id for receipt_id in selected_receipts},
        "endpoints": endpoint_values,
        "supporting_receipts": {key: sorted(value) for key, value in sorted(supporting_receipts.items())},
        "loans": loans_rows,
    }


def solve_selection_for_day(
    registry: Registry,
    component: dict[str, Any],
    chosen: dict[str, ReceiptCopy],
    domains: dict[str, tuple[int, ...]],
    missing: list[str],
    day: int,
) -> dict[str, Any] | None:
    if missing:
        return None

    item_results: dict[str, dict[str, Any]] = {}
    min_value = 0
    max_value = 0

    for item_id in component["items"]:
        loans = item_loans(registry, component["loans"], item_id)
        home = search_item_schedule(loans, domains, day, "home")
        out = search_item_schedule(loans, domains, day, "out")
        if home is None and out is None:
            return None

        forced_out = home is None
        can_out = out is not None
        min_value += 1 if forced_out else 0
        max_value += 1 if can_out else 0

        min_assignment = out if forced_out else home
        max_assignment = out if can_out else home
        item_results[item_id] = {
            "forced_out": forced_out,
            "can_out": can_out,
            "min_rows": schedule_witness(registry, loans, min_assignment, day),
            "max_rows": schedule_witness(registry, loans, max_assignment, day),
        }

    min_witness = receipt_witness(registry, chosen, domains, component["loans"], item_results, day, "min")
    max_witness = receipt_witness(registry, chosen, domains, component["loans"], item_results, day, "max")
    return {"min": min_value, "max": max_value, "min_witness": min_witness, "max_witness": max_witness}


def solve_component_day(
    registry: Registry,
    component: dict[str, Any],
    day: int,
) -> dict[str, Any] | None:
    best_min: dict[str, Any] | None = None
    best_max: dict[str, Any] | None = None
    feasible_selections = 0

    for chosen, domains, missing in selection_iter(
        registry, component["receipts"], component["loans"]
    ):
        result = solve_selection_for_day(registry, component, chosen, domains, missing, day)
        if result is None:
            continue
        feasible_selections += 1
        if best_min is None or result["min"] < best_min["min"]:
            best_min = result
        if best_max is None or result["max"] > best_max["max"]:
            best_max = result

    if best_min is None or best_max is None:
        return None

    witness = {
        "min": best_min["min"],
        "max": best_max["max"],
        "min_witness": best_min["min_witness"],
        "max_witness": best_max["max_witness"],
        "feasible_receipt_selections": feasible_selections,
    }
    return witness


def component_feasible(
    registry: Registry,
    component: dict[str, Any],
    receipt_ids: list[str] | None = None,
    allow_fallback: bool = False,
    all_known_domains: dict[str, tuple[int, ...]] | None = None,
) -> bool:
    receipt_ids = component["receipts"] if receipt_ids is None else receipt_ids
    subset_component = dict(component)
    subset_component["receipts"] = receipt_ids
    day = registry.window_start or date.min.toordinal()
    for chosen, domains, missing in selection_iter(
        registry,
        receipt_ids,
        component["loans"],
        allow_fallback=allow_fallback,
        all_known_domains=all_known_domains,
    ):
        if solve_selection_for_day(registry, subset_component, chosen, domains, missing, day) is not None:
            return True
    return False


def all_known_endpoint_domains(registry: Registry) -> dict[str, tuple[int, ...]]:
    domains: dict[str, set[int]] = {endpoint: set() for endpoint in registry.endpoint_loan}
    for endpoint, domain in registry.base_domains.items():
        domains.setdefault(endpoint, set()).update(domain)
    for receipt in registry.receipts.values():
        for copy in receipt.copies:
            for endpoint, domain in copy.claims.items():
                domains.setdefault(endpoint, set()).update(domain)
    return {endpoint: tuple(sorted(values)) for endpoint, values in domains.items()}


def relaxed_fallback_domains(
    all_known: dict[str, tuple[int, ...]],
) -> dict[str, tuple[int, ...]]:
    values = [value for domain in all_known.values() for value in domain]
    if not values:
        return {}
    start = min(values) - 1
    end = max(values) + 1
    span = tuple(range(start, end + 1))
    return {endpoint: span for endpoint in all_known}


def minimal_conflict(
    registry: Registry,
    component: dict[str, Any],
    all_known: dict[str, tuple[int, ...]],
) -> list[str]:
    fallback = relaxed_fallback_domains(all_known)
    conflict = list(component["receipts"])
    changed = True
    while changed:
        changed = False
        for receipt_id in list(conflict):
            trial = [candidate for candidate in conflict if candidate != receipt_id]
            if not component_feasible(registry, component, trial, True, fallback):
                conflict = trial
                changed = True
                break
    return conflict


def diagnose_component(
    registry: Registry,
    component: dict[str, Any],
    all_known: dict[str, tuple[int, ...]],
) -> dict[str, Any] | None:
    if component_feasible(registry, component):
        return None

    unsupported_endpoints = []
    for loan_id in component["loans"]:
        for endpoint in registry.loan_endpoints(loan_id):
            claimed = any(
                endpoint in copy.claims
                for receipt_id in component["receipts"]
                for copy in registry.receipts[receipt_id].copies
            )
            if not claimed and endpoint not in registry.base_domains:
                unsupported_endpoints.append(endpoint)

    conflict = minimal_conflict(registry, component, all_known)
    reasons = sample_conflict_reasons(registry, component)
    gap_type = "missing_evidence" if unsupported_endpoints and not conflict else "no_common_interpretation"
    return {
        "component_id": component["id"],
        "type": gap_type,
        "stuck_receipts": conflict or component["receipts"],
        "all_receipts": component["receipts"],
        "original_ids": component["items"],
        "loan_ids": component["loans"],
        "unsupported_endpoints": unsupported_endpoints,
        "sample_failed_interpretations": reasons,
    }


def sample_conflict_reasons(registry: Registry, component: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    reasons = []
    for chosen, domains, missing in selection_iter(
        registry, component["receipts"], component["loans"]
    ):
        if missing:
            reasons.append(
                {
                    "receipt_copies": {rid: copy.id for rid, copy in chosen.items()},
                    "missing_endpoints": missing,
                    "reason": "endpoint has no agreed date domain",
                }
            )
        else:
            day = registry.window_start or date.min.toordinal()
            result = solve_selection_for_day(registry, component, chosen, domains, missing, day)
            if result is None:
                reasons.append(
                    {
                        "receipt_copies": {rid: copy.id for rid, copy in chosen.items()},
                        "reason": "dates violate checkout/return order or confirmed loan sequence",
                    }
                )
        if len(reasons) >= limit:
            break
    return reasons


def window_segments(registry: Registry, all_known: dict[str, tuple[int, ...]]) -> list[tuple[int, int]]:
    if registry.window_start is None or registry.window_end is None:
        raise RegistryError("a report window is required")
    start = registry.window_start
    end = registry.window_end
    sentinel = end + 1
    breaks = {start, sentinel}
    for domain in all_known.values():
        for value in domain:
            if start <= value <= sentinel:
                breaks.add(value)
    ordered = sorted(breaks)
    segments = []
    for left, right in zip(ordered, ordered[1:]):
        segment_end = right - 1
        if left <= end:
            segments.append((left, min(segment_end, end)))
    return segments


def witness_out_count(witness: dict[str, Any], day: int) -> int:
    count = 0
    for loan in witness.get("loans", []):
        checkout = to_ordinal(loan["checkout"])
        returned = to_ordinal(loan["return"])
        if checkout <= day < returned:
            count += 1
    return count


def merge_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in rows:
        batch_counts = [(item["component_id"], item["min_out"], item["max_out"]) for item in row["batch_counts"]]
        same_conclusion = (
            merged
            and merged[-1]["min_out"] == row["min_out"]
            and merged[-1]["max_out"] == row["max_out"]
            and merged[-1]["_batch_keys"] == batch_counts
        )
        witness_still_attains = False
        if same_conclusion:
            previous = merged[-1]
            witness_still_attains = (
                witness_out_count(previous["min_witness"], row["start_ordinal"]) == row["min_out"]
                and witness_out_count(previous["max_witness"], row["start_ordinal"]) == row["max_out"]
            )

        if same_conclusion and witness_still_attains:
            previous = merged[-1]
            previous["end"] = row["end"]
            previous["date_end"] = row["date_end"]
            previous["days"] = previous["days"] + row["days"]
        else:
            row["_batch_keys"] = batch_counts
            merged.append(dict(row))
    for row in merged:
        row.pop("end", None)
        row.pop("_batch_keys", None)
    return merged


def build_report(case: dict[str, Any]) -> dict[str, Any]:
    registry = load_registry(case)
    if registry.window_start is None or registry.window_end is None:
        raise RegistryError("window.start and window.end are required")
    if registry.window_start > registry.window_end:
        raise RegistryError("window start must not be after end")

    components = build_components(registry)
    all_known = all_known_endpoint_domains(registry)
    segments = window_segments(registry, all_known)

    gaps: list[dict[str, Any]] = []
    active_components: list[dict[str, Any]] = []
    component_status: list[dict[str, Any]] = []

    for component in components:
        gap = diagnose_component(registry, component, all_known)
        if gap is None:
            active_components.append(component)
            component_status.append(
                {
                    "component_id": component["id"],
                    "status": "ok",
                    "receipts": component["receipts"],
                    "loans": component["loans"],
                    "original_ids": component["items"],
                }
            )
        else:
            gaps.append(gap)
            component_status.append(
                {
                    "component_id": component["id"],
                    "status": "gap",
                    "receipts": component["receipts"],
                    "loans": component["loans"],
                    "original_ids": component["items"],
                }
            )

    raw_rows: list[dict[str, Any]] = []
    for start_ordinal, end_ordinal in segments:
        total_min = 0
        total_max = 0
        min_witnesses: dict[str, Any] = {}
        max_witnesses: dict[str, Any] = {}
        batch_counts = []

        for component in active_components:
            result = solve_component_day(registry, component, start_ordinal)
            if result is None:
                continue
            total_min += result["min"]
            total_max += result["max"]
            min_witnesses[component["id"]] = result["min_witness"]
            max_witnesses[component["id"]] = result["max_witness"]
            batch_counts.append(
                {"component_id": component["id"], "min_out": result["min"], "max_out": result["max"]}
            )

        raw_rows.append(
            {
                "start_ordinal": start_ordinal,
                "end": end_ordinal,
                "date_start": iso_date(start_ordinal),
                "date_end": iso_date(end_ordinal),
                "days": end_ordinal - start_ordinal + 1,
                "min_out": total_min,
                "max_out": total_max,
                "batch_counts": batch_counts,
                "min_witness": merge_global_witness(min_witnesses, start_ordinal, "min"),
                "max_witness": merge_global_witness(max_witnesses, start_ordinal, "max"),
            }
        )

    runs = merge_runs(raw_rows)
    for run in runs:
        run.pop("start_ordinal", None)

    blocked_items = sorted({item for gap in gaps for item in gap["original_ids"]})
    reported_items = sorted({item for component in active_components for item in component["items"]})

    return {
        "window": {"start": iso_date(registry.window_start), "end": iso_date(registry.window_end)},
        "calendar": "ISO/Gregorian via python-edtf boundary data",
        "reported_original_ids": reported_items,
        "blocked_original_ids": blocked_items,
        "daily_runs": runs,
        "gaps": gaps,
        "batches": component_status,
        "segment_count_before_compression": len(raw_rows),
    }


def merge_global_witness(component_witnesses: dict[str, Any], day: int, bound: str) -> dict[str, Any]:
    receipt_copies: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    supporting: dict[str, list[str]] = {}
    loans: list[dict[str, Any]] = []
    receipt_set: list[str] = []

    for component_id in sorted(component_witnesses):
        witness = component_witnesses[component_id]
        receipt_set.extend(witness["receipt_set"])
        receipt_copies.update(witness["receipt_copies"])
        endpoints.update(witness["endpoints"])
        for endpoint, receipts in witness["supporting_receipts"].items():
            supporting.setdefault(endpoint, []).extend(receipts)
        loans.extend(witness["loans"])

    loans.sort(key=lambda row: (row["item_id"], row["order"], row["loan_id"]))
    return {
        "witness_day": iso_date(day),
        "bound": bound,
        "receipt_set": sorted(receipt_set),
        "receipt_copies": receipt_copies,
        "endpoints": endpoints,
        "supporting_receipts": {key: sorted(set(value)) for key, value in sorted(supporting.items())},
        "loans": loans,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# 馆藏借件核账日报", ""]
    window = report["window"]
    lines.append(f"窗口：{window['start']} 至 {window['end']}（首尾按自然日；借出日计在外，归还日不计）")
    lines.append("")
    lines.append("| 日期段 | 天数 | 最少在外 | 最多在外 | 说明 |")
    lines.append("| --- | ---: | ---: | ---: | --- |")
    for run in report["daily_runs"]:
        date_range = run["date_start"] if run["date_start"] == run["date_end"] else f"{run['date_start']} → {run['date_end']}"
        note = "；".join(f"{batch['component_id']} {batch['min_out']}-{batch['max_out']}" for batch in run["batch_counts"])
        lines.append(f"| {date_range} | {run['days']} | {run['min_out']} | {run['max_out']} | {note} |")

    if report["gaps"]:
        lines.extend(["", "## 缺口", ""])
        for gap in report["gaps"]:
            receipt_text = ", ".join(gap["stuck_receipts"]) or "无完整回执"
            lines.append(
                f"- {gap['component_id']}：原件 {', '.join(gap['original_ids'])}；"
                f"卡住回执 {receipt_text}。"
            )
            if gap.get("unsupported_endpoints"):
                lines.append(f"  - 缺少可共同解释的端点：{', '.join(gap['unsupported_endpoints'])}")
            if gap.get("type") == "no_common_interpretation":
                lines.append("  - 现有抄件整份选择后仍无满足日期一致、借件先后和同原件不重叠的解释。")
    else:
        lines.extend(["", "所有回执批次均有共同解释。"])

    lines.extend(["", "## 核账见证", ""])
    for run in report["daily_runs"]:
        lines.append(f"### {run['date_start']} → {run['date_end']}（见证日 {run['min_witness']['witness_day']}）")
        for bound_label, key in [("最少", "min_witness"), ("最多", "max_witness")]:
            witness = run[key]
            lines.append(f"- {bound_label}：回执组 `{', '.join(witness['receipt_set']) or '无'}`")
            for loan in witness["loans"]:
                lines.append(
                    f"  - {loan['item_id']} / {loan['loan_id']}：{loan['checkout']} 借出，{loan['return']} 归还"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute attainable library loan counts from EDTF receipts")
    parser.add_argument("registry", nargs="?", type=Path, help="JSON registry path")
    parser.add_argument("--window", help="ISO start:end, overrides registry window")
    parser.add_argument("--format", choices=("json", "md"), default="json")
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args(argv)

    if args.registry is None:
        parser.print_help()
        return 2

    case = json.loads(args.registry.read_text(encoding="utf-8"))
    if args.window:
        start_text, end_text = args.window.split(":", 1)
        case["window"] = [start_text, end_text]

    report = build_report(case)
    output = render_markdown(report) if args.format == "md" else json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + ("" if output.endswith("\n") else "\n"), encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
