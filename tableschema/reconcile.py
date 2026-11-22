# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import re
import six
from decimal import Decimal
from fractions import Fraction
from itertools import product

from . import exceptions


# Module API

class Reconciler(object):
    """Reconcile expense rows whose numbers mix locale conventions.

    Historical expense CSVs mix number formats of two regions, so a value
    like ``2,400`` can be read as two different magnitudes. The reconciler
    keeps every plausible reading of each amount and only confirms a row
    when the account evidence pins it down:

        - row evidence: ``fee == base * rate`` (checked exactly)
        - bill evidence: an authoritative total of the billing bases

    # Arguments
        locales (dict[]): candidate locale descriptors, each supporting
            `decimalChar`, `groupChar` and optionally `currency` with
            `currencyPosition` ('prefix' or 'suffix'). Defaults to the two
            classic conventions: `.` decimal with `,` grouping and
            `,` decimal with `.` grouping.
        base_field (str): mapping key of the billing base
        fee_field (str): mapping key of the fee
        rate_field (str): mapping key of the rate (may use `%`)
        minimum/maximum (any): canonical numeric bounds. They are checked
            only against determined amounts and never used to pick one
            reading of an ambiguous value.

    """

    def __init__(self, locales=None, base_field='base', fee_field='fee',
                 rate_field='rate', minimum=None, maximum=None):
        specs = locales if locales is not None else _DEFAULT_LOCALES
        self.__locales = [_Locale(spec) for spec in specs]
        self.__base_field = base_field
        self.__fee_field = fee_field
        self.__rate_field = rate_field
        self.__minimum = None
        self.__maximum = None
        if minimum is not None:
            self.__minimum = _parse_canonical(minimum)
        if maximum is not None:
            self.__maximum = _parse_canonical(maximum)

    @property
    def locales(self):
        return list(self.__locales)

    @property
    def base_field(self):
        return self.__base_field

    @property
    def fee_field(self):
        return self.__fee_field

    @property
    def rate_field(self):
        return self.__rate_field

    @property
    def minimum(self):
        return self.__minimum

    @property
    def maximum(self):
        return self.__maximum

    def begin_bill(self):
        """Start a new independent bill reconciliation.

        # Returns
            BillReconciliation: fresh bill; row numbers are scoped to it

        """
        return BillReconciliation(self)

    def reconcile_bill(self, rows, base_total=None):
        """Reconcile one bill in a single call.

        # Arguments
            rows ((int, dict)[]): `(row_number, mapping)` pairs
            base_total (any): authoritative billing base total if trusted

        # Returns
            BillReconciliation: the sealed bill

        """
        bill = self.begin_bill()
        for row_number, mapping in rows:
            bill.add_row(row_number, mapping)
        if base_total is not None:
            bill.set_base_total(base_total)
        bill.close()
        return bill


