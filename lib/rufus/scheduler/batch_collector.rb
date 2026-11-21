require 'digest'
require 'fileutils'
require 'json'
require 'securerandom'
require 'time'


class Rufus::Scheduler

  #
  # Collects the artifacts produced by two collection jobs for a batch,
  # verifies each one, and hands the verified pair to the merge step exactly
  # once.
  #
  # The collector is independent from rufus-scheduler's worker queue.
  # Everything needed to resume a batch lives under its :root directory:
  #
  #   <root>/batches/<batch>/state.json       durable per-batch record
  #   <root>/batches/<batch>/inputs/<key>/    immutable input snapshots
  #   <root>/batches/<batch>/work/            merge workspace (redoable)
  #   <root>/batches/<batch>/notifications/   archived deliveries
  #   <root>/published/<batch>/manifest.json  the one published manifest
  #   <root>/published/<batch>/result.json    the published result
  #   <root>/notifications/                   live delivery log
  #
  # A report describes one artifact of a batch:
  #
  #   collector.report(
  #     batch: 17, source: 'job_a', path: '/in/a.csv',
  #     digest: 'sha256:...', summary: '900 rows')
  #
  # Reports are idempotent. Both sources of a batch must verify before the
  # merge is queued; an incomplete batch costs no worker. Call #drain (for
  # example from a scheduled job) to run queued merges, or #recover after a
  # process restart to resume from the persistent directory.
  #
  class BatchCollector

    class Error < StandardError; end

    # A report that cannot become an accepted input of its batch. The batch is
    # sealed as failed; later notifications, even for an older same-named
    # file, do not re-open it.
    #
    class RejectedReport < Error; end

    # A batch is formed of the artifacts of two collection jobs.
    #
    SOURCE_COUNT = 2

    # Base class for the interruptions a caller can inject through #on.
    #
    class Interruption < StandardError; end

    # The batch is complete but its merge has not been queued yet.
    #
    class InterruptedBeforeQueue < Interruption; end

    # The result and the identities of both inputs are at the publication
    # location, but the scheduler has not recorded completion yet.
    #
    class InterruptedAfterPublish < Interruption; end

    attr_reader :root
    attr_accessor :merger
    attr_accessor :clock

    #
    # The callable receives (batch, inputs, work_dir) and returns a Hash.
    # +inputs+ is ordered like the required sources and each entry carries
    # 'source', 'path' (the original report location), 'snapshot_path' (the
    # immutable bytes accepted for this batch), 'digest' and 'summary'.
    #
    def initialize(root:, merger: nil, required_sources: nil, clock: nil,
                   scheduler: nil)

      @root = File.expand_path(root.to_s)
      @merger = merger
      @required_sources =
        if required_sources
          Array(required_sources).map(&:to_s).freeze
        else
          nil
        end
      @clock = clock || lambda { Time.now.utc }

      @locks = {}
      @locks_mutex = Mutex.new
      @hooks = {}
      @enqueued = {}
      @scheduler = nil

      FileUtils.mkdir_p(batches_dir)
      FileUtils.mkdir_p(published_dir)
      FileUtils.mkdir_p(notifications_dir)

      attach(scheduler) if scheduler
    end

    #
    # Hands merge work to a rufus-scheduler instance. A batch that is still
    # waiting for its sibling creates no job and therefore occupies no
    # worker.
    #
    def attach(scheduler)

      @scheduler = scheduler
      @enqueued.clear
      self
    end

    attr_reader :scheduler

    #
    # The fixed source jobs of every batch, or nil when a batch accepts the
    # first two distinct sources that verify.
    #
    def required_sources

      @required_sources
    end

    #
    # Registers a callback for a lifecycle event. Events:
    #
    # * :collected         both inputs are recorded, before the merge queue
    # * :merging           a merge attempt is about to run
    # * :published_files   publication files are in place, before the state
    #                      records completion
    # * :published         the batch is fully recorded as published
    #
    # The callback receives the batch id. Raising from it simulates a process
    # interruption at that point.
    #
    def on(event, &block)

      @hooks[event.to_sym] = block
      self
    end

    #
    # Entry point for a collection job.
    #
    # Returns a Hash describing the outcome. A rejected artifact raises
    # RejectedReport and seals the batch as failed.
    #
    def report(batch:, source:, path:, digest: nil, summary: nil,
               received_at: nil, **extra)

      batch = normalize_batch(batch)
      source = normalize_source(source)
      path = File.expand_path(path.to_s)
      received_at = (received_at || now).to_s

      fail RejectedReport, 'missing path' if path.empty?

      with_batch_lock(batch) do

        notification = persist_notification(
          batch, source, path, digest, summary, received_at, extra)

        state = load_state(batch)

        # A complete, validated manifest is the one accepted result of the
        # batch. It wins even if the state file is gone or still mid-flight.
        if manifest = manifest_for(batch, state)
          archive_notification(batch, notification)
          state = adopt_manifest(batch, state, manifest)
          return already_published(batch, state, source, path)
        end

        if state && state['status'] == 'published'
          archive_notification(batch, notification)
          return already_published(batch, state, source, path)
        end

        if state && state['status'] == 'failed'
          archive_notification(batch, notification)
          return sealed_report(batch, state, source, path)
        end

        state ||= new_state(batch)

        if required_sources
          unless required_sources.include?(source)
            archive_notification(batch, notification)
            fail RejectedReport,
              "source #{source.inspect} is not part of batch #{batch}"
          end
        elsif !state['inputs'].key?(source) &&
              state['inputs'].size >= SOURCE_COUNT

          archive_notification(batch, notification)
          compact_notifications(batch, state)
          return {
            'batch' => batch,
            'status' => state['status'],
            'source' => source,
            'path' => path,
            'accepted' => false,
            'reason' => 'batch already has two sources'
          }
        end

        begin
          actual_digest = verify!(path, digest)
        rescue RejectedReport => e
          archive_notification(batch, notification)
          fail_batch!(batch, state, e.message)
        end

        key = input_key(source, path)
        snapshot = store_snapshot(batch, key, path)

        record = {
          'batch' => batch,
          'source' => source,
          'path' => path,
          'digest' => actual_digest,
          'summary' => summary,
          'received_at' => received_at,
          'snapshot' => snapshot['relative'],
          'size' => snapshot['size']
        }

        previous = state['inputs'][source]

        if previous
          if previous['digest'] != record['digest']
            # The accepted artifact stays the input of this batch. A later
            # delivery with another digest is not a replacement.
            archive_notification(batch, notification)
            compact_notifications(batch, state)
            return outcome(batch, state, previous, false,
              'conflicting digest for an accepted source')
          end

          # The first verified artifact of a source stays the input. A repeat
          # delivery, even under another directory, changes nothing, but it
          # may resume a batch that was interrupted after collection.
          state = advance(batch, state)
          return outcome(batch, state, previous, true)
        end

        state['inputs'][source] = record
        state['updated_at'] = now.to_s
        write_state(batch, state)

        if complete?(state)

          fire(:collected, batch)

          state = queue(batch, state)
          return outcome(batch, state, record, true)
        end

        compact_notifications(batch, state)

        outcome(batch, state, record, true)
      end
    end

    #
    # Durably queues a complete batch for merging. This is deliberately a
    # separate step: a crash after both reports were recorded but before this
    # call leaves a complete "collecting" batch, which #recover will queue.
    #
    def queue(batch, state = nil)

      batch = normalize_batch(batch)

      with_batch_lock(batch) do

        state ||= load_state(batch)
        state = mark_queued(batch, state)
        schedule_merge(batch) if state && state['status'] == 'queued'

        state
      end
    end

    #
    # Records a complete batch as queued without handing it to a scheduler.
    #
    def mark_queued(batch, state)

      return nil unless state
      return state if state['status'] == 'published'
      return state if state['status'] == 'failed'
      return state unless complete?(state)
      return state if %w[ queued merging ].include?(state['status'])

      state['status'] = 'queued'
      state['queued_at'] = now.to_s
      state['updated_at'] = now.to_s
      write_state(batch, state)

      state
    end

    #
    # With an attached scheduler, a queued batch gets one at job. The job is
    # created only once the batch is complete, so an incomplete batch holds no
    # worker. Without a scheduler, callers use #drain.
    #
    def schedule_merge(batch, delay = 0.01)

      return unless @scheduler && @scheduler.up?

      @locks_mutex.synchronize do
        return if @enqueued[batch]
        @enqueued[batch] = true
      end

      @scheduler.at(
        Time.now + delay, discard_past: false,
        tag: "batch_collector:#{batch}"
      ) do
        @locks_mutex.synchronize { @enqueued.delete(batch) }
        drain(batch: batch)
      end

    rescue Rufus::Scheduler::NotRunningError

      @locks_mutex.synchronize { @enqueued.delete(batch) }
      nil
    end

    #
    # Runs all queued merges synchronously. Safe to call from a scheduled job
    # or after #recover. Batches already published are read back, never
    # merged again. An incomplete batch is skipped without holding a worker.
    #
    def drain(batch: nil)

      list = batch ? [ normalize_batch(batch) ] : discover_batches

      list.each do |b|

        with_batch_lock(b) do

          state = load_state(b)
          next unless state
          next if state['status'] == 'failed'

          if state['status'] == 'published'
            ensure_manifest!(b, state)
            next
          end

          if complete?(state) && state['status'] == 'collecting'
            state = mark_queued(b, state)
          end

          next unless complete?(state)
          next unless %w[ queued merging ].include?(state['status'])

          merge!(b, state)
        end
      end

      self
    end

    #
    # Rebuilds the live state from the persistent directory after a restart.
    # Complete batches that never made it to the queue are queued; merges that
    # published their files but did not record completion are finalized from
    # the manifest. The worker queue is not consulted.
    #
    def recover(batch: nil, merge: true)

      list = batch ? [ normalize_batch(batch) ] : discover_batches

      list.each do |b|

        with_batch_lock(b) do

          state = load_state(b)

          unless state
            # The state was lost but the publication survived; rebuild the
            # state from the manifest.
            manifest = read_manifest(b)
            state = adopt_manifest(b, nil, manifest) if manifest
          end

          next unless state
          next if state['status'] == 'failed'

          if state['status'] == 'published'
            next if ensure_manifest!(b, state)
            state = load_state(b)
            next unless state
          end

          if complete?(state)
            state = mark_queued(b, state) if state['status'] == 'collecting'
            merge!(b, state) if merge
          end
        end
      end

      self
    end

    def state(batch)

      load_state(normalize_batch(batch))
    end

    #
    # The one verified manifest of the batch, or nil. A directory that exists
    # without a valid manifest does not count as a completed batch.
    #
    def manifest(batch)

      b = normalize_batch(batch)
      state = load_state(b)
      manifest_for(b, state)
    end

    #
    # The published result of a batch, read back from the single manifest.
    # Returns nil when the batch has no valid publication.
    #
    def result(batch)

      m = manifest(batch)
      m && m['result']
    end

    def published?(batch)

      ! manifest(batch).nil?
    end

    def batches

      discover_batches
    end

    #
    # Delivery records still waiting on a batch, plus archived ones.
    #
    def notifications(batch: nil)

      pattern =
        if batch
          File.join(notifications_dir, "#{normalize_batch(batch)}-*.json")
        else
          File.join(notifications_dir, '*.json')
        end

      archived =
        if batch
          b = normalize_batch(batch)
          File.join(batch_dir(b), 'notifications', '*.json')
        end

      (Dir[pattern] + (archived ? Dir[archived] : []))
        .sort
        .map { |path| JSON.parse(File.read(path)) rescue nil }
        .compact
    end

    protected

    def now

      @clock.call
    end

    def normalize_batch(batch)

      s = batch.to_s
      fail ArgumentError, 'empty batch' if s.empty?
      unless s =~ /\A[A-Za-z0-9_.-]+\z/
        fail ArgumentError, "invalid batch #{batch.inspect}"
      end
      s
    end

    def normalize_source(source)

      s = source.to_s
      fail ArgumentError, 'empty source' if s.empty?
      s
    end

    def with_batch_lock(batch)

      lock = @locks_mutex.synchronize { @locks[batch] ||= Mutex.new }

      if lock.owned?
        yield
      else
        lock.synchronize { yield }
      end
    end

    def fire(event, batch)

      hook = @hooks[event.to_sym]
      hook.call(batch) if hook
    end

    def batches_dir; File.join(@root, 'batches'); end
    def published_dir; File.join(@root, 'published'); end
    def notifications_dir; File.join(@root, 'notifications'); end

    def batch_dir(batch); File.join(batches_dir, batch); end
    def state_path(batch); File.join(batch_dir(batch), 'state.json'); end
    def inputs_dir(batch); File.join(batch_dir(batch), 'inputs'); end
    def work_dir(batch); File.join(batch_dir(batch), 'work'); end
    def published_batch_dir(batch); File.join(published_dir, batch); end
    def published_manifest_path(batch)
      File.join(published_batch_dir(batch), 'manifest.json')
    end
    def published_result_path(batch)
      File.join(published_batch_dir(batch), 'result.json')
    end

    def input_key(source, path)

      "#{source}-#{Digest::SHA256.hexdigest(path)[0, 16]}"
    end

    def digest_of(path)

      "sha256:#{Digest::SHA256.file(path).hexdigest}"
    end

    def verify!(path, expected_digest)

      fail RejectedReport, "missing artifact #{path}" unless File.file?(path)

      actual = digest_of(path)

      if expected_digest && !expected_digest.to_s.empty?
        want = expected_digest.to_s
        want = "sha256:#{want}" unless want.include?(':')
        unless want == actual
          fail RejectedReport,
            "digest mismatch for #{path} (expected #{want}, got #{actual})"
        end
      end

      actual
    end

    def store_snapshot(batch, key, path)

      digest = digest_of(path)
      # Content-addressed, so a later file with the same name can never
      # overwrite the bytes accepted for this batch.
      key = "#{key}-#{digest.sub('sha256:', '')[0, 12]}"

      dir = File.join(inputs_dir(batch), key)
      FileUtils.mkdir_p(dir)
      target = File.join(dir, File.basename(path))

      unless File.file?(target) && digest_of(target) == digest
        tmp = "#{target}.tmp-#{SecureRandom.hex(6)}"
        FileUtils.cp(path, tmp)
        File.rename(tmp, target)
      end

      {
        'relative' => File.join('inputs', key, File.basename(path)),
        'path' => target,
        'digest' => digest,
        'size' => File.size(target)
      }
    end

    def new_state(batch)

      {
        'batch' => batch,
        'status' => 'collecting',
        'required_sources' => required_sources,
        'inputs' => {},
        'created_at' => now.to_s,
        'updated_at' => now.to_s,
        'merge_attempts' => 0,
        'manifest' => nil,
        'failure' => nil
      }
    end

    #
    # With a fixed source list, every listed source is required. Otherwise the
    # batch is complete as soon as two distinct sources verified, and the
    # first two determine the identity of the batch inputs.
    #
    def complete?(state)

      if required_sources
        required_sources.all? { |s| state['inputs'].key?(s) }
      else
        state['inputs'].size >= SOURCE_COUNT
      end
    end

    def source_names(state)

      names =
        if required_sources
          required_sources
        else
          state['inputs'].keys.sort.first(SOURCE_COUNT)
        end

      names
    end

    #
    # Moves a state forward as far as it can without running a merge. A
    # repeated delivery of the latest report thus requeues a batch that was
    # complete but interrupted before its merge was queued.
    #
    def advance(batch, state)

      return state if state['status'] == 'failed'

      if state['status'] == 'published'
        ensure_manifest!(batch, state)
        return state
      end

      queue(batch, state) if complete?(state)
      compact_notifications(batch, state)

      state
    end

    #
    # Runs one merge attempt. A valid manifest wins over everything: it means
    # a previous attempt published the result and the process stopped before
    # recording completion, so no re-merge happens.
    #
    def merge!(batch, state)

      # Serialize merging and publication per batch, across collector
      # instances (and processes) sharing the same root. Whoever acquires the
      # lock re-reads the manifest first, so a batch published meanwhile is
      # adopted, never merged again.
      with_merge_lock(batch) do

        # Whoever held the lock before us may have advanced the state or
        # published already. Never write back a stale in-memory copy.
        state = load_state(batch) || state

        if manifest = read_manifest_for(batch, state)
          next complete_publication(batch, state, manifest)
        end

        state['status'] = 'merging'
        state['merge_attempts'] += 1
        state['updated_at'] = now.to_s
        write_state(batch, state)

        fire(:merging, batch)

        names = source_names(state)
        inputs = names.map { |s| state['inputs'][s] }
        fail Error, 'incomplete batch' unless inputs.all?

        FileUtils.rm_rf(work_dir(batch))
        FileUtils.mkdir_p(work_dir(batch))

        result =
          if merger
            merger.call(
              batch,
              inputs.map { |i| merge_input(batch, i) },
              work_dir(batch))
          else
            default_merge(batch, inputs)
          end

        result = {} if result.nil?
        fail Error, 'merger must return a Hash' unless result.is_a?(Hash)

        publish(batch, state, inputs, result)
      end
    rescue Interruption

      # Simulated process interruption: the durable files stay exactly as they
      # are, and the next process recovers from them.
      raise
    rescue RejectedReport, Error => e
      fail_batch!(batch, state, e.message)
    rescue StandardError => e
      # The publication files may be complete even though recording completion
      # failed; read them back instead of merging again.
      if manifest = read_manifest_for(batch, state)
        return complete_publication(batch, load_state(batch) || state, manifest)
      end

      state['status'] = 'queued'
      state['last_error'] = "#{e.class}: #{e.message}"
      state['updated_at'] = now.to_s
      write_state(batch, state)
      schedule_merge(batch)
      raise
    end

    #
    # Cross-instance merge lock. Distinct collector instances sharing a root
    # use distinct file descriptions, so flock serializes them even in the
    # same process.
    #
    def with_merge_lock(batch)

      path = File.join(batch_dir(batch), 'merge.lock')
      FileUtils.mkdir_p(File.dirname(path))

      File.open(path, File::RDWR | File::CREAT, 0o644) do |f|
        f.flock(File::LOCK_EX)
        begin
          yield
        ensure
          f.flock(File::LOCK_UN)
        end
      end
    end

    #
    # The manifest of a batch, whether or not its state file survived. When
    # the state is available, the manifest must match its accepted inputs.
    #
    def manifest_for(batch, state)

      if state
        return nil unless complete?(state)
        read_manifest_for(batch, state)
      else
        read_manifest(batch)
      end
    end

    #
    # Makes a validated manifest the recorded completion. Used when the
    # publication is present but the state was lost or left mid-flight.
    #
    def adopt_manifest(batch, state, manifest)

      state ||= new_state(batch)

      manifest['inputs'].each do |i|
        state['inputs'][i['source'].to_s] ||= {
          'source' => i['source'].to_s,
          'path' => i['path'],
          'digest' => i['digest'],
          'summary' => i['summary'],
          'size' => i['size']
        }
      end

      state['status'] = 'published'
      state['manifest'] = manifest
      state['published_at'] = manifest['published_at']
      state['failure'] = nil
      state['updated_at'] = now.to_s
      write_state(batch, state)

      compact_notifications(batch, state)

      state
    end

    #
    # Reads a publication only if it belongs to this batch's accepted inputs.
    # This is what keeps a later same-named file from being treated as an
    # input after a restart.
    #
    def read_manifest_for(batch, state)

      names = source_names(state)
      manifest = read_manifest(batch, names)
      return nil unless manifest

      expected = names.map { |n| state['inputs'][n]['digest'] }
      actual = names.map do |n|
        entry = manifest['inputs'].find { |i| i['source'].to_s == n }
        entry && entry['digest']
      end

      actual == expected ? manifest : nil
    end

    #
    # The merger receives the immutable snapshot, not the original report
    # location: a later same-named file is not an input of this batch.
    #
    def merge_input(batch, input)

      {
        'batch' => batch,
        'source' => input['source'],
        'path' => input['path'],
        'snapshot_path' => File.join(batch_dir(batch), input['snapshot']),
        'digest' => input['digest'],
        'summary' => input['summary'],
        'size' => input['size']
      }
    end

    def default_merge(batch, inputs)

      {
        'batch' => batch,
        'sources' => inputs.map { |i| i['source'] },
        'digests' => inputs.map { |i| i['digest'] },
        'summaries' => inputs.map { |i| i['summary'] }
      }
    end

    #
    # Builds the publication in a staging directory and renames it into place,
    # so readers either see the previous state or the complete result plus
    # manifest. Only after that does the durable state record completion.
    #
    def publish(batch, state, inputs, result)

      staging = "#{published_batch_dir(batch)}.staging-#{SecureRandom.hex(6)}"
      FileUtils.mkdir_p(staging)

      files = []
      Dir[File.join(work_dir(batch), '**', '*')].sort.each do |path|
        next unless File.file?(path)
        relative = path[(work_dir(batch).length + 1)..-1]
        target = File.join(staging, relative)
        FileUtils.mkdir_p(File.dirname(target))
        FileUtils.cp(path, target)
        files << {
          'path' => relative,
          'digest' => "sha256:#{Digest::SHA256.file(target).hexdigest}",
          'size' => File.size(target)
        }
      end

      result_payload = JSON.pretty_generate(
        'batch' => batch,
        'result' => result,
        'inputs' => inputs.map { |i| identity(i) })

      atomic_write(File.join(staging, 'result.json'), result_payload)

      manifest = {
        'batch' => batch,
        'status' => 'published',
        'published_at' => now.to_s,
        'inputs' => inputs.map { |i| identity(i).merge('size' => i['size']) },
        'files' => files,
        'result_digest' => "sha256:#{Digest::SHA256.hexdigest(result_payload)}",
        'result_path' => published_result_path(batch),
        'result' => result
      }

      atomic_write(File.join(staging, 'manifest.json'),
        JSON.pretty_generate(manifest))

      # Renaming a complete staging directory into place means a reader sees
      # either the previous publication or the new one, never a partial mix.
      # A previous publication, if any, is replaced.
      if File.exist?(published_batch_dir(batch))
        trash = "#{published_batch_dir(batch)}.old-#{SecureRandom.hex(6)}"
        File.rename(published_batch_dir(batch), trash)
        File.rename(staging, published_batch_dir(batch))
        FileUtils.rm_rf(trash)
      else
        File.rename(staging, published_batch_dir(batch))
      end

      fire(:published_files, batch)

      complete_publication(batch, state, manifest)
    ensure

      FileUtils.rm_rf(staging) if staging && File.directory?(staging)
    end

    def identity(input)

      {
        'source' => input['source'],
        'path' => input['path'],
        'digest' => input['digest'],
        'summary' => input['summary']
      }
    end

    #
    # Records completion. The manifest has already been made visible, so this
    # is the last, idempotent step.
    #
    def complete_publication(batch, state, manifest)

      state['status'] = 'published'
      state['manifest'] = manifest
      state['published_at'] = manifest['published_at']
      state['failure'] = nil
      state['updated_at'] = now.to_s
      write_state(batch, state)

      compact_notifications(batch, state)

      fire(:published, batch)

      state
    end

    def fail_batch!(batch, state, message)

      state['status'] = 'failed'
      state['failure'] = message
      state['failed_at'] = now.to_s
      state['updated_at'] = now.to_s
      write_state(batch, state)

      compact_notifications(batch, state)

      fail RejectedReport, message
    end

    def ensure_manifest!(batch, state)

      manifest = state && complete?(state) ? read_manifest_for(batch, state) : nil

      unless manifest
        # There is no valid manifest for this batch's inputs: not done.
        if state
          state['status'] = 'queued'
          state['manifest'] = nil
          state['updated_at'] = now.to_s
          write_state(batch, state)
        end
        return nil
      end

      state['manifest'] = manifest
      manifest
    end

    def already_published(batch, state, source, path)

      {
        'batch' => batch,
        'status' => 'published',
        'source' => source,
        'path' => path,
        'accepted' => false,
        'reason' => 'already published',
        'manifest' => state['manifest']
      }
    end

    def sealed_report(batch, state, source, path)

      {
        'batch' => batch,
        'status' => 'failed',
        'source' => source,
        'path' => path,
        'accepted' => false,
        'reason' => 'batch failed',
        'failure' => state['failure']
      }
    end

    def outcome(batch, state, record, accepted, reason = nil)

      h = {
        'batch' => batch,
        'status' => state['status'],
        'source' => record['source'],
        'path' => record['path'],
        'digest' => record['digest'],
        'summary' => record['summary'],
        'accepted' => accepted
      }
      h['reason'] = reason if reason
      h
    end

    #
    # A manifest only counts when it names this batch, has an identity for
    # every input of the batch, and its result file still matches the recorded
    # digest. When +expected_sources+ is given, the identities must be those
    # inputs exactly; a later same-named file can never be substituted.
    #
    def read_manifest(batch, expected_sources = nil)

      path = published_manifest_path(batch)
      return nil unless File.file?(path)

      data = JSON.parse(File.read(path))
      return nil unless data.is_a?(Hash)
      return nil unless data['batch'].to_s == batch.to_s
      return nil unless data['status'] == 'published'
      return nil unless data['inputs'].is_a?(Array)

      expected =
        if expected_sources
          expected_sources.map(&:to_s)
        elsif required_sources
          required_sources
        else
          nil
        end

      size = expected ? expected.size : SOURCE_COUNT
      return nil unless data['inputs'].size == size

      seen = data['inputs'].map { |i| i['source'].to_s }
      return nil unless seen.uniq.size == seen.size
      return nil if expected && seen.sort != expected.sort

      result_path = data['result_path'].to_s
      return nil unless result_path == published_result_path(batch)
      return nil unless File.file?(result_path)
      return nil unless digest_of(result_path) == data['result_digest']

      data

    rescue JSON::ParserError
      nil
    end

    #
    # Returns the durable state. A state that claims publication is checked
    # against its manifest: when the manifest is not valid for this batch the
    # state cannot count as published.
    #
    def load_state(batch)

      path = state_path(batch)
      return nil unless File.file?(path)

      state = JSON.parse(File.read(path))

      if state['status'] == 'published' && manifest_for(batch, state).nil?

        state['status'] = 'queued'
        state['manifest'] = nil
        write_state(batch, state)
      end

      state

    rescue JSON::ParserError
      nil
    end

    def write_state(batch, state)

      FileUtils.mkdir_p(batch_dir(batch))
      atomic_write(state_path(batch), JSON.pretty_generate(state))
    end

    def atomic_write(path, payload)

      FileUtils.mkdir_p(File.dirname(path))
      tmp = "#{path}.tmp-#{SecureRandom.hex(6)}"
      File.open(tmp, 'wb') do |f|
        f.write(payload)
        f.flush
        f.fsync
      end
      File.rename(tmp, path)
      path
    end

    def persist_notification(batch, source, path, digest, summary,
                             received_at, extra)

      record = {
        'batch' => batch,
        'source' => source,
        'path' => path,
        'digest' => digest,
        'summary' => summary,
        'received_at' => received_at
      }
      extra.each { |k, v| record[k.to_s] = v }

      # The identity of a delivery is its batch, source, location, digest and
      # summary. Re-delivery at another time is the same notification.
      identity = record.reject { |k, _| k == 'received_at' }
      id = Digest::SHA256.hexdigest(JSON.generate(identity))
      file = File.join(notifications_dir, "#{batch}-#{id}.json")
      return file if File.file?(file)

      atomic_write(file, JSON.pretty_generate(record))
      file
    end

    def archive_notification(batch, path)

      return unless File.file?(path)

      dir = File.join(batch_dir(batch), 'notifications')
      FileUtils.mkdir_p(dir)
      target = File.join(dir, File.basename(path))
      File.rename(path, target)
    end

    #
    # Once a batch is published or failed, its delivery records are moved out
    # of the live set. They remain readable through #notifications.
    #
    def compact_notifications(batch, state)

      return unless state && %w[ published failed ].include?(state['status'])

      Dir[File.join(notifications_dir, "#{batch}-*.json")].each do |path|
        archive_notification(batch, path)
      end
    end

    def discover_batches

      (Dir[File.join(batches_dir, '*')] + Dir[File.join(published_dir, '*')])
        .select { |d| File.directory?(d) && ! staging_dir?(d) }
        .map { |d| File.basename(d) }
        .uniq
        .sort
    end

    def staging_dir?(path)

      b = File.basename(path)
      b.include?('.staging-') || b.include?('.old-')
    end
  end
end
