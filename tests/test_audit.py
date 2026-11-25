"""Tests for the sealed-archive handoff review (pystac_client.audit)."""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import pytest

from pystac_client.audit import (
    Archive,
    KeyInterval,
    Manifest,
    MIN_KEY,
    MAX_KEY,
    SealedRequest,
    SortKey,
    audit_handoff,
    geometry_intersects_bbox,
    item_sort_key,
)
from pystac_client.audit.geometry import (
    normalize_bbox,
    point_in_bbox,
    polygon_intersects_bbox,
    segments_intersect,
)
from pystac_client.audit.keys import (
    natural_compare,
    parse_datetime,
    parse_sort_key,
    sort_compare,
)
from pystac_client.audit.planner import (
    merge_intervals,
    query_time_lower_bound,
    replay_area_chains,
    uncovered_segments,
)
from pystac_client.audit.replay import (
    request_key,
    requests_match,
    resolve_link,
)

from .audit_scenarios import (
    AREA_A_BBOX,
    AREA_B_BBOX,
    build_archive,
    build_manifest,
)
from .audit_perfdata import build_large_archive


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_normalize_bbox_accepts_strings_and_antimeridian() -> None:
    assert normalize_bbox("1,2,3,4") == (1.0, 2.0, 3.0, 4.0)
    crossing = normalize_bbox([170, -10, -170, 10])
    assert crossing[0] > crossing[2]
    with pytest.raises(ValueError):
        normalize_bbox([0, 10, 10, 0])


def test_point_in_bbox_closed_and_antimeridian() -> None:
    bbox = normalize_bbox([0, 0, 10, 10])
    assert point_in_bbox((0, 0), bbox)
    assert point_in_bbox((10, 10), bbox)
    assert not point_in_bbox((10.0001, 5), bbox)

    crossing = normalize_bbox([170, -10, -170, 10])
    assert point_in_bbox((180, 0), crossing)
    assert point_in_bbox((-180, 0), crossing)
    assert point_in_bbox((175, 5), crossing)
    assert point_in_bbox((-175, -10), crossing)
    assert not point_in_bbox((0, 0), crossing)


def test_segment_intersection_boundary_counts() -> None:
    assert segments_intersect((0, 0), (10, 10), (10, 0), (0, 10))
    assert segments_intersect((0, 0), (5, 5), (5, 5), (10, 10))
    assert not segments_intersect((0, 0), (1, 1), (2, 2), (3, 3))


def test_geometry_intersects_bbox_for_all_geometry_types() -> None:
    bbox = normalize_bbox([0, 0, 10, 10])
    assert geometry_intersects_bbox(
        {"type": "Point", "coordinates": [10, 10]}, bbox
    )
    assert not geometry_intersects_bbox(
        {"type": "Point", "coordinates": [11, 10]}, bbox
    )
    line = {"type": "LineString", "coordinates": [[-5, 5], [5, 5]]}
    assert geometry_intersects_bbox(line, bbox)
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]],
        ],
    }
    assert not geometry_intersects_bbox(polygon, bbox)
    assert geometry_intersects_bbox(polygon, normalize_bbox(AREA_B_BBOX))


def test_polygon_with_hole_intersection() -> None:
    bbox = normalize_bbox([0, 0, 10, 10])
    donut = {
        "type": "Polygon",
        "coordinates": [
            [[-5, -5], [15, -5], [15, 15], [-5, 15], [-5, -5]],
            [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]],
        ],
    }
    # A rectangle entirely inside the hole has no solid-polygon contact.
    inside_hole = normalize_bbox([3, 3, 7, 7])
    assert not polygon_intersects_bbox(
        donut["coordinates"][0], donut["coordinates"][1:], inside_hole
    )
    # A rectangle reaching the solid ring does intersect.
    wide = normalize_bbox([-2, -2, 1, 1])
    assert polygon_intersects_bbox(
        donut["coordinates"][0], donut["coordinates"][1:], wide
    )


def test_antimeridian_polygon_intersection() -> None:
    crossing = normalize_bbox([170, -10, -170, 10])
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[175, -5], [180, -5], [180, 5], [175, 5], [175, -5]],
        ],
    }
    assert geometry_intersects_bbox(polygon, crossing)
    polygon_far = {
        "type": "Polygon",
        "coordinates": [
            [[0, -5], [5, -5], [5, 5], [0, 5], [0, -5]],
        ],
    }
    assert not geometry_intersects_bbox(polygon_far, crossing)


