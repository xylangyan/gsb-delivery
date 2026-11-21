# -*- coding: UTF-8 -*-
#
# Copyright 2011-2019 by Dirk Gorissen, Stephen Rauch and Contributors
# All rights reserved. This file is part of the Pycel Library, Licensed
# under GPLv3 (the 'License'). You may not use this work except in compliance
# with the License. You may obtain a copy of the Licence at:
#   https://www.gnu.org/licenses/gpl-3.0.en.html

import pytest
from openpyxl import Workbook

from pycel import ExcelCompiler
from pycel.excelcompiler import _CellRange
from pycel.excelutil import (
    BatchCheckError,
    BatchValidationError,
    DIV0,
    excel_value_equals,
)


def build_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = 'S'

    # static inputs
    ws['A1'] = 0        # numeric zero, later changed to logical FALSE
    ws['A2'] = True     # logical input, transiently changed then restored
    ws['A3'] = 2        # members of a rectangular input range A3:A4
    ws['A4'] = 4

    # formulas
    ws['B1'] = '=A1'                  # normal check / summary
    ws['B2'] = '=1/A1'                # check returning #DIV/0! while A1 is 0/FALSE
    ws['B3'] = '=IF(A2,10,20)'        # output depending on the logical input
    ws['B4'] = '=B1+B3'               # two input branches reconverge here
    ws['B5'] = '=A3+A4'               # depends on the rectangular input

    other = wb.create_sheet('Other')
    other['X1'] = '=100'              # unrelated sheet cache, no S! precedent

    excel = ExcelCompiler(excel=wb)
    excel.register_inputs(('S!A1', 'S!A2', 'S!A3:A4'))
    return excel


@pytest.fixture
def excel():
    return build_workbook()


def test_excel_value_equals_distinguishes_excel_types():
    # numeric 0, logical FALSE and blanks are distinct in Excel
    assert excel_value_equals(0, 0)
    assert excel_value_equals(False, False)
    assert not excel_value_equals(0, False)
    assert not excel_value_equals(False, 0)
    assert not excel_value_equals(1, True)
    assert excel_value_equals(None, None)
    assert not excel_value_equals(None, '')
    assert not excel_value_equals('', None)
    assert excel_value_equals('x', 'x')
    # rectangular range values compare element by element
    assert excel_value_equals(((1, 2), (3, 4)), ((1, 2), (3, 4)))
    assert not excel_value_equals(((1, 2),), ((1, 0),))
    assert not excel_value_equals(((0,),), ((False,),))
    assert not excel_value_equals(((1, 2),), ((1, 2), (3, 4)))


def test_register_inputs_rejects_formula_cells():
    excel = build_workbook()
    with pytest.raises(BatchValidationError, match='formula cell'):
        excel.register_inputs('S!B1')

    with pytest.raises(BatchValidationError, match='formula cell'):
        excel.register_inputs('S!B1:B2')


def test_batch_commit_applies_values_and_reuses_check_cache(excel):
    assert excel.evaluate('S!B1') == 0
    assert excel.evaluate('S!B2') == DIV0
    b2 = excel.cell_map['S!B2']

    with excel.batch_update(check_cells=('S!B1', 'S!B2')):
        excel.set_value('S!A1', 5)

    # inputs keep their committed values
    assert excel.cell_map['S!A1'].value == 5
    # check cells hold the committed results and are reused, not recomputed
    assert excel.evaluate('S!B1') == 5
    assert excel.evaluate('S!B2') == 0.2
    assert b2.value == 0.2
    # an as-yet unread downstream still evaluates correctly
    assert excel.evaluate('S!B4') == 15


