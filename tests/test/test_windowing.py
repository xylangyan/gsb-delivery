# ruff: noqa: ANN001, ANN201

import struct
from io import BytesIO

import lzf
import numpy as np
import pytest

from pypcd4 import Encoding, PointCloud, WindowCredentialError, open_window_credential
from pypcd4.windowing import WindowField


# ---------------------------------------------------------------------------
# Frame construction helpers
# ---------------------------------------------------------------------------


def write_frame(path, fields, sizes, types, counts, width, height, component_arrays, encoding):
    header = (
        "VERSION 0.7\n"
        f"FIELDS {' '.join(fields)}\n"
        f"SIZE {' '.join(map(str, sizes))}\n"
        f"TYPE {' '.join(types)}\n"
        f"COUNT {' '.join(map(str, counts))}\n"
        f"WIDTH {width}\nHEIGHT {height}\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {width * height}\nDATA {encoding.value}\n"
    )
    component_arrays = [
        np.frombuffer(component, dtype=np.uint8).copy()
        if isinstance(component, (bytes, bytearray))
        else component
        for component in component_arrays
    ]
    with open(path, "wb") as fp:
        fp.write(header.encode())
        if encoding == Encoding.BINARY:
            # binary stores point-interleaved records, not plane-major blocks.
            dtype = np.dtype(
                [(f"c{index}", array.dtype) for index, array in enumerate(component_arrays)]
            )
            payload = np.rec.fromarrays(component_arrays, dtype=dtype).tobytes()
            fp.write(payload)
        else:
            payload = b"".join(array.tobytes() for array in component_arrays)
            compressed = lzf.compress(payload)
            if compressed is None:  # pragma: no cover - frames below are compressible
                raise AssertionError("test frame is incompressible; use lower-entropy data")
            fp.write(struct.pack("II", len(compressed), len(payload)))
            fp.write(compressed)


def make_mixed_frame(path, width, height, encoding):
    """4 header fields, two of which are placeholder ``_``, one COUNT=3.

    Physical planes in order: x, _c0, _c1, _c2, intensity, _(float).  Data is
    kept low-entropy (small integer values) so the LZF writer always emits a
    real compressed block.
    """

    n = width * height
    x = np.array([i % 5 for i in range(n)], dtype="<f4")
    # Two distinct NaN payloads must survive as different raw words.
    x[1] = np.frombuffer(struct.pack("<I", 0x7FC00001), "<f4")[0]
    x[2] = np.frombuffer(struct.pack("<I", 0x7F800001), "<f4")[0]

    tri = np.stack(
        [
            np.array([(i + offset) % 31 for i in range(n)], dtype="<i2")
            for offset in range(3)
        ]
    )
    intensity = np.array([i % 256 for i in range(n)], dtype=np.uint8)
    tail = np.array([(i % 7) - 3 for i in range(n)], dtype="<f4")

    component_arrays = (
        [x]
        + [tri[k] for k in range(3)]
        + [intensity, tail]
    )
    write_frame(
        path,
        ["x", "_", "intensity", "_"],
        [4, 2, 1, 4],
        ["F", "I", "U", "F"],
        [1, 3, 1, 1],
        width,
        height,
        component_arrays,
        encoding,
    )
    return component_arrays


@pytest.fixture(
    params=[Encoding.BINARY, Encoding.BINARY_COMPRESSED],
    ids=["binary", "binary_compressed"],
)
def mixed_frame(tmp_path, request):
    path = tmp_path / f"mixed_{request.param.value}.pcd"
    make_mixed_frame(path, width=6, height=3, encoding=request.param)
    return path, request.param


# ---------------------------------------------------------------------------
# Credential construction and addressing
# ---------------------------------------------------------------------------


def test_credential_rejects_ascii_and_unorganized(tmp_path):
    ascii_path = tmp_path / "a.pcd"
    ascii_path.write_bytes(
        b"VERSION 0.7\nFIELDS x\nSIZE 4\nTYPE F\nCOUNT 1\nWIDTH 2\nHEIGHT 1\n"
        b"VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 2\nDATA ascii\n1.0\n2.0\n"
    )
    with pytest.raises(WindowCredentialError):
        open_window_credential(ascii_path)

    # Header declares more points than the WIDTH * HEIGHT grid can hold.
    broken = tmp_path / "b.pcd"
    broken.write_bytes(
        b"VERSION 0.7\nFIELDS x\nSIZE 4\nTYPE F\nCOUNT 1\nWIDTH 2\nHEIGHT 2\n"
        b"VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 5\nDATA binary\n" + b"\0" * 20
    )
    with pytest.raises(WindowCredentialError):
        open_window_credential(broken)


