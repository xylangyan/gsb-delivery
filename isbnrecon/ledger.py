# -*- coding: utf-8 -*-
"""Two-sided ledger, correction snapshots and migration review.

A :class:`SideView` bundles one side's surviving supplement bundles with its
own registration table.  A :class:`Ledger` answers two questions without
storing accounts per ISBN:

* ``grid`` — exact count and lexicographically smallest key of every
  non-empty ``(old result, new result)`` cell;
* ``review`` — the migration cells produced by a correction work order, with
  the still-valid supporting bundles of each side traceable for every sample.

The 12-digit ISBN-13 body axis is partitioned at bundle boundaries and
registration group boundaries.  Inside each resulting cell the covering
bundle set of every view is constant, so support and segmentation can be
decided at the digit-DP leaf from the two check characters alone.  Rule
edges of the seven-digit reading only make at most two concrete bodies per
rule ambiguous (the reading includes the check character); those are handled
as singleton points and everything else stays a bulk-counted segment.
"""

from dataclasses import dataclass

from isbnlib._core import check_digit10, check_digit13

from .bundles import BundleSet
from .counting import PREFIX_978, WIDTH13, tally_with_min
from .results import ABSENT, UNSEGMENTED, Segmented

AXIS_LO = 978 * 10 ** 9
AXIS_HI_EXCL = 980 * 10 ** 9
LAST_BODY_POS = 11
# Stable segments below this size are evaluated point by point (they are the
# narrow publisher-prefix slices); larger segments must use the digit DP.
DIRECT_LIMIT = 100_000
# Traced publisher digits above this would blow up the DP memo, so such
# narrow slices take the point-evaluation path instead.
TRACE_LIMIT = 5


@dataclass(frozen=True)
class EvidenceRecord:
    bundle_id: object
    kind: str
    printed: str
    paper_chars: tuple


@dataclass(frozen=True)
class Evidence:
    isbn13: str
    records: tuple
    result: object


@dataclass
class CellCount:
    count: int
    min_isbn13: object


class SideView:
    """One side of a snapshot: surviving bundles plus its registry table."""

    def __init__(self, name, bundle_set, table):
        self.name = name
        self.bundle_set = bundle_set
        self.table = table

    def copy(self):
        return SideView(self.name, self.bundle_set.copy(), self.table.copy())

    def covering(self, kind, body):
        width = 12 if kind == 'isbn13' else 9
        return [bundle for bundle in self.bundle_set.covering(body)
                if bundle.kind == kind and bundle.width == width
                and bundle.covers_body(body)]

    def supports(self, kind, body):
        required = (check_digit13(str(body).zfill(12)) if kind == 'isbn13'
                    else check_digit10(str(body).zfill(9)))
        return any(required in bundle.check_chars
                   for bundle in self.covering(kind, body))

    def side_supports(self, body13):
        if self.supports('isbn13', body13):
            return True
        if AXIS_LO <= body13 < PREFIX_978 + 10 ** 9:
            return self.supports('isbn10', body13 - PREFIX_978)
        return False

    def result_for(self, body13):
        if not self.side_supports(body13):
            return ABSENT
        isbn13 = str(body13).zfill(WIDTH13) + check_digit13(
            str(body13).zfill(WIDTH13))
        return self.table.segment(isbn13)

    def evidence_for(self, body13):
        """All supporting bundles of one key with paper numbers and result."""
        body13_str = str(body13).zfill(WIDTH13)
        check13 = check_digit13(body13_str)
        records = []
        for bundle in self.covering('isbn13', body13):
            if check13 in bundle.check_chars:
                records.append(EvidenceRecord(
                    bundle.id, 'isbn13', body13_str + check13,
                    tuple(sorted(bundle.check_chars))))
        if AXIS_LO <= body13 < PREFIX_978 + 10 ** 9:
            body10 = body13 - PREFIX_978
            body10_str = str(body10).zfill(9)
            check10 = check_digit10(body10_str)
            for bundle in self.covering('isbn10', body10):
                if check10 in bundle.check_chars:
                    records.append(EvidenceRecord(
                        bundle.id, 'isbn10', body10_str + check10,
                        tuple(sorted(bundle.check_chars))))
        return Evidence(body13_str + check13, tuple(records),
                        self.result_for(body13))


