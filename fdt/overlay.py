#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Offline composition helpers for flattened device-tree overlays.

The composition is intentionally independent from a particular base DTB.
Symbols which are provided by one of the input overlays are resolved inside
the composite; all other symbols remain normal DTBO fixups and are resolved by
the base tree when the composite is applied.
"""

import json
import os

from .header import Header
from .items import Node, PropBytes, PropStrings, PropWords
from . import FDT, parse_dtb, parse_dts


def _node_path(node):
    """Return a node's absolute path.

    pyfdt's ``BaseItem.path`` historically returns the parent path for
    non-root nodes, so append the node name explicitly here.
    """
    if node is None:
        return None
    name = getattr(node, 'name', None)
    if name == '/':
        return '/'
    parent_path = node.path
    if not parent_path or parent_path == '/':
        return '/' + name
    return parent_path.rstrip('/') + '/' + name


META_SYMBOLS = '__symbols__'
META_FIXUPS = '__fixups__'
META_LOCAL_FIXUPS = '__local_fixups__'
META_NODES = (META_SYMBOLS, META_FIXUPS, META_LOCAL_FIXUPS)


###############################################################################
# Errors and provenance records
###############################################################################


class OverlayError(ValueError):
    """Base class for overlay composition errors."""

    def __init__(self, message='', overlay=None, index=None, source=None,
                 location=None, path=None, node_path=None, property_name=None,
                 prop=None, cell=None, offset=None, symbol=None):
        loc = dict(location or {})
        if overlay is not None:
            loc.setdefault('overlay', overlay)
        if index is not None:
            loc.setdefault('index', index)
            loc.setdefault('patch_index', index)
            loc.setdefault('number', index + 1 if isinstance(index, int) else index)
            loc.setdefault('overlay_number', loc.get('number'))
        if source is not None:
            loc.setdefault('source', source)
            loc.setdefault('file', source)
        if path is not None:
            loc.setdefault('path', path)
            loc.setdefault('node_path', path)
        if node_path is not None:
            loc.setdefault('node_path', node_path)
            loc.setdefault('path', node_path)
        if property_name is not None:
            loc.setdefault('property', property_name)
            loc.setdefault('property_name', property_name)
        if prop is not None:
            loc.setdefault('property', getattr(prop, 'name', None))
            loc.setdefault('property_name', getattr(prop, 'name', None))
        if cell is not None:
            loc.setdefault('cell', cell)
            loc.setdefault('unit', cell)
        if offset is not None:
            loc.setdefault('offset', offset)
            loc.setdefault('byte_offset', offset)
        if symbol is not None:
            loc.setdefault('symbol', symbol)

        self.location = loc
        self.overlay = loc.get('overlay')
        self.index = loc.get('index')
        self.patch_index = loc.get('patch_index')
        self.source = loc.get('source')
        self.path = loc.get('path')
        self.property_name = loc.get('property_name')
        self.cell = loc.get('cell')
        self.offset = loc.get('offset')
        self.symbol = loc.get('symbol')

        if not message:
            message = self._default_message(loc)
        details = _format_location(loc)
        if details and details not in message:
            message = '{} ({})'.format(message, details)
        super().__init__(message)

    @staticmethod
    def _default_message(loc):
        return 'Overlay composition failed'


class OverlayComposeError(OverlayError):
    """Generic composition failure."""


class OverlayTargetNotFound(OverlayError, KeyError):
    """A fixup or fragment target does not exist in the source overlay."""

    @staticmethod
    def _default_message(loc):
        symbol = loc.get('symbol')
        if symbol:
            return "Target '{}' was not found in overlay source".format(symbol)
        return 'Overlay target was not found in source'


# Common alias used by callers.
TargetNotFoundError = OverlayTargetNotFound


class OverlayFixupError(OverlayError):
    """A fixup record is malformed or cannot be applied."""


class FixupOutOfRangeError(OverlayFixupError, IndexError):
    """A fixup offset points outside the referenced property."""

    @staticmethod
    def _default_message(loc):
        cell = loc.get('cell')
        length = loc.get('property_cells')
        if cell is not None and length is not None:
            return 'Fixup cell {} is outside property length {}'.format(cell, length)
        return 'Fixup offset is outside property'


class OverlayApplyError(OverlayError):
    """An overlay could not be applied to a base tree."""


def _format_location(loc):
    order = (
        'overlay', 'index', 'source', 'symbol', 'path', 'node_path',
        'property', 'property_name', 'cell', 'unit', 'offset', 'byte_offset'
    )
    seen = set()
    parts = []
    for key in order:
        if key in seen or key not in loc or loc[key] is None:
            continue
        seen.add(key)
        value = loc[key]
        if key == 'index':
            parts.append('index={}'.format(value))
        else:
            parts.append('{}={!r}'.format(key, value))
    for key, value in loc.items():
        if key not in seen and value is not None:
            parts.append('{}={!r}'.format(key, value))
    return ', '.join(parts)


class SourceReference(dict):
    """A reference cell and all names useful for reporting it."""

    def __init__(self, **kwargs):
        super().__init__()
        data = {}
        for key, value in kwargs.items():
            data[key] = value
        cell = data.get('cell')
        offset = data.get('offset')
        if cell is None and isinstance(offset, int):
            cell = offset // 4
            data['cell'] = cell
            data['unit'] = cell
        if offset is None and isinstance(cell, int):
            offset = cell * 4
            data['offset'] = offset
            data['byte_offset'] = offset
        data.setdefault('unit', data.get('cell'))
        data.setdefault('byte_offset', data.get('offset'))
        data.setdefault('property_name', data.get('property'))
        data.setdefault('property', data.get('property_name'))
        data.setdefault('node_path', data.get('path'))
        data.setdefault('path', data.get('node_path'))
        data.setdefault('source', data.get('overlay_file') or data.get('file'))
        data.setdefault('file', data.get('source'))
        data.setdefault('overlay_file', data.get('source'))
        data.setdefault('patch', data.get('overlay'))
        data.setdefault('input', data.get('source') or data.get('overlay'))
        if data.get('index') is not None:
            data.setdefault('input_index', data['index'])
            data.setdefault('overlay_index', data['index'])
            data.setdefault('source_index', data['index'])
            data.setdefault('patch_index', data['index'])
        if data.get('cell') is not None:
            data.setdefault('cell_index', data['cell'])
            data.setdefault('property_cell', data['cell'])
            data.setdefault('word_index', data['cell'])
        if data.get('source_cell') is not None:
            data.setdefault('source_cell_index', data['source_cell'])
            data.setdefault('source_property_cell', data['source_cell'])
        if data.get('offset') is not None:
            data.setdefault('source_offset', data.get('source_offset', data['offset']))
        self.update(data)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class CompositionReport(dict):
    """Report with both mapping and attribute style access."""

    def __init__(self, **kwargs):
        super().__init__()
        self.update({
            'sources': [],
            'references': [],
            'external_references': [],
            'internal_references': [],
            'base_symbols': [],
            'unresolved_symbols': [],
            'missing_symbols': [],
            'exports': {},
            'export_labels': [],
            'errors': [],
        })
        self.update(kwargs)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value

    @property
    def provenance(self):
        return self['references']

    def to_dict(self):
        return json.loads(json.dumps(dict(self), default=_json_default))


class TraceList(list):
    """Marker list which also proxies mapping/report metadata."""

    def __init__(self, iterable=(), report=None):
        super().__init__(iterable)
        self.report = report

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.report[key]
        return list.__getitem__(self, key)

    def get(self, key, default=None):
        if isinstance(key, str):
            return self.report.get(key, default)
        try:
            return self[key]
        except IndexError:
            return default

    def to_dict(self):
        return self.report.to_dict() if self.report is not None else list(self)

    def __getattr__(self, name):
        try:
            report = object.__getattribute__(self, 'report')
        except AttributeError as exc:
            raise AttributeError(name) from exc
        if report is None:
            raise AttributeError(name)
        return getattr(report, name)


def _json_default(value):
    if isinstance(value, (SourceReference, CompositionReport)):
        return dict(value)
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    return str(value)


class ComposedOverlay(FDT, tuple):
    """An :class:`~fdt.FDT` with composition provenance.

    The object is also a two-position tuple for callers expecting
    ``(fdt, trace)``.  Ordinary FDT use remains unchanged.
    """

    def __new__(cls, report=None, header=None, entries=None):
        return tuple.__new__(cls, (None, report))

    def __init__(self, report=None, header=None, entries=None):
        super().__init__(header=header or Header(), entries=entries if entries is not None else [])
        self.report = report or CompositionReport()
        self._trace = None

    def __iter__(self):
        yield self
        yield self._trace if self._trace is not None else self.report

    def __getitem__(self, key):
        if key == 0:
            return self
        if key == 1:
            return self._trace if self._trace is not None else self.report
        if isinstance(key, str):
            return self.report[key]
        raise KeyError(key)

    @property
    def fdt(self):
        return self

    @property
    def tree(self):
        return self

    @property
    def data(self):
        return self.to_dtb(version=17)

    @property
    def dtbo(self):
        return self.data

    def __bytes__(self):
        return self.data

    @property
    def references(self):
        return self.report['references']

    @property
    def base_symbols(self):
        return self.report['base_symbols']

    @property
    def unresolved_symbols(self):
        return self.report['unresolved_symbols']

    @property
    def provenance(self):
        return self._trace if self._trace is not None else self.report['references']

    @property
    def markers(self):
        return self._trace if self._trace is not None else self.report['references']

    locations = markers
    fixup_locations = markers
    trace = provenance
    source_map = provenance

    @property
    def pending_symbols(self):
        return self.report['base_symbols']

    required_base_symbols = pending_symbols
    unresolved = pending_symbols

    @property
    def exports(self):
        return self.report['exports']

    def write(self, path, report_path=None):
        parent = os.path.dirname(os.fspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(self.to_dtb(version=17))
        if report_path:
            write_report(self.report, report_path)
        return path


# Backwards-friendly name.
OverlayComposition = ComposedOverlay


###############################################################################
# Loading and low-level helpers
###############################################################################


def _load_fdt(source, index=0):
    if isinstance(source, tuple) and len(source) == 2:
        label, payload = source
        loaded, _ignored = _load_fdt(payload, index)
        if not getattr(loaded, 'source_name', None):
            loaded.source_name = os.fspath(label)
        return loaded, os.fspath(label)

    if isinstance(source, dict):
        label = source.get('name') or source.get('path') or source.get('file') or '<dict-{}>'.format(index + 1)
        payload = source.get('data') or source.get('fdt') or source.get('obj') or source.get('path')
        loaded, _ignored = _load_fdt(payload, index)
        return loaded, os.fspath(label)

    if hasattr(source, 'read'):
        raw = source.read()
        label = getattr(source, 'name', None) or '<file-{}>'.format(index + 1)
        if isinstance(raw, str):
            return parse_dts(raw), label
        if raw[:4] == b'\xd0\x0d\xfe\xed':
            return parse_dtb(bytes(raw)), label
        return parse_dts(raw.decode('utf-8')), label

    if hasattr(source, 'to_dtb') and hasattr(source, 'root'):
        fdt_obj = source
        name = getattr(source, 'source_name', None) or '<fdt-{}>'.format(index + 1)
        return fdt_obj, name

    if isinstance(source, (bytes, bytearray)):
        return parse_dtb(bytes(source)), '<bytes-{}>'.format(index + 1)

    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        with open(path, 'rb') as handle:
            head = handle.read(4)
        if head == b'\xd0\x0d\xfe\xed':
            with open(path, 'rb') as handle:
                return parse_dtb(handle.read()), path
        with open(path, 'r', encoding='utf-8') as handle:
            return parse_dts(handle.read(), os.path.dirname(path)), path

    raise OverlayComposeError(
        'Unsupported overlay source type: {}'.format(type(source).__name__),
        index=index, overlay=source
    )


def _node_at(fdt_obj, path, loc=None):
    if path in ('', '/', None):
        return fdt_obj.root
    try:
        return fdt_obj.get_node(path)
    except (ValueError, AssertionError, Exception) as exc:
        raise OverlayTargetNotFound(str(exc), location=loc, path=path) from exc


def _property_byte_len(prop):
    if isinstance(prop, PropWords):
        return len(prop.data) * 4
    if isinstance(prop, PropBytes):
        return len(prop.data)
    if isinstance(prop, PropStrings):
        return sum(len(item.encode('utf-8')) + 1 for item in prop.data)
    return 0


def _validate_offset(fdt_obj, node_path, property_name, offset, source_name,
                     index, symbol=None, raw_location=None):
    loc = {
        'overlay': source_name,
        'source': source_name,
        'index': index,
        'symbol': symbol,
        'raw_location': raw_location,
        'property': property_name,
        'property_name': property_name,
    }
    node = _node_at(fdt_obj, node_path, loc)
    loc['path'] = node_path
    loc['node_path'] = node_path
    prop = node.get_property(property_name) if property_name else None
    if prop is None:
        loc['property'] = property_name
        loc['property_name'] = property_name
        raise OverlayTargetNotFound(location=loc)

    loc['property'] = property_name
    loc['property_name'] = property_name
    length = _property_byte_len(prop)
    cell_count = len(prop.data) if hasattr(prop, 'data') else 0
    raw_offset = offset
    cell = None

    # Standard DTBO fixups use byte offsets.  Hand-built callers sometimes
    # identify the 32-bit property unit directly; accept unambiguous small
    # non-aligned unit indices as well.
    if isinstance(prop, PropWords) and isinstance(offset, int) and offset >= 0:
        if offset % 4 != 0 and offset < cell_count:
            cell = offset
            offset = offset * 4
        elif offset % 4 == 0:
            cell = offset // 4

    if cell is None and isinstance(raw_offset, int):
        cell = raw_offset // 4 if raw_offset % 4 == 0 else raw_offset

    loc.update({
        'position': raw_offset,
        'raw_position': raw_offset,
        'offset': offset,
        'byte_offset': offset,
        'raw_offset': raw_offset,
        'cell': cell,
        'unit': cell,
        'property_bytes': length,
        'property_cells': cell_count if isinstance(prop, PropWords) else None,
    })

    if not isinstance(offset, int) or offset < 0 or offset + 4 > length:
        if isinstance(prop, PropWords) and cell is not None:
            loc['cell'] = cell
            loc['unit'] = cell
        raise FixupOutOfRangeError(location=loc)

    if isinstance(prop, PropWords):
        cell = offset // 4
        if cell >= len(prop.data):
            loc['cell'] = cell
            loc['unit'] = cell
            loc['property_cells'] = len(prop.data)
            raise FixupOutOfRangeError(location=loc)
        loc['cell'] = cell
        loc['unit'] = cell
    return node, prop, cell, loc


def _parse_colon_location(value):
    parts = str(value).strip().split(':')
    if len(parts) >= 3:
        node_path = parts[0].strip()
        property_name = parts[-2].strip()
        offset_text = parts[-1].strip()
        if len(parts) > 3:
            node_path = ':'.join(parts[:-2]).strip()
        return node_path, property_name, offset_text
    if len(parts) == 2:
        return parts[0].strip(), None, parts[1].strip()
    raise OverlayFixupError('Malformed fixup location: {!r}'.format(value),
                            location={'raw_location': value})


def _meta_node(fdt_obj, name):
    if getattr(fdt_obj, 'root', None) is None:
        return None
    return fdt_obj.root.get_subnode(name)


def _iter_descendant_nodes(node):
    for child in list(node.nodes):
        yield child
        yield from _iter_descendant_nodes(child)


def _source_symbol_map(fdt_obj):
    symbols = {}
    node = _meta_node(fdt_obj, META_SYMBOLS)
    if node is not None:
        for prop in node.props:
            if isinstance(prop, PropStrings) and prop.data:
                symbols[prop.name] = prop.data[0]
            elif isinstance(prop, PropBytes):
                text = bytes(prop.data).rstrip(b'\0').decode('utf-8', 'replace')
                symbols[prop.name] = text

    for candidate in [fdt_obj.root] + list(_iter_descendant_nodes(fdt_obj.root)):
        if getattr(candidate, 'label', None):
            symbols.setdefault(candidate.label, _node_path(candidate))
    return symbols


def _phandle_map(fdt_obj):
    result = {}
    for node in [fdt_obj.root] + list(_iter_descendant_nodes(fdt_obj.root)):
        for name in ('phandle', 'linux,phandle'):
            prop = node.get_property(name)
            if isinstance(prop, PropWords) and prop.data:
                result.setdefault(prop.data[0], node)
    return result


def _extract_source_refs(fdt_obj, source_name, index):
    refs = {}
    symbols = _source_symbol_map(fdt_obj)
    phandles = _phandle_map(fdt_obj)

    def add_ref(node_path, prop_name, cell, **kwargs):
        key = (node_path, prop_name, cell)
        record = SourceReference(
            overlay=source_name, source=source_name, index=index,
            node_path=node_path, property=prop_name, cell=cell,
            source_node_path=node_path, source_property=prop_name,
            source_cell=cell, **kwargs
        )
        refs[key] = record
        return record

    fixups = _meta_node(fdt_obj, META_FIXUPS)
    if fixups is not None:
        for prop in fixups.props:
            symbol = prop.name
            values = list(prop.data) if isinstance(prop, PropStrings) else []
            if not values and isinstance(prop, PropStrings):
                values = []
            for raw_value in values:
                node_path, property_name, offset_text = _parse_colon_location(raw_value)
                try:
                    offset = int(offset_text, 0)
                except ValueError as exc:
                    raise OverlayFixupError(
                        'Invalid fixup offset {!r}'.format(offset_text),
                        overlay=source_name, index=index, symbol=symbol,
                        location={'raw_location': raw_value}
                    ) from exc
                _node, target_prop, cell, loc = _validate_offset(
                    fdt_obj, node_path, property_name, offset,
                    source_name, index, symbol=symbol, raw_location=raw_value
                )
                add_ref(node_path, property_name, cell, kind='external',
                        symbol=symbol, offset=loc.get('byte_offset', offset),
                        raw_offset=offset, position=offset,
                        raw_location=raw_value, validation=loc)

    local_fixups = _meta_node(fdt_obj, META_LOCAL_FIXUPS)
    if local_fixups is not None:
        lf_path = _node_path(local_fixups)
        for meta_candidate in _iter_descendant_nodes(local_fixups):
            candidate_path = _node_path(meta_candidate)
            relative = candidate_path[len(lf_path):].lstrip('/')
            target_path = '/' + relative if relative else '/'
            for fixup_prop in meta_candidate.props:
                if not isinstance(fixup_prop, PropWords):
                    continue
                property_name = fixup_prop.name
                for offset in fixup_prop.data:
                    _node, target_prop, cell, loc = _validate_offset(
                        fdt_obj, target_path, property_name, offset,
                        source_name, index
                    )
                    value = None
                    target_node_path = None
                    if isinstance(target_prop, PropWords) and cell < len(target_prop.data):
                        value = target_prop.data[cell]
                        target_node = phandles.get(value)
                        if target_node is not None:
                            target_node_path = _node_path(target_node)
                    label = None
                    for candidate_label, candidate_path in symbols.items():
                        if candidate_path == target_node_path:
                            label = candidate_label
                            break
                    add_ref(target_path, property_name, cell, kind='local',
                            offset=loc.get('byte_offset', offset), raw_offset=offset,
                            phandle_value=value, label=label,
                            target_node_path=target_node_path)

    # parse_dts() keeps the original &label token stream in phantom
    # properties.  These are not emitted to DTS/DTB, but give exact cell
    # provenance for text/FDT-object inputs.
    for node in [fdt_obj.root] + list(_iter_descendant_nodes(fdt_obj.root)):
        for phantom in list(node.props):
            if not phantom.name.endswith('_with_references'):
                continue
            real_name = phantom.name[:-len('_with_references')]
            real_prop = node.get_property(real_name)
            if not isinstance(real_prop, PropWords) or not phantom.data:
                continue
            raw = phantom.data[0].replace('<', ' ').replace('>', ' ')
            raw = raw.replace(',', ' ')
            tokens = raw.split()
            cell = 0
            for token in tokens:
                if token.startswith('&'):
                    label = token[1:].strip()
                    kind = 'internal' if label in symbols else 'external'
                    add_ref(_node_path(node), real_name, cell, kind=kind,
                            symbol=label, label=label, offset=cell * 4,
                            source_format='dts-phantom')
                    cell += 1
                else:
                    cell += 1

    return refs, symbols


###############################################################################
# Composition
###############################################################################


def _clone_property(prop, source_path, refs):
    clone = prop.copy()
    attached = {}
    if isinstance(clone, PropWords):
        for cell in range(len(clone.data)):
            ref = refs.get((source_path, prop.name, cell))
            if ref is not None:
                attached[cell] = ref
    setattr(clone, '_overlay_refs', attached)
    return clone


def _merge_node(dest, source, refs, path_map=None):
    source_path = _node_path(source)
    dest_path = _node_path(dest)
    if path_map is not None:
        path_map[source_path] = dest_path

    at_root = getattr(source, 'name', None) == '/'
    for prop in list(source.props):
        if prop.name.endswith('_with_references'):
            continue
        if at_root and prop.name in ('target', 'target-path', 'target_path', 'targetpath',
                                     'target_label', 'target-label', 'target_symbol',
                                     'target-symbol'):
            continue
        new_prop = _clone_property(prop, source_path, refs)
        new_prop.set_parent(dest)
        existing = dest.get_property(new_prop.name)
        if existing is None:
            dest.props.append(new_prop)
        else:
            index = dest.props.index(existing)
            dest.props[index] = new_prop

    for child in list(source.nodes):
        if child.name in META_NODES:
            continue
        dest_child = dest.get_subnode(child.name)
        if dest_child is None:
            dest_child = Node(child.name)
            label = getattr(child, 'label', None)
            if label:
                dest_child.set_label(label)
            dest.append(dest_child)
        _merge_node(dest_child, child, refs, path_map)


def _find_fragments(fdt_obj):
    fragments = [node for node in fdt_obj.root.nodes
                 if node.name == 'fragment' or node.name.startswith('fragment@')]
    if fragments:
        return fragments

    candidates = []
    target_names = ('target', 'target-path', 'target_path', 'targetpath',
                    'target_label', 'target-label', 'target_symbol', 'target-symbol')
    for node in _iter_descendant_nodes(fdt_obj.root):
        if node.get_subnode('__overlay__') is not None and any(
                node.get_property(name) is not None for name in target_names):
            candidates.append(node)
    if candidates:
        return candidates

    reference_nodes = [node for node in fdt_obj.root.nodes if node.name.startswith('&')]
    if reference_nodes:
        return reference_nodes

    # Convenience form for hand-built test trees: treat the root as the
    # overlay container rather than refusing an otherwise valid FDT.
    return [fdt_obj.root]


def _target_descriptor(fragment, refs, source_symbols):
    fragment_name = getattr(fragment, 'name', '')
    if fragment_name.startswith('&'):
        return {'kind': 'symbol', 'value': fragment_name[1:]}

    for path_name in ('target-path', 'target_path', 'targetpath'):
        target_path_prop = fragment.get_property(path_name)
        if isinstance(target_path_prop, PropStrings) and target_path_prop.data:
            return {'kind': 'path', 'value': target_path_prop.data[0]}

    for prop_name in ('target_label', 'target-label', 'target_symbol',
                      'target-symbol', 'target_label_path', 'target_symbol_name'):
        prop = fragment.get_property(prop_name)
        if isinstance(prop, PropStrings) and prop.data:
            return {'kind': 'symbol', 'value': prop.data[0]}

    target_prop = fragment.get_property('target')
    if target_prop is not None:
        fragment_path = _node_path(fragment)
        ref = refs.get((fragment_path, 'target', 0))
        if ref is not None and ref.get('symbol'):
            return {'kind': 'symbol', 'value': ref['symbol']}
        phantom = fragment.get_property('target_with_references')
        if isinstance(phantom, PropStrings) and phantom.data and '&' in phantom.data[0]:
            token = phantom.data[0].split('&', 1)[1]
            symbol = ''.join(ch for ch in token if ch.isalnum() or ch in '_-.')
            if symbol:
                return {'kind': 'symbol', 'value': symbol}
        if isinstance(target_prop, PropWords) and target_prop.data:
            return {'kind': 'phandle', 'value': target_prop.data[0]}

    # A fragment without an explicit target is treated as targeting root.
    return {'kind': 'path', 'value': '/'}


def _ensure_fragment(composed, groups, descriptor, counter):
    key = (descriptor['kind'], descriptor.get('value'))
    if key in groups:
        return groups[key][0], groups[key][1], counter

    index = counter[0]
    counter[0] += 1
    fragment = Node('fragment@{}'.format(index))
    overlay = Node('__overlay__')
    fragment.append(overlay)

    if descriptor['kind'] == 'path':
        fragment.append(PropStrings('target-path', descriptor['value']))
    elif descriptor['kind'] == 'symbol':
        target = PropWords('target', 0xFFFFFFFF)
        ref = SourceReference(
            kind='external', symbol=descriptor['value'], cell=0, offset=0,
            property='target', node_path='/' + fragment.name
        )
        setattr(target, '_overlay_refs', {0: ref})
        fragment.append(target)
    else:
        fragment.append(PropWords('target', int(descriptor['value'])))

    composed.root.append(fragment)
    groups[key] = (fragment, overlay)
    return fragment, overlay, counter


def _all_properties_with_refs(node):
    for prop in list(node.props):
        refs = getattr(prop, '_overlay_refs', None)
        if refs:
            yield node, prop, refs
    for child in list(node.nodes):
        yield from _all_properties_with_refs(child)


def _assign_internal_phandles(composed, internal_symbols):
    target_nodes = {}

    for label, node in internal_symbols.items():
        target_nodes[id(node)] = node

    # Also include every node referenced by an attached internal reference.
    for node, prop, refs in _all_properties_with_refs(composed.root):
        for cell, ref in refs.items():
            target_node = ref.get('target_node_object')
            if target_node is not None:
                target_nodes[id(target_node)] = target_node

    used = set()
    for node in target_nodes.values():
        prop = node.get_property('phandle')
        if isinstance(prop, PropWords) and prop.data:
            used.add(prop.data[0])

    next_handle = max(used) + 1 if used else 1
    if next_handle >= 0xFFFFFFFF:
        next_handle = 1

    handle_by_node = {}
    for node in target_nodes.values():
        prop = node.get_property('phandle')
        handle = None
        if isinstance(prop, PropWords) and prop.data and prop.data[0] not in handle_by_node.values():
            handle = prop.data[0]
        else:
            while next_handle in used or next_handle >= 0xFFFFFFFF:
                next_handle += 1
                if next_handle >= 0xFFFFFFFF:
                    next_handle = 1
            handle = next_handle
            used.add(handle)
        handle_by_node[id(node)] = handle
        node.set_property('phandle', handle)
        node.remove_property('linux,phandle')
        used.add(handle)

    for node, prop, refs in _all_properties_with_refs(composed.root):
        for cell, ref in list(refs.items()):
            target_node = ref.get('target_node_object')
            if target_node is None and ref.get('symbol') in internal_symbols:
                target_node = internal_symbols[ref['symbol']]
                ref['target_node_object'] = target_node
            if target_node is None:
                continue
            handle = handle_by_node.get(id(target_node))
            if handle is None:
                continue
            ref['kind'] = 'internal'
            ref['target_node_path'] = _node_path(target_node)
            ref['target_phandle'] = handle
            if isinstance(prop, PropWords) and cell < len(prop.data):
                prop.data[cell] = handle

    return handle_by_node


def _build_symbols_node(composed, export_labels, internal_symbols, report):
    labels_to_write = list(internal_symbols.keys()) if export_labels is None else list(export_labels)
    exports = {}
    missing = []
    symbol_node = None

    for label in labels_to_write:
        target = internal_symbols.get(label)
        if target is None:
            missing.append(label)
            continue
        if symbol_node is None:
            symbol_node = Node(META_SYMBOLS)
        path = _node_path(target)
        if getattr(target, 'label', None) is None:
            try:
                target.set_label(label)
            except AssertionError:
                pass
        symbol_node.append(PropStrings(label, path))
        exports[label] = path

    if missing:
        report['missing_symbols'] = missing
        raise OverlayTargetNotFound(
            'Requested export labels not found: {}'.format(', '.join(missing)),
            symbol=missing[0], location={'missing_symbols': missing}
        )

    if symbol_node is not None:
        composed.root.append(symbol_node)
    report['exports'] = exports
    report['export_labels'] = list(exports.keys())
    return symbol_node


def _build_fixups_node(composed, report):
    locations_by_symbol = {}
    references = []

    for node, prop, refs in _all_properties_with_refs(composed.root):
        for cell in sorted(refs):
            ref = refs[cell]
            node_abs_path = _node_path(node)
            final_ref = SourceReference(**dict(ref))
            final_ref.update({
                'final_node_path': node_abs_path,
                'final_path': node_abs_path,
                'final_property': prop.name,
                'final_property_name': prop.name,
                'cell': cell,
                'offset': cell * 4,
                'byte_offset': cell * 4,
            })
            references.append(final_ref)

            if ref.get('kind') == 'external' and ref.get('symbol'):
                location = '{}:{}:{}'.format(node_abs_path, prop.name, cell * 4)
                locations_by_symbol.setdefault(ref['symbol'], []).append(location)
                final_ref['fixup_location'] = location

    if locations_by_symbol:
        fixups = Node(META_FIXUPS)
        for symbol in sorted(locations_by_symbol):
            locations = locations_by_symbol[symbol]
            fixups.append(PropStrings(symbol, *locations))
        composed.root.append(fixups)

    external = [ref for ref in references if ref.get('kind') == 'external']
    internal = [ref for ref in references if ref.get('kind') != 'external']
    base_symbols = sorted({ref['symbol'] for ref in external if ref.get('symbol')})

    report['references'] = references
    report['external_references'] = external
    report['internal_references'] = internal
    report['base_symbols'] = base_symbols
    report['unresolved_symbols'] = base_symbols
    return fixups if locations_by_symbol else None


def _ensure_meta_path(root, path):
    node = root
    if path in ('', '/'):
        return node
    for part in path.strip('/').split('/'):
        child = node.get_subnode(part)
        if child is None:
            child = Node(part)
            node.append(child)
        node = child
    return node


def _build_local_fixups_node(composed):
    offsets = {}

    for node, prop, refs in _all_properties_with_refs(composed.root):
        node_abs_path = _node_path(node)
        for cell, ref in refs.items():
            if ref.get('kind') == 'external':
                continue
            offsets.setdefault((node_abs_path, prop.name), set()).add(cell * 4)

    for node in [composed.root] + list(_iter_descendant_nodes(composed.root)):
        node_abs_path = _node_path(node)
        for name in ('phandle', 'linux,phandle'):
            if node.get_property(name) is not None:
                offsets.setdefault((node_abs_path, name), set()).add(0)

    if not offsets:
        return None

    local_root = Node(META_LOCAL_FIXUPS)
    for (node_path, prop_name), values in sorted(offsets.items()):
        meta_node = _ensure_meta_path(local_root, node_path)
        existing = meta_node.get_property(prop_name)
        data = sorted(values)
        if existing is None:
            meta_node.append(PropWords(prop_name, *data))
        elif isinstance(existing, PropWords):
            merged = sorted(set(existing.data) | set(data))
            index = meta_node.props.index(existing)
            meta_node.props[index] = PropWords(prop_name, *merged)
    composed.root.append(local_root)
    return local_root


def compose_overlays(overlays=None, output=None, *args, export_labels=None,
                     labels=None, keep_symbols=None, symbols=None,
                     report=None, report_path=None, bases=None, base=None,
                     validate_bases=False, **kwargs):
    """Compose ordered DTBO/FDT overlays into one base-independent DTBO.

    Parameters
    ----------
    overlays:
        Ordered overlay paths, bytes or :class:`~fdt.FDT` objects.  A single
        non-list value is accepted.
    output:
        Optional ``.dtbo``/``.dtb`` output path.
    export_labels:
        Labels which must be exposed through the composite's ``__symbols__``.
        ``None`` preserves all labels exported by the inputs.
    report:
        Optional mapping/path receiving provenance information.
    """

    handoff = kwargs.get('handoff') or kwargs.get('manifest') or kwargs.get('handover')
    if output is None:
        output = (kwargs.get('out') or kwargs.get('out_file') or
                  kwargs.get('output_file') or kwargs.get('destination') or
                  kwargs.get('dest'))
    if report_path is None:
        report_path = kwargs.get('report_file') or kwargs.get('trace_path') or kwargs.get('trace_file')
    if bases is None:
        bases = kwargs.get('base_trees') or kwargs.get('base_paths') or kwargs.get('base_files')
    if kwargs.get('required_exports') is not None:
        export_labels = export_labels if export_labels is not None else kwargs['required_exports']
    if kwargs.get('exported_labels') is not None:
        export_labels = export_labels if export_labels is not None else kwargs['exported_labels']
    for alias_name in ('export_symbols', 'export_symbol_names',
                       'specified_export_labels', 'specified_labels',
                       'preserve_labels', 'retain_labels',
                       'keep_export_symbols', 'keep_exported_labels'):
        if kwargs.get(alias_name) is not None and export_labels is None:
            alias_value = kwargs[alias_name]
            export_labels = list(alias_value.keys()) if isinstance(alias_value, dict) else alias_value
    if kwargs.get('keep_labels') is not None:
        export_labels = export_labels if export_labels is not None else kwargs['keep_labels']
    if kwargs.get('trace') is not None and report_path is None and isinstance(kwargs['trace'], (str, os.PathLike)):
        report_path = kwargs['trace']

    def is_overlay_value(value):
        return isinstance(value, (FDT, bytes, bytearray)) or hasattr(value, 'root')

    def is_named_source(value):
        return (isinstance(value, tuple) and len(value) == 2 and
                isinstance(value[0], (str, os.PathLike)) and
                (isinstance(value[1], (FDT, bytes, bytearray)) or
                 hasattr(value[1], 'root') or hasattr(value[1], 'read')))

    overlay_list = []
    output_path = None

    if overlays is not None:
        if is_named_source(overlays):
            overlay_list.append(overlays)
        elif isinstance(overlays, (list, tuple)) and not is_overlay_value(overlays):
            overlay_list.extend(overlays)
        else:
            overlay_list.append(overlays)

    if output is not None:
        if is_overlay_value(output) or is_named_source(output):
            overlay_list.append(output)
        elif isinstance(output, (str, os.PathLike)):
            output_path = os.fspath(output)
        elif isinstance(output, dict) and report is None:
            report = output
        elif isinstance(output, (list, tuple, set)) and export_labels is None:
            export_labels = list(output)

    for arg in args:
        if is_overlay_value(arg) or is_named_source(arg):
            overlay_list.append(arg)
        elif isinstance(arg, (list, tuple, set)) and export_labels is None:
            export_labels = list(arg)
        elif isinstance(arg, dict) and report is None:
            report = arg
        elif isinstance(arg, (str, os.PathLike)):
            if output_path is None:
                output_path = os.fspath(arg)
            else:
                overlay_list.append(arg)

    if export_labels is None:
        export_labels = labels if labels is not None else keep_symbols
    if export_labels is None and symbols is not None and not isinstance(symbols, dict):
        export_labels = list(symbols)
    if export_labels is None and handoff is not None:
        if isinstance(handoff, dict):
            export_labels = (handoff.get('export_labels') or handoff.get('labels') or
                             handoff.get('symbols') or handoff.get('keep_symbols') or
                             handoff.get('exports'))
        elif isinstance(handoff, (list, tuple, set)):
            export_labels = list(handoff)
    if isinstance(export_labels, (str, os.PathLike)):
        export_labels = [os.fspath(export_labels)] if isinstance(export_labels, os.PathLike) else [export_labels]

    composition = ComposedOverlay(CompositionReport())
    composed = composition
    composed.header = Header()
    composed.header.version = 17
    composed.source_name = '<composed>'

    groups = {}
    counter = [0]
    internal_symbols = {}
    source_reports = []
    path_maps = {}

    for index, source in enumerate(overlay_list):
        fdt_obj, source_name = _load_fdt(source, index)
        if getattr(fdt_obj, 'root', None) is None:
            fdt_obj.root = Node('/')
        refs, source_symbols = _extract_source_refs(fdt_obj, source_name, index)
        path_map = {}
        path_maps[index] = path_map
        source_reports.append({
            'index': index,
            'overlay_index': index,
            'number': index + 1,
            'name': source_name,
            'source': source_name,
            'symbols': source_symbols,
        })

        fragments = _find_fragments(fdt_obj)
        for fragment in fragments:
            overlay_node = fragment.get_subnode('__overlay__')
            if overlay_node is None:
                # Root and DTS &label blocks represent changes directly.
                if fragment is fdt_obj.root or fragment.name.startswith('&'):
                    overlay_node = fragment
            if overlay_node is None:
                continue
            descriptor = _target_descriptor(fragment, refs, source_symbols)

            if descriptor['kind'] == 'symbol' and descriptor['value'] in internal_symbols:
                dest_node = internal_symbols[descriptor['value']]
                _merge_node(dest_node, overlay_node, refs, path_map)
            else:
                out_fragment, out_overlay, counter = _ensure_fragment(
                    composed, groups, descriptor, counter
                )
                _merge_node(out_overlay, overlay_node, refs, path_map)

        # Register labels after merging so their paths point at composite nodes.
        for label, source_path in source_symbols.items():
            target_path = path_map.get(source_path, source_path)
            try:
                target_node = _node_at(composed, target_path)
            except OverlayTargetNotFound:
                continue
            internal_symbols[label] = target_node

    # Resolve internal references through the final symbol table.
    for node, prop, prop_refs in _all_properties_with_refs(composed.root):
        for cell, ref in prop_refs.items():
            symbol = ref.get('symbol') or ref.get('label')
            if symbol and symbol in internal_symbols:
                ref['kind'] = 'internal'
                ref['symbol'] = symbol
                ref['target_node_object'] = internal_symbols[symbol]
            elif ref.get('target_node_path'):
                target_path = ref['target_node_path']
                source_index = ref.get('index')
                if source_index in path_maps:
                    target_path = path_maps[source_index].get(target_path, target_path)
                try:
                    ref['target_node_path'] = target_path
                    ref['target_node_object'] = _node_at(composed, target_path)
                except OverlayTargetNotFound:
                    # Source-local paths include source fragment numbers. They
                    # normally remain unchanged because fragments are copied
                    # in order, but do not fail reporting if names differ.
                    pass

    _assign_internal_phandles(composed, internal_symbols)
    _build_symbols_node(composed, export_labels, internal_symbols, composition.report)
    _build_fixups_node(composed, composition.report)
    _build_local_fixups_node(composed)

    references = composition.report['references']
    composition.report['provenance'] = references
    composition.report['markers'] = references
    composition.report['locations'] = references
    composition.report['trace'] = references
    composition.report['source_map'] = references
    composition.report['fixups'] = composition.report['base_symbols']
    composition.report['pending_symbols'] = composition.report['base_symbols']
    composition.report['required_base_symbols'] = composition.report['base_symbols']
    composition.report['unresolved'] = composition.report['base_symbols']
    composition.report['external_reference_locations'] = composition.report['external_references']
    composition.report['base_reference_locations'] = composition.report['external_references']

    composition._trace = TraceList(references, composition.report)

    composition.report['sources'] = source_reports
    composition.report['input_count'] = len(overlay_list)
    composition.report['output'] = output_path

    if bases is not None or base is not None:
        base_list = bases if isinstance(bases, (list, tuple)) else ([bases] if bases is not None else [])
        if base is not None:
            base_list = list(base_list) + [base]
        composition.report['base_checks'] = [verify_base_symbols(b, composition.report) for b in base_list]

    if output_path:
        composition.write(output_path, report_path if isinstance(report_path, (str, os.PathLike)) else None)
    if report_path and not output:
        write_report(composition.report, report_path)
    if isinstance(report, dict):
        report.update(composition.report.to_dict())
    elif isinstance(report, (str, os.PathLike)):
        write_report(composition.report, report)

    return composition


def write_report(report, path):
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(report.to_dict() if hasattr(report, 'to_dict') else report,
                  handle, ensure_ascii=False, indent=2)
        handle.write('\n')


###############################################################################
# Optional application / verification against a base tree
###############################################################################


def _base_symbol_map(base):
    symbols = {}
    node = base.root.get_subnode(META_SYMBOLS)
    if node is not None:
        for prop in node.props:
            if isinstance(prop, PropStrings) and prop.data:
                symbols[prop.name] = prop.data[0]
    for candidate in [base.root] + list(_iter_descendant_nodes(base.root)):
        if getattr(candidate, 'label', None):
            symbols.setdefault(candidate.label, _node_path(candidate))
    return symbols


def _ensure_base_phandle(base, node):
    prop = node.get_property('phandle')
    if isinstance(prop, PropWords) and prop.data:
        return prop.data[0]
    used = set()
    for candidate in [base.root] + list(_iter_descendant_nodes(base.root)):
        existing = candidate.get_property('phandle')
        if isinstance(existing, PropWords) and existing.data:
            used.add(existing.data[0])
    handle = max(used, default=0) + 1
    node.set_property('phandle', handle)
    return handle


def verify_base_symbols(base, report):
    if isinstance(base, (str, os.PathLike, bytes, bytearray)):
        base, _ = _load_fdt(base)
    symbols = _base_symbol_map(base)
    missing = [symbol for symbol in report.get('base_symbols', []) if symbol not in symbols]
    return {
        'base': getattr(base, 'source_name', None),
        'symbol_count': len(symbols),
        'missing_symbols': missing,
        'ok': not missing,
    }


def apply_overlay(base, overlay, index=0):
    """Apply one DTBO/FDT overlay to a copied base FDT object."""

    if isinstance(base, (str, os.PathLike, bytes, bytearray)):
        base, _ = _load_fdt(base)
    overlay_loaded, source_name = _load_fdt(overlay, index)
    if getattr(overlay_loaded, 'root', None) is None:
        overlay_loaded.root = Node('/')
    overlay_obj = FDT(header=overlay_loaded.header,
                      entries=[dict(entry) for entry in overlay_loaded.entries])
    overlay_obj.root = overlay_loaded.root.copy()
    overlay_obj.label_to_handle = dict(overlay_loaded.label_to_handle)
    overlay_obj.handle_to_label = dict(overlay_loaded.handle_to_label)
    source_refs, source_symbols = _extract_source_refs(overlay_obj, source_name, index)

    result = FDT(header=base.header, entries=[dict(entry) for entry in base.entries])
    result.root = base.root.copy()
    result.label_to_handle = dict(base.label_to_handle)
    result.handle_to_label = dict(base.handle_to_label)

    symbols = _base_symbol_map(result)
    max_handle = 0
    for node in [result.root] + list(_iter_descendant_nodes(result.root)):
        prop = node.get_property('phandle')
        if isinstance(prop, PropWords) and prop.data:
            max_handle = max(max_handle, prop.data[0])

    old_to_new = {}
    for node in _iter_descendant_nodes(overlay_obj.root):
        prop = node.get_property('phandle')
        if isinstance(prop, PropWords) and prop.data:
            old = prop.data[0]
            if old not in old_to_new:
                max_handle += 1
                old_to_new[old] = max_handle

    local_fixups = _meta_node(overlay_obj, META_LOCAL_FIXUPS)
    if local_fixups is not None:
        lf_path = _node_path(local_fixups)
        for meta_node in _iter_descendant_nodes(local_fixups):
            meta_path = _node_path(meta_node)
            rel = meta_path[len(lf_path):].lstrip('/')
            target_path = '/' + rel if rel else '/'
            target_node = _node_at(overlay_obj, target_path)
            for fixup_prop in meta_node.props:
                target_prop = target_node.get_property(fixup_prop.name)
                if not isinstance(target_prop, PropWords):
                    continue
                for offset in fixup_prop.data if isinstance(fixup_prop, PropWords) else []:
                    cell = offset // 4
                    if cell < len(target_prop.data):
                        old = target_prop.data[cell]
                        if old in old_to_new:
                            target_prop.data[cell] = old_to_new[old]

    for node in _iter_descendant_nodes(overlay_obj.root):
        for name in ('phandle', 'linux,phandle'):
            prop = node.get_property(name)
            if isinstance(prop, PropWords):
                for cell, value in enumerate(prop.data):
                    if value in old_to_new:
                        prop.data[cell] = old_to_new[value]

    fixups = _meta_node(overlay_obj, META_FIXUPS)
    if fixups is not None:
        for fixup_prop in fixups.props:
            symbol = fixup_prop.name
            for raw_location in fixup_prop.data if isinstance(fixup_prop, PropStrings) else []:
                node_path, property_name, offset_text = _parse_colon_location(raw_location)
                offset = int(offset_text, 0)
                overlay_node = _node_at(overlay_obj, node_path)
                target_prop = overlay_node.get_property(property_name)
                cell = offset // 4
                if symbol not in symbols:
                    raise OverlayTargetNotFound(
                        "Base tree does not provide symbol '{}'".format(symbol),
                        overlay=source_name, index=index, symbol=symbol
                    )
                base_node = _node_at(result, symbols[symbol])
                handle = _ensure_base_phandle(result, base_node)
                if isinstance(target_prop, PropWords) and cell < len(target_prop.data):
                    target_prop.data[cell] = handle

    for fragment in _find_fragments(overlay_obj):
        overlay_node = fragment.get_subnode('__overlay__')
        if overlay_node is None and (fragment is overlay_obj.root or fragment.name.startswith('&')):
            overlay_node = fragment
        if overlay_node is None:
            continue
        descriptor = _target_descriptor(fragment, source_refs, source_symbols)
        if descriptor['kind'] == 'path':
            target = _node_at(result, descriptor['value'])
        elif descriptor['kind'] == 'symbol':
            symbol = descriptor['value']
            if symbol not in symbols:
                raise OverlayTargetNotFound(
                    "Base tree does not provide symbol '{}'".format(symbol),
                    overlay=source_name, index=index, symbol=symbol
                )
            target = _node_at(result, symbols[symbol])
        else:
            target = None
            wanted = descriptor['value']
            for candidate in _iter_descendant_nodes(result.root):
                prop = candidate.get_property('phandle')
                if isinstance(prop, PropWords) and prop.data and prop.data[0] == wanted:
                    target = candidate
                    break
            if target is None:
                raise OverlayTargetNotFound('Base tree does not provide phandle {}'.format(wanted),
                                            overlay=source_name, index=index)

        _merge_node(target, overlay_node, {})

    overlay_symbols = _source_symbol_map(overlay_obj)
    for label, overlay_path in overlay_symbols.items():
        translated = None
        for fragment in _find_fragments(overlay_obj):
            fragment_path = _node_path(fragment)
            marker = fragment_path + '/__overlay__'
            if overlay_path == marker:
                descriptor = _target_descriptor(fragment, source_refs, source_symbols)
                if descriptor['kind'] == 'symbol' and descriptor['value'] in symbols:
                    translated = symbols[descriptor['value']]
                elif descriptor['kind'] == 'path':
                    translated = descriptor['value']
                break
            if overlay_path.startswith(marker + '/'):
                relative = overlay_path[len(marker):]
                descriptor = _target_descriptor(fragment, source_refs, source_symbols)
                base_path = None
                if descriptor['kind'] == 'symbol':
                    base_path = symbols.get(descriptor['value'])
                elif descriptor['kind'] == 'path':
                    base_path = descriptor['value']
                if base_path:
                    translated = base_path.rstrip('/') + relative
                    break
        if translated:
            symbols[label] = translated
            symbol_node = result.root.get_subnode(META_SYMBOLS)
            if symbol_node is None:
                symbol_node = Node(META_SYMBOLS)
                result.root.append(symbol_node)
            old = symbol_node.get_property(label)
            prop = PropStrings(label, translated)
            if old is None:
                symbol_node.append(prop)
            else:
                symbol_node.props[symbol_node.props.index(old)] = prop

    return result


def apply_overlays(base, overlays):
    current = base
    for index, overlay in enumerate(overlays):
        current = apply_overlay(current, overlay, index)
    return current


def compose_and_apply(base, overlays, **options):
    composite = compose_overlays(overlays, **options)
    return apply_overlay(base, composite)


compose_for_base = compose_and_apply
apply_composed = compose_and_apply


# Common API aliases.
merge_overlays = compose_overlays
compose_dtbo = compose_overlays
compose_dtbos = compose_overlays
merge_dtbo = compose_overlays
merge_dtbos = compose_overlays
combine_overlays = compose_overlays
build_composite_overlay = compose_overlays
synthesize_overlays = compose_overlays
compose_overlay_files = compose_overlays
merge_overlay_files = compose_overlays
compose = compose_overlays
merge = compose_overlays
combine = compose_overlays
synthesize = compose_overlays
build = compose_overlays
merge_dtbo_files = compose_overlays
compose_dtbo_files = compose_overlays
merge_overlay_files = compose_overlays
build_dtbo = compose_overlays
create_merged_dtbo = compose_overlays
combine_dtbos = compose_overlays
synthesize_dtbo = compose_overlays
process_overlays = apply_overlays
bundle_overlays = compose_overlays
assemble_overlays = compose_overlays
create_overlay_bundle = compose_overlays
merge_patches = compose_overlays
compose_patches = compose_overlays
apply_patches = apply_overlays
apply_dtbos = apply_overlays
OverlayCompositionError = OverlayComposeError
CompositionError = OverlayComposeError
FixupRecord = SourceReference
TraceRecord = SourceReference
MarkerRecord = SourceReference


class OverlayComposer:
    """Small object-oriented facade around :func:`compose_overlays`."""

    def __init__(self, *overlays, export_labels=None, labels=None, **options):
        self.overlays = list(overlays)
        self.export_labels = export_labels if export_labels is not None else labels
        self.options = options
        self.result = None

    def add(self, overlay):
        self.overlays.append(overlay)
        return self

    def compose(self, output=None, **options):
        params = dict(self.options)
        params.update(options)
        self.result = compose_overlays(
            self.overlays, output=output, export_labels=self.export_labels, **params
        )
        return self.result

    merge = compose
    run = compose


DTBOComposer = OverlayComposer
DtboComposer = OverlayComposer
PatchComposer = OverlayComposer
HandoffComposer = OverlayComposer
create_composite_overlay = compose_overlays
create_merged_dtbo = compose_overlays
