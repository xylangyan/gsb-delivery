# -*- coding: UTF-8 -*-
"""
dtaidistance.dtw_joint
~~~~~~~~~~~~~~~~~~~~~~

Joint, anchor-constrained DTW for two or more independently sampled channels.

A bench trial can produce a vibration curve and a current curve that are
sampled by different instruments.  The calibration pulses written by both
instruments belong to the same physical events.  Aligning every channel on
its own can therefore produce local warping paths that do not describe the
same trial: a pulse that one instrument missed can turn one real shift into
two separate matches.

This module aligns all channels against one shared, ordered set of *anchor
groups*.  An anchor group represents one physical pulse (or another verified
synchronisation event) and may contain one or more legal positions in every
channel.  A solution visits exactly one position of every group that is
observable in a channel.  Between consecutive groups each channel is an
ordinary DTW problem.

The local cost is the squared difference of the two sample values.  The
channel cost is the sum of those squared differences along the channel's
warping path.  The joint cost is the unweighted sum of the channel costs::

    joint cost = cost_vibration + cost_current

Without anchor groups, the optimiser reduces exactly to one independent DTW
per channel, and ``channel_cost == dtw.distance(...) ** 2``.  The
:meth:`JointAlignment.release` method performs that check explicitly.

The implementation does not build a product grid over the channels.  It runs
one two-dimensional DTW segment per channel at a time and keeps only the
segment costs needed for the anchor dynamic program.  Anchors are expected to
be sparse, as they are in a normal bench test.  Very long inter-anchor
segments are reconstructed with a divide-and-conquer DTW that uses O(length)
working memory instead of a full path matrix.

:author: DTAI Research Group
:copyright: Copyright 2017-2026 KU Leuven, DTAI Research Group.
:license: Apache License, Version 2.0, see LICENSE for details.

"""
import array
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("be.kuleuven.dtai.distance")

inf = float("inf")

# A position is (index in the first series, index in the second series).
Point = Tuple[int, int]


class JointAlignmentError(ValueError):
    """Base class for invalid or unsatisfiable joint-alignment requests."""

    def __init__(self, message, groups=None):
        super().__init__(message)
        self.groups = list(groups) if groups is not None else []

    @property
    def anchors(self):
        """Alias for :attr:`groups`."""
        return self.groups


class InfeasibleJointAlignment(JointAlignmentError):
    """No alignment satisfies all anchor groups.

    :attr:`groups` identifies the markers that cannot be satisfied together.
    A caller can report those identifiers instead of silently returning two
    unrelated high-similarity local alignments.
    """

    def __init__(self, message, groups=None, channel=None, details=None):
        super().__init__(message, groups=groups)
        self.channel = channel
        self.details = details


class UnknownAnchorError(JointAlignmentError):
    """An anchor refers to an unknown channel or an invalid sample index."""


@dataclass
class AnchorPlacement:
    """The position selected for one anchor group in one channel."""

    group: Any
    channel: str
    first: int
    second: int

    @property
    def point(self):
        return self.first, self.second

    @property
    def index(self):
        """Alias emphasising the position rather than the coordinate pair."""
        return self.point

    def __iter__(self):
        return iter(self.point)


@dataclass
class ChannelAlignment:
    """Alignment result for a single channel."""

    channel: str
    cost: float
    path: List[Point]
    anchors: List[AnchorPlacement] = field(default_factory=list)

    @property
    def squared_distance(self):
        """Cumulative squared-difference cost along the path."""
        return self.cost

    @property
    def distance(self):
        """Square root of :attr:`squared_distance`, as in ``dtw.distance``."""
        return math.sqrt(self.cost) if self.cost != inf else inf

    @property
    def first_indices(self):
        """Original sample numbers used from the first (new) series."""
        return _unique_in_order(point[0] for point in self.path)

    @property
    def second_indices(self):
        """Original sample numbers used from the second (control) series."""
        return _unique_in_order(point[1] for point in self.path)

    @property
    def observation_points(self):
        """Distinct original observation pairs used by the path."""
        return _unique_in_order(self.path)

    def event_position(self, group):
        """Selected position of ``group`` in this channel, or ``None``."""
        for placement in self.anchors:
            if placement.group == group:
                return placement.point
        return None


