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

"""
In-memory handoff ledger for migrating devices between old and new Kafka
partitions while reporting continues and HTTP backfills keep arriving.

The ledger preserves each device's original sequence number for the business
account while tracking physical Kafka keys and the partitions messages
actually landed on separately. See :class:`~cloudevents.migration.ledger.MigrationLedger`
for the full intake and dashboard API.
"""

from cloudevents.migration.exceptions import (
    DuplicateClaimError,
    HandoffStateError,
    MigrationClosedError,
    MigrationLedgerError,
    MissingIdentityError,
)
from cloudevents.migration.identity import (
    business_key_of,
    header_partition_key_of,
    partition_key_of,
    physical_key_of,
    position_of,
)
from cloudevents.migration.ledger import MigrationLedger
from cloudevents.migration.models import (
    BatchReport,
    BusinessKey,
    DeliveryReceipt,
    GapRange,
    HandoffState,
    LedgerSnapshot,
    Origin,
    PageRecord,
    PartitionReadRange,
    PhysicalPosition,
)

__all__ = [
    "BatchReport",
    "BusinessKey",
    "DeliveryReceipt",
    "DuplicateClaimError",
    "GapRange",
    "HandoffState",
    "HandoffStateError",
    "LedgerSnapshot",
    "MigrationClosedError",
    "MigrationLedger",
    "MigrationLedgerError",
    "MissingIdentityError",
    "Origin",
    "PageRecord",
    "PartitionReadRange",
    "PhysicalPosition",
    "business_key_of",
    "header_partition_key_of",
    "partition_key_of",
    "physical_key_of",
    "position_of",
]
