import os

import pytest

import pyexcel as p
import pyexcel.publisher as publisher_mod
from pyexcel.internal.garbagecollector import GARBAGE
from pyexcel.internal.generators import SheetStream
from pyexcel.publisher import (
    PublicationError,
    SheetPublication,
    STAGE_CONVERSION,
    STAGE_PUBLICATION,
    publish_sheets,
)

SELECTED = ["Alpha", "Bravo", "Charlie"]
UNSELECTED = "Delta"


def _make_workbook(path):
    data = {
        "Alpha": [["id", "name"], [1, "a"], [2, "b"], [3, "c"]],
        "Bravo": [["x", "y"], [10, 20], [30, 40]],
        "Charlie": [["n"]] + [[index] for index in range(500)],
        "Delta": [["skip"], [8], [9]],
    }
    p.save_book_as(bookdict=data, dest_file_name=str(path))
    return data


def _snapshot(output_dir):
    result = {}
    for entry in os.listdir(str(output_dir)):
        with open(os.path.join(str(output_dir), entry), "rb") as handle:
            result[entry] = handle.read()
    return result


def _private_leftovers(output_dir):
    return [
        entry
        for entry in os.listdir(str(output_dir))
        if entry.startswith(".") and entry.endswith(".pubtmp")
    ]


def _setup(output_dir, workbook):
    targets = {
        name: os.path.join(str(output_dir), f"{name}.csv")
        for name in SELECTED
    }
    with open(targets["Alpha"], "wb") as handle:
        handle.write(b"id,name\r\n1,old-alpha\r\n")
    with open(targets["Bravo"], "wb") as handle:
        handle.write(b"x,y\r\n0,old-bravo\r\n")
    unselected = os.path.join(str(output_dir), f"{UNSELECTED}.csv")
    with open(unselected, "wb") as handle:
        handle.write(b"skip\r\nold-delta\r\n")
    unrelated = os.path.join(str(output_dir), "notes.txt")
    with open(unrelated, "wb") as handle:
        handle.write(b"downstream notes, do not touch\n")
    selections = [
        SheetPublication(name, targets[name]) for name in SELECTED
    ]
    return targets, selections, unselected, unrelated


def test_publish_sheets_transactional_lifecycle(tmp_path, monkeypatch):
    output_dir = tmp_path / "downstream"
    output_dir.mkdir()
    workbook = tmp_path / "monthly_inventory.xlsx"
    data = _make_workbook(workbook)
    targets, selections, unselected, unrelated = _setup(
        output_dir, workbook
    )

    before = _snapshot(output_dir)
    assert not os.path.exists(targets["Charlie"])

    # Scenario 1: conversion of the last selected sheet fails while its
    # rows are streamed into the real csv writer.
    real_iget_book = publisher_mod.iget_book

    def broken_iget_book(*args, **kwargs):
        book_stream = real_iget_book(*args, **kwargs)
        for name in SELECTED:
            original_payload = book_stream.sheets[name].payload
            book_stream.sheets[name] = SheetStream(
                name, original_payload
            )

        def broken_payload():
            raise RuntimeError("simulated conversion failure")
            yield  # pragma: no cover

        book_stream.sheets["Charlie"] = SheetStream(
            "Charlie", broken_payload()
        )
        return book_stream

    monkeypatch.setattr(
        publisher_mod, "iget_book", broken_iget_book
    )
    with pytest.raises(PublicationError) as exc_info:
        publish_sheets(str(workbook), selections)
    error = exc_info.value
    assert error.stage == STAGE_CONVERSION
    assert error.sheet_name == "Charlie"
    assert "simulated conversion failure" in str(error)
    assert _snapshot(output_dir) == before
    assert _private_leftovers(output_dir) == []
    assert len(GARBAGE) == 0

    # Scenario 2: all sheets convert, replacing the last target fails.
    monkeypatch.undo()
    real_replace = os.replace

    def failing_replace(source, destination, *args, **kwargs):
        if os.path.basename(destination) == "Charlie.csv":
            raise OSError("simulated target replacement failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "pyexcel.publisher.os.replace", failing_replace
    )
    with pytest.raises(PublicationError) as exc_info:
        publish_sheets(str(workbook), selections)
    error = exc_info.value
    assert error.stage == STAGE_PUBLICATION
    assert error.sheet_name == "Charlie"
    assert "simulated target replacement" in str(error)
    assert _snapshot(output_dir) == before
    assert not os.path.exists(targets["Charlie"])
    assert _private_leftovers(output_dir) == []
    assert len(GARBAGE) == 0

    # Scenario 3: fix the cause and rerun the same input successfully.
    monkeypatch.undo()
    returned = publish_sheets(str(workbook), selections)
    assert returned == [targets[name] for name in SELECTED]
    assert _private_leftovers(output_dir) == []
    assert len(GARBAGE) == 0

    expected_book = p.get_book(file_name=str(workbook))
    for name in SELECTED:
        target = targets[name]
        assert os.path.exists(target)
        on_disk = open(target, "rb").read()
        ref_save = os.path.join(str(tmp_path), f"{name}.ref.csv")
        ref_stream = os.path.join(str(tmp_path), f"{name}.iref.csv")
        p.save_as(
            file_name=str(workbook),
            sheet_name=name,
            dest_file_name=ref_save,
        )
        p.isave_as(
            file_name=str(workbook),
            sheet_name=name,
            dest_file_name=ref_stream,
        )
        assert on_disk == open(ref_save, "rb").read()
        assert on_disk == open(ref_stream, "rb").read()
        read_back = p.get_sheet(file_name=target)
        assert read_back.to_array() == expected_book[name].to_array()

    assert open(unselected, "rb").read() == before[
        f"{UNSELECTED}.csv"
    ]
    assert open(unrelated, "rb").read() == before["notes.txt"]
    assert set(_snapshot(output_dir).keys()) == set(before.keys()) | {
        "Charlie.csv"
    }
    assert data["Charlie"][-1] == [499]
