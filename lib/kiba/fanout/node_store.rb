require "kiba/fanout/fingerprint"

module Kiba
  module Fanout
    # Caches the concrete copy object for one ledger version.
    #
    # The key includes the group, logical node, generation, original identity
    # and content fingerprint. That makes the object handed to receivers the
    # same object the ledger billed, and prevents equal-but-distinct originals
    # from accidentally sharing.
    class NodeStore
      def initialize
        @entries = {}
      end

      def fetch(group:, node_path:, source_id:, fingerprint:, scope_key:)
        key = [group, node_path, scope_key, source_id, fingerprint]
        @entries.fetch(key) { @entries[key] = yield }
      end

      def entries
        @entries.map do |(group, node_path, scope_key, source_id, fingerprint), copy|
          {
            group: group,
            node: node_path,
            scope_key: scope_key,
            source_id: source_id,
            fingerprint: fingerprint,
            object: copy
          }
        end
      end

      def clear
        @entries.clear
      end
    end
  end
end
