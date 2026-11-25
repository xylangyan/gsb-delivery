"""Acceptance tests for the migration planner.

Covers, for every step of a computed sequence, the in-flight and
freshly written mixes （尚在途 / 新的混批）: keys, values and business
types must be right at every actual reader — checked symbolically by
the planner and confirmed here against real umsgpack frames.
"""

import datetime
import json
import os
import struct

import pytest

import umsgpack

from migration import spec as sp
from migration.checks import find_key_collision, validate_inflight
from migration.planner import plan
from migration.solver import Solver
from migration import wire

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "examples", "handover_demo.json")


def load_demo(**overrides):
    with open(DEMO, "r", encoding="utf-8") as fh:
        sheet = json.load(fh)
    for path, value in overrides.items():
        target = sheet
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return sp.load(sheet)


# ---------------------------------------------------------------------------
# solver: exact over huge ranges, never enumerates
# ---------------------------------------------------------------------------

def test_solver_linear_equality_huge_range():
    s = Solver()
    s.add_var("x", 0, 2**63 - 1)
    s.add_var("y", 0, 2**64 - 1)
    s.add_lineq({"x": 2, "y": -1}, 0)          # 2x == y
    s.add_lineq({"x": 1}, 2**62)               # x == 2**62
    got = s.solve()
    assert got is not None
    assert got["x"] == 2**62
    assert got["y"] == 2 * got["x"]


def test_solver_parity_infeasible_without_enumeration():
    s = Solver()
    s.add_var("x", 0, 2**63 - 1)
    s.add_var("y", 0, 2**63 - 1)
    s.add_lineq({"x": 2, "y": -2}, 1)          # 2x - 2y == 1: impossible
    assert s.solve() is None


def test_solver_negative_coefficient_bounds():
    s = Solver()
    s.add_var("x", 0, 100)
    s.add_var("y", 0, 100)
    s.add_lineq({"x": 1, "y": 2}, 50)          # x + 2y == 50
    got = s.solve()
    assert got is not None
    assert got["x"] + 2 * got["y"] == 50


def test_solver_lookup_and_sign_extend():
    s = Solver()
    s.add_var("x", 0, 255)
    s.add_var("y", -128, 127)
    s.add_var("z", 0, 10)
    s.add_sign_extend("x", "y", 8)
    s.add_lookup("y", "z", [(0, 7), (5, 9), (-3, 4)])
    got = s.solve()
    assert got is not None
    assert got["y"] in (0, 5, -3)
    if got["y"] >= 0:
        assert got["x"] == got["y"]
    else:
        assert got["x"] == got["y"] + 256


# ---------------------------------------------------------------------------
# umsgpack behaviour the model relies on
# ---------------------------------------------------------------------------

def test_umsgpack_array_keys_become_tuples():
    blob = umsgpack.packb({("a", (1, 2)): "v"})
    assert umsgpack.unpackb(blob) == {("a", (1, 2)): "v"}


def test_umsgpack_bool_and_int_keys_collapse():
    # inside one frame umsgpack guards duplicates (True == 1 as dict
    # keys) and refuses the map
    blob = (b"\x82"                            # map of 2 pairs
            + b"\xc3" + umsgpack.packb("bool")  # true -> "bool"
            + b"\x01" + umsgpack.packb("int"))  # 1    -> "int"
    with pytest.raises(umsgpack.DuplicateKeyException):
        umsgpack.unpackb(blob)
    # across separately decoded frames the guard cannot fire: merging
    # the 合单 silently keeps one entry
    frame_a = umsgpack.packb({True: "bool"})
    frame_b = umsgpack.packb({1: "int"})
    merged = {}
    merged.update(umsgpack.unpackb(frame_a))
    merged.update(umsgpack.unpackb(frame_b))
    assert len(merged) == 1                            # 只剩一项


def test_umsgpack_ext_handlers_override_registered():
    @umsgpack.ext_serializable(99)
    class Registered(object):
        @classmethod
        def unpackb(cls, data):
            return "registered"

    ext = umsgpack.Ext(99, b"")
    blob = umsgpack.packb({"k": ext})
    assert umsgpack.unpackb(blob)["k"] == "registered"
    out = umsgpack.unpackb(blob, ext_handlers={99: lambda e: "handler"})
    assert out["k"] == "handler"


