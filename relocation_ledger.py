"""Relocation ledger for offset -> Z folding in ``strict_rfc3339``.

The old collection index is ordered by the float returned from
``strict_rfc3339.rfc3339_to_timestamp`` and, for equal floats, by original
record order.  The new pipeline rewrites each label from

    2038-01-19T04:15:00.0000002+02:00

to the equivalent

    2038-01-19T02:15:00.0000002Z

keeping the fractional digits untouched, then sorts with the same library.

The library computes::

    timestamp  = calendar.timegm(whole_second_tuple)
    timestamp += float("0." + fractional_digits)
    timestamp -= offset_seconds            # only for non-Z labels

Floating-point addition is not translation invariant.  Folding the offset
into the whole second changes the binade that the fraction is added to, so a
sample can move by one or more fraction grid steps, or not at all, even when
the decimal instants are identical.  This module reproduces both library calls
with exact integer arithmetic and does not depend on the host FPU.

Deliverable
===========

``dossier.json`` describes the collection::

    {
      "segments": [
        {
          "name": "A",
          "wall_start": "2038-01-19T04:15:00",
          "offset": "+02:00",
          "digits": 7,
          "tick_start": 2,
          "step_ticks": 1,
          "count": 100000000,
          "line_start": 1,
          "line_step": 1
        }
      ]
    }

``tick_start`` and ``step_ticks`` are integers in units of ``10**-digits``
seconds.  ``line_start`` is the original line number of the first sample and
``line_step`` the original line stride.  Usage::

    python relocation_ledger.py dossier.json --report ledger.json
    python relocation_ledger.py dossier.json --line 12345
    python relocation_ledger.py dossier.json --range 1000 2000

The report records, per segment, the starting whole second, offset,
fractional pattern, exact decimal step and original line stride; per line
range it gives the number of rows that really moved, the in-place rows whose
key changed, displacement statistics, and every cross-segment equality
source.

Why ranges are not ranked row by row
====================================

Let ``D`` be the number of fractional digits and ``P = 5**D``.  Advancing a
sample's tick by ``P`` (that is, ``2**-D`` seconds) advances its float key by
exactly ``2**-D`` inside a power-of-two binade.  Choose a tick shift
``T = lcm(P, every segment step)``; every segment then advances by an integer
number of samples and every key shifts by the same amount.  Consequently, in
the interior of a segment, the "new rank minus old rank" of a sample is
exactly periodic.

The ledger locates all boundary windows (other segments' first/last keys and
binade changes), evaluates only those windows directly, and certifies each
interior piece by comparing a *full* period with the next period phase by
phase.  Only a certified piece is extended by multiplication.  Two equal
corner deltas can therefore never hide a changed interior; if a piece cannot
be certified it is split or evaluated directly, and an explicit error is
raised instead of reporting a guess.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "Segment",
    "RelocationLedger",
    "RowPosition",
    "RangeSummary",
    "Equality",
    "key_pair",
    "label_pair",
    "parse_offset",
    "format_offset",
    "parse_second",
    "format_second",
    "load_dossier",
    "build_report",
]

# Every IEEE-754 double is an integer multiple of 2**-1074.
SCALE_BITS = 1074
_ONE = 1 << SCALE_BITS

# Ranges up to this many rows are evaluated directly.
DIRECT_LIMIT = 50_000
# A certifiable period may not exceed this many rows.
PERIOD_LIMIT = 2_000_000
# The compact equality-family catalogue refuses larger layouts.
MAX_FAMILY_PERIOD = 100_000_000


# ---------------------------------------------------------------------------
# Exact IEEE-754 arithmetic
# ---------------------------------------------------------------------------


def _round_half_even(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient & 1):
        quotient += 1
    return quotient


def _fraction_binade(ticks: int, scale: int) -> int:
    """Exponent ``e`` with ``2**e <= ticks / scale < 2**(e+1)``."""

    exponent = ticks.bit_length() - scale.bit_length()
    if exponent >= 0:
        if ticks < scale << exponent:
            exponent -= 1
    elif ticks << -exponent < scale:
        exponent -= 1
    return exponent


def _decimal_double(ticks: int, digits: int) -> tuple[int, int]:
    """Return ``(significand, exponent)`` of ``float("0." + digits)``.

    The value is ``significand * 2**(exponent - 52)`` with a 53-bit
    significand, or ``(0, 0)`` for zero.
    """

    if ticks == 0:
        return 0, 0
    scale = 10 ** digits
    exponent = _fraction_binade(ticks, scale)
    significand = _round_half_even(ticks << (52 - exponent), scale)
    if significand >= 1 << 53:
        significand >>= 1
        exponent += 1
    return significand, exponent


def _round_fixed(value: int) -> int:
    """Round an exact fixed-point value to the nearest IEEE-754 double."""

    if value == 0:
        return 0
    exponent = value.bit_length() - 1 - SCALE_BITS
    drop = exponent - 52 + SCALE_BITS
    if drop <= 0:
        return value
    step = 1 << drop
    quotient, remainder = divmod(abs(value), step)
    half = step >> 1
    if remainder > half or (remainder == half and quotient & 1):
        quotient += 1
    result = quotient << drop
    return -result if value < 0 else result


def key_pair(
    wall_seconds: int, offset_seconds: int, ticks: int, digits: int
) -> tuple[int, int]:
    """Exact old and new keys for one sample, scaled by ``2**SCALE_BITS``.

    ``wall_seconds`` is the whole second of the local label before the
    fractional part, ``ticks`` the integer formed by the fractional digits,
    and ``offset_seconds`` the label's UTC offset.

    The old key is ``(float(wall) + float(frac)) - offset``; the new key is
    ``float(wall - offset) + float(frac)``.  Every operation is reproduced
    exactly, including the decimal-to-double conversion of the fraction.
    """

    scale = 10 ** digits
    carry, remainder = divmod(ticks, scale)
    wall = wall_seconds + carry
    utc = wall - offset_seconds
    significand, fraction_exponent = _decimal_double(remainder, digits)
    shift = fraction_exponent - 52 + SCALE_BITS
    if shift >= 0:
        fraction = significand << shift
    else:  # pragma: no cover - only possible with hundreds of digits
        fraction = significand >> -shift
    new_key = _round_fixed((utc << SCALE_BITS) + fraction)
    if wall == utc:
        return new_key, new_key
    wall_sum = _round_fixed((wall << SCALE_BITS) + fraction)
    old_key = _round_fixed(wall_sum - (offset_seconds << SCALE_BITS))
    return old_key, new_key


# ---------------------------------------------------------------------------
# Labels and civil time
# ---------------------------------------------------------------------------


def parse_offset(text: str | int) -> int:
    """Parse ``Z``, ``+HH:MM``/``-HH:MM`` or an integer offset in seconds."""

    if isinstance(text, int):
        return text
    if text in ("Z", "z"):
        return 0
    sign = 1 if text[0] == "+" else -1
    hours, minutes = text[1:].split(":")
    return sign * (int(hours) * 3600 + int(minutes) * 60)


def format_offset(offset_seconds: int) -> str:
    if offset_seconds == 0:
        return "Z"
    sign = "+" if offset_seconds > 0 else "-"
    magnitude = abs(offset_seconds)
    return f"{sign}{magnitude // 3600:02d}:{magnitude % 3600 // 60:02d}"


def _civil_from_epoch(seconds: int) -> tuple[int, int, int, int, int, int]:
    days, remainder = divmod(seconds, 86400)
    hour, remainder = divmod(remainder, 3600)
    minute, second = divmod(remainder, 60)
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    if month <= 2:
        year += 1
    return year, month, day, hour, minute, second


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _label_seconds(epoch_seconds: int) -> str:
    year, month, day, hour, minute, second = _civil_from_epoch(epoch_seconds)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def parse_second(text: str | int) -> int:
    """Parse ``YYYY-MM-DDTHH:MM:SS`` (or an integer epoch second)."""

    if isinstance(text, int):
        return text
    date, clock = text.split("T")
    year, month, day = (int(part) for part in date.split("-"))
    hour, minute, second = (int(part) for part in clock.split(":"))
    return _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second


def format_second(epoch_seconds: int) -> str:
    return _label_seconds(epoch_seconds)


def label_pair(
    wall_seconds: int, offset_seconds: int, ticks: int, digits: int
) -> tuple[str, str]:
    """Return the old (offset) and new (Z) labels for one sample."""

    scale = 10 ** digits
    carry, remainder = divmod(ticks, scale)
    wall = wall_seconds + carry
    utc = wall - offset_seconds
    tail = "." + str(remainder).zfill(digits) if digits else ""
    return (
        _label_seconds(wall) + tail + format_offset(offset_seconds),
        _label_seconds(utc) + tail + "Z",
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return -((-numerator) // denominator)


# ---------------------------------------------------------------------------
# Dossier model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A maximal run of samples with an affine decimal clock.

    ``wall_start``      whole local second of the first sample (epoch based)
    ``offset_seconds``  UTC offset of the local label
    ``digits``          fractional digits, 0..9
    ``tick_start``      integer value of the first fractional field
    ``step_ticks``      exact decimal step between samples, in ticks
    ``count``           number of samples
    ``line_start``      original line number of the first sample
    ``line_step``       original line stride between samples

    Consecutive ticks may carry into whole seconds; the fraction width stays
    fixed at ``digits`` and is written with leading zeroes.
    """

    name: str
    wall_start: int
    offset_seconds: int
    digits: int
    tick_start: int
    step_ticks: int
    count: int
    line_start: int = 1
    line_step: int = 1

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("count must be non-negative")
        if not 0 <= self.digits <= 9:
            raise ValueError("digits must be between 0 and 9")
        if self.step_ticks <= 0:
            raise ValueError("step_ticks must be positive")
        if self.line_step <= 0:
            raise ValueError("line_step must be positive")

    @property
    def scale(self) -> int:
        return 10 ** self.digits

    @property
    def utc_start(self) -> int:
        return self.wall_start - self.offset_seconds

    def ticks(self, index: int) -> int:
        return self.tick_start + index * self.step_ticks

    def line(self, index: int) -> int:
        return self.line_start + index * self.line_step

    def index_of_line(self, line: int) -> int | None:
        delta = line - self.line_start
        if delta < 0 or delta % self.line_step:
            return None
        index = delta // self.line_step
        return index if index < self.count else None

    def key_pair(self, index: int) -> tuple[int, int]:
        return key_pair(
            self.wall_start, self.offset_seconds, self.ticks(index), self.digits
        )

    def labels(self, index: int) -> tuple[str, str]:
        return label_pair(
            self.wall_start, self.offset_seconds, self.ticks(index), self.digits
        )

    def fractional_pattern(self) -> dict:
        if self.count == 0:
            return {"digits": self.digits, "first": "", "last": "", "step_ticks": self.step_ticks}
        scale = self.scale
        first = self.tick_start % scale
        last = self.ticks(self.count - 1) % scale
        return {
            "digits": self.digits,
            "first": f"{first:0{self.digits}d}",
            "last": f"{last:0{self.digits}d}",
            "step_ticks": self.step_ticks,
            "step_seconds": str(Fraction(self.step_ticks, scale)),
        }

    def common_start(self, digits: int) -> int:
        return self.wall_start * 10 ** digits + self.tick_start * 10 ** (
            digits - self.digits
        )

    def common_step(self, digits: int) -> int:
        return self.step_ticks * 10 ** (digits - self.digits)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowPosition:
    segment: str
    index: int
    line: int
    old_rank: int
    new_rank: int
    old_key: int
    new_key: int

    @property
    def delta(self) -> int:
        return self.new_rank - self.old_rank

    @property
    def moved(self) -> bool:
        return self.delta != 0

    @property
    def key_changed(self) -> bool:
        return self.old_key != self.new_key

    @property
    def in_place_key_changed(self) -> bool:
        return self.delta == 0 and self.key_changed

    def to_json(self) -> dict:
        return {
            "segment": self.segment,
            "index": self.index,
            "line": self.line,
            "old_rank_1based": self.old_rank + 1,
            "new_rank_1based": self.new_rank + 1,
            "position_delta": self.delta,
            "key_changed": self.key_changed,
            "in_place_key_changed": self.in_place_key_changed,
            "old_key_exact": _pow2_text(self.old_key),
            "new_key_exact": _pow2_text(self.new_key),
        }


