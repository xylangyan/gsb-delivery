"""Replay sealed STAC search chains.

A sealed archive stores read-only copies of search pages together with the
exact requests that produced them.  A page is only trustworthy for a given
starting request when the chain of STAC paging links can be followed from
that request to the page and every hop reproduces a request that was actually
recorded.  The rules here mirror :class:`pystac_client.stac_api_io.StacApiIO`
and :class:`pystac.link.Link` semantics:

* a link without ``method`` is a GET whose href already carries the query,
* a POST link replaces the request body unless ``merge`` is true, in which
  case the body is merged on top of the previous body,
* the same URL may back different pages, so bodies and query strings are
  part of the request identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class SealedRequest:
    """The concrete HTTP request that produced a sealed page."""

    url: str
    method: str = "GET"
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SealedRequest":
        method = str(data.get("method", "GET")).upper()
        url = data["url"]
        if method in ("POST", "PUT", "PATCH"):
            parameters = dict(data.get("body") or data.get("parameters") or {})
        else:
            parameters = dict(data.get("parameters") or {})
        return cls(url=url, method=method, parameters=parameters)

    def to_dict(self) -> dict[str, Any]:
        if self.method in ("POST", "PUT", "PATCH"):
            return {
                "method": self.method,
                "url": self.url,
                "body": dict(self.parameters),
            }
        return {"method": self.method, "url": self.url}


def _canonical_scalar(text: str) -> str:
    # Numeric query values (bbox coordinates, limits) compare by number, so
    # "0" and "0.0" are identical; everything else stays a literal string.
    try:
        return str(float(text))
    except ValueError:
        return text


_UNORDERED_LIST_PARAMS = ("collections", "ids")


def _canonical_query_value(key: str, text: str) -> str:
    parts = [_canonical_scalar(part) for part in text.split(",")]
    if key in _UNORDERED_LIST_PARAMS:
        parts = sorted(parts)
    return ",".join(parts)


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    normalized_query = "&".join(
        f"{key}={_canonical_query_value(key, value)}"
        for key in sorted(query)
        for value in sorted(query[key])
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            normalized_query,
            "",
        )
    )


def requests_match(left: SealedRequest, right: SealedRequest) -> bool:
    """Semantic request equality, independent of query/body key order."""
    return request_key(left) == request_key(right)


def request_key(request: SealedRequest) -> tuple[str, str, str]:
    """Hashable signature with the same equality semantics as requests_match."""
    normalized = _normalize_url(request.url)
    if request.method == "GET":
        return ("GET", normalized, "")
    base_url = normalized.split("?", 1)[0]
    body = json.dumps(
        _canonicalize(request.parameters), sort_keys=True, default=str
    )
    return (request.method, base_url, body)


def _canonicalize(value: Any) -> Any:
    """Normalize a request body so unordered scalar lists compare equal.

    STAC treats ``collections`` and ``ids`` as unordered sets, while ordered
    arrays such as ``bbox`` must keep their sequence, so only lists made
    entirely of strings are sorted.
    """
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [_canonicalize(item) for item in value]
        if normalized and all(isinstance(item, str) for item in normalized):
            return sorted(normalized)
        return normalized
    return value


def resolve_link(
    link: dict[str, Any], current: SealedRequest
) -> SealedRequest:
    """Resolve a STAC paging link against the request of the current page."""
    href = link.get("href")
    if not href:
        raise ValueError("paging link is missing href")
    href = urljoin(current.url, str(href))
    method = str(link.get("method", "GET")).upper()
    merge = bool(link.get("merge", False))

    if method in ("POST", "PUT", "PATCH"):
        link_body = link.get("body", {}) or {}
        if merge:
            parameters = {**current.parameters, **dict(link_body)}
        else:
            parameters = dict(link_body)
        return SealedRequest(url=href, method=method, parameters=parameters)

    # GET links carry their parameters in the href; extra link bodies are not
    # sent by the reference STAC IO implementation.
    return SealedRequest(url=href, method="GET", parameters={})


class UnsealableChainError(ValueError):
    """Raised when a sealed chain cannot be replayed for a given request."""


def find_page(
    request: SealedRequest,
    pages: list["SealedPage"],  # noqa: F821 - forward reference
    *,
    after: int = -1,
) -> tuple[int, "SealedPage"] | None:
    """Find the first page after ``after`` whose recorded request matches."""
    for index in range(after + 1, len(pages)):
        page = pages[index]
        if requests_match(page.request, request):
            return index, page
    return None
