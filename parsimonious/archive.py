"""Random-access PEG record queries over lazily expanded byte volumes.

An archive *volume* is a virtual byte string described by an index DAG:

* :class:`Leaf` -- a byte range of a named source file,
* :class:`Concat` -- a splice of several child fragments,
* :class:`Repeat` -- one child fragment referenced ``count`` times
  (repeats may nest, and the count is not materialized).

A :class:`Volume` is never expanded.  An :class:`Archive` matches a
parsimonious byte grammar against the volume from its head, one record at
a time, and answers "give me record number k" directly, without
materializing repetitions or storing one result per repetition.

Constraints honoured by the engine:

* Each record-recognition attempt (including failed ordered branches and
  lookaheads) only inspects at most ``WINDOW`` (64) bytes past its start.
* Every successful record consumes at least one byte.
* Named rule references are acyclic.
* At most ~1e6 record-match attempts are performed in total (repeat
  regions are crossed by cycle detection and arithmetic skipping, and
  ``Archive.attempts`` exposes the actual count).

Correction volumes are cheap: :meth:`Volume.correct` replaces a single
leaf on a given reference path and shares every unchanged node, so source
data already fetched for the original volume is reused.
"""

from bisect import bisect_right
from collections import OrderedDict, defaultdict, namedtuple

from parsimonious.exceptions import ParseError
from parsimonious.expressions import (Literal, Lookahead, OneOf, Quantifier,
                                      Regex, Sequence, TokenMatcher)
from parsimonious.expressions import re as _re
from parsimonious.grammar import Grammar

#: How many bytes a single record-recognition attempt may inspect,
#: including failed branches and lookaheads.
WINDOW = 64

#: Repeat-remaining counts are capped at this in canonical positions.
#: Any cap >= WINDOW keeps the next WINDOW bytes of a position fully
#: determined by its canonical form.
CAP = 2 * WINDOW


# ---------------------------------------------------------------------------
# Sources


def _read_file(path):
    with open(path, 'rb') as file:
        return file.read()


class SourceStore:
    """Named byte sources backing the leaves of archive volumes.

    Source data is fetched lazily and cached, so volumes derived by
    correction reuse the bytes of sources they did not change.
    """

    def __init__(self):
        self._providers = {}
        self._cache = {}

    def add(self, name, data):
        """Register ``data`` (bytes) under ``name``."""
        data = bytes(data)
        self._providers[name] = lambda: data
        self._cache.pop(name, None)

    def add_file(self, name, path):
        """Register the contents of the file at ``path`` under ``name``."""
        self._providers[name] = lambda: _read_file(path)
        self._cache.pop(name, None)

    def _fetch(self, name):
        return self._providers[name]()

    def get(self, name):
        """Return the bytes of source ``name``, reading them at most once."""
        try:
            return self._cache[name]
        except KeyError:
            data = self._fetch(name)
            self._cache[name] = data
            return data


# ---------------------------------------------------------------------------
# Index DAG


class Leaf:
    """A byte range of one source (a leaf of the archive index)."""

    __slots__ = ('source', 'start', 'length', 'name')

    def __init__(self, source, start=0, length=None, name=None):
        self.source = source
        self.start = start
        self.length = length
        self.name = name

    def __repr__(self):
        return 'Leaf(%r, %r, %r)' % (self.source, self.start, self.length)


class Concat:
    """A splice of several child fragments, end to end."""

    __slots__ = ('children', 'name')

    def __init__(self, children, name=None):
        self.children = tuple(children)
        self.name = name

    def __repr__(self):
        return 'Concat(%r)' % (self.children,)


class Repeat:
    """One child fragment referenced ``count`` times in a row."""

    __slots__ = ('child', 'count', 'name')

    def __init__(self, child, count, name=None):
        if count < 0:
            raise ValueError('repeat count must be >= 0')
        self.child = child
        self.count = count
        self.name = name

    def __repr__(self):
        return 'Repeat(%r, %r)' % (self.child, self.count)


def _replace(node, path, new_leaf):
    if not path:
        return new_leaf
    index, rest = path[0], path[1:]
    if isinstance(node, Concat):
        children = list(node.children)
        children[index] = _replace(children[index], rest, new_leaf)
        return Concat(children, name=node.name)
    if isinstance(node, Repeat):
        if index != 0:
            raise IndexError('a Repeat node has a single child (index 0)')
        return Repeat(_replace(node.child, rest, new_leaf), node.count,
                      name=node.name)
    raise TypeError('correction path continues past a leaf')


