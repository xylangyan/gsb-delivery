"""Formula reconciliation entry point for two revisions of an XLSB workbook.

The workbook itself never calls an external calculation engine: only cached
values are read by :class:`pyxlsb.Worksheet.rows`, while this module compares
the stored formula definitions.

Supported formula subset (anything else is reported as pending, never claimed
equivalent):

* numeric/boolean/string constants
* arithmetic operators and parentheses
* same-sheet single cell and area references, absolute or relative
* ``SUM`` with any number of cell/area arguments

Layout moves are described by non-overlapping segments mapping raw cells of
each workbook to shared business coordinates.
"""

from collections import namedtuple, OrderedDict

from . import formula as fm
from .worksheet import (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER,
                        KIND_REGULAR, KIND_SHARED_MASTER, KIND_SHARED_MEMBER)

Rect = namedtuple('Rect', ['r1', 'c1', 'r2', 'c2'])
Segment = namedtuple('Segment', ['sheet', 'raw', 'business'])
Window = namedtuple('Window', ['sheet', 'rect'])

EQUIVALENT = 'equivalent'
DIFFERENT = 'different'
PENDING = 'pending'


class Spec(namedtuple('Spec', ['window', 'old_segments', 'new_segments'])):
  """Reconciliation specification.

  ``window`` is a :class:`Window` in business coordinates. ``old_segments`` /
  ``new_segments`` are lists of :class:`Segment` for the respective workbook;
  segments must be non-overlapping and each raw/business rectangle pair must
  have identical dimensions.
  """


def make_spec(window_sheet, window_rect, old_segments, new_segments):
  segments = {'old': [], 'new': []}
  for label, raw_list in (('old', old_segments), ('new', new_segments)):
    for sheet, raw_rect, biz_rect in raw_list:
      _check_same_shape(raw_rect, biz_rect)
      segments[label].append(Segment(sheet, Rect(*raw_rect), Rect(*biz_rect)))
    _check_non_overlapping(segments[label], lambda seg: seg.raw, 'raw')
    _check_non_overlapping(segments[label], lambda seg: seg.business, 'business')
  return Spec(Window(window_sheet, Rect(*window_rect)),
              segments['old'], segments['new'])


def _check_same_shape(a, b):
  if (a[2] - a[0], a[3] - a[1]) != (b[2] - b[0], b[3] - b[1]):
    raise ValueError('segment raw/business rectangles must share dimensions')


def _check_non_overlapping(segments, selector, side):
  for i in range(len(segments)):
    for j in range(i + 1, len(segments)):
      if side == 'raw' and segments[i].sheet != segments[j].sheet:
        continue
      if _intersect(selector(segments[i]), selector(segments[j])) is not None:
        raise ValueError(
          '{} segments overlap (sheet {})'.format(side, segments[i].sheet))


def _intersect(a, b):
  r1 = max(a.r1, b.r1)
  c1 = max(a.c1, b.c1)
  r2 = min(a.r2, b.r2)
  c2 = min(a.c2, b.c2)
  if r1 <= r2 and c1 <= c2:
    return Rect(r1, c1, r2, c2)
  return None


def _rect_subtract(rect, hole):
  """Partition rect minus hole into at most four disjoint rectangles."""
  if _intersect(rect, hole) is None:
    return [rect]
  pieces = []
  if rect.r1 < hole.r1:
    pieces.append(Rect(rect.r1, rect.c1, hole.r1 - 1, rect.c2))
  if hole.r2 < rect.r2:
    pieces.append(Rect(hole.r2 + 1, rect.c1, rect.r2, rect.c2))
  if rect.c1 < hole.c1:
    pieces.append(Rect(max(rect.r1, hole.r1), rect.c1,
                       min(rect.r2, hole.r2), hole.c1 - 1))
  if hole.c2 < rect.c2:
    pieces.append(Rect(max(rect.r1, hole.r1), hole.c2 + 1,
                       min(rect.r2, hole.r2), rect.c2))
  return [p for p in pieces if p.r1 <= p.r2 and p.c1 <= p.c2]


