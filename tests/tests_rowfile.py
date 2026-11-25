# coding: utf-8

# Copyright 2014-2025 Álvaro Justen <https://github.com/turicas/rows/>
#    This program is free software: you can redistribute it and/or modify it under the terms of the GNU Lesser General
#    Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option)
#    any later version.
#    This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
#    warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for
#    more details.
#    You should have received a copy of the GNU Lesser General Public License along with this program.  If not, see
#    <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import random

import pytest

import rows.rowfile as rowfile


class CountingSource(object):
    """Forward-only iterable that records how many items were pulled, to prove single-pass reads."""

    def __init__(self, values):
        self._iterator = iter(values)
        self.pulled = 0

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self._iterator)
        self.pulled += 1
        return value

    next = __next__  # Python 2


def plain_replay(length, orders):
    """Literal plain-list execution: atomic per order, void orders rolled back."""
    container = [rowfile.Origin(index) for index in range(length)]
    statuses = []
    for order in orders:
        snapshot = list(container)
        try:
            rowfile._plain_execute(container, order)
        except rowfile.OrderVoid:
            container = snapshot
            statuses.append(rowfile.VOID)
        else:
            statuses.append(rowfile.APPLIED)
    return container, statuses


def outcome_of(report):
    outcome = report.outcome
    if isinstance(outcome, rowfile.MissingBody):
        return rowfile.Origin(outcome.index)
    return outcome


# ---------------------------------------------------------------------------
# Forward-only ingestion shared by the old and new views
# ---------------------------------------------------------------------------


def test_source_is_read_forward_exactly_once():
    source = CountingSource(["r{0}".format(index) for index in range(10)])
    rowfile_ = rowfile.RowFile(source, total_bounds=(10, 10))
    assert [row for row in rowfile_.old_view()] == ["r{0}".format(index) for index in range(10)]
    assert source.pulled == 10
    # A second old view must reuse the buffer instead of reopening/pulling the source.
    assert [row for row in rowfile_.old_view()] == ["r{0}".format(index) for index in range(10)]
    assert source.pulled == 10


def test_old_reader_may_stop_before_the_end():
    source = CountingSource(["r{0}".format(index) for index in range(50)])
    rowfile_ = rowfile.RowFile(source, total_bounds=(20, 100))
    early = []
    for row in rowfile_.old_view():
        early.append(row)
        if len(early) == 5:
            break
    assert early == ["r{0}".format(index) for index in range(5)]
    assert rowfile_.total_length is None  # not read to the end
    assert rowfile_.rows_read == 5
    assert source.pulled == 5


def test_new_view_reads_only_a_small_window_and_fills_forward():
    source = CountingSource(["r{0}".format(index) for index in range(10**6)])
    rowfile_ = rowfile.RowFile(source, total_bounds=(10**6, 10**8))
    report = rowfile_.window(0, 3, read=True)
    assert [outcome_of(entry).index for entry in report] == [0, 1, 2]
    assert source.pulled == 3  # did not wait for the tail


# ---------------------------------------------------------------------------
# Atomic slice edits (plain-list semantics)
# ---------------------------------------------------------------------------


def test_reverse_every_other_delete():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(10)], total_bounds=(10, 10))
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    report = rowfile_.window(0, 5, read=True)
    assert [outcome_of(entry).index for entry in report] == [0, 2, 4, 6, 8]
    beyond = rowfile_.window(5, 6)
    assert isinstance(beyond.positions[0].outcome, rowfile.OutOfRange)


def test_negative_step_delete_with_bounded_stop_is_tail_only():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(10)], total_bounds=(10, 10))
    rowfile_.apply(rowfile.Order().delete(-1, -4, -2))  # deletes indices 9 and 7 only
    report = rowfile_.window(0, 8, read=True)
    assert [outcome_of(entry).index for entry in report] == [0, 1, 2, 3, 4, 5, 6, 8]


def test_final_length_known_when_bounds_are_exact():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(12)], total_bounds=(12, 12))
    assert rowfile_.final_length == 12
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    assert rowfile_.final_length == 6
    rowfile_.apply(rowfile.Order().at(3, ["segA", "segB"]))
    assert rowfile_.final_length == 8
    assert rowfile_.window(0, 6, read=True).positions[3].outcome.value == "segA"


