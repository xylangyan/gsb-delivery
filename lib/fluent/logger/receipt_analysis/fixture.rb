# frozen_string_literal: true

require 'msgpack'

module Fluent
  module Logger
    module ReceiptAnalysis
      # Snapshot handed to a fixture rule so it can branch on the three
      # documented conditions: operation position, current pending length and
      # whether a named class has crossed a complete-receipt threshold.
      class Snapshot
        attr_reader :position, :time, :pending_size, :connection, :pending_numbers, :simulator

        def initialize(position:, time:, pending_size:, connection:, pending_numbers:, simulator:)
          @position = position
          @time = time
          @pending_size = pending_size
          @connection = connection
          @pending_numbers = pending_numbers
          @simulator = simulator
        end

        def complete_count(number)
          simulator.complete_count(number)
        end

        def class_complete_count(*numbers)
          simulator.class_complete_count(numbers)
        end

        def at_or_above?(threshold, *numbers)
          class_complete_count(*numbers) >= threshold
        end

        def below?(threshold, *numbers)
          class_complete_count(*numbers) < threshold
        end
      end

      # Base fixture. A rule answers two questions: may a connection be opened
      # at this point, and what happens to a write of +bytes+ pending bytes.
      module Fixture
        # Default fixture: every connection succeeds and every write accepts
        # all bytes.
        class AlwaysComplete
          def connect(_snapshot)
            true
          end

          def write(_snapshot, _bytes)
            { outcome: :complete }
          end

          def disconnect(_snapshot)
            { outcome: :drop }
          end
        end

        Outcome = Struct.new(:outcome, :accepted_bytes, keyword_init: true) do
          def complete?
            outcome == :complete
          end

          def partial?
            outcome == :partial
          end

          def failed?
            %i[partial epipe eagain].include?(outcome)
          end
        end

        # A declarative rule table: an array of entries, each with an optional
        # condition and an action. Entries are tried in order; the first one
        # whose condition matches wins. Conditions branch on the documented
        # variables:
        #   position:     operation position (1-based write/connect attempt)
        #   pending_size: current pending byte length
        #   at_least:     [threshold, class_numbers] complete-count threshold
        #   below:        [threshold, class_numbers] complete-count threshold
        #   time:         second-resolution wall clock
        class RuleTable
          Entry = Struct.new(:condition, :action, keyword_init: true)

          def initialize(connect_entries: [], write_entries: [], disconnect_entries: [])
            @connect_entries = connect_entries
            @write_entries = write_entries
            @disconnect_entries = disconnect_entries
          end

          def connect(snapshot)
            resolve(@connect_entries, snapshot, default: true) do |action|
              case action
              when :complete, :ok, true then true
              when :fail, :epipe, :refuse, false then false
              else action
              end
            end
          end

          def write(snapshot, bytes)
            action = resolve(@write_entries, snapshot, default: { outcome: :complete }) { |a| a }
            normalize_write(action, snapshot, bytes)
          end

          def disconnect(snapshot)
            action = resolve(@disconnect_entries, snapshot, default: { outcome: :drop }) { |a| a }
            normalize_write(action, snapshot, nil)
          end

          private

          def resolve(entries, snapshot, default:)
            entries.each do |entry|
              next unless condition_met?(entry.condition, snapshot)
              return yield(entry.action)
            end
            default
          end

          def condition_met?(condition, snapshot)
            return true if condition.nil?
            if (value = condition[:position])
              return false unless Array(value).include?(snapshot.position)
            end
            if (value = condition[:time])
              return false unless Array(value).include?(snapshot.time)
            end
            if (value = condition[:pending_size])
              return false unless Array(value).include?(snapshot.pending_size)
            end
            if (value = condition[:pending_size_at_least])
              return false unless snapshot.pending_size >= value
            end
            if (value = condition[:pending_size_below])
              return false unless snapshot.pending_size < value
            end
            if (value = condition[:at_least])
              threshold, numbers = value
              return false unless snapshot.class_complete_count(*Array(numbers)) >= threshold
            end
            if (value = condition[:below])
              threshold, numbers = value
              return false unless snapshot.class_complete_count(*Array(numbers)) < threshold
            end
            if (value = condition[:connection])
              return false unless Array(value).include?(snapshot.connection)
            end
            true
          end

          def normalize_write(action, snapshot, bytes)
            case action
            when :complete, true
              Outcome.new(outcome: :complete, accepted_bytes: bytes&.bytesize)
            when :fail, false
              Outcome.new(outcome: :epipe, accepted_bytes: 0)
            when :eagain
              Outcome.new(outcome: :eagain, accepted_bytes: 0)
            when Integer
              Outcome.new(outcome: :partial, accepted_bytes: action)
            when Hash
              normalize_hash(action, bytes)
            else
              raise Error, "Unsupported fixture write action: #{action.inspect}"
            end
          end

          def normalize_hash(action, bytes)
            outcome = (action[:outcome] || action['outcome'] || :complete).to_sym
            case outcome
            when :complete
              Outcome.new(outcome: :complete, accepted_bytes: bytes&.bytesize)
            when :partial
              Outcome.new(outcome: :partial, accepted_bytes: resolve_bytes(action, bytes))
            when :epipe
              Outcome.new(outcome: :epipe, accepted_bytes: 0)
            when :eagain
              Outcome.new(outcome: :eagain, accepted_bytes: 0)
            else
              raise Error, "Unsupported fixture write outcome: #{outcome.inspect}"
            end
          end

          def resolve_bytes(action, bytes)
            raw = action.key?(:bytes) ? action[:bytes] : action['bytes']
            total = bytes ? bytes.bytesize : 0
            case raw
            when nil then 0
            when Integer then raw
            when :all, 'all', 'pending' then total
            when :none, 'none' then 0
            when String
              relative = raw.match(/^(?:pending|all)(?:-(\d+))?$/)
              if relative
                return total - relative[1].to_i
              end
              raise Error, "Unsupported fixture byte expression: #{raw.inspect}"
            else
              Integer(raw)
            end
          end
        end

        # Fixture backed by a callable. +connect_proc+ receives a Snapshot and
        # returns true/false. +write_proc+ receives a Snapshot and the pending
        # bytes string and returns a Hash / Integer / Symbol understood above.
        class Callable
          def initialize(connect_proc: nil, write_proc: nil, disconnect_proc: nil)
            @connect_proc = connect_proc
            @write_proc = write_proc
            @disconnect_proc = disconnect_proc
            @table = RuleTable.new
          end

          def connect(snapshot)
            return @table.connect(snapshot) unless @connect_proc
            result = @connect_proc.call(snapshot)
            result.nil? ? true : result
          end

          def write(snapshot, bytes)
            return @table.write(snapshot, bytes) unless @write_proc
            normalize(@write_proc.call(snapshot, bytes), bytes)
          end

          def disconnect(snapshot)
            return @table.disconnect(snapshot) unless @disconnect_proc
            normalize(@disconnect_proc.call(snapshot), nil)
          end

          private

          def normalize(result, bytes)
            case result
            when Outcome then result
            when :complete, true
              Outcome.new(outcome: :complete, accepted_bytes: bytes&.bytesize)
            when :epipe, :fail, false
              Outcome.new(outcome: :epipe, accepted_bytes: 0)
            when :eagain
              Outcome.new(outcome: :eagain, accepted_bytes: 0)
            when Integer
              Outcome.new(outcome: :partial, accepted_bytes: result)
            when Hash
              out = (result[:outcome] || result['outcome'] || :complete).to_sym
              accepted = if out == :complete
                           bytes&.bytesize
                         else
                           Integer(result[:bytes] || result['bytes'] || 0)
                         end
              Outcome.new(outcome: out, accepted_bytes: accepted)
            else
              raise Error, "Unsupported fixture result: #{result.inspect}"
            end
          end
        end
      end
    end
  end
end
