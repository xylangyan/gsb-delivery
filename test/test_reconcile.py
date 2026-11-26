"""
Tests for the differential reconciliation of the two transcripts.

The running example is the one from the ledger: the page starts with only
'g'; a batch recorded as list in one transcript and as one-shot stream in
the other is applied with symmetric_difference_update; receipts of update
are then used directly as the index of a following pop and position read.
"""
import pytest

from ordered_set import OrderedSet

from reconcile import (
    LIST,
    STREAM,
    Add,
    Chain,
    DiffInterval,
    GetItem,
    IndexQuery,
    Materialized,
    Origin,
    Page,
    PagePart,
    Pop,
    RangeSeq,
    Receipt,
    Repeat,
    SymmetricDifferenceUpdate,
    Update,
    compare_transcripts,
    diff_intervals,
    read_records,
    replay,
    value_diffs,
)


def make_round():
    batches = {
        "b1": Materialized(("g", "x", "x")),
        "b2": Materialized(("x", "g")),
    }
    program = [
        SymmetricDifferenceUpdate("b1"),  # op 0
        Pop(1),                           # op 1
        Update("b2"),                     # op 2: its receipt is reused below
        Pop(Receipt(2)),                  # op 3
        IndexQuery("x"),                  # op 4
        GetItem(Receipt(2)),              # op 5
    ]
    forms_a = {"b1": LIST, "b2": LIST}
    forms_b = {"b1": STREAM, "b2": LIST}  # b1's form was recorded flipped
    ta = replay(program, batches, forms_a, initial=["g"])
    tb = replay(program, batches, forms_b, initial=["g"])
    return ta, tb


def test_stream_form_leaves_duplicates_on_the_page():
    _, tb = make_round()
    # one-shot g, x, x: the remove-set is computed after the stream is
    # exhausted, so 'g' survives and both x's land on the page
    mid = replay(
        [SymmetricDifferenceUpdate("b1")],
        {"b1": Materialized(("g", "x", "x"))},
        {"b1": STREAM},
        initial=["g"],
    )
    assert mid.items == ["g", "x", "x"]


def test_list_form_removes_g():
    mid = replay(
        [SymmetricDifferenceUpdate("b1")],
        {"b1": Materialized(("g", "x", "x"))},
        {"b1": LIST},
        initial=["g"],
    )
    assert mid.items == ["x", "x"]


def test_update_receipt_can_be_zero():
    _, tb = make_round()
    # stream transcript: update('x', 'g') ends by re-adding 'g', receipt 0
    assert tb.records[2].receipt == 0
    # list transcript: the same update ends by appending 'g', receipt 2
    ta, _ = make_round()
    assert ta.records[2].receipt == 2


def test_pop_with_receipt_index_and_stale_registration():
    ta, tb = make_round()
    # both popped 'g', but from different positions (receipt 2 vs 0)
    assert ta.records[3].receipt == tb.records[3].receipt == "g"
    # stream page keeps 'x' on the page while its registration points past
    # the end: index('x') is returned as-is, even out of bounds
    assert tb.items == ["x", "x"]
    assert tb.records[4].receipt == 2
    assert len(tb.items) == 2  # index 2 is out of bounds
    # list page: index('x') points at the *other* x's delivery
    assert ta.records[4].receipt == 1


def test_getitem_receipt_diverges_into_error():
    ta, tb = make_round()
    # list transcript: receipt 2 is out of range, the original IndexError
    # is recorded and the round continues
    assert isinstance(ta.records[5].error, IndexError)
    # stream transcript: receipt 0 reads position 0
    assert tb.records[5].error is None
    assert tb.records[5].receipt == "x"


def test_compare_transcripts_circles_key_and_position_diffs():
    ta, tb = make_round()
    report = compare_transcripts(ta, tb)
    # ops 2 (receipt 2 vs 0), 4 (index 1 vs 2) and 5 (IndexError vs 'x')
    assert [i for i, _, _ in report.record_diffs] == [2, 4, 5]
    # pages are both ['x', 'x'] but position 0 traces to different
    # deliveries: (op 0, pos 1) vs (op 0, pos 2)
    assert report.page_diffs == [DiffInterval(0, 1)]
    assert report.read_a(report.page_diffs[0]) == [(0, "x", Origin(0, 1))]
    assert report.read_b(report.page_diffs[0]) == [(0, "x", Origin(0, 2))]


