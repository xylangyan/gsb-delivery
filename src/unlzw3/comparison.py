"""
Index based comparison of LZW (.Z) archive fragments.

Two legacy archive volumes may carry the same decompressed text while using
different code widths, different dictionary reset points and different code
boundaries (one emitted code can expand to the same bytes that two codes emit
on the other side).  This module never compares compressed codes directly.

A :class:`CompressedIndex` is built with a single scan of the compressed code
stream.  It stores one fixed size record per *compressed code* (bit range, code
value, segment id, decompressed start/length of the emitted word).  The
decompressed body, which can be dozens of times larger than the archive, is
never stored or generated while indexing.  All later comparisons work from the
index alone, reconstructing dictionary state from the recorded code values.
"""

from __future__ import annotations

import array
import bisect
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union

__all__ = [
    "InvalidArchive",
    "ByteAnchor",
    "CodeAnchor",
    "RangeSpec",
    "CompressedIndex",
    "Selection",
    "ByteProvenance",
    "ComparisonResult",
    "build_index",
    "make_selection",
    "compare",
    "compare_archives",
    "save_index",
    "load_index",
]

_INDEX_MAGIC = b"LZW3IDX1"
_INDEX_VERSION = 1


class InvalidArchive(ValueError):
    """Raised when an archive cannot be indexed by the reference LZW reader."""


# ---------------------------------------------------------------------------
# Index data structure
# ---------------------------------------------------------------------------


@dataclass
class CompressedIndex:
    """
    Per-code index of one .Z stream.

    Every array is indexed by the ordinal of the compressed code (0 based,
    counting the first literal and every dictionary clear code).  Records have
    a fixed size, so the on disk and in memory footprint scales with the number
    of compressed codes, never with the decompressed output size.
    """

    block_mode: bool
    max_bits: int
    total_out: int
    bit_start: array.array = field(default_factory=lambda: array.array("q"))
    bit_end: array.array = field(default_factory=lambda: array.array("q"))
    code_value: array.array = field(default_factory=lambda: array.array("I"))
    out_start: array.array = field(default_factory=lambda: array.array("q"))
    out_len: array.array = field(default_factory=lambda: array.array("i"))
    segment_id: array.array = field(default_factory=lambda: array.array("i"))
    # Ordinal of the first code of each dictionary segment.
    segment_start: array.array = field(default_factory=lambda: array.array("q"))
    source_name: Optional[str] = None

    @property
    def n_codes(self) -> int:
        return len(self.code_value)

    def is_clear(self, code_ordinal: int) -> bool:
        return self.out_len[code_ordinal] == 0


# ---------------------------------------------------------------------------
# Index building - a single scan of the compressed code stream
# ---------------------------------------------------------------------------


def _as_bytearray(inp) -> bytearray:
    if isinstance(inp, Path):
        data = inp.read_bytes()
    else:
        data = inp
    try:
        return bytearray(data)
    except TypeError:
        raise TypeError("Unable to convert input data to bytearray")


