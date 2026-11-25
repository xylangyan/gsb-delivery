"""Large-scale sealed-archive sample generator for performance checks.

Mirrors the peak season constraints in the task brief:

* archives are sealed per UTC day,
* a volume spans at most two adjacent days,
* at most eight volumes offer Items or sweep receipts for any given day,
* volumes may serve both areas, with overlapping rectangles and sweep ranges,
* the two areas' cutoff keys may fall on different days.
"""

from __future__ import annotations

from typing import Any

SEARCH_URL = "https://vault.example.test/search"
AREA_A = [0.0, 0.0, 10.0, 10.0]
AREA_B = [20.0, 20.0, 30.0, 30.0]
COLLECTIONS = ["radar", "rgb"]


def _root_body(bbox: list[float]) -> dict[str, Any]:
    return {"bbox": bbox, "collections": list(COLLECTIONS)}


def _category_body(bbox: list[float], collection: str) -> dict[str, Any]:
    return {"bbox": bbox, "collections": [collection]}


def _post(body: dict[str, Any]) -> dict[str, Any]:
    return {"method": "POST", "url": SEARCH_URL, "body": body}


def _item(collection: str, item_id: str, day: int, index: int, lon: float,
          lat: float) -> dict[str, Any]:
    hour = index % 24
    return {
        "type": "Feature",
        "id": item_id,
        "collection": collection,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "datetime": f"2026-02-{day:02d}T{hour:02d}:00:00Z",
        },
    }


def build_large_archive(
    *,
    volumes: int = 399,
    per_day: int = 8,
    items_per_category_page: int = 12,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pages_total = 0
    items_total = 0
    archive_volumes: list[dict[str, Any]] = []

    for volume_index in range(volumes):
        day = 1 + (volume_index // per_day) % 28
        multi_day = volume_index % 3 == 0
        days = [f"2026-02-{day:02d}"]
        if multi_day and day < 28:
            days.append(f"2026-02-{day + 1:02d}")

        # All volumes can serve both areas so shared fees and overlapping
        # sweep ranges are exercised throughout the sample.
        area_codes = ["a", "b"]
        pages: list[dict[str, Any]] = []
        for area_code in area_codes:
            bbox = AREA_A if area_code == "a" else AREA_B
            lon, lat = (5.0, 5.0) if area_code == "a" else (25.0, 25.0)
            root_id = f"v{volume_index}-{area_code}-root"
            radar_id = f"v{volume_index}-{area_code}-radar"
            rgb_id = f"v{volume_index}-{area_code}-rgb"

            radar_items = [
                _item(
                    "radar",
                    f"radar-{volume_index}-{area_code}-{idx}",
                    day,
                    idx,
                    lon,
                    lat,
                )
                for idx in range(items_per_category_page)
            ]
            rgb_items = [
                _item(
                    "rgb",
                    f"rgb-{volume_index}-{area_code}-{idx}",
                    day,
                    idx,
                    lon,
                    lat,
                )
                for idx in range(items_per_category_page)
            ]
            items_total += len(radar_items) + len(rgb_items)

            root_page = {
                "id": root_id,
                "request": _post(_root_body(bbox)),
                "features": [],
                "links": [
                    {
                        "rel": "next",
                        "method": "POST",
                        "href": SEARCH_URL,
                        "body": _category_body(bbox, "radar"),
                    }
                ],
            }
            radar_page = {
                "id": radar_id,
                "request": _post(_category_body(bbox, "radar")),
                "features": radar_items,
                "links": [
                    {
                        "rel": "next",
                        "method": "POST",
                        "href": SEARCH_URL,
                        "body": _category_body(bbox, "rgb"),
                    }
                ],
                "receipt": {
                    "seal_id": f"seal-2026-02-{day:02d}",
                    "request": _post(_category_body(bbox, "radar")),
                    "swept": {"first": "max", "last": "min"},
                },
            }
            rgb_page = {
                "id": rgb_id,
                "request": _post(_category_body(bbox, "rgb")),
                "features": rgb_items,
                "links": [],
                "receipt": {
                    "seal_id": f"seal-2026-02-{day:02d}",
                    "request": _post(_category_body(bbox, "rgb")),
                    "swept": {"first": "max", "last": "min"},
                },
            }
            pages.extend([root_page, radar_page, rgb_page])
            pages_total += 3

        archive_volumes.append(
            {
                "id": f"vol-{volume_index:03d}",
                "cost": 1 + (volume_index % 6),
                "days": days,
                "pages": pages,
            }
        )

    # One guaranteed gold volume holds the globally newest k items for every
    # requirement and sweeps to min, making the handoff feasible.
    gold_pages: list[dict[str, Any]] = []
    for area_code, bbox, lon, lat in (
        ("a", AREA_A, 5.0, 5.0),
        ("b", AREA_B, 25.0, 25.0),
    ):
        gold_pages.append(
            {
                "id": f"gold-{area_code}-root",
                "request": _post(_root_body(bbox)),
                "features": [],
                "links": [
                    {
                        "rel": "next",
                        "method": "POST",
                        "href": SEARCH_URL,
                        "body": _category_body(bbox, "radar"),
                    }
                ],
            }
        )
        for collection in COLLECTIONS:
            is_last = collection == COLLECTIONS[-1]
            features = [
                _item(
                    collection,
                    f"gold-{collection}-{area_code}-{idx}",
                    28,
                    23 - idx,
                    lon,
                    lat,
                )
                for idx in range(5)
            ]
            items_total += len(features)
            links = []
            if not is_last:
                next_collection = COLLECTIONS[COLLECTIONS.index(collection) + 1]
                links.append(
                    {
                        "rel": "next",
                        "method": "POST",
                        "href": SEARCH_URL,
                        "body": _category_body(bbox, next_collection),
                    }
                )
            gold_pages.append(
                {
                    "id": f"gold-{area_code}-{collection}",
                    "request": _post(_category_body(bbox, collection)),
                    "features": features,
                    "links": links,
                    "receipt": {
                        "seal_id": "seal-gold",
                        "request": _post(_category_body(bbox, collection)),
                        "swept": {"first": "max", "last": "min"},
                    },
                }
            )
            pages_total += 1
    archive_volumes.append(
        {
            "id": "vol-gold",
            "cost": 7,
            "days": ["2026-02-27", "2026-02-28"],
            "pages": gold_pages,
        }
    )

    archive = {"volumes": archive_volumes}
    manifest = {
        "search_url": SEARCH_URL,
        "method": "POST",
        "seal_id": None,
        "k": 3,
        "areas": [
            {"id": "A", "bbox": AREA_A},
            {"id": "B", "bbox": AREA_B},
        ],
        "categories": [
            {"name": "radar", "collections": ["radar"]},
            {"name": "rgb", "collections": ["rgb"]},
        ],
        "query": {},
    }
    return archive, manifest
