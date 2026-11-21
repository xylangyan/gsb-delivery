"""
Tests for the row by row streaming workbook reader.

The scenario uses workbooks whose physical sheet order is
Archive, Nov_A, Skip, Nov_B.  Archive is a deliberately large sheet that
must only contribute its title (sheet metadata), never its rows.  Every
assertion about row reads is observable through a counting row iterator.
"""

import threading
from io import BytesIO
from types import GeneratorType

import psutil
import pytest
from openpyxl import Workbook

import pyexcel as pe
from pyexcel.exceptions import SheetNotFound
from pyexcel.internal.rowstream import open_row_book_stream
from pyexcel_xlsx.xlsxr import FastSheet

ARCHIVE_ROWS = 500
NOV_A_ROWS = [["Nov_A_header"], ["Nov_A_r1"], ["Nov_A_r2"]]
SKIP_ROWS = [["skip_header"], ["skip_r1"]]
NOV_B_ROWS = [
    ["Nov_B_header"],
    ["Nov_B_r1"],
    ["Nov_B_r2"],
    ["Nov_B_r3"],
]
WORKBOOK_ORDER = ["Archive", "Nov_A", "Skip", "Nov_B"]


def _build_workbook(path, prefix=""):
    workbook = Workbook()
    archive = workbook.active
    archive.title = "Archive"
    for index in range(ARCHIVE_ROWS):
        archive.append(["%sarchive_%d" % (prefix, index)])
    nov_a = workbook.create_sheet("Nov_A")
    for row in NOV_A_ROWS:
        nov_a.append([prefix + row[0]])
    skip = workbook.create_sheet("Skip")
    for row in SKIP_ROWS:
        skip.append([prefix + row[0]])
    nov_b = workbook.create_sheet("Nov_B")
    for row in NOV_B_ROWS:
        nov_b.append([prefix + row[0]])
    workbook.save(str(path))


@pytest.fixture
def workbook_files(tmp_path):
    first = tmp_path / "roster_one.xlsx"
    second = tmp_path / "roster_two.xlsx"
    _build_workbook(first)
    _build_workbook(second, prefix="wb2_")
    return first, second


@pytest.fixture
def row_counter(monkeypatch):
    counts = {}
    lock = threading.Lock()
    original = FastSheet.row_iterator

    def counting_row_iterator(self):
        for row in original(self):
            with lock:
                title = self.xlsx_sheet.title
                counts[title] = counts.get(title, 0) + 1
            yield row

    monkeypatch.setattr(FastSheet, "row_iterator", counting_row_iterator)
    return counts


def _held_handles(path):
    proc = psutil.Process()
    return [f.path for f in proc.open_files() if f.path == str(path)]


def _fail_after_rows(monkeypatch, fail_at):
    original = FastSheet.row_iterator
    state = {"yielded": 0, "sheets": []}
    failure = RuntimeError("simulated page read failure")

    def failing_row_iterator(self):
        state["sheets"].append(self.xlsx_sheet.title)
        yielded = 0
        for row in original(self):
            if yielded == fail_at:
                raise failure
            yielded += 1
            state["yielded"] = yielded
            yield row

    monkeypatch.setattr(FastSheet, "row_iterator", failing_row_iterator)
    return state


def test_metadata_only_for_unselected_and_large_sheets(
    workbook_files, row_counter
):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_B", "Nov_A"]
    )
    try:
        # Request order is reversed, delivery follows the workbook order.
        assert handle.sheet_names() == ["Nov_A", "Nov_B"]
        assert handle.all_sheet_names() == WORKBOOK_ORDER
        assert handle.number_of_sheets() == 2
        with pytest.raises(KeyError):
            handle["Archive"]
        with pytest.raises(KeyError):
            handle["Skip"]
        # Opening and name enumeration consume no data rows at all.
        assert row_counter == {}
    finally:
        handle.close()
    assert _held_handles(first) == []


def test_first_row_then_early_stop_releases_the_read(
    workbook_files, row_counter
):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_B", "Nov_A"]
    )
    payload = handle["Nov_A"].payload
    assert next(payload) == NOV_A_ROWS[0]
    handle.close()

    assert row_counter == {"Nov_A": 1}
    # The abandoned generator is closed instead of lingering.
    assert list(payload) == []
    assert _held_handles(first) == []


def test_unopened_selected_sheet_consumes_no_rows(
    workbook_files, row_counter
):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A", "Nov_B"]
    )
    try:
        assert list(handle["Nov_A"].payload) == NOV_A_ROWS
        # Nov_B was never iterated and Archive stays title only.
        assert row_counter == {"Nov_A": len(NOV_A_ROWS)}
    finally:
        handle.close()
    assert _held_handles(first) == []


