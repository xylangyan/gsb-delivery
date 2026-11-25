"""
    pygments.lexers.sql_jinja_audit_cli
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Small command line front end for the SQL+Jinja audit helpers.

    Usage::

        python -m pygments.lexers.sql_jinja_audit_cli audit OLD.sql NEW.sql
        python -m pygments.lexers.sql_jinja_audit_cli rounds ROUNDS.json

    ``ROUNDS.json`` is either a list of ``{"round": n, "fragments": [...]}``
    objects or the same mapping keyed by round number.  Each fragment is a
    string or ``{"text": ..., "fragment": ..., "start": ...}``.

    :copyright: Copyright 2006-present by the Pygments team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import argparse
import json
import sys

from pygments.lexers.sql_jinja_audit import (
    audit, audit_history, compare_audits, compare_classification,
    compare_tokens,
)


def _read(path):
    if path == '-':
        return sys.stdin.read()
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _diff_payload(old_source, new_source):
    old = audit(old_source)
    new = audit(new_source)
    diff = compare_audits(old, new)
    items = []
    for change in diff.changes:
        item = change.new if change.new is not None else change.old
        items.append({
            'change': change.kind,
            'text': item.text,
            'spans': [list(span) for span in item.source_spans],
            'variables': list(item.variables),
        })
    reclassified = []
    for change in compare_tokens(old, new):
        if change.kind != 'reclassified':
            continue
        reclassified.append({
            'value': change.new.value,
            'old_type': str(change.old.token_type),
            'new_type': str(change.new.token_type),
            'old_part': change.old.part,
            'new_part': change.new.part,
            'span': [change.new.start, change.new.end],
        })
    classified = []
    for change in compare_classification(old, new):
        classified.append({
            'text': change.text,
            'old_span': list(change.old_span),
            'new_span': list(change.new_span),
            'old_types': [[str(token_type), part]
                          for token_type, part in change.old_types],
            'new_types': [[str(token_type), part]
                          for token_type, part in change.new_types],
        })
    return {
        'items': items,
        'reclassified_tokens': reclassified,
        'reclassified_spans': classified,
    }


def _rounds_payload(payload):
    rounds = payload.get('rounds', payload) if isinstance(payload, dict) else payload
    if isinstance(rounds, dict):
        pairs = [(key, value) for key, value in rounds.items()]
    else:
        pairs = [(entry.get('round'), entry.get('fragments', []))
                 for entry in rounds]

    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    history = audit_history((number(round_no), fragments)
                            for round_no, fragments in pairs)
    rv = {}
    for round_no in history.rounds:
        result = history.audit_round(round_no)
        rv[str(round_no)] = {
            'items': [
                {
                    'item_id': item.item_id,
                    'uid': item.uid,
                    'text': item.text,
                    'spans': [
                        {'round': ref.round, 'fragment': ref.fragment,
                         'start': ref.start, 'end': ref.end}
                        for ref in result.original_spans(item.item_id)
                    ],
                    'variables': list(item.variables),
                }
                for item in result.items
            ],
        }
    return rv


def main(argv=None):
    parser = argparse.ArgumentParser(prog='sql-jinja-audit')
    sub = parser.add_subparsers(dest='command', required=True)

    audit_parser = sub.add_parser('audit', help='audit one source slice')
    audit_parser.add_argument('source')
    audit_parser.add_argument('--json', action='store_true')

    diff_parser = sub.add_parser('diff', help='compare old and new drafts')
    diff_parser.add_argument('old')
    diff_parser.add_argument('new')

    query_parser = sub.add_parser(
        'query', help='show all original intervals for a round item')
    query_parser.add_argument('source')
    query_parser.add_argument('item_id', type=int)

    rounds_parser = sub.add_parser('rounds', help='audit a round manifest')
    rounds_parser.add_argument('manifest')

    args = parser.parse_args(argv)
    if args.command == 'audit':
        result = audit(_read(args.source))
        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        else:
            for token in result.tokens:
                print(f'{token.start:5d}:{token.end:<5d} '
                      f'{str(token.token_type):32s} {token.value!r}')
        return 0
    if args.command == 'diff':
        print(json.dumps(_diff_payload(_read(args.old), _read(args.new)),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == 'query':
        result = audit(_read(args.source))
        item, spans = result.query(args.item_id)
        print(json.dumps({
            'text': item.text,
            'spans': [{'round': ref.round, 'fragment': ref.fragment,
                       'start': ref.start, 'end': ref.end}
                      for ref in spans],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'rounds':
        payload = json.loads(_read(args.manifest))
        print(json.dumps(_rounds_payload(payload), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
