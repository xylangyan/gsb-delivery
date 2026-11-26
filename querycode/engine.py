# -*- coding: utf-8 -*-
"""Short ASCII query codes for templated origin names.

A *sheet* (``source``) owns one or more field templates.  Templates are finite,
acyclic expressions built from short literals, fragment references, sequence,
alternation and bounded repetition.  Every fragment has an independent id so a
derivation can be reported as a list of fragment ids.

Transliteration uses this repository's Unidecode tables.  A table entry may be
``None`` (missing), ``''`` (explicitly empty) or a string of one or more ASCII
characters.  Missing characters follow the sheet policy (``ignore``,
``replace`` or ``strict``); explicitly empty characters always stay empty.
Matching works on the expanded ASCII stream, so one character that expands to
two characters can line up with two neighbouring characters that expand to one
character each.

The public surface is deliberately small:

``Spec``
    Validated, normalised specification.
``transliterate(name, missing, replace_str)``
    Exact transliteration with the sheet policies; ``StrictError`` reports the
    index in the origin name of the first rejected character.
``compare_sources(spec, source_a, source_b, prefix, limit)``
    Stream the codes both sheets can form, in byte order, and classify them.
``classify_code(spec, source_a, source_b, code)``
    Classify one code: different names, same-name repeat, or absent on a side.
"""
import json
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from unidecode import _get_repl_str

__all__ = [
    'SpecError', 'StrictError', 'Spec', 'load_spec', 'transliterate',
    'compare_sources', 'classify_code', 'iter_codes',
]


class SpecError(ValueError):
    """Malformed specification (unknown id, cycle, bad bound, ...)."""


class StrictError(ValueError):
    """A strict sheet rejected an origin name.

    ``index`` is the offset in the origin name of the first character without
    a Unidecode replacement, matching the library's ``UnidecodeError.index``.
    """

    def __init__(self, name: str, index: int) -> None:
        self.name = name
        self.index = index
        super().__init__(
            'no replacement found for character %r in position %d'
            % (name[index], index))


# ---------------------------------------------------------------------------
# Transliteration
# ---------------------------------------------------------------------------

def _entry(char: str):
    """Raw table entry: None = missing, '' = explicitly empty."""
    if ord(char) < 0x80:
        return char
    return _get_repl_str(char)


def transliterate(name: str, missing: str = 'ignore',
                  replace_str: str = '?') -> str:
    """Transliterate *name* with an explicit missing-character policy.

    ``preserve`` is rejected: a query code must be ASCII.
    """
    if missing not in ('ignore', 'replace', 'strict'):
        raise SpecError('invalid missing-character policy %r' % (missing,))
    out: List[str] = []
    for index, char in enumerate(name):
        repl = _entry(char)
        if repl is None:
            if missing == 'ignore':
                repl = ''
            elif missing == 'replace':
                repl = replace_str
            else:
                raise StrictError(name, index)
        out.append(repl)
    return ''.join(out)


# ---------------------------------------------------------------------------
# Specification
# ---------------------------------------------------------------------------

def _norm(expr) -> tuple:
    if not isinstance(expr, dict) or len(expr) != 1:
        raise SpecError('expression must be an object with one operator: %r' % (expr,))
    (op, body), = expr.items()
    if op in ('lit', 'frag', 'ref'):
        if not isinstance(body, str):
            raise SpecError('%s takes a string' % op)
        return (op, body)
    if op in ('seq', 'alt'):
        if not isinstance(body, list):
            raise SpecError('%s takes a list' % op)
        return (op, tuple(_norm(e) for e in body))
    if op == 'rep':
        if not isinstance(body, dict):
            raise SpecError('rep takes an object')
        if 'expr' not in body:
            raise SpecError('rep needs an expr')
        lo = body.get('min', 0)
        hi = body.get('max')
        if not isinstance(lo, int) or lo < 0:
            raise SpecError('rep min must be a non-negative integer')
        if not isinstance(hi, int):
            raise SpecError('rep max must be a finite integer')
        if hi < lo:
            raise SpecError('rep max must be >= min')
        if hi > 64:
            raise SpecError('rep max %d is too large for a query code' % hi)
        return ('rep', _norm(body['expr']), lo, hi)
    raise SpecError('unknown operator %r' % (op,))


def _refs(expr) -> Iterator[str]:
    op = expr[0]
    if op == 'ref':
        yield expr[1]
    elif op in ('seq', 'alt'):
        for e in expr[1]:
            yield from _refs(e)
    elif op == 'rep':
        yield from _refs(expr[1])


