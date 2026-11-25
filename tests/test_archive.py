import io

import pytest

import pyte
from pyte import archive, ledger
from pyte.screens import Char


def archive_bytes(parts, columns=10, lines=3, **metadata):
    return archive.build_archive(parts, columns=columns, lines=lines,
                                 **metadata)


def native_history(data, columns=10, lines=3, history=10000):
    screen = pyte.HistoryScreen(columns, lines, history=history)
    pyte.ByteStream(screen).feed(data)
    return ["".join(line[x].data for x in range(columns))
            for line in screen.history.top]


def texts(capture):
    return [record.stripped for record in capture]


def raw_texts(capture):
    return [record.text for record in capture]


# -- archive format -------------------------------------------------------

def test_uncompressed_bytes_are_one_literal_segment():
    data = b"plain\r\ncapture\r\n"
    parsed = archive.Archive(data)
    assert len(parsed.segments) == 1
    assert parsed.segments[0].kind == "literal"
    assert parsed.expanded_size == len(data)
    assert parsed.read_bytes() == data


def test_templates_are_not_expanded_on_parse():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.literal(b"head\r\n")
    writer.template(b"body\r\n", repeat=100_000_000)
    parsed = archive.Archive(writer.to_bytes())

    assert len(parsed.segments) == 2
    assert parsed.segments[1].kind == "run"
    assert parsed.segments[1].repeat == 100_000_000
    assert parsed.segments[1].template == b"body\r\n"
    assert parsed.expanded_size == 6 * 100_000_000 + 6
    assert parsed.size < 1024


def test_archive_roundtrip_occurrences():
    writer = archive.ArchiveWriter(columns=5, lines=2)
    writer.literal(b"abc")
    writer.template(b"de", repeat=3)
    parsed = archive.Archive(writer.to_bytes())

    occurrences = list(parsed.iter_occurrences())
    assert [item.data for item in occurrences] == [b"abc", b"de", b"de", b"de"]
    assert [item.byte_start for item in occurrences] == [0, 3, 5, 7]
    assert [item.occurrence for item in occurrences[1:]] == [1, 2, 3]
    assert parsed.read_bytes(3, 7) == b"dede"


def test_malformed_archive_is_rejected():
    with pytest.raises(archive.ArchiveFormatError):
        archive.Archive(archive.MAGIC + b"\x01" + b"\x03")


# -- records --------------------------------------------------------------

def test_short_capture_matches_native_history():
    data = b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n"
    capture = ledger.Ledger(archive_bytes([data]))
    assert raw_texts(capture) == native_history(data)


def test_linefeed_and_index_records():
    data = b"a\r\nb\r\nc\r\nd\r\n"
    capture = ledger.Ledger(archive_bytes([data]))
    records = list(capture)
    assert [record.stripped for record in records] == ["a", "b"]
    assert all(record.trigger == "linefeed" for record in records)
    assert records[0].index == 1
    assert records[0].margin_top == 0


def test_record_keeps_top_of_scrolling_region():
    data = b"\x1b[2;4r\x1b[2;1Hedge\r\nbody\r\nfoot\r\nmore\r\ntail\r\n"
    capture = ledger.Ledger(archive_bytes([data], lines=4))
    records = list(capture)

    assert [record.stripped for record in records] == ["edge", "body", "foot"]
    assert {(record.top, record.bottom) for record in records} == {(1, 3)}
    assert all(record.wrapped_region for record in records)


def test_screen_top_record_is_distinct_from_region_top():
    data = "0\r\n1\r\n2\r\n3\r\n4\r\n".encode()
    capture = ledger.Ledger(archive_bytes([data]))
    record = capture.record(1)
    assert (record.top, record.bottom) == (0, 2)
    assert not record.wrapped_region
    assert record.screen_top == 0