class Correction:
    """One work order: bundle withdrawals/replacements and rule changes.

    Bundle operations are scoped to a side (``'old'``/``'new'``).  Rule
    changes replace every rule of a registration group on the new table.
    """

    def __init__(self, name='correction'):
        self.name = name
        self.withdraw = {'old': [], 'new': []}
        self.replace = {'old': [], 'new': []}
        self.rule_changes = []

    def withdraw_bundle(self, side, bundle_id):
        self.withdraw[side].append(bundle_id)
        return self

    def replace_bundle(self, side, bundle):
        self.replace[side].append(bundle)
        return self

    def replace_group_rules(self, reg_prefix, rules):
        self.rule_changes.append((reg_prefix, tuple(rules)))
        return self


@dataclass
class MigrationReview:
    correction: Correction
    migrations: dict
    before_grid: dict  # region-scoped pre-correction (old, new) grid
    after_grid: dict   # region-scoped post-correction (old, new) grid
    views: dict

    def evidence(self, isbn13):
        """Traceable evidence of one key on all four sides of the review."""
        canonical = str(isbn13).replace('-', '')
        body13 = int(canonical[:12])
        return {name: view.evidence_for(body13)
                for name, view in self.views.items()}


class Ledger:
    """Reconciliation ledger over old and new sides."""

    def __init__(self, old_bundles, new_bundles, old_table, new_table):
        if not isinstance(old_bundles, BundleSet):
            old_bundles = BundleSet(old_bundles)
        if not isinstance(new_bundles, BundleSet):
            new_bundles = BundleSet(new_bundles)
        self.before = (
            SideView('旧', old_bundles, old_table),
            SideView('新', new_bundles, new_table),
        )

    def snapshot(self):
        old_view, new_view = self.before
        return old_view.copy(), new_view.copy()

    def apply(self, correction):
        """Return the post-correction (old, new) side views."""
        old_view, new_view = self.snapshot()
        for side, view in (('old', old_view), ('new', new_view)):
            for bundle_id in correction.withdraw[side]:
                view.bundle_set.remove(bundle_id)
            for bundle in correction.replace[side]:
                if bundle.id in view.bundle_set.ids():
                    view.bundle_set.remove(bundle.id)
                view.bundle_set.add(bundle)
        for reg_prefix, rules in correction.rule_changes:
            new_view.table.set_group_rules(reg_prefix, rules)
        return old_view, new_view

    def affected_region(self, correction, after_views):
        """Body axis region touched by a correction (closed interval)."""
        blocks = []
        for side_idx, side in enumerate(('old', 'new')):
            touched = set(correction.withdraw[side])
            touched.update(bundle.id for bundle in correction.replace[side])
            for view in (self.before[side_idx], after_views[side_idx]):
                for bundle_id in touched:
                    if bundle_id in view.bundle_set:
                        blocks.append(native_axis(
                            view.bundle_set.get(bundle_id)))
        for reg_prefix, _rules in correction.rule_changes:
            block = group_block(reg_prefix)
            if block is not None:
                blocks.append(block)
        if not blocks:
            return None
        return min(lo for lo, _hi in blocks), max(hi for _lo, hi in blocks)

    def grid(self, views=None, region=None):
        """Map ``(old result, new result)`` -> :class:`CellCount`."""
        if views is None:
            views = self.before
        return classify_region(views, region)

    def review(self, correction):
        """Apply a correction and return the migration review.

        The review only touches the affected axis region: bundles unrelated to
        the withdrawal/replacement and rule change are never re-read.
        """
        after_views = self.apply(correction)
        region = self.affected_region(correction, after_views)
        if region is None:
            return MigrationReview(correction, {}, {}, {}, {
                'before_old': self.before[0],
                'before_new': self.before[1],
                'after_old': after_views[0],
                'after_new': after_views[1],
            })
        four_views = (self.before[0], self.before[1],
                      after_views[0], after_views[1])
        classified = classify_region(four_views, region)
        moved = {}
        for label4, cell in classified.items():
            before_pair = label4[0:2]
            after_pair = label4[2:4]
            if before_pair != after_pair:
                moved[(before_pair, after_pair)] = cell
        before_grid = classify_region(
            (self.before[0], self.before[1]), region)
        after_grid = classify_region(
            (after_views[0], after_views[1]), region)
        views = {
            'before_old': self.before[0],
            'before_new': self.before[1],
            'after_old': after_views[0],
            'after_new': after_views[1],
        }
        return MigrationReview(correction, moved, before_grid, after_grid,
                               views)


