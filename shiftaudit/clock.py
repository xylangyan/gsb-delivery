# -*- coding: utf-8 -*-
"""Wall-clock conversion helpers.

Only the timezone handed to the audit is used for wall-clock work; the
machine's own timezone is never consulted here.  Ambiguous (repeated) and
non-existent (spring-forward gap) wall clocks are classified explicitly so
the engine can apply the work-order tie rule instead of guessing.
"""

import datetime


WALL_UNIQUE = 'unique'
WALL_FOLD = 'fold'
WALL_GAP = 'gap'


def _as_naive_utc(value):
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value
    raise TypeError('expected datetime.datetime, got %r' % type(value))


def utc_naive(value):
    """Return ``value`` as a naive UTC datetime."""
    return _as_naive_utc(value)


def utc_to_local(utc_anchor, zone):
    """Convert a (naive or aware) UTC datetime to an aware local datetime."""
    naive_utc = _as_naive_utc(utc_anchor)
    return naive_utc.replace(tzinfo=datetime.timezone.utc).astimezone(zone)


def local_naive(utc_anchor, zone):
    """Return the local wall-clock components of a UTC instant (naive)."""
    return utc_to_local(utc_anchor, zone).replace(tzinfo=None)


def local_to_utc(naive_wall, zone, fold=0):
    """Interpret a naive local wall clock as a real UTC instant.

    ``fold`` selects which side of a repeated (fall-back) wall clock is
    used.  For a non-existent (spring-forward) wall clock Python's fold
    convention yields the two instants bracketing the gap; callers decide
    which one a work order means via ``classify_wall_clock``.
    """
    if naive_wall.tzinfo is not None:
        raise ValueError('local wall clock must be naive')
    aware = naive_wall.replace(tzinfo=zone, fold=fold)
    return aware.astimezone(datetime.timezone.utc)


def classify_wall_clock(naive_wall, zone):
    """Classify a naive local wall clock.

    Returns one of:
      * ``WALL_UNIQUE`` - the wall clock names exactly one real instant
      * ``WALL_FOLD``   - the wall clock is repeated (autumn fall-back)
      * ``WALL_GAP``    - the wall clock never happens (spring forward)
    """
    if naive_wall.tzinfo is not None:
        raise ValueError('local wall clock must be naive')

    instant_fold0 = local_to_utc(naive_wall, zone, fold=0)
    instant_fold1 = local_to_utc(naive_wall, zone, fold=1)

    roundtrip = instant_fold0.astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive_wall:
        # A spring-forward gap maps to a wall clock on the far side of the
        # jump under either fold, so the requested clock never happens.
        return WALL_GAP

    if instant_fold0 != instant_fold1:
        # Two distinct real instants both round-trip to this wall clock.
        return WALL_FOLD

    return WALL_UNIQUE
