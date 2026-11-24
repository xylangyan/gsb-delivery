"""BIFF12 parsed formula (Ptg token stream) handling.

Only the subset required by the report reconciliation is decoded into an AST:
constants, arithmetic, same-sheet cell/area references and SUM with multiple
range arguments. Anything outside that subset raises :class:`UnsupportedFormula`
so callers can report the location as pending instead of claiming equivalence.
"""

import struct

uint16_t = struct.Struct('<H')
uint32_t = struct.Struct('<I')
int32_t = struct.Struct('<i')
double_t = struct.Struct('<d')

MAX_ROW = 1048575
MAX_COL = 16383

# Ptg identifiers, [MS-XLSB] 2.5.98.4
PTG_EXP = 0x01
PTG_TBL = 0x02
PTG_ADD = 0x03
PTG_SUB = 0x04
PTG_MUL = 0x05
PTG_DIV = 0x06
PTG_POWER = 0x07
PTG_CONCAT = 0x08
PTG_LT = 0x09
PTG_LE = 0x0A
PTG_EQ = 0x0B
PTG_GE = 0x0C
PTG_GT = 0x0D
PTG_NE = 0x0E
PTG_ISECT = 0x0F
PTG_UNION = 0x10
PTG_RANGE = 0x11
PTG_UPLUS = 0x12
PTG_UMINUS = 0x13
PTG_PERCENT = 0x14
PTG_PAREN = 0x15
PTG_MISSARG = 0x16
PTG_STR = 0x17
PTG_NLR = 0x18
PTG_ERR = 0x19
PTG_BOOL = 0x1A
PTG_INT = 0x1B
PTG_NUM = 0x1C
PTG_ARRAY = 0x1D
PTG_FUNC = 0x1E
PTG_FUNCVAR = 0x1F
PTG_NAME = 0x20
PTG_REF = 0x21
PTG_AREA = 0x22
PTG_MEMAREA = 0x23
PTG_MEMERR = 0x24
PTG_MEMNOMEM = 0x25
PTG_MEMFUNC = 0x26
PTG_REFERR = 0x27
PTG_AREAERR = 0x28
PTG_REFN = 0x29
PTG_AREAN = 0x2A
PTG_NAMEN = 0x2B
PTG_NAMEX = 0x39
PTG_REF3D = 0x3A
PTG_AREA3D = 0x3B

# Built-in function table indices actually needed, [MS-XLSB] Ftab.
FUNC_SUM = 0x0004
SUPPORTED_FUNCS = {FUNC_SUM: 'SUM'}

BINARY_OPS = {
  PTG_ADD: '+',
  PTG_SUB: '-',
  PTG_MUL: '*',
  PTG_DIV: '/',
  PTG_POWER: '^',
}
UNARY_OPS = {
  PTG_UPLUS: '+',
  PTG_UMINUS: '-',
  PTG_PERCENT: '%',
}


class FormulaParseError(Exception):
  """Raised when a formula byte stream does not match the documented layout."""


class UnsupportedFormula(Exception):
  """Raised for tokens/constructs outside the supported reconciliation subset."""

  def __init__(self, reason, ptg=None):
    super(UnsupportedFormula, self).__init__(reason)
    self.reason = reason
    self.ptg = ptg


class Const(object):
  __slots__ = ('value',)

  def __init__(self, value):
    self.value = value


class MissingArg(object):
  __slots__ = ()


class Ref(object):
  """A single-cell reference materialized against a concrete formula cell."""
  __slots__ = ('row', 'col', 'row_abs', 'col_abs')

  def __init__(self, row, col, row_abs, col_abs):
    self.row = row
    self.col = col
    self.row_abs = row_abs
    self.col_abs = col_abs


class Area(object):
  """A rectangular reference materialized against a concrete formula cell."""
  __slots__ = ('r1', 'r2', 'c1', 'c2', 'r1_abs', 'r2_abs', 'c1_abs', 'c2_abs')

  def __init__(self, r1, r2, c1, c2, r1_abs, r2_abs, c1_abs, c2_abs):
    self.r1 = r1
    self.r2 = r2
    self.c1 = c1
    self.c2 = c2
    self.r1_abs = r1_abs
    self.r2_abs = r2_abs
    self.c1_abs = c1_abs
    self.c2_abs = c2_abs

  @property
  def full_column(self):
    return self.r1 == 0 and self.r2 == MAX_ROW


class BinOp(object):
  __slots__ = ('op', 'left', 'right')

  def __init__(self, op, left, right):
    self.op = op
    self.left = left
    self.right = right


