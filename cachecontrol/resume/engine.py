# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""The resumable cache engine.

The engine orchestrates four operations over a
:class:`~cachecontrol.resume.transport.Transport` and a
:class:`~cachecontrol.resume.store.ResumeStore`:

* **claim**   -- a first GET establishes a generation (a full 200, or its
  first clean 206 segment);
* **backfill** -- further ``Range`` requests fill the gaps of that same
  generation;
* **reopen**  -- a conditional GET (``If-None-Match`` /
  ``If-Modified-Since``) revalidates one concrete stored whole item or
  fragment generation;
* **handover** -- once a whole, receipt-signed generation exists, the
  warehouse receives an immutable view plus an audit of where every byte came
  from.

Correctness rules enforced here:

* Only segments sharing one strong ETag, one total length and one
  Content-Encoding may be joined. Ranges come from ``Content-Range``; a
  half-closed segment never counts as covered.
* ``Vary`` decides which request values distinguish entries. A missing header
  and a present-but-empty header are distinct. A fresh 200 or an *accepted*
  304 may change the generation's Vary declaration.
* A revalidation targets one concrete generation. If a newer generation was
  established while the request was in flight, an old 304 is rejected even
  when the short ETag string happens to recur in another distribution zone.
* With only a date or a weak validator, stored whole items follow the ordinary
  conditional protocol; evidence-less fragments cannot be joined and are
  re-fetched.
* Incompatible overlapping bytes revoke any receipt already signed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from requests.structures import CaseInsensitiveDict

from cachecontrol.resume.http import (
    ByteRange,
    HttpResponse,
    RequestLog,
    is_strong_etag,
    parse_content_length,
    parse_content_range,
    select_vary_dimensions,
    vary_headers,
)
from cachecontrol.resume.store import (
    ByteConflict,
    Generation,
    GenerationIdentity,
    IncompleteGeneration,
    ResumeStore,
    ValidationRecord,
)
from cachecontrol.resume.transport import IncompleteTransfer, Transport


@dataclass
class GenerationView:
    """An immutable handover snapshot.

    A reader keeps working with the generation it opened even after a newer
    generation is published for the same URL.
    """

    generation_id: str
    etag: str | None
    total: int
    content_encoding: str | None
    receipt_digest: str | None
    from_cache: bool
    body: bytes
    vary: dict[str, str | None]

    @property
    def is_signed(self) -> bool:
        return self.receipt_digest is not None


@dataclass
class RangeProvenance:
    start: int
    end: int
    request_seq: int
    digest: str


@dataclass
class AuditReport:
    """Warehouse-facing trace for one stored representation."""

    url: str
    generation_id: str
    etag: str | None
    total: int | None
    complete: bool
    receipt_digest: str | None
    covered: list[RangeProvenance] = field(default_factory=list)
    missing: list[ByteRange] = field(default_factory=list)
    validations: list[ValidationRecord] = field(default_factory=list)
    vary: dict[str, str | None] = field(default_factory=dict)


class _Outcome:
    STORED = "stored"
    INTERRUPTED = "interrupted"
    IDENTITY_MISMATCH = "identity-mismatch"
    REPLACED = "replaced"


