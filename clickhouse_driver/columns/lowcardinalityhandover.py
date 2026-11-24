"""
Handover of native ``LowCardinality(String)`` column fragments.

A native LowCardinality column is a dictionary plus a key segment: every
row is a key that indexes the dictionary.  When several fragments are
merged into a single ``LowCardinality(Nullable(String))`` column there
must be exactly one dictionary, so the keys of every fragment have to be
expressed against the *same* positions.

The cache that backs a handover validates whole key segments by their raw
bytes.  Rewriting a segment means producing new key bytes for all of its
rows.  This module therefore keeps the original key bytes of every
segment it can and only rewrites segments whose positions cannot be kept.

The important properties implemented here:

* Only positions actually referenced by a segment's keys are relevant.
  Unused dictionary entries (including an unused distinct value) never
  restrict another segment.
* The first slot of a nullable LowCardinality dictionary is reserved for
  NULL.  It is not the same value as the empty string.
* Positions are kept only when the value at every used position stays the
  same.  Two segments may therefore disagree about what position ``1``
  means: at most one of them can keep it verbatim.
* A segment is either handed over in full or rewritten in full.  There is
  no partial, per-key handover.
* Key width is chosen with the same boundary the driver uses when writing
  a column (``int(log(dictionary_size, 2) / 8)``, mapped to 1/2/4/8
  bytes).  Crossing one of those boundaries invalidates original key
  bytes even when the position numbers did not change.
* The produced column is read back through the driver's own reader and
  compared value by value.  A plan that cannot be verified is never
  reported as delivered.

The target dictionary is compact: slot 0 is the reserved NULL slot and
every real value occupies exactly one of the remaining positions.
"""

from io import BytesIO
from itertools import count
import struct


__all__ = [
    'HandoverError',
    'HandoverFormatError',
    'HandoverPermissionError',
    'HandoverVerificationError',
    'LowCardinalityFragment',
    'SegmentPlan',
    'HandoverPlan',
    'parse_fragment',
    'encode_fragment',
    'fragment_from_rows',
    'fragment_from_parts',
    'analyze_fragments',
    'driver_key_width',
    'plan_handover',
    'handover_fragments',
    'merge_fragments',
]


STATE_VERSION = 1

# LowCardinalityColumn.serialization_type = has_additional_keys_bit |
# need_update_dictionary.  Both bits are set by the driver on write.
SERIALIZATION_FLAGS = (1 << 9) | (1 << 10)

# Low nibble of serialization_type -> key width in bytes.
KEY_WIDTHS = {0: 1, 1: 2, 2: 4, 3: 8}
KEY_TYPES = {1: 0, 2: 1, 4: 2, 8: 3}

# LowCardinality(Nullable(String)): slot 0 is the NULL slot, all other
# slots are real strings.  Internally values are kept as bytes so that an
# empty string and a NULL can never be confused, and so that verification
# is byte exact.  The public ``dictionary``/``values`` views decode them
# to ``str`` exactly like the driver's default String reader does.
NULL = None

_STRING_FORMATS = {1: 'B', 2: 'H', 4: 'I', 8: 'Q'}


class HandoverError(Exception):
    """Base class for all handover errors."""


class HandoverFormatError(HandoverError):
    """The supplied bytes are not a native LowCardinality segment."""


class HandoverPermissionError(HandoverError):
    """
    The requested permissions cannot be honoured.

    ``segments`` contains the complete key segments (names) that cannot be
    reused simultaneously and ``conflicts`` describes why.
    """

    def __init__(self, message, segments=(), conflicts=()):
        super(HandoverPermissionError, self).__init__(message)
        self.segments = tuple(segments)
        self.conflicts = tuple(conflicts)

    @property
    def blocked_segments(self):
        return self.segments


class HandoverVerificationError(HandoverError):
    """
    A segment or the assembled column could be read back, but the values
    did not match.  Such a segment must not be reported as delivered.

    ``segment`` names the failing segment (``None`` for the assembled
    column), ``index`` is the row index inside it (or inside the column).
    """

    def __init__(self, message, segment=None, index=None,
                 expected=None, got=None, plan=None):
        super(HandoverVerificationError, self).__init__(message)
        self.segment = segment
        self.index = index
        self.expected = expected
        self.got = got
        self.plan = plan


def driver_key_width(index_size):
    """
    Return the key width in bytes that the driver writes for a dictionary
    of ``index_size`` entries.

    This deliberately mirrors ``LowCardinalityColumn._write_data``::

        int_type = int(log(len(index), 2) / 8)

    That formula is one entry more conservative than strictly necessary
    (256 entries still fit into UInt8 keys) but it is the boundary the
    cache of validated key bytes has to agree with.  The mapping jumps
    1 -> 2 bytes at 256 entries, 2 -> 4 at 65536 and 4 -> 8 at 2 ** 24.
    The integer form avoids float rounding exactly at those boundaries.
    """
    if index_size <= 0:
        raise HandoverFormatError(
            'LowCardinality dictionary must have at least one entry')
    if index_size < 1 << 8:
        return 1
    if index_size < 1 << 16:
        return 2
    if index_size < 1 << 24:
        return 4
    if index_size < 1 << 32:
        return 8
    raise HandoverFormatError(
        'Dictionary of {} entries exceeds the driver key width'
        .format(index_size))


