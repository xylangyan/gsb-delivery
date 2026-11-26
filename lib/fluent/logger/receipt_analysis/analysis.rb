# frozen_string_literal: true

module Fluent
  module Logger
    module ReceiptAnalysis
      # Runs a parsed archive against the native sender for one or many
      # buffer_limit values, captures the certificate checkpoints, and reports
      # which limits satisfy every checkpoint.
      class Analysis
        # Number of repeat-template iterations simulated before a cycle is
        # trusted. Enough for reconnect-suppression windows and class-count
        # fixtures to settle.
        REPEAT_LEAD_IN = 64

        attr_reader :archive

        def initialize(archive)
          @archive = archive
        end

        def operations
          @operations ||= @archive.operation_stream
        end

        def checkpoints
          @archive.checkpoints
        end

        def requirements
          @archive.requirements
        end

        # Simulate one limit and capture the state after every checkpointed
        # boundary. A checkpoint latches on the first operation boundary it
        # matches (by index, number, kind or time).
        #
        # Repeat archives run their prefix, a lead-in of iterations, then fold
        # the long middle once the continuation state repeats, then the suffix.
        def run(limit)
          simulator = Simulator.new(limit: limit, fixture: @archive.fixture,
                                    identifier_width: @archive.identifier_width)
          captured = {}
          plan = @archive.repeat_plan
          if plan
            execute_repeat(simulator, plan, captured)
          else
            execute_stream(simulator, operations, captured, offset: 0)
          end
          RunResult.new(
            limit: limit,
            operations: operations,
            receipts: simulator.receipts,
            callback_records: simulator.callback_records,
            checkpoints: captured,
            final_pending: simulator.pending_items,
            connections: simulator.connections,
            totals: simulator.totals
          )
        end

        def result(limit)
          run(limit)
        end

        def violations(limit)
          result = run(limit)
          evaluate_requirements(result, requirements)
        end

        def passes?(limit)
          violations(limit).values.flatten.empty?
        end

        # Merge the limits that pass all checkpoints into contiguous integer
        # intervals, evaluating an explicit shortlist.
        def certify(limits)
          passing = []
          limits.each { |limit| passing << limit if passes?(limit) }
          Certification.new(self, group_consecutive(passing), limits)
        end

        # Probe a whole integer range without visiting every value. Sender
        # behaviour can only change at:
        #   * each attempted write length (`pending <= limit` / `> limit`),
        #   * a certificate byte cap (overflow / pending caps),
        #   * a fixture complete-count threshold.
        # Several pilot limits bound the set of attempted write lengths; one
        # representative of each induced region is certified and adjacent
        # passing regions are merged.
        def certify_intervals(range, pilot_limits: nil, thresholds: nil)
          boundaries = breakpoint_candidates(range, pilot_limits, thresholds)
          candidates = representative_limits(range, boundaries)
          passing = candidates.select { |limit| passes?(limit) }
          intervals = []
          candidates.each_with_index do |limit, index|
            next unless passing.include?(limit)
            region_start = index.zero? ? range.begin : candidates[index - 1] + 1
            region_end = index == candidates.size - 1 ? range.end : limit
            intervals << (region_start..region_end)
          end
          Certification.new(self, merge_intervals(intervals), range)
        end

        def evaluate_requirements(result, reqs = requirements)
          reqs.each_with_object({}) do |req, acc|
            state = result.checkpoint_state(req.checkpoint.label)
            if state.nil?
              acc[req.checkpoint.label] = ["checkpoint #{req.checkpoint.label} not reached"]
              next
            end
            acc[req.checkpoint.label] = Evaluator.new(state, req, result).evaluate
          end
        end

        private

        # --- streaming -----------------------------------------------------

        def execute_stream(simulator, stream, captured, offset:)
          pending = checkpoints.compact
          stream.each_with_index do |op, index|
            op_index = index + 1 + offset
            simulator.execute(op, op_index)
            capture(captured, pending, simulator, op, op_index)
            pending = pending.reject { |checkpoint| captured.key?(checkpoint.label) }
            break if pending.empty?
          end
        end

        def execute_repeat(simulator, plan, captured)
          pending = checkpoints.compact
          base = plan[:prefix].length
          plan[:prefix].each_with_index do |op, i|
            simulator.execute(op, i + 1)
            capture(captured, pending, simulator, op, i + 1)
            pending = pending.reject { |checkpoint| captured.key?(checkpoint.label) }
          end

          template = plan[:repeat]['template']
          per_iteration = template.length
          count = plan[:repeat]['count']
          lead = [count, REPEAT_LEAD_IN].min
          expander = build_expander(plan)

          seen = {}
          cycle_start = nil
          cycle_length = nil
          iteration = 0
          iteration_ops = []
          # Pull exactly the lead-in operations from the expander; never
          # materialise the whole (possibly billion-operation) repeat.
          while iteration < lead
            state = simulator.continuation_state
            key = simulator.pending_items.empty? ? state_key(state) : nil
            if key && seen.key?(key)
              cycle_start = seen[key]
              cycle_length = iteration - cycle_start
              break
            end
            seen[key] = iteration if key
            iteration_ops.clear
            per_iteration.times do
              iteration_ops << expander.next
            end
            iteration_ops.each_with_index do |op, j|
              op_index = base + iteration * per_iteration + j + 1
              resolved = resolve_template_op(op)
              simulator.execute(resolved, op_index)
              capture(captured, pending, simulator, resolved, op_index)
              pending = pending.reject { |checkpoint| captured.key?(checkpoint.label) }
            end
            iteration += 1
          end

          if cycle_start && cycle_length && count > iteration
            fold_remaining(simulator, per_iteration, plan, cycle_start, cycle_length, iteration, count)
          elsif count > iteration
            while iteration < count
              iteration_ops.clear
              per_iteration.times { iteration_ops << expander.next }
              iteration_ops.each_with_index do |op, j|
                op_index = base + iteration * per_iteration + j + 1
                resolved = resolve_template_op(op)
                simulator.execute(resolved, op_index)
                capture(captured, pending, simulator, resolved, op_index)
                pending = pending.reject { |checkpoint| captured.key?(checkpoint.label) }
              end
              iteration += 1
            end
          end

          suffix_offset = base + count * per_iteration
          plan[:suffix].each_with_index do |op, i|
            simulator.execute(op, suffix_offset + i + 1)
            capture(captured, pending, simulator, op, suffix_offset + i + 1)
            pending = pending.reject { |checkpoint| captured.key?(checkpoint.label) }
          end
        end

        def run_stream_for_pilot(simulator)
          plan = @archive.repeat_plan
          if plan
            plan[:prefix].each { |op| simulator.execute(op, 1) }
            expander = build_expander(plan)
            per_iteration = plan[:repeat]['template'].length
            limit_iterations = [plan[:repeat]['count'], REPEAT_LEAD_IN].min
            limit_iterations.times do
              per_iteration.times { simulator.execute(resolve_template_op(expander.next), 1) }
            end
            plan[:suffix].each { |op| simulator.execute(op, 1) }
          else
            operations.each_with_index { |op, index| simulator.execute(op, index + 1) }
          end
        end

        # Builds the expander lazily and pulls iteration operations in order.
        def build_expander(plan)
          spec = plan[:repeat]
          RepeatExpander.new(
            template: spec['template'], count: spec['count'], stride: spec['stride'],
            start_time: spec['start_time'], first_number: spec['first_number'],
            identifier_width: @archive.identifier_width
          ).to_enum
        end

        def resolve_template_op(op)
          return op unless op.post?
          return op if op.resolved_message
          number = op.number
          message = @archive.messages[number] || @archive.bootstrap_message(number)
          Operation.post(op.time, number, message: message)
        end

        # Measure one stable cycle's counts/callbacks and multiply them over the
        # remaining iterations. The scratch simulator replays only
        # prefix + cycle_start + cycle_length iterations, so this is independent
        # of the total repeat count.
        def fold_remaining(simulator, per_iteration, plan, cycle_start, cycle_length, played, total_count)
          extra = (total_count - played) / cycle_length
          return if extra <= 0
          scratch = Simulator.new(limit: simulator.limit, fixture: @archive.fixture,
                                  identifier_width: @archive.identifier_width)
          scratch_ops = build_expander(plan)
          plan[:prefix].each { |op| scratch.execute(op, 1) }
          cycle_start.times do
            per_iteration.times { scratch.execute(resolve_template_op(scratch_ops.next), 1) }
          end
          counts_before = scratch.totals.dup
          callbacks_before = scratch.callback_bytes
          receipts_before = scratch.receipts.size
          cycle_length.times do
            per_iteration.times { scratch.execute(resolve_template_op(scratch_ops.next), 1) }
          end
          cycle_counts = diff_counts(counts_before, scratch.totals)
          cycle_callback = scratch.callback_bytes - callbacks_before
          cycle_receipts = diff_receipts(scratch.receipts[receipts_before..] || [])
          simulator.fold_cycles(cycle_counts, cycle_callback, cycle_receipts, extra)
        end

        def diff_counts(before, after)
          result = {}
          after.each { |number, value| result[number] = value - (before[number] || 0) if value != (before[number] || 0) }
          result
        end

        def diff_receipts(receipts)
          receipts.each_with_object(Hash.new(0)) { |receipt, acc| acc[receipt.number] += 1 }
        end

        def state_key(state)
          state
        end

        def capture(captured, pending, simulator, op, op_index)
          return if pending.empty?
          pending.each do |checkpoint|
            next unless checkpoint_boundary?(checkpoint, op, op_index)
            captured[checkpoint.label] ||= snapshot_of(simulator, checkpoint, op, op_index)
          end
        end

        def checkpoint_boundary?(checkpoint, op, op_index)
          return true if checkpoint.index == op_index
          return false if checkpoint.index
          return false if checkpoint.kind && checkpoint.kind != op.kind
          if checkpoint.number
            return op.post? && op.number == checkpoint.number
          end
          return false if checkpoint.time && checkpoint.time != op.time
          true
        end

        def snapshot_of(simulator, checkpoint, op, op_index)
          CheckpointSnapshot.new(
            checkpoint: checkpoint,
            op_index: op_index,
            kind: op.kind,
            time: op.time,
            totals: simulator.totals.dup,
            callback_bytes: simulator.callback_bytes,
            callback_records: simulator.callback_records.dup,
            pending_items: simulator.pending_items.dup,
            receipts: simulator.receipts.dup
          )
        end

        # --- certification ------------------------------------------------

        def breakpoint_candidates(range, pilot_limits, thresholds = nil)
          seeds = pilot_limits || pilot_seed_limits(range)
          totals = []
          seeds.each do |limit|
            simulator = Simulator.new(limit: limit, fixture: @archive.fixture,
                                      identifier_width: @archive.identifier_width)
            run_stream_for_pilot(simulator)
            totals.concat(simulator.write_sizes)
            totals.concat(simulator.limit_probes)
            totals << simulator.pending_bytesize
          end

          boundaries = Set.new
          totals.uniq.each do |total|
            boundaries << total - 1
            boundaries << total
          end
          certificate_boundaries(boundaries)
          Array(thresholds).each { |count| boundaries << count - 1; boundaries << count } if thresholds
          boundaries.select { |value| range.cover?(value) }.sort
        end

        def pilot_seed_limits(range)
          span = range.end - range.begin
          seeds = (0..4).map { |i| range.begin + (span * i) / 4 }
          seeds << range.begin << range.end
          seeds.uniq.select { |value| range.cover?(value) }
        end

        def certificate_boundaries(boundaries)
          requirements.each do |req|
            if (cap = req.callback_cap_bytes)
              boundaries << cap - 1
              boundaries << cap
            end
            if (cap = req.pending_cap_bytes)
              boundaries << cap - 1
              boundaries << cap
            end
            req.number_bounds.each_value do |bounds|
              [bounds[:min], bounds[:max]].compact.each do |count|
                boundaries << count - 1
                boundaries << count
              end
            end
          end
        end

        def representative_limits(range, boundaries)
          points = [range.begin]
          boundaries.each do |boundary|
            points << boundary
            points << boundary + 1 if range.cover?(boundary + 1)
          end
          points << range.end
          points.select { |value| range.cover?(value) }.uniq.sort
        end

        def merge_intervals(intervals)
          intervals.sort_by(&:begin).each_with_object([]) do |interval, acc|
            if acc.empty? || interval.begin > acc.last.end + 1
              acc << interval.dup
            else
              acc[-1] = (acc.last.begin..interval.end)
            end
          end
        end

        def group_consecutive(values)
          values.sort.each_with_object([]) do |value, acc|
            if acc.empty? || value != acc.last.end + 1
              acc << (value..value)
            else
              acc[-1] = (acc.last.begin..value)
            end
          end
        end
      end

      class Certification
        attr_reader :analysis, :intervals, :scope

        def initialize(analysis, intervals, scope)
          @analysis = analysis
          @intervals = intervals
          @scope = scope
        end

        def pass?(limit)
          @intervals.any? { |interval| interval.cover?(limit) }
        end

        def empty?
          @intervals.empty?
        end

        def to_a
          @intervals
        end

        def to_h
          { 'limits' => @intervals.map { |interval| [interval.begin, interval.end] } }
        end
      end
    end
  end
end
