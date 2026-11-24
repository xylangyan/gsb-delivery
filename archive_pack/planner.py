"""Legal segment enumeration and minimum-reel planning."""

from functools import lru_cache

from segpy.datatypes import DataSampleFormat

from archive_pack.model import ReelPlan, PackPlan, Segment, UndeliverableRange
from archive_pack.time_codes import UnrepresentableTimeField, encode_times


def enumerate_segmentations(item):
    """Enumerate every legal segmentation of a handover item.

    A segmentation is a tuple of half-open ``(start_sample, stop_sample)``
    ranges covering the item exactly.  Cuts may only occur at the item's
    permitted boundaries and every segment is at most
    ``item.max_segment_samples`` long.

    Results are ordered deterministically: fewest segments first, then by
    boundary positions.
    """

    @lru_cache(maxsize=None)
    def from_position(position):
        if position == item.stop_sample:
            return ((),)
        results = []
        boundaries = [cut for cut in item.permitted_cut_after if cut >= position]
        boundaries.append(item.stop_sample - 1)
        for last_sample in boundaries:
            length = last_sample - position + 1
            if length > item.max_segment_samples:
                continue
            for tail in from_position(last_sample + 1):
                results.append(((position, last_sample + 1),) + tail)
        return tuple(results)

    segmentations = from_position(item.start_sample)
    return tuple(sorted(segmentations, key=lambda seg: (len(seg), seg)))


def _segment_true_times(source, segment):
    """The ten true time values for one output segment."""
    values = dict(source.true_times)
    values['delay_recording_time'] = source.sample_time_ms(segment.start_sample)
    return values


def build_segments(source, segmentation):
    """Build segments and verify each is header-encodable without rounding."""
    segments = []
    failures = []
    for index, (start, stop) in enumerate(segmentation):
        segment = Segment(source.item, start, stop, index)
        try:
            encode_times(_segment_true_times(source, segment))
        except UnrepresentableTimeField as failure:
            failures.append(failure)
        else:
            segments.append(segment)
    return segments, failures


def feasible_segmentations(sources_by_item):
    """Return feasible segmentations per item or raise for an undeliverable item."""
    options = {}
    for item, source in sources_by_item.items():
        legal = enumerate_segmentations(item)
        if not legal:
            raise UndeliverableRange(item)
        feasible = []
        all_failures = []
        for segmentation in legal:
            segments, failures = build_segments(source, segmentation)
            if not failures:
                feasible.append((segmentation, segments))
            else:
                all_failures.extend(failures)
        if not feasible:
            distinct = {}
            for failure in all_failures:
                distinct[(failure.field_name, failure.true_ms)] = failure
            raise UndeliverableRange(item, field_failures=distinct.values())
        options[item] = feasible
    return options


def _compatibility_key(source, segment):
    """Traces sharing one of these keys can legally share a reel."""
    return (
        segment.num_samples,
        source.sample_interval_us,
        int(source.data_sample_format),
        source.sample_time_ms(segment.start_sample),
    )


class _Bin:
    """A working reel during search."""

    def __init__(self, key):
        self.key = key
        self.items = set()
        self.segments = []

    def can_take(self, item, key, capacity):
        return (self.key == key and item not in self.items
                and len(self.segments) < capacity)


def _snapshot(bin_):
    return bin_.key, frozenset(bin_.items), list(bin_.segments)


def _greedy_solution(item_order, options, sources_by_item, capacity):
    """A deterministic first-fit solution used as the initial incumbent."""
    bins = []
    for item in item_order:
        _, segments = options[item][0]
        source = sources_by_item[item]
        for segment in segments:
            key = _compatibility_key(source, segment)
            for bin_ in bins:
                if bin_.can_take(item, key, capacity):
                    bin_.items.add(item)
                    bin_.segments.append(segment)
                    break
            else:
                bin_ = _Bin(key)
                bin_.items.add(item)
                bin_.segments.append(segment)
                bins.append(bin_)
    return len(bins), [_snapshot(bin_) for bin_ in bins]


def plan_packing(sources, max_traces_per_reel):
    """Plan a minimum-reel packing for a complete handover.

    Args:
        sources: An iterable of :class:`SourceTrace` objects, one per
            handover item.
        max_traces_per_reel: Maximum number of traces per output reel.

    Returns:
        A :class:`PackPlan` using the minimum number of reels.

    Raises:
        UndeliverableRange: If an item has no lossless legal segmentation.
    """
    sources_by_item = {source.item: source for source in sources}
    item_order = sorted(
        sources_by_item,
        key=lambda item: (item.source_reel, item.physical_trace_index,
                          item.start_sample))
    item_position = {item: index for index, item in enumerate(item_order)}
    options = feasible_segmentations(sources_by_item)
    capacity = int(max_traces_per_reel)

    best_count, best_bins = _greedy_solution(
        item_order, options, sources_by_item, capacity)

    def place_next(bins, pending, item_index, placed_count, unplaced_count):
        nonlocal best_count, best_bins

        if len(bins) >= best_count and (pending or item_index < len(item_order)):
            return
        lower_bound = max(len(bins),
                          -(-(placed_count + unplaced_count) // capacity))
        if lower_bound >= best_count:
            return

        if not pending:
            if item_index == len(item_order):
                best_count = len(bins)
                best_bins = [_snapshot(bin_) for bin_ in bins]
                return
            item = item_order[item_index]
            min_length = min(len(segmentation)
                             for segmentation, _ in options[item])
            remaining_after = unplaced_count - min_length
            for _, segments in options[item]:
                place_next(list(bins), list(segments), item_index + 1,
                           placed_count, remaining_after)
            return

        segment = pending.pop()
        item = segment.item
        source = sources_by_item[item]
        key = _compatibility_key(source, segment)

        tried_signatures = set()
        for bin_ in bins:
            signature = (bin_.key, frozenset(bin_.items))
            if signature in tried_signatures:
                continue
            tried_signatures.add(signature)
            if bin_.can_take(item, key, capacity):
                bin_.items.add(item)
                bin_.segments.append(segment)
                place_next(bins, pending, item_index,
                           placed_count + 1, unplaced_count - 1)
                bin_.segments.pop()
                bin_.items.discard(item)

        new_bin = _Bin(key)
        new_bin.items.add(item)
        new_bin.segments.append(segment)
        place_next(bins + [new_bin], pending, item_index,
                   placed_count + 1, unplaced_count - 1)
        pending.append(segment)

    minimum_segments = sum(
        min(len(segmentation) for segmentation, _ in options[item])
        for item in item_order)
    place_next([], [], 0, 0, minimum_segments)

    return _build_plan(best_bins, item_position, capacity)


def _build_plan(best_bins, item_position, capacity):
    reels = []
    segments_by_item = {}
    for reel_number, snapshot in enumerate(best_bins, start=1):
        key, _, segments = snapshot
        num_samples, interval_us, format_code, true_start_ms = key
        ordered = sorted(
            segments,
            key=lambda segment: (item_position[segment.item],
                                 segment.segment_index))
        reel = ReelPlan(
            reel_number=reel_number,
            segments=ordered,
            num_samples=num_samples,
            sample_interval_us=interval_us,
            data_sample_format=DataSampleFormat(format_code),
            true_start_ms=true_start_ms)
        reels.append(reel)
        for segment in ordered:
            segments_by_item.setdefault(segment.item, []).append(segment)

    for item in segments_by_item:
        segments_by_item[item] = tuple(sorted(
            segments_by_item[item], key=lambda segment: segment.start_sample))

    return PackPlan(reels, capacity, segments_by_item)
