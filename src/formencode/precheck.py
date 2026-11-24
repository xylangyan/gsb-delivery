"""
Local pre-submission checking for ambiguous scanned form readings.

A data-entry robot scans a paper form.  Some fragments of the scan are
ambiguous: each fragment offers two or three possible *readings*, and
each reading is a small set of control names and values that would be
submitted together.  Exactly one reading per fragment may be used.  If
several readings supply the same control name, every value is kept, in
the order the fragments appear.

This module answers, without submitting anything: how many *distinct*
complete forms (after :func:`variable_decode` and Schema validation)
can be assembled from the ambiguous fragments?

* If exactly one distinct form validates, that form is produced by
  re-running the normal decode and Schema validation on one witnessing
  combination of readings.
* If several distinct forms validate, the exact count is reported
  together with one field that genuinely differs and two complete
  combinations of fragment readings whose whole forms both validate.
* If none validates, the count is zero.

The count is computed by decomposition, not by enumerating every
combination of readings: fragments that touch disjoint sets of fields
(and are not coupled by whole-form validators such as ``FieldsMatch``)
are independent and their counts multiply.  Inside a repeated block
(``ForEach`` over ``variable_decode``'s sparse numbered rows), rows
couple only through shared fragments, so each row group is solved
separately and the distinct decoded lists are counted with a small
determinised automaton -- this handles missing row numbers (later rows
shift forward), trailing empty rows padded by ``--repetitions`` and the
collapse of two different readings into the same final form, all
without listing the exponentially many forms.
"""

import itertools

from .api import NoDefault, Invalid
from .foreach import ForEach
from .variabledecode import variable_decode, NestedVariables
from .validators import FieldsMatch

__all__ = ['PreCheckResult', 'precheck']

_ABSENT = object()
_INVALID = object()
_ENUMERATION_LIMIT = 100000
_SMALL_ENUMERATION = 256
_MAX_REPETITIONS = 100000


def _freeze(value):
    """Hashable, order-preserving snapshot of a decoded/converted value."""
    if isinstance(value, dict):
        return ('dict', tuple(sorted(
            ((_freeze(key), _freeze(val)) for key, val in value.items()),
            key=repr)))
    if isinstance(value, (list, tuple)):
        return ('list', tuple(_freeze(val) for val in value))
    if isinstance(value, (set, frozenset)):
        return ('set', tuple(sorted((_freeze(val) for val in value),
                                    key=repr)))
    try:
        hash(value)
    except TypeError:
        return ('repr', repr(value))
    return ('atom', value)


class PreCheckResult:
    """Outcome of :func:`precheck`.

    ``count``
        The exact number of distinct complete forms that validate.
    ``form``
        When ``count == 1``, the single valid form (as returned by the
        Schema).  ``None`` otherwise.
    ``field``
        When ``count > 1``, the name of a top-level field whose value
        genuinely differs between valid forms.  ``None`` otherwise.
    ``choices``
        When ``count > 1``, two tuples of reading indices (one index
        per fragment) whose complete forms both validate and differ in
        ``field``.  ``None`` otherwise.
    ``values``
        When ``count > 1``, the two differing values of ``field``
        corresponding to ``choices``.
    """

    def __init__(self, count, form=None, field=None, choices=None,
                 values=None):
        self.count = count
        self.form = form
        self.field = field
        self.choices = choices
        self.values = values

    @property
    def unique(self):
        """True when exactly one distinct form validates."""
        return self.count == 1

    def __repr__(self):
        parts = ['count=%r' % (self.count,)]
        if self.unique:
            parts.append('form=%r' % (self.form,))
        if self.field is not None:
            parts.append('field=%r' % (self.field,))
        return '%s(%s)' % (self.__class__.__name__, ', '.join(parts))

    def __eq__(self, other):
        if not isinstance(other, PreCheckResult):
            return NotImplemented
        return (self.count, self.form, self.field, self.choices,
                self.values) == (other.count, other.form, other.field,
                                 other.choices, other.values)

    def __ne__(self, other):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq


