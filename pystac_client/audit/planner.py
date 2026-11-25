"""Plan the minimum-cost sealed volumes that prove a two-area handoff.

The planner turns a delivery manifest (two lon/lat rectangles, the imagery
categories, and ``k``) into one authorized search request per area, replays
the sealed paging chains of every volume from those requests, and then checks
whether a single volume can fully prove each ``(area, category)`` requirement:

* every handed Item is present on an actual reached page,
* Items are deduplicated by ``(collection, id)`` and ordered by UTC time
  (newest first), then collection and id,
* the union of fully-swept receipt intervals reaches from the authorized
  query start down to the per-area, per-category cutoff key.

A volume can prove several requirements while being charged once, so the
final selection is a weighted set-cover over the requirements and always
returns an exact minimum fee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .geometry import geometry_intersects_bbox, normalize_bbox
from .keys import (
    MAX_KEY,
    MIN_KEY,
    KeyInterval,
    SortKey,
    item_sort_key,
    natural_compare,
    parse_datetime,
)
from .replay import (
    SealedRequest,
    request_key,
    requests_match,
    resolve_link,
)
from .vault import Archive, SealedPage, Volume

# Replacing these keys would change area/time/filter semantics; paging links
# inside an authorized chain may only narrow the category (collections/ids).
SEMANTIC_KEYS = (
    "bbox",
    "intersects",
    "datetime",
    "query",
    "filter",
    "filter-lang",
    "q",
)


@dataclass(frozen=True)
class AreaSpec:
    id: str
    bbox: tuple[float, float, float, float]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AreaSpec":
        return cls(id=str(data["id"]), bbox=normalize_bbox(data["bbox"]))


@dataclass(frozen=True)
class CategorySpec:
    name: str
    collections: frozenset[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CategorySpec":
        collections = data.get("collections") or ()
        if isinstance(collections, str):
            collections = [part for part in collections.split(",") if part]
        return cls(name=str(data["name"]), collections=frozenset(map(str, collections)))

    def matches_item(self, collection_id: str) -> bool:
        return not self.collections or collection_id in self.collections

    def in_scope(self, request_collections: frozenset[str]) -> bool:
        if not request_collections:
            return True
        return self.collections.issubset(request_collections)


@dataclass(frozen=True)
class Requirement:
    area: AreaSpec
    category: CategorySpec

    @property
    def id(self) -> str:
        return f"{self.area.id}:{self.category.name}"


@dataclass(frozen=True)
class Manifest:
    search_url: str
    method: str
    areas: tuple[AreaSpec, ...]
    categories: tuple[CategorySpec, ...]
    k: int
    base_query: dict[str, Any]
    seal_id: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        method = str(data.get("method", "POST")).upper()
        base_query = dict(data.get("query") or {})
        categories = tuple(
            CategorySpec.from_dict(item) for item in data.get("categories", [])
        )
        k = int(data.get("k", 1))
        if k < 1:
            raise ValueError("k must be a positive integer")
        if len(data.get("areas", [])) != 2:
            raise ValueError("a handoff manifest must name exactly two areas")
        return cls(
            search_url=str(data["search_url"]),
            method=method,
            areas=tuple(AreaSpec.from_dict(item) for item in data["areas"]),
            categories=categories,
            k=k,
            base_query=base_query,
            seal_id=str(data["seal_id"]) if data.get("seal_id") else None,
        )

    def requirements(self) -> list[Requirement]:
        return [
            Requirement(area=area, category=category)
            for area in self.areas
            for category in self.categories
        ]

    def root_request(self, area: AreaSpec) -> SealedRequest:
        parameters: dict[str, Any] = {
            key: value for key, value in self.base_query.items() if value is not None
        }
        parameters["bbox"] = list(area.bbox)
        all_collections = sorted(
            {
                collection
                for category in self.categories
                for collection in category.collections
            }
        )
        if all_collections:
            parameters["collections"] = all_collections
        if self.method == "GET":
            return SealedRequest(
                url=_get_url(self.search_url, parameters),
                method="GET",
                parameters={},
            )
        return SealedRequest(
            url=self.search_url, method=self.method, parameters=parameters
        )


def _get_url(url: str, parameters: dict[str, Any]) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit

    query = dict(parameters)
    if "bbox" in query:
        query["bbox"] = ",".join(str(value) for value in query["bbox"])
    if "collections" in query:
        query["collections"] = ",".join(query["collections"])
    parts = urlsplit(url)
    encoded = urlencode(query, doseq=True)
    full_query = f"{parts.query}&{encoded}" if parts.query else encoded
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, full_query, parts.fragment)
    )


def request_payload(request: SealedRequest) -> dict[str, Any]:
    if request.method == "GET":
        return {
            key: values[0] if len(values) == 1 else values
            for key, values in parse_qs(
                urlsplit(request.url).query, keep_blank_values=True
            ).items()
        }
    return dict(request.parameters)


def request_collections(request: SealedRequest) -> frozenset[str]:
    payload = request_payload(request)
    raw = payload.get("collections")
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        values = raw.split(",")
    else:
        values = list(raw)
    return frozenset(str(value) for value in values if value)


def query_time_lower_bound(root: SealedRequest) -> SortKey | None:
    """Oldest sort key that the authorized query can return, if bounded."""
    payload = request_payload(root)
    datetime_value = payload.get("datetime")
    if not datetime_value or "/" not in str(datetime_value):
        return None
    start_text = str(datetime_value).split("/", 1)[0]
    if not start_text or start_text == "..":
        return None
    return SortKey(parse_datetime(start_text), "", "")


def receipt_applies(
    receipt_request: SealedRequest,
    root: SealedRequest,
    category: CategorySpec,
) -> bool:
    """Whether a receipt can prove part of an authorized requirement.

    The receipt must stay inside the authorized area/time/filter semantics
    (paging links may only narrow the category scope), and the category must
    be part of the request's collection scope.
    """
    if not category.in_scope(request_collections(receipt_request)):
        return False
    receipt_payload = request_payload(receipt_request)
    root_payload = request_payload(root)
    for key in SEMANTIC_KEYS:
        if not _semantic_equal(key, receipt_payload.get(key), root_payload.get(key)):
            return False
    return True


def _semantic_equal(key: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if key == "bbox":
        return _numeric_list(left) == _numeric_list(right)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return sorted(map(str, left)) == sorted(map(str, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left == right
    return str(left) == str(right)


def _numeric_list(value: Any) -> list[float]:
    if isinstance(value, str):
        return [float(part) for part in value.split(",")]
    return [float(part) for part in value]


@dataclass
class ReachedChain:
    """Pages reachable from an authorized area request inside one volume."""

    area: AreaSpec
    root: SealedRequest
    pages: list[SealedPage]

    def receipts_for(
        self, root: SealedRequest, category: CategorySpec, seal_id: str | None
    ) -> tuple[list[KeyInterval], list[str]]:
        intervals: list[KeyInterval] = []
        page_ids: list[str] = []
        for page in self.pages:
            if page.receipt is None:
                continue
            if seal_id is not None and page.receipt.seal_id != seal_id:
                continue
            if not receipt_applies(page.receipt.request, root, category):
                continue
            intervals.append(page.receipt.swept)
            page_ids.append(page.id)
        return intervals, page_ids


def replay_area_chains(
    volume: Volume, area: AreaSpec, root: SealedRequest
) -> ReachedChain:
    # Index the volume's pages by request so link hops are constant time even
    # when different pages share the same URL with different bodies.
    request_index: dict[tuple[str, str, str], SealedPage] = {}
    for page in volume.pages:
        request_index.setdefault(request_key(page.request), page)

    pages: list[SealedPage] = []
    seen_page_ids: set[str] = set()
    start_pages = [
        page for page in volume.pages if requests_match(page.request, root)
    ]
    for start in start_pages:
        current_request = root
        current = start
        while current.id not in seen_page_ids:
            seen_page_ids.add(current.id)
            pages.append(current)
            link = current.next_link()
            if link is None:
                break
            current_request = resolve_link(link, current_request)
            current = request_index.get(request_key(current_request))
            if current is None:
                break
    return ReachedChain(area=area, root=root, pages=pages)


def merge_intervals(intervals: list[KeyInterval]) -> list[KeyInterval]:
    """Union inclusive key intervals; touching intervals join (closed range)."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda interval: interval.low)
    merged = [ordered[0]]
    for interval in ordered[1:]:
        current = merged[-1]
        if natural_compare(interval.low, current.high) <= 0:
            if natural_compare(interval.high, current.high) > 0:
                merged[-1] = KeyInterval(current.low, interval.high)
        else:
            merged.append(interval)
    return merged


