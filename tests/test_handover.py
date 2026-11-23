from ctypes import create_string_buffer
from io import BytesIO

import pytest

from handover import (
    Pick, build_package, resolve, scan_stream, verify_package,
    write_hardlink_entry, main as handover_main,
)
from libarchive.write import memory_writer


class OneWayStream:
    """A read-once, non-seekable stream, like the handover medium."""

    def __init__(self, data):
        self._bio = BytesIO(data)

    def readinto(self, buf):
        return self._bio.readinto(buf)

    def seekable(self):
        return False


def make_tar(spec):
    """Build a source tar in memory.

    `spec` items are `('file', name, body)` or `('link', name, target)`.
    """
    buf = create_string_buffer(8 * 1024 * 1024)
    with memory_writer(buf, 'ustar') as archive:
        for item in spec:
            if item[0] == 'file':
                _, name, body = item
                archive.add_file_from_memory(
                    name, len(body), body, mtime=1700000000,
                )
            else:
                _, name, target = item
                write_hardlink_entry(archive, name, target)
    return buf.raw


def make_package(spec, picks, buffer_limit=1 << 20):
    """Scan once, build the package, return everything for assertions."""
    scan = scan_stream(
        OneWayStream(make_tar(spec)), picks, buffer_limit=buffer_limit,
    )
    out = create_string_buffer(8 * 1024 * 1024)
    with memory_writer(out, 'ustar') as archive:
        deliveries, failures = build_package(scan, picks, archive)
    problems = verify_package(scan, deliveries, buf=out)
    return scan, deliveries, failures, problems


def read_package(buf):
    from libarchive.read import memory_reader
    entries = []
    with memory_reader(create_string_buffer(buf, len(buf))) as archive:
        for entry in archive:
            entries.append({
                'pathname': entry.pathname,
                'is_link': entry.islnk,
                'linkpath': entry.linkpath if entry.islnk else None,
                'is_regular': entry.isreg and not entry.islnk,
                'content': (
                    b''.join(entry.get_blocks())
                    if entry.isreg and not entry.islnk else None
                ),
            })
    return entries


def test_picked_link_gets_the_first_body_not_the_rewrite():
    spec = [
        ('file', 'a', b'first'),
        ('link', 'b', 'a'),
        ('file', 'a', b'second'),
    ]
    scan, deliveries, failures, problems = make_package(spec, [Pick('b', 1)])
    assert failures == []
    assert problems == []
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.kind == 'file'
    assert delivery.out_path == 'b'
    assert delivery.source.label == 'a#1'
    assert [r.label for r in delivery.chain] == ['b#1', 'a#1']
    assert scan.body_of(delivery.source) == b'first'


def test_chain_reaches_first_occurrence_through_earlier_alias():
    spec = [
        ('file', 'a', b'first'),
        ('link', 'alias', 'a'),
        ('link', 'b', 'alias'),
        ('file', 'a', b'second'),
    ]
    scan, deliveries, failures, problems = make_package(spec, [Pick('b', 1)])
    assert failures == []
    assert problems == []
    assert deliveries[0].source.label == 'a#1'
    assert [r.label for r in deliveries[0].chain] == ['b#1', 'alias#1', 'a#1']
    assert scan.body_of(deliveries[0].source) == b'first'


def test_shared_body_is_repackaged_as_a_hardlink():
    spec = [('file', 'a', b'shared'), ('link', 'b', 'a')]
    picks = [Pick('a', 1), Pick('b', 1)]
    scan, deliveries, failures, problems = make_package(spec, picks)
    assert failures == []
    assert problems == []
    assert [d.kind for d in deliveries] == ['file', 'link']
    assert deliveries[0].source is deliveries[1].source


def test_pick_order_chooses_the_carrier_of_a_shared_body():
    spec = [('file', 'a', b'shared'), ('link', 'b', 'a')]
    picks = [Pick('b', 1), Pick('a', 1)]
    scan, deliveries, failures, problems = make_package(spec, picks)
    assert failures == []
    assert problems == []
    assert [d.kind for d in deliveries] == ['file', 'link']
    assert deliveries[0].out_path == 'b'
    assert deliveries[1].link_target == 'b'


def test_independent_identical_bodies_stay_independent():
    spec = [('file', 'a', b'same'), ('file', 'b', b'same')]
    picks = [Pick('a', 1), Pick('b', 1)]
    scan, deliveries, failures, problems = make_package(spec, picks)
    assert failures == []
    assert problems == []
    assert [d.kind for d in deliveries] == ['file', 'file']
    assert deliveries[0].source is not deliveries[1].source


def test_link_before_its_target_uses_the_first_later_occurrence():
    spec = [
        ('link', 'b', 'a'),
        ('file', 'a', b'late'),
        ('file', 'a', b'later'),
    ]
    scan, deliveries, failures, problems = make_package(spec, [Pick('b', 1)])
    assert failures == []
    assert problems == []
    assert deliveries[0].source.label == 'a#1'
    assert scan.body_of(deliveries[0].source) == b'late'


def test_unrelated_cycles_and_dangling_links_do_not_block_delivery():
    spec = [
        ('link', 'x', 'y'),
        ('link', 'y', 'x'),
        ('link', 'd', 'ghost'),
        ('file', 'a', b'keep'),
    ]
    scan, deliveries, failures, problems = make_package(spec, [Pick('a', 1)])
    assert failures == []
    assert problems == []
    assert scan.body_of(deliveries[0].source) == b'keep'