def _read_exact(buf, size):
    data = buf.read(size)
    if len(data) != size:
        raise HandoverFormatError(
            'Truncated LowCardinality segment: wanted {} bytes, got {}'
            .format(size, len(data)))
    return data


def _read_varint(buf):
    result = 0
    shift = 0
    while True:
        byte = _read_exact(buf, 1)[0]
        result |= (byte & 0x7f) << shift
        if byte < 0x80:
            return result
        shift += 7


def _write_varint(number, buf):
    while True:
        byte = number & 0x7f
        number >>= 7
        if number:
            buf.write(bytes((byte | 0x80, )))
        else:
            buf.write(bytes((byte, )))
            return


def _read_u64(buf):
    return struct.unpack('<q', _read_exact(buf, 8))[0]


def _write_u64(number, buf):
    buf.write(struct.pack('<q', number))


def _read_string(buf):
    return _read_exact(buf, _read_varint(buf))


def _write_string(value, buf):
    _write_varint(len(value), buf)
    buf.write(value)


def _pack_keys(keys, width):
    return struct.pack(
        '<{}{}'.format(len(keys), _STRING_FORMATS[width]), *keys)


def _unpack_keys(data, width):
    count = len(data) // width
    fmt = '<{}{}'.format(count, _STRING_FORMATS[width])
    return struct.unpack(fmt, data)


def _coerce_value(value):
    if value is None:
        return NULL
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode('utf-8')
    raise TypeError(
        'LowCardinality(String) values must be str, bytes or None, got {}'
        .format(type(value).__name__))


def _decode_value(value):
    """Driver-default String view: ``str`` when UTF-8 decodable."""
    if value is NULL:
        return None
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        return value


def _display_value(value):
    if value is NULL:
        return 'NULL'
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return repr(value)
    return value


class LowCardinalityFragment(object):
    """
    One native LowCardinality column fragment.

    ``raw_dictionary`` is a tuple indexed by the original positions and
    holds ``bytes`` (or ``None`` for the reserved NULL slot).  The public
    ``dictionary`` view decodes values to ``str`` where possible.

    ``keys`` are the original position numbers, one per row.  ``key_bytes``
    are the exact bytes those keys occupied in the source, which is what a
    verbatim handover reuses.

    ``verbatim_only`` marks a segment that may be handed over as a whole
    but must not be rewritten.  Rewriting a segment always rewrites every
    key in it; this module never keeps a subset of a segment's keys.
    """

    def __init__(self, name, nullable, dictionary, keys, key_width,
                 key_bytes, source_bytes=None, verbatim_only=False,
                 rewrite_cost=None):
        self.name = name
        self.nullable = nullable
        self.raw_dictionary = tuple(
            _coerce_value(value) for value in dictionary)
        self.keys = tuple(keys)
        self.key_width = key_width
        self.key_bytes = key_bytes
        self.source_bytes = source_bytes
        self.verbatim_only = verbatim_only
        self.rewrite_cost_override = rewrite_cost

        if len(key_bytes) != len(self.keys) * key_width:
            raise HandoverFormatError(
                'Segment {!r}: key bytes length {} does not match {} keys '
                'of width {}'.format(
                    name, len(key_bytes), len(self.keys), key_width))
        for key in self.keys:
            if key < 0 or key >= len(self.raw_dictionary):
                raise HandoverFormatError(
                    'Segment {!r}: key {} is outside dictionary of size {}'
                    .format(name, key, len(self.raw_dictionary)))

    @property
    def dictionary(self):
        """Position indexed dictionary with NULL at slot 0 when nullable."""
        return tuple(
            _decode_value(value) for value in self.raw_dictionary)

    @property
    def row_count(self):
        return len(self.keys)

    @property
    def used_positions(self):
        """Dictionary positions actually referenced by this segment."""
        return tuple(sorted(set(self.keys)))

    @property
    def unused_positions(self):
        """Dictionary positions this segment never references."""
        used = set(self.keys)
        return tuple(
            position for position in range(len(self.raw_dictionary))
            if position not in used)

    @property
    def position_values(self):
        """Mapping of every used position to its raw value."""
        return {
            position: self.raw_dictionary[position]
            for position in self.used_positions}

    @property
    def raw_values(self):
        """Row values in original row order, as raw bytes."""
        return tuple(
            self.raw_dictionary[key] for key in self.keys)

    @property
    def values(self):
        """Row values in original row order, as the driver would read them."""
        return tuple(_decode_value(value) for value in self.raw_values)

    @property
    def rewrite_cost(self):
        if self.rewrite_cost_override is not None:
            return self.rewrite_cost_override
        return self.row_count

    def __repr__(self):
        return (
            '<LowCardinalityFragment {!r} rows={} dict={} width={} '
            'verbatim_only={}>'.format(
                self.name, self.row_count, len(self.raw_dictionary),
                self.key_width, self.verbatim_only))


_FRAGMENT_COUNTER = count()