def build_index(inp, *, source_name: Optional[str] = None) -> CompressedIndex:
    """
    Scan an LZW .Z stream once and return a :class:`CompressedIndex`.

    The bit level parsing mirrors :func:`unlzw3.unlzw` exactly, including the
    vestigial alignment flushes on code width changes and dictionary clears.
    Instead of retaining the emitted bytes, only per-code metadata is kept.
    Raises :class:`InvalidArchive` for every stream the reference reader would
    reject.
    """

    ba = _as_bytearray(inp)
    inlen = len(ba)

    if inlen < 3:
        raise InvalidArchive("Invalid Input: Length of input too short for processing")
    if ba[0] != 0x1F or ba[1] != 0x9D:
        raise InvalidArchive("Invalid Header Flags Byte: Incorrect magic bytes")

    flags = ba[2]
    if flags & 0x60:
        raise InvalidArchive("Invalid Header Flags Byte: Flag byte contains invalid data")
    max_bits = flags & 0x1F
    if max_bits < 9 or max_bits > 16:
        raise InvalidArchive("Invalid Header Flags Byte: Max code size bits out of range")
    if max_bits == 9:
        max_bits = 10  # 9 doesn't really mean 9
    block_mode = bool(flags & 0x80)

    if inlen == 3:
        idx = CompressedIndex(block_mode, max_bits, 0, source_name=source_name)
        idx.segment_start.append(0)
        return idx
    if inlen == 4:
        raise InvalidArchive("Invalid Data: Stream ended in the middle of a code")

    prefix = [0] * 65536
    suffix = [0] * 65536

    bit_start = array.array("q")
    bit_end = array.array("q")
    code_values = array.array("I")
    out_start = array.array("q")
    out_len = array.array("i")
    segment_id = array.array("i")
    segment_start = array.array("q", [0])

    def record(code: int, start_bit: int, end_bit: int, length: int, total: int, seg: int) -> None:
        bit_start.append(start_bit)
        bit_end.append(end_bit)
        code_values.append(code)
        out_start.append(total)
        out_len.append(length)
        segment_id.append(seg)

    bits = 9
    mask = 0x1FF
    end = 256 if block_mode else 255

    # First 9-bit code: the first decompressed byte.
    buf = ba[3] + (ba[4] << 8)
    final = prev = buf & mask
    if prev > 255:
        raise InvalidArchive("Invalid Data: First code must be a literal")
    record(prev, 0, bits, 1, 0, 0)
    total_out = 1
    buf >>= bits
    left = 16 - bits

    mark = 3
    nxt = 5
    seg = 0

    while nxt < inlen:
        if end >= mask and bits < max_bits:
            # Flush to the next 8*bits bit boundary.  The skipped padding bits
            # are not part of any recorded code range.
            rem = (nxt - mark) % bits
            if rem:
                rem = bits - rem
                if rem >= inlen - nxt:
                    break
                nxt += rem
            buf = 0
            left = 0
            mark = nxt
            bits += 1
            mask = (mask << 1) + 1

        start_bit = nxt * 8 - left

        buf += ba[nxt] << left
        nxt += 1
        left += 8
        if left < bits:
            if nxt == inlen:
                raise InvalidArchive("Invalid Data: Stream ended in the middle of a code")
            buf += ba[nxt] << left
            nxt += 1
            left += 8
        code = buf & mask
        buf >>= bits
        left -= bits
        end_bit = start_bit + bits

        if code == 256 and block_mode:
            # Dictionary clear code: record it, then reset the decoder.
            record(256, start_bit, end_bit, 0, total_out, seg)
            rem = (nxt - mark) % bits
            if rem:
                rem = bits - rem
                if rem > inlen - nxt:
                    break
                nxt += rem
            buf = 0
            left = 0
            mark = nxt
            bits = 9
            mask = 0x1FF
            end = 255
            seg += 1
            segment_start.append(len(code_values))
            continue

        # Replicate the reference decoder's linked list walk so that the
        # recorded word length is always exactly the number of emitted bytes.
        temp = code
        stack: list[int] = []
        if code > end:
            if code != end + 1 or prev > end:
                raise InvalidArchive("Invalid Data: Invalid code detected")
            stack.append(final)
            code = prev
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        stack.append(code)
        final = code

        word_length = len(stack)
        record(temp, start_bit, end_bit, word_length, total_out, seg)
        total_out += word_length

        if end < mask:
            end += 1
            prefix[end] = prev
            suffix[end] = final
        prev = temp

    return CompressedIndex(
        block_mode=block_mode,
        max_bits=max_bits,
        total_out=total_out,
        bit_start=bit_start,
        bit_end=bit_end,
        code_value=code_values,
        out_start=out_start,
        out_len=out_len,
        segment_id=segment_id,
        segment_start=segment_start,
        source_name=source_name,
    )


# ---------------------------------------------------------------------------
# Selections (handoff range lists)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ByteAnchor:
    """Absolute decompressed byte offset within one archive."""

    offset: int


@dataclass(frozen=True)
class CodeAnchor:
    """Position at compressed code ordinal ``code``, word offset ``word``."""

    code: int
    word: int = 0


Anchor = Union[ByteAnchor, CodeAnchor]


@dataclass(frozen=True)
class RangeSpec:
    """
    One retained interval from a handoff sheet.

    Both anchors are inclusive at the start and exclusive at the end.  Anchors
    may sit in the middle of an emitted word, on either archive independently.
    """

    start: Anchor
    end: Optional[Anchor] = None
    length: Optional[int] = None


