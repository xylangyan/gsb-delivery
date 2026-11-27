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
from cloudevents.core.v1.event import CloudEvent
from cloudevents.migration import (
    BusinessKey,
    GapRange,
    MissingIdentityError,
    PartitionReadRange,
    PhysicalPosition,
    business_key_of,
    partition_key_of,
    physical_key_of,
    position_of,
)
from cloudevents.v1.http import CloudEvent as LegacyCloudEvent


def make_event(source: str, seq: int, **extensions: object) -> CloudEvent:
    attributes = {
        "type": "com.example.measurement",
        "source": source,
        "id": f"{source}-{seq}",
        "seq": seq,
    }
    attributes.update(extensions)
    return CloudEvent(attributes=attributes, data={"seq": seq})


def test_business_key_requires_seq_and_source() -> None:
    with pytest.raises(MissingIdentityError):
        business_key_of(
            CloudEvent(attributes={"type": "t", "source": "/d", "id": "1"}, data=None)
        )
    with pytest.raises(MissingIdentityError):
        business_key_of({"type": "t", "id": "1", "seq": 1})
    with pytest.raises(MissingIdentityError):
        business_key_of({"type": "t", "source": "/d"})
    with pytest.raises(MissingIdentityError):
        business_key_of({"type": "t", "source": "/d", "seq": "not-an-int"})


def test_business_key_equality_is_identity_only() -> None:
    first = business_key_of(make_event("/d", 7))
    second = business_key_of(make_event("/d", 7))
    other_source = business_key_of(make_event("/other", 7))
    other_seq = business_key_of(make_event("/d", 8))

    assert first == second
    assert hash(first) == hash(second)
    assert first != other_source
    assert first != other_seq


def test_business_key_reads_legacy_sdk_event() -> None:
    legacy = LegacyCloudEvent(
        attributes={"type": "t", "source": "/legacy", "seq": 3}, data={"v": 1}
    )
    assert business_key_of(legacy) == BusinessKey(seq=3, source="/legacy")


def test_partitionkey_differs_from_physical_key() -> None:
    event = make_event("/d", 1, partitionkey="header-name")
    message = to_binary_event(event)
    physical = KafkaMessage(
        headers=message.headers, key=b"physical-key", value=message.value
    )

    assert partition_key_of(event) == "header-name"
    assert physical_key_of(physical) == "physical-key"


def test_physical_key_none_when_message_unkeyed() -> None:
    message = KafkaMessage(headers={}, key=None, value=b"")
    assert physical_key_of(message) is None
    assert partition_key_of({"source": "/d", "seq": 1}) is None


def test_position_and_gap_validation() -> None:
    assert position_of("p", 3) == PhysicalPosition("p", 3)
    with pytest.raises(ValueError):
        position_of("p", -1)

    gap = GapRange(source="/d", start=10, end=12)
    assert gap.contains(10) and gap.contains(12)
    assert not gap.contains(13)
    with pytest.raises(ValueError):
        GapRange(source="/d", start=12, end=10)

    with pytest.raises(ValueError):
        PartitionReadRange(partition="p", start_offset=9, end_offset=8)
