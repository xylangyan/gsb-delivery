"""Optimize ordered archive revisions for PClass and PRecord entry points.

The planner keeps every required sign-off, minimizes delivered revisions, and
verifies the resulting commit boundaries with native pyrsistent evolvers.
"""

from __future__ import annotations

import argparse
import importlib
import json
from bisect import bisect_left
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from pyrsistent import PClass, PRecord, field
from pyrsistent._pmap import pmap
from pyrsistent._precord import _PRecordEvolver


_TYPE_NAMES = {
    "Any": object,
    "any": object,
    "object": object,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "str": str,
    "string": str,
    "bool": bool,
    "boolean": bool,
    "dict": dict,
    "list": list,
}

_RAW_TYPE_ALIASES = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "null": "NoneType",
    "missing": "MissingType",
}


class ManifestError(ValueError):
    """Raised when the handoff manifest itself is inconsistent."""


def _resolve_callable(reference: str) -> Callable[..., Any]:
    if ":" in reference:
        module_name, attr = reference.split(":", 1)
    elif "." in reference:
        module_name, attr = reference.rsplit(".", 1)
    else:
        if reference in {"int", "str", "float", "bool", "dict", "list"}:
            return __builtins__[reference] if isinstance(__builtins__, dict) else getattr(__builtins__, reference)
        raise ManifestError(f"factory/invariant {reference!r} needs a module prefix")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ManifestError(f"cannot import {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ManifestError(f"{module_name!r} has no callable {attr!r}") from exc


def _comparable_raw_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    return _RAW_TYPE_ALIASES.get(type_name, type_name)


def _build_field_invariant(spec: Any) -> Callable[[Any], tuple[bool, Any]]:
    if callable(spec):
        return spec
    if isinstance(spec, str):
        if spec in {"nonnegative", "non_negative"}:
            return lambda value: (value >= 0, "negative value")
        if spec in {"positive"}:
            return lambda value: (value > 0, "non-positive value")
        resolved = _resolve_callable(spec)
        return resolved
    if not isinstance(spec, dict):
        raise ManifestError(f"unsupported field invariant: {spec!r}")

    op = spec.get("op", spec.get("operator", ">="))
    expected = spec.get("value", 0)
    code = spec.get("code", spec.get("message", "field invariant failed"))
    operations = {
        ">": lambda actual, wanted: actual > wanted,
        ">=": lambda actual, wanted: actual >= wanted,
        "<": lambda actual, wanted: actual < wanted,
        "<=": lambda actual, wanted: actual <= wanted,
        "==": lambda actual, wanted: actual == wanted,
        "!=": lambda actual, wanted: actual != wanted,
    }
    if op not in operations:
        raise ManifestError(f"unsupported invariant operator {op!r}")
    compare = operations[op]
    return lambda value: (compare(value, expected), code)


def _get_item(subject: Any, name: str) -> Any:
    try:
        return getattr(subject, name)
    except AttributeError:
        return subject[name]


def _build_global_invariant(spec: Any) -> Callable[[Any], tuple[bool, Any]]:
    if callable(spec):
        return spec
    if not isinstance(spec, dict):
        raise ManifestError(f"unsupported global invariant: {spec!r}")

    kind = spec.get("kind", "expression")
    code = spec.get("code", spec.get("message", "global invariant failed"))

    if kind in {"sum_zero", "balanced_total"}:
        names = list(spec["fields"])

        def invariant(subject: Any) -> tuple[bool, Any]:
            total = sum(_get_item(subject, name) for name in names)
            return total == 0, code

        return invariant

    if kind == "sum_equals":
        names = list(spec["fields"])
        wanted = spec["value"]

        def invariant(subject: Any) -> tuple[bool, Any]:
            total = sum(_get_item(subject, name) for name in names)
            return total == wanted, code

        return invariant

    if kind == "sum_delta":
        debits = list(spec.get("negative", spec.get("debit", ())))
        credits = list(spec.get("positive", spec.get("credit", ())))

        def invariant(subject: Any) -> tuple[bool, Any]:
            total = sum(_get_item(subject, name) for name in credits)
            total -= sum(_get_item(subject, name) for name in debits)
            return total == 0, code

        return invariant

    if kind == "dotted":
        return _resolve_callable(spec["callable"])

    raise ManifestError(f"unsupported global invariant kind {kind!r}")


def build_schema(spec: dict[str, Any] | None, base: type[PClass] | type[PRecord]) -> type:
    if spec is None:
        raise ManifestError(f"missing schema specification for {base.__name__}")

    namespace: dict[str, Any] = {}
    for name, descriptor in spec.get("fields", {}).items():
        if isinstance(descriptor, str):
            descriptor = {"type": descriptor}
        descriptor = dict(descriptor or {})
        type_spec = descriptor.get("type")
        field_type: Any = _TYPE_NAMES.get(type_spec, type_spec) if isinstance(type_spec, str) else (type_spec or ())
        if isinstance(field_type, str):
            field_type = _resolve_callable(field_type)

        kwargs: dict[str, Any] = {"type": field_type}
        if "mandatory" in descriptor:
            kwargs["mandatory"] = descriptor["mandatory"]
        if "initial" in descriptor:
            kwargs["initial"] = descriptor["initial"]
        if "factory" in descriptor:
            factory = descriptor["factory"]
            kwargs["factory"] = _resolve_callable(factory) if isinstance(factory, str) else factory
        if "invariant" in descriptor:
            kwargs["invariant"] = _build_field_invariant(descriptor["invariant"])
        namespace[name] = field(**kwargs)

    invariants = spec.get("invariants", spec.get("global_invariants", ()))
    if invariants:
        built = [_build_global_invariant(item) for item in invariants]

        def __invariant__(subject: Any) -> tuple[bool, tuple[Any, ...]]:
            errors = tuple(code for ok, code in (item(subject) for item in built) if not ok)
            return not errors, errors

        namespace["__invariant__"] = __invariant__

    class_name = str(spec.get("name", f"Generated{base.__name__}"))
    return type(class_name, (base,), namespace)


def _normalize_revisions(raw_revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw_revisions, list) or not raw_revisions:
        raise ManifestError("revisions must be a non-empty list")

    revisions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for index, raw in enumerate(raw_revisions):
        if not isinstance(raw, dict):
            raise ManifestError(f"revision {index} must be an object")
        rid = raw.get("id") or raw.get("revision_id")
        if rid is None:
            raise ManifestError(f"revision at index {index} has no id")
        rid = str(rid)
        if rid in seen_ids:
            raise ManifestError(f"duplicate revision id {rid!r}")
        seen_ids.add(rid)

        label = str(raw.get("revision", raw.get("number", rid)))
        if label in seen_labels:
            raise ManifestError(f"duplicate revision number/label {label!r}")
        seen_labels.add(label)

        operations: list[tuple[str, str, Any]] = []
        if "field" in raw:
            op = str(raw.get("op", "set"))
            operations.append((op, str(raw["field"]), raw.get("value")))
        if "fields" in raw or "values" in raw:
            values = raw.get("fields", raw.get("values"))
            if not isinstance(values, dict):
                raise ManifestError(f"revision {rid} values must be an object")
            for field_name, value in values.items():
                operations.append(("set", str(field_name), value))
        for field_name in raw.get("remove", raw.get("delete", ())) or ():
            operations.append(("remove", str(field_name), None))
        if not operations:
            raise ManifestError(f"revision {rid} has no field write")

        groups = raw.get("groups", ())
        if isinstance(groups, str):
            groups = [groups]
        if raw.get("group"):
            groups = [*groups, raw["group"]]

        revisions.append(
            {
                "id": rid,
                "label": label,
                "index": index,
                "archivable": bool(raw.get("archivable", raw.get("archive_allowed", False))),
                "operations": operations,
                "groups": [str(group) for group in groups],
                "raw": raw,
            }
        )
    return revisions


def _normalize_checkpoints(
    raw_checkpoints: list[dict[str, Any]],
    revisions: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    by_label: dict[str, str],
) -> list[dict[str, Any]]:
    if not raw_checkpoints:
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_checkpoints:
        cid = str(raw.get("id", raw.get("name", f"checkpoint-{len(normalized) + 1}")))
        after = raw.get("after", raw.get("after_revision", raw.get("at")))
        if after is None:
            raise ManifestError(f"checkpoint {cid} needs after")

        if isinstance(after, int) and not isinstance(after, bool) and after not in by_id and str(after) not in by_label:
            if not 1 <= after <= len(revisions):
                raise ManifestError(f"checkpoint {cid} after={after!r} is out of range")
            after_index = after - 1
        else:
            key = str(after)
            rid = by_label.get(key, key if key in by_id else None)
            if rid is None or rid not in by_id:
                raise ManifestError(f"checkpoint {cid} references unknown revision {after!r}")
            after_index = by_id[rid]["index"]

        raw_signoffs = raw.get("signoffs", raw.get("items", raw.get("expectations", {})))
        if isinstance(raw_signoffs, list):
            signoffs = {str(item["field"]): item for item in raw_signoffs}
        elif isinstance(raw_signoffs, dict):
            signoffs = {str(name): value for name, value in raw_signoffs.items()}
        else:
            raise ManifestError(f"checkpoint {cid} signoffs must be object or list")

        cleaned_signoffs: dict[str, dict[str, Any]] = {}
        for field_name, expectation in signoffs.items():
            if expectation is None:
                expectation = {}
            if not isinstance(expectation, dict):
                expectation = {"revision": expectation}
            expected_revision = expectation.get("revision", expectation.get("adopted", expectation.get("adopted_revision")))
            expected_id: str | None = None
            adopted_index: int | None = None
            if expected_revision is not None:
                key = str(expected_revision)
                expected_id = by_label.get(key, key if key in by_id else None)
                if expected_id is None:
                    raise ManifestError(f"checkpoint {cid} field {field_name} references unknown revision {expected_revision!r}")
                adopted_index = by_id[expected_id]["index"]

            raw_type = expectation.get("raw_type", expectation.get("original_type", expectation.get("type")))
            revision_explicit = any(
                key in expectation for key in ("revision", "adopted", "adopted_revision")
            )
            cleaned_signoffs[field_name] = {
                "revision_id": expected_id,
                "revision_explicit": revision_explicit,
                "adopted_index": adopted_index,
                "missing": expectation.get("missing"),
                "raw_type": _comparable_raw_type(raw_type),
            }

        normalized.append(
            {
                "id": cid,
                "after_index": after_index,
                "barrier": after_index + 1,
                "signoffs": cleaned_signoffs,
            }
        )

    normalized.sort(key=lambda item: (item["after_index"], item["id"]))
    return normalized


def _build_constraints(
    revisions: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_id = {rev["id"]: rev for rev in revisions}
    touches: dict[str, list[int]] = {}
    for rev in revisions:
        for _op, field_name, _value in rev["operations"]:
            touches.setdefault(field_name, []).append(rev["index"])

    required_send: set[str] = set()
    field_expectations: dict[str, list[tuple[int, int | None, str, str | None]]] = {}
    for checkpoint in checkpoints:
        for field_name, expectation in checkpoint["signoffs"].items():
            adopted_index = expectation["adopted_index"]
            if expectation.get("missing") is True and adopted_index is None:
                adopted_index = -1
            field_expectations.setdefault(field_name, []).append(
                (
                    checkpoint["after_index"],
                    adopted_index,
                    checkpoint["id"],
                    expectation["revision_id"],
                )
            )
            if expectation["revision_id"]:
                required_send.add(expectation["revision_id"])

    required_omit: dict[str, dict[str, Any]] = {}
    for field_name, entries in field_expectations.items():
        entries.sort(key=lambda item: item[0])
        checkpoint_positions = [entry[0] for entry in entries]
        suffix_min: list[tuple[int, str, str | None] | None] = [None] * (len(entries) + 1)
        best: tuple[int, str, str | None] | None = None
        for i in range(len(entries) - 1, -1, -1):
            _after, adopted_index, checkpoint_id, revision_id = entries[i]
            candidate = (adopted_index if adopted_index is not None else 10**18, checkpoint_id, revision_id)
            if best is None or candidate[0] < best[0]:
                best = candidate
            suffix_min[i] = best

        for pos in touches.get(field_name, []):
            first = bisect_left(checkpoint_positions, pos)
            witness = suffix_min[first] if first < len(entries) else None
            if witness is not None and witness[0] < pos:
                rid = revisions[pos]["id"]
                required_omit[rid] = {
                    "field": field_name,
                    "checkpoint": witness[1],
                    "adopted_revision_id": witness[2],
                }

    blockers: list[dict[str, Any]] = []
    for rid, witness in required_omit.items():
        rev = by_id[rid]
        if not rev["archivable"]:
            blockers.append(
                {
                    "kind": "mandatory_revision_supersedes_signoff",
                    "revision": rid,
                    "revision_label": rev["label"],
                    "checkpoint": witness["checkpoint"],
                    "field": witness["field"],
                    "message": "non-archivable revision would become the latest write at a sign-off",
                }
            )
        if rid in required_send:
            blockers.append(
                {
                    "kind": "inconsistent_signoff_revision",
                    "revision": rid,
                    "revision_label": rev["label"],
                    "checkpoint": witness["checkpoint"],
                    "field": witness["field"],
                    "message": "one sign-off adopts this revision while another requires it to be absent",
                }
            )

    return required_send, required_omit, {"blockers": blockers}


def _precord_write_error(precord_cls: type[PRecord], field_name: str, value: Any) -> str | None:
    evolver = _PRecordEvolver(precord_cls, pmap())
    try:
        evolver.set(field_name, value)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    errors = list(getattr(evolver, "_invariant_error_codes", ()))
    if errors:
        return f"InvariantException: {tuple(errors)!r}"
    return None


def _check_atomic_groups(
    revisions: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    commit_groups: list[list[str]] | None,
) -> list[dict[str, Any]]:
    by_id = {rev["id"]: rev for rev in revisions}
    groups: dict[str, set[str]] = {}
    for rev in revisions:
        for group in rev["groups"]:
            groups.setdefault(group, set()).add(rev["id"])
    for index, group in enumerate(commit_groups or ()):
        groups.setdefault(f"manifest-group-{index + 1}", set()).update(str(rid) for rid in group)

    blockers: list[dict[str, Any]] = []
    barriers = sorted({checkpoint["barrier"] for checkpoint in checkpoints})
    for group_name, ids in groups.items():
        unknown = sorted(rid for rid in ids if rid not in by_id)
        if unknown:
            blockers.append({"kind": "unknown_commit_group_revision", "group": group_name, "revisions": unknown})
            continue
        positions = [by_id[rid]["index"] for rid in ids]
        min_pos, max_pos = min(positions), max(positions)
        for barrier in barriers:
            if min_pos < barrier <= max_pos:
                checkpoint = next(cp["id"] for cp in checkpoints if cp["barrier"] == barrier)
                blockers.append(
                    {
                        "kind": "commit_group_split_by_signoff",
                        "group": group_name,
                        "checkpoint": checkpoint,
                        "revisions": sorted(ids),
                        "message": "sign-off commit boundary falls inside an atomic submission group",
                    }
                )
                break
    return blockers


def _initial_provenance(initial: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for name, value in (initial or {}).items():
        provenance[name] = {
            "revision_id": None,
            "raw_type": type(value).__name__,
            "missing": value is None,
        }
    return provenance


def _apply_operations(evolver: Any, operations: list[tuple[str, str, Any]], *, record_errors: bool) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for op, field_name, value in operations:
        try:
            if op == "remove":
                evolver.remove(field_name)
            else:
                evolver.set(field_name, value)
        except Exception as exc:
            error = {"field": field_name, "kind": type(exc).__name__, "message": str(exc)}
            errors.append(error)
            if record_errors:
                return errors
    if record_errors:
        sticky = list(getattr(evolver, "_invariant_error_codes", ()))
        if sticky:
            errors.append({"kind": "InvariantException", "message": repr(tuple(sticky))})
    return errors


def _native_dict(subject: Any, *, pclass: bool) -> dict[str, Any]:
    if pclass:
        return subject._to_dict()
    return dict(subject)


def _archive_items(
    revisions: list[dict[str, Any]],
    decision: dict[str, bool],
    compatibility: dict[str, str | None],
    required_omit: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    selected_indices_by_field: dict[str, list[int]] = {}
    for rev in revisions:
        if not decision[rev["id"]]:
            continue
        for _op, field_name, _value in rev["operations"]:
            selected_indices_by_field.setdefault(field_name, []).append(rev["index"])
    for indices in selected_indices_by_field.values():
        indices.sort()

    archived_only: list[dict[str, Any]] = []
    for rev in revisions:
        rid = rev["id"]
        if decision[rid]:
            continue

        reason = "redundant_optional_write"
        detail = "archive permission exists and no sign-off adopts this revision"
        if compatibility[rid]:
            reason = "record_entry_rejects_write"
            detail = compatibility[rid]
        elif rid in required_omit:
            witness = required_omit[rid]
            reason = "shadowed_to_preserve_signoff"
            detail = f"must stay archive-only for checkpoint {witness['checkpoint']} field {witness['field']}"
        else:
            covering = None
            for _op, field_name, _value in rev["operations"]:
                indices = selected_indices_by_field.get(field_name, [])
                position = bisect_left(indices, rev["index"] + 1)
                if position < len(indices):
                    covering = (field_name, revisions[indices[position]]["id"])
                    break
            if covering:
                reason = "covered_by_later_sent_write"
                detail = f"{covering[0]} is later delivered by {covering[1]}"
        archived_only.append(
            {
                "revision": rid,
                "revision_label": rev["label"],
                "reason": reason,
                "detail": detail,
            }
        )
    return archived_only


def _evaluate_selection(
    decision: dict[str, bool],
    revisions: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    *,
    initial: dict[str, Any] | None,
    pclass_cls: type[PClass] | None,
    precord_cls: type[PRecord] | None,
    initial_pclass: PClass | None,
    initial_precord: PRecord | None,
    compatibility: dict[str, str | None],
    verify: bool,
) -> dict[str, Any]:
    by_id = {rev["id"]: rev for rev in revisions}
    selected_ids = [rev["id"] for rev in revisions if decision[rev["id"]]]
    selected = [by_id[rid] for rid in selected_ids]
    blockers: list[dict[str, Any]] = []

    for rid in selected_ids:
        rev = by_id[rid]
        if compatibility[rid]:
            kind = "mandatory_write_field_error" if not rev["archivable"] else "required_write_field_error"
            blockers.append(
                {
                    "kind": kind,
                    "revision": rid,
                    "revision_label": rev["label"],
                    "error": compatibility[rid],
                    "message": "PRecord rejects or permanently records this per-write field error before commit",
                }
            )

    barriers = sorted({0, *(cp["barrier"] for cp in checkpoints), len(revisions)})
    checkpoint_by_barrier: dict[int, list[dict[str, Any]]] = {}
    for checkpoint in checkpoints:
        checkpoint_by_barrier.setdefault(checkpoint["barrier"], []).append(checkpoint)

    commits: list[dict[str, Any]] = []
    can_verify = bool(verify and pclass_cls is not None and precord_cls is not None)
    verification: dict[str, Any] = {"performed": can_verify and not blockers, "checkpoints": []}
    provenance = _initial_provenance(initial)

    pclass_subject = initial_pclass
    precord_subject = initial_precord
    if can_verify and not blockers:
        if pclass_subject is None and pclass_cls is not None:
            try:
                pclass_subject = pclass_cls.create(dict(initial or {}))
            except Exception as exc:
                blockers.append({"kind": "initial_pclass_error", "error": f"{type(exc).__name__}: {exc}"})
        if precord_subject is None and precord_cls is not None:
            try:
                precord_subject = precord_cls.create(dict(initial or {}))
            except Exception as exc:
                blockers.append({"kind": "initial_precord_error", "error": f"{type(exc).__name__}: {exc}"})

    start = 0
    selected_by_index = {rev["index"]: rev for rev in selected}
    for barrier in barriers[1:]:
        segment = [selected_by_index[i] for i in range(start, barrier) if i in selected_by_index]
        checkpoint_ids = [cp["id"] for cp in checkpoint_by_barrier.get(barrier, [])]
        commit = {
            "checkpoints": checkpoint_ids,
            "revisions": [rev["id"] for rev in segment],
            "revision_labels": [rev["label"] for rev in segment],
            "write_count": len(segment),
            "field_operation_count": sum(len(rev["operations"]) for rev in segment),
        }
        if segment:
            commits.append(commit)

        if can_verify and not blockers:
            if pclass_subject is None or precord_subject is None:
                blockers.append({"kind": "missing_native_schema_or_initial_state"})
                break

            p_evolver = pclass_subject.evolver()
            r_evolver = precord_subject.evolver()
            apply_errors: list[dict[str, Any]] = []
            for rev in segment:
                p_errors = _apply_operations(p_evolver, rev["operations"], record_errors=False)
                if p_errors:
                    apply_errors.extend({**error, "revision": rev["id"], "entry": "PClass"} for error in p_errors)
                r_errors = _apply_operations(r_evolver, rev["operations"], record_errors=True)
                if r_errors:
                    apply_errors.extend({**error, "revision": rev["id"], "entry": "PRecord"} for error in r_errors)

            p_committed = None
            r_committed = None
            try:
                p_committed = p_evolver.persistent()
            except Exception as exc:
                apply_errors.append({"entry": "PClass", "kind": type(exc).__name__, "message": str(exc)})
            try:
                r_committed = r_evolver.persistent()
            except Exception as exc:
                apply_errors.append({"entry": "PRecord", "kind": type(exc).__name__, "message": str(exc)})

            if apply_errors:
                blockers.append(
                    {
                        "kind": "native_commit_failed",
                        "checkpoints": checkpoint_ids,
                        "revisions": commit["revisions"],
                        "errors": apply_errors,
                    }
                )
                start = barrier
                continue

            p_data = _native_dict(p_committed, pclass=True)
            r_data = _native_dict(r_committed, pclass=False)
            if p_data != r_data:
                blockers.append(
                    {
                        "kind": "committed_state_divergence",
                        "checkpoints": checkpoint_ids,
                        "pclass": repr(p_data),
                        "precord": repr(r_data),
                    }
                )
                start = barrier
                continue

            for rev in segment:
                for op, field_name, value in rev["operations"]:
                    if op == "remove":
                        provenance[field_name] = {
                            "revision_id": rev["id"],
                            "raw_type": "NoneType",
                            "missing": True,
                        }
                    else:
                        provenance[field_name] = {
                            "revision_id": rev["id"],
                            "raw_type": type(value).__name__,
                            "missing": False,
                        }

            for checkpoint in checkpoint_by_barrier.get(barrier, []):
                for field_name, expected in checkpoint["signoffs"].items():
                    actual_pointer = provenance.get(field_name, {"revision_id": None, "raw_type": "MissingType"})
                    native_present = field_name in r_data
                    problems = []
                    if expected["revision_id"] is not None and actual_pointer.get("revision_id") != expected["revision_id"]:
                        problems.append(
                            {
                                "field": field_name,
                                "expected_revision": expected["revision_id"],
                                "actual_revision": actual_pointer.get("revision_id"),
                            }
                        )
                    elif expected.get("revision_explicit") and expected["revision_id"] is None and actual_pointer.get("revision_id") is not None:
                        problems.append(
                            {
                                "field": field_name,
                                "expected_revision": None,
                                "actual_revision": actual_pointer.get("revision_id"),
                            }
                        )
                    if expected["missing"] is True and native_present:
                        problems.append({"field": field_name, "expected_missing": True, "actual_missing": False})
                    if expected["missing"] is False and not native_present:
                        problems.append({"field": field_name, "expected_missing": False, "actual_missing": True})
                    if expected["raw_type"] and expected["raw_type"] != actual_pointer.get("raw_type"):
                        problems.append(
                            {
                                "field": field_name,
                                "expected_raw_type": expected["raw_type"],
                                "actual_raw_type": actual_pointer.get("raw_type"),
                            }
                        )
                    item = {
                        "checkpoint": checkpoint["id"],
                        "field": field_name,
                        "actual_revision": actual_pointer.get("revision_id"),
                        "actual_raw_type": actual_pointer.get("raw_type"),
                        "missing": not native_present,
                        "ok": not problems,
                        "problems": problems,
                    }
                    verification["checkpoints"].append(item)
                    if problems:
                        blockers.append(
                            {
                                "kind": "signoff_unreachable",
                                "checkpoint": checkpoint["id"],
                                "field": field_name,
                                "problems": problems,
                            }
                        )

            if not any(blocker["kind"] in {"signoff_unreachable", "committed_state_divergence"} for blocker in blockers):
                pclass_subject = p_committed
                precord_subject = r_committed
        start = barrier

    return {
        "feasible": not blockers,
        "blockers": blockers,
        "commits": commits,
        "verification": verification,
        "write_count": len(selected_ids),
        "commit_count": len(commits),
    }


def plan_dispatch(
    revisions: list[dict[str, Any]],
    *,
    checkpoints: list[dict[str, Any]] | None = None,
    initial: dict[str, Any] | None = None,
    pclass_cls: type[PClass] | None = None,
    precord_cls: type[PRecord] | None = None,
    initial_pclass: PClass | None = None,
    initial_precord: PRecord | None = None,
    commit_groups: list[list[str]] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    normalized = _normalize_revisions(revisions)
    by_id = {rev["id"]: rev for rev in normalized}
    by_label = {rev["label"]: rev["id"] for rev in normalized}
    normalized_checkpoints = _normalize_checkpoints(checkpoints or [], normalized, by_id, by_label)

    required_send, required_omit, constraint_result = _build_constraints(normalized, normalized_checkpoints)
    blockers = list(constraint_result["blockers"])
    blockers.extend(_check_atomic_groups(normalized, normalized_checkpoints, commit_groups))

    compatibility: dict[str, str | None] = {}
    for rev in normalized:
        error = None
        if precord_cls is not None:
            for op, field_name, value in rev["operations"]:
                if op == "set":
                    error = _precord_write_error(precord_cls, field_name, value)
                    if error:
                        break
        compatibility[rev["id"]] = error

    decision: dict[str, bool] = {}
    for rev in normalized:
        rid = rev["id"]
        send = (not rev["archivable"]) or rid in required_send
        if rid in required_omit:
            send = False
        decision[rid] = send

    evaluation = _evaluate_selection(
        decision,
        normalized,
        normalized_checkpoints,
        initial=initial,
        pclass_cls=pclass_cls,
        precord_cls=precord_cls,
        initial_pclass=initial_pclass,
        initial_precord=initial_precord,
        compatibility=compatibility,
        verify=verify,
    )

    free_ids = [
        rev["id"]
        for rev in normalized
        if rev["archivable"] and rev["id"] not in required_send and rev["id"] not in required_omit
    ]
    exhaustive_search = {"used": False, "limit": 12, "free_revisions": len(free_ids)}
    if not evaluation["feasible"] and not blockers and free_ids and len(free_ids) <= exhaustive_search["limit"]:
        best: dict[str, Any] | None = None
        best_decision: dict[str, bool] | None = None
        for extra_count in range(1, len(free_ids) + 1):
            for combo in combinations(free_ids, extra_count):
                if any(compatibility[rid] for rid in combo):
                    continue
                candidate_decision = dict(decision)
                for rid in combo:
                    candidate_decision[rid] = True
                candidate = _evaluate_selection(
                    candidate_decision,
                    normalized,
                    normalized_checkpoints,
                    initial=initial,
                    pclass_cls=pclass_cls,
                    precord_cls=precord_cls,
                    initial_pclass=initial_pclass,
                    initial_precord=initial_precord,
                    compatibility=compatibility,
                    verify=verify,
                )
                if candidate["feasible"] and (
                    best is None or candidate["commit_count"] < best["commit_count"]
                ):
                    best = candidate
                    best_decision = candidate_decision
            if best is not None:
                break
        if best is not None and best_decision is not None:
            evaluation = best
            decision = best_decision
            exhaustive_search["used"] = True
    elif not evaluation["feasible"] and len(free_ids) > exhaustive_search["limit"]:
        evaluation["blockers"].append(
            {
                "kind": "long_sheet_combinations_not_enumerated",
                "free_revision_count": len(free_ids),
                "message": "sign-off compression was used; provide complete adopted-revision sign-offs for linear planning",
            }
        )

    archived_only = _archive_items(normalized, decision, compatibility, required_omit)
    all_blockers = blockers + evaluation["blockers"]
    selected_ids = [rev["id"] for rev in normalized if decision[rev["id"]]]
    return {
        "feasible": not all_blockers,
        "write_count": evaluation["write_count"],
        "field_operation_count": sum(len(by_id[rid]["operations"]) for rid in selected_ids),
        "commit_count": evaluation["commit_count"],
        "sent_revisions": selected_ids,
        "sent_revision_labels": [by_id[rid]["label"] for rid in selected_ids],
        "commits": evaluation["commits"],
        "archived_only": archived_only,
        "blockers": all_blockers,
        "verification": evaluation["verification"],
        "optimization": exhaustive_search,
    }

def plan_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    schemas = manifest.get("schemas", {})
    pclass_spec = schemas.get("pclass", schemas.get("old"))
    precord_spec = schemas.get("precord", schemas.get("new", schemas.get("precord_record")))
    shared_spec = schemas.get("shared", schemas.get("schema"))
    pclass_cls = build_schema(pclass_spec or shared_spec, PClass) if (pclass_spec or shared_spec) else None
    precord_cls = build_schema(precord_spec or shared_spec, PRecord) if (precord_spec or shared_spec) else None

    result = plan_dispatch(
        manifest["revisions"],
        checkpoints=manifest.get("checkpoints", []),
        initial=manifest.get("initial", {}),
        pclass_cls=pclass_cls,
        precord_cls=precord_cls,
        commit_groups=manifest.get("commit_groups", []),
        verify=bool(manifest.get("verify_native", True)),
    )
    result["schema_names"] = {
        "pclass": getattr(pclass_cls, "__name__", None),
        "precord": getattr(precord_cls, "__name__", None),
    }
    return result


def render_markdown(result: dict[str, Any]) -> str:
    status = "可行" if result["feasible"] else "不存在共用走法"
    lines = [
        "# 发单规划结果",
        "",
        f"- 结论：{status}",
        f"- 真正送出的修订数：{result['write_count']}",
        f"- 提交次数：{result['commit_count']}",
        "",
        "## 写入清单与提交边界",
    ]
    if not result["commits"]:
        lines.append("- 无需新提交")
    for index, commit in enumerate(result["commits"], 1):
        boundary = "/".join(commit["checkpoints"]) or "END"
        lines.append(
            f"{index}. `{boundary}`：" + ", ".join(f"`{rid}`" for rid in commit["revisions"])
        )

    lines.extend(["", "## 只留档修订", ""])
    if not result["archived_only"]:
        lines.append("- 无")
    for item in result["archived_only"]:
        lines.append(f"- `{item['revision']}`：{item['reason']}；{item['detail']}")

    lines.extend(["", "## 阻断项", ""])
    if not result["blockers"]:
        lines.append("- 无")
    for blocker in result["blockers"]:
        lines.append(f"- `{blocker.get('kind')}`：{blocker.get('message', blocker)}")

    checkpoint_results = result.get("verification", {}).get("checkpoints", [])
    if checkpoint_results:
        lines.extend(["", "## 签批点核对", ""])
        for item in checkpoint_results:
            mark = "OK" if item["ok"] else "FAIL"
            lines.append(
                f"- [{mark}] `{item['checkpoint']}` / `{item['field']}`："
                f"revision={item['actual_revision']}, raw_type={item['actual_raw_type']}, missing={item['missing']}"
            )
    return "\n".join(lines) + "\n"


def _demo_manifest() -> dict[str, Any]:
    return {
        "initial": {"location_no": 100, "loc1": 10, "loc2": 10, "width_cm": "3"},
        "schemas": {
            "shared": {
                "name": "StockTicket",
                "fields": {
                    "location_no": {"type": "int", "mandatory": True},
                    "loc1": {"type": "int"},
                    "loc2": {"type": "int"},
                    "width_cm": {"type": "int", "factory": "__main__:cm_from_m"},
                },
                "invariants": [{"kind": "sum_equals", "fields": ["loc1", "loc2"], "value": 20}],
            }
        },
        "revisions": [
            {"id": "R7", "field": "location_no", "value": "TEMP-TEXT", "archivable": True},
            {"id": "R12", "field": "location_no", "value": 4321, "archivable": False},
            {"id": "R18", "field": "loc1", "value": 7, "archivable": False, "group": "MOVE-3"},
            {"id": "R19", "field": "loc2", "value": 13, "archivable": False, "group": "MOVE-3"},
        ],
        "checkpoints": [
            {
                "id": "APPROVAL",
                "after": "R19",
                "signoffs": {
                    "location_no": {"revision": "R12", "raw_type": "int", "missing": False},
                    "loc1": {"revision": "R18", "raw_type": "int", "missing": False},
                    "loc2": {"revision": "R19", "raw_type": "int", "missing": False},
                    "width_cm": {"revision": None, "raw_type": "str", "missing": False},
                },
            }
        ],
    }


def cm_from_m(value: Any) -> int:
    return int(value) * 100


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, help="handoff manifest JSON")
    parser.add_argument("--output", "-o", type=Path, help="write result here")
    parser.add_argument("--format", choices=("json", "md"), default="json", help="output format")
    parser.add_argument("--demo", action="store_true", help="run the clue-shaped built-in example")
    args = parser.parse_args(argv)

    if args.demo:
        manifest = _demo_manifest()
    else:
        if args.manifest is None:
            parser.error("manifest is required unless --demo is used")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    result = plan_manifest(manifest)
    if args.format == "md":
        text = render_markdown(result)
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False)
    if args.output:
        args.output.write_text(text if args.format == "md" else text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["feasible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
