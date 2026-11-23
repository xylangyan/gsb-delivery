"""Surgical, byte-level editor for Thrift compact-protocol struct payloads.

Inbound messages are compact-serialized structs that may only be modified
according to an *old* Thrift definition (a :class:`~thriftpy2.thrift.TPayload`
subclass).  A correction order is a list of :class:`Edit` items addressed by
position of occurrence (``Field`` / ``Item`` path steps).  Each edit may

* ``REPLACE`` a known scalar value,
* ``CLEAR`` an optional value, or
* ``ADD`` a previously missing value.

The corrected payload stays readable by the existing thriftpy2 compact
reader.  Alongside the new bytes, :func:`apply_corrections` returns
:class:`TraceSpan` records mapping every unedited *value* byte range of the
old payload to its position in the new payload -- including fields the old
definition does not know at all -- so on-disk validation can follow the
untouched bytes across the rewrite.  Field-id (delta) or length adjustments
forced by an edit only touch the corresponding metadata (headers), never the
preserved value bytes or their order of occurrence.

Duplicate field ids follow the old reader's semantics: the reader adopts the
last occurrence whose wire type is compatible with the definition, so
``REPLACE`` rewrites exactly that occurrence, while ``CLEAR`` drops every
compatible occurrence (an earlier duplicate must not become visible again)
and keeps same-id occurrences of an incompatible type for newer readers.

Correction orders are atomic: if any selected edit cannot be applied under
the current definition (unknown field, bad path, or a value whose type does
not fit), the whole order is rejected and the original payload is returned
unchanged with ``CorrectionResult.applied == False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from struct import pack
from typing import Any, List, Optional, Tuple

from ..protocol.compact import (
    BIN_TYPES,
    CTYPES,
    TTYPES,
    CompactType,
    check_integer_limits,
    from_zig_zag,
    make_zig_zag,
)
from ..thrift import TType

__all__ = [
    "ADD",
    "CLEAR",
    "REPLACE",
    "CorrectionResult",
    "Edit",
    "EditError",
    "Field",
    "Item",
    "TraceSpan",
    "apply_corrections",
]

REPLACE = "replace"
CLEAR = "clear"
ADD = "add"

_INT_BITS = {TType.BYTE: 8, TType.I16: 16, TType.I32: 32, TType.I64: 64}
_INT_CTYPES = (CompactType.I16, CompactType.I32, CompactType.I64)


class EditError(Exception):
    """A correction order cannot be applied to the given payload."""


@dataclass(frozen=True)
class Field:
    """Path step selecting a field of the current struct by field id."""
    fid: int


@dataclass(frozen=True)
class Item:
    """Path step selecting a list element by position of occurrence."""
    index: int


@dataclass(frozen=True)
class Edit:
    """One correction: apply ``op`` (with ``value``) at ``path``."""
    path: Tuple[Any, ...]
    op: str
    value: Any = None


@dataclass(frozen=True)
class TraceSpan:
    """Byte range copied unedited: old [start, end) -> new [start, end)."""
    old_start: int
    old_end: int
    new_start: int
    new_end: int


@dataclass
class CorrectionResult:
    """Outcome of :func:`apply_corrections`.

    ``data`` is the corrected payload (or the untouched original when the
    order was rejected).  ``spans`` traces unedited value bytes from the old
    payload to ``data``; ``applied`` tells whether the edits went through
    and ``error`` carries the rejection reason when they did not.
    """
    data: bytes
    applied: bool
    spans: List[TraceSpan]
    edits: int
    error: Optional[str] = None


@dataclass
class _FieldRec:
    """One field occurrence located inside a scanned struct."""
    fid: int
    ctype: int
    hstart: int  # offset of the field header
    vstart: int  # offset of the value (== header end)
    vend: int    # offset just past the value


def _varint_bytes(n: int) -> bytes:
    out = bytearray()
    while True:
        if n & ~0x7F == 0:
            out.append(n)
            return bytes(out)
        out.append((n & 0xFF) | 0x80)
        n >>= 7


def _encode_header(ctype: int, fid: int, last_fid: int) -> bytes:
    delta = fid - last_fid
    if 0 < delta <= 15:
        return bytes([(delta << 4) | ctype])
    return bytes([ctype]) + _varint_bytes(make_zig_zag(fid, 16))


def _encode_scalar(ftype: int, value: Any) -> bytes:
    if ftype == TType.BYTE:
        return pack("!b", value)
    if ftype in _INT_BITS:
        return _varint_bytes(make_zig_zag(value, _INT_BITS[ftype]))
    if ftype == TType.DOUBLE:
        return pack("<d", value)
    if ftype == TType.STRING:
        raw = value.encode("utf-8")
        return _varint_bytes(len(raw)) + raw
    if ftype == TType.BINARY:
        raw = bytes(value)
        return _varint_bytes(len(raw)) + raw
    raise EditError(f"cannot encode value for field type {ftype}")


def _check_scalar_value(ftype: int, value: Any) -> None:
    if ftype == TType.BOOL:
        if not isinstance(value, bool):
            raise EditError(f"expected a bool, got {type(value).__name__}")
    elif ftype in _INT_BITS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise EditError(f"expected an int, got {type(value).__name__}")
        try:
            check_integer_limits(value, _INT_BITS[ftype])
        except Exception as exc:
            raise EditError(str(exc)) from exc
    elif ftype == TType.DOUBLE:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EditError(f"expected a float, got {type(value).__name__}")
    elif ftype == TType.STRING:
        if not isinstance(value, str):
            raise EditError(f"expected a str, got {type(value).__name__}")
    elif ftype == TType.BINARY:
        if not isinstance(value, (bytes, bytearray)):
            raise EditError(f"expected bytes, got {type(value).__name__}")
    else:
        raise EditError(f"field type {ftype} is not a scalar")


def _entry_for(payload_cls: type, fid: int) -> Optional[tuple]:
    return getattr(payload_cls, "thrift_spec", {}).get(fid)


def _entry_spec(entry: tuple) -> Any:
    return entry[2] if len(entry) > 3 else None


def _compatible(ctype: int, ftype: int) -> bool:
    try:
        wtype = TTYPES[ctype]
    except KeyError:
        raise EditError(f"invalid compact type {ctype}") from None
    return wtype == ftype or (wtype in BIN_TYPES and ftype in BIN_TYPES)


class _Scanner:
    """Locates field occurrences and value ranges inside a payload."""

    def __init__(self, data: bytes):
        self.data = data
        self.limit = len(data)

    def _byte(self, pos: int) -> int:
        if pos >= self.limit:
            raise EditError("truncated payload")
        return self.data[pos]

    def _varint(self, pos: int) -> Tuple[int, int]:
        result = 0
        shift = 0
        while True:
            byte = self._byte(pos)
            pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result, pos
            shift += 7
            if shift > 63:
                raise EditError("varint is too long")

    def skip_value(self, ctype: int, pos: int,
                   in_container: bool = False) -> int:
        if ctype in (CompactType.TRUE, CompactType.FALSE):
            # A bool field keeps its value in the header; only container
            # elements occupy a separate byte.
            return pos + 1 if in_container else pos
        if ctype == CompactType.BYTE:
            self._byte(pos)
            return pos + 1
        if ctype in _INT_CTYPES:
            _, pos = self._varint(pos)
            return pos
        if ctype == CompactType.DOUBLE:
            if pos + 8 > self.limit:
                raise EditError("truncated payload")
            return pos + 8
        if ctype == CompactType.BINARY:
            length, pos = self._varint(pos)
            if pos + length > self.limit:
                raise EditError("truncated payload")
            return pos + length
        if ctype in (CompactType.LIST, CompactType.SET):
            byte = self._byte(pos)
            pos += 1
            size = byte >> 4
            etype = byte & 0x0F
            if size == 15:
                size, pos = self._varint(pos)
            for _ in range(size):
                pos = self.skip_value(etype, pos, in_container=True)
            return pos
        if ctype == CompactType.MAP:
            size, pos = self._varint(pos)
            if size > 0:
                types = self._byte(pos)
                pos += 1
                for _ in range(size):
                    pos = self.skip_value(types >> 4, pos, in_container=True)
                    pos = self.skip_value(types & 0x0F, pos, in_container=True)
            return pos
        if ctype == CompactType.STRUCT:
            _, pos = self.scan_struct(pos)
            return pos
        raise EditError(f"invalid compact type {ctype}")

    def scan_struct(self, pos: int) -> Tuple[List[_FieldRec], int]:
        """Return (field occurrences, offset past the STOP byte)."""
        fields = []
        last_fid = 0
        while True:
            hstart = pos
            byte = self._byte(pos)
            pos += 1
            ctype = byte & 0x0F
            if ctype == CompactType.STOP:
                return fields, pos
            delta = byte >> 4
            if delta:
                fid = last_fid + delta
            else:
                zigzag, pos = self._varint(pos)
                fid = from_zig_zag(zigzag)
            last_fid = fid
            vstart = pos
            pos = self.skip_value(ctype, pos)
            fields.append(_FieldRec(fid, ctype, hstart, vstart, pos))

    def scan_list(self, rec: _FieldRec):
        """Return (element ctype, header end, [(start, end) per element])."""
        pos = rec.vstart
        byte = self._byte(pos)
        pos += 1
        size = byte >> 4
        etype = byte & 0x0F
        if size == 15:
            size, pos = self._varint(pos)
        header_end = pos
        elements = []
        for _ in range(size):
            start = pos
            pos = self.skip_value(etype, pos, in_container=True)
            elements.append((start, pos))
        return etype, header_end, elements


class _StructPlan:
    def __init__(self, fields: List[_FieldRec], payload_cls: type):
        self.fields = fields
        self.payload_cls = payload_cls
        self.replacements = {}  # id(rec) -> (rec, entry, value)
        self.clears = set()     # id(rec) of occurrences to drop
        self.descents = {}      # id(rec) -> _StructPlan | _ListPlan
        self.adds = []          # (fid, entry, value)


class _ListPlan:
    def __init__(self, rec: _FieldRec, header_end: int, elements: list):
        self.rec = rec
        self.header_end = header_end
        self.elements = elements
        self.edited = {}        # element index -> _StructPlan


def _build_descent(scanner: _Scanner, rec: _FieldRec, entry: tuple,
                   sub_edits: List[Edit]):
    first_step = sub_edits[0].path[0]
    if isinstance(first_step, Item):
        if entry[0] not in (TType.LIST, TType.SET):
            raise EditError("item step selects a non-list field")
        espec = _entry_spec(entry)
        if isinstance(espec, tuple):
            etype = espec[0]
            einner = espec[1] if len(espec) > 1 else None
        else:
            etype, einner = espec, None
        if etype != TType.STRUCT or einner is None:
            raise EditError("list elements are not defined structs")
        ctype, header_end, elements = scanner.scan_list(rec)
        if elements and ctype != CompactType.STRUCT:
            raise EditError("wire list elements are not structs")
        plan = _ListPlan(rec, header_end, elements)
        by_index = {}
        for edit in sub_edits:
            step = edit.path[0]
            if not isinstance(step, Item):
                raise EditError("mixed path steps under one list field")
            by_index.setdefault(step.index, []).append(
                Edit(edit.path[1:], edit.op, edit.value))
        for index, grouped in by_index.items():
            if not 0 <= index < len(elements):
                raise EditError(f"list element {index} is out of range")
            efields, _ = scanner.scan_struct(elements[index][0])
            plan.edited[index] = _build_struct_plan(
                scanner, efields, einner, grouped)
        return plan
    if entry[0] != TType.STRUCT:
        raise EditError("cannot descend into a non-struct field")
    klass = _entry_spec(entry)
    if klass is None:
        raise EditError("struct field has no definition to descend with")
    sfields, _ = scanner.scan_struct(rec.vstart)
    return _build_struct_plan(scanner, sfields, klass, sub_edits)


def _build_struct_plan(scanner: _Scanner, fields: List[_FieldRec],
                       payload_cls: type, edits: List[Edit]) -> _StructPlan:
    plan = _StructPlan(fields, payload_cls)
    compat: dict[int, list] = {}
    for rec in fields:
        entry = _entry_for(payload_cls, rec.fid)
        if entry is not None and _compatible(rec.ctype, entry[0]):
            compat.setdefault(rec.fid, []).append(rec)

    descent_groups: dict[int, list] = {}
    cleared_fids = set()
    replaced_fids = set()
    for edit in edits:
        if not edit.path or not isinstance(edit.path[0], Field):
            raise EditError("a path must start with a field step")
        fid = edit.path[0].fid
        entry = _entry_for(payload_cls, fid)
        if entry is None:
            raise EditError(f"field {fid} is not defined")
        occs = compat.get(fid, [])
        if len(edit.path) > 1:
            descent_groups.setdefault(fid, []).append(edit)
            continue
        if edit.op == REPLACE:
            if fid in cleared_fids or fid in replaced_fids:
                raise EditError(f"conflicting edits for field {fid}")
            _check_scalar_value(entry[0], edit.value)
            if not occs:
                raise EditError(f"field {fid} has no value to replace")
            target = occs[-1]  # the occurrence the old reader adopts
            plan.replacements[id(target)] = (target, entry, edit.value)
            replaced_fids.add(fid)
        elif edit.op == CLEAR:
            if fid in replaced_fids:
                raise EditError(f"conflicting edits for field {fid}")
            # Drop every compatible occurrence so an earlier duplicate
            # cannot resurface; incompatible same-id occurrences stay.
            for rec in occs:
                plan.clears.add(id(rec))
            cleared_fids.add(fid)
        elif edit.op == ADD:
            _check_scalar_value(entry[0], edit.value)
            plan.adds.append((fid, entry, edit.value))
        else:
            raise EditError(f"unknown operation {edit.op!r}")

    for fid, grouped in descent_groups.items():
        if fid in cleared_fids:
            raise EditError(f"conflicting edits for field {fid}")
        occs = compat.get(fid, [])
        if not occs:
            raise EditError(f"field {fid} is missing")
        target = occs[-1]
        entry = _entry_for(payload_cls, fid)
        sub_edits = [Edit(e.path[1:], e.op, e.value) for e in grouped]
        plan.descents[id(target)] = _build_descent(
            scanner, target, entry, sub_edits)
    return plan


def _emit_struct(scanner: _Scanner, plan: _StructPlan,
                 out: bytearray, spans: List[TraceSpan]) -> None:
    data = scanner.data
    last_fid = 0
    pending_header = False  # a removal invalidated the next delta header
    for rec in plan.fields:
        if id(rec) in plan.clears:
            pending_header = True
            continue
        replacement = plan.replacements.get(id(rec))
        ctype = rec.ctype
        if replacement is not None and replacement[1][0] == TType.BOOL:
            ctype = CompactType.TRUE if replacement[2] else CompactType.FALSE
        if pending_header:
            # Re-encode the same field id (and bool value nibble) against
            # the new predecessor; the value bytes stay untouched.
            out += _encode_header(ctype, rec.fid, last_fid)
        elif ctype != rec.ctype:
            # Bool value flip: keep the original delta bits, swap nibble.
            out += bytes([(data[rec.hstart] & 0xF0) | ctype])
            out += data[rec.hstart + 1:rec.vstart]
        else:
            out += data[rec.hstart:rec.vstart]
        if replacement is not None:
            if replacement[1][0] != TType.BOOL:
                out += _encode_scalar(replacement[1][0], replacement[2])
        elif id(rec) in plan.descents:
            _emit_descent(scanner, plan.descents[id(rec)], out, spans)
        else:
            length = rec.vend - rec.vstart
            if length:
                new_start = len(out)
                out += data[rec.vstart:rec.vend]
                spans.append(TraceSpan(rec.vstart, rec.vend,
                                       new_start, new_start + length))
        last_fid = rec.fid
        pending_header = False
    for fid, entry, value in plan.adds:
        if entry[0] == TType.BOOL:
            ctype = CompactType.TRUE if value else CompactType.FALSE
            out += _encode_header(ctype, fid, last_fid)
        else:
            out += _encode_header(CTYPES[entry[0]], fid, last_fid)
            out += _encode_scalar(entry[0], value)
        last_fid = fid
    out.append(CompactType.STOP)


def _emit_descent(scanner: _Scanner, subplan,
                  out: bytearray, spans: List[TraceSpan]) -> None:
    data = scanner.data
    if isinstance(subplan, _StructPlan):
        _emit_struct(scanner, subplan, out, spans)
        return
    out += data[subplan.rec.vstart:subplan.header_end]
    for index, (start, end) in enumerate(subplan.elements):
        element_plan = subplan.edited.get(index)
        if element_plan is not None:
            _emit_struct(scanner, element_plan, out, spans)
        else:
            length = end - start
            if length:
                new_start = len(out)
                out += data[start:end]
                spans.append(TraceSpan(start, end,
                                       new_start, new_start + length))


def apply_corrections(data: bytes, payload_cls: type,
                      edits: List[Edit]) -> CorrectionResult:
    """Apply a correction order to a compact-struct payload.

    ``payload_cls`` is the old Thrift definition (a TPayload subclass) the
    message must remain readable with.  Returns a :class:`CorrectionResult`;
    when any edit cannot be applied under that definition the whole order is
    rejected and the original bytes are returned unchanged.
    """
    data = bytes(data)
    edits = list(edits)
    try:
        scanner = _Scanner(data)
        fields, end = scanner.scan_struct(0)
        if end != len(data):
            raise EditError("trailing bytes after the struct payload")
        plan = _build_struct_plan(scanner, fields, payload_cls, edits)
        out = bytearray()
        spans: List[TraceSpan] = []
        _emit_struct(scanner, plan, out, spans)
    except EditError as exc:
        return CorrectionResult(
            data=data,
            applied=False,
            spans=[TraceSpan(0, len(data), 0, len(data))],
            edits=0,
            error=str(exc),
        )
    spans.sort(key=lambda span: (span.old_start, span.old_end))
    return CorrectionResult(
        data=bytes(out),
        applied=True,
        spans=spans,
        edits=len(edits),
    )
