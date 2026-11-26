from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .model import TaskSpec
from .planner import plan_task
from .report import format_plan
from .segments import plan_compressed


def load_input(path: str | None) -> Dict[str, Any]:
    if path and path != "-":
        text = Path(path).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    return json.loads(text)


def solve(data: Dict[str, Any]) -> Dict[str, Any]:
    query = data.get("query_case_id", data.get("query_case"))
    if isinstance(query, str) and query.lstrip("-").isdigit():
        query = int(query)
    if data.get("segments") or data.get("booklets"):
        result = plan_compressed(data)
        manifest = result.get("manifest")
        if manifest is not None and query is not None:
            return manifest.locate_case(query)
        if manifest is not None:
            return result["summary"]
        return result

    result = plan_task(TaskSpec.from_dict(data))
    if query is not None and result.get("status") == "feasible":
        for command in result.get("commands", []):
            if query in command.get("case_ids", []):
                return {
                    "case_id": query,
                    "command_index": command.get("command_index"),
                    "command_case_ids": command.get("case_ids"),
                    "folder": command.get("folder"),
                    "cut_id": command.get("cut_id"),
                    "bytes_by_fixture": command.get("bytes_by_fixture"),
                    "starting_budget": command.get("budgets"),
                }
        raise KeyError(f"case id {query!r} was not found")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan dual-relay MULTIAPPEND case batches")
    parser.add_argument("input", nargs="?", help="JSON task file; stdin if omitted")
    parser.add_argument("-q", "--query-case-id", help="locate a case in a compressed manifest")
    parser.add_argument("-f", "--format", choices=("json", "text"), default="json")
    parser.add_argument("-o", "--output", help="write output to this path")
    args = parser.parse_args(argv)

    data = load_input(args.input)
    if args.query_case_id is not None:
        data["query_case_id"] = args.query_case_id
    result = solve(data)
    if args.format == "text" and isinstance(result, dict) and "commands" in result:
        text = format_plan(result)
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
