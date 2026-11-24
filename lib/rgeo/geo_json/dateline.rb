# frozen_string_literal: true

require "json"
require_relative "dateline/geometry"
require_relative "dateline/ring"
require_relative "dateline/area"
require_relative "dateline/handover"
require_relative "dateline/polygon"
require_relative "dateline/clipper"
require_relative "dateline/splitter"
require_relative "dateline/stations"
require_relative "dateline/encoder"
require_relative "dateline/junction"
require_relative "dateline/solver"
require_relative "dateline/loader"

module RGeo
  module GeoJSON
    module Dateline
      # High-level entry point. Reads a registration document, resolves all
      # legal interpretations, and writes a GeoJSON FeatureCollection that
      # RGeo::GeoJSON can read back, plus a sidecar report whose junctions
      # can be checked against original edges and along-edge parameters.
      module Publisher
        module_function

        def publish(input_path, output_dir: nil, geo_factory: nil)
          areas, handover = Loader.load_registrations(input_path)
          publish_areas(areas, handover, output_dir: output_dir, geo_factory: geo_factory)
        end

        def publish_areas(areas, handover, output_dir: nil, geo_factory: nil)
          solver = Solver.new(areas: areas, handover: handover)
          result = solver.solve

          features = result["signed_pieces"] || []
          collection = Encoder.json_safe(
            Encoder.feature_collection(
              features,
              {
                "status" => result["status"],
                "display_decimals" => handover.display_decimals,
                "boundary_rule" => handover.boundary_rule,
              }
            )
          )

          report = build_report(result, handover)

          if output_dir
            require "fileutils"
            FileUtils.mkdir_p(output_dir)
            File.write(File.join(output_dir, "coverage_fragments.geojson"),
                       JSON.pretty_generate(collection))
            File.write(File.join(output_dir, "resolution_report.json"),
                       JSON.pretty_generate(report))
          end

          readback = verify_readback(collection, geo_factory: geo_factory) if features.any?

          {
            "geojson" => collection,
            "report" => report,
            "solver_result" => result,
            "readback" => readback,
          }
        end

        def build_report(result, handover)
          report = {
            "status" => result["status"],
            "display_decimals" => handover.display_decimals,
            "boundary_rule" => handover.boundary_rule,
            "signed_areas" => (result["signed_pieces"] || []).map { |f| f["properties"]["area_id"] }.uniq,
            "fragment_count" => (result["signed_pieces"] || []).size,
            "station_ownership" => result["station_ownership"] || [],
            "ambiguous_stations" => result["ambiguous_stations"] || [],
            "withheld_areas" => result["withheld_areas"] || [],
            "unreadable_areas" => result["unreadable_areas"] || [],
            "overlaps" => result["overlaps"] || [],
          }

          report["junction_records"] = junction_records(result)
          report["interpretations"] = result["interpretations"] if result["interpretations"]
          report["blocking_evidence"] = result["blocking_evidence"] if result["blocking_evidence"]
          report["independent_areas"] = result["independent_areas"] if result["independent_areas"]
          Encoder.json_safe(report)
        end

        # Rebuild exact junction coordinates from the solver's pieces. This
        # runs after the pieces are already produced, so it sees the same
        # segment provenance used to emit the geometry.
        def junction_records(result)
          return [] unless result["status"] == "ok"

          # Junction records are produced by the solver alongside pieces; if
          # absent, derive from encoded geometry is not lossless, so return
          # whatever the solver attached.
          result["junction_records"] || []
        end

        def verify_readback(collection, geo_factory: nil)
          feature = Readback.parse(collection, geo_factory: geo_factory)
          return { "ok" => false, "reason" => "decode returned nil" } if feature.nil?

          geometries = feature.map(&:geometry)
          {
            "ok" => feature.size == collection["features"].size,
            "feature_count" => feature.size,
            "geometry_types" => geometries.map(&:geometry_type).map(&:to_s),
          }
        rescue StandardError => e
          { "ok" => false, "reason" => "#{e.class}: #{e.message}" }
        end
      end
    end
  end
end
