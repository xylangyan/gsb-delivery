"""Tests for row-windowed reads of (nested) columns.

The hand-crafted files built here contain a list column with NULL lists,
empty lists and lists holding NULL elements, laid out over several data
pages such that one row's elements span a page boundary (the first page of
a window can start in the middle of a row).
"""
import struct

import numpy as np
import pandas as pd
import pytest

import fastparquet
from fastparquet import parquet_thrift as pt
from fastparquet import core


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def bitpack(values, width):
    vals = list(values)
    while len(vals) % 8:
        vals.append(0)
    out = bytearray()
    cur = 0
    nbits = 0
    for v in vals:
        cur |= v << nbits
        nbits += width
        while nbits >= 8:
            out.append(cur & 0xFF)
            cur >>= 8
            nbits -= 8
    return bytes(out)


def encode_levels(values, width):
    """RLE/bit-packed hybrid with 4-byte length prefix (data page v1)."""
    groups = (len(values) + 7) // 8
    run = varint((groups << 1) | 1) + bitpack(values, width)
    return struct.pack("<I", len(run)) + run


def data_page_v1(entries, max_def=3):
    """entries: list of (rep, def, value)"""
    rep = encode_levels([e[0] for e in entries], 1)
    defi = encode_levels([e[1] for e in entries], 2)
    vals = b"".join(struct.pack("<q", e[2]) for e in entries
                    if e[1] == max_def)
    data = rep + defi + vals
    ph = pt.PageHeader(
        type=pt.PageType.DATA_PAGE,
        uncompressed_page_size=len(data),
        compressed_page_size=len(data),
        data_page_header=pt.DataPageHeader(
            num_values=len(entries),
            encoding=pt.Encoding.PLAIN,
            definition_level_encoding=pt.Encoding.RLE,
            repetition_level_encoding=pt.Encoding.RLE,
        ),
    )
    return bytes(ph.to_bytes()) + data


def rows_to_entries(rows, max_def=3):
    """rows: None (NULL list) or lists, possibly holding NULL elements"""
    entries = []
    for row in rows:
        if row is None:
            entries.append((0, 0, None))
        elif len(row) == 0:
            entries.append((0, 1, None))
        else:
            for j, v in enumerate(row):
                entries.append((0 if j == 0 else 1,
                                max_def if v is not None else 2, v))
    return entries


def write_nested_file(fn, row_groups):
    """Write a file with a nested list column "nest" and a flat required
    int64 column "flag".

    row_groups: list of (nest_pages, flags), where nest_pages is a list of
    pages, each a list of (rep, def, value) entries.
    """
    schema = [
        pt.SchemaElement(name="schema", num_children=2),
        pt.SchemaElement(name="nest",
                         repetition_type=pt.FieldRepetitionType.OPTIONAL,
                         converted_type=pt.ConvertedType.LIST, num_children=1),
        pt.SchemaElement(name="list",
                         repetition_type=pt.FieldRepetitionType.REPEATED,
                         num_children=1),
        pt.SchemaElement(name="element", type=pt.Type.INT64,
                         repetition_type=pt.FieldRepetitionType.OPTIONAL),
        pt.SchemaElement(name="flag", type=pt.Type.INT64,
                         repetition_type=pt.FieldRepetitionType.REQUIRED),
    ]
    out = bytearray(b"PAR1")
    rgs = []
    n_rows = 0
    for nest_pages, flags in row_groups:
        columns = []
        nest_off = len(out)
        n_entries = 0
        total = 0
        for page in nest_pages:
            blob = data_page_v1(page)
            out += blob
            n_entries += len(page)
            total += len(blob)
        nest_cmd = pt.ColumnMetaData(
            type=pt.Type.INT64,
            encoding=[pt.Encoding.PLAIN, pt.Encoding.RLE],
            path_in_schema=["nest", "list", "element"],
            codec=pt.CompressionCodec.UNCOMPRESSED,
            num_values=n_entries,
            total_uncompressed_size=total,
            total_compressed_size=total,
            data_page_offset=nest_off,
        )
        columns.append(pt.ColumnChunk(meta_data=nest_cmd, file_offset=nest_off))

        flag_off = len(out)
        vals = b"".join(struct.pack("<q", f) for f in flags)
        ph = pt.PageHeader(
            type=pt.PageType.DATA_PAGE,
            uncompressed_page_size=len(vals),
            compressed_page_size=len(vals),
            data_page_header=pt.DataPageHeader(
                num_values=len(flags),
                encoding=pt.Encoding.PLAIN,
                definition_level_encoding=pt.Encoding.RLE,
                repetition_level_encoding=pt.Encoding.RLE,
            ),
        )
        blob = bytes(ph.to_bytes()) + vals
        out += blob
        flag_cmd = pt.ColumnMetaData(
            type=pt.Type.INT64,
            encoding=[pt.Encoding.PLAIN],
            path_in_schema=["flag"],
            codec=pt.CompressionCodec.UNCOMPRESSED,
            num_values=len(flags),
            total_uncompressed_size=len(blob),
            total_compressed_size=len(blob),
            data_page_offset=flag_off,
        )
        columns.append(pt.ColumnChunk(meta_data=flag_cmd, file_offset=flag_off))
        rgs.append(pt.RowGroup(columns=columns,
                               total_byte_size=total + len(blob),
                               num_rows=len(flags)))
        n_rows += len(flags)
    fmd = pt.FileMetaData(
        version=1,
        schema=schema,
        num_rows=n_rows,
        row_groups=rgs,
        created_by="fastparquet-test",
    )
    foot = bytes(fmd.to_bytes())
    out += foot
    out += struct.pack("<I", len(foot))
    out += b"PAR1"
    with open(fn, "wb") as f:
        f.write(out)


