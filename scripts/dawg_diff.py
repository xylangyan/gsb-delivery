# -*- coding: utf-8 -*-
"""
Fixed-page diff support for IntCompletionDAWG books.

The on-disk format is two sections:

    uint32 unit_count, unit_count * uint32 units,
    uint32 guide_count, guide_count * 2 bytes (child, sibling)

A unit is addressed by ``index ^ offset``.  A transition to ``label`` from
``index`` exists when that target unit's low label byte equals ``label``.
The guide only controls the child/sibling walk used by key completion.
"""
from __future__ import absolute_import, unicode_literals

import hashlib
import random
import struct
import zlib

import itertools

from dawg_python import units

PAGE_SIZE = 512


class DiffError(Exception):
    pass


class LayoutInfeasible(DiffError):
    def __init__(self, message, report):
        super(LayoutInfeasible, self).__init__(message)
        self.report = report


def read_dawg(path):
    raw = open(path, 'rb').read()
    if len(raw) < 8:
        raise DiffError('file too short: %s' % path)
    n = struct.unpack(str('=I'), raw[:4])[0]
    expected = 8 + 6 * n
    if len(raw) != expected:
        raise DiffError('unexpected file length %d for %d units' % (len(raw), n))
    unit_table = list(struct.unpack(str('=%dI' % n), raw[4:4 + 4 * n]))
    guide_count = struct.unpack(str('=I'), raw[4 + 4 * n:8 + 4 * n])[0]
    if guide_count != n:
        raise DiffError('unexpected guide count %d for %d units' % (guide_count, n))
    guide = list(raw[8 + 4 * n:])
    if len(guide) != 2 * n:
        raise DiffError('unexpected guide length %d' % len(guide))
    return raw, n, unit_table, guide


def serialize_dawg(n, unit_table, guide):
    return (struct.pack(str('=I'), n)
            + struct.pack(str('=%dI' % n), *unit_table)
            + struct.pack(str('=I'), n)
            + bytes(bytearray(guide)))


def label_of(unit):
    return unit & (units.IS_LEAF_BIT | 0xFF)


def base_of(index, unit_table):
    return (index ^ units.offset(unit_table[index])) & units.PRECISION_MASK


def follow(index, label, unit_table):
    nxt = base_of(index, unit_table) ^ label
    if nxt >= len(unit_table):
        return None
    if label_of(unit_table[nxt]) != label:
        return None
    return nxt


def guide_children(index, unit_table, guide):
    out = []
    label = guide[2 * index]
    seen = set()
    while label:
        nxt = follow(index, label, unit_table)
        out.append((label, nxt))
        if nxt is None:
            break
        if nxt in seen:
            raise DiffError('guide cycle at unit %d' % index)
        seen.add(nxt)
        label = guide[2 * nxt + 1]
    return out


def completion_items(unit_table, guide, index=0, prefix=b''):
    out = []
    if units.has_leaf(unit_table[index]):
        out.append((prefix, units.value(unit_table[index ^ units.offset(unit_table[index])])))
    for label, nxt in guide_children(index, unit_table, guide):
        if nxt is None:
            raise DiffError('broken guide at %r' % prefix)
        out.extend(completion_items(unit_table, guide, nxt, prefix + bytes([label])))
    return out


def direct_items(unit_table, max_depth=64):
    """Enumerate keys accepted by the unit-level lookup, ignoring the guide."""
    out = {}

    def rec(index, key, path):
        if index in path or len(key) > max_depth:
            raise DiffError('direct traversal cycle/depth at %r' % key)
        path.add(index)
        if units.has_leaf(unit_table[index]):
            out[key] = units.value(unit_table[index ^ units.offset(unit_table[index])])
        for label in range(1, 256):
            nxt = follow(index, label, unit_table)
            if nxt is not None:
                rec(nxt, key + bytes([label]), path)
        path.remove(index)

    rec(0, b'', set())
    return out


def page_slice(data, page, page_size=PAGE_SIZE):
    return data[page * page_size:(page + 1) * page_size]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def crc32(data):
    return '%08x' % (zlib.crc32(data) & 0xFFFFFFFF)