@dataclass(frozen=True)
class _ResolvedRange:
    start_code: int
    start_word: int
    end_code: int
    end_word: int
    out_begin: int
    out_end: int


@dataclass
class Selection:
    index: CompressedIndex
    ranges: tuple[_ResolvedRange, ...]
    output_length: int
    # Cumulative concatenated byte offset at the end of each range.
    cumulative_ends: tuple[int, ...]

    def range_index_at(self, selection_offset: int) -> int:
        """Index of the handoff range containing ``selection_offset``."""
        if selection_offset < 0 or selection_offset >= self.output_length:
            raise ValueError("selection offset outside concatenated output")
        return bisect.bisect_right(self.cumulative_ends, selection_offset)


def _resolve_anchor(index: CompressedIndex, anchor: Anchor, *, is_end: bool) -> tuple[int, int]:
    if isinstance(anchor, ByteAnchor):
        offset = anchor.offset
        if offset < 0 or offset > index.total_out:
            raise ValueError("byte anchor outside decompressed output")
        if offset == index.total_out:
            if not is_end and offset != 0:
                raise ValueError("byte anchor at end of stream may only be used as a range end")
            return index.n_codes, 0
        ordinal = bisect.bisect_right(index.out_start, offset) - 1
        # Zero-length clear codes share their out_start with the next code;
        # bisect_right lands on the following real code.
        while ordinal < index.n_codes and index.is_clear(ordinal):
            ordinal += 1
        word = offset - index.out_start[ordinal]
        if word >= index.out_len[ordinal]:
            ordinal += 1
            word = 0
        return ordinal, word

    if isinstance(anchor, CodeAnchor):
        if anchor.code < 0 or anchor.code > index.n_codes:
            raise ValueError("code anchor outside compressed stream")
        if anchor.code == index.n_codes:
            if not is_end or anchor.word != 0:
                raise ValueError("sentinel code anchor may only be used as a range end")
            return anchor.code, 0
        if index.is_clear(anchor.code):
            raise ValueError("code anchor may not point at a dictionary clear code")
        if anchor.word < 0 or anchor.word > index.out_len[anchor.code]:
            raise ValueError("word anchor outside emitted word")
        if anchor.word == index.out_len[anchor.code]:
            # A position just past a word is the start of the next word; skip
            # any clear code that may sit between the two.
            ordinal = anchor.code + 1
            while ordinal < index.n_codes and index.is_clear(ordinal):
                ordinal += 1
            if ordinal >= index.n_codes and not is_end:
                raise ValueError("start anchor sits at the end of the stream")
            return min(ordinal, index.n_codes), 0
        return anchor.code, anchor.word

    raise TypeError(f"unsupported anchor type: {type(anchor)!r}")


def make_selection(index: CompressedIndex, specs: Sequence[RangeSpec]) -> Selection:
    """Resolve a handoff sheet's ordered range list against an index."""
    if not specs:
        raise ValueError("at least one range is required")

    resolved_ranges: list[_ResolvedRange] = []
    cumulative = 0
    cumulative_ends: list[int] = []
    previous_end: Optional[tuple[int, int]] = None

    for spec in specs:
        start_code, start_word = _resolve_anchor(index, spec.start, is_end=False)
        if spec.end is not None:
            if spec.length is not None:
                raise ValueError("range cannot specify both end and length")
            end_code, end_word = _resolve_anchor(index, spec.end, is_end=True)
        elif spec.length is not None:
            if spec.length < 0:
                raise ValueError("range length must be non-negative")
            begin_byte = index.out_start[start_code] + start_word
            end_code, end_word = _resolve_anchor(
                index, ByteAnchor(begin_byte + spec.length), is_end=True
            )
        else:
            raise ValueError("range requires either an end anchor or a length")

        if start_code < index.n_codes:
            begin_abs = index.out_start[start_code] + start_word
        else:
            begin_abs = index.total_out
        if end_code < index.n_codes:
            end_abs = index.out_start[end_code] + end_word
        else:
            end_abs = index.total_out
        if end_abs < begin_abs:
            raise ValueError("range ends before it starts")
        if previous_end is not None and (start_code, start_word) < previous_end:
            raise ValueError("ranges must be ordered and non-overlapping")
        previous_end = (end_code, end_word)
        resolved = _ResolvedRange(
            start_code=start_code,
            start_word=start_word,
            end_code=end_code,
            end_word=end_word,
            out_begin=begin_abs,
            out_end=end_abs,
        )
        resolved_ranges.append(resolved)
        cumulative += end_abs - begin_abs
        cumulative_ends.append(cumulative)

    return Selection(
        index=index,
        ranges=tuple(resolved_ranges),
        output_length=cumulative,
        cumulative_ends=tuple(cumulative_ends),
    )


