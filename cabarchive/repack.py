#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2020 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: LGPL-2.1+
#
# pylint: disable=protected-access,too-few-public-methods

"""Repack a MSZIP cabinet into two new deliveries, reusing original data blocks.

This implements "block grafting" for a signed-off source archive: the old CAB
has already issued a receipt number for each original CFDATA block (its zero
based index in the folder), and two new packages have to be produced that
``cabarchive`` can open again.  Whenever an original MSZIP block decodes to
exactly the bytes a new package needs at a given position, the compressed
record is copied verbatim -- compressed body and checksum preserved -- instead
of recompressing that part of the payload.

A block may secretly rely on the previous block as a deflate dictionary, so
reusability is always checked against the *actual decode result* in the new
context, never assumed from content equality alone.  Each receipt number is a
physical voucher: it may be registered at most once across both deliveries.
The pair of packages delivered is the one that minimises the total number of
uncompressed bytes that have to be recompressed.

Only single-folder MSZIP archives that ``cabarchive`` itself can open are
supported as source.  Files may span compression blocks and one compression
block may cover the seam between two files; all planning is done on the
linear uncompressed stream, so no candidate split plan ever requires writing
a whole CAB before reuse eligibility is known.

The handover sheet is a JSON object::

    {
      "source": "old.cab",
      "packages": [
        {
          "output": "batch-a.cab",
          "files": [
            {"name": "qc/img1.jpg", "from": "qc/img1.jpg"},
            {"name": "notes/table2.csv", "text": "new,contents\n"},
            {"name": "qc/img9.jpg", "data": "newfiles/img9.jpg", "mtime": 0}
          ],
          "breakpoints": [0, 32768, 195000],
          "blocks": {"0": [0], "4": [131072]}
        },
        { "...": "second package, same shape" }
      ]
    }

* ``files`` gives the fixed file order of the new package.  Exactly one of
  ``from`` (copy bytes and metadata from a source archive entry), ``data``
  (read replacement bytes from a file, relative to the sheet) or ``text``
  (literal UTF-8 contents) is required.  ``mtime`` is an optional UNIX
  timestamp overriding the file timestamp.
* ``breakpoints`` lists the uncompressed offsets in the new package where a
  block boundary may exist; 0 and the total size are always allowed.  If
  omitted, every 0x8000 boundary is allowed.  Freshly compressed runs are
  cut into 0x8000 chunks internally; those interior cuts need no breakpoint.
* ``blocks`` maps a receipt number to the uncompressed offsets in *this*
  package the old block is permitted to occupy.  Receipts not listed may not
  be placed in this package.
"""

import argparse
import bisect
import datetime
import json
import os
import struct
import sys
import zlib

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from cabarchive.archive import CabArchive
from cabarchive.errors import CorruptionError, NotSupportedError
from cabarchive.file import CabFile
from cabarchive.utils import (
    FMT_CFHEADER,
    FMT_CFHEADER_RESERVE,
    FMT_CFFOLDER,
    FMT_CFFILE,
    FMT_CFDATA,
    _checksum_compute,
)

#: maximum uncompressed bytes in one CFDATA block
CHUNK_SIZE = 0x8000

#: deflate window size, i.e. how much history a MSZIP block may reference
WINDOW_SIZE = 0x8000

# reuse verdicts and rejection reasons
REASON_OK = "ok"
REASON_POSITION_NOT_ALLOWED = "position-not-allowed"
REASON_BREAKPOINT_NOT_ALLOWED = "breakpoint-not-allowed"
REASON_OUT_OF_RANGE = "out-of-range"
REASON_CONTENT_MISMATCH = "content-mismatch"
REASON_DICTIONARY_MISMATCH = "dictionary-mismatch"
REASON_VOUCHER_TAKEN = "voucher-taken"
REASON_NOT_SELECTED = "not-selected"
REASON_USED_AT_OTHER_POSITION = "used-at-other-position"
REASON_DEMOTED = "demoted-dictionary-truncated"

_COMPRESSION_MASK_TYPE = 0x000F
_COMPRESSION_TYPE_MSZIP = 0x0001


@dataclass
class SourceBlock:
    """One original CFDATA block of the source archive.

    The ``receipt`` is the voucher number issued by the old CAB, i.e. the
    zero based index of the block in its folder.
    """

    receipt: int  #: voucher number of this block
    checksum: int  #: checksum stored in the CFDATA header
    offset: int  #: uncompressed offset in the source folder
    cb_data: int  #: compressed size, including the CK header
    cb_uncomp: int  #: uncompressed size
    record: bytes  #: complete CFDATA record, header and compressed body
    data: bytes  #: compressed body only, including the CK header