class Spec:
    """A validated set of fragments, templates and sheets."""

    def __init__(self, data) -> None:
        if not isinstance(data, dict):
            raise SpecError('spec must be an object')
        tpl = data.get('templates', {})
        frag = data.get('fragments', {})
        src = data.get('sources', {})
        if not isinstance(frag, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in frag.items()):
            raise SpecError('fragments must map ids to strings')
        if not isinstance(tpl, dict):
            raise SpecError('templates must be an object')
        self.fragments = dict(frag)
        self.templates = {k: _norm(v) for k, v in tpl.items()}
        self.sources: Dict[str, dict] = {}
        if not isinstance(src, dict):
            raise SpecError('sources must be an object')
        for sid, sheet in src.items():
            self._add_source(sid, sheet)
        self.queries = self._add_queries(data.get('queries', []))
        self._check_refs()

    def _add_source(self, sid, sheet) -> None:
        if not isinstance(sheet, dict):
            raise SpecError('source %r must be an object' % (sid,))
        tids = sheet.get('templates')
        if not isinstance(tids, list) or not tids or not all(isinstance(t, str) for t in tids):
            raise SpecError('source %r needs a non-empty template id list' % (sid,))
        for tid in tids:
            if tid not in self.templates:
                raise SpecError('source %r references unknown template %r' % (sid, tid))
        missing = sheet.get('missing', 'ignore')
        if missing not in ('ignore', 'replace', 'strict'):
            raise SpecError('source %r has invalid missing policy %r' % (sid, missing))
        replace_str = sheet.get('replace_str', '?')
        if not isinstance(replace_str, str) or not replace_str.isascii():
            raise SpecError('replace_str must be an ASCII string')
        self.sources[sid] = {
            'templates': list(tids),
            'missing': missing,
            'replace_str': replace_str,
        }

    def _add_queries(self, queries) -> List[dict]:
        if not isinstance(queries, list):
            raise SpecError('queries must be a list')
        out = []
        for q in queries:
            if not isinstance(q, dict):
                raise SpecError('each query must be an object')
            sid = q.get('source')
            if sid not in self.sources:
                raise SpecError('query references unknown source %r' % (sid,))
            prefix = q.get('prefix', '')
            limit = q.get('limit', 20)
            if not isinstance(prefix, str) or not prefix.isascii():
                raise SpecError('query prefix must be an ASCII string')
            if not isinstance(limit, int) or limit < 0:
                raise SpecError('query limit must be a non-negative integer')
            out.append({'source': sid, 'prefix': prefix, 'limit': limit})
        return out

    def _check_refs(self) -> None:
        state: Dict[str, int] = {}

        def visit(tid: str, stack: List[str]) -> None:
            mark = state.get(tid, 0)
            if mark == 1:
                raise SpecError('template cycle: %s' % ' -> '.join(stack + [tid]))
            if mark == 2:
                return
            state[tid] = 1
            for ref in _refs(self.templates[tid]):
                if ref not in self.templates:
                    raise SpecError('template %r references unknown template %r'
                                    % (tid, ref))
                visit(ref, stack + [tid])
            state[tid] = 2

        for tid in self.templates:
            visit(tid, [])
        for tid, expr in self.templates.items():
            for op_body in _fragment_ids(expr):
                if op_body not in self.fragments:
                    raise SpecError('template %r references unknown fragment %r'
                                    % (tid, op_body))


def _fragment_ids(expr) -> Iterator[str]:
    op = expr[0]
    if op == 'frag':
        yield expr[1]
    elif op in ('seq', 'alt'):
        for e in expr[1]:
            yield from _fragment_ids(e)
    elif op == 'rep':
        yield from _fragment_ids(expr[1])


def load_spec(path) -> Spec:
    with open(path, encoding='utf-8') as fp:
        return Spec(json.load(fp))


# ---------------------------------------------------------------------------
# NFA construction
# ---------------------------------------------------------------------------

# A transition carries:
#   output character, target state, origin character or None,
#   derivation metadata (fragment id, template id, expression path) or None.
@dataclass(frozen=True)
class _Meta:
    fragment: str
    template: str
    path: Tuple[str, ...]


