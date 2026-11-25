"""End-to-end revision tests on real MDF4 volumes written by mdfreader.

These build genuine MDF4 files (compressed interleaved DZ, plain DT and column
oriented LD/DV/DI layouts), plan against manifests derived from their physical
block addresses, rebuild the revised volume and reread it with mdfreader to
prove that only approved samples regain validity while signal values,
conversions and the master channel are untouched.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

import mdfreader
from mdfreader.mdfinfo4 import Info4, _HeaderStruct
from mdfreader.revision import (
    build_revised_volume,
    parse_manifest,
    plan_revision,
)


def _block_map(path):
    raw = Path(path).read_bytes() if not isinstance(path, (bytes, bytearray)) else bytes(path)
    blocks = {}
    p = 64
    while p + 24 <= len(raw):
        block_id, _res, length, link_count = _HeaderStruct.unpack_from(raw, p)
        if not block_id.startswith(b"##"):
            break
        blocks.setdefault(block_id, []).append((p, length, link_count))
        p = (p + length + 7) & ~7
    return raw, blocks


def _first(blocks, signature):
    return blocks[signature][0][0]


def _write_interleaved(tmp_path, name, n, all_invalid, compression, dtype="<u4"):
    m = mdfreader.Mdf()
    time = np.arange(n, dtype="<u8")
    values = (np.arange(n) % 7).astype(dtype)
    if all_invalid:
        signal = np.ma.MaskedArray(values, mask=np.ones(n, dtype=bool))
    else:
        signal = values
    m.add_channel("Time", time, "Time", master_type=1, unit="s")
    m.add_channel("Sig", signal, "Time", unit="V")
    path = tmp_path / name
    m.write4(str(path), compression=compression)
    return path


def _cn_pointer_for(path, channel_name):
    info = Info4(str(path), minimal=1)
    for dgp in info["DG"]:
        for cgp in info["CG"][dgp]:
            for cn in info["CN"][dgp][cgp].values():
                if cn["name"] == channel_name:
                    return cn["pointer"], info
    raise KeyError(channel_name)


def _dz_pointer(raw, blocks):
    return _first(blocks, b"##DZ")


def _record_stride(raw, blocks):
    info_blocks = blocks
    # stride = (DZ original length) / cycle count is read from the DZ header.
    dz_pointer = _dz_pointer(raw, info_blocks)
    _org, _zt, _res, zip_parameter, _ol, _dl = struct.unpack_from(
        "<2s2BI2Q", raw, dz_pointer + 24)
    return zip_parameter


def test_compressed_dz_interleaved_revision_rereads_correctly(tmp_path):
    n = 24
    path = _write_interleaved(tmp_path, "mix.mf4", n, all_invalid=True,
                              compression=True)
    raw, blocks = _block_map(path)
    dz_pointer = _dz_pointer(raw, blocks)
    stride = _record_stride(raw, blocks)  # data bytes + 1 invalidation byte
    cn_pointer, _ = _cn_pointer_for(path, "Sig")

    approved = {0, 2, 5}
    samples = []
    for index in range(n):
        samples.append({
            "sample_index": index,
            "block_id": "DZ1",
            "block_pointer": dz_pointer,
            "block_role": "data",
            "invalid_byte_offset": index * stride + (stride - 1),
            "approved": index in approved,
            "candidate_bits": [{"bit_position": 0, "original_value": 1}],
        })
    manifest = parse_manifest({
        "block_order": ["DZ1"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "Sig", "cn_pointer": cn_pointer,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 0b01,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": samples,
            }],
        }],
    })
    plan = plan_revision(manifest)
    assert plan.retransmit_blocks == ["DZ1"]

    output = tmp_path / "mix_revised.mf4"
    summary = build_revised_volume(manifest, plan, path, output)
    assert [item["block_id"] for item in summary["retransmitted_blocks"]] == ["DZ1"]

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("Sig"))
    assert mask.tolist() == [index not in approved for index in range(n)]
    # Signal raw values and the master channel survive unchanged.
    assert np.array_equal(np.asarray(reread.get_channel_data("Sig")),
                          (np.arange(n) % 7))
    assert np.array_equal(reread.get_channel_data("Time"), np.arange(n))


def test_unselected_data_block_is_reused_byte_for_byte(tmp_path):
    # Two data groups, each with its own compressed block; revise only the first.
    n = 16

    def build(name):
        mm = mdfreader.Mdf()
        mm.add_channel("Time", np.arange(n, dtype="<u8"), "Time", master_type=1)
        signal = np.ma.MaskedArray((np.arange(n) % 7).astype("<u4"),
                                   mask=np.ones(n, dtype=bool))
        mm.add_channel(name, signal, "Time")
        path = tmp_path / f"{name}.mf4"
        mm.write4(str(path), compression=True)
        return path

    path_a = build("SigA")
    path_b = build("SigB")
    merged = mdfreader.Mdf(str(path_a))
    merged.merge_mdf(mdfreader.Mdf(str(path_b)))
    combined = tmp_path / "two_dg.mf4"
    merged.write4(str(combined), compression=True)

    raw, blocks = _block_map(combined)
    dz_pointers = [item[0] for item in blocks[b"##DZ"]]
    assert len(dz_pointers) == 2
    target_dz, untouched_dz = dz_pointers
    _, untouched_length = blocks[b"##DZ"][1][:2]
    untouched_payload = raw[untouched_dz:untouched_dz + untouched_length]

    cn_pointer, _ = _cn_pointer_for(combined, "SigA")
    stride = _record_stride(raw, blocks)
    samples = [{
        "sample_index": index,
        "block_id": "DZA",
        "block_pointer": target_dz,
        "block_role": "data",
        "invalid_byte_offset": index * stride + (stride - 1),
        "approved": index == 1,
        "candidate_bits": [{"bit_position": 0, "original_value": 1}],
    } for index in range(n)]
    manifest = parse_manifest({
        "block_order": ["DZA"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "SigA", "cn_pointer": cn_pointer,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 0b01,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": samples,
            }],
        }],
    })
    plan = plan_revision(manifest)
    output = tmp_path / "two_dg_revised.mf4"
    build_revised_volume(manifest, plan, combined, output)

    revised_raw, _ = _block_map(output)
    # The untouched compressed block keeps its exact original bytes.
    assert bytes(untouched_payload) in revised_raw

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("SigA"))
    assert bool(mask[1]) is False
    assert all(mask[index] for index in range(n) if index != 1)
    # The second data group remains entirely invalid.
    other = np.ma.getmaskarray(reread.get_channel_data("SigB"))
    assert other.all()


def test_column_di_revision_rereads_correctly(tmp_path):
    n = 10
    m = mdfreader.Mdf()
    time = np.arange(n, dtype="<u8")
    signal = np.ma.MaskedArray(np.arange(100, 100 + n, dtype="<u2"),
                               mask=np.ones(n, dtype=bool))
    m.add_channel("Time", time, "Time", master_type=1)
    m.add_channel("Col", signal, "Time")
    path = tmp_path / "col.mf4"
    m.write4(str(path), column_oriented=True)
    original_values = np.asarray(mdfreader.Mdf(str(path)).get_channel_data("Col"))

    raw, blocks = _block_map(path)
    di_pointer = _first(blocks, b"##DI")
    cn_pointer, _ = _cn_pointer_for(path, "Col")
    approved = {3, 7}
    samples = [{
        "sample_index": index,
        "block_id": "DI1",
        "block_pointer": di_pointer,
        "block_role": "invalid",
        "invalid_byte_offset": index,  # one invalidation byte per column sample
        "approved": index in approved,
        "candidate_bits": [{"bit_position": 0, "original_value": 1}],
    } for index in range(n)]
    manifest = parse_manifest({
        "block_order": ["DI1"],
        "groups": [{
            "group_id": "G1", "layout": "column",
            "data_group": 1, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": 2, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "Col", "cn_pointer": cn_pointer,
                "data_group": 1, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 0b01,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": samples,
            }],
        }],
    })
    plan = plan_revision(manifest)
    output = tmp_path / "col_revised.mf4"
    build_revised_volume(manifest, plan, path, output)

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("Col"))
    assert mask.tolist() == [index not in approved for index in range(n)]
    # The companion DV block is not retransmitted, so its bytes and the signal
    # values are preserved byte-for-byte even though the all-invalid write used
    # a placeholder fill.
    assert np.array_equal(np.asarray(reread.get_channel_data("Col")),
                          original_values)


def test_plain_dt_uncompressed_in_place_edit(tmp_path):
    n = 12
    path = _write_interleaved(tmp_path, "plain.mf4", n, True, compression=False)
    raw, blocks = _block_map(path)
    dt_pointer = _first(blocks, b"##DT")
    cn_pointer, _ = _cn_pointer_for(path, "Sig")
    stride = 8 + 4 + 1  # u8 time + u4 signal + 1 invalidation byte
    samples = [{
        "sample_index": index,
        "block_id": "DT1",
        "block_pointer": dt_pointer,
        "block_role": "data",
        "invalid_byte_offset": index * stride + (stride - 1),
        "approved": index == 4,
        "candidate_bits": [{"bit_position": 0, "original_value": 1}],
    } for index in range(n)]
    manifest = parse_manifest({
        "block_order": ["DT1"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "Sig", "cn_pointer": cn_pointer,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 0b01,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": samples,
            }],
        }],
    })
    plan = plan_revision(manifest)
    output = tmp_path / "plain_revised.mf4"
    build_revised_volume(manifest, plan, path, output)

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("Sig"))
    assert mask.tolist() == [index != 4 for index in range(n)]
    # Same in-place length: output keeps the DT block size.
    revised_raw, revised_blocks = _block_map(output)
    assert revised_blocks[b"##DT"][0][1] == blocks[b"##DT"][0][1]


def test_no_data_retransmit_but_cn_flag_switch_still_revises(tmp_path):
    # The approved sample already carries a clear bit in the source, so no data
    # block changes; the revision only has to drop CN_F_ALL_INVALID and switch
    # the channel to bit-driven validity.
    n = 8
    path = _write_interleaved(tmp_path, "flagonly.mf4", n, True, compression=False)
    raw, blocks = _block_map(path)
    dt_pointer = _first(blocks, b"##DT")
    stride = 8 + 4 + 1
    # Clear bit 0 of the first record's invalidation byte directly in source.
    patched = bytearray(raw)
    first_invalid = dt_pointer + 24 + (stride - 1)
    patched[first_invalid] = 0
    path.write_bytes(bytes(patched))

    cn_pointer, _ = _cn_pointer_for(path, "Sig")
    samples = [{
        "sample_index": index,
        "block_id": "DT1",
        "block_pointer": dt_pointer,
        "block_role": "data",
        "invalid_byte_offset": index * stride + (stride - 1),
        "approved": index == 0,
        # First sample already valid in the raw byte, rest invalid.
        "candidate_bits": [{"bit_position": 0,
                            "original_value": 0 if index == 0 else 1}],
    } for index in range(n)]
    manifest = parse_manifest({
        "block_order": ["DT1"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [{
                "channel_name": "Sig", "cn_pointer": cn_pointer,
                "data_group": 0, "channel_group": 0,
                "current_bit_position": 0, "current_flags": 0b01,
                "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
                "samples": samples,
            }],
        }],
    })
    plan = plan_revision(manifest)
    assert plan.retransmit_blocks == []
    assert plan.block_edits == {}

    output = tmp_path / "flagonly_revised.mf4"
    summary = build_revised_volume(manifest, plan, path, output)
    assert summary["retransmitted_blocks"] == []
    assert summary["channel_flag_updates"][0]["flags"] == 0b10

    reread = mdfreader.Mdf(str(output))
    mask = np.ma.getmaskarray(reread.get_channel_data("Sig"))
    assert mask.tolist() == [index != 0 for index in range(n)]


def _header(block_id, length, link_count):
    return struct.pack("<4sI2Q", block_id, 0, length, link_count)


def _tx_block(text):
    payload = text.encode("utf-8") + b"\x00"
    return _header(b"##TX", 24 + len(payload), 0) + payload


def _cn_block(links, cn_type, sync, data_type, byte_offset, bit_count,
              flags, invalid_bit):
    out = _header(b"##CN", 160, 8) + struct.pack("<8Q", *links)
    out += struct.pack(
        "<4B4IBcH6d",
        cn_type, sync, data_type, 0,
        byte_offset, bit_count, flags, invalid_bit,
        0, b"\x00", 0,
        0, 0, 0, 0, 0, 0,
    )
    return out


def _build_unsorted_volume(path, cycle_count):
    """Two channel groups interleaved by record id inside one shared DT block.

    CG1 (rid 1): u8 master Time + u4 SigA, one invalidation byte per record.
    CG2 (rid 2): u4 SigB, one invalidation byte per record.
    Every invalidation byte starts at 0xFF (all channels wholesale invalid).
    """
    n = cycle_count
    payload = bytearray()
    stride1 = 1 + 8 + 4 + 1  # rid + master + SigA + invalidation
    stride2 = 1 + 4 + 1      # rid + SigB + invalidation
    for i in range(n):
        payload += b"\x01" + struct.pack("<QI", i, 100 + i) + b"\xff"
    for i in range(n):
        payload += b"\x02" + struct.pack("<I", 200 + i) + b"\xff"

    hd_p, fh_p = 64, 168
    dg1_p = fh_p + 56
    cg1_p = dg1_p + 64
    cg2_p = cg1_p + 104
    cn_master_p = cg2_p + 104
    tx_master_p = cn_master_p + 160
    cn_a_p = tx_master_p + 32
    tx_a_p = cn_a_p + 160
    cn_b_p = tx_a_p + 32
    tx_b_p = cn_b_p + 160
    dt_p = (tx_b_p + 32 + 7) & ~7

    with open(path, "wb") as fh:
        fh.write(struct.pack("<8s8s8s4sH30s2H", b"MDF     ", b"4.11    ",
                             b"MDFreadr", b"\x00" * 4, 411, b"\x00" * 30, 0, 0))
        fh.seek(hd_p)
        fh.write(_header(b"##HD", 104, 6)
                 + struct.pack("<6Q", dg1_p, fh_p, 0, 0, 0, 0)
                 + struct.pack("<q2h4B2d", 0, 0, 0, 1, 0, 0, 0, 0, 0))
        fh.seek(fh_p)
        fh.write(_header(b"##FH", 56, 2) + struct.pack("<2Q", 0, 0)
                 + b"\x00" * 16)
        fh.seek(dg1_p)
        fh.write(_header(b"##DG", 64, 4)
                 + struct.pack("<4Q", 0, cg1_p, dt_p, 0)
                 + struct.pack("<B7s", 1, b"\x00" * 7))
        fh.seek(cg1_p)
        fh.write(_header(b"##CG", 104, 6)
                 + struct.pack("<6Q", cg2_p, cn_master_p, 0, 0, 0, 0)
                 + struct.pack("<2Q2H3I", 1, n, 0, 0, 0, 12, 1))
        fh.seek(cg2_p)
        fh.write(_header(b"##CG", 104, 6)
                 + struct.pack("<6Q", 0, cn_b_p, 0, 0, 0, 0)
                 + struct.pack("<2Q2H3I", 2, n, 0, 0, 0, 4, 1))
        fh.seek(cn_master_p)
        fh.write(_cn_block((cn_a_p, 0, tx_master_p, 0, 0, 0, 0, 0),
                           2, 1, 0, 0, 64, 0, 0))
        fh.seek(tx_master_p); fh.write(_tx_block("Time"))
        # ALL_INVALID flag (bit 0) before revision; bit 0 is the candidate.
        fh.seek(cn_a_p)
        fh.write(_cn_block((0, 0, tx_a_p, 0, 0, 0, 0, 0),
                           0, 0, 0, 8, 32, 0b01, 0))
        fh.seek(tx_a_p); fh.write(_tx_block("SigA"))
        fh.seek(cn_b_p)
        fh.write(_cn_block((0, 0, tx_b_p, 0, 0, 0, 0, 0),
                           0, 0, 0, 0, 32, 0b01, 0))
        fh.seek(tx_b_p); fh.write(_tx_block("SigB"))
        fh.seek(dt_p)
        fh.write(_header(b"##DT", 24 + len(payload), 0) + bytes(payload))

    layout = {
        "dt_pointer": dt_p,
        "cn_a_pointer": cn_a_p,
        "cn_b_pointer": cn_b_p,
        "stride1": stride1,
        "stride2": stride2,
        "n": n,
    }
    return layout


def test_unsorted_mixed_group_shared_bit_rereads_independently(tmp_path):
    n = 5
    path = tmp_path / "unsorted.mf4"
    layout = _build_unsorted_volume(str(path), n)
    dt_pointer = layout["dt_pointer"]
    stride1, stride2 = layout["stride1"], layout["stride2"]

    # Invalidation byte offset relative to the DT payload (the rewriter adds
    # the 24 byte block header itself).
    def offset_cg1(index):
        return index * stride1 + (stride1 - 1)

    def offset_cg2(index):
        cg1_bytes = n * stride1
        return cg1_bytes + index * stride2 + (stride2 - 1)

    def channel(channel_name, cn_pointer, data_group, channel_group, offset_fn):
        return {
            "channel_name": channel_name,
            "cn_pointer": cn_pointer,
            "data_group": data_group,
            "channel_group": channel_group,
            "current_bit_position": 0,
            "current_flags": 0b01,
            "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
            "samples": [
                {
                    "sample_index": index,
                    "block_id": "DT1",
                    "block_pointer": dt_pointer,
                    "block_role": "data",
                    "invalid_byte_offset": offset_fn(index),
                    "approved": index == 0,
                    "candidate_bits": [{"bit_position": 0, "original_value": 1}],
                }
                for index in range(n)
            ],
        }

    manifest = parse_manifest({
        "block_order": ["DT1"],
        "groups": [
            {
                "group_id": "G1", "layout": "interleaved",
                "data_group": 0, "channel_group": 0,
                "record_id": 1, "record_id_size": 1,
                "record_length": stride1, "invalid_bytes": 1,
                "channels": [channel("SigA", layout["cn_a_pointer"], 0, 0, offset_cg1)],
            },
            {
                "group_id": "G2", "layout": "interleaved",
                "data_group": 0, "channel_group": 1,
                "record_id": 2, "record_id_size": 1,
                "record_length": stride2, "invalid_bytes": 1,
                "channels": [channel("SigB", layout["cn_b_pointer"], 0, 1, offset_cg2)],
            },
        ],
    })
    plan = plan_revision(manifest)
    # Both groups edit the same shared DT block: billed exactly once.
    assert plan.retransmit_blocks == ["DT1"]
    assert plan.retransmit_count == 1

    output = tmp_path / "unsorted_revised.mf4"
    build_revised_volume(manifest, plan, path, output)

    reread = mdfreader.Mdf(str(output))
    mask_a = np.ma.getmaskarray(reread.get_channel_data("SigA"))
    mask_b = np.ma.getmaskarray(reread.get_channel_data("SigB"))
    expected = [False] + [True] * (n - 1)
    assert mask_a.tolist() == expected
    assert mask_b.tolist() == expected
    # Signal values and master are untouched.
    assert np.array_equal(np.asarray(reread.get_channel_data("SigA")),
                          np.arange(100, 100 + n))
    assert np.array_equal(np.asarray(reread.get_channel_data("SigB")),
                          np.arange(200, 200 + n))
    assert np.array_equal(reread.get_channel_data("Time"), np.arange(n))


def _build_single_group_shared_byte_volume(path, cycle_count):
    """One sorted CG: master u8 + two u4 signals + one invalidation byte.

    Both signal CN blocks point at invalidation bit 0 of the same trailing
    invalidation byte, so the two channels genuinely share one physical bit.
    Every invalidation byte starts at 0xFF (wholesale invalid).
    """
    n = cycle_count
    stride = 8 + 4 + 4 + 1  # master + SigA + SigB + invalidation byte
    payload = bytearray()
    for i in range(n):
        payload += struct.pack("<QII", i, 100 + i, 200 + i) + b"\xff"

    hd_p, fh_p = 64, 168
    dg_p = fh_p + 56
    cg_p = dg_p + 64
    cn_master_p = cg_p + 104
    tx_master_p = cn_master_p + 160
    cn_a_p = tx_master_p + 32
    tx_a_p = cn_a_p + 160
    cn_b_p = tx_a_p + 32
    tx_b_p = cn_b_p + 160
    dt_p = (tx_b_p + 32 + 7) & ~7

    with open(path, "wb") as fh:
        fh.write(struct.pack("<8s8s8s4sH30s2H", b"MDF     ", b"4.11    ",
                             b"MDFreadr", b"\x00" * 4, 411, b"\x00" * 30, 0, 0))
        fh.seek(hd_p)
        fh.write(_header(b"##HD", 104, 6)
                 + struct.pack("<6Q", dg_p, fh_p, 0, 0, 0, 0)
                 + struct.pack("<q2h4B2d", 0, 0, 0, 1, 0, 0, 0, 0, 0))
        fh.seek(fh_p)
        fh.write(_header(b"##FH", 56, 2) + struct.pack("<2Q", 0, 0) + b"\x00" * 16)
        fh.seek(dg_p)
        fh.write(_header(b"##DG", 64, 4)
                 + struct.pack("<4Q", 0, cg_p, dt_p, 0)
                 + struct.pack("<B7s", 0, b"\x00" * 7))
        fh.seek(cg_p)
        fh.write(_header(b"##CG", 104, 6)
                 + struct.pack("<6Q", 0, cn_master_p, 0, 0, 0, 0)
                 + struct.pack("<2Q2H3I", 0, n, 0, 0, 0, 16, 1))
        fh.seek(cn_master_p)
        fh.write(_cn_block((cn_a_p, 0, tx_master_p, 0, 0, 0, 0, 0),
                           2, 1, 0, 0, 64, 0, 0))
        fh.seek(tx_master_p); fh.write(_tx_block("Time"))
        # Both signals declare invalidation bit 0 of the shared byte.
        fh.seek(cn_a_p)
        fh.write(_cn_block((cn_b_p, 0, tx_a_p, 0, 0, 0, 0, 0),
                           0, 0, 0, 8, 32, 0b10, 0))
        fh.seek(tx_a_p); fh.write(_tx_block("SigA"))
        fh.seek(cn_b_p)
        fh.write(_cn_block((0, 0, tx_b_p, 0, 0, 0, 0, 0),
                           0, 0, 0, 12, 32, 0b10, 0))
        fh.seek(tx_b_p); fh.write(_tx_block("SigB"))
        fh.seek(dt_p)
        fh.write(_header(b"##DT", 24 + len(payload), 0) + bytes(payload))

    return {"dt_pointer": dt_p, "cn_a_pointer": cn_a_p,
            "cn_b_pointer": cn_b_p, "stride": stride, "n": n}


def test_same_group_channels_share_one_invalidation_bit(tmp_path):
    n = 6
    path = tmp_path / "shared.mf4"
    layout = _build_single_group_shared_byte_volume(str(path), n)
    stride = layout["stride"]

    def channel(name, cn_pointer):
        return {
            "channel_name": name,
            "cn_pointer": cn_pointer,
            "data_group": 0,
            "channel_group": 0,
            "current_bit_position": 0,
            "current_flags": 0b10,
            "candidate_bits": [{"bit_position": 0, "occupied_by": []}],
            "samples": [
                {
                    "sample_index": index,
                    "block_id": "DT1",
                    "block_pointer": layout["dt_pointer"],
                    "block_role": "data",
                    "invalid_byte_offset": index * stride + (stride - 1),
                    "approved": index == 0,
                    "candidate_bits": [{"bit_position": 0, "original_value": 1}],
                }
                for index in range(n)
            ],
        }

    manifest = parse_manifest({
        "block_order": ["DT1"],
        "groups": [{
            "group_id": "G1", "layout": "interleaved",
            "data_group": 0, "channel_group": 0,
            "record_id": 0, "record_id_size": 0,
            "record_length": stride, "invalid_bytes": 1,
            "channels": [
                channel("SigA", layout["cn_a_pointer"]),
                channel("SigB", layout["cn_b_pointer"]),
            ],
        }],
    })
    plan = plan_revision(manifest)
    assert plan.retransmit_blocks == ["DT1"]
    # Both channels share the exact byte/bit: the approved first record needs a
    # single 1 -> 0 edit (original byte is 0xFF), deduplicated across channels.
    dt_edits = plan.block_edits["DT1"]
    assert len(dt_edits) == 1
    assert dt_edits[0].byte_offset == stride - 1
    assert dt_edits[0].target_value == 0

    output = tmp_path / "shared_revised.mf4"
    build_revised_volume(manifest, plan, path, output)
    reread = mdfreader.Mdf(str(output))
    expected = [False] + [True] * (n - 1)
    assert np.ma.getmaskarray(reread.get_channel_data("SigA")).tolist() == expected
    assert np.ma.getmaskarray(reread.get_channel_data("SigB")).tolist() == expected
