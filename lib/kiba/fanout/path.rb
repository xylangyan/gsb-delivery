module Kiba
  module Fanout
    # Dotted field paths. "*" matches one array index or one hash key.
    #
    # Work-order paths are patterns; concrete paths use actual keys/indexes.
    # A row may use string or symbol hash keys.
    module Path
      module_function

      def split(path)
        case path
        when Array
          path.map(&:to_s)
        else
          path.to_s.split(".").reject(&:empty?)
        end
      end

      def join(path)
        split(path).join(".")
      end

      def match?(pattern, path)
        pattern = split(pattern)
        path = split(path)
        return false unless pattern.size == path.size

        matches?(pattern, path)
      end

      def ancestor?(pattern, path)
        pattern = split(pattern)
        path = split(path)
        return false unless pattern.size < path.size

        matches?(pattern, path)
      end

      def at_or_below?(pattern, path)
        pattern = split(pattern)
        path = split(path)
        return false if pattern.size < path.size

        matches?(path, pattern)
      end

      def at_or_above?(pattern, path)
        match?(pattern, path) || ancestor?(pattern, path)
      end

      def overlaps?(left, right)
        at_or_above?(left, right) || at_or_above?(right, left)
      end

      def common_prefix(paths)
        values = paths.map { |path| split(path) }
        return [] if values.empty?

        values.map(&:size).min.times do |index|
          return values.first.first(index) unless values.map { |path| path[index] }.uniq.size == 1
        end
        values.first
      end

      def each_value(value, pattern, &block)
        return enum_for(:each_value, value, pattern) unless block

        each_concrete(value, pattern) do |_keys, matched, complete|
          block.call(matched) if complete
        end
      end

      def fetch(value, pattern)
        each_value(value, pattern).first
      end

      # Walk a pattern and yield [keys, value, complete].
      #
      # `keys` are the real keys/indexes, so a caller can write the same
      # location back without changing string keys into symbols. When part of
      # the pattern is missing, the deepest existing container is yielded with
      # complete=false. That is enough for a write into a new field: the
      # containing detail node still has to be copied.
      def each_concrete(root, pattern, &block)
        return enum_for(:each_concrete, root, pattern) unless block

        segments = split(pattern)
        raise ArgumentError, "empty path" if segments.empty?

        walk(root, segments, [], block)
      end

      def dig_keys(value, keys)
        keys.reduce(value) do |current, key|
          case current
          when Hash
            if current.key?(key)
              current[key]
            elsif key.is_a?(String) && current.key?(key.to_sym)
              current[key.to_sym]
            elsif key.is_a?(Symbol) && current.key?(key.to_s)
              current[key.to_s]
            end
          when Array
            key.is_a?(Integer) ? current[key] : nil
          end
        end
      end

      def assign_at!(root, keys, value)
        return value if keys.empty?
        return assign_key!(root, keys.first, value) if keys.size == 1

        parent = dig_keys(root, keys[0...-1])
        return root if parent.nil?

        assign_key!(parent, keys.last, value)
        root
      end

      def assign_key!(container, key, value)
        case container
        when Hash
          if container.key?(key)
            container[key] = value
          elsif key.is_a?(String) && container.key?(key.to_sym)
            container[key.to_sym] = value
          elsif key.is_a?(Symbol) && container.key?(key.to_s)
            container[key.to_s] = value
          else
            container[key] = value
          end
        when Array
          container[key] = value if key.is_a?(Integer)
        end
        container
      end

      def matches?(pattern, path)
        pattern.each_with_index.all? do |segment, index|
          segment == "*" || segment == path[index]
        end
      end
      private_class_method :matches?

      def walk(value, segments, keys, block)
        if segments.empty?
          block.call(keys, value, true)
          return
        end

        segment = segments.first
        rest = segments.drop(1)
        case value
        when Hash
          if segment == "*"
            if value.empty?
              block.call(keys, value, false)
            else
              value.each { |key, child| walk(child, rest, keys + [key], block) }
            end
          else
            key = resolve_key(value, segment)
            if key == :__missing__
              block.call(keys, value, false)
            else
              walk(value[key], rest, keys + [key], block)
            end
          end
        when Array
          if segment == "*"
            if value.empty?
              block.call(keys, value, false)
            else
              value.each_with_index { |child, index| walk(child, rest, keys + [index], block) }
            end
          elsif segment.match?(/\A\d+\z/) && segment.to_i < value.size
            index = segment.to_i
            walk(value[index], rest, keys + [index], block)
          else
            block.call(keys, value, false)
          end
        else
          block.call(keys, value, false)
        end
      end

      def resolve_key(hash, segment)
        return segment if hash.key?(segment)
        return segment.to_sym if hash.key?(segment.to_sym)

        :__missing__
      end
      private_class_method :resolve_key
    end
  end
end
