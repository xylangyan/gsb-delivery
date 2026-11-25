#!/usr/bin/env python3
"""Shared original-row manifest planner.

The input bundle is intentionally JSON so a ledger export can be handed to this
tool without a bespoke ETL step.  The module has no third-party runtime
dependency except ``mo_sql_parsing``; SQLite is used as the replay oracle for
the restricted outer-join/filter/distinct/window-rank query class.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROW_ID_KEYS = ("row_id", "_row_id", "rid", "source_row_id")
DEFAULT_COST_KEYS = ("storage_cost", "row_cost", "cost")


class ManifestInputError(ValueError):
    pass


class InsufficientAllowedRows(RuntimeError):
    def __init__(self, diagnosis: dict[str, Any]):
        super().__init__(json.dumps(diagnosis, ensure_ascii=False, sort_keys=True))
        self.diagnosis = diagnosis


@dataclass(frozen=True, order=True)
class RowRef:
    table: str
    row_id: str

    def as_dict(self) -> dict[str, str]:
        return {"table": self.table, "row_id": self.row_id}

    def key(self) -> str:
        return f"{self.table}\u0000{self.row_id}"


@dataclass
class OriginalRow:
    ref: RowRef
    columns: dict[str, Any]
    cost: float = 1

    def as_manifest_row(self) -> dict[str, Any]:
        return {
            "source_table": self.ref.table,
            "row_id": self.ref.row_id,
            "storage_cost": self.cost,
            "columns": self.columns,
        }


@dataclass
class QueryCase:
    query_id: str
    sql: str
    expected: Any
    views: dict[str, str] = field(default_factory=dict)
    view: str | None = None
    required_refs: set[RowRef] = field(default_factory=set)
    order_sensitive: bool = True
    dialect: str = "common"


def _camel_to_snake(name: str) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return name.replace(" ", "_")


def _identifier(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "name" in value and isinstance(value["name"], str):
            return value["name"]
        if "value" in value and isinstance(value["value"], str):
            return value["value"]
    return None


def _walk(node: Any) -> Iterable[Any]:
    stack = [node]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            yield current
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            for item in current:
                if isinstance(item, (dict, list, tuple)):
                    stack.append(item)
                else:
                    yield item
        else:
            yield current


def parse_sql_requirements(sql: str, dialect: str = "common") -> dict[str, Any]:
    """Return tables, columns, predicates, joins, DISTINCT and window keys.

    The summary is deliberately conservative: it keeps the parser's structures
    for predicates/order specifications instead of trying to be a second SQL
    evaluator.
    """

    parse_fn = None
    if dialect in {"postgres", "postgresql", "common", "ansi"}:
        from mo_sql_parsing import parse as parse_fn  # noqa: PLC0415
    else:
        module = __import__("mo_sql_parsing", fromlist=[f"parse_{dialect}"])
        parse_fn = getattr(module, f"parse_{dialect}", None)
        if parse_fn is None:
            from mo_sql_parsing import parse as parse_fn  # noqa: PLC0415

    tree = parse_fn(sql)
    tables: dict[str, dict[str, str | None]] = {}
    join_conditions: list[Any] = []
    filters: list[Any] = []
    windows: list[dict[str, Any]] = []
    columns: set[str] = set()
    distinct = False

    def remember_table(name: str, alias: str | None = None) -> None:
        if name and not name.startswith("__"):
            tables.setdefault(name, {"name": name, "alias": alias})
            if alias:
                tables[name]["alias"] = alias

    def source_name(source: Any) -> tuple[str | None, str | None]:
        if isinstance(source, str):
            parts = source.split(".")
            return parts[-1], None
        if isinstance(source, dict):
            name = _identifier(source.get("value")) or _identifier(source.get("name"))
            alias = _identifier(source.get("name")) if source.get("value") is not None else None
            if isinstance(source.get("value"), dict):
                return None, alias
            if name:
                parts = name.split(".")
                name = parts[-1]
            return name, alias
        return None, None

    for node in _walk(tree):
        if not isinstance(node, dict):
            continue

        for key, value in list(node.items()):
            key_lower = key.lower()
            if key_lower in {"select_distinct", "distinct"}:
                distinct = True
            if key_lower == "from":
                sources = value if isinstance(value, list) else [value]
                for source in sources:
                    if isinstance(source, dict):
                        for join_key, join_value in source.items():
                            jk = join_key.lower()
                            if jk.endswith(" join") or jk in {"join", "left join", "right join", "full join"}:
                                name, alias = source_name(join_value)
                                if name:
                                    remember_table(name, alias)
                                if "on" in source:
                                    join_conditions.append({"type": join_key, "on": source["on"]})
                        name, alias = source_name(source)
                        if name and not any(k.lower().endswith(" join") for k in source):
                            remember_table(name, alias)
                    else:
                        name, alias = source_name(source)
                        if name:
                            remember_table(name, alias)
            if key_lower == "on":
                if not any(cond.get("on") == value for cond in join_conditions):
                    join_conditions.append({"on": value})
            if key_lower == "where":
                filters.append(value)
            if key_lower == "over" and isinstance(value, dict):
                windows.append(
                    {
                        "alias": node.get("name"),
                        "partition_by": value.get("partitionby") or value.get("partition_by"),
                        "order_by": value.get("orderby") or value.get("order_by"),
                        "frame": value.get("rows") or value.get("range"),
                    }
                )
            if isinstance(value, str) and "." in value:
                columns.add(value)

        select_items = node.get("select") or node.get("select_distinct")
        if isinstance(select_items, list):
            for item in select_items:
                if isinstance(item, dict):
                    value = item.get("value")
                    if isinstance(value, str):
                        columns.add(value)
                    elif isinstance(value, dict):
                        for leaf in _walk(value):
                            if isinstance(leaf, str):
                                columns.add(leaf)

    return {
        "tables": list(tables.values()),
        "columns": sorted(columns),
        "join_conditions": join_conditions,
        "filters": filters,
        "windows": windows,
        "distinct": distinct or "select_distinct" in tree,
        "parse_tree": tree,
    }


def _read_row_id(table: str, raw: dict[str, Any], id_keys: Iterable[str]) -> str:
    for key in id_keys:
        if key in raw and raw[key] is not None:
            return str(raw[key])
    raise ManifestInputError(
        f"table {table!r} has a row without stable id; add one of {tuple(id_keys)} "
        "or configure row_id_keys"
    )


def _read_cost(raw: dict[str, Any], default: float = 1) -> float:
    for key in DEFAULT_COST_KEYS:
        if key in raw and raw[key] is not None:
            return float(raw[key])
    return default


def normalize_bundle(bundle: dict[str, Any]) -> tuple[dict[RowRef, OriginalRow], list[QueryCase], set[RowRef], dict[str, list[str]]]:
    if not isinstance(bundle, dict):
        raise ManifestInputError("input bundle must be a JSON object")

    id_keys = tuple(bundle.get("row_id_keys") or DEFAULT_ROW_ID_KEYS)
    cost_default = float(bundle.get("default_cost", 1))
    rows: dict[RowRef, OriginalRow] = {}
    table_schemas: dict[str, list[str]] = {}

    raw_tables = bundle.get("tables")
    if raw_tables is None:
        raise ManifestInputError("bundle is missing 'tables'")
    if isinstance(raw_tables, dict):
        table_items = [{"name": name, **(spec if isinstance(spec, dict) else {"rows": spec})} for name, spec in raw_tables.items()]
    elif isinstance(raw_tables, list):
        table_items = raw_tables
    else:
        raise ManifestInputError("'tables' must be an object or list")

    cost_overrides: dict[str, float] = {}
    for item in bundle.get("costs", []) if isinstance(bundle.get("costs", []), list) else []:
        if isinstance(item, dict) and item.get("table") and item.get("row_id") is not None:
            cost_overrides[RowRef(str(item["table"]), str(item["row_id"])).key()] = float(item.get("cost", item.get("storage_cost", cost_default)))
    if isinstance(bundle.get("costs"), dict):
        for key, value in bundle["costs"].items():
            cost_overrides[key] = float(value)

    for table_spec in table_items:
        table = str(table_spec["name"])
        table_id_keys = tuple(table_spec.get("row_id_keys") or id_keys)
        table_cost_default = float(table_spec.get("default_cost", cost_default))
        raw_rows = table_spec.get("rows")
        if not isinstance(raw_rows, list):
            raise ManifestInputError(f"table {table!r} must contain a 'rows' list")
        declared_columns = list(table_spec.get("columns") or [])
        table_schemas[table] = [str(column) for column in declared_columns]
        for index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, dict):
                raise ManifestInputError(f"table {table!r} row {index} must be an object")
            row_id = _read_row_id(table, raw_row, table_id_keys)
            ref = RowRef(table, row_id)
            if ref in rows:
                raise ManifestInputError(f"duplicate source row identity {table}:{row_id}")
            if isinstance(raw_row.get("columns"), dict):
                columns = dict(raw_row["columns"])
            else:
                reserved = set(DEFAULT_ROW_ID_KEYS) | set(DEFAULT_COST_KEYS) | {"columns"}
                columns = {key: value for key, value in raw_row.items() if key not in reserved}
            for column in declared_columns:
                columns.setdefault(column, None)
            cost = cost_overrides.get(ref.key(), _read_cost(raw_row, table_cost_default))
            rows[ref] = OriginalRow(ref=ref, columns=columns, cost=cost)

    all_refs = set(rows)
    allowed: set[RowRef]
    allowed_raw = bundle.get("allowed_rows", "all")
    if allowed_raw in (None, "all", "*"):
        allowed = set(all_refs)
    else:
        allowed = set()
        if isinstance(allowed_raw, dict):
            entries = [{"table": table, "row_ids": ids} for table, ids in allowed_raw.items()]
        elif isinstance(allowed_raw, list):
            entries = allowed_raw
        else:
            raise ManifestInputError("'allowed_rows' must be 'all', an object, or a list")
        for entry in entries:
            if isinstance(entry, str) and "\u0000" in entry:
                table, row_id = entry.split("\u0000", 1)
                allowed.add(RowRef(table, row_id))
            elif isinstance(entry, dict) and entry.get("table"):
                table = str(entry["table"])
                if entry.get("row_id") is not None:
                    allowed.add(RowRef(table, str(entry["row_id"])))
                for row_id in entry.get("row_ids", []) or []:
                    allowed.add(RowRef(table, str(row_id)))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                allowed.add(RowRef(str(entry[0]), str(entry[1])))
    unknown_allowed = allowed - all_refs
    if unknown_allowed:
        first = sorted(unknown_allowed)[0]
        raise ManifestInputError(f"allowed row does not exist: {first.table}:{first.row_id}")

    queries: list[QueryCase] = []
    raw_queries = bundle.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ManifestInputError("bundle is missing a non-empty 'queries' list")
    global_views = bundle.get("views", {})
    if not isinstance(global_views, dict):
        raise ManifestInputError("'views' must be an object mapping view name to SQL")

    for index, raw_query in enumerate(raw_queries):
        if not isinstance(raw_query, dict):
            raise ManifestInputError(f"query {index} must be an object")
        sql = raw_query.get("sql")
        if not isinstance(sql, str) and raw_query.get("view"):
            sql = f'SELECT * FROM "{raw_query["view"]}"'
        if not isinstance(sql, str):
            raise ManifestInputError(f"query {index} is missing SQL")
        query_id = str(raw_query.get("id") or raw_query.get("query_id") or f"q{index + 1}")
        views = dict(global_views)
        query_views = raw_query.get("views") or {}
        if not isinstance(query_views, dict):
            raise ManifestInputError(f"query {query_id} views must be an object")
        views.update({str(k): str(v) for k, v in query_views.items()})
        required: set[RowRef] = set()
        for item in raw_query.get("required_rows", []) or []:
            if isinstance(item, dict):
                required.add(RowRef(str(item["table"]), str(item["row_id"])))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                required.add(RowRef(str(item[0]), str(item[1])))
        queries.append(
            QueryCase(
                query_id=query_id,
                sql=sql,
                expected=raw_query.get("expected", raw_query.get("signed_result", raw_query.get("signed_results"))),
                views=views,
                view=raw_query.get("view"),
                required_refs=required,
                order_sensitive=bool(raw_query.get("order_sensitive", True)),
                dialect=str(raw_query.get("dialect", bundle.get("dialect", "common"))),
            )
        )

    top_required = set()
    for item in bundle.get("required_rows", []) or []:
        if isinstance(item, dict):
            top_required.add(RowRef(str(item["table"]), str(item["row_id"])))
    for query in queries:
        query.required_refs.update(top_required)
    return rows, queries, allowed, table_schemas


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, sqlite3.Binary):
        return bytes(value).hex()
    return value


def _row_tuple(row: sqlite3.Row | tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_canonical_value(value) for value in tuple(row))


def normalize_expected(expected: Any, actual_columns: list[str]) -> tuple[list[str] | None, list[tuple[Any, ...]]]:
    if expected is None:
        raise ManifestInputError("query is missing signed expected result")

    expected_columns: list[str] | None = None
    raw_rows: Any = expected
    if isinstance(expected, dict):
        raw_rows = expected.get("rows", expected.get("data", expected.get("results")))
        cols = expected.get("columns")
        if isinstance(cols, list):
            expected_columns = [str(column) for column in cols]
        if raw_rows is None:
            raise ManifestInputError("signed result object must contain 'rows'")
    if not isinstance(raw_rows, list):
        raise ManifestInputError("signed result rows must be a list")

    normalized: list[tuple[Any, ...]] = []
    for raw_row in raw_rows:
        if isinstance(raw_row, dict):
            normalized.append(tuple(raw_row.get(column) for column in actual_columns))
        elif isinstance(raw_row, (list, tuple)):
            normalized.append(tuple(raw_row))
        else:
            normalized.append((raw_row,))
    return expected_columns, normalized


class ReplayEngine:
    def __init__(
        self,
        rows: dict[RowRef, OriginalRow],
        queries: list[QueryCase],
        table_schemas: dict[str, list[str]] | None = None,
    ):
        self.rows = rows
        self.queries = queries
        self.table_schemas = table_schemas or {}
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.table_index: dict[str, str] = {}
        self.columns_by_table: dict[str, list[str]] = {}
        self.current_selection: set[RowRef] = set()
        self.requirements = [safe_requirements(query.sql, query.dialect) for query in queries]
        self._create_raw_tables()
        self._create_key_indexes()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ReplayEngine":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _create_raw_tables(self) -> None:
        grouped: dict[str, list[OriginalRow]] = {name: [] for name in self.table_schemas}
        for row in self.rows.values():
            grouped.setdefault(row.ref.table, []).append(row)
        cursor = self.connection.cursor()
        for index, (table, table_rows) in enumerate(sorted(grouped.items())):
            internal = f"__raw_{index}"
            self.table_index[table] = internal
            columns: list[str] = list(self.table_schemas.get(table, []))
            for row in table_rows:
                for column in row.columns:
                    if column not in columns:
                        columns.append(column)
            self.columns_by_table[table] = columns
            column_sql = ", ".join([
                '"__gsb_row_id" TEXT PRIMARY KEY',
                '"__gsb_selected" INTEGER NOT NULL DEFAULT 0',
                *[_quote_ident(column) for column in columns],
            ])
            cursor.execute(f"CREATE TABLE {internal} ({column_sql})")
            insert_sql = (
                f"INSERT INTO {internal} ("
                + ", ".join(['"__gsb_row_id"', '"__gsb_selected"', *[_quote_ident(c) for c in columns]])
                + ") VALUES (" + ", ".join("?" for _ in range(len(columns) + 2)) + ")"
            )
            cursor.executemany(
                insert_sql,
                [(row.ref.row_id, 0, *(row.columns.get(column) for column in columns)) for row in table_rows],
            )
        self.connection.commit()

    def _create_key_indexes(self) -> None:
        referenced: dict[str, set[str]] = {}
        for requirement in self.requirements:
            aliases: dict[str, str] = {}
            for table in requirement["tables"]:
                name = table.get("name")
                alias = table.get("alias")
                if name:
                    referenced.setdefault(name, set())
                    if alias:
                        aliases[alias] = name
            for column in requirement["columns"]:
                if "." in column:
                    qualifier, leaf = column.split(".", 1)
                    table = aliases.get(qualifier, qualifier)
                    if table in self.table_index:
                        referenced.setdefault(table, set()).add(leaf)
        cursor = self.connection.cursor()
        for table, columns in referenced.items():
            internal = self.table_index.get(table)
            if not internal:
                continue
            valid = [column for column in columns if column in self.columns_by_table.get(table, [])]
            for column in valid:
                index_name = f"__idx_{abs(hash((internal, column))) & 0x7fffffff}"
                try:
                    cursor.execute(
                        f"CREATE INDEX {_quote_ident(index_name)} ON {internal} ({_quote_ident(column)})"
                    )
                except sqlite3.Error:
                    pass
        self.connection.commit()

    def set_selection(self, refs: set[RowRef]) -> None:
        refs = set(refs)
        removed = self.current_selection - refs
        added = refs - self.current_selection
        cursor = self.connection.cursor()
        by_table: dict[str, tuple[list[str], list[str]]] = {}
        for ref in removed:
            by_table.setdefault(ref.table, ([], []))[0].append(ref.row_id)
        for ref in added:
            by_table.setdefault(ref.table, ([], []))[1].append(ref.row_id)
        for table, (remove_ids, add_ids) in by_table.items():
            internal = self.table_index.get(table)
            if not internal:
                continue
            if remove_ids:
                cursor.executemany(
                    f"UPDATE {internal} SET \"__gsb_selected\"=0 WHERE \"__gsb_row_id\"=?",
                    [(row_id,) for row_id in remove_ids],
                )
            if add_ids:
                cursor.executemany(
                    f"UPDATE {internal} SET \"__gsb_selected\"=1 WHERE \"__gsb_row_id\"=?",
                    [(row_id,) for row_id in add_ids],
                )
        self.connection.commit()
        self.current_selection = refs
        self._recreate_table_views()

    def _recreate_table_views(self) -> None:
        cursor = self.connection.cursor()
        existing = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type IN ('view','table')")]
        for name in existing:
            if name.startswith("__user_view_"):
                cursor.execute(f"DROP VIEW IF EXISTS {_quote_ident(name)}")
        for table, internal in self.table_index.items():
            columns = self.columns_by_table[table]
            select_columns = ", ".join(_quote_ident(column) for column in columns)
            cursor.execute(f"DROP VIEW IF EXISTS {_quote_ident(table)}")
            cursor.execute(
                f"CREATE TEMP VIEW {_quote_ident(table)} AS "
                f"SELECT {select_columns} FROM {internal} WHERE \"__gsb_selected\"=1"
            )
        self.connection.commit()

    def _install_query_views(self, query: QueryCase) -> None:
        cursor = self.connection.cursor()
        for name, sql in query.views.items():
            cursor.execute(f"DROP VIEW IF EXISTS {_quote_ident(name)}")
            cursor.execute(f"CREATE TEMP VIEW {_quote_ident(name)} AS {sql}")

    def run_query(self, query: QueryCase) -> dict[str, Any]:
        self._install_query_views(query)
        cursor = self.connection.cursor()
        try:
            cursor.execute(query.sql)
            actual_columns = [description[0] for description in cursor.description or []]
            actual_rows = [_row_tuple(row) for row in cursor.fetchall()]
            return {"columns": actual_columns, "rows": actual_rows, "error": None}
        except sqlite3.Error as exc:
            return {"columns": [], "rows": [], "error": f"{type(exc).__name__}: {exc}"}

    def verify(self, refs: set[RowRef]) -> list[dict[str, Any]]:
        self.set_selection(refs)
        failures: list[dict[str, Any]] = []
        for query_index, query in enumerate(self.queries):
            result = self.run_query(query)
            requirement = self.requirements[query_index]
            if result["error"]:
                failures.append(
                    {
                        "query_index": query_index,
                        "query_id": query.query_id,
                        "kind": "sql_error",
                        "error": result["error"],
                        "join_conditions": requirement.get("join_conditions", []),
                        "windows": requirement.get("windows", []),
                    }
                )
                break
            try:
                expected_columns, expected_rows = normalize_expected(query.expected, result["columns"])
            except ManifestInputError as exc:
                failures.append(
                    {"query_index": query_index, "query_id": query.query_id, "kind": "bad_expectation", "error": str(exc)}
                )
                break

            column_mismatch = (
                expected_columns is not None
                and [column.lower() for column in expected_columns] != [column.lower() for column in result["columns"]]
            )
            mismatch_index = None
            if not column_mismatch:
                limit = max(len(expected_rows), len(result["rows"]))
                for index in range(limit):
                    expected_row = expected_rows[index] if index < len(expected_rows) else None
                    actual_row = result["rows"][index] if index < len(result["rows"]) else None
                    width = max(len(expected_row or ()), len(actual_row or ()))
                    expected_padded = tuple((expected_row or ()) + (None,) * (width - len(expected_row or ())))
                    actual_padded = tuple((actual_row or ()) + (None,) * (width - len(actual_row or ())))
                    if not query.order_sensitive:
                        if index == 0:
                            expected_compare = sorted(expected_rows, key=lambda value: json.dumps(value, ensure_ascii=False, default=str))
                            actual_compare = sorted(result["rows"], key=lambda value: json.dumps(value, ensure_ascii=False, default=str))
                            if expected_compare != actual_compare:
                                mismatch_index = next(
                                    (i for i in range(max(len(expected_compare), len(actual_compare))) if (expected_compare[i] if i < len(expected_compare) else None) != (actual_compare[i] if i < len(actual_compare) else None)),
                                    0,
                                )
                        if mismatch_index is not None:
                            break
                    elif expected_padded != actual_padded:
                        mismatch_index = index
                        break
            if column_mismatch or mismatch_index is not None or len(expected_rows) != len(result["rows"]):
                kind = _classify_mismatch(
                    requirement,
                    result["columns"],
                    expected_rows,
                    result["rows"],
                    mismatch_index,
                )
                failures.append(
                    {
                        "query_index": query_index,
                        "query_id": query.query_id,
                        "kind": kind,
                        "row_index": mismatch_index,
                        "expected_columns": expected_columns,
                        "actual_columns": result["columns"],
                        "expected_row": expected_rows[mismatch_index] if mismatch_index is not None and mismatch_index < len(expected_rows) else None,
                        "actual_row": result["rows"][mismatch_index] if mismatch_index is not None and mismatch_index < len(result["rows"]) else None,
                        "expected_count": len(expected_rows),
                        "actual_count": len(result["rows"]),
                        "join_conditions": requirement.get("join_conditions", []),
                        "windows": requirement.get("windows", []),
                        "order_by": [window.get("order_by") for window in requirement.get("windows", [])],
                    }
                )
                break
        return failures


def _classify_mismatch(
    requirement: dict[str, Any],
    columns: list[str],
    expected_rows: list[tuple[Any, ...]],
    actual_rows: list[tuple[Any, ...]],
    index: int | None,
) -> str:
    windows = requirement.get("windows") or []
    joins = requirement.get("join_conditions") or []
    window_aliases = {str(window.get("alias")).lower() for window in windows if window.get("alias")}
    window_column_mismatch = False
    ordinary_column_mismatch = False

    if index is not None and index < len(expected_rows) and index < len(actual_rows):
        expected_row = expected_rows[index]
        actual_row = actual_rows[index]
        for column_index, column in enumerate(columns):
            expected_value = expected_row[column_index] if column_index < len(expected_row) else None
            actual_value = actual_row[column_index] if column_index < len(actual_row) else None
            if expected_value != actual_value:
                if column.lower() in window_aliases:
                    window_column_mismatch = True
                else:
                    ordinary_column_mismatch = True

    count_mismatch = len(expected_rows) != len(actual_rows)
    if joins and (ordinary_column_mismatch or count_mismatch):
        return "join_condition"
    if windows and (window_column_mismatch or not ordinary_column_mismatch):
        return "window"
    if joins:
        return "join_condition"
    return "result_mismatch"


def safe_requirements(sql: str, dialect: str) -> dict[str, Any]:
    try:
        return parse_sql_requirements(sql, dialect)
    except Exception as exc:
        return {"tables": [], "columns": [], "join_conditions": [], "filters": [], "windows": [], "distinct": False, "parse_error": str(exc)}


def solve_hitting_set(clauses: list[frozenset[int]], costs: list[float]) -> set[int] | None:
    clause_sets = [set(clause) for clause in clauses if clause]
    if any(len(clause) == 0 for clause in clauses):
        return None
    n = len(costs)

    def reduce_units(included: set[int], remaining: list[set[int]]) -> tuple[set[int], list[set[int]]] | None:
        included = set(included)
        changed = True
        while changed:
            changed = False
            next_remaining: list[set[int]] = []
            for clause in remaining:
                if clause & included:
                    continue
                clause = set(clause)
                if not clause:
                    return None
                if len(clause) == 1:
                    variable = next(iter(clause))
                    if variable not in included:
                        included.add(variable)
                        changed = True
                    continue
                next_remaining.append(clause)
            remaining = next_remaining
        return included, remaining

    best: tuple[float, frozenset[int]] | None = None

    def cost_of(included: set[int]) -> float:
        return sum(costs[i] for i in included)

    def search(included: set[int], remaining: list[set[int]]) -> None:
        nonlocal best
        reduced = reduce_units(included, remaining)
        if reduced is None:
            return
        included, remaining = reduced
        current_cost = cost_of(included)
        if best is not None and current_cost >= best[0]:
            return
        if not remaining:
            best = (current_cost, frozenset(included))
            return

        clause = min(remaining, key=lambda item: (len(item), min(costs[i] for i in item)))
        choices = sorted(clause, key=lambda i: (costs[i], -len([c for c in remaining if i in c])))
        for variable in choices:
            next_remaining = [item for item in remaining if variable not in item]
            search(included | {variable}, next_remaining)

    search(set(), clause_sets)
    return set(best[1]) if best else None


def minimize_shared_manifest(
    rows: dict[RowRef, OriginalRow],
    queries: list[QueryCase],
    allowed: set[RowRef],
    table_schemas: dict[str, list[str]] | None = None,
) -> tuple[set[RowRef], list[dict[str, Any]], dict[str, Any]]:
    for query in queries:
        missing = query.required_refs - allowed
        if missing:
            ref = sorted(missing)[0]
            diagnosis = {
                "query_id": query.query_id,
                "kind": "required_attribution_row_not_allowed",
                "table": ref.table,
                "row_id": ref.row_id,
            }
            raise InsufficientAllowedRows(diagnosis)

    refs = sorted(allowed)
    index_by_ref = {ref: i for i, ref in enumerate(refs)}
    costs = [rows[ref].cost for ref in refs]

    with ReplayEngine(rows, queries, table_schemas) as engine:
        initial_failures = engine.verify(allowed)
        if initial_failures:
            diagnosis = {
                "kind": "allowed_rows_do_not_replay_signed_results",
                "failure": initial_failures[0],
                "allowed_row_count": len(allowed),
            }
            raise InsufficientAllowedRows(diagnosis)

        current = set(allowed)
        clauses: list[frozenset[int]] = []
        verification_count = 1
        mandatory: set[RowRef] = set()

        for ref in refs:
            failures = engine.verify(allowed - {ref})
            verification_count += 1
            if failures:
                mandatory.add(ref)
                clauses.append(frozenset({index_by_ref[ref]}))

        ordered_for_removal = sorted(
            (ref for ref in refs if ref not in mandatory),
            key=lambda ref: (-rows[ref].cost, ref.table, ref.row_id),
        )
        for ref in ordered_for_removal:
            candidate = current - {ref}
            failures = engine.verify(candidate)
            verification_count += 1
            if failures:
                clause = frozenset(index_by_ref[missing_ref] for missing_ref in allowed - candidate)
                if clause:
                    clauses.append(clause)
            else:
                current = candidate

        best = set(current)
        best_cost = sum(rows[ref].cost for ref in best)

        cegar_iterations = 0
        while True:
            cegar_iterations += 1
            candidate_indexes = solve_hitting_set(clauses, costs)
            if candidate_indexes is None:
                break
            lower_bound_cost = sum(costs[i] for i in candidate_indexes)
            if lower_bound_cost + 1e-9 >= best_cost:
                break
            candidate = {refs[i] for i in candidate_indexes}
            failures = engine.verify(candidate)
            verification_count += 1
            if failures:
                clauses.append(frozenset(index_by_ref[ref] for ref in allowed - candidate))
                continue
            candidate_cost = sum(rows[ref].cost for ref in candidate)
            if candidate_cost < best_cost - 1e-9:
                best = candidate
                best_cost = candidate_cost

        final_failures = engine.verify(best)
        verification_count += 1
        if final_failures:
            raise InsufficientAllowedRows(
                {"kind": "internal_minimization_error", "failure": final_failures[0]}
            )

    stats = {
        "candidate_row_count": len(refs),
        "selected_row_count": len(best),
        "total_cost": best_cost,
        "verification_runs": verification_count,
        "mandatory_row_count": len(mandatory),
        "cegar_iterations": cegar_iterations,
        "learned_clause_count": len(clauses),
    }
    requirements = []
    for query, parsed in zip(queries, engine.requirements):
        summary = dict(parsed)
        summary.pop("parse_tree", None)
        requirements.append({"query_id": query.query_id, **summary})
    return best, requirements, stats


def build_manifest(bundle: dict[str, Any]) -> dict[str, Any]:
    rows, queries, allowed, table_schemas = normalize_bundle(bundle)
    selected, requirements, stats = minimize_shared_manifest(rows, queries, allowed, table_schemas)
    manifest_rows = [rows[ref].as_manifest_row() for ref in sorted(selected)]
    return {
        "schema": "gsb.shared_original_rows.v1",
        "shared": True,
        "row_count": len(manifest_rows),
        "total_cost": stats["total_cost"],
        "stats": stats,
        "query_requirements": requirements,
        "rows": manifest_rows,
    }


def _load_bundle(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        return {"tables": records}
    return json.loads(text)


def _write_output(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if path:
        path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="build and validate the minimum shared manifest")
    solve.add_argument("--input", type=Path, help="JSON bundle containing rows, SQL, views and signed results")
    solve.add_argument("--output", type=Path, help="write manifest JSON here instead of stdout")

    inspect = sub.add_parser("inspect", help="parse SQL and print input dependencies")
    inspect.add_argument("--sql", required=True)
    inspect.add_argument("--dialect", default="common")

    args = parser.parse_args(argv)
    if args.command == "inspect":
        summary = parse_sql_requirements(args.sql, args.dialect)
        summary.pop("parse_tree", None)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    input_path = args.input
    if input_path is None:
        for candidate in (Path("manifest_input.json"), Path("task.json"), Path("input.json"), Path("data/manifest_input.json")):
            if candidate.exists():
                input_path = candidate
                break
    if input_path is None or not input_path.exists():
        print(
            "缺少实际账包：请用 --input 提供包含 tables、queries[].sql/expected、"
            "allowed_rows 和 costs 的 JSON。",
            file=sys.stderr,
        )
        return 2

    try:
        bundle = _load_bundle(input_path)
        manifest = build_manifest(bundle)
    except InsufficientAllowedRows as exc:
        payload = {"ok": False, "diagnosis": exc.diagnosis}
        _write_output(args.output, payload)
        print(json.dumps(exc.diagnosis, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    except (ManifestInputError, json.JSONDecodeError) as exc:
        print(f"输入无效: {exc}", file=sys.stderr)
        return 2

    _write_output(args.output, manifest)
    print(f"已生成共用清单：{manifest['row_count']} 行，总成本 {manifest['total_cost']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
