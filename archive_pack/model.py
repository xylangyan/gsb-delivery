"""Data structures describing a handover and the resulting pack plan."""

from fractions import Fraction

from segpy.datatypes import DataSampleFormat


class LosslessDeliveryError(Exception):
    """Base class for conditions that prevent lossless delivery."""


class UnrepresentableTimeField(LosslessDeliveryError):
    """A true time value cannot be written exactly into a trace header.

    Attributes:
        field_name: Segpy trace-header field name.
        true_ms: The true time in milliseconds (a :class:`Fraction`).
        reason: Human-readable explanation.
    """

    def __init__(self, field_name, true_ms, reason):
        self.field_name = field_name
        self.true_ms = true_ms
        self.reason = reason
        super().__init__(
            "Time field {!r} with true time {} ms cannot be encoded losslessly: {}"
            .format(field_name, true_ms, reason))


class UndeliverableRange(LosslessDeliveryError):
    """A requested source sample range cannot fit into any legal segment.

    Attributes:
        item: The :class:`HandoverItem` whose range cannot be delivered.
        field_failures: Sequence of :class:`UnrepresentableTimeField`
            describing header fields that cannot be written, if any.
    """

    def __init__(self, item, field_failures=()):
        self.item = item
        self.field_failures = tuple(field_failures)
        message = (
            "Source {!r} physical trace {} samples [{}:{}) cannot be delivered "
            "losslessly under the permitted cuts".format(
                item.source_reel, item.physical_trace_index,
                item.start_sample, item.stop_sample))
        if field_failures:
            message += "; un-writable fields: " + "; ".join(
                "{} ({} ms: {})".format(f.field_name, f.true_ms, f.reason)
                for f in field_failures)
        super().__init__(message)


class HandoverItem:
    """One physical source trace and the continuous samples to preserve.

    Args:
        source_reel: Identifier of the source SEG-Y reel (used for provenance
            and as the manifest key).
        physical_trace_index: Zero-based physical trace index within the
            source reel.
        start_sample: Zero-based index of the first retained sample.
        stop_sample: Zero-based index one past the last retained sample.
        permitted_cut_after: Sorted iterable of one-based sample ordinals
            after which a cut is allowed.  A cut after point ``p`` separates
            the p-th sample (zero-based index ``p - 1``) from sample
            ``p + 1``.  The end of the range is always an implicit boundary.
        max_segment_samples: Maximum number of samples per output segment.
        label: Short display label (e.g. ``"A"``).
    """

    def __init__(self, source_reel, physical_trace_index, start_sample,
                 stop_sample, permitted_cut_after, max_segment_samples,
                 label=None):
        if stop_sample <= start_sample:
            raise ValueError("Handover range must contain at least one sample")
        if max_segment_samples < 1:
            raise ValueError("max_segment_samples must be positive")
        self.source_reel = source_reel
        self.physical_trace_index = int(physical_trace_index)
        self.start_sample = int(start_sample)
        self.stop_sample = int(stop_sample)
        self.label = label if label is not None else "{}-{}".format(
            source_reel, physical_trace_index)
        self.max_segment_samples = int(max_segment_samples)

        self.permitted_cut_after_points = tuple(sorted({
            int(point) for point in permitted_cut_after
        }))

        # Handover sheets number points from one; convert to zero-based
        # boundary indices usable directly against start/stop sample indices.
        boundaries = sorted({
            point - 1 for point in self.permitted_cut_after_points
            if self.start_sample <= point - 1 < self.stop_sample - 1
        })
        self.permitted_cut_after = tuple(boundaries)

    @property
    def num_samples(self):
        return self.stop_sample - self.start_sample

    def __repr__(self):
        return ("HandoverItem(label={!r}, source_reel={!r}, trace={}, "
                "range=[{}:{}), cuts_after={}, max_segment={})").format(
            self.label, self.source_reel, self.physical_trace_index,
            self.start_sample, self.stop_sample,
            self.permitted_cut_after, self.max_segment_samples)


class SourceTrace:
    """Everything the packer needs from one source trace.

    Args:
        item: The :class:`HandoverItem` requesting this trace.
        sample_interval_us: Sample interval in microseconds (integer).
        data_sample_format: A :class:`segpy.datatypes.DataSampleFormat` value.
        delay_recording_ms: True delay recording time in milliseconds; may be
            fractional, expressed exactly as a :class:`Fraction`.
        true_times: Mapping of the ten time-scalar field names to true
            millisecond :class:`Fraction` values.
        inline_number: Source inline number (copied verbatim).
        crossline_number: Source crossline number (copied verbatim).
        header_template: A Segpy ``TraceHeaderRev1`` supplying every other
            header field; only the time/sample/provenance fields are
            overridden in the output.
    """

    def __init__(self, item, sample_interval_us, data_sample_format,
                 delay_recording_ms, true_times, inline_number,
                 crossline_number, header_template):
        self.item = item
        self.sample_interval_us = int(sample_interval_us)
        self.data_sample_format = DataSampleFormat(int(data_sample_format))
        self.delay_recording_ms = Fraction(delay_recording_ms)
        self.true_times = dict(true_times)
        self.inline_number = int(inline_number)
        self.crossline_number = int(crossline_number)
        self.header_template = header_template

    def sample_time_ms(self, sample_index):
        """True time of an absolute source sample index, in milliseconds."""
        offset_us = (sample_index - self.item.start_sample) * self.sample_interval_us
        return self.delay_recording_ms + Fraction(offset_us, 1000)


class Segment:
    """One output trace: a contiguous slice of one source trace.

    Attributes:
        item: The owning handover item.
        start_sample: Absolute zero-based first source sample index.
        stop_sample: One past the last source sample index.
        segment_index: Zero-based ordinal of this segment within its item.
    """

    def __init__(self, item, start_sample, stop_sample, segment_index):
        self.item = item
        self.start_sample = int(start_sample)
        self.stop_sample = int(stop_sample)
        self.segment_index = int(segment_index)

    @property
    def num_samples(self):
        return self.stop_sample - self.start_sample

    @property
    def key(self):
        return (self.item.source_reel, self.item.physical_trace_index,
                self.start_sample, self.stop_sample)

    def __repr__(self):
        return "Segment({} [{}:{}] #{})".format(
            self.item.label, self.start_sample, self.stop_sample,
            self.segment_index)


class ReelPlan:
    """One output reel and the segments packed into it.

    All traces in a reel share sample count, sample interval, sample format
    and true start time; no physical source trace contributes more than one
    segment to the same reel.
    """

    def __init__(self, reel_number, segments, num_samples, sample_interval_us,
                 data_sample_format, true_start_ms):
        self.reel_number = int(reel_number)
        self.segments = tuple(segments)
        self.num_samples = int(num_samples)
        self.sample_interval_us = int(sample_interval_us)
        self.data_sample_format = data_sample_format
        self.true_start_ms = Fraction(true_start_ms)

    def __repr__(self):
        return "ReelPlan(number={}, traces={}, samples={}, start={}ms)".format(
            self.reel_number, len(self.segments), self.num_samples,
            self.true_start_ms)


class PackPlan:
    """The complete minimum-reel packing for a handover."""

    def __init__(self, reels, max_traces_per_reel, segments_by_item):
        self.reels = tuple(reels)
        self.max_traces_per_reel = int(max_traces_per_reel)
        self.segments_by_item = dict(segments_by_item)

    @property
    def num_reels(self):
        return len(self.reels)

    def all_segments(self):
        for reel in self.reels:
            for segment in reel.segments:
                yield segment
