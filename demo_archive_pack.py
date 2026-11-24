#!/usr/bin/env python3
"""End-to-end demonstration of the handover-driven archive packer.

Reproduces the two handover outcomes from the brief:

1. Traces A, B, C (6 samples each, -1 ms start, 500 us interval) with at most
   3 samples per segment and 3 traces per reel -> 3 reels, A in 2-sample
   segments.
2. Adding D, whose mute end time must stay exactly 4000 ms -> 5 reels,
   A switches to two 3-sample segments and D uses three 2-sample segments.

It also shows a genuinely undeliverable segment being rejected rather than
silently rounded.
"""

import shutil
from fractions import Fraction
from pathlib import Path

from segpy.datatypes import DataSampleFormat
from segpy.trace_header import TraceHeaderRev1

from archive_pack.dataset import InMemorySourceRepository
from archive_pack.model import HandoverItem, SourceTrace, UndeliverableRange
from archive_pack.planner import plan_packing
from archive_pack.verify import verify_plan
from archive_pack.writer import write_plan

MAX_SEGMENT_SAMPLES = 3
MAX_TRACES_PER_REEL = 3
SAMPLE_INTERVAL_US = 500
NUM_SAMPLES = 6
START_TIME_MS = Fraction(-1)

OUTPUT_ROOT = Path(__file__).resolve().parent / "demo_output"


def make_item(label, physical_trace_index, cuts):
    return HandoverItem(
        source_reel="legacy_survey",
        physical_trace_index=physical_trace_index,
        start_sample=0,
        stop_sample=NUM_SAMPLES,
        permitted_cut_after=cuts,
        max_segment_samples=MAX_SEGMENT_SAMPLES,
        label=label,
    )


def make_source(item, samples, inline, crossline, mute_end_ms=0,
                delay_ms=START_TIME_MS):
    header = TraceHeaderRev1(
        delay_recording_time=-1,
        mute_end_time=4000 if mute_end_ms else 0,
        sample_interval=SAMPLE_INTERVAL_US,
        num_samples=NUM_SAMPLES,
        inline_number=inline,
        crossline_number=crossline,
    )
    zero_times = {
        name: Fraction(0)
        for name in (
            "uphole_time_at_source", "uphole_time_at_group",
            "source_static_correction", "group_static_correction",
            "total_static", "lag_time_a", "lag_time_b",
            "mute_start_time",
        )
    }
    true_times = dict(zero_times)
    true_times["delay_recording_time"] = Fraction(delay_ms)
    true_times["mute_end_time"] = Fraction(mute_end_ms)
    source = SourceTrace(
        item=item,
        sample_interval_us=SAMPLE_INTERVAL_US,
        data_sample_format=DataSampleFormat.INT16,
        delay_recording_ms=Fraction(delay_ms),
        true_times=true_times,
        inline_number=inline,
        crossline_number=crossline,
        header_template=header,
    )
    return source


def describe(plan):
    lines = []
    for reel in plan.reels:
        descriptions = []
        for segment in reel.segments:
            descriptions.append(
                "{}[{}:{}]".format(
                    segment.item.label,
                    segment.start_sample,
                    segment.stop_sample))
        lines.append(
            "  reel {} ({} traces, {} samples, start {} ms): {}".format(
                reel.reel_number,
                len(reel.segments),
                reel.num_samples,
                reel.true_start_ms,
                ", ".join(descriptions)))
    return "\n".join(lines)


def run_scenario(name, sources_and_samples, output_name):
    repository = InMemorySourceRepository()
    sources = []
    source_samples = {}
    for source, samples in sources_and_samples:
        repository.add(source, samples)
        sources.append(source)
        key = (source.item.source_reel, source.item.physical_trace_index)
        source_samples[key] = samples

    plan = plan_packing(sources, MAX_TRACES_PER_REEL)
    print("Scenario {}: {} reels".format(name, plan.num_reels))
    print(describe(plan))

    output_dir = OUTPUT_ROOT / output_name
    if output_dir.exists():
        shutil.rmtree(output_dir)
    result = write_plan(plan, repository, output_dir)
    verify_plan(output_dir, source_samples, result["manifest_file"])
    print("  wrote and read back {} reels; manifest verified: {}".format(
        len(result["reel_files"]), result["manifest_file"]))
    return plan


def main():
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)

    item_a = make_item("A", 0, cuts=[2, 3, 4])
    item_b = make_item("B", 1, cuts=[2, 4])
    item_c = make_item("C", 2, cuts=[2, 4])
    item_d = make_item("D", 3, cuts=[2, 3, 4])

    source_a = make_source(item_a, [10, 11, 12, 13, 14, 15], 7, 7)
    source_b = make_source(item_b, [20, 21, 22, 23, 24, 25], 9, 9)
    source_c = make_source(item_c, [30, 31, 32, 33, 34, 35], 9, 9)
    source_d = make_source(item_d, [40, 41, 42, 43, 44, 45], 11, 12,
                           mute_end_ms=4000)

    plan_abc = run_scenario(
        "A+B+C",
        [
            (source_a, [10, 11, 12, 13, 14, 15]),
            (source_b, [20, 21, 22, 23, 24, 25]),
            (source_c, [30, 31, 32, 33, 34, 35]),
        ],
        "abc",
    )
    assert plan_abc.num_reels == 3
    abc_lengths = {
        segment.item.label: segment.num_samples
        for segment in plan_abc.all_segments()
    }
    assert all(length == 2 for length in abc_lengths.values())

    plan_abcd = run_scenario(
        "A+B+C+D",
        [
            (source_a, [10, 11, 12, 13, 14, 15]),
            (source_b, [20, 21, 22, 23, 24, 25]),
            (source_c, [30, 31, 32, 33, 34, 35]),
            (source_d, [40, 41, 42, 43, 44, 45]),
        ],
        "abcd",
    )
    assert plan_abcd.num_reels == 5
    abcd_lengths = {}
    for segment in plan_abcd.all_segments():
        abcd_lengths.setdefault(segment.item.label, set()).add(
            segment.num_samples)
    assert abcd_lengths["A"] == {3}
    assert abcd_lengths["D"] == {2}

    print("\nUndeliverable field demonstration:")
    broken_item = make_item("E", 4, cuts=[2, 3, 4])
    broken_source = make_source(
        broken_item, [50, 51, 52, 53, 54, 55], 13, 14,
        delay_ms=Fraction(1, 3))
    try:
        plan_packing([broken_source], MAX_TRACES_PER_REEL)
    except UndeliverableRange as failure:
        print("  rejected as expected: {}".format(failure))
        for field_failure in failure.field_failures:
            print("    - field {}: {} ms ({})".format(
                field_failure.field_name,
                field_failure.true_ms,
                field_failure.reason))
    else:
        raise AssertionError("The unrepresentable trace should be rejected")


if __name__ == "__main__":
    main()
