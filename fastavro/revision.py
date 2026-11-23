"""Approval-driven, block-preserving revision of archived Avro container files.

Archived batches may contain files written with several generations of a
writer schema while consumers preview them through a newer reader schema.
This module provides two things:

``preview``
    Reads a container file block by block and resolves every record against
    the reader schema, keeping provenance that a plain dict cannot express:

    * enum values that the reader does not know fall back to the reader
      enum's default symbol, and are flagged as such (a record that actually
      wrote the default symbol is reported differently from one that fell
      back to it);
    * union values report the branch that was actually written (index and
      name taken from the binary encoding), never guessed from the decoded
      dict, even when two branches are records with identical field values;
    * fields are distinguished as written, reader-supplied defaults, or
      pruned (present in the writer schema but dropped by the reader).

``revise``
    Applies approved changes addressed by ``(block, record)`` coordinates
    and *source* (writer schema) field paths, and writes a new container
    file that is still readable with the original writer schema:

    * blocks with no approved change are copied byte for byte (still
      compressed);
    * blocks with changes are decompressed, only the approved fields are
      re-encoded, and every other byte of the decompressed payload --
      including untouched records and fields the reader schema does not
      project -- is preserved exactly before the block is recompressed;
    * if an approved value cannot be encoded with the writer schema (for
      example a logical-typed value the source format cannot express), the
      whole block's changes are rejected and the block is copied verbatim;
    * the report maps every applied approval to its coordinates in the new
      file together with a read-back verification.
"""

import json
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ._read_common import MAGIC, SYNC_SIZE, HEADER_SCHEMA, SchemaResolutionError
from ._read_py import (
    BLOCK_READERS,
    _default_named_schemas,
    _read_default_value,
    match_schemas,
    match_types,
    maybe_promote,
    read_data,
)
from ._write_py import BLOCK_WRITERS, write_data
from .const import NAMED_TYPES
from .io.binary_decoder import BinaryDecoder
from .io.binary_encoder import BinaryEncoder
from .logical_readers import LOGICAL_READERS
from .schema import extract_logical_type, extract_record_type, parse_schema
from .types import Schema

__all__ = [
    "Approval",
    "AppliedRevision",
    "BlockOutcome",
    "PreviewResult",
    "Provenance",
    "RecordPreview",
    "RejectedApproval",
    "RevisionReport",
    "preview",
    "revise",
]

_PRIMITIVES = {
    "null",
    "boolean",
    "int",
    "long",
    "float",
    "double",
    "bytes",
    "string",
}


# ---------------------------------------------------------------------------
# container parsing
# ---------------------------------------------------------------------------


@dataclass
class _RawBlock:
    index: int
    count: int
    start: int  # offset of the record-count varint in the file
    end: int  # offset just past the trailing sync marker
    decoded: bytes  # decompressed block payload


@dataclass
class _Container:
    data: bytes
    header_end: int
    metadata: Dict[str, str]
    sync: bytes
    codec: str
    blocks: List[_RawBlock]


def _parse_container(data: bytes) -> _Container:
    if data[: len(MAGIC)] != MAGIC:
        raise ValueError("cannot read header - is it an avro file?")
    fo = BytesIO(data)
    decoder = BinaryDecoder(fo)
    header = read_data(decoder, HEADER_SCHEMA, _default_named_schemas(), None, {})
    metadata = {key: value.decode() for key, value in header["meta"].items()}
    sync = header["sync"]
    header_end = fo.tell()
    codec = metadata.get("avro.codec", "null")
    read_block = BLOCK_READERS.get(codec)
    if read_block is None:
        raise ValueError(f"Unrecognized codec: {codec}")

    blocks = []
    while True:
        start = fo.tell()
        try:
            count = decoder.read_long()
        except EOFError:
            break
        block_fo = read_block(decoder)
        if fo.read(SYNC_SIZE) != sync:
            raise ValueError("expected sync marker not found")
        blocks.append(
            _RawBlock(
                index=len(blocks),
                count=count,
                start=start,
                end=fo.tell(),
                decoded=block_fo.getvalue(),
            )
        )
    return _Container(data, header_end, metadata, sync, codec, blocks)