def _normalize_reading(reading):
    """A reading is a mapping or an iterable of ``(name, value)`` pairs."""
    if hasattr(reading, 'items'):
        return tuple((str(name), value) for name, value in reading.items())
    return tuple((str(name), value) for name, value in reading)


def _top_field(name, dict_char, list_char):
    """Top-level decoded field a control name feeds, or ``None`` when the
    control is a no-op (a ``--repetitions`` count whose list never
    exists, which ``variable_decode`` silently ignores)."""
    first = name.split(dict_char, 1)[0]
    if first.endswith('--repetitions'):
        base = first[:-len('--repetitions')]
        if list_char in base:
            _prefix, index = base.split(list_char, 1)
            if index.isdigit():
                return None
        return base
    if list_char in first:
        base, index = first.split(list_char, 1)
        if index.isdigit():
            return base
    return first


def _classify_control(name, field, dict_char, list_char):
    """Classify a control known to feed ``field`` for the repeated-block
    solver: ``('reps', None)`` for the top-level repetition count,
    ``('row', index)`` for a numbered row, ``('other', None)`` for
    anything else."""
    first = name.split(dict_char, 1)[0]
    if first == '%s--repetitions' % field:
        return ('reps', None)
    if list_char in first:
        base, index = first.split(list_char, 1)
        if base == field and index.isdigit():
            return ('row', int(index))
    return ('other', None)


def _merge_pairs(pairs_seq):
    """Merge ordered ``(name, value)`` pairs into a flat dict, keeping
    every value for repeated names in order (like a query string)."""
    flat = {}
    for pairs in pairs_seq:
        for name, value in pairs:
            if name in flat:
                existing = flat[name]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    flat[name] = [existing, value]
            else:
                flat[name] = value
    return flat


class _UnionFind:

    def __init__(self):
        self.parent = {}

    def find(self, item):
        parent = self.parent
        root = item
        while parent.setdefault(root, root) != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class _DictSolution:
    """Solution of one independent component, kept as an explicit mapping
    of frozen partial results to witnessing fragment assignments."""

    def __init__(self, results):
        self.results = results
        self.count = len(results)

    def some_assignment(self):
        return next(iter(self.results.values()))

    def two_assignments(self):
        values = iter(self.results.values())
        return next(values), next(values)


class _ListSolution:
    """Solution of a single repeated-block field, counted with an
    automaton instead of enumerating the decoded lists."""

    def __init__(self, count, examples):
        self.count = count
        self._examples = examples

    def some_assignment(self):
        return self._examples[0]

    def two_assignments(self):
        return self._examples[0], self._examples[1]


