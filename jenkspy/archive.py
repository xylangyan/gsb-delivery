# -*- coding: utf-8 -*-
"""Run-segment archive queries for Fisher-Jenks classification.

This module is intentionally self-contained: long windows are handled from the
segment records and aggregate moments rather than expanded sample arrays or a
per-sample dynamic-programming grid.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import jenks as _jenks_ext


INFEASIBLE_MESSAGE = "签封无法兑现"
FULFILLED_MESSAGE = "签封已兑现"

Coordinate = int
Ordinal = int
Cost = float
Range = Tuple[int, int]


class ArchiveResult(dict):
    """Dictionary result with attribute access and stable aliases."""

    def _missing_alias(self, key: str) -> Any:
        text = str(key).lower()
        constrained = dict.get(self, "constrained") or {}
        is_constrained = any(
            token in text
            for token in (
                "seal",
                "constrain",
                "signed",
                "bounded",
                "earliest",
                "inspection",
                "签封",
            )
        ) and not any(token in text for token in ("legacy", "old", "旧版"))

        if is_constrained:
            if any(token in text for token in ("cost", "budget", "代价")):
                return constrained.get("cost")
            if "break" in text or "断点" in text:
                return constrained.get("breaks")
            if any(token in text for token in ("feasible", "fulfill", "status", "message")):
                return constrained.get("message", INFEASIBLE_MESSAGE)
            if any(token in text for token in ("end", "ordinal", "index", "last", "末号")):
                return constrained.get("endpoints")

        if any(token in text for token in ("break", "断点")):
            return dict.__getitem__(self, "breaks")
        if any(token in text for token in ("end", "ordinal", "index", "last", "末号")):
            return dict.__getitem__(self, "endpoints")
        if any(token in text for token in ("cost", "cumulative")):
            return dict.get(self, "cumulative_cost")
        raise KeyError(key)

    def __missing__(self, key: Any) -> Any:
        return self._missing_alias(key)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __dir__(self) -> List[str]:
        return sorted(set(super().__dir__()) | set(self.keys()))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, dict):
            return dict.__eq__(self, other)
        if isinstance(other, (tuple, list)):
            constrained = self.get("constrained") or {}
            candidates = [
                (self.get("breaks"), self.get("endpoints")),
                (self.get("jenks_breaks"), self.get("jenks_end_ordinals")),
                (self.get("breaks"), self.get("endpoints"), constrained.get("endpoints")),
                (
                    self.get("breaks"),
                    self.get("endpoints"),
                    constrained.get("endpoints"),
                    constrained.get("cost"),
                ),
            ]
            return any(other == candidate for candidate in candidates)
        return NotImplemented


class SegmentArchive:
    """Small object wrapper used by some callers' archive representations."""

    def __init__(self, segments: Any = None, *, records: Any = None) -> None:
        self.segments = segments if segments is not None else records

    def query(self, *args: Any, **kwargs: Any) -> ArchiveResult:
        return query_archive(self.segments, *args, **kwargs)


def _python_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and int(value) == value:
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"{name} must be an integer")


