# -*- coding: UTF-8 -*-
"""
dtaidistance.subsequence.subsequenceindex
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Indexed, interval-restricted DTW subsequence matching.

A :class:`SubsequenceIndex` stores a fixed query template and an archive
series.  It answers, for any inclusive archive interval ``[begin, end]``, the
best occurrence of the template that lies completely inside that interval.
The answer is the raw one-dimensional DTW distance with ``penalty=0`` and
squared-Euclidean inner distance, plus the matched archive start/end and a
best warping path.

The index is exact: it uses the same dynamic-programming recurrence as
:meth:`dtaidistance.dtw.warping_paths`.  It is not a heuristic or a
lower-bound filter.  Distances, starts, ends and paths can therefore be
checked directly against the stock implementation.

:author: Wannes Meert
:copyright: Copyright 2026 KU Leuven, DTAI Research Group.
:license: Apache License, Version 2.0, see LICENSE for details.

"""
import logging
import math

from .. import util_numpy


try:
    if util_numpy.test_without_numpy():
        raise ImportError()
    import numpy as np
except ImportError:  # pragma: no cover - the index requires NumPy
    np = None


logger = logging.getLogger("be.kuleuven.dtai.distance")

_DEFAULT_BLOCK_SIZE = 256
_INF = float("inf")


def _require_numpy():
    if np is None:
        raise util_numpy.NumpyException("Numpy is required for SubsequenceIndex")


def _as_float64_series(series, name="series"):
    """Return ``series`` as a contiguous one-dimensional float64 array.

    Integer archives are accepted.  The conversion to float64 is explicit
    because the index (and the stock DTW implementation) compare values in
    float64.  A two-dimensional input is not silently flattened.

    The result is always a copy.  ``update`` mutates the index's internal
    buffer, and it must never mutate an array owned by the caller.
    """
    _require_numpy()
    arr = np.asarray(series)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {arr.shape}")
    return np.array(arr, dtype=np.float64, order="C", copy=True)


def _as_sequence_numbers(sequence_numbers, length):
    """Validate/convert the optional original acquisition sequence numbers.

    Duplicate values are allowed: an archive can contain two records for the
    same acquisition timestamp.  The index always reads them in the order in
    which they were supplied; it never sorts them.
    """
    if sequence_numbers is None:
        return np.arange(length, dtype=np.int64)
    arr = np.asarray(sequence_numbers)
    if arr.ndim != 1:
        raise ValueError(
            "sequence_numbers must be one-dimensional, "
            f"got shape {arr.shape}"
        )
    if len(arr) != length:
        raise ValueError(
            f"sequence_numbers has length {len(arr)} but series has length {length}"
        )
    return np.array(arr, copy=True)


def _python_scalar(value):
    """Convert a NumPy scalar to a Python scalar, leaving other objects alone."""
    if np is not None and isinstance(value, np.generic):
        return value.item()
    return value


def _as_interval(length, begin=None, end=None):
    """Normalise an inclusive interval and clip it to ``[0, length-1]``."""
    if begin is None:
        begin = 0
    if end is None:
        end = length - 1
    begin = int(begin)
    end = int(end)
    if begin < 0:
        begin = 0
    if end > length - 1:
        end = length - 1
    return begin, end


class _BlockSummary:
    """Summary of the DTW dynamic programme over one contiguous archive block.

    The summary is deliberately independent of the block's absolute position.
    It can be combined with another summary by :meth:`combine`, and the
    combination is associative.  A segment tree therefore only needs to
    rebuild O(log n) summaries after a point correction.

    All costs are in the DTW "cost" space: squared differences plus step
    penalties.  The public distance is ``sqrt`` of the best cost.  ``penalty``
    is fixed at zero by the public API, but the recurrence keeps the term so
    the summary can be reused by future settings without changing the algebra.

    Notation (``m`` query points, ``d = m + 1`` DP rows, ``B`` block columns):

    ``T[i, k]``
        Best cost after the block when row ``i`` is the current DP row and the
        block was entered in row ``k``.  Row ``0`` is the free "haven't matched
        a query point yet" row.  Entering in row ``k > 0`` means row ``k`` was
        active at the left edge of the block; this carries a path across a
        block boundary.  ``T`` does not allow a fresh start inside the block.

    ``E[k]`` / ``E_end[k]``
        Best cost of a path that entered in row ``k`` and whose endpoint is
        the last query row ``k = m``, together with the first (earliest)
        block column where that cost was reached.

    ``F[i]`` / ``F_start[i]``
        Best cost after the block for a fresh path that started inside the
        block and whose current DP row is ``i``.  ``F_start`` is the earliest
        block column at which the path started while obtaining ``F[i]``.

    ``G`` / ``G_end`` / ``G_start``
        Best complete fresh match inside the block (all ``m`` query points
        matched), with the tie rule distance, then earliest end, then earliest
        start.  Columns and starts are block-relative.
    """

    __slots__ = ("T", "E", "E_end", "F", "F_start", "G", "G_end", "G_start", "B")

    def __init__(self, T, E, E_end, F, F_start, G, G_end, G_start, B):
        self.T = T
        self.E = E
        self.E_end = E_end
        self.F = F
        self.F_start = F_start
        self.G = G
        self.G_end = G_end
        self.G_start = G_start
        self.B = B

    def __repr__(self):  # pragma: no cover - debugging helper
        return (f"_BlockSummary(B={self.B}, G={self.G}, "
                f"G_end={self.G_end}, G_start={self.G_start})")


