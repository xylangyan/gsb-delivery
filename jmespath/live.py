"""Incremental ("live") leaderboard queries over a sharded inventory.

This module adds a small live-query layer on top of jmespath.  A
publisher receives a supplier leaderboard in shards, subscribes to
several JMESPath expressions and then keeps correcting individual
rows.  The first registration may read the whole inventory; every
later correction round only touches a logarithmic number of cached
records plus the rows that actually changed, no matter how large the
boards are.  Reads of unchanged records are counted per round so the
caller can verify the access budget (repeated reads count as well;
reads served from already aggregated counts or ranking segments do
not count per record inside the segment).

Usage
-----

>>> import jmespath.live
>>> lb = jmespath.live.Leaderboard()
>>> lb.register(document)             # full read, once
>>> sub = lb.subscribe(EXPRESSION)    # initial evaluation
>>> lb.replace('main', 0, record)     # corrections by row number
>>> diffs = lb.diff()                 # per-subscription diff report
>>> lb.round_stats.accesses           # unchanged-record reads this round

Supported subscriptions are evaluated exactly like a plain
``jmespath.search`` call over the whole current document, including
stable ordering of equal sort keys, ``map`` keeping null slots and
``sort_by`` key type errors raised for rows outside the page range.
Expressions that the engine cannot evaluate incrementally raise
:class:`UnsupportedExpressionError` at subscription time.  Plain
``jmespath.search`` is not affected by this module.
"""

import heapq
import random
from numbers import Number

from jmespath import exceptions
from jmespath.compat import string_type
from jmespath.functions import Functions
from jmespath.parser import Parser
from jmespath.visitor import _equals, _is_comparable

__all__ = [
    'Leaderboard', 'Diff', 'QueryError', 'RoundStats',
    'UnsupportedExpressionError',
    'MAIN_BOARD', 'BACKUP_BOARD',
]

MAIN_BOARD = 'main'
BACKUP_BOARD = 'backup'

_BOARD_SOURCES = {
    MAIN_BOARD: 'shards[].rows[] | [?live]',
    BACKUP_BOARD: 'backup[?live]',
}

_SORT_KEY_TYPES = ('number', 'string')


class UnsupportedExpressionError(exceptions.JMESPathError):
    """A subscription uses constructs the live engine cannot evaluate."""


class QueryError(object):
    """Describes why a subscription currently fails.

    Carries the board (``main``/``backup``) and source expression of
    the failing leaderboard branch, the stable material id of the row
    whose sort key failed first, the received key type and the key
    type that was required at that point.
    """

    def __init__(self, board, source, material_id, key, actual_type,
                 expected_types, function):
        self.board = board
        self.source = source
        self.material_id = material_id
        self.key = key
        self.actual_type = actual_type
        self.expected_types = list(expected_types)
        self.function = function

    def __repr__(self):
        return ('QueryError(board=%r, material_id=%r, actual_type=%r, '
                'expected_types=%r, function=%r)' % (
                    self.board, self.material_id, self.actual_type,
                    self.expected_types, self.function))

    def __str__(self):
        return ('In function %s() on board "%s" (%s), invalid sort key '
                'for material %s: expected one of: %s, received: "%s"' % (
                    self.function, self.board, self.source,
                    self.material_id, self.expected_types,
                    self.actual_type))


class Diff(object):
    """The per-subscription change report produced by ``Leaderboard.diff``.

    ``kind`` is one of:

    - ``unchanged``: the result can be reused as is.
    - ``value_changed``: the page value changed.
    - ``source_changed``: the page shows the same value but at least
      one slot now comes from a different material.
    - ``error``: the query turned into an error (or still fails, with
      updated details in ``error``).
    - ``recovered``: the query recovered from an error.
    """

    UNCHANGED = 'unchanged'
    VALUE_CHANGED = 'value_changed'
    SOURCE_CHANGED = 'source_changed'
    ERROR = 'error'
    RECOVERED = 'recovered'

    def __init__(self, subscription, expression, kind, transition,
                 result, previous_result, error, previous_error):
        self.subscription = subscription
        self.expression = expression
        self.kind = kind
        self.transition = transition
        self.result = result
        self.previous_result = previous_result
        self.error = error
        self.previous_error = previous_error

    def __repr__(self):
        return 'Diff(%r, kind=%r, transition=%r)' % (
            self.subscription, self.kind, self.transition)


