# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # ESRI MultiPatch part type codes (shape type 31).
      PART_TYPES = {
        triangle_strip: 0,
        triangle_fan: 1,
        outer_ring: 2,
        inner_ring: 3,
        first_ring: 4,
        ring: 5
      }.freeze

      # A faithful, triangulation-free view of one MultiPatch record part.
      # points is an array of RGeo z-points in recorded order; kind is one
      # of the PART_TYPES keys; record_index and part_index keep the
      # provenance back to the original shapefile record.
      class RawPart
        attr_reader :kind, :points, :record_index, :part_index, :m_values

        def initialize(kind, points, record_index, part_index, m_values)
          @kind = kind
          @points = points
          @record_index = record_index
          @part_index = part_index
          @m_values = m_values
        end

        def ring?
          %i[outer_ring inner_ring first_ring ring].include?(@kind)
        end

        def triangle_part?
          @kind == :triangle_strip || @kind == :triangle_fan
        end
      end

      # Decodes the binary payload of a shape type 31 record into RawParts.
      # Every part keeps its recorded point order: triangle strips and fans
      # are triangulated directly later (they may run along a folded,
      # non-planar surface), while ring parts are grouped into planar
      # faces. No points are dropped and no geometry is merged.
      class PartDecoder
        NODATA_LIMIT = -1e38

        def initialize(factory)
          @factory = factory
        end

        def decode(data, record_index)
          num_parts, num_points = data[36, 8].unpack("VV")
          values = data[44, 32 + (num_parts * 8) + (num_points * 32)]
                   .unpack("V#{num_parts * 2}E*")

          part_indexes = values.slice!(0, num_parts) + [num_points]
          part_type_codes = values.slice!(0, num_parts)
          xys = values.slice!(0, num_points * 2)
          zs = values.slice!(2, num_points)
          ms = values.slice!(4, num_points)
          zs.map! { |value| value < NODATA_LIMIT ? 0.0 : value } if zs
          ms.map! { |value| value < NODATA_LIMIT ? 0.0 : value } if ms

          points = (0...num_points).map do |index|
            @factory.point(xys[index * 2], xys[(index * 2) + 1], zs[index])
          end

          (0...num_parts).map do |part_index|
            range = part_indexes[part_index]...part_indexes[part_index + 1]
            kind = PART_TYPES.key(part_type_codes[part_index])
            RawPart.new(kind, points[range], record_index, part_index,
                        ms ? ms[range] : [])
          end
        end
      end
    end
  end
end