def test_credential_keeps_original_field_positions(mixed_frame):
    path, encoding = mixed_frame
    credential = open_window_credential(path)

    assert credential.encoding == encoding
    # Placeholder names are preserved verbatim, never replaced by random ids.
    assert credential.field_names == ("x", "_", "intensity", "_")
    assert all(not name.startswith("#$%&~~") for name in credential.field_names)

    # COUNT=3 materialises three physical component planes.
    assert len(credential.planes) == 6
    triple = [p for p in credential.planes if p.field == 1]
    assert [(p.component, p.item_size) for p in triple] == [(0, 2), (1, 2), (2, 2)]
    # Plane-major blocks are contiguous and each hold one word per point.
    for previous, current in zip(triple, triple[1:]):
        assert current.block_offset == previous.block_offset + previous.byte_size


def test_underscore_placeholders_are_independently_addressable(mixed_frame):
    path, _ = mixed_frame
    pc = PointCloud.from_path(path)
    credential = open_window_credential(path)
    names = pc.pc_data.dtype.names

    first_underscore = WindowField(field=1, component=2)
    second_underscore = WindowField(field=3, component=0)

    left = credential.read_point(*first_underscore, row=2, column=3)
    right = credential.read_point(*second_underscore, row=2, column=3)

    point_index = 2 * 6 + 3
    # The two "_" header fields resolve to different physical planes.
    assert left.raw == pc.pc_data[names[3]][point_index].tobytes()
    assert right.raw == pc.pc_data[names[5]][point_index].tobytes()
    assert left.raw != right.raw


def test_point_numbering_uses_width_and_height(mixed_frame):
    path, _ = mixed_frame
    credential = open_window_credential(path)

    cell = credential.read_point(0, 0, row=2, column=4)
    assert cell.row == 2
    assert cell.column == 4
    assert cell.point_index == 2 * 6 + 4
    assert cell.width == 6
    assert cell.height == 3


# ---------------------------------------------------------------------------
# Value interpretation
# ---------------------------------------------------------------------------


def test_signed_unsigned_and_float_interpretation(tmp_path):
    width, height = 4, 1
    n = width * height
    floats = np.array([1.5, -2.25, float("inf"), 0.0], dtype="<f4")
    ints = np.array([-1, -128, 127, 1], dtype=np.int8)
    uints = np.array([255, 0, 128, 1], dtype=np.uint8)
    path = tmp_path / "types.pcd"
    write_frame(
        path,
        ["f", "i", "u"],
        [4, 1, 1],
        ["F", "I", "U"],
        [1, 1, 1],
        width,
        height,
        [floats, ints, uints],
        Encoding.BINARY,
    )
    credential = open_window_credential(path)

    signed_cell = credential.read_point(1, 0, 0, 0)
    assert signed_cell.raw == b"\xff"
    assert signed_cell.signed == -1
    assert signed_cell.unsigned == 255

    unsigned_cell = credential.read_point(2, 0, 0, 2)
    assert unsigned_cell.raw == b"\x80"
    assert unsigned_cell.unsigned == 128
    assert unsigned_cell.signed == -128

    float_cell = credential.read_point(0, 0, 0, 0)
    assert float_cell.floating == pytest.approx(1.5)
    assert float_cell.raw == struct.pack("<f", 1.5)


def test_distinct_nan_bit_patterns_keep_original_words(mixed_frame):
    path, _ = mixed_frame
    credential = open_window_credential(path)

    nan_a = credential.read_point(0, 0, 0, 1)
    nan_b = credential.read_point(0, 0, 0, 2)

    assert nan_a.raw == struct.pack("<I", 0x7FC00001)
    assert nan_b.raw == struct.pack("<I", 0x7F800001)
    assert nan_a.raw != nan_b.raw
    assert np.isnan(nan_a.floating)
    assert np.isnan(nan_b.floating)