def test_equal_items_trace_to_their_own_delivery():
    ta, tb = make_round()
    # position 1 came from the same delivery in both transcripts
    assert ta.page.origin_at(1) == tb.page.origin_at(1) == Origin(2, 0)
    # position 0 did not
    assert ta.page.origin_at(0) != tb.page.origin_at(0)


def test_stream_exhaustion_when_ops_reference_one_batch_repeatedly():
    batches = {"b": Materialized(("a", "b", "c"))}
    program = [Update("b"), Update("b")]
    ta = replay(program, batches, {"b": LIST})
    tb = replay(program, batches, {"b": STREAM})
    # list form: the second update re-iterates the whole batch
    assert ta.receipts == [2, 2]
    assert ta.items == ["a", "b", "c"]
    # stream form: the second update finds the stream exhausted, receipt 0
    assert tb.receipts == [2, 0]
    assert tb.items == ["a", "b", "c"]


def test_error_is_caught_and_earlier_changes_stand():
    program = [
        Update("b"),      # op 0
        Pop(9),           # op 1: IndexError
        Add("z"),         # op 2: still runs
        Pop(Receipt(1)),  # op 3: the receipt is an error -> RuntimeError
        Add("w"),         # op 4: still runs
    ]
    t = replay(program, {"b": Materialized(("a", "b"))}, {"b": LIST})
    assert isinstance(t.records[1].error, IndexError)
    assert isinstance(t.records[3].error, RuntimeError)
    assert t.items == ["a", "b", "z", "w"]


class FaultySegment(Materialized):
    """A segment whose stream raises after two items."""

    def __iter__(self):
        yield self.values[0]
        yield self.values[1]
        raise RuntimeError("stream broke mid-batch")


def test_partial_mutation_before_an_error_counts():
    t = replay(
        [Update("b")],
        {"b": FaultySegment(("a", "b", "c"))},
        {"b": STREAM},
    )
    assert isinstance(t.records[0].error, RuntimeError)
    # the two items delivered before the error stay on the page
    assert t.items == ["a", "b"]


def test_fancy_indexing_duplicates_and_negatives():
    t = replay(
        [GetItem([1, -1, 1, 0])],
        {},
        {},
        initial=["a", "b", "c"],
    )
    assert t.records[0].receipt == ["b", "c", "b", "a"]


def test_stepped_slice_uses_ordered_sets_own_slice_result():
    t = replay(
        [GetItem(slice(0, 5, 2))],
        {},
        {},
        initial=["a", "b", "c", "d", "e"],
    )
    receipt = t.records[0].receipt
    assert isinstance(receipt, OrderedSet)
    assert receipt == OrderedSet(["a", "c", "e"])


def test_index_query_records_original_exception():
    t = replay([IndexQuery("missing")], {}, {}, initial=["a"])
    assert isinstance(t.records[0].error, KeyError)
    t2 = replay([IndexQuery(["a", "missing"])], {}, {}, initial=["a"])
    assert isinstance(t2.records[0].error, KeyError)
    t3 = replay([IndexQuery(["a", "a"])], {}, {}, initial=["a"])
    assert t3.records[0].receipt == [0, 0]


def test_segment_basics():
    seg = Chain([RangeSeq(0, 5), Repeat("z", 3), RangeSeq(10, 0, -2)])
    assert len(seg) == 13
    assert list(seg) == [0, 1, 2, 3, 4, "z", "z", "z", 10, 8, 6, 4, 2]
    assert seg[0] == 0 and seg[12] == 2 and seg[-1] == 2
    assert list(seg.sub(3, 10)) == [3, 4, "z", "z", "z", 10, 8]
    # a stream is one-shot
    stream = seg.stream()
    assert next(stream) == 0
    assert len(list(stream)) == 12