@dataclass
class JointAlignment:
    """Result of :func:`joint_alignment`.

    ``cost`` is in squared-difference units.  ``distance`` is its square root,
    matching the convention of :func:`dtaidistance.dtw.distance`.
    """

    cost: float
    channels: Dict[str, ChannelAlignment]
    anchor_order: List[Any] = field(default_factory=list)
    feasible: bool = True
    conflicts: List[Any] = field(default_factory=list)
    message: str = ""
    _series: Optional[Dict[str, Tuple[Sequence, Sequence]]] = field(
        default=None, repr=False, compare=False)

    @property
    def squared_distance(self):
        """Joint cost in squared-difference units."""
        return self.cost

    @property
    def distance(self):
        """Square root of the joint squared cost."""
        return math.sqrt(self.cost) if self.cost != inf else inf

    @property
    def channel_costs(self):
        """Per-channel squared costs."""
        return {name: alignment.cost for name, alignment in self.channels.items()}

    @property
    def channel_distances(self):
        """Per-channel DTW distances (square roots)."""
        return {name: alignment.distance for name, alignment in self.channels.items()}

    @property
    def paths(self):
        """Full warping path per channel, as lists of index pairs."""
        return {name: alignment.path for name, alignment in self.channels.items()}

    @property
    def anchors(self):
        """Selected anchor placements per channel."""
        return {name: alignment.anchors for name, alignment in self.channels.items()}

    @property
    def anchor_map(self):
        """Mapping from group id to the selected point in every channel."""
        result = {}
        for name, alignment in self.channels.items():
            for placement in alignment.anchors:
                result.setdefault(placement.group, {})[name] = placement.point
        return result

    @property
    def sample_indices(self):
        """Original first/second sample numbers used, per channel."""
        return {name: (alignment.first_indices, alignment.second_indices)
                for name, alignment in self.channels.items()}

    def path(self, channel):
        return self.channels[channel].path

    def channel(self, channel):
        return self.channels[channel]

    def event_positions(self, group):
        """Return where one physical event landed in every channel.

        Channels in which the event was not observed have no entry.  For a
        missed pulse this is the honest answer: the other channel still fixes
        the event, but this instrument did not record a marker for it.
        """
        return self.anchor_map.get(group, {})

    def release(self):
        """Recompute every channel without the shared anchor constraint.

        Returns a mapping ``channel -> squared DTW distance``.  Each value is
        the square of the ordinary :func:`dtaidistance.dtw.distance` result
        for that channel's two series.
        """
        return {name: _unconstrained_cost(a, b)
                for name, (a, b) in self._require_series().items()}

    def released_alignment(self):
        """Return an unconstrained :class:`JointAlignment`.

        This is useful for comparing the joint optimum with the independent
        per-channel DTW baselines.
        """
        channels = {}
        series = self._require_series()
        for name, (a, b) in series.items():
            cost, path = _segment_path(a, b, None, (len(a) - 1, len(b) - 1))
            channels[name] = ChannelAlignment(name, float(cost), path, [])
        total = sum(alignment.cost for alignment in channels.values())
        return JointAlignment(total, channels, anchor_order=[], feasible=True,
                              message="anchor constraint released",
                              _series=series)

    def without_anchors(self):
        """Alias for :meth:`released_alignment`."""
        return self.released_alignment()

    def _require_series(self):
        if self._series is None:
            raise ValueError("The original series are not attached to this result")
        return self._series

    def __getitem__(self, channel):
        return self.channels[channel]


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


class _RawAnchor:
    __slots__ = ("id", "points", "order", "buffer")

    def __init__(self, ident, points, order=None, buffer=None):
        self.id = ident
        self.points = points  # channel -> list[Point]
        self.order = order
        self.buffer = buffer


def _is_pair(value):
    return (isinstance(value, (tuple, list)) and len(value) == 2
            and not isinstance(value[0], (tuple, list, dict, Mapping)))


def _as_point(value):
    if isinstance(value, Mapping):
        for keys in (("first", "second"), ("a", "b"), ("i", "j"),
                     ("row", "col"), ("new", "control")):
            if keys[0] in value and keys[1] in value:
                return int(value[keys[0]]), int(value[keys[1]])
        if "point" in value:
            return _as_point(value["point"])
        if "index" in value:
            idx = value["index"]
            if isinstance(idx, (tuple, list)) and len(idx) == 2:
                return int(idx[0]), int(idx[1])
    if _is_pair(value):
        return int(value[0]), int(value[1])
    raise ValueError("Cannot interpret anchor position {!r} as an index pair".format(value))


def _normalise_series_pair(value):
    if isinstance(value, Mapping):
        for keys in (("first", "second"), ("new", "control"), ("a", "b"),
                     ("test", "reference")):
            if keys[0] in value and keys[1] in value:
                return value[keys[0]], value[keys[1]]
        if "series" in value and _is_pair(value["series"]):
            return value["series"][0], value["series"][1]
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    raise ValueError("Each channel must be a pair (first_series, second_series)")


def _normalise_inputs(s1, s2=None, channels=None):
    """Return an ordered mapping channel -> (first_series, second_series)."""
    if channels is not None:
        if isinstance(channels, Mapping):
            return {name: _normalise_series_pair(value)
                    for name, value in channels.items()}
        names = list(channels)
        if isinstance(s1, Mapping) and isinstance(s2, Mapping):
            return {name: (s1[name], s2[name]) for name in names}
        if isinstance(s1, Mapping) and s2 is None:
            return {name: _normalise_series_pair(s1[name]) for name in names}
        if (isinstance(s1, (tuple, list)) and isinstance(s2, (tuple, list))
                and len(s1) == len(names) and len(s2) == len(names)):
            return {name: (s1[index], s2[index]) for index, name in enumerate(names)}
        if isinstance(s1, Mapping) and isinstance(s2, Mapping):
            return {name: (s1[name], s2[name]) for name in names}
        raise TypeError("Cannot interpret positional series with channels={}".format(names))

    if s2 is None:
        if not isinstance(s1, Mapping):
            raise TypeError("When only one argument is given it must map channels "
                            "to (first_series, second_series) pairs")
        return {name: _normalise_series_pair(value) for name, value in s1.items()}

    if isinstance(s1, Mapping) and isinstance(s2, Mapping):
        names = list(s1.keys())
        if set(names) != set(s2.keys()):
            raise ValueError("Both channel mappings must contain the same channel names")
        return {name: (s1[name], s2[name]) for name in names}

    raise TypeError("s1 and s2 must both be channel mappings; for positional series "
                    "pass a channel mapping or use channels=[...]")


def _normalise_channel_value(value, channel, group_id):
    if value is None:
        return []
    if isinstance(value, Mapping):
        if "candidates" in value:
            return [_as_point(v) for v in value["candidates"]]
        if "points" in value:
            return [_as_point(v) for v in value["points"]]
        return [_as_point(value)]
    if _is_pair(value):
        return [_as_point(value)]
    if isinstance(value, (tuple, list)):
        return [_as_point(v) for v in value]
    raise ValueError("Cannot interpret position for channel {!r} in anchor {!r}"
                     .format(channel, group_id))