class SegmentMap(object):
  """Raw <-> business coordinate lookup over one workbook's segments."""

  def __init__(self, segments):
    self._segments = segments

  def raw_for_business(self, row, col):
    point = Rect(row, col, row, col)
    for seg in self._segments:
      hit = _intersect(point, seg.business)
      if hit is not None:
        return seg.sheet, seg.raw.r1 + (row - seg.business.r1), \
            seg.raw.c1 + (col - seg.business.c1)
    return None

  def business_rects(self, sheet, raw_rect):
    """Map a raw rectangle to business rectangle fragments.

    Returns ``(covered, uncovered)`` lists of :class:`Rect`. Rectangles are
    split geometrically against the segments; cells are never enumerated, so
    full-column areas and large blocks stay compact. Segments are pre-scoped to
    the window's business sheet, so each workbook's own raw sheet name is kept
    on the segment rather than matched against the business name.
    """
    covered = []
    uncovered = [raw_rect]
    for seg in self._segments:
      if seg.sheet != sheet:
        continue
      hit = _intersect(raw_rect, seg.raw)
      if hit is None:
        continue
      biz = Rect(
        seg.business.r1 + (hit.r1 - seg.raw.r1),
        seg.business.c1 + (hit.c1 - seg.raw.c1),
        seg.business.r1 + (hit.r2 - seg.raw.r1),
        seg.business.c1 + (hit.c2 - seg.raw.c1))
      covered.append(biz)
      next_uncovered = []
      for piece in uncovered:
        next_uncovered.extend(_rect_subtract(piece, hit))
      uncovered = next_uncovered
    return _merge_rects(covered), uncovered


def _merge_rects(rects):
  """Merge rectangles that share an edge and span the same perpendicular strip."""
  merged = list(rects)
  changed = True
  while changed:
    changed = False
    for i in range(len(merged)):
      for j in range(i + 1, len(merged)):
        a, b = merged[i], merged[j]
        combined = None
        if a.c1 == b.c1 and a.c2 == b.c2 and a.r2 + 1 == b.r1:
          combined = Rect(a.r1, a.c1, b.r2, a.c2)
        elif a.c1 == b.c1 and a.c2 == b.c2 and b.r2 + 1 == a.r1:
          combined = Rect(b.r1, b.c1, a.r2, a.c2)
        elif a.r1 == b.r1 and a.r2 == b.r2 and a.c2 + 1 == b.c1:
          combined = Rect(a.r1, a.c1, a.r2, b.c2)
        elif a.r1 == b.r1 and a.r2 == b.r2 and b.c2 + 1 == a.c1:
          combined = Rect(a.r1, b.c1, a.r2, a.c2)
        if combined is not None:
          merged = [r for k, r in enumerate(merged) if k not in (i, j)]
          merged.append(combined)
          changed = True
          break
      if changed:
        break
  return sorted(merged)


def weighted_rects(rect_terms, rows=None, cols=None):
  """Compress rectangles (with repetition) into disjoint weighted rectangles.

  Coordinate compression keeps whole-column and large-block inputs compact:
  the grid is bounded by the number of term boundaries, never by cell count,
  while overlap multiplicity (a cell summed twice) is preserved exactly.
  Passing explicit shared ``rows``/``cols`` grids makes two independently
  built term lists directly comparable.
  """
  if not rect_terms:
    return {}
  if rows is None:
    rows = sorted({r for rect in rect_terms for r in (rect.r1, rect.r2 + 1)})
  if cols is None:
    cols = sorted({c for rect in rect_terms for c in (rect.c1, rect.c2 + 1)})
  weights = {}
  for rect in rect_terms:
    for i in range(_bisect(rows, rect.r1), _bisect(rows, rect.r2 + 1)):
      for j in range(_bisect(cols, rect.c1), _bisect(cols, rect.c2 + 1)):
        box = Rect(rows[i], cols[j], rows[i + 1] - 1, cols[j + 1] - 1)
        weights[box] = weights.get(box, 0) + 1
  return weights


