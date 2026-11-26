"""
Page snapshots with provenance, kept compressed.

A Page is the sequence of item-numbers on one transcript's page at the end
of a round. It is stored as a list of PageParts, each pointing back into
the segment that delivered those items, so a page built from huge segments
stays small and individual records are only read out on demand.

Origin(op, pos) means: this item was delivered by the op with index `op`
of the round's program, at position `pos` within that delivery. The initial
page content uses op = INITIAL_OP (-1). Two positions on two transcripts
have the same provenance iff their origins are equal.
"""
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterator, List, NamedTuple, Tuple

from .segments import Segment

INITIAL_OP = -1


class Origin(NamedTuple):
    op: int
    pos: int


@dataclass(frozen=True)
class PagePart:
    """A run of page positions whose values come from `segment` and whose
    origins are Origin(op, base + k) for k in range(len(segment))."""

    segment: Segment
    op: int
    base: int

    def __len__(self) -> int:
        return len(self.segment)


class Page:
    def __init__(self, parts=()):
        self.parts: Tuple[PagePart, ...] = tuple(p for p in parts if len(p) > 0)
        cum = [0]
        for part in self.parts:
            cum.append(cum[-1] + len(part))
        self._cum = cum

    def __len__(self) -> int:
        return self._cum[-1]

    def _locate(self, index: int) -> Tuple[PagePart, int]:
        if not 0 <= index < len(self):
            raise IndexError(f"page position {index} out of range (page length {len(self)})")
        slot = bisect_right(self._cum, index) - 1
        return self.parts[slot], index - self._cum[slot]

    def value_at(self, index: int) -> Any:
        part, offset = self._locate(index)
        return part.segment.value_at(offset)

    def origin_at(self, index: int) -> Origin:
        part, offset = self._locate(index)
        return Origin(part.op, part.base + offset)

    def iter_records(self, start: int = 0, end: int = None) -> Iterator[Tuple[int, Any, Origin]]:
        """Stream (position, value, origin) records over [start, end)
        without materializing one record per element up front."""
        if end is None:
            end = len(self)
        end = min(end, len(self))
        pos = max(start, 0)
        while pos < end:
            part, offset = self._locate(pos)
            take = min(len(part) - offset, end - pos)
            for k in range(take):
                yield (
                    pos + k,
                    part.segment.value_at(offset + k),
                    Origin(part.op, part.base + offset + k),
                )
            pos += take

    def read(self, start: int, end: int) -> List[Tuple[int, Any, Origin]]:
        """Read out the records of one contiguous interval, on demand."""
        return list(self.iter_records(start, end))

    def __repr__(self) -> str:
        return f"Page(parts={self.parts!r})"