def test_auto_wrap_trigger():
    data = b"abcdefghijklmnop"
    capture = ledger.Ledger(archive_bytes([data], columns=5, lines=3))
    records = list(capture)

    assert [record.stripped for record in records] == ["abcde"]
    assert records[0].trigger == "auto-wrap"


def test_auto_wrap_at_last_column_then_one_more_char():
    # 5 columns: pyte wraps only after the next character arrives, and a
    # record needs a full screen of wrapped lines first.
    data = b"a" * 25
    capture = ledger.Ledger(archive_bytes([data], columns=5, lines=3))
    assert [record.stripped for record in capture] == ["aaaaa", "aaaaa"]
    assert all(record.trigger == "auto-wrap" for record in capture)

    full = ledger.Ledger(archive_bytes([b"a" * 15], columns=5, lines=3))
    assert list(full) == []


def test_reverse_index_and_paging_do_not_create_records():
    data = b"one\r\ntwo\r\nthree\r\n\x1bM\x1bM"
    capture = ledger.Ledger(archive_bytes([data]))
    assert texts(capture) == ["one"]

    data = b"one\r\ntwo\r\nthree\r\n" + pyte.control.ESC.encode() + b"P"
    capture = ledger.Ledger(archive_bytes([data]))
    assert texts(capture) == ["one"]


def test_history_clear_does_not_remove_records():
    data = b"one\r\ntwo\r\nthree\r\nOK\r\nnext\r\nlast\r\n"
    clean = ledger.Ledger(archive_bytes([data]))
    cleared = ledger.Ledger(archive_bytes(
        [b"one\r\ntwo\r\nthree\r\n\x1b[3JOK\r\nnext\r\nlast\r\n"]))
    assert texts(clean) == ["one", "two", "three", "OK"]
    # The historical grid is cleared, so the following scrolls copy a blank
    # top line, but the records taken before the clear are still there.
    assert texts(cleared) == ["one", "", "", "OK"]


def test_reset_does_not_remove_records():
    whole = ledger.Ledger(archive_bytes(
        [b"one\r\ntwo\r\nthree\r\nx\r\ny\r\n"]))
    reset = ledger.Ledger(archive_bytes(
        [b"one\r\ntwo\r\nthree\r\n\x1bcx\r\ny\r\n"]))
    assert texts(whole) == ["one", "two", "three"]
    # Records already taken survive RIS; the reset screen only yields new
    # records after it fills up again.
    assert texts(reset) == ["one"]
    assert reset.record(1).stripped == "one"


# -- provenance -----------------------------------------------------------

def test_byte_ranges_point_into_expanded_stream():
    data = b"ab\r\ncd\r\nef\r\n"
    raw = archive_bytes([data])
    parsed = archive.Archive(raw)
    capture = ledger.Ledger(parsed)
    record = capture.record(1)

    assert record.cells[0].start == 0
    assert record.cells[0].end == 1
    assert record.cells[1].start == 1
    assert record.cells[1].end == 2
    assert parsed.read_bytes(*record.cells[0].byte_range) == b"a"
    assert parsed.read_bytes(*record.byte_range) == b"ab"


def test_template_occurrence_and_stored_range():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.template(b"row\r\n", repeat=4)
    raw = writer.to_bytes()
    parsed = archive.Archive(raw)
    capture = ledger.Ledger(parsed)
    record = capture.record(2)  # second scrolled row
    cell = record.cells[0]

    assert record.stripped.startswith("row")
    assert cell.source == 0
    assert cell.occurrence == 2
    assert cell.start == 5
    assert raw[cell.stored_start:cell.stored_end] == b"r"
    assert parsed.read_bytes(*cell.byte_range) == b"r"


