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

import pytest

from cloudevents.core.bindings.kafka import KafkaMessage, to_binary_event
from cloudevents.migration import (
    BatchReport,
    BusinessKey,
    DeliveryReceipt,
    DuplicateClaimError,
    HandoffState,
    MigrationClosedError,
    MigrationLedger,
    MigrationLedgerError,
    Origin,
    PartitionReadRange,
    PhysicalPosition,
)
from tests.test_migration.test_identity_models import make_event

OLD = "old-0"
NEW = "new-1"
DEVICE_A = "/device-a"
DEVICE_B = "/device-b"


def test_physical_order_reversed_from_business_order_with_parking() -> None:
    # Device A seq 1 and device B seq 1 live on the same physical partition.
    # Physical arrival order is reversed relative to the business sequence
    # order: A#1 sits behind B#1 on the wire, while B's page cannot confirm
    # until its HTTP backfill arrives.
    ledger = MigrationLedger(new_partitions={NEW})

    # Physical offset 0: device B seq 1 arrives first on the wire.
    ledger.record_kafka_event(make_event(DEVICE_B, 1), partition=OLD, offset=0)
    # Physical offset 1: device A seq 1 arrives later on the wire.
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=1)

    snapshot = ledger.snapshot()
    assert snapshot.partition_positions == {OLD: 0}
    blocker = snapshot.position_blocker(OLD)
    assert blocker == PhysicalPosition(partition=OLD, offset=0)
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 1, DEVICE_B: 1}

    # Confirming A#1 must not move the partition past the still-pending B#1.
    ledger.record_receipt(
        DeliveryReceipt(
            key=BusinessKey(seq=1, source=DEVICE_A),
            positions=(PhysicalPosition(OLD, 1),),
        )
    )
    snapshot = ledger.snapshot()
    assert snapshot.partition_positions == {OLD: 0}
    assert snapshot.position_blocker(OLD) == PhysicalPosition(OLD, 0)
    assert snapshot.next_contiguous_seqs[DEVICE_A] == 2
    assert snapshot.next_contiguous_seqs[DEVICE_B] == 1

    # The parked B#1 page is still the business blocker for device B.
    page_blocker = snapshot.page_blocker(DEVICE_B)
    assert page_blocker is not None
    assert page_blocker.key == BusinessKey(1, DEVICE_B)
    assert page_blocker.blocking is True

    # HTTP backfill confirms B#1; now both pages can confirm and the whole
    # physical range becomes consumable.
    ledger.record_http_backfill(make_event(DEVICE_B, 1))
    snapshot = ledger.snapshot()
    assert snapshot.partition_positions == {OLD: 2}
    assert snapshot.blocking_positions == ()
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 2, DEVICE_B: 2}


def test_late_kafka_original_after_http_backfill_keeps_http_origin() -> None:
    ledger = MigrationLedger()

    # HTTP backfill confirms the page before the Kafka original shows up.
    record = ledger.record_http_backfill(make_event(DEVICE_A, 1))
    assert record.origin == Origin.HTTP
    assert record.confirmed is True

    # The late Kafka original attaches physical evidence but cannot rewrite
    # the recorded delivery origin or confirmation.
    record = ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=4)
    assert record.origin == Origin.HTTP
    assert record.confirmed is True
    assert PhysicalPosition(OLD, 4) in record.physical_positions
    assert ledger.snapshot().next_contiguous_seqs == {DEVICE_A: 2}


def test_http_backfill_identity_mismatch_does_not_fill_page() -> None:
    ledger = MigrationLedger()
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)

    # Same sequence number, different sender: a different page, not a fill.
    ledger.record_http_backfill(make_event(DEVICE_B, 1))
    snapshot = ledger.snapshot()
    assert snapshot.next_contiguous_seqs[DEVICE_A] == 1
    assert snapshot.next_contiguous_seqs[DEVICE_B] == 2


def test_equal_payloads_never_count_as_duplicate_claim() -> None:
    ledger = MigrationLedger()
    ledger.record_http_backfill(make_event(DEVICE_A, 1))

    # A second HTTP backfill for the very same identity is rejected.
    with pytest.raises(DuplicateClaimError):
        ledger.record_http_backfill(make_event(DEVICE_A, 1))

    # A page with identical data but another identity is accepted.
    other = ledger.record_http_backfill(make_event(DEVICE_B, 1))
    assert other.key == BusinessKey(1, DEVICE_B)


