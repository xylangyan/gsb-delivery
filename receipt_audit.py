#!/usr/bin/env python3
"""Replay the receipt-placement ticket against an unmodified stock Clize.

Run from the repository root with ``.venv/bin/python receipt_audit.py``.
The script only parses commands; it never invokes collection or disk writes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from clize import errors, parser


@dataclass(frozen=True)
class Preregistration:
    file: str
    pos0: int
    first_slot: int
    calibration: int | None
    channel: str | None


class RecordingFileArgs(parser.ExtraPosArgsParameter):
    suffixes = (".dat",)

    def __init__(self, ledger: list[Preregistration]):
        super().__init__(
            required=True,
            argument_name="files",
            display_name="file",
        )
        self.ledger = ledger

    def read_argument(self, ba: parser.CliBoundArguments, i: int) -> None:
        raw = ba.in_args[i]
        calibration = ba.kwargs.pop("calibration", None)
        channel = ba.kwargs.pop("channel", None)
        self.ledger.append(
            Preregistration(
                file=raw,
                pos0=i,
                first_slot=i,
                calibration=calibration,
                channel=channel,
            )
        )
        if not raw.endswith(self.suffixes):
            raise errors.BadArgumentFormat(raw)
        super().read_argument(ba, i)


def scan(*files: str, **options: Any) -> dict[str, Any]:
    return {"action": "scan", "files": files, "options": options}


def receipt(entry: str, *words: str, **options: Any) -> dict[str, Any]:
    return {
        "action": "receipt",
        "entry": entry,
        "words": words,
        "options": options,
    }


def first_only(entry: str, *words: str, **options: Any) -> dict[str, Any]:
    return {
        "action": "first-only",
        "entry": entry,
        "words": words,
        "options": options,
    }


def build_signature(ledger: list[Preregistration]) -> parser.CliSignature:
    file_args = RecordingFileArgs(ledger)
    calibration = parser.IntOptionParameter(
        argument_name="calibration",
        aliases=["-i", "--calibration"],
        conv=int,
        default=None,
    )
    channel = parser.FlagParameter(
        argument_name="channel",
        aliases=["-f"],
        value="f",
    )
    receipt_param = parser.FallbackCommandParameter(
        func=receipt,
        aliases=["--receipt"],
    )
    alternate_param = parser.AlternateCommandParameter(
        func=first_only,
        aliases=["--first-only"],
    )
    return parser.CliSignature(
        [file_args, calibration, channel, receipt_param, alternate_param]
    )


def parse(argv: list[str]) -> dict[str, Any]:
    ledger: list[Preregistration] = []
    signature = build_signature(ledger)
    try:
        bound = signature.read_arguments(argv, "scan")
    except errors.ArgumentError as exc:
        pos0 = getattr(exc, "pos", None)
        error: dict[str, Any] = {
            "type": type(exc).__name__,
            "pos0": pos0,
            "original_position": pos0 if pos0 is not None else "end",
            "message": str(exc).splitlines()[0],
        }
        if isinstance(exc, errors.MissingRequiredArguments):
            error["missing_variants"] = [
                sorted(param.display_name for param in exc.missing)
            ]
            error["pos0"] = len(argv)
        return {
            "ok": False,
            "error": error,
            "preregistrations": [asdict(item) for item in ledger],
        }

    action = bound.func.__name__ if bound.func is not None else "scan"
    target = bound.func or scan
    result = target(*bound.args, **bound.kwargs)
    return {
        "ok": True,
        "action": action,
        "args": list(bound.args),
        "kwargs": dict(bound.kwargs),
        "result": result,
        "preregistrations": [asdict(item) for item in ledger],
    }


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}\n expected={expected!r}\n actual={actual!r}")


def run_small_ticket() -> dict[str, Any]:
    first_slot = parse(["--receipt", "a.dat", "b.dat"])
    assert first_slot["ok"] is True
    assert_equal(first_slot["action"], "receipt", "first slot action")
    assert_equal(
        first_slot["args"],
        ["scan --receipt", "a.dat", "b.dat"],
        "first slot receipt arguments",
    )

    after_bad_suffix = parse(["bad.txt", "--receipt"])
    assert after_bad_suffix["ok"] is True
    assert_equal(after_bad_suffix["action"], "receipt", "post-error action")
    assert_equal(
        after_bad_suffix["args"],
        ["scan --receipt"],
        "fallback receipt discards trailing words",
    )
    assert_equal(
        after_bad_suffix["preregistrations"],
        [
            {
                "file": "bad.txt",
                "pos0": 0,
                "first_slot": 0,
                "calibration": None,
                "channel": None,
            }
        ],
        "failed file remains preregistered",
    )

    after_double_dash = parse(["--", "bad.txt", "--receipt"])
    assert after_double_dash["ok"] is True
    assert_equal(after_double_dash["action"], "receipt", "after -- fallback")
    assert_equal(
        after_double_dash["preregistrations"][0]["pos0"],
        1,
        "bad file position after --",
    )

    decorated = parse(["-i12f", "a.dat", "-i3", "b.dat"])
    assert decorated["ok"] is True
    assert_equal(decorated["args"], ["a.dat", "b.dat"], "decorated file args")
    assert_equal(
        decorated["preregistrations"],
        [
            {
                "file": "a.dat",
                "pos0": 1,
                "first_slot": 1,
                "calibration": 12,
                "channel": "f",
            },
            {
                "file": "b.dat",
                "pos0": 3,
                "first_slot": 3,
                "calibration": 3,
                "channel": None,
            },
        ],
        "short option cluster binding",
    )

    alternate_after_file = parse(["bad.txt", "--first-only"])
    assert alternate_after_file["ok"] is False
    assert_equal(
        alternate_after_file["error"]["type"],
        "BadArgumentFormat",
        "alternate after positional does not recover",
    )
    assert_equal(
        alternate_after_file["error"]["original_position"],
        0,
        "original error position is retained",
    )

    receipt_after_failed_alternate = parse(
        ["bad.txt", "--first-only", "--receipt"]
    )
    assert receipt_after_failed_alternate["ok"] is True
    assert_equal(
        receipt_after_failed_alternate["action"],
        "receipt",
        "fallback scan continues after a rejected alternate",
    )

    return {
        "ticket": "CLIZE-RECEIPT-SMALL",
        "status": "verified-with-stock-clize",
        "cases": {
            "receipt_first_slot": first_slot,
            "receipt_after_bad_suffix": after_bad_suffix,
            "receipt_after_double_dash": after_double_dash,
            "short_option_cluster": decorated,
            "alternate_rejected_after_file": alternate_after_file,
            "receipt_after_failed_alternate": receipt_after_failed_alternate,
        },
    }


def run_false_success_counterexample() -> dict[str, Any]:
    before_marker_change = parse(["bad.txt", "--receipt", "-g"])
    after_marker_change = parse(["bad.txt", "--receipt", "-f"])

    def naive_sees_success(outcome: dict[str, Any]) -> bool:
        return outcome["ok"] and outcome["action"] == "receipt"

    naive_before = naive_sees_success(before_marker_change)
    naive_after = naive_sees_success(after_marker_change)
    strict_marker_bound = (
        bool(after_marker_change["preregistrations"])
        and after_marker_change["preregistrations"][0]["channel"] == "f"
    )

    assert naive_before is True
    assert naive_after is True
    assert strict_marker_bound is False
    assert before_marker_change["args"] == after_marker_change["args"]
    assert (
        before_marker_change["preregistrations"]
        == after_marker_change["preregistrations"]
    )

    return {
        "ticket": "CLIZE-RECEIPT-FALSE-SUCCESS",
        "warning": (
            "Both rear-marker variants reach the same receipt function, but "
            "the marker is not parsed or bound. Matching only final action is "
            "a false success."
        ),
        "naive_check": {
            "before_change_to_f": naive_before,
            "after_change_to_f": naive_after,
        },
        "strict_check": {
            "requested_channel_f_bound": strict_marker_bound,
            "same_exact_args": (
                before_marker_change["args"] == after_marker_change["args"]
            ),
        },
        "before": before_marker_change,
        "after": after_marker_change,
    }


def main() -> None:
    report = {
        "small_ticket": run_small_ticket(),
        "counterexample": run_false_success_counterexample(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
