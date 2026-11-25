"""
    pyte.archive
    ~~~~~~~~~~~~

    Reading and writing for captured terminal byte streams.

    A capture archive is a sequence of *segments*.  A segment is either a
    literal byte string (an uncompressed capture) or a template together with
    the number of times it occurred consecutively::

        archive := MAGIC VERSION block* END
        block   := LITERAL length bytes
                 | DEFINE  template-id length bytes
                 | RUN     template-id repeat
                 | INLINE  repeat length bytes
                 | META    length json-utf8

    A file without :data:`MAGIC` is treated as a single literal segment, so a
    plain capture (for example ``tests/captured/ls.input``) can be opened with
    the same reader.  Repeated templates are never expanded unless the caller
    explicitly asks for bytes: :attr:`Archive.expanded_size` and the ledger
    entry point work from the templates and repeat counts directly.

    :copyright: (c) 2012-2017 by pyte authors and contributors,
                    see AUTHORS for details.
    :license: LGPL, see LICENSE for more details.
"""
from __future__ import annotations

import json
import mmap
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, NamedTuple

if TYPE_CHECKING:
    from collections.abc import BinaryIO

MAGIC = b"PYTEARCH"
VERSION = 1

#: Block tags.
END = 0x00
LITERAL = 0x01
DEFINE = 0x02
RUN = 0x03
INLINE = 0x04
META = 0x05

_HEADER = struct.Struct("<8sB")
_U8 = struct.Struct("<B")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")


class ArchiveError(ValueError):
    """Base class for archive related errors."""


class ArchiveFormatError(ArchiveError):
    """The archive is malformed."""


