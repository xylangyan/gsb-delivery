# frozen_string_literal: true

require_relative "geometry"
require_relative "polygon"

module RGeo
  module GeoJSON
    module Dateline
      # Assigns stations to a polygon interpretation under the handover's
      # boundary rule. A station exactly on an edge is decided by that rule,
      # never by floating-point luck.
      module StationAssignment
        module_function

        # A station belongs to an interpretation when it is inside the outer
        # ring and outside every hole, or on a boundary edge that the rule
        # counts as inside.
        #
        # boundary_rule:
        #   "closed"   -> boundary points count as inside
        #   "open"     -> boundary points count as outside
        #   "exterior" -> same as "open"
        def assign(interpretation, station, boundary_rule: "closed")
          on_boundary = on_registered_boundary?(interpretation, station)
          if on_boundary
            case boundary_rule.to_s
            when "closed", "inclusive"
              return { inside: true, on_edge: true, edge: on_boundary }
            else
              return { inside: false, on_edge: true, edge: on_boundary }
            end
          end

          outer_loc = Geometry.point_location(interpretation.outer_ring, station)
          return { inside: false, on_edge: false, edge: nil } unless outer_loc == :inside

          interpretation.holes.each do |hole|
            hole_loc = Geometry.point_location(hole.closed_vertices, station)
            case hole_loc
            when :inside
              return { inside: false, on_edge: false, edge: nil }
            when :boundary
              # A hole ring is part of the polygon boundary, so the same
              # handover boundary rule applies as for the outer ring.
              inside = %w[closed inclusive].include?(boundary_rule.to_s)
              return { inside: inside, on_edge: true, edge: boundary_edge(hole, station) }
            end
          end

          { inside: true, on_edge: false, edge: nil }
        end

        def on_registered_boundary?(interpretation, point)
          interpretation.ring_interpretations.each do |ring|
            edge = boundary_edge(ring, point)
            return edge if edge
          end
          nil
        end

        def boundary_edge(ring, point)
          ring.closed_vertices.each_cons(2).with_index do |(a, b), i|
            next unless Geometry.on_segment?(a, b, point)

            t = parameter_along(a, b, point)
            return {
              "edge_id" => ring.edge_ids[i % ring.edge_ids.size],
              "ring_index" => ring.ring_index,
              "role" => ring.role.to_s,
              "t" => t,
              "half_direction" => ring.half_directions[ring.edge_ids[i % ring.edge_ids.size]]&.to_s,
            }
          end
          nil
        end

        def parameter_along(a, b, point)
          if a.lon == b.lon && a.lat == b.lat
            0r
          elsif a.lon != b.lon
            Rational(point.lon - a.lon, b.lon - a.lon)
          else
            Rational(point.lat - a.lat, b.lat - a.lat)
          end
        end
      end

      # Checks that two registered edges described as the same physical line
      # are expanded consistently. Directions are given as forward/reverse
      # relative to each registration's own vertex order.
      module SharedEdgeCheck
        module_function

        # interpretations : Hash{area_id => PolygonInterpretation}
        # group : Array of { area_id:, edge_id:, direction: }
        #
        # Returns { ok:, reason:, forward:, reverse: } where forward/reverse
        # are the canonical segment endpoints in the forward registration.
        def check(group, interpretations)
          resolved = group.map do |entry|
            interp = interpretations[entry[:area_id]]
            return { ok: false, reason: "area #{entry[:area_id]} has no legal interpretation" } if interp.nil?

            edge = find_edge(interp, entry[:edge_id])
            return { ok: false, reason: "edge #{entry[:edge_id]} is not in area #{entry[:area_id]}" } if edge.nil?

            { entry: entry, edge: edge }
          end

          base = resolved.first[:edge]
          fwd = orient(base[:a], base[:b], resolved.first[:entry][:direction])

          resolved.drop(1).each do |item|
            edge = item[:edge]
            other = orient(edge[:a], edge[:b], item[:entry][:direction])

            # Two registrations may express the same physical line with
            # longitudes differing by a whole turn (170 vs -190). Compare the
            # points modulo 360 so only a genuine side change fails.
            unless same_physical_segment?(fwd[0], fwd[1], other[0], other[1])
              return {
                ok: false,
                reason: "edge #{item[:entry][:edge_id]} of #{item[:entry][:area_id]} is placed on the other side of the dateline",
                expected: [fwd[0].to_a, fwd[1].to_a],
                actual: [other[0].to_a, other[1].to_a],
              }
            end
          end

          { ok: true, forward: fwd[0], reverse: fwd[1] }
        end

        def same_physical_segment?(a1, b1, a2, b2)
          turn = a2.lon - a1.lon
          aligned_a = a2.shift(-turn)
          aligned_b = b2.shift(-turn)
          (a1 == aligned_a && b1 == aligned_b) ||
            (a1 == aligned_b && b1 == aligned_a)
        end

        def orient(a, b, direction)
          case direction.to_s
          when "reverse", "reverse_direction", "-", "backward"
            [b, a]
          else
            [a, b]
          end
        end

        def find_edge(interpretation, edge_id)
          interpretation.ring_interpretations.each do |ring|
            idx = ring.edge_ids.index(edge_id)
            next if idx.nil?

            return { a: ring.vertices[idx], b: ring.vertices[(idx + 1) % ring.vertices.size] }
          end
          nil
        end
      end
    end
  end
end
