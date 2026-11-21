# -*- coding: UTF-8 -*-
#
# Copyright 2011-2019 by Dirk Gorissen, Stephen Rauch and Contributors
# All rights reserved.
# This file is part of the Pycel Library, Licensed under GPLv3 (the 'License')
# You may not use this work except in compliance with the License.
# You may obtain a copy of the Licence at:
#   https://www.gnu.org/licenses/gpl-3.0.en.html

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook

import pycel.lib.date_time as date_time
from pycel import ExcelCompiler
from pycel.clock import (
    ClockError,
    NaiveDatetimeError,
    SettlementClock,
    clock_dependencies,
    excel_date_serial,
)

SHANGHAI = ZoneInfo('Asia/Shanghai')
LOS_ANGELES = ZoneInfo('America/Los_Angeles')

# The instant given to both ledgers.  It is just after midnight in Shanghai
# and still the previous day in Los Angeles.
SHARED_INSTANT = dt.datetime(2026, 11, 22, 0, 30, tzinfo=SHANGHAI)


def build_ledger(path, sheet, label):
    """A ledger with clock formulas, a combined label, and a fixed total."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws['A1'] = '=TODAY()'
    ws['A2'] = '=NOW()'
    ws['A3'] = '=TODAY()+NOW()'
    ws['A4'] = f'="{label}: "&TEXT(TODAY(),"yyyy-mm-dd")&"/"&TEXT(NOW(),"hh:mm")'
    ws['A5'] = '=123.25'
    ws['A6'] = '=A5*2'
    # Formulas downstream of a clock cell.
    ws['B1'] = '=A1+1'
    ws['B2'] = '=A2+1'
    ws['B3'] = '=A3+1'
    ws['B4'] = '=A4&"!"'
    wb.save(path)
    return str(path)


@pytest.fixture(scope='module')
def ledgers(tmp_path_factory):
    base = tmp_path_factory.mktemp('settlement')
    return (
        build_ledger(base / 'ledger_a.xlsx', 'A', 'SHA'),
        build_ledger(base / 'ledger_b.xlsx', 'B', 'LAX'),
    )


@pytest.fixture
def ledger_pair(ledgers):
    sha_path, lax_path = ledgers
    sha = ExcelCompiler(sha_path)
    sha.set_settlement_clock(SHARED_INSTANT, 'Asia/Shanghai')
    lax = ExcelCompiler(lax_path)
    lax.set_settlement_clock(SHARED_INSTANT, 'America/Los_Angeles')
    return sha, lax


def test_per_evaluation_settlement_time(ledgers):
    sha_path, _ = ledgers
    compiler = ExcelCompiler(sha_path)
    assert compiler.settlement_clock is None

    shanghai = compiler.evaluate('A!A1', settlement_time=SHARED_INSTANT)
    assert shanghai == 46348

    # The next evaluation may pass another accounting zone for the same
    # instant; the earlier per-call clock did not become the default.
    los_angeles = compiler.evaluate(
        'A!A1', settlement_time=SettlementClock(SHARED_INSTANT, LOS_ANGELES))
    assert los_angeles == 46347
    # A clockless evaluation uses the system time, not the last per-call one.
    assert compiler.settlement_clock is None
    assert compiler.evaluate('A!A2') == pytest.approx(
        (dt.datetime.now() - dt.datetime(1899, 12, 30)).total_seconds() / 86400,
        abs=0.01)


def test_evaluation_clock_context_manager():
    clock = SettlementClock(SHARED_INSTANT, 'Asia/Shanghai')
    with clock:
        assert date_time.today() == 46348
        assert date_time.now() == pytest.approx(46348.0208333333)
    assert date_time.get_clock() is None
    assert date_time.today() == expected_today(
        dt.datetime.now(dt.timezone.utc), SHANGHAI)


def expected_today(when, timezone):
    return excel_date_serial(when.astimezone(timezone).date())


class CountingClock(SettlementClock):
    """Settlement clock that counts how often TODAY()/NOW() read it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = {'now': 0, 'today': 0}

    @property
    def serial(self):
        self.calls['now'] += 1
        return super().serial

    @property
    def today(self):
        self.calls['today'] += 1
        return super().today


def read(compiler, sheet, addresses=('A1', 'A2', 'A3', 'A4', 'A5', 'A6',
                                     'B1', 'B2', 'B3', 'B4')):
    return {address: compiler.evaluate(f'{sheet}!{address}')
            for address in addresses}


