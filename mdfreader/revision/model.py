"""Data model for the MDF4 invalidation-bit revision work order.

The archive hands the planning stage a *manifest* (the work order) plus channel
descriptions.  The original volume's data blocks are not opened at this stage,
so every structure here is derived from the manifest alone.

A manifest describes the channel groups whose records were wholesale marked
invalid.  For every repairable channel it lists the approved in-group sample
numbers, the physical block each sample lives in, the absolute invalidation
byte offset inside the decompressed / record-laid-out buffer and the original
value of each candidate invalidation bit.

Two storage layouts are supported:

* ``interleaved`` -- a mixed (unsorted) group whose fixed-length records are
  fully contained in a single block and whose invalidation bytes trail each
  record.
* ``column`` -- a column oriented group using a single DV / DI pair; the
  invalidation bits live in the companion DI block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

# Layout identifiers
INTERLEAVED = "interleaved"
COLUMN = "column"

# Where the invalidation byte is patched inside the decompressed block buffer
ROLE_DATA = "data"      # invalidation bytes embedded in the (mixed) record stream
ROLE_INVALID = "invalid"  # column oriented companion DI block


class ManifestError(ValueError):
    """Raised when the revision manifest is structurally inconsistent."""


@dataclass(frozen=True)
class CandidateBit:
    """A candidate invalidation bit for a channel.

    Attributes
    ----------
    bit_position:
        Bit position inside the channel group invalidation bytes (the value
        stored in ``CN_BLOCK.cn_invalid_bit_pos``).
    occupied_by:
        Names of the *existing* (non work-order) channels that already use this
        very bit position in the same channel group.  Their validity must stay
        byte-for-byte identical, so normalising this bit is only legal when its
        final pattern equals the original physical pattern.
    """

    bit_position: int
    occupied_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sample:
    """One in-group sample of a repairable channel."""

    sample_index: int
    block_id: str
    block_pointer: int
    block_role: str
    invalid_byte_offset: int
    approved: bool
    candidate_values: dict[int, int]  # bit_position -> original bit (0/1)


@dataclass
class RepairChannel:
    """A channel whose invalidation policy has to be revised."""

    channel_name: str
    cn_pointer: int
    data_group: int
    channel_group: int
    current_bit_position: int
    current_flags: int
    samples: list[Sample] = field(default_factory=list)
    candidate_bits: dict[int, CandidateBit] = field(default_factory=dict)

    @property
    def group_key(self) -> tuple[int, int]:
        return self.data_group, self.channel_group

    def ordered_samples(self) -> list[Sample]:
        return sorted(self.samples, key=lambda sample: sample.sample_index)

    def original_pattern(self, bit_position: int) -> tuple[int, ...]:
        """Original physical bit pattern across the channel samples."""
        return tuple(
            sample.candidate_values[bit_position]
            for sample in self.ordered_samples()
        )

    def target_pattern(self) -> tuple[int, ...]:
        """Desired invalidation pattern: approved samples valid (0), rest invalid (1)."""
        return tuple(0 if sample.approved else 1 for sample in self.ordered_samples())


@dataclass
class RepairGroup:
    """A record group carrying one or two repairable channels."""

    group_id: str
    layout: str
    data_group: int
    channel_group: int
    record_id: int
    record_id_size: int
    record_length: int
    invalid_bytes: int
    channels: list[RepairChannel] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return self.data_group, self.channel_group

    def block_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for channel in self.channels:
            for sample in channel.samples:
                if sample.block_id not in seen:
                    seen.add(sample.block_id)
                    ids.append(sample.block_id)
        return ids


@dataclass
class Manifest:
    """Parsed revision manifest."""

    groups: list[RepairGroup]
    block_order: list[str]
    block_pointers: dict[str, int]

    def group_by_id(self, group_id: str) -> RepairGroup:
        for group in self.groups:
            if group.group_id == group_id:
                return group
        raise ManifestError(f"unknown group id {group_id!r}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _parse_sample(raw: dict, block_pointers: dict[str, int]) -> Sample:
    sample_index = raw["sample_index"]
    block_id = str(raw["block_id"])
    block_role = raw.get("block_role", ROLE_DATA)
    _require(block_role in (ROLE_DATA, ROLE_INVALID),
             f"sample {sample_index}: invalid block_role {block_role!r}")
    candidate_values = {}
    for bit_raw in raw["candidate_bits"]:
        bit_position = int(bit_raw["bit_position"])
        value = int(bit_raw["original_value"])
        _require(value in (0, 1),
                 f"sample {sample_index}: bit {bit_position} value must be 0 or 1")
        candidate_values[bit_position] = value
    _require(candidate_values, f"sample {sample_index}: no candidate bit values")
    pointer = raw.get("block_pointer", block_pointers.get(block_id))
    _require(pointer is not None,
             f"sample {sample_index}: block {block_id!r} has no pointer")
    pointer = int(pointer)
    block_pointers.setdefault(block_id, pointer)
    return Sample(
        sample_index=int(sample_index),
        block_id=block_id,
        block_pointer=pointer,
        block_role=block_role,
        invalid_byte_offset=int(raw["invalid_byte_offset"]),
        approved=bool(raw["approved"]),
        candidate_values=candidate_values,
    )


def _parse_channel(raw: dict) -> RepairChannel:
    candidate_bits: dict[int, CandidateBit] = {}
    for bit_raw in raw.get("candidate_bits", []):
        bit_position = int(bit_raw["bit_position"])
        occupied = tuple(str(name) for name in bit_raw.get("occupied_by", ()))
        candidate_bits[bit_position] = CandidateBit(bit_position, occupied)
    block_pointers: dict[str, int] = {}
    samples = [_parse_sample(sample_raw, block_pointers)
               for sample_raw in raw.get("samples", [])]
    _require(samples, f"channel {raw.get('channel_name')!r}: no samples")
    _require(1 <= len(candidate_bits) <= 2,
             f"channel {raw.get('channel_name')!r}: expected 1 or 2 candidate bits")
    for sample in samples:
        _require(set(sample.candidate_values) == set(candidate_bits),
                 f"channel {raw.get('channel_name')!r} sample {sample.sample_index}: "
                 "candidate bit set mismatch")
    indexes = [sample.sample_index for sample in samples]
    _require(len(indexes) == len(set(indexes)),
             f"channel {raw.get('channel_name')!r}: duplicate sample indexes")
    return RepairChannel(
        channel_name=str(raw["channel_name"]),
        cn_pointer=int(raw["cn_pointer"]),
        data_group=int(raw["data_group"]),
        channel_group=int(raw["channel_group"]),
        current_bit_position=int(raw.get("current_bit_position", 0)),
        current_flags=int(raw.get("current_flags", 0)),
        samples=samples,
        candidate_bits=candidate_bits,
    )


def _parse_group(raw: dict) -> RepairGroup:
    layout = raw.get("layout", INTERLEAVED)
    _require(layout in (INTERLEAVED, COLUMN),
             f"group {raw.get('group_id')!r}: unknown layout {layout!r}")
    channels = [_parse_channel(channel_raw) for channel_raw in raw.get("channels", [])]
    _require(1 <= len(channels) <= 2,
             f"group {raw.get('group_id')!r}: expected 1 or 2 repairable channels")
    group_keys = {channel.group_key for channel in channels}
    _require(len(group_keys) == 1,
             f"group {raw.get('group_id')!r}: channels span several channel groups")
    for channel in channels:
        _require(channel.group_key == (raw["data_group"], raw["channel_group"]),
                 f"group {raw.get('group_id')!r}: channel {channel.channel_name!r} "
                 "data/channel group mismatch")
        if layout == COLUMN:
            roles = {sample.block_role for sample in channel.samples}
            _require(roles <= {ROLE_INVALID},
                     f"group {raw.get('group_id')!r}: column layout requires DI block roles")
    return RepairGroup(
        group_id=str(raw["group_id"]),
        layout=layout,
        data_group=int(raw["data_group"]),
        channel_group=int(raw["channel_group"]),
        record_id=int(raw.get("record_id", 0)),
        record_id_size=int(raw.get("record_id_size", 0)),
        record_length=int(raw.get("record_length", 0)),
        invalid_bytes=int(raw.get("invalid_bytes", 0)),
        channels=channels,
    )


def parse_manifest(payload: dict | str | Path) -> Manifest:
    """Validate and build a :class:`Manifest` from a JSON mapping / file."""
    if isinstance(payload, (str, Path)):
        with open(payload, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    _require(isinstance(payload, dict), "manifest root must be a JSON object")
    groups = [_parse_group(raw) for raw in payload.get("groups", [])]
    _require(groups, "manifest contains no repair groups")
    _require(len(groups) <= 80, "manifest exceeds the 80 repair group limit")

    block_pointers: dict[str, int] = {}
    for group in groups:
        for channel in group.channels:
            for sample in channel.samples:
                previous = block_pointers.get(sample.block_id)
                if previous is not None and previous != sample.block_pointer:
                    raise ManifestError(
                        f"block {sample.block_id!r} mapped to several pointers")
                block_pointers[sample.block_id] = sample.block_pointer

    explicit_order = payload.get("block_order")
    if explicit_order:
        block_order = [str(block_id) for block_id in explicit_order]
        known = set(block_pointers)
        # The archive physical order may also list blocks that no repair group
        # touches; every referenced block must nevertheless be present.
        missing = known - set(block_order)
        _require(not missing,
                 f"block_order is missing referenced blocks: {sorted(missing)}")
    else:
        # Archive physical order is represented by ascending block address.
        block_order = sorted(block_pointers, key=lambda block_id: block_pointers[block_id])

    group_ids = [group.group_id for group in groups]
    _require(len(group_ids) == len(set(group_ids)), "duplicate group ids")
    return Manifest(groups=groups, block_order=block_order, block_pointers=block_pointers)
