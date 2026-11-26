"""Row-number lineage for :func:`toolz.join` results.

A join result only contains ``(left, right)`` pairs.  It does not say whether
a side was produced by ``left_default`` or ``right_default``, nor from which
input record it came.  That information is needed when a report recorded old
row numbers and a new report must be reconciled with it.

This module provides two layers:

``join_rows``
    A drop-in replacement for :func:`toolz.join` that yields
    :class:`JoinRow` objects.  The order is exactly the order of
    :func:`toolz.join`.

``JoinIndex``
    A compact, bidirectional index between an old and a new join result.
    It answers point and range questions without materialising a Cartesian
    product and without replaying either join for every query.  Rows are
    first matched by their exact pair; a pair that vanished is then followed
    through the real record on its non-default side.  The exact-pair query
    :meth:`JoinIndex.lookup_pair` reports a mismatch when the pair itself no
    longer occurs.

Example
-------
::

    from toolz import first, second, join_index

    index = join_index(
        (first, old_left, second, old_right, None, None),
        (first, new_left, second, new_right, None, None),
    )

    index.lookup(3)                  # new row number, or LOST
    index.range_counts(10, 20)       # same / moved / lost counts
    index.lookup_pair(left, right)   # row number of an old pair
"""

from bisect import bisect_left
from collections import deque, namedtuple

from .itertoolz import getter
from .utils import no_default


__all__ = (
    'join_rows',
    'join_index',
    'JoinIndex',
    'JoinRow',
    'JoinSpec',
    'LineageSegment',
    'RangeCounts',
    'NewRangeCounts',
    'LOST',
    'ADDED',
)


class JoinRow(tuple):
    """One join result row with its provenance.

    ``JoinRow`` is a two-tuple ``(left, right)`` so it unpacks and compares
    exactly like a row of :func:`toolz.join`.  The provenance is available as
    attributes.

    Parameters
    ----------
    left, right : object
        The values in the result row.  A side produced by a join default
        holds the default value itself.
    left_default, right_default : bool
        Whether that side was supplied by the join default rather than by an
        input sequence.  A real record that happens to equal the default
        value therefore remains distinguishable from a default-filled side.
    left_pos, right_pos : int or None
        Position of the real record in ``leftseq`` or ``rightseq``.  ``None``
        when the side came from a default.  Positions are stable when the
        input records and their order are unchanged, even if a record's join
        key was edited.
    """

    def __new__(
        cls,
        left,
        right,
        left_default=False,
        right_default=False,
        left_pos=None,
        right_pos=None,
    ):
        row = super().__new__(cls, (left, right))
        row.left_default = left_default
        row.right_default = right_default
        row.left_pos = left_pos
        row.right_pos = right_pos
        return row

    @property
    def left(self):
        return self[0]

    @property
    def right(self):
        return self[1]

    def __repr__(self):
        return (
            'JoinRow(%r, %r, left_default=%r, right_default=%r, '
            'left_pos=%r, right_pos=%r)'
            % (
                self.left,
                self.right,
                self.left_default,
                self.right_default,
                self.left_pos,
                self.right_pos,
            )
        )

    @property
    def is_left_default(self):
        return self.left_default

    @property
    def is_right_default(self):
        return self.right_default

    @property
    def is_empty(self):
        """True when either side came from a join default."""
        return self.left_default or self.right_default

    @property
    def left_origin(self):
        """``('default',)`` or ``('record', left_pos)``."""
        if self.left_default:
            return ('default',)
        return ('record', self.left_pos)

    @property
    def right_origin(self):
        """``('default',)`` or ``('record', right_pos)``."""
        if self.right_default:
            return ('default',)
        return ('record', self.right_pos)


class JoinSpec(
    namedtuple(
        'JoinSpec',
        'leftkey leftseq rightkey rightseq left_default right_default',
    )
):
    """The arguments of one version of a join.

    A four- or six-tuple is accepted anywhere a :class:`JoinSpec` is.
    """

    __slots__ = ()

    def __new__(
        cls,
        leftkey,
        leftseq,
        rightkey,
        rightseq,
        left_default=no_default,
        right_default=no_default,
    ):
        return super().__new__(
            cls,
            leftkey,
            leftseq,
            rightkey,
            rightseq,
            left_default,
            right_default,
        )


