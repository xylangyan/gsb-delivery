"""Column-aware re-extraction of receipt pages with font corrections.

This module adds an optional entry point on top of the regular page
extraction pipeline.  The ordinary content streams of a PDF are interpreted
unchanged, but horizontal text is additionally traced glyph by glyph and
assigned to column windows.  A column window is the half-open interval
``[bound[k-1], bound[k])``; a glyph is placed by the x coordinate of its
origin.

Corrections (encoding splits and Unicode amendments) are registered against
the *actual* font object a glyph is rendered with, never against a resource
name such as ``/F1`` that may resolve to different objects in different form
XObjects.  Every emitted glyph keeps a provenance record that points back to
the text operator, the byte range inside the PDF string and the font object.

The session keeps an immutable snapshot of the column contents for every
published version.  A reader that started with an older version keeps reading
that snapshot while new readers see the version that contains every
correction applied so far.
"""

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import BinaryIO, cast

from pdfminer.pdfcolor import PDFColorSpace
from pdfminer.pdfdevice import PDFTextDevice, PDFTextSeq
from pdfminer.pdffont import PDFFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import (
    PDFGraphicState,
    PDFPageInterpreter,
    PDFResourceManager,
    PDFStackT,
    PDFTextState,
)
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import stream_value
from pdfminer.utils import (
    MATRIX_IDENTITY,
    FileOrName,
    Matrix,
    Rect,
    apply_matrix_pt,
    mult_matrix,
    open_filename,
)

__all__ = [
    "CodeRule",
    "ColumnReissue",
    "CorrectionRegistry",
    "DecodedGlyph",
    "FontIdentity",
    "GlyphRecord",
    "ReceiptColumnSession",
    "UnicodeCandidate",
    "UnicodeConflict",
    "UnicodeRule",
    "extract_receipt_columns",
]


@dataclass(frozen=True)
class DecodedGlyph:
    """A glyph decoded from one PDF string object.

    The byte range is relative to the bytes of that individual string; a
    range never spans two independent string objects.
    """

    cid: int
    byte_range: tuple[int, int]
    width: float
    text: str


@dataclass(frozen=True)
class FontIdentity:
    """Identity of the concrete font dictionary behind a resource name."""

    objid: int | None
    basefont: str


@dataclass(frozen=True)
class CodeRule:
    """Encoding correction for one byte fragment of one font."""

    fragment: bytes
    cids: tuple[int, ...]
    widths: tuple[float, ...]
    characters: tuple[str, ...]


@dataclass(frozen=True)
class UnicodeCandidate:
    text: str
    byte_range: tuple[int, int]


@dataclass
class UnicodeRule:
    """Unicode evidence for one CID.

    ``width`` is recorded in thousandths of the font size.  When it is fixed,
    every candidate reading produces identical glyph positions, so the column
    assignment does not change while the evidence remains unconfirmed.
    """

    cid: int
    candidates: list[UnicodeCandidate]
    width: float | None
    confirmed_text: str | None = None


@dataclass(frozen=True)
class GlyphRecord:
    """One placed glyph with full provenance."""

    page: int
    column: int
    text: str
    cid: int
    width: float
    x0: float
    x1: float
    local_x0: float
    local_x1: float
    figure: str | None
    operator: str
    string_index: int
    string_bytes: bytes
    byte_range: tuple[int, int]
    stream_objid: int | None
    font: FontIdentity


@dataclass(frozen=True)
class ColumnReissue:
    page: int
    column: int
    old_content: str
    new_content: str
    old_version: int
    new_version: int
    glyphs: tuple[GlyphRecord, ...]


@dataclass(frozen=True)
class UnicodeConflict:
    page: int
    column: int
    cid: int
    candidates: tuple[UnicodeCandidate, ...]
    current_text: str
    columns_identical: bool
    glyphs: tuple[GlyphRecord, ...]


