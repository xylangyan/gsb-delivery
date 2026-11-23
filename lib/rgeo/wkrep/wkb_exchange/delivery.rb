# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Delivery request for one atom and one reader
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # A Delivery is the resolved assignment of one atomic node to one reader.
      # It carries the effective SRID and the representations the business list
      # permits for that reader.
      #
      # The source representation is always available. A manifest permit adds
      # alternative representations. The planner chooses one representation per
      # packet, subject to the packet's fixed dimension configuration.
      class Delivery
        attr_reader :node, :reader, :srid, :representations, :z_purpose, :m_purpose,
                    :declared_srid

        def initialize(node:, reader:, srid:, representations: [], z_purpose: nil,
                       m_purpose: nil, declared_srid: false)
          @node = node
          @reader = reader
          @srid = srid
          @representations = representations.freeze
          @z_purpose = z_purpose
          @m_purpose = m_purpose
          @declared_srid = declared_srid
        end

        def path
          @node.path
        end

        def path_string
          @node.path_string
        end

        def declared_srid?
          @declared_srid
        end

        # The representation that preserves the source bytes exactly.
        def source_representation
          Representation.new(dimensions: [@node.dimensions], axes: {}, allow_drop: false)
        end

        # All representations that may be considered, cheapest rewrite first.
        def candidates
          ([source_representation] + @representations).uniq
        end

        # The best representation that produces +dimensions+, or nil.
        # Prefers a representation with no axis remap, then one that drops least.
        def candidate_for(dimensions)
          matches = candidates.filter_map do |rep|
            effective = rep.dimensions.empty? ? [@node.dimensions] : rep.dimensions
            next unless effective.include?(dimensions)

            remaps = rep.axes.count { |out_axis, in_axis| out_axis != in_axis }
            [rep, rep.axes.empty? ? 0 : 1, remaps, dropped_axes(rep, dimensions).size]
          end
          return nil if matches.empty?

          matches.min_by { |_rep, remapped, remaps, dropped| [remapped, remaps, dropped] }.first
        end

        # True when the source bytes can be forwarded unchanged in a packet with
        # this dimension configuration.
        def forwardable_for?(dimensions)
          @node.dialect == @reader.dialect &&
            @node.dimensions == dimensions
        end

        # The projected shape for a candidate representation.
        def shape_for(representation, dimensions)
          case @node.base_type
          when 1 then project(@node.coordinates, representation, dimensions)
          when 2 then @node.coordinates.map { |coords| project(coords, representation, dimensions) }
          when 3 then @node.rings.map do |ring|
            ring.map { |coords| project(coords, representation, dimensions) }
          end
          end
        end

        def inspect
          "#<#{self.class} path=#{path_string} reader=#{@reader.name} " \
            "from=#{@node.dimensions} srid=#{@srid.inspect}>"
        end

        private

        def dropped_axes(representation, dimensions)
          source_axes = @node.axes
          output = Axes.for_dimensions(dimensions)
          used = output.map { |axis| representation.axes.fetch(axis, axis) }.uniq
          (source_axes - %i[x y]) - used
        end

        def project(values, representation, dimensions)
          available = source_axes(values)
          output_axes = Axes.for_dimensions(dimensions)
          output_axes.map do |out_axis|
            source_axis = representation.axes.fetch(out_axis, out_axis)
            if available.key?(source_axis)
              available[source_axis]
            elsif representation.allow_drop?
              0.0
            else
              raise UnsupportedMember.new(
                path_string, @reader.name, @reader.capability_for(source_axis),
                "Member at #{path_string} has no #{source_axis} to place on #{out_axis}"
              )
            end
          end
        end

        def source_axes(values)
          available = { x: values[0], y: values[1] }
          case @node.dimensions
          when :xyz
            available[:z] = values[2]
          when :xym
            available[:m] = values[2]
          when :xyzm
            available[:z] = values[2]
            available[:m] = values[3]
          end
          available
        end
      end

      # Dimension and axis helpers shared by the planner and verifier.
      module Axes
        module_function

        def for_dimensions(dim)
          case dim
          when :xy then %i[x y]
          when :xyz then %i[x y z]
          when :xym then %i[x y m]
          when :xyzm then %i[x y z m]
          else
            raise ArgumentError, "Unknown dimension configuration #{dim.inspect}"
          end
        end

        # True when every axis of +contained+ is present in +container+.
        def covers?(container, contained)
          (for_dimensions(contained) - for_dimensions(container)).empty?
        end

        # Expands an output-axis => source-axis mapping to the source axes that
        # survive in the output.
        def surviving_source_axes(dimensions, axes)
          for_dimensions(dimensions).map { |axis| axes.fetch(axis, axis) }.uniq
        end
      end
    end
  end
end