def test_opening_write_keeps_its_source_after_progress_rewrites():
    # The station code is registered once.  The progress field is then
    # rewritten in the same fixed columns with the same value, so the screen
    # looks identical although the number now comes from the new write.
    parts = [
        b"\x1b[1;1HBJ47\x1b[1;6H0001",
        b"\x1b[1;6H0001",
        b"\x1b[2;1Hmid",
        b"\x1b[3;1Hbot",
        b"\n",
    ]
    capture = ledger.Ledger(archive_bytes(parts, columns=10, lines=3))
    record = capture.record(1)

    assert record.raw_text[:9] == "BJ47 0001"
    station = record.cells[0]
    progress = record.cells[5]
    assert (station.data, station.source, station.occurrence) == ("B", 0, 1)
    assert (progress.data, progress.source, progress.occurrence) == (
        "0", 1, 1)
    assert progress.start > station.start


def test_inserted_characters_keep_original_byte_range():
    parts = [b"abcd\r\n", b"\x1b[1;1H\x1b[2@", b"\r\nef\r\ngh\r\n"]
    capture = ledger.Ledger(archive_bytes(parts, columns=8, lines=3))
    record = capture.record(1)
    # "  abcd": the two blanks are the ICH fill, "a".."d" moved right.
    moved = record.cells[2]
    assert moved.data == "a"
    assert moved.start == 0
    assert moved.end == 1


def test_wide_char_provenance():
    data = "あb\r\n".encode()
    raw = b""
    writer = archive.ArchiveWriter(columns=4, lines=2)
    writer.literal(data)
    raw = writer.to_bytes()
    capture = ledger.Ledger(archive.Archive(raw))
    # Nothing has scrolled yet: fill and force a scroll.
    capture2 = ledger.Ledger(archive_bytes([data + b"c\r\nd\r\n"], columns=4,
                                           lines=2))
    record = capture2.record(1)
    assert record.cells[0].data == "あ"
    assert record.cells[0].end - record.cells[0].start == 3


# -- long runs ------------------------------------------------------------

def test_hundred_million_repeats_is_fast_and_accurate():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.template(b"row-0000\r\n", repeat=100_000_000)
    parsed = archive.Archive(writer.to_bytes())
    capture = ledger.Ledger(parsed)

    assert len(capture) == 100_000_000 - 2
    tail = capture.tail(2)
    assert [record.stripped for record in tail] == ["row-0000", "row-0000"]
    assert tail[-1].cells[0].occurrence == 100_000_000 - 2
    assert capture.record(50_000_000).cells[0].occurrence == 50_000_000


def test_long_run_matches_native_history():
    template = b"line-1\r\n"
    native = native_history(template * 20)
    capture = ledger.Ledger(archive_bytes(
        [(template, 20)], lines=3, columns=10))
    assert raw_texts(capture) == native


def test_short_repeat_uses_native_history():
    template = b"tick\r\n"
    native = native_history(template * 12)
    capture = ledger.Ledger(archive_bytes(
        [(template, 12)], lines=3, columns=10))
    assert raw_texts(capture) == native


def test_chunked_literal_equals_whole_literal():
    data = b"alpha\r\nbeta\r\ngamma\r\ndelta\r\n"
    one = ledger.Ledger(archive_bytes([data]))
    split = ledger.Ledger(archive_bytes(
        [data[:8], data[8:15], data[15:]]))
    assert texts(one) == texts(split)


def test_split_utf8_code_point_keeps_byte_ranges():
    data = "é\r\nx\r\n".encode()
    assert data[:1] != data[:2]
    split = ledger.Ledger(archive_bytes([data[:1], data[1:]], columns=4,
                                        lines=2))
    record = split.record(1)
    assert record.cells[0].data == "é"
    assert record.cells[0].start == 0
    assert record.cells[0].end == 2


def test_records_do_not_store_expanded_tail():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.template(b"row\r\n", repeat=50_000_000)
    capture = ledger.Ledger(archive.Archive(writer.to_bytes()))
    stored = sum(len(span.cycle) + len(span.lead) + len(span.leftover)
                 for span in capture._spans)
    assert stored < 100
    assert len(capture) == 50_000_000 - 2


