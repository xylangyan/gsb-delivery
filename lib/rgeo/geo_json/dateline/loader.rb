# frozen_string_literal: true

require "json"
require_relative "geometry"
require_relative "area"
require_relative "handover"

module RGeo
  module GeoJSON
    module Dateline
      # Reads supplier registrations from a GeoJSON FeatureCollection.
      #
      # The collection may be the bare FeatureCollection or a Hash with
      # "areas" and "handover" keys. Coordinates must be expressible as
      # integer microdegrees; fractional values that cannot be are rejected
      # instead of being silently rounded.
      module Loader
        module_function

        def load_registrations(path)
          hash = JSON.parse(File.read(path))
          from_hash(hash)
        end

        def from_hash(hash)
          features = if hash["type"] == "FeatureCollection"
                       hash["features"]
                     else
                       hash["areas"] || hash["features"]
                     end

          raise ArgumentError, "no survey area features found" unless features.is_a?(Array)

          areas = features.map { |f| Area.from_feature(f) }
          handover_hash = hash["handover"] || {}
          handover = Handover.from_hash(handover_hash)
          [areas, handover]
        end
      end
    end
  end
end
