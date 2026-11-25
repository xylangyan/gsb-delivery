from decimal import Decimal

import pytest

from gcodeparser import parse_gcode_lines
from gcodeparser.reconcile import (
    CommandId,
    Handover,
    MaterialLedger,
    PrintProgram,
    Repeat,
    RetractionId,
    Snippet,
    decimal_text,
)


def ledger(body, **kwargs):
    program = PrintProgram(Snippet(body, name="root"))
    kwargs.setdefault("initial_spools", {0: "A"})
    kwargs.setdefault("mode", "relative")
    return MaterialLedger(program, **kwargs)


def text_usage(ledger_, *args, **kwargs):
    return {
        spool: decimal_text(amount)
        for spool, amount in ledger_.usage(*args, **kwargs).items()
    }


def test_negative_then_positive_is_new_material_after_repayment():
    led = ledger("M83\nG1 E-4.0\nG1 E3.0\nG1 E2.0\n")
    assert led.usage() == {"A": Decimal("1.0")}
    assert decimal_text(led.repaid("A:0")) == "4.0"
    first = led.first_repayment("A:0")
    assert first.ordinal == 2
    assert led.first_new_usage().ordinal == 3


def test_overlapping_retractions_repaid_first_opened_first_repaid():
    led = ledger("M83\nG1 E-2.0\nG1 E-3.0\nG1 E4.0\nG1 E3.0\n")
    assert led.usage() == {"A": Decimal("2.0")}
    assert decimal_text(led.repaid("A:0")) == "2.0"
    assert decimal_text(led.repaid("A:1")) == "3.0"
    assert led.first_repayment("A:0").ordinal == 3
    assert led.first_repayment("A:1").ordinal == 3
    partial = led.repayment("A:1", 3, 3)
    assert decimal_text(partial[0]) == "2.0"
    assert partial[1].ordinal == 3
    assert decimal_text(led.repaid("A:1", 4, 4)) == "1.0"


def test_prompt_worked_example_handover_to_new_spool_and_back():
    body = [
        "M83\nG1 E-4.0\nG92 E0\nT1\nG1 E3.0\nT0",
        Handover(0, "B"),
        "G1 E2.0",
        Handover(0, "A"),
        "G1 E2.0",
    ]
    led = MaterialLedger(
        Snippet(body, name="root"),
        initial_spools={0: "A", 1: "A"},
        mode="relative",
    )
    assert led.usage() == {"A": Decimal("1.0"), "B": Decimal("2.0")}
    # T1 repaid 3 of the debt left on A; the switch to B makes all 2 new.
    assert decimal_text(led.repaid("A:0")) == "4.0"
    events = led.usage_events()
    assert [event.spool for event in events] == ["B", "A"]
    assert [decimal_text(event.amount) for event in events] == ["2.0", "1.0"]


def test_debt_stays_on_old_spool_and_tool_coordinate_continues():
    body = [
        "M83\nG1 E-5.0",
        Handover(0, "B"),
        "G1 E3.0",
        Handover(0, "A"),
        "G1 E1.0",
    ]
    led = ledger(body)
    assert led.outstanding() == {"A": Decimal("4.0")}
    assert text_usage(led) == {"B": "3.0"}


def test_window_before_retraction_counts_prior_debt():
    led = ledger("M83\nG1 E-4.0\nG1 E1.0\nG1 E5.0\n")
    # Positions: 0 M83, 1 retract, 2 repay 1, 3 repay 3 and start new material.
    assert text_usage(led, 2, 3) == {"A": "2.0"}
    assert decimal_text(led.repaid("A:0", 2, 3)) == "4.0"
    # A window that starts after the retraction still knows the debt opened before it.
    assert decimal_text(led.repaid("A:0", 3, 3)) == "3.0"
    assert text_usage(led, 3, 3) == {"A": "2.0"}


def test_window_excludes_commands_outside_it():
    led = ledger("M83\nG1 E1.0\nG1 E2.0\nG1 E3.0\n")
    assert text_usage(led, 2, 2) == {"A": "2.0"}
    assert text_usage(led, 2, 2, inclusive_end=False) == {}


def test_absolute_mode_and_g92_reset_only_current_tool():
    gcode = """M82
G92 E0
G1 E1.5
T1
G1 E-0.5
T0
G1 E2.0
G92 E0
G1 E0.5
"""
    led = ledger(gcode, initial_spools={0: "A", 1: "A"}, mode="absolute")
    assert led.usage() == {"A": Decimal("2.0")}
    # Tool 0: 1.5 then absolute 2.0, then G92 resets to 0 and 0.5 is new.
    # Tool 1: -0.5 opened a retraction; tool 0's subsequent 0.5 cannot repay it?
    # No: debt belongs to spool A, and tool 0 feeds A, so it repays tool 1's debt.
    assert led.state()["e"] == {0: "0.5", 1: "-0.5"}


def test_per_tool_coordinates_survive_tool_changes():
    gcode = "M83\nT1\nG1 E1.0\nT0\nG1 E2.0\nT1\nG1 E1.0\n"
    led = ledger(gcode, initial_spools={0: "A", 1: "A"})
    # T1 extrudes 1, T0 extrudes 2, then T1 continues from its own 1.0 and adds 1.
    assert led.usage() == {"A": Decimal("4.0")}
    assert led.state()["e"] == {0: "2.0", 1: "2.0"}
    assert [decimal_text(e.amount) for e in led.usage_events()] == ["1.0", "2.0", "1.0"]


