import jmespath
import pytest

from jmespath.exceptions import JMESPathTypeError
from jmespath.live import (
    Leaderboard,
    Diff,
    UnsupportedExpressionError,
)

OR_EXPR = (
    'map(&label, (sort_by(shards[].rows[] | [?live], &rank) | [0:2])'
    ' || (sort_by(backup[?live], &rank) | [0:2]))'
)
NOT_NULL_EXPR = (
    'not_null(sort_by(shards[].rows[] | [?live], &rank) | [0:2],'
    ' sort_by(backup[?live], &rank) | [0:2])'
)

MAX_ROUND_ACCESSES = 256
MAIN_ROWS = 10000


def build_document(main_rows=MAIN_ROWS):
    rows = [
        {'id': 'A', 'live': True, 'rank': 10, 'label': '阀门'},
        {'id': 'N', 'live': True, 'rank': 10, 'label': None},
    ]
    for i in range(main_rows):
        rows.append({
            'id': 'M%d' % i,
            'live': True,
            'rank': 11 + i,
            'label': 'm%d' % i,
        })
    rows.append({'id': 'Z', 'live': False, 'rank': '80', 'label': 'z'})
    return {
        'shards': [
            {'rows': rows[:2]},
            {'rows': rows[2:502]},
            {'rows': rows[502:]},
        ],
        'backup': [
            {'id': 'BANG', 'live': False, 'rank': '!', 'label': 'bang'},
            {'id': 'E', 'live': True, 'rank': 8, 'label': 'e'},
        ],
    }


def flat_rows(doc):
    rows = []
    for shard in doc['shards']:
        rows.extend(shard['rows'])
    return rows


def replace_row(doc, board, row, record):
    if board == 'main':
        shards = doc['shards']
        offset = row
        for shard in shards:
            if offset < len(shard['rows']):
                shard['rows'][offset] = record
                return
            offset -= len(shard['rows'])
        raise IndexError(row)
    doc['backup'][row] = record


def delete_row(doc, board, row):
    if board == 'main':
        offset = row
        for shard in doc['shards']:
            if offset < len(shard['rows']):
                del shard['rows'][offset]
                return
            offset -= len(shard['rows'])
        raise IndexError(row)
    del doc['backup'][row]


def insert_row(doc, board, row, record):
    if board == 'main':
        offset = row
        for shard in doc['shards']:
            if offset <= len(shard['rows']):
                shard['rows'].insert(offset, record)
                return
            offset -= len(shard['rows'])
        doc['shards'][-1]['rows'].append(record)
        return
    doc['backup'].insert(row, record)


@pytest.fixture()
def setup():
    doc = build_document()
    leaderboard = Leaderboard()
    leaderboard.register(doc)
    sub_or = leaderboard.subscribe(OR_EXPR)
    sub_not_null = leaderboard.subscribe(NOT_NULL_EXPR)
    return doc, leaderboard, sub_or, sub_not_null


def assert_matches_plain(doc, leaderboard, subscriptions):
    for subscription, expression in subscriptions:
        try:
            expected = jmespath.search(expression, doc)
            expected_error = None
        except JMESPathTypeError as error:
            expected = None
            expected_error = error
        if expected_error is None:
            assert leaderboard.error(subscription) is None
            assert leaderboard.result(subscription) == expected
        else:
            error = leaderboard.error(subscription)
            assert error is not None
            assert error.actual_type == expected_error.actual_type
            assert (list(error.expected_types) ==
                    list(expected_error.expected_types))


def check_budget(leaderboard):
    stats = leaderboard.round_stats
    assert stats.accesses <= MAX_ROUND_ACCESSES
    return stats


