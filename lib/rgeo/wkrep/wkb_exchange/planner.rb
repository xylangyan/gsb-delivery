# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Cost-optimal packet planner for a WKB exchange
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Plans byte-preserving delivery of selected atomic members to downstream
      # readers.
      #
      # == Packet model
      #
      # For each reader, selected atoms are ordered by original preorder. A
      # packet covers a contiguous run of that stream. Every packet has exactly
      # one dialect, one dimension configuration, and one byte order. Its SRID is
      # either:
      #
      # * one packet-level SRID prelude (shared by all children), or
      # * no prelude, in which case EWKB children may carry explicit SRIDs and
      #   preserve distinct local SRIDs.
      #
      # An atom's bytes are forwarded unchanged whenever its source dialect,
      # dimensions, SRID, and axes match the packet. Otherwise the atom is
      # rewritten using a representation the manifest permits.
      #
      # The planner minimizes split, packet, byte, and rewrite costs with dynamic
      # programming over contiguous segments and packet configurations.
      class Planner
        attr_reader :archive, :manifest, :readers, :cost_model

        def initialize(archive:, manifest:, readers:, cost_model: CostModel.new)
          @archive = archive
          @manifest = manifest
          @readers = readers.to_h do |name, reader|
            built = reader.is_a?(Reader) ? reader : Reader.new(name: name, **reader)
            [name.to_sym, built]
          end.freeze
          @cost_model = cost_model
        end

        # Returns a Plan. Every locally unrepresentable member is reported with
        # its original collection path and the missing capability, independently
        # of whether other members could be delivered.
        def plan
          deliveries, failures = resolve_deliveries
          unless failures.empty?
            return Plan.new(packets: [], cost: Float::INFINITY, archive: @archive,
                            manifest: @manifest, readers: @readers,
                            infeasibilities: failures)
          end

          packets = []
          total = 0
          deliveries.group_by { |delivery| delivery.reader.name }.each_value do |group|
            reader = group.first.reader
            ordered = group.sort_by { |delivery| delivery.node.preorder_index }
            begin
              chosen, cost = optimize(reader, ordered)
            rescue UnsupportedMember => e
              return Plan.new(packets: [], cost: Float::INFINITY, archive: @archive,
                              manifest: @manifest, readers: @readers,
                              infeasibilities: [e])
            end
            packets.concat(chosen)
            total += cost
          end
          Plan.new(packets: packets, cost: total, archive: @archive,
                   manifest: @manifest, readers: @readers)
        end

        # Resolve every atomic leaf into Delivery objects, collecting every
        # source-local capability failure rather than stopping at the first.
        def resolve_deliveries
          deliveries = []
          failures = []
          @archive.atoms.each do |atom|
            @manifest.selections(atom.path).each do |selection|
              reader = @readers[selection.reader_name.to_sym]
              unless reader
                failures << UnsupportedMember.new(
                  atom.path_string, selection.reader_name, "unknown reader",
                  "Unknown target reader #{selection.reader_name} for member at #{atom.path_string}"
                )
                next
              end
              srid = if selection.srid_declared
                       selection.srid
                     else
                       atom.effective_srid || selection.srid
                     end
              begin
                validate_source!(atom, reader, srid, selection)
                representations = candidate_representations(atom, selection, reader)
              rescue UnsupportedMember => e
                failures << e
                next
              end
              deliveries << Delivery.new(
                node: atom,
                reader: reader,
                srid: srid,
                representations: representations,
                z_purpose: selection.z_purpose,
                m_purpose: selection.m_purpose,
                declared_srid: selection.srid_declared
              )
            end
          end
          [deliveries, failures]
        end

        private

        # Checks that do not depend on a packet choice: the reader must have at
        # least one way to carry this member's SRID, and at least one permitted
        # representation must be expressible.
        def validate_source!(atom, reader, srid, selection)
          unless reader.supports_srid?(srid)
            raise UnsupportedMember.new(
              atom.path_string, reader.name, reader.capability_for(:srid),
              "Reader #{reader.name} cannot express SRID #{srid.inspect} at #{atom.path_string}; " \
              "an outer SRID does not override this member's local SRID",
              node: atom
            )
          end

          return if representation_possible?(atom, reader, selection)

          available = reader.dimension_options.flat_map { |dim| Axes.for_dimensions(dim) }
          missing = atom.axes - available
          missing = [atom.dimensions] if missing.empty?
          raise UnsupportedMember.new(
            atom.path_string, reader.name,
            missing.map { |axis| reader.capability_for(axis) }.join(", "),
            "Reader #{reader.name} cannot express member at #{atom.path_string} " \
            "with dimensions #{atom.dimensions}",
            node: atom
          )
        end

        def representation_possible?(atom, reader, selection)
          return true if reader.supports_dimensions?(atom.dimensions)

          selection.representations.any? do |rep|
            requested = rep.dimensions.empty? ? [atom.dimensions] : rep.dimensions
            requested.any? do |dim|
              reader.supports_dimensions?(dim) &&
                representation_expressible?(atom, dim, rep.axes, rep.allow_drop?, reader)
            end
          end
        end

        # Source representation plus every manifest-permitted representation
        # that the reader can express for this member.
        def candidate_representations(atom, selection, reader)
          representations = selection.representations.filter_map do |permitted|
            requested = permitted.dimensions.empty? ? [atom.dimensions] : permitted.dimensions
            expressible = requested.select do |dim|
              reader.supports_dimensions?(dim) &&
                representation_expressible?(atom, dim, permitted.axes, permitted.allow_drop?, reader)
            end
            next if expressible.empty?

            Representation.new(
              dimensions: expressible,
              axes: permitted.axes,
              allow_drop: permitted.allow_drop?
            )
          end

          if !reader.supports_dimensions?(atom.dimensions) && representations.empty?
            raise UnsupportedMember.new(
              atom.path_string, reader.name,
              atom.axes.map { |axis| reader.capability_for(axis) }.join(", "),
              "Reader #{reader.name} has no permitted representation for member at #{atom.path_string}"
            )
          end
          representations
        end

        def representation_expressible?(atom, dimensions, axes, allow_drop, reader)
          return false unless reader.supports_dimensions?(dimensions)

          output_axes = Axes.for_dimensions(dimensions)
          source_axes = atom.axes
          axes.each do |out_axis, in_axis|
            return false unless output_axes.include?(out_axis)
            return false unless source_axes.include?(in_axis)
          end

          # Every output axis must be supplied either by a mapped source axis or
          # by an identically named source axis. An explicitly permitted
          # representation with allow_drop may fill an absent output axis with a
          # defined 0.0 rather than failing the whole member.
          output_axes.each do |out_axis|
            next if axes.key?(out_axis)
            next if source_axes.include?(out_axis)
            return false unless allow_drop
          end

          surviving = Axes.surviving_source_axes(dimensions, axes)
          dropped = (source_axes - %i[x y]) - surviving
          dropped.empty? || allow_drop
        end

        # Dynamic program over contiguous segments of one reader's stream.
        #
        # States are compared lexicographically: minimum cost first, then the
        # most original atom bytes preserved, then the fewest packets. The second
        # key is what implements "retain as much original atom data as possible"
        # without changing the minimum-cost guarantee.
        #
        # Entry cost, preserved-byte count, and emitted size are independent of
        # the segment, so they are precomputed per configuration and shared
        # through prefix sums. Segment evaluation is then O(1).
        def optimize(reader, ordered)
          return [[], 0] if ordered.empty?

          configs = packet_options(reader, ordered)
          tables = configs.to_h do |config|
            [config, build_table(reader, ordered, config)]
          end

          best = Array.new(ordered.size + 1)
          trace = Array.new(ordered.size + 1)
          best[0] = [0, 0, 0]

          (0...ordered.size).each do |start|
            next if best[start].nil?

            tables.each do |config, table|
              next unless table.feasible_from?(start)

              (start + 1..ordered.size).each do |finish|
                break unless table.feasible_range?(start, finish)

                candidate_cost, preserved = table.cost_for(
                  reader, config, start, finish, ordered, @cost_model
                )
                previous = best[start]
                candidate = [
                  previous[0] + candidate_cost,
                  previous[1] - preserved,
                  previous[2] + 1
                ]
                next unless best[finish].nil? || (candidate <=> best[finish]).negative?

                best[finish] = candidate
                trace[finish] = [start, config]
              end
            end
          end

          if best[ordered.size].nil?
            offender = ordered.first
            raise UnsupportedMember.new(
              offender.path_string, reader.name, "packet configuration",
              "No packet configuration can express member at #{offender.path_string}"
            )
          end

          packets = []
          position = ordered.size
          while position.positive?
            start, config = trace[position]
            packets.unshift(make_packet(reader, ordered[start...position], config,
                                        first_packet: start.zero?))
            position = start
          end
          [packets, best[ordered.size][0]]
        end

        # Precomputed per-delivery data for one packet configuration.
        class SegmentTable # :nodoc:
          attr_reader :entries, :costs, :preserved, :sizes

          def initialize(entries, costs, preserved, sizes)
            @entries = entries
            @costs = costs
            @preserved = preserved
            @sizes = sizes
            @prefix_cost = prefix(costs)
            @prefix_preserved = prefix(preserved)
            @prefix_size = prefix(sizes)
            @blocked = prefix(entries.map { |entry| entry.nil? ? 1 : 0 })
          end

          def feasible_range?(start, finish)
            @blocked[finish] == @blocked[start]
          end

          def feasible_from?(index)
            !@entries[index].nil?
          end

          def cost_for(reader, config, start, finish, ordered, cost_model)
            little = ordered[start].node.byte_order == :little
            prelude_format = config.prelude ? reader.prelude_format : nil
            root_size = Serializer.emit_packet_root(
              dialect: reader.dialect,
              little_endian: little,
              dimensions: config.dimensions,
              srid: config.root_srid,
              prelude_format: prelude_format
            ).bytesize
            size = root_size + 4 + (@prefix_size[finish] - @prefix_size[start])
            total = (@prefix_cost[finish] - @prefix_cost[start]) +
                    cost_model.packet_fixed(reader) +
                    (start.zero? ? 0 : cost_model.split(reader)) +
                    cost_model.packet_bytes(size, reader)
            [total, @prefix_preserved[finish] - @prefix_preserved[start]]
          end

          private

          def prefix(values)
            result = Array.new(values.size + 1, 0)
            values.each_with_index { |value, index| result[index + 1] = result[index] + value }
            result
          end
        end

        def build_table(reader, ordered, config)
          entries = []
          costs = []
          preserved = []
          sizes = []
          ordered.each do |delivery|
            entry, cost = make_entry(reader, delivery, config)
            entries << entry
            costs << (cost || 0)
            preserved << (entry&.preserved? ? entry.bytes.bytesize : 0)
            sizes << (entry ? entry.bytes.bytesize : 0)
          end
          SegmentTable.new(entries, costs, preserved, sizes)
        end

        # Enumerates the universe of viable packet configurations for a stream. A
        # configuration fixes the output dimensions, the packet-level SRID (if
        # any), and whether rewritten EWKB children may state their own SRID.
        # Per-delivery expressibility is captured in each SegmentTable.
        def packet_options(reader, ordered)
          options = []
          srids = ordered.map(&:srid).compact.uniq
          reader.dimension_options.each do |dimensions|
            root_values = [nil]
            root_values << reader.srid unless reader.srid.nil?
            root_values.concat(srids) if reader.srid_prelude?

            root_values.uniq.each do |root_srid|
              prelude = !root_srid.nil? && reader.srid_prelude?
              next if !root_srid.nil? && !prelude && reader.srid != root_srid

              child_modes = [false]
              child_modes << true if reader.accepts_child_srid?

              child_modes.each do |child_srids|
                options << Configuration.new(
                  dimensions: dimensions,
                  root_srid: root_srid,
                  prelude: prelude,
                  child_srids: child_srids
                )
              end
            end
          end
          options.uniq
        end

        Configuration = Struct.new(:dimensions, :root_srid, :prelude, :child_srids,
                                   keyword_init: true) do
          def key
            [dimensions, root_srid, prelude, child_srids]
          end
        end

        def make_packet(reader, segment, config, first_packet:)
          entries = []
          atom_cost = 0
          segment.each do |delivery|
            entry, cost = make_entry(reader, delivery, config)
            return nil if entry.nil?

            entries << entry
            atom_cost += cost
          end

          prelude_format = config.prelude ? reader.prelude_format : nil
          little = entries.empty? ? true : entries.first.node.byte_order == :little
          size = packet_size(reader, entries, config, little_endian: little)
          packet_cost = atom_cost +
                        @cost_model.packet_fixed(reader) +
                        (first_packet ? 0 : @cost_model.split(reader)) +
                        @cost_model.packet_bytes(size, reader)
          Packet.new(
            reader: reader,
            entries: entries,
            dialect: reader.dialect,
            dimensions: config.dimensions,
            srid: config.root_srid,
            prelude_format: prelude_format,
            little_endian: little,
            cost: packet_cost,
            srid_mode: srid_mode_for(entries, config)
          )
        end

        def srid_mode_for(entries, config)
          return :prelude if config.prelude
          return :child if config.child_srids && entries.any? { |entry| entry.node.explicit_srid? }

          :none
        end

        # Exact byte size of the packet without concatenating its payload.
        def packet_size(reader, entries, config, little_endian: nil)
          little = little_endian.nil? ? (entries.empty? ? true : entries.first.node.byte_order == :little) : little_endian
          prelude_format = config.prelude ? reader.prelude_format : nil
          root = Serializer.emit_packet_root(
            dialect: reader.dialect,
            little_endian: little,
            dimensions: config.dimensions,
            srid: config.root_srid,
            prelude_format: prelude_format
          )
          root.bytesize + 4 + entries.sum { |entry| entry.bytes.bytesize }
        end

        def make_entry(reader, delivery, config)
          node = delivery.node
          representation = delivery.candidate_for(config.dimensions)
          return nil if representation.nil?
          return nil unless srid_expressible?(delivery, reader, config)

          if forwardable?(node, delivery, reader, config, representation)
            return [Packet::Entry.new(
              node: node,
              bytes: node.raw,
              source: :original,
              source_dimensions: node.dimensions,
              output_dimensions: config.dimensions,
              written_srid: node.effective_srid,
              expected_srid: observed_srid(node, delivery, reader, config, preserved: true),
              expected_shape: node.coordinate_shape,
              z_purpose: delivery.z_purpose,
              m_purpose: delivery.m_purpose,
              representation: representation
            ), 0]
          end

          emit_srid = reader.dialect == :ewkb && config.child_srids &&
                      !delivery.srid.nil? && !srid_from_default?(delivery, reader, config)
          bytes = Serializer.emit_leaf(
            node.base_type,
            delivery.shape_for(representation, config.dimensions),
            dialect: reader.dialect,
            dimensions: config.dimensions,
            little_endian: node.byte_order == :little,
            srid: delivery.srid,
            emit_srid: emit_srid
          )
          cost = @cost_model.rewrite(
            dimensions_changed: node.dimensions != config.dimensions,
            srid_changed: node.effective_srid != delivery.srid,
            dialect_changed: node.dialect != reader.dialect,
            axis_remapped: !representation.axes.empty?
          )
          [Packet::Entry.new(
            node: node,
            bytes: bytes,
            source: :rewritten,
            source_dimensions: node.dimensions,
            output_dimensions: config.dimensions,
            written_srid: delivery.srid || node.effective_srid,
            expected_srid: observed_srid(node, delivery, reader, config, preserved: false,
                                         emit_srid: emit_srid),
            expected_shape: delivery.shape_for(representation, config.dimensions),
            z_purpose: delivery.z_purpose,
            m_purpose: delivery.m_purpose,
            representation: representation
          ), cost]
        end

        def srid_from_default?(delivery, reader, config)
          default = config.prelude ? config.root_srid : reader.srid
          !default.nil? && delivery.srid == default
        end

        # The SRID a child will actually observe after the packet is read.
        # A preserved child keeps any explicit SRID in its original bytes. A
        # rewritten child observes its emitted SRID only when one was written;
        # otherwise it inherits the packet-level/default SRID.
        def observed_srid(node, delivery, reader, config, preserved:, emit_srid: false)
          default = config.prelude ? config.root_srid : reader.srid
          if preserved
            if node.explicit_srid? && reader.accepts_child_srid?
              delivery.srid
            else
              default || delivery.srid
            end
          elsif emit_srid
            delivery.srid
          else
            default || delivery.srid
          end
        end

        def srid_expressible?(delivery, reader, config)
          value = delivery.srid
          return true if value.nil?

          default = config.prelude ? config.root_srid : reader.srid
          return true if !default.nil? && value == default

          # A child may state its own SRID only in EWKB and only when the packet
          # configuration allowed child SRIDs for rewrites.
          config.child_srids && reader.accepts_child_srid?
        end

        def forwardable?(node, delivery, reader, config, representation)
          return false unless reader.compatible_dialect?(node)
          return false unless node.dimensions == config.dimensions
          return false unless representation.axes.empty?

          default = config.prelude ? config.root_srid : reader.srid
          if node.explicit_srid?
            # An explicit child SRID can be forwarded when the reader can read
            # EWKB child SRIDs and the child states the SRID the manifest asks
            # for. It may intentionally differ from the packet-level SRID.
            reader.accepts_child_srid? && node.effective_srid == delivery.srid
          else
            # No SRID bytes on the child: the packet-level/default SRID is what
            # the reader will observe. A reader that assumes no SRID at all can
            # still take the child unchanged when nothing declares one.
            default.nil? ? delivery.srid.nil? : delivery.srid == default
          end
        end
      end
    end
  end
end