class _Missing:
    __slots__ = ('_name',)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return '<%s>' % self._name


LOST = _Missing('LOST')
"""Marker for an old row whose pair does not occur in the new result."""

ADDED = _Missing('ADDED')
"""Marker for a new row whose pair does not occur in the old result."""


LineageSegment = namedtuple('LineageSegment', 'start stop status offset')
"""A half-open range ``[start, stop)`` with constant status and offset.

``status`` is ``'same'``, ``'moved'``, ``'lost'`` for old-to-new segments and
``'same'``, ``'moved'``, ``'added'`` for new-to-old segments.  For a row that
exists at both versions, ``new_index - old_index`` is ``offset``.
"""


class RangeCounts(namedtuple('RangeCounts', 'total same moved lost')):
    """Counts of old rows by lineage status in a range."""

    __slots__ = ()


class NewRangeCounts(namedtuple('NewRangeCounts', 'total same moved added')):
    """Counts of new rows by lineage status in a range."""

    __slots__ = ()


class JoinIndex:
    """A bidirectional index between an old and a new join result.

    Parameters
    ----------
    old_rows, new_rows : iterable of JoinRow or pair
        Result rows of the old and the new joins.  :class:`JoinRow` instances
        carry default-origin flags and input positions.  Plain
        ``(left, right)`` pairs are also accepted; in that case defaults and
        real records with the default value cannot be told apart.
    left_id, right_id : callable or index, optional
        Identity of left and right records.  They may be non-callable index
        keys, as accepted by :func:`toolz.join`.
    by_position : bool, optional
        Match records by their position in the input sequences.  This is the
        default because the input records and their order are unchanged
        between versions; only some join-key fields were edited.  Positions
        are recorded by :func:`join_rows`.  For raw rows without positions,
        or when ``by_position=False``, records are matched by value (or by
        ``left_id``/``right_id`` when given).
    """

    def __init__(
        self,
        old_rows,
        new_rows,
        *,
        left_id=None,
        right_id=None,
        by_position=True,
    ):
        self._old_rows = [_as_joinrow(row) for row in old_rows]
        self._new_rows = [_as_joinrow(row) for row in new_rows]
        self._left_id = None if left_id is None else _as_getter(left_id)
        self._right_id = None if right_id is None else _as_getter(right_id)
        self._use_left_pos = by_position and left_id is None
        self._use_right_pos = by_position and right_id is None

        old_len = len(self._old_rows)
        new_len = len(self._new_rows)

        # First, match identical record pairs.  This preserves rows whose
        # left and right records both survived a key edit elsewhere.
        exact_new = {}
        for new_index, row in enumerate(self._new_rows):
            exact_new.setdefault(self._row_key(row), deque()).append(new_index)

        self._old_to_new = [LOST] * old_len
        self._new_to_old = [ADDED] * new_len
        used_new = [False] * new_len
        remaining_old = []
        for old_index, row in enumerate(self._old_rows):
            candidates = exact_new.get(self._row_key(row))
            if candidates:
                new_index = candidates.popleft()
                self._old_to_new[old_index] = new_index
                self._new_to_old[new_index] = old_index
                used_new[new_index] = True
            else:
                remaining_old.append(old_index)

        # A row whose exact pair vanished still follows the real record on
        # its non-default side.  The k-th old occurrence of that record maps
        # to its k-th new occurrence among the rows not matched above.  This
        # is why an edited right key can move early rows to the trailing
        # left-by-default block, while a later right row with the old key
        # supplies a genuine match and prevents that move.
        anchors = {}
        for new_index, row in enumerate(self._new_rows):
            if not used_new[new_index]:
                key = self._anchor_key(row)
                anchors.setdefault(key, []).append(new_index)

        used = {}
        for old_index in remaining_old:
            key = self._anchor_key(self._old_rows[old_index])
            occurrence = used.get(key, 0)
            used[key] = occurrence + 1
            candidates = anchors.get(key)
            if candidates and occurrence < len(candidates):
                new_index = candidates[occurrence]
                self._old_to_new[old_index] = new_index
                self._new_to_old[new_index] = old_index

        self._old_pair_positions = None
        self._new_pair_positions = None

        self._old_runs = _build_runs(self._old_to_new)
        self._new_runs = _build_runs(self._new_to_old)
        self._old_prefix, self._old_starts, self._old_status = _build_prefix(
            self._old_runs
        )
        self._new_prefix, self._new_starts, self._new_status = _build_prefix(
            self._new_runs
        )
        self._old_next_full = _next_full_rows(self._old_rows)
        self._new_next_full = _next_full_rows(self._new_rows)

    # ------------------------------------------------------------------
    # Alternate constructors
    # ------------------------------------------------------------------
    @classmethod
    def from_versions(
        cls, old, new, *, left_id=None, right_id=None, by_position=True
    ):
        """Build from two join specifications.

        Each specification may be a :class:`JoinSpec` or a four/six-tuple
        ``(leftkey, leftseq, rightkey, rightseq[, left_default,
        right_default])``.
        """
        old_spec = coerce_join_spec(old)
        new_spec = coerce_join_spec(new)
        return cls(
            join_rows(*old_spec),
            join_rows(*new_spec),
            left_id=left_id,
            right_id=right_id,
            by_position=by_position,
        )

    @classmethod
    def from_rows(
        cls,
        old_rows,
        new_rows,
        *,
        left_id=None,
        right_id=None,
        by_position=True,
    ):
        """Build from two already-computed row sequences."""
        return cls(
            old_rows,
            new_rows,
            left_id=left_id,
            right_id=right_id,
            by_position=by_position,
        )

    # ------------------------------------------------------------------
    # Shape
    # ------------------------------------------------------------------
    def __len__(self):
        return len(self._old_rows)

    def __repr__(self):
        return '<JoinIndex old=%d new=%d>' % (self.old_len, self.new_len)

    def __iter__(self):
        return iter(range(self.old_len))

    def __getitem__(self, old_index):
        """Return the new row number, or :data:`LOST` if the pair is gone."""
        return self.lookup(old_index)

    def get(self, old_index, default=None):
        """Like :meth:`lookup`, but return ``default`` for a lost pair."""
        new_index = self.lookup(old_index)
        return default if new_index is LOST else new_index

    @property
    def old_len(self):
        return len(self._old_rows)

    @property
    def new_len(self):
        return len(self._new_rows)

    @property
    def mapping(self):
        """A plain ``{old_row: new_row}`` dict for pairs that still exist."""
        return {
            old: new
            for old, new in enumerate(self._old_to_new)
            if new is not LOST
        }

    @property
    def reverse_mapping(self):
        """A plain ``{new_row: old_row}`` dict for pairs that existed."""
        return {
            new: old
            for new, old in enumerate(self._new_to_old)
            if old is not ADDED
        }

    # ------------------------------------------------------------------
    # Point lookups
    # ------------------------------------------------------------------
    def lookup(self, old_index):
        """Return the new row number for an old row, or :data:`LOST`."""
        return self._old_to_new[_normalize_index(old_index, self.old_len)]

    def old_of_new(self, new_index):
        """Return the old row number for a new row, or :data:`ADDED`."""
        return self._new_to_old[_normalize_index(new_index, self.new_len)]

    new_lookup = old_of_new
    lookup_new = old_of_new

    def row(self, old_index):
        """Return the :class:`JoinRow` at an old row number."""
        return self._old_rows[_normalize_index(old_index, self.old_len)]

    def new_row(self, new_index):
        """Return the :class:`JoinRow` at a new row number."""
        return self._new_rows[_normalize_index(new_index, self.new_len)]

    def records(self, old_index):
        """Return the old row's ``(left, right)`` values, defaults included."""
        row = self.row(old_index)
        return (row.left, row.right)

    def new_records(self, new_index):
        """Return the new row's ``(left, right)`` values, defaults included."""
        row = self.new_row(new_index)
        return (row.left, row.right)

    def source_records(self, old_index):
        """Return both real source records of an old row.

        Returns ``None`` when either side is a default.  Otherwise returns
        ``(left_record, right_record)`` from the original input sequences.
        """
        row = self.row(old_index)
        if row.is_empty:
            return None
        return (row.left, row.right)

    def new_source_records(self, new_index):
        """Like :meth:`source_records`, but for a new row number."""
        row = self.new_row(new_index)
        if row.is_empty:
            return None
        return (row.left, row.right)

    def lookup_pair(
        self,
        left,
        right,
        occurrence=0,
        left_default=False,
        right_default=False,
    ):
        """Return the new row number of an exact old ``(left, right)`` pair.

        This is the stricter "old pair still present" question: both record
        identities and both default-origin flags must match.  It returns
        :data:`LOST` when the pair no longer occurs, even if one of its
        records survived and its row moved for another reason.  Use
        :meth:`lookup` for the row-number lineage of an old row.

        ``occurrence`` chooses among duplicate pairs in old-result order: the
        k-th old occurrence is checked against the k-th new occurrence.
        Set ``left_default`` or ``right_default`` when that side was filled
        by a join default.
        """
        key = self._value_key_for(left, right, left_default, right_default)
        matches = self._old_pairs().get(key)
        if not matches or occurrence >= len(matches):
            return LOST
        new_matches = self._new_pairs().get(key)
        if not new_matches or occurrence >= len(new_matches):
            return LOST
        return new_matches[occurrence]

    def lookup_new_pair(
        self,
        left,
        right,
        occurrence=0,
        left_default=False,
        right_default=False,
    ):
        """Return the new row number of an exact ``(left, right)`` pair.

        Use this for pairs that first appear in the new result.  Set
        ``left_default`` or ``right_default`` when that side was filled by a
        join default.  Returns :data:`ADDED` when the pair does not occur
        there at all.  Use :meth:`old_of_new` to ask whether a given new row
        has an old counterpart.
        """
        key = self._value_key_for(left, right, left_default, right_default)
        matches = self._new_pairs().get(key)
        if not matches or occurrence >= len(matches):
            return ADDED
        return matches[occurrence]

    def pair_occurrences(
        self, left, right, left_default=False, right_default=False
    ):
        """Return how many times an exact old pair occurs."""
        key = self._value_key_for(left, right, left_default, right_default)
        return len(self._old_pairs().get(key, ()))

    def new_pair_occurrences(
        self, left, right, left_default=False, right_default=False
    ):
        """Return how many times an exact pair occurs in the new result."""
        key = self._value_key_for(left, right, left_default, right_default)
        return len(self._new_pairs().get(key, ()))

    # ------------------------------------------------------------------
    # Range queries
    # ------------------------------------------------------------------
    def range_counts(self, start=0, stop=None, step=1):
        """Count old rows in ``[start, stop)`` by lineage status.

        Returns :class:`RangeCounts` with ``same`` rows that stayed at their
        old number, ``moved`` rows that exist elsewhere in the new result,
        and ``lost`` rows whose old pair is gone.  ``step`` selects every
        step-th row, so evenly spaced row numbers can be checked as one
        range.
        """
        start, stop, step = _normalize_range(start, stop, self.old_len, step)
        rows = range(start, stop, step)
        if step == 1:
            before = _prefix_at(
                self._old_prefix, self._old_starts, self._old_status, start
            )
            after = _prefix_at(
                self._old_prefix, self._old_starts, self._old_status, stop
            )
            same = after[0] - before[0]
            moved = after[1] - before[1]
            lost = after[2] - before[2]
        else:
            same = moved = lost = 0
            for index in rows:
                target = self._old_to_new[index]
                if target is LOST:
                    lost += 1
                elif target == index:
                    same += 1
                else:
                    moved += 1
        return RangeCounts(len(rows), same, moved, lost)

    def new_range_counts(self, start=0, stop=None, step=1):
        """Count new rows in ``[start, stop)``.

        Returns :class:`NewRangeCounts` with ``same``, ``moved`` and
        ``added`` counts.  ``step`` selects every step-th row.
        """
        start, stop, step = _normalize_range(start, stop, self.new_len, step)
        rows = range(start, stop, step)
        if step == 1:
            before = _prefix_at(
                self._new_prefix, self._new_starts, self._new_status, start
            )
            after = _prefix_at(
                self._new_prefix, self._new_starts, self._new_status, stop
            )
            same = after[0] - before[0]
            moved = after[1] - before[1]
            added = after[2] - before[2]
        else:
            same = moved = added = 0
            for index in rows:
                source = self._new_to_old[index]
                if source is ADDED:
                    added += 1
                elif source == index:
                    same += 1
                else:
                    moved += 1
        return NewRangeCounts(len(rows), same, moved, added)

    def summary(self):
        """Whole-old-result :class:`RangeCounts`."""
        return self.range_counts()

    def new_summary(self):
        """Whole-new-result :class:`NewRangeCounts`."""
        return self.new_range_counts()

    def first_records(self, start=0, stop=None):
        """Both source records of the first full row in a range.

        A full row has a real record on both sides.  Returns ``None`` when
        the range has no full row.  This uses a precomputed next-full table,
        not a scan of the requested range.
        """
        start, stop, step = _normalize_range(start, stop, self.old_len)
        if start >= stop:
            return None
        index = self._old_next_full[start]
        if index is None or index >= stop:
            return None
        row = self._old_rows[index]
        return (row.left, row.right)

    def first_new_records(self, start=0, stop=None):
        """Like :meth:`first_records`, but over new row numbers."""
        start, stop, step = _normalize_range(start, stop, self.new_len)
        if start >= stop:
            return None
        index = self._new_next_full[start]
        if index is None or index >= stop:
            return None
        row = self._new_rows[index]
        return (row.left, row.right)

    # ------------------------------------------------------------------
    # Compressed ledgers
    # ------------------------------------------------------------------
    def segments(self):
        """Old-to-new lineage as compressed :class:`LineageSegment` runs.

        Adjacent rows with the same status and offset share one segment, so a
        region with a constant shift is stored once.  A small correction
        splits the segment exactly where it occurs.
        """
        return tuple(self._old_runs)

    def new_segments(self):
        """New-to-old lineage as compressed :class:`LineageSegment` runs."""
        return tuple(self._new_runs)

    def lost_rows(self):
        """Return old row numbers that have no pair in the new result."""
        return tuple(i for i, j in enumerate(self._old_to_new) if j is LOST)

    def added_rows(self):
        """Return new row numbers that have no pair in the old result."""
        return tuple(j for j, i in enumerate(self._new_to_old) if i is ADDED)

    # ------------------------------------------------------------------
    # Internal keys
    # ------------------------------------------------------------------
    def _old_pairs(self):
        if self._old_pair_positions is None:
            self._old_pair_positions = _positions_by_value(
                self._old_rows, self._left_id, self._right_id
            )
        return self._old_pair_positions

    def _new_pairs(self):
        if self._new_pair_positions is None:
            self._new_pair_positions = _positions_by_value(
                self._new_rows, self._left_id, self._right_id
            )
        return self._new_pair_positions

    def _row_key(self, row):
        return _pair_key(
            row.left,
            row.right,
            self._left_id,
            self._right_id,
            row.left_default,
            row.right_default,
            row.left_pos,
            row.right_pos,
            self._use_left_pos,
            self._use_right_pos,
        )

    def _value_key_for(self, left, right, left_default, right_default):
        return _pair_key(
            left,
            right,
            self._left_id,
            self._right_id,
            left_default,
            right_default,
        )

    def _anchor_key(self, row):
        if not row.left_default:
            return _record_key(
                row.left,
                'left',
                self._left_id,
                row.left_pos,
                self._use_left_pos,
            )
        return _record_key(
            row.right,
            'right',
            self._right_id,
            row.right_pos,
            self._use_right_pos,
        )


