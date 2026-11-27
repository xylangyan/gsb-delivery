"""Exact integer feasible-set arithmetic.

A write delta for a segment is always a union of finite arithmetic
progressions (APs):

* same physical disk, raw counters retained: ``base + k * modulus``
  for ``k = 0..K`` where ``K`` comes from the device capability bound
  (an unmeasured window may legally pass several counter periods, so
  ``k`` is never assumed to be a single turnover);
* disk swap: an integer interval (the new disk restarts its counter
  at zero, the old disk may have written anything up to its own
  capability bound);
* unmeasured window with no raw reading: an interval ``[0, cap]``.

Sets are kept symbolic (start / step / count); we never enumerate one
archive per counter period or per swap combination.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd

#: Safety bound for exact translate enumeration in :func:`_ap_sum`.
_MAX_TRANSLATES = 4096


@dataclass(frozen=True)
class AP:
    """Finite arithmetic progression: ``start + i*step`` for ``0 <= i < count``."""

    start: int
    step: int
    count: int

    def __post_init__(self):
        if self.step < 1:
            raise ValueError("step must be >= 1")
        if self.count < 1:
            raise ValueError("count must be >= 1")

    @property
    def last(self) -> int:
        return self.start + (self.count - 1) * self.step

    def contains(self, value: int) -> bool:
        return (
            self.start <= value <= self.last
            and (value - self.start) % self.step == 0
        )

    def translate(self, delta: int) -> "AP":
        return AP(self.start + delta, self.step, self.count)

    def intersect(self, other: "AP") -> "AP | None":
        """Exact intersection via the Chinese remainder theorem."""
        step_g = gcd(self.step, other.step)
        if (other.start - self.start) % step_g != 0:
            return None
        lcm = self.step // step_g * other.step
        # Solve self.start + self.step * t == other.start (mod other.step).
        coeff = self.step // step_g
        rhs = (other.start - self.start) // step_g
        mod = other.step // step_g
        t0 = (rhs * pow(coeff, -1, mod)) % mod
        x0 = self.start + self.step * t0
        lo = max(self.start, other.start)
        hi = min(self.last, other.last)
        if lo > hi:
            return None
        first = lo + (x0 - lo) % lcm
        if first > hi:
            return None
        return AP(first, lcm, (hi - first) // lcm + 1)


def _normalize(pieces):
    """Sort pieces and merge same-residue APs that overlap or touch."""
    groups = {}
    for piece in pieces:
        groups.setdefault(piece.step, []).append(piece)
    out = []
    for step, group in groups.items():
        group.sort(key=lambda p: (p.start % step, p.start))
        cur = group[0]
        for nxt in group[1:]:
            same_residue = (nxt.start - cur.start) % step == 0
            if same_residue and nxt.start <= cur.last + step:
                last = max(cur.last, nxt.last)
                cur = AP(cur.start, step, (last - cur.start) // step + 1)
            else:
                out.append(cur)
                cur = nxt
        out.append(cur)
    out.sort(key=lambda p: (p.start, p.step))
    return tuple(out)


def _ap_sum(a: AP, b: AP) -> list[AP]:
    """Exact Minkowski sum of two APs."""
    if a.step == b.step:
        return [AP(a.start + b.start, a.step, a.count + b.count - 1)]
    if a.step == 1 and a.count - 1 >= b.step - 1:
        # Dense enough to swallow the gaps: contiguous interval.
        return [AP(a.start + b.start, 1, a.last + b.last - a.start - b.start + 1)]
    if b.step == 1 and b.count - 1 >= a.step - 1:
        return [AP(a.start + b.start, 1, a.last + b.last - a.start - b.start + 1)]
    big, small = (a, b) if a.count >= b.count else (b, a)
    if small.count > _MAX_TRANSLATES:
        raise ValueError(
            "exact AP sum would need too many translates; "
            "use equal moduli or interval pieces"
        )
    return [big.translate(small.start + i * small.step) for i in range(small.count)]


class FeasibleSet:
    """A normalized union of finite arithmetic progressions."""

    __slots__ = ("pieces",)

    def __init__(self, pieces=()):
        self.pieces = _normalize(tuple(pieces))

    def __repr__(self):
        return f"FeasibleSet({self.pieces!r})"

    def __eq__(self, other):
        return isinstance(other, FeasibleSet) and self.pieces == other.pieces

    # -- constructors ---------------------------------------------------

    @classmethod
    def empty(cls) -> "FeasibleSet":
        return cls(())

    @classmethod
    def interval(cls, lo: int, hi: int) -> "FeasibleSet":
        """Every integer in ``[lo, hi]`` (empty when ``hi < lo``)."""
        if hi < lo:
            return cls.empty()
        return cls((AP(lo, 1, hi - lo + 1),))

    @classmethod
    def progression(cls, start: int, step: int, count: int) -> "FeasibleSet":
        if count < 1:
            return cls.empty()
        return cls((AP(start, step, count),))

    @classmethod
    def wrapped_deltas(cls, base: int, modulus: int, cap: int) -> "FeasibleSet":
        """``{base + k*modulus : k >= 0, base + k*modulus <= cap}``.

        ``k`` counts legal counter turnovers in the window; it is bounded
        only by the device capability record, never assumed to be one.
        """
        if base < 0 or modulus < 1:
            raise ValueError("base must be >= 0 and modulus >= 1")
        if base > cap:
            return cls.empty()
        return cls.progression(base, modulus, (cap - base) // modulus + 1)

    # -- queries --------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return not self.pieces

    @property
    def min(self) -> int:
        return min(p.start for p in self.pieces)

    @property
    def max(self) -> int:
        return max(p.last for p in self.pieces)

    @property
    def singleton(self) -> "int | None":
        """The only feasible value, or None when several are feasible."""
        if len(self.pieces) == 1 and self.pieces[0].count == 1:
            return self.pieces[0].start
        return None

    def contains(self, value: int) -> bool:
        return any(p.contains(value) for p in self.pieces)

    # -- algebra --------------------------------------------------------

    def union(self, other: "FeasibleSet") -> "FeasibleSet":
        return FeasibleSet(self.pieces + other.pieces)

    def intersect(self, other: "FeasibleSet") -> "FeasibleSet":
        out = []
        for a in self.pieces:
            for b in other.pieces:
                hit = a.intersect(b)
                if hit is not None:
                    out.append(hit)
        return FeasibleSet(out)

    def minkowski(self, other: "FeasibleSet") -> "FeasibleSet":
        """All sums ``x + y`` with ``x`` in self and ``y`` in other."""
        if self.is_empty or other.is_empty:
            return FeasibleSet.empty()
        out = []
        for a in self.pieces:
            for b in other.pieces:
                out.extend(_ap_sum(a, b))
        return FeasibleSet(out)

    @classmethod
    def sum_all(cls, sets) -> "FeasibleSet":
        total = cls.interval(0, 0)
        for item in sets:
            total = total.minkowski(item)
        return total


def pick_sum(sets, target: int) -> "list[int] | None":
    """Pick one value per set summing exactly to ``target``.

    Returns None when no such combination exists.  Used to produce
    witnesses for signed totals without enumerating combinations.
    """
    sets = list(sets)
    suffix = [FeasibleSet.interval(0, 0)]
    for item in reversed(sets):
        suffix.append(item.minkowski(suffix[-1]))
    suffix.reverse()
    # suffix[i] == sum of sets[i:]
    if not suffix[0].contains(target):
        return None
    out = []
    remaining = target
    for i, item in enumerate(sets):
        rest = suffix[i + 1]
        value = _find_member(item, rest, remaining)
        if value is None:  # pragma: no cover - contains() guaranteed it
            return None
        out.append(value)
        remaining -= value
    return out


def _find_member(item: FeasibleSet, rest: FeasibleSet, total: int) -> "int | None":
    """Find ``v`` in ``item`` such that ``total - v`` is in ``rest``."""
    for piece in item.pieces:
        for rp in rest.pieces:
            # total - v in rp  <=>  v in reflection of rp around total.
            reflected = AP(total - rp.last, rp.step, rp.count)
            hit = piece.intersect(reflected)
            if hit is not None:
                return hit.start
    return None
