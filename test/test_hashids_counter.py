import json

import pytest

import hashids_counter as counter
from hashids_counter import (
    Book,
    BookSpec,
    MinimumRuleSolver,
    Rule,
    classify,
    demo_manifest,
    load_manifest,
    reading,
    solve_manifest,
)


@pytest.fixture()
def manifest():
    return load_manifest(demo_manifest())


def test_demo_pair_round_trips_in_both_books(manifest):
    old, other = manifest.books
    assert old.encode(151) == "epQ6"
    assert old.decode("epQ6") == (151,)
    assert other.encode(55263) == "epQ6"
    assert other.decode("epQ6") == (55263,)
    assert old.reprint((151,)) == "epQ6"
    assert other.reprint((55263,)) == "epQ6"


def test_epq6_is_a_true_collision_of_two_real_originals(manifest):
    info = classify(manifest, "epQ6")
    assert info["collision"] is True
    assert info["books"] == ["old", "other"]
    readings = {item["book"]: item for item in info["readings"]}
    assert readings["old"]["numbers"] == [151]
    assert readings["other"]["numbers"] == [55263]
    assert readings["old"]["original"] and readings["other"]["original"]


def test_native_lookup_rejects_non_round_tripping_codes(manifest):
    old, other = manifest.books
    # 'x' is not in the old default alphabet at all.
    assert old.lookup("x") is None
    assert other.lookup("x") is None
    # Valid characters but not the exact reprint: hashids.decode refuses it.
    assert old.lookup("epQ7") is None


def test_collision_without_both_real_originals_is_not_a_collision():
    book = BookSpec(0, "old").build()
    other = BookSpec(
        1,
        "other",
        salt=counter.OTHER_SALT,
        alphabet="".join(
            char for char in counter.DEFAULT_ALPHABET if char not in counter.OTHER_DROP
        ),
    ).build()
    # epQ6 reads (151,) in old and (55263,) in other, but only old holds 151.
    book.register((151,))
    assert reading(book, "epQ6")["original"] is True
    assert reading(other, "epQ6")["original"] is False


def test_unreviewed_collision_does_not_force_a_rule(manifest):
    # epQ6 is the only reviewed code, and both sides are reviewed.
    result = solve_manifest(manifest)
    assert result.feasible is False
    assert result.rules == []
    assert len(result.indistinguishable) == 1
    entry = result.indistinguishable[0]
    assert entry["code"] == "epQ6"
    assert entry["blocking_prefix"] == "epQ6"
    assert {row["book"] for row in entry["readings"]} == {"old", "other"}
    assert {tuple(row["order"]) for row in entry["readings"]} == {(151,), (55263,)}


def test_reviewed_one_sided_collision_routes_to_reviewed_book(manifest):
    document = demo_manifest()
    # Shrink the old reviewed box so only the other book's 55263 is reviewed.
    document["books"][0]["reviewed_ranges"] = []
    document["books"][1]["reviewed_ranges"] = [{"min": [55263], "max": [55263]}]
    result = solve_manifest(load_manifest(document))
    assert result.feasible is True
    assert result.targets == {"epQ6": 1}
    assert [rule.as_dict() for rule in result.rules] == [{"prefix": "e", "book": 1}]


@pytest.mark.parametrize(
    "targets, allowed, expected, feasible",
    [
        ({"a12": 0, "a34": 1}, None, [("a3", 1)], True),
        ({"b12": 1, "b34": 1}, None, [("b", 1)], True),
        ({"c12": 1, "c34": 1, "d12": 0}, None, [("c", 1)], True),
        ({"a12": 0, "a13": 1}, ["a", "a1"], [], False),
        ({"a12": 0, "a13": 1}, ["a1"], [], False),
        ({"a12": 0, "a13": 1}, ["a"], [], False),
        ({"z12": 1, "z34": 1}, ["z"], [("z", 1)], True),
    ],
)
def test_minimum_rule_solver(targets, allowed, expected, feasible):
    solver = MinimumRuleSolver(targets, [0, 1], 0, allowed_prefixes=allowed)
    got_feasible, rules, _ = solver.solve()
    assert got_feasible is feasible
    assert [(rule.prefix, rule.book) for rule in rules] == expected


def test_solver_prefers_shorter_shared_prefix():
    # Both codes are the same target, so the shortest allowed prefix wins.
    solver = MinimumRuleSolver({"abcdef": 1, "abcxyz": 1}, [0, 1], 0)
    feasible, rules, count = solver.solve()
    assert feasible and count == 1
    assert [(rule.prefix, rule.book) for rule in rules] == [("a", 1)]


