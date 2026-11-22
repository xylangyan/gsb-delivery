"""Compare legacy and modern scalar readings and plan lossless fixes.

The engine parses the same YAML text twice — once per rule set — and
walks the two representation trees in parallel.  Every scalar whose
*constructed value* differs between the rules is reported with its
source line and its old/new (legacy/modern) readings.

Correction actions, chosen so the rewritten text reads identically under
both rules:

* ``tag``     — prefix an explicit tag (``!!bool``/``!!timestamp``);
                both loaders share those constructors.
* ``literal`` — replace an octal/sexagesimal scalar with the canonical
                decimal/float literal of the preserved value.
* ``quote``   — double-quote the scalar so both rules read it as the
                preserved string.

Mappings merged from anchors can collapse keys under one rule while the
other rule keeps them distinct.  Such a merge is irreversible from the
constructed data, so it is reported as a conflict and blocks rewriting.
"""

__all__ = ['ScalarRow', 'MergeConflict', 'scan', 'plan_rewrite',
           'quote_rewrite', 'find_merge_conflicts', 'load',
           'get_at', 'tag_shorthand']

import datetime
import json

from yaml.nodes import ScalarNode, SequenceNode, MappingNode

from .rules import LEGACY_RULE, MODERN_RULE, get_loader

TAG_PREFIX = 'tag:yaml.org,2002:'


def tag_shorthand(tag):
    if tag and tag.startswith(TAG_PREFIX):
        return '!!' + tag[len(TAG_PREFIX):]
    return tag


