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

from cloudevents.core.bindings.kafka import to_structured_event
from cloudevents.migration import (
    BatchReport,
    BusinessKey,
    DeliveryReceipt,
    GapRange,
    HandoffState,
    HandoffStateError,
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


def test_long_gap_skips_many_sequences_without_per_seq_records() -> None:
    ledger = MigrationLedger(base_sequences={DEVICE_A: 0})
    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    # Nothing happens for sequences 2..999; the batch seals them as one range.
    ledger.seal_gap(DEVICE_A, 2, 999)
    ledger.record_kafka_event(make_event(DEVICE_A, 1000), partition=NEW, offset=0)

    snapshot = ledger.snapshot()
    assert snapshot.sealed_gaps == (GapRange(DEVICE_A, 2, 999),)
    # Only the two real pages exist; no confirmation record per skipped seq.
    assert len(snapshot.blocking_pages) == 2
    assert snapshot.next_contiguous_seqs == {DEVICE_A: 1000}


def test_adjacent_gap_ranges_merge_and_stay_scoped_per_source() -> None:
    ledger = MigrationLedger()
    ledger.seal_gap(DEVICE_A, 2, 4)
    ledger.seal_gap(DEVICE_A, 5, 8)
    ledger.seal_gap(DEVICE_B, 2, 4)

    snapshot = ledger.snapshot()
    assert GapRange(DEVICE_A, 2, 8) in snapshot.sealed_gaps
    assert GapRange(DEVICE_B, 2, 4) in snapshot.sealed_gaps
    # Device B's gap must not satisfy device A's sequence numbers and vice
    # versa beyond their own scope.
    assert ledger.next_contiguous_seqs() == {DEVICE_A: 1, DEVICE_B: 1}
    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    assert ledger.next_contiguous_seqs() == {DEVICE_A: 9, DEVICE_B: 1}


def test_sealing_gap_over_confirmed_page_is_rejected() -> None:
    ledger = MigrationLedger()
    ledger.record_http_backfill(make_event(DEVICE_A, 5))
    with pytest.raises(MigrationLedgerError):
        ledger.seal_gap(DEVICE_A, 4, 6)
    # The same range on another source is fine.
    ledger.seal_gap(DEVICE_B, 4, 6)


def test_reopen_requires_receipt_with_actual_positions() -> None:
    ledger = MigrationLedger(new_partitions={NEW})
    key = BusinessKey(1, DEVICE_A)
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=NEW, offset=0)
    ledger.record_receipt(
        DeliveryReceipt(key=key, positions=(PhysicalPosition(NEW, 0),))
    )
    assert ledger.handoff_state == HandoffState.DELIVERED

    ledger.reopen_handoff()
    assert ledger.handoff_state == HandoffState.REOPENED

    # A redelivered receipt after reopen must re-verify actual delivery.
    with pytest.raises(MigrationLedgerError):
        ledger.record_receipt(DeliveryReceipt(key=key, positions=()))
    record = ledger.record_receipt(
        DeliveryReceipt(key=key, positions=(PhysicalPosition(NEW, 0),))
    )
    assert record.confirmed is True
    assert record.origin == Origin.KAFKA


def test_delivered_origin_is_unchanged_by_late_receipt_redelivery() -> None:
    ledger = MigrationLedger()
    ledger.record_http_backfill(make_event(DEVICE_A, 1))
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=3)

    ledger.record_receipt(DeliveryReceipt(key=BusinessKey(1, DEVICE_A), positions=()))
    page = next(
        page
        for page in ledger.snapshot().blocking_pages
        if page.key == BusinessKey(1, DEVICE_A)
    )
    assert page.origin == Origin.HTTP
    assert page.confirmed is True


def test_handoff_state_transitions_are_guarded() -> None:
    pending = MigrationLedger()
    with pytest.raises(HandoffStateError):
        pending.reopen_handoff()

    pending.commit_handoff()
    pending.reopen_handoff()
    with pytest.raises(HandoffStateError):
        pending.commit_handoff()


def test_signed_base_sequence_is_the_contiguity_floor() -> None:
    ledger = MigrationLedger(base_sequences={DEVICE_A: 10, DEVICE_B: 0})
    ledger.record_http_backfill(make_event(DEVICE_A, 11))

    assert ledger.next_contiguous_seqs() == {DEVICE_A: 12, DEVICE_B: 1}
    # An integer base applies uniformly to sources without an explicit base.
    uniform = MigrationLedger(base_sequences=5)
    uniform.record_http_backfill(make_event(DEVICE_B, 5))
    assert uniform.next_contiguous_seqs() == {DEVICE_B: 6}


def test_structured_message_with_header_and_physical_key_separation() -> None:
    ledger = MigrationLedger()
    event = make_event(DEVICE_A, 1, partitionkey="header-name")
    structured = to_structured_event(event)
    message = type(structured)(
        headers=structured.headers, key=b"physical-key", value=structured.value
    )

    record = ledger.record_kafka_message(message, partition=OLD, offset=2)
    assert record.key == BusinessKey(1, DEVICE_A)
    assert record.partition_keys == ("header-name",)
    assert record.physical_keys == ("physical-key",)


def test_snapshot_dict_rendering_and_batch_gap_report() -> None:
    ledger = MigrationLedger()
    ledger.record_kafka_event(make_event(DEVICE_A, 1), partition=OLD, offset=0)
    report = BatchReport(
        line="old",
        read_ranges=(PartitionReadRange(OLD, 0, 5),),
        gap_ranges=(GapRange(DEVICE_A, 2, 3),),
    )
    ledger.record_batch(report)

    rendered = ledger.snapshot().as_dict()
    assert rendered["handoff_state"] == "pending"
    assert rendered["partition_positions"] == {OLD: 0}
    assert rendered["batches"][0]["line"] == "old"
    assert rendered["sealed_gaps"] == [{"source": DEVICE_A, "start": 2, "end": 3}]
    assert rendered["blocking_positions"] == [f"{OLD}@0"]


def test_receipt_for_unknown_page_is_rejected() -> None:
    ledger = MigrationLedger()
    with pytest.raises(MigrationLedgerError):
        ledger.record_receipt(
            DeliveryReceipt(
                key=BusinessKey(42, DEVICE_A),
                positions=(PhysicalPosition(OLD, 0),),
            )
        )