@dataclass(frozen=True)
class RangeSummary:
    line_start: int
    line_end: int
    rows: int
    moved: int
    max_displacement: int
    sum_abs_displacement: int
    net_displacement: int
    in_place_key_changed: int
    identical: int

    @property
    def mean_abs_displacement(self) -> float:
        return self.sum_abs_displacement / self.rows if self.rows else 0.0

    def to_json(self) -> dict:
        data = asdict(self)
        data["mean_abs_displacement"] = self.mean_abs_displacement
        return data


@dataclass(frozen=True)
class Equality:
    """One cross-segment equality source.

    ``side`` is ``"old"`` or ``"new"``.  Line ranges are inclusive; the
    ``count_a``/``count_b`` fields give how many samples on each side share
    this key.
    """

    side: str
    key: str
    segment_a: str
    segment_b: str
    line_a_start: int
    line_a_end: int
    line_b_start: int
    line_b_end: int
    count_a: int
    count_b: int

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Counting by exact inverse
# ---------------------------------------------------------------------------


class _Counter:
    """Count rows of a monotone segment before a target ``(key, line)``.

    The key is monotone in the tick, and the tick whose real value crosses the
    target is found from the target exactly.  Only a tiny window around that
    estimate is searched (the width is the double rounding error, not the
    segment length), so counting never iterates over the segment.
    """

    __slots__ = ("segment", "old", "scale", "base_offset", "step", "slot")

    def __init__(self, segment: Segment, old: bool, digits: int) -> None:
        self.segment = segment
        self.old = old
        self.slot = 0 if old else 1
        self.scale = 10 ** digits
        self.base_offset = segment.common_start(digits)
        self.step = segment.common_step(digits)

    def _key_at_tick(self, tick: int) -> int:
        wall, remainder = divmod(tick, self.scale)
        return key_pair(wall, self.segment.offset_seconds, remainder, self.segment.digits)[
            self.slot
        ]

    def _boundary_tick(self, target_key: int) -> int:
        """Smallest tick whose key is >= ``target_key``."""

        segment = self.segment
        offset = segment.offset_seconds << SCALE_BITS
        # Both key paths denote ``wall + tick/scale - offset``; the reverse
        # estimate is the corresponding common tick.
        estimate = ((target_key + offset) * self.scale) >> SCALE_BITS
        value = estimate // self.scale
        exponent = max(
            abs(value).bit_length(), abs(value - segment.offset_seconds).bit_length()
        )
        if exponent:
            window = (self.scale << exponent) >> 52
        else:
            window = 0
        window += 8
        low = estimate - window
        high = estimate + window + 2
        while low < high:
            middle = (low + high) // 2
            if self._key_at_tick(middle) < target_key:
                low = middle + 1
            else:
                high = middle
        return low

    def count_less(self, key: int, line: int) -> int:
        segment = self.segment
        if segment.count == 0:
            return 0
        first = self._boundary_tick(key)
        first_index = _ceil_div(first - self.base_offset, self.step)
        first_index = min(max(first_index, 0), segment.count)
        if first_index == segment.count:
            return segment.count
        after = self._boundary_tick(_successor(key))
        after_index = _ceil_div(after - self.base_offset, self.step)
        after_index = min(max(after_index, first_index), segment.count)
        lines_before = _ceil_div(line - segment.line(first_index), segment.line_step)
        lines_before = min(max(lines_before, 0), after_index - first_index)
        return first_index + lines_before


