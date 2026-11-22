from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

import pytest

pytest.importorskip("lmdb")

from zict import (  # noqa: E402
    BatchItemError,
    JournalError,
    LMDB,
    MapSizeExceededError,
    VersionConflictError,
)
from zict.tests import utils_test  # noqa: E402

WORKER = "zict.tests.lmdb_batch_worker"
TINY_MAP = 1024 * 1024


def _run_worker(*args: object, timeout: float = 60.0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", WORKER, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout
    return json.loads(lines[-1].removeprefix("RESULT "))


def _start_worker(*args: object) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", WORKER, *[str(a) for a in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait(path: pathlib.Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.01)


def _read_result(proc: subprocess.Popen, timeout: float = 60.0) -> dict:
    stdout, stderr = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, stderr
    lines = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, stdout
    return json.loads(lines[-1].removeprefix("RESULT "))


def _write_manifest(
    path: pathlib.Path, items: list[tuple[str, object]]
) -> pathlib.Path:
    with open(path, "w") as fh:
        for key, value in items:
            if isinstance(value, bytes):
                payload: object = value.hex()
            else:
                payload = {"bad": True}
            fh.write(json.dumps([key, payload]) + "\n")
    return path


def _stage(tmp_path: pathlib.Path, db: pathlib.Path, items) -> pathlib.Path:
    z = LMDB(db, map_size=TINY_MAP)
    try:
        return z.stage_batch(iter(items))
    finally:
        z.close()


def test_mapping_still_works(tmp_path, check_fd_leaks):
    z = LMDB(tmp_path)
    utils_test.check_mapping(z)


def test_bad_types(tmp_path, check_fd_leaks):
    z = LMDB(tmp_path)
    utils_test.check_bad_key_types(z)
    utils_test.check_bad_value_types(z)


def test_map_size_cap_still_raw_error(tmp_path, check_fd_leaks):
    import lmdb

    z = LMDB(tmp_path, map_size=2**20)
    z["x"] = b"x" * 2**19
    with pytest.raises(lmdb.MapFullError):
        z["y"] = b"x" * 2**20


def test_stage_reports_original_position(tmp_path):
    z = LMDB(tmp_path, map_size=TINY_MAP)

    def manifest():
        yield ("good0", b"0")
        yield ("good1", b"1")
        yield (123, b"bad-key")

    with pytest.raises(BatchItemError) as exc:
        z.stage_batch(manifest())
    assert exc.value.index == 2
    assert exc.value.bad_key is True

    def manifest2():
        for index in range(5):
            yield (f"k{index}", b"v")
        yield ("tail", object())

    with pytest.raises(BatchItemError) as exc:
        z.stage_batch(manifest2())
    assert exc.value.index == 5
    assert exc.value.bad_key is False
    assert list((tmp_path / "zict-journal").glob("*.tmp")) == []
    assert list((tmp_path / "zict-journal").glob("*.journal")) == []
    z.close()


def test_conditional_commit_and_retry_keep_journal(tmp_path):
    z = LMDB(tmp_path, map_size=TINY_MAP, max_map_size=8 * TINY_MAP)
    z["v"] = b"v1"

    journal = z.stage_batch([("a", b"1"), ("a", b"2")])
    with pytest.raises(VersionConflictError) as exc:
        z.commit_staged(journal, condition=("v", b"v0"))
    assert exc.value.actual == b"v1"
    assert exc.value.journal_path is not None
    assert exc.value.journal_path.exists()
    assert "a" not in z

    z.commit_staged(journal, condition=("v", b"v1"))
    assert z["a"] == b"2"
    assert not journal.exists()
    z.close()


def test_absent_condition(tmp_path):
    z = LMDB(tmp_path)
    journal = z.stage_batch([("a", b"1")])
    z.commit_staged(journal, condition=("guard", None))
    assert z["a"] == b"1"

    z["guard"] = b"g"
    journal = z.stage_batch([("b", b"2")])
    with pytest.raises(VersionConflictError) as exc:
        z.commit_staged(journal, condition=("guard", None))
    assert exc.value.actual == b"g"
    z.commit_staged(journal, condition=("guard", b"g"))
    assert z["b"] == b"2"
    z.close()


def test_auto_grow_single_pass_generator(tmp_path):
    z = LMDB(tmp_path, map_size=TINY_MAP, max_map_size=16 * TINY_MAP)

    def manifest():
        for index in range(12):
            yield (f"k{index}", b"x" * 200_000)
        yield ("dup", b"first")
        yield ("dup", b"last")

    z.commit_items(manifest())
    assert len(z) == 13
    assert z["dup"] == b"last"
    assert z.map_size > TINY_MAP
    assert list((tmp_path / "zict-journal").glob("*")) == []
    z.close()


def test_replay_spans_multiple_chunks_and_keeps_last_write(tmp_path):
    # Larger than _REPLAY_CHUNK (256), so the journal is replayed in several
    # putmulti chunks inside one transaction. Duplicates across chunks still
    # resolve to the last write.
    z = LMDB(tmp_path)

    def manifest():
        for index in range(600):
            yield (f"k{index % 100}", f"v{index}".encode())

    z.commit_items(manifest())
    assert len(z) == 100
    assert z["k0"] == b"v500"
    assert z["k99"] == b"v599"
    z.close()


def test_cap_failure_is_explicit_and_small_batch_still_commits(tmp_path):
    z = LMDB(tmp_path, map_size=TINY_MAP, max_map_size=2 * TINY_MAP)

    def huge():
        for index in range(20):
            yield (f"big{index}", b"x" * 300_000)

    with pytest.raises(MapSizeExceededError) as exc:
        z.commit_items(huge())
    assert exc.value.max_map_size == 2 * TINY_MAP
    assert exc.value.journal_path is not None
    assert exc.value.journal_path.exists()
    assert len(z) == 0

    z.commit_items([("small", b"ok")])
    assert z["small"] == b"ok"
    z.close()


def test_journal_replay_validates_seal(tmp_path):
    z = LMDB(tmp_path)
    journal = z.stage_batch([("a", b"1")])
    data = journal.read_bytes()
    journal.write_bytes(data[:-2])
    with pytest.raises(JournalError):
        z.commit_staged(journal)
    assert "a" not in z
    z.close()


def test_snapshot_isolation_in_process(tmp_path):
    z = LMDB(tmp_path)
    z["v"] = b"v1"
    snapshot = z.snapshot()
    assert snapshot["v"] == b"v1"
    z["v"] = b"v2"
    z["extra"] = b"e"
    assert snapshot["v"] == b"v1"
    assert "extra" not in snapshot
    fresh = z.snapshot()
    assert fresh["v"] == b"v2" and "extra" in fresh
    snapshot.close()
    fresh.close()
    z.close()


def test_two_writers_version_conflict(tmp_path):
    """Scenario 1: A aborts on MDB_MAP_FULL before retrying; B swaps the
    condition key and grows the map; A's retry reports the actual conflict and
    never publishes any part of its batch. The retained journal can later be
    recommitted against B's new version."""
    db = tmp_path / "db"
    sig = tmp_path / "sig"
    sig.mkdir()
    paused = sig / "a_paused"
    old_version, new_version = b"v1", b"v2"

    _run_worker(db, db, "seed", "v", old_version.hex(), TINY_MAP)

    manifest = _write_manifest(
        tmp_path / "a.manifest",
        [(f"a{index}", b"x" * 200_000) for index in range(10)],
    )

    proc_a = _start_worker(
        db,
        db / "zict-journal",
        "writer",
        manifest,
        "v",
        old_version.hex(),
        paused,
        TINY_MAP,
        8 * TINY_MAP,
    )
    _wait(paused)

    # B swaps the precondition version (small enough for the tiny map) and grows
    # the capacity while A is parked between abort and retry.
    b_manifest = _write_manifest(tmp_path / "b.manifest", [("v", new_version)])
    b_result = _run_worker(
        db, db / "zict-journal", "writer", b_manifest, "-", "-", "-", TINY_MAP,
        8 * TINY_MAP,
    )
    assert b_result["outcome"] == "committed"
    resize = _run_worker(db, db / "zict-journal", "resize", TINY_MAP, 4 * TINY_MAP)
    assert resize["map_size"] == 4 * TINY_MAP

    paused.with_suffix(".resume").write_text("")
    a_result = _read_result(proc_a)

    assert a_result["outcome"] == "conflict"
    assert a_result["key"] == "v"
    assert a_result["expected"] == old_version.hex()
    assert a_result["actual"] == new_version.hex()
    assert a_result["journal_exists"] is True
    assert a_result["grow_events"] == [TINY_MAP]

    inspection = _run_worker(
        db,
        db / "zict-journal",
        "inspect",
        32 * TINY_MAP,
        "v",
        *[f"a{index}" for index in range(10)],
    )
    assert inspection["values"]["v"] == new_version.hex()
    assert all(
        inspection["values"][f"a{index}"] is None for index in range(10)
    )

    leftovers = sorted((db / "zict-journal").glob("*.journal"))
    assert len(leftovers) == 1
    assert list((db / "zict-journal").glob("*.tmp")) == []

    # Recommit the retained journal against B's new version, no re-read needed.
    retry = _run_worker(
        db,
        db / "zict-journal",
        "writer",
        leftovers[0],
        "v",
        new_version.hex(),
        "-",
        TINY_MAP,
        8 * TINY_MAP,
    )
    assert retry["outcome"] == "committed"
    inspection = _run_worker(
        db, db / "zict-journal", "inspect", 32 * TINY_MAP, "v", "a9"
    )
    assert inspection["values"]["v"] == new_version.hex()
    assert inspection["values"]["a9"] == (b"x" * 200_000).hex()
    assert not leftovers[0].exists()


def test_tail_type_error_after_space_full(tmp_path):
    """Scenario 2: a legal batch survives a space-full pause and commits; a later
    single-pass manifest with a malformed tail reports the bad position without
    disturbing successful data; a compliant small batch still commits."""
    db = tmp_path / "db"
    sig = tmp_path / "sig"
    sig.mkdir()
    paused = sig / "p_paused"

    _run_worker(db, db, "seed", "b-seed", b"from-b".hex(), TINY_MAP)

    legal_journal = _stage(
        tmp_path, db, [(f"legal{index}", b"x" * 200_000) for index in range(8)]
    )
    small_journal = _stage(tmp_path, db, [("small", b"ok")])
    malformed = _write_manifest(
        tmp_path / "tail-bad.manifest",
        [(f"good{index}", b"v") for index in range(5)] + [("tail", object())],
    )

    proc = _start_worker(
        db,
        db / "zict-journal",
        "pipeline",
        legal_journal,
        paused,
        malformed,
        small_journal,
        TINY_MAP,
        4 * TINY_MAP,
    )
    _wait(paused)
    paused.with_suffix(".resume").write_text("")
    result = _read_result(proc)

    assert result["first_batch"]["outcome"] == "committed"
    assert result["grow_events"] == [TINY_MAP]
    assert result["manifest"]["outcome"] == "bad_item"
    assert result["manifest"]["index"] == 5
    assert result["manifest"]["bad_key"] is False
    # The malformed manifest created no journal; only the pre-staged small batch
    # is still waiting to be committed.
    assert result["manifest"]["sealed_journals_before_small"] == [
        small_journal.name
    ]
    assert result["small_batch"]["outcome"] == "committed"
    assert result["final_map_size"] >= 2 * TINY_MAP

    inspection = _run_worker(
        db,
        db / "zict-journal",
        "inspect",
        8 * TINY_MAP,
        "b-seed",
        "legal7",
        "small",
        "tail",
        *[f"good{index}" for index in range(5)],
    )
    values = inspection["values"]
    assert values["b-seed"] == b"from-b".hex()
    assert values["legal7"] == (b"x" * 200_000).hex()
    assert values["small"] == b"ok".hex()
    assert values["tail"] is None
    assert all(values[f"good{index}"] is None for index in range(5))
    assert list((db / "zict-journal").glob("*")) == []


def test_reader_snapshot_and_conditional_recommit(tmp_path):
    """Scenario 3: a paused read-only traversal finishes on its original revision
    while a conditional recommit against B's new version lands atomically; a new
    traversal sees the complete commit. B adjusts capacity mid-flight without
    touching the condition key."""
    db = tmp_path / "db"
    sig = tmp_path / "sig"
    sig.mkdir()
    pause_sig, resume_sig = sig / "reader_paused", sig / "reader_resume"
    a_paused = sig / "a_paused"

    _run_worker(db, db, "seed", "v", b"v1".hex(), TINY_MAP)
    base = _stage(tmp_path, db, [(f"base{index}", b"old") for index in range(4)])
    _run_worker(
        db, db / "zict-journal", "writer", base, "-", "-", "-", TINY_MAP, "-"
    )

    reader = _start_worker(db, db / "zict-journal", "reader", pause_sig, resume_sig)
    _wait(pause_sig)

    # B publishes the new condition-key version.
    b_swap = _write_manifest(tmp_path / "b-swap.manifest", [("v", b"v2")])
    b_result = _run_worker(
        db, db / "zict-journal", "writer", b_swap, "-", "-", "-", TINY_MAP, "-"
    )
    assert b_result["outcome"] == "committed"
    # B grows the map to give A's oversized batch room.
    _run_worker(db, db / "zict-journal", "resize", TINY_MAP, 8 * TINY_MAP)

    # A recommits a legal (large) manifest conditional on B's new version. Its
    # first attempt fills the map and pauses; B then adjusts capacity again
    # without touching the condition key.
    manifest = _write_manifest(
        tmp_path / "a-recommit.manifest",
        [("v", b"v2"), ("dup", b"x"), ("dup", b"y")]
        + [(f"new{index}", b"z" * 2_000_000) for index in range(6)],
    )
    proc_a = _start_worker(
        db,
        db / "zict-journal",
        "writer",
        manifest,
        "v",
        b"v2".hex(),
        a_paused,
        8 * TINY_MAP,
        32 * TINY_MAP,
    )
    _wait(a_paused)
    resize = _run_worker(
        db, db / "zict-journal", "resize", 8 * TINY_MAP, 16 * TINY_MAP
    )
    assert resize["map_size"] == 16 * TINY_MAP
    a_paused.with_suffix(".resume").write_text("")
    a_result = _read_result(proc_a)

    assert a_result["outcome"] == "committed"
    assert a_result["grow_events"] == [8 * TINY_MAP]
    assert a_result["final_map_size"] == 16 * TINY_MAP

    resume_sig.write_text("")
    reader_result = _read_result(reader)

    assert set(reader_result["old_keys"]) == {
        "v",
        "base0",
        "base1",
        "base2",
        "base3",
    }
    assert reader_result["old_values"]["v"] == b"v1".hex()
    assert "new0" in reader_result["fresh_keys"]
    assert "new5" in reader_result["fresh_keys"]
    assert reader_result["fresh_values"]["v"] == b"v2".hex()
    assert reader_result["fresh_values"]["dup"] == b"y".hex()

    inspection = _run_worker(
        db, db / "zict-journal", "inspect", 32 * TINY_MAP, "v", "dup", "new5"
    )
    assert inspection["values"]["v"] == b"v2".hex()
    assert inspection["values"]["dup"] == b"y".hex()
    assert inspection["values"]["new5"] == (b"z" * 2_000_000).hex()
    assert inspection["file_size"] == 16 * TINY_MAP
    assert list((db / "zict-journal").glob("*")) == []
