from io import BytesIO

from thriftpy2.contrib.compact_edit import (
    ADD,
    CLEAR,
    REPLACE,
    Edit,
    Field,
    Item,
    apply_corrections,
)
from thriftpy2.protocol.compact import TCompactProtocol, make_zig_zag
from thriftpy2.thrift import TPayload, TType


class TDetail(TPayload):
    """Old definition of a detail record."""
    thrift_spec = {
        1: (TType.I32, "sku", False),
        2: (TType.STRING, "note", False),
        3: (TType.I32, "qty", False),
    }
    default_spec = [("sku", None), ("note", None), ("qty", None)]


class TBatch(TPayload):
    """Old definition of the inbound message."""
    thrift_spec = {
        1: (TType.STRING, "batch_id", False),
        2: (TType.LIST, "details", (TType.STRUCT, TDetail), False),
    }
    default_spec = [("batch_id", None), ("details", None)]


class TDetailV2(TPayload):
    """Newer definition that also knows fields 4 and 5."""
    thrift_spec = {
        1: (TType.I32, "sku", False),
        2: (TType.STRING, "note", False),
        3: (TType.I32, "qty", False),
        4: (TType.BOOL, "fragile", False),
        5: (TType.STRING, "extra", False),
    }
    default_spec = [("sku", None), ("note", None), ("qty", None),
                    ("fragile", None), ("extra", None)]


class TBatchV2(TPayload):
    thrift_spec = {
        1: (TType.STRING, "batch_id", False),
        2: (TType.LIST, "details", (TType.STRUCT, TDetailV2), False),
    }
    default_spec = [("batch_id", None), ("details", None)]


def varint(n):
    out = bytearray()
    while True:
        if n & ~0x7F == 0:
            out.append(n)
            return bytes(out)
        out.append((n & 0xFF) | 0x80)
        n >>= 7


def i32(value):
    return varint(make_zig_zag(value, 32))


def string(text):
    raw = text.encode("utf-8")
    return varint(len(raw)) + raw


def decode(cls, data):
    obj = cls()
    obj.read(TCompactProtocol(BytesIO(data)))
    return obj


# detail 0: an ordinary record (sku=1, qty=9)
DETAIL0 = bytes([
    0x15, 0x02,        # field 1 I32 sku = 1
    0x25, 0x12,        # field 3 I32 qty = 9 (delta 2)
    0x00,              # STOP
])

# detail 1: field 3 occurs three times -- two compatible I32 values (the old
# reader adopts the second) plus an incompatible STRING kept for newer
# readers -- followed by a bool (4) and a string (5) the old definition
# does not know at all.
DETAIL1 = (
    bytes([0x35]) + i32(11) +                    # field 3 I32 = 11
    bytes([0x05, 0x06]) + i32(22) +              # field 3 I32 = 22 (long form)
    bytes([0x08, 0x06]) + string("oops") +       # field 3 STRING "oops"
    bytes([0x11]) +                              # field 4 bool TRUE (delta 1)
    bytes([0x18]) + string("zz") +               # field 5 STRING "zz"
    bytes([0x00])                                # STOP
)

ORIGINAL = (
    bytes([0x18]) + string("B1") +               # field 1 STRING "B1"
    bytes([0x19, 0x2C]) +                        # field 2 LIST<STRUCT>, size 2
    DETAIL0 + DETAIL1 +
    bytes([0x00])                                # STOP
)


def test_original_reads_with_old_definition():
    batch = decode(TBatch, ORIGINAL)
    assert batch.batch_id == "B1"
    assert len(batch.details) == 2
    assert batch.details[0].sku == 1
    assert batch.details[0].qty == 9
    # the old reader adopts the last compatible occurrence
    assert batch.details[1].sku is None
    assert batch.details[1].qty == 22


def test_replace_edits_the_adopted_duplicate():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(1), Field(3)), REPLACE, 99),
    ])
    assert result.applied
    batch = decode(TBatch, result.data)
    assert batch.details[1].qty == 99
    assert batch.details[0].qty == 9

    expected_detail1 = (
        bytes([0x35]) + i32(11) +                 # first duplicate untouched
        bytes([0x05, 0x06]) + i32(99) +           # adopted occurrence replaced
        bytes([0x08, 0x06]) + string("oops") +
        bytes([0x11]) +
        bytes([0x18]) + string("zz") +
        bytes([0x00])
    )
    assert expected_detail1 in result.data
    # the incompatible same-id value and the unknown fields survive
    assert decode(TBatchV2, result.data).details[1].fragile is True
    assert decode(TBatchV2, result.data).details[1].extra == "zz"


def test_clear_removes_every_compatible_duplicate():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(1), Field(3)), CLEAR),
    ])
    assert result.applied
    batch = decode(TBatch, result.data)
    # neither compatible occurrence may resurface
    assert batch.details[1].qty is None

    expected_detail1 = (
        bytes([0x38]) + string("oops") +   # same-id string kept, header redone
        bytes([0x11]) +                    # unknown bool keeps id 4 and value
        bytes([0x18]) + string("zz") +
        bytes([0x00])
    )
    assert expected_detail1 in result.data
    batch_v2 = decode(TBatchV2, result.data)
    assert batch_v2.details[1].fragile is True
    assert batch_v2.details[1].extra == "zz"