def interval_union_contains(
    intervals: list[KeyInterval], target: KeyInterval
) -> bool:
    return not uncovered_segments(intervals, target)


def uncovered_segments(
    intervals: list[KeyInterval], target: KeyInterval
) -> list[KeyInterval]:
    merged = merge_intervals(intervals)
    cursor = target.low
    gaps: list[KeyInterval] = []
    for interval in merged:
        if natural_compare(interval.high, cursor) < 0:
            continue
        if natural_compare(interval.low, cursor) > 0:
            high = (
                interval.low
                if natural_compare(interval.low, target.high) <= 0
                else target.high
            )
            gaps.append(KeyInterval(cursor, high))
            if natural_compare(high, target.high) >= 0:
                return gaps
        if natural_compare(interval.high, cursor) >= 0:
            cursor = interval.high
        if natural_compare(cursor, target.high) >= 0:
            return gaps
    # No receipt ever reached the cursor, so the remaining target range is open.
    gaps.append(KeyInterval(cursor, target.high))
    return gaps


@dataclass(frozen=True)
class ItemRecord:
    key: SortKey
    feature: dict[str, Any]
    page_id: str
    volume_id: str


@dataclass
class RequirementProof:
    requirement: Requirement
    items: list[ItemRecord]
    cutoff: SortKey | None
    target: KeyInterval
    receipt_intervals: list[KeyInterval]
    receipt_pages: list[str]
    covered: bool


