from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vibration_audit import (  # noqa: E402
    Archive,
    cell_by_cell_audit,
    example_job,
    integer_d_equal_stats,
    run_job,
    word_stats_from_source,
)


N = 1 << 53


def test_builtin_audit_edge_buckets():
    report = run_job(example_job(), ROOT)
    checks = {check["id"]: check for check in report["checks"]}

    near = checks["near-2p53-d"]["counts"]
    assert near["same_value_and_word"] == 0
    assert near["same_word_different_value"] == 4
    assert near["different_word"] == 4
    assert near["rejected_either"] == 0

    zero = checks["negative-zero-d"]
    assert zero["counts"]["different_word"] == 1
    example = zero["first"]["different_word"]
    assert example["left"]["literal"] == "-0"
    assert example["left"]["word"] == "0x0000000000000000"
    assert example["right"]["literal"] == "-0.0"
    assert example["right"]["word"] == "0x8000000000000000"
    assert example["left"]["path"] == "/0/8/0"
    assert example["right"]["path"] == "/0/1"

    unsigned = checks["unsigned-with-decimal-u"]["counts"]
    assert unsigned["rejected_left"] == 0
    assert unsigned["rejected_right"] == 2
    assert unsigned["rejected_both"] == 0
    rejected = checks["unsigned-with-decimal-u"]["first"]["rejected_right"]
    assert rejected["right"]["literal"] == "9223372036854775808.0"
    assert "INCORRECT_TYPE" in rejected["right"]["error"]


def test_small_aggregate_matches_native_cell_by_cell():
    job = example_job()
    archives = {
        name: Archive(name, spec, ROOT)
        for name, spec in job["archives"].items()
    }
    report = run_job(job, ROOT)
    for spec, aggregate in zip(job["checks"], report["checks"]):
        reference = cell_by_cell_audit(spec, archives)
        assert aggregate["counts"] == reference["counts"]


def test_unsigned_buffer_accepts_values_above_signed_max():
    archive = Archive(
        "u64",
        {
            "segments": [{
                "count": 2,
                "start": str(1 << 63),
                "step": str((1 << 64) - 1 - (1 << 63)),
                "literal": "integer",
            }]
        },
        ROOT,
    )
    source = archive.root_source("u")
    assert source is not None
    buffer, count = source
    view = memoryview(buffer).cast("Q")
    assert count == 2
    assert view[0] == 1 << 63
    assert view[1] == (1 << 64) - 1


def test_nested_shape_preserves_empty_array_locations():
    archive = Archive(
        "nested",
        {
            "segments": [{"count": 2, "start": "0", "step": "1"}],
            "shape": [[2, []]],
        },
        ROOT,
    )
    assert archive.data == b"[[0,1,[]]]"
    assert archive.path_and_token(0)[0] == "/0/0"
    assert archive.path_and_token(1)[0] == "/0/1"


def test_raw_json_paths_and_literals():
    archive = Archive(
        "raw",
        {"json_text": '[[1, [-0.0]], [], {"x": 2}]'},
        ROOT,
    )
    assert archive.path_and_token(0) == ("/0/0", "1")
    assert archive.path_and_token(1) == ("/0/1/0", "-0.0")
    rejected = archive.native_cell(2, "i")
    assert rejected["ok"] is False
    assert "INCORRECT_TYPE" in rejected["error"]


@pytest.mark.parametrize(
    ("count", "step_a", "step_b", "start_a", "start_b"),
    [
        (5000, 1, 1, N, N + 1),
        (5000, 2, 2, N, N + 1),
        (5000, 3, 3, N, N + 1),
        (5000, 5, 2, N - 50, N + 1),
        (5000, 1, 1, 1 << 54, (1 << 54) + 1),
        (5000, 7, 3, (1 << 55) - 100, (1 << 55) + 1),
    ],
)
def test_integer_segment_formula_matches_native_words(
    count, step_a, step_b, start_a, start_b
):
    left = Archive(
        "left",
        {"segments": [{
            "count": count,
            "start": str(start_a),
            "step": str(step_a),
            "literal": "integer",
        }]},
        ROOT,
    )
    right = Archive(
        "right",
        {"segments": [{
            "count": count,
            "start": str(start_b),
            "step": str(step_b),
            "literal": "integer",
        }]},
        ROOT,
    )
    left_source = left.root_source("d")
    right_source = right.root_source("d")
    assert left_source is not None
    assert right_source is not None
    actual = word_stats_from_source(
        left_source[0], left_source[1], 0,
        right_source[0], right_source[1], 0,
        count,
    )
    formula = integer_d_equal_stats(
        start_a, step_a, start_b, step_b, count
    )
    assert formula == actual