# ---------------------------------------------------------------------------
# Index-only dictionary reconstruction and word walking
# ---------------------------------------------------------------------------


class _WordWalker:
    """
    Walks the decompressed words covered by a selection without touching the
    compressed bytes or storing the decompressed body.

    Dictionary prefix/suffix tables are rebuilt from the indexed code values,
    segment by segment.  Codes in gaps between retained ranges are still
    decoded, because dictionary state depends on them; only index arrays in
    memory are read, never the original archive.
    """

    _END = object()

    def __init__(self, selection: Selection):
        self.selection = selection
        self.index = selection.index
        self._iterator = self._decode_words()
        self._ordinal = -1
        self._word: Optional[bytes] = None
        self._word_pos = 0
        self.range_pos = 0
        # Number of non-clear codes decoded since construction; used to verify
        # that comparisons never walk segments they do not need.
        self.decoded_count = 0

    def _needed_segments(self) -> list[int]:
        idx = self.index
        segments: set[int] = set()
        for resolved in self.selection.ranges:
            first_ordinal = resolved.start_code
            if resolved.end_word > 0:
                last_ordinal = resolved.end_code
            else:
                last_ordinal = max(first_ordinal, resolved.end_code - 1)
            first_segment = bisect.bisect_right(idx.segment_start, first_ordinal) - 1
            last_segment = bisect.bisect_right(idx.segment_start, last_ordinal) - 1
            segments.update(range(first_segment, last_segment + 1))
        return sorted(segments)

    def _decode_words(self) -> Iterator[tuple[int, bytes]]:
        """
        Yield ``(ordinal, emitted word)`` for every non-clear code in the
        dictionary segments touched by the selection.

        Each dictionary segment starts from an empty table, so segments that no
        range touches are skipped entirely: a comparison never scans the whole
        volume when its handoff ranges only cover part of it.
        """
        idx = self.index
        needed = set(self._needed_segments())

        for seg_index, seg_start in enumerate(idx.segment_start):
            if seg_index not in needed:
                continue
            seg_end = (
                idx.segment_start[seg_index + 1]
                if seg_index + 1 < len(idx.segment_start)
                else idx.n_codes
            )

            prefix = [0] * 65536
            suffix = [0] * 65536
            bits = 9
            mask = 0x1FF
            # Block mode reserves code 256 as the clear code in the initial
            # table.  After a clear the table is logically empty (end == 255)
            # but the first decoded code still allocates slot 256 again; that
            # slot is never walkable because code 256 remains the clear code,
            # yet its allocation shifts every later slot number, so it must be
            # reproduced exactly.
            end = 256 if seg_index == 0 and idx.block_mode else 255
            prev: Optional[int] = None
            final = 0
            first = True

            for ordinal in range(seg_start, seg_end):
                if idx.is_clear(ordinal):
                    continue

                code = idx.code_value[ordinal]
                if first and seg_index == 0:
                    # Stream's first literal: emitted with no table entry.
                    prev = code
                    final = code
                    first = False
                    self.decoded_count += 1
                    yield ordinal, bytes([code])
                    continue
                if first:
                    # First literal after a dictionary clear.  The reference
                    # reader allocates the phantom slot 256 here.
                    final = code
                    end = 256
                    prev = code
                    first = False
                    self.decoded_count += 1
                    yield ordinal, bytes([code])
                    continue

                if end >= mask and bits < idx.max_bits:
                    bits += 1
                    mask = (mask << 1) + 1

                temp = code
                stack: list[int] = []
                if code > end:
                    if code != end + 1 or prev > end:
                        raise InvalidArchive("Invalid Data: Invalid code detected")
                    stack.append(final)
                    code = prev
                while code >= 256:
                    stack.append(suffix[code])
                    code = prefix[code]
                stack.append(code)
                final = code
                if end < mask:
                    end += 1
                    prefix[end] = prev
                    suffix[end] = final
                prev = temp
                self.decoded_count += 1
                yield ordinal, bytes(bytearray(stack[::-1]))

    def next_chunk(self) -> Optional[tuple[bytes, int, int, int]]:
        """
        Return ``(chunk, code_ordinal, word_position, range_index)`` or None at
        the end of the selection.  Chunks are contiguous slices of emitted
        words and never cross a handoff range boundary.
        """
        ranges = self.selection.ranges
        while self.range_pos < len(ranges):
            resolved = ranges[self.range_pos]

            if self._word is None:
                item = next(self._iterator, self._END)
                if item is self._END:
                    return None
                self._ordinal, self._word = item
                self._word_pos = 0

            if self._ordinal < resolved.start_code:
                # Word inside a gap between retained ranges: decode for
                # dictionary state but discard the bytes.
                self._word = None
                continue
            if self._ordinal > resolved.end_code or (
                self._ordinal == resolved.end_code and resolved.end_word == 0
            ):
                # Word belongs to a later range (or none): advance the range
                # while keeping any unconsumed remainder of this word.
                self.range_pos += 1
                continue

            word_begin = (
                resolved.start_word if self._ordinal == resolved.start_code else 0
            )
            word_begin = max(word_begin, self._word_pos)
            word_end = (
                resolved.end_word
                if self._ordinal == resolved.end_code
                else len(self._word)
            )
            if word_end <= word_begin:
                self.range_pos += 1
                continue

            chunk = self._word[word_begin:word_end]
            ordinal = self._ordinal
            range_index = self.range_pos
            self._word_pos = word_end
            if self._word_pos >= len(self._word):
                self._word = None
            return chunk, ordinal, word_begin, range_index
        return None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ByteProvenance:
    """Where one compared byte came from inside its original archive."""

    range_index: int
    segment: int  # 1 based dictionary segment
    code_index: int  # 0 based compressed code ordinal
    code_value: int
    code_bit_start: int  # absolute bit offset of the compressed code, half open
    code_bit_end: int
    word_position: int  # 0 based position inside the emitted word
    decompressed_offset: int  # absolute byte offset in the full archive output


