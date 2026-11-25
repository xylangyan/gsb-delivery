"""
    pyte.ledger
    ~~~~~~~~~~~

    Record extraction for captured terminal byte streams.

    A :class:`Ledger` feeds an :class:`~pyte.archive.Archive` through a real
    :class:`~pyte.screens.HistoryScreen` and turns every *forward* scroll of
    the current scrolling region into one :class:`Record`.  The record keeps
    the line which was at the top of the scrolling region immediately before
    the scroll, exactly as it was rendered, together with per-cell byte
    provenance.

    Only :meth:`~pyte.screens.Screen.index` scrolls produce records.  Paging
    through history, :meth:`~pyte.screens.Screen.reverse_index` and
    ``CSI 3 J`` do not, and records already taken survive a later
    ``CSI 3 J`` or ``RIS``.

    Records display the actual grid contents.  A cell remembers the archive
    segment and occurrence which last wrote it, the exact byte span in the
    reconstructed stream, and the span of the stored template bytes.  Since
    this information lives on the character itself, characters shifted by
    ``ICH``/``DCH`` or moved by scrolling keep their provenance.

    The ledger never expands a long run of a repeated template one occurrence
    at a time.  It probes a few occurrences, detects the periodic screen
    transition, and stores one template cycle plus the occurrence count.  Thus
    ``len(ledger)`` and ``ledger.tail(n)`` are cheap even for runs with
    hundreds of millions of repeats.
"""
from __future__ import annotations

import bisect
import codecs
import unicodedata
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING, Any, Iterator, NamedTuple, Sequence
)

from . import control as ctrl, modes as mo
from .archive import Archive, ArchiveError, Segment
from .screens import Char, HistoryScreen, Margins, grapheme_clusters, wcswidth
from .streams import Stream

if TYPE_CHECKING:
    from collections.abc import Callable

_STREAM_SPECIAL = frozenset(
    {ctrl.ESC, ctrl.CSI_C1, ctrl.NUL, ctrl.DEL, ctrl.OSC_C1}
    | set(Stream.basic))

#: Runs longer than this are summarised instead of being fed occurrence by
#: occurrence.
DEFAULT_FAST_THRESHOLD = 1024

#: Upper bound on occurrences probed while looking for a periodic run.  The
#: screen state after one occurrence can have a period as long as the line
#: width (a short template straddling line boundaries), so the probe must be
#: comfortably longer than the screen width.
_MAX_PROBE = 2048

#: Fallback limit for a non-periodic run which is still too long.
_HARD_LIMIT = 1_000_000


class LedgerError(ArchiveError):
    """A capture cannot be turned into records."""


class CellRecord(NamedTuple):
    """One effective grid cell of a record line.

    :param int x: zero-based column.
    :param str data: character in the cell (empty for a wide-char stub).
    :param int | None source: archive segment index.
    :param int | None occurrence: one-based occurrence of that segment.
    :param int | None start: exact first byte in the reconstructed stream.
    :param int | None end: one past the last byte in the reconstructed stream.
    :param int | None stored_start: first stored byte in the archive file.
    :param int | None stored_end: one past the last stored byte.
    :param str fg: foreground colour.
    :param str bg: background colour.
    :param bool bold: bold flag.
    :param bool italics: italics flag.
    :param bool underscore: underline flag.
    :param bool strikethrough: strike-through flag.
    :param bool reverse: reverse flag.
    :param bool blink: blink flag.
    """

    x: int
    data: str
    source: int | None = None
    occurrence: int | None = None
    start: int | None = None
    end: int | None = None
    stored_start: int | None = None
    stored_end: int | None = None
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False

    @property
    def byte_range(self) -> tuple[int | None, int | None]:
        return self.start, self.end

    @property
    def stored_range(self) -> tuple[int | None, int | None]:
        return self.stored_start, self.stored_end

    @property
    def template_range(self) -> tuple[int | None, int | None]:
        return self.stored_start, self.stored_end

    @property
    def template_index(self) -> int | None:
        return self.source

    @property
    def repeat(self) -> int | None:
        return self.occurrence

    @property
    def offset(self) -> int | None:
        if self.start is None or self.stored_start is None:
            return None
        return self.stored_start - self.start

    @property
    def effective(self) -> bool:
        """Whether the cell holds visible text (not a blank or wide stub)."""
        return bool(self.data.strip())