def weighted_rects_equal(left_terms, right_terms):
  """Compare rectangle multisets on one shared compression grid."""
  rows = sorted({r for rect in left_terms + right_terms
                 for r in (rect.r1, rect.r2 + 1)})
  cols = sorted({c for rect in left_terms + right_terms
                 for c in (rect.c1, rect.c2 + 1)})
  return weighted_rects(left_terms, rows, cols) == \
      weighted_rects(right_terms, rows, cols)


def _bisect(values, target):
  lo, hi = 0, len(values)
  while lo < hi:
    mid = (lo + hi) // 2
    if values[mid] < target:
      lo = mid + 1
    else:
      hi = mid
  return lo


class PendingReason(namedtuple('PendingReason', ['kind', 'detail', 'rects'])):
  __slots__ = ()


class CellReport(namedtuple('CellReport', [
    'business', 'status', 'old_location', 'new_location',
    'old_definition', 'new_definition', 'detail', 'pending'])):
  __slots__ = ()


class Definition(namedtuple('Definition', ['kind', 'source_cell', 'text', 'group_rect'])):
  __slots__ = ()


class Reconciler(object):
  """Compares formula cells of two open workbooks over one business window."""

  def __init__(self, old_workbook, new_workbook, spec):
    self.old_wb = old_workbook
    self.new_wb = new_workbook
    self.spec = spec
    self.old_map = SegmentMap(spec.old_segments)
    self.new_map = SegmentMap(spec.new_segments)
    self._sheets = {'old': {}, 'new': {}}

  def run(self):
    reports = []
    win = self.spec.window.rect
    handled_arrays = set()
    for row in range(win.r1, win.r2 + 1):
      for col in range(win.c1, win.c2 + 1):
        old_loc = self.old_map.raw_for_business(row, col)
        new_loc = self.new_map.raw_for_business(row, col)
        report = self._compare_cell((row, col), old_loc, new_loc, handled_arrays)
        if report is not None:
          reports.append(report)
    return Report(self.spec, reports)

  def _compare_cell(self, business, old_loc, new_loc, handled_arrays):
    try:
      old_view = self._view('old', old_loc) if old_loc else None
      new_view = self._view('new', new_loc) if new_loc else None
    except fm.UnsupportedFormula as exc:
      return self._pending(business, old_loc, new_loc, None, None,
                           PendingReason('unsupported-formula', exc.reason, []))
    except fm.FormulaParseError as exc:
      return self._pending(business, old_loc, new_loc, None, None,
                           PendingReason('malformed-formula', str(exc), []))
    if old_view is None and new_view is None:
      return None
    if old_view is None or new_view is None:
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           PendingReason('uncovered-cell',
                                         'business cell exists on only one side', []))
    if isinstance(old_view, NonFormulaView) and isinstance(new_view, NonFormulaView):
      return None
    if isinstance(old_view, NonFormulaView) or isinstance(new_view, NonFormulaView):
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           PendingReason('cell-kind',
                                         'one side holds a constant/blank cell instead '
                                         'of a formula cell', []))
    if old_view.kind in (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER) or \
        new_view.kind in (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER):
      return self._compare_array(business, old_loc, new_loc,
                                 old_view, new_view, handled_arrays)
    if old_view.kind in (KIND_SHARED_MASTER, KIND_SHARED_MEMBER, KIND_REGULAR) and \
        new_view.kind in (KIND_SHARED_MASTER, KIND_SHARED_MEMBER, KIND_REGULAR):
      return self._compare_regular(business, old_loc, new_loc, old_view, new_view)
    return self._pending(business, old_loc, new_loc, old_view, new_view,
                         PendingReason('cell-kind',
                                       'formula status differs between versions', []))

  def _view(self, label, location):
    sheet, row, col = location
    wb = self.old_wb if label == 'old' else self.new_wb
    ws = self._sheet(label, wb, sheet)
    index = ws.formula_index()
    if (row, col) not in index.all_cells:
      return None
    materialized = ws.materialize_formula(row, col)
    if materialized is None:
      return NonFormulaView(label, sheet, (row, col))
    kind, group, ast = materialized
    seg_map = self.old_map if label == 'old' else self.new_map
    return FormulaView(label, sheet, (row, col), kind, group, ast, seg_map)

  def _sheet(self, label, wb, sheet):
    cache = self._sheets[label]
    if sheet not in cache:
      cache[sheet] = wb.get_sheet(sheet)
    return cache[sheet]

  def close(self):
    for cache in self._sheets.values():
      for ws in cache.values():
        ws.close()

  def _compare_regular(self, business, old_loc, new_loc, old_view, new_view):
    old_pending = old_view.uncovered_references()
    new_pending = new_view.uncovered_references()
    if old_pending or new_pending:
      reasons = old_pending + new_pending
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           reasons[0] if len(reasons) == 1 else
                           PendingReason('uncovered-reference',
                                         '{} uncovered reference fragments'
                                         .format(len(reasons)),
                                         [rect for reason in reasons for rect in reason.rects]))
    try:
      equivalent, detail = _ast_equivalent(old_view.mapped_ast(),
                                           new_view.mapped_ast())
    except fm.UnsupportedFormula as exc:
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           PendingReason('unsupported-formula', exc.reason, []))
    status = EQUIVALENT if equivalent else DIFFERENT
    return CellReport(business, status, old_loc, new_loc,
                      old_view.definition(), new_view.definition(),
                      None if equivalent else detail, None)

  def _compare_array(self, business, old_loc, new_loc, old_view, new_view,
                     handled_arrays):
    old_group = old_view.group if old_view and old_view.group else None
    new_group = new_view.group if new_view and new_view.group else None
    key = (None if old_group is None else (old_group.r, old_group.c),
           None if new_group is None else (new_group.r, new_group.c))
    if key in handled_arrays:
      return None
    handled_arrays.add(key)
    if old_group is None or new_group is None or \
        old_view.kind not in (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER) or \
        new_view.kind not in (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER):
      return CellReport(business, DIFFERENT, old_loc, new_loc,
                        old_view.definition() if old_view else None,
                        new_view.definition() if new_view else None,
                        'array formula replaced by per-cell calculation; '
                        'array output members: {}'.format(
                          self._array_members(old_view, new_view)), None)
    old_region, old_uncovered = old_view.seg_map.business_rects(
      old_view.sheet, Rect(old_group.r, old_group.c,
                           old_group.r + old_group.h - 1,
                           old_group.c + old_group.w - 1))
    new_region, new_uncovered = new_view.seg_map.business_rects(
      new_view.sheet, Rect(new_group.r, new_group.c,
                           new_group.r + new_group.h - 1,
                           new_group.c + new_group.w - 1))
    if old_uncovered or new_uncovered:
      rects = old_uncovered + new_uncovered
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           PendingReason('uncovered-reference',
                                         'array output region crosses segment coverage',
                                         rects))
    if not weighted_rects_equal(old_region, new_region):
      return CellReport(business, DIFFERENT, old_loc, new_loc,
                        old_view.definition(), new_view.definition(),
                        'array output members differ: {} vs {}'.format(
                          [_rect_a1(r) for r in old_region],
                          [_rect_a1(r) for r in new_region]), None)
    try:
      equivalent, detail = _ast_equivalent(old_view.mapped_ast(),
                                           new_view.mapped_ast())
    except fm.UnsupportedFormula as exc:
      return self._pending(business, old_loc, new_loc, old_view, new_view,
                           PendingReason('unsupported-formula', exc.reason, []))
    if not equivalent:
      return CellReport(business, DIFFERENT, old_loc, new_loc,
                        old_view.definition(), new_view.definition(), detail, None)
    return CellReport(business, EQUIVALENT, old_loc, new_loc,
                      old_view.definition(), new_view.definition(), None, None)

  def _array_members(self, old_view, new_view):
    view = old_view if old_view is not None and old_view.group is not None else new_view
    if view is None or view.group is None:
      return '[]'
    region, _ = view.seg_map.business_rects(
      view.sheet, Rect(view.group.r, view.group.c,
                       view.group.r + view.group.h - 1,
                       view.group.c + view.group.w - 1))
    return '[{}]'.format(', '.join(_rect_a1(rect) for rect in region))

  def _pending(self, business, old_loc, new_loc, old_view, new_view, reason):
    return CellReport(business, PENDING, old_loc, new_loc,
                      old_view.definition() if old_view else None,
                      new_view.definition() if new_view else None,
                      None, reason)


