# frozen_string_literal: true

require "fileutils"
require "json"
require "digest"

module ArchiveHandoff
  class Store
    STATE_FILE = "handoff-state.json"
    LOG_FILE = "stderr.jsonl"

    attr_reader :root

    def initialize(root)
      @root = File.expand_path(root)
      FileUtils.mkdir_p(@root)
      @lock_mutex = Mutex.new
    end

    def state_path
      File.join(@root, STATE_FILE)
    end

    def log_path
      File.join(@root, LOG_FILE)
    end

    def artifact_dir(occurrence_id)
      path = File.join(@root, "artifacts", key(occurrence_id))
      FileUtils.mkdir_p(path)
      path
    end

    def artifact_path(occurrence_id, name)
      File.join(artifact_dir(occurrence_id), name)
    end

    def write_artifact(occurrence_id, name, bytes)
      path = artifact_path(occurrence_id, name)
      binary = bytes.b
      File.open(path, "wb") { |file| file.write(binary) }
      [path, Digest::SHA256.hexdigest(binary)]
    end

    def read_artifact(path)
      File.binread(path)
    end

    def load_state
      JSON.parse(File.read(state_path), symbolize_names: true)
    rescue Errno::ENOENT
      { version: 1, occurrences: {} }
    end

    def save_state(state)
      FileUtils.mkdir_p(@root)
      tmp = "#{state_path}.tmp-#{Process.pid}-#{Thread.current.object_id}"
      File.open(tmp, "w") do |file|
        file.write(JSON.pretty_generate(state))
        file.fsync
      end
      File.rename(tmp, state_path)
    ensure
      File.unlink(tmp) if File.exist?(tmp) && tmp != state_path
    end

    def with_lock
      @lock_mutex.synchronize do
        File.open(File.join(@root, "handoff.lock"), File::RDWR | File::CREAT, 0o600) do |lock|
          lock.flock(File::LOCK_EX)
          yield
        end
      end
    end

    def mutate
      with_lock do
        state = load_state
        result = yield state
        save_state(state)
        result
      end
    end

    def occurrence(state, occurrence_id)
      state.fetch(:occurrences).fetch(key(occurrence_id).to_sym)
    rescue KeyError
      raise NotFound, "unknown occurrence: #{occurrence_id}"
    end

    def find_occurrence(state, occurrence_id)
      state.dig(:occurrences, key(occurrence_id).to_sym)
    end

    def put_occurrence(state, record)
      state[:occurrences][key(record.fetch(:occurrence_id)).to_sym] = record
    end

    def append_log(event)
      with_lock do
        event = event.merge(
          "seq" => next_log_seq,
          "at" => Time.now.utc.iso8601(6)
        )
        File.open(log_path, "a") do |file|
          file.flock(File::LOCK_EX)
          file.write(JSON.generate(event) << "\n")
          file.fsync
        end
        event
      end
    end

    def read_log
      return [] unless File.file?(log_path)

      File.readlines(log_path).map { |line| JSON.parse(line) }
    end

    def key(value)
      Digest::SHA256.hexdigest(value.to_s)
    end

    private

    def next_log_seq
      return 0 unless File.file?(log_path)

      count = 0
      File.foreach(log_path) { count += 1 }
      count
    end
  end
end