def test_solver_uses_shortest_separating_prefix():
    solver = MinimumRuleSolver({"abcdef": 1, "abcxyz": 0}, [0, 1], 0)
    feasible, rules, count = solver.solve()
    assert feasible and count == 1
    assert [(rule.prefix, rule.book) for rule in rules] == [("abcd", 1)]


def test_solver_ascii_tie_break():
    # Single character target: rules 'A' and 'B' are both one entry; ASCII
    # order picks the actual needed prefix anyway, so use two targets whose
    # prefixes differ only in case.
    solver = MinimumRuleSolver({"B1": 1, "b1": 1}, [0, 1], 0)
    feasible, rules, _ = solver.solve()
    assert feasible
    assert [rule.prefix for rule in rules] == ["B", "b"]


def test_solver_book_number_ascii_tie_break():
    solver = MinimumRuleSolver({"a1": 1}, [1, 10], 0, allowed_prefixes=["a"])
    feasible, rules, _ = solver.solve()
    assert feasible
    assert [rule.book for rule in rules] == [1]


def test_counter_table_uses_longest_rule(manifest):
    table = counter.CounterTable(
        manifest,
        [Rule("e", 0), Rule("ep", 1)],
    )
    assert table.choose("epQ6")["book"] == 1
    assert table.choose("ejRe")["book"] == 0


def test_counter_table_defaults_to_old_book(manifest):
    table = counter.CounterTable(manifest, [])
    decision = table.choose("epQ6")
    assert decision["book"] == 0
    assert "default" in decision["reason"]


def test_counter_table_directs_single_real_book_even_without_rule(manifest):
    table = counter.CounterTable(manifest, [])
    # 'j0gW' is 12345 in the old book; the other book cannot round-trip it.
    decision = table.choose("j0gW")
    assert decision["book"] == 0
    assert "only one" in decision["reason"]


def test_manifest_rejects_duplicate_orders(manifest):
    old = manifest.books[0]
    assert old.register((151,)) is False
    assert len([order for order in old.orders if order == (151,)]) == 1


def test_manifest_rejects_bad_order_arity():
    book = BookSpec(0, "old").build()
    with pytest.raises(counter.ManifestError):
        book.register(())
    with pytest.raises(counter.ManifestError):
        book.register((1, 2, 3, 4))


def test_manifest_rejects_non_ascii():
    document = demo_manifest()
    document["books"][0]["salt"] = "盐"
    with pytest.raises(counter.ManifestError):
        load_manifest(document)


def test_audit_small_matches_native(manifest):
    result = solve_manifest(manifest)
    audit = counter.audit_small(manifest, result.rules, 5000)
    assert audit["failures"] == []


def test_loaded_range_alone_makes_an_order_real():
    document = demo_manifest()
    document["books"][0]["orders"] = []
    document["books"][1]["orders"] = []
    result = solve_manifest(load_manifest(document))
    # Both single-value reviewed boxes are also loaded ranges in the demo.
    assert result.indistinguishable[0]["code"] == "epQ6"


def test_three_field_range_and_reprint():
    spec = counter.BookSpec(0, "old")
    book = spec.build()
    valid = (10, 20, 30)
    assert book.register(valid) is True
    code = book.reprint(valid)
    assert code and book.decode(code) == valid
    book.reviewed_ranges = counter._parse_boxes(
        [{"min": [0, 0, 0], "max": [10, 20, 30]}], 3
    )
    book.ranges = counter._parse_boxes(
        [{"min": [0, 0, 0], "max": [10, 20, 30]}], 3
    )
    assert book.has_original(valid)
    assert book.is_reviewed((10, 20, 30))
    assert not book.is_reviewed((10, 20, 31))


def test_large_value_is_handled_without_enumeration():
    book = counter.BookSpec(0, "old").build()
    code = book.encode(9_999_999_999)
    assert book.decode(code) == (9_999_999_999,)
    assert book.round_trips(code)


def test_allowed_prefix_limit_reports_blocking_prefix():
    solver = MinimumRuleSolver({"a12": 0, "a13": 1}, [0, 1], 0, allowed_prefixes=["a"])
    feasible, rules, _ = solver.solve()
    assert feasible is False and rules == []
    diagnostics = solver.diagnose()
    assert diagnostics and diagnostics[0]["blocking_prefix"] == "a"


def test_cli_demo_json(capsys):
    code = counter.main(["--demo", "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["feasible"] is False
    assert payload["indistinguishable"][0]["code"] == "epQ6"