def _empty_summary(nrows):
    """Summary of an empty block.  Its existence simplifies tree padding."""
    d = nrows + 1
    return _BlockSummary(
        T=np.full((d, d), _INF),
        E=np.full(d, _INF),
        E_end=np.full(d, -1, dtype=np.int64),
        F=np.full(d, _INF),
        F_start=np.full(d, -1, dtype=np.int64),
        G=_INF,
        G_end=-1,
        G_start=-1,
        B=0,
    )


def _prefix_min_with_first(values):
    """Running minima along the last axis, and the first index attaining each.

    ``values`` has shape ``(..., B)``.  The returned ``first`` array contains
    the *earliest* index of the running minimum.  This is exactly the start
    position needed for the "earlier start wins an exact-distance tie" rule.
    """
    running = np.minimum.accumulate(values, axis=-1)
    before = np.empty_like(values)
    before[..., 0] = _INF
    before[..., 1:] = running[..., :-1]
    is_new_min = values < before
    first = np.maximum.accumulate(
        np.where(is_new_min, np.arange(values.shape[-1], dtype=np.int64), -1),
        axis=-1,
    )
    return running, first


def _summary_for_block(query, series, begin, end):
    """Build the summary for ``series[begin:end+1]``.

    The implementation is vectorised over the block's columns.  For each DP
    row it computes

    ``D[i, c] = cost[i, c] + min(D[i-1, c-1], D[i-1, c], D[i, c-1])``

    where the horizontal predecessor is evaluated from left to right.  After
    subtracting the prefix sum of the row's costs, the horizontal recurrence
    becomes a running minimum, so one NumPy cumulative minimum handles a whole
    row.  Work per block is O(m^2 * B) in the worst-case DP sense but is done
    in a small number of NumPy operations rather than Python-level loops over
    archive points.
    """
    q = query
    x = series
    m = len(q)
    d = m + 1
    B = end - begin + 1

    # cost[i, c] is the squared difference between query point i and the
    # archive point at block-relative column c.
    cost = (q[:, None] - x[None, begin:end + 1]) ** 2

    # ------------------------------------------------------------------
    # Inherited paths: a path is already in row k at the left edge.
    #
    # We keep one state vector per k.  ``prev[k, r]`` is the DP value in
    # row r after the previous column.  Row 0 is reset to zero at every
    # column: the DP may not consume archive points before the first query
    # point has been matched.  This is what makes a block summary
    # independent of the absolute position of the block.
    # ------------------------------------------------------------------
    T = np.full((d, d), _INF)
    E = np.full(d, _INF)
    E_end = np.full(d, -1, dtype=np.int64)

    prev = np.full((d, B + 1), _INF)
    prev[0, 0] = 0.0
    for r in range(1, m + 1):
        prefix = np.empty(B + 1)
        prefix[0] = 0.0
        prefix[1:] = np.cumsum(cost[r - 1])
        prefix_prev = prefix[:-1]

        init = np.full(d, _INF)
        init[r] = 0.0

        # A: no horizontal step before entering this block column.
        # Y: invoke the horizontal step zero or more times.
        a = np.minimum(prev[:, 1:], prev[:, :-1]) - prefix_prev[None, :]
        y = np.concatenate([init[:, None], a], axis=1)
        y = np.minimum.accumulate(y, axis=1)
        cur = y + prefix[None, :]

        T[r, :] = cur[:, B]
        if r == m:
            values = cur[:, 1:]
            ends = np.argmin(values, axis=1)
            E[:] = values[np.arange(d), ends]
            E_end[:] = ends
        prev = cur

    # ------------------------------------------------------------------
    # Fresh paths: a match may start at any archive point in the block.
    #
    # Row 0 is free.  For importance the "diagonal" from row 0 to row 1 is
    # the start of a match, not an inherited state, so it is handled by the
    # separate row-1 formula below.  ``F_start`` tracks the earliest start
    # achieving each row's cost.
    # ------------------------------------------------------------------
    F = np.full(d, _INF)
    F_start = np.full(d, -1, dtype=np.int64)
    F[0] = 0.0
    F_start[0] = B - 1

    G = _INF
    G_end = -1
    G_start = -1

    prev = np.zeros(B + 1)
    prev_start = np.full(B + 1, -1, dtype=np.int64)
    for r in range(1, m + 1):
        prefix = np.empty(B + 1)
        prefix[0] = 0.0
        prefix[1:] = np.cumsum(cost[r - 1])
        prefix_prev = prefix[:-1]

        if r == 1:
            # Both diagonal and vertical predecessors are the free row 0.
            a = -prefix_prev
            a_start = np.arange(B, dtype=np.int64)
        else:
            vertical = prev[1:]
            horizontal = prev[:-1]
            a = np.minimum(vertical, horizontal) - prefix_prev
            v_start = prev_start[1:]
            h_start = prev_start[:-1]
            a_start = np.where(
                vertical < horizontal,
                v_start,
                np.where(horizontal < vertical, h_start,
                         np.minimum(v_start, h_start)),
            )

        # The first candidate is the empty prefix (only a vertical step from
        # row r-1 is possible before the first horizontal step of this row).
        y = np.concatenate([np.array([_INF]), a])
        y = np.minimum.accumulate(y)

        before = np.empty(B)
        before[0] = _INF
        before[1:] = y[1:B]
        is_new_min = a < before
        first = np.maximum.accumulate(
            np.where(is_new_min, np.arange(B, dtype=np.int64), -1)
        )

        cur = y + prefix
        cur_start = np.concatenate(
            [np.array([-1], dtype=np.int64), a_start[first]]
        )

        if r == m:
            values = cur[1:]
            end = int(np.argmin(values))
            G = float(values[end])
            G_end = end
            G_start = int(cur_start[1 + end])
        F[r] = cur[B]
        F_start[r] = cur_start[B]
        prev = cur
        prev_start = cur_start

    return _BlockSummary(T, E, E_end, F, F_start, G, G_end, G_start, B)


