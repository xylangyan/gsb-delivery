"""Archive entry point for segment-based NFC delivery with provenance.

A volume (册) is an ordered list of segments (片). Each segment has a fixed
identifier and a fixed position in the volume order, and carries raw UTF-8
bytes. For delivery (出库) the concatenated text of a volume is normalized
to NFC using pyunormalize's tables and algorithm.

This module provides:

- ``Archive.create_volume``: build the normalized product of a volume and
  record, for every delivered code point, the half-open byte intervals in
  the original segments that actually compose it.
- ``Archive.read_window``: random-access read of a window of the normalized
  product by code point position, with per-code-point provenance, without
  replaying normalization from the first segment.
- ``Archive.lookup_source``: reverse lookup from a byte range in an
  original segment to the delivered code point positions it participates
  in. A single source character that decomposes into code points landing
  at different product positions is reported at every one of them.
- ``Archive.correct_segment``: ingest a corrected segment as a new volume
  version. Only the contiguous run of combining sequences affected by the
  change is reprocessed; provably unchanged units are reused as-is. Old
  versions remain queryable.
- ``Archive.verify``: independent cross-check that the delivered character
  order equals ``pyunormalize.NFC`` of the concatenated raw text.

Incremental model
-----------------
NFC never lets canonical reordering or composition cross a canonical
starter (a code point with combining class 0), with one exception: Hangul
vowel (V) and trailing (T) jamo also have combining class 0 but compose
with a preceding L/LV. The concatenated text is therefore fully decomposed
and split into independent units at every combining-class-0 code point
that is not a V or T jamo. Each unit is reordered and composed on its own,
exactly mirroring ``pyunormalize._reorder`` / ``pyunormalize._compose``,
while every decomposed code point carries the byte range of its source
character. Concatenating the unit products yields the NFC of the whole
volume, and a correction only invalidates the units whose source bytes
overlap the corrected segment.
"""

from bisect import bisect_right
from typing import NamedTuple

from pyunormalize import NFC
from pyunormalize.normalization import (
    _SB,
    _SL,
    _TB,
    _TL,
    _VB,
    _VL,
    _ccc,
    _cdecomp_table,
    _composition_table,
    _compose_hangul_syllable,
    _decompose_hangul_syllable,
)

__all__ = (
    "Archive",
    "ProductChar",
    "SourceHit",
    "SourceRef",
    "VersionInfo",
    "Window",
)


class SourceRef(NamedTuple):
    """Half-open byte interval [start, end) in an original segment."""

    segment_id: str
    start: int
    end: int


class ProductChar(NamedTuple):
    """One delivered code point and the source intervals composing it."""

    position: int
    char: str
    sources: tuple


class Window(NamedTuple):
    """A window of the normalized product with per-code-point provenance."""

    text: str
    chars: tuple


class SourceHit(NamedTuple):
    """One delivered code point a source byte range participates in."""

    position: int
    char: str
    source: SourceRef


class VersionInfo(NamedTuple):
    """Summary of a stored volume version."""

    number: int
    unit_count: int
    length: int


class _Unit:
    """One independently normalizable combining sequence.

    ``src_start``/``src_end`` are (segment index, byte offset) positions of
    the source characters covered by the unit. ``refs[i]`` holds the source
    intervals composing product code point ``i`` of the unit.
    """

    __slots__ = ("src_start", "src_end", "text", "refs")

    def __init__(self, src_start, src_end, text, refs):
        self.src_start = src_start
        self.src_end = src_end
        self.text = text
        self.refs = refs


class _Version:
    __slots__ = ("number", "seg_data", "units", "offsets", "text", "rev")

    def __init__(self, number, seg_data, units, offsets, text, rev):
        self.number = number
        self.seg_data = seg_data
        self.units = units
        self.offsets = offsets
        self.text = text
        self.rev = rev