@dataclass(frozen=True)
class Record:
    """One line which was scrolled out of the scrolling region.

    :param int index: one-based record number.
    :param str text: full-width rendered line, matching ``HistoryScreen``.
    :param str trigger: what caused the scroll -- ``"linefeed"``,
                        ``"auto-wrap"`` or ``"index"``.
    :param int top: zero-based top line of the scrolling region.
    :param int bottom: zero-based bottom line of the scrolling region.
    :param tuple[CellRecord, ...] cells: effective grid cells, from the
        first column through the last written cell.
    """

    index: int
    text: str
    trigger: str
    top: int
    bottom: int
    cells: tuple[CellRecord, ...]

    @property
    def stripped(self) -> str:
        """The line without trailing blank cells."""
        return self.text.rstrip()

    #: Alias for :attr:`stripped`.
    @property
    def effective_text(self) -> str:
        return self.stripped

    @property
    def raw_text(self) -> str:
        """Alias kept for callers which distinguish full-width text."""
        return self.text

    @property
    def line(self) -> str:
        return self.text

    @property
    def text_line(self) -> str:
        return self.text

    @property
    def cause(self) -> str:
        return self.trigger

    @property
    def event(self) -> str:
        return self.trigger

    @property
    def triggered_by(self) -> str:
        return self.trigger

    @property
    def margin_top(self) -> int:
        return self.top

    @property
    def region_top(self) -> int:
        return self.top

    @property
    def screen_top(self) -> int:
        return 0

    @property
    def wrapped_region(self) -> bool:
        return self.top > 0

    @property
    def effective_cells(self) -> tuple[CellRecord, ...]:
        return tuple(cell for cell in self.cells if cell.effective)

    @property
    def first_source(self) -> int | None:
        for cell in self.cells:
            if cell.source is not None:
                return cell.source
        return None

    @property
    def sources(self) -> tuple[int, ...]:
        return tuple(sorted({cell.source for cell in self.cells
                             if cell.source is not None}))

    @property
    def byte_range(self) -> tuple[int | None, int | None]:
        starts = [cell.start for cell in self.cells
                  if cell.start is not None]
        ends = [cell.end for cell in self.cells if cell.end is not None]
        if not starts or not ends:
            return None, None
        return min(starts), max(ends)

    def cell(self, x: int) -> CellRecord:
        return self.cells[x]

    def __str__(self) -> str:
        return self.text


@dataclass
class _RawRecord:
    text: str
    trigger: str
    top: int
    bottom: int
    cells: tuple[CellRecord, ...]

    def materialise(self, index: int) -> Record:
        return Record(index, self.text, self.trigger, self.top, self.bottom,
                      self.cells)


@dataclass(frozen=True)
class _Origin:
    source: int
    occurrence: int
    occurrence_start: int
    stride: int
    stored_offset: int
    stored_length: int

    def stored_span(self, start: int | None,
                    end: int | None) -> tuple[int | None, int | None]:
        if start is None or end is None:
            return None, None
        offset = start - self.occurrence_start
        if self.stride:
            offset %= self.stride
        length = max(0, end - start)
        begin = self.stored_offset + offset
        return begin, begin + length


class _MappedByteStream(Stream):
    """A text stream fed one code point at a time with byte spans.

    Decoding is incremental, so a code point split between two archive
    occurrences is still emitted with the byte span it actually occupied.
    """

    def __init__(self, screen: "LedgerScreen") -> None:
        self.use_utf8 = True
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending_start: int | None = None
        self.byte_offset = 0
        super().__init__(screen)

    def select_other_charset(self, code: str) -> None:
        if code == "@":
            self.use_utf8 = False
            self._decoder.reset()
            self._pending_start = None
        elif code in "G8":
            self.use_utf8 = True
            self._decoder.reset()
            self._pending_start = None

    def feed(self, data: bytes) -> None:  # type: ignore[override]
        start = self.byte_offset
        listener = self.listener
        assert listener is not None
        if self.use_utf8:
            text: list[str] = []
            spans: list[tuple[int, int]] = []
            for index, byte in enumerate(data):
                if self._pending_start is None:
                    self._pending_start = start + index
                decoded = self._decoder.decode(bytes((byte,)))
                if decoded:
                    text.append(decoded)
                    spans.append((self._pending_start, start + index + 1))
                    self._pending_start = None
        else:
            text = [chr(byte) for byte in data]
            spans = [(start + index, start + index + 1)
                     for index in range(len(data))]

        for char, span in zip(text, spans):
            listener.set_span(*span)
            try:
                if self._taking_plain_text and char not in _STREAM_SPECIAL:
                    listener.draw(char)
                else:
                    self._taking_plain_text = self._send_to_parser(char)
            finally:
                listener.set_span(None, None)
        self.byte_offset = start + len(data)


