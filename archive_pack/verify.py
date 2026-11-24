"""Read-back verification of written reels against the pack manifest."""

import json
from fractions import Fraction
from pathlib import Path

from segpy.binary_reel_header import FixedLengthTraceFlag
from segpy.reader import create_reader
from segpy.revisions import SegYRevision

from archive_pack.time_codes import TIME_FIELDS, decode_time


class VerificationError(AssertionError):
    """Raised when a written reel does not match its plan or manifest."""


def _read_manifest(manifest_path):
    with open(manifest_path, encoding="utf-8") as handle:
        return json.load(handle)


def verify_plan(output_directory, source_samples, manifest_path=None):
    """Read reels back with an ordinary Segpy reader and verify everything.

    Args:
        output_directory: Directory containing the reels and manifest.
        source_samples: Mapping of
            ``(source_reel, physical_trace_index)`` to the full retained sample
            sequence handed over, used to confirm byte-identical sample values.
        manifest_path: Optional explicit manifest path.

    Returns:
        The loaded manifest dict when every check passes.

    Raises:
        VerificationError: On any mismatch.
    """
    output_directory = Path(output_directory)
    if manifest_path is None:
        manifest_path = output_directory / "pack_manifest.json"
    manifest = _read_manifest(manifest_path)

    covered = {}
    total_traces = 0

    for reel_entry in manifest["reels"]:
        reel_path = output_directory / reel_entry["file"]
        with reel_path.open("rb") as handle:
            reader = create_reader(handle, cache_directory=None)
            _check_reel_header(reader, reel_entry)
            if reader.num_traces() != reel_entry["num_traces"]:
                raise VerificationError(
                    "{}: reader sees {} traces, manifest says {}".format(
                        reel_entry["file"], reader.num_traces(),
                        reel_entry["num_traces"]))

            for trace_index, trace_entry in enumerate(
                    reel_entry["traces"]):
                total_traces += 1
                _check_trace(reader, trace_index, trace_entry,
                             reel_entry, source_samples, covered)

    _check_coverage(manifest, covered)
    manifest["_verified_trace_count"] = total_traces
    return manifest


def _check_reel_header(reader, reel_entry):
    binary = reader.binary_reel_header
    declared = reel_entry["binary_header"]
    if int(binary.format_revision_num) != int(SegYRevision.REVISION_1):
        raise VerificationError(
            "{}: revision is {:#x}, not Rev 1".format(
                reel_entry["file"], int(binary.format_revision_num)))
    if int(binary.fixed_length_trace_flag) != int(
            FixedLengthTraceFlag.FIXED_LENGTH):
        raise VerificationError(
            "{}: fixed-length trace flag is not set".format(
                reel_entry["file"]))
    checks = {
        "sample_interval_us": binary.sample_interval,
        "num_samples": binary.num_samples,
        "data_sample_format": int(binary.data_sample_format),
        "fixed_length_trace_flag": int(binary.fixed_length_trace_flag),
        "num_extended_textual_headers": int(
            binary.num_extended_textual_headers),
    }
    for name, actual in checks.items():
        if actual != declared[name]:
            raise VerificationError(
                "{}: reel header {} = {} but manifest says {}".format(
                    reel_entry["file"], name, actual, declared[name]))


def _check_trace(reader, trace_index, trace_entry, reel_entry,
                 source_samples, covered):
    header = reader.trace_header(trace_index)
    actual_count = reader.num_trace_samples(trace_index)
    if actual_count != trace_entry["num_samples"]:
        raise VerificationError(
            "{} trace {}: catalog reports {} samples, header/manifest "
            "expect {}".format(
                reel_entry["file"], trace_index, actual_count,
                trace_entry["num_samples"]))
    if header.num_samples != trace_entry["num_samples"]:
        raise VerificationError(
            "{} trace {}: header num_samples {} != {}".format(
                reel_entry["file"], trace_index, header.num_samples,
                trace_entry["num_samples"]))
    if header.sample_interval != reel_entry["binary_header"][
            "sample_interval_us"]:
        raise VerificationError(
            "{} trace {}: sample interval disagrees with reel header".format(
                reel_entry["file"], trace_index))
    if header.num_samples != reel_entry["binary_header"]["num_samples"]:
        raise VerificationError(
            "{} trace {}: sample count disagrees with fixed-length reel "
            "header".format(reel_entry["file"], trace_index))

    scalar_code = int(header.time_scalar)
    if scalar_code != trace_entry["time_scalar"]:
        raise VerificationError(
            "{} trace {}: time scalar {} != manifest {}".format(
                reel_entry["file"], trace_index, scalar_code,
                trace_entry["time_scalar"]))

    for field_name in TIME_FIELDS:
        stored = getattr(header, field_name)
        if stored != trace_entry["stored_time_fields"][field_name]:
            raise VerificationError(
                "{} trace {}: stored {} = {} != manifest {}".format(
                    reel_entry["file"], trace_index, field_name, stored,
                    trace_entry["stored_time_fields"][field_name]))
        true_ms = decode_time(stored, scalar_code)
        expected = Fraction(trace_entry["true_time_fields"][field_name])
        if true_ms != expected:
            raise VerificationError(
                "{} trace {}: decoded {} = {} ms != expected {}".format(
                    reel_entry["file"], trace_index, field_name, true_ms,
                    expected))

    key = (trace_entry["source_reel"],
           trace_entry["physical_trace_index"])
    source = source_samples[key]
    handover_start = trace_entry["handover_sample_start"]
    offset = trace_entry["source_sample_start"] - handover_start
    stop = trace_entry["source_sample_stop"] - handover_start
    expected_samples = list(source[offset:stop])
    actual_samples = list(reader.trace_samples(trace_index))
    if actual_samples != expected_samples:
        raise VerificationError(
            "{} trace {}: samples do not match source {} [{}:{}]".format(
                reel_entry["file"], trace_index, key,
                trace_entry["source_sample_start"],
                trace_entry["source_sample_stop"]))

    bucket = covered.setdefault(key, [])
    bucket.append((trace_entry["source_sample_start"],
                   trace_entry["source_sample_stop"],
                   reel_entry["reel_number"],
                   trace_index, trace_entry["segment_index"]))


def _check_coverage(manifest, covered):
    for item_entry in manifest["items"]:
        key = (item_entry["source_reel"],
               item_entry["physical_trace_index"])
        ranges = sorted(covered.get(key, []))
        if not ranges:
            raise VerificationError(
                "Item {}: no output segments found".format(key))
        cursor = item_entry["requested_sample_start"]
        for start, stop, _, _, _ in ranges:
            if start != cursor:
                raise VerificationError(
                    "Item {}: gap or overlap at sample {} (got [{}:{}])".format(
                        key, cursor, start, stop))
            cursor = stop
        if cursor != item_entry["requested_sample_stop"]:
            raise VerificationError(
                "Item {}: coverage ends at {} not {}".format(
                    key, cursor, item_entry["requested_sample_stop"]))
        if len({(start, stop) for start, stop, *_ in ranges}) != len(ranges):
            raise VerificationError(
                "Item {}: duplicate segment ranges".format(key))
