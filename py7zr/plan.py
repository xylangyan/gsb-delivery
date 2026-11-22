#!/usr/bin/env python
#
# p7zr library
#
# Copyright (c) 2026 Hiroshi Miura <miurahr@linux.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
"""Delivery planning for solid 7z archives.

A 7z folder is decoded as one solid block.  The decoder cannot be paused and
must start again from the block's first packed byte, so serving members in
receiver order may require decoding the same block several times.  This module
builds a minimum-cost delivery plan from

* the block and substream boundaries stored in the archive header,
* the receiver's ordered list of members (repeats allowed),
* a per-block open cost, and
* a staging quota in bytes.

Only one block is decoded at a time.  Opening a block makes every member of
that block available; the planner may keep any of them in staging, each
charged at its own uncompressed size.  The member currently being handed over
does not count against the quota.  A repeated request for the same member is
delivered again and may reuse a staged copy, so it costs nothing beyond the
bytes retained.

The plan records the staging high-water mark of every delivery.  Execution
checks each decode against the real substream boundaries from the header.  If
the header stores only a whole-block checksum, members may be delivered
provisionally while the tail is still being decoded; their verification state
changes to verified only after the checksum passes.  Verified whole-block bytes
may be kept in a :class:`VerificationCache` and reused for the same archive
version.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any, Iterable, Sequence

from py7zr.exceptions import Bad7zFile, StagingQuotaError, SubstreamBoundaryError
from py7zr.helpers import calculate_crc32

__all__ = [
    "Block",
    "Delivery",
    "DeliveryPlan",
    "DeliveryStep",
    "ExtractionPlan",
    "PlanBlock",
    "PlanExecution",
    "PlannerProof",
    "PlanExecutor",
    "Substream",
    "VerificationCache",
    "VerificationRecord",
    "block_header_fingerprint",
    "blocks_from_header",
    "build_plan",
    "plan_extraction",
]

#: Beyond this many candidate members per transition the exact subset
#: enumeration is replaced by a next-use ordering.  The planner still returns
#: a feasible plan, and marks it ``optimal=False`` when it had to approximate.
MAX_SUBSET_CANDIDATES = 12


@dataclass(frozen=True)
class Substream:
    """One member-sized piece of a solid block's decompressed output.

    :ivar name: Member name as stored in the archive.
    :ivar block: Index of the solid block (folder) that produces the piece.
    :ivar index: Position of the piece inside the block, starting at zero.
    :ivar offset: Byte offset of the piece inside the block output.
    :ivar size: Uncompressed size in bytes; also the staging cost of keeping a
        copy.
    :ivar crc32: CRC-32 stored for this piece, or ``None``.
    :ivar file_id: Index in ``SevenZipFile.files``.
    """

    name: str
    block: int
    index: int
    offset: int
    size: int
    crc32: int | None
    file_id: int

    @property
    def start(self) -> int:
        return self.offset

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def length(self) -> int:
        return self.size


@dataclass(frozen=True)
class Block:
    """A solid block together with its real header boundaries.

    :ivar index: Folder index.
    :ivar pack_offset: Absolute file offset of the first packed byte.
    :ivar pack_size: Number of packed bytes belonging to the block.
    :ivar unpack_size: Total decompressed size of the block.
    :ivar crc32: Whole-block CRC, or ``None`` when the header stores none.
    :ivar substreams: Member pieces in output order.
    :ivar multi_stream: ``True`` when the folder consumes several packed
        streams. Such a block is still decoded as a unit.
    """

    index: int
    pack_offset: int
    pack_size: int
    unpack_size: int
    crc32: int | None
    substreams: tuple[Substream, ...]
    multi_stream: bool = False

    @property
    def members(self) -> tuple[Substream, ...]:
        return self.substreams

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.substreams)

    @property
    def packed_size(self) -> int:
        return self.pack_size

    @property
    def unpacked_size(self) -> int:
        return self.unpack_size

    @property
    def end(self) -> int:
        return self.pack_offset + self.pack_size

    def substream(self, name: str) -> Substream:
        for s in self.substreams:
            if s.name == name:
                return s
        raise KeyError(name)


PlanBlock = Block


@dataclass(frozen=True)
class DeliveryStep:
    """One member handed to the receiver.

    :ivar name: Requested member name.
    :ivar block: Solid block that produces the member.
    :ivar offset: Byte offset of the member inside the block output.
    :ivar size: Member size in bytes.
    :ivar open_cost: Cost charged for the block decode that produced it.
    :ivar reopened: ``True`` when the block had been decoded before.
    :ivar from_staging: ``True`` when a staged copy served the request and no
        new decode was needed.
    :ivar staged_after: Member names retained after this delivery.
    :ivar staged_bytes_after: Staging bytes in use after this delivery.
    :ivar peak_bytes: Highest staging usage observed up to and including this
        step.
    :ivar continued: ``True`` when the member was produced by continuing the
        decoder that was already open, without a new block open.
    """

    name: str
    block: int
    offset: int
    size: int
    open_cost: int
    reopened: bool
    from_staging: bool
    staged_after: tuple[str, ...]
    staged_bytes_after: int
    peak_bytes: int
    continued: bool = False

    @property
    def bytes(self) -> int:
        return self.size

    @property
    def is_reopen(self) -> bool:
        return self.reopened

    @property
    def is_continuation(self) -> bool:
        return self.continued


@dataclass
class PlannerProof:
    """Header-boundary evidence that the plan is executable.

    :ivar boundaries: ``(block, offset, size)`` for every substream, derived
        from the folder list and the substream size vector.
    :ivar block_spans: ``(block, pack_offset, pack_size)`` for every block,
        derived from PackInfo.
    :ivar header_fingerprint: CRC-32 over the header fields that describe the
        blocks. Equal fingerprints identify an unchanged archive version.
    """

    boundaries: tuple[tuple[int, int, int], ...]
    block_spans: tuple[tuple[int, int, int], ...]
    header_fingerprint: int | None = None


@dataclass
class DeliveryPlan:
    """Cheapest executable delivery plan for a receiver order.

    :ivar order: Receiver order as requested.
    :ivar steps: Delivery steps in receiver order, one per request.
    :ivar total_open_cost: Sum of the cost of every block decode.
    :ivar opens: ``(block, step_index)`` for every block decode, in order.
    :ivar peak_bytes: Highest staging usage reached.
    :ivar quota: Staging quota in bytes.
    :ivar feasible: ``False`` when no schedule obeying *quota* was found.
    :ivar unverifiable: Blocks whose header span is not reachable.
    :ivar proof: Header-boundary evidence for the plan.
    :ivar reason: Explanation when the plan is infeasible.
    :ivar optimal: ``False`` when an approximation was used.
    """

    order: tuple[str, ...]
    steps: list[DeliveryStep]
    total_open_cost: int
    opens: list[tuple[int, int]]
    peak_bytes: int
    quota: int | None
    feasible: bool
    unverifiable: tuple[int, ...]
    proof: PlannerProof
    reason: str | None = None
    optimal: bool = True

    @property
    def total_cost(self) -> int:
        return self.total_open_cost

    @property
    def block_opens(self) -> list[int]:
        return [block for block, _step in self.opens]

    @property
    def peak(self) -> int:
        return self.peak_bytes

    @property
    def open_count(self) -> int:
        return len(self.opens)

    def open_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for block, _step in self.opens:
            counts[block] = counts.get(block, 0) + 1
        return counts

    def steps_for_block(self, block: int) -> list[DeliveryStep]:
        return [s for s in self.steps if s.block == block]

    def replay(self) -> list[tuple[int, int]]:
        """Return the ``(block, step_index)`` decode sequence."""
        return list(self.opens)

    def describe(self) -> str:
        parts = [
            "order=" + "/".join(self.order),
            f"opens={len(self.opens)}",
            f"cost={self.total_open_cost}",
            f"peak={self.peak_bytes}",
            f"quota={self.quota}",
        ]
        if not self.feasible:
            parts.append("feasible=False")
        if self.unverifiable:
            parts.append("unverifiable=" + ",".join(str(b) for b in self.unverifiable))
        return " ".join(parts)

    def execute(self, archive: Any, **kwargs: Any) -> "PlanExecution":
        """Execute this plan against *archive* (a ``SevenZipFile``)."""
        return archive.execute_plan(self, **kwargs)


@dataclass
class Delivery:
    """One member handed to the receiver by plan execution.

    :ivar step: The planned step that produced it.
    :ivar name: Member name.
    :ivar data: Delivered bytes, or ``None`` when not collected or reused
        from a cache that does not retain bytes.
    :ivar verified: ``True`` once a matching checksum has been observed.
    :ivar provisional: ``True`` while the bytes are available but their
        checksum has not passed yet.
    :ivar unverifiable: ``True`` when the header span cannot be read.
    :ivar from_cache: ``True`` when a previously verified block was reused.
    :ivar reason: Why the delivery is provisional or unverifiable.
    """

    step: DeliveryStep
    name: str
    data: bytes | None
    verified: bool
    provisional: bool
    unverifiable: bool = False
    from_cache: bool = False
    reason: str | None = None

    @property
    def block(self) -> int:
        return self.step.block

    @property
    def size(self) -> int:
        return self.step.size


@dataclass
class VerificationRecord:
    """Verification state gathered for one block during execution.

    :ivar block: Block index.
    :ivar block_scope_crc: ``True`` when a checksum covers the whole block.
    :ivar block_verified: ``True`` once the whole-block check passed.
    :ivar members_verified: Member names whose own checksum matched.
    :ivar provisional: Members handed over before their check passed.
    :ivar failed: Members whose checksum did not match.
    :ivar reason: Explanation for an unverifiable or unchecked block.
    """

    block: int
    block_scope_crc: bool
    block_verified: bool = False
    members_verified: tuple[str, ...] = ()
    provisional: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reason: str | None = None


@dataclass
class PlanExecution:
    """Result of executing a :class:`DeliveryPlan`.

    :ivar plan: The executed plan.
    :ivar deliveries: Member deliveries in receiver order.
    :ivar verifications: Per-block verification state.
    :ivar events: Status-change log: ``provisional``, ``verified`` and
        ``failed`` entries with the member or block name.
    :ivar decoded_blocks: Blocks that were decoded, in decode order.
    :ivar reused_blocks: Blocks served from the verification cache.
    :ivar unverifiable_blocks: Blocks whose header span was not reachable.
    :ivar boundary_checks: ``(block, substream_index, end_offset, ok)`` for
        every substream checked against the produced block output.
    """

    plan: DeliveryPlan
    deliveries: list[Delivery]
    verifications: dict[int, VerificationRecord]
    events: list[tuple[str, str]] = field(default_factory=list)
    decoded_blocks: list[int] = field(default_factory=list)
    reused_blocks: list[int] = field(default_factory=list)
    unverifiable_blocks: list[int] = field(default_factory=list)
    boundary_checks: list[tuple[int, int, int, bool]] = field(default_factory=list)
    observed_peak: int = 0
    quota_ok: bool = True

    @property
    def total_delivered(self) -> int:
        return sum(len(d.data) for d in self.deliveries if d.data is not None)

    @property
    def verified(self) -> bool:
        return bool(self.deliveries) and all(d.verified for d in self.deliveries)

    @property
    def provisional_names(self) -> list[str]:
        return [d.name for d in self.deliveries if d.provisional]

    @property
    def unverifiable_names(self) -> list[str]:
        return [d.name for d in self.deliveries if d.unverifiable]

    @property
    def failed_names(self) -> list[str]:
        return [d.name for d in self.deliveries if d.reason and not d.verified and not d.provisional]

    @property
    def verified_names(self) -> list[str]:
        return [d.name for d in self.deliveries if d.verified]

    def by_name(self, name: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.name == name]


class VerificationCache:
    """In-memory cache of verified whole-block bytes for one archive version.

    A block is stored only after its checksum has passed.  The version key
    makes a reused result valid only for the same header and packed layout.
    """

    def __init__(self) -> None:
        self._blocks: dict[tuple[int, int], bytes] = {}

    def get(self, version: int, block: int) -> bytes | None:
        return self._blocks.get((version, block))

    def put(self, version: int, block: int, data: bytes) -> None:
        self._blocks[(version, block)] = data

    def discard(self, version: int, block: int) -> None:
        self._blocks.pop((version, block), None)

    def clear(self) -> None:
        self._blocks.clear()

    def __contains__(self, item: tuple[int, int]) -> bool:
        return item in self._blocks

    def __len__(self) -> int:
        return len(self._blocks)


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def blocks_from_header(
    header: Any,
    pack_data_start: int,
    file_ids: dict[str, int] | None = None,
) -> list[Block]:
    """Build :class:`Block` objects from a parsed archive header.

    The boundaries come only from the header: the folder list, PackInfo
    positions and the SubstreamsInfo size vector.  The function checks that
    the substream sizes add up to each folder's declared output size and
    raises :class:`~py7zr.exceptions.SubstreamBoundaryError` otherwise.

    :param header: Parsed archive header.
    :param pack_data_start: Absolute offset of the first packed byte.
    :param file_ids: Optional map from member name to ``SevenZipFile.files``
        index.  When omitted, archive order is used.
    """
    streams = getattr(header, "main_streams", None)
    if streams is None or streams.unpackinfo is None:
        return []
    folders = streams.unpackinfo.folders
    packinfo = streams.packinfo
    subinfo = streams.substreamsinfo
    if packinfo is None:
        raise Bad7zFile("streams without pack information")
    pack_sizes = list(packinfo.packsizes)
    pack_positions = getattr(packinfo, "packpositions", None)
    if pack_positions is None:
        pack_positions = list(accumulate(pack_sizes, initial=0))
    else:
        pack_positions = list(pack_positions)
    all_files = getattr(getattr(header, "files_info", None), "files", None) or []
    # Non-empty members, in archive order.  Each solid substream consumes one
    # of them; empty files and directories have no substream.
    nonempty = [(idx, info) for idx, info in enumerate(all_files) if not info.get("emptystream")]
    cursor = 0
    stream_cursor = 0
    flat_size_cursor = 0
    flat_sizes = list(subinfo.unpacksizes) if (subinfo is not None and subinfo.unpacksizes is not None) else []
    flat_digests = list(subinfo.digests) if subinfo is not None else []
    flat_digest_defined = list(subinfo.digestsdefined) if subinfo is not None else []
    blocks: list[Block] = []
    for folder_index, folder in enumerate(folders):
        num_instreams = max((coder.get("numinstreams", 1) for coder in folder.coders), default=1)
        if stream_cursor + num_instreams > len(pack_sizes):
            raise SubstreamBoundaryError(
                folder_index,
                f"folder {folder_index} needs {num_instreams} packed streams but the header has "
                f"{len(pack_sizes) - stream_cursor}",
            )
        pack_size = sum(pack_sizes[stream_cursor : stream_cursor + num_instreams])
        pack_offset = pack_data_start + pack_positions[stream_cursor]
        if subinfo is not None and folder_index < len(subinfo.num_unpackstreams_folders):
            num_substreams = subinfo.num_unpackstreams_folders[folder_index]
        else:
            num_substreams = 1
        if subinfo is not None and flat_sizes:
            sizes = flat_sizes[flat_size_cursor : flat_size_cursor + num_substreams]
        elif num_substreams == 1:
            sizes = [folder.get_unpack_size()]
        else:
            declared_total = folder.get_unpack_size()
            if declared_total % num_substreams != 0:
                raise SubstreamBoundaryError(
                    folder_index,
                    f"folder {folder_index} declares {num_substreams} equal substreams but its "
                    f"output size {declared_total} is not divisible by that count",
                )
            sizes = [declared_total // num_substreams] * num_substreams
        if len(sizes) != num_substreams:
            raise SubstreamBoundaryError(
                folder_index,
                f"folder {folder_index} declares {num_substreams} substreams but only {len(sizes)} sizes are available",
            )
        total = sum(sizes)
        declared = folder.get_unpack_size()
        if total != declared:
            raise SubstreamBoundaryError(
                folder_index,
                f"folder {folder_index} substream sizes add up to {total}, header declares {declared}",
            )
        substreams: list[Substream] = []
        offset = 0
        for sub_index, size in enumerate(sizes):
            if cursor >= len(nonempty):
                raise SubstreamBoundaryError(
                    folder_index,
                    "the header declares more substreams than there are non-empty members",
                )
            file_id, info = nonempty[cursor]
            name = info.get("filename", "")
            if file_ids is not None and name in file_ids:
                file_id = file_ids[name]
            crc: int | None = None
            flat_index = flat_size_cursor + sub_index
            if flat_digest_defined and flat_index < len(flat_digest_defined):
                if flat_digest_defined[flat_index]:
                    crc = flat_digests[flat_index]
            if crc is None and folder.digestdefined and num_substreams == 1:
                crc = folder.crc
            substreams.append(
                Substream(
                    name=name,
                    block=folder_index,
                    index=sub_index,
                    offset=offset,
                    size=size,
                    crc32=crc,
                    file_id=file_id,
                )
            )
            offset += size
            cursor += 1
        flat_size_cursor += num_substreams
        stream_cursor += num_instreams
        blocks.append(
            Block(
                index=folder_index,
                pack_offset=pack_offset,
                pack_size=pack_size,
                unpack_size=declared,
                crc32=folder.crc if folder.digestdefined else None,
                substreams=tuple(substreams),
                multi_stream=num_instreams > 1,
            )
        )
    if cursor != len(nonempty):
        raise SubstreamBoundaryError(
            -1,
            f"header has {len(nonempty)} non-empty members but folders describe {cursor} substreams",
        )
    return blocks


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class ExtractionPlan:
    """Planner bound to one archive version.

    :param blocks: Solid blocks in folder order.
    :param open_costs: Cost of decoding each block, as a mapping from block
        index, a sequence indexed by block, or one value for all blocks.
    :param available_end: Optional absolute end of readable bytes. Blocks whose
        packed span lies beyond it are flagged as unverifiable.
    """

    #: Safety valve for pathological inputs.  When a layer grows past this,
    #: the cheapest states are kept and the plan is marked non-optimal.
    max_states = 200_000

    def __init__(self, blocks: Sequence[Block], open_costs: Any = 1, available_end: int | None = None):
        self.blocks: list[Block] = list(blocks)
        self.available_end = available_end
        self.open_costs = _costs_for(self.blocks, open_costs)
        self._by_name: dict[str, list[Substream]] = {}
        self._by_block: dict[int, Block] = {b.index: b for b in self.blocks}
        self._by_fid: dict[int, Substream] = {}
        for block in self.blocks:
            for s in block.substreams:
                self._by_name.setdefault(s.name, []).append(s)
                self._by_fid[s.file_id] = s
        self._name_of_fid = {s.file_id: s.name for s in self._by_fid.values()}
        self._sizes = {s.file_id: s.size for s in self._by_fid.values()}

    @classmethod
    def from_header(
        cls,
        header: Any,
        pack_data_start: int,
        file_ids: dict[str, int] | None = None,
        open_costs: Any = 1,
        available_end: int | None = None,
    ) -> "ExtractionPlan":
        blocks = blocks_from_header(header, pack_data_start, file_ids)
        return cls(blocks, open_costs, available_end)

    @classmethod
    def from_archive(cls, archive: Any, open_costs: Any = 1) -> "ExtractionPlan":
        """Build a planner from a ``SevenZipFile`` in read mode."""
        return cls.from_header(
            archive.header,
            archive.pack_data_start,
            file_ids={f.filename: f.id for f in archive.files},
            open_costs=open_costs,
            available_end=archive.available_end(),
        )

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def member_names(self) -> list[str]:
        return [s.name for b in self.blocks for s in b.substreams]

    def block_for(self, name: str) -> int:
        return self._by_name[name][0].block

    def copy_size(self, file_id: int) -> int:
        """Staging cost of retaining the member with *file_id*."""
        return self._sizes.get(file_id, 0)

    def block(self, index: int) -> Block:
        """Return the block with folder index *index*."""
        return self._by_block[index]

    def member_name(self, file_id: int) -> str:
        """Return the member name for *file_id*."""
        return self._name_of_fid[file_id]

    def unverifiable_blocks(self, available_end: int | None = None) -> list[int]:
        end = self.available_end if available_end is None else available_end
        if end is None:
            return []
        return [b.index for b in self.blocks if b.end > end]

    def proof(self) -> PlannerProof:
        boundaries = tuple((s.block, s.offset, s.size) for b in self.blocks for s in b.substreams)
        spans = tuple((b.index, b.pack_offset, b.pack_size) for b in self.blocks)
        return PlannerProof(boundaries=boundaries, block_spans=spans)

    def build(self, order: Iterable[str], quota: int | None = None) -> DeliveryPlan:
        """Return the cheapest plan delivering *order* within *quota*.

        :param order: Member names in receiver order.  A repeated name is a
            repeated delivery and may reuse a staged copy.
        :param quota: Maximum staging bytes; ``None`` means unbounded.
        :raises KeyError: If a requested member is not in the blocks.
        """
        requested = tuple(order)
        if quota is not None and quota < 0:
            raise ValueError("staging quota must not be negative")
        unknown = [name for name in requested if not self._by_name.get(name)]
        if unknown:
            raise KeyError(unknown[0])
        limit = float("inf") if quota is None else int(quota)
        if not requested:
            return DeliveryPlan(
                order=(),
                steps=[],
                total_open_cost=0,
                opens=[],
                peak_bytes=0,
                quota=quota,
                feasible=True,
                unverifiable=tuple(self.unverifiable_blocks()),
                proof=self.proof(),
            )
        return _Search(self, requested, limit).run()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class _Search:
    """Layered shortest path over staged copies and the open decoder.

    The state after a delivery is:

    * the member copies kept in staging (each charged its own size),
    * the block whose decoder is currently open, if any, and
    * that decoder's cursor: the index of the next substream it will produce.

    A request whose member lies at or after the open block's cursor is served
    by continuing the same decode at no extra cost.  A request for an earlier
    substream of the open block, or for another block, must come from staging
    or pay for a fresh decode from the block's first byte.  Only one decoder
    is ever open, so switching blocks loses the previous cursor.
    """

    def __init__(self, planner: ExtractionPlan, order: tuple[str, ...], limit: float):
        self.planner = planner
        self.order = order
        self.limit = limit
        self.approximated = False
        self.n = len(order)
        self.sub = [planner._by_name[name][0] for name in order]
        self.positions: dict[int, list[int]] = {}
        for i, s in enumerate(self.sub):
            self.positions.setdefault(s.file_id, []).append(i)
        self._bytes_cache: dict[frozenset[int], int] = {}
        self.max_states = planner.max_states

    # -- helpers ----------------------------------------------------------

    def next_use(self, fid: int, after_step: int) -> int | None:
        """First request index at or after *after_step* for a member."""
        positions = self.positions.get(fid)
        if not positions:
            return None
        j = bisect.bisect_left(positions, after_step)
        return positions[j] if j < len(positions) else None

    def _bytes(self, cached: Iterable[int]) -> int:
        key = frozenset(cached)
        found = self._bytes_cache.get(key)
        if found is not None:
            return found
        total = sum(self.planner.copy_size(fid) for fid in key)
        self._bytes_cache[key] = total
        return total

    def _fits(self, cached: Iterable[int]) -> bool:
        return self.limit == float("inf") or self._bytes(cached) <= self.limit

    def _keep(self, fixed: Iterable[int], extra: Iterable[int], after_step: int) -> list[frozenset[int]]:
        """Return feasible retained sets built from ``fixed`` plus ``extra``."""
        fixed_list = [fid for fid in dict.fromkeys(fixed) if self.next_use(fid, after_step) is not None]
        extra_list = [fid for fid in dict.fromkeys(extra) if self.next_use(fid, after_step) is not None]
        candidates = list(dict.fromkeys(fixed_list + extra_list))
        if len(candidates) > MAX_SUBSET_CANDIDATES:
            self.approximated = True
            chosen = set(fixed_list) | set(extra_list)
            return [self._evict(chosen, after_step)]
        results: set[frozenset[int]] = set()
        for mask in range(1 << len(candidates)):
            chosen = {candidates[j] for j in range(len(candidates)) if mask & (1 << j)}
            if self._fits(chosen):
                results.add(frozenset(chosen))
        if not results:
            results.add(frozenset())
        return list(results)

    def _evict(self, chosen: Iterable[int], after_step: int) -> frozenset[int]:
        base = [fid for fid in dict.fromkeys(chosen) if self.next_use(fid, after_step) is not None]
        if self._fits(base):
            return frozenset(base)
        ordered = sorted(base, key=lambda fid: (self.next_use(fid, after_step), fid))
        kept: list[int] = []
        total = 0
        for fid in ordered:
            size = self.planner.copy_size(fid)
            if total + size <= self.limit:
                kept.append(fid)
                total += size
        return frozenset(kept)

    # -- search -----------------------------------------------------------

    # State: (open_block or -1, cursor, staged frozenset)
    # Value: (cost, opens, previous_state, mode)
    def run(self) -> DeliveryPlan:
        State = tuple[int, int, frozenset[int]]
        layers: list[dict[State, tuple[int, int, State | None, tuple[str, int] | None]]] = []
        start: State = (-1, 0, frozenset())
        layer: dict[State, tuple[int, int, State | None, tuple[str, int] | None]] = {start: (0, 0, None, None)}
        layers.append(layer)
        optimal = True
        for step in range(self.n):
            next_layer: dict[State, tuple[int, int, State | None, tuple[str, int] | None]] = {}
            cand = self.sub[step]
            fid = cand.file_id
            block = cand.block
            block_obj = self.planner.block(block)
            for state, (cost, opens, _prev, _mode) in layer.items():
                open_block, cursor, staged = state
                # 1) Staged copy serves the request.
                if fid in staged:
                    rest = set(staged)
                    rest.discard(fid)
                    keep = [fid] if self.next_use(fid, step + 1) is not None else []
                    for after in self._keep(rest, keep, step + 1):
                        self._relax(next_layer, (open_block, cursor, after), cost, opens, state, ("stage", block))
                # 2) Continue the already-open decoder.
                if open_block == block and cand.index >= cursor:
                    new_cursor = cand.index + 1
                    retain = [s.file_id for s in block_obj.substreams]
                    for after in self._keep(staged, retain, step + 1):
                        self._relax(
                            next_layer,
                            (block, new_cursor, after),
                            cost,
                            opens,
                            state,
                            ("continue", block),
                        )
                # 3) Open (or re-open) the block from its first byte.
                if not (open_block == block and cand.index >= cursor):
                    open_cost = self.planner.open_costs.get(block, 1)
                    retain = [s.file_id for s in block_obj.substreams]
                    for after in self._keep(staged, retain, step + 1):
                        self._relax(
                            next_layer,
                            (block, cand.index + 1, after),
                            cost + open_cost,
                            opens + 1,
                            state,
                            ("open", block),
                        )
            if not next_layer:
                return self._infeasible()
            if len(next_layer) > self.max_states:
                optimal = False
                next_layer = self._prune(next_layer, step + 1)
            layers.append(next_layer)
            layer = next_layer
        final_state, value = min(layer.items(), key=lambda item: (item[1][0], item[1][1], item[0]))
        cost, opens, _prev, _mode = value
        decisions: list[tuple[tuple[str, int] | None, State]] = [(None, final_state)] * self.n
        state = final_state
        for step in range(self.n - 1, -1, -1):
            _cost, _opens, prev, mode = layers[step + 1][state]
            decisions[step] = (mode, state)
            if prev is None and step != 0:
                raise AssertionError("broken planner back-pointer")
            state = prev  # type: ignore[assignment]
        return self._make_plan(decisions, cost, opens, optimal and not self.approximated)

    def _relax(
        self,
        store: dict,
        after: tuple,
        cost: int,
        opens: int,
        prev: tuple,
        mode: tuple[str, int],
    ) -> None:
        if not self._fits(after[2]):
            return
        current = store.get(after)
        if current is None or (cost, opens) < (current[0], current[1]):
            store[after] = (cost, opens, prev, mode)

    def _prune(self, layer: dict, after_step: int) -> dict:
        items = sorted(layer.items(), key=lambda item: (item[1][0], item[1][1], -len(item[0][2])))
        return dict(items[: self.max_states])

    def _infeasible(self) -> DeliveryPlan:
        return DeliveryPlan(
            order=self.order,
            steps=[],
            total_open_cost=0,
            opens=[],
            peak_bytes=0,
            quota=None if self.limit == float("inf") else int(self.limit),
            feasible=False,
            unverifiable=tuple(self.planner.unverifiable_blocks()),
            proof=self.planner.proof(),
            reason="no schedule keeps staging within the quota",
        )

    def _make_plan(
        self,
        decisions: list[tuple[tuple[str, int] | None, tuple]],
        cost: int,
        opens: int,
        optimal: bool,
    ) -> DeliveryPlan:
        steps: list[DeliveryStep] = []
        open_log: list[tuple[int, int]] = []
        opened_ever: set[int] = set()
        peak = 0
        for i, (mode, state) in enumerate(decisions):
            sub = self.sub[i]
            _open_block, _cursor, after = state
            kind = mode[0] if mode is not None else "stage"
            if kind == "stage":
                is_reopen = False
                from_staging = True
                continued = False
                step_cost = 0
            elif kind == "continue":
                is_reopen = False
                from_staging = False
                continued = True
                step_cost = 0
            else:
                is_reopen = sub.block in opened_ever
                from_staging = False
                continued = False
                step_cost = self.planner.open_costs.get(sub.block, 1)
                opened_ever.add(sub.block)
                open_log.append((sub.block, i))
            bytes_after = self._bytes(after)
            peak = max(peak, bytes_after)
            steps.append(
                DeliveryStep(
                    name=sub.name,
                    block=sub.block,
                    offset=sub.offset,
                    size=sub.size,
                    open_cost=step_cost,
                    reopened=is_reopen,
                    from_staging=from_staging,
                    staged_after=tuple(self.planner.member_name(fid) for fid in sorted(after)),
                    staged_bytes_after=bytes_after,
                    peak_bytes=peak,
                    continued=continued,
                )
            )
        if cost != sum(s.open_cost for s in steps) or opens != len(open_log):
            raise AssertionError("planner bookkeeping mismatch")
        return DeliveryPlan(
            order=self.order,
            steps=steps,
            total_open_cost=cost,
            opens=open_log,
            peak_bytes=peak,
            quota=None if self.limit == float("inf") else int(self.limit),
            feasible=True,
            unverifiable=tuple(self.planner.unverifiable_blocks()),
            proof=self.planner.proof(),
            optimal=optimal,
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class PlanExecutor:
    """Execute a delivery plan against an archive, verifying real boundaries.

    One block decoder is active at a time. Each decode starts at the block's
    first packed byte and consumes exactly the block's packed span. Consecutive
    planned deliveries from the same block continue that decoder and do not pay
    another open cost; a switch to another block drains and verifies the open
    one first.

    The decompressed output is cut at the substream offsets recorded in the
    header; a mismatch raises
    :class:`~py7zr.exceptions.SubstreamBoundaryError` before any affected
    member is marked verified.

    When the header stores only a whole-block checksum, the current member is
    handed over provisionally as soon as its byte range is complete, and its
    state changes to verified only once the whole-block checksum passes. An
    ``on_status`` callback observes every transition, so a caller can receive
    provisional bytes before the tail arrives. When member checksums exist, a
    member becomes verified as soon as its own checksum agrees.
    """

    def __init__(
        self,
        archive: Any,
        plan: DeliveryPlan,
        block_cache: VerificationCache | None = None,
        collect: bool = True,
        chunk_size: int = 65536,
        on_status: Any = None,
    ) -> None:
        self.archive = archive
        self.plan = plan
        self.block_cache = block_cache
        self.collect = collect
        self.chunk_size = max(1, chunk_size)
        self.on_status = on_status
        self.blocks = {b.index: b for b in self._planner_blocks()}
        self.version = block_header_fingerprint(self.archive.header, self.archive.pack_data_start)
        self.execution = PlanExecution(plan=plan, deliveries=[], verifications={})
        self._staged: dict[int, bytes] = {}
        self._staged_status: dict[int, str] = {}
        self._staged_cache: dict[int, bool] = {}
        self._session: _BlockSession | None = None

    def _planner_blocks(self) -> list[Block]:
        # Rebuild from the same header the plan was built from, so execution
        # never trusts caller-supplied boundaries.
        return blocks_from_header(
            self.archive.header,
            self.archive.pack_data_start,
            file_ids={f.filename: f.id for f in self.archive.files},
        )

    # -- public -----------------------------------------------------------

    def run(self) -> PlanExecution:
        available_end = self.archive.available_end()
        unreachable = {b.index: b for b in self.blocks.values() if available_end is not None and b.end > available_end}
        for step in self.plan.steps:
            delivery = Delivery(step=step, name=step.name, data=None, verified=False, provisional=False)
            self.execution.deliveries.append(delivery)
            block = self.blocks.get(step.block)
            if block is None:
                raise SubstreamBoundaryError(step.block, "plan refers to a block absent from the header")
            if step.block in unreachable:
                self._mark_unreachable(delivery, block, available_end)
            elif step.from_staging:
                self._deliver_from_staging(delivery)
            elif step.continued:
                self._continue_session(delivery, block)
            else:
                self._finish_session()
                self._start_session(block)
                self._continue_session(delivery, block)
            self._prune(step)
        self._finish_session()
        return self.execution

    # -- delivery paths ---------------------------------------------------

    def _mark_unreachable(self, delivery: Delivery, block: Block, available_end: int | None) -> None:
        delivery.unverifiable = True
        delivery.reason = (
            f"block {block.index} packed span {block.pack_offset}+{block.pack_size} exceeds readable end {available_end}"
        )
        if block.index not in self.execution.unverifiable_blocks:
            self.execution.unverifiable_blocks.append(block.index)
            self.execution.verifications[block.index] = VerificationRecord(
                block=block.index,
                block_scope_crc=block.crc32 is not None,
                reason=delivery.reason,
            )
            for s in block.substreams:
                self._event("unverifiable", s.name)

    def _deliver_from_staging(self, delivery: Delivery) -> None:
        step = delivery.step
        fid = self._fid_of(step)
        data = b"" if step.size == 0 else self._staged.get(fid)
        if data is None:
            raise SubstreamBoundaryError(
                step.block,
                f"plan expected a staged copy of {step.name} but none is available",
            )
        delivery.data = data if self.collect else None
        delivery.from_cache = self._staged_cache.get(fid, False)
        status = self._staged_status.get(fid, "provisional")
        record = self.execution.verifications.get(step.block)
        self._set_status(delivery, status, record)

    # -- decoder session --------------------------------------------------

    def _start_session(self, block: Block) -> None:
        cached = self.block_cache.get(self.version, block.index) if self.block_cache is not None else None
        if cached is not None:
            self.execution.reused_blocks.append(block.index)
            record = VerificationRecord(
                block=block.index,
                block_scope_crc=True,
                block_verified=True,
                members_verified=tuple(s.name for s in block.substreams),
            )
            self.execution.verifications[block.index] = record
            self._event("verified", f"block:{block.index}")
            self._session = _BlockSession(
                block=block,
                decompressor=None,
                output=bytearray(cached),
                record=record,
                reused=True,
            )
            return
        folder = self.archive.header.main_streams.unpackinfo.folders[block.index]
        self.archive.fp.seek(block.pack_offset)
        decompressor = folder.get_decompressor(block.pack_size, reset=True)
        record = VerificationRecord(block=block.index, block_scope_crc=block.crc32 is not None)
        self.execution.verifications[block.index] = record
        self._session = _BlockSession(block=block, decompressor=decompressor, output=bytearray(), record=record)

    def _continue_session(self, delivery: Delivery, block: Block) -> None:
        session = self._session
        if session is None or session.block.index != block.index:
            raise SubstreamBoundaryError(block.index, f"plan continues block {block.index} but a different decoder is open")
        self._ensure_range(session, delivery.step.offset + delivery.step.size)
        self._take_data(session, delivery)
        self._set_status(delivery, self._status_for(session, delivery.step), session.record)
        if session.reused:
            delivery.from_cache = True
        if delivery.step.staged_after:
            self._retain(session, delivery.step.staged_after)
        if session.complete:
            self._finalize(session)

    def _ensure_range(self, session: "_BlockSession", end: int) -> None:
        block = session.block
        output = session.output
        total = block.unpack_size
        while len(output) < end and len(output) < total:
            if session.decompressor is None:
                break
            before = self.archive.fp.tell()
            try:
                chunk = session.decompressor.decompress(self.archive.fp, min(total - len(output), self.chunk_size))
            except Exception as exc:
                raise SubstreamBoundaryError(
                    block.index,
                    f"block {block.index} could not be decoded after {len(output)} of {total} bytes: {exc}",
                ) from exc
            if not chunk:
                if self.archive.fp.tell() == before:
                    raise SubstreamBoundaryError(
                        block.index,
                        f"block {block.index} stopped after {len(output)} of {total} bytes",
                    )
                continue
            output.extend(chunk)
        if end > len(output):
            raise SubstreamBoundaryError(
                block.index,
                f"substream for {end} bytes is outside the block output ({len(output)} available)",
            )
        if len(output) >= total:
            self._check_block_end(session)

    def _check_block_end(self, session: "_BlockSession") -> None:
        block = session.block
        if session.checked:
            return
        session.checked = True
        for s in block.substreams:
            end = s.offset + s.size
            ok = end <= len(session.output)
            self.execution.boundary_checks.append((block.index, s.index, end, ok))
            if not ok:
                raise SubstreamBoundaryError(
                    block.index,
                    f"substream {s.name} ends at {end} but block output has {len(session.output)} bytes",
                )
        if session.decompressor is not None and session.decompressor.consumed > block.pack_size:
            raise SubstreamBoundaryError(
                block.index,
                f"block {block.index} consumed {session.decompressor.consumed} packed bytes, "
                f"header declares {block.pack_size}",
            )
        if not session.reused:
            self.execution.decoded_blocks.append(block.index)
        self._finalize(session)

    def _finalize(self, session: "_BlockSession") -> None:
        if session.finalized:
            return
        session.finalized = True
        block = session.block
        output = session.output
        record = session.record
        if block.crc32 is not None:
            actual = calculate_crc32(bytes(output))
            record.block_verified = actual == block.crc32
            if record.block_verified:
                record.members_verified = tuple(s.name for s in block.substreams)
                self._event("verified", f"block:{block.index}")
            else:
                record.failed = tuple(s.name for s in block.substreams)
                record.reason = f"block CRC {actual:08x} does not match {block.crc32:08x}"
                for s in block.substreams:
                    self._event("failed", s.name)
        else:
            verified: list[str] = []
            provisional: list[str] = []
            failed: list[str] = []
            for s in block.substreams:
                piece = bytes(output[s.offset : s.offset + s.size])
                if s.crc32 is None:
                    provisional.append(s.name)
                elif calculate_crc32(piece) == s.crc32:
                    verified.append(s.name)
                else:
                    failed.append(s.name)
            record.members_verified = tuple(verified)
            record.provisional = tuple(provisional)
            record.failed = tuple(failed)
            record.block_verified = not provisional and not failed
            if provisional:
                record.reason = "no checksum is available for this member"
            if failed:
                record.reason = record.reason or "member checksum mismatch"
        # Retroactively settle every delivery from this block, including
        # copies already handed over from staging while the tail was arriving.
        for delivery in self.execution.deliveries:
            if delivery.block == block.index and not delivery.unverifiable:
                self._set_status(delivery, self._status_for(session, delivery.step), record)
        for fid, data in list(self._staged.items()):
            if self._name_of_fid(fid) in set(s.name for s in block.substreams):
                for s in block.substreams:
                    if s.file_id == fid:
                        self._staged_status[fid] = self._status_for(session, s)
                        break
        if record.block_verified and self.block_cache is not None:
            self.block_cache.put(self.version, block.index, bytes(output))

    def _take_data(self, session: "_BlockSession", delivery: Delivery) -> None:
        step = delivery.step
        if step.size == 0:
            delivery.data = b"" if self.collect else None
        elif self.collect:
            delivery.data = bytes(session.output[step.offset : step.offset + step.size])
        else:
            delivery.data = None
        session.deliveries.append(delivery)

    def _retain(self, session: "_BlockSession", names: Iterable[str]) -> None:
        """Make copies available while the block's decoder is still open.

        Retaining a member whose bytes have not been produced yet advances the
        open decoder to that member's end. The advance costs nothing beyond the
        open that is already in progress, and it is what makes a later staged
        delivery possible without reopening the block.
        """
        keep = set(names)
        # Drop copies the plan no longer wants before adding new ones, so the
        # transient set never exceeds the quota.
        self._drop_except(keep)
        for s in session.block.substreams:
            if s.name not in keep:
                continue
            end = s.offset + s.size
            if end > len(session.output):
                self._ensure_range(session, end)
            if end <= len(session.output):
                self._store_staged(session, s)

    def _store_staged(self, session: "_BlockSession", sub: Substream) -> None:
        self._staged[sub.file_id] = bytes(session.output[sub.offset : sub.offset + sub.size])
        self._staged_status[sub.file_id] = self._status_for(session, sub)
        self._staged_cache[sub.file_id] = session.reused

    def _finish_session(self) -> None:
        session = self._session
        if session is None:
            return
        if not session.complete and session.decompressor is not None:
            # Drain the rest so the decoder is not abandoned mid-block.
            self._ensure_range(session, session.block.unpack_size)
        self._finalize(session)
        self._session = None

    def _status_for(self, session: "_BlockSession", step: DeliveryStep | Substream) -> str:
        block = session.block
        record = session.record
        member: Substream | None = step if isinstance(step, Substream) else None
        if member is None:
            for s in block.substreams:
                if s.name == step.name and s.offset == step.offset:
                    member = s
                    break
        if member is not None and member.crc32 is not None:
            end = member.offset + member.size
            if end <= len(session.output):
                piece = bytes(session.output[member.offset : end])
                return "verified" if calculate_crc32(piece) == member.crc32 else "failed"
        if member is not None and member.name in record.failed:
            return "failed"
        if record.block_verified:
            return "verified"
        return "provisional"

    # -- status bookkeeping ----------------------------------------------

    def _set_status(self, delivery: Delivery, status: str, record: VerificationRecord | None) -> None:
        if status == "verified":
            if not delivery.verified:
                self._event("verified", delivery.name)
            delivery.verified = True
            delivery.provisional = False
            delivery.reason = None
        elif status == "failed":
            if delivery.verified or delivery.provisional:
                self._event("failed", delivery.name)
            delivery.verified = False
            delivery.provisional = False
            delivery.reason = (record.reason if record else None) or "checksum mismatch"
        else:
            if not delivery.provisional:
                self._event("provisional", delivery.name)
            delivery.provisional = True
            delivery.verified = False
            delivery.reason = (record.reason if record else None) or "waiting for whole-block checksum"

    def _event(self, kind: str, name: str) -> None:
        self.execution.events.append((kind, name))
        if self.on_status is not None:
            self.on_status(kind, name)

    # -- staging ----------------------------------------------------------

    def _prune(self, step: DeliveryStep) -> None:
        self._drop_except(step.staged_after)
        used = sum(len(data) for data in self._staged.values())
        self.execution.observed_peak = max(self.execution.observed_peak, used)
        if self.plan.quota is not None and used > self.plan.quota:
            self.execution.quota_ok = False
            raise StagingQuotaError(self.plan.quota, used)

    def _drop_except(self, names: Iterable[str]) -> None:
        keep_names = set(names)
        for fid in list(self._staged):
            if self._name_of_fid(fid) not in keep_names:
                self._staged.pop(fid, None)
                self._staged_status.pop(fid, None)
                self._staged_cache.pop(fid, None)

    def _name_of_fid(self, fid: int) -> str:
        for block in self.blocks.values():
            for s in block.substreams:
                if s.file_id == fid:
                    return s.name
        return ""

    def _fid_of(self, step: DeliveryStep) -> int:
        for s in self.blocks[step.block].substreams:
            if s.name == step.name and s.offset == step.offset:
                return s.file_id
        raise KeyError(step.name)


class _BlockSession:
    """Mutable state for the single open decoder."""

    __slots__ = [
        "block",
        "decompressor",
        "output",
        "record",
        "deliveries",
        "checked",
        "finalized",
        "reused",
    ]

    def __init__(self, block: Block, decompressor: Any, output: bytearray, record: VerificationRecord, reused: bool = False):
        self.block = block
        self.decompressor = decompressor
        self.output = output
        self.record = record
        self.deliveries: list[Delivery] = []
        self.checked = False
        self.finalized = False
        self.reused = reused

    @property
    def complete(self) -> bool:
        return len(self.output) >= self.block.unpack_size


def block_header_fingerprint(header: Any, pack_data_start: int) -> int:
    """Return a CRC-32 identifying the block layout of an archive.

    Only fields that determine the decoded block bytes are mixed in: folder
    coders, packed sizes and positions, unpack sizes and substream sizes.
    Two archives with the same value and the same packed layout yield the same
    block content, so verified results may be reused.
    """
    streams = getattr(header, "main_streams", None)
    if streams is None:
        return 0
    parts: list[bytes] = []
    if streams.packinfo is not None:
        parts.append(repr((streams.packinfo.packpos, tuple(streams.packinfo.packsizes))).encode())
    if streams.unpackinfo is not None:
        for folder in streams.unpackinfo.folders:
            coders = tuple((c.get("method"), c.get("numinstreams"), c.get("numoutstreams")) for c in folder.coders)
            parts.append(repr(coders).encode())
            parts.append(repr(tuple(folder.unpacksizes)).encode())
            parts.append(repr(folder.crc).encode())
    if streams.substreamsinfo is not None:
        parts.append(repr(tuple(streams.substreamsinfo.num_unpackstreams_folders)).encode())
        if streams.substreamsinfo.unpacksizes is not None:
            parts.append(repr(tuple(streams.substreamsinfo.unpacksizes)).encode())
        parts.append(repr(tuple(streams.substreamsinfo.digests)).encode())
    parts.append(repr(pack_data_start).encode())
    return calculate_crc32(b"|".join(parts))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _costs_for(blocks: Sequence[Block], open_costs: Any) -> dict[int, int]:
    if isinstance(open_costs, dict):
        return {b.index: int(open_costs.get(b.index, 1)) for b in blocks}
    if isinstance(open_costs, (list, tuple)):
        return {b.index: int(open_costs[i]) if i < len(open_costs) else 1 for i, b in enumerate(blocks)}
    return {b.index: int(open_costs) for b in blocks}


def build_plan(
    plan: ExtractionPlan | Any,
    order: Iterable[str],
    quota: int | None = None,
    *,
    open_costs: Any = None,
    available_end: int | None = None,
) -> DeliveryPlan:
    """Convenience wrapper: build a plan from a planner or a ``SevenZipFile``."""
    if hasattr(plan, "extraction_plan") and not isinstance(plan, ExtractionPlan):
        return plan.extraction_plan(order, staging_quota=quota, open_costs=open_costs, available_end=available_end)
    if open_costs is not None:
        plan = ExtractionPlan(plan.blocks, open_costs, available_end)
    return plan.build(order, quota)


def plan_extraction(
    archive: Any,
    order: Iterable[str],
    staging_quota: int | None = None,
    open_costs: Any = 1,
    available_end: int | None = None,
) -> DeliveryPlan:
    """Build a delivery plan directly from a ``SevenZipFile``."""
    return archive.extraction_plan(
        order,
        staging_quota=staging_quota,
        open_costs=open_costs,
        available_end=available_end,
    )
