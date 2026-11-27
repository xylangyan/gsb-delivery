# frozen_string_literal: true

require_relative 'int_seq'
require_relative 'table'

module Errata
  # One approved errata batch.  Selecting it applies the whole batch; leaving
  # it out keeps every row it touches untouched.  One approval may cover
  # several base tables through +targets+ keyed by table id.
  class Batch
    attr_reader :id, :cost, :targets

    def initialize(id:, cost:, targets:)
      @id = id
      @cost = Integer(cost)
      @targets = build_targets(targets)
      raise ArgumentError, "batch #{id} has no targets" if @targets.empty?
      raise ArgumentError, "batch #{id} cost must be non-negative" if @cost.negative?
    end

    def touches_table?(table_id)
      @targets.key?(table_id)
    end

    # Symbolic position set inside one table.
    def positions_in(table_id)
      target = @targets.fetch(table_id)
      target[:positions]
    end

    def new_category_for(table_id)
      @targets.fetch(table_id)[:new_category]
    end

    def material_set
      @targets.values.map { |target| target[:materials] }.reduce(:union)
    end

    def touches_material?(material)
      @targets.values.any? { |target| target[:materials].include?(material) }
    end

    # Build targets from a compact spec.  Accepted shapes per table:
    #   { "materials" => [first, step, count], "positions" => [..same..],
    #     "new_category" => "乙" }
    # or explicit integer arrays for small hand-written tables.
    def build_targets(raw_targets)
      raw_targets.each_with_object({}) do |(table_id, spec), memo|
        materials = build_seq(spec['materials'] || spec[:materials])
        positions =
          if spec['positions'] || spec[:positions]
            build_seq(spec['positions'] || spec[:positions])
          else
            materials
          end
        memo[table_id] = {
          materials: materials,
          positions: positions,
          new_category: normalize(spec['new_category'] || spec[:new_category])
        }
      end
    end

    def build_seq(value)
      return value if value.is_a?(IntSeq)
      return IntSeq.from_a(value) if value.is_a?(Array)

      first = Integer(value['first'] || value[:first])
      step = Integer(value['step'] || value[:step] || 1)
      count = Integer(value['count'] || value[:count])
      IntSeq.seq(first, step, count)
    end

    def normalize(value)
      Errata.normalize_value(value)
    end
  end

  # The fixed night order of batches, with last-batch-wins semantics for any
  # material number reached by several approvals.
  class Night
    attr_reader :batches

    def initialize(batches)
      @batches = batches
    end

    def by_id
      @batches.each_with_object({}) { |batch, memo| memo[batch.id] = batch }
    end
  end
end
