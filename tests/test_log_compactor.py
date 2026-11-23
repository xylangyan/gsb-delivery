"""Tests for log_compactor: unit scenarios plus a fuzz cross-check of the
symbolic equivalence model against real betterproto parse behaviour."""

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional

import betterproto

from log_compactor import logs_equivalent, minimize_log


class Color(betterproto.Enum):
    RED = 0
    GREEN = 1
    BLUE = 2


@dataclass
class SubV1(betterproto.Message):
    x: int = betterproto.int32_field(1)


@dataclass
class SubV2(betterproto.Message):
    x: int = betterproto.int32_field(1)


@dataclass
class GenV1(betterproto.Message):
    count: int = betterproto.int32_field(1)
    tags: List[int] = betterproto.int32_field(2)
    name: str = betterproto.string_field(3)
    legacy: int = betterproto.int32_field(4)  # removed in V2 -> unknown there
    labels: List[str] = betterproto.string_field(5)
    pick_a: int = betterproto.int32_field(6, group="pick")
    pick_b: str = betterproto.string_field(7, group="pick")
    props: Dict[str, int] = betterproto.map_field(
        8, betterproto.TYPE_STRING, betterproto.TYPE_INT32
    )
    sub: SubV1 = betterproto.message_field(9)
    opt: Optional[int] = betterproto.int32_field(10, optional=True)
    scores: List[int] = betterproto.int32_field(12)
    color: Color = betterproto.enum_field(13)
    flag: bool = betterproto.bool_field(14)
    ratio: float = betterproto.double_field(15)
    blob: bytes = betterproto.bytes_field(16)
    score_f: float = betterproto.float_field(17)


@dataclass
class GenV2(betterproto.Message):
    count: int = betterproto.int32_field(1)
    tags: List[int] = betterproto.int32_field(2)
    name: str = betterproto.string_field(3)
    # field 4 was removed; it is an unknown field here
    labels: List[str] = betterproto.string_field(5)
    pick_a: int = betterproto.int32_field(6, group="pick")
    pick_b: str = betterproto.string_field(7, group="pick")
    props: Dict[str, int] = betterproto.map_field(
        8, betterproto.TYPE_STRING, betterproto.TYPE_INT32
    )
    sub: SubV2 = betterproto.message_field(9)
    opt: Optional[int] = betterproto.int32_field(10, optional=True)
    extra: int = betterproto.int32_field(11)  # new in V2 -> unknown to V1
    scores: List[int] = betterproto.int32_field(12)
    color: Color = betterproto.enum_field(13)
    flag: bool = betterproto.bool_field(14)
    ratio: float = betterproto.double_field(15)
    blob: bytes = betterproto.bytes_field(16)
    score_f: float = betterproto.float_field(17)


@dataclass
class OneRepA(betterproto.Message):
    vals: List[int] = betterproto.int32_field(2)


@dataclass
class OneRepB(betterproto.Message):
    vals: List[int] = betterproto.int32_field(2)


# ---------------------------------------------------------------------------
# wire helpers


def varint(value):
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(number, wire_type):
    return varint((number << 3) | wire_type)


def vfield(number, value):
    return tag(number, 0) + varint(value)


def lfield(number, payload):
    return tag(number, 2) + varint(len(payload)) + payload


def sfield(number, text):
    return lfield(number, text.encode("utf-8"))


def packed(number, values):
    return lfield(number, b"".join(varint(v) for v in values))


def map_entry(key, value):
    return sfield(1, key) + vfield(2, value)


def f64field(number, value):
    import struct

    return tag(number, 1) + struct.pack("<d", value)


def f32field(number, value):
    import struct

    return tag(number, 5) + struct.pack("<f", value)


# ---------------------------------------------------------------------------
# unit scenarios


def test_empty_log():
    assert minimize_log(GenV1, GenV2, []) == (0, [])


def test_single_fragment_cannot_be_dropped():
    data = vfield(1, 5)
    # dropping everything would also lose the serialized-on-wire effect
    assert minimize_log(GenV1, GenV2, [(1, data)]) == (len(data), [1])


def test_append_before_full_replace_is_redundant_single_repeated_field():
    # with only one repeated field there is nobody to alias with, so the
    # append that a later packed replace wipes out can be dropped
    append = vfield(2, 5)
    replace = packed(2, [9])
    assert minimize_log(OneRepA, OneRepB, [(1, append), (2, replace)]) == (
        len(replace),
        [2],
    )


def test_append_before_replace_is_kept_when_other_repeated_fields_exist():
    # tags may share a list with labels/scores in the existing message, and
    # then the append is observable through the aliased field
    append = vfield(2, 5)
    replace = packed(2, [9])
    assert minimize_log(GenV1, GenV2, [(1, append), (2, replace)]) == (
        len(append) + len(replace),
        [1, 2],
    )


