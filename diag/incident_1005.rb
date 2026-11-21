#!/usr/bin/env ruby
# diag/incident_1005.rb
#
# Post-mortem replay for the "10:05 run never logged a START" incident.
# Fixed version: rufus-scheduler 3.9.2 (the workspace copy, unmodified).
#
# Three records are produced, all of them missing the 10:05 START receipt
# for the incident job (an `every '5s', overlap: false` job):
#
#   record A: first run blocked INSIDE the handler
#             -> 10:05 window skipped by `overlap: false`, never enqueued
#             (contrasted with `interval` and plain `every` references)
#   record B: first run stuck BEFORE the handler, still inside work_queue
#             (single worker occupied by another same-tag job)
#   record C: first run dequeued by a worker but parked on the job mutex
#             (mutex held by another same-tag job)
#
# Observation only: every hook below is a logging `prepend` applied inside
# this process. No scheduler file is modified and no scheduling decision
# is altered.
#
# Run with:
#   BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby diag/incident_1005.rb

require 'fileutils'
$LOAD_PATH.unshift File.expand_path('../lib', __dir__)
require 'rufus/scheduler'

abort "this replay is pinned to rufus-scheduler 3.9.2, got " +
      Rufus::Scheduler::VERSION unless Rufus::Scheduler::VERSION == '3.9.2'

# ---------------------------------------------------------------------------
# diag event log: logical clock (10:00 == first window of the incident job)
# ---------------------------------------------------------------------------

module Diag

  BASE = Time.local(2026, 11, 22, 10, 0, 0) # logical 10:00:00.000

  @mu = Mutex.new
  @out = nil

  class << self

    # logical 10:00 is the first window: jobs use first_in: '1s'
    def arm!(record_name)
      @wall00 = Time.now + 1.0
      @record = record_name
      FileUtils.mkdir_p(File.expand_path('out', __dir__))
      @out&.close
      @out = File.open(File.expand_path("out/#{record_name}.log", __dir__), 'w')
      log('diag', 'record', "=== #{record_name} (logical 10:00 == first window) ===")
    end

    def close
      @out&.close
    end

    def logical(t = Time.now)
      BASE + (t - @wall00)
    end

    def fmt(t)
      t.strftime('%H:%M:%S') + format('.%03d', t.usec / 1000)
    end

    # renders next_time / window values (EoTime, Time, false, nil)
    def fmtv(v)
      case v
      when nil   then 'nil'
      when false then 'false'
      else
        w = v.is_a?(EtOrbi::EoTime) ? Time.at(v.to_f) : v
        fmt(logical(w))
      end
    end

    def item(x)
      case x
      when Array then "#{x[0].name} window=#{fmtv(x[1])}"
      else x.inspect
      end
    end

    def tid
      @tids ||= {}
      @tids[Thread.current] ||= "w#{@tids.size}"
    end

    def log(source, event, details)
      line = "[#{fmt(logical)}] %-12.12s %-16.16s %s (%s)" %
        [ source, event, details, tid ]
      @mu.synchronize do
        puts(line)
        @out&.puts(line)
        @out&.flush
      end
    end
  end
end

# ---------------------------------------------------------------------------
# observation hooks (prepend = logging only, scheduling behaviour untouched)
# ---------------------------------------------------------------------------

# 1) every trigger decision: running? as seen by do_trigger, skip or accept
module TriggerDecisionTrace
  def do_trigger(time)
    skip = (opts[:overlap] == false && running?)
    Diag.log(name || id, 'trigger-decision',
      "window=#{Diag.fmtv(time)} running?=#{running?} " \
      "overlap=#{opts[:overlap].inspect} " \
      "active_workers=#{@scheduler.work_threads(:active).size} " \
      "=> #{skip ? 'SKIP (never enqueued)' : 'ACCEPT'}")
    super
  end
end
Rufus::Scheduler::Job.prepend(TriggerDecisionTrace)

# 2) next_time changes (schedule advancement / rescheduling)
module NextTimeTrace
  def set_next_time(trigger_time, is_post = false, now = nil)
    before = @next_time
    super
    if @next_time != before
      Diag.log(name || id, 'next-time',
        "#{Diag.fmtv(before)} -> #{Diag.fmtv(@next_time)}" \
        "#{' (post-trigger: rescheduled from actual end)' if is_post}")
    end
  end
end
Rufus::Scheduler::EveryJob.prepend(NextTimeTrace)
Rufus::Scheduler::IntervalJob.prepend(NextTimeTrace)

