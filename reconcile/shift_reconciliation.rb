require "json"
require "time"
require "set"
require "yaml"
require "ice_cube"

module ShiftReconciliation
  Shift = Struct.new(:id, :schedule_id, :index, :time, keyword_init: true)
  Receipt = Struct.new(
    :id, :time, :schedule_ids, :early, :late,
    :segment_key, :segment_index, keyword_init: true
  )
  Pair = Struct.new(
    :receipt_id, :shift_id, :schedule_id, :shift_index, :shift_time,
    :hard, :segment_key, :segment_index, :receipt_at, keyword_init: true
  )

  module TimeIO
    module_function

    def parse(value)
      case value
      when Integer then value
      when Numeric then value.to_i
      when Time then value.utc.to_i
      when String
        return value.to_i if value.match?(/\A-?\d+\z/)

        Time.iso8601(value).utc.to_i
      else
        raise ArgumentError, "unsupported time #{value.inspect}"
      end
    rescue ArgumentError
      Time.parse(value.to_s).utc.to_i
    end

    def format(time)
      Time.at(time).utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end
  end

  class Archive
    ScheduleInput = Struct.new(
      :id, :schedule, :extra_times, :exception_times,
      :horizon_start, :horizon_end, keyword_init: true
    )

    attr_accessor :horizon_start, :horizon_end
    attr_reader :schedule_inputs, :receipt_inputs, :hard_inputs

    def initialize
      @schedule_inputs = {}
      @receipt_inputs = []
      @hard_inputs = []
      yield self if block_given?
    end

    def horizon(start: nil, finish: nil)
      @horizon_start = start.nil? ? nil : TimeIO.parse(start)
      @horizon_end = finish.nil? ? nil : TimeIO.parse(finish)
    end

    def schedule(id, schedule = nil, start: nil, horizon_start: nil, horizon_end: nil,
                 extra_times: [], exception_times: [])
      key = id.to_s
      schedule ||= IceCube::Schedule.new(Time.at(TimeIO.parse(start)).utc) if start
      raise ArgumentError, "schedule or start is required for #{key}" unless schedule

      input = ScheduleInput.new(
        id: key,
        schedule: schedule,
        extra_times: extra_times.map { |time| TimeIO.parse(time) },
        exception_times: exception_times.map { |time| TimeIO.parse(time) },
        horizon_start: horizon_start.nil? ? nil : TimeIO.parse(horizon_start),
        horizon_end: horizon_end.nil? ? nil : TimeIO.parse(horizon_end)
      )
      @schedule_inputs[key] = input
      yield schedule if block_given?
      input
    end

    def extra_time(schedule_id, time)
      @schedule_inputs[schedule_id.to_s].extra_times << TimeIO.parse(time)
    end

    def exception_time(schedule_id, time)
      @schedule_inputs[schedule_id.to_s].exception_times << TimeIO.parse(time)
    end

    def receipt(id:, at: nil, time: nil, schedule: nil, schedule_ids: nil,
                early: 0, late: 0, tolerance: nil,
                segment_key: nil, segment_index: nil)
      early = tolerance if tolerance
      late = tolerance if tolerance
      ids = Array(schedule || schedule_ids).flatten.compact.map(&:to_s)
      @receipt_inputs << {
        id: id.to_s,
        time: TimeIO.parse(at || time),
        schedule_ids: ids,
        early: early.to_i,
        late: late.to_i,
        segment_key: segment_key&.to_s,
        segment_index: segment_index&.to_i
      }
    end

    def segment(start:, step:, count:, gaps: [], id_template: nil,
                id_prefix: nil, id_start: 1, digits: 4, key: nil, **options)
      gap_indices = gaps.map(&:to_i).to_set
      first_time = TimeIO.parse(start)
      count.to_i.times do |index|
        next if gap_indices.include?(index)

        number = id_start.to_i + index
        receipt_id =
          if id_template
            format(id_template, number)
          elsif id_prefix
            "#{id_prefix}#{number.to_s.rjust(digits.to_i, "0")}"
          else
            "#{key || "segment"}##{index}"
          end

        receipt(
          id: receipt_id,
          at: first_time + (index * step.to_i),
          segment_key: key&.to_s,
          segment_index: index,
          **options
        )
      end
    end

    def hard_match(receipt:, shift: nil, shift_id: nil, schedule: nil,
                   shift_index: nil, at: nil)
      @hard_inputs << {
        receipt_id: receipt.to_s,
        shift_id: (shift || shift_id)&.to_s,
        schedule_id: schedule&.to_s,
        shift_index: shift_index&.to_i,
        time: at.nil? ? nil : TimeIO.parse(at)
      }
    end

    def prepare
      Engine.new(self)
    end

    def self.load(path)
      content = File.read(path)
      data =
        if File.extname(path).downcase == ".json"
          JSON.parse(content)
        else
          YAML.safe_load(content, permitted_classes: [Date, Symbol, Time], aliases: true)
        end
      from_hash(data)
    end

    def self.from_hash(data)
      string_keyed = deep_stringify(data)
      new do |archive|
        horizon_data = string_keyed["horizon"] || {}
        archive.horizon(start: horizon_data["start"], finish: horizon_data["end"])

        Array(string_keyed["schedules"]).each do |item|
          schedule =
            if item["yaml"]
              IceCube::Schedule.from_yaml(item["yaml"])
            elsif item["hash"]
              IceCube::Schedule.from_hash(item["hash"])
            elsif item["ical"]
              IceCube::Schedule.from_ical(item["ical"])
            end

          archive.schedule(
            item["id"],
            schedule,
            start: item["start"],
            horizon_start: item["horizon_start"],
            horizon_end: item["horizon_end"],
            extra_times: Array(item["extra_times"]),
            exception_times: Array(item["exception_times"])
          )
        end

        Array(string_keyed["receipts"]).each do |item|
          if item["step"] && item["count"] && (item["start"] || item["first"])
            archive.segment(
              start: item["start"] || item["first"],
              step: item["step"],
              count: item["count"],
              gaps: item["gaps"] || [],
              id_template: item["id_template"],
              id_prefix: item["id_prefix"],
              id_start: item["id_start"] || 1,
              digits: item["digits"] || 4,
              key: item["key"] || item["id"],
              schedule: item["schedule"],
              schedule_ids: item["schedule_ids"],
              early: item["early"] || 0,
              late: item["late"] || 0,
              tolerance: item["tolerance"]
            )
          else
            archive.receipt(
              id: item["id"],
              at: item["at"] || item["time"],
              schedule: item["schedule"],
              schedule_ids: item["schedule_ids"],
              early: item["early"] || 0,
              late: item["late"] || 0,
              tolerance: item["tolerance"]
            )
          end
        end

        Array(string_keyed["hard_matches"]).each do |item|
          archive.hard_match(
            receipt: item["receipt"] || item["receipt_id"],
            shift: item["shift"] || item["shift_id"],
            schedule: item["schedule"],
            shift_index: item["shift_index"],
            at: item["at"]
          )
        end
      end
    end

    def self.deep_stringify(value)
      case value
      when Hash
        value.to_h { |key, item| [key.to_s, deep_stringify(item)] }
      when Array
        value.map { |item| deep_stringify(item) }
      else
        value
      end
    end
  end

  class HopcroftKarp
    attr_reader :pair_u, :pair_v, :distance_nil

    def initialize(adjacency, right_count)
      @adjacency = adjacency
      @left_count = adjacency.size
      @right_count = right_count
      @pair_u = Array.new(@left_count)
      @pair_v = Array.new(@right_count)
      @distance = Array.new(@left_count)
      @distance_nil = @left_count + 1
      @infinity = @left_count + 1
    end

    def seed(pairs)
      pairs.each do |left, right|
        raise ArgumentError, "left #{left} already seeded" unless @pair_u[left].nil?
        raise ArgumentError, "right #{right} already seeded" unless @pair_v[right].nil?

        @pair_u[left] = right
        @pair_v[right] = left
      end
    end

    def maximize(roots = (0...@left_count).to_a)
      root_ids = roots.to_a
      loop do
        break unless bfs(root_ids)

        augmented = false
        root_ids.each do |left|
          next unless @pair_u[left].nil?
          augmented = true if dfs(left)
        end
        break unless augmented
      end
      matched_count
    end

    def matched_count
      @pair_u.count { |right| !right.nil? }
    end

    private

    def bfs(roots)
      queue = []
      @distance.fill(@infinity)
      @distance_nil = @infinity

      roots.each do |left|
        next unless @pair_u[left].nil?

        @distance[left] = 0
        queue << left
      end

      until queue.empty?
        left = queue.shift
        next if @distance[left] >= @distance_nil

        @adjacency[left].each do |right|
          next unless right < @right_count

          next_left = @pair_v[right]
          if next_left.nil?
            @distance_nil = @distance[left] + 1
          elsif @distance[next_left] == @infinity
            @distance[next_left] = @distance[left] + 1
            queue << next_left
          end
        end
      end

      @distance_nil != @infinity
    end

    def dfs(left)
      @adjacency[left].each do |right|
        next_left = @pair_v[right]

        if next_left.nil?
          next unless @distance[left] + 1 == @distance_nil
        else
          next unless @distance[next_left] == @distance[left] + 1
          next unless dfs(next_left)
        end

        @pair_u[left] = right
        @pair_v[right] = left
        return true
      end

      @distance[left] = @infinity
      false
    end
  end

  class Engine
    attr_reader :archive, :shifts, :receipts, :hard_pairs, :adjacency,
                :free_shifts, :free_receipts, :free_shift_index,
                :free_receipt_index, :free_max_count, :duplicate_receipt_count

    def initialize(archive)
      @archive = archive
      generate_shifts
      canonicalize_receipts
      resolve_hard_matches
      build_free_graph
      run_full_matching
    end

    def shift(reference)
      case reference
      when Shift then reference
      when Integer then @shifts[reference]
      else @shifts_by_id[reference.to_s]
      end
    end

    def unavoidable_missing
      @free_shifts
        .select { |shift| @adjacency[@free_shift_index[shift.id]].empty? }
        .sort_by { |shift| [shift.time, shift.schedule_id, shift.index] }
    end

    def earliest_unavoidable_missing
      unavoidable_missing.first
    end

    def ambiguity_swap_example
      unmatched = @free_shifts.find do |shift|
        left = @free_shift_index[shift.id]
        @canonical_pair_u[left].nil? && @adjacency[left].any?
      end
      return nil unless unmatched

      left = @free_shift_index[unmatched.id]
      right = @adjacency[left].first
      displaced_left = @canonical_pair_v[right]
      return nil if displaced_left.nil?

      {
        added_pair: [unmatched, @free_receipts[right]],
        removed_pair: [@free_shifts[displaced_left], @free_receipts[right]]
      }
    end

    def pair_for_preferred(preferred_shift_ids)
      preferred_lefts = Array(preferred_shift_ids)
        .map do |value|
          case value
          when Integer then value
          when Shift then @free_shift_index[value.id]
          else @free_shift_index[value.to_s]
          end
        end
        .compact
        .uniq

      matcher = HopcroftKarp.new(@adjacency, @free_receipts.size)
      preferred_rank = matcher.maximize(preferred_lefts)
      matcher.maximize(0...@free_shifts.size)

      {
        preferred_rank: preferred_rank,
        free_count: matcher.matched_count,
        pairs: build_pairs(matcher.pair_u)
      }
    end

    def window_report(start_time, end_time)
      window_start = TimeIO.parse(start_time)
      window_end = TimeIO.parse(end_time)
      window_shifts = shifts_between(window_start, window_end)
      window_ids = window_shifts.map(&:id).to_set
      window_free_lefts = window_ids.filter_map { |id| @free_shift_index[id] }
      outside_free_lefts = (0...@free_shifts.size).to_a - window_free_lefts

      fewest_missing_run = pair_for_preferred(window_free_lefts)
      most_missing_run = pair_for_preferred(outside_free_lefts)

      hard_in_window = @hard_pairs.count do |pair|
        pair.shift_time >= window_start && pair.shift_time <= window_end
      end

      maximum_collected = hard_in_window + fewest_missing_run[:preferred_rank]
      minimum_collected = hard_in_window + @free_max_count - most_missing_run[:preferred_rank]
      total = window_shifts.size

      {
        window_start: window_start,
        window_end: window_end,
        total_shifts: total,
        hard_matched_shifts: hard_in_window,
        maximum_collected: maximum_collected,
        minimum_collected: minimum_collected,
        fewest_missing: total - maximum_collected,
        most_missing: total - minimum_collected,
        fewest_missing_pairs: fewest_missing_run[:pairs],
        most_missing_pairs: most_missing_run[:pairs]
      }
    end

    def full_report
      if @archive.horizon_start && @archive.horizon_end
        window_report(@archive.horizon_start, @archive.horizon_end)
      else
        times = @shifts.map(&:time)
        window_report(times.min || 0, times.max || 0)
      end
    end

    def compact_pairs(pairs)
      groups = []
      pairs.group_by(&:schedule_id).sort.each do |schedule_id, schedule_pairs|
        schedule_pairs.sort_by!(&:shift_index)
        schedule_pairs.each do |pair|
          group = groups.last
          if group && can_extend_group?(group, pair)
            extend_group(group, pair)
          else
            groups << new_group(pair)
          end
        end
      end
      groups
    end

    def verify_pairs(pairs)
      errors = []
      receipt_counts = Hash.new(0)
      shift_counts = Hash.new(0)

      pairs.each do |pair|
        receipt_counts[pair.receipt_id] += 1
        shift_counts[pair.shift_id] += 1

        unless @receipts_by_id.key?(pair.receipt_id)
          errors << "unknown receipt #{pair.receipt_id}"
        end
        unless @shifts_by_id.key?(pair.shift_id)
          errors << "unknown shift #{pair.shift_id}"
        end

        next if pair.hard

        receipt = @receipts_by_id[pair.receipt_id]
        shift = @shifts_by_id[pair.shift_id]
        if receipt && shift && !candidate?(receipt, shift)
          errors << "pair outside tolerance: #{pair.receipt_id} -> #{pair.shift_id}"
        end
      end

      receipt_counts.each { |id, count| errors << "receipt #{id} used #{count} times" if count > 1 }
      shift_counts.each { |id, count| errors << "shift #{id} used #{count} times" if count > 1 }

      @hard_pairs.each do |hard_pair|
        unless pairs.any? { |pair| pair.receipt_id == hard_pair.receipt_id && pair.shift_id == hard_pair.shift_id }
          errors << "hard pair missing #{hard_pair.receipt_id} -> #{hard_pair.shift_id}"
        end
      end

      errors
    end

    private

    def generate_shifts
      @shifts_by_schedule = {}
      @shifts = []

      @archive.schedule_inputs.each_value do |input|
        schedule = input.schedule
        input.extra_times.each { |time| schedule.add_recurrence_time(Time.at(time).utc) }
        input.exception_times.each { |time| schedule.add_exception_time(Time.at(time).utc) }

        range_start = input.horizon_start || @archive.horizon_start || schedule.start_time.utc.to_i
        range_end = input.horizon_end || @archive.horizon_end

        occurrences =
          if range_end
            schedule.occurrences_between(
              Time.at(range_start).utc,
              Time.at(range_end).utc
            ).to_a
          elsif schedule.terminating?
            schedule.all_occurrences.to_a
          else
            raise ArgumentError, "horizon end is required for non-terminating schedule #{input.id}"
          end

        times = occurrences.map { |occurrence| occurrence.start_time.utc.to_i }.uniq.sort
        schedule_shifts = times.each_with_index.map do |time, index|
          Shift.new(
            id: "#{input.id}##{index + 1}",
            schedule_id: input.id,
            index: index + 1,
            time: time
          )
        end
        @shifts_by_schedule[input.id] = schedule_shifts
        @shifts.concat(schedule_shifts)
      end

      @shifts.sort_by! { |shift| [shift.time, shift.schedule_id, shift.index] }
      @shifts_by_id = @shifts.to_h { |shift| [shift.id, shift] }
    end

    def canonicalize_receipts
      raw = @archive.receipt_inputs
      @duplicate_receipt_count = 0
      canonical = {}

      raw.each do |attributes|
        id = attributes[:id]
        if canonical.key?(id)
          @duplicate_receipt_count += 1
          existing = canonical[id]
          if existing[:time] != attributes[:time] ||
              existing[:schedule_ids] != attributes[:schedule_ids] ||
              existing[:early] != attributes[:early] ||
              existing[:late] != attributes[:late]
            raise ArgumentError, "conflicting resent receipt #{id}"
          end
          existing[:segment_key] ||= attributes[:segment_key]
          existing[:segment_index] ||= attributes[:segment_index]
        else
          canonical[id] = attributes.dup
        end
      end

      @receipts = canonical.values.map do |attributes|
        Receipt.new(
          id: attributes[:id],
          time: attributes[:time],
          schedule_ids: attributes[:schedule_ids],
          early: attributes[:early],
          late: attributes[:late],
          segment_key: attributes[:segment_key],
          segment_index: attributes[:segment_index]
        )
      end
      @receipts_by_id = @receipts.to_h { |receipt| [receipt.id, receipt] }
    end

    def resolve_hard_matches
      @hard_pairs = []
      @hard_receipt_ids = Set.new
      @hard_shift_ids = Set.new

      @archive.hard_inputs.each do |input|
        receipt = @receipts_by_id[input[:receipt_id]]
        raise ArgumentError, "hard match references unknown receipt #{input[:receipt_id]}" unless receipt

        shift = resolve_hard_shift(input)
        if @hard_receipt_ids.include?(receipt.id)
          raise ArgumentError, "receipt #{receipt.id} has conflicting hard matches"
        end
        if @hard_shift_ids.include?(shift.id)
          raise ArgumentError, "shift #{shift.id} has conflicting hard matches"
        end

        @hard_receipt_ids << receipt.id
        @hard_shift_ids << shift.id
        @hard_pairs << pair_from(receipt, shift, hard: true)
      end
    end

    def resolve_hard_shift(input)
      if input[:shift_id]
        shift = @shifts_by_id[input[:shift_id]]
        raise ArgumentError, "hard match references unknown shift #{input[:shift_id]}" unless shift

        return shift
      end

      schedule_id = input[:schedule_id]
      schedule_shifts = @shifts_by_schedule[schedule_id]
      raise ArgumentError, "hard match references unknown schedule #{schedule_id}" unless schedule_shifts

      if input[:shift_index]
        shift = schedule_shifts[input[:shift_index] - 1]
        raise ArgumentError, "hard match shift index is out of range #{schedule_id}##{input[:shift_index]}" unless shift

        return shift
      end

      if input[:time]
        shift = schedule_shifts.find { |candidate| candidate.time == input[:time] }
        raise ArgumentError, "hard match references missing occurrence at #{TimeIO.format(input[:time])}" unless shift

        return shift
      end

      raise ArgumentError, "hard match needs shift id, schedule/index, or schedule/at"
    end

    def build_free_graph
      @free_shifts = @shifts.reject { |shift| @hard_shift_ids.include?(shift.id) }
      @free_receipts = @receipts.reject { |receipt| @hard_receipt_ids.include?(receipt.id) }
      @free_shift_index = @free_shifts.each_with_index.to_h { |shift, index| [shift.id, index] }
      @free_receipt_index = @free_receipts.each_with_index.to_h { |receipt, index| [receipt.id, index] }
      @adjacency = Array.new(@free_shifts.size) { [] }

      @free_receipts.each_with_index do |receipt, right|
        schedule_ids = receipt.schedule_ids
        schedule_ids = @shifts_by_schedule.keys if schedule_ids.empty?

        schedule_ids.each do |schedule_id|
          schedule_shifts = @shifts_by_schedule[schedule_id]
          raise ArgumentError, "receipt #{receipt.id} references unknown schedule #{schedule_id}" unless schedule_shifts

          lower = lower_bound_time(schedule_shifts, receipt.time - receipt.late)
          upper_exclusive = upper_bound_time(schedule_shifts, receipt.time + receipt.early)

          schedule_shifts[lower...upper_exclusive].to_a.each do |shift|
            left = @free_shift_index[shift.id]
            next if left.nil?
            next if @adjacency[left].include?(right)

            @adjacency[left] << right
          end
        end
      end
    end

    def run_full_matching
      matcher = HopcroftKarp.new(@adjacency, @free_receipts.size)
      matcher.maximize
      @canonical_pair_u = matcher.pair_u
      @canonical_pair_v = matcher.pair_v
      @free_max_count = matcher.matched_count
    end

    def build_pairs(pair_u)
      pairs = @hard_pairs.dup
      pair_u.each_with_index do |right, left|
        next if right.nil?

        shift = @free_shifts[left]
        receipt = @free_receipts[right]
        pairs << pair_from(receipt, shift, hard: false)
      end
      pairs.sort_by { |pair| [pair.shift_time, pair.schedule_id, pair.shift_index] }
    end

    def pair_from(receipt, shift, hard:)
      Pair.new(
        receipt_id: receipt.id,
        shift_id: shift.id,
        schedule_id: shift.schedule_id,
        shift_index: shift.index,
        shift_time: shift.time,
        hard: hard,
        segment_key: receipt.segment_key,
        segment_index: receipt.segment_index,
        receipt_at: receipt.time
      )
    end

    def candidate?(receipt, shift)
      return false unless receipt.schedule_ids.empty? || receipt.schedule_ids.include?(shift.schedule_id)

      shift.time >= receipt.time - receipt.late &&
        shift.time <= receipt.time + receipt.early
    end

    def shifts_between(start_time, end_time)
      lower = lower_bound_time(@shifts, start_time)
      upper_exclusive = upper_bound_time(@shifts, end_time)
      @shifts[lower...upper_exclusive].to_a
    end

    def lower_bound_time(list, time)
      (0...list.size).bsearch { |index| list[index].time >= time } || list.size
    end

    def upper_bound_time(list, time)
      (0...list.size).bsearch { |index| list[index].time > time } || list.size
    end

    def new_group(pair)
      numeric = numeric_receipt_parts(pair.receipt_id)
      {
        schedule_id: pair.schedule_id,
        hard: pair.hard,
        count: 1,
        first_shift_index: pair.shift_index,
        last_shift_index: pair.shift_index,
        first_receipt_id: pair.receipt_id,
        last_receipt_id: pair.receipt_id,
        segment_key: pair.segment_key,
        first_segment_index: pair.segment_index,
        last_segment_index: pair.segment_index,
        receipt_prefix: numeric&.dig(:prefix),
        receipt_width: numeric&.dig(:width),
        last_receipt_number: numeric&.dig(:number),
        first_shift_time: pair.shift_time,
        last_shift_time: pair.shift_time
      }
    end

    def can_extend_group?(group, pair)
      return false unless group[:schedule_id] == pair.schedule_id
      return false unless group[:hard] == pair.hard
      return false unless pair.shift_index == group[:last_shift_index] + 1

      if group[:segment_key]
        return false unless pair.segment_key == group[:segment_key]
        return false unless pair.segment_index == group[:last_segment_index] + 1

        return true
      end

      numeric = numeric_receipt_parts(pair.receipt_id)
      return false unless numeric
      return false unless numeric[:prefix] == group[:receipt_prefix]
      return false unless numeric[:width] == group[:receipt_width]
      return false unless numeric[:number] == group[:last_receipt_number] + 1

      true
    end

    def extend_group(group, pair)
      group[:count] += 1
      group[:last_shift_index] = pair.shift_index
      group[:last_shift_time] = pair.shift_time
      group[:last_receipt_id] = pair.receipt_id
      group[:last_segment_index] = pair.segment_index if pair.segment_key
      numeric = numeric_receipt_parts(pair.receipt_id)
      group[:last_receipt_number] = numeric[:number] if numeric
    end

    def numeric_receipt_parts(id)
      match = id.match(/\A(.*?)(\d+)\z/)
      return nil unless match

      { prefix: match[1], number: match[2].to_i, width: match[2].length }
    end
  end

  module Report
    module_function

    def window_hash(engine, report, include_pairs: false)
      {
        window: {
          start: TimeIO.format(report[:window_start]),
          end: TimeIO.format(report[:window_end])
        },
        shifts_in_window: report[:total_shifts],
        hard_matched_shifts: report[:hard_matched_shifts],
        missing: {
          maximum: report[:most_missing],
          minimum: report[:fewest_missing]
        },
        collected: {
          minimum: report[:minimum_collected],
          maximum: report[:maximum_collected]
        },
        unavoidable_earliest_missing: shift_hash(engine.earliest_unavoidable_missing),
        unavoidable_missing_count: engine.unavoidable_missing.size,
        unavoidable_missing_ranges: shift_ranges(engine.unavoidable_missing),
        unavoidable_missing: include_pairs ? engine.unavoidable_missing.map { |shift| shift_hash(shift) } : nil,
        pairings: {
          achieve_minimum_missing: pairing_hash(
            engine, report[:fewest_missing_pairs], include_pairs: include_pairs
          ),
          achieve_maximum_missing: pairing_hash(
            engine, report[:most_missing_pairs], include_pairs: include_pairs
          )
        },
        ambiguity_swap_example: ambiguity_hash(engine.ambiguity_swap_example)
      }
    end

    def pairing_hash(engine, pairs, include_pairs: false)
      result = {
        count: pairs.size,
        compact: engine.compact_pairs(pairs).map { |group| group_hash(group) }
      }
      result[:pairs] = pairs.map { |pair| pair_hash(pair) } if include_pairs
      result
    end

    def group_hash(group)
      shift_range =
        if group[:first_shift_index] == group[:last_shift_index]
          "#{group[:schedule_id]}##{group[:first_shift_index]}"
        else
          "#{group[:schedule_id]}##{group[:first_shift_index]}..##{group[:last_shift_index]}"
        end
      receipt_range =
        if group[:first_receipt_id] == group[:last_receipt_id]
          group[:first_receipt_id].to_s
        else
          "#{group[:first_receipt_id]}..#{group[:last_receipt_id]}"
        end

      group.merge(shift_range: shift_range, receipt_range: receipt_range)
    end

    def pair_hash(pair)
      {
        receipt: pair.receipt_id,
        shift: pair.shift_id,
        schedule: pair.schedule_id,
        shift_index: pair.shift_index,
        shift_time: TimeIO.format(pair.shift_time),
        receipt_time: TimeIO.format(pair.receipt_at),
        hard: pair.hard
      }
    end

    def shift_hash(shift)
      return nil unless shift

      {
        shift: shift.id,
        schedule: shift.schedule_id,
        index: shift.index,
        time: TimeIO.format(shift.time)
      }
    end

    def shift_ranges(shifts)
      shifts.group_by(&:schedule_id).flat_map do |schedule_id, schedule_shifts|
        schedule_shifts.sort_by!(&:index)
        ranges = []
        schedule_shifts.each do |shift|
          range = ranges.last
          if range && range[:last_index] + 1 == shift.index
            range[:last_index] = shift.index
            range[:last_time] = shift.time
            range[:count] += 1
          else
            ranges << {
              schedule: schedule_id,
              first_index: shift.index,
              last_index: shift.index,
              count: 1,
              first_time: shift.time,
              last_time: shift.time
            }
          end
        end

        ranges.each do |range|
          range[:range] =
            if range[:first_index] == range[:last_index]
              "#{schedule_id}##{range[:first_index]}"
            else
              "#{schedule_id}##{range[:first_index]}..##{range[:last_index]}"
            end
          range[:first_time] = TimeIO.format(range[:first_time])
          range[:last_time] = TimeIO.format(range[:last_time])
        end
      end
    end

    def ambiguity_hash(example)
      return nil unless example

      added_shift, added_receipt = example[:added_pair]
      removed_shift, removed_receipt = example[:removed_pair]
      {
        add: {
          receipt: added_receipt.id,
          shift: added_shift.id
        },
        remove: {
          receipt: removed_receipt.id,
          shift: removed_shift.id
        }
      }
    end

    def text(engine, report)
      data = window_hash(engine, report)
      lines = []
      lines << "窗口 #{data[:window][:start]} ~ #{data[:window][:end]}"
      lines << "班次 #{data[:shifts_in_window]}；最少补采 #{data[:missing][:minimum]}；最多补采 #{data[:missing][:maximum]}"
      earliest = data[:unavoidable_earliest_missing]
      lines << if earliest
        "全档最优均不可避免的最早缺采：#{earliest[:shift]} @ #{earliest[:time]}"
      else
        "没有全档最优均不可避免的缺采"
      end
      lines << "达到最少补采的配对："
      append_groups(lines, data[:pairings][:achieve_minimum_missing][:compact])
      lines << "达到最多补采的配对："
      append_groups(lines, data[:pairings][:achieve_maximum_missing][:compact])
      lines.join("\n")
    end

    def append_groups(lines, groups)
      groups.each do |group|
        label = group[:hard] ? "硬配对 " : ""
        lines << "  - #{label}#{group[:receipt_range]} ↔ #{group[:shift_range]} ×#{group[:count]}"
      end
    end
  end
end
