"""Command line entry for the batch-slip reconciliation tool.

Input is one JSON document, either a single slip::

    {
      "name": "slip-0042",
      "doc": "Usage: prog ...\\n\\nOptions:\\n  ...",
      "receipt": ["code", "amount", "amount"],
      "candidate_argv": [["-fcode", "--field=amount", "b"], "..."],
      "field_options": ["--field"],
      "options_first": false
    }

or a batch of them under ``{"slips": [...]}``. The report is printed as JSON,
one object per slip, in input order. Candidate commands are parsed through the
original docopt-ng library one at a time; slot combinations are never expanded.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from docopt.reconcile import ReconcileError, load_slip, reconcile_many


def _load(payload: Any) -> list:
    if isinstance(payload, dict) and "slips" in payload:
        return [load_slip(item) for item in payload["slips"]]
    return [load_slip(payload)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or args in (["-h"], ["--help"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        if not args or args[0] == "-":
            payload = json.load(sys.stdin)
        else:
            with open(args[0], encoding="utf-8") as handle:
                payload = json.load(handle)
        slips = _load(payload)
    except (OSError, json.JSONDecodeError, ReconcileError) as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    results = reconcile_many(slips)
    report = [result.as_dict() for result in results]
    json.dump(report if len(report) > 1 else report[0], sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
