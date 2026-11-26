# -*- coding: utf-8 -*-
"""Offset search engine for the pre-work shift audit.

The full cartesian product of candidate offsets and historical anchors is
never replayed.  Each route keeps only the distinct local wall-clock
anchors implied by its anchor batches, and parsedatetime is a pure
function of (note, local anchor), so every distinct pair is evaluated once
and memoised.  The offset sweep itself is integer-second arithmetic plus
half-open interval overlap checks.
"""

import contextlib
import datetime
import time as _time

import parsedatetime as pdt

from .clock import (
    WALL_FOLD,
    WALL_GAP,
    classify_wall_clock,
    local_naive,
    local_to_utc,
)
from .model import (
    BookedRange,
    Collision,
    OffsetResult,
    OrderResult,
    REASON_CLOCK_FALLBACK,
    REASON_INVERTED,
    REASON_MISSING_CELL,
    REASON_NOT_A_RANGE,
    TIE_EARLIER,
    TIE_LATER,
    TIE_NONE,
)

def _struct_to_naive(struct):
    return datetime.datetime(*struct[:6])


@contextlib.contextmanager
def _frozen_localtime(frozen_wall):
    """Pin ``time.localtime`` to the recorded machine clock.

    A leftover note that cannot establish a date makes the stock library
    read the host clock.  Freezing it to the value preserved in the
    handover material makes every audit reproduce that original run
    instead of silently depending on today's host clock.
    """
    frozen = _time.struct_time((
        frozen_wall.year, frozen_wall.month, frozen_wall.day,
        frozen_wall.hour, frozen_wall.minute, frozen_wall.second,
        0, 0, -1))
    original = _time.localtime
    _time.localtime = lambda *args: frozen
    try:
        yield
    finally:
        _time.localtime = original


def merge_ranges(ranges):
    """Merge half-open ranges that touch or overlap.

    Adjacent intervals (one ends exactly when the next begins) describe
    one continuous real-time booking and are combined.
    """
    if not ranges:
        return ()
    ordered = sorted(ranges, key=lambda booked: booked.start_utc)
    merged = [ordered[0]]
    for booked in ordered[1:]:
        last = merged[-1]
        if booked.start_utc <= last.end_utc:
            merged[-1] = BookedRange(
                start_utc=last.start_utc,
                end_utc=max(last.end_utc, booked.end_utc),
                start_local=last.start_local,
                end_local=booked.end_local,
                start_wall=last.start_wall,
                end_wall=booked.end_wall,
                range_flag=last.range_flag)
        else:
            merged.append(booked)
    return tuple(merged)


