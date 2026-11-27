# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""Persistent model for resumable partial downloads.

A single normalized URL may carry several *generations*. A generation is one
specific representation, identified by the tuple

    (strong ETag, total length, Content-Encoding, Vary selection)

and keyed by a stable digest of that tuple. Bytes are only ever shared between
segments of the *same* generation. Old generations are retained so that a
reader already holding one keeps working after a newer generation is published.

The store is backend agnostic: it works over any
:class:`~cachecontrol.cache.BaseCache`. With a
:class:`~cachecontrol.cache.SeparateBodyBaseCache` the manifest (headers,
segment map, receipt) and the body file are landed separately, exactly the way
the warehouse allows header/body split writes. Every load reconciles the
manifest against the bytes actually present, so an interrupted write can never
be mistaken for a complete generation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import msgpack
from requests.structures import CaseInsensitiveDict

from cachecontrol.cache import BaseCache, SeparateBodyBaseCache
from cachecontrol.resume.http import (
    ByteRange,
    is_strong_etag,
    merge_ranges,
    missing_ranges,
    vary_matches,
)

# Namespace prefix keeps resumable keys distinct from classic CacheControl
# entries (which are keyed by the bare normalized URL).
_NS = "cc-resume:"
_INDEX_KEY = _NS + "index"


class ByteConflict(RuntimeError):
    """Raised when overlapping bytes disagree for the same generation."""


class GenerationIdentity(RuntimeError):
    """Raised when a response cannot belong to the targeted generation."""