class RoundStats(object):
    """Access statistics for the last completed correction round.

    ``accesses`` counts reads of records that were not modified in
    that round (reading cached records one by one, re-validating a
    key type or taking part in a ranking comparison; repeated reads
    count as well).  ``changes`` counts the records modified in the
    round.
    """

    def __init__(self, accesses=0, changes=0):
        self.accesses = accesses
        self.changes = changes

    def __repr__(self):
        return 'RoundStats(accesses=%r, changes=%r)' % (
            self.accesses, self.changes)


def _jtype(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, Number):
        return 'number'
    if isinstance(value, string_type):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'unknown'


def _is_false(value):
    return (value == '' or value == [] or value == {} or
            value is None or value is False)


def _rank_order(rank):
    # Total, deterministic order used only for aggregate maintenance.
    # Validation guarantees a homogeneous key type before any ranking
    # result is consumed, so the cross-type branches never leak into
    # a visible result.
    keytype = _jtype(rank)
    if keytype == 'number':
        return (0, rank)
    elif keytype == 'string':
        return (1, rank)
    return (2, 0)


class _Node(object):
    __slots__ = ('record', 'mid', 'live', 'key', 'keytype', 'prio',
                 'left', 'right', 'parent', 'size', 'nlive', 'nnum',
                 'nstr', 'nother', 'best_rank', 'best_pos')

    def __init__(self, record, mid, live, key_field, prio):
        self.record = record
        self.mid = mid
        self.live = live
        if key_field is None:
            self.key = None
            self.keytype = 'null'
        else:
            self.key = record.get(key_field)
            self.keytype = _jtype(self.key)
        self.prio = prio
        self.left = None
        self.right = None
        self.parent = None
        self.size = 1
        self.nlive = 1 if live else 0
        self.nnum = 0
        self.nstr = 0
        self.nother = 0
        if live:
            if self.keytype == 'number':
                self.nnum = 1
            elif self.keytype == 'string':
                self.nstr = 1
            else:
                self.nother = 1
        self.best_rank = self.key if live else None
        self.best_pos = 0 if live else -1


def _pull(node):
    left = node.left
    right = node.right
    left_size = left.size if left is not None else 0
    right_size = right.size if right is not None else 0
    node.size = left_size + 1 + right_size
    live = 1 if node.live else 0
    node.nlive = live
    node.nnum = 0
    node.nstr = 0
    node.nother = 0
    if node.live:
        if node.keytype == 'number':
            node.nnum = 1
        elif node.keytype == 'string':
            node.nstr = 1
        else:
            node.nother = 1
    best = None
    if left is not None:
        node.nlive += left.nlive
        node.nnum += left.nnum
        node.nstr += left.nstr
        node.nother += left.nother
        if left.nlive:
            best = (_rank_order(left.best_rank), left.best_pos,
                    left.best_rank)
    if node.live:
        candidate = (_rank_order(node.key), left_size, node.key)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if right is not None:
        node.nlive += right.nlive
        node.nnum += right.nnum
        node.nstr += right.nstr
        node.nother += right.nother
        if right.nlive:
            candidate = (_rank_order(right.best_rank),
                         left_size + 1 + right.best_pos, right.best_rank)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    if best is None:
        node.best_rank = None
        node.best_pos = -1
    else:
        node.best_rank = best[2]
        node.best_pos = best[1]