def join_rows(
    leftkey,
    leftseq,
    rightkey,
    rightseq,
    left_default=no_default,
    right_default=no_default,
):
    """Like :func:`toolz.join`, but yield :class:`JoinRow` objects.

    Row order, key equality, duplicate expansion, and the trailing
    unmatched-left block are exactly those of :func:`toolz.join`.
    """
    if not callable(leftkey):
        leftkey = getter(leftkey)
    if not callable(rightkey):
        rightkey = getter(rightkey)

    # Unlike ``groupby``, keep each record's position in the left input.
    groups = {}
    for left_pos, item in enumerate(leftseq):
        groups.setdefault(leftkey(item), []).append((item, left_pos))

    if left_default == no_default and right_default == no_default:
        # Inner join
        for right_pos, item in enumerate(rightseq):
            key = rightkey(item)
            if key in groups:
                for left, left_pos in groups[key]:
                    yield JoinRow(
                        left, item, False, False, left_pos, right_pos
                    )
    elif left_default != no_default and right_default == no_default:
        # Right join
        for right_pos, item in enumerate(rightseq):
            key = rightkey(item)
            if key in groups:
                for left, left_pos in groups[key]:
                    yield JoinRow(
                        left, item, False, False, left_pos, right_pos
                    )
            else:
                yield JoinRow(left_default, item, True, False, None, right_pos)
    elif right_default != no_default:
        seen_keys = set()
        seen = seen_keys.add

        if left_default == no_default:
            # Left join
            for right_pos, item in enumerate(rightseq):
                key = rightkey(item)
                seen(key)
                if key in groups:
                    for left, left_pos in groups[key]:
                        yield JoinRow(
                            left, item, False, False, left_pos, right_pos
                        )
        else:
            # Full join
            for right_pos, item in enumerate(rightseq):
                key = rightkey(item)
                seen(key)
                if key in groups:
                    for left, left_pos in groups[key]:
                        yield JoinRow(
                            left, item, False, False, left_pos, right_pos
                        )
                else:
                    yield JoinRow(
                        left_default, item, True, False, None, right_pos
                    )

        # Unmatched left groups, in first-group-creation order.
        for key, matches in groups.items():
            if key not in seen_keys:
                for left, left_pos in matches:
                    yield JoinRow(
                        left, right_default, False, True, left_pos, None
                    )