def _combine_summaries(left, right):
    """Combine two adjacent block summaries.

    ``left`` covers an earlier range than ``right``.  The result describes the
    concatenation and has the same meaning as a directly built summary.
    """
    if left.B == 0:
        return right
    if right.B == 0:
        return left
    d = left.T.shape[0]
    BL = left.B

    # A path that was inherited at the left edge and leaves in row i either
    # passed through row 0 (the only row where a fresh start is allowed) or
    # continued through the usual three DP predecessors.  In matrix form the
    # inherited transfer is the min-plus product of right's T with left's T,
    # except that entering right in row 0 means a fresh start at right's
    # first column.  That case is represented by right's row 0 of T, which
    # is itself the free row and therefore correctly absorbs the fresh start.
    T = np.min(right.T[:, :, None] + left.T[None, :, :], axis=1)

    E = left.E.copy()
    E_end = left.E_end.copy()
    for k in range(d):
        candidate = left.T[:, k] + right.E
        for t in range(d):
            if right.E_end[t] < 0:
                continue
            cost = candidate[t]
            if cost == _INF:
                continue
            end = BL + int(right.E_end[t])
            if cost < E[k] or (cost == E[k] and (E_end[k] < 0 or end < E_end[k])):
                E[k] = cost
                E_end[k] = end

    # Fresh paths either start in the right block, or start in the left block
    # and cross the boundary.  Crossing in row 0 is not a real match (no query
    # point has been matched yet), so only rows 1..m are transferred.
    F = right.F.copy()
    F_start = right.F_start + BL
    for i in range(d):
        for t in range(1, d):
            cost = left.F[t] + right.T[i, t]
            if cost == _INF:
                continue
            start = int(left.F_start[t])
            if cost < F[i] or (cost == F[i] and start >= 0 and
                               (F_start[i] < 0 or start < F_start[i])):
                F[i] = cost
                F_start[i] = start

    G = left.G
    G_end = left.G_end
    G_start = left.G_start
    if right.G != _INF and right.G_end >= 0:
        if right.G < G or (right.G == G and
                           (G_end < 0 or BL + right.G_end < G_end)) or \
                (right.G == G and BL + right.G_end == G_end and
                 (G_start < 0 or BL + right.G_start < G_start)):
            G = right.G
            G_end = BL + right.G_end
            G_start = BL + right.G_start

    # A match that starts in the left block and ends in the right block.
    for t in range(1, d):
        if right.E_end[t] < 0:
            continue
        cost = left.F[t] + right.E[t]
        if cost == _INF:
            continue
        end = BL + int(right.E_end[t])
        start = int(left.F_start[t])
        if cost < G or (cost == G and (G_end < 0 or
                                       (end < G_end or
                                        (end == G_end and start < G_start)))):
            G = cost
            G_end = end
            G_start = start

    return _BlockSummary(T, E, E_end, F, F_start, G, G_end, G_start, BL + right.B)