def test_umsgpack_timestamp_type_and_precision():
    aware = datetime.datetime(2026, 11, 26, 1, 2, 3, 456789,
                              tzinfo=datetime.timezone.utc)
    blob = umsgpack.packb({"ts": aware})
    out = umsgpack.unpackb(blob)["ts"]
    assert isinstance(out, datetime.datetime)
    assert out == aware
    # 96-bit timestamp with nanosecond resolution loses sub-microsecond
    # precision, as documented in docs/behavior-notes.md
    nsec = 234_567_890
    sec = 1_800_000_000
    data = struct.pack(">Iq", nsec, sec)
    blob = umsgpack.packb({"ts": umsgpack.Ext(-1, data)})
    out = umsgpack.unpackb(blob)["ts"]
    assert isinstance(out, datetime.datetime)
    assert out.microsecond == nsec // 1000


# ---------------------------------------------------------------------------
# symbolic collision checks
# ---------------------------------------------------------------------------

def test_bool_int_collision_old_vs_final_alpha():
    ho = load_demo()
    w = find_key_collision(ho, "alpha", "old", "alpha", "final")
    assert w is not None
    assert w.key_left == w.key_right
    assert hash(w.key_left) == hash(w.key_right)
    # the old side carries a real bool, the new side a real int
    assert isinstance(w.key_left[1], bool)
    assert isinstance(w.key_right[1], int) and not isinstance(w.key_right[1], bool)
    # the original flag values agree (True == 1 as Python keys) and the
    # shared id lands both nested keys on the same tuple
    assert w.fields_left["flag"] == w.fields_right["flag"]
    assert w.fields_left["id"] == w.fields_right["id"]


def test_sign_extend_collision_shared_count():
    ho = load_demo()
    w = find_key_collision(ho, "beta", "old", "beta", "final")
    assert w is not None
    # both sides decode correctly, yet a commonly permitted count lets
    # the keys land on the same tuple
    assert 0 <= w.fields_left["count"] <= 127
    assert w.fields_left["count"] == w.fields_right["count"]
    assert w.key_left == w.key_right


def test_no_collision_when_permitted_counts_above_sign_boundary():
    ho = load_demo(**{"segments.beta.template.permitted":
                      [{"fields": ["count"], "allow": [[128], [200], [255]]}]})
    assert find_key_collision(ho, "beta", "old", "beta", "final") is None


def test_temp_tables_avoid_their_neighbours():
    ho = load_demo()
    assert find_key_collision(ho, "alpha", "old", "alpha", "tmp42") is None
    assert find_key_collision(ho, "alpha", "tmp42", "alpha", "final") is None
    assert find_key_collision(ho, "beta", "old", "beta", "tmp43") is None
    assert find_key_collision(ho, "beta", "tmp43", "beta", "final") is None


def test_nested_array_keys_and_timestamp():
    sheet = {
        "segments": {
            "g1": {
                "writer": "w1", "sinks": ["s1"],
                "template": {"fields": {
                    "n": {"kind": "int", "lo": 0, "hi": 2**40},
                    "ts": {"kind": "timestamp"}}},
                "tables": {
                    "old": {"ext_code": 1,
                            "key": [[{"field": "n"}, {"field": "ts"}]],
                            "value": {"field": "n", "type": "int"}},
                    "final": {"ext_code": 2,
                              "key": [[{"field": "n"}, {"field": "ts"}]],
                              "value": {"field": "n", "type": "int"}},
                },
            },
        },
        "readers": [{"name": "r", "registered": [1, 2]}],
    }
    ho = sp.load(sheet)
    w = find_key_collision(ho, "g1", "old", "g1", "final")
    assert w is not None
    assert isinstance(w.key_left[0][1], datetime.datetime)
    assert w.key_left == w.key_right
    # timestamp vs int at the same key position can never collide
    sheet["segments"]["g1"]["tables"]["final"]["key"] = \
        [[{"field": "n"}, {"field": "n"}]]
    ho = sp.load(sheet)
    assert find_key_collision(ho, "g1", "old", "g1", "final") is None


