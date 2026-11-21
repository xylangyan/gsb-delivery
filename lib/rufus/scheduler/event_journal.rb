
#
# A bounded, replayable record of the events a subscribed observer may
# have missed while it was away.
#
# Subscriptions are passive. The scheduler appends to their in-memory ring
# buffer and immediately goes on with its work; it never calls back into the
# subscriber, so a slow (or vanished) observer cannot delay a job.
#
class Rufus::Scheduler::EventJournal

  EoTime = ::EtOrbi::EoTime

  DEFAULT_CAPACITY = 64

  #
  # One recorded run event. Sequence numbers are per subscription and
  # strictly contiguous (1, 2, 3, ...) for as long as the event is retained.
  #
  class Event

    attr_reader :seq
    attr_reader :kind
    attr_reader :job_id
    attr_reader :tags
    attr_reader :time
    attr_reader :at
    attr_reader :detail

    def initialize(seq, kind, job_id, tags, time, detail)

      @seq = seq
      @kind = kind
      @job_id = job_id
      @tags = tags
      @time = time
      @at = EoTime.now
      @detail = detail.freeze

      freeze
    end

    alias type kind
    alias name kind
    alias phase kind
    alias sequence seq
    alias job job_id
    alias ts time

    def reason; @detail[:reason]; end
    def error; @detail[:error]; end

    def overlap_skip?
      @kind == :skipped && reason == :overlap
    end

    def enqueued?; @kind == :enqueued; end
    def started?; @kind == :started; end
    def finished?; @kind == :finished; end
    def failed?; @kind == :failed; end

    def alive?; @kind != :finished && @kind != :failed; end

    def [](key)

      case key
      when :seq then @seq
      when :kind, :type, :name then @kind
      when :job_id then @job_id
      when :tags then @tags
      when :time then @time
      when :at then @at
      else @detail[key]
      end
    end

    def to_h

      {
        seq: @seq,
        kind: @kind,
        job_id: @job_id,
        tags: @tags.dup,
        time: @time,
        at: @at,
        detail: @detail.dup
      }
    end

    def inspect

      "#<#{self.class} #{@seq} #{@kind} #{@job_id} #{@detail.inspect}>"
    end
  end
  #
  # A missing range of sequence numbers. +from+ and +to+ are inclusive.
  #
  class Gap

    attr_reader :from
    attr_reader :to
    attr_reader :count

    def initialize(from, to)

      @from = from
      @to = to
      @count = to - from + 1

      freeze
    end

    alias first from
    alias last to
    alias missing count
    alias size count

    def [](key)

      case key
      when :from, :first then @from
      when :to, :last then @to
      when :count, :missing, :size then @count
      end
    end

    def to_h; { from: @from, to: @to, count: @count }; end

    def inspect; "#<#{self.class} #{@from}..#{@to} (#{@count})>"; end
  end

  #
  # The state of one job at the instant its subscription's snapshot was
  # taken. A started or failed event that committed before the snapshot is
  # reflected here (and has a +status_seq+ no greater than +seq+); one that
  # commits afterwards is only available from Subscription#since.
  #
  class JobState

    attr_reader :job_id
    attr_reader :tags
    attr_reader :status
    attr_reader :last_kind
    attr_reader :last_event_seq
    attr_reader :status_seq
    attr_reader :due_at
    attr_reader :enqueued_at
    attr_reader :started_at
    attr_reader :finished_at
    attr_reader :failed_at
    attr_reader :error
    attr_reader :runs
    attr_reader :successes
    attr_reader :failures
    attr_reader :skips

    def initialize(h)

      @job_id = h[:job_id]
      @tags = h[:tags]
      @status = h[:status]
      @last_kind = h[:last_kind]
      @last_event_seq = h[:last_event_seq]
      @status_seq = h[:status_seq]
      @due_at = h[:due_at]
      @enqueued_at = h[:enqueued_at]
      @started_at = h[:started_at]
      @finished_at = h[:finished_at]
      @failed_at = h[:failed_at]
      @error = h[:error]
      @runs = h[:runs]
      @successes = h[:successes]
      @failures = h[:failures]
      @skips = h[:skips]

      freeze
    end

    def running?; @status == :running; end
    def queued?; @status == :queued; end
    def failed?; @status == :failed; end
    def finished?; @status == :finished; end

    def [](key); respond_to?(key) ? send(key) : nil; end

    def to_h

      {
        job_id: @job_id,
        tags: @tags.dup,
        status: @status,
        last_kind: @last_kind,
        last_event_seq: @last_event_seq,
        status_seq: @status_seq,
        due_at: @due_at,
        enqueued_at: @enqueued_at,
        started_at: @started_at,
        finished_at: @finished_at,
        failed_at: @failed_at,
        error: @error,
        runs: @runs,
        successes: @successes,
        failures: @failures,
        skips: @skips
      }
    end

    def inspect

      "#<#{self.class} #{@job_id} #{@status} seq:#{@last_event_seq}>"
    end
  end

  #
  # A snapshot of the subscription's current job states, bound to the
  # sequence number of the last event it includes.
  #
  class State

    attr_reader :seq
    attr_reader :oldest_seq
    attr_reader :latest_seq
    attr_reader :dropped
    attr_reader :jobs

    def initialize(seq, oldest_seq, latest_seq, dropped, jobs)

      @seq = seq
      @oldest_seq = oldest_seq
      @latest_seq = latest_seq
      @dropped = dropped
      @jobs = jobs.freeze

      freeze
    end

    alias cursor seq
    alias next_seq seq
    alias last_seq seq

    def empty?; @jobs.empty?; end

    def job(job_id); @jobs[job_id.to_s]; end
    alias [] job

    def job_ids; @jobs.keys; end

    def to_h

      {
        seq: @seq,
        oldest_seq: @oldest_seq,
        latest_seq: @latest_seq,
        dropped: @dropped,
        jobs: @jobs.each_with_object({}) { |(id, st), h| h[id] = st.to_h }
      }
    end

    def inspect

      "#<#{self.class} seq:#{@seq} jobs:#{@jobs.size}>"
    end
  end

  #
  # What Subscription#since returns: the retained events after a cursor, an
  # optional gap, and an atomic snapshot that lets the reader continue.
  #
  class Page

    attr_reader :events
    attr_reader :gap
    attr_reader :state
    attr_reader :cursor
    attr_reader :oldest_seq
    attr_reader :latest_seq
    attr_reader :dropped

    def initialize(events, gap, state, cursor, oldest_seq, latest_seq, dropped)

      @events = events.freeze
      @gap = gap
      @state = state
      @cursor = cursor
      @oldest_seq = oldest_seq
      @latest_seq = latest_seq
      @dropped = dropped

      freeze
    end

    def gap?; !! @gap; end
    def missed; @gap; end

    def next_seq

      [ @cursor || 0, @latest_seq || 0 ].max
    end

    def empty?; @events.empty?; end

    def kinds; @events.map(&:kind); end
    alias types kinds

    def seqs; @events.map(&:seq); end

    def each(&block); @events.each(&block); end
    include Enumerable

    def to_h

      {
        cursor: @cursor,
        next_seq: next_seq,
        oldest_seq: @oldest_seq,
        latest_seq: @latest_seq,
        dropped: @dropped,
        gap: @gap && @gap.to_h,
        events: @events.map(&:to_h),
        state: @state && @state.to_h
      }
    end

    def inspect

      "#<#{self.class} cursor:#{@cursor} events:#{@events.size} " +
      "gap:#{@gap ? "#{@gap.from}..#{@gap.to}" : 'none'}>"
    end
  end

  attr_reader :scheduler
  attr_reader :default_capacity

  def initialize(scheduler=nil, opts={})

    @scheduler = scheduler
    @default_capacity = opts[:capacity] || opts[:limit] || DEFAULT_CAPACITY
    @mutex = Mutex.new
    @subscriptions = []
  end

  def subscribe(opts={})

    opts = { tag: opts } unless opts.is_a?(Hash)

    tag = opts[:tag] || opts[:tags]
    capacity =
      opts[:capacity] || opts[:limit] || opts[:max_events] ||
      @default_capacity
    job_id = opts[:job_id] || opts[:job]

    sub =
      Subscription.new(
        self,
        :tags => tag,
        :job_id => job_id,
        :capacity => capacity)

    @mutex.synchronize { @subscriptions << sub }

    sub
  end

  def subscriptions

    @mutex.synchronize { @subscriptions.dup }
  end

  def unsubscribe(subscription)

    @mutex.synchronize { @subscriptions.delete(subscription) }
  end

  def size; subscriptions.size; end

  #
  # Called by the scheduler when a run event happens. It never blocks on a
  # subscriber: each matching subscription simply appends to its ring buffer.
  #
  def record_event(job, kind, time=nil, detail={})

    return nil unless job

    job_id = (job.respond_to?(:job_id) ? job.job_id : job.id).to_s
    tags = Array(job.respond_to?(:tags) ? job.tags : nil)
      .collect { |t| t.to_s }.freeze

    subs = @mutex.synchronize { @subscriptions.dup }
    subs.each { |s| s.send(:record, kind, job_id, tags, time, detail) }

    nil
  end

  #
  # A passive, bounded event log for one observer.
  #
  class Subscription

    attr_reader :journal
    attr_reader :tags
    attr_reader :job_id
    attr_reader :created_at

    def initialize(journal, opts)

      @journal = journal
      @tags = Array(opts[:tags]).collect { |t| t.to_s }.freeze
      @job_id = opts[:job_id] && opts[:job_id].to_s
      @capacity = normalize_capacity(opts[:capacity])
      @created_at = EoTime.now

      @mutex = Mutex.new
      @events = []
      @latest_seq = 0
      @dropped = 0
      @states = {}
      @closed = false
    end

    def capacity; @mutex.synchronize { @capacity }; end
    alias limit capacity
    alias max_events capacity

    def capacity=(c)

      c = normalize_capacity(c)
      @mutex.synchronize do
        @capacity = c
        trim
      end
    end

    def closed?; @mutex.synchronize { @closed }; end

    def unsubscribe

      @mutex.synchronize { @closed = true }
      @journal.unsubscribe(self)

      self
    end

    def matches?(job_id, tags)

      return false if @job_id && @job_id != job_id.to_s

      return true if @tags.empty?

      @tags.all? { |t| tags.include?(t) }
    end

    def oldest_seq

      @mutex.synchronize { @events.empty? ? nil : @events.first.seq }
    end

    def latest_seq

      @mutex.synchronize { @latest_seq }
    end

    def dropped; @mutex.synchronize { @dropped }; end

    # All currently retained events.
    #
    def events

      @mutex.synchronize { @events.dup }
    end

    # Returns the events whose sequence number is strictly greater than
    # +cursor+, plus an atomic snapshot of the current state.
    #
    def since(cursor, opts={})

      cursor = normalize_cursor(cursor)

      events, latest, dropped, states_copy = snapshot_for_read

      first = events.first

      gap = nil

      if first.nil?
        selected = []
      elsif cursor + 1 < first.seq
        gap = Gap.new(cursor + 1, first.seq - 1)
        selected = events
      else
        selected = events.select { |ev| ev.seq > cursor }
      end

      include_state =
        opts.has_key?(:include_state) ? opts[:include_state] : true

      state = include_state ?
        build_state(states_copy, events, latest, dropped) : nil

      Page.new(
        selected,
        gap,
        state,
        cursor,
        first && first.seq,
        latest,
        dropped)
    end

    def read(opts={})

      c = opts[:after] || opts[:cursor] || opts[:since]
      c = 0 if c == nil

      since(c, :include_state => opts.fetch(:include_state, true))
    end

    # An atomic snapshot of the current state, bound to the latest sequence
    # number held by this subscription.
    #
    def state

      events, latest, dropped, states_copy = snapshot_for_read

      build_state(states_copy, events, latest, dropped)
    end
    alias snapshot state

    def to_h

      s = state

      {
        tags: @tags.dup,
        job_id: @job_id,
        capacity: capacity,
        oldest_seq: s.oldest_seq,
        latest_seq: s.latest_seq,
        dropped: s.dropped,
        jobs: s.jobs.each_with_object({}) { |(id, st), h| h[id] = st.to_h }
      }
    end

    def inspect

      "#<#{self.class} tags:#{@tags.inspect} " +
      "capacity:#{capacity} latest:#{latest_seq}>"
    end

    protected
    def record(kind, job_id, tags, time, detail)

      return unless matches?(job_id, tags)

      @mutex.synchronize do

        return if @closed

        @latest_seq += 1
        ev = Event.new(@latest_seq, kind, job_id, tags, time, detail)

        @events << ev
        update_state(ev)
        trim
      end
    end

    private

    def normalize_capacity(c)

      c = c.to_i

      fail ArgumentError.new(
        "subscription capacity must be a positive integer, got #{c.inspect}"
      ) if c < 1

      c
    end

    def normalize_cursor(c)

      c = Integer(c)

      fail ArgumentError.new(
        "subscription cursor must be >= 0, got #{c.inspect}"
      ) if c < 0

      c
    rescue TypeError

      fail ArgumentError.new(
        "subscription cursor must be an integer, got #{c.inspect}")
    end

    def trim

      while @events.size > @capacity
        @events.shift
        @dropped += 1
      end

      trim_states
    end

    #
    # Keeps the job-state map bounded too. Only terminal states whose last
    # event is already outside the retained window may be forgotten, and
    # only once the map grows past a comfortable multiple of the event
    # capacity. Running/queued jobs and anything still represented in the
    # ring buffer are never pruned.
    #
    def trim_states

      max_states = [ @capacity * 8, 32 ].max

      return if @states.size <= max_states

      oldest = @events.empty? ? @latest_seq + 1 : @events.first.seq

      candidates =
        @states.select { |_id, st|
          st[:last_event_seq] &&
          st[:last_event_seq] < oldest &&
          st[:status] != :running && st[:status] != :queued }

      candidates
        .sort_by { |_id, st| st[:last_event_seq] }
        .first(@states.size - max_states)
        .each { |id, _st| @states.delete(id) }
    end

    def update_state(ev)

      st = (@states[ev.job_id] ||= {
        job_id: ev.job_id,
        tags: ev.tags,
        status: nil,
        last_kind: nil,
        last_event_seq: nil,
        status_seq: nil,
        due_at: nil,
        enqueued_at: nil,
        started_at: nil,
        finished_at: nil,
        failed_at: nil,
        error: nil,
        runs: 0,
        successes: 0,
        failures: 0,
        skips: 0
      })

      st[:tags] = ev.tags
      st[:last_kind] = ev.kind
      st[:last_event_seq] = ev.seq

      case ev.kind
      when :due
        st[:status] = :due
        st[:due_at] = ev.time
        st[:status_seq] = ev.seq
      when :enqueued
        st[:status] = :queued
        st[:enqueued_at] = ev.time
        st[:status_seq] = ev.seq
      when :started
        st[:status] = :running
        st[:started_at] = ev.time
        st[:status_seq] = ev.seq
        st[:runs] += 1
      when :finished
        st[:status] = :finished
        st[:finished_at] = ev.time
        st[:status_seq] = ev.seq
        st[:successes] += 1
      when :failed
        st[:status] = :failed
        st[:failed_at] = ev.time
        st[:status_seq] = ev.seq
        st[:error] = ev.error
        st[:failures] += 1
      when :skipped
        st[:skips] += 1
        # a skip does not replace the job's actual status (an overlap skip
        # usually leaves the job running)
      end
    end

    def build_state(states, events, latest, dropped)

      jobs = {}

      states.each do |job_id, st|
        jobs[job_id] = JobState.new(st)
      end

      State.new(
        latest,
        events.empty? ? nil : events.first.seq,
        latest,
        dropped,
        jobs)
    end

    #
    # Copies the reader-visible data while holding the mutex, so that the
    # (potentially slower) assembly of a Page or State happens without
    # blocking the scheduler appending new events.
    #
    def snapshot_for_read

      @mutex.synchronize do

        events = @events.dup

        states = @states.each_with_object({}) do |(job_id, st), h|
          h[job_id] = st.merge(tags: st[:tags].dup)
        end

        [ events, @latest_seq, @dropped, states ]
      end
    end
  end
end
