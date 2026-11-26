# built-in
from itertools import product

# external
import pytest

# project
import textdistance
from merge_audit.engine import (
    BOTH,
    NEW_ONLY,
    OLD_ONLY,
    Entry,
    MergeRule,
    _family_suffix_pair,
    alternating_family,
    audit_band,
    audit_long_band,
    boundary_family_pair,
    family_parity_table,
    lowercased_unrestricted,
    score_pair,
)


RULE1 = MergeRule(threshold=1)


def test_alias_keeps_original_characters():
    pair = score_pair('bA', 'AB', RULE1)
    assert pair.restricted == 1
    assert pair.unrestricted == 2
    assert pair.bucket == OLD_ONLY
    assert pair.direction == 'equal'


def test_lowercasing_collapses_the_split():
    assert lowercased_unrestricted('bA', 'AB') == 1
    exact = textdistance.DamerauLevenshtein(external=False, restricted=False)
    assert exact('ba', 'ab') == 1


@pytest.mark.parametrize('length, left, right, restricted, unrestricted, lowered',
                         family_parity_table(12))
def test_family_parity_table(length, left, right, restricted, unrestricted, lowered):
    assert restricted == 1
    assert lowered == 1
    if length % 2 == 0:
        assert unrestricted == 2
        assert score_pair(left, right, RULE1).bucket == OLD_ONLY
    else:
        assert unrestricted == 1
        assert score_pair(left, right, RULE1).bucket == BOTH


def test_threshold_one_never_admits_new_only_pairs():
    alphabet = ['a', 'A', 'b', 'c']
    strings = [
        ''.join(chars)
        for size in range(1, 5)
        for chars in product(alphabet, repeat=size)
    ]
    new_only = []
    for index, left in enumerate(strings):
        for right in strings[index:]:
            if abs(len(left) - len(right)) > RULE1.length_drift:
                continue
            pair = score_pair(left, right, RULE1)
            if pair.bucket == NEW_ONLY:
                new_only.append(pair)
    assert new_only == []


def test_threshold_two_reopens_the_new_only_bucket():
    pair = score_pair('ab', 'bca', MergeRule(threshold=2))
    assert pair.restricted == 3
    assert pair.unrestricted == 2
    assert pair.bucket == NEW_ONLY


def _sample_entries():
    return [
        Entry('bA', code='x1', group='g', lengths=(2, 3)),
        Entry('AB', code='x1', group='g', lengths=(2, 3)),
        Entry('ab', code='dup'),
        Entry('ab', code='dup'),
        Entry('bca', code='x2'),
    ]


def test_audit_band_counts_duplicates_in_scan_order():
    reports = audit_band(_sample_entries(), RULE1)
    assert len(reports) == 2
    first_step = reports[0]
    assert first_step.lengths == {'g': 2}
    assert first_step.counts[NEW_ONLY] == 0
    assert first_step.counts[OLD_ONLY] >= 1
    first_old = first_step.first[OLD_ONLY]
    assert (first_old.left_index, first_old.right_index) == (0, 1)
    assert first_old.left_token == 'bA'
    assert first_old.right_token == 'AB'


def test_coupled_lengths_move_together_and_split_reappears():
    entries = [
        Entry(boundary_family_pair(4)[0], group='g', lengths=(3, 4)),
        Entry(boundary_family_pair(4)[1], group='g', lengths=(3, 4)),
    ]
    reports = audit_band(entries, RULE1)
    assert [report.lengths['g'] for report in reports] == [3, 4]
    assert reports[0].first[BOTH] is not None
    assert reports[1].first[OLD_ONLY] is not None


def test_first_bucket_winner_is_earliest_scan_position():
    entries = [Entry(chars) for chars in ('ab', 'bA', 'aAb', 'abA')]
    report = audit_band(entries, RULE1)[0]
    winner = report.first[OLD_ONLY]
    scanned = [
        (pair.left_index, pair.right_index)
        for pair in report.pairs
        if pair.bucket == OLD_ONLY
    ]
    assert (winner.left_index, winner.right_index) == min(scanned)


def test_long_band_uses_fragments_not_per_length_distance():
    verdict = audit_long_band(2, 300_000_003, RULE1)
    assert verdict.boundary_pairs[0].direction == 'length-2'
    for pair in verdict.boundary_pairs + verdict.spot_checks:
        assert pair.left_token.startswith('...')
        assert len(pair.left_token) <= RULE1.threshold + 5
    assert verdict.even_length_bucket == OLD_ONLY
    assert verdict.odd_length_bucket == BOTH
    assert verdict.stable_bucket == 'neither'


def test_long_band_parity_verdicts_hold_inside_range():
    verdict = audit_long_band(3, 7, RULE1)
    assert verdict.even_length_bucket == OLD_ONLY
    assert verdict.odd_length_bucket == BOTH


def test_suffix_fragments_match_real_strings():
    for length in range(2, 40):
        left, right = boundary_family_pair(length)
        for window in range(2, 8):
            fragment_left, fragment_right = _family_suffix_pair(length, window)
            assert fragment_left == left[-window:]
            assert fragment_right == right[-window:]


def test_alternating_family_truncation():
    assert alternating_family(0) == ''
    assert alternating_family(1) == 'a'
    assert alternating_family(5) == 'aAaAa'
