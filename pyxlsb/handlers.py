from . import biff12
from collections import namedtuple

class Handler(object):
  def __init__(self):
    super(Handler, self).__init__()

  def read(self, reader, recid, reclen):
    if reclen > 0:
      reader.skip(reclen)


class BasicHandler(Handler):
  def __init__(self, name=None):
    super(BasicHandler, self).__init__()
    self.name = name

  def read(self, reader, recid, reclen):
    super(BasicHandler, self).read(reader, recid, reclen)
    return self.name


class StringTableHandler(Handler):
  cls = namedtuple('sst', ['count', 'uniqueCount'])

  def __init__(self):
    super(StringTableHandler, self).__init__()

  def read(self, reader, recid, reclen):
    count = reader.read_int()
    unique = reader.read_int()
    return self.cls._make([count, unique])


class StringInstanceHandler(Handler):
  cls = namedtuple('si', ['t'])

  def __init__(self):
    super(StringInstanceHandler, self).__init__()

  def read(self, reader, recid, reclen):
    reader.skip(1)
    val = reader.read_string()
    return self.cls._make([val])


class SheetHandler(Handler):
  cls = namedtuple('sheet', ['sheetId', 'rId', 'name'])

  def __init__(self):
    super(SheetHandler, self).__init__()

  def read(self, reader, recid, reclen):
    reader.skip(4)
    sheetid = reader.read_int()
    relid = reader.read_string()
    name = reader.read_string()
    return self.cls._make([sheetid, relid, name])


class DimensionHandler(Handler):
  cls = namedtuple('dimension', ['r', 'c', 'h', 'w'])

  def __init__(self):
    super(DimensionHandler, self).__init__()

  def read(self, reader, recid, reclen):
    r1 = reader.read_int()
    r2 = reader.read_int()
    c1 = reader.read_int()
    c2 = reader.read_int()
    return self.cls._make([r1, c1, r2 - r1 + 1, c2 - c1 + 1])


class ColumnHandler(Handler):
  cls = namedtuple('col', ['c1', 'c2', 'width', 'style'])

  def __init__(self):
    super(ColumnHandler, self).__init__()

  def read(self, reader, recid, reclen):
    c1 = reader.read_int()
    c2 = reader.read_int()
    width = reader.read_int() / 256
    style = reader.read_int()
    return self.cls._make([c1, c2, width, style])


class RowHandler(Handler):
  cls = namedtuple('row', ['r'])

  def __init__(self):
    super(RowHandler, self).__init__()

  def read(self, reader, recid, reclen):
    r = reader.read_int()
    return self.cls._make([r])


class CellHandler(Handler):
  cls = namedtuple('c', ['c', 'v', 'f', 'style'])

  def __init__(self):
    super(CellHandler, self).__init__()

  def read(self, reader, recid, reclen):
    col = reader.read_int()
    style = reader.read_int()
    val = None
    if recid == biff12.NUM:
      val = reader.read_float()
    elif recid == biff12.BOOLERR:
      val = hex(reader.read_byte())
    elif recid == biff12.BOOL:
      val = reader.read_byte() != 0
    elif recid == biff12.FLOAT:
      val = reader.read_double()
    elif recid == biff12.STRING:
      val = reader.read_int()
    elif recid == biff12.FORMULA_STRING:
      val = reader.read_string()
    elif recid == biff12.FORMULA_FLOAT:
      val = reader.read_double()
    elif recid == biff12.FORMULA_BOOL:
      val = reader.read_byte() != 0
    elif recid == biff12.FORMULA_BOOLERR:
      val = hex(reader.read_byte())
    formula = None
    if biff12.FORMULA_STRING <= recid <= biff12.FORMULA_BOOLERR:
      formula = self._read_parsed_formula(reader)
    return self.cls._make([col, val, formula, style])

  def _read_parsed_formula(self, reader):
    cb = reader.read_int()
    if cb is None:
      return RawFormula(b'', b'')
    rgce = reader.read(cb)
    cb_extra = reader.read_int()
    extra = reader.read(cb_extra) if cb_extra else b''
    return RawFormula(rgce, extra or b'')


class RawFormula(object):
  """Raw CellParsedFormula payload (Rgce plus optional RgceExtra)."""
  __slots__ = ('rgce', 'extra')

  def __init__(self, rgce, extra=b''):
    self.rgce = rgce
    self.extra = extra

  def __repr__(self):
    return 'RawFormula({} bytes rgce, {} bytes extra)'.format(
      len(self.rgce), len(self.extra))


class GroupFormulaHandler(Handler):
  """BrtShrFmla (0x0041): rfx followed directly by CellParsedFormula."""
  cls = namedtuple('groupfmla', ['r', 'c', 'h', 'w', 'rgce', 'extra'])

  def read(self, reader, recid, reclen):
    r1 = reader.read_int()
    r2 = reader.read_int()
    c1 = reader.read_int()
    c2 = reader.read_int()
    cb = reader.read_int()
    rgce = reader.read(cb) if cb else b''
    cb_extra = reader.read_int()
    extra = reader.read(cb_extra) if cb_extra else b''
    return self.cls._make([r1, c1, r2 - r1 + 1, c2 - c1 + 1, rgce, extra or b''])


class ArrayFormulaHandler(Handler):
  """BrtArrFmla (0x0040): rfx, fAlwaysCalc byte, then CellParsedFormula."""
  cls = namedtuple('arrayfmla', ['r', 'c', 'h', 'w', 'always_calc',
                                 'rgce', 'extra'])

  def read(self, reader, recid, reclen):
    r1 = reader.read_int()
    r2 = reader.read_int()
    c1 = reader.read_int()
    c2 = reader.read_int()
    always_calc = reader.read_byte()
    cb = reader.read_int()
    rgce = reader.read(cb) if cb else b''
    cb_extra = reader.read_int()
    extra = reader.read(cb_extra) if cb_extra else b''
    return self.cls._make([r1, c1, r2 - r1 + 1, c2 - c1 + 1,
                           bool(always_calc), rgce, extra or b''])


class HyperlinkHandler(Handler):
  cls = namedtuple('hyperlink', ['r', 'c', 'h', 'w', 'rId'])

  def __init__(self):
    super(HyperlinkHandler, self).__init__()

  def read(self, reader, recid, reclen):
    r1 = reader.read_int()
    r2 = reader.read_int()
    c1 = reader.read_int()
    c2 = reader.read_int()
    rId = reader.read_string()
    return self.cls._make([r1, c1, r2 - r1 + 1, c2 - c1 + 1, rId])
