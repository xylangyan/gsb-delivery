# frozen_string_literal: true

require 'msgpack'

module Fluent
  module Logger
    module ReceiptAnalysis
      module_function

      # Identifier of a received tuple. Archives may store messages as
      # [tag, time, record] with the original number under "id"/:id.
      def identifier_of(tuple)
        record = tuple[2]
        if record.is_a?(Hash)
          return record['id'].to_s if record.key?('id')
          return record[:id].to_s if record.key?(:id)
        end
        tuple.inspect
      end

      # One native connection. Only whole messages read on this same connection
      # are counted; a recurring original number counts once per full receipt.
      class ConnectionRecord
        attr_reader :id

        def initialize(id)
          @id = id
          @unpacker = MessagePack::Unpacker.new
          @counts = Hash.new(0)
          @bytes_read = 0
        end

        def bytes_read
          @bytes_read
        end

        def accept(bytes)
          return [] if bytes.nil? || bytes.empty?
          @bytes_read += bytes.bytesize
          @unpacker.feed(bytes)
          newly = []
          loop do
            begin
              tuple = @unpacker.read
            rescue EOFError
              break
            end
            break if tuple.nil?
            identifier = ReceiptAnalysis.identifier_of(tuple)
            @counts[identifier] += 1
            newly << identifier
          end
          newly
        end

        def complete_count(identifier)
          @counts[identifier]
        end

        def seen?(identifier)
          @counts[identifier] > 0
        end
      end

      class Checkpoint
        attr_reader :index, :time, :number, :kind, :name

        def initialize(index: nil, time: nil, number: nil, kind: nil, name: nil)
          @index = index
          @time = time
          @number = number
          @kind = kind
          @name = name
        end

        def label
          @name || begin
            parts = []
            parts << "op=#{index}" if index
            parts << "number=#{number}" if number
            parts << "time=#{time}" if time
            parts << "kind=#{kind}" if kind
            parts.join(',')
          end
        end

        def matches?(op_index:, op:, totals:, pending_numbers:)
          return true if @index == op_index
          if @index
            return false
          end
          return false if @kind && @kind != op.kind
          if @number
            return op.post? && op.number == @number
          end
          return false if @time && @time != op.time
          true
        end
      end

      class Requirement
        attr_reader :checkpoint, :number_bounds, :callback_cap_bytes, :pending_cap_bytes,
                    :forbidden_numbers

        def initialize(checkpoint:)
          @checkpoint = checkpoint
          @number_bounds = {}
          @callback_cap_bytes = nil
          @pending_cap_bytes = nil
          @forbidden_numbers = []
        end

        def require_number(number, min: 0, max: nil)
          @number_bounds[number] = { min: min, max: max }
          self
        end

        def callback_cap(bytes)
          @callback_cap_bytes = bytes
          self
        end

        def pending_cap(bytes)
          @pending_cap_bytes = bytes
          self
        end

        def forbid(number)
          @forbidden_numbers << number
          self
        end

        def to_h
          {
            'checkpoint' => checkpoint.label,
            'numbers' => @number_bounds.each_with_object({}) do |(number, bounds), acc|
              acc[number.to_s] = { 'min' => bounds[:min], 'max' => bounds[:max] }
            end,
            'callback_cap' => @callback_cap_bytes,
            'pending_cap' => @pending_cap_bytes,
            'forbidden' => @forbidden_numbers.map(&:to_s)
          }
        end
      end

      # Evaluates one requirement against a checkpoint snapshot.
      class Evaluator
        def initialize(snapshot, requirement, _result = nil)
          @snapshot = snapshot
          @requirement = requirement
        end

        def evaluate
          req = @requirement
          violations = []

          req.number_bounds.each do |number, bounds|
            count = @snapshot.count(number)
            if count < bounds[:min]
              violations << "number #{number}: received #{count}, want >= #{bounds[:min]}"
            end
            if bounds[:max] && count > bounds[:max]
              violations << "number #{number}: received #{count}, want <= #{bounds[:max]}"
            end
          end

          req.forbidden_numbers.each do |number|
            if @snapshot.count(number) > 0
              violations << "number #{number}: must not be received but was"
            end
          end

          if req.callback_cap_bytes && @snapshot.callback_bytes > req.callback_cap_bytes
            violations << "callback bytes #{@snapshot.callback_bytes} > cap #{req.callback_cap_bytes}"
          end

          if req.pending_cap_bytes && @snapshot.pending_bytes > req.pending_cap_bytes
            violations << "pending bytes #{@snapshot.pending_bytes} > cap #{req.pending_cap_bytes}"
          end

          violations
        end
      end
    end
  end
end
