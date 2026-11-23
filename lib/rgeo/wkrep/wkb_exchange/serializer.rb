# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# WKB atom serialization for exchange packets
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Builds WKB fragments. Reusing a node's original bytes is always
      # preferred; rewriting only happens when the destination packet's fixed
      # dialect or dimension configuration differs from the source, or when an
      # explicit SRID must be stated differently.
      module Serializer
        EWKB_Z = 0x80000000
        EWKB_M = 0x40000000
        EWKB_SRID = 0x20000000

        module_function

        # Emits a leaf geometry with already-projected coordinate values.
        #
        # +shape+ is Point => [x, y, ...], LineString => [[x, y, ...], ...],
        # Polygon => [[[x, y, ...], ...], ...].
        def emit_leaf(base_type, shape, dialect:, dimensions:, little_endian:,
                      srid: nil, emit_srid: false)
          has_z = dimensions == :xyz || dimensions == :xyzm
          has_m = dimensions == :xym || dimensions == :xyzm
          little = little_endian ? true : false
          out = +""
          out << emit_header(base_type, dialect: dialect, has_z: has_z, has_m: has_m,
                             little: little, srid: srid, emit_srid: emit_srid)
          case base_type
          when 1
            out << pack_coordinates(shape, little)
          when 2
            out << [shape.size].pack(little ? "V" : "N")
            shape.each { |coords| out << pack_coordinates(coords, little) }
          when 3
            out << [shape.size].pack(little ? "V" : "N")
            shape.each do |ring|
              out << [ring.size].pack(little ? "V" : "N")
              ring.each { |coords| out << pack_coordinates(coords, little) }
            end
          else
            raise ArgumentError, "Not an atomic geometry type: #{base_type}"
          end
          out
        end

        # Emits a packet root: a GeometryCollection with the packet's dialect,
        # byte order, dimension configuration, and optional downstream SRID.
        def emit_packet_root(dialect:, little_endian:, dimensions: :xy, srid: nil, prelude_format: nil)
          little = little_endian ? true : false
          has_z = dimensions == :xyz || dimensions == :xyzm
          has_m = dimensions == :xym || dimensions == :xyzm
          out = +""
          case prelude_format
          when :uint32_le
            out << [srid || 0].pack("V")
          when :uint32_be
            out << [srid || 0].pack("N")
          end
          emit_srid = dialect == :ewkb && prelude_format == :ewkb && !srid.nil?
          out << emit_header(7, dialect: dialect, has_z: has_z, has_m: has_m,
                             little: little, srid: srid, emit_srid: emit_srid)
          out
        end

        def emit_header(base_type, dialect:, has_z:, has_m:, little:, srid: nil, emit_srid: false)
          type_code = case dialect
                      when :ewkb
                        code = base_type
                        code |= EWKB_Z if has_z
                        code |= EWKB_M if has_m
                        code
                      when :wkb12
                        base_type + (has_z ? 1000 : 0) + (has_m ? 2000 : 0)
                      when :wkb11
                        base_type
                      else
                        raise ArgumentError, "Unknown dialect #{dialect.inspect}"
                      end
          srid = nil unless emit_srid && dialect == :ewkb && !srid.nil?
          type_code |= EWKB_SRID if !srid.nil?
          out = +""
          out << [little ? 1 : 0].pack("C")
          out << [type_code].pack(little ? "V" : "N")
          out << [srid].pack(little ? "V" : "N") unless srid.nil?
          out
        end

        def pack_coordinates(coords, little)
          coords.pack(little ? "E*" : "G*")
        end
      end
    end
  end
end