# ---------------------------------------------------------------------------
# span-tracking binary decoder (writer schema)
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    """A decoded value together with its byte span in the block payload."""

    schema: Schema
    kind: str
    start: int
    end: int
    value: Any = None
    fields: Optional[Dict[str, "_Span"]] = None  # record
    branch: Optional[int] = None  # union: chosen branch index
    child: Optional["_Span"] = None  # union: chosen branch value
    items: Optional[List["_Span"]] = None  # array
    entries: Optional[Dict[str, "_Span"]] = None  # map


def _read_long(buf: Union[bytes, bytearray], pos: int) -> Tuple[int, int]:
    shift = 0
    result = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return (result >> 1) ^ -(result & 1), pos


def _deref(schema: Schema, named: Dict[str, Schema]) -> Schema:
    while isinstance(schema, str) and schema not in _PRIMITIVES:
        schema = named[schema]
    return schema


def _decode_span(
    schema: Schema,
    buf: Union[bytes, bytearray],
    pos: int,
    named: Dict[str, Schema],
) -> Tuple[_Span, int]:
    if isinstance(schema, list):
        start = pos
        index, pos = _read_long(buf, pos)
        child, pos = _decode_span(schema[index], buf, pos, named)
        return (
            _Span(schema, "union", start, pos, branch=index, child=child),
            pos,
        )

    if isinstance(schema, dict):
        schema_type = schema["type"]
        if isinstance(schema_type, (dict, list)):
            return _decode_span(schema_type, buf, pos, named)
        if schema_type not in _PRIMITIVES and schema_type not in (
            "record",
            "enum",
            "array",
            "map",
            "fixed",
        ):
            # named type reference kept as a string (recursive schemas)
            return _decode_span(named[schema_type], buf, pos, named)
        if schema_type == "record":
            start = pos
            fields = {}
            for field_schema in schema["fields"]:
                field_span, pos = _decode_span(
                    field_schema["type"], buf, pos, named
                )
                fields[field_schema["name"]] = field_span
            return _Span(schema, "record", start, pos, fields=fields), pos
        if schema_type == "enum":
            start = pos
            index, pos = _read_long(buf, pos)
            return (
                _Span(schema, "enum", start, pos, value=schema["symbols"][index]),
                pos,
            )
        if schema_type == "array":
            start = pos
            items = []
            while True:
                count, pos = _read_long(buf, pos)
                if count == 0:
                    break
                if count < 0:
                    count = -count
                    _, pos = _read_long(buf, pos)  # block byte size
                for _ in range(count):
                    item, pos = _decode_span(schema["items"], buf, pos, named)
                    items.append(item)
            return _Span(schema, "array", start, pos, items=items), pos
        if schema_type == "map":
            start = pos
            entries = {}
            while True:
                count, pos = _read_long(buf, pos)
                if count == 0:
                    break
                if count < 0:
                    count = -count
                    _, pos = _read_long(buf, pos)  # block byte size
                for _ in range(count):
                    key, pos = _decode_span("string", buf, pos, named)
                    value, pos = _decode_span(schema["values"], buf, pos, named)
                    entries[key.value] = value
            return _Span(schema, "map", start, pos, entries=entries), pos
        if schema_type == "fixed":
            end = pos + schema["size"]
            return (
                _Span(schema, "fixed", pos, end, value=bytes(buf[pos:end])),
                end,
            )
        schema = schema_type  # primitive with optional logicalType wrapper

    start = pos
    if schema == "null":
        return _Span(schema, "null", pos, pos), pos
    if schema == "boolean":
        return _Span(schema, "boolean", pos, pos + 1, value=bool(buf[pos])), pos + 1
    if schema in ("int", "long"):
        value, pos = _read_long(buf, pos)
        return _Span(schema, schema, start, pos, value=value), pos
    if schema == "float":
        return (
            _Span(
                schema,
                "float",
                pos,
                pos + 4,
                value=struct.unpack("<f", buf[pos : pos + 4])[0],
            ),
            pos + 4,
        )
    if schema == "double":
        return (
            _Span(
                schema,
                "double",
                pos,
                pos + 8,
                value=struct.unpack("<d", buf[pos : pos + 8])[0],
            ),
            pos + 8,
        )
    if schema in ("bytes", "string"):
        length, pos = _read_long(buf, pos)
        raw = bytes(buf[pos : pos + length])
        pos += length
        value = raw.decode("utf-8") if schema == "string" else raw
        return _Span(schema, schema, start, pos, value=value), pos
    raise ValueError(f"cannot decode schema: {schema!r}")


