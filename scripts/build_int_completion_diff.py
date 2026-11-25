# -*- coding: utf-8 -*-
"""Build the concrete fixed-page diff for an IntCompletionDAWG book."""
from __future__ import absolute_import, unicode_literals

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dawg_diff  # noqa: E402

SOURCE = os.path.join(ROOT, 'dev_data', 'small', 'int_completion_dawg.dawg')
OUT_DIR = os.path.join(ROOT, 'artifacts', 'int_completion_diff')
PATCHED = os.path.join(OUT_DIR, 'int_completion_dawg.patched.dawg')
MANIFEST = os.path.join(OUT_DIR, 'patch.json')

# Original book: bar=5, foo=1, foobar=3.
# Edits:
#   bar 5 -> 7        (suffix nodes shared with foobar; foobar stays 3)
#   foo 1 -> 2        (internal terminal; foo/foo bar path retained)
#   baz=9, food=4     (two new keys)
#   f not in this book, so no deletion here; see the completion.dawg variant
#   for the delete-terminal-keep-descendant case.
TARGET = {
    b'bar': 7,
    b'baz': 9,
    b'foo': 2,
    b'foobar': 3,
    b'food': 4,
}

PAGES = [{'page': 0}, {'page': 2}]
CONSTRAINTS = {'page_size': 512, 'total_size': 1544, 'pages': PAGES}


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
    if dawg.items() != expected_items:
        raise AssertionError('library items mismatch: %r' % (dawg.items(),))
    if dawg.keys() != [key.decode('utf-8') for key in sorted(TARGET)]:
        raise AssertionError('library keys mismatch: %r' % (dawg.keys(),))
    for key, value in TARGET.items():
        if key not in dawg or dawg.get(key) != value:
            raise AssertionError('lookup mismatch for %r' % key)
    for missing in (b'f', b'fo', b'fooba'):
        if missing in dawg:
            raise AssertionError('unexpected key accepted: %r' % missing)

    prefix_cases = {
        'foobarz': ['foo', 'foobar'],
        'bar': ['bar'],
        'baz': ['baz'],
        'food': ['foo', 'food'],
        'foobar': ['foo', 'foobar'],
        'f': [],
        'fo': [],
        'b': [],
        'x': [],
    }
    for probe, want in prefix_cases.items():
        got = dawg.prefixes(probe)
        if got != want:
            raise AssertionError('prefixes(%r)=%r != %r' % (probe, got, want))

    for prefix in ['', 'b', 'ba', 'f', 'fo', 'foo', 'food', 'z']:
        got = dawg.keys(prefix)
        byte_prefix = prefix.encode('utf-8')
        want = [key.decode('utf-8') for key in sorted(TARGET)
                if key.startswith(byte_prefix)]
        if got != want:
            raise AssertionError('keys(%r)=%r != %r' % (prefix, got, want))

    page_count = (len(raw) + dawg_diff.PAGE_SIZE - 1) // dawg_diff.PAGE_SIZE
    changed_pages = report['pages']
    pages = []
    for page in changed_pages:
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
        'page_count': page_count,
        'changed_pages': changed_pages,
        'received_page_count': len(changed_pages),
        'book': {key.decode('utf-8'): value for key, value in sorted(TARGET.items())},
        'changed': {'bar': {'from': 5, 'to': 7}, 'foo': {'from': 1, 'to': 2}},
        'added': {'baz': 9, 'food': 4},
        'shared_suffix': {'changed': 'bar', 'other': 'foobar', 'other_value': 3},
        'layout': {
            'unit_slots': report['slot'],
            'value_slots': report['value_slot'],
            'base': report['base'],
            'attempts': report['attempts'],
        },
        'pages': pages,
        'checks': {
            'direct_single_key': 'exact target; no extra key accepted',
            'completion': 'exact target in ascending key order',
            'prefixes': 'checked for retained/branch prefixes',
            'shared_suffix': 'bar changed to 7; foobar still returns 3',
            'internal_terminal': 'foo changed to 2; foo/foobar retained',
            'new_keys': 'baz=9 and food=4 added',
        },
    }
    with open(MANIFEST, 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')

    print('source   :', SOURCE)
    print('patched  :', PATCHED)
    print('manifest :', MANIFEST)
    print('size     :', len(raw), '->', len(patched))
    print('pages    :', changed_pages, 'of', page_count)
    for item in pages:
        print('  page %d  %s -> %s' % (item['page'], item['sha256_original'],
                                       item['sha256_patched']))
    print('book     :', dict(sorted(TARGET.items())))


if __name__ == '__main__':
    main()
