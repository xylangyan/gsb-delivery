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

from collections.abc import Mapping
from json import JSONDecodeError, loads
from typing import Any

from cloudevents.core.bindings.kafka import (
    CE_PREFIX,
    PARTITIONKEY_ATTR,
    KafkaMessage,
)
from cloudevents.migration.exceptions import MissingIdentityError
from cloudevents.migration.models import (
    SEQUENCE_ATTR,
    SOURCE_ATTR,
    BusinessKey,
    PhysicalPosition,
)


def attributes_of(event_like: Any) -> Mapping[str, Any]:
    """
    Normalize a CloudEvent-like object to an attribute mapping.

    Accepts the v2 core events (``BaseCloudEvent``), the v1 compatibility
    layer events (which expose ``get_attributes``) and plain mappings, so the
    legacy SDK entry points that know nothing about the handoff rules keep
    working unchanged.
    """
    if isinstance(event_like, Mapping):
        return event_like
    get_attributes = getattr(event_like, "get_attributes", None)
    if get_attributes is not None:
        attributes = get_attributes()
        if isinstance(attributes, Mapping):
            return attributes
    raise TypeError(f"cannot read CloudEvent attributes from {type(event_like)!r}")


def business_key_of(event_like: Any) -> BusinessKey:
    """
    Extract the business identity (original sequence number + sender source).

    The sequence number travels in the ``seq`` CloudEvents extension and the
    sender in the standard ``source`` attribute. Equal payloads never imply
    the same page: only this pair does.

    :raises MissingIdentityError: if either attribute is absent.
    """
    attributes = attributes_of(event_like)
    sequence_raw = attributes.get(SEQUENCE_ATTR)
    source = attributes.get(SOURCE_ATTR)
    if sequence_raw is None or source in (None, ""):
        raise MissingIdentityError(
            "event is missing the business identity "
            f"('{SEQUENCE_ATTR}' and '{SOURCE_ATTR}' are required)"
        )
    try:
        sequence = int(sequence_raw)
    except (TypeError, ValueError) as error:
        raise MissingIdentityError(
            f"sequence attribute '{SEQUENCE_ATTR}' must be an integer, "
            f"got {sequence_raw!r}"
        ) from error
    return BusinessKey(seq=sequence, source=str(source))


def partition_key_of(event_like: Any) -> str | None:
    """
    Read the ``partitionkey`` CloudEvents extension.

    This is the name written into the event header, which may differ from the
    physical Kafka key and from the partition the message actually landed on.
    Note that parsing a keyed Kafka message overwrites this attribute with the
    physical key, so for wire messages prefer :func:`header_partition_key_of`.
    """
    value = attributes_of(event_like).get(PARTITIONKEY_ATTR)
    return None if value is None else str(value)


def header_partition_key_of(message: KafkaMessage) -> str | None:
    """
    Read the ``partitionkey`` name originally written into the wire message.

    The Kafka binding replaces a parsed event's ``partitionkey`` with the
    physical message key, which is why a debugger can show a partitionkey that
    is not the name selected when the event was produced. This helper inspects
    the raw message instead: the ``ce_partitionkey`` header in binary mode, or
    the ``partitionkey`` field of the structured JSON body.
    """
    for header_name, header_value in message.headers.items():
        if header_name.lower() == f"{CE_PREFIX}{PARTITIONKEY_ATTR}":
            return header_value.decode("utf-8")

    try:
        body = loads(message.value.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError):
        return None
    if isinstance(body, Mapping):
        value = body.get(PARTITIONKEY_ATTR)
        return None if value is None else str(value)
    return None


def physical_key_of(message: KafkaMessage) -> str | None:
    """Read the physical Kafka key carried beside a CloudEvent message."""
    if message.key is None:
        return None
    if isinstance(message.key, bytes):
        return message.key.decode("utf-8")
    return str(message.key)


def position_of(partition: str, offset: int) -> PhysicalPosition:
    """Build a physical position, rejecting negative offsets."""
    if offset < 0:
        raise ValueError(f"offset must not be negative, got {offset}")
    return PhysicalPosition(partition=partition, offset=offset)
