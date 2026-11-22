import pickle
import threading
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from zict import (
    BatchConflictError,
    DeferredWriteError,
    RelocatingSieve,
    RelocationItem,
    RelocationRequest,
    VersionMismatchError,
)


class FailOnceStaging(UserDict):
    """Staging backend that, while armed, stores the value and then reports
    one deferred failure (the confirmation arrives later).
    """

    def __init__(self):
        super().__init__()
        self.armed = False
        self.failed = False

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        if self.armed and not self.failed:
            self.failed = True
            raise DeferredWriteError("fate of the write is unknown")


def _to_near(k, v):
    return "near"


def make_directory():
    """A partitioned directory: a job's body A and index B live in different
    layers. Two layer names ("near" and "near-alias") point to the same
    physical mapping.
    """
    near = {}
    far = {}
    near_staging = FailOnceStaging()
    z = RelocatingSieve(
        {"near": near, "near-alias": near, "far": far},
        selector=_to_near,
        staging={"near": near_staging},
    )
    seed = RelocationRequest(
        batch_id="seed",
        partition="job",
        items=(
            RelocationItem("A", b"body-v1", "near"),
            RelocationItem("B", b"index-v1", "far"),
        ),
    )
    receipt = z.relocate(seed)
    assert receipt.status == "committed"
    assert receipt.versions == {"A": 1, "B": 1}
    assert near == {"A": b"body-v1"}
    assert far == {"B": b"index-v1"}
    return z, near, far, near_staging


def test_joint_relocation_with_deferred_failure_and_retries():
    z, near, far, near_staging = make_directory()

    # First batch: swap A (near -> far) and B (far -> near).
    # A reaches the staging area; B's backend reports one deferred failure.
    near_staging.armed = True
    batch1 = RelocationRequest(
        batch_id="batch-1",
        partition="job",
        items=(
            RelocationItem("A", b"body-v2", "far"),
            RelocationItem("B", b"index-v2", "near"),
        ),
        expected={"A": 1, "B": 1},
    )
    receipt = z.relocate(batch1)
    assert receipt.status == "pending"
    assert z.staging["far"] == {("batch-1", "A"): b"body-v2"}
    assert dict(near_staging) == {("batch-1", "B"): b"index-v2"}
    # No half-relocated group is visible: both members are still the old ones
    assert z.read_group("job", ["A", "B"]) == {"A": b"body-v1", "B": b"index-v1"}
    assert near == {"A": b"body-v1"}
    assert far == {"B": b"index-v1"}

    # Second batch: update only B, written through the other layer name that
    # aliases the mapping the first batch is still preparing.
    batch2 = RelocationRequest(
        batch_id="batch-2",
        partition="job",
        items=(RelocationItem("B", b"index-v2b", "near-alias"),),
        expected={"B": 1},
    )
    receipt2 = z.relocate(batch2)
    assert receipt2.status == "committed"
    assert receipt2.locations == {"B": "near-alias"}
    assert receipt2.versions == {"B": 2}
    assert z.read_group("job", ["A", "B"]) == {"A": b"body-v1", "B": b"index-v2b"}
    assert near == {"A": b"body-v1", "B": b"index-v2b"}
    assert far == {}

    # Immediately resubmit the first batch with its original id: the version
    # guard rejects it; it must not overwrite the new B or modify the old A.
    with pytest.raises(VersionMismatchError):
        z.relocate(batch1)
    assert near == {"A": b"body-v1", "B": b"index-v2b"}
    assert far == {}

    # The delayed failure receipt for the first batch finally arrives;
    # its staged leftovers are cleaned up.
    assert z.confirm_failure("batch-1") == "failed"
    assert z.staging["far"] == {}
    assert dict(near_staging) == {}

    # Same id and same expected versions, but different content for A:
    # explicit id conflict, again without side effects.
    tampered = replace(
        batch1,
        items=(
            RelocationItem("A", b"body-v2-TAMPERED", "far"),
            RelocationItem("B", b"index-v2", "near"),
        ),
    )
    with pytest.raises(BatchConflictError):
        z.relocate(tampered)
    assert near == {"A": b"body-v1", "B": b"index-v2b"}
    assert far == {}

    # Revise the first batch for the new B and commit it.
    revised = RelocationRequest(
        batch_id="batch-1-revised",
        partition="job",
        items=(
            RelocationItem("A", b"body-v3", "far"),
            RelocationItem("B", b"index-v2b", "near"),
        ),
        expected={"A": 1, "B": 2},
    )
    receipt3 = z.relocate(revised)
    assert receipt3.status == "committed"
    assert receipt3.locations == {"A": "far", "B": "near"}
    assert receipt3.versions == {"A": 2, "B": 3}
    # The two physical mappings only keep the effective versions
    assert near == {"B": b"index-v2b"}
    assert far == {"A": b"body-v3"}
    assert z.read_group("job", ["A", "B"]) == {"A": b"body-v3", "B": b"index-v2b"}

    # Identical replay: reported as already published, nothing is rewritten.
    replay = z.relocate(revised)
    assert replay.status == "already_published"
    assert replay.locations == receipt3.locations
    assert replay.versions == receipt3.versions
    assert near == {"B": b"index-v2b"}
    assert far == {"A": b"body-v3"}