def _merge(left, right):
    if left is None:
        return right
    if right is None:
        return left
    if left.prio < right.prio:
        left.right = _merge(left.right, right)
        if left.right is not None:
            left.right.parent = left
        _pull(left)
        return left
    else:
        right.left = _merge(left, right.left)
        if right.left is not None:
            right.left.parent = right
        _pull(right)
        return right


def _split(node, count):
    # Split off the first ``count`` in-order nodes.
    if node is None:
        return None, None
    left_size = node.left.size if node.left is not None else 0
    if left_size >= count:
        left, right = _split(node.left, count)
        node.left = right
        if right is not None:
            right.parent = node
        _pull(node)
        if left is not None:
            left.parent = None
        node.parent = None
        return left, node
    else:
        left, right = _split(node.right, count - left_size - 1)
        node.right = left
        if left is not None:
            left.parent = node
        _pull(node)
        if right is not None:
            right.parent = None
        node.parent = None
        return node, right


class _Board(object):
    """One leaderboard (main or backup) as an implicit treap.

    Rows are kept in row-number order.  Subtree aggregates (live
    counts, per-type key counts, best live sort key) let the engine
    answer validation and page queries in O(log n) without walking
    the cached records one by one.
    """

    def __init__(self, name, source, touch, seed):
        self.name = name
        self.source = source
        self._touch = touch
        self._rand = random.Random(seed)
        self.root = None
        self.key_field = None
        self._index = {}
        self._version = 0
        self._validation = None
        self._validation_version = -1
        self._top_cache = {}
        self._top_version = -1

    @property
    def size(self):
        return self.root.size if self.root is not None else 0

    @property
    def live_count(self):
        return self.root.nlive if self.root is not None else 0

    def _bump(self):
        self._version += 1

    def _new_node(self, record, id_field, allow=None):
        if id_field not in record:
            raise ValueError('record is missing the %r field' % id_field)
        mid = record[id_field]
        if mid != allow and mid in self._index:
            raise ValueError('duplicate material id: %r' % (mid,))
        return _Node(record, mid, bool(record.get('live')),
                     self.key_field, self._rand.random())

    def load(self, records, id_field):
        for record in records:
            node = self._new_node(record, id_field)
            self._index[node.mid] = node
            self.root = _merge(self.root, node)
        if self.root is not None:
            self.root.parent = None
        self._bump()

    def use_key_field(self, field):
        if self.key_field == field:
            return
        if self.key_field is not None:
            raise UnsupportedExpressionError(
                'board %r is already sorted by %r, cannot also sort '
                'by %r' % (self.name, self.key_field, field))
        self.key_field = field
        for node in self._index.values():
            node.key = node.record.get(field)
            node.keytype = _jtype(node.key)
        self._repull(self.root)
        self._bump()

    def _repull(self, node):
        if node is None:
            return
        self._repull(node.left)
        self._repull(node.right)
        _pull(node)

    def _check_row(self, pos, allow_end=False):
        upper = self.size if allow_end else self.size - 1
        if pos < 0 or pos > upper:
            raise IndexError(
                'row number %r out of range for board %r with %r rows' % (
                    pos, self.name, self.size))

    def insert(self, pos, node):
        self._check_row(pos, allow_end=True)
        left, right = _split(self.root, pos)
        self.root = _merge(_merge(left, node), right)
        if self.root is not None:
            self.root.parent = None
        self._index[node.mid] = node
        self._bump()

    def delete(self, pos):
        self._check_row(pos)
        left, right = _split(self.root, pos)
        middle, right = _split(right, 1)
        self.root = _merge(left, right)
        if self.root is not None:
            self.root.parent = None
        del self._index[middle.mid]
        self._bump()
        return middle

    def replace(self, pos, node):
        self._check_row(pos)
        left, right = _split(self.root, pos)
        middle, right = _split(right, 1)
        self.root = _merge(_merge(left, node), right)
        if self.root is not None:
            self.root.parent = None
        del self._index[middle.mid]
        self._index[node.mid] = node
        self._bump()
        return middle

    def node_at(self, pos):
        self._check_row(pos)
        node = self.root
        while node is not None:
            left_size = node.left.size if node.left is not None else 0
            if pos < left_size:
                node = node.left
            elif pos == left_size:
                return node
            else:
                pos -= left_size + 1
                node = node.right
        raise IndexError(pos)

    def position_of(self, mid):
        node = self._index[mid]
        pos = node.left.size if node.left is not None else 0
        while node.parent is not None:
            if node.parent.right is node:
                parent_left = node.parent.left
                pos += (parent_left.size if parent_left is not None
                        else 0) + 1
            node = node.parent
        return pos

    def validate(self):
        """Mirror sort_by() key validation over the live rows.

        Returns ``None`` or a :class:`QueryError` describing the first
        failing row in row order, without walking the rows one by one.
        """
        if self._validation_version == self._version:
            return self._validation
        error = None
        if self.live_count:
            first = self._first_live()
            required = first.keytype
            if required not in _SORT_KEY_TYPES:
                error = self._key_error(first, ['string', 'number'])
            else:
                bad = self._first_mismatch(required)
                if bad is not None:
                    error = self._key_error(bad, [required])
        self._validation = error
        self._validation_version = self._version
        return error

    def _key_error(self, node, expected_types):
        return QueryError(
            board=self.name, source=self.source, material_id=node.mid,
            key=node.key, actual_type=node.keytype,
            expected_types=expected_types, function='sort_by')

    def _first_live(self):
        node = self.root
        while node is not None:
            if node.left is not None and node.left.nlive:
                node = node.left
            elif node.live:
                self._touch(node)
                return node
            else:
                node = node.right
        return None

    def _first_mismatch(self, required):
        node = self.root
        while node is not None:
            left = node.left
            if left is not None and self._mismatches(left, required):
                node = left
            elif node.live and node.keytype != required:
                self._touch(node)
                return node
            else:
                node = node.right
        return None

    @staticmethod
    def _mismatches(node, required):
        if required == 'number':
            return node.nstr + node.nother
        return node.nnum + node.nother

    def top(self, count):
        """The first ``count`` live rows in (rank, row order) order.

        Best-first descent over aggregated ranking segments; only the
        extracted rows and the segment boundary nodes are read.
        """
        if count <= 0 or self.live_count == 0:
            return []
        if self._top_version == self._version and count in self._top_cache:
            return self._top_cache[count]
        if self._top_version != self._version:
            self._top_cache = {}
            self._top_version = self._version
        result = []
        heap = []
        sequence = 0

        def push_segment(node, start):
            heapq.heappush(
                heap, (node.best_rank, start + node.best_pos, sequence,
                       False, node, start))

        def push_leaf(node, pos):
            self._touch(node)
            heapq.heappush(
                heap, (node.key, pos, sequence, True, node, pos))

        push_segment(self.root, 0)
        sequence += 1
        while heap and len(result) < count:
            _, _, _, is_leaf, node, start = heapq.heappop(heap)
            if is_leaf:
                self._touch(node)
                result.append(node)
                continue
            left = node.left
            left_size = left.size if left is not None else 0
            if left is not None and left.nlive:
                push_segment(left, start)
                sequence += 1
            if node.live:
                push_leaf(node, start + left_size)
                sequence += 1
            right = node.right
            if right is not None and right.nlive:
                push_segment(right, start + left_size + 1)
                sequence += 1
        self._top_cache[count] = result
        return result


