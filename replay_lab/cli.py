from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from replay_lab.core import (
    PlanEvaluation,
    certify_minimal,
    evaluate_plan,
    job_ids,
    load_case,
    plan_cost,
    schedule_words,
)
from replay_lab.graph import explore_canonical_graph
from replay_lab.repro import write_repro_script


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE = REPO_ROOT / "replay_cases" / "supply_chain.json"


def _json_default(value: Any):
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _evaluation_summary(evaluation: PlanEvaluation) -> Dict[str, Any]:
    divergence = None
    if evaluation.divergence is not None:
        divergence = {
            key: value
            for key, value in asdict(evaluation.divergence).items()
            if key != "events"
        }
    return {
        "plan_id": evaluation.plan_id,
        "equivalent": evaluation.equivalent,
        "schedule_count": evaluation.schedule_count,
        "cost_bytes": evaluation.cost_bytes,
        "response_item_count": evaluation.response_item_count,
        "missing_required": evaluation.missing_required,
        "state_count": evaluation.state_count,
        "divergence": divergence,
        "branches": evaluation.branches,
    }


def _find_plan(case: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    try:
        return next(plan for plan in case["plans"] if plan["id"] == plan_id)
    except StopIteration as exc:
        raise SystemExit("unknown plan: %s" % plan_id) from exc


def _evaluate_with_graph(case: Dict[str, Any], plan: Dict[str, Any], *args, **kwargs) -> PlanEvaluation:
    evaluation = evaluate_plan(case, plan, *args, **kwargs)
    evaluation.state_count = explore_canonical_graph(case, plan)["state_count"]
    return evaluation


def command_list(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    print("case:", case.get("name", case.get("id", "unnamed")))
    print("jobs:", ", ".join(job_ids(case)))
    print("schedules:", len(schedule_words(case)))
    for plan in case["plans"]:
        cost, items = plan_cost(case, plan)
        print("plan %-20s cost=%-6d items=%d  %s" % (
            plan["id"], cost, items, plan.get("description", "")
        ))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    baseline_plan = _find_plan(case, case["baseline_plan_id"])
    baseline = _evaluate_with_graph(case, baseline_plan)
    if baseline.divergence:
        print("BASELINE-INVALID", baseline.divergence.message)
        return 2

    if args.plan:
        plan = _find_plan(case, args.plan)
        evaluation = _evaluate_with_graph(
            case,
            plan,
            baseline.job_signatures,
            baseline.branches,
        )
        results = [evaluation]
    else:
        results = []
        for candidate in case["plans"]:
            if candidate["id"] == baseline.plan_id:
                results.append(baseline)
                continue
            results.append(_evaluate_with_graph(
                case,
                candidate,
                baseline.job_signatures,
                baseline.branches,
            ))

    failed = False
    for evaluation in results:
        status = "PASS" if evaluation.equivalent else "FAIL"
        print("%s %s schedules=%d bytes=%d states=%s" % (
            status,
            evaluation.plan_id,
            evaluation.schedule_count,
            evaluation.cost_bytes,
            evaluation.state_count,
        ))
        if evaluation.divergence is not None:
            failed = True
            divergence = evaluation.divergence
            event = divergence.events[divergence.global_index - 1]
            print("  earliest: schedule=%s global=%d job=%s" % (
                divergence.schedule,
                divergence.global_index,
                divergence.job,
            ))
            print("  request : %s %s" % (event["method"], event["url"]))
            print("  consumed: %s" % json.dumps(event["consumed"], ensure_ascii=False))
            print("  expected: %s" % json.dumps(divergence.expected, ensure_ascii=False, sort_keys=True))
            print("  actual  : %s" % json.dumps(divergence.actual, ensure_ascii=False, sort_keys=True))
        if evaluation.missing_required:
            failed = True
            print("  missing required originals:", ", ".join(evaluation.missing_required))

    return 1 if failed else 0


def _write_report(
    output_dir: Path,
    case: Dict[str, Any],
    baseline: PlanEvaluation,
    selection: Dict[str, Any],
    results: List[PlanEvaluation],
    repro_paths: Dict[str, Path],
    minimality_checks: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    lines: List[str] = []
    lines.append("# 本地回放包选择结果")
    lines.append("")
    lines.append("案例：`%s`；合规交错：`%d` 种。" % (
        case.get("name", case.get("id", "supply-chain")),
        baseline.schedule_count,
    ))
    lines.append("")
    lines.append("**选择**")
    lines.append("")
    selected_id = selection["selected_plan_id"]
    if selected_id is None:
        lines.append("- 没有候选登记能在全部调度下保持基线行为。")
    else:
        selected = next(result for result in results if result.plan_id == selected_id)
        lines.append("- 已批准候选中的最小合规回放包：`%s`。" % selected_id)
        lines.append("- 登记原文字节：`%d`；回包项：`%d`；规范化状态：`%s`。" % (
            selected.cost_bytes,
            selected.response_item_count,
            selected.state_count,
        ))
        if minimality_checks is not None:
            all_necessary = all(check["necessary"] for check in minimality_checks)
            lines.append("- 逐项删除证书：`%s`；检查 `%d` 项，结果见 `replay_output/minimality_certificate.json`。" % (
                "全部不可删" if all_necessary else "存在可删项",
                len(minimality_checks),
            ))
    lines.append("")
    lines.append("| 登记包 | 合规 | 字节 | 回包项 | 状态数 | 最早分歧 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for result in results:
        divergence = ""
        if result.divergence is not None:
            divergence = "`%s#%d` %s" % (
                result.divergence.schedule,
                result.divergence.global_index,
                result.divergence.message,
            )
        lines.append("| `%s` | %s | %d | %d | %s | %s |" % (
            result.plan_id,
            "是" if result.equivalent else "否",
            result.cost_bytes,
            result.response_item_count,
            result.state_count,
            divergence or "—",
        ))

    invalid = [result for result in results if result.divergence is not None]
    if invalid:
        earliest = min(invalid, key=lambda item: item.divergence.global_index)
        divergence = earliest.divergence
        event = divergence.events[divergence.global_index - 1]
        prior = divergence.events[divergence.global_index - 2] if divergence.global_index >= 2 else None
        lines.append("")
        lines.append("**最早分歧**")
        lines.append("")
        lines.append("- 登记包：`%s`；调度：`%s`；全局事件：`%d`。" % (
            earliest.plan_id,
            divergence.schedule,
            divergence.global_index,
        ))
        lines.append("- 实际请求：`%s %s`。" % (event["method"], event["url"]))
        if prior is not None:
            lines.append("- 上一笔回包：原件 `%s`；许可下一页/补件字段：`%s`。" % (
                prior.get("original_id"),
                json.dumps(prior.get("json"), ensure_ascii=False, sort_keys=True),
            ))
        lines.append("- 消耗位置：`%s`。" % json.dumps(event.get("consumed"), ensure_ascii=False, sort_keys=True))
        lines.append("- 期望：`%s`。" % json.dumps(divergence.expected, ensure_ascii=False, sort_keys=True))
        lines.append("- 实际：`%s`。" % json.dumps(divergence.actual, ensure_ascii=False, sort_keys=True))
    for plan_id, path in repro_paths.items():
        try:
            display_path = path.relative_to(REPO_ROOT)
        except ValueError:
            display_path = path
        lines.append("- 原库重现脚本：`%s`。" % display_path)

    lines.append("")
    lines.append("**复跑**")
    lines.append("")
    lines.append("- 校验：`./.venv/bin/python local_regression.py verify`。")
    lines.append("- 重选：`./.venv/bin/python local_regression.py select`。")
    for plan_id, path in repro_paths.items():
        try:
            display_path = path.relative_to(REPO_ROOT)
        except ValueError:
            display_path = path
        lines.append("- 重现 `%s`：`./.venv/bin/python %s`。" % (plan_id, display_path))

    report_path = output_dir / "selection_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def command_select(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    baseline, selection, results = _evaluate_all_with_graphs(case)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    repro_paths: Dict[str, Path] = {}
    plans_by_id = {plan["id"]: plan for plan in case["plans"]}
    for result in results:
        if result.divergence is None:
            continue
        repro_path = output_dir / ("repro_%s.py" % result.plan_id.replace("-", "_"))
        write_repro_script(repro_path, case, plans_by_id[result.plan_id], result)
        repro_paths[result.plan_id] = repro_path

    selected = None
    minimality_checks: List[Dict[str, Any]] = []
    if selection["selected_plan_id"]:
        selected = _find_plan(case, selection["selected_plan_id"])
        minimality_checks = certify_minimal(
            case,
            selected,
            baseline.job_signatures,
            baseline.branches,
        )
        all_necessary = all(check["necessary"] for check in minimality_checks)
        _write_json(output_dir / "minimality_certificate.json", {
            "locally_minimal": all_necessary,
            "checks": minimality_checks,
        })
        _write_json(output_dir / "selected_package.json", {
            "selection": selection,
            "locally_minimal": all_necessary,
            "plan": selected,
        })
        minimal_case = {
            key: value
            for key, value in case.items()
            if not key.startswith("_")
        }
        minimal_case["baseline_plan_id"] = selected["id"]
        minimal_case["plans"] = [selected]
        used_originals = {
            item.get("original")
            for registration in selected.get("registrations", [])
            for item in registration.get("responses", [])
            if item.get("original")
        }
        minimal_case["originals"] = {
            key: value
            for key, value in minimal_case.get("originals", {}).items()
            if key in used_originals
        }
        _write_json(output_dir / "minimal_replay_case.json", minimal_case)

    _write_json(output_dir / "evaluation_results.json", {
        "selection": selection,
        "results": [_evaluation_summary(result) for result in results],
        "repro_scripts": {key: str(path) for key, path in repro_paths.items()},
    })
    report_path = _write_report(
        output_dir,
        case,
        baseline,
        selection,
        results,
        repro_paths,
        minimality_checks,
    )

    print(json.dumps(selection, ensure_ascii=False, sort_keys=True))
    print("report:", report_path)
    for plan_id, path in repro_paths.items():
        print("repro[%s]: %s" % (plan_id, path))
    return 0 if selection["selected_plan_id"] else 1


def _evaluate_all_with_graphs(case: Dict[str, Any]):
    from replay_lab.core import evaluate_all

    baseline, selection, results = evaluate_all(case)
    for result in results:
        plan = _find_plan(case, result.plan_id)
        result.state_count = explore_canonical_graph(case, plan)["state_count"]
    return baseline, selection, results


def command_repro(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    baseline_plan = _find_plan(case, case["baseline_plan_id"])
    baseline = evaluate_plan(case, baseline_plan)
    plan = _find_plan(case, args.plan)
    evaluation = evaluate_plan(
        case,
        plan,
        baseline.job_signatures,
        baseline.branches,
    )
    if evaluation.divergence is None:
        print("plan %s is equivalent; no divergence to reproduce" % args.plan)
        return 0
    path = write_repro_script(args.out, case, plan, evaluation)
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--case", default=argparse.SUPPRESS, help="Path to replay case JSON")

    parser = argparse.ArgumentParser(description="Select schedule-safe requests-mock replay registrations")
    parser.add_argument("--case", default=str(DEFAULT_CASE), help="Path to replay case JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list",
        parents=[common],
        help="List jobs and candidate plans",
    ).set_defaults(func=command_list)

    verify = subparsers.add_parser(
        "verify",
        parents=[common],
        help="Verify one or all candidate plans",
    )
    verify.add_argument("--plan")
    verify.set_defaults(func=command_verify)

    select = subparsers.add_parser(
        "select",
        parents=[common],
        help="Select the minimum equivalent package",
    )
    select.add_argument("--out", default=str(REPO_ROOT / "replay_output"))
    select.set_defaults(func=command_select)

    repro = subparsers.add_parser(
        "repro",
        parents=[common],
        help="Write an earliest-divergence reproduction script",
    )
    repro.add_argument("--plan", required=True)
    repro.add_argument("--out", default=str(REPO_ROOT / "replay_output" / "earliest_divergence.py"))
    repro.set_defaults(func=command_repro)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
