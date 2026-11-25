"""Symbolic validation of mixed in-flight batches.

The functions here decide — without expanding record templates into
sample messages — whether a set of in-flight table versions can be
read safely at every reader:

* every in-flight ext code must be decodable at every reader, either
  through a passed ext handler that is (or may be) active, or through
  a registered ext type — matching umsgpack's resolution order
  (``ext_handlers`` first, then registered classes);
* no two in-flight versions may produce the same Python map key when
  their records are merged into one 合单 map.  Keys are nested arrays,
  which umsgpack unpacks into nested tuples, and Python key semantics
  apply: ``True == 1``, ``False == 0``.  A merge that silently drops
  an entry is a fault even when every record decoded fine.

The check is symbolic: field domains may be astronomically large and
are never enumerated (see :mod:`migration.solver`).
"""

from __future__ import annotations

import datetime

from .solver import Solver
from . import spec as sp

# Canonical timestamp used in witnesses when two timestamp fields only
# need to be equal.  Microsecond precision matches umsgpack's unpacking.
WITNESS_EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class Collision:
    """Witness that two in-flight versions can collapse into one key."""

    def __init__(self, left, right, fields_left, fields_right,
                 key_left, key_right):
        self.left = left                    # (segment, table_id, ext_code)
        self.right = right
        self.fields_left = fields_left      # original field choices
        self.fields_right = fields_right
        self.key_left = key_left            # decoded Python key tuples
        self.key_right = key_right

    def describe(self):
        return (
            "%s/%s (ext %d) vs %s/%s (ext %d) can both decode to key %r; "
            "left fields %r, right fields %r" % (
                self.left[0], self.left[1], self.left[2],
                self.right[0], self.right[1], self.right[2],
                self.key_left, self.fields_left, self.fields_right))


class Problem:
    """One reason an in-flight set is not safe at every reader."""

    DECODE = "decode"
    CODE_CONFLICT = "code_conflict"
    COLLISION = "collision"

    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)

    def describe(self):
        if self.kind == self.DECODE:
            return ("reader %s cannot decode ext code %d from %s/%s "
                    "(handler start not permitted)" %
                    (self.reader, self.code, self.segment, self.table_id))
        if self.kind == self.CODE_CONFLICT:
            return ("ext code %d is used by both %s and %s with different "
                    "tables" % (self.code, self.first, self.second))
        if self.kind == self.COLLISION:
            return "merged map at %s may drop an entry: %s" % (
                ", ".join(self.readers), self.collision.describe())
        return repr(self.__dict__)


# ---------------------------------------------------------------------------
# expression compilation
# ---------------------------------------------------------------------------

class _Int:
    """Linear integer term: coeffs over solver vars plus a constant."""

    def __init__(self, coeffs, const=0):
        self.coeffs = coeffs
        self.const = const


class _Str:
    """Finite-choice string expression.

    options: tuple of (value, var, preimage) where restricting ``var``
    to ``preimage`` forces the expression to ``value``.
    """

    def __init__(self, options):
        self.options = tuple(options)


class _Ts:
    """Timestamp atom (a bare timestamp field)."""

    def __init__(self, field):
        self.field = field


def _term_bounds(solver, term):
    lo = term.const
    hi = term.const
    for v, c in term.coeffs.items():
        vlo, vhi = solver.domains[v]
        if c > 0:
            lo += c * vlo
            hi += c * vhi
        else:
            lo += c * vhi
            hi += c * vlo
    return lo, hi


def _materialize(solver, term):
    if term.const == 0 and len(term.coeffs) == 1:
        (v, c), = term.coeffs.items()
        if c == 1:
            return v
    lo, hi = _term_bounds(solver, term)
    aux = solver.fresh_var(lo, hi)
    coeffs = {aux: -1}
    coeffs.update(term.coeffs)
    solver.add_lineq(coeffs, -term.const)
    return aux


