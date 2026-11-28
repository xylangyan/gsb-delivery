require "kiba/fanout/copy_ledger"
require "kiba/fanout/node_store"
require "kiba/fanout/reference_store"
require "kiba/fanout/views"
require "kiba/fanout/work_order"
require "kiba/row_phase"

module Kiba
  module Fanout
    # Records what one receiving group actually saw, including the original
    # order key. Used to report both sides of a cross-group conflict.
    Observation = Struct.new(:group, :phase, :order_id, :paths, keyword_init: true)

    # A Kiba destination that fans one input row out to isolated receiver
    # groups according to a WorkOrder.
    #
    # `write` delivers per-group delivery-time views. `close` closes every
    # wrapped destination exactly once. A group that defers reads to close
    # keeps the snapshot taken when the row was delivered, so later rows cannot
    # rewrite pages it has not copied yet.
    #
    # Close-origin rows emitted by a transform are ordinary new deliveries.
    # Groups that request `close_only_if_received` see them only after they
    # received the order named by the marker.
    class Multiplexer
      attr_reader :work_order, :ledger, :observations, :runtime_conflicts

      def initialize(work_order, node_store: NodeStore.new,
        reference_store: ReferenceStore.new, on_init: nil)
        @work_order = work_order
        @ledger = CopyLedger.new(work_order.manifest)
        @node_store = node_store
        @reference_store = reference_store
        @static_conflicts = work_order.validate!
        @runtime_conflicts = []
        @observations = []
        @destinations = instantiate_destinations
        @received_by_group = Hash.new { |hash, key| hash[key] = [] }
        @delivered = 0
        @group_delivered = Hash.new(0)
        @closed = false
        on_init&.call(self)
      end

      def write(row)
        fail_on_conflicts!
        verify_order_key!(row)

        order_id = @work_order.order_id_for(row)
        phase = RowPhase.close? ? :close : :write
        generation = order_id.nil? ? @delivered : order_id

        views = Views.new(
          source: row,
          work_order: @work_order,
          ledger: @ledger,
          node_store: @node_store,
          reference_store: @reference_store,
          generation: generation,
          delivery: @delivered,
          order_id: order_id
        )

        @work_order.groups.each do |group|
          next unless deliver?(group, order_id, phase, row)

          views.first_delivery = @group_delivered[group.name].zero?
          view = views.view_for(group)
          observe(group, order_id, view, phase)
          dispatch(group, view)
          @group_delivered[group.name] += 1
          note_received(group, order_id) if phase == :write
        end

        @delivered += 1
        nil
      end

      def close
        return nil if @closed

        @work_order.groups.each do |group|
          @destinations.fetch(group.name).each do |destination|
            destination.close if destination.respond_to?(:close)
          end
        end

        enforce_quota!
        @closed = true
        nil
      end

      def conflicts
        @static_conflicts + @runtime_conflicts
      end

      # A single report suitable for a trial receipt. It includes both sides'
      # original orders for each conflict and the version decisions that
      # determine the copy budget.
      def report
        {
          rows: @delivered,
          conflicts: conflicts.map do |conflict|
            conflict.to_h.merge(
              observations: conflict_observations(conflict)
            )
          end,
          budget: budget
        }
      end

      def total
        @ledger.total
      end

      # Minimum cumulative fixed bytes actually needed for the declared reads
      # and writes: only the versions the ledger had to create are counted.
      def minimum_copy_bytes
        @ledger.total_bytes
      end

      def budget
        {
          minimum_copy_bytes: minimum_copy_bytes,
          quota_bytes: @work_order.quota_bytes,
          over_quota: @work_order.quota_bytes && minimum_copy_bytes > @work_order.quota_bytes,
          decisions: @ledger.decisions.map do |version|
            {
              group: version[:group],
              node: version[:node],
              bytes: version[:bytes],
              orders: version[:orders],
              reasons: version[:reasons],
              determiner: version[:determiner]
            }
          end
        }
      end

      def summary
        {
          rows: @delivered,
          conflicts: conflicts.map(&:to_s),
          copies: @ledger.total,
          budget: budget
        }
      end

      def release(group: nil, path: nil)
        @ledger.release(group: group, path: path)
      end

      # Original order keys each side of a conflict actually observed.
      def conflict_observations(conflict)
        groups = [conflict.group, conflict.other_group].compact
        @observations
          .select { |observation| groups.include?(observation.group) }
          .map do |observation|
            {
              group: observation.group,
              phase: observation.phase,
              order_id: observation.order_id,
              nodes: observation.paths.fetch(conflict.path, [])
            }
          end
      end

      private

      def deliver?(group, order_id, phase, row)
        return false unless group.accept?(row)
        return true unless group.close_only_if_received? && phase == :close
        return false unless group.closes_marker_for?(order_id)

        !order_id.nil? && @received_by_group[group.name].include?(order_id)
      end

      def note_received(group, order_id)
        return if order_id.nil?

        list = @received_by_group[group.name]
        list << order_id unless list.include?(order_id)
      end

      def instantiate_destinations
        @work_order.groups.each_with_object({}) do |group, result|
          result[group.name] = group.destinations.map { |definition| instantiate(definition) }
        end
      end

      def instantiate(definition)
        klass = definition[:klass]
        args = definition[:args]
        kwargs = definition[:kwargs]
        if kwargs.nil? || kwargs.empty?
          klass.new(*args)
        else
          klass.new(*args, **kwargs)
        end
      end

      def dispatch(group, view)
        @destinations.fetch(group.name).each do |destination|
          destination.write(view) if destination.respond_to?(:write)
        end
      end

      def observe(group, order_id, view, phase)
        observed = {}
        (group.live_write_paths + group.close_read_paths + group.retained_only_paths).uniq.each do |path|
          values = []
          Path.each_value(view, path) do |value|
            values << {
              source_id: value.__id__,
              fingerprint: Fingerprint.for(value)
            }
          end
          observed[path] = values
        end
        @observations << Observation.new(
          group: group.name,
          phase: phase,
          order_id: order_id,
          paths: observed
        )
      end

      def verify_order_key!(row)
        return unless @work_order.order_key_name

        value = Path.fetch(row, @work_order.order_key_name)
        return unless value.nil?

        raise ConflictError, [
          Conflict.new(
            kind: :missing_order_key,
            group: :all,
            path: @work_order.order_key_name,
            phase: :write,
            message: "row has no order key"
          )
        ]
      end

      def fail_on_conflicts!
        return if @static_conflicts.empty?
        return unless @work_order.conflict_mode == :raise

        raise ConflictError, @static_conflicts
      end

      def enforce_quota!
        return unless @work_order.quota_bytes

        used = @ledger.total_bytes
        return if used <= @work_order.quota_bytes

        conflict = Conflict.new(
          kind: :quota_exceeded,
          group: :all,
          path: nil,
          phase: :close,
          message: "copied #{used} bytes against quota #{@work_order.quota_bytes}",
          observed: @ledger.decisions
        )
        @runtime_conflicts << conflict
        raise ConflictError, [conflict] if @work_order.conflict_mode == :raise
      end
    end

    def self.backend(work_order)
      {klass: Multiplexer, args: [work_order]}
    end
  end
end
