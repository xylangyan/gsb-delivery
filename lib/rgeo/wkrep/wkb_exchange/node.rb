# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# A byte-preserving node in a WKB archive
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # One geometry node in an Archive. Unlike a normal geometry object, a node
      # retains the byte span it occupied in the source and enough metadata to
      # forward it unchanged to another reader.
      class Node
        TYPES = {
          1 => :point,
          2 => :line_string,
          3 => :polygon,
          4 => :multi_point,
          5 => :multi_line_string,
          6 => :multi_polygon,
          7 => :geometry_collection
        }.freeze

        TYPE_NAMES = {
          1 => "Point",
          2 => "LineString",
          3 => "Polygon",
          4 => "MultiPoint",
          5 => "MultiLineString",
          6 => "MultiPolygon",
          7 => "GeometryCollection"
        }.freeze

        attr_reader :archive, :parent, :path, :preorder_index,
                    :offset, :header_end, :byte_order,
                    :type_code, :base_type, :dialect, :has_z, :has_m,
                    :explicit_srid, :effective_srid, :coordinates, :rings
        attr_accessor :children, :length

        def initialize(archive:, parent:, path:, offset:, header_end:,
                       byte_order:, type_code:, base_type:, dialect:, has_z:, has_m:,
                       explicit_srid:, effective_srid:)
          @archive = archive
          @parent = parent
          @path = path.freeze
          @preorder_index = nil
          @offset = offset
          @length = header_end - offset
          @header_end = header_end
          @byte_order = byte_order
          @type_code = type_code
          @base_type = base_type
          @dialect = dialect
          @has_z = has_z
          @has_m = has_m
          @explicit_srid = explicit_srid
          @effective_srid = effective_srid
          @children = []
          @coordinates = nil
          @rings = nil
        end

        # The exact original bytes for this node.
        def raw
          @raw ||= @archive.bytes.byteslice(@offset, @length)
        end
        alias to_bytes raw

        def preorder_index=(value)
          @preorder_index = value
        end

        def coordinates=(value)
          @coordinates = value
        end

        def rings=(value)
          @rings = value
        end

        # The geometry type, e.g. :point or :geometry_collection.
        def geometry_type
          TYPES[@base_type]
        end
        alias type geometry_type

        def type_name
          TYPE_NAMES[@base_type]
        end

        # True when this node has contained geometry nodes.
        def composite?
          @base_type >= 4
        end
        alias collection? composite?

        # Atomic members are the routable units.
        def leaf?
          @base_type.between?(1, 3)
        end

        def has_z?
          @has_z
        end

        def has_m?
          @has_m
        end

        def dimensions
          if @has_z && @has_m
            :xyzm
          elsif @has_z
            :xyz
          elsif @has_m
            :xym
          else
            :xy
          end
        end
        alias native_dimensions dimensions

        # Axes in their WKB coordinate order.
        def axes
          result = %i[x y]
          result << :z if @has_z
          result << :m if @has_m
          result
        end

        def explicit_srid?
          !@explicit_srid.nil?
        end

        def inherited_srid?
          @explicit_srid.nil?
        end

        def path_string
          return "/" if @path.empty?
          "/#{@path.join('/')}"
        end

        def root?
          @parent.nil?
        end

        def each_descendant(&)
          return enum_for(:each_descendant) unless block_given?
          @children.each do |child|
            yield child
            child.each_descendant(&)
          end
          self
        end

        def descendants
          each_descendant.to_a
        end

        # Nested coordinate shape, matching the geometry type:
        # Point => [x, y, ...]
        # LineString => [[x, y, ...], ...]
        # Polygon => [[[x, y, ...], ...], ...]
        def coordinate_shape
          case @base_type
          when 1 then @coordinates
          when 2 then @coordinates
          when 3 then @rings
          end
        end

        def inspect
          "#<#{self.class} path=#{path_string.inspect} type=#{type_name} " \
            "dialect=#{@dialect} dims=#{dimensions} srid=#{@effective_srid.inspect} " \
            "offset=#{@offset} length=#{@length}>"
        end
      end
    end
  end
end
