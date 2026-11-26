"""Tests the ordinal replay of :meth:`pyformlang.fst.FST.translate`."""
import pytest

from pyformlang.fst import FST
from pyformlang.fst.replay import (
    RangeStats, Receipt, TranslationReplay, VersionedReplay,
    assert_matches_translate, compare_replays, replay)


def _simple():
    fst = FST()
    fst.add_start_state("q0")
    fst.add_transition("q0", "a", "q1", ["b"])
    fst.add_final_state("q1")
    return fst


def _epsilon_loop():
    fst = FST()
    fst.add_start_state("q0")
    fst.add_transition("q0", "a", "q1", ["b"])
    fst.add_transition("q1", "epsilon", "q1", ["c"])
    fst.add_final_state("q1")
    return fst


def _overshoot():
    fst = FST()
    fst.add_start_state("s")
    fst.add_transition("s", "epsilon", "a", ["x", "y"])
    fst.add_transition("a", "e", "f", ["z"])
    fst.add_final_state("f")
    return fst


class TestReplayMatchesTranslate:
    """The replay must reproduce the original enumeration exactly."""

    def test_simple(self):
        fst = _simple()
        r = replay(fst, ["a"], 10)
        assert [receipt.output for receipt in r] == [["b"]]
        assert r.receipt(1).output == ["b"]
        assert r.receipt(1).final_state == "q1"

    def test_epsilon_loop(self):
        fst = _epsilon_loop()
        for limit in (1, 2, 3, 10, 25):
            assert replay(fst, ["a"], limit).matches_translate()
            assert list(replay(fst, ["a"], limit).translations()) == \
                list(fst.translate(["a"], max_length=limit))

    def test_paper_order(self):
        fst = FST()
        fst.add_transitions(
            [(0, "I", 1, ["Je"]), (1, "am", 2, ["suis"]),
             (2, "alone", 3, ["tout", "seul"]),
             (2, "alone", 3, ["seul"])])
        fst.add_start_state(0)
        fst.add_final_state(3)
        expected = list(fst.translate(["I", "am", "alone"]))
        assert [x.output for x in replay(fst, ["I", "am", "alone"])] == \
            expected

    def test_ordinals_are_one_based_and_dense(self):
        fst = _epsilon_loop()
        receipts = replay(fst, ["a"], 5).receipts()
        assert [x.ordinal for x in receipts] == \
            list(range(1, len(receipts) + 1))
        assert [x.index for x in receipts] == \
            list(range(len(receipts)))


class TestLengthLimitQuirks:
    """The limit is checked before an epsilon edge, not per symbol."""

    def test_epsilon_edge_can_overshoot_limit(self):
        fst = _overshoot()
        # The epsilon edge emits two symbols although the limit is one, and
        # the following input edge is not limited: three symbols in total.
        assert list(fst.translate(["e"], max_length=1)) == \
            [["x", "y", "z"]]
        assert replay(fst, ["e"], 1).receipt(1).output == ["x", "y", "z"]

    def test_input_edge_is_never_limited(self):
        fst = _overshoot()
        assert replay(fst, ["e"], 1).receipt(1).output == ["x", "y", "z"]


class TestProvenance:
    """A receipt points at the edge that first formed it."""

    def test_last_transition(self):
        fst = _overshoot()
        receipt = replay(fst, ["e"], 1).receipt(1)
        assert isinstance(receipt, Receipt)
        source = receipt.provenance
        assert source.source_state == "a"
        assert source.input_symbol == "e"
        assert source.target_state == "f"
        assert source.output == ["z"]
        assert source.edge_index == 0
        assert source.edge_number == 1
        assert not source.is_epsilon

    def test_whole_derivation(self):
        fst = _overshoot()
        receipt = replay(fst, ["e"], 1).receipt(1)
        assert [s.kind for s in receipt.path] == ["epsilon", "input"]
        assert receipt.first_source.input_symbol == "epsilon"

    def test_first_visit_wins(self):
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "x", ["c"])
        fst.add_transition("s", "a", "x", ["c"])
        fst.add_final_state("x")
        receipts = replay(fst, ["a"]).receipts()
        assert len(receipts) == 1
        assert receipts[0].provenance.edge_index == 1


class TestReverseLookup:
    """Codes plus end states map back to receipt numbers."""

    @staticmethod
    def _same_code_two_ends():
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "f1", ["c"])
        fst.add_transition("s", "a", "f2", ["c"])
        fst.add_final_state("f1")
        fst.add_final_state("f2")
        return fst

    def test_same_code_two_receipts(self):
        fst = self._same_code_two_ends()
        assert list(fst.translate(["a"])) == [["c"], ["c"]]
        r = replay(fst, ["a"])
        assert r.ordinal_of(["c"], "f1") == 2
        assert r.ordinal_of(["c"], "f2") == 1
        assert r.ordinals_for(["c"]) == [1, 2]
        assert r.ordinal_of(["c"]) == 1
        assert r.ordinal_of(["missing"]) is None

    def test_ordinal_of_absent_end_state(self):
        fst = _simple()
        assert replay(fst, ["a"]).ordinal_of(["b"], "nope") is None