def _cost_to_distance(cost, query_length):
    """Convert the index's stored cost to the public DTW distance."""
    if cost == _INF:
        return _INF
    return math.sqrt(cost)


class IntervalMatch:
    """Result of an interval query.

    Attributes
    ----------
    distance:
        Raw DTW distance (not divided by the query length), matching
        ``SAMatch.distance`` in the existing subsequence API.
    value:
        ``distance / len(query)``, matching the existing normalised value.
    begin, end:
        Inclusive archive indices of the matched subsequence.  ``end`` is the
        archive index of the last matched point.
    sequence_begin, sequence_end:
        The original acquisition sequence numbers at ``begin`` and ``end``.
        They are taken from the index's ``sequence_numbers`` track, in supplied
        order, so duplicate sequence numbers are preserved.
    start, stop:
        Python-slice style aliases: ``start == begin`` and
        ``stop == end + 1``.
    cost:
        The squared-cost value before the final square root.
    """

    __slots__ = ("index", "_distance", "_begin", "_end", "_path")

    def __init__(self, index, distance, begin, end):
        self.index = index
        self._distance = distance
        self._begin = begin
        self._end = end
        self._path = None

    @property
    def distance(self):
        return self._distance

    @property
    def value(self):
        return self._distance / len(self.index._query)

    @property
    def begin(self):
        return self._begin

    @property
    def end(self):
        return self._end

    @property
    def sequence_begin(self):
        if self._end < self._begin:
            return None
        return _python_scalar(self.index.sequence_numbers[self._begin])

    @property
    def sequence_end(self):
        if self._end < self._begin:
            return None
        return _python_scalar(self.index.sequence_numbers[self._end])

    @property
    def original_begin(self):
        """Alias for :attr:`sequence_begin`."""
        return self.sequence_begin

    @property
    def original_end(self):
        """Alias for :attr:`sequence_end`."""
        return self.sequence_end

    @property
    def start(self):
        return self._begin

    @property
    def stop(self):
        return self._end + 1

    @property
    def segment(self):
        """Inclusive ``[begin, end]``, mirroring ``SAMatch.segment``."""
        return [self._begin, self._end]

    @property
    def cost(self):
        return self._distance * self._distance

    @property
    def path(self):
        """Best warping path as ``(query_idx, archive_idx)`` pairs.

        The path is materialised lazily.  Only the matched archive interval
        is evaluated, so the work is proportional to the path (and matched
        window) length, not to the length of the archive or the index.
        """
        if self._path is None:
            self._path = self.index._best_path(self._begin, self._end)
        return self._path

    def __str__(self):
        return (f"IntervalMatch(begin={self._begin}, end={self._end}, "
                f"distance={self._distance})")

    __repr__ = __str__