class NonFormulaView(object):
  """A cell covered by the segment list that holds no formula."""
  __slots__ = ('label', 'sheet', 'location')

  def __init__(self, label, sheet, location):
    self.label = label
    self.sheet = sheet
    self.location = location

  def definition(self):
    return None


class FormulaView(object):
  """One formula cell materialized against its own workbook coordinates."""

  def __init__(self, label, sheet, location, kind, group, ast, seg_map):
    self.label = label
    self.sheet = sheet
    self.location = location
    self.kind = kind
    self.group = group
    self.ast = ast
    self.seg_map = seg_map
  def mapped_ast(self):
    return self._map_node(self.ast)

  def _map_node(self, node):
    if isinstance(node, fm.Ref):
      rects, uncovered = self.seg_map.business_rects(
        self.sheet, Rect(node.row, node.col, node.row, node.col))
      if uncovered:
        raise fm.UnsupportedFormula('reference leaves segment coverage')
      return MappedRef(rects[0], node.row_abs, node.col_abs)
    if isinstance(node, fm.Area):
      rects, uncovered = self.seg_map.business_rects(
        self.sheet, Rect(node.r1, node.c1, node.r2, node.c2))
      if uncovered:
        raise fm.UnsupportedFormula('area leaves segment coverage')
      return MappedArea(tuple(rects))
    if isinstance(node, fm.BinOp):
      return MappedBinOp(node.op, self._map_node(node.left),
                         self._map_node(node.right))
    if isinstance(node, fm.UnaryOp):
      return MappedUnaryOp(node.op, self._map_node(node.operand))
    if isinstance(node, fm.Paren):
      return self._map_node(node.operand)
    if isinstance(node, fm.FuncCall):
      return MappedFuncCall(node.name, [self._map_node(arg) for arg in node.args])
    if isinstance(node, fm.Const):
      return MappedConst(node.value)
    if isinstance(node, fm.MissingArg):
      return MappedMissing()
    raise fm.UnsupportedFormula('unsupported AST node {}'.format(type(node).__name__))

  def uncovered_references(self):
    reasons = []
    for ref in fm.iter_refs(self.ast):
      if isinstance(ref, fm.Ref):
        rect = Rect(ref.row, ref.col, ref.row, ref.col)
      else:
        rect = Rect(ref.r1, ref.c1, ref.r2, ref.c2)
      _, uncovered = self.seg_map.business_rects(self.sheet, rect)
      if uncovered:
        reasons.append(PendingReason(
          'uncovered-reference',
          '{}!{} is not covered by the segment list'.format(
            self.sheet, _rect_a1(rect)),
          uncovered))
    return reasons

  def definition(self):
    if self.kind in (KIND_ARRAY_MASTER, KIND_ARRAY_MEMBER,
                     KIND_SHARED_MASTER, KIND_SHARED_MEMBER):
      group = self.group
      source = (group.r, group.c)
      group_rect = Rect(group.r, group.c, group.r + group.h - 1,
                        group.c + group.w - 1)
    else:
      source = self.location
      group_rect = None
    return Definition(self.kind, source, fm.render_a1(self.ast), group_rect)