def _span_value(span: _Span) -> Any:
    if span.kind == "record":
        return {name: _span_value(child) for name, child in span.fields.items()}
    if span.kind == "union":
        return _span_value(span.child)
    if span.kind == "array":
        return [_span_value(item) for item in span.items]
    if span.kind == "map":
        return {key: _span_value(value) for key, value in span.entries.items()}
    return span.value


def _collect_named(schema: Schema, out=None, _seen=None) -> Dict[str, Schema]:
    if out is None:
        out, _seen = {}, set()
    if isinstance(schema, dict):
        if id(schema) in _seen:
            return out
        _seen.add(id(schema))
        schema_type = schema.get("type")
        if "name" in schema and schema_type in NAMED_TYPES:
            out[schema["name"]] = schema
        _collect_named(schema_type, out, _seen)
        for field_schema in schema.get("fields", []):
            _collect_named(field_schema["type"], out, _seen)
        if "items" in schema:
            _collect_named(schema["items"], out, _seen)
        if "values" in schema:
            _collect_named(schema["values"], out, _seen)
    elif isinstance(schema, list):
        for branch in schema:
            _collect_named(branch, out, _seen)
    return out


# ---------------------------------------------------------------------------
# writer -> reader resolution with provenance
# ---------------------------------------------------------------------------


@dataclass
class Provenance:
    """Where a previewed value actually came from."""

    source: str = "written"  # "written" | "reader_default"
    kind: str = ""
    enum_fallback: bool = False
    written_symbol: Optional[str] = None
    branch_index: Optional[int] = None  # union branch actually written
    branch_name: Optional[str] = None
    reader_branch_index: Optional[int] = None
    reader_branch_name: Optional[str] = None
    fields: Dict[str, "Provenance"] = field(default_factory=dict)
    element: Optional["Provenance"] = None  # union branch value provenance
    pruned: Dict[str, Any] = field(default_factory=dict)


def _schema_name(schema: Schema, named: Dict[str, Schema]) -> Optional[str]:
    schema = _deref(schema, named)
    if isinstance(schema, dict) and schema.get("type") in NAMED_TYPES:
        return schema["name"]
    return None


def _unwrap(schema: Schema, named: Dict[str, Schema]) -> Schema:
    schema = _deref(schema, named)
    while isinstance(schema, dict) and isinstance(schema.get("type"), (dict, list)):
        schema = schema["type"]
    return schema