class UnaryOp(object):
  __slots__ = ('op', 'operand')

  def __init__(self, op, operand):
    self.op = op
    self.operand = operand


class Paren(object):
  __slots__ = ('operand',)

  def __init__(self, operand):
    self.operand = operand


class FuncCall(object):
  __slots__ = ('name', 'args')

  def __init__(self, name, args):
    self.name = name
    self.args = args


class ExpRef(object):
  """PtgExp: a non-master array formula cell pointing at its array master."""
  __slots__ = ('row', 'col')

  def __init__(self, row, col):
    self.row = row
    self.col = col


def _col_abs_flags(raw_col):
  col = raw_col & 0x3FFF
  col_rel = bool(raw_col & 0x4000)
  row_rel = bool(raw_col & 0x8000)
  return col, col_rel, row_rel


def _resolve_row(raw_row, rel, base_row):
  if rel:
    return base_row + int32_t.unpack(uint32_t.pack(raw_row & 0xFFFFFFFF))[0]
  return raw_row


def _resolve_col(raw_col, rel, base_col):
  col = raw_col & 0x3FFF
  if rel:
    if col & 0x2000:
      delta = col - 0x4000
    else:
      delta = col
    return base_col + delta
  return col


def _read_wide_string(data, pos):
  if pos + 4 > len(data):
    raise FormulaParseError('truncated string length')
  length = uint32_t.unpack(data[pos:pos + 4])[0]
  pos += 4
  if length == 0xFFFFFFFF:
    return None, pos
  end = pos + length * 2
  if end > len(data):
    raise FormulaParseError('truncated string body')
  value = data[pos:end].decode('utf-16-le', errors='replace')
  return value, end


def parse_rgce(rgce, base_row, base_col):
  """Parse an Rgce token stream into an AST.

  Relative references are resolved against ``(base_row, base_col)`` so the same
  shared-formula token stream can be materialized for every member cell.
  """
  pos = 0
  stack = []
  size = len(rgce)
  while pos < size:
    ptg = rgce[pos]
    pos += 1
    if ptg in BINARY_OPS:
      if len(stack) < 2:
        raise FormulaParseError('missing operand for binary op')
      right = stack.pop()
      left = stack.pop()
      stack.append(BinOp(BINARY_OPS[ptg], left, right))
    elif ptg in UNARY_OPS:
      if not stack:
        raise FormulaParseError('missing operand for unary op')
      stack.append(UnaryOp(UNARY_OPS[ptg], stack.pop()))
    elif ptg == PTG_PAREN:
      if not stack:
        raise FormulaParseError('missing operand for parenthesis')
      stack.append(Paren(stack.pop()))
    elif ptg == PTG_MISSARG:
      stack.append(MissingArg())
    elif ptg == PTG_INT:
      stack.append(Const(uint16_t.unpack(rgce[pos:pos + 2])[0]))
      pos += 2
    elif ptg == PTG_NUM:
      stack.append(Const(double_t.unpack(rgce[pos:pos + 8])[0]))
      pos += 8
    elif ptg == PTG_BOOL:
      stack.append(Const(bool(rgce[pos])))
      pos += 1
    elif ptg == PTG_STR:
      value, pos = _read_wide_string(rgce, pos)
      stack.append(Const(value if value is not None else ''))
    elif ptg == PTG_REF:
      node, pos = _parse_ref(rgce, pos, base_row, base_col)
      stack.append(node)
    elif ptg == PTG_AREA:
      node, pos = _parse_area(rgce, pos, base_row, base_col)
      stack.append(node)
    elif ptg == PTG_EXP:
      row = uint32_t.unpack(rgce[pos:pos + 4])[0]
      col = uint32_t.unpack(rgce[pos + 4:pos + 8])[0]
      pos += 8
      stack.append(ExpRef(row, col))
    elif ptg == PTG_FUNCVAR:
      argc = rgce[pos]
      pos += 1
      if pos + 2 > size:
        raise FormulaParseError('truncated function token')
      iftab = uint16_t.unpack(rgce[pos:pos + 2])[0]
      pos += 2
      stack, pos = _parse_func(iftab, argc, stack, pos)
    else:
      raise UnsupportedFormula('ptg 0x{:02X} is outside the supported subset'.format(ptg), ptg)
  if len(stack) != 1:
    raise FormulaParseError('formula token stream did not yield one result')
  return stack[0]


