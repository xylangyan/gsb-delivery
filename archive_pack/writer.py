"""Write planned reels as SEG-Y Rev 1 files plus a provenance manifest."""

import json
from pathlib import Path

from segpy.binary_reel_header import (
    BinaryReelHeader,
    FixedLengthTraceFlag,
)
from segpy.datatypes import data_sample_format_size_in_bytes
from segpy.revisions import SegYRevision
from segpy.toolkit import format_standard_textual_header
from segpy.writer import write_segy

from archive_pack.dataset import ReelDataset
from archive_pack.time_codes import encode_times
from archive_pack.planner import _segment_true_times


def fraction_string(value):
    """Render a :class:`Fraction` exactly as ``numerator/denominator`` text."""
    if value.denominator == 1:
        return str(value.numerator)
    return "{}/{}".format(value.numerator, value.denominator)


def _build_binary_header(reel):
    return BinaryReelHeader(
        reel_num=reel.reel_number,
        sample_interval=reel.sample_interval_us,
        original_field_sample_interval=reel.sample_interval_us,
        num_samples=reel.num_samples,
        original_field_num_samples=reel.num_samples,
        data_sample_format=reel.data_sample_format,
        format_revision_num=SegYRevision.REVISION_1,
        fixed_length_trace_flag=FixedLengthTraceFlag.FIXED_LENGTH,
        num_extended_textual_headers=0,
    )


def _build_textual_header(reel):
    return format_standard_textual_header(
        SegYRevision.REVISION_1,
        company="ARCHIVE RE-PACK",
        processing1="Lossless handover repack via Segpy",
        reel_number=str(reel.reel_number),
        sample_interval=str(reel.sample_interval_us),
        samples_per_trace=str(reel.num_samples),
        bytes_per_sample=str(
            data_sample_format_size_in_bytes(reel.data_sample_format)),
    )


def _trace_manifest_entry(reel, trace_index, global_trace_index, segment,
                          source, scalar_code, stored_times, true_times,
                          handover_sample_start):
    return {
        "output_trace_index": trace_index,
        "file_sequence_num": trace_index + 1,
        "line_sequence_num": global_trace_index + 1,
        "source_reel": segment.item.source_reel,
        "physical_trace_index": segment.item.physical_trace_index,
        "source_label": segment.item.label,
        "segment_index": segment.segment_index,
        "handover_sample_start": handover_sample_start,
        "source_sample_start": segment.start_sample,
        "source_sample_stop": segment.stop_sample,
        "num_samples": segment.num_samples,
        "true_start_time_ms": fraction_string(
            source.sample_time_ms(segment.start_sample)),
        "sample_interval_us": reel.sample_interval_us,
        "time_scalar": scalar_code,
        "true_time_fields": {
            name: fraction_string(value) for name, value in true_times.items()
        },
        "stored_time_fields": dict(stored_times),
        "inline_number": source.inline_number,
        "crossline_number": source.crossline_number,
    }


def write_plan(plan, repository, output_directory, filename_prefix="reel"):
    """Write every reel in ``plan`` and a cross-reference manifest.

    Args:
        plan: A :class:`~archive_pack.model.PackPlan`.
        repository: A source repository providing headers and samples.
        output_directory: Directory created if necessary.
        filename_prefix: Prefix for ``<prefix>_<reel>.segy`` file names.

    Returns:
        A dict with ``reel_files`` (ordered paths) and ``manifest_file``.
    """
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    segment_reel_numbers = {
        segment: reel.reel_number
        for reel in plan.reels
        for segment in reel.segments
    }

    manifest = {
        "seg_y_revision": "1.0",
        "max_traces_per_reel": plan.max_traces_per_reel,
        "reels": [],
        "items": [],
    }
    reel_files = []
    global_trace_index = 0

    for item, segments in sorted(
            plan.segments_by_item.items(),
            key=lambda pair: (pair[0].source_reel,
                              pair[0].physical_trace_index)):
        manifest["items"].append({
            "source_reel": item.source_reel,
            "physical_trace_index": item.physical_trace_index,
            "source_label": item.label,
            "requested_sample_start": item.start_sample,
            "requested_sample_stop": item.stop_sample,
            "permitted_cut_after": list(item.permitted_cut_after_points),
            "max_segment_samples": item.max_segment_samples,
            "delivered_segments": [
                {
                    "reel_number": segment_reel_numbers[segment],
                    "segment_index": segment.segment_index,
                    "source_sample_start": segment.start_sample,
                    "source_sample_stop": segment.stop_sample,
                }
                for segment in segments
            ],
        })

    for reel in plan.reels:
        binary_header = _build_binary_header(reel)
        textual_header = _build_textual_header(reel)
        dataset = ReelDataset(reel, repository, binary_header, textual_header)

        reel_name = "{}_{:03d}.segy".format(filename_prefix, reel.reel_number)
        reel_path = output_directory / reel_name
        with reel_path.open("wb") as file_handle:
            write_segy(file_handle, dataset)
        reel_files.append(str(reel_path))

        trace_entries = []
        for trace_index, segment in enumerate(reel.segments):
            source = repository.source_for(segment.item)
            true_times = _segment_true_times(source, segment)
            scalar_code, stored_times = encode_times(true_times)
            trace_entries.append(_trace_manifest_entry(
                reel, trace_index, global_trace_index, segment, source,
                scalar_code, stored_times, true_times,
                segment.item.start_sample))
            global_trace_index += 1

        manifest["reels"].append({
            "reel_number": reel.reel_number,
            "file": reel_name,
            "num_traces": len(reel.segments),
            "binary_header": {
                "format_revision_num": int(binary_header.format_revision_num),
                "sample_interval_us": binary_header.sample_interval,
                "num_samples": binary_header.num_samples,
                "data_sample_format": int(binary_header.data_sample_format),
                "fixed_length_trace_flag": int(
                    binary_header.fixed_length_trace_flag),
                "num_extended_textual_headers": int(
                    binary_header.num_extended_textual_headers),
            },
            "true_start_time_ms": fraction_string(reel.true_start_ms),
            "traces": trace_entries,
        })

    manifest_path = output_directory / "pack_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, indent=2, sort_keys=False)
        manifest_file.write("\n")

    return {
        "reel_files": reel_files,
        "manifest_file": str(manifest_path),
    }
