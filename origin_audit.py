#!/usr/bin/env python3
"""Local identity reconciliation entry for the PysonDB duplicate-id case.

The tool keeps stable ``origin`` identities for rows even when their literal JSON
values are equal.  It replays two workstation streams as all interleavings that
preserve each stream's order, checks sparse return/error receipts, and reports
the minimum/maximum number of distinct originals touched in a posting range.

Input is a small JSON case document.  A complete example is available from
``python origin_audit.py --write-example origin-audit-example.json``.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

UNSET = object()
ORIGIN_META_KEYS = (
    "$origin",
    "@origin",
    "_origin",
    "__origin__",
    "origin_id",
    "original_id",
    "original_no",
    "origin",
    "uid",
    "oid",
    "原件号",
    "原件",
)

OP_ALIASES = {
    "getbyid": "getById",
    "find": "getById",
    "id": "getById",
    "get": "get",
    "getnum": "get",
    "getall": "getAll",
    "all": "getAll",
    "getbyquery": "getByQuery",
    "getby": "getByQuery",
    "where": "getByQuery",
    "query": "getByQuery",
    "research": "reSearch",
    "search": "reSearch",
    "regex": "reSearch",
    "updatebyid": "updateById",
    "updateid": "updateById",
    "save": "updateById",
    "update": "updateByQuery",
    "updatebyquery": "updateByQuery",
    "updatewhere": "updateByQuery",
    "patch": "updateByQuery",
    "backfill": "updateByQuery",
    "supplement": "updateByQuery",
    "补录": "updateByQuery",
    "deletebyid": "deleteById",
    "removebyid": "deleteById",
    "delete": "deleteById",
    "remove": "deleteById",
    "deleteall": "deleteAll",
    "truncate": "deleteAll",
    "add": "add",
    "create": "add",
    "insert": "add",
    "append": "add",
    "addmany": "addMany",
    "insertmany": "addMany",
    "createmany": "addMany",
}

ERROR_ALIASES = {
    "idnotfound": "IdNotFoundError",
    "idnotfounderror": "IdNotFoundError",
    "datanotfound": "DataNotFoundError",
    "datanotfounderror": "DataNotFoundError",
    "schema": "SchemaError",
    "schemaerror": "SchemaError",
    "valueerror": "ValueError",
    "keyerror": "KeyError",
    "indexerror": "IndexError",
    "typeerror": "TypeError",
    "reerror": "error",
    "regexerror": "error",
    "jsondecodeerror": "JSONDecodeError",
}


class CaseFormatError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def safe_int(value: Any) -> int:
    return int(value)


def mask_count(mask: int) -> int:
    bit_count = getattr(mask, "bit_count", None)
    return bit_count() if callable(bit_count) else bin(mask).count("1")


def simple_error_name(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "error" and type(exc).__module__.startswith("re"):
        return "re.error"
    return name


def error_matches(expected: str, actual: str) -> bool:
    wanted = expected.strip()
    if wanted in {"*", "any", "Any", "Exception", "Error", "失败", "报错"}:
        return True
    wanted_simple = wanted.rsplit(".", 1)[-1]
    actual_simple = actual.rsplit(".", 1)[-1]
    wanted_norm = re.sub(r"[^a-z0-9]+", "", wanted_simple.lower())
    actual_norm = re.sub(r"[^a-z0-9]+", "", actual_simple.lower())
    mapped = ERROR_ALIASES.get(wanted_norm)
    if mapped:
        wanted_norm = re.sub(r"[^a-z0-9]+", "", mapped.lower())
    return wanted_norm == actual_norm or wanted.endswith(actual) or actual.endswith(wanted)


def receipt_equal(expected: Any, actual: Any, id_field: str = "id", key: Optional[str] = None) -> bool:
    if expected is ... or expected == "<any>" or expected == "*":
        return True
    if isinstance(expected, str) and expected.startswith("regex:"):
        return re.search(expected[6:], canonical(actual)) is not None
    if type(expected) is type(actual):
        if isinstance(expected, dict):
            if set(expected) != set(actual):
                # Origin assertions are audit-only and do not exist in PysonDB output.
                comparable_expected = {
                    k: v for k, v in expected.items() if k not in ORIGIN_META_KEYS
                }
                comparable_actual = {k: v for k, v in actual.items() if k not in ORIGIN_META_KEYS}
                if set(comparable_expected) == set(comparable_actual):
                    return all(
                        receipt_equal(comparable_expected[k], comparable_actual[k], id_field, k)
                        for k in comparable_expected
                    )
                return False
            return all(
                receipt_equal(expected[k], actual[k], id_field, k) for k in expected
            )
        if isinstance(expected, list):
            return len(expected) == len(actual) and all(
                receipt_equal(e, a, id_field, key) for e, a in zip(expected, actual)
            )
        return expected == actual
    if key == id_field and expected is not None and actual is not None:
        try:
            return safe_int(expected) == safe_int(actual)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    return expected == actual


# ---------------------------------------------------------------------------
# Case loading and normalization
# ---------------------------------------------------------------------------


def _first_key(obj: Dict[str, Any], keys: Sequence[str]) -> Tuple[Optional[str], Any]:
    for key in keys:
        if key in obj:
            return key, obj[key]
    return None, UNSET


def _parse_if_json_string(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{\"" and text[-1] in "}]\"":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def load_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl" or path.suffix.lower() == ".ndjson":
        rows = []
        for line_no, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CaseFormatError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaseFormatError(f"{path}: invalid JSON: {exc}") from exc


def load_case(path_str: Optional[str]) -> Dict[str, Any]:
    if path_str:
        path = Path(path_str).expanduser().resolve()
        if not path.exists():
            raise CaseFormatError(f"case path does not exist: {path}")
        if path.is_dir():
            return load_case_dir(path)
        obj = load_structured_file(path)
        if not isinstance(obj, dict):
            raise CaseFormatError("case file must contain a JSON object")
        obj.setdefault("_case_dir", str(path.parent))
        return obj

    cwd = Path.cwd()
    candidates = [
        cwd / "origin-audit-case.json",
        cwd / "origin_audit_case.json",
        cwd / "audit-case.json",
        cwd / "case.json",
        cwd / "audit" / "case.json",
        cwd / "cases" / "case.json",
    ]
    candidates.extend(sorted(cwd.glob("*.case.json")))
    for candidate in candidates:
        if candidate.exists():
            obj = load_structured_file(candidate)
            if isinstance(obj, dict):
                obj.setdefault("_case_dir", str(candidate.parent))
                return obj
    raise CaseFormatError(
        "no case supplied and no origin-audit-case.json/case.json found; "
        "use --case PATH or --initial/--a-log/--b-log, or run --write-example"
    )


def read_tabular_file(path: Path, delimiter: str = ",") -> List[Dict[str, Any]]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def parse_text_log(text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    call_pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
                continue
        except json.JSONDecodeError:
            pass

        split = re.split(r"\s*(?:=>|->|⇒|→|返回值?|回执|result\s*:|response\s*:)\s*", line, maxsplit=1)
        call_text = split[0]
        receipt_text = split[1].strip() if len(split) == 2 else ""
        match = call_pattern.search(call_text)
        event: Dict[str, Any] = {"line": line_no}
        seq_match = re.search(r"(?:^|[\s#:.-])(\d{1,8})(?=[\s:.-]|$)", call_text[: match.start()] if match else call_text)
        if seq_match:
            event["seq"] = int(seq_match.group(1))
        if not match:
            # Chinese prose lines such as "读取 17 返回 IdNotFoundError".
            prose_op = None
            lowered = line.lower()
            if "getbyid" in lowered or "按编号读" in line or "读 17" in line or "读取17" in line:
                prose_op = "getById"
            pk_match = re.search(r"\b(\d+)\b", line)
            if prose_op and pk_match:
                event.update({"op": prose_op, "pk": int(pk_match.group(1))})
            else:
                continue
        else:
            parsed = parse_call_string(call_text[match.start():])
            if not parsed:
                continue
            op, args, kwargs = parsed
            event.update({"op": op, "args": args, "kwargs": kwargs})

        if receipt_text:
            receipt_text = receipt_text.strip()
            error_name = receipt_text.split()[0].strip("`:：,，")
            if re.sub(r"[^A-Za-z]", "", error_name) in ERROR_ALIASES or error_name.endswith("Error"):
                event["error"] = error_name
            else:
                parsed_receipt = _parse_if_json_string(receipt_text)
                if parsed_receipt != receipt_text:
                    event["result"] = parsed_receipt
                else:
                    try:
                        event["result"] = ast.literal_eval(receipt_text)
                    except Exception:
                        event["result_text"] = receipt_text
        events.append(event)
    return events


def read_artifact(path_str: str, kind: str = "data") -> Any:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise CaseFormatError(f"{kind} path does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_tabular_file(path)
    if suffix in {".tsv", ".tab"}:
        return read_tabular_file(path, "\t")
    if suffix in {".log", ".txt", ".text"}:
        if kind == "log":
            return parse_text_log(path.read_text(encoding="utf-8"))
        return path.read_text(encoding="utf-8")
    return load_structured_file(path)


def case_from_cli_sources(args: argparse.Namespace) -> Dict[str, Any]:
    raw_case: Dict[str, Any]
    if args.case:
        raw_case = load_case(args.case)
    else:
        raw_case = {}
    if args.case_id:
        raw_case["case_id"] = args.case_id
    if args.id_field:
        raw_case["id_field"] = args.id_field
    if args.origin_field:
        raw_case["origin_field"] = args.origin_field
    if args.initial:
        raw_case["initial"] = read_artifact(args.initial, "initial")
    if args.a_log or args.b_log:
        streams = raw_case.setdefault("streams", {})
        if args.a_log:
            streams[args.a_name] = read_artifact(args.a_log, "log")
        if args.b_log:
            streams[args.b_name] = read_artifact(args.b_log, "log")
    if args.checkpoints:
        raw_case["checkpoints"] = read_artifact(args.checkpoints, "checkpoints")
    return raw_case


def load_case_dir(directory: Path) -> Dict[str, Any]:
    preferred = ["case.json", "origin-audit-case.json", "audit-case.json"]
    for name in preferred:
        candidate = directory / name
        if candidate.exists():
            obj = load_structured_file(candidate)
            if isinstance(obj, dict):
                obj["_case_dir"] = str(directory)
                return obj

    skip_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
    files: List[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if any(part in skip_parts or part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        if path.suffix.lower() in {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".log", ".txt"}:
            files.append(path)

    def score(path: Path, words: Sequence[str]) -> int:
        haystack = str(path.relative_to(directory)).lower()
        haystack += " " + path.stem.lower()
        return sum(3 if w.lower() in path.stem.lower() else 1 for w in words if w.lower() in haystack)

    initial_words = ("initial", "merge", "merged", "row_order", "rows", "origin", "base", "before",
                     "snapshot", "原件", "行序", "合并", "原库", "旧账")
    checkpoint_words = ("checkpoint", "posting", "过账", "点位")
    a_words = ("station-a", "stream-a", "log-a", "a-log", "left", "workstation-a",
               "工位一", "工位a", "甲", "a流水")
    b_words = ("station-b", "stream-b", "log-b", "b-log", "right", "workstation-b",
               "工位二", "工位b", "乙", "b流水")

    def best(words: Sequence[str]) -> Optional[Path]:
        ranked = sorted(((score(p, words), p) for p in files), key=lambda x: x[0], reverse=True)
        return ranked[0][1] if ranked and ranked[0][0] > 0 else None

    case: Dict[str, Any] = {"_case_dir": str(directory)}
    initial_path = best(initial_words)
    checkpoint_path = best(checkpoint_words)
    a_path = best(a_words)
    b_path = best(b_words)

    if initial_path:
        case["initial"] = read_artifact(str(initial_path), "initial")
    if checkpoint_path:
        case["checkpoints"] = read_artifact(str(checkpoint_path), "checkpoints")
    if a_path or b_path:
        streams: Dict[str, Any] = {}
        if a_path:
            streams["A"] = read_artifact(str(a_path), "log")
        if b_path:
            streams["B"] = read_artifact(str(b_path), "log")
        case["streams"] = streams

    # Generic two-log fallback when names are simply a.jsonl / b.jsonl.
    if not a_path or not b_path:
        simple_a = directory / "a.jsonl"
        simple_b = directory / "b.jsonl"
        if simple_a.exists() and simple_b.exists():
            case["streams"] = {
                "A": read_artifact(str(simple_a), "log"),
                "B": read_artifact(str(simple_b), "log"),
            }
    return case


def extract_origin(row: Dict[str, Any], explicit_key: Optional[str], fallback: str) -> Tuple[str, Dict[str, Any]]:
    record = dict(row)
    origin = None
    if explicit_key and explicit_key in record:
        origin = record.pop(explicit_key)
    else:
        for key in ORIGIN_META_KEYS:
            if key in record:
                origin = record.pop(key)
                break
    if origin is None:
        origin = fallback
    return str(origin), record


def rows_from_value(value: Any) -> List[Dict[str, Any]]:
    value = _parse_if_json_string(value)
    if value is UNSET or value is None:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("data"), list):
            return [dict(r) for r in value["data"]]
        for key in ("rows", "records", "items", "row_order", "order"):
            if isinstance(value.get(key), list):
                return [r if isinstance(r, dict) else {"_value": r} for r in value[key]]
        # A mapping of origin -> record.
        if all(isinstance(v, dict) for v in value.values()):
            rows = []
            for origin, record in value.items():
                item = dict(record)
                item.setdefault("$origin", origin)
                rows.append(item)
            return rows
    if isinstance(value, list):
        return [dict(r) if isinstance(r, dict) else {"_value": r} for r in value]
    raise CaseFormatError("initial rows must be a list, PysonDB {'data': [...]}, or origin map")


def normalize_initial(case: Dict[str, Any], id_field: str) -> List[Tuple[str, str]]:
    explicit_origin_key = case.get("origin_field") or case.get("origin_key")
    initial_value, initial = _first_key(
        case,
        (
            "initial_rows",
            "merge_rows",
            "merged_rows",
            "rows_at_merge",
            "actual_rows",
            "initial",
            "merged",
            "merge_time",
            "at_merge",
            "snapshot",
            "base",
            "before",
        ),
    )
    if initial is UNSET:
        if any(key in case for key in ("row_order", "order", "originals", "records_by_origin", "rows", "data")):
            initial = case
        else:
            raise CaseFormatError("case is missing initial/merge-time rows")

    # Separate row order + originals map representation.
    if isinstance(initial, dict):
        order = initial.get("row_order") or initial.get("order")
        originals = initial.get("originals") or initial.get("records_by_origin")
        if isinstance(order, list) and originals is not UNSET:
            raw_rows: List[Dict[str, Any]] = []
            if isinstance(originals, dict):
                for origin in order:
                    if origin not in originals:
                        raise CaseFormatError(f"row order references unknown origin {origin!r}")
                    row = dict(originals[origin])
                    row.setdefault("$origin", origin)
                    raw_rows.append(row)
            elif isinstance(originals, list):
                by_origin = {}
                for row in originals:
                    origin = row.get("$origin") or row.get("origin") or row.get("original_id")
                    if origin is not None:
                        by_origin[str(origin)] = row
                for origin in order:
                    if str(origin) not in by_origin:
                        raise CaseFormatError(f"row order references unknown origin {origin!r}")
                    raw_rows.append(dict(by_origin[str(origin)]))
            else:
                raise CaseFormatError("originals must be a map or list")
        else:
            raw_rows = rows_from_value(initial)
    else:
        raw_rows = rows_from_value(initial)

    # Parallel origin list is supported.
    origin_order = case.get("origin_order") or case.get("origins")
    if isinstance(origin_order, list) and len(origin_order) == len(raw_rows):
        for row, origin in zip(raw_rows, origin_order):
            row.setdefault("$origin", origin)

    state: List[Tuple[str, str]] = []
    used: Dict[str, int] = {}
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raw = {"value": raw}
        # Wrapper form: {"origin": "O1", "record": {...}}.
        wrapper_origin = None
        for key in ("origin", "origin_id", "original", "original_ref", "原件"):
            if key in raw and isinstance(raw[key], (str, int)) and any(
                isinstance(raw.get(k), dict) for k in ("record", "row", "values", "data")
            ):
                wrapper_origin = raw.pop(key)
                break
        if wrapper_origin is not None:
            record_key = next(k for k in ("record", "row", "values", "data") if isinstance(raw.get(k), dict))
            record = dict(raw.pop(record_key))
            record.update({k: v for k, v in raw.items() if k not in ORIGIN_META_KEYS})
            origin = str(wrapper_origin)
        else:
            origin, record = extract_origin(raw, explicit_origin_key, f"O{index + 1}")

        if id_field in record and record[id_field] is not None and not isinstance(record[id_field], bool):
            try:
                record[id_field] = int(record[id_field])
            except (TypeError, ValueError):
                pass

        if origin in used:
            used[origin] += 1
            origin = f"{origin}#{used[origin]}"
        else:
            used[origin] = 1
        state.append((origin, canonical(record)))
    return state


def parse_call_string(text: str) -> Optional[Tuple[str, List[Any], Dict[str, Any]]]:
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", text.strip(), re.S)
    if not match:
        return None
    op = match.group(1)
    arg_text = match.group(2).strip()
    args: List[Any] = []
    if arg_text:
        # Logs usually use JSON-ish dicts; quote bare keys and evaluate safely-ish.
        try:
            args = json.loads("[" + arg_text + "]")
        except json.JSONDecodeError:
            try:
                args = ast.literal_eval("[" + arg_text + "]")
            except Exception:
                return op, [], {"_unparsed_arguments": arg_text}
    return op, list(args), {}


@dataclasses.dataclass(frozen=True)
class Event:
    side: str
    index: int
    ref: str
    op: str
    call: Dict[str, Any]
    expected_result: Any = UNSET
    expected_error: Optional[str] = None
    has_constraint: bool = False
    label: str = ""
    order_key: Tuple[Any, ...] = ()
    tags: Tuple[str, ...] = ()
    raw_ref: str = ""


def _event_ref(raw: Dict[str, Any], side: str, index: int) -> Tuple[str, str]:
    for key in ("id", "event_id", "request_id", "req_id", "seq", "sequence", "line", "line_no", "no"):
        if key in raw and raw[key] not in (None, ""):
            value = raw[key]
            return str(value), f"{side}:{value}"
    generated = str(index + 1)
    return generated, f"{side}:#{index + 1}"


def normalize_op(raw: Dict[str, Any]) -> str:
    for key in ("op", "method", "api", "call", "action", "operation", "function", "cmd", "命令", "操作"):
        if key in raw and isinstance(raw[key], str):
            text = raw[key].strip()
            parsed = parse_call_string(text)
            if parsed:
                text = parsed[0]
            return OP_ALIASES.get(re.sub(r"[^a-z0-9]+", "", text.lower()), text)
    for key, value in raw.items():
        if isinstance(value, str) and key.lower() in {"call", "request", "invocation"}:
            parsed = parse_call_string(value)
            if parsed:
                return OP_ALIASES.get(re.sub(r"[^a-z0-9]+", "", parsed[0].lower()), parsed[0])
    raise CaseFormatError(f"cannot determine operation for event: {raw!r}")


def _args_to_call(op: str, args: List[Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    call = dict(kwargs)
    positional_names = {
        "getById": ["pk"],
        "deleteById": ["pk"],
        "updateById": ["pk", "data"],
        "getByQuery": ["query"],
        "updateByQuery": ["query", "data"],
        "add": ["data"],
        "addMany": ["items"],
        "get": ["num"],
        "reSearch": ["key", "pattern"],
    }.get(op, [])
    for name, value in zip(positional_names, args):
        call.setdefault(name, value)
    return call


def normalize_event(raw: Any, side: str, index: int) -> Event:
    if isinstance(raw, str):
        parsed_line = parse_call_string(raw)
        if not parsed_line:
            raise CaseFormatError(f"{side}[{index}] plain log line is not a call: {raw!r}")
        op_name, args, kwargs = parsed_line
        raw = {"op": op_name, "args": args, "kwargs": kwargs}
    if not isinstance(raw, dict):
        raise CaseFormatError(f"{side}[{index}] event must be object")

    raw = dict(raw)
    raw_ref, ref = _event_ref(raw, side, index)
    op = normalize_op(raw)

    embedded_args: List[Any] = []
    for key in ("args", "arguments", "params", "parameters"):
        if key in raw:
            value = raw[key]
            if isinstance(value, list):
                embedded_args.extend(value)
            elif isinstance(value, dict):
                raw.setdefault("kwargs", {}).update(value)
            else:
                embedded_args.append(value)
    if isinstance(raw.get("kwargs"), dict):
        kwargs = dict(raw["kwargs"])
    else:
        kwargs = {}

    parsed_call_text = None
    for key in ("call", "request", "invocation"):
        if isinstance(raw.get(key), str):
            parsed_call_text = parse_call_string(raw[key])
            if parsed_call_text:
                op = OP_ALIASES.get(re.sub(r"[^a-z0-9]+", "", parsed_call_text[0].lower()), op)
                embedded_args.extend(parsed_call_text[1])
                kwargs.update(parsed_call_text[2])

    call = _args_to_call(op, embedded_args, kwargs)
    for target, keys in {
        "pk": ("pk", "id", "target_id", "record_id", "编号"),
        "query": ("query", "where", "filter", "db_dataset", "criteria", "条件"),
        "data": ("data", "new_data", "new_dataset", "patch", "values", "update", "record", "补录内容"),
        "items": ("items", "records", "rows", "data_list"),
        "num": ("num", "limit", "count"),
        "key": ("key", "field"),
        "pattern": ("pattern", "regex", "re"),
    }.items():
        for key in keys:
            if key in raw and target not in call:
                call[target] = raw[key]
                break

    # Deprecated PysonDB update(query, data).
    if op == "updateByQuery" and "query" not in call and embedded_args:
        call["query"] = embedded_args[0]
        if len(embedded_args) > 1:
            call["data"] = embedded_args[1]

    expected_result = UNSET
    expected_error = None
    has_constraint = False

    receipt_container = raw.get("receipt")
    if isinstance(receipt_container, dict):
        if "return" in receipt_container:
            expected_result = _parse_if_json_string(receipt_container["return"])
            has_constraint = True
        if "result" in receipt_container:
            expected_result = _parse_if_json_string(receipt_container["result"])
            has_constraint = True
        if "error" in receipt_container:
            expected_error = str(receipt_container["error"])
            has_constraint = True
    for key in ("expected_result", "result", "returns", "return_value", "response", "output", "ret", "返回", "回执"):
        if key in raw and expected_result is UNSET:
            value = _parse_if_json_string(raw[key])
            if isinstance(value, str) and re.sub(r"[^A-Za-z]", "", value) in ERROR_ALIASES:
                expected_error = value
            else:
                expected_result = value
            has_constraint = True
    for key in ("expected_error", "error", "exception", "raises", "failure", "err", "报错"):
        if key in raw and raw[key] not in (None, False):
            value = raw[key]
            expected_error = "*" if value is True else str(value)
            has_constraint = True
    if raw.get("status") in {"error", "failed", "failure"} and expected_error is None:
        expected_error = "*"
        has_constraint = True

    label = str(raw.get("label") or raw.get("kind") or raw.get("type") or raw.get("备注") or "")
    tags = tuple(str(t) for t in raw.get("tags", ()))
    if any(token in (label + " " + " ".join(tags) + " " + ref).lower() for token in ("补录", "supplement", "backfill")):
        tags = tuple(set(tags + ("supplement",)))

    ts = raw.get("timestamp") or raw.get("time") or raw.get("at") or raw.get("ts")
    order_key: Tuple[Any, ...]
    if isinstance(ts, (int, float)):
        order_key = (0, float(ts))
    elif ts:
        order_key = (1, str(ts))
    else:
        order_key = (2, index)

    return Event(
        side=side,
        index=index,
        ref=ref,
        raw_ref=raw_ref,
        op=op,
        call=call,
        expected_result=expected_result,
        expected_error=expected_error,
        has_constraint=has_constraint,
        label=label,
        order_key=order_key,
        tags=tags,
    )


def normalize_streams(case: Dict[str, Any]):  # type: ignore[no-untyped-def]
    from collections import OrderedDict

    sides: "OrderedDict[str, List[Any]]" = OrderedDict()
    raw_streams = case.get("streams") or case.get("stations") or case.get("workstations")
    if isinstance(raw_streams, dict):
        for side, value in raw_streams.items():
            sides[str(side)] = stream_events_from_value(value)
    elif isinstance(raw_streams, list):
        for i, value in enumerate(raw_streams):
            side = str(value.get("id") or value.get("side") or value.get("name") or chr(65 + i)) if isinstance(value, dict) else chr(65 + i)
            sides[side] = stream_events_from_value(value)
    else:
        for side, keys in {
            "A": ("stream_a", "station_a", "workstation_a", "log_a", "requests_a", "a"),
            "B": ("stream_b", "station_b", "workstation_b", "log_b", "requests_b", "b"),
        }.items():
            for key in keys:
                if key in case:
                    sides[side] = stream_events_from_value(case[key])
                    break

    if len(sides) < 2:
        for key, value in case.items():
            if key.endswith(("_log", "_stream", "_requests", "_flow")) and isinstance(value, (list, dict)):
                side = key.split("_", 1)[0].upper()
                sides.setdefault(side, stream_events_from_value(value))
            if len(sides) == 2:
                break
    if len(sides) < 2:
        raise CaseFormatError("case must contain two workstation streams")

    preferred = case.get("side_order") or case.get("sides")
    if preferred and len(preferred) >= 2:
        ordered = OrderedDict()
        for side in preferred[:2]:
            ordered[str(side)] = sides[str(side)]
        for side, events in sides.items():
            if side not in ordered:
                ordered[side] = events
        sides = ordered
    else:
        # Sensible A/B/left/right ordering when present.
        keys = list(sides)
        if len(keys) > 2:
            chosen = None
            for pair in (("A", "B"), ("left", "right"), ("station-a", "station-b"), ("1", "2")):
                if all(x in sides for x in pair):
                    chosen = pair
                    break
            if chosen:
                sides = OrderedDict((x, sides[x]) for x in chosen)
            else:
                sides = OrderedDict(
                    [(keys[0], sides[keys[0]]), (keys[1], sides[keys[1]])]
                )
    return sides


def stream_events_from_value(value: Any) -> List[Any]:
    value = _parse_if_json_string(value)
    if isinstance(value, dict):
        for key in ("requests", "events", "entries", "flow", "items", "流水", "请求"):
            if isinstance(value.get(key), list):
                return value[key]
        if all(isinstance(v, (dict, str)) for v in value.values()):
            return list(value.values())
    if isinstance(value, list):
        return value
    raise CaseFormatError("stream must be a list or contain requests/events list")


@dataclasses.dataclass(frozen=True)
class Checkpoint:
    id: str
    cuts: Dict[str, Any]
    order: int
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)


def normalize_checkpoints(case: Dict[str, Any], sides: Sequence[str]) -> List[Checkpoint]:
    raw_cps = case.get("checkpoints") or case.get("posting_points") or case.get("postings") or case.get("过账点") or []
    if isinstance(raw_cps, dict):
        raw_cps = [dict(v, id=k) if isinstance(v, dict) else {"id": k, "positions": v} for k, v in raw_cps.items()]
    if not isinstance(raw_cps, list):
        raise CaseFormatError("checkpoints must be a list or map")

    result: List[Checkpoint] = []
    for order, raw in enumerate(raw_cps):
        if not isinstance(raw, dict):
            raw = {"id": str(raw)}
        cp_id = str(raw.get("id") or raw.get("name") or raw.get("point") or raw.get("过账点") or f"CP{order + 1}")
        cuts: Dict[str, Any] = {}
        positions = raw.get("positions") or raw.get("after") or raw.get("at") or raw.get("through") or raw
        if isinstance(positions, dict):
            for side in sides:
                for key in (side, side.lower(), side.upper()):
                    if key in positions:
                        cuts[side] = positions[key]
                        break
        if isinstance(raw.get("cuts"), dict):
            cuts.update(raw["cuts"])
        for side in sides:
            if side not in cuts:
                for key in (f"{side}_after", f"{side}_through", f"{side}_event", f"{side}_seq"):
                    if key in raw:
                        cuts[side] = raw[key]
                        break
        if len(sides) == 2 and isinstance(positions, list) and len(positions) == 2:
            cuts.setdefault(sides[0], positions[0])
            cuts.setdefault(sides[1], positions[1])
        result.append(Checkpoint(cp_id, cuts, order, raw))

    # Markers embedded in events can also define common posting points.
    marker_map: Dict[str, Dict[str, Any]] = {}
    for side_index, side in enumerate(sides):
        for event_index, raw_event in enumerate(case.get("_raw_events", {}).get(side, [])):
            if not isinstance(raw_event, dict):
                continue
            marker = None
            for key in ("posting_point", "posting", "checkpoint", "cp", "过账点"):
                if raw_event.get(key):
                    marker = str(raw_event[key])
                    break
            if marker:
                marker_map.setdefault(marker, {"id": marker, "cuts": {}})
                marker_map[marker]["cuts"][side] = event_index + 1
    existing = {cp.id for cp in result}
    for marker, data in marker_map.items():
        if marker not in existing and len(data["cuts"]) == len(sides):
            result.append(Checkpoint(marker, data["cuts"], len(result), data))
    return result


# ---------------------------------------------------------------------------
# Exact PysonDB 1.7 semantics
# ---------------------------------------------------------------------------


State = Tuple[Tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class Outcome:
    ok: bool
    state: State
    error: Optional[str] = None
    result: Any = UNSET
    matches: Tuple[Dict[str, Any], ...] = ()
    write_mask: int = 0
    field_mask: int = 0
    target_mask: int = 0
    generated_id: Optional[int] = None


def thaw_state(state: State) -> Tuple[List[str], List[Dict[str, Any]]]:
    origins = [origin for origin, _ in state]
    records = [json.loads(blob) for _, blob in state]
    return origins, records


def freeze_records(origins: Sequence[str], records: Sequence[Dict[str, Any]]) -> State:
    return tuple((origin, canonical(record)) for origin, record in zip(origins, records))


def planned_add_id(event: Event, bit_index: int) -> int:
    result = event.expected_result
    if isinstance(result, dict) and "id" in result:
        try:
            return int(result["id"])
        except (TypeError, ValueError):
            pass
    if isinstance(result, int):
        return result
    generated = event.call.get("generated_id") or event.call.get("new_id")
    if generated is not None:
        return int(generated)
    # A deterministic stand-in for PysonDB's random 18-digit primary key.
    return 880_000_000_000_000_000 + bit_index + 1


def simulate(event: Event, state: State, bits: Dict[str, int], id_field: str = "id") -> Outcome:
    op = event.op
    origins, records = thaw_state(state)

    def pack(
        *,
        ok: bool = True,
        error: Optional[str] = None,
        result: Any = None,
        match_info: Optional[List[Dict[str, Any]]] = None,
        write_origins: Iterable[str] = (),
        field_origins: Optional[Iterable[str]] = None,
        generated_id: Optional[int] = None,
    ) -> Outcome:
        write_mask = sum_bits(bits, write_origins)
        field_mask = write_mask if field_origins is None else sum_bits(bits, field_origins)
        return Outcome(
            ok=ok,
            error=error,
            state=state if error is not None and not ok else freeze_records(origins, records),
            result=result,
            matches=tuple(match_info or []),
            write_mask=write_mask,
            field_mask=field_mask,
            target_mask=write_mask,
            generated_id=generated_id,
        )

    if op == "getAll":
        all_matches = tuple(
            {"origin": origin, "index": index, "position": index + 1}
            for index, origin in enumerate(origins)
        )
        return Outcome(True, state, result=list(records), matches=all_matches)

    if op == "get":
        try:
            num = int(event.call.get("num", 1))
            if num <= len(records):
                get_matches = tuple(
                    {"origin": origins[index], "index": index, "position": index + 1}
                    for index in range(num)
                )
                return Outcome(True, state, result=list(records[0:num]), matches=get_matches)
            return Outcome(True, state, result=[{"": ""}])
        except Exception:
            return Outcome(True, state, result=[{"": ""}])

    if op == "getById":
        try:
            pk = int(event.call["pk"])
            for index, (origin, record) in enumerate(zip(origins, records)):
                if record[id_field] == pk:
                    same_id = [
                        {"origin": origins[k], "index": k, "position": k + 1}
                        for k, other in enumerate(records)
                        if other.get(id_field) == pk
                    ]
                    return Outcome(
                        True,
                        state,
                        result=dict(record),
                        matches=(
                            {
                                "origin": origin,
                                "index": index,
                                "position": index + 1,
                                "returned": True,
                                "same_id_rows": same_id,
                            },
                        ),
                    )
            raise IdNotFoundError(pk)  # type: ignore[name-defined]
        except Exception as exc:
            return Outcome(False, state, error="IdNotFoundError", result=None)

    if op == "getByQuery":
        query = event.call.get("query") or {}
        result = []
        matches = []
        for index, (origin, record) in enumerate(zip(origins, records)):
            if all(key in record and record[key] == query[key] for key in query):
                result.append(dict(record))
                matches.append({"origin": origin, "index": index, "position": index + 1})
        return Outcome(True, state, result=result, matches=tuple(matches))

    if op == "reSearch":
        key = str(event.call.get("key"))
        pattern_value = event.call.get("pattern", "")
        pattern = re.compile(pattern_value) if not hasattr(pattern_value, "match") else pattern_value
        result = []
        matches = []
        for index, (origin, record) in enumerate(zip(origins, records)):
            matched = False
            for field, value in record.items():
                if field == key and re.match(pattern, str(value)):
                    result.append(dict(record))
                    matched = True
                    break
            if matched:
                matches.append({"origin": origin, "index": index, "position": index + 1})
        return Outcome(True, state, result=result, matches=tuple(matches))

    if op == "updateById":
        new_data = event.call.get("data") or {}
        try:
            pk = int(event.call["pk"])
        except Exception as exc:
            return Outcome(False, state, error=simple_error_name(exc))
        try:
            first_keys = set(records[0].keys())
        except IndexError:
            return Outcome(False, state, error="IndexError")
        if not set(new_data.keys()).issubset(first_keys):
            return Outcome(False, state, error="SchemaError")

        updated_origins: List[str] = []
        matches: List[Dict[str, Any]] = []
        for index, (origin, record) in enumerate(zip(origins, records)):
            if record[id_field] == pk:
                before_index = index
                record.update(dict(new_data))
                updated_origins.append(origin)
                matches.append(
                    {"origin": origin, "before_index": before_index, "before_position": before_index + 1}
                )
        if not updated_origins:
            return Outcome(False, state, error="IdNotFoundError")
        for match in matches:
            origin = match["origin"]
            index = origins.index(origin)
            match.update({"after_index": index, "after_position": index + 1})
        return pack(write_origins=updated_origins, result=None, match_info=matches)

    if op == "updateByQuery":
        query = event.call.get("query") or {}
        new_data = event.call.get("data") or {}
        try:
            first_keys = set(records[0].keys())
        except IndexError:
            return Outcome(False, state, error="IndexError")
        if not set(query.keys()).issubset(first_keys) or not set(new_data.keys()).issubset(first_keys):
            return Outcome(False, state, error="SchemaError")

        updated_origins: List[str] = []
        matches: List[Dict[str, Any]] = []
        new_records: List[Dict[str, Any]] = []
        new_origins: List[str] = []
        for index, (origin, record) in enumerate(zip(origins, records)):
            matched = all(key in record and record[key] == query[key] for key in query)
            if matched:
                if not set(new_data.keys()).issubset(first_keys):
                    return Outcome(False, state, error="SchemaError")
                record.update(dict(new_data))
                updated_origins.append(origin)
                matches.append({"origin": origin, "before_index": index, "before_position": index + 1})
            new_records.append(record)
            new_origins.append(origin)
        if not updated_origins:
            return Outcome(False, state, error="DataNotFoundError")
        records, origins = new_records, new_origins
        for match in matches:
            origin = match["origin"]
            index = origins.index(origin)
            match.update({"after_index": index, "after_position": index + 1})
        return pack(write_origins=updated_origins, result=None, match_info=matches)

    if op == "deleteById":
        try:
            pk = int(event.call["pk"])
        except Exception as exc:
            return Outcome(False, state, error=simple_error_name(exc))
        deleted_origins: List[str] = []
        matches: List[Dict[str, Any]] = []
        kept_records: List[Dict[str, Any]] = []
        kept_origins: List[str] = []
        for index, (origin, record) in enumerate(zip(origins, records)):
            if record[id_field] == pk:
                deleted_origins.append(origin)
                matches.append({"origin": origin, "before_index": index, "position": index + 1})
            else:
                kept_records.append(record)
                kept_origins.append(origin)
        if not deleted_origins:
            return Outcome(False, state, error="IdNotFoundError")
        records, origins = kept_records, kept_origins
        return pack(write_origins=deleted_origins, field_origins=(), result=True, match_info=matches)

    if op == "deleteAll":
        deleted = list(origins)
        records, origins = [], []
        return pack(write_origins=deleted, field_origins=(), result=None)

    if op == "add":
        new_data = dict(event.call.get("data") or {})
        origin = f"new:{event.ref}"
        bit_index = next((i for i, name in enumerate(bits) if name == origin), len(bits))
        generated_id = planned_add_id(event, bit_index)
        if records:
            first_keys = set(records[0].keys())
            if first_keys != set(new_data.keys()).union([id_field]):
                return Outcome(False, state, error="SchemaError")
        new_data[id_field] = generated_id
        records.append(new_data)
        origins.append(origin)
        match = [{"origin": origin, "after_index": len(records) - 1, "position": len(records)}]
        return pack(write_origins=[origin], result=generated_id, match_info=match, generated_id=generated_id)

    if op == "addMany":
        items = [dict(x) for x in event.call.get("items") or []]
        if records:
            expected_keys = set(records[0].keys())
            index_keys = [id_field]
        else:
            expected_keys = set(items[0].keys()) if items else set()
            index_keys = None
        added_origins = []
        for k, item in enumerate(items):
            item_keys = set(item.keys() | index_keys) if index_keys else set(item.keys())
            if expected_keys != item_keys:
                return Outcome(False, state, error="SchemaError")
            origin = f"new:{event.ref}#{k + 1}"
            bit_index = next((i for i, name in enumerate(bits) if name == origin), len(bits) + k)
            item[id_field] = planned_add_id(event, bit_index) + k
            records.append(item)
            origins.append(origin)
            added_origins.append(origin)
        return pack(write_origins=added_origins, result=None)

    return Outcome(False, state, error=f"UnsupportedOperation:{op}")


def sum_bits(bits: Dict[str, int], origins: Iterable[str]) -> int:
    value = 0
    for origin in origins:
        if origin not in bits:
            # Defensive: catalog construction should prevent this.
            bits[origin] = 1 << len(bits)
        value |= bits[origin]
    return value


def check_provenance_assertions(expected: Any, outcome: Outcome) -> Optional[str]:
    origin_keys = ("$origin", "@origin", "__origin__", "_origin")
    position_keys = ("$position", "@position", "__position__", "_position")
    index_keys = ("$index", "@index", "__index__", "_index")

    def check_one(wanted: Dict[str, Any], match: Optional[Dict[str, Any]], where: str) -> Optional[str]:
        wanted_origin = next((str(wanted[k]) for k in origin_keys if k in wanted), None)
        if wanted_origin is not None:
            if not match or match.get("origin") != wanted_origin:
                actual = match.get("origin") if match else None
                return f"{where}: expected origin {wanted_origin}, actual {actual}"
        wanted_position = next((wanted[k] for k in position_keys if k in wanted), None)
        if wanted_position is not None:
            if not match or int(wanted_position) != match.get("position"):
                actual = match.get("position") if match else None
                return f"{where}: expected position {wanted_position}, actual {actual}"
        wanted_index = next((wanted[k] for k in index_keys if k in wanted), None)
        if wanted_index is not None:
            if not match or int(wanted_index) != match.get("index"):
                actual = match.get("index") if match else None
                return f"{where}: expected zero-based index {wanted_index}, actual {actual}"
        return None

    if isinstance(expected, dict):
        return check_one(expected, outcome.matches[0] if outcome.matches else None, "receipt")
    if isinstance(expected, list):
        for index, item in enumerate(expected):
            if not isinstance(item, dict):
                continue
            match = outcome.matches[index] if index < len(outcome.matches) else None
            error = check_one(item, match, f"receipt[{index}]")
            if error:
                return error
    return None


def constraint_failure(event: Event, outcome: Outcome, id_field: str) -> Optional[str]:
    if event.expected_error is not None:
        if outcome.ok:
            return f"expected {event.expected_error}, but call succeeded with {brief(outcome.result)}"
        if not error_matches(event.expected_error, outcome.error or ""):
            return f"expected error {event.expected_error}, got {outcome.error}"
        return None
    if event.has_constraint and event.expected_result is not UNSET:
        if not outcome.ok:
            return f"expected return {brief(event.expected_result)}, but got error {outcome.error}"
        if not receipt_equal(event.expected_result, outcome.result, id_field):
            return (
                "return mismatch: expected "
                f"{brief(event.expected_result)}; actual {brief(outcome.result)}"
            )
        provenance_error = check_provenance_assertions(event.expected_result, outcome)
        if provenance_error:
            return provenance_error
    return None


def brief(value: Any, limit: int = 500) -> str:
    if value is UNSET:
        return "<unconstrained>"
    try:
        text = canonical(value)
    except Exception:
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Interleaving DP
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Step:
    side: str
    index: int
    ref: str
    op: str
    after: Tuple[int, int]
    ok: bool
    error: Optional[str]
    matches: Tuple[Dict[str, Any], ...]
    generated_id: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class Witness:
    parent: Optional["Witness"]
    step: Step

    def depth(self) -> int:
        return 1 + (self.parent.depth() if self.parent else 0)


@dataclasses.dataclass
class Group:
    state: State
    paths: Dict[Tuple[int, int], Witness]
    target_seen: bool = False
    target_all: int = 0
    target_any: int = 0

    def one_witness(self) -> Optional[Witness]:
        return next(iter(self.paths.values()), None)


@dataclasses.dataclass
class Failure:
    ref: str
    side: str
    index: int
    op: str
    after: Tuple[int, int]
    reason: str
    actual_error: Optional[str]
    actual_result: Any
    depth: int
    witness: Optional[Witness]


@dataclasses.dataclass
class EventStat:
    ref: str
    side: str
    index: int
    op: str
    attempts: int = 0
    valid: int = 0
    failures: List[Failure] = dataclasses.field(default_factory=list)
    first_order_key: Tuple[Any, ...] = ()


@dataclasses.dataclass
class SegmentResult:
    a: str
    b: str
    a_events: List[Event]
    b_events: List[Event]
    final: Dict[State, Group]
    failures: List[Failure]
    stats: Dict[str, EventStat]
    truncated: bool = False


def merge_group(cell: Dict[State, Group], group: Group) -> None:
    existing = cell.get(group.state)
    if existing is None:
        cell[group.state] = group
        return
    for mask, witness in group.paths.items():
        existing.paths.setdefault(mask, witness)
    if group.target_seen:
        if not existing.target_seen:
            existing.target_seen = True
            existing.target_all = group.target_all
            existing.target_any = group.target_any
        else:
            existing.target_all &= group.target_all
            existing.target_any |= group.target_any


def merge_cells(left: Dict[State, Group], right: Dict[State, Group]) -> Dict[State, Group]:
    cell: Dict[State, Group] = {}
    for group in left.values():
        merge_group(cell, group)
    for group in right.values():
        merge_group(cell, group)
    return cell


def advance(
    cell: Dict[State, Group],
    event: Event,
    side_name: str,
    after: Tuple[int, int],
    bits: Dict[str, int],
    id_field: str,
    stats: Dict[str, EventStat],
    failures: List[Failure],
    target_ref: Optional[str],
    require_target_success: bool,
    track_masks: bool,
) -> Dict[State, Group]:
    out: Dict[State, Group] = {}
    stat = stats.setdefault(
        event.ref, EventStat(event.ref, event.side, event.index, event.op, first_order_key=event.order_key)
    )
    for group in cell.values():
        stat.attempts += 1
        outcome = simulate(event, group.state, bits, id_field)
        reason = constraint_failure(event, outcome, id_field)
        if reason is None and target_ref and event.ref == target_ref and require_target_success and not outcome.ok:
            reason = f"specified posted supplement must succeed, got {outcome.error}"
        if reason is not None:
            witness = group.one_witness()
            failure = Failure(
                ref=event.ref,
                side=side_name,
                index=event.index,
                op=event.op,
                after=after,
                reason=reason,
                actual_error=outcome.error,
                actual_result=UNSET if outcome.ok is False and outcome.result is UNSET else outcome.result,
                depth=(witness.depth() + 1 if witness else 1),
                witness=witness,
            )
            failures.append(failure)
            if len(stat.failures) < 10:
                stat.failures.append(failure)
            continue

        stat.valid += 1
        step = Step(
            side=side_name,
            index=event.index,
            ref=event.ref,
            op=event.op,
            after=after,
            ok=outcome.ok,
            error=outcome.error,
            matches=outcome.matches,
            generated_id=outcome.generated_id,
        )
        new_paths: Dict[Tuple[int, int], Witness] = {}
        if track_masks:
            for (write_mask, field_mask), parent in group.paths.items():
                key = (write_mask | outcome.write_mask, field_mask | outcome.field_mask)
                new_paths.setdefault(key, Witness(parent, step))
        else:
            parent = group.one_witness()
            new_paths[(0, 0)] = Witness(parent, step)  # type: ignore[arg-type]

        is_target = bool(target_ref and event.ref == target_ref)
        if is_target:
            target_seen = True
            target_all = outcome.target_mask
            target_any = outcome.target_mask
        else:
            target_seen = group.target_seen
            target_all = group.target_all
            target_any = group.target_any

        new_group = Group(
            state=outcome.state,
            paths=new_paths,
            target_seen=target_seen,
            target_all=target_all,
            target_any=target_any,
        )
        merge_group(out, new_group)
    return out


def run_segment(
    start: Dict[State, Group],
    a_name: str,
    b_name: str,
    a_events: List[Event],
    b_events: List[Event],
    bits: Dict[str, int],
    id_field: str = "id",
    target_ref: Optional[str] = None,
    require_target_success: bool = True,
    track_masks: bool = True,
    max_groups_per_cell: int = 100_000,
) -> SegmentResult:
    stats: Dict[str, EventStat] = {}
    failures: List[Failure] = []

    start_cell: Dict[State, Group] = {}
    for group in start.values():
        if track_masks:
            paths = dict(group.paths) or {(0, 0): None}  # type: ignore[dict-item]
        else:
            paths = {(0, 0): group.one_witness()}
        merge_group(
            start_cell,
            Group(
                group.state,
                paths,  # type: ignore[arg-type]
                group.target_seen,
                group.target_all,
                group.target_any,
            ),
        )

    nb = len(b_events)
    row: List[Optional[Dict[State, Group]]] = [start_cell]
    for j, event in enumerate(b_events):
        cell = advance(
            row[j] or {}, event, b_name, (0, j + 1), bits, id_field, stats, failures,
            target_ref, require_target_success, track_masks,
        )
        if len(cell) > max_groups_per_cell:
            raise CaseFormatError(f"state frontier exceeded --max-groups={max_groups_per_cell}")
        row.append(cell)

    truncated = False
    for i, a_event in enumerate(a_events):
        current: List[Optional[Dict[State, Group]]] = [None] * (nb + 1)
        current[0] = advance(
            row[0] or {}, a_event, a_name, (i + 1, 0), bits, id_field, stats, failures,
            target_ref, require_target_success, track_masks,
        )
        for j in range(1, nb + 1):
            from_a = advance(
                row[j] or {}, a_event, a_name, (i + 1, j), bits, id_field, stats, failures,
                target_ref, require_target_success, track_masks,
            )
            from_b = advance(
                current[j - 1] or {}, b_events[j - 1], b_name, (i + 1, j), bits, id_field,
                stats, failures, target_ref, require_target_success, track_masks,
            )
            current[j] = merge_cells(from_a, from_b)
            if len(current[j]) > max_groups_per_cell:
                raise CaseFormatError(f"state frontier exceeded --max-groups={max_groups_per_cell}")
        row = current

    final = row[nb] or {}
    return SegmentResult(a_name, b_name, a_events, b_events, final, failures, stats, truncated)


def hard_impossible_events(result: SegmentResult) -> List[EventStat]:
    hard = [stat for stat in result.stats.values() if stat.attempts > 0 and stat.valid == 0]
    def sort_key(stat: EventStat):
        shallow = min((f.depth for f in stat.failures), default=10**9)
        event_ts = stat.first_order_key
        return (shallow, event_ts, stat.side, stat.index)
    return sorted(hard, key=sort_key)


def witness_steps(witness: Optional[Witness]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    current = witness
    while current is not None:
        step = current.step
        steps.append(
            {
                "global_order": len(steps) + 1,
                "side": step.side,
                "stream_index_zero_based": step.index,
                "stream_line": step.index + 1,
                "ref": step.ref,
                "op": step.op,
                "dp_coordinate_after_a_b": list(step.after),
                "ok": step.ok,
                "error": step.error,
                "generated_id": step.generated_id,
                "positions": [dict(m) for m in step.matches],
            }
        )
        current = current.parent
    steps.reverse()
    for i, step in enumerate(steps, 1):
        step["global_order"] = i
    return steps


def failure_to_dict(failure: Failure, bits: Dict[str, int]) -> Dict[str, Any]:
    return {
        "ref": failure.ref,
        "side": failure.side,
        "stream_line": failure.index + 1,
        "op": failure.op,
        "dp_coordinate_after_a_b": list(failure.after),
        "reason_first_impossible_candidate": failure.reason,
        "actual_error": failure.actual_error,
        "actual_result": None if failure.actual_result is UNSET else failure.actual_result,
        "minimum_prefix_depth": failure.depth,
        "reachable_prefix_witness": witness_steps(failure.witness),
    }


# ---------------------------------------------------------------------------
# Case orchestration
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Case:
    raw: Dict[str, Any]
    id_field: str
    sides: List[str]
    events: Dict[str, List[Event]]
    checkpoints: List[Checkpoint]
    initial_state: State
    bits: Dict[str, int]


def build_case(raw_case: Dict[str, Any]) -> Case:
    id_field = str(raw_case.get("id_fieldname") or raw_case.get("id_field") or "id")
    initial_list = normalize_initial(raw_case, id_field)
    sides_raw = normalize_streams(raw_case)
    sides = list(sides_raw.keys())[:2]

    raw_events: Dict[str, List[Any]] = {}
    events: Dict[str, List[Event]] = {}
    for side in sides:
        raw_list = stream_events_from_value(sides_raw[side])
        raw_events[side] = raw_list
        events[side] = [normalize_event(item, side, i) for i, item in enumerate(raw_list)]
    raw_case["_raw_events"] = raw_events

    checkpoints = normalize_checkpoints(raw_case, sides)

    origin_names = [origin for origin, _ in initial_list]
    for side in sides:
        for event in events[side]:
            if event.op == "add":
                origin_names.append(f"new:{event.ref}")
            elif event.op == "addMany":
                count = len(event.call.get("items") or [])
                origin_names.extend(f"new:{event.ref}#{k + 1}" for k in range(count))
    # De-duplicate while preserving order.
    seen = set()
    origin_names = [x for x in origin_names if not (x in seen or seen.add(x))]
    bits = {name: 1 << i for i, name in enumerate(origin_names)}

    return Case(raw_case, id_field, sides, events, checkpoints, tuple(initial_list), bits)


def resolve_cut(value: Any, events: Sequence[Event], id_field: str) -> int:
    if isinstance(value, dict):
        if "count" in value:
            return max(0, int(value["count"]))
        if "offset" in value:
            return max(0, int(value["offset"]))
        if "after_index" in value:
            return max(0, int(value["after_index"]))
        if "through_index" in value:
            return max(0, int(value["through_index"]) + 1)
        if "event_id" in value:
            value = value["event_id"]
        elif "ref" in value:
            value = value["ref"]
        else:
            raise CaseFormatError(f"cannot resolve checkpoint position {value!r}")

    if isinstance(value, str):
        text = value.strip()
        for event in events:
            if text in {event.ref, event.raw_ref, event.ref.split(":", 1)[-1]}:
                return event.index + 1
        if text.isdigit():
            value = int(text)
        else:
            raise CaseFormatError(f"checkpoint event {text!r} not found")

    number = int(value)
    for event in events:
        try:
            if int(event.raw_ref) == number:
                return event.index + 1
        except (TypeError, ValueError):
            pass
    if number == 0:
        return 0
    if 1 <= number <= len(events):
        # Log line/sequence numbers are normally one-based and inclusive.
        return number
    if 0 <= number <= len(events):
        return number
    raise CaseFormatError(f"checkpoint position {number} is outside stream length {len(events)}")


def checkpoint_cuts(case: Case, cp: Checkpoint) -> Dict[str, int]:
    return {
        side: resolve_cut(cp.cuts.get(side, len(case.events[side])), case.events[side], case.id_field)
        for side in case.sides
    }


def initial_frontier(case: Case) -> Dict[State, Group]:
    return {
        case.initial_state: Group(case.initial_state, {(0, 0): None})  # type: ignore[dict-item]
    }


def verify_prefix(
    case: Case,
    through_cp_index: int = -1,
    target_ref: Optional[str] = None,
    require_target_success: bool = True,
) -> Tuple[Dict[State, Group], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Verify checkpoints through the inclusive index; -1 keeps merge state."""
    frontier = initial_frontier(case)
    verified: List[Dict[str, Any]] = []
    previous_cuts = {side: 0 for side in case.sides}
    for cp_index, cp in enumerate(case.checkpoints):
        if cp_index > through_cp_index:
            break
        cuts = checkpoint_cuts(case, cp)
        slices = {
            side: case.events[side][previous_cuts[side]:cuts[side]] for side in case.sides
        }
        result = run_segment(
            frontier,
            case.sides[0],
            case.sides[1],
            slices[case.sides[0]],
            slices[case.sides[1]],
            case.bits,
            case.id_field,
            target_ref=target_ref,
            require_target_success=require_target_success,
            track_masks=False,
        )
        if not result.final:
            diag = diagnose_failure(case, cp.id, result)
            return {}, verified, diag
        frontier = result.final
        verified.append({"checkpoint": cp.id, "cuts": cuts, "possible_states": len(frontier)})
        previous_cuts = cuts
    return frontier, verified, None


