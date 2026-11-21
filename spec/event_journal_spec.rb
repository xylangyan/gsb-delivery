
#
# Specifying rufus-scheduler's replayable run event journal
#
require 'spec_helper'


describe 'Rufus::Scheduler event journal' do

  before :each do

    @taoe = Thread.abort_on_exception
    Thread.abort_on_exception = false

    @ose = $stderr
    $stderr = StringIO.new

    @scheduler = Rufus::Scheduler.new(frequency: 0.05)
  end

  after :each do

    @scheduler.shutdown

    Thread.abort_on_exception = @taoe
    $stderr = @ose
  end

  def kinds(sub)

    sub.events.map { |e| e.kind.to_s }
  end

  def wait_event(sub, kind, timeout=10)

    wait_until(timeout) { sub.events.any? { |e| e.kind == kind } }
    sub.events.find { |e| e.kind == kind }
  end

  context 'event classification' do

    it 'records due, enqueued, started, and finished for a threaded job' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      job =
        @scheduler.schedule_in('0s', tag: 't0') do
          # quick
        end

      wait_until { sub.events.any? { |e| e.kind == :finished } }

      expect(kinds(sub)).to eq(%w[ due enqueued started finished ])

      expect(sub.events.map(&:job_id).uniq).to eq([ job.job_id ])
    end

    it 'does not record enqueued for a blocking job' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      @scheduler.schedule_in('0s', tag: 't0', blocking: true) do
        # quick
      end

      wait_until { sub.events.any? { |e| e.kind == :finished } }

      expect(kinds(sub)).to eq(%w[ due started finished ])
    end

    it 'records failed instead of finished when the job raises' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      @scheduler.schedule_in('0s', tag: 't0') do
        fail 'kaboom'
      end

      wait_until { sub.events.any? { |e| e.kind == :failed } }

      expect(kinds(sub)).to eq(%w[ due enqueued started failed ])
      expect(kinds(sub)).not_to include('finished')

      failed = sub.events.last
      expect(failed.error).to be_a(RuntimeError)
      expect(failed.error.message).to eq('kaboom')
      expect(failed.reason).to eq(:error)
      expect(failed.alive?).to eq(false)

      state = sub.state.jobs.values.first
      expect(state.status).to eq(:failed)
      expect(state.failures).to eq(1)
      expect(state.successes).to eq(0)
    end

    it 'never records a failing run as a success' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      job =
        @scheduler.schedule_every('0.1', tag: 't0', times: 3) do
          fail 'always'
        end

      wait_until { sub.events.count { |e| e.kind == :failed } >= 3 }

      expect(sub.events.count { |e| e.kind == :finished }).to eq(0)

      state = sub.state.job(job.job_id)
      expect(state.successes).to eq(0)
      expect(state.failures).to be >= 3
    end

    it 'records a skipped event (reason overlap) when overlap: false' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      gate = Queue.new

      job =
        @scheduler.schedule_every('0.1', tag: 't0', overlap: false) do
          gate.pop
        end

      wait_until { sub.events.any? { |e| e.kind == :started } }

      wait_until(10) { sub.events.any? { |e| e.kind == :skipped } }

      skipped = sub.events.find { |e| e.kind == :skipped }

      expect(skipped.reason).to eq(:overlap)
      expect(skipped.overlap_skip?).to eq(true)
      expect(skipped.job_id).to eq(job.job_id)

      gate << :go
    end
  end

  context 'replay and retention' do

    it 'continues after the last retained sequence without duplicates or gaps' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 3)

      3.times do
        @scheduler.schedule_in('0s', tag: 't0') { }
        sleep 0.2
      end

      wait_until { sub.latest_seq >= 9 }

      first = sub.since(0)

      expect(first.gap?).to eq(true)
      expect(first.gap.from).to eq(1)

      cursor = first.events.last.seq

      second = sub.since(cursor)

      expect(second.events.map(&:seq)).to eq(
        ((cursor + 1)..sub.latest_seq).to_a)

      expect(second.gap?).to eq(false)
      expect(second.cursor).to eq(cursor)
      expect(second.next_seq).to eq(sub.latest_seq)

      seqs = first.events.map(&:seq) + second.events.map(&:seq)
      expect(seqs).to eq(seqs.uniq)
      expect(seqs).to eq(seqs.sort)
    end

    it 'reports a gap when the cursor is older than the retained window' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 2)

      @scheduler.schedule_in('0s', tag: 't0') { }
      wait_until { sub.latest_seq >= 4 }

      page = sub.since(1)

      expect(page.gap?).to eq(true)
      expect(page.gap.from).to eq(2)
      expect(page.gap.to).to eq(sub.oldest_seq - 1)
      expect(page.gap.count).to eq(page.gap.to - page.gap.from + 1)
      expect(page.events.first.seq).to eq(sub.oldest_seq)
    end

    it 'reports no gap when the cursor is exactly one before the oldest' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 2)

      @scheduler.schedule_in('0s', tag: 't0') { }
      wait_until { sub.latest_seq >= 4 }

      page = sub.since(sub.oldest_seq - 1)

      expect(page.gap?).to eq(false)
      expect(page.events.first.seq).to eq(sub.oldest_seq)
    end
  end

  context 'snapshot and subsequent events' do

    it 'shows a queued job turning into a running one while events churn' do

      @scheduler.max_work_threads = 1

      sub = @scheduler.subscribe(tag: 't0', capacity: 4)

      gate = Queue.new
      release = Queue.new

      holder =
        @scheduler.schedule_in('0s', tag: 't0') do
          gate << :holding
          release.pop
        end

      gate.pop

      queued =
        @scheduler.schedule_in('0.2s', tag: 't0') do
          # quick
        end

      wait_until { sub.state.job(queued.job_id) }

      snapshot = sub.state

      expect(snapshot.job(holder.job_id).status).to eq(:running)
      expect(snapshot.job(queued.job_id).status).to eq(:queued)

      release << :go

      wait_until { sub.state.job(queued.job_id).finished? }

      later = sub.since(snapshot.seq)

      queued_kinds = later.events.select { |e| e.job_id == queued.job_id }
      expect(queued_kinds.map(&:kind)).to include(:started, :finished)

      state = later.state.job(queued.job_id)
      expect(state.status).to eq(:finished)

      # no event for the queued job fell between the snapshot and the cursor
      expect(sub.events.select { |e| e.job_id == queued.job_id }
        .map(&:seq).min).to be <= snapshot.job(queued.job_id).status_seq
    end

    it 'shows a run that straddles the snapshot in state or later events' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      gate = Queue.new
      release = Queue.new

      job =
        @scheduler.schedule_in('0s', tag: 't0') do
          gate << :started
          release.pop
        end

      gate.pop

      snapshot = sub.state

      expect(snapshot.job(job.job_id).status).to eq(:running)
      expect(snapshot.seq).to eq(sub.latest_seq)

      started_seq = sub.events.find { |e| e.kind == :started }.seq
      expect(started_seq).to be <= snapshot.seq

      release << :go

      wait_until { sub.events.any? { |e| e.kind == :finished } }

      later = sub.since(snapshot.seq)

      expect(later.events.map(&:kind)).to eq([ :finished ])
      expect(later.state.job(job.job_id).status).to eq(:finished)
      expect(later.next_seq).to eq(sub.latest_seq)
    end

    it 'never loses a failure committed around the snapshot' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 8)

      gate = Queue.new
      release = Queue.new

      job =
        @scheduler.schedule_in('0s', tag: 't0') do
          gate << :about_to_fail
          release.pop
          fail 'late boom'
        end

      gate.pop

      snapshot = sub.state
      release << :go

      wait_until { sub.events.any? { |e| e.kind == :failed } }

      failed_in_state = snapshot.job(job.job_id).failed?
      failed_in_later = sub.since(snapshot.seq).events.any? do |e|
        e.kind == :failed && e.job_id == job.job_id
      end

      expect(failed_in_state || failed_in_later).to eq(true)
    end

    it 'binds the state sequence to the events it already contains' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      @scheduler.schedule_in('0s', tag: 't0') { }
      wait_until { sub.events.any? { |e| e.kind == :finished } }

      snapshot = sub.state

      expect(snapshot.job_ids.size).to eq(1)
      expect(snapshot.jobs.values.first.status).to eq(:finished)
      expect(snapshot.seq).to eq(sub.latest_seq)

      expect(sub.since(snapshot.seq).events).to eq([])
    end
  end

  context 'tags and jobs' do

    it 'does not mix two jobs carrying the same tag' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 64)

      gate = Queue.new

      j0 =
        @scheduler.schedule_in('0s', tag: 't0') do
          gate.pop
        end
      j1 =
        @scheduler.schedule_in('0.1s', tag: 't0') do
          # quick
        end

      wait_until do
        sub.state.job(j0.job_id) &&
        sub.state.job(j1.job_id) &&
        sub.state.job(j1.job_id).finished?
      end

      s = sub.state

      expect(s.job_ids.sort).to eq([ j0.job_id, j1.job_id ].sort)
      expect(s.job(j0.job_id).status).to eq(:running)
      expect(s.job(j1.job_id).status).to eq(:finished)

      expect(sub.events.select { |e| e.kind == :finished }.map(&:job_id))
        .to eq([ j1.job_id ])
      expect(sub.events.select { |e| e.kind == :started }.map(&:job_id).sort)
        .to eq([ j0.job_id, j1.job_id ].sort)

      gate << :go
    end

    it 'filters events by tag' do

      a = @scheduler.subscribe(tag: 'a', capacity: 64)
      b = @scheduler.subscribe(tag: 'b', capacity: 64)

      @scheduler.schedule_in('0s', tag: 'a') { }
      wait_until { a.events.any? { |e| e.kind == :finished } }

      expect(a.events).not_to be_empty
      expect(b.events).to eq([])
    end
  end

  context 'slow subscribers' do

    it 'does not delay job execution' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 2)

      started = Queue.new
      finished = Queue.new

      t0 = Time.now

      @scheduler.schedule_in('0s', tag: 't0') do
        started << :started
        sleep 0.05
        finished << :finished
      end

      started.pop
      finished.pop

      elapsed = Time.now - t0

      expect(elapsed).to be < 1.0
      expect(sub.latest_seq).to be > 0
    end
  end

  context 'capacity' do

    it 'is an explicit, per-subscription bound' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 2)

      expect(sub.capacity).to eq(2)

      3.times do
        @scheduler.schedule_in('0s', tag: 't0') { }
        sleep 0.15
      end

      wait_until { sub.dropped >= 1 }

      expect(sub.events.size).to eq(2)
      expect(sub.oldest_seq).to be > 1
      expect(sub.latest_seq).to eq(sub.oldest_seq + 1)
    end

    it 'rejects a non-positive capacity' do

      expect {
        @scheduler.subscribe(tag: 't0', capacity: 0)
      }.to raise_error(ArgumentError)
    end
  end

  context 'continuation state' do

    it 'offers a current state plus the sequence to continue from' do

      sub = @scheduler.subscribe(tag: 't0', capacity: 2)

      3.times do
        @scheduler.schedule_in('0s', tag: 't0') { }
        sleep 0.15
      end

      wait_until { sub.dropped >= 1 }

      page = sub.since(0)

      expect(page.gap?).to eq(true)
      expect(page.state).not_to be_nil
      expect(page.state.seq).to eq(page.latest_seq)
      expect(page.next_seq).to eq(page.latest_seq)

      follow = sub.since(page.next_seq)
      expect(follow.events).to eq([])
      expect(follow.gap?).to eq(false)
    end
  end

  context 'an offline operator reconnecting under churn' do

    it 'keeps the retained tail contiguous and current state complete' do

      @scheduler.max_work_threads = 2

      sub = @scheduler.subscribe(tag: 'collect', capacity: 6)

      gate = Queue.new
      release = Queue.new

      a =
        @scheduler.schedule_in('0s', tag: 'collect') do
          gate << :a_running
          release.pop
        end

      gate.pop

      b =
        @scheduler.schedule_every('0.1', tag: 'collect', overlap: false) do
          sleep 0.25
          fail 'collector down'
        end

      wait_until(10) { sub.events.any? { |e| e.kind == :skipped } }

      cursor = sub.latest_seq
      Thread.new { sleep 0.4; release << :go }

      wait_until { sub.dropped >= 2 }

      page = sub.since(cursor)
      retained = sub.events

      if page.gap?
        expect(page.gap.to).to eq(retained.first.seq - 1)
        expect(page.events.first.seq).to eq(retained.first.seq)
      else
        expect(page.events.map(&:seq)).to eq(
          ((cursor + 1)..sub.latest_seq).to_a)
      end

      seqs = page.events.map(&:seq)
      expect(seqs).to eq(seqs.sort.uniq)

      expect(page.state.seq).to eq(page.latest_seq)
      expect(page.state.job(a.job_id)).not_to be_nil

      again = sub.since(page.next_seq)
      expect(again.events).to eq([])
      expect(again.gap?).to eq(false)

      b.unschedule
      release << :go
    end
  end
end