class SubsequenceIndex:
    """Exact interval-query index for a fixed query template.

    Parameters
    ----------
    query:
        Template time series.  Must be one-dimensional.  At most 32 points
        are supported (this is a template, not an archive).
    series:
        Archive series to index.  One-dimensional; stored as float64.  Integer
        input (including signed 16-bit counts) is accepted.
    block_size:
        Number of archive points in a leaf block.  Larger blocks make queries
        faster and updates more expensive.  The default is a good compromise
        for template sizes up to 32.

    Notes
    -----
    The index size is O(number_of_blocks * m^2), where ``m`` is the fixed
    template length.  Because ``m <= 32`` is a constant, this is linear in the
    number of archived points.

    The query recurrence is the unconstrained one-dimensional DTW recurrence
    with ``penalty=0`` and squared-Euclidean local cost, restricted so that
    every path point lies inside the requested interval.  A path may not
    borrow a point from outside the interval.  In particular, the index does
    not reuse a full-archive path that happens to enter the interval through
    its left boundary; the first path point must be inside the interval.
    """

    MAX_QUERY_LENGTH = 32

    def __init__(self, query, series, block_size=_DEFAULT_BLOCK_SIZE,
                 sequence_numbers=None):
        _require_numpy()
        q = _as_float64_series(query, "query")
        x = _as_float64_series(series, "series")
        if len(q) == 0:
            raise ValueError("query must not be empty")
        if len(q) > self.MAX_QUERY_LENGTH:
            raise ValueError(
                f"query length {len(q)} exceeds the fixed template limit "
                f"({self.MAX_QUERY_LENGTH})"
            )
        if len(x) == 0:
            raise ValueError("series must not be empty")
        block_size = int(block_size)
        if block_size < 1:
            raise ValueError("block_size must be at least 1")

        self._query = q
        self.series = x
        self.sequence_numbers = _as_sequence_numbers(sequence_numbers, len(x))
        self.block_size = block_size
        self._m = len(q)
        self._d = self._m + 1

        nblocks = (len(x) + block_size - 1) // block_size
        # A power-of-two leaf count makes a compact, simple segment tree.
        size = 1
        while size < nblocks:
            size *= 2
        self._nblocks = nblocks
        self._size = size
        self._tree = [_empty_summary(self._m) for _ in range(2 * size)]

        for b in range(nblocks):
            begin = b * block_size
            end = min(len(x) - 1, begin + block_size - 1)
            self._tree[size + b] = _summary_for_block(q, x, begin, end)
        for node in range(size - 1, 0, -1):
            self._tree[node] = _combine_summaries(
                self._tree[2 * node], self._tree[2 * node + 1]
            )

    # ------------------------------------------------------------------
    # Tree maintenance
    # ------------------------------------------------------------------
    def _set_leaf(self, block, summary):
        node = self._size + block
        self._tree[node] = summary
        node //= 2
        while node:
            self._tree[node] = _combine_summaries(
                self._tree[2 * node], self._tree[2 * node + 1]
            )
            node //= 2

    def _update_point(self, position, series):
        block = position // self.block_size
        begin = block * self.block_size
        end = min(len(series) - 1, begin + self.block_size - 1)
        summary = _summary_for_block(self._query, series, begin, end)
        self._set_leaf(block, summary)

    def update(self, position, value, *, sequence_number=None):
        """Correct one archived point, in place.

        Parameters
        ----------
        position:
            Archive index to change.  ``0 <= position < len(series)``.
        value:
            New value.  Converted to float64.
        sequence_number:
            Optional replacement original acquisition sequence number for this
            position.  Omitting it leaves the stored track unchanged.

        Only the leaf block containing ``position`` is recomputed; the
        segment tree is then repaired along one root path.  This is
        O(block_size * m^2 + log(number_of_blocks) * m^3) and independent of
        the total archive length.
        """
        _require_numpy()
        position = int(position)
        length = len(self.series)
        if position < 0 or position >= length:
            raise IndexError(
                f"position {position} out of range for series of length {length}"
            )
        self.series[position] = np.float64(value)
        if sequence_number is not None:
            self.sequence_numbers[position] = sequence_number
        self._update_point(position, self.series)

    # Alias kept for callers that describe the operation as a correction.
    correct = update

    def correct_sequence_number(self, position, sequence_number):
        """Change only an archived point's original acquisition number.

        Sequence numbers are query metadata (they are returned with matches);
        they do not participate in the DTW recurrence, so this operation does
        not touch the index tree.
        """
        position = int(position)
        if position < 0 or position >= len(self.series):
            raise IndexError(
                f"position {position} out of range for series of length "
                f"{len(self.series)}"
            )
        self.sequence_numbers[position] = sequence_number

    def set_series(self, series):
        """Replace the whole archive and rebuild the index in O(n) points."""
        new_series = _as_float64_series(series, "series")
        if len(new_series) != len(self.series):
            raise ValueError(
                "set_series only supports same-length replacements; "
                "construct a new SubsequenceIndex for a different length"
            )
        self.series = new_series
        for b in range(self._nblocks):
            begin = b * self.block_size
            end = min(len(new_series) - 1, begin + self.block_size - 1)
            self._tree[self._size + b] = _summary_for_block(
                self._query, new_series, begin, end
            )
        for node in range(self._size - 1, 0, -1):
            self._tree[node] = _combine_summaries(
                self._tree[2 * node], self._tree[2 * node + 1]
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _range_summary(self, begin, end):
        """Combine the summaries covering ``[begin, end]``.

        Partial first/last blocks are materialised directly.  Full interior
        blocks are collected from the segment tree with an iterative range
        query, so the number of combines is O(log n) and independent of the
        requested interval length.  At most two partial blocks are evaluated;
        each such evaluation costs O(block_size), which is a fixed index
        parameter rather than a function of the requested interval length.
        """
        b0 = begin // self.block_size
        b1 = end // self.block_size
        if b0 == b1:
            return _summary_for_block(self._query, self.series, begin, end)

        parts = []
        first_block_begin = b0 * self.block_size
        first_block_end = first_block_begin + self.block_size - 1
        last_block_begin = b1 * self.block_size

        if begin == first_block_begin:
            interior_lo = b0
        else:
            parts.append(_summary_for_block(
                self._query, self.series, begin, first_block_end
            ))
            interior_lo = b0 + 1

        if end == last_block_begin + self.block_size - 1:
            interior_hi = b1
        else:
            interior_hi = b1 - 1

        if interior_lo <= interior_hi:
            # Iterative segment-tree range query over complete leaf blocks.
            left = self._size + interior_lo
            right = self._size + interior_hi + 1
            left_parts = []
            right_parts = []
            while left < right:
                if left & 1:
                    left_parts.append(self._tree[left])
                    left += 1
                if right & 1:
                    right -= 1
                    right_parts.append(self._tree[right])
                left //= 2
                right //= 2
            parts.extend(left_parts)
            parts.extend(reversed(right_parts))

        if interior_hi < b1:
            parts.append(_summary_for_block(
                self._query, self.series, last_block_begin, end
            ))

        summary = parts[0]
        for part in parts[1:]:
            summary = _combine_summaries(summary, part)
        return summary

    def query(self, begin=None, end=None, *, stop=None):
        """Best template match inside the inclusive interval ``[begin, end]``.

        Args:
            begin: Inclusive archive index.  Defaults to 0.
            end: Inclusive archive index.  Defaults to ``len(series) - 1``.
            stop: Optional exclusive end alias.  ``stop=k`` is equivalent to
                ``end=k-1``.  Supplying both ``end`` and ``stop`` is an error.

        Returns:
            :class:`IntervalMatch`.  If the interval cannot hold the template
            (for example an empty interval), the result has infinite distance
            and an empty path.

        Ties are resolved deterministically: smallest distance, then earliest
        end, then earliest start.
        """
        length = len(self.series)
        if stop is not None:
            if end is not None:
                raise TypeError("pass either end or stop, not both")
            end = stop - 1
        begin, end = _as_interval(length, begin, end)
        if begin > end:
            return IntervalMatch(self, _INF, begin, begin - 1)
        summary = self._range_summary(begin, end)
        if summary.G == _INF or summary.G_end < 0:
            return IntervalMatch(self, _INF, begin, begin - 1)
        match_begin = begin + summary.G_start
        match_end = begin + summary.G_end
        return IntervalMatch(self, _cost_to_distance(summary.G, self._m),
                             match_begin, match_end)

    def best_match(self, begin=None, end=None, *, stop=None):
        """Alias for :meth:`query`."""
        return self.query(begin=begin, end=end, stop=stop)

    def find(self, begin=None, end=None, *, stop=None):
        """Alias for :meth:`query`."""
        return self.query(begin=begin, end=end, stop=stop)

    # ------------------------------------------------------------------
    # Path materialisation
    # ------------------------------------------------------------------
    def _best_path(self, begin, end):
        """Recompute the optimal path inside ``[begin, end]``.

        The summary already gives the optimal end and start.  Re-running the
        DP on exactly the matched window is deliberate: it keeps the index
        memory small and makes the returned path independently checkable
        against the stock implementation.  Work is proportional to the
        matched window length times the query length.
        """
        m = self._m
        n = end - begin + 1
        if n <= 0:
            return []
        x = self.series[begin:end + 1]
        q = self._query
        d = m + 1
        # Same recurrence as _summary_for_block, but retain the full matrix
        # and the start position of every cell's best predecessor.
        cost = (q[:, None] - x[None, :]) ** 2
        W = np.full((d, n + 1), _INF)
        W[0, :] = 0.0
        start = np.full((d, n + 1), -1, dtype=np.int64)

        for col in range(1, n + 1):
            c = cost[:, col - 1]
            for row in range(1, d):
                diagonal = W[row - 1, col - 1]
                vertical = W[row - 1, col]
                horizontal = W[row, col - 1]
                if row == 1:
                    # A diagonal or vertical step from row 0 starts at this
                    # archive column.  The horizontal predecessor already
                    # carries its own start.
                    best = min(diagonal, vertical)
                    best_start = col - 1
                    if horizontal < best or (horizontal == best and
                                             start[row, col - 1] >= 0 and
                                             start[row, col - 1] < best_start):
                        best = horizontal
                        best_start = int(start[row, col - 1])
                else:
                    best = diagonal
                    best_start = int(start[row - 1, col - 1])
                    if vertical < best or (vertical == best and
                                           start[row - 1, col] >= 0 and
                                           start[row - 1, col] < best_start):
                        best = vertical
                        best_start = int(start[row - 1, col])
                    if horizontal < best or (horizontal == best and
                                             start[row, col - 1] >= 0 and
                                             start[row, col - 1] < best_start):
                        best = horizontal
                        best_start = int(start[row, col - 1])
                W[row, col] = c[row - 1] + best
                start[row, col] = best_start

        # The summary's tie rule is distance, then earliest end, then earliest
        # start.  The chosen end is the last column of the matched window;
        # this is guaranteed by query(), since IntervalMatch ends at the chosen
        # archive end.  Walk deterministic ties towards the earliest start.
        path = []
        row, col = m, n
        while row > 0 and col > 0:
            path.append((row - 1, begin + col - 1))
            diagonal = W[row - 1, col - 1]
            vertical = W[row - 1, col]
            horizontal = W[row, col - 1]
            best = min(diagonal, vertical, horizontal)
            chosen = None
            # Prefer the predecessor whose stored start matches this cell's
            # start, then use a stable preference order (diagonal, vertical,
            # horizontal).  This mirrors dtw.best_path's preference.
            target_start = int(start[row, col])
            for _kind, value, predecessor in (
                (0, diagonal, (row - 1, col - 1)),
                (1, vertical, (row - 1, col)),
                (2, horizontal, (row, col - 1)),
            ):
                if value == best:
                    pstart = int(start[predecessor])
                    if pstart == target_start:
                        chosen = predecessor
                        break
            if chosen is None:
                if diagonal == best:
                    chosen = (row - 1, col - 1)
                elif vertical == best:
                    chosen = (row - 1, col)
                else:
                    chosen = (row, col - 1)
            row, col = chosen
        path.reverse()
        return path

    def __len__(self):
        return len(self.series)

    def __repr__(self):  # pragma: no cover - debugging helper
        return (f"SubsequenceIndex(query_length={self._m}, "
                f"series_length={len(self.series)}, "
                f"block_size={self.block_size})")


def interval_subsequence_index(query, series, block_size=_DEFAULT_BLOCK_SIZE,
                               sequence_numbers=None):
    """Convenience constructor for :class:`SubsequenceIndex`."""
    return SubsequenceIndex(query, series, block_size=block_size,
                            sequence_numbers=sequence_numbers)


def subsequence_index(query, series, block_size=_DEFAULT_BLOCK_SIZE,
                      sequence_numbers=None):
    """Convenience constructor for :class:`SubsequenceIndex`.

    Named by analogy with the existing :func:`subsequence_alignment` and
    :func:`subsequence_search` entry points.
    """
    return SubsequenceIndex(query, series, block_size=block_size,
                            sequence_numbers=sequence_numbers)


# Common descriptive aliases.  These keep the API discoverable without
# committing callers to one particular noun.
SubsequenceIntervalIndex = SubsequenceIndex
IntervalSubsequenceIndex = SubsequenceIndex
SubsequenceIntervalMatch = IntervalMatch