class ResumableCache:
    """State machine for resumable, Vary/ETag-correct partial caching."""

    def __init__(self, transport: Transport, store: ResumeStore) -> None:
        self.transport = transport
        self.store = store
        self._seq = 0
        self._lock = threading.RLock()

    # -------------------------------------------------------------- public

    def get(self, url: str, headers: dict[str, str] | None = None) -> GenerationView:
        """Return a complete representation, fetching only what is missing."""
        request_headers = CaseInsensitiveDict(headers or {})
        with self._lock:
            generation = self.store.current_generation(url, request_headers)

            if generation is None:
                generation = self.claim(url, request_headers)

            if not generation.is_complete:
                generation = self.reopen(generation, request_headers)
                if not generation.is_complete:
                    generation = self.backfill(generation, request_headers)

            current = self.store.current_generation(url, request_headers)
            if current is not None and current.is_complete:
                return self.handover(current, from_cache=True)

            if generation is None or not generation.is_complete:
                unresolved = generation.missing() if generation is not None else []
                target_id = generation.id if generation is not None else url
                raise IncompleteGeneration(target_id, unresolved)
            return self.handover(generation, from_cache=False)

    def claim(
        self,
        url: str,
        headers: dict[str, str] | CaseInsensitiveDict[str] | None = None,
        range_header: str | None = None,
    ) -> Generation:
        """First GET for a URL, establishing (or reusing) a generation."""
        request_headers = CaseInsensitiveDict(headers or {})
        outgoing = dict(request_headers)
        if range_header:
            outgoing["Range"] = range_header
        response = self._send("GET", url, outgoing)
        return self._establish(url, request_headers, response)

    def backfill(
        self,
        generation: Generation,
        headers: dict[str, str] | CaseInsensitiveDict[str] | None = None,
    ) -> Generation:
        """Fill every remaining gap of ``generation`` with range requests."""
        request_headers = CaseInsensitiveDict(headers or {})
        for _ in range(self._round_trip_budget(generation)):
            current = self.store.load_generation(generation.id)
            if current is None or current.is_complete:
                return current or generation
            gaps = current.missing()
            if not gaps:
                return current
            outcome, continuation = self._fetch_range(
                current.url, request_headers, current, gaps[0]
            )
            if continuation is not None:
                # A full 200 already opened the replacement generation; keep
                # filling it rather than issuing another request.
                if continuation.is_complete:
                    return continuation
                generation = continuation
                continue
            if outcome == _Outcome.IDENTITY_MISMATCH:
                # The representation moved on without a usable 200 body:
                # a full GET opens the new generation.
                return self.claim(current.url, request_headers)
            # A STORED segment shrinks the gaps; an INTERRUPTED one leaves the
            # same gap for the next pass, and the loop bound prevents a spin.
        return self.store.load_generation(generation.id) or generation

    def reopen(
        self,
        generation: Generation,
        headers: dict[str, str] | CaseInsensitiveDict[str] | None = None,
    ) -> Generation:
        """Conditionally revalidate one stored whole item or fragment."""
        request_headers = CaseInsensitiveDict(headers or {})
        conditional = self._conditional_headers(generation)
        if conditional is None:
            # A fragment with no strong validator has no joinable evidence.
            return self.claim(generation.url, request_headers)

        response = self._send(
            "GET", generation.url, {**dict(request_headers), **conditional}
        )

        if response.is_not_modified:
            return self._apply_not_modified(generation, response, request_headers)
        if response.status == 200:
            return self._establish(generation.url, request_headers, response)
        if response.is_partial:
            self._store_partial(generation, response)
            return self.store.load_generation(generation.id) or generation
        return generation

    def handover(
        self, generation: Generation, from_cache: bool = True
    ) -> GenerationView:
        """Hand the warehouse a signed, immutable view of a whole generation."""
        current = self.store.load_generation(generation.id) or generation
        if not current.is_complete:
            raise IncompleteGeneration(current.id, current.missing())
        body = bytes(current.body)
        return GenerationView(
            generation_id=current.id,
            etag=current.etag,
            total=current.total if current.total is not None else len(body),
            content_encoding=current.content_encoding,
            receipt_digest=current.receipt.body_digest if current.receipt else None,
            from_cache=from_cache,
            body=body,
            vary=dict(current.vary),
        )

    def open_generation(self, generation: Generation) -> GenerationView:
        """Give a reader an immutable view it may retain across republishing."""
        return self.handover(generation, from_cache=True)

    def audit(
        self, url: str, headers: dict[str, str] | None = None
    ) -> list[AuditReport]:
        """Report provenance, effective confirmations and remaining gaps."""
        request_headers = CaseInsensitiveDict(headers or {})
        reports: list[AuditReport] = []
        for generation in self.store.matching_generations(url, request_headers):
            covered = [
                RangeProvenance(
                    segment.start, segment.end, segment.request_seq, segment.digest
                )
                for segment in generation.segments
            ]
            reports.append(
                AuditReport(
                    url=url,
                    generation_id=generation.id,
                    etag=generation.etag,
                    total=generation.total,
                    complete=generation.is_complete,
                    receipt_digest=(
                        generation.receipt.body_digest if generation.receipt else None
                    ),
                    covered=covered,
                    missing=generation.missing(),
                    validations=list(generation.validations),
                    vary=dict(generation.vary),
                )
            )
        return reports

    def request_log(self) -> list[RequestLog]:
        """Every wire request the stand-in observed, in order."""
        return [
            RequestLog(item.method, item.path, dict(item.headers))
            for item in self.transport.log  # type: ignore[attr-defined]
        ]

    # ------------------------------------------------------------ establish

    def _send(
        self, method: str, url: str, headers: dict[str, str] | None
    ) -> HttpResponse:
        self._seq += 1
        return self.transport.send(method, url, headers)

    def _establish(
        self,
        url: str,
        request_headers: CaseInsensitiveDict[str],
        response: HttpResponse,
    ) -> Generation:
        etag = response.etag
        strong = is_strong_etag(etag)
        last_modified = response.last_modified
        declared = vary_headers(response.headers)
        # RFC 9111: a ``Vary: *`` response can never be selected from a cache.
        if "*" in declared:
            raise GenerationIdentity(
                "refusing to cache a response with a wildcard Vary header"
            )
        vary = select_vary_dimensions(declared, request_headers)
        content_encoding = response.headers.get("Content-Encoding")
        content_range = parse_content_range(response.headers)

        if response.is_partial and content_range is not None:
            total = content_range.total
        else:
            total = parse_content_length(response.headers)
            if total is None:
                total = len(response.body)

        generation = self.store.new_generation(
            url,
            etag,
            total,
            content_encoding,
            vary,
            declared,
            self._seq,
            strong=strong,
            last_modified=last_modified,
        )

        if response.is_partial and content_range is not None:
            if content_range.length != len(response.body):
                self._record_interruption(
                    generation,
                    ByteRange(content_range.start, content_range.end, total),
                    None,
                )
                return generation
            self._store_partial(generation, response)
        else:
            generation.install_full(response.body, self._seq)
            self.store.save(generation)
        return self.store.load_generation(generation.id) or generation

    # ------------------------------------------------------------- backfill

    def _round_trip_budget(self, generation: Generation) -> int:
        if generation.total is None:
            return 1
        return max(1, generation.total + 1)

    def _fetch_range(
        self,
        url: str,
        request_headers: CaseInsensitiveDict[str],
        generation: Generation,
        gap: ByteRange,
    ) -> tuple[str, Generation | None]:
        headers = dict(request_headers)
        headers["Range"] = gap.as_request_header()
        try:
            response = self._send("GET", url, headers)
        except IncompleteTransfer as interrupted:
            self._record_interruption(generation, gap, interrupted)
            return _Outcome.INTERRUPTED, None

        if response.status == 200:
            # The origin ignored the range and returned the whole, possibly
            # newer, representation. Establish it directly so no second full
            # GET is wasted.
            replacement = self._establish(url, request_headers, response)
            return _Outcome.REPLACED, replacement

        if not response.is_partial:
            self._record_identity_mismatch(generation, response)
            return _Outcome.IDENTITY_MISMATCH, None

        if not self._identity_holds(generation, response, gap):
            self._record_identity_mismatch(generation, response)
            return _Outcome.IDENTITY_MISMATCH, None

        outcome = self._store_partial(generation, response)
        return outcome, None

    def _identity_holds(
        self, generation: Generation, response: HttpResponse, gap: ByteRange
    ) -> bool:
        if not is_strong_etag(response.etag) or response.etag != generation.etag:
            return False
        content_range = parse_content_range(response.headers)
        if content_range is None:
            return False
        if content_range.total != generation.total:
            return False
        if response.headers.get("Content-Encoding") != generation.content_encoding:
            return False
        if content_range.start != gap.start or content_range.end != gap.end:
            return False
        if content_range.length != len(response.body):
            return False
        return True

    def _store_partial(
        self, generation: Generation, response: HttpResponse
    ) -> str:
        content_range = parse_content_range(response.headers)
        if content_range is None:
            return _Outcome.INTERRUPTED
        target = self.store.load_generation(generation.id) or generation
        try:
            target.append(content_range.start, response.body, self._seq)
        except ByteConflict:
            # The store already withdrew the signed receipt; force a refetch.
            self.store.save(target)
            return _Outcome.IDENTITY_MISMATCH
        self.store.save(target)
        return _Outcome.STORED

    # -------------------------------------------------------- revalidation

    def _conditional_headers(
        self, generation: Generation
    ) -> dict[str, str] | None:
        if is_strong_etag(generation.etag):
            return {"If-None-Match": generation.etag}  # type: ignore[dict-item]
        # An incomplete fragment may only be joined/revalidated through a
        # strong validator; with a weak tag or no tag its bytes are not
        # joinable evidence and must be re-fetched from scratch.
        if not generation.is_complete:
            return None
        # A whole item with a weak tag or only a date follows the ordinary
        # conditional revalidation protocol.
        if generation.etag:
            return {"If-None-Match": generation.etag}
        if generation.last_modified:
            return {"If-Modified-Since": generation.last_modified}
        return None

    def _apply_not_modified(
        self,
        generation: Generation,
        response: HttpResponse,
        request_headers: CaseInsensitiveDict[str],
    ) -> Generation:
        target = self.store.load_generation(generation.id) or generation
        current_id = self.store.current_id_for(target)
        accepted = current_id == target.id
        reason = "accepted: still the current generation"

        if accepted and response.etag and response.etag != target.etag:
            accepted = False
            reason = "rejected: 304 ETag names another representation"
        elif not accepted:
            reason = "rejected: a newer generation superseded this target"

        record = ValidationRecord(
            request_seq=self._seq,
            target_generation_id=target.id,
            if_none_match=(
                target.etag if target.etag else None
            ),
            if_modified_since=target.last_modified,
            status=304,
            applied=accepted,
            reason=reason,
        )
        target.validations.append(record)
        if accepted:
            self._apply_vary_update(target, response, request_headers)
        self.store.save(target)
        return target

    def _apply_vary_update(
        self,
        generation: Generation,
        response: HttpResponse,
        request_headers: CaseInsensitiveDict[str],
    ) -> None:
        declared = vary_headers(response.headers)
        if declared:
            # Only the declared header names change on an accepted 304. The
            # Vary selection that defines this generation's identity stays
            # fixed; a genuinely new selection arrives on a fresh 200 and
            # establishes its own generation.
            generation.vary_declared = declared

    # --------------------------------------------------------------- ledger

    def _record_interruption(
        self,
        generation: Generation,
        gap: ByteRange,
        interrupted: IncompleteTransfer | None,
    ) -> None:
        target = self.store.load_generation(generation.id) or generation
        partial_len = len(interrupted.partial) if interrupted else 0
        # The trailing, half-closed bytes are deliberately not stored.
        target.validations.append(
            ValidationRecord(
                request_seq=self._seq,
                target_generation_id=target.id,
                if_none_match=None,
                if_modified_since=None,
                status=0,
                applied=False,
                reason=(
                    f"interrupted transfer for {gap.start}-{gap.end}; "
                    f"{partial_len} unterminated bytes discarded"
                ),
            )
        )
        self.store.save(target)

    def _record_identity_mismatch(
        self, generation: Generation, response: HttpResponse
    ) -> None:
        target = self.store.load_generation(generation.id) or generation
        target.validations.append(
            ValidationRecord(
                request_seq=self._seq,
                target_generation_id=target.id,
                if_none_match=target.etag,
                if_modified_since=None,
                status=response.status,
                applied=False,
                reason="rejected: ETag, length or encoding no longer matches",
            )
        )
        self.store.save(target)