def parse_fragment(data, nullable=True, name=None, has_state_prefix=True,
                   has_custom_flag=False, verbatim_only=False,
                   rewrite_cost=None):
    """
    Parse a native LowCardinality fragment.

    :param data: bytes of the column.  By default they start with the
        column state prefix (UInt64 keys serialization version) and end
        with the last key byte.
    :param nullable: whether the source column type was
        ``LowCardinality(Nullable(String))``.  The wire format of the
        LowCardinality layer itself does not carry this bit.
    :param has_custom_flag: set when the block-level "has custom
        serialization" byte was included in front of the column.
    """
    buf = BytesIO(bytes(data))

    if has_custom_flag:
        flag = _read_exact(buf, 1)[0]
        if flag:
            raise HandoverFormatError(
                'Custom serialization of LowCardinality is not supported')

    if has_state_prefix:
        version = _read_u64(buf)
        if version != STATE_VERSION:
            raise HandoverFormatError(
                'Unsupported LowCardinality keys serialization version {}'
                .format(version))

    serialization_type = _read_u64(buf)
    key_type = serialization_type & 0xf
    key_width = KEY_WIDTHS.get(key_type)
    if key_width is None:
        raise HandoverFormatError(
            'Unknown LowCardinality key type {}'.format(key_type))

    index_size = _read_u64(buf)
    if index_size < 0:
        raise HandoverFormatError(
            'Negative LowCardinality dictionary size')
    if index_size == 0:
        raw_dictionary = ()
    else:
        raw_dictionary = tuple(_read_string(buf) for _ in range(index_size))

    n_keys = _read_u64(buf)
    if n_keys < 0:
        raise HandoverFormatError('Negative LowCardinality key count')
    key_bytes = _read_exact(buf, n_keys * key_width)
    if buf.read(1):
        raise HandoverFormatError(
            'Trailing bytes after LowCardinality key segment')

    keys = _unpack_keys(key_bytes, key_width)

    if n_keys and index_size == 0:
        raise HandoverFormatError(
            'LowCardinality segment has keys but an empty dictionary')

    if nullable:
        if index_size == 0 and n_keys:
            raise HandoverFormatError(
                'Nullable LowCardinality segment has no NULL slot')
        # Slot 0 is the reserved NULL slot.  Its raw bytes are a
        # placeholder written by the driver and carry no value.
        dictionary = (NULL, ) + raw_dictionary[1:]
    else:
        dictionary = raw_dictionary

    if has_state_prefix:
        normalized = bytes(data)
    else:
        prefix = BytesIO()
        _write_u64(STATE_VERSION, prefix)
        normalized = prefix.getvalue() + bytes(data)

    if name is None:
        name = 'fragment-{}'.format(next(_FRAGMENT_COUNTER))

    return LowCardinalityFragment(
        name=name,
        nullable=nullable,
        dictionary=dictionary,
        keys=keys,
        key_width=key_width,
        key_bytes=key_bytes,
        source_bytes=normalized,
        verbatim_only=verbatim_only,
        rewrite_cost=rewrite_cost,
    )


def _encode_fragment_bytes(dictionary, keys, key_width, nullable):
    if key_width not in KEY_TYPES:
        raise HandoverFormatError(
            'Key width must be one of 1, 2, 4, 8 bytes, got {}'
            .format(key_width))
    if nullable:
        if not dictionary or dictionary[0] is not NULL:
            raise HandoverFormatError(
                'Nullable LowCardinality dictionary must start with NULL')
        slots = (b'', ) + tuple(dictionary[1:])
    else:
        slots = tuple(dictionary)
    for slot in slots:
        if not isinstance(slot, bytes):
            raise HandoverFormatError(
                'Dictionary entries must be bytes, got {!r}'.format(slot))
    if keys:
        if max(keys) >= len(slots):
            raise HandoverFormatError(
                'Key {} is outside dictionary of size {}'
                .format(max(keys), len(slots)))
        if max(keys) >= (1 << (8 * key_width)):
            raise HandoverFormatError(
                'Key {} does not fit into {} byte keys'
                .format(max(keys), key_width))

    out = BytesIO()
    _write_u64(STATE_VERSION, out)
    _write_u64(
        SERIALIZATION_FLAGS | KEY_TYPES[key_width], out)
    _write_u64(len(slots), out)
    for slot in slots:
        _write_string(slot, out)
    _write_u64(len(keys), out)
    out.write(_pack_keys(keys, key_width))
    return out.getvalue()


def _fragment_from_rows(rows, nullable, name, key_width, verbatim_only,
                        rewrite_cost):
    rows = tuple(_coerce_value(x) for x in rows)

    if nullable:
        dictionary = [NULL]
    else:
        dictionary = []
        if any(value is NULL for value in rows):
            raise ValueError(
                'NULL row in a non-nullable LowCardinality fragment')

    position_by_value = {}
    keys = []
    for value in rows:
        if value is NULL:
            if not nullable:
                raise ValueError(
                    'NULL row in a non-nullable LowCardinality fragment')
            # Reserve slot 0 for NULL, exactly like the driver writer.
            keys.append(0)
            continue
        position = position_by_value.get(value)
        if position is None:
            position = len(dictionary)
            position_by_value[value] = position
            dictionary.append(value)
        keys.append(position)

    if key_width is None:
        key_width = driver_key_width(max(1, len(dictionary)))
    elif keys and max(keys) >= (1 << (8 * key_width)):
        raise ValueError(
            'Keys do not fit into requested width {}'.format(key_width))

    data = _encode_fragment_bytes(dictionary, keys, key_width, nullable)
    return parse_fragment(
        data, nullable=nullable, name=name, has_state_prefix=True,
        verbatim_only=verbatim_only, rewrite_cost=rewrite_cost)