class _Solver:

    def __init__(self, schema, fragments, dict_char, list_char):
        self.schema = schema
        self.dict_char = dict_char
        self.list_char = list_char
        # fragments: list of fragments; each a list of normalized readings
        self.fragments = fragments
        pre = list(getattr(schema, 'pre_validators', ()) or ())
        chained = list(getattr(schema, 'chained_validators', ()) or ())
        self.schema_decodes = any(isinstance(v, NestedVariables)
                                  for v in pre)
        self.decomposable = (
            all(isinstance(v, NestedVariables) for v in pre)
            and all(isinstance(v, FieldsMatch) for v in chained))
        self.chained = chained

    # -- decoding / validation helpers --------------------------------

    def decode(self, flat):
        return variable_decode(flat, dict_char=self.dict_char,
                               list_char=self.list_char)

    def run_real(self, flat):
        """The authoritative full-form read: decode, then the Schema."""
        if self.schema_decodes:
            return self.schema.to_python(flat)
        return self.schema.to_python(self.decode(flat))

    def validate_partial(self, decoded, field_names, chained):
        """Replicates ``Schema._convert_to_python`` restricted to the
        given fields; returns the converted partial dict, or ``None``
        when any of those fields (or the component's whole-form
        validators) fails."""
        schema = self.schema
        new = {}
        fields = schema.fields
        for name in field_names:
            validator = fields.get(name)
            if validator is None:
                if name not in decoded:
                    continue
                if not schema.allow_extra_fields:
                    return None
                if not schema.filter_extra_fields:
                    new[name] = decoded[name]
                continue
            if name in decoded:
                value = decoded[name]
                if (schema._value_is_iterator(value)
                        and not getattr(validator, 'accept_iterator', False)):
                    return None
                try:
                    new[name] = validator.to_python(value)
                except Invalid:
                    return None
            else:
                if_missing = getattr(validator, 'if_missing', NoDefault)
                if if_missing is not NoDefault:
                    new[name] = if_missing
                elif schema.ignore_key_missing:
                    continue
                elif schema.if_key_missing is not NoDefault:
                    try:
                        new[name] = validator.to_python(
                            schema.if_key_missing)
                    except Invalid:
                        return None
                else:
                    return None
        for validator in chained:
            if (getattr(validator, 'validate_partial_form', False)
                    and hasattr(validator, 'validate_partial')):
                try:
                    validator.validate_partial(decoded, None)
                except Invalid:
                    return None
            try:
                new = validator.to_python(new)
            except Invalid:
                return None
        return new

    def missing_field_ok(self, validator):
        """Whether a field no fragment can fill gets a usable default."""
        schema = self.schema
        if getattr(validator, 'if_missing', NoDefault) is not NoDefault:
            return True
        if schema.ignore_key_missing:
            return True
        if schema.if_key_missing is not NoDefault:
            try:
                validator.to_python(schema.if_key_missing)
            except Invalid:
                return False
            return True
        return False

    # -- component structure ------------------------------------------

    def components(self):
        """Split touched fields into independent components.

        Returns ``(components, trivial_fields)`` where each component is
        ``(field_names, fragment_indices, chained_validators)`` and
        trivial fields are schema fields no fragment and no whole-form
        validator can influence.
        """
        schema = self.schema
        uf = _UnionFind()
        fragment_fields = []
        for readings in self.fragments:
            touched = set()
            for pairs in readings:
                for name, _value in pairs:
                    field = _top_field(name, self.dict_char, self.list_char)
                    if field is not None:
                        touched.add(field)
            fragment_fields.append(touched)
            for field in touched:
                uf.find(field)
            for other in list(touched)[1:]:
                uf.union(next(iter(touched)), other)
        chained_fields = []
        for validator in self.chained:
            names = list(validator.field_names)
            chained_fields.append((validator, names))
            for name in names:
                uf.find(name)
            for other in names[1:]:
                uf.union(names[0], other)
        comps = {}
        for index, touched in enumerate(fragment_fields):
            if not touched:
                continue
            root = uf.find(next(iter(touched)))
            comp = comps.setdefault(root, [set(), [], []])
            comp[0].update(touched)
            comp[1].append(index)
        for validator, names in chained_fields:
            if not names:
                continue
            root = uf.find(names[0])
            comp = comps.setdefault(root, [set(), [], []])
            comp[0].update(names)
            comp[2].append(validator)
        touched_fields = set()
        for field_names, _frags, _ch in comps.values():
            touched_fields.update(field_names)
        trivial = [name for name in schema.fields
                   if name not in touched_fields]
        components = [(sorted(fields), frags, ch)
                      for fields, frags, ch in comps.values()]
        components.sort(key=lambda comp: comp[0])
        return components, trivial

    # -- solving -------------------------------------------------------

    def solve(self):
        """Returns ``(count, solutions)``."""
        if not self.decomposable:
            fields = list(self.schema.fields)
            extra = sorted({field
                            for readings in self.fragments
                            for pairs in readings
                            for name, _value in pairs
                            for field in [_top_field(name, self.dict_char,
                                                     self.list_char)]
                            if field is not None and field not in fields})
            space = 1
            for readings in self.fragments:
                space *= len(readings)
            if space > _ENUMERATION_LIMIT:
                raise ValueError(
                    'schema-level validators couple all fields into %d '
                    'reading combinations; cannot pre-check by '
                    'enumeration' % (space,))
            results = self._enumerate(fields + extra,
                                      range(len(self.fragments)),
                                      self.chained, global_mode=True)
            solution = _DictSolution(results)
            return solution.count, [solution]
        components, trivial = self.components()
        for name in trivial:
            if not self.missing_field_ok(self.schema.fields[name]):
                return 0, []
        solutions = []
        count = 1
        for field_names, frag_indices, chained in components:
            solution = self.solve_component(field_names, frag_indices,
                                            chained)
            if solution.count == 0:
                return 0, []
            count *= solution.count
            solutions.append(solution)
        return count, solutions

    def solve_component(self, field_names, frag_indices, chained):
        frag_indices = list(frag_indices)
        space = 1
        for index in frag_indices:
            space *= len(self.fragments[index])
        if len(field_names) == 1 and not chained:
            field = field_names[0]
            validator = self.schema.fields.get(field)
            if validator is not None:
                kind = self.control_kind(field, frag_indices)
                if kind == 'scalar' and not getattr(
                        validator, 'accept_iterator', False):
                    return self.solve_scalar(field, frag_indices, validator)
                if (kind == 'indexed' and type(validator) is ForEach
                        and space > _SMALL_ENUMERATION):
                    return self.solve_repeated(field, frag_indices,
                                               validator)
        if space > _ENUMERATION_LIMIT:
            raise ValueError(
                'component over fields %r has %d reading combinations; '
                'cannot pre-check by enumeration'
                % (field_names, space))
        results = self._enumerate(field_names, frag_indices, chained)
        return _DictSolution(results)

    def control_kind(self, field, frag_indices):
        """'scalar' when every control is the bare field name, 'indexed'
        when every control is a numbered row or the repetition count,
        'other' otherwise."""
        kind = None
        for index in frag_indices:
            for pairs in self.fragments[index]:
                for name, _value in pairs:
                    if _top_field(name, self.dict_char,
                                  self.list_char) != field:
                        continue
                    cls, _idx = _classify_control(name, field,
                                                  self.dict_char,
                                                  self.list_char)
                    if cls == 'other':
                        if name == field:
                            this = 'scalar'
                        else:
                            return 'other'
                    else:
                        this = 'indexed'
                    if kind is None:
                        kind = this
                    elif kind != this:
                        return 'other'
        return kind or 'scalar'

    # -- generic enumeration ------------------------------------------

    def _enumerate(self, field_names, frag_indices, chained,
                   global_mode=False):
        frag_indices = list(frag_indices)
        results = {}
        spaces = [range(len(self.fragments[index]))
                  for index in frag_indices]
        for combo in itertools.product(*spaces):
            flat = _merge_pairs(self.fragments[index][reading]
                                for index, reading
                                in zip(frag_indices, combo))
            try:
                decoded = self.decode(flat)
            except Exception:
                continue
            if global_mode:
                try:
                    full = self.run_real(flat)
                except Invalid:
                    continue
                partial = full
            else:
                partial = self.validate_partial(decoded, field_names,
                                                chained)
                if partial is None:
                    continue
            key = _freeze(partial)
            if key not in results:
                results[key] = dict(zip(frag_indices, combo))
        return results

    # -- single scalar field ------------------------------------------

    def solve_scalar(self, field, frag_indices, validator):
        empty_reading = {}
        singles = {}
        for index in frag_indices:
            for reading_no, pairs in enumerate(self.fragments[index]):
                controls = [(name, value) for name, value in pairs
                            if _top_field(name, self.dict_char,
                                          self.list_char) == field]
                if not controls:
                    empty_reading.setdefault(index, reading_no)
                elif len(controls) == 1:
                    singles.setdefault(index, []).append(
                        (reading_no, controls[0][1]))
                # readings with several controls for the field always
                # produce a multi-value list, which the validator
                # rejects (accept_iterator is false): never valid
        results = {}

        def record(decoded, assignment):
            partial = self.validate_partial(decoded, [field], [])
            if partial is None:
                return
            key = _freeze(partial)
            if key not in results:
                results[key] = assignment

        if len(empty_reading) == len(frag_indices):
            record({}, {index: reading
                        for index, reading in empty_reading.items()})
        for index in frag_indices:
            if any(other not in empty_reading
                   for other in frag_indices if other != index):
                continue
            for reading_no, value in singles.get(index, []):
                assignment = {other: empty_reading[other]
                              for other in frag_indices if other != index}
                assignment[index] = reading_no
                record({field: value}, assignment)
        return _DictSolution(results)

    # -- single repeated (ForEach) field -------------------------------

    def convert_item(self, foreach, item):
        """Convert one row the way ``ForEach`` would."""
        try:
            for validator in foreach.validators:
                item = validator.to_python(item)
        except Invalid:
            return _INVALID
        return item

    def solve_repeated(self, field, frag_indices, foreach):
        # group row indices (and the repetition count) by the fragments
        # that couple them
        uf = _UnionFind()
        for index in frag_indices:
            indices = set()
            for pairs in self.fragments[index]:
                for name, _value in pairs:
                    if _top_field(name, self.dict_char,
                                  self.list_char) != field:
                        continue
                    cls, row = _classify_control(name, field,
                                                 self.dict_char,
                                                 self.list_char)
                    if cls == 'other':
                        continue
                    indices.add(row if cls == 'row' else 'reps')
            if not indices:
                continue
            for other in list(indices)[1:]:
                uf.union(next(iter(indices)), other)
        groups = {}
        for index in frag_indices:
            for pairs in self.fragments[index]:
                for name, _value in pairs:
                    if _top_field(name, self.dict_char,
                                  self.list_char) != field:
                        continue
                    cls, row = _classify_control(name, field,
                                                 self.dict_char,
                                                 self.list_char)
                    if cls == 'other':
                        continue
                    key = row if cls == 'row' else 'reps'
                    root = uf.find(key)
                    group = groups.setdefault(root, {'indices': set(),
                                                     'fragments': set()})
                    group['indices'].add(key)
                    group['fragments'].add(index)
        # merge groups whose index intervals interleave, so that every
        # super-group contributes one contiguous decoded segment

        def group_lo(group):
            rows = [i for i in group['indices'] if i != 'reps']
            return min(rows) if rows else (1 << 60)

        ordered = sorted(groups.values(), key=group_lo)
        super_groups = []
        for group in ordered:
            rows = sorted(i for i in group['indices'] if i != 'reps')
            lo = rows[0] if rows else (1 << 60)
            hi = rows[-1] if rows else (1 << 60)
            if (super_groups and lo < (1 << 60)
                    and super_groups[-1]['hi'] >= lo):
                prev = super_groups[-1]
                prev['indices'] |= group['indices']
                prev['fragments'] |= group['fragments']
                prev['hi'] = max(prev['hi'], hi)
            else:
                super_groups.append({'indices': set(group['indices']),
                                     'fragments': set(group['fragments']),
                                     'hi': hi})
        # enumerate each super-group's reading combinations into
        # distinct (decoded segment, repetitions) results
        group_results = []
        for group in super_groups:
            frags = sorted(group['fragments'])
            space = 1
            for index in frags:
                space *= len(self.fragments[index])
            if space > _ENUMERATION_LIMIT:
                raise ValueError(
                    'row group %r of field %r has %d reading '
                    'combinations; cannot pre-check by enumeration'
                    % (sorted(i for i in group['indices'] if i != 'reps'),
                       field, space))
            results = {}
            for combo in itertools.product(
                    *[range(len(self.fragments[index])) for index in frags]):
                row_pairs = []
                reps_values = []
                for index, reading_no in zip(frags, combo):
                    for name, value in self.fragments[index][reading_no]:
                        if _top_field(name, self.dict_char,
                                      self.list_char) != field:
                            continue
                        cls, _row = _classify_control(
                            name, field, self.dict_char, self.list_char)
                        if cls == 'reps':
                            reps_values.append(value)
                        elif cls == 'row':
                            row_pairs.append((name, value))
                if len(reps_values) > 1:
                    continue  # variable_decode would crash on a list
                reps = None
                if reps_values:
                    try:
                        reps = int(reps_values[0])
                    except (TypeError, ValueError):
                        continue  # variable_decode would crash
                    if reps > _MAX_REPETITIONS:
                        continue
                segment = self.decode_segment(field, row_pairs, foreach)
                if segment is None:
                    continue
                key = (segment, reps)
                if key not in results:
                    results[key] = dict(zip(frags, combo))
            group_results.append([(segment, reps, witness)
                                  for (segment, reps), witness
                                  in results.items()])
        # the value a padded empty row converts to
        pad = self.convert_item(foreach, '')
        pad_valid = pad is not _INVALID
        pad_letter = _freeze(pad) if pad_valid else None
        count, machine = self._count_distinct(group_results, pad_letter,
                                              pad_valid)
        # the all-empty combination leaves the field missing entirely
        # (variable_decode never materialises the list), which yields
        # the validator's if_missing value
        missing_possible = bool(group_results) and all(
            any(segment == () for segment, _reps, _w in results)
            for results in group_results)
        examples = []
        if missing_possible:
            if_missing = foreach.if_missing
            frozen_missing = _freeze(if_missing)
            collides = (isinstance(frozen_missing, tuple)
                        and len(frozen_missing) == 2
                        and frozen_missing[0] == 'list'
                        and self._accepts(machine, frozen_missing[1]))
            if not collides:
                count += 1
            missing_assignment = {}
            for results in group_results:
                for segment, _reps, witness in results:
                    if segment == ():
                        missing_assignment.update(witness)
                        break
            examples.append(missing_assignment)
        strings = self._extract_strings(machine, 2)
        assignments = [self._assignment_for_string(machine, group_results,
                                                   string)
                       for string in strings]
        examples = assignments + examples
        return _ListSolution(count, examples)

    def decode_segment(self, field, row_pairs, foreach):
        """Decode and validate the rows one super-group produces;
        returns the frozen converted segment tuple, or ``None`` when
        the combination is invalid."""
        if not row_pairs:
            return ()
        try:
            decoded = self.decode(_merge_pairs([row_pairs]))
        except Exception:
            return None
        rows = decoded.get(field, ())
        if not isinstance(rows, list):
            return None
        segment = []
        for row in rows:
            converted = self.convert_item(foreach, row)
            if converted is _INVALID:
                return None
            segment.append(_freeze(converted))
        return tuple(segment)

    # -- distinct-string counting over the row groups ------------------

    def _build_nfa(self, group_results, pad_letter, pad_valid):
        """NFA whose accepted strings are exactly the distinct valid
        decoded lists (as tuples of frozen converted rows).  Edges are
        tagged with ``(group, result)`` so a witness combination of
        readings can be recovered from any accepted string."""
        transitions = {}
        eps = {}
        accept = set()
        next_state = itertools.count()

        def add_edge(source, letter, target, tag):
            transitions.setdefault(source, {}).setdefault(
                letter, []).append((target, tag))

        start = next(next_state)
        frontier = {(0, None): start}
        for group_no, results in enumerate(group_results):
            new_frontier = {}

            def target_for(present, reps):
                key = (present, reps)
                if key not in new_frontier:
                    new_frontier[key] = next(next_state)
                return new_frontier[key]

            for (present, reps), source in frontier.items():
                for result_no, (segment, reps2, _w) in enumerate(results):
                    if reps is not None and reps2 is not None \
                            and reps2 != reps:
                        continue
                    new_reps = reps2 if reps2 is not None else reps
                    tag = (group_no, result_no)
                    if not segment:
                        eps.setdefault(source, []).append(
                            (target_for(present, new_reps), tag))
                        continue
                    current = source
                    for letter in segment[:-1]:
                        middle = next(next_state)
                        add_edge(current, letter, middle, None)
                        current = middle
                    add_edge(current, segment[-1],
                             target_for(present + len(segment), new_reps),
                             tag)
            frontier = new_frontier
        for (present, reps), source in frontier.items():
            if present == 0:
                # no numbered control at all: the field is missing,
                # repetitions or not -- handled outside the automaton
                continue
            padding = 0 if reps is None else max(0, reps - present)
            if padding == 0:
                accept.add(source)
            elif pad_valid:
                current = source
                for _ in range(padding - 1):
                    middle = next(next_state)
                    add_edge(current, pad_letter, middle, None)
                    current = middle
                final = next(next_state)
                add_edge(current, pad_letter, final, None)
                accept.add(final)
        return transitions, eps, start, accept

    @staticmethod
    def _closure(eps, states):
        result = set(states)
        stack = list(states)
        while stack:
            state = stack.pop()
            for target, _tag in eps.get(state, ()):
                if target not in result:
                    result.add(target)
                    stack.append(target)
        return result

    def _count_distinct(self, group_results, pad_letter, pad_valid):
        transitions, eps, start, accept = self._build_nfa(
            group_results, pad_letter, pad_valid)
        # subset construction: in the resulting DFA each distinct
        # string is exactly one path, so counting paths counts the
        # distinct decoded lists
        dfa_trans = {}
        dfa_accept = set()
        start_set = frozenset(self._closure(eps, {start}))
        stack = [start_set]
        while stack:
            state = stack.pop()
            if state in dfa_trans:
                continue
            merged = {}
            for nfa_state in state:
                for letter, edges in transitions.get(nfa_state, {}).items():
                    bucket = merged.setdefault(letter, set())
                    for target, _tag in edges:
                        bucket.add(target)
            dfa_trans[state] = {
                letter: frozenset(self._closure(eps, targets))
                for letter, targets in merged.items()}
            if state & accept:
                dfa_accept.add(state)
            stack.extend(dfa_trans[state].values())
        # count distinct strings (paths to an accepting DFA state)
        counts = {}
        walk = [(start_set, iter(dfa_trans.get(start_set, {}).values()))]
        counts[start_set] = None
        while walk:
            state, iterator = walk[-1]
            descended = False
            for target in iterator:
                if counts.get(target) is None and target not in counts:
                    counts[target] = None
                    walk.append((target, iter(
                        dfa_trans.get(target, {}).values())))
                    descended = True
                    break
            if descended:
                continue
            total = 1 if state in dfa_accept else 0
            for target in dfa_trans.get(state, {}).values():
                total += counts[target]
            counts[state] = total
            walk.pop()
        machine = (dfa_trans, start_set, dfa_accept, counts,
                   transitions, eps, start, accept)
        return counts[start_set], machine

    def _accepts(self, machine, word):
        dfa_trans, start_set, dfa_accept = machine[0], machine[1], machine[2]
        state = start_set
        for letter in word:
            targets = dfa_trans.get(state, {})
            if letter not in targets:
                return False
            state = targets[letter]
        return state in dfa_accept

    def _extract_strings(self, machine, limit):
        """Up to ``limit`` distinct accepted strings, via the per-state
        string counts computed during determinisation."""
        dfa_trans, start_set, dfa_accept, counts = machine[:4]
        if not counts.get(start_set):
            return []
        strings = []

        def greedy(state):
            word = []
            while state not in dfa_accept:
                for letter, target in dfa_trans.get(state, {}).items():
                    if counts.get(target):
                        word.append(letter)
                        state = target
                        break
            return word

        first = greedy(start_set)
        strings.append(tuple(first))
        if limit > 1 and counts[start_set] > 1:
            # walk the first string's path and take the first possible
            # deviation that still leads to an accepting state
            state = start_set
            for depth, letter in enumerate(first):
                if state in dfa_accept:
                    strings.append(tuple(first[:depth]))
                    break
                for alt, target in dfa_trans.get(state, {}).items():
                    if alt != letter and counts.get(target):
                        strings.append(tuple(first[:depth]) + (alt,)
                                       + tuple(greedy(target)))
                        break
                if len(strings) > 1:
                    break
                state = dfa_trans[state][letter]
        return strings

    def _assignment_for_string(self, machine, group_results, string):
        """Recover one witnessing combination of readings for an
        accepted string, following the tags on the NFA edges."""
        _dfa_trans, _start_set, _dfa_accept, _counts, transitions, eps, \
            start, accept = machine

        def closure(states):
            result = dict(states)
            stack = list(states.items())
            while stack:
                state, tags = stack.pop()
                for target, tag in eps.get(state, ()):
                    new_tags = tags if tag is None else tags + (tag,)
                    if target not in result:
                        result[target] = new_tags
                        stack.append((target, new_tags))
            return result

        states = closure({start: ()})
        for letter in string:
            following = {}
            for state, tags in states.items():
                for target, tag in transitions.get(state, {}).get(
                        letter, ()):
                    if target in following:
                        continue
                    following[target] = tags if tag is None \
                        else tags + (tag,)
            states = closure(following)
        for state, tags in states.items():
            if state in accept:
                assignment = {}
                for group_no, result_no in tags:
                    assignment.update(
                        group_results[group_no][result_no][2])
                return assignment
        return {}


