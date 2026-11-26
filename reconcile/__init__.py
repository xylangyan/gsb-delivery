"""
Differential reconciliation for the two transcripts of the old item-number
pages.

One transcript records, per batch, whether the goods were handed over as a
reusable list or as a one-shot stream; the other transcript flipped that
form for a few batches. Item-numbers and their order of appearance are
identical. This package replays a round of OrderedSet operations under
each transcript (reconcile.replay), keeps the resulting pages compressed
as segment parts with provenance (reconcile.page, reconcile.segments), and
circles out the differences the two transcripts would cause — per-op receipts or
exceptions, and merged page-position intervals (reconcile.pagediff).
"""
from .pagediff import (
    DiffInterval,
    RoundReport,
    compare_transcripts,
    diff_intervals,
    read_records,
)
from .page import INITIAL_OP, Origin, Page, PagePart
from .replay import (
    LIST,
    STREAM,
    Add,
    Clear,
    DifferenceUpdate,
    Discard,
    GetItem,
    IndexQuery,
    IntersectionUpdate,
    OpRecord,
    Pop,
    Receipt,
    Remove,
    SymmetricDifferenceUpdate,
    Transcript,
    Update,
    replay,
)
from .segments import Chain, Materialized, RangeSeq, Repeat, Segment, value_diffs

__all__ = [
    "Add",
    "Chain",
    "Clear",
    "DiffInterval",
    "DifferenceUpdate",
    "Discard",
    "GetItem",
    "INITIAL_OP",
    "IndexQuery",
    "IntersectionUpdate",
    "LIST",
    "Materialized",
    "OpRecord",
    "Origin",
    "Page",
    "PagePart",
    "Pop",
    "RangeSeq",
    "Receipt",
    "Remove",
    "Repeat",
    "RoundReport",
    "STREAM",
    "Segment",
    "SymmetricDifferenceUpdate",
    "Transcript",
    "Update",
    "compare_transcripts",
    "diff_intervals",
    "read_records",
    "replay",
    "value_diffs",
]
