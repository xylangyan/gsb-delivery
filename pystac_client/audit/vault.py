"""Sealed archive data model and chain traversal.

An archive is a read-only replica organized into volumes.  Each volume groups
sealed pages, has a single access fee that is charged at most once, and every
page carries an optional receipt describing the fully swept sort-key interval
of the request that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .keys import KeyInterval, SortKey, item_sort_key
from .replay import (
    SealedRequest,
    request_key,
    requests_match,
    resolve_link,
)


@dataclass(frozen=True)
class Receipt:
    """A per-page sealed receipt.

    Attributes:
        seal_id: Archive seal number under which the page was sealed.
        request: The actual request whose sweep the receipt documents.
        swept: The inclusive sort-key interval fully swept by that request.
    """

    seal_id: str
    request: SealedRequest
    swept: KeyInterval

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Receipt":
        request = SealedRequest.from_dict(data["request"])
        swept_data = data.get("swept") or {}
        swept = KeyInterval.parse(
            swept_data.get("first"),
            swept_data.get("last"),
        )
        return cls(
            seal_id=str(data["seal_id"]),
            request=request,
            swept=swept,
        )


@dataclass
class SealedPage:
    """One sealed search page and its receipt (if any)."""

    id: str
    request: SealedRequest
    features: list[dict[str, Any]]
    links: list[dict[str, Any]]
    receipt: Receipt | None
    volume_id: str

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], volume_id: str, default_id: str
    ) -> "SealedPage":
        request = SealedRequest.from_dict(data["request"])
        receipt_data = data.get("receipt")
        receipt = Receipt.from_dict(receipt_data) if receipt_data else None
        if receipt is not None and not requests_match(receipt.request, request):
            raise ValueError(
                f"page {default_id} receipt request does not match the recorded "
                "request"
            )
        return cls(
            id=str(data.get("id", default_id)),
            request=request,
            features=list(data.get("features") or []),
            links=list(data.get("links") or []),
            receipt=receipt,
            volume_id=volume_id,
        )

    def next_link(self) -> dict[str, Any] | None:
        return next(
            (link for link in self.links if link.get("rel") == "next"),
            None,
        )

    def item_keys(self) -> list[SortKey]:
        return [item_sort_key(feature) for feature in self.features]


@dataclass
class Volume:
    """A payable unit of sealed pages."""

    id: str
    cost: int
    pages: list[SealedPage]
    days: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Volume":
        volume_id = str(data["id"])
        pages = [
            SealedPage.from_dict(page, volume_id, f"{volume_id}-{index}")
            for index, page in enumerate(data.get("pages") or [])
        ]
        days = tuple(str(day) for day in data.get("days") or ())
        reserved = {"id", "cost", "pages", "days"}
        metadata = {k: v for k, v in data.items() if k not in reserved}
        return cls(
            id=volume_id,
            cost=int(data["cost"]),
            pages=pages,
            days=days,
            metadata=metadata,
        )


class Archive:
    """A read-only sealed archive backed by in-memory volumes."""

    def __init__(self, volumes: list[Volume]):
        self.volumes = list(volumes)
        self._volumes_by_id = {volume.id: volume for volume in self.volumes}
        if len(self._volumes_by_id) != len(self.volumes):
            raise ValueError("volume ids must be unique")
        self._request_index: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
        for volume in self.volumes:
            for index, page in enumerate(volume.pages):
                self._request_index.setdefault(
                    request_key(page.request), []
                ).append((volume.id, index))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Archive":
        return cls([Volume.from_dict(volume) for volume in data.get("volumes", [])])

    @classmethod
    def from_file(cls, path: str | Path) -> "Archive":
        with open(path) as handle:
            return cls.from_dict(json.load(handle))

    def volume(self, volume_id: str) -> Volume:
        return self._volumes_by_id[volume_id]

    def all_pages(self) -> list[tuple[Volume, SealedPage]]:
        return [
            (volume, page) for volume in self.volumes for page in volume.pages
        ]

    def find_start_pages(
        self, request: SealedRequest
    ) -> list[tuple[Volume, SealedPage]]:
        signature = request_key(request)
        matches: list[tuple[Volume, SealedPage]] = []
        for volume_id, index in self._request_index.get(signature, []):
            volume = self._volumes_by_id[volume_id]
            page = volume.pages[index]
            if requests_match(page.request, request):
                matches.append((volume, page))
        return matches

    def replay_chain(
        self, volume: Volume, start: SealedRequest
    ) -> list[SealedPage]:
        """Follow STAC paging links from ``start`` inside one volume.

        Only pages reachable from the start request by the original link
        semantics are returned, so receipts on those pages can be tied back
        to the authorized query.  Cycles and repeated pages stop the walk.
        """
        index: dict[tuple[str, str, str], SealedPage] = {
            request_key(page.request): page for page in volume.pages
        }
        current = index.get(request_key(start))
        if current is None:
            return []
        reached: list[SealedPage] = []
        seen: set[str] = set()
        current_request = start
        while current.id not in seen:
            seen.add(current.id)
            reached.append(current)
            link = current.next_link()
            if link is None:
                break
            current_request = resolve_link(link, current_request)
            current = index.get(request_key(current_request))
            if current is None:
                break
        return reached