class BillReconciliation(object):
    """Reconciliation state of a single bill.

    Rows are added as they are read; the authoritative base total may only
    be provided afterwards. While evidence is pending, unconfirmed rows are
    reported with their candidate readings and no confirmed amount.

    """

    def __init__(self, reconciler):
        self.__reconciler = reconciler
        self.__rows = []
        self.__row_numbers = set()
        self.__base_total = None
        self.__sealed = False

    @property
    def status(self):
        """Bill status: 'pending', 'reconciled' or 'ambiguous'.

        # Returns
            str: 'pending' while evidence may still arrive, otherwise
            'reconciled' when every row is confirmed else 'ambiguous'

        """
        if not self.__sealed:
            return 'pending'
        if all(row.confirmed is not None for row in self.__rows):
            return 'reconciled'
        return 'ambiguous'

    @property
    def base_total(self):
        """Authoritative billing base total once provided.

        # Returns
            Decimal/None: the canonical total

        """
        if self.__base_total is None:
            return None
        return _fraction_to_decimal(self.__base_total)

    @property
    def rows(self):
        """Per-row reconciliation results in the order rows were added.

        # Returns
            dict[]: each entry has `row_number`, `status` ('confirmed' or
            'unconfirmed'), `base`/`fee`/`rate` (Decimals when confirmed,
            otherwise None) and `candidates` (the surviving readings of an
            unconfirmed row)

        """
        result = []
        for row in self.__rows:
            entry = {'row_number': row.row_number}
            if row.confirmed is not None:
                entry['status'] = 'confirmed'
                entry['base'] = _fraction_to_decimal(row.confirmed.base)
                entry['fee'] = _fraction_to_decimal(row.confirmed.fee)
                entry['rate'] = _fraction_to_decimal(row.confirmed.rate)
                entry['candidates'] = []
            else:
                entry['status'] = 'unconfirmed'
                entry['base'] = None
                entry['fee'] = None
                entry['rate'] = None
                entry['candidates'] = [
                    {'base': _fraction_to_decimal(combo.base),
                     'fee': _fraction_to_decimal(combo.fee),
                     'rate': _fraction_to_decimal(combo.rate)}
                    for combo in row.combos]
            result.append(entry)
        return result

    @property
    def unconfirmed(self):
        """Numbers of the rows still waiting for disambiguating evidence.

        # Returns
            int[]: row numbers not confirmed yet

        """
        return [row.row_number for row in self.__rows
                if row.confirmed is None]

    def add_row(self, row_number, mapping):
        """Add one expense row of this bill.

        # Arguments
            row_number (int): row number within this bill
            mapping (dict): raw values keyed by the reconciler's field names

        # Raises
            NumberSyntaxError: a value can't be read under any locale
            ReconciliationError: no reading satisfies `fee == base * rate`
            AmountLimitError: the determined amount exceeds the bounds

        """
        if self.__sealed:
            message = 'Bill is sealed; no more rows can be added'
            raise exceptions.TableSchemaException(message)
        if row_number in self.__row_numbers:
            message = 'Row "%s" is already part of this bill' % row_number
            raise exceptions.TableSchemaException(message)
        reconciler = self.__reconciler
        raw = {}
        for field in (reconciler.base_field, reconciler.fee_field,
                      reconciler.rate_field):
            if field not in mapping:
                message = 'Row "%s" has no "%s" value' % (row_number, field)
                raise exceptions.TableSchemaException(message)
            raw[field] = mapping[field]
        bases = _field_candidates(
            reconciler, row_number, reconciler.base_field, raw)
        fees = _field_candidates(
            reconciler, row_number, reconciler.fee_field, raw)
        rates = _field_candidates(
            reconciler, row_number, reconciler.rate_field, raw)
        combos = []
        for base in bases:
            for fee in fees:
                for rate in rates:
                    if base * rate == fee:
                        combo = _Combo(base, fee, rate)
                        if combo not in combos:
                            combos.append(combo)
        if not combos:
            message = (
                'Row "%s" has no reading satisfying fee = base * rate '
                'for base "%s", fee "%s" and rate "%s"' % (
                    row_number,
                    raw[reconciler.base_field],
                    raw[reconciler.fee_field],
                    raw[reconciler.rate_field]))
            raise ReconciliationError(
                message, row_number=row_number, column=reconciler.fee_field)
        row = _Row(row_number, combos)
        self.__rows.append(row)
        self.__row_numbers.add(row_number)
        if len(combos) == 1:
            self.__confirm(row, combos[0])

    def set_base_total(self, total):
        """Provide the authoritative billing base total of this bill.

        Only readings whose bases add up to the total survive; a row is
        confirmed once every surviving reading agrees on its amounts.

        # Arguments
            total (any): canonical number (no locale/percent semantics)

        # Raises
            NumberSyntaxError: the total is not a canonical number
            ReconciliationError: no combination of readings adds up
            AmountLimitError: a newly determined amount exceeds the bounds

        """
        if self.__sealed:
            message = 'Bill is sealed; the base total can\'t be changed'
            raise exceptions.TableSchemaException(message)
        canonical = _parse_canonical(total)
        self.__base_total = canonical
        self.__sealed = True
        options = [row.combos for row in self.__rows]
        valid = []
        if options:
            for assignment in product(*options):
                if sum(combo.base for combo in assignment) == canonical:
                    valid.append(assignment)
        if not valid:
            message = (
                'No reading of this bill adds up to the authoritative '
                'base total "%s"' % _fraction_to_decimal(canonical))
            raise ReconciliationError(message)
        for index, row in enumerate(self.__rows):
            distinct = []
            for assignment in valid:
                combo = assignment[index]
                if combo not in distinct:
                    distinct.append(combo)
            row.combos = distinct
            if row.confirmed is None and len(distinct) == 1:
                self.__confirm(row, distinct[0])

    def close(self):
        """Seal the bill; unconfirmed rows stay ambiguous.

        # Returns
            str: the final bill status

        """
        self.__sealed = True
        return self.status

    # Private

    def __confirm(self, row, combo):
        row.confirmed = combo
        row.combos = [combo]
        reconciler = self.__reconciler
        amounts = ((reconciler.base_field, combo.base),
                   (reconciler.fee_field, combo.fee))
        for column, value in amounts:
            if reconciler.minimum is not None and value < reconciler.minimum:
                self.__raise_limit(row, column, value, 'minimum',
                                   reconciler.minimum)
            if reconciler.maximum is not None and value > reconciler.maximum:
                self.__raise_limit(row, column, value, 'maximum',
                                   reconciler.maximum)

    def __raise_limit(self, row, column, value, limit_name, limit):
        message = (
            'Row "%s" has determined %s "%s" which is out of the %s '
            'bound "%s"' % (
                row.row_number, column, _fraction_to_decimal(value),
                limit_name, _fraction_to_decimal(limit)))
        raise AmountLimitError(
            message, row_number=row.row_number, column=column,
            value=_fraction_to_decimal(value),
            limit=_fraction_to_decimal(limit))