# ---------------------------------------------------------------------------
# Provenance


RefFrame = namedtuple('RefFrame', ['node', 'iteration'])
RefFrame.__doc__ = ('One frame of a reference path: which Repeat node was '
                    'entered, and which repetition (0-based) of it.')

Segment = namedtuple('Segment', ['source', 'start', 'end', 'refs'])
Segment.__doc__ = ('One consumed source fragment: byte range '
                   '[start:end) of ``source``, reached through ``refs``.')

Decision = namedtuple('Decision', ['expression', 'index'])
Decision.__doc__ = ('One ordered-choice decision: the OneOf expression and '
                    'the index of the alternative that matched.')

Tail = namedtuple('Tail', ['offset', 'total', 'exhausted'])
Tail.__doc__ = ('End state of a volume scan: first unconsumed byte offset, '
                'total record count, and whether the volume was used up.')

Field = namedtuple('Field', ['name', 'start', 'end', 'segments'])
Field.__doc__ = ('A named field of a record: expanded byte range plus its '
                 'source segments in consumption order.')


class Volume:
    """A lazily expanded byte string described by an index DAG."""

    def __init__(self, root, sources):
        self.root = root
        self.sources = sources
        self._lens = {}
        self.length = self._len(root)

    # -- structure ---------------------------------------------------------

    def _len(self, node):
        try:
            return self._lens[node]
        except KeyError:
            if isinstance(node, Leaf):
                data = self.sources.get(node.source)
                available = len(data) - node.start
                if available < 0:
                    raise ValueError('leaf start past end of source %r'
                                     % (node.source,))
                length = node.length if node.length is not None else available
                if length > available:
                    raise ValueError('leaf range past end of source %r'
                                     % (node.source,))
            elif isinstance(node, Concat):
                length = sum(self._len(child) for child in node.children)
            elif isinstance(node, Repeat):
                length = node.count * self._len(node.child)
            else:
                raise TypeError('unknown index node: %r' % (node,))
            self._lens[node] = length
            return length

    def correct(self, path, new_leaf):
        """Return a new volume with the leaf at ``path`` replaced.

        ``path`` is a tuple of child indices from the root down to the
        leaf being corrected (a Repeat contributes a single child, index
        0).  All unchanged nodes -- and their already-fetched source
        bytes -- are shared with the original volume.
        """
        return Volume(_replace(self.root, tuple(path), new_leaf), self.sources)

    # -- random access ------------------------------------------------------

    def read(self, offset, n):
        """Return up to ``n`` expanded bytes starting at ``offset``."""
        if offset < 0:
            raise ValueError('negative offset')
        n = max(0, min(n, self.length - offset))
        out = bytearray()
        if n:
            self._read_into(self.root, 0, offset, offset + n, out)
        return bytes(out)

    def _read_into(self, node, start, lo, hi, out):
        if isinstance(node, Leaf):
            data = self.sources.get(node.source)
            out += data[node.start + lo - start: node.start + hi - start]
        elif isinstance(node, Concat):
            child_start = start
            for child in node.children:
                child_end = child_start + self._len(child)
                if lo < child_end and hi > child_start:
                    self._read_into(child, child_start,
                                    max(lo, child_start),
                                    min(hi, child_end), out)
                child_start = child_end
                if child_start >= hi:
                    break
        else:  # Repeat
            body = self._len(node.child)
            first = (lo - start) // body
            last = (hi - 1 - start) // body
            for iteration in range(first, last + 1):
                it_start = start + iteration * body
                self._read_into(node.child, it_start,
                                max(lo, it_start),
                                min(hi, it_start + body), out)

    def expand(self):
        """Return the whole expanded volume as bytes (small volumes only)."""
        return self.read(0, self.length)

    def _frames(self, offset):
        """Return the zipper of (node, payload) frames from root to offset.

        Payloads: leaf offset for Leaf, child index for Concat, remaining
        iteration count for Repeat.
        """
        frames = []
        node, start = self.root, 0
        while True:
            if isinstance(node, Leaf):
                frames.append((node, offset - start))
                return frames
            if isinstance(node, Concat):
                child_start = start
                for index, child in enumerate(node.children):
                    child_end = child_start + self._len(child)
                    if offset < child_end:
                        frames.append((node, index))
                        node, start = child, child_start
                        break
                    child_start = child_end
                else:
                    raise IndexError(offset)
            else:  # Repeat
                body = self._len(node.child)
                iteration = (offset - start) // body
                frames.append((node, node.count - iteration))
                start += iteration * body
                node = node.child

    def canonical(self, offset):
        """Return a hashable canonical form of a position.

        Two positions with the same canonical form have the same next
        ``WINDOW`` bytes (and the same EOF behaviour within that window),
        because repeat-remaining counts are capped at ``CAP >= WINDOW``.
        """
        return tuple((node, min(payload, CAP) if isinstance(node, Repeat)
                      else payload)
                     for node, payload in self._frames(offset))

    def repeat_frames(self, offset):
        """Return [(Repeat node, remaining iterations)] along the zipper."""
        return [(node, payload) for node, payload in self._frames(offset)
                if isinstance(node, Repeat)]

    def segments_for_range(self, start, end):
        """Map an expanded byte range to its source segments, in order."""
        out = []
        if end > start:
            self._segments(self.root, 0, start, end, (), out)
        merged = []
        for leaf, source, seg_start, seg_end, refs in out:
            if (merged and merged[-1][0] is leaf
                    and merged[-1][2] == seg_start and merged[-1][4] == refs):
                merged[-1][3] = seg_end
            else:
                merged.append([leaf, source, seg_start, seg_end, refs])
        return tuple(Segment(source, seg_start, seg_end, refs)
                     for _, source, seg_start, seg_end, refs in merged)

    def _segments(self, node, start, lo, hi, refs, out):
        if isinstance(node, Leaf):
            out.append((node, node.source,
                        node.start + lo - start, node.start + hi - start,
                        refs))
        elif isinstance(node, Concat):
            child_start = start
            for child in node.children:
                child_end = child_start + self._len(child)
                if lo < child_end and hi > child_start:
                    self._segments(child, child_start,
                                   max(lo, child_start),
                                   min(hi, child_end), refs, out)
                child_start = child_end
                if child_start >= hi:
                    break
        else:  # Repeat
            body = self._len(node.child)
            first = (lo - start) // body
            last = (hi - 1 - start) // body
            for iteration in range(first, last + 1):
                it_start = start + iteration * body
                self._segments(node.child, it_start,
                               max(lo, it_start),
                               min(hi, it_start + body),
                               refs + (RefFrame(node, iteration),), out)


