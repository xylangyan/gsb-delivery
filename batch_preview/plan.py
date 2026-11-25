"""Build the per-release constraint model from a batch specification.

For every release we compute, from the ordered segment list:

* which resolved references are included (directly, or transitively through
  pulled-in footnotes) and which definition identity each of them needs;
* which definition lines travel physically inside the selected segments;
* which literal bracket texts must stay literal (their would-be labels are
  therefore forbidden for any definition line in that release);
* which footnotes get pulled in, and which literal footnote labels the
  renumbering must avoid.

Marks (the approval table) are attached to the reference/definition positions
they identify.  Everything is keyed by original-manual position.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manual import DefInfo, LiteralInfo, Manual, RefInfo
from .model import BatchSpec

# A definition identity: (manual name, definition ordinal).
DefKey = tuple[str, int]
# A marked position: "manual@offset".
MarkId = str


@dataclass
class RefReq:
    """A reference included in a release; needs its definition brought in."""

    ref: RefInfo
    def_key: DefKey
    orig_label: str  # normalized label the reference uses
    mark_id: MarkId | None


@dataclass
class PhysDef:
    """A definition line physically present in a selected segment."""

    dinfo: DefInfo
    def_key: DefKey
    orig_label: str
    mark_id: MarkId | None


@dataclass
class SegmentSlice:
    manual: str
    start: int
    end: int


@dataclass
class PulledFootnote:
    manual: str
    label: str  # normalized label in the original manual
    label_raw: str


@dataclass
class ReleaseModel:
    name: str
    slices: list[SegmentSlice] = field(default_factory=list)
    ref_reqs: list[RefReq] = field(default_factory=list)
    phys_defs: list[PhysDef] = field(default_factory=list)
    forbidden: dict[str, LiteralInfo] = field(default_factory=dict)
    fn_literals: set[str] = field(default_factory=set)
    footnotes: list[PulledFootnote] = field(default_factory=list)
    active_marks: set[MarkId] = field(default_factory=set)
    fixed_conflicts: list[str] = field(default_factory=list)


def mark_id(manual: str, offset: int) -> MarkId:
    return f"{manual}@{offset}"


def build_release(
    name: str,
    segment_names: list[str],
    spec: BatchSpec,
    manuals: dict[str, Manual],
    marks_by_position: dict[tuple[str, int], MarkId],
) -> ReleaseModel:
    model = ReleaseModel(name)

    for seg_name in segment_names:
        seg = spec.segments[seg_name]
        manual = manuals[seg.manual]
        start, end = manual.line_range_offsets(seg.start_line, seg.end_line)
        model.slices.append(SegmentSlice(seg.manual, start, end))

    def inside(manual_name: str, offset_start: int, offset_end: int) -> bool:
        return any(
            s.manual == manual_name and s.start <= offset_start and offset_end <= s.end
            for s in model.slices
        )

    pulled: dict[tuple[str, str], PulledFootnote] = {}
    queue: list[tuple[str, str]] = []

    def include_footnote(manual_name: str, label: str) -> None:
        key = (manual_name, label)
        if key in pulled:
            return
        manual = manuals[manual_name]
        fdef = manual.footnote_defs.get(label)
        if fdef is None:
            return
        pulled[key] = PulledFootnote(manual_name, label, fdef.label_raw)
        queue.append(key)

    # Directly included references and footnote references.
    seen: set[tuple[str, int]] = set()
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        for ref in manual.refs:
            if (
                ref.in_footnote is None
                and inside(slice_.manual, ref.start, ref.end)
                and (slice_.manual, ref.start) not in seen
            ):
                seen.add((slice_.manual, ref.start))
                _add_ref_req(model, ref, marks_by_position)
        for fnref in manual.footnote_refs:
            if fnref.in_footnote is None and inside(
                slice_.manual, fnref.start, fnref.end
            ):
                include_footnote(slice_.manual, fnref.label)
        for d in manual.defs:
            if inside(slice_.manual, d.start, d.end) and (
                slice_.manual,
                d.start,
            ) not in seen:
                seen.add((slice_.manual, d.start))
                mid = marks_by_position.get((slice_.manual, d.start))
                model.phys_defs.append(
                    PhysDef(d, (slice_.manual, d.index), d.label, mid)
                )
                if mid:
                    model.active_marks.add(mid)
        for literal in manual.literals:
            if literal.in_footnote is None and inside(
                slice_.manual, literal.start, literal.end
            ):
                _add_literal(model, literal)

    # Transitively pulled footnote bodies.
    while queue:
        manual_name, label = queue.pop(0)
        manual = manuals[manual_name]
        fdef = manual.footnote_defs[label]
        for ref in manual.refs:
            if ref.in_footnote == fdef.start:
                _add_ref_req(model, ref, marks_by_position)
        for fnref in manual.footnote_refs:
            if fnref.in_footnote == fdef.start:
                include_footnote(manual_name, fnref.label)
        for literal in manual.literals:
            if literal.in_footnote == fdef.start:
                _add_literal(model, literal)

    model.footnotes = list(pulled.values())
    _check_fixed_conflicts(model)
    return model


def _add_ref_req(model, ref, marks_by_position) -> None:
    mid = marks_by_position.get((ref.manual, ref.start))
    model.ref_reqs.append(
        RefReq(ref, (ref.manual, ref.def_index), ref.label, mid)
    )
    if mid:
        model.active_marks.add(mid)


def _add_literal(model, literal: LiteralInfo) -> None:
    if literal.kind == "footnote":
        model.fn_literals.add(literal.label)
    else:
        model.forbidden.setdefault(literal.label, literal)


def _check_fixed_conflicts(model: ReleaseModel) -> None:
    """Decision-independent conflicts: unchangeable text that clashes no
    matter what the approval table decides."""
    fixed: dict[str, tuple[DefKey, str]] = {}  # label -> first claimant

    def claim(label: str, key: DefKey, desc: str) -> None:
        other = fixed.get(label)
        if other is not None and other[0] != key:
            model.fixed_conflicts.append(
                f"标签 {label!r} 在版内冲突: {desc} 与 {other[1]}"
            )
        else:
            fixed[label] = (key, desc)
        if label in model.forbidden:
            literal = model.forbidden[label]
            model.fixed_conflicts.append(
                f"标签 {label!r} 与未获准改动的方括号文字 {literal.raw!r} 冲突"
            )

    for req in model.ref_reqs:
        if req.mark_id is None:
            claim(req.orig_label, req.def_key, f"引用 {req.ref.text!r}")
    for pd in model.phys_defs:
        if pd.mark_id is None:
            claim(pd.orig_label, pd.def_key, f"定义行 [{pd.dinfo.label_raw}]")
