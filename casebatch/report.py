from __future__ import annotations

from typing import Any, Dict, List


def display_width(text: str) -> int:
    width = 0
    for char in str(text):
        code = ord(char)
        width += 2 if (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE4F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        ) else 1
    return width


def pad_label(label: str, width: int = 10) -> str:
    padding = max(0, width - display_width(label))
    return label + " " * padding


def format_plan(result: Dict[str, Any], label_width: int = 10) -> str:
    lines: List[str] = []
    status = result.get("status", "unknown")
    lines.append(f"status={status} commands={result.get('command_count')}")

    if status == "infeasible":
        failure = result.get("failure") or {}
        group = failure.get("group") or {}
        lines.append("failure_group=" + ",".join(map(str, group.get("case_ids", []))))
        for origin in failure.get("budget_origins", []):
            label = pad_label(str(origin.get("fixture", "?")), label_width)
            lines.append(
                f"  budget {label} tier={origin.get('tier')} "
                f"burst={origin.get('burst_limit')} command={origin.get('command_limit')} "
                f"origin={origin.get('origin')}"
            )
        for budget in group.get("budgets", []):
            violation = budget.get("violation") or {}
            if violation:
                label = pad_label(str(budget.get("fixture", "?")), label_width)
                lines.append(
                    f"  overflow {label} boundary={violation.get('boundary')} "
                    f"limit={violation.get('limit')} sent={violation.get('sent_bytes')} "
                    f"message=#{violation.get('message_number')} "
                    f"source={violation.get('source')}"
                )
        return "\n".join(lines)

    for command in result.get("commands", []):
        lines.append(
            f"CMD {command.get('command_index'):>4} "
            f"cases=[{','.join(map(str, command.get('case_ids', [])))}] "
            f"folder={command.get('folder')} cut={command.get('cut_id')}"
        )
        for budget in command.get("budgets", []):
            label = pad_label(str(budget.get("fixture", "?")), label_width)
            violation = budget.get("violation") or {}
            flag = "OK " if budget.get("ok") else "ERR"
            lines.append(
                f"  [{flag}] {label} tier {budget.get('before_tier')}->{budget.get('after_tier')} "
                f"burst={budget.get('burst_limit')} command={budget.get('command_limit')} "
                f"bytes={budget.get('total_bytes')} boundary={violation.get('boundary') or '-'}"
            )
    verification = result.get("verification") or {}
    if verification:
        lines.append(
            f"receipts checked={verification.get('checked_messages')} ok={verification.get('ok')}"
        )
    return "\n".join(lines)
