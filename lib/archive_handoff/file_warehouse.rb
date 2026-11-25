# frozen_string_literal: true

require "fileutils"
require "json"

module ArchiveHandoff
  class FileWarehouse
    def initialize(root, auto_commit: true)
      @root = File.expand_path(root)
      @auto_commit = auto_commit
      FileUtils.mkdir_p(@root)
      @state_path = File.join(@root, "warehouse.json")
    end

    def submit_package(submission)
      mutate do |state|
        record = deep_dup(submission)
        state["submission_calls"] << record
        key = key_for(record)
        if state["settlements"][key] && state["settlements"][key]["status"] == "unpostable"
          state["late_calls"] << record
          return { "status" => "rejected_after_settlement", "key" => key }
        end
        if @auto_commit
          commit_locked(state, record, key)
          { "status" => "committed", "key" => key }
        else
          { "status" => "received", "key" => key }
        end
      end
    end
    alias submit submit_package

    def deliver_submission(submission)
      mutate do |state|
        record = deep_dup(submission)
        key = key_for(record)
        commit_locked(state, record, key) unless state["settlements"][key] && state["settlements"][key]["status"] == "unpostable"
        state["settlements"][key] || { "status" => "unpostable", "settled" => true }
      end
    end

    def find_attempt(key)
      with_key(key) do |state, normalized|
        state["settlements"][normalized] || state["accounts"][normalized]
      end
    end
    alias query_attempt find_attempt
    alias query find_attempt

    def settle_attempt(key)
      with_key(key) do |state, normalized|
        state["settlements"][normalized] ||= if state["accounts"][normalized]
                                              state["accounts"][normalized].merge("settled" => true)
                                            else
                                              { "status" => "unpostable", "settled" => true }
                                            end
        deep_dup(state["settlements"][normalized])
      end
    end
    alias settle settle_attempt

    def submission_count(occurrence_id, segment = nil)
      read_state["submission_calls"].count do |call|
        call["occurrence_id"] == occurrence_id && (segment.nil? || call["segment"] == segment)
      end
    end

    private

    def default_state
      { "accounts" => {}, "settlements" => {}, "submission_calls" => [], "late_calls" => [] }
    end

    def read_state
      JSON.parse(File.read(@state_path))
    rescue Errno::ENOENT
      default_state
    end

    def mutate
      File.open(File.join(@root, "warehouse.lock"), File::RDWR | File::CREAT, 0o600) do |lock|
        lock.flock(File::LOCK_EX)
        state = read_state
        result = yield state
        tmp = "#{@state_path}.tmp-#{Process.pid}"
        File.write(tmp, JSON.pretty_generate(state))
        File.rename(tmp, @state_path)
        result
      end
    end

    def with_key(key)
      normalized = normalize_key(key)
      string_key = serialize_key(normalized)
      mutate { |state| yield state, string_key }
    end

    def normalize_key(key)
      {
        occurrence_id: fetch(key, :occurrence_id, "occurrence_id", :occurrence, "occurrence"),
        segment: Integer(fetch(key, :segment, "segment", :segment_number, "segment_number")),
        attempt: Integer(fetch(key, :attempt, "attempt", :attempt_number, "attempt_number"))
      }
    end

    def serialize_key(key)
      "#{key[:occurrence_id]}##{key[:segment]}##{key[:attempt]}"
    end

    def key_for(record)
      serialize_key(normalize_key(record))
    end

    def fetch(hash, *names)
      names.each do |name|
        return hash[name] if hash.key?(name)
        return hash[name.to_s] if hash.key?(name.to_s)
      end
      nil
    end

    def commit_locked(state, record, key)
      digest = record[:package_digest] || record["package_digest"] || record[:digest] || record["digest"]
      receipt = {
        "id" => "receipt-#{key}",
        "occurrence_id" => record[:occurrence_id] || record["occurrence_id"],
        "segment" => record[:segment] || record["segment"],
        "attempt" => record[:attempt] || record["attempt"],
        "digest" => digest
      }
      state["accounts"][key] = { "status" => "committed", "receipt" => receipt, "digest" => digest }
      state["settlements"][key] = state["accounts"][key].merge("settled" => true)
    end

    def deep_dup(object)
      case object
      when Hash then object.each_with_object({}) { |(k, v), h| h[k] = deep_dup(v) }
      when Array then object.map { |v| deep_dup(v) }
      else object
      end
    end
  end
end
