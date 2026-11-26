import json

import pytest

from docopt import DocoptExit
from docopt.reconcile import (
    ReconcileError,
    Slip,
    load_slip,
    reconcile,
    reconcile_many,
)

FIELD_DOC = """Usage:
  prog import -f <code> [--field=<x>] <branch>
  prog audit --field=<amount> <branch> [-f <code>]

Options:
  -f, --field=<f>   Field value [default: amount]
"""


def test_story_first_receipt_matching_command_wins():
    slip = Slip(
        name="field-slip",
        doc=FIELD_DOC,
        receipt=["code", "amount"],
        candidate_argv=[
            ["audit", "--field=amount", "b", "-fcode"],
            ["import", "-fcode", "--field=amount", "b"],
            ["import", "--field=amount", "-fcode", "b"],
        ],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert result.match.command_string == "import -fcode --field=amount b"
    assert result.match.field_order == ["code", "amount"]
    assert [entry.slot for entry in result.match.fields] == [
        "--field[0]",
        "--field[1]",
    ]
    assert [entry.origin for entry in result.match.fields] == ["argv", "argv"]
    assert result.match.fields[0].argv_index == 1
    assert result.match.fields[0].raw_token == "-fcode"
    assert result.match.fields[1].raw_token == "--field=amount"
    assert "import" in result.match.receiving_route
    assert "audit" not in result.match.receiving_route


def test_candidates_that_do_not_reproduce_receipt_are_rejected():
    slip = Slip(
        name="field-slip",
        doc=FIELD_DOC,
        receipt=["code", "amount"],
        candidate_argv=[
            ["audit", "--field=amount", "b", "-fcode"],
            ["import", "--field=amount", "-fcode", "b"],
        ],
    )

    result = reconcile(slip)

    assert result.status == "no_match"
    assert result.match is None
    assert [attempt["field_order"] for attempt in result.attempts] == [
        ["amount", "code"],
        ["amount", "code"],
    ]
    assert all(attempt["receipt_matches"] is False for attempt in result.attempts)


def test_spelling_variants_with_same_slots_are_not_ambiguous():
    slip = Slip(
        name="spelling",
        doc=FIELD_DOC,
        receipt=["code", "amount"],
        candidate_argv=[
            ["import", "-f", "code", "--field", "amount", "b"],
            ["import", "-fcode", "--field=amount", "b"],
        ],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert result.match.command == ["import", "-f", "code", "--field", "amount", "b"]
    assert result.alternate is None
    assert result.attempts[1]["receipt_matches"] is True


def test_long_option_abbreviation_uses_the_slip_doc():
    doc = """Usage: prog [--field=<f>] <branch>

Options:
  --field=<f>  Field value
"""
    slip = Slip(
        name="abbrev",
        doc=doc,
        receipt=["code"],
        candidate_argv=[["--fie=code", "b"], ["--field=code", "b"]],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert result.match.command_string == "--fie=code b"
    assert result.match.fields[0].raw_token == "--fie=code"
    assert result.match.fields[0].value == "code"


def test_clustered_short_options_split_switches_and_values():
    doc = """Usage: prog [-ab] [-f <f>] [<x>]

Options:
  -a        A flag
  -b        B flag
  -f <f>    F takes a value
"""
    slip = Slip(
        name="cluster",
        doc=doc,
        receipt=["code"],
        candidate_argv=[["-abfcode"]],
        field_options=["-f"],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    entry = result.match.fields[0]
    assert entry.value == "code"
    assert entry.raw_token == "-abfcode"
    assert entry.option == "-f"


def test_clustered_short_flags_do_not_swallow_following_words():
    doc = """Usage: prog [-ab] [-f <f>] [<x>]

Options:
  -a        A flag
  -b        B flag
  -f <f>    F takes a value
"""
    slip = Slip(
        name="flags",
        doc=doc,
        receipt=["x"],
        candidate_argv=[["-ab", "-f", "x"]],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert result.match.field_order == ["x"]
    assert result.match.fields[0].raw_token == "-f"


def test_default_value_booked_when_nothing_supplied():
    doc = """Usage: prog [--field=<f>] <branch>

Options:
  --field=<f>  Field value [default: amount]
"""
    slip = Slip(
        name="default",
        doc=doc,
        receipt=["amount"],
        candidate_argv=[["b"]],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    entry = result.match.fields[0]
    assert entry.value == "amount"
    assert entry.origin == "default"
    assert entry.argv_index is None


def test_supplied_value_replaces_repeating_default():
    doc = """Usage: prog [--field=<f>...] <branch>

Options:
  -f, --field=<f>  Field value [default: amount]
"""
    slip = Slip(
        name="repeat-default",
        doc=doc,
        receipt=["code", "amount"],
        candidate_argv=[["-fcode", "--field=amount", "b"]],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert [entry.origin for entry in result.match.fields] == ["argv", "argv"]
    assert result.match.field_order == ["code", "amount"]


def test_options_first_setting_is_per_slip():
    doc = """Usage: prog [<branch>] [--field=<f>]

Options:
  --field=<f>  Field value [default: amount]
"""
    flexible = Slip(
        name="intermixed",
        doc=doc,
        receipt=["code"],
        candidate_argv=[["b", "--field=code"]],
    )
    strict = Slip(
        name="strict",
        doc=doc,
        receipt=["code"],
        candidate_argv=[["b", "--field=code"]],
        options_first=True,
    )

    assert reconcile(flexible).status == "unique"
    strict_result = reconcile(strict)
    assert strict_result.status == "no_match"
    assert strict_result.attempts[0]["accepted"] is False


def test_missing_option_value_is_recorded_as_rejected():
    doc = """Usage: prog [-f <f>]

Options:
  -f <f>  F [default: amount]
"""
    slip = Slip(name="missing", doc=doc, receipt=["amount"], candidate_argv=[["-f"]])

    result = reconcile(slip)

    assert result.status == "no_match"
    assert result.attempts[0]["accepted"] is False
    assert "requires argument" in result.attempts[0]["reason"]


def test_ambiguous_result_reports_alternate_and_inconsistent_fields():
    doc = """Usage:
  prog implicit [--field=<v>] <branch>
  prog explicit --field=<v> <branch>

Options:
  --field=<v>  Field value [default: amount]
"""
    slip = Slip(
        name="ambiguous",
        doc=doc,
        receipt=["amount"],
        candidate_argv=[
            ["implicit", "b"],
            ["explicit", "--field=amount", "b"],
        ],
    )

    result = reconcile(slip)

    assert result.status == "ambiguous"
    assert result.match.command_string == "implicit b"
    assert result.match.fields[0].origin == "default"
    assert result.alternate is not None
    assert result.alternate.command_string == "explicit --field=amount b"
    assert result.alternate.fields[0].origin == "argv"
    origins = {item["origin"] for item in result.inconsistent_fields}
    assert origins == {"argv", "default"}


def test_first_matching_command_is_reported_even_when_later_matches_exist():
    slip = Slip(
        name="ordering",
        doc=FIELD_DOC,
        receipt=["code", "amount"],
        candidate_argv=[
            ["import", "-fcode", "--field=amount", "b"],
            ["import", "-f", "code", "--field", "amount", "b"],
        ],
    )

    result = reconcile(slip)

    assert result.status == "unique"
    assert result.match.command_string == "import -fcode --field=amount b"


def test_many_optional_slots_do_not_expand_combinations(monkeypatch):
    options = "\n".join(f"  --slot{i}=<v{i}>  Slot {i}" for i in range(40))
    usage = "  prog " + " ".join(f"[--slot{i}=<v{i}>]" for i in range(40)) + " <branch>"
    doc = f"Usage:\n{usage}\n\nOptions:\n{options}\n"
    slip = Slip(
        name="wide",
        doc=doc,
        receipt=["v7"],
        candidate_argv=[
            ["--slot7=v7", "b"],
            ["--slot8=other", "b"],
        ],
    )

    import docopt.reconcile as module

    calls = []
    original = module._evaluate

    def counting(prepared, argv):
        calls.append(argv)
        return original(prepared, argv)

    monkeypatch.setattr(module, "_evaluate", counting)
    result = reconcile(slip)

    assert len(calls) == len(slip.candidate_argv)
    assert result.status == "unique"
    assert result.match.field_order == ["v7"]


def test_reconcile_many_preserves_order_and_reports_no_match():
    good = Slip(
        name="good",
        doc=FIELD_DOC,
        receipt=["code", "amount"],
        candidate_argv=[["import", "-fcode", "--field=amount", "b"]],
    )
    bad = Slip(
        name="bad",
        doc=FIELD_DOC,
        receipt=["nope"],
        candidate_argv=[["import", "-fcode", "b"]],
    )

    results = reconcile_many([good, bad])

    assert [result.slip for result in results] == ["good", "bad"]
    assert [result.status for result in results] == ["unique", "no_match"]


def test_load_slip_accepts_string_commands_and_settings():
    payload = {
        "name": "from-json",
        "doc": FIELD_DOC,
        "receipt": ["code", "amount"],
        "candidate_argv": [
            "import -fcode --field=amount b",
            ["import", "--field=amount", "-fcode", "b"],
        ],
        "options_first": False,
    }

    slip = load_slip(payload)

    assert slip.candidate_argv[0] == ["import", "-fcode", "--field=amount", "b"]
    result = reconcile(slip)
    assert result.status == "unique"


def test_unknown_field_option_is_a_slip_error():
    slip = Slip(
        name="bad-config",
        doc=FIELD_DOC,
        receipt=["code"],
        candidate_argv=[["import", "-fcode", "b"]],
        field_options=["--nope"],
    )

    with pytest.raises(ReconcileError):
        reconcile(slip)


def test_empty_candidate_list_is_a_slip_error():
    slip = Slip(name="empty", doc=FIELD_DOC, receipt=[], candidate_argv=[])

    with pytest.raises(ReconcileError):
        reconcile(slip)


def test_docopt_exit_still_raised_by_original_library():
    with pytest.raises(DocoptExit):
        import docopt

        docopt.docopt(FIELD_DOC, argv=["import", "-fcode"])