class _LiveQueryError(Exception):
    def __init__(self, info):
        super(_LiveQueryError, self).__init__(str(info))
        self.info = info


class _V(object):
    """A jmespath value together with its provenance.

    ``origin`` is the stable material id the value was read from, or
    ``None`` when it does not come from a single material.  Array
    values keep their elements as ``_V`` so provenance is tracked per
    slot.
    """
    __slots__ = ('value', 'origin')

    def __init__(self, value, origin=None):
        self.value = value
        self.origin = origin


def _plain(value):
    if isinstance(value, _V):
        if isinstance(value.value, list):
            return [_plain(element) for element in value.value]
        return value.value
    return value


def _origins(value):
    if isinstance(value.value, list):
        return tuple(_origins(element) for element in value.value)
    return value.origin


class _RootRef(object):
    pass


class _ShardsRef(object):
    pass


class _RowsRef(object):
    pass


class _FlatRef(object):
    pass


class _BackupListRef(object):
    pass


class _BoardRef(object):
    def __init__(self, board, live_only):
        self.board = board
        self.live_only = live_only


class _SortedRef(object):
    def __init__(self, board):
        self.board = board


class _Interpreter(object):
    """Evaluates a subscription against the live boards.

    Board level sub-expressions stay lazy (``_BoardRef``/``_SortedRef``)
    so the 10k+ rows are never materialized; everything past the page
    slice is evaluated eagerly on page sized values.
    """

    def __init__(self, leaderboard):
        self._leaderboard = leaderboard
        self._functions = Functions()

    def evaluate(self, node):
        result = self._eval(node, _RootRef())
        if not isinstance(result, _V):
            raise UnsupportedExpressionError(
                'expression does not produce a materialized result')
        return result

    def _eval(self, node, value):
        method = getattr(self, '_eval_%s' % node['type'], None)
        if method is None:
            raise UnsupportedExpressionError(
                'cannot evaluate %r expressions incrementally'
                % node['type'])
        return method(node, value)

    def _eval_identity(self, node, value):
        return value

    def _eval_current(self, node, value):
        return value

    def _eval_literal(self, node, value):
        return _V(node['value'])

    def _eval_field(self, node, value):
        name = node['value']
        if isinstance(value, _RootRef):
            if name == 'shards':
                return _ShardsRef()
            elif name == 'backup':
                return _BackupListRef()
            return _V(None)
        if isinstance(value, _V):
            current = value.value
            if isinstance(current, dict):
                return _V(current.get(name), value.origin)
            return _V(None)
        raise UnsupportedExpressionError(
            'cannot read field %r here' % name)

    def _eval_flatten(self, node, value):
        value = self._eval(node['children'][0], value)
        if isinstance(value, _ShardsRef):
            return value
        if isinstance(value, _RowsRef):
            return _FlatRef()
        if isinstance(value, _V):
            base = value.value
            if not isinstance(base, list):
                return _V(None)
            merged = []
            for element in base:
                if isinstance(element, _V) and \
                        isinstance(element.value, list):
                    merged.extend(element.value)
                else:
                    merged.append(element)
            return _V(merged)
        raise UnsupportedExpressionError('cannot flatten here')

    def _eval_projection(self, node, value):
        left = self._eval(node['children'][0], value)
        right = node['children'][1]
        if isinstance(left, _ShardsRef) and right['type'] == 'field' \
                and right['value'] == 'rows':
            return _RowsRef()
        if isinstance(left, _FlatRef) and right['type'] == 'identity':
            return _BoardRef(MAIN_BOARD, live_only=False)
        if isinstance(left, _V):
            base = left.value
            if not isinstance(base, list):
                return _V(None)
            collected = []
            for element in base:
                current = self._eval(right, element)
                if _plain(current) is not None:
                    collected.append(current)
            return _V(collected)
        raise UnsupportedExpressionError('cannot project here')

    def _eval_filter_projection(self, node, value):
        base = self._eval(node['children'][0], value)
        right = node['children'][1]
        comparator = node['children'][2]
        is_live_filter = comparator['type'] == 'field' and \
            comparator['value'] == 'live'
        if is_live_filter and isinstance(base, _BackupListRef):
            return _BoardRef(BACKUP_BOARD, live_only=True)
        if is_live_filter and isinstance(base, _BoardRef) and \
                not base.live_only:
            return _BoardRef(base.board, live_only=True)
        if isinstance(base, _V):
            elements = base.value
            if not isinstance(elements, list):
                return _V(None)
            collected = []
            for element in elements:
                if not _is_false(_plain(self._eval(comparator, element))):
                    current = self._eval(right, element)
                    if _plain(current) is not None:
                        collected.append(current)
            return _V(collected)
        raise UnsupportedExpressionError('cannot filter here')

    def _eval_pipe(self, node, value):
        result = value
        for child in node['children']:
            result = self._eval(child, result)
        return result

    _eval_subexpression = _eval_pipe
    _eval_index_expression = _eval_pipe

    def _eval_index(self, node, value):
        index = node['value']
        if isinstance(value, _SortedRef):
            if index < 0:
                raise UnsupportedExpressionError(
                    'negative indexes are not supported on boards')
            board = self._leaderboard.board(value.board)
            nodes = board.top(index + 1)
            if index >= len(nodes):
                return _V(None)
            return _V(nodes[index].record, nodes[index].mid)
        if isinstance(value, _V):
            base = value.value
            if not isinstance(base, list):
                return _V(None)
            try:
                return base[index]
            except IndexError:
                return _V(None)
        raise UnsupportedExpressionError('cannot index here')

    def _eval_slice(self, node, value):
        start, stop, step = node['children']
        if step not in (None, 1):
            raise UnsupportedExpressionError(
                'slice steps are not supported on boards')
        if isinstance(value, _SortedRef):
            if (start is not None and start < 0) or \
                    (stop is not None and stop < 0):
                raise UnsupportedExpressionError(
                    'negative slices are not supported on boards')
            board = self._leaderboard.board(value.board)
            begin = start or 0
            count = board.live_count if stop is None else stop
            nodes = board.top(count)[begin:stop]
            return _V([_V(node.record, node.mid) for node in nodes])
        if isinstance(value, _V):
            base = value.value
            if not isinstance(base, list):
                return _V(None)
            return _V(base[slice(start, stop, step)])
        raise UnsupportedExpressionError('cannot slice here')

    def _eval_or_expression(self, node, value):
        matched = self._eval(node['children'][0], value)
        if _is_false(_plain(matched)):
            matched = self._eval(node['children'][1], value)
        return matched

    def _eval_and_expression(self, node, value):
        matched = self._eval(node['children'][0], value)
        if _is_false(_plain(matched)):
            return matched
        return self._eval(node['children'][1], value)

    def _eval_not_expression(self, node, value):
        original = _plain(self._eval(node['children'][0], value))
        if isinstance(original, Number) and not isinstance(original, bool) \
                and original == 0:
            return _V(False)
        return _V(not original)

    def _eval_comparator(self, node, value):
        left = _plain(self._eval(node['children'][0], value))
        right = _plain(self._eval(node['children'][1], value))
        comparator = node['value']
        if comparator == 'eq':
            return _V(_equals(left, right))
        elif comparator == 'ne':
            return _V(not _equals(left, right))
        if not (_is_comparable(left) and _is_comparable(right)):
            return _V(None)
        if comparator == 'lt':
            return _V(left < right)
        elif comparator == 'gt':
            return _V(left > right)
        elif comparator == 'lte':
            return _V(left <= right)
        else:
            return _V(left >= right)

    def _eval_function_expression(self, node, value):
        name = node['value']
        if name == 'sort_by':
            return self._sort_by(node, value)
        elif name == 'map':
            return self._map(node, value)
        elif name == 'not_null':
            # Like plain jmespath, every argument is evaluated first,
            # so an error in a later argument still surfaces.
            resolved = [self._eval(child, value)
                        for child in node['children']]
            for current in resolved:
                if _plain(current) is not None:
                    return current
            return _V(None)
        args = []
        for child in node['children']:
            current = self._eval(child, value)
            if not isinstance(current, _V):
                raise UnsupportedExpressionError(
                    'function %s() cannot read leaderboard rows directly'
                    % name)
            args.append(_plain(current))
        return _V(self._functions.call_function(name, args))

    def _sort_by(self, node, value):
        target = self._eval(node['children'][0], value)
        expref = node['children'][1]
        if expref['type'] != 'expref':
            raise UnsupportedExpressionError('sort_by needs an expref key')
        key_node = expref['children'][0]
        if isinstance(target, _BoardRef):
            if not target.live_only:
                raise UnsupportedExpressionError(
                    'sort_by is only supported on [?live] filtered boards')
            if key_node['type'] != 'field':
                raise UnsupportedExpressionError(
                    'sort_by keys on boards must be plain field references')
            board = self._leaderboard.board(target.board)
            board.use_key_field(key_node['value'])
            error = board.validate()
            if error is not None:
                raise _LiveQueryError(error)
            return _SortedRef(target.board)
        if isinstance(target, _V):
            base = target.value
            if not isinstance(base, list):
                raise exceptions.JMESPathTypeError(
                    'sort_by', _plain(target), _jtype(_plain(target)),
                    ['array'])
            if not base:
                return _V([])
            keys = [self._eval(key_node, element) for element in base]
            required = _jtype(_plain(keys[0]))
            if required not in _SORT_KEY_TYPES:
                raise exceptions.JMESPathTypeError(
                    'sort_by', _plain(keys[0]), required,
                    ['string', 'number'])
            for key in keys:
                if _jtype(_plain(key)) != required:
                    raise exceptions.JMESPathTypeError(
                        'sort_by', _plain(key), _jtype(_plain(key)),
                        [required])
            order = sorted(range(len(base)),
                           key=lambda i: _plain(keys[i]))
            return _V([base[i] for i in order])
        raise UnsupportedExpressionError('cannot sort here')

    def _map(self, node, value):
        expref = node['children'][0]
        target = self._eval(node['children'][1], value)
        if not isinstance(target, _V):
            raise UnsupportedExpressionError(
                'map() cannot read leaderboard rows directly')
        if not isinstance(target.value, list):
            raise exceptions.JMESPathTypeError(
                'map', _plain(target), _jtype(_plain(target)), ['array'])
        return _V([self._eval(expref['children'][0], element)
                   for element in target.value])


