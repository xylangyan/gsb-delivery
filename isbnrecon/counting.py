# -*- coding: utf-8 -*-
"""Exact counting of realized ISBN keys without enumerating bodies.

A bundle interval may span hundreds of millions of bodies, so counts use a
digit DP over the fixed-width 12-digit ISBN-13 body axis.  For a body the
ISBN-13 check character and (for 978 prefixes) the ISBN-10 check character
are uniquely determined by the checksums tracked in the DP state, hence a
body realizes at most one valid ISBN-13 key and support can be decided at the
leaf.

The engine never stores the growing body string in memoized state: callers
either pin specific digit positions (``fixed``) to target a known publisher
slice, or mark a short set of trace positions whose digits are carried in the
state (used to enumerate the few cells touched by a correction).  Memo size is
independent of the interval length, so a 200-million-body interval costs the
same as a two-body interval.
"""

from functools import lru_cache

from isbnlib._core import check_digit13

WIDTH13 = 12
PREFIX_978 = 978 * 10 ** 9


def check13_from_sum(checksum):
    digit = 10 - checksum % 10
    return '0' if digit == 10 else str(digit)


def check10_from_sum(checksum):
    remainder = checksum % 11
    if remainder == 0:
        return '0'
    if remainder == 1:
        return 'X'
    return str(11 - remainder)


def _weight13(pos):
    return (pos % 2) * 2 + 1


def _weight10(pos):
    # ISBN-10 weights 10..2 over body positions 3..11 of the ISBN-13.
    return 13 - pos


def _next_sums(pos, digit, sum13, sum10):
    sum13 += _weight13(pos) * digit
    if pos >= 3:
        sum10 += _weight10(pos) * digit
    return sum13, sum10


def _bounds(lo, hi, width=WIDTH13):
    if lo > hi:
        return None
    low = str(lo).zfill(width)
    high = str(hi).zfill(width)
    if len(high) > width:
        raise ValueError('interval exceeds the declared body width')
    return low, high


def _digit_span(pos, low, high, tight_lo, tight_hi, fixed):
    if pos in fixed:
        digit = fixed[pos]
        return digit, digit
    d_lo = int(low[pos]) if tight_lo else 0
    d_hi = int(high[pos]) if tight_hi else 9
    return d_lo, d_hi


def count_matches(lo, hi, leaf, fixed=None):
    """Count bodies of the closed interval ``[lo, hi]`` accepted by ``leaf``.

    ``leaf(c13, c10)`` decides on the two check characters derived from the
    body.  ``fixed`` optionally pins body positions to exact digits (publisher
    slices of a target cell).
    """
    fixed = fixed or {}
    bounded = _bounds(lo, hi)
    if bounded is None:
        return 0
    low, high = bounded

    @lru_cache(maxsize=None)
    def dp(pos, tight_lo, tight_hi, sum13, sum10):
        if pos == WIDTH13:
            return 1 if leaf(check13_from_sum(sum13),
                             check10_from_sum(sum10)) else 0
        d_lo, d_hi = _digit_span(pos, low, high, tight_lo, tight_hi, fixed)
        total = 0
        for digit in range(d_lo, d_hi + 1):
            nxt13, nxt10 = _next_sums(pos, digit, sum13, sum10)
            total += dp(pos + 1, tight_lo and digit == d_lo,
                        tight_hi and digit == d_hi, nxt13, nxt10)
        return total

    return dp(0, True, True, 0, 0)


def min_match(lo, hi, leaf, fixed=None):
    """Smallest accepted ISBN-13 key on ``[lo, hi]``, or ``None``."""
    fixed = fixed or {}
    bounded = _bounds(lo, hi)
    if bounded is None:
        return None
    low, high = bounded

    @lru_cache(maxsize=None)
    def feasible(pos, tight_lo, tight_hi, sum13, sum10):
        if pos == WIDTH13:
            return leaf(check13_from_sum(sum13), check10_from_sum(sum10))
        d_lo, d_hi = _digit_span(pos, low, high, tight_lo, tight_hi, fixed)
        for digit in range(d_lo, d_hi + 1):
            nxt13, nxt10 = _next_sums(pos, digit, sum13, sum10)
            if feasible(pos + 1, tight_lo and digit == d_lo,
                        tight_hi and digit == d_hi, nxt13, nxt10):
                return True
        return False

    if not feasible(0, True, True, 0, 0):
        return None

    digits = []
    tight_lo = tight_hi = True
    sum13 = sum10 = 0
    for pos in range(WIDTH13):
        d_lo, d_hi = _digit_span(pos, low, high, tight_lo, tight_hi, fixed)
        for digit in range(d_lo, d_hi + 1):
            nxt13, nxt10 = _next_sums(pos, digit, sum13, sum10)
            nxt_lo = tight_lo and digit == d_lo
            nxt_hi = tight_hi and digit == d_hi
            if feasible(pos + 1, nxt_lo, nxt_hi, nxt13, nxt10):
                digits.append(str(digit))
                tight_lo, tight_hi = nxt_lo, nxt_hi
                sum13, sum10 = nxt13, nxt10
                break
    return ''.join(digits) + check13_from_sum(sum13)


