# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

from datetime import datetime
from collections import OrderedDict
import threading

import pytest

from tableschema import Table, WindowedTable, WindowParseError, exceptions
from tableschema.windows import _SpooledQueue


# Fixtures

SCHEMA = {
    'fields': [
        {'name': 'device_id', 'type': 'string',
         'constraints': {'unique': True}},
        {'name': 'ts', 'type': 'string'},
        {'name': 'warehouse_id', 'type': 'string'},
        {'name': 'value', 'type': 'integer'},
    ],
    'foreignKeys': [
        {
            'fields': 'warehouse_id',
            'reference': {'resource': 'warehouses', 'fields': 'id'},
        },
    ],
}

RELATIONS = {
    'warehouses': [
        {'id': 'W1'},
        {'id': 'W2'},
    ],
}

BOUND = '2026-01-01T01:00:00'

WINDOWS = [
    {'name': 'first', 'start': '2026-01-01T00:00:00', 'end': BOUND},
    {'name': 'second', 'start': BOUND, 'end': '2026-01-02T00:00:00'},
]


def make_source():
    # Two rows land exactly on the boundary (different offsets, same instant)
    # followed by many late rows of the first window before the second window
    # gets its next record.
    return [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:00:00', 'W1', 1],   # row 2: first window
        ['d2', BOUND, 'W2', 2],                   # row 3: boundary -> second
        ['d3', '2026-01-01T00:10:00', 'W1', 3],   # row 4: late first
        ['d4', '2026-01-01T00:20:00', 'W1', 4],   # row 5: late first
        ['d5', '2026-01-01T00:30:00', 'W1', 5],   # row 6: late first
        ['d6', '2026-01-01T02:00:00', 'W2', 6],   # row 7: second's next
    ]


def make_table(**options):
    options.setdefault('schema', SCHEMA)
    options.setdefault('buffer_size', 1)
    return WindowedTable(
        make_source(), time_field='ts', windows=WINDOWS, **options)


# Window membership

def test_window_is_left_closed_right_open():
    table = make_table()
    first = table.window('first')
    second = table.window('second')
    boundary = datetime(2026, 1, 1, 1, 0, 0)
    assert first.contains(datetime(2026, 1, 1, 0, 0, 0)) is True
    assert first.contains(boundary) is False
    assert second.contains(boundary) is True
    assert second.contains(datetime(2026, 1, 1, 2, 0, 0)) is True
    table.close()


def test_boundary_rows_are_only_seen_by_the_later_window():
    table = make_table()
    first_rows = table.window('first').read(keyed=True)
    second_rows = table.window('second').read(keyed=True)
    assert [row['device_id'] for row in first_rows] == ['d1', 'd3', 'd4', 'd5']
    assert [row['device_id'] for row in second_rows] == ['d2', 'd6']
    table.close()


def test_boundary_rows_keep_their_source_order_per_window():
    table = make_table()
    first_numbers = [
        row[0] for row in table.window('first').iter(extended=True)]
    second_numbers = [
        row[0] for row in table.window('second').iter(extended=True)]
    assert first_numbers == [2, 4, 5, 6]
    assert second_numbers == [3, 7]
    table.close()


def test_two_rows_on_the_same_boundary_instant_go_only_to_later_window():
    # Two rows with different offsets but exactly at the front window's upper
    # bound represent the same instant and both belong to the later window.
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:30:00', 'W1', 1],
        ['d2', BOUND, 'W2', 2],                  # naive, at upper bound
        ['d3', '2026-01-01T01:00:00Z', 'W2', 3],  # same instant, explicit tz
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, timezone='UTC', buffer_size=1)
    first = table.window('first').read(keyed=True)
    second = table.window('second').read(keyed=True)
    assert [row['device_id'] for row in first] == ['d1']
    assert [row['device_id'] for row in second] == ['d2', 'd3']
    table.close()


# Interleaved consumption of the single stream

