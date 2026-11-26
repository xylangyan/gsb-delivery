# -*- coding: utf-8 -*-
"""Command-line interface for query-code review.

Examples::

    python -m querycode report spec.json
    python -m querycode compare spec.json --left OLD_A --right OLD_B --prefix f
    python -m querycode code spec.json --left OLD_A --right OLD_B --code fis
    python -m querycode check spec.json
"""
import argparse
import json
import sys
from typing import Optional

from .engine import SpecError, StrictError, load_spec, transliterate
from .workflow import classify_code, compare_sources, count_distinct_name_codes
from . import verify as _verify


def _load(path):
    try:
        return load_spec(path)
    except (OSError, ValueError) as exc:
        sys.stderr.write('cannot load %s: %s\n' % (path, exc))
        raise SystemExit(2)


def _print_result(result) -> None:
    kid = result.kind
    print('%s\t%s' % (result.code, kid))
    for label, witness in (('left', result.left), ('right', result.right)):
        if witness is None:
            continue
        print('  %s name: %r' % (label, witness.name))
        print('  %s template: %s' % (label, witness.template))
        print('  %s path: %s' % (label, ' -> '.join(witness.path)))
        print('  %s fragments: %s' % (label, ', '.join(witness.fragments)))


def cmd_report(args) -> int:
    spec = _load(args.spec)
    if not spec.queries:
        sys.stderr.write('spec has no queries\n')
        return 2
    out = {'queries': []}
    for query in spec.queries:
        source = query['source']
        prefix = query['prefix']
        limit = query['limit']
        shared, _all = compare_sources(spec, source, args.right, prefix=prefix,
                                       limit=0)
        distinct, page = count_distinct_name_codes(
            spec, source, args.right, prefix=prefix, limit=limit)
        if args.all:
            _shared, page = compare_sources(spec, source, args.right,
                                            prefix=prefix, limit=limit)
        entry = {
            'source': source,
            'prefix': prefix,
            'limit': limit,
            'shared': shared,
            'distinct_names': distinct,
            'page': [result.as_dict() for result in page],
        }
        out['queries'].append(entry)
        if args.json:
            continue
        print('source=%s prefix=%r limit=%d shared=%d distinct_names=%d'
              % (source, prefix, limit, shared, distinct))
        for result in page:
            _print_result(result)
    if args.json:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    return 0


def cmd_compare(args) -> int:
    spec = _load(args.spec)
    total, page = compare_sources(spec, args.left, args.right,
                                  prefix=args.prefix, limit=args.limit)
    if args.distinct:
        total, page = count_distinct_name_codes(
            spec, args.left, args.right, prefix=args.prefix,
            limit=args.limit)
    print('shared=%d' % total)
    for result in page:
        _print_result(result)
    return 0


def cmd_code(args) -> int:
    spec = _load(args.spec)
    result = classify_code(spec, args.left, args.right, args.code)
    _print_result(result)
    return 0


def cmd_check(args) -> int:
    spec = _load(args.spec)
    failed = False
    if args.left and args.right:
        total, page = compare_sources(spec, args.left, args.right, limit=None)
        for result in page:
            for label, side, witness in (('left', args.left, result.left),
                                         ('right', args.right, result.right)):
                if witness is None:
                    continue
                try:
                    got = transliterate(
                        witness.name,
                        spec.sources[side]['missing'],
                        spec.sources[side]['replace_str'])
                except StrictError as exc:
                    print('FAIL %s: strict rejection at %d' % (label, exc.index))
                    failed = True
                    continue
                if got != result.code:
                    print('FAIL %s witness %r -> %r, expected %r'
                          % (label, witness.name, got, result.code))
                    failed = True
        reference_total, reference_kinds = _verify.verify_by_enumeration(
            spec, args.left, args.right)
        if reference_total != total:
            print('FAIL enumeration count %d != search count %d'
                  % (reference_total, total))
            failed = True
        for code, kind in reference_kinds.items():
            result = classify_code(spec, args.left, args.right, code)
            if result.kind != kind:
                print('FAIL %r classified %s, enumeration says %s'
                      % (code, result.kind, kind))
                failed = True
    for source in spec.sources:
        from .search import Sheet
        sheet = Sheet(spec, source)
        for code in sheet.iter_codes():
            witness = sheet.witness(code)
            if witness is None:
                print('FAIL no witness for %r on %s' % (code, source))
                failed = True
                continue
            got = transliterate(
                witness.name, spec.sources[source]['missing'],
                spec.sources[source]['replace_str'])
            if got != code:
                print('FAIL %s witness %r -> %r for %r'
                      % (source, witness.name, got, code))
                failed = True
    print('FAILED' if failed else 'OK')
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='querycode')
    sub = parser.add_subparsers(dest='command', required=True)

    report = sub.add_parser('report', help='run every query in the spec')
    report.add_argument('spec')
    report.add_argument('--right', default='OLD_B',
                        help='sheet compared against each query source')
    report.add_argument('--json', action='store_true')
    report.add_argument('--all', action='store_true',
                        help='page same-name repeats too, not only distinct names')
    report.set_defaults(func=cmd_report)

    compare = sub.add_parser('compare', help='compare two sheets')
    compare.add_argument('spec')
    compare.add_argument('--left', required=True)
    compare.add_argument('--right', required=True)
    compare.add_argument('--prefix', default='')
    compare.add_argument('--limit', type=int, default=20)
    compare.add_argument('--distinct', action='store_true',
                         help='count only codes spelled by different names')
    compare.set_defaults(func=cmd_compare)

    code = sub.add_parser('code', help='classify one code')
    code.add_argument('spec')
    code.add_argument('--left', required=True)
    code.add_argument('--right', required=True)
    code.add_argument('--code', required=True)
    code.set_defaults(func=cmd_code)

    check = sub.add_parser('check', help='verify witnesses and brute force')
    check.add_argument('spec')
    check.add_argument('--left')
    check.add_argument('--right')
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SpecError as exc:
        sys.stderr.write('spec error: %s\n' % (exc,))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