class _NFA:
    """Acyclic character-level NFA over ASCII output.

    States are integers allocated in increasing order; every transition goes to
    a strictly larger state, which makes path enumeration finite.
    """

    MAX_STATES = 200000

    def __init__(self, spec: Spec, source: str) -> None:
        self.spec = spec
        self.source = source
        self.sheet = spec.sources[source]
        self.eps: Dict[int, List[Tuple[int, Optional[str], Optional[_Meta]]]] = {}
        self.trans: Dict[int, List[Tuple[str, int, Optional[str], Optional[_Meta]]]] = {}
        self._next = 0
        self.start = self._new()
        self.end = self._new()
        for tid in self.sheet['templates']:
            entry = self._compile_template(tid, self.end, ())
            if entry is not None:
                self.eps.setdefault(self.start, []).append((entry, None, None))
        self._check_acyclic()

    def _new(self) -> int:
        state = self._next
        self._next += 1
        if self._next > self.MAX_STATES:
            raise SpecError('template expansion exceeds %d NFA states'
                            % self.MAX_STATES)
        return state

    def _compile_template(self, tid: str, nxt: int, path: Tuple[str, ...]):
        expr = self.spec.templates[tid]
        return self._compile(expr, nxt, (tid,) + path)

    def _compile(self, expr, nxt: int, path: Tuple[str, ...]):
        op = expr[0]
        if op == 'lit':
            return self._compile_text(expr[1], nxt, path, None)
        if op == 'frag':
            fid = expr[1]
            meta = _Meta(fid, path[0] if path else '', path)
            return self._compile_text(self.spec.fragments[fid], nxt, path, meta)
        if op == 'ref':
            tid = expr[1]
            # The referenced template is inlined with its own template id.
            expr2 = self.spec.templates[tid]
            return self._compile(expr2, nxt, (tid,) + path)
        if op == 'seq':
            entry = nxt
            for index in range(len(expr[1]) - 1, -1, -1):
                entry = self._compile(expr[1][index], entry,
                                      path + ('seq[%d]' % index,))
                if entry is None:
                    return None
            return entry
        if op == 'alt':
            entry = self._new()
            for index, sub in enumerate(expr[1]):
                branch = self._compile(sub, nxt, path + ('alt[%d]' % index,))
                if branch is not None:
                    self.eps.setdefault(entry, []).append((branch, None, None))
            return entry
        if op == 'rep':
            inner, lo, hi = expr[1], expr[2], expr[3]
            chain = nxt
            for index in range(hi - 1, lo - 1, -1):
                copy = self._new()
                body = self._compile(inner, chain, path + ('rep[%d]' % index,))
                self.eps.setdefault(copy, []).append((chain, None, None))
                if body is not None:
                    self.eps.setdefault(copy, []).append((body, None, None))
                chain = copy
            for index in range(lo - 1, -1, -1):
                chain = self._compile(inner, chain, path + ('rep[%d]' % index,))
                if chain is None:
                    return None
            return chain
        raise SpecError('unexpected operator %r' % (op,))

    def _compile_text(self, text: str, nxt: int, path: Tuple[str, ...],
                      meta: Optional[_Meta]):
        """Compile literal text; returns None if a strict sheet rejects it."""
        chain = nxt
        for char in reversed(text):
            repl = _entry(char)
            if repl is None:
                if self.sheet['missing'] == 'strict':
                    return None
                repl = '' if self.sheet['missing'] == 'ignore' else self.sheet['replace_str']
            chain = self._emit(char, repl, chain, meta)
        return chain

    def _emit(self, origin: str, out: str, nxt: int,
              meta: Optional[_Meta]) -> int:
        if not out:
            state = self._new()
            self.eps.setdefault(state, []).append((nxt, origin, meta))
            return state
        state = nxt
        for index in range(len(out) - 1, -1, -1):
            prev = self._new()
            origin_here = origin if index == 0 else None
            meta_here = meta if index == 0 else None
            self.trans.setdefault(prev, []).append((out[index], state, origin_here, meta_here))
            state = prev
        return state

    def _check_acyclic(self) -> None:
        colour = [0] * self._next

        def walk(state: int) -> None:
            colour[state] = 1
            edges = [v for v, _, _ in self.eps.get(state, ())]
            edges += [v for _, v, _, _ in self.trans.get(state, ())]
            for nxt in edges:
                if colour[nxt] == 1:
                    raise SpecError('internal error: cyclic NFA')
                if colour[nxt] == 0:
                    walk(nxt)
            colour[state] = 2

        walk(self.start)


def _nfa(spec: Spec, source: str) -> _NFA:
    return _NFA(spec, source)