def _resolve(
    w_schema: Schema,
    r_schema: Schema,
    span: _Span,
    prov: Provenance,
    named: Dict[str, Schema],
    r_named: Dict[str, Schema],
) -> Any:
    w_schema = _unwrap(w_schema, named)
    r_schema = _unwrap(r_schema, r_named)

    if isinstance(r_schema, list):
        prov.kind = "union"
        if span.kind == "union":
            w_branch = w_schema[span.branch]
            prov.branch_index = span.branch
            prov.branch_name = _schema_name(w_branch, named)
            child_span = span.child
        else:
            w_branch = w_schema
            child_span = span
        for index, r_branch in enumerate(r_schema):
            if (
                match_schemas(
                    _unwrap(w_branch, named),
                    _unwrap(r_branch, r_named),
                    _default_named_schemas(),
                    raise_on_error=False,
                )
                is not None
            ):
                prov.reader_branch_index = index
                prov.reader_branch_name = _schema_name(r_branch, r_named)
                prov.element = Provenance(source="written")
                return _resolve(
                    w_branch, r_branch, child_span, prov.element, named, r_named
                )
        raise SchemaResolutionError(
            f"Schema mismatch: {w_branch!r} is not in {r_schema!r}"
        )

    if isinstance(w_schema, list):
        prov.kind = "union"
        w_branch = w_schema[span.branch]
        prov.branch_index = span.branch
        prov.branch_name = _schema_name(w_branch, named)
        prov.element = Provenance(source="written")
        return _resolve(w_branch, r_schema, span.child, prov.element, named, r_named)

    w_type = extract_record_type(w_schema)
    r_type = extract_record_type(r_schema)
    prov.kind = w_type

    if w_type == "record" and r_type in ("record", "error"):
        value = {}
        writer_fields = {f["name"]: f for f in w_schema["fields"]}
        matched = set()
        for r_field in r_schema["fields"]:
            name = r_field["name"]
            w_field = writer_fields.get(name)
            if w_field is None:
                for alias in r_field.get("aliases", []):
                    if alias in writer_fields:
                        w_field = writer_fields[alias]
                        break
            if w_field is not None:
                matched.add(w_field["name"])
                child_prov = Provenance(source="written")
                prov.fields[name] = child_prov
                value[name] = _resolve(
                    w_field["type"],
                    r_field["type"],
                    span.fields[w_field["name"]],
                    child_prov,
                    named,
                    r_named,
                )
            elif "default" in r_field:
                value[name] = _read_default_value(
                    r_field["type"], r_field["default"], _default_named_schemas()
                )
                prov.fields[name] = Provenance(
                    source="reader_default",
                    kind=extract_record_type(_unwrap(r_field["type"], r_named)),
                )
            else:
                raise SchemaResolutionError(
                    f"No default value for field {name} in {r_schema['name']}"
                )
        prov.pruned = {
            f["name"]: _span_value(span.fields[f["name"]])
            for f in w_schema["fields"]
            if f["name"] not in matched
        }
        return value

    if w_type == "enum" and r_type == "enum":
        symbol = span.value
        prov.written_symbol = symbol
        if symbol in r_schema["symbols"]:
            return symbol
        if "default" not in r_schema:
            raise SchemaResolutionError(
                f"Schema mismatch: enum symbol {symbol} not in {r_schema['symbols']}"
            )
        prov.enum_fallback = True
        return r_schema["default"]

    if w_type == "array" and r_type == "array":
        return [
            _resolve(
                w_schema["items"], r_schema["items"], item, Provenance(), named, r_named
            )
            for item in span.items
        ]

    if w_type == "map" and r_type == "map":
        return {
            key: _resolve(
                w_schema["values"],
                r_schema["values"],
                child,
                Provenance(),
                named,
                r_named,
            )
            for key, child in span.entries.items()
        }

    if not match_types(w_type, r_type, _default_named_schemas()):
        raise SchemaResolutionError(f"Schema mismatch: {w_type} is not {r_type}")

    value = maybe_promote(span.value, w_type, r_type)

    if isinstance(w_schema, dict):
        logical_reader = LOGICAL_READERS.get(extract_logical_type(w_schema) or "")
        if logical_reader:
            value = logical_reader(value, w_schema, r_schema)
    return value


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


@dataclass
class RecordPreview:
    """One record seen through the reader schema, with provenance."""

    block: int
    record: int
    data: Dict[str, Any]
    fields: Dict[str, Provenance]
    pruned: Dict[str, Any]


class PreviewResult:
    """Reader-schema preview of every record in a container file."""

    def __init__(
        self,
        records: List[RecordPreview],
        writer_schema: Schema,
        reader_schema: Schema,
    ):
        self.records = records
        self.writer_schema = writer_schema
        self.reader_schema = reader_schema
        self._by_coordinate = {(r.block, r.record): r for r in records}

    def __iter__(self) -> Iterator[RecordPreview]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def at(self, block: int, record: int) -> RecordPreview:
        return self._by_coordinate[(block, record)]


def _read_source(source: Union[str, bytes, bytearray, Any]) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, str):
        with open(source, "rb") as fo:
            return fo.read()
    return source.read()


def _load_writer_schema(container: _Container) -> Schema:
    return parse_schema(json.loads(container.metadata["avro.schema"]))


