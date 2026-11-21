#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Regression net for the public CSV conversion entry points
:func:`pyexcel.save_as` and :func:`pyexcel.isave_as`, written ahead of
replacing the CSV plugin.

Only the native public entry points are exercised here. No internal
function of the single-sheet conversion pipeline is named, mocked or
otherwise relied upon, so that pipeline may be reimplemented freely
while this file keeps pinning the observable contract.

Behaviour verified as of today (and the divergence each check is
meant to catch):

1. Read-side ``encoding``/``delimiter`` and their ``dest_`` prefixed
   write-side counterparts are honoured independently, in both
   conversion directions and regardless of conversion order inside
   one process. (Guards against read/write parameters being crossed
   or a previous task's parameters being reused: a simple sample may
   still look byte-correct while later conversions silently drop
   records that span multiple physical lines.)
2. ``isave_as`` writes the first logical record before the source
   has been read to the end. (Guards against read-all-then-write.)
3. A read error raised inside the quoted field of the second record,
   after the writer has received the first record, aborts the
   conversion; the incomplete record is not flushed as a complete
   row. (Guards against failed records materialising as full rows.)
4. The conversion issued right after a failed one, with a different
   encoding and different delimiters, produces byte-exact output.
   (Guards against a failed task's configuration leaking into the
   next call.)

The fixtures are generated locally from exact byte literals instead
of being checked into ``tests/fixtures``, so the UTF-8 BOM and the
CP1252-only bytes survive editors and version control untouched.
Each encoding carries a sentinel that only that encoding decodes
correctly: ``中文`` for UTF-8 (silent mojibake under CP1252) and
CP1252 smart quotes (a hard ``UnicodeDecodeError`` under UTF-8).
Each fixture holds a quoted field containing the source delimiter,
the target delimiter, doubled quotes and an embedded newline.

All output is verified with independent tools: raw byte reads,
``codecs.BOM_UTF8`` and the standard library :mod:`csv` module,
never through pyexcel itself. Source files are byte-compared before
and after each conversion.
"""

import csv
import codecs
import io
import threading
from collections import namedtuple

import pytest
import pyexcel as pe

# keep every cell verbatim so that byte-exact comparison only
# exercises encoding, delimiter and quoting, never type detection
KEEP_CELLS_AS_TEXT = dict(
    auto_detect_int=False,
    auto_detect_float=False,
    auto_detect_datetime=False,
)

UTF8_SENTINEL = "中文"
CP1252_SENTINEL = "“tail”"

# a utf-8-sig log file, ';' delimited. The second logical record
# spans two physical lines and its quoted field carries the source
# delimiter ';', the target delimiter '|', doubled quotes and an
# embedded newline.
UTF8_LOG_PHYSICAL_LINES = [
    "id;message;level\r\n",
    '1;"reboot; now | said ""go""\n',
    'continued 中文";INFO\r\n',
    "2;plain tail 中文;WARN\r\n",
]
UTF8_LOG_BYTES = codecs.BOM_UTF8 + "".join(UTF8_LOG_PHYSICAL_LINES).encode(
    "utf-8"
)

# a cp1252 quotation sheet, ',' delimited. The second logical record
# spans two physical lines and its quoted field carries the source
# delimiter ',', the target delimiter ';', doubled quotes and an
# embedded newline. Bytes 0x93/0x94 (the smart quotes) are invalid
# utf-8.
CP1252_QUOTE_PHYSICAL_LINES = [
    "sku,note,price\r\n",
    'Q1,"quoted, and; semicolon ""double""\n',
    "and “smart” quotes\",10\r\n",
    "Q2,plain “tail”,20\r\n",
]
CP1252_QUOTE_BYTES = "".join(CP1252_QUOTE_PHYSICAL_LINES).encode("cp1252")

# a source whose second record opens a quoted field and then fails
# to decode: the read error surfaces strictly inside the quotes of
# the second logical record, after the first one was served
FAILING_PHYSICAL_LINES = [
    "1;first record;INFO\r\n",
    '2;"broken; record | said ""go""\n',
    'continued 中文";WARN\r\n',
]

GATE_TIMEOUT_SECONDS = 5

_Spec = namedtuple(
    "_Spec",
    [
        "name",
        "raw",
        "read_encoding",
        "read_delimiter",
        "write_encoding",
        "write_delimiter",
        "sentinel",
    ],
)

UTF8_LOG = _Spec(
    name="utf8_bom_log",
    raw=UTF8_LOG_BYTES,
    read_encoding="utf-8-sig",
    read_delimiter=";",
    write_encoding="utf-8",
    write_delimiter="|",
    sentinel=UTF8_SENTINEL,
)
CP1252_QUOTES = _Spec(
    name="cp1252_quotes",
    raw=CP1252_QUOTE_BYTES,
    read_encoding="cp1252",
    read_delimiter=",",
    write_encoding="utf-8",
    write_delimiter=";",
    sentinel=CP1252_SENTINEL,
)


def _parse_csv_bytes(raw, encoding, delimiter):
    """Independent reader: decode and split into logical records."""
    text = raw.decode(encoding)
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _render_csv_bytes(rows, delimiter, encoding):
    """Independent writer: the bytes a correct conversion must make."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode(encoding)


