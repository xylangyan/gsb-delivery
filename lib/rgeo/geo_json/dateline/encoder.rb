# frozen_string_literal: true

require "json"
require_relative "geometry"

module RGeo
  module GeoJSON
    module Dateline
      # Encodes pieces as GeoJSON that RGeo::GeoJSON can read back. All
      # coordinates are folded into [-180, 180]; there is no third ordinate.
      module Encoder
        module_function

        def round_value(value, decimals)
          # Exact Rational rounding, half up, to the requested decimals.
          scale = 10**decimals
          rational = value.is_a?(Rational) ? value : Rational(value)
          (rational * scale).round.to_f / scale
        end

        def degrees(value, decimals)
          round_value(Rational(value, Geometry::MICRO_PER_DEGREE), decimals)
        end

        def point_coordinates(point, decimals)
          [degrees(point.lon, decimals), degrees(point.lat, decimals)]
        end

        def ring_coordinates(points, decimals)
          points.map { |p| point_coordinates(p, decimals) }
        end

        def piece_geometry(piece, decimals)
          outers = piece.rings.map do |ring|
            [
              ring_coordinates(ring[:points], decimals),
              *ring[:holes].map { |h| ring_coordinates(h[:points], decimals) },
            ]
          end
          if outers.size == 1
            { "type" => "Polygon", "coordinates" => outers[0] }
          else
            { "type" => "MultiPolygon", "coordinates" => outers }
          end
        end

        def feature(id:, geometry:, properties:)
          {
            "type" => "Feature",
            "id" => id,
            "geometry" => geometry,
            "properties" => properties,
          }
        end

        def feature_collection(features, properties = {})
          {
            "type" => "FeatureCollection",
            "properties" => properties,
            "features" => features,
          }
        end

        # JSON-ready point from an exact MicroPoint without losing the
        # microdegree provenance.
        def exact_point_json(point)
          {
            "lon_micro" => integer_or_rational(point.lon),
            "lat_micro" => integer_or_rational(point.lat),
          }
        end

        def integer_or_rational(value)
          value.is_a?(Rational) && value.denominator == 1 ? value.to_i : value
        end

        def json_safe(value)
          case value
          when Rational
            value.denominator == 1 ? value.to_i : value.to_f
          when Array
            value.map { |v| json_safe(v) }
          when Hash
            value.transform_values { |v| json_safe(v) }
          else
            value
          end
        end
      end

      # Verifies a piece geometry by reading it back with RGeo::GeoJSON.
      module Readback
        module_function

        def parse(hash, geo_factory: nil)
          require "rgeo/geo_json"
          factory = geo_factory || RGeo::Cartesian.simple_factory(srid: 4326)
          RGeo::GeoJSON.decode(hash, geo_factory: factory)
        end
      end
    end
  end
end