# NULL list, empty list, list with NULL elements, and a row ([6,7,8,9])
# whose elements are split across two data pages
ROWS = [
    [1, 2, 3],
    None,
    [],
    [4, None, 5],
    [6, 7, 8, 9],
    [None, None],
    [10],
    None,
    [],
    [11, None],
]
FLAGS = list(range(len(ROWS)))


def make_pages(rows, split_row=4, split_at=2):
    """One page per row, except split_row, which is broken into two pages
    so that the second page starts in the middle of the row."""
    pages = []
    for i, row in enumerate(rows):
        entries = rows_to_entries([row])
        if i == split_row:
            pages.append(entries[:split_at])
            pages.append(entries[split_at:])
        else:
            pages.append(entries)
    return pages


@pytest.fixture()
def nested_file(tmpdir):
    fn = str(tmpdir.join("nested.parquet"))
    write_nested_file(fn, [(make_pages(ROWS), FLAGS)])
    return fn


@pytest.fixture()
def nested_file_two_rgs(tmpdir):
    fn = str(tmpdir.join("nested2.parquet"))
    write_nested_file(fn, [(make_pages(ROWS[:4]), FLAGS[:4]),
                           (make_pages(ROWS[4:]), FLAGS[4:])])
    return fn


def norm(series):
    return [(None if r is None else list(r)) for r in series.tolist()]


def test_handcrafted_full_read(nested_file):
    # the file itself is readable and preserves NULL/empty/element semantics
    df = fastparquet.ParquetFile(nested_file).to_pandas()
    assert norm(df.nest) == ROWS
    assert df.flag.tolist() == FLAGS


def test_row_window_matches_full_read(nested_file):
    pf = fastparquet.ParquetFile(nested_file)
    full = pf.to_pandas()
    n = len(ROWS)
    for start in range(n + 1):
        for stop in range(start, n + 1):
            out = pf.read_row_window(start, stop, columns=["nest"])
            assert norm(out.nest) == ROWS[start:stop], (start, stop)
            # output carries the actual original row numbers
            assert out.index.tolist() == list(range(start, stop))


def test_row_window_reads_fewer_values(nested_file, monkeypatch):
    # pages fully before the window are not decoded, but their levels are
    # still read to establish row boundaries
    decoded = []
    levels = []
    orig_page = core.read_data_page
    orig_levels = core.read_page_levels

    def spy_page(*args, **kwargs):
        decoded.append(args[2].data_page_header.num_values)
        return orig_page(*args, **kwargs)

    def spy_levels(*args, **kwargs):
        levels.append(args[2].data_page_header.num_values)
        return orig_levels(*args, **kwargs)

    monkeypatch.setattr(core, "read_data_page", spy_page)
    monkeypatch.setattr(core, "read_page_levels", spy_levels)
    pf = fastparquet.ParquetFile(nested_file)
    out = pf.read_row_window(5, 10, columns=["nest"])
    assert norm(out.nest) == ROWS[5:]
    # nest pages: rows 0..3 are one page each (skipped), row 4 is split
    # over two pages (both skipped: the row ends before the window), and
    # rows 5..9 are decoded; the "flag" column is not read at all
    assert len(decoded) == 5
    # levels were consulted for the skipped pages too (row boundaries)
    assert len(levels) == 6 + 5