class LedgerScreen(HistoryScreen):
    """A :class:`HistoryScreen` which records forward scrolls.

    This class is normally used through :class:`Ledger`; it can also be fed
    manually with :meth:`feed_bytes` when byte provenance is not needed.
    """

    _wrapped = set(HistoryScreen._wrapped)
    _wrapped.update({"draw", "index", "linefeed", "erase_characters",
                     "erase_in_line", "erase_in_display"})

    def __init__(self, columns: int, lines: int, history: int = 100,
                 ratio: float = .5,
                 resolver: Callable[[int], _Origin | None] | None = None
                 ) -> None:
        self.captured: list[_RawRecord] = []
        self._resolver = resolver
        self._origin: _Origin | None = None
        self._span: tuple[int | None, int | None] = (None, None)
        self._trigger: str | None = None
        self._drawing = False
        super().__init__(columns, lines, history=history, ratio=ratio)

    def set_span(self, start: int | None, end: int | None) -> None:
        if start is None or self._resolver is None:
            self._origin = None
            self._span = (None, None)
        else:
            self._origin = self._resolver(start)
            self._span = (start, end)

    def _char(self, data: str) -> Char:
        origin = self._origin
        if origin is None:
            attrs = self.cursor.attrs
            return attrs._replace(
                data=data, source=None, occurrence=None,
                start=None, end=None, stored_start=None, stored_end=None)
        start, end = self._span
        stored_start, stored_end = origin.stored_span(start, end)
        return self.cursor.attrs._replace(
            data=data, source=origin.source,
            occurrence=origin.occurrence, start=start, end=end,
            stored_start=stored_start, stored_end=stored_end)

    def draw(self, data: str) -> None:
        data = data.translate(
            self.g1_charset if self.charset else self.g0_charset)

        self._drawing = True
        try:
            for char in grapheme_clusters(data):
                char_width = wcswidth(char)

                if self.cursor.x == self.columns:
                    if mo.DECAWM in self.mode:
                        self.dirty.add(self.cursor.y)
                        self.carriage_return()
                        self.linefeed()
                    elif char_width > 0:
                        self.cursor.x -= char_width

                if mo.IRM in self.mode and char_width > 0:
                    self.insert_characters(char_width)

                line = self.buffer[self.cursor.y]
                if char_width == 1:
                    line[self.cursor.x] = self._char(char)
                elif char_width == 2:
                    line[self.cursor.x] = self._char(char)
                    if self.cursor.x + 1 < self.columns:
                        line[self.cursor.x + 1] = self._char("")
                elif (char_width == 0
                      and all(unicodedata.combining(c) for c in char)):
                    if self.cursor.x:
                        last = line[self.cursor.x - 1]
                        normalized = unicodedata.normalize(
                            "NFC", last.data + char)
                        line[self.cursor.x - 1] = last._replace(
                            data=normalized)
                    elif self.cursor.y:
                        last = self.buffer[self.cursor.y - 1][
                            self.columns - 1]
                        normalized = unicodedata.normalize(
                            "NFC", last.data + char)
                        self.buffer[self.cursor.y - 1][
                            self.columns - 1] = last._replace(data=normalized)
                else:
                    break

                if char_width > 0:
                    self.cursor.x = min(self.cursor.x + char_width,
                                        self.columns)

            self.dirty.add(self.cursor.y)
        finally:
            self._drawing = False

    def linefeed(self) -> None:
        previous = self._trigger
        self._trigger = "auto-wrap" if self._drawing else "linefeed"
        try:
            super().linefeed()
        finally:
            self._trigger = previous

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom:
            self._capture(top, bottom, self._trigger or "index")
        super().index()

    def _capture(self, top: int, bottom: int, trigger: str) -> None:
        line = self.buffer[top]
        cells = tuple(
            _capture_cell(line, x)
            for x in range(self.columns))
        raw = "".join(cell.data for cell in cells)
        last = -1
        for cell in cells:
            if cell.data or cell.source is not None:
                last = max(last, cell.x)
        visible = cells[:last + 1] if last >= 0 else ()
        self.captured.append(_RawRecord(
            text=raw, trigger=trigger, top=top,
            bottom=bottom, cells=visible))

    def _clear(self) -> Char:
        """Return cursor attributes without provenance for erasing."""
        return self.cursor.attrs._replace(
            source=None, occurrence=None, start=None, end=None,
            stored_start=None, stored_end=None)

    def erase_characters(self, count: int | None = None) -> None:
        attrs = self.cursor.attrs
        self.cursor.attrs = self._clear()
        try:
            super().erase_characters(count)
        finally:
            self.cursor.attrs = attrs

    def erase_in_line(self, how: int = 0,
                      private: bool = False) -> None:
        attrs = self.cursor.attrs
        self.cursor.attrs = self._clear()
        try:
            super().erase_in_line(how, private)
        finally:
            self.cursor.attrs = attrs

    def erase_in_display(self, how: int = 0, *args: Any,
                         **kwargs: Any) -> None:
        attrs = self.cursor.attrs
        self.cursor.attrs = self._clear()
        try:
            super().erase_in_display(how, *args, **kwargs)
        finally:
            self.cursor.attrs = attrs

    def reset(self) -> None:
        captured = getattr(self, "captured", None)
        super().reset()
        if captured is not None:
            self.captured = captured

    def feed_bytes(self, data: bytes) -> None:
        """Feed bytes without archive provenance."""
        for char in data.decode("utf-8", "replace"):
            self.set_span(None, None)
            Stream.feed(self, char)


