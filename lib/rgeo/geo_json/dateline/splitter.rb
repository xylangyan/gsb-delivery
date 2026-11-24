# frozen_string_literal: true

require_relative "geometry"
require_relative "ring"
require_relative "polygon"
require_relative "clipper"

module RGeo
  module GeoJSON
    module Dateline
      # Splits one polygon interpretation into pieces whose longitudes all
      # lie in [-180, 180].
      #
      # The unwrapped ring may sit anywhere on the longitude line. For each
      # integer number of full turns k, the ring is translated by k*360 and
      # intersected with the fixed window [-180, 180]. Segment provenance
      # (original edge id and along-edge parameters) is carried through the
      # translation and clipping, so every emitted piece and every junction
      # can be traced back to the registration.
      class PieceSplitter
        WINDOW_MIN = -Geometry::HALF
        WINDOW_MAX = Geometry::HALF

        Piece = Struct.new(
          :area_id, :piece_id, :side, :shift, :rings, :crossings,
          :interpretation,
          keyword_init: true
        )

        def initialize(interpretation, seam_meridians: nil)
          @interpretation = interpretation
          @seam_meridians = seam_meridians
        end

        def pieces
          result = []

          turn_range.each do |turns|
            shift = turns * Geometry::CIRCLE
            sets = clip_polygon(shift)
            next if sets.empty?

            sets.each_with_index do |set, idx|
              next if set[:rings].empty?

              result << Piece.new(
                area_id: @interpretation.area_id,
                piece_id: "#{@interpretation.area_id}:t#{turns}:#{idx}",
                side: side_for(shift),
                shift: shift,
                rings: set[:rings],
                crossings: set[:crossings],
                interpretation: @interpretation
              )
            end
          end

          dedupe(result)
        end

        private

        # Only integer turns that can overlap the fixed window matter. A ring
        # spanning at most one turn needs at most two candidates; wider
        # unwrapped rings (e.g. successive crossings) are covered too.
        def turn_range
          rings = @interpretation.ring_interpretations
          min_lon = rings.map(&:min_lon).min
          max_lon = rings.map(&:max_lon).max

          low = (-Geometry::HALF - max_lon).fdiv(Geometry::CIRCLE).floor - 1
          high = (Geometry::HALF - min_lon).fdiv(Geometry::CIRCLE).ceil + 1
          (low..high)
        end

        def side_for(shift)
          if shift.zero?
            :whole
          elsif shift.positive?
            :west
          else
            :east
          end
        end

        def clip_polygon(shift)
          outer = @interpretation.outer
          outer_pieces = clip_ring_to_window(RingSegments.build(outer), shift)
          return [] if outer_pieces.empty?

          hole_pieces = @interpretation.holes.flat_map do |hole|
            clip_ring_to_window(RingSegments.build(hole), shift).map do |hp|
              { points: hp[:points], segments: hp[:segments], source: hole }
            end
          end

          outer_pieces.filter_map do |op|
            next if op[:points].size < 4 || Geometry.ring_area2(op[:points]).zero?

            holes = hole_pieces.select do |hp|
              loc = Geometry.point_location(op[:points], Geometry.representative_point(hp[:points]))
              loc == :inside || loc == :boundary
            end

            {
              rings: [{
                role: :outer,
                points: op[:points],
                segments: op[:segments],
                holes: holes.map { |h| { points: h[:points], segments: h[:segments] } },
              }],
              crossings: op[:crossings],
            }
          end
        end

        def translate_segments(segments, delta)
          return segments if delta.zero?

          segments.map do |s|
            s.merge(a: s[:a].shift(delta), b: s[:b].shift(delta))
          end
        end

        # Intersect one ring (as provenance-carrying segments) with the fixed
        # window, returning pieces that all sit in [-180, 180].
        def clip_ring_to_window(segments, shift)
          segs = translate_segments(segments, shift)
          pieces = [{ points: nil, segments: segs, crossings: [] }]

          [WINDOW_MIN, WINDOW_MAX].each do |meridian|
            clipper = Clipper.new(meridian)
            new_pieces = []
            pieces.each do |piece|
              clipped = meridian == WINDOW_MIN ? clipper.clip_right(piece[:segments]) : clipper.clip_left(piece[:segments])
              clipped.each do |c|
                next if c[:points].size < 4
                next if Geometry.ring_area2(c[:points]).zero?

                new_pieces << {
                  points: c[:points],
                  segments: c[:segments],
                  crossings: piece[:crossings] + c[:crossings],
                }
              end
            end
            pieces = new_pieces
            break if pieces.empty?
          end

          pieces
        end

        def dedupe(pieces)
          seen = {}
          pieces.select do |piece|
            key = piece.rings.map { |r| r[:points].map(&:to_a) }
            next false if seen[key]

            seen[key] = true
          end
        end
      end
    end
  end
end