class ReconciliationError(exceptions.TableSchemaException):
    """No reading of the data matches the account evidence.

    # Arguments
        message (str): error message
        row_number (int/None): row the error belongs to, if row-scoped
        column (str/None): column the error belongs to, if column-scoped

    """

    def __init__(self, message, row_number=None, column=None):
        self.__row_number = row_number
        self.__column = column
        super(ReconciliationError, self).__init__(message)

    @property
    def row_number(self):
        return self.__row_number

    @property
    def column(self):
        return self.__column


class NumberSyntaxError(exceptions.CastError):
    """A value can't be read as a number under any candidate locale.

    # Arguments
        message (str): error message
        row_number (int/None): row the value belongs to
        column (str/None): column the value belongs to
        value (any): the offending raw value

    """

    def __init__(self, message, row_number=None, column=None, value=None):
        self.__row_number = row_number
        self.__column = column
        self.__value = value
        super(NumberSyntaxError, self).__init__(message)

    @property
    def row_number(self):
        return self.__row_number

    @property
    def column(self):
        return self.__column

    @property
    def value(self):
        return self.__value


class AmountLimitError(exceptions.CastError):
    """A determined amount falls outside the configured bounds.

    # Arguments
        message (str): error message
        row_number (int/None): row the amount belongs to
        column (str/None): column the amount belongs to
        value (Decimal/None): the determined amount
        limit (Decimal/None): the violated bound

    """

    def __init__(self, message, row_number=None, column=None,
                 value=None, limit=None):
        self.__row_number = row_number
        self.__column = column
        self.__value = value
        self.__limit = limit
        super(AmountLimitError, self).__init__(message)

    @property
    def row_number(self):
        return self.__row_number

    @property
    def column(self):
        return self.__column

    @property
    def value(self):
        return self.__value

    @property
    def limit(self):
        return self.__limit


# Internal

_DEFAULT_LOCALES = [
    {'decimalChar': '.', 'groupChar': ','},
    {'decimalChar': ',', 'groupChar': '.'},
]
_RE_WHITESPACE = re.compile(r'\s')


class _Locale(object):

    def __init__(self, spec):
        self.name = spec.get('name')
        self.decimal_char = spec.get('decimalChar', '.')
        self.group_char = spec.get('groupChar', '')
        self.currency = spec.get('currency')
        self.currency_position = spec.get('currencyPosition')
        if self.group_char and self.group_char == self.decimal_char:
            message = 'Locale decimalChar and groupChar must differ'
            raise exceptions.TableSchemaException(message)
        if self.currency_position not in (None, 'prefix', 'suffix'):
            message = 'Unsupported currencyPosition: %s' % self.currency_position
            raise exceptions.TableSchemaException(message)


class _Combo(object):

    def __init__(self, base, fee, rate):
        self.base = base
        self.fee = fee
        self.rate = rate

    def __eq__(self, other):
        return (isinstance(other, _Combo) and
                self.base == other.base and
                self.fee == other.fee and
                self.rate == other.rate)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.base, self.fee, self.rate))


class _Row(object):

    def __init__(self, row_number, combos):
        self.row_number = row_number
        self.combos = combos
        self.confirmed = None


