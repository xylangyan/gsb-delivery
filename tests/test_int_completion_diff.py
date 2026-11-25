# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

import json
import os
import subprocess
import sys

import pytest
import dawg_python
import dawg_python.units as units

from .utils import data_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(ROOT, 'scripts')
ARTIFACT_DIR = os.path.join(ROOT, 'artifacts', 'int_completion_diff')


def _load_module():
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    import dawg_diff
    return dawg_diff


dawg_diff = _load_module()


def _source(name):
    return data_path('small', name)


def _build(name, target, pages=None, total_size=1544):
    constraints = {'page_size': 512, 'total_size': total_size}
    if pages is not None:
        constraints['pages'] = pages
    return dawg_diff.build_book(_source(name), target, constraints)


def _write(tmpdir, name, data):
    path = os.path.join(str(tmpdir), name)
    with open(path, 'wb') as handle:
        handle.write(data)
    return path


class TestSharedSuffix(object):
    """bar and foobar share the 'ba'/'bar' nodes in this fixture."""

    TARGET = {b'bar': 7, b'baz': 9, b'foo': 2, b'foobar': 3, b'food': 4}

    def test_direct_and_completion(self, tmp_path):
        _, patched, report = _build('int_completion_dawg.dawg', self.TARGET)
        path = _write(tmp_path, 'patched.dawg', patched)
        dawg = dawg_python.IntCompletionDAWG().load(path)
        assert dawg.items() == [
            ('bar', 7), ('baz', 9), ('foo', 2), ('foobar', 3), ('food', 4)
        ]
        assert dawg.keys() == ['bar', 'baz', 'foo', 'foobar', 'food']
        assert dawg.keys('ba') == ['bar', 'baz']
        assert dawg.get('bar') == 7
        assert dawg.get('foobar') == 3
        assert report['pages'] == [0, 2]

    def test_direct_raw_graph_has_no_extra_keys(self, tmp_path):
        _, patched, _ = _build('int_completion_dawg.dawg', self.TARGET)
        path = _write(tmp_path, 'patched.dawg', patched)
        _, n, unit_table, guide = dawg_diff.read_dawg(path)
        assert dawg_diff.direct_items(unit_table) == self.TARGET
        completed = dawg_diff.completion_items(unit_table, guide)
        assert [k for k, v in completed] == sorted(self.TARGET)

    def test_prefixes(self, tmp_path):
        _, patched, _ = _build('int_completion_dawg.dawg', self.TARGET)
        path = _write(tmp_path, 'patched.dawg', patched)
        dawg = dawg_python.IntCompletionDAWG().load(path)
        assert dawg.prefixes('foobarz') == ['foo', 'foobar']
        assert dawg.prefixes('bazooka') == ['baz']
        assert dawg.prefixes('bar') == ['bar']
        assert dawg.prefixes('f') == []


class TestDeletedTerminal(object):
    """Delete a terminal while keeping its longer descendants."""

    TARGET = {b'bar': 7, b'baz': 9, b'foob': 6, b'fooc': 8, b'foobar': 3}

    def test_terminal_is_gone_but_descendants_remain(self, tmp_path):
        _, patched, _ = _build('int_completion_dawg.dawg', self.TARGET)
        path = _write(tmp_path, 'patched.dawg', patched)
        dawg = dawg_python.IntCompletionDAWG().load(path)
        assert 'foo' not in dawg
        with pytest.raises(KeyError):
            dawg['foo']
        assert dawg['foob'] == 6
        assert dawg['fooc'] == 8
        assert dawg['foobar'] == 3
        assert dawg.keys('foo') == ['foob', 'foobar', 'fooc']
        assert dawg.prefixes('foobarz') == ['foob', 'foobar']

    def test_no_phantom_key(self, tmp_path):
        _, patched, _ = _build('int_completion_dawg.dawg', self.TARGET)
        path = _write(tmp_path, 'patched.dawg', patched)
        dawg = dawg_python.IntCompletionDAWG().load(path)
        # These are exactly the keys the raw unit graph accepts.
        _, n, unit_table, guide = dawg_diff.read_dawg(path)
        assert dawg_diff.direct_items(unit_table) == self.TARGET
        for phantom in (b'fo', b'fooa', b'food', b'foobarx'):
            assert phantom not in dawg
            assert dawg.get(phantom) is None


