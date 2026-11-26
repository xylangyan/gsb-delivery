from __future__ import annotations

import threading
import time

from fasteners.lock import ReaderWriterLock


class TracedCondition(threading.Condition):
    def __init__(self, on_wait):
        super().__init__()
        self._on_wait = on_wait

    def wait(self, timeout=None):
        self._on_wait(threading.current_thread())
        return super().wait(timeout)


def make_lock(wait_events):
    def on_wait(thread):
        event = wait_events.get(thread.name)
        if event is not None:
            event.set()

    return ReaderWriterLock(condition_cls=lambda: TracedCondition(on_wait))


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def event_bag(names):
    return {name: threading.Event() for name in names}


def verify_full_chain(order):
    wait_events = {
        "new-reader": threading.Event(),
        "writer-2": threading.Event(),
    }
    events = event_bag([
        "old_r1", "allow_r2", "old_r2",
        "release_old_one", "old_one_done",
        "release_old_final", "old_done",
        "w1_acquired", "w1_run", "inner_granted", "published",
        "w1_exit_write", "w1_write_released",
        "w1_release_inner", "w1_inner_released",
        "w2_acquired", "w2_release",
        "new_acquired", "new_release",
    ])
    lock = make_lock(wait_events)
    version = {"current": "V0"}
    observed = {}
    trace = []

    def old_reader():
        lock.acquire_read_lock()
        observed["old1"] = version["current"]
        trace.append(f"G(R_old#1)={observed['old1']}")
        events["old_r1"].set()

        events["allow_r2"].wait(2)
        lock.acquire_read_lock()
        observed["old2"] = version["current"]
        trace.append(f"G(R_old#2)={observed['old2']} while W1 pending")
        events["old_r2"].set()

        events["release_old_one"].wait(2)
        lock.release_read_lock()
        trace.append("Rel(R_old#2): one reader entry remains")
        events["old_one_done"].set()

        events["release_old_final"].wait(2)
        lock.release_read_lock()
        trace.append("Rel(R_old#1): readers empty")
        events["old_done"].set()

    def writer_one():
        trace.append("Q(W1)")
        lock.acquire_write_lock()
        trace.append("G(W1)")
        events["w1_acquired"].set()
        events["w1_run"].wait(2)

        if order == "read-before-publish":
            lock.acquire_read_lock()
            observed["inner"] = version["current"]
            observed["grant_marker"] = (
                "acquire_read_lock returned / _readers[T_W1] inserted, before P1"
            )
            trace.append("G(R_inner)")
            events["inner_granted"].set()

            version["current"] = "V1"
            trace.append("P1=V1")
            events["published"].set()
        else:
            version["current"] = "V1"
            trace.append("P1=V1")
            events["published"].set()

            lock.acquire_read_lock()
            observed["inner"] = version["current"]
            observed["grant_marker"] = (
                "acquire_read_lock returned / _readers[T_W1] inserted, after P1"
            )
            trace.append("G(R_inner)")
            events["inner_granted"].set()

        events["w1_exit_write"].wait(2)
        lock.release_write_lock()
        trace.append("exit W1 write: writer=None, R_inner still held")
        events["w1_write_released"].set()

        events["w1_release_inner"].wait(2)
        lock.release_read_lock()
        trace.append("Rel(R_inner)")
        events["w1_inner_released"].set()

    def writer_two():
        trace.append("Q(W2)")
        lock.acquire_write_lock()
        version["current"] = "V2"
        trace.append("G(W2) and P2=V2")
        events["w2_acquired"].set()

        events["w2_release"].wait(2)
        lock.release_write_lock()
        trace.append("Rel(W2)")

    def new_reader():
        trace.append("request R_new: wait behind pending/active writer")
        lock.acquire_read_lock()
        observed["new"] = version["current"]
        trace.append(f"G(R_new)={observed['new']} after W2 release")
        events["new_acquired"].set()

        events["new_release"].wait(2)
        lock.release_read_lock()

    old = threading.Thread(target=old_reader, name="old-reader")
    w1 = threading.Thread(target=writer_one, name="writer-1")
    w2 = threading.Thread(target=writer_two, name="writer-2")
    new = threading.Thread(target=new_reader, name="new-reader")

    old.start()
    assert events["old_r1"].wait(2)

    w1.start()
    assert wait_for(lambda: lock.has_pending_writers)

    events["allow_r2"].set()
    assert events["old_r2"].wait(2)
    assert observed["old1"] == "V0"
    assert observed["old2"] == "V0"
    assert lock._readers[old] == 2

    new.start()
    assert wait_events["new-reader"].wait(2)
    assert not events["new_acquired"].wait(0.05)

    events["release_old_one"].set()
    assert events["old_one_done"].wait(2)
    assert lock._readers[old] == 1
    assert not events["w1_acquired"].wait(0.05)

    events["release_old_final"].set()
    assert events["w1_acquired"].wait(2)
    assert events["old_done"].wait(2)
    assert not lock._readers

    w2.start()
    assert wait_for(lambda: lock.has_pending_writers)
    assert wait_events["writer-2"].wait(2)

    events["w1_run"].set()
    assert events["published"].wait(2)
    assert events["inner_granted"].wait(2)
    assert observed["inner"] == ("V0" if order == "read-before-publish" else "V1")

    events["w1_exit_write"].set()
    assert events["w1_write_released"].wait(2)
    assert lock._writer is None
    assert lock._readers[w1] == 1
    assert lock._pending_writers[0] is w2
    assert not events["w2_acquired"].wait(0.1)
    assert not events["new_acquired"].wait(0.05)

    events["w1_release_inner"].set()
    assert events["w1_inner_released"].wait(2)
    assert events["w2_acquired"].wait(2)
    assert not events["new_acquired"].wait(0.05)

    events["w2_release"].set()
    assert events["new_acquired"].wait(2)
    assert observed["new"] == "V2"

    events["new_release"].set()
    for thread in (old, w1, w2, new):
        thread.join(2)
        assert not thread.is_alive()

    return {
        "order": order,
        "inner_version": observed["inner"],
        "new_reader_version": observed["new"],
        "actual_inner_grant": observed["grant_marker"],
        "trace": trace,
    }


def verify_contradictions():
    lock = ReaderWriterLock()
    outcomes = []

    def reader_then_writer():
        lock.acquire_read_lock()
        try:
            lock.acquire_write_lock()
        except RuntimeError:
            outcomes.append("reader-escalation")
        lock.release_read_lock()

    reader = threading.Thread(target=reader_then_writer, daemon=True)
    reader.start()
    reader.join(2)

    pending_lock = ReaderWriterLock()
    pending_lock._pending_writers.append(threading.current_thread())
    try:
        pending_lock.acquire_read_lock()
    except RuntimeError:
        outcomes.append("pending-writer-read")
    finally:
        pending_lock._pending_writers.clear()

    assert outcomes == ["reader-escalation", "pending-writer-read"]
    return outcomes


if __name__ == "__main__":
    contradictions = verify_contradictions()
    evidence_a = verify_full_chain("read-before-publish")
    evidence_b = verify_full_chain("publish-before-read")

    print("CONTRADICTION_REJECTIONS")
    for outcome in contradictions:
        print(f"  - {outcome}")

    for evidence in (evidence_a, evidence_b):
        print(f"REPLAY {evidence['order']}")
        print(f"  R_inner: {evidence['inner_version']}")
        print(f"  R_new: {evidence['new_reader_version']}")
        print(f"  grant: {evidence['actual_inner_grant']}")
        for step in evidence["trace"]:
            print(f"  step: {step}")

    print("ALL_CHECKS_PASSED")