class TestVersionComparison:
    """Old receipts are compared with the other length limit."""

    @staticmethod
    def _reorder():
        # With the old limit the late branch is reached first; relaxing the
        # limit lets the early branch overtake.
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "m", ["c"])
        fst.add_transition("s", "epsilon", "m2", ["b"])
        fst.add_transition("m2", "a", "end", ["d"])
        fst.add_transition("s", "a", "end", ["e"])
        fst.add_final_state("end")
        return fst

    @staticmethod
    def _swap():
        # Limit 1: ["u"] at B is first, ["p", "q", "r"] at A is second.
        # Limit 2: the epsilon detour reaches A first, so the same two
        # receipts swap numbers even though both codes and multiplicities
        # are unchanged.
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "A", ["p", "q", "r"])
        fst.add_transition("s", "a", "B", ["u"])
        fst.add_transition("s", "epsilon", "E1", ["p"])
        fst.add_transition("E1", "epsilon", "E2", ["q"])
        fst.add_transition("E2", "a", "A", ["r"])
        fst.add_final_state("A")
        fst.add_final_state("B")
        return fst

    def test_same_multiset_can_swap(self):
        fst = self._swap()
        old = replay(fst, ["a"], 1)
        new = replay(fst, ["a"], 2)
        old_codes = sorted((x.output_tuple for x in old))
        new_codes = sorted((x.output_tuple for x in new))
        assert old_codes == new_codes
        assert [x.output for x in old] == [["u"], ["p", "q", "r"]]
        assert [x.output for x in new] == [["p", "q", "r"], ["u"]]

    def test_swap_renumbers_both(self):
        fst = self._swap()
        both = compare_replays(fst, ["a"], 1, 2)
        assert both.renumber(1) == 2
        assert both.renumber(2) == 1
        stats = both.range_stats(1, 2)
        assert stats.total == 2
        assert stats.moved == 2
        assert stats.missing == 0
        assert stats.unchanged == 0
        assert stats.moved_or_missing == 2

    def test_reverse_lookup_both_versions(self):
        fst = self._swap()
        both = compare_replays(fst, ["a"], 1, 2)
        assert both.ordinals(["p", "q", "r"], "A") == (2, 1)
        assert both.ordinals(["u"], "B") == (1, 2)
        assert both.ordinals(["never"], "A") == (None, None)

    def test_renumber_and_range(self):
        fst = self._reorder()
        both = compare_replays(fst, ["a"], 10, 1)
        assert both.renumber(1) == both.new.ordinal_of(["b", "d"])
        stats = both.range_stats(1, both.old.count())
        assert isinstance(stats, RangeStats)
        assert stats.total == both.old.count()
        assert stats.total == stats.moved + stats.missing + stats.unchanged
        assert stats.moved_or_missing == both.moved_or_missing(
            1, both.old.count())

    def test_blank_for_missing_receipt(self):
        fst = _epsilon_loop()
        both = compare_replays(fst, ["a"], 1, 4)
        assert both.old.count() == 1
        assert both.new.count() > 1
        assert both.ordinals(["b"], "q1") == (1, 1)
        assert both.old_ordinal(["b", "c", "c", "c"]) is None

    def test_missing_in_new_version(self):
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "f", ["b"])
        fst.add_transition("s", "epsilon", "g", ["c", "d"])
        fst.add_transition("g", "a", "f", [])
        fst.add_final_state("f")
        old = replay(fst, ["a"], 10)
        new = replay(fst, ["a"], 1)
        assert [x.output for x in old] == [["c", "d"], ["b"]]
        # The epsilon edge would start at zero symbols and emit two, which
        # is allowed at limit one, so the detour still runs here.
        assert [x.output for x in new] == [["c", "d"], ["b"]]
        both = compare_replays(fst, ["a"], 10, 1)
        stats = both.range_stats(1, old.count())
        assert stats.total == 2
        assert stats.missing == 0

    def test_receipt_closed_by_new_limit(self):
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "f", ["b"])
        fst.add_transition("s", "epsilon", "g", ["c"])
        fst.add_transition("g", "epsilon", "h", ["d"])
        fst.add_transition("h", "a", "f", ["e"])
        fst.add_final_state("f")
        old = replay(fst, ["a"], 10)
        new = replay(fst, ["a"], 1)
        assert [x.output for x in old] == [["c", "d", "e"], ["b"]]
        # At limit one the second epsilon edge is closed.
        assert [x.output for x in new] == [["b"]]
        both = compare_replays(fst, ["a"], 10, 1)
        stats = both.range_stats(1, old.count())
        assert stats.total == 2
        assert stats.missing == 1

    def test_relocation_count(self):
        fst = self._swap()
        identical = compare_replays(fst, ["a"], 2, 2)
        stats = identical.range_stats(1, identical.old.count())
        assert stats.moved == 0
        assert stats.missing == 0
        assert stats.unchanged == stats.total
        assert identical.moved_or_missing(1, stats.total) == 0


