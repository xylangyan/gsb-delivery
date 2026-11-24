"""Re-read a delivered handover archive with the public Whisper API only.

This is the acceptance view: no model internals, just ``whisper.info`` and
``whisper.fetch`` over the target layers, checked against the report.
"""

import argparse
import json
import os
import sys


def check(report_path, archive_path):
    with open(report_path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    if report["status"] != "ok":
        return {"ok": False, "reason": "report status is %s" % report["status"]}

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    import whisper

    window = report["window"]
    now = window["end"]
    problems = []
    levels = []
    for level in report["levels"]:
        step = level["level"]
        time_info, values = whisper.fetch(
            archive_path, window["start"] - 1, window["end"] - 1,
            now=now, archiveToSelect=step,
        )
        first = time_info[0]
        fetched = {first + i * step: v for i, v in enumerate(values)}
        for cell in level["cells"]:
            got = fetched.get(cell["start"])
            if cell["state"] == "value":
                if got is None or abs(got - cell["value"]) > 1e-6:
                    problems.append(
                        {"level": step, "start": cell["start"], "expected":
                         cell["value"], "fetched": got}
                    )
            elif cell["state"] in ("missing", "pending"):
                if got is not None:
                    problems.append(
                        {"level": step, "start": cell["start"], "expected": None,
                         "fetched": got}
                    )
        levels.append(
            {
                "level": step,
                "time_info": list(time_info),
                "values": values,
            }
        )
    return {"ok": not problems, "levels": levels, "problems": problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="reconstruction.json")
    parser.add_argument("archive", help="handover .wsp")
    args = parser.parse_args(argv)
    result = check(args.report, args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
