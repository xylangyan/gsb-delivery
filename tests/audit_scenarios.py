"""Scenario builder for the A/B/X/R handoff example from the task brief."""

from __future__ import annotations

import copy
from typing import Any

SEARCH_URL = "https://vault.example.test/search"

AREA_A_BBOX = [0.0, 0.0, 10.0, 10.0]
AREA_B_BBOX = [20.0, 20.0, 30.0, 30.0]

TEN = "2026-01-01T10:00:00Z"
NINE = "2026-01-01T09:00:00Z"


def item(
    collection: str, item_id: str, when: str, lon: float, lat: float
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": item_id,
        "collection": collection,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"datetime": when},
        "links": [],
        "assets": {},
    }


def post_request(bbox: list[float], collections: list[str]) -> dict[str, Any]:
    return {
        "method": "POST",
        "url": SEARCH_URL,
        "body": {"bbox": bbox, "collections": collections},
    }


def page(
    page_id: str,
    bbox: list[float],
    collections: list[str],
    features: list[dict[str, Any]],
    *,
    next_link: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    links = [next_link] if next_link else []
    data: dict[str, Any] = {
        "id": page_id,
        "request": post_request(bbox, collections),
        "features": features,
        "links": links,
    }
    if receipt:
        data["receipt"] = receipt
    return data


def receipt(
    bbox: list[float],
    collections: list[str],
    first: str,
    last: str,
    seal_id: str = "seal-1",
) -> dict[str, Any]:
    return {
        "seal_id": seal_id,
        "request": post_request(bbox, collections),
        "swept": {"first": first, "last": last},
    }


def radar_link(bbox: list[float]) -> dict[str, Any]:
    # A non-merge POST link replaces the whole body with a radar-only query,
    # but keeps hitting the same address.
    return {
        "rel": "next",
        "method": "POST",
        "href": SEARCH_URL,
        "merge": False,
        "body": {"bbox": bbox, "collections": ["radar"]},
    }


def rgb_merge_link(bbox: list[float]) -> dict[str, Any]:
    return {
        "rel": "next",
        "method": "POST",
        "href": SEARCH_URL,
        "merge": True,
        "body": {"collections": ["rgb"]},
    }


def key_text(when: str, collection: str, item_id: str) -> str:
    return f"{when}|{collection}|{item_id}"


def rgb_key(area: str) -> str:
    return key_text(TEN, "rgb", f"rgb-{area}")


def radar_key(area: str) -> str:
    return key_text(TEN, "radar", f"radar-{area}")


def old_radar_key(area: str) -> str:
    return key_text(NINE, "radar", f"radar-{area}-old")


def build_manifest() -> dict[str, Any]:
    return {
        "search_url": SEARCH_URL,
        "method": "POST",
        "seal_id": "seal-1",
        "k": 1,
        "areas": [
            {"id": "A", "bbox": AREA_A_BBOX},
            {"id": "B", "bbox": AREA_B_BBOX},
        ],
        "categories": [
            {"name": "radar", "collections": ["radar"]},
            {"name": "rgb", "collections": ["rgb"]},
        ],
        "query": {},
    }


def _area_items(area: str) -> dict[str, list[dict[str, Any]]]:
    lon, lat = (5.0, 5.0) if area == "a" else (25.0, 25.0)
    bbox = AREA_A_BBOX if area == "a" else AREA_B_BBOX
    upper = area.upper()
    return {
        "bbox": bbox,
        "items": [
            item("rgb", f"rgb-{upper}", TEN, lon, lat),
            item("radar", f"radar-{upper}", TEN, lon, lat),
            item("radar", f"radar-{upper}-old", NINE, lon, lat),
        ],
    }


def build_archive(*, trim_radar_receipt: bool = False) -> dict[str, Any]:
    volumes: list[dict[str, Any]] = []

    # Dedicated per-area volumes, cost 4 each, sweeping the whole area.
    for area in ("a", "b"):
        bundle = _area_items(area)
        bbox = bundle["bbox"]
        upper = area.upper()
        full_receipt = receipt(
            bbox,
            ["radar", "rgb"],
            first=rgb_key(upper),
            last="min",
        )
        volumes.append(
            {
                "id": f"vol-{upper}",
                "cost": 4,
                "days": ["2026-01-01"],
                "pages": [
                    page(
                        f"page-{upper}-root",
                        bbox,
                        ["radar", "rgb"],
                        bundle["items"],
                        receipt=full_receipt,
                    )
                ],
            }
        )

    # X volume, cost 3: both rgb items, rgb-only sweep receipts per area.
    x_pages: list[dict[str, Any]] = []
    for area in ("a", "b"):
        bundle = _area_items(area)
        bbox = bundle["bbox"]
        upper = area.upper()
        rgb_item_only = [
            feature for feature in bundle["items"] if feature["collection"] == "rgb"
        ]
        x_pages.append(
            page(
                f"page-X-{upper}-root",
                bbox,
                ["radar", "rgb"],
                rgb_item_only,
                next_link=rgb_merge_link(bbox),
            )
        )
        x_pages.append(
            page(
                f"page-X-{upper}-rgb",
                bbox,
                ["rgb"],
                rgb_item_only,
                receipt=receipt(bbox, ["rgb"], first=rgb_key(upper), last="min"),
            )
        )
    volumes.append({"id": "vol-X", "cost": 3, "days": ["2026-01-01"], "pages": x_pages})

    # R volume, cost 2: empty root pages at the same address, replacement links
    # narrow the body to radar; radar pages carry the radar sweep receipts.
    r_pages: list[dict[str, Any]] = []
    for area in ("a", "b"):
        bundle = _area_items(area)
        bbox = bundle["bbox"]
        upper = area.upper()
        radar_items = [
            feature for feature in bundle["items"] if feature["collection"] == "radar"
        ]
        if trim_radar_receipt:
            # The same-second radar key is removed from the swept range, so the
            # newest radar record cannot be proven even though the page exists.
            radar_receipt = receipt(
                bbox, ["radar"], first=old_radar_key(upper), last="min"
            )
        else:
            radar_receipt = receipt(
                bbox, ["radar"], first=radar_key(upper), last="min"
            )
        r_pages.append(
            page(
                f"page-R-{upper}-empty",
                bbox,
                ["radar", "rgb"],
                [],
                next_link=radar_link(bbox),
            )
        )
        r_pages.append(
            page(
                f"page-R-{upper}-radar",
                bbox,
                ["radar"],
                radar_items,
                receipt=radar_receipt,
            )
        )
    volumes.append({"id": "vol-R", "cost": 2, "days": ["2026-01-01"], "pages": r_pages})

    return {"volumes": volumes}


def build_archive_deepcopy_safe() -> dict[str, Any]:
    return copy.deepcopy(build_archive())


if __name__ == "__main__":
    import json

    print(json.dumps(build_manifest(), indent=2))
