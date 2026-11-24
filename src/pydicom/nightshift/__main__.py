"""Command line entry for night-shift planning and Label Map delivery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .delivery import deliver_plan, preview_delivery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pydicom.nightshift",
        description="Plan Binary SEG to DICOM Label Map delivery.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="preview check arrangements only")
    plan.add_argument("manifest", type=Path)
    plan.add_argument("--quota", "--file-quota", type=int, default=None)
    plan.add_argument("--json", dest="json_path", type=Path, default=None)

    deliver = subparsers.add_parser("deliver", help="write planned DICOM Label Maps")
    deliver.add_argument("manifest", type=Path)
    deliver.add_argument("--output-dir", type=Path, default=None)
    deliver.add_argument("--quota", "--file-quota", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        result = preview_delivery(args.manifest, quota=args.quota)
        text = json.dumps(_public(result), ensure_ascii=False, indent=2)
        if args.json_path:
            args.json_path.write_text(text, encoding="utf-8")
        print(
            f"score={result['total_score']} checks={result['delivered_check_count']} "
            f"files={result['used_files']}/{result['quota']} "
            f"unallocated_artifacts={len(result['quota_unallocated_artifacts'])}"
        )
        print(text if args.json_path is None else f"wrote {args.json_path}")
        return 0

    result = deliver_plan(
        args.manifest,
        output_dir=args.output_dir,
        quota=args.quota,
    )
    print(
        f"delivered score={result['total_score']} "
        f"checks={result['delivered_check_count']} files={result['used_files']}; "
        f"index={result['index_path']}"
    )
    return 0


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public(item)
            for key, item in value.items()
            if key not in {"catalog", "dataset", "pixel_array", "metadata"}
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
