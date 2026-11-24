"""Build the two-machine demo and run the reconstruction on it.

Fixture design
--------------
* One truth at 1-second resolution: ``truth(t) = 0`` for seconds in
  ``[80, 88)`` (a proved real-zero stretch), otherwise ``(7*t) % 13``.
* Machine A stores 4s means and machine B stores 6s means, each written the way
  Whisper actually writes them (one update per cell, so the stored value is
  exactly that window's mean).
* A 12-second stretch (offsets 48..59) is sampled by neither machine.  Their
  finest cells there are genuinely absent, so the handover marks them
  ``empty`` with ``reason=threshold`` -- not rollover.
* Machine B's 6s slot at offset 12 is overwritten by a later ring publication;
  the handover marks it ``lost`` and the model does not borrow the bytes now
  occupying that slot.
* A value is written straight into machine A's 24s archive at offset 24; it is
  marked ``origin=direct`` and used only as provenance.
* The 4s/6s evidence overlaps on every window that is not a multiple of 12, and
  a 24s target cell is written only when the joint model pins it.

Run ``python demo/build_demo.py`` to regenerate every artifact.
"""

import json
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import whisper

START = 1008
SECONDS = 96
FIXTURES = os.path.join(HERE, "fixtures")
OUT = os.path.join(HERE, "out")


def truth(second):
    if 80 <= second < 88:
        return 0.0
    return float((second * 7) % 13)


def truth_window(offset, span):
    return sum(truth(t) for t in range(offset, offset + span)) / span


def write_archive(path, archives, xff, cadence, samples):
    if os.path.exists(path):
        os.remove(path)
    whisper.create(path, archives, xFilesFactor=xff, aggregationMethod="average")
    now = START + SECONDS
    for offset in samples:
        whisper.update(
            path, truth_window(offset, cadence), timestamp=START + offset, now=now
        )
    return path


def read_levels(path, steps):
    info = whisper.info(path)
    out = {}
    with open(path, "rb") as handle:
        for archive in info["archives"]:
            step = archive["secondsPerPoint"]
            if step not in steps:
                continue
            handle.seek(archive["offset"])
            values = {}
            for _ in range(archive["points"]):
                timestamp, amount = struct.unpack(
                    whisper.pointFormat, handle.read(whisper.pointSize)
                )
                if START <= timestamp < START + SECONDS:
                    values[timestamp] = amount
            out[step] = values
    return info, out


def overwrite_slot(path, step, interval, timestamp, value):
    info = whisper.info(path)
    archive = next(a for a in info["archives"] if a["secondsPerPoint"] == step)
    with open(path, "rb+") as handle:
        handle.seek(archive["offset"])
        base, _ = struct.unpack(whisper.pointFormat, handle.read(whisper.pointSize))
        distance = (interval - base) // step
        offset = archive["offset"] + (
            (distance * whisper.pointSize) % archive["size"]
        )
        handle.seek(offset)
        handle.write(struct.pack(whisper.pointFormat, timestamp, value))


