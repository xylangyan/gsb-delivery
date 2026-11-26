"""Self-test for the rebuild engine using a fully synthetic scenario.

The real task fixtures live outside the readable sandbox, so this builds a
representative scenario that exercises every documented quirk and cross-checks
the engine against direct, hand-written Faker calls.
"""

from __future__ import annotations

from pathlib import Path

from faker import Faker

from rebuild.engine import (
    Cell,
    CellRef,
    ProbRule,
    QuerySession,
    Row,
    Scenario,
    Simulator,
    TailStep,
)

SEED = 20261127
LOCALES = {"en_US": 5, "fr_FR": 3, "ja_JP": 2}
GATE_TABLE = {0: 0.29, 1: 0.07, 2: 0.81}


def build_scenario() -> Scenario:
    cell_a = Cell("A", lo=1, hi=4, step=1)
    cell_b = Cell("B", lo=0, hi=6, step=2)
    rows = [
        Row("lead_int", "random_int", args=(0, 3)),
        Row(
            "gated_optional",
            "random_int",
            args=(0, 99),
            optional=True,
            prob_rule=ProbRule("gated", table=GATE_TABLE, default=0.5),
        ),
        # method fetched before it is called; B feeds the argument, prob truncates
        Row(
            "early_optional",
            "random_int",
            args=(0, CellRef("B")),
            optional=True,
            prob_rule=ProbRule("fixed", prob=0.29),
        ),
        Row("global_unique", "random_int", kwargs={"min": 1, "max": 6}, unique=True),
        Row("locale_unique", "random_int", kwargs={"min": 1, "max": 6}, unique=True, locale="fr_FR"),
        Row("global_unique_2", "random_int", kwargs={"min": 1, "max": 6}, unique=True),
        Row("arg_cell_a", "random_int", args=(0, CellRef("A"))),
    ]
    # fetch early_optional's callable before row index 1, call it at index 2
    prefetch = {1: ["early_optional"]}
    tail = [
        TailStep("random_int", kwargs={"min": 1, "max": 6}, unique=True),
        TailStep("random_int", kwargs={"min": 0, "max": 999}),
    ]
    registry = {(a, b) for a in cell_a.values() for b in cell_b.values() if (a + b) % 2 == 0}
    return Scenario(SEED, LOCALES, [cell_a, cell_b], rows, tail, registry, prefetch)


def reference_run(ctx: dict[str, int]) -> tuple[list, list]:
    """Hand-written reference using the real Faker API in exact row order."""
    fake = Faker(LOCALES)
    Faker.seed(SEED)
    out: list = []

    out.append(fake.random_int(0, 3))

    # fetch the optional callable now, then fill another item before calling it
    bound = fake.optional.random_int

    prev = out[-1]
    lit = GATE_TABLE[prev] if isinstance(prev, int) and prev in GATE_TABLE else 0.5
    out.append(fake.optional.random_int(0, 99, prob=lit))

    out.append(bound(0, ctx["B"], prob=0.29))

    out.append(fake.unique.random_int(min=1, max=6))
    out.append(fake.unique["fr_FR"].random_int(min=1, max=6))
    out.append(fake.unique.random_int(min=1, max=6))
    out.append(fake.random_int(0, ctx["A"]))

    tail = [
        fake.unique.random_int(min=1, max=6),
        fake.random_int(min=0, max=999),
    ]
    return out, tail


def main() -> None:
    scn = build_scenario()
    sim = Simulator(scn)
    ctx = {"A": 3, "B": 4}

    audit, history, tail = sim.run(ctx)
    ref_history, ref_tail = reference_run(ctx)
    assert history == ref_history, (history, ref_history)
    assert tail == ref_tail, (tail, ref_tail)
    print("PASS engine matches hand-written reference generation")

    trunc = next(e for e in audit if e.row == "early_optional")
    assert trunc.effective_percent == int(0.29 * 100) == 28
    gated = next(e for e in audit if e.row == "gated_optional")
    assert gated.formula in {"default", "table[0]", "table[1]", "table[2]"}
    print(
        f"PASS audit formula/percent: gated={gated.formula}->{gated.effective_percent}, "
        f"early={trunc.formula}->{trunc.effective_percent}"
    )

    unique_rows = [e for e in audit if e.unique_attempts]
    assert unique_rows, "expected observable unique retries"
    unique_values = [e.result for e in audit if e.row.startswith(("global_unique", "locale_unique"))]
    assert len(set(unique_values)) == len(unique_values)
    print(f"PASS shared unique pool, retries: {[(e.row, e.unique_attempts) for e in unique_rows]}")

    assert sim.run(ctx)[1:] == (history, tail)
    print("PASS deterministic re-run")

    session = QuerySession(scn)
    full = session.query()
    assert full["count"] >= 1 and full["earliest"] is not None
    pairs = sorted((p["a"], p["b"]) for p in full["pairs"])
    assert (full["earliest"]["a"], full["earliest"]["b"]) == pairs[0]
    print(f"PASS full query: {full['count']} matching pairs, earliest={pairs[0]}")

    narrow = session.query(range_a=(2, 4), range_b=(2, 6))
    assert all(2 <= p["a"] <= 4 and 2 <= p["b"] <= 6 for p in narrow["pairs"])
    assert narrow["count"] <= full["count"]
    cached_before = len(session._evaluated)
    session.query(range_a=(2, 4), range_b=(2, 6))
    assert len(session._evaluated) == cached_before
    print(f"PASS narrowed query: {narrow['count']} pairs, evaluation cache reused")

    state = session.prime_prefix(ctx, through_row=3)
    assert session.prime_prefix(ctx, 3) is state
    print("PASS shared prefix materialized once and memoized")

    # continuing from the prefix snapshot must equal the full straight run
    prefix_audit, prefix_history, _ = sim.run(ctx)
    cut = 3
    snapshot = session.prime_prefix(ctx, cut)
    prior = prefix_history[: cut + 1]
    cont_audit, cont_history, cont_tail = sim.run_remaining(snapshot, cut + 1, ctx, prior)
    assert cont_history == history, (cont_history, history)
    assert cont_tail == tail, (cont_tail, tail)
    assert [e.row for e in cont_audit] == [r.name for r in scn.rows[cut + 1:]]
    print("PASS prefix restore + continuation reproduces the full generation")

    # a cut that splits an open prefetched binding is rejected
    try:
        session.prime_prefix(ctx, through_row=1)
    except ValueError:
        print("PASS prefix cut across an open prefetch binding is rejected")
    else:
        raise AssertionError("expected ValueError for split prefetch boundary")

    path = Path("rebuild/_selftest_session.pkl")
    session.save(path)
    reloaded = QuerySession.load(path)
    assert reloaded.query(range_a=(2, 4), range_b=(2, 6))["pairs"] == narrow["pairs"]
    path.unlink()
    print("PASS save/load round trip preserves query results")

    assert len(session._evaluated) <= len(scn.cells[0].values()) * len(scn.cells[1].values())
    print("PASS lazy evaluation (no precomputed pair/branch tables)")

    print("\nAudit of earliest match:")
    for e in full["earliest"]["audit"]:
        print(
            f"  {e.row:15s} formula={e.formula:10s} literal={e.prob_literal} "
            f"percent={e.effective_percent} attempts={e.unique_attempts} result={e.result!r}"
        )
    print("tail:", full["earliest"]["tail"])


if __name__ == "__main__":
    main()
