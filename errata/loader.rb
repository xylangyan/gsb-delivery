# frozen_string_literal: true

require 'json'
require_relative 'value'
require_relative 'int_seq'
require_relative 'table'
require_relative 'batch'
require_relative 'signature'
require_relative 'replay'

module Errata
  # Loads a planning file, builds the symbolic tables/batches and derives row
  # positions for batch targets from material numbers without expanding rows.
  class Loader
    def self.load(path)
      new(File.read(path)).build
    end

    def initialize(text)
      @spec = JSON.parse(text)
    end

    def build
      tables = @spec.fetch('tables').map { |item| build_table(item) }
      table_index = tables.each_with_object({}) { |table, memo| memo[table.id] = table }
      batches = @spec.fetch('batches', []).map { |item| build_batch(item, table_index) }
      night = Night.new(batches)
      signatures = build_signatures
      work_orders = @spec.fetch('work_orders', [])
      Plan.new(tables: tables, batches: batches, night: night,
               signatures: signatures, work_orders: work_orders)
    end

    private

    def build_table(item)
      materials =
        if item['materials'].is_a?(Array)
          IntSeq.from_a(item['materials'].map { |value| Integer(value) })
        else
          spec = item['materials']
          IntSeq.seq(Integer(spec['first']), Integer(spec.fetch('step', 1)),
                     Integer(spec['count']))
        end
      BaseTable.new(id: item['id'], materials: materials,
                    categories: item['categories'].map { |value| normalize(value) })
    end

    def build_batch(item, table_index)
      targets = item.fetch('targets').each_with_object({}) do |(table_id, spec), memo|
        table = table_index.fetch(table_id)
        material_seq = build_material_seq(spec['materials'])
        position_seq =
          if spec['positions']
            build_position_seq(spec['positions'])
          else
            derive_positions(table, material_seq)
          end
        memo[table_id] = {
          materials: material_seq,
          positions: position_seq,
          new_category: normalize(spec['new_category'])
        }
      end
      Batch.new(id: item['id'], cost: Integer(item.fetch('cost', 0)), targets: targets)
    end

    def build_material_seq(value)
      return IntSeq.from_a(value.map { |number| Integer(number) }) if value.is_a?(Array)

      IntSeq.seq(Integer(value['first']), Integer(value.fetch('step', 1)),
                 Integer(value['count']))
    end

    def build_position_seq(value)
      return IntSeq.from_a(value.map { |number| Integer(number) }) if value.is_a?(Array)

      IntSeq.seq(Integer(value['first']), Integer(value.fetch('step', 1)),
                 Integer(value['count']))
    end

    # Material AP -> row positions.  The table's own materials are one AP, so
    # matching material numbers occupy consecutive positions per AP part.
    def derive_positions(table, material_seq)
      parts = material_seq.parts.map do |first, step, count|
        position = table.position_of_material(first)
        raise ArgumentError, "material #{first} is not on table #{table.id}" if position.nil?
        if step != table.materials.parts.first[1]
          positions = count.times.map do |k|
            found = table.position_of_material(first + k * step)
            raise ArgumentError, "material #{first + k * step} missing on #{table.id}" if found.nil?

            found
          end
          next [nil, positions]
        end
        [nil, IntSeq.seq(position, 1, count)]
      end
      parts.map(&:last).reduce { |left, right| left.union(right) }
    end

    def build_signatures
      legacy = Array(@spec.dig('signatures', 'legacy')).map { |item| Signature.new(item, :legacy) }
      tonight = Array(@spec.dig('signatures', 'tonight')).map { |item| Signature.new(item, :tonight) }
      legacy + tonight
    end

    def normalize(value)
      Errata.normalize_value(value)
    end
  end

  # The full planning instance.
  class Plan
    attr_reader :tables, :batches, :night, :signatures, :work_orders

    def initialize(tables:, batches:, night:, signatures:, work_orders:)
      @tables = tables
      @batches = batches
      @night = night
      @signatures = signatures
      @work_orders = work_orders
    end

    def table_index
      @table_index ||= @tables.each_with_object({}) { |table, memo| memo[table.id] = table }
    end
  end
end