# ---------------------------------------------------------------------------
# Sort keys and intervals
# ---------------------------------------------------------------------------


def _key(when: str, collection: str, item_id: str) -> SortKey:
    return parse_sort_key(f"{when}|{collection}|{item_id}")


def test_datetime_parsing_utc() -> None:
    dt = parse_datetime("2026-01-01T10:00:00Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timezone.utc.utcoffset(dt)
    assert parse_datetime("2026-01-01T12:00:00+02:00").hour == 10


def test_sort_order_time_desc_then_collection_then_id() -> None:
    newer = _key("2026-01-01T10:00:00Z", "b", "x")
    older = _key("2026-01-01T09:00:00Z", "a", "z")
    assert sort_compare(newer, older) > 0
    same_time_a = _key("2026-01-01T10:00:00Z", "a", "z")
    same_time_b = _key("2026-01-01T10:00:00Z", "b", "a")
    assert sort_compare(same_time_a, same_time_b) < 0
    same_coll_x = _key("2026-01-01T10:00:00Z", "a", "x")
    assert sort_compare(same_coll_x, same_time_a) < 0
    assert natural_compare(MIN_KEY, newer) < 0
    assert natural_compare(newer, MAX_KEY) < 0


def test_item_sort_key_uses_properties() -> None:
    feature = {
        "id": "i1",
        "collection": "radar",
        "properties": {"datetime": "2026-01-01T10:00:00Z"},
    }
    key = item_sort_key(feature)
    assert key.collection == "radar" and key.id == "i1"


def test_interval_merge_and_uncovered_segments() -> None:
    first = _key("2026-01-01T10:00:00Z", "rgb", "a")
    middle = _key("2026-01-01T09:00:00Z", "rgb", "b")
    last = _key("2026-01-01T08:00:00Z", "rgb", "c")
    intervals = [
        KeyInterval(middle, first),
        KeyInterval(last, middle),
    ]
    merged = merge_intervals(intervals)
    assert len(merged) == 1
    assert merged[0].low == last and merged[0].high == first
    assert uncovered_segments(merged, KeyInterval(last, first)) == []

    gapped = [KeyInterval(last, middle)]
    gaps = uncovered_segments(gapped, KeyInterval(last, first))
    assert gaps == [KeyInterval(middle, first)]

    # Receipts entirely below the target still report the full open range.
    below = KeyInterval(MIN_KEY, last)
    target = KeyInterval(middle, first)
    assert uncovered_segments([below], target) == [target]


# ---------------------------------------------------------------------------
# Request replay semantics
# ---------------------------------------------------------------------------


def test_requests_match_ignores_key_order_and_query_order() -> None:
    post_a = SealedRequest(
        "https://v.test/search",
        "POST",
        {"bbox": [0, 0, 1, 1], "collections": ["radar", "rgb"]},
    )
    post_b = SealedRequest(
        "https://v.test/search",
        "POST",
        {"collections": ["rgb", "radar"], "bbox": [0, 0, 1, 1]},
    )
    assert requests_match(post_a, post_b)
    get_a = SealedRequest("https://v.test/search?bbox=0,0,1,1&limit=5", "GET")
    get_b = SealedRequest("https://v.test/search?limit=5&bbox=0,0,1,1", "GET")
    assert requests_match(get_a, get_b)
    assert request_key(get_a) != request_key(post_a)


def test_same_address_different_bodies_are_distinct_pages() -> None:
    url = "https://v.test/search"
    full = SealedRequest(
        url, "POST", {"bbox": [0, 0, 1, 1], "collections": ["radar", "rgb"]}
    )
    radar_only = SealedRequest(
        url, "POST", {"bbox": [0, 0, 1, 1], "collections": ["radar"]}
    )
    assert not requests_match(full, radar_only)


def test_resolve_link_post_merge_and_replace() -> None:
    url = "https://v.test/search"
    current = SealedRequest(
        url,
        "POST",
        {"bbox": [0, 0, 1, 1], "collections": ["radar", "rgb"], "limit": 10},
    )
    merged = resolve_link(
        {"rel": "next", "method": "POST", "href": url, "merge": True,
         "body": {"collections": ["rgb"]}},
        current,
    )
    assert merged.parameters == {
        "bbox": [0, 0, 1, 1],
        "collections": ["rgb"],
        "limit": 10,
    }
    replaced = resolve_link(
        {
            "rel": "next",
            "method": "POST",
            "href": url,
            "body": {"collections": ["radar"]},
        },
        current,
    )
    assert replaced.parameters == {"collections": ["radar"]}

    get_link = resolve_link(
        {"rel": "next", "href": f"{url}?token=abc"}, current
    )
    assert get_link.method == "GET" and get_link.url.endswith("token=abc")


# ---------------------------------------------------------------------------
# End-to-end planner on the A/B/X/R example
# ---------------------------------------------------------------------------


def test_minimum_cost_picks_x_and_r() -> None:
    result = audit_handoff(build_archive(), build_manifest())
    assert result.feasible
    assert result.total_cost == 5
    assert set(result.selected_volumes) == {"vol-X", "vol-R"}
    by_id = {r.requirement.id: r for r in result.requirements}
    assert by_id["A:rgb"].selected_volume == "vol-X"
    assert by_id["A:radar"].selected_volume == "vol-R"
    assert by_id["B:rgb"].selected_volume == "vol-X"
    assert by_id["B:radar"].selected_volume == "vol-R"
    for requirement in result.requirements:
        assert requirement.covered
        assert len(requirement.items) == 1
        record = requirement.items[0]
        assert record.page_id  # the item was found on an actual page
        assert requirement.receipt_pages  # cutoff is backed by reached receipts
    report = result.to_dict()
    for entry in report["requirements"]:
        assert entry["receipt_pages"]


def test_dedicated_volumes_cost_eight_when_alone() -> None:
    archive = {
        "volumes": [
            volume
            for volume in build_archive()["volumes"]
            if volume["id"] in ("vol-A", "vol-B")
        ]
    }
    result = audit_handoff(archive, build_manifest())
    assert result.feasible
    assert result.total_cost == 8
    assert set(result.selected_volumes) == {"vol-A", "vol-B"}


def test_shared_volume_charged_once_even_when_serving_both_areas() -> None:
    # Build one volume that proves all four requirements for cost 1.
    archive = build_archive()
    shared = {
        "id": "vol-shared",
        "cost": 1,
        "pages": [],
    }
    for dedicated in ("vol-A", "vol-B"):
        source = next(v for v in archive["volumes"] if v["id"] == dedicated)
        shared["pages"].extend(source["pages"])
    archive["volumes"].append(shared)
    result = audit_handoff(archive, build_manifest())
    assert result.feasible
    assert result.total_cost == 1
    assert result.selected_volumes == ("vol-shared",)


def test_trimmed_receipt_reports_same_second_radar_gap_for_both_areas() -> None:
    trimmed = {
        "volumes": [
            volume
            for volume in build_archive(trim_radar_receipt=True)["volumes"]
            if volume["id"] in ("vol-X", "vol-R")
        ]
    }
    result = audit_handoff(trimmed, build_manifest())
    assert not result.feasible
    gaps_by_id: dict[str, list] = {}
    for requirement in result.requirements:
        gaps_by_id[requirement.requirement.id] = requirement.gaps
        if requirement.requirement.category.name == "rgb":
            assert requirement.covered
            assert requirement.selected_volume == "vol-X"
    for area in ("A", "B"):
        radar_gaps = gaps_by_id[f"{area}:radar"]
        assert len(radar_gaps) == 1
        gap = radar_gaps[0]
        assert gap.positions == (1, 1)
        assert "10:00:00Z" in gap.interval.low.to_string()
        assert gap.area == area and gap.category == "radar"


def test_empty_page_chain_is_replayed_via_replacement_link() -> None:
    archive = Archive.from_dict(build_archive())
    volume = archive.volume("vol-R")
    manifest = Manifest.from_dict(build_manifest())
    area = manifest.areas[0]
    chain = replay_area_chains(volume, area, manifest.root_request(area))
    page_ids = {page.id for page in chain.pages}
    assert f"page-R-{area.id}-empty" in page_ids
    assert f"page-R-{area.id}-radar" in page_ids


def test_dedup_by_collection_and_id_and_ordering() -> None:
    archive = build_archive()
    # Duplicate the rgb item on a second page of X with identical identity.
    x_volume = next(v for v in archive["volumes"] if v["id"] == "vol-X")
    root_page = next(p for p in x_volume["pages"] if p["id"] == "page-X-A-root")
    x_volume["pages"].append(
        {
            "id": "page-X-A-duplicate",
            "request": root_page["request"],
            "features": [dict(root_page["features"][0])],
            "links": [],
        }
    )
    result = audit_handoff(archive, build_manifest())
    rgb_a = next(
        r for r in result.requirements
        if r.requirement.area.id == "A" and r.requirement.category.name == "rgb"
    )
    assert len(rgb_a.items) == 1
    assert rgb_a.items[0].key.id == "rgb-A"


def test_boundary_touch_counts_as_intersection() -> None:
    manifest = Manifest.from_dict(build_manifest())
    area = manifest.areas[0]
    on_boundary = {
        "type": "Feature",
        "id": "edge",
        "collection": "rgb",
        "geometry": {"type": "Point", "coordinates": [10.0, 10.0]},
        "properties": {"datetime": "2026-01-01T11:00:00Z"},
    }
    assert geometry_intersects_bbox(on_boundary["geometry"], area.bbox)


def test_geometry_filter_rejects_items_outside_area() -> None:
    archive = build_archive()
    # Add an item that matches the category but sits outside both rectangles.
    x_volume = next(v for v in archive["volumes"] if v["id"] == "vol-X")
    root_page = next(p for p in x_volume["pages"] if p["id"] == "page-X-A-root")
    root_page["features"].append(
        {
            "type": "Feature",
            "id": "rgb-elsewhere",
            "collection": "rgb",
            "geometry": {"type": "Point", "coordinates": [45.0, 45.0]},
            "properties": {"datetime": "2026-01-01T12:00:00Z"},
        }
    )
    result = audit_handoff(archive, build_manifest())
    rgb_a = next(
        r for r in result.requirements
        if r.requirement.area.id == "A" and r.requirement.category.name == "rgb"
    )
    assert [record.key.id for record in rgb_a.items] == ["rgb-A"]


def test_fewer_than_k_requires_whole_query_range() -> None:
    archive = build_archive()
    manifest = build_manifest()
    manifest["k"] = 5
    manifest["query"] = {"datetime": "2026-01-01T00:00:00Z/2026-01-02T00:00:00Z"}
    # Existing receipts only sweep back to "min" as a sentinel; they do not
    # prove the whole bounded day range down to the query lower bound, so the
    # handoff cannot be proven for k larger than the evidence.
    result = audit_handoff(archive, manifest)
    assert not result.feasible
    lower = query_time_lower_bound(
        Manifest.from_dict(manifest).root_request(Manifest.from_dict(manifest).areas[0])
    )
    assert lower is not None and lower.dt.day == 1


def test_manifest_requires_two_areas() -> None:
    bad = build_manifest()
    bad["areas"] = bad["areas"][:1]
    with pytest.raises(ValueError):
        Manifest.from_dict(bad)


def test_archive_from_file_roundtrip(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.json"
    archive_path.write_text(json.dumps(build_archive()))
    loaded = Archive.from_file(archive_path)
    assert {volume.id for volume in loaded.volumes} == {
        "vol-A",
        "vol-B",
        "vol-X",
        "vol-R",
    }


def test_receipt_request_must_match_page_request() -> None:
    archive = build_archive()
    r_volume = next(v for v in archive["volumes"] if v["id"] == "vol-R")
    page = next(p for p in r_volume["pages"] if p.get("receipt"))
    page["receipt"]["request"]["body"]["bbox"] = [99, 99, 100, 100]
    with pytest.raises(ValueError):
        Archive.from_dict(archive)


def test_empty_page_receipt_and_next_link_are_both_usable() -> None:
    # An empty page can itself carry a full-sweep receipt; it must still be
    # reached and its receipt counted, and its next link followed.
    search = "https://v.test/search"
    bbox = [0.0, 0.0, 10.0, 10.0]
    archive = {
        "volumes": [
            {
                "id": "vol-empty-receipt",
                "cost": 1,
                "pages": [
                    {
                        "id": "empty-root",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {"bbox": bbox, "collections": ["radar", "rgb"]},
                        },
                        "features": [],
                        "links": [
                            {
                                "rel": "next",
                                "method": "POST",
                                "href": search,
                                "body": {"bbox": bbox, "collections": ["rgb"]},
                            }
                        ],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {
                                    "bbox": bbox,
                                    "collections": ["radar", "rgb"],
                                },
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    },
                    {
                        "id": "rgb-page",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {"bbox": bbox, "collections": ["rgb"]},
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [5, 5]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {"bbox": bbox, "collections": ["rgb"]},
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    },
                ],
            }
        ]
    }
    manifest = {
        "search_url": search,
        "method": "POST",
        "k": 1,
        "areas": [
            {"id": "A", "bbox": bbox},
            {"id": "B", "bbox": [20, 20, 30, 30]},
        ],
        "categories": [
            {"name": "radar", "collections": ["radar"]},
            {"name": "rgb", "collections": ["rgb"]},
        ],
        "query": {},
    }
    # B has no evidence at all, so the handoff is infeasible overall, but A
    # must be proven using the empty-page receipt plus the followed rgb page.
    result = audit_handoff(archive, manifest)
    assert not result.feasible
    a_rgb = next(r for r in result.requirements if r.requirement.id == "A:rgb")
    assert a_rgb.covered and a_rgb.selected_volume == "vol-empty-receipt"
    a_radar = next(r for r in result.requirements if r.requirement.id == "A:radar")
    assert a_radar.covered
    b_rgb = next(r for r in result.requirements if r.requirement.id == "B:rgb")
    assert not b_rgb.covered and b_rgb.gaps


def test_get_request_chain_matches_query_string() -> None:
    search = "https://v.test/search"
    archive = {
        "volumes": [
            {
                "id": "vol-get",
                "cost": 1,
                "pages": [
                    {
                        "id": "get-root",
                        "request": {
                            "method": "GET",
                            "url": (
                                f"{search}?bbox=0.0,0.0,10.0,10.0"
                                "&collections=rgb"
                            ),
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [5, 5]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "GET",
                                "url": (
                                    f"{search}?bbox=0.0,0.0,10.0,10.0"
                                    "&collections=rgb"
                                ),
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    }
                ],
            }
        ]
    }
    manifest = {
        "search_url": search,
        "method": "GET",
        "k": 1,
        "areas": [
            {"id": "A", "bbox": [0, 0, 10, 10]},
            {"id": "B", "bbox": [20, 20, 30, 30]},
        ],
        "categories": [{"name": "rgb", "collections": ["rgb"]}],
        "query": {},
    }
    result = audit_handoff(archive, manifest)
    # A is provable; B has no volume at all.
    a = next(r for r in result.requirements if r.requirement.area.id == "A")
    assert a.covered
    b = next(r for r in result.requirements if r.requirement.area.id == "B")
    assert not b.covered and b.gaps


def test_antimeridian_area_uses_delivery_rectangle() -> None:
    search = "https://v.test/search"
    crossing = [170.0, -10.0, -170.0, 10.0]
    archive = {
        "volumes": [
            {
                "id": "vol-cross",
                "cost": 1,
                "pages": [
                    {
                        "id": "cross-root",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {"bbox": crossing, "collections": ["rgb"]},
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-180",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [180, 0]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            },
                            {
                                "type": "Feature",
                                "id": "rgb-elsewhere",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [0, 0]},
                                "properties": {"datetime": "2026-01-01T11:00:00Z"},
                            },
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {"bbox": crossing, "collections": ["rgb"]},
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    }
                ],
            }
        ]
    }
    manifest = {
        "search_url": search,
        "method": "POST",
        "k": 1,
        "areas": [
            {"id": "C", "bbox": crossing},
            {"id": "D", "bbox": [20, 20, 30, 30]},
        ],
        "categories": [{"name": "rgb", "collections": ["rgb"]}],
        "query": {},
    }
    result = audit_handoff(archive, manifest)
    c = next(r for r in result.requirements if r.requirement.area.id == "C")
    assert c.covered
    assert [record.key.id for record in c.items] == ["rgb-180"]


def test_fewer_than_k_provable_when_receipts_cover_bounded_range() -> None:
    search = "https://v.test/search"
    bbox = [0.0, 0.0, 10.0, 10.0]
    only_item_key = "2026-01-01T10:00:00Z|rgb|rgb-1"
    archive = {
        "volumes": [
            {
                "id": "vol-bounded",
                "cost": 1,
                "pages": [
                    {
                        "id": "bounded-root",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {
                                "bbox": bbox,
                                "collections": ["rgb"],
                                "datetime": "2026-01-01T00:00:00Z/2026-01-02T00:00:00Z",
                            },
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [5, 5]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {
                                    "bbox": bbox,
                                    "collections": ["rgb"],
                                    "datetime": (
                                        "2026-01-01T00:00:00Z/2026-01-02T00:00:00Z"
                                    ),
                                },
                            },
                            "swept": {
                                "first": "max",
                                "last": "2026-01-01T00:00:00Z||",
                            },
                        },
                    }
                ],
            }
        ]
    }
    manifest = {
        "search_url": search,
        "method": "POST",
        "k": 5,
        "areas": [
            {"id": "A", "bbox": bbox},
            {"id": "B", "bbox": [20, 20, 30, 30]},
        ],
        "categories": [{"name": "rgb", "collections": ["rgb"]}],
        "query": {"datetime": "2026-01-01T00:00:00Z/2026-01-02T00:00:00Z"},
    }
    result = audit_handoff(archive, manifest)
    a = next(r for r in result.requirements if r.requirement.area.id == "A")
    # The single item plus a sweep down to the query lower bound proves the
    # whole agreed range even though fewer than k items exist.
    assert a.covered and len(a.items) == 1
    assert a.cutoff is None
    assert only_item_key.startswith("2026-01-01T10:00:00Z")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_audit_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pystac_client.cli import audit_command

    archive_path = tmp_path / "archive.json"
    manifest_path = tmp_path / "manifest.json"
    archive_path.write_text(json.dumps(build_archive()))
    manifest_path.write_text(json.dumps(build_manifest()))
    exit_code = audit_command(str(archive_path), str(manifest_path))
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["total_cost"] == 5


