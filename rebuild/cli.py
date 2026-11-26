"""Command line entry for the rebuild engine.

Loads a scenario JSON and answers pair-range queries, reusing a persisted
session so repeated narrowing queries build on the first run's material.

Scenario JSON schema (see rebuild/example_scenario.json):

{
  "seed": 20261127,
  "locales": {"en_US": 5, "fr_FR": 3, "ja_JP": 2},
  "cells": [
    {"name": "A", "lo": 1, "hi": 4, "step": 1},
    {"name": "B", "lo": 0, "hi": 6, "step": 2}
  ],
  "registry": [[1, 0], [1, 2]],
  "prefetch": {"1": ["early_optional"]},
  "rows": [
    {"name": "lead_int", "method": "random_int", "args": [0, 3]},
    {"name": "gated_optional", "method": "random_int", "args": [0, 99],
     "optional": true,
     "prob_rule": {"kind": "gated", "table": {"0": 0.29, "1": 0.07}, "default": 0.5}}
  ],
  "tail": [
    {"method": "random_int", "kwargs": {"min": 1, "max": 6}, "unique": true}
  ]
}

Cell references inside args/kwargs use the string "cell:A".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rebuild.engine import Cell, CellRef, ProbRule, QuerySession, Row, Scenario, TailStep, MATCH


def _convert(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("cell:"):
        return CellRef(value.split(":", 1)[1])
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v) for v in value]
    return value


def load_scenario(path: str | Path) -> Scenario:
    data = json.loads(Path(path).read_text())
    cells = [Cell(**c) for c in data["cells"]]
    rows = []
    for raw in data["rows"]:
        raw = dict(raw)
        if "prob_rule" in raw and raw["prob_rule"]:
            rule = raw["prob_rule"]
            table = rule.get("table")
            if table:
                table = {int(k): float(v) for k, v in table.items()}
            raw["prob_rule"] = ProbRule(
                kind=rule["kind"],
                prob=_convert(rule.get("prob")),
                table=table,
                default=rule.get("default"),
            )
        raw["args"] = tuple(_convert(a) for a in raw.get("args", ()))
        raw["kwargs"] = {k: _convert(v) for k, v in raw.get("kwargs", {}).items()}
        if "expect" not in raw:
            raw["expect"] = MATCH
        rows.append(Row(**raw))
    tail = []
    for raw in data.get("tail", []):
        raw = dict(raw)
        raw["args"] = tuple(_convert(a) for a in raw.get("args", ()))
        raw["kwargs"] = {k: _convert(v) for k, v in raw.get("kwargs", {}).items()}
        raw.setdefault("expect", MATCH)
        tail.append(TailStep(**raw))
    registry = {tuple(pair) for pair in data.get("registry", [])}
    return Scenario(
        seed=data["seed"],
        locales=data["locales"],
        cells=cells,
        rows=rows,
        tail=tail,
        registry=registry,
        prefetch={int(k): list(v) for k, v in data.get("prefetch", {}).items()},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-cell Faker rebuild query")
    parser.add_argument("scenario", help="path to scenario JSON")
    parser.add_argument("--session", default=None, help="persisted session pickle")
    parser.add_argument("--range-a", default=None, help="lo:hi override for cell A")
    parser.add_argument("--range-b", default=None, help="lo:hi override for cell B")
    args = parser.parse_args()

    session_path = Path(args.session) if args.session else None
    if session_path and session_path.exists():
        session = QuerySession.load(session_path)
    else:
        session = QuerySession(load_scenario(args.scenario))

    def parse_range(text: str | None):
        if not text:
            return None
        lo, hi = text.split(":")
        return int(lo), int(hi)

    result = session.query(parse_range(args.range_a), parse_range(args.range_b))
    print(json.dumps(
        {
            "count": result["count"],
            "pairs": result["pairs"],
            "earliest": _serializable(result["earliest"]),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))
    if session_path:
        session.save(session_path)


def _serializable(outcome: dict[str, Any] | None) -> dict[str, Any] | None:
    if not outcome:
        return None
    return {
        "a": outcome["a"],
        "b": outcome["b"],
        "history": outcome.get("history"),
        "tail": outcome.get("tail"),
        "audit": [
            {
                "row": e.row,
                "formula": e.formula,
                "prob_literal": e.prob_literal,
                "effective_percent": e.effective_percent,
                "unique_attempts": e.unique_attempts,
                "result": e.result,
            }
            for e in outcome.get("audit", [])
        ],
    }


if __name__ == "__main__":
    main()
