"""Cross-check the heuristic/pruned planner against exhaustive search."""

import itertools
from fractions import Fraction

from archive_pack.planner import (
    _compatibility_key,
    enumerate_segmentations,
    feasible_segmentations,
)
from archive_pack.planner import plan_packing
from test_archive_pack.helpers import make_item, make_source


def _brute_force_min_reels(sources, capacity):
    """Exhaustive reference solver for tiny instances."""
    sources_by_item = {source.item: source for source in sources}
    options = feasible_segmentations(sources_by_item)
    items = list(sources_by_item)

    best = [float("inf")]

    def pack(segments, bins):
        if not segments:
            best[0] = min(best[0], len(bins))
            return
        if len(bins) >= best[0]:
            return
        segment = segments[0]
        source = sources_by_item[segment.item]
        key = _compatibility_key(source, segment)
        item = segment.item
        rest = segments[1:]

        for index, bin_ in enumerate(bins):
            bin_key, bin_items = bin_
            if bin_key == key and item not in bin_items and len(bin_items) < capacity:
                new_bins = list(bins)
                new_bins[index] = (bin_key, bin_items | {item})
                pack(rest, new_bins)
        pack(rest, bins + [(key, {item})])

    choices = []
    for item in items:
        item_choices = []
        for _, segments in options[item]:
            item_choices.append(segments)
        choices.append(item_choices)

    for combination in itertools.product(*choices):
        all_segments = [segment for segments in combination for segment in segments]
        pack(all_segments, [])
    return best[0]


def _instance(seed):
    cuts_a = [[2, 3, 4], [2, 4], [3, 4]][seed % 3]
    cuts_b = [[2, 4], [2, 3, 4], [3]][seed % 3]
    cuts_c = [[2, 3, 4], [3], [2, 4]][seed % 3]
    items = [
        make_item("A", 0, cuts=cuts_a),
        make_item("B", 1, cuts=cuts_b),
        make_item("C", 2, cuts=cuts_c),
    ]
    sources = []
    for index, item in enumerate(items):
        delay = Fraction(-1) if seed % 2 == 0 else Fraction(1, 2)
        sources.append(make_source(
            item,
            [index * 10 + sample for sample in range(6)],
            delay_ms=delay,
        ))
    return sources


def test_planner_matches_brute_force_on_small_instances():
    for seed in range(6):
        for capacity in (2, 3):
            sources = _instance(seed)
            expected = _brute_force_min_reels(sources, capacity)
            plan = plan_packing(sources, max_traces_per_reel=capacity)
            assert plan.num_reels == expected, (seed, capacity)


def test_enumeration_is_complete_against_manual_partitions():
    item = make_item("A", 0, cuts=[2, 3, 4])
    segmentations = enumerate_segmentations(item)

    # Enumerate every subset of the internal boundaries (zero-based 1, 2, 3)
    # and keep the partitions whose runs are all at most three samples.
    boundaries = (1, 2, 3)
    manual = set()
    for subset in itertools.chain.from_iterable(
            itertools.combinations(boundaries, size)
            for size in range(len(boundaries) + 1)):
        edges = [0] + [cut + 1 for cut in sorted(subset)] + [6]
        ranges = tuple(zip(edges, edges[1:]))
        if all(right - left <= 3 for left, right in ranges):
            manual.add(ranges)
    assert set(segmentations) == manual
