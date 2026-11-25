"""Command line: ``python -m migration plan HANDOVER.json``.

Prints an executable switch sequence to the final tables, or — when
no sequence exists — the earliest blocking permission sets and
collision witnesses.
"""

from __future__ import annotations

import argparse
import json
import sys

from .spec import load, SpecError
from .planner import plan


def main(argv=None):
    parser = argparse.ArgumentParser(prog="migration")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="compute a switch sequence")
    p_plan.add_argument("sheet", help="handover sheet (JSON)")
    p_plan.add_argument("--json", action="store_true",
                        help="emit the result as JSON")
    args = parser.parse_args(argv)

    with open(args.sheet, "r", encoding="utf-8") as fh:
        try:
            handover = load(fh.read())
        except SpecError as exc:
            print("invalid handover sheet: %s" % exc, file=sys.stderr)
            return 2

    result = plan(handover)
    if args.json:
        if result.ok:
            payload = {
                "ok": True,
                "borrowed": list(result.borrowed),
                "steps": [
                    {
                        "index": s.index,
                        "segment": s.segment or None,
                        "from": s.from_table or None,
                        "to": s.to_table or None,
                        "ext_code": s.ext_code if s.segment else None,
                        "enables": [list(e) for e in s.enables],
                        "disables": [list(e) for e in s.disables],
                        "awaits": [list(a) for a in s.awaits],
                        "cleared": [list(c) for c in s.cleared],
                        "returned": list(s.returned),
                    }
                    for s in result.steps
                ],
            }
        else:
            payload = {
                "ok": False,
                "blocking": [
                    {"depth": b["depth"], "state": b["state"],
                     "reasons": list(b["reasons"])}
                    for b in result.blocking
                ],
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(result.describe())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