def _parse_anchors(anchors, channel_names):
    if anchors is None:
        return []

    if isinstance(anchors, Mapping):
        items = []
        for ident, spec in anchors.items():
            if isinstance(spec, Mapping):
                spec = dict(spec)
                spec.setdefault("id", ident)
            else:
                spec = {"id": ident, "positions": spec}
            items.append(spec)
    else:
        items = list(anchors)

    parsed = []
    for idx, spec in enumerate(items):
        ident = idx
        points = {}
        order = None
        buffer = None

        if isinstance(spec, Mapping):
            ident = spec.get("id", spec.get("group", spec.get("pulse",
                                                             spec.get("name", idx))))
            order = spec.get("order")
            buffer = spec.get("buffer")
            source = spec.get("positions", spec.get("candidates", spec.get("points")))
            if source is None:
                source = {}
                nested = spec.get("channels")
                for name in channel_names:
                    if name in spec:
                        source[name] = spec[name]
                    elif isinstance(nested, Mapping) and name in nested:
                        source[name] = nested[name]
            if isinstance(source, Mapping):
                unknown_channels = [name for name in source if name not in channel_names]
                if unknown_channels:
                    raise UnknownAnchorError(
                        "Anchor {!r} refers to unknown channels: {}"
                        .format(ident, unknown_channels), groups=[ident])
                for name in channel_names:
                    if name in source:
                        points[name] = _normalise_channel_value(source[name], name, ident)
            elif isinstance(source, (tuple, list)):
                flat = list(source)
                # A flat list is either (vi, vj, ci, cj) for two channels or
                # one (i, j) pair per channel.
                if len(flat) == len(channel_names) and all(_is_pair(v) for v in flat):
                    for name, value in zip(channel_names, flat):
                        points[name] = [_as_point(value)]
                elif len(flat) == 2 * len(channel_names):
                    for ci, name in enumerate(channel_names):
                        points[name] = [(int(flat[2 * ci]), int(flat[2 * ci + 1]))]
                else:
                    raise ValueError("Cannot interpret anchor positions {!r}".format(source))
            if not points:
                flat = spec.get("indices", spec.get("index", spec.get("point")))
                if flat is not None:
                    flat = list(flat)
                    if len(flat) == 2 * len(channel_names):
                        for ci, name in enumerate(channel_names):
                            points[name] = [(int(flat[2 * ci]), int(flat[2 * ci + 1]))]
        else:
            sequence = list(spec)
            if (len(channel_names) == 2 and len(sequence) == 2
                    and all(_is_pair(value) for value in sequence)):
                points[channel_names[0]] = [_as_point(sequence[0])]
                points[channel_names[1]] = [_as_point(sequence[1])]
            elif len(sequence) == 2 * len(channel_names):
                for ci, name in enumerate(channel_names):
                    points[name] = [(int(sequence[2 * ci]), int(sequence[2 * ci + 1]))]
            elif len(sequence) == len(channel_names) and all(_is_pair(v) for v in sequence):
                for name, value in zip(channel_names, sequence):
                    points[name] = [_as_point(value)]
            elif len(sequence) >= 1 and isinstance(sequence[0], (str, int)):
                ident = sequence[0]
                rest = sequence[1:]
                if len(rest) == len(channel_names) and all(_is_pair(v) for v in rest):
                    for name, value in zip(channel_names, rest):
                        points[name] = [_as_point(value)]
                elif len(rest) == 2 * len(channel_names):
                    for ci, name in enumerate(channel_names):
                        points[name] = [(int(rest[2 * ci]), int(rest[2 * ci + 1]))]
                else:
                    raise ValueError("Cannot interpret anchor {!r}: {!r}".format(idx, spec))
            else:
                raise ValueError("Cannot interpret anchor {!r}: {!r}".format(idx, spec))

        if not points:
            raise UnknownAnchorError(
                "Anchor {!r} has no positions for the known channels {}"
                .format(ident, channel_names), groups=[ident])
        parsed.append(_RawAnchor(ident, points, order=order, buffer=buffer))
    return parsed


def _channel_buffer(buffer, channel, anchor, default=0):
    value = default
    if buffer is not None:
        if isinstance(buffer, Mapping):
            if channel in buffer:
                value = buffer[channel]
            elif "default" in buffer:
                value = buffer["default"]
        else:
            value = buffer
    if anchor is not None and anchor.buffer is not None:
        if isinstance(anchor.buffer, Mapping):
            value = anchor.buffer.get(channel, value)
        else:
            value = anchor.buffer
    return int(value or 0)


def _expand_candidates(candidates, first_length, second_length, buffer):
    """Expand exact points by a per-side buffer and clip to the series."""
    result = set()
    if buffer <= 0:
        for i, j in candidates:
            if 0 <= i < first_length and 0 <= j < second_length:
                result.add((i, j))
    else:
        for i, j in candidates:
            for di in range(-buffer, buffer + 1):
                ni = i + di
                if ni < 0 or ni >= first_length:
                    continue
                for dj in range(-buffer, buffer + 1):
                    nj = j + dj
                    if 0 <= nj < second_length:
                        result.add((ni, nj))
    return sorted(result)


