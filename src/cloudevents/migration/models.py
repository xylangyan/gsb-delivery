#  Copyright 2018-Present The CloudEvents Authors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

#: CloudEvents extension attribute carrying the device's original sequence
#: number (the page number in the business ledger).
SEQUENCE_ATTR: Final[str] = "seq"

#: CloudEvents extension attribute carrying the sender origin. Together with
#: ``SEQUENCE_ATTR`` it identifies one business page.
SOURCE_ATTR: Final[str] = "source"


class Origin(str, Enum):
    """Where a business page entered the ledger from."""

    KAFKA = "kafka"
    HTTP = "http"


class HandoffState(str, Enum):
    """
    State of the old-line to new-line handoff.

    ``PENDING``  - new line has not received its first page yet, issued old
                   tickets may still be withdrawn.
    ``DELIVERED`` - at least one page was delivered via the new line; rollback
                   to the old line is no longer possible.
    ``REOPENED``  - a previously signed handoff failed and was reopened;
                   receipts arriving afterwards are re-verified.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    REOPENED = "reopened"


@dataclass(frozen=True)
class BusinessKey:
    """
    Identity of one business page.

    Two pages are the same page only when both the original sequence number
    and the sender source match; equal payloads never count as duplicates.
    """

    seq: int
    source: str

    def __str__(self) -> str:
        return f"{self.source}#{self.seq}"


@dataclass(frozen=True)
class PhysicalPosition:
    """
    A physical position inside one Kafka partition.

    Offsets follow Kafka convention: the position identifies the *next*
    unread offset, so a message read at offset ``n`` advances the position to
    ``n + 1``.
    """

    partition: str
    offset: int

    def __str__(self) -> str:
        return f"{self.partition}@{self.offset}"


@dataclass(frozen=True)
class GapRange:
    """
    A sealed range of sequence numbers that will never produce a page.

    Gap ranges are scoped to one sender source because sequence numbers are
    assigned per device.
    """

    source: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"gap start {self.start} is after end {self.end}")

    def contains(self, seq: int) -> bool:
        return self.start <= seq <= self.end


@dataclass(frozen=True)
class DeliveryReceipt:
    """
    A warehouse arrival credential.

    One receipt may stamp several physical positions at once, but only when
    they all belong to the same business page (``key``).
    """

    key: BusinessKey
    positions: tuple[PhysicalPosition, ...]


@dataclass(frozen=True)
class PageRecord:
    """Everything the ledger knows about one business page."""

    key: BusinessKey
    origin: Origin
    confirmed: bool
    physical_positions: tuple[PhysicalPosition, ...] = field(default_factory=tuple)
    physical_keys: tuple[str, ...] = field(default_factory=tuple)
    partition_keys: tuple[str, ...] = field(default_factory=tuple)
    blocking: bool = False


@dataclass(frozen=True)
class PartitionReadRange:
    """The actual offset range read from one partition in a batch."""

    partition: str
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        if self.start_offset > self.end_offset:
            raise ValueError(
                f"read range start {self.start_offset} is after end {self.end_offset}"
            )


@dataclass(frozen=True)
class BatchReport:
    """One batch's actual read ranges and the gap ranges sealed in it."""

    line: str
    read_ranges: tuple[PartitionReadRange, ...]
    gap_ranges: tuple[GapRange, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LedgerSnapshot:
    """
    Point-in-time view for the on-call dashboard.

    ``next_contiguous_seqs`` is, per sender source, the next sequence number
    that can still be delivered contiguously. ``partition_positions`` are the
    offsets that can really be confirmed right now. Both stalls can be traced
    back to the underlying tickets via ``blocking_pages`` and
    ``blocking_positions``.
    """

    handoff_state: HandoffState
    next_contiguous_seqs: dict[str, int]
    confirmed_seqs: dict[str, tuple[int, ...]]
    partition_positions: dict[str, int]
    blocking_pages: tuple[PageRecord, ...]
    blocking_positions: tuple[PhysicalPosition, ...]
    sealed_gaps: tuple[GapRange, ...]
    batches: tuple[BatchReport, ...]

    def page_blocker(self, source: str) -> PageRecord | None:
        """The page holding up contiguous delivery for ``source``, if any."""
        return next(
            (
                page
                for page in self.blocking_pages
                if page.blocking and page.key.source == source
            ),
            None,
        )

    def position_blocker(self, partition: str) -> PhysicalPosition | None:
        """The earliest unconfirmable position in ``partition``, if any."""
        return next(
            (
                position
                for position in self.blocking_positions
                if position.partition == partition
            ),
            None,
        )

    def as_dict(self) -> dict[str, Any]:
        """Plain ``dict`` rendering, handy for logging or JSON dumps."""
        return {
            "handoff_state": self.handoff_state.value,
            "next_contiguous_seqs": dict(self.next_contiguous_seqs),
            "confirmed_seqs": {
                source: list(seqs) for source, seqs in self.confirmed_seqs.items()
            },
            "partition_positions": dict(self.partition_positions),
            "blocking_pages": [
                {
                    "key": str(page.key),
                    "origin": page.origin.value,
                    "confirmed": page.confirmed,
                    "physical_positions": [str(p) for p in page.physical_positions],
                }
                for page in self.blocking_pages
            ],
            "blocking_positions": [str(p) for p in self.blocking_positions],
            "sealed_gaps": [
                {"source": gap.source, "start": gap.start, "end": gap.end}
                for gap in self.sealed_gaps
            ],
            "batches": [
                {
                    "line": batch.line,
                    "read_ranges": [
                        {
                            "partition": read_range.partition,
                            "start_offset": read_range.start_offset,
                            "end_offset": read_range.end_offset,
                        }
                        for read_range in batch.read_ranges
                    ],
                    "gap_ranges": [
                        {
                            "source": gap.source,
                            "start": gap.start,
                            "end": gap.end,
                        }
                        for gap in batch.gap_ranges
                    ],
                }
                for batch in self.batches
            ],
        }
