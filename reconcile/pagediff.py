"""
Local reconciliation of two page snapshots.

diff_intervals walks the two pages' parts in lockstep and merges every
position where the two transcripts differ — in value or in provenance —
into contiguous intervals. Atomic segment pairs are compared
arithmetically, so checking a large archive never materializes one record
per expanded item-number; records of either side are read out on demand
with read_records.
"""
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Tuple

from .page import Origin, Page
from .replay import OpRecord, Transcript
from .segments import value_diffs


@dataclass(frozen=True)
class DiffInterval:
    """A contiguous half-open interval [start, end) of page positions where
    the two transcripts differ in value or in provenance."""

    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start


def diff_intervals(
    page_a: Page,
    page_b: Page,
    start: int = 0,
    end: Optional[int] = None,
) -> Iterator[DiffInterval]:
    """
    Yield merged DiffIntervals over page positions [start, end). `end`
    defaults to the longer page; positions only one page has always differ.
    """
    if end is None:
        end = max(len(page_a), len(page_b))
    start = max(start, 0)
    common_end = min(len(page_a), len(page_b), end)

    open_start: Optional[int] = None
    open_end: Optional[int] = None

    def emit(s: int, e: int) -> Iterator[DiffInterval]:
        nonlocal open_start, open_end
        if s >= e:
            return
        if open_end is not None and s <= open_end:
            open_end = max(open_end, e)
            return
        if open_start is not None:
            yield DiffInterval(open_start, open_end)
        open_start, open_end = s, e

    def flush() -> Iterator[DiffInterval]:
        nonlocal open_start, open_end
        if open_start is not None:
            yield DiffInterval(open_start, open_end)
            open_start = open_end = None

    parts_a, parts_b = page_a.parts, page_b.parts
    cum_a, cum_b = page_a._cum, page_b._cum
    from bisect import bisect_right

    slot_a = max(bisect_right(cum_a, start) - 1, 0) if parts_a else 0
    slot_b = max(bisect_right(cum_b, start) - 1, 0) if parts_b else 0

    lo = start
    while lo < common_end:
        part_a = parts_a[slot_a]
        part_b = parts_b[slot_b]
        hi = min(common_end, cum_a[slot_a + 1], cum_b[slot_b + 1])
        rel_a = lo - cum_a[slot_a]
        rel_b = lo - cum_b[slot_b]
        span = hi - lo
        origins_equal = part_a.op == part_b.op and (
            part_a.base + rel_a == part_b.base + rel_b
        )
        if not origins_equal:
            yield from emit(lo, hi)
        else:
            seg_a = part_a.segment.sub(rel_a, rel_a + span)
            seg_b = part_b.segment.sub(rel_b, rel_b + span)
            for s, e in value_diffs(seg_a, seg_b):
                yield from emit(lo + s, lo + e)
        lo = hi
        if lo >= cum_a[slot_a + 1]:
            slot_a += 1
        if lo >= cum_b[slot_b + 1]:
            slot_b += 1

    tail_start = max(start, common_end)
    tail_end = min(end, max(len(page_a), len(page_b)))
    if tail_start < tail_end:
        yield from emit(tail_start, tail_end)
    yield from flush()


def read_records(page: Page, interval: DiffInterval) -> List[Tuple[int, Any, Origin]]:
    """Read out one side's records for a diff interval, on demand."""
    return page.read(interval.start, interval.end)


def _receipt_equal(a: Any, b: Any) -> bool:
    return a == b


def record_equal(ra: OpRecord, rb: OpRecord) -> bool:
    if ra.error is not None or rb.error is not None:
        return (
            ra.error is not None
            and rb.error is not None
            and type(ra.error) is type(rb.error)
            and ra.error.args == rb.error.args
        )
    return _receipt_equal(ra.receipt, rb.receipt)


@dataclass
class RoundReport:
    """The differences one round's two transcripts would cause."""

    record_diffs: List[Tuple[int, OpRecord, OpRecord]]
    page_diffs: List[DiffInterval]
    transcript_a: Transcript
    transcript_b: Transcript

    def read_a(self, interval: DiffInterval):
        return read_records(self.transcript_a.page, interval)

    def read_b(self, interval: DiffInterval):
        return read_records(self.transcript_b.page, interval)


def compare_transcripts(
    ta: Transcript,
    tb: Transcript,
    start: int = 0,
    end: Optional[int] = None,
) -> RoundReport:
    """Circle out where the two transcripts disagree: per-op receipts or
    exceptions ("keys"), and merged page-position intervals."""
    record_diffs = [
        (i, ra, rb)
        for i, (ra, rb) in enumerate(zip(ta.records, tb.records))
        if not record_equal(ra, rb)
    ]
    longer = ta.records if len(ta.records) >= len(tb.records) else tb.records
    for i in range(min(len(ta.records), len(tb.records)), len(longer)):
        record_diffs.append((i, longer[i], None))
    page_diffs = list(diff_intervals(ta.page, tb.page, start, end))
    return RoundReport(record_diffs, page_diffs, ta, tb)
