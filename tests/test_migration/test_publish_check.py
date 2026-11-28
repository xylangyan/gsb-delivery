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

import json
from typing import Any

import pytest

from cloudevents.core.bindings import kafka as core_kafka
from cloudevents.core.formats.json import JSONFormat
from cloudevents.migration import (
    BINARY,
    COMPAT,
    CORE,
    STRUCTURED,
    DataRequirement,
    DatacontenttypeRequirement,
    ExtensionRequirement,
    Link,
    Node,
    Topology,
    build_default_topology,
    check_http,
    replay_hop,
)
from cloudevents.v1 import kafka as compat_kafka

STRUCTURED_HEADERS = {"content-type": "application/cloudevents+json"}
FMT = JSONFormat()


def http_body(
    lane: Any,
    *,
    data: Any = ...,
    data_base64: str | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "specversion": "1.0",
        "id": "evt-1",
        "source": "/dev",
        "type": "com.example.event",
        "time": "2026-11-28T00:00:00Z",
        "datacontenttype": "application/json",
        "lane": lane,
    }
    if data_base64 is not None:
        payload["data_base64"] = data_base64
    elif data is not ...:
        payload["data"] = data
    elif data is ...:
        payload["data"] = {"n": 1}
    return json.dumps(payload).encode()


def replay(hop):
    """Re-read a hop through the receiver's real original SDK entry."""
    if hop.receiver_sdk == CORE:
        if hop.receiver_mode == STRUCTURED:
            return core_kafka.from_structured(hop.message, FMT)
        return core_kafka.from_binary(hop.message, FMT)
    message = compat_kafka.KafkaMessage(
        headers=dict(hop.message.headers),
        key=hop.message.key,
        value=hop.message.value,
    )
    if hop.receiver_mode == STRUCTURED:
        return compat_kafka.from_structured(message)
    return compat_kafka.from_binary(message)


def hop(plan, source, target):
    matches = [
        hop_record
        for hop_record in plan.hops
        if hop_record.source == source and hop_record.target == target
    ]
    assert len(matches) == 1
    return matches[0]


def test_history_store_only_int_lane_is_zero_cost() -> None:
    result = check_http(
        STRUCTURED_HEADERS, http_body(7), build_default_topology(), ("B",)
    )
    assert result.feasible
    plan = result.best_plan
    assert plan is not None
    assert plan.cost == 0
    assert plan.routes["B"] == ("G", "L", "B")
    assert plan.g_message.key is None


def test_both_subscribers_int_lane_cost_three() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7),
        build_default_topology(),
        ("A", "B"),
    )
    assert result.feasible
    plan = result.best_plan
    assert plan is not None
    assert plan.cost == 3
    assert plan.g_mode == STRUCTURED
    assert plan.routes == {
        "A": ("G", "A"),
        "B": ("G", "P", "L", "B"),
    }
    # One publisher message is shared by every enabled downstream connection.
    for hop_record in plan.hops:
        if hop_record.source == "G":
            assert hop_record.message is plan.g_message


def test_cost_three_plan_replays_through_original_entries() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7),
        build_default_topology(),
        ("A", "B"),
    )
    plan = result.best_plan
    assert plan is not None

    # A re-reads G's structured core message: int lane and dict body survive.
    a_event = replay(hop(plan, "G", "A"))
    assert a_event.get_extension("lane") == 7
    assert isinstance(a_event.get_extension("lane"), int)
    assert a_event.get_data() == {"n": 1}

    # P re-reads G structured with the core entry; lane stays an int.
    p_event = replay(hop(plan, "G", "P"))
    assert isinstance(p_event.get_extension("lane"), int)

    # L re-reads P's binary message through the compat v1 entry.
    l_event = replay(hop(plan, "P", "L"))
    assert l_event.get_attributes()["datacontenttype"] == "application/json"

    # B re-reads L's binary output through the core entry.
    b_event = replay(hop(plan, "L", "B"))
    assert b_event.get_data() == {"n": 1}
    assert isinstance(b_event.get_data(), dict)
    assert b_event.get_datacontenttype() == "application/json"

    # replay_hop() returns the same event as calling the entry directly.
    assert replay_hop(hop(plan, "L", "B")).get_data() == b_event.get_data()


def test_removing_spare_relay_names_two_conflicting_handoffs() -> None:
    topology = build_default_topology()
    links = tuple(
        link
        for link in topology.links
        if not ((link.source, link.target) in {("G", "P"), ("P", "L")})
    )
    reduced = Topology(nodes=topology.nodes, links=links)

    result = check_http(
        STRUCTURED_HEADERS, http_body(7), reduced, ("A", "B")
    )
    assert not result.feasible
    assert result.best_plan is None

    handoffs = {conflict.handoff for conflict in result.conflicts}
    assert handoffs == {"G -> A", "G -> L"}

    a_conflicts = [c for c in result.conflicts if c.handoff == "G -> A"]
    assert {c.g_mode for c in a_conflicts} == {BINARY}
    assert any("lane" in c.detail for c in a_conflicts)
    assert any("lane=7" in lost for c in a_conflicts for lost in c.lost)

    l_conflicts = [c for c in result.conflicts if c.handoff == "G -> L"]
    assert {c.g_mode for c in l_conflicts} == {STRUCTURED}
    # Structured mode strips the explicit datacontenttype on the compat relay.
    assert any("datacontenttype" in c.detail for c in l_conflicts)
    assert all(conflict.chain[0] == "G" for conflict in result.conflicts)