def _compile_elem(elem, prefix, template, solver):
    """Compile one key element into a _Int/_Str/_Ts expression."""
    if elem.is_const:
        if isinstance(elem.const, bool):
            return _Int({}, int(elem.const))
        if isinstance(elem.const, int):
            return _Int({}, elem.const)
        return _Str(((elem.const, None, None),))
    field = template.fields[elem.field]
    if field.kind == sp.KIND_TIMESTAMP:
        return _Ts("%s.%s" % (prefix, elem.field))
    var = "%s.%s" % (prefix, elem.field)
    term = _Int({var: 1})
    for rule in elem.rules:
        if rule.op == sp.OP_SCALE:
            term = _Int({v: c * rule.mul for v, c in term.coeffs.items()},
                        term.const * rule.mul + rule.add)
        elif rule.op == sp.OP_SIGN_EXTEND:
            src = _materialize(solver, term)
            m = 1 << (rule.bits - 1)
            out = solver.fresh_var(-m, m - 1)
            solver.add_sign_extend(src, out, rule.bits)
            term = _Int({out: 1})
        elif rule.op == sp.OP_LOOKUP:
            src = _materialize(solver, term)
            table = rule.table
            if isinstance(table[0][1], str):
                options = []
                for value in sorted({v for _, v in table}):
                    pre = tuple(k for k, v in table if v == value)
                    options.append((value, src, pre))
                return _Str(options)
            pairs = tuple((k, int(v)) for k, v in table)
            lo = min(b for _, b in pairs)
            hi = max(b for _, b in pairs)
            out = solver.fresh_var(lo, hi)
            solver.add_lookup(src, out, pairs)
            term = _Int({out: 1})
        else:  # pragma: no cover - validated at load
            raise AssertionError(rule.op)
    return term


def _flatten(key, path=()):
    """Flatten a nested key spec into (path, elem) pairs."""
    out = []
    for i, item in enumerate(key):
        if isinstance(item, tuple):
            out.extend(_flatten(item, path + (i,)))
        else:
            out.append((path + (i,), item))
    return out


def _lineq_of(term_a, term_b):
    coeffs = dict(term_a.coeffs)
    for v, c in term_b.coeffs.items():
        coeffs[v] = coeffs.get(v, 0) - c
    return coeffs, term_b.const - term_a.const


class _TsUnion:
    def __init__(self):
        self.parent = {}
        self.const = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            if rb in self.const:
                if not self.set_const(ra, self.const[rb]):
                    return False
        return True

    def set_const(self, a, value):
        ra = self.find(a)
        if ra in self.const and self.const[ra] != value:
            return False
        self.const[ra] = value
        return True


def _add_field_vars(solver, prefix, template):
    for name, field in template.fields.items():
        if field.kind == sp.KIND_TIMESTAMP:
            continue
        solver.add_var("%s.%s" % (prefix, name), field.lo, field.hi)


def _apply_permitted(solver, prefix, template):
    """Hull-prune domains from permitted combos and add leaf checks."""
    for combo in template.permitted:
        for idx, fname in enumerate(combo.fields):
            vals = [row[idx] for row in combo.allow]
            solver.add_var("%s.%s" % (prefix, fname),
                           min(vals), max(vals))

        fields = combo.fields
        allow = combo.allow

        def check(assignment, fields=fields, allow=allow, prefix=prefix):
            return tuple(assignment["%s.%s" % (prefix, f)]
                         for f in fields) in allow

        solver.add_leaf_check(check)


def _eval_elem(elem, values, template):
    """Evaluate one key element concretely (mirrors the wire handler)."""
    if elem.is_const:
        return elem.const
    field = template.fields[elem.field]
    if field.kind == sp.KIND_TIMESTAMP:
        return values[elem.field]
    value = values[elem.field]
    for rule in elem.rules:
        value = apply_rule(value, rule)
    return value


