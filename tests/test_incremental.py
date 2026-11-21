"""Tests for resumable CSV projection of headerless supplier files."""

import json
import os

import pyexcel as pe

from pyexcel.incremental import (
    CSVProjection,
    MalformedCSVError,
    Progress,
    ProgressError,
    ProjectionConfigurationError,
    RecordTooWideError,
    StaleProgressError,
    UnterminatedFieldError,
    UpdateIntent,
)

from .nose_tools import eq_, raises


SCHEMA = ["order_no", "store_no", "amount"]

SOURCE = (
    b"1001,2001,0\r\n"
    b"1002,2002,\r\n"
    b'1003,"note with, comma\nand a newline",17\r\n'
    b"1004\n"
    b"1005,2005,\"\"\r\n"
    b"1006,2006,2006\r\n"
)


def _write(tmp_path, content, name="supplier.csv"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _project(path, progress_file=None, chunk_size=65536, **keywords):
    return CSVProjection(
        SCHEMA,
        SCHEMA,
        file_name=path,
        progress_file=progress_file,
        chunk_size=chunk_size,
        **keywords
    )


def _snapshot(projection):
    result = []
    for record in projection:
        result.append(
            (
                record.index,
                record.position,
                tuple(
                    (
                        update.column,
                        update.column_index,
                        update.intent,
                        update.text,
                        update.value,
                    )
                    for update in record.updates.values()
                ),
            )
        )
    return result


def test_three_update_intents_are_distinct(tmp_path):
    path = _write(tmp_path, SOURCE)
    records = list(_project(path))

    eq_(records[0].intents()["amount"], UpdateIntent.SET)
    eq_(records[0]["amount"].value, 0)
    eq_(records[1].intents()["amount"], UpdateIntent.CLEAR)
    eq_(records[1]["amount"].value, "")
    eq_(records[3].intents()["store_no"], UpdateIntent.ABSENT)
    eq_(records[3]["store_no"].value, None)
    eq_(records[4].intents()["amount"], UpdateIntent.CLEAR)
    eq_(records[4]["amount"].quoted, True)

    eq_(
        [
            (record["order_no"].intent, record["order_no"].value)
            for record in records
        ],
        [
            (UpdateIntent.SET, 1001),
            (UpdateIntent.SET, 1002),
            (UpdateIntent.SET, 1003),
            (UpdateIntent.SET, 1004),
            (UpdateIntent.SET, 1005),
            (UpdateIntent.SET, 1006),
        ],
    )


def test_quoted_newline_and_comma_stay_inside_field(tmp_path):
    path = _write(tmp_path, SOURCE)
    record = list(_project(path))[2]
    eq_(record["store_no"].value, "note with, comma\nand a newline")
    eq_(record.position.line_number, 3)
    eq_(record.position.end_line_number, 5)


def test_quoted_crlf_is_a_single_field_value(tmp_path):
    path = _write(
        tmp_path,
        b'1,"first\r\nsecond",3\r\n4,5,6\r\n',
        name="crlf.csv",
    )
    records = list(_project(path))
    eq_(records[0]["store_no"].value, "first\nsecond")
    eq_(records[0].position.end_line_number, 3)
    eq_(records[1]["order_no"].value, 4)


def test_projection_honours_requested_column_order(tmp_path):
    path = _write(tmp_path, SOURCE)
    projection = CSVProjection(
        ["amount", "order_no"],
        SCHEMA,
        file_name=path,
    )
    first = next(iter(projection))
    eq_(first.columns, ("amount", "order_no"))
    eq_(first.values(), (0, 1001))
    eq_(first["order_no"].column_index, 0)


def test_source_position_is_byte_exact(tmp_path):
    path = _write(tmp_path, SOURCE)
    records = list(_project(path))
    eq_(records[0].position.offset, 0)
    eq_(records[0].position.end_offset, SOURCE.index(b"1002"))
    eq_(records[1].position.offset, SOURCE.index(b"1002"))
    eq_(records[2].position.offset, SOURCE.index(b"1003"))
    eq_(records[2].position.end_offset, SOURCE.index(b"1004"))
    delta = records[2].position.end_offset - records[2].position.offset
    eq_(delta, len(b'1003,"note with, comma\nand a newline",17\r\n'))


def test_every_chunk_size_produces_the_same_projection(tmp_path):
    path = _write(tmp_path, SOURCE)
    baseline = _snapshot(_project(path))
    for chunk_size in (1, 2, 3, 4, 5, 7, 11, 13, 64, 4096):
        eq_(_snapshot(_project(path, chunk_size=chunk_size)), baseline)


def test_resume_after_unconfirmed_multiline_record(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")

    first_run = _project(path, progress_file, chunk_size=5)
    iterator = iter(first_run)
    for _ in range(2):
        confirmed = next(iterator)
        first_run.ack(confirmed)
    eq_(confirmed.index, 1)
    unconfirmed = next(iterator)
    eq_(unconfirmed.index, 2)
    del iterator

    resumed = _project(path, progress_file, chunk_size=7)
    eq_(resumed.progress.record_index, 2)
    eq_(resumed.progress.offset, source_offset(path, b"1003"))
    iterator = iter(resumed)
    again = next(iterator)
    eq_(again.index, 2)
    eq_(again.position, unconfirmed.position)
    eq_(again.to_dict(), unconfirmed.to_dict())
    eq_(again["store_no"].value, "note with, comma\nand a newline")
    resumed.ack(again)
    eq_(next(iterator).index, 3)


def source_offset(path, needle):
    with open(path, "rb") as handle:
        return handle.read().index(needle)


def test_confirmed_records_are_not_replayed(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")

    projection = _project(path, progress_file)
    delivered = []
    for record in projection:
        delivered.append(record)
        projection.ack(record)
        if record.index == 2:
            break

    resumed = _project(path, progress_file)
    replayed = [record.index for record in resumed]
    eq_(replayed, [3, 4, 5])


def test_recovered_stream_equals_full_projection(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    baseline = _snapshot(_project(path))

    projection = _project(path, progress_file, chunk_size=4)
    prefix = []
    for record in projection:
        prefix.append(
            (
                record.index,
                record.position,
                tuple(
                    (
                        update.column,
                        update.column_index,
                        update.intent,
                        update.text,
                        update.value,
                    )
                    for update in record.updates.values()
                ),
            )
        )
        projection.ack(record)
        if record.index == 1:
            break

    remainder = _snapshot(_project(path, progress_file, chunk_size=9))
    eq_(prefix + remainder, baseline)


def test_unterminated_field_has_no_confirmable_progress(tmp_path):
    content = SOURCE + b'1007,"never closed,2007\r\n'
    path = _write(tmp_path, content, name="broken.csv")
    progress_file = os.path.join(str(tmp_path), "progress.json")

    projection = _project(path, progress_file)
    iterator = iter(projection)
    confirmed = 0
    while True:
        try:
            record = next(iterator)
        except UnterminatedFieldError as error:
            eq_(error.record_index, 6)
            eq_(error.column_index, 1)
            eq_(error.column_name, "store_no")
            eq_(error.line_number, 8)
            break
        projection.ack(record)
        confirmed = record.index + 1

    eq_(confirmed, 6)
    resumed = _project(path, progress_file)
    eq_(resumed.progress, Progress(source_offset(path, b"1007"), 6, 8))


def test_wide_record_reports_column_and_logical_record(tmp_path):
    content = b"1,2,3\r\n4,5,6,7,8\r\n"
    path = _write(tmp_path, content, name="wide.csv")
    projection = _project(path)
    iterator = iter(projection)
    eq_(next(iterator).index, 0)
    try:
        next(iterator)
    except RecordTooWideError as error:
        eq_(error.record_index, 1)
        eq_(error.record_number, 2)
        eq_(error.column_index, 3)
        eq_(error.column_number, 4)
        eq_(error.line_number, 2)
        eq_(error.extra_fields, ("7", "8"))
        eq_(error.position.offset, len(b"1,2,3\r\n"))
    else:
        raise AssertionError("RecordTooWideError was not raised")


def test_wide_record_keeps_previous_progress(tmp_path):
    content = b"1,2,3\r\n4,5\r\n6,7,8,9\r\n"
    path = _write(tmp_path, content, name="wide.csv")
    progress_file = os.path.join(str(tmp_path), "progress.json")

    projection = _project(path, progress_file)
    iterator = iter(projection)
    first = next(iterator)
    projection.ack(first)
    second = next(iterator)
    projection.ack(second)
    before = projection.progress
    try:
        next(iterator)
    except RecordTooWideError:
        pass

    resumed = _project(path, progress_file)
    eq_(resumed.progress, before)
    eq_(before.record_index, 2)


def test_malformed_quote_is_reported_with_position(tmp_path):
    content = b'1,2,3\r\n4,"5"x,6\r\n'
    path = _write(tmp_path, content, name="malformed.csv")
    try:
        list(_project(path))
    except MalformedCSVError as error:
        eq_(error.record_index, 1)
        eq_(error.column_index, 1)
        eq_(error.column_name, "store_no")
        eq_(error.line_number, 2)
    else:
        raise AssertionError("MalformedCSVError was not raised")


def test_progress_file_holds_only_counters(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    projection = _project(path, progress_file)
    for record in projection:
        projection.ack(record)
        if record.index == 1:
            break

    with open(progress_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    eq_(set(payload["position"]), {"offset", "record_index", "line_number"})
    flat = json.dumps(payload)
    assert "note with" not in flat
    assert "1001" not in flat
    assert len(flat) < 1024


def test_changed_source_is_rejected(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    projection = _project(path, progress_file)
    projection.ack(next(iter(projection)))

    with open(path, "ab") as handle:
        handle.write(b"1007,2007,2007\r\n")

    try:
        _project(path, progress_file)
    except StaleProgressError:
        pass
    else:
        raise AssertionError("StaleProgressError was not raised")


def test_progress_for_another_projection_is_rejected(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    projection = _project(path, progress_file)
    projection.ack(next(iter(projection)))

    try:
        CSVProjection(
            ["store_no", "amount"],
            SCHEMA,
            file_name=path,
            progress_file=progress_file,
        )
    except ProjectionConfigurationError:
        pass
    else:
        raise AssertionError("ProjectionConfigurationError was not raised")


def test_garbage_progress_file_raises(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    with open(progress_file, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    try:
        _project(path, progress_file)
    except ProgressError:
        pass
    else:
        raise AssertionError("ProgressError was not raised")


def test_ack_is_idempotent(tmp_path):
    path = _write(tmp_path, SOURCE)
    progress_file = os.path.join(str(tmp_path), "progress.json")
    projection = _project(path, progress_file)
    record = next(iter(projection))
    first = projection.ack(record)
    eq_(projection.ack(record), first)


def test_ack_skipping_an_unconfirmed_record_is_refused(tmp_path):
    path = _write(tmp_path, SOURCE)
    progression = _project(path)
    iterator = iter(progression)
    next(iterator)
    second = next(iterator)
    try:
        progression.ack(second)
    except Exception as error:
        eq_(type(error).__name__, "ProjectionError")
    else:
        raise AssertionError("skipping a record must not advance progress")


def test_stream_source_is_not_closed(tmp_path):
    import io

    stream = io.BytesIO(SOURCE)
    projection = CSVProjection(
        SCHEMA, SCHEMA, file_stream=stream
    )
    list(projection)
    assert not stream.closed


def test_bom_does_not_change_first_column(tmp_path):
    path = _write(
        tmp_path,
        b"\xef\xbb\xbf1001,2001,0\r\n1002,2002,1\r\n",
        name="bom.csv",
    )
    records = list(_project(path))
    eq_(records[0]["order_no"].value, 1001)
    eq_(records[0]["order_no"].text, "1001")


def test_default_csv_reading_is_unchanged(tmp_path):
    path = _write(tmp_path, b"a,b\r\n1,2\r\n", name="plain.csv")
    eq_(pe.get_sheet(file_name=path).array, [["a", "b"], [1, 2]])


def test_projection_requires_configuration():
    try:
        CSVProjection(["a"], ["a", "b"])
    except ProjectionConfigurationError:
        pass
    else:
        raise AssertionError("ProjectionConfigurationError was not raised")


def test_missing_column_is_rejected():
    try:
        CSVProjection(["a", "c"], ["a", "b"])
    except ProjectionConfigurationError:
        pass
    else:
        raise AssertionError("ProjectionConfigurationError was not raised")