def test_windows_pull_interleaved_from_one_single_stream_read():
    read_rows = []

    def source():
        for row in make_source():
            read_rows.append(row)
            yield row

    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    first = table.window('first')
    second = table.window('second')

    first_iter = first.iter(extended=True)
    second_iter = second.iter(extended=True)

    # The first window stops after its very first record.
    assert next(first_iter)[2][0] == 'd1'
    # The second window reads fast, reaching the boundary and beyond while
    # the first window is parked on its late rows.
    assert next(second_iter)[2][0] == 'd2'
    assert next(second_iter)[2][0] == 'd6'
    # The first window resumes and receives every late row in source order.
    assert [row[2][0] for row in first_iter] == ['d3', 'd4', 'd5']
    assert list(second_iter) == []

    # The source stream has been consumed exactly once.
    assert read_rows == make_source()
    table.close()


def test_slow_window_does_not_block_a_faster_window():
    table = make_table(buffer_size=1)
    first = table.window('first')
    second = table.window('second')
    second_iter = second.iter(extended=True)
    # The second window drains fully although the first never pulls and its
    # late rows overflow the tiny in-memory buffer onto the spool file.
    assert [row[2][0] for row in second_iter] == ['d2', 'd6']
    assert [row[2][0] for row in first.iter(extended=True)] == \
        ['d1', 'd3', 'd4', 'd5']
    table.close()


# Bounded memory

def test_spooled_queue_keeps_memory_bounded_and_preserves_order():
    queue = _SpooledQueue(2)
    for item in range(5):
        queue.put(item)
        assert len(queue) <= 2 + queue.on_disk
    assert queue.spool_file_open is True
    assert queue.on_disk == 3
    assert [queue.get() for _ in range(5)] == [0, 1, 2, 3, 4]
    assert not queue
    assert queue.spool_file_open is False


def test_spooled_queue_clear_releases_the_spool_file():
    queue = _SpooledQueue(1)
    for item in range(4):
        queue.put(item)
    queue.clear()
    assert not queue
    assert queue.spool_file_open is False


# Per-window integrity

def test_unique_and_foreign_keys_are_checked_within_each_window():
    table = make_table()
    first_errors = []
    second_errors = []

    def handler(store):
        def collect(exc, row_number=None, row_data=None, error_data=None):
            store.append((exc.__class__.__name__, row_number))
        return collect

    first_rows = table.window('first').read(
        relations=RELATIONS, exc_handler=handler(first_errors))
    second_rows = table.window('second').read(
        relations=RELATIONS, exc_handler=handler(second_errors))

    assert first_rows == [
        ['d1', '2026-01-01T00:00:00', {'id': 'W1'}, 1],
        ['d3', '2026-01-01T00:10:00', {'id': 'W1'}, 3],
        ['d4', '2026-01-01T00:20:00', {'id': 'W1'}, 4],
        ['d5', '2026-01-01T00:30:00', {'id': 'W1'}, 5],
    ]
    assert second_rows == [
        ['d2', BOUND, {'id': 'W2'}, 2],
        ['d6', '2026-01-01T02:00:00', {'id': 'W2'}, 6],
    ]
    assert first_errors == []
    assert second_errors == []
    table.close()


def test_same_device_in_two_windows_is_not_a_duplicate():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:30:00', 'W1', 1],
        ['d1', BOUND, 'W2', 2],
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    errors = []
    table.window('first').read(exc_handler=lambda *a, **k: errors.append(a))
    table.window('second').read(exc_handler=lambda *a, **k: errors.append(a))
    assert errors == []
    table.close()


def test_duplicate_device_fails_only_inside_its_own_window():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:10:00', 'W1', 1],
        ['d1', '2026-01-01T00:20:00', 'W1', 2],
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    errors = []
    table.window('first').read(
        exc_handler=lambda exc, **kw: errors.append(type(exc).__name__))
    assert errors == ['UniqueKeyError']
    table.close()


