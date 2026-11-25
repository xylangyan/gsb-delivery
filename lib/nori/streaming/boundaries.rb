class Nori
  module Streaming
    # A single commit point. A boundary is described by the raw XML element
    # name (including any namespace prefix, before Nori's key normalization)
    # and, optionally, the directory hierarchy it must appear in.
    class Boundary
      WILDCARD = "*".freeze

      attr_reader :path, :name, :depth, :mode, :alternate_paths, :alternate_names

      def initialize(spec)
        spec = { :name => spec } unless spec.is_a?(Hash)
        @name = string_or_nil(spec[:name] || spec[:element] || spec[:tag] || spec[:record])
        path = spec[:path] || spec[:paths] || spec[:parents] || spec[:ancestors] ||
               spec[:hierarchy] || spec[:directory]
        path = normalize_path(path) unless path.nil?

        if path && !path.empty?
          if @name && path.last != @name
            path = path + [@name]
          elsif @name.nil?
            @name = path.last
          end
          @path = path
          @name ||= path.last
        elsif @name
          @path = nil
        else
          raise ArgumentError, "A record boundary needs a raw element name or path"
        end

        @depth = integer_or_nil(spec[:depth] || spec[:level])
        @mode = (spec[:match] || spec[:matching] || :any).to_sym
        @alternate_paths = Array(spec[:alternate_paths]).map { |candidate| normalize_path(candidate) }
        @alternate_names = Array(spec[:alternate_names]).map(&:to_s)
      end

      def matches?(stack_path)
        return false if @depth && stack_path.length != @depth
        match_base?(stack_path) ||
          @alternate_names.any? { |candidate| element_matches?(stack_path.last, candidate) } ||
          @alternate_paths.any? { |candidate| path_matches?(stack_path, candidate) }
      end

      def to_h
        hash = {}
        hash[:path] = @path.dup if @path
        hash[:name] = @name if @name
        hash[:depth] = @depth if @depth
        hash[:match] = @mode
        hash[:alternate_paths] = @alternate_paths.map(&:dup) unless @alternate_paths.empty?
        hash[:alternate_names] = @alternate_names.dup unless @alternate_names.empty?
        hash
      end

      private

      def match_base?(stack_path)
        return false if @path && !path_matches?(stack_path, @path)
        return false if @name && !element_matches?(stack_path.last, @name)
        true
      end

      def path_matches?(stack_path, path)
        return false if stack_path.length < path.length
        return exact_path?(stack_path, path) if @mode == :exact
        exact_path?(stack_path, path) ||
          stack_path.last(path.length).zip(path).all? { |actual, wanted| element_matches?(actual, wanted) }
      end

      def exact_path?(stack_path, path)
        stack_path.length == path.length &&
          stack_path.zip(path).all? { |actual, wanted| element_matches?(actual, wanted) }
      end

      def element_matches?(actual, wanted)
        wanted == WILDCARD || actual == wanted
      end

      def normalize_path(value)
        if value.is_a?(String) && value.include?("/")
          value.split("/").reject(&:empty?)
        elsif value.is_a?(String) || value.is_a?(Symbol)
          [value.to_s]
        else
          Array(value).map(&:to_s)
        end
      end

      def string_or_nil(value)
        value.nil? ? nil : value.to_s
      end

      def integer_or_nil(value)
        value.nil? ? nil : Integer(value)
      end
    end

    # Normalizes the many convenient spellings a caller may use for commit
    # points into a list of {Boundary} objects.
    #
    # Accepted spellings:
    #   "record"                                  element name
    #   :record                                   element name
    #   "daily/warehouse/record"                  directory hierarchy
    #   ["daily", "warehouse", "record"]          hierarchy, or sibling names
    #   [["daily", "warehouse", "record"]]        explicit hierarchy
    #   [{:path => %w[daily warehouse record]}]   explicit hierarchy
    #   [{:name => "record", :depth => 3}]        name plus depth
    class Boundaries
      include Enumerable

      attr_reader :items

      def initialize(specs)
        @items = normalize(specs)
        raise ArgumentError, "At least one record boundary is required" if @items.empty?
      end

      def each(&block)
        @items.each(&block)
      end

      def match(stack_path)
        @items.find { |boundary| boundary.matches?(stack_path) }
      end

      def empty?
        @items.empty?
      end

      def to_a
        @items.map(&:to_h)
      end

      def self.from_state(state)
        return state if state.is_a?(Boundaries)
        new(state)
      end

      private

      def normalize(specs)
        case specs
        when nil
          []
        when Boundaries
          specs.items.dup
        when Boundary
          [specs]
        when String, Symbol
          if specs.to_s.include?("/")
            [Boundary.new(:path => specs.to_s)]
          else
            [Boundary.new(:name => specs.to_s)]
          end
        when Hash
          if boundary_hash?(specs)
            [Boundary.new(normalize_hash(specs))]
          else
            specs.flat_map do |key, value|
              if value.is_a?(Array) && value.all? { |entry| entry.is_a?(String) || entry.is_a?(Symbol) }
                [Boundary.new(:name => key.to_s, :path => value.map(&:to_s) + [key.to_s])]
              else
                normalize(value)
              end
            end
          end
        when Array
          normalize_array(specs)
        else
          raise ArgumentError, "Unsupported record boundary specification: #{specs.inspect}"
        end
      end

      def normalize_array(specs)
        return [] if specs.empty?
        if specs.all? { |entry| entry.is_a?(String) || entry.is_a?(Symbol) }
          entries = specs.map(&:to_s)
          # A flat list of strings is a directory hierarchy. Use an array of
          # hashes (or nested arrays) when you mean several independent commit
          # points.
          [Boundary.new(:path => entries)]
        else
          specs.flat_map do |entry|
            case entry
            when Array, String, Symbol
              [Boundary.new(:path => normalize_path_entry(entry))]
            else
              normalize(entry)
            end
          end
        end
      end

      def normalize_path_entry(entry)
        entry.is_a?(Array) ? entry.map(&:to_s) : [entry.to_s]
      end

      BOUNDARY_KEYS = [:name, :element, :tag, :record, :path, :paths, :parents,
                       :ancestors, :hierarchy, :directory, :depth, :level,
                       :match, :matching, :alternate_paths, :alternate_names].freeze

      def boundary_hash?(spec)
        spec.keys.any? { |key| BOUNDARY_KEYS.include?(key.to_sym) }
      end

      def normalize_hash(spec)
        spec.each_with_object({}) { |(key, value), hash| hash[key.to_sym] = value }
      end
    end
  end
end
