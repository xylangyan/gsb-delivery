"""Tests for byte-range planning and filtered execution.

The fixtures hand-build parquet files with full control over page boundaries
and page-level statistics, so that the tricky cases are reproducible without
relying on a writer that emits page statistics:

* one dictionary-encoded column whose two-plus candidate data pages share a
  single preceding dictionary page and carry no page statistics;
* aligned pages of a predicate column that do carry statistics, so the
  statistics-less pages can only be skipped via the cross-column row mask;
* a second row group that row-group statistics can exclude outright.
"""
import struct

import numpy as np
import pandas as pd
import pytest

from fastparquet import parquet_thrift, writer
from fastparquet.api import ParquetFile
from fastparquet.cencoding import ThriftObject
from fastparquet.range_plan import (
    REQUIRED, LOCATING, SKIPPED,
    build_range_plan, execute_range_plan, scan_column_pages,
)
from fastparquet.writer import (
    MARKER, encode_dict, encode_plain, write_thrift,
)

from .util import tempdir  # noqa: F401  (registers the pytest fixture)

PT = parquet_thrift.Type
PAGE_SIZE = 10
LABELS = ["click", "view", "buy", "noop"]


def _int_stats(values):
    arr = np.asarray(values, dtype="int64")
    return parquet_thrift.Statistics(
        max=arr.max().astype("<i8").tobytes(),
        min=arr.min().astype("<i8").tobytes(),
        null_count=0,
    )


def _pages(values, page_size=PAGE_SIZE):
    return [values[i:i + page_size] for i in range(0, len(values), page_size)]


def _write_page_header(f, header):
    write_thrift(f, header)


def _data_page_header(num_values, encoding, stats):
    return parquet_thrift.DataPageHeader(
        num_values=num_values,
        encoding=encoding,
        definition_level_encoding=parquet_thrift.Encoding.RLE,
        repetition_level_encoding=parquet_thrift.Encoding.BIT_PACKED,
        statistics=stats,
        i32=1,
    )


def _write_plain_int_column(f, se, values, page_stats):
    start = f.tell()
    for page_values, stats in zip(_pages(values), page_stats):
        payload = np.asarray(page_values, dtype="<i8").tobytes()
        header = parquet_thrift.PageHeader(
            type=parquet_thrift.PageType.DATA_PAGE,
            uncompressed_page_size=len(payload),
            compressed_page_size=len(payload),
            data_page_header=_data_page_header(
                len(page_values), parquet_thrift.Encoding.PLAIN, stats),
            i32=1,
        )
        _write_page_header(f, header)
        f.write(payload)
    size = f.tell() - start
    cmd = ThriftObject.from_fields(
        "ColumnMetaData",
        type=PT.INT64,
        encodings=[parquet_thrift.Encoding.PLAIN],
        codec=parquet_thrift.CompressionCodec.UNCOMPRESSED,
        path_in_schema=[se.name],
        num_values=len(values),
        statistics=_int_stats(values),
        data_page_offset=start,
        total_uncompressed_size=size,
        total_compressed_size=size,
        i32list=[1, 4],
    )
    return parquet_thrift.ColumnChunk(file_offset=start, meta_data=cmd)


def _write_dict_string_column(f, se, labels, codes):
    """UTF8 dictionary column; data pages deliberately have no statistics."""
    start = f.tell()
    dict_payload = encode_plain(pd.Series(labels, dtype=object), se)
    dict_header = parquet_thrift.PageHeader(
        type=parquet_thrift.PageType.DICTIONARY_PAGE,
        uncompressed_page_size=len(dict_payload),
        compressed_page_size=len(dict_payload),
        dictionary_page_header=parquet_thrift.DictionaryPageHeader(
            num_values=len(labels),
            encoding=parquet_thrift.Encoding.PLAIN,
            i32=1),
        i32=1,
    )
    _write_page_header(f, dict_header)
    f.write(dict_payload)
    data_page_offset = f.tell()

    for page_codes in _pages(codes):
        payload = encode_dict(pd.Series(page_codes, dtype="int8"), se)
        header = parquet_thrift.PageHeader(
            type=parquet_thrift.PageType.DATA_PAGE,
            uncompressed_page_size=len(payload),
            compressed_page_size=len(payload),
            data_page_header=_data_page_header(
                len(page_codes),
                parquet_thrift.Encoding.RLE_DICTIONARY, None),
            i32=1,
        )
        _write_page_header(f, header)
        f.write(payload)

    size = f.tell() - start
    cmd = ThriftObject.from_fields(
        "ColumnMetaData",
        type=PT.BYTE_ARRAY,
        encodings=[parquet_thrift.Encoding.PLAIN,
                   parquet_thrift.Encoding.RLE_DICTIONARY],
        codec=parquet_thrift.CompressionCodec.UNCOMPRESSED,
        path_in_schema=[se.name],
        num_values=len(codes),
        statistics=parquet_thrift.Statistics(null_count=0),
        data_page_offset=data_page_offset,
        dictionary_page_offset=start,
        total_uncompressed_size=size,
        total_compressed_size=size,
        i32list=[1, 4],
    )
    return parquet_thrift.ColumnChunk(file_offset=start, meta_data=cmd)