class IncompleteGeneration(RuntimeError):
    """Raised on handover while byte ranges are still missing.

    The message carries the ranges that remain to be fetched, so callers can
    present an explicit "incomplete" state rather than a false sign-off.
    """

    def __init__(self, generation_id: str, missing: list["ByteRange"]) -> None:
        self.generation_id = generation_id
        self.missing = missing
        super().__init__(
            f"generation {generation_id} is missing ranges "
            f"{[(r.start, r.end) for r in missing]}"
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generation_id(
    etag: str | None,
    total: int | None,
    content_encoding: str | None,
    vary: dict[str, str | None],
    last_modified: str | None = None,
) -> str:
    material = _canonical(
        {
            "etag": etag,
            "last_modified": last_modified,
            "total": total,
            "content_encoding": content_encoding,
            "vary": vary,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class Segment:
    """One cleanly terminated 206 segment held by a generation."""

    start: int
    end: int
    request_seq: int
    digest: str

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def range(self, total: int | None) -> ByteRange:
        return ByteRange(self.start, self.end, total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "request_seq": self.request_seq,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        return cls(
            start=data["start"],
            end=data["end"],
            request_seq=data["request_seq"],
            digest=data["digest"],
        )


@dataclass
class ValidationRecord:
    """A ledger entry for one conditional revalidation round trip."""

    request_seq: int
    target_generation_id: str
    if_none_match: str | None
    if_modified_since: str | None
    status: int
    applied: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_seq": self.request_seq,
            "target_generation_id": self.target_generation_id,
            "if_none_match": self.if_none_match,
            "if_modified_since": self.if_modified_since,
            "status": self.status,
            "applied": self.applied,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationRecord:
        return cls(
            request_seq=data["request_seq"],
            target_generation_id=data["target_generation_id"],
            if_none_match=data.get("if_none_match"),
            if_modified_since=data.get("if_modified_since"),
            status=data["status"],
            applied=data["applied"],
            reason=data["reason"],
        )


@dataclass
class Receipt:
    """A warehouse-receivable sign-off for a genuinely complete generation."""

    generation_id: str
    etag: str | None
    total: int
    content_encoding: str | None
    body_digest: str
    request_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "etag": self.etag,
            "total": self.total,
            "content_encoding": self.content_encoding,
            "body_digest": self.body_digest,
            "request_seq": self.request_seq,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Receipt:
        return cls(
            generation_id=data["generation_id"],
            etag=data["etag"],
            total=data["total"],
            content_encoding=data.get("content_encoding"),
            body_digest=data["body_digest"],
            request_seq=data["request_seq"],
        )


@dataclass
class Generation:
    """A single representation and the bytes accumulated for it."""

    id: str
    url: str
    etag: str | None
    total: int | None
    content_encoding: str | None
    vary: dict[str, str | None]
    vary_declared: list[str]
    segments: list[Segment] = field(default_factory=list)
    validations: list[ValidationRecord] = field(default_factory=list)
    receipt: Receipt | None = None
    created_seq: int = 0
    last_seq: int = 0
    strong: bool = True
    last_modified: str | None = None
    revocation_reason: str | None = None
    body: bytearray = field(default_factory=bytearray)

    # -- identity -----------------------------------------------------------

    @property
    def resource_key(self) -> str:
        return self.url + "|" + _canonical(self.vary)

    @property
    def covered_ranges(self) -> list[ByteRange]:
        return merge_ranges(
            [segment.range(self.total) for segment in self.segments]
        )

    @property
    def is_complete(self) -> bool:
        return (
            self.receipt is not None
            and self.total is not None
            and not missing_ranges(
                [segment.range(self.total) for segment in self.segments], self.total
            )
        )

    def missing(self) -> list[ByteRange]:
        if self.total is None:
            return []
        return missing_ranges(
            [segment.range(self.total) for segment in self.segments], self.total
        )

    def covers_offset(self, offset: int) -> bool:
        return any(
            segment.start <= offset <= segment.end for segment in self.segments
        )

    # -- mutation -----------------------------------------------------------

    def append(
        self, start: int, data: bytes, request_seq: int
    ) -> None:
        """Append a clean segment, rejecting incompatible overlap.

        The body buffer is indexed by absolute byte offset. New bytes are
        compared against anything already stored for the same offsets; a real
        mismatch revokes any receipt previously signed for this generation
        and drops the incoming segment.
        """
        end = start + len(data) - 1
        if len(data) == 0:
            raise GenerationIdentity("refusing to record an empty segment")

        # Grow the buffer so absolute offsets line up.
        if len(self.body) < end + 1:
            self.body.extend(b"\x00" * (end + 1 - len(self.body)))

        overlap = self._overlap_span(start, end)
        if overlap is not None:
            overlap_start, overlap_end = overlap
            stored = bytes(self.body[overlap_start : overlap_end + 1])
            incoming = data[
                overlap_start - start : overlap_end - start + 1
            ]
            if stored != incoming:
                # Real incompatibility: any sign-off already written against these
                # bytes must be withdrawn, and the contradicting segment is refused.
                self.revoke("incompatible overlapping bytes")
                raise ByteConflict(
                    f"generation {self.id}: bytes {start}-{end} disagree with storage"
                )

        self.body[start : end + 1] = data
        self.segments.append(
            Segment(start, end, request_seq, digest_bytes(data))
        )
        self.segments.sort(key=lambda item: item.start)
        self.last_seq = request_seq
        self.revocation_reason = None

        self._sign_if_complete(request_seq)

    def install_full(self, data: bytes, request_seq: int) -> None:
        """Install a complete 200 representation in one clean step."""
        self.body = bytearray(data)
        self.segments = [
            Segment(0, len(data) - 1, request_seq, digest_bytes(data))
        ] if data else []
        self.total = len(data)
        self.last_seq = request_seq
        self._sign_if_complete(request_seq)

    def revoke(self, reason: str) -> None:
        """Withdraw a signed receipt, leaving an explicit incomplete state."""
        self.receipt = None
        self.revocation_reason = reason

    def _sign_if_complete(self, request_seq: int) -> None:
        if self.total is None:
            return
        if not missing_ranges(
            [segment.range(self.total) for segment in self.segments], self.total
        ):
            if len(self.body) != self.total:
                return
            self.receipt = Receipt(
                generation_id=self.id,
                etag=self.etag,
                total=self.total,
                content_encoding=self.content_encoding,
                body_digest=digest_bytes(bytes(self.body)),
                request_seq=request_seq,
            )

    def _overlap_span(self, start: int, end: int) -> tuple[int, int] | None:
        for segment in self.segments:
            if segment.start <= end and start <= segment.end:
                return (max(start, segment.start), min(end, segment.end))
        return None

    # -- (de)serialization --------------------------------------------------

    def to_dict(self, include_body: bool) -> dict[str, Any]:
        data = {
            "id": self.id,
            "url": self.url,
            "etag": self.etag,
            "total": self.total,
            "content_encoding": self.content_encoding,
            "vary": self.vary,
            "vary_declared": self.vary_declared,
            "segments": [segment.to_dict() for segment in self.segments],
            "validations": [record.to_dict() for record in self.validations],
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "created_seq": self.created_seq,
            "last_seq": self.last_seq,
            "strong": self.strong,
            "last_modified": self.last_modified,
            "revocation_reason": self.revocation_reason,
        }
        if include_body:
            data["body"] = bytes(self.body)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], body: bytes) -> Generation:
        generation = cls(
            id=data["id"],
            url=data["url"],
            etag=data["etag"],
            total=data["total"],
            content_encoding=data.get("content_encoding"),
            vary=data["vary"],
            vary_declared=data.get("vary_declared", []),
            segments=[Segment.from_dict(item) for item in data["segments"]],
            validations=[
                ValidationRecord.from_dict(item) for item in data["validations"]
            ],
            receipt=Receipt.from_dict(data["receipt"]) if data.get("receipt") else None,
            created_seq=data.get("created_seq", 0),
            last_seq=data.get("last_seq", 0),
            strong=data.get("strong", True),
            last_modified=data.get("last_modified"),
            revocation_reason=data.get("revocation_reason"),
            body=bytearray(body),
        )
        return generation


class ResumeStore:
    """Persist generations over a classic :class:`BaseCache` backend."""

    def __init__(self, cache: BaseCache) -> None:
        self.cache = cache
        self.separate_body = isinstance(cache, SeparateBodyBaseCache)
        self._lock = threading.RLock()
        self._index = self._load_index()

    # -- key helpers --------------------------------------------------------

    def _meta_key(self, generation_id: str) -> str:
        return f"{_NS}meta::{generation_id}"

    def _body_key(self, generation_id: str) -> str:
        return f"{_NS}body::{generation_id}"

    def _load_index(self) -> dict[str, Any]:
        raw = self.cache.get(_INDEX_KEY)
        if not raw:
            return {"generations": {}, "current": {}}
        try:
            return msgpack.loads(raw, raw=False)
        except ValueError:
            return {"generations": {}, "current": {}}

    def _save_index(self) -> None:
        self.cache.set(_INDEX_KEY, msgpack.dumps(self._index, use_bin_type=True))

    # -- lookup -------------------------------------------------------------

    def matching_generations(
        self, url: str, request_headers: CaseInsensitiveDict[str]
    ) -> list[Generation]:
        """Return stored generations for ``url`` whose Vary selection matches."""
        matches: list[Generation] = []
        for generation_id_value in self._index["generations"]:
            generation = self.load_generation(generation_id_value)
            if generation is None or generation.url != url:
                continue
            if vary_matches(generation.vary, request_headers):
                matches.append(generation)
        matches.sort(key=lambda item: item.last_seq)
        return matches

    def current_generation(
        self, url: str, request_headers: CaseInsensitiveDict[str]
    ) -> Generation | None:
        for generation in reversed(self.matching_generations(url, request_headers)):
            return generation
        return None

    def load_generation(self, generation_id_value: str) -> Generation | None:
        raw = self.cache.get(self._meta_key(generation_id_value))
        if not raw:
            return None
        try:
            manifest = msgpack.loads(raw, raw=False)
        except ValueError:
            return None

        if self.separate_body:
            body_file = self.cache.get_body(self._body_key(generation_id_value))
            try:
                body = body_file.read() if body_file is not None else b""
            finally:
                if body_file is not None:
                    body_file.close()
        else:
            body = manifest.get("body", b"")

        generation = Generation.from_dict(manifest, body)
        return self._reconcile(generation)

    # -- write path ---------------------------------------------------------

    def new_generation(
        self,
        url: str,
        etag: str | None,
        total: int | None,
        content_encoding: str | None,
        vary: dict[str, str | None],
        vary_declared: list[str],
        request_seq: int,
        strong: bool = True,
        last_modified: str | None = None,
    ) -> Generation:
        with self._lock:
            identifier = generation_id(
                etag, total, content_encoding, vary, last_modified
            )
            existing = self.load_generation(identifier)
            if existing is not None:
                return existing
            generation = Generation(
                id=identifier,
                url=url,
                etag=etag,
                total=total,
                content_encoding=content_encoding,
                vary=dict(vary),
                vary_declared=list(vary_declared),
                created_seq=request_seq,
                last_seq=request_seq,
                strong=strong and is_strong_etag(etag),
                last_modified=last_modified,
            )
            self._persist(generation)
            self._index["generations"][identifier] = self._summary(generation)
            self._index["current"][generation.resource_key] = identifier
            self._save_index()
            return generation

    def save(self, generation: Generation) -> None:
        with self._lock:
            self._reconcile(generation)
            self._persist(generation)
            # Saving an existing generation (a new segment, a validation
            # ledger entry, a revocation) must not re-promote it: the current
            # pointer only advances when a new representation is established.
            self._index["generations"].setdefault(
                generation.id, self._summary(generation)
            )
            self._index["generations"][generation.id] = self._summary(generation)
            self._save_index()

    def _persist(self, generation: Generation) -> None:
        include_body = not self.separate_body
        manifest = generation.to_dict(include_body=include_body)
        encoded = msgpack.dumps(manifest, use_bin_type=True)
        if self.separate_body:
            # Land the body first; a manifest that references bytes not yet
            # present is healed by _reconcile on the next load.
            self.cache.set_body(self._body_key(generation.id), bytes(generation.body))
        self.cache.set(self._meta_key(generation.id), encoded)

    def _reconcile(self, generation: Generation) -> Generation:
        """Drop any segment whose bytes are not actually present.

        After a crash between the separate header and body writes, the manifest
        may describe ranges the body file no longer (or never) contained. We
        verify length plus per-segment digest and discard anything that cannot
        be proven, so the generation is reported either whole or explicitly
        incomplete -- never falsely complete.
        """
        body = bytes(generation.body)
        verified: list[Segment] = []
        for segment in generation.segments:
            if segment.end >= len(body):
                continue
            candidate = body[segment.start : segment.end + 1]
            if len(candidate) != segment.length:
                continue
            if digest_bytes(candidate) != segment.digest:
                continue
            verified.append(segment)
        if len(verified) != len(generation.segments):
            generation.segments = verified
            if generation.is_complete is False:
                generation.revoke("unverified segment after interrupted write")
        return generation

    def current_id_for(self, generation: Generation) -> str | None:
        return self._index["current"].get(generation.resource_key)

    def _summary(self, generation: Generation) -> dict[str, Any]:
        return {
            "url": generation.url,
            "etag": generation.etag,
            "total": generation.total,
            "content_encoding": generation.content_encoding,
            "vary": generation.vary,
            "complete": generation.is_complete,
            "strong": generation.strong,
            "last_seq": generation.last_seq,
        }

    # -- introspection ------------------------------------------------------

    def all_generations(self, url: str) -> list[Generation]:
        result = []
        for identifier in self._index["generations"]:
            generation = self.load_generation(identifier)
            if generation is not None and generation.url == url:
                result.append(generation)
        result.sort(key=lambda item: item.created_seq)
        return result
