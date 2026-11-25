"""Indexed reissue queries for corrected moving-window rankings.

An archive builds :class:`RankReissueIndex` once from the historical readings
and the rankings that were published from them.  Each correction slip is then
compared independently with that same published report.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "CorrectionArchive",
    "CorrectionIndex",
    "CorrectionPlan",
    "CorrectionQueryResult",
    "CorrectionSlip",
    "DailyRankArchive",
    "IntervalResult",
    "MoveRankCorrectionIndex",
    "MoveRankIndex",
    "RankCorrectionIndex",
    "RankReissueIndex",
    "ReissueIndex",
    "ReissuePlan",
    "ReissueQueryResult",
    "ReissueSlip",
    "build_correction_index",
    "build_index",
    "count_changed_ranks",
    "count_reissues",
    "earliest_changed_rank",
    "earliest_reissue",
    "index_move_rank",
    "index_ranks",
    "move_rank_correction_index",
    "query_correction",
    "query_reissue",
]


def _move_rank_value(n: int, difference: int) -> float:
    """Reproduce the arithmetic order used by native ``move_rank``."""
    if n < 1:
        return np.nan
    if n == 1:
        return 0.0
    result = 0.5 * (float(n + difference) - 1.0)
    result = result / (n - 1.0)
    return 2.0 * (result - 0.5)


def _rank_of_window(window: np.ndarray, min_count: int) -> float:
    """Rank the last element of a one-dimensional window."""
    if window.size == 0:
        return np.nan
    endpoint = window[-1]
    if endpoint != endpoint:
        return np.nan
    finite = ~np.isnan(window)
    n = int(np.count_nonzero(finite))
    if n < min_count:
        return np.nan
    if n == 1:
        return 0.0
    others = window[:-1]
    others = others[~np.isnan(others)]
    less = int(np.count_nonzero(others < endpoint))
    greater = int(np.count_nonzero(others > endpoint))
    return _move_rank_value(n, less - greater)


def _ranks_differ(issued: np.ndarray, corrected: np.ndarray) -> np.ndarray:
    """NaNs count as equal only when both sides are NaN."""
    issued_nan = np.isnan(issued)
    corrected_nan = np.isnan(corrected)
    both_nan = issued_nan & corrected_nan
    return ~both_nan & ((issued_nan != corrected_nan) | (issued != corrected))


def _normalise_interval(
    n: int, start: Any, stop: Any, *, inclusive: bool
) -> tuple[int, int]:
    """Return inclusive bounds clamped to the available samples."""
    if n == 0:
        return 0, -1
    lo = 0 if start is None else int(start)
    hi = n - 1 if stop is None else int(stop)
    if not inclusive:
        hi -= 1
    if lo < 0:
        lo += n
    if hi < 0:
        hi += n
    lo = max(0, lo)
    hi = min(n - 1, hi)
    return lo, hi


def _as_position(value: Any, n: int) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise TypeError("a sample position must be an integer")
    try:
        position = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"invalid sample position: {value!r}") from exc
    if position < 0:
        position += n
    if not 0 <= position < n:
        raise IndexError(f"sample position {value!r} is outside 0..{n - 1}")
    return position


def _as_reading(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"invalid reading: {value!r}") from exc


def _sign(value: float) -> int:
    if value != value:
        return 0
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _contribution(value: float, endpoint: float) -> int:
    return _sign(endpoint - value)


def _array_sign(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    result = np.zeros(values.shape, dtype=np.int64)
    result[finite] = np.sign(values[finite]).astype(np.int64)
    return result


def _first_key(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    raise KeyError(f"none of the keys {keys!r} is present")


def _normalise_corrections(
    corrections: Any, n: int, original: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return sorted positions, original readings and replacement readings."""
    positions: list[int] = []
    values: list[float] = []

    def add(position: Any, value: Any) -> None:
        positions.append(_as_position(position, n))
        values.append(_as_reading(value))

    if corrections is None:
        pass
    elif isinstance(corrections, Mapping):
        for position, value in corrections.items():
            add(position, value)
    elif isinstance(corrections, np.ndarray):
        if corrections.ndim == 2 and corrections.shape[1] == 2:
            for position, value in corrections:
                add(position, value)
        elif corrections.ndim == 1:
            for position, value in enumerate(corrections):
                add(position, value)
        else:
            raise TypeError("a correction array must be one- or two-dimensional")
    else:
        items = list(corrections)
        if items and all(isinstance(item, Mapping) for item in items):
            for item in items:
                position = _first_key(
                    item,
                    "position",
                    "index",
                    "sample",
                    "point",
                    "at",
                    "date",
                    "位置",
                    "测点",
                    "采样点",
                )
                value = _first_key(
                    item,
                    "value",
                    "new",
                    "new_value",
                    "reading",
                    "replacement",
                    "读数",
                    "更正",
                    "新值",
                )
                add(position, value)
        else:
            for item in items:
                if isinstance(item, (tuple, list, np.ndarray)) and len(item) == 2:
                    add(item[0], item[1])
                else:
                    raise TypeError(
                        "corrections must map positions to readings or be "
                        "(position, reading) pairs"
                    )

    if not positions:
        empty_int = np.empty(0, dtype=np.intp)
        empty_float = np.empty(0, dtype=np.float64)
        return empty_int, empty_float, empty_float

    by_position: dict[int, float] = {}
    for position, value in zip(positions, values, strict=True):
        by_position[position] = value
    ordered = sorted(by_position)
    pos = np.asarray(ordered, dtype=np.intp)
    new = np.asarray([by_position[position] for position in ordered], dtype=np.float64)
    old = np.asarray(original[pos], dtype=np.float64).copy()
    return pos, old, new