# ----------------------------------------------------------------------
# Coordinate helpers
# ----------------------------------------------------------------------
def native_axis(bundle):
    if bundle.kind == 'isbn13':
        return bundle.lo, bundle.hi
    return PREFIX_978 + bundle.lo, PREFIX_978 + bundle.hi


def group_block(reg_prefix):
    """Full 12-digit body block of a registration group key."""
    parts = reg_prefix.split('-')
    if len(parts) != 2 or parts[0] not in ('978', '979'):
        return None
    digits = parts[0] + parts[1]
    cur = len(digits) - 1
    shift = WIDTH13 - cur - 1
    base = int(digits) * 10 ** shift
    return base, base + 10 ** shift - 1


def _event_points(views, region_lo, region_hi):
    points = {region_lo, region_hi + 1}
    for view in views:
        for bundle in view.bundle_set:
            lo, hi = native_axis(bundle)
            points.add(lo)
            points.add(hi + 1)
        for reg_prefix in view.table.groups():
            block = group_block(reg_prefix)
            if block is not None:
                points.add(block[0])
                points.add(block[1] + 1)
    return sorted(p for p in points if region_lo <= p <= region_hi + 1)


# ----------------------------------------------------------------------
# Rule edge geometry on the tail of a group block
# ----------------------------------------------------------------------
def _rule_geometry(view, group_key, cur, cell_lo, cell_hi):
    """Stable-segment events and ambiguous bodies for one rule view.

    Returns ``(events, ambiguous)`` in body coordinates.  A body is ambiguous
    when its seven-digit reading can fall on either side of a rule edge
    depending on the check character; these are exactly the two tails
    neighbouring a rule's guaranteed-containment interval.
    """
    rules = view.table._ranges[group_key]
    block_lo, _block_hi = group_block(group_key)
    tail_count = LAST_BODY_POS - cur
    events = {cell_lo, cell_hi + 1}
    ambiguous = set()

    def to_body(tail):
        return block_lo + tail

    if tail_count >= 7:
        # The check character drops out of the seven-digit reading.
        shrink = 10 ** (tail_count - 7)
        for lo7, hi7, _length in rules:
            for tail in (lo7 * shrink, (hi7 + 1) * shrink):
                body = to_body(tail)
                if cell_lo <= body <= cell_hi + 1:
                    events.add(body)
        return events, ambiguous

    pad = 6 - tail_count
    unit = 10 ** (pad + 1)
    slack = 9 * 10 ** pad
    for lo7, hi7, _length in rules:
        inner_lo = _ceil_div(lo7, unit)
        inner_hi = (hi7 - slack) // unit
        for tail in (inner_lo, inner_hi + 1):
            body = to_body(tail)
            if cell_lo <= body <= cell_hi + 1:
                events.add(body)
        for tail in (inner_lo - 1, inner_hi + 1):
            body = to_body(tail)
            if cell_lo <= body <= cell_hi:
                ambiguous.add(body)
    return events, ambiguous


