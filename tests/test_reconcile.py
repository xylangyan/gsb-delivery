import os
import tempfile
import unittest

from pyxlsb import biff12, open_workbook
from pyxlsb.reconcile import make_spec, reconcile
from tests import biff12_builders as b
from tests.xlsb_factory import build_xlsb


def write_workbook(records, dim):
  fd, path = tempfile.mkstemp(suffix='.xlsb')
  os.close(fd)
  build_xlsb(path, b.worksheet_blob(records, dim=dim))
  return path


def rel_times_two():
  # =A{r}*2 with a fully relative reference (column delta -2, row delta 0)
  return b.ptg_ref_rel(0, -2) + b.ptg_int(2) + b.ptg_mul()


def rows(row, *records):
  return [b.row_record(row)] + list(records)


class ReconcileTests(unittest.TestCase):

  def setUp(self):
    self.tempfiles = []

  def tearDown(self):
    for path in self.tempfiles:
      if os.path.exists(path):
        os.remove(path)

  def _wb(self, records, dim=(0, 30, 0, 5)):
    path = write_workbook(records, dim)
    self.tempfiles.append(path)
    return path

  def _spec(self, old_segments, new_segments, window=(10, 2, 13, 2)):
    return make_spec('S', window, old_segments, new_segments)

  def _identity_segments(self):
    return [('S', (0, 0, 30, 5), (0, 0, 30, 5))]

  def test_shared_formula_split_and_expanded_cells_equivalent(self):
    old_records = []
    old_records += rows(10,
      b.group_record(biff12.SHRFMLA, 10, 12, 2, 2, rel_times_two()),
      b.formula_float(2, 20.0, rel_times_two()))
    old_records += rows(11, b.formula_float(2, 22.0, b.ptg_exp(10, 2)))
    old_records += rows(12, b.formula_float(2, 24.0, b.ptg_exp(10, 2)))
    old_records += rows(13, b.formula_float(2, 26.0, rel_times_two()))
    old_path = self._wb(old_records)

    new_records = []
    new_records += rows(20,
      b.group_record(biff12.SHRFMLA, 20, 21, 2, 2, rel_times_two()),
      b.formula_float(2, 20.0, rel_times_two()))
    new_records += rows(21, b.formula_float(2, 22.0, b.ptg_exp(20, 2)))
    new_records += rows(22, b.formula_float(2, 24.0, rel_times_two()))
    new_records += rows(23, b.formula_float(2, 26.0, rel_times_two()))
    new_path = self._wb(new_records)

    old_segments = self._identity_segments()
    new_segments = [
      ('S', (20, 0, 23, 0), (10, 0, 13, 0)),
      ('S', (20, 2, 23, 2), (10, 2, 13, 2))]
    spec = self._spec(old_segments, new_segments)

    with open_workbook(old_path) as old_wb, open_workbook(new_path) as new_wb:
      report = reconcile(old_wb, new_wb, spec)

    self.assertEqual(len(report.different), 0)
    self.assertEqual(len(report.pending), 0)
    self.assertEqual(len(report.equivalent), 4)
    sources = {r.business: r.new_definition.source_cell for r in report.reports}
    self.assertEqual(sources[(10, 2)], (20, 2))
    self.assertEqual(sources[(11, 2)], (20, 2))
    self.assertEqual(sources[(12, 2)], (22, 2))
    self.assertEqual(sources[(13, 2)], (23, 2))

  def test_sum_split_into_two_fragments_equivalent_with_column_swap(self):
    # Old layout: data in raw column A (col 0), identity segments.
    old_formula = (b.ptg_area(0, 3, 0, 0) +
                   b.ptg_funcvar(0x0004, 1))
    old_path = self._wb(
      rows(1, b.formula_float(2, 10.0, old_formula)))

    # New layout: fixed column swap (raw col 1 = business col 0) and row move.
    new_formula = (b.ptg_area(10, 11, 1, 1) +
                   b.ptg_area(12, 13, 1, 1) +
                   b.ptg_funcvar(0x0004, 2))
    new_path = self._wb(
      rows(5, b.formula_float(2, 10.0, new_formula)))

    old_segments = [
      ('S', (0, 0, 30, 0), (0, 0, 30, 0)),
      ('S', (0, 2, 30, 2), (0, 2, 30, 2))]
    new_segments = [
      ('S', (10, 1, 13, 1), (0, 0, 3, 0)),
      ('S', (5, 2, 5, 2), (1, 2, 1, 2))]
    spec = self._spec(old_segments, new_segments, window=(1, 2, 1, 2))

    with open_workbook(old_path) as old_wb, open_workbook(new_path) as new_wb:
      report = reconcile(old_wb, new_wb, spec)

    self.assertEqual(report.pending, [])
    self.assertEqual(len(report.equivalent), 1)

  def test_sum_duplicated_cell_count_must_be_preserved(self):
    # Old side sums A1:A2 twice; new side covers the same cells only once.
    old_formula = (b.ptg_area(0, 1, 0, 0) + b.ptg_area(0, 1, 0, 0) +
                   b.ptg_funcvar(0x0004, 2))
    old_path = self._wb(rows(1, b.formula_float(2, 6.0, old_formula)))

    new_formula = b.ptg_area(0, 1, 0, 0) + b.ptg_funcvar(0x0004, 1)
    new_path = self._wb(rows(1, b.formula_float(2, 6.0, new_formula)))

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(1, 2, 1, 2))
    with open_workbook(old_path) as old_wb, open_workbook(new_path) as new_wb:
      report = reconcile(old_wb, new_wb, spec)

    self.assertEqual(len(report.different), 1)
    self.assertIn('multiplicity', report.first_difference().detail)

  def test_duplicated_count_equivalent_when_both_double_count(self):
    old_formula = (b.ptg_area(0, 1, 0, 0) + b.ptg_area(0, 1, 0, 0) +
                   b.ptg_funcvar(0x0004, 2))
    # Same double count, but expressed with split fragments on the new side.
    new_formula = (b.ptg_area(0, 0, 0, 0) + b.ptg_area(1, 1, 0, 0) +
                   b.ptg_area(0, 1, 0, 0) + b.ptg_funcvar(0x0004, 3))
    old_path = self._wb(rows(1, b.formula_float(2, 6.0, old_formula)))
    new_path = self._wb(rows(1, b.formula_float(2, 6.0, new_formula)))

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(1, 2, 1, 2))
    with open_workbook(old_path) as old_wb, open_workbook(new_path) as new_wb:
      report = reconcile(old_wb, new_wb, spec)

    self.assertEqual(report.different, [])
    self.assertEqual(len(report.equivalent), 1)

  def test_array_formula_replaced_by_per_cell_calculation_fails(self):
    old_records = []
    old_records += rows(10,
      b.group_record(biff12.ARRFMLA, 10, 11, 2, 2, rel_times_two()),
      b.formula_float(2, 20.0, rel_times_two()))
    old_records += rows(11, b.formula_float(2, 22.0, b.ptg_exp(10, 2)))
    old_path = self._wb(old_records)

    new_records = []
    new_records += rows(20, b.formula_float(2, 20.0, rel_times_two()))
    new_records += rows(21, b.formula_float(2, 22.0, rel_times_two()))
    new_path = self._wb(new_records)

    new_segments = [('S', (20, 2, 21, 2), (10, 2, 11, 2))]
    spec = self._spec(self._identity_segments(), new_segments,
                      window=(10, 2, 11, 2))
    with open_workbook(old_path) as old_wb, open_workbook(new_path) as new_wb:
      report = reconcile(old_wb, new_wb, spec)

    self.assertEqual(len(report.different), 1)
    difference = report.first_difference()
    self.assertIn('per-cell calculation', difference.detail)
    self.assertIn('C11:C12', difference.detail)

  def test_equivalent_array_formulas_pass_with_output_members(self):
    records = []
    records += rows(10,
      b.group_record(biff12.ARRFMLA, 10, 11, 2, 2, rel_times_two()),
      b.formula_float(2, 20.0, rel_times_two()))
    records += rows(11, b.formula_float(2, 22.0, b.ptg_exp(10, 2)))
    path = self._wb(records)

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(10, 2, 11, 2))
    with open_workbook(path) as wb1, open_workbook(path) as wb2:
      report = reconcile(wb1, wb2, spec)

    self.assertEqual(report.different, [])
    self.assertEqual(report.pending, [])
    self.assertEqual(len(report.equivalent), 1)

  def test_uncovered_reference_is_pending(self):
    # Formula points at row 40, outside every supplied segment.
    formula_rgce = b.ptg_ref(40, 0) + b.ptg_int(1) + b.ptg_add()
    path = self._wb(rows(10, b.formula_float(2, 1.0, formula_rgce)))

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(10, 2, 10, 2))
    with open_workbook(path) as wb1, open_workbook(path) as wb2:
      report = reconcile(wb1, wb2, spec)

    self.assertEqual(len(report.pending), 1)
    self.assertEqual(report.pending[0].pending.kind, 'uncovered-reference')
    self.assertEqual(report.different, [])

  def test_unsupported_formula_is_pending_not_equivalent(self):
    # PtgName (0x20) defined-name token is outside the supported subset.
    formula_rgce = bytes([0x20]) + b.i32(0) + b.i32(0)
    path = self._wb(rows(10, b.formula_float(2, 1.0, formula_rgce)))

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(10, 2, 10, 2))
    with open_workbook(path) as wb1, open_workbook(path) as wb2:
      report = reconcile(wb1, wb2, spec)

    self.assertEqual(len(report.pending), 1)
    self.assertEqual(report.pending[0].pending.kind, 'unsupported-formula')

  def test_shared_master_outside_window_still_resolved(self):
    # Master lives on row 9; window starts at row 10 and only the member is in
    # it. The group definition just outside the window must still be included.
    records = []
    records += rows(9,
      b.group_record(biff12.SHRFMLA, 9, 11, 2, 2, rel_times_two()),
      b.formula_float(2, 18.0, rel_times_two()))
    records += rows(10, b.formula_float(2, 20.0, b.ptg_exp(9, 2)))
    records += rows(11, b.formula_float(2, 22.0, b.ptg_exp(9, 2)))
    path = self._wb(records)

    spec = self._spec(self._identity_segments(), self._identity_segments(),
                      window=(10, 2, 11, 2))
    with open_workbook(path) as wb1, open_workbook(path) as wb2:
      report = reconcile(wb1, wb2, spec)

    self.assertEqual(report.different, [])
    self.assertEqual(report.pending, [])
    self.assertEqual(len(report.equivalent), 2)
    self.assertEqual(report.equivalent[0].old_definition.source_cell, (9, 2))

  def test_cached_value_reading_still_works(self):
    path = self._wb(
      rows(0, b.num_cell(0, 7), b.formula_float(1, 3.5, rel_times_two())))
    with open_workbook(path) as wb:
      with wb.get_sheet(1) as sheet:
        data = list(sheet.rows(sparse=True))
    self.assertEqual(data[0][0].v, 7)
    self.assertEqual(data[0][1].v, 3.5)


if __name__ == '__main__':
  unittest.main()
