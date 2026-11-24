"""Command line entry: ``python -m pyxlsb reconcile ...``."""

import argparse
import json
import sys

from . import open_workbook
from .reconcile import make_spec, reconcile


def _load_spec(path):
  with open(path, 'r', encoding='utf-8') as fp:
    data = json.load(fp)
  window = data['window']
  return make_spec(
    window['sheet'], tuple(window['rect']),
    [(item['sheet'], tuple(item['raw']), tuple(item['business']))
     for item in data['old_segments']],
    [(item['sheet'], tuple(item['raw']), tuple(item['business']))
     for item in data['new_segments']])


def _cmd_reconcile(args):
  spec = _load_spec(args.spec)
  with open_workbook(args.old) as old_wb, open_workbook(args.new) as new_wb:
    report = reconcile(old_wb, new_wb, spec)
  json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
  sys.stdout.write('\n')
  return 0 if not report.different else 1


def main(argv=None):
  parser = argparse.ArgumentParser(prog='pyxlsb')
  sub = parser.add_subparsers(dest='command', required=True)
  rec = sub.add_parser('reconcile',
                       help='compare stored formulas of two XLSB revisions')
  rec.add_argument('old', help='path to the old revision .xlsb')
  rec.add_argument('new', help='path to the new revision .xlsb')
  rec.add_argument('spec', help='JSON file with window and segment mappings')
  rec.set_defaults(func=_cmd_reconcile)
  args = parser.parse_args(argv)
  return args.func(args)


if __name__ == '__main__':
  sys.exit(main())
