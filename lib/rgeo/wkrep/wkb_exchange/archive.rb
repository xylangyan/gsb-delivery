# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# A parsed, byte-preserving WKB/EWKB archive
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Parses WKB or EWKB into a tree of Node objects.
      #
      # Children may change byte order, may carry an explicit SRID even when their
      # parent does not, and may have a different dimension configuration when the
      # underlying format allows it. The archive retains all of those facts.
      class Archive
        attr_reader :bytes, :root, :srid

        def self.parse(data, srid: nil, **options)
          new(data, srid: srid, **options)
        end

        def initialize(data, srid: nil, dialect: :auto, ignore_extra_bytes: false, max_depth: 1024)
          @bytes = data.b
          @srid = srid
          @nodes = []
          parser = Parser.new(self, dialect: dialect, ignore_extra_bytes: ignore_extra_bytes,
                              max_depth: max_depth)
          @root = parser.parse
          finalize_tree(@root)
          @atoms = @nodes.select(&:leaf?)
        end

        # All nodes in preorder (the root first).
        attr_reader :nodes, :atoms

        def each_node(&)
          return enum_for(:each_node) unless block_given?
          @nodes.each(&)
          self
        end

        # Atomic members are leaves: Point, LineString, or Polygon.
        def each_atom(&)
          return enum_for(:each_atom) unless block_given?
          @atoms.each(&)
          self
        end

        # Paths are zero-based child indexes from the root, e.g. [1, 0].
        def node_at(path)
          path.reduce(@root) do |node, index|
            return nil unless node
            node.children[index]
          end
        end

        def node_by_preorder(index)
          @nodes[index]
        end

        def size
          @nodes.size
        end

        def register(node)
          @nodes << node
          node
        end

        # Counts leaves below each composite node so structural validation can be
        # expressed without re-walking.
        def atom_count
          @atoms.size
        end

        # A compact description of the tree, useful for diagnostics.
        def tree_description
          lines = []
          walk = lambda do |node, depth|
            lines << "#{'  ' * depth}#{node.type_name} #{node.path.inspect} " \
              "dialect=#{node.dialect} dims=#{node.dimensions} srid=#{node.effective_srid.inspect} " \
              "explicit_srid=#{node.explicit_srid?}"
            node.children.each { |child| walk.call(child, depth + 1) }
          end
          walk.call(@root, 0)
          lines.join("\n")
        end

        private

        def finalize_tree(root)
          ordered = []
          walk = lambda do |node, parent|
            node.instance_variable_set(:@parent, parent)
            node.preorder_index = ordered.size
            ordered << node
            node.children.each { |child| walk.call(child, node) }
          end
          walk.call(root, nil)
          @nodes = ordered
          root
        end
      end
    end
  end
end