def test_large_sample_within_fifteen_seconds() -> None:
    import time

    archive_data, manifest_data = build_large_archive()
    page_count = sum(len(volume["pages"]) for volume in archive_data["volumes"])
    item_count = sum(
        len(page["features"])
        for volume in archive_data["volumes"]
        for page in volume["pages"]
    )
    assert len(archive_data["volumes"]) <= 400
    assert page_count <= 4000
    assert item_count <= 20000

    started = time.perf_counter()
    result = audit_handoff(archive_data, manifest_data)
    elapsed = time.perf_counter() - started

    assert result.feasible
    # Every requirement must be covered and the gold volume is the only one
    # holding the globally newest k items, so it is forced into the solution.
    assert result.total_cost == 7
    assert result.selected_volumes == ("vol-gold",)
    assert all(requirement.covered for requirement in result.requirements)
    assert elapsed < 15.0


# ---------------------------------------------------------------------------
# Receipt acceptance security semantics
# ---------------------------------------------------------------------------


def _single_volume_manifest(
    search: str, *, seal_id: str | None = "seal-1"
) -> dict[str, Any]:
    return {
        "search_url": search,
        "method": "POST",
        "seal_id": seal_id,
        "k": 1,
        "areas": [
            {"id": "A", "bbox": [0.0, 0.0, 10.0, 10.0]},
            {"id": "B", "bbox": [20.0, 20.0, 30.0, 30.0]},
        ],
        "categories": [{"name": "rgb", "collections": ["rgb"]}],
        "query": {},
    }


