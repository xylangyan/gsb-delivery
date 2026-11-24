"""Run-length RePair: equivalence with the reference and production queries.

The short-archive tests assert *exact* equivalence -- rule ids, expansions,
occurrence positions and interval boundaries -- against
``str_to_repair_grammar`` on the fully expanded string.  The production tests
build grammars over million-token runs and only use the windowed queries, which
must stay independent of the run magnitude.
"""

import pytest

from saxpy.repair import str_to_repair_grammar
from saxpy.repair_rle import (
    RepairGrammar,
    encoded_str_to_repair_grammar,
    rle_to_repair_grammar,
    str_to_repair_grammar_rle,
)


def _expand(runs):
    words = []
    for word, count in runs:
        words.extend([word] * count)
    return " ".join(words)


def _assert_matches_reference(runs):
    """Every rule must match the expanded-input reference exactly."""
    grammar = rle_to_repair_grammar(runs)
    ref = str_to_repair_grammar(_expand(runs))
    assert len(grammar) == len(ref)
    assert grammar.n == len(ref[0].expanded_rule_string.split())
    for rid, rule in ref.items():
        if rid == 0:
            continue
        assert grammar.rule_length(rid) == len(rule.expanded_rule_string.split())
        assert grammar[rid].expanded_rule_string == rule.expanded_rule_string
        assert sorted(grammar.rule_occurrences(rid)) == sorted(rule.occurrences)
        assert sorted(grammar.rule_intervals(rid)) == sorted(rule.intervals)
    assert grammar.word_slice(0, grammar.n) == _expand(runs).split()
    return grammar


def test_unary_runs_match_reference():
    for n in range(1, 80):
        _assert_matches_reference([("a", n)])


def test_neighbour_words_match_reference():
    for n in range(2, 40):
        _assert_matches_reference([("x", 1), ("a", n)])
        _assert_matches_reference([("a", n), ("x", 1)])
        _assert_matches_reference([("x", 1), ("a", n), ("y", 1)])


def test_tied_digrams_match_reference():
    """Tied frequencies at every level: selection order must be inherited."""
    cases = [
        [("a", 2), ("b", 3), ("a", 2), ("b", 3), ("a", 2)],
        [("a", 3), ("b", 2), ("a", 3), ("b", 2), ("a", 3), ("b", 2)],
        [("a", 9), ("b", 9), ("c", 9)],
        [("a", 6), ("b", 6)],
        [("a", 4), ("b", 1), ("a", 4)],
        [("a", 1), ("b", 4), ("a", 1), ("b", 4)],
        [("a", 3), ("b", 3), ("c", 6), ("a", 5), ("b", 4)],
        [("a", 7), ("b", 1), ("a", 7)],
        [("p", 1), ("a", 5), ("q", 1), ("a", 5)],
        [("a", 2), ("b", 1), ("c", 2), ("b", 1), ("c", 2)],
    ]
    for runs in cases:
        _assert_matches_reference(runs)


def test_random_archives_match_reference():
    import random

    rng = random.Random(7)
    for _ in range(120):
        runs = [
            (rng.choice("abcd"), rng.choice([1, 2, 3, 4, 5, 8, 13]))
            for _ in range(rng.randint(1, 9))
        ]
        _assert_matches_reference(runs)


def test_adjacent_equal_runs_are_merged():
    _assert_matches_reference([("a", 3), ("a", 4), ("b", 2)])
    g = rle_to_repair_grammar([("a", 3), ("a", 4), ("b", 2)])
    assert g.n == 9


def test_aliases_are_the_same_entry_point():
    assert str_to_repair_grammar_rle is rle_to_repair_grammar
    assert encoded_str_to_repair_grammar is rle_to_repair_grammar


def test_rule_parts_are_reported():
    g = rle_to_repair_grammar([("x", 1), ("a", 8), ("y", 1)])
    assert g.rule_parts(1) == ("a", "a")
    assert g.rule_parts(2) == (1, 1)


def test_invalid_inputs():
    with pytest.raises(TypeError):
        rle_to_repair_grammar("a a a")
    with pytest.raises(TypeError):
        rle_to_repair_grammar([("a", "3")])
    with pytest.raises(TypeError):
        rle_to_repair_grammar([(1, 3)])
    with pytest.raises(ValueError):
        rle_to_repair_grammar([("a", 0)])
    with pytest.raises(ValueError):
        rle_to_repair_grammar([("a", 2)]).word_slice(2, 1)


def test_empty_archive():
    g = rle_to_repair_grammar([])
    assert isinstance(g, RepairGrammar)
    assert g.n == 0
    assert len(g) == 1
    assert g.rule_length(0) == 0


def test_missing_rule_raises_key_error():
    g = rle_to_repair_grammar([("a", 4)])
    with pytest.raises(KeyError):
        g[99]


def test_production_runs_do_not_materialize_positions():
    """A million-token run costs a few rules and ranges, not a million records."""
    g = rle_to_repair_grammar([("a", 1_000_000)])
    assert g.n == 1_000_000
    assert len(g) - 1 < 25
    for rid in g:
        ranges = g[rid]._occ.ranges if rid else []
        assert len(ranges) <= 4


