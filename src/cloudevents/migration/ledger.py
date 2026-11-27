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

from cloudevents.core.bindings.kafka import KafkaMessage, from_kafka_event
from cloudevents.migration.exceptions import (
    DuplicateClaimError,
    HandoffStateError,
    MigrationClosedError,
    MigrationLedgerError,
)
from cloudevents.migration.identity import (
    business_key_of,
    header_partition_key_of,
    partition_key_of,
    physical_key_of,
    position_of,
)
from cloudevents.migration.models import (
    BatchReport,
    BusinessKey,
    DeliveryReceipt,
    GapRange,
    HandoffState,
    LedgerSnapshot,
    Origin,
    PageRecord,
    PhysicalPosition,
)


@dataclass
class _PageState:
    """Mutable internal state for one business page."""

    key: BusinessKey
    origin: Origin
    confirmed: bool
    positions: set[PhysicalPosition] = field(default_factory=set)
    physical_keys: set[str] = field(default_factory=set)
    partition_keys: set[str] = field(default_factory=set)

    def to_record(self, blocking: bool = False) -> PageRecord:
        ordered_positions = tuple(
            sorted(self.positions, key=lambda p: (p.partition, p.offset))
        )
        return PageRecord(
            key=self.key,
            origin=self.origin,
            confirmed=self.confirmed,
            physical_positions=ordered_positions,
            physical_keys=tuple(sorted(self.physical_keys)),
            partition_keys=tuple(sorted(self.partition_keys)),
            blocking=blocking,
        )