@dataclass
class Candidate:
    """A feasible placement of one source block in one new package."""

    receipt: int
    start: int
    end: int
    size: int


@dataclass
class Placement:
    """A source block actually copied into a new package."""

    receipt: int
    offset: int
    size: int
    checksum: int
    compressed_size: int


@dataclass
class RepackPackage:
    """One built delivery."""

    name: str
    cab_bytes: bytes
    files: List[str]
    total_uncompressed: int
    reused_bytes: int
    fresh_bytes: int
    placements: List[Placement]
    fresh_ranges: List[Tuple[int, int]]
    rejected: List[Dict[str, object]]
    demoted: List[Dict[str, object]]


@dataclass
class RepackResult:
    """The pair (or set) of built deliveries plus verification output."""

    packages: List[RepackPackage]
    verification: List[str] = field(default_factory=list)

    def report(self) -> Dict[str, object]:
        """Return a JSON serialisable handover report."""
        return {
            "packages": [
                {
                    "output": pkg.name,
                    "files": pkg.files,
                    "total_uncompressed": pkg.total_uncompressed,
                    "reused_bytes": pkg.reused_bytes,
                    "fresh_bytes": pkg.fresh_bytes,
                    "placements": [
                        {
                            "receipt": p.receipt,
                            "offset": p.offset,
                            "size": p.size,
                            "checksum": p.checksum,
                            "compressed_size": p.compressed_size,
                        }
                        for p in pkg.placements
                    ],
                    "fresh_ranges": [
                        {"offset": off, "size": size} for off, size in pkg.fresh_ranges
                    ],
                    "rejected": pkg.rejected,
                    "demoted": pkg.demoted,
                }
                for pkg in self.packages
            ],
            "verification": self.verification,
        }

    def save(self, outdir: str = ".") -> None:
        """Write every package CAB below ``outdir``."""
        for pkg in self.packages:
            path = os.path.join(outdir, pkg.name)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "wb") as f:
                f.write(pkg.cab_bytes)


def _parse_source_blocks(buf: bytes) -> Tuple[int, List[SourceBlock]]:
    """Extract the raw CFDATA records of a single folder MSZIP cabinet."""
    try:
        (
            signature,
            _size,
            _off_cffile,
            version_minor,
            version_major,
            nr_folders,
            _nr_files,
            flags,
            set_id,
            idx_cabinet,
        ) = struct.unpack_from(FMT_CFHEADER, buf, 0)
    except struct.error as e:
        raise CorruptionError from e
    if signature != b"MSCF":
        raise NotSupportedError("Data is not application/vnd.ms-cab-compressed")
    if version_major != 1 or version_minor != 3:
        raise NotSupportedError(f"Version {version_major}.{version_minor} not supported")
    if idx_cabinet != 0:
        raise NotSupportedError("Chained cab file not supported")
    if nr_folders != 1:
        raise NotSupportedError("Only single folder cabinets can be repacked")

    offset = struct.calcsize(FMT_CFHEADER)
    rsvd_block = 0
    if flags & 0x0004:
        try:
            (rsvd_hdr, _rsvd_folder, rsvd_block) = struct.unpack_from(
                FMT_CFHEADER_RESERVE, buf, offset
            )
        except struct.error as e:
            raise CorruptionError from e
        offset += struct.calcsize(FMT_CFHEADER_RESERVE) + rsvd_hdr

    try:
        (off_cfdata, ndatab, compression) = struct.unpack_from(FMT_CFFOLDER, buf, offset)
    except struct.error as e:
        raise CorruptionError from e
    compression &= _COMPRESSION_MASK_TYPE
    if compression != _COMPRESSION_TYPE_MSZIP:
        raise NotSupportedError("Only MSZIP compressed cabinets can be repacked")

    blocks: List[SourceBlock] = []
    hdr_sz = struct.calcsize(FMT_CFDATA) + rsvd_block
    offset = off_cfdata
    uoffset = 0
    for receipt in range(ndatab):
        try:
            (checksum, cb_data, cb_uncomp) = struct.unpack_from(FMT_CFDATA, buf, offset)
        except struct.error as e:
            raise CorruptionError from e
        record = buf[offset : offset + hdr_sz + cb_data]
        data = buf[offset + hdr_sz : offset + hdr_sz + cb_data]
        if len(data) != cb_data:
            raise CorruptionError("CFDATA record truncated")
        if data[:2] != b"CK":
            raise CorruptionError("Compression header invalid")
        blocks.append(
            SourceBlock(
                receipt=receipt,
                checksum=checksum,
                offset=uoffset,
                cb_data=cb_data,
                cb_uncomp=cb_uncomp,
                record=record,
                data=data,
            )
        )
        uoffset += cb_uncomp
        offset += hdr_sz + cb_data
    return set_id, blocks


