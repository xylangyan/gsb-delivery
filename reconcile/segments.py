"""
Compressed sequence segments.

A Segment is a re-iterable, sized, indexable sequence that does not
materialize one record per element. The archive keeps hundreds of millions
of repeated fixed item-numbers (Repeat) and consecutive numberings
(RangeSeq, possibly reversed) as segments; several segments may be chained,
staggered and overlapping in value space (Chain). Small or irregular
sequences use Materialized.

Every segment can also be viewed as a one-shot stream via .stream(), which
is what the second transcript uses when a batch's form was recorded as
"stream" instead of "list".
"""
import itertools as it
from bisect import bisect_right
from typing import Any, Iterator, List, Tuple


class Segment:
    """Abstract compressed sequence. Subclasses must stay re-iterable."""

    def __len__(self) -> int:
        raise NotImplementedError

    def value_at(self, index: int) -> Any:
        raise NotImplementedError

    def sub(self, lo: int, hi: int) -> "Segment":
        """Return the contiguous view self[lo:hi] as a Segment."""
        raise NotImplementedError

    def __iter__(self) -> Iterator[Any]:
        raise NotImplementedError

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step == 1:
                return self.sub(start, stop)
            return Materialized(tuple(self.value_at(i) for i in range(start, stop, step)))
        index = int(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(f"index {index} out of range for segment of length {len(self)}")
        return self.value_at(index)

    def stream(self) -> Iterator[Any]:
        """A one-shot view: each call makes an independent iterator, but the
        returned iterator itself is exhausted as it is consumed."""
        return iter(self)


class Repeat(Segment):
    """A fixed item-number repeated `count` times."""

    __slots__ = ("value", "count")

    def __init__(self, value: Any, count: int):
        if count < 0:
            raise ValueError("count must be >= 0")
        self.value = value
        self.count = count

    def __len__(self) -> int:
        return self.count

    def value_at(self, index: int) -> Any:
        return self.value

    def sub(self, lo: int, hi: int) -> Segment:
        return Repeat(self.value, hi - lo)

    def __iter__(self) -> Iterator[Any]:
        return it.repeat(self.value, self.count)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Repeat)
            and self.value == other.value
            and self.count == other.count
        )

    def __repr__(self) -> str:
        return f"Repeat({self.value!r}, {self.count})"


class RangeSeq(Segment):
    """Consecutive numbering, possibly reversed (negative step)."""

    __slots__ = ("_range",)

    def __init__(self, start: int, stop: int = None, step: int = 1):
        if stop is None:
            start, stop = 0, start
        self._range = range(start, stop, step)

    @property
    def start(self) -> int:
        return self._range.start

    @property
    def stop(self) -> int:
        return self._range.stop

    @property
    def step(self) -> int:
        return self._range.step

    def __len__(self) -> int:
        return len(self._range)

    def value_at(self, index: int) -> int:
        return self._range[index]

    def sub(self, lo: int, hi: int) -> Segment:
        return RangeSeq._from(self._range[lo:hi])

    @staticmethod
    def _from(r: range) -> "RangeSeq":
        obj = RangeSeq.__new__(RangeSeq)
        obj._range = r
        return obj

    def __iter__(self) -> Iterator[int]:
        return iter(self._range)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RangeSeq) and self._range == other._range

    def __repr__(self) -> str:
        return f"RangeSeq({self.start}, {self.stop}, {self.step})"


class Materialized(Segment):
    """An explicitly held tuple of values, for small or irregular sequences."""

    __slots__ = ("values",)

    def __init__(self, values):
        self.values = tuple(values)

    def __len__(self) -> int:
        return len(self.values)

    def value_at(self, index: int) -> Any:
        return self.values[index]

    def sub(self, lo: int, hi: int) -> Segment:
        return Materialized(self.values[lo:hi])

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Materialized) and self.values == other.values

    def __repr__(self) -> str:
        return f"Materialized({self.values!r})"


