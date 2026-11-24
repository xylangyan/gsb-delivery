"""Integration tests reading real legacy SEG-Y reels through Segpy."""

import json

from segpy.binary_reel_header import BinaryReelHeader
from segpy.datatypes import DataSampleFormat
from segpy.revisions import SegYRevision
from segpy.toolkit import format_standard_textual_header
from segpy.writer import write_segy

from segpy.dataset import Dataset
from archive_pack.dataset import InMemorySourceRepository
from archive_pack.planner import plan_packing
from archive_pack.source_reader import (
    build_source_trace,
    load_handover_sheet,
    load_sources_from_reels,
)
from archive_pack.verify import verify_plan
from archive_pack.writer import write_plan
from test_archive_pack.helpers import make_item


class _LegacyDataset(Dataset):
    def __init__(self, headers, samples):
        self._headers = headers
        self._samples = samples
        self._binary = BinaryReelHeader(
            sample_interval=500,
            original_field_sample_interval=500,
            num_samples=6,
            original_field_num_samples=6,
            data_sample_format=DataSampleFormat.INT16,
            format_revision_num=SegYRevision.REVISION_1,
        )
        self._textual = format_standard_textual_header(
            SegYRevision.REVISION_1, company="LEGACY")

    @property
    def textual_reel_header(self):
        return self._textual

    @property
    def binary_reel_header(self):
        return self._binary

    @property
    def extended_textual_header(self):
        return []

    def num_traces(self):
        return len(self._headers)

    def trace_indexes(self):
        return range(self.num_traces())

    def trace_header(self, trace_index):
        return self._headers[trace_index]

    def trace_samples(self, trace_index, start=None, stop=None):
        values = self._samples[trace_index]
        return values[start:stop]


def _write_legacy_reel(path, headers, samples):
    with open(path, "wb") as handle:
        write_segy(handle, _LegacyDataset(headers, samples))


def test_loads_sources_from_real_reel_and_repacks(tmp_path):
    from segpy.reader import create_reader
    from segpy.trace_header import TraceHeaderRev1

    headers = [
        TraceHeaderRev1(delay_recording_time=-1, mute_end_time=0,
                        sample_interval=500, num_samples=6,
                        inline_number=7, crossline_number=7),
        TraceHeaderRev1(delay_recording_time=-1, mute_end_time=4000,
                        sample_interval=500, num_samples=6,
                        inline_number=11, crossline_number=12),
    ]
    samples = [
        [10, 11, 12, 13, 14, 15],
        [40, 41, 42, 43, 44, 45],
    ]
    source_path = tmp_path / "legacy.segy"
    _write_legacy_reel(source_path, headers, samples)

    sheet = {
        "max_segment_samples": 3,
        "max_traces_per_reel": 3,
        "items": [
            {"source_reel": "legacy", "physical_trace_index": 0,
             "label": "A", "start_sample": 0, "stop_sample": 6,
             "permitted_cut_after": [2, 3, 4]},
            {"source_reel": "legacy", "physical_trace_index": 1,
             "label": "D", "start_sample": 0, "stop_sample": 6,
             "permitted_cut_after": [2, 3, 4]},
        ],
    }
    sheet_path = tmp_path / "handover.json"
    sheet_path.write_text(json.dumps(sheet), encoding="utf-8")
    loaded = load_handover_sheet(sheet_path)
    assert loaded["max_traces_per_reel"] == 3
    assert [item.label for item in loaded["items"]] == ["A", "D"]

    sources, source_samples = load_sources_from_reels(
        {"legacy": str(source_path)}, loaded["items"])

    with open(source_path, "rb") as handle:
        reader = create_reader(handle, cache_directory=None)
        source_a = build_source_trace(reader, 0, loaded["items"][0])
        assert source_a.sample_interval_us == 500
        assert source_a.delay_recording_ms == -1

    plan = plan_packing(sources, max_traces_per_reel=3)
    repository = InMemorySourceRepository()
    for source in sources:
        key = (source.item.source_reel, source.item.physical_trace_index)
        repository.add(source, source_samples[key])

    output_dir = tmp_path / "out"
    result = write_plan(plan, repository, output_dir)
    verify_plan(output_dir, source_samples, result["manifest_file"])
    assert plan.num_reels >= 2


def test_partial_range_reads_only_handover_samples(tmp_path):
    from segpy.reader import create_reader
    from segpy.trace_header import TraceHeaderRev1

    headers = [TraceHeaderRev1(delay_recording_time=-1,
                               sample_interval=500, num_samples=8)]
    samples = [[0, 1, 2, 3, 4, 5, 6, 7]]
    source_path = tmp_path / "legacy.segy"
    _write_legacy_reel(source_path, headers, samples)

    item = make_item("P", 0, cuts=[2, 4], start=2, stop=6)
    with open(source_path, "rb") as handle:
        reader = create_reader(handle, cache_directory=None)
        source = build_source_trace(reader, 0, item)
        from archive_pack.source_reader import read_source_samples
        handed = read_source_samples(reader, 0, item)
    assert handed == [2, 3, 4, 5]
    assert source.item.num_samples == 4
