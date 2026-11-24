"""In-memory minimal .xlsb container factory for end-to-end tests."""

import io
import zipfile

from pyxlsb import biff12
from tests import biff12_builders as b

CONTENT_TYPES = (
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
  '<Default Extension="rels" '
  'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
  '<Default Extension="bin" '
  'ContentType="application/vnd.ms-excel.sheet.binary.macroEnabled.main"/>'
  '<Override PartName="/xl/workbook.bin" '
  'ContentType="application/vnd.ms-excel.workbook.binary"/>'
  '</Types>')

ROOT_RELS = (
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
  '<Relationship Id="rId1" '
  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
  'Target="xl/workbook.bin"/>'
  '</Relationships>')

WORKBOOK_RELS = (
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
  '<Relationship Id="rId1" '
  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
  'Target="worksheets/sheet1.bin"/>'
  '</Relationships>')


def workbook_blob(sheet_name):
  sheet_payload = b'\x00\x00\x00\x00' + b.i32(1) + \
      b.xlpstr('rId1') + b.xlpstr(sheet_name)
  return (b.record(biff12.WORKBOOK) +
          b.record(biff12.SHEETS) +
          b.record(biff12.SHEET, sheet_payload) +
          b.record(biff12.SHEETS_END) +
          b.record(biff12.WORKBOOK_END))


def build_xlsb(path, sheet_blob, sheet_name='S'):
  with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.writestr('[Content_Types].xml', CONTENT_TYPES)
    zf.writestr('_rels/.rels', ROOT_RELS)
    zf.writestr('xl/workbook.bin', workbook_blob(sheet_name))
    zf.writestr('xl/_rels/workbook.bin.rels', WORKBOOK_RELS)
    zf.writestr('xl/worksheets/sheet1.bin', sheet_blob)


def open_bytes(blob):
  return io.BytesIO(blob)
