"""Compare two collation tables against an archived goods-name catalog.

The module does not re-implement the Unicode Collation Algorithm.  Every key
is produced by a real :class:`pyuca.collator.BaseCollator`, so NFD
normalization, discontiguous contractions, expansions and implicit weights
behave exactly as they do in the library.

Public API::

    load_collator(spec)          load an allkeys table or UCA profile
    diff_tables(old, new)        changed rules and implicit ranges
    audit(old, new, catalog)     rank a catalog under both tables
    format_report(report)        human-readable report

Catalog records may be individual cards or compressed accession ranges.
Records with the same source text share one key.  Large repeated cards stay
as one weighted block rather than being expanded into one record per copy.
"""

from __future__ import unicode_literals

import bisect
import json
import os
import unicodedata
from dataclasses import dataclass, field
from io import open

from .collator import (
    BaseCollator,
    Collator,
    Collator_5_2_0,
    Collator_6_3_0,
    Collator_8_0_0,
    Collator_9_0_0,
    Collator_10_0_0,
)
from .utils import format_sort_key


LEVEL_NAMES = ("primary", "secondary", "tertiary", "quaternary")
MAX_EXPANDED_ENTRIES = 200000
MAX_FRAGMENT_CHARS = 100000
MAX_PAGE_RANGES = 5000
LARGE_THRESHOLD = 10000


class AuditError(ValueError):
    """Raised for invalid catalogs, tables or comparison options."""


# ---------------------------------------------------------------------------
# Keys


def _split_flat_key(flat):
    levels = []
    current = []
    for weight in flat:
        if weight == 0:
            levels.append(tuple(current))
            current = []
        else:
            current.append(weight)
    levels.append(tuple(current))
    return levels


def _normalise_levels(value, max_level=4):
    """Return ``(levels, depth)`` for any supported key representation."""
    if value is None:
        return None, 0
    if isinstance(value, Key):
        return value.levels, value.depth
    if isinstance(value, dict):
        if "levels" in value:
            return _normalise_levels(value["levels"], max_level)
        if "key" in value:
            return _normalise_levels(value["key"], max_level)
        raise AuditError("key object needs a 'levels' or 'key' member")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return tuple(() for _ in range(max_level)), 0
        if "|" in text:
            levels = [tuple(int(token, 16) for token in part.split())
                      for part in text.split("|")]
        else:
            flat = [int(token, 16) for token in text.split()]
            levels = _split_flat_key(flat)
        depth = len(levels)
    elif isinstance(value, (list, tuple)):
        if not value:
            levels = [()]
        elif all(isinstance(part, (list, tuple)) for part in value):
            levels = [tuple(int(weight) for weight in part)
                      for part in value]
        else:
            levels = _split_flat_key([int(weight) for weight in value])
        depth = len(levels)
    else:
        raise AuditError("unsupported sort-key representation: {0!r}"
                         .format(type(value).__name__))

    levels = [tuple(level) for level in levels]
    depth = min(depth, max_level)
    if len(levels) < max_level:
        levels.extend(() for _ in range(max_level - len(levels)))
    return tuple(levels[:max_level]), depth


@dataclass(frozen=True)
class Key:
    """A sort key split into UCA levels.

    ``depth`` records how many levels were explicitly present in the source
    representation.  A primary-only index key therefore has ``depth == 1``
    even though ``levels`` is padded to four entries for comparison.
    """

    levels: tuple
    depth: int = 4

    @classmethod
    def from_value(cls, value, max_level=4):
        levels, depth = _normalise_levels(value, max_level=max_level)
        if levels is None:
            return None
        return cls(levels, depth)

    def truncated(self, levels):
        return tuple(self.levels[:levels])

    def matches(self, other, levels):
        return self.truncated(levels) == other.truncated(levels)

    def flat(self):
        result = []
        for level in self.levels:
            if result:
                result.append(0)
            result.extend(level)
        return tuple(result)

    def __str__(self):
        return format_sort_key(self.flat())

def first_difference(left, right, max_levels=4, left_entry=None,
                     right_entry=None):
    """Return the earliest level (or entry number) separating two keys."""
    for index in range(min(max_levels, len(LEVEL_NAMES))):
        name = LEVEL_NAMES[index]
        a = left.levels[index] if index < len(left.levels) else ()
        b = right.levels[index] if index < len(right.levels) else ()
        if a == b:
            continue
        common = min(len(a), len(b))
        for position in range(common):
            if a[position] != b[position]:
                return {
                    "level": name,
                    "level_index": index + 1,
                    "position": position,
                    "left_weight": a[position],
                    "right_weight": b[position],
                    "kind": "weight",
                }
        return {
            "level": name,
            "level_index": index + 1,
            "position": common,
            "left_weight": a[common] if common < len(a) else None,
            "right_weight": b[common] if common < len(b) else None,
            "kind": "prefix",
        }
    if left_entry is not None and right_entry is not None:
        if left_entry != right_entry:
            return {
                "level": "entry",
                "level_index": None,
                "left_entry": left_entry,
                "right_entry": right_entry,
                "kind": "tie_break",
            }
    return {
        "level": "tie",
        "level_index": None,
        "kind": "tie",
        "left_entry": left_entry,
        "right_entry": right_entry,
    }


# ---------------------------------------------------------------------------
# Tables


_VERSION_COLLATORS = {
    "5.2.0": Collator_5_2_0,
    "6.3.0": Collator_6_3_0,
    "8.0.0": Collator_8_0_0,
    "9.0.0": Collator_9_0_0,
    "10.0.0": Collator_10_0_0,
}


def _table_version(path):
    try:
        with open(path, encoding="utf-8") as table_file:
            for line in table_file:
                if line.startswith("@version"):
                    return line[len("@version"):].strip()
    except (IOError, OSError, UnicodeDecodeError):
        return None
    return None


def load_collator(spec, base_dir=None):
    """Load a collator from a path, version string, mapping or instance."""
    if spec is None:
        raise AuditError("a collation table is required")
    if hasattr(spec, "sort_key") and hasattr(spec, "collation_elements"):
        return spec
    if isinstance(spec, dict):
        version = spec.get("version")
        if "collator" in spec:
            return load_collator(spec["collator"], base_dir=base_dir)
        if "path" in spec:
            path = spec["path"]
            if base_dir and not os.path.isabs(path):
                path = os.path.join(base_dir, path)
            if version is not None:
                collator_class = _VERSION_COLLATORS.get(version,
                                                         BaseCollator)
                return collator_class(filename=path)
            return load_collator(path, base_dir=base_dir)
        if version is not None:
            return load_collator(version, base_dir=base_dir)
        raise AuditError("table object needs 'path', 'version' or 'collator'")
    if not isinstance(spec, str):
        raise AuditError("unsupported collation table: {0!r}".format(spec))

    name = spec.strip()
    if name in ("default", "python", "stdlib"):
        return Collator()
    version = name
    for prefix in ("ucd:", "uca:", "v"):
        if version.lower().startswith(prefix):
            version = version[len(prefix):]
    if version in _VERSION_COLLATORS:
        return _VERSION_COLLATORS[version]()

    path = name
    if base_dir and not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    if not os.path.exists(path):
        raise AuditError("collation table not found: {0}".format(spec))
    detected = _table_version(path)
    collator_class = _VERSION_COLLATORS.get(detected, BaseCollator)
    return collator_class(filename=path)


def _format_codepoints(codepoints):
    return " ".join("{0:04X}".format(point) for point in codepoints)


def _rule_text(rule):
    try:
        return _format_codepoints(rule)
    except TypeError:
        return repr(rule)