# ---------------------------------------------------------------------------
# Parity with the full-frame entry point
# ---------------------------------------------------------------------------


def test_every_cell_matches_full_frame_loader(mixed_frame):
    path, _ = mixed_frame
    pc = PointCloud.from_path(path)
    credential = open_window_credential(path)
    names = pc.pc_data.dtype.names

    for plane_index, plane in enumerate(credential.planes):
        for row in range(3):
            for column in range(6):
                cell = credential.read_point(plane.field, plane.component, row, column)
                expected = pc.pc_data[names[plane_index]][cell.point_index].tobytes()
                assert cell.raw == expected


def test_window_block_layout_and_provenance(mixed_frame):
    path, _ = mixed_frame
    pc = PointCloud.from_path(path)
    credential = open_window_credential(path)
    name = pc.pc_data.dtype.names[1]  # first component of the COUNT=3 field

    block = credential.read_window(1, 0, row_start=1, row_end=3, column_start=2, column_end=5)

    assert block.row_range == (1, 3)
    assert block.column_range == (2, 5)
    assert block.values().shape == (2, 3)
    assert len(block.cells) == 6

    for cell in block.cells:
        assert cell.field == 1
        assert cell.component == 0
        assert cell.point_index == cell.row * 6 + cell.column
        assert cell.raw == pc.pc_data[name][cell.point_index].tobytes()

    provenance = block.provenance()
    assert provenance["field_index"] == 1
    assert provenance["field_name"] == "_"
    assert provenance["component"] == 0
    assert provenance["item_size"] == 2
    assert provenance["header"]["data_offset"] == credential.data_offset
    assert provenance["header"]["width"] == 6
    assert provenance["point_range"] == (block.cells[0].point_index, block.cells[-1].point_index)
    assert provenance["first_raw"] == block.cells[0].raw.hex()

    describe = credential.describe()
    assert describe["width"] == 6
    assert describe["height"] == 3
    assert describe["verified"] is True
    assert describe["planes"][0]["block_offset"] == 0


def test_invalid_rectangle_and_component_are_rejected(mixed_frame):
    path, _ = mixed_frame
    credential = open_window_credential(path)

    with pytest.raises(ValueError):
        credential.read_window(0, 0, 0, 4, 0, 1)  # row end beyond HEIGHT
    with pytest.raises(ValueError):
        credential.read_window(0, 0, 0, 1, 3, 9)  # column end beyond WIDTH
    with pytest.raises(ValueError):
        credential.read_window(9, 0, 0, 1, 0, 1)  # unknown field position
    with pytest.raises(ValueError):
        credential.read_window(1, 5, 0, 1, 0, 1)  # component beyond COUNT=3


# ---------------------------------------------------------------------------
# LZF cross-plane / mid-copy boundary behaviour
# ---------------------------------------------------------------------------


