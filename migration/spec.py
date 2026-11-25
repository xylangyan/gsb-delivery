"""Data model for a collector handover sheet (移交单).

A handover sheet describes, for every collecting segment (写端):

* the record template (字段域、共用原值关系、许可组合),
* the old and final MessagePack extension tables (旧表/终表),
* any borrowable temporary extension codes (可借的号),

and for every reader (读端) which ext handlers it may start/stop
(准许启停的处理器) and which ext types it resolves through registered
``ext_serializable`` classes.

The sheet is plain JSON; ``load`` validates it and infers the decoded
business type of every key element and value so that type mismatches in
the conversion rules (查表/符号扩展/整数缩放) are rejected up front.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

KIND_INT = "int"
KIND_BOOL = "bool"
KIND_TIMESTAMP = "timestamp"
KIND_STR = "str"

VALUE_TYPES = (KIND_INT, KIND_BOOL, KIND_STR, KIND_TIMESTAMP)

OP_LOOKUP = "lookup"
OP_SIGN_EXTEND = "sign_extend"
OP_SCALE = "scale"


class SpecError(ValueError):
    """Raised when a handover sheet is internally inconsistent."""


@dataclass(frozen=True)
class Rule:
    op: str
    table: tuple = ()   # lookup: sorted tuple of (input, output) pairs
    bits: int = 0       # sign_extend
    mul: int = 1        # scale
    add: int = 0        # scale


@dataclass(frozen=True)
class Elem:
    """One key element: either a constant or a field ref plus rules."""

    is_const: bool
    const: object = None          # int / bool / str
    field: str = ""
    rules: tuple = ()             # tuple[Rule, ...]


@dataclass(frozen=True)
class ValueSpec:
    is_const: bool
    const: object = None
    field: str = ""
    rules: tuple = ()
    type: str = KIND_INT          # declared business type


@dataclass(frozen=True)
class Field:
    name: str
    kind: str                     # int / bool / timestamp
    lo: int = 0                   # int domains only
    hi: int = 0


@dataclass(frozen=True)
class PermittedCombo:
    fields: tuple                 # tuple[str, ...]
    allow: frozenset              # frozenset[tuple[int, ...]]


@dataclass(frozen=True)
class Template:
    fields: dict                  # name -> Field
    permitted: tuple              # tuple[PermittedCombo, ...]


@dataclass(frozen=True)
class Table:
    table_id: str
    ext_code: int
    key: tuple                    # nested tuple Elem / tuple
    value: ValueSpec
    borrowed: bool = False


@dataclass(frozen=True)
class Segment:
    name: str
    writer: str
    sinks: tuple
    template: Template
    tables: dict                  # table_id -> Table (old/final/borrowable)
    old: str                      # table_id of the old table
    final: str                    # table_id of the final table
    borrowable: tuple             # tuple of borrowable table_ids


@dataclass(frozen=True)
class Reader:
    name: str
    eh_initial: frozenset         # ext codes with a passed handler at start
    eh_may_toggle: frozenset      # ext codes whose handler may be started/stopped
    registered: frozenset         # ext codes resolved via registered classes

    def decodable(self, code):
        return code in self.registered or code in self.eh_initial \
            or code in self.eh_may_toggle


@dataclass(frozen=True)
class Handover:
    name: str
    segments: dict                # name -> Segment
    readers: tuple                # tuple[Reader, ...]
    shared: tuple                 # tuple of tuples of "segment.field"

    def table(self, segment, table_id):
        return self.segments[segment].tables[table_id]


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def _parse_rule(raw, where):
    if not isinstance(raw, dict) or "op" not in raw:
        raise SpecError("%s: rule must be an object with an 'op'" % where)
    op = raw["op"]
    if op == OP_LOOKUP:
        table = raw.get("table")
        if not isinstance(table, dict) or not table:
            raise SpecError("%s: lookup rule needs a non-empty 'table'" % where)
        pairs = []
        for key, val in table.items():
            try:
                k = int(key)
            except (TypeError, ValueError):
                raise SpecError("%s: lookup key %r is not an integer"
                                % (where, key))
            if not isinstance(val, (int, bool, str)):
                raise SpecError("%s: lookup value %r must be int/bool/str"
                                % (where, val))
            pairs.append((k, val))
        if len({k for k, _ in pairs}) != len(pairs):
            raise SpecError("%s: duplicate lookup keys" % where)
        return Rule(op=OP_LOOKUP, table=tuple(sorted(pairs,
                                                     key=lambda p: p[0])))
    if op == OP_SIGN_EXTEND:
        bits = raw.get("bits")
        if not isinstance(bits, int) or isinstance(bits, bool) or bits < 1:
            raise SpecError("%s: sign_extend needs integer 'bits' >= 1" % where)
        return Rule(op=OP_SIGN_EXTEND, bits=bits)
    if op == OP_SCALE:
        mul = raw.get("mul", 1)
        add = raw.get("add", 0)
        for name, val in (("mul", mul), ("add", add)):
            if not isinstance(val, int) or isinstance(val, bool):
                raise SpecError("%s: scale '%s' must be an integer"
                                % (where, name))
        if mul == 0:
            raise SpecError("%s: scale 'mul' must be non-zero" % where)
        return Rule(op=OP_SCALE, mul=mul, add=add)
    raise SpecError("%s: unknown rule op %r" % (where, op))


def _parse_rules(raw, where):
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SpecError("%s: 'rules' must be a list" % where)
    return tuple(_parse_rule(r, where) for r in raw)


def _parse_elem(raw, where):
    if not isinstance(raw, dict):
        raise SpecError("%s: key element must be an object" % where)
    if "const" in raw:
        val = raw["const"]
        if not isinstance(val, (int, bool, str)):
            raise SpecError("%s: const key element must be int/bool/str"
                            % where)
        return Elem(is_const=True, const=val)
    if "field" in raw:
        if not isinstance(raw["field"], str):
            raise SpecError("%s: 'field' must be a string" % where)
        return Elem(is_const=False, field=raw["field"],
                    rules=_parse_rules(raw.get("rules"), where))
    raise SpecError("%s: key element needs 'const' or 'field'" % where)


def _parse_key(raw, where):
    if not isinstance(raw, list) or not raw:
        raise SpecError("%s: key must be a non-empty array" % where)
    out = []
    for i, item in enumerate(raw):
        sub = "%s key[%d]" % (where, i)
        if isinstance(item, list):
            out.append(_parse_key(item, sub))
        else:
            out.append(_parse_elem(item, sub))
    return tuple(out)


def _parse_value(raw, where):
    if not isinstance(raw, dict):
        raise SpecError("%s: value must be an object" % where)
    vtype = raw.get("type")
    if vtype not in VALUE_TYPES:
        raise SpecError("%s: value 'type' must be one of %s"
                        % (where, VALUE_TYPES))
    if "const" in raw:
        val = raw["const"]
        if not isinstance(val, (int, bool, str)):
            raise SpecError("%s: const value must be int/bool/str" % where)
        return ValueSpec(is_const=True, const=val, type=vtype)
    if "field" in raw:
        return ValueSpec(is_const=False, field=raw["field"],
                         rules=_parse_rules(raw.get("rules"), where),
                         type=vtype)
    raise SpecError("%s: value needs 'const' or 'field'" % where)


def _parse_template(raw, where):
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise SpecError("%s: template needs a 'fields' object" % where)
    fields = {}
    for name, spec in raw["fields"].items():
        fwhere = "%s field %r" % (where, name)
        if not isinstance(spec, dict):
            raise SpecError("%s: field spec must be an object" % fwhere)
        kind = spec.get("kind")
        if kind == KIND_INT:
            lo, hi = spec.get("lo"), spec.get("hi")
            for label, val in (("lo", lo), ("hi", hi)):
                if not isinstance(val, int) or isinstance(val, bool):
                    raise SpecError("%s: '%s' must be an integer"
                                    % (fwhere, label))
            if lo > hi:
                raise SpecError("%s: lo > hi" % fwhere)
            fields[name] = Field(name=name, kind=kind, lo=lo, hi=hi)
        elif kind == KIND_BOOL:
            fields[name] = Field(name=name, kind=kind, lo=0, hi=1)
        elif kind == KIND_TIMESTAMP:
            fields[name] = Field(name=name, kind=kind)
        else:
            raise SpecError("%s: kind must be int/bool/timestamp" % fwhere)
    permitted = []
    for i, combo in enumerate(raw.get("permitted", []) or []):
        cwhere = "%s permitted[%d]" % (where, i)
        cfields = tuple(combo.get("fields", ()))
        if not cfields or any(f not in fields for f in cfields):
            raise SpecError("%s: unknown field in 'fields'" % cwhere)
        for f in cfields:
            if fields[f].kind == KIND_TIMESTAMP:
                raise SpecError("%s: timestamp fields cannot appear in "
                                "permitted combinations" % cwhere)
        allow = set()
        for row in combo.get("allow", ()):
            row = tuple(row)
            if len(row) != len(cfields):
                raise SpecError("%s: allow row arity mismatch" % cwhere)
            norm = []
            for f, v in zip(cfields, row):
                if fields[f].kind == KIND_BOOL:
                    if not isinstance(v, (bool, int)) or v not in (0, 1, True, False):
                        raise SpecError("%s: bad bool value %r" % (cwhere, v))
                    norm.append(int(v))
                else:
                    if not isinstance(v, int) or isinstance(v, bool):
                        raise SpecError("%s: bad int value %r" % (cwhere, v))
                    norm.append(v)
            allow.add(tuple(norm))
        if not allow:
            raise SpecError("%s: 'allow' must be non-empty" % cwhere)
        permitted.append(PermittedCombo(fields=cfields,
                                        allow=frozenset(allow)))
    return Template(fields=fields, permitted=tuple(permitted))


def _check_expr_type(template, is_const, const, field_name, rules, where):
    """Infer the decoded type of an expression; raise SpecError if broken."""
    if is_const:
        if isinstance(const, bool):
            return KIND_BOOL
        if isinstance(const, int):
            return KIND_INT
        return KIND_STR
    if field_name not in template.fields:
        raise SpecError("%s: unknown field %r" % (where, field_name))
    kind = template.fields[field_name].kind
    if kind == KIND_TIMESTAMP:
        if rules:
            raise SpecError("%s: timestamp fields take no conversion rules"
                            % where)
        return KIND_TIMESTAMP
    cur = kind
    for rule in rules:
        if rule.op == OP_SCALE or rule.op == OP_SIGN_EXTEND:
            if cur != KIND_INT:
                raise SpecError("%s: %s requires an int input, got %s"
                                % (where, rule.op, cur))
            cur = KIND_INT
        elif rule.op == OP_LOOKUP:
            if cur not in (KIND_INT, KIND_BOOL):
                raise SpecError("%s: lookup requires an int/bool input"
                                % where)
            out_kinds = set()
            for _, v in rule.table:
                if isinstance(v, bool):
                    out_kinds.add(KIND_BOOL)
                elif isinstance(v, int):
                    out_kinds.add(KIND_INT)
                else:
                    out_kinds.add(KIND_STR)
            if len(out_kinds) != 1:
                raise SpecError("%s: lookup table mixes output types" % where)
            cur = out_kinds.pop()
    return cur


def _walk_key(key):
    for item in key:
        if isinstance(item, tuple):
            for sub in _walk_key(item):
                yield sub
        else:
            yield item


def _parse_table(table_id, raw, template, borrowed, where):
    if not isinstance(raw, dict):
        raise SpecError("%s: table must be an object" % where)
    code = raw.get("ext_code")
    if not isinstance(code, int) or isinstance(code, bool) \
            or not (-2**7 <= code <= 2**7 - 1):
        raise SpecError("%s: ext_code must be an integer in -128..127" % where)
    key = _parse_key(raw.get("key"), "%s (%s)" % (where, table_id))
    for elem in _walk_key(key):
        _check_expr_type(template, elem.is_const, elem.const,
                         elem.field, elem.rules,
                         "%s (%s) key" % (where, table_id))
    value = _parse_value(raw.get("value"), "%s (%s)" % (where, table_id))
    inferred = _check_expr_type(template, value.is_const, value.const,
                                value.field, value.rules,
                                "%s (%s) value" % (where, table_id))
    if inferred != value.type:
        raise SpecError(
            "%s (%s) value: rules decode to type %r but the sheet declares "
            "%r" % (where, table_id, inferred, value.type))
    return Table(table_id=table_id, ext_code=code, key=key, value=value,
                 borrowed=borrowed)


def _parse_segment(name, raw, where):
    if not isinstance(raw, dict):
        raise SpecError("%s: segment must be an object" % where)
    template = _parse_template(raw.get("template"), where)
    tables = {}
    raw_tables = raw.get("tables")
    if not isinstance(raw_tables, dict) or "old" not in raw_tables \
            or "final" not in raw_tables:
        raise SpecError("%s: tables must define 'old' and 'final'" % where)
    for table_id, spec in raw_tables.items():
        if table_id in tables:
            raise SpecError("%s: duplicate table %r" % (where, table_id))
        tables[table_id] = _parse_table(table_id, spec, template, False, where)
    borrowable = []
    raw_borrow = raw.get("borrowable", {}) or {}
    if not isinstance(raw_borrow, dict):
        raise SpecError("%s: 'borrowable' must be an object" % where)
    for table_id, spec in raw_borrow.items():
        if table_id in tables:
            raise SpecError("%s: borrowable table %r clashes with a "
                            "regular table" % (where, table_id))
        tables[table_id] = _parse_table(table_id, spec, template, True, where)
        borrowable.append(table_id)
    codes = [t.ext_code for t in tables.values()]
    if len(set(codes)) != len(codes):
        raise SpecError("%s: ext codes must be unique within a segment"
                        % where)
    sinks = raw.get("sinks")
    if not isinstance(sinks, list) or not sinks \
            or not all(isinstance(s, str) for s in sinks):
        raise SpecError("%s: 'sinks' must be a non-empty list of names"
                        % where)
    return Segment(name=name,
                   writer=raw.get("writer", name),
                   sinks=tuple(sinks),
                   template=template,
                   tables=tables,
                   old="old",
                   final="final",
                   borrowable=tuple(borrowable))


def _parse_reader(raw, where):
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise SpecError("%s: reader needs a 'name'" % where)
    eh = raw.get("ext_handlers", {}) or {}
    initial = frozenset(eh.get("initial", ()) or ())
    may_toggle = frozenset(eh.get("may_toggle", ()) or ())
    registered = frozenset(raw.get("registered", ()) or ())
    for label, codes in (("initial", initial), ("may_toggle", may_toggle),
                         ("registered", registered)):
        for c in codes:
            if not isinstance(c, int) or isinstance(c, bool):
                raise SpecError("%s: %s code %r is not an integer"
                                % (where, label, c))
    if not initial <= may_toggle:
        raise SpecError("%s: initially active handlers must be a subset of "
                        "may_toggle" % where)
    return Reader(name=raw["name"], eh_initial=initial,
                  eh_may_toggle=may_toggle, registered=registered)


def load(raw):
    """Parse and validate a handover sheet (dict or JSON text)."""
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise SpecError("handover sheet must be a JSON object")
    segments = {}
    for name, spec in (raw.get("segments") or {}).items():
        segments[name] = _parse_segment(name, spec, "segment %r" % name)
    if not segments:
        raise SpecError("handover sheet defines no segments")
    readers = tuple(_parse_reader(r, "reader %r" % (r.get("name") if
                                  isinstance(r, dict) else r, ))
                    for r in (raw.get("readers") or ()))
    if not readers:
        raise SpecError("handover sheet defines no readers")
    shared = []
    for group in raw.get("shared", ()) or ():
        group = tuple(group)
        if len(group) < 2:
            raise SpecError("shared group %r needs at least two fields"
                            % (group,))
        for ref in group:
            seg, _, fname = ref.partition(".")
            if seg not in segments:
                raise SpecError("shared field %r: unknown segment" % ref)
            if fname not in segments[seg].template.fields:
                raise SpecError("shared field %r: unknown field" % ref)
            if segments[seg].template.fields[fname].kind == KIND_TIMESTAMP:
                raise SpecError("shared field %r: timestamp fields share "
                                "values implicitly, drop the relation" % ref)
        shared.append(group)
    return Handover(name=raw.get("name", "handover"),
                    segments=segments, readers=readers,
                    shared=tuple(shared))