def test_receipt_stamps_several_positions_of_one_page() -> None:
    ledger = MigrationLedger()
    # One business page physically delivered on both the old and new line.
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=2)
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=NEW, offset=0)

    ledger.record_receipt(
        DeliveryReceipt(
            key=BusinessKey(1, DEVICE_A),
            positions=(PhysicalPosition(OLD, 2), PhysicalPosition(NEW, 0)),
        )
    )
    snapshot = ledger.snapshot()
    assert snapshot.partition_positions == {OLD: 3, NEW: 1}
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 2}


def test_receipt_rejects_foreign_and_unobserved_positions() -> None:
    ledger = MigrationLedger()
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)
    ledger.record_kafka_event(make_event(DEVICE_B, 1), partition=OLD, offset=1)

    with pytest.raises(MigrationLedgerError):
        ledger.record_receipt(
            DeliveryReceipt(
                key=BusinessKey(1, DEVICE_A),
                positions=(PhysicalPosition(OLD, 1),),
            )
        )
    with pytest.raises(MigrationLedgerError):
        ledger.record_receipt(
            DeliveryReceipt(
                key=BusinessKey(9, DEVICE_A),
                positions=(),
            )
        )


def test_redelivered_receipt_is_idempotent_and_keeps_origin() -> None:
    ledger = MigrationLedger()
    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=7)

    # Old receipt arriving late, after the page was already delivered.
    record = ledger.record_receipt(
        DeliveryReceipt(key=BusinessKey(1, DEVICE_A), positions=())
    )
    assert record.origin == Origin.HTTP
    assert record.confirmed is True


def test_withdraw_allowed_before_first_new_delivery_then_blocked() -> None:
    ledger = MigrationLedger(new_partitions={NEW})
    key = BusinessKey(1, DEVICE_A)
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)
    ledger.record_kafka_event(make_event(DEVICE_A, 2), partition=OLD, offset=1)

    # Old tickets issued before cutover are still retractable. Withdrawing
    # the only position of an unconfirmed page removes it from the ledger.
    ledger.withdraw(key, positions=(PhysicalPosition(OLD, 0),))
    snapshot = ledger.snapshot()
    assert all(page.key != key for page in snapshot.blocking_pages)
    assert any(page.key == BusinessKey(2, DEVICE_A) for page in snapshot.blocking_pages)

    # First genuinely successful new-line delivery commits the handoff.
    ledger.record_kafka_event(make_event(DEVICE_A, 3), partition=NEW, offset=0)
    assert ledger.handoff_state == HandoffState.DELIVERED

    with pytest.raises(MigrationClosedError):
        ledger.withdraw(BusinessKey(2, DEVICE_A))


def test_binary_header_partitionkey_differs_from_physical_key() -> None:
    ledger = MigrationLedger()
    event = make_event(DEVICE_A, 1, partitionkey="selected-material-name")
    wire = to_binary_event(event)
    # The physical Kafka key is something else than the header name.
    message = KafkaMessage(
        headers=wire.headers, key=b"routing-hash-9", value=wire.value
    )

    record = ledger.record_kafka_message(message, partition=OLD, offset=5)
    assert record.partition_keys == ("selected-material-name",)
    assert record.physical_keys == ("routing-hash-9",)
    assert record.physical_positions == (PhysicalPosition(OLD, 5),)


def test_batch_report_tracks_read_ranges_and_advances_positions() -> None:
    ledger = MigrationLedger()
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)
    ledger.record_receipt(
        DeliveryReceipt(
            key=BusinessKey(1, DEVICE_A),
            positions=(PhysicalPosition(OLD, 0),),
        )
    )

    report = BatchReport(
        line="old",
        read_ranges=(
            PartitionReadRange(partition=OLD, start_offset=0, end_offset=10),
            PartitionReadRange(partition=NEW, start_offset=0, end_offset=4),
        ),
    )
    ledger.record_batch(report)
    snapshot = ledger.snapshot()

    assert snapshot.batches == (report,)
    # All observed entries are confirmed, so positions reach the read hi-water.
    assert snapshot.partition_positions == {OLD: 10, NEW: 4}


def test_parked_pages_span_partitions_without_breaking_contiguity() -> None:
    ledger = MigrationLedger()
    # Device A#1 unconfirmed on the old partition; A#2 already on the new one.
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)
    ledger.record_kafka_event(make_event(DEVICE_A, 2), partition=NEW, offset=0)

    snapshot = ledger.snapshot()
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 1}
    # A#2 is parked behind A#1 even though it sits on another partition.
    parked = next(
        page for page in snapshot.blocking_pages if page.key == BusinessKey(2, DEVICE_A)
    )
    assert parked.confirmed is False
    assert parked.blocking is False

    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    snapshot = ledger.snapshot()
    # A#1 now delivered; A#2 is still unconfirmed but becomes the new head.
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 2}
    assert snapshot.page_blocker(DEVICE_A).key == BusinessKey(2, DEVICE_A)


