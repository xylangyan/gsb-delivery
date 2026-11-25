"""Build the revised MDF4 volume from a minimum-block revision plan.

The rewriter opens the original volume only after planning.  It performs a
generic block-graph walk from the header block: every MDF4 block starts with a
24 byte header whose link count lets us enumerate and relocate all cross-block
pointers without hard-coding every block type.

Retransmitted data blocks are rebuilt (plain DT/DV/DI payloads are edited in
place; DZ blocks are decompressed, patched in their record-laid-out buffer and
recompressed).  Every other data block is reused byte-for-byte.  Metadata
blocks keep their content except for remapped links and the channel flag /
invalidation-bit updates delivered separately from data billing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from zlib import compress, decompress, decompressobj

import numpy as np

from .model import Manifest
from .planner import RevisionPlan

_HEADER = struct.Struct("<4sI2Q")
_DZ = struct.Struct("<2s2BI2Q")

_DATA_IDS = {b"##DT", b"##DV", b"##DI", b"##RD", b"##RV", b"##RI", b"##SD"}
_LIST_IDS = {b"##DL", b"##LD", b"##HL"}
_CN_ID = b"##CN"
_DZ_ID = b"##DZ"

# CN block data section starts after the 24 byte header and 8 link pointers.
# Its layout is _CNStruct2 = "<4B 4I B c H 6d" (72 bytes): the four leading
# bytes are cn_type / cn_sync_type / cn_data_type / cn_bit_offset, then the
# four u32s are cn_byte_offset, cn_bit_count, cn_flags, cn_invalid_bit_pos.
_CN_DATA_OFFSET = 24 + 8 * 8
_CN_FLAGS_OFFSET = _CN_DATA_OFFSET + 4 + 4 + 4          # 100
_CN_INVALID_BIT_OFFSET = _CN_FLAGS_OFFSET + 4           # 104

try:  # optional codecs, mirroring mdfinfo4 availability checks
    from zstandard import ZstdCompressor, ZstdDecompressor

    def _zstd_compress(data: bytes) -> bytes:
        return ZstdCompressor().compress(data)

    def _zstd_decompress(data: bytes) -> bytes:
        return ZstdDecompressor().decompress(data)
except Exception:  # pragma: no cover - exercised only without pyzstd
    _zstd_compress = None
    _zstd_decompress = None

try:
    from lz4.block import compress as _lz4_compress, decompress as _lz4_decompress
except Exception:  # pragma: no cover - exercised only without lz4
    _lz4_compress = None
    _lz4_decompress = None


class RevisionBuildError(RuntimeError):
    """Raised when the revised volume cannot be produced."""


@dataclass
class BlockMeta:
    pointer: int
    block_id: bytes
    length: int
    link_count: int
    links: tuple[int, ...]


@dataclass
class ChannelFlagUpdate:
    cn_pointer: int
    invalid_bit_position: int
    flags: int


def _align8(position: int) -> int:
    remainder = position % 8
    return position if remainder == 0 else position + 8 - remainder


def _decompress(payload: bytes, zip_type: int, zip_parameter: int,
                org_data_length: int) -> bytes:
    if zip_type in (0, 1):
        try:
            raw = decompress(payload)
        except Exception:
            decoder = decompressobj()
            raw = decoder.decompress(payload) + decoder.flush()
    elif zip_type in (2, 3):
        if _zstd_decompress is None:
            raise RevisionBuildError("pyzstd is required to rebuild a ZStandard DZ block")
        raw = _zstd_decompress(payload)
    elif zip_type in (4, 5):
        if _lz4_decompress is None:
            raise RevisionBuildError("lz4 is required to rebuild an LZ4 DZ block")
        raw = _lz4_decompress(payload, uncompressed_size=org_data_length)
    else:
        raise RevisionBuildError(f"unsupported DZ compression type {zip_type}")
    if zip_type in (1, 3, 5):  # inverse column-major transpose
        columns = org_data_length // zip_parameter
        aligned = columns * zip_parameter
        tail = raw[aligned:]
        matrix = np.frombuffer(raw[:aligned], dtype="u1").reshape(zip_parameter, columns)
        raw = matrix.T.tobytes() + (tail if tail else b"")
    return raw


def _recompress(logical: bytes, zip_type: int, zip_parameter: int) -> bytes:
    if zip_type in (1, 3, 5):
        columns = len(logical) // zip_parameter
        aligned = columns * zip_parameter
        tail = logical[aligned:]
        matrix = np.frombuffer(logical[:aligned], dtype="u1").reshape(columns, zip_parameter)
        prepared = matrix.T.tobytes() + (tail if tail else b"")
    else:
        prepared = logical
    if zip_type in (0, 1):
        return compress(prepared)
    if zip_type in (2, 3):
        if _zstd_compress is None:
            raise RevisionBuildError("pyzstd is required to rebuild a ZStandard DZ block")
        return _zstd_compress(prepared)
    if zip_type in (4, 5):
        if _lz4_compress is None:
            raise RevisionBuildError("lz4 is required to rebuild an LZ4 DZ block")
        return _lz4_compress(prepared)
    raise RevisionBuildError(f"unsupported DZ compression type {zip_type}")


def _walk_blocks(source) -> dict[int, BlockMeta]:
    """Enumerate every block reachable from the header block (at offset 64)."""
    blocks: dict[int, BlockMeta] = {}
    stack = [64]
    while stack:
        pointer = stack.pop()
        if not pointer or pointer in blocks:
            continue
        source.seek(pointer)
        header = source.read(24)
        if len(header) < 24:
            raise RevisionBuildError(f"truncated block header at {pointer}")
        block_id, _reserved, length, link_count = _HEADER.unpack(header)
        link_bytes = source.read(8 * link_count)
        links = struct.unpack(f"<{link_count}Q", link_bytes) if link_count else ()
        blocks[pointer] = BlockMeta(pointer, block_id, length, link_count, links)
        stack.extend(link for link in links if link)
    return blocks


def _apply_edits(buffer: bytearray, edits, header_offset: int = 0) -> int:
    """Apply bit rewrites in place and return how many edits were applied."""
    applied = 0
    for edit in edits:
        byte_offset = header_offset + edit.byte_offset + (edit.bit_position >> 3)
        if byte_offset >= len(buffer):
            raise RevisionBuildError(
                f"invalidation offset {byte_offset} outside block of {len(buffer)} bytes")
        mask = 1 << (edit.bit_position & 7)
        if edit.target_value:
            buffer[byte_offset] |= mask
        else:
            buffer[byte_offset] &= ~mask
        applied += 1
    return applied


def _rebuild_dz(source, meta: BlockMeta, edits) -> tuple[bytes, int]:
    """Decompress a DZ block, patch it and recompress with the same codec."""
    source.seek(meta.pointer + 24)
    org_type, zip_type, _reserved, zip_parameter, org_length, dz_length = \
        _DZ.unpack(source.read(24))
    payload = source.read(dz_length)
    logical = bytearray(_decompress(payload, zip_type, zip_parameter, org_length))
    if len(logical) != org_length:
        raise RevisionBuildError("DZ decompressed length mismatch")
    applied = _apply_edits(logical, edits)
    if applied != len(edits):
        raise RevisionBuildError("not every DZ edit could be applied")
    compressed = _recompress(bytes(logical), zip_type, zip_parameter)
    header = _HEADER.pack(_DZ_ID, 0, 48 + len(compressed), 0)
    body = _DZ.pack(org_type, zip_type, 0, zip_parameter, org_length, len(compressed))
    return header + body + compressed, 48 + len(compressed)


def _patch_cn(raw: bytes, update: ChannelFlagUpdate) -> bytes:
    patched = bytearray(raw)
    struct.pack_into("<I", patched, _CN_FLAGS_OFFSET, update.flags)
    struct.pack_into("<I", patched, _CN_INVALID_BIT_OFFSET, update.invalid_bit_position)
    return bytes(patched)


def build_revised_volume(manifest: Manifest, plan: RevisionPlan,
                         source_path: str | Path, output_path: str | Path) -> dict:
    """Write the revised volume and return a delivery summary.

    The summary reports the retransmitted (billed) physical data blocks, the
    channel flag updates and every remapped link block.
    """
    source_path = Path(source_path)
    output_path = Path(output_path)

    edits_by_pointer: dict[int, list] = {}
    for block_id, edits in plan.block_edits.items():
        pointer = manifest.block_pointers[block_id]
        edits_by_pointer.setdefault(pointer, []).extend(edits)

    flag_updates = _collect_flag_updates(manifest, plan)
    cn_pointers = {update.cn_pointer for update in flag_updates}

    with open(source_path, "rb") as source:
        blocks = _walk_blocks(source)
        order = list(blocks)  # discovery order: parents before children

        # Resolve every retransmitted block.  DZ blocks change length when
        # recompressed, so their final bytes are built before addressing.
        rebuilt: dict[int, bytes] = {}
        for pointer, block_edits in edits_by_pointer.items():
            meta = blocks.get(pointer)
            if meta is None:
                raise RevisionBuildError(
                    f"edited block at {pointer} is not reachable from the header")
            if meta.block_id == _DZ_ID:
                rebuilt[pointer], new_length = _rebuild_dz(source, meta, block_edits)
            elif meta.block_id in _DATA_IDS:
                source.seek(meta.pointer)
                raw = bytearray(source.read(meta.length))
                # Offsets are relative to the payload, skip the 24 byte header.
                _apply_edits(raw, block_edits, header_offset=24)
                rebuilt[pointer] = bytes(raw)
            else:
                raise RevisionBuildError(
                    f"cannot edit block of type {meta.block_id!r} at {pointer}")

        cursor = 64
        final_addresses: dict[int, int] = {}
        for pointer in order:
            meta = blocks[pointer]
            final_addresses[pointer] = cursor
            length = len(rebuilt[pointer]) if pointer in rebuilt else meta.length
            cursor = _align8(cursor + length)

        remapped_link_blocks: list[int] = []
        with open(output_path, "wb") as target:
            source.seek(0)
            target.write(source.read(64))  # ID block, no internal pointers

            for pointer in order:
                meta = blocks[pointer]
                target.seek(final_addresses[pointer])
                if pointer in rebuilt:
                    target.write(rebuilt[pointer])
                    if meta.block_id in _LIST_IDS or meta.link_count:
                        remapped_link_blocks.append(pointer)
                    continue

                source.seek(meta.pointer)
                raw = bytearray(source.read(meta.length))
                if meta.link_count:
                    for link_index, old_target in enumerate(meta.links):
                        if not old_target:
                            continue
                        if old_target not in final_addresses:
                            raise RevisionBuildError(
                                f"block {pointer} links to orphan block {old_target}")
                        struct.pack_into(
                            "<Q", raw, 24 + 8 * link_index,
                            final_addresses[old_target],
                        )
                    remapped_link_blocks.append(pointer)

                if pointer in cn_pointers:
                    update = next(item for item in flag_updates
                                  if item.cn_pointer == pointer)
                    raw = bytearray(_patch_cn(bytes(raw), update))

                target.write(bytes(raw))

    return {
        "retransmitted_blocks": [
            {"block_id": block_id, "pointer": manifest.block_pointers[block_id]}
            for block_id in plan.retransmit_blocks
        ],
        "channel_flag_updates": [
            {
                "cn_pointer": update.cn_pointer,
                "invalid_bit_position": update.invalid_bit_position,
                "flags": update.flags,
            }
            for update in flag_updates
        ],
        "remapped_link_blocks": remapped_link_blocks,
        "output_size": cursor,
    }


def _collect_flag_updates(manifest: Manifest,
                          plan: RevisionPlan) -> list[ChannelFlagUpdate]:
    """Turn chosen bits into CN flag / invalidation-bit updates."""
    updates: list[ChannelFlagUpdate] = []
    for group in manifest.groups:
        group_plan = plan.group_plans[group.group_id]
        chosen = group_plan.chosen
        for channel in group.channels:
            new_bit = chosen.bit_for(channel.channel_name)
            # Drop CN_F_ALL_INVALID (bit 0) and drive validity from the bit (bit 1).
            new_flags = (channel.current_flags & ~0b11) | 0b10
            updates.append(ChannelFlagUpdate(
                cn_pointer=channel.cn_pointer,
                invalid_bit_position=new_bit,
                flags=new_flags,
            ))
    return updates
