# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at:
#
#    http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

"""Split a binary Ion stream into independently readable volumes.

The splitter treats every top-level value as an opaque TLV: the original body
bytes, including field-name, annotation, and symbol-value SIDs, are copied
verbatim.  Each volume receives only the version marker and local symbol table
needed to give those SIDs the same text *and* the same import identity that the
original stream gave them.

The public entry points are:

``plan_volumes``
    Build (but do not raise for) a plan.  The plan reports values/groups that
    cannot be moved and the cheapest opening overhead found.

``split_volumes``
    Return the planned volumes, raising :class:`IonVolumeError` when the
    requested contract cannot be met.

``audit_volumes``
    Re-read the source and the produced volumes, compare every value's raw TLV
    bytes and managed field/annotation/value events, and report opening costs
    and group placement.

Contract summary
----------------
* Values retain their original top-level order.
* A volume starts with an IVM (``E0 01 00 EA``) and, if required, one local
  symbol table.  No other preamble is needed.
* A value with an unresolved symbol that has no import location (for example an
  out-of-range local SID) is reported as non-movable.  A guessed text is never
  substituted for an unproven symbol identity.
* Symbols imported from shared tables keep their ``ImportLocation`` by being
  imported under the same name and position in the output volume.  If the text
  is unknown, it remains unknown at the same position.  The declared shared
  table *version* is also preserved: a same-named table at another version is
  a different identity.
* ``max_opening_bytes`` limits ``IVM + LST`` bytes per volume.
* ``groups`` names values (zero-based indices into the top-level value stream)
  that must share a volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .core import IonEventType, IonType
from .exceptions import IonException
from .reader import NEXT_EVENT, blocking_reader
from .reader_binary import binary_reader
from .reader_managed import managed_reader
from .symbols import (
    ImportLocation,
    LOCAL_TABLE_TYPE,
    SHARED_TABLE_TYPE,
    SID_IMPORTS,
    SID_ION_SYMBOL_TABLE,
    SID_MAX_ID,
    SID_NAME,
    SID_SYMBOLS,
    SID_VERSION,
    SymbolTable,
    SymbolTableCatalog,
    SYSTEM_SYMBOL_TABLE,
    TEXT_ION_1_0,
    TEXT_ION_SYMBOL_TABLE,
    CannotSubstituteTable,
)

IVM = b'\xe0\x01\x00\xea'

_TID_NULL = 0
_TID_BOOL = 1
_TID_SYMBOL = 7
_TID_STRING = 8
_TID_LIST = 11
_TID_SEXP = 12
_TID_STRUCT = 13
_TID_ANNOTATION = 14

_SYSTEM_MAX_SID = 9
_SYSTEM_TEXTS = {
    1: '$ion',
    2: '$ion_1_0',
    3: '$ion_symbol_table',
    4: 'name',
    5: 'version',
    6: 'imports',
    7: 'symbols',
    8: 'max_id',
    9: '$ion_shared_symbol_table',
}
_SYSTEM_LOCATIONS = {sid: ImportLocation('$ion', sid) for sid in range(1, _SYSTEM_MAX_SID + 1)}


class IonVolumeError(IonException):
    """Raised when the requested volume contract cannot be satisfied."""


@dataclass(frozen=True)
class SymbolNeed:
    """One symbol reference made by a top-level value.

    Args:
        sid: The source-local symbol ID.
        text: The resolved text, or ``None`` when the text is unknown.
        location: The import position, or ``None`` for a local/system symbol.
        version: The declared shared-table version when ``location`` is not
            ``None``.  Version is kept separately because ``ImportLocation``
            identifies a slot by name and position only.
    """

    sid: int
    text: Optional[str]
    location: Optional[ImportLocation]
    version: Optional[int] = None

    @property
    def is_system(self) -> bool:
        return 1 <= self.sid <= _SYSTEM_MAX_SID

    @property
    def is_symbol_zero(self) -> bool:
        return self.sid == 0


@dataclass
class ValueRecord:
    """A movable top-level value and the symbol context it used."""

    raw: bytes
    start: int
    end: int
    table: SymbolTable
    imports: Tuple[Tuple[str, int, int], ...]
    needs: Tuple[SymbolNeed, ...]
    index: int = -1
    movable: bool = True

    @property
    def size(self) -> int:
        return len(self.raw)


class Volume(bytes):
    """One independently readable output volume.

    This is a :class:`bytes` subclass so a volume can be handed directly to a
    file or reader.  The extra attributes describe the layout:

    Args:
        data (bytes): The complete volume (preamble followed by raw values).
        value_indices (Sequence[int]): Indices of the source values in this
            volume, in source order.
        preamble (bytes): The IVM plus optional local symbol table.
    """

    def __new__(cls, data: bytes, value_indices: Sequence[int] = (),
                preamble: bytes = b''):
        instance = super().__new__(cls, data)
        instance.value_indices = tuple(value_indices)
        instance.preamble = bytes(preamble)
        return instance

    @property
    def data(self) -> bytes:
        return bytes(self)

    @property
    def size(self) -> int:
        return len(self)

    @property
    def opening_bytes(self) -> int:
        return len(self.preamble)

    @property
    def values_size(self) -> int:
        return len(self) - len(self.preamble)

    @property
    def body(self) -> bytes:
        return bytes(self)[len(self.preamble):]


@dataclass(frozen=True)
class PlanProblem:
    """A reason why a value, group, or whole stream cannot be split."""

    kind: str
    message: str
    value_indices: Tuple[int, ...] = ()
    group_index: Optional[int] = None

    def __str__(self) -> str:
        where = ''
        if self.value_indices:
            where = ' values=%r' % (self.value_indices,)
        if self.group_index is not None:
            where += ' group=%d' % self.group_index
        return '%s: %s%s' % (self.kind, self.message, where)


@dataclass
class VolumePlan:
    """The result of :func:`plan_volumes`."""

    volumes: List[Volume] = field(default_factory=list)
    values: List[ValueRecord] = field(default_factory=list)
    groups: Tuple[Tuple[int, ...], ...] = ()
    total_opening_bytes: int = 0
    problems: List[PlanProblem] = field(default_factory=list)
    #: Indices into ``groups`` that fail independently of other groups.
    group_failures: Tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_problems(self) -> None:
        if self.problems:
            raise IonVolumeError('; '.join(str(p) for p in self.problems))


# ---------------------------------------------------------------------------
# Binary TLV walking
# ---------------------------------------------------------------------------

def _read_varuint(data: bytes, pos: int) -> Tuple[int, int]:
    value = 0
    while True:
        if pos >= len(data):
            raise IonVolumeError('truncated VarUInt')
        octet = data[pos]
        pos += 1
        value = (value << 7) | (octet & 0x7f)
        if octet & 0x80:
            return value, pos


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    if pos >= len(data):
        raise IonVolumeError('truncated VarInt')
    first = data[pos]
    pos += 1
    negative = bool(first & 0x40)
    value = first & 0x3f
    if not (first & 0x80):
        while True:
            if pos >= len(data):
                raise IonVolumeError('truncated VarInt')
            octet = data[pos]
            pos += 1
            value = (value << 7) | (octet & 0x7f)
            if octet & 0x80:
                break
    return (-value if negative else value), pos


def _tlv_end(data: bytes, pos: int) -> int:
    """Returns the first byte after the TLV that starts at ``pos``."""
    if pos >= len(data):
        raise IonVolumeError('truncated TLV')
    type_octet = data[pos]
    tid = type_octet >> 4
    ln = type_octet & 0x0f
    cursor = pos + 1

    if tid == _TID_NULL:
        if ln == 0x0f:
            return cursor
        if ln == 0x0e:
            length, cursor = _read_varuint(data, cursor)
        else:
            length = ln
        end = cursor + length
        if end > len(data):
            raise IonVolumeError('truncated NOP pad')
        return end

    if tid == _TID_BOOL:
        return cursor

    if tid in (_TID_LIST, _TID_SEXP, _TID_STRUCT):
        if ln == 0x0f:
            return cursor
        if ln == 0x0e:
            length, cursor = _read_varuint(data, cursor)
        elif tid == _TID_STRUCT and ln == 1:
            length, cursor = _read_varuint(data, cursor)
        else:
            length = ln
        end = cursor + length
        if end > len(data):
            raise IonVolumeError('truncated container')
        return end

    if tid == _TID_ANNOTATION:
        if ln == 0x0f or ln < 3:
            raise IonVolumeError('invalid annotation wrapper')
        if ln == 0x0e:
            length, cursor = _read_varuint(data, cursor)
        else:
            length = ln
        end = cursor + length
        if end > len(data):
            raise IonVolumeError('truncated annotation wrapper')
        return end

    if ln == 0x0f:
        return cursor
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
    else:
        length = ln
    end = cursor + length
    if end > len(data):
        raise IonVolumeError('truncated scalar')
    return end


def _annotation_sids(data: bytes, start: int) -> Tuple[Tuple[int, ...], int]:
    """Returns the annotation SIDs and the offset of the wrapped value."""
    type_octet = data[start]
    if type_octet >> 4 != _TID_ANNOTATION:
        return (), start
    ln = type_octet & 0x0f
    cursor = start + 1
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
        wrapper_end = cursor + length
    else:
        wrapper_end = cursor + ln
    if wrapper_end > len(data):
        raise IonVolumeError('truncated annotation wrapper')
    anno_len, cursor = _read_varuint(data, cursor)
    anno_end = cursor + anno_len
    if anno_end > wrapper_end:
        raise IonVolumeError('annotation subfield exceeds the wrapper')
    sids = []
    while cursor < anno_end:
        sid, cursor = _read_varuint(data, cursor)
        sids.append(sid)
    if cursor != anno_end:
        raise IonVolumeError('annotation subfield ends between SIDs')
    return tuple(sids), anno_end


def _is_nop_pad(data: bytes, start: int) -> bool:
    """True for a type-0 TLV that is padding, not null.null (0x0F)."""
    return start < len(data) and (data[start] >> 4) == _TID_NULL and \
        (data[start] & 0x0f) != 0x0f


def _symbol_scalar_sid(data: bytes, start: int) -> Optional[int]:
    type_octet = data[start]
    tid = type_octet >> 4
    ln = type_octet & 0x0f
    if tid != _TID_SYMBOL or ln == 0x0f:
        return None
    if ln == 0x00:
        return 0
    cursor = start + 1
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
    else:
        length = ln
    return int.from_bytes(data[cursor:cursor + length], 'big')


def _walk_sids(data: bytes, start: int, end: int) -> Iterable[int]:
    """Yields every SID used by the TLV in ``data[start:end]``.

    Field names, annotations, and symbol scalars are all included.  Symbol zero
    is omitted because every volume has it implicitly.
    """
    if start >= end:
        return
    type_octet = data[start]
    tid = type_octet >> 4
    ln = type_octet & 0x0f

    if tid == _TID_NULL:
        return

    if tid == _TID_ANNOTATION:
        sids, value_start = _annotation_sids(data, start)
        for sid in sids:
            if sid:
                yield sid
        yield from _walk_sids(data, value_start, end)
        return

    if tid == _TID_STRUCT:
        if ln == 0x0f:
            return
        cursor = start + 1
        if ln == 0x0e:
            length, cursor = _read_varuint(data, cursor)
        elif ln == 1:
            length, cursor = _read_varuint(data, cursor)
        else:
            length = ln
        body_end = cursor + length
        while cursor < body_end:
            if _is_nop_pad(data, cursor):
                cursor = _tlv_end(data, cursor)
                continue
            field_sid, cursor = _read_varuint(data, cursor)
            if field_sid:
                yield field_sid
            child_end = _tlv_end(data, cursor)
            yield from _walk_sids(data, cursor, child_end)
            cursor = child_end
        return

    if tid in (_TID_LIST, _TID_SEXP):
        if ln == 0x0f:
            return
        cursor = start + 1
        if ln == 0x0e:
            length, cursor = _read_varuint(data, cursor)
        else:
            length = ln
        body_end = cursor + length
        while cursor < body_end:
            child_end = _tlv_end(data, cursor)
            yield from _walk_sids(data, cursor, child_end)
            cursor = child_end
        return

    if tid == _TID_SYMBOL:
        sid = _symbol_scalar_sid(data, start)
        if sid:
            yield sid
        return


def _top_level_tlvs(data: bytes) -> Iterable[Tuple[int, int]]:
    cursor = 0
    while cursor < len(data):
        if data[cursor:cursor + 4] == IVM:
            yield cursor, cursor + 4
            cursor += 4
            continue
        end = _tlv_end(data, cursor)
        yield cursor, end
        cursor = end


# ---------------------------------------------------------------------------
# LST parsing
# ---------------------------------------------------------------------------

@dataclass
class _LstInfo:
    symbols: List[Optional[str]]
    imports: Optional[List[Tuple[str, int, Optional[int]]]] = None
    append: bool = False


def _decode_string(data: bytes, start: int) -> Optional[str]:
    tid = data[start] >> 4
    ln = data[start] & 0x0f
    if ln == 0x0f:
        return None
    if tid != _TID_STRING:
        return None
    cursor = start + 1
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
    else:
        length = ln
    return data[cursor:cursor + length].decode('utf-8')


def _decode_int(data: bytes, start: int) -> Optional[int]:
    tid = data[start] >> 4
    ln = data[start] & 0x0f
    if tid not in (2, 3) or ln == 0x0f:
        return None
    cursor = start + 1
    if ln == 0x0e:
        ln, cursor = _read_varuint(data, cursor)
    value = int.from_bytes(data[cursor:cursor + ln], 'big')
    if tid == 3:
        value = -value
    return value


def _parse_symbols_list(data: bytes, start: int) -> List[Optional[str]]:
    ln = data[start] & 0x0f
    cursor = start + 1
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
    else:
        length = ln
    end = cursor + length
    out: List[Optional[str]] = []
    while cursor < end:
        child_end = _tlv_end(data, cursor)
        if data[cursor] >> 4 == _TID_STRING:
            out.append(_decode_string(data, cursor))
        # Non-string elements are ignored, matching the managed reader.
        cursor = child_end
    return out


def _parse_imports_list(data: bytes, start: int, table: SymbolTable) -> List[Tuple[str, int, Optional[int]]]:
    ln = data[start] & 0x0f
    cursor = start + 1
    if ln == 0x0e:
        length, cursor = _read_varuint(data, cursor)
    else:
        length = ln
    end = cursor + length
    out: List[Tuple[str, int, Optional[int]]] = []
    while cursor < end:
        desc_end = _tlv_end(data, cursor)
        if data[cursor] >> 4 != _TID_STRUCT:
            cursor = desc_end
            continue
        name: Optional[str] = None
        version = 1
        max_id: Optional[int] = None
        body = cursor + 1
        desc_ln = data[cursor] & 0x0f
        if desc_ln == 0x0e:
            _desc_len, body = _read_varuint(data, body)
        elif desc_ln == 1:
            _desc_len, body = _read_varuint(data, body)
        while body < desc_end:
            if _is_nop_pad(data, body):
                body = _tlv_end(data, body)
                continue
            field_sid, body = _read_varuint(data, body)
            next_body = _tlv_end(data, body)
            field_text = _resolve_text(table, field_sid)
            if field_text == 'name':
                name = _decode_string(data, body)
            elif field_text == 'version':
                value = _decode_int(data, body)
                if value is not None:
                    version = value
            elif field_text == 'max_id':
                value = _decode_int(data, body)
                if value is not None:
                    max_id = value
            body = next_body
        if name is not None:
            out.append((name, version, max_id))
        cursor = desc_end
    return out


def _unwrap_annotations(data: bytes, start: int, end: int) -> int:
    """Returns the offset of the value inside any annotation wrappers."""
    while start < end and data[start] >> 4 == _TID_ANNOTATION:
        _sids, start = _annotation_sids(data, start)
    return start


def _is_container(data: bytes, start: int, tid: int) -> bool:
    return start < len(data) and data[start] >> 4 == tid and (data[start] & 0x0f) != 0x0f


def _parse_lst(data: bytes, start: int, end: int, table: SymbolTable) -> _LstInfo:
    sids, value_start = _annotation_sids(data, start)
    if not sids or not _is_container(data, value_start, _TID_STRUCT):
        raise IonVolumeError('local symbol table annotation does not wrap a struct')
    cursor = value_start + 1
    ln = data[value_start] & 0x0f
    if ln == 0x0e:
        _length, cursor = _read_varuint(data, cursor)
    elif ln == 1:
        _length, cursor = _read_varuint(data, cursor)
    symbols: List[Optional[str]] = []
    imports: Optional[List[Tuple[str, int, Optional[int]]]] = None
    append = False
    while cursor < end:
        if _is_nop_pad(data, cursor):
            cursor = _tlv_end(data, cursor)
            continue
        field_sid, cursor = _read_varuint(data, cursor)
        next_cursor = _tlv_end(data, cursor)
        field_text = _resolve_text(table, field_sid)
        child = _unwrap_annotations(data, cursor, next_cursor)
        if field_text == 'symbols' and _is_container(data, child, _TID_LIST):
            symbols.extend(_parse_symbols_list(data, child))
        elif field_text == 'imports':
            if _is_container(data, child, _TID_LIST):
                imports = _parse_imports_list(data, child, table)
                append = False
            elif child < next_cursor and data[child] >> 4 == _TID_SYMBOL:
                value_sid = _symbol_scalar_sid(data, child)
                # The append marker is identified by the symbol's *text*.
                # Using SID 3 directly would miss a local symbol that shadows
                # it or an LST that renamed the slot.
                append = value_sid is not None and \
                    _resolve_text(table, value_sid) == TEXT_ION_SYMBOL_TABLE
                imports = None
        cursor = next_cursor
    return _LstInfo(symbols=symbols, imports=imports, append=append)


def _resolve_text(table: SymbolTable, sid: int) -> Optional[str]:
    if 1 <= sid <= _SYSTEM_MAX_SID:
        return _SYSTEM_TEXTS[sid]
    token = table.get(sid)
    return token.text if token is not None else None


# ---------------------------------------------------------------------------
# Environment tracking
# ---------------------------------------------------------------------------

class _EnvironmentBuilder:
    def __init__(self, catalog: SymbolTableCatalog):
        self.catalog = catalog
        self.table: SymbolTable = SYSTEM_SYMBOL_TABLE
        self.imports: Tuple[Tuple[str, int, int], ...] = ()
        # True when an import was not resolvable.  The identity can still be
        # preserved if its max_id (and therefore the SID layout) was declared.
        self.unresolved = False
        # True when the SID layout itself is unknowable.
        self.unbounded = False

    def reset(self) -> None:
        self.table = SYSTEM_SYMBOL_TABLE
        self.imports = ()
        self.unresolved = False
        self.unbounded = False

    def apply(self, info: _LstInfo) -> None:
        symbols = info.symbols
        if info.append:
            if self.table.table_type.is_system:
                imports = None
                new_imports = ()
            else:
                imports = [self.table]
                new_imports = self.imports
        else:
            imports = []
            new_imports_list = []
            for name, version, max_id in (info.imports or []):
                table, resolved_max, exact = self._resolve_import(name, version, max_id)
                if not exact:
                    self.unresolved = True
                    if max_id is None:
                        self.unbounded = True
                imports.append(table)
                new_imports_list.append((name, version, resolved_max))
            imports = imports or None
            new_imports = tuple(new_imports_list)
        self.table = SymbolTable(LOCAL_TABLE_TYPE, symbols, imports=imports)
        self.imports = new_imports

    def _resolve_import(self, name: str, version: int,
                        max_id: Optional[int]) -> Tuple[SymbolTable, int, bool]:
        """Resolves an import *without* trusting text from another version.

        ``SymbolTableCatalog.resolve`` may return a substitute built from a
        different version.  Its text is useful for display, but it is not proof
        of this import's text.  Only an exact version contributes text; a
        shorter or longer *same-version* declaration contributes text for the
        positions it actually defines and unknown text beyond them.
        """
        exact = None
        try:
            candidate = self.catalog.resolve(name, version, None)
        except CannotSubstituteTable:
            candidate = None
        if candidate is not None and not candidate.is_substitute \
                and candidate.version == version:
            exact = candidate
        if exact is None:
            declared_max = max_id
            if declared_max is None:
                # Without max_id the catalog may have guessed a length too.
                try:
                    fallback = self.catalog.resolve(name, version, None)
                except CannotSubstituteTable:
                    fallback = None
                declared_max = fallback.max_id if fallback is not None else 0
            return _placeholder(name, version, declared_max), declared_max, False

        if max_id is None:
            return exact, exact.max_id, True
        if max_id <= exact.max_id:
            symbols = [token.text for token in _take(exact, max_id)]
            table = SymbolTable(SHARED_TABLE_TYPE, symbols, name=name, version=version)
            return table, max_id, True
        symbols = [token.text for token in exact]
        symbols.extend([None] * (max_id - exact.max_id))
        table = SymbolTable(SHARED_TABLE_TYPE, symbols, name=name, version=version)
        return table, max_id, True

    def snapshot(self) -> Tuple[SymbolTable, Tuple[Tuple[str, int, int], ...], bool, bool]:
        return self.table, self.imports, self.unresolved, self.unbounded


def _placeholder(name: str, version: int, max_id: int) -> SymbolTable:
    from .symbols import placeholder_symbol_table
    return placeholder_symbol_table(name, version, max_id)


def _take(table: SymbolTable, count: int) -> Iterable[Any]:
    for index, token in enumerate(table):
        if index >= count:
            return
        yield token


def _is_lst(data: bytes, start: int, end: int, table: SymbolTable) -> bool:
    if data[start] >> 4 != _TID_ANNOTATION:
        return False
    sids, value_start = _annotation_sids(data, start)
    # null.struct is not a local symbol table even with the annotation.
    if not sids or not _is_container(data, value_start, _TID_STRUCT):
        return False
    first = sids[0]
    if first == SID_ION_SYMBOL_TABLE:
        return True
    token = table.get(first)
    return token is not None and token.text == TEXT_ION_SYMBOL_TABLE


def _resolve_needs(data: bytes, start: int, end: int, table: SymbolTable,
                   import_versions: Mapping[str, int]) -> Tuple[SymbolNeed, ...]:
    needs = []
    for sid in _walk_sids(data, start, end):
        token = table.get(sid)
        if token is None:
            needs.append(SymbolNeed(sid, None, None))
            continue
        location = token.location
        version = import_versions.get(location.name) if location is not None else None
        needs.append(SymbolNeed(sid, token.text, location, version))
    return tuple(needs)


def _needs_are_movable(needs: Sequence[SymbolNeed], table: SymbolTable,
                       unresolved: bool) -> bool:
    """Returns whether every referenced SID has a provable identity.

    A SID beyond the table's range is *undefined*, not a symbol with unknown
    text.  It cannot be recreated in a fresh volume, because no local slot or
    import position describes it.  A SID within range may legitimately have
    unknown text (a null slot or a placeholder import); that identity is
    preserved by position.
    """
    if unresolved:
        if any(not need.is_symbol_zero and not need.is_system for need in needs):
            return False
    for need in needs:
        if need.is_symbol_zero or need.is_system:
            continue
        if need.sid < 0 or table.get(need.sid) is None:
            return False
    return True


def _read_stream(data: bytes, catalog: SymbolTableCatalog) -> List[ValueRecord]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise IonVolumeError('expected a bytes-like binary Ion stream')
    data = bytes(data)
    if data and data[:4] != IVM:
        raise IonVolumeError('a binary ion stream must start with the IVM')
    builder = _EnvironmentBuilder(catalog)
    values: List[ValueRecord] = []
    for start, end in _top_level_tlvs(data):
        if start + 4 == end and data[start:end] == IVM:
            builder.reset()
            continue
        table, imports, unresolved, unbounded = builder.snapshot()
        if _is_lst(data, start, end, table):
            info = _parse_lst(data, start, end, table)
            builder.apply(info)
            continue
        if data[start] >> 4 == _TID_NULL and (data[start] & 0x0f) != 0x0f:
            # NOP padding is not a value.
            continue
        if _is_faux_ivm(data, start, end, table):
            # An unannotated symbol whose text is $ion_1_0 is a no-op at the
            # top level, exactly like the managed reader treats it.
            continue
        needs = _resolve_needs(data, start, end, table,
                               {name: version for name, version, _max_id in imports})
        record = ValueRecord(
            raw=data[start:end],
            start=start,
            end=end,
            table=table,
            imports=imports,
            needs=needs,
            index=len(values),
        )
        record.movable = _needs_are_movable(needs, table, unbounded)
        values.append(record)
    return values


def _is_faux_ivm(data: bytes, start: int, end: int, table: SymbolTable) -> bool:
    if data[start] >> 4 != _TID_SYMBOL:
        return False
    if data[start:start + 4] == IVM:
        return False
    sids, value_start = _annotation_sids(data, start)
    if sids or value_start != start:
        return False
    sid = _symbol_scalar_sid(data, start)
    if sid is None:
        return False
    return _resolve_text(table, sid) == TEXT_ION_1_0


# ---------------------------------------------------------------------------
# Encoding a local symbol table
# ---------------------------------------------------------------------------

def _encode_varuint(value: int) -> bytes:
    if value < 0:
        raise IonVolumeError('VarUInt must be non-negative')
    parts = [value & 0x7f]
    value >>= 7
    while value:
        parts.append(value & 0x7f)
        value >>= 7
    parts.reverse()
    parts[-1] |= 0x80
    return bytes(parts)


def _encode_type(tid: int, body: bytes) -> bytes:
    if len(body) < 14:
        return bytes([(tid << 4) | len(body)]) + body
    return bytes([(tid << 4) | 0x0e]) + _encode_varuint(len(body)) + body


def _encode_string(text: Optional[str]) -> bytes:
    if text is None:
        return b'\x8f'  # null.string
    return _encode_type(_TID_STRING, text.encode('utf-8'))


def _encode_int(value: int) -> bytes:
    if value == 0:
        return b'\x20'
    tid = 2 if value > 0 else 3
    magnitude = abs(value)
    body = magnitude.to_bytes((magnitude.bit_length() + 7) // 8, 'big')
    return _encode_type(tid, body)


def _encode_struct(fields: Sequence[Tuple[int, bytes]]) -> bytes:
    body = b''.join(_encode_varuint(sid) + value for sid, value in fields)
    return _encode_type(_TID_STRUCT, body)


def _encode_list(values: Sequence[bytes]) -> bytes:
    return _encode_type(_TID_LIST, b''.join(values))


def _encode_lst(imports: Sequence[Tuple[str, int, int]],
                symbols: Sequence[Optional[str]]) -> bytes:
    fields: List[Tuple[int, bytes]] = []
    if imports:
        descs = []
        for name, version, max_id in imports:
            desc_fields = [
                (SID_NAME, _encode_string(name)),
                (SID_VERSION, _encode_int(version)),
                (SID_MAX_ID, _encode_int(max_id)),
            ]
            descs.append(_encode_struct(desc_fields))
        fields.append((SID_IMPORTS, _encode_list(descs)))
    if symbols:
        fields.append((SID_SYMBOLS, _encode_list([_encode_string(s) for s in symbols])))
    struct_body = _encode_struct(fields)
    wrapper_body = _encode_varuint(1) + _encode_varuint(SID_ION_SYMBOL_TABLE) + struct_body
    return _encode_type(_TID_ANNOTATION, wrapper_body)


# ---------------------------------------------------------------------------
# Layout planning
# ---------------------------------------------------------------------------

@dataclass
class _Layout:
    imports: Tuple[Tuple[str, int, int], ...]
    symbols: Tuple[Optional[str], ...]
    preamble: bytes

    @property
    def opening_bytes(self) -> int:
        return len(self.preamble)


class _LayoutConflict(Exception):
    """A set of values cannot share one rebuilt symbol table."""


class _GapConflict(_LayoutConflict):
    """An import prefix before a referenced block cannot be reconstructed."""


_Entry = Tuple[Optional[str], Optional[ImportLocation], Optional[int]]


def _entry_map(records: Sequence[ValueRecord]) -> Dict[int, _Entry]:
    mapping: Dict[int, _Entry] = {}
    for record in records:
        for need in record.needs:
            if need.is_symbol_zero:
                continue
            if need.is_system:
                if need.text != _SYSTEM_TEXTS[need.sid]:
                    raise _LayoutConflict(
                        'system SID %d cannot preserve its text' % need.sid)
                continue
            current = (need.text, need.location, need.version)
            previous = mapping.get(need.sid)
            if previous is not None and previous != current:
                raise _LayoutConflict(
                    'SID %d is used with two different meanings: %r and %r'
                    % (need.sid, previous, current))
            mapping[need.sid] = current
    return mapping


def _version_for(name: str, records: Sequence[ValueRecord],
                 shared_tables: Mapping[str, SymbolTable]) -> int:
    versions = set()
    for record in records:
        for import_name, version, _max_id in record.imports:
            if import_name == name:
                versions.add(version)
    if versions:
        if len(versions) > 1:
            raise _LayoutConflict('shared table %r is imported at multiple versions' % (name,))
        return next(iter(versions))
    table = shared_tables.get(name)
    if table is not None:
        return table.version
    return 1


def _filler_imports(records: Sequence[ValueRecord], gap: int,
                    shared_tables: Mapping[str, SymbolTable]) -> Tuple[Tuple[str, int, int], ...]:
    """Builds import descriptors that occupy the SID range before the first
    referenced import block.  Only unreferenced slots are covered here."""
    if gap <= 0:
        return ()
    for record in records:
        if not record.imports:
            continue
        out = []
        consumed = 0
        for name, version, max_id in record.imports:
            take = min(max_id, gap - consumed)
            if take > 0:
                out.append((name, version, take))
                consumed += take
            if consumed == gap:
                return tuple(out)
        # A malformed/partial original prefix cannot fill the gap.
    # As a last resort, use a caller-provided shared table as a filler.  Its
    # slots are unreferenced by every value in this volume.
    for name, table in shared_tables.items():
        if table.max_id >= gap:
            return ((name, table.version, gap),)
    raise _GapConflict('cannot reconstruct the import slots before SID %d' % (10 + gap,))


def _build_layout(records: Sequence[ValueRecord],
                  shared_tables: Mapping[str, SymbolTable]) -> _Layout:
    mapping = _entry_map(records)
    imported: Dict[str, List[Tuple[int, int, Optional[int]]]] = {}
    locals_: Dict[int, Tuple[Optional[str], None]] = {}
    for sid, (text, location, version) in mapping.items():
        if location is None:
            if sid < 10:
                raise _LayoutConflict('local symbol SID %d collides with the system table' % sid)
            locals_[sid] = (text, None)
        elif location.name == '$ion':
            # A non-system SID claiming a system location is not reproducible.
            raise _LayoutConflict('SID %d claims the system import location %r' % (sid, location))
        else:
            if sid < 10:
                raise _LayoutConflict('imported SID %d collides with the system table' % sid)
            imported.setdefault(location.name, []).append((sid, location.position, version))

    blocks: List[Tuple[int, str, int, Optional[int]]] = []
    for name, refs in imported.items():
        starts = {sid - position + 1 for sid, position, _version in refs}
        if len(starts) != 1:
            raise _LayoutConflict(
                'shared table %r is referenced at inconsistent SID offsets: %r'
                % (name, sorted(starts)))
        start = next(iter(starts))
        if start < 10:
            raise _LayoutConflict('shared table %r would start before SID 10' % (name,))
        versions = {version for _sid, _position, version in refs if version is not None}
        if len(versions) > 1:
            raise _LayoutConflict(
                'shared table %r is referenced at multiple versions: %r'
                % (name, sorted(versions)))
        version = next(iter(versions)) if versions else None
        blocks.append((start, name, max(position for _sid, position, _v in refs), version))
    blocks.sort(key=lambda block: block[0])

    descriptors: List[Tuple[str, int, int]] = []
    if blocks:
        first_start = blocks[0][0]
        if first_start > 10:
            descriptors.extend(_filler_imports(records, first_start - 10, shared_tables))
        for i, (start, name, max_position, version) in enumerate(blocks):
            consumed = 10 + sum(d[2] for d in descriptors)
            if consumed != start:
                raise _LayoutConflict(
                    'shared table %r cannot start at SID %d (layout is at %d)'
                    % (name, start, consumed))
            if i + 1 < len(blocks):
                next_start = blocks[i + 1][0]
                max_id = next_start - start
            else:
                max_id = max_position
            if max_id < max_position:
                raise _LayoutConflict('shared table %r has overlapping SID ranges' % (name,))
            if version is None:
                version = _version_for(name, records, shared_tables)
            descriptors.append((name, version, max_id))
        # Extend the final import only through its required positions; local
        # symbols follow immediately.
        import_span = sum(max_id for _name, _version, max_id in descriptors)
    else:
        import_span = 0

    local_start = 10 + import_span
    if locals_ and min(locals_) < local_start:
        raise _LayoutConflict(
            'local symbols start at SID %d but SID %d is required'
            % (local_start, min(locals_)))

    symbols: List[Optional[str]] = []
    if locals_:
        max_local = max(locals_)
        for sid in range(local_start, max_local + 1):
            symbols.append(locals_.get(sid, (None, None))[0])

    lst = _encode_lst(descriptors, symbols)
    preamble = IVM + lst if (descriptors or symbols) else IVM
    return _Layout(tuple(descriptors), tuple(symbols), preamble)


# ---------------------------------------------------------------------------
# Public parsing / planning
# ---------------------------------------------------------------------------

def read_values(data: bytes,
                catalog: Optional[SymbolTableCatalog] = None,
                shared_tables: Optional[Iterable[SymbolTable]] = None) -> List[ValueRecord]:
    """Parses ``data`` into top-level :class:`ValueRecord` objects.

    Local symbol tables and IVMs are consumed, not returned as values.
    """
    catalog = _catalog(catalog, tuple(shared_tables or ()))
    return _read_stream(data, catalog)


def _catalog(catalog: Optional[SymbolTableCatalog],
             shared_tables: Optional[Iterable[SymbolTable]]) -> SymbolTableCatalog:
    if catalog is None:
        catalog = SymbolTableCatalog()
    if shared_tables:
        for table in shared_tables:
            if table.table_type is not SHARED_TABLE_TYPE:
                raise IonVolumeError('only shared symbol tables may be carried')
            try:
                catalog.register(table)
            except ValueError:
                # An equivalent registration is harmless; a substitute table
                # cannot be carried.
                if table.is_substitute:
                    raise IonVolumeError('a substitute table cannot be carried as a shared table')
    return catalog


def _shared_table_map(tables: Optional[Iterable[SymbolTable]]) -> Dict[str, SymbolTable]:
    out: Dict[str, SymbolTable] = {}
    for table in tables or ():
        previous = out.get(table.name)
        if previous is None or previous.version < table.version:
            out[table.name] = table
    return out


def _normalize_groups(groups: Optional[Iterable[Any]], count: int) -> Tuple[Tuple[int, ...], ...]:
    if groups is None:
        return ()
    if isinstance(groups, Mapping):
        raw_groups = list(groups.values())
    else:
        raw_groups = list(groups)
    out: List[Tuple[int, ...]] = []
    for raw in raw_groups:
        if isinstance(raw, (str, bytes)):
            raise IonVolumeError('a group must contain value indices, not text')
        try:
            members = tuple(int(index) for index in raw)
        except TypeError:
            members = (int(raw),)
        if len(set(members)) != len(members):
            raise IonVolumeError('group contains duplicate value indices: %r' % (members,))
        for index in members:
            if index < 0 or index >= count:
                raise IonVolumeError('group value index out of range: %d' % index)
        out.append(tuple(sorted(members)))
    return tuple(out)


def _cut_allowed(groups: Sequence[Sequence[int]], count: int) -> List[bool]:
    allowed = [True] * (count + 1)
    for group in groups:
        lo, hi = min(group), max(group)
        for cut in range(lo + 1, hi + 1):
            allowed[cut] = False
    return allowed


def _conflict_values(records: Sequence[ValueRecord], exc: Exception) -> Tuple[int, ...]:
    """Finds the values that reference the SID named in a layout conflict."""
    message = str(exc)
    if 'SID ' not in message:
        return ()
    # Diagnostic only: identify the values mentioning this SID.
    digits = []
    for char in message.split('SID ', 1)[1]:
        if char.isdigit():
            digits.append(char)
        else:
            break
    if not digits:
        return ()
    sid = int(''.join(digits))
    return tuple(record.index for record in records
                 if any(need.sid == sid for need in record.needs))


def plan_volumes(data: bytes,
                 catalog: Optional[SymbolTableCatalog] = None,
                 shared_tables: Optional[Iterable[SymbolTable]] = None,
                 max_opening_bytes: Optional[int] = None,
                 groups: Optional[Iterable[Any]] = None,
                 **aliases: Any) -> VolumePlan:
    """Plans the cheapest contiguous partition of top-level values into volumes.

    Args:
        data (bytes): A complete binary Ion stream.
        catalog (Optional[SymbolTableCatalog]): Catalog used to resolve imports.
        shared_tables (Optional[Iterable[SymbolTable]]): Shared tables that may
            travel with a volume (and therefore may be imported by an output
            LST).  Equivalent to registering them in a fresh catalog.
        max_opening_bytes (Optional[int]): Maximum ``IVM + LST`` bytes per
            volume.  ``None`` means unlimited.
        groups (Optional[Iterable[Iterable[int]]]): Groups of zero-based
            top-level value indices that must remain in one volume.  A group
            that spans non-adjacent values also keeps the values between them
            in the same volume, because volumes are contiguous.

    Other accepted keyword aliases:
        ``opening_budget`` / ``max_extra_bytes`` for ``max_opening_bytes``;
        ``carry_tables`` for ``shared_tables``; ``value_groups`` for ``groups``.

    Returns:
        VolumePlan: Contains ``volumes``, ``problems``, and the total opening
        byte count.  Problems do not raise; use :meth:`VolumePlan.raise_if_problems`
        or :func:`split_volumes` to fail instead.

    The planner is exact for contiguous partitions: when every prefix is a
    candidate it finds the minimum-total-opening partition (ties go to fewer
    volumes).  A group disables cuts inside its span, so only partitions that
    keep the group together are considered.
    """
    if 'opening_budget' in aliases:
        if max_opening_bytes is not None:
            raise TypeError('specify only one of max_opening_bytes and opening_budget')
        max_opening_bytes = aliases.pop('opening_budget')
    if 'max_extra_bytes' in aliases:
        if max_opening_bytes is not None:
            raise TypeError('specify only one opening-budget argument')
        max_opening_bytes = aliases.pop('max_extra_bytes')
    if 'carry_tables' in aliases:
        if shared_tables is not None:
            raise TypeError('specify only one of shared_tables and carry_tables')
        shared_tables = aliases.pop('carry_tables')
    if 'value_groups' in aliases:
        if groups is not None:
            raise TypeError('specify only one of groups and value_groups')
        groups = aliases.pop('value_groups')
    if aliases:
        raise TypeError('unexpected keyword arguments: %r' % (sorted(aliases),))

    if max_opening_bytes is not None and max_opening_bytes < len(IVM):
        raise IonVolumeError('the opening budget cannot be smaller than the IVM (%d bytes)' % len(IVM))

    # Materialize once: callers may pass a generator, and the tables are
    # consulted both for catalog resolution and for layout fillers.
    shared_tables = tuple(shared_tables or ())
    catalog = _catalog(catalog, shared_tables)
    carry = _shared_table_map(shared_tables)
    try:
        records = _read_stream(data, catalog)
    except IonVolumeError as exc:
        return VolumePlan(problems=[PlanProblem('invalid_stream', str(exc))])
    count = len(records)
    normalized_groups = _normalize_groups(groups, count)
    plan = VolumePlan(values=records, groups=normalized_groups)

    if count == 0:
        return plan

    # Mark values that cannot preserve symbol identity.
    unmovable: Set[int] = set()
    for record in records:
        if not record.movable:
            unmovable.add(record.index)
            plan.problems.append(PlanProblem(
                'unresolved_symbol',
                'value references a symbol with no provable text or import location',
                (record.index,)))

    # Check each group in isolation so an impossible group is named even when
    # the whole-stream DP has no solution.
    group_failures: List[int] = []
    for group_index, group in enumerate(normalized_groups):
        if not group:
            continue
        lo, hi = min(group), max(group) + 1
        block = records[lo:hi]
        bad = [r.index for r in block if r.index in unmovable]
        if bad:
            group_failures.append(group_index)
            plan.problems.append(PlanProblem(
                'group_unmovable',
                'group contains a value whose symbol identity cannot be preserved',
                tuple(bad), group_index))
            continue
        try:
            layout = _build_layout(block, carry)
        except _LayoutConflict as exc:
            group_failures.append(group_index)
            conflicts = _conflict_values(block, exc)
            plan.problems.append(PlanProblem(
                'group_conflict',
                'the group cannot share one symbol table: %s' % (exc,),
                conflicts or tuple(range(lo, hi)), group_index))
            continue
        if max_opening_bytes is not None and layout.opening_bytes > max_opening_bytes:
            group_failures.append(group_index)
            plan.problems.append(PlanProblem(
                'group_budget',
                'the group needs %d opening bytes but the budget is %d'
                % (layout.opening_bytes, max_opening_bytes),
                tuple(range(lo, hi)), group_index))

    plan.group_failures = tuple(group_failures)
    if plan.problems:
        return plan

    # When there is no per-volume opening budget, every requested group is
    # already satisfied by one volume and the union symbol table is never more
    # expensive than the sum of split tables.  Avoid the quadratic DP.
    if max_opening_bytes is None:
        try:
            layout = _build_layout(records, carry)
        except _LayoutConflict:
            layout = None
        if layout is not None:
            volume_data = layout.preamble + b''.join(record.raw for record in records)
            plan.volumes.append(Volume(
                data=volume_data,
                value_indices=tuple(range(count)),
                preamble=layout.preamble,
            ))
            plan.total_opening_bytes = layout.opening_bytes
            return plan

    cut_allowed = _cut_allowed(normalized_groups, count)
    # DP over prefixes.  Cost is (opening bytes, volume count) so a tie is
    # resolved in favor of fewer volumes.
    best: List[Optional[Tuple[int, int]]] = [None] * (count + 1)
    previous: List[Optional[int]] = [None] * (count + 1)
    layouts: Dict[Tuple[int, int], Optional[_Layout]] = {}
    best[0] = (0, 0)
    for end in range(1, count + 1):
        for start in range(end - 1, -1, -1):
            if not cut_allowed[start]:
                continue
            if best[start] is None:
                continue
            key = (start, end)
            if key in layouts:
                layout = layouts[key]
            else:
                try:
                    layout = _build_layout(records[start:end], carry)
                except _GapConflict:
                    # Adding earlier records may supply the import prefix that
                    # makes this block reproducible, so keep looking.
                    continue
                except _LayoutConflict:
                    layout = None
                layouts[key] = layout
            if layout is None:
                # A SID-meaning conflict among values in [start, end) persists
                # in every superset, so smaller starts cannot fix it.
                break
            if max_opening_bytes is not None and layout.opening_bytes > max_opening_bytes:
                # Opening size only grows as the block grows.
                break
            candidate = (best[start][0] + layout.opening_bytes, best[start][1] + 1)
            if best[end] is None or candidate < best[end]:
                best[end] = candidate
                previous[end] = start
    if best[count] is None:
        plan.problems.append(PlanProblem(
            'no_partition',
            'no contiguous partition satisfies the budget and group constraints'))
        return plan

    # Reconstruct.
    bounds = []
    cursor = count
    while cursor > 0:
        start = previous[cursor]
        assert start is not None
        bounds.append((start, cursor))
        cursor = start
    bounds.reverse()
    for start, end in bounds:
        layout = layouts[(start, end)]
        assert layout is not None
        volume_data = layout.preamble + b''.join(records[i].raw for i in range(start, end))
        plan.volumes.append(Volume(
            data=volume_data,
            value_indices=tuple(range(start, end)),
            preamble=layout.preamble,
        ))
        plan.total_opening_bytes += layout.opening_bytes
    return plan


def split_volumes(data: bytes,
                  catalog: Optional[SymbolTableCatalog] = None,
                  shared_tables: Optional[Iterable[SymbolTable]] = None,
                  max_opening_bytes: Optional[int] = None,
                  groups: Optional[Iterable[Any]] = None,
                  **aliases: Any) -> List[Volume]:
    """Returns the planned volumes, raising on any unsatisfied constraint."""
    plan = plan_volumes(data, catalog=catalog, shared_tables=shared_tables,
                        max_opening_bytes=max_opening_bytes, groups=groups, **aliases)
    plan.raise_if_problems()
    return plan.volumes


# Compatibility aliases; these names are useful for callers that think in
# terms of streams or bytes rather than the plan object.
split_ion_volumes = split_volumes
plan_ion_volumes = plan_volumes


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValueAudit:
    index: int
    volume_index: Optional[int]
    raw_equal: bool
    events_equal: bool
    symbol_meaning_equal: bool
    message: str = ''
    needs_equal: bool = True

    @property
    def ok(self) -> bool:
        return self.raw_equal and self.events_equal and \
            self.symbol_meaning_equal and self.needs_equal


@dataclass
class AuditReport:
    values: List[ValueAudit] = field(default_factory=list)
    volumes: List[Dict[str, Any]] = field(default_factory=list)
    group_results: List[Dict[str, Any]] = field(default_factory=list)
    minimal_opening_bytes: Optional[int] = None
    actual_opening_bytes: int = 0
    minimal: bool = False
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(value.ok for value in self.values)


def _collect_event_groups(data: bytes, catalog: SymbolTableCatalog) -> List[List[Any]]:
    """Returns one event list per top-level user value."""
    reader = blocking_reader(managed_reader(binary_reader(), catalog), BytesIO(data))
    groups: List[List[Any]] = []
    current: Optional[List[Any]] = None
    event = reader.send(NEXT_EVENT)
    while event.event_type is not IonEventType.STREAM_END:
        if event.depth == 0:
            if current is None:
                current = []
                groups.append(current)
            current.append(event)
            if event.event_type is IonEventType.SCALAR or event.event_type is IonEventType.CONTAINER_END:
                current = None
        else:
            if current is None:
                raise IonVolumeError('managed reader produced an unexpected nested event')
            current.append(event)
        event = reader.send(NEXT_EVENT)
    return groups


def _tokens_in_events(events: Sequence[Any]) -> List[Any]:
    tokens = []
    for event in events:
        if event.field_name is not None:
            tokens.append(event.field_name)
        tokens.extend(event.annotations)
        if event.ion_type is IonType.SYMBOL and event.value is not None:
            tokens.append(event.value)
    return tokens


def _token_key(token: Any) -> Tuple[Any, Any, Any]:
    text = getattr(token, 'text', token)
    location = getattr(token, 'location', None)
    return (text, location, getattr(token, 'sid', None))


def _event_skeleton(events: Sequence[Any]) -> List[Tuple[Any, ...]]:
    """Semantic event shape without resolved symbol text.

    Using SIDs directly here would force every value to keep its original SID
    when split into a fresh volume.  That is not required: only the *meaning*
    of a token has to match.  This skeleton therefore compares Ion types,
    scalar values, field presence, and annotation count/positions.
    """
    shape = []
    for event in events:
        value = event.value
        if isinstance(value, float):
            value = ('nan',) if value != value else ('float', value)
        shape.append((
            event.event_type,
            event.ion_type,
            value,
            event.field_name is not None,
            len(event.annotations),
            event.depth,
        ))
    return shape


def audit_volumes(source: bytes,
                  volumes: Sequence[Union[Volume, bytes]],
                  catalog: Optional[SymbolTableCatalog] = None,
                  shared_tables: Optional[Iterable[SymbolTable]] = None,
                  groups: Optional[Iterable[Any]] = None,
                  max_opening_bytes: Optional[int] = None) -> AuditReport:
    """Audits volumes against the source stream.

    Checks performed:
        * every original value appears once, in order, with identical raw TLV
          bytes;
        * every referenced SID has the same text and import identity
          (shared-table name, position, and version) before and after;
        * reopening each volume with the managed reader yields the same
          field-name, annotation, and symbol-value semantics (Ion types,
          scalar values, field presence, annotation positions);
        * all requested groups share a volume;
        * each volume's opening is no larger than the same values require, and
          the total equals the planner's cheapest partition.

    The source and volumes are never modified.
    """
    shared_tables = tuple(shared_tables or ())
    catalog = _catalog(catalog, shared_tables)
    carry = _shared_table_map(shared_tables)
    report = AuditReport()
    try:
        original_values = _read_stream(source, catalog)
    except IonVolumeError as exc:
        report.problems.append('invalid source stream: %s' % (exc,))
        return report
    except Exception as exc:
        report.problems.append('invalid source stream: %s' % (exc,))
        return report
    count = len(original_values)
    normalized_groups = _normalize_groups(groups, count)

    try:
        original_events = _collect_event_groups(source, catalog)
    except Exception as exc:  # pragma: no cover - defensive, malformed input
        report.problems.append('could not read source events: %s' % (exc,))
        original_events = []
    if len(original_events) != count:
        report.problems.append(
            'source produced %d event groups for %d top-level values'
            % (len(original_events), count))

    flat_values: List[ValueRecord] = []
    flat_events: List[List[Any]] = []
    value_to_volume: Dict[int, int] = {}
    for volume_index, volume in enumerate(volumes):
        blob = volume.data if isinstance(volume, Volume) else bytes(volume)
        try:
            parsed = _read_stream(blob, catalog)
        except IonVolumeError as exc:
            report.problems.append('volume %d cannot be read: %s' % (volume_index, exc))
            report.volumes.append({'index': volume_index, 'readable': False,
                                   'error': str(exc), 'opening_bytes': None,
                                   'value_indices': []})
            continue
        try:
            events = _collect_event_groups(blob, catalog)
        except Exception as exc:
            events = []
            report.problems.append('volume %d events cannot be read: %s' % (volume_index, exc))
        opening = len(blob) - sum(record.size for record in parsed)
        report.actual_opening_bytes += opening
        indices = []
        for record, value_events in zip(parsed, events):
            global_index = len(flat_values)
            indices.append(global_index)
            value_to_volume[global_index] = volume_index
            flat_values.append(record)
            flat_events.append(value_events)
        if len(parsed) != len(events):
            report.problems.append(
                'volume %d produced %d TLVs but %d event groups'
                % (volume_index, len(parsed), len(events)))
        report.volumes.append({
            'index': volume_index,
            'readable': True,
            'opening_bytes': opening,
            'value_indices': indices,
        })

    if len(flat_values) != count:
        report.problems.append(
            'volumes contain %d values but the source contains %d'
            % (len(flat_values), count))

    for index in range(min(count, len(flat_values))):
        source_record = original_values[index]
        volume_record = flat_values[index]
        raw_equal = source_record.raw == volume_record.raw
        needs_equal = source_record.needs == volume_record.needs
        events_equal = False
        meaning_equal = False
        message = ''
        if not needs_equal:
            message = 'symbol text/import identity differs'
        if index < len(original_events) and index < len(flat_events):
            source_events = original_events[index]
            target_events = flat_events[index]
            try:
                # Force the managed symbols to resolve so a corrupt SID is a
                # failed audit rather than an exception escaping.
                _tokens_in_events(source_events)
                _tokens_in_events(target_events)
                events_equal = _event_skeleton(source_events) == _event_skeleton(target_events)
            except Exception as exc:
                events_equal = False
                message = 'managed event materialization failed: %s' % (exc,)
            if not events_equal and not message:
                message = 'managed events differ'
            if events_equal and needs_equal:
                source_tokens = _tokens_in_events(source_events)
                target_tokens = _tokens_in_events(target_events)
                meaning_equal = [_token_key(t) for t in source_tokens] == \
                                [_token_key(t) for t in target_tokens]
                if not meaning_equal:
                    message = 'resolved symbol text/import identity differs'
        elif index >= len(flat_events):
            message = message or 'no readable value in the volumes'
        report.values.append(ValueAudit(
            index=index,
            volume_index=value_to_volume.get(index),
            raw_equal=raw_equal,
            events_equal=events_equal,
            symbol_meaning_equal=meaning_equal,
            message=message,
            needs_equal=needs_equal,
        ))

    for group_index, group in enumerate(normalized_groups):
        placed = {value_to_volume.get(index) for index in group}
        ok = len(placed) == 1 and None not in placed
        report.group_results.append({
            'group_index': group_index,
            'values': list(group),
            'volume_index': next(iter(placed)) if ok else None,
            'ok': ok,
        })
        if not ok:
            report.problems.append(
                'group %d spans volumes or is missing: values=%r volumes=%r'
                % (group_index, group, sorted(placed, key=lambda x: -1 if x is None else x)))

    # Recompute the cheapest opening that the planner can find.  This uses the
    # actual volumes' value indices as groups so the comparison is like for
    # like; the source itself is never reflowed.
    try:
        baseline = plan_volumes(source, catalog=catalog,
                                shared_tables=shared_tables,
                                max_opening_bytes=max_opening_bytes,
                                groups=normalized_groups)
        if baseline.ok:
            report.minimal_opening_bytes = baseline.total_opening_bytes
            report.minimal = baseline.total_opening_bytes == report.actual_opening_bytes
            if not report.minimal:
                report.problems.append(
                    'opening overhead is %d bytes; the minimum is %d'
                    % (report.actual_opening_bytes, baseline.total_opening_bytes))
        else:
            report.problems.extend(str(problem) for problem in baseline.problems)
    except Exception as exc:
        report.problems.append('could not recompute the minimum opening: %s' % (exc,))

    # A volume carrying a full but unneeded LST is readable and byte-faithful
    # yet not the cheapest plan.  Compare each actual preamble against the
    # exact preamble required by the values it holds, which catches redundant
    # imports/symbols even if the total happens to match another partition.
    _check_preamble_minimality(original_values, volumes, carry, report)

    return report


def _check_preamble_minimality(original_values: Sequence[ValueRecord],
                               volumes: Sequence[Union[Volume, bytes]],
                               carry: Mapping[str, SymbolTable],
                               report: AuditReport) -> None:
    """Flags any volume whose opening can be shortened for the same values."""
    for volume_index, volume in enumerate(volumes):
        if volume_index >= len(report.volumes):
            continue
        entry = report.volumes[volume_index]
        if not entry.get('readable'):
            continue
        indices = entry.get('value_indices') or ()
        if not indices:
            continue
        if indices[0] >= len(original_values) or indices[-1] >= len(original_values):
            continue
        try:
            layout = _build_layout(
                [original_values[i] for i in indices], carry)
        except _LayoutConflict:
            continue
        blob = volume.data if isinstance(volume, Volume) else bytes(volume)
        actual = entry.get('opening_bytes')
        if actual is None:
            actual = len(blob)
        if actual > layout.opening_bytes:
            entry['minimal_opening_bytes'] = layout.opening_bytes
            report.problems.append(
                'volume %d opening is %d bytes; the same values need only %d'
                % (volume_index, actual, layout.opening_bytes))


__all__ = [
    'IVM',
    'IonVolumeError',
    'SymbolNeed',
    'ValueRecord',
    'Volume',
    'VolumePlan',
    'PlanProblem',
    'read_values',
    'plan_volumes',
    'split_volumes',
    'split_ion_volumes',
    'plan_ion_volumes',
    'audit_volumes',
    'ValueAudit',
    'AuditReport',
]
