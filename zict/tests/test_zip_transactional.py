import json
import os
import struct
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

import zict
from zict import TransactionalZip, Zip
from zict.zip import ConflictError, SimulatedCrash

REPO_ROOT = Path(zict.__file__).resolve().parent.parent

BIG = bytes(range(256)) * 4096  # 1 MiB, stored uncompressed by zict.Zip

COMMIT_CHILD = """
import json, sys
from zict.zip import TransactionalZip

plan = json.load(open(sys.argv[1]))
changes = {k: bytes.fromhex(v) for k, v in plan["changes"].items()}
z = TransactionalZip(plan["filename"], crash_at=plan["crash_at"], crash_exit=True)
receipt = z.commit(changes=changes, deletions=plan["deletions"],
                   base=plan["base"], app_id=plan["app_id"])
print(json.dumps(receipt._asdict()))
"""

RECOVER_CHILD = """
import json, sys
from zict.zip import TransactionalZip

z = TransactionalZip(sys.argv[1])
out = {
    "members": {k: z[k].hex() for k in z},
    "revisions": z.revisions(),
    "generation": z.generation,
}
json.dump(out, open(sys.argv[2], "w"))
"""


def run_child(script, *args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
    )


def raw_member(path, name):
    """(header_offset, raw local header + compressed bytes) of a member"""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(name)
    with open(path, "rb") as f:
        f.seek(info.header_offset)
        header = f.read(30)
        fname_len, extra_len = struct.unpack("<HH", header[26:30])
        f.seek(info.header_offset)
        raw = f.read(30 + fname_len + extra_len + info.compress_size)
    return info.header_offset, raw


def write_plan(path, **kwargs):
    path.write_text(json.dumps(kwargs))
    return path


@pytest.fixture
def formal(tmp_path):
    """A standard zip archive, filled with zict Zip, holding one large member
    and a manifest whose path appears twice in the central directory (a legacy
    append-mode update)
    """
    fn = tmp_path / "formal.zip"
    with Zip(str(fn)) as z:
        z["big"] = BIG
        z["manifest"] = b"manifest-v1"
        z["part-b"] = b"B0"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(fn, "a") as zf:
            zf.writestr("manifest", b"manifest-v2")
    return fn


def test_nightly_handoff(formal, tmp_path, check_fd_leaks):
    fn = str(formal)
    handoff = fn + ".handoff"

    # The formal package starts with a duplicated manifest path
    with zipfile.ZipFile(fn) as zf:
        assert zf.namelist().count("manifest") == 2
        assert zf.read("manifest") == b"manifest-v2"
    big_offset, big_raw = raw_member(fn, "big")

    # A downstream reader opens the large member stream before the handoff
    downstream = zipfile.ZipFile(fn)
    stream = downstream.open("big")
    prefix = stream.read(100)

    # Two writers open the store and see the same member revisions
    za = TransactionalZip(fn)
    zb = TransactionalZip(fn)
    revs_a = za.revisions()
    revs_b = zb.revisions()
    assert revs_a == revs_b
    assert set(revs_a) == {"big", "manifest", "part-b"}

    # 甲 (A): delete the manifest, add partition part-a, at the old revisions
    ra = za.commit(
        changes={"part-a": b"A1"},
        deletions=["manifest"],
        base={"part-a": None, "manifest": revs_a["manifest"]},
        app_id="nightly-001",
    )
    assert ra.applied and ra.generation == 1
    with zipfile.ZipFile(fn) as zf:
        names = zf.namelist()
        assert "manifest" not in names  # deleted path fully gone from the CD
        assert zf.read("part-a") == b"A1"
        assert zf.read("big") == BIG

    # 乙 (B): stale commit based on the same old revisions -> manifest
    # conflict, and its other partition change must not leak in
    with pytest.raises(ConflictError) as excinfo:
        zb.commit(
            changes={"manifest": b"manifest-v3", "part-b": b"B1"},
            base={"manifest": revs_b["manifest"], "part-b": revs_b["part-b"]},
            app_id="nightly-002",
        )
    assert set(excinfo.value.conflicts) == {"manifest"}
    with zipfile.ZipFile(fn) as zf:
        assert zf.read("part-b") == b"B0"
        assert "manifest" not in zf.namelist()
        assert zf.namelist().count("part-a") == 1

    # The downstream stream opened before the commits reads the same
    # compressed entry to the end
    assert prefix + stream.read() == BIG
    stream.close()
    downstream.close()
    # Unchanged members keep their disk location and compressed bytes
    assert raw_member(fn, "big") == (big_offset, big_raw)

    # B resubmits its other item alone; the process dies after the new member
    # is written but before the new formal directory completes
    plan1 = write_plan(
        tmp_path / "plan1.json",
        filename=fn,
        changes={"part-b": b"B1".hex()},
        deletions=[],
        base={"part-b": revs_b["part-b"]},
        app_id="nightly-003",
        crash_at=["members_written"],
    )
    proc = run_child(COMMIT_CHILD, plan1)
    assert proc.returncode != 0

    # The handoff directory only ever holds the changed members, the new
    # directory, and recovery info - never a copy of the whole archive
    formal_size = os.path.getsize(fn)
    handoff_size = sum(f.stat().st_size for f in Path(handoff).iterdir())
    assert handoff_size < formal_size // 10

    # Until recovery, an independent reader still sees the complete previous
    # handoff
    with zipfile.ZipFile(fn) as zf:
        assert zf.read("part-b") == b"B0"
        assert zf.read("part-a") == b"A1"

    # A new process recovers and opens the formal path
    out1 = tmp_path / "recovered1.json"
    proc = run_child(RECOVER_CHILD, fn, out1)
    assert proc.returncode == 0, proc.stderr
    state = json.loads(out1.read_text())
    assert state["generation"] == 2
    assert bytes.fromhex(state["members"]["big"]) == BIG
    assert state["members"]["part-a"] == b"A1".hex()
    assert state["members"]["part-b"] == b"B1".hex()
    assert "manifest" not in state["members"]

    # The independent ZIP reader now sees the complete new handoff, with no
    # duplicate entries for the deleted path
    with zipfile.ZipFile(fn) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))
        assert zf.read("part-b") == b"B1"
    assert raw_member(fn, "big") == (big_offset, big_raw)

    # Second interruption: the new formal directory is already on disk, but
    # the process dies before the commit receipt is returned
    plan2 = write_plan(
        tmp_path / "plan2.json",
        filename=fn,
        changes={"part-c": b"C1".hex()},
        deletions=[],
        base={"part-c": None},
        app_id="nightly-004",
        crash_at=["directory_written"],
    )
    proc = run_child(COMMIT_CHILD, plan2)
    assert proc.returncode != 0

    # The durable directory is already readable as the complete new handoff
    with zipfile.ZipFile(fn) as zf:
        assert zf.read("part-c") == b"C1"

    # Recover in a new process, then retry with the same application id
    out2 = tmp_path / "recovered2.json"
    proc = run_child(RECOVER_CHILD, fn, out2)
    assert proc.returncode == 0, proc.stderr
    state = json.loads(out2.read_text())
    assert state["generation"] == 3
    assert state["members"]["part-c"] == b"C1".hex()

    plan3 = write_plan(
        tmp_path / "plan3.json",
        filename=fn,
        changes={"part-c": b"C1".hex()},
        deletions=[],
        base={"part-c": None},
        app_id="nightly-004",
        crash_at=[],
    )
    proc = run_child(COMMIT_CHILD, plan3)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt == {"app_id": "nightly-004", "generation": 3, "applied": False}

    # The retried application took effect exactly once; neither the conflict
    # nor the interruptions lost any later successful partition
    with zipfile.ZipFile(fn) as zf:
        names = zf.namelist()
        assert names.count("part-c") == 1
        assert len(names) == len(set(names))
        assert "manifest" not in names
        assert zf.read("big") == BIG
        assert zf.read("part-a") == b"A1"
        assert zf.read("part-b") == b"B1"
        assert zf.read("part-c") == b"C1"
    assert raw_member(fn, "big") == (big_offset, big_raw)
    assert not os.path.exists(os.path.join(handoff, "journal.json"))

    za.close()
    zb.close()


