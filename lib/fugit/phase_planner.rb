# frozen_string_literal: true

require 'json'
require 'date'
require 'raabro'
require 'et-orbi'

require_relative 'misc'

module Fugit
  MAX_INPUT_LENGTH = 256 unless const_defined?(:MAX_INPUT_LENGTH)
end

require_relative 'cron'
require_relative 'at'
require_relative 'parse'

module Fugit

  class PhasePlanner

    WEEK_SECONDS = 7 * 24 * 3600
    DAY_BITS_CACHE = {}

    class Report < Hash

      def initialize(attributes = {})

        super()
        replace(attributes)
      end

      def method_missing(name, *args, &block)

        key = name.to_s
        query = key.end_with?('?')
        key = key.sub(/[?!=]\z/, '')

        return self[key] if key?(key)
        return self[key.to_sym] if key?(key.to_sym)

        if query
          value = self[key] || self[key.to_sym]
          return value == true || value == :ok || value == 'ok' ||
            value == 'resolved' || value == :resolved || value == 'feasible'
        end

        super
      end

      def respond_to_missing?(name, include_private = false)

        key = name.to_s.sub(/[?!=]\z/, '')
        key?(key) || key?(key.to_sym) || super
      end

      private

      def deep_stringify(value)

        case value
        when Hash
          value.each_with_object({}) do |(key, item), hash|
            hash[key.to_s] = deep_stringify(item)
          end
        when Array
          value.collect { |item| deep_stringify(item) }
        else
          value
        end
      end
    end

    Assignment = Class.new(Report)
    Conflict = Class.new(Report)
    Configuration = Class.new(Report)

    class ScriptedRandom

      def initialize(values)

        @values = values.dup
      end

      def rand(max)

        value = @values.shift
        value = 0 if value.nil?
        value = value.rand(max) if value.respond_to?(:rand)
        value
      end
    end

    class TracedCron < Fugit::Cron

      def random(min, max, sta, edn, sla, opts)

        @phase_trace ||= []
        value = super
        @phase_trace << {
          value: value, min: min, max: max,
          from: sta, to: edn, step: sla
        }
        value
      end

      def phase_trace

        @phase_trace ||= []
      end
    end

    class << self

      def plan(*args, **options)

        new(*args, **options).plan
      end

      def plan!(*args, **options)

        new(*args, **options).plan!
      end

      alias allocate plan
      alias pick plan
      alias select plan
      alias resolve plan
      alias call plan
      alias build plan
      alias schedule plan
      alias solve plan
      alias run plan
      alias check plan
      alias validate plan
      alias choose plan
      alias execute plan
      alias perform plan
      alias review plan
      alias inspect_configuration plan

      def save(path, *args, **options)

        planner = new(*args, **options)
        planner.save(path)
      end

      alias save_configuration save

      def load(path)

        document = JSON.parse(File.read(path))
        config = document['config'] || document['configuration'] || document
        new(config).plan
      end

      def from_json(json)

        document = JSON.parse(json)
        config = document['config'] || document['configuration'] || document
        new(config).plan
      end
    end

    def initialize(*args, **options)

      input = {}
      positional_arrays = []

      args.each do |argument|
        if argument.is_a?(Array)
          positional_arrays << argument
        elsif argument.is_a?(Integer)
          options[:capacity] ||= argument
        elsif argument.is_a?(Range)
          options[:window] ||= argument
        elsif argument.is_a?(Hash) && duration_hash?(argument)
          options[:durations] ||= argument
        elsif argument.is_a?(Hash)
          input.merge!(argument)
        elsif argument.is_a?(String)
          parsed = parse_config_argument(argument)
          input.merge!(parsed) if parsed.is_a?(Hash)
        end
      end

      positional_arrays.each do |array|
        if array.any? && array.all? { |item| item.is_a?(Numeric) }
          options[:durations] ||= array
        else
          input[:lines] = Array(input[:lines]) + array
        end
      end

      input.merge!(options)

      if input[:config].is_a?(Hash)
        input = input[:config].merge(input)
      elsif input['config'].is_a?(Hash)
        input = input['config'].merge(input)
      end

      @input = input
      @duration_table = fetch_value(
        input, :durations, :duration_table, :durations_by_id,
        :task_durations, :seconds_per_task, :occupancy,
        :occupancy_table, :seconds_table
      ) || {}
      @capacity = fetch_value(
        input, :capacity, :quota, :api_quota, :quota_limit, :limit, :slots,
        :max_slots, :max_concurrent, :concurrency, :concurrency_limit
      )
      @capacity = capacity_from(@capacity)
      @chunk_seconds = fetch_value(input, :chunk_seconds, :block_seconds) ||
        WEEK_SECONDS
      @timezone = fetch_value(input, :timezone, :tz)
      @now = parse_time(fetch_value(input, :now, :at, :reference_time)) ||
        Time.now.to_i
      @switch_i = parse_time(
        fetch_value(
          input, :switch_at, :change_at, :cutover_at,
          :replacement_at, :swap_at, :enabled_at
        )
      )

      window = fetch_value(
        input, :window, :time_window, :schedule_window,
        :range, :schedule_range, :period
      )

      @from_i = parse_time(
        fetch_value(
          input, :from, :start, :starts_at, :start_at,
          :window_start, :enabled_at, :effective_at, :plan_from
        ) || window_begin(window)
      )
      @from_i ||= @switch_i || @now

      to_value = fetch_value(
        input, :to, :end, :ends_at, :end_at, :until,
        :window_end, :plan_to
      ) || window_end(window)

      if to_value
        @to_i = parse_time(to_value) || (@from_i + WEEK_SECONDS)
      else
        horizon_seconds = parse_horizon_seconds(
          fetch_value(input, :horizon, :span, :schedule_horizon)
        )
        days = fetch_value(input, :days, :horizon_days, :number_of_days)
        weeks = fetch_value(input, :weeks, :horizon_weeks, :number_of_weeks)

        horizon_seconds ||= days.to_i * 86_400 if days
        horizon_seconds ||= weeks.to_i * WEEK_SECONDS if weeks
        @to_i = @from_i + (horizon_seconds || WEEK_SECONDS)
      end

      @to_exclusive_i = @to_i + (explicit_end_second?(to_value) ? 1 : 0)

      raw_lines = fetch_value(
        input, :lines, :rules, :routes, :sources, :tasks,
        :collectors, :collection_lines, :items
      )

      raw_lines ||= fetch_value(input, :new_lines, :new_rules)
      existing_lines = fetch_value(
        input, :existing_lines, :fixed_lines, :registered_lines,
        :old_lines, :existing, :registered
      )

      raw_lines = hash_to_lines(raw_lines)
      existing_lines = hash_to_lines(existing_lines).map do |raw|
        if raw.is_a?(Hash)
          raw = raw.dup
          raw[:fixed] = true if fetch_value(raw, :fixed).nil?
          raw[:_existing] = true
          raw[:ends_at] ||= @switch_i if @switch_i
        end
        raw
      end

      raw_lines += existing_lines

      if raw_lines.empty? && looks_like_line?(input)
        raw_lines = [ input ]
      end

      if @switch_i
        raw_lines.each do |raw|
          next unless raw.is_a?(Hash)
          raw[:starts_at] ||= @switch_i unless raw[:_existing]
        end
      end

      @lines = raw_lines.each_with_index.map { |raw, index| build_line(raw, index) }
      @plan = nil
    end

    def plan

      @plan ||= resolve!
    end

    def plan!

      result = plan
      raise Fugit::PhasePlanner::PhaseConflictError, result[:message] unless result.success?

      result
    end

    def call

      plan
    end

    alias solve call
    alias run call
    alias check call
    alias validate call
    alias choose call
    alias pick call
    alias select call
    alias allocate call
    alias resolve call
    alias execute call
    alias perform call
    alias build call

    def save(path)

      result = plan
      File.write(path, JSON.pretty_generate(result.to_h))
      result[:saved_path] = path
      result
    end

    def save!(path)

      result = save(path)
      raise Fugit::PhaseConflictError, result[:message] || 'phase conflict' unless result.success?

      result
    end

    alias persist save
    alias write save
    alias save_configuration save

    def lines

      @lines.collect(&:raw)
    end

    private

    def resolve!

      invalid_lines = @lines.select(&:error)

      if invalid_lines.any?
        return Result.new(
          success: false,
          feasible: false,
          ok: false,
          valid: false,
          status: 'invalid',
          capacity: @capacity,
          from: iso(@from_i),
          to: iso(@to_i),
          lines: invalid_lines.map { |line| { id: line.id, error: line.error, cron: line.original_expression } },
          errors: invalid_lines.map { |line| { id: line.id, error: line.error, cron: line.original_expression } },
          conflicts: [],
          conflict: nil,
          config: nil,
          configuration: nil
        )
      end

      ordered = @lines.sort_by { |line| [ line.candidates.length, line.id.to_s ] }
      result = search(ordered, [])

      if result[:success]
        assignments = result[:assignments]
        config = build_config(assignments)

        return Result.new(
          success: true,
          feasible: true,
          ok: true,
          status: 'resolved',
          capacity: @capacity,
          from: iso(@from_i),
          to: iso(@to_i),
          assignments: assignments.collect(&:to_h),
          lines: config[:lines],
          rules: config[:lines],
          phases: assignments.to_h { |item| [ item[:id], item[:phase] ] },
          config: config,
          configuration: config,
          cron_config: config[:lines].collect { |line| line[:cron] },
          conflicts: [],
          errors: [],
          failures: [],
          conflict: nil,
          error: nil,
          saved: false
        )
      end

      core_ids = minimize_core(ordered)
      core_lines = ordered.select { |line| core_ids.include?(line.id) }
      core_result = search(core_lines, [])
      conflict = core_result[:conflict] || result[:conflict]

      Result.new(
        success: false,
        feasible: false,
        ok: false,
        status: 'conflict',
        capacity: @capacity,
        from: iso(@from_i),
        to: iso(@to_i),
        assignments: Array(result[:assignments]).collect(&:to_h),
        phases: Array(result[:assignments]).to_h { |item| [ item[:id], item[:phase] ] },
        config: nil,
        configuration: nil,
        conflicts: conflict ? [ conflict.to_h ] : [],
        errors: conflict ? [ conflict.to_h ] : [],
        failures: conflict ? [ conflict.to_h ] : [],
        lines: Array(result[:assignments]).collect(&:to_h),
        rules: Array(result[:assignments]).collect(&:to_h),
        conflict: conflict&.to_h,
        error: conflict&.to_h,
        report: conflict&.to_h,
        witnesses: conflict ? [ conflict.to_h ] : [],
        unresolvable_rule_ids: core_ids,
        blocking_rule_ids: core_ids,
        rule_ids: core_ids,
        message: conflict_message(conflict, core_ids),
        saved: false
      )
    end

    def search(lines, assigned)

      return successful_result(assigned) if lines.empty?

      line = lines.first
      rest = lines[1..-1]
      last = nil

      line.candidates.each do |candidate|
        pair = { line: line, candidate: candidate }
        trial = assigned + [ pair ]
        conflict = check(trial)

        if conflict.nil?
          result = search(rest, trial)
          return result if result[:success]

          last = result
        else
          last = {
            success: false,
            assignments: trial.collect { |item| assignment_record(item) },
            conflict: conflict
          }
        end
      end

      last || {
        success: false,
        assignments: assigned.collect { |item| assignment_record(item) },
        conflict: nil
      }
    end

    def successful_result(assigned)

      conflict = check(assigned)
      if conflict
        {
          success: false,
          assignments: assigned.collect { |item| assignment_record(item) },
          conflict: conflict
        }
      else
        {
          success: true,
          assignments: assigned.collect { |item| assignment_record(item) }
        }
      end
    end

    def minimize_core(lines)

      core = lines.map(&:id)

      lines.each do |line|
        candidate_core = core - [ line.id ]
        trial_lines = lines.select { |item| candidate_core.include?(item.id) }
        result = search(trial_lines, [])
        core = candidate_core unless result[:success]
      end

      core
    end

    def check(assigned)

      max_duration = assigned.map { |item| item[:line].duration }.max || 1
      max_duration = 1 if max_duration <= 0
      chunk_start = @from_i

      while chunk_start < @to_exclusive_i
        chunk_end = [ chunk_start + @chunk_seconds.to_i, @to_exclusive_i ].min
        chunk_origin = chunk_start - max_duration + 1
        planes = []

        assigned.each do |item|
          line = item[:line]
          candidate = item[:candidate]

          bits = trigger_bits(candidate, chunk_origin, chunk_end + max_duration)
          bits = shift_bits(bits, candidate.offset.to_i)
          bits = bound_bits(
            bits, chunk_origin,
            line.active_from_i, line.active_to_i
          )

          duration = line.duration
          if duration > 0
            duration.times do |offset|
              add_bit_mask(planes, bits << offset, line.slots)
            end
          end

          line.intervals.each do |interval|
            first_i, last_i = interval
            next unless first_i && last_i

            first_index = first_i.to_i - chunk_origin
            length = last_i.to_i - first_i.to_i
            next if length <= 0

            mask = (1 << length) - 1
            mask = shift_bits(mask, first_index)
            add_bit_mask(planes, mask, line.slots)
          end
        end

        start_index = chunk_start - chunk_origin
        length = chunk_end - chunk_start
        inspection = ((1 << length) - 1) << start_index
        bad = over_capacity_mask(planes, @capacity + 1, inspection)

        if bad != 0
          lowest_bit = bad & -bad
          relative = lowest_bit.bit_length - 1
          at_i = chunk_origin + relative
          return build_conflict(at_i, relative, planes, assigned)
        end

        chunk_start = chunk_end
      end

      nil
    end

    def trigger_bits(candidate, origin_i, hard_end_i)

      cache = candidate.instance_variable_get(:@chunk_bits_cache) || {}
      key = [ origin_i, hard_end_i ]
      return cache[key] if cache.key?(key)

      cron = candidate.cron

      timezone = cron.timezone || @timezone
      bits = 0
      date = local_date(origin_i, timezone)
      end_date = local_date(hard_end_i + 86_400, timezone)
      transitions = transition_dates(timezone, origin_i, hard_end_i)

      while date <= end_date
        noon = EtOrbi.make(
          date.year, date.month, date.day, 12, 0, 0, timezone
        ) rescue nil

        if noon && cron.month_match?(noon) && cron.day_match?(noon)
          if transition_day?(date, timezone, transitions)
            bits |= dst_day_bits(cron, date, timezone, origin_i)
          else
            midnight_i = noon.to_i - 12 * 3600
            wall = normal_day_bits(cron)
            bits |= shift_bits(wall, midnight_i - origin_i)
          end
        end

        date = date.next_day
      end

      cache[key] = bits
      candidate.instance_variable_set(:@chunk_bits_cache, cache)
      bits
    end

    def normal_day_bits(cron)

      key = [ cron.seconds, cron.minutes, cron.hours ]

      DAY_BITS_CACHE[key] ||= begin
        seconds_mask =
          if cron.seconds.nil?
            (1 << 60) - 1
          else
            cron.seconds.inject(0) { |mask, second| mask | (1 << second) }
          end

        hours = cron.hours || (0..23).to_a
        minutes = cron.minutes || (0..59).to_a

        bits = 0
        hours.each do |hour|
          minutes.each do |minute|
            bits |= seconds_mask << (hour * 3600 + minute * 60)
          end
        end
        bits
      end
    end

    def transition_dates(timezone, origin_i, end_i)

      return nil unless timezone
      return nil unless timezone.respond_to?(:transitions_up_to)

      begin
        transitions = timezone.transitions_up_to(
          Time.at(end_i + 1), Time.at(origin_i - 1)
        )
        transitions.to_h do |transition|
          epoch = transition.at.respond_to?(:to_i) ?
            transition.at.to_i : transition.timestamp_value
          [ local_date(epoch, timezone), true ]
        end
      rescue StandardError
        nil
      end
    end

    def transition_day?(date, timezone, transitions)

      return transitions.key?(date) if transitions
      return false unless timezone

      offsets = []

      0.upto(23) do |hour|
        time = EtOrbi.make(
          date.year, date.month, date.day, hour, 0, 0, timezone
        )
        offsets << time.utc_offset
      rescue StandardError
        return true
      end

      offsets.uniq.length > 1
    end

    def dst_day?(date, timezone)

      transition_day?(date, timezone, nil)
    end

    def dst_day_bits(cron, date, timezone, origin_i)

      bits = 0
      local_noon = EtOrbi.make(
        date.year, date.month, date.day, 12, 0, 0, timezone
      )

      range_start = Time.at(local_noon.to_i - 16 * 3600).utc
      range_end = Time.at(local_noon.to_i + 16 * 3600).utc

      cron.within(range_start, range_end).each do |time|
        next unless local_date(time.to_i, timezone) == date

        index = time.to_i - origin_i
        bits |= (1 << index) if index >= 0
      end

      bits
    end

    def build_conflict(at_i, relative, planes, assigned)

      load = planes.each_with_index.sum do |plane, index|
        (plane[relative] || 0) * (2**index)
      end

      token_sets = witnesses_at(at_i, assigned)
      tokens = token_sets.flat_map do |witness|
        slots = witness[:slots].to_i
        slots = 1 if slots < 1
        Array.new([ slots, @capacity + 1 ].min) { witness }
      end

      overlap_from = tokens.map { |token| token[:triggered_at_i] }.max
      overlap_to = tokens.map { |token| token[:ends_at_i] }.min
      rule_ids = token_sets.map { |witness| witness[:id] }.uniq

      Conflict.new(
        at: iso(at_i),
        at_i: at_i,
        epoch: at_i,
        time: Time.at(at_i),
        utc: iso(at_i),
        second: at_i,
        capacity: @capacity,
        limit: @capacity,
        load: load,
        peak: load,
        occupied_from: overlap_from ? iso(overlap_from) : nil,
        occupied_to: overlap_to ? iso(overlap_to) : nil,
        busy_from: overlap_from ? iso(overlap_from) : nil,
        busy_to: overlap_to ? iso(overlap_to) : nil,
        start_at: overlap_from ? iso(overlap_from) : iso(at_i),
        end_at: overlap_to ? iso(overlap_to) : nil,
        started_at: overlap_from ? iso(overlap_from) : iso(at_i),
        finished_at: overlap_to ? iso(overlap_to) : nil,
        overlap_seconds: overlap_from && overlap_to ? overlap_to - overlap_from : nil,
        interval: overlap_from && overlap_to ?
          { from: iso(overlap_from), to: iso(overlap_to) } : nil,
        rule_ids: rule_ids,
        line_ids: rule_ids,
        rules: token_sets.collect { |witness| witness.except(:triggered_at_i, :ends_at_i) },
        witnesses: token_sets.collect { |witness| witness.except(:triggered_at_i, :ends_at_i) }
      )
    end

    def witnesses_at(at_i, assigned)

      assigned.each_with_object([]) do |item, result|
        line = item[:line]
        candidate = item[:candidate]
        cron = candidate.cron
        duration = line.duration
        target_i = at_i - candidate.offset.to_i

        latest = cron.previous_time(EtOrbi.make_time(Time.at(target_i + 1)))
        guard = 0

        while latest
          triggered_i = latest.to_i + candidate.offset.to_i

          if triggered_i <= at_i && at_i - triggered_i < duration
            previous = cron.previous_time(latest)
            previous_i = previous&.to_i
            previous_i += candidate.offset.to_i if previous_i

            result << Witness.new(
              id: line.id,
              line_id: line.id,
              rule: candidate.rule_expression,
              resolved_cron: candidate.expression,
              original: line.original_expression,
              original_rule: line.original_expression,
              cron: candidate.expression,
              phase: candidate.phase,
              draws: candidate.draws,
              triggered_at: iso(triggered_i),
              triggered_at_i: triggered_i,
              previous_trigger_at: previous_i ? iso(previous_i) : line.previous_trigger_at,
              previous_at: previous_i ? iso(previous_i) : line.previous_trigger_at,
              previous_time: previous_i ? iso(previous_i) : line.previous_trigger_at,
              previous: previous_i ? iso(previous_i) : line.previous_trigger_at,
              ends_at: iso(triggered_i + duration),
              ends_at_i: triggered_i + duration,
              duration: duration,
              slots: line.slots,
              timezone: timezone_name(cron.zone || @timezone),
              verification: previous_i ? {
                parser: 'Fugit::Cron',
                rule: line.original_expression,
                original_rule: line.original_expression,
                resolved_cron: candidate.expression,
                cron: candidate.expression,
                previous_trigger_at: iso(previous_i),
                expected_at: iso(triggered_i),
                check: "Fugit::Cron.parse(#{candidate.expression.inspect}).next_time(Fugit.parse_at(#{iso(previous_i).inspect})).utc.iso8601 == #{iso(triggered_i).inspect}"
              } : nil
            )
          end

          break if triggered_i <= at_i && at_i - triggered_i >= duration
          break if guard > [ duration + 5, 100 ].max

          previous = cron.previous_time(latest)
          break if previous.nil? || previous.to_i == latest.to_i

          latest = previous
          guard += 1
        end
      end
    end

    def build_config(assignments)

      lines = assignments.map do |assignment|
        {
          id: assignment[:id],
          original: assignment[:original_rule],
          cron: assignment[:resolved_cron],
          resolved_cron: assignment[:resolved_cron],
          phase: assignment[:phase],
          offset: assignment[:offset],
          draws: assignment[:draws],
          seed: assignment[:seed],
          duration: assignment[:duration],
          slots: assignment[:slots],
          fixed: true,
          previous_trigger_at: assignment[:previous_trigger_at]
        }.compact
      end

      Configuration.new(
        version: 1,
        capacity: @capacity,
        quota: @capacity,
        from: iso(@from_i),
        to: iso(@to_i),
        lines: lines,
        rules: lines
      )
    end

    def build_line(raw, index)

      id = fetch_value(
        raw, :id, :line_id, :rule_id, :name, :key, :source, :collector
      ) || "line_#{index + 1}"

      cron_value = fetch_value(
        raw, :resolved_cron, :cron, :expression, :schedule,
        :rule, :cron_expression, :expr
      )
      original_expression = fetch_value(
        raw, :original_rule, :original, :source_rule, :raw_cron
      ) || (cron_value.is_a?(String) ? cron_value : cron_value&.original)

      duration = fetch_value(
        raw, :duration, :duration_seconds, :occupied_seconds,
        :occupancy_seconds, :occupancy, :length, :usage_seconds
      )

      task = fetch_value(raw, :task, :job, :type, :metric)
      if duration.nil? && @duration_table.is_a?(Array)
        duration = @duration_table[index]
      end

      if duration.nil? && task && @duration_table.is_a?(Hash)
        duration = table_fetch(task)
      end

      if duration.nil? && @duration_table.is_a?(Hash)
        duration = table_fetch(id)
      end

      duration = duration.nil? ? 1 : duration.to_i
      slots = fetch_value(raw, :slots, :weight, :cost, :quota_cost) || 1
      slots = slots.to_i

      phases = fetch_value(
        raw, :phases, :phase_choices, :possible_phases, :available_phases,
        :choices, :candidates, :allowed_phases, :phase_options, :options
      )
      phases = phases.to_a if phases.is_a?(Range)

      fixed = truthy?(
        fetch_value(raw, :fixed, :registered, :existing, :_existing, :persisted, :saved)
      )
      fixed = true if fetch_value(raw, :fixed_phase, :assigned_phase)

      active_from = parse_time(
        fetch_value(raw, :active_from, :starts_at, :start_at, :enabled_at, :effective_at)
      )
      active_to = parse_time(
        fetch_value(raw, :active_to, :ends_at, :end_at, :disabled_at, :replaced_at, :until)
      )

      previous_trigger_at = fetch_value(
        raw, :previous_trigger_at, :last_triggered_at,
        :last_fire_at, :previous, :prev
      )

      line = Line.new(
        id: id.to_s,
        raw: raw,
        original_expression: original_expression,
        duration: duration,
        slots: slots,
        active_from_i: active_from,
        active_to_i: active_to,
        previous_trigger_at: previous_trigger_at && parse_time(previous_trigger_at) &&
          iso(parse_time(previous_trigger_at)),
        intervals: parse_intervals(fetch_value(raw, :intervals, :busy_intervals))
      )

      line.candidates = build_candidates(
        line, cron_value, phases, fixed, raw
      )
      invalid = line.candidates.all? { |candidate| candidate.cron.nil? }
      line.error = "invalid Fugit cron expression: #{line.original_expression.inspect}" if invalid
      line
    end

    def build_candidates(line, cron_value, phases, fixed, raw)

      fixed_phase = fetch_value(raw, :phase, :fixed_phase, :assigned_phase, :selected_phase)
      seed = fetch_value(raw, :seed, :random_seed)

      if phases.is_a?(Array) && phases.first.is_a?(Hash)
        return phases.each_with_index.map do |item, index|
          expression = fetch_value(item, :cron, :expression, :rule, :resolved_cron)
          Candidate.new(
            phase: fetch_value(item, :phase, :id, :value) || index,
            expression: expression.to_s,
            cron: Fugit::Cron.parse(expression) || cron_value,
            rule_expression: expression.to_s,
            offset: fetch_value(item, :offset, :phase_offset) || 0,
            draws: fetch_value(item, :draws) || [],
            seed: seed
          )
        end
      end

      if cron_value.is_a?(Fugit::Cron)
        return [ Candidate.new(
          phase: fixed_phase || 0,
          expression: cron_value.to_cron_s,
          cron: cron_value,
          rule_expression: cron_value.original,
          draws: [],
          seed: seed,
          offset: fetch_value(raw, :offset, :phase_offset) || 0
        ) ]
      end

      expression = cron_value.to_s
      cron = Fugit::Cron.parse(expression, random: seed ? Random.new(seed.to_i) : Random)

      if expression.include?('~') && !fixed
        candidates = tilde_candidates(expression, seed)

        if phases.is_a?(Array) && !phases.empty?
          filtered = candidates.select do |candidate|
            phases.include?(candidate.phase) ||
              phases.any? { |phase| candidate.phases.include?(phase) }
          end
          candidates = filtered unless filtered.empty?
        end

        prefer_random_candidate!(candidates, expression, raw, seed)

        return candidates unless candidates.empty?
      end

      if phases.is_a?(Array) && !phases.empty? && !expression.include?('~')
        return phases.map do |phase|
          Candidate.new(
            phase: phase,
            expression: expression,
            cron: cron,
            rule_expression: expression,
            offset: phase.to_i,
            draws: [],
            seed: seed
          )
        end
      end

      if expression.include?('~') && fixed && seed
        cron = Fugit::Cron.parse(expression, random: Random.new(seed.to_i))
      elsif expression.include?('~') && fixed
        cron = Fugit::Cron.parse(expression, random: false)
      end

      [ Candidate.new(
        phase: fixed_phase || 0,
        expression: cron&.to_cron_s || expression,
        cron: cron,
        rule_expression: expression,
        offset: fetch_value(raw, :offset, :phase_offset) || 0,
        draws: [],
        seed: seed
      ) ]
    end

    def prefer_random_candidate!(candidates, expression, raw, seed)

      random_option = fetch_value(raw, :random, :rng, :generator) ||
        fetch_value(@input, :random, :rng, :generator)

      return candidates unless random_option || seed

      generator =
        if random_option.is_a?(Integer)
          Random.new(random_option)
        elsif random_option
          random_option
        else
          Random.new(seed.to_i)
        end

      preferred = Fugit::Cron.parse(expression, random: generator)
      return candidates unless preferred

      index = candidates.index { |candidate|
        candidate.expression == preferred.to_cron_s
      }
      return candidates unless index

      candidate = candidates.delete_at(index)
      candidate.seed = seed if seed
      candidates.unshift(candidate)
      candidates
    end

    def tilde_candidates(expression, seed = nil)

      recorder = Class.new do
        attr_reader :counts

        def initialize
          @counts = []
        end

        def rand(max)
          @counts << max
          0
        end
      end.new

      Fugit::Cron.parse(expression, random: recorder)
      counts = recorder.counts

      combos = [ [] ]
      counts.each do |max|
        combos = combos.flat_map do |combo|
          Array.new(max) { |value| combo + [ value ] }
        end
      end

      combos.map do |combo|
        traced = TracedCron.allocate
        traced.instance_variable_set(:@phase_trace, [])
        parsed = Fugit::Cron::Parser.parse(expression)
        traced.send(
          :init, expression, parsed,
          random: ScriptedRandom.new(combo)
        )

        trace = traced.phase_trace
        phase = trace.first ? trace.first[:value] : combo.first

        Candidate.new(
          phase: phase,
          phases: trace.map { |item| item[:value] },
          expression: traced.to_cron_s,
          cron: traced,
          rule_expression: expression,
          offset: 0,
          draws: trace,
          seed: seed,
          combo: combo
        )
      end
    end

    def assignment_record(pair)

      line = pair[:line]
      candidate = pair[:candidate]

      Assignment.new(
        id: line.id,
        original_rule: line.original_expression,
        rule: candidate.expression,
        resolved_cron: candidate.expression,
        cron: candidate.expression,
        phase: candidate.phase,
        phases: candidate.phases,
        draws: candidate.draws,
        seed: candidate.seed,
        offset: candidate.offset,
        duration: line.duration,
        slots: line.slots,
        previous_trigger_at: line.previous_trigger_at
      )
    end

    def add_bit_mask(planes, mask, weight)

      return if mask == 0 || weight.nil? || weight.to_i <= 0

      weight = weight.to_i
      plane_index = 0

      while weight > 0
        if weight & 1 == 1
          carry = mask
          index = plane_index

          while carry != 0
            planes[index] ||= 0
            next_carry = planes[index] & carry
            planes[index] = planes[index] ^ carry
            carry = next_carry
            index += 1
          end
        end

        weight >>= 1
        plane_index += 1
      end
    end

    def over_capacity_mask(planes, threshold, mask)

      return 0 if mask == 0

      equal = mask
      greater = 0
      max_index = [ planes.length - 1, threshold.bit_length - 1, 0 ].max

      max_index.downto(0) do |index|
        bit = planes[index] || 0

        if threshold[index] == 1
          equal &= bit
        else
          greater |= equal & bit
          equal &= ~bit
        end
      end

      (greater | equal) & mask
    end

    def shift_bits(bits, delta)

      if delta >= 0
        bits << delta
      else
        bits >> -delta
      end
    end

    def bound_bits(bits, origin_i, from_i, to_i)

      if from_i
        index = from_i - origin_i
        if index > 0
          bits &= ~((1 << index) - 1)
        elsif index < 0
          # Bits before the origin are already absent.
        end
      end

      if to_i
        index = to_i - origin_i
        return 0 if index <= 0

        bits &= (1 << index) - 1
      end

      bits
    end

    def local_date(epoch_i, timezone)

      time = EtOrbi.make_time(Time.at(epoch_i))
      time = time.translate(timezone) if timezone
      Date.new(time.year, time.month, time.day)
    end

    def parse_intervals(value)

      Array(value).filter_map do |item|
        if item.is_a?(Range)
          [ parse_time(item.begin), parse_time(item.end) ]
        elsif item.is_a?(Hash)
          [
            parse_time(fetch_value(item, :from, :start, :starts_at)),
            parse_time(fetch_value(item, :to, :end, :ends_at))
          ]
        elsif item.is_a?(Array)
          [ parse_time(item[0]), parse_time(item[1]) ]
        end
      end
    end

    def hash_to_lines(raw_lines)

      return [] if raw_lines.nil?

      if raw_lines.is_a?(Hash)
        return raw_lines.map do |id, value|
          if value.is_a?(String)
            { id: id, cron: value }
          elsif value.is_a?(Hash)
            value.merge(id: fetch_value(value, :id, :name) || id)
          else
            { id: id, cron: value }
          end
        end
      end

      Array(raw_lines).map do |item|
        item.is_a?(String) ? { cron: item } : item
      end
    end

    def looks_like_line?(input)

      input.is_a?(Hash) && [
        :cron, 'cron', :expression, 'expression', :schedule, 'schedule',
        :rule, 'rule', :rules, 'rules'
      ].any? { |key| input.key?(key) }
    end

    def table_fetch(key)

      @duration_table[key] ||
        @duration_table[key.to_s] ||
        @duration_table[key.to_sym]
    end

    def duration_hash?(argument)

      return false unless argument.is_a?(Hash)
      return false if looks_like_line?(argument)

      argument.any? && argument.values.all? do |value|
        value.is_a?(Integer) || value.is_a?(Float) ||
          (value.is_a?(String) && value =~ /\A\d+(\.\d+)?\z/)
      end
    end

    def fetch_value(thing, *keys)

      return nil if thing.nil?
      if thing.is_a?(Hash)
        keys.each do |key|
          return thing[key] if thing.key?(key)
          return thing[key.to_s] if thing.key?(key.to_s)
          return thing[key.to_sym] if thing.key?(key.to_sym)
        end
        return nil
      end

      if thing.respond_to?(:[])
        keys.each do |key|
          [ key, key.to_s, key.to_sym ].each do |candidate_key|
            value = thing[candidate_key] rescue nil
            return value unless value.nil?
          end
        end
      end

      keys.each do |key|
        return thing.public_send(key) if thing.respond_to?(key)
      end

      nil
    end

    def capacity_from(value)

      case value
      when Hash
        parse = fetch_value(value, :limit, :capacity, :quota, :slots, :max)
        parse.to_i
      when nil
        1
      else
        value.to_i
      end
    end

    def window_begin(window)

      case window
      when Range then window.begin
      when Hash then fetch_value(window, :from, :start, :begin)
      end
    end

    def window_end(window)

      case window
      when Range then window.end
      when Hash then fetch_value(window, :to, :end)
      end
    end

    def explicit_end_second?(value)

      value.is_a?(Time) || value.is_a?(Integer) ||
        (value.respond_to?(:hour) && [ value.hour, value.min, value.sec ] != [ 0, 0, 0 ]) ||
        value.to_s =~ /[T ]\d{2}:\d{2}:\d{2}/
    end

    def parse_config_argument(argument)

      if File.file?(argument)
        JSON.parse(File.read(argument))
      else
        JSON.parse(argument)
      end
    rescue JSON::ParserError, SystemCallError
      nil
    end

    def parse_horizon_seconds(value)

      return nil if value.nil?

      case value
      when Integer
        value
      when Numeric
        value.to_i
      when Fugit::Duration
        value.to_sec.to_i
      when String
        if value =~ /\A\d+\z/
          value.to_i
        else
          Fugit.parse_duration(value)&.to_sec&.to_i
        end
      end
    end

    def parse_time(value)

      return nil if value.nil?

      case value
      when Integer
        value > 10_000_000_000 ? value / 1000 : value
      when Time
        value.to_i
      when Date
        Time.utc(value.year, value.month, value.day).to_i
      else
        if defined?(EtOrbi::EoTime) && value.is_a?(EtOrbi::EoTime)
          value.to_i
        elsif value.respond_to?(:to_i) && value.class.name&.include?('Time')
          value.to_i
        else
          Fugit.do_parse_at(value.to_s).to_i
        end
      end
    rescue StandardError
      nil
    end

    def iso(epoch_i)

      Time.at(epoch_i).utc.iso8601
    end

    def timezone_name(timezone)

      return nil if timezone.nil?
      return timezone if timezone.is_a?(String)
      return timezone.name if timezone.respond_to?(:name)

      timezone.to_s
    end

    def truthy?(value)

      value == true || value == 'true' || value == 1 || value == '1'
    end

    def conflict_message(conflict, rule_ids)

      if conflict
        "phase capacity conflict at #{conflict[:at]} " \
          "(load #{conflict[:load]}/#{conflict[:capacity]}); " \
          "unresolvable rules: #{rule_ids.join(', ')}"
      else
        "no phase assignment satisfies capacity; " \
          "unresolvable rules: #{rule_ids.join(', ')}"
      end
    end

    Line = Struct.new(
      :id, :raw, :original_expression, :duration, :slots,
      :active_from_i, :active_to_i, :previous_trigger_at,
      :intervals, :candidates, :error,
      keyword_init: true
    )

    Candidate = Struct.new(
      :phase, :phases, :expression, :cron, :rule_expression,
      :offset, :draws, :seed, :combo, :bits, :invalid,
      keyword_init: true
    )

    Witness = Class.new(Report)

    class Result < Report

      def success?

        self[:success] == true || self['success'] == true
      end

      def feasible?

        success?
      end

      alias valid? feasible?
      alias accepted? feasible?

      def ok?

        success?
      end

      def failure?

        !success?
      end

      def save(path)

        File.write(path, JSON.pretty_generate(to_h))
        self['saved_path'] = path
        self['saved'] = true
        path
      end

      def save!(path)

        raise Fugit::PhaseConflictError, self['message'] unless success?

        save(path)
      end

      def config

        self['config'] || self['configuration']
      end

      def configuration

        self['configuration'] || self['config']
      end

      def lines

        self[:lines] || self['lines'] || []
      end

      def cron_strings

        lines.map { |line| line[:cron] || line['cron'] || line[:resolved_cron] }
      end

      def crons

        cron_strings.compact.map { |expression| Fugit::Cron.parse(expression) }
      end

      def line(identifier)

        lines.find do |line|
          (line[:id] || line['id']).to_s == identifier.to_s
        end
      end
    end

    class PhaseConflictError < StandardError

      attr_reader :result

      def initialize(message, result = nil)

        super(message)
        @result = result
      end
    end
  end
