"""
Command line interface for read-only forensic recovery.

Usage::

    python -m jsonlines recover STREAM --receipt RECEIPT.json \
        [--base-offset N] [--limit N] [--node-budget N] \
        [--trial-budget N] [--json]
"""

import argparse
import json
import sys
from typing import Optional, Sequence

from .recovery import (
    Alternative,
    ByteRange,
    ConfirmedRecord,
    RecoveryResult,
    UncertainRegion,
    receipt_from_dict,
    recover_file,
)


def _format_range(byte_range: ByteRange) -> str:
    return f"[{byte_range.start}, {byte_range.end})"


def _print_record(record: ConfirmedRecord) -> None:
    print(
        f"  confirmed {record.source}#{record.sequence_index}"
        f" value={json.dumps(record.value, ensure_ascii=False)}"
    )
    print(f"    body:      {_format_range(record.body)}")
    print(f"    line feed: {_format_range(record.line_feed)}")


def _print_alternative(alternative: Alternative, indent: str) -> None:
    line_feed = (
        "unpaired"
        if alternative.line_feed is None
        else _format_range(alternative.line_feed)
    )
    print(
        f"{indent}{alternative.kind} {alternative.source}"
        f"#{alternative.sequence_index} value="
        f"{json.dumps(alternative.value, ensure_ascii=False)}"
    )
    print(f"{indent}  body:      {_format_range(alternative.body)}")
    print(f"{indent}  line feed: {line_feed}")


def _print_region(region: UncertainRegion) -> None:
    print(f"  uncertain {_format_range(region.byte_range)}: {region.reason}")
    for alternative in region.alternatives:
        _print_alternative(alternative, indent="    ")


def print_report(result: RecoveryResult) -> None:
    """
    Render a human-readable recovery report.
    """
    print(f"analysed range: {_format_range(result.analyzed_range)}")
    print(f"feasible interpretations: {result.solution_count}")
    if result.truncated:
        print("note: interpretation enumeration was truncated")
    print()
    if result.incompatible:
        print("incompatible receipt facts:")
        for item in result.incompatible:
            print(f"  {item.fact}: {item.reason}")
        return
    print(f"confirmed records: {len(result.confirmed)}")
    for record in result.confirmed:
        _print_record(record)
    print()
    print(f"uncertain regions: {len(result.uncertain)}")
    for region in result.uncertain:
        _print_region(region)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jsonlines",
        description="Read-only forensic recovery for a jsonlines append stream.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    recover_parser = subparsers.add_parser(
        "recover", help="recover records from an affected byte range"
    )
    recover_parser.add_argument(
        "stream", help="path to the binary append stream (opened read-only)"
    )
    recover_parser.add_argument(
        "--receipt", required=True, help="JSON file with remembered write facts"
    )
    recover_parser.add_argument(
        "--base-offset",
        type=int,
        default=0,
        help="absolute file offset of the first analysed byte",
    )
    recover_parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="maximum number of interpretations to enumerate",
    )
    recover_parser.add_argument(
        "--node-budget",
        type=int,
        default=200000,
        help="maximum number of search nodes before truncating",
    )
    recover_parser.add_argument(
        "--trial-budget",
        type=int,
        default=1000000,
        help="maximum number of option trials before truncating",
    )
    recover_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )

    args = parser.parse_args(argv)
    with open(args.receipt, "r", encoding="utf-8") as fp:
        receipt = receipt_from_dict(json.load(fp))
    result = recover_file(
        args.stream,
        receipt,
        base_offset=args.base_offset,
        solution_limit=args.limit,
        node_budget=args.node_budget,
        trial_budget=args.trial_budget,
    )
    if args.json:
        json.dump(result.as_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print_report(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