def test_receipt_with_mismatched_area_is_rejected() -> None:
    # The second page is reached by a replacement link that changes the bbox.
    # Its receipt is internally consistent, but it cannot prove the authorized
    # rectangle, so the requirement stays uncovered.
    search = "https://v.test/search"
    authorized_bbox = [0.0, 0.0, 10.0, 10.0]
    foreign_bbox = [90.0, 90.0, 95.0, 95.0]
    archive = {
        "volumes": [
            {
                "id": "vol-wrong-area",
                "cost": 1,
                "pages": [
                    {
                        "id": "root",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {
                                "bbox": authorized_bbox,
                                "collections": ["rgb"],
                            },
                        },
                        "features": [],
                        "links": [
                            {
                                "rel": "next",
                                "method": "POST",
                                "href": search,
                                "body": {
                                    "bbox": foreign_bbox,
                                    "collections": ["rgb"],
                                },
                            }
                        ],
                    },
                    {
                        "id": "foreign-page",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {
                                "bbox": foreign_bbox,
                                "collections": ["rgb"],
                            },
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [92, 92]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {
                                    "bbox": foreign_bbox,
                                    "collections": ["rgb"],
                                },
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    },
                ],
            }
        ]
    }
    result = audit_handoff(archive, _single_volume_manifest(search))
    a = next(r for r in result.requirements if r.requirement.area.id == "A")
    assert not a.covered
    assert a.gaps
    # The foreign item must not be handed over for the authorized area.
    assert a.items == []


