"""Segpy :class:`Dataset` adapter for one planned reel and source repositories."""

from segpy.dataset import Dataset

from archive_pack.planner import _segment_true_times
from archive_pack.time_codes import encode_times


class SourceRepository:
    """Abstract source repository.

    A repository resolves a :class:`~archive_pack.model.HandoverItem` to its
    :class:`~archive_pack.model.SourceTrace` and returns source samples.
    Implementations may hold traces in memory or read source SEG-Y reels.
    """

    def source_for(self, item):
        raise NotImplementedError

    def samples_for(self, item, start_sample, stop_sample):
        raise NotImplementedError


class InMemorySourceRepository(SourceRepository):
    """Repository backed by an in-memory mapping."""

    def __init__(self):
        self._sources = {}
        self._samples = {}

    def add(self, source, samples):
        item = source.item
        if len(samples) != item.num_samples:
            raise ValueError(
                "Expected {} samples for item {!r}, got {}".format(
                    item.num_samples, item.label, len(samples)))
        self._sources[item] = source
        self._samples[item] = list(samples)
        return self

    def source_for(self, item):
        return self._sources[item]

    def samples_for(self, item, start_sample, stop_sample):
        offset = item.start_sample
        return self._samples[item][
            start_sample - offset:stop_sample - offset]


class ReelDataset(Dataset):
    """A Segpy dataset materialising one :class:`ReelPlan`."""

    def __init__(self, reel, repository, binary_reel_header,
                 textual_reel_header):
        self._reel = reel
        self._repository = repository
        self._binary_reel_header = binary_reel_header
        self._textual_reel_header = textual_reel_header

    @property
    def textual_reel_header(self):
        return self._textual_reel_header

    @property
    def binary_reel_header(self):
        return self._binary_reel_header

    @property
    def extended_textual_header(self):
        return []

    def num_traces(self):
        return len(self._reel.segments)

    def trace_indexes(self):
        return range(self.num_traces())

    def trace_header(self, trace_index):
        segment = self._reel.segments[trace_index]
        source = self._repository.source_for(segment.item)
        scalar_code, stored_times = encode_times(
            _segment_true_times(source, segment))

        header = source.header_template.copy(
            line_sequence_num=trace_index + 1,
            file_sequence_num=trace_index + 1,
            inline_number=source.inline_number,
            crossline_number=source.crossline_number,
            num_samples=segment.num_samples,
            sample_interval=source.sample_interval_us,
            time_scalar=scalar_code,
            **stored_times)
        return header

    def trace_samples(self, trace_index, start=None, stop=None):
        segment = self._reel.segments[trace_index]
        samples = self._repository.samples_for(
            segment.item, segment.start_sample, segment.stop_sample)
        if start is None:
            start = 0
        if stop is None:
            stop = segment.num_samples
        return samples[start:stop]

    @property
    def data_sample_format(self):
        from segpy.datatypes import DATA_SAMPLE_FORMAT_TO_SEG_Y_TYPE
        return DATA_SAMPLE_FORMAT_TO_SEG_Y_TYPE[
            self._reel.data_sample_format]