def _successor(key: int) -> int:
    """Smallest double strictly greater than the scaled key ``key``."""

    if key == 0:
        return 1
    magnitude = abs(key)
    exponent = magnitude.bit_length() - 1
    ulp = 1 << (exponent - 52) if exponent >= 52 else 1
    return key + ulp


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class RelocationLedger:
    """Positions, range summaries and equality sources for a dossier."""

    def __init__(self, segments: Sequence[Segment]) -> None:
        if not segments:
            raise ValueError("at least one segment is required")
        self.segments = list(segments)
        names = [segment.name for segment in self.segments]
        if len(set(names)) != len(names):
            raise ValueError("segment names must be unique")
        self.digits = max((segment.digits for segment in self.segments), default=0)
        self._counters = {
            (segment.name, old): _Counter(segment, old, self.digits)
            for segment in self.segments
            for old in (True, False)
        }
        self._period_cache: dict[str, int] = {}

    # -- geometry ---------------------------------------------------------

    @property
    def scale(self) -> int:
        return 10 ** self.digits

    @property
    def quantum(self) -> int:
        return 1 << (SCALE_BITS - self.digits)

    def segment(self, name: str) -> Segment:
        for segment in self.segments:
            if segment.name == name:
                return segment
        raise KeyError(name)

    def locate_line(self, line: int) -> tuple[Segment, int]:
        for segment in self.segments:
            index = segment.index_of_line(line)
            if index is not None:
                return segment, index
        raise KeyError(f"line {line} is not in any segment")

    def old_key(self, segment: Segment | str, index: int) -> int:
        if isinstance(segment, str):
            segment = self.segment(segment)
        return segment.key_pair(index)[0]

    def new_key(self, segment: Segment | str, index: int) -> int:
        if isinstance(segment, str):
            segment = self.segment(segment)
        return segment.key_pair(index)[1]

    # -- per-row positions -------------------------------------------------

    def rank_pair(self, segment: Segment | str, index: int) -> tuple[int, int]:
        """Actual 0-based old and new positions of one row."""

        if isinstance(segment, str):
            segment = self.segment(segment)
        old_key, new_key = segment.key_pair(index)
        line = segment.line(index)
        old_rank = index
        new_rank = index
        for other in self.segments:
            if other is segment or other.count == 0:
                continue
            old_rank += self._counters[(other.name, True)].count_less(old_key, line)
            new_rank += self._counters[(other.name, False)].count_less(new_key, line)
        return old_rank, new_rank

    def position(self, segment: Segment | str, index: int) -> RowPosition:
        if isinstance(segment, str):
            segment = self.segment(segment)
        if not 0 <= index < segment.count:
            raise IndexError("sample index out of range")
        old_rank, new_rank = self.rank_pair(segment, index)
        old_key, new_key = segment.key_pair(index)
        return RowPosition(
            segment=segment.name,
            index=index,
            line=segment.line(index),
            old_rank=old_rank,
            new_rank=new_rank,
            old_key=old_key,
            new_key=new_key,
        )

    def position_of_line(self, line: int) -> RowPosition:
        segment, index = self.locate_line(line)
        return self.position(segment, index)

    # -- periodicity -------------------------------------------------------

    def layout_shift(self) -> int:
        """Common tick shift advancing every segment by an integer index."""

        shift = 5 ** self.digits
        for segment in self.segments:
            if segment.count:
                shift = math.lcm(shift, segment.common_step(self.digits))
        return shift

    def index_period(self, segment: Segment) -> int:
        cached = self._period_cache.get(segment.name)
        if cached is None:
            cached = self.layout_shift() // segment.common_step(self.digits)
            self._period_cache[segment.name] = cached
        return cached

    def _deltas(self, target: Segment, first: int, length: int) -> tuple[list[int], list[bool]]:
        """``new_rank - old_rank`` and key-change flags for a run of rows."""

        deltas: list[int] = []
        changed: list[bool] = []
        for index in range(first, first + length):
            old_rank, new_rank = self.rank_pair(target, index)
            deltas.append(new_rank - old_rank)
            old_key, new_key = target.key_pair(index)
            changed.append(old_key != new_key)
        return deltas, changed

    def _break_indices(self, target: Segment, first: int, stop: int) -> list[int]:
        """Target indices where any segment changes power-of-two binade.

        Inside a binade of every segment's local and UTC whole second, a tick
        shift moves every key by exactly the same constant.  Equal keys stay
        equal, so the tied line order is unchanged and the rank-delta pattern
        is exactly periodic.  These boundaries are the only places where that
        guarantee can fail; no assumption is made across them.
        """

        points = {first, stop}
        scale = self.scale
        start = target.common_start(self.digits)
        step = target.common_step(self.digits)
        for segment in self.segments:
            if not segment.count:
                continue
            last_tick = segment.common_start(self.digits) + (
                segment.count - 1
            ) * segment.common_step(self.digits)
            wall_end = last_tick // scale
            utc_end = wall_end - segment.offset_seconds
            for boundary in _binade_boundaries(segment.wall_start, wall_end):
                tick = boundary * scale
                index = _ceil_div(tick - start, step)
                if first < index < stop:
                    points.add(index)
            for boundary in _binade_boundaries(segment.utc_start, utc_end):
                tick = (boundary + segment.offset_seconds) * scale
                index = _ceil_div(tick - start, step)
                if first < index < stop:
                    points.add(index)
        return sorted(points)

    # -- range summaries ---------------------------------------------------

    def summarize_lines(self, line_start: int, line_end: int) -> RangeSummary:
        if line_end < line_start:
            raise ValueError("line_end must not be before line_start")
        total = RangeSummary(
            line_start=line_start,
            line_end=line_end,
            rows=0,
            moved=0,
            max_displacement=0,
            sum_abs_displacement=0,
            net_displacement=0,
            in_place_key_changed=0,
            identical=0,
        )
        for segment in self.segments:
            if segment.count == 0:
                continue
            low = max(line_start, segment.line_start)
            high = min(line_end, segment.line(segment.count - 1))
            if low > high:
                continue
            first = max(_ceil_div(low - segment.line_start, segment.line_step), 0)
            last = min((high - segment.line_start) // segment.line_step, segment.count - 1)
            total = _merge(total, self._summarize_indices(segment, first, last + 1))
        return total

    def _summarize_indices(self, segment: Segment, first: int, stop: int) -> RangeSummary:
        count = stop - first
        if count <= 0:
            return RangeSummary(-1, -1, 0, 0, 0, 0, 0, 0, 0)
        if count <= DIRECT_LIMIT:
            deltas, changed = self._deltas(segment, first, count)
            return _summarize_blocks(
                deltas, changed, 1, segment.line(first), segment.line(stop - 1)
            )
        total = RangeSummary(-1, -1, 0, 0, 0, 0, 0, 0, 0)
        points = self._break_indices(segment, first, stop)
        for low, high in zip(points, points[1:]):
            if low < high:
                total = _merge(total, self._summarize_piece(segment, low, high))
        total = RangeSummary(
            line_start=segment.line(first),
            line_end=segment.line(stop - 1),
            rows=total.rows,
            moved=total.moved,
            max_displacement=total.max_displacement,
            sum_abs_displacement=total.sum_abs_displacement,
            net_displacement=total.net_displacement,
            in_place_key_changed=total.in_place_key_changed,
            identical=total.identical,
        )
        if total.rows != count:
            raise AssertionError("summary lost rows")
        return total

    def _summarize_piece(self, segment: Segment, first: int, stop: int) -> RangeSummary:
        """Summarise a range that lies inside one binade of every segment."""

        count = stop - first
        period = self.index_period(segment)
        if count <= DIRECT_LIMIT or count < period:
            deltas, changed = self._deltas(segment, first, count)
            return _summarize_blocks(
                deltas, changed, 1, segment.line(first), segment.line(stop - 1)
            )
        if period > PERIOD_LIMIT:
            raise ValueError(
                f"segment {segment.name}: period {period} exceeds "
                f"PERIOD_LIMIT={PERIOD_LIMIT}; raise the limit deliberately "
                f"or feed a shorter range"
            )
        # Verify the first period against the second one phase by phase.  This
        # checks the interior, not just the endpoints.
        deltas, changed = self._deltas(segment, first, period)
        if count >= 2 * period:
            second, _ = self._deltas(segment, first + period, period)
            if second != deltas:
                # The binade analysis says this cannot happen inside a piece;
                # if it does, split rather than trusting the pattern.
                middle = first + count // 2
                return _merge(
                    self._summarize_piece(segment, first, middle),
                    self._summarize_piece(segment, middle, stop),
                )
        blocks, tail = divmod(count, period)
        if tail:
            # Verify the tail's phases against the candidate pattern before
            # using them; when fewer than two full periods exist this is the
            # only interior check available.
            tail_deltas, tail_changed = self._deltas(
                segment, first + blocks * period, tail
            )
            if tail_deltas != deltas[:tail] or tail_changed != changed[:tail]:
                middle = first + count // 2
                return _merge(
                    self._summarize_piece(segment, first, middle),
                    self._summarize_piece(segment, middle, stop),
                )
        summary = _summarize_blocks(
            deltas,
            changed,
            blocks,
            segment.line(first),
            segment.line(first + blocks * period - 1),
        )
        if tail:
            tail_summary = _summarize_blocks(
                deltas[:tail],
                changed[:tail],
                1,
                segment.line(first + blocks * period),
                segment.line(stop - 1),
            )
            summary = _merge(summary, tail_summary)
        return summary

    def summarize_segment(self, segment: Segment | str) -> RangeSummary:
        if isinstance(segment, str):
            segment = self.segment(segment)
        if segment.count == 0:
            return RangeSummary(
                segment.line_start, segment.line_start - 1, 0, 0, 0, 0, 0, 0, 0
            )
        return self._summarize_indices(segment, 0, segment.count)

    # -- equalities --------------------------------------------------------

    def equalities(
        self,
        line_start: int | None = None,
        line_end: int | None = None,
        *,
        max_rows: int = 500_000,
    ) -> list[Equality]:
        """Every cross-segment key equality source in a line range.

        Rows with equal keys are coalesced into runs; each record names the
        two segments and the inclusive original-line ranges sharing the key.
        Inputs above ``max_rows`` raise instead of truncating.
        """

        if line_start is None:
            line_start = min((segment.line_start for segment in self.segments), default=0)
        if line_end is None:
            line_end = max(
                (
                    segment.line(segment.count - 1)
                    for segment in self.segments
                    if segment.count
                ),
                default=line_start,
            )
        entries: list[tuple[int, int, str, int, int]] = []
        budget = 0
        for segment in self.segments:
            if segment.count == 0:
                continue
            low = max(line_start, segment.line_start)
            high = min(line_end, segment.line(segment.count - 1))
            if low > high:
                continue
            first = max(_ceil_div(low - segment.line_start, segment.line_step), 0)
            last = min((high - segment.line_start) // segment.line_step, segment.count - 1)
            index = first
            while index <= last:
                old_key, new_key = segment.key_pair(index)
                old_end = self._run_end(segment, index, old_key, True, last)
                new_end = self._run_end(segment, index, new_key, False, last)
                end = min(old_end, new_end)
                entries.append(
                    (
                        segment.line(index),
                        segment.line(end),
                        segment.name,
                        old_key,
                        new_key,
                    )
                )
                budget += end - index + 1
                if budget > max_rows:
                    raise ValueError(
                        "range needs more than max_rows rows to enumerate "
                        "equality sources; narrow the line range"
                    )
                index = end + 1
        result: list[Equality] = []
        for side, slot in (("old", 3), ("new", 4)):
            groups: dict[int, list[tuple[int, int, str]]] = {}
            for first_line, last_line, name, old_key, new_key in entries:
                groups.setdefault(old_key if slot == 3 else new_key, []).append(
                    (first_line, last_line, name)
                )
            for key, group in sorted(groups.items()):
                names = sorted({name for _, _, name in group})
                if len(names) < 2:
                    continue
                for left_name in names:
                    for right_name in names:
                        if left_name >= right_name:
                            continue
                        left = [(a, b) for a, b, name in group if name == left_name]
                        right = [(a, b) for a, b, name in group if name == right_name]
                        result.append(
                            Equality(
                                side=side,
                                key=_key_text(key),
                                segment_a=left_name,
                                segment_b=right_name,
                                line_a_start=min(a for a, _ in left),
                                line_a_end=max(b for _, b in left),
                                line_b_start=min(a for a, _ in right),
                                line_b_end=max(b for _, b in right),
                                count_a=sum(b - a + 1 for a, b in left),
                                count_b=sum(b - a + 1 for a, b in right),
                            )
                        )
        return result

    def equality_families(self, *, max_runs: int = 2_000_000) -> dict:
        """Compact exact catalogue of every cross-segment equality.

        Index one aligned block on absolute decimal ticks.  For a pair of
        segments let ``LP``/``RP`` be their index advances over one layout
        period.  An equality family is a set of index runs ``i0..i1`` plus a
        constant index offset ``d`` such that, for every ``i`` in a run::

            A[i + k*LP]  ==  B[i + d + k*RP]

        for ``block_min <= k <= block_max``.  The shared key at block ``k``
        is the block-0 key plus ``k * key_step``, where ``key_step`` is exact.
        """

        period = self.layout_shift()
        if period <= 0:
            raise ValueError("layout has no positive period")
        if period > MAX_FAMILY_PERIOD:
            raise ValueError(
                f"layout period {period} ticks exceeds MAX_FAMILY_PERIOD; "
                "use exact range enumeration"
            )
        # ``period`` is a tick count on the 10**-self.digits grid.
        assert (period * _ONE) % (10 ** self.digits) == 0
        key_step = period * _ONE // (10 ** self.digits)
        # Verify the translation on every phase used, for both key paths.
        for segment in self.segments:
            if not segment.count:
                continue
            step = segment.common_step(self.digits)
            advance = period // step
            span = min(advance, segment.count - advance)
            if span <= 0:
                continue
            # Verify every phase of the advanced block, and sample the far
            # end of the segment: the full translation, not just one corner.
            checks = set(range(span)) if span <= 200_000 else set(range(0, span, span // 200_000))
            checks.add(span - 1)
            for index in checks:
                for slot in (0, 1):
                    before = segment.key_pair(index)[slot]
                    after = segment.key_pair(index + advance)[slot]
                    if after - before != key_step:
                        raise ValueError(
                            f"segment {segment.name}: key does not advance by the "
                            f"layout quantum at index {index}; use exact range "
                            "enumeration"
                        )
        grouped_families: dict[tuple, dict] = {}
        run_total = 0
        for left in self.segments:
            if not left.count:
                continue
            for right in self.segments:
                if right is left or not right.count or left.name >= right.name:
                    continue
                left_period = period // left.common_step(self.digits)
                right_period = period // right.common_step(self.digits)
                left_span = min(left_period, left.count)
                right_span = min(right_period, right.count)
                for side, slot in (("old", 0), ("new", 1)):
                    base_a = left.common_start(self.digits)
                    base_b = right.common_start(self.digits)
                    left_step = left.common_step(self.digits)
                    right_step = right.common_step(self.digits)
                    right_keys = [
                        self._tick_key(right, base_b + index * right_step, slot)
                        for index in range(right_span)
                    ]
                    offset_runs: dict[int, list[tuple[int, int]]] = {}
                    for index in range(left_span):
                        key = self._tick_key(left, base_a + index * left_step, slot)
                        low = _bisect_left(right_keys, key)
                        high = _bisect_right(right_keys, key)
                        for other in range(low, high):
                            offset_runs.setdefault(other - index, []).append(index)
                    for offset, indices in sorted(offset_runs.items()):
                        for run_start, run_end in _runs(indices):
                            low_k = max(
                                0,
                                _ceil_div(-run_start, left_period),
                                _ceil_div(-run_end, left_period),
                                _ceil_div(-(run_start + offset), right_period),
                                _ceil_div(-(run_end + offset), right_period),
                            )
                            high_k = min(
                                (left.count - 1 - run_end) // left_period,
                                (right.count - 1 - run_end - offset) // right_period,
                            )
                            if low_k > high_k:
                                continue
                            run_total += 1
                            if run_total > max_runs:
                                raise ValueError(
                                    "equality run catalogue exceeds max_runs"
                                )
                            key = (
                                left.name,
                                right.name,
                                side,
                                offset,
                                low_k,
                                high_k,
                                left_period,
                                right_period,
                            )
                            family = grouped_families.get(key)
                            if family is None:
                                family = {
                                    "segment_a": left.name,
                                    "segment_b": right.name,
                                    "side": side,
                                    "index_offset": offset,
                                    "block_min": low_k,
                                    "block_max": high_k,
                                    "blocks": high_k - low_k + 1,
                                    "a_period": left_period,
                                    "b_period": right_period,
                                    "period_ticks": period,
                                    "key_step": _pow2_text(key_step),
                                    "runs": [],
                                }
                                grouped_families[key] = family
                            family["runs"].append([run_start, run_end])
        families = list(grouped_families.values())
        for family in families:
            runs = family["runs"]
            count = sum(end - start + 1 for start, end in runs)
            family["run_count"] = len(runs)
            family["count_a"] = count * family["blocks"]
            family["count_b"] = count * family["blocks"]
            family["pairs"] = count * family["blocks"]
            left = self.segment(family["segment_a"])
            right = self.segment(family["segment_b"])
            family["a_line_start"] = left.line(runs[0][0])
            family["a_line_end"] = left.line(runs[-1][1])
            family["b_line_start"] = right.line(runs[0][0] + family["index_offset"])
            family["b_line_end"] = right.line(runs[-1][1] + family["index_offset"])
        return {
            "period_ticks": period,
            "key_step": _pow2_text(key_step),
            "families": families,
            "family_count": len(families),
            "total_pairs": sum(item["pairs"] for item in families),
            "note": (
                "in block k: A index = run_index + k*a_period, B index = "
                "run_index + index_offset + k*b_period; key = block0_key + "
                "k*key_step, block0_key read from the labels of the run start"
            ),
        }

    def _tick_key(self, segment: Segment, tick: int, slot: int) -> int:
        wall, remainder = divmod(tick, self.scale)
        return key_pair(wall, segment.offset_seconds, remainder, segment.digits)[slot]

    def _run_end(
        self, segment: Segment, index: int, key: int, old: bool, limit: int
    ) -> int:
        low, high = index, limit + 1
        slot = 0 if old else 1
        while low < high:
            middle = (low + high) // 2
            if segment.key_pair(middle)[slot] <= key:
                low = middle + 1
            else:
                high = middle
        return low - 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bisect_left(values: Sequence[int], target: int) -> int:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] < target:
            low = middle + 1
        else:
            high = middle
    return low


def _bisect_right(values: Sequence[int], target: int) -> int:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] <= target:
            low = middle + 1
        else:
            high = middle
    return low


def _runs(indices: Sequence[int]):
    if not indices:
        return
    start = previous = indices[0]
    for value in indices[1:]:
        if value == previous + 1:
            previous = value
            continue
        yield start, previous
        start = previous = value
    yield start, previous




def _binade_boundaries(first_second: int, last_second: int) -> list[int]:
    """Power-of-two rounding-grid changes in ``[first_second, last_second]``.

    The binary exponent of a value changes at ``+2**k`` and, for negatives,
    where the magnitude crosses ``2**k``.
    """

    if last_second < first_second:
        return []
    boundaries: set[int] = set()
    exponent = 0
    while (1 << exponent) <= last_second:
        value = 1 << exponent
        if value > first_second:
            boundaries.add(value)
        exponent += 1
    exponent = 0
    while -(1 << exponent) >= first_second:
        value = -(1 << exponent)
        if value < last_second:
            boundaries.add(value)
        exponent += 1
    return sorted(boundaries)


def _merge(left: RangeSummary, right: RangeSummary) -> RangeSummary:
    if left.rows == 0:
        return right
    if right.rows == 0:
        return left
    return RangeSummary(
        line_start=left.line_start,
        line_end=right.line_end,
        rows=left.rows + right.rows,
        moved=left.moved + right.moved,
        max_displacement=max(left.max_displacement, right.max_displacement),
        sum_abs_displacement=left.sum_abs_displacement + right.sum_abs_displacement,
        net_displacement=left.net_displacement + right.net_displacement,
        in_place_key_changed=left.in_place_key_changed + right.in_place_key_changed,
        identical=left.identical + right.identical,
    )


def _summarize_blocks(
    deltas: Sequence[int],
    changed: Sequence[bool],
    blocks: int,
    line_start: int,
    line_end: int,
) -> RangeSummary:
    """Aggregate one verified phase pattern over identical blocks.

    Every phase of the period is included, so two equal corner deltas cannot
    hide a changed interior.
    """

    rows = len(deltas) * blocks
    moved = in_place = identical = 0
    max_disp = 0
    abs_sum = 0
    net = 0
    for delta, key_changed in zip(deltas, changed):
        if delta:
            moved += blocks
            abs_sum += abs(delta) * blocks
            net += delta * blocks
            max_disp = max(max_disp, abs(delta))
        elif key_changed:
            in_place += blocks
        else:
            identical += blocks
    return RangeSummary(
        line_start=line_start,
        line_end=line_end,
        rows=rows,
        moved=moved,
        max_displacement=max_disp,
        sum_abs_displacement=abs_sum,
        net_displacement=net,
        in_place_key_changed=in_place,
        identical=identical,
    )


def _key_text(key: int) -> str:
    """Stable byte-prefixed hex form, used when a compact form is not wanted."""

    sign = "1" if key < 0 else "0"
    magnitude = abs(key)
    width = (magnitude.bit_length() + 8) // 8 or 1
    return sign + magnitude.to_bytes(width, "big").hex()


def _pow2_text(key: int) -> str:
    """Exact compact text ``odd * 2**exponent`` for a scaled key."""

    if key == 0:
        return "0"
    magnitude = abs(key)
    shift = (magnitude & -magnitude).bit_length() - 1
    odd = magnitude >> shift
    sign = "-" if key < 0 else ""
    return f"{sign}{odd}*2^{shift - SCALE_BITS}"


# ---------------------------------------------------------------------------
# Dossier loading and reporting
# ---------------------------------------------------------------------------


def load_dossier(path: str | Path) -> list[Segment]:
    data = json.loads(Path(path).read_text())
    segments = []
    for raw in data["segments"]:
        segments.append(
            Segment(
                name=raw["name"],
                wall_start=parse_second(raw["wall_start"]),
                offset_seconds=parse_offset(raw.get("offset", "Z")),
                digits=int(raw.get("digits", 0)),
                tick_start=int(raw.get("tick_start", 0)),
                step_ticks=int(raw.get("step_ticks", 1)),
                count=int(raw["count"]),
                line_start=int(raw.get("line_start", 1)),
                line_step=int(raw.get("line_step", 1)),
            )
        )
    return segments


def build_report(
    ledger: RelocationLedger,
    ranges: Iterable[tuple[int, int]] = (),
    *,
    include_equalities: bool = True,
) -> dict:
    segments = []
    for segment in ledger.segments:
        summary = ledger.summarize_segment(segment)
        segments.append(
            {
                "name": segment.name,
                "wall_start": _label_seconds(segment.wall_start),
                "utc_start": _label_seconds(segment.utc_start),
                "offset": format_offset(segment.offset_seconds),
                "offset_seconds": segment.offset_seconds,
                "wall_start_epoch": segment.wall_start,
                "fraction": segment.fractional_pattern(),
                "count": segment.count,
                "line_start": segment.line_start,
                "line_step": segment.line_step,
                "first_line": segment.line(0) if segment.count else None,
                "last_line": segment.line(segment.count - 1) if segment.count else None,
                "first_sample": segment.labels(0) if segment.count else None,
                "last_sample": (
                    segment.labels(segment.count - 1) if segment.count else None
                ),
                "summary": summary.to_json(),
            }
        )
    report = {"segments": segments, "ranges": []}
    for line_start, line_end in ranges:
        report["ranges"].append(ledger.summarize_lines(line_start, line_end).to_json())
    if include_equalities:
        try:
            report["equalities"] = ledger.equality_families()
            report["equalities"]["mode"] = "families"
        except ValueError as error:
            report["equalities"] = {
                "mode": "ranges",
                "reason": str(error),
                "items": [item.to_json() for item in ledger.equalities()],
            }
    return report


def _sample_row(ledger: RelocationLedger, segment: Segment, index: int) -> dict:
    position = ledger.position(segment, index)
    old_label, new_label = segment.labels(index)
    data = position.to_json()
    data["old_label"] = old_label
    data["new_label"] = new_label
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relocation ledger for strict_rfc3339 offset->Z folding."
    )
    parser.add_argument("dossier", help="dossier JSON")
    parser.add_argument("--report", help="write the full report JSON here")
    parser.add_argument("--line", type=int, help="report one original line")
    parser.add_argument(
        "--range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="summarise an inclusive original-line range",
    )
    parser.add_argument(
        "--sample",
        help="print rows: SEGMENT:INDEX or SEGMENT:FIRST:LAST",
    )
    parser.add_argument("--no-equalities", action="store_true")
    args = parser.parse_args(argv)

    ledger = RelocationLedger(load_dossier(args.dossier))
    if args.line is not None:
        print(json.dumps(ledger.position_of_line(args.line).to_json(), ensure_ascii=False))
    if args.range is not None:
        print(
            json.dumps(
                ledger.summarize_lines(args.range[0], args.range[1]).to_json(),
                ensure_ascii=False,
            )
        )
    if args.sample:
        parts = args.sample.split(":")
        segment = ledger.segment(parts[0])
        first = int(parts[1])
        last = int(parts[2]) if len(parts) > 2 else first
        for index in range(first, last + 1):
            print(json.dumps(_sample_row(ledger, segment, index), ensure_ascii=False))
    if args.report:
        ranges = [tuple(args.range)] if args.range else []
        report = build_report(ledger, ranges, include_equalities=not args.no_equalties)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"wrote {args.report}")
    if not any((args.line is not None, args.range, args.sample, args.report)):
        parser.error("nothing to do; pass --report, --line, --range or --sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