def test_consecutive_long_runs_keep_their_own_sources():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.template(b"seg\r\n", repeat=100_000_000)
    writer.template(b"seg\r\n", repeat=100_000_000)
    capture = ledger.Ledger(archive.Archive(writer.to_bytes()))

    assert len(capture) == 200_000_000 - 2
    assert capture.record(99_999_999).cells[0].occurrence == 99_999_999
    assert capture.record(100_000_000).cells[0].occurrence == 100_000_000
    first_of_second = capture.record(100_000_001).cells[0]
    assert first_of_second.source == 1
    assert first_of_second.occurrence == 1
    assert first_of_second.start == 500_000_000
    last = capture.record(len(capture)).cells[0]
    assert last.source == 1
    assert last.occurrence == 99_999_998
    assert last.start == 999_999_985


def test_huge_progress_run_keeps_opening_code():
    writer = archive.ArchiveWriter(columns=10, lines=3)
    writer.literal(b"\x1b[1;1HBJ47")
    writer.template(b"\x1b[1;6H0042\x1b[3;1H\n", repeat=100_000_000)
    capture = ledger.Ledger(archive.Archive(writer.to_bytes()))

    assert len(capture) == 100_000_000
    first = capture.record(1)
    assert first.stripped[:9] == "BJ47 0042"
    assert (first.cells[0].source, first.cells[0].occurrence) == (0, 1)
    assert (first.cells[5].source, first.cells[5].occurrence) == (1, 1)

    last = capture.record(len(capture))
    assert last.stripped[:9] == "     0042"
    assert last.cells[4].source is None
    assert last.cells[5].occurrence == 100_000_000
    assert last.cells[5].start == 16 + 17 * 99_999_999


# -- API surface ----------------------------------------------------------

def test_archive_accepts_path_and_file(tmp_path):
    raw = archive_bytes([b"hello\r\nworld\r\n"])
    path = tmp_path / "capture.arc"
    path.write_bytes(raw)
    assert len(ledger.Ledger(path)) == len(ledger.Ledger(io.BytesIO(raw)))


def test_record_slice_and_index():
    data = b"a\r\nb\r\nc\r\nd\r\n"
    capture = ledger.Ledger(archive_bytes([data]))
    assert [record.stripped for record in capture[:2]] == ["a", "b"]
    assert capture[-1].stripped == "b"
    with pytest.raises(IndexError):
        capture.record(99)


def test_cell_entry_is_namedtuple_compatible():
    data = b"a\r\nb\r\nc\r\n"
    capture = ledger.Ledger(archive_bytes([data]))
    record = capture.record(1)
    cell = record.cells[0]
    assert cell.data == "a"
    x, data, source, occurrence, start, end = cell[:6]
    assert (x, data, source, occurrence, start, end) == (0, "a", 0, 1, 0, 1)


# -- reader facade --------------------------------------------------------

def test_reader_facade_count_get_and_json(tmp_path, capsys):
    import json

    from pyte import reader

    raw = archive_bytes([b"one\r\ntwo\r\nthree\r\nfour\r\n"], lines=2)
    capture = reader.open_capture(raw, columns=10, lines=2)
    assert reader.count(capture) == 3
    assert reader.get(capture, 1).stripped == "one"
    assert reader.get(capture, -1).stripped == "three"

    payload = reader.record_to_dict(reader.get(capture, 2))
    assert payload["index"] == 2
    assert payload["stripped"] == "two"
    assert payload["cells"][0]["source"] == 0

    path = tmp_path / "capture.arc"
    path.write_bytes(raw)
    assert reader.main([str(path), "--count"]) == 0
    assert capsys.readouterr().out.strip() == "3"

    assert reader.main([str(path), "--tail", "1", "--json",
                        "--no-cells"]) == 0
    tail = json.loads(capsys.readouterr().out)
    assert tail["stripped"] == "three"
    assert "cells" not in tail
