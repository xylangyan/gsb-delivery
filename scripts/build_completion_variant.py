# -*- coding: utf-8 -*-
"""Build the delete-terminal variant from completion.dawg."""
from __future__ import absolute_import, unicode_literals

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dawg_diff  # noqa: E402

SOURCE = os.path.join(ROOT, 'dev_data', 'small', 'completion.dawg')
OUT_DIR = os.path.join(ROOT, 'artifacts', 'int_completion_diff')
PATCHED = os.path.join(OUT_DIR, 'completion.patched.dawg')
MANIFEST = os.path.join(OUT_DIR, 'completion.patch.json')

# Original book: bar=0, f=0, foo=0, foobar=0.
# Delete f (terminal with the longer foo/foobar descendants).
TARGET = {b'bar': 7, b'baz': 9, b'foo': 2, b'foobar': 0, b'food': 4}
CONSTRAINTS = {
    'page_size': 512,
    'total_size': 1544,
    'pages': [{'page': 0}, {'page': 2}],
}


def main():
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    raw, patched, report = dawg_diff.build_book(SOURCE, TARGET, CONSTRAINTS)
    with open(PATCHED, 'wb') as handle:
        handle.write(patched)

    from dawg_python import IntCompletionDAWG

    dawg = IntCompletionDAWG().load(PATCHED)
    expected_items = [(key.decode('utf-8'), value)
                      for key, value in sorted(TARGET.items())]
    assert dawg.items() == expected_items
    assert dawg.keys() == [key.decode('utf-8') for key in sorted(TARGET)]
    assert 'f' not in dawg
    assert dawg['foo'] == 2
    assert dawg['foobar'] == 0
    assert dawg.keys('fo') == ['foo', 'foobar', 'food']
    assert dawg.prefixes('foobarz') == ['foo', 'foobar']

    pages = []
    for page in report['pages']:
        before = dawg_diff.page_slice(raw, page)
        after = dawg_diff.page_slice(patched, page)
        pages.append({
            'page': page,
            'offset': page * dawg_diff.PAGE_SIZE,
            'length': len(after),
            'sha256_original': dawg_diff.sha256(before),
            'sha256_patched': dawg_diff.sha256(after),
            'crc32_original': dawg_diff.crc32(before),
            'crc32_patched': dawg_diff.crc32(after),
            'original_base64': base64.b64encode(before).decode('ascii'),
            'patch_base64': base64.b64encode(after).decode('ascii'),
        })

    manifest = {
        'format': 'dawg-int-completion-fixed-page-diff-v1',
        'source': os.path.relpath(SOURCE, ROOT),
        'source_size': len(raw),
        'total_size': len(patched),
        'page_size': dawg_diff.PAGE_SIZE,
        'page_count': 4,
        'changed_pages': report['pages'],
        'received_page_count': len(report['pages']),
        'book': {key.decode('utf-8'): value for key, value in sorted(TARGET.items())},
        'deleted': ['f'],
        'changed': {'bar': {'from': 0, 'to': 7}, 'foo': {'from': 0, 'to': 2}},
        'added': {'baz': 9, 'food': 4},
        'layout': {
            'unit_slots': report['slot'],
            'value_slots': report['value_slot'],
            'base': report['base'],
            'attempts': report['attempts'],
        },
        'pages': pages,
    }
    with open(MANIFEST, 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print('pages', report['pages'])
    print('book', dict(sorted(TARGET.items())))


if __name__ == '__main__':
    main()
