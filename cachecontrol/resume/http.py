# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP value objects and header parsing for resumable partial caching.

This module is intentionally independent of both :mod:`requests` and
:mod:`urllib3` so the resumable cache can be driven by the in-process
:class:`~cachecontrol.resume.transport.Transport` stand-in without opening a
single socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from requests.structures import CaseInsensitiveDict

#: The single byte-range unit we know how to speak and to validate.
BYTE_RANGE_UNIT = "bytes"


@dataclass(frozen=True)
class ByteRange:
    """An inclusive, absolute byte range ``start``..``end``.

    ``total`` is the complete representation length advertised by the
    ``Content-Range`` header (``"bytes start-end/total"``); it is ``None``
    when the origin declines to state it (``"*/"`` form is never produced by
    a 206, but parsers stay defensive).
    """

    start: int
    end: int
    total: int | None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"inverted byte range: {self.start}-{self.end}")
        if self.total is not None and self.total <= self.end:
            raise ValueError(
                f"content-range total {self.total} does not cover end {self.end}"
            )

    @property
    def length(self) -> int:
        """Number of bytes the range covers."""
        return self.end - self.start + 1

    def covers(self, other: ByteRange) -> bool:
        """Return whether ``other`` lies entirely within this range."""
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: ByteRange) -> bool:
        """Return whether the two ranges share at least one byte."""
        return self.start <= other.end and other.start <= self.end

    def as_request_header(self) -> str:
        """Render the range for a request ``Range`` header."""
        return f"{BYTE_RANGE_UNIT}={self.start}-{self.end}"


class HttpResponse:
    """A transport-neutral HTTP response.

    ``body`` is the (possibly partial) payload exactly as delivered. It is
    ``b""`` for a 304. Headers are case-insensitive like the real thing.
    """

    def __init__(
        self,
        status: int,
        headers: Iterable[tuple[str, str]] | dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.body = body

    @property
    def is_not_modified(self) -> bool:
        return self.status == 304

    @property
    def is_partial(self) -> bool:
        return self.status == 206

    @property
    def etag(self) -> str | None:
        return self.headers.get("ETag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("Last-Modified")


def split_header_list(value: str | None) -> list[str]:
    """Split a comma separated header into trimmed, non-empty tokens."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def vary_headers(headers: CaseInsensitiveDict[str]) -> list[str]:
    """Return the header names named by ``Vary`` in declaration order."""
    return split_header_list(headers.get("Vary"))


def parse_content_range(
    headers: CaseInsensitiveDict[str],
) -> ByteRange | None:
    """Parse a ``Content-Range: bytes start-end/total`` header.

    Returns ``None`` when the header is absent, malformed, or uses any range
    unit other than ``bytes``.
    """
    raw = headers.get("Content-Range")
    if not raw:
        return None
    try:
        unit, spec = raw.split(None, 1)
    except ValueError:
        return None
    if unit.lower() != BYTE_RANGE_UNIT:
        return None
    try:
        span, total_part = spec.split("/", 1)
        start_text, end_text = span.split("-", 1)
        start, end = int(start_text), int(end_text)
        total = None if total_part.strip() == "*" else int(total_part)
        return ByteRange(start, end, total)
    except (ValueError, TypeError):
        return None


def parse_content_length(headers: CaseInsensitiveDict[str]) -> int | None:
    """Return a non-negative ``Content-Length`` or ``None``."""
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def is_strong_etag(etag: str | None) -> bool:
    """A strong validator is a non-empty tag not prefixed with ``W/``.

    ETag grammar (RFC 9110 8.8.3) always begins with a quote, so the weak
    marker, if present, is exactly the leading ``W/``.
    """
    return bool(etag) and not etag.lstrip().startswith(("W/", "w/"))


def select_vary_dimensions(
    declared: Iterable[str], request_headers: CaseInsensitiveDict[str]
) -> dict[str, str | None]:
    """Snapshot the request header values selected by a ``Vary`` declaration.

    A header the request did not carry is recorded as ``None``. An explicitly
    empty value is recorded as the empty string and kept distinct from a
    missing header, mirroring how a stand-in must recognise "not filled" and
    "filled but empty" separately.
    """
    selected: dict[str, str | None] = {}
    for name in declared:
        if name == "*":
            # ``Vary: *`` never matches; callers treat the wildcard on its own.
            continue
        selected[name] = request_headers.get(name)
    return selected


def vary_matches(
    stored: dict[str, str | None],
    request_headers: CaseInsensitiveDict[str],
) -> bool:
    """Compare a stored Vary selection against a fresh request."""
    for name, value in stored.items():
        if request_headers.get(name) != value:
            return False
    return True


def merge_ranges(ranges: Iterable[ByteRange]) -> list[ByteRange]:
    """Merge overlapping/adjacent ranges into a minimal, sorted list."""
    ordered = sorted(ranges, key=lambda item: (item.start, item.end))
    merged: list[ByteRange] = []
    for current in ordered:
        if not merged or current.start > merged[-1].end + 1:
            merged.append(current)
            continue
        previous = merged[-1]
        total = previous.total if previous.total is not None else current.total
        merged[-1] = ByteRange(previous.start, max(previous.end, current.end), total)
    return merged


def missing_ranges(
    covered: Iterable[ByteRange], total: int
) -> list[ByteRange]:
    """Given covered ranges within a ``total`` length, list the gaps."""
    gaps: list[ByteRange] = []
    cursor = 0
    for segment in merge_ranges(covered):
        if segment.start > cursor:
            gaps.append(ByteRange(cursor, segment.start - 1, total))
        cursor = max(cursor, segment.end + 1)
    if cursor < total:
        gaps.append(ByteRange(cursor, total - 1, total))
    return gaps


@dataclass
class RequestLog:
    """An audit record of one transport round trip."""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
