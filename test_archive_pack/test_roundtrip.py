"""End-to-end write/read-back tests using an ordinary Segpy reader."""

import json
from fractions import Fraction

from segpy.binary_reel_header import FixedLengthTraceFlag
from segpy.reader import create_reader
from segpy.revisions import SegYRevision

from archive_pack.dataset import InMemorySourceRepository
from archive_pack.planner import plan_packing
from archive_pack.time_codes import decode_time
from archive_pack.verify import verify_plan
from archive_pack.writer import write_plan
from test_archive_pack.helpers import make_item, make_source


def _build_repo(sources, samples):
    repository = InMemorySourceRepository()
    for source, values in zip(sources, samples):
        repository.add(source, values)
    return repository


def _abcd():
    item_a = make_item("A", 0, cuts=[2, 3, 4])
    item_b = make_item("B", 1, cuts=[2, 4])
    item_c = make_item("C", 2, cuts=[2, 4])
    item_d = make_item("D", 3, cuts=[2, 3, 4])
    sources = [
        make_source(item_a, [10, 11, 12, 13, 14, 15], inline=7, crossline=7),
        make_source(item_b, [20, 21, 22, 23, 24, 25]),
        make_source(item_c, [30, 31, 32, 33, 34, 35]),
        make_source(item_d, [40, 41, 42, 43, 44, 45],
                    inline=11, crossline=12, mute_end_ms=4000),
    ]
    samples = [
        [10, 11, 12, 13, 14, 15],
        [20, 21, 22, 23, 24, 25],
        [30, 31, 32, 33, 34, 35],
        [40, 41, 42, 43, 44, 45],
    ]
    return sources, samples


def test_write_and_verify_abcd(tmp_path):
    sources, samples = _abcd()
    repository = _build_repo(sources, samples)
    plan = plan_packing(sources, max_traces_per_reel=3)
    result = write_plan(plan, repository, tmp_path)

    source_samples = {
        (source.item.source_reel, source.item.physical_trace_index): values
        for source, values in zip(sources, samples)
    }
    manifest = verify_plan(tmp_path, source_samples, result["manifest_file"])
    assert len(manifest["reels"]) == 5


def test_ordinary_reader_sees_consistent_headers_and_samples(tmp_path):
    sources, samples = _abcd()
    repository = _build_repo(sources, samples)
    plan = plan_packing(sources, max_traces_per_reel=3)
    write_plan(plan, repository, tmp_path)

    reel_paths = sorted(tmp_path.glob("reel_*.segy"))
    assert len(reel_paths) == 5
    for path in reel_paths:
        with path.open("rb") as handle:
            reader = create_reader(handle, cache_directory=None)
            binary = reader.binary_reel_header
            assert int(binary.format_revision_num) == int(
                SegYRevision.REVISION_1)
            assert int(binary.fixed_length_trace_flag) == int(
                FixedLengthTraceFlag.FIXED_LENGTH)
            assert reader.num_traces() <= 3
            for trace_index in reader.trace_indexes():
                header = reader.trace_header(trace_index)
                assert header.num_samples == binary.num_samples
                assert header.sample_interval == binary.sample_interval
                assert reader.num_trace_samples(trace_index) == \
                    binary.num_samples
                values = list(reader.trace_samples(trace_index))
                assert len(values) == binary.num_samples


def test_d_mute_end_is_exactly_four_thousand_ms(tmp_path):
    sources, samples = _abcd()
    repository = _build_repo(sources, samples)
    plan = plan_packing(sources, max_traces_per_reel=3)
    write_plan(plan, repository, tmp_path)

    with (tmp_path / "pack_manifest.json").open() as handle:
        manifest = json.load(handle)
    d_entries = [
        trace for reel in manifest["reels"] for trace in reel["traces"]
        if trace["source_label"] == "D"
    ]
    assert len(d_entries) == 3
    for entry in d_entries:
        assert entry["true_time_fields"]["mute_end_time"] == "4000"
        assert entry["stored_time_fields"]["mute_end_time"] == 4000
        assert entry["time_scalar"] == 1