def test_full_delivery_of_both_selected_sheets(workbook_files, row_counter):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_B", "Nov_A"]
    )
    try:
        assert list(handle["Nov_A"].payload) == NOV_A_ROWS
        assert list(handle["Nov_B"].payload) == NOV_B_ROWS
        assert row_counter == {
            "Nov_A": len(NOV_A_ROWS),
            "Nov_B": len(NOV_B_ROWS),
        }
        assert "Archive" not in row_counter
        assert "Skip" not in row_counter
    finally:
        handle.close()
    assert _held_handles(first) == []


def test_rows_are_generators_not_a_loaded_book(workbook_files):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    )
    try:
        sheet = handle["Nov_A"]
        assert isinstance(sheet.payload, GeneratorType)
        assert iter(sheet.payload) is sheet.payload
    finally:
        handle.close()


def test_exception_mid_page_from_path_releases_handle(
    workbook_files, monkeypatch
):
    first, _ = workbook_files
    state = _fail_after_rows(monkeypatch, fail_at=1)
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A", "Nov_B"]
    )
    with pytest.raises(RuntimeError, match="simulated page read failure"):
        list(handle["Nov_A"].payload)
    handle.close()

    # Only Nov_A was touched and one row was delivered before the fault.
    assert state["yielded"] == 1
    assert state["sheets"] == ["Nov_A"]
    assert _held_handles(first) == []


def test_exception_mid_page_with_opened_stream_keeps_caller_stream(
    workbook_files, monkeypatch
):
    first, _ = workbook_files
    state = _fail_after_rows(monkeypatch, fail_at=1)
    caller_stream = open(str(first), "rb")
    try:
        handle = open_row_book_stream(
            file_stream=caller_stream,
            file_type="xlsx",
            sheet_names=["Nov_A"],
        )
        with pytest.raises(RuntimeError):
            list(handle["Nov_A"].payload)
        handle.close()

        assert state["yielded"] == 1
        # The caller owns the stream and can still seek and read it.
        assert not caller_stream.closed
        caller_stream.seek(0)
        assert caller_stream.read(2) == b"PK"
    finally:
        caller_stream.close()
    assert caller_stream.closed


def test_opened_stream_full_read_stays_usable_after_close(
    workbook_files, row_counter
):
    first, _ = workbook_files
    caller_stream = open(str(first), "rb")
    try:
        handle = open_row_book_stream(
            file_stream=caller_stream,
            file_type="xlsx",
            sheet_names=["Nov_B", "Nov_A"],
        )
        try:
            assert list(handle["Nov_A"].payload) == NOV_A_ROWS
            assert list(handle["Nov_B"].payload) == NOV_B_ROWS
        finally:
            handle.close()
        assert not caller_stream.closed
        caller_stream.seek(0)
        assert caller_stream.read(2) == b"PK"
    finally:
        caller_stream.close()
    assert _held_handles(first) == []


def test_bytes_io_stream_is_not_closed_by_handle(workbook_files):
    first, _ = workbook_files
    raw = first.read_bytes()
    caller_stream = BytesIO(raw)
    handle = open_row_book_stream(
        file_stream=caller_stream,
        file_type="xlsx",
        sheet_names=["Nov_A"],
    )
    try:
        assert list(handle["Nov_A"].payload) == NOV_A_ROWS
    finally:
        handle.close()
    assert not caller_stream.closed
    caller_stream.seek(0)
    assert caller_stream.read(2) == b"PK"


def test_parallel_reader_continues_after_other_read_is_closed(
    workbook_files, row_counter
):
    first, second = workbook_files
    early_handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    )
    other_handle = open_row_book_stream(
        file_name=str(second), sheet_names=["Nov_A", "Nov_B"]
    )
    try:
        assert next(early_handle["Nov_A"].payload) == NOV_A_ROWS[0]
        early_handle.close()
        assert list(early_handle["Nov_A"].payload) == []

        # The second workbook read is untouched by the first close.
        assert list(other_handle["Nov_A"].payload) == [
            ["wb2_" + row[0]] for row in NOV_A_ROWS
        ]
        assert list(other_handle["Nov_B"].payload) == [
            ["wb2_" + row[0]] for row in NOV_B_ROWS
        ]
    finally:
        other_handle.close()
    assert _held_handles(first) == []
    assert _held_handles(second) == []