def join_index(old, new, *, left_id=None, right_id=None, by_position=True):
    """Build a :class:`JoinIndex` between two join versions.

    ``old`` and ``new`` are :class:`JoinSpec` values or four/six-tuples of
    join arguments.  See :class:`JoinIndex` for the query methods.
    """
    return JoinIndex.from_versions(
        old, new, left_id=left_id, right_id=right_id, by_position=by_position
    )


def coerce_join_spec(value):
    """Normalize a four/six-tuple or :class:`JoinSpec`."""
    if isinstance(value, JoinSpec):
        return value
    if isinstance(value, (tuple, list)) and len(value) in (4, 6):
        return JoinSpec(*value)
    raise TypeError('expected a JoinSpec or a 4/6 tuple of join arguments')


def _as_joinrow(row):
    if isinstance(row, JoinRow):
        return row
    try:
        count = len(row)
    except TypeError:
        raise TypeError('join rows must be JoinRow instances or pairs')
    if count == 2:
        return JoinRow(row[0], row[1])
    if count == 4:
        return JoinRow(*row)
    if count == 6:
        return JoinRow(*row)
    raise TypeError('join rows must have 2, 4 or 6 items')


def _as_getter(key):
    return key if callable(key) else getter(key)


def _record_key(value, side, identity, position, use_position):
    if use_position and position is not None:
        return (side, 'position', position)
    if identity is None:
        return (side, 'value', _identity_value(value))
    return (side, 'value', _identity_value(identity(value)))