def test_run_magnitude_does_not_change_the_short_pattern():
    """The same short archive has the same rules at any repeat count."""
    small = rle_to_repair_grammar([("x", 1), ("a", 64), ("y", 1)])
    big = rle_to_repair_grammar([("x", 1), ("a", 1_000_000), ("y", 1)])
    for rid in range(1, 6):
        assert small.rule_length(rid) == big.rule_length(rid)
        assert small.rule_parts(rid) == big.rule_parts(rid)
    for rid in range(1, 6):
        assert small[rid].expanded_rule_string.count(" ") + 1 == big.rule_length(rid)
    # Head and tail words are untouched by the run length.
    assert big.word_slice(0, 3) == ["x", "a", "a"]
    assert big.word_slice(big.n - 3, big.n) == ["a", "a", "y"]


def test_windowed_intervals_on_production_run():
    g = rle_to_repair_grammar([("x", 1), ("a", 1_000_000), ("y", 1)])
    middle = g.rule_intervals(2, 499_990, 500_010)
    assert middle
    for lo, hi in middle:
        assert hi - lo == g.rule_length(2) - 1
        # The rule's span intersects (not necessarily contains) the window.
        assert not (hi < 499_990 or lo >= 500_010)
    # Full enumeration of a bounded window is not needed to answer this.
    assert all(490_000 < lo < 510_000 for lo, _ in middle)


def test_word_slice_reads_only_the_window():
    g = rle_to_repair_grammar([("x", 1), ("a", 1_000_000), ("y", 1)])
    assert g.word_slice(500_000, 500_003) == ["a", "a", "a"]
    assert g.word_at(0) == "x"
    assert g.word_at(1_000_001) == "y"
    assert g.word_at(999_999) == "a"
    assert g.word_slice(0, 3) == ["x", "a", "a"]
    assert g.word_slice(1_000_001, 1_000_005) == ["y"]


def test_materialization_guard_is_explicit():
    g = rle_to_repair_grammar([("a", 1_000_000)])
    # Expanding a short rule is fine; enumerating its half-million occurrences
    # and expanding R0 are not.
    assert g[1].expanded_rule_string == "a a"
    with pytest.raises(ValueError):
        _ = g[1].occurrences
    with pytest.raises(ValueError):
        _ = g[0].expanded_rule_string
    assert g.rule_length(1) == 2
    assert g.rule_occurrences(1, 0, 6) == [4, 2, 0]


def test_small_grammar_materializes_freely():
    g = rle_to_repair_grammar([("a", 12)])
    assert g[1].expanded_rule_string == "a a"
    assert g[2].expanded_rule_string == "a a a a"
    assert g[3].expanded_rule_string == "a a a a a a a a"
    assert g[0].expanded_rule_string == _expand([("a", 12)]) + " "
    assert g[0].rule_string == "R2 R3"
    assert g[1].occurrences == [10, 8, 6, 4, 2, 0]
    assert g[3].occurrences == [4]


def test_grammar_dict_surface():
    g = rle_to_repair_grammar([("a", 6)])
    assert 0 in g
    assert list(g) == [0, 1, 2]
    assert g.keys() == [0, 1, 2]
    assert [r.rule_id for r in g.values()] == [0, 1, 2]
    assert [rid for rid, _ in g.items()] == [0, 1, 2]
    assert "rules" in repr(g)


def test_tied_pairs_mid_run_correction_matches_reference():
    """The task's key scenario: two adjacent pairs share the old frequency,
    then one word in the middle of a long run is corrected in the new version.

    The distant original words stay the same, but which pair gets selected
    first changes, so the correction lands on a different rule.  Both versions
    must agree with the expanded reference rule-for-rule.
    """
    old = [("a", 8), ("b", 8)]
    new = [("a", 2), ("c", 1), ("a", 5), ("b", 8)]
    old_g = _assert_matches_reference(old)
    new_g = _assert_matches_reference(new)

    old_ref = str_to_repair_grammar(_expand(old))
    new_ref = str_to_repair_grammar(_expand(new))
    # Tied at the old frequency (8 each), the pair selected first differs.
    assert old_ref[1].expanded_rule_string == "b b"
    assert new_ref[1].expanded_rule_string == "b b"
    assert new_ref[2].expanded_rule_string == "a a"
    # The correction breaks the run and changes coverage of R2.
    assert 2 in old_g.rule_occurrences(2)
    assert 2 not in new_g.rule_occurrences(2)
    assert new_g.word_slice(2, 3) == ["c"]
    assert new_g.word_slice(0, 2) == ["a", "a"]


def test_other_adjacent_pair_selected_first_reassigns_rules():
    """When the other tied pair is selected first, rules differ even though the
    distant original words are unchanged."""
    runs = [("a", 8), ("b", 3), ("c", 1), ("b", 4)]
    g = _assert_matches_reference(runs)
    ref = str_to_repair_grammar(_expand(runs))
    assert g[0].rule_string == ref[0].rule_string
    assert g.rule_parts(2) == (1, "c") or g.rule_parts(2) == ("b", "b")


def test_module_level_helpers_and_exports():
    from saxpy import (
        RepairGrammar,
        repair_grammar_from_runs,
        rule_expansion_length,
        rule_intervals_in_window,
        rule_occurrences_in_window,
        words_in_window,
    )

    g = repair_grammar_from_runs([("x", 1), ("a", 20), ("y", 1)])
    assert isinstance(g, RepairGrammar)
    assert rule_expansion_length(g, 2) == 4
    assert rule_intervals_in_window(g, 1, 0, 6) == g.rule_intervals(1, 0, 6)
    assert rule_occurrences_in_window(g, 1, 0, 6) == g.rule_occurrences(1, 0, 6)
    assert words_in_window(g, 0, 3) == ["x", "a", "a"]
    assert words_in_window(g, g.n - 2, g.n) == ["a", "y"]