class CorrectionRegistry:
    """Holds encoding and Unicode corrections keyed by font object."""

    def __init__(self) -> None:
        self._code_rules: dict[FontIdentity, list[CodeRule]] = {}
        self._unicode_rules: dict[FontIdentity, dict[int, UnicodeRule]] = {}
        self.version = 1

    def register_code_correction(
        self,
        font: FontIdentity,
        fragment: bytes,
        cids: Sequence[int],
        widths: Sequence[float],
        characters: Sequence[str],
    ) -> int:
        if not (len(cids) == len(widths) == len(characters)):
            msg = "cids, widths and characters must have the same length"
            raise ValueError(msg)
        if not fragment:
            msg = "the corrected byte fragment must not be empty"
            raise ValueError(msg)
        rule = CodeRule(
            fragment=bytes(fragment),
            cids=tuple(cids),
            widths=tuple(widths),
            characters=tuple(characters),
        )
        self._code_rules.setdefault(font, []).append(rule)
        self.version += 1
        return self.version

    def add_unicode_evidence(
        self,
        font: FontIdentity,
        cid: int,
        candidates: Sequence[UnicodeCandidate],
        width: float | None,
    ) -> int:
        if len(candidates) < 2:
            msg = "at least two candidate readings are required"
            raise ValueError(msg)
        rules = self._unicode_rules.setdefault(font, {})
        rules[cid] = UnicodeRule(
            cid=cid,
            candidates=list(candidates),
            width=width,
            confirmed_text=None,
        )
        self.version += 1
        return self.version

    def confirm_unicode(self, font: FontIdentity, cid: int, text: str) -> int:
        rules = self._unicode_rules.get(font)
        if rules is None or cid not in rules:
            msg = f"no Unicode evidence registered for cid {cid} on {font!r}"
            raise KeyError(msg)
        rules[cid].confirmed_text = text
        self.version += 1
        return self.version

    def code_rules(self, font: FontIdentity) -> tuple[CodeRule, ...]:
        return tuple(self._code_rules.get(font, ()))

    def unicode_rule(self, font: FontIdentity, cid: int) -> UnicodeRule | None:
        rules = self._unicode_rules.get(font)
        if rules is None:
            return None
        return rules.get(cid)

    def decode_string(
        self,
        font: FontIdentity,
        pdf_font: PDFFont,
        data: bytes,
    ) -> list[DecodedGlyph]:
        """Decode one PDF string object into glyphs.

        Corrected fragments are matched as longest prefixes and may be
        concatenated inside the same string.  Matching never crosses a string
        boundary, so half codes living in independent strings are not joined.
        """
        rules = self.code_rules(font)
        code_size = 2 if pdf_font.is_multibyte() else 1
        glyphs: list[DecodedGlyph] = []
        pos = 0
        while pos < len(data):
            rule = self._match_rule(rules, data, pos)
            if rule is not None:
                for offset, (cid, width, text) in enumerate(
                    zip(rule.cids, rule.widths, rule.characters, strict=True),
                ):
                    glyphs.append(
                        DecodedGlyph(
                            cid=cid,
                            byte_range=(pos + offset, pos + offset + 1),
                            width=width,
                            text=text,
                        ),
                    )
                pos += len(rule.fragment)
            else:
                end = min(pos + code_size, len(data))
                cid = int.from_bytes(data[pos:end], "big")
                glyphs.append(
                    DecodedGlyph(
                        cid=cid,
                        byte_range=(pos, end),
                        width=self._raw_width(pdf_font, cid),
                        text=self._current_text(font, pdf_font, cid),
                    ),
                )
                pos = end
        return glyphs

    @staticmethod
    def _match_rule(
        rules: Sequence[CodeRule],
        data: bytes,
        pos: int,
    ) -> CodeRule | None:
        matched: CodeRule | None = None
        for rule in rules:
            if data.startswith(rule.fragment, pos) and (
                matched is None or len(rule.fragment) > len(matched.fragment)
            ):
                matched = rule
        return matched

    @staticmethod
    def _raw_width(pdf_font: PDFFont, cid: int) -> float:
        width = pdf_font.widths.get(cid)
        if width is None:
            return float(pdf_font.default_width)
        return float(width)

    def _current_text(
        self,
        font: FontIdentity,
        pdf_font: PDFFont,
        cid: int,
    ) -> str:
        rule = self.unicode_rule(font, cid)
        if rule is not None and rule.confirmed_text is not None:
            return rule.confirmed_text
        try:
            return pdf_font.to_unichr(cid)
        except PDFUnicodeNotDefined:
            return f"(cid:{cid})"


class _RecordingResourceManager(PDFResourceManager):
    """Resource manager that remembers the object id behind each font."""

    def __init__(self, caching: bool = True) -> None:
        super().__init__(caching=caching)
        self.font_identities: dict[int, FontIdentity] = {}

    def get_font(self, objid: object, spec: dict[str, object]) -> PDFFont:
        font = super().get_font(objid, spec)
        if isinstance(objid, int):
            self.font_identities[id(font)] = FontIdentity(
                objid=objid,
                basefont=font.fontname,
            )
        return font