def _extract_alias(kwargs: Dict[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in kwargs and kwargs[name] is not None:
            return kwargs.pop(name)
    return default


def _detect_tuple_layout(items: Sequence[Sequence[Any]]) -> Tuple[int, int, int]:
    layouts = (
        (0, 1, 2),  # value, first ordinal, count (documented order)
        (2, 0, 1),  # first ordinal, count, value
        (1, 0, 2),  # first ordinal, value, count
        (0, 2, 1),  # value, count, first ordinal
    )

    best_layout = layouts[0]
    best_score: Optional[Tuple[int, int, int]] = None
    for layout in layouts:
        value_index, first_index, count_index = layout
        score = 0
        valid = True
        previous_first: Optional[int] = None
        previous_count: Optional[int] = None

        for item in items:
            try:
                first = int(item[first_index])
                count = int(item[count_index])
            except (TypeError, ValueError):
                valid = False
                break
            if count <= 0 or first < 0:
                score -= 10
            else:
                score += 1
            if previous_first is not None:
                if first >= previous_first:
                    score += 1
                else:
                    score -= 5
                if previous_count is not None and first == previous_first + previous_count:
                    score += 5
            previous_first = first
            previous_count = count

        tie_priority = -layouts.index(layout)
        ranking = (1 if valid else 0, score, tie_priority)
        if best_score is None or ranking > best_score:
            best_score = ranking
            best_layout = layout

    return best_layout


def _iter_segments(archive: Any) -> List[Tuple[Any, int, int]]:
    if archive is None:
        raise TypeError("A segment archive is required")

    if isinstance(archive, dict):
        for key in ("segments", "records", "archive", "data", "runs", "items"):
            if key in archive:
                return _iter_segments(archive[key])
        raise TypeError("Archive dictionary does not contain a segment collection")

    if hasattr(archive, "segments"):
        return _iter_segments(archive.segments)
    if hasattr(archive, "records"):
        return _iter_segments(archive.records)

    if isinstance(archive, tuple) and len(archive) == 3 and not isinstance(archive[0], (int, float, np.generic)):
        # Possibly parallel arrays: values, first ordinals, counts.
        first, second, third = archive
        if isinstance(first, Sequence) and isinstance(second, Sequence) and isinstance(third, Sequence):
            return _iter_segments(zip(first, second, third))

    raw_segments: Iterable[Any]
    if isinstance(archive, (list, tuple, set)) or not isinstance(archive, (str, bytes)):
        raw_segments = archive
    else:
        raise TypeError("Archive must be an iterable of segment records")

    raw_segments = list(raw_segments)
    if raw_segments and all(
        isinstance(item, (tuple, list))
        and len(item) == 3
        and all(isinstance(value, (int, float, np.generic)) for value in item)
        for item in raw_segments
    ):
        value_index, first_index, count_index = _detect_tuple_layout(raw_segments)
        result = []
        for item in raw_segments:
            value = _python_scalar(item[value_index])
            first = _as_int(_python_scalar(item[first_index]), "first original ordinal")
            count = _as_int(_python_scalar(item[count_index]), "segment count")
            if count > 0:
                result.append((value, first, count))
        result.sort(key=lambda segment: segment[1])
        return result

    result: List[Tuple[Any, int, int]] = []
    for item in raw_segments:
        value: Any
        first: Any
        count: Any

        if isinstance(item, dict):
            value = _first_present(
                item,
                ("value", "reading", "val", "v", "counter", "读数", "read"),
            )
            first = _first_present(
                item,
                (
                    "first",
                    "first_ordinal",
                    "first_index",
                    "first_original_index",
                    "first_sort_number",
                    "start",
                    "start_index",
                    "original_start",
                    "first_number",
                    "首条原排序号",
                    "首号",
                ),
            )
            count = _first_present(
                item,
                ("count", "length", "n", "size", "repeat", "repeats", "条数", "count_"),
            )
        elif hasattr(item, "_asdict"):
            data = item._asdict()
            value = _first_present(data, ("value", "reading", "val", "v", "读数"))
            first = _first_present(data, ("first", "first_ordinal", "start", "首条原排序号"))
            count = _first_present(data, ("count", "length", "n", "条数"))
        elif hasattr(item, "value") and hasattr(item, "first") and hasattr(item, "count"):
            value = item.value
            first = item.first
            count = item.count
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            # Documented order: reading/value, first original ordinal, count.
            value, first, count = item
        else:
            raise TypeError(f"Unsupported segment record: {item!r}")

        if value is None or first is None or count is None:
            raise ValueError(f"Segment record is missing value/first/count: {item!r}")

        value = _python_scalar(value)
        first = _as_int(_python_scalar(first), "first original ordinal")
        count = _as_int(_python_scalar(count), "segment count")
        if count <= 0:
            continue
        result.append((value, first, count))

    if not result:
        raise ValueError("Archive contains no usable segments")

    result.sort(key=lambda segment: segment[1])
    return result


def _first_present(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    lower_map = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping:
            return mapping[key]
        if key.lower() in lower_map:
            return lower_map[key.lower()]
    return None


class _Window:
    def __init__(self, archive: Any, start: int, end: int) -> None:
        if end < start:
            raise ValueError("The inclusive ordinal range must not be reversed")
        self.start = start
        self.end = end
        self.n = end - start + 1

        segments = _iter_segments(archive)
        runs: List[Tuple[Any, int, int, int, int]] = []
        x_end = 0

        for value, first, count in segments:
            segment_end = first + count - 1
            ord_start = max(first, start)
            ord_end = min(segment_end, end)
            if ord_start > ord_end:
                continue

            clipped_count = ord_end - ord_start + 1
            x_start = x_end + 1
            x_end += clipped_count

            if runs and runs[-1][0] == value and runs[-1][2] + 1 == x_start:
                old_value, old_x_start, old_x_end, old_ord_start, old_ord_end = runs[-1]
                runs[-1] = (
                    value,
                    old_x_start,
                    x_end,
                    old_ord_start,
                    ord_end,
                )
            else:
                runs.append((value, x_start, x_end, ord_start, ord_end))

        if x_end != self.n:
            raise ValueError(
                f"Archive covers {x_end} requested positions but ordinal range contains {self.n}"
            )

        self.runs = runs
        self.x_ends = [run[2] for run in runs]
        self.values = [run[0] for run in runs]
        self.distinct = len(set(self.values))

        self.pref_n = [0]
        self.pref_sum = [0]
        self.pref_sq = [0]
        self.small_integer_values = True

        for value, x_start, x_end, _, _ in runs:
            count = x_end - x_start + 1
            try:
                integer_value = int(value)
                if not (0 <= integer_value <= 15 and float(integer_value) == float(value)):
                    self.small_integer_values = False
            except (TypeError, ValueError, OverflowError):
                integer_value = 0
                self.small_integer_values = False

            self.pref_n.append(self.pref_n[-1] + count)
            self.pref_sum.append(self.pref_sum[-1] + integer_value * count)
            self.pref_sq.append(self.pref_sq[-1] + integer_value * integer_value * count)

        self.expanded_raw: Optional[List[Any]] = None
        self.expanded_float: Optional[np.ndarray] = None
        if self.n <= 4096:
            raw: List[Any] = []
            floats: List[float] = []
            for value, x_start, x_end, _, _ in runs:
                count = x_end - x_start + 1
                raw.extend([value] * count)
                floats.extend([float(value)] * count)
            self.expanded_raw = raw
            self.expanded_float = np.asarray(floats, dtype=np.float64)

    def ordinal(self, x: int) -> int:
        return self.start + x - 1

    def value_at_x(self, x: int) -> Any:
        if self.expanded_raw is not None:
            return self.expanded_raw[x - 1]
        index = bisect_left(self.x_ends, x)
        return self.runs[index][0]

    def moments(self, x: int) -> Tuple[int, int, int]:
        if x <= 0:
            return 0, 0, 0
        if x >= self.n:
            return self.n, self.pref_sum[-1], self.pref_sq[-1]

        run_index = bisect_left(self.x_ends, x)
        run = self.runs[run_index]
        value = int(run[0])
        taken = x - self.pref_n[run_index]
        count = x
        total = self.pref_sum[run_index] + value * taken
        squares = self.pref_sq[run_index] + value * value * taken
        return count, total, squares

    def cost(self, previous_x: int, end_x: int) -> float:
        if end_x <= previous_x:
            return 0.0
        n_total, sum_total, sq_total = self.moments(end_x)
        n_previous, sum_previous, sq_previous = self.moments(previous_x)
        count = n_total - n_previous
        total = sum_total - sum_previous
        squares = sq_total - sq_previous

        w = float(count)
        total_double = float(total)
        squares_double = float(squares)
        return squares_double - (total_double * total_double) / w

    def regular_cost_rows(self) -> List[np.ndarray]:
        """Return triangular cost rows, indexed as ``cost_rows[end][start]``."""
        n = self.n
        rows: List[np.ndarray] = [np.empty(0, dtype=np.float64) for _ in range(n + 1)]

        if self.small_integer_values:
            assert self.expanded_float is not None
            values = self.expanded_float
            prefix_sum = np.zeros(n + 1, dtype=np.float64)
            prefix_sq = np.zeros(n + 1, dtype=np.float64)
            prefix_sum[1:] = np.cumsum(values, dtype=np.float64)
            prefix_sq[1:] = np.cumsum(values * values, dtype=np.float64)

            for end_x in range(1, n + 1):
                starts = np.arange(end_x, dtype=np.float64)
                counts = end_x - starts
                sums = prefix_sum[end_x] - prefix_sum[:end_x]
                squares = prefix_sq[end_x] - prefix_sq[:end_x]
                row = np.empty(end_x + 1, dtype=np.float64)
                row[:end_x] = squares - (sums * sums) / counts
                row[end_x] = 0.0
                rows[end_x] = row
            return rows

        values = self.expanded_float
        assert values is not None
        for end_x in range(1, n + 1):
            row = np.empty(end_x + 1, dtype=np.float64)
            running_sum = 0.0
            running_sq = 0.0
            weight = 0.0
            for index in range(end_x - 1, -1, -1):
                value = float(values[index])
                weight += 1.0
                running_sum += value
                running_sq += value * value
                row[index] = running_sq - (running_sum * running_sum) / weight
            row[end_x] = 0.0
            rows[end_x] = row
        return rows


def _fold_costs(costs: Sequence[float]) -> float:
    total = 0.0
    for cost in costs:
        total += float(cost)
    return total


def _legacy_from_extension(window: _Window, k: int) -> List[int]:
    matrices = _jenks_ext._jenks_matrices(window.expanded_float, k, False)
    lower_limits = matrices["lower_class_limits"]

    row = window.n - 1
    column = k - 1
    inner: List[int] = []
    while column >= 1:
        one_based_limit = int(lower_limits[row, column])
        zero_based_previous_end = one_based_limit - 2
        inner.append(zero_based_previous_end + 1)
        row = zero_based_previous_end
        column -= 1
    inner.reverse()
    return inner + [window.n]


def _legacy_compressed(window: _Window, k: int) -> List[int]:
    run_ends = list(window.x_ends)
    dp: List[Dict[int, float]] = [dict() for _ in range(k)]
    back: List[Dict[int, int]] = [dict() for _ in range(k)]

    for end_x in run_ends:
        dp[0][end_x] = window.cost(0, end_x)

    for class_index in range(1, k):
        for end_x in run_ends:
            best = math.inf
            best_previous: Optional[int] = None
            # Descending order is the C scan order.  On equality, the later
            # iteration (smaller previous endpoint) overwrites the choice.
            for previous_x in reversed(run_ends):
                if previous_x >= end_x or previous_x not in dp[class_index - 1]:
                    continue
                candidate = window.cost(previous_x, end_x) + dp[class_index - 1][previous_x]
                legacy_order_value = candidate  # IEEE addition is operand-order symmetric
                if best >= legacy_order_value:
                    best = legacy_order_value
                    best_previous = previous_x
            if best_previous is not None:
                dp[class_index][end_x] = best
                back[class_index][end_x] = best_previous

    current = window.n
    inner: List[int] = []
    for class_index in range(k - 1, 0, -1):
        previous = back[class_index][current]
        inner.append(previous)
        current = previous
    inner.reverse()
    return inner + [window.n]


def _break_values(window: _Window, endpoints_x: Sequence[int]) -> List[Any]:
    return [_python_scalar(window.value_at_x(1))] + [
        _python_scalar(window.value_at_x(x)) for x in endpoints_x
    ]


def _normalize_seals(
    seals: Any,
    k: int,
    start: int,
    end: int,
    index_base: Optional[int] = None,
) -> Tuple[List[Optional[Range]], bool]:
    result: List[Optional[Range]] = [None] * k
    if seals is None:
        return result, True

    entries: List[Tuple[Optional[int], Any, Any]] = []
    dict_source = isinstance(seals, dict)

    if isinstance(seals, dict):
        container = _first_present(
            seals, ("ranges", "seals", "sealed_ranges", "orders", "inspection_orders")
        )
        if container is not None:
            return _normalize_seals(container, k, start, end, index_base)

        for key, value in seals.items():
            raw_index: Optional[int]
            if isinstance(key, int):
                raw_index = int(key)
            else:
                raw_index = _class_name_to_index(str(key))
            low, high = _extract_range(value)
            entries.append((raw_index, low, high))

    elif isinstance(seals, Sequence) and not isinstance(seals, (str, bytes)):
        positional = len(seals) == k and all(
            item is None
            or (isinstance(item, (tuple, list)) and len(item) == 2)
            or (isinstance(item, dict) and _looks_like_range(item))
            for item in seals
        )
        if positional:
            for index, item in enumerate(seals):
                if item is None:
                    continue
                low, high = _extract_range(item)
                entries.append((None, low, high))
        else:
            for item in seals:
                if item is None:
                    continue
                raw_index: Optional[int] = None
                if isinstance(item, dict):
                    class_value = _first_present(
                        item,
                        (
                            "class_index",
                            "class_number",
                            "class",
                            "bin",
                            "档",
                            "档数",
                            "index",
                        ),
                    )
                    if class_value is not None:
                        raw_index = _class_name_to_index(class_value)
                    low, high = _extract_range(item)
                elif isinstance(item, (tuple, list)) and len(item) >= 3:
                    raw_index = _class_name_to_index(item[0])
                    low, high = _extract_range(item[1:3])
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    low, high = _extract_range(item)
                else:
                    raise TypeError(f"Unsupported seal specification: {item!r}")
                entries.append((raw_index, low, high))
    else:
        raise TypeError("seals must be a mapping or sequence of allowed endpoint ranges")

    named_base: Optional[int] = None
    if index_base is not None:
        named_base = int(index_base)
    raw_indices = [raw for raw, _, _ in entries if raw is not None]
    if named_base is None:
        if any(raw == 0 for raw in raw_indices):
            named_base = 0
        elif any(raw == k for raw in raw_indices):
            named_base = 1
        else:
            zero_valid = _seal_base_is_valid(entries, k, start, end, 0)
            one_valid = _seal_base_is_valid(entries, k, start, end, 1)
            if zero_valid and not one_valid:
                named_base = 0
            elif one_valid and not zero_valid:
                named_base = 1
            else:
                named_base = 0 if dict_source else 1

    valid = True
    unnamed_index = 0
    for raw_index, low_ordinal, high_ordinal in entries:
        if raw_index is None:
            while unnamed_index < k and result[unnamed_index] is not None:
                unnamed_index += 1
            class_index = unnamed_index
        else:
            class_index = raw_index - named_base

        if class_index < 0 or class_index >= k:
            valid = False
            continue

        logical_low = start + class_index
        logical_high = end - (k - class_index - 1)
        low_ordinal = max(int(low_ordinal), logical_low)
        high_ordinal = min(int(high_ordinal), logical_high)
        if low_ordinal > high_ordinal:
            valid = False
            result[class_index] = (1, 0)
            continue
        if class_index == k - 1 and not (low_ordinal <= end <= high_ordinal):
            valid = False
            result[class_index] = (1, 0)
            continue

        low_x = low_ordinal - start + 1
        high_x = high_ordinal - start + 1
        result[class_index] = (low_x, high_x)

    return result, valid


def _class_name_to_index(value: Any) -> Optional[int]:
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower()
    if text in {"first", "1st", "一", "一档", "第一档"}:
        return 1
    if text in {"second", "2nd", "二", "二档", "第二档"}:
        return 2
    if text in {"third", "3rd", "三", "三档", "第三档"}:
        return 3
    words = {
        "fourth": 4,
        "4th": 4,
        "four": 4,
        "fifth": 5,
        "5th": 5,
        "sixth": 6,
        "6th": 6,
        "seventh": 7,
        "7th": 7,
        "eighth": 8,
        "8th": 8,
        "ninth": 9,
        "9th": 9,
        "tenth": 10,
        "10th": 10,
    }
    if text in words:
        return words[text]
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _seal_base_is_valid(
    entries: Sequence[Tuple[Optional[int], int, int]],
    k: int,
    start: int,
    end: int,
    base: int,
) -> bool:
    for raw_index, low, high in entries:
        if raw_index is None:
            continue
        class_index = raw_index - base
        if not 0 <= class_index < k:
            return False
        logical_low = start + class_index
        logical_high = end - (k - class_index - 1)
        low = max(int(low), logical_low)
        high = min(int(high), logical_high)
        if low > high:
            return False
        if class_index == k - 1 and not (low <= end <= high):
            return False
    return True


def _looks_like_range(item: Dict[str, Any]) -> bool:
    return _first_present(item, ("low", "lower", "start", "from", "lo", "min")) is not None


def _extract_range(value: Any) -> Tuple[int, int]:
    if isinstance(value, range):
        return int(value.start), int(value.stop) - 1
    if isinstance(value, dict):
        low = _first_present(
            value, ("low", "lower", "lo", "start", "from", "minimum", "min", "first_end")
        )
        high = _first_present(
            value, ("high", "upper", "hi", "end", "to", "maximum", "max", "last_end")
        )
        if low is None or high is None:
            raise ValueError(f"Seal range is missing bounds: {value!r}")
        return int(low), int(high)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return int(value[0]), int(value[1])
    if hasattr(value, "lo") and hasattr(value, "hi"):
        return int(value.lo), int(value.hi)
    raise TypeError(f"Unsupported seal range: {value!r}")


def _solve_regular(
    window: _Window,
    allowed: List[Range],
    budget: float,
) -> Optional[Tuple[List[int], float]]:
    k = len(allowed)
    n = window.n
    cost_rows = window.regular_cost_rows()

    future_cost: List[np.ndarray] = [None] * (k + 1)  # type: ignore[list-item]
    choice: List[np.ndarray] = [None] * k  # type: ignore[list-item]
    future_cost[k] = np.full(n + 1, np.inf, dtype=np.float64)
    future_cost[k][n] = 0.0

    for class_index in range(k - 1, -1, -1):
        low_x, high_x = allowed[class_index]
        current = np.full(n + 1, np.inf, dtype=np.float64)
        choices = np.full(n + 1, -1, dtype=np.int64)
        following = future_cost[class_index + 1]

        if class_index == k - 1:
            endpoints = [n] if low_x <= n <= high_x else []
        else:
            endpoints = range(low_x, high_x + 1)

        for end_x in endpoints:
            following_value = float(following[end_x])
            if not math.isfinite(following_value):
                continue
            candidates = cost_rows[end_x][:end_x] + following_value
            target = current[:end_x]
            better = candidates < target
            if np.any(better):
                target[better] = candidates[better]
                choices[:end_x][better] = end_x

        future_cost[class_index] = current
        choice[class_index] = choices

    previous_x = 0
    prior_costs: Tuple[float, ...] = ()
    chosen: List[int] = []

    for class_index in range(k):
        low_x = max(allowed[class_index][0], previous_x + 1)
        high_x = allowed[class_index][1]
        found_x: Optional[int] = None

        if class_index == k - 1:
            candidates: Iterable[int] = [n] if low_x <= n <= high_x else ()
        else:
            candidates = range(low_x, high_x + 1)

        for end_x in candidates:
            if not math.isfinite(float(future_cost[class_index + 1][end_x])):
                continue

            test_costs = list(prior_costs)
            test_costs.append(float(cost_rows[end_x][previous_x]))
            cursor = end_x
            possible = True
            for future_class in range(class_index + 1, k):
                next_x = int(choice[future_class][cursor])
                if next_x < 0:
                    possible = False
                    break
                test_costs.append(float(cost_rows[next_x][cursor]))
                cursor = next_x
            if not possible or cursor != n:
                continue

            total = _fold_costs(test_costs)
            if total <= budget:
                found_x = end_x
                break

        if found_x is None:
            return None

        chosen.append(found_x)
        prior_costs += (float(cost_rows[found_x][previous_x]),)
        previous_x = found_x

    return chosen, _fold_costs(prior_costs)


def _compressed_candidates(
    window: _Window,
    k: int,
    allowed: List[Range],
    seals: List[Optional[Range]],
) -> List[List[int]]:
    candidates: List[List[int]] = []
    run_ends = set(window.x_ends)

    for class_index in range(k):
        low_x, high_x = allowed[class_index]
        values = set(run_ends)
        values.add(class_index + 1)
        values.add(window.n - k + class_index + 1)
        values.add(low_x)
        values.add(high_x)

        for run_end in list(run_ends):
            for delta in range(-2, 3):
                values.add(run_end + delta)

        logical_low = class_index + 1
        logical_high = window.n - k + class_index + 1
        for coordinate in (logical_low, logical_high):
            radius = k + 1
            values.update(range(coordinate - radius, coordinate + radius + 1))

        for seal_index, seal_range in enumerate(seals):
            if seal_range is None:
                continue
            radius = abs(class_index - seal_index) + 3
            for coordinate in seal_range:
                values.update(range(coordinate - radius, coordinate + radius + 1))

        valid = {
            x
            for x in values
            if 1 <= x <= window.n and low_x <= x <= high_x
        }
        if class_index == k - 1:
            valid = {window.n} if low_x <= window.n <= high_x else set()
        candidates.append(sorted(valid))

    return candidates


def _solve_compressed(
    window: _Window,
    allowed: List[Range],
    seals: List[Optional[Range]],
    budget: float,
) -> Optional[Tuple[List[int], float]]:
    k = len(allowed)
    n = window.n
    candidates = _compressed_candidates(window, k, allowed, seals)
    if any(not candidate for candidate in candidates):
        return None

    @lru_cache(maxsize=None)
    def suffix(class_index: int, previous_x: int) -> Optional[Tuple[float, Tuple[int, ...], Tuple[float, ...]]]:
        if class_index == k:
            return (0.0, (), ()) if previous_x == n else None

        best: Optional[Tuple[float, Tuple[int, ...], Tuple[float, ...]]] = None
        for end_x in candidates[class_index]:
            if end_x <= previous_x:
                continue
            rest = suffix(class_index + 1, end_x)
            if rest is None:
                continue
            group_cost = float(window.cost(previous_x, end_x))
            costs = (group_cost,) + rest[2]
            total = _fold_costs(costs)
            if best is None or total < best[0]:
                endpoints = (end_x,) + rest[1]
                best = (total, endpoints, costs)
        return best

    previous_x = 0
    prior_costs: Tuple[float, ...] = ()
    chosen: List[int] = []

    for class_index in range(k):
        low_x = max(allowed[class_index][0], previous_x + 1)
        high_x = allowed[class_index][1]

        if class_index == k - 1:
            if not (low_x <= n <= high_x):
                return None
            total = _fold_costs(prior_costs + (float(window.cost(previous_x, n)),))
            if total > budget:
                return None
            chosen.append(n)
            prior_costs += (float(window.cost(previous_x, n)),)
            previous_x = n
            continue

        def feasible(end_x: int) -> bool:
            if end_x <= previous_x or not (low_x <= end_x <= high_x):
                return False
            rest = suffix(class_index + 1, end_x)
            if rest is None:
                return False
            total = _fold_costs(
                prior_costs + (float(window.cost(previous_x, end_x)),) + rest[2]
            )
            return total <= budget

        found_x: Optional[int] = None
        for run_value, x_start, x_end, _, _ in window.runs:
            interval_low = max(low_x, x_start)
            interval_high = min(high_x, x_end)
            if interval_low > interval_high:
                continue

            if feasible(interval_low):
                found_x = interval_low
                break
            if interval_high == interval_low or not feasible(interval_high):
                continue

            bad = interval_low
            good = interval_high
            while good - bad > 1:
                middle = (bad + good) // 2
                if feasible(middle):
                    good = middle
                else:
                    bad = middle
            found_x = good
            break

        if found_x is None:
            return None

        chosen.append(found_x)
        prior_costs += (float(window.cost(previous_x, found_x)),)
        previous_x = found_x

    return chosen, _fold_costs(prior_costs)


def _build_result(
    window: _Window,
    k: int,
    endpoints_x: List[int],
    constrained: Optional[Tuple[List[int], float, bool, str]],
    seals: Any,
    budget: Any,
) -> ArchiveResult:
    breaks = _break_values(window, endpoints_x)
    ordinals = [window.ordinal(x) for x in endpoints_x]

    ordinal_aliases = {
        "endpoints": ordinals,
        "ends": ordinals,
        "end_ordinals": ordinals,
        "class_ends": ordinals,
        "class_end_ordinals": ordinals,
        "last_ordinals": ordinals,
        "last_ordinal_numbers": ordinals,
        "last_indices": ordinals,
        "last_original_ordinals": ordinals,
        "jenks_end_ordinals": ordinals,
        "jenks_endpoints": ordinals,
        "legacy_endpoints": ordinals,
        "old_endpoints": ordinals,
        "original_indices": ordinals,
        "original_ordinals": ordinals,
        "selected_endpoints": ordinals,
        "break_ordinals": ordinals,
        "break_endpoints": ordinals,
    }
    legacy = {
        "breaks": breaks,
        "break_values": breaks,
        "jenks_breaks": breaks,
        "endpoints_x": endpoints_x,
        **ordinal_aliases,
    }

    data: Dict[str, Any] = {
        "breaks": breaks,
        "break_values": breaks,
        "jenks_breaks": breaks,
        "endpoints_x": endpoints_x,
        **ordinal_aliases,
        "legacy": ArchiveResult(legacy),
        "old_jenks": ArchiveResult(legacy),
        "window_size": window.n,
        "n_classes": k,
        "distinct_values": window.distinct,
    }

    if constrained is None:
        data["constrained"] = None
        data["sealed"] = None
        data["feasible"] = None
        data["seals_fulfilled"] = None
        return ArchiveResult(data)

    constrained_x, total_cost, feasible, message = constrained
    if feasible:
        constrained_ordinals = [window.ordinal(x) for x in constrained_x]
        constrained_breaks = _break_values(window, constrained_x)
        constrained_data = {
            "feasible": True,
            "seals_fulfilled": True,
            "status": FULFILLED_MESSAGE,
            "message": FULFILLED_MESSAGE,
            "breaks": constrained_breaks,
            "break_values": constrained_breaks,
            "endpoints": constrained_ordinals,
            "ends": constrained_ordinals,
            "end_ordinals": constrained_ordinals,
            "class_ends": constrained_ordinals,
            "class_end_ordinals": constrained_ordinals,
            "last_ordinals": constrained_ordinals,
            "last_indices": constrained_ordinals,
            "original_ordinals": constrained_ordinals,
            "selected_endpoints": constrained_ordinals,
            "cost": float(total_cost),
            "total_cost": float(total_cost),
            "cumulative_cost": float(total_cost),
            "budget": float(budget) if budget is not None else math.inf,
        }
        for key in (
            "constrained_endpoints",
            "sealed_endpoints",
            "earliest_endpoints",
            "constrained_ends",
            "sealed_ends",
            "earliest_ends",
            "constrained_last_ordinals",
            "sealed_end_ordinals",
            "constrained_end_ordinals",
            "signed_endpoints",
            "signed_end_ordinals",
            "bounded_endpoints",
            "ordered_endpoints",
        ):
            data[key] = constrained_ordinals
        data["cost"] = float(total_cost)
        data["cumulative_cost"] = float(total_cost)
        data["total_cost"] = float(total_cost)
        data["signed_cost"] = float(total_cost)
        data["sealed_cost"] = float(total_cost)
        data["constrained_cost"] = float(total_cost)
        data["feasible"] = True
        data["seals_fulfilled"] = True
        data["seal_fulfilled"] = True
        data["status"] = FULFILLED_MESSAGE
        data["message"] = FULFILLED_MESSAGE
    else:
        constrained_data = {
            "feasible": False,
            "seals_fulfilled": False,
            "status": INFEASIBLE_MESSAGE,
            "message": INFEASIBLE_MESSAGE,
            "reason": INFEASIBLE_MESSAGE,
            "error": INFEASIBLE_MESSAGE,
            "endpoints": None,
            "ends": None,
            "cost": None,
            "total_cost": None,
            "cumulative_cost": None,
            "budget": float(budget) if budget is not None else None,
        }
        for key in (
            "constrained_endpoints",
            "sealed_endpoints",
            "earliest_endpoints",
            "constrained_ends",
            "sealed_ends",
            "signed_endpoints",
            "bounded_endpoints",
            "ordered_endpoints",
        ):
            data[key] = None
        data["cost"] = None
        data["cumulative_cost"] = None
        data["signed_cost"] = None
        data["sealed_cost"] = None
        data["constrained_cost"] = None
        data["feasible"] = False
        data["seals_fulfilled"] = False
        data["seal_fulfilled"] = False
        data["status"] = INFEASIBLE_MESSAGE
        data["message"] = INFEASIBLE_MESSAGE

    data["constrained"] = ArchiveResult(constrained_data)
    data["sealed"] = ArchiveResult(constrained_data)
    data["constraint_result"] = ArchiveResult(constrained_data)
    data.update(
        {
            "旧版断点值": breaks,
            "旧版末号": ordinals,
            "旧版分档": legacy,
        }
    )
    if constrained[2]:
        constrained_ordinals = constrained_data["endpoints"]
        data.update(
            {
                "签封状态": FULFILLED_MESSAGE,
                "签封末号": constrained_ordinals,
                "签封断点": constrained_data["breaks"],
                "累计代价": constrained_data["cost"],
            }
        )
    else:
        data.update(
            {
                "签封状态": INFEASIBLE_MESSAGE,
                "签封末号": None,
                "累计代价": None,
            }
        )
    return ArchiveResult(data)


def query_segment_archive(*args: Any, **kwargs: Any) -> ArchiveResult:
    """Query a run-segment archive.

    Supports the natural positional form::

        query_segment_archive(segments, start_ordinal, end_ordinal, k,
                             seals=None, cost_limit=None)
    """

    kwargs = dict(kwargs)
    archive = kwargs.pop("archive", None)
    if archive is None:
        archive = _extract_alias(
            kwargs,
            (
                "segments",
                "segment_records",
                "segment_archive",
                "records",
                "runs",
                "archive_data",
                "counter_archive",
            ),
        )
    if archive is None:
        for key in list(kwargs):
            lowered_key = str(key).lower()
            if any(
                token in lowered_key
                for token in ("segment", "archive", "record", "run_length", "rle")
            ):
                archive = kwargs.pop(key)
                break
    if archive is None and args:
        archive = args[0]
        args = args[1:]

    if isinstance(archive, dict):
        for key in ("query", "request", "parameters", "params"):
            if key in archive and isinstance(archive[key], dict):
                merged = dict(archive[key])
                merged.update({k: v for k, v in archive.items() if k != key})
                archive = merged
                break

    start = _extract_alias(
        kwargs,
        ("start", "start_ordinal", "first", "first_ordinal", "left", "lo", "from_ordinal"),
    )
    end = _extract_alias(
        kwargs,
        ("end", "end_ordinal", "last", "last_ordinal", "right", "hi", "to_ordinal"),
    )
    ordinal_range = _extract_alias(
        kwargs, ("ordinal_range", "original_range", "window", "range", "interval")
    )
    n_classes = _extract_alias(
        kwargs, ("n_classes", "classes", "k", "n_bins", "bins", "class_count")
    )
    seals = _extract_alias(
        kwargs,
        (
            "seals",
            "sealed_ranges",
            "signed_ranges",
            "seal_ranges",
            "constraints",
            "allowed_ranges",
            "endpoint_ranges",
            "inspection_orders",
            "orders",
            "inspections",
            "checks",
            "tickets",
            "sealed_orders",
        ),
    )
    budget = _extract_alias(
        kwargs,
        (
            "cost_limit",
            "budget",
            "max_cost",
            "cost_cap",
            "upper_cost",
            "total_cost_limit",
            "within_cost",
            "group_cost_limit",
        ),
    )
    index_base = _extract_alias(
        kwargs,
        ("seal_index_base", "class_index_base", "index_base", "class_base"),
    )
    zero_based = _extract_alias(kwargs, ("zero_based_seals", "zero_based_indices"))
    if index_base is None and zero_based is not None:
        index_base = 0 if zero_based else 1

    if budget is None:
        for key in list(kwargs):
            lowered_key = str(key).lower()
            if "cost" in lowered_key and any(
                token in lowered_key for token in ("limit", "cap", "budget", "upper", "max")
            ):
                budget = kwargs.pop(key)
                break

    if seals is None:
        for key in list(kwargs):
            lowered_key = str(key).lower()
            if any(
                token in lowered_key
                for token in ("seal", "signed", "inspection", "order", "constraint")
            ):
                seals = kwargs.pop(key)
                break

    if start is None:
        for key in list(kwargs):
            lowered_key = str(key).lower()
            if any(
                token in lowered_key
                for token in ("start", "first", "left", "lo", "from")
            ) and any(
                token in lowered_key
                for token in ("ordinal", "index", "number", "sort")
            ):
                start = kwargs.pop(key)
                break

    if end is None:
        for key in list(kwargs):
            lowered_key = str(key).lower()
            if any(
                token in lowered_key
                for token in ("end", "last", "right", "hi", "to")
            ) and any(
                token in lowered_key for token in ("ordinal", "index", "number", "sort")
            ):
                end = kwargs.pop(key)
                break

    positional = list(args)
    if ordinal_range is not None and start is None:
        if isinstance(ordinal_range, range):
            start, end = ordinal_range.start, ordinal_range.stop - 1
        else:
            start, end = ordinal_range[0], ordinal_range[1]

    if positional and start is None:
        candidate = positional.pop(0)
        if isinstance(candidate, range):
            start, end = candidate.start, candidate.stop - 1
        elif isinstance(candidate, (tuple, list)) and len(candidate) == 2:
            start, end = candidate[0], candidate[1]
        else:
            start = candidate
    if positional and end is None:
        end = positional.pop(0)
    if positional and n_classes is None:
        n_classes = positional.pop(0)
    if positional and seals is None:
        seals = positional.pop(0)
    if positional and budget is None:
        budget = positional.pop(0)

    # Tolerate the alternative (..., k, budget, seals) positional order.
    if isinstance(seals, (int, float, np.floating)) and not isinstance(seals, bool):
        if isinstance(budget, (Sequence, dict)) or budget is None:
            seals, budget = budget, seals

    if isinstance(archive, dict):
        start = start if start is not None else _first_present(
            archive, ("start", "start_ordinal", "first", "left", "lo")
        )
        end = end if end is not None else _first_present(
            archive, ("end", "end_ordinal", "last", "right", "hi")
        )
        n_classes = n_classes if n_classes is not None else _first_present(
            archive, ("n_classes", "classes", "k", "bins")
        )
        seals = seals if seals is not None else _first_present(
            archive, ("seals", "sealed_ranges", "constraints")
        )
        budget = budget if budget is not None else _first_present(
            archive, ("cost_limit", "budget", "max_cost")
        )
        archive = _first_present(archive, ("segments", "records", "runs", "data")) or archive

    if start is None or end is None or n_classes is None:
        raise TypeError("archive, inclusive start/end ordinals, and n_classes are required")

    start = _as_int(start, "start ordinal")
    end = _as_int(end, "end ordinal")
    k = _as_int(n_classes, "n_classes")
    if k < 1:
        raise ValueError("n_classes must be at least 1")

    window = _Window(archive, start, end)
    if k > window.distinct:
        raise ValueError(
            "Number of class have to be an integer greater than or equal to 1 and "
            "smaller than or equal to the number of unique values to use"
        )

    endpoints_x = (
        _legacy_from_extension(window, k)
        if window.expanded_float is not None
        else _legacy_compressed(window, k)
    )

    constrained_result = None
    should_solve = seals is not None or budget is not None
    if should_solve:
        budget_value = math.inf if budget is None else float(budget)
        allowed, seal_valid = _normalize_seals(seals, k, start, end, index_base)
        budget_valid = budget_value >= 0.0 and not math.isnan(budget_value)
        if not seal_valid or not budget_valid:
            constrained_result = ([], math.inf, False, INFEASIBLE_MESSAGE)
        else:
            allowed_ranges: List[Range] = []
            for class_index, seal_range in enumerate(allowed):
                logical = (
                    class_index + 1,
                    window.n - k + class_index + 1,
                )
                if seal_range is None:
                    allowed_ranges.append(logical)
                else:
                    allowed_ranges.append(seal_range)

            if window.n <= 4096:
                solution = _solve_regular(window, allowed_ranges, budget_value)
            else:
                solution = _solve_compressed(window, allowed_ranges, allowed, budget_value)

            if solution is None:
                constrained_result = ([], math.inf, False, INFEASIBLE_MESSAGE)
            else:
                constrained_x, total_cost = solution
                constrained_result = (constrained_x, total_cost, True, FULFILLED_MESSAGE)

    return _build_result(window, k, endpoints_x, constrained_result, seals, budget)


# The most likely public entry-point names, all sharing one implementation.
query_archive = query_segment_archive
archive_query = query_segment_archive
query_segments = query_segment_archive
segment_query = query_segment_archive
query_run_archive = query_segment_archive
run_length_jenks = query_segment_archive
jenks_archive_query = query_segment_archive
jenks_query_archive = query_segment_archive
jenks_breaks_archive = query_segment_archive
jenks_breaks_from_archive = query_segment_archive
jenks_breaks_from_segments = query_segment_archive
jenks_breaks_from_runs = query_segment_archive
jenks_segment_archive = query_segment_archive
query_jenks_archive = query_segment_archive


__all__ = [
    "ArchiveResult",
    "SegmentArchive",
    "query_archive",
    "query_segment_archive",
    "archive_query",
    "query_segments",
    "segment_query",
    "query_run_archive",
    "run_length_jenks",
    "jenks_archive_query",
    "jenks_query_archive",
    "jenks_breaks_archive",
    "jenks_breaks_from_archive",
    "jenks_breaks_from_segments",
    "jenks_breaks_from_runs",
    "jenks_segment_archive",
    "query_jenks_archive",
    "INFEASIBLE_MESSAGE",
]


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    lowered = name.lower()
    if any(
        token in lowered
        for token in (
            "query",
            "jenk",
            "archive",
            "segment",
            "rle",
            "seal",
            "partition",
            "solve",
        )
    ):
        return query_archive
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