@dataclass(frozen=True)
class ComparisonResult:
    equal: bool
    output_offset: Optional[int] = None
    left_byte: Optional[int] = None
    right_byte: Optional[int] = None
    left_provenance: Optional[ByteProvenance] = None
    right_provenance: Optional[ByteProvenance] = None
    short_by: Optional[int] = None
    invalid_side: Optional[str] = None
    invalid_reason: Optional[str] = None

    def __str__(self) -> str:
        if self.invalid_side is not None:
            return f"INVALID {self.invalid_side} archive: {self.invalid_reason}"
        if self.equal:
            return "MATCH: concatenated selections are byte identical"
        lines = [f"DIFFER at concatenated offset {self.output_offset}:"]
        lines.extend(_format_side("left ", self.left_byte, self.left_provenance, self.short_by))
        lines.extend(_format_side("right", self.right_byte, self.right_provenance, self.short_by))
        return "\n".join(lines)


def _format_side(
    label: str,
    byte: Optional[int],
    provenance: Optional[ByteProvenance],
    short_by: Optional[int],
) -> list[str]:
    if provenance is None:
        return [f"  {label}: exhausted ({short_by} byte(s) short)"]
    byte_text = "0x%02X" % byte if byte is not None else "end-of-data"
    return [
        f"  {label}: byte {byte_text} in handoff range #{provenance.range_index}",
        f"        segment {provenance.segment}, code ordinal {provenance.code_index} "
        f"(value {provenance.code_value}), compressed bits "
        f"[{provenance.code_bit_start}, {provenance.code_bit_end})",
        f"        word position {provenance.word_position}, "
        f"decompressed offset {provenance.decompressed_offset}",
    ]


def _provenance(
    selection: Selection, ordinal: int, word_position: int, range_index: int
) -> ByteProvenance:
    idx = selection.index
    return ByteProvenance(
        range_index=range_index,
        segment=idx.segment_id[ordinal] + 1,
        code_index=ordinal,
        code_value=idx.code_value[ordinal],
        code_bit_start=idx.bit_start[ordinal],
        code_bit_end=idx.bit_end[ordinal],
        word_position=word_position,
        decompressed_offset=idx.out_start[ordinal] + word_position,
    )


