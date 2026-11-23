# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Result of planning a WKB exchange
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Exposes the packets, the minimum total cost, and an auditable account of
      # which atoms were forwarded unchanged or rewritten.
      class Plan
        attr_reader :packets, :cost, :archive, :manifest, :readers, :infeasibilities

        def initialize(packets:, cost:, archive:, manifest:, readers:, infeasibilities: [])
          @packets = packets
          @cost = cost
          @archive = archive
          @manifest = manifest
          @readers = readers
          @infeasibilities = infeasibilities
        end

        def feasible?
          @infeasibilities.empty?
        end

        def each_packet(&)
          return enum_for(:each_packet) unless block_given?
          @packets.each(&)
          self
        end

        # Packets addressed to one reader, in packet order.
        def packets_for(reader)
          name = reader.respond_to?(:name) ? reader.name : reader.to_s
          @packets.select { |packet| packet.reader_name == name }
        end

        def reader_names
          @packets.map(&:reader_name).uniq
        end

        def num_packets
          @packets.size
        end

        def rewritten_count
          @packets.sum(&:rewritten_count)
        end

        def preserved_count
          @packets.sum(&:preserved_count)
        end

        def bytes
          @packets.map(&:bytes)
        end

        # Re-reads every produced packet and verifies the exact ordered shape,
        # dimensions, byte order, and SRID. Raises with a precise entry path on
        # mismatch.
        def verify!
          if feasible?
            @packets.each { |packet| verify_packet!(packet) }
            return true
          end

          first = @infeasibilities.first
          raise first if first

          raise Error::ParseError, "Plan is infeasible"
        end

        def to_h
          {
            cost: @cost,
            feasible: feasible?,
            infeasibilities: @infeasibilities.map(&:to_h),
            packets: @packets.map do |packet|
              {
                reader: packet.reader_name,
                dialect: packet.dialect,
                dimensions: packet.dimensions,
                srid: packet.srid,
                prelude_format: packet.prelude_format,
                srid_mode: packet.srid_mode,
                bytes: packet.bytes.bytesize,
                cost: packet.cost,
                entries: packet.entries.map do |entry|
                  {
                    path: entry.path_string,
                    type: entry.node.geometry_type,
                    preserved: entry.preserved?,
                    rewritten: entry.rewritten?,
                    source_dimensions: entry.source_dimensions,
                    output_dimensions: entry.output_dimensions,
                    source_srid: entry.node.effective_srid,
                    written_srid: entry.written_srid,
                    z_purpose: entry.z_purpose,
                    m_purpose: entry.m_purpose
                  }
                end
              }
            end
          }
        end

        private

        def verify_packet!(packet)
          decoded = Decoder.new(
            packet.bytes,
            prelude_format: packet.prelude_format,
            dialect: packet.dialect,
            default_srid: packet.srid || packet.reader.srid
          ).read
          unless decoded.size == packet.entries.size
            raise Error::ParseError,
                  "Packet for #{packet.reader_name} decoded #{decoded.size} members, " \
                  "expected #{packet.entries.size}"
          end
          packet.entries.each_with_index do |entry, index|
            verify_entry!(packet, entry, decoded[index], index)
          end
        end

        def verify_entry!(packet, entry, actual, index)
          unless actual[:dimensions] == entry.output_dimensions
            raise Error::ParseError,
                  "Packet for #{packet.reader_name} member #{index} at #{entry.path_string} decoded " \
                  "dimensions #{actual[:dimensions]}, expected #{entry.output_dimensions}"
          end

          expected_srid = entry.expected_srid
          expected_srid = packet.srid if expected_srid.nil?
          unless actual[:srid] == expected_srid
            raise Error::ParseError,
                  "Packet for #{packet.reader_name} member #{index} at #{entry.path_string} decoded " \
                  "SRID #{actual[:srid].inspect}, expected #{expected_srid.inspect}"
          end

          expected_shape = entry.preserved? ? entry.node.coordinate_shape : entry.expected_shape
          unless shapes_match?(actual[:shape], expected_shape)
            raise Error::ParseError,
                  "Packet for #{packet.reader_name} member #{index} at #{entry.path_string} decoded " \
                  "coordinates #{actual[:shape].inspect}, expected #{expected_shape.inspect}"
          end

          return unless entry.preserved?

          expected_order = entry.node.byte_order
          unless actual[:byte_order] == expected_order
            raise Error::ParseError,
                  "Packet for #{packet.reader_name} member #{index} at #{entry.path_string} decoded " \
                  "byte order #{actual[:byte_order]}, expected preserved #{expected_order}"
          end
        end

        def shapes_match?(actual, expected)
          if expected.is_a?(Array) && expected.first.is_a?(Numeric)
            return false unless actual.is_a?(Array) && actual.size == expected.size
            actual.zip(expected).all? { |a, e| (a - e).abs <= 1e-12 }
          elsif expected.is_a?(Array)
            return false unless actual.is_a?(Array) && actual.size == expected.size
            actual.zip(expected).all? { |a, e| shapes_match?(a, e) }
          else
            false
          end
        end
      end
    end
  end
end
