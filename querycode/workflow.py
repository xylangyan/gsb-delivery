# -*- coding: utf-8 -*-
"""Whole-workflow API built on the search primitives."""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .engine import Spec, SpecError, StrictError, load_spec, transliterate
from .search import Product, Sheet, Witness
from . import verify as _verify

__all__ = [
    'Spec', 'SpecError', 'StrictError', 'load_spec', 'transliterate',
    'Witness', 'Sheet', 'Product', 'CodeResult', 'compare_sources',
    'classify_code', 'verify_by_enumeration',
]


@dataclass
class CodeResult:
    code: str
    kind: str
    left: Optional[Witness]
    right: Optional[Witness]

    @property
    def distinct_names(self) -> bool:
        return self.kind == 'different-name'

    def as_dict(self) -> dict:
        return {
            'code': self.code,
            'kind': self.kind,
            'left': self.left.as_dict() if self.left else None,
            'right': self.right.as_dict() if self.right else None,
        }


def _kind(sa: Sheet, sb: Sheet, code: str) -> Tuple[str, Optional[Witness], Optional[Witness]]:
    names_a = sa.names(code, limit=2)
    names_b = sb.names(code, limit=2)
    if not names_a and not names_b:
        return 'absent-both', None, None
    if not names_a:
        return 'absent-a', None, sb.witness_for(code, names_b[0])
    if not names_b:
        return 'absent-b', sa.witness_for(code, names_a[0]), None
    if len(names_a) == 1 and len(names_b) == 1 and names_a[0] == names_b[0]:
        return ('same-name-repeat',
                sa.witness_for(code, names_a[0]),
                sb.witness_for(code, names_b[0]))
    for a_name in names_a:
        for b_name in names_b:
            if a_name != b_name:
                return ('different-name',
                        sa.witness_for(code, a_name),
                        sb.witness_for(code, b_name))
    return ('same-name-repeat',
            sa.witness_for(code, names_a[0]),
            sb.witness_for(code, names_b[0]))


def classify_code(spec: Spec, source_a: str, source_b: str, code: str) -> CodeResult:
    if not code.isascii():
        raise ValueError('query code must be ASCII')
    kind, wa, wb = _kind(Sheet(spec, source_a), Sheet(spec, source_b), code)
    return CodeResult(code, kind, wa, wb)


def compare_sources(spec: Spec, source_a: str, source_b: str,
                    prefix: str = '', limit: Optional[int] = None):
    """Count and page codes both sheets can form, under an ASCII prefix.

    Only ``limit`` :class:`CodeResult` objects are constructed per page.
    """
    left = Sheet(spec, source_a)
    right = Sheet(spec, source_b)
    product = Product(left.matcher, right.matcher)
    total = product.count(prefix)
    page: List[CodeResult] = []
    if total and limit != 0:
        for code in product.iter_codes(prefix):
            kind, wa, wb = _kind(left, right, code)
            page.append(CodeResult(code, kind, wa, wb))
            if limit is not None and len(page) >= limit:
                break
    return total, page


def verify_by_enumeration(spec: Spec, source_a: str, source_b: str,
                          prefix: str = ''):
    """Small-template cross-check against the brute-force reference."""
    total, kinds = _verify.verify_by_enumeration(spec, source_a, source_b, prefix)
    return total, kinds


def count_distinct_name_codes(spec: Spec, source_a: str, source_b: str,
                              prefix: str = '', limit: Optional[int] = None):
    """Count intersection codes that two *different* names can spell.

    This streams the shared codes in byte order and classifies each one; names
    are never collected for the whole sheet, and every shared code is counted
    once regardless of how many template routes or fragment choices spell it.
    """
    left = Sheet(spec, source_a)
    right = Sheet(spec, source_b)
    product = Product(left.matcher, right.matcher)
    total = 0
    page: List[CodeResult] = []
    for code in product.iter_codes(prefix):
        kind, wa, wb = _kind(left, right, code)
        if kind == 'different-name':
            total += 1
            if limit is None or len(page) < limit:
                page.append(CodeResult(code, kind, wa, wb))
    return total, page


def derivations(spec: Spec, source: str, code: str,
                limit: Optional[int] = None) -> List[Witness]:
    """Every distinct way *source* can form *code*.

    Each witness records the origin name, template id, expression path and
    the fragment ids actually used, so two legal routes that assemble the same
    name are both visible.  ``limit`` bounds the result for large templates.
    """
    sheet = Sheet(spec, source)
    return sheet.derivations(code, limit=limit)