class MappedConst(namedtuple('MappedConst', ['value'])):
  __slots__ = ()


class MappedMissing(namedtuple('MappedMissing', [])):
  __slots__ = ()


class MappedRef(namedtuple('MappedRef', ['rect', 'row_abs', 'col_abs'])):
  __slots__ = ()


class MappedArea(namedtuple('MappedArea', ['rects'])):
  __slots__ = ()


class MappedBinOp(namedtuple('MappedBinOp', ['op', 'left', 'right'])):
  __slots__ = ()


class MappedUnaryOp(namedtuple('MappedUnaryOp', ['op', 'operand'])):
  __slots__ = ()


class MappedFuncCall(namedtuple('MappedFuncCall', ['name', 'args'])):
  __slots__ = ()


def _ast_equivalent(left, right):
  if isinstance(left, MappedConst) and isinstance(right, MappedConst):
    if isinstance(left.value, bool) or isinstance(right.value, bool):
      if not (isinstance(left.value, bool) and isinstance(right.value, bool)) \
          or left.value != right.value:
        return False, 'constant differs: {!r} vs {!r}'.format(
          left.value, right.value)
      return True, None
    if isinstance(left.value, str) or isinstance(right.value, str):
      if left.value != right.value:
        return False, 'constant differs: {!r} vs {!r}'.format(
          left.value, right.value)
      return True, None
    if not isinstance(left.value, (int, float)) or \
        not isinstance(right.value, (int, float)) or \
        left.value != right.value:
      return False, 'constant type/value differs: {!r} vs {!r}'.format(
        left.value, right.value)
    return True, None
  if isinstance(left, MappedMissing) and isinstance(right, MappedMissing):
    return True, None
  if isinstance(left, MappedRef) and isinstance(right, MappedRef):
    if left.rect != right.rect:
      return False, 'reference differs: {} vs {}'.format(
        _rect_a1(left.rect), _rect_a1(right.rect))
    return True, None
  if isinstance(left, MappedArea) and isinstance(right, MappedArea):
    if not weighted_rects_equal(list(left.rects), list(right.rects)):
      return False, 'area members differ: {} vs {}'.format(
        [_rect_a1(r) for r in left.rects], [_rect_a1(r) for r in right.rects])
    return True, None
  if isinstance(left, MappedUnaryOp) and isinstance(right, MappedUnaryOp):
    if left.op != right.op:
      return False, 'unary operator differs: {} vs {}'.format(left.op, right.op)
    return _ast_equivalent(left.operand, right.operand)
  if isinstance(left, MappedBinOp) and isinstance(right, MappedBinOp):
    return _binop_equivalent(left, right)
  if isinstance(left, MappedFuncCall) and isinstance(right, MappedFuncCall):
    return _func_equivalent(left, right)
  return False, 'formula structure differs: {} vs {}'.format(
    _node_name(left), _node_name(right))


