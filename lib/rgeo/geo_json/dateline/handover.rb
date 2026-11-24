# frozen_string_literal: true

require "json"
require_relative "geometry"

module RGeo
  module GeoJSON
    module Dateline
      # Parsed handover sheet ("交接单"). The sheet is deliberately small and
      # declarative: it carries only the facts the supplier registration
      # cannot express by itself.
      #
      # Supported keys:
      #
      #   display_decimals   Integer, coordinate rounding for emitted geometry.
      #   boundary_rule      "closed" (default) or "open"; how a station that
      #                      lies exactly on an edge is counted.
      #   half_circle_edges  Array of { "edge_id" => id,
      #                                 "directions" => ["positive", "negative"] }
      #                      or a Hash{edge_id => directions}.
      #   shared_edges       Array of { "edges" => [
      #                         { "area_id" => .., "edge_id" => ..,
      #                           "direction" => "forward"|"reverse" }, ... ] }
      #                      Two or more original edges that are the same
      #                      physical line.
      #   stations           Array of { "id", "lon", "lat", "areas" => [...],
      #                                  "status" => "confirmed"|"questioned",
      #                                  "on_edge" => optional override }
      #   independent_areas  Area ids that are independently confirmed and
      #                      remain publishable even when another registration
      #                      group conflicts.
      class Handover
        attr_reader :display_decimals, :boundary_rule, :half_circle_edges,
                    :shared_edge_groups, :stations, :independent_areas, :source

        def self.load(path)
          new(JSON.parse(File.read(path)))
        end

        def self.from_hash(hash)
          new(hash)
        end

        def initialize(raw)
          @source = raw
          @display_decimals = Integer(raw.fetch("display_decimals", 6))
          @boundary_rule = (raw["boundary_rule"] || "closed").to_s
          @half_circle_edges = normalize_half_edges(raw["half_circle_edges"])
          @shared_edge_groups = normalize_shared(raw["shared_edges"])
          @stations = Array(raw["stations"]).map { |s| normalize_station(s) }
          @independent_areas = Array(raw["independent_areas"]).map(&:to_s)
        end

        # directions allowed for a half-circle edge, as [:positive, :negative]
        def directions_for(edge_id)
          @half_circle_edges[edge_id.to_s]
        end

        def half_rule_map
          @half_circle_edges
        end

        def confirmed_stations
          @stations.select { |s| s[:status] == "confirmed" }
        end

        def station(id)
          @stations.find { |s| s[:id] == id.to_s }
        end

        private

        def normalize_half_edges(value)
          result = {}
          case value
          when Hash
            value.each do |edge_id, dirs|
              result[edge_id.to_s] = normalize_directions(dirs)
            end
          when Array
            value.each do |entry|
              edge_id = entry["edge_id"] || entry["edge"]
              next unless edge_id

              result[edge_id.to_s] = normalize_directions(entry["directions"] || entry["allowed"])
            end
          end
          result
        end

        def normalize_directions(dirs)
          Array(dirs).map do |d|
            case d.to_s.downcase
            when "positive", "plus", "east", "+", "1"
              :positive
            when "negative", "minus", "west", "-", "-1"
              :negative
            else
              raise ArgumentError, "unknown half-circle direction #{d.inspect}"
            end
          end.uniq
        end

        def normalize_shared(value)
          Array(value).map do |group|
            entries = group["edges"] || group["edge"] || group
            Array(entries).map do |e|
              {
                area_id: (e["area_id"] || e["area"]).to_s,
                edge_id: (e["edge_id"] || e["edge"]).to_s,
                direction: (e["direction"] || "forward").to_s,
              }
            end
          end.reject { |g| g.size < 2 }
        end

        def normalize_station(raw)
          {
            id: raw["id"].to_s,
            lon: Integer(raw["lon_micro"] || (raw["lon"].to_f * Geometry::MICRO_PER_DEGREE).round),
            lat: Integer(raw["lat_micro"] || (raw["lat"].to_f * Geometry::MICRO_PER_DEGREE).round),
            areas: Array(raw["areas"]).map(&:to_s),
            status: (raw["status"] || "confirmed").to_s,
            on_edge: raw["on_edge"] && raw["on_edge"].to_s,
            note: raw["note"],
          }
        end
      end
    end
  end
end