# ---------------------------------------------------------------------------
# Byte grammars


def bytes_grammar(rules, default_rule=None):
    """Return a Grammar that matches bytes, built from ``rules``.

    ``rules`` may be a parsimonious rule string or an existing
    :class:`~parsimonious.grammar.Grammar`.  ASCII (str) literals and
    patterns are re-encoded to bytes; existing byte literals are kept.
    Only the byte-oriented constructs are supported: literals, single
    regex atoms (e.g. unanchored single-byte character classes),
    sequences, ordered choice, quantifiers and both lookaheads.
    """
    grammar = rules if isinstance(rules, Grammar) else Grammar(rules)
    memo = {}

    def convert(expr):
        key = id(expr)
        if key in memo:
            return memo[key]
        if isinstance(expr, TokenMatcher):
            raise TypeError('token grammars are not supported')
        if isinstance(expr, Literal):
            literal = expr.literal
            if isinstance(literal, str):
                literal = literal.encode('latin-1')
            new = Literal(literal, name=expr.name)
        elif isinstance(expr, Regex):
            pattern = expr.re.pattern
            if isinstance(pattern, str):
                pattern = pattern.encode('latin-1')
            flags = expr.re.flags & ~_re.UNICODE & ~_re.LOCALE
            new = Regex(pattern, name=expr.name)
            new.re = _re.compile(pattern, flags)
            new.identity_tuple = (expr.name, new.re)
        elif isinstance(expr, (Sequence, OneOf)):
            new = type(expr)(*[convert(member) for member in expr.members],
                             name=expr.name)
        elif isinstance(expr, Lookahead):
            new = Lookahead(convert(expr.members[0]),
                            negative=expr.negativity, name=expr.name)
        elif isinstance(expr, Quantifier):
            new = Quantifier(convert(expr.members[0]),
                             min=expr.min, max=expr.max, name=expr.name)
        else:
            raise TypeError('unsupported expression type: %r' % type(expr))
        memo[key] = new
        return new

    new_grammar = Grammar.__new__(Grammar)
    OrderedDict.__init__(new_grammar,
                         ((name, convert(expr))
                          for name, expr in grammar.items()))
    if default_rule is not None:
        new_grammar.default_rule = new_grammar[default_rule]
    elif grammar.default_rule is not None:
        new_grammar.default_rule = convert(grammar.default_rule)
    else:
        new_grammar.default_rule = None
    return new_grammar


