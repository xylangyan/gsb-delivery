# -*- coding: UTF-8 -*-
"""
dtaidistance.dtw_rle
~~~~~~~~~~~~~~~~~~~~

Dynamic Time Warping for run-length encoded (RLE) series.

A run-length encoded series is a sequence of ``(value, count)`` pairs where
``value`` is repeated ``count`` times.  This module computes exactly the same
DTW distance as :func:`dtaidistance.dtw.distance` on the expanded series, but
it operates on the runs instead of on every individual sample.  This makes it
possible to compare series that contain constant regions of millions of
samples without materialising the expanded grid.

The implementation is a block-wise dynamic program.  Every pair of runs is a
rectangular block with a constant local cost ``c = (v1 - v2) ** 2``.  Inside
such a block the accumulated cost is the lower envelope of cones that are
anchored on the block's top and left borders.  Those envelopes are represented
as piecewise linear functions with integer (doubled) values, so the amount of
work depends on the number of linear pieces and not on the number of samples.

The result can be returned as a compact warping path: a list of vertices on
the original sample indices whose consecutive pairs are connected by a
horizontal, vertical, or 45-degree diagonal run.  No individual grid cell is
materialised for the constant regions.

:author: DTAI Research Group
:copyright: Copyright 2017-2026 KU Leuven, DTAI Research Group.
:license: Apache License, Version 2.0, see LICENSE for details.

"""
import bisect
import logging
import math
import numbers

logger = logging.getLogger("be.kuleuven.dtai.distance")

inf = float("inf")


def compress(series):
    """Compress a sequence of values into ``(value, count)`` runs.

    Adjacent equal values are merged.  The returned list uses the same
    convention as the rest of this module: ``[(value, count), ...]``.
    """
    runs = []
    for value in series:
        if runs and runs[-1][0] == value:
            runs[-1] = (value, runs[-1][1] + 1)
        else:
            runs.append((value, 1))
    return runs


def expand(runs):
    """Expand ``(value, count)`` runs into the original sequence.

    This is intended for short series and for verification.  It should not be
    used for the long constant regions this module is designed for.
    """
    series = []
    for value, count in _normalize_runs(runs):
        series.extend([value] * count)
    return series


class RunLengthSeries:
    """Container for a run-length encoded series.

    The container can be constructed from ``(value, count)`` pairs, from two
    parallel sequences of values and counts, or from a regular (expanded)
    sequence by passing ``expanded=True``.
    """

    def __init__(self, values, counts=None, expanded=False):
        if counts is None and not expanded:
            self.runs = _normalize_runs(values)
            self.values = [v for v, _ in self.runs]
            self.counts = [c for _, c in self.runs]
        elif counts is None:
            self.runs = compress(values)
            self.values = [v for v, _ in self.runs]
            self.counts = [c for _, c in self.runs]
        else:
            if len(values) != len(counts):
                raise ValueError("values and counts must have the same length")
            self.runs = _normalize_runs(list(zip(values, counts)))
            self.values = [v for v, _ in self.runs]
            self.counts = [c for _, c in self.runs]

    @classmethod
    def from_expanded(cls, series):
        return cls(series, expanded=True)

    @property
    def length(self):
        return sum(self.counts)

    def __len__(self):
        return self.length

    def __iter__(self):
        return iter(self.runs)

    def __getitem__(self, item):
        return self.runs[item]

    def expand(self):
        return expand(self.runs)

    def to_expanded(self):
        return self.expand()

    def distance(self, other, **kwargs):
        """DTW distance to another run-length encoded series."""
        return distance(self, other, **kwargs)

    def warping_path(self, other, **kwargs):
        """Compact optimal warping path to another run-length encoded series."""
        return warping_path(self, other, **kwargs)

    def distance_and_path(self, other, **kwargs):
        """Distance and compact optimal path to another RLE series."""
        return distance_and_path(self, other, **kwargs)

    def __repr__(self):
        return f"RunLengthSeries({self.runs})"


def _is_pair(value):
    if _is_number(value) or isinstance(value, (str, bytes)):
        return False
    try:
        return len(value) == 2
    except TypeError:
        return False