def _binop_equivalent(left, right):
  if left.op != right.op:
    return False, 'operator differs: {} vs {}'.format(left.op, right.op)
  if left.op in ('+', '*'):
    left_terms = _flatten(left, left.op)
    right_terms = _flatten(right, right.op)
    if len(left_terms) != len(right_terms):
      return False, 'operand count differs for {}'.format(left.op)
    used = [False] * len(right_terms)
    for term in left_terms:
      for idx, candidate in enumerate(right_terms):
        if not used[idx]:
          ok, _ = _ast_equivalent(term, candidate)
          if ok:
            used[idx] = True
            break
      else:
        return False, 'operand {} has no matching term'.format(_describe(term))
    return True, None
  ok, detail = _ast_equivalent(left.left, right.left)
  if not ok:
    return False, detail
  return _ast_equivalent(left.right, right.right)


def _flatten(node, op):
  terms = []
  if isinstance(node, MappedBinOp) and node.op == op:
    terms.extend(_flatten(node.left, op))
    terms.extend(_flatten(node.right, op))
  else:
    terms.append(node)
  return terms


def _func_equivalent(left, right):
  if left.name != right.name:
    return False, 'function differs: {} vs {}'.format(left.name, right.name)
  if left.name == 'SUM':
    return _sum_equivalent(left, right)
  raise fm.UnsupportedFormula('function {} is outside the supported subset'.format(left.name))


def _sum_equivalent(left, right):
  left_rects, left_other = _split_sum_terms(left)
  right_rects, right_other = _split_sum_terms(right)
  if not weighted_rects_equal(left_rects, right_rects):
    return False, 'SUM input cells differ (multiplicity preserved): {} vs {}'.format(
      [_rect_a1(r) for r in left_rects], [_rect_a1(r) for r in right_rects])
  if len(left_other) != len(right_other):
    return False, 'SUM non-reference argument count differs'
  for idx, (a, b) in enumerate(zip(left_other, right_other)):
    ok, detail = _ast_equivalent(a, b)
    if not ok:
      return False, 'SUM argument {} differs: {}'.format(idx + 1, detail)
  return True, None


