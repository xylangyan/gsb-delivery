"""Emit release Markdown and verify it by re-parsing with Marko.

The emitted document keeps every selected segment verbatim except for the
approved rewrites (reference labels and definition labels at marked
positions).  Footnote references are renumbered per release -- their display
numbers may follow the release -- while link definitions that the included
references need are appended at the end, so the result can be read by any
existing footnote extension.

After emission the text is parsed again with Marko and checked against the
expectations derived from the original manuals: every included reference must
still resolve to the destination of its own original definition (definitions
are tracked by original position, never merged by address), every literal
bracket text must still be literal, and every footnote reference must have a
definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import marko
from marko.helpers import normalize_label
from marko.inline_parser import _parse_link_label

from .manual import Manual, RefInfo, _plain_text
from .plan import DefKey, MarkId, ReleaseModel


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DefLineReport:
    manual: str
    def_index: int
    label: str
    dest: str
    origin: str  # "segment" | "appended"


@dataclass
class EmittedRelease:
    name: str
    text: str
    definitions: list[DefLineReport] = field(default_factory=list)
    footnotes: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _rewrite_ref(text: str, ref: RefInfo, new_label: str) -> str:
    raw = text[ref.start : ref.end]
    if ref.kind == "shortcut":
        return f"{raw}[{new_label}]"
    if ref.kind == "collapsed":
        return f"{raw[:-2]}[{new_label}]"
    # full reference: replace the second label, keep the display text
    pos = ref.start + 1 if ref.image else ref.start
    first = _parse_link_label(text, pos)
    second = _parse_link_label(text, first.end)
    return f"{text[ref.start:second.start]}[{new_label}]{text[second.end:ref.end]}"


def _rewrite_def_label(text: str, start: int, end: int, new_label: str) -> str:
    bracket = text.find("[", start, min(start + 4, end))
    group = _parse_link_label(text, bracket)
    return f"{text[start:bracket]}[{new_label}]{text[group.end:end]}"


def _apply_edits(text: str, base: int, edits: list[tuple[int, int, str]]) -> str:
    out = text
    for start, end, new in sorted(edits, reverse=True):
        out = out[: start - base] + new + out[end - base :]
    return out


class _FootnoteNumbers:
    """Assigns fresh footnote labels for one release, avoiding labels that
    appear as literal footnote-looking text in the selected segments."""

    def __init__(self, avoid: set[str]) -> None:
        self.avoid = avoid
        self.map: dict[tuple[str, str], str] = {}
        self.counter = 0

    def label_for(self, manual: str, label: str) -> str:
        key = (manual, label)
        if key not in self.map:
            while True:
                self.counter += 1
                candidate = f"fn{self.counter}"
                if (
                    normalize_label(candidate) not in self.avoid
                    and candidate not in self.map.values()
                ):
                    break
            self.map[key] = candidate
        return self.map[key]


def emit_release(
    name: str,
    model: ReleaseModel,
    assignment: dict[str, str],
    manuals: dict[str, Manual],
    variables: dict[MarkId, object] | None = None,
) -> EmittedRelease:
    numbers = _FootnoteNumbers(model.fn_literals)

    def raw_label(mark_id: MarkId, normalized: str) -> str:
        # Label matching is case-insensitive, but the release writes the
        # replacement exactly as approved in the table.
        if variables is None or mark_id not in variables:
            return normalized
        return variables[mark_id].candidate_raw.get(normalized, normalized)

    # Pre-assign footnote labels in body order so numbering follows the
    # release's reading order.
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        for fnref in manual.footnote_refs:
            if fnref.in_footnote is None and slice_.start <= fnref.start < slice_.end:
                numbers.label_for(slice_.manual, fnref.label)
    for pulled in model.footnotes:
        fdef = manuals[pulled.manual].footnote_defs[pulled.label]
        for fnref in manuals[pulled.manual].footnote_refs:
            if fnref.in_footnote == fdef.start:
                numbers.label_for(pulled.manual, fnref.label)

    # Collect text edits per manual, keyed by span so overlapping segments
    # cannot queue the same rewrite twice.
    edits: dict[str, dict[tuple[int, int], str]] = {m: {} for m in manuals}
    decisions = assignment  # mark_id -> new label (changed positions only)

    def add_edit(manual_name: str, start: int, end: int, new: str) -> None:
        edits[manual_name][(start, end)] = new

    for req in model.ref_reqs:
        if req.mark_id is None:
            continue
        new_label = decisions.get(req.mark_id)
        if new_label is None:
            continue
        ref = req.ref
        add_edit(
            ref.manual,
            ref.start,
            ref.end,
            _rewrite_ref(
                manuals[ref.manual].text, ref, raw_label(req.mark_id, new_label)
            ),
        )
    for pd in model.phys_defs:
        if pd.mark_id is None:
            continue
        new_label = decisions.get(pd.mark_id)
        if new_label is None:
            continue
        d = pd.dinfo
        add_edit(
            d.manual,
            d.start,
            d.end,
            _rewrite_def_label(
                manuals[d.manual].text, d.start, d.end, raw_label(pd.mark_id, new_label)
            ),
        )
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        for fnref in manual.footnote_refs:
            if fnref.in_footnote is None and slice_.start <= fnref.start < slice_.end:
                new = numbers.label_for(slice_.manual, fnref.label)
                add_edit(slice_.manual, fnref.start, fnref.end, f"[^{new}]")
    for pulled in model.footnotes:
        manual = manuals[pulled.manual]
        fdef = manual.footnote_defs[pulled.label]
        for fnref in manual.footnote_refs:
            if fnref.in_footnote == fdef.start:
                new = numbers.label_for(pulled.manual, fnref.label)
                add_edit(pulled.manual, fnref.start, fnref.end, f"[^{new}]")

    # Assemble segment text.
    parts: list[str] = []
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        raw = manual.text[slice_.start : slice_.end]
        local = [
            (s, e, t)
            for (s, e), t in edits[slice_.manual].items()
            if slice_.start <= s and e <= slice_.end
        ]
        parts.append(_apply_edits(raw, slice_.start, local).strip("\n"))
    body = "\n\n".join(p for p in parts if p)

    # Definitions that must be brought in: required (identity, label) pairs
    # not already travelling physically inside a segment.
    lines = _release_lines(model, decisions)
    physical: set[tuple[DefKey, str]] = set()
    definitions: list[DefLineReport] = []
    for pd in model.phys_defs:
        label = pd.orig_label
        if pd.mark_id is not None and decisions.get(pd.mark_id):
            label = decisions[pd.mark_id]
        physical.add((pd.def_key, label))
        definitions.append(
            DefLineReport(
                pd.def_key[0], pd.def_key[1], label, pd.dinfo.dest, "segment"
            )
        )
    def_marks = {
        v.def_key: mid
        for mid, v in (variables or {}).items()
        if getattr(v, "kind", None) == "def"
    }
    appended: list[str] = []
    for (def_key, label) in lines:
        if (def_key, label) in physical:
            continue
        dinfo = manuals[def_key[0]].defs[def_key[1]]
        raw = manuals[def_key[0]].text[dinfo.start : dinfo.end].rstrip("\n")
        if label != dinfo.label:
            display = label
            def_mark = def_marks.get(def_key)
            if def_mark is not None:
                display = raw_label(def_mark, label)
            raw = _rewrite_def_label(
                manuals[def_key[0]].text, dinfo.start, dinfo.end, display
            ).rstrip("\n")
        appended.append(raw)
        definitions.append(
            DefLineReport(def_key[0], def_key[1], label, dinfo.dest, "appended")
        )

    # Pulled footnote definitions, renumbered, with inner rewrites applied.
    footnote_blocks: list[str] = []
    footnote_report: list[dict] = []
    for pulled in model.footnotes:
        manual = manuals[pulled.manual]
        fdef = manual.footnote_defs[pulled.label]
        new = numbers.label_for(pulled.manual, pulled.label)
        raw = manual.text[fdef.content_start : fdef.end].rstrip("\n")
        local = [
            (s, e, t)
            for (s, e), t in edits[pulled.manual].items()
            if fdef.content_start <= s and e <= fdef.end
        ]
        content = _apply_edits(raw, fdef.content_start, local)
        content_lines = content.split("\n")
        first = content_lines[0].lstrip()
        rest = content_lines[1:]
        block = f"[^{new}]: {first}"
        if rest:
            block += "\n" + "\n".join(rest)
        footnote_blocks.append(block)
        footnote_report.append(
            {"manual": pulled.manual, "original": pulled.label_raw, "label": new}
        )

    text = body
    if appended:
        text += "\n\n" + "\n".join(appended)
    if footnote_blocks:
        text += "\n\n" + "\n\n".join(footnote_blocks)
    text += "\n"

    emitted = EmittedRelease(
        name=name,
        text=text,
        definitions=definitions,
        footnotes=footnote_report,
    )
    emitted.checks = _verify(emitted, model, decisions, manuals, lines)
    return emitted


def _release_lines(
    model: ReleaseModel, decisions: dict[str, str]
) -> list[tuple[DefKey, str]]:
    """Required (definition identity, label) pairs, in first-need order."""
    seen: set[tuple[DefKey, str]] = set()
    ordered: list[tuple[DefKey, str]] = []

    def add(key: DefKey, label: str) -> None:
        if (key, label) not in seen:
            seen.add((key, label))
            ordered.append((key, label))

    for req in model.ref_reqs:
        label = req.orig_label
        if req.mark_id is not None and decisions.get(req.mark_id):
            label = decisions[req.mark_id]
        add(req.def_key, label)
    for pd in model.phys_defs:
        label = pd.orig_label
        if pd.mark_id is not None and decisions.get(pd.mark_id):
            label = decisions[pd.mark_id]
        add(pd.def_key, label)
    return ordered


def _verify(
    emitted: EmittedRelease,
    model: ReleaseModel,
    decisions: dict[str, str],
    manuals: dict[str, Manual],
    lines: list[tuple[DefKey, str]],
) -> list[Check]:
    checks: list[Check] = []
    doc = marko.Markdown(extensions=["footnote"]).parse(emitted.text)

    # 1. Every required definition is present with its original destination.
    missing = []
    for def_key, label in lines:
        found = doc.link_ref_defs.get(normalize_label(label))
        dinfo = manuals[def_key[0]].defs[def_key[1]]
        if found is None or found[0] != dinfo.dest:
            missing.append(f"[{label}] -> {dinfo.dest} ({def_key[0]}#{def_key[1]})")
    checks.append(
        Check(
            "definitions",
            not missing,
            "全部定义可按原册位置核对"
            if not missing
            else "缺失/不符: " + "; ".join(missing),
        )
    )

    # 2. No forbidden label got defined (literal bracket texts stay literal).
    leaked = sorted(l for l in model.forbidden if l in doc.link_ref_defs)
    checks.append(
        Check(
            "literals",
            not leaked,
            "方括号文字保持原样"
            if not leaked
            else "被意外激活的标签: " + ", ".join(leaked),
        )
    )

    # 3. Every footnote reference resolves.
    unresolved: list[str] = []

    def walk(el) -> None:
        typename = type(el).__name__
        if typename == "FootnoteRef":
            if el.label not in doc.footnotes:
                unresolved.append(el.label)
            return
        children = getattr(el, "children", None)
        if isinstance(children, list):
            for child in children:
                walk(child)

    walk(doc)
    checks.append(
        Check(
            "footnotes",
            not unresolved,
            "脚注引用均有定义"
            if not unresolved
            else "未定义: " + ", ".join(unresolved),
        )
    )

    # 4. The links/images of the release are exactly the expected ones.
    expected: list[tuple[bool, str, str]] = []
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        for link in manual.links:
            if link.in_footnote is None and slice_.start <= link.start < slice_.end:
                expected.append((link.image, link.display, link.dest))
    for pulled in model.footnotes:
        manual = manuals[pulled.manual]
        fdef = manual.footnote_defs[pulled.label]
        for link in manual.links:
            if link.in_footnote == fdef.start:
                expected.append((link.image, link.display, link.dest))
    actual: list[tuple[bool, str, str]] = []

    def walk_links(el) -> None:
        typename = type(el).__name__
        if typename in ("Link", "Image"):
            actual.append((typename == "Image", _plain_text(el), el.dest))
            return
        children = getattr(el, "children", None)
        if isinstance(children, list):
            for child in children:
                walk_links(child)

    walk_links(doc)
    checks.append(
        Check(
            "links",
            sorted(expected) == sorted(actual),
            "链接与图片逐一对应"
            if sorted(expected) == sorted(actual)
            else f"期望 {sorted(expected)!r} 实际 {sorted(actual)!r}",
        )
    )

    # 5. Literal bracket texts still appear verbatim.
    lost = []
    for slice_ in model.slices:
        manual = manuals[slice_.manual]
        for literal in manual.literals:
            if (
                literal.in_footnote is None
                and slice_.start <= literal.start
                and literal.end <= slice_.end
                and literal.raw not in emitted.text
            ):
                lost.append(literal.raw)
    for pulled in model.footnotes:
        manual = manuals[pulled.manual]
        fdef = manual.footnote_defs[pulled.label]
        for literal in manual.literals:
            if literal.in_footnote == fdef.start and literal.raw not in emitted.text:
                lost.append(literal.raw)
    checks.append(
        Check(
            "verbatim",
            not lost,
            "方括号文字原样可见"
            if not lost
            else "丢失: " + ", ".join(sorted(set(lost))),
        )
    )
    return checks
