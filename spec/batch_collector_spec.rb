#
# Specifying rufus-scheduler
#
# Batch collection and merge (persistent, restart-safe)
#

require 'spec_helper'
require 'json'
require 'tmpdir'
require 'fileutils'


describe Rufus::Scheduler::BatchCollector do

  def sha(path)

    "sha256:#{Digest::SHA256.file(path).hexdigest}"
  end

  def artifacts(dir, name)

    path = File.join(dir, name)
    FileUtils.mkdir_p(File.dirname(path))
    File.write(path, "payload-#{name}-#{rand(1_000_000)}")
    path
  end

  def new_collector(root, opts = {})

    Rufus::Scheduler::BatchCollector.new(root: root, **opts)
  end

  def merge_into_result

    lambda do |batch, inputs, work_dir|

      names = inputs.map { |i| i['source'] }.sort
      text = inputs.map { |i| File.read(i['snapshot_path']) }.join('|')
      File.write(File.join(work_dir, 'merged.txt'), "#{batch}:#{text}")

      { 'sources' => names, 'length' => text.length }
    end
  end

  def run_ruby(script)

    lib = File.expand_path('../lib', __dir__)
    out = IO.popen(
      [ RbConfig.ruby, '-I', lib, '-e', script ], err: [ :child, :out ]
    ) { |io| io.read }
    status = $?

    fail "ruby failed (#{status.exitstatus}):\n#{out}" unless status.success?

    out
  end

  around do |example|

    Dir.mktmpdir('rufus-batch') do |dir|
      @root = File.join(dir, 'state')
      @inbox = File.join(dir, 'inbox')
      FileUtils.mkdir_p(@inbox)
      example.run
    end
  end

  describe '#report' do

    it 'accepts the two verified artifacts of a batch and queues it' do

      c = new_collector(@root)

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      ra = c.report(batch: 17, source: :a, path: a, summary: 'a rows')
      rb = c.report(batch: 17, source: :b, path: b, summary: 'b rows')

      expect(ra['accepted']).to be(true)
      expect(ra['status']).to eq('collecting')
      expect(rb['status']).to eq('queued')
      expect(rb['batch']).to eq('17')
      expect(rb['source']).to eq('b')
      expect(rb['path']).to eq(File.expand_path(b))
      expect(rb['summary']).to eq('b rows')
    end

    it 'does not queue a batch with a single source' do

      c = new_collector(@root)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))

      expect(c.state(17)['status']).to eq('collecting')
      expect(c.manifest(17)).to be_nil
    end

    it 'reports a digest mismatch and seals the batch' do

      c = new_collector(@root)
      a = artifacts(@inbox, 'a.csv')

      expect {
        c.report(batch: 20, source: :a, path: a, digest: 'sha256:deadbeef')
      }.to raise_error(Rufus::Scheduler::BatchCollector::RejectedReport, /digest/)

      expect(c.state(20)['status']).to eq('failed')
    end

    it 'distinguishes a missing artifact from a digest mismatch' do

      c = new_collector(@root)

      expect {
        c.report(batch: 20, source: :a, path: File.join(@inbox, 'nope.csv'))
      }.to raise_error(Rufus::Scheduler::BatchCollector::RejectedReport, /missing/)
    end

    it 'keeps the original report location and the snapshot bytes' do

      c = new_collector(@root)
      a = artifacts(@inbox, 'a.csv')

      c.report(batch: 17, source: :a, path: a)

      input = c.state(17)['inputs']['a']

      expect(input['path']).to eq(File.expand_path(a))
      expect(input['digest']).to eq(sha(a))
      expect(
        File.read(File.join(@root, 'batches', '17', input['snapshot']))
      ).to eq(File.read(a))
    end
  end

  describe 'interleaved batches' do

    it 'lets batches 17 and 18 become ready in any order' do

      c = new_collector(@root, merger: merge_into_result)

      # interleaved arrivals
      c.report(batch: 17, source: :a, path: artifacts(@inbox, '17a.csv'))
      c.report(batch: 18, source: :b, path: artifacts(@inbox, '18b.csv'))
      c.report(batch: 18, source: :a, path: artifacts(@inbox, '18a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, '17b.csv'))

      expect(c.state(17)['status']).to eq('queued')
      expect(c.state(18)['status']).to eq('queued')

      c.drain

      expect(c.manifest(17)['inputs'].map { |i| i['digest'] }).to eq(
        %w[ a b ].map { |s| c.state(17)['inputs'][s]['digest'] })
      expect(c.manifest(18)['batch']).to eq('18')
      expect(c.published?(17)).to be(true)
      expect(c.published?(18)).to be(true)
    end

    it 'allows the same file name in different batches' do

      c = new_collector(@root, merger: merge_into_result)

      name = 'same.csv'

      c.report(batch: 17, source: :a, path: artifacts(@inbox, name))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, '17b.csv'))
      c.report(batch: 18, source: :a, path: artifacts(@inbox, name))
      c.report(batch: 18, source: :b, path: artifacts(@inbox, '18b.csv'))

      c.drain

      expect(c.manifest(17)['inputs'][0]['path']).to end_with('/same.csv')
      expect(c.manifest(18)['inputs'][0]['path']).to end_with('/same.csv')
      expect(c.manifest(17)['inputs'][0]['digest']).not_to eq(
        c.manifest(18)['inputs'][0]['digest'])
    end
  end

  describe 'repeated delivery' do

    it 'counts a repeated latest report once' do

      c = new_collector(@root)
      merges = 0

      c.merger = lambda do |batch, inputs, work_dir|
        merges += 1
        File.write(File.join(work_dir, 'merged.txt'), 'x')
        { 'batch' => batch }
      end

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      c.report(batch: 17, source: :a, path: a)
      c.report(batch: 17, source: :b, path: b)
      c.report(batch: 17, source: :b, path: b)
      c.drain
      c.report(batch: 17, source: :b, path: b)

      expect(merges).to eq(1)
      expect(c.state(17)['merge_attempts']).to eq(1)
    end

    it 'requeues a complete batch when the latest report is delivered again' do

      c = new_collector(@root, merger: merge_into_result)

      c.on(:collected) do |batch|
        fail Rufus::Scheduler::BatchCollector::InterruptedBeforeQueue
      end

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      c.report(batch: 17, source: :a, path: a)

      expect {
        c.report(batch: 17, source: :b, path: b)
      }.to raise_error(
        Rufus::Scheduler::BatchCollector::InterruptedBeforeQueue)

      c.on(:collected) { |batch| }

      # redelivery of the latest report after the restart
      result = c.report(batch: 17, source: :b, path: b)

      expect(result['accepted']).to be(true)
      expect(c.state(17)['status']).to eq('queued')

      c.drain
      expect(c.published?(17)).to be(true)
    end

    it 'keeps one report per batch, source, location and summary' do

      c = new_collector(@root)

      a = artifacts(@inbox, 'a.csv')

      3.times do
        c.report(batch: 17, source: :a, path: a, summary: 'same report')
      end

      expect(c.notifications(batch: 17).size).to eq(1)
    end

    it 'does not replace an accepted input with a later same-named file' do

      c = new_collector(@root, merger: merge_into_result)

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      c.report(batch: 17, source: :a, path: a)
      c.report(batch: 17, source: :b, path: b)

      accepted = c.state(17)['inputs']['a']['digest']

      # same name, different content, after the batch is queued
      File.write(a, 'changed later')
      result = c.report(batch: 17, source: :a, path: a)

      expect(result['accepted']).to be(false)
      expect(c.state(17)['inputs']['a']['digest']).to eq(accepted)
      expect(c.state(17)['status']).to eq('queued')
    end
  end

  describe 'interruptions' do

    it 'resumes a batch collected but never queued' do

      c = new_collector(@root, merger: merge_into_result)

      c.on(:collected) do |batch|
        fail Rufus::Scheduler::BatchCollector::InterruptedBeforeQueue
      end

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      c.report(batch: 17, source: :a, path: a)

      expect {
        c.report(batch: 17, source: :b, path: b)
      }.to raise_error(
        Rufus::Scheduler::BatchCollector::InterruptedBeforeQueue)

      expect(c.state(17)['status']).to eq('collecting')
      expect(c.state(17)['inputs'].keys.sort).to eq(%w[ a b ])

      # a fresh process, same directory
      c2 = new_collector(@root, merger: merge_into_result)
      c2.recover

      expect(c2.state(17)['status']).to eq('published')
      expect(c2.manifest(17)['inputs'].size).to eq(2)
    end

    it 'resumes a publication whose completion was not recorded' do

      c = new_collector(@root, merger: merge_into_result)
      merges = 0

      lambda_merger = lambda do |batch, inputs, work_dir|
        merges += 1
        File.write(File.join(work_dir, 'merged.txt'), 'result')
        { 'batch' => batch }
      end
      c.merger = lambda_merger

      c.on(:published_files) do |batch|
        fail Rufus::Scheduler::BatchCollector::InterruptedAfterPublish
      end

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      expect {
        c.drain
      }.to raise_error(
        Rufus::Scheduler::BatchCollector::InterruptedAfterPublish)

      # the publication location already has the result and both identities
      expect(
        File.file?(File.join(@root, 'published', '17', 'manifest.json'))
      ).to be(true)
      expect(c.manifest(17)).not_to be_nil

      # the scheduler had not recorded completion
      state = JSON.parse(
        File.read(File.join(@root, 'batches', '17', 'state.json')))
      expect(state['status']).to eq('merging')

      c2 = new_collector(@root, merger: lambda_merger)
      c2.recover

      expect(c2.state(17)['status']).to eq('published')
      expect(merges).to eq(1)
    end

    it 'redoes a merge that failed before publishing' do

      c = new_collector(@root)
      calls = 0

      c.merger = lambda do |batch, inputs, work_dir|
        calls += 1
        fail 'transient merge failure' if calls == 1
        File.write(File.join(work_dir, 'merged.txt'), 'ok')
        { 'batch' => batch }
      end

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      expect { c.drain }.to raise_error(/transient merge failure/)

      expect(c.state(17)['status']).to eq('queued')
      expect(c.published?(17)).to be(false)

      c.drain

      expect(calls).to eq(2)
      expect(c.published?(17)).to be(true)
    end

    it 'does not let a late same-named file redo a published batch' do

      c = new_collector(@root, merger: merge_into_result)

      a = artifacts(@inbox, 'a.csv')
      c.report(batch: 17, source: :a, path: a)
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain

      published_digest = c.manifest(17)['inputs'][0]['digest']

      File.write(a, 'a different file with the same name')
      result = c.report(batch: 17, source: :a, path: a)

      expect(result['accepted']).to be(false)
      expect(result['reason']).to eq('already published')
      expect(c.manifest(17)['inputs'][0]['digest']).to eq(published_digest)
      expect(c.state(17)['merge_attempts']).to eq(1)
    end

    it 'rebuilds a missing state from the published manifest' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain

      result = c.result(17)

      FileUtils.rm_rf(File.join(@root, 'batches', '17'))

      c2 = new_collector(@root, merger: merge_into_result)
      c2.recover

      expect(c2.state(17)['status']).to eq('published')
      expect(c2.result(17)).to eq(result)
      expect(c2.state(17)['merge_attempts']).to eq(0)
    end

    it 'does not treat a publication path without a valid manifest as done' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      # simulate a write that stopped after creating the directory
      FileUtils.mkdir_p(File.join(@root, 'published', '17'))
      File.write(
        File.join(@root, 'published', '17', 'result.json'), 'half')

      expect(c.manifest(17)).to be_nil
      expect(c.published?(17)).to be(false)

      c.drain

      expect(c.published?(17)).to be(true)
      expect(c.manifest(17)['result_digest']).to match(/^sha256:/)
    end

    it 'rolls a published state back when its manifest cannot be validated' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain

      manifest_path = File.join(@root, 'published', '17', 'manifest.json')

      # a truncated or tampered manifest is not a valid publication
      File.write(manifest_path, JSON.generate(
        'batch' => '17', 'status' => 'published', 'inputs' => []))

      expect(c.published?(17)).to be(false)
      expect(c.state(17)['status']).to eq('queued')

      c2 = new_collector(@root, merger: merge_into_result)
      c2.recover

      expect(c2.published?(17)).to be(true)
    end

    it 'rejects a manifest whose result no longer matches its digest' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain

      File.write(
        File.join(@root, 'published', '17', 'result.json'), 'tampered')

      expect(c.published?(17)).to be(false)
    end
  end

  describe 'failed batch' do

    it 'seals on a bad artifact and ignores a later old notification' do

      c = new_collector(@root, merger: merge_into_result)

      good = artifacts(@inbox, 'a.csv')
      bad = File.join(@inbox, 'b.csv')
      File.write(bad, 'poison')

      c.report(batch: 21, source: :a, path: good)

      expect {
        c.report(batch: 21, source: :b, path: bad,
          digest: 'sha256:not-the-real-one')
      }.to raise_error(Rufus::Scheduler::BatchCollector::RejectedReport)

      expect(c.state(21)['status']).to eq('failed')

      # now an "older notification" with the same name and a valid digest
      File.write(bad, 'an older, valid artifact?')

      result = c.report(batch: 21, source: :b, path: bad)

      expect(result['accepted']).to be(false)
      expect(result['reason']).to eq('batch failed')
      expect(c.state(21)['status']).to eq('failed')

      c.drain
      expect(c.published?(21)).to be(false)
    end

    it 'keeps the failed state across a restart' do

      c = new_collector(@root)

      expect {
        c.report(batch: 21, source: :a, path: File.join(@inbox, 'none'))
      }.to raise_error(Rufus::Scheduler::BatchCollector::RejectedReport)

      c2 = new_collector(@root)
      c2.recover

      expect(c2.state(21)['status']).to eq('failed')
      expect(c2.published?(21)).to be(false)
    end
  end

  describe 'persistence' do

    it 'resumes from the directory, not from a work queue' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      expect(c.state(17)['status']).to eq('queued')

      # a brand new collector, no memory of the queue
      c2 = new_collector(@root, merger: merge_into_result)
      c2.recover

      expect(c2.state(17)['status']).to eq('published')
      expect(c2.manifest(17)['files'].map { |f| f['path'] }).to eq(
        [ 'merged.txt' ])
    end

    it 'reads a complete manifest back even when the state file is gone' do

      c = new_collector(@root, merger: merge_into_result)

      a = artifacts(@inbox, 'a.csv')
      c.report(batch: 17, source: :a, path: a)
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain

      digest = c.manifest(17)['inputs'][0]['digest']
      result = c.result(17)

      FileUtils.rm_f(File.join(@root, 'batches', '17', 'state.json'))

      c2 = new_collector(@root, merger: merge_into_result)

      expect(c2.published?(17)).to be(true)
      expect(c2.result(17)).to eq(result)

      # a late same-named file does not become an input of this batch
      File.write(a, 'late different bytes')
      r = c2.report(batch: 17, source: :a, path: a)

      expect(r['reason']).to eq('already published')
      expect(c2.manifest(17)['inputs'][0]['digest']).to eq(digest)
    end

    it 'keeps exactly one publication per batch' do

      c = new_collector(@root, merger: merge_into_result)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))
      c.drain
      first = c.manifest(17)

      c.recover
      c.recover(batch: 17)

      expect(c.manifest(17)).to eq(first)
      expect(c.state(17)['merge_attempts']).to eq(1)
    end

    it 'merges once when two collectors share the directory' do

      c1 = new_collector(@root, merger: merge_into_result)
      c1.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c1.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      calls = 0
      slow = lambda do |batch, inputs, work_dir|
        calls += 1
        sleep 0.2
        File.write(File.join(work_dir, 'merged.txt'), 'x')
        { 'batch' => batch }
      end

      c2 = new_collector(@root, merger: slow)
      c1.merger = slow

      threads = [ c1, c2 ].map do |c|
        Thread.new { c.recover }
      end
      threads.each(&:join)

      expect(calls).to eq(1)
      expect(c1.state(17)['merge_attempts']).to eq(1)
      expect(c2.state(17)['merge_attempts']).to eq(1)
      expect(c1.manifest(17)).to eq(c2.manifest(17))
      expect(
        Dir[File.join(@root, 'published', '17', 'manifest.json')].size
      ).to eq(1)
    end

    it 'publishes exactly one result per batch' do

      c = new_collector(@root, merger: merge_into_result)
      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))
      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      c.drain
      c.drain
      c.recover

      manifests = Dir[File.join(@root, 'published', '*', 'manifest.json')]
      expect(manifests.size).to eq(1)
      expect(c.state(17)['merge_attempts']).to eq(1)
    end

    it 'keeps failed notifications readable' do

      c = new_collector(@root)

      expect {
        c.report(batch: 21, source: :a, path: File.join(@inbox, 'none'))
      }.to raise_error(Rufus::Scheduler::BatchCollector::RejectedReport)

      expect(c.notifications(batch: 21).size).to eq(1)
    end
  end

  describe 'persistence across processes' do

    it 'recovers a queued batch in a brand new process' do

      a = artifacts(@inbox, 'a.csv')
      b = artifacts(@inbox, 'b.csv')

      writer = <<~RUBY
        require 'rufus-scheduler'

        c = Rufus::Scheduler::BatchCollector.new(root: #{@root.inspect})
        c.report(batch: 17, source: :a, path: #{a.inspect}, summary: 'a')
        c.report(batch: 17, source: :b, path: #{b.inspect}, summary: 'b')
      RUBY

      reader = <<~RUBY
        require 'rufus-scheduler'
        require 'json'

        c = Rufus::Scheduler::BatchCollector.new(
          root: #{@root.inspect},
          merger: lambda { |batch, inputs, work_dir|
            text = inputs.map { |i| File.read(i['snapshot_path']) }.join('|')
            File.write(File.join(work_dir, 'merged.txt'), text)
            { 'batch' => batch, 'length' => text.length }
          })

        c.recover

        print JSON.generate(
          'status' => c.state(17)['status'],
          'published' => c.published?(17),
          'result' => c.result(17),
          'attempts' => c.state(17)['merge_attempts'],
          'sources' => c.manifest(17)['inputs'].map { |i| i['source'] })
      RUBY

      run_ruby(writer)

      expect(
        File.read(File.join(@root, 'batches', '17', 'state.json'))
      ).to include('"queued"')

      out = run_ruby(reader)
      data = JSON.parse(out)

      expect(data['status']).to eq('published')
      expect(data['published']).to be(true)
      expect(data['attempts']).to eq(1)
      expect(data['sources'].sort).to eq(%w[ a b ])
      expect(data['result']['batch']).to eq('17')
    end
  end

  describe 'scheduler integration' do

    it 'uses an at job only once a batch is complete' do

      scheduler = Rufus::Scheduler.new
      c = new_collector(@root, merger: merge_into_result, scheduler: scheduler)

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))

      expect(scheduler.jobs.size).to eq(0)

      c.report(batch: 17, source: :b, path: artifacts(@inbox, 'b.csv'))

      expect(
        wait_until(5) { c.published?(17) }
      ).to be(true)

    ensure

      scheduler.shutdown(:kill) if scheduler
    end

    it 'leaves ordinary scheduling untouched while a batch waits' do

      scheduler = Rufus::Scheduler.new
      c = new_collector(@root, merger: merge_into_result, scheduler: scheduler)

      ticks = []

      scheduler.every('0.5s') { ticks << :tick }

      c.report(batch: 17, source: :a, path: artifacts(@inbox, 'a.csv'))

      expect(wait_until(5) { ticks.size >= 2 }).to be(true)
      expect(c.state(17)['status']).to eq('collecting')

    ensure

      scheduler.shutdown(:kill) if scheduler
    end
  end
end
