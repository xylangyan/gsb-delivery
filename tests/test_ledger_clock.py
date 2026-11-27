"""
    test_ledger_clock
    ~~~~~~~~~~~~~~~~~

    Tests for the controllable millisecond clock used to replay clock rollback.
"""
import pytest

from ulid.ledger import ControlledClock


def test_clock_starts_at_given_millis():
    clock = ControlledClock(1234)
    assert clock.millis == 1234
    assert clock.milliseconds() == 1234


def test_clock_defaults_to_epoch():
    assert ControlledClock().millis == 0


def test_clock_advances_forward():
    clock = ControlledClock(10)
    assert clock.advance(5) == 15


def test_clock_rolls_back_to_earlier_millis():
    clock = ControlledClock(1000)
    assert clock.rollback(400) == 600
    assert clock.millis == 600


def test_clock_set_allows_going_backwards():
    clock = ControlledClock(1000)
    assert clock.set(10) == 10


def test_clock_rejects_negative_time():
    clock = ControlledClock(10)
    with pytest.raises(ValueError):
        clock.set(-1)
    with pytest.raises(ValueError):
        clock.rollback(11)


def test_clock_rejects_negative_rollback_distance():
    with pytest.raises(ValueError):
        ControlledClock(10).rollback(-1)
