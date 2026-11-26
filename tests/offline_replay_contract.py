"""Offline replay contract checker for two collection procedures.

The checker deliberately has no network integration.  Every URL is built with
the in-repository :mod:`yarl` library.  Large permitted domains are covered by
a small symbolic proof instead of materialising more than
``HARD_MAX_CONCRETE_CASES`` concrete ``(r, k, v)`` inputs in one decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Literal

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from yarl import URL  # noqa: E402

SCHEMA_VERSION = "offline-replay-contract/v1"
HARD_MAX_CONCRETE_CASES = 512
REPLAY_TARGET_PARAM = "target"
STEP_ORDER = ("catalog", "join", "query_modification", "replay")

MethodName = Literal["extend_query", "update_query"]
BindingKind = Literal["independent", "equal_to_k", "append_literal_to_k"]


class ContractConfigError(ValueError):
    """Raised when a supplier contract configuration is invalid."""


class ConcreteBudgetExceeded(RuntimeError):
    """Raised if a decision would materialise more than 512 inputs."""


@dataclass(frozen=True)
class StringDomain:
    alphabet: tuple[str, ...]
    min_length: int
    max_length: int

    @property
    def base(self) -> int:
        return len(self.alphabet)

    @property
    def char_index(self) -> dict[str, int]:
        return {char: index for index, char in enumerate(self.alphabet)}

    def count(self) -> int:
        return sum(self.base**length for length in range(self.min_length, self.max_length + 1))

    def contains(self, value: str) -> bool:
        return (
            self.min_length <= len(value) <= self.max_length
            and all(char in self.char_index for char in value)
        )

    def first(self) -> str:
        return self.alphabet[0] * self.min_length

    def rank(self, value: str) -> int | None:
        if not self.contains(value):
            return None
        before = sum(self.base**length for length in range(self.min_length, len(value)))
        lex_rank = 0
        indexes = self.char_index
        for char in value:
            lex_rank = lex_rank * self.base + indexes[char]
        return before + lex_rank

    def at_rank(self, rank: int) -> str:
        if rank < 0 or rank >= self.count():
            raise IndexError("string domain rank out of range")
        offset = rank
        before = 0
        for length in range(self.min_length, self.max_length + 1):
            block = self.base**length
            if offset < before + block:
                lex_rank = offset - before
                digits = [0] * length
                for position in range(length - 1, -1, -1):
                    digits[position] = lex_rank % self.base
                    lex_rank //= self.base
                return "".join(self.alphabet[digit] for digit in digits)
            before += block
        raise IndexError("string domain rank out of range")

    def iter_strings(self):
        for length in range(self.min_length, self.max_length + 1):
            for chars in product(self.alphabet, repeat=length):
                yield "".join(chars)


@dataclass(frozen=True)
class ValueBinding:
    kind: BindingKind
    suffix: str = ""

    def value_for_k(self, k: str) -> str | None:
        if self.kind == "independent":
            return None
        if self.kind == "equal_to_k":
            return k
        return k + self.suffix


@dataclass(frozen=True)
class InputSpace:
    references: tuple[str, ...]
    k: StringDomain
    v: StringDomain
    binding: ValueBinding

    def __post_init__(self) -> None:
        if self.binding.kind == "equal_to_k":
            if not set(self.k.alphabet).issubset(set(self.v.alphabet)):
                raise ContractConfigError("v alphabet must contain k alphabet when v equals k")
            if self.v.min_length > self.k.min_length or self.v.max_length < self.k.max_length:
                raise ContractConfigError("v length range must contain every v equal to k")
        elif self.binding.kind == "append_literal_to_k":
            suffix = self.binding.suffix
            if not set(self.k.alphabet).issubset(set(self.v.alphabet)):
                raise ContractConfigError("v alphabet must contain k alphabet for k+suffix")
            if any(char not in self.v.char_index for char in suffix):
                raise ContractConfigError("v alphabet must contain every literal suffix character")
            suffix_length = len(suffix)
            if (
                self.v.min_length > self.k.min_length + suffix_length
                or self.v.max_length < self.k.max_length + suffix_length
            ):
                raise ContractConfigError("v length range does not contain every k+suffix value")

    @property
    def dependent_v(self) -> bool:
        return self.binding.kind != "independent"

    def v_values_per_k(self) -> int:
        return 1 if self.dependent_v else self.v.count()

    def total_cases(self) -> int:
        return len(self.references) * self.k.count() * self.v_values_per_k()

    def first_case(self, r_index: int = 0) -> "Case":
        k = self.k.first()
        if self.dependent_v:
            v = self.binding.value_for_k(k)
            assert v is not None
        else:
            v = self.v.first()
        return Case(self.references[r_index], k, v, r_index)

    def is_permitted(self, case: "Case") -> bool:
        if case.r_index is not None:
            if not (0 <= case.r_index < len(self.references)):
                return False
            if self.references[case.r_index] != case.r:
                return False
        elif case.r not in self.references:
            return False
        if not self.k.contains(case.k):
            return False
        if self.dependent_v:
            expected = self.binding.value_for_k(case.k)
            return case.v == expected and self.v.contains(case.v)
        return self.v.contains(case.v)

    def case_index(self, case: "Case", r_index: int | None = None) -> int:
        if r_index is None:
            r_index = self.references.index(case.r)
        k_rank = self.k.rank(case.k)
        if k_rank is None:
            raise ContractConfigError(f"k is outside permitted domain: {case.k!r}")
        per_k = self.v_values_per_k()
        if self.dependent_v:
            v_rank = 0
        else:
            v_rank = self.v.rank(case.v)
            if v_rank is None:
                raise ContractConfigError(f"v is outside permitted domain: {case.v!r}")
        return r_index * self.k.count() * per_k + k_rank * per_k + v_rank

    def iter_cases(self, budget: "ConcreteBudget | None" = None):
        for r_index, r in enumerate(self.references):
            for k in self.k.iter_strings():
                if self.dependent_v:
                    v = self.binding.value_for_k(k)
                    assert v is not None
                    case = Case(r, k, v, r_index)
                    if budget is not None:
                        budget.record_generated(case)
                    yield case
                else:
                    for v in self.v.iter_strings():
                        case = Case(r, k, v, r_index)
                        if budget is not None:
                            budget.record_generated(case)
                        yield case


@dataclass(frozen=True)
class Case:
    r: str
    k: str
    v: str
    r_index: int | None = None

    def as_dict(self) -> dict[str, str]:
        return {"r": self.r, "k": self.k, "v": self.v}


@dataclass(frozen=True)
class ValueTemplate:
    literal: str | None = None
    variable: Literal["k", "v"] | None = None

    def resolve(self, case: Case) -> str:
        if self.literal is not None:
            return self.literal
        assert self.variable is not None
        return case.k if self.variable == "k" else case.v


@dataclass(frozen=True)
class ProcedureConfig:
    id: str
    catalog_url: str
    encoded: bool
    method: MethodName
    pairs: tuple[tuple[ValueTemplate, ValueTemplate], ...]
    replay_base_url: str | None = None


@dataclass
class ContractConfig:
    replay_base_url: str
    input_space: InputSpace
    procedures: tuple[ProcedureConfig, ProcedureConfig]
    max_concrete_cases: int = HARD_MAX_CONCRETE_CASES
    name: str = "offline-replay-contract"

    def __post_init__(self) -> None:
        if not 1 <= self.max_concrete_cases <= HARD_MAX_CONCRETE_CASES:
            raise ContractConfigError(
                "max_concrete_cases must be between 1 and 512, inclusive"
            )
        if len(self.procedures) != 2:
            raise ContractConfigError("exactly two procedures are required")


@dataclass
class ConcreteBudget:
    limit: int = HARD_MAX_CONCRETE_CASES
    generated: int = 0
    compared: int = 0

    def __post_init__(self) -> None:
        self.limit = min(self.limit, HARD_MAX_CONCRETE_CASES)

    def record_generated(self, case: Case | None = None) -> None:
        if self.generated >= self.limit:
            raise ConcreteBudgetExceeded(
                f"refusing to generate more than {HARD_MAX_CONCRETE_CASES} concrete inputs"
            )
        self.generated += 1

    def compare(self, case: Case) -> None:
        self.record_generated(case)
        self.compared += 1
        if self.compared > self.limit:
            raise ConcreteBudgetExceeded(
                f"refusing to compare more than {HARD_MAX_CONCRETE_CASES} concrete inputs"
            )


@dataclass(frozen=True)
class NormExpr:
    kind: Literal["literal", "k", "v"]
    value: str = ""


def _parse_string_domain(raw: dict[str, Any], field_name: str, *, k_field: bool) -> StringDomain:
    if not isinstance(raw, dict):
        raise ContractConfigError(f"{field_name} must be an object")
    alphabet_raw = raw.get("alphabet")
    if isinstance(alphabet_raw, str):
        chars = tuple(alphabet_raw)
    elif isinstance(alphabet_raw, list) and all(isinstance(char, str) and len(char) == 1 for char in alphabet_raw):
        chars = tuple(alphabet_raw)
    else:
        raise ContractConfigError(f"{field_name}.alphabet must be a string or one-character strings")
    if not chars:
        raise ContractConfigError(f"{field_name}.alphabet must not be empty")
    if any(ord(char) >= 128 for char in chars):
        raise ContractConfigError(f"{field_name}.alphabet must contain ASCII characters only")
    if len(set(chars)) != len(chars):
        raise ContractConfigError(f"{field_name}.alphabet must not contain duplicates")
    min_length = raw.get("min_length")
    max_length = raw.get("max_length")
    if not isinstance(min_length, int) or not isinstance(max_length, int):
        raise ContractConfigError(f"{field_name} requires integer min_length and max_length")
    if min_length < 0 or max_length < min_length:
        raise ContractConfigError(f"{field_name} has an invalid closed length range")
    if k_field and min_length < 1:
        raise ContractConfigError("k must allow at least one character")
    return StringDomain(tuple(sorted(chars, key=ord)), min_length, max_length)


def _parse_binding(raw: dict[str, Any] | None) -> ValueBinding:
    if raw is None:
        return ValueBinding("independent")
    if not isinstance(raw, dict):
        raise ContractConfigError("v.binding must be an object")
    kind = raw.get("kind", "independent")
    aliases = {
        "independent": "independent",
        "separate": "independent",
        "equal_to_k": "equal_to_k",
        "v_equals_k": "equal_to_k",
        "equals_k": "equal_to_k",
        "append_literal_to_k": "append_literal_to_k",
        "k_plus_literal": "append_literal_to_k",
        "v_equals_k_plus_literal": "append_literal_to_k",
    }
    normalized = aliases.get(kind)
    if normalized is None:
        raise ContractConfigError(f"unsupported v binding kind: {kind!r}")
    if normalized == "append_literal_to_k":
        suffix = raw.get("suffix")
        if not isinstance(suffix, str):
            raise ContractConfigError("append_literal_to_k requires a string suffix")
        return ValueBinding(normalized, suffix)  # type: ignore[arg-type]
    return ValueBinding(normalized)  # type: ignore[arg-type]


def _parse_template(raw: Any) -> ValueTemplate:
    if not isinstance(raw, dict):
        raise ContractConfigError("each key/value template must be an object")
    literal = raw.get("literal")
    variable = raw.get("var")
    if literal is not None and variable is not None:
        raise ContractConfigError("a template must set exactly one of literal or var")
    if literal is not None:
        if not isinstance(literal, str):
            raise ContractConfigError("literal template must contain a string")
        return ValueTemplate(literal=literal)
    if variable in ("k", "v"):
        return ValueTemplate(variable=variable)  # type: ignore[arg-type]
    raise ContractConfigError("template requires literal or var='k'/'v'")


def _parse_procedure(raw: dict[str, Any], index: int) -> ProcedureConfig:
    if not isinstance(raw, dict):
        raise ContractConfigError(f"procedures[{index}] must be an object")
    procedure_id = raw.get("id", f"procedure_{index + 1}")
    if not isinstance(procedure_id, str) or not procedure_id:
        raise ContractConfigError(f"procedures[{index}].id must be a non-empty string")
    catalog_url = raw.get("catalog_url")
    if not isinstance(catalog_url, str):
        raise ContractConfigError(f"procedures[{index}].catalog_url must be a string")
    encoded = raw.get("encoded", True)
    if not isinstance(encoded, bool):
        raise ContractConfigError(f"procedures[{index}].encoded must be boolean")
    method = raw.get("method")
    if method not in ("extend_query", "update_query"):
        raise ContractConfigError(f"procedures[{index}].method must be extend_query or update_query")
    pairs_raw = raw.get("pairs", [])
    if not isinstance(pairs_raw, list):
        raise ContractConfigError(f"procedures[{index}].pairs must be a list")
    pairs: list[tuple[ValueTemplate, ValueTemplate]] = []
    for pair_index, pair in enumerate(pairs_raw):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ContractConfigError(f"pairs[{pair_index}] must be a [key, value] pair")
        pairs.append((_parse_template(pair[0]), _parse_template(pair[1])))
    replay_base_url = raw.get("replay_base_url")
    if replay_base_url is not None and not isinstance(replay_base_url, str):
        raise ContractConfigError(f"procedures[{index}].replay_base_url must be a string")
    return ProcedureConfig(
        id=procedure_id,
        catalog_url=catalog_url,
        encoded=encoded,
        method=method,  # type: ignore[arg-type]
        pairs=tuple(pairs),
        replay_base_url=replay_base_url,
    )


def parse_config(raw: dict[str, Any]) -> ContractConfig:
    if not isinstance(raw, dict):
        raise ContractConfigError("configuration must be a JSON object")
    input_raw = raw.get("input")
    if not isinstance(input_raw, dict):
        raise ContractConfigError("configuration requires an input object")
    references = input_raw.get("references")
    if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
        raise ContractConfigError("input.references must be an ordered list of strings")
    if not references:
        raise ContractConfigError("input.references must not be empty")
    k_domain = _parse_string_domain(input_raw.get("k"), "input.k", k_field=True)
    v_raw = input_raw.get("v")
    v_domain = _parse_string_domain(v_raw, "input.v", k_field=False)
    binding = _parse_binding(v_raw.get("binding") if isinstance(v_raw, dict) else None)
    space = InputSpace(tuple(references), k_domain, v_domain, binding)

    replay_base_url = raw.get("replay_base_url")
    if not isinstance(replay_base_url, str) or not replay_base_url:
        raise ContractConfigError("replay_base_url must be a non-empty string")
    procedures_raw = raw.get("procedures")
    if not isinstance(procedures_raw, list) or len(procedures_raw) != 2:
        raise ContractConfigError("procedures must contain exactly two entries")
    procedures = tuple(_parse_procedure(item, index) for index, item in enumerate(procedures_raw))
    max_concrete_cases = raw.get("max_concrete_cases", HARD_MAX_CONCRETE_CASES)
    if not isinstance(max_concrete_cases, int):
        raise ContractConfigError("max_concrete_cases must be an integer")
    name = raw.get("name", "offline-replay-contract")
    if not isinstance(name, str):
        raise ContractConfigError("name must be a string")
    return ContractConfig(
        replay_base_url=replay_base_url,
        input_space=space,
        procedures=procedures,  # type: ignore[arg-type]
        max_concrete_cases=max_concrete_cases,
        name=name,
    )


def load_config(path: str | Path) -> ContractConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return parse_config(raw)


def _replay_base_for(config: ContractConfig, procedure: ProcedureConfig) -> str:
    return procedure.replay_base_url or config.replay_base_url


def join_reference(procedure: ProcedureConfig, reference: str) -> URL:
    catalog = URL(procedure.catalog_url, encoded=procedure.encoded)
    relative = URL(reference, encoded=procedure.encoded)
    return catalog.join(relative)


def render_procedure(
    config: ContractConfig, procedure: ProcedureConfig, case: Case
) -> dict[str, Any]:
    catalog = URL(procedure.catalog_url, encoded=procedure.encoded)
    relative = URL(case.r, encoded=procedure.encoded)
    joined = catalog.join(relative)
    pairs = [(key.resolve(case), value.resolve(case)) for key, value in procedure.pairs]
    mutated = getattr(joined, procedure.method)(pairs)
    replay_base = URL(_replay_base_for(config, procedure))
    outer = replay_base.with_query([(REPLAY_TARGET_PARAM, mutated.raw_path_qs)])
    return {
        "id": procedure.id,
        "outer_raw_path_qs": outer.raw_path_qs,
        "outer_url": str(outer),
        "inner_raw_path_qs": mutated.raw_path_qs,
        "stages": {
            "catalog": catalog.raw_path_qs,
            "join": joined.raw_path_qs,
            "query_modification": mutated.raw_path_qs,
            "replay": outer.raw_path_qs,
        },
    }


def first_different_step(rendered: tuple[dict[str, Any], dict[str, Any]]) -> str | None:
    left, right = rendered
    for step in STEP_ORDER:
        if left["stages"][step] != right["stages"][step]:
            return step
    return None


def evaluate_case(
    config: ContractConfig, case: Case, budget: ConcreteBudget | None = None
) -> dict[str, Any]:
    if budget is not None:
        budget.compare(case)
    rendered = tuple(render_procedure(config, procedure, case) for procedure in config.procedures)
    assert len(rendered) == 2
    different_step = first_different_step(rendered)
    ids = [procedure.id for procedure in config.procedures]
    return {
        "same": different_step is None,
        "first_different_step": different_step,
        "rendered": rendered,
        "outer_raw_path_qs": {
            ids[0]: rendered[0]["outer_raw_path_qs"],
            ids[1]: rendered[1]["outer_raw_path_qs"],
        },
        "outer_urls": {ids[0]: rendered[0]["outer_url"], ids[1]: rendered[1]["outer_url"]},
        "stage_raw_path_qs": {
            step: {
                ids[0]: rendered[0]["stages"][step],
                ids[1]: rendered[1]["stages"][step],
            }
            for step in STEP_ORDER
        },
    }


def _normalize_expr(template: ValueTemplate, space: InputSpace) -> NormExpr:
    if template.literal is not None:
        return NormExpr("literal", template.literal)
    assert template.variable is not None
    if template.variable == "k":
        return NormExpr("k", "")
    if space.binding.kind == "independent":
        return NormExpr("v", "")
    if space.binding.kind == "equal_to_k":
        return NormExpr("k", "")
    return NormExpr("k", space.binding.suffix)


def _singleton_constant(expr: NormExpr, space: InputSpace) -> str | None:
    if expr.kind == "literal":
        return expr.value
    if expr.kind == "k" and space.k.count() == 1:
        return space.k.first() + expr.value
    if expr.kind == "v" and space.v.count() == 1:
        return space.v.first()
    return None


def _eval_norm(expr: NormExpr, case: Case) -> str:
    if expr.kind == "literal":
        return expr.value
    if expr.kind == "k":
        return case.k + expr.value
    return case.v


def _exprs_equivalent_all(left: NormExpr, right: NormExpr, space: InputSpace) -> bool:
    left_constant = _singleton_constant(left, space)
    right_constant = _singleton_constant(right, space)
    if left_constant is not None and right_constant is not None:
        return left_constant == right_constant
    if left.kind != right.kind:
        return False
    if left.kind in ("literal", "k"):
        return left.value == right.value
    return True


def _earliest_expr_difference(
    space: InputSpace, left: NormExpr, right: NormExpr, r_index: int
) -> Case | None:
    k_candidates = [space.k.first()]
    if space.k.count() > 1:
        k_candidates.append(space.k.at_rank(1))
    for k in k_candidates:
        if space.dependent_v:
            v = space.binding.value_for_k(k)
            assert v is not None
            case = Case(space.references[r_index], k, v, r_index)
            if _eval_norm(left, case) != _eval_norm(right, case):
                return case
            continue
        v0 = space.v.first()
        case0 = Case(space.references[r_index], k, v0, r_index)
        if _eval_norm(left, case0) != _eval_norm(right, case0):
            return case0
        if space.v.count() > 1:
            v1 = space.v.at_rank(1)
            case1 = Case(space.references[r_index], k, v1, r_index)
            if _eval_norm(left, case1) != _eval_norm(right, case1):
                return case1
    return None


def _pairs_equivalent_all(
    left: ProcedureConfig, right: ProcedureConfig, space: InputSpace
) -> bool:
    if len(left.pairs) != len(right.pairs):
        return False
    for (left_key, left_value), (right_key, right_value) in zip(left.pairs, right.pairs):
        if not _exprs_equivalent_all(
            _normalize_expr(left_key, space), _normalize_expr(right_key, space), space
        ):
            return False
        if not _exprs_equivalent_all(
            _normalize_expr(left_value, space), _normalize_expr(right_value, space), space
        ):
            return False
    return True


def _earliest_pair_expression_difference(
    left: ProcedureConfig, right: ProcedureConfig, space: InputSpace, r_index: int
) -> Case | None:
    if len(left.pairs) != len(right.pairs):
        return space.first_case(r_index)
    candidates: list[Case] = []
    for (left_key, left_value), (right_key, right_value) in zip(left.pairs, right.pairs):
        for left_expr, right_expr in (
            (_normalize_expr(left_key, space), _normalize_expr(right_key, space)),
            (_normalize_expr(left_value, space), _normalize_expr(right_value, space)),
        ):
            if not _exprs_equivalent_all(left_expr, right_expr, space):
                candidate = _earliest_expr_difference(space, left_expr, right_expr, r_index)
                if candidate is not None:
                    candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda case: space.case_index(case, r_index))


def _earliest_key_hit(
    space: InputSpace, key_expr: NormExpr, target_key: str, r_index: int
) -> Case | None:
    r = space.references[r_index]
    if key_expr.kind == "literal":
        if key_expr.value != target_key:
            return None
        return space.first_case(r_index)

    if key_expr.kind == "v":
        if not space.v.contains(target_key):
            return None
        k = space.k.first()
        return Case(r, k, target_key, r_index)

    required_k: str | None
    if key_expr.value:
        if not target_key.endswith(key_expr.value):
            return None
        required_k = target_key[: -len(key_expr.value)]
    else:
        required_k = target_key
    if not space.k.contains(required_k):
        return None

    if space.dependent_v:
        v = space.binding.value_for_k(required_k)
        assert v is not None
    else:
        v = space.v.first()
    case = Case(r, required_k, v, r_index)
    return case if space.is_permitted(case) else None


def _earliest_existing_key_collision(
    procedure: ProcedureConfig, joined: URL, space: InputSpace, r_index: int
) -> Case | None:
    existing_keys = {key for key, _value in joined.query.items()}
    candidates: list[Case] = []
    for key_template, _value_template in procedure.pairs:
        key_expr = _normalize_expr(key_template, space)
        for existing_key in existing_keys:
            candidate = _earliest_key_hit(space, key_expr, existing_key, r_index)
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda case: space.case_index(case, r_index))


def _update_is_canonical(joined: URL) -> bool:
    existing_keys = {key for key, _value in joined.query.items()}
    probe_key = "__yarl_contract_probe__"
    suffix = 0
    while probe_key in existing_keys:
        suffix += 1
        probe_key = f"__yarl_contract_probe__{suffix}"
    probe = [(probe_key, "offline-contract")]
    return joined.extend_query(probe).raw_path_qs == joined.update_query(probe).raw_path_qs


def _coverage_dict(
    space: InputSpace, budget: ConcreteBudget, method: str, all_covered: bool
) -> dict[str, Any]:
    return {
        "all_permitted_inputs_covered": all_covered,
        "method": method,
        "total_inputs": space.total_cases(),
        "concrete_cases_generated": budget.generated,
        "concrete_cases_compared": budget.compared,
        "hard_max_concrete_cases": HARD_MAX_CONCRETE_CASES,
    }


def _divergent_report(
    config: ContractConfig,
    space: InputSpace,
    budget: ConcreteBudget,
    case: Case,
    result: dict[str, Any],
    coverage_method: str,
    proof: str | None = None,
) -> dict[str, Any]:
    r_index = case.r_index if case.r_index is not None else space.references.index(case.r)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": config.name,
        "status": "divergent",
        "replay_base_url": config.replay_base_url,
        "input_space": _input_space_dict(space),
        "coverage": _coverage_dict(space, budget, coverage_method, True),
        "first_divergence": {
            "input": case.as_dict(),
            "r_index": r_index,
            "order_index": space.case_index(case, r_index),
            "first_different_step": result["first_different_step"],
            "outer_raw_path_qs": result["outer_raw_path_qs"],
            "outer_urls": result["outer_urls"],
            "stage_raw_path_qs": result["stage_raw_path_qs"],
            "replayed_with_yarl": True,
        },
    }
    if proof:
        report["coverage"]["proof"] = proof
    return report


def _same_report(
    config: ContractConfig,
    space: InputSpace,
    budget: ConcreteBudget,
    coverage_method: str,
    proof: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": config.name,
        "status": "same",
        "replay_base_url": config.replay_base_url,
        "input_space": _input_space_dict(space),
        "coverage": _coverage_dict(space, budget, coverage_method, True),
        "equivalence_proof": proof,
        "first_divergence": None,
    }


def _inconclusive_report(
    config: ContractConfig,
    space: InputSpace,
    budget: ConcreteBudget,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": config.name,
        "status": "inconclusive",
        "replay_base_url": config.replay_base_url,
        "input_space": _input_space_dict(space),
        "coverage": _coverage_dict(space, budget, "bounded_concrete_scan", False),
        "reason": reason,
        "first_divergence": None,
    }


def _input_space_dict(space: InputSpace) -> dict[str, Any]:
    return {
        "references": list(space.references),
        "k": {
            "alphabet": "".join(space.k.alphabet),
            "min_length": space.k.min_length,
            "max_length": space.k.max_length,
            "count": space.k.count(),
        },
        "v": {
            "alphabet": "".join(space.v.alphabet),
            "min_length": space.v.min_length,
            "max_length": space.v.max_length,
            "count": space.v.count(),
            "binding": {
                "kind": space.binding.kind,
                "suffix": space.binding.suffix if space.binding.kind == "append_literal_to_k" else None,
            },
        },
        "total_inputs": space.total_cases(),
    }


def _exhaustive_evaluate(
    config: ContractConfig, budget: ConcreteBudget, start_r_index: int = 0
) -> dict[str, Any] | None:
    space = config.input_space
    for r_index in range(start_r_index, len(space.references)):
        r = space.references[r_index]
        for k in space.k.iter_strings():
            if space.dependent_v:
                v = space.binding.value_for_k(k)
                assert v is not None
                case = Case(r, k, v, r_index)
                result = evaluate_case(config, case, budget)
                if not result["same"]:
                    return _divergent_report(
                        config, space, budget, case, result, "exhaustive_concrete"
                    )
                if budget.compared >= budget.limit:
                    return None
            else:
                for v in space.v.iter_strings():
                    case = Case(r, k, v, r_index)
                    result = evaluate_case(config, case, budget)
                    if not result["same"]:
                        return _divergent_report(
                            config, space, budget, case, result, "exhaustive_concrete"
                        )
                    if budget.compared >= budget.limit:
                        return None
    return None


def evaluate_config(config: ContractConfig | dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(config, (str, Path)):
        parsed = load_config(config)
    elif isinstance(config, dict):
        parsed = parse_config(config)
    else:
        parsed = config
    config = parsed
    space = config.input_space
    budget = ConcreteBudget(config.max_concrete_cases)

    if space.total_cases() <= config.max_concrete_cases:
        divergence = _exhaustive_evaluate(config, budget)
        if divergence is not None:
            return divergence
        return _same_report(
            config,
            space,
            budget,
            "exhaustive_concrete",
            "Every permitted ordered input was generated, rendered by yarl, and compared.",
        )

    left_proc, right_proc = config.procedures
    left_catalog_raw = URL(left_proc.catalog_url, encoded=left_proc.encoded).raw_path_qs
    right_catalog_raw = URL(right_proc.catalog_url, encoded=right_proc.encoded).raw_path_qs
    if left_catalog_raw != right_catalog_raw:
        case = space.first_case(0)
        result = evaluate_case(config, case, budget)
        return _divergent_report(
            config,
            space,
            budget,
            case,
            result,
            "ordered_symbolic_search",
            "Catalog raw_path_qs values differ before reference resolution.",
        )

    left_replay_raw = URL(_replay_base_for(config, left_proc)).raw_path_qs
    right_replay_raw = URL(_replay_base_for(config, right_proc)).raw_path_qs
    if left_replay_raw != right_replay_raw:
        case = space.first_case(0)
        result = evaluate_case(config, case, budget)
        return _divergent_report(
            config,
            space,
            budget,
            case,
            result,
            "ordered_symbolic_search",
            "Replay base raw_path_qs values differ before wrapping the inner target.",
        )

    unsupported_from_r: int | None = None
    for r_index, r in enumerate(space.references):
        left_joined = join_reference(left_proc, r)
        right_joined = join_reference(right_proc, r)
        if left_joined.raw_path_qs != right_joined.raw_path_qs:
            case = space.first_case(r_index)
            result = evaluate_case(config, case, budget)
            return _divergent_report(
                config,
                space,
                budget,
                case,
                result,
                "ordered_symbolic_search",
                "Joined raw_path_qs values differ before query modification.",
            )

        same_pairs = _pairs_equivalent_all(left_proc, right_proc, space)
        same_method = left_proc.method == right_proc.method

        if same_method and same_pairs:
            continue

        if left_proc.method == "extend_query" and right_proc.method == "extend_query":
            candidate = _earliest_pair_expression_difference(
                left_proc, right_proc, space, r_index
            )
            if candidate is None:
                continue
            result = evaluate_case(config, candidate, budget)
            if result["same"]:
                raise AssertionError("symbolic extend_query witness did not replay as divergent")
            return _divergent_report(
                config,
                space,
                budget,
                candidate,
                result,
                "ordered_symbolic_search",
                "The first unequal encoded query slot determines the first raw target difference.",
            )

        methods = {left_proc.method, right_proc.method}
        if methods == {"extend_query", "update_query"} and same_pairs:
            if not left_proc.pairs:
                continue
            canonical = _update_is_canonical(left_joined)
            collision = _earliest_existing_key_collision(left_proc, left_joined, space, r_index)
            if collision is None and right_proc is not left_proc:
                collision_right = _earliest_existing_key_collision(
                    right_proc, right_joined, space, r_index
                )
                collision = collision_right
            if collision is None:
                if canonical:
                    continue
                candidate = space.first_case(r_index)
                proof = (
                    "The recorded query is not in yarl's canonical re-encoded form, so "
                    "update_query normalises it while extend_query preserves it."
                )
            else:
                candidate = collision
                proof = (
                    "The first permitted key equal to an existing decoded query key causes "
                    "update_query to overwrite/remove values while extend_query appends."
                )
            result = evaluate_case(config, candidate, budget)
            if result["same"]:
                raise AssertionError("symbolic extend/update witness did not replay as divergent")
            return _divergent_report(
                config, space, budget, candidate, result, "ordered_symbolic_search", proof
            )

        unsupported_from_r = r_index
        break

    if unsupported_from_r is None:
        return _same_report(
            config,
            space,
            budget,
            "domain_proof_with_yarl_replay",
            "For every reference, joined raw targets agree and the query operations are "
            "extensionally identical or are extend/update with canonical queries and no "
            "permitted key can collide with an existing query key.",
        )

    divergence = _exhaustive_evaluate(config, budget, unsupported_from_r)
    if divergence is not None:
        divergence["coverage"]["method"] = "bounded_concrete_scan"
        return divergence
    return _inconclusive_report(
        config,
        space,
        budget,
        "The configuration uses a large domain with an update_query shape that the symbolic "
        "proof does not cover; the first 512 ordered inputs were checked without a mismatch.",
    )


def replay_counterexample(
    config: ContractConfig | dict[str, Any] | str | Path, report: dict[str, Any]
) -> dict[str, str]:
    if isinstance(config, (str, Path)):
        parsed = load_config(config)
    elif isinstance(config, dict):
        parsed = parse_config(config)
    else:
        parsed = config
    if report.get("status") != "divergent" or report.get("first_divergence") is None:
        raise ContractConfigError("only a divergent report has a counterexample to replay")
    raw_case = report["first_divergence"]["input"]
    case = Case(raw_case["r"], raw_case["k"], raw_case["v"])
    replayed = evaluate_case(parsed, case)
    expected = report["first_divergence"]["outer_raw_path_qs"]
    if replayed["outer_raw_path_qs"] != expected:
        raise AssertionError(
            f"counterexample replay mismatch:\n{replayed['outer_raw_path_qs']!r}\n!=\n{expected!r}"
        )
    return replayed["outer_raw_path_qs"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Supplier contract JSON configuration")
    parser.add_argument("--write-report", type=Path, help="Optional path for the JSON report")
    args = parser.parse_args(argv)

    report = evaluate_config(args.config)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if args.write_report:
        args.write_report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if report["status"] == "same":
        return 0
    if report["status"] == "divergent":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