def _preview_container(container: _Container, reader_schema: Schema) -> PreviewResult:
    writer_schema = _load_writer_schema(container)
    r_schema = parse_schema(reader_schema)
    named = _collect_named(writer_schema)
    r_named = _collect_named(r_schema)

    records = []
    for block in container.blocks:
        pos = 0
        for record_index in range(block.count):
            span, pos = _decode_span(writer_schema, block.decoded, pos, named)
            prov = Provenance(source="written")
            data = _resolve(writer_schema, r_schema, span, prov, named, r_named)
            records.append(
                RecordPreview(
                    block=block.index,
                    record=record_index,
                    data=data,
                    fields=prov.fields,
                    pruned=prov.pruned,
                )
            )
    return PreviewResult(records, writer_schema, r_schema)


def preview(
    source: Union[str, bytes, bytearray, Any], reader_schema: Schema
) -> PreviewResult:
    """Preview a container file through ``reader_schema`` with provenance."""
    return _preview_container(_parse_container(_read_source(source)), reader_schema)


# ---------------------------------------------------------------------------
# revision
# ---------------------------------------------------------------------------


@dataclass
class Approval:
    """One approved change: set ``path`` of record ``record`` in block
    ``block`` to ``value``. ``path`` addresses *source* (writer schema)
    fields; unions are navigated through the branch actually written in
    each record."""

    block: int
    record: int
    path: Tuple[str, ...]
    value: Any

    def __post_init__(self):
        if isinstance(self.path, str):
            self.path = tuple(segment for segment in self.path.split("/") if segment)
        else:
            self.path = tuple(self.path)


@dataclass
class AppliedRevision:
    approval: Approval
    new_block: int
    new_record: int
    note: str  # "projected" | "pruned_source"
    readback_value: Any  # value read back from the new file (writer view)
    verified: bool


@dataclass
class RejectedApproval:
    approval: Approval
    reason: str
    detail: str


@dataclass
class BlockOutcome:
    index: int
    action: str  # "copied" | "rewritten" | "rejected"
    detail: str = ""


@dataclass
class RevisionReport:
    applied: List[AppliedRevision]
    rejected: List[RejectedApproval]
    blocks: List[BlockOutcome]
    readback: PreviewResult  # reader-schema preview of the new file

    def applied_at(self, block: int, record: int) -> List[AppliedRevision]:
        return [
            entry
            for entry in self.applied
            if entry.new_block == block and entry.new_record == record
        ]


def _record_branches(schema: Schema, named: Dict[str, Schema]) -> List[Schema]:
    schema = _unwrap(schema, named)
    if isinstance(schema, list):
        return [
            branch
            for branch in (_unwrap(b, named) for b in schema)
            if isinstance(branch, dict) and branch.get("type") == "record"
        ]
    if isinstance(schema, dict) and schema.get("type") == "record":
        return [schema]
    return []


def _locate_path(
    writer_schema: Schema,
    reader_schema: Schema,
    path: Tuple[str, ...],
    named: Dict[str, Schema],
    r_named: Dict[str, Schema],
) -> Tuple[str, str]:
    """Classify an approval path against both schemas.

    Returns one of "projected", "pruned_source", "reader_supplied",
    "unknown_field".
    """
    w_top = _unwrap(writer_schema, named)
    r_top = _unwrap(reader_schema, r_named)
    w_fields = {f["name"]: f for f in w_top["fields"]}
    r_fields = {f["name"]: f for f in r_top["fields"]}

    first = path[0]
    if first not in w_fields:
        if first in r_fields:
            return (
                "reader_supplied",
                f"field {first!r} only exists in the reader schema; "
                "it is not a source field and cannot be revised",
            )
        return (
            "unknown_field",
            f"field {first!r} exists in neither the writer nor the reader schema",
        )

    pruned = first not in r_fields
    w_current = w_fields[first]["type"]
    r_current = r_fields[first]["type"] if first in r_fields else None

    for segment in path[1:]:
        w_next = None
        for branch in _record_branches(w_current, named):
            for f in branch["fields"]:
                if f["name"] == segment:
                    w_next = f["type"]
                    break
            if w_next is not None:
                break
        if w_next is None:
            return (
                "unknown_field",
                f"field {segment!r} not found in the writer schema at "
                f"{'/'.join(path)!r}",
            )
        w_current = w_next
        if r_current is not None:
            r_next = None
            for branch in _record_branches(r_current, r_named):
                for f in branch["fields"]:
                    if f["name"] == segment:
                        r_next = f["type"]
                        break
                if r_next is not None:
                    break
            if r_next is None:
                pruned = True
            r_current = r_next

    if pruned:
        return (
            "pruned_source",
            f"field {path[-1]!r} is a source field the reader schema does not "
            "project; it can still be revised",
        )
    return "projected", ""


