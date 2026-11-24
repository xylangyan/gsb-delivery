"""Turn a joint reconstruction model into a Whisper file and a report.

The rule for writing a target cell is strict: it is written only when every
feasible original series gives the cell the same presence and the same numeric
value.  Otherwise the slot is left empty and the report records the evidence
that could change it.

A second is reported ``missing`` only when no feasible series observes it;
``bounded`` seconds have a non-degenerate range; ``value`` seconds have a
singleton range.  A proved real zero is a ``value`` second, never a hole.
"""

import json
import os
import struct

import whisper


def analyse_target(instance, result):
    rows = []
    for step, level in instance.target_levels:
        cells = []
        for start in sorted(level):
            cell = level[start]
            presence = instance.bounds_of(cell.presence)
            entry = {
                "level": step,
                "start": start,
                "end": start + step,
                "state": None,
                "value": None,
                "range": None,
            }
            if presence is None or presence[1] < 0.5:
                entry["state"] = "missing"
            elif presence[0] >= 0.5:
                bounds = instance.bounds_of(cell.value)
                if bounds is None:
                    entry["state"] = "pending"
                    entry["pending_reason"] = "value-unbounded"
                else:
                    low, high = bounds
                    if abs(high - low) <= 1e-6:
                        entry.update(
                            state="value", value=(low + high) / 2.0,
                            range=[low, high],
                        )
                    else:
                        entry.update(
                            state="pending", pending_reason="value-range",
                            range=[low, high],
                        )
            else:
                # Existence itself is not settled; report the value it would
                # take if present, which is still useful to close the gap.
                value_bounds = instance.bounds_of(
                    cell.value, conditional=cell.presence
                )
                entry["state"] = "pending"
                entry["pending_reason"] = "presence"
                entry["range"] = list(value_bounds) if value_bounds else None
                entry["presence"] = list(presence)
            cells.append(entry)
        rows.append({"level": step, "cells": cells})
    return rows


def analyse_seconds(instance, result):
    rows = []
    for t, second in enumerate(instance.seconds):
        presence = instance.bounds_of(second.presence)
        if presence is None or presence[1] < 0.5:
            rows.append(
                {
                    "second": instance.series.start + t,
                    "state": "missing",
                    "value": None,
                    "range": None,
                }
            )
            continue
        low, high = instance.bounds_of(second.value)
        if low is None:
            rows.append(
                {
                    "second": instance.series.start + t,
                    "state": "missing",
                    "value": None,
                    "range": None,
                }
            )
            continue
        if abs(high - low) <= 1e-6:
            rows.append(
                {
                    "second": instance.series.start + t,
                    "state": "value",
                    "value": (low + high) / 2.0,
                    "range": [low, high],
                }
            )
        else:
            rows.append(
                {
                    "second": instance.series.start + t,
                    "state": "bounded",
                    "value": None,
                    "range": [low, high],
                }
            )
    return rows


def _evidence_index(instance):
    """Map each target cell to the aggregate cells that constrain it."""
    index = {}
    for leg in instance.spec.legacy:
        _leg, steps, levels = instance.legacy_levels[leg.id]
        for cell, claim, step, start in instance.legacy_cells:
            if claim.state != "value" or claim.origin != "aggregate":
                continue
            for target_step, target_level in instance.target_levels:
                for target_start, target_cell in target_level.items():
                    if target_start <= claim.start < target_start + target_step:
                        key = "%d:%d" % (target_step, target_start)
                        index.setdefault(key, []).append(
                            {
                                "legacy": leg.id,
                                "level": claim.level,
                                "start": claim.start,
                                "value": claim.value,
                                "publication": claim.publication,
                            }
                        )
    return index


def pending_driver(instance, step, start, low, high):
    """What would have to change for a pending target cell.

    Every driver is an original second covered by the cell whose reading is not
    yet fixed.  Any original series that moves this cell must change at least
    one of them, so they are the evidence gap to close.
    """
    drivers = []
    best = None
    for t in range(start, start + step):
        offset = t - instance.series.start
        if not 0 <= offset < instance.series.seconds:
            continue
        second = instance.seconds[offset]
        bounds = instance.bounds_of(second.value)
        if bounds is None or abs(bounds[1] - bounds[0]) <= 1e-9:
            continue
        entry = {
            "second": t,
            "range": [bounds[0], bounds[1]],
            "span": bounds[1] - bounds[0],
        }
        if best is None or entry["span"] > best["span"]:
            best = entry
        drivers.append(entry)
    return {
        "cell_range": [low, high],
        "width": high - low,
        "widest_second": best,
        "unresolved_seconds": drivers,
    }