def _assert_conversion(actual, spec):
    """Byte-exact verification of one converted file."""
    rows = _parse_csv_bytes(
        spec.raw, spec.read_encoding, spec.read_delimiter
    )
    expected = _render_csv_bytes(
        rows, spec.write_delimiter, spec.write_encoding
    )
    # byte exact: catches crossed encodings, crossed delimiters and
    # records dropped across physical lines
    assert actual == expected
    # the write side was asked for plain utf-8: no BOM may appear,
    # however the utf-8-sig source did carry one
    assert not actual.startswith(codecs.BOM_UTF8)
    assert spec.sentinel in actual.decode(spec.write_encoding)
    reparsed = _parse_csv_bytes(
        actual, spec.write_encoding, spec.write_delimiter
    )
    assert reparsed == rows


def test_fixtures_discriminate_the_two_encodings():
    assert UTF8_LOG_BYTES.startswith(codecs.BOM_UTF8)
    assert UTF8_SENTINEL in UTF8_LOG_BYTES.decode("utf-8-sig")
    # silent mojibake: the utf-8 sentinel never survives a cp1252 read
    assert UTF8_SENTINEL not in UTF8_LOG_BYTES.decode("cp1252")
    assert CP1252_SENTINEL in CP1252_QUOTE_BYTES.decode("cp1252")
    # hard failure: the cp1252 sentinel is not decodable as utf-8
    with pytest.raises(UnicodeDecodeError):
        CP1252_QUOTE_BYTES.decode("utf-8")
    for raw, encoding, delimiter, source, target in [
        (UTF8_LOG_BYTES, "utf-8-sig", ";", ";", "|"),
        (CP1252_QUOTE_BYTES, "cp1252", ",", ",", ";"),
    ]:
        rows = _parse_csv_bytes(raw, encoding, delimiter)
        assert len(rows) == 3
        field = rows[1][1]
        assert source in field
        assert target in field
        assert '"' in field
        assert "\n" in field


@pytest.mark.parametrize(
    "entry_point", [pe.save_as, pe.isave_as], ids=["save_as", "isave_as"]
)
@pytest.mark.parametrize(
    "specs",
    [[UTF8_LOG, CP1252_QUOTES], [CP1252_QUOTES, UTF8_LOG]],
    ids=["utf8-bom-log-first", "cp1252-quotes-first"],
)
def test_sequential_file_conversions_keep_each_call_its_own_parameters(
    tmp_path, entry_point, specs
):
    """Two opposite conversions run back to back in this very process.

    Each call passes different read-side and write-side encodings and
    delimiters; both outputs must be byte-exact and both sources must
    be untouched afterwards.
    """
    sources = []
    for spec in specs:
        path = tmp_path / ("source_%s.csv" % spec.name)
        path.write_bytes(spec.raw)
        sources.append((spec, path))

    outputs = []
    for spec, path in sources:
        output = tmp_path / ("converted_%s.csv" % spec.name)
        entry_point(
            file_name=str(path),
            encoding=spec.read_encoding,
            delimiter=spec.read_delimiter,
            dest_file_name=str(output),
            dest_encoding=spec.write_encoding,
            dest_delimiter=spec.write_delimiter,
            **KEEP_CELLS_AS_TEXT,
        )
        outputs.append((spec, output))

    for spec, output in outputs:
        _assert_conversion(output.read_bytes(), spec)

    for spec, path in sources:
        assert path.read_bytes() == spec.raw


