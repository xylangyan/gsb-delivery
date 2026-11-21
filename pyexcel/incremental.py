"""
pyexcel.incremental
~~~~~~~~~~~~~~~~~~~

Incremental, resumable projection of headerless CSV files.

The classes in this module are meant for import pipelines that receive a
headerless CSV file, project a fixed set of named columns out of every logical
record and confirm each record asynchronously.  A confirmation persists a
small checkpoint (a byte offset plus counters) so that the next run can
continue with the same unchanged file without replaying confirmed records.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import codecs
import io
import json
import os
import tempfile
from collections import namedtuple
from enum import Enum

from pyexcel_io import service

__all__ = [
    "CSVProjection",
    "FieldUpdate",
    "ProjectedRecord",
    "ProjectionSpec",
    "Progress",
    "SourcePosition",
    "UpdateIntent",
    "ProjectionError",
    "ProjectionConfigurationError",
    "ProgressError",
    "StaleProgressError",
    "CSVFormatError",
    "MalformedCSVError",
    "RecordTooWideError",
    "UnterminatedFieldError",
    "project_csv",
    "resume_csv",
]


PROGRESS_FORMAT = "pyexcel-csv-projection-progress"
PROGRESS_VERSION = 1

_CR = 13
_LF = 10
_UTF8_BOM = b"\xef\xbb\xbf"


def _convert_value(text):
    """Convert like pyexcel's default CSV reader does.

    Keeping the same heuristics means a projected value is the value the
    ordinary :func:`pyexcel.get_sheet` path would produce.
    """
    if text == "":
        return ""
    value = service.detect_int_value(text)
    if value is None:
        value = service.detect_float_value(text)
        if value in (float("inf"), float("-inf")):
            value = None
    if value is None:
        value = service.detect_date_value(text)
    if value is None:
        value = text
    return value


class UpdateIntent(Enum):
    """What an import should do with one projected field.

    ``ABSENT``
        The logical record has no such column (for example a short row).
        The stored value must be left untouched.

    ``CLEAR``
        The column exists but its value is empty, either as an explicit
        empty field or as a quoted empty string.  The stored value must be
        cleared.

    ``SET``
        The column exists and carries a value.  ``"0"`` is a value and is
        therefore different from both ``ABSENT`` and ``CLEAR``.
    """

    ABSENT = "absent"
    CLEAR = "clear"
    SET = "set"


class ProjectionError(Exception):
    """Base class for projection failures."""


class ProjectionConfigurationError(ProjectionError, ValueError):
    """The projection was configured inconsistently."""


class ProgressError(ProjectionError):
    """A checkpoint file cannot be read or trusted."""


class StaleProgressError(ProgressError):
    """The source file changed since the checkpoint was written."""


class CSVFormatError(ProjectionError):
    """A CSV record cannot be projected.

    The error always points at the logical record (``record_index``, counted
    from zero) and, where meaningful, at the offending column
    (``column_index`` and ``column_name``) and its physical location
    (``position``, ``line_number``).
    """

    def __init__(
        self,
        message,
        record_index=None,
        column_index=None,
        column_name=None,
        line_number=None,
        position=None,
    ):
        super().__init__(message)
        self.record_index = record_index
        self.column_index = column_index
        self.column_name = column_name
        self.line_number = line_number
        self.position = position

    @property
    def record_number(self):
        """One-based logical record number, or ``None``."""
        if self.record_index is None:
            return None
        return self.record_index + 1

    @property
    def column_number(self):
        """One-based column number, or ``None``."""
        if self.column_index is None:
            return None
        return self.column_index + 1


class MalformedCSVError(CSVFormatError):
    """A quoted field is malformed (for example text after a closing quote)."""


class RecordTooWideError(CSVFormatError):
    """A record has more fields than the declared file schema.

    ``column_index`` is the zero-based index of the first extra field and
    ``extra_fields`` holds the surplus values.
    """

    def __init__(self, message, extra_fields=(), **keywords):
        super().__init__(message, **keywords)
        self.extra_fields = tuple(extra_fields)


class UnterminatedFieldError(CSVFormatError):
    """The file ends while a quoted field is still open."""


SourcePosition = namedtuple(
    "SourcePosition",
    "offset end_offset line_number end_line_number",
)
SourcePosition.__doc__ = """Physical location of a logical record.

