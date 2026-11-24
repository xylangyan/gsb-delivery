"""Recover XDF recordings from damaged or incomplete replicas.

:func:`pyxdf.load_xdf` returns the observations a single file yields.  After a
power loss there can be several copies ("replicas") of the same recording, and
each copy can have unreadable regions.  This module resolves all replicas into
one recovery view and keeps three things apart for every observation:

* the original sample value,
* the device time (the time stamp stored by the recording device),
* the position on the unified, clock-synchronized timeline.

Every observation carries the volume identifier and the raw byte range it came
from.  :class:`RecoveryChecks` records which chunk boundaries, clock-offset
measurements and explicit time stamps were verified, so the replicas can be
checked against each other.

The entry point is :class:`RecoveryJob`.  Feed it one or more :class:`Replica`
objects and, when available, a :class:`RecordingArchive` that knows the sampling
period of each stream and which device restart changed the clock.  Resolving the
job returns a :class:`RecoveryView`.  Adding a replica later returns a
:class:`MergeReport` that states which layer (value, device time, unified time,
position or gap) changed.

Rules that the resolver enforces:

* A time stamp is only used as a predecessor if it was read from a fully framed
  chunk and verified.  A time stamp read next to a truncated value ("trial
  read") is kept as ``unverified_device_time`` and never used in a calculation.
* Omitted time stamps are deduced forward only, from the actual predecessor at
  the previous position, in the same stream and the same device-clock epoch.
* A later verified anchor never back-fills samples before it across a possible
  sample loss; those stay pending.
* Different streams and different device restarts never share a predecessor.
* A complete segment that arrives late is merged by original position, so the
  same original sample is not counted again as a new device record.
"""

from __future__ import annotations

import copy
import hashlib
import io
import logging
import struct
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Literal, Sequence
from xml.etree.ElementTree import ParseError, fromstring

import numpy as np

from .pyxdf import _robust_fit, _xml2dict, load_xdf

__all__ = [
    "ByteRange",
    "CheckRecord",
    "ClockEpoch",
    "ClockOffset",
    "CommittedWindow",
    "Conflict",
    "Evidence",
    "Gap",
    "LayerChange",
    "MergeReport",
    "Observation",
    "PyXDFComparison",
    "RawSample",
    "RecordingArchive",
    "RecoveryChecks",
    "RecoveryJob",
    "RecoveryView",
    "Replica",
    "ReplicaScan",
    "ScanIssue",
    "StreamSpec",
    "StreamView",
    "recover",
    "recovery_view",
    "scan_replica",
]

logger = logging.getLogger(__name__)

_MAGIC = b"XDF:"
_BOUNDARY = bytes(
    [
        0x43,
        0xA5,
        0x46,
        0xDC,
        0xCB,
        0xF5,
        0x41,
        0x0F,
        0xB3,
        0x0E,
        0xD5,
        0x46,
        0x73,
        0x83,
        0xCB,
        0xE4,
    ]
)

_CHANNEL_FORMATS = {
    "double64": np.dtype("<f8"),
    "float32": np.dtype("<f4"),
    "int64": np.dtype("<i8"),
    "int32": np.dtype("<i4"),
    "int16": np.dtype("<i2"),
    "int8": np.dtype("<i1"),
}

TimeStatus = Literal["explicit", "deduced", "pending"]
UnifiedStatus = Literal["synced", "uncalibrated", "pending"]
PositionStatus = Literal["anchored", "deduced", "tentative", "pending"]


# ---------------------------------------------------------------------------
# Byte-level provenance and checks


@dataclass(frozen=True, order=True)
class ByteRange:
    """A half-open byte range in one replica.

    ``end`` is exclusive, so ``length == end - start``.  Ranges from different
    volumes are never compared byte by byte; they are cross-checked through the
    original sample position and the sample value instead.
    """

    volume_id: str
    start: int
    end: int

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid byte range [{self.start}, {self.end})")

    @property
    def offset(self) -> int:
        return self.start

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, other: "ByteRange") -> bool:
        return (
            self.volume_id == other.volume_id
            and self.start <= other.start
            and other.end <= self.end
        )

    def overlaps(self, other: "ByteRange") -> bool:
        return (
            self.volume_id == other.volume_id
            and self.start < other.end
            and other.start < self.end
        )

    def __str__(self) -> str:
        return f"{self.volume_id}:[{self.start}, {self.end})"


@dataclass(frozen=True)
class Evidence:
    """One byte-level source supporting an observation."""

    volume_id: str
    byte_range: ByteRange
    kind: str
    verified: bool
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "volume_id": self.volume_id,
            "start": self.byte_range.start,
            "end": self.byte_range.end,
            "kind": self.kind,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CheckRecord:
    """Result of checking one boundary, clock offset or explicit timestamp."""

    kind: str
    volume_id: str
    byte_range: ByteRange
    verified: bool
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "volume_id": self.volume_id,
            "start": self.byte_range.start,
            "end": self.byte_range.end,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass
class RecoveryChecks:
    """All byte-level checks performed while scanning the replicas."""

    boundaries: list[CheckRecord] = field(default_factory=list)
    clock_offsets: list[CheckRecord] = field(default_factory=list)
    explicit_timestamps: list[CheckRecord] = field(default_factory=list)
    file_headers: list[CheckRecord] = field(default_factory=list)

    def extend(self, other: "RecoveryChecks") -> None:
        self.boundaries.extend(other.boundaries)
        self.clock_offsets.extend(other.clock_offsets)
        self.explicit_timestamps.extend(other.explicit_timestamps)
        self.file_headers.extend(other.file_headers)

    @property
    def all_verified(self) -> bool:
        records = (
            self.file_headers
            + self.boundaries
            + self.clock_offsets
            + self.explicit_timestamps
        )
        return all(record.verified for record in records)

    def summary(self) -> dict[str, tuple[int, int]]:
        """Return ``{kind: (verified, total)}`` for each check kind."""
        result = {}
        for name, records in (
            ("file_header", self.file_headers),
            ("boundary", self.boundaries),
            ("clock_offset", self.clock_offsets),
            ("explicit_timestamp", self.explicit_timestamps),
        ):
            result[name] = (sum(record.verified for record in records), len(records))
        return result

    def by_volume(self) -> dict[str, dict[str, tuple[int, int]]]:
        """Return the same summary split by volume identifier."""
        result: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
        for kind, records in (
            ("file_header", self.file_headers),
            ("boundary", self.boundaries),
            ("clock_offset", self.clock_offsets),
            ("explicit_timestamp", self.explicit_timestamps),
        ):
            volumes = defaultdict(list)
            for record in records:
                volumes[record.volume_id].append(record)
            for volume, items in volumes.items():
                result[volume][kind] = (
                    sum(item.verified for item in items),
                    len(items),
                )
        return dict(result)

    def unverified(self, kind: str | None = None) -> list[CheckRecord]:
        records = (
            self.file_headers
            + self.boundaries
            + self.clock_offsets
            + self.explicit_timestamps
        )
        return [
            record
            for record in records
            if not record.verified and (kind is None or record.kind == kind)
        ]


# ---------------------------------------------------------------------------
# Archive knowledge: sampling periods and device-clock epochs


@dataclass(frozen=True)
class ClockEpoch:
    """One device-clock epoch of a stream.

    A device restart starts a new epoch.  Time stamps from different epochs are
    never used to deduce each other.  ``first_sample_index`` is the fallback for
    assigning a sample to an epoch when its device time is not verified.

    ``device_origin`` and ``position_offset`` map a device time onto an original
    sample position::

        position = position_offset + round((device_time - device_origin) / tdiff)

    ``first_clock_index``/``last_clock_index`` select the clock-offset
    measurements that belong to this epoch.  ``calibration_complete`` may be set
    to ``False`` by the archive when it knows that a clock-offset measurement of
    the epoch is missing; in that case no sample of the epoch receives a unified
    time.
    """

    epoch_id: int | str
    stream_key: str | None = None
    first_sample_index: int | None = None
    start_device_time: float | None = None
    end_device_time: float | None = None
    position_offset: int = 0
    device_origin: float | None = None
    first_clock_index: int | None = None
    last_clock_index: int | None = None
    clock_time_start: float | None = None
    clock_time_end: float | None = None
    calibration_complete: bool | None = None

    def contains_time(self, value: float | None) -> bool | None:
        if value is None:
            return None
        if self.start_device_time is not None and value < self.start_device_time:
            return False
        if self.end_device_time is not None and value >= self.end_device_time:
            return False
        return True

    def contains_clock_time(self, value: float) -> bool | None:
        if self.clock_time_start is None and self.clock_time_end is None:
            return None
        if self.clock_time_start is not None and value < self.clock_time_start:
            return False
        if self.clock_time_end is not None and value >= self.clock_time_end:
            return False
        return True


@dataclass(frozen=True)
class StreamSpec:
    """Archive knowledge about one source stream.

    ``tdiff`` is the sampling period used by the device.  It is used to deduce
    omitted time stamps and to map a device time onto the original sample
    position.
    """

    stream_key: str
    tdiff: float
    name: str | None = None
    source_id: str | None = None
    uid: str | None = None
    stream_id: int | None = None
    position_origin: float | None = None
    device_origin: float | None = None
    epochs: tuple[ClockEpoch, ...] = ()
    can_drop_samples: bool = False
    nchannels: int | None = None
    channel_format: str | None = None

    def __post_init__(self):
        if self.tdiff < 0:
            raise ValueError("tdiff must not be negative")

    def default_epoch(self) -> ClockEpoch:
        return ClockEpoch(
            epoch_id=0,
            stream_key=self.stream_key,
            position_offset=int(self.position_origin or 0),
            device_origin=self.device_origin,
        )

    def epoch_for(
        self,
        sample_index: int,
        device_time: float | None,
        *,
        time_verified: bool = True,
    ) -> ClockEpoch:
        if not self.epochs:
            return self.default_epoch()
        if device_time is not None and time_verified:
            candidates = [
                epoch for epoch in self.epochs if epoch.contains_time(device_time)
            ]
            if candidates:
                return candidates[0]
        with_index = [
            epoch
            for epoch in self.epochs
            if epoch.first_sample_index is not None
            and epoch.first_sample_index <= sample_index
        ]
        if with_index:
            return with_index[-1]
        return self.epochs[0]

    def position_for(
        self,
        epoch: ClockEpoch,
        device_time: float,
        *,
        origin: float | None = None,
        offset: int | None = None,
    ) -> int | None:
        if self.tdiff <= 0:
            return None
        if origin is None:
            origin = epoch.device_origin if epoch.device_origin is not None else 0.0
        if offset is None:
            offset = epoch.position_offset
        return int(offset + round((device_time - origin) / self.tdiff))