def _fresh_record(chunk: bytes) -> bytes:
    """Compress one chunk into a self contained MSZIP CFDATA record."""
    compressobj = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    data = b"CK" + compressobj.compress(chunk) + compressobj.flush()
    checksum = _checksum_compute(data)
    hdr = struct.pack("<HH", len(data), len(chunk))
    checksum = _checksum_compute(hdr, checksum)
    return struct.pack(FMT_CFDATA, checksum, len(data), len(chunk)) + data


def assemble_cab(
    files: Sequence[Tuple[str, CabFile]], records: Sequence[bytes], set_id: int = 0
) -> bytes:
    """Build a single folder MSZIP cabinet from prepared CFDATA records.

    ``files`` is the fixed file order of the package and ``records`` the
    ordered CFDATA records (header plus body) covering the concatenated
    uncompressed file data exactly.
    """
    if not files:
        raise ValueError("A package needs at least one file")
    if not records:
        raise ValueError("A package needs at least one CFDATA block")
    if len(records) > 0xFFFF:
        raise ValueError("Too many CFDATA blocks for one folder")
    for _name, cffile in files:
        if not cffile._filename_win32:
            raise ValueError("Every file needs a filename")

    archive_size = struct.calcsize(FMT_CFHEADER) + struct.calcsize(FMT_CFFOLDER)
    for _name, cffile in files:
        archive_size += struct.calcsize(FMT_CFFILE) + len(cffile._filename_win32.encode()) + 1
    for record in records:
        archive_size += len(record)
    offset = struct.calcsize(FMT_CFHEADER) + struct.calcsize(FMT_CFFOLDER)

    parts: List[bytes] = []
    parts.append(
        struct.pack(
            FMT_CFHEADER,
            b"MSCF",  # signature
            archive_size,  # complete size
            offset,  # offset to CFFILE
            3,
            1,  # ver minor major
            1,  # no of CFFOLDERs
            len(files),  # no of CFFILEs
            0,  # flags
            set_id,  # setID
            0,  # cnt of cabs in set
        )
    )

    # create folder
    coff_cfdata = offset
    for _name, cffile in files:
        coff_cfdata += struct.calcsize(FMT_CFFILE) + len(cffile._filename_win32.encode()) + 1
    parts.append(
        struct.pack(
            FMT_CFFOLDER,
            coff_cfdata,  # offset to CFDATA
            len(records),  # number of CFDATA blocks
            _COMPRESSION_TYPE_MSZIP,
        )
    )

    # create each CFFILE
    index_into = 0
    for _name, cffile in files:
        parts.append(
            struct.pack(
                FMT_CFFILE,
                len(cffile),  # uncompressed size
                index_into,  # uncompressed offset
                0,  # index into CFFOLDER
                cffile._date_encode(),  # date
                cffile._time_encode(),  # time
                cffile._attr_encode(),
            )
        )
        parts.append(cffile._filename_win32.encode() + b"\0")
        index_into += len(cffile)

    # create each CFDATA
    parts.extend(records)
    return b"".join(parts)


def _default_breakpoints(total: int) -> List[int]:
    """Allowed split positions when the sheet does not list any."""
    return sorted(set(range(0, total, CHUNK_SIZE)) | {total})


def _decode_mszip(data: bytes, dictionary: bytes) -> Optional[bytes]:
    """Decode a MSZIP body (with CK header) against a dictionary, or None."""
    try:
        decompress = zlib.decompressobj(-zlib.MAX_WBITS, zdict=dictionary)
        return decompress.decompress(data[2:]) + decompress.flush()
    except zlib.error:
        return None


class _StateOverflow(Exception):
    pass


def _next_indices(cands: Sequence[Candidate]) -> List[int]:
    starts = [c.start for c in cands]
    return [bisect.bisect_left(starts, c.end, lo=i + 1) for i, c in enumerate(cands)]