def test_item_without_geometry_is_excluded_but_empty_range_can_be_proven() -> None:
    search = "https://v.test/search"
    archive = {
        "volumes": [
            {
                "id": "vol-no-geometry",
                "cost": 1,
                "pages": [
                    {
                        "id": "root",
                        "request": {
                            "method": "POST",
                            "url": search,
                            "body": {
                                "bbox": [0.0, 0.0, 10.0, 10.0],
                                "collections": ["rgb"],
                            },
                        },
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": None,
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            "seal_id": "seal-1",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": {
                                    "bbox": [0.0, 0.0, 10.0, 10.0],
                                    "collections": ["rgb"],
                                },
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    }
                ],
            }
        ]
    }
    result = audit_handoff(archive, _single_volume_manifest(search))
    a = next(r for r in result.requirements if r.requirement.area.id == "A")
    # The geometry-less item cannot establish area membership, so it is never
    # handed over; a full sweep still proves the category range is exhausted.
    assert a.items == []
    assert a.covered
    assert a.gaps == []


def test_receipt_from_another_seal_is_ignored() -> None:
    search = "https://v.test/search"
    body = {"bbox": [0.0, 0.0, 10.0, 10.0], "collections": ["rgb"]}
    archive = {
        "volumes": [
            {
                "id": "vol-foreign-seal",
                "cost": 1,
                "pages": [
                    {
                        "id": "root",
                        "request": {"method": "POST", "url": search, "body": body},
                        "features": [
                            {
                                "type": "Feature",
                                "id": "rgb-1",
                                "collection": "rgb",
                                "geometry": {"type": "Point", "coordinates": [5, 5]},
                                "properties": {"datetime": "2026-01-01T10:00:00Z"},
                            }
                        ],
                        "links": [],
                        "receipt": {
                            # The manifest only trusts seal-1, so this receipt
                            # must not count even though its request matches.
                            "seal_id": "seal-other",
                            "request": {
                                "method": "POST",
                                "url": search,
                                "body": body,
                            },
                            "swept": {"first": "max", "last": "min"},
                        },
                    }
                ],
            }
        ]
    }
    result = audit_handoff(archive, _single_volume_manifest(search, seal_id="seal-1"))
    a = next(r for r in result.requirements if r.requirement.area.id == "A")
    assert not a.covered
    assert a.gaps