def cell_claims(values, step, publication, origin="aggregate"):
    out = []
    for index in range(SECONDS // step):
        start = START + index * step
        if start in values:
            out.append(
                {
                    "level": step,
                    "start": start,
                    "state": "value",
                    "value": values[start],
                    "origin": origin,
                    "publication": publication,
                }
            )
    return out


def build_manifest(a_path, b_path, a_cells, b_cells):
    return {
        "series": {
            "start": START,
            "seconds": SECONDS,
            "range": [0, 12],
            "known": [{"second": START + t, "value": 0.0} for t in range(80, 88)],
        },
        "publications": [
            "A-2026-01-05",
            "B-2026-01-06",
            "A-2026-03-01",
            "B-2026-03-02",
            "A-2026-04-09",
        ],
        "target": {
            "archives": [[4, 24], [12, 12], [24, 8]],
            "xFilesFactor": 0.5,
            "aggregationMethod": "average",
            "path": "handover.wsp",
        },
        "legacy": [
            {
                "id": "A",
                "file": os.path.relpath(a_path, ROOT),
                "archives": [[4, 24], [24, 8]],
                "xFilesFactor": 0.25,
                "aggregationMethod": "average",
                "cells": a_cells,
            },
            {
                "id": "B",
                "file": os.path.relpath(b_path, ROOT),
                "archives": [[6, 16], [24, 8]],
                "xFilesFactor": 0.75,
                "aggregationMethod": "average",
                "cells": b_cells,
            },
        ],
    }


def conflict_manifest():
    """Two surviving cells that cannot both describe the original seconds."""
    return {
        "series": {"start": START, "seconds": SECONDS, "range": [0, 12]},
        "publications": ["p1", "p2"],
        "target": {"archives": [[4, 24], [12, 12], [24, 8]], "xFilesFactor": 0.5},
        "legacy": [
            {
                "id": "A",
                "archives": [[4, 24], [12, 12], [24, 8]],
                "xFilesFactor": 0.5,
                "aggregationMethod": "average",
                "cells": [
                    {
                        "level": 4,
                        "start": START,
                        "state": "value",
                        "value": 10.0,
                        "origin": "aggregate",
                        "publication": "p1",
                    },
                    {
                        "level": 12,
                        "start": START,
                        "state": "value",
                        "value": 0.0,
                        "origin": "aggregate",
                        "publication": "p2",
                    },
                ],
            }
        ],
    }


def main():
    if os.path.isdir(FIXTURES):
        shutil.rmtree(FIXTURES)
    os.makedirs(FIXTURES)
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    a_path = os.path.join(FIXTURES, "machine_a.wsp")
    a_samples = [o for o in range(0, SECONDS, 4) if o not in {48, 52, 56}]
    write_archive(a_path, [(4, 24), (24, 8)], 0.25, 4, a_samples)

    b_path = os.path.join(FIXTURES, "machine_b.wsp")
    b_samples = [o for o in range(0, SECONDS, 6) if o not in {48, 54}]
    write_archive(b_path, [(6, 16), (24, 8)], 0.75, 6, b_samples)
    overwrite_slot(b_path, 6, START + 12, START + SECONDS + 120, 99.0)

    _a_info, a_values = read_levels(a_path, {4, 24})
    _b_info, b_values = read_levels(b_path, {6, 24})

    a_cells = cell_claims(a_values[4], 4, "A-2026-03-01")
    for offset in (48, 52, 56):
        a_cells.append(
            {
                "level": 4,
                "start": START + offset,
                "state": "empty",
                "reason": "threshold",
                "publication": "A-2026-03-01",
            }
        )
    a_cells.append(
        {
            "level": 24,
            "start": START + 24,
            "state": "value",
            "value": 6.0,
            "origin": "direct",
            "publication": "A-2026-04-09",
        }
    )

    b_cells = []
    for index in range(SECONDS // 6):
        start = START + index * 6
        if start == START + 12:
            b_cells.append(
                {
                    "level": 6,
                    "start": start,
                    "state": "lost",
                    "reason": "rollover",
                    "publication": "B-2026-03-02",
                }
            )
        elif start in {START + 48, START + 54}:
            b_cells.append(
                {
                    "level": 6,
                    "start": start,
                    "state": "empty",
                    "reason": "threshold",
                    "publication": "B-2026-03-02",
                }
            )
        else:
            b_cells.append(
                {
                    "level": 6,
                    "start": start,
                    "state": "value",
                    "value": b_values[6][start],
                    "origin": "aggregate",
                    "publication": "B-2026-03-02",
                }
            )

    manifest = build_manifest(a_path, b_path, a_cells, b_cells)
    manifest_path = os.path.join(FIXTURES, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    conflict_path = os.path.join(FIXTURES, "conflicting_manifest.json")
    with open(conflict_path, "w", encoding="utf-8") as handle:
        json.dump(conflict_manifest(), handle, ensure_ascii=False, indent=2)

    from archive_recon.cli import run

    outcome = run(manifest_path, OUT)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return outcome


if __name__ == "__main__":
    sys.exit(0 if main()["status"] == "ok" else 1)
