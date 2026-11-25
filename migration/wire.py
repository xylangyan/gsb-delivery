"""Wire-level simulation of readers, built on the real umsgpack.

These helpers turn a handover sheet plus concrete field values into
actual MessagePack frames and decode them the way a reader would —
with passed ``ext_handlers`` and/or registered ext types — so test
suites and audits can confirm that the symbolic checks match the
bytes on the wire.

Frame convention for one merged-map entry of a table version:

* the map key is the (possibly nested) array of key elements; every
  field-backed element is packed as ``Ext(code, packb(["k", j, raw]))``
  where ``j`` is the element's position in the flattened key;
* the map value is packed as ``Ext(code, packb(["v", raw]))``.

The ext handler reconstructed from the sheet applies the attached
conversion rules (查表 / 符号扩展 / 整数缩放) and returns the
fixed-type business value, exactly as the on-site processors do.
"""

from __future__ import annotations

import umsgpack

from . import spec as sp
from .checks import apply_rule, _flatten


def _flatten_key_spec(key):
    return [elem for _, elem in _flatten(key)]


def pack_entry(handover, segment, table_id, field_values):
    """Pack one record of a table version into (key, value) Python objects
    ready for ``umsgpack.packb``."""
    table = handover.table(segment, table_id)
    template = handover.segments[segment].template
    code = table.ext_code
    flat = _flatten_key_spec(table.key)
    position = [0]

    def build(key_spec):
        out = []
        for item in key_spec:
            if isinstance(item, tuple):
                out.append(build(item))
            else:
                idx = position[0]
                position[0] += 1
                if item.is_const:
                    out.append(item.const)
                else:
                    field = template.fields[item.field]
                    raw = field_values[item.field]
                    if field.kind == sp.KIND_TIMESTAMP:
                        out.append(raw)
                    else:
                        payload = umsgpack.packb(["k", idx, raw])
                        out.append(umsgpack.Ext(code, payload))
        return out

    key = build(table.key)
    value_spec = table.value
    if value_spec.is_const:
        value = value_spec.const
    else:
        field = template.fields[value_spec.field]
        raw = field_values[value_spec.field]
        if field.kind == sp.KIND_TIMESTAMP:
            value = raw
        else:
            value = umsgpack.Ext(code, umsgpack.packb(["v", raw]))
    return key, value


def make_ext_handler(handover, segment, table_id):
    """Reconstruct the ext handler a reader would run for this table."""
    table = handover.table(segment, table_id)
    flat = _flatten_key_spec(table.key)
    value_spec = table.value

    def handler(ext):
        tag = umsgpack.unpackb(ext.data)
        if tag[0] == "k":
            elem = flat[tag[1]]
            value = tag[2]
            for rule in elem.rules:
                value = apply_rule(value, rule)
            return value
        value = tag[1]
        for rule in value_spec.rules:
            value = apply_rule(value, rule)
        return value

    return handler


def pack_frame(handover, segment, table_id, records):
    """Pack one collector frame: a map of the given records."""
    mapping = {}
    for values in records:
        key, value = pack_entry(handover, segment, table_id, values)
        mapping[tuple(_freeze(key))] = value
    return umsgpack.packb(mapping)


def _freeze(key):
    out = []
    for item in key:
        if isinstance(item, list):
            out.append(tuple(_freeze(item)))
        else:
            out.append(item)
    return out


def reader_ext_handlers(handover, active_codes):
    """Build the ext_handlers dict a reader would pass, mapping each
    active code to the handler of the table that owns it."""
    handlers = {}
    for seg in handover.segments.values():
        for table in seg.tables.values():
            if table.ext_code in active_codes:
                handlers[table.ext_code] = make_ext_handler(
                    handover, seg.name, table.table_id)
    return handlers


def simulate_merged_map(handover, entries, active_codes):
    """Simulate the 合单 at a reader.

    ``entries`` is a list of ``(segment, table_id, field_values)``; every
    entry travels as its own MessagePack frame (so umsgpack's
    duplicate-key guard inside one frame does not fire), each frame is
    unpacked with the reader's active handlers, and the decoded maps are
    merged into one dict — where a key collision silently drops an
    entry (只剩一项).
    """
    handlers = reader_ext_handlers(handover, active_codes)
    merged = {}
    for seg, table_id, values in entries:
        blob = pack_frame(handover, seg, table_id, [values])
        decoded = umsgpack.unpackb(blob, ext_handlers=handlers)
        merged.update(decoded)
    return merged