def _is_number(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _normalize_runs(series):
    """Normalize the accepted RLE input formats into ``(value, count)`` runs."""
    if isinstance(series, RunLengthSeries):
        return list(series.runs)
    if hasattr(series, "runs") and not isinstance(series, (list, tuple)):
        return _normalize_runs(series.runs)
    series = list(series)
    if not series:
        return []
    if all(_is_pair(item) for item in series):
        runs = []
        for value, count in series:
            if not _is_number(value):
                raise ValueError(f"RLE values must be numbers, got {value!r}")
            count = int(count)
            if count < 0:
                raise ValueError(f"RLE counts must be non-negative, got {count}")
            if count == 0:
                continue
            runs.append((value, count))
        return _merge_runs(runs)
    if all(_is_number(item) for item in series):
        return compress(series)
    raise ValueError(
        "Expected run-length encoded input as (value, count) pairs, "
        "a RunLengthSeries, or a regular sequence of numbers"
    )


def _merge_runs(runs):
    merged = []
    for value, count in runs:
        if merged and merged[-1][0] == value:
            merged[-1] = (value, merged[-1][1] + count)
        else:
            merged.append((value, count))
    return merged


def _band_cols(i, n, m, window):
    lo = max(0, i - max(0, n - m) - window + 1)
    hi = min(m, i + max(0, m - n) + window) - 1
    return lo, hi


def _band_rows(j, n, m, window):
    lo = max(0, j - max(0, m - n) - window + 1)
    hi = min(n, j + max(0, n - m) + window) - 1
    return lo, hi


def _clip_atoms(atoms, lo, hi):
    clipped = []
    for a, b, left, right in atoms:
        left = max(left, lo)
        right = min(right, hi)
        if left <= right:
            clipped.append((a, b, left, right))
    return clipped


def _line_atoms(x0, x1, alpha, beta, sign, bound, q, w_lo, w_hi):
    """Atoms of a source piece inside a constant-cost block.

    The source coordinate is ``x`` in ``[x0, x1]`` with ``K(x) = alpha*x+beta``.
    Its diagonal coordinate is ``sign*x``.  The source is valid for a target
    with diagonal coordinate ``w`` when ``x - sign*w <= bound`` (an infinite
    bound means unconstrained).

    The returned atoms are ``(a, b, lo, hi)`` line pieces over ``w`` such that
    the minimum of ``K(x) + q*|w - sign*x|`` is represented by their lower
    envelope.
    """
    atoms = []

    def add(a, b, lo, hi):
        lo = max(lo, w_lo)
        hi = min(hi, w_hi)
        if lo <= hi:
            atoms.append((a, b, lo, hi))

    if x0 > x1:
        return atoms
    neg_inf = -10 ** 60
    pos_inf = 10 ** 60
    k0 = alpha * x0 + beta
    k1 = alpha * x1 + beta

    if bound is None:
        range0 = (neg_inf, pos_inf)
    elif sign == 1:
        range0 = (x0 - bound, pos_inf)
    else:
        range0 = (neg_inf, bound - x0)
    merged = sign * x0
    add(-q, k0 + q * merged, range0[0], min(range0[1], merged))
    add(q, k0 - q * merged, max(range0[0], merged), range0[1])

    if bound is None:
        range1 = (neg_inf, pos_inf)
    elif sign == 1:
        range1 = (x1 - bound, pos_inf)
    else:
        range1 = (neg_inf, bound - x1)
    merged = sign * x1
    add(-q, k1 + q * merged, range1[0], min(range1[1], merged))
    add(q, k1 - q * merged, max(range1[0], merged), range1[1])

    if sign == 1:
        kink_lo, kink_hi = x0, x1
    else:
        kink_lo, kink_hi = -x1, -x0
    add(alpha * sign, beta, kink_lo, kink_hi)

    if bound is not None:
        if sign == 1:
            boundary_lo, boundary_hi = x0 - bound, x1 - bound
        else:
            boundary_lo, boundary_hi = bound - x1, bound - x0
        add(alpha * sign, alpha * bound + beta + q * bound,
            boundary_lo, boundary_hi)
    return atoms


def _envelope(atoms, w_lo, w_hi):
    """Lower envelope of line atoms over integer coordinates.

    Two lines can change order only where they intersect, so it is enough to
    cut the domain at all line intersections and at the ends of each atom's
    validity interval.  Within such an interval a single line is minimal.
    """
    if not atoms:
        return []

    # Merge atoms that share the same line before looking for intersections.
    by_line = {}
    for a, b, left, right in atoms:
        left = max(left, w_lo)
        right = min(right, w_hi)
        if left <= right:
            by_line.setdefault((a, b), []).append((left, right))
    if not by_line:
        return []
    merged_atoms = []
    for (a, b), intervals in by_line.items():
        intervals.sort()
        start, end = intervals[0]
        for left, right in intervals[1:]:
            if left <= end + 1:
                end = max(end, right)
            else:
                merged_atoms.append((a, b, start, end))
                start, end = left, right
        merged_atoms.append((a, b, start, end))

    candidates = {w_lo, w_hi + 1}
    for _, _, left, right in merged_atoms:
        candidates.add(left)
        candidates.add(right + 1)

    count = len(merged_atoms)
    for i in range(count):
        a1, b1, l1, r1 = merged_atoms[i]
        for j in range(i + 1, count):
            a2, b2, l2, r2 = merged_atoms[j]
            if a1 == a2:
                continue
            lo = max(l1, l2)
            hi = min(r1, r2)
            if lo > hi:
                continue
            num = b2 - b1
            den = a1 - a2
            cross = num // den
            if lo <= cross <= hi:
                candidates.add(cross)
                if cross + 1 <= hi:
                    candidates.add(cross + 1)

    coords = sorted(candidates)
    envelope = []
    for pos in range(len(coords) - 1):
        left = coords[pos]
        right = coords[pos + 1] - 1
        if left > right:
            continue
        best = None
        best_value = None
        for a, b, atom_lo, atom_hi in merged_atoms:
            if atom_lo <= left <= atom_hi:
                value = a * left + b
                if best_value is None or value < best_value:
                    best_value = value
                    best = (a, b)
        if best is None:
            continue
        a, b = best
        if envelope and envelope[-1][0] == a and envelope[-1][1] == b \
                and envelope[-1][3] + 1 == left:
            envelope[-1] = (a, b, envelope[-1][2], right)
        else:
            envelope.append((a, b, left, right))
    return envelope


def _block_bottom(top, left, c, h, wd, j0, i0, q):
    sources = []
    for a, b, lo, hi in top:
        x_lo = max(lo - j0 + 1, 0)
        x_hi = min(hi - j0 + 1, wd)
        if x_lo <= x_hi:
            sources.append((x_lo, x_hi, a - c,
                            a * (j0 - 1) + b + 2 * c, -1, h))
    for a, b, lo, hi in left:
        x_lo = max(lo - i0 + 1, 1)
        x_hi = min(hi - i0 + 1, h)
        if x_lo <= x_hi:
            sources.append((x_lo, x_hi, a - c,
                            a * (i0 - 1) + b + 2 * c, 1, None))
    w_lo = h - wd
    w_hi = h - 1
    atoms = []
    for x0, x1, alpha, beta, sign, bound in sources:
        atoms.extend(_line_atoms(x0, x1, alpha, beta, sign, bound, q,
                                 w_lo, w_hi))
    result = []
    for a, b, lo, hi in _envelope(atoms, w_lo, w_hi):
        slope = c - a
        intercept = (c + a) * (h - 1) + b - slope * j0
        result.append((slope, intercept, j0 + (h - 1 - hi),
                       j0 + (h - 1 - lo)))
    return result


def _block_right(top, left, c, h, wd, j0, i0, q):
    sources = []
    for a, b, lo, hi in top:
        x_lo = max(lo - j0 + 1, 0)
        x_hi = min(hi - j0 + 1, wd)
        if x_lo <= x_hi:
            sources.append((x_lo, x_hi, a - c,
                            a * (j0 - 1) + b + 2 * c, -1, None))
    for a, b, lo, hi in left:
        x_lo = max(lo - i0 + 1, 1)
        x_hi = min(hi - i0 + 1, h)
        if x_lo <= x_hi:
            sources.append((x_lo, x_hi, a - c,
                            a * (i0 - 1) + b + 2 * c, 1, wd))
    w_lo = -(wd - 1)
    w_hi = h - 1
    atoms = []
    for x0, x1, alpha, beta, sign, bound in sources:
        atoms.extend(_line_atoms(x0, x1, alpha, beta, sign, bound, q,
                                 w_lo, w_hi))
    result = []
    for a, b, lo, hi in _envelope(atoms, w_lo, w_hi):
        slope = c + a
        intercept = (c - a) * (wd - 1) + b - slope * i0
        result.append((slope, intercept, i0 + (lo + (wd - 1)),
                       i0 + (hi + (wd - 1))))
    return result


def _block_pieces(top, left, c, h, wd, j0, i0):
    pieces = []
    for a, b, lo, hi in top:
        x_lo = max(lo - j0 + 1, 0)
        x_hi = min(hi - j0 + 1, wd)
        if x_lo <= x_hi:
            pieces.append((x_lo, x_hi, a - c,
                           a * (j0 - 1) + b + 2 * c, -1))
    for a, b, lo, hi in left:
        x_lo = max(lo - i0 + 1, 1)
        x_hi = min(hi - i0 + 1, h)
        if x_lo <= x_hi:
            pieces.append((x_lo, x_hi, a - c,
                           a * (i0 - 1) + b + 2 * c, 1))
    return pieces


def _piece_min(x0, x1, alpha, beta, sign, u, valid_hi, q):
    hi = min(x1, valid_hi)
    if hi < x0:
        return None
    candidates = {x0, hi}
    kink = sign * u
    for x in (math.floor(kink), math.ceil(kink)):
        if x0 <= x <= hi:
            candidates.add(x)
    best = None
    for x in candidates:
        value = alpha * x + beta + q * abs(u - sign * x)
        if best is None or value < best[0]:
            best = (value, x)
    return best


def _block_argmin(pieces, i, j, q):
    u = i - j
    best = None
    for x0, x1, alpha, beta, sign in pieces:
        valid_hi = j + 1 if sign == -1 else i + 1
        candidate = _piece_min(x0, x1, alpha, beta, sign, u, valid_hi, q)
        if candidate is not None and (best is None or candidate[0] < best[0]):
            best = (candidate[0], candidate[1], sign)
    return best


def _staircase(si, sj, ti, tj):
    points = [(si, sj)]
    di = ti - si
    dj = tj - sj
    steps = min(di, dj)
    if steps > 0:
        points.append((si + steps, sj + steps))
        si += steps
        sj += steps
        di -= steps
        dj -= steps
    if di > 0:
        points.append((ti, sj))
    if dj > 0:
        points.append((ti, tj))
    return points


def _compact_path(points):
    compact = []
    for point in points:
        if compact and compact[-1] == point:
            continue
        compact.append(point)
    changed = True
    while changed:
        changed = False
        result = [compact[0]]
        idx = 1
        while idx < len(compact):
            if len(result) >= 2:
                first, second, third = result[-2], result[-1], compact[idx]
                d1 = (second[0] - first[0], second[1] - first[1])
                d2 = (third[0] - second[0], third[1] - second[1])
                if d1[0] * d2[1] == d1[1] * d2[0] \
                        and d1[0] * d2[0] >= 0 and d1[1] * d2[1] >= 0:
                    result[-1] = third
                    changed = True
                    idx += 1
                    continue
            result.append(compact[idx])
            idx += 1
        compact = result
    return compact


def _run_bounds(runs):
    bounds = []
    position = 0
    for value, count in runs:
        bounds.append((position, position + count - 1, value))
        position += count
    return bounds


def _check_options(window, penalty, max_length_diff, psi, inner_dist, use_c):
    if psi not in (None, 0):
        raise NotImplementedError("psi relaxation is not supported for RLE series")
    if inner_dist not in (None, "squared euclidean", 0):
        raise NotImplementedError(
            "Only the squared Euclidean inner distance is supported for RLE series")
    if use_c:
        raise NotImplementedError(
            "The C implementation does not support run-length encoded series")
    if penalty is None:
        penalty = 0
    # DTWSettings stores the penalty in distance space and transforms it to
    # cost space with inner_val (x*x for squared Euclidean).
    penalty = penalty * penalty
    if max_length_diff is None:
        max_length_diff = inf
    return window, penalty, max_length_diff


def _compute(r1, r2, window, penalty, max_length_diff, want_path):
    r1 = _normalize_runs(r1)
    r2 = _normalize_runs(r2)
    if not r1 and not r2:
        return (0.0, []) if want_path else (0.0, [])
    if not r1 or not r2:
        return (inf, None) if want_path else (inf, [])

    n = sum(count for _, count in r1)
    m = sum(count for _, count in r2)
    if abs(n - m) > max_length_diff:
        return (inf, None) if want_path else (inf, [])
    if window is None:
        window = max(n, m)

    bounds1 = _run_bounds(r1)
    bounds2 = _run_bounds(r2)
    previous_bottom = []
    previous_right = []
    blocks = {}
    for ai, (i0, i1, value1) in enumerate(bounds1):
        height = i1 - i0 + 1
        current_bottom = [None] * len(bounds2)
        current_right = [None] * len(bounds2)
        for bi, (j0, j1, value2) in enumerate(bounds2):
            width = j1 - j0 + 1
            cost = (value1 - value2) ** 2
            top = []
            left = current_right[bi - 1] if bi > 0 else []
            if ai > 0:
                top = list(previous_bottom[bi] or [])
                if bi > 0:
                    corner_atoms = [atom for atom in (previous_bottom[bi - 1] or [])
                                    if atom[2] <= j0 - 1 <= atom[3]]
                    if corner_atoms:
                        a, b, _, _ = corner_atoms[0]
                        top.append((a, b, j0 - 1, j0 - 1))
            elif bi == 0:
                # Phantom source at (-1, -1) with cost 0.  Together with the
                # constant block cost this represents starting at (0, 0).
                top = [(0, 0, -1, -1)]

            q = cost + 2 * penalty
            bottom = _block_bottom(top, left, cost, height, width,
                                   j0, i0, q)
            right = _block_right(top, left, cost, height, width,
                                 j0, i0, q)
            column_lo, column_hi = _band_cols(i1, n, m, window)
            row_lo, row_hi = _band_rows(j1, n, m, window)
            current_bottom[bi] = _clip_atoms(bottom, max(column_lo, j0),
                                             min(column_hi, j1))
            current_right[bi] = _clip_atoms(right, max(row_lo, i0),
                                            min(row_hi, i1))
            if want_path:
                blocks[(ai, bi)] = (
                    i0, i1, j0, j1, cost, q,
                    _block_pieces(top, left, cost, height, width, j0, i0)
                )
        previous_bottom = current_bottom
        previous_right = current_right

    final_atoms = previous_bottom[-1]
    doubled = min((a * (m - 1) + b for a, b, lo, hi in final_atoms
                   if lo <= m - 1 <= hi), default=None)
    if doubled is None:
        return (inf, None) if want_path else (inf, [])
    distance = math.sqrt(doubled / 2)
    if not want_path:
        return distance, []

    ends1 = [item[1] for item in bounds1]
    ends2 = [item[1] for item in bounds2]

    def find_block(i, j):
        return bisect.bisect_left(ends1, i), bisect.bisect_left(ends2, j)

    points = []
    i, j = n - 1, m - 1
    while i > 0 or j > 0:
        ai, bi = find_block(i, j)
        i0, i1, j0, j1, cost, q, pieces = blocks[(ai, bi)]
        best = _block_argmin(pieces, i - i0, j - j0, q)
        if best is None:
            raise RuntimeError(
                f"Unable to reconstruct warping path at sample ({i}, {j})")
        _, x, sign = best
        if sign == -1:
            source_i, source_j = i0 - 1, j0 + x - 1
        else:
            source_i, source_j = i0 + x - 1, j0 - 1
        if source_i < 0 or source_j < 0:
            source_i, source_j = 0, 0
        points.append((i, j))
        i, j = source_i, source_j
    points.append((0, 0))
    points.reverse()

    # Turn the per-block staircase endpoints into one ordered vertex list.
    vertices = [points[0]]
    for (si, sj), (ti, tj) in zip(points, points[1:]):
        vertices.extend(_staircase(si, sj, ti, tj)[1:])
    path = _compact_path(vertices)
    return distance, path


def distance(s1, s2, window=None, penalty=None, max_length_diff=None,
             psi=None, inner_dist=None, use_c=False):
    """DTW distance between two run-length encoded series.

    The return value is identical to :func:`dtaidistance.dtw.distance` on the
    expanded series, including the square root that belongs to the default
    squared Euclidean scoring.

    :param s1: First series as ``(value, count)`` runs.  A regular sequence of
        numbers is also accepted and will be compressed first.
    :param s2: Second series, same formats as ``s1``.
    :param window: Warping window with the same meaning as in
        :class:`dtaidistance.dtw.DTWSettings`.
    :param penalty: Penalty for a horizontal or vertical step.
    :param max_length_diff: Return infinity if the series lengths differ more
        than this amount.
    :param psi: Not supported; only the default (no relaxation) is available.
    :param inner_dist: Only ``'squared euclidean'`` is supported.
    :param use_c: Not supported for RLE series.
    :returns: The DTW distance, or ``inf`` when the window contains no complete
        alignment.
    """
    window, penalty, max_length_diff = _check_options(
        window, penalty, max_length_diff, psi, inner_dist, use_c)
    value, _ = _compute(s1, s2, window, penalty, max_length_diff, False)
    return value


def distance_cost(s1, s2, window=None, penalty=None, max_length_diff=None,
                  psi=None, inner_dist=None, use_c=False):
    """Accumulated squared cost, before the square root.

    This is the integer-valued score that the dynamic program minimises.
    """
    window, penalty, max_length_diff = _check_options(
        window, penalty, max_length_diff, psi, inner_dist, use_c)
    value, _ = _compute(s1, s2, window, penalty, max_length_diff, False)
    return value * value


def warping_path(s1, s2, window=None, penalty=None, max_length_diff=None,
                 psi=None, inner_dist=None, use_c=False):
    """Optimal warping path between two run-length encoded series.

    The path is returned as a list of ``(i, j)`` vertices on the original
    sample indices, from ``(0, 0)`` to ``(len(s1)-1, len(s2)-1)``.  Each pair of
    consecutive vertices is connected by a horizontal, vertical, or 45-degree
    diagonal run.  This compact form avoids listing every cell in long constant
    regions while still describing an actual cell-by-cell path.

    ``None`` is returned when the window does not contain a complete path.
    """
    window, penalty, max_length_diff = _check_options(
        window, penalty, max_length_diff, psi, inner_dist, use_c)
    _, path = _compute(s1, s2, window, penalty, max_length_diff, True)
    return path


def distance_and_path(s1, s2, window=None, penalty=None, max_length_diff=None,
                      psi=None, inner_dist=None, use_c=False):
    """Return both the DTW distance and a compact optimal warping path.

    If no complete path exists inside the window, the distance is ``inf`` and
    the path is ``None``.
    """
    window, penalty, max_length_diff = _check_options(
        window, penalty, max_length_diff, psi, inner_dist, use_c)
    return _compute(s1, s2, window, penalty, max_length_diff, True)


def warping_path_segments(s1, s2, **kwargs):
    """Return the optimal path as ``(i0, j0, i1, j1)`` segments.

    Each segment is horizontal, vertical, or diagonal.  This is a convenience
    wrapper around :func:`warping_path`.
    """
    path = warping_path(s1, s2, **kwargs)
    if path is None:
        return None
    return [(i0, j0, i1, j1) for (i0, j0), (i1, j1) in zip(path, path[1:])]


def warping_path_full(s1, s2, **kwargs):
    """Expand a compact path into every visited cell.

    This is intended for short series and for verification only.
    """
    path = warping_path(s1, s2, **kwargs)
    if path is None:
        return None
    full = [path[0]]
    for (i0, j0), (i1, j1) in zip(path, path[1:]):
        di = i1 - i0
        dj = j1 - j0
        if di == dj:
            step_i, step_j, steps = (1 if di > 0 else -1), (1 if dj > 0 else -1), abs(di)
        elif di == 0:
            step_i, step_j, steps = 0, (1 if dj > 0 else -1), abs(dj)
        elif dj == 0:
            step_i, step_j, steps = (1 if di > 0 else -1), 0, abs(di)
        else:
            raise ValueError(f"Invalid compact path segment {(i0, j0, i1, j1)}")
        i, j = full[-1]
        for _ in range(steps):
            i += step_i
            j += step_j
            full.append((i, j))
    return full


def path_cost(s1, s2, path, penalty=None):
    """Score a compact or full path with the squared-difference scoring.

    The compact path is evaluated segment by segment.  A segment is split only
    at run boundaries, so the cost of a path that crosses a constant region of
    a million samples is still computed in a small number of steps.

    :param path: A compact list of vertices or a full list of visited cells.
    :param penalty: Penalty for horizontal and vertical steps.
    :returns: The accumulated cost, including the penalty.
    """
    if penalty is None:
        penalty = 0
    penalty = penalty * penalty
    if path is None or not path:
        return inf
    runs1 = _normalize_runs(s1)
    runs2 = _normalize_runs(s2)
    bounds1 = _run_bounds(runs1)
    bounds2 = _run_bounds(runs2)
    starts1 = [item[0] for item in bounds1]
    starts2 = [item[0] for item in bounds2]

    def run_index(starts, idx):
        return bisect.bisect_right(starts, idx) - 1

    def value1(idx):
        return bounds1[run_index(starts1, idx)][2]

    def value2(idx):
        return bounds2[run_index(starts2, idx)][2]

    def end1(idx):
        return bounds1[run_index(starts1, idx)][1]

    def end2(idx):
        return bounds2[run_index(starts2, idx)][1]

    i, j = path[0]
    total = (value1(i) - value2(j)) ** 2
    for i1, j1 in path[1:]:
        di = i1 - i
        dj = j1 - j
        if di < 0 or dj < 0 or (di != 0 and dj != 0 and di != dj):
            raise ValueError(
                f"Invalid compact path segment ({i}, {j}) -> ({i1}, {j1})")
        steps = max(di, dj)
        axis = 1 if (di == 0 or dj == 0) else 0
        done = 0
        while done < steps:
            ci = i + di * (done + 1) // max(di, 1) if di else i
            cj = j + dj * (done + 1) // max(dj, 1) if dj else j
            chunk = steps - done
            if di:
                chunk = min(chunk, end1(ci) - ci + 1)
            if dj:
                chunk = min(chunk, end2(cj) - cj + 1)
            total += chunk * ((value1(ci) - value2(cj)) ** 2)
            if axis:
                total += chunk * penalty
            done += chunk
        i, j = i1, j1
    return total


def _path_cells(path):
    if not path:
        return []
    cells = [tuple(path[0])]
    for current, nxt in zip(path, path[1:]):
        i0, j0 = current
        i1, j1 = nxt
        di = i1 - i0
        dj = j1 - j0
        if di == dj:
            step_i = 1 if di > 0 else -1
            step_j = 1 if dj > 0 else -1
            steps = abs(di)
        elif di == 0:
            step_i, step_j, steps = 0, (1 if dj > 0 else -1), abs(dj)
        elif dj == 0:
            step_i, step_j, steps = (1 if di > 0 else -1), 0, abs(di)
        else:
            raise ValueError(
                f"Invalid compact path segment {current} -> {nxt}")
        i, j = cells[-1]
        for _ in range(steps):
            i += step_i
            j += step_j
            cells.append((i, j))
    return cells


# Explicit RLE aliases, useful when importing from the top-level package.
distance_rle = distance
warping_path_rle = warping_path
distance_cost_rle = distance_cost
distance_and_path_rle = distance_and_path
