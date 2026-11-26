# -*- coding: utf-8 -*-
"""Deterministic, acyclic query-code automata.

The NFA in :mod:`querycode.engine` is determinised lazily.  Because templates
are finite and acyclic, every reachable DFA state graph is a DAG.  That makes
three operations cheap and exact:

* count accepted codes under a prefix (dynamic programming, no enumeration),
* enumerate the first page in true byte order (guided DFS),
* reconstruct a legal derivation for one code (DP over NFA states).

No origin name or code list is ever built for a whole sheet.
"""
from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .engine import Spec, _Meta, _NFA

Config = Tuple[int, Optional[str], Optional[_Meta]]


@dataclass(frozen=True)
class Witness:
    """One legal derivation of a code on one sheet."""
    name: str
    fragments: Tuple[str, ...]
    template: str
    path: Tuple[str, ...]
    origins: Tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'fragments': list(self.fragments),
            'template': self.template,
            'path': list(self.path),
        }


class Matcher:
    """Lazy DFA for one sheet's transliterated language."""

    MAX_STATES = 500000

    def __init__(self, spec: Spec, source: str) -> None:
        self.spec = spec
        self.source = source
        self.sheet = spec.sources[source]
        self.nfa = _NFA(spec, source)
        self._states: List[Tuple[Config, ...]] = []
        self._index: Dict[Tuple[Config, ...], int] = {}
        self._edges: Dict[Tuple[int, str], int] = {}
        self._labels: Dict[int, Tuple[str, ...]] = {}
        self._accepting = set()
        self.start = self._intern(self._closure([(self.nfa.start, None, None)]))

    # -- subset construction ----------------------------------------------
    def _closure(self, configs: Iterable[Config]) -> Tuple[Config, ...]:
        seen = set()
        queue = deque(configs)
        out: List[Config] = []
        while queue:
            cfg = queue.popleft()
            if cfg in seen:
                continue
            seen.add(cfg)
            out.append(cfg)
            state, origin, meta = cfg
            for nxt, eps_origin, eps_meta in self.nfa.eps.get(state, ()):
                queue.append((nxt,
                              eps_origin if eps_origin is not None else origin,
                              eps_meta if eps_meta is not None else meta))
        return tuple(out)

    def _intern(self, configs: Tuple[Config, ...]) -> int:
        try:
            return self._index[configs]
        except KeyError:
            if len(self._states) >= self.MAX_STATES:
                raise RuntimeError('DFA exceeded %d states; simplify the templates'
                                   % self.MAX_STATES)
            index = len(self._states)
            self._states.append(configs)
            self._index[configs] = index
            if any(cfg[0] == self.nfa.end for cfg in configs):
                self._accepting.add(index)
            return index

    def step(self, state: int, char: str) -> int:
        key = (state, char)
        try:
            return self._edges[key]
        except KeyError:
            pass
        moved: List[Config] = []
        for cfg in self._states[state]:
            nfa_state, origin, meta = cfg
            for out_char, target, trans_origin, trans_meta in self.nfa.trans.get(nfa_state, ()):
                if out_char == char:
                    moved.append((target,
                                  trans_origin if trans_origin is not None else origin,
                                  trans_meta if trans_meta is not None else meta))
        result = self._intern(self._closure(moved))
        self._edges[key] = result
        return result

    def labels(self, state: int) -> Tuple[str, ...]:
        try:
            return self._labels[state]
        except KeyError:
            chars = set()
            for cfg in self._states[state]:
                for out_char, _target, _origin, _meta in self.nfa.trans.get(cfg[0], ()):
                    chars.add(out_char)
            result = tuple(sorted(chars))
            self._labels[state] = result
            return result

    def accepting(self, state: int) -> bool:
        return state in self._accepting

    def advance(self, state: int, text: str) -> int:
        for char in text:
            state = self.step(state, char)
            if not self._states[state]:
                break
        return state


class _Counter:
    """Memoised accepted-string counts on the acyclic DFA graph."""

    def __init__(self, matcher: Matcher) -> None:
        self.matcher = matcher
        self.memo: Dict[int, int] = {}
        self.visiting = set()

    def count(self, state: int) -> int:
        if state in self.memo:
            return self.memo[state]
        if state in self.visiting:
            raise RuntimeError('query-code automaton is cyclic')
        self.visiting.add(state)
        total = 1 if self.matcher.accepting(state) else 0
        for char in self.matcher.labels(state):
            nxt = self.matcher.step(state, char)
            if self.matcher._states[nxt]:
                total += self.count(nxt)
        self.visiting.discard(state)
        self.memo[state] = total
        return total


