#!/usr/bin/env python

"""Tests for the single-source, multi-subscriber ZipFanout."""

import io
import threading
import time
import zipfile

import pytest

from zipstream import (
    ZipStream,
    ZipFanout,
    ZIP_DEFLATED,
    SubscriberClosed,
    StagingWindowExpired,
    SourceIncomplete,
)


################################
# Helpers
################################

class ControlledIterable:
    """Yield fixed-size chunks and count how often iteration is started.

    The count lets tests prove the read-once source is never re-opened, even
    when a subscriber is cancelled. Optionally raises after `fail_after`
    chunks to simulate a source that dies partway through a member.
    """

    def __init__(self, data, chunk_size=10):
        self._data = data
        self._chunk_size = chunk_size
        self.iterations = 0
        self.chunks_produced = 0
        self._fail_after = None
        self._fail_exc = None

    def fail_after(self, count, exc):
        self._fail_after = count
        self._fail_exc = exc

    def __iter__(self):
        self.iterations += 1
        index = 0
        for start in range(0, len(self._data), self._chunk_size):
            if self._fail_after is not None and index >= self._fail_after:
                raise self._fail_exc
            index += 1
            self.chunks_produced += 1
            yield self._data[start:start + self._chunk_size]


def _zip_bytes(subscriber, timeout=5):
    chunks = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("subscriber did not finish in time")
        try:
            chunks.append(next(subscriber))
        except StopIteration:
            return b"".join(chunks)