def _cell_from_char(x: int, char: Char) -> CellRecord:
    return CellRecord(
        x=x, data=char.data, source=char.source,
        occurrence=char.occurrence, start=char.start, end=char.end,
        stored_start=char.stored_start, stored_end=char.stored_end,
        fg=char.fg, bg=char.bg, bold=char.bold, italics=char.italics,
        underscore=char.underscore, strikethrough=char.strikethrough,
        reverse=char.reverse, blink=char.blink)


def _capture_cell(line: Any, x: int) -> CellRecord:
    char = line[x]
    if not isinstance(char, Char):
        return CellRecord(x=x, data=" ")
    return _cell_from_char(x, char)


class _Span:
    count: int

    def get(self, offset: int) -> _RawRecord:
        raise NotImplementedError

    def materialise(self, first: int) -> Iterator[Record]:
        for offset in range(self.count):
            yield self.get(offset).materialise(first + offset)


@dataclass
class _Explicit(_Span):
    records: list[_RawRecord]

    @property
    def count(self) -> int:  # type: ignore[override]
        return len(self.records)

    def get(self, offset: int) -> _RawRecord:
        return self.records[offset]


@dataclass
class _Repeated(_Span):
    lead: list[_RawRecord]
    cycle: list[_RawRecord]
    dynamic: list[tuple[bool, ...]]
    groups: int
    cycle_occurrences: int
    leftover: list[_RawRecord]
    source: int | None
    base_occurrence: int
    stride: int

    @property
    def count(self) -> int:  # type: ignore[override]
        return (len(self.lead) + self.groups * len(self.cycle)
                + len(self.leftover))

    def get(self, offset: int) -> _RawRecord:
        if offset < len(self.lead):
            return self.lead[offset]
        offset -= len(self.lead)
        cycle_count = len(self.cycle)
        if cycle_count and offset < self.groups * cycle_count:
            group, within = divmod(offset, cycle_count)
            delta = group * self.cycle_occurrences
            return _rebase(self.cycle[within], self.dynamic[within],
                           self.source, delta, self.stride)
        offset -= self.groups * cycle_count
        return self.leftover[offset]


