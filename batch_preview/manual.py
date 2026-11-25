"""Analyse one manual with Marko.

Parsing a whole manual with Marko (plus the footnote extension) tells us
exactly which definitions were *actually in effect* for every reference, in
line with CommonMark's first-definition-wins rule.  On top of the parsed AST
we recover, for every link/image reference, the label it used (taken from the
source slice, since the AST only keeps the resolved destination), and we scan
the remaining text for bracket constructs that stayed literal because no
definition matched them.

Everything is keyed by original-manual offsets so that two definitions that
happen to carry the same address stay distinguishable by position.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

import marko
from marko.ext import footnote as _footnote_ext  # noqa: F401  (registered by name)
from marko.helpers import normalize_label
from marko.inline_parser import _parse_link_label, _UNESCAPED_BRACKET

_FOOTNOTE_DEF_RE = re.compile(r" {,3}\[\^([^\]]+)\]:[^\n\S]*")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")

# Inline element types whose source spans can never contain live references.
_INERT_INLINE = {"CodeSpan", "InlineHTML", "AutoLink", "Literal"}
# Block element types whose whole span is inert for our purposes.
_INERT_BLOCK = {"CodeBlock", "FencedCode", "HTMLBlock"}


@dataclass
class DefInfo:
    """One link reference definition line of the manual."""

    manual: str
    index: int  # ordinal among the manual's definitions
    start: int
    end: int
    label: str  # normalized
    label_raw: str
    dest: str
    title: str | None
    effective: bool  # False when shadowed by an earlier same-label definition


@dataclass
class RefInfo:
    """One resolved link/image reference (not inline links)."""

    manual: str
    start: int
    end: int
    kind: str  # "shortcut" | "collapsed" | "full"
    image: bool
    text: str  # display text as written
    label: str  # normalized lookup label
    label_raw: str
    def_index: int  # index of the effective definition it resolves to
    in_footnote: int | None = None  # start offset of enclosing footnote def


@dataclass
class LinkInfo:
    """Any Link/Image element (reference or inline), for verification."""

    start: int
    end: int
    image: bool
    dest: str
    display: str
    in_footnote: int | None = None


@dataclass
class FootnoteDefInfo:
    label: str  # normalized
    label_raw: str
    start: int
    end: int
    content_start: int  # offset just after the "[^label]:" prefix


@dataclass
class FootnoteRefInfo:
    start: int
    end: int
    label: str  # normalized
    label_raw: str
    in_footnote: int | None = None


@dataclass
class LiteralInfo:
    """Bracket text that renders literally because no definition matched."""

    start: int
    end: int
    kind: str  # "link" | "image" | "footnote"
    label: str  # normalized lookup label
    raw: str
    in_footnote: int | None = None
    manual: str = ""


def _plain_text(element) -> str:
    children = getattr(element, "children", None)
    if isinstance(children, str):
        return children
    if isinstance(children, list):
        return "".join(_plain_text(c) for c in children)
    return ""


class Manual:
    """A parsed manual with every reference, definition and literal located."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        # Marko normalizes line endings before parsing; positions reported on
        # the AST refer to the normalized text, so we do the same up front.
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        md = marko.Markdown(extensions=["footnote"])
        self.doc = md.parse(self.text)

        self.defs: list[DefInfo] = []
        self.refs: list[RefInfo] = []
        self.links: list[LinkInfo] = []
        self.footnote_defs: dict[str, FootnoteDefInfo] = {}
        self.footnote_refs: list[FootnoteRefInfo] = []
        self.literals: list[LiteralInfo] = []

        self._collect()
        self._scan_literals()
        self._index()

    # ------------------------------------------------------------------ AST

    def _collect(self) -> None:
        text = self.text
        excluded: list[tuple[int, int]] = []
        fn_def_spans: list[FootnoteDefInfo] = []
        raw_links: list[tuple[int, int, bool, str, str]] = []

        def walk(el) -> None:
            typename = type(el).__name__
            span = getattr(el, "source_span", None)
            if typename in ("Link", "Image") and span:
                raw_links.append(
                    (span[0], span[1], typename == "Image", el.dest, _plain_text(el))
                )
                excluded.append(span)
                return  # link text cannot contain live references
            if typename == "FootnoteRef" and span:
                raw = text[span[0] : span[1]]
                m = _FOOTNOTE_REF_RE.fullmatch(raw)
                label_raw = m.group(1) if m else el.label
                self.footnote_refs.append(
                    FootnoteRefInfo(span[0], span[1], el.label, label_raw)
                )
                excluded.append(span)
                return
            if typename in _INERT_INLINE and span:
                excluded.append(span)
                return
            if typename in _INERT_BLOCK and span:
                excluded.append(span)
                return
            if typename == "LinkRefDef" and span:
                self._add_def(el, span)
                excluded.append(span)
                return
            if typename == "FootnoteDef" and span:
                m = _FOOTNOTE_DEF_RE.match(text, span[0])
                content_start = m.end() if m else span[0]
                fn_def_spans.append(
                    FootnoteDefInfo(
                        el.label,
                        m.group(1) if m else el.label,
                        span[0],
                        span[1],
                        content_start,
                    )
                )
                excluded.append((span[0], content_start))
                # fall through: the footnote body can contain references
            children = getattr(el, "children", None)
            if isinstance(children, list):
                for child in children:
                    walk(child)

        walk(self.doc)

        # Effective footnote definitions: mirror Marko's own table.
        for label, element in self.doc.footnotes.items():
            for info in fn_def_spans:
                if info.start == element.source_span[0]:
                    self.footnote_defs[label] = info
                    break

        def enclosing_footnote(start: int) -> int | None:
            for info in fn_def_spans:
                if info.content_start <= start < info.end:
                    return info.start
            return None

        # Classify every Link/Image element from its source slice.
        for start, end, image, dest, display in raw_links:
            in_fn = enclosing_footnote(start)
            self.links.append(LinkInfo(start, end, image, dest, display, in_fn))
            ref = self._classify_ref(start, end, image)
            if ref is not None:
                kind, label_raw = ref
                label = normalize_label(label_raw)
                def_index = self._effective_def_index(label)
                if def_index is None:
                    # A reference Marko resolved but we cannot attribute; this
                    # should not happen, treat it as unverifiable inline text.
                    continue
                self.refs.append(
                    RefInfo(
                        manual=self.name,
                        start=start,
                        end=end,
                        kind=kind,
                        image=image,
                        text=display,
                        label=label,
                        label_raw=label_raw,
                        def_index=def_index,
                        in_footnote=in_fn,
                    )
                )

        for fnref in self.footnote_refs:
            fnref.in_footnote = enclosing_footnote(fnref.start)

        self._excluded = sorted(excluded)
        self._fn_def_spans = fn_def_spans
        self._enclosing_footnote = enclosing_footnote

    def _add_def(self, element, span: tuple[int, int]) -> None:
        text = self.text
        bracket = text.find("[", span[0], min(span[0] + 4, span[1]))
        label_raw = ""
        if bracket != -1:
            group = _parse_link_label(text, bracket)
            if group is not None and group.text:
                label_raw = group.text[1:-1]
        index = len(self.defs)
        effective = not any(d.label == element.label for d in self.defs)
        self.defs.append(
            DefInfo(
                manual=self.name,
                index=index,
                start=span[0],
                end=span[1],
                label=element.label,
                label_raw=label_raw,
                dest=element.dest,
                title=element.title,
                effective=effective,
            )
        )

    def _effective_def_index(self, label: str) -> int | None:
        for d in self.defs:
            if d.label == label and d.effective:
                return d.index
        return None

    def _classify_ref(
        self, start: int, end: int, image: bool
    ) -> tuple[str, str] | None:
        """Recover (kind, lookup label) for a resolved Link/Image element."""
        text = self.text
        pos = start + 1 if image and text[start] == "!" else start
        group = _parse_link_label(text, pos)
        if group is None or group.text is None:
            return None
        link_text = group.text[1:-1]
        if group.end < end and text[group.end] == "[":
            if text[group.end : group.end + 2] == "[]" and group.end + 2 == end:
                return ("collapsed", link_text)
            second = _parse_link_label(text, group.end)
            if second is not None and second.text is not None and second.end == end:
                return ("full", second.text[1:-1])
            return None
        if group.end < end and text[group.end] == "(":
            return None  # inline link, no label involved
        if group.end == end:
            return ("shortcut", link_text)
        return None

    # -------------------------------------------------------------- literals

    def _scan_literals(self) -> None:
        text = self.text
        excluded = self._excluded
        n = len(text)
        i = 0
        ex = 0
        while i < n:
            while ex < len(excluded) and excluded[ex][1] <= i:
                ex += 1
            if ex < len(excluded) and excluded[ex][0] <= i < excluded[ex][1]:
                i = excluded[ex][1]
                continue
            char = text[i]
            image = False
            if char == "!" and i + 1 < n and text[i + 1] == "[":
                j = i + 1
                image = True
            elif char == "[":
                j = i
            else:
                i += 1
                continue
            if not image and text.startswith("[^", j):
                m = _FOOTNOTE_REF_RE.match(text, j)
                if m:
                    self.literals.append(
                        LiteralInfo(
                            i,
                            m.end(),
                            "footnote",
                            normalize_label(m.group(1)),
                            text[i : m.end()],
                        )
                    )
                    i = m.end()
                else:
                    i = j + 2
                continue
            group = _parse_link_label(text, j)
            if group is None or group.text is None:
                i = j + 1
                continue
            link_text = group.text[1:-1]
            end = group.end
            lookup = link_text
            if end < n and text[end] == "[":
                if text[end : end + 2] == "[]":
                    end += 2  # collapsed reference: lookup stays the link text
                else:
                    second = _parse_link_label(text, end)
                    if second is not None and second.text is not None:
                        inner = second.text[1:-1]
                        lookup = inner if inner else link_text
                        end = second.end
            # A following "(" can never start a valid inline link here (those
            # spans are excluded already), so the label stays a shortcut.
            if not lookup.strip() or _UNESCAPED_BRACKET.search(lookup):
                i = j + 1
                continue
            self.literals.append(
                LiteralInfo(
                    i,
                    end,
                    "image" if image else "link",
                    normalize_label(lookup),
                    text[i:end],
                )
            )
            i = end

        for literal in self.literals:
            literal.in_footnote = self._enclosing_footnote(literal.start)
            literal.manual = self.name

    # ---------------------------------------------------------------- index

    def _index(self) -> None:
        self._ref_starts = [r.start for r in self.refs]
        self._def_starts = [d.start for d in self.defs]
        self._line_starts = [0]
        for m in re.finditer("\n", self.text):
            self._line_starts.append(m.end())

    # -------------------------------------------------------------- queries

    def line_col(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self._line_starts, offset)
        col = offset - self._line_starts[line - 1] + 1
        return line, col

    def offset_of(self, line: int, col: int) -> int:
        if line < 1 or line > len(self._line_starts):
            raise ValueError(f"{self.name}: line {line} out of range")
        return self._line_starts[line - 1] + col - 1

    def line_range_offsets(self, start_line: int, end_line: int) -> tuple[int, int]:
        lines = self.text.split("\n")
        total = len(lines)
        if start_line < 1 or end_line > total:
            raise ValueError(
                f"{self.name}: line range {start_line}-{end_line} out of 1-{total}"
            )
        start = self._line_starts[start_line - 1]
        if end_line >= total:
            end = len(self.text)
        else:
            end = self._line_starts[end_line]  # start of the line after
        return start, end

    def find_ref_at(self, offset: int) -> RefInfo | None:
        for ref in self.refs:
            if ref.start == offset or (ref.image and ref.start + 1 == offset):
                return ref
        return None

    def find_def_at(self, offset: int) -> DefInfo | None:
        for d in self.defs:
            if d.start == offset:
                return d
            # allow pointing at the "[" after up to 3 leading spaces
            bracket = self.text.find("[", d.start, min(d.start + 4, d.end))
            if bracket == offset:
                return d
        return None

    def footnote_ref_at(self, offset: int) -> FootnoteRefInfo | None:
        for ref in self.footnote_refs:
            if ref.start == offset:
                return ref
        return None
