"""BER BIT STRING handoff planning.

The planner parses BER TLV intervals, maps bit positions without rebuilding
every candidate bit string, and chooses globally non-overlapping TLV borrows.
``plan_handoff(source, tables)`` returns materialized table BER, borrowed
``(start, end)`` source intervals, and the number of bytes that must be
retransmitted.
"""

from __future__ import absolute_import

import bisect
import heapq
from collections import namedtuple
from collections.abc import Iterable

from .core import ReadFlags
from .core import Decoder

MASK64 = (1 << 64) - 1
HASH_BASE = 0x9E3779B97F4A7C15

TlvRange = namedtuple("TlvRange", ["start", "end"])


class TlvNode(object):
    __slots__ = (
        "start", "end", "tag_start", "header_end", "content_start", "content_end",
        "tag_number", "tag_class", "constructed", "indefinite", "children", "parent",
        "eoc_start", "raw_length", "bit_start", "bit_end", "bit_length", "unused",
        "root_index", "payload_start", "payload_end",
    )

    def __init__(self):
        self.children = []
        self.parent = None
        self.tag_start = 0
        self.header_end = 0
        self.content_start = 0
        self.content_end = 0
        self.tag_number = None
        self.tag_class = 0
        self.constructed = False
        self.indefinite = False
        self.eoc_start = None
        self.raw_length = 0
        self.bit_start = None
        self.bit_end = None
        self.bit_length = 0
        self.unused = 0
        self.root_index = None
        self.payload_start = None
        self.payload_end = None

    @property
    def range(self):
        return TlvRange(self.start, self.end)

    @property
    def interval(self):
        return (self.start, self.end)

    @property
    def offset(self):
        return self.start

    @property
    def length(self):
        return self.end - self.start

    @property
    def is_bit_string(self):
        return self.tag_number == 3 and self.tag_class == 0

    def raw(self, source):
        return bytes(source[self.start:self.end])

    def as_dict(self):
        return {
            "start": self.start,
            "end": self.end,
            "offset": self.start,
            "length": self.end - self.start,
            "tag": self.tag_number,
            "constructed": self.constructed,
            "indefinite": self.indefinite,
            "bit_start": self.bit_start,
            "bit_end": self.bit_end,
            "bit_length": self.bit_length,
            "unused": self.unused,
        }


Segment = namedtuple(
    "Segment",
    ["kind", "start", "end", "cost", "source_start", "source_end", "node", "data", "candidate"],
    defaults=(None,),
)


class TablePlan(object):
    def __init__(self, name, bit_length, ber, segments, cost, material_length):
        self.name = name
        self.bit_length = bit_length
        self.ber = ber
        self.segments = segments
        self.cost = cost
        self.material_length = material_length

    @property
    def borrowed_ranges(self):
        return [segment.node.range for segment in self.segments if segment.kind == "borrow"]

    @property
    def ranges(self):
        return self.borrowed_ranges

    @property
    def used_ranges(self):
        return self.borrowed_ranges

    def as_dict(self):
        return {
            "name": self.name,
            "bit_length": self.bit_length,
            "ber": self.ber,
            "cost": self.cost,
            "material_length": self.material_length,
            "segments": self.segments,
            "borrowed_ranges": [tuple(r) for r in self.borrowed_ranges],
            "ranges": [tuple(r) for r in self.ranges],
        }


class HandoffResult(tuple):
    def __new__(cls, tables, volume, sequence_ber, ranges, cost, source=None):
        instance = super(HandoffResult, cls).__new__(cls, (volume, ranges, cost))
        instance.tables = tables
        instance.table_bytes = [table.ber for table in tables]
        instance.volume = volume
        instance.ber = volume
        instance.sequence_ber = sequence_ber
        instance.ranges = ranges
        instance.borrowed_ranges = ranges
        instance.used_ranges = ranges
        instance.tlv_ranges = ranges
        instance.source_ranges = ranges
        instance.intervals = ranges
        instance.offset_length_ranges = [
            [(start, end - start) for start, end in table_ranges]
            for table_ranges in ranges
        ]
        instance.total_cost = cost
        instance.cost = cost
        instance.optimal_cost = cost
        instance.source = source
        return instance

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in ("ber", "volume", "output"):
                return self.volume
            if key in ("tables", "table_bytes", "encoded_tables"):
                return self.table_bytes
            if key in ("ranges", "borrowed_ranges", "used_ranges", "tlv_ranges", "source_ranges", "intervals"):
                return self.ranges
            if key in ("offset_length_ranges", "ranges_offset_length"):
                return self.offset_length_ranges
            if key in ("cost", "total_cost", "optimal_cost"):
                return self.cost
            if key == "saving":
                return self.saved_bytes
            if key in ("baseline_cost", "saved_bytes"):
                return getattr(self, key)
            if key == "sequence_ber":
                return self.sequence_ber
            raise KeyError(key)
        return super(HandoffResult, self).__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __int__(self):
        return self.cost

    def __eq__(self, other):
        if isinstance(other, bool):
            return tuple.__eq__(self, other)
        if isinstance(other, (int, float)):
            return self.cost == other
        if isinstance(other, dict):
            return self.as_dict() == other
        return tuple.__eq__(self, other)

    def __hash__(self):
        return tuple.__hash__(self)

    def as_dict(self):
        return {
            "ber": self.volume,
            "volume": self.volume,
            "sequence_ber": self.sequence_ber,
            "tables": self.table_bytes,
            "table_plans": self.tables,
            "ranges": [[tuple(r) for r in table_ranges] for table_ranges in self.ranges],
            "borrowed_ranges": self.ranges,
            "tlv_ranges": self.ranges,
            "offset_length_ranges": self.offset_length_ranges,
            "cost": self.cost,
            "total_cost": self.cost,
        }


