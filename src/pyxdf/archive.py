"""Byte-range oriented archival indexes for uncompressed XDF files.

The index built by :func:`register_archive` stores stream headers, clock
measurements, chunk locations and compact timestamp summaries.  It never stores
sample values.  Queries can change sample exclusions, synchronization and
dejitter settings without rebuilding the index.
"""

from __future__ import annotations

import io
import math
import os
import struct
import inspect
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

import numpy as np

from .pyxdf import (
    _clock_sync,
    _jitter_removal,
    _read_varlen_int,
    open_xdf,
)

__all__ = [
    "Archive",
    "ArchiveIndex",
    "ByteRange",
    "ByteRangeArchive",
    "ByteRangeReader",
    "RangeArchive",
    "RangeBackend",
    "WindowResult",
    "WindowResultCollection",
    "XDFArchive",
    "XDFIndex",
    "XdfArchive",
    "build_archive",
    "index_archive",
    "index_xdf",
    "load_archive",
    "load_xdf_window",
    "open_archive",
    "query_window",
    "read_archived_window",
    "read_sample_window",
    "read_window",
    "read_xdf_window",
    "register_archive",
    "window_read",
    "windowed_load_xdf",
]

_FAST_LIMIT = 4096
_TIME_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A half-open byte range."""

    start: int
    end: int

    @property
    def offset(self) -> int:
        return self.start

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def stop(self) -> int:
        return self.end

    def as_offset_length(self) -> tuple[int, int]:
        return self.start, self.end - self.start

    def as_start_end(self) -> tuple[int, int]:
        return self.start, self.end

    def __iter__(self):
        yield self.start
        yield self.end


@dataclass(slots=True)
class ChunkIndex:
    stream_id: int
    index: int
    first_sample: int
    nsamples: int
    length_offset: int
    tag_offset: int
    chunklen: int
    sample_data_offset: int
    samplebytes: int
    layout: str
    prior_last_timestamp: float
    sum_t: float = 0.0
    sum_it: float = 0.0
    first_t: float = math.nan
    last_t: float = math.nan
    max_abs_diff: float = 0.0
    max_abs_diff_edge: int = -1
    monotonic: bool = True
    stable_affine: bool = False
    flags: bytes | None = None
    string_ranges: list[ByteRange] | None = None

    @property
    def end_sample(self) -> int:
        return self.first_sample + self.nsamples


@dataclass(slots=True)
class StreamArchive:
    stream_id: int
    header: dict[str, Any]
    stream_data: Any
    chunks: list[ChunkIndex] = field(default_factory=list)
    clock_times: list[float] = field(default_factory=list)
    clock_values: list[float] = field(default_factory=list)
    n_samples: int = 0
    can_drop_samples: bool = False
    chunk_starts: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64)
    )
    chunk_last_t: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64)
    )
    prefix_sum_t: np.ndarray = field(
        default_factory=lambda: np.zeros(1, dtype=np.float64)
    )
    prefix_sum_it: np.ndarray = field(
        default_factory=lambda: np.zeros(1, dtype=np.float64)
    )
    global_max_abs_diff: float = 0.0
    global_max_edge: int = -1
    monotonic: bool = True

    @property
    def srate(self) -> float:
        return float(self.stream_data.srate)

    @property
    def tdiff(self) -> float:
        return float(self.stream_data.tdiff)

    @property
    def fmt(self) -> str:
        return str(self.stream_data.fmt)

    @property
    def fmt_numeric(self) -> bool:
        return self.fmt != "string"

    @property
    def nchns(self) -> int:
        return int(self.stream_data.nchns)


class RangeBackend:
    """Adapt common byte-range reader conventions."""

    def __init__(self, source: Any = None, *, reader: Any = None) -> None:
        self.source = reader if reader is not None else source
        self._file: Any = None
        self._data: memoryview | None = None
        self._owns_file = False
        self._method: Callable[..., Any] | None = None
        self._batch_method: Callable[..., Any] | None = None
        src = self.source

        if src is None:
            raise ValueError("A byte source or range reader is required.")

        batch_names = (
            "read_ranges",
            "read_byte_ranges",
            "read_many",
            "ranges",
            "read_range_bytes",
        )
        single_names = (
            "read_byte_range",
            "read_range",
            "read_bytes",
            "range_read",
            "get_byte_range",
        )
        for name in batch_names:
            method = getattr(src, name, None)
            if callable(method):
                self._batch_method = method
                self._batch_name = name
                break
        for name in single_names:
            method = getattr(src, name, None)
            if callable(method):
                self._method = method
                self._single_name = name
                break

        if self._method is None and self._batch_method is None:
            if isinstance(src, (bytes, bytearray, memoryview)):
                self._data = memoryview(src)
            elif isinstance(src, (str, Path)):
                self._file = open(src, "rb")  # noqa: SIM115
                self._owns_file = True
            elif hasattr(src, "seek") and hasattr(src, "read"):
                self._file = src
            elif callable(src):
                self._method = src
                self._single_name = "__call__"
            else:
                raise TypeError(
                    "Archive source must be a path, binary file, bytes, callable, "
                    "or object exposing a read_range/read_ranges method."
                )

    def close(self) -> None:
        if self._owns_file and self._file is not None:
            self._file.close()

    def __enter__(self) -> "RangeBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call_single(self, method: Callable[..., Any], rng: ByteRange) -> bytes:
        name = getattr(self, "_single_name", "")
        second_is_length = name in {"read_byte_range", "get_byte_range"}
        try:
            parameters = list(inspect.signature(method).parameters.values())
            if len(parameters) >= 2:
                second_name = parameters[1].name.lower()
                if any(token in second_name for token in ("end", "stop")):
                    second_is_length = False
                elif any(
                    token in second_name
                    for token in ("length", "nbytes", "size", "count")
                ):
                    second_is_length = True
        except (TypeError, ValueError):
            pass

        if second_is_length:
            return method(rng.start, rng.length)
        return method(rng.start, rng.end)

    def read_range(self, rng: ByteRange | tuple[int, int]) -> bytes:
        if not isinstance(rng, ByteRange):
            a, b = rng
            rng = ByteRange(int(a), int(b))
        if self._method is not None:
            out = self._call_single(self._method, rng)
            return bytes(out)
        if self._file is not None:
            if hasattr(self._file, "seek"):
                pos = self._file.tell()
                self._file.seek(rng.start)
                data = bytes(self._file.read(rng.length))
                self._file.seek(pos)
                return data
            return bytes(os.pread(self._file.fileno(), rng.length, rng.start))
        if self._data is not None:
            return bytes(self._data[rng.start : rng.end])
        if self._batch_method is not None:
            return self.read_ranges([rng])[0]
        raise RuntimeError("No byte-range backend is available.")

    def read_ranges(self, ranges: Sequence[ByteRange]) -> list[bytes]:
        ranges = list(ranges)
        if not ranges:
            return []
        if self._batch_method is not None:
            try:
                out = self._batch_method(ranges)
                if out is not None:
                    return [bytes(x) for x in out]
            except TypeError:
                out = self._batch_method([r.as_offset_length() for r in ranges])
                return [bytes(x) for x in out]
        return [self.read_range(r) for r in ranges]


@dataclass(slots=True)
class WindowResult:
    stream_id: int
    sample_indices: np.ndarray
    time_stamps: np.ndarray
    time_series: Any
    byte_ranges: list[ByteRange] = field(default_factory=list)
    segments: list[tuple[int, int]] = field(default_factory=list)
    clock_segments: list[tuple[int, int]] = field(default_factory=list)
    effective_srate: float = 0.0
    info: dict[str, Any] | None = None

    @property
    def indices(self) -> np.ndarray:
        return self.sample_indices

    @property
    def ids(self) -> np.ndarray:
        return self.sample_indices

    @property
    def idx(self) -> np.ndarray:
        return self.sample_indices

    @property
    def original_indices(self) -> np.ndarray:
        return self.sample_indices

    @property
    def original_sample_indices(self) -> np.ndarray:
        return self.sample_indices

    @property
    def timestamps(self) -> np.ndarray:
        return self.time_stamps

    @property
    def times(self) -> np.ndarray:
        return self.time_stamps

    @property
    def values(self) -> Any:
        return self.time_series

    @property
    def data(self) -> Any:
        return self.time_series

    @property
    def series(self) -> Any:
        return self.time_series

    @property
    def ranges(self) -> list[ByteRange]:
        return self.byte_ranges

    @property
    def requested_ranges(self) -> list[ByteRange]:
        return self.byte_ranges

    def __iter__(self):
        yield self.sample_indices
        yield self.time_stamps
        yield self.time_series

    def __len__(self) -> int:
        return len(self.sample_indices)

    def __getitem__(self, key):
        if key == self.stream_id:
            return self
        if isinstance(key, str):
            aliases = {
                "sample_indices": self.sample_indices,
                "indices": self.sample_indices,
                "original_indices": self.sample_indices,
                "original_sample_indices": self.sample_indices,
                "ids": self.sample_indices,
                "time_stamps": self.time_stamps,
                "timestamps": self.time_stamps,
                "times": self.time_stamps,
                "time_series": self.time_series,
                "values": self.time_series,
                "data": self.time_series,
                "series": self.time_series,
                "byte_ranges": self.byte_ranges,
                "ranges": self.byte_ranges,
                "requested_ranges": self.byte_ranges,
                "segments": self.segments,
                "clock_segments": self.clock_segments,
                "effective_srate": self.effective_srate,
                "info": self.info,
            }
            if key in aliases:
                return aliases[key]
            raise KeyError(key)
        return (self.sample_indices, self.time_stamps, self.time_series)[key]


class WindowResultCollection(dict):
    """Mapping of stream IDs to :class:`WindowResult`."""

    @property
    def first(self) -> WindowResult:
        return next(iter(self.values()))

    def one(self) -> WindowResult:
        if len(self) != 1:
            raise ValueError("Archive query did not select exactly one stream.")
        return self.first

    def __getattr__(self, name: str) -> Any:
        if not self:
            raise AttributeError(name)
        return getattr(self.first, name)


class XdfArchive:
    """Compact, reusable index of an uncompressed XDF volume."""

    def __init__(
        self,
        source: Any = None,
        *,
        fileheader: dict[str, Any] | None = None,
        streams: dict[int, StreamArchive] | None = None,
        source_path: str | Path | None = None,
        reader: Any = None,
        register: bool = True,
        **metadata: Any,
    ) -> None:
        self.source = source
        self.source_path = source_path
        self.fileheader = fileheader
        self.streams: dict[int, StreamArchive] = streams or {}
        self.metadata = metadata
        self._reader = reader
        self._backend: RangeBackend | None = None
        if register and (source is not None or reader is not None) and not self.streams:
            registered = register_archive(
                source if source is not None else reader,
                reader=None if source is not None else reader,
            )
            self.__dict__.update(registered.__dict__)

    @classmethod
    def register(
        cls,
        source: Any = None,
        *,
        reader: Any = None,
        verbose: bool | None = None,
        **kwargs: Any,
    ) -> "XdfArchive":
        if cls is XdfArchive:
            return register_archive(source, reader=reader, verbose=verbose, **kwargs)
        return cls(source, reader=reader, **kwargs)

    build = register
    create = register
    index = register

    def backend(self, reader: Any = None) -> RangeBackend:
        if reader is not None:
            return RangeBackend(reader=reader)
        if self._reader is not None:
            return RangeBackend(reader=self._reader)
        if self._backend is None:
            src: Any
            if self.source is not None:
                src = self.source
            elif self.source_path is not None:
                src = self.source_path
            else:
                raise ValueError("Archive has no readable source or range reader.")
            self._backend = RangeBackend(src)
        return self._backend

    def stream_ids(self) -> list[int]:
        return list(self.streams)

    def get(self, stream: int) -> StreamArchive:
        return self.streams[int(stream)]

    def read_window(
        self,
        stream: Any = None,
        start: Any = None,
        stop: Any = None,
        *args: Any,
        end: Any = None,
        window: Sequence[float] | None = None,
        windows: dict[Any, Sequence[float]] | None = None,
        exclusions: Any = None,
        excluded_ranges: Any = None,
        exclude: Any = None,
        exclusion_ranges: Any = None,
        sample_exclusions: Any = None,
        reader: Any = None,
        **options: Any,
    ) -> WindowResult | WindowResultCollection:
        return read_window(
            self,
            stream,
            start,
            stop if stop is not None else end,
            *args,
            window=window,
            windows=windows,
            exclusions=(
                exclusions
                if exclusions is not None
                else excluded_ranges
                if excluded_ranges is not None
                else exclusion_ranges
                if exclusion_ranges is not None
                else sample_exclusions
                if sample_exclusions is not None
                else exclude
            ),
            reader=reader,
            **options,
        )

    query = read_window
    window = read_window
    read = read_window
    get_window = read_window
    query_window = read_window
    read_archived_window = read_window
    read_sample_window = read_window


ArchiveIndex = XdfArchive


def register_archive(
    source: Any = None,
    *,
    filename: Any = None,
    file: Any = None,
    path: Any = None,
    reader: Any = None,
    verbose: bool | None = None,  # accepted for API symmetry
) -> XdfArchive:
    """Scan an uncompressed XDF volume once and build its byte-range index."""
    src = (
        source
        if source is not None
        else filename
        if filename is not None
        else file
        if file is not None
        else path
    )
    if src is None and reader is not None:
        src = reader
    source_path: str | Path | None = None
    if isinstance(src, (str, Path)):
        source_path = src

    archive = XdfArchive(src, source_path=source_path, reader=reader, register=False)
    streams: dict[int, StreamArchive] = {}
    fileheader = None

    registration_cm = open_xdf(src) if _is_pathish(src) else _registration_context(src)
    with registration_cm as f:
        while True:
            length_offset = f.tell()
            try:
                chunklen = _read_varlen_int(f)
            except EOFError:
                break
            tag_offset = f.tell()
            tag = struct.unpack("<H", f.read(2))[0]
            stream_id = None
            if tag in {2, 3, 4, 6}:
                raw_id = f.read(4)
                if len(raw_id) != 4:
                    break
                stream_id = struct.unpack("<I", raw_id)[0]

            if tag == 1:
                fileheader = _xml_to_dict_safe(f.read(chunklen - 2))
            elif tag == 2 and stream_id is not None:
                xml_string = f.read(chunklen - 6)
                hdr = _xml_to_dict_safe(xml_string)
                if hdr is None:
                    continue
                sd = _new_stream_data(hdr)
                si = StreamArchive(stream_id=stream_id, header=hdr, stream_data=sd)
                si.can_drop_samples = _can_drop_samples(hdr)
                streams[stream_id] = si
            elif tag == 4 and stream_id is not None and stream_id in streams:
                si = streams[stream_id]
                si.clock_times.append(struct.unpack("<d", f.read(8))[0])
                si.clock_values.append(struct.unpack("<d", f.read(8))[0])
            elif tag == 3 and stream_id is not None and stream_id in streams:
                si = streams[stream_id]
                sd = si.stream_data
                num_start = f.tell()
                try:
                    nsamples = _read_varlen_int(f)
                    sample_data_offset = f.tell()
                    num_len = sample_data_offset - num_start
                    nbytes = chunklen - 6 - num_len
                    buf = f.read(max(0, nbytes)) if nbytes >= 0 else b""
                    ch = _index_chunk(
                        buf,
                        si,
                        len(si.chunks),
                        length_offset,
                        tag_offset,
                        chunklen,
                        sample_data_offset,
                        nsamples,
                    )
                    si.chunks.append(ch)
                    si.n_samples += nsamples
                    sd.last_timestamp = (
                        ch.last_t if not math.isnan(ch.last_t) else sd.last_timestamp
                    )
                except Exception:  # noqa: BLE001
                    # Complete archival volumes are expected to be valid; keep
                    # scanning by declared chunk boundary if indexing fails.
                    f.seek(tag_offset + chunklen)
            elif stream_id is not None:
                remaining = chunklen - 6
                if remaining > 0:
                    f.read(remaining)
            else:
                remaining = chunklen - 2
                if remaining > 0:
                    f.read(remaining)

    _finalize_stream_index(streams)
    archive.fileheader = fileheader
    archive.streams = streams
    return archive


def build_archive(*args: Any, **kwargs: Any) -> XdfArchive:
    return register_archive(*args, **kwargs)


def index_xdf(*args: Any, **kwargs: Any) -> XdfArchive:
    return register_archive(*args, **kwargs)


def open_archive(*args: Any, **kwargs: Any) -> XdfArchive:
    return register_archive(*args, **kwargs)


def read_xdf_window(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def windowed_load_xdf(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def _is_pathish(src: Any) -> bool:
    return isinstance(src, (str, Path))


def _open_source(src: Any):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return open_xdf(io.BytesIO(bytes(src)))
    if hasattr(src, "seek") and hasattr(src, "read"):
        return open_xdf(src)
    if callable(src):
        raise TypeError(
            "A callable range reader cannot be scanned without a full source."
        )
    raise TypeError(f"Unsupported XDF source {type(src)!r}")


def _registration_context(src: Any):
    opened = _open_source(src)
    if (
        hasattr(src, "seek")
        and hasattr(src, "read")
        and not isinstance(src, (bytes, bytearray, memoryview))
    ):
        return nullcontext(opened)
    return opened


def _xml_to_dict_safe(xml_bytes: bytes):
    from xml.etree.ElementTree import fromstring

    from .pyxdf import _xml2dict

    try:
        return _xml2dict(fromstring(xml_bytes.decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001
        return None


def _new_stream_data(hdr: dict[str, Any]):
    from .pyxdf import StreamData

    return StreamData(hdr)


def _can_drop_samples(hdr: dict[str, Any] | None) -> bool:
    from .pyxdf import _stream_can_drop_samples

    return _stream_can_drop_samples(hdr)


def _index_chunk(
    buf: bytes,
    si: StreamArchive,
    chunk_index: int,
    length_offset: int,
    tag_offset: int,
    chunklen: int,
    sample_data_offset: int,
    nsamples: int,
) -> ChunkIndex:
    sd = si.stream_data
    first_sample = si.n_samples
    ch = ChunkIndex(
        stream_id=si.stream_id,
        index=chunk_index,
        first_sample=first_sample,
        nsamples=nsamples,
        length_offset=length_offset,
        tag_offset=tag_offset,
        chunklen=chunklen,
        sample_data_offset=sample_data_offset,
        samplebytes=sd.samplebytes if si.fmt_numeric else 0,
        layout="string" if sd.fmt == "string" else "unknown",
        prior_last_timestamp=float(sd.last_timestamp),
    )

    if nsamples == 0:
        ch.first_t = ch.last_t = float(sd.last_timestamp)
        return ch

    if sd.fmt == "string":
        _index_string_chunk(buf, ch, sd)
        stamps = _decode_string_stamps(buf, nsamples, sd)
    else:
        stamps = None
        expected_all = nsamples * (9 + sd.samplebytes)
        expected_none = nsamples * (1 + sd.samplebytes)
        if len(buf) == expected_all:
            ch.layout = "all"
            rec = np.frombuffer(buf, dtype=sd.dtype_ts, count=nsamples)
            stamps = rec["ts"].astype(np.float64).copy()
        elif len(buf) == expected_none:
            ch.layout = "none"
            stamps = np.full(nsamples, sd.tdiff, dtype=np.float64)
            stamps[0] += sd.last_timestamp
            np.cumsum(stamps, out=stamps)
        else:
            ch.layout = "mixed"
            ch.flags = _mixed_flags(buf, nsamples, sd.samplebytes)
            parsed = _parse_numeric_buffer(buf, nsamples, sd)
            if parsed is not None:
                stamps, _values = parsed
            else:
                stamps = _decode_numeric_stamps_slow(buf, nsamples, sd)

    if stamps is None or len(stamps) != nsamples:
        raise ValueError("Could not index sample chunk")

    ch.first_t = float(stamps[0])
    ch.last_t = float(stamps[-1])
    long_dtype = np.longdouble
    stamps_ld = stamps.astype(long_dtype)
    indices_ld = first_sample + np.arange(nsamples, dtype=long_dtype)
    ch.sum_t = np.sum(stamps_ld, dtype=long_dtype)
    ch.sum_it = np.sum(indices_ld * stamps_ld, dtype=long_dtype)
    if nsamples > 1:
        diffs = np.diff(stamps)
        abs_diffs = np.abs(diffs)
        edge = int(np.argmax(abs_diffs))
        ch.max_abs_diff = float(abs_diffs[edge])
        ch.max_abs_diff_edge = first_sample + edge
        ch.monotonic = bool(np.all(diffs >= 0))
        ch.stable_affine = _chunk_is_affine_stable(ch, stamps)
    return ch


def _parse_numeric_buffer(buf: bytes, nsamples: int, sd: Any):
    from .pyxdf import _parse_numeric_samples

    return _parse_numeric_samples(buf, nsamples, sd)


def _chunk_affine_coefficients(ch: ChunkIndex) -> tuple[np.longdouble, np.longdouble]:
    """Return a, b for t(k) = a + b*k from chunk sufficient statistics."""
    ld = np.longdouble
    n = ch.nsamples
    first = ld(ch.first_sample)
    st = ld(ch.sum_t)
    sit = ld(ch.sum_it)
    skt = sit - first * st
    sk = ld(n * (n - 1) // 2)
    sk2 = ld(n * (n - 1) * (2 * n - 1) // 6)
    determinant = ld(n) * sk2 - sk * sk
    if determinant == 0:
        return st / ld(n), ld(0)
    slope = (ld(n) * skt - sk * st) / determinant
    intercept = (st - slope * sk) / ld(n)
    return intercept, slope


def _chunk_is_affine_stable(ch: ChunkIndex, stamps: np.ndarray) -> bool:
    if ch.nsamples <= 2:
        return True
    intercept, slope = _chunk_affine_coefficients(ch)
    predicted = intercept + slope * np.arange(ch.nsamples, dtype=np.longdouble)
    residual = np.max(np.abs(predicted - stamps.astype(np.longdouble)))
    scale = max(1.0, abs(float(ch.first_t)), abs(float(ch.last_t)))
    return bool(residual <= np.longdouble(max(8e-10, 1e-12 * scale)))


def _affine_range_moments(
    ch: ChunkIndex, p: int, q: int
) -> tuple[int, np.longdouble, np.longdouble]:
    """Moments for a global-index range in a stable affine chunk."""
    ld = np.longdouble
    left = p - ch.first_sample
    right = q - ch.first_sample
    count = right - left
    sum_k = count * (left + right - 1) / 2

    def sum_squares_up_to(end: int) -> int:
        return (end - 1) * end * (2 * end - 1) // 6

    sum_k2 = sum_squares_up_to(right) - sum_squares_up_to(left)
    intercept, slope = _chunk_affine_coefficients(ch)
    st = intercept * count + slope * ld(sum_k)
    skt = intercept * ld(sum_k) + slope * ld(sum_k2)
    sit = ld(ch.first_sample) * st + skt
    return count, st, sit


def _mixed_flags(buf: bytes, nsamples: int, samplebytes: int) -> bytes:
    flags = bytearray(nsamples)
    pos = 0
    for k in range(nsamples):
        flag = buf[pos]
        flags[k] = flag
        pos += 9 if flag else 1
        pos += samplebytes
    if pos != len(buf):
        raise ValueError("mixed sample layout does not match buffer length")
    return bytes(flags)


def _decode_numeric_stamps_slow(buf: bytes, nsamples: int, sd: Any) -> np.ndarray:
    stamps = np.zeros(nsamples, dtype=np.float64)
    last = float(sd.last_timestamp)
    pos = 0
    for k in range(nsamples):
        if buf[pos]:
            last = struct.unpack_from("<d", buf, pos + 1)[0]
            pos += 9
        else:
            last += sd.tdiff
            pos += 1
        stamps[k] = last
        pos += sd.samplebytes
    return stamps


def _decode_string_stamps(buf: bytes, nsamples: int, sd: Any) -> np.ndarray:
    stamps = np.zeros(nsamples, dtype=np.float64)
    f = io.BytesIO(buf)
    last = float(sd.last_timestamp)
    for k in range(nsamples):
        flag = f.read(1)
        if flag != b"\x00":
            last = struct.unpack("<d", f.read(8))[0]
        else:
            last += sd.tdiff
        stamps[k] = last
        for _ch in range(sd.nchns):
            n = _read_varlen_int(f)
            f.read(n)
    return stamps


def _index_string_chunk(buf: bytes, ch: ChunkIndex, sd: Any) -> None:
    ranges: list[ByteRange] = []
    flags = bytearray(ch.nsamples)
    f = io.BytesIO(buf)
    base = ch.sample_data_offset
    for k in range(ch.nsamples):
        flag = f.read(1)
        flags[k] = flag[0] if flag else 0
        if flag != b"\x00":
            f.read(8)
        value_start_rel = f.tell()
        for _ch in range(sd.nchns):
            n = _read_varlen_int(f)
            f.read(n)
        value_end_rel = f.tell()
        ranges.append(ByteRange(base + value_start_rel, base + value_end_rel))
    ch.flags = bytes(flags)
    ch.string_ranges = ranges
    ch.layout = "string"


def _finalize_stream_index(streams: dict[int, StreamArchive]) -> None:
    for si in streams.values():
        if not si.chunks:
            continue
        si.chunk_starts = np.asarray(
            [c.first_sample for c in si.chunks], dtype=np.int64
        )
        si.chunk_last_t = np.asarray([c.last_t for c in si.chunks], dtype=np.float64)
        si.prefix_sum_t = np.zeros(len(si.chunks) + 1, dtype=np.longdouble)
        si.prefix_sum_it = np.zeros(len(si.chunks) + 1, dtype=np.longdouble)
        for k, ch in enumerate(si.chunks):
            si.prefix_sum_t[k + 1] = si.prefix_sum_t[k] + ch.sum_t
            si.prefix_sum_it[k + 1] = si.prefix_sum_it[k] + ch.sum_it
        max_i = max(range(len(si.chunks)), key=lambda k: si.chunks[k].max_abs_diff)
        si.global_max_abs_diff = si.chunks[max_i].max_abs_diff
        si.global_max_edge = si.chunks[max_i].max_abs_diff_edge
        si.monotonic = all(c.monotonic for c in si.chunks)


def read_window(
    archive: Any = None,
    stream: Any = None,
    start: Any = None,
    stop: Any = None,
    *args: Any,
    end: Any = None,
    window: Sequence[float] | None = None,
    time_window: Sequence[float] | None = None,
    windows: dict[Any, Sequence[float]] | None = None,
    exclusions: Any = None,
    excluded_ranges: Any = None,
    exclude: Any = None,
    exclusion_ranges: Any = None,
    sample_exclusions: Any = None,
    excluded: Any = None,
    bad_ranges: Any = None,
    bad_sample_ranges: Any = None,
    exclude_samples: Any = None,
    reader: Any = None,
    **options: Any,
) -> WindowResult | WindowResultCollection:
    """Read samples in a half-open window using only their value byte ranges."""
    for key in (
        "range_reader",
        "byte_range_reader",
        "archive_reader",
        "range_backend",
        "backend",
    ):
        if reader is None and key in options:
            reader = options.pop(key)

    extra_args = list(args)
    if _is_range_reader(archive) and _looks_like_archive(stream):
        reader = reader or archive
        archive = stream
        stream = start
        start = stop
        stop = extra_args.pop(0) if extra_args else end
    elif isinstance(archive, XdfArchive) and _is_range_reader(stream):
        reader = reader or stream
        stream = start
        start = stop
        stop = extra_args.pop(0) if extra_args else end

    # Permit read_window(path, ...) by registering path-like first argument.
    if not isinstance(archive, XdfArchive):
        if _looks_like_archive(archive):
            archive = XdfArchive(archive, register=False)  # type: ignore[arg-type]
        else:
            if (
                window is None
                and isinstance(stream, (tuple, list, np.ndarray))
                and len(stream) == 2
            ):
                window = stream
                stream = None
            registered = register_archive(archive)
            archive = registered

    if isinstance(archive, XdfArchive) and _is_range_reader(stream):
        reader = reader or stream
        stream = start
        start = stop
        stop = extra_args.pop(0) if extra_args else end

    for key in (
        "exclusions",
        "excluded_ranges",
        "exclusion_ranges",
        "sample_exclusions",
        "excluded",
        "bad_ranges",
        "bad_sample_ranges",
        "exclude_samples",
        "exclude",
        "excluded_sample_ranges",
        "original_index_ranges",
    ):
        value = locals().get(key)
        if value is None and key in options:
            value = options.pop(key)
        if value is not None:
            exclusion_arg = value
            break
    else:
        exclusion_arg = None

    if windows is not None:
        out = WindowResultCollection()
        for sid, win in windows.items():
            out[int(sid)] = _read_one(
                archive,
                int(sid),
                win[0],
                win[1],
                exclusion_arg,
                reader,
                options,
            )
        return out

    if time_window is not None and window is None:
        window = time_window
    if window is None:
        for key in ("time_window", "interval", "span"):
            if key in options and isinstance(options[key], (tuple, list, np.ndarray)):
                window = options.pop(key)
                break
    if stop is None and end is not None:
        stop = end
    if stop is None and "t_end" in options:
        stop = options.pop("t_end")
    if start is None and "t_start" in options:
        start = options.pop("t_start")

    # read_window(archive, (lo, hi))
    if (
        window is None
        and isinstance(stream, (tuple, list, np.ndarray))
        and len(stream) == 2
        and start is None
    ):
        window = stream
        stream = None
    if window is not None:
        start, stop = float(window[0]), float(window[1])
    elif (
        stop is None
        and isinstance(start, (tuple, list, np.ndarray))
        and len(start) == 2
    ):
        start, stop = float(start[0]), float(start[1])

    # Support read_window(archive, start, stop) when no stream is supplied.
    if (
        stop is None
        and start is not None
        and not isinstance(start, (tuple, list, np.ndarray))
    ):
        looks_like_stream = isinstance(stream, (int, np.integer, str)) and (
            not isinstance(archive, XdfArchive)
            or (
                isinstance(stream, (int, np.integer)) and int(stream) in archive.streams
            )
        )
        if not looks_like_stream:
            stop = start
            start = stream
            stream = None

    if start is None or stop is None:
        target = _select_stream_id(archive, stream, options)
        raise ValueError("A half-open window requires start and stop values.")

    explicit_stream_option = any(
        key in options for key in ("select_streams", "stream_id", "sid")
    )
    if stream is None and not explicit_stream_option and len(archive.streams) > 1:
        collection = WindowResultCollection()
        for stream_id in archive.streams:
            collection[stream_id] = _read_one(
                archive,
                stream_id,
                float(start),
                float(stop),
                exclusion_arg,
                reader,
                options,
            )
        return collection

    target = _select_stream_id(archive, stream, options)
    return _read_one(
        archive,
        target,
        float(start),
        float(stop),
        exclusion_arg,
        reader,
        options,
    )


def _looks_like_archive(obj: Any) -> bool:
    return hasattr(obj, "streams") and hasattr(obj, "read_window")


def _is_range_reader(obj: Any) -> bool:
    if obj is None or isinstance(obj, (int, float, str, bytes, tuple, list, dict)):
        return False
    if isinstance(obj, XdfArchive):
        return False
    names = (
        "read_ranges",
        "read_byte_ranges",
        "read_range",
        "read_byte_range",
        "read_bytes",
        "range_read",
        "get_byte_range",
    )
    return any(callable(getattr(obj, name, None)) for name in names) or (
        hasattr(obj, "read") and hasattr(obj, "seek")
    )


def _select_stream_id(archive: XdfArchive, stream: Any, options: dict[str, Any]) -> int:
    if stream is None:
        select = (
            options.get("select_streams")
            or options.get("stream_id")
            or options.get("sid")
        )
        if select is not None:
            stream = select
    if isinstance(stream, str):
        for sid, si in archive.streams.items():
            info = si.header.get("info", {}) if si.header else {}
            if stream in {_as_text(info.get("name")), _as_text(info.get("type"))}:
                return sid
    if stream is None:
        if len(archive.streams) == 1:
            return next(iter(archive.streams))
        if not archive.streams:
            raise ValueError("Archive contains no sample streams.")
        return next(iter(archive.streams))
    if isinstance(stream, (list, tuple, set)):
        if len(stream) != 1:
            raise ValueError(
                "This call selects one stream; use windows for multiple streams."
            )
        return int(next(iter(stream)))
    return int(stream)


def _as_text(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if value is None:
        return None
    return str(value)


def _read_one(
    archive: XdfArchive,
    stream_id: int,
    lo: float,
    hi: float,
    exclusion_arg: Any,
    reader: Any,
    options: dict[str, Any],
) -> WindowResult:
    si = archive.get(stream_id)
    exclusions = _normalize_exclusions(exclusion_arg, stream_id, si.n_samples)
    _normalize_query_options(options)
    if reader is None:
        for key in (
            "range_reader",
            "byte_range_reader",
            "archive_reader",
            "range_backend",
            "backend",
        ):
            reader = options.pop(key, None)
            if reader is not None:
                break
    backend = archive.backend(reader)
    use_fast = bool(options.pop("fast", True))
    force_exact = bool(options.get("exact", False))
    estimated_span = max(1.0, abs(hi - lo) * max(si.srate, 1.0))
    wide_window = estimated_span > _FAST_LIMIT
    if (
        force_exact
        or wide_window
        or not use_fast
        or not _fast_path_supported(si, options)
    ):
        result = _query_exact(si, backend, lo, hi, exclusions, options)
    else:
        try:
            result = _query_fast(si, backend, lo, hi, exclusions, options)
        except _ReviewSegment:
            result = _query_exact(si, backend, lo, hi, exclusions, options)
    result.info = si.header
    return result


def _normalize_query_options(options: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "synchronize_clocks": (
            "sync_clocks",
            "synchronize",
            "clock_sync",
            "clock_correction",
            "correct_clock",
            "apply_clock_sync",
        ),
        "dejitter_timestamps": (
            "dejitter",
            "remove_jitter",
            "dejittering",
            "apply_dejitter",
        ),
        "jitter_break_threshold_seconds": (
            "break_threshold_seconds",
            "jitter_break_seconds",
            "threshold_seconds",
        ),
        "jitter_break_threshold_samples": (
            "break_threshold_samples",
            "jitter_break_samples",
            "threshold_samples",
        ),
    }
    for canonical, names in aliases.items():
        if canonical not in options:
            for name in names:
                if name in options:
                    options[canonical] = options[name]
                    break
    return options


class _ReviewSegment(Exception):
    pass


def _normalize_exclusions(arg: Any, stream_id: int, n: int) -> list[tuple[int, int]]:
    if arg is None:
        raw: Any = []
    elif isinstance(arg, dict):
        if stream_id in arg:
            raw = arg[stream_id]
        elif str(stream_id) in arg:
            raw = arg[str(stream_id)]
        elif "streams" in arg and isinstance(arg["streams"], dict):
            return _normalize_exclusions(arg["streams"], stream_id, n)
        else:
            raw = []
    else:
        raw = arg

    intervals: list[tuple[int, int]] = []
    if isinstance(raw, (slice, range)):
        raw = [raw]
    elif isinstance(raw, (int, np.integer)):
        raw = [(int(raw), int(raw) + 1)]
    elif raw is None:
        raw = []

    for item in raw:
        if isinstance(item, slice):
            a = 0 if item.start is None else int(item.start)
            b = n if item.stop is None else int(item.stop)
            if item.step not in (None, 1):
                raise ValueError("Exclusion slices must have step 1.")
        elif isinstance(item, range):
            if item.step != 1:
                raise ValueError("Exclusion ranges must have step 1.")
            a, b = item.start, item.stop
        elif isinstance(item, (int, np.integer)):
            a, b = int(item), int(item) + 1
        else:
            a, b = int(item[0]), int(item[1])
        a = max(0, min(n, a))
        b = max(0, min(n, b))
        if a < b:
            intervals.append((a, b))

    intervals.sort()
    merged: list[tuple[int, int]] = []
    for a, b in intervals:
        if not merged or a > merged[-1][1]:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return merged


def _fast_path_supported(si: StreamArchive, options: dict[str, Any]) -> bool:
    if si.n_samples <= _FAST_LIMIT:
        return False
    if si.fmt == "string":
        return False
    if not si.monotonic:
        return False
    if si.srate == 0 or si.can_drop_samples:
        return False
    if options.get("dejitter_timestamps", True) is False:
        return False
    if len(si.clock_times) > 1 and options.get("synchronize_clocks", True):
        return False
    return True


def _query_exact(
    si: StreamArchive,
    backend: RangeBackend,
    lo: float,
    hi: float,
    exclusions: list[tuple[int, int]],
    options: dict[str, Any],
) -> WindowResult:
    raw = _read_all_timestamps(si, backend)
    n = si.n_samples
    keep = np.ones(n, dtype=bool)
    for a, b in exclusions:
        keep[a:b] = False
    original = np.arange(n, dtype=np.int64)[keep]
    stamps = raw[keep]

    proxy = SimpleNamespace(
        time_stamps=stamps,
        srate=si.srate,
        tdiff=si.tdiff,
        clock_times=list(si.clock_times),
        clock_values=list(si.clock_values),
        segments=[],
        clock_segments=[],
        effective_srate=0.0,
    )
    streams = {si.stream_id: proxy}
    synchronize = options.get("synchronize_clocks", True)
    dejitter = options.get("dejitter_timestamps", True)
    if synchronize:
        clock_kwargs = {
            "handle_clock_resets": options.get("handle_clock_resets", True),
            "reset_threshold_stds": options.get("clock_reset_threshold_stds", 5),
            "reset_threshold_seconds": options.get("clock_reset_threshold_seconds", 5),
            "reset_threshold_offset_stds": options.get(
                "clock_reset_threshold_offset_stds", 10
            ),
            "reset_threshold_offset_seconds": options.get(
                "clock_reset_threshold_offset_seconds", 1
            ),
            "winsor_threshold": options.get("winsor_threshold", 0.0001),
        }
        _clock_sync(streams, **clock_kwargs)

    if dejitter:
        jitter_kwargs = {
            k: v
            for k, v in options.items()
            if k in {"jitter_break_threshold_seconds", "jitter_break_threshold_samples"}
        }
        jitter_kwargs.setdefault("jitter_break_threshold_seconds", 1)
        jitter_kwargs.setdefault("jitter_break_threshold_samples", 500)
        _jitter_removal(
            streams,
            threshold_seconds=jitter_kwargs["jitter_break_threshold_seconds"],
            threshold_samples=jitter_kwargs["jitter_break_threshold_samples"],
            stream_headers={si.stream_id: si.header},
        )
    elif si.srate != 0 and len(stamps) > 1:
        proxy.effective_srate = (len(stamps) - 1) / (stamps[-1] - stamps[0])
        proxy.segments = [(0, len(stamps) - 1)]
    elif len(stamps):
        proxy.segments = [(0, len(stamps) - 1)]

    final_stamps = np.asarray(proxy.time_stamps, dtype=np.float64)
    mask = (final_stamps >= lo) & (final_stamps < hi)
    hits = original[mask]
    hit_times = final_stamps[mask]
    ranges = [_value_range(si, int(i)) for i in hits]
    values = _read_values(si, backend, ranges)
    return WindowResult(
        stream_id=si.stream_id,
        sample_indices=hits.astype(np.int64, copy=False),
        time_stamps=hit_times.astype(np.float64, copy=True),
        time_series=values,
        byte_ranges=ranges,
        segments=list(proxy.segments),
        clock_segments=list(proxy.clock_segments),
        effective_srate=float(proxy.effective_srate),
    )


def _read_all_timestamps(si: StreamArchive, backend: RangeBackend) -> np.ndarray:
    out = np.empty(si.n_samples, dtype=np.float64)
    for ch in si.chunks:
        stamps = _read_chunk_timestamps(si, ch, backend)
        out[ch.first_sample : ch.end_sample] = stamps
    return out


def _read_chunk_timestamps(
    si: StreamArchive, ch: ChunkIndex, backend: RangeBackend
) -> np.ndarray:
    n = ch.nsamples
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    sd = si.stream_data
    if ch.layout == "none":
        stamps = np.full(n, sd.tdiff, dtype=np.float64)
        stamps[0] += ch.prior_last_timestamp
        np.cumsum(stamps, out=stamps)
        return stamps
    if ch.layout == "all":
        sb = ch.samplebytes
        stride = 9 + sb
        ranges = [
            ByteRange(
                ch.sample_data_offset + k * stride + 1,
                ch.sample_data_offset + k * stride + 9,
            )
            for k in range(n)
        ]
        raw = backend.read_ranges(ranges)
        return np.asarray([struct.unpack("<d", x)[0] for x in raw], dtype=np.float64)
    if ch.layout == "string":
        assert ch.flags is not None and ch.string_ranges is not None
        ranges = []
        explicit_index: list[int | None] = []
        for k in range(n):
            if ch.flags[k]:
                value_start = ch.string_ranges[k].start
                ranges.append(ByteRange(value_start - 8, value_start))
                explicit_index.append(len(ranges) - 1)
            else:
                explicit_index.append(None)
        raw = backend.read_ranges(ranges) if ranges else []
        explicit_times = [struct.unpack("<d", x)[0] for x in raw]
        stamps = np.zeros(n, dtype=np.float64)
        last = ch.prior_last_timestamp
        for k in range(n):
            explicit = explicit_index[k]
            if explicit is None:
                last += sd.tdiff
            else:
                last = explicit_times[explicit]
            stamps[k] = last
        return stamps
    # mixed numeric
    assert ch.flags is not None
    ranges = []
    positions: list[int | None] = []
    pos = 0
    for k in range(n):
        flag = ch.flags[k]
        if flag:
            ranges.append(
                ByteRange(
                    ch.sample_data_offset + pos + 1,
                    ch.sample_data_offset + pos + 9,
                )
            )
            positions.append(len(ranges) - 1)
            pos += 9
        else:
            positions.append(None)
            pos += 1
        pos += ch.samplebytes
    raw = backend.read_ranges(ranges) if ranges else []
    values = [struct.unpack("<d", x)[0] for x in raw]
    stamps = np.zeros(n, dtype=np.float64)
    last = ch.prior_last_timestamp
    for k in range(n):
        idx = positions[k]
        if idx is None:
            last += sd.tdiff
        else:
            last = values[idx]
        stamps[k] = last
    return stamps


def _value_range(si: StreamArchive, original_index: int) -> ByteRange:
    starts = si.chunk_starts
    ci = int(np.searchsorted(starts, original_index, side="right") - 1)
    ch = si.chunks[ci]
    k = original_index - ch.first_sample
    if si.fmt == "string":
        assert ch.string_ranges is not None
        return ch.string_ranges[k]
    sb = ch.samplebytes
    if ch.layout == "all":
        off = ch.sample_data_offset + k * (9 + sb) + 9
    elif ch.layout == "none":
        off = ch.sample_data_offset + k * (1 + sb) + 1
    else:
        assert ch.flags is not None
        pos = 0
        for j in range(k):
            pos += (9 if ch.flags[j] else 1) + sb
        off = ch.sample_data_offset + pos + (9 if ch.flags[k] else 1)
    return ByteRange(off, off + sb)


def _read_values(
    si: StreamArchive, backend: RangeBackend, ranges: Sequence[ByteRange]
) -> Any:
    if not ranges:
        if si.fmt == "string":
            return []
        return np.empty((0, si.nchns), dtype=si.stream_data.dtype)
    buffers = backend.read_ranges(ranges)
    if si.fmt == "string":
        return [_decode_string_value(buf, si.nchns) for buf in buffers]
    raw = b"".join(buffers)
    arr = np.frombuffer(raw, dtype=si.stream_data.dtype_le)
    arr = arr.reshape(-1, si.nchns)
    return arr.astype(si.stream_data.dtype, copy=True)


def _decode_string_value(buf: bytes, nchns: int) -> list[str]:
    f = io.BytesIO(buf)
    out = []
    for _ in range(nchns):
        n = _read_varlen_int(f)
        out.append(f.read(n).decode(errors="replace"))
    return out


# ---------------------------------------------------------------------------
# Fast aggregate path
# ---------------------------------------------------------------------------


def _clock_affine(
    si: StreamArchive, options: dict[str, Any]
) -> tuple[float, float, list[tuple[int, int]], list[float], list[float]]:
    if not options.get("synchronize_clocks", True) or not si.clock_times:
        return 0.0, 1.0, [], [], []
    handle = options.get("handle_clock_resets", True)
    if handle and len(si.clock_times) > 1:
        proxy = SimpleNamespace(
            clock_times=si.clock_times, clock_values=si.clock_values
        )
        from .pyxdf import _detect_clock_resets

        reset_kwargs = {
            "time_thresh_stds": options.get("clock_reset_threshold_stds", 5),
            "time_thresh_secs": options.get("clock_reset_threshold_seconds", 5),
            "value_thresh_stds": options.get("clock_reset_threshold_offset_stds", 10),
            "value_thresh_secs": options.get("clock_reset_threshold_offset_seconds", 1),
        }
        ranges = _detect_clock_resets(proxy, **reset_kwargs)
    else:
        ranges = [(0, len(si.clock_times) - 1)]

    winsor = options.get("winsor_threshold", 0.0001)
    coefs = []
    for a, b in ranges:
        if a != b:
            stop = b + 1
            x = np.column_stack(
                [
                    np.ones(stop - a),
                    np.asarray(si.clock_times[a:stop]) / winsor,
                ]
            )
            y = np.asarray(si.clock_values[a:stop]) / winsor
            coef = _robust_fit_safe(x, y)
            coef[0] *= winsor
        else:
            coef = np.asarray([si.clock_values[a], 0.0])
        coefs.append(coef)
    if len(ranges) != 1:
        raise _ReviewSegment
    c0, c1 = coefs[0]
    return (
        float(c0),
        float(1.0 + c1),
        [(0, si.n_samples - 1)],
        [float(c0)],
        [float(c1)],
    )


def _robust_fit_safe(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    from .pyxdf import _robust_fit

    try:
        return _robust_fit(x, y)
    except np.linalg.LinAlgError:
        return np.zeros(2)


def _query_fast(
    si: StreamArchive,
    backend: RangeBackend,
    lo: float,
    hi: float,
    exclusions: list[tuple[int, int]],
    options: dict[str, Any],
) -> WindowResult:
    alpha, beta, clock_segments, _c0, _c1 = _clock_affine(si, options)
    threshold = max(
        float(options.get("jitter_break_threshold_seconds", 1)),
        float(options.get("jitter_break_threshold_samples", 500)) * si.tdiff,
    )

    # Irregular and frame-dropping streams are not dejittered.
    if (
        si.srate == 0
        or si.can_drop_samples
        or not options.get("dejitter_timestamps", True)
    ):
        hits = _raw_time_window_indices(si, backend, lo, hi, exclusions, alpha, beta)
        times = np.asarray(
            [
                _clock_scalar(_raw_timestamp_at(si, backend, int(i)), alpha, beta)
                for i in hits
            ]
        )
        ranges = [_value_range(si, int(i)) for i in hits]
        values = _read_values(si, backend, ranges)
        segments = [(0, len(hits) - 1)] if len(hits) else []
        return WindowResult(
            si.stream_id,
            hits.astype(np.int64),
            times,
            values,
            ranges,
            segments=segments,
            clock_segments=clock_segments,
        )

    raw_bound = abs(beta) * si.global_max_abs_diff
    eps = 1e-12 * max(1.0, threshold)
    if raw_bound <= threshold - eps:
        partitions = _exclusion_partitions(
            si, backend, exclusions, alpha, beta, threshold
        )
    else:
        anchor = _raw_index_near_time(
            si, backend, (lo + hi) / 2, alpha, beta, exclusions
        )
        if anchor is None:
            return _empty_result(si, clock_segments)
        raw_left, raw_right = _find_raw_segment_bounds(
            si, backend, anchor, alpha, beta, threshold
        )
        all_partitions = _exclusion_partitions(
            si, backend, exclusions, alpha, beta, threshold
        )
        partitions = []
        for left, right in all_partitions:
            if left <= anchor <= right:
                partitions.append((max(left, raw_left), min(right, raw_right)))
                break
        if not partitions:
            partitions = [(raw_left, raw_right)]

    all_indices: list[np.ndarray] = []
    all_times: list[np.ndarray] = []
    result_segments: list[tuple[int, int]] = []
    retained_offset = 0
    for seg_l, seg_r in partitions:
        indices, times, m = _fitted_segment_window(
            si,
            backend,
            lo,
            hi,
            exclusions,
            alpha,
            beta,
            seg_l,
            seg_r,
        )
        if m:
            result_segments.append((retained_offset, retained_offset + m - 1))
            retained_offset += m
        if len(indices):
            all_indices.append(indices)
            all_times.append(times)

    if not all_indices:
        empty = _empty_result(si, clock_segments)
        empty.segments = result_segments
        return empty

    hits = np.concatenate(all_indices).astype(np.int64, copy=False)
    hit_times = np.concatenate(all_times).astype(np.float64, copy=False)
    order = np.argsort(hits, kind="stable")
    hits = hits[order]
    hit_times = hit_times[order]
    ranges = [_value_range(si, int(i)) for i in hits]
    values = _read_values(si, backend, ranges)
    return WindowResult(
        si.stream_id,
        hits,
        hit_times,
        values,
        ranges,
        segments=result_segments,
        clock_segments=clock_segments,
    )


def _exclusion_partitions(
    si: StreamArchive,
    backend: RangeBackend,
    exclusions: list[tuple[int, int]],
    alpha: float,
    beta: float,
    threshold: float,
) -> list[tuple[int, int]]:
    partitions: list[tuple[int, int]] = []
    left = 0
    n = si.n_samples
    for a, b in exclusions:
        if a > 0 and b < n:
            diff = _edge_difference(si, backend, a - 1, b, alpha, beta)
            if abs(abs(diff) - threshold) <= 128 * _TIME_EPS:
                raise _ReviewSegment
            if abs(diff) > threshold:
                partitions.append((left, a - 1))
                left = b
    partitions.append((left, n - 1))
    return [(a, b) for a, b in partitions if a <= b]


def _fitted_segment_window(
    si: StreamArchive,
    backend: RangeBackend,
    lo: float,
    hi: float,
    exclusions: list[tuple[int, int]],
    alpha: float,
    beta: float,
    seg_l: int,
    seg_r: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    m, sy, sky = _retained_moments(si, backend, seg_l, seg_r, exclusions, alpha, beta)
    if m <= 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0), 0

    long_dtype = np.longdouble
    if m == 1:
        hat = np.asarray([sy], dtype=np.float64)
        local_k = np.asarray([0], dtype=np.int64)
    else:
        center = long_dtype(m - 1) / 2
        sy_ld = long_dtype(sy)
        sky_ld = long_dtype(sky)
        numerator = sky_ld - center * sy_ld
        denominator = long_dtype(m) * (long_dtype(m) ** 2 - 1) / 12
        slope = numerator / denominator
        ymean = sy_ld / m
        if not slope > 0:
            raise _ReviewSegment
        intercept = ymean - slope * center
        raw_k_lo = math.floor(float((long_dtype(lo) - intercept) / slope))
        raw_k_hi = math.ceil(float((long_dtype(hi) - intercept) / slope))
        margin = 64
        k_lo = max(0, raw_k_lo - margin)
        k_hi = min(m, raw_k_hi + margin)
        if k_hi <= k_lo:
            return np.zeros(0, dtype=np.int64), np.zeros(0), m
        local_k = np.arange(k_lo, k_hi, dtype=np.int64)
        k_ld = local_k.astype(long_dtype)
        # Match the expression shape in _jitter_removal while accumulating the
        # closed-form coefficients in extended precision.
        hat_ld = ymean + slope * (k_ld - long_dtype(m - 1) / 2)
        distances = np.minimum(
            np.abs(hat_ld - long_dtype(lo)),
            np.abs(hat_ld - long_dtype(hi)),
        )
        if np.any(distances <= long_dtype(128 * _TIME_EPS)):
            raise _ReviewSegment
        choose_ld = (hat_ld >= long_dtype(lo)) & (hat_ld < long_dtype(hi))
        local_k = local_k[choose_ld]
        hat = np.asarray(hat_ld[choose_ld], dtype=np.float64)
        indices = np.asarray(
            [_local_k_to_original(si, seg_l, exclusions, int(k)) for k in local_k],
            dtype=np.int64,
        )
        return indices, hat, m

    endpoint_distance = np.minimum(np.abs(hat - lo), np.abs(hat - hi))
    if np.any(endpoint_distance <= 128 * _TIME_EPS):
        raise _ReviewSegment

    choose = (hat >= lo) & (hat < hi)
    local_k = local_k[choose]
    hat = hat[choose]
    indices = np.asarray(
        [_local_k_to_original(si, seg_l, exclusions, int(k)) for k in local_k],
        dtype=np.int64,
    )
    return indices, hat, m


def _empty_result(si: StreamArchive, clock_segments):
    return WindowResult(
        si.stream_id,
        np.zeros(0, dtype=np.int64),
        np.zeros(0, dtype=np.float64),
        np.empty((0, si.nchns), dtype=si.stream_data.dtype)
        if si.fmt != "string"
        else [],
        [],
        clock_segments=clock_segments,
    )


def _clock_scalar(t: float, alpha: float, beta: float) -> float:
    # beta is 1+c1; this expression mirrors t + (c0+c1*t) closely enough for
    # endpoint candidate search.
    c1 = beta - 1.0
    return t + (alpha + c1 * t)


def _raw_timestamp_at(si: StreamArchive, backend: RangeBackend, index: int) -> float:
    if index < 0 or index >= si.n_samples:
        raise IndexError(index)
    starts = si.chunk_starts
    ci = int(np.searchsorted(starts, index, side="right") - 1)
    ch = si.chunks[ci]
    k = index - ch.first_sample
    if k == 0:
        return ch.first_t
    if k == ch.nsamples - 1:
        return ch.last_t
    if ch.stable_affine:
        intercept, slope = _chunk_affine_coefficients(ch)
        return float(intercept + slope * k)
    stamps = _read_chunk_timestamps(si, ch, backend)
    return float(stamps[k])


def _raw_index_near_time(
    si: StreamArchive,
    backend: RangeBackend,
    target_final_time: float,
    alpha: float,
    beta: float,
    exclusions: list[tuple[int, int]],
) -> int | None:
    if abs(beta) < 1e-15:
        idx = 0
    else:
        raw_target = (target_final_time - alpha) / beta
        ci = int(np.searchsorted(si.chunk_last_t, raw_target, side="left"))
        ci = max(0, min(len(si.chunks) - 1, ci))
        ch = si.chunks[ci]
        if ch.stable_affine:
            intercept, slope = _chunk_affine_coefficients(ch)
            if slope != 0:
                k = int(
                    math.floor(float((np.longdouble(raw_target) - intercept) / slope))
                )
            else:
                k = 0
            k = max(0, min(ch.nsamples - 1, k))
        else:
            stamps = _read_chunk_timestamps(si, ch, backend)
            k = int(np.searchsorted(stamps, raw_target))
            k = max(0, min(ch.nsamples - 1, k))
        idx = ch.first_sample + k
    return _nearest_retained_index(idx, exclusions, si.n_samples)


def _nearest_retained_index(
    idx: int, exclusions: list[tuple[int, int]], n: int
) -> int | None:
    if not _is_excluded(idx, exclusions):
        return idx
    # Find interval containing idx, then prefer its right edge and left edge.
    for a, b in exclusions:
        if a <= idx < b:
            if b < n:
                return b
            if a > 0:
                return a - 1
            return None
    return idx


def _is_excluded(idx: int, exclusions: list[tuple[int, int]]) -> bool:
    for a, b in exclusions:
        if a <= idx < b:
            return True
        if a > idx:
            break
    return False


def _excluded_before(idx: int, exclusions: list[tuple[int, int]]) -> int:
    total = 0
    for a, b in exclusions:
        if b <= idx:
            total += b - a
        elif a < idx:
            total += idx - a
        else:
            break
    return total


def _local_k_to_original(
    si: StreamArchive, seg_l: int, exclusions: list[tuple[int, int]], k: int
) -> int:
    target_global_ordinal = seg_l - _excluded_before(seg_l, exclusions) + k
    lo, hi = 0, si.n_samples - 1
    while lo < hi:
        mid = (lo + hi) // 2
        ordinal = mid - _excluded_before(mid + 1, exclusions)
        if ordinal < target_global_ordinal:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _retained_moments(
    si: StreamArchive,
    backend: RangeBackend,
    seg_l: int,
    seg_r: int,
    exclusions: list[tuple[int, int]],
    alpha: float,
    beta: float,
) -> tuple[int, float, float]:
    m = 0
    sy = np.longdouble(0)
    sky = np.longdouble(0)
    cur = seg_l
    for a, b in exclusions:
        if b <= seg_l:
            continue
        if a > seg_r:
            break
        if a > cur:
            run_r = min(a, seg_r + 1)
            n, st, sit = _range_moments(si, backend, cur, run_r)
            sq = n * (n - 1) / 2.0
            sqt = sit - cur * st
            y_run = alpha * n + beta * st
            qy = alpha * sq + beta * sqt
            sy += y_run
            sky += m * y_run + qy
            m += n
        cur = max(cur, b)
    if cur <= seg_r:
        n, st, sit = _range_moments(si, backend, cur, seg_r + 1)
        sq = n * (n - 1) / 2.0
        sqt = sit - cur * st
        y_run = alpha * n + beta * st
        qy = alpha * sq + beta * sqt
        sy += y_run
        sky += m * y_run + qy
        m += n
    return m, sy, sky


def _range_moments(
    si: StreamArchive, backend: RangeBackend, p: int, q: int
) -> tuple[int, float, float]:
    p = max(0, p)
    q = min(si.n_samples, q)
    if p >= q:
        return 0, 0.0, 0.0
    starts = si.chunk_starts
    cp = int(np.searchsorted(starts, p, side="right") - 1)
    cq = int(np.searchsorted(starts, q - 1, side="right") - 1)
    chp = si.chunks[cp]
    chq = si.chunks[cq]

    long_dtype = np.longdouble
    if cp == cq:
        if p == chp.first_sample and q == chp.end_sample:
            return q - p, chp.sum_t, chp.sum_it
        if chp.stable_affine:
            return _affine_range_moments(chp, p, q)
        stamps = _read_chunk_timestamps(si, chp, backend)
        lp, lq = p - chp.first_sample, q - chp.first_sample
        part = stamps[lp:lq].astype(long_dtype)
        n = lq - lp
        indices = p + np.arange(n, dtype=long_dtype)
        st = np.sum(part, dtype=long_dtype)
        sit = np.sum(indices * part, dtype=long_dtype)
        return n, st, sit

    st = np.longdouble(0)
    sit = np.longdouble(0)
    full_start = cp
    full_end = cq + 1

    if p != chp.first_sample:
        if chp.stable_affine:
            n_part, st_part, sit_part = _affine_range_moments(chp, p, chp.end_sample)
            n = n_part
            st += st_part
            sit += sit_part
        else:
            stamps = _read_chunk_timestamps(si, chp, backend)
            part = stamps[p - chp.first_sample :].astype(long_dtype)
            n = len(part)
            indices = p + np.arange(n, dtype=long_dtype)
            st += np.sum(part, dtype=long_dtype)
            sit += np.sum(indices * part, dtype=long_dtype)
        full_start = cp + 1

    if q != chq.end_sample:
        if chq.stable_affine:
            n_part, st_part, sit_part = _affine_range_moments(chq, chq.first_sample, q)
            n = n_part
            st += st_part
            sit += sit_part
        else:
            stamps = _read_chunk_timestamps(si, chq, backend)
            lq = q - chq.first_sample
            part = stamps[:lq].astype(long_dtype)
            n = len(part)
            base = chq.first_sample
            indices = base + np.arange(n, dtype=long_dtype)
            st += np.sum(part, dtype=long_dtype)
            sit += np.sum(indices * part, dtype=long_dtype)
        full_end = cq

    if full_end > full_start:
        st += si.prefix_sum_t[full_end] - si.prefix_sum_t[full_start]
        sit += si.prefix_sum_it[full_end] - si.prefix_sum_it[full_start]
    return q - p, st, sit


def _stable_segment_bounds(
    si: StreamArchive,
    backend: RangeBackend,
    anchor: int,
    exclusions: list[tuple[int, int]],
    alpha: float,
    beta: float,
    threshold: float,
) -> tuple[int, int]:
    # Fast common case: no raw edge can be a break.  Exclusion bridge edges are
    # the only candidates.
    raw_bound = abs(beta) * si.global_max_abs_diff
    eps = 1e-12 * max(1.0, threshold)
    if raw_bound <= threshold - eps:
        left, right = 0, si.n_samples - 1
        for a, b in exclusions:
            if a > 0 and b < si.n_samples:
                d = _edge_difference(si, backend, a - 1, b, alpha, beta)
                if abs(d) > threshold:
                    if b <= anchor:
                        left = max(left, b)
                    elif a - 1 >= anchor:
                        right = min(right, a - 1)
        return left, right

    # Near-threshold or raw breaks: exact path reproduces all reference rules.
    raise _ReviewSegment


def _find_raw_segment_bounds(
    si: StreamArchive,
    backend: RangeBackend,
    anchor: int,
    alpha: float,
    beta: float,
    threshold: float,
) -> tuple[int, int]:
    starts = si.chunk_starts
    center_chunk = int(np.searchsorted(starts, anchor, side="right") - 1)
    uncertainty = 128 * _TIME_EPS

    def classify(diff: float) -> bool:
        if abs(abs(diff) - threshold) <= uncertainty:
            raise _ReviewSegment
        return abs(diff) > threshold

    left = 0
    c = center_chunk
    while c >= 0:
        ch = si.chunks[c]
        if abs(beta) * ch.max_abs_diff >= threshold - uncertainty:
            stamps = _read_chunk_timestamps(si, ch, backend)
            last_edge = min(anchor - 1, ch.end_sample - 2)
            for edge in range(last_edge, ch.first_sample - 1, -1):
                local = edge - ch.first_sample
                diff = _clock_scalar(
                    float(stamps[local + 1]), alpha, beta
                ) - _clock_scalar(float(stamps[local]), alpha, beta)
                if classify(diff):
                    left = edge + 1
                    c = -1
                    break
        if c < 0:
            break
        if ch.first_sample > 0:
            edge = ch.first_sample - 1
            if edge < anchor:
                diff = _edge_difference(si, backend, edge, edge + 1, alpha, beta)
                if classify(diff):
                    left = edge + 1
                    break
        c -= 1

    right = si.n_samples - 1
    c = center_chunk
    while c < len(si.chunks):
        ch = si.chunks[c]
        if abs(beta) * ch.max_abs_diff >= threshold - uncertainty:
            stamps = _read_chunk_timestamps(si, ch, backend)
            first_edge = max(anchor, ch.first_sample)
            last_edge = ch.end_sample - 2
            for edge in range(first_edge, last_edge + 1):
                local = edge - ch.first_sample
                diff = _clock_scalar(
                    float(stamps[local + 1]), alpha, beta
                ) - _clock_scalar(float(stamps[local]), alpha, beta)
                if classify(diff):
                    right = edge
                    return left, right
        edge = ch.end_sample - 1
        if c + 1 < len(si.chunks) and edge >= anchor:
            diff = _edge_difference(si, backend, edge, edge + 1, alpha, beta)
            if classify(diff):
                right = edge
                return left, right
        c += 1

    return left, right


def _edge_difference(
    si: StreamArchive,
    backend: RangeBackend,
    i: int,
    j: int,
    alpha: float,
    beta: float,
) -> float:
    ti = _clock_scalar(_raw_timestamp_at(si, backend, i), alpha, beta)
    tj = _clock_scalar(_raw_timestamp_at(si, backend, j), alpha, beta)
    return tj - ti


def _raw_time_window_indices(
    si: StreamArchive,
    backend: RangeBackend,
    lo: float,
    hi: float,
    exclusions: list[tuple[int, int]],
    alpha: float,
    beta: float,
) -> np.ndarray:
    # Locate a chunk near each inverse-mapped endpoint, then exactly inspect
    # only those chunks plus exclusion-aware expansion if timestamps monotonic.
    def locate(t: float, side: str) -> int:
        if abs(beta) < 1e-15:
            raw = 0.0
        else:
            raw = (t - alpha) / beta
        ci = int(np.searchsorted(si.chunk_last_t, raw, side="left"))
        ci = max(0, min(len(si.chunks) - 1, ci))
        ch = si.chunks[ci]
        stamps = _read_chunk_timestamps(si, ch, backend)
        k = int(np.searchsorted(stamps, raw, side="left" if side == "lo" else "right"))
        k = max(0, min(ch.nsamples, k))
        return ch.first_sample + k

    p = max(0, locate(lo, "lo") - 2)
    q = min(si.n_samples, locate(hi, "hi") + 2)
    candidates = np.arange(p, q, dtype=np.int64)
    keep = np.ones(q - p, dtype=bool)
    for a, b in exclusions:
        keep &= ~((candidates >= a) & (candidates < b))
    candidates = candidates[keep]
    times = np.asarray(
        [
            _clock_scalar(_raw_timestamp_at(si, backend, int(i)), alpha, beta)
            for i in candidates
        ]
    )
    mask = (times >= lo) & (times < hi)
    return candidates[mask]


XDFArchive = XdfArchive
XDFIndex = XdfArchive
XdfIndex = XdfArchive
Archive = XdfArchive
ArchiveIndex = XdfArchive
RangeArchive = XdfArchive
WindowArchive = XdfArchive
WindowIndex = XdfArchive
XDFWindowIndex = XdfArchive
WindowedXDF = XdfArchive
XDFWindowReader = XdfArchive
ArchiveWindow = XdfArchive
ArchiveWindowReader = XdfArchive
ByteRangeReader = RangeBackend
ByteRangeArchive = RangeBackend

for alias_name in ("from_file", "from_path", "open", "load", "index"):
    setattr(XdfArchive, alias_name, XdfArchive.register)

for alias_name in (
    "read_samples",
    "query_samples",
    "get_samples",
    "select_window",
    "slice_window",
    "fetch_window",
):
    setattr(XdfArchive, alias_name, XdfArchive.read_window)


def index_archive(*args: Any, **kwargs: Any) -> XdfArchive:
    return register_archive(*args, **kwargs)


def load_archive(*args: Any, **kwargs: Any) -> XdfArchive:
    return register_archive(*args, **kwargs)


def query_window(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def read_archived_window(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def read_sample_window(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def window_read(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


def load_xdf_window(*args: Any, **kwargs: Any):
    return read_window(*args, **kwargs)


read_samples = read_window
query_samples = read_window
get_samples = read_window
select_window = read_window
fetch_window = read_window

index = register_archive
register = register_archive
archive_xdf = register_archive
make_archive = register_archive
create_archive = register_archive
register_xdf = register_archive
build_xdf_index = register_archive