def diagnose_failure(case: Case, checkpoint_id: str, result: SegmentResult) -> Dict[str, Any]:
    hard = hard_impossible_events(result)
    first = hard[0] if hard else None
    candidates = []
    for stat in hard[:10]:
        failure = min(stat.failures, key=lambda f: f.depth) if stat.failures else None
        candidates.append(
            {
                "ref": stat.ref,
                "side": stat.side,
                "stream_line": stat.index + 1,
                "op": stat.op,
                "attempted_distinct_states": stat.attempts,
                "reason": failure.reason if failure else "no reachable transition",
                "actual_error": failure.actual_error if failure else None,
                "actual_result": None if failure is None or failure.actual_result is UNSET else failure.actual_result,
                "reachable_prefix_witness": witness_steps(failure.witness) if failure else [],
            }
        )
    all_failures = sorted(result.failures, key=lambda f: (f.depth, f.side, f.index))
    shallow = all_failures[0] if all_failures else None
    return {
        "checkpoint": checkpoint_id,
        "feasible": False,
        "first_impossible": candidates[0] if candidates else None,
        "hard_impossible_events": candidates,
        "shallowest_branch_failure": failure_to_dict(shallow, case.bits) if shallow else None,
        "hint": "first_impossible is an event whose receipt fails in every reachable state; "
        "shallowest_branch_failure may only rule out one interleaving.",
    }