def test_atomic_value_diffs():
    assert list(value_diffs(Repeat("a", 5), Repeat("a", 5))) == []
    assert list(value_diffs(Repeat("a", 5), Repeat("b", 5))) == [(0, 5)]
    # equal at exactly one position
    assert list(value_diffs(Repeat(3, 6), RangeSeq(0, 6))) == [(0, 3), (4, 6)]
    # two arithmetic progressions meet at most once
    assert list(value_diffs(RangeSeq(0, 10), RangeSeq(0, 20, 2))) == [(1, 10)]
    assert list(value_diffs(RangeSeq(0, 10), RangeSeq(0, 10))) == []
    # reversed segment vs forward
    assert list(value_diffs(RangeSeq(9, -1, -1), RangeSeq(0, 10))) == [(0, 10)]


def make_large_pages():
    """Two archive-scale pages built from a handful of segments."""
    seg_numbers = RangeSeq(0, 50_000_000)
    seg_fixed_a = Repeat("FIXED-77", 100_000_000)
    seg_fixed_b = Repeat("FIXED-78", 100_000_000)
    seg_reversed = RangeSeq(80_000_000, 30_000_000, -3)
    page_a = Page(
        [
            PagePart(seg_numbers, op=0, base=0),
            PagePart(seg_fixed_a, op=1, base=0),
            PagePart(seg_reversed, op=2, base=0),
        ]
    )
    page_b = Page(
        [
            PagePart(seg_numbers, op=0, base=0),
            PagePart(seg_fixed_b, op=1, base=0),
            PagePart(seg_reversed, op=5, base=0),  # same values, other delivery
        ]
    )
    return page_a, page_b


def test_large_archive_diff_stays_segmented():
    page_a, page_b = make_large_pages()
    assert len(page_a) == len(page_b) == 50_000_000 + 100_000_000 + 16_666_667
    diffs = list(diff_intervals(page_a, page_b))
    # identical consecutive numbering collapses to nothing; the fixed-value
    # run differs in value and the reversed run in provenance, and the two
    # contiguous regions merge into a single interval
    assert diffs == [DiffInterval(50_000_000, 166_666_667)]


def test_large_archive_local_check_and_on_demand_reads():
    page_a, page_b = make_large_pages()
    # local reconciliation of one contiguous interval only
    diffs = list(diff_intervals(page_a, page_b, 49_999_999, 50_000_002))
    assert diffs == [DiffInterval(50_000_000, 50_000_002)]
    # records are read out on demand, only for the asked interval
    assert read_records(page_a, DiffInterval(50_000_000, 50_000_002)) == [
        (50_000_000, "FIXED-77", Origin(1, 0)),
        (50_000_001, "FIXED-77", Origin(1, 1)),
    ]
    assert read_records(page_b, DiffInterval(50_000_000, 50_000_002)) == [
        (50_000_000, "FIXED-78", Origin(1, 0)),
        (50_000_001, "FIXED-78", Origin(1, 1)),
    ]


def test_diff_handles_length_mismatch_and_offset_parts():
    page_a = Page([PagePart(RangeSeq(0, 4), op=0, base=0)])
    page_b = Page(
        [
            PagePart(Materialized((0,)), op=0, base=0),
            PagePart(RangeSeq(1, 5), op=0, base=1),
        ]
    )
    # values and origins agree on [0, 4) even though the part boundaries are
    # offset differently; page_b is longer, so only the tail differs
    assert list(diff_intervals(page_a, page_b)) == [DiffInterval(4, 5)]
    page_c = Page([PagePart(RangeSeq(1, 5), op=0, base=0)])
    assert list(diff_intervals(page_a, page_c)) == [DiffInterval(0, 4)]


def test_replay_compresses_page_into_segment_parts():
    batches = {"big": RangeSeq(0, 1000)}
    t = replay([Update("big")], batches, {"big": LIST})
    assert len(t.page.parts) == 1
    part = t.page.parts[0]
    assert part.op == 0 and part.base == 0
    assert isinstance(part.segment, RangeSeq)
    assert len(t.page) == 1000
    assert t.page.value_at(999) == 999
    assert t.page.origin_at(999) == Origin(0, 999)


def test_round_report_reads_both_sides():
    ta, tb = make_round()
    report = compare_transcripts(ta, tb)
    assert report.read_a(DiffInterval(0, 2))[1] == (1, "x", Origin(2, 0))
    assert report.read_b(DiffInterval(0, 2))[1] == (1, "x", Origin(2, 0))
