"""Build :class:`SourceTrace` objects from legacy SEG-Y reels via Segpy."""

import json

from segpy.reader import create_reader

from archive_pack.model import HandoverItem, SourceTrace
from archive_pack.time_codes import read_true_times


def build_source_trace(reader, trace_index, item):
    """Derive a :class:`SourceTrace` from an open legacy Segpy reader.

    The source header's time fields are decoded to true millisecond values
    using its own time scalar, so the original time basis is preserved
    independently of whatever scalar the output headers choose.
    """
    header = reader.trace_header(trace_index)
    true_times = read_true_times(header)
    data_sample_format = reader.binary_reel_header.data_sample_format
    return SourceTrace(
        item=item,
        sample_interval_us=header.sample_interval,
        data_sample_format=data_sample_format,
        delay_recording_ms=true_times['delay_recording_time'],
        true_times=true_times,
        inline_number=header.inline_number,
        crossline_number=header.crossline_number,
        header_template=header,
    )


def read_source_samples(reader, trace_index, item):
    """Read exactly the handed-over samples from one source trace."""
    return list(reader.trace_samples(
        trace_index, item.start_sample, item.stop_sample))


def load_handover_sheet(path):
    """Load a handover sheet from JSON.

    The sheet structure is::

        {
          "max_segment_samples": 3,
          "max_traces_per_reel": 3,
          "items": [
            {
              "source_reel": "legacy_001",
              "physical_trace_index": 0,
              "label": "A",
              "start_sample": 0,
              "stop_sample": 6,
              "permitted_cut_after": [2, 3, 4]
            }
          ]
        }
    """
    with open(path, encoding="utf-8") as handle:
        sheet = json.load(handle)
    items = [
        HandoverItem(
            source_reel=entry["source_reel"],
            physical_trace_index=entry["physical_trace_index"],
            start_sample=entry["start_sample"],
            stop_sample=entry["stop_sample"],
            permitted_cut_after=entry["permitted_cut_after"],
            max_segment_samples=sheet["max_segment_samples"],
            label=entry.get("label"),
        )
        for entry in sheet["items"]
    ]
    return {
        "max_segment_samples": int(sheet["max_segment_samples"]),
        "max_traces_per_reel": int(sheet["max_traces_per_reel"]),
        "items": items,
    }


def load_sources_from_reels(reel_paths, items, cache_directory=None):
    """Open source reels and build sources plus their handed-over samples.

    Args:
        reel_paths: Mapping of source reel identifier to file path.
        items: Iterable of :class:`HandoverItem`.
        cache_directory: Passed to :func:`segpy.reader.create_reader`.

    Returns:
        A tuple of ``(sources, samples_by_key)`` where ``samples_by_key`` is
        keyed by ``(source_reel, physical_trace_index)``.
    """
    open_readers = {}
    sources = []
    samples_by_key = {}
    try:
        for item in items:
            if item.source_reel not in open_readers:
                handle = open(reel_paths[item.source_reel], "rb")
                open_readers[item.source_reel] = (
                    handle, create_reader(handle, cache_directory=cache_directory))
            _, reader = open_readers[item.source_reel]
            source = build_source_trace(
                reader, item.physical_trace_index, item)
            sources.append(source)
            samples_by_key[(item.source_reel, item.physical_trace_index)] = (
                read_source_samples(reader, item.physical_trace_index, item))
    finally:
        for handle, _ in open_readers.values():
            handle.close()
    return sources, samples_by_key
