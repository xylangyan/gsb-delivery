require "kiba/fanout/fingerprint"

module Kiba
  module Fanout
    # Delivery-time snapshots of declared reference fields.
    #
    # Sharing is per group and keyed by original identity plus content:
    # * two orders pointing at the same list receive the same copy object, so
    #   they can still be queried together;
    # * two different lists with equal values receive different objects and
    #   are never merged;
    # * if the upstream mutates the original after a delivery, the fingerprint
    #   changes and later orders get a new copy, leaving earlier pages intact.
    #
    # These read-only snapshots are not billable by themselves.
    class ReferenceStore
      def initialize
        @entries = {}
      end

      def fetch(group:, path:, value:)
        key = [group, path, value.__id__, Fingerprint.for(value)]
        @entries.fetch(key) { @entries[key] = deep_dup(value) }
      end

      def deep_dup(value)
        case value
        when Hash
          result = value.class.new
          value.each { |key, child| result[key] = deep_dup(child) }
          result
        when Array
          result = value.class.new
          value.each { |child| result << deep_dup(child) }
          result
        when String
          value.dup
        else
          value
        end
      end
    end
  end
end
