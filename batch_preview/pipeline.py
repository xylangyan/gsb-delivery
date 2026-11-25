"""Orchestrate one batch preview run.

Pipeline: parse manuals with Marko -> locate the marked positions -> build
per-release constraint models -> solve one global assignment -> emit and
verify the publishable releases -> assemble a report keyed by original
manual positions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from marko.helpers import normalize_label

from .emit import EmittedRelease, emit_release
from .manual import Manual
from .model import BatchSpec
from .plan import ReleaseModel, build_release, mark_id
from .solve import Solver, Variable


@dataclass
class BatchResult:
    status: str  # "ok" | "blocked" | "pending"
    releases: dict[str, EmittedRelease] = field(default_factory=dict)
    release_status: dict[str, str] = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    elapsed: float = 0.0


def run_batch(spec: BatchSpec, only_releases: list[str] | None = None) -> BatchResult:
    started = time.monotonic()
    warnings: list[str] = []

    manuals = {name: Manual(name, text) for name, text in spec.manuals.items()}

    # Locate every marked position in its original manual.
    variables: dict[str, Variable] = {}
    marks_by_position: dict[tuple[str, int], str] = {}
    for i, mark in enumerate(spec.marks):
        manual = manuals[mark.manual]
        offset = mark.offset
        if offset is None:
            try:
                offset = manual.offset_of(mark.line, mark.col)
            except ValueError as exc:
                warnings.append(f"marks[{i}]: {exc}")
                continue
        ref = manual.find_ref_at(offset)
        dfn = manual.find_def_at(offset)
        if ref is None and dfn is None:
            if manual.footnote_ref_at(offset) is not None:
                warnings.append(
                    f"marks[{i}]: {mark.manual}@{offset} 是脚注引用，"
                    "脚注编号可随发行稿重排，无需标注"
                )
            else:
                warnings.append(
                    f"marks[{i}]: {mark.manual}@{offset} 未指向任何引用或定义"
                )
            continue
        mid = mark_id(mark.manual, ref.start if ref is not None else dfn.start)
        candidate_raw: dict[str, str] = {}
        for c in mark.candidates:
            candidate_raw.setdefault(normalize_label(c), c)
        if mid in variables:
            variables[mid].candidate_raw.update(candidate_raw)
            variables[mid].candidates = sorted(
                c
                for c in variables[mid].candidate_raw
                if c != variables[mid].orig_label
            )
            continue
        if ref is not None:
            orig_label = ref.label
            def_key = (mark.manual, ref.def_index)
            kind = "ref"
        else:
            orig_label = dfn.label
            def_key = (mark.manual, dfn.index)
            kind = "def"
        candidates = [c for c in candidate_raw if c != orig_label]
        variables[mid] = Variable(
            mark_id=mid,
            manual=mark.manual,
            offset=ref.start if ref is not None else dfn.start,
            kind=kind,
            orig_label=orig_label,
            candidates=candidates,
            candidate_raw=candidate_raw,
            def_key=def_key,
        )
        marks_by_position[(mark.manual, variables[mid].offset)] = mid

    # Build per-release constraint models.
    release_names = {
        name: segs
        for name, segs in spec.releases.items()
        if only_releases is None or name in only_releases
    }
    models: dict[str, ReleaseModel] = {}
    for name, segs in release_names.items():
        models[name] = build_release(name, segs, spec, manuals, marks_by_position)

    # Activate variables and register which releases each one touches.
    active: set[str] = set()
    for model in models.values():
        active |= model.active_marks
    # A definition position is active whenever a release needs its definition,
    # even if the definition line itself is not inside any selected segment.
    needed_defs: set[tuple[str, int]] = set()
    for model in models.values():
        for req in model.ref_reqs:
            needed_defs.add(req.def_key)
    for mid, var in variables.items():
        if var.kind == "def" and var.def_key in needed_defs:
            active.add(mid)
    variables = {mid: v for mid, v in variables.items() if mid in active}

    def_marks = {
        var.def_key: mid
        for mid, var in variables.items()
        if var.kind == "def"
    }
    for model in models.values():
        for req in model.ref_reqs:
            if req.mark_id and req.mark_id in variables:
                variables[req.mark_id].releases.add(model.name)
                variables[req.mark_id].physical_releases.add(model.name)
        for pd in model.phys_defs:
            if pd.mark_id and pd.mark_id in variables:
                variables[pd.mark_id].releases.add(model.name)
                variables[pd.mark_id].physical_releases.add(model.name)
        for req in model.ref_reqs:
            def_mark = def_marks.get(req.def_key)
            if def_mark is not None:
                variables[def_mark].releases.add(model.name)

    solver = Solver(variables, models, manuals, spec.time_budget)
    solved = solver.solve()

    # Emit publishable releases.
    emitted: dict[str, EmittedRelease] = {}
    release_reports: dict[str, dict] = {}
    for name, model in models.items():
        status = solved.release_status.get(name, "ok")
        if status == "ok":
            try:
                rel = emit_release(name, model, solved.assignment, manuals, variables)
            except Exception as exc:  # an emission bug must not look like a verdict
                rel = None
                status = "pending"
                warnings.append(f"{name}: 生成发行稿时出错: {exc}")
            if rel is not None:
                emitted[name] = rel
                if not rel.ok:
                    status = "pending"
                    warnings.append(f"{name}: 发行稿核对未通过，预览结果未确认")
        release_reports[name] = {"status": status}

    # Global change list, keyed by original position (counted once).  A
    # change is used by every release that includes the position itself or
    # (for definitions) a reference relabeled to the same new label.
    def change_used_in(var: Variable, new_label: str) -> list[str]:
        used = set(var.physical_releases)
        if var.kind == "def":
            for model in models.values():
                for req in model.ref_reqs:
                    if (
                        req.def_key == var.def_key
                        and req.mark_id is not None
                        and solved.assignment.get(req.mark_id) == new_label
                    ):
                        used.add(model.name)
        return sorted(used)

    changes = []
    for mid, new_label in sorted(
        solved.assignment.items(), key=lambda kv: variables[kv[0]].offset
    ):
        var = variables[mid]
        manual = manuals[var.manual]
        line, col = manual.line_col(var.offset)
        changes.append(
            {
                "manual": var.manual,
                "line": line,
                "col": col,
                "kind": "reference" if var.kind == "ref" else "definition",
                "original_label": var.orig_label,
                "new_label": var.candidate_raw.get(new_label, new_label),
                "releases": change_used_in(var, new_label),
            }
        )

    blockers = []
    for b in solved.blockers:
        entry = {
            "release": b.release,
            "label": b.label,
            "kind": b.kind,
            "text": b.text,
            "blocked_marks": sorted(b.blocked_marks),
        }
        if b.manual is not None and b.offset is not None:
            line, col = manuals[b.manual].line_col(b.offset)
            entry["manual"] = b.manual
            entry["line"] = line
            entry["col"] = col
        blockers.append(entry)

    for name, rel in emitted.items():
        release_reports[name].update(
            {
                "text": rel.text,
                "definitions": [
                    {
                        "manual": d.manual,
                        "definition_index": d.def_index,
                        "line": manuals[d.manual].line_col(
                            manuals[d.manual].defs[d.def_index].start
                        )[0],
                        "label": d.label,
                        "dest": d.dest,
                        "origin": d.origin,
                    }
                    for d in rel.definitions
                ],
                "footnotes": rel.footnotes,
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail}
                    for c in rel.checks
                ],
            }
        )

    statuses = {r["status"] for r in release_reports.values()}
    if "pending" in statuses:
        overall = "pending"
    elif "blocked" in statuses:
        overall = "blocked"
    else:
        overall = "ok"

    report = {
        "status": overall,
        "time_budget_seconds": spec.time_budget,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "releases": release_reports,
        "changes": changes,
        "blockers": blockers,
        "fixed_conflicts": solved.fixed_conflicts,
        "warnings": warnings,
    }
    return BatchResult(
        status=overall,
        releases=emitted,
        release_status={n: r["status"] for n, r in release_reports.items()},
        report=report,
        elapsed=report["elapsed_seconds"],
    )
