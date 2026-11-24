# frozen_string_literal: true

require_relative "geometry"

module RGeo
  module GeoJSON
    module Dateline
      # Exact clipping of a ring against a meridian half-plane.
      #
      # A ring is carried as an array of segments:
      #   { a: MicroPoint, b: MicroPoint, edge_id: String, t0: Rational, t1: Rational }
      # where t0/t1 are parameters along the *original* edge, so a clipped
      # segment can always be reported back as an interval of its source edge.
      #
      # Points introduced on the cut meridian carry Rational longitude equal
      # to the meridian and Rational latitude when interpolated.
      #
      # For a simple ring the crossings along the meridian alternate
      # outside/inside. Sorting them by latitude and pairing (0,1), (2,3), ...
      # gives the interior intervals; each interior chain therefore has
      # exactly one chain partner across each of its two crossings, and the
      # traversal of pieces is forced.
      class Clipper
        Chain = Struct.new(:points, :segments, :entry, :exit_point, keyword_init: true)

        def initialize(meridian)
          @meridian = meridian
        end

        # Clip to lon <= meridian.
        def clip_left(segments)
          clip(segments, :left)
        end

        # Clip to lon >= meridian.
        def clip_right(segments)
          clip(segments, :right)
        end

        private

        def inside?(point, side)
          side == :left ? point.lon <= @meridian : point.lon >= @meridian
        end

        def strictly_inside?(point, side)
          side == :left ? point.lon < @meridian : point.lon > @meridian
        end

        def clip(segments, side)
          return [] if segments.empty?

          if segments.all? { |s| strictly_inside?(s[:a], side) && strictly_inside?(s[:b], side) }
            return [ring_from_segments(segments)]
          end

          unless segments.any? { |s| !inside?(s[:a], side) }
            # Every vertex lies on or inside the meridian: no cut needed.
            return [ring_from_segments(segments)]
          end

          rotated = rotate_to_outside(segments, side)
          chains = build_chains(rotated, side)
          return [] if chains.empty?

          stitch(chains)
        end

        def rotate_to_outside(segments, side)
          idx = segments.index { |s| !inside?(s[:a], side) }
          idx ? segments.rotate(idx) : segments
        end

        def build_chains(segments, side)
          chains = []
          current = nil

          segments.each do |seg|
            a = seg[:a]
            b = seg[:b]
            a_in = inside?(a, side)
            b_in = inside?(b, side)

            if a_in && b_in
              current ||= { points: [a], segments: [] }
              current[:points] << b
              current[:segments] << seg
            elsif a_in && !b_in
              current ||= { points: [a], segments: [] }
              hit, t = cut_point(a, b)
              current[:points] << hit
              current[:segments] << seg.merge(b: hit, t1: t)
              chains << Chain.new(
                points: current[:points],
                segments: current[:segments],
                entry: current[:points].first,
                exit_point: hit
              )
              current = nil
            elsif !a_in && b_in
              hit, t = cut_point(a, b)
              current = { points: [hit, b], segments: [seg.merge(a: hit, t0: t)] }
            end
          end

          if current
            # A chain wrapped past the end of the rotated list. Because the
            # list started outside this should not happen for a normal ring;
            # treat the leftover as a chain ending on the seam.
            chains << Chain.new(
              points: current[:points],
              segments: current[:segments],
              entry: current[:points].first,
              exit_point: current[:points].last
            )
          end

          chains
        end

        def cut_point(a, b)
          return [a, 0r] if a.lon == b.lon

          t = Rational(@meridian - a.lon, b.lon - a.lon)
          lat = a.lat + t * (b.lat - a.lat)
          [MicroPoint.new(@meridian, lat), t]
        end

        def stitch(chains)
          crossings = []
          chains.each_with_index do |chain, ci|
            crossings << { point: chain.entry, chain: ci, kind: :entry }
            crossings << { point: chain.exit_point, chain: ci, kind: :exit }
          end
          crossings.sort_by! { |c| [c[:point].lat, c[:chain], c[:kind] == :entry ? 0 : 1] }

          raise "odd number of meridian crossings (#{crossings.size})" if crossings.size.odd?

          # Map each exit crossing to the entry crossing that closes the
          # corresponding interior interval along the meridian.
          partner = {}
          crossings.each_slice(2) do |low, high|
            unless low[:kind] != high[:kind]
              raise "cannot pair two #{low[:kind]} crossings at latitude #{low[:point].lat}"
            end

            exit_c = low[:kind] == :exit ? low : high
            entry_c = low[:kind] == :entry ? low : high
            partner[exit_c[:chain]] = entry_c
          end

          used = Array.new(chains.size, false)
          rings = []

          chains.each_index do |start_ci|
            next if used[start_ci]

            points = []
            segs_out = []
            crossing_records = []
            current = start_ci
            first = true

            loop do
              break if used[current]

              used[current] = true
              chain = chains[current]
              points.concat(first ? chain.points : chain.points[1..-1])
              segs_out.concat(chain.segments)
              first = false

              nxt = partner[current]
              break if nxt.nil?

              connector = meridian_segment(chain.exit_point, nxt[:point])
              if connector
                points << nxt[:point]
                segs_out << connector
                crossing_records << {
                  "from" => point_json(chain.exit_point),
                  "to" => point_json(nxt[:point]),
                  "meridian" => @meridian,
                }
              end

              current = nxt[:chain]
              break if current == start_ci
            end

            cleaned = cleanup(points, segs_out)
            next if cleaned[:points].size < 4
            next if Geometry.ring_area2(cleaned[:points]).zero?

            rings << cleaned.merge(crossings: crossing_records)
          end
          rings
        end

        def point_json(pt)
          [pt.lon, pt.lat]
        end

        def meridian_segment(a, b)
          return nil if a == b

          { a: a, b: b, edge_id: nil, t0: nil, t1: nil, meridian: @meridian, seam: true }
        end

        def ring_from_segments(segments)
          points = []
          segments.each do |s|
            points << s[:a]
            points << s[:b]
          end
          { points: Geometry.close_ring(dedup(points)), segments: segments, crossings: [] }
        end

        def cleanup(points, segments)
          out_points = [points.first]
          out_segments = []
          points.each_cons(2).with_index do |(a, b), i|
            next if a == b

            out_points << b
            out_segments << segments[i] if segments[i]
          end
          out_points = Geometry.close_ring(out_points)
          { points: out_points, segments: out_segments }
        end

        def dedup(points)
          out = []
          points.each { |p| out << p unless out.last == p }
          out
        end
      end

      # Builds the segment list for a ring interpretation. Each segment
      # remembers the original edge and the parameter interval it covers.
      module RingSegments
        module_function

        def build(interpretation)
          verts = interpretation.vertices
          n = verts.size
          segs = []
          n.times do |i|
            a = verts[i]
            b = verts[(i + 1) % n]
            segs << {
              a: a,
              b: b,
              edge_id: interpretation.edge_ids[i],
              t0: 0r,
              t1: 1r,
            }
          end
          segs
        end
      end
    end
  end
end