def _parse(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None
    return zf


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("condition was not met in time")


def _entry_done(subscriber, arcname):
    with subscriber._fanout._cond:
        return any(z.filename == arcname for z in subscriber._delivered)


def _three_member_source(chunk_size=10):
    x_data = b"x" * (chunk_size * 6)
    y_data = b"y" * (chunk_size * 4)
    z_data = b"z" * (chunk_size * 2)
    x_iter = ControlledIterable(x_data, chunk_size)
    zs = ZipStream()
    zs.add(x_iter, "x")
    zs.add(y_data, "y")
    zs.add(z_data, "z")
    return zs, x_iter, x_data, y_data, z_data


################################
# Basic delivery
################################

def test_complete_and_selective_subscribers_each_get_valid_zips():
    zs, x_iter, x_data, y_data, z_data = _three_member_source()
    fan = ZipFanout(zs, staging_limit=1 << 20)

    complete = fan.subscribe("complete", complete=True)
    only_x = fan.subscribe("only-x", members=["x"])
    only_y = fan.subscribe("only-y", members=["y"])
    xy = fan.subscribe("xy", members=["x", "y"])

    fan.run_threaded()

    zf_full = _parse(_zip_bytes(complete))
    zf_x = _parse(_zip_bytes(only_x))
    zf_y = _parse(_zip_bytes(only_y))
    zf_xy = _parse(_zip_bytes(xy))

    assert set(zf_full.namelist()) == {"x", "y", "z"}
    assert zf_full.read("x") == x_data
    assert zf_full.read("y") == y_data
    assert zf_full.read("z") == z_data

    assert zf_x.namelist() == ["x"]
    assert zf_x.read("x") == x_data
    assert zf_y.namelist() == ["y"]
    assert zf_y.read("y") == y_data
    assert set(zf_xy.namelist()) == {"x", "y"}

    # The source iterator was driven exactly once for the whole fanout.
    assert x_iter.iterations == 1


def test_each_subscriber_has_independent_offsets_and_central_directory():
    zs = ZipStream()
    zs.add(b"w" * 30, "w")
    zs.add(b"x" * 30, "x")
    zs.add(b"y" * 30, "y")

    fan = ZipFanout(zs, staging_limit=1 << 20)
    complete = fan.subscribe("A", complete=True)
    only_x = fan.subscribe("B", members=["x"])
    fan.run_threaded()

    full = _parse(_zip_bytes(complete))
    partial = _parse(_zip_bytes(only_x))

    # B's x starts at zero in its own package while A's x follows w.
    assert full.getinfo("x").header_offset != 0
    assert partial.getinfo("x").header_offset == 0
    with fan._cond:
        a_x_offset = next(
            z.header_offset for z in complete._delivered if z.filename == "x"
        )
        b_x_offset = only_x._delivered[0].header_offset
    assert a_x_offset != b_x_offset


def test_compressed_entries_round_trip_with_own_central_directories():
    zs = ZipStream(compress_type=ZIP_DEFLATED)
    zs.add(b"compressible " * 200, "x")
    zs.add(b"other data " * 100, "y")

    fan = ZipFanout(zs, staging_limit=1 << 20)
    complete = fan.subscribe("A", complete=True)
    only_y = fan.subscribe("B", members=["y"])
    fan.run_threaded()

    full = _parse(_zip_bytes(complete))
    partial = _parse(_zip_bytes(only_y))
    assert full.getinfo("x").compress_type == ZIP_DEFLATED
    assert full.read("x") == b"compressible " * 200
    assert partial.getinfo("y").compress_type == ZIP_DEFLATED
    assert partial.read("y") == b"other data " * 100


################################
# Cancellation / read-once guarantees
################################

def test_cancelling_a_subscriber_keeps_others_offsets_and_data_intact():
    zs, x_iter, x_data, y_data, z_data = _three_member_source(chunk_size=10)
    fan = ZipFanout(zs, staging_limit=1 << 20)

    a = fan.subscribe("A", complete=True)
    b = fan.subscribe("B", members=["x"])
    c = fan.subscribe("C", members=["y"])
    fan.run_threaded()

    # Drive A through the whole of x (up to its data descriptor).
    a_chunks = []
    while not _entry_done(a, "x"):
        a_chunks.append(next(a))

    # Pause B partway through x's content (past the local header, mid-data).
    b_chunks = []
    while True:
        b_chunks.append(next(b))
        with fan._cond:
            sent = b._entry_bytes_sent if b._current is not None else 0
        if sent >= 41:
            break
    # B has already published its own local header for x (at offset 0 in B's
    # own package). Record it so we can prove cancellation never rewrites it.
    assert b_chunks[0][:4] == b"PK\x03\x04"
    b_header_prefix = bytes(b_chunks[0])

    # Cancel A mid-stream and let the rest of the source flow to B and C.
    a.close()
    with pytest.raises(SubscriberClosed):
        next(a)

    b_data = b"".join(b_chunks) + _zip_bytes(b)
    c_zip = _parse(_zip_bytes(c))

    b_zip = _parse(b_data)
    assert b_zip.namelist() == ["x"]
    assert b_zip.read("x") == x_data
    # B's x still lives at B's own offset, never re-pointed into A's package.
    assert b_zip.getinfo("x").header_offset == 0
    assert b_data[:len(b_header_prefix)] == b_header_prefix
    assert c_zip.namelist() == ["y"]
    assert c_zip.read("y") == y_data
    assert c_zip.getinfo("y").header_offset == 0

    # The shared x input was iterated exactly once despite A's cancellation.
    assert x_iter.iterations == 1
    fan.wait_until_idle(5)


def test_close_releases_staging_and_is_idempotent():
    zs, x_iter, _, _, _ = _three_member_source()
    fan = ZipFanout(zs, staging_limit=1 << 20)
    sub = fan.subscribe("slow", complete=True)
    fan.run_threaded()

    _wait_for(lambda: fan._retained_total > 0)
    sub.close()
    sub.close()  # must not raise
    _wait_for(lambda: fan._retained_total == 0)
    assert sub.closed


def test_selective_subscriber_finalizes_before_source_is_exhausted():
    zs = ZipStream()
    zs.add(b"y" * 20, "y")
    zs.add(b"z" * 20, "z")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    only_y = fan.subscribe("C", members=["y"])
    fan.run_threaded()

    zf = _parse(_zip_bytes(only_y))
    assert zf.namelist() == ["y"]
    assert only_y.is_finished()


################################
# Staging window / slow reader
################################

def test_slow_reader_can_resume_within_retained_window():
    zs, x_iter, x_data, _, _ = _three_member_source(chunk_size=10)
    fan = ZipFanout(zs, staging_limit=1 << 20)
    fast = fan.subscribe("fast", complete=True)
    slow = fan.subscribe("slow", members=["x"])
    fan.run_threaded()

    first = next(slow)
    assert first[:4] == b"PK\x03\x04"
    # While slow is paused mid-x the fast reader runs to completion.
    _zip_bytes(fast)
    resumed = _zip_bytes(slow)
    zf = _parse(first + resumed)
    assert zf.read("x") == x_data


def test_slow_reader_outside_window_is_explicitly_rejected():
    # Small budget with an actively-draining fast reader: a paused slow reader
    # that blocks reclamation must be failed instead of stalling the fast one.
    # A gate per later entry makes the pump/fast alternation deterministic.
    entry_count = 8
    gates = [threading.Event() for _ in range(entry_count - 1)]

    def gated(data, event):
        class Gated:
            def __iter__(self):
                event.wait(5)
                yield data
        return Gated()

    zs = ZipStream()
    payloads = [bytes([ord("a") + i]) * 40 for i in range(entry_count)]
    zs.add(payloads[0], "m0")
    for i in range(1, entry_count):
        # size keeps the local header compact (no forced zip64)
        zs.add(gated(payloads[i], gates[i - 1]), "m{}".format(i), size=40)

    fan = ZipFanout(zs, staging_limit=120)
    fast = fan.subscribe("fast", complete=True)
    slow = fan.subscribe("slow", members=["m0"])
    fan.run_threaded()

    # Pump is blocked at m1's gate: read m0's local header and stall, pinning
    # slow partway through m0's content.
    header = next(slow)
    assert header[:4] == b"PK\x03\x04"

    fast_result = {}

    def drain_fast():
        try:
            fast_result["zip"] = _zip_bytes(fast)
        except BaseException as exc:  # pragma: no cover - failure path
            fast_result["error"] = exc

    fast_thread = threading.Thread(target=drain_fast)
    fast_thread.start()

    # Strict alternation: let the pump produce entry i+1 only after fast has
    # finished entry i, so fast is never the straggler.
    for i in range(entry_count - 1):
        _wait_for(lambda i=i: len(fast._delivered) >= i + 1)
        gates[i].set()
    fast_thread.join(5)
    assert not fast_thread.is_alive()
    assert "error" not in fast_result

    fast_zip = _parse(fast_result["zip"])
    assert set(fast_zip.namelist()) == {
        "m{}".format(i) for i in range(entry_count)
    }

    with pytest.raises(StagingWindowExpired) as exc_info:
        _zip_bytes(slow)
    # The error reports where the resumable window ended.
    assert exc_info.value.window_end_offset is not None
    assert slow._failed


def test_late_subscriber_within_window_gets_retained_entries():
    zs, _, x_data, y_data, _ = _three_member_source()
    fan = ZipFanout(zs, staging_limit=1 << 20)
    first = fan.subscribe("first", complete=True)
    fan.run()

    # Everything was produced and retained (budget is huge) before subscribing.
    late = fan.subscribe("late", members=["x", "y"])
    zf = _parse(_zip_bytes(late))
    assert set(zf.namelist()) == {"x", "y"}
    assert zf.read("x") == x_data
    assert zf.read("y") == y_data
    _zip_bytes(first)


def test_late_subscriber_outside_window_gets_window_expired():
    zs = ZipStream()
    zs.add(b"x" * 50, "x")
    zs.add(b"y" * 50, "y")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    first = fan.subscribe("first", complete=True)
    fan.run()
    _zip_bytes(first)  # drains everything so staging can be reclaimed
    fan._reclaim_locked()

    late = fan.subscribe("late", members=["x"])
    with pytest.raises(StagingWindowExpired) as exc_info:
        _zip_bytes(late)
    assert exc_info.value.window_end_offset is not None


################################
# Source failure semantics
################################

class _Boom(Exception):
    pass


def _failing_source():
    good = b"x" * 30
    zs = ZipStream()
    zs.add(good, "x")

    def partial_y():
        yield b"y" * 10
        yield b"y" * 10
        raise _Boom("source iterator died")

    zs.add(partial_y(), "y")
    zs.add(b"z" * 10, "z")
    return zs, good


def test_source_failure_only_subscribers_with_complete_entries_succeed():
    zs, good = _failing_source()
    fan = ZipFanout(zs, staging_limit=1 << 20)

    promised = fan.subscribe("promised", complete=True)
    has_x = fan.subscribe("has-x", members=["x"])
    wants_both = fan.subscribe("wants-both", members=["x", "y"])
    wants_y_only = fan.subscribe("wants-y", members=["y"])
    fan.run_threaded()

    # A complete package cannot silently become a missing-file preview.
    with pytest.raises(SourceIncomplete):
        _zip_bytes(promised)

    zf_x = _parse(_zip_bytes(has_x))
    assert zf_x.namelist() == ["x"]
    assert zf_x.read("x") == good
    assert has_x.missing == set()

    # x was fully delivered before the failure; y is reported as missing.
    zf_partial = _parse(_zip_bytes(wants_both))
    assert zf_partial.namelist() == ["x"]
    assert wants_both.missing == {"y"}

    # No complete entry for y-only means it cannot deliver anything valid.
    with pytest.raises(SourceIncomplete):
        _zip_bytes(wants_y_only)


def test_source_failure_before_any_entry_fails_everyone():
    zs = ZipStream()

    def nothing_usable():
        raise _Boom("dead on arrival")
        yield b""  # pragma: no cover

    zs.add(nothing_usable(), "x")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    selective = fan.subscribe("selective", members=["x"])
    promised = fan.subscribe("promised", complete=True)
    fan.run_threaded()

    with pytest.raises(SourceIncomplete):
        _zip_bytes(selective)
    with pytest.raises(SourceIncomplete):
        _zip_bytes(promised)
    assert isinstance(fan.source_error, _Boom)


def test_failed_member_is_never_published_half_completed():
    gate = threading.Event()

    class PartialThenBoom:
        def __iter__(self):
            yield b"partial-" * 3
            gate.wait(5)
            raise _Boom("died mid-member")
            yield b""  # pragma: no cover

    zs = ZipStream()
    zs.add(b"good", "g")
    zs.add(PartialThenBoom(), "bad")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    good_only = fan.subscribe("good-only", members=["g"])
    promised = fan.subscribe("promised", complete=True)
    fan.run_threaded()

    # The complete subscriber consumes the finished member and blocks waiting
    # for "bad" while the source is parked inside it.
    chunks = []
    _wait_for(lambda: promised._current is None and promised._queue
              and promised._queue[0].arcname == "g")
    gate.set()

    with pytest.raises(SourceIncomplete):
        _zip_bytes(promised)

    # "bad" never completed, so it must not have been published to anyone.
    with fan._cond:
        bad = next(e for e in fan._entries if e.arcname == "bad")
        assert bad.zinfo is None
        for sub in fan._subscribers:
            assert all(r is not bad for r in sub._queue)
            assert all(z.filename != "bad" for z in sub._delivered)

    # The selective subscriber that only wanted the finished member succeeds.
    zf = _parse(_zip_bytes(good_only))
    assert zf.namelist() == ["g"]
    assert zf.read("g") == b"good"
    assert good_only.missing == set()


################################
# API validation
################################

def test_subscription_arguments_are_validated():
    fan = ZipFanout(ZipStream())
    with pytest.raises(ValueError):
        fan.subscribe("no-members", members=[])
    with pytest.raises(ValueError):
        fan.subscribe("nothing")
    with pytest.raises(ValueError):
        fan.subscribe("conflict", members=["x"], complete=True)
    with pytest.raises(TypeError):
        ZipFanout("not a zipstream")
    with pytest.raises(ValueError):
        ZipFanout(ZipStream(), staging_limit=-1)


def test_unknown_members_are_reported_missing_not_guessed():
    zs = ZipStream()
    zs.add(b"x", "x")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    sub = fan.subscribe("sub", members=["x", "ghost"])
    fan.run()

    zf = _parse(_zip_bytes(sub))
    assert zf.namelist() == ["x"]
    assert sub.missing == {"ghost"}


def test_comment_is_written_into_each_package():
    zs = ZipStream()
    zs.add(b"x", "x")
    fan = ZipFanout(zs, staging_limit=1 << 20, comment="audit-package")
    complete = fan.subscribe("A", complete=True)
    only_x = fan.subscribe("B", members=["x"])
    fan.run()

    assert _parse(_zip_bytes(complete)).comment == b"audit-package"
    assert _parse(_zip_bytes(only_x)).comment == b"audit-package"


def test_forced_zip64_entry_round_trips_with_rebuilt_headers():
    # An unsized iterable forces zip64 extensions in the source local header.
    # Subscribers rebuild that header with their own offset but identical size.
    def unsized():
        yield b"abcdef" * 20

    zs = ZipStream()
    zs.add(unsized(), "big")
    zs.add(b"tail", "tail")

    fan = ZipFanout(zs, staging_limit=1 << 20)
    complete = fan.subscribe("A", complete=True)
    only_big = fan.subscribe("B", members=["big"])
    fan.run_threaded()
    _wait_for(lambda: fan._source_done)

    # Capture the forced-zip64 source header before consumers drain staging.
    with fan._cond:
        retained = fan._entries[0]
        assert retained.head.frame == "header"
        source_header = bytes(retained.head.data)
        # base header (30) + "big" filename (3) + zip64 extra (20)
        assert len(source_header) == 30 + 3 + 20

    full = _parse(_zip_bytes(complete))
    partial = _parse(_zip_bytes(only_big))
    assert full.read("big") == b"abcdef" * 20
    assert full.read("tail") == b"tail"
    assert partial.read("big") == b"abcdef" * 20

    # The rebuilt header keeps the same byte size so all retained data stays
    # aligned regardless of the subscriber's own local offset.
    rebuilt = only_big._rebuild_header(retained, source_header)
    assert len(rebuilt) == len(source_header)


def test_directory_entries_fan_out():
    zs = ZipStream()
    zs.mkdir("d/")
    zs.add(b"file", "d/f")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    complete = fan.subscribe("A", complete=True)
    only_dir = fan.subscribe("B", members=["d/"])
    fan.run()

    full = _parse(_zip_bytes(complete))
    partial = _parse(_zip_bytes(only_dir))
    assert full.getinfo("d/").is_dir()
    assert partial.namelist() == ["d/"]
    assert partial.getinfo("d/").is_dir()


def test_staging_never_exceeds_budget_while_subscribers_are_active():
    zs = ZipStream()
    for i in range(20):
        zs.add(bytes([i]) * 100, "m{:02d}".format(i))

    limit = 256
    fan = ZipFanout(zs, staging_limit=limit)
    # A deliberately slow complete subscriber keeps entries pinned for a while.
    slow = fan.subscribe("slow", complete=True)
    fan.run_threaded()

    seen_max = 0
    chunks = []
    while True:
        try:
            chunks.append(next(slow))
        except StagingWindowExpired:
            break
        except StopIteration:
            break
        with fan._cond:
            seen_max = max(seen_max, fan._retained_total)

    # A single in-flight chunk may be appended atomically; allow that slack.
    assert seen_max <= limit + 64 * 1024
    fan.wait_until_idle(5)


def test_run_on_calling_thread_delivers_without_a_background_thread():
    zs = ZipStream()
    zs.add(b"hello", "x")
    fan = ZipFanout(zs, staging_limit=1 << 20)
    sub = fan.subscribe("sync", complete=True)
    fan.run()  # drives the whole source synchronously
    assert fan._pump_thread is None
    zf = _parse(_zip_bytes(sub))
    assert zf.read("x") == b"hello"