def test_initial_registration(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    assert leaderboard.result(sub_or) == ['阀门', None]
    assert leaderboard.result(sub_not_null) == flat_rows(doc)[:2]
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_replace_keeps_display_but_changes_source(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    # Replace A with another material id, keeping its row, key and
    # label; enable the string-keyed backup row.
    leaderboard.replace('main', 0,
                        {'id': 'A2', 'live': True, 'rank': 10,
                         'label': '阀门'})
    leaderboard.replace('backup', 0,
                        {'id': 'BANG', 'live': True, 'rank': '!',
                         'label': 'bang'})
    replace_row(doc, 'main', 0,
                {'id': 'A2', 'live': True, 'rank': 10, 'label': '阀门'})
    replace_row(doc, 'backup', 0,
                {'id': 'BANG', 'live': True, 'rank': '!', 'label': 'bang'})
    diffs = leaderboard.diff()

    or_diff = diffs[sub_or]
    assert or_diff.kind == Diff.SOURCE_CHANGED
    assert or_diff.transition == 'ok->ok'
    assert or_diff.result == ['阀门', None]
    assert or_diff.previous_result == ['阀门', None]

    not_null_diff = diffs[sub_not_null]
    assert not_null_diff.kind == Diff.ERROR
    assert not_null_diff.transition == 'ok->error'
    error = not_null_diff.error
    assert error.board == 'backup'
    assert error.material_id == 'E'
    assert error.key == 8
    assert error.actual_type == 'number'
    assert error.expected_types == ['string']
    assert error.function == 'sort_by'

    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_string_key_on_main_board_fails_on_first_numeric_row(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    leaderboard.replace('main', 0,
                        {'id': 'A2', 'live': True, 'rank': 10,
                         'label': '阀门'})
    leaderboard.replace('backup', 0,
                        {'id': 'BANG', 'live': True, 'rank': '!',
                         'label': 'bang'})
    leaderboard.diff()

    # Turn the replacement's key into a string and enable Z.
    z_row = len(flat_rows(doc)) - 1
    leaderboard.replace('main', 0,
                        {'id': 'A2', 'live': True, 'rank': '10',
                         'label': '阀门'})
    leaderboard.replace('main', z_row,
                        {'id': 'Z', 'live': True, 'rank': '80',
                         'label': 'z'})
    replace_row(doc, 'main', 0,
                {'id': 'A2', 'live': True, 'rank': '10', 'label': '阀门'})
    replace_row(doc, 'main', z_row,
                {'id': 'Z', 'live': True, 'rank': '80', 'label': 'z'})
    diffs = leaderboard.diff()

    # Both subscriptions fail on the main board first: N holds the
    # first numeric key after the string "10".
    for subscription, transition in ((sub_or, 'ok->error'),
                                     (sub_not_null, 'error->error')):
        diff = diffs[subscription]
        assert diff.kind == Diff.ERROR
        assert diff.transition == transition
        assert diff.error.board == 'main'
        assert diff.error.material_id == 'N'
        assert diff.error.key == 10
        assert diff.error.actual_type == 'number'
        assert diff.error.expected_types == ['string']

    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_error_site_moves_to_untouched_row(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    z_row = len(flat_rows(doc)) - 1
    corrections = [
        ('replace', 'main', 0,
         {'id': 'A2', 'live': True, 'rank': 10, 'label': '阀门'}),
        ('replace', 'backup', 0,
         {'id': 'BANG', 'live': True, 'rank': '!', 'label': 'bang'}),
        ('replace', 'main', 0,
         {'id': 'A2', 'live': True, 'rank': '10', 'label': '阀门'}),
        ('replace', 'main', z_row,
         {'id': 'Z', 'live': True, 'rank': '80', 'label': 'z'}),
    ]
    for _, board, row, record in corrections[:2]:
        leaderboard.replace(board, row, record)
        replace_row(doc, board, row, record)
    leaderboard.diff()
    for _, board, row, record in corrections[2:]:
        leaderboard.replace(board, row, record)
        replace_row(doc, board, row, record)
    leaderboard.diff()

    # Only remove the replacement row; Z is not touched this round,
    # yet both subscriptions must now report Z's string key.
    leaderboard.delete('main', 0)
    delete_row(doc, 'main', 0)
    diffs = leaderboard.diff()

    for subscription in (sub_or, sub_not_null):
        diff = diffs[subscription]
        assert diff.kind == Diff.ERROR
        assert diff.transition == 'error->error'
        assert diff.error.board == 'main'
        assert diff.error.material_id == 'Z'
        assert diff.error.key == '80'
        assert diff.error.actual_type == 'string'
        assert diff.error.expected_types == ['number']
        assert diff.previous_error.material_id == 'N'

    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_recovered_and_value_changed_and_unchanged(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    z_row = len(flat_rows(doc)) - 1
    leaderboard.replace('main', 0,
                        {'id': 'A2', 'live': True, 'rank': '10',
                         'label': '阀门'})
    leaderboard.replace('main', z_row,
                        {'id': 'Z', 'live': True, 'rank': '80',
                         'label': 'z'})
    replace_row(doc, 'main', 0,
                {'id': 'A2', 'live': True, 'rank': '10', 'label': '阀门'})
    replace_row(doc, 'main', z_row,
                {'id': 'Z', 'live': True, 'rank': '80', 'label': 'z'})
    leaderboard.diff()
    leaderboard.delete('main', 0)
    delete_row(doc, 'main', 0)
    leaderboard.diff()

    # Disable Z again: both subscriptions recover.
    z_row = len(flat_rows(doc)) - 1
    leaderboard.replace('main', z_row,
                        {'id': 'Z', 'live': False, 'rank': '80',
                         'label': 'z'})
    replace_row(doc, 'main', z_row,
                {'id': 'Z', 'live': False, 'rank': '80', 'label': 'z'})
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.RECOVERED
    assert diffs[sub_or].transition == 'error->ok'
    assert diffs[sub_or].result == [None, 'm0']
    assert diffs[sub_not_null].kind == Diff.RECOVERED
    assert diffs[sub_not_null].result == flat_rows(doc)[:2]
    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])

    # Change N's label: the page value changes.
    leaderboard.replace('main', 0,
                        {'id': 'N', 'live': True, 'rank': 10,
                         'label': '氮'})
    replace_row(doc, 'main', 0,
                {'id': 'N', 'live': True, 'rank': 10, 'label': '氮'})
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.VALUE_CHANGED
    assert diffs[sub_or].result == ['氮', 'm0']
    assert diffs[sub_or].previous_result == [None, 'm0']
    assert diffs[sub_not_null].kind == Diff.VALUE_CHANGED
    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])

    # No corrections: both results can be reused.
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.UNCHANGED
    assert diffs[sub_not_null].kind == Diff.UNCHANGED
    check_budget(leaderboard)


def test_insert_and_delete_by_row_and_material_id(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    leaderboard.insert('main', 0,
                       {'id': 'NEW', 'live': True, 'rank': 5,
                        'label': '新'})
    insert_row(doc, 'main', 0,
               {'id': 'NEW', 'live': True, 'rank': 5, 'label': '新'})
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.VALUE_CHANGED
    assert diffs[sub_or].result == ['新', '阀门']
    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])

    removed = leaderboard.delete('main', material_id='NEW')
    assert removed['id'] == 'NEW'
    delete_row(doc, 'main', 0)
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.VALUE_CHANGED
    assert diffs[sub_or].result == ['阀门', None]
    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_replace_by_material_id(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    leaderboard.replace('main', record={'id': 'N', 'live': True,
                                        'rank': 10, 'label': '氮'},
                        material_id='N')
    replace_row(doc, 'main', 1,
                {'id': 'N', 'live': True, 'rank': 10, 'label': '氮'})
    diffs = leaderboard.diff()
    assert diffs[sub_or].kind == Diff.VALUE_CHANGED
    assert diffs[sub_or].result == ['阀门', '氮']
    check_budget(leaderboard)
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_row_and_material_id_mismatch_rejected(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    with pytest.raises(ValueError):
        leaderboard.delete('main', row=0, material_id='N')
    with pytest.raises(ValueError):
        leaderboard.delete('main', material_id='UNKNOWN')
    with pytest.raises(ValueError):
        leaderboard.delete('main')
    with pytest.raises(IndexError):
        leaderboard.delete('main', row=10 ** 9)
    with pytest.raises(ValueError):
        leaderboard.delete('shards', row=0)


def test_or_falls_back_to_backup_board():
    doc = {
        'shards': [{'rows': [
            {'id': 'X', 'live': False, 'rank': 1, 'label': 'x'},
        ]}],
        'backup': [
            {'id': 'B1', 'live': True, 'rank': 8, 'label': 'b1'},
            {'id': 'B2', 'live': True, 'rank': 9, 'label': 'b2'},
        ],
    }
    leaderboard = Leaderboard()
    leaderboard.register(doc)
    sub_or = leaderboard.subscribe(OR_EXPR)
    sub_not_null = leaderboard.subscribe(NOT_NULL_EXPR)
    # Main page is empty (falsy), so || takes the backup page.
    assert leaderboard.result(sub_or) == ['b1', 'b2']
    # not_null sees the (non-null) empty main page first.
    assert leaderboard.result(sub_not_null) == []
    assert_matches_plain(doc, leaderboard, [
        (sub_or, OR_EXPR), (sub_not_null, NOT_NULL_EXPR)])


def test_first_live_row_with_bad_key_type():
    doc = {
        'shards': [{'rows': [
            {'id': 'Q', 'live': True, 'rank': None, 'label': 'q'},
            {'id': 'R', 'live': True, 'rank': 3, 'label': 'r'},
        ]}],
        'backup': [],
    }
    leaderboard = Leaderboard()
    leaderboard.register(doc)
    sub_or = leaderboard.subscribe(OR_EXPR)
    error = leaderboard.error(sub_or)
    assert error is not None
    assert error.board == 'main'
    assert error.material_id == 'Q'
    assert error.actual_type == 'null'
    assert error.expected_types == ['string', 'number']
    assert_matches_plain(doc, leaderboard, [(sub_or, OR_EXPR)])


def test_unsupported_expression_rejected_at_subscribe(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    with pytest.raises(UnsupportedExpressionError):
        # sort_by over unfiltered rows cannot stay incremental.
        leaderboard.subscribe('sort_by(shards[].rows[], &rank)')
    with pytest.raises(UnsupportedExpressionError):
        leaderboard.subscribe('length(shards)')
    with pytest.raises(UnsupportedExpressionError):
        # A bare sorted board is never materialized.
        leaderboard.subscribe('sort_by(backup[?live], &rank)')
    with pytest.raises(UnsupportedExpressionError):
        leaderboard.subscribe('max_by(backup[?live], &rank)')


def test_plain_search_is_unaffected(setup):
    doc, leaderboard, sub_or, sub_not_null = setup
    assert jmespath.search('shards[0].rows[0].label', doc) == '阀门'
    assert jmespath.search(OR_EXPR, doc) == ['阀门', None]


def test_randomized_differential_against_plain_search():
    import random

    rng = random.Random(42)

    def rand_rank():
        roll = rng.random()
        if roll < 0.6:
            return rng.randint(1, 100)
        if roll < 0.8:
            return str(rng.randint(1, 100))
        if roll < 0.9:
            return None
        return rng.choice(['!', '80', 'x'])

    def rand_record(mid):
        return {
            'id': mid,
            'live': rng.random() < 0.8,
            'rank': rand_rank(),
            'label': rng.choice(['a', 'b', None, '阀门']),
        }

    subscriptions = [(OR_EXPR, None), (NOT_NULL_EXPR, None)]
    fresh = [0]
    for _ in range(3):
        rows = [rand_record('R%d' % i) for i in range(rng.randint(0, 60))]
        backup = [rand_record('B%d' % i) for i in range(rng.randint(0, 8))]
        doc = {'shards': [{'rows': rows[:10]}, {'rows': rows[10:]}],
               'backup': list(backup)}
        leaderboard = Leaderboard()
        leaderboard.register(doc)
        subs = [(leaderboard.subscribe(expression), expression)
                for expression, _ in subscriptions]
        leaderboard.diff()
        assert_matches_plain(doc, leaderboard, subs)
        for _ in range(30):
            size = len(flat_rows(doc))
            backup_size = len(doc['backup'])
            board = rng.choice(['main', 'backup'])
            board_size = size if board == 'main' else backup_size
            op = rng.choice(['replace', 'insert', 'delete'])
            if op == 'insert' or board_size == 0:
                record = rand_record('N%d' % fresh[0])
                fresh[0] += 1
                pos = rng.randint(0, board_size)
                leaderboard.insert(board, pos, record)
                insert_row(doc, board, pos, record)
            elif op == 'delete':
                pos = rng.randrange(board_size)
                leaderboard.delete(board, pos)
                delete_row(doc, board, pos)
            else:
                record = rand_record('N%d' % fresh[0])
                fresh[0] += 1
                pos = rng.randrange(board_size)
                leaderboard.replace(board, pos, record)
                replace_row(doc, board, pos, record)
            leaderboard.diff()
            check_budget(leaderboard)
            assert_matches_plain(doc, leaderboard, subs)
