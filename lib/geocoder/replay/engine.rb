require 'digest'
require 'geocoder/replay/errors'
require 'geocoder/replay/runtime'
require 'geocoder/replay/scenario'

module Geocoder
  module Replay
    ##
    # Executes operations against a set of process runtimes in one specified
    # global order of (slot, operation index) pairs.
    #
    # The engine never infers an order. It takes one, runs it, and reports:
    #   * each fetch: cache key, source body, marker, parse outcome
    #   * each result: candidate data, coordinates, inherited marker
    #   * each cache read/write/delete with its physical key
    #
    # Fetch uses Geocoder's real fetch path, so cache reads, writes and the
    # @cache_hit marker are the gem's.
    #
    class Engine
      Step = Struct.new(:slot, :operation, :index, :label, keyword_init: true)
      Event = Struct.new(:sequence, :step, :kind, :message, :data, keyword_init: true)

      attr_reader :scenario, :runtimes, :events, :violations, :results, :fetches, :order

      def initialize(scenario, record_cache: true)
        @scenario = scenario
        @record_cache = record_cache
        reset
      end

      def reset
        @runtimes = {}
        @scenario.processes.each_value do |process|
          @runtimes[process[:id]] = Runtime.new(process, @scenario.registry, record: @record_cache)
        end
        @events = []
        @violations = []
        @results = []
        @fetches = []
        @sequence = 0
        @slot_fetches = Hash.new { |hash, key| hash[key] = [] }
        @order = []
        @last_successful = nil
      end

      def steps
        @scenario.slots.flat_map do |slot|
          slot[:operations].each_with_index.map do |operation, index|
            Step.new(slot: slot, operation: operation, index: index, label: "#{slot[:id]}:#{index + 1}")
          end
        end
      end

      def natural_order
        steps.map { |step| [step.slot[:id], step.index] }
      end

      def run(order = nil)
        reset
        @order = normalize_order(order || @scenario.order || natural_order)
        @order.each do |slot_id, index|
          step = step_for(slot_id, index)
          begin
            execute(step)
            @last_successful = step.label
          rescue Violation => e
            @violations << violation_record(step, e)
            emit(step, :violation, e.message, e.to_h)
            break
          end
        end
        self
      end

      def ok?
        @violations.empty?
      end

      ##
      # State checkpoint for branching. Captures every runtime plus the
      # accumulated report arrays. #rollback restores all of it, so a failed
      # branch cannot leak cache entries, queued responses or markers.
      ##
      def checkpoint
        {
          runtimes: @runtimes.transform_values(&:snapshot),
          events: @events.length,
          violations: @violations.length,
          results: @results.length,
          fetches: @fetches.length,
          sequence: @sequence,
          slot_fetches: @slot_fetches.transform_values(&:dup),
          order: @order.dup,
          last_successful: @last_successful
        }
      end

      def rollback(checkpoint)
        checkpoint[:runtimes].each do |process_id, snapshot|
          @runtimes.fetch(process_id).restore(snapshot)
        end
        @events.slice!(checkpoint[:events]..-1) if @events.length > checkpoint[:events]
        @violations.slice!(checkpoint[:violations]..-1) if @violations.length > checkpoint[:violations]
        @results.slice!(checkpoint[:results]..-1) if @results.length > checkpoint[:results]
        @fetches.slice!(checkpoint[:fetches]..-1) if @fetches.length > checkpoint[:fetches]
        @sequence = checkpoint[:sequence]
        @slot_fetches = checkpoint[:slot_fetches].transform_values(&:dup)
        @slot_fetches.default_proc = proc { |hash, key| hash[key] = [] }
        @order = checkpoint[:order].dup
        @last_successful = checkpoint[:last_successful]
      end

      ##
      # Execute one step, returning nil on success or the Violation without
      # recording it. Used by the solver's branching search.
      ##
      def step_once(slot_id, index)
        step = step_for(slot_id, index)
        @order << [slot_id.to_s, index]
        execute(step)
        nil
      rescue Violation => e
        e
      end

      def runtime(process_id)
        @runtimes.fetch(process_id.to_s) do
          raise Violation.new('unknown_process', "unknown process #{process_id.inspect}")
        end
      end

      def step_for(slot_id, index)
        steps.find { |step| step.slot[:id] == slot_id.to_s && step.index == Integer(index) } or
          raise Violation.new('unknown_operation', "no operation #{slot_id}:#{index + 1}")
      end

      def provenance
        @results.map do |record|
          {
            step: record[:step],
            process: record[:runtime],
            query: record[:query],
            key: record[:key],
            candidate_index: record[:candidate],
            body: record[:body],
            fetch: record[:fetch],
            cache_hit: record[:marker],
            coordinates: record[:coordinates],
            address: record[:address],
            evidence: record[:evidence]
          }
        end
      end

      def cache_trace
        @runtimes.flat_map do |process_id, runtime|
          runtime.cache_events.each_with_index.map do |event, index|
            body = event[:body]
            {
              process: process_id,
              index: index + 1,
              op: event[:op],
              key: event[:key],
              body: body && @scenario.registry.find_by_raw(body)&.id
            }
          end
        end
      end

      private

      def normalize_order(order)
        raise InputError, 'order must be an array of (slot, operation) pairs' unless order.is_a?(Array)

        expected = steps.map { |step| [step.slot[:id], step.index] }
        actual = order.map { |entry| order_entry(entry) }
        check_intra_slot!(actual)
        unless actual.sort == expected.sort
          missing = expected - actual
          extra = actual - expected
          duplicated = actual.group_by(&:itself).select { |_, group| group.size > 1 }.keys
          problems = []
          problems << "missing #{label_list(missing)}" if missing.any?
          problems << "unknown #{label_list(extra)}" if extra.any?
          problems << "duplicated #{label_list(duplicated)}" if duplicated.any?
          raise InputError, "order must list every operation exactly once (#{problems.join('; ')})"
        end
        actual
      end

      ##
      # Pairs are zero-based (the shape emitted by Scenario#order); strings and
      # hashes are one-based, matching the way operators read their logs.
      ##
      def order_entry(entry)
        if entry.is_a?(Array)
          return [entry[0].to_s, Integer(entry[1])]
        end

        slot_id, operation =
          if entry.is_a?(String) && entry.include?(':')
            entry.split(':', 2)
          elsif entry.is_a?(Hash)
            hash = entry.each_with_object({}) { |(key, value), out| out[key.to_s] = value }
            [hash['slot'], hash['operation'] || hash['index']]
          else
            raise InputError, "bad order entry #{entry.inspect}"
          end
        [slot_id.to_s, Integer(operation) - 1]
      rescue ArgumentError, TypeError
        raise InputError, "bad order entry #{entry.inspect}"
      end

      ##
      # A slot's own log fixes the order of its operations. An interleaving may
      # place operations of different slots between them, but may never swap two
      # operations of the same slot.
      ##
      def check_intra_slot!(actual)
        actual.group_by(&:first).each do |slot_id, pairs|
          indices = pairs.map(&:last)
          next if indices == indices.sort

          raise InputError,
                "slot #{slot_id}: operations must keep their log order " \
                "(got #{indices.map { |index| "#{slot_id}:#{index + 1}" }.join(', ')})"
        end
      end

      def label_list(pairs)
        pairs.map { |slot, index| "#{slot}:#{index + 1}" }.join(', ')
      end

      def execute(step)
        operation = step.operation
        case operation[:op]
        when :note
          emit(step, :note, operation[:message], {})
        when :respond
          deliver(step, operation)
        when :search, :fetch
          perform_fetch(step, operation)
        when :result
          result(step, operation)
        when :cache_delete
          delete(step, operation)
        when :cache_write
          write(step, operation)
        else
          raise InputError, "unsupported operation #{operation[:op].inspect}"
        end
      end

      def deliver(step, operation)
        runtime = runtime(step.slot[:process])
        query = runtime.lookup.query_for(operation[:query], operation[:options] || {})
        key = runtime.cache_key(query)
        response = runtime.enqueue(query, key, operation[:body])
        emit(step, :deliver,
             "response #{operation[:body].id} arrives for #{operation[:query].inspect}",
             response.to_h.merge(process: runtime.id))
      end

      def perform_fetch(step, operation)
        runtime = runtime(step.slot[:process])
        query = runtime.lookup.query_for(operation[:query], operation[:options] || {})
        key = runtime.cache_key(query)
        body_spec = operation[:body]

        raw =
          if body_spec
            runtime.with_override(body_spec, code: body_spec.code, headers: body_spec.headers) do
              runtime.lookup.fetch_raw(query)
            end
          else
            runtime.lookup.fetch_raw(query)
          end

        marker = runtime.marker
        parse_error = Body.parse_error(raw)
        candidates = parse_error.nil? ? runtime.lookup.candidates_for(raw) : []
        body = @scenario.registry.find_by_raw(raw)
        body ||= body_spec if body_spec && body_spec.raw == raw
        source = body ? body.id : "inline@#{Digest::SHA256.hexdigest(raw.to_s)[0, 12]}"
        fetched = runtime.record_fetch(
          query: operation[:query], key: key, raw: raw,
          body: body, marker: marker, source: source,
          parse_error: parse_error, candidates: candidates,
          id: operation[:id]
        )
        record = fetched.merge(step: step.label)
        @fetches << record
        @slot_fetches[step.slot[:id]] << fetched
        emit(step, :fetch,
             "#{operation[:query].inspect} -> #{fetched[:id]} from #{source} (cache_hit=#{marker.inspect})",
             record)

        check_expected(operation, :cache_hit, marker, 'marker_mismatch')
        stamp_results(step, runtime, operation[:results], fetched) if operation[:results]
        fetched
      end

      def result(step, operation)
        runtime = runtime(step.slot[:process])
        fetched =
          if operation[:fetch]
            runtime.fetch(operation[:fetch])
          else
            slot_fetches = @slot_fetches[step.slot[:id]]
            matching = slot_fetches.reverse.find do |item|
              operation[:query].nil? || item[:query].to_s == operation[:query].to_s
            end
            matching || slot_fetches.last
          end
        unless fetched
          raise Violation.new(
            'no_fetch',
            "result for #{operation[:query].inspect} has no preceding fetch in slot #{step.slot[:id]}"
          )
        end

        body = operation[:body] || fetched[:body]
        candidates = body ? runtime.lookup.candidates_for(body.raw) : fetched[:candidates]
        candidate = take_candidate(operation, body, candidates)
        record = result_record(step, runtime, operation, fetched, body, candidate)
        @results << record
        emit(step, :result, describe_result(record), record)
        verify_result(operation, record)
        record
      end

      def stamp_results(step, runtime, specs, fetched)
        specs.each do |spec|
          body = spec[:body] || fetched[:body]
          candidates = body ? runtime.lookup.candidates_for(body.raw) : fetched[:candidates]
          candidate = take_candidate(spec, body, candidates)
          record = result_record(step, runtime, spec.merge(query: fetched[:query]), fetched, body, candidate)
          @results << record
          emit(step, :result, describe_result(record), record)
          verify_result(spec, record)
        end
      end

      def take_candidate(spec, body, candidates)
        index = spec[:candidate]
        if candidates.size <= index
          raise Violation.new(
            'candidate_missing',
            "candidate #{index} requested from #{body ? body.id : 'the fetched body'} " \
            "which has #{candidates.size} candidate(s)",
            candidate: index, available: candidates.size, body: body&.id
          )
        end
        candidates[index]
      end

      def result_record(step, runtime, operation, fetched, body, candidate)
        {
          step: step.label,
          runtime: runtime.id,
          query: operation[:query] || fetched[:query],
          key: fetched[:key],
          candidate: operation[:candidate],
          body: body ? body.id : fetched[:source],
          fetch: fetched[:id],
          marker: runtime.marker,
          coordinates: runtime.lookup.coordinates_for(candidate),
          address: runtime.lookup.address_for(candidate),
          expected_coordinates: operation[:coordinates],
          expected_address: operation[:address],
          evidence: operation[:evidence]
        }
      end

      def describe_result(record)
        "#{record[:query].inspect} candidate #{record[:candidate]} from #{record[:body]} " \
          "at #{record[:coordinates].inspect} (cache_hit=#{record[:marker].inspect})"
      end

      def verify_result(operation, record)
        if operation[:coordinates] && operation[:coordinates] != record[:coordinates]
          raise Violation.new(
            'coordinates_mismatch',
            "candidate #{record[:candidate]} from #{record[:body]} has coordinates " \
            "#{record[:coordinates].inspect}, expected #{operation[:coordinates].inspect}",
            expected: operation[:coordinates], actual: record[:coordinates]
          )
        end
        if operation[:address] && operation[:address] != record[:address]
          raise Violation.new(
            'address_mismatch',
            "candidate #{record[:candidate]} from #{record[:body]} has address " \
            "#{record[:address].inspect}, expected #{operation[:address].inspect}",
            expected: operation[:address], actual: record[:address]
          )
        end
        check_expected(operation, :cache_hit, record[:marker], 'marker_mismatch')
      end

      def check_expected(operation, field, actual, kind)
        expected = operation[field]
        return if expected == :unspecified || expected == actual

        raise Violation.new(
          kind,
          "expected cache_hit=#{expected.inspect} but got #{actual.inspect}",
          expected: expected, actual: actual, evidence: operation[:evidence]
        )
      end

      def delete(step, operation)
        runtime = runtime(step.slot[:process])
        key =
          if operation[:key]
            operation[:key].to_s
          else
            query = runtime.lookup.query_for(operation[:query], operation[:options] || {})
            runtime.cache_key(query)
          end
        before = runtime.cache.store.raw(key)
        runtime.cache.expire(key)
        after = runtime.cache.store.raw(key)
        emit(step, :cache_delete,
             "delete #{key.inspect} (#{before ? 'removed' : 'was absent'})",
             { key: key, removed: !before.nil?, present_after: !after.nil?, process: runtime.id })
      end

      def write(step, operation)
        runtime = runtime(step.slot[:process])
        key =
          if operation[:key]
            operation[:key].to_s
          else
            query = runtime.lookup.query_for(operation[:query], operation[:options] || {})
            runtime.cache_key(query)
          end
        runtime.cache.write(key, operation[:body].raw)
        emit(step, :cache_write, "write #{key.inspect} = #{operation[:body].id}",
             { key: key, body: operation[:body].id, process: runtime.id })
      end

      def violation_record(step, error)
        {
          step: step.label,
          slot: step.slot[:id],
          operation: step.index + 1,
          kind: error.kind,
          message: error.message,
          details: error.details,
          last_successful: @last_successful
        }
      end

      def emit(step, kind, message, data)
        @sequence += 1
        @events << Event.new(sequence: @sequence, step: step.label, kind: kind, message: message, data: data)
      end
    end
  end
end