def _field_candidates(reconciler, row_number, column, raw):
    try:
        return _parse_candidates(raw[column], reconciler.locales)
    except NumberSyntaxError:
        message = 'Row "%s" has unreadable %s value "%s"' % (
            row_number, column, raw[column])
        raise NumberSyntaxError(
            message, row_number=row_number, column=column,
            value=raw[column])


def _parse_candidates(value, locales):
    # Canonical values (e.g. cast numbers read back from a storage) are
    # compared as-is; no locale or percent semantics are applied again.
    if isinstance(value, bool):
        pass
    elif isinstance(value, Fraction):
        return [value]
    elif isinstance(value, Decimal):
        return [Fraction(value)]
    elif isinstance(value, six.integer_types):
        return [Fraction(value)]
    elif isinstance(value, float):
        return [Fraction(Decimal(str(value)))]
    elif isinstance(value, six.string_types):
        return _parse_string(value, locales)
    message = 'Value "%s" is not a number' % (value,)
    raise NumberSyntaxError(message, value=value)


def _parse_string(text, locales):
    stripped = _RE_WHITESPACE.sub('', text)
    currencies = set()
    for locale in locales:
        if locale.currency and locale.currency in stripped:
            currencies.add(locale.currency)
    if len(currencies) > 1:
        message = 'Value "%s" mixes currency symbols' % (text,)
        raise NumberSyntaxError(message, value=text)
    currency = list(currencies)[0] if currencies else None
    candidates = []
    for locale in locales:
        if currency is not None and locale.currency != currency:
            continue
        candidate = _parse_with_locale(stripped, locale,
                                       currency_present=bool(currency))
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        message = 'Value "%s" can\'t be read under any locale' % (text,)
        raise NumberSyntaxError(message, value=text)
    return candidates


def _parse_with_locale(text, locale, currency_present=False):
    value = text
    if currency_present:
        currency = locale.currency
        if locale.currency_position == 'prefix':
            if not value.startswith(currency):
                return None
            value = value[len(currency):]
        elif locale.currency_position == 'suffix':
            if not value.endswith(currency):
                return None
            value = value[:-len(currency)]
        elif value.startswith(currency):
            value = value[len(currency):]
        elif value.endswith(currency):
            value = value[:-len(currency)]
        else:
            return None
    percent = False
    if value.endswith('%'):
        percent = True
        value = value[:-1]
    sign = 1
    if value.startswith('-'):
        sign = -1
        value = value[1:]
    elif value.startswith('+'):
        value = value[1:]
    if locale.group_char:
        int_pattern = r'(?:\d{1,3}(?:%s\d{3})+|\d+)'
        int_pattern %= re.escape(locale.group_char)
    else:
        int_pattern = r'\d+'
    pattern = r'^(%s)(?:%s(\d+))?$' % (
        int_pattern, re.escape(locale.decimal_char))
    match = re.match(pattern, value)
    if not match:
        return None
    int_part = match.group(1)
    if locale.group_char:
        int_part = int_part.replace(locale.group_char, '')
    result = Fraction(int(int_part), 1)
    frac_part = match.group(2)
    if frac_part:
        result += Fraction(int(frac_part), 10 ** len(frac_part))
    result *= sign
    if percent:
        result /= 100
    return result


def _parse_canonical(value):
    try:
        candidates = _parse_candidates(value, [
            _Locale({'decimalChar': '.', 'groupChar': ''})])
    except NumberSyntaxError:
        message = 'Value "%s" is not a canonical number' % (value,)
        raise NumberSyntaxError(message, value=value)
    if len(candidates) != 1:
        message = 'Value "%s" is not a canonical number' % (value,)
        raise NumberSyntaxError(message, value=value)
    return candidates[0]


def _fraction_to_decimal(value):
    sign = '-' if value < 0 else ''
    numerator = abs(value.numerator)
    denominator = value.denominator
    twos = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    fives = 0
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    if denominator != 1:
        message = 'Value "%s" has no finite decimal form' % (value,)
        raise exceptions.TableSchemaException(message)
    exponent = max(twos, fives)
    scaled = numerator * (2 ** (exponent - twos)) * (5 ** (exponent - fives))
    digits = str(scaled).rjust(exponent + 1, '0')
    if exponent:
        int_part = digits[:-exponent]
        frac_part = digits[-exponent:].rstrip('0')
        if frac_part:
            return Decimal('%s%s.%s' % (sign, int_part, frac_part))
        return Decimal('%s%s' % (sign, int_part))
    return Decimal('%s%s' % (sign, digits))