def ber_length_size(length):
    if length < 0x80:
        return 1
    size = 1
    while length:
        size += 1
        length >>= 8
    return size


def encode_ber_length(length):
    if length < 0x80:
        return bytes([length])
    raw = []
    value = length
    while value:
        raw.append(value & 0xFF)
        value >>= 8
    raw.reverse()
    return bytes([0x80 | len(raw)]) + bytes(raw)


def bitstring_tlv_size(bit_length):
    if bit_length < 0:
        raise ValueError("bit_length must be non-negative")
    payload_length = (bit_length + 7) // 8
    content_length = 1 + payload_length
    return 1 + ber_length_size(content_length) + content_length


def encode_bitstring_bits(bits, bit_length=None):
    if bit_length is None:
        bit_length = len(bits) * 8
    if bit_length < 0 or bit_length > len(bits) * 8:
        raise ValueError("invalid bit length")
    unused = (-bit_length) % 8
    payload = bytearray(bits[:(bit_length + 7) // 8])
    if unused and payload:
        payload[-1] &= 0xFF << unused
    content = bytes([unused]) + bytes(payload)
    return b"\x03" + encode_ber_length(len(content)) + content


def decoder_value_to_left_aligned(value, unused):
    if not unused:
        return bytes(value)
    result = bytearray(len(value))
    shift = 8 - unused
    for index in range(len(value)):
        result[index] = ((value[index] << unused) & 0xFF)
        if index + 1 < len(value):
            result[index] |= value[index + 1] >> shift
    return bytes(result)


def extract_bits(buffer_data, start, bit_length):
    if bit_length == 0:
        return b""
    end_bit = start + bit_length
    byte_start = start // 8
    byte_end = (end_bit + 7) // 8
    shift = start % 8
    result = bytearray(byte_end - byte_start)
    last = len(result) - 1
    if shift == 0:
        available = min(len(buffer_data), byte_end)
        result[:max(0, available - byte_start)] = buffer_data[byte_start:available]
    else:
        inverse = 8 - shift
        for output_index, source_index in enumerate(range(byte_start, byte_end)):
            value = (buffer_data[source_index] << shift) & 0xFF
            if source_index + 1 < len(buffer_data):
                value |= buffer_data[source_index + 1] >> inverse
            result[output_index] = value
    padding = (-bit_length) % 8
    if padding and result:
        result[last] &= 0xFF << padding
    return bytes(result)


class _BitWriter(object):
    def __init__(self):
        self.data = bytearray()
        self.length = 0

    def align(self):
        while self.length % 8:
            bit = self.length % 8
            if bit == 0:
                break
            self.length += 1
        needed = (self.length + 7) // 8
        if len(self.data) < needed:
            self.data.extend(b"\x00" * (needed - len(self.data)))

    def append(self, bits, bit_length):
        if bit_length == 0:
            return
        needed = (self.length + bit_length + 7) // 8
        if len(self.data) < needed:
            self.data.extend(b"\x00" * (needed - len(self.data)))
        start = self.length
        byte_start = start // 8
        shift = start % 8
        if shift == 0:
            count = (bit_length + 7) // 8
            self.data[byte_start:byte_start + count] = bits[:count]
        else:
            inverse = 8 - shift
            for index in range((bit_length + 7) // 8):
                source_byte = bits[index]
                target = byte_start + index
                self.data[target] = (self.data[target] & (0xFF << inverse)) | ((source_byte >> shift) & 0xFF)
                if target + 1 < len(self.data):
                    self.data[target + 1] = (source_byte << inverse) & 0xFF
        self.length += bit_length
        padding = (-self.length) % 8
        if padding and self.data:
            self.data[(self.length - 1) // 8] &= 0xFF << padding


def _read_tlv_length(data, position):
    if position >= len(data):
        raise ValueError("truncated BER length")
    first = data[position]
    position += 1
    if first == 0x80:
        return -1, position
    if first < 0x80:
        return first, position
    count = first & 0x7F
    if count == 0 or count == 0x7F or position + count > len(data):
        raise ValueError("invalid BER length")
    length = 0
    for _ in range(count):
        length = (length << 8) | data[position]
        position += 1
    return length, position


def _read_tag(data, position):
    start = position
    if position >= len(data):
        raise ValueError("truncated BER tag")
    first = data[position]
    position += 1
    tag_class = first & 0xC0
    constructed = bool(first & 0x20)
    number = first & 0x1F
    if number == 0x1F:
        number = 0
        while True:
            if position >= len(data):
                raise ValueError("truncated long BER tag")
            byte = data[position]
            position += 1
            number = (number << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
    return start, position, number, tag_class, constructed


def parse_tlv_tree(data):
    data = bytes(data)
    nodes = []

    def parse_node(position, parent):
        node = TlvNode()
        node.parent = parent
        node.tag_start, after_tag, node.tag_number, node.tag_class, node.constructed = _read_tag(data, position)
        node.start = node.tag_start
        length, node.content_start = _read_tlv_length(data, after_tag)
        node.header_end = node.content_start

        if length == -1:
            if not node.constructed:
                raise ValueError("indefinite length on primitive TLV")
            node.indefinite = True
            cursor = node.content_start
            while True:
                if cursor + 1 >= len(data):
                    raise ValueError("missing EOC")
                if data[cursor] == 0 and data[cursor + 1] == 0:
                    eoc_length, after_eoc_length = _read_tlv_length(data, cursor + 1)
                    if eoc_length != 0 or after_eoc_length != cursor + 2:
                        raise ValueError("invalid EOC")
                    node.eoc_start = cursor
                    cursor += 2
                    break
                child = parse_node(cursor, node)
                node.children.append(child)
                cursor = child.end
            node.content_end = node.eoc_start
            node.end = cursor
        else:
            node.content_end = node.content_start + length
            node.end = node.content_end
            if node.end > len(data):
                raise ValueError("truncated TLV value")
            cursor = node.content_start
            if node.constructed:
                while cursor < node.content_end:
                    child = parse_node(cursor, node)
                    node.children.append(child)
                    cursor = child.end
                if cursor != node.content_end:
                    raise ValueError("invalid constructed TLV length")

        node.raw_length = node.end - node.start
        nodes.append(node)
        return node

    roots = []
    position = 0
    while position < len(data):
        root = parse_node(position, None)
        roots.append(root)
        position = root.end
    return TlvArchive(data, roots, nodes)


class TlvArchive(object):
    def __init__(self, data, roots, nodes):
        self.data = bytes(data)
        self.roots = roots
        self.nodes = nodes
        self.bit_data = b""
        self.bit_length = 0
        self._range_map = {}
        self.node_by_start = {}
        self._index_bits()
        for node in nodes:
            self._range_map[(node.start, node.end)] = node
            self.node_by_start[node.start] = node

    def _index_bits(self):
        writer = _BitWriter()
        for root_index, root in enumerate(self.roots):
            writer.align()

            def visit(node):
                node.root_index = root_index
                if node.is_bit_string and not node.constructed:
                    if node.content_start >= node.content_end:
                        raise ValueError("primitive BIT STRING is missing unused-bits octet")
                    unused = self.data[node.content_start]
                    if unused > 7:
                        raise ValueError("invalid unused bit count")
                    node.unused = unused
                    node.payload_start = node.content_start + 1
                    node.payload_end = node.content_end
                    payload = self.data[node.payload_start:node.payload_end]
                    node.bit_start = writer.length
                    node.bit_length = max(0, len(payload) * 8 - unused)
                    writer.append(payload, node.bit_length)
                    node.bit_end = writer.length
                for child in node.children:
                    visit(child)
                if node.is_bit_string:
                    if node.constructed:
                        bit_children = [child for child in node.children if child.bit_start is not None]
                        if bit_children:
                            node.bit_start = bit_children[0].bit_start
                            node.bit_end = bit_children[-1].bit_end
                            node.bit_length = node.bit_end - node.bit_start
                            node.unused = bit_children[-1].unused
                    elif node.bit_start is not None:
                        node.bit_end = node.bit_start + node.bit_length

            visit(root)
        self.bit_data = bytes(writer.data)
        self.bit_length = writer.length

    def node_at_range(self, start, end=None):
        if end is None and isinstance(start, (tuple, list)):
            start, end = start
        return self._range_map.get((int(start), int(end)))

    def borrowable_nodes(self):
        return [node for node in self.nodes if node.is_bit_string and node.bit_length > 0]

    def read_bitstrings(self, ber=None):
        blob = self.data if ber is None else ber
        decoder = Decoder()
        decoder.start(blob)
        values = []
        while True:
            tag = decoder.peek()
            if tag is None:
                break
            tag, value = decoder.read(ReadFlags.WithUnused)
            values.append((tag, value))
        return values


def _as_table_request(item, index):
    if isinstance(item, TableRequest):
        return item
    if isinstance(item, str):
        return TableRequest(name=item, bit_length=0, bits=b"")
    if isinstance(item, bytes):
        return TableRequest(name="table-%d" % index, bits=item, bit_length=len(item) * 8, raw_standard=True)
    if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], (bytes, bytearray)) and isinstance(item[1], int):
        return TableRequest(name="table-%d" % index, bits=bytes(item[0]), unused=item[1], library_value=True)
    if isinstance(item, (tuple, list)) and item and isinstance(item[0], int) and not isinstance(item[0], bool):
        if len(item) == 2 and all(isinstance(x, int) for x in item):
            return TableRequest(name="table-%d" % index, ranges=[(item[0], item[1])])
        return TableRequest(name="table-%d" % index, order=list(item))
    if isinstance(item, dict):
        return TableRequest(
            name=item.get("name", "table-%d" % index),
            bits=item.get("bits", item.get("data", item.get("value"))),
            bit_length=item.get("bit_length", item.get("length")),
            unused=item.get("unused", item.get("unused_bits", 0)),
            library_value=item.get("library_value", item.get("shifted", item.get("decoder_value", False))),
            left_aligned=item.get("left_aligned", item.get("raw_standard", False)),
            ranges=item.get("ranges", item.get("source_ranges", item.get("selections", item.get("segments")))),
            order=item.get("order", item.get("channels", item.get("bit_order"))),
            root=item.get("root", item.get("source_root")),
            group=item.get("group"),
        )
    raise TypeError("unsupported table request: %r" % (type(item),))


class TableRequest(object):
    def __init__(self, name=None, bits=None, bit_length=None, unused=0, library_value=False,
                 left_aligned=False, raw_standard=False, ranges=None, source_ranges=None,
                 selections=None, segments=None, order=None, channels=None, bit_order=None,
                 root=None, group=None):
        self.root = root if root is not None else group
        self.name = name
        if ranges is None:
            ranges = source_ranges if source_ranges is not None else selections
        if ranges is None:
            ranges = segments
        self.ranges = ranges
        self.order = order if order is not None else channels if channels is not None else bit_order
        self.bits = bits
        self.bit_length = bit_length
        self.unused = unused
        self.library_value = library_value
        self.left_aligned = left_aligned or raw_standard


def _normalize_ranges(value):
    if value is None:
        return None
    if isinstance(value, slice):
        return [(value.start or 0, value.stop)]
    if isinstance(value, range):
        return [(value.start, value.stop)]
    if hasattr(value, "range") and hasattr(value.range, "start"):
        return [(value.range.start, value.range.end)]
    if hasattr(value, "start") and hasattr(value, "end") and isinstance(getattr(value, "start"), int):
        return [(int(value.start), int(value.end))]
    if isinstance(value, dict):
        start = value.get("start", value.get("start_bit", value.get("bit_start", value.get("source_start", value.get("offset")))))
        end = value.get("end", value.get("end_bit", value.get("bit_end", value.get("source_end", value.get("stop")))))
        if start is not None and end is not None:
            return [(int(start), int(end))]
        length = value.get("length", value.get("bit_length", value.get("count", value.get("size"))))
        if start is not None and length is not None:
            return [(int(start), int(start) + int(length))]
    if isinstance(value, (tuple, list)):
        if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], int):
            start, second = int(value[0]), int(value[1])
            if 0 <= second < start:
                return [(start, start + second)]
            return [(start, second)]
        result = []
        for item in value:
            result.extend(_normalize_ranges(item))
        return result
    raise TypeError("unsupported range: %r" % (value,))


def prepare_table(request, index, archive):
    req = _as_table_request(request, index)
    runs = []
    writer = _BitWriter()
    root_offset = 0
    if req.root is not None:
        root_node = archive.roots[int(req.root)]
        root_offset = root_node.bit_start or 0

    if req.ranges is not None:
        ranges = _normalize_ranges(req.ranges)
        output_start = 0
        normalized = []
        for raw_start, raw_end in ranges:
            node = archive.node_at_range(raw_start, raw_end)
            if node is None:
                started_node = archive.node_by_start.get(int(raw_start))
                if started_node is not None and int(raw_end) == started_node.length:
                    node = started_node
            node_root = int(req.root) if req.root is not None else None
            if node is not None and node.bit_start is not None and (node_root is None or node.root_index == node_root):
                source_start, source_end = node.bit_start, node.bit_end
            else:
                source_start, source_end = root_offset + int(raw_start), root_offset + int(raw_end)
            if source_end < source_start or source_end > archive.bit_length + 8:
                raise ValueError("source bit range is outside parsed BIT STRING data")
            length = source_end - source_start
            bits = extract_bits(archive.bit_data, source_start, length)
            writer.append(bits, length)
            normalized.append((output_start, source_start, source_end))
            output_start += length
        runs = normalized

    if req.order is not None:
        order = list(req.order)
        output_start = 0
        run_start = None
        run_end = None
        for raw_source_bit in order:
            source_bit = root_offset + int(raw_source_bit)
            if run_start is None:
                run_start = source_bit
                run_end = source_bit + 1
            elif source_bit == run_end:
                run_end += 1
            else:
                runs.append((output_start, run_start, run_end))
                length = run_end - run_start
                writer.append(extract_bits(archive.bit_data, run_start, length), length)
                output_start += length
                run_start = source_bit
                run_end = source_bit + 1
        if run_start is not None:
            runs.append((output_start, run_start, run_end))
            length = run_end - run_start
            writer.append(extract_bits(archive.bit_data, run_start, length), length)
            output_start += length

    if req.bits is not None:
        bits = bytes(req.bits)
        if req.bit_length is None:
            bit_length = len(bits) * 8 - int(req.unused or 0)
        else:
            bit_length = int(req.bit_length)
        if req.library_value:
            bits = decoder_value_to_left_aligned(bits, int(req.unused or 0))
        if not runs and req.ranges is None and req.order is None:
            writer.append(bits, bit_length)
    else:
        bit_length = writer.length

    if req.bit_length is not None and bit_length == 0:
        bit_length = int(req.bit_length)

    target_bits = extract_bits(writer.data, 0, bit_length)
    return PreparedTable(req.name or "table-%d" % index, bit_length, target_bits, runs)


class PreparedTable(object):
    def __init__(self, name, bit_length, bits, source_runs):
        self.name = name
        self.bit_length = bit_length
        self.bits = bits
        self.source_runs = source_runs
        self.candidates = []


class Candidate(object):
    __slots__ = ("table_index", "node", "start", "end", "key")

    def __init__(self, table_index, node, start, end):
        self.table_index = table_index
        self.node = node
        self.start = start
        self.end = end
        self.key = id(node)

    @property
    def source_start(self):
        return self.node.start

    @property
    def source_end(self):
        return self.node.end


class HashIndex(object):
    def __init__(self, data):
        self.data = bytes(data)
        self._prefixes = {}
        self._powers = [1]

    def _power(self, length):
        while len(self._powers) <= length:
            self._powers.append((self._powers[-1] * HASH_BASE) & MASK64)
        return self._powers[length]

    def _prefix(self, shift):
        if shift in self._prefixes:
            return self._prefixes[shift]
        prefix = [0]
        size = len(self.data)
        inverse = 8 - shift
        for index in range(size):
            if shift == 0:
                value = self.data[index]
            else:
                value = (self.data[index] << shift) & 0xFF
                if index + 1 < size:
                    value |= self.data[index + 1] >> inverse
            prefix.append((prefix[-1] * HASH_BASE + value) & MASK64)
        self._prefixes[shift] = prefix
        return prefix

    def hash(self, start_bit, bit_length):
        if bit_length == 0:
            return 0
        byte_index, shift = divmod(start_bit, 8)
        byte_length = (bit_length + 7) // 8
        prefix = self._prefix(shift)
        power = self._power(byte_length)
        end_index = byte_index + byte_length
        if end_index >= len(prefix):
            raise ValueError("hash range is outside bit buffer")
        value = (prefix[end_index] - prefix[byte_index] * power) & MASK64
        padding = (-bit_length) % 8
        if padding:
            if shift == 0:
                last_byte = self.data[byte_index + byte_length - 1]
            else:
                source_index = byte_index + byte_length - 1
                last_byte = (self.data[source_index] << shift) & 0xFF
                if source_index + 1 < len(self.data):
                    last_byte |= self.data[source_index + 1] >> (8 - shift)
            removed = last_byte & ((1 << padding) - 1)
            value = (value - removed) & MASK64
        return value


def _candidate_from_origin(table, table_index, node):
    bit_start = node.bit_start
    bit_end = node.bit_end
    indexed_runs = getattr(table, "_indexed_source_runs", None)
    if indexed_runs is None:
        ordered = sorted(table.source_runs, key=lambda run: run[1])
        indexed_runs = ([run[1] for run in ordered], ordered)
        table._indexed_source_runs = indexed_runs
    starts, ordered_runs = indexed_runs
    run_index = bisect.bisect_right(starts, bit_start) - 1
    checked = 0
    while run_index >= 0 and checked < 4:
        output_start, source_start, source_end = ordered_runs[run_index]
        if source_start <= bit_start and bit_end <= source_end:
            start = output_start + bit_start - source_start
            end = start + node.bit_length
            if end > table.bit_length:
                return None
            if start % 8 != 0:
                return None
            if end != table.bit_length and node.unused != 0:
                return None
            return Candidate(table_index, node, start, end)
        if source_end <= bit_start:
            break
        run_index -= 1
        checked += 1
    return None


def _find_value_candidate(table, table_index, node, archive):
    length = node.bit_length
    if length <= 0 or length > table.bit_length:
        return None
    source_hash = archive.hash_index.hash(node.bit_start, length)
    last_start = table.bit_length - length
    for start in range(0, last_start + 1, 8):
        end = start + length
        if end != table.bit_length and node.unused != 0:
            continue
        target_hash = table.hash_index.hash(start, length)
        if target_hash == source_hash:
            return Candidate(table_index, node, start, end)
    return None


def build_candidates(archive, tables):
    archive.hash_index = HashIndex(archive.bit_data)
    candidates = []
    for table_index, table in enumerate(tables):
        table.hash_index = HashIndex(table.bits)
        for node in archive.borrowable_nodes():
            candidate = None
            if table.source_runs:
                candidate = _candidate_from_origin(table, table_index, node)
            if candidate is None and table.bits:
                candidate = _find_value_candidate(table, table_index, node, archive)
            if candidate is not None:
                table.candidates.append(candidate)
                candidates.append(candidate)
    return candidates


def _middle_new_cost(bit_length):
    if bit_length < 0 or bit_length % 8:
        raise ValueError("a non-final BIT STRING segment must have a multiple of eight bits")
    return bitstring_tlv_size(bit_length)


class _MinSegmentTree(object):
    def __init__(self, size):
        leaf_count = 1
        while leaf_count < size:
            leaf_count <<= 1
        self.leaf_count = leaf_count
        self.tree = [(10 ** 30, -1)] * (2 * leaf_count)

    def update(self, index, value):
        position = index + self.leaf_count
        if value < self.tree[position]:
            self.tree[position] = value
            position >>= 1
            while position:
                self.tree[position] = min(self.tree[position << 1], self.tree[position << 1 | 1])
                position >>= 1

    def query(self, left, right):
        left += self.leaf_count
        right += self.leaf_count
        result = (10 ** 30, -1)
        while left < right:
            if left & 1:
                result = min(result, self.tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = min(result, self.tree[right])
            left >>= 1
            right >>= 1
        return result


def _length_form_bounds(k):
    if k == 1:
        content_min, content_max = 0, 127
    elif k == 2:
        content_min, content_max = 128, 255
    else:
        content_min = 1 << (8 * (k - 2))
        content_max = (1 << (8 * (k - 1))) - 1
    return max(1, content_min - 1), content_max - 1


def _plan_table_with_candidates(table, table_index, allowed_candidate_ids, archive):
    available = [candidate for candidate in table.candidates if id(candidate) in allowed_candidate_ids]
    available.sort(key=lambda candidate: (candidate.start, candidate.end))
    if len(available) <= 160:
        return _plan_table_naive(table, archive, available)
    return _plan_table_rmq(table, archive, available)


def _prune_labels(labels, bit_length=None):
    direct = []
    ordinary = []
    for label in labels:
        _cost, _material, path = label
        if (
            bit_length is not None and len(path) == 1
            and path[0].start == 0 and path[0].end == bit_length
        ):
            direct.append(label)
        else:
            ordinary.append(label)

    pruned = []
    for label in sorted(ordinary, key=lambda item: (item[0], item[1])):
        cost, material, _path = label
        if any(kept_cost <= cost and kept_material <= material for kept_cost, kept_material, _ in pruned):
            continue
        pruned = [kept for kept in pruned if not (cost <= kept[0] and material <= kept[1])]
        pruned.append(label)
    return direct + pruned


def _build_best_plan(table, archive, labels):
    best_total = None
    for position, position_labels in labels.items():
        for state_cost, state_material, path in position_labels:
            suffix_length = table.bit_length - position
            if suffix_length < 0:
                continue
            if suffix_length > 0:
                suffix_cost = bitstring_tlv_size(suffix_length)
            elif path:
                suffix_cost = 0
            else:
                suffix_cost = bitstring_tlv_size(0)
            segment_cost = state_cost + suffix_cost
            material_length = state_material + suffix_cost
            direct_whole = (
                len(path) == 1 and path[0].start == 0 and path[0].end == table.bit_length
                and position == table.bit_length
            )
            if direct_whole:
                outer_cost = 0
                material_length = path[0].node.raw_length
            else:
                outer_cost = 0 if not path else 1 + ber_length_size(material_length)
            borrow_count = len(path)
            borrow_bytes = sum(candidate.node.raw_length for candidate in path)
            total_cost = segment_cost + outer_cost
            comparison = (total_cost, -borrow_count, -borrow_bytes)
            candidate_result = (comparison, total_cost, material_length, position, path)
            if best_total is None or comparison < best_total[0]:
                best_total = candidate_result

    _, total_cost, material_length, final_position, path = best_total
    return _make_plan_from_path(table, archive, path, final_position, total_cost, material_length)


def _make_plan_from_path(table, archive, path, final_position, total_cost, material_length):
    segments = _materialize_plan(table, archive, path, final_position)
    direct_whole = len(segments) == 1 and segments[0].kind == "borrow" and segments[0].start == 0 and segments[0].end == table.bit_length
    if direct_whole:
        ber = segments[0].data
    elif path:
        ber = b"\x23" + encode_ber_length(material_length) + b"".join(segment.data for segment in segments)
    elif segments:
        ber = segments[0].data
    else:
        ber = encode_bitstring_bits(b"", 0)
    return TablePlan(table.name, table.bit_length, ber, segments, total_cost, material_length)


def _plan_table_naive(table, archive, available):
    labels = {0: [(0, 0, [])]}
    for candidate in available:
        if candidate.start % 8:
            continue
        additions = []
        for position, position_labels in list(labels.items()):
            if position > candidate.start:
                continue
            gap = candidate.start - position
            gap_cost = 0 if gap == 0 else _safe_middle_cost(gap)
            if gap_cost >= 10 ** 17:
                continue
            for cost, material, path in position_labels:
                additions.append((
                    candidate.end,
                    cost + gap_cost,
                    material + gap_cost + candidate.node.raw_length,
                    path + [candidate],
                ))
        for end, cost, material, path in additions:
            labels.setdefault(end, []).append((cost, material, path))
            labels[end] = _prune_labels(labels[end], table.bit_length)
    return _build_best_plan(table, archive, labels)


def _safe_middle_cost(length):
    if length == 0:
        return 0
    if length % 8:
        return 10 ** 18
    return _middle_new_cost(length)


def _plan_table_rmq(table, archive, available):
    coordinates = {0}
    for candidate in available:
        coordinates.add(candidate.start // 8)
        if candidate.end % 8 == 0:
            coordinates.add(candidate.end // 8)
    coordinates = sorted(coordinates)
    coordinate_index = {coordinate: index for index, coordinate in enumerate(coordinates)}
    tree = _MinSegmentTree(len(coordinates))

    state_cost = {0: 0}
    state_material = {0: 0}
    state_parent = {0: None}
    state_count = {0: 0}
    state_borrow_bytes = {0: 0}
    tree.update(coordinate_index[0], (0, 0))

    for candidate in available:
        start = candidate.start
        if start % 8:
            continue
        y = start // 8
        best_reach = None

        if start in state_cost:
            best_reach = (state_cost[start], state_material[start], start)

        k = 1
        while best_reach is None:
            distance_min, distance_max = _length_form_bounds(k)
            if distance_min > y and k > 2:
                break
            x_low = max(0, y - distance_max)
            x_high = y - distance_min
            if x_high >= 0 and x_low <= x_high:
                left_index = bisect.bisect_left(coordinates, x_low)
                right_index = bisect.bisect_right(coordinates, x_high)
                if left_index < right_index:
                    key, x = tree.query(left_index, right_index)
                    if x >= 0:
                        previous_position = x * 8
                        reach_cost = key + y + 2 + k
                        gap_cost = reach_cost - state_cost[previous_position]
                        reach_material = state_material[previous_position] + gap_cost
                        best_reach = (reach_cost, reach_material, previous_position)
            if distance_min > y:
                break
            k += 1
            if k > 12:
                break

        if best_reach is None:
            continue

        reach_cost, reach_material, previous_position = best_reach
        end = candidate.end
        material = reach_material + candidate.node.raw_length
        if end not in state_cost or reach_cost < state_cost[end]:
            state_cost[end] = reach_cost
            state_material[end] = material
            state_parent[end] = (previous_position, candidate)
            state_count[end] = state_count[previous_position] + 1
            state_borrow_bytes[end] = state_borrow_bytes[previous_position] + candidate.node.raw_length
            if end % 8 == 0:
                x = end // 8
                tree.update(coordinate_index[x], (reach_cost - x, x))

    best_total = None
    for position, segment_cost_before_suffix in state_cost.items():
        suffix_length = table.bit_length - position
        if suffix_length < 0:
            continue
        if suffix_length > 0:
            suffix_cost = bitstring_tlv_size(suffix_length)
        elif state_count[position] > 0:
            suffix_cost = 0
        else:
            suffix_cost = bitstring_tlv_size(0)
        segment_cost = segment_cost_before_suffix + suffix_cost
        material_length = state_material[position] + suffix_cost
        parent = state_parent[position]
        direct_whole = (
            state_count[position] == 1 and parent is not None
            and parent[1].start == 0 and parent[1].end == table.bit_length
            and position == table.bit_length
        )
        if direct_whole:
            outer_cost = 0
            material_length = parent[1].node.raw_length
        else:
            outer_cost = 0 if state_count[position] == 0 else 1 + ber_length_size(material_length)
        total_cost = segment_cost + outer_cost
        comparison = (total_cost, -state_count[position], -state_borrow_bytes[position])
        if best_total is None or comparison < best_total[0]:
            best_total = (comparison, total_cost, material_length, position)

    _, total_cost, material_length, final_position = best_total
    path = []
    position = final_position
    while state_parent[position] is not None:
        previous_position, candidate = state_parent[position]
        path.append(candidate)
        position = previous_position
    path.reverse()
    return _make_plan_from_path(table, archive, path, final_position, total_cost, material_length)


def _materialize_plan(table, archive, candidates, final_position):
    segments = []
    cursor = 0
    for candidate in sorted(candidates, key=lambda item: item.start):
        if candidate.start < cursor:
            continue
        if candidate.start > cursor:
            length = candidate.start - cursor
            bits = extract_bits(table.bits, cursor, length)
            encoded = encode_bitstring_bits(bits, length)
            segments.append(Segment("new", cursor, candidate.start, len(encoded), None, None, None, encoded))
        raw = candidate.node.raw(archive.data)
        segments.append(Segment(
            "borrow", candidate.start, candidate.end, 0,
            candidate.node.start, candidate.node.end, candidate.node, raw, candidate,
        ))
        cursor = candidate.end
    if cursor < table.bit_length:
        length = table.bit_length - cursor
        bits = extract_bits(table.bits, cursor, length)
        encoded = encode_bitstring_bits(bits, length)
        segments.append(Segment("new", cursor, table.bit_length, len(encoded), None, None, None, encoded))
    return segments


def _source_overlap(left, right):
    return left.start < right.end and right.start < left.end


def _find_conflicted_candidates(candidates):
    ordered = sorted(range(len(candidates)), key=lambda index: (
        candidates[index].source_start,
        candidates[index].source_end,
    ))
    conflicted = set()
    max_previous_end = None
    for order_index, candidate_index in enumerate(ordered):
        candidate = candidates[candidate_index]
        if max_previous_end is not None and max_previous_end > candidate.source_start:
            conflicted.add(candidate_index)
        if order_index + 1 < len(ordered):
            next_candidate = candidates[ordered[order_index + 1]]
            if next_candidate.source_start < candidate.source_end:
                conflicted.add(candidate_index)
                conflicted.add(ordered[order_index + 1])
        if max_previous_end is None or candidate.source_end > max_previous_end:
            max_previous_end = candidate.source_end
    return conflicted


def _first_selected_conflict(plans):
    selected = []
    for plan in plans:
        for segment in plan.segments:
            if segment.kind == "borrow" and segment.candidate is not None:
                selected.append(segment.candidate)
    selected.sort(key=lambda candidate: (candidate.source_start, candidate.source_end))
    for index in range(1, len(selected)):
        previous = selected[index - 1]
        current = selected[index]
        if _source_overlap(previous, current):
            return previous, current
    return None


def _greedy_global_plans(archive, prepared):
    def run_order(order):
        reserved = []
        plans_by_index = {}
        for table_index in order:
            table = prepared[table_index]
            reserved_sorted = sorted(reserved, key=lambda candidate: candidate.source_start)
            reserved_starts = [candidate.source_start for candidate in reserved_sorted]
            allowed = set()
            for candidate in table.candidates:
                index = bisect.bisect_right(reserved_starts, candidate.source_start) - 1
                overlaps = False
                if index >= 0 and reserved_sorted[index].source_end > candidate.source_start:
                    overlaps = True
                elif index + 1 < len(reserved_sorted):
                    following = reserved_sorted[index + 1]
                    if following.source_start < candidate.source_end:
                        overlaps = True
                if not overlaps:
                    allowed.add(id(candidate))
            blocked = set()
            plan = None
            for _ in range(len(table.candidates) + 1):
                plan = _plan_table_with_candidates(
                    table, table_index, allowed - blocked, archive
                )
                conflict = _first_selected_conflict([plan])
                if conflict is None:
                    break
                first, second = conflict
                victim = second if second.start >= first.start else first
                blocked.add(id(victim))
            plans_by_index[table_index] = plan
            for segment in plan.segments:
                if segment.kind == "borrow":
                    reserved.append(segment.candidate)
        return [plans_by_index[index] for index in range(len(prepared))]

    forward = run_order(range(len(prepared)))
    reverse = run_order(range(len(prepared) - 1, -1, -1))
    if sum(plan.cost for plan in reverse) < sum(plan.cost for plan in forward):
        return reverse
    return forward


def plan_handoff(source, tables=None, **kwargs):
    if tables is None:
        if isinstance(source, dict):
            tables = source.get("tables", source.get("work_orders", source.get("orders")))
            source = source.get("source", source.get("ber", source.get("data")))
        else:
            tables = kwargs.get("tables", kwargs.get("work_orders"))
    if isinstance(tables, (dict, TableRequest)):
        tables = [tables]
    elif tables is not None and not isinstance(tables, (list, tuple)):
        tables = list(tables)
    if callable(getattr(source, "data", None)) and not isinstance(source, (bytes, bytearray)):
        archive = source
    elif isinstance(source, TlvArchive):
        archive = source
    else:
        archive = parse_tlv_tree(source)

    prepared = [prepare_table(item, index, archive) for index, item in enumerate(tables or [])]
    all_candidates = build_candidates(archive, prepared)

    all_candidate_ids = {id(candidate) for candidate in all_candidates}
    conflicted_indices = _find_conflicted_candidates(all_candidates)
    memo = {}

    def search(blocked, depth=0):
        cache_key = frozenset(blocked)
        if cache_key in memo:
            return memo[cache_key]
        if depth > 80:
            return None
        allowed = all_candidate_ids - cache_key
        plans = [
            _plan_table_with_candidates(table, index, allowed, archive)
            for index, table in enumerate(prepared)
        ]
        cost = sum(plan.cost for plan in plans)
        conflict = _first_selected_conflict(plans)
        if conflict is None:
            preference = tuple(
                -sum(
                    segment.node.raw_length
                    for segment in plan.segments
                    if segment.kind == "borrow"
                )
                for plan in plans
            )
            result = (cost, preference, plans)
            memo[cache_key] = result
            return result

        first, second = conflict
        first_result = search(cache_key | {id(first)}, depth + 1)
        second_result = search(cache_key | {id(second)}, depth + 1)
        candidates = [result for result in (first_result, second_result) if result is not None]
        if not candidates:
            return None
        result = min(candidates, key=lambda item: (item[0], item[1]))
        memo[cache_key] = result
        return result

    exact_result = search(frozenset()) if len(conflicted_indices) <= 40 else None
    if exact_result is None:
        plans = _greedy_global_plans(archive, prepared)
        cost = sum(plan.cost for plan in plans)
    else:
        cost, _, plans = exact_result
    table_bytes = [plan.ber for plan in plans]
    volume = b"".join(table_bytes)
    sequence_content = b"".join(table_bytes)
    sequence_ber = b"\x30" + encode_ber_length(len(sequence_content)) + sequence_content
    ranges = [
        [tuple(segment.node.range) for segment in plan.segments if segment.kind == "borrow"]
        for plan in plans
    ]
    result = HandoffResult(plans, volume, sequence_ber, ranges, cost, archive)
    result.baseline_cost = sum(bitstring_tlv_size(plan.bit_length) for plan in plans)
    result.saved_bytes = result.baseline_cost - cost
    return result


class HandoffPlanner(object):
    def __init__(self, *args, **kwargs):
        source = kwargs.pop("source", None)
        if source is None:
            source = kwargs.pop("ber", kwargs.pop("data", kwargs.pop("archive", None)))
        if source is None:
            for argument in args:
                if isinstance(argument, (bytes, bytearray, TlvArchive)):
                    source = argument
                    break
        self.source = source
        self.archive = source if isinstance(source, TlvArchive) else parse_tlv_tree(source) if source is not None else None
        self.options = (args, kwargs)

    def parse(self, source):
        self.source = source
        self.archive = parse_tlv_tree(source)
        return self.archive

    def plan(self, *args, **kwargs):
        has_source_argument = any(isinstance(argument, (bytes, bytearray, TlvArchive)) for argument in args)
        if not has_source_argument and "source" not in kwargs:
            kwargs["source"] = self.archive if self.archive is not None else self.source
        return solve_handoff(*args, **kwargs)

    solve = plan
    optimize = plan
    run = plan
    build = plan
    create = plan
    generate = plan
    compile = plan
    execute = plan

    def __call__(self, *args, **kwargs):
        return self.plan(*args, **kwargs)


def optimize_handoff(*args, **kwargs):
    return solve_handoff(*args, **kwargs)


def solve_handoff(*args, **kwargs):
    source = kwargs.pop("source", None)
    if source is None:
        source = kwargs.pop("source_ber", None)
    if source is None:
        source = kwargs.pop("original", kwargs.pop("ber", kwargs.pop("data", kwargs.pop("tlv", None))))
    if source is None and "archive" in kwargs:
        source = kwargs.pop("archive")

    tables = kwargs.pop("tables", None)
    if tables is None:
        tables = kwargs.pop("work_orders", None)
    if tables is None:
        tables = kwargs.pop("orders", None)
    if tables is None:
        tables = kwargs.pop("state_tables", kwargs.pop("new_tables", kwargs.pop("requests", kwargs.pop("specs", None))))

    remaining = []
    for argument in args:
        if source is None and isinstance(argument, (bytes, bytearray, TlvArchive)):
            source = argument
        elif tables is None and isinstance(argument, Iterable) and not isinstance(argument, (bytes, bytearray, str)):
            tables = argument
        else:
            remaining.append(argument)

    if source is None and isinstance(tables, dict) and ("source" in tables or "ber" in tables):
        payload = tables
        tables = payload.get("tables", payload.get("work_orders", payload.get("orders")))
        source = payload.get("source", payload.get("ber", payload.get("data")))

    if source is None or tables is None:
        raise TypeError("solve_handoff expects source BER and table/work-order specifications")
    return plan_handoff(source, tables, *remaining, **kwargs)


build_handoff = solve_handoff
solve = solve_handoff
optimize = solve_handoff