def load(text, rule):
    loader = get_loader(rule)(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def get_at(data, path):
    cur = data
    for segment in path:
        cur = cur[segment]
    return cur


def _compose(text, rule):
    loader = get_loader(rule)(text)
    return loader, loader.get_single_node()


def _describe(loader, node):
    value = loader.construct_object(node, deep=True)
    return "%s %r" % (type(value).__name__, value), value


class ScalarRow:
    def __init__(self, path, kind, line, column, start, end, text,
                 legacy_tag, legacy_read, legacy_value,
                 modern_tag, modern_read, modern_value,
                 action, replacement, blocked=False):
        self.path = path
        self.kind = kind
        self.line = line
        self.column = column
        self.start = start
        self.end = end
        self.text = text
        self.legacy_tag = legacy_tag
        self.legacy_read = legacy_read
        self.legacy_value = legacy_value
        self.modern_tag = modern_tag
        self.modern_read = modern_read
        self.modern_value = modern_value
        self.action = action
        self.replacement = replacement
        self.blocked = blocked

    def __repr__(self):
        return ('ScalarRow(line=%d, kind=%r, text=%r, legacy=%r, modern=%r, '
                'action=%r, replacement=%r, blocked=%r)'
                % (self.line, self.kind, self.text, self.legacy_read,
                   self.modern_read, self.action, self.replacement,
                   self.blocked))


class MergeConflict:
    def __init__(self, line, key_groups):
        self.line = line
        self.key_groups = key_groups


def _plan_action(legacy_value, modern_value, text, preserve):
    preserved = legacy_value if preserve == LEGACY_RULE else modern_value
    if isinstance(preserved, bool):
        return 'tag', '!!bool ' + text
    if isinstance(preserved, (datetime.datetime, datetime.date)):
        return 'tag', '!!timestamp ' + text
    if isinstance(preserved, str):
        return 'quote', json.dumps(preserved)
    if isinstance(preserved, int):
        return 'literal', str(preserved)
    if isinstance(preserved, float):
        return 'literal', repr(preserved)
    return None, None


def _walk_scalars(legacy_node, modern_node, legacy_loader, modern_loader,
                  path, kind, ancestor_map_lines, conflict_lines, rows,
                  preserve, seen):
    node_id = id(legacy_node)
    if node_id in seen:
        return
    seen.add(node_id)
    if isinstance(legacy_node, ScalarNode):
        legacy_read, legacy_value = _describe(legacy_loader, legacy_node)
        modern_read, modern_value = _describe(modern_loader, modern_node)
        if (legacy_node.tag != modern_node.tag
                or legacy_value != modern_value
                or type(legacy_value) is not type(modern_value)):
            blocked = any(line in conflict_lines
                          for line in ancestor_map_lines)
            action, replacement = (None, None) if blocked else \
                _plan_action(legacy_value, modern_value,
                             legacy_node.value, preserve)
            rows.append(ScalarRow(
                path=path, kind=kind,
                line=legacy_node.start_mark.line + 1,
                column=legacy_node.start_mark.column + 1,
                start=legacy_node.start_mark.index,
                end=legacy_node.end_mark.index,
                text=legacy_node.value,
                legacy_tag=legacy_node.tag,
                legacy_read=legacy_read,
                legacy_value=legacy_value,
                modern_tag=modern_node.tag,
                modern_read=modern_read,
                modern_value=modern_value,
                action=action, replacement=replacement, blocked=blocked))
        return
    if isinstance(legacy_node, SequenceNode):
        for index, (lchild, mchild) in enumerate(
                zip(legacy_node.value, modern_node.value)):
            _walk_scalars(lchild, mchild, legacy_loader, modern_loader,
                          path + (index,), 'value', ancestor_map_lines,
                          conflict_lines, rows, preserve, seen)
        return
    if isinstance(legacy_node, MappingNode):
        legacy_loader.flatten_mapping(legacy_node)
        modern_loader.flatten_mapping(modern_node)
        map_lines = ancestor_map_lines + (legacy_node.start_mark.line,)
        for (lkey, lval), (mkey, mval) in zip(legacy_node.value,
                                              modern_node.value):
            _walk_scalars(lkey, mkey, legacy_loader, modern_loader,
                          path, 'key', map_lines, conflict_lines, rows,
                          preserve, seen)
            key_segment = lkey.value if isinstance(lkey, ScalarNode) else None
            _walk_scalars(lval, mval, legacy_loader, modern_loader,
                          path + (key_segment,), 'value', map_lines,
                          conflict_lines, rows, preserve, seen)


def _index_mappings(node, index):
    if isinstance(node, MappingNode):
        index[node.start_mark.line] = node
        for key_node, value_node in node.value:
            _index_mappings(key_node, index)
            _index_mappings(value_node, index)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _index_mappings(child, index)


def find_merge_conflicts(text):
    legacy_loader, legacy_root = _compose(text, LEGACY_RULE)
    modern_loader, modern_root = _compose(text, MODERN_RULE)
    legacy_maps, modern_maps = {}, {}
    _index_mappings(legacy_root, legacy_maps)
    _index_mappings(modern_root, modern_maps)

    conflicts = []
    try:
        for line in sorted(set(legacy_maps) & set(modern_maps)):
            legacy_map_node = legacy_maps[line]
            modern_map_node = modern_maps[line]
            legacy_loader.flatten_mapping(legacy_map_node)
            modern_loader.flatten_mapping(modern_map_node)

            def key_records(loader, node):
                records = []
                for key_node, _ in node.value:
                    key = loader.construct_object(key_node, deep=True)
                    records.append((key_node.start_mark.line + 1,
                                    key_node.value if isinstance(
                                        key_node, ScalarNode) else None,
                                    key))
                return records

            legacy_keys = key_records(legacy_loader, legacy_map_node)
            modern_keys = key_records(modern_loader, modern_map_node)

            def collapsed(records):
                groups = {}
                for source_line, _scalar, key in records:
                    groups.setdefault(repr(key), set()).add(source_line)
                return {key: lines for key, lines in groups.items()
                        if len(lines) > 1}

            if (collapsed(legacy_keys) != collapsed(modern_keys)
                    or len({repr(k) for _, _, k in legacy_keys})
                    != len({repr(k) for _, _, k in modern_keys})):
                key_groups = []
                all_lines = sorted({line for line, _, _ in legacy_keys}
                                   | {line for line, _, _ in modern_keys})
                for source_line in all_lines:
                    legacy_entry = next(
                        ((scalar, key) for line, scalar, key in legacy_keys
                         if line == source_line), None)
                    modern_entry = next(
                        ((scalar, key) for line, scalar, key in modern_keys
                         if line == source_line), None)
                    key_groups.append((source_line, legacy_entry,
                                       modern_entry))
                conflicts.append(MergeConflict(line=line + 1,
                                               key_groups=key_groups))
    finally:
        legacy_loader.dispose()
        modern_loader.dispose()
    return conflicts


def scan(text, preserve=LEGACY_RULE):
    if preserve not in (LEGACY_RULE, MODERN_RULE):
        raise ValueError("preserve must be %r or %r"
                         % (LEGACY_RULE, MODERN_RULE))
    conflicts = find_merge_conflicts(text)
    conflict_lines = {conflict.line - 1 for conflict in conflicts}
    legacy_loader, legacy_root = _compose(text, LEGACY_RULE)
    modern_loader, modern_root = _compose(text, MODERN_RULE)
    rows = []
    try:
        _walk_scalars(legacy_root, modern_root, legacy_loader, modern_loader,
                      (), 'value', (), conflict_lines, rows, preserve,
                      set())
    finally:
        legacy_loader.dispose()
        modern_loader.dispose()
    return rows, conflicts


def _edits(text, rows, quote_only=False):
    edits = []
    for row in rows:
        if row.blocked or row.action is None:
            continue
        if quote_only and row.action == 'quote':
            replacement = json.dumps(row.text)
        else:
            replacement = row.replacement
        edits.append((row.start, row.end, replacement, row))
    edits.sort(key=lambda item: item[0], reverse=True)
    return edits


def plan_rewrite(text, preserve=LEGACY_RULE):
    """Rewrite with tags/literal corrections; conflicts block output."""
    rows, conflicts = scan(text, preserve)
    if conflicts:
        raise ValueError("irreversible merge conflict; resolve before rewrite")
    rewritten = text
    for start, end, replacement, _row in _edits(text, rows):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten, rows


def quote_rewrite(text, preserve=MODERN_RULE):
    """Double-quote every divergent scalar as a preserved string."""
    rows, conflicts = scan(text, preserve)
    if conflicts:
        raise ValueError("irreversible merge conflict; resolve before rewrite")
    rewritten = text
    for start, end, replacement, _row in _edits(text, rows,
                                                quote_only=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten, rows