def resolve_target(case: Case, target: Optional[str]) -> Optional[str]:
    if target:
        text = target
        for side in case.sides:
            if ":" not in text and "#" not in text:
                candidate = f"{side}:{text}"
            else:
                candidate = text.replace("#", ":", 1) if "#" in text and ":" not in text else text
            for event in case.events[side]:
                if candidate == event.ref or text in {event.ref, event.raw_ref, event.ref.split(":", 1)[-1]}:
                    return event.ref
        # Try numeric line on either side.
        for side in case.sides:
            for event in case.events[side]:
                if text.lstrip("#") in {event.raw_ref, str(event.index + 1), str(event.index)}:
                    return event.ref
        raise CaseFormatError(f"target event {target!r} not found")

    tagged = [event for side in case.sides for event in case.events[side] if "supplement" in event.tags]
    if len(tagged) == 1:
        return tagged[0].ref
    if tagged:
        return tagged[-1].ref
    return None


def analyze(case: Case, range_text: Optional[str], target_text: Optional[str],
            require_target_success: bool = True, cross_check: bool = False) -> Dict[str, Any]:
    cp_start_index = -1
    cp_end_index: Optional[int] = None
    if range_text:
        normalized_range = range_text.replace("..", ":").replace("到", ":")
        parts = re.split(r"\s+(?:to|TO)\s+|[:：，,]", normalized_range, maxsplit=1)
        start_name = parts[0].strip()
        end_name = parts[1].strip() if len(parts) == 2 else None
        cp_by_id = {cp.id: i for i, cp in enumerate(case.checkpoints)}
        if start_name not in cp_by_id:
            raise CaseFormatError(f"start checkpoint {start_name!r} not found")
        cp_start_index = cp_by_id[start_name]
        if end_name:
            if end_name not in cp_by_id:
                raise CaseFormatError(f"end checkpoint {end_name!r} not found")
            cp_end_index = cp_by_id[end_name]
            if cp_end_index < cp_start_index:
                raise CaseFormatError("range end precedes start")

    target_ref = resolve_target(case, target_text)
    if target_text and target_ref is None:
        raise CaseFormatError(f"target {target_text!r} not found")

    start_frontier, verified, prefix_error = verify_prefix(
        case,
        cp_start_index,
        target_ref=target_ref,
        require_target_success=require_target_success,
    )
    if prefix_error:
        return {"ok": False, "error": "checkpoint_conflict", "diagnosis": prefix_error}

    # Reset range masks while retaining compact current states and one prefix witness.
    range_frontier: Dict[State, Group] = {}
    for state, group in start_frontier.items():
        witness = group.one_witness()
        merge_group(
            range_frontier,
            Group(
                state,
                {(0, 0): witness},  # type: ignore[dict-item]
                group.target_seen,
                group.target_all,
                group.target_any,
            ),
        )

    previous_cuts = {side: 0 for side in case.sides}
    if cp_start_index >= 0:
        previous_cuts = checkpoint_cuts(case, case.checkpoints[cp_start_index])

    end_cuts: Dict[str, int]
    range_chain: List[Checkpoint] = []
    if cp_end_index is not None:
        range_chain = case.checkpoints[cp_start_index + 1 : cp_end_index + 1]
        end_cuts = checkpoint_cuts(case, case.checkpoints[cp_end_index])
    else:
        end_cuts = {side: len(case.events[side]) for side in case.sides}

    # If no end checkpoint and a start checkpoint was supplied, use all remaining events.
    current_cuts = dict(previous_cuts)
    all_failures: List[Failure] = []
    last_result: Optional[SegmentResult] = None
    for cp in range_chain:
        cuts = checkpoint_cuts(case, cp)
        slices = {side: case.events[side][current_cuts[side]:cuts[side]] for side in case.sides}
        result = run_segment(
            range_frontier, case.sides[0], case.sides[1],
            slices[case.sides[0]], slices[case.sides[1]], case.bits, case.id_field,
            target_ref=target_ref, require_target_success=require_target_success,
        )
        all_failures.extend(result.failures)
        if not result.final:
            diagnosis = diagnose_failure(case, cp.id, result)
            return {"ok": False, "error": "checkpoint_conflict", "diagnosis": diagnosis,
                    "verified_prefix": verified}
        range_frontier = result.final
        current_cuts = cuts
        last_result = result

    if not range_chain:
        slices = {
            side: case.events[side][previous_cuts[side]:end_cuts[side]] for side in case.sides
        }
        last_result = run_segment(
            range_frontier, case.sides[0], case.sides[1],
            slices[case.sides[0]], slices[case.sides[1]], case.bits, case.id_field,
            target_ref=target_ref, require_target_success=require_target_success,
        )
        all_failures = last_result.failures
        if not last_result.final:
            diagnosis = diagnose_failure(case, range_text or "FULL", last_result)
            return {"ok": False, "error": "checkpoint_conflict", "diagnosis": diagnosis,
                    "verified_prefix": verified}
        range_frontier = last_result.final

    choices: List[Tuple[int, int, State, Tuple[int, int], Witness]] = []
    for state, group in range_frontier.items():
        for mask_pair, witness in group.paths.items():
            choices.append((mask_count(mask_pair[0]), mask_count(mask_pair[1]), state, mask_pair, witness))

    min_write = min(mask_count(x[3][0]) for x in choices)
    max_write = max(mask_count(x[3][0]) for x in choices)
    min_field = min(mask_count(x[3][1]) for x in choices)
    max_field = max(mask_count(x[3][1]) for x in choices)

    def origin_names(mask: int) -> List[str]:
        return [name for name, bit in case.bits.items() if mask & bit]

    min_choice = min(choices, key=lambda x: (x[0], x[1]))
    max_choice = max(choices, key=lambda x: (x[0], x[1]))
    field_min_choice = min(choices, key=lambda x: (x[1], x[0]))
    field_max_choice = max(choices, key=lambda x: (x[1], x[0]))

    guaranteed_mask: Optional[int] = None
    possible_target_mask = 0
    target_seen_any = False
    for group in range_frontier.values():
        if group.target_seen:
            target_seen_any = True
            guaranteed_mask = group.target_all if guaranteed_mask is None else guaranteed_mask & group.target_all
            possible_target_mask |= group.target_any

    target_report = None
    if target_ref:
        target_event = next(
            event for side in case.sides for event in case.events[side] if event.ref == target_ref
        )
        target_report = {
            "ref": target_ref,
            "op": target_event.op,
            "certain_originals": origin_names(guaranteed_mask) if target_seen_any and guaranteed_mask is not None else None,
            "possible_originals": origin_names(possible_target_mask) if target_seen_any else [],
            "never_originals": [
                name for name in case.bits
                if target_seen_any
                and not (possible_target_mask & case.bits[name])
                and not name.startswith("new:")
            ],
            "all_valid_executions_observe_target": target_seen_any,
            "warning": None if target_seen_any else "target event is outside the verified/range prefix",
            "note": "certain is the intersection over every receipt-consistent interleaving; "
                    "possible is the union.",
        }

    answer = {
        "written_originals_min_including_deletes": min_write,
        "written_originals_max_including_deletes": max_write,
        "field_or_add_written_originals_min": min_field,
        "field_or_add_written_originals_max": max_field,
        "count_basis": "first pair counts successful add/update/delete; second excludes deletes",
        "specified_supplement_ref": target_ref,
        "specified_supplement_certain_originals": (
            target_report["certain_originals"] if target_report else None
        ),
    }

    report: Dict[str, Any] = {
        "ok": True,
        "answer": answer,
        "case_id": case.raw.get("case_id") or case.raw.get("id") or "origin-audit",
        "id_field": case.id_field,
        "sides": case.sides,
        "range": range_text or "ALL",
        "start_checkpoint": case.checkpoints[cp_start_index].id if cp_start_index >= 0 else "MERGE/INITIAL",
        "end_checkpoint": case.checkpoints[cp_end_index].id if cp_end_index is not None else "END",
        "verified_prefix": verified,
        "possible_final_state_groups": len(range_frontier),
        "distinct_originals_in_catalog": len(case.bits),
        "written_including_deletes": {"min": min_write, "max": max_write},
        "field_or_add_writes_only": {"min": min_field, "max": max_field},
        "min_witness": {
            "count": min_choice[0],
            "origins": origin_names(min_choice[3][0]),
            "steps": witness_steps(min_choice[4]),
            "final_rows": [
                {"origin": origin, "position": i + 1, "record": json.loads(blob)}
                for i, (origin, blob) in enumerate(min_choice[2])
            ],
        },
        "max_witness": {
            "count": max_choice[0],
            "origins": origin_names(max_choice[3][0]),
            "steps": witness_steps(max_choice[4]),
            "final_rows": [
                {"origin": origin, "position": i + 1, "record": json.loads(blob)}
                for i, (origin, blob) in enumerate(max_choice[2])
            ],
        },
        "field_min_witness_origins": origin_names(field_min_choice[3][1]),
        "field_max_witness_origins": origin_names(field_max_choice[3][1]),
        "target": target_report,
        "semantics": "PysonDB JsonDatabase 1.7 loop semantics; getById returns first match, "
                     "update/delete by id touch every duplicate match.",
    }

    if cross_check:
        report["original_library_cross_check"] = cross_check_witness(case, min_choice[4], min_choice[2])
        max_check = cross_check_witness(case, max_choice[4], max_choice[2])
        report["original_library_cross_check_max"] = max_check
    return report