def _normalize_fragments(fragments):
    normalized = []
    for fragment in fragments:
        if hasattr(fragment, 'items'):
            # a bare mapping is a single-reading fragment
            fragment = [fragment]
        readings = [_normalize_reading(reading) for reading in fragment]
        if not readings:
            readings = [()]
        normalized.append(readings)
    return normalized


def precheck(schema, fragments, dict_char='.', list_char='-'):
    """Pre-check ambiguous scanned readings against a FormEncode schema.

    ``fragments`` is a sequence of fragments; each fragment is a
    sequence of alternative readings; each reading is a mapping or an
    iterable of ``(control_name, value)`` pairs.  Every fragment
    contributes exactly one of its readings; repeated control names
    keep every value in fragment order.

    Returns a :class:`PreCheckResult` with the exact number of distinct
    complete forms that pass ``variable_decode`` and the schema.  When
    the count is one, ``result.form`` holds that form, re-read with the
    normal decode and Schema validation.  When it is larger,
    ``result.field`` names a genuinely divergent field and
    ``result.choices`` gives two complete per-fragment reading
    selections whose whole forms both validate.
    """
    fragments = _normalize_fragments(fragments)
    solver = _Solver(schema, fragments, dict_char, list_char)
    count, solutions = solver.solve()
    if count == 0:
        return PreCheckResult(0)
    base = {}
    for solution in solutions:
        base.update(solution.some_assignment())
    full = [base.get(index, 0) for index in range(len(fragments))]
    if count == 1:
        form = solver.run_real(_merge_pairs(
            fragments[index][reading]
            for index, reading in enumerate(full)))
        return PreCheckResult(1, form=form)
    divergent = None
    for solution in solutions:
        if solution.count > 1:
            divergent = solution
            break
    first, second = divergent.two_assignments()
    choices = []
    forms = []
    for override in (first, second):
        merged = dict(base)
        merged.update(override)
        assignment = [merged.get(index, 0)
                      for index in range(len(fragments))]
        choices.append(tuple(assignment))
        forms.append(solver.run_real(_merge_pairs(
            fragments[index][reading]
            for index, reading in enumerate(assignment))))
    form_a, form_b = forms
    field = next(name for name in sorted(set(form_a) | set(form_b))
                 if form_a.get(name, _ABSENT) != form_b.get(name, _ABSENT))
    return PreCheckResult(count, field=field,
                          choices=(choices[0], choices[1]),
                          values=(form_a.get(field), form_b.get(field)))