def test_shared_instant_is_different_day_in_the_two_timezones(ledger_pair):
    sha, lax = ledger_pair

    sha_today = sha.evaluate('A!A1')
    lax_today = lax.evaluate('B!A1')

    assert sha_today == expected_today(SHARED_INSTANT, SHANGHAI)
    assert lax_today == expected_today(SHARED_INSTANT, LOS_ANGELES)
    assert sha_today == lax_today + 1
    assert sha.evaluate('A!A2') == pytest.approx(46348.0208333333)
    assert lax.evaluate('B!A2') == pytest.approx(46347.3541666667)


def test_interleaved_reads_keep_independent_clocks(ledger_pair):
    sha, lax = ledger_pair
    lax_values = read(lax, 'B')
    sha_values = read(sha, 'A')
    assert lax_values == read(lax, 'B')
    assert sha_values == read(sha, 'A')
    assert sha_values['A1'] == lax_values['A1'] + 1


def test_clock_labels_and_fixed_summaries(ledger_pair):
    sha, lax = ledger_pair
    assert sha.evaluate('A!A4').startswith('SHA: 2026-11-22/00:30')
    assert lax.evaluate('B!A4').startswith('LAX: 2026-11-21/08:30')
    assert sha.evaluate('A!A5') == 123.25
    assert sha.evaluate('A!A6') == 246.5
    assert lax.evaluate('B!A6') == 246.5


def test_same_day_now_refresh_only(ledger_pair, monkeypatch):
    sha, lax = ledger_pair
    read(sha, 'A')
    read(lax, 'B')
    lax_before = read(lax, 'B')

    clock = CountingClock(
        SHARED_INSTANT + dt.timedelta(hours=1), 'Asia/Shanghai')
    sha.set_settlement_clock(clock)
    clock.calls.update(now=0, today=0)
    sha_after = read(sha, 'A')

    assert sha_after['A1'] == sha_after['B1'] - 1  # TODAY did not move
    assert sha_after['A2'] == pytest.approx(46348.0625)
    assert sha_after['B2'] == pytest.approx(46349.0625)
    assert clock.calls['now'] == 3  # A!A2, A!A3 and the A!A4 label
    assert clock.calls['today'] == 2  # A!A3 and the A!A4 label only

    # The fixed summary and the untouched ledger are unchanged.
    assert sha_after['A5'] == 123.25
    assert sha_after['A6'] == 246.5
    assert read(lax, 'B') == lax_before
    assert clock.calls['now'] == 3


def test_crossing_midnight_refreshes_today_and_downstream(ledger_pair, monkeypatch):
    sha, lax = ledger_pair
    read(sha, 'A')
    read(lax, 'B')
    lax_before = read(lax, 'B')

    # 2026-11-22 01:30 Shanghai + 22.5h -> 2026-11-23 00:00 Shanghai
    clock = CountingClock(
        SHARED_INSTANT + dt.timedelta(hours=23, minutes=30), 'Asia/Shanghai')
    sha.set_settlement_clock(clock)
    clock.calls.update(now=0, today=0)
    sha_after = read(sha, 'A')

    assert sha_after['A1'] == expected_today(
        SHARED_INSTANT + dt.timedelta(hours=23, minutes=30), SHANGHAI)
    assert sha_after['A1'] == 46349
    assert sha_after['B1'] == 46350
    assert sha_after['A2'] == pytest.approx(46349.0)
    assert clock.calls['today'] == 3  # A!A1, A!A3 and A!A4
    assert clock.calls['now'] == 3

    assert read(lax, 'B') == lax_before
    assert lax.evaluate('B!A1') == expected_today(
        SHARED_INSTANT, LOS_ANGELES)


def test_repeated_same_instant_recomputes_nothing(ledger_pair, monkeypatch):
    sha, lax = ledger_pair
    read(sha, 'A')
    read(lax, 'B')

    clock = CountingClock(SHARED_INSTANT, 'Asia/Shanghai')
    sha.set_settlement_clock(clock)
    sha.set_settlement_clock(SettlementClock(SHARED_INSTANT, 'Asia/Shanghai'))
    clock.calls.update(now=0, today=0)
    assert read(sha, 'A') == read(sha, 'A')
    assert clock.calls == {'now': 0, 'today': 0}


