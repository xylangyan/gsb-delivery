"""Historical archive handover (历史档).

Archives are handed over by *omitted sample-number spans and stride*,
not by enumerating every counter period or every swap transcript.
Reconciliation therefore works directly on the retained sample
numbers: a gap between two retained samples is simply a longer
segment whose feasible set already carries every legal number of
counter turnovers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OmittedSpan:
    """Samples ``first..last`` were omitted; with ``stride > 1`` every
    stride-th sample (counting from ``first``) was kept."""

    first: int
    last: int
    stride: int = 1

    def __post_init__(self):
        if self.last < self.first:
            raise ValueError("empty span")
        if self.stride < 1:
            raise ValueError("stride must be >= 1")

    def is_omitted(self, sample_no: int) -> bool:
        if not self.first <= sample_no <= self.last:
            return False
        if self.stride == 1:
            return True
        return (sample_no - self.first) % self.stride != 0


class ArchiveIndex:
    """Which sample numbers survive in a historical archive."""

    def __init__(self, omitted_spans=()):
        self.omitted_spans = tuple(omitted_spans)

    def is_retained(self, sample_no: int) -> bool:
        return not any(
            span.is_omitted(sample_no) for span in self.omitted_spans
        )

    def retained(self, sample_nos):
        """Lazily yield the retained numbers, in input order."""
        return (no for no in sample_nos if self.is_retained(no))

    def filter_samples(self, samples):
        """Keep only samples whose read-action number was retained."""
        return [s for s in samples if self.is_retained(s.sample_no)]