def _rebase(record: _RawRecord, dynamic: Sequence[bool],
            source: int | None, delta: int, stride: int) -> _RawRecord:
    if not delta:
        return record
    cells = []
    for cell, shift in zip(record.cells, dynamic):
        if (shift and source is None
                and cell.source is not None and cell.occurrence is not None):
            # Multi-segment summary: every effective cell has advanced by the
            # same number of complete cycles.
            cells.append(cell._replace(
                occurrence=cell.occurrence + delta,
                start=(cell.start + delta * stride
                       if cell.start is not None else None),
                end=(cell.end + delta * stride
                     if cell.end is not None else None)))
        elif (shift and cell.source == source
                and cell.occurrence is not None):
            cells.append(cell._replace(
                occurrence=cell.occurrence + delta,
                start=(cell.start + delta * stride
                       if cell.start is not None else None),
                end=(cell.end + delta * stride
                     if cell.end is not None else None)))
        else:
            cells.append(cell)
    return _RawRecord(record.text, record.trigger, record.top,
                      record.bottom, tuple(cells))


def _translate_to(record: _RawRecord, delta: int, stride: int) -> _RawRecord:
    """Shift every provenance-bearing cell by ``delta`` occurrences."""
    if not delta:
        return record
    cells = []
    for cell in record.cells:
        if cell.occurrence is not None:
            cells.append(cell._replace(
                occurrence=cell.occurrence + delta,
                start=(cell.start + delta * stride
                       if cell.start is not None else None),
                end=(cell.end + delta * stride
                     if cell.end is not None else None)))
        else:
            cells.append(cell)
    return _RawRecord(record.text, record.trigger, record.top,
                      record.bottom, tuple(cells))


def _dynamic_cells(
    first: Sequence[_RawRecord],
    second: Sequence[_RawRecord],
    cycle_occurrences: int,
    source: int,
) -> list[tuple[bool, ...]]:
    """Find cells whose provenance advances once per cycle.

    A cell rewritten by every occurrence of a template gets a new archive
    occurrence.  A cell left over from an opening write (for example a
    station code) keeps its original occurrence even though the rendered text
    repeats.  Only the former may be rebased for later cycles.
    """
    masks: list[tuple[bool, ...]] = []
    for before, after in zip(first, second):
        mask = []
        for old, new in zip(before.cells, after.cells):
            advanced = (
                old.source == source and new.source == source
                and old.occurrence is not None and new.occurrence is not None
                and new.occurrence - old.occurrence == cycle_occurrences
            )
            mask.append(advanced)
        masks.append(tuple(mask))
    return masks


