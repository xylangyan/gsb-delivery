# frozen_string_literal: true

module ArchiveHandoff
  class Gateway
    def initialize(submit: nil, query: nil, settle: nil, ack_volume: nil, object: nil)
      @submit_callback = submit
      @query_callback = query
      @settle_callback = settle
      @ack_volume_callback = ack_volume
      @object = object
    end

    def submit(submission)
      if @submit_callback
        @submit_callback.call(submission)
      elsif @object
        call_object(@object, :submit_package, :submit_submission, :submit, :deposit, submission)
      else
        raise Error, "no warehouse submit port configured"
      end
    end

    def query(key)
      if @query_callback
        @query_callback.call(key)
      elsif @object
        call_object(@object, :find_attempt, :query_attempt, :query, :find, :lookup, key)
      else
        raise Error, "no warehouse query port configured"
      end
    end

    def settle(key)
      if @settle_callback
        @settle_callback.call(key)
      elsif @object
        call_object(@object, :settle_attempt, :settle, :resolve, key)
      else
        raise Error, "no warehouse settle port configured"
      end
    end

    def ack_volume(ack)
      if @ack_volume_callback
        @ack_volume_callback.call(ack)
      elsif @object&.respond_to?(:acknowledge_volume)
        @object.acknowledge_volume(ack)
      elsif @object&.respond_to?(:ack_volume)
        @object.ack_volume(ack)
      end
    end

    private

    def call_object(object, *names, key)
      names.each do |name|
        next unless object.respond_to?(name)

        method = object.method(name)
        return invoke_method(method, key)
      end
      raise Error, "warehouse does not respond to any of #{names.first(names.length - 1).join(', ')}"
    end

    def invoke_method(method, key)
      parameters = method.parameters
      parameter_names = parameters.map { |_, name| name }.compact
      keyrest = parameters.any? { |type, _| type == :keyrest }

      if keyrest
        return method.call(**key)
      end

      keyword_parameters = parameters.select { |type, _| %i[key keyreq].include?(type) }
      if keyword_parameters.any?
        kwargs = {}
        keyword_parameters.each do |_, name|
          kwargs[name] = key[name] if key.key?(name)
        end
        return method.call(**kwargs)
      end

      required_positional = parameters.count { |type, _| %i[req opt].include?(type) }
      if required_positional >= 3
        occurrence = key[:occurrence_id] || key["occurrence_id"] || key[:occurrence]
        segment = key[:segment] || key[:segment_number] || key["segment"] || key["segment_number"]
        attempt = key[:attempt] || key[:attempt_number] || key["attempt"] || key["attempt_number"]
        return method.call(occurrence, segment, attempt)
      end

      method.call(key)
    rescue ArgumentError
      method.call(key)
    end
  end

  class MemoryWarehouse
    attr_reader :submission_calls, :received_calls, :accounts, :settlements, :late_calls

    def initialize(auto_commit: true)
      @mutex = Mutex.new
      @auto_commit = auto_commit
      @submission_calls = []
      @received_calls = []
      @accounts = {}
      @settlements = {}
      @late_calls = []
    end

    def submit_package(submission)
      @mutex.synchronize do
        record = deep_dup(submission)
        @submission_calls << record
        @received_calls << record
        key = attempt_key(record)
        duplicate = @submission_calls.count { |candidate| same_package?(candidate, record) } > 1
        record["duplicate"] = duplicate
        if @settlements[key] && result_status(@settlements[key]) == "unpostable"
          @late_calls << record
          return { "status" => "rejected_after_settlement", "key" => key, "duplicate" => duplicate }
        end

        if @auto_commit
          commit_locked(record, key)
          return { "status" => "committed", "key" => key, "duplicate" => duplicate }
        end
        { "status" => "received", "key" => key, "duplicate" => duplicate }
      end
    end
    alias submit submit_package

    def deliver_submission(submission, receipt_id: nil)
      @mutex.synchronize do
        record = deep_dup(submission)
        key = attempt_key(record)
        unless @received_calls.any? { |candidate| same_attempt?(candidate, record) }
          @submission_calls << record
          @received_calls << record
        end
        if @settlements[key] && result_status(@settlements[key]) == "unpostable"
          @late_calls << record unless @late_calls.include?(record)
          return { "status" => "rejected_after_settlement", "key" => key }
        end

        commit_locked(record, key, receipt_id: receipt_id)
      end
    end

    def find_attempt(key)
      normalized = normalize_key(key)
      @mutex.synchronize do
        return deep_dup(@settlements[normalized]) if @settlements.key?(normalized)
        return deep_dup(@accounts[normalized]) if @accounts.key?(normalized)

        segment_record = find_by_segment(normalized)
        segment_record ? deep_dup(segment_record) : nil
      end
    end
    alias query_attempt find_attempt
    alias query find_attempt

    def settle_attempt(key)
      normalized = normalize_key(key)
      @mutex.synchronize do
        return deep_dup(@settlements[normalized]) if @settlements.key?(normalized)

        if @accounts.key?(normalized)
          @settlements[normalized] = deep_dup(@accounts[normalized]).merge("settled" => true)
        else
          @settlements[normalized] = {
            "status" => "unpostable",
            "settled" => true,
            "key" => normalized,
            "at" => Time.now.utc.iso8601(6)
          }
        end
        deep_dup(@settlements[normalized])
      end
    end
    alias settle settle_attempt

    def submission_count(occurrence_id, segment = nil)
      @submission_calls.count do |call|
        same_occurrence = fetch_value(call, :occurrence_id, "occurrence_id") == occurrence_id
        same_segment = segment.nil? || fetch_value(call, :segment, :segment_number, "segment", "segment_number") == segment
        same_occurrence && same_segment
      end
    end

    def package_received?(digest)
      @received_calls.any? { |call| fetch_value(call, :package_digest, :digest, "package_digest", "digest") == digest }
    end

    private

    def commit_locked(record, key, receipt_id: nil)
      digest = fetch_value(record, :warehouse_digest, :digest, "warehouse_digest", "digest") ||
               fetch_value(record, :package_digest, "package_digest")
      receipt = {
        "id" => receipt_id || "receipt-#{key[:segment]}-#{key[:attempt]}",
        "occurrence_id" => key[:occurrence_id],
        "segment" => key[:segment],
        "attempt" => key[:attempt],
        "digest" => digest,
        "at" => Time.now.utc.iso8601(6)
      }
      @accounts[key] = {
        "status" => "committed",
        "receipt" => receipt,
        "digest" => digest
      }
      @settlements[key] = @accounts[key].merge("settled" => true)
      @accounts[key]
    end

    def same_attempt?(candidate, record)
      attempt_key(candidate) == attempt_key(record)
    end

    def same_package?(candidate, record)
      fetch_value(candidate, :package_digest, :digest, "package_digest", "digest") ==
        fetch_value(record, :package_digest, :digest, "package_digest", "digest")
    end

    def find_by_segment(key)
      matching = @accounts.select do |stored_key, _|
        stored_key[:occurrence_id] == key[:occurrence_id] && stored_key[:segment] == key[:segment]
      end
      matching.values.max_by { |record| record.dig("receipt", "attempt") || -1 }
    end

    def attempt_key(record)
      normalize_key(
        occurrence_id: fetch_value(record, :occurrence_id, "occurrence_id"),
        segment: fetch_value(record, :segment, :segment_number, "segment", "segment_number"),
        attempt: fetch_value(record, :attempt, :attempt_number, "attempt", "attempt_number")
      )
    end

    def normalize_key(key)
      if key.is_a?(Hash)
        {
          occurrence_id: fetch_value(key, :occurrence_id, "occurrence_id"),
          segment: Integer(fetch_value(key, :segment, :segment_number, "segment", "segment_number")),
          attempt: Integer(fetch_value(key, :attempt, :attempt_number, "attempt", "attempt_number"))
        }
      else
        raise ArgumentError, "warehouse key must be a hash"
      end
    end

    def result_status(result)
      fetch_value(result, :status, "status").to_s
    end

    def fetch_value(hash, *keys)
      keys.each do |key|
        key_s = key.to_s
        key_sym = key.to_sym
        return hash[key] if hash.key?(key)
        return hash[key_s] if hash.key?(key_s)
        return hash[key_sym] if hash.key?(key_sym)
      end
      nil
    end

    def deep_dup(object)
      case object
      when Hash
        object.each_with_object({}) { |(key, value), result| result[key] = deep_dup(value) }
      when Array
        object.map { |item| deep_dup(item) }
      else
        object
      end
    end
  end
end

require "time"