class MigrationLedger:
    """
    In-memory handoff ledger for moving devices from old Kafka partitions to
    new ones while the devices keep reporting.

    The ledger keeps two independent progressions:

    * the business ledger - pages delivered contiguously by the device's
      original sequence number; HTTP backfills and sealed gap ranges count as
      delivered, unconfirmed pages park the pages behind them;
    * the physical partitions - per-partition consumer positions that may
      only cross entries whose business page is confirmed. An earlier entry
      belonging to another, still unconfirmable device pins the partition.

    Duplicate detection is strictly identity based: a page is the same page
    only when sequence number and sender source both match. Equal payloads
    never count as duplicates.
    """

    def __init__(
        self,
        base_sequences: dict[str, int] | int = 0,
        new_partitions: set[str] | frozenset[str] | tuple[str, ...] = (),
    ) -> None:
        """
        :param base_sequences: sequence number each sender source's business
            account was already signed off at, so contiguous delivery
            restarts after it. A single integer applies that base to every
            source; a mapping sets the base per source.
        :param new_partitions: partitions belonging to the new line. The first
            Kafka event observed on any of them commits the handoff.
        """
        self._default_base = base_sequences if isinstance(base_sequences, int) else 0
        self._base_sequences = (
            dict(base_sequences) if isinstance(base_sequences, dict) else {}
        )
        self._new_partitions = frozenset(new_partitions)
        self._pages: dict[BusinessKey, _PageState] = {}
        self._gaps: list[GapRange] = []
        self._batches: list[BatchReport] = []
        # Inclusive lowest offset and exclusive highest offset ever observed
        # for each physical partition.
        self._partition_lo: dict[str, int] = {}
        self._partition_hi: dict[str, int] = {}
        self._handoff = HandoffState.PENDING

    def _base_seq(self, source: str) -> int:
        return self._base_sequences.get(source, self._default_base)

    @property
    def handoff_state(self) -> HandoffState:
        return self._handoff

    # ------------------------------------------------------------------
    # Intake: original Kafka events
    # ------------------------------------------------------------------
    def record_kafka_event(
        self,
        event_like: object,
        partition: str,
        offset: int,
        message: KafkaMessage | None = None,
        header_partition_key: str | None = None,
    ) -> PageRecord:
        """
        Record an original event read from a physical Kafka partition.

        The event header feeds the business ledger while the physical key and
        the actual partition/offset are tracked separately. A late Kafka
        original for an HTTP-confirmed page is attached as physical evidence
        but never changes the recorded delivery origin or confirmation.

        :param header_partition_key: the partitionkey name as written on the
            wire. Required when a keyed ``message`` would otherwise overwrite
            the event's own partitionkey during parsing; defaults to the event
            attribute when not given.
        """
        key = business_key_of(event_like)
        position = position_of(partition, offset)
        self._observe_partition(partition, offset)

        if partition in self._new_partitions and self._handoff == HandoffState.PENDING:
            self._handoff = HandoffState.DELIVERED

        page = self._pages.get(key)
        if page is None:
            page = _PageState(key=key, origin=Origin.KAFKA, confirmed=False)
            self._pages[key] = page

        page.positions.add(position)
        if header_partition_key is None:
            header_partition_key = partition_key_of(event_like)
        if header_partition_key is not None:
            page.partition_keys.add(header_partition_key)
        if message is not None:
            physical_key = physical_key_of(message)
            if physical_key is not None:
                page.physical_keys.add(physical_key)
        return page.to_record()

    def record_kafka_message(
        self, message: KafkaMessage, partition: str, offset: int
    ) -> PageRecord:
        """
        Parse a Kafka ``KafkaMessage`` (binary or structured CloudEvent) and
        record it. Useful when the physical key deliberately differs from the
        ``partitionkey`` written in the event header: the original header name
        is read straight from the wire before parsing can overwrite it.
        """
        event = from_kafka_event(message)
        return self.record_kafka_event(
            event,
            partition,
            offset,
            message=message,
            header_partition_key=header_partition_key_of(message),
        )

    # ------------------------------------------------------------------
    # Intake: HTTP backfills
    # ------------------------------------------------------------------
    def record_http_backfill(self, event_like: object) -> PageRecord:
        """
        Record an HTTP backfill claiming one missing page.

        Only a backfill whose sequence number and sender source identify an
        already known (or new) business page can fill it. A second backfill
        for an already claimed or already delivered page is rejected rather
        than treated as a duplicate based on payload equality.
        """
        key = business_key_of(event_like)
        page = self._pages.get(key)
        if page is not None and (page.confirmed or page.origin == Origin.HTTP):
            raise DuplicateClaimError(
                f"page {key} is already claimed or delivered; "
                "equal payloads do not make a second HTTP backfill a duplicate"
            )

        if page is None:
            page = _PageState(key=key, origin=Origin.HTTP, confirmed=True)
            self._pages[key] = page
        else:
            page.origin = Origin.HTTP
            page.confirmed = True

        header_partition_key = partition_key_of(event_like)
        if header_partition_key is not None:
            page.partition_keys.add(header_partition_key)
        return page.to_record()

    # ------------------------------------------------------------------
    # Intake: sealed gap ranges and batch read ranges
    # ------------------------------------------------------------------
    def seal_gap(self, source: str, start: int, end: int) -> None:
        """
        Seal a sequence range that will never produce a page for ``source``.
        The ledger does not create a confirmation record per skipped number.
        """
        gap = GapRange(source=source, start=start, end=end)
        for page in self._pages.values():
            if (
                page.confirmed
                and page.key.source == source
                and gap.contains(page.key.seq)
            ):
                raise MigrationLedgerError(
                    f"cannot seal gap {start}-{end} for {source} "
                    f"over confirmed page {page.key}"
                )
        self._merge_gap(gap)

    def record_batch(self, report: BatchReport) -> None:
        """
        Record one batch: the offset ranges actually read on the old and new
        partitions and the gap ranges sealed by the batch.
        """
        for read_range in report.read_ranges:
            self._observe_range(
                read_range.partition, read_range.start_offset, read_range.end_offset
            )
        for gap in report.gap_ranges:
            self.seal_gap(gap.source, gap.start, gap.end)
        self._batches.append(report)

    # ------------------------------------------------------------------
    # Intake: warehouse arrival receipts
    # ------------------------------------------------------------------
    def record_receipt(self, receipt: DeliveryReceipt) -> PageRecord:
        """
        Apply a warehouse arrival receipt.

        One receipt may stamp several physical positions at once, but every
        stamped position must belong to the same business page and must have
        actually been observed. After a reopened handoff this doubles as the
        re-verification of the original sequence against actual delivery. A
        redelivered receipt for an already delivered page is accepted
        idempotently and never changes the recorded delivery origin.
        """
        page = self._pages.get(receipt.key)
        if page is None:
            raise MigrationLedgerError(
                f"receipt for unknown business page {receipt.key}; "
                "record the original event or backfill first"
            )
        if self._handoff == HandoffState.REOPENED and not receipt.positions:
            raise MigrationLedgerError(
                f"receipt after reopen must verify actual delivery positions "
                f"for page {receipt.key}"
            )
        for position in receipt.positions:
            if position not in page.positions:
                raise MigrationLedgerError(
                    f"receipt position {position} was never observed for page "
                    f"{receipt.key}"
                )
        page.confirmed = True
        return page.to_record()

    # ------------------------------------------------------------------
    # Handoff lifecycle: withdrawal, commit, reopen
    # ------------------------------------------------------------------
    def withdraw(
        self,
        key: BusinessKey,
        positions: tuple[PhysicalPosition, ...] | None = None,
    ) -> None:
        """
        Withdraw old-line tickets issued before the new line went live.

        Only allowed while the first new-line delivery has not happened. Once
        the first page has been successfully delivered through the new line
        the handoff cannot be rolled back to the old version.
        """
        if self._handoff != HandoffState.PENDING:
            raise MigrationClosedError(
                "first new-line delivery already succeeded; "
                "old tickets can no longer be withdrawn"
            )
        page = self._pages.get(key)
        if page is None:
            raise MigrationLedgerError(f"no ticket known for page {key}")

        targets = set(positions) if positions is not None else set(page.positions)
        unknown = targets - page.positions
        if unknown:
            raise MigrationLedgerError(
                f"positions {sorted(unknown)} are not held by page {key}"
            )
        page.positions.difference_update(targets)
        if not page.positions and not page.confirmed:
            del self._pages[key]

    def commit_handoff(self) -> None:
        """
        Mark the handoff as having a successful new-line delivery.

        Normally the first Kafka event on a configured new partition triggers
        this automatically; this method covers receipt-only commit paths.
        """
        if self._handoff == HandoffState.PENDING:
            self._handoff = HandoffState.DELIVERED
        elif self._handoff == HandoffState.REOPENED:
            raise HandoffStateError("handoff was reopened after a failure")

    def reopen_handoff(self) -> None:
        """
        Reopen a failed handoff for re-verification. Already delivered pages
        keep their recorded origin; later receipts are checked against actual
        delivery positions.
        """
        if self._handoff == HandoffState.PENDING:
            raise HandoffStateError(
                "cannot reopen a handoff that never delivered a new-line page"
            )
        self._handoff = HandoffState.REOPENED

    # ------------------------------------------------------------------
    # Read side: the continuously updated on-call dashboard
    # ------------------------------------------------------------------
    def next_contiguous_seqs(self) -> dict[str, int]:
        """
        The next sequence number deliverable contiguously, per sender source.
        Sources are discovered from intake and from sealed gap ranges.
        """
        sources = {page.key.source for page in self._pages.values()}
        sources.update(gap.source for gap in self._gaps)
        sources.update(self._base_sequences.keys())
        return {source: self._next_contiguous_seq(source) for source in sorted(sources)}

    def partition_positions(self) -> dict[str, int]:
        """The consumer position really confirmable on each partition now."""
        positions, _ = self._partition_progress()
        return positions

    def snapshot(self) -> LedgerSnapshot:
        """Build a point-in-time dashboard snapshot from all intake so far."""
        head_seqs = self.next_contiguous_seqs()
        records = []
        for page in self._pages.values():
            head_seq = head_seqs.get(page.key.source)
            blocking = (not page.confirmed) and head_seq == page.key.seq
            records.append(page.to_record(blocking=blocking))
        records.sort(key=lambda record: (record.key.source, record.key.seq))

        positions, blockers = self._partition_progress()
        confirmed_seqs: dict[str, list[int]] = {}
        for page in self._pages.values():
            if page.confirmed:
                confirmed_seqs.setdefault(page.key.source, []).append(page.key.seq)
        confirmed = {
            source: tuple(sorted(seqs)) for source, seqs in confirmed_seqs.items()
        }
        return LedgerSnapshot(
            handoff_state=self._handoff,
            next_contiguous_seqs=head_seqs,
            confirmed_seqs=confirmed,
            partition_positions=positions,
            blocking_pages=tuple(records),
            blocking_positions=tuple(blockers),
            sealed_gaps=tuple(self._gaps),
            batches=tuple(self._batches),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _next_contiguous_seq(self, source: str) -> int:
        # The base is the last already-signed sequence number, so the first
        # page still to be delivered is the one right after it.
        seq = self._base_seq(source) + 1
        while self._is_seq_done(source, seq):
            seq += 1
        return seq

    def _is_seq_done(self, source: str, seq: int) -> bool:
        if any(gap.source == source and gap.contains(seq) for gap in self._gaps):
            return True
        page = self._pages.get(BusinessKey(seq=seq, source=source))
        return page is not None and page.confirmed

    def _merge_gap(self, gap: GapRange) -> None:
        same_source = [
            candidate for candidate in self._gaps if candidate.source == gap.source
        ]
        others = [
            candidate for candidate in self._gaps if candidate.source != gap.source
        ]
        same_source.append(gap)
        same_source.sort(key=lambda candidate: candidate.start)
        merged: list[GapRange] = []
        for candidate in same_source:
            if merged and candidate.start <= merged[-1].end + 1:
                previous = merged[-1]
                merged[-1] = GapRange(
                    source=gap.source,
                    start=previous.start,
                    end=max(previous.end, candidate.end),
                )
            else:
                merged.append(candidate)
        self._gaps = others + merged

    def _observe_partition(self, partition: str, offset: int) -> None:
        self._observe_range(partition, offset, offset + 1)

    def _observe_range(
        self, partition: str, start_offset: int, end_offset: int
    ) -> None:
        if start_offset < 0 or end_offset < start_offset:
            raise ValueError(
                f"invalid read range {start_offset}-{end_offset} on {partition}"
            )
        current_lo = self._partition_lo.get(partition)
        self._partition_lo[partition] = (
            start_offset if current_lo is None else min(current_lo, start_offset)
        )
        self._partition_hi[partition] = max(
            self._partition_hi.get(partition, end_offset), end_offset
        )

    def _partition_progress(
        self,
    ) -> tuple[dict[str, int], list[PhysicalPosition]]:
        confirmed_offsets: dict[str, set[int]] = {}
        blocked_offsets: dict[str, set[int]] = {}
        for page in self._pages.values():
            for position in page.positions:
                target = confirmed_offsets if page.confirmed else blocked_offsets
                target.setdefault(position.partition, set()).add(position.offset)

        positions: dict[str, int] = {}
        blockers: list[PhysicalPosition] = []
        for partition, hi in self._partition_hi.items():
            lo = self._partition_lo[partition]
            blocked = blocked_offsets.get(partition, set())
            candidate = lo
            for offset in range(lo, hi):
                if offset in blocked:
                    blockers.append(
                        PhysicalPosition(partition=partition, offset=offset)
                    )
                    break
                candidate = offset + 1
            positions[partition] = candidate
        blockers.sort(key=lambda position: (position.partition, position.offset))
        return positions, blockers
