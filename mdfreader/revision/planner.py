"""Minimum-block retransmission planner.

The planner only consumes the parsed manifest: the original volume's data
blocks are still closed at this stage.  It decides, for every repairable
channel, which single candidate invalidation bit the whole channel group uses,
and which physical data blocks therefore have to be retransmitted.

Semantics
---------
For a channel assigned bit ``b`` the physical bit is normalised across the whole
channel group: approved samples are made valid (bit cleared) while every other
sample is kept invalid (bit set).  Only bytes that actually differ from the
original buffer are rewritten, so a block is billed only when at least one byte
changes.

* A bit already used by another existing channel may only be selected when the
  normalised pattern is identical to the original pattern (those channels keep
  their validity byte-for-byte).
* Two work-order channels of the same group may share a bit only when their
  target patterns are identical: one physical pattern then serves both.

Exact optimisation
------------------
A group's coverage is the interval of physical blocks from its first to its
last candidate bit.  The archive guarantees any block lies in at most three
coverage intervals, so the interaction graph is an interval graph whose clique
number never exceeds three (tree width at most two).  A left-to-right sweep over
the physical block order keeps at most three *active* groups in its frontier;
each group has at most four bit assignments (two channels times two candidate
bits).  Dynamic programming over those states yields the globally minimum
number of distinct changed blocks even when groups chain into one big segment.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Manifest,
    ManifestError,
    RepairChannel,
    RepairGroup,
)


class PlanningConflict(RuntimeError):
    """Raised when no legal bit assignment exists for one or more groups."""

    def __init__(self, conflicts: list["Conflict"]):
        self.conflicts = conflicts
        detail = "; ".join(str(conflict) for conflict in conflicts)
        super().__init__(f"no legal revision arrangement: {detail}")


@dataclass
class Conflict:
    """A legal-arrangement failure with enough detail for the archive report."""

    group_id: str
    channels: tuple[str, ...]
    samples: tuple[int, ...]
    reason: str

    def __str__(self) -> str:
        return (f"group {self.group_id!r} channels {self.channels!r} "
                f"samples {self.samples!r}: {self.reason}")


@dataclass(frozen=True)
class BitAssignment:
    """One legal bit choice for a single channel."""

    channel_name: str
    bit_position: int
    target_pattern: tuple[int, ...]
    changed_samples: tuple[int, ...]
    changed_blocks: frozenset[str]
    occupied: bool


@dataclass(frozen=True)
class GroupOption:
    """A complete bit assignment for every repairable channel of a group."""

    assignments: tuple[BitAssignment, ...]
    changed_blocks: frozenset[str]
    edits: dict[str, tuple["ByteEdit", ...]]

    def bit_for(self, channel_name: str) -> int:
        for assignment in self.assignments:
            if assignment.channel_name == channel_name:
                return assignment.bit_position
        raise KeyError(channel_name)


@dataclass(frozen=True)
class ByteEdit:
    """A single invalidation byte rewrite inside a decompressed block."""

    block_id: str
    block_role: str
    byte_offset: int
    bit_position: int
    target_value: int  # desired bit value (0 or 1)


@dataclass
class GroupPlan:
    group_id: str
    layout: str
    data_group: int
    channel_group: int
    options: list[GroupOption]
    chosen: GroupOption | None = None
    coverage: tuple[int, int] = (0, 0)


@dataclass
class RevisionPlan:
    """Result of a successful planning run."""

    block_order: list[str]
    group_plans: dict[str, GroupPlan]
    retransmit_blocks: list[str]
    block_edits: dict[str, list[ByteEdit]]
    coverage_segments: dict[str, tuple[str, str]]

    @property
    def retransmit_count(self) -> int:
        return len(self.retransmit_blocks)


def _channel_assignments(channel: RepairChannel) -> tuple[list[BitAssignment], list[Conflict]]:
    """All individually legal bit choices for one channel."""
    assignments: list[BitAssignment] = []
    conflicts: list[Conflict] = []
    target = channel.target_pattern()
    ordered = channel.ordered_samples()
    for bit_position, candidate in channel.candidate_bits.items():
        original = channel.original_pattern(bit_position)
        changed_indexes = tuple(
            sample.sample_index
            for sample, old_value, wanted in zip(ordered, original, target)
            if old_value != wanted
        )
        changed_blocks = frozenset(
            sample.block_id
            for sample in ordered
            if sample.sample_index in set(changed_indexes)
        )
        occupied = bool(candidate.occupied_by)
        if occupied and changed_indexes:
            conflicts.append(Conflict(
                group_id="",
                channels=(channel.channel_name,),
                samples=changed_indexes,
                reason=(
                    f"bit {bit_position} is already used by "
                    f"{', '.join(candidate.occupied_by)} and would change; "
                    "existing channel validity must be preserved"
                ),
            ))
            continue
        assignments.append(BitAssignment(
            channel_name=channel.channel_name,
            bit_position=bit_position,
            target_pattern=target,
            changed_samples=changed_indexes,
            changed_blocks=changed_blocks,
            occupied=occupied,
        ))
    return assignments, conflicts


def _build_edits(group: RepairGroup, assignments: tuple[BitAssignment, ...]) -> dict[str, tuple[ByteEdit, ...]]:
    """Collect the minimal byte rewrites implied by a group option."""
    by_channel = {assignment.channel_name: assignment for assignment in assignments}
    edits: dict[str, list[ByteEdit]] = {}
    for channel in group.channels:
        assignment = by_channel[channel.channel_name]
        wanted = dict(zip(
            (sample.sample_index for sample in channel.ordered_samples()),
            assignment.target_pattern,
        ))
        original = dict(zip(
            (sample.sample_index for sample in channel.ordered_samples()),
            channel.original_pattern(assignment.bit_position),
        ))
        for sample in channel.ordered_samples():
            if wanted[sample.sample_index] == original[sample.sample_index]:
                continue
            edits.setdefault(sample.block_id, []).append(ByteEdit(
                block_id=sample.block_id,
                block_role=sample.block_role,
                byte_offset=sample.invalid_byte_offset,
                bit_position=assignment.bit_position,
                target_value=wanted[sample.sample_index],
            ))
    # Channels sharing one bit edit the same physical byte: collapse duplicates
    # and confirm the shared assignment really converges on one target value.
    deduped: dict[str, list[ByteEdit]] = {}
    for block_id, block_edits in edits.items():
        seen: dict[tuple[int, int], tuple[int, str]] = {}
        for edit in block_edits:
            key = (edit.byte_offset, edit.bit_position)
            if key in seen and seen[key][0] != edit.target_value:
                raise PlanningConflict([Conflict(
                    group_id=group.group_id,
                    channels=tuple(channel.channel_name for channel in group.channels),
                    samples=(),
                    reason="shared bit edits disagree on the target bit value",
                )])
            seen.setdefault(key, (edit.target_value, edit.block_role))
        deduped[block_id] = [
            ByteEdit(block_id, role, offset, bit, target)
            for (offset, bit), (target, role) in seen.items()
        ]
    return {block_id: tuple(block_edits) for block_id, block_edits in deduped.items()}


def _group_options(group: RepairGroup) -> tuple[list[GroupOption], list[Conflict]]:
    per_channel: list[tuple[BitAssignment, ...]] = []
    conflicts: list[Conflict] = []
    for channel in group.channels:
        assignments, channel_conflicts = _channel_assignments(channel)
        for conflict in channel_conflicts:
            conflicts.append(Conflict(
                group_id=group.group_id,
                channels=conflict.channels,
                samples=conflict.samples,
                reason=conflict.reason,
            ))
        if not assignments:
            return [], conflicts
        per_channel.append(tuple(assignments))

    options: list[GroupOption] = []
    seen: set[frozenset[tuple[str, int]]] = set()

    def expand(index: int, picked: list[BitAssignment]) -> None:
        if index == len(per_channel):
            signature = frozenset(
                (assignment.channel_name, assignment.bit_position) for assignment in picked
            )
            if signature in seen:
                return
            seen.add(signature)
            # Two channels sharing one physical bit must target the same pattern.
            bit_channels: dict[int, list[BitAssignment]] = {}
            for assignment in picked:
                bit_channels.setdefault(assignment.bit_position, []).append(assignment)
            for shared in bit_channels.values():
                if len(shared) > 1:
                    patterns = {assignment.target_pattern for assignment in shared}
                    if len(patterns) != 1:
                        conflicts.append(Conflict(
                            group_id=group.group_id,
                            channels=tuple(assignment.channel_name for assignment in shared),
                            samples=tuple(
                                index_ for index_, values in enumerate(zip(*patterns))
                                if len(set(values)) > 1
                            ),
                            reason="channels would share a bit but need different validity patterns",
                        ))
                        return
            edits = _build_edits(group, tuple(picked))
            changed_blocks = frozenset(
                block_id
                for assignment in picked
                for block_id in assignment.changed_blocks
            )
            options.append(GroupOption(
                assignments=tuple(picked),
                changed_blocks=changed_blocks,
                edits=edits,
            ))
            return
        for assignment in per_channel[index]:
            picked.append(assignment)
            expand(index + 1, picked)
            picked.pop()

    expand(0, [])
    if not options:
        return [], conflicts
    return options, []


def _validate_manifest_limits(manifest: Manifest) -> dict[str, tuple[int, int]]:
    """Return coverage interval (block-order indexes) per group and check limits."""
    index_of = {block_id: index for index, block_id in enumerate(manifest.block_order)}
    coverage: dict[str, tuple[int, int]] = {}
    for group in manifest.groups:
        indexes = [index_of[block_id] for block_id in group.block_ids()]
        if not indexes:
            raise ManifestError(f"group {group.group_id!r} references no blocks")
        coverage[group.group_id] = (min(indexes), max(indexes))

    # Archive guarantee: a block belongs to at most three coverage intervals.
    overlap: dict[int, int] = {}
    for start, end in coverage.values():
        for position in range(start, end + 1):
            overlap[position] = overlap.get(position, 0) + 1
    overloaded = [manifest.block_order[position]
                  for position, count in overlap.items() if count > 3]
    if overloaded:
        raise ManifestError(
            "blocks exceed the three-group coverage guarantee: "
            + ", ".join(overloaded)
        )
    return coverage


def _sweep(manifest: Manifest, group_plans: dict[str, GroupPlan],
           coverage: dict[str, tuple[int, int]]):
    """Left-to-right sweep over physical blocks.

    A DP state is the frozenset of ``(group_id, option_index)`` choices of the
    groups whose coverage interval currently contains the swept block.  Each
    state carries the minimum changed-block cost reaching it and a lineage that
    accumulates every group decision surviving on that cheapest path.  At most
    three groups are active at once, so the frontier stays tiny.
    """
    groups_by_start: dict[int, list[str]] = {}
    groups_by_end: dict[int, list[str]] = {}
    for group in manifest.groups:
        start, end = coverage[group.group_id]
        groups_by_start.setdefault(start, []).append(group.group_id)
        groups_by_end.setdefault(end, []).append(group.group_id)

    empty = frozenset()
    dp: dict[frozenset, int] = {empty: 0}
    lineage: dict[frozenset, frozenset] = {empty: empty}

    for block_index, block_id in enumerate(manifest.block_order):
        # Expand newly active groups over all of their legal options.
        expanded: dict[frozenset, int] = {}
        parents: dict[frozenset, frozenset] = {}
        for state, cost in dp.items():
            branches = [(state, state)]
            for group_id in groups_by_start.get(block_index, ()):
                option_count = len(group_plans[group_id].options)
                next_branches = []
                for branch, branch_parent in branches:
                    for option_index in range(option_count):
                        next_branches.append((
                            branch | frozenset(((group_id, option_index),)),
                            branch_parent,
                        ))
                branches = next_branches
            for branch, parent in branches:
                if branch not in expanded or cost < expanded[branch]:
                    expanded[branch] = cost
                    parents[branch] = parent
        dp = expanded
        for state, parent in parents.items():
            lineage[state] = lineage[parent] | state

        # Charge the block once when any active option rewrites it.
        for state in dp:
            changes_block = any(
                block_id in group_plans[group_id].options[option_index].changed_blocks
                for group_id, option_index in state
            )
            if changes_block:
                dp[state] += 1

        # Retire groups whose coverage ends at this block, keeping the cheapest.
        ending = set(groups_by_end.get(block_index, ()))
        if ending:
            merged: dict[frozenset, int] = {}
            for state, cost in dp.items():
                reduced = frozenset(item for item in state if item[0] not in ending)
                if reduced not in merged or cost < merged[reduced]:
                    merged[reduced] = cost
                    lineage[reduced] = lineage[state]
            dp = merged

    if empty not in dp:
        raise PlanningConflict([Conflict(
            group_id="-",
            channels=(),
            samples=(),
            reason="dynamic programming frontier collapsed; no legal assignment",
        )])
    chosen_options = {group_id: option_index
                      for group_id, option_index in lineage[empty]}
    return dp[empty], chosen_options


def plan_revision(manifest: Manifest) -> RevisionPlan:
    """Compute the minimum-block legal revision plan.

    Raises :class:`PlanningConflict` when no legal assignment exists.
    """
    coverage = _validate_manifest_limits(manifest)

    group_plans: dict[str, GroupPlan] = {}
    conflicts: list[Conflict] = []
    for group in manifest.groups:
        options, group_conflicts = _group_options(group)
        group_plans[group.group_id] = GroupPlan(
            group_id=group.group_id,
            layout=group.layout,
            data_group=group.data_group,
            channel_group=group.channel_group,
            options=options,
            coverage=coverage[group.group_id],
        )
        conflicts.extend(group_conflicts)
    if conflicts:
        raise PlanningConflict(conflicts)

    minimum_blocks, chosen_options = _sweep(manifest, group_plans, coverage)

    retransmit: set[str] = set()
    block_edits: dict[str, list[ByteEdit]] = {}
    for group_id, option_index in chosen_options.items():
        option = group_plans[group_id].options[option_index]
        group_plans[group_id].chosen = option
        retransmit.update(option.changed_blocks)
        for block_id, edits in option.edits.items():
            block_edits.setdefault(block_id, []).extend(edits)

    ordered_retransmit = [
        block_id for block_id in manifest.block_order if block_id in retransmit
    ]
    if len(ordered_retransmit) != minimum_blocks:
        raise RuntimeError("internal planning mismatch between cost and edit set")
    segments = {
        group_id: (manifest.block_order[start], manifest.block_order[end])
        for group_id, (start, end) in coverage.items()
    }
    return RevisionPlan(
        block_order=manifest.block_order,
        group_plans=group_plans,
        retransmit_blocks=ordered_retransmit,
        block_edits=block_edits,
        coverage_segments=segments,
    )