class _ColumnTraceDevice(PDFTextDevice):
    """Collects provenance records for horizontal text only."""

    def __init__(
        self,
        rsrcmgr: _RecordingResourceManager,
        registry: CorrectionRegistry,
        bounds: Sequence[float],
    ) -> None:
        super().__init__(rsrcmgr)
        self.registry = registry
        self.bounds = tuple(bounds)
        self.records: list[GlyphRecord] = []
        self.page = 0
        self.current_operator = "TJ"
        self._figures: list[str] = []
        self._streams: list[int | None] = []

    def begin_page(self, page: PDFPage, ctm: Matrix) -> None:
        self.records = []

    def begin_figure(self, name: str, bbox: Rect, matrix: Matrix) -> None:
        self._figures.append(name)

    def end_figure(self, name: str) -> None:
        self._figures.pop()

    def push_streams(self, objids: Sequence[int | None]) -> None:
        self._streams.extend(objids)

    def pop_streams(self, count: int) -> None:
        del self._streams[-count:]

    def render_string(
        self,
        textstate: PDFTextState,
        seq: PDFTextSeq,
        ncs: PDFColorSpace,
        graphicstate: PDFGraphicState,
    ) -> None:
        # The public entry point supports horizontal writing only.  Vertical
        # fonts are skipped rather than mis-assigned to a column window.
        assert self.ctm is not None
        font = textstate.font
        assert font is not None
        if font.is_vertical():
            return
        identity = self._identity(font)
        matrix = mult_matrix(textstate.matrix, self.ctm)
        fontsize = textstate.fontsize
        scaling = textstate.scaling * 0.01
        charspace = textstate.charspace * scaling
        wordspace = textstate.wordspace * scaling
        if font.is_multibyte():
            wordspace = 0
        dxscale = 0.001 * fontsize * scaling
        x, y = textstate.linematrix
        need_charspace = False
        for string_index, obj in enumerate(seq):
            if isinstance(obj, (int, float)):
                x -= obj * dxscale
                need_charspace = True
            elif isinstance(obj, bytes):
                for glyph in self.registry.decode_string(identity, font, obj):
                    if need_charspace:
                        x += charspace
                    x0, _ = apply_matrix_pt(matrix, (x, 0))
                    advance = glyph.width * dxscale
                    x1, _ = apply_matrix_pt(matrix, (x + advance, 0))
                    local_x0, _ = apply_matrix_pt(textstate.matrix, (x, 0))
                    local_x1, _ = apply_matrix_pt(
                        textstate.matrix,
                        (x + advance, 0),
                    )
                    self.records.append(
                        GlyphRecord(
                            page=self.page,
                            column=bisect_right(self.bounds, x0),
                            text=glyph.text,
                            cid=glyph.cid,
                            width=glyph.width,
                            x0=x0,
                            x1=x1,
                            local_x0=local_x0,
                            local_x1=local_x1,
                            figure=self._figures[-1] if self._figures else None,
                            operator=self.current_operator,
                            string_index=string_index,
                            string_bytes=obj,
                            byte_range=glyph.byte_range,
                            stream_objid=self._streams[-1] if self._streams else None,
                            font=identity,
                        ),
                    )
                    x += advance
                    if glyph.cid == 32 and wordspace:
                        x += wordspace
                    need_charspace = True
        textstate.linematrix = (x, y)

    def _identity(self, font: PDFFont) -> FontIdentity:
        rsrcmgr = cast(_RecordingResourceManager, self.rsrcmgr)
        identity = rsrcmgr.font_identities.get(id(font))
        if identity is None:
            identity = FontIdentity(objid=None, basefont=font.fontname)
        return identity


