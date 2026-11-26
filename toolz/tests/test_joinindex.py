import random
from operator import itemgetter

from toolz.itertoolz import join
from toolz.joinindex import (
    ADDED,
    LOST,
    JoinIndex,
    JoinRow,
    JoinSpec,
    RangeCounts,
    join_index,
    join_rows,
)


def identity(x):
    return x


def _pairs(rows):
    return [(row.left, row.right) for row in rows]


def test_join_rows_matches_join_under_fuzzing():
    rng = random.Random(0)
    keys = ('a', 'b', 'c', 1, True, None)
    modes = ((None, None), (None, 'R'), ('L', None), ('L', 'R'))
    for _ in range(500):
        left = [(rng.choice(keys), i) for i in range(rng.randint(0, 5))]
        right = [(rng.choice(keys), i) for i in range(rng.randint(0, 5))]
        for left_default, right_default in modes:
            expected = list(
                join(0, left, 0, right, left_default, right_default)
            )
            actual = _pairs(
                join_rows(0, left, 0, right, left_default, right_default)
            )
            assert actual == expected


def test_default_origin_is_distinct_from_real_default_value():
    left = [(1, 'a')]
    right = [(1, 'x'), (2, 'y')]
    rows = list(join_rows(0, left, 0, right, 1, 1))
    assert rows[0] == JoinRow((1, 'a'), (1, 'x'), False, False, 0, 0)
    assert rows[1] == JoinRow(1, (2, 'y'), True, False, None, 1)

    index = JoinIndex.from_rows(rows, rows)
    assert index.row(0).left_origin == ('record', 0)
    assert index.row(1).left_origin == ('default',)
    assert index.records(1) == (1, (2, 'y'))
    assert index.source_records(1) is None


def test_true_and_one_share_a_group_but_keep_their_row_numbers():
    left = [('a', True), ('b', 1)]
    right = [('x', 1)]
    rows = list(join_rows(1, left, 1, right, None, None))
    assert [(r.left, r.left_pos) for r in rows] == [
        (('a', True), 0),
        (('b', 1), 1),
    ]

    index = JoinIndex.from_rows(rows, rows)
    assert index.lookup(0) == 0
    assert index.lookup(1) == 1

    # Editing the first right row's 1 moves the two early rows to the tail.
    new_right = [('x', 2)]
    new = list(join_rows(1, left, 1, new_right, None, None))
    new_index = JoinIndex.from_rows(rows, new, right_id=itemgetter(0))
    assert [new_index.lookup(i) for i in range(2)] == [1, 2]
    assert (
        new_index.lookup_new_pair(('a', True), None, right_default=True) == 1
    )
    assert new_index.lookup_new_pair(('b', 1), None, right_default=True) == 2

    # A second right row with 1 later in the right sequence supplies a real
    # match, so the trailing left group is not emitted a second time.
    new_right = [('x', 2), ('y', 1)]
    new = list(join_rows(1, left, 1, new_right, None, None))
    assert _pairs(new) == [
        (None, ('x', 2)),
        (('a', True), ('y', 1)),
        (('b', 1), ('y', 1)),
    ]
    new_index = JoinIndex.from_rows(rows, new, right_id=itemgetter(0))
    assert [new_index.lookup(i) for i in range(2)] == [1, 2]


def test_left_groups_are_emitted_in_creation_order_not_source_order():
    left = [('a', 1), ('b', 2)]
    right = [('first', 1), ('second', 2)]
    rows = list(join_rows(1, left, 1, right, None, None))
    assert [r.left_pos for r in rows] == [0, 1]

    # A group whose first row is far down the source keeps its group order.
    left = [('a', 1), ('mid', 9), ('b', 2), ('a2', 1)]
    rows = list(join_rows(1, left, 1, right, None, None))
    assert [r.left for r in rows] == [
        ('a', 1),
        ('a2', 1),
        ('b', 2),
        ('mid', 9),
    ]


def test_trailing_left_block_includes_untouched_groups_in_creation_order():
    left = [('a', 1), ('mid', 9), ('b', 2), ('a2', 1)]
    right = [('r', 1)]
    rows = list(join_rows(1, left, 1, right, None, None))
    assert [(r.left, r.left_pos, r.right_default) for r in rows] == [
        (('a', 1), 0, False),
        (('a2', 1), 3, False),
        (('mid', 9), 1, True),
        (('b', 2), 2, True),
    ]


def test_right_key_edit_moves_early_rows_to_trailing_left_block():
    left = [('a', 1), ('b', 1), ('c', 3)]
    old_right = [('r1', 1), ('r2', 3)]
    new_right = [('r1', 2), ('r2', 3)]

    old = list(join_rows(1, left, 1, old_right, None, None))
    new = list(join_rows(1, left, 1, new_right, None, None))
    assert _pairs(old) == [
        (('a', 1), ('r1', 1)),
        (('b', 1), ('r1', 1)),
        (('c', 3), ('r2', 3)),
    ]
    assert _pairs(new) == [
        (None, ('r1', 2)),
        (('c', 3), ('r2', 3)),
        (('a', 1), None),
        (('b', 1), None),
    ]

    # The right records are edited in place here, so match them by their id.
    old_records = [dict(id='r1', key=1), dict(id='r2', key=3)]
    new_records = [dict(id='r1', key=2), dict(id='r2', key=3)]
    old = list(join_rows(1, left, 'key', old_records, None, None))
    new = list(join_rows(1, left, 'key', new_records, None, None))
    index = JoinIndex.from_rows(
        old, new, left_id=itemgetter(0), right_id=itemgetter('id')
    )
    assert [index.lookup(i) for i in range(3)] == [2, 3, 1]
    assert index.range_counts() == RangeCounts(3, 0, 3, 0)