@dataclass
class TableDiff:
    old_version: object = None
    new_version: object = None
    rule_changes: list = field(default_factory=list)
    implicit_changes: list = field(default_factory=list)
    old_rule_count: int = 0
    new_rule_count: int = 0
    unapproved: list = field(default_factory=list)

    def to_dict(self):
        changes = [change.copy() for change in self.rule_changes]
        implicit = [change.copy() for change in self.implicit_changes]
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_rule_count": self.old_rule_count,
            "new_rule_count": self.new_rule_count,
            "rule_changes": changes,
            "implicit_changes": implicit,
            "summary": {
                "rules": len(changes),
                "contractions": sum(
                    1 for change in changes if change["is_contraction"]
                ),
                "implicit_ranges": len(implicit),
                "unapproved": len(self.unapproved),
            },
            "unapproved": list(self.unapproved),
        }


def _collator_version(collator):
    return getattr(collator, "UCA_VERSION", None)


def _rule_changes(old_rules, new_rules):
    changes = []
    for rule in sorted(set(old_rules) | set(new_rules)):
        old_value = old_rules.get(rule)
        new_value = new_rules.get(rule)
        if old_value == new_value:
            continue
        if old_value is None:
            kind = "added"
        elif new_value is None:
            kind = "removed"
        else:
            kind = "changed"
        changes.append({
            "kind": kind,
            "key": _rule_text(rule),
            "codepoints": list(rule),
            "is_contraction": len(rule) > 1,
            "old": old_value,
            "new": new_value,
        })
    return changes


def _implicit_changes(old_ranges, new_ranges):
    old_map = {(start, end): base for start, end, base in old_ranges}
    new_map = {(start, end): base for start, end, base in new_ranges}
    changes = []
    for span in sorted(set(old_map) | set(new_map)):
        old_value = old_map.get(span)
        new_value = new_map.get(span)
        if old_value == new_value:
            continue
        if old_value is None:
            kind = "added"
        elif new_value is None:
            kind = "removed"
        else:
            kind = "changed"
        changes.append({
            "kind": kind,
            "start": span[0],
            "end": span[1],
            "old_base": old_value,
            "new_base": new_value,
        })
    return changes


def _normalise_approval_rules(approvals):
    approved = set()
    for item in (approvals or {}).get("rules", []):
        if isinstance(item, str):
            tokens = item.replace(",", " ").split()
            approved.add(tuple(int(token, 16) for token in tokens))
        else:
            approved.add(tuple(int(token) for token in item))
    return approved


def _normalise_approval_ranges(approvals):
    approved = set()
    for item in (approvals or {}).get("implicit_ranges", []):
        if isinstance(item, dict):
            approved.add((
                int(item["start"]),
                int(item["end"]),
                int(item.get("base", item.get("new_base"))),
            ))
        else:
            approved.add(tuple(int(value) for value in item))
    return approved


def diff_tables(old, new, approvals=None):
    """Return a :class:`TableDiff` for two loaded collators or specs."""
    old_collator = load_collator(old)
    new_collator = load_collator(new)
    if not hasattr(old_collator, "rules") or not hasattr(new_collator, "rules"):
        raise AuditError("both collators must expose their loaded rules")

    diff = TableDiff(
        old_version=_collator_version(old_collator),
        new_version=_collator_version(new_collator),
        old_rule_count=len(old_collator.rules),
        new_rule_count=len(new_collator.rules),
    )
    diff.rule_changes = _rule_changes(old_collator.rules, new_collator.rules)
    diff.implicit_changes = _implicit_changes(
        old_collator.implicit_weights, new_collator.implicit_weights)

    if approvals is not None:
        approved_rules = _normalise_approval_rules(approvals)
        approved_ranges = _normalise_approval_ranges(approvals)
        for change in diff.rule_changes:
            change["approved"] = tuple(change["codepoints"]) in approved_rules
            if not change["approved"]:
                diff.unapproved.append({"kind": "rule",
                                        "key": change["key"]})
        for change in diff.implicit_changes:
            span = (change["start"], change["end"], change["new_base"])
            change["approved"] = span in approved_ranges
            if not change["approved"]:
                diff.unapproved.append({
                    "kind": "implicit_range",
                    "start": change["start"],
                    "end": change["end"],
                })
    return diff


# ---------------------------------------------------------------------------
# Catalog records


@dataclass(frozen=True)
class EntryRange:
    """A contiguous arithmetic accession interval, inclusive."""

    start: int
    count: int
    step: int = 1

    def __post_init__(self):
        if self.count < 0:
            raise AuditError("entry range count cannot be negative")
        if self.count and self.step <= 0:
            raise AuditError("entry range step must be positive")

    @property
    def first(self):
        return self.start

    @property
    def last(self):
        return self.start + (self.count - 1) * self.step

    def __len__(self):
        return self.count

    def __iter__(self):
        for offset in range(self.count):
            yield self.start + offset * self.step

    def entry_at(self, offset):
        if offset < 0 or offset >= self.count:
            raise IndexError(offset)
        return self.start + offset * self.step

    def offset_of(self, entry):
        if not self.contains(entry):
            raise AuditError("entry {0} is not in range {1}-{2}"
                             .format(entry, self.first, self.last))
        return (entry - self.start) // self.step

    def contains(self, entry):
        if not self.count:
            return False
        if self.step == 1:
            return self.first <= entry <= self.last
        if entry < self.first or entry > self.last:
            return False
        return (entry - self.first) % self.step == 0

    def overlaps(self, other):
        if not self.count or not other.count:
            return False
        low = max(self.first, other.first)
        high = min(self.last, other.last)
        if low > high:
            return False
        if self.step == 1 or other.step == 1:
            return True
        return _progressions_intersect(
            self.first, self.step, other.first, other.step, low, high)