@dataclass
class RecordingArchive:
    """Information the archive knows independently of the damaged replicas."""

    streams: dict[str, StreamSpec] | Iterable[StreamSpec]

    def __post_init__(self):
        if not isinstance(self.streams, dict):
            self.streams = {spec.stream_key: spec for spec in self.streams}

    def get(self, stream_key: str) -> StreamSpec | None:
        return self.streams.get(stream_key)

    def match(self, header: dict, stream_id: int | None) -> StreamSpec | None:
        info = header.get("info", {})
        values = {
            "uid": _first(info.get("uid")),
            "source_id": _first(info.get("source_id")),
            "name": _first(info.get("name")),
            "stream_id": stream_id,
        }
        for spec in self.streams.values():
            for key, value in values.items():
                expected = getattr(spec, key)
                if expected is not None and value is not None and expected == value:
                    return spec
        return self.streams.get(_stream_key_from_header(header, stream_id))

    def infer_spec(self, header: dict, stream_id: int | None) -> StreamSpec:
        """Return the archive spec for a header, or a spec inferred from it."""
        matched = self.match(header, stream_id)
        if matched is not None:
            return matched
        return _infer_spec(header, stream_id)


# ---------------------------------------------------------------------------
# Replicas, raw samples and the tolerant scanner


@dataclass
class Replica:
    """One physical copy of an XDF recording."""

    volume_id: str
    data: bytes
    path: Path | None = None
    name: str | None = None

    @classmethod
    def from_path(cls, volume_id: str, path: str | Path, name: str | None = None):
        path = Path(path)
        return cls(volume_id=volume_id, data=path.read_bytes(), path=path, name=name)

    @classmethod
    def from_bytes(cls, volume_id: str, data: bytes, name: str | None = None):
        return cls(volume_id=volume_id, data=bytes(data), name=name)

    def __post_init__(self):
        if not isinstance(self.data, (bytes, bytearray)):
            raise TypeError("replica data must be bytes")
        self.data = bytes(self.data)

    @property
    def size(self) -> int:
        return len(self.data)

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def byte_range(self, start: int, end: int) -> ByteRange:
        return ByteRange(self.volume_id, start, end)

    def open(self):
        return io.BytesIO(self.data)


@dataclass(frozen=True)
class ScanIssue:
    """A problem encountered while scanning a replica."""

    volume_id: str
    byte_range: ByteRange
    kind: str
    detail: str
    stream_key: str | None = None


@dataclass
class RawSample:
    """A sample as found in one replica, before cross-replica resolution."""

    volume_id: str
    stream_key: str
    stream_id: int | None
    ordinal: int
    epoch_id: int | str
    value: np.ndarray | tuple[str, ...] | None
    value_complete: bool
    device_time: float | None
    time_kind: str
    time_verified: bool
    unverified_device_time: float | None
    gap_id: int
    run_start: bool
    byte_range: ByteRange
    value_range: ByteRange | None
    time_range: ByteRange | None
    channel_format: str
    nchannels: int
    sample_verified: bool
    position: int | None = None
    position_status: PositionStatus = "pending"
    deduced_from: int | None = None
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class ClockOffset:
    """One ClockOffset measurement and the bytes it was read from."""

    time: float
    value: float
    volume_id: str
    byte_range: ByteRange
    verified: bool = True
    stream_key: str | None = None

    def close_to(self, other: "ClockOffset", atol: float = 1e-9) -> bool:
        return bool(
            np.isclose(self.time, other.time, atol=atol, rtol=0)
            and np.isclose(self.value, other.value, atol=atol, rtol=0)
        )


@dataclass
class ReplicaScan:
    """Everything a tolerant scan found in one replica."""

    replica: Replica
    streams: dict[str, dict] = field(default_factory=dict)
    specs: dict[str, StreamSpec] = field(default_factory=dict)
    stream_ids: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[RawSample]] = field(default_factory=dict)
    clock_offsets: dict[str, list[ClockOffset]] = field(default_factory=dict)
    chunks: list[dict] = field(default_factory=list)
    checks: RecoveryChecks = field(default_factory=RecoveryChecks)
    issues: list[ScanIssue] = field(default_factory=list)
    complete: bool = True
    fileheader: dict | None = None

    @property
    def volume_id(self) -> str:
        return self.replica.volume_id

    @property
    def stream_keys(self) -> tuple[str, ...]:
        return tuple(self.streams)

    def samples_for(self, stream_key: str) -> list[RawSample]:
        return self.samples.get(stream_key, [])


def _read_varlen_at(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise EOFError("unexpected end of file while reading a length")
    nbytes = data[pos]
    if nbytes == 1:
        if pos + 2 > len(data):
            raise EOFError("truncated variable-length integer")
        return data[pos + 1], 2
    if nbytes == 4:
        if pos + 5 > len(data):
            raise EOFError("truncated variable-length integer")
        return struct.unpack_from("<I", data, pos + 1)[0], 5
    if nbytes == 8:
        if pos + 9 > len(data):
            raise EOFError("truncated variable-length integer")
        return struct.unpack_from("<Q", data, pos + 1)[0], 9
    raise ValueError(f"invalid variable-length integer type {nbytes}")


def _looks_like_chunk(data: bytes, pos: int) -> bool:
    """Return True if ``pos`` starts a plausible XDF chunk or the file end."""
    if pos == len(data):
        return True
    try:
        nbytes, consumed = _read_varlen_at(data, pos)
    except (EOFError, ValueError):
        return False
    if nbytes < 2 or pos + consumed + nbytes > len(data):
        return False
    tag = struct.unpack_from("<H", data, pos + consumed)[0]
    return tag in (0, 1, 2, 3, 4, 5, 6)


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stream_key_from_header(header: dict, stream_id: int | None) -> str:
    info = header.get("info", {})
    uid = _first(info.get("uid"))
    if uid:
        return f"uid:{uid}"
    source_id = _first(info.get("source_id"))
    if source_id:
        return f"source:{source_id}"
    name = _first(info.get("name"))
    if name:
        return f"name:{name}"
    return f"stream_id:{stream_id}"


def _infer_spec(header: dict, stream_id: int | None) -> StreamSpec:
    info = header.get("info", {})
    srate = _as_float(_first(info.get("nominal_srate")), 0.0)
    return StreamSpec(
        stream_key=_stream_key_from_header(header, stream_id),
        tdiff=1.0 / srate if srate > 0 else 0.0,
        name=_first(info.get("name")),
        source_id=_first(info.get("source_id")),
        uid=_first(info.get("uid")),
        stream_id=stream_id,
        nchannels=_as_int(_first(info.get("channel_count"))),
        channel_format=_first(info.get("channel_format")),
    )


def _complete_spec(spec: StreamSpec, header: dict, stream_id: int | None) -> StreamSpec:
    """Fill attributes an archive spec may leave open from the stream header."""
    info = header.get("info", {})
    missing = {
        "nchannels": _as_int(_first(info.get("channel_count"))),
        "channel_format": _first(info.get("channel_format")),
        "stream_id": stream_id,
    }
    updates = {}
    for name, value in missing.items():
        if getattr(spec, name) is None and value is not None:
            updates[name] = value
    if updates:
        spec = replace(spec, **updates)
    return spec


def _find_boundary(data: bytes, start: int) -> int | None:
    index = data.find(_BOUNDARY, start)
    if index == -1:
        return None
    return index + len(_BOUNDARY)


def scan_replica(
    replica: Replica,
    archive: RecordingArchive | None = None,
) -> ReplicaScan:
    """Scan one replica without assuming that it is intact.

    Unreadable regions are never guessed over.  The scanner records the byte
    range of every chunk, sample, value and time stamp and starts a new sample
    run whenever it has to resynchronize.
    """
    data = replica.data
    scan = ReplicaScan(replica=replica)
    if not data.startswith(_MAGIC):
        scan.complete = False
        scan.issues.append(
            ScanIssue(
                replica.volume_id,
                ByteRange(replica.volume_id, 0, min(len(data), 4)),
                "magic",
                "file does not start with the XDF magic bytes",
            )
        )
        return scan

    pos = len(_MAGIC)
    stream_keys: dict[int, str] = {}
    specs: dict[str, StreamSpec] = {}
    ordinals: dict[str, int] = defaultdict(int)
    gap_ids: dict[str, int] = defaultdict(int)
    pending_gap: dict[str, bool] = defaultdict(bool)

    while pos < len(data):
        chunk_start = pos
        try:
            nbytes, consumed = _read_varlen_at(data, pos)
        except (EOFError, ValueError) as exc:
            scan.complete = False
            end = _find_boundary(data, pos + 1)
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, chunk_start, end or len(data)),
                    "resync",
                    f"could not read chunk length ({exc})",
                )
            )
            _mark_gap_all(stream_keys.values(), pending_gap)
            if end is None:
                break
            pos = end
            continue

        content_start = pos + consumed
        if content_start + 2 > len(data):
            scan.complete = False
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, chunk_start, len(data)),
                    "truncated",
                    "file ends inside a chunk tag",
                )
            )
            break
        tag = struct.unpack_from("<H", data, content_start)[0]
        cursor = content_start + 2
        stream_id = None
        if tag in (2, 3, 4, 6):
            if cursor + 4 > len(data):
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, chunk_start, len(data)),
                        "truncated",
                        "file ends inside a stream id",
                    )
                )
                break
            stream_id = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4

        chunk_end = pos + consumed + nbytes
        framed = chunk_end <= len(data)
        limit = min(chunk_end, len(data))
        boundary_verified = framed and _looks_like_chunk(data, chunk_end)
        chunk = {
            "tag": tag,
            "stream_id": stream_id,
            "start": chunk_start,
            "content_start": cursor,
            "end": limit,
            "declared_end": chunk_end,
            "framed": framed,
            "boundary_verified": boundary_verified,
        }
        scan.chunks.append(chunk)

        # A boundary is verified only if the declared length fits the file and
        # the next bytes are the end of the file or a plausible chunk.
        scan.checks.boundaries.append(
            CheckRecord(
                "boundary",
                replica.volume_id,
                ByteRange(replica.volume_id, chunk_start, limit),
                boundary_verified,
                None
                if boundary_verified
                else "declared chunk length does not lead to a plausible next chunk",
            )
        )
        if not boundary_verified:
            scan.complete = False

        stream_key = stream_keys.get(stream_id)
        if tag == 1:
            xml_bytes = data[cursor:limit]
            try:
                fileheader = _xml2dict(fromstring(xml_bytes.decode("utf-8", "replace")))
                chunk["fileheader"] = fileheader
                scan.fileheader = fileheader
            except (ParseError, ValueError) as exc:
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, cursor, limit),
                        "file_header",
                        f"could not parse file header XML ({exc})",
                    )
                )
            scan.checks.file_headers.append(
                CheckRecord(
                    "file_header",
                    replica.volume_id,
                    ByteRange(replica.volume_id, cursor, limit),
                    boundary_verified and "fileheader" in chunk,
                    None if "fileheader" in chunk else "file header XML unreadable",
                )
            )
        elif tag == 2:
            xml_bytes = data[cursor:limit]
            try:
                header = _xml2dict(fromstring(xml_bytes.decode("utf-8", "replace")))
            except (ParseError, ValueError) as exc:
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, cursor, limit),
                        "stream_header",
                        f"could not parse stream header XML ({exc})",
                    )
                )
                header = None
            if header is not None:
                key = _stream_key_from_header(header, stream_id)
                stream_keys[stream_id] = key
                spec = (
                    archive.infer_spec(header, stream_id)
                    if archive is not None
                    else _infer_spec(header, stream_id)
                )
                spec = _complete_spec(spec, header, stream_id)
                specs[key] = spec
                scan.streams.setdefault(key, header)
                scan.specs.setdefault(key, spec)
                scan.stream_ids.setdefault(key, stream_id)
                scan.samples.setdefault(key, [])
        elif tag == 3:
            if stream_key is None:
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, chunk_start, limit),
                        "orphan_samples",
                        f"samples for unknown stream id {stream_id}",
                    )
                )
            else:
                _scan_samples_chunk(
                    data,
                    replica,
                    scan,
                    specs[stream_key],
                    stream_key,
                    stream_id,
                    chunk_start,
                    cursor,
                    limit,
                    chunk_end,
                    framed,
                    boundary_verified,
                    ordinals,
                    gap_ids,
                    pending_gap,
                )
        elif tag == 4:
            if stream_key is None:
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, chunk_start, limit),
                        "orphan_clock_offset",
                        f"clock offset for unknown stream id {stream_id}",
                    )
                )
            else:
                _scan_clock_offset_chunk(
                    data,
                    replica,
                    scan,
                    stream_key,
                    cursor,
                    limit,
                    chunk_end,
                    framed,
                    boundary_verified,
                )
                if not boundary_verified:
                    pending_gap[stream_key] = True
        elif tag == 6 and stream_key is not None and framed:
            try:
                scan.streams[stream_key]["footer"] = _xml2dict(
                    fromstring(data[cursor:limit].decode("utf-8", "replace"))
                )
            except (ParseError, ValueError):
                scan.complete = False

        if not framed:
            scan.complete = False
            # An impossible declared length (larger than the remaining file)
            # is itself corruption.  Try to resynchronize at the next boundary
            # chunk instead of silently ending the scan; only when there is no
            # boundary left does the file really end here.
            end = _find_boundary(data, content_start)
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, chunk_start, end or len(data)),
                    "truncated" if end is None else "resync",
                    "chunk length extends past the end of the file",
                    stream_key,
                )
            )
            if end is None:
                _mark_gap_all(stream_keys.values(), pending_gap)
                break
            _mark_gap_all(stream_keys.values(), pending_gap)
            pos = end
            continue

        pos = chunk_end

    if not scan.chunks:
        scan.complete = False
    return scan