class ScheduleAuditor(object):
    """Audit two routes over one shared integer-second offset range."""

    def __init__(self, config):
        self.config = config
        self._calendar = pdt.Calendar(
            constants=pdt.Constants(localeID=config.locale_id))
        self._zone = config.zone
        # evalRanges outcomes memoised by (note, local wall-clock anchor).
        self._parse_cache = {}
        # Leftover-anchor decisions depend only on the note text.
        self._leftover_cache = {}
        self._distinct_anchors = {
            route.route_id: self._distinct_local_anchors(route)
            for route in config.routes}

    # ------------------------------------------------------------------
    # Anchor preparation
    # ------------------------------------------------------------------
    def _distinct_local_anchors(self, route):
        distinct = []
        seen = set()
        for utc_anchor in route.anchors():
            wall = local_naive(utc_anchor, self._zone)
            if wall not in seen:
                seen.add(wall)
                distinct.append(wall)
        return tuple(distinct)

    def distinct_local_anchors(self, route_id):
        """The local wall-clock anchors actually fed to evalRanges."""
        return self._distinct_anchors[route_id]

    # ------------------------------------------------------------------
    # Stock-library evaluation
    # ------------------------------------------------------------------
    def _eval_note(self, note, local_anchor):
        key = (note, local_anchor)
        cached = self._parse_cache.get(key)
        if cached is not None:
            return cached

        with _frozen_localtime(self.config.machine_clock):
            start_struct, end_struct, flag = self._calendar.evalRanges(
                note, local_anchor.timetuple())

        result = (flag,
                  _struct_to_naive(start_struct),
                  _struct_to_naive(end_struct))
        self._parse_cache[key] = result
        return result

    def _leftover_loses_anchor(self, note):
        """Mirror evalRanges' leftover-note handling exactly.

        When the range regex matches only part of the note, the stock
        library re-parses the surrounding text; if that parse carries no
        date or time the supplied source time is dropped and the library
        subsequently reads the machine clock.  This method reproduces the
        same regex/context decision instead of guessing from the output.
        """
        cached = self._leftover_cache.get(note)
        if cached is not None:
            return cached

        ptc = self._calendar.ptc
        text = note.strip().lower()
        if ptc.rangeSep in text:
            text = text.replace(
                ptc.rangeSep, ' %s ' % ptc.rangeSep).replace('  ', ' ')

        match = None
        for cre in (ptc.CRE_TIMERNG1, ptc.CRE_TIMERNG2, ptc.CRE_TIMERNG4,
                    ptc.CRE_TIMERNG3, ptc.CRE_DATERNG1, ptc.CRE_DATERNG2,
                    ptc.CRE_DATERNG3):
            match = cre.search(text)
            if match is not None:
                break
        if match is None or match.group() == text:
            self._leftover_cache[note] = False
            return False

        leftover = '%s %s' % (
            text[:match.start()], text[match.end():])
        with _frozen_localtime(self.config.machine_clock):
            _, context = self._calendar.parse(
                leftover, self.config.machine_clock.timetuple(),
                pdt.VERSION_CONTEXT_STYLE)
        loses_anchor = not context.hasDateOrTime
        self._leftover_cache[note] = loses_anchor
        return loses_anchor

    def _wall_to_utc(self, wall, tie):
        kind = classify_wall_clock(wall, self._zone)
        if kind == WALL_GAP:
            return None, kind
        if kind == WALL_FOLD:
            if tie == TIE_EARLIER:
                # fold=0 is the first pass: the earlier real instant.
                return local_to_utc(wall, self._zone, fold=0), kind
            if tie == TIE_LATER:
                return local_to_utc(wall, self._zone, fold=1), kind
            return None, kind
        return local_to_utc(wall, self._zone, fold=0), kind

    def _build_range(self, flag, start_wall, end_wall, order):
        start_utc, start_kind = self._wall_to_utc(start_wall, order.tie)
        end_utc, end_kind = self._wall_to_utc(end_wall, order.tie)

        if WALL_GAP in (start_kind, end_kind):
            return None, REASON_MISSING_CELL
        if WALL_FOLD in (start_kind, end_kind) and order.tie == TIE_NONE:
            return None, REASON_MISSING_CELL
        if start_utc >= end_utc:
            # Reversed or degenerate window; no overnight rollover exists
            # for the old orders, so the paper cannot be booked.
            return None, REASON_INVERTED

        return BookedRange(
            start_utc=start_utc,
            end_utc=end_utc,
            start_local=start_wall,
            end_local=end_wall,
            start_wall=start_kind,
            end_wall=end_kind,
            range_flag=flag), None

    def evaluate_order(self, route, order, utc_anchor, offset_seconds):
        """Evaluate one order against one shifted real UTC anchor."""
        shifted_utc = utc_anchor + datetime.timedelta(seconds=offset_seconds)
        local_anchor = local_naive(shifted_utc, self._zone)

        flag, start_wall, end_wall = self._eval_note(order.note, local_anchor)

        if not flag:
            # flag 0 has two stock-library shapes: the leftover branch
            # hands back time.localtime() verbatim, while a recognised but
            # unparseable window keeps the supplied source anchor.
            if start_wall == self.config.machine_clock:
                reason = REASON_CLOCK_FALLBACK
            else:
                reason = REASON_NOT_A_RANGE
            return OrderResult(
                route_id=route.route_id,
                order=order,
                anchor_utc=shifted_utc,
                local_anchor=local_anchor,
                offset_seconds=offset_seconds,
                bookable=False,
                reason=reason)

        if self._leftover_loses_anchor(order.note):
            # The window was silently rebuilt from the machine clock;
            # reproduce it but the old paper cannot be safely booked.
            return OrderResult(
                route_id=route.route_id,
                order=order,
                anchor_utc=shifted_utc,
                local_anchor=local_anchor,
                offset_seconds=offset_seconds,
                bookable=False,
                reason=REASON_CLOCK_FALLBACK)

        booked, reason = self._build_range(
            flag, start_wall, end_wall, order)
        if reason is not None:
            return OrderResult(
                route_id=route.route_id,
                order=order,
                anchor_utc=shifted_utc,
                local_anchor=local_anchor,
                offset_seconds=offset_seconds,
                bookable=False,
                reason=reason)

        return OrderResult(
            route_id=route.route_id,
            order=order,
            anchor_utc=shifted_utc,
            local_anchor=local_anchor,
            offset_seconds=offset_seconds,
            bookable=True,
            ranges=(booked,))

    # ------------------------------------------------------------------
    # Offset sweep
    # ------------------------------------------------------------------
    def run(self):
        """Evaluate every candidate offset; one OffsetResult per offset."""
        return [self._evaluate_offset(offset)
                for offset in self.config.candidate_offsets]

    def bookable_offsets(self):
        """Offsets where both routes book cleanly with no device clash."""
        return [result.offset_seconds for result in self.run()
                if result.bookable]

    def _evaluate_offset(self, offset_seconds):
        order_results = []
        for route in self.config.routes:
            for order, utc_anchor in zip(route.orders, route.anchors()):
                order_results.append(
                    self.evaluate_order(
                        route, order, utc_anchor, offset_seconds))

        bookable = all(result.bookable for result in order_results)
        collisions = []
        if bookable:
            collisions = self._find_collisions(order_results)
            if collisions:
                bookable = False

        earliest = collisions[0] if collisions else None
        return OffsetResult(
            offset_seconds=offset_seconds,
            bookable=bookable,
            order_results=tuple(order_results),
            collisions=tuple(collisions),
            earliest_collision=earliest)

    def _find_collisions(self, order_results):
        bookings = [(result.route_id, result.order, booked)
                    for result in order_results
                    for booked in result.ranges]

        collisions = []
        for index in range(len(bookings)):
            route_a, order_a, range_a = bookings[index]
            for route_b, order_b, range_b in bookings[index + 1:]:
                if order_a.device != order_b.device:
                    continue
                if not range_a.overlaps(range_b):
                    continue
                collisions.append(Collision(
                    route_a=route_a,
                    order_a=order_a,
                    route_b=route_b,
                    order_b=order_b,
                    range_a=range_a,
                    range_b=range_b,
                    overlap_start_utc=max(
                        range_a.start_utc, range_b.start_utc),
                    overlap_end_utc=min(range_a.end_utc, range_b.end_utc)))

        # Real collision start first; the original order number breaks
        # ties when two pairs collide at the same instant.
        collisions.sort(key=lambda collision: (
            collision.collision_start_utc,
            collision.order_a.order_id,
            collision.order_b.order_id))
        return collisions

    # ------------------------------------------------------------------
    # Single-offset inspection
    # ------------------------------------------------------------------
    def explain_offset(self, offset_seconds):
        """Full OffsetResult plus a per-order wall-clock conversion trace."""
        result = self._evaluate_offset(offset_seconds)
        trace = []
        for order_result in result.order_results:
            entry = {
                'route_id': order_result.route_id,
                'order_id': order_result.order.order_id,
                'device': order_result.order.device,
                'anchor_utc': order_result.anchor_utc,
                'local_anchor': order_result.local_anchor,
                'bookable': order_result.bookable,
                'reason': order_result.reason,
                'ranges': [],
            }
            for booked in order_result.ranges:
                entry['ranges'].append({
                    'start_local': booked.start_local,
                    'end_local': booked.end_local,
                    'start_utc': booked.start_utc,
                    'end_utc': booked.end_utc,
                    'start_wall': booked.start_wall,
                    'end_wall': booked.end_wall,
                    'range_flag': booked.range_flag,
                })
            trace.append(entry)
        return result, trace

    def merged_device_ranges(self, offset_result):
        """Per-device real UTC bookings, merged where segments connect."""
        per_device = {}
        for order_result in offset_result.order_results:
            for booked in order_result.ranges:
                per_device.setdefault(
                    order_result.order.device, []).append(booked)
        return {device: merge_ranges(ranges)
                for device, ranges in per_device.items()}