def test_string_lane_allows_zero_cost_for_both() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body("7"),
        build_default_topology(),
        ("A", "B"),
    )
    assert result.feasible
    plan = result.best_plan
    assert plan is not None
    assert plan.cost == 0
    assert plan.g_mode == BINARY
    assert plan.routes == {
        "A": ("G", "A"),
        "B": ("G", "L", "B"),
    }
    a_event = replay(hop(plan, "G", "A"))
    assert a_event.get_extension("lane") == "7"
    assert isinstance(a_event.get_extension("lane"), str)
    b_event = replay(hop(plan, "L", "B"))
    assert b_event.get_datacontenttype() == "application/json"
    assert b_event.get_data() == {"n": 1}


def test_empty_byte_body_cannot_reuse_object_body_plan() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7, data_base64=""),
        build_default_topology(),
        ("A", "B"),
    )
    assert not result.feasible
    details = " ".join(c.detail for c in result.conflicts)
    lost_values = tuple(
        value for conflict in result.conflicts for value in conflict.lost
    )
    # The zero-byte body kind (bytes, not dict) is re-checked, not assumed.
    assert "data" in details
    assert any("data=b''" in value for value in lost_values)
    # The compat binary entry cannot parse an empty JSON value.
    assert any("receive failed" in c.detail for c in result.conflicts)


def test_invalid_http_original_is_reported_as_original_error() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        b"{not-json",
        build_default_topology(),
        ("B",),
    )
    assert not result.feasible
    assert result.original_error is not None
    assert result.conflicts == ()


def test_unknown_required_subscriber_is_rejected() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7),
        build_default_topology(),
        ("Z",),
    )
    assert not result.feasible
    assert result.conflicts[0].handoff == "Z"


def test_multilevel_topology_with_shared_relay() -> None:
    # G -> R1 (core relay, structured in / binary out) -> R2 (compat relay) -> S1
    # and R1 -> S2, so R1 is a shared publish point emitting one binary message.
    nodes = {
        "G": Node("G", "publisher", CORE, (), (STRUCTURED, BINARY)),
        "R1": Node("R1", "relay", CORE, (STRUCTURED,), (BINARY,)),
        "R2": Node(
            "R2",
            "relay",
            COMPAT,
            (STRUCTURED, BINARY),
            (STRUCTURED, BINARY),
        ),
        "S1": Node(
            "S1",
            "subscriber",
            CORE,
            (STRUCTURED, BINARY),
            (),
            (DataRequirement(), DatacontenttypeRequirement()),
        ),
        "S2": Node(
            "S2",
            "subscriber",
            CORE,
            (STRUCTURED, BINARY),
            (),
            (ExtensionRequirement("lane"),),
        ),
    }
    links = (
        Link("G", "R1", 1),
        Link("R1", "R2", 2),
        Link("R2", "S1", 0),
        Link("R1", "S2", 4),
    )
    topology = Topology(nodes=nodes, links=links)

    result = check_http(
        STRUCTURED_HEADERS, http_body("7"), topology, ("S1", "S2")
    )
    assert result.feasible
    plan = result.best_plan
    assert plan is not None
    assert plan.cost == 7
    assert plan.g_mode == STRUCTURED
    assert plan.routes == {
        "S1": ("G", "R1", "R2", "S1"),
        "S2": ("G", "R1", "S2"),
    }
    assert plan.relay_modes["R1"] == (STRUCTURED, BINARY)
    assert plan.relay_modes["R2"] == (BINARY, BINARY)

    # The shared R1 publish point hands its single binary message to both legs.
    r1_hops = [h for h in plan.hops if h.source == "R1"]
    assert len(r1_hops) == 2
    assert r1_hops[0].message == r1_hops[1].message

    s1_event = replay(hop(plan, "R2", "S1"))
    assert s1_event.get_data() == {"n": 1}
    assert s1_event.get_datacontenttype() == "application/json"
    s2_event = replay(hop(plan, "R1", "S2"))
    assert s2_event.get_extension("lane") == "7"


def test_every_kafka_key_is_empty() -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7),
        build_default_topology(),
        ("A", "B"),
    )
    plan = result.best_plan
    assert plan is not None
    assert plan.g_message.key is None
    for hop_record in plan.hops:
        assert hop_record.message.key is None


def test_single_message_sdk_entries_remain_usable() -> None:
    # The existing one-shot core and compat entries keep working unchanged.
    result = check_http(
        STRUCTURED_HEADERS, http_body(7), build_default_topology(), ("A",)
    )
    message = result.best_plan.g_message

    core_event = core_kafka.from_kafka(message, FMT)
    assert core_event.get_extension("lane") == 7

    compat_message = compat_kafka.KafkaMessage(
        headers=dict(message.headers), key=message.key, value=message.value
    )
    compat_event = compat_kafka.from_structured(compat_message)
    assert compat_event.get_attributes()["lane"] == 7


@pytest.mark.parametrize(
    "required,expected_cost",
    [(("B",), 0), (("A",), 0), (("A", "B"), 3)],
)
def test_minimum_cost_selection(required, expected_cost) -> None:
    result = check_http(
        STRUCTURED_HEADERS,
        http_body(7),
        build_default_topology(),
        required,
    )
    assert result.feasible
    assert result.best_plan.cost == expected_cost
    assert all(plan.cost >= expected_cost for plan in result.plans)
