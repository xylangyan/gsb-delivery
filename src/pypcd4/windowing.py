"""Re-openable window credentials for organized PCD frames.

The regular :class:`~pypcd4.pypcd4.PointCloud` entry point always decodes a
whole frame into memory.  Quality inspection only needs narrow pixel
rectangles of a single component plane, so this module builds a *credential*
for a frame once and then reopens the file per query, touching only the source
bytes that the requested window actually depends on.

Only organized grids stored as ``binary`` or ``binary_compressed`` are
supported.  Fields are always addressed by their position in the original
``FIELDS`` header line together with a component index, so placeholder fields
written as ``_`` stay independently addressable and the randomized internal
numpy field names never leak into a long-lived credential.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import IO, List, NamedTuple, Optional, Sequence, Tuple, Union, cast

import numpy as np
import numpy.typing as npt

from .pypcd4 import Encoding, MetaData, PCD_TYPE_TO_NUMPY_TYPE

PathLike = Union[str, Path]
SourceLike = Union[PathLike, BufferedReader]

# A back reference in LZF can reach at most (31 << 8) + 255 + 1 bytes back.
_LZF_MAX_OFFSET = 1 << 13


class WindowCredentialError(Exception):
    """Raised when a frame cannot be turned into a reusable window credential."""


class WindowField(NamedTuple):
    """Stable address of one physical component plane.

    ``field`` is the zero-based position in the original ``FIELDS`` header
    line (placeholder ``_`` fields keep their own position) and ``component``
    is the zero-based component inside that field's ``COUNT``.
    """

    field: int
    component: int


class WindowCell(NamedTuple):
    """One decoded point of a window, with the original bytes preserved."""

    field: int
    component: int
    point_index: int
    row: int
    column: int
    width: int
    height: int
    raw: bytes
    signed: int
    unsigned: int
    floating: float


@dataclass(frozen=True)
class Plane:
    """Description of one physical component plane.

    The two on-disk layouts address a component differently:

    * ``binary_compressed`` stores every component as one contiguous
      plane-major block starting at ``block_offset``.
    * ``binary`` stores interleaved point records; a component lives at
      ``record_offset`` inside each record of size ``point_step``.
    """

    field: int
    component: int
    item_size: int
    numpy_type: object
    point_count: int
    block_offset: int
    record_offset: int
    point_step: int

    @property
    def byte_size(self) -> int:
        return self.item_size * self.point_count


@dataclass(frozen=True)
class WindowBlock:
    """A pixel rectangle read back through a credential.

    ``cells`` is row-major over the requested rectangle.  Every cell carries
    the provenance needed to audit the read: the header field position and its
    original name, the component, the actual point id derived from
    ``WIDTH``/``HEIGHT`` and the original component bytes.
    """

    field: int
    component: int
    row_range: Tuple[int, int]
    column_range: Tuple[int, int]
    width: int
    height: int
    cells: Tuple[WindowCell, ...]
    field_name: str = ""
    item_size: int = 0
    data_offset: int = 0

    def raw(self) -> bytes:
        return b"".join(cell.raw for cell in self.cells)

    def values(self) -> np.ndarray:
        rows = self.row_range[1] - self.row_range[0]
        cols = self.column_range[1] - self.column_range[0]
        return np.array([cell.floating for cell in self.cells], dtype=np.float64).reshape(
            rows, cols
        )

    def provenance(self) -> dict:
        first = self.cells[0]
        last = self.cells[-1]
        return {
            "header": {"data_offset": self.data_offset, "width": self.width, "height": self.height},
            "field_index": self.field,
            "field_name": self.field_name,
            "component": self.component,
            "item_size": self.item_size,
            "row_range": self.row_range,
            "column_range": self.column_range,
            "point_range": (first.point_index, last.point_index),
            "first_raw": first.raw.hex(),
            "last_raw": last.raw.hex(),
        }


# Reusable LZF bytecode, one row per opcode.  A literal copies in_end-in_start
# source bytes straight from the compressed stream; a copy materialises
# out_end-out_start bytes from already produced output starting at ref.
# The PCD size header is uint32, so every offset fits in uint32 and the whole
# table stays compact even for very large frames (no per-object overhead).
_LZF_OP_DTYPE = np.dtype(
    [
        ("literal", np.bool_),
        ("out_start", np.uint32),
        ("out_end", np.uint32),
        ("in_start", np.uint32),
        ("in_end", np.uint32),
        ("ref", np.int32),
    ]
)


class _RollingHistory:
    """Fixed-size window over the bytes produced so far.

    LZF references never look back more than ``_LZF_MAX_OFFSET`` bytes, so a
    ring buffer is enough to resolve every copy without retaining a full
    decompressed frame.
    """

    def __init__(self, capacity: int = _LZF_MAX_OFFSET) -> None:
        self._capacity = capacity
        self._buffer = bytearray(capacity)
        self._length = 0

    def append(self, data: bytes) -> None:
        view = memoryview(data)
        capacity = self._capacity
        while len(view) > 0:
            start = self._length % capacity
            take = min(capacity - start, len(view))
            self._buffer[start : start + take] = view[:take]
            view = view[take:]
            self._length += take

    def _at(self, absolute_index: int) -> int:
        if absolute_index < 0 or absolute_index >= self._length:
            raise WindowCredentialError("LZF back reference points outside produced output")
        if self._length - absolute_index > self._capacity:
            raise WindowCredentialError("LZF back reference reaches beyond the history window")
        return self._buffer[absolute_index % self._capacity]

    def copy(self, ref: int, length: int) -> bytes:
        """Materialise a back reference, allowing self-overlapping copies.

        LZF permits a copy to read bytes produced earlier in the same
        operation (this is how long constant runs compress), so each byte is
        resolved against the history and appended immediately.
        """

        if ref < 0 or ref >= self._length:
            raise WindowCredentialError("LZF back reference points outside produced output")

        produced = bytearray(length)
        for index in range(length):
            value = self._at(ref + index)
            produced[index] = value
            self.append(bytes((value,)))
        return bytes(produced)

    @property
    def position(self) -> int:
        return self._length


def _read_header(fp: IO[bytes]) -> Tuple[MetaData, Tuple[str, ...], int]:
    lines: List[str] = []
    raw_field_names: Tuple[str, ...] = ()
    data_offset = -1

    while True:
        line_start = fp.tell()
        raw_line = fp.readline()
        if not raw_line:
            raise WindowCredentialError("Unexpected end of file while reading PCD header")
        line = raw_line.decode("utf-8").strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
        if line.startswith("FIELDS"):
            raw_field_names = tuple(line.split()[1:])
        if line.startswith("DATA"):
            data_offset = fp.tell()
            break
        if fp.tell() == line_start:  # pragma: no cover - defensive, readline always advances
            raise WindowCredentialError("Stalled while reading PCD header")

    if data_offset < 0:
        raise WindowCredentialError("PCD header is missing a DATA line")

    metadata = MetaData.parse_header(lines)
    if len(raw_field_names) != len(metadata.fields):
        raise WindowCredentialError("Failed to recover original FIELDS ordering")
    return metadata, raw_field_names, data_offset


def _build_planes(metadata: MetaData) -> List[Plane]:
    point_step = sum(size * count for size, count in zip(metadata.size, metadata.count))

    planes: List[Plane] = []
    block_offset = 0
    record_offset = 0
    for field_index in range(len(metadata.fields)):
        count = metadata.count[field_index]
        item_size = metadata.size[field_index]
        numpy_type = PCD_TYPE_TO_NUMPY_TYPE[
            (metadata.type[field_index], metadata.size[field_index])
        ]
        for component in range(count):
            planes.append(
                Plane(
                    field=field_index,
                    component=component,
                    item_size=item_size,
                    numpy_type=numpy_type,
                    point_count=metadata.points,
                    block_offset=block_offset,
                    record_offset=record_offset,
                    point_step=point_step,
                )
            )
            block_offset += item_size * metadata.points
            record_offset += item_size
    return planes


def _parse_lzf(stream: bytes, expected_size: int) -> npt.NDArray:
    """Validate a whole LZF block once and record its reusable bytecode.

    The block is decoded sequentially with only a rolling history, so a full
    decompressed copy is never retained.  Any malformed opcode, dangling
    reference or size mismatch raises and no credential is produced.  The
    returned structured array holds one compact row per opcode.
    """

    records: List[tuple] = []
    history = _RollingHistory()
    in_pos = 0
    stream_len = len(stream)

    while in_pos < stream_len:
        ctrl = stream[in_pos]
        op_in_start = in_pos
        out_start = history.position
        in_pos += 1

        if ctrl < 32:  # literal run
            length = ctrl + 1
            payload_start = in_pos
            in_end = in_pos + length
            if in_end > stream_len:
                raise WindowCredentialError("Truncated LZF literal run")
            history.append(stream[payload_start:in_end])
            if history.position > expected_size:
                raise WindowCredentialError("Decompressed length exceeds the header declaration")
            records.append(
                (True, out_start, history.position, payload_start, in_end, -1)
            )
            in_pos = in_end
            continue

        length = ctrl >> 5
        ref = history.position - ((ctrl & 0x1F) << 8) - 1
        if length == 7:
            if in_pos >= stream_len:
                raise WindowCredentialError("Truncated LZF extended length")
            length += stream[in_pos]
            in_pos += 1
        if in_pos >= stream_len:
            raise WindowCredentialError("Truncated LZF back reference")
        ref -= stream[in_pos]
        in_pos += 1
        length += 2

        if ref < 0 or ref >= history.position:
            raise WindowCredentialError("LZF back reference points outside produced output")
        if history.position + length > expected_size:
            raise WindowCredentialError("Decompressed length exceeds the header declaration")

        history.copy(ref, length)
        records.append(
            (False, out_start, history.position, op_in_start, in_pos, ref)
        )

    if history.position != expected_size:
        raise WindowCredentialError(
            f"Decompressed length {history.position} does not match header {expected_size}"
        )
    return np.array(records, dtype=_LZF_OP_DTYPE)


def _interpret(raw: bytes) -> Tuple[int, int, float]:
    unsigned = int.from_bytes(raw, byteorder="little", signed=False)
    signed = int.from_bytes(raw, byteorder="little", signed=True)
    if len(raw) == 4:
        floating = struct.unpack("<f", raw)[0]
    elif len(raw) == 8:
        floating = struct.unpack("<d", raw)[0]
    else:
        floating = float("nan")
    return signed, unsigned, floating


class WindowCredential:
    """Reusable, re-openable handle for narrow window reads.

    Construct one with :func:`open_window_credential` (or
    :meth:`pypcd4.PointCloud.open_window_credential`).  The construction pass
    validates the frame and, for compressed frames, records the LZF bytecode
    plus plane layout.  No full decompressed copy and no open file handle are
    retained, so the same credential can serve many queries across repeated
    opens of the same path.
    """

    def __init__(
        self,
        source: SourceLike,
        metadata: MetaData,
        field_names: Tuple[str, ...],
        data_offset: int,
        planes: Sequence[Plane],
        lzf_ops: Optional[npt.NDArray],
        owns_path: bool,
    ) -> None:
        self._path: Optional[Path] = Path(source) if owns_path else None
        self._external_fp: Optional[IO[bytes]] = (
            None if owns_path else cast(IO[bytes], source)
        )
        self.metadata = metadata
        self.field_names = field_names
        self.data_offset = data_offset
        self.encoding = metadata.data
        self.verified = True
        self._planes = tuple(planes)
        self._lzf_ops: Optional[npt.NDArray] = lzf_ops
        self._plane_lookup = {
            (plane.field, plane.component): index for index, plane in enumerate(self._planes)
        }

    @property
    def planes(self) -> Tuple[Plane, ...]:
        return self._planes

    def describe(self) -> dict:
        return {
            "encoding": self.encoding.value,
            "data_offset": self.data_offset,
            "verified": self.verified,
            "width": self.metadata.width,
            "height": self.metadata.height,
            "points": self.metadata.points,
            "field_names": self.field_names,
            "planes": [
                {
                    "field": plane.field,
                    "component": plane.component,
                    "block_offset": plane.block_offset,
                    "record_offset": plane.record_offset,
                    "point_step": plane.point_step,
                    "item_size": plane.item_size,
                    "byte_size": plane.byte_size,
                }
                for plane in self._planes
            ],
        }

    def _open_fp(self) -> Tuple[IO[bytes], bool]:
        if self._path is not None:
            return self._path.open("rb"), True
        assert self._external_fp is not None
        return self._external_fp, False

    def _resolve_plane(self, field: int, component: int) -> Plane:
        if not 0 <= field < len(self.metadata.fields):
            raise ValueError(f"field index {field} is outside FIELDS {self.field_names}")
        if not 0 <= component < self.metadata.count[field]:
            raise ValueError(
                f"component {component} is outside COUNT {self.metadata.count[field]} "
                f"of field {field} ({self.field_names[field]!r})"
            )
        return self._planes[self._plane_lookup[(field, component)]]

    def _check_rectangle(
        self, row_start: int, row_end: int, column_start: int, column_end: int
    ) -> None:
        width = self.metadata.width
        height = self.metadata.height
        if not (0 <= row_start < row_end <= height):
            raise ValueError(f"row range [{row_start}, {row_end}) must fit HEIGHT {height}")
        if not (0 <= column_start < column_end <= width):
            raise ValueError(
                f"column range [{column_start}, {column_end}) must fit WIDTH {width}"
            )

    def read_window(
        self,
        field: int,
        component: int,
        row_start: int,
        row_end: int,
        column_start: int,
        column_end: int,
    ) -> WindowBlock:
        """Read a half-open pixel rectangle of one component plane.

        Rows and columns follow the organized grid (point id =
        ``row * WIDTH + column``).  Only the source bytes covering the
        requested intervals are read.
        """

        self._check_rectangle(row_start, row_end, column_start, column_end)
        plane = self._resolve_plane(field, component)
        width = self.metadata.width

        if self.encoding == Encoding.BINARY:
            cell_raw = self._read_binary_cells(
                plane, width, row_start, row_end, column_start, column_end
            )
        else:
            cell_raw = self._read_compressed_cells(
                plane, width, row_start, row_end, column_start, column_end
            )

        cells: List[WindowCell] = []
        index = 0
        for row in range(row_start, row_end):
            for column in range(column_start, column_end):
                raw = cell_raw[index]
                index += 1
                signed, unsigned, floating = _interpret(raw)
                cells.append(
                    WindowCell(
                        field=field,
                        component=component,
                        point_index=row * width + column,
                        row=row,
                        column=column,
                        width=width,
                        height=self.metadata.height,
                        raw=raw,
                        signed=signed,
                        unsigned=unsigned,
                        floating=floating,
                    )
                )

        return WindowBlock(
            field=field,
            component=component,
            row_range=(row_start, row_end),
            column_range=(column_start, column_end),
            width=width,
            height=self.metadata.height,
            cells=tuple(cells),
            field_name=self.field_names[field],
            item_size=plane.item_size,
            data_offset=self.data_offset,
        )

    def read_point(self, field: int, component: int, row: int, column: int) -> WindowCell:
        block = self.read_window(field, component, row, row + 1, column, column + 1)
        return block.cells[0]

    def _read_binary_cells(
        self,
        plane: Plane,
        width: int,
        row_start: int,
        row_end: int,
        column_start: int,
        column_end: int,
    ) -> List[bytes]:
        intervals: List[Tuple[int, int]] = []
        for row in range(row_start, row_end):
            for column in range(column_start, column_end):
                record = row * width + column
                start = record * plane.point_step + plane.record_offset
                intervals.append((start, start + plane.item_size))

        fp, opened = self._open_fp()
        try:
            chunks: List[bytes] = []
            for start, end in intervals:
                fp.seek(self.data_offset + start)
                chunk = fp.read(end - start)
                if len(chunk) != end - start:
                    raise WindowCredentialError("Binary frame ended inside a requested window")
                chunks.append(chunk)
            return chunks
        finally:
            if opened:
                fp.close()

    def _read_compressed_cells(
        self,
        plane: Plane,
        width: int,
        row_start: int,
        row_end: int,
        column_start: int,
        column_end: int,
    ) -> List[bytes]:
        intervals: List[Tuple[int, int]] = []
        for row in range(row_start, row_end):
            start = plane.block_offset + (row * width + column_start) * plane.item_size
            end = plane.block_offset + (row * width + column_end) * plane.item_size
            intervals.append((start, end))

        row_chunks = self._read_compressed_intervals(intervals)

        cell_raw: List[bytes] = []
        for chunk in row_chunks:
            for offset in range(0, len(chunk), plane.item_size):
                cell_raw.append(bytes(chunk[offset : offset + plane.item_size]))
        return cell_raw

    def _read_compressed_intervals(
        self, intervals: Sequence[Tuple[int, int]]
    ) -> List[bytes]:
        assert self._lzf_ops is not None
        target_end = intervals[-1][1]

        needed_stream_end = 0
        for op in self._lzf_ops:
            if int(op["out_start"]) >= target_end:
                break
            needed_stream_end = int(op["in_end"])

        fp, opened = self._open_fp()
        try:
            fp.seek(self.data_offset + 8)
            stream = fp.read(needed_stream_end)
        finally:
            if opened:
                fp.close()
        if len(stream) != needed_stream_end:
            raise WindowCredentialError("Compressed block is shorter than its header declaration")

        return self._extract_intervals(stream, intervals)

    def _extract_intervals(
        self, stream: bytes, intervals: Sequence[Tuple[int, int]]
    ) -> List[bytes]:
        history = _RollingHistory()
        sinks = [bytearray(end - start) for start, end in intervals]
        interval_idx = 0

        for op in self._lzf_ops:
            if interval_idx >= len(intervals):
                break

            is_literal = bool(op["literal"])
            out_start = int(op["out_start"])
            out_end = int(op["out_end"])

            if is_literal:
                chunk = stream[int(op["in_start"]) : int(op["in_end"])]
            else:
                # copy() appends the materialised bytes to the history itself,
                # including self-overlapping (RLE-style) back references.
                chunk = history.copy(int(op["ref"]), out_end - out_start)

            while interval_idx < len(intervals) and intervals[interval_idx][0] < out_end:
                iv_start, iv_end = intervals[interval_idx]
                if iv_end <= out_start:
                    interval_idx += 1
                    continue

                ov_start = max(out_start, iv_start)
                ov_end = min(out_end, iv_end)
                sink_offset = ov_start - iv_start
                overlap = chunk[ov_start - out_start : ov_end - out_start]
                sinks[interval_idx][sink_offset : sink_offset + len(overlap)] = overlap

                if iv_end <= out_end:
                    interval_idx += 1
                else:
                    break

            if is_literal:
                history.append(chunk)

        results: List[bytes] = []
        for (start, end), sink in zip(intervals, sinks):
            if len(sink) != end - start:
                raise WindowCredentialError(
                    f"Window extraction only recovered {len(sink)} of {end - start} bytes"
                )
            results.append(bytes(sink))
        return results


def open_window_credential(source: SourceLike) -> WindowCredential:
    """Build a reusable window credential for an organized PCD frame.

    Args:
        source: A filesystem path (preferred, allows the credential to reopen
            the file on every query) or an already open binary file object.

    Raises:
        WindowCredentialError: If the frame is not an organized binary /
            binary_compressed grid, or if its data cannot be fully verified.
    """

    owns_path = isinstance(source, (str, Path))
    if owns_path:
        path = cast(PathLike, source)
        if not Path(path).is_file():
            raise WindowCredentialError(f"PCD file does not exist: {path}")
        fp = open(path, "rb")
    else:
        fp = cast(IO[bytes], source)
        fp.seek(0)

    try:
        metadata, field_names, data_offset = _read_header(fp)

        if metadata.data not in (Encoding.BINARY, Encoding.BINARY_COMPRESSED):
            raise WindowCredentialError(
                f"Window credentials only support binary layouts, got {metadata.data.value!r}"
            )
        if metadata.points != metadata.width * metadata.height or metadata.height < 1:
            raise WindowCredentialError(
                "Window credentials require an organized grid where POINTS == WIDTH * HEIGHT"
            )

        planes = _build_planes(metadata)
        frame_byte_size = sum(plane.byte_size for plane in planes)
        lzf_ops: Optional[npt.NDArray] = None

        if metadata.data == Encoding.BINARY:
            fp.seek(0, 2)
            file_size = fp.tell()
            if file_size < data_offset + frame_byte_size:
                raise WindowCredentialError(
                    "Binary frame is truncated before the declared point data"
                )
        else:
            header_bytes = fp.read(8)
            if len(header_bytes) != 8:
                raise WindowCredentialError("Truncated binary_compressed size header")
            compressed_size, uncompressed_size = struct.unpack("II", header_bytes)
            if uncompressed_size != frame_byte_size:
                raise WindowCredentialError(
                    "Decompressed size does not match the FIELDS/SIZE/COUNT/POINTS layout: "
                    f"{uncompressed_size} != {frame_byte_size}"
                )
            stream = fp.read(compressed_size)
            if len(stream) != compressed_size:
                raise WindowCredentialError(
                    "binary_compressed block is truncated before its declared compressed size"
                )
            lzf_ops = _parse_lzf(stream, uncompressed_size)
    finally:
        if owns_path:
            fp.close()

    return WindowCredential(
        source=source,
        metadata=metadata,
        field_names=field_names,
        data_offset=data_offset,
        planes=planes,
        lzf_ops=lzf_ops,
        owns_path=owns_path,
    )
