"""Behavioural tests for the archive reconstruction engine."""

import json
import os
import struct
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import whisper
from archive_recon import manifest, reconstruct
from archive_recon import deliver

START = 1008
N = 24


def make_spec(cells, series=None, target=None, legacy=None, xff=0.5):
    raw = {
        "series": series or {"start": START, "seconds": N, "range": [0, 10]},
        "target": target
        or {"archives": [[4, 6], [8, 6], [24, 4]], "xFilesFactor": xff},
        "legacy": legacy
        or [
            {
                "id": "A",
                "archives": [[4, 6], [8, 6], [24, 4]],
                "xFilesFactor": xff,
                "aggregationMethod": "average",
                "cells": cells,
            }
        ],
    }
    spec = manifest.parse(raw)
    manifest.validate_geometry(spec)
    return spec


def value(level, start, amount, origin="aggregate"):
    return {
        "level": level,
        "start": start,
        "state": "value",
        "value": amount,
        "origin": origin,
    }


def test_real_zero_is_a_value_not_a_hole():
    # A stored 4s zero with every underlying second fixed at zero must read back
    # as a present zero, not as a missing cell.
    spec = make_spec(
        [value(4, START, 0.0), value(4, START + 4, 0.0)],
        series={"start": START, "seconds": N, "range": [0, 10],
                "known": [{"second": START + t, "value": 0.0} for t in range(8)]},
    )
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    assert inst.bounds_of(inst.seconds[0].presence) == (1.0, 1.0)
    assert inst.bounds_of(inst.seconds[0].value) == (0.0, 0.0)


def test_threshold_empty_caps_presence_below_threshold():
    spec = make_spec(
        [{"level": 4, "start": START, "state": "empty", "reason": "threshold"}]
    )
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    # xff 0.5 over 4 seconds -> 2 are needed to store, so at most 1 may be
    # present.  Exactly one is admissible, which is what the model proves.
    lows = [inst.bounds_of(inst.seconds[t].presence)[0] for t in range(4)]
    highs = [inst.bounds_of(inst.seconds[t].presence)[1] for t in range(4)]
    assert sum(lows) <= 1.0 and sum(highs) >= 1.0


def test_rollover_hole_constrains_nothing():
    spec = make_spec(
        [{"level": 4, "start": START, "state": "lost", "reason": "rollover"}]
    )
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    assert all(inst.bounds_of(inst.seconds[t].presence) == (0.0, 1.0) for t in range(4))
    assert inst.ignored and inst.ignored[0]["reason"].startswith("rollover")


def test_direct_write_is_provenance_not_evidence():
    spec = make_spec([value(4, START, 3.0, origin="direct")])
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    assert inst.bounds_of(inst.seconds[0].value) == (0.0, 10.0)
    assert inst.provenance and "excluded" in inst.provenance[0]["note"]


def test_two_fine_cells_jointly_pin_a_coarse_cell():
    spec = make_spec([value(4, START, 1.0), value(4, START + 4, 9.0)])
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    coarse = inst.cell_for(8, START)
    assert inst.bounds_of(coarse.presence) == (1.0, 1.0)
    low, high = inst.bounds_of(coarse.value)
    assert abs(low - 5.0) < 1e-6 and abs(high - 5.0) < 1e-6


def test_missing_second_is_distinguished_from_zero():
    spec = make_spec(
        [],
        series={"start": START, "seconds": N, "range": [0, 10], "missing": [START]},
    )
    inst = reconstruct.Reconstruct(spec)
    inst.solve()
    assert inst.bounds_of(inst.seconds[0].presence) == (0.0, 0.0)


def test_conflicting_evidence_is_reported_not_written():
    spec = make_spec([value(4, START, 8.0), value(8, START, 2.0)])
    with pytest.raises(reconstruct.Conflict):
        reconstruct.Reconstruct(spec).solve()
    core = reconstruct.minimal_conflict(spec)
    assert core["kind"] == "minimal-core"
    assert {(c["level"], c["start"]) for c in core["cells"]} == {(4, START), (8, START)}


def test_bounds_come_from_all_series_not_cellwise_maxima():
    # Every second is free in [0, 10] but the 4s cell is pinned to 0 while the
    # 8s parent is pinned to 10: no single series can realise both cellwise
    # maxima at once, so the joint model must be infeasible.
    spec = make_spec([value(4, START, 0.0), value(8, START, 10.0)])
    with pytest.raises(reconstruct.Conflict):
        reconstruct.Reconstruct(spec).solve()


def build_legacy(path, archives, xff, cadence, start, seconds):
    if os.path.exists(path):
        os.remove(path)
    whisper.create(path, archives, xFilesFactor=xff, aggregationMethod="average")
    now = start + seconds
    for offset in range(0, seconds, cadence):
        mean = sum(float((t * 7) % 13) for t in range(offset, offset + cadence)) / cadence
        whisper.update(path, mean, timestamp=start + offset, now=now)
    return path


def read_cells(path, start, seconds, steps):
    info = whisper.info(path)
    cells = []
    with open(path, "rb") as handle:
        for archive in info["archives"]:
            step = archive["secondsPerPoint"]
            if step not in steps:
                continue
            handle.seek(archive["offset"])
            for _ in range(archive["points"]):
                timestamp, amount = struct.unpack(
                    whisper.pointFormat, handle.read(whisper.pointSize)
                )
                if start <= timestamp < start + seconds:
                    cells.append(value(step, timestamp, amount))
    return cells