def page_geometry(total_size, unit_count, page_size=PAGE_SIZE):
    """Return page -> (unit slots, guide slots) for the fixed page grid."""
    page_count = (total_size + page_size - 1) // page_size
    guide_start = 8 + 4 * unit_count
    geometry = {}
    for page in range(page_count):
        start = page * page_size
        end = min(start + page_size, total_size)
        unit_slots = []
        guide_slots = []
        for slot in range(unit_count):
            offset = 4 + 4 * slot
            if start <= offset < end:
                unit_slots.append(slot)
        for slot in range(unit_count):
            offset = guide_start + 2 * slot
            if start <= offset < end:
                guide_slots.append(slot)
        geometry[page] = {'unit_slots': unit_slots, 'guide_slots': guide_slots,
                          'offset': start, 'length': end - start}
    return geometry


def build_trie(target):
    """Return states for ``{bytes_key: int_value}``.

    State names are key prefixes (b'' is the root).  Each state carries its
    incoming label, ordered children, and an optional value.
    """
    raw = {b'': {'label': 0, 'children': {}, 'value': None}}
    for key, value in sorted(target.items()):
        prefix = b''
        node = raw[prefix]
        for pos, byte in enumerate(bytearray(key)):
            child = key[:pos + 1]
            node['children'][byte] = child
            if child not in raw:
                raw[child] = {'label': byte, 'children': {}, 'value': None}
            node = raw[child]
        if node['value'] is not None:
            raise DiffError('duplicate key %r' % key)
        node['value'] = value
    states = {}
    for name, node in raw.items():
        states[name] = {
            'label': node['label'],
            'children': sorted(node['children'].items()),
            'value': node['value'],
        }
    return states


def _base_map(states, slot, value_slot):
    base = {}
    for name, state in states.items():
        if state['children']:
            label, child = state['children'][0]
            base[name] = slot[child] ^ label
        else:
            base[name] = value_slot[state['value']]
    return base


def _check_phantom_edges(states, slot, base):
    owned = {slot[name]: state['label'] for name, state in states.items()}
    for name, state in states.items():
        intended = {slot[child]: label for label, child in state['children']}
        for query in range(1, 256):
            target = base[name] ^ query
            if target in owned and owned[target] == query and intended.get(target) != query:
                return False
    return True


def _encode(states, slot, value_slot, unit_count, unit_table=None, guide=None,
            clear_units=(), clear_guides=()):
    if unit_table is None:
        unit_table = [0] * unit_count
    else:
        unit_table = list(unit_table)
    if guide is None:
        guide = [0] * (2 * unit_count)
    else:
        guide = list(guide)
    for index in clear_units:
        unit_table[index] = 0
    for index in clear_guides:
        guide[2 * index] = 0
        guide[2 * index + 1] = 0
    base = _base_map(states, slot, value_slot)
    for name, state in states.items():
        index = slot[name]
        offset = (index ^ base[name]) & units.PRECISION_MASK
        unit = (offset << 10) | state['label']
        if state['value'] is not None:
            unit |= units.HAS_LEAF_BIT
        unit_table[index] = unit
    for value, index in value_slot.items():
        unit_table[index] = units.IS_LEAF_BIT | value
    for name, state in states.items():
        index = slot[name]
        guide[2 * index] = state['children'][0][0] if state['children'] else 0
    for name, state in states.items():
        for pos, (label, child) in enumerate(state['children']):
            sibling = state['children'][pos + 1][0] if pos + 1 < len(state['children']) else 0
            guide[2 * slot[child] + 1] = sibling
    return unit_table, guide, base


def _solution_ok(states, slot, value_slot, unit_count, target,
                 original_units=None, original_guide=None,
                 clear_units=(), clear_guides=()):
    if len(set(slot.values())) != len(slot):
        return False
    if len(set(value_slot.values())) != len(value_slot):
        return False
    if set(slot.values()) & set(value_slot.values()):
        return False
    base = _base_map(states, slot, value_slot)
    if any(value in (0, 1) for value in base.values()):
        return False
    for name, state in states.items():
        for label, child in state['children']:
            if (slot[child] ^ label) != base[name]:
                return False
    if not _check_phantom_edges(states, slot, base):
        return False
    unit_table, guide, _ = _encode(states, slot, value_slot, unit_count,
                                   original_units, original_guide,
                                   clear_units, clear_guides)
    try:
        if direct_items(unit_table) != target:
            return False
        completed = completion_items(unit_table, guide)
    except DiffError:
        return False
    if dict(completed) != target:
        return False
    if [key for key, value in completed] != sorted(target):
        return False
    return True