def test_later_right_group_with_same_key_suppresses_trailing_block():
    left = [('a', 1), ('b', 1)]
    old_right = [('r1', 1)]
    new_right = [('r1', 9), ('r2', 1)]
    old = list(join_rows(1, left, 1, old_right, None, None))
    new = list(join_rows(1, left, 1, new_right, None, None))
    assert _pairs(old) == [(('a', 1), ('r1', 1)), (('b', 1), ('r1', 1))]
    assert _pairs(new) == [
        (None, ('r1', 9)),
        (('a', 1), ('r2', 1)),
        (('b', 1), ('r2', 1)),
    ]

    old_records = [dict(id='r1', key=1)]
    new_records = [dict(id='r1', key=9), dict(id='r2', key=1)]
    old = list(join_rows(1, left, 'key', old_records, None, None))
    new = list(join_rows(1, left, 'key', new_records, None, None))
    index = JoinIndex.from_rows(
        old, new, left_id=itemgetter(0), right_id=itemgetter('id')
    )
    assert [index.lookup(i) for i in range(2)] == [1, 2]


def test_lost_rows_are_counted_separately_from_new_pairs():
    old_left = [('a', 1), ('b', 1)]
    old_right = [('r1', 1)]
    new_left = [('a', 1)]
    new_right = [('r2', 1)]

    old = list(join_rows(1, old_left, 1, old_right, None, None))
    new = list(join_rows(1, new_left, 1, new_right, None, None))
    index = JoinIndex.from_rows(old, new)

    # The 'a' record keeps its occurrence and now pairs with r2; 'b' is gone.
    assert index.lookup(0) == 0
    assert index.lookup(1) is LOST
    assert index.range_counts(0, 2) == RangeCounts(2, 1, 0, 1)
    assert index.new_range_counts(0, 1) == (1, 1, 0, 0)
    assert index.lost_rows() == (1,)


def test_duplicate_pairs_stay_distinct_and_ordered():
    left = [('a', 1), ('a', 1)]
    right = [('r', 1)]
    old = list(join_rows(1, left, 1, right, None, None))
    new = list(join_rows(1, left, 1, right, None, None))
    index = JoinIndex.from_rows(old, new)

    assert index.lookup(0) == 0
    assert index.lookup(1) == 1
    assert index.lookup_pair(('a', 1), ('r', 1), 0) == 0
    assert index.lookup_pair(('a', 1), ('r', 1), 1) == 1
    assert index.pair_occurrences(('a', 1), ('r', 1)) == 2
    assert index.lookup_pair(('a', 1), ('r', 1), 2) is LOST


def test_lookup_new_pair_reports_the_actual_new_row():
    old_left = [('a', 1)]
    old_right = [('r1', 1)]
    new_left = [('a', 1), ('b', 2)]
    new_right = [('r1', 1), ('r2', 2)]
    old = list(join_rows(1, old_left, 1, old_right, None, None))
    new = list(join_rows(1, new_left, 1, new_right, None, None))
    index = JoinIndex.from_rows(old, new)

    assert _pairs(new) == [(('a', 1), ('r1', 1)), (('b', 2), ('r2', 2))]
    assert index.lookup_new_pair(('b', 2), ('r2', 2)) == 1
    assert index.lookup_new_pair(('a', 1), ('r1', 1)) == 0
    assert index.lookup_new_pair(('b', 2), ('r1', 1)) is ADDED
    assert index.old_of_new(1) is ADDED


def test_new_rows_that_move_earlier_or_later():
    old_left = [('a', 1), ('b', 2)]
    old_right = [('r1', 1), ('r2', 2)]
    new_right = [('r2', 2), ('r1', 1)]
    old = list(join_rows(1, old_left, 1, old_right, None, None))
    new = list(join_rows(1, old_left, 1, new_right, None, None))

    index = JoinIndex.from_rows(old, new, right_id=itemgetter(0))
    assert [index.lookup(i) for i in range(2)] == [1, 0]
    assert index.range_counts() == RangeCounts(2, 0, 2, 0)
    assert index.new_range_counts() == (2, 0, 2, 0)