def _build_controlled_file(path, groups):
    """groups: list of dicts with keys id, score, codes (each len multiple of 10)."""
    first = groups[0]
    schema_frame = pd.DataFrame({
        "id": np.asarray(first["id"], dtype="int64"),
        "score": np.asarray(first["score"], dtype="int64"),
        "event": pd.Series([LABELS[c] for c in first["codes"]], dtype=object),
    })
    fmd = writer.make_metadata(
        schema_frame, has_nulls=False,
        object_encoding={"event": "utf8"})
    schema = fmd.schema
    se_by_name = {se.name: se for se in schema if hasattr(se, "name")}

    row_groups = []
    total_rows = 0
    with open(path, "wb") as f:
        f.write(MARKER)
        for group in groups:
            ids, scores, codes = group["id"], group["score"], group["codes"]
            chunk_id = _write_plain_int_column(
                f, se_by_name["id"], ids,
                [_int_stats(page) for page in _pages(ids)])
            chunk_score = _write_plain_int_column(
                f, se_by_name["score"], scores,
                [_int_stats(page) for page in _pages(scores)])
            chunk_event = _write_dict_string_column(
                f, se_by_name["event"], LABELS, codes)
            chunks = [chunk_id, chunk_score, chunk_event]
            num_rows = len(ids)
            total_rows += num_rows
            row_groups.append(parquet_thrift.RowGroup(
                num_rows=num_rows, columns=chunks,
                total_byte_size=sum(
                    c.meta_data.total_uncompressed_size for c in chunks)))

        fmd.row_groups = row_groups
        fmd.num_rows = total_rows
        footer = bytes(fmd.to_bytes())
        f.write(footer)
        f.write(struct.pack("<I", len(footer)))
        f.write(MARKER)


@pytest.fixture()
def dataset(tempdir):
    path = tempdir + "/range_plan.parquet"
    groups = [
        {"id": list(range(30)),
         "score": list(range(30)),
         "codes": [i % 4 for i in range(30)]},
        {"id": list(range(100, 130)),
         "score": list(range(100, 130)),
         "codes": [(i + 1) % 4 for i in range(30)]},
    ]
    _build_controlled_file(path, groups)
    frames = []
    for group in groups:
        frames.append(pd.DataFrame({
            "id": group["id"],
            "score": group["score"],
            "event": [LABELS[c] for c in group["codes"]],
        }))
    return path, pd.concat(frames, ignore_index=True)


def _chunk(plan, rg_idx, column):
    matches = [c for c in plan.chunk_plans
               if c.row_group == rg_idx and c.column == column]
    assert len(matches) == 1
    return matches[0]