def cross_check_witness(case: Case, witness: Optional[Witness], expected_state: State) -> Dict[str, Any]:
    steps = witness_steps(witness)
    if not steps:
        return {"checked": True, "steps": 0}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from pysondb.db import JsonDatabase  # type: ignore
    except Exception as exc:
        return {"checked": False, "reason": f"cannot import original pysondb: {exc}"}

    events_by_ref = {event.ref: event for side in case.sides for event in case.events[side]}
    with tempfile.TemporaryDirectory(prefix="origin-audit-") as tmp:
        db_path = Path(tmp) / "crosscheck.json"
        initial_records = [json.loads(blob) for _, blob in case.initial_state]
        db_path.write_text(json.dumps({"data": initial_records}, ensure_ascii=False), encoding="utf-8")
        db = JsonDatabase(str(db_path), id_fieldname=case.id_field)

        def deterministic_id() -> int:
            # Monkeypatched per add event immediately before invocation.
            return getattr(db, "_audit_next_id", 880_000_000_000_000_001)

        db._get_id = deterministic_id  # type: ignore[method-assign]
        errors: List[str] = []
        for step in steps:
            event = events_by_ref[step["ref"]]
            call = dict(event.call)
            if event.op == "add":
                bit_index = next((i for i, name in enumerate(case.bits) if name == f"new:{event.ref}"), 0)
                db._audit_next_id = planned_add_id(event, bit_index)  # type: ignore[attr-defined]
            try:
                method = getattr(db, event.op)
                if event.op in {"getById", "deleteById"}:
                    method(call["pk"])
                elif event.op == "updateById":
                    method(call["pk"], call.get("data") or {})
                elif event.op == "getByQuery":
                    method(call.get("query") or {})
                elif event.op == "updateByQuery":
                    method(call.get("query") or {}, call.get("data") or {})
                elif event.op == "add":
                    method(call.get("data") or {})
                elif event.op == "addMany":
                    method(call.get("items") or [])
                elif event.op == "get":
                    method(call.get("num", 1))
                elif event.op == "reSearch":
                    method(call.get("key"), call.get("pattern", ".*"))
                else:
                    method()
            except Exception as exc:
                # Expected errors are fine; compare type where the witness says error.
                if step["ok"]:
                    errors.append(f"{event.ref}: unexpected {simple_error_name(exc)}: {exc}")
                elif step.get("error") and not error_matches(step["error"], simple_error_name(exc)):
                    errors.append(f"{event.ref}: model error {step['error']} but library {simple_error_name(exc)}")
        actual = json.loads(db_path.read_text(encoding="utf-8")).get("data", [])
        expected = [json.loads(blob) for _, blob in expected_state]
        same = canonical(actual) == canonical(expected)
        if not same:
            errors.append("final original-library state differs from model witness")
        return {
            "checked": not errors,
            "steps": len(steps),
            "state_matches_model": same,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# Example and CLI
# ---------------------------------------------------------------------------


EXAMPLE_CASE: Dict[str, Any] = {
    "case_id": "GSB1109-PYSONDB-demo",
    "id_field": "id",
    "origin_field": "$origin",
    "initial": [
        {"$origin": "O0-front-header", "id": 16, "legacy": "front"},
        {"$origin": "O1-first-17", "id": 17, "name": "same-literal", "note": "x"},
        {"$origin": "O2-second-17", "id": 17, "name": "same-literal", "note": "x"},
        {"$origin": "O3-original-18", "id": 18, "name": "resident-18", "note": "y"},
    ],
    "streams": {
        "A": [
            {"id": "a1", "op": "getById", "pk": 17, "result": {"$origin": "O1-first-17", "id": 17, "name": "same-literal", "note": "x"}},
            {"id": "a2", "op": "deleteById", "pk": 16},
            {"id": "a3", "op": "updateById", "pk": 17, "data": {"id": 18}},
        ],
        "B": [
            {"id": "b1", "op": "getByQuery", "query": {"id": 18}, "result": [{"id": 18, "name": "resident-18", "note": "y"}]},
            {"id": "b2", "op": "updateById", "pk": 18, "data": {"note": "supplement"}, "tags": ["supplement"]},
            {"id": "b3", "op": "updateByQuery", "query": {"name": "same-literal"}, "data": {"note": "field-fixed"}},
        ],
    },
    "checkpoints": [
        {"id": "CP1", "positions": {"A": "a2", "B": "b1"}},
        {"id": "CP2", "positions": {"A": "a3", "B": "b3"}},
    ],
}


def verify_all(case: Case, cross_check: bool = False) -> Dict[str, Any]:
    frontier, verified, diagnosis = verify_prefix(case, len(case.checkpoints) - 1)
    if diagnosis:
        return {"ok": False, "error": "checkpoint_conflict", "diagnosis": diagnosis}
    report = {
        "ok": True,
        "case_id": case.raw.get("case_id") or case.raw.get("id") or "origin-audit",
        "checkpoints_verified": verified,
        "possible_final_state_groups": len(frontier),
        "sides": case.sides,
    }
    if cross_check:
        # In verification mode each state retains one witness only.
        group = next(iter(frontier.values()), None)
        if group and group.one_witness():
            report["original_library_cross_check"] = cross_check_witness(
                case, group.one_witness(), next(iter(frontier.keys()))
            )
    return report


def render_text(report: Dict[str, Any]) -> str:
    if not report.get("ok"):
        diag = report.get("diagnosis") or {}
        lines = ["复核不通过：存在现有过账点无法由任何回执一致的交错顺序满足。"]
        first = diag.get("first_impossible")
        if first:
            lines.append(
                f"最先彻底不可能：{first['side']} 流水第 {first['stream_line']} 行 "
                f"{first['ref']} {first['op']} — {first['reason']}"
            )
        shallow = diag.get("shallowest_branch_failure")
        if shallow and not first:
            lines.append(
                f"最早分支失败：{shallow['side']} 第 {shallow['stream_line']} 行 "
                f"{shallow['ref']} {shallow['op']} — {shallow['reason_first_impossible_candidate']}"
            )
        lines.append(json.dumps(report, ensure_ascii=False, indent=2))
        return "\n".join(lines)

    if "checkpoints_verified" in report:
        lines = [f"案件 {report['case_id']}：全部过账点可核回。"]
        for item in report["checkpoints_verified"]:
            lines.append(f"- {item['checkpoint']}: cuts={item['cuts']}, states={item['possible_states']}")
        check = report.get("original_library_cross_check")
        if check:
            lines.append(f"原库交叉核验：{'通过' if check.get('checked') else '未通过'}（{check.get('steps', 0)} 步）")
        return "\n".join(lines)

    write = report["written_including_deletes"]
    field = report["field_or_add_writes_only"]
    lines = [
        f"案件 {report['case_id']}：范围 {report['range']} "
        f"({report['start_checkpoint']} -> {report['end_checkpoint']})",
        f"曾被写到的不同原件（含删除）：最少 {write['min']}，最多 {write['max']}",
        f"仅新增/字段写入：最少 {field['min']}，最多 {field['max']}",
    ]
    target = report.get("target")
    if target:
        lines.append(f"指定补录 {target['ref']} 一定写到：{', '.join(target['certain_originals']) or '（无）'}")
        lines.append(f"该补录可能写到：{', '.join(target['possible_originals']) or '（无）'}")
    lines.append(f"可行最终状态组：{report['possible_final_state_groups']}；原件目录：{report['distinct_originals_in_catalog']}")
    if report.get("original_library_cross_check"):
        check = report["original_library_cross_check"]
        lines.append(f"原库交叉核验：{'通过' if check.get('checked') else '未通过'}（{check.get('steps', 0)} 步）")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="origin_audit.py",
        description="Reconcile PysonDB duplicate original identities across two workstation logs.",
    )
    parser.add_argument("--case", "-c", help="case JSON file or case directory")
    parser.add_argument("--initial", help="merge-time rows: PysonDB JSON, JSON/NDJSON, or CSV")
    parser.add_argument("--a-log", "--left-log", dest="a_log", help="workstation A request log")
    parser.add_argument("--b-log", "--right-log", dest="b_log", help="workstation B request log")
    parser.add_argument("--a-name", default="A", help="side name used in --a-log (default A)")
    parser.add_argument("--b-name", default="B", help="side name used in --b-log (default B)")
    parser.add_argument("--checkpoints", help="checkpoint JSON/NDJSON file")
    parser.add_argument("--case-id", help="override case id in output")
    parser.add_argument("--identity-map-out", metavar="PATH",
                        help="write origin/position/record sidecar JSON and exit")
    parser.add_argument("--id-field", help="PysonDB primary-key field (default id)")
    parser.add_argument("--origin-field", help="explicit stable origin metadata field")
    parser.add_argument("--range", "-r", dest="range_text", help="posting range, e.g. CP1:CP2")
    parser.add_argument("--start", help="start checkpoint; use with --end instead of --range")
    parser.add_argument("--end", help="end checkpoint; omit to analyze the remaining log")
    parser.add_argument("--target", "-t", help="specified supplement event ref, e.g. B:b2")
    parser.add_argument("--check", action="store_true", help="only verify all known posting points")
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    parser.add_argument("--allow-target-failure", action="store_true",
                        help="do not assume the specified supplement posted successfully")
    parser.add_argument("--cross-check", action="store_true",
                        help="replay min/max witnesses through the real pysondb package")
    parser.add_argument("--write-example", metavar="PATH", help="write a documented example case and exit")
    parser.add_argument("--selftest", action="store_true", help="run built-in semantic self-tests")
    return parser


