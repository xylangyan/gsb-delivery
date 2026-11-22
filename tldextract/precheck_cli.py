"""Command line interface for replayable site-log prechecks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .precheck import PrecheckError, SiteLogPrecheck, Update, UpdateLedger


def main() -> None:
    """Run the ``tldextract-precheck`` command."""
    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    engine = _new_engine(args)

    try:
        new_updates = _run(args, engine)
    except (OSError, PrecheckError) as exc:
        print(f"tldextract-precheck: {exc}", file=sys.stderr)
        sys.exit(2)

    _emit(args, engine, new_updates)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tldextract-precheck",
        description=(
            "Classify JSON Lines site-log rows with versioned missing-value"
            " policies and emit replayable corrections."
        ),
    )
    parser.add_argument(
        "events",
        metavar="events.jsonl",
        nargs="+",
        help="JSON Lines file of config and row events, read in order",
    )
    parser.add_argument(
        "--state",
        help="snapshot of retained configs and row facts; loaded when it"
        " exists and always saved after processing",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore any existing --state snapshot instead of loading it",
    )
    parser.add_argument(
        "--suffix-list",
        help="local Public Suffix List file; defaults to the bundled snapshot",
    )
    parser.add_argument(
        "--internal-host",
        action="append",
        default=[],
        help="hostname or parent domain registered as internal; repeatable",
    )
    parser.add_argument(
        "--missing-value",
        action="append",
        default=None,
        help="token treated as missing when no config covers the row; repeatable",
    )
    parser.add_argument(
        "--updates",
        metavar="updates.jsonl",
        help="write only the updates emitted by this run to this JSON Lines file",
    )
    parser.add_argument(
        "--full-updates",
        metavar="all-updates.jsonl",
        help="write every update this snapshot has ever emitted, for a fresh"
        " downstream replay",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print a reconciliation report at the end instead of updates",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.fresh and not args.state:
        parser.error("--fresh requires --state")


def _new_engine(args: argparse.Namespace) -> SiteLogPrecheck:
    missing_values = args.missing_value
    return SiteLogPrecheck(
        suffix_list_path=args.suffix_list,
        internal_hosts=args.internal_host,
        default_missing_values=(
            ("-", "") if missing_values is None else tuple(missing_values)
        ),
    )


def _run(args: argparse.Namespace, engine: SiteLogPrecheck) -> tuple[Update, ...]:
    if args.state and not args.fresh and Path(args.state).exists():
        engine.load_file(args.state)

    for events_path in args.events:
        engine.ingest_file(events_path)

    new_updates = engine.drain_updates()

    if args.updates:
        _write_updates(Path(args.updates), new_updates)
    if args.full_updates:
        _write_updates(Path(args.full_updates), engine.updates)
    if args.state:
        engine.save(args.state)

    return new_updates


def _emit(
    args: argparse.Namespace,
    engine: SiteLogPrecheck,
    new_updates: tuple[Update, ...],
) -> None:
    ledger = UpdateLedger()
    ledger.apply_all(engine.updates)

    if args.report:
        _print_report(engine, ledger)
        return

    for update in new_updates:
        print(update.to_json())


def _write_updates(path: Path, updates: tuple[Update, ...]) -> None:
    with path.open("w", encoding="utf-8") as update_file:
        for update in updates:
            update_file.write(update.to_json() + "\n")


def _print_report(engine: SiteLogPrecheck, ledger: UpdateLedger) -> None:
    report: dict[str, Any] = {
        "records": engine.reconciliation(),
        "ledger": ledger.reconciliation(),
        "by_source": _counts_by_source(engine),
        "pending": [record.as_dict() for record in engine.records() if record.pending],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _counts_by_source(engine: SiteLogPrecheck) -> dict[str, dict[str, int]]:
    by_source: dict[str, dict[str, int]] = {}
    for record in engine.records():
        counts = by_source.setdefault(record.source, {})
        counts[record.bucket] = counts.get(record.bucket, 0) + 1
    return by_source


if __name__ == "__main__":
    main()