def test_row_window_first_page_mid_row(nested_file):
    # row 4 spans two pages; a window starting at row 4 must assemble the
    # whole row even though its first page starts mid-row for the window
    pf = fastparquet.ParquetFile(nested_file)
    out = pf.read_row_window(4, 5, columns=["nest"])
    assert norm(out.nest) == [[6, 7, 8, 9]]
    assert out.index.tolist() == [4]
    # and a window starting just after must not pick up the spanned row
    out = pf.read_row_window(5, 6, columns=["nest"])
    assert norm(out.nest) == [[None, None]]
    assert out.index.tolist() == [5]


def test_row_window_flat_column(nested_file):
    pf = fastparquet.ParquetFile(nested_file)
    out = pf.read_row_window(3, 8, columns=["flag"])
    assert out.flag.tolist() == FLAGS[3:8]
    assert out.index.tolist() == list(range(3, 8))
    assert "nest" not in out.columns


def test_row_window_filter_keeps_row_numbers(nested_file):
    # excluding parent rows via another column's predicate must not shift
    # the remaining lists left to fill the gaps
    pf = fastparquet.ParquetFile(nested_file)
    full = pf.to_pandas()
    out = pf.read_row_window(0, len(ROWS), columns=["nest"],
                             filters=[("flag", ">", 4)])
    expected = full[full.flag > 4]
    assert out.index.tolist() == expected.index.tolist()
    assert norm(out.nest) == norm(expected.nest)
    # the kept rows are not contiguous: no left-shifting happened
    assert out.index.tolist() != list(range(len(out)))


def test_row_window_filter_within_window(nested_file):
    pf = fastparquet.ParquetFile(nested_file)
    full = pf.to_pandas()
    out = pf.read_row_window(2, 9, filters=[("flag", "<", 6)])
    expected = full.iloc[2:9]
    expected = expected[expected.flag < 6]
    assert out.index.tolist() == expected.index.tolist()
    assert norm(out.nest) == norm(expected.nest)
    assert out.flag.tolist() == expected.flag.tolist()


def test_row_window_across_row_groups(nested_file_two_rgs):
    pf = fastparquet.ParquetFile(nested_file_two_rgs)
    assert len(pf.row_groups) == 2
    full = pf.to_pandas()
    assert norm(full.nest) == ROWS
    for start, stop in [(0, 10), (3, 5), (4, 8), (2, 6), (7, 10)]:
        out = pf.read_row_window(start, stop)
        assert norm(out.nest) == ROWS[start:stop], (start, stop)
        assert out.flag.tolist() == FLAGS[start:stop]
        # row numbers are global to the dataset, not per row-group
        assert out.index.tolist() == list(range(start, stop))


def test_row_window_empty_and_invalid(nested_file):
    pf = fastparquet.ParquetFile(nested_file)
    out = pf.read_row_window(4, 4, columns=["nest"])
    assert len(out) == 0
    assert out.index.tolist() == []
    with pytest.raises(ValueError):
        pf.read_row_window(-1, 3)
    with pytest.raises(ValueError):
        pf.read_row_window(5, 3)
    with pytest.raises(ValueError):
        pf.read_row_window(0, len(ROWS) + 1)


def test_row_window_dict_encoded_nested():
    # pre-existing test data: dict-encoded strings, four-level list
    import os
    fn = os.path.join(os.path.dirname(__file__), "..", "..",
                      "test-data", "nested.parq")
    pf = fastparquet.ParquetFile(fn)
    full = pf.to_pandas()
    for start, stop in [(0, 10), (2, 7), (5, 6), (9, 10)]:
        out = pf.read_row_window(start, stop)
        got = [(None if r is None else list(r))
               for r in out["nest.thing"].tolist()]
        want = [(None if r is None else list(r))
                for r in full["nest.thing"].iloc[start:stop].tolist()]
        assert got == want, (start, stop)
        assert out.index.tolist() == list(range(start, stop))


def test_row_window_datapage_v2_unsupported(tmpdir, monkeypatch):
    from fastparquet import writer
    monkeypatch.setattr(writer, "DATAPAGE_VERSION", 2)
    fn = str(tmpdir.join("v2.parquet"))
    df = pd.DataFrame({"a": range(10)})
    fastparquet.write(fn, df, row_group_offsets=[0])
    pf = fastparquet.ParquetFile(fn)
    with pytest.raises(NotImplementedError):
        pf.read_row_window(2, 5)