def _parse_ref(data, pos, base_row, base_col):
  raw_row = uint32_t.unpack(data[pos:pos + 4])[0]
  raw_col = uint32_t.unpack(data[pos + 4:pos + 8])[0]
  pos += 8
  col, col_rel, row_rel = _col_abs_flags(raw_col)
  row = _resolve_row(raw_row, row_rel, base_row)
  col = _resolve_col(raw_col, col_rel, base_col)
  return Ref(row, col, not row_rel, not col_rel), pos


def _parse_area(data, pos, base_row, base_col):
  r_first = uint32_t.unpack(data[pos:pos + 4])[0]
  r_last = uint32_t.unpack(data[pos + 4:pos + 8])[0]
  raw_c_first = uint32_t.unpack(data[pos + 8:pos + 12])[0]
  raw_c_last = uint32_t.unpack(data[pos + 12:pos + 16])[0]
  pos += 16
  c1, c1_rel, r1_rel = _col_abs_flags(raw_c_first)
  c2, c2_rel, r2_rel = _col_abs_flags(raw_c_last)
  r1 = _resolve_row(r_first, r1_rel, base_row)
  r2 = _resolve_row(r_last, r2_rel, base_row)
  c1 = _resolve_col(raw_c_first, c1_rel, base_col)
  c2 = _resolve_col(raw_c_last, c2_rel, base_col)
  if r2 < r1 or c2 < c1:
    raise FormulaParseError('inverted area reference')
  return Area(r1, r2, c1, c2, not r1_rel, not r2_rel, not c1_rel, not c2_rel), pos


def _parse_func(iftab, argc, stack, pos):
  if iftab & 0x8000:
    raise UnsupportedFormula('user-defined function call')
  index = iftab & 0x7FFF
  if index not in SUPPORTED_FUNCS:
    raise UnsupportedFormula('function index {} is outside the supported subset'.format(index))
  if len(stack) < argc:
    raise FormulaParseError('missing function arguments')
  args = stack[-argc:] if argc else []
  if argc:
    del stack[-argc:]
  stack.append(FuncCall(SUPPORTED_FUNCS[index], args))
  return stack, pos


def _a1_cell(row, col, row_abs, col_abs):
  return '{}{}{}'.format('$' if col_abs else '', _col_letters(col),
                         '${}'.format(row + 1) if row_abs else str(row + 1))


def _col_letters(col):
  letters = ''
  col += 1
  while col:
    col, rem = divmod(col - 1, 26)
    letters = chr(65 + rem) + letters
  return letters


def render_a1(node):
  """Render a materialized AST back to A1-style formula text for reports."""
  if isinstance(node, Const):
    value = node.value
    if isinstance(value, bool):
      return 'TRUE' if value else 'FALSE'
    if isinstance(value, str):
      return '"{}"'.format(value.replace('"', '""'))
    return str(value)
  if isinstance(node, MissingArg):
    return ''
  if isinstance(node, Ref):
    return _a1_cell(node.row, node.col, node.row_abs, node.col_abs)
  if isinstance(node, Area):
    if node.full_column:
      head = '{}:{}'.format(
        ('$' if node.c1_abs else '') + _col_letters(node.c1),
        ('$' if node.c2_abs else '') + _col_letters(node.c2))
    else:
      head = '{}:{}'.format(
        _a1_cell(node.r1, node.c1, node.r1_abs, node.c1_abs),
        _a1_cell(node.r2, node.c2, node.r2_abs, node.c2_abs))
    return head
  if isinstance(node, BinOp):
    return '({}{}{})'.format(render_a1(node.left), node.op, render_a1(node.right))
  if isinstance(node, UnaryOp):
    return '({}{})'.format(node.op, render_a1(node.operand))
  if isinstance(node, Paren):
    return '({})'.format(render_a1(node.operand))
  if isinstance(node, FuncCall):
    return '{}({})'.format(node.name, ','.join(render_a1(arg) for arg in node.args))
  if isinstance(node, ExpRef):
    return _a1_cell(node.row, node.col, True, True)
  raise FormulaParseError('unknown AST node')


def iter_refs(node):
  """Yield every Ref/Area node contained in an AST."""
  if isinstance(node, (Ref, Area)):
    yield node
  elif isinstance(node, BinOp):
    for child in iter_refs(node.left):
      yield child
    for child in iter_refs(node.right):
      yield child
  elif isinstance(node, UnaryOp):
    for child in iter_refs(node.operand):
      yield child
  elif isinstance(node, Paren):
    for child in iter_refs(node.operand):
      yield child
  elif isinstance(node, FuncCall):
    for arg in node.args:
      for child in iter_refs(arg):
        yield child
