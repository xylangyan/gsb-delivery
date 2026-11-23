# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Strict byte-level parser for WKB archives
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Internal parser used by Archive. It consumes the source byte string and
      # builds Nodes with exact spans. Parent links and preorder indexes are
      # assigned after the tree is complete.
      class Parser # :nodoc:
        EWKB_Z = 0x80000000
        EWKB_M = 0x40000000
        EWKB_SRID = 0x20000000
        EWKB_MASK = 0x0fffffff

        Context = Struct.new(:effective_srid, keyword_init: true)

        def initialize(archive, dialect: :auto, ignore_extra_bytes: false, max_depth: 1024)
          @archive = archive
          @data = archive.bytes
          @len = @data.bytesize
          @pos = 0
          @dialect = dialect
          @ignore_extra_bytes = ignore_extra_bytes
          @max_depth = max_depth
        end

        def parse
          node = parse_node(parent_context: nil, path: [], depth: 0)
          if !@ignore_extra_bytes && @pos < @len
            raise Error::ParseError, "Found #{@len - @pos} extra bytes at the end of the WKB archive."
          end
          node
        end

        private

        def parse_node(parent_context:, path:, depth:)
          raise Error::ParseError, "WKB nesting exceeds #{@max_depth} levels" if depth > @max_depth

          start = @pos
          order = read_byte
          little = case order
                   when 0 then false
                   when 1 then true
                   else
                     raise Error::ParseError, "Bad WKB endian byte value: #{order} at #{format_path(path)}"
                   end
          raw_type = read_uint32(little)
          dialect = detect_dialect(raw_type)
          if @dialect != :auto && @dialect != dialect
            raise Error::ParseError,
                  "WKB member at #{format_path(path)} uses #{dialect} but #{@dialect} was required"
          end

          has_z = false
          has_m = false
          explicit_srid = nil
          type_code = raw_type
          case dialect
          when :ewkb
            has_z = (type_code & EWKB_Z) != 0
            has_m = (type_code & EWKB_M) != 0
            explicit_srid = read_uint32(little) if (type_code & EWKB_SRID) != 0
            type_code &= EWKB_MASK
          when :wkb12
            has_z = ((type_code / 1000) & 1) != 0
            has_m = ((type_code / 1000) & 2) != 0
            type_code %= 1000
          end

          unless (1..7).cover?(type_code)
            raise Error::ParseError, "Unknown WKB type value #{raw_type} at #{format_path(path)}"
          end

          header_end = @pos
          effective_srid = explicit_srid
          effective_srid = parent_context.effective_srid if explicit_srid.nil? && parent_context
          effective_srid = @archive.srid if effective_srid.nil?

          node = Node.new(
            archive: @archive,
            parent: nil,
            path: path,
            offset: start,
            header_end: header_end,
            byte_order: little ? :little : :big,
            type_code: raw_type,
            base_type: type_code,
            dialect: dialect,
            has_z: has_z,
            has_m: has_m,
            explicit_srid: explicit_srid,
            effective_srid: effective_srid
          )

          context = Context.new(effective_srid: effective_srid)
          case type_code
          when 1
            node.coordinates = read_coordinates(little, has_z, has_m, path)
          when 2
            node.coordinates = read_sequence(little, has_z, has_m, path)
          when 3
            node.rings = read_rings(little, has_z, has_m, path)
          when 4, 5, 6
            expected = { 4 => 1, 5 => 2, 6 => 3 }.fetch(type_code)
            node.children = read_children(little, path, depth, expected, context)
          when 7
            node.children = read_children(little, path, depth, nil, context)
          end

          node.length = @pos - start
          @archive.register(node)
          node
        end

        def read_children(little, path, depth, expected_base_type, parent_context)
          count = read_uint32(little)
          if count > @len
            raise Error::ParseError, "Implausible child count #{count} at #{format_path(path)}"
          end
          children = Array.new(count) do |index|
            parse_node(parent_context: parent_context, path: path + [index], depth: depth + 1)
          end
          if expected_base_type
            children.each_with_index do |child, index|
              next if child.base_type == expected_base_type
              raise Error::ParseError,
                    "Enclosed type #{child.base_type} at #{format_path(path + [index])} does not match " \
                    "the required member type #{expected_base_type}"
            end
          end
          children
        end

        def read_sequence(little, has_z, has_m, path)
          count = read_uint32(little)
          if count > @len
            raise Error::ParseError, "Implausible coordinate count #{count} at #{format_path(path)}"
          end
          Array.new(count) { read_coordinates(little, has_z, has_m, path) }
        end

        def read_rings(little, has_z, has_m, path)
          count = read_uint32(little)
          if count > @len
            raise Error::ParseError, "Implausible ring count #{count} at #{format_path(path)}"
          end
          Array.new(count) { read_sequence(little, has_z, has_m, path) }
        end

        def read_coordinates(little, has_z, has_m, path)
          values = Array.new(2) { read_double(little, path) }
          values << read_double(little, path) if has_z
          values << read_double(little, path) if has_m
          values
        end

        def read_byte
          raise Error::ParseError, "Not enough bytes left to fulfill 1 byte" if @pos + 1 > @len
          value = @data.getbyte(@pos)
          @pos += 1
          value
        end

        def read_uint32(little)
          raise Error::ParseError, "Not enough bytes left to fulfill 1 integer" if @pos + 4 > @len
          value = @data.byteslice(@pos, 4).unpack1(little ? "V" : "N")
          @pos += 4
          value
        end

        def read_double(little, path)
          if @pos + 8 > @len
            raise Error::ParseError, "Not enough bytes left to fulfill 1 double at #{format_path(path)}"
          end
          value = @data.byteslice(@pos, 8).unpack1(little ? "E" : "G")
          @pos += 8
          value
        end

        def detect_dialect(raw_type)
          return :ewkb if (raw_type & (EWKB_Z | EWKB_M | EWKB_SRID)) != 0
          return :wkb12 if raw_type >= 1000
          :wkb11
        end

        def format_path(path)
          return "/" if path.empty?
          "/#{path.join('/')}"
        end
      end
    end
  end
end