def test_mode_switch_is_modal_from_that_command_onwards():
    gcode = "M82\nG1 E1.0\nM83\nG1 E2.0\nM82\nG1 E5.0\n"
    led = ledger(gcode, mode="absolute")
    # absolute 1.0 -> new 1.0; relative 2.0 -> new 2.0; absolute 5.0 from 3.0 -> new 2.0
    assert led.usage() == {"A": Decimal("5.0")}


def test_nested_repeats_lazy_and_credentials_distinguish_iterations():
    snippets = {
        "unit": Snippet(["M83\nG1 E0.5"], name="unit"),
        "pass": Snippet([Repeat("unit", 2), "G1 E-1.0"], name="pass"),
    }
    program = PrintProgram(Repeat("pass", 3), snippets=snippets)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    # Each pass is two "unit" invocations (each one M83 + one G1) plus G1 E-1.0.
    assert led.command_count == 3 * (2 * 2 + 1)
    first = led.first_new_usage()
    assert first.ordinal == 1
    # The same source command repeated in a later iteration is a distinct execution.
    assert first.source == led.credential(6).source
    assert first != led.credential(6)
    assert first.iteration == (("pass", 0), ("unit", 0))
    assert led.credential(6).iteration == (("pass", 1), ("unit", 0))
    # Pass 0 makes 1.0 new and then retracts 1.0; every later pass fully repays
    # that debt before opening a new one, so it adds no new material.
    assert led.outstanding() == {"A": Decimal("1.0")}
    assert led.usage() == {"A": Decimal("1.0")}


def test_two_commands_on_one_line_have_distinct_positions():
    led = ledger("M83 G1 E-1.0 G1 E2.0\n")
    lines = list(parse_gcode_lines("M83 G1 E-1.0 G1 E2.0\n"))
    assert len(lines) == 3
    assert lines[0].command_index == 0
    assert lines[1].command_index == 1
    assert lines[2].command_index == 2
    assert lines[1].line_index == lines[2].line_index == 0
    assert led.command_count == 3
    assert led.credential(1).source.line_index == 0
    assert led.credential(1).source.command_index == 1
    assert led.credential(2).source.command_index == 2
    assert led.first_new_usage().ordinal == 2


def test_decimal_precision_is_taken_from_source_text():
    led = ledger("M83\nG1 E-0.050\nG1 E0.020\nG1 E0.100\n")
    usage = led.usage()
    assert decimal_text(usage["A"]) == "0.070"
    assert decimal_text(led.retractions()[0].amount) == "0.050"
    assert decimal_text(led.repaid("A:0")) == "0.050"
    # The first repayment is partial and keeps the source scale used by the payment.
    assert decimal_text(led._payments[0].amount) == "0.020"


def test_window_report_includes_first_usage_and_events():
    led = ledger("M83\nG1 E1.0\nG1 E2.0\n")
    report = led.window(1, 2)
    assert report.usage == {"A": Decimal("3.0")}
    assert report.first_new_usage.ordinal == 1
    assert report.first_new_usage_by_spool["A"].ordinal == 1
    assert len(report.events) == 2
    assert report.to_dict()["usage"] == {"A": "3.0"}


def test_retraction_queries_accept_origin_ordinal_and_string():
    led = ledger("M83\nG1 E-2.0\nG1 E2.0\n")
    by_string = led.repayment("A:0")
    by_ordinal = led.repayment(1)
    by_id = led.repayment(RetractionId("A", 0))
    assert by_string == by_ordinal == by_id == (Decimal("2.0"), led.credential(2))
    assert led.retraction_at(1).id == RetractionId("A", 0)


def test_missing_spool_for_active_tool_is_an_error():
    led = MaterialLedger("M83\nG1 E1.0\n", initial_spools={})
    with pytest.raises(ValueError, match="not connected"):
        led.usage()


def test_initial_extruder_coordinates_are_used_in_absolute_mode():
    led = ledger("M82\nG1 E5.0\n", mode="absolute", initial_e={0: "4.5"})
    assert led.usage() == {"A": Decimal("0.5")}


def test_program_from_spec_and_json_roundtrip(tmp_path):
    spec = {
        "initial": {"spools": {"0": "A"}, "mode": "relative"},
        "snippets": {
            "base": ["M83\nG1 E0.250"],
            "job": [
                {"repeat": {"snippet": "base", "count": 4}},
                {"handover": {"tool": 0, "spool": "B"}},
                "G1 E1.000",
            ],
        },
        "root": {"snippet": "job", "count": 2},
    }
    program = PrintProgram.from_spec(spec)
    led = MaterialLedger(
        program,
        initial_spools=spec["initial"]["spools"],
        mode=spec["initial"]["mode"],
    )
    assert led.command_count == 2 * (4 * 2 + 1)
    # First job: 4 * 0.250 = 1.000 on A, then a handover to B and 1.000 on B.
    # Second job stays on B for its 4 * 0.250, then adds another 1.000 on B.
    assert text_usage(led) == {"A": "1.000", "B": "3.000"}


def test_time_based_handover_before_command():
    led = MaterialLedger(
        "M83\nG1 E1.0\nG1 E2.0\n",
        initial_spools={0: "A"},
        mode="relative",
        timed_handovers={2: [Handover(0, "B")]},
    )
    assert led.usage() == {"A": Decimal("1.0"), "B": Decimal("2.0")}