@dataclass
class VolumeCapability:
    volume: Volume
    proofs: dict[str, RequirementProof]
    requirement_mask: int

    def covers(self, requirement_id: str) -> bool:
        proof = self.proofs.get(requirement_id)
        return proof is not None and proof.covered


@dataclass(frozen=True)
class Gap:
    area: str
    category: str
    positions: tuple[int, int] | None
    interval: KeyInterval
    detail: str


@dataclass
class RequirementResult:
    requirement: Requirement
    cutoff: SortKey | None
    items: list[ItemRecord]
    selected_volume: str | None
    covered: bool
    receipt_pages: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)


@dataclass
class AuditResult:
    feasible: bool
    total_cost: int
    selected_volumes: tuple[str, ...]
    requirements: list[RequirementResult]
    selections: dict[str, VolumeCapability]
    manifest: Manifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "total_cost": self.total_cost,
            "selected_volumes": list(self.selected_volumes),
            "requirements": [
                {
                    "area": result.requirement.area.id,
                    "category": result.requirement.category.name,
                    "covered": result.covered,
                    "cutoff": result.cutoff.to_string() if result.cutoff else None,
                    "items": [
                        {
                            "collection": record.key.collection,
                            "id": record.key.id,
                            "datetime": record.key.dt.isoformat(),
                            "volume": record.volume_id,
                            "page": record.page_id,
                        }
                        for record in result.items
                    ],
                    "volume": result.selected_volume,
                    "receipt_pages": list(result.receipt_pages),
                    "gaps": [
                        {
                            "area": gap.area,
                            "category": gap.category,
                            "positions": list(gap.positions) if gap.positions else None,
                            "key_range": [
                                gap.interval.low.to_string(),
                                gap.interval.high.to_string(),
                            ],
                            "detail": gap.detail,
                        }
                        for gap in result.gaps
                    ],
                }
                for result in self.requirements
            ],
        }


def _sort_key_key(key: SortKey) -> tuple[Any, ...]:
    return (-key.dt.timestamp(), key.collection, key.id)


def span_interval(first: SortKey, last: SortKey) -> KeyInterval:
    """Natural-order interval covering two catalog endpoints.

    Catalog order is time descending and then id ascending, so two records at
    the same time invert their natural id order.  The sweep target must use
    the natural minimum and maximum of the first and last required keys.
    """
    if natural_compare(first, last) <= 0:
        return KeyInterval(first, last)
    return KeyInterval(last, first)


def _feature_matches(
    feature: dict[str, Any], area: AreaSpec, category: CategorySpec
) -> bool:
    collection_id = str(
        feature.get("collection") or (feature.get("properties") or {}).get("collection")
    )
    if not category.matches_item(collection_id):
        return False
    geometry = feature.get("geometry")
    # Area membership is defined by the Item's actual geometry intersecting
    # the delivery rectangle; an item without geometry cannot establish it.
    return geometry is not None and geometry_intersects_bbox(geometry, area.bbox)


