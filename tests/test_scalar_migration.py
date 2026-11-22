import datetime

import pytest

from migration import (LEGACY_RULE, MODERN_RULE, get_loader, load, scan,
                       plan_rewrite, quote_rewrite, find_merge_conflicts)
from migration.cli import main


def test_rule_specific_scalar_readings():
    text = "a: on\nb: 01\nc: 013\nd: 2021-05-01T08:30:00+08:00\n"
    legacy = load(text, LEGACY_RULE)
    modern = load(text, MODERN_RULE)
    assert legacy['a'] is True
    assert modern['a'] == 'on'
    assert legacy['b'] == 1 and modern['b'] == 1
    assert legacy['c'] == 11
    assert modern['c'] == 13
    assert isinstance(legacy['d'], datetime.datetime)
    assert legacy['d'].utcoffset() == datetime.timedelta(hours=8)
    assert modern['d'] == '2021-05-01T08:30:00+08:00'


def test_modern_rule_core_schema_basics():
    assert load("x: yes", MODERN_RULE) == {'x': 'yes'}
    assert load("x: true", MODERN_RULE) == {'x': True}
    assert load("x: 0o17", MODERN_RULE) == {'x': 15}
    assert load("x: 1:30", MODERN_RULE) == {'x': '1:30'}
    assert isinstance(load("x: 2020-01-01", MODERN_RULE)['x'], str)


def test_explicit_tags_read_identically_under_both_rules():
    text = ("a: !!bool on\nb: !!int 01\n"
            "c: !!timestamp 2021-05-01T08:30:00+08:00\n")
    assert load(text, LEGACY_RULE) == load(text, MODERN_RULE)


def test_scan_reports_confirmable_lines_and_readings():
    text = "a: on\nb: 013\n"
    rows, conflicts = scan(text)
    assert not conflicts
    by_line = {row.line: row for row in rows}
    on_row = by_line[1]
    assert on_row.kind == 'value'
    assert on_row.legacy_value is True
    assert on_row.modern_value == 'on'
    assert on_row.action == 'tag' and on_row.replacement == '!!bool on'
    octal_row = by_line[2]
    assert octal_row.legacy_value == 11 and octal_row.modern_value == 13
    assert octal_row.action == 'literal' and octal_row.replacement == '11'


def test_legacy_preserve_rewrite_reads_identically_under_both_rules():
    text = "active: on\nsince: 2021-05-02T10:00:00Z\ncode: 013\n"
    rewritten, rows = plan_rewrite(text, preserve=LEGACY_RULE)
    expected = load(text, LEGACY_RULE)
    assert load(rewritten, LEGACY_RULE) == expected
    assert load(rewritten, MODERN_RULE) == expected
    assert '!!bool on' in rewritten
    assert '!!timestamp' in rewritten
    assert '11' in rewritten


def test_modern_preserve_rewrite_reads_identically_under_both_rules():
    text = "active: on\nsince: 2021-05-02T10:00:00Z\ncode: 013\n"
    rewritten, _ = plan_rewrite(text, preserve=MODERN_RULE)
    expected = load(text, MODERN_RULE)
    assert load(rewritten, LEGACY_RULE) == expected
    assert load(rewritten, MODERN_RULE) == expected
    assert rewritten.splitlines()[0] == 'active: "on"'
    assert rewritten.splitlines()[2] == 'code: 13'


def test_quote_rewrite_only_quotes_string_preservations():
    text = "active: on\ncode: 013\n"
    rewritten, _ = quote_rewrite(text, preserve=MODERN_RULE)
    assert rewritten.splitlines()[0] == 'active: "on"'
    assert rewritten.splitlines()[1] == 'code: 13'
    expected = load(text, MODERN_RULE)
    assert load(rewritten, LEGACY_RULE) == expected
    assert load(rewritten, MODERN_RULE) == expected


def test_anchor_merge_key_collision_is_conflict_and_blocks_rewrite():
    text = (
        "defaults: &defaults\n"
        "  on: 1\n"
        '  "on": 2\n'
        "profile:\n"
        "  <<: *defaults\n"
        "  on: 3\n")
    conflicts = find_merge_conflicts(text)
    assert conflicts
    lines = {entry[0] for conflict in conflicts
             for entry in conflict.key_groups}
    assert lines == {2, 3, 6}
    rows, _ = scan(text)
    assert all(row.blocked for row in rows)
    with pytest.raises(ValueError):
        plan_rewrite(text)


def test_benign_merge_has_no_conflict():
    text = ("base: &base\n  retries: 01\n"
            "profile:\n  <<: *base\n  active: on\n")
    conflicts = find_merge_conflicts(text)
    assert not conflicts


def test_unknown_rule_rejected():
    with pytest.raises(ValueError):
        get_loader('y2k')


def test_cli_interleaves_batches_with_their_own_rules(tmp_path):
    old_dir = tmp_path / 'old'
    new_dir = tmp_path / 'new'
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / 'ok.yaml').write_text("a: on\nwhen: 2020-01-01T00:00:00Z\n")
    (old_dir / 'conflict.yaml').write_text(
        "d: &d\n  on: 1\n  \"on\": 2\np:\n  <<: *d\n  on: 3\n")
    (new_dir / 'acct.yaml').write_text("flag: off\ncode: 013\n")
    out_dir = tmp_path / 'out'
    report = tmp_path / 'report.json'
    exit_code = main([
        '--batch', '%s=%s' % (old_dir, LEGACY_RULE),
        '--batch', '%s=%s' % (new_dir, MODERN_RULE),
        '--out-dir', str(out_dir),
        '--report', str(report)])
    assert exit_code == 1
    rewritten = (out_dir / 'ok.yaml').read_text()
    assert '!!bool on' in rewritten and '!!timestamp' in rewritten
    acct = (out_dir / 'acct.yaml').read_text()
    assert 'flag: "off"' in acct and 'code: 13' in acct
    assert not (out_dir / 'conflict.yaml').exists()
    import json
    data = json.loads(report.read_text())
    statuses = {item['status']
                for batch in data for item in batch['files']}
    assert statuses == {'conflict', 'rewritten'}
