require "dotenv/handover/errors"

module Dotenv
  module Handover
    # An isolated environment used while rehearsing a handover.
    #
    # It behaves like the small part of +ENV+ the parser needs, records every
    # read so a route's dependencies can be explained, and never touches the
    # caller's real +ENV+.
    class Sandbox
      include Enumerable

      # A single recorded lookup.
      Lookup = Struct.new(:kind, :key, :found, :value, :label) do
        def to_h
          {"kind" => kind.to_s, "key" => key, "found" => found,
           "value" => value, "label" => label}
        end
      end

      attr_reader :lookups

      def initialize(values = {}, lookups: [])
        @values = {}
        values.each { |key, value| @values[key.to_s] = value.nil? ? nil : value.to_s }
        @lookups = lookups
        @label = nil
      end

      def [](key)
        key = key.to_s
        record(:read, key, @values.key?(key), @values[key])
        @values[key]
      end

      # Read a key without recording a dependency, for internal probing.
      def peek(key)
        @values[key.to_s]
      end

      def defined_key?(key)
        @values.key?(key.to_s)
      end

      def key?(key)
        key = key.to_s
        record(:key?, key, @values.key?(key), @values[key])
        @values.key?(key)
      end
      alias_method :has_key?, :key?
      alias_method :include?, :key?
      alias_method :member?, :key?

      def []=(key, value)
        @values[key.to_s] = value.nil? ? nil : value.to_s
      end

      def store(key, value)
        self[key] = value
        value
      end

      def fetch(key, default = nil)
        key = key.to_s
        record(:read, key, @values.key?(key), @values[key])
        return @values[key] if @values.key?(key)
        return yield(key) if block_given?
        default
      end

      def delete(key)
        @values.delete(key.to_s)
      end

      def update(other)
        other.each do |key, value|
          key = key.to_s
          next if @values.key?(key) && block_given? && !yield(key, @values[key], value)
          @values[key] = value.nil? ? nil : value.to_s
        end
        self
      end
      alias_method :merge!, :update

      def to_h
        @values.dup
      end
      alias_method :to_hash, :to_h

      def keys
        @values.keys
      end

      def values
        @values.values
      end

      def each(&block)
        @values.each(&block)
      end

      def empty?
        @values.empty?
      end

      def size
        @values.size
      end

      def dup
        Sandbox.new(@values, lookups: @lookups.dup)
      end

      def fork
        Sandbox.new(@values, lookups: @lookups.dup)
      end

      # Label subsequent lookups with the checkpoint/call they belong to.
      def with_label(label)
        previous = @label
        @label = label
        yield self
      ensure
        @label = previous
      end

      attr_reader :label

      def lookups_for(label)
        @lookups.select { |lookup| lookup.label == label }
      end

      def clear_lookups!
        @lookups = []
      end

      private

      def record(kind, key, found, value)
        @lookups << Lookup.new(kind, key, found, value, @label)
      end
    end
  end
end