def test_two_handles_on_same_file_keep_independent_resources(
    workbook_files, row_counter
):
    first, _ = workbook_files
    first_handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    )
    second_handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_B"]
    )
    try:
        assert next(first_handle["Nov_A"].payload) == NOV_A_ROWS[0]
        first_handle.close()
        # A second read of the same file is unaffected by the first close.
        assert list(second_handle["Nov_B"].payload) == NOV_B_ROWS
        assert row_counter == {
            "Nov_A": 1,
            "Nov_B": len(NOV_B_ROWS),
        }
    finally:
        second_handle.close()
    assert _held_handles(first) == []


def test_concurrent_reads_in_threads_keep_independent_resources(
    workbook_files, row_counter
):
    first, second = workbook_files
    started = threading.Event()
    release_worker = threading.Event()
    failures = []

    def worker():
        try:
            handle = open_row_book_stream(
                file_name=str(second), sheet_names=["Nov_A"]
            )
            started.set()
            release_worker.wait(timeout=5)
            try:
                assert list(handle["Nov_A"].payload) == [
                    ["wb2_" + row[0]] for row in NOV_A_ROWS
                ]
            finally:
                handle.close()
        except Exception as exc:  # pragma: no cover - failure reporter
            failures.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(timeout=5)

    main_handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    )
    try:
        assert next(main_handle["Nov_A"].payload) == NOV_A_ROWS[0]
    finally:
        main_handle.close()
    release_worker.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert _held_handles(first) == []
    assert _held_handles(second) == []


def test_context_manager_closes_on_exception(workbook_files):
    first, _ = workbook_files
    with pytest.raises(ValueError):
        with open_row_book_stream(
            file_name=str(first), sheet_names=["Nov_A"]
        ) as handle:
            raise ValueError("consumer aborted")
    assert _held_handles(first) == []
    assert list(handle["Nov_A"].payload) == []


def test_missing_sheet_name_releases_resource(workbook_files):
    first, _ = workbook_files
    with pytest.raises(SheetNotFound):
        open_row_book_stream(
            file_name=str(first), sheet_names=["Nov_A", "Nope"]
        )
    assert _held_handles(first) == []


def test_missing_sheet_index_releases_resource(workbook_files):
    first, _ = workbook_files
    with pytest.raises(SheetNotFound):
        open_row_book_stream(file_name=str(first), sheet_indexes=[9])
    assert _held_handles(first) == []


def test_sheet_indexes_follow_workbook_order(workbook_files, row_counter):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_indexes=[3, 1]
    )
    try:
        assert handle.sheet_names() == ["Nov_A", "Nov_B"]
        assert handle.all_sheet_names() == WORKBOOK_ORDER
        assert list(handle["Nov_B"].payload) == NOV_B_ROWS
        assert row_counter == {"Nov_B": len(NOV_B_ROWS)}
    finally:
        handle.close()
    assert _held_handles(first) == []


def test_requires_a_source():
    with pytest.raises(ValueError):
        open_row_book_stream(sheet_names=["Nov_A"])


def test_rejects_two_sources(workbook_files):
    first, _ = workbook_files
    with pytest.raises(ValueError):
        open_row_book_stream(
            file_name=str(first),
            file_stream=BytesIO(b""),
            file_type="xlsx",
        )


def test_corrupt_path_does_not_leak_handle(tmp_path):
    corrupt = tmp_path / "broken.xlsx"
    corrupt.write_bytes(b"not a real workbook")
    with pytest.raises(Exception):
        open_row_book_stream(
            file_name=str(corrupt), sheet_names=["Nov_A"]
        )
    assert _held_handles(corrupt) == []


def test_corrupt_caller_stream_stays_open(tmp_path):
    corrupt = tmp_path / "broken.xlsx"
    corrupt.write_bytes(b"not a real workbook")
    caller_stream = open(str(corrupt), "rb")
    try:
        with pytest.raises(Exception):
            open_row_book_stream(
                file_stream=caller_stream,
                file_type="xlsx",
                sheet_names=["Nov_A"],
            )
        assert not caller_stream.closed
    finally:
        caller_stream.close()


def test_double_close_is_safe(workbook_files):
    first, _ = workbook_files
    handle = open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    )
    assert next(handle["Nov_A"].payload) == NOV_A_ROWS[0]
    handle.close()
    handle.close()
    assert _held_handles(first) == []


def test_context_manager_delivers_and_releases(workbook_files):
    first, _ = workbook_files
    with open_row_book_stream(
        file_name=str(first), sheet_names=["Nov_A"]
    ) as handle:
        assert list(handle["Nov_A"].payload) == NOV_A_ROWS
    assert _held_handles(first) == []


def test_public_api_exported():
    assert pe.open_row_book_stream is open_row_book_stream
    assert pe.RowBookStream.__name__ == "RowBookStream"
    assert pe.SheetNotFound is SheetNotFound
