# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# One destination packet produced by the exchange planner
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # A packet is a standalone WKB GeometryCollection addressed to one reader.
      # Its dialect, dimension configuration, byte order, and optional SRID
      # prelude are fixed; entries appear in original collection order.
      class Packet
        # One atom inside a packet.
        class Entry
          attr_reader :node, :bytes, :source, :source_dimensions, :output_dimensions,
                      :written_srid, :expected_srid, :expected_shape,
                      :z_purpose, :m_purpose, :representation

          def initialize(node:, bytes:, source:, source_dimensions:, output_dimensions:,
                         written_srid:, expected_srid:, expected_shape:,
                         z_purpose: nil, m_purpose: nil, representation: nil)
            @node = node
            @bytes = bytes
            @source = source
            @source_dimensions = source_dimensions
            @output_dimensions = output_dimensions
            @written_srid = written_srid
            @expected_srid = expected_srid
            @expected_shape = expected_shape
            @z_purpose = z_purpose
            @m_purpose = m_purpose
            @representation = representation
          end

          def path
            @node.path
          end

          def path_string
            @node.path_string
          end

          def rewritten?
            @source != :original
          end

          def preserved?
            @source == :original
          end
        end

        attr_reader :reader, :entries, :dialect, :dimensions, :srid,
                    :prelude_format, :little_endian, :cost, :srid_mode

        def initialize(reader:, entries:, dialect:, dimensions:, srid:,
                       prelude_format:, little_endian:, cost: 0, srid_mode: :none)
          @reader = reader
          @entries = entries
          @dialect = dialect
          @dimensions = dimensions
          @srid = srid
          @prelude_format = prelude_format
          @little_endian = little_endian ? true : false
          @cost = cost
          @srid_mode = srid_mode
          @bytes = nil
        end

        def bytes
          @bytes ||= build_bytes
        end

        def reader_name
          @reader.name
        end

        def num_entries
          @entries.size
        end

        def paths
          @entries.map(&:path)
        end

        def rewritten_count
          @entries.count(&:rewritten?)
        end

        def preserved_count
          @entries.count(&:preserved?)
        end

        # True when distinct explicit child SRIDs are allowed in this packet.
        def child_srid_mode?
          @srid_mode == :child
        end

        def inspect
          "#<#{self.class} reader=#{reader_name} dialect=#{@dialect} dims=#{@dimensions} " \
            "entries=#{@entries.size} bytes=#{bytes.bytesize} cost=#{@cost}>"
        end

        private

        def build_bytes
          out = +""
          out << Serializer.emit_packet_root(
            dialect: @dialect,
            little_endian: @little_endian,
            dimensions: @dimensions,
            srid: @srid,
            prelude_format: @prelude_format
          )
          out << [@entries.size].pack(@little_endian ? "V" : "N")
          @entries.each { |entry| out << entry.bytes }
          out
        end
      end
    end
  end
end