def test_conflict_tombstone_prevents_false_match(formal, check_fd_leaks):
    fn = str(formal)
    za = TransactionalZip(fn)
    revs = za.revisions()
    za.commit(
        deletions=["manifest"], base={"manifest": revs["manifest"]}, app_id="a1"
    )
    # A third writer recreates the manifest; a stale commit based on the
    # original revision must still conflict
    zb = TransactionalZip(fn)
    zb.commit(
        changes={"manifest": b"manifest-v3"}, base={"manifest": None}, app_id="a2"
    )
    zc = TransactionalZip(fn)
    with pytest.raises(ConflictError):
        zc.commit(
            changes={"manifest": b"manifest-v4"},
            base={"manifest": revs["manifest"]},
            app_id="a3",
        )
    with zipfile.ZipFile(fn) as zf:
        assert zf.read("manifest") == b"manifest-v3"
    za.close()
    zb.close()
    zc.close()


def test_retry_same_app_id_in_process(formal, check_fd_leaks):
    fn = str(formal)
    z = TransactionalZip(fn)
    r1 = z.commit(changes={"x": b"1"}, base={"x": None}, app_id="app-1")
    assert r1.applied
    r2 = z.commit(changes={"x": b"1"}, base={"x": None}, app_id="app-1")
    assert not r2.applied and r2.generation == r1.generation
    with zipfile.ZipFile(fn) as zf:
        assert zf.namelist().count("x") == 1
    # Same app_id, different request
    with pytest.raises(ValueError, match="app_id"):
        z.commit(changes={"x": b"2"}, base={"x": None}, app_id="app-1")
    z.close()


def test_recover_in_process(formal, check_fd_leaks):
    fn = str(formal)
    z = TransactionalZip(fn, crash_at={"meta_written"})
    with pytest.raises(SimulatedCrash):
        z.commit(changes={"x": b"1"}, base={"x": None}, app_id="app-1")
    # A fresh instance rolls the interrupted commit forward
    z2 = TransactionalZip(fn)
    assert z2["x"] == b"1"
    assert z2.generation == 1
    r = z2.commit(changes={"x": b"1"}, base={"x": None}, app_id="app-1")
    assert not r.applied
    with zipfile.ZipFile(fn) as zf:
        assert zf.namelist().count("x") == 1
    z.close()
    z2.close()


def test_commit_validation(formal, check_fd_leaks):
    z = TransactionalZip(str(formal))
    with pytest.raises(ValueError, match="app_id"):
        z.commit(changes={"x": b"1"}, base={"x": None})
    with pytest.raises(ValueError, match="base"):
        z.commit(changes={"x": b"1"}, app_id="a1")
    with pytest.raises(ValueError, match="changes and deletions"):
        z.commit(
            changes={"x": b"1"}, deletions=["x"], base={"x": None}, app_id="a1"
        )
    z.close()


def test_dedup_iter_len(formal, check_fd_leaks):
    z = TransactionalZip(str(formal))
    assert sorted(z) == ["big", "manifest", "part-b"]
    assert len(z) == 3
    assert z["manifest"] == b"manifest-v2"
    z.close()