``offset`` and ``end_offset`` are byte offsets into the source file.  The
offset points at the first byte of the record and ``end_offset`` points at
the first byte after the record terminator (or at end of file).  Line numbers
are one-based; ``end_line_number`` is the line of the byte at ``end_offset``.
"""


FieldUpdate = namedtuple(
    "FieldUpdate",
    "column column_index intent text value quoted",
)
FieldUpdate.__doc__ = """One of the projected columns of a logical record.

``intent`` is an :class:`UpdateIntent`.  ``value`` carries the intended new
value: ``None`` for ``ABSENT``, ``""`` for ``CLEAR`` and the detected scalar
(``"0"`` becomes ``0``) for ``SET``.  ``text`` keeps the original text.
"""

FieldUpdate.present = property(
    lambda self: self.intent is not UpdateIntent.ABSENT
)
FieldUpdate.is_absent = property(
    lambda self: self.intent is UpdateIntent.ABSENT
)
FieldUpdate.is_clear = property(
    lambda self: self.intent is UpdateIntent.CLEAR
)


Progress = namedtuple("Progress", "offset record_index line_number")
Progress.__doc__ = """A checkpoint written after a confirmed record.

Only counters are kept; no part of the source content is referenced.
"""


class ProjectionSpec:
    """The declared, headerless schema of a CSV file.

    :param field_names: ordered logical names of every column in the file.
    :param columns: ordered names to project; each name must exist in
        ``field_names``.
    """

    def __init__(self, columns, field_names):
        if isinstance(columns, str) or not columns:
            raise ProjectionConfigurationError(
                "columns must be a non-empty sequence of column names"
            )
        if isinstance(field_names, str) or not field_names:
            raise ProjectionConfigurationError(
                "field_names must be a non-empty sequence of column names"
            )
        columns = tuple(columns)
        field_names = tuple(field_names)
        if len(set(field_names)) != len(field_names):
            raise ProjectionConfigurationError(
                "field_names contains duplicate names"
            )
        unknown = [name for name in columns if name not in field_names]
        if unknown:
            raise ProjectionConfigurationError(
                "projected columns are not part of the file schema: %s"
                % ", ".join(repr(name) for name in unknown)
            )
        if len(set(columns)) != len(columns):
            raise ProjectionConfigurationError(
                "columns contains duplicate names"
            )
        self.columns = columns
        self.field_names = field_names
        self.column_indexes = tuple(
            field_names.index(name) for name in columns
        )

    def column_name(self, column_index):
        if column_index is None:
            return None
        if 0 <= column_index < len(self.field_names):
            return self.field_names[column_index]
        return None

    def __eq__(self, other):
        return (
            isinstance(other, ProjectionSpec)
            and self.columns == other.columns
            and self.field_names == other.field_names
        )

    def __repr__(self):
        return "ProjectionSpec(columns=%r, field_names=%r)" % (
            self.columns,
            self.field_names,
        )


class ProjectedRecord:
    """A logical CSV record projected onto the configured columns."""

    __slots__ = ("index", "position", "updates")

    def __init__(self, index, position, updates):
        self.index = index
        self.position = position
        self.updates = updates

    @property
    def columns(self):
        return tuple(self.updates)

    def __getitem__(self, column):
        if isinstance(column, int):
            return tuple(self.updates.values())[column]
        return self.updates[column]

    def get(self, column, default=None):
        return self.updates.get(column, default)

    def to_dict(self):
        """Return ``{column: intended value}`` in projection order."""
        return {
            column: update.value for column, update in self.updates.items()
        }

    def intents(self):
        """Return ``{column: UpdateIntent}`` in projection order."""
        return {
            column: update.intent for column, update in self.updates.items()
        }

    def values(self):
        """Return the intended values as a tuple in projection order."""
        return tuple(update.value for update in self.updates.values())

    def __repr__(self):
        return "ProjectedRecord(index=%r, position=%r, values=%r)" % (
            self.index,
            self.position,
            self.values(),
        )


class _ByteReader:
    """A small chunked byte reader with one-byte look-ahead."""

    __slots__ = ("_stream", "_chunk_size", "_buffer", "_index", "_base")

    def __init__(
        self, stream, chunk_size, start_offset=0, initial=b"", positioned=False
    ):
        self._stream = stream
        self._chunk_size = chunk_size
        self._buffer = initial
        self._index = 0
        self._base = start_offset
        if not initial and not positioned:
            self._seek_or_skip(start_offset)

    def _seek_or_skip(self, offset):
        try:
            self._stream.seek(offset)
            return
        except (AttributeError, io.UnsupportedOperation, OSError):
            pass
        if offset <= 0:
            return
        remaining = offset
        while remaining:
            chunk = self._stream.read(min(remaining, self._chunk_size))
            if not chunk:
                raise ProgressError(
                    "checkpoint offset %s is beyond the end of the source"
                    % offset
                )
            remaining -= len(chunk)

    def _fill(self):
        if self._index < len(self._buffer):
            return True
        self._base += len(self._buffer)
        self._buffer = self._stream.read(self._chunk_size)
        self._index = 0
        if not self._buffer:
            self._buffer = b""
            return False
        return True

    def read(self):
        if not self._fill():
            return None
        byte = self._buffer[self._index]
        self._index += 1
        return byte

    def peek(self):
        if not self._fill():
            return None
        return self._buffer[self._index]

    def tell(self):
        return self._base + self._index


class _ParsedRecord:
    __slots__ = ("fields", "quoted", "position")

    def __init__(self, fields, quoted, position):
        self.fields = fields
        self.quoted = quoted
        self.position = position


def _as_single_byte(character, name, encoding):
    if not isinstance(character, str) or len(character) != 1:
        raise ProjectionConfigurationError(
            "%s must be a single character" % name
        )
    try:
        encoded = character.encode(encoding)
    except UnicodeEncodeError:
        raise ProjectionConfigurationError(
            "%s cannot be encoded with %s" % (name, encoding)
        )
    if len(encoded) != 1:
        raise ProjectionConfigurationError(
            "%s must be a single byte with encoding %s" % (name, encoding)
        )
    return encoded[0]


def _validate_encoding(encoding, delimiter, quotechar):
    if delimiter == quotechar:
        raise ProjectionConfigurationError(
            "delimiter and quotechar must differ"
        )
    for character, name in (
        (quotechar, "quotechar"),
        (delimiter, "delimiter"),
        ("\r", "carriage return"),
        ("\n", "line feed"),
    ):
        _as_single_byte(character, name, encoding)
    try:
        codecs.lookup(encoding)
    except LookupError:
        raise ProjectionConfigurationError(
            "unknown encoding: %s" % encoding
        )


def _normalise_encoding(encoding):
    if not isinstance(encoding, str) or not encoding:
        raise ProjectionConfigurationError("encoding must be a string")
    name = encoding.lower().replace("_", "-")
    if name in ("utf-8-sig", "utf8-sig", "utf-8sig"):
        return "utf-8", True
    return encoding, False


def _iter_parsed_records(
    stream,
    chunk_size,
    start_offset,
    start_line,
    encoding,
    strip_bom,
    delimiter,
    quotechar,
    skip_blank_lines,
    strict,
):
    delimiter_byte = _as_single_byte(delimiter, "delimiter", encoding)
    quote_byte = _as_single_byte(quotechar, "quotechar", encoding)
    reader = _ByteReader(stream, chunk_size, start_offset)

    if start_offset == 0 and strip_bom:
        prefix = b""
        while len(prefix) < 3:
            chunk = stream.read(1)
            if not chunk:
                break
            prefix += chunk
        if prefix == _UTF8_BOM:
            reader = _ByteReader(stream, chunk_size, 3, positioned=True)
        else:
            reader = _ByteReader(
                stream, chunk_size, 0, initial=prefix, positioned=True
            )

    line = start_line

    while True:
        record_start = reader.tell()
        record_line = line
        fields = []
        quoted_flags = []
        field = bytearray()
        field_quoted = False
        field_start_line = line
        field_start_offset = record_start
        state = 0  # 0 field start, 1 unquoted, 2 quoted, 3 quote in quoted
        terminated = False
        consumed_anything = False

        while True:
            byte = reader.read()
            if byte is None:
                break
            consumed_anything = True

            if state == 0:  # field start
                if byte == delimiter_byte:
                    fields.append("")
                    quoted_flags.append(False)
                elif byte == quote_byte:
                    state = 2
                    field_quoted = True
                    field_start_line = line
                    field_start_offset = reader.tell() - 1
                elif byte in (_CR, _LF):
                    if fields:
                        fields.append("")
                        quoted_flags.append(False)
                    if byte == _CR:
                        if reader.peek() == _LF:
                            reader.read()
                        line += 1
                    else:
                        line += 1
                    terminated = True
                    break
                else:
                    field.append(byte)
                    state = 1

            elif state == 1:  # unquoted
                if byte == delimiter_byte:
                    fields.append(bytes(field).decode(encoding))
                    quoted_flags.append(False)
                    field = bytearray()
                    state = 0
                elif byte in (_CR, _LF):
                    fields.append(bytes(field).decode(encoding))
                    quoted_flags.append(False)
                    field = bytearray()
                    if byte == _CR:
                        if reader.peek() == _LF:
                            reader.read()
                        line += 1
                    else:
                        line += 1
                    terminated = True
                    break
                else:
                    field.append(byte)

            elif state == 2:  # quoted
                if byte == quote_byte:
                    state = 3
                elif byte == _CR:
                    if reader.peek() == _LF:
                        reader.read()
                    field.append(_LF)
                    line += 1
                elif byte == _LF:
                    field.append(byte)
                    line += 1
                else:
                    field.append(byte)

            else:  # quote in quoted
                if byte == quote_byte:
                    field.append(byte)
                    state = 2
                elif byte == delimiter_byte:
                    fields.append(bytes(field).decode(encoding))
                    quoted_flags.append(True)
                    field = bytearray()
                    state = 0
                elif byte in (_CR, _LF):
                    fields.append(bytes(field).decode(encoding))
                    quoted_flags.append(True)
                    field = bytearray()
                    if byte == _CR:
                        if reader.peek() == _LF:
                            reader.read()
                        line += 1
                    else:
                        line += 1
                    terminated = True
                    break
                elif strict:
                    raise MalformedCSVError(
                        "unexpected character %r after a closing quote"
                        % chr(byte),
                        column_index=len(fields),
                        line_number=line,
                        position=SourcePosition(
                            record_start,
                            reader.tell(),
                            record_line,
                            line,
                        ),
                    )
                else:
                    field.append(byte)
                    state = 2

        end_offset = reader.tell()
        position = SourcePosition(
            record_start, end_offset, record_line, line
        )

        if not terminated:
            if state == 2:
                raise UnterminatedFieldError(
                    "file ends inside a quoted field",
                    column_index=len(fields),
                    line_number=field_start_line,
                    position=position,
                )
            if not consumed_anything:
                return
            if state == 0:
                if fields:
                    fields.append("")
                    quoted_flags.append(False)
                elif not field_quoted:
                    return
            else:
                fields.append(bytes(field).decode(encoding))
                quoted_flags.append(state == 3)

        blank = not fields
        if blank:
            if not skip_blank_lines:
                yield _ParsedRecord([], [], position)
            continue

        yield _ParsedRecord(fields, quoted_flags, position)


class CSVProjection:
    """Project named columns out of a headerless CSV file, incrementally.

    :param columns: ordered column names to project.
    :param field_names: ordered names of all columns in the file.  The file
        is headerless; these names describe its positional schema.
    :param file_name: path of the CSV file.
    :param file_stream: binary stream instead of ``file_name``.
    :param file_content: bytes instead of ``file_name`` or ``file_stream``.
    :param progress_file: path of the JSON checkpoint.  When omitted,
        confirmations are kept in memory only.
    :param encoding: source encoding.  ``utf-8-sig`` (the default) removes a
        leading BOM; any ASCII-compatible, single-byte-structural encoding
        works.
    :param chunk_size: number of bytes read per chunk.
    :param delimiter: field delimiter, one character.
    :param quotechar: quote character, one character.
    :param strict: reject text after a closing quote.
    :param skip_blank_lines: do not deliver records for empty physical lines.

    Iterating yields :class:`ProjectedRecord` objects.  After the downstream
    consumer has confirmed a record, call :meth:`ack` with that record; the
    checkpoint then points at the first byte after it.  A later
    ``CSVProjection`` using the same ``progress_file`` and the same unchanged
    source resumes at that byte and never replays confirmed records.
    """

    def __init__(
        self,
        columns,
        field_names,
        file_name=None,
        file_stream=None,
        file_content=None,
        progress_file=None,
        encoding="utf-8-sig",
        chunk_size=65536,
        delimiter=",",
        quotechar='"',
        strict=True,
        skip_blank_lines=True,
    ):
        provided = [
            value is not None
            for value in (file_name, file_stream, file_content)
        ]
        if sum(provided) != 1:
            raise ProjectionConfigurationError(
                "exactly one of file_name, file_stream or file_content "
                "must be given"
            )
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ProjectionConfigurationError(
                "chunk_size must be a positive integer"
            )

        self.spec = ProjectionSpec(columns, field_names)
        self._file_name = file_name
        self._stream = file_stream
        self._file_content = file_content
        self._progress_file = progress_file
        self._encoding, self._strip_bom = _normalise_encoding(encoding)
        _validate_encoding(self._encoding, delimiter, quotechar)
        self._chunk_size = chunk_size
        self._delimiter = delimiter
        self._quotechar = quotechar
        self._strict = bool(strict)
        self._skip_blank_lines = bool(skip_blank_lines)

        if file_name is not None:
            if not os.path.isfile(file_name):
                raise ProjectionConfigurationError(
                    "source file does not exist: %s" % file_name
                )
            stat = os.stat(file_name)
            self._source_size = stat.st_size
            self._source_mtime_ns = getattr(stat, "st_mtime_ns", None)
            self._source_inode = getattr(stat, "st_ino", None)
            self._source_name = os.path.abspath(file_name)
        elif file_content is not None:
            if not isinstance(file_content, bytes):
                raise ProjectionConfigurationError(
                    "file_content must be bytes"
                )
            self._source_size = len(file_content)
            self._source_mtime_ns = None
            self._source_inode = None
            self._source_name = "<bytes>"
        else:
            if isinstance(file_stream, io.TextIOBase):
                raise ProjectionConfigurationError(
                    "file_stream must be a binary stream"
                )
            self._source_size = self._stream_size(file_stream)
            self._source_mtime_ns = None
            self._source_inode = None
            self._source_name = "<stream>"

        self._iterating = False
        self._progress = Progress(0, 0, 1)
        if progress_file is not None:
            self._progress = self._load_progress()


    @staticmethod
    def _stream_size(stream):
        getbuffer = getattr(stream, "getbuffer", None)
        if getbuffer is not None:
            try:
                return len(getbuffer())
            except (AttributeError, TypeError, ValueError):
                pass
        try:
            position = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(position)
            return size
        except (AttributeError, io.UnsupportedOperation, OSError):
            return None

    @property
    def progress(self):
        """The last confirmed :class:`Progress`."""
        return self._progress

    def _source_identity(self):
        return {
            "name": self._source_name,
            "size": self._source_size,
            "mtime_ns": self._source_mtime_ns,
            "inode": self._source_inode,
        }

    def _projection_identity(self):
        return {
            "columns": list(self.spec.columns),
            "field_names": list(self.spec.field_names),
            "encoding": self._encoding,
            "delimiter": self._delimiter,
            "quotechar": self._quotechar,
            "strict": self._strict,
            "skip_blank_lines": self._skip_blank_lines,
        }

    def _load_progress(self):
        try:
            with open(self._progress_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return Progress(0, 0, 1)
        except (OSError, ValueError) as exc:
            raise ProgressError(
                "cannot read progress file %s: %s"
                % (self._progress_file, exc)
            )
        try:
            if payload.get("format") != PROGRESS_FORMAT:
                raise ProgressError(
                    "unrecognised progress format in %s" % self._progress_file
                )
            if payload.get("version") != PROGRESS_VERSION:
                raise ProgressError(
                    "unsupported progress version in %s"
                    % self._progress_file
                )
            source = payload["source"]
            projection = payload["projection"]
            position = payload["position"]
            offset = int(position["offset"])
            record_index = int(position["record_index"])
            line_number = int(position["line_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProgressError(
                "malformed progress file %s: %s"
                % (self._progress_file, exc)
            )

        expected_source = self._source_identity()
        size = source.get("size")
        if (
            size is not None
            and expected_source["size"] is not None
            and int(size) != int(expected_source["size"])
        ):
            raise StaleProgressError(
                "source size changed since the checkpoint was written"
            )
        stored_mtime = source.get("mtime_ns")
        if (
            stored_mtime is not None
            and expected_source["mtime_ns"] is not None
            and int(stored_mtime) != int(expected_source["mtime_ns"])
        ):
            raise StaleProgressError(
                "source modification time changed since the checkpoint "
                "was written"
            )
        stored_inode = source.get("inode")
        if (
            stored_inode is not None
            and expected_source["inode"] is not None
            and int(stored_inode) != int(expected_source["inode"])
        ):
            raise StaleProgressError(
                "the checkpoint belongs to a different file at %s"
                % expected_source["name"]
            )
        if offset < 0 or record_index < 0 or line_number < 1:
            raise ProgressError(
                "malformed progress position in %s" % self._progress_file
            )
        if (
            expected_source["size"] is not None
            and offset > expected_source["size"]
        ):
            raise StaleProgressError(
                "checkpoint offset is beyond the end of the source"
            )
        if projection != self._projection_identity():
            raise ProjectionConfigurationError(
                "the checkpoint was written for a different projection"
            )
        return Progress(offset, record_index, line_number)

    def _save_progress(self):
        if self._progress_file is None:
            return
        payload = {
            "format": PROGRESS_FORMAT,
            "version": PROGRESS_VERSION,
            "source": self._source_identity(),
            "projection": self._projection_identity(),
            "position": {
                "offset": self._progress.offset,
                "record_index": self._progress.record_index,
                "line_number": self._progress.line_number,
            },
        }
        directory = os.path.dirname(os.path.abspath(self._progress_file))
        handle = None
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".pyexcel-progress-",
                suffix=".tmp",
                dir=directory,
            )
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temporary, self._progress_file)
            temporary = None
        except OSError as exc:
            raise ProgressError(
                "cannot write progress file %s: %s"
                % (self._progress_file, exc)
            )
        finally:
            if handle is not None:
                handle.close()
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _open(self):
        if self._file_name is not None:
            return open(self._file_name, "rb")
        if self._file_content is not None:
            return io.BytesIO(self._file_content)
        stream = self._stream
        if isinstance(stream, io.TextIOBase):
            raise ProjectionConfigurationError(
                "file_stream must be a binary stream"
            )
        return _NonClosingStream(stream)

    def __iter__(self):
        if self._iterating:
            raise ProjectionError("this projection is already being iterated")
        self._iterating = True
        try:
            stream = self._open()
            try:
                yield from self._iterate(stream)
            finally:
                if (
                    self._file_name is not None
                    or self._file_content is not None
                ):
                    stream.close()
        finally:
            self._iterating = False

    def _iterate(self, stream):
        record_index = self._progress.record_index
        records = _iter_parsed_records(
            stream,
            chunk_size=self._chunk_size,
            start_offset=self._progress.offset,
            start_line=self._progress.line_number,
            encoding=self._encoding,
            strip_bom=self._strip_bom,
            delimiter=self._delimiter,
            quotechar=self._quotechar,
            skip_blank_lines=self._skip_blank_lines,
            strict=self._strict,
        )
        width = len(self.spec.field_names)
        while True:
            try:
                parsed = next(records)
            except StopIteration:
                return
            except CSVFormatError as exc:
                if exc.record_index is None:
                    exc.record_index = record_index
                if exc.column_name is None:
                    exc.column_name = self.spec.column_name(
                        exc.column_index
                    )
                raise

            if len(parsed.fields) > width:
                extra = parsed.fields[width:]
                error = RecordTooWideError(
                    "record %d at line %d has %d fields but the schema "
                    "declares %d"
                    % (
                        record_index,
                        parsed.position.line_number,
                        len(parsed.fields),
                        width,
                    ),
                    record_index=record_index,
                    column_index=width,
                    column_name=self.spec.column_name(width),
                    line_number=parsed.position.line_number,
                    position=parsed.position,
                    extra_fields=extra,
                )
                raise error

            yield self._build_record(parsed, record_index)
            record_index += 1

    def _build_record(self, parsed, record_index):
        updates = {}
        for column in self.spec.columns:
            column_index = self.spec.field_names.index(column)
            if column_index >= len(parsed.fields):
                update = FieldUpdate(
                    column,
                    column_index,
                    UpdateIntent.ABSENT,
                    None,
                    None,
                    False,
                )
            else:
                text = parsed.fields[column_index]
                quoted = parsed.quoted[column_index]
                if text == "":
                    update = FieldUpdate(
                        column,
                        column_index,
                        UpdateIntent.CLEAR,
                        "",
                        "",
                        quoted,
                    )
                else:
                    update = FieldUpdate(
                        column,
                        column_index,
                        UpdateIntent.SET,
                        text,
                        _convert_value(text),
                        quoted,
                    )
            updates[column] = update
        return ProjectedRecord(record_index, parsed.position, updates)

    def ack(self, record):
        """Confirm a delivered record and persist the checkpoint.

        Confirmation is cumulative: confirming a later record also confirms
        every record before it.  Confirming a record that is already covered
        by the checkpoint is a no-op.
        """
        if not isinstance(record, ProjectedRecord):
            raise ProjectionConfigurationError(
                "ack() expects a ProjectedRecord"
            )
        if record.columns != self.spec.columns:
            raise ProjectionConfigurationError(
                "the record does not belong to this projection"
            )
        next_index = record.index + 1
        if next_index <= self._progress.record_index:
            return self._progress
        if record.index > self._progress.record_index:
            raise ProjectionError(
                "cannot confirm record %d while record %d is still "
                "unconfirmed"
                % (record.index, self._progress.record_index)
            )
        self._progress = Progress(
            record.position.end_offset,
            next_index,
            record.position.end_line_number,
        )
        self._save_progress()
        return self._progress

    confirm = ack

    def close(self):
        """Compatibility hook; streams are closed by their owners."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class _NonClosingStream:
    """Wrap a caller-owned stream so iteration never closes it."""

    __slots__ = ("_stream",)

    def __init__(self, stream):
        self._stream = stream

    def read(self, size=-1):
        return self._stream.read(size)

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def close(self):
        pass


def project_csv(columns, field_names, **keywords):
    """Return a :class:`CSVProjection` for the given source keywords."""
    return CSVProjection(columns, field_names, **keywords)


def resume_csv(columns, field_names, progress_file, **keywords):
    """Resume a :class:`CSVProjection` from a persisted checkpoint."""
    if not progress_file:
        raise ProjectionConfigurationError(
            "progress_file is required to resume a projection"
        )
    keywords["progress_file"] = progress_file
    return CSVProjection(columns, field_names, **keywords)
