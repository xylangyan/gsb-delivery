# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for resumable, range-aware partial caching.

Everything here runs against :class:`InProcessTransport`, an in-memory
stand-in: no socket is bound. The stand-in can interleave two distribution
zones, two ``Vary`` declarations, delayed (possibly stale) validators and
interrupted response bodies.
"""

from __future__ import annotations

import io
from threading import Lock

import pytest

from cachecontrol.cache import DictCache, SeparateBodyBaseCache
from cachecontrol.resume import (
    AuditReport,
    ByteConflict,
    GenerationIdentity,
    InProcessTransport,
    IncompleteGeneration,
    IncompleteTransfer,
    Interrupt,
    ResumableCache,
    ResumeStore,
)
from cachecontrol.resume.http import (
    ByteRange,
    is_strong_etag,
    merge_ranges,
    missing_ranges,
    parse_content_range,
    select_vary_dimensions,
    vary_matches,
)
from requests.structures import CaseInsensitiveDict


# --------------------------------------------------------------------------- #
# Local doubles
# --------------------------------------------------------------------------- #


class ThreadFileLock:
    """A filelock-free lock so SeparateBodyFileCache works without filelock."""

    _guard = Lock()
    _locks: dict[str, Lock] = {}

    def __init__(self, path: str) -> None:
        with self._guard:
            self._lock = self._locks.setdefault(path, Lock())

    def __enter__(self) -> "ThreadFileLock":
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


class MemorySeparateBodyCache(SeparateBodyBaseCache):
    """In-memory implementation of the header/body-splitting cache contract."""

    def __init__(self) -> None:
        self.meta: dict[str, bytes] = {}
        self.bodies: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.meta.get(key)

    def set(self, key: str, value: bytes, expires=None) -> None:
        self.meta[key] = value

    def delete(self, key: str) -> None:
        self.meta.pop(key, None)
        self.bodies.pop(key, None)

    def set_body(self, key: str, body: bytes) -> None:
        self.bodies[key] = bytes(body)

    def get_body(self, key: str):
        return io.BytesIO(self.bodies[key]) if key in self.bodies else None


class Representation:
    def __init__(
        self,
        body: bytes,
        etag: str | None = '"e"',
        last_modified: str | None = None,
        content_encoding: str | None = None,
    ) -> None:
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.content_encoding = content_encoding


class StandinResource:
    """An in-process origin supporting zones, ranges, 304s and interruptions."""

    def __init__(self, vary: tuple[str, ...] = ("X-Zone",)) -> None:
        self.vary = list(vary)
        self.reps: dict[tuple[object, ...], Representation] = {}
        # (selection key, start, end) -> partial bytes delivered before reset
        self.interrupt_once: set[tuple[object, ...]] = set()
        # When True, any If-None-Match earns a 304 echoing that (possibly
        # stale) tag, modelling a confirmation that arrives late.
        self.echo_conditional = False

    def add(self, selection: tuple[object, ...], rep: Representation) -> None:
        self.reps[selection] = rep

    def _selection(self, request) -> tuple[object, ...]:
        return tuple(request.header(name) for name in self.vary)

    def _base_headers(self, rep: Representation) -> dict[str, str]:
        headers = {
            "Vary": ", ".join(self.vary),
            "Accept-Ranges": "bytes",
        }
        if rep.etag is not None:
            headers["ETag"] = rep.etag
        if rep.last_modified is not None:
            headers["Last-Modified"] = rep.last_modified
        if rep.content_encoding is not None:
            headers["Content-Encoding"] = rep.content_encoding
        return headers

    def __call__(self, request):
        from cachecontrol.resume.http import HttpResponse

        selection = self._selection(request)
        rep = self.reps.get(selection)
        if rep is None:
            return HttpResponse(404, {}, b"")

        if_none_match = request.header("If-None-Match")
        if_modified_since = request.header("If-Modified-Since")
        if if_none_match is not None:
            if self.echo_conditional or if_none_match == rep.etag:
                return HttpResponse(304, self._base_headers(rep))
        if if_modified_since is not None and rep.etag is None:
            return HttpResponse(304, self._base_headers(rep))

        range_header = request.header("Range")
        if range_header:
            start_text, end_text = range_header.removeprefix("bytes=").split("-")
            start, end = int(start_text), int(end_text)
            interrupt_key = (selection, start, end)
            if interrupt_key in self.interrupt_once:
                self.interrupt_once.discard(interrupt_key)
                raise Interrupt(
                    "connection reset mid-segment", partial=rep.body[start : start + 2]
                )
            data = rep.body[start : end + 1]
            headers = self._base_headers(rep)
            headers["Content-Range"] = (
                f"bytes {start}-{end}/{len(rep.body)}"
            )
            headers["Content-Length"] = str(len(data))
            return HttpResponse(206, headers, data)

        headers = self._base_headers(rep)
        headers["Content-Length"] = str(len(rep.body))
        return HttpResponse(200, headers, rep.body)


@pytest.fixture()
def transport():
    return InProcessTransport()


@pytest.fixture()
def cache(transport):
    return ResumableCache(transport, ResumeStore(DictCache()))


# --------------------------------------------------------------------------- #
# Pure header/range helpers
# --------------------------------------------------------------------------- #


class TestRangeMath:
    def test_parse_content_range(self):
        headers = CaseInsensitiveDict({"Content-Range": "bytes 10-19/40"})
        parsed = parse_content_range(headers)
        assert parsed == ByteRange(10, 19, 40)
        assert parsed.length == 10

    def test_parse_content_range_rejects_other_units_and_garbage(self):
        other_unit = CaseInsensitiveDict({"Content-Range": "items 0-1/2"})
        assert parse_content_range(other_unit) is None
        garbage = CaseInsensitiveDict({"Content-Range": "nonsense"})
        assert parse_content_range(garbage) is None
        assert parse_content_range(CaseInsensitiveDict()) is None

    def test_missing_ranges_and_merge(self):
        covered = [ByteRange(0, 4, 12), ByteRange(8, 11, 12)]
        assert missing_ranges(covered, 12) == [ByteRange(5, 7, 12)]
        merged = merge_ranges([ByteRange(0, 5, 12), ByteRange(3, 9, 12)])
        assert merged == [ByteRange(0, 9, 12)]

    def test_strong_etag_detection(self):
        assert is_strong_etag('"abc"')
        assert not is_strong_etag('W/"abc"')
        assert not is_strong_etag(None)

    def test_missing_and_empty_vary_values_are_distinct(self):
        declared = ("X-Zone",)
        missing = select_vary_dimensions(declared, CaseInsensitiveDict({}))
        empty = select_vary_dimensions(
            declared, CaseInsensitiveDict({"X-Zone": ""})
        )
        assert missing == {"X-Zone": None}
        assert empty == {"X-Zone": ""}
        assert not vary_matches(missing, CaseInsensitiveDict({"X-Zone": ""}))
        assert vary_matches(empty, CaseInsensitiveDict({"X-Zone": ""}))


# --------------------------------------------------------------------------- #
# Claim + backfill + handover
# --------------------------------------------------------------------------- #


class TestClaimBackfillHandover:
    def _resource(self, body: bytes = bytes(range(12))) -> StandinResource:
        resource = StandinResource()
        resource.add(("north",), Representation(body, '"n1"'))
        return resource

    def test_claim_segment_then_backfill_to_signed_whole(self, transport, cache):
        resource = self._resource()
        transport.handle("/report", resource)
        generation = cache.claim(
            "/report", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        assert not generation.is_complete
        assert [(s.start, s.end) for s in generation.segments] == [(0, 4)]

        generation = cache.backfill(generation, {"X-Zone": "north"})
        assert generation.is_complete
        view = cache.handover(generation)
        assert view.is_signed
        assert view.body == bytes(range(12))
        assert view.generation_id == generation.id

    def test_interrupted_segment_is_not_covered_then_retry_completes(
        self, transport, cache
    ):
        body = bytes(range(12))
        resource = self._resource(body)
        resource.interrupt_once.add((("north",), 5, 11))
        transport.handle("/report", resource)

        generation = cache.claim(
            "/report", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        generation = cache.backfill(generation, {"X-Zone": "north"})

        assert generation.is_complete
        assert bytes(generation.body) == body
        interruptions = [v for v in generation.validations if v.status == 0]
        assert len(interruptions) == 1
        assert "5-11" in interruptions[0].reason
        assert "unterminated bytes discarded" in interruptions[0].reason

    def test_get_returns_cached_view_without_new_wire_request(
        self, transport, cache
    ):
        resource = self._resource()
        transport.handle("/report", resource)
        first = cache.get("/report", {"X-Zone": "north"})
        requests_after_first = len(transport.log)
        second = cache.get("/report", {"X-Zone": "north"})
        assert second.from_cache
        assert second.generation_id == first.generation_id
        assert len(transport.log) == requests_after_first

    def test_audit_traces_provenance_and_remaining_gaps(self, transport, cache):
        resource = self._resource()
        resource.interrupt_once.add((("north",), 5, 11))
        transport.handle("/report", resource)

        generation = cache.claim(
            "/report", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        partial_report = cache.audit("/report", {"X-Zone": "north"})[0]
        assert partial_report.complete is False
        assert [(c.start, c.end) for c in partial_report.covered] == [(0, 4)]
        assert [(m.start, m.end) for m in partial_report.missing] == [(5, 11)]

        cache.backfill(generation, {"X-Zone": "north"})
        report = cache.audit("/report", {"X-Zone": "north"})[0]
        assert isinstance(report, AuditReport)
        assert report.complete
        assert report.receipt_digest
        # Each covered range records the request sequence that produced it.
        assert [c.request_seq for c in report.covered] == sorted(
            c.request_seq for c in report.covered
        )

    def test_handover_of_incomplete_generation_raises(self, transport, cache):
        resource = self._resource()
        transport.handle("/report", resource)
        generation = cache.claim(
            "/report", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        with pytest.raises(IncompleteGeneration) as exc:
            cache.handover(generation)
        assert exc.value.missing == [ByteRange(5, 11, 12)]


# --------------------------------------------------------------------------- #
# Vary: two zones, two declarations, missing vs empty
# --------------------------------------------------------------------------- #


class TestVary:
    def test_two_zones_get_distinct_generations(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(b"N" * 12, '"n1"'))
        resource.add(("south",), Representation(b"S" * 12, '"s1"'))
        transport.handle("/report", resource)

        north = cache.get("/report", {"X-Zone": "north"})
        south = cache.get("/report", {"X-Zone": "south"})
        assert north.generation_id != south.generation_id
        assert north.body == b"N" * 12
        assert south.body == b"S" * 12

        # Each zone reuses its own stored bytes.
        north_again = cache.get("/report", {"X-Zone": "north"})
        south_again = cache.get("/report", {"X-Zone": "south"})
        assert north_again.generation_id == north.generation_id
        assert south_again.generation_id == south.generation_id

    def test_missing_header_distinct_from_empty_value(self, transport, cache):
        resource = StandinResource()
        resource.add((None,), Representation(b"missing", '"m"'))
        resource.add(("",), Representation(b"empty", '"e"'))
        transport.handle("/report", resource)

        missing = cache.get("/report", {})
        empty = cache.get("/report", {"X-Zone": ""})
        assert missing.generation_id != empty.generation_id
        assert missing.body == b"missing"
        assert empty.body == b"empty"

    def test_two_vary_dimensions_must_both_match(self, transport, cache):
        resource = StandinResource(vary=("X-Zone", "X-Format"))
        resource.add(
            ("north", "json"), Representation(b"north-json", '"nj"')
        )
        resource.add(
            ("north", "csv"), Representation(b"north-csv", '"nc"')
        )
        transport.handle("/report", resource)

        json_view = cache.get("/report", {"X-Zone": "north", "X-Format": "json"})
        csv_view = cache.get("/report", {"X-Zone": "north", "X-Format": "csv"})
        assert json_view.generation_id != csv_view.generation_id
        assert json_view.body == b"north-json"
        assert csv_view.body == b"north-csv"

    def test_accepted_304_can_change_vary_declaration(self, transport, cache):
        from cachecontrol.resume.http import HttpResponse

        resource = StandinResource(vary=("X-Zone",))
        resource.add(("north",), Representation(b"hello", '"v1"'))

        def handler(request):
            if request.header("If-None-Match") == '"v1"':
                # The origin widens its Vary on a still-valid 304.
                return HttpResponse(
                    304, {"ETag": '"v1"', "Vary": "X-Zone, X-Format"}
                )
            return resource(request)

        transport.handle("/report", handler)
        generation = cache.claim("/report", {"X-Zone": "north"})
        assert generation.vary_declared == ["X-Zone"]

        reopened = cache.reopen(generation, {"X-Zone": "north"})
        assert reopened.validations[-1].applied
        assert reopened.vary_declared == ["X-Zone", "X-Format"]

    def test_wildcard_vary_is_never_cached(self, transport, cache):
        from cachecontrol.resume.http import HttpResponse

        def handler(request):
            return HttpResponse(
                200, {"Vary": "*", "ETag": '"e"', "Content-Length": "3"}, b"abc"
            )

        transport.handle("/report", handler)
        with pytest.raises(GenerationIdentity):
            cache.claim("/report", {"X-Zone": "north"})
        assert cache.audit("/report", {"X-Zone": "north"}) == []

    def test_fresh_200_changes_vary_declaration(self, transport, cache):
        resource = StandinResource(vary=("X-Zone",))
        resource.add(("north",), Representation(b"one", '"v1"'))
        transport.handle("/report", resource)
        cache.claim("/report", {"X-Zone": "north"})

        resource.vary = ["X-Zone", "X-Format"]
        resource.add(("north", "json"), Representation(b"two", '"v2"'))
        new_generation = cache.claim(
            "/report", {"X-Zone": "north", "X-Format": "json"}
        )
        assert new_generation.vary_declared == ["X-Zone", "X-Format"]
        assert new_generation.etag == '"v2"'


# --------------------------------------------------------------------------- #
# Revalidation and generation guarding
# --------------------------------------------------------------------------- #


class TestRevalidation:
    def _two_generations(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(10)), '"v1"'))
        transport.handle("/report", resource)
        old = cache.claim(
            "/report", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        # A new representation is published while nothing is in flight.
        resource.add(("north",), Representation(bytes(range(10, 20)), '"v2"'))
        current = cache.claim("/report", {"X-Zone": "north"})
        return resource, old, current

    def test_stale_304_for_superseded_generation_is_rejected(
        self, transport, cache
    ):
        resource, old, current = self._two_generations(transport, cache)
        resource.echo_conditional = True  # late 304 echoing the old tag

        reopened = cache.reopen(old, {"X-Zone": "north"})
        record = reopened.validations[-1]
        assert record.applied is False
        assert record.target_generation_id == old.id
        assert "superseded" in record.reason
        # The old generation's fragment is untouched and still incomplete.
        assert reopened.id == old.id
        assert not reopened.is_complete

    def test_current_generation_304_is_accepted(self, transport, cache):
        resource, _old, current = self._two_generations(transport, cache)
        resource.echo_conditional = True
        reopened = cache.reopen(current, {"X-Zone": "north"})
        assert reopened.validations[-1].applied

    def test_304_naming_another_etag_is_rejected(self, transport, cache):
        from cachecontrol.resume.http import HttpResponse

        resource = StandinResource()
        resource.add(("north",), Representation(b"abc", '"v1"'))
        transport.handle("/report", resource)
        generation = cache.claim("/report", {"X-Zone": "north"})

        # A late 304 that names a different ETag than the targeted generation.
        def mismatched_304(request):
            return HttpResponse(304, {"ETag": '"v9"', "Vary": "X-Zone"})

        transport.handle("/report", mismatched_304)
        reopened = cache.reopen(generation, {"X-Zone": "north"})
        record = reopened.validations[-1]
        assert record.status == 304
        assert record.applied is False
        assert "another representation" in record.reason

    def test_identical_short_tag_in_other_zone_is_not_cross_applied(
        self, transport, cache
    ):
        resource = StandinResource()
        # Same literal ETag string, different distribution zone/representation.
        resource.add(("north",), Representation(b"north-body", '"same"'))
        resource.add(("south",), Representation(b"south-body", '"same"'))
        transport.handle("/report", resource)

        north = cache.claim("/report", {"X-Zone": "north"})
        south = cache.claim("/report", {"X-Zone": "south"})
        assert north.id != south.id

        resource.echo_conditional = True
        north_reopened = cache.reopen(north, {"X-Zone": "north"})
        south_reopened = cache.reopen(south, {"X-Zone": "south"})
        assert north_reopened.validations[-1].target_generation_id == north.id
        assert south_reopened.validations[-1].target_generation_id == south.id
        assert north_reopened.id != south_reopened.id

    def test_revalidation_request_targets_the_right_generation(
        self, transport, cache
    ):
        resource, old, _current = self._two_generations(transport, cache)
        cache.reopen(old, {"X-Zone": "north"})
        last_request = transport.log[-1]
        assert last_request.header("If-None-Match") == '"v1"'
        assert last_request.header("Range") is None


# --------------------------------------------------------------------------- #
# Weak / date validators
# --------------------------------------------------------------------------- #


class TestWeakAndDateValidators:
    def test_weak_fragment_has_no_validator_and_is_refetched(
        self, transport, cache
    ):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(8)), 'W/"w"'))
        transport.handle("/r", resource)

        fragment = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-3"
        )
        assert cache._conditional_headers(fragment) is None

        whole = cache.reopen(fragment, {"X-Zone": "north"})
        assert whole.is_complete
        assert [(s.start, s.end) for s in whole.segments] == [(0, 7)]

    def test_weak_whole_item_uses_if_none_match(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(b"abcdefgh", 'W/"w"'))
        transport.handle("/r", resource)
        whole = cache.claim("/r", {"X-Zone": "north"})
        reopened = cache.reopen(whole, {"X-Zone": "north"})
        record = reopened.validations[-1]
        assert record.applied
        assert record.if_none_match == 'W/"w"'

    def test_date_only_whole_item_uses_if_modified_since(self, transport, cache):
        resource = StandinResource()
        resource.add(
            ("north",),
            Representation(
                b"hello", etag=None, last_modified="Mon, 01 Jan 2024 00:00:00 GMT"
            ),
        )
        transport.handle("/r", resource)
        whole = cache.claim("/r", {"X-Zone": "north"})
        reopened = cache.reopen(whole, {"X-Zone": "north"})
        record = reopened.validations[-1]
        assert record.applied
        assert record.if_modified_since == "Mon, 01 Jan 2024 00:00:00 GMT"
        assert cache.handover(reopened).is_signed


# --------------------------------------------------------------------------- #
# Byte conflicts
# --------------------------------------------------------------------------- #


class TestByteConflict:
    def test_incompatible_overlap_revokes_receipt(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(20)), '"e"'))
        transport.handle("/x", resource)
        generation = cache.claim(
            "/x", {"X-Zone": "north"}, range_header="bytes=0-9"
        )
        generation = cache.backfill(generation, {"X-Zone": "north"})
        assert generation.is_complete

        target = cache.store.load_generation(generation.id)
        target.body[0:10] = b"Z" * 10  # tamper the previously accepted bytes
        with pytest.raises(ByteConflict):
            target.append(0, bytes(range(20))[0:5], request_seq=99)
        assert target.receipt is None

    def test_compatible_overlap_is_idempotent(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(20)), '"e"'))
        transport.handle("/x", resource)
        generation = cache.claim(
            "/x", {"X-Zone": "north"}, range_header="bytes=0-9"
        )
        target = cache.store.load_generation(generation.id)
        target.append(5, bytes(range(20))[5:10], request_seq=2)
        assert generation.receipt is None  # still incomplete, but no conflict


# --------------------------------------------------------------------------- #
# Identity guards on join
# --------------------------------------------------------------------------- #


class TestIdentityGuards:
    def test_segment_with_different_etag_is_not_joined(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)
        generation = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-4"
        )

        # Origin changes representation: range responses now carry v2.
        resource.add(("north",), Representation(bytes(range(100, 112)), '"v2"'))
        result = cache.backfill(generation, {"X-Zone": "north"})
        # A full GET established the new generation rather than joining bytes.
        assert result.etag == '"v2"'
        assert result.id != generation.id

        stored_old = cache.store.load_generation(generation.id)
        mismatch = [
            v for v in stored_old.validations if "no longer matches" in v.reason
        ]
        assert mismatch

    def test_segment_with_different_length_is_rejected(self, transport):
        store = ResumeStore(DictCache())
        cache = ResumableCache(transport, store)
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)
        generation = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        # Shrink the representation so Content-Range totals disagree.
        resource.add(("north",), Representation(b"short", '"v1"'))
        cache.backfill(generation, {"X-Zone": "north"})
        stored = cache.store.load_generation(generation.id)
        assert any(
            "no longer matches" in v.reason for v in stored.validations
        )


# --------------------------------------------------------------------------- #
# Reader pinning across republishing
# --------------------------------------------------------------------------- #


class TestReaderPinning:
    def test_old_reader_keeps_its_generation(self, transport, cache):
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)

        old_generation = cache.get("/r", {"X-Zone": "north"})
        reader_view = cache.open_generation(
            cache.store.load_generation(old_generation.generation_id)
        )

        resource.add(("north",), Representation(bytes(range(12, 24)), '"v2"'))
        new_generation = cache.claim("/r", {"X-Zone": "north"})
        assert new_generation.id != reader_view.generation_id

        # The reader's view is an immutable snapshot of the old bytes.
        assert reader_view.body == bytes(range(12))
        assert bytes(new_generation.body) == bytes(range(12, 24))


# --------------------------------------------------------------------------- #
# Persistence and crash recovery
# --------------------------------------------------------------------------- #


class TestPersistence:
    def test_state_survives_a_new_store_instance(self, transport):
        backend = DictCache()
        first = ResumableCache(transport, ResumeStore(backend))
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)
        generation = first.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-4"
        )

        second = ResumableCache(transport, ResumeStore(backend))
        reloaded = second.store.current_generation(
            "/r", CaseInsensitiveDict({"X-Zone": "north"})
        )
        assert reloaded is not None
        assert reloaded.id == generation.id
        completed = second.backfill(reloaded, {"X-Zone": "north"})
        assert completed.is_complete

    def test_separate_body_cache_heals_lost_body_file(self):
        backend = MemorySeparateBodyCache()
        transport = InProcessTransport()
        cache = ResumableCache(transport, ResumeStore(backend))
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(10)), '"v1"'))
        transport.handle("/r", resource)
        generation = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-4"
        )

        # Simulate a crash after the manifest landed but the body file was lost.
        body_key = f"cc-resume:body::{generation.id}"
        del backend.bodies[body_key]

        reloaded = cache.store.load_generation(generation.id)
        assert reloaded is not None
        assert reloaded.segments == []
        assert not reloaded.is_complete
        assert reloaded.receipt is None

        healed = cache.backfill(reloaded, {"X-Zone": "north"})
        assert healed.is_complete
        assert bytes(healed.body) == bytes(range(10))

    def test_separate_body_cache_heals_truncated_body_file(self):
        backend = MemorySeparateBodyCache()
        transport = InProcessTransport()
        cache = ResumableCache(transport, ResumeStore(backend))
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)

        generation = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-3"
        )
        generation = cache.backfill(generation, {"X-Zone": "north"})
        assert generation.is_complete

        # Crash after the manifest landed while the body file was truncated:
        # only the first four bytes survived on disk.
        body_key = f"cc-resume:body::{generation.id}"
        backend.bodies[body_key] = backend.bodies[body_key][:4]

        reloaded = cache.store.load_generation(generation.id)
        assert reloaded is not None
        # No false sign-off: the warehouse sees an explicit missing state.
        assert not reloaded.is_complete
        assert reloaded.receipt is None
        surviving = [(s.start, s.end) for s in reloaded.segments]
        assert surviving == [(0, 3)]
        assert [(m.start, m.end) for m in reloaded.missing()] == [(4, 11)]

        healed = cache.backfill(reloaded, {"X-Zone": "north"})
        assert healed.is_complete
        assert bytes(healed.body) == bytes(range(12))
        assert cache.handover(healed).is_signed

    def test_separate_body_file_cache_roundtrip(self, tmp_path):
        from cachecontrol.caches import SeparateBodyFileCache

        backend = SeparateBodyFileCache(
            str(tmp_path), lock_class=ThreadFileLock
        )
        transport = InProcessTransport()
        cache = ResumableCache(transport, ResumeStore(backend))
        resource = StandinResource()
        resource.add(("north",), Representation(bytes(range(12)), '"v1"'))
        transport.handle("/r", resource)

        generation = cache.claim(
            "/r", {"X-Zone": "north"}, range_header="bytes=0-4"
        )
        generation = cache.backfill(generation, {"X-Zone": "north"})
        view = cache.handover(generation)
        assert view.body == bytes(range(12))

        # Reopen against the on-disk cache in a brand-new store instance.
        reopened_cache = ResumableCache(
            InProcessTransport(), ResumeStore(backend)
        )
        loaded = reopened_cache.store.current_generation(
            "/r", CaseInsensitiveDict({"X-Zone": "north"})
        )
        assert loaded is not None
        assert loaded.is_complete
        assert bytes(loaded.body) == bytes(range(12))


# --------------------------------------------------------------------------- #
# Full lifecycle: claim, backfill, reopen, handover, warehouse audit
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_claim_backfill_reopen_handover_for_two_zones(self, transport, cache):
        north_body = bytes(range(16))
        south_body = b"Z" * 16
        resource = StandinResource(vary=("X-Zone", "X-Format"))
        resource.add(("north", "csv"), Representation(north_body, '"n1"',
                                                       content_encoding="identity"))
        resource.add(("south", "csv"), Representation(south_body, '"s1"',
                                                       content_encoding="identity"))
        # The first north backfill segment is interrupted once (gap 4-15).
        resource.interrupt_once.add((("north", "csv"), 4, 15))
        transport.handle("/daily", resource)

        north_headers = {"X-Zone": "north", "X-Format": "csv"}
        south_headers = {"X-Zone": "south", "X-Format": "csv"}

        # 1) claim: north delivers only its first segment.
        north = cache.claim(
            "/daily", north_headers, range_header="bytes=0-3"
        )
        assert not north.is_complete

        # 2) backfill: an interrupted first attempt is retried and completed.
        north = cache.backfill(north, north_headers)
        assert north.is_complete
        north_view = cache.handover(north)
        assert north_view.body == north_body

        # 3) the second zone claims and completes independently.
        south = cache.get("/daily", south_headers)
        assert south.body == south_body
        assert south.generation_id != north_view.generation_id

        # 4) reopen: a still-valid 304 reconfirms the stored north generation.
        reopened_north = cache.reopen(
            cache.store.load_generation(north_view.generation_id), north_headers
        )
        confirmation = reopened_north.validations[-1]
        assert confirmation.applied and confirmation.status == 304
        assert confirmation.if_none_match == '"n1"'

        # 5) warehouse audit answers: provenance per range, the effective
        #    confirmation and the minimum ranges still required (none here).
        report = cache.audit("/daily", north_headers)[0]
        assert report.complete
        assert report.receipt_digest == north_view.receipt_digest
        assert report.missing == []
        # Every covered interval traces back to the request that fetched it.
        assert all(item.request_seq > 0 for item in report.covered)
        assert any(
            record.applied and record.status == 304
            for record in report.validations
        )

        # Each range request on the wire carried the zone and format.
        for wire in transport.log:
            if wire.path == "/daily":
                assert wire.header("X-Zone") in {"north", "south"}
                assert wire.header("X-Format") == "csv"

        # The request ledger reconstructs the same round trips in order.
        logged = cache.request_log()
        assert [entry.path for entry in logged] == [
            wire.path for wire in transport.log
        ]
        assert any(entry.headers.get("Range") for entry in logged)


# --------------------------------------------------------------------------- #
# Transport error mapping
# --------------------------------------------------------------------------- #


class TestTransportErrors:
    def test_interrupt_becomes_incomplete_transfer(self, transport):
        def handler(request):
            raise Interrupt("reset", partial=b"ab")

        transport.handle("/boom", handler)
        with pytest.raises(IncompleteTransfer) as exc:
            transport.send("GET", "/boom")
        assert exc.value.partial == b"ab"


class TestRequestsTransportBridge:
    def test_maps_a_session_response(self):
        from cachecontrol.resume.transport import RequestsTransport

        class FakeResponse:
            status_code = 206
            headers = {"Content-Range": "bytes 0-3/8", "ETag": '"e"'}
            content = b"0123"

        class FakeSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, headers=None, stream=False):
                self.calls.append((method, url, dict(headers or {}), stream))
                return FakeResponse()

            def close(self):
                self.closed = True

        session = FakeSession()
        bridge = RequestsTransport(session)
        response = bridge.send(
            "GET", "http://local/r", {"Range": "bytes=0-3"}
        )
        assert response.status == 206
        assert response.body == b"0123"
        assert response.headers["Content-Range"] == "bytes 0-3/8"
        method, url, headers, stream = session.calls[0]
        assert (method, url, headers["Range"], stream) == (
            "GET",
            "http://local/r",
            "bytes=0-3",
            True,
        )
        bridge.close()
        assert session.closed is True
