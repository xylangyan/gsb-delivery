"""Interleaved batch processing with per-batch scalar rules.

Each ``--batch PATH=RULE`` pair is processed in order with its own
rule set, so legacy and modern batches can be interleaved without ever
sharing a resolver.  By default a batch preserves the reading of its
own rule; ``--preserve`` overrides that for every batch.  For every
file the tool prints confirmable lines with their old/new readings;
files containing an irreversible merge conflict are reported and never
rewritten.
"""

import argparse
import json
import os
import sys

from .audit import scan, plan_rewrite, quote_rewrite, load
from .rules import RULES, LEGACY_RULE, MODERN_RULE


def _iter_yaml_files(path):
    if os.path.isfile(path):
        return [path]
    return sorted(
        os.path.join(path, name)
        for name in os.listdir(path)
        if name.endswith(('.yaml', '.yml')))


def _print_rows(rows):
    for row in rows:
        print("  L%-3d %-5s %-28s legacy=%-26s modern=%-26s action=%s%s"
              % (row.line, row.kind, repr(row.text), row.legacy_read,
                 row.modern_read,
                 row.replacement if not row.blocked else 'BLOCKED',
                 '' if not row.blocked else ''))


def _print_conflicts(conflicts):
    for conflict in conflicts:
        print("  CONFLICT at mapping line %d: key equality differs"
              % conflict.line)
        for source_line, legacy_entry, modern_entry in conflict.key_groups:
            legacy_scalar, legacy_key = legacy_entry
            modern_scalar, modern_key = modern_entry
            print("    L%-3d scalar=%-8r legacy key=%-10r modern key=%r"
                  % (source_line, legacy_scalar, legacy_key, modern_key))


def process_batch(batch_path, rule, preserve, out_dir, use_quote):
    print("== batch %s  rule=%s  preserve=%s"
          % (batch_path, rule, preserve))
    summary = {'batch': batch_path, 'rule': rule,
               'preserve': preserve, 'files': []}
    for filename in _iter_yaml_files(batch_path):
        with open(filename, encoding='utf-8') as stream:
            text = stream.read()
        rows, conflicts = scan(text, preserve=preserve)
        report = {'file': filename, 'divergent_scalars': len(rows),
                  'conflicts': len(conflicts)}
        print("-- %s" % filename)
        _print_rows(rows)
        if conflicts:
            _print_conflicts(conflicts)
            report['status'] = 'conflict'
            summary['files'].append(report)
            continue

        if use_quote:
            rewritten, _ = quote_rewrite(text, preserve=preserve)
        else:
            rewritten, _ = plan_rewrite(text, preserve=preserve)
        _verify(text, rewritten, preserve)
        report['status'] = 'rewritten'
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            target = os.path.join(out_dir, os.path.basename(filename))
            with open(target, 'w', encoding='utf-8') as stream:
                stream.write(rewritten)
            report['output'] = target
            print("  wrote %s" % target)
        else:
            report['output_preview'] = rewritten
        summary['files'].append(report)
    return summary


def _verify(original, rewritten, preserve):
    expected = load(original, preserve)
    legacy_rewritten = load(rewritten, LEGACY_RULE)
    modern_rewritten = load(rewritten, MODERN_RULE)
    assert legacy_rewritten == expected, "legacy read-back mismatch"
    assert modern_rewritten == expected, "modern read-back mismatch"
    assert type_compatible(legacy_rewritten, expected)
    print("  read-back OK under both rules (preserve=%s)" % preserve)


def type_compatible(actual, expected):
    if isinstance(expected, dict):
        return all(type_compatible(actual[key], value)
                   for key, value in expected.items())
    if isinstance(expected, list):
        return all(type_compatible(a, e)
                   for a, e in zip(actual, expected))
    return type(actual) is type(expected)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Process YAML batches with explicit scalar rules.")
    parser.add_argument('--batch', action='append', default=[],
                        metavar='PATH=RULE',
                        help='batch directory or file with its rule, '
                             'e.g. samples/batch_old=legacy; repeatable')
    parser.add_argument('--preserve', choices=RULES, default=None,
                        help='reading to preserve for every batch; '
                             'defaults to each batch own rule')
    parser.add_argument('--quote', action='store_true',
                        help='pin every divergent scalar as a quoted string')
    parser.add_argument('--out-dir', default=None,
                        help='directory for rewritten files')
    parser.add_argument('--report', default=None,
                        help='write a JSON summary to this path')
    args = parser.parse_args(argv)
    if not args.batch:
        parser.error("at least one --batch PATH=RULE is required")

    summaries = []
    had_conflict = False
    for spec in args.batch:
        if '=' not in spec:
            parser.error("invalid --batch %r; expected PATH=RULE" % spec)
        path, rule = spec.rsplit('=', 1)
        if rule not in RULES:
            parser.error("unknown rule %r; expected one of %s"
                         % (rule, ', '.join(RULES)))
        preserve = args.preserve or rule
        summary = process_batch(path, rule, preserve, args.out_dir,
                                args.quote)
        summaries.append(summary)
        had_conflict = had_conflict or any(
            item['status'] == 'conflict' for item in summary['files'])

    if args.report:
        with open(args.report, 'w', encoding='utf-8') as stream:
            json.dump(summaries, stream, indent=2, ensure_ascii=False,
                      default=str)
    return 1 if had_conflict else 0


if __name__ == '__main__':
    sys.exit(main())
