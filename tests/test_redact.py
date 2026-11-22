# -*- coding:utf-8 -*-
import json
import subprocess
import sys

import pytest

from ijson import redact
from ijson.backends import python as pybackend
from ijson.common import ObjectBuilder


# The engine must run on ijson's pure-python entry points, not the C backends.
def test_uses_pure_python_entry_points():
    assert redact.Lexer is pybackend.Lexer
    assert redact.parse_value is pybackend.parse_value


def _write_source(tmp_path, payload, name='archive.json'):
    path = tmp_path / name
    path.write_bytes(payload.encode('utf-8'))
    return path


# A document exercising: twin decoded keys (the second column is spelled with
# a unicode escape), nested supplier mappings, arrays and multibyte text.
SOURCE = (
    '{\n'
    '  "records": [\n'
    '    {"id": 1, "name": "Alpha", "\\u006eame": "Alpha-dup",\n'
    '     "vendor": {"tax_id": "X-001", "city": "Oldtown"}, "note": "中文"},\n'
    '    {"id": 2, "name": "Beta", "\\u006eame": "Beta-dup",\n'
    '     "vendor": {"tax_id": "X-002", "city": "Newtown"}}\n'
    '  ]\n'
    '}\n'
)

LITERAL_SHEET = {
    "sheet_id": "LIT",
    "requests": [
        {"request_id": "LIT-1", "array": "records", "index": 0, "key": "name",
         "old_value": "Alpha", "new_value": "Alpha-fixed"},
        {"request_id": "LIT-2", "array": "records", "index": 1, "key": "name",
         "key_literal": '"\\u006eame"',
         "old_value": "Beta-dup", "new_value": "Beta-dup-fixed"},
    ],
}

PATH_SHEET = {
    "sheet_id": "PATH",
    "requests": [
        {"request_id": "P-1", "path": "records.item.vendor.tax_id",
         "occurrence": 1, "old_value": "X-001", "new_value": "Y-100"},
    ],
}


def _by_id(report):
    return {item['request_id']: item for item in report.results}


@pytest.mark.parametrize("buf_size", [1, 7, 64, 4096])
def test_two_sheet_forms_apply_exactly(tmp_path, buf_size):
    source = _write_source(tmp_path, SOURCE)
    destination = tmp_path / 'out.json'
    report = redact.apply_corrections(
        source, destination, [LITERAL_SHEET, PATH_SHEET], buf_size=buf_size)

    text = destination.read_text(encoding='utf-8')
    corrected = json.loads(text)
    first = corrected['records'][0]
    assert first['vendor']['tax_id'] == 'Y-100'
    assert first['vendor']['city'] == 'Oldtown'
    assert first['note'] == '中文'
    # Twin decoded keys cannot be told apart via json.loads (last wins), so
    # assert against the raw literal spelling in the output.
    assert '"name": "Alpha-fixed"' in text
    assert '"\\u006eame": "Alpha-dup"' in text
    assert '"name": "Beta"' in text
    assert '"\\u006eame": "Beta-dup-fixed"' in text
    assert report.verified is True

    rows = _by_id(report)
    for request_id in ('LIT-1', 'LIT-2', 'P-1'):
        row = rows[request_id]
        assert row['status'] == 'accepted'
        assert row['verified'] is True
        assert row['old_value_expected'] is not None
        assert row['old_value_actual'] == row['old_value_expected']
        # Concrete source provenance + exact lookback range.
        start, end = row['byte_span']
        assert source.read_bytes()[start:end].decode('utf-8') \
            == row['old_raw_literal']
        assert any(s['request_id'] == request_id for s in row['sources'])