def test_picked_dangling_link_fails_with_a_trace():
    spec = [('link', 'd', 'ghost'), ('file', 'a', b'keep')]
    picks = [Pick('d', 1), Pick('a', 1)]
    scan, deliveries, failures, problems = make_package(spec, picks)
    assert problems == []
    assert [d.pick.label for d in deliveries] == ['a#1']
    assert len(failures) == 1
    failure = failures[0]
    assert failure.reason == 'dangling'
    assert failure.pick.label == 'd#1'
    assert "'ghost'" in failure.describe()


def test_picked_cycle_fails_with_a_trace():
    spec = [('link', 'x', 'y'), ('link', 'y', 'x')]
    scan, deliveries, failures, problems = make_package(spec, [Pick('x', 1)])
    assert deliveries == []
    assert len(failures) == 1
    failure = failures[0]
    assert failure.reason == 'cycle'
    assert [r.label for r in failure.chain] == ['x#1', 'y#1', 'x#1']


def test_second_occurrence_can_be_picked():
    spec = [('file', 'a', b'first'), ('file', 'a', b'second')]
    scan, deliveries, failures, problems = make_package(spec, [Pick('a', 2)])
    assert failures == []
    assert problems == []
    assert deliveries[0].source.label == 'a#2'
    assert scan.body_of(deliveries[0].source) == b'second'


def test_missing_occurrence_is_reported():
    spec = [('file', 'a', b'only')]
    scan, deliveries, failures, problems = make_package(spec, [Pick('a', 3)])
    assert deliveries == []
    assert failures[0].reason == 'missing'


def test_evicted_body_failure_names_member_chain_and_occurrence():
    spec = [
        ('file', 'a', b'A' * 100),
        ('file', 'filler1', b'F' * 100),
        ('file', 'filler2', b'F' * 100),
        ('link', 'b', 'a'),
    ]
    scan, deliveries, failures, problems = make_package(
        spec, [Pick('b', 1)], buffer_limit=150,
    )
    assert deliveries == []
    assert len(failures) == 1
    failure = failures[0]
    assert failure.reason == 'body-lost'
    description = failure.describe()
    assert 'b#1 -> a#1' in description
    assert 'a#1' in description
    assert any(e.label == 'a#1' for e in scan.evicted)


def test_staging_budget_prefers_dropping_unneeded_bodies():
    spec = [
        ('file', 'a', b'A' * 100),
        ('file', 'filler', b'F' * 100),
        ('link', 'b', 'a'),
    ]
    picks = [Pick('a', 1), Pick('b', 1)]
    scan, deliveries, failures, problems = make_package(
        spec, picks, buffer_limit=150,
    )
    assert failures == []
    assert problems == []
    assert [d.kind for d in deliveries] == ['file', 'link']
    assert [e.label for e in scan.evicted] == ['filler#1']


def test_verification_detects_a_corrupted_package():
    spec = [('file', 'a', b'first')]
    scan = scan_stream(OneWayStream(make_tar(spec)), [Pick('a', 1)])
    out = create_string_buffer(8 * 1024 * 1024)
    with memory_writer(out, 'ustar') as archive:
        deliveries, failures = build_package(scan, [Pick('a', 1)], archive)
    assert verify_package(scan, deliveries, buf=out) == []
    corrupted = bytearray(out.raw)
    pos = corrupted.find(b'first')
    corrupted[pos] = ord('F')
    out2 = create_string_buffer(bytes(corrupted), len(corrupted))
    problems = verify_package(scan, deliveries, buf=out2)
    assert len(problems) == 1
    assert 'content differs' in problems[0]


def test_resolve_ignores_unpicked_entries():
    spec = [
        ('file', 'a', b'first'),
        ('link', 'b', 'a'),
        ('file', 'a', b'second'),
    ]
    scan = scan_stream(OneWayStream(make_tar(spec)), [Pick('b', 1)])
    record = scan.find('b', 1)
    source, chain = resolve(scan, record)
    assert source.label == 'a#1'


def test_cli_delivers_and_reports(tmp_path, capsys):
    src = tmp_path / 'src.tar'
    src.write_bytes(make_tar([
        ('file', 'a', b'first'),
        ('link', 'b', 'a'),
        ('file', 'a', b'second'),
    ]))
    out = tmp_path / 'out.tar'
    rc = handover_main([
        str(src), str(out), '--pick', 'b#1', '--format', 'ustar',
    ])
    assert rc == 0
    entries = read_package(out.read_bytes())
    assert [e['pathname'] for e in entries] == ['b']
    assert entries[0]['content'] == b'first'


def test_cli_reports_failures(tmp_path, capsys):
    src = tmp_path / 'src.tar'
    src.write_bytes(make_tar([('link', 'd', 'ghost')]))
    out = tmp_path / 'out.tar'
    rc = handover_main([str(src), str(out), '--pick', 'd#1'])
    assert rc == 1
    captured = capsys.readouterr()
    assert 'FAILED d#1' in captured.out
    assert 'ghost' in captured.out


def test_pick_parse():
    assert Pick.parse('a') == Pick('a', 1)
    assert Pick.parse('a#2') == Pick('a', 2)
    assert Pick.parse('dir/a#b') == Pick('dir/a#b', 1)
    with pytest.raises(ValueError):
        Pick('a', 0)