def _split_sum_terms(call):
  rects = []
  other = []
  for arg in call.args:
    if isinstance(arg, MappedRef):
      rects.append(arg.rect)
    elif isinstance(arg, MappedArea):
      rects.extend(arg.rects)
    else:
      other.append(arg)
  return rects, other


def _node_name(node):
  if isinstance(node, MappedConst):
    return 'constant {!r}'.format(node.value)
  if isinstance(node, MappedRef):
    return 'reference {}'.format(_rect_a1(node.rect))
  if isinstance(node, MappedArea):
    return 'area {}'.format(','.join(_rect_a1(r) for r in node.rects))
  if isinstance(node, MappedBinOp):
    return 'arithmetic expression ({})'.format(node.op)
  if isinstance(node, MappedUnaryOp):
    return 'unary expression ({})'.format(node.op)
  if isinstance(node, MappedFuncCall):
    return 'function {}'.format(node.name)
  if isinstance(node, MappedMissing):
    return 'missing argument'
  return type(node).__name__.lower()


def _describe(node):
  return _node_name(node)


def _rect_a1(rect):
  return '{}{}:{}{}'.format(_col_letters(rect.c1), rect.r1 + 1,
                            _col_letters(rect.c2), rect.r2 + 1)


def _col_letters(col):
  letters = ''
  col += 1
  while col:
    col, rem = divmod(col - 1, 26)
    letters = chr(65 + rem) + letters
  return letters


class Report(object):
  def __init__(self, spec, reports):
    self.spec = spec
    self.reports = reports

  @property
  def equivalent(self):
    return [r for r in self.reports if r.status == EQUIVALENT]

  @property
  def different(self):
    return [r for r in self.reports if r.status == DIFFERENT]

  @property
  def pending(self):
    return [r for r in self.reports if r.status == PENDING]

  @property
  def summary(self):
    return {
      'equivalent': len(self.equivalent),
      'different': len(self.different),
      'pending': len(self.pending)}

  def first_difference(self):
    return self.different[0] if self.different else None

  def to_dict(self):
    return {
      'window': self.spec.window._asdict(),
      'summary': {
        'equivalent': len(self.equivalent),
        'different': len(self.different),
        'pending': len(self.pending)},
      'different': [_report_dict(r) for r in self.different],
      'pending': [_report_dict(r) for r in self.pending]}


def _report_dict(report):
  data = OrderedDict()
  data['business_cell'] = _cell_a1(report.business[0], report.business[1])
  data['status'] = report.status
  data['old_cell'] = _location_a1(report.old_location)
  data['new_cell'] = _location_a1(report.new_location)
  if report.old_definition is not None:
    data['old_definition'] = _definition_dict(report.old_definition)
  if report.new_definition is not None:
    data['new_definition'] = _definition_dict(report.new_definition)
  if report.detail is not None:
    data['detail'] = report.detail
  if report.pending is not None:
    data['pending_reason'] = report.pending.kind
    data['pending_detail'] = report.pending.detail
    if report.pending.rects:
      data['pending_rects'] = [_rect_a1(r) for r in report.pending.rects]
  return data


def _definition_dict(definition):
  data = OrderedDict()
  data['kind'] = definition.kind
  data['source_cell'] = _cell_a1(definition.source_cell[0],
                                 definition.source_cell[1])
  data['formula'] = definition.text
  if definition.group_rect is not None:
    data['group_rect'] = _rect_a1(definition.group_rect)
  return data


def _cell_a1(row, col):
  return '{}{}'.format(_col_letters(col), row + 1)


def _location_a1(location):
  if location is None:
    return None
  sheet, row, col = location
  return '{}!{}{}'.format(sheet, _col_letters(col), row + 1)


def reconcile(old_workbook, new_workbook, spec):
  """Compare the formula cells inside ``spec.window`` of two open workbooks."""
  reconciler = Reconciler(old_workbook, new_workbook, spec)
  try:
    return reconciler.run()
  finally:
    reconciler.close()