def test_file_is_readable_by_fastparquet(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    loaded = pf.to_pandas()
    pd.testing.assert_frame_equal(
        loaded[["id", "score", "event"]].reset_index(drop=True),
        frame.reset_index(drop=True),
        check_dtype=False,
    )


def test_scan_reports_pages_and_offsets(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    rg = pf.row_groups[0]
    event = next(c for c in rg.columns
                 if ".".join(c.meta_data.path_in_schema) == "event")
    pages = scan_column_pages(pf, path, event)
    kinds = [p.kind for p in pages]
    assert kinds[0] == "dictionary"
    assert kinds[1:] == ["data_page", "data_page", "data_page"]
    assert [p.num_values for p in pages[1:]] == [10, 10, 10]
    assert [p.row_start for p in pages[1:]] == [0, 10, 20]
    assert all(not p.has_statistics for p in pages[1:])
    assert pages[0].offset == event.meta_data.dictionary_page_offset


def test_scan_grows_window_until_header_complete(dataset, monkeypatch):
    import fastparquet.range_plan as rp
    path, _ = dataset
    pf = ParquetFile(path)
    rg = pf.row_groups[0]
    ident = next(c for c in rg.columns
                 if ".".join(c.meta_data.path_in_schema) == "id")
    monkeypatch.setattr(rp, "_HEADER_WINDOW_START", 8)
    pages = scan_column_pages(pf, path, ident)
    assert [p.num_values for p in pages] == [10, 10, 10]
    assert [p.offset for p in pages] == [4, 127, 250]
    assert pages[0].end == 127


def test_plan_classifies_required_locating_skipped(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    plan = build_range_plan(
        pf, columns=["id", "event"],
        filters=[[("score", ">=", 15)]], request_overhead=64)

    score = _chunk(plan, 0, "score")
    assert [p.role for p in score.pages] == [SKIPPED, LOCATING, LOCATING]
    assert all("locate" in p.reason for p in score.pages[1:])

    event = _chunk(plan, 0, "event")
    roles = [p.role for p in event.pages]
    assert roles == [REQUIRED, SKIPPED, REQUIRED, REQUIRED]
    skipped_page = event.pages[1]
    assert not skipped_page.has_statistics
    assert "no statistics" in skipped_page.reason
    assert "aligned" in skipped_page.reason

    ident = _chunk(plan, 0, "id")
    assert [p.role for p in ident.pages] == [SKIPPED, REQUIRED, REQUIRED]

    assert {r.role for r in plan.required_ranges} == {REQUIRED}
    assert {r.role for r in plan.locating_ranges} == {LOCATING}
    assert {r.role for r in plan.skipped_ranges} == {SKIPPED}
    assert plan.estimated_cost == plan.fetched_bytes + \
        plan.num_requests * 64


def test_missing_statistics_never_implies_empty(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    plan = build_range_plan(
        pf, columns=["id"],
        filters=[[("event", "==", "click")]])
    event = _chunk(plan, 0, "event")
    data_pages = [p for p in event.pages if p.kind != "dictionary"]
    assert all(p.role == LOCATING for p in data_pages)
    assert all(not p.has_statistics for p in data_pages)
    assert all("cannot be assumed empty" in p.reason for p in data_pages)
    assert event.pages[0].role == LOCATING
    assert all(p.role != SKIPPED for p in data_pages)


def test_dictionary_page_billed_once(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    plan = build_range_plan(
        pf, columns=["id", "event"],
        filters=[[("score", ">=", 15)]])
    event = _chunk(plan, 0, "event")
    dict_offset = event.pages[0].offset
    dict_end = event.pages[0].end
    covering = [r for r in plan.fetch_ranges
                if r.offset <= dict_offset and r.end >= dict_end]
    assert len(covering) == 1


def test_request_budget_coalesces_ranges(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    natural = build_range_plan(
        pf, columns=["id", "event"],
        filters=[[("score", ">=", 15)]], request_limit=100)
    tight = build_range_plan(
        pf, columns=["id", "event"],
        filters=[[("score", ">=", 15)]], request_limit=2)
    assert natural.num_requests > 2
    assert tight.num_requests <= 2
    assert tight.coalesced
    assert tight.coalesced_extra_bytes > 0
    natural_union = sum(r.length for r in
                        _merge(natural.required_ranges + natural.locating_ranges))
    tight_union = sum(r.length for r in
                      _merge(tight.required_ranges + tight.locating_ranges))
    assert tight_union >= natural_union


def _merge(ranges):
    ordered = sorted(ranges, key=lambda r: r.offset)
    out = []
    for rng in ordered:
        if out and rng.offset <= out[-1].end:
            prev = out[-1]
            out[-1] = type(rng)(prev.offset,
                                max(prev.end, rng.end) - prev.offset,
                                prev.role, prev.reason)
        else:
            out.append(rng)
    return out


def test_row_group_statistics_skip_whole_group(dataset):
    path, _ = dataset
    pf = ParquetFile(path)
    plan = build_range_plan(
        pf, columns=["id", "event"],
        filters=[[("score", "<", 50)]])
    rg1 = {c.column: c for c in plan.chunk_plans if c.row_group == 1}
    assert all(c.role == SKIPPED for c in rg1.values())
    for chunk in rg1.values():
        assert all(p.role == SKIPPED for p in chunk.pages)
    rg0 = {c.column: c for c in plan.chunk_plans if c.row_group == 0}
    assert any(c.role != SKIPPED for c in rg0.values())


def test_execution_matches_full_read_and_keeps_row_numbers(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    filters = [[("score", ">=", 15)]]
    plan = build_range_plan(pf, columns=["id", "event"], filters=filters)

    result = execute_range_plan(pf, plan)
    expected = frame.loc[frame["score"] >= 15, ["id", "event"]]

    assert list(result.columns) == ["id", "event"]
    assert result.index.tolist() == expected.index.tolist()
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_dtype=False,
    )


def test_execution_filter_on_unstatisticed_dict_column(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    filters = [[("event", "==", "click")]]
    plan = build_range_plan(pf, columns=["id", "score"], filters=filters)
    result = execute_range_plan(pf, plan)
    expected = frame.loc[frame["event"] == "click", ["id", "score"]]
    assert result.index.tolist() == expected.index.tolist()
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_dtype=False,
    )


def test_execution_with_or_of_ands(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    filters = [[("score", ">=", 25)], [("event", "==", "buy")]]
    plan = build_range_plan(pf, columns=["id"], filters=filters)
    result = execute_range_plan(pf, plan)
    wanted = (frame["score"] >= 25) | (frame["event"] == "buy")
    expected = frame.loc[wanted, ["id"]]
    assert result.index.tolist() == expected.index.tolist()
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_dtype=False,
    )


def test_execution_uses_only_planned_bytes(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    filters = [[("score", ">=", 15)]]
    plan = build_range_plan(pf, columns=["id", "event"], filters=filters)
    allowed = []
    for ranges in plan.fetch_ranges_by_file.values():
        for rng in ranges:
            allowed.append((rng.offset, rng.end))

    def guarded_reader(pf_obj, file_path, ranges):
        with open(file_path, "rb") as f:
            out = []
            for rng in ranges:
                assert any(start <= rng.offset and rng.end <= end
                           for start, end in allowed)
                f.seek(rng.offset)
                out.append((rng.offset, f.read(rng.length)))
        return out

    result = execute_range_plan(pf, plan, range_reader=guarded_reader)
    assert result.index.tolist() == \
        frame.index[frame["score"] >= 15].tolist()


def test_plan_against_writer_output(tempdir):
    path = tempdir + "/written.parquet"
    frame = pd.DataFrame({
        "id": np.arange(60, dtype="int64"),
        "event": pd.Categorical(
            [LABELS[i % 3] for i in range(60)]),
    })
    writer.write(path, frame, stats=True)
    pf = ParquetFile(path)
    filters = [[("event", "==", "click")]]
    plan = build_range_plan(pf, columns=["id"], filters=filters)
    event = _chunk(plan, 0, "event")
    assert all(p.role != SKIPPED for p in event.pages)
    result = execute_range_plan(pf, plan)
    wanted = frame.index[frame["event"] == "click"]
    assert result.index.tolist() == wanted.tolist()
    expected = frame.loc[frame["event"] == "click", ["id"]]
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_dtype=False,
    )


def test_nullable_and_plain_string_columns_round_trip(tempdir):
    path = tempdir + "/nullable.parquet"
    scores = [float(i) if i % 7 else None for i in range(60)]
    frame = pd.DataFrame({
        "id": np.arange(60, dtype="int64"),
        "score": pd.Series(scores, dtype="float64"),
        "name": ["row-%02d" % i for i in range(60)],
    })
    old = writer.MAX_PAGE_SIZE
    writer.MAX_PAGE_SIZE = 200
    try:
        writer.write(path, frame, stats=True)
    finally:
        writer.MAX_PAGE_SIZE = old
    pf = ParquetFile(path)
    filters = [[("score", ">=", 20.0)]]
    plan = build_range_plan(pf, columns=["id", "name"], filters=filters)
    result = execute_range_plan(pf, plan)

    wanted = frame.index[frame["score"] >= 20.0]
    assert result.index.tolist() == wanted.tolist()
    expected = frame.loc[frame["score"] >= 20.0, ["id", "name"]]
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_dtype=False,
    )
    # null rows must never satisfy the predicate and must not appear
    assert frame["score"].isna().sum() > 0
    assert result.index.isin(frame.index[frame["score"].isna()]).sum() == 0


def test_no_filters_projects_every_kept_page(dataset):
    path, frame = dataset
    pf = ParquetFile(path)
    plan = build_range_plan(pf, columns=["id", "event"])
    assert not plan.skipped_ranges
    assert all(r.role != SKIPPED
               for chunk in plan.chunk_plans for r in [p.range for p in chunk.pages])
    result = execute_range_plan(pf, plan)
    assert result.index.tolist() == frame.index.tolist()
    pd.testing.assert_frame_equal(
        result.reset_index(drop=True),
        frame[["id", "event"]].reset_index(drop=True),
        check_dtype=False,
    )
