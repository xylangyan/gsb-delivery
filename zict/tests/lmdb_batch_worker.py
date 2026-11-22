"""Worker process for the multi-process LMDB batch tests.

Imported and run as a standalone script by ``test_lmdb_batch.py``. Communication
happens through command-line arguments, signal files and JSON-lines manifests, so
the worker never imports pytest.

Manifest file format (one JSON array per line)::

    ["key", "hex-encoded bytes value"]
    ["key", {"bad": true}]   # value is intentionally not bytes

Usage::

    python -m zict.tests.lmdb_batch_worker <db_dir> <journal_dir> <role> <args...>
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from collections.abc import Iterator
from typing import Any

from zict import BatchItemError, LMDB, MapSizeExceededError, VersionConflictError


def _wait_for(path: str) -> None:
    while not os.path.exists(path):
        time.sleep(0.01)


def _open(db_dir: pathlib.Path, map_size: int, max_map_size: int | None) -> LMDB:
    return LMDB(db_dir, map_size=map_size, max_map_size=max_map_size)


def _parse_manifest(path: pathlib.Path) -> Iterator[tuple[str, bytes | object]]:
    """Yield (key, value) pairs, reading the manifest exactly once.

    A marker object instead of a bytes value produces the same kind of bad input
    an upstream manifest could contain.
    """
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            key, raw_value = json.loads(line)
            if isinstance(raw_value, dict) and raw_value.get("bad"):
                value: bytes | object = object()
            else:
                value = bytes.fromhex(raw_value)
            yield key, value


def _attach_grow_tracking(z: LMDB, sig_file: str) -> list[int]:
    grow_events: list[int] = []
    map_sizes: list[int] = []

    def on_map_full() -> None:
        grow_events.append(z.map_size)
        if sig_file != "-":
            pathlib.Path(sig_file).touch()
            _wait_for(sig_file + ".resume")

    z.on_map_full = on_map_full
    original_grow = z._grow_map

    def tracked_grow() -> int:
        new_size = original_grow()
        map_sizes.append(new_size)
        return new_size

    z._grow_map = tracked_grow  # type: ignore[method-assign]
    z._test_map_sizes = map_sizes  # type: ignore[attr-defined]
    z._test_grow_events = grow_events  # type: ignore[attr-defined]
    return map_sizes


def _run_writer(
    z: LMDB,
    journal: pathlib.Path,
    condition: tuple[str, bytes | None] | None,
) -> dict[str, object]:
    result: dict[str, object] = {"outcome": None}
    try:
        z.commit_staged(journal, condition=condition)
    except VersionConflictError as exc:
        result["outcome"] = "conflict"
        result["key"] = exc.key
        result["expected"] = None if exc.expected is None else exc.expected.hex()
        result["actual"] = None if exc.actual is None else exc.actual.hex()
        result["journal_exists"] = (
            exc.journal_path is not None and pathlib.Path(exc.journal_path).exists()
        )
    except MapSizeExceededError as exc:
        result["outcome"] = "map_full"
        result["map_size"] = exc.map_size
        result["max_map_size"] = exc.max_map_size
        result["journal_exists"] = (
            exc.journal_path is not None and pathlib.Path(exc.journal_path).exists()
        )
    else:
        result["outcome"] = "committed"
    return result


def main() -> int:
    db_dir = pathlib.Path(sys.argv[1])
    journal_dir = pathlib.Path(sys.argv[2])
    role = sys.argv[3]

    if role == "seed":
        key, value_hex, map_size = sys.argv[4], sys.argv[5], int(sys.argv[6])
        z = _open(db_dir, map_size, None)
        z[key] = bytes.fromhex(value_hex)
        z.close()
        print("RESULT " + json.dumps({"outcome": "seeded"}))
        return 0

    if role == "writer":
        source = pathlib.Path(sys.argv[4])
        condition_key = sys.argv[5]
        expected_hex = sys.argv[6]
        sig_file = sys.argv[7]
        map_size = int(sys.argv[8])
        max_arg = sys.argv[9]
        max_map_size = None if max_arg == "-" else int(max_arg)
        if condition_key == "-":
            condition = None
        else:
            expected = None if expected_hex == "-" else bytes.fromhex(expected_hex)
            condition = (condition_key, expected)

        z = _open(db_dir, map_size, max_map_size)
        if source.suffix == ".manifest":
            # Read the upstream manifest exactly once and stage it locally.
            # The manifest may deliberately contain a non-bytes value; stage_batch
            # validates types and reports the offending position at runtime.
            journal = z.stage_batch(
                _parse_manifest(source)  # type: ignore[arg-type]
            )
        else:
            journal = source
        map_sizes = _attach_grow_tracking(z, sig_file)
        result = _run_writer(z, journal, condition)
        result["grow_events"] = z._test_grow_events  # type: ignore[attr-defined]
        result["map_sizes"] = map_sizes
        result["final_map_size"] = z.db.info()["map_size"]
        z.close()
        print("RESULT " + json.dumps(result))
        return 0

    if role == "resize":
        initial_map_size = int(sys.argv[4])
        new_size = int(sys.argv[5])
        z = LMDB(db_dir, map_size=initial_map_size)
        effective = z.set_map_size(new_size)
        z.close()
        print("RESULT " + json.dumps({"outcome": "resized", "map_size": effective}))
        return 0

    if role == "inspect":
        # Read-only so inspection never changes the map size writers agreed on.
        map_size = int(sys.argv[4])
        keys = sys.argv[5:]
        z = LMDB(db_dir, map_size=map_size, readonly=True)
        values: dict[str, str | None] = {}
        for key in keys:
            try:
                values[key] = z[key].hex()
            except KeyError:
                values[key] = None
        inspect_result: dict[str, Any] = {
            "outcome": "inspect",
            "map_size": z.db.info()["map_size"],
            "file_size": (db_dir / "data.mdb").stat().st_size,
            "entries": len(z),
            "values": values,
        }
        z.close()
        print("RESULT " + json.dumps(inspect_result))
        return 0

    if role == "reader":
        pause_sig, resume_sig = sys.argv[4], sys.argv[5]
        z = LMDB(db_dir, readonly=True)
        snapshot = z.snapshot()
        iterator = iter(snapshot)
        head = []
        for _ in range(2):
            try:
                head.append(next(iterator))
            except StopIteration:
                break
        pathlib.Path(pause_sig).touch()
        _wait_for(resume_sig)
        old_keys = head + list(iterator)
        old_values = {key: snapshot[key].hex() for key in old_keys}
        snapshot.close()
        with z.snapshot() as fresh:
            fresh_keys = list(fresh)
            fresh_values = dict(fresh.items())
        z.close()
        print(
            "RESULT "
            + json.dumps(
                {
                    "outcome": "reader",
                    "old_keys": old_keys,
                    "old_values": old_values,
                    "fresh_keys": fresh_keys,
                    "fresh_values": {k: v.hex() for k, v in fresh_values.items()},
                }
            )
        )
        return 0

    if role == "pipeline":
        # Commit a legal batch (surviving a MDB_MAP_FULL pause), then stream a
        # manifest whose tail is malformed, then still commit a small compliant
        # batch - all within one process, without re-reading any source.
        legal_journal = pathlib.Path(sys.argv[4])
        sig_file = sys.argv[5]
        manifest = pathlib.Path(sys.argv[6])
        small_journal = pathlib.Path(sys.argv[7])
        map_size = int(sys.argv[8])
        max_map_size = int(sys.argv[9])

        z = _open(db_dir, map_size, max_map_size)
        map_sizes = _attach_grow_tracking(z, sig_file)
        first = _run_writer(z, legal_journal, None)

        manifest_result: dict[str, object] = {}
        try:
            z.stage_batch(
                _parse_manifest(manifest)  # type: ignore[arg-type]
            )
        except BatchItemError as exc:
            manifest_result["outcome"] = "bad_item"
            manifest_result["index"] = exc.index
            manifest_result["bad_key"] = exc.bad_key
        else:  # pragma: no cover - the manifest is malformed by construction
            manifest_result["outcome"] = "unexpectedly_valid"

        sealed = sorted(p.name for p in journal_dir.glob("*.journal"))
        small = _run_writer(z, small_journal, None)
        manifest_result["sealed_journals_before_small"] = sealed
        manifest_result["entries"] = len(z)
        final_map_size = z.db.info()["map_size"]
        grow_events = z._test_grow_events  # type: ignore[attr-defined]
        z.close()
        print(
            "RESULT "
            + json.dumps(
                {
                    "outcome": "pipeline",
                    "first_batch": first,
                    "manifest": manifest_result,
                    "small_batch": small,
                    "grow_events": grow_events,
                    "map_sizes": map_sizes,
                    "final_map_size": final_map_size,
                }
            )
        )
        return 0

    print(f"unknown role: {role}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
