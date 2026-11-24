# frozen_string_literal: true

require_relative "geometry"
require_relative "ring"
require_relative "area"

module RGeo
  module GeoJSON
    module Dateline
      # One consistent way of reading a registered polygon: an outer ring
      # expansion plus one expansion for each registered hole. Every ring
      # keeps the original vertex order from the registration.
      class PolygonInterpretation
        attr_reader :area_id, :outer, :holes, :signature

        def initialize(area_id:, outer:, holes:)
          @area_id = area_id
          @outer = outer
          @holes = holes
          @signature = [outer.signature, holes.map(&:signature)]
        end

        def ring_interpretations
          [outer, *holes]
        end

        def outer_ring
          outer.closed_vertices
        end

        def valid?
          return false unless outer_self_ok?

          holes.all? do |hole|
            Geometry.point_location(outer_ring, Geometry.representative_point(hole.closed_vertices)) == :inside
          end
        end

        def outer_self_ok?
          !outer.self_intersecting? && !outer.degenerate?
        end

        def orientation_ok?
          # GeoJSON right-hand rule: outer counter-clockwise, holes clockwise
          # in the unwrapped plane. Suppliers preserve order, so this is a
          # diagnostic rather than a hard requirement.
          outer.clockwise? == false
        end

        # Flatten to a list of edges for diagnostics.
        def edge_inventory
          ring_interpretations.flat_map do |ring|
            ring.edge_ids.each_with_index.map do |eid, i|
              {
                "ring_index" => ring.ring_index,
                "role" => ring.role.to_s,
                "edge_id" => eid,
                "from" => ring.vertices[i].to_a,
                "to" => ring.vertices[(i + 1) % ring.vertices.size].to_a,
                "half_direction" => ring.half_directions[eid]&.to_s,
              }
            end
          end
        end

        def describe
          {
            "area_id" => area_id,
            "rings" => ring_interpretations.map(&:describe),
          }
        end
      end

      # Enumerates legal polygon interpretations for one registered area.
      module PolygonExpander
        module_function

        def expand(area, handover)
          outer_candidates = RingExpander.expand(
            area_id: area.area_id,
            ring_index: 0,
            role: :outer,
            raw_points: area.rings[0],
            edge_ids: area.edge_ids[0],
            half_rules: handover.half_rule_map
          )
          return [] if outer_candidates.empty?

          hole_candidates = (1...area.rings.size).map do |r|
            RingExpander.expand(
              area_id: area.area_id,
              ring_index: r,
              role: :hole,
              raw_points: area.rings[r],
              edge_ids: area.edge_ids[r],
              half_rules: handover.half_rule_map
            )
          end
          return [] if hole_candidates.any?(&:empty?)

          combos = [outer_candidates]
          hole_candidates.each { |list| combos << list }

          results = []
          each_combination(combos) do |rings|
            outer = rings[0]
            holes = rings[1..-1] || []
            next if outer.self_intersecting? || outer.degenerate?

            interp = PolygonInterpretation.new(area_id: area.area_id, outer: outer, holes: holes)
            results << interp if interp.valid?
          end
          results
        end

        def each_combination(lists, prefix = [], &block)
          if lists.empty?
            yield prefix
            return
          end

          lists.first.each do |item|
            each_combination(lists[1..-1], prefix + [item], &block)
          end
        end
      end
    end
  end
end