class Product:
    """Product of two sheet DFAs: intersection of their code languages."""

    def __init__(self, left: Matcher, right: Matcher) -> None:
        self.left = left
        self.right = right
        self._states: List[Tuple[int, int]] = []
        self._index: Dict[Tuple[int, int], int] = {}
        self._edges: Dict[Tuple[int, str], int] = {}
        self.start = self._intern((left.start, right.start))
        self._counter = _ProductCounter(self)

    def _intern(self, pair: Tuple[int, int]) -> int:
        try:
            return self._index[pair]
        except KeyError:
            index = len(self._states)
            self._states.append(pair)
            self._index[pair] = index
            return index

    def step(self, state: int, char: str) -> int:
        key = (state, char)
        try:
            return self._edges[key]
        except KeyError:
            pass
        a, b = self._states[state]
        na = self.left.step(a, char)
        nb = self.right.step(b, char)
        result = self._intern((na, nb))
        self._edges[key] = result
        return result

    def labels(self, state: int) -> Tuple[str, ...]:
        a, b = self._states[state]
        return tuple(sorted(set(self.left.labels(a)) & set(self.right.labels(b))))

    def accepting(self, state: int) -> bool:
        a, b = self._states[state]
        return self.left.accepting(a) and self.right.accepting(b)

    def prefix_state(self, prefix: str) -> int:
        state = self.start
        for char in prefix:
            state = self.step(state, char)
            if not self.left._states[self._states[state][0]] and \
                    not self.right._states[self._states[state][1]]:
                break
        return state

    def count(self, prefix: str = '') -> int:
        return self._counter.count(self.prefix_state(prefix))

    def iter_codes(self, prefix: str = '') -> Iterator[str]:
        state = self.prefix_state(prefix)
        if not self._can_accept(state):
            return
        stack: List[Tuple[str, int, int]] = [(prefix, state, 0)]
        if self.accepting(state):
            yield prefix
        while stack:
            text, state, index = stack[-1]
            labels = self.labels(state)
            if index >= len(labels):
                stack.pop()
                continue
            char = labels[index]
            stack[-1] = (text, state, index + 1)
            nxt = self.step(state, char)
            if not self._can_accept(nxt):
                continue
            child = text + char
            if self.accepting(nxt):
                yield child
            stack.append((child, nxt, 0))

    def _can_accept(self, state: int) -> bool:
        return self._counter.count(state) > 0


class _ProductCounter:
    def __init__(self, product: Product) -> None:
        self.product = product
        self.memo: Dict[int, int] = {}
        self.visiting = set()

    def count(self, state: int) -> int:
        if state in self.memo:
            return self.memo[state]
        if state in self.visiting:
            raise RuntimeError('product automaton is cyclic')
        self.visiting.add(state)
        total = 1 if self.product.accepting(state) else 0
        for char in self.product.labels(state):
            total += self.count(self.product.step(state, char))
        self.visiting.discard(state)
        self.memo[state] = total
        return total