class Chain(Segment):
    """Concatenation of segments. Segments may be reversed, staggered and
    overlapping in value space; a Chain just glues them in order."""

    __slots__ = ("parts", "_cum")

    def __init__(self, parts):
        flat: List[Segment] = []
        for part in parts:
            if isinstance(part, Chain):
                flat.extend(part.parts)
            elif len(part) > 0:
                flat.append(part)
        self.parts: Tuple[Segment, ...] = tuple(flat)
        cum = [0]
        for part in self.parts:
            cum.append(cum[-1] + len(part))
        self._cum = cum

    def __len__(self) -> int:
        return self._cum[-1]

    def _locate(self, index: int) -> Tuple[Segment, int]:
        slot = bisect_right(self._cum, index) - 1
        return self.parts[slot], index - self._cum[slot]

    def value_at(self, index: int) -> Any:
        part, offset = self._locate(index)
        return part.value_at(offset)

    def sub(self, lo: int, hi: int) -> Segment:
        if lo >= hi:
            return Materialized(())
        pieces = []
        first = bisect_right(self._cum, lo) - 1
        last = bisect_right(self._cum, hi - 1) - 1
        for slot in range(first, last + 1):
            part = self.parts[slot]
            part_lo = max(lo - self._cum[slot], 0)
            part_hi = min(hi - self._cum[slot], len(part))
            pieces.append(part.sub(part_lo, part_hi))
        if len(pieces) == 1:
            return pieces[0]
        return Chain(pieces)

    def __iter__(self) -> Iterator[Any]:
        return it.chain.from_iterable(self.parts)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Chain) and self.parts == other.parts

    def __repr__(self) -> str:
        return f"Chain({self.parts!r})"


def coerce_segment(values) -> Segment:
    if isinstance(values, Segment):
        return values
    return Materialized(tuple(values))


def _generic_value_diffs(a: Segment, b: Segment) -> Iterator[Tuple[int, int]]:
    """Streaming fallback: merge adjacent differing positions into intervals.
    Uses O(1) memory and never stores a record per element."""
    run_start = None
    pos = 0
    for va, vb in zip(iter(a), iter(b)):
        if va != vb:
            if run_start is None:
                run_start = pos
        elif run_start is not None:
            yield (run_start, pos)
            run_start = None
        pos += 1
    if run_start is not None:
        yield (run_start, pos)


def _single_equal_point(n: int, i0) -> Iterator[Tuple[int, int]]:
    """Diff intervals for two length-n sequences equal only at position i0
    (or nowhere, if i0 is None)."""
    if i0 is None:
        if n:
            yield (0, n)
        return
    if i0 > 0:
        yield (0, i0)
    if i0 + 1 < n:
        yield (i0 + 1, n)


def value_diffs(a: Segment, b: Segment) -> Iterator[Tuple[int, int]]:
    """
    Yield (start, end) intervals, relative to the aligned start of `a` and
    `b`, where their values differ. `a` and `b` must have equal length.

    Atomic segment pairs are compared arithmetically, so this stays O(1)
    even for segments of hundreds of millions of elements.
    """
    assert len(a) == len(b), "value_diffs requires equal lengths"
    n = len(a)
    if n == 0:
        return
    if isinstance(a, Chain):
        offset = 0
        for part in a.parts:
            plen = len(part)
            for s, e in value_diffs(part, b.sub(offset, offset + plen)):
                yield (offset + s, offset + e)
            offset += plen
        return
    if isinstance(b, Chain):
        offset = 0
        for part in b.parts:
            plen = len(part)
            for s, e in value_diffs(a.sub(offset, offset + plen), part):
                yield (offset + s, offset + e)
            offset += plen
        return
    if isinstance(a, Repeat) and isinstance(b, Repeat):
        if a.value != b.value:
            yield (0, n)
        return
    if isinstance(a, Repeat) and isinstance(b, RangeSeq):
        delta = a.value - b.start
        i0 = delta // b.step if delta % b.step == 0 and 0 <= delta // b.step < n else None
        yield from _single_equal_point(n, i0)
        return
    if isinstance(a, RangeSeq) and isinstance(b, Repeat):
        for s, e in value_diffs(b, a):
            yield (s, e)
        return
    if isinstance(a, RangeSeq) and isinstance(b, RangeSeq):
        # a[i] == b[i]  <=>  i * (a.step - b.step) == b.start - a.start
        p = a.step - b.step
        d = b.start - a.start
        if p == 0:
            if d != 0:
                yield (0, n)
            return
        i0 = d // p if d % p == 0 and 0 <= d // p < n else None
        yield from _single_equal_point(n, i0)
        return
    yield from _generic_value_diffs(a, b)