def apply_rule(value, rule):
    if rule.op == sp.OP_SCALE:
        return value * rule.mul + rule.add
    if rule.op == sp.OP_SIGN_EXTEND:
        m = 1 << (rule.bits - 1)
        return value - (1 << rule.bits) if value >= m else value
    if rule.op == sp.OP_LOOKUP:
        table = dict(rule.table)
        return table[int(value)]
    raise AssertionError(rule.op)  # pragma: no cover


def _eval_key(key, values, template):
    out = []
    for item in key:
        if isinstance(item, tuple):
            out.append(_eval_key(item, values, template))
        else:
            out.append(_eval_elem(item, values, template))
    return tuple(out)


def find_key_collision(handover, seg_a, table_a, seg_b, table_b):
    """Decide whether records of two table versions can share one map key.

    Returns a :class:`Collision` witness (with the original field
    choices that generate the clashing real message) or ``None``.
    Timestamps compare by value; bools and ints compare as Python dict
    keys (``True == 1``).
    """
    ta = handover.table(seg_a, table_a)
    tb = handover.table(seg_b, table_b)
    flat_a = _flatten(ta.key)
    flat_b = _flatten(tb.key)
    if [p for p, _ in flat_a] != [p for p, _ in flat_b]:
        return None

    template_a = handover.segments[seg_a].template
    template_b = handover.segments[seg_b].template
    prefix_a = "L"
    prefix_b = "R"

    solver = Solver()
    _add_field_vars(solver, prefix_a, template_a)
    _add_field_vars(solver, prefix_b, template_b)

    # Fields declared to share the same original value are unified.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for group in handover.shared:
        refs = []
        for ref in group:
            seg, _, fname = ref.partition(".")
            if seg == seg_a:
                refs.append("%s.%s" % (prefix_a, fname))
            if seg == seg_b:
                refs.append("%s.%s" % (prefix_b, fname))
        for other in refs[1:]:
            union(refs[0], other)
    aliased = {}
    for name in list(solver.domains):
        root = find(name)
        if root != name and root in solver.domains:
            aliased.setdefault(root, []).append(name)
    for root, names in aliased.items():
        lo, hi = solver.domains[root]
        for name in names:
            nlo, nhi = solver.domains[name]
            lo, hi = max(lo, nlo), min(hi, nhi)
        if lo > hi:
            return None
        solver.domains[root] = (lo, hi)
        for name in names:
            solver.domains[name] = (lo, hi)
            solver.add_lineq({name: 1, root: -1}, 0)

    _apply_permitted(solver, prefix_a, template_a)
    _apply_permitted(solver, prefix_b, template_b)

    ts_union = _TsUnion()
    str_choices = []   # list of lists of (value, [(var, preimage), ...])

    for (path, elem_a), (_, elem_b) in zip(flat_a, flat_b):
        expr_a = _compile_elem(elem_a, prefix_a, template_a, solver)
        expr_b = _compile_elem(elem_b, prefix_b, template_b, solver)
        if isinstance(expr_a, _Int) and isinstance(expr_b, _Int):
            coeffs, rhs = _lineq_of(expr_a, expr_b)
            solver.add_lineq(coeffs, rhs)
        elif isinstance(expr_a, _Ts) and isinstance(expr_b, _Ts):
            ts_union.union(expr_a.field, expr_b.field)
        elif isinstance(expr_a, _Str) and isinstance(expr_b, _Str):
            common = []
            for value_a, var_a, pre_a in expr_a.options:
                for value_b, var_b, pre_b in expr_b.options:
                    if value_a == value_b:
                        sides = []
                        if var_a is not None:
                            sides.append((var_a, pre_a))
                        if var_b is not None:
                            sides.append((var_b, pre_b))
                        common.append((value_a, tuple(sides)))
            if not common:
                return None
            str_choices.append(common)
        elif isinstance(expr_a, _Str) and isinstance(expr_b, _Str) is False \
                or isinstance(expr_b, _Str) and isinstance(expr_a, _Str) is False:
            return None
        else:
            # timestamp vs non-timestamp can never be equal keys
            return None

    base_lineqs = list(solver.lineqs)
    base_lookups = list(solver.lookups)
    base_signexts = list(solver.signexts)
    base_leaf = list(solver.leaf_checks)
    base_domains = dict(solver.domains)

    combos = [()]  # cartesian product of str choices
    for choice_set in str_choices:
        combos = [prefix + (choice,) for prefix in combos
                  for choice in choice_set]

    for combo in combos:
        trial = Solver()
        trial.domains = dict(base_domains)
        trial.lineqs = list(base_lineqs)
        trial.lookups = list(base_lookups)
        trial.signexts = list(base_signexts)
        trial.leaf_checks = list(base_leaf)
        trial._fresh = solver._fresh
        ok = True
        for _, sides in combo:
            for var, preimage in sides:
                pre = frozenset(preimage)
                aux = trial.fresh_var(0, 0)
                trial.add_lookup(var, aux, tuple((k, 0) for k in pre))

                def check(assignment, var=var, pre=pre):
                    return assignment[var] in pre

                trial.add_leaf_check(check)
        assignment = trial.solve()
        if assignment is None:
            continue
        fields_a = _typed_fields(assignment, prefix_a, template_a,
                                 ts_union, seg_a)
        fields_b = _typed_fields(assignment, prefix_b, template_b,
                                 ts_union, seg_b)
        key_a = _eval_key(ta.key, fields_a, template_a)
        key_b = _eval_key(tb.key, fields_b, template_b)
        if key_a != key_b or hash(key_a) != hash(key_b):
            raise AssertionError("solver produced inconsistent witness")
        return Collision((seg_a, table_a, ta.ext_code),
                         (seg_b, table_b, tb.ext_code),
                         fields_a, fields_b, key_a, key_b)
    return None