def fragment_from_rows(rows, nullable=True, name=None, key_width=None,
                       verbatim_only=False, rewrite_cost=None):
    """
    Build a fragment from row values (``str``, ``bytes`` or ``None``).

    This is mostly useful for tests and for synthesizing a fragment whose
    dictionary contains values that are never used by its keys.
    """
    return _fragment_from_rows(
        rows, nullable, name, key_width, verbatim_only, rewrite_cost)


LowCardinalityFragment.from_rows = staticmethod(fragment_from_rows)


def fragment_from_parts(dictionary, keys, nullable=True, name=None,
                        key_width=None, verbatim_only=False,
                        rewrite_cost=None):
    """
    Build a fragment from an explicit dictionary and key list.

    The dictionary is indexed by position.  For a nullable fragment
    position 0 must be ``None``.  Entries that are never referenced by
    ``keys`` are preserved in the dictionary (they still occupy a
    position in the source) but, as everywhere else, only used positions
    participate in the handover analysis.
    """
    dictionary = tuple(_coerce_value(value) for value in dictionary)
    keys = tuple(int(key) for key in keys)
    nullable = bool(nullable)

    if nullable:
        if not dictionary:
            dictionary = (NULL, )
        elif dictionary[0] is not NULL:
            raise HandoverFormatError(
                'A nullable LowCardinality dictionary must start with NULL '
                'at position 0')

    if key_width is None:
        key_width = driver_key_width(max(1, len(dictionary)))
    if keys and max(keys) >= (1 << (8 * key_width)):
        raise ValueError(
            'Keys do not fit into requested width {}'.format(key_width))

    data = _encode_fragment_bytes(dictionary, keys, key_width, nullable)
    return parse_fragment(
        data, nullable=nullable, name=name, has_state_prefix=True,
        verbatim_only=verbatim_only, rewrite_cost=rewrite_cost)


LowCardinalityFragment.from_parts = staticmethod(fragment_from_parts)


def encode_fragment(fragment):
    """Serialize a fragment back to its native bytes."""
    return _encode_fragment_bytes(
        fragment.raw_dictionary, fragment.keys, fragment.key_width,
        fragment.nullable)


def _all_values_in_order(fragments):
    """Distinct real values in first-use order across all fragments."""
    seen = set()
    ordered = []
    for fragment in fragments:
        for value in fragment.raw_values:
            if value is NULL or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
    return ordered


def _ineligible_reasons(fragment, dictionary_size, target_width):
    reasons = []
    if not fragment.row_count:
        return reasons

    # The driver derives the key width from the *target* dictionary size.
    # A dictionary can grow across 256/65536/2**24 entries while every
    # original position number survives; the original key bytes still
    # stop being usable because their width changed.
    if fragment.key_width != target_width:
        reasons.append(
            'original key width {} does not match target width {} for a '
            'dictionary of {} entries'.format(
                fragment.key_width, target_width, dictionary_size))

    if not fragment.nullable and 0 in fragment.used_positions:
        reasons.append(
            'a real value occupies position 0, which is the reserved NULL '
            'slot of the target column')

    seen = {}
    for position in fragment.used_positions:
        value = fragment.raw_dictionary[position]
        if value is NULL:
            continue
        if value in seen and seen[value] != position:
            reasons.append(
                'value {!r} is used at positions {} and {}; the target '
                'dictionary has one position per real value'.format(
                    _display_value(value), seen[value], position))
            break
        seen[value] = position

    max_position = max(fragment.used_positions)
    if max_position >= dictionary_size:
        reasons.append(
            'position {} is outside the {} entry target dictionary'
            .format(max_position, dictionary_size))
    return reasons


def _candidate_conflict(left, right):
    """
    Return a description of why two verbatim candidates cannot coexist,
    or ``None`` when they are compatible.

    Two segments only interact through the positions and values they
    actually use.  Unused dictionary entries are invisible here.
    """
    left_positions = left.position_values
    right_positions = right.position_values

    for position in set(left_positions) & set(right_positions):
        if left_positions[position] != right_positions[position]:
            return {
                'kind': 'position',
                'position': position,
                'segments': (left.name, right.name),
                'values': (
                    left_positions[position], right_positions[position]),
            }

    left_values = {
        value: position for position, value in left_positions.items()
        if value is not NULL}
    right_values = {
        value: position for position, value in right_positions.items()
        if value is not NULL}
    for value in set(left_values) & set(right_values):
        if left_values[value] != right_values[value]:
            return {
                'kind': 'value',
                'value': value,
                'segments': (left.name, right.name),
                'positions': (
                    left_values[value], right_values[value]),
            }
    return None


def _describe_segment(fragment, reasons=()):
    return {
        'name': fragment.name,
        'used_positions': fragment.used_positions,
        'unused_positions': fragment.unused_positions,
        'dictionary_size': len(fragment.raw_dictionary),
        'position_values': {
            position: _decode_value(value)
            for position, value in fragment.position_values.items()},
        'position_raw': fragment.position_values,
        'position_display': {
            position: _display_value(value)
            for position, value in fragment.position_values.items()},
        'row_count': fragment.row_count,
        'key_width': fragment.key_width,
        'verbatim_only': fragment.verbatim_only,
        'can_rewrite': not fragment.verbatim_only,
        'rewrite_cost': fragment.rewrite_cost,
        'verbatim_eligible': not reasons,
        'ineligible_reasons': tuple(reasons),
    }


