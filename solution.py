from __future__ import annotations

from typing import Any, Mapping

from tiktoken.attestation import (
    attest_catalog,
    build_encoding,
    verify_small_catalog,
)


def solve(task: Mapping[str, Any] | None = None, *, verify: bool | None = None) -> dict[str, Any]:
    if task is None:
        from tiktoken.attestation import _demo_manifest

        task = _demo_manifest()

    left = build_encoding(task.get("left_encoding", task.get("encoding_left")))
    right = build_encoding(task.get("right_encoding", task.get("encoding_right")))
    catalog = task.get("catalog", task)
    report = attest_catalog(
        left,
        right,
        catalog,
        allowed_special_left=task.get("allowed_special_left"),
        allowed_special_right=task.get("allowed_special_right"),
    )

    should_verify = bool(task.get("verify", False)) if verify is None else verify
    if should_verify:
        report.native_verification = verify_small_catalog(
            left,
            right,
            catalog,
            report,
            allowed_special_left=task.get("allowed_special_left"),
            allowed_special_right=task.get("allowed_special_right"),
        )

    return report.to_dict()


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1:
        from pathlib import Path

        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    else:
        payload = None
    print(json.dumps(solve(payload), ensure_ascii=False, indent=2))
