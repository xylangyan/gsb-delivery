#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2016-2026 European Commission (JRC);
# Licensed under the EUPL (the 'Licence');
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at: http://ec.europa.eu/idabc/eupl

import os
import shutil
import tempfile
import unittest

import schedula as sh

import formulas
from formulas.excel import ERR_CIRCULAR, _values_equal
from formulas.ranges import Ranges

# Settlement report: an IF switches between two source blocks. The selected
# block spills over B1:B3 (B2 and B3 are empty cells), G1 references the
# spilled B3, and H1/H2 form a cycle that only exists in the branch not
# taken when F1 = 1.
CELLS = {
    'A1': 1, 'A2': 2, 'A3': 3,  # Source block A.
    'D1': 10, 'D2': 20,  # Source block B (smaller).
    'F1': 1,  # Branch switch.
    'B1:B3': '=IF(F1=1, A1:A3, D1:D2)',  # Spills onto B2 and B3.
    'G1': '=B3*2',  # References a spilled cell.
    'H1': '=IF(F1=1, 5, H2)',  # Cycle only in the branch not taken.
    'H2': '=H1+1',
}


def _value(v):
    if isinstance(v, Ranges):
        v = v.value
    if hasattr(v, 'ravel'):
        v = v.ravel().tolist()
        return v[0] if len(v) == 1 else v
    return v


def _values(sol):
    return {k: _value(v) for k, v in sol.items()}


class TestIncremental(unittest.TestCase):
    def setUp(self):
        self.model = formulas.ExcelModel().from_dict(CELLS).finish(
            complete=False, circular=True
        )
        self.inc = self.model.incremental()

    def full_recalc(self, inputs=None):
        model = formulas.ExcelModel().from_dict(CELLS).finish(
            complete=False, circular=True
        )
        return {
            k: v for k, v in model.calculate(inputs).items()
            if not isinstance(k, sh.Token)
        }

    def check_full_recalc(self, inputs):
        # Values, errors, and unaffected parts of the current state must
        # correspond to a complete recalculation with the same inputs.
        expected = self.full_recalc(inputs)
        self.assertEqual(set(expected), set(self.inc.solution))
        for k, v in expected.items():
            self.assertTrue(
                _values_equal(v, self.inc.solution[k]),
                'Mismatch on %s: %r != %r' % (k, v, self.inc.solution[k])
            )

    def test_first_batch_returns_all_tracked_cells(self):
        changed = self.inc.calculate()
        self.assertEqual({
            'A1', 'A2', 'A3', 'D1', 'D2', 'F1', 'A1:A3', 'D1:D2', 'B1:B3',
            'B3', 'G1', 'H1', 'H2'
        }, set(changed))
        self.assertEqual([1, 2, 3], _value(changed['B1:B3']))
        self.assertEqual(3, _value(changed['B3']))
        self.assertEqual(6, _value(changed['G1']))

    def test_shrunk_array_restores_underlying_cells(self):
        self.inc.calculate()
        changed = _values(self.inc.calculate({'F1': 0}))
        # The array shrunk to two rows: B3 reverts to its underlying empty
        # value and G1 is refreshed accordingly.
        self.assertIs(sh.EMPTY, changed['B3'])
        self.assertEqual([10, 20, sh.EMPTY], changed['B1:B3'])
        self.assertEqual(0, changed['G1'])
        # The cycle is now in the taken branch.
        self.assertIs(ERR_CIRCULAR, changed['H1'])
        self.assertIs(ERR_CIRCULAR, changed['H2'])

    def test_cycle_in_untaken_branch(self):
        self.inc.calculate({'F1': 0})
        self.assertIs(ERR_CIRCULAR, _value(self.inc.solution['H1']))
        # The correction makes the cycle exist only in the branch not taken:
        # it is handled according to the actual evaluation of the batch.
        changed = _values(self.inc.calculate({'F1': 1}))
        self.assertEqual(5, changed['H1'])
        self.assertEqual(6, changed['H2'])

    def test_old_branch_changes_do_not_invalidate(self):
        self.inc.calculate()
        changed = self.inc.calculate({'D1': 100})
        # D1 belongs to the branch not taken: only D1 itself is refreshed.
        self.assertEqual({'D1', 'D1:D2'}, set(changed))
        changed = self.inc.calculate({'A2': 22})
        # A2 belongs to the taken branch, but B3 and G1 do not change.
        self.assertEqual({'A2', 'A1:A3', 'B1:B3'}, set(changed))

    def test_noop_correction_refreshes_nothing(self):
        self.inc.calculate()
        self.assertEqual({}, self.inc.calculate({'A2': 2}))

    def test_state_matches_full_recalculation(self):
        inputs = {}
        for batch in ({}, {'F1': 0}, {'F1': 1}, {'D1': 100}, {'A2': 22}):
            self.inc.calculate(batch)
            inputs.update(batch)
            self.check_full_recalc(inputs or None)

    def test_outputs_restriction(self):
        inc = self.model.incremental(outputs=['G1'])
        changed = inc.calculate()
        self.assertEqual({'G1'}, set(changed) - {'B1:B3', 'B3'} - {
            'A1', 'A2', 'A3', 'D1', 'D2', 'F1', 'A1:A3', 'D1:D2'
        })
        self.assertNotIn('H1', changed)
        changed = _values(inc.calculate({'F1': 0}))
        self.assertEqual(0, changed['G1'])
        self.assertNotIn('H1', changed)
        # Untracked cells are not part of the state.
        self.assertNotIn('H1', inc.solution)