def _mark_gap_all(keys: Iterable[str], pending_gap: dict[str, bool]) -> None:
    for key in keys:
        pending_gap[key] = True


def _scan_samples_chunk(
    data,
    replica,
    scan,
    spec,
    stream_key,
    stream_id,
    chunk_start,
    cursor,
    limit,
    chunk_end,
    framed,
    boundary_verified,
    ordinals,
    gap_ids,
    pending_gap,
):
    try:
        nsamples, consumed = _read_varlen_at(data, cursor)
    except (EOFError, ValueError) as exc:
        scan.complete = False
        scan.issues.append(
            ScanIssue(
                replica.volume_id,
                ByteRange(replica.volume_id, cursor, limit),
                "samples_header",
                f"could not read sample count ({exc})",
                stream_key,
            )
        )
        pending_gap[stream_key] = True
        return

    pos = cursor + consumed
    fmt = spec.channel_format or "float32"
    nchannels = spec.nchannels or 1
    dtype = _CHANNEL_FORMATS.get(fmt)
    item_size = dtype.itemsize if dtype is not None else 0
    values: list[RawSample] = []
    parsed = 0
    all_complete = True
    # True while every sample so far in this chunk parsed completely, which
    # makes the start of the next sample unambiguous.
    sequence_intact = True

    for local in range(nsamples):
        if pending_gap[stream_key]:
            gap_ids[stream_key] += 1
            pending_gap[stream_key] = False
            run_start = True
        else:
            run_start = ordinals[stream_key] == 0
        sample_start = pos
        if pos >= limit:
            all_complete = False
            scan.complete = False
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, pos, limit),
                    "truncated_sample",
                    f"sample {local} of the samples chunk is missing",
                    stream_key,
                )
            )
            pending_gap[stream_key] = True
            break

        flag = data[pos]
        pos += 1
        time_start = None
        time_end = None
        device_time = None
        if flag:
            if pos + 8 > limit:
                all_complete = False
                scan.complete = False
                scan.issues.append(
                    ScanIssue(
                        replica.volume_id,
                        ByteRange(replica.volume_id, pos, limit),
                        "truncated_timestamp",
                        f"explicit time stamp of sample {local} is truncated",
                        stream_key,
                    )
                )
                time_start, time_end = pos, limit
                sequence_intact = False
                pending_gap[stream_key] = True
                break
            device_time = struct.unpack_from("<d", data, pos)[0]
            time_start, time_end = pos, pos + 8
            pos += 8

        value_start = pos
        value_complete = True
        value = None
        if fmt == "string":
            channels = []
            for _ in range(nchannels):
                try:
                    length, length_bytes = _read_varlen_at(data, pos)
                except (EOFError, ValueError):
                    value_complete = False
                    break
                pos += length_bytes
                if pos + length > limit:
                    value_complete = False
                    pos = limit
                    break
                channels.append(data[pos : pos + length].decode("utf-8", "replace"))
                pos += length
            if value_complete:
                value = tuple(channels)
        else:
            sample_bytes = nchannels * item_size
            if pos + sample_bytes > limit:
                value_complete = False
                pos = limit
            else:
                value = np.frombuffer(data, dtype=dtype, count=nchannels, offset=pos)
                value = value.astype(dtype.newbyteorder("="))
                pos += sample_bytes

        sample_end = pos
        # The start and the bytes of this sample are unambiguous because the
        # declared sample count and every earlier sample size fixed them.  The
        # chunk-level check below decides whether the chunk as a whole is
        # internally consistent; until then only value completeness is known.
        intact_value = value_complete
        values.append(
            RawSample(
                volume_id=replica.volume_id,
                stream_key=stream_key,
                stream_id=stream_id,
                ordinal=ordinals[stream_key],
                epoch_id=0,
                value=value,
                value_complete=value_complete,
                device_time=device_time,
                time_kind="explicit" if flag else "omitted",
                time_verified=bool(flag) and sequence_intact and value_complete,
                unverified_device_time=device_time,
                gap_id=gap_ids[stream_key],
                run_start=run_start,
                byte_range=ByteRange(replica.volume_id, sample_start, sample_end),
                value_range=(
                    ByteRange(replica.volume_id, value_start, sample_end)
                    if value_complete
                    else None
                ),
                time_range=(
                    ByteRange(replica.volume_id, time_start, time_end)
                    if time_start is not None
                    else None
                ),
                channel_format=fmt,
                nchannels=nchannels,
                sample_verified=sequence_intact and intact_value,
            )
        )
        ordinals[stream_key] += 1
        parsed += 1
        if not value_complete:
            all_complete = False
            scan.complete = False
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, value_start, limit),
                    "truncated_value",
                    f"value of sample {local} is truncated",
                    stream_key,
                )
            )
            sequence_intact = False
            # This sample itself is present and its position is unambiguous if
            # every earlier sample parsed.  Whether the rest of the chunk is
            # missing decides below whether a possible sample loss follows.
            break

    # Three cases at the end of a samples chunk:
    #
    # 1. The chunk parses exactly and its end is a plausible boundary.  Every
    #    sample is proven and the next sample continues the same run.
    # 2. The chunk parses exactly inside the file, but its end is not a
    #    plausible boundary.  The samples themselves are internally consistent
    #    (their byte starts were fixed by the declared count and sizes), so
    #    they stay usable; a possible sample loss starts *after* the chunk.
    # 3. The chunk does not parse exactly (truncated value, missing samples,
    #    wrong length).  The samples after the first inconsistency cannot be
    #    trusted, so their values are left as a gap.
    # A genuine samples chunk never contains a boundary signature.  If one
    # appears inside the declared payload, the declared length is wrong and the
    # bytes that "parsed" are not this chunk's samples.
    internal_boundary = data.find(_BOUNDARY, cursor, limit) != -1
    self_consistent = (
        framed and parsed == nsamples and pos == chunk_end and not internal_boundary
    )
    end_proven = self_consistent and boundary_verified
    if not self_consistent:
        scan.complete = False
        pending_gap[stream_key] = True
        if framed and parsed == nsamples and all_complete and pos != chunk_end:
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(
                        replica.volume_id, min(pos, chunk_end), max(pos, chunk_end)
                    ),
                    "chunk_length",
                    "declared chunk length does not match the parsed samples",
                    stream_key,
                )
            )
        if internal_boundary:
            # The declared prefix "parsed", but a boundary signature inside it
            # proves the length is wrong: none of those samples are this
            # chunk's data.
            scan.issues.append(
                ScanIssue(
                    replica.volume_id,
                    ByteRange(replica.volume_id, chunk_start, limit),
                    "internal_boundary",
                    "a boundary signature lies inside the declared chunk; its "
                    "length is wrong and none of its parsed samples are trusted",
                    stream_key,
                )
            )
            for sample in values:
                sample.value = None
                sample.value_complete = False
                sample.value_range = None
                sample.time_verified = False
                sample.sample_verified = False
                if sample.time_range is not None:
                    sample.unverified_device_time = sample.device_time
                sample.evidence = [
                    Evidence(
                        replica.volume_id,
                        sample.byte_range,
                        "sample",
                        False,
                        "chunk is not internally consistent",
                    )
                ]
        else:
            # End-of-file truncation or a missing tail: every sample whose own
            # bytes and whose predecessor chain are complete keeps its value;
            # only the damaged sample is left as a gap.
            for sample in values:
                if sample.sample_verified:
                    continue
                if sample.time_range is not None:
                    sample.unverified_device_time = sample.device_time
                sample.value = None
                sample.value_complete = False
                sample.value_range = None
                sample.time_verified = False
                sample.evidence = [
                    Evidence(
                        replica.volume_id,
                        sample.byte_range,
                        "sample",
                        False,
                        "value/sample could not be proven",
                    )
                ]
        scan.samples[stream_key].extend(values)
        return

    if not end_proven:
        # Internally consistent, but the next boundary is not proven.  A
        # possible loss (or a resynchronization) starts after this chunk, so
        # the run ends here.
        scan.complete = False
        scan.issues.append(
            ScanIssue(
                replica.volume_id,
                ByteRange(replica.volume_id, chunk_start, chunk_end),
                "unproven_boundary",
                "chunk end boundary is not proven; a possible sample loss starts "
                "after this chunk",
                stream_key,
            )
        )
        pending_gap[stream_key] = True

    # A sample's own bytes can still be proven when a later sample of the same
    # chunk is damaged: the start of this sample is unambiguous because every
    # preceding sample parsed completely.  The trial-read time stamp of the
    # damaged sample itself is never marked verified.
    for sample in values:
        if sample.time_kind == "explicit" and sample.time_range is not None:
            scan.checks.explicit_timestamps.append(
                CheckRecord(
                    "explicit_timestamp",
                    replica.volume_id,
                    sample.time_range,
                    sample.time_verified,
                    None
                    if sample.time_verified
                    else "time stamp could not be checked against a proven chunk",
                )
            )
        sample.evidence = [
            Evidence(
                replica.volume_id, sample.byte_range, "sample", sample.sample_verified
            )
        ]
        if sample.value_range is not None:
            sample.evidence.append(
                Evidence(
                    replica.volume_id,
                    sample.value_range,
                    "value",
                    sample.value_complete,
                )
            )
        if sample.time_range is not None:
            sample.evidence.append(
                Evidence(
                    replica.volume_id,
                    sample.time_range,
                    "device_time",
                    sample.time_verified,
                )
            )

    scan.samples[stream_key].extend(values)