def test_unknown_warehouse_in_one_window_does_not_invalidate_the_other():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:20:00', 'W9', 1],   # late first, unknown wh
        ['d2', BOUND, 'W2', 2],
        ['d3', '2026-01-01T00:30:00', 'W1', 3],   # late first, known wh
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    first_errors = []
    second_errors = []
    first_rows = table.window('first').read(
        relations=RELATIONS,
        exc_handler=lambda exc, **kw: first_errors.append(type(exc).__name__))
    second_rows = table.window('second').read(
        relations=RELATIONS,
        exc_handler=lambda exc, **kw: second_errors.append(type(exc).__name__))
    assert first_errors == ['UnresolvedFKError']
    assert second_errors == []
    assert [row[0] for row in first_rows] == ['d1', 'd3']
    assert [row[0] for row in second_rows] == ['d2']
    table.close()


# Unparseable timestamps are tracked, not dropped

def test_unparseable_and_empty_timestamps_are_tracked_with_raw_data():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:10:00', 'W1', 1],
        ['d2', 'not-a-time', 'W1', 2],
        ['d3', '', 'W1', 3],
        ['d4', BOUND, 'W2', 4],
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    first_rows = table.window('first').read(keyed=True)
    second_rows = table.window('second').read(keyed=True)

    assert [row['device_id'] for row in first_rows] == ['d1']
    assert [row['device_id'] for row in second_rows] == ['d4']

    for window in (table.window('first'), table.window('second')):
        parse_errors = window.parse_errors
        assert len(parse_errors) == 2
        assert all(isinstance(error, WindowParseError)
                   for error in parse_errors)
        bad_time, empty_time = parse_errors
        # Raw values and source line numbers are preserved.
        assert bad_time.row_number == 3
        assert bad_time.row_data == OrderedDict([
            ('device_id', 'd2'), ('ts', 'not-a-time'),
            ('warehouse_id', 'W1'), ('value', 2)])
        assert isinstance(bad_time.reason, Exception)
        assert empty_time.row_number == 4
        assert empty_time.row_data['ts'] == ''
    table.close()


# Timezone handling

def test_timezone_less_rows_are_tracked_with_raw_data_and_line_numbers():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:10:00', 'W1', 1],   # naive, first window
        ['d2', BOUND, 'W2', 2],                    # naive, second window
        ['d3', '2026-01-01T00:20:00Z', 'W1', 3],   # aware, first window
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    first = table.window('first')
    second = table.window('second')
    first.read()
    second.read()

    first_naive = first.naive_rows
    second_naive = second.naive_rows
    assert len(first_naive) == 1
    row_number, keyed_row = first_naive[0]
    assert row_number == 2
    assert keyed_row['device_id'] == 'd1'
    assert keyed_row['ts'] == '2026-01-01T00:10:00'
    assert len(second_naive) == 1
    assert second_naive[0][0] == 3
    # The aware record is not listed as timezone-less.
    assert all(row[1]['device_id'] != 'd3' for row in first_naive)
    table.close()


def test_configured_timezone_clears_naive_row_tracking():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        ['d1', '2026-01-01T00:10:00', 'W1', 1],
        ['d2', BOUND, 'W2', 2],
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, timezone='UTC', buffer_size=1)
    first = table.window('first')
    second = table.window('second')
    first.read()
    second.read()
    assert first.naive_rows == []
    assert second.naive_rows == []
    table.close()


def test_mixed_naive_bounds_and_aware_records_do_not_crash_routing():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        # Aware records against naive window bounds, with no default timezone.
        ['d1', '2026-01-01T02:30:00+02:00', 'W1', 1],
        ['d2', '2026-01-01T02:00:00+02:00', 'W2', 2],
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    # UTC-normalized: 00:30 and 00:00 on 2026-01-01 land in the first window.
    assert [row[0] for row in table.window('first').read()] == ['d1', 'd2']
    assert table.window('second').read() == []
    table.close()


