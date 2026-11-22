# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import decimal
import pytest
from decimal import Decimal
from tableschema import Reconciler
from tableschema import exceptions
from tableschema.reconcile import (
    ReconciliationError, NumberSyntaxError, AmountLimitError)


# Helpers

LOCALES = [
    {'decimalChar': '.', 'groupChar': ','},
    {'decimalChar': ',', 'groupChar': '.'},
]


def make_reconciler(**options):
    return Reconciler(locales=LOCALES, **options)


def make_bill(reconciler=None):
    reconciler = reconciler or make_reconciler()
    bill = reconciler.begin_bill()
    bill.add_row(1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'})
    bill.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
    return bill


# Tests

def test_authoritative_total_resolves_ambiguous_row():
    bill = make_bill()
    bill.set_base_total('6.40')
    rows = bill.rows
    assert rows[0]['status'] == 'confirmed'
    assert rows[0]['base'] == Decimal('2.4')
    assert rows[0]['fee'] == Decimal('1.2')
    assert rows[0]['rate'] == Decimal('0.5')
    assert rows[1]['status'] == 'confirmed'
    assert rows[1]['base'] == Decimal('4')
    assert bill.status == 'reconciled'


def test_unconfirmed_rows_reported_while_waiting_for_evidence():
    bill = make_bill()
    assert bill.status == 'pending'
    assert bill.unconfirmed == [1]
    row = bill.rows[0]
    assert row['status'] == 'unconfirmed'
    # No confirmed amount at a possibly wrong magnitude is emitted
    assert row['base'] is None
    assert row['fee'] is None
    assert sorted(c['base'] for c in row['candidates']) == [
        Decimal('2.4'), Decimal('2400')]


def test_ambiguity_kept_without_total():
    bill = make_bill()
    assert bill.close() == 'ambiguous'
    row = bill.rows[0]
    assert row['status'] == 'unconfirmed'
    assert row['base'] is None
    assert sorted(c['base'] for c in row['candidates']) == [
        Decimal('2.4'), Decimal('2400')]


def test_bounds_do_not_pick_a_reading():
    # Only the 2.4 reading fits the maximum; still not allowed to choose
    reconciler = make_reconciler(maximum='100')
    bill = reconciler.begin_bill()
    bill.add_row(1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'})
    assert bill.close() == 'ambiguous'
    assert bill.rows[0]['status'] == 'unconfirmed'
    assert bill.rows[0]['base'] is None


def test_determined_amount_exceeding_limit():
    reconciler = make_reconciler(maximum='100')
    bill = reconciler.begin_bill()
    bill.add_row(1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'})
    bill.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
    with pytest.raises(AmountLimitError) as excinfo:
        bill.set_base_total('2404.00')
    error = excinfo.value
    assert error.row_number == 1
    assert error.column == 'base'
    assert error.value == Decimal('2400')
    assert error.limit == Decimal('100')


def test_syntax_error_traces_row_and_column():
    bill = make_reconciler().begin_bill()
    with pytest.raises(NumberSyntaxError) as excinfo:
        bill.add_row(3, {'base': 'not-a-number', 'fee': '1.00', 'rate': '50%'})
    error = excinfo.value
    assert error.row_number == 3
    assert error.column == 'base'
    assert error.value == 'not-a-number'
    assert isinstance(error, exceptions.CastError)


def test_no_reading_matches_row_evidence():
    bill = make_reconciler().begin_bill()
    with pytest.raises(ReconciliationError) as excinfo:
        bill.add_row(2, {'base': '2.00', 'fee': '3.00', 'rate': '50%'})
    error = excinfo.value
    assert error.row_number == 2
    assert error.column == 'fee'


def test_no_reading_matches_authoritative_total():
    bill = make_bill()
    with pytest.raises(ReconciliationError):
        bill.set_base_total('7.00')


def test_bills_with_same_row_numbers_are_independent():
    reconciler = make_reconciler()
    first = reconciler.begin_bill()
    first.add_row(1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'})
    first.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
    second = reconciler.begin_bill()
    second.add_row(1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'})
    second.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
    first.set_base_total('6.40')
    assert first.rows[0]['base'] == Decimal('2.4')
    # The second bill got no evidence and stays ambiguous
    assert second.close() == 'ambiguous'
    assert second.rows[0]['status'] == 'unconfirmed'
    assert second.rows[0]['base'] is None


def test_duplicate_row_number_within_one_bill():
    bill = make_reconciler().begin_bill()
    bill.add_row(1, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
    with pytest.raises(exceptions.TableSchemaException):
        bill.add_row(1, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})


def test_low_precision_context_does_not_fake_balance():
    rate = '33.333333333333333333333333333333%'
    exact_fee = '0.99999999999999999999999999999999'
    with decimal.localcontext() as context:
        context.prec = 2
        reconciler = make_reconciler()
        # A rounded fee must not pose as balanced
        bill = reconciler.begin_bill()
        with pytest.raises(ReconciliationError):
            bill.add_row(1, {'base': '3.00', 'fee': '1.00', 'rate': rate})
        # The exact fee reconciles
        bill = reconciler.begin_bill()
        bill.add_row(1, {'base': '3.00', 'fee': exact_fee, 'rate': rate})
        assert bill.rows[0]['status'] == 'confirmed'
        assert bill.rows[0]['fee'] == Decimal(exact_fee)
        # Refunds (negative amounts) reconcile exactly as well
        bill = reconciler.begin_bill()
        bill.add_row(1, {'base': '-3.00', 'fee': '-' + exact_fee, 'rate': rate})
        assert bill.rows[0]['status'] == 'confirmed'
        assert bill.rows[0]['fee'] == Decimal('-' + exact_fee)


def test_low_precision_context_does_not_fake_total():
    with decimal.localcontext() as context:
        context.prec = 2
        reconciler = make_reconciler()
        bill = reconciler.begin_bill()
        bill.add_row(1, {'base': '1.005', 'fee': '0.5025', 'rate': '50%'})
        bill.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
        # 1.005 + 4.00 rounds to 5.0 under prec=2 but is exactly 5.005
        with pytest.raises(ReconciliationError):
            bill.set_base_total('5.00')
        bill = reconciler.begin_bill()
        bill.add_row(1, {'base': '1.005', 'fee': '0.5025', 'rate': '50%'})
        bill.add_row(2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'})
        bill.set_base_total('5.005')
        assert bill.status == 'reconciled'


def test_canonical_numbers_are_not_reinterpreted():
    # Values already cast (e.g. read back from a storage) keep their
    # canonical numeric value; no locale or percent semantics apply
    bill = make_reconciler().begin_bill()
    bill.add_row(1, {'base': Decimal('2.40'), 'fee': Decimal('1.20'),
                     'rate': Decimal('0.5')})
    row = bill.rows[0]
    assert row['status'] == 'confirmed'
    assert row['base'] == Decimal('2.4')
    assert row['fee'] == Decimal('1.2')


def test_currency_position_disambiguates_locale():
    locales = [
        {'decimalChar': '.', 'groupChar': ',', 'currency': '$',
         'currencyPosition': 'prefix'},
        {'decimalChar': ',', 'groupChar': '.', 'currency': '€',
         'currencyPosition': 'suffix'},
    ]
    reconciler = Reconciler(locales=locales)
    bill = reconciler.begin_bill()
    bill.add_row(1, {'base': '1.234,56€', 'fee': '617,28€', 'rate': '50%'})
    bill.add_row(2, {'base': '$1,234.56', 'fee': '$617.28', 'rate': '50%'})
    rows = bill.rows
    assert rows[0]['status'] == 'confirmed'
    assert rows[0]['base'] == Decimal('1234.56')
    assert rows[1]['status'] == 'confirmed'
    assert rows[1]['fee'] == Decimal('617.28')


def test_reconcile_bill_sequential_submission():
    reconciler = make_reconciler()
    rows = [
        (1, {'base': '2,400', 'fee': '1,200', 'rate': '50%'}),
        (2, {'base': '4.00', 'fee': '2.00', 'rate': '50%'}),
    ]
    first = reconciler.reconcile_bill(rows, base_total='6.40')
    second = reconciler.reconcile_bill(rows)
    assert first.status == 'reconciled'
    assert first.rows[0]['fee'] == Decimal('1.2')
    assert second.status == 'ambiguous'
    assert second.rows[0]['base'] is None


def test_rows_cannot_be_added_after_sealing():
    bill = make_bill()
    bill.set_base_total('6.40')
    with pytest.raises(exceptions.TableSchemaException):
        bill.add_row(3, {'base': '1.00', 'fee': '0.50', 'rate': '50%'})


def test_missing_field_raises():
    bill = make_reconciler().begin_bill()
    with pytest.raises(exceptions.TableSchemaException):
        bill.add_row(1, {'base': '4.00', 'rate': '50%'})
