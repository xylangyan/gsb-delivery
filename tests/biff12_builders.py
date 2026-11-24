"""Minimal BIFF12 byte-stream builders for tests.

Real .xlsb writers are unavailable in this environment, so fixtures encode the
documented record layouts directly and are consumed by the production parsers.
"""

import struct

from pyxlsb import biff12

uint16 = struct.Struct('<H')
uint32 = struct.Struct('<I')
double = struct.Struct('<d')


def varint(value):
  """7-bit LEB128 used for record *lengths*."""
  out = bytearray()
  while True:
    byte = value & 0x7F
    value >>= 7
    if value:
      out.append(byte | 0x80)
    else:
      out.append(byte)
      break
  return bytes(out)


def recid_bytes(recid):
  """Record ID encoding: low byte as-is (bit 7 doubles as continuation),
  followed by the high byte when the ID does not fit in seven bits."""
  if recid < 0x80:
    return bytes([recid])
  return bytes([recid & 0xFF, recid >> 8])


def record(recid, payload=b''):
  return recid_bytes(recid) + varint(len(payload)) + payload


def i32(value):
  return uint32.pack(value & 0xFFFFFFFF)


def xlpstr(text):
  encoded = text.encode('utf-16-le')
  return i32(len(text)) + encoded


def dimension(r1, r2, c1, c2):
  return record(biff12.DIMENSION, b''.join(map(i32, (r1, r2, c1, c2))))


def row_record(row):
  return record(biff12.ROW, i32(row))


def parsed_formula_payload(rgce, extra=b''):
  return i32(len(rgce)) + rgce + i32(len(extra)) + extra


def formula_float(col, value, rgce, style=0, extra=b''):
  payload = i32(col) + i32(style) + double.pack(value) + \
      parsed_formula_payload(rgce, extra)
  return record(biff12.FORMULA_FLOAT, payload)


def formula_string(col, value, rgce, style=0):
  payload = i32(col) + i32(style) + xlpstr(value) + parsed_formula_payload(rgce)
  return record(biff12.FORMULA_STRING, payload)


def num_cell(col, value, style=0):
  # NUM uses the compressed 4-byte float encoding; small integers use the
  # integer path (flag 0x02, no percent): value stored as int << 2.
  payload = i32(col) + i32(style) + i32((int(value) << 2) | 0x02)
  return record(biff12.NUM, payload)


def group_record(recid, r1, r2, c1, c2, rgce, extra=b'', always_calc=False):
  head = b''.join(map(i32, (r1, r2, c1, c2)))
  if recid == 0x0040:  # BrtArrFmla carries a one-byte fAlwaysCalc
    head += bytes([1 if always_calc else 0])
  payload = head + parsed_formula_payload(rgce, extra)
  return record(recid, payload)


def sheetdata(records):
  return record(biff12.SHEETDATA) + b''.join(records) + \
      record(biff12.SHEETDATA_END)


def worksheet_blob(body_records, dim=(0, 100, 0, 10)):
  return record(biff12.WORKSHEET) + dimension(*dim) + \
      sheetdata(body_records) + record(biff12.WORKSHEET_END)


# --- Ptg token builders -----------------------------------------------------

def ptg_int(value):
  return bytes([0x1B]) + uint16.pack(value)


def ptg_num(value):
  return bytes([0x1C]) + double.pack(value)


def ptg_ref(row, col, row_abs=True, col_abs=True):
  flags = 0
  raw_col = col & 0x3FFF
  if not row_abs:
    flags |= 0x8000
  if not col_abs:
    flags |= 0x4000
  return bytes([0x21]) + i32(row) + i32(raw_col | flags)


def ptg_ref_rel(delta_row, delta_col):
  raw_col = delta_col & 0x3FFF
  return bytes([0x21]) + i32(delta_row & 0xFFFFFFFF) + \
      i32(raw_col | 0x8000 | 0x4000)


def ptg_area(r1, r2, c1, c2, full_abs=True):
  flags = 0 if full_abs else (0x8000 | 0x4000)
  return bytes([0x22]) + b''.join(map(i32, (
      r1, r2, c1 | flags, c2 | flags)))


def ptg_exp(row, col):
  return bytes([0x01]) + i32(row) + i32(col)


def ptg_funcvar(iftab, argc):
  return bytes([0x1F, argc]) + uint16.pack(iftab)


def ptg_add():
  return bytes([0x03])


def ptg_mul():
  return bytes([0x05])


def ptg_uminus():
  return bytes([0x13])
