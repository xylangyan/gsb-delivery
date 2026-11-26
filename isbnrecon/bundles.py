# -*- coding: utf-8 -*-
"""Supplement bundles and interval indexes over ISBN bodies.

A bundle is a supplier record with a unique id.  It covers a *closed* decimal
interval of equal-width bodies (the ISBN without its check digit) and a set of
paper-confirmed check characters.  A title counts at most once on a side even
when several bundles support it.

The :class:`BundleSet` keeps bundles in a segment tree over the numeric body
axis, so point coverage, overlap unions and interval intersections never
require scanning every bundle or every ISBN.  Building the tree is
``O(n log n)``; point queries and range intersections are ``O(log n + k)``
where ``k`` is the number of reported segments/bundles.
"""

from bisect import bisect_left, bisect_right


class Bundle:
    """One supplier bundle.

    Parameters
    ----------
    bundle_id:
        Unique bundle id within its side.
    kind:
        ``'isbn10'`` or ``'isbn13'``.
    body_start, body_end:
        Decimal strings of identical width: the closed interval of bodies
        (ISBN digits excluding the check character).
    check_chars:
        Iterable of single paper-confirmed check characters (digits, plus
        ``'X'`` for ISBN-10).
    """

    def __init__(self, bundle_id, kind, body_start, body_end, check_chars):
        if kind not in ('isbn10', 'isbn13'):
            raise ValueError("kind must be 'isbn10' or 'isbn13'")
        width = len(body_start)
        if width == 0 or len(body_end) != width:
            raise ValueError('body endpoints must be non-empty equal-width '
                             'decimal strings')
        if not (body_start.isdigit() and body_end.isdigit()):
            raise ValueError('bodies must be decimal strings')
        chars = frozenset(check_chars)
        if not chars or any(len(ch) != 1 for ch in chars):
            raise ValueError('check_chars must be non-empty single chars')
        allowed = '0123456789X' if kind == 'isbn10' else '0123456789'
        if any(ch not in allowed for ch in chars):
            raise ValueError('illegal check character for %s' % kind)
        lo, hi = int(body_start), int(body_end)
        if lo > hi:
            raise ValueError('body interval is empty or reversed')
        self.id = bundle_id
        self.kind = kind
        self.width = width
        self.lo = lo
        self.hi = hi
        self.body_start = body_start
        self.body_end = body_end
        self.check_chars = chars

    def __repr__(self):
        return ('Bundle(%r, %r, %r, %r, {%s})'
                % (self.id, self.kind, self.body_start, self.body_end,
                   ','.join(sorted(self.check_chars))))

    def covers_body(self, body):
        """Whether the numeric body falls in the closed interval."""
        return self.lo <= body <= self.hi

    def supports(self, kind, body, check_char):
        """Whether the bundle supplies the given printed number."""
        return (self.kind == kind and self.width == len(str(body))
                and check_char in self.check_chars
                and self.covers_body(body))


class BundleSet:
    """Mutable set of bundles with interval-indexed access.

    Bundle ids are unique.  ``add``/``remove`` touch only the affected id; the
    segment tree is rebuilt lazily, so a work order that withdraws a handful of
    bundles never re-reads or re-indexes the unrelated bundles.
    """

    def __init__(self, bundles=()):
        self._bundles = {}
        for bundle in bundles:
            self.add(bundle)

    def add(self, bundle):
        if not isinstance(bundle, Bundle):
            raise TypeError('expected Bundle instance')
        if bundle.id in self._bundles:
            raise ValueError('duplicate bundle id %r' % (bundle.id,))
        self._bundles[bundle.id] = bundle
        self._dirty = True

    def remove(self, bundle_id):
        del self._bundles[bundle_id]
        self._dirty = True

    def get(self, bundle_id):
        return self._bundles[bundle_id]

    def __contains__(self, bundle_id):
        return bundle_id in self._bundles

    def __len__(self):
        return len(self._bundles)

    def __iter__(self):
        return iter(self._bundles.values())

    def ids(self):
        return self._bundles.keys()

    def copy(self):
        clone = BundleSet()
        clone._bundles = dict(self._bundles)
        clone._dirty = True
        return clone

    # ------------------------------------------------------------------
    # Segment tree over the numeric body axis.
    # ------------------------------------------------------------------
    def _ensure_index(self):
        if getattr(self, '_dirty', True):
            self._build_index()
            self._dirty = False

    def _build_index(self):
        bundles = list(self._bundles.values())
        points = set()
        for bundle in bundles:
            points.add(bundle.lo)
            points.add(bundle.hi + 1)
        coords = sorted(points)
        # Elementary segments [coords[j], coords[j+1]) tile the covered axis.
        size = 1
        while size < max(1, len(coords) - 1):
            size *= 2
        nodes = [[] for _ in range(2 * size)]

        def place(node_lo, node_hi, node_idx, seg_lo, seg_hi, bundle):
            if seg_hi <= node_lo or node_hi <= seg_lo:
                return
            if seg_lo <= node_lo and node_hi <= seg_hi:
                nodes[node_idx].append(bundle)
                return
            mid = (node_lo + node_hi) // 2
            place(node_lo, mid, node_idx * 2, seg_lo, seg_hi, bundle)
            place(mid, node_hi, node_idx * 2 + 1, seg_lo, seg_hi, bundle)

        for bundle in bundles:
            seg_lo = bisect_left(coords, bundle.lo)
            seg_hi = bisect_left(coords, bundle.hi + 1)
            place(0, size, 1, seg_lo, seg_hi, bundle)
        self._coords = coords
        self._tree = nodes
        self._size = size

    def covering(self, body):
        """All bundles whose closed interval contains ``body``."""
        self._ensure_index()
        coords = self._coords
        if not coords or body < coords[0] or body >= coords[-1]:
            return ()
        seg = bisect_right(coords, body) - 1
        idx = seg + self._size
        found = []
        while idx:
            found.extend(self._tree[idx])
            idx //= 2
        return tuple(found)

    def intersecting(self, lo, hi):
        """All bundles whose closed interval intersects ``[lo, hi]``."""
        if lo > hi:
            return ()
        self._ensure_index()
        coords = self._coords
        if not coords or hi < coords[0] or lo > coords[-1] - 1:
            return ()
        first = max(0, bisect_right(coords, lo) - 1)
        last = min(len(coords) - 2, bisect_left(coords, hi + 1) - 1)
        if first > last:
            return ()
        found = {}

        def walk(node_lo, node_hi, idx):
            if node_hi <= first or node_lo > last:
                return
            if first <= node_lo and node_hi <= last + 1:
                for bundle in self._tree[idx]:
                    found[bundle.id] = bundle
                if node_hi - node_lo == 1:
                    return
            if node_hi - node_lo == 1:
                return
            mid = (node_lo + node_hi) // 2
            walk(node_lo, mid, idx * 2)
            walk(mid, node_hi, idx * 2 + 1)

        walk(0, self._size, 1)
        return list(found.values())

    def union_segments(self):
        """Disjoint closed intervals covered by at least one bundle.

        Intervals touching only at a boundary that no bundle actually spans
        stay separate; the merge only joins intervals that overlap on at
        least one integer body.
        """
        raw = sorted((bundle.lo, bundle.hi) for bundle in self._bundles.values())
        merged = []
        for lo, hi in raw:
            if merged and lo <= merged[-1][1]:
                if hi > merged[-1][1]:
                    merged[-1] = (merged[-1][0], hi)
            else:
                merged.append((lo, hi))
        return merged
