# frozen_string_literal: true

class JsonPath
  # Plans a compact sequence of delete calls for an ordered cleanup list.
  #
  # Adjacent delete operations that address entries of the same array may share
  # one bracket, for example "$.items[2]" followed by "$.items[0]" becomes
  # "$.items[2,0]". A merged bracket is only used when it produces the same
  # document at every marked handoff as running the original operations one by
  # one. The plan also records the original slot of every surviving value, so
  # equal values that came from different array positions are not confused.
  #
  # Merged delete calls still use JsonPath's native cleanup semantics. That
  # means the cleanup runs once per call, not once per comma item. This is why
  # a merge that looks harmless may be rejected: the native call can see an
  # empty object that a preceding single delete would already have removed.
  class DeletePlan
    INTEGER_SELECTOR = /\A-?\d+\z/.freeze
    RANGE_SELECTOR = /\A-?\d*:-?\d*(?::-?\d+)?\z/.freeze

    class Error < StandardError; end

    class Call
      attr_reader :path, :operation_indexes

      def initialize(path, operation_indexes)
        @path = path
        @operation_indexes = operation_indexes.freeze
      end

      def merged?
        @operation_indexes.length > 1
      end

      def to_s
        @path
      end

      def inspect
        "#<#{self.class} path=#{@path.inspect} operations=#{@operation_indexes.inspect}>"
      end
    end

    class Rejection
      attr_reader :document, :operations, :segment_start, :segment_operations,
                  :segment_document, :calls, :attempted_calls, :merged_path,
                  :operation_indexes, :expected_json, :actual_json,
                  :expected_slots, :actual_slots, :first_difference

      def initialize(attrs)
        attrs.each { |key, value| instance_variable_set("@#{key}", value) }
      end

      # A self-contained reproduction from the full input: apply
      # +operations[0...segment_start]+ to +document+ to reach
      # +segment_document+, then apply +attempted_calls+ to compare
      # +actual_json+ with +expected_json+. +first_difference+ points at the
      # first differing JSON value or original slot.
      def replay
        {
          :document => Slots.deep_copy(@document),
          :operations => @operations.dup,
          :segment_start => @segment_start,
          :segment_operations => @segment_operations.dup,
          :segment_document => Slots.deep_copy(@segment_document),
          :calls => @calls.dup,
          :attempted_calls => @attempted_calls.dup,
          :merged_path => @merged_path,
          :operation_indexes => @operation_indexes.dup,
          :expected_json => Slots.deep_copy(@expected_json),
          :actual_json => Slots.deep_copy(@actual_json),
          :expected_slots => Slots.deep_copy(@expected_slots),
          :actual_slots => Slots.deep_copy(@actual_slots),
          :first_difference => Slots.deep_copy(@first_difference)
        }
      end

      def inspect
        "#<#{self.class} #{@merged_path.inspect} first_difference=#{@first_difference.inspect}>"
      end
    end

    class << self
      def build(document, operations, handoffs: nil, check_final: true, max_candidates: 4096)
        new(document, operations,
            :handoffs => handoffs,
            :check_final => check_final,
            :max_candidates => max_candidates)
      end

      alias combine build

      def decode_document(document)
        document.is_a?(String) ? MultiJson.decode(document) : document
      end
    end

    attr_reader :document, :operations, :handoffs, :steps, :rejections,
                :segment_boundaries

    def initialize(document, operations, handoffs: nil, check_final: true, max_candidates: 4096)
      @document = DeletePlan.decode_document(document)
      @document = Slots.deep_copy(@document)
      @operations = operations.map(&:to_s).freeze
      @check_final = check_final
      @max_candidates = Integer(max_candidates)
      raise ArgumentError, 'max_candidates must be positive' unless @max_candidates.positive?
      @handoffs = normalize_handoffs(handoffs)
      @segment_boundaries = build_segment_boundaries
      @parsed_operations = @operations.map { |path| parse_operation(path) }
      @boundary_states = trace_sequential_states
      @rejections = []
      optimize!
      validate_plan!
    end

    def calls
      @steps.map(&:to_s)
    end

    def execute(document = @document, handoffs = self.handoffs)
      apply(document, handoffs)
    end

    def call_count
      @steps.size
    end

    def operation_count
      @operations.size
    end

    def merged_call_count
      @steps.count(&:merged?)
    end

    def handoff_calls
      boundary_counts = { 0 => 0 }
      cumulative = 0
      @steps.each_with_index do |step, index|
        cumulative += step.operation_indexes.length
        boundary_counts[cumulative] = index + 1
      end
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = boundary_counts.fetch(handoff, @steps.size)
      end
    end

    # The document visible at each handoff after running this plan.
    def snapshots
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.deep_copy(@planned_plain_states[handoff])
      end
    end

    # The document produced by the original one-operation-at-a-time list.
    def expected_snapshots
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.deep_copy(@boundary_states[handoff][:plain])
      end
    end

    # Original slots are stable strings such as "$.items[3]". They point at the
    # slot where each surviving value started, not where it currently sits.
    def handoff_slots
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.deep_copy(Slots.slots(@planned_shadow_states[handoff]))
      end
    end

    def expected_handoff_slots
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.deep_copy(Slots.slots(@boundary_states[handoff][:shadow]))
      end
    end

    # A compact form of handoff_slots: nested arrays and hashes whose leaves
    # are the original slot strings, for example "$.items[3]".
    def handoff_origins
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.origins(@planned_shadow_states[handoff])
      end
    end

    def expected_handoff_origins
      @handoffs.each_with_object({}) do |handoff, memo|
        memo[handoff] = Slots.origins(@boundary_states[handoff][:shadow])
      end
    end

    def safe?
      @handoffs.all? do |handoff|
        @planned_plain_states[handoff] == @boundary_states[handoff][:plain] &&
          Slots.slots(@planned_shadow_states[handoff]) == Slots.slots(@boundary_states[handoff][:shadow])
      end
    end

    def handoff_snapshots
      snapshots
    end

    def report
      {
        :calls => calls,
        :call_count => call_count,
        :operation_count => operation_count,
        :merged_call_count => merged_call_count,
        :handoffs => @handoffs.dup,
        :handoff_calls => handoff_calls,
        :snapshots => snapshots,
        :expected_snapshots => expected_snapshots,
        :handoff_slots => handoff_slots,
        :expected_handoff_slots => expected_handoff_slots,
        :handoff_origins => handoff_origins,
        :expected_handoff_origins => expected_handoff_origins,
        :safe => safe?,
        :rejections => @rejections.map(&:replay)
      }
    end

    def apply(document = @document, handoffs = self.handoffs)
      copy = Slots.deep_copy(DeletePlan.decode_document(document))
      states = {}
      states[0] = Slots.deep_copy(copy) if handoffs.include?(0)
      executed = 0
      @steps.each do |step|
        copy = JsonPath.for(copy).delete(step.to_s).obj
        executed += step.operation_indexes.length
        states[executed] = Slots.deep_copy(copy) if handoffs.include?(executed)
      end
      [copy, states]
    end

    def to_s
      calls.inspect
    end

    def inspect
      "#<#{self.class} calls=#{calls.inspect} handoffs=#{@handoffs.inspect} safe=#{safe?}>"
    end

    private

    def normalize_handoffs(handoffs)
      values = handoffs.nil? ? [@operations.size] : Array(handoffs)
      values = values.map { |handoff| Integer(handoff) }.uniq.sort
      if values.empty?
        values = [@operations.size]
      end
      values.each do |handoff|
        next if handoff >= 0 && handoff <= @operations.size
        raise ArgumentError, "handoff #{handoff} is outside 0..#{@operations.size}"
      end
      values << @operations.size if @check_final && values.last != @operations.size
      values.uniq.sort.freeze
    end

    def build_segment_boundaries
      boundaries = [0]
      @handoffs.each { |handoff| boundaries << handoff if handoff > boundaries.last }
      boundaries << @operations.size if boundaries.last < @operations.size
      boundaries.freeze
    end

    def parse_operation(path)
      tokens = JsonPath.new(path).path
      last = tokens.last
      match = last && last.match(/\A\[(.*)\]\z/m)
      return nil unless match

      selector = match[1]
      return nil if selector.start_with?("'", '"', '?', '*')
      items = selector_items(selector)
      return nil unless items

      {
        :path => path,
        :prefix_tokens => tokens[0...-1],
        :prefix => path_prefix(path),
        :items => items
      }
    rescue ArgumentError
      nil
    end

    def selector_items(selector)
      items = selector.split(',').map(&:strip)
      return nil if items.empty?
      return nil unless items.all? { |item| selection_item?(item) }
      items
    end

    def selection_item?(item)
      item.match?(INTEGER_SELECTOR) || item.match?(RANGE_SELECTOR)
    end

    def path_prefix(path)
      index = final_bracket_start(path)
      index ? path[0...index] : nil
    end

    def final_bracket_start(path)
      return nil unless path.end_with?(']')

      depth = 0
      quote = nil
      escaped = false
      path.length.times.reverse_each do |index|
        char = path[index]
        if quote
          if escaped
            escaped = false
          elsif char == '\\'
            escaped = true
          elsif char == quote
            quote = nil
          end
          next
        end
        case char
        when "'", '"'
          quote = char
        when ']'
          depth += 1
        when '['
          depth -= 1
          return index if depth.zero?
        end
      end
      nil
    end

    # Tracks the original list once, mutating a shadow copy and a plain copy.
    # Only handoff states are snapshotted, so a long list does not keep a full
    # document for every operation.
    def trace_sequential_states
      shadow = Slots.tag(Slots.deep_copy(@document), '$')
      plain = Slots.untag(shadow)
      @operations.each do |path|
        begin
          JsonPath.new(path)
        rescue StandardError => error
          raise Error, "invalid delete path #{path.inspect}: #{error.message}"
        end
      end
      states = { 0 => { :shadow => Slots.deep_copy(shadow), :plain => Slots.deep_copy(plain) } }
      @operations.each_with_index do |path, index|
        shadow = JsonPath.for(shadow).delete!(path).obj
        plain = JsonPath.for(plain).delete!(path).obj
        if Slots.untag(shadow) != plain
          raise Error, "slot tracker diverged from native delete at operation #{index + 1}"
        end
        if @segment_boundaries.include?(index + 1)
          states[index + 1] = {
            :shadow => Slots.deep_copy(shadow),
            :plain => Slots.deep_copy(plain)
          }
        end
      end
      states.freeze
    end

    def optimize!
      @steps = []
      @planned_shadow_states = {}
      @planned_plain_states = {}

      if @segment_boundaries.include?(0)
        @planned_shadow_states[0] = Slots.deep_copy(@boundary_states[0][:shadow])
        @planned_plain_states[0] = Slots.deep_copy(@boundary_states[0][:plain])
      end

      @segment_boundaries.each_cons(2) do |start_index, end_index|
        next if end_index <= start_index

        calls, end_shadow = optimize_segment(start_index, end_index, @boundary_states[start_index])
        @steps.concat(calls)
        @planned_shadow_states[end_index] = end_shadow
        @planned_plain_states[end_index] = Slots.untag(end_shadow)
      end
    end

    def optimize_segment(start_index, end_index, start_state)
      operations = @parsed_operations[start_index...end_index]
      local = operations.each_with_index.map do |operation, index|
        operation && operation.merge(:local_index => index)
      end
      runs = merge_runs(local)
      singles = singleton_calls(start_index, end_index)
      target = @boundary_states[end_index]
      minimum_groups = local.size - runs.sum { |run| run[:finish] - run[:start] + 1 } + runs.size
      candidate_count = 0

      accepted = nil
      begin
        (minimum_groups..local.size).each do |group_count|
          try_group_count(local, runs, group_count, start_index) do |calls|
            candidate_count += 1
            raise TooManyCandidates if candidate_count > @max_candidates
            result = simulate(@boundary_states[start_index][:shadow], calls)
            next unless result
            if segment_matches?(result, target)
              accepted = [calls, result]
              raise CandidateFound
            end
          end
        end
      rescue CandidateFound
        record_run_rejections(start_index, end_index, local, runs, target, accepted.first)
        return accepted
      rescue TooManyCandidates
        return heuristic_partition(start_index, end_index, local, runs, singles, target)
      end

      heuristic_partition(start_index, end_index, local, runs, singles, target)
    end

    class TooManyCandidates < Error; end

    class CandidateFound < StandardError; end

    def try_group_count(local, runs, group_count, start_index, &block)
      minimum = local.size - runs.sum { |run| run[:finish] - run[:start] + 1 } + runs.size
      extra = group_count - minimum
      each_distribution(runs, extra) do |distribution|
        each_partition(local, runs, distribution, start_index, &block)
      end
    end

    def each_distribution(runs, extra, &block)
      return yield [] if runs.empty? && extra.zero?
      return if runs.empty?

      counts = Array.new(runs.size, 0)
      rec = lambda do |index, remaining|
        if index == runs.size - 1
          counts[index] = remaining
          yield counts.dup if remaining >= 0 && remaining <= runs[index][:finish] - runs[index][:start]
          return
        end
        (0..[remaining, runs[index][:finish] - runs[index][:start]].min).each do |value|
          counts[index] = value
          rec.call(index + 1, remaining - value)
        end
      end
      rec.call(0, extra)
    end

    def each_partition(local, runs, distribution, start_index, &block)
      walk_partitions(local, runs, distribution, 0, [], start_index, &block)
    end

    def walk_partitions(local, runs, distribution, index, chosen, start_index, &block)
      if index == runs.size
        groups = build_groups(local.size, runs, chosen)
        block.call(calls_for_groups(groups, start_index))
        return
      end

      length = runs[index][:finish] - runs[index][:start] + 1
      each_composition(length, distribution[index] + 1) do |composition|
        walk_partitions(local, runs, distribution, index + 1, chosen + [composition], start_index, &block)
      end
    end

    def each_composition(total, parts, &block)
      return if parts <= 0 || total < parts
      if parts == 1
        block.call([total])
        return
      end

      (1..(total - parts + 1)).each do |first|
        each_composition(total - first, parts - 1) do |rest|
          block.call([first] + rest)
        end
      end
    end

    def build_groups(operation_count, runs, compositions)
      groups = []
      cursor = 0
      runs.each_with_index do |run, run_index|
        while cursor < run[:start]
          groups << [cursor, cursor]
          cursor += 1
        end
        compositions[run_index].each do |length|
          groups << [cursor, cursor + length - 1]
          cursor += length
        end
      end
      while cursor < operation_count
        groups << [cursor, cursor]
        cursor += 1
      end
      groups
    end

    def merge_runs(local)
      runs = []
      index = 0
      while index < local.size
        if mergeable_operation?(local[index])
          finish = index
          while finish + 1 < local.size &&
                mergeable_pair?(local[finish], local[finish + 1])
            finish += 1
          end
          runs << { :start => index, :finish => finish } if finish > index
          index = finish + 1
        else
          index += 1
        end
      end
      runs
    end

    def mergeable_operation?(operation)
      operation && operation[:items]
    end

    def mergeable_pair?(left, right)
      left && right &&
        left[:prefix_tokens] == right[:prefix_tokens] &&
        left[:items] && right[:items]
    end

    def calls_for_groups(groups, start_index)
      groups.map do |first_local, last_local|
        first_index = start_index + first_local
        last_index = start_index + last_local
        if first_local == last_local
          Call.new(@operations[first_index], [first_index])
        else
          instruction = @parsed_operations[first_index]
          selectors = (first_index..last_index).flat_map { |index| @parsed_operations[index][:items] }
          Call.new("#{instruction[:prefix]}[#{selectors.join(',')}]", (first_index..last_index).to_a)
        end
      end
    end

    def singleton_calls(start_index, end_index)
      (start_index...end_index).map { |index| Call.new(@operations[index], [index]) }
    end

    def simulate(start_shadow, calls)
      shadow = Slots.deep_copy(start_shadow)
      begin
        calls.each { |call| shadow = JsonPath.for(shadow).delete!(call.path).obj }
      rescue StandardError
        return nil
      end
      shadow
    end

    def segment_matches?(result_shadow, target)
      return false unless result_shadow
      return false unless Slots.untag(result_shadow) == target[:plain]

      Slots.slots(result_shadow) == target_slots_for(target)
    end

    def target_slots_for(target)
      @target_slots ||= {}
      @target_slots[target.object_id] ||= Slots.slots(target[:shadow])
    end

    # Used when a segment has more possible partitions than max_candidates.
    # It starts from the most merged shape and progressively splits unsafe
    # groups, always checking the handoff before accepting the plan.
    def heuristic_partition(start_index, end_index, local, runs, singles, target)
      groups = build_groups(local.size, runs, runs.map { |run| [run[:finish] - run[:start] + 1] })
      attempts = 0

      loop do
        calls = calls_for_groups(groups, start_index)
        result = simulate(@boundary_states[start_index][:shadow], calls)
        if segment_matches?(result, target)
          record_run_rejections(start_index, end_index, local, runs, target, calls)
          return [calls, result]
        end

        attempts += 1
        if attempts > @max_candidates
          record_run_rejections(start_index, end_index, local, runs, target)
          return [singles, simulate(@boundary_states[start_index][:shadow], singles)]
        end

        split = find_splitting_partition(groups, start_index, target)
        if split
          groups = split
        else
          group = groups.select { |first, last| last > first }.max_by { |first, last| last - first }
          break unless group
          first, last = group
          middle = (first + last) / 2
          index = groups.index(group)
          groups = groups.dup
          groups[index, 1] = [[first, middle], [middle + 1, last]]
          groups.sort!
        end
      end

      record_run_rejections(start_index, end_index, local, runs, target)
      [singles, simulate(@boundary_states[start_index][:shadow], singles)]
    end

    def find_splitting_partition(groups, start_index, target)
      candidates = []
      groups.each_with_index do |(first, last), index|
        next if first == last
        (first...last).each do |split_at|
          candidate = groups.dup
          candidate[index, 1] = [[first, split_at], [split_at + 1, last]]
          calls = calls_for_groups(candidate, start_index)
          result = simulate(@boundary_states[start_index][:shadow], calls)
          next unless segment_matches?(result, target)
          candidates << candidate.sort
        end
      end
      candidates.min_by(&:size)
    end

    def record_run_rejections(start_index, end_index, local, runs, target, accepted_calls = nil)
      runs.each do |run|
        next if run[:start] == run[:finish]

        calls = calls_for_groups(isolated_run_groups(local.size, run), start_index)
        result = simulate(@boundary_states[start_index][:shadow], calls)
        next if result && segment_matches?(result, target)

        merged = calls.find(&:merged?)
        add_rejection(start_index, end_index, calls, merged, target, result)
      end
    end

    def isolated_run_groups(operation_count, run)
      groups = []
      index = 0
      while index < operation_count
        if index == run[:start]
          groups << [run[:start], run[:finish]]
          index = run[:finish] + 1
        else
          groups << [index, index]
          index += 1
        end
      end
      groups
    end

    def add_rejection(start_index, end_index, calls, call, target, result)
      return unless call && call.merged? && result
      return if @rejections.any? do |rejection|
        rejection.operation_indexes == call.operation_indexes &&
          rejection.merged_path == call.path &&
          rejection.operations == @operations[start_index...end_index]
      end

      actual_plain = Slots.untag(result)
      @rejections << Rejection.new(
        :document => Slots.deep_copy(@document),
        :operations => @operations.dup,
        :segment_start => start_index,
        :segment_operations => @operations[start_index...end_index].dup,
        :segment_document => Slots.deep_copy(@boundary_states[start_index][:plain]),
        :calls => calls.map(&:to_s),
        :attempted_calls => calls.map(&:to_s),
        :merged_path => call.path,
        :operation_indexes => call.operation_indexes.dup,
        :expected_json => Slots.deep_copy(target[:plain]),
        :actual_json => Slots.deep_copy(actual_plain),
        :expected_slots => Slots.deep_copy(Slots.slots(target[:shadow])),
        :actual_slots => Slots.deep_copy(Slots.slots(result)),
        :first_difference => first_difference(target[:plain], actual_plain,
                                              Slots.slots(target[:shadow]), Slots.slots(result),
                                              start_index, end_index)
      )
    end

    def first_difference(expected_json, actual_json, expected_slots, actual_slots,
                         start_index, end_index)
      {
        :after_operations => end_index,
        :operation_index => start_index,
        :json => deep_difference(expected_json, actual_json),
        :slots => deep_difference(expected_slots, actual_slots)
      }
    end

    def deep_difference(expected, actual, path = '$')
      if expected.class != actual.class
        return { :path => path, :expected => expected, :actual => actual }
      end

      case expected
      when Hash
        expected.each_key do |key|
          unless actual.key?(key)
            return { :path => "#{path}.#{key}", :expected => expected[key], :actual => nil }
          end
        end
        actual.each_key do |key|
          unless expected.key?(key)
            return { :path => "#{path}.#{key}", :expected => nil, :actual => actual[key] }
          end
        end
        expected.each_key do |key|
          difference = deep_difference(expected[key], actual[key], "#{path}.#{key}")
          return difference if difference
        end
      when Array
        if expected.length != actual.length
          return { :path => path, :expected => expected.length, :actual => actual.length }
        end
        expected.each_with_index do |value, index|
          difference = deep_difference(value, actual[index], "#{path}[#{index}]")
          return difference if difference
        end
      else
        return { :path => path, :expected => expected, :actual => actual } unless expected == actual
      end
      nil
    end

    def validate_plan!
      @handoffs.each do |handoff|
        actual = @planned_plain_states[handoff]
        expected = @boundary_states[handoff][:plain]
        unless actual == expected
          raise Error, "merged plan differs from original delete sequence at handoff #{handoff}"
        end
        unless Slots.slots(@planned_shadow_states[handoff]) == Slots.slots(@boundary_states[handoff][:shadow])
          raise Error, "merged plan lost original slots at handoff #{handoff}"
        end
      end
    end

    # Container subclasses used to simulate native delete while retaining the
    # slot each value came from. Scalar values are left untouched so filters,
    # comparisons, and native compaction behave exactly as before.
    module Slots
      class OriginHash < ::Hash
        attr_accessor :origin, :key_origins

        def initialize(origin = nil)
          super()
          @origin = origin
          @key_origins = {}
        end

        def []=(key, value)
          super
          @key_origins[key] = slot_for_child(key, value)
        end
        alias store []=

        def delete(key, &block)
          existed = key?(key)
          result = super
          @key_origins.delete(key) if existed
          result
        end

        def delete_if
          kept = []
          kept_origins = {}
          each do |key, value|
            next if yield(key, value)
            kept << [key, value]
            kept_origins[key] = @key_origins[key]
          end
          clear
          kept.each { |key, value| self[key] = value }
          @key_origins = kept_origins
          self
        end

        def replace(other)
          clear
          other.each { |key, value| self[key] = value }
          self
        end

        def clear
          super
          @key_origins = {}
        end

        def slot_for_child(key, value)
          if value.is_a?(OriginHash) || value.is_a?(OriginArray)
            value.origin
          else
            @key_origins[key] || "#{@origin}.#{key}"
          end
        end

        def marshal_dump
          [to_h, @origin, @key_origins]
        end

        def marshal_load(data)
          clear
          @origin = data[1]
          values = data[0]
          origins = data[2] || {}
          ::Hash.instance_method(:each).bind(values).call do |key, value|
            @key_origins[key] = origins[key] || "#{@origin}.#{key}"
            ::Hash.instance_method(:[]=).bind(self).call(key, value)
          end
        end
      end

      class OriginArray < ::Array
        attr_accessor :origin, :slot_map

        def initialize(origin = nil)
          super()
          @origin = origin
          @slot_map = []
        end

        def <<(value)
          @slot_map << slot_for_child(size, value)
          super
        end
        alias push <<

        def concat(values)
          values.each { |value| self << value }
          self
        end

        def []=(index, value)
          normalized = index.is_a?(Integer) ? index : nil
          if normalized
            normalized += size if normalized.negative?
            if normalized >= 0
              @slot_map[normalized] = slot_for_child(normalized, value)
            end
          end
          super
        end

        def delete_at(index)
          result = super
          if index.is_a?(Integer)
            normalized = index.negative? ? index + size : index
            @slot_map.delete_at(normalized) if normalized >= 0
          end
          result
        end

        def compact!
          kept = []
          kept_slots = []
          changed = false
          each_with_index do |value, index|
            if value.nil?
              changed = true
            else
              kept << value
              kept_slots << @slot_map[index]
            end
          end
          return nil unless changed

          replace_contents(kept, kept_slots)
          self
        end

        def compact
          copy = dup
          copy.compact!
          copy
        end

        def delete(item, &block)
          kept = []
          kept_slots = []
          found = false
          last = nil
          each_with_index do |value, index|
            if value == item
              found = true
              last = value
            else
              kept << value
              kept_slots << @slot_map[index]
            end
          end
          return block ? block.call(item) : nil unless found

          replace_contents(kept, kept_slots)
          last
        end

        def delete_if
          kept = []
          kept_slots = []
          each_with_index do |value, index|
            next if yield(value)
            kept << value
            kept_slots << @slot_map[index]
          end
          replace_contents(kept, kept_slots)
          self
        end

        def reject!
          kept = []
          kept_slots = []
          each_with_index do |value, index|
            next if yield(value)
            kept << value
            kept_slots << @slot_map[index]
          end
          return nil if kept.size == size

          replace_contents(kept, kept_slots)
          self
        end

        def replace(values)
          clear
          values.each { |value| self << value }
          self
        end

        def clear
          super
          @slot_map = []
        end

        def shift
          result = super
          @slot_map.shift unless result.nil?
          result
        end

        def pop
          result = super
          @slot_map.pop unless result.nil?
          result
        end

        def slot_for_child(index, value)
          if value.is_a?(OriginHash) || value.is_a?(OriginArray)
            value.origin
          else
            @slot_map[index] || "#{@origin}[#{index}]"
          end
        end

        def marshal_dump
          [to_a, @origin, @slot_map]
        end

        def marshal_load(data)
          clear
          @origin = data[1]
          @slot_map = (data[2] || []).dup
          data[0].each_with_index do |value, index|
            @slot_map[index] ||= "#{@origin}[#{index}]"
            ::Array.instance_method(:<<).bind(self).call(value)
          end
        end

        private

        def replace_contents(values, slots)
          clear
          values.each { |value| self << value }
          @slot_map = slots
          self
        end
      end

      class << self
        def deep_copy(value)
          Marshal.load(Marshal.dump(value))
        end

        def tag(node, origin)
          case node
          when ::Hash
            result = OriginHash.new(origin)
            node.each { |key, value| result[key] = tag(value, "#{origin}.#{key}") }
            result
          when ::Array
            result = OriginArray.new(origin)
            node.each_with_index { |value, index| result << tag(value, "#{origin}[#{index}]") }
            result
          else
            node
          end
        end

        def untag(node)
          case node
          when OriginHash
            node.each_with_object({}) { |(key, value), memo| memo[key] = untag(value) }
          when OriginArray
            node.map { |value| untag(value) }
          else
            node
          end
        end

        def origins(node)
          case node
          when OriginHash
            node.each_with_object({}) do |(key, value), memo|
              memo[key] = if value.is_a?(OriginHash) || value.is_a?(OriginArray)
                            origins(value)
                          else
                            node.key_origins[key]
                          end
            end
          when OriginArray
            node.each_with_index.map do |value, index|
              if value.is_a?(OriginHash) || value.is_a?(OriginArray)
                origins(value)
              else
                node.slot_map[index]
              end
            end
          else
            nil
          end
        end

        def slots(node)
          case node
          when OriginHash
            {
              'type' => 'hash',
              'origin' => node.origin,
              'entries' => node.each_with_object({}) do |(key, value), memo|
                memo[key] = if value.is_a?(OriginHash) || value.is_a?(OriginArray)
                              slots(value)
                            else
                              { 'type' => 'value', 'origin' => node.key_origins[key] }
                            end
              end
            }
          when OriginArray
            {
              'type' => 'array',
              'origin' => node.origin,
              'entries' => node.each_with_index.map do |value, index|
                if value.is_a?(OriginHash) || value.is_a?(OriginArray)
                  slots(value)
                else
                  { 'type' => 'value', 'origin' => node.slot_map[index] }
                end
              end
            }
          else
            { 'type' => 'value', 'origin' => nil }
          end
        end
      end
    end
  end
end