def test_appends_after_last_replace_are_kept():
    replace = packed(2, [1])
    tail1 = vfield(2, 7)
    tail2 = vfield(2, 8)
    frags = [(1, replace), (2, tail1), (3, tail2)]
    total = len(replace) + len(tail1) + len(tail2)
    assert minimize_log(OneRepA, OneRepB, frags) == (total, [1, 2, 3])


def test_earlier_replace_is_redundant():
    first = packed(2, [1])
    second = packed(2, [2])
    assert minimize_log(OneRepA, OneRepB, [(1, first), (2, second)]) == (
        len(second),
        [2],
    )


def test_unknown_field_bytes_pin_fragments():
    # field 4 is known to V1 (last writer wins, so frag 1 is redundant there)
    # but unknown to V2, where both raw chunks must survive in order
    frag1 = vfield(4, 1)
    frag2 = vfield(4, 2)
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (
        len(frag1) + len(frag2),
        [1, 2],
    )


def test_unknown_to_both_generations_is_kept():
    frag = vfield(15, 1)
    assert minimize_log(GenV1, GenV2, [(1, frag)]) == (len(frag), [1])


def test_field_known_only_in_second_generation():
    # field 11 is unknown to V1 (pins the fragment) but a plain field in V2
    frag = vfield(11, 3)
    assert minimize_log(GenV1, GenV2, [(1, frag)]) == (len(frag), [1])


def test_same_value_rewrite_can_be_dropped():
    frag1 = vfield(1, 5) + sfield(3, "x")  # count=5, name="x"
    frag2 = vfield(1, 5)  # count=5 again, nothing else
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (len(frag1), [1])


def test_same_column_same_value_but_different_side_fields():
    # both write count=5, but each carries another field the log still needs
    frag1 = vfield(1, 5) + sfield(3, "x")
    frag2 = vfield(1, 5) + vfield(10, 7)
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (
        len(frag1) + len(frag2),
        [1, 2],
    )


def test_oneof_earlier_member_is_redundant():
    frag1 = vfield(6, 3)  # pick_a, later cleared
    frag2 = sfield(7, "s")  # pick_b wins the group
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (len(frag2), [2])


def test_oneof_same_member_last_value_wins():
    frag1 = vfield(6, 3)
    frag2 = vfield(6, 4)
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (len(frag2), [2])


def test_map_overwritten_entry_is_redundant():
    frag1 = lfield(8, map_entry("a", 1))
    frag2 = lfield(8, map_entry("a", 2)) + lfield(8, map_entry("b", 3))
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (len(frag2), [2])


def test_map_distinct_keys_are_all_needed():
    frag1 = lfield(8, map_entry("a", 1)) + lfield(8, map_entry("b", 3))
    frag2 = lfield(8, map_entry("a", 2))
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (
        len(frag1) + len(frag2),
        [1, 2],
    )


def test_explicit_default_value_is_observable():
    # opt explicitly set to 0 differs from opt never set (None)
    frag = vfield(10, 0)
    assert minimize_log(GenV1, GenV2, [(1, frag)]) == (len(frag), [1])


def test_message_field_last_write_wins():
    frag1 = lfield(9, vfield(1, 1))
    frag2 = lfield(9, vfield(1, 2))
    assert minimize_log(GenV1, GenV2, [(1, frag1), (2, frag2)]) == (len(frag2), [2])


def test_aliasing_interleaving_pins_cross_field_appends():
    # tags gets replaced at the end, yet its earlier appends stay observable
    # through scores when the two lists are shared
    frag1 = vfield(2, 1)  # append tags
    frag2 = vfield(12, 2)  # append scores
    frag3 = packed(2, [9])  # replace tags
    frags = [(1, frag1), (2, frag2), (3, frag3)]
    total = sum(len(data) for _, data in frags)
    assert minimize_log(GenV1, GenV2, frags) == (total, [1, 2, 3])


# ---------------------------------------------------------------------------
# fuzz: symbolic model vs real betterproto behaviour


def _freeze(value):
    if isinstance(value, betterproto.Message):
        # the flag decides whether the parent serializes an "empty" submessage
        return ("msg", bytes(value), value._serialized_on_wire)
    if isinstance(value, list):
        return ("list", tuple(_freeze(v) for v in value))
    if isinstance(value, dict):
        return ("dict", {k: _freeze(v) for k, v in value.items()})
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return value