def test_step_one_replacement_changes_middle_length():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(10)], total_bounds=(10, 10))
    rowfile_.apply(rowfile.Order().assign(3, 5, ["fix-a", "fix-b", "fix-c"]))
    outcome = [entry.outcome for entry in rowfile_.window(0, 11, read=True)]
    assert [item.index for item in outcome[:3] if isinstance(item, rowfile.Origin)] == [0, 1, 2]
    assert isinstance(outcome[3], rowfile.Inserted)
    assert outcome[3].value == "fix-a"
    # Original rows 5..9 shift down by one (new length 11).
    assert [item.index for item in outcome[6:11] if isinstance(item, rowfile.Origin)] == [5, 6, 7, 8, 9]


def test_void_order_is_rolled_back_as_a_whole():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(10)], total_bounds=(10, 10))
    # The delete would leave length 8, where [0:6:2] targets 3 slots; supplying 2 items voids the whole order.
    order = rowfile.Order().delete(0, 2).assign(0, 6, ["a", "b"], 2)
    assert rowfile_.apply(order) == rowfile.VOID
    report = rowfile_.window(0, 10, read=True)
    assert [outcome_of(entry).index for entry in report] == list(range(10))


def test_extended_slice_length_mismatch_voids_the_order():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(10)], total_bounds=(10, 10))
    # assigns 2 items to a 3-element extended slice: must void the whole (otherwise valid) order.
    order = rowfile.Order().assign(0, 6, ["a", "b"], 2)
    assert rowfile_.apply(order) == rowfile.VOID
    assert [outcome_of(entry).index for entry in rowfile_.window(0, 10, read=True)] == list(range(10))


# ------------------------------------------------------------------------------------------------------------------------------------------
# Bounded / unknown total length
# ------------------------------------------------------------------------------------------------------------------------------------------


def test_bounded_total_is_reported_as_interval():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(4)], total_bounds=(20000000, 100000000))
    assert rowfile_.total_bounds == (20000000, 100000000)
    assert rowfile_.total_length is None


def test_undecided_when_order_validity_depends_on_unknown_length():
    # [:-1:2] replaces 5 slots for lengths 10..11 but 6 slots for 12..13, so a 5-item assignment is undecided.
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(20)], total_bounds=(10, 13))
    order = rowfile.Order().assign(None, -1, ["x"] * 5, 2)
    assert rowfile_.apply(order) == rowfile.UNDECIDED
    status = rowfile_.finish()[0]
    container, replayed = plain_replay(20, [order])
    assert status == replayed[0]


@pytest.mark.parametrize("real_length", [0, 1, 2, 5, 20])
def test_finish_resolves_every_order_at_the_exact_length(real_length):
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(real_length)], total_bounds=(0, real_length + 5))
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    rowfile_.apply(rowfile.Order().assign(0, 0, ["front"]))
    statuses = rowfile_.finish()
    container, replayed = plain_replay(real_length, rowfile_._orders)
    assert statuses == replayed
    assert rowfile_.total_length == real_length
    assert rowfile_.final_length == len(container)


# ------------------------------------------------------------------------------------------------------------------------------------------
# Window selection, pending, and delivery without the tail
# ------------------------------------------------------------------------------------------------------------------------------------------


def test_window_inside_correction_scope_is_delivered_without_the_tail():
    # Deleting every other row from the end only reaches index 1, so position 0 is certified up front.
    source = CountingSource("r{0}".format(index) for index in range(10**8))
    rowfile_ = rowfile.RowFile(source, total_bounds=(10**8, 10**8))
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    assert rowfile_.safe_prefix_stop() == 1
    report = rowfile_.window(0, 1, read=True)
    assert outcome_of(report.positions[0]).index == 0
    assert source.pulled == 1


def test_safe_window_rows_are_identity_whether_order_applies_or_voids():
    # A bounded tail edit can only reach the last few rows, so the head is identical in both outcomes.
    for order, expected_status in (
        (rowfile.Order().delete(-1, -4, -2), rowfile.APPLIED),
        (rowfile.Order().delete(-1, -4, -2).assign(0, 4, ["only-one"], 2), rowfile.VOID),
    ):
        rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(100)], total_bounds=(100, 100))
        assert rowfile_.apply(order) == expected_status
        assert rowfile_.safe_prefix_stop() > 90
        report = rowfile_.window(0, 90, read=True)
        assert [outcome_of(entry).index for entry in report] == list(range(90))


def test_pending_gives_two_witness_lengths_with_different_results():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(20)], total_bounds=(0, 10**8))
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    pending = rowfile_.window(1, 2).positions[0].outcome
    assert isinstance(pending, rowfile.Pending)
    assert len(pending.alternatives) == 2
    (length_a, result_a), (length_b, result_b) = pending.alternatives
    assert length_a != length_b
    assert result_a != result_b


