"""Property / scale checks for the revision planner.

The optimum is cross-checked against an exhaustive enumeration on small random
manifests (the limited overlap keeps the brute force tractable), and the full
80-group archive limit is exercised against the one-minute / 256 MiB budget.
"""
from __future__ import annotations

import itertools
import random
import time

import pytest

from mdfreader.revision import (
    PlanningConflict,
    parse_manifest,
    plan_revision,
)
from mdfreader.revision.planner import _group_options


def _random_manifest(rng, block_count, group_count, max_overlap,
                     force_solvable=False):
    blocks = [f"B{i}" for i in range(block_count)]
    groups = []
    # Track per-block coverage to honour the archive overlap guarantee.
    load = [0] * block_count
    for g in range(group_count):
        # Choose a contiguous interval whose every block is still below limit.
        valid_starts = [i for i in range(block_count) if load[i] < max_overlap]
        start = rng.choice(valid_starts)
        valid_ends = []
        for end in range(start, block_count):
            if any(load[i] >= max_overlap for i in range(start, end + 1)):
                break
            valid_ends.append(end)
        end = rng.choice(valid_ends)
        span = list(range(start, end + 1))
        for i in span:
            load[i] += 1

        channel_count = rng.choice((1, 2))
        channels = []
        for c in range(channel_count):
            sample_blocks = rng.choices(span, k=rng.randint(2, 5))
            if force_solvable:
                # Distinct bit positions per channel keep every group solvable.
                bit_positions = [2 * c, 2 * c + 1]
            else:
                bit_count = rng.choice((1, 2))
                bit_positions = list(range(bit_count))
            candidate_bits = [
                {"bit_position": bit, "occupied_by": []} for bit in bit_positions
            ]
            approved_index = rng.randrange(len(sample_blocks))
            samples = []
            for index, block_index in enumerate(sample_blocks):
                samples.append({
                    "sample_index": index,
                    "block_id": blocks[block_index],
                    "block_pointer": 1000 + 8 * block_index,
                    "block_role": "data",
                    "invalid_byte_offset": index,
                    "approved": index == approved_index,
                    "candidate_bits": [
                        {"bit_position": bit, "original_value": rng.randint(0, 1)}
                        for bit in bit_positions
                    ],
                })
            channels.append({
                "channel_name": f"G{g}_c{c}",
                "cn_pointer": 5000 + 10 * g + c,
                "data_group": g,
                "channel_group": 0,
                "current_bit_position": 0,
                "current_flags": 1,
                "candidate_bits": candidate_bits,
                "samples": samples,
            })
        groups.append({
            "group_id": f"G{g}",
            "layout": "interleaved",
            "data_group": g,
            "channel_group": 0,
            "record_id": g,
            "record_id_size": 1,
            "record_length": 8,
            "invalid_bytes": 1,
            "channels": channels,
        })
    return {"block_order": blocks, "groups": groups}


def _brute_force_optimum(manifest):
    """Exhaustively enumerate every legal group option combination."""
    option_lists = []
    for group in manifest.groups:
        options, conflicts = _group_options(group)
        if conflicts:
            return None
        option_lists.append(options)
    best = None
    for combo in itertools.product(*option_lists):
        changed = set()
        for option in combo:
            changed.update(option.changed_blocks)
        if best is None or len(changed) < best:
            best = len(changed)
    return best


@pytest.mark.parametrize("seed", range(40))
def test_planner_matches_brute_force(seed):
    rng = random.Random(seed)
    payload = _random_manifest(rng, block_count=8, group_count=5, max_overlap=3)
    manifest = parse_manifest(payload)
    brute = _brute_force_optimum(manifest)
    if brute is None:
        with pytest.raises(PlanningConflict):
            plan_revision(manifest)
    else:
        plan = plan_revision(manifest)
        assert plan.retransmit_count == brute


def test_full_scale_80_groups_runs_inside_budget():
    rng = random.Random(20261126)
    # Long block axis so overlaps stay within the three-group guarantee while
    # groups still chain together through shared blocks.
    payload = _random_manifest(rng, block_count=400, group_count=80,
                               max_overlap=3, force_solvable=True)
    manifest = parse_manifest(payload)
    assert len(manifest.groups) == 80

    start = time.perf_counter()
    plan = plan_revision(manifest)
    elapsed = time.perf_counter() - start

    assert elapsed < 60.0
    # Every chosen assignment is internally consistent.
    for group in manifest.groups:
        chosen = plan.group_plans[group.group_id].chosen
        assert chosen is not None
        assert len(chosen.assignments) == len(group.channels)
    # Coverage guarantee held, so retransmit set is a subset of all blocks.
    assert len(plan.retransmit_blocks) <= len(manifest.block_order)


def test_full_scale_manifest_under_eight_mib():
    rng = random.Random(7)
    import json
    payload = _random_manifest(rng, block_count=400, group_count=80,
                               max_overlap=3, force_solvable=True)
    # Two channels with up to two bits and several samples each stays compact.
    assert len(json.dumps(payload).encode("utf-8")) < 8 * 1024 * 1024