# 3) real work_queue in/out receipts (per scheduler instance)
module WorkQueueTrace
  def <<(item)
    super
    Diag.log('work-queue', 'IN ', "#{Diag.item(item)} (size=#{size})")
    self
  end

  def pop(*args)
    r = super
    Diag.log('work-queue', 'OUT', "#{Diag.item(r)} (size=#{size})")
    r
  end
end

def trace_queue(scheduler)
  scheduler.work_queue.singleton_class.prepend(WorkQueueTrace)
end

# 4) mutex wait/acquire/release receipts
module MutexTrace
  def synchronize
    Diag.log('mutex', 'WAIT', "thread #{Diag.tid} wants collect-mx")
    super do
      Diag.log('mutex', 'ACQUIRED', "thread #{Diag.tid} holds collect-mx")
      begin
        yield
      ensure
        Diag.log('mutex', 'RELEASED', "thread #{Diag.tid} left collect-mx")
      end
    end
  end
end

def traced_mutex(scheduler, name)
  m = Mutex.new
  m.singleton_class.prepend(MutexTrace)
  scheduler.mutexes[name] = m
end

# 5) handler receipts: the "START log" that was missing at 10:05
def handler(name, gate = nil)
  lambda do |job, _time|
    Diag.log(name, 'START', 'handler entered (the start receipt)')
    gate&.pop
    Diag.log(name, 'END', 'handler finished')
  end
end

# ===========================================================================
# record A: first run blocked INSIDE the handler
#   incident: every 5s, overlap: false   -> 10:05 never enqueued
#   refs:     interval 5s (reschedules from actual end),
#             every 5s with overlap allowed (10:05 DOES log a start)
# ===========================================================================

def record_a
  Diag.arm!('record_A_blocked_inside_handler')

  s = Rufus::Scheduler.new(frequency: 0.2)
  trace_queue(s)
  gate = Queue.new

  s.interval('5s', first_in: '1s', name: 'ref-interval', &handler('ref-interval', gate))
  s.every('5s', first_in: '1s', name: 'ref-every', &handler('ref-every', gate))
  s.every('5s', first_in: '1s', name: 'incident', overlap: false, tag: 'collect',
          &handler('incident', gate))

  sleep 8   # release at ~10:07
  Diag.log('diag', 'release', 'unblocking handlers')
  8.times { gate << :go }

  sleep 7   # observe: interval end+5s (~10:12), every 10:10 window
  s.shutdown
  Diag.close
end

# ===========================================================================
# record B: first run stuck BEFORE the handler, still inside work_queue
#   one worker thread only, occupied by another same-tag job
# ===========================================================================

def record_b
  Diag.arm!('record_B_stuck_in_work_queue')

  s = Rufus::Scheduler.new(frequency: 0.2, max_work_threads: 1)
  trace_queue(s)
  gate = Queue.new

  # same-tag sibling grabs the single worker at ~09:59:59 and holds it
  s.schedule_in('0.2s', name: 'blocker', tag: 'collect', &handler('blocker', gate))

  s.every('5s', first_in: '1s', name: 'incident', overlap: false, tag: 'collect',
          &handler('incident'))

  sleep 8   # release blocker at ~10:07
  Diag.log('diag', 'release', 'unblocking the same-tag blocker')
  gate << :go

  sleep 6   # observe: backlog drain (10:00 + 10:05 late), then 10:10 on time
  s.shutdown
  Diag.close
end

# ===========================================================================
# record C: first run dequeued but parked on the job mutex
#   mutex held by another same-tag job
# ===========================================================================

def record_c
  Diag.arm!('record_C_stuck_on_mutex')

  s = Rufus::Scheduler.new(frequency: 0.2)
  trace_queue(s)
  traced_mutex(s, 'collect-mx')
  gate = Queue.new

  # same-tag sibling holds collect-mx from ~09:59:59 until release
  s.schedule_in('0.2s', name: 'blocker', tag: 'collect', mutex: 'collect-mx',
                &handler('blocker', gate))

  s.every('5s', first_in: '1s', name: 'incident', overlap: false, tag: 'collect',
          mutex: 'collect-mx', &handler('incident'))

  sleep 8   # release blocker (and the mutex) at ~10:07
  Diag.log('diag', 'release', 'unblocking the mutex holder')
  gate << :go

  sleep 6   # observe: mutex-serialized backlog, then 10:10 on time
  s.shutdown
  Diag.close
end

record_a
record_b
record_c

puts
puts 'records written to diag/out/*.log'
