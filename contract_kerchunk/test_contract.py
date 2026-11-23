"""End-to-end tests for the minimum-cost contract planner."""
from __future__ import annotations

import json
import os

import fsspec
import numpy as np
import pytest
import zarr

from .optimality import audit
from .planner import plan
from .publisher import publish
from .scenario import FILL
from .verifier import verify


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("contract"))
    manifest, plan_doc = plan(out)
    publish(manifest, plan_doc, out)
    report = verify(out)
    optimality = audit(out)
    return out, manifest, plan_doc, report, optimality


def test_reread_matches_ledger_truth(built):
    out, manifest, _, _, _ = built
    group = zarr.open_group(
        fsspec.get_mapper("reference://", fo=os.path.join(out, "index.json")),
        mode="r", zarr_format=2)
    assert group["time"][:].tolist() == manifest["timestamps"]
    for var in manifest["contract"]["variables"]:
        arr = group[var][:]
        expected = np.asarray(manifest["truth"][var])
        if var == "soil":
            assert np.array_equal(arr[:6], expected[:6])
            assert np.all(arr[6:] == FILL)
        else:
            assert np.array_equal(arr, expected)
            assert np.flatnonzero(arr == FILL).tolist() == [5]


def test_gap_and_conflict_resolution(built):
    _, manifest, plan_doc, _, _ = built
    decisions = {(d["variable"], tuple(d["window"])): d
                 for d in plan_doc["decisions"]}
    # B's corrupt temp value at index 6 never appears; temp[4:8] is rebuilt
    # from A and persisted as a new remote block.
    temp1 = decisions[("temp", (4, 8))]
    assert temp1["kind"] == "recompute"
    assert all(r["archive"] == "A" for r in temp1["reads"])
    assert temp1["published_uri"].endswith("chunk_4_8.bin")


def test_direct_reference_when_whole_block_decodes(built):
    _, _, plan_doc, _, _ = built
    decisions = {(d["variable"], tuple(d["window"])): d
                 for d in plan_doc["decisions"]}
    direct = decisions[("temp", (0, 4))]
    assert direct["kind"] == "direct" and direct["fee"] == 0
    # gust's last block is the same physical bytes as B/wind and is in-place.
    assert decisions[("gust", (6, 8))]["kind"] == "direct"


def test_physical_range_dedup_and_fee(built):
    _, manifest, plan_doc, _, _ = built
    ranges = [(r["file"], r["start"], r["end"])
              for r in plan_doc["paid_ranges"]]
    assert len(ranges) == len(set(ranges))
    fee = sum(manifest["contract"]["read_fee"][r["archive"]]
              for r in plan_doc["paid_ranges"])
    assert fee == plan_doc["total_fee"] == 22
    # B ranges used by both wind and gust must be billed once each.
    b_ranges = [r for r in plan_doc["paid_ranges"]
                if r["archive"] == "B"]
    assert len(b_ranges) == 5


def test_missing_permission_reported(built):
    _, _, plan_doc, _, _ = built
    assert plan_doc["infeasible"] == [{
        "variable": "soil",
        "target_window": [6, 8],
        "missing_permission": plan_doc["infeasible"][0]["missing_permission"],
    }]
    assert "A/soil" in plan_doc["infeasible"][0]["missing_permission"]


def test_all_checks_and_optimality(built):
    _, _, _, report, optimality = built
    assert report["ok"]
    assert all(c["passed"] for c in report["checks"])
    assert optimality["optimal"]
    assert optimality["minimum_fee"] == 22


def test_no_inline_data_chunks(built):
    out, *_ = built
    refs = json.load(open(os.path.join(out, "index.json")))["refs"]
    for key, value in refs.items():
        if key.split("/")[-1].isdigit():
            assert isinstance(value, list) and os.path.isfile(value[0])