def test_shared_original_values_across_segments():
    sheet = {
        "shared": [["s1.site", "s2.site"]],
        "segments": {
            "s1": {
                "writer": "w1", "sinks": ["k1"],
                "template": {"fields": {
                    "level": {"kind": "int", "lo": 0, "hi": 3},
                    "site": {"kind": "int", "lo": 10**9, "hi": 10**9 + 50}}},
                "tables": {
                    "old": {"ext_code": 1,
                            "key": [{"field": "level"}, {"field": "site"}],
                            "value": {"field": "level", "type": "int"}},
                    "final": {"ext_code": 2,
                              "key": [{"const": "v2"}, {"field": "level"}],
                              "value": {"field": "level", "type": "int"}},
                },
            },
            "s2": {
                "writer": "w2", "sinks": ["k2"],
                "template": {"fields": {
                    "count": {"kind": "int", "lo": 0, "hi": 255},
                    "site": {"kind": "int", "lo": 0, "hi": 2**31}}},
                "tables": {
                    "old": {"ext_code": 3,
                            "key": [{"field": "count"}, {"field": "site"}],
                            "value": {"field": "count", "type": "int"}},
                    "final": {"ext_code": 4,
                              "key": [{"const": "v2"}, {"field": "count"},
                                      {"const": "degC"}],
                              "value": {"field": "count", "type": "int"}},
                },
            },
        },
        "readers": [{"name": "r", "registered": [1, 2, 3, 4]}],
    }
    ho = sp.load(sheet)
    # s1.old [level, site] vs s2.old [count, site]: ranges overlap and the
    # shared site forces the second element to be equal -> collision
    w = find_key_collision(ho, "s1", "old", "s2", "old")
    assert w is not None
    assert w.fields_left["site"] == w.fields_right["site"] >= 10**9
    # different key shapes never collide
    assert find_key_collision(ho, "s1", "final", "s2", "final") is None


def test_validate_inflight_reports_decode_and_collision():
    ho = load_demo()
    problems = validate_inflight(ho, [("alpha", "old"), ("alpha", "final")])
    kinds = {p.kind for p in problems}
    assert "collision" in kinds
    # reader r1 has no active/permitted problem here (8 is toggleable),
    # but a reader missing the code entirely is a decode problem
    ho2 = load_demo(**{"readers": [{"name": "r1", "registered": [5]}]})
    problems = validate_inflight(ho2, [("alpha", "old"), ("alpha", "final")])
    decode = [p for p in problems if p.kind == "decode"]
    assert decode and decode[0].code == 8 and decode[0].reader == "r1"


# ---------------------------------------------------------------------------
# wire-level confirmation of a collision witness (真实报文)
# ---------------------------------------------------------------------------

def test_witness_generates_a_real_collapsing_message():
    ho = load_demo()
    w = find_key_collision(ho, "alpha", "old", "alpha", "final")
    ts = datetime.datetime(2026, 11, 26, tzinfo=datetime.timezone.utc)
    left = dict(w.fields_left, ts=ts)
    right = dict(w.fields_right, ts=ts)
    merged = wire.simulate_merged_map(
        ho,
        [("alpha", "old", left), ("alpha", "final", right)],
        active_codes={5, 8})
    # both frames decode successfully, yet the merged map keeps one entry
    assert len(merged) == 1
    (key, value), = merged.items()
    assert key == w.key_left
    assert isinstance(value, datetime.datetime) and value == ts


def test_wire_values_follow_conversion_rules():
    ho = load_demo()
    merged = wire.simulate_merged_map(
        ho, [("beta", "final", {"count": 200, "site": 9})],
        active_codes={7})
    (key, value), = merged.items()
    assert key == ("beta", 200 - 256, 9)       # sign extension
    assert value == (200 - 256) * 10           # + integer scaling
    assert isinstance(value, int)


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------