class _ColumnInterpreter(PDFPageInterpreter):
    """Interpreter that records the active content stream id."""

    def render_contents(
        self,
        resources: dict[object, object],
        streams: Sequence[object],
        ctm: Matrix = MATRIX_IDENTITY,
    ) -> None:
        stream_list = list(streams)
        objids = [stream_value(s).objid for s in stream_list]
        device = cast(_ColumnTraceDevice, self.device)
        device.push_streams(objids)
        try:
            super().render_contents(resources, stream_list, ctm=ctm)
        finally:
            device.pop_streams(len(objids))

    def do_TJ(self, seq: PDFStackT) -> None:
        self._trace_text("TJ", lambda: PDFPageInterpreter.do_TJ(self, seq))

    def do_Tj(self, s: PDFStackT) -> None:
        self._trace_text("Tj", lambda: PDFPageInterpreter.do_Tj(self, s))

    def do__q(self, s: PDFStackT) -> None:
        self._trace_text("'", lambda: PDFPageInterpreter.do__q(self, s))

    def do__w(
        self,
        aw: PDFStackT,
        ac: PDFStackT,
        s: PDFStackT,
    ) -> None:
        self._trace_text('"', lambda: PDFPageInterpreter.do__w(self, aw, ac, s))

    def _trace_text(self, operator: str, render: Callable[[], None]) -> None:
        device = cast(_ColumnTraceDevice, self.device)
        previous = device.current_operator
        device.current_operator = operator
        try:
            render()
        finally:
            device.current_operator = previous


def column_contents(records: Sequence[GlyphRecord]) -> dict[tuple[int, int], str]:
    contents: dict[tuple[int, int], list[str]] = {}
    for record in records:
        contents.setdefault((record.page, record.column), []).append(record.text)
    return {key: "".join(chars) for key, chars in contents.items()}


class ReceiptColumnSession:
    """Re-readable column extraction session with versioned snapshots."""

    def __init__(
        self,
        fp: BinaryIO,
        column_bounds: Sequence[float],
        registry: CorrectionRegistry | None = None,
        password: str = "",
        caching: bool = True,
    ) -> None:
        self._fp = fp
        self.bounds = tuple(column_bounds)
        self.registry = registry if registry is not None else CorrectionRegistry()
        self.password = password
        self.caching = caching
        self.records: list[GlyphRecord] = []
        self.parsed_pages: set[int] = set()
        self.reread_pages: set[int] = set()
        self.snapshots: dict[int, dict[tuple[int, int], str]] = {}
        self.current_version = 0
        self._owns_handle = False

    def read(self, page_numbers: Sequence[int] | None = None) -> int:
        """Read the requested pages (all pages by default) and publish v1."""
        wanted = set(page_numbers) if page_numbers is not None else None
        self.records, self.parsed_pages = self._run_pass(wanted)
        self.reread_pages = set()
        self.current_version = self.registry.version
        self.snapshots = {self.current_version: column_contents(self.records)}
        return self.current_version

    def contents(
        self,
        version: int | None = None,
    ) -> dict[tuple[int, int], str]:
        """Return an immutable copy of one published version's contents."""
        version = self.current_version if version is None else version
        return dict(self.snapshots[version])

    def glyphs(
        self,
        page: int,
        column: int | None = None,
    ) -> tuple[GlyphRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.page == page
            and (column is None or record.column == column)
        )

    def apply_code_correction(
        self,
        font: FontIdentity,
        fragment: bytes,
        cids: Sequence[int],
        widths: Sequence[float],
        characters: Sequence[str],
    ) -> tuple[ColumnReissue, ...]:
        old_version = self.current_version
        new_version = self.registry.register_code_correction(
            font,
            fragment,
            cids,
            widths,
            characters,
        )
        return self._reread(font, old_version, new_version)

    def add_unicode_evidence(
        self,
        font: FontIdentity,
        cid: int,
        candidates: Sequence[UnicodeCandidate],
        width: float | None,
    ) -> UnicodeConflict:
        existing = [
            record
            for record in self.records
            if record.font == font and record.cid == cid
        ]
        if not existing:
            msg = f"no glyph for cid {cid} on {font!r} has been extracted"
            raise KeyError(msg)
        before_columns = {(record.page, record.column) for record in existing}
        self.registry.add_unicode_evidence(font, cid, candidates, width)
        # Evidence without a confirmation changes no text or position, so the
        # re-read keeps every column assignment identical.
        self._reread(font, self.current_version, self.registry.version)
        after_columns = {
            (record.page, record.column)
            for record in self.records
            if record.font == font and record.cid == cid
        }
        conflict_glyphs = tuple(
            record
            for record in self.records
            if record.font == font and record.cid == cid
        )
        first = conflict_glyphs[0]
        return UnicodeConflict(
            page=first.page,
            column=first.column,
            cid=cid,
            candidates=tuple(candidates),
            current_text=first.text,
            columns_identical=before_columns == after_columns,
            glyphs=conflict_glyphs,
        )

    def confirm_unicode(
        self,
        font: FontIdentity,
        cid: int,
        text: str,
    ) -> tuple[ColumnReissue, ...]:
        old_version = self.current_version
        new_version = self.registry.confirm_unicode(font, cid, text)
        return self._reread(font, old_version, new_version)

    def close(self) -> None:
        if self._owns_handle:
            self._fp.close()

    def _reread(
        self,
        font: FontIdentity,
        old_version: int,
        new_version: int,
    ) -> tuple[ColumnReissue, ...]:
        affected_pages = {
            record.page for record in self.records if record.font == font
        }
        old_snapshot = self.snapshots[old_version]
        if not affected_pages:
            self.reread_pages = set()
            if new_version != old_version:
                self.snapshots[new_version] = dict(old_snapshot)
                self.current_version = new_version
            return ()
        new_records, parsed = self._run_pass(affected_pages)
        self.reread_pages = parsed
        self.records = [
            record for record in self.records if record.page not in affected_pages
        ]
        self.records.extend(new_records)
        self.parsed_pages |= parsed
        new_snapshot = column_contents(self.records)
        reissues = self._diff(
            affected_pages,
            old_snapshot,
            new_snapshot,
            old_version,
            new_version,
        )
        self.snapshots[new_version] = new_snapshot
        self.current_version = new_version
        return tuple(reissues)

    def _diff(
        self,
        pages: set[int],
        old_snapshot: dict[tuple[int, int], str],
        new_snapshot: dict[tuple[int, int], str],
        old_version: int,
        new_version: int,
    ) -> list[ColumnReissue]:
        keys = {
            key
            for key in old_snapshot.keys() | new_snapshot.keys()
            if key[0] in pages
        }
        reissues: list[ColumnReissue] = []
        for page, column in sorted(keys):
            old_content = old_snapshot.get((page, column), "")
            new_content = new_snapshot.get((page, column), "")
            if old_content == new_content:
                continue
            reissues.append(
                ColumnReissue(
                    page=page,
                    column=column,
                    old_content=old_content,
                    new_content=new_content,
                    old_version=old_version,
                    new_version=new_version,
                    glyphs=tuple(
                        record
                        for record in self.records
                        if record.page == page and record.column == column
                    ),
                ),
            )
        return reissues

    def _run_pass(
        self,
        page_numbers: set[int] | None,
    ) -> tuple[list[GlyphRecord], set[int]]:
        rsrcmgr = _RecordingResourceManager(caching=self.caching)
        device = _ColumnTraceDevice(rsrcmgr, self.registry, self.bounds)
        interpreter = _ColumnInterpreter(rsrcmgr, device)
        records: list[GlyphRecord] = []
        parsed: set[int] = set()
        for page_index, page in enumerate(
            PDFPage.get_pages(
                self._fp,
                password=self.password,
                caching=self.caching,
            ),
        ):
            if page_numbers is not None and page_index not in page_numbers:
                continue
            device.page = page_index
            parsed.add(page_index)
            interpreter.process_page(page)
            records.extend(device.records)
        return records, parsed