def test_batch_check_error_names_failing_cell_and_rolls_back(excel):
    # pre-populate a valid summary cache and an unrelated sheet cache
    assert excel.evaluate('S!B1') == 0
    assert excel.evaluate('Other!X1') == 100
    b1 = excel.cell_map['S!B1']
    x1 = excel.cell_map['Other!X1']

    with pytest.raises(BatchCheckError) as exc_info:
        with excel.batch_update(check_cells=('S!B1', 'S!B2')):
            excel.set_value('S!A1', False)

    assert exc_info.value.failures == (('S!B2', DIV0),)

    # inputs restored to their pre-batch values
    assert excel.cell_map['S!A1'].value == 0
    # the successful preview read of B1 (FALSE) must not occupy the cache
    assert b1.needs_calc
    assert excel.evaluate('S!B1') == 0
    # unrelated worksheet cache is untouched
    assert x1.value == 100


def test_batch_invalidated_cell_recomputes_once_from_old_inputs(excel):
    excel.evaluate('S!B4')
    b4 = excel.cell_map['S!B4']
    assert b4.value == 10

    with pytest.raises(BatchCheckError):
        with excel.batch_update(check_cells=('S!B2',)):
            excel.set_value('S!A1', False)

    # B4 was invalidated by the preview; after rollback it recomputes with the
    # restored old inputs, not the rejected preview inputs
    assert b4.needs_calc
    assert excel.evaluate('S!B4') == 10
    assert excel.cell_map['S!A1'].value == 0


def test_batch_transient_change_restored_keeps_valid_cache(excel):
    # output depending on A2 is calculated before the batch
    assert excel.evaluate('S!B3') == 10
    b3 = excel.cell_map['S!B3']

    with pytest.raises(BatchCheckError):
        with excel.batch_update(check_cells=('S!B2',)):
            excel.set_value('S!A1', False)
            excel.set_value('S!A2', False)   # transient change
            excel.set_value('S!A2', True)    # restored logical value

    # A2 netted no change, so B3's valid cache survives the rejected batch
    assert b3.value == 10
    assert not b3.needs_calc
    assert excel.cell_map['S!A2'].value is True


def test_batch_zero_to_false_is_a_real_change(excel):
    excel.evaluate('S!B1')
    b1 = excel.cell_map['S!B1']
    assert b1.value == 0

    with excel.batch_update(check_cells=('S!B1',)):
        excel.set_value('S!A1', False)

    # the check no longer errors (FALSE coerces in division), and B1 is logical
    assert excel.evaluate('S!B1') is False
    assert excel.cell_map['S!A1'].value is False


def test_batch_blank_and_empty_string_are_distinct(excel):
    wb = Workbook()
    ws = wb.active
    ws.title = 'T'
    ws['A1'] = None
    ws['B1'] = '=A1'
    local = ExcelCompiler(excel=wb)
    local.register_inputs('T!A1')

    local.evaluate('T!B1')
    with local.batch_update(check_cells=('T!B1',)):
        local.set_value('T!A1', '')
    assert local.cell_map['T!A1'].value == ''
    assert local.evaluate('T!B1') == ''

    with local.batch_update(check_cells=('T!B1',)):
        local.set_value('T!A1', None)
    assert local.cell_map['T!A1'].value is None


def test_batch_rectangle_then_single_cell_override(excel):
    with excel.batch_update(check_cells=('S!B5',)):
        excel.set_value('S!A3:A4', ((7,), (8,)), set_as_range=True)
        excel.set_value('S!A3', 2)          # overrides one rectangle member

    assert excel.cell_map['S!A3'].value == 2
    assert excel.cell_map['S!A4'].value == 8
    assert excel.evaluate('S!B5') == 10


def test_batch_flattened_rectangle_assignment(excel):
    with excel.batch_update(check_cells=('S!B5',)):
        excel.set_value('S!A3:A4', ((7,), (8,)))

    assert excel.cell_map['S!A3'].value == 7
    assert excel.cell_map['S!A4'].value == 8
    assert excel.evaluate('S!B5') == 15


def test_batch_bad_shape_runs_no_checks(excel):
    excel.evaluate('S!B2')
    b2_value = excel.cell_map['S!B2'].value
    with pytest.raises(BatchValidationError, match='shape'):
        with excel.batch_update(check_cells=('S!B1', 'S!B2')):
            excel.set_value('S!A1', 9)
            excel.set_value('S!A3:A4', ((1,), (2,), (3,)), set_as_range=True)

    # nothing was applied and no check cell was evaluated
    assert excel.cell_map['S!A1'].value == 0
    assert excel.cell_map['S!B2'].value == b2_value


