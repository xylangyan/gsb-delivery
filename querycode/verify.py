# -*- coding: utf-8 -*-
"""Brute-force reference implementation used by the tests.

Only small specs are supported: every template is expanded into the set of
origin names it can produce and each name is transliterated exactly as the
library does.  ``verify_by_enumeration`` compares the optimised search against
this reference for a bounded number of names.
"""
from itertools import product
from typing import Dict, Iterable, Iterator, List, Set, Tuple

from .engine import Spec, SpecError, StrictError, transliterate


MAX_NAMES = 100000


def expand_template(spec: Spec, tid: str) -> Set[str]:
    """All origin names template *tid* can form (finite by construction)."""
    cache: Dict[str, Set[str]] = {}

    def expand(expr) -> Set[str]:
        op = expr[0]
        if op == 'lit':
            return {expr[1]}
        if op == 'frag':
            return {spec.fragments[expr[1]]}
        if op == 'ref':
            return expand_template_cached(expr[1])
        if op == 'seq':
            result = {''}
            for sub in expr[1]:
                part = expand(sub)
                result = {a + b for a in result for b in part}
                if len(result) > MAX_NAMES:
                    raise SpecError('template expansion is too large to verify')
            return result
        if op == 'alt':
            result: Set[str] = set()
            for sub in expr[1]:
                result |= expand(sub)
            return result
        if op == 'rep':
            inner, lo, hi = expr[1], expr[2], expr[3]
            part = expand(inner)
            result: Set[str] = set()
            current = {''}
            for count in range(hi + 1):
                if count >= lo:
                    result |= current
                current = {a + b for a in current for b in part}
                if len(current) > MAX_NAMES:
                    raise SpecError('template expansion is too large to verify')
            return result
        raise SpecError('unexpected operator %r' % (op,))

    def expand_template_cached(template_id: str) -> Set[str]:
        if template_id not in cache:
            cache[template_id] = expand(spec.templates[template_id])
        return cache[template_id]

    return set(expand_template_cached(tid))


def source_names(spec: Spec, source: str) -> Set[str]:
    """All origin names a sheet can form, over all of its templates."""
    names: Set[str] = set()
    for tid in spec.sources[source]['templates']:
        names |= expand_template(spec, tid)
    return names


def source_codes(spec: Spec, source: str) -> Dict[str, Set[str]]:
    """Map every code the sheet can form to the names that form it."""
    sheet = spec.sources[source]
    result: Dict[str, Set[str]] = {}
    for name in source_names(spec, source):
        try:
            code = transliterate(name, sheet['missing'], sheet['replace_str'])
        except StrictError:
            continue
        result.setdefault(code, set()).add(name)
    return result


def verify_by_enumeration(spec: Spec, source_a: str, source_b: str,
                          prefix: str = '') -> Tuple[int, Dict[str, str]]:
    """Return ``(intersection_size, {code: kind})`` via brute force.

    ``kind`` is ``different-name`` when the two sides can choose different
    origin names for the same code, otherwise ``same-name-repeat``.
    """
    a = source_codes(spec, source_a)
    b = source_codes(spec, source_b)
    shared = {code for code in a.keys() & b.keys() if code.startswith(prefix)}
    kinds = {}
    for code in shared:
        if len(a[code] | b[code]) > 1:
            kinds[code] = 'different-name'
        else:
            kinds[code] = 'same-name-repeat'
    return len(shared), kinds