class _Volume:
    __slots__ = ("volume_id", "order", "index_of", "versions")

    def __init__(self, volume_id, order):
        self.volume_id = volume_id
        self.order = order
        self.index_of = {seg_id: i for i, seg_id in enumerate(order)}
        self.versions = []


# --- Normalization with provenance (mirrors pyunormalize internals) ---------


def _decompose_cp(cp, cdecomp):
    """Fully decompose one code point, as pyunormalize._decompose does."""
    mapped = cdecomp.get(cp)
    if mapped is not None:
        return mapped
    if _SB <= cp <= _SL:
        return _decompose_hangul_syllable(cp)
    return (cp,)


def _is_boundary(cp, ccc):
    """Tell whether a decomposed code point starts a new unit.

    A new unit starts before every code point with combining class 0,
    except Hangul V and T jamo, which compose with a preceding L/LV
    despite having combining class 0.
    """
    if cp in ccc:
        return False
    return not (_VB <= cp <= _VL or _TB <= cp <= _TL)


def _first_decomposed_cp(order, seg_data, pos):
    """First decomposed code point of the first source character at ``pos``.

    Scans forward from ``pos`` (a character-aligned position) over the
    ordered segments; empty segments are skipped.
    """
    cdecomp = _cdecomp_table()
    for seg_index in range(pos[0], len(order)):
        text = seg_data[order[seg_index]].decode("utf-8")
        bpos = 0
        for ch in text:
            if (seg_index, bpos) >= pos:
                return _decompose_cp(ord(ch), cdecomp)[0]
            bpos += len(ch.encode("utf-8"))
    raise ValueError(f"no source character at {pos!r}")


def _process_unit(items, ccc, comp):
    """Reorder and compose one unit, tracking source intervals.

    ``items`` is a list of (code point, source ref, ...) tuples in
    decomposed order. Returns the unit's product text and, for each
    product code point, the tuple of source refs composing it.
    """
    items = list(items)

    # Canonical ordering, mirroring pyunormalize._reorder.
    size = len(items)
    while size > 1:
        swap_pos = 0
        i = 1
        while i < size:
            curr = items[i][0]
            if curr not in ccc:
                i += 2
                continue
            prev = items[i - 1][0]
            if prev not in ccc or ccc[prev] <= ccc[curr]:
                i += 1
                continue
            items[i - 1], items[i] = items[i], items[i - 1]
            swap_pos = i
            i += 1
        size = swap_pos

    # Canonical composition, mirroring pyunormalize._compose.
    out = []
    starter_index = None
    last_ccc = 0
    for cp, ref, _, _ in items:
        c = ccc.get(cp, 0)
        if starter_index is not None and (last_ccc == 0 or last_ccc < c):
            precomposed = comp.get((out[starter_index][0], cp))
            if precomposed is None:
                precomposed = _compose_hangul_syllable(out[starter_index][0], cp)
            if precomposed is not None:
                out[starter_index][0] = precomposed
                refs = out[starter_index][1]
                if ref not in refs:
                    refs.append(ref)
                continue
        out.append([cp, [ref]])
        if c == 0:
            starter_index = len(out) - 1
            last_ccc = 0
        else:
            last_ccc = c

    text = "".join(chr(cp) for cp, _ in out)
    refs = tuple(tuple(refs) for _, refs in out)
    return text, refs


def _build_units(order, seg_data, start, end):
    """Normalize the source span [start, end) into provenance units.

    ``start`` and ``end`` are (segment index, byte offset) positions and
    must fall on source character boundaries.
    """
    ccc = _ccc()
    cdecomp = _cdecomp_table()
    comp = _composition_table()
    units = []
    pending = []

    def flush():
        if pending:
            text, refs = _process_unit(pending, ccc, comp)
            units.append(_Unit(pending[0][2], pending[-1][3], text, refs))
            pending.clear()

    for seg_index in range(start[0], end[0] + 1):
        seg_id = order[seg_index]
        text = seg_data[seg_id].decode("utf-8")
        bpos = 0
        for ch in text:
            bnext = bpos + len(ch.encode("utf-8"))
            if (seg_index, bpos) >= start and (seg_index, bnext) <= end:
                ref = SourceRef(seg_id, bpos, bnext)
                for dcp in _decompose_cp(ord(ch), cdecomp):
                    if pending and _is_boundary(dcp, ccc):
                        flush()
                    pending.append((dcp, ref, (seg_index, bpos), (seg_index, bnext)))
            bpos = bnext
    flush()
    return units