def _scan_clock_offset_chunk(
    data,
    replica,
    scan,
    stream_key,
    cursor,
    limit,
    chunk_end,
    framed,
    boundary_verified,
):
    available = max(0, limit - cursor)
    if available < 16:
        scan.complete = False
        scan.issues.append(
            ScanIssue(
                replica.volume_id,
                ByteRange(replica.volume_id, cursor, limit),
                "truncated_clock_offset",
                "clock-offset chunk is shorter than 16 bytes",
                stream_key,
            )
        )
        scan.checks.clock_offsets.append(
            CheckRecord(
                "clock_offset",
                replica.volume_id,
                ByteRange(replica.volume_id, cursor, limit),
                False,
                "incomplete clock-offset measurement",
            )
        )
        return
    time_value, offset_value = struct.unpack_from("<dd", data, cursor)
    verified = (
        framed
        and boundary_verified
        and chunk_end <= len(data)
        and chunk_end - cursor == 16
    )
    clock = ClockOffset(
        time=time_value,
        value=offset_value,
        volume_id=replica.volume_id,
        byte_range=ByteRange(replica.volume_id, cursor, cursor + 16),
        verified=verified,
        stream_key=stream_key,
    )
    scan.clock_offsets.setdefault(stream_key, []).append(clock)
    scan.checks.clock_offsets.append(
        CheckRecord(
            "clock_offset",
            replica.volume_id,
            clock.byte_range,
            verified,
            None if verified else "clock-offset chunk framing is not proven",
        )
    )
    if not verified:
        scan.complete = False


# ---------------------------------------------------------------------------
# Resolved observations


@dataclass(frozen=True)
class Observation:
    """One original sample on the recovered, canonical timeline."""

    stream_key: str
    position: int
    ordinal: int
    epoch_id: int | str
    value: np.ndarray | tuple[str, ...] | None
    value_complete: bool
    device_time: float | None
    device_time_status: TimeStatus
    unverified_device_time: float | None
    sync_time: float | None
    unified_time: float | None
    unified_status: UnifiedStatus
    position_status: PositionStatus
    predecessor_position: int | None
    evidence: tuple[Evidence, ...]
    source_volume: str
    anchor_position: int | None = None
    anchor_offset: int | None = None
    gaps: tuple[int, ...] = ()
    conflicts: tuple[int, ...] = ()

    def value_equals(self, other) -> bool:
        if not isinstance(other, Observation):
            return False
        if self.value_complete != other.value_complete:
            return False
        if self.value is None or other.value is None:
            return self.value is other.value
        if isinstance(self.value, np.ndarray):
            return np.array_equal(self.value, other.value)
        return tuple(self.value) == tuple(other.value)

    def copy(self) -> "Observation":
        value = self.value.copy() if isinstance(self.value, np.ndarray) else self.value
        return replace(self, value=value, evidence=tuple(self.evidence))

    def as_dict(self) -> dict:
        return {
            "stream_key": self.stream_key,
            "position": self.position,
            "ordinal": self.ordinal,
            "epoch_id": self.epoch_id,
            "value": self.value.tolist()
            if isinstance(self.value, np.ndarray)
            else list(self.value)
            if self.value is not None
            else None,
            "value_complete": self.value_complete,
            "device_time": self.device_time,
            "device_time_status": self.device_time_status,
            "unverified_device_time": self.unverified_device_time,
            "sync_time": self.sync_time,
            "unified_time": self.unified_time,
            "unified_status": self.unified_status,
            "position_status": self.position_status,
            "predecessor_position": self.predecessor_position,
            "evidence": [item.as_dict() for item in self.evidence],
            "gaps": self.gaps,
            "conflicts": self.conflicts,
        }


@dataclass(frozen=True)
class Gap:
    """A region whose value, device time or unified time cannot be proven."""

    gap_id: int
    stream_key: str
    kind: str
    start_position: int | None
    end_position: int | None
    reason: str
    volumes: tuple[str, ...] = ()
    evidence: tuple[ByteRange, ...] = ()

    def as_dict(self) -> dict:
        return {
            "gap_id": self.gap_id,
            "stream_key": self.stream_key,
            "kind": self.kind,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "reason": self.reason,
            "volumes": self.volumes,
            "evidence": [str(item) for item in self.evidence],
        }


@dataclass(frozen=True)
class Conflict:
    """A disagreement between replicas that could not be resolved."""

    conflict_id: int
    stream_key: str
    position: int
    layer: str
    values: tuple[str, ...]
    volumes: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "conflict_id": self.conflict_id,
            "stream_key": self.stream_key,
            "position": self.position,
            "layer": self.layer,
            "values": self.values,
            "volumes": self.volumes,
        }


@dataclass
class StreamView:
    """Recovered view of one source stream, ordered by original position."""

    stream_key: str
    spec: StreamSpec
    observations: list[Observation]
    gaps: list[Gap]
    conflicts: list[Conflict]
    epochs: tuple[ClockEpoch, ...] = ()
    clock_offsets: tuple[ClockOffset, ...] = ()

    def __iter__(self):
        return iter(self.observations)

    def __getitem__(self, index):
        return self.observations[index]

    def __len__(self):
        return len(self.observations)

    @property
    def positions(self) -> np.ndarray:
        return np.array([o.position for o in self.observations], dtype=np.int64)

    @property
    def values(self) -> list:
        return [o.value for o in self.observations]

    @property
    def device_times(self) -> np.ndarray:
        return np.array(
            [np.nan if o.device_time is None else o.device_time for o in self],
            dtype=float,
        )

    @property
    def unified_times(self) -> np.ndarray:
        return np.array(
            [np.nan if o.unified_time is None else o.unified_time for o in self],
            dtype=float,
        )

    def at_position(self, position: int) -> Observation | None:
        for observation in self.observations:
            if observation.position == position:
                return observation
        return None

    def window(
        self,
        start: float | int | None = None,
        end: float | int | None = None,
        *,
        by: str = "position",
    ) -> list[Observation]:
        if by not in ("position", "unified_time"):
            raise ValueError("by must be 'position' or 'unified_time'")
        selected = []
        for observation in self.observations:
            value = (
                observation.position if by == "position" else observation.unified_time
            )
            if value is None:
                continue
            if start is not None and value < start:
                continue
            if end is not None and value >= end:
                continue
            selected.append(observation)
        return selected

    def pending_times(self) -> list[Observation]:
        return [o for o in self if o.device_time_status == "pending"]


@dataclass
class PyXDFComparison:
    """Comparison of a complete replica against ordinary pyxdf loading."""

    volume_id: str
    stream_key: str
    complete_input: bool
    pyxdf_samples: int
    recovered_samples: int
    values_match: bool
    sync_time_matches: bool
    unified_time_matches: bool
    max_sync_time_difference: float | None
    max_unified_time_difference: float | None
    mismatched_positions: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "volume_id": self.volume_id,
            "stream_key": self.stream_key,
            "complete_input": self.complete_input,
            "pyxdf_samples": self.pyxdf_samples,
            "recovered_samples": self.recovered_samples,
            "values_match": self.values_match,
            "sync_time_matches": self.sync_time_matches,
            "unified_time_matches": self.unified_time_matches,
            "max_sync_time_difference": self.max_sync_time_difference,
            "max_unified_time_difference": self.max_unified_time_difference,
            "mismatched_positions": self.mismatched_positions,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CommittedWindow:
    """A window that has entered the repository's unified timeline.

    The selected observations are frozen; later replica merges cannot change
    what :meth:`requery` returns.
    """

    revision: int
    stream_key: str
    key: str
    start: float | int | None
    end: float | int | None
    by: str
    samples: tuple[Observation, ...]
    digest: str

    @classmethod
    def create(
        cls,
        revision: int,
        stream_key: str,
        start,
        end,
        by: str,
        observations: Sequence[Observation],
    ) -> "CommittedWindow":
        frozen = tuple(observation.copy() for observation in observations)
        digest = hashlib.sha256()
        for observation in frozen:
            digest.update(observation.stream_key.encode())
            digest.update(struct.pack("<q", observation.position))
            digest.update(
                np.asarray(observation.value).tobytes()
                if isinstance(observation.value, np.ndarray)
                else repr(observation.value).encode()
            )
            digest.update(
                struct.pack(
                    "<dd",
                    np.nan
                    if observation.device_time is None
                    else observation.device_time,
                    np.nan
                    if observation.unified_time is None
                    else observation.unified_time,
                )
            )
        key = f"{stream_key}|{by}|{start!r}|{end!r}"
        return cls(
            revision=revision,
            stream_key=stream_key,
            key=key,
            start=start,
            end=end,
            by=by,
            samples=frozen,
            digest=digest.hexdigest(),
        )

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self):
        return iter(self.samples)

    def requery(self) -> "CommittedWindow":
        """Return the frozen version, even if a newer revision exists."""
        return copy.deepcopy(self)

    @property
    def positions(self) -> tuple[int, ...]:
        return tuple(sample.position for sample in self.samples)


