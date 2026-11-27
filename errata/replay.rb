# frozen_string_literal: true

require_relative 'int_seq'
require_relative 'table'
require_relative 'batch'

module Errata
  # Replays a work order against tonight's tables the way Daru's
  # CategoricalIndex does: #pos, #subset and #at, with numeric arguments first
  # interpreted as categories and only then as positions.  Every step keeps a
  # trace noting at which layer a number became a row position.
  class Replay
    # One named output of the replay.
    class Output
      attr_reader :name, :kind, :value, :tickets, :trace

      def initialize(name:, kind:, value:, tickets:, trace:)
        @name = name
        @kind = kind            # :scalar or :table
        @value = value          # category for scalar, category array for table
        @tickets = tickets      # original material numbers, duplicates kept
        @trace = trace
      end

      def scalar?
        @kind == :scalar
      end
    end

    attr_reader :outputs, :traces

    def initialize(views, work_orders)
      @views = views
      @work_orders = work_orders
      @outputs = {}
      @traces = []
    end

    def run
      @work_orders.each do |step|
        @traces << replay_step(step)
      end
      self
    end

    def output(name)
      @outputs.fetch(name)
    end

    private

    def replay_step(step)
      name = step['name'] || step[:name]
      table = resolve_table(step['table'] || step[:table])
      op = (step['op'] || step[:op]).to_s
      args = resolve_args(step['args'] || step[:args] || [])
      trace = {
        name: name, op: op, table: table.id, layer: table.layer,
        args: args, interpretations: [], branch: nil, error: nil
      }

      case op
      when 'pos'
        positions = daru_pos(table, args, trace)
        @outputs[name] = scalar_or_positions(name, positions, table, trace)
      when 'subset'
        handle_subset(step, name, table, args, trace)
      when 'at'
        handle_at(name, table, args, trace)
      else
        raise ArgumentError, "unknown replay op #{op}"
      end
      trace
    end

    def handle_subset(step, name, table, args, trace)
      positions = daru_pos(table, args, trace)
      if positions.is_a?(Integer)
        raise OldLibraryError,
              "subset of #{table.id} resolved to a single position (#{positions})"
      end
      sub = build_subtable(name, table, positions)
      @outputs[name] = Output.new(
        name: name, kind: :table, value: sub.categories,
        tickets: sub.tickets, trace: trace
      )
    rescue OldLibraryError, IndexError => e
      trace[:error] = "#{e.class}: #{e.message}"
      fallback = step['fallback_at'] || step[:fallback_at]
      raise if fallback.nil?

      trace[:branch] = 'old_library'
      at_args = resolve_args(fallback)
      at_positions = preprocess_at(at_args, table.size)
      validate_at(at_positions, table)
      value = table.raw_category_at(at_positions)
      ticket = table.material_at(at_positions)
      trace[:fallback_at] = at_args
      trace[:result] = value
      @outputs[name] = Output.new(
        name: name, kind: :scalar, value: value,
        tickets: [ticket], trace: trace
      )
    end

    def handle_at(name, table, args, trace)
      at_positions = preprocess_at(args, table.size)
      validate_at(at_positions, table)
      if at_positions.is_a?(Integer)
        value = table.raw_category_at(at_positions)
        ticket = table.material_at(at_positions)
        trace[:result] = value
        @outputs[name] = Output.new(
          name: name, kind: :scalar, value: value,
          tickets: [ticket], trace: trace
        )
      else
        sub = build_subtable(name, table, at_positions)
        @outputs[name] = Output.new(
          name: name, kind: :table, value: sub.categories,
          tickets: sub.tickets, trace: trace
        )
      end
    end

    # Daru::CategoricalIndex#pos semantics.
    def daru_pos(table, args, trace)
      contribs = args.map do |arg|
        if table.displays?(arg)
          positions = table.positions_for(arg)
          trace[:interpretations] << "#{arg.inspect}: category on #{table.id} -> positions #{summarize(positions)}"
          positions
        elsif arg.is_a?(Numeric) && arg < table.size
          trace[:interpretations] << "#{arg.inspect}: not a category on #{table.id}, used as row position"
          IntSeq.point(arg)
        else
          raise IndexError,
                "#{arg.inspect} is neither a valid category nor a valid position on #{table.id}"
        end
      end
      total = contribs.sum(&:size)
      flattened = IntSeq.flatten_sort(contribs, expansion_cap)
      return flattened.first if total == 1

      flattened
    end

    def scalar_or_positions(name, positions, table, trace)
      if positions.is_a?(Integer)
        # Daru's #pos returns the bare position number; the category at that
        # position is recorded for the audit but is not the returned value.
        trace[:position] = positions
        trace[:displayed] = table.raw_category_at(positions)
        trace[:number_role] = 'row position at layer %d' % table.layer
        Output.new(name: name, kind: :scalar, value: positions,
                   tickets: [table.material_at(positions)], trace: trace)
      else
        sub = build_subtable(name, table, positions)
        Output.new(name: name, kind: :table, value: sub.categories,
                   tickets: sub.tickets, trace: trace)
      end
    end

    def build_subtable(name, table, positions)
      rows = positions.map do |position|
        [table.material_at(position), table.raw_category_at(position),
         table.respond_to?(:winner_at) ? table.winner_at(position) : nil]
      end
      SubTable.new("#{table.id}>#{name}", rows.map(&:first), rows.map { |row| row[1] },
                   table.layer + 1, rows.map { |row| row[2] })
    end

    # Daru::Index#preprocess_positions semantics.
    def preprocess_at(args, size)
      if args.size == 1
        arg = args.first
        case arg
        when Integer then arg
        when Range then size.times.to_a[arg]
        else raise ArgumentError, 'Unknown position type.'
        end
      else
        args
      end
    end

    def validate_at(positions, table)
      Array(positions).each do |position|
        unless table.in_range?(position)
          raise IndexError, "#{position} is not a valid position on #{table.id}"
        end
      end
    end

    def resolve_table(ref)
      return @views[ref] if @views.key?(ref)

      output = @outputs.fetch(ref)
      raise ArgumentError, "#{ref} is a scalar result, not a table" if output.scalar?

      SubTable.new(ref, output.tickets, output.value, output.trace[:layer] + 1)
    end

    def resolve_args(args)
      args.map do |arg|
        if arg.is_a?(Hash) && (arg.key?('ref') || arg.key?(:ref))
          dereference(arg['ref'] || arg[:ref])
        else
          normalize(arg)
        end
      end
    end

    def dereference(name)
      referenced = @outputs.fetch(name)
      unless referenced.scalar?
        raise ArgumentError, "reference #{name} is a table, not a number"
      end

      referenced.value
    end

    def normalize(value)
      Errata.normalize_value(value)
    end

    def summarize(seq)
      preview = seq.first(8)
      return preview.inspect if seq.size <= 8

      "#{preview.inspect[0...-1]}, ...] (#{seq.size} rows)"
    end

    def expansion_cap
      ENV['ERRATA_EXPAND_CAP']&.then { |value| Integer(value) } || 1_000_000
    end
  end
end