def _unique_in_order(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# DTW segments
# ---------------------------------------------------------------------------


def _local(a, b, i, j):
    difference = a[i] - b[j]
    return difference * difference


def _successors(r, c, rows, cols):
    """Monotone successors in lexicographic order of the next coordinate.

    Moving right keeps the first index as small as possible, then moving down,
    then moving diagonally.  This order is what makes a reconstructed path
    lexicographically smallest.
    """
    if c + 1 < cols:
        yield r, c + 1
    if r + 1 < rows:
        yield r + 1, c
    if r + 1 < rows and c + 1 < cols:
        yield r + 1, c + 1


def _segment_cost(a, b, start: Optional[Point], end: Point):
    """Minimum squared cost of a DTW segment (rolling rows only)."""
    ei, ej = end
    if start is None:
        base_i, base_j = 0, 0
        include_start = True
    else:
        base_i, base_j = start
        if ei < base_i or ej < base_j:
            return inf
        include_start = False
    rows, cols = ei - base_i + 1, ej - base_j + 1
    if rows <= 0 or cols <= 0:
        return inf
    prev = array.array("d", [inf]) * cols
    cur = array.array("d", [inf]) * cols
    for r in range(rows):
        for c in range(cols):
            i, j = base_i + r, base_j + c
            if r == 0 and c == 0:
                value = _local(a, b, i, j) if include_start else 0.0
            else:
                best = inf
                if r > 0 and c > 0 and prev[c - 1] < best:
                    best = prev[c - 1]
                if r > 0 and prev[c] < best:
                    best = prev[c]
                if c > 0 and cur[c - 1] < best:
                    best = cur[c - 1]
                value = best + _local(a, b, i, j)
            cur[c] = value
        prev, cur = cur, prev
        for c in range(cols):
            cur[c] = inf
    return prev[cols - 1]


def _segment_path_exact(a, b, start: Optional[Point], end: Point):
    """Exact segment path with one cost row and predecessor storage.

    Ties are resolved by preferring the right move, then the down move, then
    the diagonal move from the start of the path.  This produces the
    lexicographically smallest sequence of original sample positions, because
    the first coordinate grows slowest.
    """
    ei, ej = end
    if start is None:
        base_i, base_j = 0, 0
        include_start = True
    else:
        base_i, base_j = start
        if ei < base_i or ej < base_j:
            return inf, []
        include_start = False
    rows, cols = ei - base_i + 1, ej - base_j + 1
    if rows <= 0 or cols <= 0:
        return inf, []

    prev = array.array("d", [inf]) * cols
    cur = array.array("d", [inf]) * cols
    directions = bytearray(rows * cols)
    for r in range(rows):
        for c in range(cols):
            i, j = base_i + r, base_j + c
            if r == 0 and c == 0:
                value = _local(a, b, i, j) if include_start else 0.0
            else:
                best = inf
                direction = 0
                # On equal cost prefer right, then down, then diagonal.  This
                # keeps the next first-series index as small as possible and
                # is the tie-break used by the joint result.
                if c > 0:
                    best = cur[c - 1]
                    direction = 2
                if r > 0 and prev[c] < best:
                    best = prev[c]
                    direction = 1
                if r > 0 and c > 0 and prev[c - 1] < best:
                    best = prev[c - 1]
                    direction = 0
                value = best + _local(a, b, i, j)
                directions[r * cols + c] = direction
            cur[c] = value
        prev, cur = cur, prev
        for c in range(cols):
            cur[c] = inf

    cost = prev[cols - 1]
    if math.isinf(cost):
        return inf, []
    path = []
    r, c = rows - 1, cols - 1
    while True:
        path.append((base_i + r, base_j + c))
        if r == 0 and c == 0:
            break
        direction = directions[r * cols + c]
        if direction == 0:
            r, c = r - 1, c - 1
        elif direction == 1:
            r -= 1
        else:
            c -= 1
    path.reverse()
    return cost, path


def _forward_to_row(a, b, r0, r1, c0, c1):
    """Costs from (r0,c0) to every column of row ``r1``, both included."""
    cols = c1 - c0
    prev = array.array("d", [inf]) * cols
    cur = array.array("d", [inf]) * cols
    for r in range(r0, r1 + 1):
        for idx, c in enumerate(range(c0, c1)):
            if r == r0 and c == c0:
                value = _local(a, b, r, c)
            else:
                best = inf
                if idx > 0 and cur[idx - 1] < best:
                    best = cur[idx - 1]
                if r > r0:
                    if idx > 0 and prev[idx - 1] < best:
                        best = prev[idx - 1]
                    if prev[idx] < best:
                        best = prev[idx]
                value = best + _local(a, b, r, c)
            cur[idx] = value
        if r != r1:
            prev, cur = cur, prev
            for idx in range(cols):
                cur[idx] = inf
    return cur


def _backward_to_row(a, b, r0, r1, c0, c1):
    """Costs from every column of row ``r0`` to (r1-1,c1-1), both included."""
    cols = c1 - c0
    next_row = array.array("d", [inf]) * cols
    cur = array.array("d", [inf]) * cols
    for r in range(r1 - 1, r0 - 1, -1):
        for idx in range(cols - 1, -1, -1):
            c = c0 + idx
            if r == r1 - 1 and c == c1 - 1:
                value = _local(a, b, r, c)
            else:
                best = inf
                if idx + 1 < cols and cur[idx + 1] < best:
                    best = cur[idx + 1]
                if r < r1 - 1:
                    if idx + 1 < cols and next_row[idx + 1] < best:
                        best = next_row[idx + 1]
                    if next_row[idx] < best:
                        best = next_row[idx]
                value = best + _local(a, b, r, c)
            cur[idx] = value
        if r != r0:
            next_row, cur = cur, next_row
            for idx in range(cols):
                cur[idx] = inf
    return cur


def _rect_path_dp(a, b, r0, r1, c0, c1):
    """Exact DTW path inside a small rectangle, with predecessor storage."""
    rows, cols = r1 - r0, c1 - c0
    if rows <= 0 or cols <= 0:
        return inf, []
    prev = array.array("d", [inf]) * cols
    cur = array.array("d", [inf]) * cols
    directions = bytearray(rows * cols)
    for rr in range(rows):
        for cc in range(cols):
            i, j = r0 + rr, c0 + cc
            if rr == 0 and cc == 0:
                value = _local(a, b, i, j)
            else:
                best = inf
                direction = 0
                if cc > 0:
                    best, direction = cur[cc - 1], 2
                if rr > 0 and prev[cc] < best:
                    best, direction = prev[cc], 1
                if rr > 0 and cc > 0 and prev[cc - 1] < best:
                    best, direction = prev[cc - 1], 0
                value = best + _local(a, b, i, j)
                directions[rr * cols + cc] = direction
            cur[cc] = value
        prev, cur = cur, prev
        for cc in range(cols):
            cur[cc] = inf
    cost = prev[cols - 1]
    if math.isinf(cost):
        return inf, []
    path = []
    rr, cc = rows - 1, cols - 1
    while True:
        path.append((r0 + rr, c0 + cc))
        if rr == 0 and cc == 0:
            break
        direction = directions[rr * cols + cc]
        if direction == 0:
            rr, cc = rr - 1, cc - 1
        elif direction == 1:
            rr -= 1
        else:
            cc -= 1
    path.reverse()
    return cost, path


def _rect_path_lowmem(a, b, r0, r1, c0, c1):
    """Divide-and-conquer DTW path with O(columns) working memory."""
    rows, cols = r1 - r0, c1 - c0
    if rows <= 0 or cols <= 0:
        return inf, []
    if rows <= 2 or cols == 1:
        return _rect_path_dp(a, b, r0, r1, c0, c1)

    mid = (r0 + r1) // 2
    if mid <= r0:
        mid = r0 + 1
    if mid >= r1 - 1:
        mid = r1 - 2
    forward = _forward_to_row(a, b, r0, mid, c0, c1)
    backward = _backward_to_row(a, b, mid, r1, c0, c1)
    best_c = c0
    best = inf
    for idx, c in enumerate(range(c0, c1)):
        value = forward[idx] + backward[idx] - _local(a, b, mid, c)
        if value < best:
            best = value
            best_c = c

    top_cost, top_path = _rect_path_lowmem(a, b, r0, mid + 1, c0, best_c + 1)
    bottom_cost, bottom_path = _rect_path_lowmem(a, b, mid, r1, best_c, c1)
    return top_cost + bottom_cost - _local(a, b, mid, best_c), top_path + bottom_path[1:]


# Above this number of cells a segment is reconstructed with the linear-memory
# divide-and-conquer routine.  Short segments use the exact lexicographic mode.
_EXACT_PATH_CELL_LIMIT = 262144


def _segment_path(a, b, start: Optional[Point], end: Point):
    """Minimum segment cost and a corresponding path.

    The returned path includes ``start`` when one is given, and always includes
    ``end``.  The cost excludes the local cost of ``start`` so adjacent
    segments can be added without counting an anchor twice.
    """
    ei, ej = end
    if start is None:
        base_i, base_j = 0, 0
        include_start = True
    else:
        base_i, base_j = start
        if ei < base_i or ej < base_j:
            return inf, []
        include_start = False
    rows, cols = ei - base_i + 1, ej - base_j + 1
    if rows <= 0 or cols <= 0:
        return inf, []
    if start is not None and rows == 1 and cols == 1:
        return 0.0, [start]

    if rows * cols <= _EXACT_PATH_CELL_LIMIT:
        return _segment_path_exact(a, b, start, end)

    cost, path = _rect_path_lowmem(a, b, base_i, ei + 1, base_j, ej + 1)
    if start is not None:
        cost -= _local(a, b, base_i, base_j)
    return cost, path


def _unconstrained_cost(a, b):
    """Squared DTW distance without anchors, in raw cost units."""
    return _segment_cost(a, b, None, (len(a) - 1, len(b) - 1))


# ---------------------------------------------------------------------------
# Anchor ordering
# ---------------------------------------------------------------------------


def _nominal_order(anchors):
    indexed = list(enumerate(anchors))
    if all(anchor.order is not None for anchor in anchors):
        indexed.sort(key=lambda item: (item[1].order, item[0]))
    return [anchor for _, anchor in indexed]


def _linear_extensions(nodes, edges, limit=10000):
    """Enumerate topological orders with a lexicographic tie-break.

    ``nodes`` is the nominal id order.  ``edges`` is a set of ``(before,
    after)`` id pairs.  Returns a list of id lists.  The enumeration is capped
    because anchors are expected to be sparse; exceeding the cap is reported
    instead of silently truncating the search space.
    """
    order_index = {ident: index for index, ident in enumerate(nodes)}
    successors = {ident: set() for ident in nodes}
    indegree = {ident: 0 for ident in nodes}
    for before, after in edges:
        if before == after or after in successors[before]:
            continue
        successors[before].add(after)
        indegree[after] += 1

    if len(nodes) > 900:
        raise ValueError("Too many anchors ({}) for order enumeration"
                         .format(len(nodes)))

    results = []
    sequence = []

    def visit(available):
        if len(results) >= limit:
            raise ValueError(
                "Too many possible anchor orders (more than {}). "
                "Pass an explicit order= to disambiguate.".format(limit))
        if len(sequence) == len(nodes):
            results.append(list(sequence))
            return
        for ident in sorted(available, key=order_index.get):
            sequence.append(ident)
            next_available = set(available)
            next_available.remove(ident)
            for nxt in successors[ident]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    next_available.add(nxt)
            visit(next_available)
            for nxt in successors[ident]:
                indegree[nxt] += 1
            sequence.pop()

    visit({ident for ident in nodes if indegree[ident] == 0})
    if not results:
        raise ValueError("The requested anchor order errors create a cycle")
    return results


def _allowed_orders(anchors, order=None, order_errors=None):
    """Return the candidate anchor orders.

    ``order`` is an explicit sequence of ids or a list of allowed sequences.
    ``order_errors`` is a sequence of id pairs whose relative order may be
    wrong according to the test sheet.  Such a pair is treated as an unknown
    relative order; every linear extension of the remaining known order is
    tried.  Anchors are sparse, so this stays small in practice.
    """
    nominal = _nominal_order(anchors)
    by_id = {anchor.id: anchor for anchor in nominal}

    if order is None:
        if not order_errors:
            return [nominal]
        unknown_pairs = set()
        for pair in order_errors:
            if isinstance(pair, Mapping):
                pair = (pair.get("left", pair.get("first")),
                        pair.get("right", pair.get("second")))
            pair = tuple(pair)
            if len(pair) != 2:
                raise ValueError("order_errors entries must be pairs of anchor ids")
            left, right = pair
            unknown = [ident for ident in (left, right) if ident not in by_id]
            if unknown:
                raise UnknownAnchorError(
                    "Unknown anchor id in order_errors: {}".format(unknown),
                    groups=unknown)
            unknown_pairs.add(frozenset(pair))

        ids = [anchor.id for anchor in nominal]
        edges = set()
        for left_index, left in enumerate(ids):
            for right in ids[left_index + 1:]:
                if frozenset((left, right)) in unknown_pairs:
                    continue
                edges.add((left, right))
        orders = _linear_extensions(ids, edges)
        return [[by_id[ident] for ident in sequence] for sequence in orders]

    if isinstance(order, (str, bytes)) or not isinstance(order, (list, tuple)):
        raise TypeError("order must be a sequence of anchor ids or a list of such sequences")
    if order and isinstance(order[0], (list, tuple)) and not isinstance(order[0], str):
        sequences = list(order)
    else:
        sequences = [list(order)]

    result = []
    for sequence in sequences:
        seq = []
        for ident in sequence:
            if ident not in by_id:
                raise UnknownAnchorError(
                    "Unknown anchor id in order: {!r}".format(ident), groups=[ident])
            if by_id[ident] in seq:
                raise UnknownAnchorError(
                    "Anchor id repeated in order: {!r}".format(ident), groups=[ident])
            seq.append(by_id[ident])
        mentioned = {anchor.id for anchor in seq}
        seq.extend(anchor for anchor in nominal if anchor.id not in mentioned)
        result.append(seq)
    return result


# ---------------------------------------------------------------------------
# Per-channel anchored dynamic program
# ---------------------------------------------------------------------------


def _advances(pi, pj, qi, qj):
    """Can a monotone DTW path visit ``(pi,pj)`` before ``(qi,qj)``?

    Equal indices are allowed in one coordinate: when the two instruments use
    different sampling rates, two pulses may share a sample in one series while
    advancing in the other.  Visiting the exact same observation twice is not
    allowed, because each pulse then would not have its own attribution.
    """
    return qi >= pi and qj >= pj and (qi != pi or qj != pj)


def _pairwise_order_conflicts(groups):
    """Return id pairs for which no monotone candidate pair exists."""
    conflicts = []
    constrained = [(anchor, candidates) for anchor, candidates in groups if candidates]
    for index, (left, left_candidates) in enumerate(constrained):
        for right, right_candidates in constrained[index + 1:]:
            possible = False
            for pi, pj in left_candidates:
                for qi, qj in right_candidates:
                    if _advances(pi, pj, qi, qj):
                        possible = True
                        break
                if possible:
                    break
            if not possible:
                conflicts.append((left.id, right.id))
    return conflicts


def _state_better(candidate, best):
    """Compare ``(cost, path_key, last_cell, cells)`` states."""
    if best is None:
        return True
    if candidate[0] != best[0]:
        return candidate[0] < best[0]
    return candidate[1] < best[1]


def _best_anchored(a, b, groups, channel):
    """Best anchored plan for one channel.

    ``groups`` is the shared ordered list of ``(RawAnchor, candidates)``.
    Groups with no candidates in this channel are optional for this channel.
    Returns ``(cost, path, placements)`` or raises an
    :class:`InfeasibleJointAlignment` naming the conflicting groups.

    States are kept per candidate anchor cell and store the winning partial
    path.  Equal-cost plans are therefore compared on the actual sequence of
    original sample coordinates, as required.  Memory is proportional to the
    number of anchor candidates times the length of a path, not to the
    product of the two raw sequence lengths.
    """
    constrained = [(anchor, candidates) for anchor, candidates in groups if candidates]
    if not constrained:
        cost, path = _segment_path(a, b, None, (len(a) - 1, len(b) - 1))
        if math.isinf(cost):
            raise InfeasibleJointAlignment(
                "Channel {!r} has no DTW path".format(channel),
                groups=[anchor.id for anchor, _ in groups], channel=channel)
        return cost, path, []

    conflicts = _pairwise_order_conflicts(constrained)
    if conflicts:
        ids = []
        for left, right in conflicts:
            for ident in (left, right):
                if ident not in ids:
                    ids.append(ident)
        raise InfeasibleJointAlignment(
            "Anchor groups cannot be ordered on channel {!r}: {}".format(channel, conflicts),
            groups=ids, channel=channel, details=conflicts)

    cost_cache = {}
    path_cache = {}

    def segment_cost(p, q):
        key = (p, q)
        if key not in cost_cache:
            cost_cache[key] = _segment_cost(a, b, p, q)
        return cost_cache[key]

    def segment_path(p, q):
        key = (p, q)
        if key not in path_cache:
            path_cache[key] = _segment_path(a, b, p, q)
        return path_cache[key]

    states = None
    for anchor, candidates in constrained:
        new_states = {}
        for q in candidates:
            best = None
            if states is None:
                cost, seg_path = segment_path(None, q)
                if not math.isinf(cost):
                    best = (cost, tuple(seg_path), q, (q,))
            else:
                for p, state in states.items():
                    if not _advances(p[0], p[1], q[0], q[1]):
                        continue
                    seg_cost = segment_cost(p, q)
                    if math.isinf(seg_cost):
                        continue
                    cost = state[0] + seg_cost
                    _, seg_path = segment_path(p, q)
                    key = state[1] + tuple(seg_path[1:])
                    candidate = (cost, key, q, state[3] + (q,))
                    if best is None or candidate[:2] < best[:2]:
                        best = candidate
            if best is not None:
                new_states[q] = best
        if not new_states:
            ids = [item.id for item, _ in constrained]
            raise InfeasibleJointAlignment(
                "Anchor groups cannot be satisfied together on channel {!r}: {}"
                .format(channel, ids), groups=ids, channel=channel)
        states = new_states

    end = (len(a) - 1, len(b) - 1)
    best = None
    for p, state in states.items():
        seg_cost, seg_path = segment_path(p, end)
        if math.isinf(seg_cost):
            continue
        total = state[0] + seg_cost
        key = state[1] + tuple(seg_path[1:])
        candidate = (total, key, p, state[3])
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        ids = [item.id for item, _ in constrained]
        raise InfeasibleJointAlignment(
            "No anchor plan reaches the end of channel {!r}: {}".format(channel, ids),
            groups=ids, channel=channel)

    total, key, _, cells = best
    placements = [
        AnchorPlacement(anchor.id, channel, cell[0], cell[1])
        for (anchor, _), cell in zip(constrained, cells)
    ]
    return total, list(key), placements


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def joint_alignment(s1=None, s2=None, anchors=None, *, channels=None, buffer=0,
                    order=None, order_errors=None, on_infeasible="raise",
                    tie_break=None, vibration=None, current=None, **kwargs):
    """Find the minimum-cost feasible joint alignment of two or more channels.

    :param s1: Mapping ``channel -> series`` for the first trial.  Alternatively,
        a mapping ``channel -> (first_series, second_series)`` when ``s2`` is
        omitted.
    :param s2: Mapping ``channel -> series`` for the second (control) trial.
    :param anchors: Ordered anchor groups.  A group describes one physical
        pulse and its legal positions in each channel::

            {"id": "pulse-7",
             "vibration": (12, 15),          # one exact position
             "current": [(4, 5), (5, 5)]}    # two legal candidates

        A flat four-index tuple ``(vi, vj, ci, cj)`` is also accepted for the
        two default channels ``vibration`` and ``current``.
    :param channels: Channel names when series are passed positionally, or an
        explicit mapping ``channel -> (first_series, second_series)``.
    :param buffer: Maximum positional slack around each anchor, in samples.
        May be an integer or a per-channel mapping.  The anchor position in a
        channel may move by at most this many samples in either series.
    :param order: Explicit group order (ids) or a list of allowed orders.
        By default the input order is used.
    :param order_errors: Pairs of group ids whose relative order may be
        wrong according to the test sheet.  Each marked pair may be inverted.
    :param on_infeasible: ``"raise"`` (default) or ``"result"``.  The latter
        returns a :class:`JointAlignment` with ``feasible=False`` and the
        conflicting group ids in ``conflicts``.
    :param tie_break: Channel order for exact-cost ties.  Defaults to
        ``vibration`` first and then ``current``; other channels follow the
        input order.
    :param vibration, current: Convenience keyword arguments for the usual two
        channels, each given as a ``(first_series, second_series)`` pair.
    :return: :class:`JointAlignment`.
    """
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError("Unknown arguments: {}".format(unknown))

    # Allow joint_alignment(series, anchors) as a convenient two-argument form.
    if s2 is not None and anchors is None and not isinstance(s2, Mapping):
        anchors, s2 = s2, None

    if vibration is not None or current is not None:
        if s1 is not None or s2 is not None or channels is not None:
            raise TypeError("Pass either positional series or vibration/current keywords")
        series = {}
        if vibration is not None:
            series["vibration"] = _normalise_series_pair(vibration)
        if current is not None:
            series["current"] = _normalise_series_pair(current)
    else:
        series = _normalise_inputs(s1, s2=s2, channels=channels)

    if not series:
        raise ValueError("At least one channel is required")
    channel_names = list(series.keys())
    for name, (a, b) in series.items():
        if len(a) == 0 or len(b) == 0:
            raise ValueError("Channel {!r} contains an empty series".format(name))

    raw_anchors = _parse_anchors(anchors, channel_names)
    ids = [anchor.id for anchor in raw_anchors]
    if len(ids) != len(set(ids)):
        seen = set()
        duplicates = []
        for ident in ids:
            if ident in seen:
                duplicates.append(ident)
            seen.add(ident)
        raise UnknownAnchorError(
            "Anchor ids must be unique; duplicated: {}".format(duplicates),
            groups=duplicates)

    orders = _allowed_orders(raw_anchors, order=order, order_errors=order_errors)

    if tie_break is None:
        # The bench convention compares the vibration sample sequence first,
        # then the current sample sequence, independent of mapping order.
        preferred = [name for name in ("vibration", "current") if name in channel_names]
        tie_break = preferred + [name for name in channel_names if name not in preferred]
    else:
        tie_break = list(tie_break)
        unknown = [name for name in tie_break if name not in channel_names]
        if unknown:
            raise ValueError("tie_break contains unknown channels: {}".format(unknown))
        tie_break.extend(name for name in channel_names if name not in tie_break)

    best_result = None
    failures = []
    for sequence in orders:
        channel_results = {}
        failure = None
        for name in channel_names:
            a, b = series[name]
            n, m = len(a), len(b)
            groups = []
            invalid = []
            for anchor in sequence:
                if name not in anchor.points:
                    groups.append((anchor, []))
                    continue
                points = anchor.points[name]
                for i, j in points:
                    if not (0 <= i < n and 0 <= j < m):
                        invalid.append((anchor.id, i, j))
                slack = _channel_buffer(buffer, name, anchor)
                candidates = _expand_candidates(points, n, m, slack)
                groups.append((anchor, candidates))
            if invalid:
                failure = InfeasibleJointAlignment(
                    "Anchor positions outside channel {!r}: {}".format(name, invalid),
                    groups=_unique_in_order(item[0] for item in invalid),
                    channel=name, details=invalid)
                break
            try:
                cost, path, placements = _best_anchored(a, b, groups, name)
            except InfeasibleJointAlignment as exc:
                failure = exc
                break
            channel_results[name] = ChannelAlignment(name, cost, path, placements)
        if failure is not None:
            failures.append(failure)
            continue

        total = sum(channel_results[name].cost for name in channel_names)
        # Compare the vibration path first, independently of its length, and
        # only then the current path.
        key = tuple(tuple(channel_results[name].path) for name in tie_break)
        candidate = (total, key, sequence, channel_results)
        if best_result is None or candidate[:2] < best_result[:2]:
            best_result = candidate

    if best_result is None:
        groups = []
        for failure in failures:
            for group in failure.groups:
                if group not in groups:
                    groups.append(group)
        message = "No feasible joint alignment for anchor groups: {}".format(groups)
        if failures:
            message += " ({})".format(failures[0])
        if on_infeasible == "result":
            channels_out = {
                name: ChannelAlignment(name, inf, [], [])
                for name in channel_names
            }
            return JointAlignment(
                inf, channels_out, anchor_order=[a.id for a in raw_anchors],
                feasible=False, conflicts=groups, message=message,
                _series=series)
        raise InfeasibleJointAlignment(message, groups=groups) from failures[0]

    total, key, sequence, channel_results = best_result
    return JointAlignment(
        total, channel_results, anchor_order=[anchor.id for anchor in sequence],
        feasible=True, message="", _series=series)


def align(*args, **kwargs):
    """Alias for :func:`joint_alignment`."""
    return joint_alignment(*args, **kwargs)


def joint_dtw(*args, **kwargs):
    """Alias for :func:`joint_alignment`."""
    return joint_alignment(*args, **kwargs)


def joint_distance(s1=None, s2=None, anchors=None, **kwargs):
    """Joint cost in squared-difference units."""
    return joint_alignment(s1, s2, anchors=anchors, **kwargs).cost


def joint_distance_sqrt(s1=None, s2=None, anchors=None, **kwargs):
    """Square root of the joint cost, analogous to ``dtw.distance``."""
    return joint_alignment(s1, s2, anchors=anchors, **kwargs).distance


def distance(s1=None, s2=None, anchors=None, **kwargs):
    """Square root of the joint cost."""
    return joint_distance_sqrt(s1, s2, anchors=anchors, **kwargs)


def path(s1=None, s2=None, anchors=None, **kwargs):
    """Warping paths per channel."""
    return joint_alignment(s1, s2, anchors=anchors, **kwargs).paths


def warping_path(s1=None, s2=None, anchors=None, **kwargs):
    """Alias for :func:`path`."""
    return path(s1, s2, anchors=anchors, **kwargs)


def distance_and_path(s1=None, s2=None, anchors=None, **kwargs):
    """Return ``(cost, paths)`` with the cost in squared-difference units."""
    result = joint_alignment(s1, s2, anchors=anchors, **kwargs)
    return result.cost, result.paths


def compare(trial, control, anchors=None, *, channels=None, **kwargs):
    """Align a new trial and a control group on their common anchored process.

    ``trial`` and ``control`` are channel mappings with the same names.  The
    result is one :class:`JointAlignment`; use :meth:`JointAlignment.event_positions`
    to report where a physical pulse landed on each instrument, and
    :attr:`JointAlignment.sample_indices` to report the original sample
    numbers used by each path.
    """
    return joint_alignment(trial, control, anchors=anchors, channels=channels,
                           **kwargs)


def compare_trials(trial, control, anchors=None, **kwargs):
    """Alias for :func:`compare`."""
    return compare(trial, control, anchors=anchors, **kwargs)


def release(s1=None, s2=None, channels=None, vibration=None, current=None):
    """Per-channel squared DTW distance without any shared anchor.

    Accepts the same channel input as :func:`joint_alignment` (but no anchors).
    """
    if vibration is not None or current is not None:
        series = {}
        if vibration is not None:
            series["vibration"] = _normalise_series_pair(vibration)
        if current is not None:
            series["current"] = _normalise_series_pair(current)
    else:
        series = _normalise_inputs(s1, s2=s2, channels=channels)
    return {name: _unconstrained_cost(a, b) for name, (a, b) in series.items()}


def path_cost(a, b, path):
    """Recompute the squared cost of one explicit path."""
    return sum(_local(a, b, i, j) for i, j in path)
