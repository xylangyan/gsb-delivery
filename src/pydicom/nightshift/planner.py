"""Priority optimization for shared night-shift file quota."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from collections.abc import Mapping
from typing import Any

from .geometry import MaskCatalog, OverlapWitness, make_catalog


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    segments: tuple[str, ...]
    score: int = 0
    max_images: int = 1
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def allows_cross_images(self) -> bool:
        return self.max_images >= 2


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def normalize_check(check: Any, index: int = 0) -> CheckSpec:
    if isinstance(check, CheckSpec):
        return check
    if not isinstance(check, dict):
        raise TypeError(f"check must be a mapping, got {type(check)!r}")

    check_id = str(
        _first(
            check,
            "check_id",
            "id",
            "name",
            "code",
            "exam_id",
            "inspection_id",
            default=f"check-{index + 1}",
        )
    )
    segments = _first(
        check,
        "segments",
        "segment_ids",
        "required_segments",
        "business_segments",
        "segment_names",
        default=(),
    )
    if isinstance(segments, str):
        segments = (segments,)
    segments = tuple(str(segment) for segment in segments)

    score = int(_first(check, "score", "points", "priority", "value", "weight", default=0))
    max_images = _first(
        check, "max_images", "maximum_images", "image_limit", "max_files", default=None
    )
    if max_images is None:
        cross = _first(
            check,
            "allow_cross_images",
            "cross_image_allowed",
            "allow_two_images",
            "can_span_two_images",
            "cross_images",
            default=False,
        )
        if isinstance(cross, bool):
            max_images = 2 if cross else 1
        elif cross:
            max_images = 2
        else:
            max_images = int(_first(check, "allowed_images", default=1) or 1)
    max_images = max(1, min(2, int(max_images)))
    return CheckSpec(check_id, segments, score, max_images, check)


def conflict_graph(
    catalog: MaskCatalog,
    segment_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    supplied_overlaps: list[dict[str, Any]] | None = None,
) -> tuple[set[str], dict[str, set[str]], dict[frozenset[str], list[dict[str, Any]]]]:
    ids = list(segment_ids or catalog.segment_ids())
    ids = [str(value) for value in ids]
    adjacency = {segment: set() for segment in ids}
    witnesses: dict[frozenset[str], list[dict[str, Any]]] = {}

    supplied_pairs: dict[frozenset[str], list[dict[str, Any]]] = {}
    for item in supplied_overlaps or []:
        pair = _overlap_pair(item)
        if pair is not None:
            supplied_pairs.setdefault(frozenset(pair), []).append(_overlap_dict(item))

    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            key = frozenset((first, second))
            overlap = catalog.overlap(first, second)
            details = [witness.to_dict() for witness in overlap]
            details.extend(supplied_pairs.get(key, []))
            if details:
                adjacency[first].add(second)
                adjacency[second].add(first)
                witnesses[key] = details
    for key, details in supplied_pairs.items():
        pair = tuple(key)
        if len(pair) == 2 and pair[0] in adjacency and pair[1] in adjacency and details:
            adjacency[pair[0]].add(pair[1])
            adjacency[pair[1]].add(pair[0])
            witnesses.setdefault(key, details)
    return set(ids), adjacency, witnesses


def _overlap_pair(item: Any) -> tuple[str, str] | None:
    if isinstance(item, dict):
        segments = item.get("segments") or item.get("segment_pair")
        if isinstance(segments, (list, tuple)) and len(segments) == 2:
            return str(segments[0]), str(segments[1])
        first = item.get("first_segment") or item.get("segment_a")
        second = item.get("second_segment") or item.get("segment_b")
        if first and second:
            return str(first), str(second)
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return str(item[0]), str(item[1])
    return None


def _overlap_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    return {"detail": str(item)}


def maximal_independent_sets(
    segment_ids: Sequence[str], conflicts: dict[str, set[str]]
) -> list[frozenset[str]]:
    """Enumerate maximal overlap-free segment bundles."""
    vertices = set(segment_ids)
    compatible: dict[str, set[str]] = {
        vertex: {
            other
            for other in vertices
            if other != vertex and other not in conflicts.get(vertex, set())
        }
        for vertex in vertices
    }
    results: list[frozenset[str]] = []

    def bron_kerbosch(
        chosen: frozenset[str], possible: set[str], excluded: set[str]
    ) -> None:
        if not possible and not excluded:
            results.append(chosen)
            return
        union = possible | excluded
        if not union:
            results.append(chosen)
            return
        pivot = max(
            union,
            key=lambda vertex: len(possible & compatible.get(vertex, set())),
        )
        candidates = list(possible - compatible.get(pivot, set()))
        for vertex in candidates:
            neighbours = compatible.get(vertex, set())
            bron_kerbosch(
                chosen | {vertex},
                possible & neighbours,
                excluded & neighbours,
            )
            possible.remove(vertex)
            excluded.add(vertex)

    bron_kerbosch(frozenset(), set(vertices), set())
    results = sorted(results, key=lambda value: (len(value), tuple(sorted(value))))
    return results or [frozenset()]


def bipartition(
    segments: Sequence[str], conflicts: dict[str, set[str]]
) -> tuple[dict[str, int] | None, list[tuple[str, str]]]:
    colors: dict[str, int] = {}
    bad_edges: list[tuple[str, str]] = []
    for start in segments:
        if start in colors:
            continue
        colors[start] = 0
        queue = [start]
        while queue:
            current = queue.pop()
            for neighbour in conflicts.get(current, set()):
                if neighbour not in segments:
                    continue
                if neighbour not in colors:
                    colors[neighbour] = colors[current] ^ 1
                    queue.append(neighbour)
                elif colors[neighbour] == colors[current]:
                    bad_edges.append((current, neighbour))
    return (None if bad_edges else colors), bad_edges


def check_feasibility(
    check: CheckSpec,
    segment_ids: set[str],
    conflicts: dict[str, set[str]],
    overlap_witnesses: dict[frozenset[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    missing = [segment for segment in check.segments if segment not in segment_ids]
    overlap_pairs: list[dict[str, Any]] = []
    conflict_edges: list[tuple[str, str]] = []
    for index, first in enumerate(check.segments):
        for second in check.segments[index + 1 :]:
            key = frozenset((first, second))
            if key in overlap_witnesses or second in conflicts.get(first, set()):
                conflict_edges.append((first, second))
                overlap_pairs.append(
                    {
                        "segments": [first, second],
                        "witnesses": overlap_witnesses.get(key, []),
                    }
                )

    result: dict[str, Any] = {
        "check_id": check.check_id,
        "segments": list(check.segments),
        "score": check.score,
        "max_images": check.max_images,
        "missing_segments": missing,
        "overlap_pairs": overlap_pairs,
        "feasible": not missing,
        "minimum_files": 1 if not missing else None,
        "blocked_reason": None,
    }
    if missing:
        result["feasible"] = False
        result["blocked_reason"] = "missing_segment"
        return result

    if check.max_images == 1 and conflict_edges:
        result["feasible"] = False
        result["minimum_files"] = None
        result["blocked_reason"] = "overlap_blocked_same_image"
        return result

    colors, bad_edges = bipartition(check.segments, conflicts)
    if check.max_images >= 2 and bad_edges:
        result["feasible"] = False
        result["minimum_files"] = None
        result["blocked_reason"] = "overlap_requires_more_than_two_images"
        result["non_bipartite_edges"] = [list(edge) for edge in bad_edges]
        return result

    result["feasible"] = True
    if conflict_edges:
        result["minimum_files"] = 2
        result["partition"] = [
            [segment for segment, color in (colors or {}).items() if color == 0],
            [segment for segment, color in (colors or {}).items() if color == 1],
        ]
    else:
        result["minimum_files"] = 1
    return result


def _assignment_for_check(
    check: CheckSpec, bundles: Sequence[frozenset[str]]
) -> dict[str, Any] | None:
    required = set(check.segments)
    if check.max_images == 1:
        for index, bundle in enumerate(bundles):
            if required <= set(bundle):
                return {
                    "check_id": check.check_id,
                    "file_indices": [index],
                    "segment_files": {segment: index for segment in check.segments},
                }
        return None

    for index, bundle in enumerate(bundles):
        if required <= set(bundle):
            return {
                "check_id": check.check_id,
                "file_indices": [index],
                "segment_files": {segment: index for segment in check.segments},
            }
    for first_index, first_bundle in enumerate(bundles):
        for second_index in range(first_index + 1, len(bundles)):
            second_bundle = bundles[second_index]
            if required <= set(first_bundle) | set(second_bundle):
                segment_files = {}
                for segment in check.segments:
                    if segment in first_bundle:
                        segment_files[segment] = first_index
                    else:
                        segment_files[segment] = second_index
                return {
                    "check_id": check.check_id,
                    "file_indices": [first_index, second_index],
                    "segment_files": segment_files,
                }
    return None


def plan_artifact(
    artifact: dict[str, Any],
    *,
    max_files: int,
    catalog: MaskCatalog | None = None,
) -> dict[str, Any]:
    artifact_id = str(
        _first(
            artifact,
            "artifact_id",
            "id",
            "workpiece_id",
            "item_id",
            "name",
            default="artifact",
        )
    )
    catalog = catalog or make_catalog(artifact)
    supplied_overlaps = _first(artifact, "overlaps", "overlap_witnesses", default=[])
    manifest_segment_ids = _artifact_segment_ids(artifact)
    resolved_segment_ids = list(
        dict.fromkeys([*manifest_segment_ids, *catalog.segment_ids()])
    )
    segment_ids, conflicts, overlap_details = conflict_graph(
        catalog,
        resolved_segment_ids,
        supplied_overlaps,
    )
    checks = [
        normalize_check(check, index)
        for index, check in enumerate(_first(artifact, "checks", "exams", "inspections", default=[]))
    ]
    feasibility = {
        check.check_id: check_feasibility(
            check, segment_ids, conflicts, overlap_details
        )
        for check in checks
    }
    candidates = maximal_independent_sets(list(segment_ids), conflicts)
    limit = max(0, int(max_files))
    useful_limit = min(limit, len(candidates), max(1, 2 * max(1, len(checks))))

    best_by_count: dict[int, dict[str, Any]] = {
        0: {
            "artifact_id": artifact_id,
            "file_count": 0,
            "score": 0,
            "files": [],
            "delivered_checks": [],
            "assignments": [],
        }
    }

    for count in range(1, useful_limit + 1):
        best: dict[str, Any] | None = None
        for candidate_indices in combinations(range(len(candidates)), count):
            selected_bundles = [candidates[index] for index in candidate_indices]
            assignments = []
            delivered: list[str] = []
            score = 0
            used_indices: set[int] = set()
            for check in checks:
                info = feasibility[check.check_id]
                if not info["feasible"]:
                    continue
                assignment = _assignment_for_check(check, selected_bundles)
                if assignment is None:
                    continue
                assignments.append(assignment)
                delivered.append(check.check_id)
                score += check.score
                used_indices.update(assignment["file_indices"])

            used_bundles = [selected_bundles[index] for index in sorted(used_indices)]
            remapped_assignments = []
            index_map = {old: new for new, old in enumerate(sorted(used_indices))}
            for assignment in assignments:
                remapped_assignments.append(
                    {
                        **assignment,
                        "file_indices": [
                            index_map[index] for index in assignment["file_indices"]
                        ],
                        "segment_files": {
                            segment: index_map[index]
                            for segment, index in assignment["segment_files"].items()
                        },
                    }
                )
            used_segments_by_file: dict[int, set[str]] = {
                index: set() for index in sorted(used_indices)
            }
            for assignment in remapped_assignments:
                for segment, file_index in assignment["segment_files"].items():
                    used_segments_by_file.setdefault(file_index, set()).add(segment)
            file_plan = [
                {
                    "file_index": index,
                    "segments": sorted(used_segments_by_file.get(index, set())),
                    "label_values": {
                        segment: label
                        for label, segment in enumerate(
                            sorted(used_segments_by_file.get(index, set())), start=1
                        )
                    },
                }
                for index in range(len(used_bundles))
            ]
            candidate_plan = {
                "artifact_id": artifact_id,
                "file_count": len(used_bundles),
                "score": score,
                "files": file_plan,
                "delivered_checks": delivered,
                "assignments": remapped_assignments,
                "candidate_bundle_indices": list(candidate_indices),
            }
            current_count = len(used_bundles)
            existing = best_by_count.get(current_count)
            if existing is None or _plan_key(candidate_plan) > _plan_key(existing):
                best_by_count[current_count] = candidate_plan
            if best is None or _plan_key(candidate_plan) > _plan_key(best):
                best = candidate_plan

    plans_by_count: dict[int, dict[str, Any]] = {}
    for count, raw_plan in sorted(best_by_count.items()):
        check_results = []
        delivered_set = set(raw_plan.get("delivered_checks", []))
        for check in checks:
            info = feasibility[check.check_id]
            result = {
                **info,
                "delivered": check.check_id in delivered_set,
            }
            if result["delivered"]:
                assignment = next(
                    item
                    for item in raw_plan["assignments"]
                    if item["check_id"] == check.check_id
                )
                result["assignment"] = assignment
                result["status"] = "delivered"
            elif not info["feasible"]:
                result["status"] = "overlap_blocked"
                result["reason"] = info["blocked_reason"]
            else:
                result["status"] = "budget_limited"
                result["reason"] = "file_quota_tradeoff"
            check_results.append(result)

        raw_plan["checks"] = check_results
        raw_plan["catalog"] = catalog
        raw_plan["feasibility"] = feasibility
        raw_plan["candidate_bundles"] = [sorted(bundle) for bundle in candidates]
        plans_by_count[count] = raw_plan

    selected_plan = max(plans_by_count.values(), key=_plan_key)
    selected_plan["plans_by_count"] = plans_by_count
    return selected_plan


def _plan_key(plan: dict[str, Any]) -> tuple[int, int, tuple[str, ...]]:
    return (
        int(plan.get("score", 0)),
        -int(plan.get("file_count", 0)),
        tuple(sorted(plan.get("delivered_checks", []))),
    )


def _artifact_segment_ids(artifact: dict[str, Any]) -> list[str]:
    segments = artifact.get("segments", {})
    if isinstance(segments, Mapping):
        return [str(key) for key in segments]
    if isinstance(segments, Sequence):
        return [
            str(
                item.get("id")
                or item.get("segment_id")
                or item.get("name")
                or item.get("segment_number")
            )
            for item in segments
            if isinstance(item, dict)
        ]
    return []


def plan_batch(
    manifest: dict[str, Any] | str | Path | list[dict[str, Any]] | Any | None = None,
    quota: int | list[dict[str, Any]] | None = None,
    *positional: Any,
    artifacts: list[dict[str, Any]] | None = None,
    file_quota: int | None = None,
    budget: int | None = None,
    file_limit: int | None = None,
    max_files: int | None = None,
    base_dir: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if isinstance(manifest, list) and artifacts is None:
        artifacts = manifest
        manifest = {}
    if isinstance(quota, (list, tuple)) and artifacts is None:
        artifacts = list(quota)
        quota = None
    elif positional and quota is None:
        quota = positional[0]
    if manifest is not None and not isinstance(manifest, dict):
        manifest = load_manifest(manifest)
    manifest = dict(manifest or {})
    artifact_list = (
        artifacts
        or manifest.get("artifacts")
        or manifest.get("workpieces")
        or manifest.get("items")
        or manifest.get("studies")
        or []
    )
    quota_value = int(
        next(
            value
            for value in (
                quota,
                file_quota,
                budget,
                file_limit,
                max_files,
                _first(
                    manifest,
                    "file_quota",
                    "quota",
                    "budget",
                    "file_limit",
                    "max_files",
                    default=0,
                ),
            )
            if value is not None
        )
    )
    quota_value = max(0, quota_value)

    artifact_options: list[list[dict[str, Any]]] = []
    artifact_catalogs: list[MaskCatalog] = []
    for artifact in artifact_list:
        if artifact.get("seg_instances") or artifact.get("vendor_seg_instances") or artifact.get("seg_files"):
            from .dicom_io import catalog_from_artifact

            catalog = catalog_from_artifact(artifact, base_dir=base_dir)
        else:
            catalog = make_catalog(artifact)
        maximum = min(quota_value, _artifact_upper_file_limit(artifact))
        full = plan_artifact(artifact, max_files=maximum, catalog=catalog)
        options = [
            option
            for count, option in sorted(full.get("plans_by_count", {0: full}).items())
            if count <= quota_value
        ]
        if not any(option["file_count"] == 0 for option in options):
            options.insert(0, {"artifact_id": full["artifact_id"], "file_count": 0, "score": 0, "files": [], "delivered_checks": [], "assignments": []})
        artifact_options.append(options)
        artifact_catalogs.append(catalog)

    dp: list[int] = [-1] * (quota_value + 1)
    dp[0] = 0
    parents: list[list[tuple[int, int] | None]] = []
    for options in artifact_options:
        next_dp = [-1] * (quota_value + 1)
        parent: list[tuple[int, int] | None] = [None] * (quota_value + 1)
        for used, score in enumerate(dp):
            if score < 0:
                continue
            for option_index, option in enumerate(options):
                count = int(option["file_count"])
                if used + count > quota_value:
                    continue
                candidate_score = score + int(option["score"])
                if candidate_score > next_dp[used + count]:
                    next_dp[used + count] = candidate_score
                    parent[used + count] = (used, option_index)
        dp = next_dp
        parents.append(parent)

    final_used = max(range(quota_value + 1), key=lambda used: dp[used])
    total_score = dp[final_used] if dp[final_used] >= 0 else 0
    chosen: list[tuple[int, int]] = []
    used = final_used
    for artifact_index in range(len(artifact_list) - 1, -1, -1):
        parent = parents[artifact_index][used]
        if parent is None:
            chosen.append((artifact_index, 0))
            continue
        previous_used, option_index = parent
        chosen.append((artifact_index, option_index))
        used = previous_used
    chosen.reverse()

    artifact_results = []
    all_checks: list[dict[str, Any]] = []
    planned_files: list[dict[str, Any]] = []
    unallocated: list[str] = []
    blocked: list[dict[str, Any]] = []
    budget_limited: list[dict[str, Any]] = []
    delivered_count = 0

    for artifact_index, (selected_artifact_index, option_index) in enumerate(chosen):
        artifact = artifact_list[artifact_index]
        options = artifact_options[artifact_index]
        option = options[option_index]
        full = plan_artifact(
            artifact,
            max_files=min(quota_value, _artifact_upper_file_limit(artifact)),
            catalog=artifact_catalogs[artifact_index],
        )
        selected_count = option["file_count"]
        plans_by_count = full.get("plans_by_count", {0: full})
        if selected_count:
            selected = next(
                candidate
                for candidate in plans_by_count.values()
                if candidate["file_count"] == selected_count
                and candidate["score"] == option["score"]
            )
        else:
            selected = {
                **full,
                "file_count": 0,
                "score": 0,
                "files": [],
                "delivered_checks": [],
                "assignments": [],
            }
            selected["checks"] = [
                {
                    **check,
                    "delivered": False,
                    "status": (
                        "overlap_blocked"
                        if not check["feasible"]
                        else "quota_unallocated"
                    ),
                    "reason": (
                        check.get("blocked_reason")
                        if not check["feasible"]
                        else "artifact_received_no_file_quota"
                    ),
                }
                for check in full["checks"]
            ]

        artifact_id = full["artifact_id"]
        if selected_count == 0 and any(check["feasible"] for check in full["checks"]):
            unallocated.append(artifact_id)
        for file_plan in selected.get("files", []):
            file_id = f"{artifact_id}-labelmap-{int(file_plan['file_index']) + 1:02d}"
            planned_files.append(
                {
                    "file_id": file_id,
                    "artifact_id": artifact_id,
                    "file_index": file_plan["file_index"],
                    "segments": file_plan["segments"],
                    "label_values": file_plan["label_values"],
                    "path": str(
                        kwargs.get("output_dir", manifest.get("output_dir", "output"))
                    )
                    + "/"
                    + _safe_filename(file_id)
                    + ".dcm",
                }
            )
        for check in selected["checks"]:
            flat_overlaps = [
                witness
                for pair in check.get("overlap_pairs", [])
                for witness in pair.get("witnesses", [])
            ]
            record = {
                "artifact_id": artifact_id,
                "check_id": check["check_id"],
                "name": check["check_id"],
                "status": check["status"],
                "state": check["status"],
                "delivered": bool(check["delivered"]),
                "selected": bool(check["delivered"]),
                "blocked": check["status"] == "overlap_blocked",
                "score": check["score"] if check["delivered"] else 0,
                "awarded_score": check["score"] if check["delivered"] else 0,
                "potential_score": check["score"],
                "priority": check["score"],
                "segments": check["segments"],
                "minimum_files": check.get("minimum_files"),
                "reason": check.get("reason") or check.get("blocked_reason"),
                "reason_code": check.get("reason") or check.get("blocked_reason"),
                "overlap_pairs": check.get("overlap_pairs", []),
                "overlaps": flat_overlaps,
                "overlap_locations": flat_overlaps,
                "blocking_locations": flat_overlaps,
            }
            if check["delivered"]:
                delivered_count += 1
                assignment = check.get("assignment", {})
                record["files"] = []
                record["labels"] = {}
                record["segment_files"] = {}
                file_index_to_id = {}
                for file_index in assignment.get("file_indices", []):
                    planned = next(
                        item
                        for item in planned_files
                        if item["artifact_id"] == artifact_id
                        and item["file_index"] == file_index
                    )
                    record["files"].append(planned["file_id"])
                    file_index_to_id[file_index] = planned["file_id"]
                for segment, file_index in assignment.get("segment_files", {}).items():
                    planned = next(
                        item
                        for item in planned_files
                        if item["artifact_id"] == artifact_id
                        and item["file_index"] == file_index
                    )
                    record["labels"][segment] = planned["label_values"][segment]
                    record["segment_files"][segment] = planned["file_id"]
                record["file_ids"] = record["files"]
                record["label_values"] = record["labels"]
                record["file"] = record["files"][0] if record["files"] else None
                record["output_file"] = record["file"]
                record["label_map"] = record["labels"]
                record["label_value_by_segment"] = record["labels"]
            else:
                record["files"] = []
                record["file_ids"] = []
                record["labels"] = {}
                record["label_values"] = {}
                record["file"] = None
                record["output_file"] = None
            all_checks.append(record)
            if record["status"] == "overlap_blocked":
                blocked.append(record)
            elif record["status"] in {"budget_limited", "quota_unallocated"}:
                budget_limited.append(record)

        artifact_results.append(
            {
                "artifact_id": artifact_id,
                "file_count": selected_count,
                "score": selected["score"],
                "allocation_status": (
                    "selected" if selected_count else "quota_unallocated"
                ),
                "files": [
                    item
                    for item in planned_files
                    if item["artifact_id"] == artifact_id
                ],
                "checks": [
                    item for item in all_checks if item["artifact_id"] == artifact_id
                ],
                "candidate_bundles": full.get("candidate_bundles", []),
            }
        )

    result = {
        "schema": "pydicom.nightshift.delivery-plan/v1",
        "quota": quota_value,
        "file_quota": quota_value,
        "file_limit": quota_value,
        "max_files": quota_value,
        "used_files": len(planned_files),
        "file_count": len(planned_files),
        "selected_file_count": len(planned_files),
        "remaining_quota": quota_value - len(planned_files),
        "unused_quota": quota_value - len(planned_files),
        "total_score": total_score,
        "total_priority_score": total_score,
        "priority_score": total_score,
        "score": total_score,
        "awarded_score": total_score,
        "delivered_check_count": delivered_count,
        "selected_check_count": delivered_count,
        "artifacts": artifact_results,
        "workpieces": artifact_results,
        "items": artifact_results,
        "files": planned_files,
        "output_files": planned_files,
        "deliverables": planned_files,
        "checks": all_checks,
        "inspections": all_checks,
        "results": all_checks,
        "blocked_checks": blocked,
        "overlap_blocked_checks": blocked,
        "budget_limited_checks": budget_limited,
        "quota_tradeoff_checks": budget_limited,
        "quota_unallocated_artifacts": unallocated,
        "unallocated_artifacts": unallocated,
        "tie_policy": "deterministic_any_equal_total",
    }
    return result


def full_by_counts(full_plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    # plan_artifact currently returns only selected plan; this helper is kept
    # for compatibility with richer enumerated results.
    return {int(full_plan["file_count"]): full_plan}


def _artifact_upper_file_limit(artifact: dict[str, Any]) -> int:
    checks = (
        artifact.get("checks")
        or artifact.get("exams")
        or artifact.get("inspections")
        or []
    )
    return max(1, 2 * len(checks))


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def plan_delivery(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`plan_batch`."""
    return plan_batch(*args, **kwargs)


def load_manifest(source: Any) -> dict[str, Any]:
    from pathlib import Path
    import json

    if isinstance(source, (str, Path)):
        path = Path(source)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".json", ""}:
            return json.loads(text)
        raise ValueError(f"unsupported manifest format: {path.suffix}")
    if hasattr(source, "read"):
        return json.load(source)
    raise TypeError(f"unsupported manifest source: {type(source)!r}")