def extract_receipt_columns(
    pdf_file: FileOrName,
    column_bounds: Sequence[float],
    registry: CorrectionRegistry | None = None,
    password: str = "",
    page_numbers: Sequence[int] | None = None,
    caching: bool = True,
) -> ReceiptColumnSession:
    """Extract horizontal text into column windows with corrections.

    This is an optional alternative to :func:`pdfminer.high_level.extract_pages`.
    The PDF content streams are interpreted normally; in addition every
    horizontal glyph is assigned to the half-open column window whose bounds
    contain its origin, together with a provenance record covering the text
    operator, the in-string byte range and the concrete font object.

    :param pdf_file: file path or binary file object of the PDF.
    :param column_bounds: ascending x coordinates separating the columns, e.g.
        ``(12.0,)`` defines a left column ``(-inf, 12)`` and a right column
        ``[12, +inf)``.
    :param registry: optional shared :class:`CorrectionRegistry`.
    :returns: a :class:`ReceiptColumnSession` holding the first published
        version and supporting selective re-reads after corrections.
    """
    context = open_filename(pdf_file, "rb")
    fp = cast(BinaryIO, context.__enter__())
    session = ReceiptColumnSession(
        fp,
        column_bounds,
        registry=registry,
        password=password,
        caching=caching,
    )
    session._owns_handle = context.closing
    try:
        session.read(page_numbers)
    except BaseException:
        context.__exit__(None, None, None)
        raise
    return session