def test_batch_bad_address_runs_no_checks(excel):
    with pytest.raises(BatchValidationError, match='not found'):
        with excel.batch_update(check_cells=('S!B1',)):
            excel.set_value('S!A999', 1)
    assert excel.cell_map['S!A1'].value == 0


def test_batch_unregistered_address_rejected(excel):
    excel.evaluate('S!B1')
    with pytest.raises(BatchValidationError, match='registered'):
        with excel.batch_update(check_cells=('S!B1',)):
            excel.set_value('S!B1', 3)
    # formula cell left untouched, still evaluates to its original value
    assert excel.evaluate('S!B1') == 0


def test_batch_check_cell_range_rejected(excel):
    with pytest.raises(BatchValidationError, match='single cell'):
        with excel.batch_update(check_cells='S!B1:B2'):
            excel.set_value('S!A1', 1)


def test_batch_read_order_change_on_next_commit(excel):
    # first batch is rejected, leaving B1 invalidated but B3's cache valid
    excel.evaluate('S!B3')
    b3 = excel.cell_map['S!B3']

    with pytest.raises(BatchCheckError):
        with excel.batch_update(check_cells=('S!B1', 'S!B2')):
            excel.set_value('S!A1', False)

    # second commit reads the checks in a different order
    with excel.batch_update(check_cells=('S!B2', 'S!B1')):
        excel.set_value('S!A1', 4)

    assert b3.value == 10                 # previously valid cache still valid
    assert excel.evaluate('S!B1') == 4   # due-for-recalc cell uses new input
    assert excel.evaluate('S!B2') == 0.25


def test_batch_exception_inside_context_rolls_back(excel):
    with pytest.raises(RuntimeError):
        with excel.batch_update(check_cells=('S!B1',)):
            excel.set_value('S!A1', 6)
            raise RuntimeError('boom')
    assert excel.cell_map['S!A1'].value == 0


def test_batch_reconverging_branches_share_summary(excel):
    with excel.batch_update(check_cells=('S!B4',)):
        excel.set_value('S!A1', 3)
        excel.set_value('S!A2', False)
    # B4 = B1(=A1=3) + B3(IF(FALSE,..)=20)
    assert excel.evaluate('S!B4') == 23


def test_batch_direct_excel_error_read_still_works(excel):
    with excel.batch_update(check_cells=('S!B1',)):
        excel.set_value('S!A1', 0)       # no effective change
    assert excel.evaluate('S!B2') == DIV0


def test_batch_legacy_single_cell_operations_unchanged():
    excel = build_workbook()
    excel.set_value('S!A1', 5)
    assert excel.evaluate('S!B1') == 5
    assert excel.evaluate('S!B2') == 0.2
    excel.set_value('S!A1', 0)
    assert excel.evaluate('S!B2') == DIV0


def test_batch_nesting_rejected(excel):
    with excel.batch_update(check_cells=('S!B1',)):
        with pytest.raises(Exception, match='cannot be nested'):
            with excel.batch_update(check_cells=('S!B1',)):
                excel.set_value('S!A1', 1)


def test_batch_rejected_for_circular_workbook(fixture_xls_path_circular):
    circular = ExcelCompiler(fixture_xls_path_circular, cycles=True)
    with pytest.raises(Exception, match='non-circular'):
        with circular.batch_update(check_cells=()):
            circular.set_value('Sheet1!A2', 0.3)


def test_batch_range_node_is_a_cell_range(excel):
    assert isinstance(excel.cell_map['S!A3:A4'], _CellRange)


def test_batch_registration_persists_across_batches(excel):
    with excel.batch_update(check_cells=('S!B1',)):
        excel.set_value('S!A1', 2)
    assert excel.evaluate('S!B1') == 2

    # registration from the first fixture remains usable in a later batch
    with excel.batch_update(check_cells=('S!B1',)):
        excel.set_value('S!A1', 3)
    assert excel.evaluate('S!B1') == 3
