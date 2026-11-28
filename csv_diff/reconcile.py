"""Reconcile two stocktake files when packages are split and merged.

The ordinary ``csv-diff`` output is still the right answer for plain tables.
This module handles the harder case where a package can be broken into parts,
those parts can be moved or issued, and parts can later be merged into a new
package.  The planner replays the signed occurrence archive from the previous
official stocktake to the current one and emits a resumable repair plan.

Design rules:

* Every occurrence is applied at most once, even if both teams received the
  same pending transfer message.
* No package may go negative at any intermediate step.
* A physical tag may be transferred by only one occurrence.
* The final balance must match the current stocktake *and* its provenance must
  name the original package(s), not merely the same grand total.
* All issue and merge records in the archive appear on both acceptance
  checklists, with their disposition.

Inputs accept CSV, TSV or JSON and use either English or Chinese headings.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

EPSILON = Decimal("0.000000001")
TAG_PREFIX = "~part~"

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except InvalidOperation:
        match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", text)
        if match:
            return Decimal(match.group(0))
        raise ValueError("not a number: {!r}".format(value))


def _num(value: Decimal) -> Any:
    value = Decimal(value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _norm(key: Any) -> str:
    return re.sub(r"[\s_\-./]+", "", str(key).strip().lower())


def _identity(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first(row: Mapping[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    """Look up a field by alias priority, not by source-column order.

    Source files sometimes carry both ``qty`` and ``available``.  Alias order
    makes the chosen meaning deterministic.
    """
    index = {_norm(key): value for key, value in row.items()}
    for alias in aliases:
        if _norm(alias) not in index:
            continue
        value = index[_norm(alias)]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def _list_value(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        parts = re.split(r"[;|]", text)
        out: List[Any] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                package, qty = part.rsplit(":", 1)
                out.append({"package": package.strip(), "qty": qty.strip()})
            elif "," in part:
                package, qty = part.rsplit(",", 1)
                out.append({"package": package.strip(), "qty": qty.strip()})
            else:
                out.append(part)
        return out
    return [value]


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "n", "否", "未批准"}


def _signature(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf8")
    ).hexdigest()


def _money(value: Decimal) -> Any:
    return _num(value)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_text(source: Any) -> str:
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        return data
    with io.open(os.fspath(source), "r", encoding="utf-8-sig", newline="") as fp:
        return fp.read()


def read_rows(source: Any, fmt: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read a CSV/TSV/JSON table into a list of dictionaries."""
    if source is None:
        return []
    if isinstance(source, Mapping):
        for key in ("rows", "records", "data", "items", "occurrences", "events"):
            if isinstance(source.get(key), list):
                return [dict(row) for row in source[key]]
        return [dict(source)]
    if isinstance(source, (list, tuple)):
        # A list of dicts is a row list.  A list of strings is ambiguous, so
        # treat it as raw CSV text (a useful convenience for tests and pipes).
        if source and all(isinstance(row, str) for row in source):
            source = "\n".join(source)
        else:
            return [dict(row) for row in source]
    if isinstance(source, str) and "\n" in source and not os.path.exists(source):
        text = source
        source = None
    if source is not None:
        text = _read_text(source)
    suffix = ""
    if source is not None and not hasattr(source, "read"):
        suffix = os.path.splitext(os.fspath(source))[1].lower().lstrip(".")
    kind = (fmt or suffix or "").lower()
    if kind in ("json", "js") or (not kind and text.lstrip()[:1] in "[{"):
        data = json.loads(text)
        if isinstance(data, Mapping):
            for key in ("rows", "records", "data", "items", "occurrences", "events"):
                if isinstance(data.get(key), list):
                    return [dict(row) for row in data[key]]
            return [dict(data)]
        if not isinstance(data, list):
            raise ValueError("expected a JSON list of objects")
        return [dict(row) for row in data]
    try:
        dialect = csv.Sniffer().sniff(text[: 1024 * 1024], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if kind == "tsv" else csv.excel
    return [dict(row) for row in csv.DictReader(io.StringIO(text), dialect=dialect)]


STOCK_ALIASES = {
    "package": (
        "package_id",
        "packageid",
        "package",
        "pack",
        "pid",
        "stocktake_id",
        "tag",
        "tag_id",
        "lot",
        "lot_id",
        "包号",
        "包",
        "编号",
        "盘点号",
        "实物号",
        "批次号",
        "库存号",
        "货号",
    ),
    "location": (
        "location",
        "loc",
        "warehouse",
        "store",
        "site",
        "bin",
        "slot",
        "库位",
        "仓库",
        "仓",
        "门店",
        "点位",
        "登记点",
        "机构",
    ),
    "qty": (
        "quantity",
        "qty",
        "amount",
        "count",
        "on_hand",
        "onhand",
        "stock",
        "数量",
        "件数",
        "可用数量",
        "可用量",
        "库存",
        "库存量",
        "结存",
        "可用",
    ),
    "occurrence": (
        "occurrence_id",
        "occurrenceid",
        "occurrence",
        "event_id",
        "eventid",
        "voucher",
        "voucher_id",
        "发生号",
        "发生编号",
        "凭据号",
        "业务号",
        "单据号",
    ),
    "origins": (
        "origins",
        "origin",
        "source",
        "source_package",
        "原包",
        "原包号",
        "来源",
        "来源包",
    ),
    "tags": ("tags", "tag_ids", "physical_tags", "标签", "实物标签"),
    "item": ("item", "sku", "product", "goods", "品名", "商品", "物料", "货品"),
    "status": ("status", "state", "状态"),
    "team": ("team", "teams", "signed_by", "handler", "班组", "登记点", "签收方", "经办"),
    "available": (
        "available_qty",
        "qty_available",
        "capacity",
        "limit",
        "available",
        "可用数量",
        "可用量",
        "库位可用数量",
    ),
    "note": ("note", "remark", "memo", "备注", "说明"),
}


def _parse_origins(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [{"package": k, "qty": v} for k, v in value.items()]
    return _list_value(value)


def parse_stocktake(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        package = _identity(_first(row, STOCK_ALIASES["package"]))
        if not package:
            raise ValueError("stocktake row {} has no package id".format(index + 1))
        out.append(
            {
                "package": package,
                "location": _identity(_first(row, STOCK_ALIASES["location"], "")),
                "qty": _dec(_first(row, STOCK_ALIASES["qty"], "1")),
                "occurrence": _identity(_first(row, STOCK_ALIASES["occurrence"], "")),
                "origins": _parse_origins(_first(row, STOCK_ALIASES["origins"])),
                "tags": [_identity(t) for t in _list_value(_first(row, STOCK_ALIASES["tags"]))],
                "item": _identity(_first(row, STOCK_ALIASES["item"], "")),
                "available": (
                    _dec(_first(row, STOCK_ALIASES["available"]))
                    if _first(row, STOCK_ALIASES["available"]) is not None
                    else None
                ),
                "note": _identity(_first(row, STOCK_ALIASES["note"], "")),
                "status": _identity(_first(row, STOCK_ALIASES["status"], "")).lower(),
                "teams": [
                    _team_key(t)
                    for t in _list_value(_first(row, STOCK_ALIASES["team"]))
                    if _team_key(t)
                ],
                "raw": dict(row),
                "index": index,
            }
        )
    return out


OCCURRENCE_KIND_ALIASES = {
    "split": ("split", "break", "unpack", "拆", "拆包", "拆分", "分拆", "分包"),
    "merge": ("merge", "combine", "pack", "合", "合包", "合并", "组包"),
    "transfer": ("transfer", "move", "relocate", "调拨", "调", "移库", "移", "转库"),
    "issue": ("issue", "consume", "out", "pick", "领用", "领", "出库", "发出", "消耗"),
    "receive": ("receive", "inbound", "in", "return", "入库", "入", "收回", "退回"),
    "set": ("set", "stocktake", "count", "盘点", "实盘", "覆盖"),
    "adjust": ("adjust", "change", "delta", "调整", "修正", "盈亏"),
}


def _team_key(value: Any) -> str:
    text = _identity(value)
    if not text:
        return ""
    normalized = re.sub(r"[\s_\-./]+", "", text).lower()
    if normalized in {"a", "teama", "team1", "班a", "a班", "甲", "甲班", "一组", "1组"}:
        return "A"
    if normalized in {"b", "teamb", "team2", "班b", "b班", "乙", "乙班", "二组", "2组"}:
        return "B"
    if re.match(r"^(a|甲|一)", normalized):
        return "A"
    if re.match(r"^(b|乙|二)", normalized):
        return "B"
    return text


def _kind(value: Any) -> str:
    text = _norm(value or "")
    for kind, aliases in OCCURRENCE_KIND_ALIASES.items():
        if text in {_norm(a) for a in aliases}:
            return kind
    for kind, aliases in OCCURRENCE_KIND_ALIASES.items():
        for alias in aliases:
            if _norm(alias) and _norm(alias) in text:
                return kind
    return "adjust"


def _parse_node(value: Any, default_location: str = "") -> Dict[str, Any]:
    if isinstance(value, str):
        text = value.strip()
        if ":" in text:
            package, qty = text.rsplit(":", 1)
            try:
                parsed = _dec(qty)
            except (ValueError, InvalidOperation):
                parsed = None
            if parsed is not None:
                # A bare "A:7" carries no location; fill it from the
                # occurrence's from/to location.  The caller re-homes nodes
                # that still have no location.
                return {
                    "package": package.strip(),
                    "location": default_location,
                    "qty": parsed,
                    "tags": [],
                    "origins": [],
                    "item": "",
                }
    if isinstance(value, Mapping):
        package = _identity(
            _first(
                value,
                ("package", "package_id", "pid", "pack", "tag", "id", "包号", "实物号"),
            )
        )
        if not package:
            raise ValueError("occurrence node has no package id")
        location = _identity(
            _first(value, ("location", "loc", "库位", "仓库", "仓"), default_location)
        )
        qty_value = _first(value, STOCK_ALIASES["qty"])
        qty = _dec(qty_value) if qty_value is not None else None
        return {
            "package": package,
            "location": location,
            "qty": qty,
            "tags": [_identity(t) for t in _list_value(_first(value, STOCK_ALIASES["tags"]))],
            "origins": _parse_origins(_first(value, STOCK_ALIASES["origins"])),
            "item": _identity(_first(value, STOCK_ALIASES["item"], "")),
        }
    package = _identity(value)
    return {
        "package": package,
        "location": default_location,
        "qty": None,
        "tags": [],
        "origins": [],
        "item": "",
    }


def _combine_nodes(nodes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    positions: Dict[Tuple[str, str], int] = {}
    for node in nodes:
        key = (node.get("location", ""), node["package"])
        if key not in positions:
            positions[key] = len(out)
            out.append(dict(node))
            continue
        current = out[positions[key]]
        if node.get("qty") is not None:
            current["qty"] = (current.get("qty") or Decimal(0)) + node["qty"]
        for field in ("tags", "origins"):
            for value in node.get(field) or []:
                if value not in current[field]:
                    current[field].append(value)
    return out


def _parse_occurrence(row: Mapping[str, Any], index: int = 0) -> Dict[str, Any]:
    occurrence_id = _identity(
        _first(
            row,
            (
                "occurrence_id",
                "occurrence",
                "event_id",
                "op_id",
                "voucher_id",
                "id",
                "发生号",
                "发生编号",
                "凭据号",
                "业务号",
                "单据号",
            ),
        )
    )
    if not occurrence_id:
        occurrence_id = "OCC-{}".format(index + 1)
    kind = _kind(
        _first(
            row,
            ("type", "kind", "action", "operation", "event", "类型", "业务类型", "动作"),
            "",
        )
    )
    from_location = _identity(
        _first(
            row,
            (
                "from_location",
                "from",
                "source_location",
                "out_location",
                "from_warehouse",
                "调出库位",
                "调出仓",
                "原库位",
                "出库位",
                "来源库位",
            ),
            "",
        )
    )
    to_location = _identity(
        _first(
            row,
            (
                "to_location",
                "to",
                "target_location",
                "in_location",
                "to_warehouse",
                "destination",
                "调入库位",
                "调入仓",
                "入货库位",
                "新库位",
                "入库位",
                "目标库位",
            ),
            "",
        )
    )
    package = _identity(
        _first(
            row,
            ("package_id", "package", "pack", "pid", "tag", "包号", "实物号", "货号", "库存号"),
        )
    )
    qty_value = _first(
        row,
        ("quantity", "qty", "amount", "count", "数量", "件数", "发生数量", "调拨数量", "领用数量"),
    )
    qty = _dec(qty_value) if qty_value is not None else None

    raw_inputs = _list_value(
        _first(
            row,
            (
                "inputs",
                "input",
                "sources",
                "parts",
                "from_packages",
                "from_package",
                "source_package",
                "source_id",
                "src_package",
                "part_package",
                "来源包",
                "原包",
                "输入",
            ),
        )
    )
    raw_outputs = _list_value(
        _first(
            row,
            (
                "outputs",
                "output",
                "targets",
                "results",
                "to_packages",
                "to_package",
                "target_package",
                "target_id",
                "dst_package",
                "merged_package",
                "combined_package",
                "产物",
                "新包",
                "输出",
                "接收包",
            ),
        )
    )
    parsed_inputs = [_parse_node(item, from_location) for item in raw_inputs]
    parsed_outputs = [
        _parse_node(item, to_location or from_location) for item in raw_outputs
    ]
    inputs = _combine_nodes([node for node in parsed_inputs if node["package"]])
    outputs = _combine_nodes([node for node in parsed_outputs if node["package"]])
    if kind == "merge":
        for node in inputs:
            if not node.get("location"):
                node["location"] = to_location or from_location
    if kind == "split":
        for node in outputs:
            if not node.get("location"):
                node["location"] = from_location or to_location
    if kind == "transfer":
        for node in inputs:
            if not node.get("location"):
                node["location"] = from_location
        for node in outputs:
            if not node.get("location"):
                node["location"] = to_location or from_location

    if kind == "transfer":
        if not inputs and package:
            inputs = [
                _parse_node({"package": package, "location": from_location, "qty": qty})
            ]
        if not outputs and package:
            outputs = [
                _parse_node(
                    {
                        "package": package,
                        "location": to_location,
                        "qty": qty,
                        "tags": _first(row, ("tags", "tag_ids", "标签"), []),
                    }
                )
            ]
    elif kind == "split":
        if not inputs and package:
            inputs = [
                _parse_node({"package": package, "location": from_location, "qty": qty})
            ]
        if not outputs:
            kind = "adjust"
    elif kind == "merge":
        if not outputs and package:
            outputs = [
                _parse_node({"package": package, "location": to_location, "qty": qty})
            ]
        if not inputs:
            kind = "adjust"
    elif kind in ("issue", "receive", "set", "adjust"):
        location = from_location or to_location or _identity(
            _first(row, STOCK_ALIASES["location"], "")
        )
        # A compact row may name the package column directly, while a
        # JSON-style row may only carry inputs; fill missing locations on
        # already-parsed nodes from the row-level location.
        for node in inputs:
            if not node.get("location"):
                node["location"] = location
        for node in outputs:
            if not node.get("location"):
                node["location"] = location
        if kind in ("issue", "set", "adjust") and not inputs and package:
            inputs = [
                _parse_node({"package": package, "location": location, "qty": qty})
            ]
        if kind == "receive" and not outputs and package:
            outputs = [
                _parse_node(
                    {
                        "package": package,
                        "location": location,
                        "qty": qty,
                        "tags": _list_value(_first(row, ("tags", "tag_ids", "标签"), [])),
                        "origins": _parse_origins(_first(row, STOCK_ALIASES["origins"])),
                    }
                )
            ]

    teams = [
        _team_key(t)
        for t in _list_value(
            _first(
                row,
                (
                    "signed_by",
                    "teams",
                    "team",
                    "handled_by",
                    "operator",
                    "clerk",
                    "班组",
                    "登记点",
                    "签收方",
                    "经办",
                ),
            )
        )
        if _team_key(t)
    ]
    variants = _list_value(
        _first(
            row,
            (
                "variants",
                "approved_variants",
                "alternatives",
                "comparisons",
                "approved_comparisons",
                "批准对照",
                "对照",
                "备选",
            ),
        )
    )
    return {
        "occurrence_id": occurrence_id,
        "kind": kind,
        "inputs": inputs,
        "outputs": outputs,
        "from_location": from_location,
        "to_location": to_location,
        "package": package,
        "qty": qty,
        "teams": teams,
        "status": _identity(
            _first(row, ("status", "state", "状态", "核对状态"), "confirmed")
        ).lower(),
        "approved": _truthy(_first(row, ("approved", "legal", "valid", "批准", "合法"), True)),
        "variants": variants,
        "order": index,
        "raw": dict(row),
    }


def parse_occurrences(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [_parse_occurrence(row, i) for i, row in enumerate(rows)]


def _fingerprint(op: Mapping[str, Any]) -> Tuple[Any, ...]:
    def side(nodes):
        return tuple(
            sorted(
                (
                    node.get("location", ""),
                    node["package"],
                    str(node.get("qty")),
                )
                for node in nodes
            )
        )

    return (
        op["kind"],
        op.get("from_location", ""),
        op.get("to_location", ""),
        side(op.get("inputs", [])),
        side(op.get("outputs", [])),
    )


def _dedupe_occurrences(
    occurrences: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse duplicate vouchers without losing either team's signed id.

    Two different occurrence ids may describe the *same* physical posting when
    each team signs its own copy of a pending transfer message.  They are
    collapsed only when the postings are identical, the teams are disjoint,
    and at least one copy is pending or the operation is a transfer.  The
    retained voucher records the other signed ids in ``also_signed_as`` so the
    acceptance checklist can still audit every archive record.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    for op in occurrences:
        oid = op["occurrence_id"]
        if oid not in by_id:
            by_id[oid] = copy.deepcopy(op)
            by_id[oid].setdefault("also_signed_as", [])
            continue
        existing = by_id[oid]
        if _fingerprint(existing) != _fingerprint(op):
            conflicts.append(
                {
                    "type": "shared_credential_divergence",
                    "occurrence_id": oid,
                    "detail": "the same occurrence id has different postings",
                    "teams": sorted(
                        set(existing.get("teams", [])) | set(op.get("teams", []))
                    ),
                }
            )
            continue
        existing["teams"] = sorted(
            set(existing.get("teams", [])) | set(op.get("teams", []))
        )
        if op.get("status") == "pending":
            existing["status"] = "pending"
        if op.get("variants"):
            existing["variants"] = list(existing.get("variants", [])) + list(
                op["variants"]
            )

    # Second pass: logical duplicates carrying different team-signed ids.
    logical: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for op in sorted(by_id.values(), key=lambda item: item.get("order", 0)):
        key = _fingerprint(op)
        buckets = logical.setdefault(key, [])
        placed = False
        for existing in buckets:
            teams_existing = set(existing.get("teams", []))
            teams_new = set(op.get("teams", []))
            disjoint = not (teams_existing & teams_new)
            pending = "pending" in (existing.get("status"), op.get("status"))
            transfer = op["kind"] == "transfer"
            if not (disjoint and (pending or transfer)):
                continue
            existing.setdefault("also_signed_as", []).append(op["occurrence_id"])
            existing["teams"] = sorted(teams_existing | teams_new)
            existing["status"] = "pending" if pending else existing.get("status")
            if op.get("variants"):
                existing["variants"] = list(existing.get("variants", [])) + list(
                    op["variants"]
                )
            placed = True
            break
        if not placed:
            buckets.append(op)
    ordered = sorted(
        (item for bucket_list in logical.values() for item in bucket_list),
        key=lambda item: item.get("order", 0),
    )
    return ordered, conflicts


# ---------------------------------------------------------------------------
# State and provenance
# ---------------------------------------------------------------------------


class ReplayError(Exception):
    """A replay step would violate stock, tag, or conservation rules."""

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.details = dict(details or {})


def _origin_dict(value: Any) -> Dict[str, Decimal]:
    result: Dict[str, Decimal] = defaultdict(Decimal)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        for key, qty in value.items():
            result[str(key)] += _dec(qty)
        return {k: v for k, v in result.items() if v != 0}
    for item in _list_value(value):
        if isinstance(item, Mapping):
            package = _identity(
                _first(item, ("package", "package_id", "origin", "原包", "来源包"))
            )
            qty = _first(item, ("qty", "quantity", "数量"))
            if package:
                result[package] += _dec(qty)
        else:
            result[str(item)] += Decimal(0)
    return {k: v for k, v in result.items() if v != 0}


def _spread_origins(origins: Mapping[str, Decimal], qty: Decimal) -> Dict[str, Decimal]:
    total = sum(origins.values(), Decimal(0))
    if qty == 0:
        return {}
    if total <= 0:
        return {}
    if total == qty:
        return {k: Decimal(v) for k, v in origins.items() if v != 0}
    scale = Decimal(qty) / Decimal(total)
    out: Dict[str, Decimal] = {}
    remaining = Decimal(qty)
    items = sorted(origins.items())
    for index, (origin, value) in enumerate(items):
        if index == len(items) - 1:
            portion = remaining
        else:
            portion = (Decimal(value) * scale).quantize(Decimal("0.000000001"))
            portion = min(portion, remaining)
        if portion <= 0:
            continue
        out[origin] = out.get(origin, Decimal(0)) + portion
        remaining -= portion
    if remaining > 0 and items:
        origin = max(items, key=lambda pair: pair[1])[0]
        out[origin] = out.get(origin, Decimal(0)) + remaining
    return {k: v for k, v in out.items() if v != 0}


@dataclass
class State:
    balances: Dict[Tuple[str, str], Decimal] = dc_field(default_factory=dict)
    provenance: Dict[Tuple[str, str], Dict[str, Decimal]] = dc_field(default_factory=dict)
    tags: Dict[str, Dict[str, Any]] = dc_field(default_factory=dict)
    applied: List[str] = dc_field(default_factory=list)

    def copy(self) -> "State":
        return State(
            {key: Decimal(value) for key, value in self.balances.items() if value != 0},
            {
                key: {origin: Decimal(qty) for origin, qty in origins.items() if qty != 0}
                for key, origins in self.provenance.items()
            },
            {tag: copy.deepcopy(info) for tag, info in self.tags.items()},
            list(self.applied),
        )

    def get(self, location: str, package: str) -> Decimal:
        return self.balances.get((location, package), Decimal(0))

    def set(self, location: str, package: str, qty: Decimal) -> None:
        key = (location, package)
        if qty == 0:
            self.balances.pop(key, None)
        else:
            self.balances[key] = Decimal(qty)

    def add(self, location: str, package: str, qty: Decimal) -> None:
        self.set(location, package, self.get(location, package) + Decimal(qty))

    def add_origins(
        self, location: str, package: str, origins: Mapping[str, Decimal]
    ) -> None:
        key = (location, package)
        bucket = self.provenance.setdefault(key, {})
        for origin, qty in origins.items():
            if qty == 0:
                continue
            bucket[str(origin)] = bucket.get(str(origin), Decimal(0)) + Decimal(qty)
        self.provenance[key] = {k: v for k, v in bucket.items() if v != 0}

    def take_origins(
        self, location: str, package: str, qty: Decimal
    ) -> Dict[str, Decimal]:
        key = (location, package)
        origins = self.provenance.get(key, {})
        if qty <= 0 or not origins:
            return {}
        taken = _spread_origins(origins, min(qty, sum(origins.values())))
        remaining = {}
        for origin, value in origins.items():
            left = Decimal(value) - taken.get(origin, Decimal(0))
            if left > 0:
                remaining[origin] = left
        self.provenance[key] = {k: v for k, v in remaining.items() if v != 0}
        return taken

    def search_signature(self) -> str:
        """Memo key for search branches (excludes the action path)."""
        payload = {
            "balances": sorted(
                [loc, pkg, str(value)]
                for (loc, pkg), value in self.balances.items()
                if value != 0
            ),
            "provenance": sorted(
                [
                    loc,
                    pkg,
                    sorted([origin, str(value)] for origin, value in origins.items()),
                ]
                for (loc, pkg), origins in self.provenance.items()
                if origins
            ),
            "tags": sorted(
                [
                    tag,
                    info.get("package", ""),
                    info.get("location", ""),
                    str(info.get("qty", 0)),
                    info.get("status", ""),
                    info.get("transferred_by", ""),
                ]
                for tag, info in self.tags.items()
            ),
        }
        return _signature(payload)

    def signature(self) -> str:
        payload = {
            "balances": sorted(
                [loc, pkg, str(value)]
                for (loc, pkg), value in self.balances.items()
                if value != 0
            ),
            "provenance": sorted(
                [
                    loc,
                    pkg,
                    sorted([origin, str(value)] for origin, value in origins.items()),
                ]
                for (loc, pkg), origins in self.provenance.items()
                if origins
            ),
            "tags": sorted(
                [
                    tag,
                    info.get("package", ""),
                    info.get("location", ""),
                    str(info.get("qty", 0)),
                    info.get("status", ""),
                    info.get("transferred_by", ""),
                ]
                for tag, info in self.tags.items()
            ),
            "applied": list(self.applied),
        }
        return _signature(payload)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "signature": self.signature(),
            "balances": [
                {"location": loc, "package": pkg, "qty": _money(value)}
                for (loc, pkg), value in sorted(self.balances.items())
                if value != 0
            ],
            "provenance": [
                {
                    "location": loc,
                    "package": pkg,
                    "origins": [
                        {"package": origin, "qty": _money(value)}
                        for origin, value in sorted(origins.items())
                    ],
                }
                for (loc, pkg), origins in sorted(self.provenance.items())
                if origins
            ],
            "tags": [
                {
                    "tag": tag,
                    "package": info.get("package", ""),
                    "location": info.get("location", ""),
                    "qty": _money(info.get("qty", 0)),
                    "status": info.get("status", ""),
                    "transferred_by": info.get("transferred_by", ""),
                }
                for tag, info in sorted(self.tags.items())
            ],
            "applied": list(self.applied),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "State":
        state = cls()
        for row in snapshot.get("balances", []):
            state.set(row["location"], row["package"], _dec(row["qty"]))
        for row in snapshot.get("provenance", []):
            for origin in row.get("origins", []):
                state.add_origins(
                    row["location"],
                    row["package"],
                    {origin["package"]: _dec(origin["qty"])},
                )
        for row in snapshot.get("tags", []):
            state.tags[row["tag"]] = {
                "package": row.get("package", ""),
                "location": row.get("location", ""),
                "qty": _dec(row.get("qty", 0)),
                "status": row.get("status", ""),
                "transferred_by": row.get("transferred_by", ""),
            }
        state.applied = list(snapshot.get("applied", []))
        return state


def state_from_stocktake(
    rows: Sequence[Mapping[str, Any]],
    *,
    generated_tags: bool = True,
) -> State:
    state = State()
    for row in rows:
        location = row["location"]
        package = row["package"]
        qty = row["qty"]
        state.add(location, package, qty)
        origins = _origin_dict(row.get("origins"))
        if not origins:
            origins = {package: qty}
        elif sum(origins.values()) != qty:
            origins = _spread_origins(origins, qty)
        state.add_origins(location, package, origins)
        for tag in row.get("tags") or []:
            state.tags[tag] = {
                "package": package,
                "location": location,
                "qty": qty,
                "status": "present",
                "origins": sorted(origins),
            }
        if generated_tags and not row.get("tags"):
            implicit = "pkg:{}".format(package)
            state.tags.setdefault(
                implicit,
                {
                    "package": package,
                    "location": location,
                    "qty": qty,
                    "status": "present",
                    "origins": sorted(origins),
                    "implicit": True,
                },
            )
    return state


# ---------------------------------------------------------------------------
# Occurrence legs
# ---------------------------------------------------------------------------


@dataclass
class Leg:
    direction: str
    node: Dict[str, Any]
    qty: Decimal
    role: str
    tags: List[str] = dc_field(default_factory=list)

    def copy_with_qty(self, qty: Decimal) -> "Leg":
        clone = copy.deepcopy(self)
        clone.qty = Decimal(qty)
        return clone


def _leg_tags(node: Mapping[str, Any], op: Mapping[str, Any], role: str) -> List[str]:
    """Only explicit tags live here; implicit ids are resolved from state."""
    return list(node.get("tags") or [])


def occurrence_legs(op: Mapping[str, Any]) -> List[Leg]:
    kind = op["kind"]
    legs: List[Leg] = []
    if kind == "split":
        source = op["inputs"][0]
        qty = source.get("qty")
        if qty is None:
            qty = sum((node.get("qty") or Decimal(0)) for node in op["outputs"])
        legs.append(
            Leg(
                "in",
                dict(source),
                Decimal(qty),
                "source",
                _leg_tags(source, op, "source"),
            )
        )
        for node in op["outputs"]:
            if node.get("qty") is None:
                raise ReplayError(
                    "split output has no quantity",
                    {"occurrence_id": op["occurrence_id"], "package": node["package"]},
                )
            legs.append(
                Leg(
                    "out",
                    dict(node),
                    node["qty"],
                    "piece",
                    _leg_tags(node, op, "piece"),
                )
            )
    elif kind == "merge":
        for node in op["inputs"]:
            if node.get("qty") is None:
                raise ReplayError(
                    "merge input has no quantity",
                    {"occurrence_id": op["occurrence_id"], "package": node["package"]},
                )
            legs.append(
                Leg(
                    "in",
                    dict(node),
                    node["qty"],
                    "piece",
                    _leg_tags(node, op, "piece"),
                )
            )
        target = op["outputs"][0]
        qty = target.get("qty")
        if qty is None:
            qty = sum((node.get("qty") or Decimal(0)) for node in op["inputs"])
        legs.append(
            Leg(
                "out",
                {**dict(target), "qty": Decimal(qty)},
                Decimal(qty),
                "output",
                _leg_tags(target, op, "output"),
            )
        )
    elif kind == "transfer":
        source = op["inputs"][0]
        target = op["outputs"][0]
        qty = source.get("qty")
        if qty is None:
            qty = target.get("qty")
        if qty is None:
            raise ReplayError(
                "transfer has no quantity",
                {"occurrence_id": op["occurrence_id"]},
            )
        tags = list(target.get("tags") or source.get("tags") or _leg_tags(source, op, "source"))
        legs.append(
            Leg(
                "in",
                {**dict(source), "qty": Decimal(qty)},
                Decimal(qty),
                "move",
                list(tags),
            )
        )
        legs.append(
            Leg(
                "out",
                {**dict(target), "package": source["package"], "qty": Decimal(qty)},
                Decimal(qty),
                "move",
                list(tags),
            )
        )
    elif kind == "issue":
        for node in op["inputs"]:
            if node.get("qty") is None:
                continue
            legs.append(
                Leg(
                    "in",
                    dict(node),
                    node["qty"],
                    "issue",
                    _leg_tags(node, op, "issue"),
                )
            )
    elif kind == "receive":
        for node in op["outputs"]:
            if node.get("qty") is None:
                continue
            legs.append(
                Leg(
                    "out",
                    dict(node),
                    node["qty"],
                    "receive",
                    _leg_tags(node, op, "receive"),
                )
            )
    return legs


def _node_origins(
    state: State,
    op: Mapping[str, Any],
    node: Mapping[str, Any],
    qty: Decimal,
    source_origins: Optional[Mapping[str, Decimal]] = None,
) -> Dict[str, Decimal]:
    explicit = _origin_dict(node.get("origins"))
    if explicit:
        if sum(explicit.values()) == qty:
            return explicit
        return _spread_origins(explicit, qty)
    if source_origins:
        return _spread_origins(source_origins, qty)
    return {node["package"]: qty}


def _check_negative(state: State, context: Mapping[str, Any]) -> None:
    for (location, package), qty in state.balances.items():
        if qty < -EPSILON:
            raise ReplayError(
                "negative inventory: {} at {}".format(package, location),
                {
                    **dict(context),
                    "package": package,
                    "location": location,
                    "available": _money(qty),
                },
            )


def _apply_leg(
    state: State,
    op: Mapping[str, Any],
    leg: Leg,
    origins: Mapping[str, Decimal],
    *,
    moved_tags: Optional[Set[str]] = None,
) -> None:
    """Apply one leg.  ``origins`` describes the quantity leaving/entering."""
    key = (leg.node.get("location", ""), leg.node["package"])
    qty = Decimal(leg.qty)
    if qty < 0:
        raise ReplayError(
            "leg quantity cannot be negative",
            {"occurrence_id": op["occurrence_id"], "package": leg.node["package"]},
        )
    if leg.direction == "in":
        available = state.get(*key)
        if available < qty - EPSILON:
            raise ReplayError(
                "insufficient stock: {} at {} has {}, needs {}".format(
                    leg.node["package"],
                    leg.node.get("location", ""),
                    _money(available),
                    _money(qty),
                ),
                {
                    "occurrence_id": op["occurrence_id"],
                    "package": leg.node["package"],
                    "location": leg.node.get("location", ""),
                    "available": _money(available),
                    "required": _money(qty),
                    "kind": op["kind"],
                },
            )
        state.add(key[0], key[1], -qty)
        if leg.role == "source" and op["kind"] == "split":
            for tag in leg.tags:
                info = state.tags.get(tag)
                if info:
                    info["qty"] = max(Decimal(0), _dec(info.get("qty", 0)) - qty)
                    if _dec(info.get("qty", 0)) <= EPSILON:
                        info["status"] = "consumed"
        if leg.role == "issue":
            for tag in leg.tags:
                info = state.tags.get(tag)
                if info:
                    info["status"] = "issued"
                    info["qty"] = Decimal(0)
                elif tag not in state.tags:
                    state.tags[tag] = {
                        "package": leg.node["package"],
                        "location": leg.node.get("location", ""),
                        "qty": Decimal(0),
                        "status": "issued",
                    }
        elif leg.role == "move":
            for tag in leg.tags:
                info = state.tags.get(tag)
                if info and info.get("transferred_by"):
                    raise ReplayError(
                        "physical tag has already been transferred once: {}".format(tag),
                        {
                            "occurrence_id": op["occurrence_id"],
                            "tag": tag,
                            "previous_occurrence": info.get("transferred_by"),
                            "kind": "tag_reused",
                        },
                    )
                if info:
                    info["status"] = "in_transit"
                    info["qty"] = qty
                    info["transferred_by"] = op["occurrence_id"]
            implicit = "pkg:{}".format(leg.node["package"])
            info = state.tags.get(implicit)
            if info and info.get("transferred_by"):
                raise ReplayError(
                    "physical package has already been transferred once: {}".format(
                        leg.node["package"]
                    ),
                    {
                        "occurrence_id": op["occurrence_id"],
                        "tag": implicit,
                        "previous_occurrence": info.get("transferred_by"),
                        "kind": "tag_reused",
                    },
                )
            if info:
                info["transferred_by"] = op["occurrence_id"]
                info["status"] = "in_transit"
            if moved_tags is not None:
                moved_tags.update(leg.tags)
                moved_tags.add(implicit)
        elif leg.role == "piece" and op["kind"] == "merge":
            for tag in leg.tags:
                info = state.tags.get(tag)
                if info:
                    info["status"] = "merged"
                    info["qty"] = Decimal(0)
                    info["merged_by"] = op["occurrence_id"]
        return

    # Outgoing stock, with the caller-supplied provenance for this side.
    state.add(key[0], key[1], qty)
    state.add_origins(key[0], key[1], origins)
    if leg.role in ("piece", "output", "receive"):
        for tag in leg.tags:
            state.tags[tag] = {
                "package": leg.node["package"],
                "location": leg.node.get("location", ""),
                "qty": qty,
                "status": "present",
                "origins": sorted(origins),
            }
    elif leg.role == "move":
        for tag in leg.tags:
            info = state.tags.get(tag)
            if info and info.get("transferred_by") not in (None, op["occurrence_id"]):
                raise ReplayError(
                    "physical tag has already been transferred once: {}".format(tag),
                    {
                        "occurrence_id": op["occurrence_id"],
                        "tag": tag,
                        "previous_occurrence": info.get("transferred_by"),
                        "kind": "tag_reused",
                    },
                )
            if info:
                info["package"] = leg.node["package"]
                info["location"] = leg.node.get("location", "")
                info["qty"] = qty
                info["status"] = "present"
                info["transferred_by"] = op["occurrence_id"]
            else:
                state.tags[tag] = {
                    "package": leg.node["package"],
                    "location": leg.node.get("location", ""),
                    "qty": qty,
                    "status": "present",
                    "origins": sorted(origins),
                    "transferred_by": op["occurrence_id"],
                }
            if moved_tags is not None:
                moved_tags.add(tag)
        implicit = "pkg:{}".format(leg.node["package"])
        info = state.tags.get(implicit)
        if info and info.get("transferred_by") not in (None, op["occurrence_id"]):
            raise ReplayError(
                "physical package has already been transferred once: {}".format(
                    leg.node["package"]
                ),
                {
                    "occurrence_id": op["occurrence_id"],
                    "tag": implicit,
                    "previous_occurrence": info.get("transferred_by"),
                    "kind": "tag_reused",
                },
            )
        if info:
            info["package"] = leg.node["package"]
            info["location"] = leg.node.get("location", "")
            info["qty"] = qty
            info["status"] = "present"
            info["transferred_by"] = op["occurrence_id"]
        else:
            state.tags[implicit] = {
                "package": leg.node["package"],
                "location": leg.node.get("location", ""),
                "qty": qty,
                "status": "present",
                "origins": sorted(origins),
                "transferred_by": op["occurrence_id"],
            }


def _resolve_leg_tags(state: State, leg: Leg, op: Mapping[str, Any]) -> None:
    """Use the live tag registry when an archive omits explicit tag ids."""
    if leg.tags:
        return
    package = leg.node["package"]
    location = leg.node.get("location", "")
    matches = [
        tag
        for tag, info in state.tags.items()
        if info.get("package") == package
        and info.get("status", "present") in ("present", "in_transit")
        and _dec(info.get("qty", 0)) > 0
        and (
            leg.role != "move"
            or not location
            or info.get("location") == location
        )
    ]
    if matches:
        leg.tags = sorted(matches)
    elif leg.role in ("piece", "output", "receive"):
        leg.tags = [
            "{}:{}:{}".format(TAG_PREFIX, op["occurrence_id"], package)
        ]
    else:
        leg.tags = ["pkg:{}".format(package)]


def apply_occurrence(
    state: State,
    op: Mapping[str, Any],
    *,
    leg_indexes: Optional[Sequence[int]] = None,
) -> None:
    kind = op["kind"]
    if kind in ("set", "adjust"):
        if kind == "set":
            for node in op["inputs"]:
                if node.get("qty") is None:
                    continue
                key = (node.get("location", ""), node["package"])
                old = state.get(*key)
                new = node["qty"]
                if new < old:
                    state.take_origins(*key, old - new)
                elif new > old:
                    state.add_origins(
                        *key, _node_origins(state, op, node, new - old)
                    )
                state.set(*key, new)
        else:
            for node in op["inputs"]:
                if node.get("qty") is None:
                    continue
                key = (node.get("location", ""), node["package"])
                delta = node["qty"]
                old = state.get(*key)
                new = old + delta
                if new < -EPSILON:
                    raise ReplayError(
                        "adjustment would make inventory negative",
                        {
                            "occurrence_id": op["occurrence_id"],
                            "package": node["package"],
                            "location": node.get("location", ""),
                            "available": _money(old),
                            "required": _money(-delta),
                            "kind": "adjust",
                        },
                    )
                if delta < 0:
                    state.take_origins(*key, -delta)
                elif delta > 0:
                    state.add_origins(
                        *key, _node_origins(state, op, node, delta)
                    )
                state.set(*key, new)
        if op["occurrence_id"] not in state.applied:
            state.applied.append(op["occurrence_id"])
        _check_negative(state, {"occurrence_id": op["occurrence_id"], "kind": kind})
        return

    if leg_indexes is None and kind in ("split", "merge"):
        _validate_occurrence(op)
    legs = occurrence_legs(op)
    if kind == "transfer" and not any(leg.tags for leg in legs):
        _resolve_leg_tags(state, legs[0], op)
        for leg in legs:
            leg.tags = list(legs[0].tags)
    indexes = list(range(len(legs))) if leg_indexes is None else list(leg_indexes)
    ordered = sorted(
        indexes,
        key=lambda index: (0 if legs[index].direction == "in" else 1, index),
    )
    taken_origins: Dict[str, Decimal] = defaultdict(Decimal)
    has_in_leg = any(legs[index].direction == "in" for index in ordered)
    if not has_in_leg:
        # A repair may apply only an output leg whose source was already
        # retired on the draft.  Its roots still come from the voucher's
        # source packages, not from the new package id.
        for source in op.get("inputs", []):
            key = (source.get("location", ""), source["package"])
            for origin, value in state.provenance.get(key, {}).items():
                taken_origins[origin] += value
    for index in ordered:
        if index < 0 or index >= len(legs):
            continue
        leg = legs[index]
        _resolve_leg_tags(state, leg, op)
        if leg.direction == "in":
            key = (leg.node.get("location", ""), leg.node["package"])
            taken = state.take_origins(*key, leg.qty)
            if not taken:
                # Untracked stock falls back to the physical package itself.
                taken = {leg.node["package"]: leg.qty}
            for origin, value in taken.items():
                taken_origins[origin] += value
            _apply_leg(state, op, leg, {}, moved_tags=set())
            continue
        # Distribute the accumulated provenance over the outgoing legs.
        if kind == "split":
            origins = _node_origins(state, op, leg.node, leg.qty, taken_origins)
        elif kind == "merge":
            origins = _node_origins(state, op, leg.node, leg.qty, taken_origins)
        elif kind == "transfer":
            origins = _node_origins(state, op, leg.node, leg.qty, taken_origins)
        else:
            origins = _node_origins(state, op, leg.node, leg.qty, taken_origins or None)
        _apply_leg(state, op, leg, origins, moved_tags=set())

    # Remove provisional provenance for consumed inputs when no output used it
    # (a repair leg may intentionally leave a consumed source unpaired).
    if op["occurrence_id"] not in state.applied:
        state.applied.append(op["occurrence_id"])
    _check_negative(state, {"occurrence_id": op["occurrence_id"], "kind": kind})


# ---------------------------------------------------------------------------
# Action search
# ---------------------------------------------------------------------------


@dataclass
class Action:
    occurrence_id: str
    kind: str
    mode: str  # whole | legs | write
    action_id: str
    variant: str = ""
    delta: Dict[Tuple[str, str], Decimal] = dc_field(default_factory=dict)
    leg_indexes: Tuple[int, ...] = ()
    leg_qtys: Dict[int, Decimal] = dc_field(default_factory=dict)
    cost: int = 0
    occurrence: Dict[str, Any] = dc_field(default_factory=dict)
    stateful: bool = False

    def effects(
        self, state: "State"
    ) -> Tuple[Dict[Tuple[str, str], Decimal], Dict[Tuple[str, str], Decimal]]:
        """Return ``(remove_before, add_after)`` for gap/pruning checks.

        Stateful actions (``set``) depend on the current value, so their
        effect is derived on demand from ``state``.
        """
        if self.stateful and self.kind == "set":
            remove: Dict[Tuple[str, str], Decimal] = {}
            add: Dict[Tuple[str, str], Decimal] = {}
            for node in self.occurrence.get("inputs", []):
                if node.get("qty") is None:
                    continue
                key = (node.get("location", ""), node["package"])
                current = state.get(*key)
                target = node["qty"]
                if target < current:
                    remove[key] = remove.get(key, Decimal(0)) + (current - target)
                elif target > current:
                    add[key] = add.get(key, Decimal(0)) + (target - current)
            return remove, add
        remove = {key: -value for key, value in self.delta.items() if value < 0}
        add = {key: value for key, value in self.delta.items() if value > 0}
        return remove, add

    def describe(self) -> Dict[str, Any]:
        op = self.occurrence
        item: Dict[str, Any] = {
            "action_id": self.action_id,
            "occurrence_id": self.occurrence_id,
            "kind": self.kind,
            "mode": self.mode,
            "inputs": [_describe_node(node) for node in op.get("inputs", [])],
            "outputs": [_describe_node(node) for node in op.get("outputs", [])],
            "delta": _describe_delta(self.delta),
        }
        if self.variant:
            item["variant"] = self.variant
        if op.get("teams"):
            item["teams"] = list(op["teams"])
        if self.mode == "legs":
            item["legs"] = list(self.leg_indexes)
            if self.leg_qtys:
                item["leg_quantities"] = {
                    str(index): _money(qty) for index, qty in sorted(self.leg_qtys.items())
                }
        return item


def _describe_node(node: Mapping[str, Any]) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "package": node["package"],
        "location": node.get("location", ""),
    }
    if node.get("qty") is not None:
        item["qty"] = _money(node["qty"])
    if node.get("tags"):
        item["tags"] = list(node["tags"])
    if node.get("origins"):
        item["origins"] = [
            {"package": origin, "qty": _money(qty)}
            for origin, qty in _origin_dict(node["origins"]).items()
        ]
    return item


def _describe_delta(delta: Mapping[Tuple[str, str], Decimal]) -> List[Dict[str, Any]]:
    return [
        {"location": loc, "package": pkg, "qty": _money(qty)}
        for (loc, pkg), qty in sorted(delta.items())
        if abs(qty) > EPSILON
    ]


def _leg_delta(
    legs: Sequence[Leg], qtys: Optional[Mapping[int, Decimal]] = None
) -> Dict[Tuple[str, str], Decimal]:
    delta: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    for index, leg in enumerate(legs):
        qty = leg.qty if qtys is None else qtys.get(index, leg.qty)
        key = (leg.node.get("location", ""), leg.node["package"])
        delta[key] += qty if leg.direction == "out" else -qty
    return {key: value for key, value in delta.items() if abs(value) > EPSILON}


def _write_delta(op: Mapping[str, Any]) -> Dict[Tuple[str, str], Decimal]:
    """Net delta for a ``set`` or ``adjust`` occurrence.

    For ``set`` the target value is absolute, so its delta cannot be known
    without the start state; the search treats it as state dependent and the
    delta is only a hint.  For ``adjust`` the node quantity is a signed delta.
    """
    delta: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    if op["kind"] == "adjust":
        for node in op.get("inputs", []):
            if node.get("qty") is None:
                continue
            delta[(node.get("location", ""), node["package"])] += node["qty"]
    else:
        for node in op.get("inputs", []):
            if node.get("qty") is None:
                continue
            delta[(node.get("location", ""), node["package"])] += node["qty"]
    return dict(delta)


def _occurrence_delta(op: Mapping[str, Any]) -> Dict[Tuple[str, str], Decimal]:
    if op["kind"] in ("set", "adjust"):
        return _write_delta(op)
    return _leg_delta(occurrence_legs(op))


def _variant_occurrence(
    op: Mapping[str, Any], variant: Mapping[str, Any], label: str
) -> Dict[str, Any]:
    clone = copy.deepcopy(op)
    for field in (
        "kind",
        "from_location",
        "to_location",
        "package",
        "qty",
        "teams",
        "occurrence_id",
        "approved",
        "status",
    ):
        if field in variant:
            clone[field] = variant[field]
    if "type" in variant and "kind" not in variant:
        clone["kind"] = _kind(variant["type"])
    if "inputs" in variant:
        clone["inputs"] = _combine_nodes(
            [_parse_node(item, clone.get("from_location", "")) for item in _list_value(variant["inputs"])]
        )
    if "outputs" in variant:
        clone["outputs"] = _combine_nodes(
            [_parse_node(item, clone.get("to_location", "")) for item in _list_value(variant["outputs"])]
        )
    clone["variant"] = label
    return clone


def _expanded_variants(op: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = op.get("variants") or []
    if not raw:
        return [op]
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        label = _identity(
            item.get("variant")
            or item.get("label")
            or item.get("occurrence_id")
            or "v{}".format(index + 1)
        )
        out.append(_variant_occurrence(op, item, label))
    return out or [op]


def _leg_subsets(count: int) -> List[Tuple[int, ...]]:
    if count <= 0:
        return []
    if count <= 4:
        return [
            tuple(i for i in range(count) if mask & (1 << i))
            for mask in range(1, 1 << count)
        ]
    return [tuple(range(count))] + [(i,) for i in range(count)]


def _validate_occurrence(op: Mapping[str, Any]) -> None:
    kind = op["kind"]
    if kind == "split":
        if len(op["inputs"]) != 1 or not op["outputs"]:
            raise ReplayError(
                "split needs exactly one source and at least one output",
                {"occurrence_id": op["occurrence_id"]},
            )
        source_qty = op["inputs"][0].get("qty")
        output_total = sum((n.get("qty") or Decimal(0)) for n in op["outputs"])
        if any((n.get("qty") or Decimal(0)) <= 0 for n in op["outputs"]):
            raise ReplayError(
                "split parts must be positive",
                {"occurrence_id": op["occurrence_id"]},
            )
        if source_qty is not None and output_total != source_qty:
            raise ReplayError(
                "split does not conserve quantity: source {} vs parts {}".format(
                    _money(source_qty), _money(output_total)
                ),
                {"occurrence_id": op["occurrence_id"]},
            )
    elif kind == "merge":
        if not op["inputs"] or len(op["outputs"]) != 1:
            raise ReplayError(
                "merge needs at least one input and exactly one output",
                {"occurrence_id": op["occurrence_id"]},
            )
        input_total = sum((n.get("qty") or Decimal(0)) for n in op["inputs"])
        if any((n.get("qty") or Decimal(0)) <= 0 for n in op["inputs"]):
            raise ReplayError(
                "merge inputs must be positive",
                {"occurrence_id": op["occurrence_id"]},
            )
        target_qty = op["outputs"][0].get("qty")
        if target_qty is not None and input_total != target_qty:
            raise ReplayError(
                "merge does not conserve quantity: inputs {} vs output {}".format(
                    _money(input_total), _money(target_qty)
                ),
                {"occurrence_id": op["occurrence_id"]},
            )
    elif kind == "transfer":
        if len(op["inputs"]) != 1 or len(op["outputs"]) != 1:
            raise ReplayError(
                "transfer needs exactly one source and one target",
                {"occurrence_id": op["occurrence_id"]},
            )
        if op["inputs"][0]["package"] != op["outputs"][0]["package"]:
            raise ReplayError(
                "transfer must keep the physical package id",
                {"occurrence_id": op["occurrence_id"]},
            )
        qty = op["inputs"][0].get("qty")
        if qty is None:
            qty = op["outputs"][0].get("qty")
        if qty is None or qty <= 0:
            raise ReplayError(
                "transfer must have a positive quantity",
                {"occurrence_id": op["occurrence_id"]},
            )
    elif kind in ("issue", "receive", "set", "adjust"):
        if not op["inputs"] and not op["outputs"]:
            raise ReplayError(
                "{} needs at least one package".format(kind),
                {"occurrence_id": op["occurrence_id"]},
            )


def _natural_key(value: Any) -> Tuple[Any, ...]:
    parts = re.split(r"([0-9]+)", _identity(value))
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _causal_order(occurrences: Sequence[Mapping[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    by_id: Dict[str, Mapping[str, Any]] = {op["occurrence_id"]: op for op in occurrences}
    producers: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    # A package that is both consumed and re-produced in the same occurrence
    # is a remainder being split again; such keys must not create dependency
    # edges (they would form a false cycle).
    churn_keys: Set[Tuple[str, str]] = set()
    for op in occurrences:
        input_keys = {
            (node.get("location", ""), node["package"])
            for node in op.get("inputs", [])
        }
        output_keys = {
            (node.get("location", ""), node["package"])
            for node in op.get("outputs", [])
        }
        churn_keys |= input_keys & output_keys
        for node in op.get("outputs", []):
            producers[(node.get("location", ""), node["package"])].add(op["occurrence_id"])
    edges: Dict[str, Set[str]] = defaultdict(set)
    indegree: Dict[str, int] = {oid: 0 for oid in by_id}

    for op in occurrences:
        oid = op["occurrence_id"]
        for node in op.get("inputs", []):
            key = (node.get("location", ""), node["package"])
            if key in churn_keys:
                continue
            for producer in producers.get(key, set()):
                if producer == oid or oid in edges[producer]:
                    continue
                edges[producer].add(oid)
                indegree[oid] += 1
    ready = sorted(
        (oid for oid, degree in indegree.items() if degree == 0),
        key=_natural_key,
    )
    ordered: List[str] = []
    while ready:
        oid = ready.pop(0)
        ordered.append(oid)
        for other in sorted(edges.get(oid, set())):
            indegree[other] -= 1
            if indegree[other] == 0:
                ready.append(other)
                ready.sort(key=_natural_key)
    conflicts: List[Dict[str, Any]] = []
    if len(ordered) != len(by_id):
        cycle = sorted(oid for oid, degree in indegree.items() if degree > 0)
        conflicts.append(
            {
                "type": "dependency_cycle",
                "occurrence_ids": cycle,
                "credentials": [
                    {
                        "occurrence_id": oid,
                        "teams": list(by_id[oid].get("teams", [])),
                    }
                    for oid in cycle
                ],
                "detail": "no common replay order exists without a manual decision",
            }
        )
        ordered.extend(cycle)
    return ordered, conflicts


def build_groups(
    occurrences: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ordered_ids, conflicts = _causal_order(occurrences)
    by_id = {op["occurrence_id"]: op for op in occurrences}
    groups: List[Dict[str, Any]] = []
    for oid in ordered_ids:
        op = by_id[oid]
        actions: List[Action] = []
        for variant in _expanded_variants(op):
            label = variant.get("variant", "")
            variant_id = _identity(variant.get("occurrence_id"))
            kind = variant["kind"]
            try:
                _validate_occurrence(variant)
            except ReplayError as ex:
                conflicts.append(
                    {
                        "type": "invalid_occurrence",
                        "occurrence_id": oid,
                        "detail": str(ex),
                        "details": ex.details,
                    }
                )
                continue
            if kind in ("set", "adjust"):
                actions.append(
                    Action(
                        occurrence_id=oid,
                        kind=kind,
                        mode="write",
                        action_id="{}:{}:write".format(
                            variant_id or oid, label or "full"
                        ),
                        variant=label,
                        delta=_write_delta(variant),
                        cost=0,
                        occurrence=variant,
                        stateful=kind == "set",
                    )
                )
                continue
            legs = occurrence_legs(variant)
            actions.append(
                Action(
                    occurrence_id=oid,
                    kind=kind,
                    mode="whole",
                    action_id="{}:{}:whole".format(
                        variant_id or oid, label or "full"
                    ),
                    variant=label,
                    delta=_leg_delta(legs),
                    cost=0,
                    occurrence=variant,
                )
            )
            # A repair may need only the "received into B" leg or only the
            # "retire the old A pack" leg.  Generate those partial options;
            # each still goes through the same non-negative guard.
            for indexes in _leg_subsets(len(legs)):
                partial = _leg_delta([legs[i] for i in indexes])
                actions.append(
                    Action(
                        occurrence_id=oid,
                        kind=kind,
                        mode="legs",
                        action_id="{}:{}:legs-{}".format(
                            variant_id or oid,
                            label or "full",
                            "-".join(map(str, indexes)),
                        ),
                        variant=label,
                        delta=partial,
                        leg_indexes=indexes,
                        cost=1 + len(indexes),
                        occurrence=variant,
                    )
                )
        groups.append({"occurrence_id": oid, "actions": actions, "occurrence": op})
    return groups, conflicts


def _gap(
    state: State, target: Mapping[Tuple[str, str], Decimal]
) -> Dict[Tuple[str, str], Decimal]:
    keys = set(state.balances) | set(target)
    out: Dict[Tuple[str, str], Decimal] = {}
    for key in keys:
        value = target.get(key, Decimal(0)) - state.get(*key)
        if abs(value) > EPSILON:
            out[key] = value
    return out



# ---------------------------------------------------------------------------
# Target matching, provenance checks, and plan assembly
# ---------------------------------------------------------------------------


def target_balances(rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Decimal]:
    out: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        out[(row["location"], row["package"])] += row["qty"]
    return dict(out)


def _target_origins(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Decimal]]:
    out: Dict[Tuple[str, str], Dict[str, Decimal]] = {}
    for row in rows:
        explicit = _origin_dict(row.get("origins"))
        if not explicit:
            continue
        key = (row["location"], row["package"])
        if key in out:
            merged = dict(out[key])
            for origin, qty in explicit.items():
                merged[origin] = merged.get(origin, Decimal(0)) + qty
            out[key] = merged
        else:
            out[key] = explicit
    return out


def _origins_match(
    actual: Mapping[str, Decimal],
    expected: Mapping[str, Decimal],
) -> bool:
    if not expected:
        return True
    for origin, value in expected.items():
        if abs(Decimal(actual.get(origin, 0)) - Decimal(value)) > EPSILON:
            return False
    for origin, value in actual.items():
        if origin not in expected and abs(Decimal(value)) > EPSILON:
            return False
    return True


def goal_met(
    state: State,
    target: Mapping[Tuple[str, str], Decimal],
    origins: Optional[Mapping[Tuple[str, str], Mapping[str, Decimal]]] = None,
) -> bool:
    gap = _gap(state, target)
    if gap:
        return False
    for key, expected in (origins or {}).items():
        if not _origins_match(state.provenance.get(key, {}), expected):
            return False
    return True




def _action_score(
    action: Action,
    legal_ids: Set[str],
    previous_packages: Set[Tuple[str, str]],
    action_teams: Mapping[str, Sequence[str]],
    preferred_team: Optional[str],
    preferred_ids: Optional[Set[str]] = None,
) -> int:
    """Lexicographic preference encoded as one integer.

    Thousands: keep the existing legal stocktake/occurrence id.
    Hundreds: avoid re-receiving an original package that already existed.
    Units: prefer whole vouchers and short repair legs.
    """
    score = 0
    candidates = {action.occurrence_id, action.action_id.split(":", 1)[0]}
    if preferred_ids and not (candidates & preferred_ids):
        score += 10000
    if legal_ids and not (candidates & legal_ids):
        score += 1000
    if action.mode == "whole":
        score += 0
    else:
        score += 1 + len(action.leg_indexes)
    for (location, package), delta in action.delta.items():
        if (
            delta > 0
            and (location, package) in previous_packages
            and action.kind not in ("split", "merge")
        ):
            score += 100
    if preferred_team:
        teams = action_teams.get(action.occurrence_id, [])
        if teams and preferred_team not in teams:
            score += 10
    return score


def search_plan(
    start: State,
    target: Mapping[Tuple[str, str], Decimal],
    target_origins: Mapping[Tuple[str, str], Mapping[str, Decimal]],
    groups: Sequence[Mapping[str, Any]],
    *,
    legal_ids: Optional[Set[str]] = None,
    previous_packages: Optional[Set[Tuple[str, str]]] = None,
    action_teams: Optional[Mapping[str, Sequence[str]]] = None,
    preferred_team: Optional[str] = None,
    preferred_ids: Optional[Set[str]] = None,
    location_limits: Optional[Mapping[str, Decimal]] = None,
    max_nodes: int = 300000,
) -> Optional[List[Action]]:
    """Branch-and-bound with provenance-aware goals and archive preferences."""
    suffix: List[Dict[Tuple[str, str], Tuple[Decimal, Decimal]]] = [
        {} for _ in range(len(groups) + 1)
    ]
    stateful_suffix: List[bool] = [False for _ in range(len(groups) + 1)]
    for index in range(len(groups) - 1, -1, -1):
        bounds = dict(suffix[index + 1])
        stateful_suffix[index] = stateful_suffix[index + 1]
        for action in groups[index]["actions"]:
            if action.stateful:
                stateful_suffix[index] = True
                continue
            for key, value in action.delta.items():
                positive, negative = bounds.get(key, (Decimal(0), Decimal(0)))
                if value > 0:
                    positive += value
                elif value < 0:
                    negative += value
                bounds[key] = (positive, negative)
        suffix[index] = bounds

    legal_ids = legal_ids or set()
    previous_packages = previous_packages or set()
    action_teams = action_teams or {}
    nodes = [0]
    exhausted = [False]
    memo: Dict[Tuple[Any, ...], Optional[Tuple[int, Tuple[str, ...]]]] = {}

    def solve(index: int, state: State) -> Optional[Tuple[int, List[Action]]]:
        nodes[0] += 1
        if nodes[0] > max_nodes:
            exhausted[0] = True
            return None
        key = (index, state.search_signature())
        if key in memo and memo[key] is None:
            return None
        gap = _gap(state, target)
        if not gap and goal_met(state, target, target_origins):
            return (0, [])
        if index >= len(groups):
            return None
        bounds = suffix[index]
        if not stateful_suffix[index]:
            for key, value in gap.items():
                positive, negative = bounds.get(key, (Decimal(0), Decimal(0)))
                if value > 0 and value > positive + EPSILON:
                    return None
                if value < 0 and value < negative - EPSILON:
                    return None
        actions = sorted(
            groups[index]["actions"],
            key=lambda action: (
                _action_score(
                    action,
                    legal_ids,
                    previous_packages,
                    action_teams,
                    preferred_team,
                    preferred_ids,
                ),
                action.action_id,
            ),
        )
        best: Optional[Tuple[int, List[Action]]] = None
        # Skipping a group is always allowed; an occurrence may have been
        # settled before the previous stocktake.
        choices: List[Optional[Action]] = [None] + actions
        for action in choices:
            if action is None:
                result = solve(index + 1, state)
                candidate = None if result is None else (result[0], list(result[1]))
            else:
                trial = state.copy()
                try:
                    apply_action(trial, action)
                    _check_location_limits(
                        trial,
                        location_limits or {},
                        {"action_id": action.action_id},
                        start.balances,
                    )
                except ReplayError:
                    continue
                result = solve(index + 1, trial)
                if result is None:
                    candidate = None
                else:
                    score = _action_score(
                        action,
                        legal_ids,
                        previous_packages,
                        action_teams,
                        preferred_team,
                        preferred_ids,
                    )
                    candidate = (
                        result[0] + score,
                        [action] + list(result[1]),
                    )
            if candidate is not None and (best is None or candidate[0] < best[0]):
                best = candidate
        memo[key] = (
            None
            if best is None
            else (best[0], tuple(action.action_id for action in best[1]))
        )
        return best

    result = solve(0, start)
    search_plan.last_exhausted = exhausted[0]
    return None if result is None else result[1]


def apply_action(state: State, action: Action) -> None:
    apply_occurrence(
        state,
        action.occurrence,
        leg_indexes=None if action.mode == "whole" else action.leg_indexes,
    )


# ---------------------------------------------------------------------------
# Repair options for half-posted teams
# ---------------------------------------------------------------------------


def _clone_occurrence(op: Mapping[str, Any], **changes: Any) -> Dict[str, Any]:
    clone = copy.deepcopy(op)
    clone.update(changes)
    if "inputs" in changes:
        clone["inputs"] = [dict(node) for node in changes["inputs"]]
    if "outputs" in changes:
        clone["outputs"] = [dict(node) for node in changes["outputs"]]
    return clone


def repair_options(op: Mapping[str, Any]) -> List[Action]:
    """Extra candidate actions for a half-posted voucher.

    Example: B has already recorded the 5-piece merge receipt, while A still
    carries the original 7 and has not retired it.  The archive may also only
    have retired the 3-piece leg.  These options let the planner compose a
    correction without re-receiving physical goods.
    """
    out: List[Action] = []
    kind = op["kind"]
    if kind not in ("split", "merge", "transfer"):
        return out
    legs = occurrence_legs(op)
    oid = op["occurrence_id"]
    # Received-only / retired-only partial legs are already generated by
    # build_groups.  Add a few semantic repair forms that are convenient for
    # operators and keep the original occurrence id.
    if kind == "split" and len(op["inputs"]) == 1:
        source = op["inputs"][0]
        for node in op["outputs"]:
            if node.get("qty") is None:
                continue
            repair = _clone_occurrence(
                op,
                inputs=[
                    {
                        **source,
                        "qty": node["qty"],
                    }
                ],
                outputs=[dict(node)],
                repair="split-piece-only",
            )
            try:
                _validate_occurrence(repair)
            except ReplayError:
                continue
            out.append(
                Action(
                    occurrence_id=oid,
                    kind=kind,
                    mode="whole",
                    action_id="{}:repair:split:{}".format(oid, node["package"]),
                    variant="repair",
                    delta=_occurrence_delta(repair),
                    cost=2,
                    occurrence=repair,
                )
            )
    if kind == "merge" and len(op["outputs"]) == 1:
        target = op["outputs"][0]
        for node in op["inputs"]:
            if node.get("qty") is None:
                continue
            repair = _clone_occurrence(
                op,
                inputs=[dict(node)],
                outputs=[
                    {
                        **target,
                        "qty": node["qty"],
                    }
                ],
                repair="merge-piece-only",
            )
            try:
                _validate_occurrence(repair)
            except ReplayError:
                continue
            out.append(
                Action(
                    occurrence_id=oid,
                    kind=kind,
                    mode="whole",
                    action_id="{}:repair:merge:{}".format(oid, node["package"]),
                    variant="repair",
                    delta=_occurrence_delta(repair),
                    cost=2,
                    occurrence=repair,
                )
            )
    return out


def _expand_actions(
    groups: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    for group in groups:
        actions = list(group["actions"]) + repair_options(group["occurrence"])
        # Deduplicate by net delta and kind; prefer whole-voucher options.
        unique: Dict[Tuple[Any, ...], Action] = {}
        for action in actions:
            key = (
                tuple(sorted(action.delta.items())),
                action.kind,
                action.mode,
                action.leg_indexes,
                action.action_id,
            )
            existing = unique.get(key)
            if existing is None or action.cost < existing.cost:
                unique[key] = action
        expanded.append({**group, "actions": list(unique.values())})
    return expanded


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------




def _leg_actions_for_team(
    action: Action, team: str
) -> List[Action]:
    """Assign an occurrence to exactly one delivery segment.

    A transfer may be *visible* to both teams because the pending message
    reaches both, but it must be *executed* once.  The owner is the first
    signing team, otherwise the source location's team.  Both checklists still
    show the voucher; the non-owner sees it as already settled.
    """
    op = action.occurrence
    legs = occurrence_legs(op)
    indexes = (
        list(range(len(legs)))
        if action.mode == "whole"
        else list(action.leg_indexes)
    )
    owner = ""
    teams = [t for t in op.get("teams", []) if t]
    if teams:
        owner = teams[-1] if len(teams) > 1 else teams[0]
    else:
        locations = [legs[i].node.get("location", "") for i in indexes]
        # A move is executed by the receiving location; other operations by
        # the source/owning location.
        ordered_locations = locations if action.kind == "transfer" else list(reversed(locations))
        for location in ordered_locations:
            if location.startswith(("A", "甲", "一")):
                owner = "A"
                break
            if location.startswith(("B", "乙", "二")):
                owner = "B"
                break
    owner = owner or team
    return [
        Action(
            occurrence_id=action.occurrence_id,
            kind=action.kind,
            mode=action.mode,
            action_id="{}@{}".format(action.action_id, owner),
            variant=action.variant,
            delta=action.delta,
            leg_indexes=action.leg_indexes,
            leg_qtys=action.leg_qtys,
            cost=action.cost,
            occurrence=op,
        )
    ]


def _segment_plan(
    start_snapshot: Mapping[str, Any],
    ordered_actions: Sequence[Tuple[str, Action]],
    target: Mapping[Tuple[str, str], Decimal],
    target_origins: Mapping[Tuple[str, str], Mapping[str, Decimal]],
    *,
    limits: Mapping[str, Optional[int]],
) -> Dict[str, Any]:
    """Build causally ordered segments, grouped into per-team handovers.

    The global order follows the repair plan.  A new segment starts when the
    owning team changes or the team's chunk limit is reached.  This keeps
    cross-team dependencies intact while giving each team a contiguous slice.
    """
    state = State.from_snapshot(start_snapshot)
    segments: List[Dict[str, Any]] = []
    overall: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for team, action in ordered_actions:
        limit = limits.get(team)
        if current is None or current["team"] != team:
            current = {
                "segment": len(segments) + 1,
                "team": team,
                "part": 1,
                "actions": [],
                "start": state.signature(),
                "start_index": len(overall),
            }
            segments.append(current)
        elif limit is not None and len(current["actions"]) >= limit:
            current = {
                "segment": len(segments) + 1,
                "team": team,
                "part": current["part"] + 1,
                "actions": [],
                "start": state.signature(),
                "start_index": len(overall),
            }
            segments.append(current)
        try:
            apply_action(state, action)
        except ReplayError as ex:
            current["error"] = {"action_id": action.action_id, **ex.details}
            current["end"] = state.signature()
            current["next_index"] = len(overall)
            return {
                "segments": segments,
                "final": state.snapshot(),
                "overall": overall,
                "complete": False,
            }
        described = action.describe()
        described["team"] = team
        current["actions"].append(described)
        overall.append(described)
        current["end"] = state.signature()
        current["next_index"] = len(overall)
        current["complete"] = goal_met(state, target, target_origins)
    return {
        "segments": segments,
        "final": state.snapshot(),
        "overall": overall,
        "complete": goal_met(state, target, target_origins),
    }


def _seed_archive_lineage(
    state: State, occurrences: Sequence[Mapping[str, Any]]
) -> None:
    """Attach archive lineage to packages already present on a draft sheet.

    If a draft already shows the merged 5 but has no origin metadata, the
    archive still tells us that 5 is 3 from A plus 2 from B0.  Seeding that
    fact avoids a fake "receive the 5 again" repair later.
    """
    lineage = archive_lineage(occurrences)
    for key, qty in list(state.balances.items()):
        expected = lineage.get(key)
        if not expected or qty == 0:
            continue
        current = state.provenance.get(key, {})
        # Only replace a self-referential / missing provenance.  Explicit
        # origins on the draft are authoritative and are left alone.
        if current and set(current) != {key[1]}:
            continue
        state.provenance[key] = _spread_origins(expected, qty)


def _action_sort_key(
    action: Action,
    legal_ids: Optional[Set[str]] = None,
    previous_packages: Optional[Set[Tuple[str, str]]] = None,
) -> Tuple[Any, ...]:
    legal_ids = legal_ids or set()
    previous_packages = previous_packages or set()
    re_receipt = 0
    for (location, package), delta in action.delta.items():
        if delta <= 0:
            continue
        if action.kind not in ("split", "merge"):
            re_receipt += 1
        if (location, package) in previous_packages:
            re_receipt += 1
    return (
        0 if action.mode == "whole" else 1,
        0
        if not legal_ids
        or ({action.occurrence_id, action.action_id.split(":", 1)[0]} & legal_ids)
        else 1,
        re_receipt,
        action.cost,
        action.variant,
        action.action_id,
    )


def _linear_whole_plan(
    start: State,
    target: Mapping[Tuple[str, str], Decimal],
    target_origins: Mapping[Tuple[str, str], Mapping[str, Decimal]],
    groups: Sequence[Mapping[str, Any]],
    location_limits: Optional[Mapping[str, Decimal]] = None,
    legal_ids: Optional[Set[str]] = None,
    preferred_ids: Optional[Set[str]] = None,
) -> Optional[List[Action]]:
    """Fast path for a consistent archive: one whole voucher per group.

    Real stocktake archives are normally already correct; only a handful need
    repair legs.  Trying the straightforward replay first keeps large archives
    fast.  If a whole voucher cannot be posted (e.g. a draft already contains
    its receipt), that group is skipped and the slower search takes over.
    """
    if goal_met(start, target, target_origins):
        # The two official sheets already agree; replaying vouchers that are
        # net-zero would be a re-post, not a repair.
        return []

    state = start.copy()
    chosen: List[Action] = []
    for group in groups:
        whole = [
            action
            for action in group["actions"]
            if action.mode == "whole"
            and (
                not legal_ids
                or (
                    {action.occurrence_id, action.action_id.split(":", 1)[0]}
                    & legal_ids
                )
            )
        ]
        if not whole:
            return None
        candidates: List[Tuple[Tuple[Any, ...], Action, State]] = []
        for action in whole:
            trial = state.copy()
            try:
                apply_action(trial, action)
                _check_location_limits(
                    trial,
                    location_limits or {},
                    {"action_id": action.action_id},
                    start.balances,
                )
            except ReplayError:
                continue
            candidates.append(
                (
                    _action_sort_key(
                        action,
                        preferred_ids or legal_ids,
                        set(start.balances),
                    ),
                    action,
                    trial,
                )
            )
        if not candidates:
            # Leave the group unposted; the full search can consider partial
            # legs or alternatives.
            continue
        _, action, state = min(candidates, key=lambda item: item[0])
        chosen.append(action)
    if not goal_met(state, target, target_origins):
        return None

    # Prune vouchers already reflected in the previous official sheet.  A
    # greedy backward pass is enough: removing an action is safe only if the
    # remaining sequence still reaches the target and preserves lineage.
    def replay(actions: Sequence[Action]) -> Optional[State]:
        probe = start.copy()
        for item in actions:
            try:
                apply_action(probe, item)
                _check_location_limits(
                    probe,
                    location_limits or {},
                    {"action_id": item.action_id},
                    start.balances,
                )
            except ReplayError:
                return None
        return probe

    # Remove redundant vouchers, preferring to drop ones whose occurrence id
    # is not an existing legal stocktake id.  Work by object identity so that
    # earlier removals cannot invalidate a later index.
    kept = list(chosen)
    order = sorted(
        chosen,
        key=lambda item: _action_sort_key(
            item, legal_ids or set(), set(start.balances)
        ),
        reverse=True,
    )
    for item in order:
        if item not in kept:
            continue
        trial = [action for action in kept if action is not item]
        probe = replay(trial)
        if probe is not None and goal_met(probe, target, target_origins):
            kept = trial
    return kept


def _group_keys(group: Mapping[str, Any]) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for action in group.get("actions", []):
        keys.update(action.delta)
        for node in action.occurrence.get("inputs", []) + action.occurrence.get("outputs", []):
            keys.add((node.get("location", ""), node["package"]))
    return keys


def _partition_groups(
    groups: Sequence[Mapping[str, Any]],
) -> List[List[Mapping[str, Any]]]:
    """Split groups into independent package-lineage components."""
    parent: Dict[int, int] = {i: i for i in range(len(groups))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: Dict[Tuple[str, str], int] = {}
    for index, group in enumerate(groups):
        for key in _group_keys(group):
            if key in owner:
                union(owner[key], index)
            else:
                owner[key] = index
    buckets: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for index, group in enumerate(groups):
        buckets[find(index)].append(group)
    return [buckets[key] for key in sorted(buckets)]


def _state_subset(
    state: State, keys: Set[Tuple[str, str]]
) -> State:
    balances = {key: value for key, value in state.balances.items() if key in keys}
    provenance = {
        key: origins for key, origins in state.provenance.items() if key in keys
    }
    tags = {
        tag: info
        for tag, info in state.tags.items()
        if (info.get("location", ""), info.get("package", "")) in keys
    }
    return State(balances, provenance, tags)


def _search_independent_components(
    start: State,
    target: Mapping[Tuple[str, str], Decimal],
    target_origins: Mapping[Tuple[str, str], Mapping[str, Decimal]],
    groups: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> Optional[List[Action]]:
    components = _partition_groups(groups)
    if len(components) <= 1:
        return None
    combined: List[Action] = []
    for component in components:
        keys = set()
        for group in component:
            keys |= _group_keys(group)
        local_start = _state_subset(start, keys)
        local_target = {key: value for key, value in target.items() if key in keys}
        local_origins = {
            key: value for key, value in target_origins.items() if key in keys
        }
        local = search_plan(
            local_start,
            local_target,
            local_origins,
            component,
            **kwargs,
        )
        if local is None:
            return None
        combined.extend(local)
    # Component searches see only local state.  Re-validate the combined
    # plan globally, especially for location-level availability limits.
    probe = start.copy()
    try:
        for action in combined:
            apply_action(probe, action)
            _check_location_limits(
                probe,
                kwargs.get("location_limits") or {},
                {"action_id": action.action_id},
                start.balances,
            )
    except ReplayError:
        return None
    if not goal_met(probe, target, target_origins):
        return None
    return combined


def render_plan(
    previous_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    occurrence_rows: Sequence[Mapping[str, Any]],
    *,
    team_order: Sequence[str] = ("A", "B"),
    segment_limits: Optional[Mapping[str, Optional[int]]] = None,
    rules: Any = None,
    max_nodes: int = 300000,
) -> Dict[str, Any]:
    """Build a machine-readable repair and acceptance plan."""
    previous = parse_stocktake(previous_rows)
    current = parse_stocktake(current_rows)
    raw_occurrences = parse_occurrences(occurrence_rows)
    occurrences, conflicts = _dedupe_occurrences(raw_occurrences)

    normalized_rules = normalize_rules(rules)
    if not normalized_rules["location_limits"]:
        seen: Dict[str, int] = defaultdict(int)
        filled: Dict[str, int] = defaultdict(int)
        declared: Dict[str, Decimal] = {}
        for row in previous + current:
            location = row["location"]
            seen[location] += 1
            if row.get("available") is not None:
                filled[location] += 1
                declared[location] = max(
                    declared.get(location, Decimal(0)), Decimal(row["available"])
                )
        normalized_rules["location_limits"] = {
            location: value
            for location, value in declared.items()
            if filled[location] == seen[location]
        }
    if normalized_rules["transfers"] is not None:
        allowed = normalized_rules["transfers"]
        kept: List[Dict[str, Any]] = []
        for op in occurrences:
            if _transfer_allowed(op, allowed):
                kept.append(op)
            else:
                conflicts.append(
                    {
                        "type": "illegal_transfer",
                        "occurrence_id": op["occurrence_id"],
                        "detail": "transfer pair {!r} is not in the approved relations".format(
                            (op.get("from_location"), op.get("to_location"))
                        ),
                    }
                )
        occurrences = kept

    start = state_from_stocktake(previous, generated_tags=False)
    _seed_archive_lineage(start, occurrences)
    target = target_balances(current)
    # A destination itself cannot create stock out of nowhere.  Every positive
    # target quantity must be traceable to the previous sheet or be produced
    # by the archive; unknown positive lines are a manual-review signal.
    produced_keys = {
        (node.get("location", ""), node["package"])
        for op in occurrences
        for node in op.get("outputs", [])
    }
    unknown_positive = {
        key: value
        for key, value in target.items()
        if value > EPSILON and key not in start.balances and key not in produced_keys
    }
    if unknown_positive:
        conflicts.append(
            {
                "type": "unexplained_stock",
                "detail": "current stocktake has positive packages with no archive source",
                "balances": [
                    {"location": loc, "package": pkg, "qty": _money(qty)}
                    for (loc, pkg), qty in sorted(unknown_positive.items())
                ],
            }
        )
        return {
            "status": "manual_review",
            "reason": "unexplained_stock",
            "conflicts": conflicts,
            "shared_credentials": [],
            "previous": [dict(row) for row in previous_rows],
            "current": [dict(row) for row in current_rows],
            "archive": [_occurrence_summary(op) for op in occurrences],
            "acceptance": _acceptance_lists(
                occurrences,
                expected_target_origins(current, occurrences),
                applied=set(),
            ),
        }

    negative_targets = {
        key: value for key, value in target.items() if value < -EPSILON
    }
    if negative_targets:
        conflicts.append(
            {
                "type": "negative_target",
                "detail": "current stocktake contains a negative quantity",
                "balances": [
                    {"location": loc, "package": pkg, "qty": _money(qty)}
                    for (loc, pkg), qty in sorted(negative_targets.items())
                ],
            }
        )
        return {
            "status": "manual_review",
            "reason": "invalid_target",
            "conflicts": conflicts,
            "shared_credentials": [],
            "previous": [dict(row) for row in previous_rows],
            "current": [dict(row) for row in current_rows],
            "archive": [_occurrence_summary(op) for op in occurrences],
            "acceptance": _acceptance_lists(
                occurrences,
                expected_target_origins(current, occurrences),
                applied=set(),
            ),
        }
    preferred_ids: Set[str] = set()
    for op in occurrences:
        for approved in _list_value(op["raw"].get("approved_ids")):
            value = _identity(approved)
            if value:
                preferred_ids.add(value)
    for row in current:
        if row.get("occurrence"):
            preferred_ids.add(row["occurrence"])

    target_origins = _target_origins(current)
    expected_origins = expected_target_origins(current, occurrences)
    for key, origins in expected_origins.items():
        target_origins.setdefault(key, origins)

    legal_ids: Set[str] = set()
    for op in occurrences:
        if not op.get("approved") or op.get("status") == "pending":
            continue
        approved_ids = {
            _identity(x)
            for x in _list_value(op["raw"].get("approved_ids"))
            if _identity(x)
        }
        if not approved_ids:
            legal_ids.add(op["occurrence_id"])
        # An explicit approved list replaces the base id; but a row with no
        # alternatives still keeps its own legal id even if approved_ids is
        # present (it may refer to a sibling team's copy).
        if not op.get("variants"):
            legal_ids.add(op["occurrence_id"])
        legal_ids.update(op.get("also_signed_as", []))
        legal_ids.update(approved_ids)
        for variant in op.get("variants") or []:
            if not isinstance(variant, Mapping):
                continue
            variant_id = _identity(
                variant.get("occurrence_id")
                or variant.get("stocktake_id")
                or variant.get("id")
            )
            if variant_id:
                legal_ids.add(variant_id)
    for row in previous + current:
        if row.get("occurrence"):
            legal_ids.add(row["occurrence"])
    groups, group_conflicts = build_groups(occurrences)
    conflicts.extend(group_conflicts)
    groups = _expand_actions(groups)

    hard_conflicts = [
        conflict
        for conflict in conflicts
        if conflict.get("type")
        in ("shared_credential_divergence", "illegal_transfer", "dependency_cycle")
    ]
    if hard_conflicts:
        return {
            "status": "manual_review",
            "reason": "shared_credential_conflict"
            if any(
                conflict.get("type") == "shared_credential_divergence"
                for conflict in hard_conflicts
            )
            else "illegal_or_cyclic_archive",
            "conflicts": conflicts,
            "shared_credentials": hard_conflicts,
            "previous": [dict(row) for row in previous_rows],
            "current": [dict(row) for row in current_rows],
            "archive": [_occurrence_summary(op) for op in occurrences],
            "acceptance": _acceptance_lists(
                occurrences,
                expected_target_origins(current, occurrences),
                applied=set(),
            ),
        }

    action_teams: Dict[str, Sequence[str]] = {
        op["occurrence_id"]: op.get("teams", []) for op in occurrences
    }
    # For a plan, each occurrence is executed by one team.  Team membership in
    # the archive is an audit fact, not a request to double-post it.
    preferred_team = team_order[0] if team_order else None
    plan = _linear_whole_plan(
        start,
        target,
        target_origins,
        groups,
        location_limits=normalized_rules.get("location_limits") or {},
        legal_ids=legal_ids,
        preferred_ids=preferred_ids,
    )
    if plan is None:
        search_kwargs = dict(
            legal_ids=legal_ids,
            previous_packages=set(start.balances),
            action_teams=action_teams,
            preferred_team=preferred_team,
            preferred_ids=preferred_ids,
            location_limits=normalized_rules.get("location_limits") or {},
            max_nodes=max_nodes,
        )
        plan = _search_independent_components(
            start, target, target_origins, groups, **search_kwargs
        )
    if plan is None:
        plan = search_plan(
            start,
            target,
            target_origins,
            groups,
            legal_ids=legal_ids,
            previous_packages=set(start.balances),
            action_teams=action_teams,
            preferred_team=preferred_team,
            preferred_ids=preferred_ids,
            location_limits=normalized_rules.get("location_limits") or {},
            max_nodes=max_nodes,
        )
    if plan is None:
        if not conflicts and getattr(search_plan, "last_exhausted", False):
            conflicts.append(
                {
                    "type": "search_exhausted",
                    "detail": "the repair search exceeded its branch limit; "
                    "split the archive into smaller occurrence ranges or raise max_nodes",
                }
            )
        if not conflicts:
            if normalized_rules.get("location_limits"):
                conflicts.append(
                    {
                        "type": "availability_conflict",
                        "detail": "no replay fits the supplied location availability",
                        "limits": {
                            key: _money(value)
                            for key, value in normalized_rules["location_limits"].items()
                        },
                    }
                )
            else:
                # Show what remains after the best straightforward replay of
                # whole vouchers, not merely the raw sheet-to-sheet delta.
                state_after = start.copy()
                for group in groups:
                    posted = False
                    for action in group["actions"]:
                        if action.mode != "whole":
                            continue
                        trial = state_after.copy()
                        try:
                            apply_action(trial, action)
                        except ReplayError:
                            continue
                        state_after = trial
                        posted = True
                        break
                    if not posted:
                        continue
                gap = _gap(state_after, target)
                if gap:
                    conflicts.append(
                        {
                            "type": "unexplained_difference",
                            "detail": "the archive cannot explain the change between the two sheets",
                            "difference": [
                                {
                                    "location": loc,
                                    "package": pkg,
                                    "qty": _money(qty),
                                }
                                for (loc, pkg), qty in sorted(gap.items())
                            ],
                        }
                    )
        return {
            "status": "manual_review",
            "reason": "no_common_replay",
            "conflicts": conflicts,
            "shared_credentials": [
                conflict
                for conflict in conflicts
                if conflict.get("type") in ("shared_credential_divergence", "dependency_cycle")
            ],
            "previous": [dict(row) for row in previous_rows],
            "current": [dict(row) for row in current_rows],
            "archive": [_occurrence_summary(op) for op in occurrences],
            "acceptance": _acceptance_lists(
                occurrences, expected_target_origins(current, occurrences), applied=set()
            ),
        }

    final = start.copy()
    actions_by_team: Dict[str, List[Action]] = defaultdict(list)
    ordered_actions: List[Tuple[str, Action]] = []
    for action in plan:
        pieces = _leg_actions_for_team(action, preferred_team or "shared")
        if not pieces:
            pieces = [action]
        for piece in pieces:
            team = (
                piece.action_id.rsplit("@", 1)[-1]
                if "@" in piece.action_id
                else (preferred_team or "shared")
            )
            actions_by_team[team].append(piece)
            ordered_actions.append((team, piece))
    # Replay for final provenance in the canonical order.
    for action in plan:
        apply_action(final, action)

    ordered_teams = list(team_order) + sorted(
        team for team in actions_by_team if team not in team_order
    )
    segmented = _segment_plan(
        start.snapshot(),
        ordered_actions,
        target,
        target_origins,
        limits=dict(segment_limits or {}),
    )

    applied_ids = {action.occurrence_id for action in plan}
    acceptance = _acceptance_lists(
        occurrences, target_origins, applied=applied_ids
    )
    return {
        "status": "ok",
        "previous": [dict(row) for row in previous_rows],
        "current": [dict(row) for row in current_rows],
        "start": start.snapshot(),
        "target": {
            "balances": [
                {"location": loc, "package": pkg, "qty": _money(qty)}
                for (loc, pkg), qty in sorted(target.items())
                if qty != 0
            ],
            "origins": [
                {
                    "location": loc,
                    "package": pkg,
                    "origins": [
                        {"package": origin, "qty": _money(qty)}
                        for origin, qty in sorted(origins.items())
                    ],
                }
                for (loc, pkg), origins in sorted(target_origins.items())
            ],
        },
        "actions": [action.describe() for action in plan],
        "selected": [
            {
                "occurrence_id": action.occurrence_id,
                "action_id": action.action_id,
                "mode": action.mode,
                "variant": action.variant or None,
                "legs": list(action.leg_indexes),
            }
            for action in plan
        ],
        "final": final.snapshot(),
        "final_matches_target": goal_met(final, target, target_origins),
        "segments": segmented["segments"],
        "execution": segmented.get("overall", []),
        "teams": {
            team: [action.describe() for action in actions_by_team.get(team, [])]
            for team in ordered_teams
            if actions_by_team.get(team)
        },
        "acceptance": acceptance,
        "archive": [_occurrence_summary(op) for op in occurrences],
        "rules": {
            "transfers": sorted(
                [list(pair) for pair in normalized_rules["transfers"]]
            )
            if normalized_rules["transfers"] is not None
            else None,
            "location_limits": {
                key: _money(value)
                for key, value in normalized_rules.get("location_limits", {}).items()
            },
        },
        "conflicts": conflicts,
        "shared_credentials": [
            conflict
            for conflict in conflicts
            if conflict.get("type")
            in ("shared_credential_divergence", "illegal_transfer")
        ],
    }


def _occurrence_summary(op: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "occurrence_id": op["occurrence_id"],
        "also_signed_as": list(op.get("also_signed_as", [])),
        "kind": op["kind"],
        "teams": list(op.get("teams", [])),
        "status": op.get("status", ""),
        "approved": bool(op.get("approved")),
        "inputs": [_describe_node(node) for node in op.get("inputs", [])],
        "outputs": [_describe_node(node) for node in op.get("outputs", [])],
    }


def _acceptance_lists(
    occurrences: Sequence[Mapping[str, Any]],
    target_origins: Mapping[Tuple[str, str], Mapping[str, Decimal]],
    *,
    applied: Set[str],
) -> Dict[str, Any]:
    archive = [_occurrence_summary(op) for op in occurrences]
    expanded_archive: List[Dict[str, Any]] = []
    for item in archive:
        expanded_archive.append(item)
        for alias in item.get("also_signed_as", []):
            expanded_archive.append(
                {
                    **item,
                    "occurrence_id": alias,
                    "also_signed_as": [],
                    "primary_occurrence_id": item["occurrence_id"],
                }
            )
    issue_merge = [
        item
        for item in expanded_archive
        if item["kind"] in ("issue", "merge")
    ]
    receipt = []
    for item in issue_merge:
        primary = item.get("primary_occurrence_id") or item["occurrence_id"]
        receipt.append(
            {
                "occurrence_id": item["occurrence_id"],
                "primary_occurrence_id": primary,
                "kind": item["kind"],
                "replayed": primary in applied,
                "inputs": item["inputs"],
                "outputs": item["outputs"],
            }
        )
    lineage = archive_lineage(occurrences)
    journal = []
    for item in expanded_archive:
        if item["kind"] not in ("issue", "merge"):
            continue
        outputs = []
        for node in item["outputs"]:
            key = (node.get("location", ""), node["package"])
            outputs.append(
                {
                    "package": node["package"],
                    "location": node.get("location", ""),
                    "expected_origins": [
                        {"package": origin, "qty": _money(qty)}
                        for origin, qty in sorted(lineage.get(key, {}).items())
                    ],
                }
            )
        primary = item.get("primary_occurrence_id") or item["occurrence_id"]
        journal.append(
            {
                "occurrence_id": item["occurrence_id"],
                "primary_occurrence_id": primary,
                "also_signed_as": item.get("also_signed_as", []),
                "kind": item["kind"],
                "replayed": primary in applied,
                "teams": item.get("teams", []),
                "inputs": item["inputs"],
                "outputs": outputs,
                "detail": "archive lineage",
            }
        )
    origin_cover: Dict[str, Decimal] = defaultdict(Decimal)
    for item in archive:
        nodes = item["inputs"] or item["outputs"]
        for node in nodes:
            key = (node.get("location", ""), node["package"])
            explicit = _origin_dict(node.get("origins"))
            if explicit:
                for origin, qty in explicit.items():
                    origin_cover[str(origin)] += qty
            elif key in lineage:
                for origin, qty in lineage[key].items():
                    origin_cover[str(origin)] += qty
            else:
                origin_cover[node["package"]] += _dec(node.get("qty", 0))
    uncovered = []
    for key, expected in target_origins.items():
        if key not in lineage and key not in {
            (node.get("location", ""), node["package"])
            for item in archive
            for node in item["outputs"]
        }:
            continue
        expected_roots = {origin for origin, qty in expected.items() if qty != 0}
        if expected_roots and not expected_roots <= set(origin_cover):
            uncovered.append(
                {
                    "package": key[1],
                    "location": key[0],
                    "missing_origins": sorted(expected_roots - set(origin_cover)),
                }
            )
    replay_ids = set(applied)
    for item in archive:
        if item["occurrence_id"] in applied:
            replay_ids.update(item.get("also_signed_as", []))
    unapplied = [
        item["occurrence_id"]
        for item in receipt
        if item["occurrence_id"] not in replay_ids
    ]
    # Two independent teams each receive the full archive list.  A record
    # owned by the other team is still shown, marked ``other_team``; this is
    # what lets the two checklists audit *all* original issue and merge
    # records, not just the segment each team executed.
    teams = sorted(
        {"A", "B"}
        | {
            team
            for item in archive
            for team in item.get("teams", [])
            if team
        }
    )
    checklists: Dict[str, Any] = {}
    for team in teams:
        entries = []
        for item in journal:
            owner = item.get("teams") or []
            entry = {
                "occurrence_id": item["occurrence_id"],
                "primary_occurrence_id": item.get("primary_occurrence_id"),
                "also_signed_as": item.get("also_signed_as", []),
                "kind": item["kind"],
                "replayed": item["replayed"],
                "owner": owner[0] if owner else None,
                "shared": not owner,
                "other_team": bool(owner and team not in owner),
                "inputs": item["inputs"],
                "outputs": item["outputs"],
            }
            entries.append(entry)
        checklists[team] = {
            "team": team,
            "entries": entries,
            "archive_ids": [item["occurrence_id"] for item in issue_merge],
            "matches_archive": [entry["occurrence_id"] for entry in entries]
            == [item["occurrence_id"] for item in issue_merge],
        }
    signed_id_count = sum(
        1 + len(item.get("also_signed_as", [])) for item in archive
    )
    return {
        "issue_and_merge": issue_merge,
        "receipt_check": receipt,
        "journal": journal,
        "checklists": checklists,
        "archive_count": signed_id_count,
        "logical_count": len(archive),
        "issue_merge_count": len(issue_merge),
        "origin_coverage": {
            origin: _money(qty) for origin, qty in sorted(origin_cover.items())
        },
        "uncovered_origins": uncovered,
        "unapplied_records": unapplied,
        # Every archive issue/merge record is listed on both acceptance
        # returns.  ``complete`` means the two lists reconcile to the archive
        # and no target lineage is missing; historical records already
        # reflected in the previous sheet may legitimately appear as
        # ``replayed: false`` with their disposition still recorded.
        "complete": not uncovered
        and len(receipt) == len(issue_merge)
        and all(item["matches_archive"] for item in checklists.values()),
    }


# ---------------------------------------------------------------------------
# Archive lineage expectations
# ---------------------------------------------------------------------------


def archive_lineage(
    occurrences: Sequence[Mapping[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Decimal]]:
    """Derive expected root provenance for every archive output.

    This is deliberately computed from the vouchers alone, not from whatever
    the replayer happens to do.  It gives the acceptance check an independent
    answer to "which original pack should this 5 come from?".
    """
    lineage: Dict[Tuple[str, str], Dict[str, Decimal]] = {}
    ordered, _ = _causal_order(occurrences)
    by_id = {op["occurrence_id"]: op for op in occurrences}

    def roots_of(node: Mapping[str, Any], qty: Decimal) -> Dict[str, Decimal]:
        key = (node.get("location", ""), node["package"])
        explicit = _origin_dict(node.get("origins"))
        if explicit:
            return _spread_origins(explicit, qty)
        known = lineage.get(key)
        if known:
            return _spread_origins(known, qty)
        return {node["package"]: qty}

    for oid in ordered:
        op = by_id[oid]
        kind = op["kind"]
        if kind == "split":
            source = op["inputs"][0]
            source_qty = source.get("qty")
            outputs_total = sum((n.get("qty") or Decimal(0)) for n in op["outputs"])
            if source_qty is None:
                source_qty = outputs_total
            source_roots = roots_of(source, Decimal(source_qty))
            for node in op["outputs"]:
                qty = node.get("qty")
                if qty is None:
                    continue
                lineage[(node.get("location", ""), node["package"])] = _spread_origins(
                    source_roots, qty
                )
        elif kind == "merge":
            merged: Dict[str, Decimal] = defaultdict(Decimal)
            total = Decimal(0)
            for node in op["inputs"]:
                qty = node.get("qty")
                if qty is None:
                    continue
                total += qty
                for origin, value in roots_of(node, qty).items():
                    merged[origin] += value
            target = op["outputs"][0]
            qty = target.get("qty")
            if qty is None:
                qty = total
            lineage[(target.get("location", ""), target["package"])] = _spread_origins(
                dict(merged), qty
            )
        elif kind == "transfer":
            source = op["inputs"][0]
            target = op["outputs"][0]
            qty = source.get("qty")
            if qty is None:
                qty = target.get("qty") or Decimal(0)
            lineage[(target.get("location", ""), source["package"])] = roots_of(
                source, qty
            )
        elif kind == "receive":
            for node in op["outputs"]:
                qty = node.get("qty")
                if qty is None:
                    continue
                lineage[(node.get("location", ""), node["package"])] = roots_of(
                    node, qty
                )
    return lineage


def expected_target_origins(
    current_rows: Sequence[Mapping[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Decimal]]:
    """Expected provenance for the current stocktake.

    Explicit target origins win.  Otherwise use archive lineage if the package
    was produced by a split/merge/transfer; if it was untouched, it is its own
    origin.
    """
    lineage = archive_lineage(occurrences)
    out: Dict[Tuple[str, str], Dict[str, Decimal]] = {}
    totals: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    explicit_all: Dict[Tuple[str, str], Dict[str, Decimal]] = {}
    for row in current_rows:
        key = (row["location"], row["package"])
        totals[key] += row["qty"]
        explicit = _origin_dict(row.get("origins"))
        if explicit:
            bucket = explicit_all.setdefault(key, defaultdict(Decimal))
            scaled = _spread_origins(explicit, row["qty"])
            for origin, value in scaled.items():
                bucket[origin] += value
    for key, qty in totals.items():
        if key in explicit_all:
            out[key] = _spread_origins(dict(explicit_all[key]), qty)
        elif key in lineage:
            out[key] = _spread_origins(lineage[key], qty)
        else:
            out[key] = {key[1]: qty}
    return out


# ---------------------------------------------------------------------------
# Segment execution from a checkpoint
# ---------------------------------------------------------------------------


def execute_segment(
    plan: Mapping[str, Any],
    *,
    team: Optional[str] = None,
    segment_offset: int = 0,
    limit: Optional[int] = None,
    from_segment: int = 1,
) -> Dict[str, Any]:
    """Deliver one resumable segment to one team.

    Segments are handed over in order.  To execute segment ``n`` the runner
    re-reads the official previous stocktake and replays segments ``1..n-1``
    (both teams' segments; that is the shared checkpoint), then applies only
    segment ``n``.  ``limit`` pauses inside that segment and returns the
    segment number to re-read next.
    """
    segments = list(plan.get("segments", []))
    target = {
        (row["location"], row["package"]): _dec(row["qty"])
        for row in plan.get("target", {}).get("balances", [])
    }
    target_origins = {
        (row["location"], row["package"]): {
            origin["package"]: _dec(origin["qty"])
            for origin in row.get("origins", [])
        }
        for row in plan.get("target", {}).get("origins", [])
    }

    state = State.from_snapshot(plan["start"])
    applied: List[str] = []
    chosen: List[str] = []
    started = False

    limits = normalize_rules(plan.get("rules"))["location_limits"] if plan.get("rules") else {}
    baseline = {
        (row["location"], row["package"]): _dec(row["qty"])
        for row in plan.get("start", {}).get("balances", [])
    }

    def replay(description: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        action = _action_from_description(description, plan)
        try:
            apply_action(state, action)
            _check_location_limits(
                state,
                limits,
                {"action_id": action.action_id},
                baseline,
            )
        except ReplayError as ex:
            return ex.details or {"detail": str(ex)}
        applied.append(action.action_id)
        return None

    for segment in segments:
        number = segment.get("segment", 1)
        if number < from_segment:
            continue
        if team is not None and segment.get("team") not in (None, team):
            continue
        if not started:
            for prior in segments:
                if prior.get("segment", 1) >= number:
                    break
                for description in prior.get("actions", []):
                    error = replay(description)
                    if error is not None:
                        return {
                            "status": "blocked",
                            "error": error,
                            "applied": applied,
                            "actions": chosen,
                            "checkpoint": {
                                "next_segment": prior.get("segment", 1),
                                "signature": state.signature(),
                                "snapshot": state.snapshot(),
                            },
                            "complete": False,
                        }
            started = True
        actions_in_segment = segment.get("actions", [])
        local_offset = segment_offset if number == from_segment else 0
        segment_offset = 0
        cursor = local_offset
        for description in actions_in_segment[local_offset:]:
            if limit is not None and len(chosen) >= limit:
                offset = cursor
                next_segment = number
                if offset >= len(actions_in_segment):
                    next_segment = number + 1
                    offset = 0
                return {
                    "status": "paused",
                    "team": segment.get("team", team),
                    "applied": applied,
                    "actions": chosen,
                    "checkpoint": {
                        "next_segment": next_segment,
                        "next_offset": offset,
                        "signature": state.signature(),
                        "snapshot": state.snapshot(),
                        "applied": list(state.applied),
                    },
                    "complete": False,
                }
            cursor += 1
            error = replay(description)
            if error is not None:
                return {
                    "status": "blocked",
                    "team": segment.get("team", team),
                    "error": error,
                    "applied": applied,
                    "actions": chosen,
                    "checkpoint": {
                        "next_segment": number,
                        "next_offset": cursor,
                        "signature": state.signature(),
                        "snapshot": state.snapshot(),
                    },
                    "complete": False,
                }
            chosen.append(description.get("action_id", ""))
    complete = goal_met(state, target, target_origins)
    return {
        "status": "complete",
        "team": team,
        "applied": applied,
        "actions": chosen,
        "checkpoint": {
            "next_segment": (
                max(item.get("segment", 1) for item in segments) + 1
                if segments
                else 1
            ),
            "signature": state.signature(),
            "snapshot": state.snapshot(),
            "applied": list(state.applied),
        },
        "complete": complete,
        "final": state.snapshot(),
    }




def _action_from_description(
    description: Mapping[str, Any], plan: Mapping[str, Any]
) -> Action:
    occurrence_id = description.get("occurrence_id")
    archive = {
        item["occurrence_id"]: item for item in plan.get("archive", [])
    }
    source = archive.get(occurrence_id, {})
    inputs = [
        _node_from_description(node) for node in description.get("inputs", [])
    ]
    outputs = [
        _node_from_description(node) for node in description.get("outputs", [])
    ]
    occurrence = {
        "occurrence_id": occurrence_id,
        "kind": description.get("kind", source.get("kind", "adjust")),
        "inputs": inputs,
        "outputs": outputs,
        "teams": source.get("teams", []),
        "status": source.get("status", ""),
        "variants": [],
        "raw": {},
    }
    mode = description.get("mode")
    if mode == "legs":
        return Action(
            occurrence_id=occurrence_id,
            kind=occurrence["kind"],
            mode="legs",
            action_id=description["action_id"],
            variant=description.get("variant") or "",
            delta={},
            leg_indexes=tuple(description.get("legs", [])),
            leg_qtys={
                int(index): _dec(qty)
                for index, qty in (description.get("leg_quantities") or {}).items()
            },
            occurrence=occurrence,
        )
    return Action(
        occurrence_id=occurrence_id,
        kind=occurrence["kind"],
        mode="whole" if mode == "whole" else mode,
        action_id=description["action_id"],
        variant=description.get("variant") or "",
        occurrence=occurrence,
    )


def _node_from_description(node: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "package": node["package"],
        "location": node.get("location", ""),
        "qty": _dec(node["qty"]) if node.get("qty") is not None else None,
        "tags": list(node.get("tags") or []),
        "origins": [
            {"package": origin["package"], "qty": origin["qty"]}
            for origin in node.get("origins") or []
        ],
    }


# ---------------------------------------------------------------------------
# Public plan entry point
# ---------------------------------------------------------------------------


def plan_reconciliation(
    previous,
    current,
    occurrences,
    *,
    format: Optional[str] = None,
    team_order: Sequence[str] = ("A", "B"),
    segment_limits: Optional[Mapping[str, Optional[int]]] = None,
    rules: Any = None,
    max_nodes: int = 300000,
) -> Dict[str, Any]:
    """Compute a repair plan from file paths, file objects, or row lists."""
    previous_rows = read_rows(previous, format)
    current_rows = read_rows(current, format)
    occurrence_rows = read_rows(occurrences, format)
    result = render_plan(
        previous_rows,
        current_rows,
        occurrence_rows,
        team_order=team_order,
        segment_limits=segment_limits,
        rules=rules,
        max_nodes=max_nodes,
    )
    result["files"] = {
        "previous": _source_name(previous),
        "current": _source_name(current),
        "occurrences": _source_name(occurrences),
    }
    return result


def _source_name(source: Any) -> Optional[str]:
    if source is None:
        return None
    if hasattr(source, "name"):
        return str(source.name)
    if isinstance(source, (str, bytes, os.PathLike)):
        return os.fspath(source)
    return None


def _action_team(description: Mapping[str, Any]) -> str:
    team = description.get("team")
    if team:
        return str(team)
    action_id = description.get("action_id", "")
    if "@" in action_id:
        return action_id.rsplit("@", 1)[-1]
    teams = description.get("teams") or []
    return teams[0] if teams else "shared"


def render_text(plan: Mapping[str, Any]) -> str:
    """Human-readable repair note suitable for two independent teams."""
    lines: List[str] = []
    if plan.get("status") != "ok":
        lines.append("需主管处理：找不到可共同重放的修正顺序。")
        lines.append("")
        shared = plan.get("shared_credentials") or []
        if shared:
            lines.append("两地同时占用 / 发散的凭据：")
            for item in shared:
                if item.get("type") == "dependency_cycle":
                    for credential in item.get("credentials", []):
                        lines.append(
                            "  凭据 {}  班组 {}".format(
                                credential.get("occurrence_id"),
                                ",".join(credential.get("teams") or []) or "未签",
                            )
                        )
                    lines.append("    {}".format(item.get("detail", "")))
                else:
                    lines.append(
                        "  {}  班组 {}  {}".format(
                            item.get("occurrence_id", ""),
                            ",".join(item.get("teams") or []) or "未签",
                            item.get("detail", item.get("type", "")),
                        )
                    )
        else:
            lines.append("冲突：")
            for item in plan.get("conflicts", []):
                lines.append(
                    "  {}  {}".format(
                        item.get("occurrence_id", item.get("occurrence_ids", "")),
                        item.get("detail", item.get("type", "")),
                    )
                )
                for credential in item.get("credentials", []):
                    lines.append(
                        "    凭据 {} 班组 {}".format(
                            credential.get("occurrence_id"),
                            ",".join(credential.get("teams") or []) or "未签",
                        )
                    )
        return "\n".join(lines).strip()

    lines.append("对账修补说明")
    lines.append("")
    lines.append("起始余额：")
    for row in plan.get("start", {}).get("balances", []):
        lines.append(
            "  {} / {}: {}".format(row["location"], row["package"], row["qty"])
        )
    lines.append("")
    lines.append("目标余额（正式盘点表）：")
    for row in plan.get("target", {}).get("balances", []):
        lines.append(
            "  {} / {}: {}".format(row["location"], row["package"], row["qty"])
        )
    lines.append("")

    teams = plan.get("teams") or {}
    if teams:
        lines.append("分段执行：")
        for team in sorted(teams):
            lines.append("  [{} 班组]".format(team))
            for action in teams[team]:
                lines.append("    " + _format_action(action))
            lines.append("")

    execution = plan.get("execution") or []
    if execution:
        lines.append("总重放顺序：")
        for index, action in enumerate(execution, 1):
            lines.append(
                "  {}. {} ({})".format(
                    index, action.get("action_id"), _action_team(action)
                )
            )
        lines.append("")

    segments = plan.get("segments") or []
    if len(segments) > 1 or (segments and not segments[0].get("complete")):
        lines.append("暂停 / 续跑点：")
        for segment in segments:
            resume = segment.get("resume") or {}
            lines.append(
                "  第 {} 段（{} 班组）：{} 个动作，{}，结束签名 {}".format(
                    segment.get("segment"),
                    segment.get("team", "?"),
                    len(segment.get("actions", [])),
                    "已完成" if segment.get("complete") else "未完",
                    (segment.get("end") or "")[:12],
                )
            )
            lines.append(
                "    续跑：--checkpoint {}".format(segment.get("segment"))
            )
            if resume:
                lines.append(
                    "    重读点签名 {}，已应用 {}".format(
                        resume.get("signature", "")[:12],
                        ", ".join(resume.get("applied", [])) or "无",
                    )
                )
        lines.append("")

    acceptance = plan.get("acceptance") or {}
    lines.append("验收清单：")
    lines.append(
        "  档案共 {} 条，其中领用/合包 {} 条；全部重放：{}".format(
            acceptance.get("archive_count", 0),
            acceptance.get("issue_merge_count", 0),
            "是" if acceptance.get("complete") else "否",
        )
    )
    for item in acceptance.get("journal", []):
        lines.append(
            "  {} {}  {}  {}".format(
                item["occurrence_id"],
                item["kind"],
                "已重放" if item["replayed"] else "未重放",
                item.get("detail", ""),
            )
        )
    for team, checklist in sorted((acceptance.get("checklists") or {}).items()):
        lines.append(
            "  [{} 班组验收] {} 条领用/合包记录，与档案一致：{}".format(
                team,
                len(checklist.get("entries", [])),
                "是" if checklist.get("matches_archive") else "否",
            )
        )
        for entry in checklist.get("entries", []):
            alias = entry.get("primary_occurrence_id")
            suffix = ""
            if alias and alias != entry["occurrence_id"]:
                suffix = "（同 {}）".format(alias)
            lines.append(
                "    {} {} {}{}{}".format(
                    entry["occurrence_id"],
                    entry["kind"],
                    "本队执行" if not entry.get("other_team") else "他队执行",
                    "" if entry.get("replayed") else "（未重放）",
                    suffix,
                )
            )
    lines.append("")
    if plan.get("conflicts"):
        lines.append("提示：")
        for item in plan["conflicts"]:
            lines.append(
                "  {}  {}".format(
                    item.get("occurrence_id", item.get("occurrence_ids", "")),
                    item.get("detail", item.get("type", "")),
                )
            )
    return "\n".join(lines).rstrip()


def _format_action(action: Mapping[str, Any]) -> str:
    bits = [
        "{} [{}]".format(action.get("occurrence_id"), action.get("kind")),
    ]
    if action.get("mode") == "legs":
        bits.append("部分记账 legs={}".format(action.get("legs")))
    for node in action.get("inputs", []):
        bits.append(
            "-{} {}/{}".format(
                node.get("qty"), node.get("location", ""), node.get("package")
            )
        )
    for node in action.get("outputs", []):
        bits.append(
            "+{} {}/{}".format(
                node.get("qty"), node.get("location", ""), node.get("package")
            )
        )
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Plain row-diff fallback
# ---------------------------------------------------------------------------


def has_split_merge(occurrences) -> bool:
    """True when the archive actually uses split/merge lineage."""
    rows = read_rows(occurrences) if occurrences is not None else []
    for row in rows:
        kind = _kind(
            _first(
                row,
                ("type", "kind", "action", "operation", "event", "类型", "业务类型", "动作"),
                "",
            )
        )
        if kind in ("split", "merge"):
            return True
    return False


def compare_plain(
    previous_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    *,
    key: Optional[str] = None,
) -> Dict[str, Any]:
    """Ordinary row diff, used when no split/merge lineage is involved."""
    from . import compare  # local import avoids a cycle

    def keyed(rows):
        if key:
            return {str(row[key]): dict(row) for row in rows}
        return {
            hashlib.sha1(
                json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf8")
            ).hexdigest(): dict(row)
            for row in rows
        }

    return compare(keyed(previous_rows), keyed(current_rows))


# ---------------------------------------------------------------------------
# Optional field rules: legal transfer pairs and location availability
# ---------------------------------------------------------------------------


def normalize_rules(rules: Any) -> Dict[str, Any]:
    """Normalize the optional rules document.

    Accepted shapes::

        [["A仓", "B仓"], ...]                       # legal transfer pairs
        {"transfers": [{"from": "A仓", "to": "B仓"}]}
        {"location_limits": {"A仓": 20, "B仓": 15}}
        {"transfers": [...], "location_limits": {...}}

    Transfer pairs are directional.  A transfer whose pair is absent is
    reported as illegal and is never replayed.
    """
    out: Dict[str, Any] = {"transfers": None, "location_limits": {}}
    if rules is None:
        return out
    if isinstance(rules, (list, tuple)):
        pairs = rules
    elif isinstance(rules, Mapping):
        pairs = (
            rules.get("transfers")
            or rules.get("legal_transfers")
            or rules.get("transfer_rules")
            or rules.get("调拨关系")
            or rules.get("合法调拨")
        )
        limits = (
            rules.get("location_limits")
            or rules.get("availability")
            or rules.get("available")
            or rules.get("库位可用数量")
            or rules.get("可用数量")
            or {}
        )
        if isinstance(limits, Mapping):
            out["location_limits"] = {
                _identity(key): _dec(value) for key, value in limits.items()
            }
        elif isinstance(limits, (list, tuple)):
            for item in limits:
                if isinstance(item, Mapping):
                    location = _identity(
                        _first(item, ("location", "loc", "库位", "仓库", "仓"))
                    )
                    qty = _first(item, ("qty", "quantity", "available", "数量", "可用数量"))
                    if location and qty is not None:
                        out["location_limits"][location] = _dec(qty)
    else:
        raise ValueError("rules must be a list or mapping")
    if pairs is not None:
        allowed: Set[Tuple[str, str]] = set()
        for item in pairs:
            if isinstance(item, Mapping):
                source = _identity(
                    _first(item, ("from", "source", "from_location", "调出", "调出库位", "原库位"))
                )
                target = _identity(
                    _first(item, ("to", "target", "to_location", "调入", "调入库位", "新库位"))
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                source, target = _identity(item[0]), _identity(item[1])
            elif isinstance(item, str) and "->" in item:
                source, target = [_identity(part) for part in item.split("->", 1)]
            elif isinstance(item, str) and "→" in item:
                source, target = [_identity(part) for part in item.split("→", 1)]
            else:
                continue
            if source and target:
                allowed.add((source, target))
        out["transfers"] = allowed
    return out


def _transfer_allowed(op: Mapping[str, Any], allowed: Set[Tuple[str, str]]) -> bool:
    if op["kind"] != "transfer":
        return True
    source = op.get("from_location") or (
        op["inputs"][0].get("location", "") if op.get("inputs") else ""
    )
    target = op.get("to_location") or (
        op["outputs"][0].get("location", "") if op.get("outputs") else ""
    )
    return (source, target) in allowed


def _check_location_limits(
    state: State,
    limits: Mapping[str, Decimal],
    context: Mapping[str, Any],
    baseline: Optional[Mapping[Tuple[str, str], Decimal]] = None,
) -> None:
    """Check per-location availability.

    If the official starting sheet already exceeds a declared availability,
    the repair may reduce that overage but must not make it worse.
    """
    if not limits:
        return
    totals: Dict[str, Decimal] = defaultdict(Decimal)
    base_totals: Dict[str, Decimal] = defaultdict(Decimal)
    for (location, package), qty in state.balances.items():
        totals[location] += qty
    for (location, package), qty in (baseline or {}).items():
        base_totals[location] += qty
    for location, limit in limits.items():
        allowed = limit
        if base_totals.get(location, Decimal(0)) > allowed:
            allowed = base_totals[location]
        if totals.get(location, Decimal(0)) > allowed + EPSILON:
            raise ReplayError(
                "location availability exceeded: {} holds {}, limit {}".format(
                    location, _money(totals[location]), _money(limit)
                ),
                {
                    **dict(context),
                    "location": location,
                    "available": _money(totals[location]),
                    "limit": _money(limit),
                    "kind": "availability",
                },
            )
