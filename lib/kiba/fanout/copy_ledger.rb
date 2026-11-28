require "kiba/fanout/path"

module Kiba
  module Fanout
    # Accounts logical detail-node versions, not Ruby heap bytes.
    #
    # A version is one copy that can be mutated independently or read later.
    # Sharing is explicit and identity-based:
    #
    # * a mutable copy is per group and per delivery, so a yield/return pair or
    #   two orders sharing one original get independent mutable versions;
    # * a retained read-only copy is shared by every delivery pointing at the
    #   same original object with the same content;
    # * equal but distinct originals never share.
    class CopyLedger
      class Version
        attr_reader :group, :node, :source_id, :fingerprint, :path, :reasons,
          :orders, :scope, :generation, :delivery
        attr_accessor :bytes, :served, :released, :destinations

        def initialize(group:, node:, source_id:, fingerprint:, path:, bytes:,
          reason:, scope:, generation:, delivery:, destinations: 1)
          @group = group
          @node = node
          @source_id = source_id
          @fingerprint = fingerprint
          @path = path
          @bytes = bytes
          @scope = scope
          @generation = generation
          @delivery = delivery
          @reasons = []
          @orders = []
          @served = 0
          @destinations = destinations
          @released = false
          add_reason(reason)
        end

        def add_reason(reason)
          @reasons << reason unless @reasons.include?(reason)
          self
        end

        def serve(destinations = 1)
          @served += 1
          @destinations = [@destinations, destinations].max
          self
        end

        def note_order(order_id)
          @orders << order_id if order_id && !@orders.include?(order_id)
          self
        end

        def release!
          @released = true
          self
        end

        def released?
          @released
        end

        def live?
          !@released
        end

        def deferred?
          @reasons.include?(:deferred_read)
        end

        def mutable?
          @reasons.include?(:write_view) || @reasons.include?(:close_write)
        end

        def shared_across_orders?
          @scope == :shared && @orders.size > 1
        end

        def determiner
          if deferred? && mutable?
            "retained write version reused for close reads"
          elsif deferred?
            if shared_across_orders?
              "retained reference shared by #{@orders.size} orders"
            else
              "retained reference read again at close"
            end
          elsif @destinations > 1
            "in-group share across #{@destinations} receivers"
          else
            "independent mutable version"
          end
        end

        def to_h
          {
            group: @group,
            node: @node.path,
            path: @path,
            source_id: @source_id,
            fingerprint: @fingerprint,
            bytes: @bytes,
            scope: @scope,
            generation: @generation,
            delivery: @delivery,
            orders: @orders,
            reasons: @reasons,
            served: @served,
            destinations: @destinations,
            released: @released,
            determiner: determiner
          }
        end
      end

      attr_reader :manifest, :versions

      def initialize(manifest)
        @manifest = manifest
        @versions = []
      end

      # scope: :per_order for mutable copies, :shared for later read-only
      # copies. Released versions are never reused.
      def bill(group:, node:, source_id:, fingerprint:, path:, reason:,
        scope:, generation:, delivery:, order_id: nil, destinations: 1)
        existing = @versions.find do |version|
          version.live? &&
            version.group == group &&
            version.node.segments == node.segments &&
            version.source_id == source_id &&
            version.fingerprint == fingerprint &&
            version.scope == scope &&
            version.generation == generation &&
            (scope == :shared || version.delivery == delivery)
        end

        if existing
          existing.bytes = [existing.bytes, node.bytes].max
          existing.add_reason(reason)
          existing.serve(destinations)
          existing.note_order(order_id)
          return existing
        end

        version = Version.new(
          group: group,
          node: node,
          source_id: source_id,
          fingerprint: fingerprint,
          path: path,
          bytes: node.bytes,
          reason: reason,
          scope: scope,
          generation: generation,
          delivery: delivery,
          destinations: destinations
        )
        version.note_order(order_id)
        @versions << version
        version
      end

      # Mark an already-billed version as also used for another purpose, for
      # example a write copy that is read again at close.
      def annotate(version, reason:, order_id: nil, destinations: 1)
        version.add_reason(reason)
        version.serve(destinations)
        version.note_order(order_id)
        version
      end

      def versions_for(group)
        @versions.select { |version| version.group == group }
      end

      def total_bytes
        @versions.sum(&:bytes)
      end

      def live_bytes
        @versions.reject(&:released?).sum(&:bytes)
      end

      def released_bytes
        @versions.select(&:released?).sum(&:bytes)
      end

      def deferred_versions
        @versions.select(&:deferred?)
      end

      # Explicit release. It does not merge distinct objects and only affects
      # versions no longer required for later reads.
      def release(group: nil, path: nil)
        selected = @versions.select do |version|
          (group.nil? || version.group == group) &&
            (path.nil? || Path.overlaps?(path, version.path))
        end
        selected.each(&:release!)
        selected
      end

      def decisions
        @versions.map(&:to_h)
      end

      def total
        {
          total_bytes: total_bytes,
          live_bytes: live_bytes,
          released_bytes: released_bytes,
          versions: decisions
        }
      end
    end
  end
end