def _pair_key(
    left,
    right,
    left_id,
    right_id,
    left_default,
    right_default,
    left_pos=None,
    right_pos=None,
    use_left_pos=False,
    use_right_pos=False,
):
    if left_default:
        left_key = ('left', 'default')
    else:
        left_key = _record_key(left, 'left', left_id, left_pos, use_left_pos)
    if right_default:
        right_key = ('right', 'default')
    else:
        right_key = _record_key(
            right, 'right', right_id, right_pos, use_right_pos
        )
    return (left_key, right_key)


def _positions_by_value(rows, left_id=None, right_id=None):
    positions = {}
    for index, row in enumerate(rows):
        key = _pair_key(
            row.left,
            row.right,
            left_id,
            right_id,
            row.left_default,
            row.right_default,
        )
        positions.setdefault(key, []).append(index)
    return positions


def _identity_value(value):
    """Return a hashable value that identifies a record by value.

    The type tag keeps values that compare equal but are not interchangeable,
    such as ``True`` and ``1``, in separate identity classes.
    """
    if isinstance(value, dict):
        items = [
            (_identity_value(k), _identity_value(v)) for k, v in value.items()
        ]
        items.sort(key=repr)
        return ('dict', tuple(items))
    if isinstance(value, (list, tuple)):
        return ('seq', tuple(_identity_value(v) for v in value))
    if isinstance(value, (set, frozenset)):
        return ('set', frozenset(_identity_value(v) for v in value))
    try:
        hash(value)
    except TypeError:
        raise TypeError(
            'record identity is not hashable; pass left_id/right_id'
        )
    return ('atom', type(value).__name__, value)