def _observable(msg):
    obs = {
        "sow": msg._serialized_on_wire,
        "unknown": msg._unknown_fields,
        "groups": dict(msg._group_current),
    }
    for name in msg._betterproto.sorted_field_names:
        try:
            value = getattr(msg, name)
        except AttributeError:
            value = "<unset-oneof-member>"
        obs[name] = _freeze(value)
    return obs


def _start_fresh(cls):
    return cls()


def _start_prefilled(cls):
    msg = cls(count=1, tags=[5, 6], name="n", labels=["a"], scores=[7])
    msg.props["z"] = 9
    msg._unknown_fields = tag(50, 0) + varint(3)
    return msg


def _start_rich(cls):
    return cls(
        count=9,
        name="q",
        pick_b="b",
        props={"k": 1},
        opt=0,
        color=Color.BLUE,
        flag=True,
        ratio=2.5,
        blob=b"ab",
        score_f=0.25,
    )


def _start_alias_tags_scores(cls):
    msg = cls()
    shared = [4, 5]
    msg.tags = shared
    msg.scores = shared
    return msg


def _start_alias_tags_labels(cls):
    msg = cls()
    shared = ["x", "y"]
    msg.tags = shared
    msg.labels = shared
    return msg


def _start_alias_scores_labels(cls):
    msg = cls()
    shared = ["s"]
    msg.scores = shared
    msg.labels = shared
    return msg


_STARTS = [
    _start_fresh,
    _start_prefilled,
    _start_rich,
    _start_alias_tags_scores,
    _start_alias_tags_labels,
    _start_alias_scores_labels,
]


def _truth_equivalent(full, sub):
    for cls in (GenV1, GenV2):
        for factory in _STARTS:
            msg_full = factory(cls)
            for _, data in full:
                msg_full.parse(data)
            msg_sub = factory(cls)
            for _, data in sub:
                msg_sub.parse(data)
            if _observable(msg_full) != _observable(msg_sub):
                return False
    return True


def _model_equivalent(full, sub):
    return all(logs_equivalent(cls, full, sub) for cls in (GenV1, GenV2))


def _random_fragment(rng):
    pieces = [
        vfield(1, rng.randint(0, 5)),
        vfield(2, rng.randint(0, 5)),
        packed(2, [rng.randint(0, 5) for _ in range(rng.randint(0, 2))]),
        sfield(3, rng.choice(["a", "b", ""])),
        vfield(4, rng.randint(0, 5)),
        sfield(5, rng.choice(["x", "y"])),
        vfield(6, rng.randint(0, 5)),
        sfield(7, rng.choice(["p", "q"])),
        lfield(8, map_entry(rng.choice(["k1", "k2"]), rng.randint(0, 5))),
        lfield(9, vfield(1, rng.randint(0, 5))),
        vfield(10, rng.randint(0, 5)),
        vfield(11, rng.randint(0, 5)),
        vfield(12, rng.randint(0, 5)),
        packed(12, [rng.randint(0, 5) for _ in range(rng.randint(0, 2))]),
        vfield(13, rng.randint(0, 3)),
        vfield(14, rng.randint(0, 1)),
        f64field(15, rng.choice([0.5, -1.25, float("nan"), 0.0])),
        lfield(16, rng.choice([b"\x00\x01", b"zz", b""])),
        f32field(17, rng.choice([1.5, float("nan")])),
        vfield(20, rng.randint(0, 5)),
    ]
    return b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 4)))


def test_fuzz_model_matches_betterproto():
    rng = random.Random(20261124)
    for _ in range(400):
        frags = [(i + 1, _random_fragment(rng)) for i in range(rng.randint(1, 6))]
        keep = [i for i in range(len(frags)) if rng.random() < 0.5]
        sub = [frags[i] for i in keep]
        assert _model_equivalent(frags, sub) == _truth_equivalent(frags, sub), (
            frags,
            sub,
        )


def test_fuzz_minimality_against_brute_force():
    rng = random.Random(777)
    for _ in range(60):
        frags = [(i + 1, _random_fragment(rng)) for i in range(rng.randint(1, 8))]
        min_bytes, keep = minimize_log(GenV1, GenV2, frags)
        kept = [frag for frag in frags if frag[0] in keep]
        assert [num for num, _ in kept] == sorted(num for num, _ in kept)
        assert sum(len(data) for _, data in kept) == min_bytes
        assert _model_equivalent(frags, kept)
        assert _truth_equivalent(frags, kept)
        for mask in range(1 << len(frags)):
            sub = [frags[i] for i in range(len(frags)) if mask >> i & 1]
            if sum(len(data) for _, data in sub) < min_bytes:
                assert not _model_equivalent(frags, sub), (frags, sub)