@dataclass
class ReissueQueryResult:
    """The reissue answer for one slip and one sample interval."""

    start: int
    stop: int
    positions: np.ndarray
    issued: np.ndarray
    corrected: np.ndarray
    corrections: np.ndarray

    @property
    def count(self) -> int:
        return int(self.positions.size)

    @property
    def changed_count(self) -> int:
        return self.count

    @property
    def rerank_count(self) -> int:
        return self.count

    @property
    def reissue_count(self) -> int:
        return self.count

    @property
    def earliest(self) -> int | None:
        return int(self.positions[0]) if self.positions.size else None

    @property
    def first(self) -> int | None:
        return self.earliest

    @property
    def earliest_position(self) -> int | None:
        return self.earliest

    @property
    def indices(self) -> np.ndarray:
        return self.positions

    @property
    def changed_positions(self) -> np.ndarray:
        return self.positions

    @property
    def correction_positions(self) -> np.ndarray:
        return self.corrections

    @property
    def original(self) -> np.ndarray:
        return self.issued

    @property
    def ranks(self) -> np.ndarray:
        return self.corrected

    def __len__(self) -> int:
        return self.count

    def __bool__(self) -> bool:
        return self.count > 0

    def __iter__(self):
        return iter(int(position) for position in self.positions)

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "count": self.count,
            "changed_count": self.count,
            "rerank_count": self.count,
            "reissue_count": self.count,
            "earliest": self.earliest,
            "first": self.earliest,
            "earliest_position": self.earliest,
            "start": self.start,
            "stop": self.stop,
            "positions": self.positions,
            "indices": self.positions,
            "changed_positions": self.positions,
            "corrections": self.corrections,
            "correction_positions": self.corrections,
            "issued": self.issued,
            "original": self.issued,
            "corrected": self.corrected,
            "ranks": self.corrected,
        }
        try:
            return aliases[key]
        except KeyError as exc:
            raise KeyError(key) from exc

    def keys(self) -> tuple[str, ...]:
        return (
            "count",
            "earliest",
            "positions",
            "issued",
            "corrected",
            "corrections",
            "start",
            "stop",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "stop": self.stop,
            "count": self.count,
            "earliest": self.earliest,
            "positions": self.positions,
            "issued": self.issued,
            "corrected": self.corrected,
            "corrections": self.corrections,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()


IntervalResult = ReissueQueryResult
CorrectionQueryResult = ReissueQueryResult


class CorrectionSlip:
    """One batch of positional replacements and its cached changed samples.

    ``count(start, stop)`` and ``earliest(start, stop)`` answer interval
    queries in logarithmic time after the slip is built.  Passing
    ``inclusive=False`` treats both interval endpoints as open.
    """

    def __init__(
        self,
        index: RankReissueIndex,
        positions: np.ndarray,
        old: np.ndarray,
        new: np.ndarray,
        *,
        name: Any = None,
    ) -> None:
        self.index = index
        self.name = name
        self.positions = np.asarray(positions, dtype=np.intp)
        self.old = np.asarray(old, dtype=np.float64)
        self.new = np.asarray(new, dtype=np.float64)
        effective = ~(
            (self.old == self.new) | (np.isnan(self.old) & np.isnan(self.new))
        )
        self.positions = self.positions[effective]
        self.old = self.old[effective]
        self.new = self.new[effective]
        self._new_by_position = {
            int(position): float(value)
            for position, value in zip(self.positions, self.new, strict=True)
        }
        self._old_by_position = {
            int(position): float(value)
            for position, value in zip(self.positions, self.old, strict=True)
        }
        changed_positions, issued, corrected = self._compute_changed()
        self._changed_positions = changed_positions
        self._issued_changed = issued
        self._corrected_changed = corrected

    @property
    def changed_positions(self) -> np.ndarray:
        return self._changed_positions

    @property
    def issued(self) -> np.ndarray:
        return self._issued_changed

    @property
    def corrected(self) -> np.ndarray:
        return self._corrected_changed

    @property
    def changed_count(self) -> int:
        return int(self._changed_positions.size)

    @property
    def total_changed(self) -> int:
        return self.changed_count

    def _pieces(self) -> list[tuple[int, int, tuple[int, ...]]]:
        """Position segments where the active correction set is constant."""
        n = self.index.n
        events: dict[int, list[int]] = {}
        for ordinal, position in enumerate(self.positions):
            start = int(position)
            stop = min(n, start + self.index.window)
            if start >= stop:
                continue
            events.setdefault(start, []).append(ordinal)
            events.setdefault(stop, []).append(~ordinal)

        pieces: list[tuple[int, int, tuple[int, ...]]] = []
        active: set[int] = set()
        previous: int | None = None
        for point in sorted(events):
            if previous is not None and previous < point and active:
                pieces.append((previous, point - 1, tuple(sorted(active))))
            previous = point
            for encoded in events[point]:
                if encoded >= 0:
                    active.add(encoded)
                else:
                    active.discard(~encoded)
        return pieces

    def _correction_rank(self, position: int) -> tuple[float, int, int]:
        """Corrected rank for a correction endpoint, with n and D."""
        index = self.index
        start = max(0, position - index.window + 1)
        new_value = self._new_by_position.get(position, float(index.values[position]))
        if new_value != new_value:
            return np.nan, 0, 0

        delta_n = 0
        for correction in self.positions:
            correction = int(correction)
            if start <= correction <= position:
                old_finite = not np.isnan(self._old_by_position[correction])
                new_finite = not np.isnan(self._new_by_position[correction])
                if old_finite != new_finite:
                    delta_n += 1 if new_finite else -1
        n = int(index.counts[position]) + delta_n
        if n < index.min_count:
            return np.nan, n, 0
        if n == 1:
            return 0.0, n, 0

        others = index.values[start:position]
        others = others[~np.isnan(others)]
        difference = int(np.count_nonzero(others < new_value)) - int(
            np.count_nonzero(others > new_value)
        )
        for correction in self.positions:
            correction = int(correction)
            if start <= correction < position:
                old_value = self._old_by_position[correction]
                new_other = self._new_by_position[correction]
                difference -= _contribution(old_value, new_value)
                difference += _contribution(new_other, new_value)
        return _move_rank_value(n, difference), n, difference

    def _compute_changed(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        index = self.index
        if self.positions.size == 0 or index.n == 0:
            empty_positions = np.empty(0, dtype=np.intp)
            empty_values = np.empty(0, dtype=np.float64)
            return empty_positions, empty_values, empty_values

        changed_parts: list[np.ndarray] = []
        issued_parts: list[np.ndarray] = []
        corrected_parts: list[np.ndarray] = []
        correction_positions = self.positions
        finite_old = ~np.isnan(self.old)
        finite_new = ~np.isnan(self.new)

        for lo, hi, active in self._pieces():
            positions = np.arange(lo, hi + 1, dtype=np.intp)
            if positions.size == 0:
                continue
            x = index.values[positions]
            bulk = np.ones(positions.size, dtype=bool)
            for member in active:
                correction = int(correction_positions[member])
                if lo <= correction <= hi:
                    bulk[correction - lo] = False

            delta_n = 0
            delta_d = np.zeros(positions.size, dtype=np.int64)
            for member in active:
                old_value = float(self.old[member])
                new_value = float(self.new[member])
                old_finite = bool(finite_old[member])
                new_finite = bool(finite_new[member])
                if old_finite != new_finite:
                    delta_n += 1 if new_finite else -1
                if old_finite and new_finite:
                    delta_d += _array_sign(x - new_value) - _array_sign(x - old_value)
                elif new_finite:
                    delta_d += _array_sign(x - new_value)
                elif old_finite:
                    delta_d -= _array_sign(x - old_value)

            candidate = bulk & index.finite[positions]
            if not np.any(candidate):
                continue
            corrected_n = index.counts[positions] + delta_n
            need_d = (
                candidate
                & ~index.known_difference[positions]
                & (corrected_n >= index.min_count)
            )
            if np.any(need_d):
                index._ensure_all_differences()
            corrected_d = index.differences[positions] + delta_d

            corrected = np.full(positions.size, np.nan, dtype=np.float64)
            valid = candidate & (corrected_n >= index.min_count)
            one = valid & (corrected_n == 1)
            many = valid & (corrected_n >= 2)
            corrected[one] = 0.0
            if np.any(many):
                ns = corrected_n[many].astype(np.float64)
                ds = corrected_d[many].astype(np.float64)
                first = 0.5 * (ns + ds - 1.0)
                first = first / (ns - 1.0)
                corrected[many] = 2.0 * (first - 0.5)

            issued = index.issued[positions]
            changed = candidate & _ranks_differ(issued, corrected)
            if np.any(changed):
                changed_parts.append(positions[changed])
                issued_parts.append(issued[changed])
                corrected_parts.append(corrected[changed])

        for correction in self.positions:
            position = int(correction)
            corrected_rank, _, _ = self._correction_rank(position)
            issued_rank = float(index.issued[position])
            if _ranks_differ(np.asarray([issued_rank]), np.asarray([corrected_rank]))[
                0
            ]:
                changed_parts.append(np.asarray([position], dtype=np.intp))
                issued_parts.append(np.asarray([issued_rank], dtype=np.float64))
                corrected_parts.append(np.asarray([corrected_rank], dtype=np.float64))

        if not changed_parts:
            empty_positions = np.empty(0, dtype=np.intp)
            empty_values = np.empty(0, dtype=np.float64)
            return empty_positions, empty_values, empty_values

        positions = np.concatenate(changed_parts)
        issued = np.concatenate(issued_parts)
        corrected = np.concatenate(corrected_parts)
        order = np.argsort(positions, kind="stable")
        return positions[order], issued[order], corrected[order]

    def _interval(self, start: Any, stop: Any, inclusive: bool) -> tuple[int, int]:
        return _normalise_interval(self.index.n, start, stop, inclusive=inclusive)

    def _slice(
        self, start: Any, stop: Any, inclusive: bool
    ) -> tuple[int, int, int, int]:
        n = self.index.n
        if n == 0:
            return 0, -1, 0, 0
        if start is None:
            lower = 0
        else:
            lower = int(start)
            if lower < 0:
                lower += n
            if not inclusive:
                lower += 1
        if stop is None:
            upper = n - 1
        else:
            upper = int(stop)
            if upper < 0:
                upper += n
            if not inclusive:
                upper -= 1
        lo = max(0, lower)
        hi = min(n - 1, upper)
        if self._changed_positions.size == 0 or lo > hi:
            return lo, hi, 0, 0
        left = bisect_left(self._changed_positions, lo)
        right = bisect_right(self._changed_positions, hi)
        return lo, hi, left, right

    def count(
        self, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> int:
        _, _, left, right = self._slice(start, stop, inclusive)
        return int(right - left)

    def earliest(
        self, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> int | None:
        _, _, left, right = self._slice(start, stop, inclusive)
        return int(self._changed_positions[left]) if left < right else None

    def positions_in(
        self, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> np.ndarray:
        _, _, left, right = self._slice(start, stop, inclusive)
        return self._changed_positions[left:right]

    def corrections_in(
        self, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> np.ndarray:
        lo, hi = self._interval(start, stop, inclusive)
        if self.positions.size == 0 or lo > hi:
            return np.empty(0, dtype=np.intp)
        left = bisect_left(self.positions, lo)
        right = bisect_right(self.positions, hi)
        return self.positions[left:right]

    def query(
        self, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> ReissueQueryResult:
        lo, hi, left, right = self._slice(start, stop, inclusive)
        return ReissueQueryResult(
            start=lo,
            stop=hi,
            positions=self._changed_positions[left:right],
            issued=self._issued_changed[left:right],
            corrected=self._corrected_changed[left:right],
            corrections=self.corrections_in(start, stop, inclusive=inclusive),
        )

    def corrected_rank(self, position: Any) -> float:
        position = _as_position(position, self.index.n)
        index = self.index
        start = max(0, position - index.window + 1)
        window = index.values[start : position + 1].copy()
        for correction in self.positions:
            correction = int(correction)
            if start <= correction <= position:
                window[correction - start] = self._new_by_position[correction]
        return _rank_of_window(window, index.min_count)

    def ranks(self, positions: Any = None) -> np.ndarray:
        if positions is None:
            positions = np.arange(self.index.n)
        return np.asarray(
            [self.corrected_rank(position) for position in positions],
            dtype=np.float64,
        )

    def explain(self, positions: Any = None) -> list[dict[str, Any]]:
        if positions is None:
            positions = self._changed_positions
        details: list[dict[str, Any]] = []
        seen: set[int] = set()
        for position in positions:
            point = _as_position(position, self.index.n)
            if point in seen:
                continue
            seen.add(point)
            start = max(0, point - self.index.window + 1)
            corrections = [
                {
                    "position": int(correction),
                    "old": float(self._old_by_position[int(correction)]),
                    "new": float(self._new_by_position[int(correction)]),
                }
                for correction in self.positions
                if start <= int(correction) <= point
            ]
            details.append(
                {
                    "position": point,
                    "original": float(self.index.values[point]),
                    "corrected_reading": float(
                        self._new_by_position.get(point, self.index.values[point])
                    ),
                    "issued": float(self.index.issued[point]),
                    "corrected": float(self.corrected_rank(point)),
                    "corrections": corrections,
                }
            )
        return details

    def __len__(self) -> int:
        return self.changed_count

    def __repr__(self) -> str:
        label = f" {self.name!r}" if self.name is not None else ""
        return (
            f"<CorrectionSlip{label}: {self.positions.size} corrections, "
            f"{self.changed_count} changed>"
        )


ReissueSlip = CorrectionSlip
CorrectionPlan = CorrectionSlip
ReissuePlan = CorrectionSlip


class RankReissueIndex:
    """A one-time index of readings and their published moving-window ranks.

    Build once from the historical readings and the ranks that were published
    from them.  Each correction slip is then answered against that same report.

    Examples
    --------
    >>> index = RankReissueIndex(values, window=3, issued=published)
    >>> slip = index.slip({0: 2.0, 1: 0.0}, name="slip-1")
    >>> slip.count(start=100, stop=200)
    >>> slip.earliest(start=100, stop=200)
    """

    def __init__(
        self,
        values: Any,
        window: int,
        min_count: int | None = None,
        issued: Any = None,
        *,
        published: Any = None,
        ranks: Any = None,
    ) -> None:
        array = np.asarray(values, dtype=np.float64)
        if array.ndim != 1:
            raise ValueError("the historical readings must be one-dimensional")
        self.values = np.array(array, dtype=np.float64, copy=True)
        self.n = int(self.values.size)
        self.window = int(window)
        if self.window < 1:
            raise ValueError("`window` must be at least 1")
        if self.n and self.window > self.n:
            raise ValueError("`window` is too long")
        if min_count is None:
            self.min_count = self.window
        else:
            self.min_count = int(min_count)
            if self.min_count <= 0:
                raise ValueError("`min_count` must be greater than zero")
            if self.min_count > self.window:
                raise ValueError("`min_count` cannot be greater than `window`")

        supplied = (
            issued
            if issued is not None
            else published
            if published is not None
            else ranks
        )
        if supplied is None:
            import bottleneck as bn

            supplied = bn.move_rank(self.values, self.window, min_count=self.min_count)
        published_array = np.asarray(supplied, dtype=np.float64)
        if published_array.shape != self.values.shape:
            raise ValueError(
                "published rankings must have the same shape as the readings"
            )
        self.issued = np.array(published_array, dtype=np.float64, copy=True)

        self.finite = ~np.isnan(self.values)
        prefix = np.empty(self.n + 1, dtype=np.int64)
        prefix[0] = 0
        np.cumsum(self.finite, out=prefix[1:])
        ends = np.arange(1, self.n + 1, dtype=np.intp)
        starts = np.maximum(0, np.arange(self.n, dtype=np.intp) - self.window + 1)
        self.counts = prefix[ends] - prefix[starts]

        self.differences = np.zeros(self.n, dtype=np.int64)
        self.known_difference = np.zeros(self.n, dtype=bool)
        usable = self.finite & np.isfinite(self.issued) & (self.counts >= 1)
        single = usable & (self.counts == 1)
        many = usable & (self.counts >= 2)
        self.known_difference[single] = True
        if np.any(many):
            self.differences[many] = np.rint(
                self.issued[many] * (self.counts[many] - 1)
            ).astype(np.int64)
            self.known_difference[many] = True
        self._all_differences = False
        self._slips: dict[Any, CorrectionSlip] = {}

    def _ensure_all_differences(self) -> None:
        """Fill less-minus-greater counts for every finite endpoint."""
        if self._all_differences:
            return
        self._all_differences = True
        if self.n == 0 or not np.any(self.finite):
            return
        unique_values, inverse = np.unique(
            self.values[self.finite], return_inverse=True
        )
        size = int(unique_values.size)
        ranks = np.zeros(self.n, dtype=np.intp)
        ranks[self.finite] = inverse.astype(np.intp) + 1
        bit = [0] * (size + 1)

        def add(index: int, delta: int) -> None:
            while index <= size:
                bit[index] += delta
                index += index & -index

        def total(index: int) -> int:
            result = 0
            while index > 0:
                result += bit[index]
                index -= index & -index
            return result

        present = 0
        window = self.window
        for position in range(self.n):
            entering = position - 1
            leaving = position - window
            if leaving >= 0 and self.finite[leaving]:
                add(int(ranks[leaving]), -1)
                present -= 1
            if entering >= 0 and self.finite[entering]:
                add(int(ranks[entering]), 1)
                present += 1
            if self.finite[position]:
                rank = int(ranks[position])
                less = total(rank - 1)
                less_or_equal = total(rank)
                greater = present - less_or_equal
                self.differences[position] = less - greater
                self.known_difference[position] = True

    def _fingerprint(
        self, positions: np.ndarray, new: np.ndarray
    ) -> tuple[bytes, bytes]:
        return positions.tobytes(), np.asarray(new, dtype=np.float64).view(
            np.uint64
        ).tobytes()

    def slip(
        self, corrections: Any, *, name: Any = None, key: Any = None
    ) -> CorrectionSlip:
        positions, old, new = _normalise_corrections(corrections, self.n, self.values)
        cache_key = key if key is not None else name
        if cache_key is None:
            cache_key = ("auto",) + self._fingerprint(positions, new)
        if cache_key in self._slips:
            return self._slips[cache_key]
        slip = CorrectionSlip(
            self, positions, old, new, name=name if name is not None else key
        )
        self._slips[cache_key] = slip
        return slip

    def add_slip(self, key: Any, corrections: Any) -> CorrectionSlip:
        return self.slip(corrections, name=key, key=key)

    def _resolve_slip(
        self,
        corrections: Any,
        slip: Any,
    ) -> CorrectionSlip:
        if isinstance(corrections, CorrectionSlip):
            return corrections
        if slip is not None:
            if isinstance(slip, CorrectionSlip):
                return slip
            if slip in self._slips:
                return self._slips[slip]
            return self.slip(slip)
        if isinstance(corrections, (str, int)) and corrections in self._slips:
            return self._slips[corrections]
        return self.slip(corrections)

    def query(
        self,
        corrections: Any = None,
        start: Any = None,
        stop: Any = None,
        *,
        inclusive: bool = True,
        slip: Any = None,
        name: Any = None,
    ) -> ReissueQueryResult:
        active = self._resolve_slip(corrections, slip)
        if name is not None and active.name is None:
            active.name = name
        return active.query(start, stop, inclusive=inclusive)

    def count(
        self,
        corrections: Any = None,
        start: Any = None,
        stop: Any = None,
        *,
        inclusive: bool = True,
        slip: Any = None,
    ) -> int:
        return self.query(
            corrections, start, stop, inclusive=inclusive, slip=slip
        ).count

    def earliest(
        self,
        corrections: Any = None,
        start: Any = None,
        stop: Any = None,
        *,
        inclusive: bool = True,
        slip: Any = None,
    ) -> int | None:
        return self.query(
            corrections, start, stop, inclusive=inclusive, slip=slip
        ).earliest

    def changed_positions(
        self,
        corrections: Any = None,
        start: Any = None,
        stop: Any = None,
        *,
        inclusive: bool = True,
        slip: Any = None,
    ) -> np.ndarray:
        return self.query(
            corrections, start, stop, inclusive=inclusive, slip=slip
        ).positions

    def corrected_rank(self, corrections: Any, position: Any) -> float:
        return self._resolve_slip(corrections, None).corrected_rank(position)

    def explain(self, corrections: Any, positions: Any = None) -> list[dict[str, Any]]:
        return self._resolve_slip(corrections, None).explain(positions)

    def __len__(self) -> int:
        return self.n


MoveRankCorrectionIndex = RankReissueIndex
CorrectionIndex = RankReissueIndex
RankCorrectionIndex = RankReissueIndex
MoveRankIndex = RankReissueIndex
ReissueIndex = RankReissueIndex


class CorrectionArchive:
    """An index that keeps several independently applied correction slips."""

    def __init__(
        self,
        values: Any,
        window: int,
        min_count: int | None = None,
        issued: Any = None,
        *,
        published: Any = None,
        ranks: Any = None,
        corrections: Any = None,
    ) -> None:
        self.index = RankReissueIndex(
            values,
            window,
            min_count,
            issued,
            published=published,
            ranks=ranks,
        )
        self._slips: dict[Any, CorrectionSlip] = {}
        if corrections is not None:
            self.add_many(corrections)

    @property
    def values(self) -> np.ndarray:
        return self.index.values

    @property
    def window(self) -> int:
        return self.index.window

    @property
    def min_count(self) -> int:
        return self.index.min_count

    @property
    def issued(self) -> np.ndarray:
        return self.index.issued

    def add(self, key: Any, corrections: Any) -> CorrectionSlip:
        slip = self.index.slip(corrections, name=key, key=key)
        self._slips[key] = slip
        return slip

    add_slip = add

    def add_many(self, slips: Mapping[Any, Any] | Iterable[Any]) -> None:
        if isinstance(slips, Mapping):
            for key, corrections in slips.items():
                self.add(key, corrections)
            return
        for ordinal, item in enumerate(slips):
            if isinstance(item, Mapping) and (
                "id" in item or "name" in item or "key" in item
            ):
                key = item.get("id", item.get("name", item.get("key")))
                corrections = item.get("corrections", item.get("changes"))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                key, corrections = item
            else:
                key = ordinal
                corrections = item
            self.add(key, corrections)

    def slip(self, key: Any) -> CorrectionSlip:
        return self._slips[key]

    def query(
        self, key: Any, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> ReissueQueryResult:
        return self._slips[key].query(start, stop, inclusive=inclusive)

    def count(
        self, key: Any, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> int:
        return self._slips[key].count(start, stop, inclusive=inclusive)

    def earliest(
        self, key: Any, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> int | None:
        return self._slips[key].earliest(start, stop, inclusive=inclusive)

    def changed_positions(
        self, key: Any, start: Any = None, stop: Any = None, *, inclusive: bool = True
    ) -> np.ndarray:
        return self._slips[key].positions_in(start, stop, inclusive=inclusive)

    def explain(self, key: Any, positions: Any = None) -> list[dict[str, Any]]:
        return self._slips[key].explain(positions)

    def __getitem__(self, key: Any) -> CorrectionSlip:
        return self._slips[key]

    def __len__(self) -> int:
        return len(self.index)


DailyRankArchive = CorrectionArchive


def build_index(
    values: Any,
    window: int,
    min_count: int | None = None,
    issued: Any = None,
    *,
    published: Any = None,
    ranks: Any = None,
) -> RankReissueIndex:
    return RankReissueIndex(
        values,
        window,
        min_count,
        issued,
        published=published,
        ranks=ranks,
    )


build_correction_index = build_index
index_move_rank = build_index
index_ranks = build_index
move_rank_correction_index = build_index


def query_correction(
    index: Any,
    corrections: Any,
    start: Any = None,
    stop: Any = None,
    *,
    window: int | None = None,
    min_count: int | None = None,
    issued: Any = None,
    inclusive: bool = True,
    key: Any = None,
) -> ReissueQueryResult:
    if isinstance(index, CorrectionArchive):
        if key is None:
            if isinstance(corrections, (str, int)) and corrections in index._slips:
                key = corrections
            else:
                key = 0
        if key not in index._slips:
            index.add(key, corrections)
        return index.query(key, start, stop, inclusive=inclusive)
    if isinstance(index, RankReissueIndex):
        return index.query(corrections, start, stop, inclusive=inclusive, name=key)
    if window is None:
        raise TypeError("`window` is required when building from readings")
    built = RankReissueIndex(index, window, min_count, issued)
    return built.query(corrections, start, stop, inclusive=inclusive)


query_reissue = query_correction


def count_reissues(
    index: Any,
    corrections: Any,
    start: Any = None,
    stop: Any = None,
    **kwargs: Any,
) -> int:
    return query_correction(index, corrections, start, stop, **kwargs).count


def earliest_reissue(
    index: Any,
    corrections: Any,
    start: Any = None,
    stop: Any = None,
    **kwargs: Any,
) -> int | None:
    return query_correction(index, corrections, start, stop, **kwargs).earliest


count_changed_ranks = count_reissues
earliest_changed_rank = earliest_reissue