def _normalize_index(index, length):
    if index < 0:
        index += length
    if index < 0 or index >= length:
        raise IndexError('row index out of range')
    return index


def _normalize_range(start, stop, length, step=1):
    if isinstance(start, slice):
        if stop is not None or step != 1:
            raise TypeError('stop/step must not be given with a slice')
        start, stop, step = start.indices(length)
        if step == 0:
            raise ValueError('range step must not be zero')
        if step < 0:
            raise ValueError('range step must be positive')
        return start, stop, step
    if isinstance(start, (tuple, list)) and stop is None:
        if len(start) == 2:
            start, stop = start
        elif len(start) == 3:
            start, stop, step = start
        else:
            raise TypeError(
                'range must be (start, stop) or (start, stop, step)'
            )
    if stop is None:
        stop = length
    start, stop, step = slice(start, stop, step).indices(length)
    if step == 0:
        raise ValueError('range step must not be zero')
    if step < 0:
        raise ValueError('range step must be positive')
    return start, stop, step


def _build_runs(mapping):
    """Compress a row mapping into constant-status, constant-offset runs."""
    runs = []
    for index, target in enumerate(mapping):
        if target is LOST:
            status, offset = 'lost', None
        elif target is ADDED:
            status, offset = 'added', None
        elif target == index:
            status, offset = 'same', 0
        else:
            status, offset = 'moved', target - index

        if (
            runs
            and runs[-1].status == status
            and runs[-1].offset == offset
            and runs[-1].stop == index
        ):
            previous = runs[-1]
            runs[-1] = LineageSegment(
                previous.start, index + 1, status, offset
            )
        else:
            runs.append(LineageSegment(index, index + 1, status, offset))
    return runs