def solve_layout(target, free_unit_slots, free_guide_slots, unit_count,
                 attempts=200000, seed=20261126,
                 original_units=None, original_guide=None):
    """Find a slot layout whose nodes/values and guide entries fit the given slots."""
    states = build_trie(target)
    names = list(states)
    nonterms = [name for name in names if states[name]['children']]
    values = sorted({state['value'] for state in states.values()
                     if state['value'] is not None})
    free_unit_slots = sorted(set(free_unit_slots))
    free_guide_slots = sorted(set(free_guide_slots))

    required = {
        'states': len(names),
        'values': len(values),
        'unit_slots': len(names) + len(values),
        'guide_slots': len(names),
        'root_slot': 0,
    }
    report = {
        'required': required,
        'free_unit_slots': len(free_unit_slots),
        'free_guide_slots': len(free_guide_slots),
        'unit_slot_list': free_unit_slots,
        'guide_slot_list': free_guide_slots,
    }
    if 0 not in free_unit_slots:
        report['reason'] = 'root unit slot 0 is not writable'
        raise LayoutInfeasible(report['reason'], report)
    if len(free_unit_slots) < len(names) + len(values):
        report['reason'] = ('needs %d distinct unit slots (%d states + %d values), '
                            'only %d provided' % (len(names) + len(values), len(names),
                                                  len(values), len(free_unit_slots)))
        raise LayoutInfeasible(report['reason'], report)
    if len(free_guide_slots) < len(names):
        report['reason'] = ('needs %d guide entries, only %d provided'
                            % (len(names), len(free_guide_slots)))
        raise LayoutInfeasible(report['reason'], report)

    rng = random.Random(seed)
    for attempt in range(attempts):
        slot = {b'': 0}
        used = {0}
        queue = [b'']
        ok = True
        while queue and ok:
            name = queue.pop(0)
            children = states[name]['children']
            if not children:
                continue
            candidates = []
            for base in range(2, 256):
                child_slots = [base ^ label for label, child in children]
                if len(set(child_slots)) != len(child_slots):
                    continue
                if any(s not in free_unit_slots or s in used for s in child_slots):
                    continue
                candidates.append((base, child_slots))
            if not candidates:
                ok = False
                break
            base, child_slots = rng.choice(candidates)
            for (label, child), child_slot in zip(children, child_slots):
                slot[child] = child_slot
                used.add(child_slot)
                queue.append(child)
        if not ok or len(slot) != len(names):
            continue
        if any(slot[name] not in free_guide_slots for name in names):
            continue
        # Internal terminals: the value slot is forced to the state's base.
        base = _base_map(states, slot, {v: v for v in values})
        value_slot = {}
        impossible = False
        for name, state in states.items():
            if state['value'] is not None and state['children']:
                forced = base[name]
                if forced not in free_unit_slots or forced in used:
                    impossible = True
                    break
                if state['value'] in value_slot:
                    impossible = True
                    break
                value_slot[state['value']] = forced
        if impossible:
            continue
        free_values = [s for s in free_unit_slots
                       if s not in used and s not in value_slot.values()]
        leaf_values = [v for v in values if v not in value_slot]
        if len(free_values) < len(leaf_values):
            continue
        chosen = rng.sample(free_values, len(leaf_values))
        for value, index in zip(leaf_values, chosen):
            value_slot[value] = index
        clear_units = [s for s in free_unit_slots if s not in slot.values()
                       and s not in value_slot.values()]
        clear_guides = [s for s in free_guide_slots if s not in slot.values()]
        if _solution_ok(states, slot, value_slot, unit_count, target,
                        original_units, original_guide, clear_units, clear_guides):
            report['slot'] = {name.decode('utf-8'): index for name, index in sorted(slot.items())}
            report['value_slot'] = {str(value): index for value, index in sorted(value_slot.items())}
            report['base'] = {name.decode('utf-8'): index
                              for name, index in sorted(base.items())}
            report['attempts'] = attempt + 1
            return states, slot, value_slot, report
    report['reason'] = 'no layout found in the provided free slots'
    raise LayoutInfeasible(report['reason'], report)


