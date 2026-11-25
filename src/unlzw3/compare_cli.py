"""Command line entry point for index based .Z archive comparison."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .comparison import (
    ByteAnchor,
    CodeAnchor,
    InvalidArchive,
    RangeSpec,
    build_index,
    compare,
    load_index,
    make_selection,
    parse_range,
    save_index,
)


def _anchor_cli(text: str):
    if text.startswith("byte:"):
        return ByteAnchor(int(text[5:], 0))
    if text.startswith("code:"):
        parts = text[5:].split(":")
        if len(parts) == 1:
            return CodeAnchor(int(parts[0], 0))
        if len(parts) == 2:
            return CodeAnchor(int(parts[0], 0), int(parts[1], 0))
    raise argparse.ArgumentTypeError(
        f"anchor must look like byte:1234 or code:56:2, got {text!r}"
    )


def _build_specs(args, side: str):
    if args.ranges_json is not None:
        payload = json.loads(args.ranges_json.read_text())
        return [parse_range(item) for item in payload[side]]

    starts = getattr(args, f"{side}_start")
    ends = getattr(args, f"{side}_end") or []
    lengths = getattr(args, f"{side}_length") or []
    if not starts:
        raise SystemExit(f"{side}: at least one --{side}-start anchor is required")
    if len(ends) + len(lengths) != len(starts) or (ends and lengths):
        raise SystemExit(f"{side}: give exactly one of --{side}-end/--{side}-length per start")
    if ends:
        return [RangeSpec(start=start, end=end) for start, end in zip(starts, ends)]
    return [RangeSpec(start=start, length=length) for start, length in zip(starts, lengths)]


def _load(path: Path):
    with path.open("rb") as handle:
        signature = handle.read(8)
    if signature == b"LZW3IDX1":
        return load_index(path)
    try:
        return build_index(path.read_bytes(), source_name=str(path))
    except InvalidArchive as exc:
        raise SystemExit(f"invalid archive {path}: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m unlzw3",
        description="Compare retained fragments of two LZW .Z archives via per-code indexes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cmp_parser = sub.add_parser("compare", help="compare two archives or saved indexes")
    cmp_parser.add_argument("left", type=Path)
    cmp_parser.add_argument("right", type=Path)
    cmp_parser.add_argument("--left-start", type=_anchor_cli, action="append")
    cmp_parser.add_argument("--left-end", type=_anchor_cli, action="append")
    cmp_parser.add_argument("--left-length", type=int, action="append")
    cmp_parser.add_argument("--right-start", type=_anchor_cli, action="append")
    cmp_parser.add_argument("--right-end", type=_anchor_cli, action="append")
    cmp_parser.add_argument("--right-length", type=int, action="append")
    cmp_parser.add_argument(
        "--ranges-json",
        type=Path,
        help="JSON file with {'left': [range, ...], 'right': [range, ...]}",
    )

    idx_parser = sub.add_parser("index", help="build and save a per-code index")
    idx_parser.add_argument("archive", type=Path)
    idx_parser.add_argument("output", type=Path)

    args = parser.parse_args(argv)

    if args.command == "index":
        index = build_index(args.archive.read_bytes(), source_name=str(args.archive))
        save_index(index, args.output)
        print(
            f"indexed {index.n_codes} codes, {index.total_out} decompressed bytes, "
            f"{len(index.segment_start)} dictionary segment(s) -> {args.output}"
        )
        return 0

    left_specs = _build_specs(args, "left")
    right_specs = _build_specs(args, "right")

    left_index = _load(args.left)
    right_index = _load(args.right)
    result = compare(
        make_selection(left_index, left_specs),
        make_selection(right_index, right_specs),
    )
    print(str(result))
    return 0 if result.equal else 1


if __name__ == "__main__":
    sys.exit(main())
