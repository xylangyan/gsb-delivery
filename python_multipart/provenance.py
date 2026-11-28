"""Two-level nested multipart parsing with a byte-level provenance ledger.

A nested form is a small :mimetype:`multipart/form-data` document carried as the
value of an ordinary field of an outer multipart message.  This module parses
such nested messages in a single streaming pass while keeping a *source ledger*
(:class:`ProvenanceLedger`):

* every byte of a delivered inner (or outer) part can be traced back to the
  exact bytes of the original upload, recording only the bits that actually
  contributed to it;
* every original byte can be queried in reverse for the part bytes it feeds.

The two encoding layers independently support plain binary, base64 and
quoted-printable.  Encoding symbols that only frame the stream (base64 padding,
quoted-printable soft line breaks, ignored whitespace) never produce output and
therefore never appear in the ledger.

Only the nested windows selected up front keep detailed relations; the rest of
the stream is parsed and delivered without accumulating per-byte metadata, and
the intermediate form is never buffered in full.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple, cast

from .exceptions import FormParserError, MultipartParseError, ProvenanceDecodeError, ProvenanceError
from .multipart import END, Field, File, MultipartParser, parse_options_header

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from typing import Any, Protocol

    from .multipart import FileConfig, MultipartCallbacks

    class _Sink(Protocol):
        def __call__(self, __data: bytes, __sources: list[tuple[BitSource, ...]]) -> None: ...


#: The outer multipart layer.
OUTER = 0
#: The inner (nested) multipart layer.
INNER = 1

BINARY = "binary"
BASE64 = "base64"
QUOTED_PRINTABLE = "quoted-printable"
_ENCODINGS = frozenset((BINARY, BASE64, QUOTED_PRINTABLE))


class PartStatus(IntEnum):
    """Lifecycle of a part recorded in the :class:`ProvenanceLedger`."""

    #: Complete enough to be reported, but the outer message is not closed yet.
    PROVISIONAL = 1
    #: Outer message closed and every encoding step succeeded: safe to sign off.
    AUDITABLE = 2
    #: An encoding or framing failure touches this part.
    FAILED = 3


PROVISIONAL = PartStatus.PROVISIONAL
AUDITABLE = PartStatus.AUDITABLE
FAILED = PartStatus.FAILED


class BitSource(NamedTuple):
    """A contribution of one source byte to one output byte.

    Args:
        position: Absolute position of the source byte inside its own layer.
        mask: The bits (inside the output byte) that this source byte supplied.
    """

    position: int
    mask: int


class TargetRef(NamedTuple):
    """A single delivered byte, addressed through ledger coordinates."""

    layer: int
    window: int
    part: int
    offset: int


class PartInfo(NamedTuple):
    """Identity information recorded for each parsed part."""

    name: bytes | None
    filename: bytes | None


class RecordKey(NamedTuple):
    layer: int
    window: int
    part: int


# Bytes accepted as base64 symbols and as ignorable base64 whitespace.
_B64_SYMBOLS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
_B64_WHITESPACE = frozenset(b" \t\r\n")
_B64_DIGITS = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_VALUE = {char: index for index, char in enumerate(_B64_DIGITS)}

_HEX_DIGITS = frozenset(b"0123456789ABCDEFabcdef")
_HEX_VALUE = {char: int(chr(char), 16) for char in _HEX_DIGITS}
# Literal quoted-printable bytes: tab, CR/LF and the printable ASCII range.
_QP_LITERALS = frozenset((9, 10, 13, *range(0x20, 0x7F)))


@dataclass(frozen=True)
class SourceWindow:
    """Declaration of one outer field that carries a nested form.

    All coordinates are given before parsing starts, relative to the decoded
    content of the outer part.

    Args:
        field_name: The ``name`` parameter of the outer part's
            ``Content-Disposition`` header.
        boundary: The boundary of the nested multipart document.
        encoding: How the nested document is encoded inside the field:
            ``"binary"``, ``"base64"`` or ``"quoted-printable"``.
        start: Content offset where the nested document begins.
        end: Content offset where the nested document ends, or ``None`` for the
            end of the part.
        content_start: Start of the content window to retain, relative to
            ``start``.
        content_end: End of the content window to retain, relative to ``start``,
            or ``None`` for the rest of the document.
    """

    field_name: bytes
    boundary: bytes
    encoding: str = BINARY
    start: int = 0
    end: int | None = None
    content_start: int = 0
    content_end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.field_name, bytes) or not self.field_name:
            raise ValueError("SourceWindow.field_name must be a non-empty bytes value")
        if not isinstance(self.boundary, bytes) or not self.boundary:
            raise ValueError("SourceWindow.boundary must be a non-empty bytes value")
        if self.encoding not in _ENCODINGS:
            raise ValueError(f"Unknown nested encoding {self.encoding!r}")
        if self.start < 0:
            raise ValueError("SourceWindow.start must be non-negative")
        if self.end is not None and self.end < self.start:
            raise ValueError("SourceWindow.end must be greater than or equal to start")
        if self.content_start < 0:
            raise ValueError("SourceWindow.content_start must be non-negative")
        if self.content_end is not None and self.content_end < self.content_start:
            raise ValueError("SourceWindow.content_end must be greater than or equal to content_start")

    @property
    def window_lo(self) -> int:
        return self.start + self.content_start

    @property
    def window_hi(self) -> int | None:
        document_hi = self.end
        content_hi = None if self.content_end is None else self.start + self.content_end
        if document_hi is None:
            return content_hi
        if content_hi is None:
            return document_hi
        return min(document_hi, content_hi)


class _SimpleRun(NamedTuple):
    target_start: int
    target_end: int
    source_start: int


@dataclass
class _GeneralRun:
    target_start: int
    sources: list[tuple[BitSource, ...]] = field(default_factory=list)


class _MappingBuilder:
    """Accumulates output-byte -> source-bit relations for one part.

    Runs of plain one-to-one byte copies (the binary case) are stored as
    compressed ranges; every other output byte keeps the exact list of source
    bytes and bit masks that produced it.
    """

    def __init__(self) -> None:
        self._simple_runs: list[_SimpleRun] = []
        self._general_runs: list[_GeneralRun] = []
        self._length = 0

    def add(self, sources: tuple[BitSource, ...]) -> None:
        offset = self._length
        if len(sources) == 1 and sources[0].mask == 0xFF:
            position = sources[0].position
            run = self._simple_runs[-1] if self._simple_runs else None
            contiguous = run is not None and run.target_end == offset
            if contiguous and run.source_start + (offset - run.target_start) == position:
                assert run is not None
                self._simple_runs[-1] = _SimpleRun(run.target_start, offset + 1, run.source_start)
            else:
                self._simple_runs.append(_SimpleRun(offset, offset + 1, position))
        else:
            run = None
            if self._general_runs:
                latest = self._general_runs[-1]
                if latest.target_start + len(latest.sources) == offset:
                    run = latest
            if run is None:
                run = _GeneralRun(offset)
                self._general_runs.append(run)
            run.sources.append(sources)
        self._length += 1

    def target_sources(self, offset: int) -> tuple[BitSource, ...]:
        for run in self._simple_runs:
            if run.target_start <= offset < run.target_end:
                return (BitSource(run.source_start + (offset - run.target_start), 0xFF),)
        for run in self._general_runs:
            relative = offset - run.target_start
            if 0 <= relative < len(run.sources):
                return run.sources[relative]
        raise KeyError(f"Target offset {offset} has no recorded source")

    def targets_for_source(self, position: int) -> list[tuple[int, int]]:
        """Return ``(target offset, mask)`` pairs fed by ``position``."""
        found: list[tuple[int, int]] = []
        for run in self._simple_runs:
            source_end = run.source_start + (run.target_end - run.target_start)
            if run.source_start <= position < source_end:
                found.append((run.target_start + (position - run.source_start), 0xFF))
        for run in self._general_runs:
            for relative, sources in enumerate(run.sources):
                for source in sources:
                    if source.position == position:
                        found.append((run.target_start + relative, source.mask))
        return found

    def iter_targets(self) -> Iterable[tuple[int, tuple[BitSource, ...]]]:
        """Yield every recorded target offset with its direct sources."""
        for offset in range(self._length):
            yield offset, self.target_sources(offset)

    @property
    def length(self) -> int:
        return self._length


class _BaseMapper:
    """Streaming strict mapper.

    A mapper consumes encoded bytes (given with the absolute position of the
    first byte of each chunk) and pushes decoded output bytes to ``sink``,
    together with the exact source bits of every output byte.
    """

    def __init__(self, sink: _Sink) -> None:
        self.sink = sink

    def write(self, data: bytes, base_position: int) -> None:
        raise NotImplementedError  # pragma: no cover

    def finalize(self) -> None:
        raise NotImplementedError  # pragma: no cover


class _BinaryMapper(_BaseMapper):
    def write(self, data: bytes, base_position: int) -> None:
        if data:
            sources = [(BitSource(base_position + index, 0xFF),) for index in range(len(data))]
            self.sink(data, sources)

    def finalize(self) -> None:
        return None


class _Base64Mapper(_BaseMapper):
    """Strict base64 mapper with per-output-bit provenance."""

    def __init__(self, sink: _Sink) -> None:
        super().__init__(sink)
        # Pending symbols of the current quad: (absolute position, value or -1).
        self._pending: list[tuple[int, int]] = []
        self._padded = False

    def write(self, data: bytes, base_position: int) -> None:
        for relative, char in enumerate(data):
            if char in _B64_WHITESPACE:
                continue
            position = base_position + relative
            if char == ord("="):
                if self._padded:
                    raise ProvenanceDecodeError("Unexpected base64 padding symbol")
                value = -1
            elif char in _B64_SYMBOLS:
                if self._padded:
                    raise ProvenanceDecodeError("Base64 data found after padding")
                value = _B64_VALUE[char]
            else:
                raise ProvenanceDecodeError(f"Invalid base64 symbol {bytes([char])!r} at position {position}")
            self._pending.append((position, value))
            if len(self._pending) == 4:
                self._emit_quad()

    def _emit_quad(self) -> None:
        (p0, v0), (p1, v1), (p2, v2), (p3, v3) = self._pending
        if v0 < 0 or v1 < 0:
            raise ProvenanceDecodeError("Base64 padding is only allowed at the end of a quad")
        output = bytearray()
        sources: list[tuple[BitSource, ...]] = []
        output.append((v0 << 2) | (v1 >> 4))
        sources.append((BitSource(p0, 0xFC), BitSource(p1, 0x03)))
        if v2 < 0:
            if v3 >= 0 or v1 & 0x0F:
                raise ProvenanceDecodeError("Malformed base64 closing padding")
            self._padded = True
        else:
            output.append(((v1 & 0x0F) << 4) | (v2 >> 2))
            sources.append((BitSource(p1, 0xF0), BitSource(p2, 0x0F)))
            if v3 < 0:
                if v2 & 0x03:
                    raise ProvenanceDecodeError("Malformed base64 closing padding")
                self._padded = True
            else:
                output.append(((v2 & 0x03) << 6) | v3)
                sources.append((BitSource(p2, 0xC0), BitSource(p3, 0x3F)))
        self._pending.clear()
        self.sink(bytes(output), sources)

    def finalize(self) -> None:
        if self._pending:
            raise ProvenanceDecodeError("Base64 stream ends in the middle of a quad")


class _QPMapper(_BaseMapper):
    """Strict quoted-printable mapper with per-output-bit provenance."""

    def __init__(self, sink: _Sink) -> None:
        super().__init__(sink)
        # 0 = literal, 1 = seen "=", 2 = seen "=\r", 3 = seen "=hex".
        self._state = 0
        self._eq_position = -1
        self._hex_position = -1
        self._hex_value = -1

    def write(self, data: bytes, base_position: int) -> None:
        for relative, char in enumerate(data):
            position = base_position + relative
            if self._state == 0:
                if char == ord("="):
                    self._eq_position = position
                    self._state = 1
                elif char in _QP_LITERALS:
                    self.sink(bytes([char]), [(BitSource(position, 0xFF),)])
                else:
                    raise ProvenanceDecodeError(f"Invalid quoted-printable byte {char!r} at position {position}")
            elif self._state == 1:
                if char == ord("\r"):
                    self._state = 2
                elif char in _HEX_DIGITS:
                    self._hex_position = position
                    self._hex_value = _HEX_VALUE[char]
                    self._state = 3
                else:
                    raise ProvenanceDecodeError(
                        f"'=' must be followed by two hex digits or CRLF at {self._eq_position}"
                    )
            elif self._state == 2:
                if char != ord("\n"):
                    raise ProvenanceDecodeError(f"'=' must be followed by CRLF at {self._eq_position}")
                self._state = 0
            else:
                if char not in _HEX_DIGITS:
                    raise ProvenanceDecodeError(f"Truncated hex escape at {self._eq_position}")
                value = (self._hex_value << 4) | _HEX_VALUE[char]
                self.sink(bytes([value]), [(BitSource(self._hex_position, 0xF0), BitSource(position, 0x0F))])
                self._state = 0

    def finalize(self) -> None:
        if self._state:
            raise ProvenanceDecodeError(
                f"Quoted-printable stream ends with an incomplete escape at {self._eq_position}"
            )


def _build_mapper(encoding: str, sink: _Sink) -> _BaseMapper:
    if encoding == BINARY:
        return _BinaryMapper(sink)
    if encoding == BASE64:
        return _Base64Mapper(sink)
    return _QPMapper(sink)


class ProvenanceLedger:
    """Queryable source ledger for a :class:`NestedFormParser` run.

    Coordinates:

    * **outer layer** records (only kept for selected detailed windows) are
      addressed by window index and the outer part ordinal (1-based, in order
      of appearance); source positions are offsets in the original upload.
    * **inner layer** records are addressed by window index and the inner part
      ordinal; direct source positions address the decoded content of the outer
      field, and :meth:`origins_for_target` composes them down to the original
      upload bytes.
    """

    def __init__(self, windows: Sequence[SourceWindow]) -> None:
        self._windows = tuple(windows)
        self._records: dict[RecordKey, _MappingBuilder] = {}
        self._status: dict[RecordKey, PartStatus] = {}
        self._info: dict[RecordKey, PartInfo] = {}
        self._window_failed: dict[int, bool] = {}
        # Window index -> outer part ordinals carrying that window (occurrences).
        self._outer_ordinals: dict[int, list[int]] = {}
        # Inner record -> outer part ordinal of the occurrence that produced it.
        self._inner_to_outer: dict[RecordKey, int] = {}
        self.closed = False

    @property
    def windows(self) -> tuple[SourceWindow, ...]:
        return self._windows

    def register_part(
        self, layer: int, window: int, part: int, name: bytes | None, filename: bytes | None
    ) -> None:
        self._status[RecordKey(layer, window, part)] = PROVISIONAL
        self._info[RecordKey(layer, window, part)] = PartInfo(name, filename)

    def add_source(self, key: RecordKey, sources: tuple[BitSource, ...]) -> None:
        self._records.setdefault(key, _MappingBuilder()).add(sources)

    def mark_failed(self, key: RecordKey) -> None:
        self._status[key] = FAILED
        self._window_failed[key.window] = True

    def mark_window_failed(self, window: int) -> None:
        self._window_failed[window] = True

    def settle(self) -> None:
        for key, status in list(self._status.items()):
            if status is PROVISIONAL:
                self._status[key] = AUDITABLE

    def window_failed(self, window: int) -> bool:
        return self._window_failed.get(window, False)

    def status(self, layer: int, window: int, part: int) -> PartStatus:
        return self._status[RecordKey(layer, window, part)]

    def part_info(self, layer: int, window: int, part: int) -> PartInfo:
        return self._info[RecordKey(layer, window, part)]

    def parts(self) -> tuple[tuple[RecordKey, PartStatus, PartInfo], ...]:
        return tuple((key, self._status[key], self._info[key]) for key in sorted(self._status))

    def bind_outer_ordinal(self, window: int, outer_part: int) -> None:
        self._outer_ordinals.setdefault(window, []).append(outer_part)

    def bind_inner_occurrence(self, key: RecordKey, outer_part: int) -> None:
        self._inner_to_outer[key] = outer_part

    def target_sources(self, layer: int, window: int, part: int, offset: int) -> tuple[BitSource, ...]:
        """Return the direct source bits of one delivered byte (layer-local)."""
        builder = self._records.get(RecordKey(layer, window, part))
        if builder is None:
            raise KeyError("No detailed relations were recorded for this part")
        return builder.target_sources(offset)

    def part_length(self, layer: int, window: int, part: int) -> int:
        """Number of delivered bytes with recorded relations for one part."""
        builder = self._records.get(RecordKey(layer, window, part))
        if builder is None:
            raise KeyError("No detailed relations were recorded for this part")
        return builder.length

    def origins_for_target(self, layer: int, window: int, part: int, offset: int) -> tuple[int, ...]:
        """Return the distinct original upload bytes behind one delivered byte.

        Two intermediate routes may converge on the same original byte; the
        byte is reported once while every route still resolves through
        :meth:`target_sources`.
        """
        builder = self._records.get(RecordKey(layer, window, part))
        if builder is None:
            raise KeyError("No detailed relations were recorded for this part")
        direct = builder.target_sources(offset)
        if layer == OUTER:
            return tuple(sorted({source.position for source in direct}))
        key = RecordKey(layer, window, part)
        outer_builder = self._records.get(RecordKey(OUTER, window, self._inner_to_outer[key]))
        if outer_builder is None:
            raise KeyError("No detailed relations were recorded for this window")
        originals: set[int] = set()
        shift = self._windows[window].window_lo
        for intermediate in direct:
            for source in outer_builder.target_sources(intermediate.position + shift):
                if intermediate.mask & source.mask:
                    originals.add(source.position)
        return tuple(sorted(originals))

    def targets_for_origin(self, original_position: int) -> tuple[TargetRef, ...]:
        """Return every delivered byte that an original upload byte feeds."""
        refs: set[TargetRef] = set()
        for key, builder in self._records.items():
            if key.layer == OUTER:
                for offset, _mask in builder.targets_for_source(original_position):
                    refs.add(TargetRef(OUTER, key.window, key.part, offset))
                continue
            outer_part = self._inner_to_outer.get(key)
            if outer_part is None:
                continue
            outer_builder = self._records.get(RecordKey(OUTER, key.window, outer_part))
            if outer_builder is None:
                continue
            shift = self._windows[key.window].window_lo
            for offset, intermediate_sources in builder.iter_targets():
                if self._resolves_to(outer_builder, intermediate_sources, shift, original_position):
                    refs.add(TargetRef(INNER, key.window, key.part, offset))
        return tuple(sorted(refs, key=lambda ref: (ref.layer, ref.window, ref.part, ref.offset)))

    def _resolves_to(
        self, outer_builder: _MappingBuilder, sources: tuple[BitSource, ...], shift: int, original_position: int
    ) -> bool:
        for intermediate in sources:
            for source in outer_builder.target_sources(intermediate.position + shift):
                if source.position == original_position and (intermediate.mask & source.mask):
                    return True
        return False


class _PositionTracker:
    """Resolves absolute stream positions of MultipartParser data callbacks.

    A boundary candidate split across packets is held back and, once it turns
    out to be part data, re-emitted from the parser's boundary constant rather
    than from the current chunk.  Its bytes still carry their original
    positions, recovered from the negative mark carried between writes.
    """

    def __init__(self) -> None:
        self.total = 0
        self._chunk_base = 0
        self._chunk: bytes | None = None
        self._carry_origin: int | None = None

    def begin_write(self, parser: MultipartParser, chunk: bytes) -> None:
        self._chunk_base = self.total
        self._chunk = chunk
        mark = parser.marks.get("part_data")
        self._carry_origin = self.total + mark if isinstance(mark, int) and mark < 0 else None

    def end_write(self, processed: int) -> None:
        self.total += processed

    def resolve(self, data: bytes, start: int, end: int) -> range:
        if data is self._chunk:
            return range(self._chunk_base + start, self._chunk_base + end)
        assert self._carry_origin is not None
        return range(self._carry_origin + start, self._carry_origin + end)


@dataclass
class _WindowRuntime:
    spec: SourceWindow
    index: int
    outer_ordinal: int
    detailed: bool
    inner_parser: MultipartParser
    inner_tracker: _PositionTracker
    document_mapper: _BaseMapper
    decoded_length: int = 0
    failed: bool = False
    current_inner_part: int = 0
    inner_product: Field | File | None = None
    part_mapper: _BaseMapper | None = None
    header_name: list[bytes] = field(default_factory=list)
    header_value: list[bytes] = field(default_factory=list)
    headers: dict[bytes, bytes] = field(default_factory=dict)


@dataclass
class _OuterPart:
    product: Field | File
    mapper: _BaseMapper


class NestedFormParser:
    """Streaming parser for an outer multipart message with nested form fields.

    Args:
        outer_boundary: Boundary of the outer multipart message.
        inner_forms: Up-front declaration of the outer fields that carry nested
            forms, their boundaries, encodings and content windows.
        on_field: Called for every ordinary outer field as it completes.
        on_file: Called for every outer file part as it completes.
        on_inner_field: Called for every nested field as it completes.
        on_inner_file: Called for every nested file part as it completes.
        on_end: Called once the outer message has closed and audit results have
            been settled.
        detailed_windows: Window indices whose full byte-level relations are
            kept; other windows are parsed but keep no per-byte detail.
        config: :class:`File` configuration for spooled uploads.
    """

    def __init__(
        self,
        outer_boundary: bytes | str,
        inner_forms: Iterable[SourceWindow | dict[str, Any]],
        *,
        on_field: Callable[[Field], None] | None = None,
        on_file: Callable[[File], None] | None = None,
        on_inner_field: Callable[[Field], None] | None = None,
        on_inner_file: Callable[[File], None] | None = None,
        on_end: Callable[[], None] | None = None,
        detailed_windows: Iterable[int] = (),
        config: FileConfig = {},
    ) -> None:
        self.windows = tuple(self._coerce_window(spec) for spec in inner_forms)
        names = [window.field_name for window in self.windows]
        if len(names) != len(set(names)):
            raise ValueError("Nested SourceWindow entries must have distinct field names")
        selected = set(detailed_windows)
        if any(index < 0 or index >= len(self.windows) for index in selected):
            raise ValueError("detailed_windows contains an unknown window index")
        self._detailed = selected
        self._config = config
        self._on_field = on_field
        self._on_file = on_file
        self._on_inner_field = on_inner_field
        self._on_inner_file = on_inner_file
        self._on_end = on_end

        self.ledger = ProvenanceLedger(self.windows)
        self._runtimes: dict[int, _WindowRuntime] = {}
        self._inner_ordinal: dict[int, int] = {}
        self._ordinal = 0
        self._current: _OuterPart | None = None
        self._tracker = _PositionTracker()
        self.failures: list[str] = []
        self._header_name: list[bytes] = []
        self._header_value: list[bytes] = []
        self._headers: dict[bytes, bytes] = {}

        callbacks: MultipartCallbacks = {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
        }
        self.parser = MultipartParser(outer_boundary, callbacks=callbacks)

    @staticmethod
    def _coerce_window(spec: SourceWindow | dict[str, Any]) -> SourceWindow:
        if isinstance(spec, SourceWindow):
            return spec
        return SourceWindow(**spec)

    def write(self, data: bytes) -> int:
        """Feed one packet of the original upload stream."""
        self._tracker.begin_write(self.parser, data)
        processed = self.parser.write(data)
        self._tracker.end_write(processed)
        return processed

    def finalize(self) -> None:
        """Settle provisional parts into auditable results, or raise.

        Raises :class:`ProvenanceError` when the outer message never closed.
        Provisional parts stay queryable in the ledger either way, but only a
        closed run settles them to auditable.
        """
        if self.parser.state is not END:
            raise ProvenanceError("Outer multipart message is not closed; provisional parts remain unsigned")
        for runtime in self._runtimes.values():
            if not runtime.failed:
                self._finalize_window(runtime)
        self.ledger.settle()
        self.ledger.closed = True
        if self._on_end is not None:
            self._on_end()

    def _finalize_window(self, runtime: _WindowRuntime) -> None:
        try:
            runtime.document_mapper.finalize()
            if runtime.inner_parser.state is not END:
                raise ProvenanceError("Nested multipart message is not closed")
        except (ProvenanceDecodeError, MultipartParseError, ProvenanceError) as exc:
            self._fail_window(runtime, str(exc))

    def _fail_window(self, runtime: _WindowRuntime, message: str) -> None:
        runtime.failed = True
        self.failures.append(message)
        self.ledger.mark_window_failed(runtime.index)
        if runtime.current_inner_part:
            self.ledger.mark_failed(RecordKey(INNER, runtime.index, runtime.current_inner_part))

    def _on_part_begin(self) -> None:
        self._ordinal += 1
        self._headers = {}

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name.append(data[start:end])

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.append(data[start:end])

    def _on_header_end(self) -> None:
        self._headers[b"".join(self._header_name).lower()] = b"".join(self._header_value).strip(b" \t")
        self._header_name.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        content_disp = self._headers.get(b"content-disposition")
        _, options = parse_options_header(content_disp)
        name = options.get(b"name")
        if name is None:
            raise FormParserError(f'Field name not found in Content-Disposition: "{content_disp!r}"')
        filename = options.get(b"filename")
        content_type = self._headers.get(b"content-type")
        if filename is None:
            product: Field | File = Field(name, content_type=content_type.decode("latin-1") if content_type else None)
        else:
            product = File(
                filename,
                name,
                config=self._config,
                content_type=content_type.decode("latin-1") if content_type else None,
            )

        window_index = next((index for index, window in enumerate(self.windows) if window.field_name == name), None)
        runtime = self._build_window(window_index, self._ordinal) if window_index is not None else None
        outer_encoding = self._transfer_encoding(self._headers.get(b"content-transfer-encoding", b"7bit").lower())
        outer_record_key = (
            RecordKey(OUTER, runtime.index, self._ordinal) if runtime is not None and runtime.detailed else None
        )
        def outer_sink(data: bytes, sources: list[tuple[BitSource, ...]]) -> None:
            product.write(data)
            if outer_record_key is not None:
                for sources_for_byte in sources:
                    self.ledger.add_source(outer_record_key, sources_for_byte)
            if runtime is not None:
                self._route_to_window(runtime, data)

        part = _OuterPart(
            product=product,
            mapper=_build_mapper(outer_encoding, outer_sink),
        )
        self._current = part
        if runtime is not None:
            self.ledger.register_part(OUTER, runtime.index, self._ordinal, name, filename)
            self.ledger.bind_outer_ordinal(runtime.index, self._ordinal)

    def _build_window(self, window_index: int, outer_ordinal: int) -> _WindowRuntime:
        spec = self.windows[window_index]
        detailed = window_index in self._detailed
        runtime = _WindowRuntime(
            spec=spec,
            index=window_index,
            outer_ordinal=outer_ordinal,
            detailed=detailed,
            inner_parser=cast(MultipartParser, None),
            inner_tracker=_PositionTracker(),
            document_mapper=cast(_BaseMapper, None),
        )

        def document_sink(data: bytes, _sources: list[tuple[BitSource, ...]]) -> None:
            runtime.inner_tracker.begin_write(runtime.inner_parser, data)
            try:
                runtime.inner_parser.write(data)
            finally:
                runtime.inner_tracker.end_write(len(data))

        runtime.document_mapper = _build_mapper(spec.encoding, document_sink)
        runtime.inner_parser = MultipartParser(
            spec.boundary,
            callbacks={
                "on_part_begin": lambda: self._inner_part_begin(runtime),
                "on_header_field": lambda data, start, end: runtime.header_name.append(data[start:end]),
                "on_header_value": lambda data, start, end: runtime.header_value.append(data[start:end]),
                "on_header_end": lambda: self._inner_header_end(runtime),
                "on_headers_finished": lambda: self._inner_headers_finished(runtime),
                "on_part_data": lambda data, start, end: self._inner_part_data(runtime, data, start, end),
                "on_part_end": lambda: self._inner_part_end(runtime),
            },
        )
        self._runtimes[outer_ordinal] = runtime
        return runtime

    @staticmethod
    def _transfer_encoding(header: bytes) -> str:
        if header in (b"binary", b"8bit", b"7bit"):
            return BINARY
        if header == b"base64":
            return BASE64
        if header == b"quoted-printable":
            return QUOTED_PRINTABLE
        raise FormParserError(f'Unknown Content-Transfer-Encoding "{header!r}"')

    def _route_to_window(self, runtime: _WindowRuntime, data: bytes) -> None:
        if runtime.failed:
            runtime.decoded_length += len(data)
            return
        spec = runtime.spec
        base = runtime.decoded_length
        runtime.decoded_length += len(data)
        lo = spec.window_lo
        hi = spec.window_hi
        if base >= (hi if hi is not None else float("inf")) or runtime.decoded_length <= lo:
            return
        keep_lo = max(base, lo)
        keep_hi = min(runtime.decoded_length, hi) if hi is not None else runtime.decoded_length
        slice_lo = keep_lo - base
        slice_hi = keep_hi - base
        try:
            runtime.document_mapper.write(data[slice_lo:slice_hi], keep_lo)
        except FormParserError as exc:
            self._fail_window(runtime, str(exc))

    def _inner_header_end(self, runtime: _WindowRuntime) -> None:
        runtime.headers[b"".join(runtime.header_name).lower()] = b"".join(runtime.header_value).strip(b" \t")
        runtime.header_name.clear()
        runtime.header_value.clear()

    def _inner_part_begin(self, runtime: _WindowRuntime) -> None:
        runtime.current_inner_part = self._inner_ordinal.get(runtime.index, 0) + 1
        self._inner_ordinal[runtime.index] = runtime.current_inner_part
        runtime.headers = {}

    def _inner_headers_finished(self, runtime: _WindowRuntime) -> None:
        _, options = parse_options_header(runtime.headers.get(b"content-disposition"))
        name = options.get(b"name")
        filename = options.get(b"filename")
        if name is None:
            raise FormParserError("Nested part is missing a Content-Disposition name")
        if filename is None:
            runtime.inner_product = Field(name)
        else:
            runtime.inner_product = File(filename, name, config=self._config)
        inner_key = RecordKey(INNER, runtime.index, runtime.current_inner_part)
        self.ledger.register_part(INNER, runtime.index, runtime.current_inner_part, name, filename)
        self.ledger.bind_inner_occurrence(inner_key, runtime.outer_ordinal)
        encoding = self._transfer_encoding(runtime.headers.get(b"content-transfer-encoding", b"7bit").lower())
        record_key = inner_key if runtime.detailed else None

        def part_sink(data: bytes, sources: list[tuple[BitSource, ...]]) -> None:
            assert runtime.inner_product is not None
            runtime.inner_product.write(data)
            if record_key is None:
                return
            for sources_for_byte in sources:
                self.ledger.add_source(record_key, sources_for_byte)

        runtime.part_mapper = _build_mapper(encoding, part_sink)

    def _inner_part_data(self, runtime: _WindowRuntime, data: bytes, start: int, end: int) -> None:
        assert runtime.part_mapper is not None
        positions = runtime.inner_tracker.resolve(data, start, end)
        try:
            runtime.part_mapper.write(data[start:end], positions.start)
        except ProvenanceDecodeError as exc:
            self._fail_window(runtime, str(exc))

    def _inner_part_end(self, runtime: _WindowRuntime) -> None:
        assert runtime.inner_product is not None
        assert runtime.part_mapper is not None
        try:
            runtime.part_mapper.finalize()
        except ProvenanceDecodeError as exc:
            self._fail_window(runtime, str(exc))
        product = runtime.inner_product
        product.finalize()
        if self.ledger.status(INNER, runtime.index, runtime.current_inner_part) is FAILED:
            return
        if isinstance(product, File):
            if self._on_inner_file is not None:
                self._on_inner_file(product)
        elif self._on_inner_field is not None:
            self._on_inner_field(product)

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        assert self._current is not None
        part = self._current
        positions = self._tracker.resolve(data, start, end)
        part.mapper.write(data[start:end], positions.start)

    def _on_part_end(self) -> None:
        assert self._current is not None
        part = self._current
        part.product.finalize()
        if isinstance(part.product, File):
            if self._on_file is not None:
                self._on_file(part.product)
        elif self._on_field is not None:
            self._on_field(cast(Field, part.product))


__all__ = (
    "AUDITABLE",
    "BASE64",
    "BINARY",
    "BitSource",
    "FAILED",
    "Field",
    "File",
    "INNER",
    "OUTER",
    "PartInfo",
    "PartStatus",
    "PROVISIONAL",
    "ProvenanceDecodeError",
    "ProvenanceError",
    "ProvenanceLedger",
    "QUOTED_PRINTABLE",
    "SourceWindow",
    "TargetRef",
    "NestedFormParser",
)