def _wis_choose(cands: Sequence[Candidate], banned: set) -> List[Candidate]:
    """Weighted interval scheduling for one package, ignoring shared vouchers."""
    pool = sorted(
        (c for c in cands if c.receipt not in banned),
        key=lambda c: (c.start, c.end, c.receipt),
    )
    n = len(pool)
    if n == 0:
        return []
    nxt = _next_indices(pool)
    dp = [0] * (n + 1)
    take = [False] * n
    for i in range(n - 1, -1, -1):
        best = dp[i + 1]
        picked = False
        value = pool[i].size + dp[nxt[i]]
        if value > best:
            best = value
            picked = True
        dp[i] = best
        take[i] = picked
    chosen = []
    i = 0
    while i < n:
        if take[i]:
            chosen.append(pool[i])
            i = nxt[i]
        else:
            i += 1
    return chosen


def _solve_greedy(cands_per_pkg: Sequence[Sequence[Candidate]]) -> List[List[Candidate]]:
    chosen: List[List[Candidate]] = []
    banned: set = set()
    for cands in cands_per_pkg:
        picked = _wis_choose(cands, banned)
        chosen.append(picked)
        banned.update(c.receipt for c in picked)
    return chosen


def _solve_exact(
    cands_a: Sequence[Candidate], cands_b: Sequence[Candidate], state_cap: int = 250000
) -> Tuple[List[Candidate], List[Candidate]]:
    """Jointly optimise two packages so each receipt is used at most once.

    Maximises the reused uncompressed bytes; ties are broken towards placing
    a block in the first package.  Raises _StateOverflow if the instance is
    too large for the exact dynamic program.
    """
    a = sorted(cands_a, key=lambda c: (c.start, c.end, c.receipt))
    b = sorted(cands_b, key=lambda c: (c.start, c.end, c.receipt))
    nxa = _next_indices(a)
    nxb = _next_indices(b)
    memo: Dict[Tuple[int, int, int], int] = {}
    choice: Dict[Tuple[int, int, int], int] = {}

    def rec(i: int, j: int, mask: int) -> int:
        key = (i, j, mask)
        hit = memo.get(key)
        if hit is not None:
            return hit
        if len(memo) >= state_cap:
            raise _StateOverflow
        best = 0
        act = 0  # 1 take a[i], 2 skip a[i], 3 take b[j], 4 skip b[j]
        if i < len(a):
            cand = a[i]
            if not (mask >> cand.receipt) & 1:
                value = cand.size + rec(nxa[i], j, mask | (1 << cand.receipt))
                if value > best:
                    best, act = value, 1
            value = rec(i + 1, j, mask)
            if value > best:
                best, act = value, 2
        if j < len(b):
            cand = b[j]
            if not (mask >> cand.receipt) & 1:
                value = cand.size + rec(i, nxb[j], mask | (1 << cand.receipt))
                if value > best:
                    best, act = value, 3
            value = rec(i, j + 1, mask)
            if value > best:
                best, act = value, 4
        memo[key] = best
        choice[key] = act
        return best

    old_limit = sys.getrecursionlimit()
    needed = 4 * (len(a) + len(b)) + 100
    if needed > old_limit:
        sys.setrecursionlimit(needed)
    try:
        rec(0, 0, 0)
    finally:
        if needed > old_limit:
            sys.setrecursionlimit(old_limit)

    chosen_a: List[Candidate] = []
    chosen_b: List[Candidate] = []
    i = j = mask = 0
    while True:
        act = choice.get((i, j, mask), 0)
        if act == 1:
            chosen_a.append(a[i])
            mask |= 1 << a[i].receipt
            i = nxa[i]
        elif act == 2:
            i += 1
        elif act == 3:
            chosen_b.append(b[j])
            mask |= 1 << b[j].receipt
            j = nxb[j]
        elif act == 4:
            j += 1
        else:
            break
    return chosen_a, chosen_b


def _solve(cands_per_pkg: Sequence[Sequence[Candidate]]) -> List[List[Candidate]]:
    """Pick non overlapping placements so every receipt is used at most once."""
    if len(cands_per_pkg) == 2:
        total = len(cands_per_pkg[0]) + len(cands_per_pkg[1])
        if total <= 5000:
            try:
                chosen_a, chosen_b = _solve_exact(cands_per_pkg[0], cands_per_pkg[1])
                return [chosen_a, chosen_b]
            except _StateOverflow:
                pass
        fwd = _solve_greedy(cands_per_pkg)
        rev = _solve_greedy([cands_per_pkg[1], cands_per_pkg[0]])
        rev.reverse()
        if sum(c.size for c in rev[0]) + sum(c.size for c in rev[1]) > sum(
            c.size for c in fwd[0]
        ) + sum(c.size for c in fwd[1]):
            return rev
        return fwd
    return _solve_greedy(cands_per_pkg)


