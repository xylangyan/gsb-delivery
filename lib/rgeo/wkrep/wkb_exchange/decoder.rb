# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Independent re-reader for produced exchange packets
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Decodes a packet produced by the planner without using RGeo factories.
      # It exists so Plan#verify! can prove the exact coordinates, dimensions,
      # SRIDs, and order that a downstream reader will observe.
      class Decoder # :nodoc:
        PRELUDE_FORMATS = %i[uint32_le uint32_be].freeze

        def initialize(data, prelude_format: nil, dialect: nil, default_srid: nil)
          @data = data.b
          @pos = 0
          @prelude_format = prelude_format
          @dialect = dialect
          @default_srid = default_srid
        end

        # Returns an Array of member hashes:
        #   { type:, dimensions:, srid:, shape: }
        def read
          read_prelude
          order = read_byte
          little = case order
                   when 0 then false
                   when 1 then true
                   else raise Error::ParseError, "Bad packet endian byte #{order}"
                   end
          raw_type = read_uint32(little)
          dialect, has_z, has_m, explicit_srid, base = parse_type(raw_type, little)
          if dialect == :ewkb && !explicit_srid.nil? && base == 7
            packet_srid = explicit_srid
          else
            packet_srid = explicit_srid || @prelude_srid
          end
          packet_srid ||= @default_srid
          unless base == 7
            raise Error::ParseError, "Packet root type #{base} is not GeometryCollection"
          end
          count = read_uint32(little)
          Integer(count).times.map { read_member(packet_srid) }
        end

        private

        def read_prelude
          return unless PRELUDE_FORMATS.include?(@prelude_format)

          @prelude_srid = read_uint32(@prelude_format == :uint32_le)
        end

        def read_member(packet_srid)
          order = read_byte
          little = case order
                   when 0 then false
                   when 1 then true
                   else raise Error::ParseError, "Bad member endian byte #{order}"
                   end
          raw_type = read_uint32(little)
          _dialect, has_z, has_m, explicit_srid, base = parse_type(raw_type, little)
          if @dialect && !dialect_matches?(raw_type, @dialect)
            raise Error::ParseError, "Packet member uses a dialect other than #{@dialect}"
          end
          unless base.between?(1, 3)
            raise Error::ParseError, "Packet member type #{base} is not atomic"
          end
          shape = case base
                  when 1 then read_coordinates(little, has_z, has_m)
                  when 2 then read_sequence(little, has_z, has_m)
                  when 3 then read_polygon(little, has_z, has_m)
                  end
          {
            type: base,
            dimensions: dimension_name(has_z, has_m),
            srid: explicit_srid || packet_srid,
            shape: shape,
            byte_order: little ? :little : :big,
            explicit_srid: !explicit_srid.nil?
          }
        end

        def parse_type(raw_type, little)
          dialect = detect_dialect(raw_type)
          type = raw_type
          has_z = false
          has_m = false
          explicit_srid = nil
          case dialect
          when :ewkb
            has_z = (type & Serializer::EWKB_Z) != 0
            has_m = (type & Serializer::EWKB_M) != 0
            explicit_srid = read_uint32(little) if (type & Serializer::EWKB_SRID) != 0
            type &= 0x0fffffff
          when :wkb12
            has_z = ((type / 1000) & 1) != 0
            has_m = ((type / 1000) & 2) != 0
            type %= 1000
          end
          [dialect, has_z, has_m, explicit_srid, type]
        end

        def detect_dialect(raw_type)
          return :ewkb if (raw_type & (Serializer::EWKB_Z | Serializer::EWKB_M | Serializer::EWKB_SRID)) != 0
          return :wkb12 if raw_type >= 1000
          :wkb11
        end

        def dialect_matches?(raw_type, expected)
          actual = detect_dialect(raw_type)
          return true if actual == expected

          # A plain XY type code has the same bits in WKB 1.1, WKB 1.2 and EWKB.
          # Standard readers accept it as WKB, and EWKB readers accept it as an
          # EWKB child with no Z/M/SRID flags.
          compact = raw_type & 0x0fffffff
          return true if compact < 1000

          false
        end

        def dimension_name(has_z, has_m)
          if has_z && has_m
            :xyzm
          elsif has_z
            :xyz
          elsif has_m
            :xym
          else
            :xy
          end
        end

        def read_sequence(little, has_z, has_m)
          count = read_uint32(little)
          Array.new(count) { read_coordinates(little, has_z, has_m) }
        end

        def read_polygon(little, has_z, has_m)
          count = read_uint32(little)
          Array.new(count) { read_sequence(little, has_z, has_m) }
        end

        def read_coordinates(little, has_z, has_m)
          count = 2 + (has_z ? 1 : 0) + (has_m ? 1 : 0)
          if @pos + 8 * count > @data.bytesize
            raise Error::ParseError, "Packet ended while reading #{count} coordinates"
          end
          values = @data.byteslice(@pos, 8 * count).unpack(little ? "E*" : "G*")
          @pos += 8 * count
          values
        end

        def read_byte
          raise Error::ParseError, "Packet ended while reading a byte" if @pos + 1 > @data.bytesize
          value = @data.getbyte(@pos)
          @pos += 1
          value
        end

        def read_uint32(little)
          raise Error::ParseError, "Packet ended while reading an integer" if @pos + 4 > @data.bytesize
          value = @data.byteslice(@pos, 4).unpack1(little ? "V" : "N")
          @pos += 4
          value
        end
      end
    end
  end
end
