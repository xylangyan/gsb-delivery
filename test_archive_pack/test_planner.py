"""Tests for legal segmentation and minimum-reel planning."""

import pytest

from archive_pack.model import UndeliverableRange
from archive_pack.planner import enumerate_segmentations, plan_packing
from test_archive_pack.helpers import make_item, make_source


def test_cut_points_are_one_based_and_converted_to_zero_based():
    item = make_item("A", 0, cuts=[2, 3, 4])
    assert item.permitted_cut_after == (1, 2, 3)


def test_enumeration_respects_permitted_cuts_and_max_length():
    item = make_item("B", 1, cuts=[2, 4])
    segmentations = enumerate_segmentations(item)
    # With six samples, max length three and boundaries only after points
    # two and four, the unique legal partition is 2+2+2.
    assert segmentations == (
        ((0, 2), (2, 4), (4, 6)),
    )


def test_enumeration_offers_every_partition_for_flexible_trace():
    item = make_item("A", 0, cuts=[2, 3, 4])
    segmentations = enumerate_segmentations(item)
    # Fewest segments first; two three-sample segments are feasible.
    assert segmentations[0] == ((0, 3), (3, 6))
    assert ((0, 2), (2, 4), (4, 6)) in segmentations
    # Every partition covers the range exactly using legal boundaries.
    for segmentation in segmentations:
        flattened = [sample for segment in segmentation for sample in segment]
        assert flattened[0] == 0 and flattened[-1] == 6
        assert all(len(segment) <= 3 for segment in segmentation)


def _abc_sources():
    item_a = make_item("A", 0, cuts=[2, 3, 4])
    item_b = make_item("B", 1, cuts=[2, 4])
    item_c = make_item("C", 2, cuts=[2, 4])
    return [
        make_source(item_a, [10, 11, 12, 13, 14, 15]),
        make_source(item_b, [20, 21, 22, 23, 24, 25]),
        make_source(item_c, [30, 31, 32, 33, 34, 35]),
    ]


def test_abc_packs_into_three_reels_with_two_sample_segments():
    plan = plan_packing(_abc_sources(), max_traces_per_reel=3)
    assert plan.num_reels == 3
    for reel in plan.reels:
        assert len(reel.segments) == 3
        assert reel.num_samples == 2
    lengths = {segment.item.label: segment.num_samples
               for segment in plan.all_segments()}
    assert set(lengths.values()) == {2}


def test_abcd_packs_into_five_reels_with_expected_segment_sizes():
    sources = _abc_sources()
    item_d = make_item("D", 3, cuts=[2, 3, 4])
    source_d = make_source(
        item_d, [40, 41, 42, 43, 44, 45],
        inline=11, crossline=12, mute_end_ms=4000)
    sources.append(source_d)

    plan = plan_packing(sources, max_traces_per_reel=3)
    assert plan.num_reels == 5

    sizes = {}
    for segment in plan.all_segments():
        sizes.setdefault(segment.item.label, set()).add(segment.num_samples)
    assert sizes["A"] == {3}
    assert sizes["D"] == {2}
    assert sizes["B"] == {2}
    assert sizes["C"] == {2}


def test_no_physical_trace_appears_twice_in_one_reel():
    plan = plan_packing(_abc_sources(), max_traces_per_reel=3)
    for reel in plan.reels:
        identities = [
            (segment.item.source_reel, segment.item.physical_trace_index)
            for segment in reel.segments
        ]
        assert len(identities) == len(set(identities))


def test_reel_traces_share_required_header_basis():
    plan = plan_packing(_abc_sources(), max_traces_per_reel=3)
    for reel in plan.reels:
        starts = {segment for segment in reel.segments}
        assert all(segment.num_samples == reel.num_samples
                   for segment in starts)


def test_unrepresentable_time_rejects_entire_delivery():
    from fractions import Fraction
    item = make_item("E", 4, cuts=[2, 3, 4])
    source = make_source(item, [50, 51, 52, 53, 54, 55],
                         delay_ms=Fraction(1, 3))
    with pytest.raises(UndeliverableRange) as exc_info:
        plan_packing([source], max_traces_per_reel=3)
    assert exc_info.value.item is item
    assert exc_info.value.field_failures
    assert all(failure.field_name == "delay_recording_time"
               for failure in exc_info.value.field_failures)


def test_capacity_is_respected():
    sources = _abc_sources()
    plan = plan_packing(sources, max_traces_per_reel=2)
    assert all(len(reel.segments) <= 2 for reel in plan.reels)
    assert plan.num_reels > 3


def test_range_without_usable_cut_is_undeliverable():
    # Six samples, at most three per segment, but no interior cut allowed:
    # no legal partition exists, independent of any header field.
    item = make_item("F", 5, cuts=[])
    source = make_source(item, [60, 61, 62, 63, 64, 65])
    with pytest.raises(UndeliverableRange) as exc_info:
        plan_packing([source], max_traces_per_reel=3)
    assert exc_info.value.item is item
    assert exc_info.value.field_failures == ()


def test_manifest_locates_every_segment_by_reel_and_physical_trace(tmp_path):
    from archive_pack.dataset import InMemorySourceRepository
    from archive_pack.writer import write_plan

    sources = _abc_sources()
    samples = [
        [10, 11, 12, 13, 14, 15],
        [20, 21, 22, 23, 24, 25],
        [30, 31, 32, 33, 34, 35],
    ]
    repository = InMemorySourceRepository()
    for source, values in zip(sources, samples):
        repository.add(source, values)
    plan = plan_packing(sources, max_traces_per_reel=3)
    result = write_plan(plan, repository, tmp_path)

    import json
    with open(result["manifest_file"], encoding="utf-8") as handle:
        manifest = json.load(handle)
    located = []
    for reel in manifest["reels"]:
        for trace in reel["traces"]:
            located.append((
                reel["reel_number"],
                trace["physical_trace_index"],
                trace["source_sample_start"],
                trace["source_sample_stop"],
            ))
    # Every physical trace is traced to three contiguous source ranges.
    assert len(located) == 9
    for physical_trace_index in range(3):
        ranges = sorted(
            (start, stop) for _, trace_index, start, stop in located
            if trace_index == physical_trace_index)
        assert ranges == [(0, 2), (2, 4), (4, 6)]