class TestDuplicateCodes:
    """The same code at different end states gives two receipts."""

    @staticmethod
    def _two_ends():
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "f1", ["c"])
        fst.add_transition("s", "a", "f2", ["c"])
        fst.add_final_state("f1")
        fst.add_final_state("f2")
        return fst

    def test_same_code_different_end_state(self):
        fst = self._two_ends()
        receipts = replay(fst, ["a"]).receipts()
        assert [x.output for x in receipts] == [["c"], ["c"]]
        assert [x.final_state for x in receipts] == ["f2", "f1"]
        # The second registered edge is popped first.
        assert receipts[0].provenance.edge_index == 1
        assert receipts[1].provenance.edge_index == 0

    def test_end_state_disambiguates_renumbering(self):
        fst = self._two_ends()
        both = compare_replays(fst, ["a"], 2, 2)
        assert both.renumber_all() == {1: 1, 2: 2}
        assert both.ordinals(["c"], "f1") == (2, 2)
        assert both.ordinals(["c"], "f2") == (1, 1)
        assert both.old.ordinals_all(["c"], "f1") == [2]

    def test_same_pair_collapses_by_first_visit(self):
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "epsilon", "e", [])
        fst.add_transition("e", "a", "f", ["c"])
        fst.add_transition("s", "a", "f", ["c"])
        fst.add_final_state("f")
        receipts = replay(fst, ["a"]).receipts()
        assert [x.output for x in receipts] == [["c"]]
        # The epsilon detour is explored first, so its input edge into f is
        # what forms the receipt.
        assert receipts[0].path[0].is_epsilon
        assert receipts[0].provenance.source_state == "e"


class TestFSTHelpers:
    """Convenience methods on ``FST`` delegate to the replay module."""

    def test_replay_and_receipts(self):
        fst = _simple()
        assert [x.output for x in fst.replay(["a"])] == [["b"]]
        assert fst.receipts(["a"]) == [["b"]]
        assert fst.receipts(["a"]) == list(fst.translate(["a"]))

    def test_compare_limits(self):
        fst = TestVersionComparison._swap()
        both = fst.compare_limits(["a"], 1, 2)
        assert both.renumber_all() == {1: 2, 2: 1}

    def test_differential_helper(self):
        fst = _epsilon_loop()
        assert_matches_translate(fst, ["a"], 5)
        assert_matches_translate(fst, ["a"], 5, first=2, last=4)


class TestLazyRanges:
    """Ranges must not enumerate the whole prefix or sort by code."""

    def test_prefix_is_skipped_without_listing(self):
        fst = _epsilon_loop()
        r = replay(fst, ["a"], 30)
        receipts = r.receipts(5, 7)
        assert [x.ordinal for x in receipts] == [5, 6, 7]
        assert [len(x.output) for x in receipts] == [5, 6, 7]

    def test_open_ended_slice(self):
        fst = _epsilon_loop()
        r = replay(fst, ["a"], 6)
        assert [x.output for x in r[2:4]] == [["b"] + ["c"] * 2,
                                              ["b"] + ["c"] * 3]

    def test_order_is_enumeration_not_sorted(self):
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "a", "f", ["z"])
        fst.add_transition("s", "a", "f", ["a"])
        fst.add_transition("s", "a", "f", ["m"])
        fst.add_final_state("f")
        assert [x.output for x in replay(fst, ["a"])] == \
            [["m"], ["a"], ["z"]]

    def test_repeated_template_receipts_outnumber_events(self):
        # Each template repetition adds one symbol to the code through an
        # epsilon edge, while a single input event consumes the whole word.
        # The codes therefore grow with the repetitions, but the input word
        # stays short.
        fst = FST()
        fst.add_start_state("s")
        fst.add_transition("s", "epsilon", "s", ["b"])
        fst.add_transition("s", "go", "f", ["z"])
        fst.add_final_state("f")
        word = ["go"]
        r = replay(fst, word, 5000)
        assert r.receipt(1).output == ["b"] * 5000 + ["z"]
        assert r.receipt(5000).output == ["b"] * 1 + ["z"]
        assert len(r) == 5001

    def test_short_unit_order_check(self):
        fst = _epsilon_loop()
        for limit in range(1, 8):
            assert replay(fst, ["a"], limit).matches_translate()


class TestErrors:
    """Invalid receipt numbers are reported as index errors."""

    def test_missing_receipt(self):
        fst = _simple()
        with pytest.raises(IndexError):
            replay(fst, ["a"]).receipt(2)

    def test_zero_receipt(self):
        fst = _simple()
        with pytest.raises(IndexError):
            replay(fst, ["a"]).receipt(0)

    def test_get_default(self):
        fst = _simple()
        assert replay(fst, ["a"]).get(9) is None
        assert isinstance(replay(fst, ["a"]).get(9, "blank"), str)

    def test_bad_range(self):
        fst = _simple()
        with pytest.raises(ValueError):
            replay(fst, ["a"]).receipts(0)

    def test_empty_range(self):
        fst = _simple()
        assert replay(fst, ["a"]).receipts(3, 2) == []
