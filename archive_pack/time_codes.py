"""Exact SEG-Y Rev 1 trace-header time arithmetic.

Rev 1 trace-header time fields are recorded in milliseconds, interpreted
through the single ``time_scalar`` field (bytes 215-216).  A positive scalar
is a multiplier, a negative scalar a divisor and zero means one.  This module
performs all arithmetic with :class:`~fractions.Fraction` so a value is only
ever written when it has an *exact* integer representation under a legal
scalar.  There is deliberately no rounding.
"""

from fractions import Fraction

from segpy.field_types import Int16, NNInt16
from segpy.trace_header import ScalarFactor

from archive_pack.model import UnrepresentableTimeField

# Scalar codes in preference order.  Scalar one (whole milliseconds) is the
# simplest, then progressively finer divisors, then coarser multipliers.
SCALAR_CODES = (
    ScalarFactor.POS_1,
    ScalarFactor.NEG_1,
    ScalarFactor.NEG_10,
    ScalarFactor.NEG_100,
    ScalarFactor.NEG_1000,
    ScalarFactor.NEG_10000,
    ScalarFactor.POS_10,
    ScalarFactor.POS_100,
    ScalarFactor.POS_1000,
    ScalarFactor.POS_10000,
)

# The ten trace-header fields governed by time_scalar, mapped to whether the
# stored value is signed.  delay_recording_time and the static/lag fields are
# signed; uphole and mute fields are non-negative.
TIME_FIELD_SIGNED = {
    'uphole_time_at_source': False,
    'uphole_time_at_group': False,
    'source_static_correction': True,
    'group_static_correction': True,
    'total_static': True,
    'lag_time_a': True,
    'lag_time_b': True,
    'delay_recording_time': True,
    'mute_start_time': False,
    'mute_end_time': False,
}

TIME_FIELDS = tuple(TIME_FIELD_SIGNED)


def field_limits(field_name):
    """Return the inclusive ``(minimum, maximum)`` stored range for a field."""
    value_type = Int16 if TIME_FIELD_SIGNED[field_name] else NNInt16
    return value_type.MINIMUM, value_type.MAXIMUM


def scalar_multiplier(scalar_code):
    """Return the exact multiplier ``true_ms = stored * multiplier``."""
    scalar_code = int(scalar_code)
    if scalar_code == 0:
        return Fraction(1)
    if scalar_code > 0:
        return Fraction(scalar_code)
    return Fraction(1, -scalar_code)


def decode_time(stored_value, scalar_code):
    """Decode a stored integer into the true time in milliseconds."""
    return Fraction(int(stored_value)) * scalar_multiplier(scalar_code)


def encode_time(true_ms, scalar_code, field_name):
    """Encode a true millisecond time exactly, or return ``None``.

    ``None`` means the value cannot be represented without rounding or does
    not fit the field under this scalar; callers must try another scalar or
    report a lossless-delivery failure.
    """
    minimum, maximum = field_limits(field_name)
    multiplier = scalar_multiplier(scalar_code)
    stored = Fraction(true_ms) / multiplier
    if stored.denominator != 1:
        return None
    stored = stored.numerator
    if not minimum <= stored <= maximum:
        return None
    return stored


def read_true_times(trace_header):
    """Read all ten time fields from a source header as true millisecond values."""
    scalar_code = int(trace_header.time_scalar)
    return {
        name: decode_time(getattr(trace_header, name), scalar_code)
        for name in TIME_FIELDS
    }


def encode_times(true_times):
    """Choose one legal scalar and exactly encode every time field.

    Args:
        true_times: Mapping of field name to :class:`Fraction` millisecond
            values.  Must cover every entry of :data:`TIME_FIELDS`.

    Returns:
        A tuple of the chosen scalar code and a mapping of field name to the
        integer stored values.

    Raises:
        UnrepresentableTimeField: If no single legal scalar represents every
            field exactly within range, or a field is unrepresentable under
            every scalar.
    """
    missing = tuple(name for name in TIME_FIELDS if name not in true_times)
    if missing:
        raise ValueError("Missing time fields: {}".format(', '.join(missing)))

    per_field_scalars = {}
    for name in TIME_FIELDS:
        viable = [code for code in SCALAR_CODES
                  if encode_time(true_times[name], code, name) is not None]
        if not viable:
            name_, reason = _individual_failure(name, true_times[name])
            raise UnrepresentableTimeField(name_, true_times[name], reason)
        per_field_scalars[name] = viable

    common = [code for code in SCALAR_CODES
              if all(code in viable for viable in per_field_scalars.values())]
    if not common:
        name = min(per_field_scalars, key=lambda key: len(per_field_scalars[key]))
        requirements = ", ".join(
            "{} needs scalar(s) {}".format(
                field_name, [int(code) for code in viable])
            for field_name, viable in per_field_scalars.items())
        raise UnrepresentableTimeField(
            field_name=name,
            true_ms=true_times[name],
            reason=("no single time scalar can encode every field exactly; "
                    + requirements))

    scalar_code = common[0]
    stored_values = {
        name: encode_time(true_times[name], scalar_code, name)
        for name in TIME_FIELDS
    }
    return int(scalar_code), stored_values


def _individual_failure(name, value):
    """Explain why one field cannot be encoded under any scalar at all."""
    minimum, maximum = field_limits(name)
    limit_ms_low = Fraction(minimum)
    limit_ms_high = Fraction(maximum)
    if value < limit_ms_low or value > limit_ms_high:
        reason = (
            "true time {} ms is outside the field range {} to {} ms even "
            "with the finest scalar".format(value, limit_ms_low, limit_ms_high))
    else:
        reason = (
            "true time {} ms has no exact integer encoding under any legal "
            "time scalar {}".format(value, [int(c) for c in SCALAR_CODES]))
    return name, reason