class ByteRange(NamedTuple):
    """A half-open range of bytes in the expanded capture stream."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def __iter__(self) -> Iterator[int]:
        yield self.start
        yield self.end


class Occurrence(NamedTuple):
    """One consecutive occurrence of an archive segment.

    :param int segment: zero-based index of the segment.
    :param int occurrence: one-based occurrence inside that segment.
    :param int template: template id, or ``None`` for literal data.
    :param int byte_start: first byte in the expanded stream.
    :param int byte_end: one past the last byte in the expanded stream.
    :param bytes data: the bytes of this occurrence.
    """

    segment: int
    occurrence: int
    template: int | None
    byte_start: int
    byte_end: int
    data: bytes

    @property
    def source(self) -> int:
        return self.segment

    @property
    def repeat(self) -> int:
        return self.occurrence

    @property
    def byte_range(self) -> ByteRange:
        return ByteRange(self.byte_start, self.byte_end)


class Segment:
    """One block of an archive.

    Literal segments are stored either in memory or as a range in the backing
    file.  Template segments hold the template bytes and a repeat count; the
    bytes are not duplicated.
    """

    __slots__ = ("index", "kind", "template_id", "template", "repeat",
                 "offset", "length", "byte_start")

    def __init__(
        self,
        index: int,
        kind: str,
        template_id: int | None,
        template: bytes,
        repeat: int,
        offset: int = 0,
        length: int | None = None,
        byte_start: int = 0,
    ) -> None:
        self.index = index
        self.kind = kind
        self.template_id = template_id
        self.template = template
        self.repeat = repeat
        self.offset = offset
        self.length = len(template) if length is None else length
        self.byte_start = byte_start

    @property
    def data(self) -> bytes:
        return self.template

    @property
    def stride(self) -> int:
        """Number of expanded bytes contributed by one occurrence."""
        return self.length

    @property
    def byte_end(self) -> int:
        return self.byte_start + self.length * self.repeat

    def occurrence(self, number: int) -> Occurrence:
        """Return the ``number``-th (one-based) occurrence of this segment."""
        if not 1 <= number <= self.repeat:
            raise IndexError(number)
        start = self.byte_start + (number - 1) * self.length
        return Occurrence(
            self.index, number, self.template_id, start,
            start + self.length, self.template)

    def __repr__(self) -> str:
        return (
            f"Segment(index={self.index}, kind={self.kind!r}, "
            f"template_id={self.template_id!r}, repeat={self.repeat!r}, "
            f"length={self.length!r})"
        )


class _Backing:
    """Random access over bytes, a binary file or a file path."""

    def __init__(self, source: Any) -> None:
        self._mmap: mmap.mmap | None = None
        self._file: BinaryIO | None = None
        self._data: bytes | memoryview | None = None
        self._owned = False

        if isinstance(source, (bytes, bytearray, memoryview)):
            self._data = memoryview(source)
            self.size = len(self._data)
        elif isinstance(source, (str, os.PathLike)):
            path = Path(source)
            with path.open("rb") as handle:
                self.size = os.fstat(handle.fileno()).st_size
            if self.size:
                handle = path.open("rb")
                self._file = handle
                self._mmap = mmap.mmap(handle.fileno(), 0,
                                       access=mmap.ACCESS_READ)
            else:
                self._data = b""
        else:
            # A file-like object.  Prefer its bytes if it is in memory,
            # otherwise read it once; archive files are normally small even
            # when the expanded stream is huge due to repeated templates.
            data = source.read()
            if isinstance(data, str):
                data = data.encode()
            self._data = memoryview(data)
            self.size = len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        end = min(self.size, offset + length)
        if offset >= end:
            return b""
        if self._data is not None:
            return bytes(self._data[offset:end])
        assert self._mmap is not None
        return self._mmap[offset:end]

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None


class Archive:
    """A parsed capture archive.

    The archive is parsed into segment descriptors without expanding repeated
    templates.  Use :meth:`iter_bytes` for an explicit expansion, and the
    ledger entry points for record queries.

    :param source: bytes, a path or a binary file object.
    :param bool strict: reject truncated or unknown blocks.
    """

    def __init__(self, source: Any, strict: bool = True) -> None:
        self.strict = strict
        self._backing = _Backing(source)
        self.metadata: dict[str, Any] = {}
        self.segments: list[Segment] = []
        self._templates: dict[int, tuple[bytes, int]] = {}
        self._parse()

    @classmethod
    def from_bytes(cls, data: bytes, strict: bool = True) -> "Archive":
        return cls(data, strict=strict)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str],
                  strict: bool = True) -> "Archive":
        return cls(path, strict=strict)

    @classmethod
    def open(cls, source: Any, strict: bool = True) -> "Archive":
        return cls(source, strict=strict)

    def close(self) -> None:
        self._backing.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def size(self) -> int:
        """Size of the archive file itself."""
        return self._backing.size

    @property
    def expanded_size(self) -> int:
        """Length of the capture after expanding all repeats."""
        if not self.segments:
            return 0
        last = self.segments[-1]
        return last.byte_end

    #: Alias used by callers which think in terms of the reconstructed stream.
    @property
    def byte_length(self) -> int:
        return self.expanded_size

    @property
    def columns(self) -> int:
        return int(self.metadata.get("columns", 80))

    @property
    def lines(self) -> int:
        return int(self.metadata.get("lines", 24))

    def _parse(self) -> None:
        backing = self._backing
        head = backing.read(0, len(MAGIC) + 1)
        if not head.startswith(MAGIC):
            # Uncompressed capture: the bytes mean exactly what they say.
            self.segments.append(Segment(
                0, "literal", None, b"", 1, offset=0,
                length=backing.size, byte_start=0))
            self._literal_in_file = True
            return

        self._literal_in_file = False
        if len(head) < len(MAGIC) + 1:
            raise ArchiveFormatError("truncated archive header")
        version = head[len(MAGIC)]
        if version != VERSION:
            raise ArchiveFormatError(f"unsupported archive version {version}")

        position = len(MAGIC) + 1
        size = backing.size
        byte_start = 0
        while position < size:
            tag = backing.read(position, 1)
            if not tag:
                break
            position += 1
            kind = tag[0]
            if kind == END:
                break
            if kind == LITERAL:
                length = self._read_struct(_U64, position, "literal length")
                position += _U64.size
                payload = position
                segment = Segment(
                    len(self.segments), "literal", None, b"", 1,
                    offset=payload, length=length, byte_start=byte_start)
                byte_start += length
                self.segments.append(segment)
                position += length
            elif kind == DEFINE:
                template_id = self._read_struct(_U32, position, "template id")
                position += _U32.size
                length = self._read_struct(_U32, position, "template length")
                position += _U32.size
                payload = position
                data = backing.read(payload, length)
                if len(data) != length:
                    raise ArchiveFormatError("truncated template")
                self._templates[template_id] = (data, payload)
                position += length
            elif kind == RUN:
                template_id = self._read_struct(_U32, position, "template id")
                position += _U32.size
                repeat = self._read_struct(_U64, position, "repeat count")
                position += _U64.size
                try:
                    data, payload = self._templates[template_id]
                except KeyError as error:
                    raise ArchiveFormatError(
                        f"undefined template {template_id}") from error
                segment = Segment(
                    len(self.segments), "run", template_id, data, repeat,
                    offset=payload, length=len(data), byte_start=byte_start)
                byte_start += len(data) * repeat
                self.segments.append(segment)
            elif kind == INLINE:
                repeat = self._read_struct(_U64, position, "repeat count")
                position += _U64.size
                length = self._read_struct(_U32, position, "template length")
                position += _U32.size
                payload = position
                data = backing.read(payload, length)
                if len(data) != length:
                    raise ArchiveFormatError("truncated inline template")
                segment = Segment(
                    len(self.segments), "run", None, data, repeat,
                    offset=payload, length=length, byte_start=byte_start)
                byte_start += length * repeat
                self.segments.append(segment)
                position += length
            elif kind == META:
                length = self._read_struct(_U32, position, "metadata length")
                position += _U32.size
                raw = backing.read(position, length)
                if len(raw) != length:
                    raise ArchiveFormatError("truncated metadata")
                try:
                    self.metadata = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ArchiveFormatError("invalid metadata") from error
                position += length
            else:
                if self.strict:
                    raise ArchiveFormatError(f"unknown block tag {kind}")
                break

    def _read_struct(self, spec: struct.Struct, offset: int,
                     what: str) -> int:
        raw = self._backing.read(offset, spec.size)
        if len(raw) != spec.size:
            raise ArchiveFormatError(f"truncated {what}")
        return int(spec.unpack(raw)[0])

    def _segment_bytes(self, segment: Segment) -> bytes:
        if segment.kind == "run":
            return segment.template
        if getattr(self, "_literal_in_file", False) and segment.index == 0:
            return self._backing.read(0, self._backing.size)
        return self._backing.read(segment.offset, segment.length)

    def iter_segments(self) -> Iterator[Segment]:
        return iter(self.segments)

    def segment_occurrences(self, segment: Segment) -> Iterator[Occurrence]:
        for number in range(1, segment.repeat + 1):
            if segment.kind == "run":
                data = segment.template
            else:
                data = self._segment_bytes(segment)
            start = segment.byte_start + (number - 1) * segment.length
            yield Occurrence(
                segment.index, number, segment.template_id, start,
                start + segment.length, data)

    def iter_occurrences(self) -> Iterator[Occurrence]:
        """Yield every segment occurrence in archive order.

        Beware that an archive may describe billions of repeated occurrences;
        the ledger does not use this method for such archives.
        """
        for segment in self.segments:
            yield from self.segment_occurrences(segment)

    def occurrences(self) -> Iterator[Occurrence]:
        return self.iter_occurrences()

    def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """Yield the expanded capture in chunks.

        Compressed templates are expanded here.  The method is intentionally
        not used by :class:`~pyte.ledger.Ledger` for huge repeat counts.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        for segment in self.segments:
            if segment.kind == "literal":
                if getattr(self, "_literal_in_file", False) \
                        and segment.index == 0:
                    for data in self._chunks_from_backing(
                            0, self._backing.size, chunk_size):
                        yield data
                else:
                    for data in self._chunks_from_backing(
                            segment.offset, segment.length, chunk_size):
                        yield data
            else:
                template = segment.template
                if not template:
                    continue
                for _ in range(segment.repeat):
                    for offset in range(0, len(template), chunk_size):
                        yield template[offset:offset + chunk_size]

    def _chunks_from_backing(self, offset: int, length: int,
                             chunk_size: int) -> Iterator[bytes]:
        position = offset
        end = offset + length
        while position < end:
            data = self._backing.read(position,
                                      min(chunk_size, end - position))
            if not data:
                break
            yield data
            position += len(data)

    def read_bytes(self, start: int = 0,
                   end: int | None = None) -> bytes:
        """Return the expanded bytes in ``[start, end)``."""
        if end is None:
            end = self.expanded_size
        if start < 0 or end < start:
            raise ValueError("invalid byte range")
        pieces = []
        for segment in self.segments:
            if segment.byte_end <= start:
                continue
            if segment.byte_start >= end:
                break
            if segment.kind == "literal":
                lo = max(start, segment.byte_start) - segment.byte_start
                hi = min(end, segment.byte_end) - segment.byte_start
                pieces.append(self._backing.read(segment.offset + lo,
                                                 hi - lo))
            else:
                template = segment.template
                stride = segment.length
                if not stride:
                    continue
                lo = max(start, segment.byte_start) - segment.byte_start
                hi = min(end, segment.byte_end) - segment.byte_start
                first = lo // stride
                last = (hi + stride - 1) // stride
                for index in range(first, last):
                    inner_lo = max(lo, index * stride) - index * stride
                    inner_hi = min(hi, (index + 1) * stride) - index * stride
                    pieces.append(template[inner_lo:inner_hi])
        return b"".join(pieces)

    def byte_offset(self, segment: Segment, occurrence: int,
                    offset: int = 0) -> int:
        """Expanded byte offset for a position inside an occurrence."""
        return (segment.byte_start + (occurrence - 1) * segment.length
                + offset)

    def __iter__(self) -> Iterator[Segment]:
        return self.iter_segments()

    def __len__(self) -> int:
        return len(self.segments)

    def __repr__(self) -> str:
        return (f"Archive(segments={len(self.segments)}, "
                f"expanded_size={self.expanded_size})")