class Leaderboard(object):
    """A sharded supplier leaderboard with live JMESPath subscriptions.

    ``register`` reads the whole inventory once.  Afterwards rows are
    corrected by row number (``insert``/``replace``/``delete``), each
    record carrying a stable material id.  ``diff`` re-evaluates every
    subscription and reports how each result changed, and
    ``round_stats`` exposes how many unchanged records had to be read
    to do so.
    """

    def __init__(self, id_field='id'):
        self.id_field = id_field
        self._boards = {
            MAIN_BOARD: _Board(MAIN_BOARD, _BOARD_SOURCES[MAIN_BOARD],
                               self._touch, 0x6A1B),
            BACKUP_BOARD: _Board(BACKUP_BOARD, _BOARD_SOURCES[BACKUP_BOARD],
                                 self._touch, 0xBACC),
        }
        self._subscriptions = {}
        self._next_subscription = 0
        self._pending_changed = set()
        self._accesses = 0
        self.round_stats = RoundStats()

    def board(self, name):
        try:
            return self._boards[name]
        except KeyError:
            raise ValueError('unknown board %r, expected one of %s' % (
                name, sorted(self._boards)))

    def _touch(self, node):
        if node.mid not in self._pending_changed:
            self._accesses += 1

    def register(self, document):
        """Load the whole inventory.  This is the only full read."""
        main_rows = []
        for shard in document.get('shards', []):
            main_rows.extend(shard.get('rows', []))
        self._boards[MAIN_BOARD].load(
            [dict(record) for record in main_rows], self.id_field)
        self._boards[BACKUP_BOARD].load(
            [dict(record) for record in document.get('backup', [])],
            self.id_field)

    def subscribe(self, expression):
        """Subscribe a JMESPath expression and evaluate it once."""
        ast = Parser().parse(expression).parsed
        subscription = self._next_subscription
        self._next_subscription += 1
        state = {
            'expression': expression,
            'ast': ast,
            'state': None,
        }
        self._subscriptions[subscription] = state
        state['state'] = self._run(state)
        return subscription

    def result(self, subscription):
        """The current plain result, or ``None`` while it errors."""
        return self._subscriptions[subscription]['state'][1]

    def error(self, subscription):
        """The current :class:`QueryError`, or ``None`` while healthy."""
        return self._subscriptions[subscription]['state'][3]

    def insert(self, board, row, record):
        """Insert ``record`` so it ends up at row number ``row``."""
        target = self.board(board)
        node = target._new_node(dict(record), self.id_field)
        target.insert(row, node)
        self._pending_changed.add(node.mid)

    def delete(self, board, row=None, material_id=None):
        """Delete the row at ``row`` (or holding ``material_id``)."""
        target = self.board(board)
        pos = self._resolve(target, row, material_id)
        node = target.delete(pos)
        self._pending_changed.add(node.mid)
        return dict(node.record)

    def replace(self, board, row=None, record=None, material_id=None):
        """Replace the row at ``row`` (or holding ``material_id``).

        The replacement keeps the row number; its own material id may
        differ from the replaced one.
        """
        target = self.board(board)
        pos = self._resolve(target, row, material_id)
        node = target._new_node(dict(record), self.id_field,
                                allow=target.node_at(pos).mid)
        old = target.replace(pos, node)
        self._pending_changed.add(old.mid)
        self._pending_changed.add(node.mid)
        return dict(old.record)

    def _resolve(self, board, row, material_id):
        if row is not None:
            if material_id is not None and \
                    board.node_at(row).mid != material_id:
                raise ValueError(
                    'row %r on board %r does not hold material %r' % (
                        row, board.name, material_id))
            return row
        if material_id is None:
            raise ValueError('give a row number or a material id')
        try:
            return board.position_of(material_id)
        except KeyError:
            raise ValueError('unknown material id %r on board %r' % (
                material_id, board.name))

    def diff(self):
        """Evaluate all subscriptions and report the per-round diffs."""
        self._accesses = 0
        changes = len(self._pending_changed)
        diffs = {}
        for subscription, state in self._subscriptions.items():
            previous = state['state']
            current = self._run(state)
            diffs[subscription] = self._make_diff(
                subscription, state['expression'], previous, current)
            state['state'] = current
        self._pending_changed.clear()
        self.round_stats = RoundStats(accesses=self._accesses,
                                      changes=changes)
        return diffs

    def _run(self, state):
        interpreter = _Interpreter(self)
        try:
            value = interpreter.evaluate(state['ast'])
            return ('ok', _plain(value), _origins(value), None)
        except _LiveQueryError as error:
            return ('error', None, None, error.info)
        except exceptions.JMESPathTypeError as error:
            return ('error', None, None, QueryError(
                board=None, source=None, material_id=None,
                key=error.current_value, actual_type=error.actual_type,
                expected_types=error.expected_types,
                function=error.function_name))

    def _make_diff(self, subscription, expression, previous, current):
        old_status, old_plain, old_origins, old_error = previous
        new_status, new_plain, new_origins, new_error = current
        if new_status == 'error':
            kind = Diff.ERROR
        elif old_status == 'error':
            kind = Diff.RECOVERED
        elif old_plain != new_plain:
            kind = Diff.VALUE_CHANGED
        elif old_origins != new_origins:
            kind = Diff.SOURCE_CHANGED
        else:
            kind = Diff.UNCHANGED
        return Diff(
            subscription=subscription,
            expression=expression,
            kind=kind,
            transition='%s->%s' % (old_status, new_status),
            result=new_plain,
            previous_result=old_plain,
            error=new_error,
            previous_error=old_error,
        )