def _ceil_div(a, b):
    return -((-a) // b)


# ----------------------------------------------------------------------
# Region classification
# ----------------------------------------------------------------------
def classify_region(views, region=None):
    if region is None:
        region = (AXIS_LO, AXIS_HI_EXCL - 1)
    region_lo, region_hi = region
    points = _event_points(views, region_lo, region_hi)
    grid = {}
    for idx in range(len(points) - 1):
        cell_lo, cell_hi = points[idx], points[idx + 1] - 1
        if cell_lo <= cell_hi:
            _classify_cell(views, cell_lo, cell_hi, grid)
    return grid


def _view_plan(view, representative):
    """Constant per-cell plan for one view.

    Returns ``(cover13_chars, cover10_chars, group_plan)`` where group_plan is
    ``None`` (no registered group here) or ``(group_key, cur)``.
    """
    cover13 = [frozenset(b.check_chars)
               for b in view.covering('isbn13', representative)]
    cover10 = [frozenset(b.check_chars)
               for b in view.covering('isbn10',
                                      representative - PREFIX_978)]
    group_plan = None
    if cover13 or cover10:
        located = view.table._locate_group(
            str(representative).zfill(WIDTH13))
        if located is not None:
            group_plan = located
    return cover13, cover10, group_plan


def _classify_cell(views, cell_lo, cell_hi, grid):
    plans = [_view_plan(view, cell_lo) for view in views]
    if not any(plan[0] or plan[1] for plan in plans):
        return

    events = {cell_lo, cell_hi + 1}
    ambiguous = set()
    for view, plan in zip(views, plans):
        group_plan = plan[2]
        if group_plan is None:
            continue
        group_key, cur = group_plan
        view_events, view_ambiguous = _rule_geometry(
            view, group_key, cur, cell_lo, cell_hi)
        events.update(view_events)
        ambiguous.update(view_ambiguous)
    events = sorted(events)

    for idx in range(len(events) - 1):
        seg_lo, seg_hi = events[idx], events[idx + 1] - 1
        if seg_lo > seg_hi:
            continue
        segment_ambiguous = sorted(
            body for body in ambiguous if seg_lo <= body <= seg_hi)
        cursor = seg_lo
        for body in segment_ambiguous:
            if cursor < body:
                _stable_segment(views, plans, cursor, body - 1, grid)
            label = tuple(view.result_for(body) for view in views)
            _emit(grid, label, 1, _key_of(body))
            cursor = body + 1
        if cursor <= seg_hi:
            _stable_segment(views, plans, cursor, seg_hi, grid)


def _stable_segment(views, plans, seg_lo, seg_hi, grid):
    sample = seg_lo
    trace_positions = set()
    view_specs = []
    for view, plan in zip(views, plans):
        cover13, cover10, group_plan = plan
        if group_plan is None:
            view_specs.append(('plain', cover13, cover10, None, None))
            continue
        group_key, cur = group_plan
        sample_result = view.result_for(sample)
        if not isinstance(sample_result, Segmented):
            view_specs.append(('plain', cover13, cover10, group_key, cur))
            continue
        length = len(sample_result.publisher)
        for pos in range(cur + 1, cur + 1 + length):
            trace_positions.add(pos)
        view_specs.append(('segmented', cover13, cover10,
                           (group_key, cur), length))
    trace_positions = tuple(sorted(trace_positions))
    segment_size = seg_hi - seg_lo + 1

    # Narrow slices (or slices whose publisher window is too wide to trace)
    # are evaluated directly: stable rule regions keep every body on the same
    # publisher length, and the point count stays bounded by DIRECT_LIMIT.
    if segment_size <= DIRECT_LIMIT or len(trace_positions) > TRACE_LIMIT:
        for body in range(seg_lo, seg_hi + 1):
            label = tuple(view.result_for(body) for view in views)
            _emit(grid, label, 1, _key_of(body))
        return

    def leaf(c13, c10, trace):
        label = []
        for kind, cover13, cover10, group_info, length in view_specs:
            supported = (any(c13 in chars for chars in cover13)
                         or any(c10 in chars for chars in cover10))
            if not supported:
                label.append(ABSENT)
            elif group_info is None or kind == 'plain':
                label.append(UNSEGMENTED)
            else:
                group_key, cur = group_info
                publisher = ''.join(
                    str(_trace_digit(trace, trace_positions, pos))
                    for pos in range(cur + 1, cur + 1 + length))
                label.append(Segmented(group_key,
                                       group_key.split('-')[1], publisher))
        label = tuple(label)
        return None if all(item is ABSENT for item in label) else label

    for label, (count, min_key) in tally_with_min(
            seg_lo, seg_hi, trace_positions, leaf).items():
        _emit(grid, label, count, min_key)


def _trace_digit(trace, trace_positions, pos):
    rank = trace_positions.index(pos)
    scale = 10 ** (len(trace_positions) - rank - 1)
    return (trace // scale) % 10


def _key_of(body):
    body_str = str(body).zfill(WIDTH13)
    return body_str + check_digit13(body_str)


def _emit(grid, label, count, min_key):
    """Record a label, dropping books absent on every side."""
    if label is not None and not all(item is ABSENT for item in label):
        _add(grid, label, count, min_key)


def _add(grid, label, count, min_key):
    if count <= 0:
        return
    existing = grid.get(label)
    if existing is None:
        grid[label] = CellCount(count, min_key)
        return
    existing.count += count
    if min_key is not None and (
            existing.min_isbn13 is None or min_key < existing.min_isbn13):
        existing.min_isbn13 = min_key


def migrations(review):
    """Migration cells of a review as ``(before_pair, after_pair)`` counts."""
    return review.migrations


def cell_report(grid, old_result, new_result):
    """The :class:`CellCount` of one grid cell, or ``None`` when empty."""
    return grid.get((old_result, new_result))
