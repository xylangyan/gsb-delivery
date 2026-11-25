import time

import asn1
from asn1.handoff import parse_tlv_tree
from asn1.handoff import plan_handoff


def decode_first(ber):
    decoder = asn1.Decoder()
    decoder.start(ber)
    tag, value = decoder.read(asn1.ReadFlags.WithUnused)
    return tag, value


def test_shift_fix_supports_three_unused_bits():
    tag, value = decode_first(b"\x03\x03\x03\xab\xf8")
    assert value == (b"\x15\x7f", 3)


def test_whole_constructed_fragment_can_be_borrowed_directly():
    source = bytes.fromhex("2308030200ab030203f8")
    archive = parse_tlv_tree(source)
    constructed = archive.node_at_range(0, 10)
    assert constructed.bit_length == 13
    assert constructed.unused == 3

    result = plan_handoff(source, [{"bits": b"\xab\xf8", "bit_length": 13}])
    plan = result.tables[0]
    assert plan.ber == source
    assert plan.cost == 0
    assert result.ranges == [[(0, 10)]]
    assert decode_first(plan.ber)[1] == (b"\x15\x7f", 3)


def test_terminal_fragment_cannot_be_sandwiched():
    first_body = bytes(range(1, 31))
    first_child = b"\x03\x1f\x00" + first_body
    last_child = bytes.fromhex("030203f8")
    content = first_child + last_child
    source = b"\x23" + bytes([len(content)]) + content
    tail = b"\x03\x1f\x00" + bytes(range(32, 62))
    volume = source + tail

    result = plan_handoff(volume, [{"ranges": [(0, len(source)), (len(source), len(volume))]}])
    plan = result.tables[0]
    borrowed = [(segment.source_start, segment.source_end) for segment in plan.segments if segment.kind == "borrow"]

    assert (0, len(source)) not in borrowed
    assert (len(source) - 4, len(source)) not in borrowed
    assert (2, 2 + len(first_child)) in borrowed
    decode_first(plan.ber)


def test_each_source_tlv_is_borrowed_only_once():
    source = bytes.fromhex("2308030200ab030203f8")
    result = plan_handoff(source, [
        {"ranges": [(0, 10)]},
        {"ranges": [(0, 10)]},
    ])

    assert result.cost == 5
    assert result.ranges[0] == [(0, 10)]
    assert result.ranges[1] == []
    assert len(result.table_bytes[0]) == 10
    assert result.table_bytes[1] == bytes.fromhex("030303abf8")

    first = decode_first(result.table_bytes[0])[1]
    second = decode_first(result.table_bytes[1])[1]
    assert first == second == (b"\x15\x7f", 3)


def test_whole_ancestor_blocks_descendant_borrows():
    source = bytes.fromhex("2308030200ab030203f8")
    result = plan_handoff(source, [
        {"ranges": [(0, 10)]},
        {"ranges": [(2, 6), (6, 10)]},
    ])

    assert result.ranges[0] == [(0, 10)]
    assert result.ranges[1] == []
    assert result.cost == 5


def test_indefinite_eoc_is_distinguished_from_zero_content_bytes():
    ber = bytes.fromhex("238003030000000000")
    archive = parse_tlv_tree(ber)
    assert archive.roots[0].indefinite is True
    assert archive.roots[0].end == len(ber)
    tag, value = decode_first(ber)
    assert value == (b"\x00\x00", 0)


def test_many_disjoint_fragments_plan_quickly():
    parts = []
    ranges = []
    position = 0
    for index in range(1000):
        tlv = b"\x03\x09\x00" + bytes([index & 0xFF]) * 8
        parts.append(tlv)
        ranges.append((position, position + len(tlv)))
        position += len(tlv)
    source = b"".join(parts)

    started = time.perf_counter()
    result = plan_handoff(source, [{"ranges": ranges}])
    elapsed = time.perf_counter() - started

    assert len(result.ranges[0]) == 1000
    assert result.cost == 4
    assert elapsed < 2.0
    decode_first(result.table_bytes[0])
