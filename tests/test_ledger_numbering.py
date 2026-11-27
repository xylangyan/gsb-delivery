"""
    test_ledger_numbering
    ~~~~~~~~~~~~~~~~~~~~~

    Tests for the numbering pool: rounds, reservations, late releases, generations, reopen.
"""
import pytest

from ulid import consts
from ulid.ledger import ControlledClock, NumberingService, ReservationState, Window, WindowKind
from ulid.ulid import ULID


def make_ulid(position):
    return ULID(position.to_bytes(16, byteorder='big'))


def test_hold_uses_clock_millis_and_returns_token():
    clock = ControlledClock(1234)
    numbering = NumberingService(clock)
    token, value = numbering.hold('r1', tag='a')
    assert value.timestamp().int == 1234
    assert numbering.reservation(token).state is ReservationState.HELD


def test_hold_after_rollback_uses_earlier_millis():
    clock = ControlledClock(5000)
    numbering = NumberingService(clock)
    clock.rollback(2000)
    _token, value = numbering.hold('r1')
    assert value.timestamp().int == 3000


def test_hold_inside_millisecond_window():
    numbering = NumberingService(ControlledClock(1000))
    window = Window.millisecond(100, 200)
    _token, value = numbering.hold('r1', window)
    assert 100 <= value.timestamp().int <= 200


def test_hold_inside_suffix_window():
    numbering = NumberingService(ControlledClock(1000))
    window = Window.suffix(100, 100, 5, 9)
    _token, value = numbering.hold('r1', window)
    assert value.timestamp().int == 100
    assert 5 <= value.randomness().int <= 9


def test_hold_in_huge_window_does_not_enumerate_and_respects_guard():
    numbering = NumberingService(ControlledClock(100))
    huge = Window(
        WindowKind.SUFFIX, lower_millis=100, upper_millis=100,
        lower_suffix=0, upper_suffix=consts.MAX_RANDOMNESS.int)
    half = 1 << 79
    token, value = numbering.hold('r1', huge, guard=lambda position: (position & consts.MAX_RANDOMNESS.int) < half)
    assert value.randomness().int < half


def test_positions_are_unique_within_a_round():
    numbering = NumberingService(ControlledClock(100))
    window = Window.suffix(100, 100, 0, 32)
    seen = set()
    for _ in range(8):
        _token, value = numbering.hold('r1', window)
        seen.add(value.int)
    assert len(seen) == 8


def test_release_frees_position_and_token_is_single_use():
    numbering = NumberingService(ControlledClock(100))
    token, value = numbering.hold_position('r1', 7, 'a')
    assert numbering.release(token) is True
    assert numbering.release(token) is False
    _new_token, new_value = numbering.hold_position('r1', 7, 'a')
    assert new_value == value


def test_later_round_supersedes_and_late_release_keeps_current_holder():
    numbering = NumberingService(ControlledClock(100))
    first, _first_value = numbering.hold_position('r1', 9, 'a')
    second, second_value = numbering.hold_position('r2', 9, 'a')
    assert second_value.int == 9
    assert numbering.reservation(first).state is ReservationState.SUPERSEDED
    assert numbering.reservation(second).state is ReservationState.HELD
    assert numbering.release(first) is False
    assert numbering.reservation(second).state is ReservationState.HELD


def test_issued_positions_survive_reopen_but_holds_do_not():
    numbering = NumberingService(ControlledClock(100))
    hold_token, _hold_value = numbering.hold_position('r1', 1, 'a')
    issue_token, _issue_value = numbering.hold_position('r2', 2, 'b')
    numbering.issue(7, 'r2')
    reopened = numbering.reopen()
    assert reopened.issued_generations() == (7,)
    assert reopened.issued_positions(7) == frozenset({2})
    assert not reopened.has_round('r1')
    with pytest.raises(RuntimeError):
        reopened.hold_position('r3', 2, 'b', 2)
    assert reopened.hold_position('r3', 1, 'a')[1].int == 1


def test_hold_position_scans_to_first_free_in_small_interval():
    numbering = NumberingService(ControlledClock(100))
    numbering.hold_position('r1', 5, 'a', 5)
    _token, value = numbering.hold_position('r1', 5, 'b', 8)
    assert value.int == 6


def test_hold_position_raises_when_interval_full():
    numbering = NumberingService(ControlledClock(100))
    for position in range(4, 7):
        numbering.hold_position('r1', position, 'x', position)
    with pytest.raises(RuntimeError):
        numbering.hold_position('r1', 4, 'y', 6)
