from __future__ import annotations

import time

from zeroconf.inventory import HandoverLedger, InventoryQuery, query_inventory


TXT_HEX = "76657273696f6e3d6e6577"
TXT_BYTES = bytes.fromhex(TXT_HEX)


def worked_data(offset_range=(1, 2), pair_range=None):
    pairs = []
    if pair_range is not None:
        pairs.append(
            {
                "id": "sync-a1-b1",
                "packets": ["a1", "b1"],
                "primary": "a1",
                "receive_diff_range": pair_range,
            }
        )
    return {
        "offset_range": list(offset_range),
        "cleanups": [2001],
        "pairs": pairs,
        "packets": [
            {
                "id": "a1",
                "source": "a",
                "seq": 1,
                "time": 1000,
                "records": [
                    {"type": "A", "name": "host.local.", "address": "1.2.3.4", "ttl": 1},
                    {
                        "type": "SRV",
                        "name": "inst._tcp.local.",
                        "target": "host.local.",
                        "port": 80,
                        "ttl": 10000,
                    },
                ],
            },
            {
                "id": "b1",
                "source": "b",
                "seq": 1,
                "time": 2000,
                "records": [
                    {
                        "type": "A",
                        "name": "host.local.",
                        "address": "5.6.7.8",
                        "ttl": 10000,
                        "cache_flush": True,
                    }
                ],
            },
            {
                "id": "a2",
                "source": "a",
                "seq": 2,
                "time": 2099,
                "records": [
                    {"type": "TXT", "name": "inst._tcp.local.", "txt_hex": TXT_HEX, "ttl": 10000}
                ],
            },
            {
                "id": "b2",
                "source": "b",
                "seq": 2,
                "time": 2099,
                "records": [
                    {"type": "TXT", "name": "inst._tcp.local.", "txt_hex": TXT_HEX, "ttl": 0}
                ],
            },
        ],
    }


def worked_ledger(offset_range=(1, 2), pair_range=None):
    return HandoverLedger.from_dict(worked_data(offset_range, pair_range))


def query(at):
    return {"id": f"q{at}", "at": at, "service": "inst._tcp.local", "txt_hex": TXT_HEX, "ipv4": "1.2.3.4"}


def test_worked_example_distinguishes_offset_one_and_two():
    ledger = worked_ledger()

    at_2099 = ledger.query(query(2099))
    assert at_2099.status == "partial"
    assert at_2099.true_example == 1
    assert at_2099.false_example == 2
    assert at_2099.true_ranges == [(1, 1)]
    assert at_2099.false_ranges == [(2, 2)]

    true_trace = ledger.replay(1, 2099, query(2099))
    false_trace = ledger.replay(2, 2099, query(2099))
    assert [item["packet_id"] for item in true_trace.packet_order] == ["a1", "b1", "a2"]
    assert [item["packet_id"] for item in false_trace.packet_order] == ["a1", "b1", "a2"]
    assert true_trace.packet_order[1]["adjusted_time"] == 2001
    assert false_trace.packet_order[1]["adjusted_time"] == 2002
    assert true_trace.cleanups == [2001]
    assert false_trace.cleanups == [2001]
    assert true_trace.evidence.simultaneous is True
    assert false_trace.evidence.srv_address_present is False


def test_at_2100_each_component_survives_under_a_different_offset():
    ledger = worked_ledger()
    result = ledger.query(query(2100))

    assert result.status == "none"
    assert result.true_example is None
    assert result.false_ranges == [(1, 2)]

    offset_one = ledger.replay(1, 2100, query(2100)).evidence
    offset_two = ledger.replay(2, 2100, query(2100)).evidence
    assert offset_one.srv_address_present is True
    assert offset_one.txt_present is False
    assert offset_two.srv_address_present is False
    assert offset_two.txt_present is True


def test_pair_primary_suppresses_other_packet():
    ledger = HandoverLedger.from_dict(
        {
            "offset_range": [0, 0],
            "pairs": [{"id": "p", "packets": ["a1", "b1"], "primary": "b1", "receive_diff_range": [5, 5]}],
            "packets": [
                {"id": "a1", "source": "a", "seq": 1, "time": 100, "records": []},
                {"id": "b1", "source": "b", "seq": 7, "time": 105, "records": []},
            ],
        }
    )
    trace = ledger.replay(0, 200)
    assert [item["packet_id"] for item in trace.packet_order] == ["b1"]
    assert ledger.feasible_offsets() == (0, 0)


def test_pair_primary_can_be_marked_on_packet_index():
    ledger = HandoverLedger.from_dict(
        {
            "offset_range": [0, 0],
            "packets": [
                {"id": "a1", "source": "a", "time": 100, "pair": "same", "primary": False, "records": []},
                {"id": "b1", "source": "b", "time": 100, "pair": "same", "primary": True, "records": []},
            ],
        }
    )
    trace = ledger.replay(0, 200)
    assert [item["packet_id"] for item in trace.packet_order] == ["b1"]
    assert "same" in ledger.pairs