def test_timezone_is_applied_to_naive_timestamps_and_bounds():
    source = [
        ['device_id', 'ts', 'warehouse_id', 'value'],
        # Wall clock 02:30 with +02:00 is 00:30 UTC, i.e. inside the first
        # window only when the offset is actually honored.
        ['d1', '2026-01-01T02:30:00+02:00', 'W1', 1],
        ['d2', '2026-01-01T01:00:00', 'W2', 2],        # naive -> UTC boundary
    ]
    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, timezone='UTC', buffer_size=1)
    assert [row[0] for row in table.window('first').read()] == ['d1']
    assert [row[0] for row in table.window('second').read()] == ['d2']
    table.close()


# Independent window lifecycle

def test_closing_one_window_lets_the_other_finish():
    table = make_table()
    first = table.window('first')
    second = table.window('second')
    second_iter = second.iter(extended=True)
    assert next(second_iter)[2][0] == 'd2'
    second.close()
    assert second.closed is True
    assert first.closed is False
    with pytest.raises(exceptions.TableSchemaException):
        second.read()
    # The first window still receives every late row in order.
    assert [row[2][0] for row in first.iter(extended=True)] == \
        ['d1', 'd3', 'd4', 'd5']
    table.close()


def test_window_context_manager_closes_only_that_window():
    table = make_table()
    with table.window('second') as second:
        second.close()
    assert second.closed is True
    assert table.window('first').closed is False
    table.close()


def test_closing_all_windows_stops_pulling_the_source():

    rows = make_source()
    gate = threading.Event()
    pulled = []

    def source():
        for index, row in enumerate(rows):
            # Park right after the boundary row, before any late row is
            # pulled, so closing the windows can still stop the producer.
            if index >= 3:
                gate.wait(5)
            pulled.append(row)
            yield row

    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1, sample_size=1)
    assert next(table.window('second').iter(extended=True))[2][0] == 'd2'
    table.close()
    gate.set()
    assert table.join(5) is True
    # The producer never reaches the second window's own next record.
    assert len(pulled) < len(rows)
    assert all(row[0] != 'd6' for row in pulled[1:])


def test_closing_a_window_releases_its_spool_file():

    rows = make_source()
    gate = threading.Event()

    def source():
        for index, row in enumerate(rows):
            # Park after all late first-window rows, just before the second
            # window's own next record (d6 at index 6).
            if index >= 6:
                gate.wait(5)
            yield row

    table = WindowedTable(
        source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1, sample_size=1)
    first = table.window('first')
    second_iter = table.window('second').iter(extended=True)
    assert next(second_iter)[2][0] == 'd2'
    assert first.spooled_rows >= 1
    first.close()
    assert first.spooled_rows == 0
    gate.set()
    table.join(5)


# Producer failures surface on the windows

def test_producer_error_is_raised_by_window_consumers():

    def broken_source():
        yield ['device_id', 'ts', 'warehouse_id', 'value']
        yield ['d1', '2026-01-01T00:10:00', 'W1', 1]
        raise RuntimeError('stream broke')

    table = WindowedTable(
        broken_source, time_field='ts', windows=WINDOWS,
        schema=SCHEMA, buffer_size=1)
    with pytest.raises(Exception) as excinfo:
        table.window('first').read()
    assert 'stream broke' in str(excinfo.value)
    table.close()


# Backwards compatibility

def test_windowed_table_without_windows_behaves_like_plain_table():
    table = WindowedTable(
        make_source(), time_field='ts', windows=[],
        schema=SCHEMA, buffer_size=1)
    rows = table.read()
    plain = Table(make_source(), schema=SCHEMA).read()
    assert rows == plain


def test_windowed_table_iteration_requires_windows():
    table = make_table()
    with pytest.raises(exceptions.TableSchemaException):
        table.read()
    table.close()


def test_run_starts_the_producer_and_joins_cleanly():
    table = make_table()
    windows = table.run()
    assert list(windows) == ['first', 'second']
    assert [row[0] for row in table.window('first').read()] == \
        ['d1', 'd3', 'd4', 'd5']
    assert [row[0] for row in table.window('second').read()] == ['d2', 'd6']
    assert table.join(5) is True
    table.close()