def test_half_millisecond_start_encodes_exactly(tmp_path):
    sources, samples = _abcd()
    repository = _build_repo(sources, samples)
    plan = plan_packing(sources, max_traces_per_reel=3)
    write_plan(plan, repository, tmp_path)

    with (tmp_path / "pack_manifest.json").open() as handle:
        manifest = json.load(handle)
    a_second = [
        trace for reel in manifest["reels"] for trace in reel["traces"]
        if trace["source_label"] == "A"
        and trace["source_sample_start"] == 3
    ][0]
    assert a_second["true_start_time_ms"] == "1/2"
    assert a_second["time_scalar"] == -10
    assert a_second["stored_time_fields"]["delay_recording_time"] == 5
    assert decode_time(5, -10) == Fraction(1, 2)


def test_every_handover_sample_appears_exactly_once(tmp_path):
    sources, samples = _abcd()
    repository = _build_repo(sources, samples)
    plan = plan_packing(sources, max_traces_per_reel=3)
    result = write_plan(plan, repository, tmp_path)
    source_samples = {
        (source.item.source_reel, source.item.physical_trace_index): values
        for source, values in zip(sources, samples)
    }
    manifest = verify_plan(tmp_path, source_samples, result["manifest_file"])

    covered = {key: [] for key in source_samples}
    for reel in manifest["reels"]:
        for trace in reel["traces"]:
            key = (trace["source_reel"], trace["physical_trace_index"])
            covered[key].append(
                (trace["source_sample_start"], trace["source_sample_stop"]))
    for key, ranges in covered.items():
        assert sorted(ranges) == [(0, 2), (2, 4), (4, 6)] or \
            sorted(ranges) == [(0, 3), (3, 6)]


def test_partial_handover_range_roundtrips_with_relative_provenance(tmp_path):
    item = make_item("P", 0, cuts=[3, 5], start=2, stop=8)
    full_samples = [0, 1, 2, 3, 4, 5, 6, 7]
    handed_samples = full_samples[2:8]
    source = make_source(item, handed_samples)
    repository = _build_repo([source], [handed_samples])

    plan = plan_packing([source], max_traces_per_reel=3)
    result = write_plan(plan, repository, tmp_path)
    manifest = verify_plan(
        tmp_path,
        {(item.source_reel, item.physical_trace_index): handed_samples},
        result["manifest_file"])

    ranges = [
        (trace["source_sample_start"], trace["source_sample_stop"])
        for reel in manifest["reels"] for trace in reel["traces"]
    ]
    assert sorted(ranges) == [(2, 5), (5, 8)]


def test_duplicate_inline_crossline_still_yields_two_traces(tmp_path):
    item_b = make_item("B", 1, cuts=[2, 4])
    item_c = make_item("C", 2, cuts=[2, 4])
    sources = [
        make_source(item_b, [20, 21, 22, 23, 24, 25],
                    inline=9, crossline=9),
        make_source(item_c, [30, 31, 32, 33, 34, 35],
                    inline=9, crossline=9),
    ]
    repository = _build_repo(sources, [
        [20, 21, 22, 23, 24, 25],
        [30, 31, 32, 33, 34, 35],
    ])
    plan = plan_packing(sources, max_traces_per_reel=3)
    write_plan(plan, repository, tmp_path)
    for path in sorted(tmp_path.glob("reel_*.segy")):
        with path.open("rb") as handle:
            reader = create_reader(handle, cache_directory=None)
            headers = [reader.trace_header(i) for i in reader.trace_indexes()]
            assert all(h.inline_number == 9 and h.crossline_number == 9
                       for h in headers)
    total = sum(len(reel.segments) for reel in plan.reels)
    assert total == 6
