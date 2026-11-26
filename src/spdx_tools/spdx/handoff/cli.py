# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for a JSON to TagValue handoff.

Usage::

    pyspdxtools_handoff --manifest handoff.json --out-dir output/

The manifest is a JSON document::

    {
      "packageOrder": ["SPDXRef-A", "SPDXRef-B"],
      "verifySmallCases": true,
      "revisions": [
        {"name": "rev-1", "json": "catalog-1.json"},
        {"name": "rev-2", "json": "catalog-2.json", "fileOrder": ["SPDXRef-F2", "SPDXRef-F1"]}
      ]
    }

Each revision is written to ``<out-dir>/<name>.spdx`` and the plan, byte costs,
diagnostics and verification records are printed to stdout as JSON.
"""

import json
import os
import sys

from spdx_tools.spdx.handoff.batch import handoff_from_manifest_file, safe_name


def run(manifest: str, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    result = handoff_from_manifest_file(manifest, out_dir)
    for name, text in result.documents.items():
        path = os.path.join(out_dir, safe_name(name) + ".spdx")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if result.diagnostics else 0


def main():
    """Console-script entry point; ``click`` is imported only when invoked."""
    import click

    @click.command()
    @click.option("--manifest", "-m", required=True, help="The handoff manifest JSON file.")
    @click.option("--out-dir", "-o", default=".", help="Directory for the rendered TagValue documents.")
    def command(manifest: str, out_dir: str):
        sys.exit(run(manifest, out_dir))

    return command()


if __name__ == "__main__":
    main()
