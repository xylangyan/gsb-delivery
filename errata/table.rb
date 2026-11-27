# frozen_string_literal: true

require_relative 'int_seq'

module Errata
  # Raised when a replay step fails the way the old library does:
  # CategoricalIndex#subset calls #map on a single Integer position.
  class OldLibraryError < StandardError; end

  # A base table ("底表"): an ordered run of rows.  Each row has an original
  # ticket identity (its material number) and a raw category from a short,
  # cyclically repeated template.  Material numbers form an arithmetic
  # progression, so a table starting at position 1,000,000 costs nothing to
  # represent.
  class BaseTable
    attr_reader :id, :materials, :categories, :layer

    def initialize(id:, materials:, categories:)
      @id = id
      @layer = 0
      @materials =
        if materials.is_a?(IntSeq)
          materials
        elsif materials.is_a?(Array)
          IntSeq.from_a(materials)
        else
          spec = materials
          IntSeq.seq(Integer(spec['first'] || spec[:first]),
                     Integer(spec['step'] || spec[:step] || 1),
                     Integer(spec['count'] || spec[:count]))
        end
      @categories = Array(categories).map { |value| normalize(value) }
      raise ArgumentError, "table #{id} has no rows" if @materials.empty?
      raise ArgumentError, "table #{id} has no category template" if @categories.empty?
    end

    def size
      @materials.size
    end

    def material_at(position)
      raise IndexError, "position #{position} out of table #{@id}" unless in_range?(position)

      values = @materials.parts
      first_part = values.first
      offset = position < 0 ? size + position : position
      first_part[0] + offset * first_part[1]
    end

    def position_of_material(material)
      material = Integer(material)
      @materials.parts.each do |first, step, count|
        next if material < first || material > first + (count - 1) * step
        next unless (material - first) % step == 0

        position = (material - first) / step
        return position
      end
      nil
    end

    def raw_category_at(position)
      offset = position < 0 ? size + position : position
      @categories[offset % @categories.size]
    end

    def in_range?(position)
      return false unless position.is_a?(Numeric)

      position < size && position >= -size
    end

    # Positions (0-based) whose raw template category equals +value+.
    def raw_positions_for(value)
      roots = @categories.each_index.select { |idx| @categories[idx] == value }
      parts = roots.map do |root|
        count = ((size - 1 - root) / @categories.size) + 1
        [root, @categories.size, count]
      end
      IntSeq.new(parts)
    end

    def normalize(value)
      Errata.normalize_value(value)
    end
  end

  # A table as it reads tonight under a chosen set of approved batches.
  # Batches are applied in fixed night order; for a given material number the
  # last matching batch wins.  Everything stays symbolic: displayed category
  # sets are unions/differences of arithmetic progressions.
  class TableView
    attr_reader :table, :selected_batches

    def initialize(table, selected_batches_in_order)
      @table = table
      @selected_batches = selected_batches_in_order.select do |batch|
        batch.touches_table?(table.id)
      end
      @display = {}
      build_display_sets
    end

    def id
      @table.id
    end

    def size
      @table.size
    end

    def displays?(value)
      @display.key?(value) && !@display[value].empty?
    end

    # IntSeq of positions currently displaying +value+.
    def positions_for(value)
      @display[value] || IntSeq.new
    end

    # Category a CategoricalIndex built on tonight's view would display.
    def raw_category_at(position)
      offset = position < 0 ? size + position : position
      winner = winner_batch(offset)
      winner ? winner.new_category_for(table_id) : @table.raw_category_at(offset)
    end

    # Category from the original short template, before any approved erratum.
    def original_category_at(position)
      @table.raw_category_at(position)
    end

    def winner_at(position)
      offset = position < 0 ? size + position : position
      winner_batch(offset)&.id
    end

    def material_at(position)
      @table.material_at(position)
    end

    def in_range?(position)
      @table.in_range?(position)
    end

    def layer
      0
    end

    private

    def table_id
      @table.id
    end

    def winner_batch(offset)
      @selected_batches.reverse.find do |batch|
        batch.positions_in(table_id).include?(offset)
      end
    end

    def build_display_sets
      @table.categories.uniq.each do |value|
        @display[value] = @table.raw_positions_for(value)
      end
      @selected_batches.each do |batch|
        positions = batch.positions_in(table_id)
        new_category = batch.new_category_for(table_id)
        @display.each_key do |value|
          next if value == new_category

          @display[value] = @display[value].subtract(positions)
        end
        @display[new_category] = (@display[new_category] || IntSeq.new).union(positions)
      end
    end
  end

  # An ephemeral sub-table produced by subset/at.  Row identities keep the
  # original ticket numbers so signatures can be checked against the original
  # tickets rather than displayed values.
  class SubTable
    attr_reader :id, :tickets, :categories, :layer, :winners

    def initialize(id, tickets, categories, layer = 1, winners = nil)
      @id = id
      @tickets = tickets
      @categories = categories
      @layer = layer
      @winners = winners || Array.new(tickets.size)
      raise ArgumentError, "sub-table #{id} is empty" if tickets.empty?
    end

    def size
      @tickets.size
    end

    def material_at(position)
      offset = position < 0 ? size + position : position
      @tickets.fetch(offset)
    end

    def raw_category_at(position)
      offset = position < 0 ? size + position : position
      @categories.fetch(offset)
    end

    def winner_at(position)
      offset = position < 0 ? size + position : position
      @winners.fetch(offset)
    end

    def in_range?(position)
      position.is_a?(Numeric) && position < size && position >= -size
    end

    def raw_positions_for(value)
      IntSeq.from_a(@categories.each_index.select { |idx| @categories[idx] == value })
    end

    def displays?(value)
      @categories.include?(value)
    end

    def positions_for(value)
      raw_positions_for(value)
    end

    def category_at(position)
      raw_category_at(position)
    end
  end
end