def test_clear_rewrites_following_unknown_bool_header():
    class TControl(TPayload):
        thrift_spec = {1: (TType.I32, "n", False)}
        default_spec = [("n", None)]

    class TControlV2(TPayload):
        thrift_spec = {
            1: (TType.I32, "n", False),
            2: (TType.BOOL, "flag", False),
        }
        default_spec = [("n", None), ("flag", None)]

    original = bytes([
        0x15, 0x0A,   # field 1 I32 = 5
        0x11,         # field 2 bool TRUE (delta 1), unknown to old def
        0x00,
    ])
    result = apply_corrections(original, TControl, [
        Edit((Field(1),), CLEAR),
    ])
    assert result.applied
    # the bool keeps field id 2 and its value; only the delta header moves
    assert result.data == bytes([0x21, 0x00])
    assert decode(TControl, result.data).n is None
    assert decode(TControlV2, result.data).flag is True


def test_add_fills_missing_value():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(0), Field(2)), ADD, "hi"),
    ])
    assert result.applied
    batch = decode(TBatch, result.data)
    assert batch.details[0].note == "hi"
    assert batch.details[0].qty == 9

    expected_detail0 = (
        bytes([0x15, 0x02, 0x25, 0x12]) +     # original fields untouched
        bytes([0x08, 0x04]) + string("hi") +  # appended before STOP
        bytes([0x00])
    )
    assert expected_detail0 in result.data


def test_add_top_level_scalar():
    original = bytes([0x19, 0x2C]) + DETAIL0 + DETAIL0 + bytes([0x00])
    result = apply_corrections(original, TBatch, [
        Edit((Field(1),), ADD, "B2"),
    ])
    assert result.applied
    assert decode(TBatch, result.data).batch_id == "B2"


def test_replace_bool_flips_header_nibble_only():
    class TFlags(TPayload):
        thrift_spec = {
            1: (TType.BOOL, "enabled", False),
            2: (TType.I32, "count", False),
        }
        default_spec = [("enabled", None), ("count", None)]

    original = bytes([0x11, 0x15, 0x06, 0x00])  # enabled=TRUE, count=3
    result = apply_corrections(original, TFlags, [
        Edit((Field(1),), REPLACE, False),
    ])
    assert result.applied
    assert result.data == bytes([0x12, 0x15, 0x06, 0x00])
    flags = decode(TFlags, result.data)
    assert flags.enabled is False
    assert flags.count == 3


def test_descend_through_struct_field():
    class TEnvelope(TPayload):
        thrift_spec = {
            1: (TType.STRUCT, "detail", TDetail, False),
        }
        default_spec = [("detail", None)]

    inner = bytes([0x15, 0x0E, 0x25, 0x02, 0x00])  # sku=7, qty=1
    original = bytes([0x1C]) + inner + bytes([0x00])
    result = apply_corrections(original, TEnvelope, [
        Edit((Field(1), Field(3)), REPLACE, 42),
    ])
    assert result.applied
    envelope = decode(TEnvelope, result.data)
    assert envelope.detail.sku == 7
    assert envelope.detail.qty == 42


def test_editing_second_detail_keeps_first_detail_bytes():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(1), Field(3)), REPLACE, 99),
    ])
    assert result.applied
    assert DETAIL0 in result.data
    old_start = ORIGINAL.find(DETAIL0)
    span = next(s for s in result.spans if s.old_start == old_start)
    assert span.old_end == old_start + len(DETAIL0)
    assert span.new_end - span.new_start == len(DETAIL0)
    assert result.data[span.new_start:span.new_end] == DETAIL0


def test_trace_spans_cover_unedited_values():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(1), Field(3)), REPLACE, 99),
    ])
    assert result.applied
    for span in result.spans:
        old_bytes = ORIGINAL[span.old_start:span.old_end]
        assert result.data[span.new_start:span.new_end] == old_bytes

    # values that must be traceable: batch id, first detail, the first
    # duplicate, the incompatible same-id string, the unknown string
    traced = [(s.old_start, s.old_end) for s in result.spans]
    assert (1, 4) in traced                       # "B1" value bytes
    oops_at = ORIGINAL.find(b"\x04oops")
    assert (oops_at, oops_at + 5) in traced       # unknown-to-edit string
    zz_at = ORIGINAL.find(b"\x02zz")
    assert (zz_at, zz_at + 3) in traced           # field 5, unknown to old def
    first_dup = ORIGINAL.find(bytes([0x16]))
    assert (first_dup, first_dup + 1) in traced   # unedited first duplicate


def test_unapplicable_edit_keeps_whole_original():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(1), Field(3)), REPLACE, 5),
        Edit((Field(2), Item(0), Field(1)), REPLACE, "not-an-int"),
    ])
    assert not result.applied
    assert result.data == ORIGINAL
    assert result.error
    assert result.spans == result.spans[0:1]
    assert (result.spans[0].old_start, result.spans[0].old_end) == \
        (0, len(ORIGINAL))
    # the original is still perfectly readable
    assert decode(TBatch, result.data).details[1].qty == 22


def test_unknown_field_edit_keeps_original():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(0), Field(9)), REPLACE, 1),
    ])
    assert not result.applied
    assert result.data == ORIGINAL


def test_out_of_range_item_keeps_original():
    result = apply_corrections(ORIGINAL, TBatch, [
        Edit((Field(2), Item(5), Field(1)), REPLACE, 1),
    ])
    assert not result.applied
    assert result.data == ORIGINAL