def test_plan_demo_is_optimal():
    ho = load_demo()
    result = plan(ho)
    assert result.ok, result.describe()
    assert list(result.borrowed) == [42, 43]   # 少借扩展号
    assert len(result.steps) == 5              # 同数时少实际切换
    switches = [s for s in result.steps if s.segment]
    assert [(s.segment, s.from_table, s.to_table) for s in switches] == [
        ("alpha", "old", "tmp42"),
        ("beta", "old", "tmp43"),
        ("alpha", "tmp42", "final"),
        ("beta", "tmp43", "final"),
    ]
    # every borrowed code is returned by the end (照现场回执)
    returned = sorted(c for s in result.steps for c in s.returned)
    assert returned == [42, 43]


def test_plan_without_borrowables_is_infeasible_with_witness():
    ho = load_demo(**{"segments.alpha.borrowable": {},
                      "segments.beta.borrowable": {}})
    result = plan(ho)
    assert not result.ok
    text = result.describe()
    assert "ext 5" in text and "ext 8" in text  # collision witness
    assert "left fields" in text                # 原字段选择


def test_plan_blocked_by_reader_permission():
    sheet = json.load(open(DEMO))
    sheet["readers"][0]["ext_handlers"]["may_toggle"] = [5, 6, 7, 8, 43]
    ho = sp.load(sheet)
    result = plan(ho)
    assert not result.ok
    text = result.describe()
    assert "r1" in text and "42" in text        # 最早卡住的一组许可


def test_plan_with_safe_sign_boundary_borrows_less():
    ho = load_demo(**{"segments.beta.template.permitted":
                      [{"fields": ["count"], "allow": [[128], [200], [255]]}]})
    result = plan(ho)
    assert result.ok, result.describe()
    assert list(result.borrowed) == [42]
    assert len(result.steps) == 4


def test_plan_execution_acceptance_over_mixed_batches():
    """Walk the computed plan; at every step decode the in-flight mix at
    every reader and check keys, values and business types."""
    ho = load_demo()
    result = plan(ho)
    assert result.ok

    ts = datetime.datetime(2026, 11, 26, 8, 30, tzinfo=datetime.timezone.utc)
    samples = {
        "alpha": {"flag": True, "id": 4_000_000_001, "ts": ts},
        "beta": {"count": 200, "site": 2**31 - 1},
    }

    def values_for(seg):
        return dict(samples[seg])

    # replay the state machine, tracking in-flight versions per segment
    currents = {s: "old" for s in ho.segments}
    pending = {s: [] for s in ho.segments}
    active = {r.name: set(r.eh_initial) for r in ho.readers}

    def check_state():
        inflight = [(s, currents[s]) for s in ho.segments]
        inflight += [(s, t) for s in ho.segments for t in pending[s]]
        assert validate_inflight(ho, inflight) == []
        entries = [(s, t, values_for(s)) for s, t in inflight]
        for reader in ho.readers:
            codes = {ho.table(s, t).ext_code for s, t in inflight}
            if reader.registered:
                assert codes <= set(reader.registered) | active[reader.name]
            merged = wire.simulate_merged_map(
                ho, entries, active_codes=codes)
            # 验收: 键、值和原业务类型在各实际读端都必须对
            assert len(merged) == len(entries)
            for (seg, table_id, _), (key, value) in zip(
                    entries, merged.items()):
                table = ho.table(seg, table_id)
                if table.value.type == "timestamp":
                    assert isinstance(value, datetime.datetime)
                else:
                    assert type(value).__name__ == table.value.type

    check_state()
    for step in result.steps:
        for reader, code in step.enables:
            active[reader].add(code)
        for reader, code in step.disables:
            active[reader].discard(code)
        if step.segment:
            pending[step.segment].append(currents[step.segment])
            currents[step.segment] = step.to_table
        check_state()
        for seg, table_id in step.cleared:
            assert table_id in pending[seg]
            pending[seg].remove(table_id)
    assert all(currents[s] == "final" for s in ho.segments)
    assert not any(pending.values())
