"""Shared builders for archive-packer tests."""

from fractions import Fraction

from segpy.datatypes import DataSampleFormat
from segpy.trace_header import TraceHeaderRev1

from archive_pack.model import HandoverItem, SourceTrace

TIME_FIELD_NAMES = (
    "uphole_time_at_source",
    "uphole_time_at_group",
    "source_static_correction",
    "group_static_correction",
    "total_static",
    "lag_time_a",
    "lag_time_b",
    "delay_recording_time",
    "mute_start_time",
    "mute_end_time",
)


def make_item(label, physical_trace_index, cuts, start=0, stop=6,
              max_segment=3, source_reel="legacy"):
    return HandoverItem(
        source_reel=source_reel,
        physical_trace_index=physical_trace_index,
        start_sample=start,
        stop_sample=stop,
        permitted_cut_after=cuts,
        max_segment_samples=max_segment,
        label=label,
    )


def make_source(item, samples, inline=9, crossline=9,
                delay_ms=Fraction(-1), mute_end_ms=0,
                sample_interval_us=500,
                data_sample_format=DataSampleFormat.INT16):
    true_times = {name: Fraction(0) for name in TIME_FIELD_NAMES}
    true_times["delay_recording_time"] = Fraction(delay_ms)
    true_times["mute_end_time"] = Fraction(mute_end_ms)
    header = TraceHeaderRev1(
        delay_recording_time=int(delay_ms) if delay_ms.denominator == 1 else 0,
        mute_end_time=int(mute_end_ms),
        sample_interval=sample_interval_us,
        num_samples=len(samples),
        inline_number=inline,
        crossline_number=crossline,
    )
    return SourceTrace(
        item=item,
        sample_interval_us=sample_interval_us,
        data_sample_format=data_sample_format,
        delay_recording_ms=Fraction(delay_ms),
        true_times=true_times,
        inline_number=inline,
        crossline_number=crossline,
        header_template=header,
    )