class Ledger:
    """Extract records from a capture archive.

    :param source: an :class:`~pyte.archive.Archive`, bytes, path or file
                   object.
    :param int columns: screen width, overriding archive metadata.
    :param int lines: screen height, overriding archive metadata.
    :param int history: number of screen history lines to retain.
    :param int fast_threshold: runs longer than this are summarised.
    """

    def __init__(
        self,
        source: Any,
        columns: int | None = None,
        lines: int | None = None,
        history: int = 100,
        fast_threshold: int = DEFAULT_FAST_THRESHOLD,
    ) -> None:
        if isinstance(source, Archive):
            self.archive = source
            self._owned = False
        else:
            self.archive = Archive(source)
            self._owned = True
        self.columns = columns or self.archive.columns
        self.lines = lines or self.archive.lines
        self.fast_threshold = fast_threshold
        self.screen = LedgerScreen(
            self.columns, self.lines, history=history,
            resolver=self._origin_for)
        self.stream = _MappedByteStream(self.screen)
        self._starts = [segment.byte_start
                        for segment in self.archive.segments]
        self._spans: list[_Span] = []
        self._count = 0
        self._process()

    # -- archive lookup ---------------------------------------------------

    def _origin_for(self, offset: int) -> _Origin | None:
        index = bisect.bisect_right(self._starts, offset) - 1
        if index < 0 or index >= len(self.archive.segments):
            return None
        segment = self.archive.segments[index]
        if not segment.length:
            occurrence = 1
            occurrence_start = segment.byte_start
        elif segment.kind == "run":
            occurrence = (offset - segment.byte_start) // segment.length + 1
            occurrence = max(1, min(occurrence, segment.repeat))
            occurrence_start = (segment.byte_start
                                + (occurrence - 1) * segment.length)
        else:
            occurrence = 1
            occurrence_start = segment.byte_start
        return _Origin(
            source=segment.index, occurrence=occurrence,
            occurrence_start=occurrence_start, stride=segment.length,
            stored_offset=segment.offset, stored_length=segment.length)

    # -- feeding ----------------------------------------------------------

    def _drain(self) -> list[_RawRecord]:
        records = self.screen.captured
        self.screen.captured = []
        return records

    def _feed(self, segment: Segment, occurrence: int) -> None:
        start = segment.byte_start + (occurrence - 1) * segment.length
        if segment.kind == "literal":
            if getattr(self.archive, "_literal_in_file", False) \
                    and segment.index == 0:
                data = self.archive._backing.read(0,
                                                 self.archive._backing.size)
            else:
                data = self.archive._backing.read(segment.offset,
                                                  segment.length)
        else:
            data = segment.template
        self.stream.byte_offset = start
        self.stream.feed(data)

    def _signature(self) -> Any:
        screen = self.screen
        lines = []
        for y in range(self.lines):
            line = screen.buffer.get(y)
            if not line:
                lines.append(())
                continue
            lines.append(tuple(
                (x, char.data, char.source, char.occurrence,
                 char.fg, char.bg, char.bold, char.italics,
                 char.underscore, char.strikethrough, char.reverse,
                 char.blink)
                for x, char in sorted(line.items())))
        return (screen.cursor.x, screen.cursor.y,
                tuple(sorted(screen.mode)), screen.margins,
                tuple(sorted(screen.tabstops)), screen.charset,
                tuple(lines))

    @staticmethod
    def _relative(signature: Any, base: int, source: int) -> Any:
        """Normalise provenance of cells written by ``source``.

        Cells left over from earlier segments keep their absolute occurrence
        and therefore count as constants.  Cells written by the current run
        advance once per occurrence; subtracting the occurrence number makes
        a periodic template compare equal cycle after cycle.
        """
        (*head, lines) = signature
        relative = []
        for line in lines:
            cells = []
            for cell in line:
                if (len(cell) >= 4 and cell[2] == source
                        and cell[3] is not None):
                    cells.append(cell[:3] + (cell[3] - base,) + cell[4:])
                else:
                    cells.append(cell)
            relative.append(tuple(cells))
        return (*head, tuple(relative))

    def _process(self) -> None:
        for segment in self.archive.segments:
            if segment.repeat <= 1:
                self._process_explicit(segment)
            elif segment.repeat <= self.fast_threshold:
                self._process_repeated(segment)
            else:
                self._process_long(segment)
        self._count = sum(span.count for span in self._spans)

    def _process_explicit(self, segment: Segment) -> None:
        records: list[_RawRecord] = []
        for occurrence in range(1, segment.repeat + 1):
            self._feed(segment, occurrence)
            records.extend(self._drain())
        self._spans.append(_Explicit(records))

    def _process_repeated(self, segment: Segment) -> None:
        records = []
        for occurrence in range(1, segment.repeat + 1):
            self._feed(segment, occurrence)
            records.extend(self._drain())
        self._spans.append(_Explicit(records))

    def _process_long(self, segment: Segment) -> None:
        repeat = segment.repeat
        signatures = [self._signature()]
        per_occurrence: list[list[_RawRecord]] = []
        probe = 0

        def probe_more(until: int) -> None:
            nonlocal probe
            for occurrence in range(probe + 1, until + 1):
                self._feed(segment, occurrence)
                per_occurrence.append(self._drain())
                signatures.append(self._signature())
            probe = until

        probe_more(min(repeat, max(64, 2 * self.columns + 8)))

        found = None
        while True:
            relative = [self._relative(sig, index, segment.index)
                        for index, sig in enumerate(signatures)]
            found = self._find_period(relative)
            if found is not None or probe >= repeat:
                break
            if probe >= _MAX_PROBE:
                break
            extra = min(repeat - probe, probe, _MAX_PROBE - probe)
            if extra <= 0:
                break
            probe_more(probe + extra)

        if found is None:
            if repeat <= _HARD_LIMIT:
                for occurrence in range(probe + 1, repeat + 1):
                    self._feed(segment, occurrence)
                    per_occurrence.append(self._drain())
                records = [record for group in per_occurrence
                           for record in group]
                self._spans.append(_Explicit(records))
                return
            raise LedgerError(
                "repeated run is not periodic and is too long to expand")

        lead, cycle = found
        # Make sure there is a complete second cycle for provenance analysis.
        needed = lead + 2 * cycle
        if needed > probe:
            extra = min(repeat - probe, needed - probe)
            for occurrence in range(probe + 1, probe + extra + 1):
                self._feed(segment, occurrence)
                per_occurrence.append(self._drain())
                signatures.append(self._signature())
            probe += extra
            if probe < needed:
                # Too short to analyse two cycles; feed the remainder.
                records = [record for group in per_occurrence
                           for record in group]
                for occurrence in range(probe + 1, repeat + 1):
                    self._feed(segment, occurrence)
                    records.extend(self._drain())
                self._spans.append(_Explicit(records))
                return

        lead_records = [record for group in per_occurrence[:lead]
                        for record in group]
        cycle_records = [record for group in per_occurrence[lead:lead + cycle]
                         for record in group]
        next_records = [record for group in
                        per_occurrence[lead + cycle:lead + 2 * cycle]
                        for record in group]
        dynamic = _dynamic_cells(cycle_records, next_records, cycle,
                                 segment.index)

        # Align the probe end with the cycle boundary.
        padding = (-(probe - lead)) % cycle
        if padding:
            if probe + padding > repeat:
                records = [record for group in per_occurrence
                           for record in group]
                for occurrence in range(probe + 1, repeat + 1):
                    self._feed(segment, occurrence)
                    records.extend(self._drain())
                self._spans.append(_Explicit(records))
                return
            for occurrence in range(probe + 1, probe + padding + 1):
                self._feed(segment, occurrence)
                per_occurrence.append(self._drain())
            probe += padding

        simulated_cycles = (probe - lead) // cycle
        remaining = repeat - (lead + simulated_cycles * cycle)
        extra_cycles, leftover_occurrences = divmod(remaining, cycle)

        leftover: list[_RawRecord] = []
        for occurrence in range(probe + 1,
                                probe + leftover_occurrences + 1):
            self._feed(segment, occurrence)
            leftover.extend(self._drain())
        live = probe + leftover_occurrences

        self._spans.append(_Repeated(
            lead=lead_records,
            cycle=cycle_records,
            dynamic=dynamic,
            groups=simulated_cycles + extra_cycles,
            cycle_occurrences=cycle,
            leftover=leftover,
            source=segment.index,
            base_occurrence=lead + 1,
            stride=segment.length))

        # The probe stops near the beginning of a huge run.  Advance the live
        # screen by whole cycles (the screen state is periodic) and rebase
        # the cells written by this segment, so the next segment sees the
        # state it would have seen after all occurrences.
        remaining = repeat - live
        if remaining <= 0:
            return
        cycles = remaining // cycle
        if cycles:
            self._advance_screen(segment, lead, cycles * cycle)
        for occurrence in range(live + cycles * cycle + 1, repeat + 1):
            self._feed(segment, occurrence)
        self._drain()

    def _advance_screen(self, segment: Segment, lead: int,
                        occurrences: int) -> None:
        stride = segment.length
        source = segment.index
        for line in self.screen.buffer.values():
            for x, char in list(line.items()):
                if (char.source == source and char.occurrence is not None
                        and char.occurrence > lead):
                    line[x] = char._replace(
                        occurrence=char.occurrence + occurrences,
                        start=(char.start + occurrences * stride
                               if char.start is not None else None),
                        end=(char.end + occurrences * stride
                             if char.end is not None else None))

    @staticmethod
    def _find_period(signatures: Sequence[Any]) -> tuple[int, int] | None:
        """Return ``(lead, cycle)`` if the signature sequence is periodic.

        The last block of signatures determines the cycle; the earliest lead
        which makes the entire remaining sequence periodic is returned.
        """
        total = len(signatures) - 1
        imports = [hash(item) for item in signatures]
        for cycle in range(1, total // 2 + 1):
            first = imports[total - 2 * cycle:total - cycle]
            second = imports[total - cycle:total]
            if first != second:
                continue
            # Confirm with a full comparison before accepting the cycle.
            if (signatures[total - 2 * cycle:total - cycle]
                    != signatures[total - cycle:total]):
                continue
            lead = 0
            for index in range(total - cycle - 1, -1, -1):
                if imports[index] != imports[index + cycle]:
                    lead = index + 1
                    break
            return lead, cycle
        return None

    # -- queries ----------------------------------------------------------

    @property
    def record_count(self) -> int:
        return self._count

    @property
    def total_records(self) -> int:
        return self._count

    def count(self) -> int:
        return self._count

    def __len__(self) -> int:
        return self._count

    def records(self, start: int = 1, stop: int | None = None
                ) -> Iterator[Record]:
        """Yield records ``start`` (1-based, inclusive) to ``stop``
        (1-based, exclusive)."""
        if start < 1:
            raise IndexError(start)
        if stop is None:
            stop = self._count + 1
        stop = min(stop, self._count + 1)
        position = start
        offset = start - 1
        for span in self._spans:
            if position >= stop:
                break
            if offset >= span.count:
                offset -= span.count
                continue
            take = min(span.count - offset, stop - position)
            for inner in range(take):
                yield span.get(offset + inner).materialise(position + inner)
            position += take
            offset = 0

    def record(self, number: int) -> Record:
        """Return the ``number``-th record (1-based)."""
        if number < 1 or number > self._count:
            raise IndexError(number)
        offset = number - 1
        for span in self._spans:
            if offset < span.count:
                return span.get(offset).materialise(number)
            offset -= span.count
        raise IndexError(number)

    def get(self, number: int) -> Record:
        return self.record(number)

    def query(self, number: int) -> Record:
        return self.record(number)

    @property
    def first(self) -> Record | None:
        return self.record(1) if self._count else None

    @property
    def last(self) -> Record | None:
        return self.record(self._count) if self._count else None

    def tail(self, count: int = 8) -> list[Record]:
        if count <= 0 or not self._count:
            return []
        start = max(1, self._count - count + 1)
        return list(self.records(start))

    def __iter__(self) -> Iterator[Record]:
        return self.records()

    def __getitem__(self, item: int | slice) -> Record | list[Record]:
        if isinstance(item, slice):
            start = 1 if item.start is None else item.start + 1
            stop = (self._count + 1 if item.stop is None else item.stop + 1)
            return list(self.records(start, stop))
        if item < 0:
            item += self._count
        if item < 0 or item >= self._count:
            raise IndexError(item)
        return self.record(item + 1)

    def __repr__(self) -> str:
        return (f"Ledger(records={self._count}, "
                f"segments={len(self.archive.segments)})")


#: Compatibility aliases.
CaptureLedger = Ledger
ArchiveLedger = Ledger
RecordLedger = Ledger
CaptureRecord = Record
Cell = CellRecord


def read_capture(source: Any, **kwargs: Any) -> Ledger:
    return Ledger(source, **kwargs)


def load_capture(source: Any, **kwargs: Any) -> Ledger:
    return Ledger(source, **kwargs)


def open_capture(source: Any, **kwargs: Any) -> Ledger:
    return Ledger(source, **kwargs)


def scan_capture(source: Any, **kwargs: Any) -> Ledger:
    return Ledger(source, **kwargs)


def records(source: Any, **kwargs: Any) -> Iterator[Record]:
    return iter(Ledger(source, **kwargs))


def count_records(source: Any, **kwargs: Any) -> int:
    return len(Ledger(source, **kwargs))


__all__ = [
    "ArchiveLedger",
    "CaptureLedger",
    "CaptureRecord",
    "Cell",
    "CellRecord",
    "DEFAULT_FAST_THRESHOLD",
    "Ledger",
    "LedgerError",
    "LedgerScreen",
    "Record",
    "RecordLedger",
    "count_records",
    "load_capture",
    "open_capture",
    "read_capture",
    "records",
    "scan_capture",
]
