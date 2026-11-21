
#
# Specifying rufus-scheduler
#
# DST overlap policy (:dst_overlap) for cron jobs
#

require 'spec_helper'


describe Rufus::Scheduler::CronJob do

  before :each do
    @scheduler = Rufus::Scheduler.new
  end
  after :each do
    @scheduler.shutdown
  end

  def new_cron(line, opts={}, &block)
    Rufus::Scheduler::CronJob.new(
      @scheduler, line, { blocking: true }.merge(opts), block || lambda { |*| })
  end

  def nyu(*args) # EoTime in America/New_York, from UTC components
    ztu('America/New_York', *args)
  end

  def chain(job, from, count)
    t = from
    count.times.collect { t = job.next_time_from(t) }
  end

  context ':dst_overlap option' do

    it 'accepts :first, :all and :both' do
      expect(new_cron('* * * * *', dst_overlap: :first).dst_overlap).to eq(:first)
      expect(new_cron('* * * * *', dst_overlap: :all).dst_overlap).to eq(:all)
      expect(new_cron('* * * * *', dst_overlap: :both).dst_overlap).to eq(:all)
    end

    it 'accepts strings' do
      expect(new_cron('* * * * *', dst_overlap: 'first').dst_overlap).to eq(:first)
      expect(new_cron('* * * * *', dst_overlap: 'all').dst_overlap).to eq(:all)
    end

    it 'rejects other values' do
      expect {
        new_cron('* * * * *', dst_overlap: :whatever)
      }.to raise_error(ArgumentError, /invalid :dst_overlap option/)
    end

    it 'is nil when not set' do
      expect(new_cron('* * * * *').dst_overlap).to eq(nil)
    end
  end

  context 'fall DST transition (ambiguous local time)' do

    line = '30 1 * * * America/New_York'
      # 2025-11-02 01:30 occurs twice in New York:
      #   2025-11-02 01:30:00 -0400 (EDT) = 2025-11-02 05:30:00 UTC
      #   2025-11-02 01:30:00 -0500 (EST) = 2025-11-02 06:30:00 UTC

    around :each do |example|
      in_zone('Pacific/Auckland') { example.run }
        # the process timezone differs from the job timezone
    end

    context 'dst_overlap: :all' do

      it 'previews both occurrences of the ambiguous wall time' do

        job = new_cron(line, dst_overlap: :all)

        nts = chain(job, nyu(2025, 11, 1, 12, 0, 0), 4)

        expect(nts.collect { |t| t.getutc }).to eq([
          Time.utc(2025, 11, 2, 5, 30),
          Time.utc(2025, 11, 2, 6, 30),
          Time.utc(2025, 11, 3, 6, 30),
          Time.utc(2025, 11, 4, 6, 30) ])

        expect(nts[0].zone.name).to eq('America/New_York')
        expect(nts[0].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-11-02 01:30:00 -0400')
        expect(nts[1].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-11-02 01:30:00 -0500')
      end

      it 'triggers both occurrences, each with its own offset,' \
         ' even when both start late on the same wall clock' do

        triggers = []

        job = new_cron(line, dst_overlap: :all) do |j, t|
          triggers << [ j.previous_time, t ]
        end

        job.next_time = job.next_time_from(nyu(2025, 11, 1, 12, 0, 0))
        expect(job.next_time.getutc).to eq(Time.utc(2025, 11, 2, 5, 30))

        # both rounds open late (workers busy), the actual start
        # wall clock is 01:45 EST for both of them
        job.trigger(nyu(2025, 11, 2, 5, 45, 0))
        job.trigger(nyu(2025, 11, 2, 6, 45, 0))

        expect(triggers.size).to eq(2)

        first, second = triggers

        expect(first[0].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-11-02 01:30:00 -0400')
        expect(first[0].getutc).to eq(Time.utc(2025, 11, 2, 5, 30))

        expect(second[0].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-11-02 01:30:00 -0500')
        expect(second[0].getutc).to eq(Time.utc(2025, 11, 2, 6, 30))

        # the times handed to the worker fall into two adjacent,
        # non-overlapping UTC windows
        windows = [
          Time.utc(2025, 11, 2, 5, 30)...Time.utc(2025, 11, 2, 6, 30),
          Time.utc(2025, 11, 2, 6, 30)...Time.utc(2025, 11, 2, 7, 30) ]
        expect(windows[0]).to cover(first[1].getutc)
        expect(windows[1]).to cover(second[1].getutc)

        # next occurrence is on the next day, unaffected
        expect(job.next_time.getutc).to eq(Time.utc(2025, 11, 3, 6, 30))
      end

      it 'previews and triggers the same windows' do

        triggers = []

        job = new_cron(line, dst_overlap: :all) do |j, t|
          triggers << j.previous_time
        end

        from = nyu(2025, 11, 1, 12, 0, 0)
        preview = chain(job, from, 4)

        job.next_time = preview.first
        t = from
        4.times do
          t = job.next_time.getutc + 15 * 60
          job.trigger(EtOrbi::EoTime.new(t, 'America/New_York'))
        end

        expect(triggers.collect { |t| t.getutc }).to eq(
          preview.collect { |t| t.getutc })
      end
    end

    context 'dst_overlap: :first' do

      it 'keeps only the first occurrence and does not affect the next day' do

        job = new_cron(line, dst_overlap: :first)

        nts = chain(job, nyu(2025, 11, 1, 12, 0, 0), 3)

        expect(nts.collect { |t| t.getutc }).to eq([
          Time.utc(2025, 11, 2, 5, 30),
          Time.utc(2025, 11, 3, 6, 30),
          Time.utc(2025, 11, 4, 6, 30) ])

        expect(nts[0].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-11-02 01:30:00 -0400')
      end

      it 'triggers only the first occurrence in real time order' do

        triggers = []

        job = new_cron(line, dst_overlap: :first) do |j, t|
          triggers << j.previous_time
        end

        job.next_time = job.next_time_from(nyu(2025, 11, 1, 12, 0, 0))
        job.trigger(nyu(2025, 11, 2, 5, 45, 0))

        expect(triggers.size).to eq(1)
        expect(triggers[0].getutc).to eq(Time.utc(2025, 11, 2, 5, 30))
        expect(job.next_time.getutc).to eq(Time.utc(2025, 11, 3, 6, 30))
      end
    end

    context 'dense cron line' do

      dense_line = '58,59,0,1,2 1 * * * America/New_York'

      it 'dst_overlap: :all fires each ambiguous wall time twice' do

        job = new_cron(dense_line, dst_overlap: :all)

        nts = chain(job, nyu(2025, 11, 2, 4, 55, 0), 10)

        expect(nts.collect { |t| t.getutc }).to eq([
          Time.utc(2025, 11, 2, 5, 0),
          Time.utc(2025, 11, 2, 5, 1),
          Time.utc(2025, 11, 2, 5, 2),
          Time.utc(2025, 11, 2, 5, 58),
          Time.utc(2025, 11, 2, 5, 59),
          Time.utc(2025, 11, 2, 6, 0),
          Time.utc(2025, 11, 2, 6, 1),
          Time.utc(2025, 11, 2, 6, 2),
          Time.utc(2025, 11, 2, 6, 58),
          Time.utc(2025, 11, 2, 6, 59) ])
      end

      it 'dst_overlap: :first skips the whole repeated hour' do

        job = new_cron(dense_line, dst_overlap: :first)

        nts = chain(job, nyu(2025, 11, 2, 4, 55, 0), 7)

        expect(nts.collect { |t| t.getutc }).to eq([
          Time.utc(2025, 11, 2, 5, 0),
          Time.utc(2025, 11, 2, 5, 1),
          Time.utc(2025, 11, 2, 5, 2),
          Time.utc(2025, 11, 2, 5, 58),
          Time.utc(2025, 11, 2, 5, 59),
          Time.utc(2025, 11, 3, 6, 0),
          Time.utc(2025, 11, 3, 6, 1) ])
      end
    end
  end

  context 'spring DST transition (nonexistent local time)' do

    line = '30 2 * * * America/New_York'
      # 2025-03-09 02:30 does not exist in New York

    around :each do |example|
      in_zone('Pacific/Auckland') { example.run }
    end

    [ :all, :first ].each do |policy|

      it "skips the nonexistent 02:30 (dst_overlap: #{policy})" do

        job = new_cron(line, dst_overlap: policy)

        nts = chain(job, nyu(2025, 3, 8, 12, 0, 0), 3)

        # from the day before to the day after:
        # no fictional 2025-03-09 02:30, no shift to 03:30
        expect(nts.collect { |t| t.getutc }).to eq([
          Time.utc(2025, 3, 10, 6, 30),
          Time.utc(2025, 3, 11, 6, 30),
          Time.utc(2025, 3, 12, 6, 30) ])

        expect(nts[0].strftime('%Y-%m-%d %H:%M:%S %z')).to eq(
          '2025-03-10 02:30:00 -0400')
      end
    end
  end

  context 'no :dst_overlap option' do

    it 'follows the plain fugit time rules' do

      job = new_cron('30 1 * * * America/New_York')
      cron = Fugit::Cron.do_parse('30 1 * * * America/New_York')

      t = nyu(2025, 11, 1, 12, 0, 0)

      4.times do
        expect(job.next_time_from(t)).to eq(cron.next_time(t))
        t = job.next_time_from(t)
      end
    end
  end
end