def test_area_cutoffs_may_fall_on_different_days() -> None:
    search = "https://v.test/search"

    def rgb_page(area_id, bbox, point, when, link=None):
        body = {"bbox": bbox, "collections": ["rgb"]}
        return {
            "id": f"page-{area_id}",
            "request": {"method": "POST", "url": search, "body": body},
            "features": [
                {
                    "type": "Feature",
                    "id": f"rgb-{area_id}",
                    "collection": "rgb",
                    "geometry": {"type": "Point", "coordinates": point},
                    "properties": {"datetime": when},
                }
            ],
            "links": link or [],
            "receipt": {
                "seal_id": "seal-1",
                "request": {"method": "POST", "url": search, "body": body},
                "swept": {"first": "max", "last": "min"},
            },
        }

    # One shared volume serves both areas; the newest A item is one day newer.
    archive = {
        "volumes": [
            {
                "id": "vol-cross-day",
                "cost": 2,
                "days": ["2026-03-01", "2026-03-02"],
                "pages": [
                    rgb_page("A", [0.0, 0.0, 10.0, 10.0], [5, 5],
                             "2026-03-02T08:00:00Z"),
                    rgb_page("B", [20.0, 20.0, 30.0, 30.0], [25, 25],
                             "2026-03-01T20:00:00Z"),
                ],
            }
        ]
    }
    manifest = {
        "search_url": search,
        "method": "POST",
        "seal_id": "seal-1",
        "k": 1,
        "areas": [
            {"id": "A", "bbox": [0.0, 0.0, 10.0, 10.0]},
            {"id": "B", "bbox": [20.0, 20.0, 30.0, 30.0]},
        ],
        "categories": [{"name": "rgb", "collections": ["rgb"]}],
        "query": {},
    }
    result = audit_handoff(archive, manifest)
    assert result.feasible
    assert result.total_cost == 2 and result.selected_volumes == ("vol-cross-day",)
    by_area = {r.requirement.area.id: r for r in result.requirements}
    assert "2026-03-02" in by_area["A"].cutoff.to_string()
    assert "2026-03-01" in by_area["B"].cutoff.to_string()
