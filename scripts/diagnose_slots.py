# -*- coding: utf-8 -*-
"""Report pages and XOR jump constraints for an infeasible free-slot set."""
from __future__ import absolute_import, unicode_literals

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dawg_diff  # noqa: E402

SOURCE = os.path.join(ROOT, 'dev_data', 'small', 'completion.dawg')
TARGET = {b'bar': 7, b'baz': 9, b'foo': 2, b'foobar': 0, b'food': 4}


def summarize(report):
    out = {}
    for key in ('reason', 'required', 'pages', 'jump_constraints'):
        if key in report:
            out[key] = report[key]
    failures = report.get('failures') or []
    if not failures and 'required' in report:
        out['must_fix'] = report.get('reason')
    return out


def show(label, constraints):
    try:
        dawg_diff.build_book(SOURCE, TARGET, constraints)
    except dawg_diff.LayoutInfeasible as exc:
        print('==', label, '=> infeasible')
        print(json.dumps(summarize(exc.report), indent=2, sort_keys=True))
        return
    print('==', label, '=> feasible')


def main():
    show('page 2 only (guide slots, but no root unit slot)', {
        'page_size': 512, 'total_size': 1544,
        'pages': [{'page': 2}],
    })
    show('pages 0+1 only (unit slots, but no guide slots)', {
        'page_size': 512, 'total_size': 1544,
        'pages': [{'page': 0}, {'page': 1}],
    })
    show('sparse slots omit root guide entry', {
        'page_size': 512, 'total_size': 1544,
        'pages': [{
            'page': 0,
            'free_unit_slots': [0, 1, 2, 3, 4, 5, 6, 7],
            'free_guide_slots': [1, 2, 3, 4],
        }],
    })


if __name__ == '__main__':
    main()