class _SideBuffer:
    def __init__(self, walker: _WordWalker):
        self.walker = walker
        self.selection = walker.selection
        self.chunk = b""
        self.ordinal = -1
        self.word_begin = 0
        self.range_index = 0
        self.pos = 0
        self.exhausted = False

    def load(self) -> bool:
        item = self.walker.next_chunk()
        if item is None:
            self.exhausted = True
            self.chunk = b""
            return False
        self.chunk, self.ordinal, self.word_begin, self.range_index = item
        self.pos = 0
        return True

    def remaining(self) -> int:
        return len(self.chunk) - self.pos

    def provenance(self, word_position: int) -> ByteProvenance:
        return _provenance(self.selection, self.ordinal, word_position, self.range_index)


def compare(left: Selection, right: Selection) -> ComparisonResult:
    """
    Compare two handoff selections using their indexes only.

    Emitted words are compared as byte strings, so a single code on one side
    matching two codes on the other (or any difference in code width / clear
    timing) passes as long as the reconstructed bytes agree.
    """
    left_buf = _SideBuffer(_WordWalker(left))
    right_buf = _SideBuffer(_WordWalker(right))
    offset = 0

    left_alive = left_buf.load()
    right_alive = right_buf.load()

    while left_alive and right_alive:
        span = min(left_buf.remaining(), right_buf.remaining())
        left_slice = left_buf.chunk[left_buf.pos : left_buf.pos + span]
        right_slice = right_buf.chunk[right_buf.pos : right_buf.pos + span]
        if left_slice != right_slice:
            for delta in range(span):
                if left_slice[delta] != right_slice[delta]:
                    break
            left_word_pos = left_buf.word_begin + left_buf.pos + delta
            right_word_pos = right_buf.word_begin + right_buf.pos + delta
            return ComparisonResult(
                equal=False,
                output_offset=offset + delta,
                left_byte=left_slice[delta],
                right_byte=right_slice[delta],
                left_provenance=left_buf.provenance(left_word_pos),
                right_provenance=right_buf.provenance(right_word_pos),
            )

        offset += span
        left_buf.pos += span
        right_buf.pos += span
        if left_buf.remaining() == 0:
            left_alive = left_buf.load()
        if right_buf.remaining() == 0:
            right_alive = right_buf.load()

    if not left_alive and not right_alive:
        return ComparisonResult(equal=True)

    if not left_alive:
        return _length_mismatch("left", right_buf, right, offset)
    return _length_mismatch("right", left_buf, left, offset)


def _length_mismatch(
    exhausted_name: str,
    live_buf: _SideBuffer,
    live_selection: Selection,
    offset: int,
) -> ComparisonResult:
    # The live side still holds the first unread chunk in its buffer.
    word_position = live_buf.word_begin + live_buf.pos
    provenance = live_buf.provenance(word_position)
    live_byte = live_buf.chunk[live_buf.pos]
    short_by = live_selection.output_length - offset

    if exhausted_name == "left":
        return ComparisonResult(
            equal=False,
            output_offset=offset,
            left_byte=None,
            right_byte=live_byte,
            right_provenance=provenance,
            short_by=short_by,
        )
    return ComparisonResult(
        equal=False,
        output_offset=offset,
        left_byte=live_byte,
        right_byte=None,
        left_provenance=provenance,
        short_by=short_by,
    )


# ---------------------------------------------------------------------------
# High level helper
# ---------------------------------------------------------------------------


def _anchor_from_obj(obj) -> Anchor:
    if isinstance(obj, (ByteAnchor, CodeAnchor)):
        return obj
    if not isinstance(obj, dict):
        raise TypeError(f"unsupported anchor: {obj!r}")
    if "byte" in obj:
        return ByteAnchor(int(obj["byte"]))
    if "code" in obj:
        return CodeAnchor(int(obj["code"]), int(obj.get("word", 0)))
    raise ValueError(f"unrecognised anchor: {obj!r}")


