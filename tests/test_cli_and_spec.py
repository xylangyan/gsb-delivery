import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from pyxlsb.__main__ import main
from pyxlsb.reconcile import Rect, make_spec, weighted_rects
from tests import biff12_builders as b
from tests.xlsb_factory import build_xlsb


def rows(row, *records):
  return [b.row_record(row)] + list(records)


class CliAndCompressionTests(unittest.TestCase):

  def setUp(self):
    self.tempfiles = []

  def tearDown(self):
    for path in self.tempfiles:
      if os.path.exists(path):
        os.remove(path)

  def _wb(self, records):
    fd, path = tempfile.mkstemp(suffix='.xlsb')
    os.close(fd)
    build_xlsb(path, b.worksheet_blob(records, dim=(0, 20, 0, 5)))
    self.tempfiles.append(path)
    return path

  def test_cli_reconcile_emits_json_report(self):
    formula_rgce = b.ptg_int(1) + b.ptg_int(2) + b.ptg_add()
    old_path = self._wb(rows(10, b.formula_float(2, 3.0, formula_rgce)))
    new_path = self._wb(rows(20, b.formula_float(2, 3.0, formula_rgce)))
    spec = {
      'window': {'sheet': 'S', 'rect': [10, 2, 10, 2]},
      'old_segments': [
        {'sheet': 'S', 'raw': [0, 0, 20, 5], 'business': [0, 0, 20, 5]}],
      'new_segments': [
        {'sheet': 'S', 'raw': [20, 0, 20, 5], 'business': [10, 0, 10, 5]},
        {'sheet': 'S', 'raw': [0, 0, 19, 5], 'business': [20, 0, 39, 5]}]}
    fd, spec_path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    self.tempfiles.append(spec_path)
    with open(spec_path, 'w', encoding='utf-8') as fp:
      json.dump(spec, fp)

    buf = io.StringIO()
    with redirect_stdout(buf):
      code = main(['reconcile', old_path, new_path, spec_path])
    self.assertEqual(code, 0)
    payload = json.loads(buf.getvalue())
    self.assertEqual(payload['summary']['different'], 0)
    self.assertEqual(payload['summary']['equivalent'], 1)
    self.assertEqual(payload['different'], [])

  def test_cli_returns_nonzero_on_difference(self):
    old_formula = b.ptg_int(1) + b.ptg_int(2) + b.ptg_add()
    new_formula = b.ptg_int(3) + b.ptg_int(4) + b.ptg_add()
    old_path = self._wb(rows(10, b.formula_float(2, 3.0, old_formula)))
    new_path = self._wb(rows(10, b.formula_float(2, 7.0, new_formula)))
    segment = [{'sheet': 'S', 'raw': [0, 0, 20, 5], 'business': [0, 0, 20, 5]}]
    spec = {
      'window': {'sheet': 'S', 'rect': [10, 2, 10, 2]},
      'old_segments': segment,
      'new_segments': segment}
    fd, spec_path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    self.tempfiles.append(spec_path)
    with open(spec_path, 'w', encoding='utf-8') as fp:
      json.dump(spec, fp)

    buf = io.StringIO()
    with redirect_stdout(buf):
      code = main(['reconcile', old_path, new_path, spec_path])
    self.assertEqual(code, 1)
    payload = json.loads(buf.getvalue())
    self.assertEqual(payload['summary']['different'], 1)
    first = payload['different'][0]
    self.assertIn('old_definition', first)
    self.assertIn('new_definition', first)
    self.assertEqual(first['old_definition']['formula'], '(1+2)')
    self.assertEqual(first['new_definition']['formula'], '(3+4)')

  def test_full_column_area_never_expands_into_cells(self):
    # Two full-column areas (1,048,576 rows each) compress to one weighted
    # rectangle with weight 2 instead of millions of entries.
    terms = [Rect(0, 0, 1048575, 0), Rect(0, 0, 1048575, 0)]
    weights = weighted_rects(terms)
    self.assertEqual(len(weights), 1)
    box, weight = next(iter(weights.items()))
    self.assertEqual(weight, 2)
    self.assertEqual(box, Rect(0, 0, 1048575, 0))

  def test_overlapping_segments_rejected(self):
    with self.assertRaises(ValueError):
      make_spec('S', (0, 0, 5, 5),
                [('S', (0, 0, 5, 5), (0, 0, 5, 5)),
                 ('S', (3, 0, 8, 5), (6, 0, 11, 5))],
                [('S', (0, 0, 5, 5), (0, 0, 5, 5))])

  def test_business_side_overlap_rejected(self):
    with self.assertRaises(ValueError):
      make_spec('S', (0, 0, 5, 5),
                [('S', (0, 0, 5, 5), (0, 0, 5, 5)),
                 ('S', (6, 0, 11, 5), (3, 0, 8, 5))],
                [('S', (0, 0, 5, 5), (0, 0, 5, 5))])

  def test_shape_mismatch_rejected(self):
    with self.assertRaises(ValueError):
      make_spec('S', (0, 0, 5, 5),
                [('S', (0, 0, 5, 5), (0, 0, 4, 5))],
                [('S', (0, 0, 5, 5), (0, 0, 5, 5))])


if __name__ == '__main__':
  unittest.main()
