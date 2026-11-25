require_relative "shift_reconciliation"

module ShiftReconciliation
  module SmallLog
    module_function

    def build
      Archive.new do |archive|
        archive.horizon(start: "2026-11-18T00:00:00Z", finish: "2026-11-22T23:59:59Z")

        day = IceCube::Schedule.new(Time.utc(2026, 11, 18, 9, 0, 0))
        archive.schedule(
          "DAY",
          day,
          extra_times: [Time.utc(2026, 11, 18, 14, 0, 0)]
        )
        archive.receipt(
          id: "R-BOTH",
          at: Time.utc(2026, 11, 18, 9, 5, 0),
          schedule: "DAY",
          tolerance: 6 * 60 * 60
        )
        archive.receipt(
          id: "R-AM-ONLY",
          at: Time.utc(2026, 11, 18, 9, 7, 0),
          schedule: "DAY",
          tolerance: 10 * 60
        )

        swap = IceCube::Schedule.new(Time.utc(2026, 11, 18, 18, 0, 0))
        archive.schedule(
          "SWAP",
          swap,
          extra_times: [Time.utc(2026, 11, 18, 19, 0, 0)]
        )
        archive.receipt(
          id: "R-SWAP",
          at: Time.utc(2026, 11, 18, 18, 30, 0),
          schedule: "SWAP",
          tolerance: 2 * 60 * 60
        )

        %w[SIM-A SIM-B].each do |schedule_id|
          schedule = IceCube::Schedule.new(Time.utc(2026, 11, 19, 9, 0, 0))
          archive.schedule(schedule_id, schedule)
        end
        archive.receipt(
          id: "R-SIM-1",
          at: Time.utc(2026, 11, 19, 9, 0, 5),
          schedule: "SIM-A",
          tolerance: 60
        )
        archive.receipt(
          id: "R-SIM-2",
          at: Time.utc(2026, 11, 19, 9, 0, 5),
          schedule: "SIM-B",
          tolerance: 60
        )

        duplicate = IceCube::Schedule.new(Time.utc(2026, 11, 19, 10, 0, 0))
        archive.schedule("DUP", duplicate)
        2.times do
          archive.receipt(
            id: "R-DUP",
            at: Time.utc(2026, 11, 19, 10, 0, 3),
            schedule: "DUP",
            tolerance: 60
          )
        end

        repeated = IceCube::Schedule.new(Time.utc(2026, 11, 20, 9, 0, 0))
        repeated.add_recurrence_rule(IceCube::Rule.daily)
        repeated.add_recurrence_rule(
          IceCube::Rule.weekly.day(
            :sunday, :monday, :tuesday, :wednesday, :thursday, :friday, :saturday
          )
        )
        archive.schedule(
          "RULE-DUP",
          repeated,
          horizon_start: "2026-11-20T00:00:00Z",
          horizon_end: "2026-11-20T23:59:59Z"
        )
        archive.receipt(
          id: "R-RULE-DUP",
          at: Time.utc(2026, 11, 20, 9, 0, 9),
          schedule: "RULE-DUP",
          tolerance: 60
        )

        cancel_schedule = IceCube::Schedule.new(Time.utc(2026, 11, 21, 9, 0, 0))
        cancel_schedule.add_recurrence_rule(IceCube::Rule.daily.count(2))
        archive.schedule(
          "CANCEL",
          cancel_schedule,
          exception_times: [Time.utc(2026, 11, 22, 9, 0, 0)]
        )
        archive.receipt(
          id: "R-CANCEL",
          at: Time.utc(2026, 11, 21, 9, 0, 4),
          schedule: "CANCEL",
          tolerance: 60
        )

        hard = IceCube::Schedule.new(Time.utc(2026, 11, 20, 12, 0, 0))
        archive.schedule("HARD", hard)
        archive.receipt(
          id: "R-PAPER",
          at: Time.utc(2026, 11, 20, 12, 0, 0),
          schedule: "HARD",
          tolerance: 60
        )
        archive.hard_match(receipt: "R-PAPER", schedule: "HARD", shift_index: 1)

        run_start = Time.utc(2026, 11, 22, 0, 0, 0)
        run = IceCube::Schedule.new(run_start)
        run.add_recurrence_rule(IceCube::Rule.minutely(10).count(20))
        archive.schedule("RUN", run)
        archive.segment(
          key: "RUN-SEGMENT",
          id_prefix: "R-RUN-",
          digits: 3,
          start: run_start,
          step: 10 * 60,
          count: 20,
          gaps: [7],
          schedule: "RUN",
          tolerance: 30
        )
      end
    end
  end
end