class TestIncrementalWorkbook(unittest.TestCase):
    def setUp(self):
        self.dirpath = tempfile.mkdtemp()
        self.fpath = os.path.join(self.dirpath, 'report.xlsx')
        import openpyxl
        from openpyxl.worksheet.formula import ArrayFormula
        book = openpyxl.Workbook()
        ws = book.active
        ws.title = 'DATA'
        ws['A1'], ws['A2'], ws['A3'] = 1, 2, 3
        ws['D1'], ws['D2'] = 10, 20
        ws['F1'] = 1
        ws['B1'] = ArrayFormula('B1:B3', '=IF(F1=1, A1:A3, D1:D2)')
        ws['G1'] = '=B3*2'
        ws['H1'] = '=IF(F1=1, 5, H2)'
        ws['H2'] = '=H1+1'
        book.save(self.fpath)
        self.prefix = "'[report.xlsx]DATA'!"

    def tearDown(self):
        shutil.rmtree(self.dirpath, ignore_errors=True)

    def test_workbook_batches(self):
        model = formulas.ExcelModel().loads(self.fpath).finish(circular=True)
        inc = model.incremental()
        ref = lambda k: '%s%s' % (self.prefix, k)
        changed = _values(inc.calculate())
        self.assertEqual([1, 2, 3], changed[ref('B1:B3')])
        self.assertEqual(3, changed[ref('B3')])
        self.assertEqual(6, changed[ref('G1')])
        self.assertEqual(5, changed[ref('H1')])
        # Shrink the array: the covered cell reverts to its underlying value.
        changed = _values(inc.calculate({ref('F1'): 0}))
        self.assertIs(sh.EMPTY, changed[ref('B3')])
        self.assertEqual(0, changed[ref('G1')])
        self.assertIs(ERR_CIRCULAR, changed[ref('H1')])
        # Switch the branch back: the cycle is only in the branch not taken.
        changed = _values(inc.calculate({ref('F1'): 1}))
        self.assertEqual(3, changed[ref('B3')])
        self.assertEqual(5, changed[ref('H1')])
        # Changes on old dependencies do not invalidate the new branch.
        changed = inc.calculate({ref('D1'): 100})
        self.assertNotIn(ref('B1:B3'), changed)
        self.assertNotIn(ref('G1'), changed)
        # The state corresponds to a full recalculation with the same inputs.
        fresh = formulas.ExcelModel().loads(self.fpath).finish(circular=True)
        expected = fresh.calculate({ref('F1'): 1, ref('D1'): 100})
        for k, v in expected.items():
            if isinstance(k, sh.Token):
                continue
            self.assertTrue(
                _values_equal(v, inc.solution[k]),
                'Mismatch on %s' % k
            )


if __name__ == '__main__':
    unittest.main()