def test_unpaired_identical_packets_are_not_deduplicated():
    packet = {
        "source": "a",
        "time": 100,
        "records": [{"type": "TXT", "name": "svc.local.", "text": "same", "ttl": 100}],
    }
    ledger = HandoverLedger.from_dict(
        {
            "offset_range": [0, 0],
            "packets": [
                {**packet, "id": "x1", "seq": 1},
                {**packet, "id": "x2", "seq": 2},
            ],
        }
    )
    trace = ledger.replay(0, 101)
    assert [item["packet_id"] for item in trace.packet_order] == ["x1", "x2"]


def test_pair_limit_revisions_and_conflict():
    data = worked_data()
    data["packets"].extend(
        [
            {"id": "sync-a", "source": "a", "seq": 90, "time": 5000, "records": []},
            {"id": "sync-b", "source": "b", "seq": 90, "time": 5003, "records": []},
        ]
    )
    data["pairs"] = [{"id": "sync", "packets": ["sync-a", "sync-b"], "primary": "sync-a"}]
    ledger = HandoverLedger.from_dict(data)
    assert ledger.feasible_offsets("original") == (1, 2)

    tightened = ledger.with_pair_limit("sync", [5, 5], revision="signed")
    original = ledger.query(query(2099))
    signed = tightened.query(query(2099), revision="signed")
    assert original.status == "partial"
    assert signed.status == "none"
    assert signed.feasible_offsets == (2, 2)
    signed_checks = tightened.query(query(2099), revision="signed")
    assert signed_checks.pair_checks["false"][0]["adjusted_b_minus_a"] == 5
    assert signed_checks.pair_checks["false"][0]["satisfied"] is True

    conflicting = ledger.with_pair_limit("sync", [8, 8], revision="bad")
    result = conflicting.query(query(2099), revision="bad")
    assert result.status == "conflict"
    assert {item["id"] for item in result.conflicts} == {
        "handover.offset_range",
        "pair:sync",
    }


def test_same_millisecond_orders_a_before_b_then_cleanup():
    ledger = HandoverLedger.from_dict(
        {
            "offset_range": [100, 100],
            "cleanups": [200],
            "packets": [
                {
                    "id": "a-zero",
                    "source": "a",
                    "seq": 2,
                    "time": 200,
                    "records": [{"type": "TXT", "name": "svc.local.", "text": "v", "ttl": 0}],
                },
                {
                    "id": "b-positive",
                    "source": "b",
                    "seq": 1,
                    "time": 100,
                    "records": [{"type": "TXT", "name": "svc.local.", "text": "v", "ttl": 1000}],
                },
            ],
        }
    )
    trace = ledger.replay(100, 300, {"service": "svc.local.", "txt": b"v", "ipv4": "1.2.3.4"})
    assert [item["packet_id"] for item in trace.packet_order] == ["a-zero", "b-positive"]
    assert trace.evidence.txt_present is True


def test_wide_offset_range_does_not_iterate_milliseconds():
    ledger = worked_ledger((-302_400_000, 302_400_000))
    start = time.perf_counter()
    result = ledger.query(query(2099))
    elapsed = time.perf_counter() - start
    assert result.status == "partial"
    assert result.true_example == 1
    assert result.false_example is not None
    assert elapsed < 1.0


def test_300k_unrelated_packets_do_not_enter_projection():
    data = worked_data()
    noise = []
    for index in range(300_000):
        second = (index >> 8) & 255
        last = index & 255
        noise.append(
            {
                "id": f"noise-{index}",
                "source": "a" if index % 2 == 0 else "b",
                "seq": index + 1,
                "time": 10_000 + index,
                "records": [
                    {
                        "type": "A",
                        "name": f"noise-{index}.local.",
                        "address": f"10.0.{second}.{last}",
                        "ttl": 1000,
                    }
                ],
            }
        )
    data["packets"] = noise + data["packets"]
    ledger = HandoverLedger.from_dict(data)
    normalized = InventoryQuery.from_dict(query(2099))
    projection = ledger.projection(normalized)

    assert len(projection.packet_indices) == 4
    start = time.perf_counter()
    result = ledger.query(normalized)
    assert result.status == "partial"
    assert time.perf_counter() - start < 0.2


def test_query_inventory_convenience_entry():
    result = query_inventory(
        worked_ledger(),
        2099,
        service="inst._tcp.local.",
        txt=TXT_BYTES,
        ipv4="1.2.3.4",
    )
    assert result.status == "partial"
    assert result.true_example == 1