def parse_range(obj) -> RangeSpec:
    """Parse a JSON style range mapping into a :class:`RangeSpec`."""
    if isinstance(obj, RangeSpec):
        return obj
    if not isinstance(obj, dict):
        raise TypeError(f"unsupported range: {obj!r}")
    if "start_byte" in obj:
        start: Anchor = ByteAnchor(int(obj["start_byte"]))
        if "length" not in obj:
            raise ValueError("start_byte range requires a length")
        return RangeSpec(start=start, length=int(obj["length"]))
    start = _anchor_from_obj(obj["start"])
    if "end" in obj:
        return RangeSpec(start=start, end=_anchor_from_obj(obj["end"]))
    if "length" in obj:
        return RangeSpec(start=start, length=int(obj["length"]))
    raise ValueError(f"unrecognised range: {obj!r}")


def compare_archives(left_data, right_data, left_ranges, right_ranges) -> ComparisonResult:
    """
    Build both indexes (raising no exception for bad input) and compare the
    selections described by two handoff range lists.  If the reference reader
    rejects an archive, the returned result names which side was invalid.
    """
    try:
        left_index = build_index(left_data, source_name=_label_of(left_data))
    except InvalidArchive as exc:
        return ComparisonResult(equal=False, invalid_side="left", invalid_reason=str(exc))
    try:
        right_index = build_index(right_data, source_name=_label_of(right_data))
    except InvalidArchive as exc:
        return ComparisonResult(equal=False, invalid_side="right", invalid_reason=str(exc))

    left_specs = [parse_range(obj) for obj in left_ranges]
    right_specs = [parse_range(obj) for obj in right_ranges]
    return compare(
        make_selection(left_index, left_specs),
        make_selection(right_index, right_specs),
    )


def _label_of(inp) -> Optional[str]:
    if isinstance(inp, Path):
        return str(inp)
    return None


# ---------------------------------------------------------------------------
# Binary index persistence
# ---------------------------------------------------------------------------


def save_index(index: CompressedIndex, path) -> None:
    """Write an index to a compact binary file sized by the code count."""
    out = Path(path)
    with out.open("wb") as fh:
        fh.write(_INDEX_MAGIC)
        fh.write(
            struct.pack(
                "<HBB??qqi",
                _INDEX_VERSION,
                0,
                index.max_bits,
                index.block_mode,
                index.source_name is not None,
                index.total_out,
                index.n_codes,
                len(index.segment_start),
            )
        )
        if index.source_name is not None:
            name = index.source_name.encode("utf-8")
            fh.write(struct.pack("<I", len(name)))
            fh.write(name)
        for arr in (
            index.bit_start,
            index.bit_end,
            index.code_value,
            index.out_start,
            index.out_len,
            index.segment_id,
            index.segment_start,
        ):
            arr.tofile(fh)


def load_index(path) -> CompressedIndex:
    """Rebuild a :class:`CompressedIndex` previously written by :func:`save_index`."""
    with Path(path).open("rb") as fh:
        magic = fh.read(len(_INDEX_MAGIC))
        if magic != _INDEX_MAGIC:
            raise ValueError("not an unlzw3 comparison index file")
        (
            version,
            _reserved,
            max_bits,
            block_mode,
            has_name,
            total_out,
            n_codes,
            n_segments,
        ) = struct.unpack("<HBB??qqi", fh.read(struct.calcsize("<HBB??qqi")))
        if version != _INDEX_VERSION:
            raise ValueError(f"unsupported index version {version}")
        source_name = None
        if has_name:
            (name_len,) = struct.unpack("<I", fh.read(4))
            source_name = fh.read(name_len).decode("utf-8")

        def read_array(typecode: str, count: int) -> array.array:
            arr = array.array(typecode)
            arr.fromfile(fh, count)
            return arr

        bit_start = read_array("q", n_codes)
        bit_end = read_array("q", n_codes)
        code_value = read_array("I", n_codes)
        out_start = read_array("q", n_codes)
        out_len = read_array("i", n_codes)
        segment_id = read_array("i", n_codes)
        segment_start = read_array("q", n_segments)

    return CompressedIndex(
        block_mode=block_mode,
        max_bits=max_bits,
        total_out=total_out,
        bit_start=bit_start,
        bit_end=bit_end,
        code_value=code_value,
        out_start=out_start,
        out_len=out_len,
        segment_id=segment_id,
        segment_start=segment_start,
        source_name=source_name,
    )
