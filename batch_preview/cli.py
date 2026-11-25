"""Command line interface for the batch preview.

Usage::

    python -m batch_preview spec.json -o outdir [--budget 10] [--releases 甲,乙]

Writes one Markdown file per publishable release plus ``report.json`` with
the position-keyed change list, blockers and warnings.  Exit code is 0 when
every release can be published, 1 when some release is blocked, and 2 when
the preview could not be computed in time for some release.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import load_spec_file
from .pipeline import run_batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batch_preview")
    parser.add_argument("spec", help="批次选段表 (JSON)")
    parser.add_argument("-o", "--out", default="preview_out", help="输出目录")
    parser.add_argument(
        "--budget", type=float, default=None, help="求解时间预算（秒）"
    )
    parser.add_argument(
        "--releases", default=None, help="只预览这些发行版（逗号分隔）"
    )
    args = parser.parse_args(argv)

    spec = load_spec_file(args.spec)
    if args.budget is not None:
        spec.time_budget = args.budget
    only = args.releases.split(",") if args.releases else None

    result = run_batch(spec, only_releases=only)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    report = dict(result.report)
    for name, rel in result.releases.items():
        path = outdir / f"{name}.md"
        path.write_text(rel.text, encoding="utf-8")
        report["releases"][name]["file"] = path.name
        report["releases"][name].pop("text", None)
    (outdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for name, info in report["releases"].items():
        status = info["status"]
        if status == "ok":
            print(f"[可发行] {name} -> {outdir / (name + '.md')}")
        elif status == "blocked":
            print(f"[不可发行] {name}")
        else:
            print(f"[结果未算出] {name}（预览尚未完成，并非确认不可发行）")
    for blocker in report["blockers"]:
        where = ""
        if "manual" in blocker:
            where = f"（{blocker['manual']}:{blocker['line']}:{blocker['col']}）"
        print(
            f"  卡住: {blocker['release']} 的标签 {blocker['label']!r} "
            f"被未获准改动的文字 {blocker['text']!r}{where} 占用"
        )
    for warning in report["warnings"]:
        print(f"  警告: {warning}")
    print(f"报告: {outdir / 'report.json'}")

    if result.status == "ok":
        return 0
    if result.status == "blocked":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