def test_literal_sheet_never_touches_the_decode_equal_twin(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    # Only the plain "name" column of record 0 is authorized.
    sheet = {"sheet_id": "LIT", "requests": [
        {"request_id": "only", "array": "records", "index": 0, "key": "name",
         "old_value": "Alpha", "new_value": "Alpha-fixed"}]}
    report = redact.apply_corrections(source, tmp_path / 'out.json', [sheet])
    text = (tmp_path / 'out.json').read_text(encoding='utf-8')
    assert '"name": "Alpha-fixed"' in text
    # The escape-spelled twin must be left byte-for-byte untouched.
    assert '"\\u006eame": "Alpha-dup"' in text
    assert 'Alpha-dup"' in text
    assert len(report.applied) == 1
    assert report.applied[0]['old_raw_literal'] == '"Alpha"'


def test_duplicate_consistent_requests_replace_once(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    duplicate = {
        "sheet_id": "DUP",
        "requests": [
            {"request_id": "D-1", "path": "records.item.name", "occurrence": 1,
             "old_value": "Alpha", "new_value": "Alpha-fixed"},
            {"request_id": "D-2", "path": "records.item.name", "occurrence": 1,
             "old_value": "Alpha", "new_value": "Alpha-fixed"},
        ],
    }
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [LITERAL_SHEET, duplicate])
    text = (tmp_path / 'out.json').read_text(encoding='utf-8')
    assert text.count('Alpha-fixed') == 1
    rows = _by_id(report)
    owner = rows['LIT-1']
    assert owner['status'] == 'accepted'
    assert rows['D-1']['composed_into'] == 'LIT-1'
    assert rows['D-2']['composed_into'] == 'LIT-1'
    source_ids = {s['request_id'] for s in owner['sources']}
    assert {'LIT-1', 'D-1', 'D-2'} <= source_ids
    # Every folded request inherits the owner's re-read verification.
    assert all(rows[rid]['verified'] for rid in ('D-1', 'D-2'))


def test_conflicting_requests_are_both_rejected(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    sheets = [
        {"sheet_id": "A", "requests": [
            {"request_id": "A-1", "path": "records.item.note", "occurrence": 1,
             "old_value": "中文", "new_value": "one"}]},
        {"sheet_id": "B", "requests": [
            {"request_id": "B-1", "path": "records.item.note", "occurrence": 1,
             "old_value": "中文", "new_value": "two"}]},
    ]
    report = redact.apply_corrections(source, tmp_path / 'out.json', sheets)
    rows = _by_id(report)
    assert rows['A-1']['status'] == rows['B-1']['status'] == 'rejected'
    assert rows['A-1']['rejection_reason'] == redact.CONFLICT
    corrected = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert corrected['records'][0]['note'] == '中文'


@pytest.mark.parametrize("reversed_sheets", [False, True])
def test_whole_object_vs_subitem_are_order_independent(tmp_path, reversed_sheets):
    payload = '{"records":[{"vendor":{"tax_id":"X-1","city":"Old"},"keep":1}]}'
    source = _write_source(tmp_path, payload)
    whole = {"sheet_id": "LEFT", "requests": [
        {"request_id": "L-1", "path": "records.item.vendor", "occurrence": 1,
         "old_value": {"tax_id": "X-1", "city": "Old"},
         "new_value": {"tax_id": "Z-9", "city": "Replaced"}}]}
    subitem = {"sheet_id": "RIGHT", "requests": [
        {"request_id": "R-1", "path": "records.item.vendor.city",
         "occurrence": 1, "old_value": "Old", "new_value": "Newcity"}]}
    sheets = [subitem, whole] if reversed_sheets else [whole, subitem]
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', sheets, buf_size=5)
    corrected = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert corrected['records'][0]['vendor'] == \
        {'tax_id': 'Z-9', 'city': 'Newcity'}
    rows = _by_id(report)
    assert rows['R-1']['composed_into'] == 'L-1'
    assert rows['L-1']['status'] == 'accepted'


def test_subitem_rejected_when_scope_disappears(tmp_path):
    payload = '{"records":[{"vendor":{"tax_id":"X-1","city":"Old"},"keep":1}]}'
    source = _write_source(tmp_path, payload)
    whole = {"sheet_id": "LEFT", "requests": [
        {"request_id": "L-1", "path": "records.item.vendor", "occurrence": 1,
         "old_value": {"tax_id": "X-1", "city": "Old"},
         "new_value": {"only": "new-shape"}}]}
    subitem = {"sheet_id": "RIGHT", "requests": [
        {"request_id": "R-1", "path": "records.item.vendor.city",
         "occurrence": 1, "old_value": "Old", "new_value": "Newcity"}]}
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [whole, subitem])
    corrected = json.loads((tmp_path / 'out.json').read_text(encoding='utf-8'))
    assert corrected['records'][0]['vendor'] == {'only': 'new-shape'}
    rows = _by_id(report)
    assert rows['L-1']['status'] == 'accepted'
    assert rows['R-1']['status'] == 'rejected'
    assert rows['R-1']['rejection_reason'] == \
        redact.SCOPE_NOT_VALID


def test_rejection_reasons(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    sheets = [{
        "sheet_id": "S",
        "requests": [
            {"request_id": "bad-old", "array": "records", "index": 0,
             "key": "id", "old_value": 999, "new_value": 7},
            {"request_id": "missing", "array": "records", "index": 0,
             "key": "ghost", "old_value": 1, "new_value": 2},
            {"request_id": "no-occurrence",
             "path": "records.item.vendor.tax_id", "occurrence": 99,
             "old_value": "X", "new_value": "Y"},
            {"request_id": "no-path", "path": "records.item.absent",
             "occurrence": 1, "old_value": 1, "new_value": 2},
        ],
    }]
    report = redact.apply_corrections(source, tmp_path / 'out.json', sheets)
    rows = _by_id(report)
    assert rows['bad-old']['rejection_reason'] == redact.OLD_VALUE_MISMATCH
    assert rows['missing']['rejection_reason'] == redact.TARGET_NOT_FOUND
    assert rows['no-occurrence']['rejection_reason'] == \
        redact.OCCURRENCE_OUT_OF_RANGE
    assert rows['no-path']['rejection_reason'] == redact.TARGET_NOT_FOUND
    # Nothing accepted -> the output is the original stream verbatim.
    assert (tmp_path / 'out.json').read_bytes() == source.read_bytes()


def test_unselected_bytes_pass_through_verbatim(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [PATH_SHEET], buf_size=3)
    original = source.read_bytes()
    corrected = (tmp_path / 'out.json').read_bytes()
    changed = _by_id(report)['P-1']
    start, end = changed['byte_span']
    # Everything outside the single replaced range is identical raw bytes.
    assert original[:start] == corrected[:start]
    assert original[end:] == corrected[len(corrected) - (len(original) - end):]
    assert corrected[start:start + len(b'"Y-100"')] == b'"Y-100"'


def test_unselected_containers_are_not_materialized(tmp_path, monkeypatch):
    payload = '{"records":[{"id":1,"blob":{"big":[' + \
        ','.join(str(i) for i in range(200)) + ']}}]}'
    source = _write_source(tmp_path, payload)
    builds = {'count': 0}
    real_init = ObjectBuilder.__init__

    def counting_init(self, *args, **kwargs):
        builds['count'] += 1
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(ObjectBuilder, '__init__', counting_init)
    sheet = {"sheet_id": "S", "requests": [
        {"request_id": "scalar", "array": "records", "index": 0, "key": "id",
         "old_value": 1, "new_value": 2}]}
    redact.apply_corrections(source, tmp_path / 'out.json', [sheet],
                             buf_size=16)
    # A scalar fix streams raw bytes through without building any object.
    assert builds['count'] == 0

    container_sheet = {"sheet_id": "S", "requests": [
        {"request_id": "container", "path": "records.item.blob",
         "occurrence": 1,
         "old_value": {"big": list(range(200))},
         "new_value": {"big": []}}]}
    redact.apply_corrections(source, tmp_path / 'out2.json',
                             [container_sheet], buf_size=16)
    # Exactly one builder per pass (resolve + re-read verify) for the single
    # targeted container; the surrounding record/root are never built even
    # though the targeted blob holds 200 elements.
    assert builds['count'] == 2


def test_corrupt_source_reports_position_and_keeps_original(tmp_path):
    raw = b'{"records":[{"ok":1},{"bad": BROKEN}]}'
    source = tmp_path / 'broken.json'
    source.write_bytes(raw)
    destination = tmp_path / 'never.json'
    with pytest.raises(redact.RedactionError) as excinfo:
        redact.apply_corrections(source, destination, [PATH_SHEET],
                                 buf_size=4)
    message = str(excinfo.value)
    assert 'byte' in message
    # The actual error position of the invalid token is preserved.
    assert str(raw.index(b'BROKEN')) in message
    assert source.read_bytes() == raw
    assert not destination.exists()


def test_source_is_never_overwritten(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    with pytest.raises(redact.RedactionError):
        redact.apply_corrections(source, source, [PATH_SHEET])
    assert source.read_bytes().decode('utf-8') == SOURCE


def test_accepted_changes_recheck_by_occurrence_on_reread(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [LITERAL_SHEET, PATH_SHEET])
    import ijson
    values = list(ijson.items(
        open(str(tmp_path / 'out.json'), 'rb'),
        'records.item.vendor.tax_id'))
    assert values[0] == 'Y-100'
    for row in report.applied:
        assert row['occurrence'] >= 1
        assert row['verified'] is True


def test_byte_spans_are_exact_across_buffer_sizes():
    import random
    import string
    from ijson.redact import _EventEngine

    random.seed(7)

    def random_text():
        return ''.join(random.choice('abcé中🎉' + string.digits)
                       for _ in range(random.randint(0, 12)))

    document = {"records": [
        {"id": i, "name": random_text(),
         "meta": {"k": random_text(), "n": [1, 2, 3]}, "esc": random_text()}
        for i in range(20)]}
    raw = json.dumps(document, ensure_ascii=False, indent=1).encode('utf-8')

    for buf_size in (1, 2, 3, 5, 1000):
        engine = _EventEngine()
        position = 0
        events = []
        while position < len(raw):
            events.extend(engine.push(raw[position:position + buf_size]))
            position += buf_size
        events.extend(engine.finish())
        assert events
        for _event, _value, _cs, _ce, byte_start, byte_end, symbol in events:
            assert raw[byte_start:byte_end].decode('utf-8') == symbol
        assert events[-1][5] == len(raw)


def test_root_level_array_path_locator(tmp_path):
    payload = '[true, false, null, 1, 1.5, "x"]'
    source = _write_source(tmp_path, payload, name='root.json')
    sheet = {"sheet_id": "R", "requests": [
        {"request_id": "bool", "path": "item", "occurrence": 1,
         "old_value": True, "new_value": False},
        {"request_id": "float", "path": "item", "occurrence": 5,
         "old_value": 1.5, "new_value": 2.5},
        # 1 (int) must not be confused with true (bool).
        {"request_id": "type-guard", "path": "item", "occurrence": 4,
         "old_value": True, "new_value": 9},
    ]}
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [sheet], buf_size=3)
    rows = _by_id(report)
    assert rows['bool']['status'] == 'accepted'
    assert rows['float']['status'] == 'accepted'
    assert rows['type-guard']['rejection_reason'] == \
        redact.OLD_VALUE_MISMATCH
    assert json.loads((tmp_path / 'out.json').read_text()) == \
        [False, False, None, 1, 2.5, "x"]


def test_decimal_and_float_old_values_match(tmp_path):
    payload = '{"records":[{"fraction": 0.1, "whole": 1000, "flag": true}]}'
    source = _write_source(tmp_path, payload, name='num.json')
    sheet = {"sheet_id": "N", "requests": [
        {"request_id": "fraction", "array": "records", "index": 0,
         "key": "fraction", "old_value": 0.1, "new_value": 0.2},
        {"request_id": "whole", "array": "records", "index": 0,
         "key": "whole", "old_value": 1000, "new_value": 2000},
        # bool true must not match an integer-style comparison either.
        {"request_id": "flag-vs-one", "array": "records", "index": 0,
         "key": "flag", "old_value": 1, "new_value": False},
    ]}
    report = redact.apply_corrections(
        source, tmp_path / 'out.json', [sheet])
    rows = _by_id(report)
    assert rows['fraction']['status'] == 'accepted'
    assert rows['whole']['status'] == 'accepted'
    assert rows['flag-vs-one']['rejection_reason'] == \
        redact.OLD_VALUE_MISMATCH
    corrected = json.loads((tmp_path / 'out.json').read_text())
    assert corrected['records'][0]['fraction'] == 0.2
    assert corrected['records'][0]['whole'] == 2000


def test_cli_end_to_end(tmp_path):
    source = _write_source(tmp_path, SOURCE)
    destination = tmp_path / 'cli.json'
    sheet_path = tmp_path / 'sheet.json'
    sheet_path.write_text(json.dumps(PATH_SHEET), encoding='utf-8')
    import os
    report_path = tmp_path / 'report.json'
    env = dict(os.environ)
    env['IJSON_BACKEND'] = 'python'
    result = subprocess.run(
        [sys.executable, '-m', 'ijson.redact', str(source), str(destination),
         '--sheet', str(sheet_path), '--report', str(report_path),
         '--buf-size', '5'],
        capture_output=True, text=True, env=env, check=True)
    assert 'applied=1' in result.stdout
    assert json.loads(destination.read_text(encoding='utf-8'))['records'][0][
        'vendor']['tax_id'] == 'Y-100'
    report_doc = json.loads(report_path.read_text(encoding='utf-8'))
    assert report_doc['verified'] is True