def test_missing_body_when_origin_recognised_but_unread():
    rowfile_ = rowfile.RowFile(["r{0}".format(index) for index in range(50)], total_bounds=(50, 50))
    report = rowfile_.window(0, 1, read=False)
    outcome = report.positions[0].outcome
    assert isinstance(outcome, rowfile.MissingBody)
    assert outcome.index == 0
    # Reading the body later shares the already-read rows and does not reopen the source.
    assert rowfile_.row(0) == "r0"


def test_same_value_from_two_original_rows_is_not_merged():
    rowfile_ = rowfile.RowFile(["dup", "dup", "other"], total_bounds=(3, 3))
    report = rowfile_.window(0, 3, read=True)
    assert [outcome_of(entry).index for entry in report] == [0, 1, 2]


def test_equal_values_with_different_originals_stay_pending():
    # Rows 0 and 1 have the same value; [del -1::-2] makes position 0 map to row 0 for an even total and row 1 for an
    # odd total, so within the bound the position stays pending even though both candidate rows read identically.
    rowfile_ = rowfile.RowFile(["dup"] * 20, total_bounds=(10, 11))
    rowfile_.apply(rowfile.Order().delete(-1, None, -2))
    pending = rowfile_.window(0, 1).positions[0].outcome
    assert isinstance(pending, rowfile.Pending)
    results = [result for _, result in pending.alternatives]
    assert [result.index for result in results] == [0, 1]


def test_equal_inserted_literals_keep_distinct_tokens():
    rowfile_ = rowfile.RowFile([], total_bounds=(0, 0))
    rowfile_.apply(rowfile.Order().at(0, ["same", "same"]))
    first, second = [entry.outcome for entry in rowfile_.window(0, 2)]
    assert first.value == second.value == "same"
    assert first.token != second.token


# ------------------------------------------------------------------------------------------------------------------------------------------
# Replay verification against the same original input
# ------------------------------------------------------------------------------------------------------------------------------------------


def _random_order(rng, length):
    order = rowfile.Order()
    for _ in range(rng.randint(0, 3)):
        step = rng.choice([None, 1, 2, 3, -1, -2, -3, 5, -5])
        start = rng.choice([None, 0, rng.randint(-length - 3, length + 3)])
        stop = rng.choice([None, 0, rng.randint(-length - 3, length + 3)])
        if rng.random() < 0.4:
            order.delete(start, stop, step)
        else:
            norm_start, norm_stop, norm_step = rowfile._normalized(start, stop, step, length)
            target = rowfile._count(norm_start, norm_stop, norm_step)
            if norm_step == 1:
                count = rng.randint(0, 4)
            else:
                count = target if rng.random() < 0.85 else max(0, target + rng.choice([-1, 1]))
            order.assign(start, stop, [("lit", rng.randint(0, 5)) for _ in range(count)], norm_step)
    return order


def test_window_outcomes_match_plain_list_replay_for_random_orders():
    rng = random.Random(7)
    for _ in range(200):
        length = rng.randint(0, 10)
        orders = [_random_order(rng, length) for _ in range(rng.randint(0, 3))]
        rowfile_ = rowfile.RowFile([("orig", index) for index in range(length)], total_bounds=(length, length))
        for order in orders:
            rowfile_.apply(order)
        container, _ = plain_replay(length, orders)
        for report in rowfile_.window(0, max(len(container), length) + 4, read=False):
            outcome = report.outcome
            assert not isinstance(outcome, rowfile.Pending)
            if report.position >= len(container):
                assert isinstance(outcome, rowfile.OutOfRange)
            else:
                expected = container[report.position]
                if isinstance(expected, rowfile.Inserted):
                    assert isinstance(outcome, rowfile.Inserted)
                    assert outcome.value == expected.value
                    assert outcome.token == expected.token
                else:
                    assert outcome_of(report).index == expected.index


def test_verify_replays_every_order_success_or_void():
    rng = random.Random(9)
    for _ in range(100):
        length = rng.randint(0, 12)
        orders = [_random_order(rng, length) for _ in range(rng.randint(0, 3))]
        rowfile_ = rowfile.RowFile([("orig", index) for index in range(length)], total_bounds=(length, length))
        for order in orders:
            rowfile_.apply(order)
        ok, details = rowfile_.verify()
        assert ok, details
