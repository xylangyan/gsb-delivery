import os
import sys
import xml.etree.ElementTree as ET
from . import biff12
from . import formula as formula_mod
from .reader import BIFF12Reader
from collections import namedtuple

if sys.version_info > (3,):
  xrange = range

Cell = namedtuple('Cell', ['r', 'c', 'v'])

GroupDef = namedtuple('GroupDef', ['kind', 'r', 'c', 'h', 'w', 'rgce', 'extra'])
FormulaEntry = namedtuple('FormulaEntry',
                          ['r', 'c', 'kind', 'group', 'rgce', 'extra', 'v', 'style'])

KIND_REGULAR = 'regular'
KIND_SHARED_MASTER = 'shared_master'
KIND_SHARED_MEMBER = 'shared_member'
KIND_ARRAY_MASTER = 'array_master'
KIND_ARRAY_MEMBER = 'array_member'


class Worksheet(object):
  def __init__(self, name, fp, rels_fp=None, stringtable=None, debug=False):
    super(Worksheet, self).__init__()
    self.name = name
    self._reader = BIFF12Reader(fp=fp, debug=debug)
    self._fp = fp
    self._rels_fp = rels_fp
    self._rels = ET.parse(rels_fp).getroot() if rels_fp is not None else None
    self._stringtable = stringtable
    self._debug = debug
    self._data_offset = 0
    self.dimension = None
    self.cols = []
    self.rels = {}
    self.hyperlinks = {}
    self._formula_index = None
    self._parse()

  def __enter__(self):
    return self

  def __exit__(self, type, value, traceback):
    self.close()

  def __iter__(self):
    return self.rows()

  def _parse(self):
    if self._rels is not None:
      for el in self._rels:
        self.rels[el.attrib['Id']] = el.attrib['Target']

    for item in self._reader:
      if item[0] == biff12.DIMENSION:
        self.dimension = item[1]
      elif item[0] == biff12.COL:
        self.cols.append(item[1])
      elif item[0] == biff12.SHEETDATA:
        self._data_offset = self._reader.tell()
        if self._rels is None:
          break
      elif item[0] == biff12.HYPERLINK and self._rels is not None:
        for r in xrange(item[1].h):
          for c in xrange(item[1].w):
            self.hyperlinks[item[1].r + r, item[1].c + c] = item[1].rId

  def rows(self, sparse=False):
    self._reader.seek(self._data_offset, os.SEEK_SET)
    row_num = -1
    row = None
    for item in self._reader:
      if item[0] == biff12.ROW and item[1].r != row_num:
        if row is not None:
          yield row
        if not sparse:
          while row_num < item[1].r - 1:
            row_num += 1
            yield [Cell(row_num, i, None) for i in xrange(self.dimension.c + self.dimension.w)]
        row_num = item[1].r
        row = [Cell(row_num, i, None) for i in xrange(self.dimension.c + self.dimension.w)]
      elif item[0] >= biff12.BLANK and item[0] <= biff12.FORMULA_BOOLERR:
        if item[0] == biff12.STRING and self._stringtable is not None:
          row[item[1].c] = Cell(row_num, item[1].c, self._stringtable[item[1].v])
        else:
          row[item[1].c] = Cell(row_num, item[1].c, item[1].v)
      elif item[0] == biff12.SHEETDATA_END:
        if row is not None:
          yield row
        break

  def formula_index(self):
    """Scan sheetData once and index every formula, shared and array record.

    The scan stores token streams and group definitions only; shared formulas
    and full-column areas are never expanded into per-cell tables.
    """
    if self._formula_index is not None:
      return self._formula_index
    index = FormulaIndex()
    reader = BIFF12Reader(fp=self._fp, debug=self._debug)
    reader.seek(self._data_offset, os.SEEK_SET)
    row_num = -1
    pending = None
    for recid, payload in reader:
      if recid == biff12.ROW:
        row_num = payload.r
        pending = None
      elif recid in (biff12.SHRFMLA, biff12.ARRFMLA):
        kind = KIND_SHARED_MASTER if recid == biff12.SHRFMLA else KIND_ARRAY_MASTER
        pending = GroupDef(kind, payload.r, payload.c, payload.h, payload.w,
                           payload.rgce, payload.extra)
      elif biff12.FORMULA_STRING <= recid <= biff12.FORMULA_BOOLERR:
        entry = self._index_formula_cell(row_num, payload, pending, index)
        index.cells[(entry.r, entry.c)] = entry
        index.all_cells.add((row_num, payload.c))
        pending = None
      elif biff12.BLANK <= recid <= biff12.FLOAT or recid == biff12.STRING:
        index.all_cells.add((row_num, payload.c))
        pending = None
      elif recid == biff12.SHEETDATA_END:
        break
    self._formula_index = index
    return index

  def _index_formula_cell(self, row_num, payload, pending, index):
    rgce = payload.f.rgce if payload.f is not None else b''
    extra = payload.f.extra if payload.f is not None else b''
    if pending is not None and (row_num, payload.c) == (pending.r, pending.c):
      index.groups[(pending.r, pending.c)] = pending
      return FormulaEntry(row_num, payload.c, pending.kind, (pending.r, pending.c),
                          pending.rgce, pending.extra, payload.v, payload.style)
    master = _exp_target(rgce)
    if master is not None:
      kind = KIND_ARRAY_MEMBER if (master in index.groups and
                                   index.groups[master].kind == KIND_ARRAY_MASTER) \
        else KIND_SHARED_MEMBER
      return FormulaEntry(row_num, payload.c, kind, master, b'', b'', payload.v, payload.style)
    return FormulaEntry(row_num, payload.c, KIND_REGULAR, None, rgce, extra,
                        payload.v, payload.style)

  def materialize_formula(self, r, c):
    """Return (kind, group_def_or_None, AST) for one formula cell.

    Shared formula token streams are resolved against the member cell itself,
    so absolute and relative reference parts both follow the workbook rules.
    """
    index = self.formula_index()
    entry = index.cells.get((r, c))
    if entry is None:
      return None
    if entry.kind in (KIND_SHARED_MEMBER, KIND_ARRAY_MEMBER):
      group = index.groups.get(entry.group)
      if group is None:
        raise formula_mod.UnsupportedFormula(
          'member cell points at missing group master {}'.format(entry.group))
      if entry.kind == KIND_SHARED_MEMBER:
        # Shared formulas shift relative parts for every member cell.
        return entry.kind, group, formula_mod.parse_rgce(group.rgce, r, c)
      # CSE array formulas evaluate one identical formula over the whole
      # output region: relative parts stay anchored at the master cell.
      return entry.kind, group, formula_mod.parse_rgce(
        group.rgce, group.r, group.c)
    if entry.kind in (KIND_SHARED_MASTER, KIND_ARRAY_MASTER):
      group = index.groups[entry.group]
      return entry.kind, group, formula_mod.parse_rgce(group.rgce, r, c)
    return entry.kind, None, formula_mod.parse_rgce(entry.rgce, r, c)

  def close(self):
    self._reader.close()
    if self._rels_fp is not None:
      self._rels_fp.close()


class FormulaIndex(object):
  def __init__(self):
    self.cells = {}
    self.groups = {}
    self.all_cells = set()


def _exp_target(rgce):
  """Return the master coordinate of a PtgExp-only stream, else None."""
  if len(rgce) == 9 and rgce[0] == formula_mod.PTG_EXP:
    row = int.from_bytes(rgce[1:5], 'little')
    col = int.from_bytes(rgce[5:9], 'little')
    return row, col
  return None