def _navigate(
    root_span: _Span,
    writer_schema: Schema,
    path: Tuple[str, ...],
    named: Dict[str, Schema],
) -> Tuple[Optional[_Span], Optional[Schema]]:
    """Navigate the actual written structure to locate a field's span."""
    span = root_span
    schema = writer_schema
    for segment in path:
        while True:
            schema = _deref(schema, named)
            if isinstance(schema, list) and span.kind == "union":
                schema = schema[span.branch]
                span = span.child
            elif isinstance(schema, dict) and isinstance(
                schema.get("type"), (list, dict)
            ):
                schema = schema["type"]
            else:
                break
        if not (isinstance(schema, dict) and schema.get("type") == "record"):
            return None, None
        field_schema = None
        for f in schema["fields"]:
            if f["name"] == segment:
                field_schema = f
                break
        if field_schema is None or segment not in span.fields:
            return None, None
        span = span.fields[segment]
        schema = field_schema["type"]
    return span, schema


def _encode_value(schema: Schema, datum: Any, named: Dict[str, Schema]) -> bytes:
    buf = BytesIO()
    encoder = BinaryEncoder(buf)
    write_data(encoder, datum, schema, named, "", {})
    encoder.flush()
    return buf.getvalue()


def _dig(data: Any, path: Tuple[str, ...]) -> Any:
    for segment in path:
        data = data[segment]
    return data