class Sheet:
    """A sheet with lazily built matcher and witness reconstruction."""

    def __init__(self, spec: Spec, source: str) -> None:
        if source not in spec.sources:
            raise KeyError(source)
        self.spec = spec
        self.source = source
        self.matcher = Matcher(spec, source)
        self._counter = _Counter(self.matcher)

    def count(self, prefix: str = '') -> int:
        state = self.matcher.advance(self.matcher.start, prefix)
        if not self.matcher._states[state]:
            return 0
        return self._counter.count(state)

    def iter_codes(self, prefix: str = '') -> Iterator[str]:
        state = self.matcher.advance(self.matcher.start, prefix)
        if not self.matcher._states[state]:
            return
        stack: List[Tuple[str, int, int]] = [(prefix, state, 0)]
        if self.matcher.accepting(state):
            yield prefix
        while stack:
            text, state, index = stack[-1]
            labels = self.matcher.labels(state)
            if index >= len(labels):
                stack.pop()
                continue
            char = labels[index]
            stack[-1] = (text, state, index + 1)
            nxt = self.matcher.step(state, char)
            if not self.matcher._states[nxt]:
                continue
            if self._counter.count(nxt) == 0:
                continue
            child = text + char
            if self.matcher.accepting(nxt):
                yield child
            stack.append((child, nxt, 0))

    def witness(self, code: str) -> Optional[Witness]:
        """Return one legal derivation of *code*, or None."""
        found = self.derivations(code, limit=1)
        return found[0] if found else None

    def derivations(self, code: str, limit: Optional[int] = None) -> List[Witness]:
        """Every distinct derivation of *code* (fragment ids are reported)."""
        nfa = self.matcher.nfa
        out: List[Witness] = []
        seen: set = set()

        # Depth-first over NFA paths; acyclic by construction.
        def walk(state: int, pos: int, steps) -> None:
            if limit is not None and len(out) >= limit:
                return
            if state == nfa.end:
                if pos == len(code):
                    witness = _steps_to_witness(steps)
                    key = (witness.name, witness.fragments,
                           witness.template, witness.path)
                    if key not in seen:
                        seen.add(key)
                        out.append(witness)
                return
            for nxt, origin, meta in nfa.eps.get(state, ()):
                walk(nxt, pos, steps + ((None, origin, meta),))
                if limit is not None and len(out) >= limit:
                    return
            if pos < len(code):
                for out_char, target, origin, meta in nfa.trans.get(state, ()):
                    if out_char == code[pos]:
                        walk(target, pos + 1, steps + ((out_char, origin, meta),))
                        if limit is not None and len(out) >= limit:
                            return

        walk(nfa.start, 0, ())
        return out

    def names(self, code: str, limit: int = 2) -> List[str]:
        """Up to *limit* distinct origin names spelling *code*.

        ``limit=2`` is enough to decide whether a sheet has exactly one name
        for a code or several: any intermediate node only needs two distinct
        completions, because a common prefix preserves their distinctness.
        """
        nfa = self.matcher.nfa
        memo: Dict[Tuple[int, int], List[str]] = {}

        def solve(state: int, pos: int) -> List[str]:
            key = (state, pos)
            if key in memo:
                return memo[key]
            out: List[str] = []
            if state == nfa.end:
                memo[key] = [''] if pos == len(code) else []
                return memo[key]
            for nxt, origin, _meta in nfa.eps.get(state, ()):
                for suffix in solve(nxt, pos):
                    name = (origin or '') + suffix
                    if name not in out:
                        out.append(name)
                        if len(out) >= limit:
                            memo[key] = out
                            return out
            if pos < len(code):
                for out_char, target, origin, _meta in nfa.trans.get(state, ()):
                    if out_char != code[pos]:
                        continue
                    for suffix in solve(target, pos + 1):
                        name = (origin or '') + suffix
                        if name not in out:
                            out.append(name)
                            if len(out) >= limit:
                                memo[key] = out
                                return out
            memo[key] = out
            return out

        return solve(nfa.start, 0)

    def witness_for(self, code: str, name: str) -> Optional[Witness]:
        """A derivation of *code* whose origin name is exactly *name*."""
        nfa = self.matcher.nfa
        good: Dict[Tuple[int, int, int], bool] = {}

        def solve(state: int, pos: int, name_pos: int) -> bool:
            key = (state, pos, name_pos)
            if key in good:
                return good[key]
            good[key] = False
            if state == nfa.end:
                good[key] = pos == len(code) and name_pos == len(name)
                return good[key]
            for nxt, origin, _meta in nfa.eps.get(state, ()):
                if origin is None:
                    if solve(nxt, pos, name_pos):
                        good[key] = True
                        return True
                elif name_pos < len(name) and name[name_pos] == origin:
                    if solve(nxt, pos, name_pos + 1):
                        good[key] = True
                        return True
            if pos < len(code):
                for out_char, target, origin, _meta in nfa.trans.get(state, ()):
                    if out_char != code[pos]:
                        continue
                    if origin is None:
                        if solve(target, pos + 1, name_pos):
                            good[key] = True
                            return True
                    elif name_pos < len(name) and name[name_pos] == origin:
                        if solve(target, pos + 1, name_pos + 1):
                            good[key] = True
                            return True
            return False

        if not solve(nfa.start, 0, 0):
            return None
        state, pos, name_pos = nfa.start, 0, 0
        steps = []
        while not (state == nfa.end and pos == len(code) and name_pos == len(name)):
            advanced = False
            for nxt, origin, meta in nfa.eps.get(state, ()):
                if origin is None:
                    if solve(nxt, pos, name_pos):
                        steps.append((None, origin, meta))
                        state = nxt
                        advanced = True
                        break
                elif name_pos < len(name) and name[name_pos] == origin:
                    if solve(nxt, pos, name_pos + 1):
                        steps.append((None, origin, meta))
                        state, name_pos = nxt, name_pos + 1
                        advanced = True
                        break
            if advanced:
                continue
            for out_char, target, origin, meta in nfa.trans.get(state, ()):
                if out_char != code[pos]:
                    continue
                if origin is None:
                    if solve(target, pos + 1, name_pos):
                        steps.append((out_char, origin, meta))
                        state, pos = target, pos + 1
                        advanced = True
                        break
                elif name_pos < len(name) and name[name_pos] == origin:
                    if solve(target, pos + 1, name_pos + 1):
                        steps.append((out_char, origin, meta))
                        state, pos, name_pos = target, pos + 1, name_pos + 1
                        advanced = True
                        break
            if not advanced:
                return None
        return _steps_to_witness(steps)


def _steps_to_witness(steps) -> Witness:
    fragments: List[str] = []
    origins: List[str] = []
    template = ''
    path: Tuple[str, ...] = ()
    for _char, origin, meta in steps:
        if origin is not None:
            origins.append(origin)
        if meta is not None:
            if meta.fragment not in fragments:
                fragments.append(meta.fragment)
            if not template:
                template = meta.template
                path = meta.path
    return Witness(''.join(origins), tuple(fragments), template, path, tuple(origins))
