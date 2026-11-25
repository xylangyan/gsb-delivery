#!/usr/bin/env python3
"""Command line entry point for archive-aware sample queries."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from pygcode.sample_audit import (  # noqa: E402
    SampleArchiveError,
    audit,
    count_window,
    first_at,
    load_archive_file,
    verify_short_archive,
)

COMMANDS = ("audit", "count", "first", "verify-short")
DEFAULT_ARCHIVE_NAMES = (
    "archive.json",
    "sample_archive.json",
    "inspection_archive.json",
    "quality_archive.json",
)


def resolve_archive(path: str | None) -> str:
    if path:
        return path
    for name in DEFAULT_ARCHIVE_NAMES:
        if os.path.exists(name):
            return name
    raise SampleArchiveError(
        "archive not found; pass its JSON path or create one of: "
        + ", ".join(DEFAULT_ARCHIVE_NAMES)
    )


def write_output(payload, output_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)


def split_command(argv: list[str]) -> tuple[str, list[str]]:
    if argv and argv[0] in COMMANDS:
        return argv[0], argv[1:]
    if len(argv) >= 2 and argv[1] in COMMANDS:
        return argv[1], [argv[0]] + argv[2:]
    return "audit", argv


def build_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sample_audit.py %s" % command)
    parser.add_argument("archive", nargs="?", help="archive JSON path")
    parser.add_argument("--archive", dest="archive_option", help="archive JSON path")
    parser.add_argument("--output", help="write JSON result here")
    parser.add_argument("--verify", action="store_true",
                        help="also run every expanded line through pygcode.Machine")

    if command in ("audit", "count"):
        parser.add_argument("--window", nargs=4, type=int,
                            metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    if command in ("audit", "first"):
        parser.add_argument("--position", nargs=2, type=int, metavar=("X", "Y"))
        parser.add_argument("--work-position", nargs=2, type=int, metavar=("X", "Y"))
        parser.add_argument("--cs", default="G54",
                            help="coordinate system for --work-position")
    if command == "audit":
        parser.add_argument("--points", action="store_true",
                            help="include sorted window points")
    return parser


def main(argv: list[str] | None = None) -> int:
    original_argv = list(sys.argv[1:] if argv is None else argv)
    command, parsed_argv = split_command(original_argv)
    args = build_parser(command).parse_args(parsed_argv)

    try:
        archive_path = resolve_archive(args.archive_option or args.archive)

        if command == "audit":
            result = audit(
                archive_path,
                window=args.window,
                position=args.position,
                work_position=args.work_position,
                coordinate_system=args.cs,
                verify=args.verify,
                include_points=args.points,
            )
            result["archive"] = archive_path
            write_output(result, args.output)

        elif command == "count":
            if not args.window:
                raise SampleArchiveError("count requires --window XMIN YMIN XMAX YMAX")
            archive = load_archive_file(archive_path)
            result = {
                "archive": archive_path,
                "window": tuple(args.window),
                "window_distinct_plane_positions": count_window(
                    archive, args.window, verify=args.verify
                ),
            }
            write_output(result, args.output)

        elif command == "first":
            archive = load_archive_file(archive_path)
            result = first_at(
                archive,
                position=args.position,
                work_position=args.work_position,
                coordinate_system=args.cs,
                verify=args.verify,
            )
            if result is None:
                result = {"found": False}
            else:
                result["found"] = True
            result["archive"] = archive_path
            write_output(result, args.output)

        elif command == "verify-short":
            result = verify_short_archive(archive_path)
            result["archive"] = archive_path
            write_output(result, args.output)

        return 0
    except SampleArchiveError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
