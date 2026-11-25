"""Differential tests: the span fast path must match a plain command walk."""

from decimal import Decimal

import pytest

from gcodeparser.reconcile import (
    MaterialLedger,
    PrintProgram,
    Repeat,
    Snippet,
    decimal_text,
)


def brute_usage(program, initial_spools, mode="relative"):
    """Reference: expand every command and replay the stated rules."""

    ledger = MaterialLedger(
        program, initial_spools=initial_spools, mode=mode
    )
    return ledger.usage()


@pytest.mark.parametrize("count", [0, 1, 2, 7, 50])
def test_neutral_repeat_span_matches_full_walk(count):
    snippets = {
        "unit": Snippet(["G1 E0.125"], name="unit"),
        "job": Snippet(["M83", Repeat("unit", count)], name="job"),
    }
    program = PrintProgram(Repeat("job", 3), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    expected = {"A": Decimal("0.125") * count * 3} if count else {}
    assert led.usage() == expected
    if count:
        assert led.first_new_usage().ordinal == 1
    else:
        assert led.first_new_usage() is None
    assert led.command_count == (1 + count) * 3


def test_span_respects_target_window_and_resumes():
    snippets = {"unit": Snippet(["G1 E1"], name="unit")}
    program = PrintProgram(Repeat("unit", 1000), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    assert led.usage(0, 0) == {"A": Decimal("1")}
    assert led.usage(500, 500) == {"A": Decimal("1")}
    assert led.usage(0, 999) == {"A": Decimal("1000")}
    assert led.usage(999, 999) == {"A": Decimal("1")}
    assert led.state()["e"] == {0: "1000"}


def test_span_stops_at_retraction_inside_block():
    snippets = {
        "unit": Snippet(["G1 E1", "G1 E-0.25"], name="unit"),
    }
    program = PrintProgram(Repeat("unit", 5), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    # Each unit makes 1 and retracts 0.25; every later unit repays the 0.25 first.
    assert led.usage() == {"A": Decimal("4.00")}
    assert led.outstanding() == {"A": Decimal("0.25")}
    assert led.first_new_usage().ordinal == 0
    assert led.command_count == 10


def test_span_disabled_for_absolute_mode():
    snippets = {
        "unit": Snippet(["G1 E5"], name="unit"),
    }
    program = PrintProgram(Repeat("unit", 100), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="absolute")
    # Absolute E5 repeated: first is new material, all later ones are zero.
    assert led.usage() == {"A": Decimal("5")}
    assert led.command_count == 100


def test_span_disabled_when_timed_handover_inside_block():
    snippets = {"unit": Snippet(["G1 E1"], name="unit")}
    program = PrintProgram(Repeat("unit", 10), snippets=snippets)
    led = MaterialLedger(
        program,
        initial_spools={0: "A"},
        mode="relative",
        timed_handovers={6: [(0, "B")]},
    )
    assert led.usage() == {"A": Decimal("6"), "B": Decimal("4")}


def test_span_credential_matches_structural_lookup():
    snippets = {
        "unit": Snippet(["G1 E0.5"], name="unit"),
        "pass": Snippet(["M83\nG1 E-1.0", Repeat("unit", 4)], name="pass"),
    }
    program = PrintProgram(Repeat("pass", 3), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    first = led.first_new_usage()
    # Pass 0: retract 1, then 4 * 0.5 = 2 new.  The first two units repay the
    # debt exactly; the third unit's 0.5 is the first new material.
    assert first.ordinal == 4
    assert led.credential(first.ordinal) == first
    assert first.source.snippet == "unit"
    assert first.iteration[-2:] == (("pass", 0), ("unit", 2))


def test_span_last_ordinal_and_window_end_are_inclusive():
    snippets = {"unit": Snippet(["G1 E1"], name="unit")}
    program = PrintProgram(Repeat("unit", 10), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    assert led.usage(9, 9) == {"A": Decimal("1")}
    assert led.usage(9, 9, inclusive_end=False) == {}
    with pytest.raises(IndexError):
        led.usage(10, 10)


def _reference_usage(program, initial_spools, mode="relative", start=0, end=None):
    """Plain command-by-command reference, independent of spans."""

    total = program.command_count()
    if end is None:
        end = total - 1
    ledger = MaterialLedger(
        program,
        initial_spools=initial_spools,
        mode=mode,
    )
    # `usage` uses the fast path; walk ordinals explicitly for the reference.
    usage = {}
    for ordinal in range(start, end + 1):
        result = ledger.usage(ordinal, ordinal)
        for spool, amount in result.items():
            usage[spool] = usage.get(spool, Decimal(0)) + amount
    return usage


@pytest.mark.parametrize(
    "spec",
    [
        {"unit": ["G1 E0.25"], "outer": 6, "inner": 3},
        {"unit": ["G1 E0.1", "G1 E0.2"], "outer": 4, "inner": 5},
        {"unit": ["M83", "G1 E0.001"], "outer": 10, "inner": 7},
    ],
)
def test_nested_span_equals_sum_of_single_command_windows(spec):
    unit = Snippet(spec["unit"], name="unit")
    mid = Snippet([Repeat("unit", spec["inner"])], name="mid")
    program = PrintProgram(Repeat("mid", spec["outer"]), snippets={
        "unit": unit,
        "mid": mid,
    })
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    whole = led.usage()
    by_single = {}
    for ordinal in range(led.command_count):
        for spool, amount in led.usage(ordinal, ordinal).items():
            by_single[spool] = by_single.get(spool, Decimal(0)) + amount
    assert whole == by_single


def test_span_partial_iteration_overlap():
    unit = Snippet(["G1 E0.5", "G1 E0.25"], name="unit")
    program = PrintProgram(Repeat("unit", 100), snippets={"unit": unit})
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    # Two commands per iteration: offsets 0 and 1.
    assert led.usage(0, 0) == {"A": Decimal("0.5")}
    assert led.usage(1, 1) == {"A": Decimal("0.25")}
    assert led.usage(0, 1) == {"A": Decimal("0.75")}
    assert led.usage(1, 2) == {"A": Decimal("0.75")}
    assert led.usage(0, 199) == {"A": Decimal("75.00")}
    assert led.first_new_usage(1, 1).ordinal == 1
    assert led.first_new_usage(2, 2).ordinal == 2


def test_span_with_partial_last_iteration_window():
    unit = Snippet(["G1 E1", "G1 E2", "G1 E3"], name="unit")
    program = PrintProgram(Repeat("unit", 50), snippets={"unit": unit})
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    # Ordinals 4..7: iteration 1 offsets 1,2 (2+3) and iteration 2 offsets 0,1 (1+2).
    assert led.usage(4, 7) == {"A": Decimal("8")}
    assert led.usage(4, 7, inclusive_end=False) == {"A": Decimal("6")}
    assert led.first_new_usage(4, 7).ordinal == 4




def test_self_contained_retraction_cycles_compress_and_match_reference():
    unit = Snippet(["M83", "G1 E-1.0", "G1 E1.0"], name="unit")
    small = MaterialLedger(
        PrintProgram(Repeat("unit", 6), snippets={"unit": unit}),
        initial_spools={0: "A"},
        mode="relative",
    )
    large = MaterialLedger(
        PrintProgram(Repeat("unit", 50_000_000), snippets={"unit": unit}),
        initial_spools={0: "A"},
        mode="relative",
    )
    assert large.command_count == 150_000_000
    assert small.usage() == large.usage() == {}
    assert large._spans  # the run really was compressed rather than walked

    for index in range(6):
        assert small.repayment(f"A:{index}") == large.repayment(f"A:{index}")

    credit, credential = large.repayment("A:49_999_999")
    assert decimal_text(credit) == "1.0"
    assert credential.iteration == (("unit", 49_999_999),)
    assert credential.source.snippet == "unit"
    assert credential.source.line_index == 2


def test_self_contained_retraction_window_amount_and_first_credential():
    unit = Snippet(["M83", "G1 E-2.0", "G1 E2.0"], name="unit")
    ledger = MaterialLedger(
        PrintProgram(Repeat("unit", 10_000_000), snippets={"unit": unit}),
        initial_spools={0: "A"},
        mode="relative",
    )
    # Retraction 0 opens in the first unit and is repaid in that same unit.
    report = ledger.retraction_report("A:0", 0, 2)
    assert report == {
        "retraction": {"spool": "A", "index": 0},
        "amount": "2.0",
        "first_repayment": {
            "ordinal": 2,
            "source": {"snippet": "unit", "line_index": 2, "command_index": 0},
            "iteration": [["unit", 0]],
            "text": "G1 E2.0",
        },
    }

    # A window that only covers the opening command sees no repayment at all.
    assert ledger.retraction_report("A:0", 1, 1)["first_repayment"] is None

    # A retraction in a compressed middle run is resolved to its own cycle.
    middle = ledger.retraction_report("A:4_000_000", 12_000_001, 12_000_002)
    assert middle["retraction"] == {"spool": "A", "index": 4_000_000}
    assert middle["amount"] == "2.0"
    assert middle["first_repayment"]["iteration"] == [["unit", 4_000_000]]
