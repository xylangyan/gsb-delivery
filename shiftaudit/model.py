# -*- coding: utf-8 -*-
"""Data model for the pre-work shift audit."""

import datetime
from dataclasses import dataclass


# evalRanges returned flag 0 and the text never named a window.
REASON_NOT_A_RANGE = 'not_a_range'
# A leftover note could not establish a date and the stock library fell
# back to the machine clock; the recorded clock value is reproduced.
REASON_CLOCK_FALLBACK = 'clock_fallback'
# A wall-clock cell is missing: a spring-forward gap, or a repeated
# (fall-back) wall clock with no work-order tie rule on the order.
REASON_MISSING_CELL = 'missing_wall_clock_cell'
# evalRanges handed back an inverted date or time (end at or before
# start); old orders have no automatic overnight rollover rule.
REASON_INVERTED = 'inverted_window'

UNBOOKABLE_REASONS = (
    REASON_NOT_A_RANGE,
    REASON_CLOCK_FALLBACK,
    REASON_MISSING_CELL,
    REASON_INVERTED,
)

# Work-order tie rules for a repeated local wall clock.
TIE_EARLIER = 'earlier'
TIE_LATER = 'later'
TIE_NONE = 'none'


@dataclass(frozen=True)
class Order:
    """One paper order line.

    ``note`` is handed verbatim to the stock English parsedatetime setup.
    ``device`` is the collection machine the order is booked against.
    ``tie`` resolves a repeated local wall clock (autumn fall-back) to the
    earlier or later real instant named by the work order.
    """

    order_id: str
    note: str
    device: str
    tie: str = TIE_NONE

    def __post_init__(self):
        if self.tie not in (TIE_EARLIER, TIE_LATER, TIE_NONE):
            raise ValueError('invalid tie rule: %r' % self.tie)


@dataclass(frozen=True)
class AnchorBatch:
    """A batch of real UTC anchors.

    Recorded compactly as a start instant, an integer-second step and a
    count.  Batches cross month and year boundaries, so anchors are built
    by plain second arithmetic rather than calendar math.
    """

    start: datetime.datetime
    step_seconds: int
    count: int

    def __post_init__(self):
        if self.step_seconds <= 0:
            raise ValueError('step_seconds must be positive')
        if self.count <= 0:
            raise ValueError('count must be positive')

    def anchors(self):
        start = self.start
        if start.tzinfo is not None:
            start = start.astimezone(datetime.timezone.utc).replace(
                tzinfo=None)
        step = datetime.timedelta(seconds=self.step_seconds)
        return tuple(start + step * index for index in range(self.count))


@dataclass(frozen=True)
class Route:
    """One schedule route.

    The flattened anchor batches must line up one-to-one with the route's
    orders: order ``i`` is parsed from anchor ``i``.  Order ids are never
    merged when the two routes meet.
    """

    route_id: str
    orders: tuple
    anchor_batches: tuple

    def __post_init__(self):
        object.__setattr__(self, 'orders', tuple(self.orders))
        object.__setattr__(self, 'anchor_batches', tuple(self.anchor_batches))

        ids = [order.order_id for order in self.orders]
        if len(ids) != len(set(ids)):
            raise ValueError(
                'duplicate order id on route %r' % self.route_id)

        total = sum(batch.count for batch in self.anchor_batches)
        if total != len(self.orders):
            raise ValueError(
                'route %r has %d orders but %d anchors'
                % (self.route_id, len(self.orders), total))

    def anchors(self):
        stream = []
        for batch in self.anchor_batches:
            stream.extend(batch.anchors())
        return tuple(stream)


@dataclass(frozen=True)
class AuditConfig:
    """Static inputs shared by every candidate offset."""

    routes: tuple
    zone: object
    candidate_offsets: tuple
    # Naive local wall-clock value that time.localtime() would have read
    # on the original run, preserved in the handover material.
    machine_clock: datetime.datetime
    locale_id: str = 'en_US'

    def __post_init__(self):
        object.__setattr__(self, 'routes', tuple(self.routes))
        offsets = tuple(int(value) for value in self.candidate_offsets)
        object.__setattr__(self, 'candidate_offsets', offsets)
        if self.machine_clock.tzinfo is not None:
            raise ValueError('machine_clock must be a naive local wall clock')


@dataclass(frozen=True)
class BookedRange:
    """The real UTC interval actually taken from one paper order.

    ``start_wall`` / ``end_wall`` record which wall-clock conversion acted
    at each end (``unique``, ``fold`` or ``gap``).
    """

    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_local: datetime.datetime
    end_local: datetime.datetime
    start_wall: str
    end_wall: str
    range_flag: int

    def overlaps(self, other):
        # Left-closed, right-open real-time intersection.
        return (self.start_utc < other.end_utc
                and other.start_utc < self.end_utc)


@dataclass(frozen=True)
class OrderResult:
    """Outcome of evaluating one order at one shifted anchor."""

    route_id: str
    order: Order
    anchor_utc: datetime.datetime
    local_anchor: datetime.datetime
    offset_seconds: int
    bookable: bool
    reason: object = None
    ranges: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'ranges', tuple(self.ranges))

    @property
    def key(self):
        return self.route_id, self.order.order_id


@dataclass(frozen=True)
class Collision:
    """A same-device overlap of two paper orders in real UTC time."""

    route_a: str
    order_a: Order
    route_b: str
    order_b: Order
    range_a: BookedRange
    range_b: BookedRange
    overlap_start_utc: datetime.datetime
    overlap_end_utc: datetime.datetime

    @property
    def collision_start_utc(self):
        return self.overlap_start_utc


@dataclass(frozen=True)
class OffsetResult:
    """Audit verdict for one candidate integer-second offset."""

    offset_seconds: int
    bookable: bool
    order_results: tuple
    collisions: tuple
    earliest_collision: object = None

    def __post_init__(self):
        object.__setattr__(self, 'order_results', tuple(self.order_results))
        object.__setattr__(self, 'collisions', tuple(self.collisions))

    def result_for(self, route_id, order_id):
        """The per-sheet outcome for one order on one route."""
        for result in self.order_results:
            if result.key == (route_id, order_id):
                return result
        raise KeyError((route_id, order_id))

    def ranges_for(self, route_id, order_id):
        """Real UTC ranges actually taken from one paper sheet."""
        return self.result_for(route_id, order_id).ranges

    def anchor_for(self, route_id, order_id):
        """Real UTC anchor used as the parsing start point."""
        return self.result_for(route_id, order_id).anchor_utc