class _GatedTextStream:
    """A line server that blocks at a logical record boundary.

    Physical lines are served one by one. The moment the second
    logical record has been served completely, the stream stalls
    until the write side releases it, or fails loudly after a
    timeout. ``read()`` is a tripwire: a consumer reading the whole
    stream in one go is exactly the read-all-then-write divergence
    this test exists to catch.
    """

    def __init__(self, lines, gate_at_line, release):
        self._lines = lines
        self._gate_at_line = gate_at_line
        self._release = release
        self._served = 0
        self.gate_hit = False
        self.released_by_writer = False

    def read(self, size=-1):
        raise RuntimeError(
            "non-streaming read() on the gated source: "
            "the whole source was consumed in one go"
        )

    def close(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        if self._served == self._gate_at_line:
            self.gate_hit = True
            if not self._release.wait(GATE_TIMEOUT_SECONDS):
                raise RuntimeError(
                    "the writer did not receive the first record "
                    "within %ds: isave_as() appears to read "
                    "everything before writing" % GATE_TIMEOUT_SECONDS
                )
            self.released_by_writer = True
        if self._served >= len(self._lines):
            raise StopIteration
        line = self._lines[self._served]
        self._served += 1
        return line


class _FailingTextStream:
    """A line server that raises a decode error at a chosen line."""

    def __init__(self, lines, fail_at_line):
        self._lines = lines
        self._fail_at_line = fail_at_line
        self._served = 0

    def read(self, size=-1):
        raise RuntimeError(
            "non-streaming read() on the failing source: "
            "the whole source was consumed in one go"
        )

    def close(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        if self._served == self._fail_at_line:
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte"
            )
        if self._served >= len(self._lines):
            raise StopIteration
        line = self._lines[self._served]
        self._served += 1
        return line


class _ObservingWriteStream(io.StringIO):
    """A write destination that records every chunk it receives."""

    def __init__(self, release=None):
        super().__init__()
        self.writes = []
        self._release = release

    def write(self, chunk):
        self.writes.append(chunk)
        if self._release is not None:
            self._release.set()
        return super().write(chunk)


def test_isave_as_writes_first_record_before_reading_everything():
    """The source stalls after the second logical record boundary.

    Only the writer actually receiving the first record releases it;
    a read-all-then-write implementation deadlocks and fails here.
    """
    release = threading.Event()
    # physical line 3 starts the third logical record, so the gate
    # sits exactly on the second logical record boundary
    source = _GatedTextStream(UTF8_LOG_PHYSICAL_LINES, 3, release)
    destination = _ObservingWriteStream(release)
    try:
        pe.isave_as(
            file_type="csv",
            file_stream=source,
            encoding="utf-8",
            delimiter=";",
            dest_file_type="csv",
            dest_file_stream=destination,
            dest_delimiter="|",
            **KEEP_CELLS_AS_TEXT,
        )
    finally:
        pe.free_resources()
    assert source.gate_hit
    assert source.released_by_writer
    rows = _parse_csv_bytes(
        "".join(UTF8_LOG_PHYSICAL_LINES).encode("utf-8"), "utf-8", ";"
    )
    expected = _render_csv_bytes(rows, "|", "utf-8").decode("utf-8")
    assert destination.getvalue() == expected


def test_isave_as_read_error_boundary_then_next_call_is_clean(tmp_path):
    """A decode error inside the second record's quotes must abort
    the conversion after the first complete record, and the next
    conversion - different encoding, different delimiters - must not
    inherit anything from the failed task.
    """
    destination = _ObservingWriteStream()
    with pytest.raises(UnicodeDecodeError):
        try:
            pe.isave_as(
                file_type="csv",
                file_stream=_FailingTextStream(FAILING_PHYSICAL_LINES, 2),
                encoding="utf-8",
                delimiter=";",
                dest_file_type="csv",
                dest_file_stream=destination,
                dest_delimiter="|",
                **KEEP_CELLS_AS_TEXT,
            )
        finally:
            pe.free_resources()

    # failure boundary: exactly the first logical record came out,
    # as one write, and the broken record was not flushed as a
    # complete row
    first_record = _render_csv_bytes(
        [["1", "first record", "INFO"]], "|", "utf-8"
    ).decode("utf-8")
    assert destination.writes == [first_record]
    rows = _parse_csv_bytes(
        destination.getvalue().encode("utf-8"), "utf-8", "|"
    )
    assert rows == [["1", "first record", "INFO"]]
    assert "broken" not in destination.getvalue()

    # immediately afterwards: a normal conversion under another
    # encoding and another pair of delimiters, byte-exact
    source = tmp_path / "quotes.csv"
    source.write_bytes(CP1252_QUOTE_BYTES)
    output = tmp_path / "converted_quotes.csv"
    pe.isave_as(
        file_name=str(source),
        encoding="cp1252",
        delimiter=",",
        dest_file_name=str(output),
        dest_encoding="utf-8",
        dest_delimiter=";",
        **KEEP_CELLS_AS_TEXT,
    )
    _assert_conversion(output.read_bytes(), CP1252_QUOTES)
    assert source.read_bytes() == CP1252_QUOTE_BYTES
