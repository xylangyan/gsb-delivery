"""Tests for exact time-scalar arithmetic."""

from fractions import Fraction

import pytest

from segpy.trace_header import ScalarFactor

from archive_pack.time_codes import (
    TIME_FIELDS,
    decode_time,
    encode_time,
    encode_times,
    scalar_multiplier,
)
from archive_pack.model import UnrepresentableTimeField


def test_scalar_zero_is_identity():
    assert scalar_multiplier(ScalarFactor.UNKNOWN) == Fraction(1)


def test_positive_and_negative_scalars():
    assert scalar_multiplier(ScalarFactor.POS_10) == Fraction(10)
    assert scalar_multiplier(ScalarFactor.NEG_10) == Fraction(1, 10)


def test_whole_millisecond_uses_scalar_one():
    scalar, stored = encode_times(_all_times(Fraction(-1), Fraction(4000)))
    assert scalar == int(ScalarFactor.POS_1)
    assert stored["delay_recording_time"] == -1
    assert stored["mute_end_time"] == 4000


def test_half_millisecond_uses_divisor_ten_without_rounding():
    scalar, stored = encode_times(_all_times(Fraction(1, 2), Fraction(0)))
    assert scalar == int(ScalarFactor.NEG_10)
    assert stored["delay_recording_time"] == 5
    assert decode_time(5, scalar) == Fraction(1, 2)


def test_quarter_millisecond_uses_divisor_hundred():
    scalar, stored = encode_times(_all_times(Fraction(-3, 4), Fraction(0)))
    assert scalar == int(ScalarFactor.NEG_100)
    assert stored["delay_recording_time"] == -75
    assert decode_time(-75, scalar) == Fraction(-3, 4)


def test_non_terminating_value_is_rejected():
    with pytest.raises(UnrepresentableTimeField):
        encode_times(_all_times(Fraction(1, 3), Fraction(0)))


def test_conflicting_scalar_requirements_are_rejected():
    # A half-millisecond start needs a fine divisor; 4000 ms mute end cannot
    # survive that divisor inside an unsigned 16-bit field.
    times = _all_times(Fraction(1, 2), Fraction(4000))
    with pytest.raises(UnrepresentableTimeField) as exc_info:
        encode_times(times)
    assert "no single time scalar" in exc_info.value.reason


def test_field_range_violation_is_rejected_at_the_finest_scalar():
    # 4000 ms with divisor 10000 would need a 40 million stored value.
    assert encode_time(Fraction(4000), ScalarFactor.NEG_10000,
                       "mute_end_time") is None
    assert encode_time(Fraction(4000), ScalarFactor.POS_1,
                       "mute_end_time") == 4000


def test_signed_field_rejects_negative_in_non_negative_field():
    assert encode_time(Fraction(-1), ScalarFactor.POS_1,
                       "mute_end_time") is None
    assert encode_time(Fraction(-1), ScalarFactor.POS_1,
                       "delay_recording_time") == -1


def _all_times(delay_ms, mute_end_ms):
    times = {name: Fraction(0) for name in TIME_FIELDS}
    times["delay_recording_time"] = Fraction(delay_ms)
    times["mute_end_time"] = Fraction(mute_end_ms)
    return times