def _layout(units):
    """Assign product offsets and build the reverse index for units."""
    offsets = []
    parts = []
    rev = {}
    pos = 0
    for unit in units:
        offsets.append(pos)
        parts.append(unit.text)
        for i, refs in enumerate(unit.refs):
            for ref in refs:
                rev.setdefault(ref.segment_id, []).append(
                    (ref.start, ref.end, pos + i)
                )
        pos += len(unit.text)
    for entries in rev.values():
        entries.sort()
    return offsets, "".join(parts), rev


# --- Archive -----------------------------------------------------------------


class Archive:
    """Versioned archive of volumes delivered as NFC with provenance."""

    def __init__(self):
        self._volumes = {}

    def create_volume(self, volume_id, segments):
        """Create a volume from ordered (segment id, bytes) pairs.

        Builds version 1 of the volume and returns the version number.
        """
        if volume_id in self._volumes:
            raise ValueError(f"volume {volume_id!r} already exists")
        order = []
        seg_data = {}
        for seg_id, data in segments:
            if seg_id in seg_data:
                raise ValueError(f"duplicate segment id {seg_id!r}")
            data = bytes(data)
            data.decode("utf-8")
            order.append(seg_id)
            seg_data[seg_id] = data
        volume = _Volume(volume_id, order)
        if order:
            end = (len(order) - 1, len(seg_data[order[-1]]))
            units = _build_units(order, seg_data, (0, 0), end)
        else:
            units = []
        offsets, text, rev = _layout(units)
        volume.versions.append(_Version(1, seg_data, units, offsets, text, rev))
        self._volumes[volume_id] = volume
        return 1

    def correct_segment(self, volume_id, segment_id, data):
        """Ingest a corrected segment as a new version of the volume.

        Only the contiguous run of combining sequences overlapping the
        corrected segment is reprocessed; all other units are carried over
        unchanged. Returns the new version number.
        """
        volume = self._volumes[volume_id]
        try:
            seg_index = volume.index_of[segment_id]
        except KeyError:
            raise KeyError(f"unknown segment id {segment_id!r}") from None
        new_data = bytes(data)
        new_data.decode("utf-8")

        old = volume.versions[-1]
        old_len = len(old.seg_data[segment_id])
        change_start = (seg_index, 0)
        change_end = (seg_index, old_len)

        # Units whose source span intersects the corrected segment.
        units = old.units
        first = last = None
        for k, unit in enumerate(units):
            if unit.src_end > change_start and unit.src_start < change_end:
                if first is None:
                    first = k
                last = k

        if first is None:
            # Empty previous content: reprocess exactly the new segment.
            region_start = change_start
            region_end_old = change_end
        else:
            # Expand over units whose spans straddle the region boundary
            # (a unit boundary may fall inside a multi-code-point
            # decomposition, making adjacent unit spans overlap).
            while first > 0 and units[first - 1].src_end > units[first].src_start:
                first -= 1
            while (
                last < len(units) - 1
                and units[last + 1].src_start < units[last].src_end
            ):
                last += 1
            region_start = units[first].src_start
            region_end_old = units[last].src_end

        # Region boundaries in unchanged segments keep their coordinates;
        # boundaries in the corrected segment map onto its new extent.
        region_start_new = (
            (seg_index, 0) if region_start[0] == seg_index else region_start
        )
        region_end_new = (
            (seg_index, len(new_data))
            if region_end_old[0] == seg_index
            else region_end_old
        )

        new_seg_data = dict(old.seg_data)
        new_seg_data[segment_id] = new_data

        prefix = [u for u in units if u.src_end <= region_start]
        suffix = [u for u in units if u.src_start >= region_end_old]

        # If the new content does not begin a combining sequence of its own
        # (e.g. a starter was replaced by a combining mark), it joins the
        # preceding sequence, which must then be reprocessed as well.
        ccc = _ccc()
        while (
            prefix
            and region_start_new < region_end_new
            and not _is_boundary(
                _first_decomposed_cp(volume.order, new_seg_data, region_start_new),
                ccc,
            )
        ):
            region_start = prefix[-1].src_start
            region_start_new = (
                (seg_index, 0) if region_start[0] == seg_index else region_start
            )
            prefix.pop()

        middle = _build_units(
            volume.order, new_seg_data, region_start_new, region_end_new
        )

        new_units = prefix + middle + suffix
        offsets, text, rev = _layout(new_units)
        number = old.number + 1
        volume.versions.append(
            _Version(number, new_seg_data, new_units, offsets, text, rev)
        )
        return number

    def read_window(self, volume_id, start, end, version=None):
        """Read product code points [start, end) with their provenance.

        Resolves the window through stored unit offsets; normalization is
        not replayed from the beginning of the volume.
        """
        ver = self._version(volume_id, version)
        if not 0 <= start <= end <= len(ver.text):
            raise ValueError(
                f"window [{start}, {end}) out of range "
                f"(product length {len(ver.text)})"
            )
        chars = []
        if ver.units:
            k = max(0, bisect_right(ver.offsets, start) - 1)
            pos = ver.offsets[k]
            for unit in ver.units[k:]:
                if pos >= end:
                    break
                for i, ch in enumerate(unit.text):
                    if start <= pos + i < end:
                        chars.append(ProductChar(pos + i, ch, unit.refs[i]))
                pos += len(unit.text)
        return Window(ver.text[start:end], tuple(chars))

    def lookup_source(self, volume_id, segment_id, start, end, version=None):
        """Find delivered code points a source byte range participates in.

        Returns every product position whose composing source intervals
        intersect the half-open range [start, end) of the given segment.
        """
        ver = self._version(volume_id, version)
        if not 0 <= start <= end:
            raise ValueError(f"invalid byte range [{start}, {end})")
        hits = []
        for bstart, bend, ppos in ver.rev.get(segment_id, ()):
            if bstart < end and bend > start:
                hits.append(
                    SourceHit(ppos, ver.text[ppos], SourceRef(segment_id, bstart, bend))
                )
        return hits

    def product_text(self, volume_id, version=None):
        """Return the full normalized product text of a volume version."""
        return self._version(volume_id, version).text

    def version_info(self, volume_id, version=None):
        """Return summary information about a stored version."""
        ver = self._version(volume_id, version)
        return VersionInfo(ver.number, len(ver.units), len(ver.text))

    def version_count(self, volume_id):
        """Return the number of stored versions of a volume."""
        return len(self._volumes[volume_id].versions)

    def verify(self, volume_id, version=None):
        """Cross-check a version against an independent NFC computation.

        Concatenates the raw segment texts in order and normalizes them
        with pyunormalize.NFC; the delivered character order must match.
        """
        volume = self._volumes[volume_id]
        ver = self._version(volume_id, version)
        raw = "".join(ver.seg_data[s].decode("utf-8") for s in volume.order)
        return NFC(raw) == ver.text

    def _version(self, volume_id, version):
        volume = self._volumes[volume_id]
        if version is None:
            return volume.versions[-1]
        if not 1 <= version <= len(volume.versions):
            raise KeyError(f"unknown version {version!r} of volume {volume_id!r}")
        return volume.versions[version - 1]
