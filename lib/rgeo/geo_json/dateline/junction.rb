# frozen_string_literal: true

require_relative "geometry"
require_relative "encoder"

module RGeo
  module GeoJSON
    module Dateline
      # Records points that matter for review, with enough exact data to
      # check them against the registration:
      #
      #   * edge_id and along-edge parameter t (t=0 at the first registered
      #     vertex, t=1 at the second);
      #   * the original registered endpoints of that edge (unwrapped);
      #   * the exact interpolation in original microdegrees;
      #   * the same point in the emitted fragment's coordinates (after the
      #     integer 360-degree shift and the date-line split);
      #   * the display coordinate after the handover's rounding.
      #
      # A seam crossing caused only by the date-line split is not an original
      # edge; it is reported with kind "dateline_crossing" and the source
      # edge plus parameter of the edge it cut.
      module Junction
        module_function

        def records_for_piece(piece, display_decimals: 6)
          interpretation = piece.interpretation
          records = []

          piece.rings.each do |ring|
            ring[:segments].each do |seg|
              next if seg[:edge_id].nil?

              edge = find_edge(interpretation, seg[:edge_id])
              next if edge.nil?

              [[seg[:a], seg[:t0]], [seg[:b], seg[:t1]]].each do |point, t|
                next if t.nil?

                original = interpolate(edge[:a], edge[:b], t)
                piece_point = original.shift(piece.shift)
                kind = if point.lon == Geometry::HALF || point.lon == -Geometry::HALF
                         "dateline_crossing"
                       elsif t.zero? || t == 1
                         "vertex"
                       else
                         "intersection"
                       end

                records << {
                  "piece_id" => piece.piece_id,
                  "area_id" => piece.area_id,
                  "kind" => kind,
                  "edge_id" => seg[:edge_id],
                  "t" => t.to_f,
                  "t_fraction" => "#{t.numerator}/#{t.denominator}",
                  "original_edge" => {
                    "from_micro" => edge[:a].to_a,
                    "to_micro" => edge[:b].to_a,
                  },
                  "original_point_micro" => {
                    "lon_micro" => Encoder.integer_or_rational(original.lon),
                    "lat_micro" => Encoder.integer_or_rational(original.lat),
                  },
                  "piece_point_micro" => {
                    "lon_micro" => Encoder.integer_or_rational(piece_point.lon),
                    "lat_micro" => Encoder.integer_or_rational(piece_point.lat),
                  },
                  "display" => Encoder.point_coordinates(piece_point, display_decimals),
                }
              end
            end
          end

          records.uniq { |r| [r["piece_id"], r["edge_id"], r["t"], r["piece_point_micro"]] }
        end

        def find_edge(interpretation, edge_id)
          interpretation.ring_interpretations.each do |ring|
            idx = ring.edge_ids.index(edge_id)
            next if idx.nil?

            return { a: ring.vertices[idx], b: ring.vertices[(idx + 1) % ring.vertices.size] }
          end
          nil
        end

        def interpolate(a, b, t)
          MicroPoint.new(
            a.lon + t * (b.lon - a.lon),
            a.lat + t * (b.lat - a.lat)
          )
        end
      end
    end
  end
end
