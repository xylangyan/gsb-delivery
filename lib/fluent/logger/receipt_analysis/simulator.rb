# frozen_string_literal: true

module Fluent
  module Logger
    module ReceiptAnalysis
      # A complete receipt observed on a connection at a specific operation.
      Receipt = Struct.new(:number, :connection, :op_index, :time, keyword_init: true) do
        def to_h
          { 'number' => number, 'connection' => connection, 'op' => op_index, 'time' => time }
        end
      end

      # Bytes handed to the overflow callback during one operation.
      CallbackRecord = Struct.new(:op_index, :time, :bytes, :numbers, :connection,
                                  keyword_init: true) do
        def to_h
          { 'op' => op_index, 'time' => time, 'bytes' => bytes,
            'numbers' => numbers, 'connection' => connection }
        end
      end

      # A message still waiting to be sent, holding its exact packed bytes.
      PendingItem = Struct.new(:number, :bytes, :op_index, :time, keyword_init: true) do
        def bytesize
          bytes.bytesize
        end
      end

      # What a checkpoint observed: totals, overflow bytes, pending buffer.
      CheckpointSnapshot = Struct.new(:checkpoint, :op_index, :kind, :time, :totals,
                                      :callback_bytes, :callback_records, :pending_items,
                                      :receipts, keyword_init: true) do
        def count(number)
          totals[number] || 0
        end

        def pending_bytes
          pending_items.sum(&:bytesize)
        end

        def pending_numbers
          pending_items.map(&:number)
        end

        def occurrences(number)
          receipts.select { |receipt| receipt.number == number }
        end

        def callback_occurrences(number)
          callback_records.select { |record| record.numbers.include?(number) }
        end

        def to_h
          {
            'checkpoint' => checkpoint.label,
            'op' => op_index,
            'time' => time,
            'complete_counts' => totals.each_with_object({}) { |(k, v), acc| acc[k] = v unless v.zero? },
            'callback_bytes' => callback_bytes,
            'callback_numbers' => callback_records.flat_map(&:numbers),
            'pending_bytes' => pending_bytes,
            'pending_numbers' => pending_numbers,
            'receipts' => receipts.select { |receipt| totals[receipt.number] > 0 }.map(&:to_h)
          }
        end
      end

      class RunResult
        attr_reader :limit, :operations, :receipts, :callback_records, :checkpoints,
                    :final_pending, :connections, :totals

        def initialize(limit:, operations:, receipts:, callback_records:, checkpoints:,
                       final_pending:, connections:, totals:)
          @limit = limit
          @operations = operations
          @receipts = receipts
          @callback_records = callback_records
          @checkpoints = checkpoints
          @final_pending = final_pending
          @connections = connections
          @totals = totals
        end

        def callback_bytes
          @callback_records.sum(&:bytes)
        end

        def complete_count(number)
          @totals[number] || 0
        end

        def occurrences(number)
          @receipts.select { |receipt| receipt.number == number }
        end

        def callback_occurrences(number)
          @callback_records.select { |record| record.numbers.include?(number) }
        end

        def pending_occurrences(number)
          @final_pending.select { |item| item.number == number }
        end

        def checkpoint_states
          @checkpoints
        end

        def checkpoint_state(checkpoint)
          key = checkpoint.is_a?(Checkpoint) ? checkpoint.label : checkpoint
          @checkpoints[key]
        end

        def to_h
          {
            'limit' => @limit,
            'complete_counts' => @totals.each_with_object({}) { |(k, v), acc| acc[k] = v unless v.zero? },
            'callback_bytes' => callback_bytes,
            'callback_numbers' => @callback_records.flat_map(&:numbers),
            'pending_bytes' => @final_pending.sum(&:bytesize),
            'pending_numbers' => @final_pending.map(&:number),
            'connections' => @connections.map(&:id)
          }
        end
      end

      class ConnectFailure < StandardError; end

      class WriteFailure < StandardError
        attr_reader :accepted

        def initialize(message, accepted)
          super(message)
          @accepted = accepted
        end
      end

      # Deterministic mirror of Fluent::Logger::FluentLogger's send path.
      #
      # The behaviour reproduced here is intentionally the *native* one:
      #   * a post appends to pending and only consults reconnect suppression
      #     while pending <= limit;
      #   * a send first connects (if needed) then writes the entire pending;
      #   * a short/failed write leaves pending intact, so a later send re-sends
      #     the full pending on a fresh connection, duplicating what the
      #     receiver may already have read;
      #   * pending strictly greater than limit is handed to the overflow
      #     callback, including any prefix the receiver may already have read;
      #   * close sends pending without a limit check and hands the whole
      #     pending to the overflow callback on failure.
      class Simulator
        RECONNECT_WAIT = 0.5
        RECONNECT_WAIT_INCR_RATE = 1.5
        RECONNECT_WAIT_MAX = 60
        RECONNECT_WAIT_MAX_COUNT = 13

        attr_reader :limit, :fixture, :identifier_width, :receipts, :callback_records,
                    :connections, :totals

        def initialize(limit:, fixture:, identifier_width: DEFAULT_IDENTIFIER_WIDTH)
          @limit = Integer(limit)
          @fixture = fixture
          @identifier_width = identifier_width
          @pending_items = []
          @history = []
          @con = nil
          @connections = []
          @attempts = 0
          @totals = Hash.new(0)
          @receipts = []
          @callback_records = []
          @current_op = nil
          @current_index = nil
          @write_sizes = []
          @limit_probes = []
          @folded_callback_bytes = nil
          @folded_receipts = nil
        end

        # Every distinct pending byte length this run attempted to write, plus
        # every pending length that was compared directly against the limit.
        # These are the only values at which a `pending <= limit` or
        # `pending > limit` decision can flip, so certification only needs to
        # probe the regions they induce.
        def limit_probes
          @limit_probes
        end

        # For the certified class sizes, collect the pending size that was in
        # flight at each crossing of an integer complete-count threshold. These
        # catch fixtures that branch on "received at least k of class X".
        def write_sizes
          @write_sizes
        end

        def execute(op, op_index)
          @current_op = op
          @current_index = op_index
          case op.kind
          when :post then process_post(op, op_index)
          when :close then process_close(op, op_index)
          when :connect then process_connect(op, op_index)
          when :write then process_write(op, op_index)
          when :disconnect then process_disconnect(op, op_index)
          else raise Error, "Unsupported operation kind: #{op.kind.inspect}"
          end
        end

        def complete_count(number)
          @totals[number]
        end

        def class_complete_count(numbers)
          Array(numbers).sum { |number| @totals[number] }
        end

        # Snapshot of the sender's limit-independent state, used to detect a
        # repeating iteration in a long repeat archive.
        def continuation_state
          [
            @pending_items.map(&:bytesize),
            @con.nil? ? nil : true,
            @history.size,
            @history.empty? ? nil : @history.last
          ]
        end

        # Add the per-cycle effects to the totals for the +extra+ remaining
        # cycles of a long repeat archive. Used only when a continuation state
        # repeats, so every folded cycle is a translation of the measured one.
        def fold_cycles(cycle_counts, cycle_callback_bytes, cycle_receipts, extra)
          cycle_counts.each { |number, count| @totals[number] += count * extra }
          @folded_callback_bytes = folded_callback_bytes + cycle_callback_bytes * extra
          @folded_receipts ||= {}
          cycle_receipts.each { |number, count| @folded_receipts[number] = (@folded_receipts[number] || 0) + count * extra }
        end

        def folded_callback_bytes
          @folded_callback_bytes || 0
        end

        def callback_bytes
          @callback_records.sum(&:bytes) + folded_callback_bytes
        end

        def pending_bytesize
          @pending_items.sum(&:bytesize)
        end

        def pending_items
          @pending_items
        end

        def connection_open?
          !@con.nil?
        end

        def to_h
          {
            'limit' => limit,
            'complete_counts' => @totals.each_with_object({}) { |(k, v), acc| acc[k] = v unless v.zero? },
            'callback_bytes' => callback_bytes,
            'callback_numbers' => @callback_records.flat_map(&:numbers),
            'pending_bytes' => pending_bytesize,
            'pending_numbers' => @pending_items.map(&:number),
            'receipts' => @receipts.map(&:to_h),
            'callback_records' => @callback_records.map(&:to_h),
            'connections' => @connections.map(&:id)
          }
        end

        private

        def snapshot(pending_size:, connection: nil)
          Snapshot.new(
            position: @current_index,
            time: @current_op&.time,
            pending_size: pending_size,
            connection: connection,
            pending_numbers: @pending_items.map(&:number),
            simulator: self
          )
        end

        def suppress_sec
          size = @history.size
          if size < RECONNECT_WAIT_MAX_COUNT
            RECONNECT_WAIT * (RECONNECT_WAIT_INCR_RATE**(size - 1))
          else
            RECONNECT_WAIT_MAX.to_f
          end
        end

        def record_connect_failure(time)
          @history << Integer(time)
          @history.shift if @history.size > RECONNECT_WAIT_MAX_COUNT
        end

        def process_post(op, op_index)
          message = op.resolved_message
          raise Error, "post operation ##{op.number} has no resolved message" unless message
          @pending_items << PendingItem.new(
            number: op.number,
            bytes: message.packed(@identifier_width),
            op_index: op_index,
            time: op.time
          )

          if !@history.empty? && pending_bytesize <= @limit
            @limit_probes << pending_bytesize
            return :suppressed if Integer(op.time) - @history.last < suppress_sec
          end

          begin
            send_pending(op, op_index)
          rescue ConnectFailure, WriteFailure
            @limit_probes << pending_bytesize
            overflow(op_index) if pending_bytesize > @limit
            close_connection
          end
        end

        def process_close(op, op_index)
          unless @pending_items.empty?
            begin
              send_pending(op, op_index)
            rescue ConnectFailure, WriteFailure
              overflow(op_index)
            end
          end
          close_connection
          @pending_items = []
        end

        def process_connect(op, _op_index)
          attempt = (@attempts += 1)
          if @fixture.connect(snapshot(pending_size: pending_bytesize, connection: attempt))
            open_connection(attempt)
            @history.clear
          else
            record_connect_failure(op.time)
          end
        end

        def process_write(op, op_index)
          unless connection_open?
            process_connect(Operation.connect(op.time, @attempts + 1), op_index)
            return unless connection_open?
          end
          outcome = @fixture.write(snapshot(pending_size: pending_bytesize, connection: @con.id),
                                   pending_bytes)
          accept_bytes(op, op_index, outcome.accepted_bytes.to_i)
        end

        def process_disconnect(_op, _op_index)
          close_connection
        end

        # Mirrors send_data: connect if needed, then write the whole pending.
        def send_pending(op, op_index)
          unless connection_open?
            attempt = (@attempts += 1)
            if @fixture.connect(snapshot(pending_size: pending_bytesize, connection: attempt))
              open_connection(attempt)
              @history.clear
            else
              record_connect_failure(op.time)
              raise ConnectFailure, "connect failed at t=#{op.time}"
            end
          end

          data = pending_bytes
          @write_sizes << data.bytesize
          outcome = @fixture.write(snapshot(pending_size: data.bytesize, connection: @con.id), data)
          accepted = outcome.accepted_bytes.to_i
          accept_bytes(op, op_index, accepted)

          if outcome.complete? || accepted >= data.bytesize
            @pending_items = []
            true
          else
            raise WriteFailure.new("short write (#{accepted}/#{data.bytesize})", accepted)
          end
        end

        def pending_bytes
          @pending_items.map(&:bytes).join
        end

        def accept_bytes(op, op_index, accepted)
          return [] if accepted.nil? || accepted <= 0
          data = pending_bytes
          accepted = [accepted, data.bytesize].min
          slice = data.byteslice(0, accepted)
          newly = @con.accept(slice)
          newly.each do |number|
            @totals[number] += 1
            @receipts << Receipt.new(number: number, connection: @con.id,
                                     op_index: op_index, time: op.time)
          end
          newly
        end

        def overflow(op_index)
          data = pending_bytes
          return if data.empty?
          numbers = Packing.complete_messages(data).map { |tuple| ReceiptAnalysis.identifier_of(tuple) }
          @callback_records << CallbackRecord.new(
            op_index: op_index, time: @current_op&.time, bytes: data.bytesize,
            numbers: numbers, connection: @con&.id
          )
          @pending_items = []
        end

        def open_connection(attempt)
          @con = ConnectionRecord.new(attempt)
          @connections << @con
        end

        def close_connection
          @con = nil
        end
      end
    end
  end
end