class AuditPlanner:
    def __init__(self, archive: Archive, manifest: Manifest):
        self.archive = archive
        self.manifest = manifest
        self.requirements = manifest.requirements()
        self._roots = {
            area.id: manifest.root_request(area) for area in manifest.areas
        }
        self._chain_cache: dict[tuple[str, str], ReachedChain] = {}
        self._global_items: dict[str, list[ItemRecord]] = {}

    def chain_for(self, volume: Volume, area: AreaSpec) -> ReachedChain:
        cache_key = (volume.id, area.id)
        if cache_key not in self._chain_cache:
            self._chain_cache[cache_key] = replay_area_chains(
                volume, area, self._roots[area.id]
            )
        return self._chain_cache[cache_key]

    def global_items(self, requirement: Requirement) -> list[ItemRecord]:
        """All evidence for a requirement across the archive, deduped."""
        if requirement.id in self._global_items:
            return self._global_items[requirement.id]
        deduped: dict[tuple[str, str], ItemRecord] = {}
        for volume in self.archive.volumes:
            chain = self.chain_for(volume, requirement.area)
            for page in chain.pages:
                for feature in page.features:
                    if not _feature_matches(
                        feature, requirement.area, requirement.category
                    ):
                        continue
                    key = item_sort_key(feature)
                    deduped.setdefault(
                        (key.collection, key.id),
                        ItemRecord(
                            key=key,
                            feature=feature,
                            page_id=page.id,
                            volume_id=volume.id,
                        ),
                    )
        ordered = sorted(deduped.values(), key=lambda record: _sort_key_key(record.key))
        self._global_items[requirement.id] = ordered
        return ordered

    def evaluate_volume(self, volume: Volume) -> VolumeCapability:
        proofs: dict[str, RequirementProof] = {}
        mask = 0
        for bit, requirement in enumerate(self.requirements):
            proof = self._evaluate(volume, requirement)
            proofs[requirement.id] = proof
            if proof.covered:
                mask |= 1 << bit
        return VolumeCapability(volume=volume, proofs=proofs, requirement_mask=mask)

    def _evaluate(
        self, volume: Volume, requirement: Requirement
    ) -> RequirementProof:
        root = self._roots[requirement.area.id]
        chain = self.chain_for(volume, requirement.area)
        records: dict[tuple[str, str], ItemRecord] = {}
        for page in chain.pages:
            for feature in page.features:
                if not _feature_matches(
                    feature, requirement.area, requirement.category
                ):
                    continue
                key = item_sort_key(feature)
                records.setdefault(
                    (key.collection, key.id),
                    ItemRecord(
                        key=key,
                        feature=feature,
                        page_id=page.id,
                        volume_id=volume.id,
                    ),
                )
        volume_items = sorted(
            records.values(), key=lambda record: _sort_key_key(record.key)
        )

        global_items = self.global_items(requirement)
        wanted = global_items[: self.manifest.k]
        wanted_ids = {(record.key.collection, record.key.id) for record in wanted}
        volume_ids = {
            (record.key.collection, record.key.id) for record in volume_items
        }
        has_items = wanted_ids.issubset(volume_ids)

        if len(global_items) >= self.manifest.k:
            cutoff: SortKey | None = wanted[-1].key
            target = span_interval(wanted[0].key, wanted[-1].key)
        else:
            # Fewer than k items exist: the whole agreed query range must be
            # swept, down to the query's own time lower bound when bounded.
            cutoff = None
            lower = query_time_lower_bound(root) or MIN_KEY
            target = KeyInterval(
                lower, global_items[0].key if global_items else MAX_KEY
            )

        intervals, receipt_pages = chain.receipts_for(
            root, requirement.category, self.manifest.seal_id
        )
        merged = merge_intervals(intervals)
        swept = interval_union_contains(merged, target)
        return RequirementProof(
            requirement=requirement,
            items=volume_items,
            cutoff=cutoff,
            target=target,
            receipt_intervals=merged,
            receipt_pages=receipt_pages,
            covered=has_items and swept,
        )

    def plan(self) -> AuditResult:
        capabilities = [self.evaluate_volume(volume) for volume in self.archive.volumes]
        full_mask = (1 << len(self.requirements)) - 1

        best: dict[int, tuple[int, list[VolumeCapability]]] = {0: (0, [])}
        for capability in capabilities:
            if capability.requirement_mask == 0:
                continue
            for covered_mask, (cost, chosen) in list(best.items()):
                new_mask = covered_mask | capability.requirement_mask
                new_cost = cost + capability.volume.cost
                existing = best.get(new_mask)
                if existing is None or new_cost < existing[0]:
                    best[new_mask] = (new_cost, [*chosen, capability])

        feasible = full_mask in best
        if feasible:
            _cost, chosen = best[full_mask]
            selected = {capability.volume.id: capability for capability in chosen}
            total_cost = sum(capability.volume.cost for capability in chosen)
            selected_ids = tuple(capability.volume.id for capability in chosen)
        else:
            selected = {}
            total_cost = 0
            selected_ids = ()

        # Even without a complete handoff set, name the cheapest volume that
        # proves each requirement on its own so the report separates proven
        # requirements from the ones with outstanding key-range gaps.
        best_single: dict[str, VolumeCapability] = {}
        for requirement in self.requirements:
            candidates = [
                capability
                for capability in capabilities
                if capability.covers(requirement.id)
            ]
            if candidates:
                best_single[requirement.id] = min(
                    candidates, key=lambda capability: capability.volume.cost
                )

        results: list[RequirementResult] = []
        for requirement in self.requirements:
            capability = next(
                (
                    cap
                    for cap in selected.values()
                    if cap.covers(requirement.id)
                ),
                None,
            )
            if capability is None and not feasible:
                capability = best_single.get(requirement.id)
            global_items = self.global_items(requirement)
            wanted = global_items[: self.manifest.k]
            gaps: list[Gap] = []
            if capability is None or not capability.covers(requirement.id):
                all_intervals: list[KeyInterval] = []
                for candidate in capabilities:
                    all_intervals.extend(
                        candidate.proofs[requirement.id].receipt_intervals
                    )
                if len(global_items) >= self.manifest.k:
                    target = span_interval(wanted[0].key, wanted[-1].key)
                else:
                    lower = query_time_lower_bound(
                        self._roots[requirement.area.id]
                    ) or MIN_KEY
                    target = KeyInterval(
                        lower, global_items[0].key if global_items else MAX_KEY
                    )
                for segment in uncovered_segments(
                    merge_intervals(all_intervals), target
                ):
                    gaps.append(
                        Gap(
                            area=requirement.area.id,
                            category=requirement.category.name,
                            positions=_positions_for(segment, global_items),
                            interval=segment,
                            detail=(
                                "no volume fully sweeps this range from the "
                                "authorized query start; earlier imagery may "
                                "remain hidden here"
                            ),
                        )
                    )
            proof = capability.proofs[requirement.id] if capability else None
            if proof is not None:
                wanted_keys = {
                    (item.key.collection, item.key.id) for item in wanted
                }
                wanted_records = [
                    record
                    for record in proof.items
                    if (record.key.collection, record.key.id) in wanted_keys
                ]
                wanted_records.sort(key=lambda record: _sort_key_key(record.key))
                receipt_pages = list(proof.receipt_pages)
                is_covered = capability is not None and capability.covers(
                    requirement.id
                )
            else:
                wanted_records = wanted
                receipt_pages = []
                is_covered = False
            results.append(
                RequirementResult(
                    requirement=requirement,
                    cutoff=(
                        wanted[-1].key if len(global_items) >= self.manifest.k else None
                    ),
                    items=wanted_records,
                    selected_volume=capability.volume.id if capability else None,
                    covered=is_covered,
                    receipt_pages=receipt_pages,
                    gaps=gaps,
                )
            )

        return AuditResult(
            feasible=feasible,
            total_cost=total_cost,
            selected_volumes=selected_ids,
            requirements=results,
            selections=selected,
            manifest=self.manifest,
        )


def _positions_for(
    segment: KeyInterval, items: list[ItemRecord]
) -> tuple[int, int] | None:
    inside = [
        index + 1
        for index, record in enumerate(items)
        if segment.contains(record.key)
    ]
    if not inside:
        return None
    return (inside[0], inside[-1])


def run_audit(archive: Archive, manifest: Manifest) -> AuditResult:
    return AuditPlanner(archive, manifest).plan()