def analyze_fragments(fragments):
    """
    Describe every fragment without building a plan.

    Returns a dictionary with the target dictionary size and key width
    implied by all values, plus per-segment usage information.  This is
    the answer to "which original positions does each segment use, which
    segments may be rewritten, and at what cost".

    ``conflicts`` lists the pairs of segments that cannot both keep their
    original positions.  Each entry names two complete key segments and
    the kind of disagreement (``position`` or ``value``).
    """
    fragments = list(fragments)
    values = _all_values_in_order(fragments)
    dictionary_size = len(values) + 1  # slot 0 is NULL
    target_width = driver_key_width(dictionary_size)

    segments = []
    for fragment in fragments:
        reasons = _ineligible_reasons(
            fragment, dictionary_size, target_width)
        description = _describe_segment(fragment, reasons)
        description['rewritten_key_bytes'] = (
            fragment.row_count * target_width)
        segments.append(description)

    conflicts = []
    for i, left in enumerate(fragments):
        for right in fragments[i + 1:]:
            conflict = _candidate_conflict(left, right)
            if conflict is not None:
                conflicts.append(conflict)

    return {
        'dictionary_size': dictionary_size,
        'key_width': target_width,
        'segments': segments,
        'conflicts': conflicts,
    }


def _permission_error(message, fragments, conflicts=()):
    names = []
    for conflict in conflicts:
        for name in conflict['segments']:
            if name not in names:
                names.append(name)
    if not names:
        names = [fragment.name for fragment in fragments]
    described = tuple(
        _describe_segment(fragment) for fragment in fragments
        if fragment.name in names)
    return HandoverPermissionError(
        message, segments=tuple(names), conflicts=tuple(conflicts)), described


def _validate_mandatory(mandatory, dictionary_size, target_width):
    conflicts = []
    for i, left in enumerate(mandatory):
        for right in mandatory[i + 1:]:
            conflict = _candidate_conflict(left, right)
            if conflict is not None:
                conflicts.append(conflict)

    if conflicts:
        pairs = ', '.join(
            '{} / {}'.format(*conflict['segments'])
            for conflict in conflicts)
        message = (
            'verbatim-only segments cannot be reused together: {}. '
            'They describe the same original position or value differently'
            .format(pairs))
        error, described = _permission_error(
            message, mandatory, conflicts)
        error.segments_info = described
        raise error

    for fragment in mandatory:
        reasons = _ineligible_reasons(
            fragment, dictionary_size, target_width)
        if reasons:
            message = (
                'verbatim-only segment {!r} cannot be handed over: {}'
                .format(fragment.name, '; '.join(reasons)))
            error, described = _permission_error(message, [fragment])
            error.segments_info = described
            raise error


