import unittest

from pyxlsb import formula as fm
from tests import biff12_builders as b


class FormulaTokenTests(unittest.TestCase):

  def test_parse_absolute_ref_resolves_against_base(self):
    rgce = b.ptg_ref(4, 2)
    node = fm.parse_rgce(rgce, 10, 10)
    self.assertIsInstance(node, fm.Ref)
    self.assertEqual((node.row, node.col), (4, 2))
    self.assertTrue(node.row_abs and node.col_abs)
    self.assertEqual(fm.render_a1(node), '$C$5')

  def test_parse_relative_ref_materializes_per_cell(self):
    rgce = b.ptg_ref_rel(-2, -1)
    node = fm.parse_rgce(rgce, 10, 5)
    self.assertEqual((node.row, node.col), (8, 4))
    self.assertFalse(node.row_abs or node.col_abs)
    self.assertEqual(fm.render_a1(node), 'E9')
    shifted = fm.parse_rgce(rgce, 20, 5)
    self.assertEqual((shifted.row, shifted.col), (18, 4))

  def test_parse_mixed_abs_rel_parts(self):
    raw_col = (0x3FFF & (-1)) | 0x4000
    rgce = bytes([0x21]) + b.i32(4) + b.i32(raw_col)
    node = fm.parse_rgce(rgce, 10, 5)
    self.assertEqual((node.row, node.col), (4, 4))
    self.assertTrue(node.row_abs)
    self.assertFalse(node.col_abs)
    self.assertEqual(fm.render_a1(node), 'E$5')

  def test_arithmetic_rpn_and_render(self):
    rgce = b.ptg_int(3) + b.ptg_num(2.5) + b.ptg_add()
    node = fm.parse_rgce(rgce, 0, 0)
    self.assertIsInstance(node, fm.BinOp)
    self.assertEqual(node.op, '+')
    self.assertEqual(fm.render_a1(node), '(3+2.5)')

  def test_unary_minus_and_nesting(self):
    rgce = b.ptg_ref(0, 0) + b.ptg_uminus() + b.ptg_int(2) + b.ptg_mul()
    node = fm.parse_rgce(rgce, 0, 0)
    self.assertEqual(fm.render_a1(node), '((-$A$1)*2)')

  def test_sum_with_multiple_ranges_and_cell(self):
    rgce = (b.ptg_area(0, 2, 0, 0) +
            b.ptg_area(4, 5, 1, 1) +
            b.ptg_ref(8, 2) +
            b.ptg_funcvar(fm.FUNC_SUM, 3))
    node = fm.parse_rgce(rgce, 0, 0)
    self.assertIsInstance(node, fm.FuncCall)
    self.assertEqual(node.name, 'SUM')
    self.assertEqual(len(node.args), 3)
    self.assertEqual(fm.render_a1(node), 'SUM($A$1:$A$3,$B$5:$B$6,$C$9)')

  def test_unsupported_token_is_pending(self):
    rgce = bytes([0x20]) + b.i32(0) + b.i32(0)
    with self.assertRaises(fm.UnsupportedFormula):
      fm.parse_rgce(rgce, 0, 0)

  def test_unsupported_function_is_pending(self):
    rgce = b.ptg_int(1) + b.ptg_funcvar(0x0001, 1)
    with self.assertRaises(fm.UnsupportedFormula):
      fm.parse_rgce(rgce, 0, 0)

  def test_full_column_area_flag(self):
    rgce = b.ptg_area(0, fm.MAX_ROW, 0, 2)
    node = fm.parse_rgce(rgce, 0, 0)
    self.assertTrue(node.full_column)
    self.assertEqual(fm.render_a1(node), '$A:$C')

  def test_iter_refs_walks_nested_formula(self):
    rgce = (b.ptg_ref(0, 0) + b.ptg_ref(1, 1) + b.ptg_add() +
            b.ptg_area(2, 3, 0, 0) + b.ptg_mul())
    node = fm.parse_rgce(rgce, 0, 0)
    self.assertEqual(len(list(fm.iter_refs(node))), 3)


if __name__ == '__main__':
  unittest.main()
