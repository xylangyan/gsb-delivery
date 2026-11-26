"""Convenience import surface for the case-batch planning task."""

from casebatch.cli import main, solve
from casebatch.model import Case, Fixture, Message, TaskSpec
from casebatch.planner import evaluate_group, plan_linear, plan_task
from casebatch.report import format_plan
from casebatch.segments import CaseSequence, CompiledManifest, plan_compressed
from casebatch.wire import build_command_wire, normalize_append_bytes


def locate_case(data, case_id):
    if isinstance(case_id, str) and case_id.lstrip("-").isdigit():
        case_id = int(case_id)
    if data.get("segments") or data.get("booklets"):
        result = plan_compressed(data)
        manifest = result.get("manifest") if isinstance(result, dict) else None
        if manifest is not None:
            return manifest.locate_case(case_id)
        plan = result
    else:
        plan = plan_task(TaskSpec.from_dict(data))

    for command in plan.get("commands", []):
        if case_id in command.get("case_ids", []):
            return {
                "case_id": case_id,
                "command_index": command.get("command_index"),
                "command_case_ids": command.get("case_ids"),
                "folder": command.get("folder"),
                "cut_id": command.get("cut_id"),
                "bytes_by_fixture": command.get("bytes_by_fixture"),
                "starting_budget": command.get("budgets"),
                "message_sources": command.get("message_sources"),
            }
    raise KeyError(f"case id {case_id!r} was not found in plan")


plan_manifest = plan_task
plan = plan_task

__all__ = [
    "Case",
    "CaseSequence",
    "CompiledManifest",
    "Fixture",
    "Message",
    "TaskSpec",
    "build_command_wire",
    "evaluate_group",
    "format_plan",
    "locate_case",
    "normalize_append_bytes",
    "plan",
    "plan_compressed",
    "plan_linear",
    "plan_manifest",
    "plan_task",
    "solve",
]


if __name__ == "__main__":
    raise SystemExit(main())
