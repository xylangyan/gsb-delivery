# frozen_string_literal: true

require "json"
require_relative "geometry"
require_relative "ring"

module RGeo
  module GeoJSON
    module Dateline
      # A polygon as registered by the supplier: one outer ring followed by
      # zero or more holes, with the original vertex order preserved.
      class Area
        attr_reader :area_id, :name, :rings, :edge_ids, :source

        # rings : Array<Array<MicroPoint>> closed rings (outer first)
        # edge_ids : Array<Array<String>> per-ring original edge ids
        def initialize(area_id:, rings:, edge_ids: nil, name: nil, source: nil)
          @area_id = area_id.to_s
          @name = name
          @rings = rings.map { |ring| Geometry.close_ring(ring) }
          @edge_ids = edge_ids || default_edge_ids
          @source = source
        end

        def outer_raw
          @rings[0]
        end

        def holes_raw
          @rings[1..-1] || []
        end

        def default_edge_ids
          @rings.each_with_index.map do |ring, r|
            n = ring.size - 1
            (0...n).map { |i| "#{@area_id}/r#{r}/e#{i}" }
          end
        end

        def all_edge_ids
          @edge_ids.flatten
        end

        # Convert a GeoJSON Feature into an Area. Coordinates must be
        # integers in microdegrees or decimals that land exactly on a
        # microdegree.
        def self.from_feature(feature)
          props = feature["properties"] || {}
          area_id = props["area_id"] || props["id"] || feature["id"]
          raise ArgumentError, "survey area feature is missing area_id" if area_id.nil?

          geom = feature["geometry"] || feature
          unless geom["type"] == "Polygon"
            raise ArgumentError, "area #{area_id}: expected Polygon, got #{geom['type']}"
          end

          rings = geom["coordinates"].map { |ring| micro_ring(ring, area_id) }
          raw_ids = props["edge_ids"]
          edge_ids = raw_ids && raw_ids.map { |ids| Array(ids).map(&:to_s) }
          new(area_id: area_id, rings: rings, edge_ids: edge_ids,
              name: props["name"], source: feature)
        end

        def self.micro_ring(ring, area_id)
          pts = ring.map do |pair|
            lon, lat = pair
            MicroPoint.new(micro(lon, area_id, "lon"), micro(lat, area_id, "lat"))
          end
          Geometry.close_ring(pts)
        end

        def self.micro(value, area_id, axis)
          f = value.to_f
          micro = (f * Geometry::MICRO_PER_DEGREE).round
          unless (f * Geometry::MICRO_PER_DEGREE - micro).abs < 1e-6
            raise ArgumentError,
                  "area #{area_id}: #{axis} #{value.inspect} is not an integer microdegree"
          end
          micro
        end
      end
    end
  end
end