def _select_extra_verbatim(candidates, mandatory, exact_limit=16,
                           search_budget=200000):
    """
    Choose a maximum-weight compatible subset of rewrite-allowed
    candidates.  Weight is the rewrite cost saved by handing the segment
    over verbatim.

    Conflicts are local: two segments only conflict when they disagree
    about a position or a value.  The compatibility graph therefore
    splits into independent connected components; each component is
    solved exactly when small enough and otherwise greedily,
    deterministically.
    """
    compatible = []
    for candidate in candidates:
        if any(_candidate_conflict(candidate, fixed) is not None
               for fixed in mandatory):
            continue
        compatible.append(candidate)

    if not compatible:
        return []

    compatible.sort(key=lambda fragment: fragment.name)
    count = len(compatible)

    neighbours = [set() for _ in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            if _candidate_conflict(compatible[i], compatible[j]) is not None:
                neighbours[i].add(j)
                neighbours[j].add(i)

    chosen = []
    seen = set()
    for start in range(count):
        if start in seen:
            continue
        component = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for other in neighbours[node]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        component.sort()

        selected = None
        if len(component) <= exact_limit:
            selected = _max_weight_independent_set(
                component, neighbours,
                [compatible[i].rewrite_cost for i in component],
                search_budget)
        if selected is None:
            selected = _greedy_independent_set(
                component, neighbours,
                [compatible[i].rewrite_cost for i in component])

        chosen.extend(compatible[i] for i in selected)

    return chosen


def _max_weight_independent_set(nodes, neighbours, weights, budget):
    """
    Exact maximum-weight independent set for one small component.

    Returns ``None`` when the search budget is exhausted so the caller
    can fall back to the greedy choice.
    """
    weight_by_node = dict(zip(nodes, weights))
    order = sorted(nodes, key=lambda i: (-weight_by_node[i], i))
    best_weight = [-1]
    best_choice = [[]]
    budget_left = [budget]

    def search(position, chosen, weight):
        budget_left[0] -= 1
        if budget_left[0] < 0:
            return
        if position == len(order):
            if weight > best_weight[0]:
                best_weight[0] = weight
                best_choice[0] = list(chosen)
            return
        node = order[position]
        if all(other not in neighbours[node] for other in chosen):
            chosen.append(node)
            search(position + 1, chosen, weight + weight_by_node[node])
            chosen.pop()
        search(position + 1, chosen, weight)

    search(0, [], 0)
    if budget_left[0] < 0:
        return None
    return best_choice[0]


def _greedy_independent_set(nodes, neighbours, weights=None):
    if weights is None:
        order = list(nodes)
    else:
        weight_by_node = dict(zip(nodes, weights))
        order = sorted(nodes, key=lambda i: (-weight_by_node[i], i))
    chosen = []
    blocked = set()
    for node in order:
        if node in blocked:
            continue
        chosen.append(node)
        blocked.add(node)
        blocked.update(neighbours[node])
    return chosen


def _build_target_dictionary(all_values, verbatim, dictionary_size):
    target = [NULL] * dictionary_size
    assigned_positions = {}
    assigned_values = {}

    for fragment in verbatim:
        for position, value in fragment.position_values.items():
            if value is NULL:
                if position != 0:
                    raise HandoverError(
                        'NULL assigned to position {}'.format(position))
                continue
            previous = assigned_positions.get(position)
            if previous is not None and previous != value:
                raise HandoverError(
                    'Internal position conflict at {}'.format(position))
            assigned_positions[position] = value

            previous_position = assigned_values.get(value)
            if previous_position is not None and previous_position != position:
                raise HandoverError(
                    'Internal value conflict for {!r}'.format(
                        _display_value(value)))
            assigned_values[value] = position
            target[position] = value

    remaining = [value for value in all_values
                 if value not in assigned_values]
    free_positions = [
        position for position in range(1, dictionary_size)
        if target[position] is NULL]
    if len(free_positions) != len(remaining):
        raise HandoverError(
            'Cannot place all values into the target dictionary: {} free '
            'positions for {} values'.format(
                len(free_positions), len(remaining)))

    for position, value in zip(free_positions, remaining):
        target[position] = value

    target = tuple(target)
    if len(set(value for value in target if value is not NULL)) != \
            len(target) - 1:
        raise HandoverError(
            'Target dictionary contains duplicate real values')
    return target


class SegmentPlan(object):
    """How one fragment is delivered into the target column."""

    HANDOVER = 'handover'
    REWRITE = 'rewrite'

    def __init__(self, fragment, action, cost, position_map,
                 key_bytes, target_key_width):
        self.fragment = fragment
        self.name = fragment.name
        self.action = action
        self.cost = cost
        self.position_map = position_map
        self.key_bytes = key_bytes
        self.target_key_width = target_key_width
        self.verified = False

    @property
    def row_count(self):
        return self.fragment.row_count

    @property
    def reused_original_keys(self):
        return self.action == self.HANDOVER

    @property
    def rewritten_key_bytes(self):
        if self.action == self.HANDOVER:
            return 0
        return self.row_count * self.target_key_width

    @property
    def rewritten(self):
        """Alias for ``action == REWRITE``."""
        return not self.reused_original_keys

    @property
    def handed_over(self):
        """Alias for ``action == HANDOVER``."""
        return self.reused_original_keys

    def __repr__(self):
        return '<SegmentPlan {!r} action={} cost={}>'.format(
            self.name, self.action, self.cost)


class HandoverPlan(object):
    """
    A verified target dictionary and a full-segment delivery list.

    ``dictionary`` has the NULL slot at index 0 and one position per real
    value.  ``segments`` preserves the input fragment order, so
    ``read()`` returns exactly the original row order.
    """

    spec = 'LowCardinality(Nullable(String))'

    def __init__(self, dictionary, key_width, segments, column_bytes):
        self.raw_dictionary = tuple(dictionary)
        self.key_width = key_width
        self.segments = list(segments)
        self.column_bytes = column_bytes
        self.verified = False
        self.delivered = False

    @property
    def dictionary(self):
        """Position indexed target dictionary with NULL at slot 0."""
        return tuple(
            _decode_value(value) for value in self.raw_dictionary)

    @property
    def target_dictionary(self):
        """Alias for :attr:`dictionary`."""
        return self.dictionary

    @property
    def handover_list(self):
        """Names of the segments delivered verbatim."""
        return tuple(segment.name for segment in self.handovers)

    @property
    def rewrite_list(self):
        """Names of the segments rewritten in full."""
        return tuple(segment.name for segment in self.rewrites)

    @property
    def handovers(self):
        return tuple(s for s in self.segments if s.reused_original_keys)

    @property
    def rewrites(self):
        return tuple(s for s in self.segments if not s.reused_original_keys)

    @property
    def dictionary_size(self):
        return len(self.raw_dictionary)

    @property
    def total_rewrite_cost(self):
        return sum(segment.cost for segment in self.rewrites)

    @property
    def total_rewrite_bytes(self):
        return sum(segment.rewritten_key_bytes for segment in self.rewrites)

    @property
    def rows(self):
        """Raw row values in the original row order."""
        rows = []
        for segment in self.segments:
            rows.extend(segment.fragment.raw_values)
        return tuple(rows)

    def report(self):
        return {
            'dictionary': self.dictionary,
            'dictionary_display': [
                _display_value(value) for value in self.raw_dictionary],
            'dictionary_size': self.dictionary_size,
            'key_width': self.key_width,
            'segments': [
                {
                    'name': segment.name,
                    'action': segment.action,
                    'cost': segment.cost,
                    'row_count': segment.row_count,
                    'used_positions':
                        segment.fragment.used_positions,
                    'position_map': segment.position_map,
                    'position_display': {
                        position: _display_value(value)
                        for position, value in
                        segment.fragment.position_values.items()},
                    'reused_original_keys':
                        segment.reused_original_keys,
                    'rewritten_key_bytes':
                        segment.rewritten_key_bytes,
                    'verified': segment.verified,
                }
                for segment in self.segments
            ],
            'total_rewrite_cost': self.total_rewrite_cost,
            'total_rewrite_bytes': self.total_rewrite_bytes,
            'verified': self.verified,
            'delivered': self.delivered,
        }

    def read(self):
        """
        Read the assembled column back through this driver.

        Returns ``None`` for the NULL slot and ``str`` for real values, in
        the original row order.  Values that are not valid UTF-8 are
        returned as ``bytes``.  Raises ``HandoverVerificationError`` if
        the bytes cannot be consumed by the driver.
        """
        rows = self.rows
        try:
            values = _read_with_library(
                self.column_bytes, len(rows), nullable=True)
        except Exception as error:
            raise HandoverVerificationError(
                'Assembled column cannot be re-read: {}'.format(error),
                plan=self)
        return tuple(values)

    def verify(self):
        """
        Re-run source and output verification.

        Marks and returns ``self`` only when every segment reads back
        exactly as declared.  On any mismatch the exception is raised and
        ``delivered`` stays ``False``.
        """
        self.verified = False
        self.delivered = False
        for segment in self.segments:
            segment.verified = False
            _verify_source(segment.fragment, plan=self)
        _verify_output(self)
        return self

    def __repr__(self):
        return (
            '<HandoverPlan dict={} width={} handovers={} rewrites={} '
            'cost={}>'.format(
                len(self.raw_dictionary), self.key_width,
                len(self.handovers), len(self.rewrites),
                self.total_rewrite_cost))


def _make_context():
    from ..context import Context

    context = Context()
    context.client_settings = {
        'use_numpy': False,
        'strings_as_bytes': False,
        'strings_encoding': 'utf-8',
        'input_format_null_as_default': False,
    }
    context.settings = {}
    return context


class _BytesReader(object):
    """
    Minimal in-memory reader used when the Cython buffered reader is not
    available at runtime.  It exposes the surface the driver's String and
    integer columns need for verification.
    """

    def __init__(self, data):
        self._data = bytes(data)
        self._pos = 0

    def read(self, size):
        start = self._pos
        end = start + size
        if end > len(self._data):
            end = len(self._data)
        self._pos = end
        return self._data[start:end]

    def read_one(self):
        value = self._data[self._pos]
        self._pos += 1
        return value

    def read_strings(self, n_items, encoding=None):
        out = []
        for _ in range(n_items):
            length = _read_varint(self)
            chunk = self.read(length)
            if encoding is not None:
                try:
                    chunk = chunk.decode(encoding)
                except UnicodeDecodeError:
                    pass
            out.append(chunk)
        return tuple(out)


def _make_reader(data):
    try:
        from ..bufferedreader import CompressedBufferedReader
    except ImportError:
        return _BytesReader(data)

    chunks = [bytes(data)]
    return CompressedBufferedReader(
        lambda: chunks.pop() if chunks else b'',
        max(1024, len(data) + 1))


def _read_with_library(data, n_rows, nullable=True):
    from .service import read_column

    spec = ('LowCardinality(Nullable(String))' if nullable
            else 'LowCardinality(String)')
    buf = _make_reader(data)
    return tuple(read_column(_make_context(), spec, n_rows, buf))


def _verify_source(fragment, plan=None):
    if fragment.source_bytes is None:
        return
    try:
        read_back = _read_with_library(
            fragment.source_bytes, fragment.row_count,
            nullable=fragment.nullable)
    except Exception as error:
        raise HandoverVerificationError(
            'Segment {!r} cannot be re-read: {}'.format(
                fragment.name, error),
            segment=fragment.name, plan=plan)

    expected = fragment.values
    if len(read_back) != len(expected):
        raise HandoverVerificationError(
            'Segment {!r} re-read {} rows, declared {}'.format(
                fragment.name, len(read_back), len(expected)),
            segment=fragment.name, plan=plan)
    for index, (want, got) in enumerate(zip(expected, read_back)):
        if want != got:
            raise HandoverVerificationError(
                'Segment {!r} row {} re-read as {} but the segment declares '
                '{}'.format(
                    fragment.name, index, _display_value(got),
                    _display_value(want)),
                segment=fragment.name, index=index,
                expected=want, got=got, plan=plan)


def _verify_output(plan):
    expected = tuple(
        _decode_value(value) for value in plan.rows)
    try:
        read_back = _read_with_library(
            plan.column_bytes, len(expected), nullable=True)
    except Exception as error:
        raise HandoverVerificationError(
            'Assembled column cannot be re-read: {}'.format(error),
            plan=plan)

    if len(read_back) != len(expected):
        raise HandoverVerificationError(
            'Assembled column re-read {} rows, expected {}'.format(
                len(read_back), len(expected)), plan=plan)

    offset = 0
    for segment in plan.segments:
        count = segment.row_count
        for index in range(count):
            want = expected[offset + index]
            got = read_back[offset + index]
            if want != got:
                raise HandoverVerificationError(
                    'Segment {!r} row {} re-read as {} but expected {}'
                    .format(
                        segment.name, index, _display_value(got),
                        _display_value(want)),
                    segment=segment.name, index=index,
                    expected=want, got=got, plan=plan)
        segment.verified = True
        offset += count

    plan.verified = True
    plan.delivered = True


def plan_handover(fragments, nullable=True, verify=True):
    """
    Build a minimum-rewrite-cost handover plan.

    All fragments are merged into ``LowCardinality(Nullable(String))``.
    A segment is handed over verbatim when all of its used positions keep
    their value and its original key width equals the target key width.
    Otherwise the whole segment is rewritten.

    ``verbatim_only`` fragments must be handed over verbatim.  If they
    conflict, or if one of them cannot be kept, ``HandoverPermissionError``
    is raised and names the complete key segments that cannot be used
    together.

    With ``verify=True`` (the default) every source segment and the
    assembled column are read back through this driver.  A NULL, empty
    string or ordinary value mismatch raises
    ``HandoverVerificationError`` and the segment is not marked delivered.
    """
    if not nullable:
        raise ValueError(
            'A handover target must be LowCardinality(Nullable(String))')

    fragments = list(fragments)
    if not fragments:
        dictionary = (NULL, )
        data = _encode_fragment_bytes(dictionary, [], 1, True)
        plan = HandoverPlan(dictionary, 1, [], data)
        if verify:
            _verify_output(plan)
        return plan

    for fragment in fragments:
        if fragment.nullable and fragment.raw_dictionary and \
                fragment.raw_dictionary[0] is not NULL:
            raise HandoverFormatError(
                'Nullable segment {!r} has no NULL slot'
                .format(fragment.name))

    names = [fragment.name for fragment in fragments]
    if len(set(names)) != len(names):
        duplicates = sorted(
            {name for name in names if names.count(name) > 1})
        raise HandoverFormatError(
            'Segment names must be unique, duplicated: {}'
            .format(', '.join(duplicates)))

    all_values = _all_values_in_order(fragments)
    dictionary_size = len(all_values) + 1
    target_width = driver_key_width(dictionary_size)

    mandatory = [f for f in fragments if f.verbatim_only]
    optional = [f for f in fragments if not f.verbatim_only]

    _validate_mandatory(mandatory, dictionary_size, target_width)

    # Optional segments that are structurally impossible to keep are
    # simply rewritten.  Among the rest choose the maximum saved cost.
    candidates = [
        fragment for fragment in optional
        if not _ineligible_reasons(
            fragment, dictionary_size, target_width)]
    extra = _select_extra_verbatim(candidates, mandatory)
    verbatim = mandatory + extra
    verbatim_ids = {id(fragment) for fragment in verbatim}

    target = _build_target_dictionary(
        all_values, verbatim, dictionary_size)
    value_positions = {
        value: position
        for position, value in enumerate(target)
        if value is not NULL}

    segments = []
    key_chunks = []
    total_rows = 0

    for fragment in fragments:
        if id(fragment) in verbatim_ids:
            # Reuse the exact original key bytes.  _build_target_dictionary
            # guaranteed every used position keeps its value.
            position_map = {
                position: position
                for position in fragment.used_positions}
            key_bytes = fragment.key_bytes
            action = SegmentPlan.HANDOVER
            cost = 0
        else:
            mapped_keys = []
            position_map = {}
            for position in fragment.used_positions:
                value = fragment.raw_dictionary[position]
                target_position = (
                    0 if value is NULL
                    else value_positions[value])
                position_map[position] = target_position
            for key in fragment.keys:
                value = fragment.raw_dictionary[key]
                mapped_keys.append(
                    0 if value is NULL else value_positions[value])
            key_bytes = _pack_keys(mapped_keys, target_width)
            action = SegmentPlan.REWRITE
            cost = fragment.rewrite_cost

        segment = SegmentPlan(
            fragment=fragment,
            action=action,
            cost=cost,
            position_map=position_map,
            key_bytes=key_bytes,
            target_key_width=target_width)
        segments.append(segment)
        key_chunks.append(key_bytes)
        total_rows += fragment.row_count

    data = _encode_target_column(
        target, key_chunks, total_rows, target_width)
    plan = HandoverPlan(target, target_width, segments, data)

    if verify:
        for fragment in fragments:
            _verify_source(fragment, plan=plan)
        _verify_output(plan)

    return plan


handover_fragments = plan_handover
merge_fragments = plan_handover


def _encode_target_column(dictionary, key_chunks, total_rows, key_width):
    combined = b''.join(key_chunks)
    if len(combined) != total_rows * key_width:
        raise HandoverError(
            'Assembled key segment has {} bytes, expected {}'
            .format(len(combined), total_rows * key_width))

    out = BytesIO()
    _write_u64(STATE_VERSION, out)
    _write_u64(SERIALIZATION_FLAGS | KEY_TYPES[key_width], out)
    _write_u64(len(dictionary), out)
    for position, value in enumerate(dictionary):
        if value is NULL:
            if position != 0:
                raise HandoverError(
                    'NULL outside the reserved slot at {}'.format(position))
            slot = b''
        else:
            slot = value
        _write_string(slot, out)
    _write_u64(total_rows, out)
    out.write(combined)
    return out.getvalue()