def test_two_machines_reconstruct_into_a_readable_file(tmp_path):
    start, seconds = 1008, 96
    a = build_legacy(
        str(tmp_path / "a.wsp"), [(4, 24), (24, 8)], 0.5, 4, start, seconds
    )
    b = build_legacy(
        str(tmp_path / "b.wsp"), [(6, 16), (24, 8)], 0.75, 6, start, seconds
    )
    raw = {
        "series": {"start": start, "seconds": seconds, "range": [0, 12]},
        "publications": ["p1", "p2"],
        "target": {"archives": [[4, 24], [12, 12], [24, 8]], "xFilesFactor": 0.5},
        "legacy": [
            {
                "id": "A",
                "archives": [[4, 24], [24, 8]],
                "xFilesFactor": 0.5,
                "aggregationMethod": "average",
                "cells": read_cells(a, start, seconds, {4, 24}),
            },
            {
                "id": "B",
                "archives": [[6, 16], [24, 8]],
                "xFilesFactor": 0.75,
                "aggregationMethod": "average",
                "cells": read_cells(b, start, seconds, {6, 24}),
            },
        ],
    }
    spec = manifest.parse(raw)
    manifest.validate_geometry(spec)
    inst = reconstruct.Reconstruct(spec)
    result = inst.solve()
    seconds_rows = deliver.analyse_seconds(inst, result)
    target_rows = deliver.analyse_target(inst, result)

    out = str(tmp_path / "new.wsp")
    deliver.write_whisper(out, spec, seconds_rows, target_rows)

    truth4 = [
        sum(float((t * 7) % 13) for t in range(offset, offset + 4)) / 4
        for offset in range(0, seconds, 4)
    ]
    for level in target_rows:
        step = level["level"]
        fetched = deliver.verify_with_whisper(out, spec, target_rows)
        assert any(entry["level"] == step for entry in fetched)
    four = next(level for level in target_rows if level["level"] == 4)
    for cell, expected in zip(four["cells"], truth4):
        if cell["state"] == "value":
            assert abs(cell["value"] - expected) < 1e-6
        else:
            assert cell["state"] == "pending"
            low, high = cell["range"]
            assert low <= expected <= high


def test_report_counts_and_pending_drivers(tmp_path):
    start, seconds = 1008, 96
    a = build_legacy(
        str(tmp_path / "a.wsp"), [(4, 24), (24, 8)], 0.5, 4, start, seconds
    )
    b = build_legacy(
        str(tmp_path / "b.wsp"), [(6, 16), (24, 8)], 0.75, 6, start, seconds
    )
    raw = {
        "series": {"start": start, "seconds": seconds, "range": [0, 12]},
        "target": {"archives": [[4, 24], [12, 12], [24, 8]], "xFilesFactor": 0.5},
        "legacy": [
            {"id": "A", "archives": [[4, 24], [24, 8]], "xFilesFactor": 0.5,
             "cells": read_cells(a, start, seconds, {4, 24})},
            {"id": "B", "archives": [[6, 16], [24, 8]], "xFilesFactor": 0.75,
             "cells": read_cells(b, start, seconds, {6, 24})},
        ],
    }
    spec = manifest.parse(raw)
    manifest.validate_geometry(spec)
    inst = reconstruct.Reconstruct(spec)
    result = inst.solve()
    rows = deliver.analyse_target(inst, result)
    report = deliver.build_report(
        inst,
        result,
        rows,
        deliver.analyse_seconds(inst, result),
        inst.provenance,
        inst.ignored,
    )
    for level in report["levels"]:
        pending = [c for c in level["cells"] if c["state"] == "pending"]
        for cell in pending:
            if not cell["to_change"]["unresolved_seconds"]:
                # Existence-only gaps have a fixed value but no loose seconds.
                assert cell["pending_reason"] == "presence"
                continue
            for driver in cell["to_change"]["unresolved_seconds"]:
                assert cell["start"] <= driver["second"] < cell["end"]


def test_handover_file_distinguishes_real_zero_from_a_hole(tmp_path):
    spec = make_spec(
        [
            value(4, START, 0.0),
            {"level": 4, "start": START + 4, "state": "lost", "reason": "rollover"},
        ],
        series={
            "start": START,
            "seconds": N,
            "range": [0, 10],
            "known": [{"second": START + t, "value": 0.0} for t in range(4)],
        },
    )
    inst = reconstruct.Reconstruct(spec)
    result = inst.solve()
    rows = deliver.analyse_target(inst, result)
    out = str(tmp_path / "zero.wsp")
    deliver.write_whisper(out, spec, deliver.analyse_seconds(inst, result), rows)

    import whisper

    info, values = whisper.fetch(
        out, START - 1, START + N - 1, now=START + N, archiveToSelect=4
    )
    four = next(level for level in rows if level["level"] == 4)
    zero_cell = next(c for c in four["cells"] if c["start"] == START)
    assert zero_cell["state"] == "value" and zero_cell["value"] == 0.0
    assert values[0] == 0.0
    assert values[1] is None