def build_report(instance, result, target_rows, seconds_rows, provenance, ignored):
    index = _evidence_index(instance)
    report = {
        "window": {
            "start": instance.series.start,
            "end": instance.series.start + instance.series.seconds,
        },
        "target_archives": [
            {"step": step, "points": points}
            for step, points in instance.spec.target.archives
        ],
        "seconds": {
            "value": sum(1 for r in seconds_rows if r["state"] == "value"),
            "bounded": sum(1 for r in seconds_rows if r["state"] == "bounded"),
            "missing": sum(1 for r in seconds_rows if r["state"] == "missing"),
        },
        "seconds_detail": seconds_rows,
        "levels": [],
        "provenance": provenance,
        "ignored_cells": ignored,
        "conflicts": [],
    }
    for level in target_rows:
        cells = []
        for entry in level["cells"]:
            cell = dict(entry)
            key = "%d:%d" % (entry["level"], entry["start"])
            cell["evidence"] = index.get(key, [])
            if entry["state"] == "pending":
                cell["to_change"] = pending_driver(
                    instance, entry["level"], entry["start"],
                    entry["range"][0], entry["range"][1]
                )
            cells.append(cell)
        report["levels"].append(
            {
                "level": level["level"],
                "value": sum(1 for c in cells if c["state"] == "value"),
                "pending": sum(1 for c in cells if c["state"] == "pending"),
                "missing": sum(1 for c in cells if c["state"] == "missing"),
                "pending_presence": sum(
                    1 for c in cells if c.get("pending_reason") == "presence"
                ),
                "pending_value": sum(
                    1 for c in cells if c.get("pending_reason") == "value-range"
                ),
                "cells": cells,
            }
        )
    return report


def dump_json(report, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


class WriteRefused(Exception):
    def __init__(self, message, cells):
        super().__init__(message)
        self.cells = cells


def _place(handle, archive, base, interval, value):
    distance = (interval - base) // archive["secondsPerPoint"]
    offset = archive["offset"] + (
        (distance * whisper.pointSize) % archive["size"]
    )
    handle.seek(offset)
    handle.write(struct.pack(whisper.pointFormat, interval, float(value)))


def write_whisper(path, spec, seconds_rows, target_rows):
    """Write each target level's definite cells straight into its slots.

    Whisper stores one reading per archive step, and ``whisper.update`` would
    overwrite a whole cell with a single original second.  A handover file must
    instead carry the cell value the joint model proved, so we place one packed
    point per definite cell at the slot Whisper's ring arithmetic assigns to
    it.  Slots that are not proved keep their zero timestamp, which
    ``whisper.fetch`` reports as ``None`` -- an honest hole rather than a fake
    reading.  A genuine zero is written with a real timestamp and reads back as
    ``0.0``, never as a hole.
    """
    if os.path.exists(path):
        os.remove(path)
    whisper.create(
        path,
        [tuple(archive) for archive in spec.target.archives],
        xFilesFactor=spec.target.xFilesFactor,
        aggregationMethod=spec.target.aggregationMethod,
    )
    header = whisper.info(path)
    by_step = {archive["secondsPerPoint"]: archive for archive in header["archives"]}
    levels = {level["level"]: level for level in target_rows}
    with open(path, "r+b") as handle:
        for step, archive in by_step.items():
            if step not in levels:
                continue
            cells = levels[step]["cells"]
            definite = [c for c in cells if c["state"] == "value"]
            if not definite:
                continue
            base = definite[0]["start"]
            handle.seek(archive["offset"])
            handle.write(
                struct.pack(whisper.pointFormat, base, float(definite[0]["value"]))
            )
            for cell in definite[1:]:
                _place(handle, archive, base, cell["start"], cell["value"])

    mismatch = []
    for level in target_rows:
        step = level["level"]
        time_info, values = whisper.fetch(
            path, spec.series.start - 1, spec.series.start + spec.series.seconds - 1,
            now=spec.series.start + spec.series.seconds, archiveToSelect=step,
        )
        first = time_info[0]
        index = {}
        for i, value in enumerate(values):
            index[first + i * step] = value
        for cell in level["cells"]:
            written = index.get(cell["start"])
            if cell["state"] == "value":
                if written is None or abs(written - cell["value"]) > 1e-6:
                    mismatch.append(
                        {
                            "level": step,
                            "start": cell["start"],
                            "expected": cell["value"],
                            "written": written,
                        }
                    )
            elif written is not None:
                mismatch.append(
                    {
                        "level": step,
                        "start": cell["start"],
                        "expected": None,
                        "written": written,
                    }
                )
    if mismatch:
        raise WriteRefused(
            "the written Whisper file disagrees with the model", mismatch
        )
    return path


def verify_with_whisper(path, spec, target_rows):
    """Re-read every target level through the normal Whisper fetch path."""
    out = []
    for step, _ in spec.target.archives:
        time_info, values = whisper.fetch(
            path, spec.series.start - 1, spec.series.start + spec.series.seconds - 1,
            now=spec.series.start + spec.series.seconds, archiveToSelect=step,
        )
        out.append(
            {
                "level": step,
                "from": time_info[0],
                "until": time_info[1],
                "step": time_info[2],
                "values": values,
            }
        )
    return out