def revise(
    source: Union[str, bytes, bytearray, Any],
    target: Union[str, Any],
    approvals: List[Union[Approval, Dict[str, Any]]],
    reader_schema: Schema,
) -> RevisionReport:
    """Apply approved revisions and write a new container file.

    Only the approved source fields are re-encoded. Untouched blocks are
    copied byte for byte; touched blocks keep every unmodified byte of the
    decompressed payload. A block whose approved values cannot be encoded
    with the writer schema is rejected as a whole and copied verbatim.
    """
    approvals = [
        approval if isinstance(approval, Approval) else Approval(**approval)
        for approval in approvals
    ]
    # last approval wins for duplicate (block, record, path)
    deduped: Dict[Tuple[int, int, Tuple[str, ...]], Approval] = {}
    for approval in approvals:
        deduped[(approval.block, approval.record, approval.path)] = approval
    approvals = list(deduped.values())

    container = _parse_container(_read_source(source))
    if container.codec not in BLOCK_WRITERS:
        raise ValueError(f"Unrecognized codec: {container.codec}")
    writer_schema = _load_writer_schema(container)
    r_schema = parse_schema(reader_schema)
    named = _collect_named(writer_schema)
    r_named = _collect_named(r_schema)

    rejected: List[RejectedApproval] = []
    notes: Dict[int, str] = {}  # id(approval) -> "projected" | "pruned_source"
    by_block: Dict[int, List[Approval]] = {}
    for approval in approvals:
        if approval.block >= len(container.blocks):
            rejected.append(
                RejectedApproval(
                    approval,
                    "no_such_block",
                    f"file only has {len(container.blocks)} block(s)",
                )
            )
            continue
        status, detail = _locate_path(
            writer_schema, r_schema, approval.path, named, r_named
        )
        if status in ("reader_supplied", "unknown_field"):
            rejected.append(RejectedApproval(approval, status, detail))
            continue
        notes[id(approval)] = status
        by_block.setdefault(approval.block, []).append(approval)

    out = bytearray()
    out += container.data[: container.header_end]
    block_outcomes: List[BlockOutcome] = []
    applied: List[Approval] = []

    for block in container.blocks:
        block_approvals = by_block.get(block.index)
        if not block_approvals:
            out += container.data[block.start : block.end]
            block_outcomes.append(BlockOutcome(block.index, "copied"))
            continue

        spans = []
        pos = 0
        for _ in range(block.count):
            span, pos = _decode_span(writer_schema, block.decoded, pos, named)
            spans.append(span)

        planned = []  # (approval, span, encoded)
        block_error = None
        for approval in block_approvals:
            if approval.record >= len(spans):
                rejected.append(
                    RejectedApproval(
                        approval,
                        "no_such_record",
                        f"block {approval.block} only has {len(spans)} record(s)",
                    )
                )
                continue
            target_span, field_schema = _navigate(
                spans[approval.record], writer_schema, approval.path, named
            )
            if target_span is None:
                rejected.append(
                    RejectedApproval(
                        approval,
                        "path_not_in_actual_branch",
                        f"path {'/'.join(approval.path)!r} does not exist in the "
                        "branch actually written in record "
                        f"{approval.record}",
                    )
                )
                continue
            try:
                encoded = _encode_value(field_schema, approval.value, named)
            except Exception as ex:
                block_error = (
                    f"value for {'/'.join(approval.path)!r} cannot be encoded "
                    f"with the writer schema: {ex}"
                )
                break
            planned.append((approval, target_span, encoded))

        if block_error is not None:
            # reject the whole block's changes; copy it verbatim
            out += container.data[block.start : block.end]
            for approval in block_approvals:
                if all(existing.approval is not approval for existing in rejected):
                    rejected.append(
                        RejectedApproval(approval, "unencodable_value", block_error)
                    )
            block_outcomes.append(BlockOutcome(block.index, "rejected", block_error))
            continue

        # detect overlapping edits within a record
        replacements = []
        for approval, target_span, encoded in planned:
            for _, other_span, _ in replacements:
                if (
                    target_span.start < other_span.end
                    and other_span.start < target_span.end
                ):
                    rejected.append(
                        RejectedApproval(
                            approval,
                            "overlapping_path",
                            "edit overlaps another approved edit in the same record",
                        )
                    )
                    break
            else:
                replacements.append((approval, target_span, encoded))

        if not replacements:
            # every approval for this block was rejected individually
            out += container.data[block.start : block.end]
            block_outcomes.append(BlockOutcome(block.index, "copied"))
            continue

        revised = bytearray(block.decoded)
        for approval, target_span, encoded in sorted(
            replacements, key=lambda item: item[1].start, reverse=True
        ):
            revised[target_span.start : target_span.end] = encoded
            applied.append(approval)

        buf = BytesIO()
        encoder = BinaryEncoder(buf)
        encoder.write_long(block.count)
        BLOCK_WRITERS[container.codec](encoder, bytes(revised), None)
        buf.write(container.sync)
        out += buf.getvalue()
        block_outcomes.append(BlockOutcome(block.index, "rewritten"))

    new_data = bytes(out)
    if isinstance(target, str):
        with open(target, "wb") as fo:
            fo.write(new_data)
    else:
        target.write(new_data)

    # read the new file back: verify every applied approval and anchor it to
    # the new block coordinates
    new_container = _parse_container(new_data)
    readback = _preview_container(new_container, r_schema)

    span_cache: Dict[int, List[_Span]] = {}
    applied_entries: List[AppliedRevision] = []
    for approval in applied:
        block = new_container.blocks[approval.block]
        if approval.block not in span_cache:
            spans = []
            pos = 0
            for _ in range(block.count):
                span, pos = _decode_span(writer_schema, block.decoded, pos, named)
                spans.append(span)
            span_cache[approval.block] = spans
        span = span_cache[approval.block][approval.record]
        writer_view = _resolve(
            writer_schema, writer_schema, span, Provenance(), named, named
        )
        readback_value = _dig(writer_view, approval.path)
        applied_entries.append(
            AppliedRevision(
                approval=approval,
                new_block=approval.block,
                new_record=approval.record,
                note=notes[id(approval)],
                readback_value=readback_value,
                verified=readback_value == approval.value,
            )
        )

    return RevisionReport(
        applied=applied_entries,
        rejected=rejected,
        blocks=block_outcomes,
        readback=readback,
    )