@dataclass
class RecoveryView:
    """The result of resolving all replicas."""

    revision: int
    replicas: tuple[str, ...]
    streams: dict[str, StreamView]
    checks: RecoveryChecks
    gaps: list[Gap]
    conflicts: list[Conflict]
    scans: dict[str, ReplicaScan] = field(default_factory=dict)
    pyxdf_checks: list[PyXDFComparison] = field(default_factory=list)
    committed: dict[str, CommittedWindow] = field(default_factory=dict)

    def stream(self, stream_key: str) -> StreamView:
        try:
            return self.streams[stream_key]
        except KeyError as exc:
            raise KeyError(
                f"unknown stream {stream_key!r}; available: {sorted(self.streams)}"
            ) from exc

    def at(self, stream_key: str, position: int) -> Observation | None:
        return self.stream(stream_key).at_position(position)

    def window(self, stream_key: str, start=None, end=None, *, by="position"):
        return self.stream(stream_key).window(start, end, by=by)

    def gaps_for(self, stream_key: str) -> list[Gap]:
        return [gap for gap in self.gaps if gap.stream_key == stream_key]

    def conflicts_for(self, stream_key: str) -> list[Conflict]:
        return [
            conflict for conflict in self.conflicts if conflict.stream_key == stream_key
        ]

    def evidence(self, stream_key: str, position: int) -> tuple[Evidence, ...]:
        observation = self.at(stream_key, position)
        return () if observation is None else observation.evidence

    def commit_window(
        self,
        stream_key: str,
        start=None,
        end=None,
        *,
        by: str = "position",
    ) -> CommittedWindow:
        """Freeze the current selection so it can be re-queried unchanged."""
        observations = self.window(stream_key, start, end, by=by)
        window = CommittedWindow.create(
            self.revision, stream_key, start, end, by, observations
        )
        self.committed[window.key] = window
        return window

    def summary(self) -> dict:
        return {
            "revision": self.revision,
            "replicas": self.replicas,
            "streams": {
                key: {
                    "samples": len(stream),
                    "gaps": len(stream.gaps),
                    "conflicts": len(stream.conflicts),
                }
                for key, stream in self.streams.items()
            },
            "checks": self.checks.summary(),
        }


# ---------------------------------------------------------------------------
# Resolution: raw samples -> original positions, device time, unified time


def _value_key(value):
    if isinstance(value, np.ndarray):
        return np.asarray(value).tobytes()
    if isinstance(value, tuple):
        return value
    return value


def _epoch_starts_between(spec: StreamSpec, low: int, high: int) -> bool:
    for epoch in spec.epochs:
        if (
            epoch.first_sample_index is not None
            and low < epoch.first_sample_index <= high
        ):
            return True
    return False


def _raw_epochs(raws: list[RawSample], spec: StreamSpec) -> list[ClockEpoch]:
    """Assign a device-clock epoch to every raw sample, in source order.

    A sample with a verified explicit time is assigned by that time.  A sample
    without one keeps the epoch of the sample before it only while no archive
    epoch starts between the two; a device restart always breaks the chain,
    because the two clocks are unrelated.
    """
    epochs: list[ClockEpoch] = []
    previous: ClockEpoch | None = None
    previous_ordinal: int | None = None
    previous_gap: int | None = None
    for raw in raws:
        if raw.time_verified and raw.device_time is not None:
            epoch = spec.epoch_for(raw.ordinal, raw.device_time, time_verified=True)
        elif (
            previous is not None
            and previous_gap == raw.gap_id
            and previous_ordinal is not None
            and not _epoch_starts_between(spec, previous_ordinal, raw.ordinal)
        ):
            epoch = previous
        else:
            epoch = spec.epoch_for(raw.ordinal, None, time_verified=False)
        epochs.append(epoch)
        previous = epoch
        previous_ordinal = raw.ordinal
        previous_gap = raw.gap_id
    return epochs


def _runs(raws: list[RawSample], epochs: list[ClockEpoch]) -> list[tuple[int, int]]:
    """Split samples into runs with no possible sample loss between them."""
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(raws)):
        if (
            raws[index].gap_id != raws[index - 1].gap_id
            or epochs[index].epoch_id != epochs[index - 1].epoch_id
        ):
            runs.append((start, index))
            start = index
    if raws:
        runs.append((start, len(raws)))
    return runs


def _is_anchor(raw: RawSample) -> bool:
    return raw.time_verified and raw.device_time is not None


def _epoch_axis(
    spec: StreamSpec,
) -> dict[int | str, tuple[float, int]]:
    """Return ``{epoch_id: (device_origin, position_offset)}`` for a stream.

    The axis is fixed by the archive, never by whichever replicas happen to be
    present: otherwise adding a replica with an earlier anchor would shift the
    positions of samples that were already delivered.  When no origin is given,
    the device time itself is the coordinate (origin ``0.0``), so a position is
    a deterministic function of the device clock.
    """
    axis: dict[int | str, tuple[float, int]] = {}
    for epoch in spec.epochs or (spec.default_epoch(),):
        if epoch.device_origin is not None:
            origin = epoch.device_origin
        elif spec.device_origin is not None:
            origin = spec.device_origin
        else:
            origin = 0.0
        axis[epoch.epoch_id] = (origin, epoch.position_offset)
    return axis


def _resolve_scan_stream(
    scan: ReplicaScan,
    spec: StreamSpec,
    axis: dict[int | str, tuple[float, int]],
    *,
    strict_first: bool,
) -> list[Observation]:
    raws = scan.samples.get(spec.stream_key, [])
    if not raws:
        return []
    epochs = _raw_epochs(raws, spec)
    result: dict[int, Observation] = {}
    tdiff = spec.tdiff

    for run_start, run_stop in _runs(raws, epochs):
        run = list(range(run_start, run_stop))
        epoch = epochs[run[0]]
        origin, offset = axis.get(epoch.epoch_id, (0.0, epoch.position_offset))

        # Forward pass.  A sample's device time is deduced only from the
        # immediately preceding sample of the same run and epoch, and only if
        # that predecessor has a verified or already deduced device time.  A
        # trial-read (unverified) explicit time never enters the calculation
        # and breaks the chain for every following omitted time.
        last_position: int | None = None
        last_time: float | None = None
        forward: dict[int, dict] = {}
        for index in run:
            raw = raws[index]
            info = {
                "position": None,
                "position_status": "pending",
                "device_time": None,
                "device_status": "pending",
                "predecessor": None,
            }
            if _is_anchor(raw):
                info.update(
                    device_time=raw.device_time,
                    device_status="explicit",
                )
                position = spec.position_for(
                    epoch, raw.device_time, origin=origin, offset=offset
                )
                if position is not None:
                    info.update(position=position, position_status="anchored")
                    last_position = position
                # An irregular stream has no sampling period, so it has no
                # position axis; the verified time is still usable and simply
                # keeps the sample's observed order as its position.
                last_time = raw.device_time
            elif raw.time_kind == "omitted":
                if last_position is not None and last_time is not None and tdiff > 0:
                    info.update(
                        position=last_position + 1,
                        position_status="deduced",
                        device_time=last_time + tdiff,
                        device_status="deduced",
                        predecessor=last_position,
                    )
                    last_position += 1
                    last_time = last_time + tdiff
                elif last_position is not None:
                    # Position is known from the preceding sample, but its
                    # time is not verified, so this time stays pending.
                    last_position += 1
                    info["position"] = last_position
                    info["position_status"] = "tentative"
                    info["predecessor"] = last_position - 1
            else:
                # Explicit but unverified ("trial read"): keep the value, keep
                # the observed time for review, but do not use it anywhere.
                if last_position is not None:
                    last_position += 1
                    info["position"] = last_position
                    info["position_status"] = "tentative"
                    info["predecessor"] = last_position - 1
                last_time = None
            forward[index] = info

        # A verified anchor gives positions to the leading block of its run.
        # That is safe because a run is gap-free by construction, so no sample
        # can be missing between the run start and the anchor.  It never gives
        # those samples a device time: an anchor may not be used to extrapolate
        # backwards in time, only to place positions.
        anchor_indices = [
            index for index in run if forward[index]["position_status"] == "anchored"
        ]
        if anchor_indices:
            first_anchor = anchor_indices[0]
            anchor_position = forward[first_anchor]["position"]
            for index in range(run_start, first_anchor):
                info = forward[index]
                if info["position"] is None:
                    info["position"] = anchor_position - (first_anchor - index)
                    info["position_status"] = "tentative"
            # Anchor-relative offset within the run.  Another replica that
            # holds the same original sample but only a tentative position for
            # it can be aligned through this offset instead of being counted as
            # a second record.
            for index in run:
                forward[index]["anchor_position"] = anchor_position
                forward[index]["anchor_offset"] = index - first_anchor
        for index in run:
            raw = raws[index]
            info = forward[index]
            info.setdefault("anchor_position", None)
            info.setdefault("anchor_offset", None)
            if info["position"] is None:
                # A run without any anchor.  The value is delivered in observed
                # order, but it is not claimed to be on the position axis.
                info["position"] = raw.ordinal
                info["position_status"] = "pending"

        for index in run:
            raw = raws[index]
            info = forward[index]
            if info["position"] is None:
                # Should not happen, but never drop a value just because its
                # original position is unknown.
                info["position"] = raw.ordinal
                info["position_status"] = "pending"
            observation = Observation(
                stream_key=spec.stream_key,
                position=int(info["position"]),
                ordinal=raw.ordinal,
                epoch_id=epoch.epoch_id,
                value=raw.value,
                value_complete=raw.value_complete,
                device_time=info["device_time"],
                device_time_status=info["device_status"],
                unverified_device_time=raw.unverified_device_time,
                sync_time=None,
                unified_time=None,
                unified_status="pending",
                position_status=info["position_status"],
                predecessor_position=info["predecessor"],
                anchor_position=info["anchor_position"],
                anchor_offset=info["anchor_offset"],
                evidence=tuple(raw.evidence),
                source_volume=raw.volume_id,
            )
            existing = result.get(observation.position)
            if existing is None or _observation_preference(observation, existing) > 0:
                result[observation.position] = observation

    return [result[position] for position in sorted(result)]


def _observation_preference(candidate: Observation, current: Observation) -> int:
    """Prefer more complete values and more strongly verified times."""
    value_rank = {"pending": 0, "tentative": 1, "deduced": 2, "anchored": 3}
    score = 0
    if candidate.value_complete and not current.value_complete:
        score += 4
    elif current.value_complete and not candidate.value_complete:
        score -= 4
    score += value_rank[candidate.position_status] - value_rank[current.position_status]
    if (
        candidate.device_time_status == "explicit"
        and current.device_time_status != "explicit"
    ):
        score += 2
    elif (
        candidate.device_time_status != "explicit"
        and current.device_time_status == "explicit"
    ):
        score -= 2
    return score


