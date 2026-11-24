"""Command line front end for archive reconstruction."""

import argparse
import json
import os
import sys
import time

from . import deliver, manifest, reconstruct


def run(manifest_path, output_dir, time_limit=None):
    spec = manifest.load(manifest_path)
    manifest.validate_geometry(spec)
    os.makedirs(output_dir, exist_ok=True)

    inst = reconstruct.Reconstruct(spec)
    started = time.time()
    try:
        result = inst.solve()
    except reconstruct.Conflict as conflict:
        detail = reconstruct.minimal_conflict(spec)
        report = {
            "status": "conflict",
            "window": {
                "start": spec.series.start,
                "end": spec.series.start + spec.series.seconds,
            },
            "conflict": detail,
            "message": str(conflict),
        }
        path = os.path.join(output_dir, "reconstruction.json")
        deliver.dump_json(report, path)
        return {"status": "conflict", "report": path, "detail": detail}
    elapsed = time.time() - started

    seconds_rows = deliver.analyse_seconds(inst, result)
    target_rows = deliver.analyse_target(inst, result)
    report = deliver.build_report(
        inst, result, target_rows, seconds_rows, inst.provenance, inst.ignored
    )
    report["timing"] = {"solve_seconds": elapsed}

    target_path = os.path.join(output_dir, os.path.basename(spec.target.path))
    try:
        deliver.write_whisper(target_path, spec, seconds_rows, target_rows)
        report["target_file"] = target_path
        report["readback"] = deliver.verify_with_whisper(target_path, spec, target_rows)
        report["status"] = "ok"
    except deliver.WriteRefused as refusal:
        report["status"] = "layout_conflict"
        report["layout_conflict"] = refusal.cells

    report_path = os.path.join(output_dir, "reconstruction.json")
    deliver.dump_json(report, report_path)
    return {"status": report["status"], "report": report_path}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconstruct a Whisper archive from legacy evidence."
    )
    parser.add_argument("manifest", help="handover manifest JSON")
    parser.add_argument(
        "-o", "--output", default="reconstruction", help="output directory"
    )
    args = parser.parse_args(argv)
    outcome = run(args.manifest, args.output)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
