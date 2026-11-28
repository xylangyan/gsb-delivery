require "kiba/fanout/path"
require "kiba/fanout/fingerprint"
require "kiba/fanout/reference_store"

module Kiba
  module Fanout
    # Builds one group's delivery-time view and records the copies it needs.
    #
    # The view is isolated at delivery:
    # * every group gets a distinct root, so annotations cannot cross groups;
    # * declared references and paths read only during write keep the original
    #   objects, so they are never billed and see the untouched original;
    # * a mutable detail node is copied per group and generation, and every
    #   destination in the group receives that same copy, so a required
    #   in-group mutation is visible to the whole group;
    # * a node read later (retained/close read) is copied now, so later rows or
    #   upstream mutation of a registered reference cannot change it.
    #
    # Billing follows these copies only. Field names, references to an existing
    # copy, and read-only originals add no bytes.
    class Views
      attr_reader :source, :work_order, :ledger, :node_store, :reference_store,
        :generation, :delivery, :order_id
      attr_accessor :first_delivery

      def initialize(source:, work_order:, ledger:, node_store:, reference_store:,
        generation:, delivery:, order_id: nil, first_delivery: false)
        @source = source
        @work_order = work_order
        @ledger = ledger
        @node_store = node_store
        @reference_store = reference_store
        @generation = generation
        @delivery = delivery
        @order_id = order_id
        @first_delivery = first_delivery
        @groups = {}
      end

      def view_for(group)
        return @groups[group.name] if @groups.key?(group.name)

        # Every group gets its own root, even a read-only one. This keeps the
        # three routes from literally receiving the same object and stops a
        # later in-place upstream mutation from changing a page already
        # delivered. The structural copy is not a billable detail version.
        view = Views.deep_dup(@source)
        copies = {}
        bill_write_paths(group, view, copies)
        bill_deferred_paths(group, view, copies)
        restore_readonly_paths(group, view)
        @groups[group.name] = view
        view
      end

      def self.deep_dup(value)
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

      private

      def bill_write_paths(group, view, copies)
        group.live_write_paths.each do |path|
          bill_path(group, view, path, :write_view, copies)
        end
        group.close_write_paths.each do |path|
          bill_path(group, view, path, :close_write, copies)
        end
      end

      def bill_deferred_paths(group, view, copies)
        first_only = group.retained_first_only_paths
        paths = (group.close_read_paths + group.retained_only_paths).uniq

        paths.each do |path|
          # A first-page rule covers only the paths it names. Other retained
          # reads still need a version for every delivery.
          next if !@first_delivery && first_only.any? { |rule| Path.overlaps?(rule, path) }

          bill_path(group, view, path, :deferred_read, copies)
        end

        return unless @first_delivery

        first_only.each do |path|
          bill_path(group, view, path, :deferred_read, copies)
        end
      end

      def bill_path(group, view, pattern, reason, copies)
        @work_order.manifest.coverings(pattern).each do |node|
          if Path.at_or_above?(pattern, node.path)
            # The write targets the node or a container above it. Copy every
            # concrete instance of the node, not just the first.
            Path.each_concrete(view, node.path) do |keys, _value, complete|
              next unless complete

              bill_instance(group, view, keys, reason, copies, node)
            end
          else
            # The write targets a field inside the node. Each concrete write
            # maps to one node instance.
            Path.each_concrete(view, pattern) do |keys, _value, _complete|
              node_keys = keys.first(node.size)
              next if node_keys.size < node.size

              bill_instance(group, view, node_keys, reason, copies, node)
            end
          end
        end
      end

      def bill_instance(group, view, node_keys, reason, copies, node)
        original = Path.dig_keys(@source, node_keys)
        return if original.nil?

        copy_key = [node.path, node_keys.join(".")]
        existing = copies[copy_key]
        if existing
          @ledger.annotate(
            existing[:version],
            reason: reason,
            order_id: @order_id,
            destinations: group.destinations.size
          )
          Path.assign_at!(view, node_keys, existing[:copy])
          return
        end

        scope = reason == :deferred_read ? :shared : :per_order
        generation = scope == :shared ? :shared : @generation
        # Mutable copies are per delivery: a later yield/return with the same
        # content must not inherit annotations made to an earlier delivery.
        store_key = scope == :shared ? :shared : [@generation, @delivery]
        fingerprint = Fingerprint.for(original)
        copy = @node_store.fetch(
          group: group.name,
          node_path: node.path,
          source_id: original.__id__,
          fingerprint: fingerprint,
          scope_key: store_key
        ) { Views.deep_dup(original) }

        version = @ledger.bill(
          group: group.name,
          node: node,
          source_id: original.__id__,
          fingerprint: fingerprint,
          path: node_keys.join("."),
          reason: reason,
          scope: scope,
          generation: generation,
          delivery: @delivery,
          order_id: @order_id,
          destinations: group.destinations.size
        )
        copies[copy_key] = {version: version, copy: copy, reason: reason}
        Path.assign_at!(view, node_keys, copy)
      end

      # Delivery-time read-only values. Containers are snapshots so later
      # upstream mutation cannot rewrite a page already received; the snapshot
      # is shared inside a group by original identity and content. A path this
      # group mutates or retains is left on its copied version.
      def restore_readonly_paths(group, view)
        (group.references + group.write_reads).uniq.each do |path|
          next if group.declared_write?(path)
          next if group.deferred_path?(path)

          Path.each_concrete(@source, path) do |keys, value, complete|
            next unless complete
            next if keys.empty?

            snapshot = if value.is_a?(Hash) || value.is_a?(Array)
              @reference_store.fetch(group: group.name, path: path, value: value)
            else
              value
            end
            Path.assign_at!(view, keys, snapshot)
          end
        end
      end
    end
  end
end
