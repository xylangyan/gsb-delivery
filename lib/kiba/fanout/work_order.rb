require "kiba/fanout/path"
require "kiba/fanout/copy_manifest"

module Kiba
  module Fanout
    Conflict = Struct.new(
      :kind, :group, :other_group, :path, :phase, :message, :observed,
      keyword_init: true
    ) do
      def to_s
        parts = [kind, group, path, phase].compact
        text = "#{parts.join(" ")}: #{message}"
        text += " observed=#{observed.inspect}" if observed && !observed.empty?
        text
      end

      def to_h
        {
          kind: kind,
          group: group,
          other_group: other_group,
          path: path,
          phase: phase,
          message: message,
          observed: observed
        }
      end
    end

    class ConflictError < StandardError
      attr_reader :conflicts

      def initialize(conflicts)
        @conflicts = conflicts
        super(conflicts.map(&:to_s).join("\n"))
      end
    end

    # A receiving work order.
    #
    # A group is one isolated delivery route. Destinations in the same group
    # share one row view, so a declared in-group mutation is visible to all of
    # them. Different groups get separate mutable state and never see each
    # other's annotations.
    #
    #   work_order = Kiba::Fanout::WorkOrder.build do
    #     order_key "order_id"
    #     quota bytes: 512
    #
    #     manifest do
    #       node "lines", bytes: 48, whole: true
    #       node "adjustments", bytes: 16, whole: true
    #     end
    #
    #     group :picking do
    #       destination Picking
    #       destination PickingReview
    #       reference "order_id"
    #       reference "adjustments"
    #       mutable "lines.*.picked"
    #       require_mutation "lines.*.picked"
    #       on_write reads: ["lines.*.amount"], writes: ["lines.*.picked"]
    #     end
    #
    #     group :archive do
    #       destination Archive
    #       retain_first "lines"
    #       on_close reads: ["lines.*.amount"]
    #     end
    #   end
    class WorkOrder
      def self.build(&block)
        order = new
        order.instance_eval(&block) if block
        order
      end

      attr_reader :groups, :order_key_name, :quota_bytes, :conflict_mode

      def initialize
        @groups = []
        @manifest = CopyManifest.new
        @order_key_name = nil
        @conflict_mode = :report
        @quota_bytes = nil
      end

      def order_key(path)
        @order_key_name = Path.join(path)
      end

      def manifest(&block)
        return @manifest unless block

        @manifest = CopyManifest.build(&block)
      end

      def quota(bytes:)
        unless bytes.is_a?(Integer) && bytes >= 0
          raise ArgumentError, "quota bytes must be a non-negative Integer"
        end

        @quota_bytes = bytes
      end

      def conflict_mode=(mode)
        unless [:report, :raise].include?(mode)
          raise ArgumentError, "conflict_mode must be :report or :raise"
        end

        @conflict_mode = mode
      end

      def group(name, &block)
        current = group_named(name)
        unless current
          current = Group.new(name, self)
          @groups << current
        end
        current.instance_eval(&block) if block
        current
      end

      def group_named(name)
        @groups.find { |group| group.name == name }
      end

      def order_id_for(row)
        return nil unless @order_key_name

        Path.fetch(row, @order_key_name)
      end

      def validate!
        conflicts = static_conflicts
        if !conflicts.empty? && @conflict_mode == :raise
          raise ConflictError, conflicts
        end

        conflicts
      end

      def static_conflicts
        @groups.flat_map { |group| group_conflicts(group) }
      end

      private

      def group_conflicts(group)
        write_conflicts(group) +
          close_conflicts(group) +
          mutation_conflicts(group) +
          cross_group_conflicts(group)
      end

      def write_conflicts(group)
        group.live_write_paths.flat_map { |path| path_conflicts(group, path, :write) }
      end

      def close_conflicts(group)
        writes = group.close_write_paths.flat_map do |path|
          path_conflicts(group, path, :close)
        end
        reads = (group.close_read_paths + group.retained_only_paths +
          group.retained_first_only_paths).uniq.filter_map do |path|
          next if group.scalar?(path)
          next unless @manifest.empty? || @manifest.coverings(path).empty?

          Conflict.new(
            kind: :unbilled_detail,
            group: group.name,
            path: path,
            phase: :close,
            message: "retained detail has no fixed-byte manifest node"
          )
        end
        writes + reads
      end

      def path_conflicts(group, path, phase)
        conflicts = []
        if group.reference?(path)
          conflicts << Conflict.new(
            kind: :reference_write,
            group: group.name,
            path: path,
            phase: phase,
            message: "declared reference would be mutated by this group"
          )
        end

        unless group.mutable?(path)
          conflicts << Conflict.new(
            kind: :undeclared_write,
            group: group.name,
            path: path,
            phase: phase,
            message: "write path is not declared mutable"
          )
        end

        if !group.scalar?(path) && !@manifest.empty? && @manifest.coverings(path).empty?
          conflicts << Conflict.new(
            kind: :unbilled_detail,
            group: group.name,
            path: path,
            phase: phase,
            message: "detail write has no fixed-byte manifest node"
          )
        end

        conflicts
      end

      def mutation_conflicts(group)
        group.required_mutations.filter_map do |path|
          next if group.declared_write?(path)

          Conflict.new(
            kind: :missing_required_mutation,
            group: group.name,
            path: path,
            phase: :write,
            message: "required in-group mutation is not a declared write"
          )
        end
      end

      def cross_group_conflicts(group)
        group.cross_group_requirements.flat_map do |other_name, paths|
          paths.map do |path|
            Conflict.new(
              kind: :cross_group_requirement,
              group: group.name,
              other_group: other_name,
              path: path,
              phase: :write,
              message: "isolated groups cannot observe each other's annotations"
            )
          end
        end
      end
    end

    class Group
      attr_reader :name, :destinations, :references, :mutable_paths, :scalar_paths,
        :retained_paths, :required_mutations, :cross_group_requirements,
        :write_reads, :write_writes, :close_reads, :close_writes

      def initialize(name, work_order)
        @name = name
        @work_order = work_order
        @destinations = []
        @marker_orders = []
        @references = []
        @mutable_paths = []
        @scalar_paths = []
        @retained_paths = []
        @retained_first_paths = []
        @required_mutations = []
        @cross_group_requirements = {}
        @write_reads = []
        @write_writes = []
        @close_reads = []
        @close_writes = []
        @has_write_rules = false
        @has_close_rules = false
        @close_only_if_received = false
        @write_filter = nil
      end

      def destination(klass, *args, **kwargs)
        @destinations << {klass: klass, args: args, kwargs: kwargs}
        self
      end

      # Fields kept as references to the original object.
      def reference(*paths)
        @references.concat(normalize(paths))
        self
      end
      alias_method :shared, :reference

      # Fields this group is allowed to change.
      def mutable(*paths)
        @mutable_paths.concat(normalize(paths))
        self
      end

      # Mutable fields that are plain values, not billable detail nodes.
      def scalar(*paths)
        @scalar_paths.concat(normalize(paths))
        self
      end

      # Keep a version available for later reads (for example, at close).
      def retain(*paths)
        @retained_paths.concat(normalize(paths))
        self
      end
      alias_method :retained, :retain

      # Retain only the first delivery for this group. This matches receivers
      # that keep the first page they were handed and copy it at close; later
      # deliveries do not require a retained version.
      def retain_first(*paths)
        @retained_first_paths.concat(normalize(paths))
        self
      end

      # Changes that all destinations in this group must observe.
      def require_mutation(*paths)
        @required_mutations.concat(normalize(paths))
        self
      end

      # Ask to observe another isolated group's write. This is a conflict and is
      # reported with the original order each side actually saw.
      def require_from(other_group, *paths)
        @cross_group_requirements[other_group.to_sym] ||= []
        @cross_group_requirements[other_group.to_sym].concat(normalize(paths))
        self
      end

      def on_write(reads: [], writes: [])
        @has_write_rules = true
        @write_reads.concat(normalize(Array(reads)))
        @write_writes.concat(normalize(Array(writes)))
        self
      end

      def on_close(reads: [], writes: [])
        @has_close_rules = true
        @close_reads.concat(normalize(Array(reads)))
        @close_writes.concat(normalize(Array(writes)))
        self
      end

      # A close marker is delivered only to this group if it already received
      # the order named by the marker. When order ids are given, only markers
      # for those orders are considered for this group.
      def close_only_if_received(*order_ids)
        @close_only_if_received = order_ids.empty? ? true : order_ids.flatten
        self
      end

      def close_only_if_received?
        @close_only_if_received != false && !@close_only_if_received.nil?
      end

      def closes_marker_for?(order_id)
        return true if @close_only_if_received == true
        return false unless @close_only_if_received.is_a?(Array)

        @close_only_if_received.include?(order_id)
      end

      # Optional predicate run before this group receives a row. It must have
      # no external side effects; it only decides whether this route handles
      # the row and therefore whether a later close marker may see it.
      def accept(&block)
        @write_filter = block
        self
      end

      def accept?(row)
        @write_filter.nil? || @write_filter.call(row)
      end

      def live_write_paths
        paths = @has_write_rules ? @write_writes : @mutable_paths
        paths.reject { |path| scalar?(path) }
      end

      def close_write_paths
        @close_writes.reject { |path| scalar?(path) }
      end

      def close_read_paths
        @close_reads.reject { |path| scalar?(path) }
      end

      def retained_only_paths
        @retained_paths.reject { |path| scalar?(path) }
      end

      def retained_first_only_paths
        @retained_first_paths.reject { |path| scalar?(path) }
      end

      def deferred_path?(path)
        (close_read_paths + retained_only_paths + retained_first_only_paths).any? do |declared|
          Path.overlaps?(declared, path)
        end
      end

      def retain_first?
        !retained_first_only_paths.empty?
      end

      def declared_write?(path)
        (live_write_paths + close_write_paths).any? do |declared|
          Path.overlaps?(declared, path)
        end
      end

      def scalar?(path)
        @scalar_paths.any? { |declared| Path.overlaps?(declared, path) }
      end

      def reference?(path)
        @references.any? { |declared| Path.overlaps?(declared, path) }
      end

      def mutable?(path)
        @mutable_paths.any? { |declared| Path.overlaps?(declared, path) }
      end

      def writes?
        !(live_write_paths + close_write_paths).empty?
      end

      def deferred?
        !(close_read_paths + retained_only_paths + retained_first_only_paths).empty?
      end

      private

      def normalize(paths)
        paths.flatten.map { |path| Path.join(path) }
      end
    end
  end
end