class TestPageDiff(object):
    TARGET = {b'bar': 7, b'baz': 9, b'foo': 2, b'foobar': 3, b'food': 4}

    def test_two_pages_and_length(self, tmp_path):
        raw, patched, report = _build('int_completion_dawg.dawg', self.TARGET)
        assert len(patched) == len(raw)
        assert report['pages'] == [0, 2]
        changed = []
        for page in range(4):
            if dawg_diff.page_slice(raw, page) != dawg_diff.page_slice(patched, page):
                changed.append(page)
        assert changed == [0, 2]
        # Pages 1 and 3 are byte-identical.
        assert dawg_diff.page_slice(raw, 1) == dawg_diff.page_slice(patched, 1)
        assert dawg_diff.page_slice(raw, 3) == dawg_diff.page_slice(patched, 3)

    def test_page_hashes(self, tmp_path):
        raw, patched, _ = _build('int_completion_dawg.dawg', self.TARGET)
        for page in (0, 2):
            before = dawg_diff.page_slice(raw, page)
            after = dawg_diff.page_slice(patched, page)
            assert dawg_diff.sha256(before) != dawg_diff.sha256(after)
            assert dawg_diff.crc32(before) != dawg_diff.crc32(after)

    def test_infeasible_slots_keep_original(self):
        with pytest.raises(dawg_diff.LayoutInfeasible) as excinfo:
            _build('int_completion_dawg.dawg', self.TARGET, pages=[{'page': 2}])
        report = excinfo.value.report
        assert report['pages'] == [2]
        assert 'root unit slot 0' in json.dumps(report)


class TestArtifacts(object):
    def test_manifest_and_patch_match(self):
        manifest_path = os.path.join(ARTIFACT_DIR, 'patch.json')
        if not os.path.exists(manifest_path):
            pytest.skip('artifact not built')
        manifest = json.load(open(manifest_path))
        assert manifest['total_size'] == manifest['source_size'] == 1544
        assert manifest['received_page_count'] == 2
        assert manifest['changed_pages'] == [0, 2]
        source = os.path.join(ROOT, manifest['source'])
        original = open(source, 'rb').read()
        rebuilt = bytearray(original)
        import base64
        for page in manifest['pages']:
            before = dawg_diff.page_slice(original, page['page'])
            assert dawg_diff.sha256(before) == page['sha256_original']
            data = base64.b64decode(page['patch_base64'].encode('ascii'))
            assert dawg_diff.sha256(data) == page['sha256_patched']
            rebuilt[page['offset']:page['offset'] + page['length']] = data
        patched_path = os.path.join(ARTIFACT_DIR, 'int_completion_dawg.patched.dawg')
        assert bytes(rebuilt) == open(patched_path, 'rb').read()

    def test_apply_cli_roundtrip(self):
        import dawg_python
        import apply_page_diff
        manifest_path = os.path.join(ARTIFACT_DIR, 'patch.json')
        if not os.path.exists(manifest_path):
            pytest.skip('artifact not built')
        manifest = json.load(open(manifest_path))
        source = os.path.join(ROOT, manifest['source'])
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.dawg', delete=False) as handle:
            target = handle.name
        try:
            apply_page_diff.apply_patch(source, manifest_path, target)
            dawg = dawg_python.IntCompletionDAWG().load(target)
            assert dawg.items() == [
                ('bar', 7), ('baz', 9), ('foo', 2), ('foobar', 3), ('food', 4)
            ]
            assert dawg.keys('foo') == ['foo', 'foobar', 'food']
        finally:
            if os.path.exists(target):
                os.unlink(target)