def _merge_values(
    candidates: Sequence[Observation],
) -> tuple[Observation, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
    """Choose the best observation for one position and report conflicts."""
    ordered = sorted(
        candidates,
        key=lambda obs: (
            not obs.value_complete,
            obs.value is None,
            {"explicit": 0, "deduced": 1, "pending": 2}[obs.device_time_status],
            {"anchored": 0, "deduced": 1, "tentative": 2, "pending": 3}[
                obs.position_status
            ],
            obs.source_volume,
        ),
    )
    best = ordered[0]
    conflicts: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    complete = [obs for obs in ordered if obs.value_complete and obs.value is not None]
    if complete:
        keys = {_value_key(obs.value) for obs in complete}
        if len(keys) > 1:
            distinct = []
            for obs in complete:
                if _value_key(obs.value) not in [_value_key(item) for item in distinct]:
                    distinct.append(obs.value)
            conflicts.append(
                (
                    "value",
                    tuple(repr(value) for value in distinct),
                    tuple(obs.source_volume for obs in complete),
                )
            )

    explicit = [
        obs.device_time
        for obs in ordered
        if obs.device_time_status == "explicit" and obs.device_time is not None
    ]
    if len(explicit) > 1 and not np.allclose(explicit, explicit[0], atol=1e-9, rtol=0):
        conflicts.append(
            (
                "device_time",
                tuple(repr(value) for value in explicit),
                tuple(
                    obs.source_volume
                    for obs in ordered
                    if obs.device_time_status == "explicit"
                ),
            )
        )

    # Merge the strongest evidence from all replicas for this position.
    evidence_by_range: dict[tuple[str, int, int], Evidence] = {}
    for obs in ordered:
        for item in obs.evidence:
            key = (item.volume_id, item.byte_range.start, item.byte_range.end)
            previous = evidence_by_range.get(key)
            if previous is None or (item.verified and not previous.verified):
                evidence_by_range[key] = item
    best = replace(best, evidence=tuple(evidence_by_range.values()))
    return best, conflicts


def _clock_offsets_for_epoch(
    spec: StreamSpec, epoch: ClockEpoch, offsets: Sequence[ClockOffset]
) -> list[ClockOffset]:
    """Select verified clock offsets for an epoch (by index or by time)."""
    if epoch.first_clock_index is not None or epoch.last_clock_index is not None:
        start = epoch.first_clock_index or 0
        stop = (
            epoch.last_clock_index + 1
            if epoch.last_clock_index is not None
            else len(offsets)
        )
        return [offset for offset in offsets[start:stop] if offset.verified]
    return [
        offset
        for offset in offsets
        if offset.verified and epoch.contains_clock_time(offset.time) is not False
    ]


def _fit_clock(offsets: Sequence[ClockOffset], winsor_threshold=1e-4):
    """Fit ``offset(t) = a + b * t`` and return the coefficients or ``None``."""
    if not offsets:
        return None
    values = np.array([offset.value for offset in offsets], dtype=float)
    times = np.array([offset.time for offset in offsets], dtype=float)
    if len(offsets) == 1:
        return float(values[0]), 0.0
    X = np.column_stack([np.ones(len(times)), times / winsor_threshold])
    y = values / winsor_threshold
    try:
        coefficients = _robust_fit(X, y)
    except np.linalg.LinAlgError:
        return None
    coefficients = coefficients.copy()
    coefficients[0] *= winsor_threshold
    return float(coefficients[0]), float(coefficients[1])


def _apply_sync(
    observation: Observation,
    coefficients: tuple[float, float] | None,
    calibration_complete: bool | None,
) -> Observation:
    if observation.device_time is None:
        return replace(
            observation, sync_time=None, unified_time=None, unified_status="pending"
        )
    if coefficients is None or calibration_complete is False:
        return replace(
            observation,
            sync_time=None,
            unified_time=None,
            unified_status="uncalibrated",
        )
    intercept, slope = coefficients
    synced = observation.device_time + intercept + slope * observation.device_time
    return replace(
        observation,
        sync_time=synced,
        unified_time=synced,
        unified_status="synced",
    )


def _dejitter_observations(
    observations: list[Observation], spec: StreamSpec
) -> list[Observation]:
    """Apply the same regular-sampling fit that pyxdf uses for dejittering.

    Only unified times change.  Segments are split exactly like
    :func:`pyxdf.pyxdf._detect_breaks`: by a synchronized time difference above
    ``max(1 s, 500 * tdiff)``.  Samples without a unified time also split a
    segment, because no fit can place them.
    """
    if spec.tdiff <= 0 or spec.can_drop_samples or len(observations) < 2:
        return observations
    observations = list(observations)
    threshold = max(1.0, 500 * spec.tdiff)
    segments: list[list[int]] = []
    current: list[int] = []
    for index, observation in enumerate(observations):
        if observation.unified_time is None:
            if current:
                segments.append(current)
            current = []
            continue
        if current:
            previous = observations[current[-1]]
            if (
                abs(observation.unified_time - previous.unified_time) > threshold
                or observation.epoch_id != previous.epoch_id
            ):
                segments.append(current)
                current = []
        current.append(index)
    if current:
        segments.append(current)

    for segment in segments:
        if len(segment) < 2:
            continue
        y = np.array([observations[i].unified_time for i in segment], dtype=float)
        n = len(y)
        idx = np.arange(n) - (n - 1) / 2
        y_mean = np.mean(y)
        slope = np.dot(idx, y - y_mean) / (n * (n**2 - 1) / 12)
        for offset, value in enumerate(y_mean + slope * idx):
            position = segment[offset]
            observations[position] = replace(
                observations[position], unified_time=float(value)
            )
    return observations


def _make_gaps_and_conflicts(
    stream_key: str,
    observations: list[Observation],
    conflict_records: list[tuple[int, str, tuple[str, ...], tuple[str, ...]]],
    next_gap_id: int,
    next_conflict_id: int,
    *,
    samples_lost: bool = False,
) -> tuple[list[Gap], list[Conflict]]:
    gaps: list[Gap] = []
    if samples_lost and observations:
        # Positions between two anchored samples are missing original samples.
        # They are reported as one gap, so a reader can see that the timeline
        # has a hole and does not silently treat neighbouring samples as
        # consecutive.
        anchored = [
            observation
            for observation in observations
            if observation.position_status == "anchored"
        ]
        anchored.sort(key=lambda observation: observation.position)
        observed = {observation.position for observation in observations}
        for left, right in zip(anchored, anchored[1:]):
            # Positions strictly between the two anchors that carry no
            # observation at all are missing original samples.  Samples that
            # were deduced after the left anchor are observed and stay outside
            # the gap.
            missing = [
                position
                for position in range(left.position + 1, right.position)
                if position not in observed
            ]
            if not missing:
                continue
            gaps.append(
                Gap(
                    next_gap_id,
                    stream_key,
                    "sample",
                    min(missing),
                    max(missing),
                    "original samples are missing between two anchored samples",
                    (left.source_volume, right.source_volume),
                )
            )
            next_gap_id += 1
    for observation in observations:
        if not observation.value_complete or observation.value is None:
            gaps.append(
                Gap(
                    next_gap_id,
                    stream_key,
                    "value",
                    observation.position,
                    observation.position,
                    "no replica supplies a complete value",
                    (observation.source_volume,),
                )
            )
            next_gap_id += 1
        if observation.device_time_status == "pending":
            gaps.append(
                Gap(
                    next_gap_id,
                    stream_key,
                    "device_time",
                    observation.position,
                    observation.position,
                    "no verifiable predecessor for this omitted time stamp",
                    (observation.source_volume,),
                )
            )
            next_gap_id += 1
        if observation.unified_status != "synced":
            gaps.append(
                Gap(
                    next_gap_id,
                    stream_key,
                    "unified_time",
                    observation.position,
                    observation.position,
                    "clock calibration is unavailable or incomplete for this epoch",
                    (observation.source_volume,),
                )
            )
            next_gap_id += 1

    # Contiguous runs of missing positions are one gap each.
    merged: list[Gap] = []
    for gap in gaps:
        if (
            merged
            and merged[-1].kind == gap.kind
            and merged[-1].end_position is not None
            and gap.start_position is not None
            and gap.start_position == merged[-1].end_position + 1
        ):
            previous = merged.pop()
            merged.append(
                replace(
                    previous,
                    end_position=gap.end_position,
                    volumes=tuple(dict.fromkeys(previous.volumes + gap.volumes)),
                )
            )
        else:
            merged.append(gap)

    conflicts = [
        Conflict(next_conflict_id + index, stream_key, position, layer, values, volumes)
        for index, (position, layer, values, volumes) in enumerate(conflict_records)
    ]
    return merged, conflicts


@dataclass
class _StreamResolution:
    stream_key: str
    spec: StreamSpec
    observations: list[Observation]
    gaps: list[Gap]
    conflicts: list[Conflict]
    clock_offsets: tuple[ClockOffset, ...]
    epochs: tuple[ClockEpoch, ...]


def _resolve_stream(
    stream_key: str,
    scans: Sequence[ReplicaScan],
    specs: Sequence[StreamSpec],
    dejitter: bool,
    *,
    primary_volume: str | None = None,
) -> _StreamResolution:
    spec = next((item for item in specs if item is not None), None)
    if spec is None:
        spec = StreamSpec(stream_key=stream_key, tdiff=0.0)

    if primary_volume is None and scans:
        primary_volume = scans[0].volume_id
    axis = _epoch_axis(spec)

    per_replica = []
    for scan, scan_spec in zip(scans, specs):
        if scan_spec is None:
            continue
        per_replica.append(
            _resolve_scan_stream(
                scan,
                scan_spec,
                axis,
                strict_first=scan.volume_id != primary_volume,
            )
        )

    # Merge clock offsets from all replicas.  Unverified offsets never enter a
    # calibration; they are still listed with their byte ranges.
    all_offsets: list[ClockOffset] = []
    for scan in scans:
        for offset in scan.clock_offsets.get(stream_key, []):
            if not any(offset.close_to(existing) for existing in all_offsets):
                all_offsets.append(offset)
    all_offsets.sort(key=lambda offset: (offset.time, offset.volume_id))

    epochs = tuple(spec.epochs) if spec.epochs else (spec.default_epoch(),)

    # Fit one clock correction per epoch.
    fits: dict[int | str, tuple[float, float] | None] = {}
    for epoch in epochs:
        selected = _clock_offsets_for_epoch(spec, epoch, all_offsets)
        if epoch.calibration_complete is False:
            fits[epoch.epoch_id] = None
        else:
            fits[epoch.epoch_id] = _fit_clock(selected)

    # Align the same original sample across replicas.  A verified anchor fixes
    # the position exactly; a sample whose position is only tentative in one
    # copy must not become a second record next to the anchored copy.  The
    # alignment therefore groups a tentative observation with the anchored one
    # at the same anchor-relative offset, per epoch.
    anchors: dict[int | str, dict[int, int]] = defaultdict(dict)
    for observations in per_replica:
        for observation in observations:
            if observation.position_status == "anchored":
                anchors[observation.epoch_id][observation.anchor_offset] = (
                    observation.position
                )

    def canonical(observation: Observation) -> int:
        if observation.position_status == "anchored":
            return observation.position
        aligned = anchors.get(observation.epoch_id, {}).get(observation.anchor_offset)
        if aligned is not None:
            return aligned
        return observation.position

    by_position: dict[int, list[Observation]] = defaultdict(list)
    for observations in per_replica:
        for observation in observations:
            position = canonical(observation)
            if position != observation.position:
                observation = replace(observation, position=position)
            by_position[position].append(observation)

    merged: list[Observation] = []
    conflict_records: list[tuple[int, str, tuple[str, ...], tuple[str, ...]]] = []
    for position in sorted(by_position):
        best, conflicts = _merge_values(by_position[position])
        for layer, values, volumes in conflicts:
            conflict_records.append((position, layer, values, volumes))
        epoch = next(
            (epoch for epoch in epochs if epoch.epoch_id == best.epoch_id),
            epochs[0],
        )
        best = _apply_sync(best, fits.get(epoch.epoch_id), epoch.calibration_complete)
        merged.append(best)

    if dejitter:
        merged = _dejitter_observations(merged, spec)

    gaps, conflicts = _make_gaps_and_conflicts(
        stream_key,
        merged,
        conflict_records,
        0,
        0,
        samples_lost=any(
            observation.position_status != "anchored" for observation in merged
        )
        or any(issue.kind == "resync" for scan in scans for issue in scan.issues),
    )
    return _StreamResolution(
        stream_key=stream_key,
        spec=spec,
        observations=merged,
        gaps=gaps,
        conflicts=conflicts,
        clock_offsets=tuple(all_offsets),
        epochs=epochs,
    )


# ---------------------------------------------------------------------------
# Merge reports


@dataclass(frozen=True)
class LayerChange:
    """A change of one conclusion layer at one original position."""

    stream_key: str
    position: int
    layer: str
    before: object
    after: object
    cause: str

    def as_dict(self) -> dict:
        return {
            "stream_key": self.stream_key,
            "position": self.position,
            "layer": self.layer,
            "before": self.before,
            "after": self.after,
            "cause": self.cause,
        }


@dataclass
class MergeReport:
    """Which conclusions changed when a replica (or a clock correction) arrived."""

    previous_revision: int
    revision: int
    added_volumes: tuple[str, ...]
    changes: list[LayerChange] = field(default_factory=list)
    value_changes: list[LayerChange] = field(default_factory=list)
    device_time_changes: list[LayerChange] = field(default_factory=list)
    unified_time_changes: list[LayerChange] = field(default_factory=list)
    position_changes: list[LayerChange] = field(default_factory=list)
    resolved_gaps: list[Gap] = field(default_factory=list)
    new_gaps: list[Gap] = field(default_factory=list)
    duplicate_samples: list[tuple[str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes or self.resolved_gaps or self.new_gaps)

    def changed_layers(self) -> set[str]:
        return {
            layer
            for layer, changes in (
                ("value", self.value_changes),
                ("device_time", self.device_time_changes),
                ("unified_time", self.unified_time_changes),
                ("position", self.position_changes),
            )
            if changes
        }

    def summary(self) -> dict:
        return {
            "previous_revision": self.previous_revision,
            "revision": self.revision,
            "added_volumes": self.added_volumes,
            "changed_layers": sorted(self.changed_layers()),
            "changes": len(self.changes),
            "value_changes": len(self.value_changes),
            "device_time_changes": len(self.device_time_changes),
            "unified_time_changes": len(self.unified_time_changes),
            "position_changes": len(self.position_changes),
            "resolved_gaps": len(self.resolved_gaps),
            "new_gaps": len(self.new_gaps),
            "duplicate_samples": len(self.duplicate_samples),
            "notes": self.notes,
        }


def _describe(value) -> str:
    if isinstance(value, np.ndarray):
        return np.array2string(value)
    if isinstance(value, tuple):
        return repr(tuple(value))
    return repr(value)


def _append_layer_changes(
    report: MergeReport,
    stream_key: str,
    position: int,
    before: Observation,
    after: Observation,
) -> None:
    """Record the value/device-time/unified-time differences of one sample."""
    if not after.value_equals(before):
        change = LayerChange(
            stream_key,
            position,
            "value",
            _describe(before.value),
            _describe(after.value),
            "a replica supplied the original sample bytes",
        )
        report.changes.append(change)
        report.value_changes.append(change)
    if before.device_time_status != after.device_time_status or (
        before.device_time is not None
        and after.device_time is not None
        and not np.isclose(before.device_time, after.device_time, atol=1e-9, rtol=0)
    ):
        cause = (
            "a verified predecessor or explicit time stamp became available"
            if after.device_time_status == "explicit"
            else "the predecessor chain changed"
        )
        change = LayerChange(
            stream_key,
            position,
            "device_time",
            (before.device_time_status, before.device_time),
            (after.device_time_status, after.device_time),
            cause,
        )
        report.changes.append(change)
        report.device_time_changes.append(change)
    if after.unified_status != before.unified_status or (
        before.unified_time is not None
        and after.unified_time is not None
        and not np.isclose(before.unified_time, after.unified_time, atol=1e-9, rtol=0)
    ):
        change = LayerChange(
            stream_key,
            position,
            "unified_time",
            (before.unified_status, before.unified_time),
            (after.unified_status, after.unified_time),
            "a clock-offset measurement or calibration was added",
        )
        report.changes.append(change)
        report.unified_time_changes.append(change)


def _diff_views(
    previous: RecoveryView | None,
    current: RecoveryView,
    added_volumes: Sequence[str],
    duplicate_samples: Sequence[tuple[str, int]],
) -> MergeReport:
    report = MergeReport(
        previous_revision=-1 if previous is None else previous.revision,
        revision=current.revision,
        added_volumes=tuple(added_volumes),
        duplicate_samples=list(duplicate_samples),
    )
    if previous is None:
        for stream in current.streams.values():
            report.new_gaps.extend(stream.gaps)
        report.notes.append("first resolution")
        return report

    for stream_key, stream in current.streams.items():
        old_stream = previous.streams.get(stream_key)
        old_by_position = (
            {} if old_stream is None else {obs.position: obs for obs in old_stream}
        )
        new_by_position = {obs.position: obs for obs in stream}
        # A sample whose position was corrected after a late anchor became
        # available is the same original sample, not an addition.  Match it by
        # (epoch, ordinal) so the report attributes the change to the layers
        # that actually moved.
        old_by_identity = (
            {}
            if old_stream is None
            else {(obs.epoch_id, obs.ordinal): obs for obs in old_stream}
        )
        new_by_identity = {(obs.epoch_id, obs.ordinal): obs for obs in stream}
        matched_old: set[int] = set()
        matched_new: set[int] = set()
        for identity, after in new_by_identity.items():
            before = old_by_identity.get(identity)
            if before is None:
                continue
            matched_old.add(id(before))
            matched_new.add(id(after))
            if before.position != after.position:
                change = LayerChange(
                    stream_key,
                    after.position,
                    "position",
                    before.position,
                    after.position,
                    "the original position was corrected by a later verified anchor",
                )
                report.changes.append(change)
                report.position_changes.append(change)
            _append_layer_changes(report, stream_key, after.position, before, after)

        for position in sorted(set(old_by_position) | set(new_by_position)):
            before = old_by_position.get(position)
            after = new_by_position.get(position)
            if after is not None and id(after) in matched_new:
                continue
            if before is not None and id(before) in matched_old:
                continue
            if before is None:
                change = LayerChange(
                    stream_key,
                    position,
                    "position",
                    None,
                    after.value if after else None,
                    "added by a later replica",
                )
                report.changes.append(change)
                report.position_changes.append(change)
                continue
            if after is None:
                continue
            _append_layer_changes(report, stream_key, position, before, after)

        old_gaps = [] if old_stream is None else old_stream.gaps
        new_gaps = stream.gaps

        def same_gap(left: Gap, right: Gap) -> bool:
            if left.kind != right.kind or left.stream_key != right.stream_key:
                return False
            if left.start_position is None or right.start_position is None:
                return left.start_position == right.start_position
            return (
                left.start_position <= right.end_position
                and right.start_position <= left.end_position
            )

        for gap in old_gaps:
            # A gap counts as resolved when the new view no longer has a gap of
            # the same kind covering any of its positions (it may have shrunk).
            if not any(same_gap(gap, item) for item in new_gaps):
                report.resolved_gaps.append(gap)
        for gap in new_gaps:
            if not any(same_gap(gap, item) for item in old_gaps):
                report.new_gaps.append(gap)

    # Committed windows are immutable, but the report states whether the same
    # selection would now select different samples.
    for key, window in previous.committed.items():
        try:
            current_selection = current.window(
                window.stream_key, window.start, window.end, by=window.by
            )
        except KeyError:
            continue
        if tuple(obs.position for obs in current_selection) != window.positions:
            report.notes.append(
                f"committed window {key!r} (revision {window.revision}) differs from "
                "the current selection; requery() still returns the frozen samples"
            )
    return report


# ---------------------------------------------------------------------------
# Public job API


class RecoveryJob:
    """Resolve damaged replicas of one recording into a recovery view.

    Args:
        replicas: One or more :class:`Replica` objects.  They are matched by
            stream identity, not by byte offset, because the copies can have
            different sizes and different unreadable regions.
        archive: Optional :class:`RecordingArchive` with sampling periods and
            device-clock epochs.
        dejitter: Apply the same regular-sampling fit as :func:`load_xdf` to
            the unified times.  This never changes values or device times.
    """

    def __init__(
        self,
        replicas: Iterable[Replica] = (),
        archive: RecordingArchive | None = None,
        *,
        dejitter: bool = True,
        primary_volume: str | None = None,
    ):
        self.archive = archive
        self.dejitter = dejitter
        self.primary_volume = primary_volume
        self._replicas: dict[str, Replica] = {}
        self._scans: dict[str, ReplicaScan] = {}
        self._view: RecoveryView | None = None
        for replica in replicas:
            self.add_replica(replica, _defer=True)

    @property
    def primary_volume(self) -> str | None:
        if self._primary_volume is not None:
            return self._primary_volume
        if not self._replicas:
            return None
        return sorted(self._replicas)[0]

    @primary_volume.setter
    def primary_volume(self, value: str | None) -> None:
        self._primary_volume = value

    # -- replicas ---------------------------------------------------------

    @property
    def replicas(self) -> tuple[Replica, ...]:
        return tuple(self._replicas.values())

    def add_replica(self, replica: Replica, *, _defer: bool = False) -> ReplicaScan:
        """Add (or replace) a replica and scan it right away."""
        if replica.volume_id in self._replicas:
            existing = self._replicas[replica.volume_id]
            if existing.data == replica.data:
                return self._scans[replica.volume_id]
            logger.warning(
                "Replacing replica %s with different bytes", replica.volume_id
            )
        scan = scan_replica(replica, self.archive)
        self._replicas[replica.volume_id] = replica
        self._scans[replica.volume_id] = scan
        if not _defer:
            self._view = None
        return scan

    def add_path(self, volume_id: str, path: str | Path) -> ReplicaScan:
        return self.add_replica(Replica.from_path(volume_id, path))

    def scans(self) -> dict[str, ReplicaScan]:
        return dict(self._scans)

    # -- resolution -------------------------------------------------------

    def _ensure_view(self) -> RecoveryView:
        if self._view is not None:
            return self._view
        self._view = self._build_view(revision=0)
        return self._view

    def _build_view(self, revision: int) -> RecoveryView:
        scans = [self._scans[volume_id] for volume_id in sorted(self._scans)]
        checks = RecoveryChecks()
        for scan in scans:
            checks.extend(scan.checks)

        stream_keys: list[str] = []
        for scan in scans:
            for key in scan.stream_keys:
                if key not in stream_keys:
                    stream_keys.append(key)

        streams: dict[str, StreamView] = {}
        for stream_key in stream_keys:
            specs: list[StreamSpec] = []
            for scan in scans:
                if stream_key not in scan.specs:
                    specs.append(StreamSpec(stream_key=stream_key, tdiff=0.0))
                    continue
                specs.append(scan.specs[stream_key])
            resolution = _resolve_stream(
                stream_key,
                scans,
                specs,
                self.dejitter,
                primary_volume=self.primary_volume,
            )
            streams[stream_key] = StreamView(
                stream_key=stream_key,
                spec=resolution.spec,
                observations=resolution.observations,
                gaps=resolution.gaps,
                conflicts=resolution.conflicts,
                epochs=resolution.epochs,
                clock_offsets=resolution.clock_offsets,
            )

        all_gaps = [gap for stream in streams.values() for gap in stream.gaps]
        all_conflicts = [
            conflict for stream in streams.values() for conflict in stream.conflicts
        ]
        view = RecoveryView(
            revision=revision,
            replicas=tuple(sorted(self._scans)),
            streams=streams,
            checks=checks,
            gaps=all_gaps,
            conflicts=all_conflicts,
            scans={
                volume_id: self._scans[volume_id] for volume_id in sorted(self._scans)
            },
        )
        view.pyxdf_checks = [
            comparison
            for scan in scans
            if scan.complete
            for comparison in _compare_with_pyxdf(scan, view)
        ]
        return view

    def resolve(self) -> RecoveryView:
        """Resolve all replicas into a recovery view."""
        return self._ensure_view()

    @property
    def revision(self) -> int:
        return 0 if self._view is None else self._view.revision

    def commit_window(self, stream_key: str, start=None, end=None, *, by="position"):
        view = self._ensure_view()
        return view.commit_window(stream_key, start, end, by=by)

    def merge(self, replica: Replica) -> MergeReport:
        """Add a replica and report which conclusion layers changed."""
        previous = self._ensure_view()
        previous_committed = dict(previous.committed)
        previous_volume_ids = set(self._scans)
        # Record which original positions the existing view already covers
        # *before* replacing the view, so a sample that arrives twice can be
        # reported as a duplicate instead of as a new device record.
        covered = {
            stream_key: {observation.position for observation in stream}
            for stream_key, stream in previous.streams.items()
        }
        new_scan = self.add_replica(replica)
        duplicates = self._duplicate_positions(new_scan, covered)
        self._view = self._build_view(previous.revision + 1)
        self._view.committed = previous_committed
        report = _diff_views(
            previous,
            self._view,
            sorted(set(self._scans) - previous_volume_ids),
            duplicates,
        )
        return report

    def merge_clock_offsets(
        self,
        volume_id: str,
        stream_key: str,
        offsets: Sequence[ClockOffset],
        *,
        verified: bool = True,
    ) -> MergeReport:
        """Add late clock-offset measurements and report what they changed.

        This is the case of a replica that brings only a delayed clock
        correction, not sample bytes.  Values, device times and positions are
        expected to stay unchanged; only unified times may move.
        """
        previous = self._ensure_view()
        previous_committed = dict(previous.committed)
        target = self._scans.get(volume_id)
        if target is None:
            raise KeyError(f"unknown replica {volume_id!r}")
        current = list(target.clock_offsets.get(stream_key, []))
        for offset in offsets:
            if not isinstance(offset, ClockOffset):
                raise TypeError("offsets must be ClockOffset instances")
            current.append(
                ClockOffset(
                    time=offset.time,
                    value=offset.value,
                    volume_id=volume_id,
                    byte_range=offset.byte_range,
                    verified=verified and offset.verified,
                    stream_key=stream_key,
                )
            )
        target.clock_offsets[stream_key] = current
        self._view = self._build_view(previous.revision + 1)
        self._view.committed = previous_committed
        report = _diff_views(previous, self._view, (volume_id,), ())
        report.notes.append("late clock correction only")
        return report

    def plan_window(self, stream_key: str, start=None, end=None, *, by="position"):
        """Return the samples a unified-time window would currently select."""
        return self._ensure_view().window(stream_key, start, end, by=by)

    def _duplicate_positions(
        self, scan: ReplicaScan, covered: dict[str, set[int]]
    ) -> list[tuple[str, int]]:
        """Original positions the new replica shares with an earlier replica."""
        duplicates: list[tuple[str, int]] = []
        for stream_key in scan.stream_keys:
            spec = scan.specs.get(stream_key)
            if spec is None:
                continue
            axis = _epoch_axis(spec)
            resolved = _resolve_scan_stream(scan, spec, axis, strict_first=True)
            existing = covered.get(stream_key, set())
            duplicates.extend(
                (stream_key, observation.position)
                for observation in resolved
                if observation.position in existing
            )
        return duplicates


def recover(
    replicas: Iterable[Replica],
    archive: RecordingArchive | None = None,
    *,
    dejitter: bool = True,
) -> RecoveryView:
    """Convenience wrapper: resolve replicas and return the recovery view."""
    return RecoveryJob(replicas, archive, dejitter=dejitter).resolve()


def recovery_view(
    replicas: Iterable[Replica | str | Path],
    archive: RecordingArchive | None = None,
    *,
    dejitter: bool = True,
) -> RecoveryView:
    """Like :func:`recover`, but paths are accepted and named by their stem."""
    resolved = []
    for index, replica in enumerate(replicas):
        if isinstance(replica, (str, Path)):
            path = Path(replica)
            resolved.append(Replica.from_path(path.stem or f"replica{index}", path))
        else:
            resolved.append(replica)
    return recover(resolved, archive, dejitter=dejitter)


# ---------------------------------------------------------------------------
# Cross-check against the regular importer


def _compare_with_pyxdf(scan: ReplicaScan, view: RecoveryView) -> list[PyXDFComparison]:
    """Compare a complete replica's recovery against ordinary pyxdf output.

    A complete input is cross-checked twice: the synchronized but un-dejittered
    pyxdf result against ``sync_time``, and the synchronized plus dejittered
    result against ``unified_time``.  Observations whose time was revised still
    have to carry the same original value.
    """
    if not scan.complete:
        return []
    try:
        synced, _ = load_xdf(
            io.BytesIO(scan.replica.data),
            synchronize_clocks=True,
            dejitter_timestamps=False,
        )
        unified, _ = load_xdf(
            io.BytesIO(scan.replica.data),
            synchronize_clocks=True,
            dejitter_timestamps=True,
        )
    except Exception as exc:  # noqa: BLE001 - comparison must not hide recovery
        return [
            PyXDFComparison(
                scan.volume_id,
                key,
                True,
                0,
                len(stream),
                False,
                False,
                False,
                None,
                None,
                notes=(f"pyxdf could not load the replica: {exc}",),
            )
            for key, stream in view.streams.items()
        ]

    def find(streams, stream_key):
        for candidate in streams:
            header = {"info": candidate.get("info", {})}
            key = _stream_key_from_header(
                header, _as_int(candidate.get("info", {}).get("stream_id"))
            )
            if key == stream_key:
                return candidate
        return None

    comparisons = []
    for stream_key, stream in view.streams.items():
        synced_stream = find(synced, stream_key)
        unified_stream = find(unified, stream_key)
        if synced_stream is None or unified_stream is None:
            continue
        observations = stream.observations
        values_match = len(synced_stream["time_series"]) == len(observations) and all(
            _values_equal(observation, value)
            for observation, value in zip(observations, synced_stream["time_series"])
        )
        sync_times = np.array(
            [np.nan if obs.sync_time is None else obs.sync_time for obs in observations]
        )
        unified_times = np.array(
            [
                np.nan if obs.unified_time is None else obs.unified_time
                for obs in observations
            ]
        )
        sync_expected = np.asarray(synced_stream["time_stamps"], dtype=float)
        unified_expected = np.asarray(unified_stream["time_stamps"], dtype=float)
        sync_diff = _safe_max_diff(sync_times, sync_expected)
        unified_diff = _safe_max_diff(unified_times, unified_expected)
        mismatched = ()
        if len(observations) == len(unified_expected):
            mismatched = tuple(
                observation.position
                for observation, value, expected in zip(
                    observations, unified_times, unified_expected
                )
                if not np.isnan(value)
                and not np.isclose(value, expected, atol=1e-6, rtol=0)
            )
        comparisons.append(
            PyXDFComparison(
                scan.volume_id,
                stream_key,
                True,
                len(sync_expected),
                len(observations),
                values_match,
                sync_diff is not None and sync_diff <= 1e-6,
                unified_diff is not None and unified_diff <= 1e-6,
                sync_diff,
                unified_diff,
                mismatched,
            )
        )
    return comparisons


def _values_equal(observation: Observation, value) -> bool:
    if isinstance(observation.value, np.ndarray):
        return np.array_equal(observation.value, np.asarray(value))
    if isinstance(observation.value, tuple):
        if isinstance(value, (list, tuple)):
            return tuple(observation.value) == tuple(np.asarray(value).ravel())
        return observation.value == tuple(np.asarray(value).ravel())
    return observation.value == value


def _safe_max_diff(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) != len(right):
        return None
    mask = ~(np.isnan(left) | np.isnan(right))
    if not np.any(mask):
        return None
    return float(np.max(np.abs(left[mask] - right[mask])))