def build_book(source_path, target, constraints):
    """Build a patched byte string for ``source_path`` under ``constraints``."""
    raw, unit_count, original_units, original_guide = read_dawg(source_path)
    total_size = len(raw)
    page_size = constraints.get('page_size', PAGE_SIZE)
    if constraints.get('total_size', total_size) != total_size:
        raise DiffError('total length is locked at %d, constraints say %d'
                        % (total_size, constraints.get('total_size')))
    if page_size != PAGE_SIZE:
        raise DiffError('unsupported page size %d' % page_size)

    geometry = page_geometry(total_size, unit_count, page_size)
    pages = constraints.get('pages')
    if pages is None:
        pages = [{'page': p,
                  'free_unit_slots': list(geometry[p]['unit_slots']),
                  'free_guide_slots': list(geometry[p]['guide_slots'])}
                 for p in geometry]
    else:
        pages = [dict(item) for item in pages]
        for item in pages:
            page = item['page']
            if page not in geometry:
                raise DiffError('page %d is outside the file' % page)
            default_units = geometry[page]['unit_slots']
            default_guides = geometry[page]['guide_slots']
            item['free_unit_slots'] = list(item.get('free_unit_slots', default_units))
            item['free_guide_slots'] = list(item.get('free_guide_slots', default_guides))
            item['unit_slots'] = list(default_units)
            item['guide_slots'] = list(default_guides)

    page_numbers = sorted({item['page'] for item in pages})
    by_page = {item['page']: item for item in pages}
    # Try page subsets by increasing size; the first valid subset wins.
    failures = []
    for count in range(1, len(page_numbers) + 1):
        for subset in itertools.combinations(page_numbers, count):
            free_units = []
            free_guides = []
            for page in subset:
                free_units.extend(by_page[page]['free_unit_slots'])
                free_guides.extend(by_page[page]['free_guide_slots'])
            try:
                states, slot, value_slot, report = solve_layout(
                    target, free_units, free_guides, unit_count,
                    original_units=original_units, original_guide=original_guide)
            except LayoutInfeasible as exc:
                failures.append({'pages': list(subset), 'report': exc.report})
                continue
            clear_units = [s for s in free_units if s not in slot.values()
                           and s not in value_slot.values()]
            clear_guides = [s for s in free_guides if s not in slot.values()]
            unit_table, guide, base = _encode(
                states, slot, value_slot, unit_count, original_units,
                original_guide, clear_units, clear_guides)
            patched = serialize_dawg(unit_count, unit_table, guide)
            if len(patched) != total_size:
                raise DiffError('total length changed')
            if direct_items(unit_table) != target:
                raise DiffError('direct keys do not match target')
            completed = completion_items(unit_table, guide)
            if dict(completed) != target or [k for k, v in completed] != sorted(target):
                raise DiffError('completion view does not match target')
            page_count = (total_size + page_size - 1) // page_size
            changed = [p for p in range(page_count)
                       if page_slice(raw, p, page_size) != page_slice(patched, p, page_size)]
            if changed != list(subset):
                raise DiffError('changed pages %r do not match chosen subset %r'
                                % (changed, list(subset)))
            report['pages'] = list(subset)
            report['slot'] = {k: v for k, v in report['slot'].items()}
            return raw, patched, report

    # No subset worked.  Report the pages and the jump constraints.
    raise LayoutInfeasible(
        'no in-place layout fits the provided free slots',
        {
            'required': {
                'states': len(build_trie(target)),
                'values': len({v for v in target.values()}),
                'root_unit_slot': 0,
                'root_page': 0,
                'root_guide_slot': 0,
                'guide_page': 2,
            },
            'page_size': page_size,
            'total_size': total_size,
            'pages': page_numbers,
            'geometry': {str(p): geometry[p] for p in page_numbers},
            'failures': failures,
            'jump_constraints': [
                'root unit is slot 0 (page 0)',
                'root guide entry is slot 0 (page 2 for 512-byte pages)',
                'every node base B maps child label L to slot B^L; B=0 aliases root and B=1 aliases every unused unit',
                'an internal terminal value must occupy the node base slot',
            ],
        })