# ---------------------------------------------------------------------------
# Records


class Record:
    """One recognized record of a volume."""

    __slots__ = ('index', 'start', 'end', 'branch', 'decisions', 'fields',
                 '_volume')

    def __init__(self, index, start, end, branch, decisions, fields, volume):
        self.index = index          # 0-based record number within the volume
        self.start = start          # expanded offset of the first byte
        self.end = end              # expanded offset one past the last byte
        self.branch = branch        # Decision of the record rule, or None
        self.decisions = decisions  # every ordered-choice Decision, pre-order
        self.fields = fields        # named Fields in consumption order
        self._volume = volume

    @property
    def bytes(self):
        """The expanded bytes of this record."""
        return self._volume.read(self.start, self.end - self.start)

    @property
    def text(self):
        """The record bytes decoded as latin-1 (1 char per byte)."""
        return self.bytes.decode('latin-1')

    def fields_named(self, name):
        """Return this record's fields carrying the given rule name."""
        return tuple(field for field in self.fields if field.name == name)

    def __repr__(self):
        return '<Record %s [%s:%s] %r>' % (self.index, self.start,
                                           self.end, self.bytes)


class _Lap:
    """A run of skipped record cycles sharing one periodic pattern."""

    __slots__ = ('start', 'period_records', 'laps', 'period_bytes',
                 'lap0_start', 'rel')

    def __init__(self, start, period_records, laps, period_bytes,
                 lap0_start, rel):
        self.start = start                # first record index covered
        self.period_records = period_records  # records per lap
        self.laps = laps                  # number of laps covered
        self.period_bytes = period_bytes  # expanded bytes per lap
        self.lap0_start = lap0_start      # expanded offset of lap 0
        self.rel = rel                    # record offsets within one lap

    @property
    def n(self):
        return self.period_records * self.laps


