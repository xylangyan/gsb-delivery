# -*- coding: utf-8 -*-
"""Apply a fixed-page diff to a copy of the original book."""
from __future__ import absolute_import, unicode_literals

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dawg_diff  # noqa: E402


def apply_patch(source_path, manifest_path, output_path):
    manifest = json.load(open(manifest_path))
    if manifest.get('format') != 'dawg-int-completion-fixed-page-diff-v1':
        raise dawg_diff.DiffError('unknown diff format')
    original = open(source_path, 'rb').read()
    if len(original) != manifest['total_size']:
        raise dawg_diff.DiffError('source length %d does not match locked length %d'
                                  % (len(original), manifest['total_size']))
    page_size = manifest['page_size']
    patched = bytearray(original)
    for page in manifest['pages']:
        offset = page['offset']
        before = original[offset:offset + page['length']]
        if dawg_diff.sha256(before) != page['sha256_original']:
            raise dawg_diff.DiffError('original page %d hash mismatch' % page['page'])
        data = base64.b64decode(page['patch_base64'].encode('ascii'))
        if len(data) != page['length']:
            raise dawg_diff.DiffError('page %d payload length mismatch' % page['page'])
        if dawg_diff.sha256(data) != page['sha256_patched']:
            raise dawg_diff.DiffError('patched page %d hash mismatch' % page['page'])
        patched[offset:offset + page['length']] = data
    if len(patched) != manifest['total_size']:
        raise dawg_diff.DiffError('total length changed')
    with open(output_path, 'wb') as handle:
        handle.write(bytes(patched))
    return output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('manifest')
    parser.add_argument('output')
    args = parser.parse_args()
    apply_patch(args.source, args.manifest, args.output)
    print(args.output)


if __name__ == '__main__':
    main()
