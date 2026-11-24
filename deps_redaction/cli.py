"""Command line entry point.

Usage::

    python -m deps_redaction.cli bundle.json deliverable.conllu manifest.json
"""

from __future__ import annotations

import argparse
import sys

from .io_pipeline import run_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", help="JSON bundle with sentences, licenses and queries")
    parser.add_argument("deliverable", help="output CoNLL-U path")
    parser.add_argument("manifest", help="output manifest JSON path")
    args = parser.parse_args(argv)

    results = run_bundle(args.bundle, args.deliverable, args.manifest)
    feasible = sum(1 for result in results if result.feasible)
    for result in results:
        status = "ok" if result.feasible else "INFEASIBLE"
        print(
            f"[{status}] {result.sent_id}: {len(result.kept_edge_ids)} relations kept, "
            f"{result.placeholder_count} empty row(s)"
        )
        for line in result.diagnostics:
            print(f"    - {line}")
    print(f"{feasible}/{len(results)} sentences deliverable")
    return 0 if feasible == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