_SAME = 0
_MOVED = 1
_OTHER = 2


def _build_prefix(runs):
    """Cumulative (same, moved, other) counts at each run start."""
    prefix = [(0, 0, 0)]
    starts = []
    statuses = []
    for run in runs:
        starts.append(run.start)
        if run.status == 'same':
            statuses.append(_SAME)
        elif run.status == 'moved':
            statuses.append(_MOVED)
        else:
            statuses.append(_OTHER)
        same, moved, other = prefix[-1]
        length = run.stop - run.start
        if run.status == 'same':
            same += length
        elif run.status == 'moved':
            moved += length
        else:
            other += length
        prefix.append((same, moved, other))
    return prefix, starts, statuses


def _prefix_at(prefix, starts, statuses, index):
    """Return (same, moved, other) counts for rows before ``index``."""
    if index <= 0 or not starts:
        return (0, 0, 0)
    position = bisect_left(starts, index) - 1
    if position < 0:
        return (0, 0, 0)
    same, moved, other = prefix[position]
    extra = index - starts[position]
    if extra:
        status = statuses[position]
        if status == _SAME:
            same += extra
        elif status == _MOVED:
            moved += extra
        else:
            other += extra
    return (same, moved, other)


def _next_full_rows(rows):
    """For each index, the next index with real records on both sides."""
    result = [None] * (len(rows) + 1)
    next_full = None
    for index in range(len(rows) - 1, -1, -1):
        if not rows[index].is_empty:
            next_full = index
        result[index] = next_full
    return result
