"""Tests for the high-level revision orchestration entry points."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

import mdfreader
from mdfreader.mdfinfo4 import Info4, _HeaderStruct
from mdfreader.revision import run_planning, run_revision


def _interleaved_compressed(tmp_path, n, all_invalid=True):
    m = mdfreader.Mdf()
    m.add_channel("Time", np.arange(n, dtype="<u8"), "Time", master_type=1)
    signal = np.ma.MaskedArray((np.arange(n) % 7).astype("<u4"),
                               mask=np.ones(n, dtype=bool) if all_invalid
                               else np.zeros(n, dtype=bool))
    m.add_channel("Sig", signal, "Time")
    path = tmp_path / "src.mf4"
    m.write4(str(path), compression=True)
    return path


def test_run_planning_never_opens_volume_and_reports_conflict():
    # A structurally illegal manifest (both candidate bits occupied and needing
    # a change) yields a conflict report rather than touching any volume.
    payload = {
        "block_order": ["D1"],
        "groups": [{
            "group_id": "G", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": 8, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "C", "cn_pointer": 9,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 1,
                "candidate_bits": [
                    {"bit_position": 0, "occupied_by": ["X"]},
                    {"bit_position": 1, "occupied_by": ["Y"]},
                ],
                "samples": [
                    {"sample_index": 0, "block_id": "D1", "block_pointer": 100,
                     "block_role": "data", "invalid_byte_offset": 0,
                     "approved": True,
                     "candidate_bits": [
                         {"bit_position": 0, "original_value": 1},
                         {"bit_position": 1, "original_value": 1},
                     ]},
                ],
            }],
        }],
    }
    manifest, plan, report = run_planning(payload)
    assert plan is None
    assert report["status"] == "conflict"
    assert report["conflicts"][0]["channels"] == ["C"]


def test_run_revision_full_workflow(tmp_path):
    n = 10
    path = _interleaved_compressed(tmp_path, n)
    from mdfreader.mdfinfo4 import Info4, _HeaderStruct
    from pathlib import Path

    raw = Path(path).read_bytes()
    dz_pointer = None
    stride = None
    p = 64
    while p + 24 <= len(raw):
        block_id, _r, length, _lc = _HeaderStruct.unpack_from(raw, p)
        if block_id == b"##DZ":
            dz_pointer = p
            _org, _zt, _res, stride, _ol, _dl = struct.unpack_from(
                "<2s2BI2Q", raw, p + 24)
        p = (p + length + 7) & ~7
    cn_pointer = next(
        cn_["pointer"]
        for dgp in Info4(str(path), minimal=1)["DG"]
        for cgp in Info4(str(path), minimal=1)["CN"][dgp]
        for cn_ in Info4(str(path), minimal=1)["CN"][dgp][cgp].values()
        if cn_["name"] == "Sig"
    )
    manifest_payload = {
        "block_order": ["DZ1"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "Sig", "cn_pointer": cn_pointer,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 1,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": [
                    {"sample_index": i, "block_id": "DZ1",
                     "block_pointer": dz_pointer, "block_role": "data",
                     "invalid_byte_offset": i * stride + (stride - 1),
                     "approved": i == 2,
                     "candidate_bits": [{"bit_position": 0, "original_value": 1}]}
                    for i in range(n)
                ],
            }],
        }],
    }
    output = tmp_path / "revised.mf4"
    result = run_revision(manifest_payload, path, output, output_dir=tmp_path)

    assert result["status"] == "planned"
    assert result["report"]["retransmit_blocks"] == ["DZ1"]
    assert (tmp_path / "revision_plan.json").exists()
    assert (tmp_path / "revision_build.json").exists()

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("Sig"))
    assert bool(mask[2]) is False
    assert all(bool(mask[i]) for i in range(n) if i != 2)