def _progressions_intersect(start_a, step_a, start_b, step_b, low, high):
    """Whether two arithmetic progressions share a value in [low, high]."""
    from math import gcd
    common = gcd(step_a, step_b)
    difference = start_b - start_a
    if difference % common:
        return False
    # Find one solution to start_a + i*step_a == start_b + j*step_b.
    reduced_a = step_a // common
    reduced_b = step_b // common
    if reduced_b:
        inverse = pow(reduced_b, -1, reduced_a) if reduced_a != 1 else 0
    else:
        inverse = 0
    k = (difference // common) * inverse % reduced_a
    value = start_a + k * step_a
    period = step_a * reduced_b
    if value < low:
        value += ((low - value + period - 1) // period) * period
    elif value > high:
        return False
    return value <= high


@dataclass
class RecordGroup:
    """One or more catalog entries sharing the same source text."""

    entries: EntryRange
    name: str
    old_key: object = None
    old_page: object = None
    source: dict = field(default_factory=dict)
    index: int = 0
    new_key: object = None

    @property
    def weight(self):
        return len(self.entries)

    @property
    def first_entry(self):
        return self.entries.first


def _first_member(record, names):
    for key in names:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _entry_value(record):
    return _first_member(record, (
        "entry", "entry_no", "entry_number", "accession", "accession_no",
        "number", "id",
    ))


def _entry_range(record):
    if "entry_range" in record:
        value = record["entry_range"]
        if isinstance(value, dict):
            start = value.get("start", value.get("first"))
            end = value.get("end", value.get("last"))
            step = int(value.get("step", 1))
            if start is None or end is None:
                raise AuditError("entry_range needs start and end")
            count = (int(end) - int(start)) // step + 1
            return EntryRange(int(start), count, step)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            start, end = value
            return EntryRange(int(start), int(end) - int(start) + 1)
        raise AuditError("invalid entry_range: {0!r}".format(value))

    if "entries" in record:
        values = [int(value) for value in record["entries"]]
        if not values:
            raise AuditError("an entry list cannot be empty")
        values.sort()
        if values != list(range(values[0], values[0] + len(values))):
            raise AuditError("explicit entry lists must be contiguous; "
                             "use entry_range for larger ranges")
        return EntryRange(values[0], len(values))

    first = _first_member(record, ("entry_start", "first_entry", "start"))
    if first is None:
        first = _entry_value(record)
    if first is None:
        raise AuditError("record has no accession number")
    first = int(first)
    step = int(record.get("step", 1))
    count = record.get("count", record.get("repeat", 1))
    last = _first_member(record, ("entry_end", "last_entry", "end"))
    if last is not None:
        last = int(last)
        if last < first:
            raise AuditError("entry range ends before it starts")
        count = (last - first) // step + 1
    return EntryRange(first, int(count), step)


def _record_name(record):
    return _first_member(record, (
        "name", "text", "full_name", "title", "label",
    ))


def _record_parts(record):
    return _first_member(record, (
        "fragments", "parts", "segments", "clips", "name_parts",
    ))


class FragmentResolver:
    """Expand fragment references before any sort key is calculated.

    A part may be literal text, a nested list, or an object with ``ref`` /
    ``literal`` and an optional ``repeat``.  A plain string that exactly
    matches a fragment name is treated as a reference; use
    ``{"literal": "..."}`` to force literal text with the same spelling.
    """

    def __init__(self, definitions, max_chars=MAX_FRAGMENT_CHARS):
        self.definitions = definitions or {}
        self.max_chars = max_chars
        self.cache = {}
        self.active = set()

    def resolve(self, parts, stack=()):
        if isinstance(parts, str):
            return self._part(parts, stack)
        chunks = []
        length = 0
        for part in parts:
            chunk = self._part(part, stack)
            chunks.append(chunk)
            length += len(chunk)
            if length > self.max_chars:
                raise AuditError(
                    "expanded name exceeds {0} characters; keep a repeated "
                    "card as one compressed entry range"
                    .format(self.max_chars))
        return "".join(chunks)

    def _part(self, part, stack):
        if isinstance(part, str):
            if part in self.definitions:
                return self.fragment(part, stack)
            return part
        if isinstance(part, (list, tuple)):
            return self.resolve(part, stack)
        if isinstance(part, dict):
            repeat = int(part.get("repeat", part.get("times", 1)))
            if repeat < 0:
                raise AuditError("fragment repeat cannot be negative")
            if "ref" in part:
                chunk = self.fragment(part["ref"], stack)
            elif "fragment" in part:
                chunk = self.fragment(part["fragment"], stack)
            elif "name" in part:
                chunk = self.fragment(part["name"], stack)
            elif "literal" in part:
                chunk = part["literal"]
            elif "text" in part:
                chunk = part["text"]
            elif "value" in part:
                chunk = part["value"]
            else:
                raise AuditError("fragment part needs ref or literal")
            return chunk * repeat
        raise AuditError("unsupported fragment part: {0!r}".format(part))

    def fragment(self, name, stack=()):
        if name in self.cache:
            return self.cache[name]
        if name in stack or name in self.active:
            raise AuditError("cyclic fragment reference: {0}".format(name))
        if name not in self.definitions:
            raise AuditError("unknown fragment: {0}".format(name))
        self.active.add(name)
        try:
            value = self.resolve(self.definitions[name], stack + (name,))
        finally:
            self.active.discard(name)
        self.cache[name] = value
        return value

    def item_name(self, record):
        parts = _record_parts(record)
        name = _record_name(record)
        prefix = record.get("prefix", "")
        suffix = record.get("suffix", "")
        joiner = record.get("joiner", record.get("separator", ""))
        if parts is not None:
            expanded = self.resolve(parts)
            return "{0}{1}{2}{3}{4}".format(
                name or "", prefix, expanded, suffix, joiner)
        if name is None:
            raise AuditError("record has no name or fragments")
        return "{0}{1}{2}{3}".format(prefix, name, suffix, joiner)


def _load_old_index(data):
    index = data.get("old_index")
    if index is None:
        return []
    if isinstance(index, dict):
        index = index.get("records", index.get("items", []))
    rows = []
    for row in index:
        if not isinstance(row, dict):
            raise AuditError("old_index rows must be objects")
        rows.append((
            _entry_range(row),
            _first_member(row, ("old_key", "key", "sort_key")),
            _first_member(row, ("old_page", "page")),
        ))
    return rows


def _attach_old_index(groups, index):
    unmatched = list(index)
    for group in groups:
        if group.old_key is not None and group.old_page is not None:
            continue
        for position, (span, key, page) in enumerate(unmatched):
            if span.first == group.entries.first or span.overlaps(
                    group.entries):
                if group.old_key is None:
                    group.old_key = key
                if group.old_page is None:
                    group.old_page = page
                unmatched.pop(position)
                break


def load_catalog(data):
    """Build a list of :class:`RecordGroup` objects from a catalog mapping."""
    if isinstance(data, list):
        data = {"records": data}
    if not isinstance(data, dict):
        raise AuditError("catalog must be a mapping or a list of records")

    records = _first_member(data, ("records", "items", "catalog", "goods"))
    if records is None:
        raise AuditError("catalog needs a 'records' or 'items' list")
    if not isinstance(records, list):
        raise AuditError("catalog records must be a list")

    fragments = data.get("fragments", data.get("archive", {}).get(
        "fragments", {}))
    resolver = FragmentResolver(fragments)
    groups = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise AuditError("record {0} is not an object".format(index))
        span = _entry_range(record)
        if span.count == 0:
            continue
        groups.append(RecordGroup(
            entries=span,
            name=resolver.item_name(record),
            old_key=_first_member(record, ("old_key", "old_sort_key",
                                           "key", "sort_key")),
            old_page=_first_member(record, ("old_page", "page")),
            source=record,
            index=index,
        ))

    groups.sort(key=lambda group: (group.entries.first, group.index))
    for previous, current in zip(groups, groups[1:]):
        if previous.entries.overlaps(current.entries):
            raise AuditError(
                "overlapping accession ranges: {0}-{1} and {2}-{3}"
                .format(previous.entries.first, previous.entries.last,
                        current.entries.first, current.entries.last))
    _attach_old_index(groups, _load_old_index(data))
    return groups


# ---------------------------------------------------------------------------
# Boards and ranks


def _entry_sort_value(entry):
    if isinstance(entry, bool):
        return (0, int(entry), "")
    if isinstance(entry, int):
        return (0, entry, "")
    text = str(entry)
    try:
        return (0, int(text), "")
    except ValueError:
        return (1, 0, text)


@dataclass
class BoardNode:
    group: RecordGroup
    key: Key
    effective_key: tuple
    position: int
    weight: int
    rank_start: int
    rank_end: int

    @property
    def first_entry(self):
        return self.group.entries.first


class Board:
    def __init__(self, nodes, total, tie_break):
        self.nodes = nodes
        self.total = total
        self.tie_break = tie_break
        self.by_group = {node.group.index: node for node in nodes}
        self.by_first_entry = {node.first_entry: node for node in nodes}
        self.firsts = [node.first_entry for node in nodes]

    def node_for(self, group):
        return self.by_group[group.index]

    def node_for_entry(self, entry):
        node = self.by_first_entry.get(entry)
        if node is not None:
            return node
        value = int(entry)
        index = bisect.bisect_right(self.firsts, value) - 1
        while index >= 0:
            candidate = self.nodes[index]
            if candidate.group.entries.contains(value):
                return candidate
            if candidate.group.entries.first < value and \
                    candidate.group.entries.last < value:
                if candidate.group.entries.step == 1:
                    break
            index -= 1
        raise AuditError("entry not in catalog: {0}".format(entry))


def _sort_nodes(items, levels, tie_break):
    def sort_key(item):
        group, key = item
        return (key.truncated(levels),
                _entry_sort_value(group.entries.first))

    ordered = sorted(items, key=sort_key)
    nodes = []
    position = 0
    for group, key in ordered:
        weight = group.weight
        nodes.append(BoardNode(
            group=group,
            key=key,
            effective_key=key.truncated(levels),
            position=position,
            weight=weight,
            rank_start=position + 1,
            rank_end=position + weight,
        ))
        position += weight
    return nodes, position


def _apply_tied_ranks(nodes):
    """Give every group in a tied run the full competition-rank span."""
    index = 0
    while index < len(nodes):
        key = nodes[index].effective_key
        end = index + 1
        while end < len(nodes) and nodes[end].effective_key == key:
            end += 1
        span_start = nodes[index].position + 1
        span_end = nodes[end - 1].position + nodes[end - 1].weight
        for node in nodes[index:end]:
            node.rank_start = span_start
            node.rank_end = span_end
        index = end


class _RankPageGeometry:
    def __init__(self, total, starts=None, size=None):
        self.total = total
        self.starts = starts
        self.size = size

    @property
    def page_count(self):
        if self.size is not None:
            return max(1, (self.total + self.size - 1) // self.size)
        return max(1, len(self.starts))

    def page_index(self, position):
        if position < 0:
            raise IndexError(position)
        if self.size is not None:
            return position // self.size
        return max(0, bisect.bisect_right(self.starts, position) - 1)

    def page_number(self, position):
        return self.page_index(position) + 1

    def bounds(self, page_index):
        if page_index < 0 or page_index >= self.page_count:
            raise IndexError(page_index)
        if self.size is not None:
            start = page_index * self.size
            return start, min(start + self.size, self.total)
        start = self.starts[page_index]
        end = (self.starts[page_index + 1]
               if page_index + 1 < len(self.starts) else self.total)
        return start, max(start, end)

    def label(self, page_index):
        return page_index + 1


class _AnchorPageGeometry:
    def __init__(self, board, positions, labels):
        self.board = board
        self.total = board.total
        self.starts = sorted(set([0] + list(positions)))
        labels = dict(zip(positions, labels))
        self.labels = [labels.get(start, index + 1)
                       for index, start in enumerate(self.starts)]

    @property
    def page_count(self):
        return max(1, len(self.starts))

    def page_index(self, position):
        if position < 0:
            raise IndexError(position)
        return max(0, bisect.bisect_right(self.starts, position) - 1)

    def page_number(self, position):
        return self.page_index(position) + 1

    def bounds(self, page_index):
        if page_index < 0 or page_index >= self.page_count:
            raise IndexError(page_index)
        start = self.starts[page_index]
        end = (self.starts[page_index + 1]
               if page_index + 1 < len(self.starts) else self.total)
        return start, max(start, end)

    def label(self, page_index):
        if 0 <= page_index < len(self.labels):
            return self.labels[page_index]
        return page_index + 1


class RankPageScheme:
    def __init__(self, starts=None, size=None):
        self.starts = sorted(set(int(value) for value in starts or []))
        if self.starts and self.starts[0] != 0:
            self.starts.insert(0, 0)
        self.size = size
        if not self.starts and size is None:
            self.starts = [0]

    def pages_for(self, board):
        return _RankPageGeometry(board.total, self.starts, self.size)

    def boundary_definitions(self, selectors, old_pages, new_pages):
        for selector in selectors:
            page = _selector_page(selector)
            if page < 1 or page >= old_pages.page_count:
                raise AuditError("page boundary out of range: {0}"
                                 .format(selector))
            # Edge number 1 is the edge after page 1.  The threshold is the
            # first board position on the far side of that edge.
            yield (str(page), old_pages.bounds(page)[0],
                   new_pages.bounds(page)[0])


def _selector_page(selector):
    if isinstance(selector, dict):
        if "page" not in selector:
            raise AuditError("rank pages accept {'page': n} boundaries")
        return int(selector["page"])
    if isinstance(selector, bool):
        raise AuditError("invalid page boundary: {0!r}".format(selector))
    try:
        return int(selector)
    except (TypeError, ValueError):
        raise AuditError("page boundary must be numeric: {0!r}"
                         .format(selector))


class AnchorPageScheme:
    """Pages start at fixed accession anchors.

    The same anchors are located separately in the old and new boards, so a
    card crossing an anchored page edge is detected from its position relative
    to that accession in each version.
    """

    def __init__(self, anchors):
        if not anchors:
            raise AuditError("anchor page layout needs at least one anchor")
        self.anchors = list(anchors)

    def _position(self, board, anchor):
        node = board.node_for_entry(anchor)
        return node.position + node.group.entries.offset_of(anchor)

    def pages_for(self, board):
        positions = [self._position(board, anchor)
                     for anchor in self.anchors]
        return _AnchorPageGeometry(board, positions, self.anchors)

    def _anchor_for_selector(self, selector):
        if isinstance(selector, dict):
            if "anchor" not in selector:
                raise AuditError("anchor pages accept {'anchor': x}")
            return selector["anchor"]
        for anchor in self.anchors:
            if str(anchor) == str(selector):
                return anchor
        return None

    def boundary_definitions(self, selectors, old_pages, new_pages):
        for selector in selectors:
            anchor = self._anchor_for_selector(selector)
            if anchor is None:
                page = _selector_page(selector)
                if page < 1 or page >= old_pages.page_count:
                    raise AuditError("page boundary out of range: {0}"
                                     .format(selector))
                yield (str(page), old_pages.bounds(page)[0],
                       new_pages.bounds(page)[0])
                continue
            # A fixed accession defines the edge itself, so crossing is
            # measured against that accession's position in each version.
            yield (str(anchor),
                   self._position(old_pages.board, anchor),
                   self._position(new_pages.board, anchor))


def _scheme_from_spec(spec):
    if spec is None:
        return None
    if isinstance(spec, (RankPageScheme, AnchorPageScheme)):
        return spec
    if isinstance(spec, (list, tuple)):
        return AnchorPageScheme(spec)
    if not isinstance(spec, dict):
        raise AuditError("page layout must be an object")

    anchors = spec.get("anchors", spec.get("page_anchors",
                                           spec.get("entry_anchors")))
    if anchors:
        return AnchorPageScheme(anchors)
    size = spec.get("size", spec.get("page_size"))
    if size:
        size = int(size)
        if size <= 0:
            raise AuditError("page size must be positive")
        return RankPageScheme(size=size)
    sizes = spec.get("sizes", spec.get("page_sizes"))
    if sizes:
        starts = [0]
        running = 0
        for size in sizes:
            size = int(size)
            if size <= 0:
                raise AuditError("page sizes must be positive")
            running += size
            starts.append(running)
        return RankPageScheme(starts=starts)
    breaks = spec.get("breaks", spec.get("page_breaks",
                                        spec.get("starts")))
    if breaks:
        return RankPageScheme(starts=[0] + [int(value) for value in breaks])
    raise AuditError("page layout needs size, sizes, breaks or anchors")


# ---------------------------------------------------------------------------
# Weighted inversion counting


class _Fenwick:
    def __init__(self, size):
        self.size = size
        self.tree = [0] * (size + 1)

    def add(self, index, value):
        index += 1
        while index <= self.size:
            self.tree[index] += value
            index += index & -index

    def prefix(self, index):
        total = 0
        while index > 0:
            total += self.tree[index]
            index -= index & -index
        return total

    def total(self):
        return self.prefix(self.size)

    def kth(self, order):
        index = 0
        bit = 1 << (self.size.bit_length() - 1)
        while bit:
            nxt = index + bit
            if nxt <= self.size and self.tree[nxt] < order:
                index = nxt
                order -= self.tree[nxt]
            bit >>= 1
        return index


def _inversion_summary(old_nodes, new_nodes, sample_limit=8):
    """Count weighted block inversions and return a bounded pair sample.

    Each record group is one weighted block.  An inversion contributes
    ``left.weight * right.weight`` original-card pairs, so a repeated-card
    range is never expanded.  Complexity is O(n log n), never O(n^2).
    """
    if not old_nodes:
        return {"pairs": 0, "group_pairs": 0, "samples": []}
    old_position = {node.group.index: index
                    for index, node in enumerate(old_nodes)}
    weight_tree = _Fenwick(len(old_nodes))
    count_tree = _Fenwick(len(old_nodes))
    total_weight = 0
    pairs = 0
    group_pairs = 0
    samples = []
    by_position = {index: node for index, node in enumerate(old_nodes)}

    for new_node in new_nodes:
        pos = old_position[new_node.group.index]
        weight_before = weight_tree.prefix(pos + 1)
        count_before = count_tree.prefix(pos + 1)
        pairs += new_node.weight * (total_weight - weight_before)
        group_pairs += count_tree.total() - count_before

        if len(samples) < sample_limit:
            above = count_tree.total() - count_before
            for offset in range(min(sample_limit - len(samples), above)):
                prior_pos = count_tree.kth(count_before + 1 + offset)
                samples.append((new_node, by_position[prior_pos]))

        weight_tree.add(pos, new_node.weight)
        count_tree.add(pos, 1)
        total_weight += new_node.weight
    return {"pairs": pairs, "group_pairs": group_pairs, "samples": samples}


# ---------------------------------------------------------------------------
# Audit


@dataclass
class AuditReport:
    mode: str
    tie_break: str
    levels: int
    groups: list
    old_board: Board
    new_board: Board
    table_diff: TableDiff = None
    pair_count: int = 0
    group_pair_count: int = 0
    sample_pairs: list = field(default_factory=list)
    page_moves: list = field(default_factory=list)
    page_ranges: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    key_changes: int = 0
    rank_moves: int = 0
    total_entries: int = 0
    distinct_names: int = 0
    old_pages: object = None
    new_pages: object = None
    chosen_pair: object = None

    def _row(self, group, include_keys=False):
        old_node = self.old_board.node_for(group)
        new_node = self.new_board.node_for(group)
        row = {
            "entry_first": group.entries.first,
            "entry_last": group.entries.last,
            "count": group.weight,
            "name": group.name,
            "old_rank_first": old_node.rank_start,
            "old_rank_last": old_node.rank_end,
            "new_rank_first": new_node.rank_start,
            "new_rank_last": new_node.rank_end,
            "old_page": None,
            "old_page_last": None,
            "new_page": None,
            "new_page_last": None,
            "key_changed": old_node.effective_key != new_node.effective_key,
            "rank_moved": (
                old_node.rank_start != new_node.rank_start or
                old_node.rank_end != new_node.rank_end
            ),
        }
        if self.old_pages is not None:
            row["old_page"] = self.old_pages.page_number(old_node.position)
            row["old_page_last"] = self.old_pages.page_number(
                old_node.position + max(old_node.weight - 1, 0))
        elif group.old_page is not None:
            row["old_page"] = group.old_page
            row["old_page_last"] = group.old_page
        if self.new_pages is not None:
            row["new_page"] = self.new_pages.page_number(new_node.position)
            row["new_page_last"] = self.new_pages.page_number(
                new_node.position + max(new_node.weight - 1, 0))
        if include_keys:
            row["old_key"] = str(group.old_key)
            row["new_key"] = str(group.new_key)
        return row

    def rank_at(self, entry, version="new"):
        board = self.new_board if version == "new" else self.old_board
        pages = self.new_pages if version == "new" else self.old_pages
        node = board.node_for_entry(entry)
        return {
            "entry": int(entry),
            "name": node.group.name,
            "rank_first": node.rank_start,
            "rank_end": node.rank_end,
            "page": (None if pages is None
                     else pages.page_number(node.position)),
            "page_label": (None if pages is None
                           else pages.label(pages.page_index(node.position))),
        }

    def pair_detail(self, entry_a, entry_b):
        """Explain both sorts for any chosen pair of accession numbers."""
        node_a = self.old_board.node_for_entry(entry_a)
        node_b = self.old_board.node_for_entry(entry_b)
        if node_a.group.index == node_b.group.index:
            if entry_a == entry_b:
                raise AuditError("the two entries must differ")
            # Two cards from the same repeated-name block share every key;
            # only the accession tie-break can separate them.
            same = self.new_board.node_for(node_a.group)
            tie_level = "entry" if self.tie_break == "entry" else "tie"
            tie_kind = "tie_break" if self.tie_break == "entry" else "tie"
            return {
                "left_entry": int(entry_a),
                "right_entry": int(entry_b),
                "left_name": node_a.group.name,
                "right_name": node_b.group.name,
                "old_order": ("left_first" if entry_a < entry_b
                              else "right_first"),
                "new_order": ("left_first" if entry_a < entry_b
                              else "right_first"),
                "swapped": False,
                "old_decision": {
                    "level": tie_level,
                    "level_index": None,
                    "left_entry": int(entry_a),
                    "right_entry": int(entry_b),
                    "kind": tie_kind,
                },
                "new_decision": {
                    "level": tie_level,
                    "level_index": None,
                    "left_entry": int(entry_a),
                    "right_entry": int(entry_b),
                    "kind": tie_kind,
                },
                "old_keys": [str(same.key), str(same.key)],
                "new_keys": [str(same.key), str(same.key)],
                "old_rank": [same.rank_start, same.rank_start],
                "new_rank": [same.rank_start, same.rank_start],
            }
        old_a = self.old_board.node_for(node_a.group)
        old_b = self.old_board.node_for(node_b.group)
        new_a = self.new_board.node_for(node_a.group)
        new_b = self.new_board.node_for(node_b.group)
        old_order = _order(old_a, old_b, self.tie_break)
        new_order = _order(new_a, new_b, self.tie_break)
        return {
            "left_entry": int(entry_a),
            "right_entry": int(entry_b),
            "left_name": node_a.group.name,
            "right_name": node_b.group.name,
            "old_order": old_order,
            "new_order": new_order,
            "swapped": (old_order is not None and new_order is not None and
                        old_order != new_order),
            "old_rank": [old_a.rank_start, old_b.rank_start],
            "new_rank": [new_a.rank_start, new_b.rank_start],
            "old_decision": first_difference(
                old_a.key, old_b.key, self.levels,
                old_a.first_entry, old_b.first_entry),
            "new_decision": first_difference(
                new_a.key, new_b.key, self.levels,
                new_a.first_entry, new_b.first_entry),
            "old_keys": [str(old_a.key), str(old_b.key)],
            "new_keys": [str(new_a.key), str(new_b.key)],
        }

    def page_range(self, page):
        """Accession segments for one page, without materializing all pages."""
        if self.old_pages is None:
            raise AuditError("no page layout was configured")
        index = int(page) - 1
        if index < 0 or index >= self.old_pages.page_count:
            raise AuditError("page out of range: {0}".format(page))
        return {
            "old": _page_range_row(
                self.old_board, self.old_pages, index),
            "new": _page_range_row(
                self.new_board, self.new_pages, index),
        }

    def to_dict(self, max_records=None, expand=False, pair=None,
                include_keys=False, include_page_ranges=True):
        if expand and self.total_entries > MAX_EXPANDED_ENTRIES:
            raise AuditError(
                "refusing to expand {0} entries; deliver compressed ranges"
                .format(self.total_entries))
        records = []
        for group in self.groups:
            row = self._row(group, include_keys=include_keys)
            if expand:
                row["entries"] = list(group.entries)
            records.append(row)
        if max_records is not None:
            records = records[:max_records]

        result = {
            "mode": self.mode,
            "tie_break": self.tie_break,
            "levels": self.levels,
            "table_diff": (self.table_diff.to_dict()
                           if self.table_diff else None),
            "summary": {
                "entries": self.total_entries,
                "groups": len(self.groups),
                "distinct_names": self.distinct_names,
                "key_changes": self.key_changes,
                "rank_moves": self.rank_moves,
                "swapped_pairs": self.pair_count,
                "swapped_group_pairs": self.group_pair_count,
                "page_moves": len(self.page_moves),
            },
            "records": records,
            "sample_pairs": self.sample_pairs,
            "altered_pairs": [
                row for row in records
                if row["key_changed"] or row["rank_moved"] or
                (row["old_page"] is not None and
                 (row["old_page"], row["old_page_last"]) !=
                 (row["new_page"], row["new_page_last"]))
            ],
            "page_moves": self.page_moves,
            "warnings": self.warnings,
        }
        if include_page_ranges:
            result["page_ranges"] = self.page_ranges
        if self.chosen_pair is not None:
            result["pair"] = self.chosen_pair
        if pair is not None:
            result["pair"] = (pair if isinstance(pair, dict)
                              else self.pair_detail(*pair))
        return result


def _order(left, right, tie_break):
    if left.position < right.position:
        return "left_first"
    if left.position > right.position:
        return "right_first"
    if left.effective_key == right.effective_key:
        if tie_break == "entry":
            if left.first_entry < right.first_entry:
                return "left_first"
            if left.first_entry > right.first_entry:
                return "right_first"
        return None
    return "left_first" if left.effective_key < right.effective_key \
        else "right_first"


def _load_old_keys(groups, old_collator, verify, warnings, key_cache,
                   levels):
    for group in groups:
        stored = (Key.from_value(group.old_key)
                  if group.old_key is not None else None)
        if old_collator is None:
            if stored is None:
                raise AuditError(
                    "record {0} has no old key and no old table was supplied"
                    .format(group.entries.first))
            if stored.depth < levels:
                raise AuditError(
                    "record {0} has a primary-only old key but full keys "
                    "were requested".format(group.entries.first))
            group.old_key = stored
            continue

        if stored is not None and stored.depth >= levels and not verify:
            group.old_key = stored
            continue

        computed = key_cache.get(group.name)
        if computed is None:
            computed = Key.from_value(
                old_collator.sort_key_levels(group.name, 4))
            key_cache[group.name] = computed
        group.old_key = computed
        if stored is not None and verify:
            if stored.depth < levels:
                warnings.append({
                    "kind": "old_key_incomplete",
                    "entry_first": group.entries.first,
                    "entry_last": group.entries.last,
                    "stored_depth": stored.depth,
                    "computed": str(computed),
                })
            elif not computed.matches(stored, levels):
                warnings.append({
                    "kind": "old_key_mismatch",
                    "entry_first": group.entries.first,
                    "entry_last": group.entries.last,
                    "stored": str(stored),
                    "computed": str(computed),
                })


def _new_keys(groups, new_collator, key_cache):
    for group in groups:
        cached = key_cache.get(group.name)
        if cached is None:
            cached = Key.from_value(
                new_collator.sort_key_levels(group.name, 4))
            key_cache[group.name] = cached
        group.new_key = cached


def _affected_codepoints(table_diff):
    affected = set()
    wide = []
    for change in table_diff.rule_changes:
        affected.update(change["codepoints"])
    for change in table_diff.implicit_changes:
        start = change["start"]
        end = change["end"]
        if end - start > 100000:
            # Broad ranges are represented by their bounds; names containing
            # anything inside are still caught by the containment test below.
            wide.append((start, end))
            continue
        for codepoint in range(start, end + 1):
            affected.add(codepoint)
    return affected, wide


def _name_touches(name, affected):
    singles, wide = affected
    for char in unicodedata.normalize("NFD", name):
        codepoint = ord(char)
        if codepoint in singles:
            return True
        for start, end in wide:
            if start <= codepoint <= end:
                return True
    return False


def _reuse_unchanged_keys(groups, new_collator, key_cache, table_diff,
                          levels):
    """Generate new keys only for names touched by the approved changes."""
    affected = _affected_codepoints(table_diff)
    pending = []
    for group in groups:
        if group.old_key is None or group.old_key.depth < levels:
            pending.append(group)
            continue
        if _name_touches(group.name, affected):
            pending.append(group)
            continue
        group.new_key = group.old_key
        key_cache.setdefault(group.name, group.new_key)
    for group in pending:
        cached = key_cache.get(group.name)
        if cached is None:
            cached = Key.from_value(
                new_collator.sort_key_levels(group.name, 4))
            key_cache[group.name] = cached
        group.new_key = cached


def _page_moves(groups, old_board, new_board, scheme, selectors):
    if scheme is None:
        return [], None, None
    old_pages = scheme.pages_for(old_board)
    new_pages = scheme.pages_for(new_board)
    boundary_rows = []
    if selectors:
        boundary_rows = list(scheme.boundary_definitions(
            selectors, old_pages, new_pages))

    moves = []
    for group in groups:
        old_node = old_board.node_for(group)
        new_node = new_board.node_for(group)
        old_start = old_pages.page_number(old_node.position)
        old_end = old_pages.page_number(
            old_node.position + max(old_node.weight - 1, 0))
        new_start = new_pages.page_number(new_node.position)
        new_end = new_pages.page_number(
            new_node.position + max(new_node.weight - 1, 0))
        crossed = []
        for label, old_threshold, new_threshold in boundary_rows:
            old_after = old_node.position >= old_threshold
            new_after = new_node.position >= new_threshold
            old_end_after = (old_node.position +
                             max(old_node.weight - 1, 0)) >= old_threshold
            new_end_after = (new_node.position +
                             max(new_node.weight - 1, 0)) >= new_threshold
            if (old_after != new_after) or (old_end_after != new_end_after):
                crossed.append(label)
        if selectors and not crossed:
            continue
        if not selectors and (old_start, old_end) == (new_start, new_end):
            continue
        moves.append({
            "entry_first": group.entries.first,
            "entry_last": group.entries.last,
            "count": group.weight,
            "name": group.name,
            "from_page": old_start,
            "from_page_last": old_end,
            "to_page": new_start,
            "to_page_last": new_end,
            "from_label": old_pages.label(old_start - 1),
            "to_label": new_pages.label(new_start - 1),
            "crossed_boundaries": crossed,
            "key_changed": (
                old_node.effective_key != new_node.effective_key),
            "rank_before": old_node.rank_start,
            "rank_after": new_node.rank_start,
        })
    return moves, old_pages, new_pages


def _merge_ranges(ranges):
    merged = []
    def start_of(item):
        return item["start"] if isinstance(item, dict) else item[0]

    for item in sorted(ranges, key=lambda value: start_of(value)):
        if isinstance(item, dict):
            if (merged and isinstance(merged[-1], dict) and
                    merged[-1]["step"] == item["step"] and
                    item["start"] <= merged[-1]["end"] + item["step"]):
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            else:
                merged.append(dict(item))
        else:
            start, end = item
            if (merged and not isinstance(merged[-1], dict) and
                    start <= merged[-1][1] + 1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
    return [item if isinstance(item, dict) else [item[0], item[1]]
            for item in merged]


def _normalise_page_ranges(ranges):
    """Merge equal-step segments and turn singleton steps back into pairs."""
    merged = _merge_ranges(ranges)
    normalised = []
    for item in merged:
        if isinstance(item, dict):
            if item["step"] != 1:
                normalised.append(item)
            else:
                normalised.append([item["start"], item["end"]])
            continue
        if normalised and not isinstance(normalised[-1], dict) and \
                item[0] <= normalised[-1][1] + 1:
            normalised[-1][1] = max(normalised[-1][1], item[1])
            continue
        if normalised and isinstance(normalised[-1], dict) and \
                normalised[-1]["step"] == 1 and \
                item[0] <= normalised[-1]["end"] + 1:
            previous = normalised.pop()
            normalised.append([previous["start"],
                               max(previous["end"], item[1])])
            continue
        normalised.append(list(item))
    return normalised


def _page_range_row(board, pages, index):
    start, end = pages.bounds(index)
    start = max(start, 0)
    end = min(end, board.total)
    ranges = []
    positions = [node.position for node in board.nodes]
    cursor = max(0, bisect.bisect_right(positions, start) - 1)
    for node in board.nodes[cursor:]:
        node_start = node.position
        node_end = node.position + node.weight
        if node_end <= start:
            continue
        if node_start >= end:
            break
        local_start = max(start, node_start) - node_start
        local_end = min(end, node_end) - node_start
        first = node.group.entries.entry_at(local_start)
        last = node.group.entries.entry_at(local_end - 1)
        step = node.group.entries.step
        ranges.append([first, last] if step == 1 else
                      {"start": first, "end": last, "step": step})
    return {
        "page": index + 1,
        "label": pages.label(index),
        "ranges": _normalise_page_ranges(ranges),
        "count": max(0, end - start),
    }


def _page_ranges(board, pages):
    if pages is None:
        return []
    limit = min(pages.page_count, MAX_PAGE_RANGES)
    truncated = pages.page_count > limit
    result = [_page_range_row(board, pages, index)
              for index in range(limit)]
    if truncated:
        result.append({
            "truncated": True,
            "page_count": pages.page_count,
            "reported_pages": limit,
        })
    return result


def _pair_witness(pair):
    return {
        "left_entry": pair["left_entry"],
        "right_entry": pair["right_entry"],
        "left_name": pair["left_name"],
        "right_name": pair["right_name"],
        "old_order": pair["old_order"],
        "new_order": pair["new_order"],
        "swapped": pair["swapped"],
        "old_decision": pair["old_decision"],
        "new_decision": pair["new_decision"],
        "old_keys": pair["old_keys"],
        "new_keys": pair["new_keys"],
    }


def audit(old, new, catalog, mode="full", tie_break="entry",
          pages=None, boundaries=None, approvals=None, verify_old_keys=None,
          sample_pairs=8, base_dir=None, trust_old_keys=None,
          large_threshold=LARGE_THRESHOLD, pair=None,
          include_page_ranges=True):
    """Compare the archived catalog under two collation tables.

    ``mode`` is ``"full"`` (all four UCA levels) or ``"primary"`` (the
    primary index sheet).  ``tie_break`` is ``"entry"`` for the first
    accession number, or ``"none"`` for tied ranks.

    ``trust_old_keys`` defaults to true for catalogs above
    ``large_threshold`` entries when every record has a stored key deep
    enough for the selected mode.  Small catalogs recompute both sides
    through the two full tables; stored old keys are then checked (and a
    mismatch is reported) unless ``verify_old_keys`` is false.  Set
    ``trust_old_keys`` explicitly when only the archived old index is
    available.

    For a large order, ``old_key`` must be the genuinely stored key, not a
    primary-only index entry.  Set ``old_index`` for the actual old key
    table and let catalog records reference their accession ranges.

    On large orders, new keys are regenerated only for distinct names that
    contain a codepoint affected by an approved rule or implicit-range change;
    names outside the change set reuse the old key.  The check is performed
    on the NFD form, so a precomposed character whose decomposition contains
    a changed mark is still regenerated.
    """
    if mode not in ("full", "primary"):
        raise AuditError("mode must be 'full' or 'primary'")
    if tie_break not in ("entry", "none"):
        raise AuditError("tie_break must be 'entry' or 'none'")
    levels = 4 if mode == "full" else 1

    old_collator = (load_collator(old, base_dir=base_dir)
                    if old is not None else None)
    new_collator = load_collator(new, base_dir=base_dir)
    groups = load_catalog(catalog)
    total_entries = sum(group.weight for group in groups)

    complete_index = bool(groups) and all(
        group.old_key is not None and
        Key.from_value(group.old_key).depth >= levels
        for group in groups
    )
    if trust_old_keys is None:
        # Small orders are checked by sorting the same source text through
        # both complete tables.  Stored keys become the authority only for a
        # large order whose complete old index can be reused, or when the
        # caller explicitly asks to trust them (for example because no old
        # table is available).
        trust_old_keys = (complete_index and
                          total_entries > large_threshold)

    if verify_old_keys is None:
        # Verifying stored keys is a full extra pass, so make it explicit
        # for large catalogs; small ones can afford it.
        verify_old_keys = total_entries <= large_threshold

    warnings = []
    old_cache = {}
    if trust_old_keys:
        for group in groups:
            group.old_key = Key.from_value(group.old_key)
        if verify_old_keys:
            if old_collator is None:
                raise AuditError(
                    "verifying stored old keys needs the old table")
            for group in groups:
                computed = old_cache.get(group.name)
                if computed is None:
                    computed = Key.from_value(
                        old_collator.sort_key_levels(group.name, 4))
                    old_cache[group.name] = computed
                if not computed.matches(group.old_key, levels):
                    warnings.append({
                        "kind": "old_key_mismatch",
                        "entry_first": group.entries.first,
                        "entry_last": group.entries.last,
                        "stored": str(group.old_key),
                        "computed": str(computed),
                    })
    else:
        _load_old_keys(groups, old_collator, verify_old_keys,
                       warnings, old_cache, levels)

    table_diff = (diff_tables(old_collator, new_collator,
                              approvals=approvals)
                  if old_collator is not None else None)

    new_cache = {}
    if (trust_old_keys and total_entries > large_threshold and
            table_diff is not None):
        _reuse_unchanged_keys(groups, new_collator, new_cache, table_diff,
                              levels)
    else:
        _new_keys(groups, new_collator, new_cache)

    old_items = [(group, group.old_key) for group in groups]
    new_items = [(group, group.new_key) for group in groups]
    old_nodes, old_total = _sort_nodes(old_items, levels, tie_break)
    new_nodes, new_total = _sort_nodes(new_items, levels, tie_break)
    if tie_break == "none":
        _apply_tied_ranks(old_nodes)
        _apply_tied_ranks(new_nodes)

    old_board = Board(old_nodes, old_total, tie_break)
    new_board = Board(new_nodes, new_total, tie_break)
    scheme = _scheme_from_spec(pages)
    moves, old_pages, new_pages = _page_moves(
        groups, old_board, new_board, scheme, boundaries)

    inversion = _inversion_summary(
        old_nodes, new_nodes, sample_limit=sample_pairs)

    key_changes = sum(
        1 for group in groups
        if old_board.node_for(group).effective_key !=
        new_board.node_for(group).effective_key)
    rank_moves = sum(
        1 for group in groups
        if old_board.node_for(group).rank_start !=
        new_board.node_for(group).rank_start)

    report = AuditReport(
        mode=mode,
        tie_break=tie_break,
        levels=levels,
        groups=groups,
        old_board=old_board,
        new_board=new_board,
        table_diff=table_diff,
        pair_count=inversion["pairs"],
        group_pair_count=inversion["group_pairs"],
        page_moves=moves,
        warnings=warnings,
        key_changes=key_changes,
        rank_moves=rank_moves,
        total_entries=old_total,
        distinct_names=len({group.name for group in groups}),
        old_pages=old_pages,
        new_pages=new_pages,
    )
    if include_page_ranges and old_pages is not None:
        report.page_ranges = {
            "old": _page_ranges(old_board, old_pages),
            "new": _page_ranges(new_board, new_pages),
        }
    report.sample_pairs = []
    for left, right in inversion["samples"]:
        detail = report.pair_detail(right.first_entry, left.first_entry)
        report.sample_pairs.append(_pair_witness(detail))
    if pair is not None:
        report.chosen_pair = report.pair_detail(*pair)
    return report


# ---------------------------------------------------------------------------
# Text output


def _short(text, width=32):
    text = str(text)
    if len(text) <= width:
        return text
    return text[:width - 1] + "..."


def _difference_text(difference):
    level = difference["level"]
    if level == "entry":
        return "entry {0} vs {1}".format(
            difference["left_entry"], difference["right_entry"])
    if level == "tie":
        return "tie (entries {0} and {1})".format(
            difference.get("left_entry"), difference.get("right_entry"))
    if difference["kind"] == "prefix":
        return "{0} prefix ending at position {1}".format(
            level, difference["position"])
    return "{0} position {1} ({2!s} vs {3!s})".format(
        level, difference["position"],
        difference["left_weight"], difference["right_weight"])


def format_report(report, max_records=40, pair=None, include_page_ranges=True):
    data = report.to_dict(max_records=max_records, pair=pair,
                          include_page_ranges=include_page_ranges)
    lines = []
    summary = data["summary"]
    lines.append("Collation audit ({0}, tie-break {1})".format(
        report.mode, report.tie_break))
    lines.append(
        "  entries={0} groups={1} distinct_names={2}".format(
            summary["entries"], summary["groups"],
            summary["distinct_names"]))
    lines.append(
        "  key_changes={0} rank_moves={1} swapped_pairs={2} "
        "swapped_group_pairs={3} page_moves={4}".format(
            summary["key_changes"], summary["rank_moves"],
            summary["swapped_pairs"], summary["swapped_group_pairs"],
            summary["page_moves"]))

    diff = data["table_diff"]
    if diff:
        lines.append("")
        lines.append("Table changes")
        lines.append(
            "  rules={0} contractions={1} implicit_ranges={2}".format(
                diff["summary"]["rules"], diff["summary"]["contractions"],
                diff["summary"]["implicit_ranges"]))
        for change in diff["rule_changes"][:12]:
            marker = "contraction" if change["is_contraction"] else "rule"
            lines.append("  {0:9} {1:11} {2}".format(
                change["kind"], marker, change["key"]))
        if len(diff["rule_changes"]) > 12:
            lines.append("  ... {0} more rule changes".format(
                len(diff["rule_changes"]) - 12))
        for change in diff["implicit_changes"][:8]:
            lines.append(
                "  {0:9} implicit  {1:04X}..{2:04X} base {3!s} -> {4!s}"
                .format(change["kind"], change["start"], change["end"],
                        change["old_base"], change["new_base"]))

    lines.append("")
    lines.append("Records")
    lines.append("  {0:>11} {1:>11} {2:>11} {3:>10} {4}".format(
        "entry", "old_rank", "new_rank", "pages", "name"))
    for row in data["records"]:
        entry = str(row["entry_first"])
        if row["entry_last"] != row["entry_first"]:
            entry = "{0}-{1}".format(row["entry_first"], row["entry_last"])
        old_rank = str(row["old_rank_first"])
        new_rank = str(row["new_rank_first"])
        if row["old_rank_last"] != row["old_rank_first"]:
            old_rank = "{0}-{1}".format(
                row["old_rank_first"], row["old_rank_last"])
        if row["new_rank_last"] != row["new_rank_first"]:
            new_rank = "{0}-{1}".format(
                row["new_rank_first"], row["new_rank_last"])
        pages = "-"
        if row["old_page"] is not None:
            pages = "{0}->{1}".format(row["old_page"], row["new_page"])
        flags = []
        if row["key_changed"]:
            flags.append("key")
        if row["rank_moved"]:
            flags.append("rank")
        lines.append("  {0:>11} {1:>11} {2:>11} {3:>10} {4} {5}".format(
            entry, old_rank, new_rank, pages, _short(row["name"]),
            ",".join(flags)))
    if len(report.groups) > len(data["records"]):
        lines.append("  ... {0} more groups".format(
            len(report.groups) - len(data["records"])))

    if data["sample_pairs"]:
        lines.append("")
        lines.append("Sample swapped pairs")
        for item in data["sample_pairs"]:
            lines.append(
                "  {0} vs {1}: old={2}, new={3}".format(
                    item["left_entry"], item["right_entry"],
                    item["old_order"], item["new_order"]))
            lines.append(
                "    old differs at {0}; new differs at {1}".format(
                    _difference_text(item["old_decision"]),
                    _difference_text(item["new_decision"])))

    if data["page_moves"]:
        lines.append("")
        lines.append("Page moves")
        for move in data["page_moves"][:40]:
            entry = str(move["entry_first"])
            if move["entry_last"] != move["entry_first"]:
                entry = "{0}-{1}".format(
                    move["entry_first"], move["entry_last"])
            extra = ""
            if move["crossed_boundaries"]:
                extra = " crossed={0}".format(move["crossed_boundaries"])
            key_state = "key-changed" if move["key_changed"] else "key-same"
            lines.append(
                "  {0}: page {1}->{2} to {3}->{4} ({5}){6}".format(
                    entry, move["from_page"], move["to_page"],
                    move["from_page_last"], move["to_page_last"],
                    key_state, extra))
        if len(data["page_moves"]) > 40:
            lines.append("  ... {0} more page moves".format(
                len(data["page_moves"]) - 40))

    if "pair" in data:
        detail = data["pair"]
        lines.append("")
        lines.append("Chosen pair")
        lines.append("  {0} ({1!r}) vs {2} ({3!r})".format(
            detail["left_entry"], detail["left_name"],
            detail["right_entry"], detail["right_name"]))
        lines.append("  old order={0}; deciding {1}".format(
            detail["old_order"], _difference_text(detail["old_decision"])))
        lines.append("  new order={0}; deciding {1}".format(
            detail["new_order"], _difference_text(detail["new_decision"])))

    if data["warnings"]:
        lines.append("")
        lines.append("Warnings")
        for warning in data["warnings"][:20]:
            lines.append("  {0}".format(warning))
    return "\n".join(lines)


def load_catalog_file(path):
    with open(path, encoding="utf-8") as catalog_file:
        return json.load(catalog_file)


def run_catalog_file(path, old=None, new=None, **options):
    data = load_catalog_file(path)
    base_dir = options.pop("base_dir", None)
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(path))
    if old is None:
        old = data.get("old_table", data.get("old"))
    if new is None:
        new = data.get("new_table", data.get("new"))
    if old is None or new is None:
        raise AuditError("catalog needs old_table and new_table")
    for key in ("mode", "tie_break", "pages", "boundaries", "approvals",
                "trust_old_keys", "large_threshold", "pair"):
        if options.get(key) is None and key in data:
            options[key] = data[key]
    if options.get("mode") is None:
        options["mode"] = data.get("mode", "full")
    if options.get("tie_break") is None:
        options["tie_break"] = data.get("tie_break", "entry")
    return audit(old, new, data, base_dir=base_dir, **options)