@dataclass
class _PackageState:
    spec: Dict[str, object]
    files: List[Tuple[str, CabFile]]
    stream: bytes
    breakpoints: set
    blockspec: Dict[int, List[int]]
    candidates: List[Candidate] = field(default_factory=list)
    rejected: List[Dict[str, object]] = field(default_factory=list)


class Repacker:
    """Repacks one source cabinet into new deliveries per a handover sheet."""

    def __init__(self, buf: bytes):
        """Load the source cabinet, validating it fully once.

        Raises:
            CorruptionError: The cab file was invalid or corrupt.
            NotSupportedError: Not a single folder MSZIP cabinet.
        """
        self._buf = buf
        self.archive = CabArchive(buf)  # full parse validates checksums and streams
        self.set_id, self.blocks = _parse_source_blocks(buf)
        self._decode_cache: Dict[Tuple[int, bytes], Optional[bytes]] = {}
        self._source_output_cache: Dict[int, bytes] = {}
        # state of the last build, used by explain()
        self._states: List[_PackageState] = []
        self._chosen: List[List[Candidate]] = []
        self._demoted: List[List[Dict[str, object]]] = []

    def _source_output(self, receipt: int) -> bytes:
        """Uncompressed output of a source block in its original context."""
        if not self._source_output_cache:
            dictionary = b""
            for block in self.blocks:
                out = _decode_mszip(block.data, dictionary)
                if out is None:
                    raise CorruptionError("Failed to decompress source block")
                self._source_output_cache[block.receipt] = out
                dictionary = out
        return self._source_output_cache[receipt]

    def _decode_with(self, receipt: int, dictionary: bytes) -> Optional[bytes]:
        """Decode a source block against a new dictionary, cached.

        Two positions with identical preceding bytes share one decode, so
        attachments with lots of identical local content never cause a
        re-decode per candidate split plan.
        """
        key = (receipt, dictionary)
        if key not in self._decode_cache:
            self._decode_cache[key] = _decode_mszip(self.blocks[receipt].data, dictionary)
        return self._decode_cache[key]

    def _check_candidate(self, receipt: int, state: _PackageState, pos: int) -> str:
        """Check whether a source block can be grafted at ``pos``.

        The verdict is based on the real decode result in the new context:
        the block is decompressed against the bytes preceding ``pos`` in the
        new package and the output must equal the bytes the package needs.
        """
        block = self.blocks[receipt]
        size = block.cb_uncomp
        total = len(state.stream)
        if pos < 0 or pos + size > total:
            return REASON_OUT_OF_RANGE
        if pos not in state.breakpoints or pos + size not in state.breakpoints:
            return REASON_BREAKPOINT_NOT_ALLOWED
        dictionary = state.stream[max(0, pos - WINDOW_SIZE) : pos]
        needed = state.stream[pos : pos + size]
        out = self._decode_with(receipt, dictionary)
        if out == needed:
            return REASON_OK
        if self._source_output(receipt) == needed:
            # the block content is exactly right, only the borrowed
            # dictionary produced different output
            return REASON_DICTIONARY_MISMATCH
        return REASON_CONTENT_MISMATCH

    def _resolve_files(
        self, spec: Dict[str, object], base_dir: str
    ) -> List[Tuple[str, CabFile]]:
        files: List[Tuple[str, CabFile]] = []
        seen: set = set()
        entries = spec.get("files", [])
        if not isinstance(entries, list):
            raise ValueError("files must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry:
                raise ValueError("Every file entry needs a name")
            name = str(entry["name"])
            if name in seen:
                raise ValueError(f"Duplicate file in package: {name}")
            seen.add(name)
            kinds = [k for k in ("from", "data", "text") if k in entry]
            if len(kinds) != 1:
                raise ValueError(f"File {name} needs exactly one of from/data/text")
            if "from" in entry:
                src_name = str(entry["from"])
                src = self.archive.get(src_name) or self.archive.get(
                    src_name.replace("/", "\\")
                )
                if src is None:
                    raise ValueError(f"File not in source archive: {src_name}")
                cffile = CabFile(buf=src.buf)
                cffile.date = src.date
                cffile.time = src.time
                cffile.is_readonly = src.is_readonly
                cffile.is_hidden = src.is_hidden
                cffile.is_system = src.is_system
                cffile.is_arch = src.is_arch
                cffile.is_exec = src.is_exec
            elif "data" in entry:
                path = os.path.join(base_dir, str(entry["data"]))
                with open(path, "rb") as f:
                    cffile = CabFile(buf=f.read())
                cffile.date = datetime.date(1980, 1, 1)
                cffile.time = datetime.time(0, 0, 0)
            else:
                cffile = CabFile(buf=str(entry["text"]).encode())
                cffile.date = datetime.date(1980, 1, 1)
                cffile.time = datetime.time(0, 0, 0)
            if "mtime" in entry:
                mtime = datetime.datetime.fromtimestamp(
                    int(entry["mtime"]), datetime.timezone.utc
                )
                cffile.date = mtime.date()
                cffile.time = mtime.time()
            cffile.filename = name
            files.append((name, cffile))
        return files

    def _build_state(self, spec: Dict[str, object], base_dir: str) -> _PackageState:
        files = self._resolve_files(spec, base_dir)
        if not files:
            raise ValueError("A package needs at least one file")
        stream = b"".join(cffile.buf or b"" for _name, cffile in files)
        if not stream:
            raise ValueError("A package needs some uncompressed data")
        total = len(stream)
        raw_bps = spec.get("breakpoints")
        if raw_bps is None:
            breakpoints = set(_default_breakpoints(total))
        elif isinstance(raw_bps, list):
            breakpoints = {int(p) for p in raw_bps}
        else:
            raise ValueError("breakpoints must be a list")
        breakpoints |= {0, total}
        for point in breakpoints:
            if point < 0 or point > total:
                raise ValueError(f"Breakpoint {point} outside package")
        blockspec: Dict[int, List[int]] = {}
        raw_blocks = spec.get("blocks", {})
        if not isinstance(raw_blocks, dict):
            raise ValueError("blocks must map receipt numbers to positions")
        for key, positions in raw_blocks.items():
            receipt = int(key)
            if receipt < 0 or receipt >= len(self.blocks):
                raise ValueError(f"Unknown receipt number {receipt}")
            if not isinstance(positions, list):
                raise ValueError(f"Positions for receipt {receipt} must be a list")
            blockspec[receipt] = sorted({int(p) for p in positions})
        return _PackageState(
            spec=spec,
            files=files,
            stream=stream,
            breakpoints=breakpoints,
            blockspec=blockspec,
        )

    def _emit_records(
        self, state: _PackageState, chosen: Sequence[Candidate]
    ) -> Tuple[List[Tuple[bytes, int, Optional[int]]], List[Dict[str, object]]]:
        """Sequence reused and fresh records, then re-validate every reused
        block against the exact dictionary the parser will see.

        The parser hands each block only the *previous block's* output as
        dictionary; if that is shorter than the 32k window used for the
        candidate check, a block may fail to decode in place.  Such a block
        is demoted to freshly compressed data covering the same range, which
        never changes any uncompressed output, so a single left to right
        pass is enough.
        """
        stream = state.stream
        records: List[Tuple[bytes, int, Optional[int]]] = []
        pos = 0
        for cand in sorted(chosen, key=lambda c: c.start):
            if cand.start < pos:
                raise ValueError("Internal error: overlapping placements")
            while pos < cand.start:
                chunk = stream[pos : min(pos + CHUNK_SIZE, cand.start)]
                records.append((_fresh_record(chunk), len(chunk), None))
                pos += len(chunk)
            block = self.blocks[cand.receipt]
            records.append((block.record, block.cb_uncomp, cand.receipt))
            pos = cand.end
        while pos < len(stream):
            chunk = stream[pos : pos + CHUNK_SIZE]
            records.append((_fresh_record(chunk), len(chunk), None))
            pos += len(chunk)
        if sum(usize for _record, usize, _receipt in records) != len(stream):
            raise ValueError("Internal error: blocks do not cover the package")

        # validate with the exact parser dictionary chain, demoting as needed
        final: List[Tuple[bytes, int, Optional[int]]] = []
        demoted: List[Dict[str, object]] = []
        pos = 0
        for record, usize, receipt in records:
            if receipt is not None:
                # the parser only offers the previous block's output
                if final:
                    _rec, prev_usize, _r = final[-1]
                    dictionary = stream[pos - prev_usize : pos]
                else:
                    dictionary = b""
                out = _decode_mszip(self.blocks[receipt].data, dictionary)
                if out != stream[pos : pos + usize]:
                    demoted.append(
                        {
                            "receipt": receipt,
                            "offset": pos,
                            "reason": REASON_DEMOTED,
                        }
                    )
                    cursor = pos
                    while cursor < pos + usize:
                        chunk = stream[cursor : min(cursor + CHUNK_SIZE, pos + usize)]
                        final.append((_fresh_record(chunk), len(chunk), None))
                        cursor += len(chunk)
                    pos += usize
                    continue
            final.append((record, usize, receipt))
            pos += usize
        return final, demoted

    def build(self, sheet: Dict[str, object], base_dir: str = ".") -> RepackResult:
        """Build all packages of a handover sheet.

        Args:
            sheet: The parsed handover sheet (see module docstring).
            base_dir: Directory relative ``data`` file entries resolve against.
        Returns:
            A RepackResult with the built packages and verification output.
        """
        specs = sheet.get("packages")
        if not isinstance(specs, list) or not specs:
            raise ValueError("The sheet needs at least one package")
        if not all(isinstance(spec, dict) for spec in specs):
            raise ValueError("Every package must be an object")
        # resolve file contents relative to the sheet location
        states = [self._build_state(spec, base_dir) for spec in specs]

        # generate candidates; every rejected sheet request gets a reason
        for state in states:
            for receipt, positions in state.blockspec.items():
                for pos in positions:
                    verdict = self._check_candidate(receipt, state, pos)
                    if verdict == REASON_OK:
                        block = self.blocks[receipt]
                        state.candidates.append(
                            Candidate(
                                receipt=receipt,
                                start=pos,
                                end=pos + block.cb_uncomp,
                                size=block.cb_uncomp,
                            )
                        )
                    else:
                        state.rejected.append(
                            {"receipt": receipt, "position": pos, "reason": verdict}
                        )

        # jointly pick placements so each voucher is spent at most once
        chosen = _solve([state.candidates for state in states])
        receipt_owner: Dict[int, int] = {}
        for pkg_idx, picks in enumerate(chosen):
            for cand in picks:
                receipt_owner[cand.receipt] = pkg_idx

        packages: List[RepackPackage] = []
        demoted_all: List[List[Dict[str, object]]] = []
        for pkg_idx, (state, picks) in enumerate(zip(states, chosen)):
            records, demoted = self._emit_records(state, picks)
            demoted_all.append(demoted)
            demoted_receipts = {d["receipt"] for d in demoted}
            placements: List[Placement] = []
            fresh_ranges: List[Tuple[int, int]] = []
            pos = 0
            fresh_start: Optional[int] = None
            for record, usize, receipt in records:
                if receipt is None:
                    if fresh_start is None:
                        fresh_start = pos
                else:
                    if fresh_start is not None:
                        fresh_ranges.append((fresh_start, pos - fresh_start))
                        fresh_start = None
                    block = self.blocks[receipt]
                    placements.append(
                        Placement(
                            receipt=receipt,
                            offset=pos,
                            size=usize,
                            checksum=block.checksum,
                            compressed_size=block.cb_data,
                        )
                    )
                pos += usize
            if fresh_start is not None:
                fresh_ranges.append((fresh_start, pos - fresh_start))

            # classify every feasible sheet request that was not placed
            placed_at = {p.receipt: p.offset for p in placements}
            for cand in state.candidates:
                if placed_at.get(cand.receipt) == cand.start:
                    continue
                if cand.receipt in demoted_receipts:
                    continue  # reported as demoted
                owner = receipt_owner.get(cand.receipt)
                if owner is not None and owner != pkg_idx:
                    reason = REASON_VOUCHER_TAKEN
                elif owner == pkg_idx:
                    reason = REASON_USED_AT_OTHER_POSITION
                else:
                    reason = REASON_NOT_SELECTED
                state.rejected.append(
                    {"receipt": cand.receipt, "position": cand.start, "reason": reason}
                )

            total = len(state.stream)
            reused = sum(p.size for p in placements)
            out_name = str(state.spec.get("output", f"repack-{pkg_idx}.cab"))
            cab_bytes = assemble_cab(
                state.files, [record for record, _usize, _r in records], self.set_id
            )
            packages.append(
                RepackPackage(
                    name=out_name,
                    cab_bytes=cab_bytes,
                    files=[fname for fname, _cffile in state.files],
                    total_uncompressed=total,
                    reused_bytes=reused,
                    fresh_bytes=total - reused,
                    placements=placements,
                    fresh_ranges=fresh_ranges,
                    rejected=state.rejected,
                    demoted=demoted,
                )
            )

        self._states = states
        self._chosen = [list(picks) for picks in chosen]
        self._demoted = demoted_all
        result = RepackResult(packages=packages)
        result.verification = self.verify(result)
        return result

    def verify(self, result: RepackResult) -> List[str]:
        """Reopen every package and check it against the handover sheet.

        Confirms file names, bytes and the fixed order, and that every reused
        block kept its compressed body and checksum byte for byte.
        """
        problems: List[str] = []
        for pkg, state in zip(result.packages, self._states):
            try:
                arc = CabArchive(pkg.cab_bytes)
            except (CorruptionError, NotSupportedError) as e:
                problems.append(f"{pkg.name}: cannot be reopened: {e}")
                continue
            names = list(arc.keys())
            # the cabinet stores win32 path separators, compare like for like
            expected = [name.replace("/", "\\") for name, _cffile in state.files]
            if names != expected:
                problems.append(f"{pkg.name}: file order {names} != {expected}")
                continue
            for (name, cffile), arc_name in zip(state.files, expected):
                if arc[arc_name].buf != (cffile.buf or b""):
                    problems.append(f"{pkg.name}: contents of {name} differ")
            try:
                _set_id, blocks = _parse_source_blocks(pkg.cab_bytes)
            except (CorruptionError, NotSupportedError) as e:
                problems.append(f"{pkg.name}: cannot re-read CFDATA records: {e}")
                continue
            by_offset = {block.offset: block for block in blocks}
            for placement in pkg.placements:
                found = by_offset.get(placement.offset)
                source = self.blocks[placement.receipt]
                if found is None:
                    problems.append(
                        f"{pkg.name}: no block at offset {placement.offset}"
                    )
                elif found.record != source.record:
                    problems.append(
                        f"{pkg.name}: receipt {placement.receipt} at "
                        f"{placement.offset} not preserved verbatim"
                    )
        return problems

    def explain(self, package: int, receipt: int, position: int) -> str:
        """Explain why a source block can or cannot be grafted somewhere.

        Returns one of the REASON_* strings, telling apart a dictionary that
        changes the decode output, a disallowed split position and a voucher
        already spent on the other package.
        """
        if not self._states:
            raise ValueError("build() has not been run yet")
        if receipt < 0 or receipt >= len(self.blocks):
            raise ValueError(f"Unknown receipt number {receipt}")
        state = self._states[package]
        if position not in state.blockspec.get(receipt, []):
            return REASON_POSITION_NOT_ALLOWED
        verdict = self._check_candidate(receipt, state, position)
        if verdict != REASON_OK:
            return verdict
        for demoted in self._demoted[package]:
            if demoted["receipt"] == receipt and demoted["offset"] == position:
                return REASON_DEMOTED
        for cand in self._chosen[package]:
            if cand.receipt == receipt:
                if cand.start == position:
                    return REASON_OK
                return REASON_USED_AT_OTHER_POSITION
        for pkg_idx, picks in enumerate(self._chosen):
            if pkg_idx == package:
                continue
            if any(cand.receipt == receipt for cand in picks):
                return REASON_VOUCHER_TAKEN
        return REASON_NOT_SELECTED


def main(argv: Optional[List[str]] = None) -> int:
    """Console entry point: repack a cabinet pair from a handover sheet."""
    parser = argparse.ArgumentParser(
        description="Repack a MSZIP cabinet into new deliveries, "
        "reusing original compressed blocks where possible."
    )
    parser.add_argument("sheet", help="handover sheet JSON file")
    parser.add_argument(
        "--outdir",
        default=None,
        help="directory for the new cabinets (default: sheet directory)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="write the JSON handover report here (default: stdout)",
    )
    args = parser.parse_args(argv)

    sheet_dir = os.path.dirname(os.path.abspath(args.sheet))
    with open(args.sheet, "r", encoding="utf-8") as f:
        sheet = json.load(f)
    if "source" not in sheet:
        print("The sheet needs a source cabinet", file=sys.stderr)
        return 1
    with open(os.path.join(sheet_dir, sheet["source"]), "rb") as f:
        source_buf = f.read()

    repacker = Repacker(source_buf)
    result = repacker.build(sheet, base_dir=sheet_dir)
    result.save(args.outdir or sheet_dir)

    report = json.dumps(result.report(), indent=2, sort_keys=True)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    else:
        print(report)

    for pkg in result.packages:
        print(
            f"{pkg.name}: reused {pkg.reused_bytes} "
            f"of {pkg.total_uncompressed} bytes "
            f"({len(pkg.placements)} blocks grafted)"
        )
    if result.verification:
        for problem in result.verification:
            print(f"verification: {problem}", file=sys.stderr)
        return 1
    print("verification: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