def test_naive_instant_is_rejected_and_previous_period_still_reads(ledger_pair):
    sha, lax = ledger_pair
    before = read(sha, 'A')

    with pytest.raises(NaiveDatetimeError):
        sha.set_settlement_clock(dt.datetime(2026, 11, 22, 0, 30))
    with pytest.raises(NaiveDatetimeError):
        sha.settlement_time = dt.datetime(2026, 11, 22, 0, 30)
    with pytest.raises(ClockError):
        ExcelCompiler(settlement_time=dt.datetime(2026, 11, 22, 0, 30))

    assert read(sha, 'A') == before
    assert sha.settlement_clock.serial == pytest.approx(46348.0208333333)
    assert lax.evaluate('B!A2') == pytest.approx(46347.3541666667)

    # The rejected value did not invalidate any cached cell.
    assert sha.cell_map['A!A1'].value == before['A1']
    assert sha.cell_map['A!B1'].value == before['B1']


def test_without_a_clock_the_system_time_is_used(ledgers, monkeypatch):
    sha_path, _ = ledgers
    fixed = dt.datetime(2026, 6, 1, 12, 0, 0)

    class FixedSystemDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            snapped = FixedSystemDatetime(
                fixed.year, fixed.month, fixed.day,
                fixed.hour, fixed.minute, fixed.second, fixed.microsecond)
            return snapped if tz is None else snapped.replace(tzinfo=tz)

    monkeypatch.setattr('pycel.clock.dt.datetime', FixedSystemDatetime)
    compiler = ExcelCompiler(sha_path)
    assert compiler.settlement_clock is None
    assert compiler.evaluate('A!A1') == 46174
    assert compiler.evaluate('A!A2') == pytest.approx(46174.5)
    assert compiler.evaluate('A!A5') == 123.25


def test_account_timezone_can_be_configured_without_a_clock(ledgers):
    sha_path, _ = ledgers
    compiler = ExcelCompiler(sha_path, timezone='Asia/Shanghai')
    assert compiler.settlement_clock is None
    assert str(compiler.timezone) == 'Asia/Shanghai'

    # A bare wall time still has to say which zone it is in.
    with pytest.raises(NaiveDatetimeError):
        compiler.set_settlement_clock('2026-11-22T00:30:00')

    # An absolute instant is read in the ledger's accounting timezone.
    compiler.set_settlement_clock(
        '2026-11-21T16:30:00+00:00')
    assert compiler.evaluate('A!A1') == 46348
    assert compiler.evaluate('A!A2') == pytest.approx(46348.0208333333)


def test_clock_serialization_round_trip(ledger_pair, tmp_path):
    sha, _ = ledger_pair
    before = read(sha, 'A')

    # A compiled workbook does not carry the runtime settlement clock, so a
    # freshly loaded instance behaves like an ordinary, clockless compiler.
    sha.to_file(str(tmp_path / 'ledger.yaml'), file_types=('yml', ))
    reloaded = ExcelCompiler.from_file(str(tmp_path / 'ledger.yaml'))
    assert reloaded.settlement_clock is None
    assert reloaded.evaluate('A!A5') == before['A5']

    import pickle
    unpickled = pickle.loads(pickle.dumps(sha))
    assert unpickled.settlement_clock is None
    assert unpickled.evaluate('A!A5') == before['A5']


def test_clock_dependency_detection():
    assert clock_dependencies('today() + 1') == {'today'}
    assert clock_dependencies('now() + today()') == {'now', 'today'}
    assert clock_dependencies('"TODAY()" & _C_("A!A1")') == set()
    assert clock_dependencies('sum_(now(), _C_("A!A1"))') == {'now'}
    assert clock_dependencies('') == set()


def test_settlement_clock_serial_helpers():
    clock = SettlementClock(SHARED_INSTANT, 'Asia/Shanghai')
    assert clock.serial == pytest.approx(46348.0208333333)
    assert clock.today == 46348
    assert clock.local == dt.datetime(2026, 11, 22, 0, 30)
    assert clock.local_datetime == clock.datetime
    assert clock.astimezone('America/Los_Angeles').today == 46347
    assert clock.advance(dt.timedelta(hours=1)).today == 46348
    assert clock.replace(timezone='UTC').serial == pytest.approx(
        46347.6875)
    assert clock == SHARED_INSTANT
    assert clock == SHARED_INSTANT.astimezone(dt.timezone.utc)
    assert clock.close_enough(SHARED_INSTANT)
    assert not clock.close_enough(SHARED_INSTANT + dt.timedelta(hours=1))