def test_back_references_crossing_plane_boundary(tmp_path):
    width, height = 64, 4
    n = width * height
    item_size = 4
    plane_size = n * item_size
    plane_a = (bytes(range(256)) * (plane_size // 256 + 1))[:plane_size]
    # plane b opens by copying 512 bytes straight from plane a's material.
    plane_b = plane_a[:512] + bytes([0xAB]) * (plane_size - 512)
    plane_c = bytes([(i * 13) & 0xFF for i in range(plane_size)])

    payload = plane_a + plane_b + plane_c
    compressed = lzf.compress(payload)
    boundary = len(plane_a)

    # Sanity check: the compressor really emitted a cross-boundary copy.
    from pypcd4.windowing import _parse_lzf

    ops = _parse_lzf(compressed, len(payload))
    assert any(
        (not bool(op["literal"])) and int(op["ref"]) < boundary and int(op["out_end"]) > boundary
        for op in ops
    )

    path = tmp_path / "cross.pcd"
    write_frame(
        path,
        ["a", "b", "c"],
        [4, 4, 4],
        ["F", "F", "F"],
        [1, 1, 1],
        width,
        height,
        [plane_a, plane_b, plane_c],
        Encoding.BINARY_COMPRESSED,
    )
    pc = PointCloud.from_path(path)
    credential = open_window_credential(path)
    names = pc.pc_data.dtype.names

    # First point of plane b sits exactly on the boundary; point 25's word is
    # in the middle of the copied run.
    first = credential.read_point(1, 0, 0, 0)
    middle_index = 25
    middle = credential.read_point(1, 0, middle_index // width, middle_index % width)
    assert first.raw == plane_b[:4] == pc.pc_data[names[1]][0].tobytes()
    assert (
        middle.raw
        == plane_b[middle_index * item_size : middle_index * item_size + 4]
        == pc.pc_data[names[1]][middle_index].tobytes()
    )

    for row in range(height):
        for column in range(width):
            cell = credential.read_point(1, 0, row, column)
            assert cell.raw == pc.pc_data[names[1]][cell.point_index].tobytes()


# ---------------------------------------------------------------------------
# Re-openability and byte-budget guarantees
# ---------------------------------------------------------------------------


class CountingReader(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0
        self.read_calls = 0

    def read(self, size=-1):
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        self.read_calls += 1
        return chunk


def _build_big_frame(path, width, height):
    n = width * height
    # Periodic, low-entropy planes so LZF always emits a real block.
    planes = [
        np.array([(i % 13) + plane_index for i in range(n)], dtype="<f4")
        for plane_index in range(4)
    ]
    write_frame(
        path,
        ["a", "b", "c", "d"],
        [4, 4, 4, 4],
        ["F", "F", "F", "F"],
        [1, 1, 1, 1],
        width,
        height,
        planes,
        Encoding.BINARY_COMPRESSED,
    )
    return b"".join(plane.tobytes() for plane in planes)


def test_path_credential_reopens_and_keeps_no_full_copy(tmp_path):
    small = tmp_path / "small.pcd"
    make_mixed_frame(small, 4, 2, Encoding.BINARY_COMPRESSED)

    credential = open_window_credential(small)
    assert credential._path is not None
    assert credential._external_fp is None

    first = credential.read_point(0, 0, 0, 0)
    second = credential.read_point(0, 0, 0, 3)
    assert first.raw != second.raw

    # Only the compact LZF bytecode is retained, never a decompressed frame.
    assert not any(
        isinstance(value, (bytes, bytearray)) and len(value) > 8192
        for value in vars(credential).values()
    )


def test_file_object_credential_does_not_take_ownership(mixed_frame):
    path, _ = mixed_frame
    with open(path, "rb") as fp:
        credential = open_window_credential(fp)
        assert credential._external_fp is fp
        cell = credential.read_point(0, 0, 0, 0)
        assert cell.raw
    # The caller's handle is still closed normally by the context manager.
    assert fp.closed


def test_narrow_query_byte_budget_is_independent_of_frame_size(tmp_path):
    # Adding more rows grows the frame but the window stays at the first row,
    # so the source bytes touched must remain (nearly) constant.
    reader_sizes = []
    for height in (50, 200):
        path = tmp_path / f"big_{height}.pcd"
        _build_big_frame(path, width=500, height=height)

        reader = CountingReader(path.read_bytes())
        credential = open_window_credential(reader)
        reader.bytes_read = 0
        reader.read_calls = 0

        block = credential.read_window(0, 0, 0, 1, 10, 14)
        assert len(block.cells) == 4
        reader_sizes.append(reader.bytes_read)

    assert reader_sizes[0] > 0
    # The same narrow window at the start of the plane costs the same even
    # though the second frame is four times larger.
    assert abs(reader_sizes[1] - reader_sizes[0]) <= 64


def test_binary_narrow_query_reads_only_cell_bytes(tmp_path):
    path = tmp_path / "binary.pcd"
    make_mixed_frame(path, 6, 3, Encoding.BINARY)
    reader = CountingReader(path.read_bytes())
    credential = open_window_credential(reader)
    reader.bytes_read = 0
    reader.read_calls = 0

    block = credential.read_window(2, 0, 1, 2, 0, 3)  # uint8 intensity plane
    assert len(block.cells) == 3
    # One one-byte read per cell, nothing from neighbouring fields or rows.
    assert reader.bytes_read == 3
    assert reader.read_calls == 3


def test_compressed_query_does_not_read_later_planes(tmp_path):
    path = tmp_path / "big.pcd"
    payload = _build_big_frame(path, width=500, height=100)
    plane_zero_size = 500 * 100 * 4

    reader = CountingReader(path.read_bytes())
    credential = open_window_credential(reader)
    reader.bytes_read = 0
    reader.read_calls = 0

    block = credential.read_window(0, 0, 0, 1, 0, 4)
    assert len(block.cells) == 4

    target_end = block.cells[-1].point_index * 4 + 4
    assert target_end <= plane_zero_size

    # The implementation reads exactly the compressed prefix covering output
    # up to the window, and that prefix ends before plane zero is finished.
    needed_stream_end = 0
    last_output_start = 0
    for op in credential._lzf_ops:
        if int(op["out_start"]) >= target_end:
            break
        needed_stream_end = int(op["in_end"])
        last_output_start = int(op["out_start"])

    assert reader.bytes_read == needed_stream_end
    assert last_output_start < plane_zero_size
    assert reader.bytes_read < len(payload)


# ---------------------------------------------------------------------------
# On-disk layout wins over the requested save encoding
# ---------------------------------------------------------------------------


def test_compressed_save_fallback_to_binary_is_read_by_real_layout(tmp_path):
    path = tmp_path / "fallback.pcd"
    pc = PointCloud.from_xyz_points(np.random.rand(1, 3).astype(np.float32))
    pc.save(path, Encoding.BINARY_COMPRESSED)

    # The writer fell back to plain binary on disk.
    reloaded = PointCloud.from_path(path)
    assert reloaded.metadata.data == Encoding.BINARY

    credential = open_window_credential(path)
    assert credential.encoding == Encoding.BINARY
    for field in range(3):
        cell = credential.read_point(field, 0, 0, 0)
        assert cell.raw == reloaded.pc_data[reloaded.pc_data.dtype.names[field]][0].tobytes()


# ---------------------------------------------------------------------------
# Corrupt frames must never become reusable credentials
# ---------------------------------------------------------------------------


def test_truncated_compressed_block_is_rejected(tmp_path):
    path = tmp_path / "good.pcd"
    _build_big_frame(path, width=64, height=2)
    raw = path.read_bytes()
    data_line = raw.index(b"\n", raw.index(b"DATA")) + 1
    compressed_size, _ = struct.unpack("II", bytes(raw[data_line : data_line + 8]))

    # Keep the size header but only half of the compressed block.
    truncated = tmp_path / "truncated.pcd"
    truncated.write_bytes(raw[: data_line + 8 + compressed_size // 2])

    with pytest.raises(WindowCredentialError):
        open_window_credential(truncated)


def test_decompressed_length_mismatch_is_rejected(tmp_path):
    path = tmp_path / "good.pcd"
    _build_big_frame(path, width=64, height=2)
    raw = bytearray(path.read_bytes())
    data_line = raw.index(b"\n", raw.index(b"DATA")) + 1
    compressed_size, uncompressed_size = struct.unpack("II", bytes(raw[data_line : data_line + 8]))

    struct.pack_into("II", raw, data_line, compressed_size, uncompressed_size + 64)
    lying = tmp_path / "lying.pcd"
    lying.write_bytes(bytes(raw))

    with pytest.raises(WindowCredentialError):
        open_window_credential(lying)


def test_stream_shorter_than_declared_size_is_rejected(tmp_path):
    payload = bytes([7]) * 200
    compressed = lzf.compress(payload)
    header = (
        "VERSION 0.7\nFIELDS x\nSIZE 4\nTYPE F\nCOUNT 1\nWIDTH 64\nHEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 64\nDATA binary_compressed\n"
    )
    path = tmp_path / "short.pcd"
    path.write_bytes(header.encode() + struct.pack("II", len(compressed), 256) + compressed)

    with pytest.raises(WindowCredentialError):
        open_window_credential(path)


def test_truncated_binary_frame_is_rejected(tmp_path):
    path = tmp_path / "binary.pcd"
    make_mixed_frame(path, 6, 3, Encoding.BINARY)
    raw = path.read_bytes()
    tmp_path.joinpath("binary_trunc.pcd").write_bytes(raw[: len(raw) - 40])

    with pytest.raises(WindowCredentialError):
        open_window_credential(tmp_path / "binary_trunc.pcd")