def selftest() -> int:
    case = build_case(EXAMPLE_CASE)
    report = analyze(case, "CP1:CP2", "B:b2", cross_check=True)
    assert report["ok"], json.dumps(report, ensure_ascii=False, indent=2)
    target = report["target"]
    assert target["certain_originals"] == ["O3-original-18"], target
    assert set(target["possible_originals"]) >= {
        "O1-first-17", "O2-second-17", "O3-original-18"
    }, target
    assert report["written_including_deletes"]["min"] >= 3
    check = report["original_library_cross_check"]
    assert check["checked"], check

    bad = json.loads(json.dumps(EXAMPLE_CASE))
    bad["streams"]["A"].append({"id": "a4", "op": "getById", "pk": 17, "result": {"id": 17}})
    bad_case = build_case(bad)
    bad_report = analyze(bad_case, "CP1:CP2", "B:b2")
    # CP2 cuts only include a3/b3, so analyze full range to include a4.
    bad_report = analyze(bad_case, None, "B:b2")
    assert not bad_report["ok"] and bad_report["diagnosis"]["first_impossible"], bad_report
    print("origin_audit selftest passed")
    print(render_text(report))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.write_example:
        Path(args.write_example).write_text(
            json.dumps(EXAMPLE_CASE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(args.write_example)
        return 0
    if args.selftest:
        return selftest()
    try:
        raw_case = case_from_cli_sources(args)
        case = build_case(raw_case)
        if args.identity_map_out:
            identity_map = {
                "id_field": case.id_field,
                "rows": [
                    {
                        "origin": origin,
                        "zero_based_index": index,
                        "position": index + 1,
                        "record": json.loads(blob),
                    }
                    for index, (origin, blob) in enumerate(case.initial_state)
                ],
            }
            Path(args.identity_map_out).write_text(
                json.dumps(identity_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(args.identity_map_out)
            return 0
        range_text = args.range_text
        if not range_text and args.start:
            range_text = f"{args.start}:{args.end or ''}"
        if args.check:
            report = verify_all(case, cross_check=args.cross_check)
        else:
            report = analyze(
                case,
                range_text,
                args.target,
                require_target_success=not args.allow_target_failure,
                cross_check=args.cross_check,
            )
    except CaseFormatError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