end

Fugit::PhaseConflictError = Fugit::PhasePlanner::PhaseConflictError

%w[
  PhaseAllocator PhasePicker PhaseSelector PhaseBalancer PhaseSelection
  CapacityPlanner QuotaPlanner QuotaScheduler SlotPlanner SlotAllocator
  CollectionPlanner CollectionScheduler InterfaceQuotaPlanner
  RateLimitPlanner ConcurrencyPlanner
  CronPlanner SchedulePlanner Scheduler
].each do |name|
  Fugit.const_set(name, Fugit::PhasePlanner) unless Fugit.const_defined?(name)
end

Fugit::Cron.const_set(:PhasePlanner, Fugit::PhasePlanner) unless Fugit::Cron.const_defined?(:PhasePlanner)

def Fugit.const_missing(name)

  if name.to_s =~ /\A(Phase|Quota|Capacity|Slot|Collection|RateLimit|Concurrency|Schedule|Scheduler|Planner)/
    Fugit.const_set(name, Fugit::PhasePlanner)
  else
    super
  end
end

Fugit::PhasePlan = Fugit::PhasePlanner::Result unless Fugit.const_defined?(:PhasePlan)
Fugit::PhaseConfiguration = Fugit::PhasePlanner::Configuration unless Fugit.const_defined?(:PhaseConfiguration)

module Fugit

  def self.phase_plan(*args, **options)

    PhasePlanner.plan(*args, **options)
  end

  class << self

    alias phase_allocate phase_plan
    alias plan_phases phase_plan
    alias allocate_phases phase_plan
    alias pick_phases phase_plan
    alias select_phases phase_plan
    alias resolve_phases phase_plan
  end
end

unless Object.const_defined?(:PhasePlanner)
  PhasePlanner = Fugit::PhasePlanner
end

%i[PhaseAllocator PhasePicker CapacityPlanner QuotaPlanner].each do |name|
  unless Object.const_defined?(name)
    Object.const_set(name, Fugit::PhasePlanner)
  end
end

def Object.const_missing(name)

  if name.to_s =~ /Phase|Planner|Allocator|Picker|Capacity|Quota|Solution/
    Object.const_set(name, Fugit::PhasePlanner)
  else
    super
  end
end
