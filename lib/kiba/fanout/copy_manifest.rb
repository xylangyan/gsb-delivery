require "kiba/fanout/path"

module Kiba
  module Fanout
    class ManifestError < StandardError; end

    # Fixed billable byte sizes for logical detail nodes.
    #
    # Only detail nodes are declared. Field names, references to the same node
    # and read-only originals are never billable. A node marked `whole` must be
    # copied as one piece and cannot be split into cheaper children.
    class CopyManifest
      Node = Struct.new(:segments, :bytes, :whole, keyword_init: true) do
        def size
          segments.size
        end

        def whole?
          whole
        end

        def path
          segments.join(".")
        end
      end

      attr_reader :nodes

      def self.build(&block)
        manifest = new
        manifest.instance_eval(&block) if block
        manifest
      end

      def initialize
        @nodes = []
      end

      def node(path, bytes:, whole: false)
        unless bytes.is_a?(Integer) && bytes >= 0
          raise ManifestError, "bytes must be a non-negative Integer"
        end

        segments = Path.split(path)
        raise ManifestError, "empty node path" if segments.empty?

        @nodes << Node.new(segments: segments, bytes: bytes, whole: whole)
        self
      end

      def node_for(path)
        segments = Path.split(path)
        @nodes.find { |node| node.segments == segments }
      end

      # Manifest nodes whose storage covers a concrete path.
      #
      # * A `whole` ancestor or the node itself wins; no descendant is returned
      #   because the whole node is indivisible.
      # * Otherwise the nearest ancestor wins.
      # * If the path is above the declared nodes, include the whole nodes
      #   below it (or the nearest descendants).
      def coverings(path)
        segments = Path.split(path)
        ancestors = @nodes.select do |node|
          node.size <= segments.size && prefix?(node.segments, segments)
        end
        whole_ancestor = ancestors.select(&:whole?).max_by(&:size)
        return [whole_ancestor] if whole_ancestor

        nearest = ancestors.max_by(&:size)
        return [nearest] if nearest

        descendants = @nodes.select do |node|
          node.size > segments.size && prefix?(segments, node.segments)
        end
        return [] if descendants.empty?

        whole = descendants.select(&:whole?).min_by(&:size)
        return [whole] if whole

        nearest_size = descendants.map(&:size).min
        descendants.select { |node| node.size == nearest_size }
      end

      def billable(path)
        node = coverings(path).first
        node && node.bytes
      end

      def empty?
        @nodes.empty?
      end

      def total_declared_bytes
        @nodes.sum(&:bytes)
      end

      private

      def prefix?(pattern, segments)
        pattern.each_with_index.all? do |segment, index|
          segment == "*" || segment == segments[index]
        end
      end
    end
  end
end