def _typed_fields(assignment, prefix, template, ts_union, seg_name):
    values = {}
    for name, field in template.fields.items():
        if field.kind == sp.KIND_TIMESTAMP:
            root = ts_union.find("%s.%s" % (prefix, name))
            values[name] = ts_union.const.get(root, WITNESS_EPOCH)
        elif field.kind == sp.KIND_BOOL:
            values[name] = bool(assignment["%s.%s" % (prefix, name)])
        else:
            values[name] = assignment["%s.%s" % (prefix, name)]
    return values


# ---------------------------------------------------------------------------
# whole-state validation
# ---------------------------------------------------------------------------

def validate_inflight(handover, inflight):
    """Validate one in-flight set.

    ``inflight`` is an iterable of ``(segment, table_id)`` versions that
    may sit simultaneously in the merged map of any reader (queued old
    entries plus freshly written ones).

    Returns a list of :class:`Problem`; empty means the mix is safe.
    """
    problems = []
    versions = []
    by_code = {}
    for seg, table_id in inflight:
        table = handover.table(seg, table_id)
        versions.append((seg, table_id, table))
        owner = "%s/%s" % (seg, table_id)
        if table.ext_code in by_code:
            other_seg, other_id = by_code[table.ext_code]
            other = handover.table(other_seg, other_id)
            if other is not table:
                problems.append(Problem(
                    Problem.CODE_CONFLICT, code=table.ext_code,
                    first="%s/%s" % (other_seg, other_id), second=owner))
        else:
            by_code[table.ext_code] = (seg, table_id)

    for reader in handover.readers:
        for seg, table_id, table in versions:
            if not reader.decodable(table.ext_code):
                problems.append(Problem(
                    Problem.DECODE, reader=reader.name,
                    code=table.ext_code, segment=seg, table_id=table_id))

    reader_names = tuple(r.name for r in handover.readers)
    for i in range(len(versions)):
        for j in range(i + 1, len(versions)):
            seg_a, id_a, _ = versions[i]
            seg_b, id_b, _ = versions[j]
            witness = find_key_collision(handover, seg_a, id_a, seg_b, id_b)
            if witness is not None:
                problems.append(Problem(
                    Problem.COLLISION, collision=witness,
                    readers=reader_names))
    return problems