def test_end_to_end_short_account_handoff_narrative() -> None:
    ledger = MigrationLedger(
        base_sequences={DEVICE_A: 0, DEVICE_B: 0}, new_partitions={NEW}
    )

    # Physical order on the old partition is reversed relative to the business
    # sequence order: device B's page sits at offset 0, ahead of device A's.
    ledger.record_kafka_event(make_event(DEVICE_B, 1), partition=OLD, offset=0)

    # Device A#1 travels in binary mode with a header partitionkey that is not
    # the physical Kafka key, and was issued twice as old-line tickets.
    event_a1 = make_event(DEVICE_A, 1, partitionkey="mat-a1")
    wire_a1 = to_binary_event(event_a1)
    ticket_a1 = KafkaMessage(
        headers=wire_a1.headers, key=b"phys-001", value=wire_a1.value
    )
    ledger.record_kafka_message(ticket_a1, partition=OLD, offset=1)
    ledger.record_kafka_message(ticket_a1, partition=OLD, offset=2)

    snapshot = ledger.snapshot()
    assert snapshot.partition_positions == {OLD: 0}
    assert snapshot.position_blocker(OLD) == PhysicalPosition(OLD, 0)

    # One retraction before the new line goes live: pull the duplicate ticket.
    ledger.withdraw(BusinessKey(1, DEVICE_A), positions=(PhysicalPosition(OLD, 2),))

    # The HTTP backfill fills A#1; business delivery of A can continue even
    # though B#1 still physically pins the old partition.
    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    snapshot = ledger.snapshot()
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 2, DEVICE_B: 1}
    assert snapshot.partition_positions == {OLD: 0}

    # First genuinely successful new-line delivery commits the handoff.
    ledger.record_kafka_event(make_event(DEVICE_A, 2), partition=NEW, offset=0)
    assert ledger.handoff_state == HandoffState.DELIVERED
    with pytest.raises(MigrationClosedError):
        ledger.withdraw(BusinessKey(1, DEVICE_B))

    # Late Kafka original of A#1 arrives on the new line: evidence only, the
    # recorded HTTP origin and confirmation must not change.
    record_a1 = ledger.record_kafka_event(
        make_event(DEVICE_A, 1), partition=NEW, offset=1
    )
    assert record_a1.origin == Origin.HTTP
    assert record_a1.confirmed is True

    # One warehouse receipt stamps two physical positions of the same page.
    ledger.record_receipt(
        DeliveryReceipt(
            key=BusinessKey(1, DEVICE_A),
            positions=(PhysicalPosition(OLD, 1), PhysicalPosition(NEW, 1)),
        )
    )
    # A redelivered empty receipt is idempotent and keeps the HTTP origin.
    assert (
        ledger.record_receipt(
            DeliveryReceipt(key=BusinessKey(1, DEVICE_A), positions=())
        ).origin
        == Origin.HTTP
    )
    with pytest.raises(DuplicateClaimError):
        ledger.record_http_backfill(make_event(DEVICE_A, 1))

    # B#1 is still the earliest unconfirmable entry and keeps pinning OLD.
    snapshot = ledger.snapshot()
    assert snapshot.position_blocker(OLD) == PhysicalPosition(OLD, 0)
    assert snapshot.page_blocker(DEVICE_B).key == BusinessKey(1, DEVICE_B)

    # Filling B#1 releases the old partition up to its read hi-water mark.
    ledger.record_http_backfill(make_event(DEVICE_B, 1))
    ledger.record_receipt(
        DeliveryReceipt(
            key=BusinessKey(2, DEVICE_A),
            positions=(PhysicalPosition(NEW, 0),),
        )
    )

    ledger.record_batch(
        BatchReport(
            line="cutover",
            read_ranges=(
                PartitionReadRange(OLD, 0, 3),
                PartitionReadRange(NEW, 0, 2),
            ),
        )
    )
    snapshot = ledger.snapshot()
    assert snapshot.blocking_positions == ()
    assert snapshot.partition_positions == {OLD: 3, NEW: 2}
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 3, DEVICE_B: 2}

    # Header name, physical key and actual positions are kept apart, and the
    # withdrawn duplicate ticket left no position behind.
    final_a1 = next(
        page for page in snapshot.blocking_pages if page.key == BusinessKey(1, DEVICE_A)
    )
    assert final_a1.partition_keys == ("mat-a1",)
    assert final_a1.physical_keys == ("phys-001",)
    assert final_a1.physical_positions == (
        PhysicalPosition(NEW, 1),
        PhysicalPosition(OLD, 1),
    )
