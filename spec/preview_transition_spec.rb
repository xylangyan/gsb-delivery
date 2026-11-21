
require 'spec_helper'

#
# Regression coverage for the read-only "preview" queries
# (Rufus::Scheduler#occurrences / #timeline, backed by RepeatJob#occurrences)
# when a due job has started walking its trigger path but has not reached its
# final state yet.
#
# A controllable clock (all of the library's EoTime.now calls funnel through
# EtOrbi.now) drives two identical schedulers. One scheduler runs preview
# queries while its due every-job is frozen mid-trigger, the other control
# scheduler never calls any preview method. Comparing their independent
# schedule keys and actual start records distinguishes a preview that
# rewrites the plan from a preview that merely observes a transitional state.
#
describe 'preview queries across a trigger transition' do

  ANCHOR = Time.utc(2026, 11, 22, 12, 0, 0)

  T1 = ANCHOR + 600            # 12:10 UTC, the single due time of the every job
  T2 = ANCHOR + 1200           # 12:20 UTC, the cursor value seen mid-trigger
  T3 = ANCHOR + 1800           # 12:30 UTC, first half-hour-cron fire
  WINDOW_END = ANCHOR + 4 * 3600

  # After both jobs ran past the every job's last_at the clock sits at
  # 12:50 UTC; previews then have to look forward from that current instant.
  AFTER_T0 = ANCHOR + 3000     # 12:50 UTC
  AFTER_T1 = ANCHOR + 5400     # 13:30 UTC
  CRON_AT_1300 = ANCHOR + 3600
  CRON_AT_1330 = ANCHOR + 5400

  HALF_HOUR_TZ = 'Asia/Kolkata' # +05:30, half-hour offset vs the machine zone
  EVERY_FREQUENCY = 600

  # A clock whose "now" only moves when a test advances it. Real threads and
  # monotonic time keep running, so barriers and waits stay reproducible while
  # every scheduling decision reads this single frozen instant.
  class ControlledClock
    attr_accessor :now
    def initialize(start); @now = start; end
    def advance(seconds); @now += seconds; end
  end

  def install_clock

    @clock = ControlledClock.new(ANCHOR)
    @schedulers = []
    @handles = []

    EtOrbi.singleton_class.send(
      :alias_method, :__rufus_preview_real_now__, :now
    ) unless EtOrbi.respond_to?(:__rufus_preview_real_now__)

    clock = @clock
    EtOrbi.define_singleton_method(:now) { |zone=nil|
      EtOrbi::EoTime.new(clock.now.to_f, zone) }
  end

  def remove_clock

    EtOrbi.singleton_class.send(:alias_method, :now, :__rufus_preview_real_now__)
    EtOrbi.singleton_class.send(:remove_method, :__rufus_preview_real_now__)
  end

  def eot(time, zone=nil)

    EtOrbi::EoTime.new(time.to_f, zone)
  end

  # Builds one scheduler. Two calls yield two independent, identically
  # configured schedulers sharing the controllable clock.
  #
  # With freeze: true the scheduler thread parks inside on_pre_trigger at the
  # every job's due time, giving a reproducible mid-trigger synchronization
  # point. With freeze: false triggering runs straight through.
  def build_scheduler(freeze: true)

    scheduler = Rufus::Scheduler.new(frequency: 0.05)

    entered = Queue.new
    release = Queue.new
    started = []

    if freeze

      # The parked point sits after RepeatJob#trigger assigned @next_time = T2
      # but before the block runs and @times is decremented.
      def scheduler.on_pre_trigger(job, time)

        if job.name == 'periodic'
          @transition_entered << true
          @transition_release.pop
        end

        true
      end
      scheduler.instance_variable_set(:@transition_entered, entered)
      scheduler.instance_variable_set(:@transition_release, release)
    end

    every_job =
      scheduler.schedule_every(
        '10m',
        times: 1,
        name: 'periodic',
        last_at: T1 + 60
      ) { |_job, time| started << time.to_f }

    cron_job =
      scheduler.schedule_cron(
        "*/30 * * * * #{HALF_HOUR_TZ}", name: 'half_hour_cron'
      ) {}

    handle = {
      scheduler: scheduler,
      entered: entered,
      release: release,
      released: false,
      started: started,
      every: every_job,
      cron: cron_job
    }

    @schedulers << scheduler
    @handles << handle

    handle
  end

  def release_barrier(handle)

    unless handle[:released]
      handle[:release] << true
      handle[:released] = true
    end
  end

  def wait_notice(queue, timeout=5)

    deadline = Time.now + timeout
    loop do
      begin
        return queue.pop(true)
      rescue ThreadError
        raise 'timed out waiting for the trigger transition' if Time.now > deadline
        sleep 0.02
      end
    end
  end

  def wait_until_true(timeout=5)

    deadline = Time.now + timeout
    until yield
      raise 'wait condition was not met in time' if Time.now > deadline
      sleep 0.02
    end
  end

  # Independent schedule key, read off a single job instance.
  def plan_key(job)

    [ job.next_time && job.next_time.to_f, job.times, job.count ]
  end

  # Single clock advance for all schedulers: the every job becomes due at T1.
  # Each frozen scheduler signals its own barrier independently.
  def drive_to_in_flight(*handles)

    @clock.advance(EVERY_FREQUENCY)

    handles.each { |handle| wait_notice(handle[:entered]) }
  end

  # Lets the parked trigger finish and waits for the final single-run state.
  # @next_time is still T2 at this point; it is nulled on the next tick once
  # @times reached zero.
  def settle(handle)

    release_barrier(handle)

    wait_until_true {
      handle[:every].times == 0 && handle[:started] == [ T1.to_f ]
    }
  end

  # Moves the shared clock past the every job's last_at and past the first
  # cron fire, then lets both schedulers reach that state for real.
  def run_past_last_at(*handles)

    @clock.advance(2400) # 12:10 -> 12:50 UTC

    handles.each do |handle|

      wait_until_true {
        ! handle[:scheduler].jobs(:all).include?(handle[:every])
      }
      wait_until_true { handle[:cron].count >= 1 }
    end
  end

  before(:each) { install_clock }

  after(:each) do

    # Unblock any scheduler thread still parked on the transition barrier,
    # otherwise its queue pop would never return and shutdown would hang.
    @handles.each { |handle| release_barrier(handle) }

    @schedulers.each { |scheduler| scheduler.shutdown if scheduler.up? }

    remove_clock
  end

  describe 'the previewing scheduler, observed before, during and after' do

    before(:each) { @handle = build_scheduler }

    it 'previews the single due window before the job is due, without ' <<
       'touching the plan or starting a run' do

      key_before = plan_key(@handle[:every])

      per_job = @handle[:scheduler].occurrences(eot(T1), eot(T3))

      expect(per_job.keys).to contain_exactly(
        @handle[:every], @handle[:cron])
      expect(per_job[@handle[:every]].map(&:to_f)).to eq([ T1.to_f ])
      expect(per_job[@handle[:cron]].map(&:to_f)).to eq([ T3.to_f ])

      timeline = @handle[:scheduler].timeline(eot(T1), eot(T3))

      expect(timeline.map { |time, job| [ time.to_f, job.name ] }).to eq(
        [ [ T1.to_f, 'periodic' ], [ T3.to_f, 'half_hour_cron' ] ])

      expect(plan_key(@handle[:every])).to eq(key_before)
      expect(@handle[:started]).to eq([])
    end

    # Failure evidence, re-trigger point: freeze the clock exactly at T1 and
    # park the scheduler thread in on_pre_trigger. At that instant
    # RepeatJob#trigger has already assigned @next_time = T2 while @times is
    # still 1 and no run has started. A consistent preview must not advertise
    # T2, because no occurrence budget remains for it.
    #
    # This currently fails on the fixed library: RepeatJob#occurrences reads
    # @next_time and @times from that transitional state and returns T2.
    it 'does not advertise a next window mid-trigger once no run remains ' <<
       '(per-job form)' do

      drive_to_in_flight(@handle)

      # The transitional snapshot itself, pinned for reproducibility.
      expect(@handle[:every].next_time.to_f).to eq(T2.to_f)
      expect(@handle[:every].times).to eq(1)
      expect(@handle[:every].count).to eq(0)
      expect(@handle[:started]).to eq([])

      per_job = @handle[:scheduler].occurrences(eot(T1), eot(T3))

      # The cron view is unaffected; only the every-job view is torn.
      expect(per_job[@handle[:cron]].map(&:to_f)).to eq([ T3.to_f ])
      expect(per_job.key?(@handle[:every])).to eq(false)
    end

    # Same evidence in the timeline form: the phantom T2 entry currently
    # sorts ahead of the legitimate 12:30 cron entry.
    it 'does not advertise a next window mid-trigger once no run remains ' <<
       '(timeline form)' do

      drive_to_in_flight(@handle)

      timeline = @handle[:scheduler].timeline(eot(T1), eot(T3))

      expect(timeline.map { |time, job| [ time.to_f, job.name ] }).to eq(
        [ [ T3.to_f, 'half_hour_cron' ] ])
    end

    it 'previews no further window after the trigger completed and the job ' <<
       'was collected, having recorded exactly one real start' do

      drive_to_in_flight(@handle)
      settle(@handle)
      run_past_last_at(@handle)

      expect(plan_key(@handle[:every])).to eq([ nil, 0, 1 ])
      expect(@handle[:started]).to eq([ T1.to_f ])

      # Forward-looking preview from the current (12:50 UTC) instant: the
      # collected every job is gone, the cron keeps producing windows.
      per_job = @handle[:scheduler].occurrences(eot(AFTER_T0), eot(AFTER_T1))

      expect(per_job.keys).to eq([ @handle[:cron] ])
      expect(per_job.key?(@handle[:every])).to eq(false)
      expect(per_job[@handle[:cron]].map(&:to_f)).to eq(
        [ CRON_AT_1300.to_f, CRON_AT_1330.to_f ])

      timeline = @handle[:scheduler].timeline(eot(AFTER_T0), eot(AFTER_T1))

      expect(timeline.map { |time, job| [ time.to_f, job.name ] }).to eq(
        [
          [ CRON_AT_1300.to_f, 'half_hour_cron' ],
          [ CRON_AT_1330.to_f, 'half_hour_cron' ]
        ])
    end
  end

  describe 'the non-previewing control scheduler' do

    it 'ends with the same plan key and the same real start record as the ' <<
       'previewing scheduler, proving previews never rewrote execution' do

      preview = build_scheduler
      control = build_scheduler

      # Preview side: query before, then during the transition, then settle.
      preview[:scheduler].occurrences(eot(T1), eot(T3))
      preview[:scheduler].timeline(eot(T1), eot(T3))

      drive_to_in_flight(preview, control)

      preview[:scheduler].occurrences(eot(T1), eot(T3))
      preview[:scheduler].timeline(eot(T1), eot(T3))

      # The control side makes no preview call at any point.
      settle(preview)
      settle(control)

      run_past_last_at(preview, control)

      expect(plan_key(preview[:every])).to eq([ nil, 0, 1 ])
      expect(plan_key(control[:every])).to eq(plan_key(preview[:every]))

      # Independent real start records: identical, each exactly one run at T1.
      expect(control[:started]).to eq(preview[:started])
      expect(control[:started]).to eq([ T1.to_f ])

      expect(control[:cron].count).to eq(preview[:cron].count)
      expect(control[:cron].next_time.to_f).to eq(
        preview[:cron].next_time.to_f)
    end
  end

  describe 'fault contrast: a preview that pushes the real cursor' do

    # A broken preview double: instead of projecting state, it walks the real
    # trigger path (what a mutating implementation would do). The preview
    # output channel and the subsequent execution channel are captured
    # separately.
    def mutating_preview(handle, t0, t1)

      handle[:scheduler].every_jobs.each { |job|
        job.trigger(eot(@clock.now)) }

      handle[:scheduler].occurrences(eot(t0), eot(t1))
    end

    it 'is detected through both the changed plan key and a real start ' <<
       'record, neither of which the read-only preview produces' do

      good = build_scheduler(freeze: false)
      bad = build_scheduler(freeze: false)

      good_output = good[:scheduler].occurrences(eot(T1), eot(T3))
      bad_output = mutating_preview(bad, T1, T3)

      wait_until_true { bad[:started].size == 1 }

      # Execution channel: only the mutating double started a run, at the
      # frozen current instant rather than at the scheduled T1.
      expect(good[:started]).to eq([])
      expect(bad[:started]).to eq([ ANCHOR.to_f ])

      # Plan channel: the double consumed the budget and advanced the cursor.
      expect(plan_key(good[:every])).to eq([ T1.to_f, 1, 0 ])
      expect(plan_key(bad[:every])).to eq([ T2.to_f, 0, 1 ])

      # Preview output channel: the good preview still shows T1, the broken
      # one can no longer show the every job at all.
      expect(good_output[good[:every]].map(&:to_f)).to eq([ T1.to_f ])
      expect(bad_output.key?(bad[:every])).to eq(false)
    end
  end

  describe 'fault contrast: next_time and times read at different instants' do

    it 'shows one cursor producing different windows depending on which ' <<
       'instant supplied the remaining count' do

      handle = build_scheduler
      drive_to_in_flight(handle)
      job = handle[:every]

      # Real single-instant, in-flight snapshot through the library method.
      expect(job.next_time.to_f).to eq(T2.to_f)
      expect(job.times).to eq(1)

      in_flight_windows =
        job.occurrences(eot(T1), eot(T3)).map(&:to_f)

      expect(in_flight_windows).to eq([ T2.to_f ])

      # Torn pairing: keep the in-flight cursor but take the remaining count
      # from the later, settled instant. Same library method, same cursor,
      # values captured at two different instants.
      job.times = 0
      torn_windows = job.occurrences(eot(T1), eot(T3)).map(&:to_f)
      job.times = 1

      expect(torn_windows).to eq([])
      expect(torn_windows).not_to eq(in_flight_windows)

      # Letting the real trigger finish reproduces that settled budget while
      # the cursor is still T2 (it is only nulled on the next tick). The real
      # method then answers exactly what the torn pairing answered, which is
      # why mixing instants can mask a transition instead of exposing it.
      settle(handle)

      expect(job.times).to eq(0)
      expect(job.next_time.to_f).to eq(T2.to_f)
      expect(job.occurrences(eot(T1), eot(T3)).map(&:to_f)).to eq(torn_windows)
    end
  end

  describe 'the half-hour timezone cron and range boundaries' do

    before(:each) { @handle = build_scheduler }

    it 'computes windows in the cron zone, independently of the machine zone' do

      expect(@handle[:cron].cron_line.zone).to eq(HALF_HOUR_TZ)
      expect(EtOrbi::EoTime.local_tzone.name).not_to eq(HALF_HOUR_TZ)

      per_job =
        @handle[:scheduler].occurrences(eot(ANCHOR), eot(WINDOW_END))
      windows = per_job[@handle[:cron]]

      expect(windows.size).to eq(8)
      expect(windows.map(&:to_f)).to eq(
        (0..7).map { |k| (T3 + k * 1800).to_f })

      kolkata = windows.map { |t| t.translate(HALF_HOUR_TZ) }

      expect(kolkata.map { |t| t.strftime('%H:%M') }).to eq(
        %w[ 18:00 18:30 19:00 19:30 20:00 20:30 21:00 21:30 ])
      expect(kolkata.map { |t| t.strftime('%z') }.uniq).to eq([ '+0530' ])

      # The every-job phantom slot (12:20 UTC) is never a cron window.
      expect(windows.map(&:to_f)).not_to include(T2.to_f)

      # A preview does not move the cron cursor or start a cron run.
      expect(@handle[:cron].next_time.to_f).to eq(T3.to_f)
      expect(@handle[:cron].count).to eq(0)
    end

    it 'treats both range endpoints as inclusive planned instants' do

      at_boundary = @handle[:scheduler].occurrences(eot(T3), eot(T3))

      expect(at_boundary[@handle[:cron]].map(&:to_f)).to eq([ T3.to_f ])

      before_boundary =
        @handle[:scheduler].occurrences(eot(T3 - 1), eot(T3 - 1))

      expect(before_boundary.key?(@handle[:cron])).to eq(false)
    end
  end
end