def test_no_half_relocated_group_visible():
    entered = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()

    class BlockingLive(UserDict):
        armed = False

        def __setitem__(self, k, v):
            if self.armed:
                entered.set()
                assert release.wait(timeout=5)
            super().__setitem__(k, v)

    near = BlockingLive()
    far = {}
    z = RelocatingSieve({"near": near, "far": far}, selector=lambda k, v: "near")
    z.relocate(
        RelocationRequest(
            batch_id="seed",
            partition="job",
            items=(
                RelocationItem("A", b"body-v1", "near"),
                RelocationItem("B", b"index-v1", "far"),
            ),
        )
    )

    # Swap A and B; the commit blocks halfway (A already moved to far).
    near.armed = True
    batch = RelocationRequest(
        batch_id="batch-1",
        partition="job",
        items=(
            RelocationItem("A", b"body-v2", "far"),
            RelocationItem("B", b"index-v2", "near"),
        ),
        expected={"A": 1, "B": 1},
    )

    def reader():
        reader_started.set()
        return z.read_group("job", ["A", "B"])

    with ThreadPoolExecutor(2) as ex:
        relocate_fut = ex.submit(z.relocate, batch)
        assert entered.wait(timeout=5)
        read_fut = ex.submit(reader)
        assert reader_started.wait(timeout=5)
        # The reader is stuck behind the commit and cannot observe the
        # half-relocated group
        assert not read_fut.done()
        release.set()
        assert relocate_fut.result().status == "committed"
        assert read_fut.result() == {"A": b"body-v2", "B": b"index-v2"}


def test_blocked_backend_does_not_pause_other_partitions():
    entered = threading.Event()
    release = threading.Event()

    class BlockingStaging(UserDict):
        def __setitem__(self, k, v):
            entered.set()
            assert release.wait(timeout=5)
            super().__setitem__(k, v)

    z = RelocatingSieve(
        {"near": {}, "far": {}, "archive": {}},
        selector=lambda k, v: "near",
        staging={"far": BlockingStaging()},
    )
    z.relocate(
        RelocationRequest(
            batch_id="seed-1",
            partition="job-1",
            items=(RelocationItem("A", b"a1", "near"),),
        )
    )
    z.relocate(
        RelocationRequest(
            batch_id="seed-2",
            partition="job-2",
            items=(RelocationItem("C", b"c1", "near"),),
        )
    )

    blocked = RelocationRequest(
        batch_id="batch-x",
        partition="job-1",
        items=(RelocationItem("A", b"a2", "far"),),
        expected={"A": 1},
    )
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(z.relocate, blocked)
        assert entered.wait(timeout=5)
        # Reads of the other partition are not paused
        assert z.read_group("job-2", ["C"]) == {"C": b"c1"}
        # ... and neither are its relocations
        receipt = z.relocate(
            RelocationRequest(
                batch_id="batch-y",
                partition="job-2",
                items=(RelocationItem("C", b"c2", "archive"),),
                expected={"C": 1},
            )
        )
        assert receipt.status == "committed"
        assert z.read_group("job-2", ["C"]) == {"C": b"c2"}
        # The blocked partition still reads its old member
        assert z.read_group("job-1", ["A"]) == {"A": b"a1"}
        release.set()
        assert fut.result().status == "committed"
    assert z.read_group("job-1", ["A"]) == {"A": b"a2"}


def test_versions_mapping_interface():
    z = RelocatingSieve({"near": {}, "far": {}}, selector=lambda k, v: "near")
    z["x"] = b"1"
    assert z.versions["x"] == 1
    z["x"] = b"2"
    assert z.versions["x"] == 2
    z.update({"y": b"1"})
    assert z.versions["y"] == 1
    del z["x"]
    assert "x" not in z.versions

    # The version guard also applies to keys written through the plain
    # mapping interface
    with pytest.raises(VersionMismatchError):
        z.relocate(
            RelocationRequest(
                batch_id="batch-1",
                partition="job",
                items=(RelocationItem("y", b"2", "far"),),
                expected={"y": 0},
            )
        )
    assert z["y"] == b"1"


def test_pickle():
    z, near, far, _ = make_directory()
    z2 = pickle.loads(pickle.dumps(z))
    assert z2.read_group("job", ["A", "B"]) == {"A": b"body-v1", "B": b"index-v1"}
    receipt = z2.relocate(
        RelocationRequest(
            batch_id="batch-1",
            partition="job",
            items=(RelocationItem("A", b"body-v2", "far"),),
            expected={"A": 1},
        )
    )
    assert receipt.status == "committed"
    assert z2.read_group("job", ["A"]) == {"A": b"body-v2"}