class Archive:
    """Sequential-record, random-access view of a volume under a grammar.

    Records are consecutive matches of the grammar's record rule (the
    default rule, or ``rule``) starting at the head of the volume; each
    record starts where the previous one ended.  The scan is lazy and
    skips periodic repeat regions arithmetically, so record number k is
    answered directly even when k is in the billions.

    :arg grammar: a rule string or Grammar; see :func:`bytes_grammar`.
    :arg volume: the :class:`Volume` to read.
    :arg rule: name of the record rule (defaults to the default rule).
    :arg fields: optional collection of rule names to report as fields
        (default: every named rule below the record rule).
    :arg window: per-attempt lookahead budget in bytes (default 64).
    """

    def __init__(self, grammar, volume, rule=None, fields=None,
                 window=WINDOW):
        self.grammar = bytes_grammar(grammar)
        if rule is not None:
            self.record_expr = self.grammar[rule]
        elif self.grammar.default_rule is not None:
            self.record_expr = self.grammar.default_rule
        else:
            raise RuntimeError('no record rule given and the grammar has '
                               'no default rule')
        self.volume = volume
        self.field_names = set(fields) if fields is not None else None
        self.window = window
        #: Number of record-match attempts performed so far.
        self.attempts = 0
        self._steps = []   # stepped Records, in order
        self._spans = []   # Record or _Lap, in record order
        self._cum = []     # cumulative record counts after each span
        self._seen = {}    # canonical position -> (offset, count, step)
        self._pos = 0
        self._count = 0
        self._tail = None

    # -- scanning -----------------------------------------------------------

    def _add_span(self, span):
        n = span.n if isinstance(span, _Lap) else 1
        self._spans.append(span)
        self._cum.append((self._cum[-1] if self._cum else 0) + n)

    def _max_laps(self, pos, prev_pos, period_bytes, total):
        laps = (total - pos) // period_bytes
        now = self.volume.repeat_frames(pos)
        before = self.volume.repeat_frames(prev_pos)
        for (node, remaining), (prev_node, prev_remaining) in zip(now, before):
            delta = prev_remaining - remaining
            if delta > 0:
                # Stop skipping before any repeat's remaining count drops
                # below CAP, where canonical forms become more precise.
                laps = min(laps, (remaining - CAP) // delta)
        return max(laps, 0)

    def _scan_some(self, target):
        """Scan until record ``target`` is known (or the tail is reached).

        With ``target=None``, scan to the end of the volume.
        """
        volume = self.volume
        total = volume.length
        while self._tail is None and (target is None
                                      or self._count <= target):
            pos = self._pos
            if pos >= total:
                self._tail = Tail(pos, self._count, True)
                break
            key = volume.canonical(pos)
            prev = self._seen.get(key)
            if prev is not None:
                prev_pos, prev_count, prev_step = prev
                period_bytes = pos - prev_pos
                period_records = self._count - prev_count
                laps = self._max_laps(pos, prev_pos, period_bytes, total)
                if laps > 0:
                    rel = tuple(record.start - prev_pos
                                for record in self._steps[prev_step:])
                    assert len(rel) == period_records
                    self._add_span(_Lap(self._count, period_records, laps,
                                        period_bytes, prev_pos, rel))
                    self._pos = pos + laps * period_bytes
                    self._count += laps * period_records
                    self._seen.clear()
                    continue
            self._seen[key] = (pos, self._count, len(self._steps))
            record = self._parse_record_at(pos, self._count)
            if record is None:
                self._tail = Tail(pos, self._count, False)
                break
            self._steps.append(record)
            self._add_span(record)
            self._pos = record.end
            self._count += 1

    # -- parsing --------------------------------------------------------------

    def _parse_record_at(self, offset, index):
        self.attempts += 1
        window = self.volume.read(offset, self.window)
        cache = defaultdict(dict)
        error = ParseError(window)
        node = self.record_expr.match_core(window, 0, cache, error)
        if node is None:
            return None
        if node.end == 0:
            raise ValueError('record rule matched zero bytes at offset %d; '
                             'records must consume at least one byte' % offset)
        return self._make_record(index, offset, node)

    def _make_record(self, index, offset, node):
        decisions = []
        fields = []
        self._harvest(node, node, offset, decisions, fields)
        branch = decisions[0] if isinstance(self.record_expr, OneOf) else None
        return Record(index, offset, offset + node.end, branch,
                      tuple(decisions), tuple(fields), self.volume)

    def _harvest(self, root, node, offset, decisions, fields):
        expr = node.expr
        if isinstance(expr, OneOf) and node.children:
            chosen = node.children[0]
            for index, member in enumerate(expr.members):
                if member is chosen.expr:
                    decisions.append(Decision(expr, index))
                    break
        if expr.name and node is not root and (
                self.field_names is None or expr.name in self.field_names):
            start, end = offset + node.start, offset + node.end
            fields.append(Field(expr.name, start, end,
                                self.volume.segments_for_range(start, end)))
        for child in node.children:
            self._harvest(root, child, offset, decisions, fields)

    # -- queries -----------------------------------------------------------

    def record(self, index):
        """Return record number ``index`` (0-based) of the volume."""
        if index < 0:
            index += len(self)
        if index < 0:
            raise IndexError(index)
        self._scan_some(index)
        if index >= self._count:
            raise IndexError(index)
        span_index = bisect_right(self._cum, index)
        span_start = self._cum[span_index - 1] if span_index else 0
        span = self._spans[span_index]
        if isinstance(span, Record):
            return span
        off = index - span_start
        lap = 1 + off // span.period_records
        j = off % span.period_records
        offset = span.lap0_start + lap * span.period_bytes + span.rel[j]
        record = self._parse_record_at(offset, index)
        if record is None:
            raise AssertionError('skipped record failed to parse at %d'
                                 % offset)
        return record

    __getitem__ = record

    def match_at(self, offset):
        """Match one record at ``offset`` directly, or return None.

        This is the plain grammar applied at an arbitrary expanded
        offset; it agrees with :meth:`record` wherever a record starts.
        """
        return self._parse_record_at(offset, None)

    @property
    def tail(self):
        """The Tail state of the volume (scans to the end if needed)."""
        self._scan_some(None)
        return self._tail

    def __len__(self):
        """Total number of records in the volume."""
        self._scan_some(None)
        return self._count
