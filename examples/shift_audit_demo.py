# -*- coding: utf-8 -*-
"""Worked example of the pre-work shift audit.

Run with the project virtualenv::

    .venv/bin/python examples/shift_audit_demo.py

It reproduces the two headline collector quirks, sweeps a shared
integer-second offset range over two routes, and reports the bookable
offsets, the earliest collision pair and the wall-clock conversion that
acted on each booking.
"""

import datetime
import zoneinfo

from shiftaudit import (
    AnchorBatch,
    AuditConfig,
    Order,
    Route,
    ScheduleAuditor,
    TIE_EARLIER,
    WALL_FOLD,
    WALL_GAP,
)


def show_run(auditor):
    print('bookable offsets:', auditor.bookable_offsets())
    for result in auditor.run():
        verdict = 'BOOKABLE' if result.bookable else 'rejected'
        print('=== offset %+d seconds: %s ==='
              % (result.offset_seconds, verdict))
        for order_result in result.order_results:
            if order_result.bookable:
                booked = order_result.ranges[0]
                print('  %-7s %-8s %s -> %s UTC  (wall: %s/%s)'
                      % (order_result.order.order_id,
                         order_result.route_id,
                         booked.start_utc,
                         booked.end_utc,
                         booked.start_wall,
                         booked.end_wall))
            else:
                print('  %-7s %-8s UNBOOKABLE: %s'
                      % (order_result.order.order_id,
                         order_result.route_id,
                         order_result.reason))
        if result.earliest_collision is not None:
            collision = result.earliest_collision
            print('  earliest collision: %s (%s) vs %s (%s) at %s UTC'
                  % (collision.order_a.order_id, collision.route_a,
                     collision.order_b.order_id, collision.route_b,
                     collision.collision_start_utc))
    print()


def main():
    ny = zoneinfo.ZoneInfo('America/New_York')
    # Machine clock value preserved from the original run's handover note.
    machine_clock = datetime.datetime(2026, 11, 27, 4, 7, 26)

    # One shared anchor batch: start, integer-second step, count.
    # 2006-08-28 16:30:15 UTC reads 12:30:15 local in New York.
    batch = AnchorBatch(
        start=datetime.datetime(2006, 8, 28, 16, 30, 15),
        step_seconds=86400,
        count=1)

    print('--- clean paperwork: two routes on the same collector ---')
    route_one = Route('sheet-1', [
        # The collector books the next-morning 07:45 window, not the
        # 9-11pm evening text written in front of it.
        Order('A-101', 'tomorrow 9-11pm then 7:45-8:15', 'collector-7'),
    ], [batch])
    route_two = Route('sheet-2', [
        # Both ends inherit the anchor's hour/minute/second.
        Order('B-201', 'August 29 - September 2, 2006', 'collector-7'),
    ], [batch])

    offsets = (-86400, -3600, 0, 3600, 86400)
    auditor = ScheduleAuditor(AuditConfig(
        routes=[route_one, route_two],
        zone=ny,
        candidate_offsets=offsets,
        machine_clock=machine_clock))
    show_run(auditor)

    print('--- inspection for offset 0, order A-101 ---')
    zero, trace = auditor.explain_offset(0)
    entry = trace[0]
    print('  real UTC anchor used:  ', entry['anchor_utc'])
    print('  local anchor to library:', entry['local_anchor'])
    print('  ranges actually taken:  ',
          [(str(rng['start_utc']), str(rng['end_utc']))
           for rng in entry['ranges']])
    print('  merged device bookings: ',
          {device: [(str(rng.start_utc), str(rng.end_utc))
                    for rng in ranges]
           for device, ranges in auditor.merged_device_ranges(zero).items()})
    print()

    print('--- paperwork that cannot be booked at any offset ---')
    bad_route = Route('sheet-3', [
        Order('C-301', '9-11pm (see foreman note)', 'collector-7'),
        Order('C-302', '5-3pm', 'collector-7'),
        Order('C-303', 'noon-midnight', 'collector-7'),
    ], [batch, batch, batch])
    bad_auditor = ScheduleAuditor(AuditConfig(
        [bad_route], ny, [0], machine_clock))
    for order_result in bad_auditor.run()[0].order_results:
        print('  %-7s %s' % (order_result.order.order_id,
                             order_result.reason))
    print()

    print('--- repeated and missing wall-clock cells (DST) ---')
    # 2006-10-29 11:00 local; 01:30 is a repeated fall-back wall clock.
    fold_batch = AnchorBatch(
        datetime.datetime(2006, 10, 29, 16, 0), 3600, 1)
    fold_route = Route(
        'fallback', [Order('F-1', '1:30-1:45', 'collector-3', TIE_EARLIER)],
        [fold_batch])
    fold_auditor = ScheduleAuditor(AuditConfig(
        [fold_route], ny, [0], machine_clock))
    fold_booked = fold_auditor.run()[0].order_results[0].ranges[0]
    print('  repeated 01:30 classified as', WALL_FOLD,
          '; tie=earlier books', fold_booked.start_utc, 'UTC')

    # 2006-04-02 11:00 local; 02:30 lies in the spring-forward gap.
    gap_batch = AnchorBatch(
        datetime.datetime(2006, 4, 2, 15, 0), 3600, 1)
    gap_route = Route(
        'spring', [Order('G-1', '2:30-2:45', 'collector-3')], [gap_batch])
    gap_result = ScheduleAuditor(AuditConfig(
        [gap_route], ny, [0], machine_clock)).run()[0].order_results[0]
    print('  gap 02:30 classified as', WALL_GAP,
          '; order result:', gap_result.reason)


if __name__ == '__main__':
    main()
