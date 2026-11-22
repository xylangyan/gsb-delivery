"""Tests for the crash-safe, cross-process block publishing protocol of
zict.File.

On-disk layout:
- ``<key>#<n>``       committed (published) version n of a key
- ``<key>#<n>.part``  fully or partially written, but never published
- ``<key>#tomb``      tombstone committing the deletion of a key
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from zict import File

# Two versions with different bytes *and* different lengths
V1 = b"first version"
V2 = b"2nd version -- much longer than the first"
V3 = b"v3"


def committed(tmp_path, key="x"):
    """Committed data files for *key* currently on disk"""
    return sorted(
        fn
        for fn in os.listdir(tmp_path)
        if fn.startswith(key + "#") and not fn.endswith((".part", "tomb"))
    )


class RefusingRemover:
    """Local stand-in for an OS (e.g. Windows) that refuses to unlink a file
    while it is still memory-mapped by some process.
    """

    def __init__(self, monkeypatch):
        self.blocked = set()
        real_remove = os.remove

        def fake_remove(path, *args, **kwargs):
            if os.path.basename(path) in self.blocked:
                raise PermissionError(
                    13,
                    "The process cannot access the file because it is being "
                    "used by another process",
                    str(path),
                )
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(os, "remove", fake_remove)


@pytest.mark.parametrize("memmap", [False, True])
def test_crash_before_publish_is_not_resurrected(tmp_path, memmap, check_fd_leaks):
    """A writer that is interrupted after fully writing the new file but
    before confirming the publish leaves an orphan .part file. On reopen,
    the old version is still current and the orphan is discarded.
    """
    z = File(tmp_path, memmap=memmap)
    z["x"] = V1
    # Fully written but never published; also a partially written file
    with open(tmp_path / "x#1.part", "wb") as fh:
        fh.write(V2)
    with open(tmp_path / "x#2.part", "wb") as fh:
        fh.write(V2[:5])

    z2 = File(tmp_path, memmap=memmap)
    assert "x" in z2
    assert z2["x"] == V1
    # The unpublished files were collected, not resurrected
    assert not any(fn.endswith(".part") for fn in os.listdir(tmp_path))
    assert committed(tmp_path) == ["x#0"]


@pytest.mark.parametrize("memmap", [False, True])
def test_crash_after_publish_before_receipt(tmp_path, memmap, check_fd_leaks):
    """A writer that is interrupted after the publish (rename) but before the
    receipt (deletion of the old version) leaves both versions on disk. On
    reopen, the published version is the current one and the old one is
    collected.
    """
    z = File(tmp_path, memmap=memmap)
    z["x"] = V1
    with open(tmp_path / "x#1", "wb") as fh:
        fh.write(V2)

    z2 = File(tmp_path, memmap=memmap)
    assert z2["x"] == V2
    assert committed(tmp_path) == ["x#1"]


def test_setitem_interrupted_before_publish(tmp_path, monkeypatch, check_fd_leaks):
    """Interrupt __setitem__ itself at the commit point: the rename never
    happens, so the old version remains current.
    """
    z = File(tmp_path)
    z["x"] = V1

    def boom(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        z["x"] = V2
    monkeypatch.undo()

    assert z["x"] == V1
    z2 = File(tmp_path)
    assert z2["x"] == V1
    assert committed(tmp_path) == ["x#0"]
    assert not any(fn.endswith(".part") for fn in os.listdir(tmp_path))


def test_setitem_interrupted_after_publish(tmp_path, monkeypatch, check_fd_leaks):
    """Interrupt __setitem__ right after the commit point, while sending the
    receipt: the new version has been published and stays current.
    """
    z = File(tmp_path)
    z["x"] = V1

    crashing = True
    real_remove = os.remove

    def maybe_crash(*args, **kwargs):
        if crashing:
            raise KeyboardInterrupt
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(os, "remove", maybe_crash)
    with pytest.raises(KeyboardInterrupt):
        z["x"] = V2
    crashing = False

    # Both versions are on disk; the published one is current
    assert committed(tmp_path) == ["x#0", "x#1"]
    z2 = File(tmp_path)
    assert z2["x"] == V2
    assert committed(tmp_path) == ["x#1"]


def test_failed_setitem_leaves_no_part(tmp_path, check_fd_leaks):
    z = File(tmp_path)
    with pytest.raises(TypeError):
        z["x"] = 123
    assert os.listdir(tmp_path) == []


def test_delete_with_live_mmap_subview(tmp_path, check_fd_leaks):
    """A reader keeps a slice of the mmap of version 1 after releasing the
    original view. Deleting the key and reopening the directory hides the
    key, but the old slice keeps reading its own data, and the mapping does
    not prevent publishing the next version.
    """
    zw = File(tmp_path, memmap=True)
    zw["x"] = V1

    zr = File(tmp_path, memmap=True)  # stand-in for a reader process
    view = zr["x"]
    sub = view[0:5]
    del view  # release the original view, keep only the slice

    zw["x"] = V2  # publish a new version while the slice is alive
    assert zw["x"] == V2
    assert bytes(sub) == V1[:5]

    del zw["x"]
    assert "x" not in zw

    # The deleted key does not reappear when the directory is reopened
    z2 = File(tmp_path, memmap=True)
    assert "x" not in z2
    assert list(z2) == []

    # The old slice still reads its own data
    assert bytes(sub) == V1[:5]

    # The existing mapping does not block publishing the next version
    zw["x"] = V3
    assert zw["x"] == V3
    assert bytes(sub) == V1[:5]

    sub.release()


@pytest.mark.parametrize("trigger", ["reopen", "write", "close"])
def test_delete_refused_while_mapped(tmp_path, monkeypatch, trigger, check_fd_leaks):
    """Simulate an OS that refuses to delete a file while it is mapped.
    The key disappears immediately and never reappears on reopen; the
    physical file is removed by the next open, write, or close() after the
    reader released its views (or exited).
    """
    blocker = RefusingRemover(monkeypatch)

    z = File(tmp_path, memmap=True)
    z["x"] = V1
    view = z["x"]
    sub = view[0:5]
    del view  # the reader released the original view but still holds a slice
    # The (simulated) OS now refuses to unlink the file of version 1
    blocker.blocked.add("x#0")

    # Publishing the next version is not blocked by the existing mapping
    z["x"] = V2
    assert z["x"] == V2
    # The old version could not be unlinked yet; its deletion is deferred
    assert "x#0" in os.listdir(tmp_path)

    del z["x"]
    assert "x" not in z

    # Reopening does not resurrect the deleted key, and cleanup still cannot
    # remove the old resources while the slice is alive
    z2 = File(tmp_path)
    assert "x" not in z2
    assert list(z2) == []
    assert "x#0" in os.listdir(tmp_path)
    assert "x#tomb" in os.listdir(tmp_path)

    # The reader releases the slice (or exits): the OS no longer refuses
    sub.release()
    blocker.blocked.discard("x#0")

    if trigger == "reopen":
        File(tmp_path).close()
    elif trigger == "write":
        z2["y"] = b"unrelated"
    else:
        z2.close()

    # The abandoned version and the tombstone have been discovered and removed
    assert "x#0" not in os.listdir(tmp_path)
    assert "x#tomb" not in os.listdir(tmp_path)


def test_cleanup_waits_until_slices_released(tmp_path, monkeypatch, check_fd_leaks):
    """As long as a process still holds a slice of an old version, cleanup -
    even when triggered by open, write, and close - must not delete the old
    resources."""
    blocker = RefusingRemover(monkeypatch)

    z = File(tmp_path, memmap=True)
    z["x"] = V1
    view = z["x"]
    sub = view[0:5]
    del view
    blocker.blocked.add("x#0")

    z["x"] = V2
    del z["x"]

    # Repeated cleanup attempts while the slice is alive
    z2 = File(tmp_path)
    z2["y"] = b"unrelated"
    z2.close()
    File(tmp_path).close()
    assert "x#0" in os.listdir(tmp_path)
    assert "x#tomb" in os.listdir(tmp_path)
    assert bytes(sub) == V1[:5]

    # Once the slice is released, the next cleanup collects everything
    sub.release()
    blocker.blocked.discard("x#0")
    z2["y"] = b"again"
    assert "x#0" not in os.listdir(tmp_path)
    assert "x#tomb" not in os.listdir(tmp_path)
    assert committed(tmp_path) == []  # no committed files left for "x"


def test_non_memmap_reads_only_published_data(tmp_path, check_fd_leaks):
    """A non-memmap reader only ever sees confirmed, complete data - never a
    partially or fully written file that was not published yet."""
    zw = File(tmp_path, memmap=True)
    zw["x"] = V1
    with open(tmp_path / "x#1.part", "wb") as fh:
        fh.write(V2)

    zr = File(tmp_path, memmap=False)
    out = zr["x"]
    assert isinstance(out, bytearray)
    assert out == V1
    assert committed(tmp_path) == ["x#0"]


def test_versions_monotonic_across_reopens(tmp_path, check_fd_leaks):
    """File names are never reused, also across processes"""
    z = File(tmp_path)
    z["x"] = V1
    z2 = File(tmp_path)
    z2["x"] = V2
    assert os.listdir(tmp_path) == ["x#1"]
    assert z2["x"] == V2
    z3 = File(tmp_path)
    assert z3["x"] == V2


def test_cross_process_reader_holds_slice(tmp_path, check_fd_leaks):
    """End-to-end with a real reader process: the reader keeps a slice of
    version 1 while another process deletes the key and publishes the next
    version. The reader still reads its own data; after it exits, cleanup
    discovers the abandoned version.
    """
    child = textwrap.dedent(
        f"""
        import sys
        from zict import File

        z = File(sys.argv[1], memmap=True)
        view = z["x"]
        sub = view[0:5]
        del view  # keep only the slice
        print("mapped", flush=True)
        sys.stdin.readline()  # wait for the parent to delete and republish
        assert bytes(sub) == {V1[:5]!r}, bytes(sub)
        print("ok", flush=True)
        """
    )
    z = File(tmp_path, memmap=True)
    z["x"] = V1

    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "mapped"

        del z["x"]
        z2 = File(tmp_path, memmap=True)
        assert "x" not in z2  # deleted key stays deleted across processes
        z2["x"] = V3  # the child's mapping does not block publishing
        assert z2["x"] == V3

        proc.stdin.write("go\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "ok"
        assert proc.wait(timeout=60) == 0
    finally:
        proc.kill()

    # After the reader exited, the next open discovers the abandoned version
    z3 = File(tmp_path)
    assert len(committed(tmp_path)) == 1
    assert z3["x"] == V3