class ArchiveWriter:
    """Build an archive without materialising repeated templates.

    >>> writer = ArchiveWriter(columns=80, lines=24)
    >>> _ = writer.literal(b"hello")
    >>> _ = writer.template(b"line\\r\\n", repeat=3)
    >>> data = writer.to_bytes()
    """

    def __init__(self, columns: int | None = None,
                 lines: int | None = None, **metadata: Any) -> None:
        self._parts: list[bytes] = []
        self._templates: dict[bytes, int] = {}
        self._next_template = 1
        self.metadata: dict[str, Any] = dict(metadata)
        if columns is not None:
            self.metadata["columns"] = columns
        if lines is not None:
            self.metadata["lines"] = lines
        self._started = False

    def _start(self) -> None:
        if not self._started:
            self._parts.append(_HEADER.pack(MAGIC, VERSION))
            if self.metadata:
                raw = json.dumps(self.metadata, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
                self._parts.append(_U8.pack(META) + _U32.pack(len(raw)) + raw)
            self._started = True

    def literal(self, data: bytes | bytearray | memoryview) -> "ArchiveWriter":
        """Append literal (uncompressed) bytes."""
        if not data:
            return self
        self._start()
        raw = bytes(data)
        self._parts.append(_U8.pack(LITERAL) + _U64.pack(len(raw)) + raw)
        return self

    def raw(self, data: bytes | bytearray | memoryview) -> "ArchiveWriter":
        return self.literal(data)

    def define(self, data: bytes, template_id: int | None = None) -> int:
        """Define a reusable template and return its id."""
        self._start()
        raw = bytes(data)
        if template_id is None:
            template_id = self._templates.get(raw)
            if template_id is None:
                template_id = self._next_template
                self._next_template += 1
                self._templates[raw] = template_id
                self._parts.append(
                    _U8.pack(DEFINE) + _U32.pack(template_id)
                    + _U32.pack(len(raw)) + raw)
        else:
            self._templates[raw] = template_id
            self._next_template = max(self._next_template, template_id + 1)
            self._parts.append(
                _U8.pack(DEFINE) + _U32.pack(template_id)
                + _U32.pack(len(raw)) + raw)
        return template_id

    def run(self, template_id: int, repeat: int) -> "ArchiveWriter":
        if repeat < 0:
            raise ValueError("repeat must be non-negative")
        if not repeat:
            return self
        self._start()
        self._parts.append(
            _U8.pack(RUN) + _U32.pack(template_id) + _U64.pack(repeat))
        return self

    def template(self, data: bytes, repeat: int = 1,
                 template_id: int | None = None) -> "ArchiveWriter":
        """Append ``data`` repeated ``repeat`` times.

        The template is defined once and referenced by a run block.
        """
        if repeat < 0:
            raise ValueError("repeat must be non-negative")
        if not data or not repeat:
            return self
        template_id = self.define(data, template_id)
        return self.run(template_id, repeat)

    def inline(self, data: bytes, repeat: int = 1) -> "ArchiveWriter":
        """Append a template without adding it to the shared dictionary."""
        if repeat < 0:
            raise ValueError("repeat must be non-negative")
        if not data or not repeat:
            return self
        self._start()
        raw = bytes(data)
        self._parts.append(
            _U8.pack(INLINE) + _U64.pack(repeat) + _U32.pack(len(raw)) + raw)
        return self

    def to_bytes(self) -> bytes:
        self._start()
        return b"".join(self._parts) + _U8.pack(END)

    def write(self, target: str | os.PathLike[str] | BinaryIO) -> None:
        data = self.to_bytes()
        if hasattr(target, "write"):
            target.write(data)  # type: ignore[union-attr]
        else:
            Path(target).write_bytes(data)

    def __bytes__(self) -> bytes:
        return self.to_bytes()


def build_archive(
    parts: Iterable[Any],
    columns: int | None = None,
    lines: int | None = None,
    **metadata: Any,
) -> bytes:
    """Build an archive from an iterable of parts.

    Each part may be:

    * ``bytes`` -- literal data;
    * ``(bytes, repeat)`` -- a repeated template;
    * :class:`Segment` -- an already described segment;
    * :class:`Occurrence` -- one occurrence of an existing segment.
    """
    writer = ArchiveWriter(columns=columns, lines=lines, **metadata)
    for part in parts:
        if isinstance(part, Segment):
            if part.kind == "run":
                if part.template_id is None:
                    writer.inline(part.template, part.repeat)
                else:
                    writer.define(part.template, part.template_id)
                    writer.run(part.template_id, part.repeat)
            else:
                writer.literal(part.template or b"")
        elif isinstance(part, Occurrence):
            writer.literal(part.data)
        elif isinstance(part, (bytes, bytearray, memoryview, str)):
            writer.literal(part.encode() if isinstance(part, str) else part)
        else:
            data, repeat = part
            if isinstance(data, str):
                data = data.encode()
            writer.template(data, repeat)
    return writer.to_bytes()


def write_archive(
    target: str | os.PathLike[str] | BinaryIO,
    parts: Iterable[Any],
    columns: int | None = None,
    lines: int | None = None,
    **metadata: Any,
) -> None:
    data = build_archive(parts, columns=columns, lines=lines, **metadata)
    if hasattr(target, "write"):
        target.write(data)  # type: ignore[union-attr]
    else:
        Path(target).write_bytes(data)


def encode_archive(parts: Iterable[Any], **kwargs: Any) -> bytes:
    return build_archive(parts, **kwargs)


def parse_archive(source: Any, strict: bool = True) -> Archive:
    return Archive(source, strict=strict)


def open_archive(source: Any, strict: bool = True) -> Archive:
    return Archive(source, strict=strict)


def read_archive(source: Any, strict: bool = True) -> Archive:
    return Archive(source, strict=strict)


def load_archive(source: Any, strict: bool = True) -> Archive:
    return Archive(source, strict=strict)


#: A concise alias for callers who already have an archive.
ArchiveReader = Archive

__all__ = [
    "MAGIC",
    "VERSION",
    "Archive",
    "ArchiveError",
    "ArchiveFormatError",
    "ArchiveReader",
    "ArchiveWriter",
    "ByteRange",
    "Occurrence",
    "Segment",
    "build_archive",
    "encode_archive",
    "load_archive",
    "open_archive",
    "parse_archive",
    "read_archive",
    "write_archive",
]