def tally_with_min(lo, hi, trace_positions, leaf):
    """Count bodies and keep the smallest ISBN-13 key per label in one pass.

    Returns ``{label: (count, min_isbn13)}``.  The leaf decides support and
    segmentation from the two check characters and the traced digits, so the
    memoized state stays independent of the interval length.
    """
    bounded = _bounds(lo, hi)
    if bounded is None:
        return {}
    low, high = bounded
    trace_set = set(trace_positions)
    trace_scale = {pos: 10 ** (len(trace_positions) - rank - 1)
                   for rank, pos in enumerate(sorted(trace_set))}
    place_value = [10 ** (WIDTH13 - 1 - pos) for pos in range(WIDTH13)]

    @lru_cache(maxsize=None)
    def dp(pos, tight_lo, tight_hi, sum13, sum10, trace):
        if pos == WIDTH13:
            label = leaf(check13_from_sum(sum13),
                         check10_from_sum(sum10), trace)
            return {} if label is None else {label: (1, 0)}
        d_lo = int(low[pos]) if tight_lo else 0
        d_hi = int(high[pos]) if tight_hi else 9
        totals = {}
        for digit in range(d_lo, d_hi + 1):
            nxt13, nxt10 = _next_sums(pos, digit, sum13, sum10)
            nxt_trace = trace + digit * trace_scale[pos] if pos in trace_set \
                else trace
            for label, (count, suffix) in dp(
                    pos + 1, tight_lo and digit == d_lo,
                    tight_hi and digit == d_hi, nxt13, nxt10,
                    nxt_trace).items():
                value = digit * place_value[pos] + suffix
                if label in totals:
                    old_count, old_value = totals[label]
                    totals[label] = (old_count + count,
                                     min(old_value, value))
                else:
                    totals[label] = (count, value)
        return totals

    result = {}
    for label, (count, body) in dp(0, True, True, 0, 0, 0).items():
        body_str = str(body).zfill(WIDTH13)
        result[label] = (count, body_str + check_digit13(body_str))
    return result


# ----------------------------------------------------------------------
# Single-kind convenience helpers used by point queries and direct tests.
# ----------------------------------------------------------------------
def _profile(bundles):
    kinds = {bundle.kind for bundle in bundles}
    widths = {bundle.width for bundle in bundles}
    if len(kinds) != 1 or len(widths) != 1:
        raise ValueError('all bundles in one counting region must share kind '
                         'and body width')
    return next(iter(kinds)), next(iter(widths))


def _native_interval(kind, lo, hi):
    if kind == 'isbn13':
        return lo, hi + 1
    return PREFIX_978 + lo, PREFIX_978 + hi + 1


def count_realized(bundles, lo, hi):
    """Distinct valid keys realized on ``[lo, hi]`` by same-kind bundles."""
    bundles = list(bundles)
    if not bundles:
        return 0
    kind, _width = _profile(bundles)
    chars = frozenset().union(*(bundle.check_chars for bundle in bundles))
    axis_lo, axis_hi = _native_interval(kind, lo, hi)
    if kind == 'isbn13':
        leaf = lambda c13, _c10: c13 in chars  # noqa: E731
    else:
        leaf = lambda _c13, c10: c10 in chars  # noqa: E731
    return count_matches(axis_lo, axis_hi - 1, leaf)


def min_realized(bundles, lo, hi):
    """Smallest valid ISBN-13 key realized on ``[lo, hi]``, or ``None``."""
    bundles = list(bundles)
    if not bundles:
        return None
    kind, _width = _profile(bundles)
    chars = frozenset().union(*(bundle.check_chars for bundle in bundles))
    axis_lo, axis_hi = _native_interval(kind, lo, hi)
    if kind == 'isbn13':
        leaf = lambda c13, _c10: c13 in chars  # noqa: E731
    else:
        leaf = lambda _c13, c10: c10 in chars  # noqa: E731
    return min_match(axis_lo, axis_hi - 1, leaf)