def test_range_counts_and_segments_compress_constant_shift():
    old_left = [('a', 1), ('b', 2), ('c', 3), ('d', 4)]
    old_right = [('r1', 1), ('r2', 2), ('r3', 3), ('r4', 4)]
    new_right = old_right[1:] + old_right[:1]
    old = list(join_rows(1, old_left, 1, old_right, None, None))
    new = list(join_rows(1, old_left, 1, new_right, None, None))

    index = JoinIndex.from_rows(old, new, right_id=itemgetter(0))
    assert index.range_counts() == RangeCounts(4, 0, 4, 0)
    # A rotation is one constant shift followed by a wrap correction.
    assert index.segments() == (
        __import__(
            'toolz.joinindex', fromlist=['LineageSegment']
        ).LineageSegment(0, 1, 'moved', 3),
        __import__(
            'toolz.joinindex', fromlist=['LineageSegment']
        ).LineageSegment(1, 4, 'moved', -1),
    )

    assert index.range_counts(1, 3) == RangeCounts(2, 0, 2, 0)
    assert index.first_records(0, 4) == (('a', 1), ('r1', 1))


def test_first_records_skips_rows_with_a_default_side():
    left = [('a', 1)]
    right = [('r', 1), ('q', 2)]
    rows = list(join_rows(1, left, 1, right, None, None))
    index = JoinIndex.from_rows(rows, rows)
    assert index.first_records(0, 2) == (('a', 1), ('r', 1))
    assert index.first_records(1, 2) is None
    assert index.first_records(0, 1) == (('a', 1), ('r', 1))


def test_join_index_accepts_specs_and_tuples():
    old_left = [('a', 1)]
    old_right = [('r1', 1)]
    new_right = [('r2', 1)]
    old = (1, old_left, 1, old_right, None, None)
    new = JoinSpec(1, old_left, 1, new_right, None, None)

    index = join_index(old, new)
    assert len(index) == 1
    assert index.lookup(0) == 0
    assert index.segments()[0].status == 'same'
    assert index[0] == 0
    assert index.get(0, -1) == 0
    # The exact old pair is gone because r1 was re-keyed, even though the
    # old row itself can be tracked by its surviving record.
    assert index.lookup_pair(('a', 1), ('r1', 1)) is LOST


def test_join_index_reports_lost_only_when_left_record_disappears():
    old_left = [('a', 1), ('b', 2)]
    old_right = [('r1', 1), ('r2', 2)]
    new_left = [('a', 1)]
    new_right = [('r1', 1), ('r2', 2)]
    old = (1, old_left, 1, old_right, None, None)
    new = (1, new_left, 1, new_right, None, None)
    index = join_index(old, new)

    assert index.lookup(0) == 0
    assert index.lookup(1) is LOST
    assert index.get(1) is None
    assert index.get(1, -1) == -1


def test_join_index_custom_identity_survives_key_edits():
    old_left = [('id1', 'a'), ('id2', 'b')]
    new_left = [('id1', 'z'), ('id2', 'b')]
    right = [('id1', 'x'), ('id2', 'y')]
    old = (lambda r: r[1], old_left, 0, right, None, None)
    new = (lambda r: r[1], new_left, 0, right, None, None)

    index = join_index(old, new, left_id=itemgetter(0),
                       right_id=itemgetter(0))
    assert index.lookup(0) == 0
    assert index.lookup(1) == 1


def test_range_counts_support_equal_step_sequences():
    left = [('a', i) for i in range(6)]
    right = [('r', i) for i in range(6)]
    old = list(join_rows(1, left, 1, right, None, None))
    new = list(join_rows(1, left, 1, right, None, None))
    index = JoinIndex.from_rows(old, new)

    assert index.range_counts(0, 6, 2) == RangeCounts(3, 3, 0, 0)
    assert index.range_counts((0, 6, 2)) == RangeCounts(3, 3, 0, 0)
    assert index.range_counts(slice(0, 6, 2)) == RangeCounts(3, 3, 0, 0)
    assert index.new_range_counts(1, 6, 2) == (3, 3, 0, 0)


def test_pair_queries_use_configured_record_identities():
    old_left = [('L1', 'a', 1)]
    new_left = [('L1', 'z', 1)]
    old_right = [('R1', 'x', 1)]
    new_right = [('R1', 'y', 1)]
    old = list(join_rows(2, old_left, 2, old_right, None, None))
    new = list(join_rows(2, new_left, 2, new_right, None, None))

    index = JoinIndex.from_rows(
        old, new, left_id=itemgetter(0), right_id=itemgetter(0)
    )
    assert index.lookup(0) == 0
    # Identity is stable, so the pair is still "present" by identity even
    # though both records' key fields changed.
    assert index.lookup_pair(('L1', 'a', 1), ('R1', 'x', 1)) == 0
    assert index.lookup_new_pair(('L1', 'z', 1), ('R1', 'y', 1)) == 0
    assert index.lookup_new_pair(('L1', 'z', 1), ('R2', 'y', 1)) is ADDED


def test_index_from_raw_pairs_cannot_distinguish_default_collisions():
    old_rows = [('x', None), (None, 'y')]
    new_rows = [('x', None), (None, 'y')]
    index = JoinIndex(old_rows, new_rows)
    assert index.lookup(0) == 0
    assert index.lookup(1) == 1
    # Both sides look like ordinary values, so neither origin flag is set.
    assert index.row(0) == JoinRow('x', None)
