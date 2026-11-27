"""Command-line entry point for :mod:`pyuca.audit`."""

from __future__ import unicode_literals

import argparse
import json
import sys

from .audit import (
    AuditError,
    format_report,
    load_collator,
    run_catalog_file,
)


def _pairs(values):
    pairs = []
    for value in values or []:
        if ":" in value:
            old, new = value.split(":", 1)
        elif "->" in value:
            old, new = value.split("->", 1)
        else:
            raise AuditError("table pair should be OLD:NEW, got {0}"
                             .format(value))
        pairs.append((old, new))
    return pairs


def _boundaries(values):
    result = []
    for value in values or []:
        try:
            result.append(int(value))
        except ValueError:
            result.append(value)
    return result


def _pair_selector(values):
    if not values:
        return None
    if len(values) != 2:
        raise AuditError("--pair needs exactly two accession numbers")
    pair = []
    for value in values:
        try:
            pair.append(int(value))
        except ValueError:
            pair.append(value)
    return tuple(pair)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pyuca-audit",
        description="Compare two pyuca collation tables over an archived "
                    "goods-name catalog.",
    )
    parser.add_argument(
        "catalog", nargs="?",
        help="catalog JSON file; its old_table/new_table are used by default")
    parser.add_argument("--old", help="old allkeys file or UCA version")
    parser.add_argument("--new", help="new allkeys file or UCA version")
    parser.add_argument(
        "--compare", action="append", metavar="OLD:NEW",
        help="compare a table pair without a catalog (diff only)")
    parser.add_argument(
        "--mode", choices=("full", "primary"),
        help="full keys or the primary index sheet")
    parser.add_argument(
        "--tie-break", choices=("entry", "none"),
        help="break equal keys by first accession number, or report ties")
    parser.add_argument("--pages", help="JSON page layout or @file")
    parser.add_argument(
        "--boundary", action="append", default=[], metavar="PAGE_OR_ANCHOR",
        help="page edge to report crossings for (1 = between pages 1 and 2)")
    parser.add_argument(
        "--approvals",
        help="JSON with approved 'rules' and 'implicit_ranges'")
    parser.add_argument(
        "--verify-old-keys", dest="verify_old_keys", action="store_true",
        default=None,
        help="recompute and flag stored old keys that disagree")
    parser.add_argument(
        "--no-verify-old-keys", dest="verify_old_keys", action="store_false",
        help="trust stored old keys without a full recomputation")
    parser.add_argument("--sample-pairs", type=int, default=8,
                        help="maximum witness pairs to include")
    parser.add_argument(
        "--pair", nargs=2, metavar=("ENTRY_A", "ENTRY_B"),
        help="explain both sorts for this chosen pair of accession numbers")
    parser.add_argument(
        "--no-page-ranges", action="store_true",
        help="omit per-page accession ranges from JSON output")
    parser.add_argument(
        "--page-range", type=int, metavar="PAGE",
        help="emit only this page's old/new accession segments")
    parser.add_argument("--max-records", type=int, default=40,
                        help="maximum records in text output")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", help="write output to this path")
    parser.add_argument("--expand", action="store_true",
                        help="expand compressed ranges in JSON output")
    return parser


def _json_option(value):
    if value is None:
        return None
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as option_file:
            return json.load(option_file)
    try:
        return json.loads(value)
    except ValueError:
        with open(value, encoding="utf-8") as option_file:
            return json.load(option_file)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.compare:
            from .audit import diff_tables
            results = []
            for old_spec, new_spec in _pairs(args.compare):
                diff = diff_tables(
                    load_collator(old_spec), load_collator(new_spec),
                    approvals=_json_option(args.approvals),
                )
                results.append(diff.to_dict())
            payload = results[0] if len(results) == 1 else results
            if args.format == "json":
                output = json.dumps(payload, ensure_ascii=False, indent=2)
            else:
                output = "\n".join(
                    "rules={0} contractions={1} implicit_ranges={2}".format(
                        item["summary"]["rules"],
                        item["summary"]["contractions"],
                        item["summary"]["implicit_ranges"])
                    for item in results
                )
        else:
            if not args.catalog:
                parser.error("a catalog file or --compare is required")
            options = {
                "verify_old_keys": args.verify_old_keys,
                "sample_pairs": args.sample_pairs,
                "pair": _pair_selector(args.pair),
                "include_page_ranges": not args.no_page_ranges,
            }
            if args.mode:
                options["mode"] = args.mode
            if args.tie_break:
                options["tie_break"] = args.tie_break
            if args.pages:
                options["pages"] = _json_option(args.pages)
            if args.boundary:
                options["boundaries"] = _boundaries(args.boundary)
            if args.approvals:
                options["approvals"] = _json_option(args.approvals)
            report = run_catalog_file(
                args.catalog, old=args.old, new=args.new, **options)
            if args.format == "json":
                if args.page_range is not None:
                    payload = report.page_range(args.page_range)
                    payload["summary"] = report.to_dict(
                        max_records=0,
                        include_page_ranges=False)["summary"]
                    output = json.dumps(payload, ensure_ascii=False, indent=2)
                else:
                    output = json.dumps(
                        report.to_dict(
                            max_records=None, expand=args.expand,
                            include_page_ranges=not args.no_page_ranges),
                        ensure_ascii=False, indent=2,
                    )
            else:
                output = format_report(
                    report, max_records=args.max_records,
                    include_page_ranges=not args.no_page_ranges)
    except (AuditError, OSError, ValueError) as error:
        sys.stderr.write("pyuca-audit: {0}\n".format(error))
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(output)
            output_file.write("\n")
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
