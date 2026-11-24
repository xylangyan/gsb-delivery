# frozen_string_literal: true

require_relative "geometry"

module RGeo
  module GeoJSON
    module Dateline
      # One concrete way of expanding a ring onto the unwrapped longitude
      # plane. A ring has an ordered list of original edges; every edge has
      # either a unique short-arc delta or, for a half-circle edge, one of
      # two permitted directions.
      class RingInterpretation
        attr_reader :area_id, :ring_index, :role, :vertices, :edge_ids,
                    :edge_deltas, :half_directions

        # vertices : Array<MicroPoint> (open, first != last)
        # edge_ids : Array<String>, length == vertices.length
        # edge_deltas : Array<Integer>, length == vertices.length
        # half_directions : Hash{edge_id => :positive/:negative}
        def initialize(area_id:, ring_index:, role:, vertices:, edge_ids:, edge_deltas:, half_directions: {})
          @area_id = area_id
          @ring_index = ring_index
          @role = role
          @vertices = vertices
          @edge_ids = edge_ids
          @edge_deltas = edge_deltas
          @half_directions = half_directions
        end

        def closed_vertices
          @closed_vertices ||= Geometry.close_ring(@vertices)
        end

        def self_intersecting?
          @self_intersecting = Geometry.ring_self_intersects?(closed_vertices) if @self_intersecting.nil?
          @self_intersecting
        end

        def area2
          @area2 ||= Geometry.ring_area2(closed_vertices)
        end

        def degenerate?
          closed_vertices.size < 4 || area2.zero?
        end

        def signed_area
          Rational(area2, 2)
        end

        def clockwise?
          area2 < 0
        end

        def bbox
          Geometry.bbox(closed_vertices)
        end

        def min_lon
          closed_vertices.map(&:lon).min
        end

        def max_lon
          closed_vertices.map(&:lon).max
        end

        def shifted(delta)
          RingInterpretation.new(
            area_id: area_id,
            ring_index: ring_index,
            role: role,
            vertices: vertices.map { |v| v.shift(delta) },
            edge_ids: edge_ids,
            edge_deltas: edge_deltas,
            half_directions: half_directions
          )
        end

        def signature
          [area_id, ring_index, vertices.map { |v| [v.lon, v.lat] }]
        end

        # Vertex index of an original edge endpoint, remembering that the
        # ring is open and wraps.
        def edge_vertex_index(edge_index)
          edge_index
        end

        def describe
          {
            "area_id" => area_id,
            "ring_index" => ring_index,
            "role" => role.to_s,
            "vertices" => closed_vertices.map(&:to_a),
            "edge_ids" => edge_ids,
            "edge_deltas" => edge_deltas,
            "half_directions" => half_directions.transform_keys(&:to_s),
          }
        end
      end

      # Enumerates the legal expansions of a single ring given its raw
      # integer-microdegree vertices and the handover's half-circle rules.
      module RingExpander
        module_function

        # raw_points must be closed (first == last) or will be closed.
        # half_rules maps edge_id => [:positive], [:negative], or both.
        def expand(area_id:, ring_index:, role:, raw_points:, edge_ids: nil, half_rules: {})
          pts = Geometry.close_ring(raw_points)
          n = pts.size - 1
          return [] if n < 3

          ids = edge_ids || (0...n).map { |i| "#{area_id}/r#{ring_index}/e#{i}" }
          candidates = (0...n).map do |i|
            candidates_for_edge(pts[i], pts[i + 1], ids[i], half_rules)
          end

          results = []
          deltas = []
          directions = {}
          dfs = lambda do |edge_index, sum|
            if edge_index == n
              if sum.zero?
                vertices = build_vertices(pts, deltas)
                interp = RingInterpretation.new(
                  area_id: area_id,
                  ring_index: ring_index,
                  role: role,
                  vertices: vertices,
                  edge_ids: ids,
                  edge_deltas: deltas.dup,
                  half_directions: directions.dup
                )
                results << interp unless interp.degenerate? || interp.self_intersecting?
              end
              return
            end

            candidates[edge_index].each do |delta, direction|
              deltas << delta
              directions[ids[edge_index]] = direction if direction
              dfs.call(edge_index + 1, sum + delta)
              directions.delete(ids[edge_index])
              deltas.pop
            end
          end
          dfs.call(0, 0)
          results
        end

        def build_vertices(pts, deltas)
          x = pts[0].lon
          out = []
          pts[0...-1].each_with_index do |pt, i|
            out << MicroPoint.new(x, pt.lat)
            x += deltas[i]
          end
          out
        end

        # Raw endpoint delta, reduced to (-HALF, HALF]. HALF means the edge
        # spans exactly half a circle and has two permitted directions.
        def candidates_for_edge(a, b, edge_id, half_rules)
          raw = b.lon - a.lon
          norm = raw % CIRCLE
          norm -= CIRCLE if norm > HALF

          if norm == HALF
            allowed = Array(half_rules[edge_id] || %i[positive negative])
            allowed.filter_map do |d|
              case d
              when :positive, :plus, :east, "+", 1 then [HALF, :positive]
              when :negative, :minus, :west, "-", -1 then [-HALF, :negative]
              end
            end.uniq
          else
            [[norm, nil]]
          end
        end
      end
    end
  end
end
