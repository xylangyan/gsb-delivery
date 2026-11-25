"""
Outbound (archive) support for EBML sampling volumes.

An *outbound* copy of a sampling volume is one written to a medium whose
addressing is fixed ahead of time.  The rewrite may change container size-field
widths and may insert ``Void`` padding, but it must preserve, byte for byte,
every scalar element's header and payload and must keep every business element
in the same parent and the same sibling order under **both** the approved old
structure table and the new one.

:func:`exportVolume` is the entry point.  It accepts:

``segment``
    The sampling segment: bytes, or a seekable binary stream.

``tree``
    The approved original-volume element tree.  Entries are keyed by their
    original header offset (the offset of the element ID).  Nested trees,
    flat mappings, and parsed ``Document`` objects are accepted.

``oldSchema`` / ``newSchema``
    The old and new structure tables: ``Schema`` instances, schema XML paths or
    strings, or an element class belonging to a schema.

``permissions``
    A `PermissionTable`, or a mapping/sequence with ``containers`` and
    ``windows``.  Containers list the permitted size-field widths (and whether
    unknown length is allowed), plus the fee for rewriting the length field.
    Windows list absolute address ranges in which a ``Void`` may be inserted
    and the one-time fee for opening that window.

``locks``
    Optional mapping of original header offsets to required new addresses.
    Roots are locked to their original segment addresses by default, and the
    volume end is locked when the permission table supplies it.

The planner searches all address-feasible width choices and Void placements and
returns the cheapest one that both readers can replay correctly.  Payload bytes
are copied only after a layout has been selected.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from . import core, decoding, encoding


__all__ = [
    'ArchiveError', 'AddressConflict', 'LayoutConflict', 'ExportError',
    'Placement', 'LocatorEntry', 'Locator', 'ExportResult',
    'PermissionTable', 'exportVolume', 'archiveVolume', 'relocateVolume',
    'export_volume', 'archive_volume', 'relocate_volume',
    'repackageVolume', 'repackVolume', 'repackage_volume', 'repack_volume',
]


# ==============================================================================
# Errors and public result types
# ==============================================================================

class ArchiveError(Exception):
    """Base class for outbound archive failures."""


class AddressConflict(ArchiveError):
    """Locked addresses cannot all be satisfied."""


class LayoutConflict(ArchiveError):
    """Locked addresses are reachable, but a reader would misread parentage."""


class ExportError(ArchiveError):
    """The archive request is invalid or cannot be satisfied."""


@dataclass(frozen=True)
class Placement:
    """One item in the emitted layout, at absolute output offsets.

    ``kind`` is ``'element'`` or ``'void'``.  For an element,
    ``sizeValue is None`` denotes the EBML unknown-length descriptor.
    For a Void, ``element`` is ``None`` and all bytes after the size field are
    ``0xFF``.
    """
    kind: str
    offset: int
    headerEnd: int
    payloadEnd: int
    element: Optional['_Node'] = None
    sizeWidth: int = 0
    sizeValue: Optional[int] = None

    @property
    def totalEnd(self) -> int:
        return self.payloadEnd

    @property
    def length(self) -> int:
        return self.payloadEnd - self.offset


@dataclass(frozen=True)
class LocatorEntry:
    """New address of one business element.

    The entry is keyed by the element's **original** header offset and reports
    the new header range (ID + size field), the new payload range, the new
    element end, the original-offset parent, and original-offset children in
    order.  ``path`` is the chain of original header offsets from the root.
    """
    original_offset: int
    header_start: int
    header_end: int
    payload_start: int
    payload_end: int
    element_end: int
    id: int
    name: str
    parent: Optional[int]
    children: Tuple[int, ...]
    size_width: int
    size_value: Optional[int]
    path: Tuple[int, ...]

    @property
    def newHeaderOffset(self) -> int:
        return self.header_start

    @property
    def newPayloadOffset(self) -> int:
        return self.payload_start

    @property
    def newPayloadSize(self) -> int:
        return self.payload_end - self.payload_start

    # camelCase aliases, matching the rest of ebmlite's public API.
    @property
    def originalOffset(self) -> int:
        return self.original_offset

    @property
    def headerStart(self) -> int:
        return self.header_start

    @property
    def headerEnd(self) -> int:
        return self.header_end

    @property
    def payloadStart(self) -> int:
        return self.payload_start

    @property
    def payloadEnd(self) -> int:
        return self.payload_end

    @property
    def elementEnd(self) -> int:
        return self.element_end

    @property
    def sizeWidth(self) -> int:
        return self.size_width

    @property
    def sizeValue(self) -> Optional[int]:
        return self.size_value

    def asDict(self) -> Dict[str, Any]:
        return {'originalOffset': self.original_offset,
                'headerStart': self.header_start,
                'headerEnd': self.header_end,
                'payloadStart': self.payload_start,
                'payloadEnd': self.payload_end,
                'elementEnd': self.element_end,
                'id': self.id,
                'name': self.name,
                'parent': self.parent,
                'children': self.children,
                'sizeWidth': self.size_width,
                'sizeValue': self.size_value,
                'path': self.path}


class Locator(Mapping[int, LocatorEntry]):
    """Mapping from original header offset to `LocatorEntry`, in volume order."""

    __slots__ = ('_entries', '_order', 'volumeStart', 'volumeEnd')

    def __init__(self, entries: Iterable[LocatorEntry],
                 volumeStart: int = 0, volumeEnd: int = 0):
        ordered = list(entries)
        self._entries = {e.original_offset: e for e in ordered}
        self._order = tuple(e.original_offset for e in orderer(ordered))
        self.volumeStart = volumeStart
        self.volumeEnd = volumeEnd

    def __getitem__(self, key: int) -> LocatorEntry:
        return self._entries[key]

    def __iter__(self) -> Iterator[int]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    @property
    def entries(self) -> Tuple[LocatorEntry, ...]:
        return tuple(self._entries[k] for k in self._order)

    def byHeader(self) -> Dict[int, LocatorEntry]:
        """A plain dict copy keyed by original header offset."""
        return dict(self._entries)

    def getByOriginalOffset(self, offset: int) -> Optional[LocatorEntry]:
        return self._entries.get(offset)

    def asDict(self) -> Dict[int, Dict[str, Any]]:
        return {k: self._entries[k].asDict() for k in self._order}

    def __repr__(self) -> str:
        return '<Locator %d entries, volumeEnd=%d>' % (len(self), self.volumeEnd)


def orderer(entries):
    return sorted(entries, key=lambda e: e.header_start)


@dataclass
class ExportResult:
    """Result of :func:`exportVolume`.

    ``ok`` is true only when a volume was emitted.  On failure ``volume`` and
    ``locator`` are ``None``:

    * ``failure == 'address'``: no arrangement can hit the locked starts or
      the locked volume end.  ``affectedRanges`` lists the locked intervals
      that could not be satisfied.
    * ``failure == 'layout'``: locked addresses are reachable, but at least
      one reader would assign the wrong parent to an element at a locked
      address.  ``firstErrors`` names, per reader, the first misread element,
      the parent it would actually see, and that element's new offset.

    In both failure cases ``layout`` holds the address-feasible arrangement
    (when one exists) for inspection, but no volume bytes.
    """
    ok: bool
    failure: Optional[str] = None
    volume: Optional[bytes] = None
    locator: Optional[Locator] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    cost: Optional[int] = None
    layout: Tuple[Placement, ...] = ()

    @property
    def data(self):
        return self.volume

    @property
    def output(self):
        return self.volume

    @property
    def newVolume(self):
        return self.volume

    @property
    def newLocator(self):
        return self.locator

    @property
    def firstErrors(self):
        return self.diagnostics.get('firstErrors', ())

    @property
    def affectedRanges(self):
        return self.diagnostics.get('affectedRanges', ())

    def raise_for_status(self):
        if not self.ok:
            raise ExportError(self.diagnostics.get('message', self.failure))
        return self


# ==============================================================================
# Input normalization
# ==============================================================================

def _asBytes(segment: Any) -> bytes:
    """Return segment bytes without disturbing a caller-owned stream."""
    if isinstance(segment, (bytes, bytearray, memoryview)):
        return bytes(segment)
    if hasattr(segment, 'read'):
        try:
            pos = segment.tell()
        except Exception:
            pos = None
        try:
            segment.seek(0)
        except Exception:
            pass
        raw = segment.read()
        if pos is not None:
            try:
                segment.seek(pos)
            except Exception:
                pass
        return bytes(raw)
    raise TypeError('Sampling segment must be bytes or a seekable binary stream')


def _asSchema(source: Any, role: str) -> core.Schema:
    if isinstance(source, core.Schema):
        return source
    if isinstance(source, type) and issubclass(source, core.Element):
        schema = getattr(source, 'schema', None)
        if not isinstance(schema, core.Schema):
            raise TypeError('%s structure table has no Schema' % role)
        return schema
    if isinstance(source, core.Document):
        return source.schema
    if isinstance(source, (str, bytes)):
        if isinstance(source, bytes):
            return core.parseSchema(source.decode('utf-8'),
                                    name='%s-archive' % role)
        if source.lstrip().startswith('<'):
            return core.parseSchema(source, name='%s-archive' % role)
        return core.loadSchema(source)
    raise TypeError('Cannot use %r as the %s structure table' % (source, role))


def _intOrNone(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _asInt(value: Any, what: str) -> int:
    result = _intOrNone(value)
    if result is None:
        raise TypeError('Cannot interpret %r as %s' % (value, what))
    return result


def _asBool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(value)


# ==============================================================================
# Normalized original tree
# ==============================================================================

@dataclass
class _Node:
    """One approved business element in the original volume."""
    originalOffset: int
    id: int
    name: str
    idBytes: bytes
    rawSizeBytes: bytes
    originalSizeWidth: int
    originalSizeValue: Optional[int]
    payloadStart: int
    payloadEnd: int
    payloadSize: int
    isMaster: bool
    children: List['_Node'] = field(default_factory=list)
    parent: Optional['_Node'] = None
    rawPayload: bytes = b''
    path: Tuple[int, ...] = ()

    @property
    def originalUnknown(self) -> bool:
        return self.originalSizeValue is None

    @property
    def originalHeaderEnd(self) -> int:
        return self.payloadStart

    def __repr__(self):
        return '<_Node %s id=0x%X @%d>' % (self.name, self.id, self.originalOffset)


@dataclass
class _Spec:
    offset: int
    id: Optional[int]
    name: Optional[str]
    children: List[Any]
    declaredEnd: Optional[int]


def _attr(obj: Any, names: Sequence[str], default=None):
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _childSpecs(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)) and all(isinstance(x, int) for x in value):
        return list(value)
    if isinstance(value, Mapping):
        for key in ('children', 'elements', 'value', 'items', 'roots'):
            if key in value:
                ch = value[key]
                if callable(ch):
                    ch = ch()
                if isinstance(ch, Mapping):
                    return list(ch.values())
                if ch is not None:
                    return list(ch)
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, core.MasterElement):
        return list(value)
    for key in ('children', 'value'):
        ch = getattr(value, key, None)
        if ch is None:
            continue
        try:
            return list(ch() if callable(ch) else ch)
        except TypeError:
            pass
    return []


def _specFrom(value: Any, key: Optional[int] = None) -> _Spec:
    if isinstance(value, int):
        return _Spec(value, None, None, [], None)
    if isinstance(value, Mapping):
        offset = _attr(value, ('offset', 'headerOffset', 'header_offset',
                               'originalOffset', 'original_offset', 'start',
                               'headerStart'))
        eid = _attr(value, ('id', 'eid', 'elementId'))
        name = _attr(value, ('name', 'elementName'))
        end = _attr(value, ('payloadEnd', 'payload_end', 'elementEnd',
                            'element_end', 'end'))
        children = _childSpecs(value)
    elif isinstance(value, core.Element):
        offset = value.offset
        eid = value.id
        name = value.name
        end = value.payloadOffset + value.size
        children = _childSpecs(value)
    else:
        offset = _attr(value, ('offset', 'headerOffset', 'header_offset',
                               'originalOffset', 'original_offset', 'start'))
        eid = _attr(value, ('id', 'eid'))
        name = _attr(value, ('name', 'elementName', 'tag'))
        end = _attr(value, ('payloadEnd', 'payload_end', 'elementEnd',
                            'element_end', 'end'))
        children = _childSpecs(value)

    if offset is None:
        offset = key
    if offset is None:
        raise ValueError('Tree entry %r has no original header offset' % (value,))
    eid = _intOrNone(eid)
    return _Spec(int(offset), eid, name, children,
                 _intOrNone(end))


def _validChildIds(schemas) -> Tuple[Dict[int, set], set]:
    childrenOf: Dict[int, set] = {}
    globals_ = set()
    for schema in schemas:
        if schema is None:
            continue
        globals_.update(schema.globals)
        for eid, cls in schema.elements.items():
            if issubclass(cls, core.MasterElement):
                childrenOf.setdefault(eid, set()).update(cls.children or ())
    return childrenOf, globals_


def _attachFlat(specs: List[_Spec], data: Optional[bytes] = None,
                parents: Optional[Mapping[int, Optional[int]]] = None,
                schemas: Sequence[Any] = ()
                ) -> List[_Spec]:
    """Infer nesting for a flat spec list.

    An explicit ``parent``/``parentOffset`` field wins.  Otherwise a spec is a
    child of the nearest preceding spec whose byte range contains it.  Ranges
    come from the segment headers (known sizes) or from the next sibling
    offset (unknown sizes).
    """
    if not specs:
        return []
    specs = sorted(specs, key=lambda s: s.offset)
    byOffset = {s.offset: s for s in specs}
    explicit = {}
    for s, p in ((s, (parents or {}).get(s.offset)) for s in specs):
        if p is not None and p in byOffset:
            explicit[s.offset] = p

    childrenOf, globals_ = _validChildIds(schemas)

    def endsAt(spec: _Spec, nextOffset: Optional[int]) -> int:
        if spec.declaredEnd is not None:
            return spec.declaredEnd
        eid = spec.id
        size = None
        if data is not None:
            try:
                eid, idBytes, width, size, raw = _readHeaderAt(data, spec.offset)
            except Exception:
                size = None
            if size is not None:
                return spec.offset + len(idBytes) + width + size
        if nextOffset is None:
            return spec.offset
        if not childrenOf or eid is None:
            return nextOffset
        allowed = childrenOf.get(eid, set()) | globals_
        pos = nextOffset
        # Walk following specs while they are valid children.
        for other in specs:
            if other.offset < nextOffset:
                continue
            if other.id not in allowed and other.offset != nextOffset:
                return other.offset
            pos = endsAt(other, _followingOffset(other))
        return pos

    def _followingOffset(spec: _Spec) -> Optional[int]:
        i = specs.index(spec)
        return specs[i + 1].offset if i + 1 < len(specs) else None

    # Compute provisional ends.
    ends = {}
    for i, spec in enumerate(specs):
        nxt = specs[i + 1].offset if i + 1 < len(specs) else None
        ends[spec.offset] = endsAt(spec, nxt)

    inferred = {}
    for spec in specs:
        if spec.offset in explicit:
            continue
        best = None
        for other in specs:
            if other.offset >= spec.offset:
                break
            end = ends.get(other.offset)
            if end is None or end <= spec.offset:
                continue
            if best is None or other.offset > best.offset:
                best = other
        if best is not None:
            inferred[spec.offset] = best.offset

    # Attach in order; a spec may already have explicit nested children.
    nestedAtoms = set()
    def collect(s):
        found = set()
        for raw in s.children:
            try:
                cs = _specFrom(raw)
            except Exception:
                continue
            found.add(cs.offset)
            found |= collect(cs)
        return found
    for s in specs:
        nestedAtoms |= collect(s)

    roots = []
    for spec in specs:
        if spec.offset in nestedAtoms:
            continue
        parentOffset = explicit.get(spec.offset, inferred.get(spec.offset))
        if parentOffset is None or parentOffset not in byOffset:
            roots.append(spec)
            continue
        parent = byOffset[parentOffset]
        # Only attach if not already present as an explicit child.
        if any(_specFrom(raw).offset == spec.offset for raw in parent.children
               if not isinstance(raw, (int, str))):
            continue
        parent.children.append(spec)
        parent.children.sort(key=lambda raw: _specFrom(raw).offset)
    return sorted(roots, key=lambda s: s.offset)


def _rootSpecs(tree: Any, data: Optional[bytes] = None,
               schemas: Sequence[Any] = ()) -> List[_Spec]:
    if isinstance(tree, core.Document):
        return [_specFrom(el) for el in tree]
    if isinstance(tree, core.Element):
        return [_specFrom(tree)]
    if isinstance(tree, Mapping):
        for key in ('roots', 'rootElements'):
            if key in tree:
                return [_specFrom(v) for v in tree[key]]
        entries = []
        parentOf = {}
        for key, value in tree.items():
            if isinstance(key, str) and key in ('children', 'elements', 'tree',
                                                'volume', 'version', 'docType'):
                continue
            spec = _specFrom(value, key if isinstance(key, int) else None)
            if isinstance(key, int) and isinstance(value, (list, tuple)):
                spec.children = list(value)
            entries.append(spec)
            p = _attr(value, ('parent', 'parentOffset', 'parent_offset',
                              'parentHeaderOffset'))
            if p is not None:
                parentOf[spec.offset] = _intOrNone(p)
        byOffset = {spec.offset: spec for spec in entries}
        for spec in entries:
            spec.children = [byOffset[c] if isinstance(c, int) and c in byOffset
                             else c for c in spec.children]
        return _attachFlat(entries, data, parentOf, schemas)
    rootsAttr = getattr(tree, 'roots', None)
    if rootsAttr is not None and not isinstance(tree, (Mapping, list, tuple)):
        try:
            return [_specFrom(v) for v in rootsAttr]
        except TypeError:
            pass
    if isinstance(tree, (list, tuple)):
        specs = [_specFrom(v) for v in tree]
        childOffsets = set()
        def mark(spec):
            for raw in spec.children:
                cs = _specFrom(raw)
                childOffsets.add(cs.offset)
                mark(cs)
        for spec in specs:
            mark(spec)
        return sorted((s for s in specs if s.offset not in childOffsets),
                      key=lambda s: s.offset)
    raise TypeError('Unsupported original-volume tree: %r' % type(tree))


def _readHeaderAt(data: bytes, offset: int) -> Tuple[int, bytes, int, Optional[int], bytes]:
    stream = io.BytesIO(data)
    stream.seek(offset)
    eid, idLen = decoding.readElementID(stream)
    idBytes = data[offset:offset + idLen]
    stream.seek(offset + idLen)
    size, width = decoding.readElementSize(stream)
    rawSize = data[offset + idLen:offset + idLen + width]
    return eid, idBytes, width, size, rawSize


def _buildTree(data: bytes, tree: Any, oldSchema: core.Schema,
               newSchema: core.Schema) -> Tuple[List[_Node], List[_Node], List[_Node]]:
    specs = _rootSpecs(tree, data, (oldSchema, newSchema))
    allNodes: List[_Node] = []
    seen = set()

    def schemaMaster(eid):
        found = False
        for schema in (oldSchema, newSchema):
            cls = schema.elements.get(eid)
            if cls is not None:
                found = True
                if issubclass(cls, core.MasterElement):
                    return True
        return False if found else None

    def build(spec: _Spec, parent: Optional[_Node]) -> _Node:
        if spec.offset in seen:
            raise ValueError('Duplicate original header offset: %d' % spec.offset)
        seen.add(spec.offset)
        if spec.offset < 0 or spec.offset >= len(data):
            raise ValueError('Original header offset %d is outside the segment'
                             % spec.offset)
        eid, idBytes, width, size, rawSize = _readHeaderAt(data, spec.offset)
        if spec.id is not None and eid != spec.id:
            raise ValueError('Tree ID 0x%X at offset %d does not match segment '
                             'header ID 0x%X' % (spec.id, spec.offset, eid))
        payloadStart = spec.offset + len(idBytes) + width
        declaredChildren = [_specFrom(v) for v in spec.children]
        sm = schemaMaster(eid)
        isMaster = bool(declaredChildren) or sm is True
        if size is None:
            if not isMaster:
                raise ValueError('Unknown-size non-master at offset %d' % spec.offset)
            if spec.declaredEnd is not None:
                payloadEnd = spec.declaredEnd
            else:
                # The next sibling, or parent end, will correct this later.
                payloadEnd = payloadStart
        else:
            payloadEnd = payloadStart + size

        rawPayload = b''
        if not isMaster:
            rawPayload = data[payloadStart:payloadEnd]
        elif not declaredChildren:
            # Opaque master: preserve its payload as an uninterpreted blob.
            rawPayload = data[payloadStart:payloadEnd]

        node = _Node(originalOffset=spec.offset, id=eid,
                     name=spec.name or ('0x%X' % eid),
                     idBytes=idBytes, rawSizeBytes=rawSize,
                     originalSizeWidth=width, originalSizeValue=size,
                     payloadStart=payloadStart, payloadEnd=payloadEnd,
                     payloadSize=payloadEnd - payloadStart,
                     isMaster=isMaster, parent=parent,
                     rawPayload=rawPayload)
        allNodes.append(node)
        for chSpec in declaredChildren:
            node.children.append(build(chSpec, node))

        if size is None:
            childEnd = max((ch.payloadEnd for ch in node.children), default=payloadStart)
            if spec.declaredEnd is not None:
                payloadEnd = spec.declaredEnd
            elif childEnd > payloadStart:
                payloadEnd = childEnd
            elif not node.children:
                # Unknown zero-child master: leave the payload as whatever the
                # tree declared, or empty.
                payloadEnd = payloadStart
            node.payloadEnd = payloadEnd
            node.payloadSize = payloadEnd - payloadStart
        elif spec.declaredEnd is not None and spec.declaredEnd != payloadEnd:
            # The header is authoritative; do not silently move an approved end.
            raise ValueError('Tree end %d at offset %d disagrees with header end %d'
                             % (spec.declaredEnd, spec.offset, payloadEnd))
        return node

    roots = [build(spec, None) for spec in specs]

    def finish(node: _Node, ancestors: Tuple[int, ...]):
        node.path = ancestors + (node.originalOffset,)
        for ch in node.children:
            finish(ch, node.path)
    for root in roots:
        finish(root, ())

    # Infer unknown-size ends from following siblings when no end was declared.
    def inferEnds(node: _Node, limit: int):
        for i, ch in enumerate(node.children):
            if ch.originalSizeValue is None and ch.payloadEnd == ch.payloadStart:
                nextStart = node.children[i + 1].originalOffset if i + 1 < len(node.children) else limit
                if nextStart > ch.payloadStart:
                    ch.payloadEnd = nextStart
                    ch.payloadSize = ch.payloadEnd - ch.payloadStart
                    if ch.isMaster and not ch.children:
                        ch.rawPayload = data[ch.payloadStart:ch.payloadEnd]
            inferEnds(ch, ch.payloadEnd)
    for root in roots:
        inferEnds(root, len(data))
    allNodes.sort(key=lambda n: n.originalOffset)
    return allNodes, roots, allNodes


# ==============================================================================
# Permission table
# ==============================================================================

@dataclass(frozen=True)
class _ContainerPerm:
    key: Any
    widths: Tuple[int, ...]
    allowUnknown: bool
    rewriteCost: int = 0
    widthCosts: Mapping[int, int] = field(default_factory=dict)
    unknownSpecified: bool = False
    allowExplicit: bool = True
    explicitSpecified: bool = False

    def costFor(self, width: int) -> int:
        return int(self.widthCosts.get(width, self.rewriteCost))


@dataclass(frozen=True)
class _Window:
    start: int
    end: int
    cost: int
    containerKey: Any = None
    label: str = ''

    @property
    def length(self) -> int:
        return self.end - self.start


def _permKey(key: Any) -> str:
    if key is None:
        return ''
    if isinstance(key, (tuple, list)):
        return '/'.join(str(x) for x in key)
    return str(key).strip().lower()


class PermissionTable:
    """Normalized write-permission table.

    Construct from a mapping or sequence.  The top level recognizes:

    ``containers``
        Mapping/list of container permissions.  Each may specify ``offset``,
        ``path``, ``name``, or ``id`` (the mapping key is also used), plus
        ``widths`` (allowed size-field widths; include ``None`` or ``-1`` to
        allow unknown length), ``unknown`` (allow unknown length), ``cost``
        (flat rewrite fee), and ``costs`` (per-width rewrite fees).

    ``windows``
        Sequence of Void windows.  Each may specify ``start``/``end`` (or
        ``range``), ``cost``, and ``container``/``parent``.  Tuples of
        ``(start, end[, cost[, container]])`` are accepted.

    ``volumeEnd``
        Locked end address of the whole volume.

    ``locks``
        Mapping of original header offset to required new address, or an
        iterable of offsets to lock at their original address.
    """

    _TOP_KEYS = {
        'containers': 'containers', 'container': 'containers',
        'lengths': 'containers', 'lengthpermissions': 'containers',
        'containerpermissions': 'containers',
        'windows': 'windows', 'voidwindows': 'windows', 'ranges': 'windows',
        'addresses': 'windows', 'voidranges': 'windows',
        'paddingwindows': 'windows',
        'volumeend': 'volumeEnd', 'end': 'volumeEnd', 'totalsize': 'volumeEnd',
        'totallength': 'volumeEnd', 'endaddress': 'volumeEnd',
        'locks': 'locks', 'locked': 'locks', 'lockedoffsets': 'locks',
        'lockedstarts': 'locks', 'issuedstarts': 'locks',
        'blockstarts': 'locks', 'pins': 'locks',
        'baseaddress': 'baseAddress', 'addressbase': 'baseAddress',
        'origin': 'baseAddress', 'addressorigin': 'baseAddress',
    }

    def __init__(self, table: Any = None, **kwargs):
        self.containers: Dict[str, _ContainerPerm] = {}
        self.windows: List[_Window] = []
        self.volumeEnd: Optional[int] = None
        self.locks: Dict[str, int] = {}
        self.baseAddress: int = 0

        if isinstance(table, PermissionTable):
            self.containers = dict(table.containers)
            self.windows = list(table.windows)
            self.volumeEnd = table.volumeEnd
            self.locks = dict(table.locks)
            self.baseAddress = table.baseAddress
        elif table is not None:
            if isinstance(table, Mapping):
                self._parseMapping(table)
            elif isinstance(table, (list, tuple)):
                self._parseSequence(table)
            else:
                raise TypeError('Unsupported permission table: %r' % type(table))
        if kwargs:
            self._parseMapping(kwargs)
        self.windows.sort(key=lambda w: (w.start, w.end, w.cost))

    @staticmethod
    def _norm(key: Any) -> str:
        return ''.join(c for c in str(key).lower() if c.isalnum())

    def _pick(self, mapping: Mapping, names: Sequence[str]):
        norm = {self._norm(k): k for k in mapping}
        for name in names:
            key = norm.get(self._norm(name))
            if key is not None:
                return mapping[key]
        return None

    def _parseMapping(self, table: Mapping):
        consumed = set()
        norm = {self._norm(k): k for k in table}
        for rawKey in table:
            role = self._TOP_KEYS.get(self._norm(rawKey))
            if role is None:
                continue
            consumed.add(rawKey)
            value = table[rawKey]
            if role == 'containers':
                self._parseContainers(value)
            elif role == 'windows':
                self._parseWindows(value)
            elif role == 'volumeEnd':
                self.volumeEnd = _asInt(value, 'volume end')
            elif role == 'locks':
                self._parseLocks(value)
            elif role == 'baseAddress':
                self.baseAddress = _asInt(value, 'base address')
        extras = {k: v for k, v in table.items() if k not in consumed}
        if extras and not self.containers and not self.windows and not self.locks:
            # A bare container map (selector -> permission).
            self._parseContainers(extras)

    def _parseSequence(self, table: Sequence):
        containers = []
        windows = []
        for item in table:
            if isinstance(item, Mapping):
                kind = self._norm(item.get('kind') or item.get('type') or '')
                isWindow = kind in ('window', 'void', 'voidwindow', 'padding')
                if not isWindow:
                    hasStart = self._pick(item, ('start', 'begin', 'from')) is not None
                    hasEnd = self._pick(item, ('end', 'stop', 'to', 'range')) is not None
                    looksContainer = self._pick(item, ('widths', 'lengthWidths',
                                                       'sizeWidths', 'unknown',
                                                       'allowUnknown', 'rewriteCost')) is not None
                    isWindow = hasStart and hasEnd and not looksContainer
                (windows if isWindow else containers).append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                windows.append(item)
            else:
                raise TypeError('Invalid permission entry %r' % (item,))
        if containers:
            self._parseContainers(containers)
        if windows:
            self._parseWindows(windows)

    def _parseContainers(self, value: Any):
        if isinstance(value, Mapping):
            for key, rec in value.items():
                if isinstance(rec, Mapping):
                    self._addContainer(key, rec)
                elif isinstance(rec, (list, tuple, set, frozenset)):
                    self._addContainer(key, {'widths': list(rec)})
                else:
                    self._addContainer(key, {'widths': [rec]})
        elif isinstance(value, (list, tuple)):
            for rec in value:
                if not isinstance(rec, Mapping):
                    raise TypeError('Invalid container permission %r' % (rec,))
                key = self._pick(rec, ('offset', 'path', 'name', 'id', 'element'))
                if key is None:
                    raise ValueError('Container permission missing selector: %r' % (rec,))
                self._addContainer(key, rec)
        else:
            raise TypeError('Invalid containers table %r' % (value,))

    def _addContainer(self, key: Any, rec: Mapping):
        rawWidths = self._pick(rec, ('widths', 'lengthWidths', 'length_widths',
                                     'sizeWidths', 'size_widths', 'sizeLengths',
                                     'size_lengths', 'lengths', 'sizes',
                                     'allowedWidths', 'allowed_widths',
                                     'permittedWidths', 'permitted_widths',
                                     'lengthFieldWidths', 'length_field_widths',
                                     'allowedLengthWidths'))
        unknownRaw = self._pick(rec, ('unknown', 'allowUnknown',
                                      'allow_unknown', 'allowUnknownWidth',
                                      'allow_unknown_width', 'infinite',
                                      'canBeUnknown', 'unknownAllowed'))
        allowUnknown = _asBool(unknownRaw, False)
        unknownSpecified = unknownRaw is not None
        widthCosts: Dict[int, int] = {}
        widths: List[int] = []

        if rawWidths is None:
            pass
        elif isinstance(rawWidths, Mapping):
            for w, c in rawWidths.items():
                if w is None or str(w).strip().lower() in ('none', 'unknown', 'infinite', '*'):
                    allowUnknown = True
                else:
                    wi = _asInt(w, 'size width')
                    widths.append(wi)
                    widthCosts[wi] = _asInt(c, 'width cost')
        elif isinstance(rawWidths, (list, tuple, set, frozenset)):
            for w in rawWidths:
                if w is None or (isinstance(w, str) and
                                 w.strip().lower() in ('none', 'unknown', 'infinite', '*')):
                    allowUnknown = True
                else:
                    wi = _asInt(w, 'size width')
                    if wi < 1 or wi > 8:
                        raise ValueError('Invalid EBML size width %r' % (w,))
                    widths.append(wi)
        else:
            wi = _asInt(rawWidths, 'size width')
            if wi < 1 or wi > 8:
                raise ValueError('Invalid EBML size width %r' % (rawWidths,))
            widths.append(wi)

        explicitRaw = self._pick(rec, ('explicit', 'allowExplicit', 'known',
                                       'allowKnown', 'allowExplicitLength'))
        allowExplicit = _asBool(explicitRaw, True)
        explicitSpecified = explicitRaw is not None

        cost = self._pick(rec, ('cost', 'rewriteCost', 'rewrite_cost',
                                'lengthCost', 'length_cost', 'lengthFieldCost',
                                'length_field_cost', 'fee', 'rewriteFee',
                                'rewrite_fee', 'price'))
        cost = 0 if cost is None else _asInt(cost, 'rewrite cost')
        costsRaw = self._pick(rec, ('costs', 'widthCosts', 'width_costs',
                                    'costByWidth', 'cost_by_width',
                                    'perWidthCosts'))
        if isinstance(costsRaw, Mapping):
            for w, c in costsRaw.items():
                widthCosts[_asInt(w, 'size width')] = _asInt(c, 'width cost')

        widths = tuple(dict.fromkeys(widths))
        self.containers[_permKey(key)] = _ContainerPerm(
            key=key, widths=widths, allowUnknown=allowUnknown,
            rewriteCost=cost, widthCosts=widthCosts,
            unknownSpecified=unknownSpecified, allowExplicit=allowExplicit,
            explicitSpecified=explicitSpecified)

    def _parseWindows(self, value: Any):
        if isinstance(value, Mapping):
            if any(self._norm(k) in ('start', 'begin', 'from', 'range', 'end',
                                     'stop', 'to') for k in value):
                value = [value]
            else:
                value = [dict(v, label=k) if isinstance(v, Mapping) else v
                         for k, v in value.items()]
        if not isinstance(value, (list, tuple)):
            raise TypeError('Invalid Void windows %r' % (value,))
        for rec in value:
            if isinstance(rec, (list, tuple)):
                vals = list(rec)
                if len(vals) < 2:
                    raise TypeError('Invalid Void window %r' % (rec,))
                start = _asInt(vals[0], 'window start')
                end = _asInt(vals[1], 'window end')
                cost = _asInt(vals[2], 'window cost') if len(vals) > 2 else 0
                container = vals[3] if len(vals) > 3 else None
                label = ''
            elif isinstance(rec, Mapping):
                rng = self._pick(rec, ('range', 'interval', 'addressRange'))
                if isinstance(rng, (list, tuple)) and len(rng) >= 2:
                    start = _asInt(rng[0], 'window start')
                    end = _asInt(rng[1], 'window end')
                else:
                    startRaw = self._pick(rec, ('start', 'begin', 'from', 'first',
                                                'low', 'address', 'offset'))
                    endRaw = self._pick(rec, ('end', 'stop', 'to', 'last', 'high',
                                              'endExclusive', 'stopExclusive'))
                    lengthRaw = self._pick(rec, ('length', 'size', 'span', 'width',
                                                 'windowLength', 'window_length'))
                    if startRaw is None:
                        raise TypeError('Void window missing start: %r' % (rec,))
                    start = _asInt(startRaw, 'window start')
                    if endRaw is not None:
                        end = _asInt(endRaw, 'window end')
                        if _asBool(self._pick(rec, ('inclusiveEnd', 'endInclusive')), False):
                            end += 1
                    elif lengthRaw is not None:
                        end = start + _asInt(lengthRaw, 'window length')
                    else:
                        raise TypeError('Void window missing end/length: %r' % (rec,))
                costRaw = self._pick(rec, ('cost', 'fee', 'openCost',
                                           'open_cost', 'openingFee',
                                           'opening_fee', 'openFee', 'open_fee',
                                           'price', 'windowCost', 'window_cost'))
                cost = _asInt(costRaw, 'window cost') if costRaw is not None else 0
                container = self._pick(rec, ('container', 'parent', 'containerOffset',
                                             'container_offset', 'parentOffset',
                                             'parent_offset', 'element', 'owner'))
                label = self._pick(rec, ('label', 'name', 'id', 'tag')) or ''
            else:
                raise TypeError('Invalid Void window %r' % (rec,))
            if start < 0 or end < start:
                raise ValueError('Invalid Void window [%d, %d)' % (start, end))
            self.windows.append(_Window(start, end, cost, container, str(label)))

    def _parseLocks(self, value: Any):
        if isinstance(value, Mapping):
            for key, target in value.items():
                if isinstance(target, Mapping):
                    target = self._pick(target, ('offset', 'address', 'target',
                                                 'newOffset', 'lockedOffset'))
                self.locks[_permKey(key)] = _asInt(target, 'locked address')
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, Mapping):
                    key = self._pick(item, ('offset', 'originalOffset', 'element',
                                            'name', 'id', 'path'))
                    target = self._pick(item, ('address', 'target', 'newOffset',
                                               'lockedOffset'))
                    if key is None:
                        raise TypeError('Lock entry missing element: %r' % (item,))
                    self.locks[_permKey(key)] = _asInt(
                        target if target is not None else key, 'locked address')
                else:
                    self.locks[_permKey(item)] = _asInt(item, 'locked address')
        else:
            raise TypeError('Invalid locks %r' % (value,))

    def shiftedTo(self, baseAddress: int) -> 'PermissionTable':
        """Return a copy with absolute addresses translated to volume-relative.

        ``baseAddress`` is the volume's start address on the medium.  Windows,
        locks, the volume end, and the stored base address are all reduced by
        it; container permissions are unchanged.
        """
        if baseAddress == 0:
            return self
        result = PermissionTable(self)
        result.windows = [_Window(w.start - baseAddress, w.end - baseAddress,
                                  w.cost, w.containerKey, w.label)
                          for w in self.windows]
        result.locks = {k: v - baseAddress for k, v in self.locks.items()}
        if self.volumeEnd is not None:
            result.volumeEnd = self.volumeEnd - baseAddress
        result.baseAddress = 0
        return result

    # -- lookups ---------------------------------------------------------

    def containerFor(self, node: '_Node') -> Optional[_ContainerPerm]:
        for key in (_permKey(node.originalOffset), _permKey(node.name),
                    _permKey(node.id), _permKey(node.path)):
            if key in self.containers:
                return self.containers[key]
        return None

    def windowsFor(self, node: Optional['_Node']) -> List[_Window]:
        if node is None:
            return [w for w in self.windows
                    if w.containerKey is None
                    or _permKey(w.containerKey) in ('', 'root', 'document', 'none')]
        keys = {_permKey(node.originalOffset), _permKey(node.name),
                _permKey(node.id), _permKey(node.path)}
        return [w for w in self.windows if _permKey(w.containerKey) in keys]

    def lockFor(self, node: '_Node') -> Optional[int]:
        for key in (_permKey(node.originalOffset), _permKey(node.name),
                    _permKey(node.id), _permKey(node.path)):
            if key in self.locks:
                return self.locks[key]
        return None

    def __repr__(self):
        return '<PermissionTable %d containers, %d windows>' % (
            len(self.containers), len(self.windows))


# ==============================================================================
# Layout planning
# ==============================================================================

@dataclass
class _Marker:
    """A position-sensitive point in emission order.

    ``basePos`` is the zero-gap offset computed with the chosen size widths.
    ``kind`` is ``'slot'`` (a place a Void may be inserted), ``'event'`` (a
    locked element start or the volume end), or ``'final'``.
    """
    kind: str
    basePos: int
    key: Any = None
    owner: Optional[_Node] = None
    target: Optional[int] = None
    node: Optional[_Node] = None




def _nodeItems(node: _Node, data: bytes) -> List[Tuple[str, Any]]:
    """Items in a master payload: business children plus preserved raw bytes."""
    if not node.isMaster:
        return []
    if not node.children:
        if node.rawPayload:
            return [('raw', node.rawPayload)]
        return []
    items: List[Tuple[str, Any]] = []
    pos = node.payloadStart
    for child in sorted(node.children, key=lambda c: c.originalOffset):
        if child.originalOffset < pos:
            raise ValueError('Overlapping child at original offset %d'
                             % child.originalOffset)
        if child.originalOffset > pos:
            items.append(('raw', data[pos:child.originalOffset]))
        items.append(('node', child))
        pos = child.payloadEnd
    if node.payloadEnd > pos:
        items.append(('raw', data[pos:node.payloadEnd]))
    return items


def _documentItems(data: bytes, roots: Sequence[_Node]) -> List[Tuple[str, Any]]:
    items: List[Tuple[str, Any]] = []
    pos = 0
    for root in sorted(roots, key=lambda n: n.originalOffset):
        if root.originalOffset < pos:
            raise ValueError('Overlapping root at original offset %d'
                             % root.originalOffset)
        if root.originalOffset > pos:
            items.append(('raw', data[pos:root.originalOffset]))
        items.append(('node', root))
        pos = root.payloadEnd
    if pos < len(data):
        items.append(('raw', data[pos:]))
    return items


def _widthCandidates(node: _Node, perms: PermissionTable) -> List[Tuple[int, bool]]:
    """Allowed ``(sizeWidth, unknown)`` descriptors for one container.

    ``widths`` in the permission table is the complete permitted set.  An
    unknown descriptor may be retained only if ``unknown`` is allowed.  Turning
    it into an explicit descriptor of the same width additionally requires
    ``explicit`` permission (the default).  Scalars never change.
    """
    if not node.isMaster:
        return [(node.originalSizeWidth, node.originalSizeValue is None)]

    perm = perms.containerFor(node)
    originalUnknown = node.originalSizeValue is None
    if perm is None:
        return [(node.originalSizeWidth, originalUnknown)]

    widths = list(perm.widths) or [node.originalSizeWidth]

    # Retaining an unknown descriptor is the default for a container that
    # already had one; switching it to explicit is opt-in.  Explicit
    # containers may keep/change explicit widths by default.
    canUnknown = (perm.allowUnknown if perm.unknownSpecified
                  else originalUnknown)
    canExplicit = (perm.allowExplicit if (not originalUnknown
                                          or perm.explicitSpecified)
                   else False)

    candidates: List[Tuple[int, bool]] = []
    for w in widths:
        if canExplicit:
            candidates.append((w, False))
    if canUnknown:
        unknownWidth = (node.originalSizeWidth if node.originalSizeWidth in widths
                        else widths[0])
        candidates.append((unknownWidth, True))
    if not candidates:
        if originalUnknown:
            # Unknown original with neither retention nor explicit permission:
            # no legal encoding for this container.
            return []
        candidates = [(node.originalSizeWidth, False)]

    def key(c):
        w, unk = c
        changed = (w != node.originalSizeWidth or unk != originalUnknown)
        return (changed, perm.costFor(w) if changed else 0, unk, w)

    return list(dict.fromkeys(sorted(candidates, key=key)))


def _makeMarkers(data: bytes, roots: Sequence[_Node],
                 itemsByOwner: Dict[Any, List[Tuple[str, Any]]],
                 widths: Dict[int, Tuple[int, bool]],
                 lockTargets: Mapping[int, int],
                 volumeEnd: int) -> Tuple[List[_Marker], int]:
    """Flatten the emission order into slots and locked-position events."""
    markers: List[_Marker] = []

    def walk(owner: Optional[_Node], ownerKey: Any, items,
             pos: int) -> int:
        for i, (kind, value) in enumerate(items):
            markers.append(_Marker('slot', pos, key=(ownerKey, i), owner=owner))
            if kind == 'raw':
                pos += len(value)
                continue
            node: _Node = value
            if node.originalOffset in lockTargets:
                markers.append(_Marker('event', pos,
                                       target=lockTargets[node.originalOffset],
                                       node=node))
            width, _unknown = widths[node.originalOffset]
            payloadStart = pos + len(node.idBytes) + width
            if node.isMaster:
                pos = walk(node, node.originalOffset,
                           itemsByOwner[node.originalOffset], payloadStart)
            else:
                pos = payloadStart + node.payloadSize
        markers.append(_Marker('slot', pos, key=(ownerKey, len(items)),
                               owner=owner))
        return pos

    baseEnd = walk(None, 'document', itemsByOwner['document'], 0)
    markers.append(_Marker('final', baseEnd, target=volumeEnd))
    return markers, baseEnd


def _windowMatches(window: _Window, owner: Optional[_Node]) -> bool:
    if window.containerKey is None:
        return True
    key = _permKey(window.containerKey)
    if owner is None:
        return key in ('', 'root', 'document', 'none')
    return key in {_permKey(owner.originalOffset), _permKey(owner.name),
                   _permKey(owner.id), _permKey(owner.path)}


def _gapPlan(markers: Sequence[_Marker], perms: PermissionTable,
             baseEnd: int, volumeEnd: int,
             keepPerState: int = 24, maxPlans: int = 512):
    """Enumerate exact, address-feasible gap plans, cheapest first.

    State is ``(cumulative gap bytes D, opened window ids)``.  An event marker
    requires ``D == target - basePos`` exactly.  At a slot, a Void may start at
    ``basePos + D`` and must lie entirely inside one permitted window.
    """
    if volumeEnd < baseEnd:
        return []

    def finalKeys(gaps):
        return tuple(sorted(((repr(k), v[0], v[1].start, v[1].end)
                             for k, v in gaps.items())))

    # Required cumulative shift at the next event/final marker.
    nextRequired = [None] * len(markers)
    upcoming = None
    for i in range(len(markers) - 1, -1, -1):
        m = markers[i]
        if m.kind == 'event':
            upcoming = m.target - m.basePos
        elif m.kind == 'final':
            upcoming = volumeEnd - baseEnd
        nextRequired[i] = upcoming

    states: Dict[Tuple[int, frozenset], List[Tuple[Dict, int]]] = {
        (0, frozenset()): [({}, 0)]}

    for mi, m in enumerate(markers):
        if m.kind == 'event':
            want = m.target - m.basePos
            nextStates = {}
            for (D, used), plans in states.items():
                if D == want:
                    nextStates[(D, used)] = plans
            states = nextStates
            if not states:
                return []
            continue

        if m.kind == 'final':
            want = volumeEnd - baseEnd
            nextStates = {}
            for (D, used), plans in states.items():
                if D == want:
                    nextStates[(D, used)] = plans
            states = nextStates
            if not states:
                return []
            continue

        # slot
        nextStates = {}
        for (D, used), plans in states.items():
            actualStart = m.basePos + D
            options: List[Tuple[int, Optional[_Window]]] = [(0, None)]
            for window in perms.windows:
                if not _windowMatches(window, m.owner):
                    continue
                if actualStart < window.start:
                    continue
                capacity = window.end - actualStart
                if capacity < 2:
                    continue
                lengths = set()
                if capacity <= 24:
                    lengths.update(range(2, capacity + 1))
                else:
                    lengths.update((2, 3, 4, capacity - 1, capacity))
                required = nextRequired[mi + 1] if mi + 1 < len(markers) else None
                if required is not None:
                    exact = required - D
                    if 2 <= exact <= capacity:
                        lengths.add(exact)
                lengths = sorted(x for x in lengths if 2 <= x <= capacity)
                for length in lengths:
                    options.append((length, window))

            for length, window in options:
                extra = 0
                newUsed = used
                if length and window is not None and id(window) not in used:
                    extra = window.cost
                    newUsed = used | {id(window)}
                for gaps, cost in plans:
                    ng = gaps
                    if length:
                        ng = dict(gaps)
                        ng[m.key] = (length, window)
                    key = (D + length, newUsed)
                    nextStates.setdefault(key, []).append((ng, cost + extra))

        if not nextStates:
            return []

        # Deduplicate and retain the cheapest distinct plans per state.
        states = {}
        for key, plans in nextStates.items():
            seen = set()
            unique = []
            for gaps, cost in sorted(plans, key=lambda p: p[1]):
                gk = finalKeys(gaps)
                if gk in seen:
                    continue
                seen.add(gk)
                unique.append((gaps, cost))
                if len(unique) >= keepPerState:
                    break
            states[key] = unique

    results = {}
    for (D, used), plans in states.items():
        for gaps, cost in plans:
            gk = finalKeys(gaps)
            if gk not in results or cost < results[gk][1]:
                results[gk] = (gaps, cost)
    return sorted(results.values(), key=lambda p: (p[1], finalKeys(p[0])))[:maxPlans]


# ==============================================================================
# Materialization
# ==============================================================================

def _sizeWidthForVoid(length: int) -> int:
    for width in range(1, 9):
        payload = length - 1 - width
        if 0 <= payload <= (1 << (7 * width)) - 2:
            return width
    raise ValueError('Cannot encode a %d-byte Void' % length)


def _encodeSize(value: Optional[int], width: int) -> bytes:
    if value is None:
        return b'\xff' * width
    if value > (1 << (7 * width)) - 2:
        raise ValueError('Payload size %d does not fit in %d bytes'
                         % (value, width))
    return encoding.encodeSize(value, width)


def _encodeVoid(length: int) -> bytes:
    width = _sizeWidthForVoid(length)
    payload = length - 1 - width
    return b'\xec' + _encodeSize(payload, width) + b'\xff' * payload


def _allPayloadLengths(roots, itemsByOwner, widths, gaps) -> Dict[int, int]:
    result: Dict[int, int] = {}

    def length(n: _Node) -> int:
        if not n.isMaster:
            result[n.originalOffset] = n.payloadSize
            return n.payloadSize
        total = 0
        items = itemsByOwner[n.originalOffset]
        for i, (kind, value) in enumerate(items):
            if (n.originalOffset, i) in gaps:
                total += gaps[(n.originalOffset, i)][0]
            if kind == 'raw':
                total += len(value)
            else:
                child: _Node = value
                cw, _ = widths[child.originalOffset]
                total += len(child.idBytes) + cw + length(child)
        if (n.originalOffset, len(items)) in gaps:
            total += gaps[(n.originalOffset, len(items))][0]
        result[n.originalOffset] = total
        return total

    for root in roots:
        length(root)
    return result


def _materialize(data: bytes, roots, itemsByOwner, widths, gaps,
                 volumeEnd: int, copyPayload: bool = True):
    """Emit the volume.

    With ``copyPayload=False`` leaf payload bytes and raw gaps are zero-filled
    at their exact lengths; framing, headers, and sizes are identical.  This
    is what the layout search validates, so business payload bytes are copied
    only after a plan has been chosen.
    """
    lengths = _allPayloadLengths(roots, itemsByOwner, widths, gaps)
    out = bytearray()
    placements: List[Placement] = []
    elementPlacements: Dict[int, Placement] = {}
    voidSlots: Dict[Tuple[Any, int], Tuple[int, int, _Window]] = {}

    def emitItems(ownerKey, items):
        for i, (kind, value) in enumerate(items):
            gap = gaps.get((ownerKey, i))
            if gap:
                length, window = gap
                start = len(out)
                width = _sizeWidthForVoid(length)
                out.extend(_encodeVoid(length))
                placements.append(Placement('void', start, start + 1 + width,
                                            len(out), None, width, length - 1 - width))
                voidSlots[(ownerKey, i)] = (start, length, window)
            if kind == 'raw':
                out.extend(value if copyPayload else b'\x00' * len(value))
                continue
            node: _Node = value
            width, unknown = widths[node.originalOffset]
            start = len(out)
            out.extend(node.idBytes)
            if node.isMaster:
                payloadLen = lengths[node.originalOffset]
                out.extend(_encodeSize(None if unknown else payloadLen, width))
                payloadStart = len(out)
                placement = Placement('element', start, payloadStart,
                                      payloadStart + payloadLen, node, width,
                                      None if unknown else payloadLen)
                elementPlacements[node.originalOffset] = placement
                placements.append(placement)
                emitItems(node.originalOffset, itemsByOwner[node.originalOffset])
            else:
                out.extend(node.rawSizeBytes)
                payloadStart = len(out)
                if copyPayload:
                    out.extend(node.rawPayload)
                else:
                    out.extend(b'\x00' * node.payloadSize)
                placement = Placement('element', start, payloadStart,
                                      len(out), node, width,
                                      node.originalSizeValue)
                elementPlacements[node.originalOffset] = placement
                placements.append(placement)

        gap = gaps.get((ownerKey, len(items)))
        if gap:
            length, window = gap
            start = len(out)
            width = _sizeWidthForVoid(length)
            out.extend(_encodeVoid(length))
            placements.append(Placement('void', start, start + 1 + width,
                                        len(out), None, width, length - 1 - width))
            voidSlots[(ownerKey, len(items))] = (start, length, window)

    emitItems('document', itemsByOwner['document'])
    if len(out) != volumeEnd:
        raise AssertionError('emitted %d bytes, expected %d' % (len(out), volumeEnd))
    return bytes(out), placements, elementPlacements, voidSlots


# ==============================================================================
# Reader simulation
# ==============================================================================

@dataclass
class _ReadElement:
    eid: int
    offset: int
    headerEnd: int
    payloadEnd: int
    size: Optional[int]
    parent: Optional['_ReadElement']

    @property
    def isVoid(self) -> bool:
        return self.eid == 0xEC


def _readVolume(data: bytes, schema: core.Schema) -> List[_ReadElement]:
    """Crawl the emitted volume as one structure table would.

    A known-size master is bounded by its declared payload size.  An
    unknown-size master consumes subsequent elements while they remain valid
    children; the first non-child ends it.  This is exactly the containment
    behavior that decides whether a nearby Void belongs to a master or its
    parent.
    """
    globals_ = set(schema.globals)
    childrenOf: Dict[int, set] = {}
    for eid, cls in schema.elements.items():
        if issubclass(cls, core.MasterElement):
            childrenOf[eid] = set(cls.children or ())
    rootChildren = set(schema.document.children or ())

    def isChild(parent: Optional[_ReadElement], eid: int) -> bool:
        if eid in globals_:
            return True
        if parent is None:
            return eid in rootChildren
        return eid in childrenOf.get(parent.eid, ())

    result: List[_ReadElement] = []

    def parseAt(pos: int, limit: int,
                parent: Optional[_ReadElement]) -> Optional[_ReadElement]:
        if pos < 0 or pos >= len(data) or pos >= limit:
            return None
        try:
            stream = io.BytesIO(data)
            stream.seek(pos)
            eid, idLen = decoding.readElementID(stream)
            size, width = decoding.readElementSize(stream)
        except Exception:
            return None
        headerEnd = pos + idLen + width
        if headerEnd > len(data):
            return None
        if not isChild(parent, eid):
            return None
        element = _ReadElement(eid=eid, offset=pos, headerEnd=headerEnd,
                               payloadEnd=headerEnd + (size or 0),
                               size=size, parent=parent)
        result.append(element)

        if size is not None:
            end = headerEnd + size
            if end > len(data):
                element.payloadEnd = len(data)
                return element
            childPos = headerEnd
            while childPos < end:
                child = parseAt(childPos, end, element)
                if child is None:
                    break
                childPos = max(child.payloadEnd, childPos + 1)
            element.payloadEnd = end
            return element

        # Unknown size: consume valid children.
        childPos = headerEnd
        end = headerEnd
        while childPos < limit:
            probe = io.BytesIO(data)
            probe.seek(childPos)
            try:
                childId, _ = decoding.readElementID(probe)
            except Exception:
                break
            if not isChild(element, childId):
                break
            child = parseAt(childPos, limit, element)
            if child is None:
                break
            if child.payloadEnd <= childPos:
                break
            childPos = child.payloadEnd
            end = child.payloadEnd
        element.payloadEnd = end
        return element

    pos = 0
    while pos < len(data):
        root = parseAt(pos, len(data), None)
        if root is None:
            # Not a schema root; skip is impossible without framing, so stop.
            break
        if root.payloadEnd <= pos:
            break
        pos = root.payloadEnd
    return result


def _nearestBusiness(readEl: Optional[_ReadElement],
                     originalByOffset: Mapping[int, int]) -> Optional[int]:
    while readEl is not None:
        found = originalByOffset.get(readEl.offset)
        if found is not None:
            return found
        readEl = readEl.parent
    return None


def _parentErrors(readElements: Sequence[_ReadElement],
                  placements: Mapping[int, Placement],
                  nodesByOriginal: Mapping[int, _Node],
                  originalByOffset: Mapping[int, int]):
    """Business elements must keep the same nearest-business parent."""
    byOffset = {el.offset: el for el in readElements}
    errors = []
    for original, placement in placements.items():
        node = nodesByOriginal[original]
        el = byOffset.get(placement.offset)
        expected = node.parent.originalOffset if node.parent else None
        if el is None:
            errors.append({'kind': 'missing', 'originalOffset': original,
                           'offset': placement.offset, 'name': node.name,
                           'id': node.id, 'expectedParent': expected,
                           'actualParent': None, 'actualParentName': None})
            continue
        actual = _nearestBusiness(el.parent, originalByOffset)
        if actual != expected:
            errors.append({'kind': 'parent', 'originalOffset': original,
                           'offset': placement.offset, 'name': node.name,
                           'id': node.id, 'expectedParent': expected,
                           'actualParent': actual,
                           'actualParentName': (nodesByOriginal[actual].name
                                                if actual in nodesByOriginal
                                                else None),
                           'actualParentId': (el.parent.eid
                                              if el.parent is not None else None)})
    errors.sort(key=lambda e: e['offset'])
    return errors


def _orderErrors(readElements, placements, nodesByOriginal,
                 originalByOffset):
    byOffset = {el.offset: el for el in readElements}
    errors = []
    for original, placement in placements.items():
        node = nodesByOriginal[original]
        if not node.children:
            continue
        expected = [c.originalOffset for c in node.children]
        planned = {placements[o].offset: o for o in expected}
        actual = []
        for el in readElements:
            if el.offset in planned and _nearestBusiness(
                    el.parent, originalByOffset) == original:
                actual.append(planned[el.offset])
        if actual != expected:
            mismatch = next((o for o, a in zip(expected, actual) if o != a),
                            (expected[len(actual)] if len(actual) < len(expected)
                             else original))
            errors.append({'kind': 'order', 'originalOffset': original,
                           'name': node.name, 'expectedOrder': expected,
                           'actualOrder': actual,
                           'offset': placements[mismatch].offset
                           if mismatch in placements else placement.offset})
    return errors


def _voidErrors(readElements, placements, nodesByOriginal,
                originalByOffset, voidSlots, gaps):
    byOffset = {el.offset: el for el in readElements}
    errors = []
    for (ownerKey, index), (offset, length, window) in sorted(
            voidSlots.items(), key=lambda kv: kv[1][0]):
        if not gaps.get((ownerKey, index)):
            continue
        el = byOffset.get(offset)
        expected = None if ownerKey == 'document' else ownerKey
        if el is None or el.eid != 0xEC:
            errors.append({'kind': 'void-missing', 'offset': offset,
                           'expectedParent': expected})
            continue
        actual = _nearestBusiness(el.parent, originalByOffset)
        # The Void's direct parent must be the intended container structure.
        parentIsRoot = el.parent is None
        if expected is None:
            if not parentIsRoot:
                errors.append({'kind': 'void-parent', 'offset': offset,
                               'expectedParent': None,
                               'actualParent': actual,
                               'actualParentName': ('Void' if el.parent and
                                                    el.parent.isVoid else
                                                    (nodesByOriginal[actual].name
                                                     if actual in nodesByOriginal
                                                     else None))})
        else:
            expectedPlacement = placements.get(expected)
            if el.parent is None or expectedPlacement is None or \
                    el.parent.offset != expectedPlacement.offset:
                errors.append({'kind': 'void-parent', 'offset': offset,
                               'expectedParent': expected,
                               'actualParent': actual,
                               'actualParentName': ('Void' if el.parent and
                                                    el.parent.isVoid else
                                                    (nodesByOriginal[actual].name
                                                     if actual in nodesByOriginal
                                                     else None)),
                               'actualParentOffset': (el.parent.offset
                                                      if el.parent else None)})
    errors.sort(key=lambda e: e.get('offset', 0))
    return errors


def _locator(nodes, placements, volumesEnd) -> Locator:
    entries = []
    for node in nodes:
        p = placements[node.originalOffset]
        entries.append(LocatorEntry(
            original_offset=node.originalOffset,
            header_start=p.offset,
            header_end=p.headerEnd,
            payload_start=p.headerEnd,
            payload_end=p.payloadEnd,
            element_end=p.payloadEnd,
            id=node.id, name=node.name,
            parent=(node.parent.originalOffset if node.parent else None),
            children=tuple(c.originalOffset for c in node.children),
            size_width=p.sizeWidth, size_value=p.sizeValue,
            path=node.path))
    return Locator(entries, volumeStart=0, volumeEnd=volumesEnd)


def _errorSummary(err, reader, placements=None):
    isVoid = str(err.get('kind', '')).startswith('void')
    actual = err.get('actualParent')
    actualNew = err.get('actualParentOffset')
    if actualNew is None and placements is not None and actual in placements:
        actualNew = placements[actual].offset
    return {'reader': reader,
            'element': err.get('name', 'Void' if isVoid else None),
            'elementId': err.get('id', 0xEC if isVoid else None),
            'originalOffset': err.get('originalOffset'),
            'newOffset': err.get('offset'),
            'expectedParent': err.get('expectedParent'),
            'expectedParentOffset': (placements[err['expectedParent']].offset
                                     if placements is not None
                                     and err.get('expectedParent') in placements
                                     else None),
            'actualParent': actual,
            'actualParentName': err.get('actualParentName'),
            'actualParentOffset': actualNew,
            'kind': err.get('kind')}


def _firstError(errors):
    """Earliest misread by new offset, regardless of error category."""
    flattened = []
    for category in ('parent', 'void', 'order'):
        for err in errors.get(category, ()):
            flattened.append(err)
    if not flattened:
        return None
    return min(flattened, key=lambda e: e.get('offset', 0))


# ==============================================================================
# Public entry point
# ==============================================================================

def _volumeEndValue(perms: PermissionTable, roots, dataLen: int) -> int:
    if perms.volumeEnd is not None:
        return perms.volumeEnd
    return dataLen


def _lockTargets(perms: PermissionTable, nodes, roots) -> Dict[int, int]:
    targets = {}
    for node in nodes:
        target = perms.lockFor(node)
        if target is not None:
            targets[node.originalOffset] = target
    for root in roots:
        targets.setdefault(root.originalOffset, root.originalOffset)
    return targets


def exportVolume(segment: Any,
                 tree: Any,
                 oldSchema: Any = None,
                 newSchema: Any = None,
                 permissions: Any = None,
                 locks: Any = None,
                 *,
                 volumeEnd: Optional[int] = None,
                 baseAddress: Optional[int] = None,
                 maxLayouts: int = 8192,
                 maxGapPlans: int = 256,
                 **aliases) -> ExportResult:
    """Re-archive a sampling volume onto an addressed medium.

    Positional order is ``(segment, tree, oldSchema, newSchema, permissions,
    locks)``.  See the module docstring for accepted input forms.

    Returns success with the emitted ``volume`` bytes and a `Locator`, or
    failure with ``failure='address'`` / ``'layout'`` and diagnostics.  No
    volume is emitted on failure.
    """
    if oldSchema is None:
        oldSchema = aliases.pop('old', None)
    if oldSchema is None:
        oldSchema = aliases.pop('old_schema', None)
    if newSchema is None:
        newSchema = aliases.pop('new', None)
    if newSchema is None:
        newSchema = aliases.pop('new_schema', None)
    if permissions is None:
        permissions = aliases.pop('permissionsTable', None)
    if permissions is None:
        permissions = aliases.pop('permissionTable', None)
    if permissions is None:
        permissions = aliases.pop('permission_table', None)
    if permissions is None:
        permissions = aliases.pop('writePermissions', None)
    if locks is None:
        locks = aliases.pop('locked', None)
    if locks is None:
        locks = aliases.pop('lockedOffsets', None)
    if volumeEnd is None:
        volumeEnd = aliases.pop('end', None)
    if baseAddress is None:
        baseAddress = aliases.pop('origin', None)
    if aliases:
        raise TypeError('Unexpected keyword argument(s): %s'
                        % ', '.join(sorted(aliases)))
    if oldSchema is None:
        oldSchema = getattr(tree, 'schema', None)
    if newSchema is None:
        newSchema = oldSchema
    if oldSchema is None or newSchema is None:
        raise TypeError('Both old and new structure tables are required')

    old = _asSchema(oldSchema, 'old')
    new = _asSchema(newSchema, 'new')
    data = _asBytes(segment)
    perms = permissions if isinstance(permissions, PermissionTable) \
        else PermissionTable(permissions)

    if baseAddress is None:
        baseAddress = perms.baseAddress

    if locks:
        if isinstance(locks, Mapping):
            for key, target in locks.items():
                perms.locks[_permKey(key)] = _asInt(target, 'locked address')
        else:
            for item in locks:
                perms.locks[_permKey(item)] = _asInt(item, 'locked address')

    if baseAddress:
        # Translate absolute medium addresses to volume-relative ones after
        # any caller-supplied locks have been merged.
        perms = perms.shiftedTo(baseAddress)

    nodes, roots, _ = _buildTree(data, tree, old, new)
    nodesByOriginal = {n.originalOffset: n for n in nodes}
    if not roots:
        raise ValueError('Original tree contains no root elements')

    if volumeEnd is None:
        volumeEnd = _volumeEndValue(perms, roots, len(data))
    elif baseAddress:
        volumeEnd -= baseAddress
    if volumeEnd < 0:
        raise ValueError('Invalid volume end %d' % volumeEnd)

    itemsByOwner = {'document': _documentItems(data, roots)}
    for node in nodes:
        if node.isMaster:
            itemsByOwner[node.originalOffset] = _nodeItems(node, data)

    lockTargets = _lockTargets(perms, nodes, roots)
    masters = [n for n in nodes if n.isMaster]
    candidates = [_widthCandidates(n, perms) for n in masters]

    import itertools
    combos = list(itertools.product(*candidates)) if candidates else [()]
    masterByOffset = {n.originalOffset: n for n in masters}

    def widthCost(widths):
        total = 0
        for node in masters:
            w, unk = widths[node.originalOffset]
            if w != node.originalSizeWidth or unk != (node.originalSizeValue is None):
                perm = perms.containerFor(node)
                total += perm.costFor(w) if perm else 0
        return total

    def widthChange(widths):
        return sum(1 for n in masters
                   if widths[n.originalOffset] != (n.originalSizeWidth,
                                                   n.originalSizeValue is None))

    ordered = []
    for combo in combos:
        widths = {n.originalOffset: c for n, c in zip(masters, combo)}
        for node in nodes:
            widths.setdefault(node.originalOffset,
                              (node.originalSizeWidth,
                               node.originalSizeValue is None))
        ordered.append((widthCost(widths), widthChange(widths), combo, widths))
    ordered.sort(key=lambda x: (x[0], x[1], repr(x[2])))

    addressFallback = None
    layoutFallback = None
    bestValid = None
    tested = 0

    for rewriteCost, _changes, combo, widths in ordered:
        if tested >= maxLayouts:
            break
        if bestValid is not None and rewriteCost > bestValid[0]:
            # Width combos are sorted by rewrite cost and Void costs are
            # non-negative, so no later combo can beat the incumbent.
            break
        try:
            markers, baseEnd = _makeMarkers(data, roots, itemsByOwner,
                                             widths, lockTargets, volumeEnd)
        except ValueError:
            continue
        plans = _gapPlan(markers, perms, baseEnd, volumeEnd,
                         maxPlans=maxGapPlans)
        if not plans:
            continue

        for gaps, voidCost in plans:
            tested += 1
            if tested > maxLayouts:
                break
            totalCost = rewriteCost + voidCost
            if bestValid is not None and totalCost >= bestValid[0]:
                # Plans are sorted by Void cost within this width combo.
                break
            try:
                volume, placements, elementPlacements, voidSlots = _materialize(
                    data, roots, itemsByOwner, widths, gaps, volumeEnd,
                    copyPayload=False)
            except (ValueError, AssertionError):
                continue
            try:
                oldRead = _readVolume(volume, old)
                newRead = _readVolume(volume, new)
            except Exception:
                continue

            originalByOffset = {p.offset: o for o, p in elementPlacements.items()}
            oldParent = _parentErrors(oldRead, elementPlacements, nodesByOriginal,
                                      originalByOffset)
            newParent = _parentErrors(newRead, elementPlacements, nodesByOriginal,
                                      originalByOffset)
            oldOrder = _orderErrors(oldRead, elementPlacements, nodesByOriginal,
                                    originalByOffset)
            newOrder = _orderErrors(newRead, elementPlacements, nodesByOriginal,
                                    originalByOffset)
            oldVoid = _voidErrors(oldRead, elementPlacements, nodesByOriginal,
                                  originalByOffset, voidSlots, gaps)
            newVoid = _voidErrors(newRead, elementPlacements, nodesByOriginal,
                                  originalByOffset, voidSlots, gaps)

            record = (totalCost, volume, placements, elementPlacements, gaps,
                      voidSlots, widths, oldParent, newParent, oldOrder,
                      newOrder, oldVoid, newVoid)
            if addressFallback is None:
                addressFallback = record
            if oldParent or newParent or oldOrder or newOrder or oldVoid or newVoid:
                if layoutFallback is None or totalCost < layoutFallback[0]:
                    layoutFallback = record
                continue
            if bestValid is None or totalCost < bestValid[0]:
                bestValid = record

    if bestValid is not None:
        (cost, _dryVolume, _placements, _elementPlacements, gaps, _voidSlots,
         widths, oldParent, newParent, oldOrder, newOrder, oldVoid,
         newVoid) = bestValid
        volume, placements, elementPlacements, voidSlots = _materialize(
            data, roots, itemsByOwner, widths, gaps, volumeEnd,
            copyPayload=True)
        locator = _locator(nodes, elementPlacements, volumeEnd)
        return ExportResult(
            ok=True, volume=volume, locator=locator, cost=cost,
            layout=tuple(placements),
            diagnostics={
                'voidWindows': [{'start': w.start, 'end': w.end,
                                 'cost': w.cost, 'label': w.label}
                                for w in perms.windows
                                if any(g[1] is w for g in gaps.values())],
                'rewrittenContainers': [
                    {'originalOffset': n.originalOffset,
                     'name': n.name,
                     'fromWidth': n.originalSizeWidth,
                     'toWidth': widths[n.originalOffset][0],
                     'toUnknown': widths[n.originalOffset][1]}
                    for n in masters
                    if widths[n.originalOffset] != (
                        n.originalSizeWidth, n.originalSizeValue is None)],
            })

    if addressFallback is None:
        affected = []
        orderedRoots = sorted(roots, key=lambda n: n.originalOffset)
        for i, root in enumerate(orderedRoots):
            start = lockTargets.get(root.originalOffset, root.originalOffset)
            if i + 1 < len(orderedRoots):
                end = lockTargets.get(orderedRoots[i + 1].originalOffset,
                                      orderedRoots[i + 1].originalOffset)
            else:
                end = volumeEnd
            affected.append({'originalOffset': root.originalOffset,
                             'name': root.name,
                             'start': start, 'end': end,
                             'lockedStart': start, 'lockedEnd': end,
                             'length': end - start})
        return ExportResult(
            ok=False, failure='address',
            diagnostics={'message': 'No feasible layout reaches the locked '
                                    'element starts and volume end',
                         'affectedRanges': affected,
                         'originalEnd': len(data),
                         'requestedEnd': volumeEnd,
                         'testedLayouts': tested},
            layout=())

    fallback = layoutFallback or addressFallback
    (cost, volume, placements, elementPlacements, gaps, voidSlots, widths,
     oldParent, newParent, oldOrder, newOrder, oldVoid, newVoid) = fallback
    firstErrors = []
    for reader, errs in (('old', {'parent': oldParent, 'void': oldVoid,
                                  'order': oldOrder}),
                         ('new', {'parent': newParent, 'void': newVoid,
                                  'order': newOrder})):
        first = _firstError(errs)
        if first is not None:
            firstErrors.append(_errorSummary(first, reader, elementPlacements))

    layoutPlan = [{'kind': p.kind, 'offset': p.offset,
                   'headerEnd': p.headerEnd, 'payloadEnd': p.payloadEnd,
                   'originalOffset': (p.element.originalOffset
                                      if p.element is not None else None),
                   'sizeWidth': p.sizeWidth, 'sizeValue': p.sizeValue}
                  for p in placements]
    return ExportResult(
        ok=False, failure='layout',
        diagnostics={'message': 'Locked addresses are reachable, but at '
                                'least one reader would misread parentage',
                     'firstErrors': firstErrors,
                     'layoutPlan': layoutPlan,
                     'oldErrors': {'parent': oldParent, 'void': oldVoid,
                                   'order': oldOrder},
                     'newErrors': {'parent': newParent, 'void': newVoid,
                                   'order': newOrder},
                     'testedLayouts': tested},
        cost=cost, layout=tuple(placements))


archiveVolume = exportVolume
relocateVolume = exportVolume
repackageVolume = exportVolume
repackVolume = exportVolume
export_volume = exportVolume
archive_volume = exportVolume
relocate_volume = exportVolume
repackage_volume = exportVolume
repack_volume = exportVolume
