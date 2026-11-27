# frozen_string_literal: true

require_relative 'value'
require_relative 'table'
require_relative 'batch'

module Errata
  # Answers the archive questions: for a material number (the reading-round key)
  # which original ticket does each table row really carry, which approved
  # erratum currently decides it, and at which layer a number became a row
  # position.
  class Audit
    def initialize(plan, selected_ids)
      @plan = plan
      @selected_ids = selected_ids
    end

    def by_material(material)
      material = Integer(material)
      @plan.tables.filter_map do |table|
        position = table.position_of_material(material)
        next unless position

        view = build_view(table)
        {
          table: table.id,
          material: material,
          row_position: position,
          layer: 0,
          original_ticket: material,
          original_category: table.raw_category_at(position),
          displayed_category: view.raw_category_at(position),
          winning_batch: view.winner_at(position)
        }
      end
    end

    def trace_layers(traces)
      traces.map do |trace|
        {
          step: trace[:name],
          table: trace[:table],
          layer: trace[:layer],
          became_row_position: trace[:interpretations].grep(/used as row position/),
          category_lookups: trace[:interpretations].grep(/category/),
          branch: trace[:branch],
          result: trace[:result],
          position: trace[:position]
        }
      end
    end

    private

    def build_view(table)
      chosen = @selected_ids.filter_map { |id| @plan.night.by_id[id] }
      TableView.new(table, chosen)
    end
  end
end
