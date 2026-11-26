# frozen_string_literal: true

require 'ox'
require 'set'

module Ox
  # Counts safe interleavings of workstation cancellation tickets around one
  # whole-group ticket.
  #
  # The implementation keeps the caller's Ox tree untouched.  Every replay runs
  # on an identity-preserving deep copy so two mounts of one structurally equal
  # object still have independent registration chains.
  class CancellationScheduler
    Ticket = Struct.new(
      :id, :station_id, :bin_id, :mode, :receiver, :argument, :approved,
      :raw, keyword_init: true
    )

    Registration = Struct.new(:code, :index_path, :chain, keyword_init: true)

    StepReceipt = Struct.new(
      :ticket_id, :phase, :ok, :removed, :approved, :error, :details,
      keyword_init: true
    ) do
      def to_h
        {
          ticket_id: ticket_id,
          phase: phase,
          ok: ok,
          removed: removed,
          approved: approved,
          error: error,
          details: details
        }
      end
    end

    Position = Struct.new(
      :local_count, :count, :sequence, :steps, :safe,
      keyword_init: true
    ) do
      def safe?
        safe
      end

      def to_h
        {
          local_count: local_count,
          count: count,
          safe: safe,
          sequence: sequence,
          steps: steps&.map(&:to_h)
        }
      end
    end

    Result = Struct.new(:positions, :counts, :total_local_actions, keyword_init: true) do
      attr_accessor :scheduler

      def witness(position)
        index = position.is_a?(Integer) ? position : position.local_count
        positions[index]&.sequence
      end

      def witness_for(position)
        witness(position)
      end

      def count_at(index)
        counts[index]
      end

      def [](index)
        positions[index]
      end

      def replay(position)
        index = position.is_a?(Integer) ? position : position.local_count
        position = positions[index]
        return nil unless position&.sequence && scheduler

        verification = scheduler.replay_order(position.sequence)
        position.steps = verification[:steps]
        position.safe = verification[:ok]
        verification
      end

      def replay_all_witnesses
        positions.each_with_object({}) do |position, map|
          next unless position.sequence

          map[position.local_count] = replay(position)
        end
      end

      def to_h
        {
          total_local_actions: total_local_actions,
          counts: counts,
          positions: positions.map(&:to_h)
        }
      end

      def to_json(*args)
        require 'json'
        to_h.to_json(*args)
      end
    end

    class ResolutionError < StandardError; end
    class ReplayError < StandardError; end

    # One edge in the tracked object graph.  Edge ids are stable across clones
    # and reconciliation, allowing registrations to refer to a particular mount
    # rather than to an XML shape or Ruby object_id alone.
    class Edge
      attr_reader :id
      attr_accessor :child_node, :literal

      def initialize(id, child_node: nil, literal: nil)
        @id = id
        @child_node = child_node
        @literal = literal
      end

      def node?
        !@child_node.nil?
      end

      def key
        node? ? [:ox_node, @child_node.ox.object_id] : [:literal, @literal.class, @literal]
      end
    end

    class TrackedNode
      attr_reader :ox, :edges

      def initialize(ox)
        @ox = ox
        @edges = []
      end
    end

    class World
      attr_reader :tree, :root_node, :registrations, :removed_edge_ids, :id_sequence
      attr_accessor :last_change_set

      def initialize(original_tree, registration_specs = [])
        @tree = CancellationScheduler.normalize_tree(original_tree)
        @id_sequence = 0
        @removed_edge_ids = Set.new
        @nodes_by_original_oid = {}
        clone_memo = {}
        cloned_tree = CancellationScheduler.deep_clone_ox(@tree, clone_memo)
        @tree = cloned_tree
        @root_node = build_from_original(@tree, cloned_tree, clone_memo)
        @registrations = registration_specs.map do |spec|
          code = spec[:code]
          index_path = spec[:index_path]
          chain = chain_for_index_path(@tree, @root_node, index_path)
          Registration.new(code: code, index_path: index_path.dup, chain: chain)
        end
        @registration_by_code = @registrations.to_h { |reg| [reg.code, reg] }
        @codes_by_edge = Hash.new { |hash, key| hash[key] = [] }
        @registrations.each do |registration|
          registration.chain.each do |edge_id|
            @codes_by_edge[edge_id] << registration.code
          end
        end
      end

      private_class_method def self.allocate_dup
        allocate
      end

      def deep_dup
        copy = self.class.allocate
        copy.instance_variable_set(:@id_sequence, @id_sequence)
        copy.instance_variable_set(:@removed_edge_ids, @removed_edge_ids.dup)
        copy.instance_variable_set(:@registrations, @registrations.map do |reg|
          Registration.new(code: reg.code, index_path: reg.index_path.dup, chain: reg.chain.dup)
        end)
        copy.instance_variable_set(:@registration_by_code, @registration_by_code)
        copy.instance_variable_set(:@codes_by_edge, @codes_by_edge)

        clone_memo = {}
        new_tree = CancellationScheduler.deep_clone_ox(@tree, clone_memo)
        copy.instance_variable_set(:@tree, new_tree)

        node_memo = {}
        new_root = duplicate_node(@root_node, new_tree, node_memo, copy)
        copy.instance_variable_set(:@root_node, new_root)
        copy
      end

      def next_edge_id
        @id_sequence += 1
        "edge-#{@id_sequence}"
      end

      def node_for_ox(ox_object)
        return nil unless ox_object.is_a?(Ox::Node)

        @nodes_by_ox_oid ||= {}
        @nodes_by_ox_oid[ox_object.object_id]
      end

      def present_codes
        @registrations.filter_map do |reg|
          reg.code if reg.chain.none? { |edge_id| @removed_edge_ids.include?(edge_id) }
        end.to_set
      end

      def codes_disappearing(new_edge_ids, previously_removed)
        codes = new_edge_ids.flat_map { |edge_id| @codes_by_edge[edge_id] }.uniq
        codes.select do |code|
          registration = @registration_by_code[code]
          registration && registration.chain.none? { |edge_id| previously_removed.include?(edge_id) }
        end.to_set
      end

      def reconcile!
        newly_removed = Set.new
        restorations = []
        nodes = all_tracked_nodes
        nodes.each do |tracked|
          current_children = tracked.ox.nodes
          next if tracked.edges.size == current_children.size

          old_edges = tracked.edges.dup
          matched = align_edges(old_edges, current_children)
          new_edges = Array.new(current_children.size)

          matched.each do |old_index, current_index|
            edge = old_edges[old_index]
            new_edges[current_index] = edge
          end

          old_edges.each_with_index do |edge, old_index|
            next if matched.key?(old_index)

            newly_removed << edge.id
            @removed_edge_ids << edge.id
          end

          current_children.each_with_index do |child, index|
            next if new_edges[index]

            edge = Edge.new(next_edge_id)
            if child.is_a?(Ox::Node)
              child_node = node_for_ox(child) || TrackedNode.new(child)
              register_node(child_node)
              edge.child_node = child_node
            else
              edge.literal = child
            end
            new_edges[index] = edge
          end

          if old_edges.map(&:id) != new_edges.map(&:id)
            restorations << [tracked, old_edges]
          end
          tracked.instance_variable_set(:@edges, new_edges)
        end
        [newly_removed, restorations]
      end

      def undo_change_set(change_set)
        return unless change_set

        change_set[:restorations].each do |tracked, old_edges|
          old_children = old_edges.map do |edge|
            edge.node? ? edge.child_node.ox : edge.literal
          end
          tracked.ox.instance_variable_set(:@nodes, old_children)
          tracked.instance_variable_set(:@edges, old_edges)
        end
        change_set[:removed_ids].each { |edge_id| @removed_edge_ids.delete(edge_id) }
        @last_change_set = nil
      end

      def structural_signature(ox_object = @tree)
        CancellationScheduler.structural_signature(ox_object)
      end

      def index_path_for_path(path, base: nil)
        CancellationScheduler.index_path_for_path(@tree, path, base: base || @tree)
      end

      def resolve_path(path, base: nil)
        CancellationScheduler.resolve_path(@tree, path, base: base)
      end

      def chain_for_index_path(root = @tree, root_tracked = @root_node, index_path)
        current_ox = root
        current_tracked = root_tracked
        chain = []
        Array(index_path).each do |child_index|
          edge = current_tracked.edges[child_index]
          raise ReplayError, "registration index path is invalid at #{child_index}" unless edge

          chain << edge.id
          child = current_ox.nodes[child_index]
          current_ox = child
          current_tracked = edge.child_node
        end
        chain
      end

      def chain_live?(chain)
        chain.none? { |edge_id| @removed_edge_ids.include?(edge_id) }
      end

      private

      def build_from_original(original_tree, cloned_tree, _clone_memo)
        tracked_by_clone_oid = {}
        build = nil
        build = lambda do |original, cloned|
          if cloned.is_a?(Ox::Node) && tracked_by_clone_oid.key?(cloned.object_id)
            return tracked_by_clone_oid[cloned.object_id]
          end

          tracked = TrackedNode.new(cloned)
          if cloned.is_a?(Ox::Node)
            tracked_by_clone_oid[cloned.object_id] = tracked
            register_node(tracked)
          end

          if original.is_a?(Ox::Node)
            original.nodes.each_with_index do |original_child, index|
              cloned_child = cloned.nodes[index]
              edge = Edge.new(next_edge_id)
              tracked.edges << edge
              if original_child.is_a?(Ox::Node)
                child_tracked = build.call(original_child, cloned_child)
                edge.child_node = child_tracked
              else
                edge.literal = cloned_child
              end
            end
          end
          tracked
        end
        build.call(original_tree, cloned_tree)
      end

      def register_node(tracked)
        @nodes_by_ox_oid ||= {}
        @nodes_by_ox_oid[tracked.ox.object_id] = tracked
      end

      def duplicate_node(old_tracked, new_ox, memo, new_world)
        return memo[old_tracked.object_id] if memo.key?(old_tracked.object_id)

        tracked = TrackedNode.new(new_ox)
        memo[old_tracked.object_id] = tracked
        new_world.send(:register_node, tracked)

        old_tracked.edges.each_with_index do |old_edge, index|
          edge = Edge.new(old_edge.id)
          tracked.edges << edge
          new_child = new_ox.nodes[index]
          if old_edge.node?
            edge.child_node = duplicate_node(old_edge.child_node, new_child, memo, new_world)
          else
            edge.literal = new_child
          end
        end
        tracked
      end

      def all_tracked_nodes
        (@nodes_by_ox_oid ||= {}).values.uniq
      end

      # Longest-common-subsequence alignment by object identity.  Equal keys
      # are aligned as far to the right as possible, which preserves the later
      # mount when Ox's object deletion consumes one duplicate object_id.
      def align_edges(old_edges, current_children)
        new_keys = current_children.map do |child|
          if child.is_a?(Ox::Node)
            [:ox_node, child.object_id]
          else
            [:literal, child.class, child]
          end
        end
        old_keys = old_edges.map(&:key)

        rows = old_edges.size
        cols = current_children.size
        dp = Array.new(rows + 1) { Array.new(cols + 1, 0) }

        (1..rows).each do |i|
          (1..cols).each do |j|
            dp[i][j] = if old_keys[i - 1] == new_keys[j - 1]
                         dp[i - 1][j - 1] + 1
                       else
                         [dp[i - 1][j], dp[i][j - 1]].max
                       end
          end
        end

        matched = {}
        i = rows
        j = cols
        while i.positive? && j.positive?
          if old_keys[i - 1] == new_keys[j - 1] &&
             dp[i][j] == dp[i - 1][j - 1] + 1
            matched[i - 1] = j - 1
            i -= 1
            j -= 1
          elsif dp[i - 1][j] >= dp[i][j - 1]
            i -= 1
          else
            j -= 1
          end
        end
        matched
      end
    end

    attr_reader :tree, :tickets, :registrations, :stations, :bins, :group_ticket, :options

    def initialize(input = nil, **kwargs)
      data = if input.is_a?(Hash)
               input.merge(kwargs)
             elsif input.nil?
               kwargs
             else
               kwargs.merge(tree: input)
             end
      @options = data
      @tree = data[:tree] || data['tree'] || data[:ox_tree] || data['ox_tree'] ||
              data[:document] || data['document'] || data[:xml] || data['xml']
      raise ArgumentError, 'an Ox tree or XML string is required' if @tree.nil?

      @tree = Ox.parse(@tree) if @tree.is_a?(String)
      @tree = self.class.normalize_tree(@tree)
      raw_registrations = data[:registrations] || data['registrations'] ||
                          data[:mounts] || data['mounts'] ||
                          data[:ledger] || data['ledger'] || []
      @registration_specs = self.class.normalize_registrations(raw_registrations)
      @registration_specs = resolve_registration_specs(@tree, @registration_specs)
      @tickets = self.class.normalize_tickets(data)
      @stations = self.class.normalize_stations(data, @tickets)
      @bins = self.class.normalize_bins(data, @stations, @tickets)
      group_data = data[:group_ticket] || data['group_ticket'] ||
                   data[:group_order] || data['group_order'] ||
                   data[:whole_group] || data['whole_group'] ||
                   data[:group] || data['group']
      if group_data.is_a?(String)
        group_data = { mode: :path, path: group_data }
      end
      @group_ticket = group_data ? self.class.build_ticket(group_data) : nil
      @group_ticket.id ||= :group if @group_ticket
      @mirror_config = self.class.normalize_mirror(data)
      infer_bin_metadata!
    end

    def infer_bin_metadata!
      @bins.each do |bin|
        if bin[:path].nil?
          receivers = bin[:stations].flat_map { |station| station[:tickets].map(&:receiver) }
                                    .compact
                                    .select { |receiver| receiver.is_a?(String) }
          bin[:path] = receivers.first if receivers.uniq.size == 1
        end

        next unless bin[:path] && bin[:mirror_path].nil?

        if @mirror_config[:left_path] && @mirror_config[:right_path] && bin[:path].start_with?(@mirror_config[:right_path])
          suffix = bin[:path][@mirror_config[:right_path].length..]
          bin[:mirror_path] = "#{@mirror_config[:left_path]}#{suffix}"
        elsif bin[:path].include?('group[-1]')
          bin[:mirror_path] = bin[:path].sub('group[-1]', 'group[0]')
        elsif bin[:path].include?('group[1]')
          bin[:mirror_path] = bin[:path].sub('group[1]', 'group[0]')
        end
      end
    end

    def analyze
      @tickets_by_id = @tickets.each_with_object({}) do |ticket, map|
        map[ticket.id] = ticket
        map[ticket.id.to_s] = ticket
        map[ticket.id.to_sym] = ticket if ticket.id.respond_to?(:to_sym)
      end
      if @group_ticket
        @tickets_by_id[:group] = @group_ticket
        @tickets_by_id['group'] = @group_ticket
        @tickets_by_id[@group_ticket.id] = @group_ticket
        @tickets_by_id[@group_ticket.id.to_s] = @group_ticket
      end
      @group_ticket&.freeze if @group_ticket
      @group_policy = build_group_policy
      bin_results = @bins.map { |bin| analyze_bin(bin) }
      @bin_results = bin_results
      total_actions = bin_results.sum { |result| result[:action_count] }
      dp = initial_position_dp

      bin_results.each do |bin_result|
        dp = combine_bin(dp, bin_result)
      end
      @witness_layers = build_witness_layers

      positions = (0..total_actions).map do |k|
        build_position(k, dp, total_actions)
      end

      result = Result.new(
        positions: positions,
        counts: positions.map(&:count),
        total_local_actions: total_actions
      )
      result.scheduler = self
      result
    end

    def resolve_registration_specs(tree, specs)
      specs.map do |spec|
        index_path = spec[:index_path]
        if index_path.nil? && spec[:path]
          index_path = self.class.index_path_for_path(tree, spec[:path])
        elsif index_path.nil? && spec[:node].is_a?(Ox::Node)
          paths = self.class.find_index_paths(tree, spec[:node].object_id)
          if paths.size == 1
            index_path = paths.first
          elsif paths.empty?
            raise ArgumentError, 'registration node is not present in tree'
          else
            raise ArgumentError, 'registration node is mounted multiple times; supply its mount path'
          end
        end
        raise ArgumentError, 'registration is missing a path or index_path' if index_path.nil?

        { code: spec[:code], index_path: index_path }
      end
    end

    def self.analyze(input = nil, **kwargs)
      new(input, **kwargs).analyze
    end

    def self.count_safe_positions(input = nil, **kwargs)
      analyze(input, **kwargs).counts
    end

    def self.safe_counts(input = nil, **kwargs)
      count_safe_positions(input, **kwargs)
    end

    def call
      analyze
    end

    def safe_counts
      analyze.counts
    end

    # Replay an explicit ordered id list.  The group ticket may appear anywhere
    # once.  Returns receipts for every step.
    def replay_order(order)
      ticket_map!
      world = World.new(@tree, @registration_specs)
      steps = []
      order.each do |ticket_id|
        ticket = @tickets_by_id[ticket_id] ||
                 @tickets_by_id[ticket_id.to_s] ||
                 (ticket_id.respond_to?(:to_sym) ? @tickets_by_id[ticket_id.to_sym] : nil)
        raise ReplayError, "unknown ticket #{ticket_id.inspect}" unless ticket

        receipt = execute_ticket(world, ticket)
        steps << receipt
        break unless receipt.ok
      end
      {
        ok: steps.all?(&:ok) && steps.size == order.size,
        steps: steps,
        removed_edge_ids: world.removed_edge_ids.to_a
      }
    end

    def ticket_map!
      @tickets_by_id ||= {}
      @tickets.each do |ticket|
        @tickets_by_id[ticket.id] = ticket
        @tickets_by_id[ticket.id.to_s] = ticket
        @tickets_by_id[ticket.id.to_sym] = ticket if ticket.id.respond_to?(:to_sym)
      end
      if @group_ticket
        @tickets_by_id[:group] = @group_ticket
        @tickets_by_id['group'] = @group_ticket
        @tickets_by_id[@group_ticket.id] = @group_ticket
        @tickets_by_id[@group_ticket.id.to_s] = @group_ticket
      end
      @tickets_by_id
    end

    def verify_sequence(order)
      replay_order(order)[:ok]
    end

    class << self
      def normalize_tree(tree)
        case tree
        when Ox::Document
          tree.root || tree
        else
          tree
        end
      end

      def deep_clone_ox(source, memo = {})
        return source if source.nil?
        return memo[source.object_id] if source.is_a?(Ox::Node) && memo.key?(source.object_id)

        case source
        when Ox::Element
          copy = source.class.allocate
          memo[source.object_id] = copy
          value = source.instance_variable_defined?(:@value) ? source.instance_variable_get(:@value) : source.value
          copy.instance_variable_set(:@value, value)
          attrs = source.instance_variable_defined?(:@attributes) ? source.instance_variable_get(:@attributes) : nil
          copy.instance_variable_set(:@attributes, attrs ? attrs.dup : {})
          copy.instance_variable_set(:@nodes, [])
          source.nodes.each do |child|
            copy.nodes << deep_clone_ox(child, memo)
          end
          copy
        when Ox::Node
          copy = source.class.allocate
          memo[source.object_id] = copy
          source.instance_variables.each do |var|
            value = source.instance_variable_get(var)
            copy.instance_variable_set(var, deep_clone_ox(value, memo))
          end
          copy
        when String
          source.dup
        when Array
          source.map { |item| deep_clone_ox(item, memo) }
        when Hash
          source.to_h { |key, value| [deep_clone_ox(key, memo), deep_clone_ox(value, memo)] }
        else
          source
        end
      end

      def structural_signature(node)
        case node
        when Ox::Element
          attrs = node.attributes || {}
          attr_sig = attrs.keys.map { |key| [key.to_s, attrs[key]] }.sort_by(&:first)
          [:element, node.name.to_s, attr_sig, node.nodes.map { |child| structural_signature(child) }]
        when Ox::Node
          [:node, node.class.name, node.value.to_s]
        when String
          [:text, node]
        else
          [:literal, node.class.name, node]
        end
      end

      def resolve_path(tree, path, base: nil)
        return nil if path.nil?

        path = path.to_s.sub(%r{\A/}, '')
        bases = [base, tree].compact.uniq
        bases.each do |candidate_base|
          found = candidate_base.locate(path) rescue []
          first = found&.first
          return first unless first.nil?
        end
        nil
      end

      # Resolve a simple Ox path to a child-index path from tree.  Falls back to
      # identity search when Ox locate finds a unique object occurrence.
      def index_path_for_path(tree, path, base: nil)
        target = resolve_path(tree, path, base: base)
        if target.nil?
          simple = simple_index_path(tree, path)
          return simple if simple
          raise ResolutionError, "registration path not found: #{path}"
        end
        return [] if target.equal?(tree)

        paths = find_index_paths(tree, target.object_id)
        return paths.first if paths.size == 1

        simple = simple_index_path(tree, path)
        return simple if simple && paths.include?(simple)
        raise ResolutionError, "registration path #{path} is ambiguous between #{paths.size} mounts"
      end

      def find_index_paths(node, target_oid, prefix = [])
        results = []
        if node.is_a?(Ox::Node)
          node.nodes.each_with_index do |child, index|
            path = prefix + [index]
            if child.is_a?(Ox::Node)
              results << path if child.object_id == target_oid
              results.concat(find_index_paths(child, target_oid, path))
            end
          end
        end
        results
      end

      def simple_index_path(tree, path)
        segments = path.to_s.sub(%r{\A/}, '').split('/')
        current = [[[], tree]]
        segments.each do |segment|
          next_current = []
          name, qualifier, index_value, attr_predicate = parse_segment(segment)
          current.each do |prefix, node|
            next unless node.is_a?(Ox::Element)

            matches = node.nodes.each_with_index.select do |child, child_index|
              child.is_a?(Ox::Element) && segment_name_match?(child, name) &&
                (!attr_predicate || attr_match?(child, attr_predicate))
            end
            selected = select_matches(matches, qualifier, index_value)
            selected.each do |child, child_index|
              next_current << [prefix + [child_index], child]
            end
          end
          current = next_current
        end
        current.first&.first
      end

      def parse_segment(segment)
        if (bracket = segment.index('['))
          name = segment[0...bracket]
          inner = segment[(bracket + 1)...-1]
          if inner.start_with?('@')
            attr_predicate = parse_attr_predicate(inner[1..])
            return [name, :attr, nil, attr_predicate]
          end

          if inner[0] =~ /[0-9]/
            return [name, :plus, inner.to_i, nil]
          end

          qualifier = inner[0]
          value = inner[1..].to_i
          symbol = { '-' => :minus, '<' => :lt, '>' => :gt }[qualifier]
          return [name, symbol, value, nil]
        end
        [segment, nil, nil, nil]
      end

      def parse_attr_predicate(text)
        key, value = text.split('=', 2)
        value = value[1...-1] if value && value.start_with?("'") && value.end_with?("'")
        value = value[1...-1] if value && value.start_with?('"') && value.end_with?('"')
        { key: key, value: value }
      end

      def segment_name_match?(element, name)
        return true if name == '?' || name == '*'

        element.name == name
      end

      def attr_match?(element, predicate)
        value = element.attributes[predicate[:key].to_sym] || element.attributes[predicate[:key]]
        if predicate[:value].nil?
          !value.nil?
        else
          value.to_s == predicate[:value].to_s
        end
      end

      def select_matches(matches, qualifier, index_value)
        elements = matches
        case qualifier
        when :plus
          index_value < elements.size ? [elements[index_value]] : []
        when :minus
          index_value <= elements.size ? [elements[-index_value]] : []
        when :lt
          index_value.positive? ? elements[0...index_value] : []
        when :gt
          index_value <= elements.size ? elements[(index_value + 1)..] || [] : []
        else
          elements
        end
      end

      def normalize_registrations(data)
        result = []
        case data
        when Hash
          data.each do |key, target|
            if key.is_a?(Ox::Node)
              Array(target).each do |code|
                result << { code: code, index_path: nil, node: key }
              end
            else
              Array(target).each do |item|
                result.concat(registration_specs_from(item, key))
              end
            end
          end
        when Array
          data.each_with_index do |item, index|
            result.concat(registration_specs_from(item, index + 1))
          end
        end
        result
      end

      def registration_specs_from(item, default_code)
        case item
        when Ox::Node
          [{ code: default_code, index_path: nil, node: item }]
        when String
          [{ code: default_code, path: item }]
        when Hash
          code = item[:code] || item['code'] || item[:registration] || item['registration'] ||
                 item[:registration_number] || item['registration_number'] ||
                 item[:id] || item['id'] || default_code
          path = item[:path] || item['path'] || item[:mount_path] || item['mount_path'] ||
                 item[:xpath] || item['xpath'] || item[:location] || item['location']
          index_path = item[:index_path] || item['index_path']
          node = item[:node] || item['node'] || item[:element] || item['element'] ||
                 item[:mount] || item['mount'] || item[:target] || item['target']
          if node.is_a?(String) && path.nil?
            path = node
            node = nil
          end
          [{ code: code, path: path, index_path: index_path, node: node }]
        else
          [{ code: default_code, index_path: nil }]
        end
      end

      def normalize_tickets(data)
        raw = data[:tickets] || data['tickets'] || data[:orders] || data['orders'] ||
              data[:local_orders] || data['local_orders'] || []
        tickets = []
        if raw.is_a?(Hash)
          raw.each do |station_id, list|
            Array(list).each { |item| tickets << build_ticket(item, station_id: station_id) }
          end
        else
          Array(raw).each { |item| tickets << build_ticket(item) }
        end

        stations_data = data[:stations] || data['stations'] || data[:workstations] || data['workstations']
        Array(stations_data).each do |station|
          id = station[:id] || station['id'] || station[:station_id] || station['station_id']
          list = station[:tickets] || station['tickets'] || station[:orders] || station['orders'] || []
          Array(list).each do |item|
            tickets << build_ticket(
              item,
              station_id: id,
              bin_id: station[:bin_id] || station['bin_id'] || station[:bin] || station['bin']
            )
          end
        end

        bins_data = data[:bins] || data['bins']
        bins_data.each do |bin|
          bin_id = bin[:id] || bin['id'] || bin[:bin_id] || bin['bin_id']
          nested_stations = bin[:stations] || bin['stations'] ||
                            bin[:workstations] || bin['workstations'] || []
          nested_stations.each do |station|
            station_id = station[:id] || station['id'] ||
                         station[:station_id] || station['station_id'] ||
                         station[:workstation_id] || station['workstation_id']
            list = station[:tickets] || station['tickets'] ||
                   station[:orders] || station['orders'] || []
            Array(list).each do |item|
              tickets << build_ticket(item, station_id: station_id, bin_id: bin_id)
            end
          end

          bin_tickets = bin[:tickets] || bin['tickets'] || bin[:orders] || bin['orders']
          Array(bin_tickets).each do |item|
            tickets << build_ticket(item, bin_id: bin_id)
          end
        end if bins_data.is_a?(Array)

        tickets.each_with_index { |ticket, index| ticket.id ||= "ticket-#{index + 1}" }
        tickets
      end

      def build_ticket(item, station_id: nil, bin_id: nil, id: nil)
        return item if item.is_a?(Ticket)

        hash = item.is_a?(Hash) ? item : { id: item }
        mode_value = hash[:mode] || hash['mode'] || hash[:type] || hash['type'] ||
                     hash[:delete_by] || hash['delete_by'] || hash[:deletion] || hash['deletion']
        mode = normalize_mode(mode_value, hash)
        receiver = hash[:receiver] || hash['receiver'] || hash[:receiver_path] || hash['receiver_path'] ||
                   hash[:parent] || hash['parent']
        argument = hash[:argument] || hash['argument'] || hash[:arg] || hash['arg'] ||
                   hash[:path] || hash['path'] || hash[:target_path] || hash['target_path'] ||
                   hash[:object_path] || hash['object_path'] || hash[:object] || hash['object'] ||
                   hash[:target] || hash['target']
        approved = hash[:approved] || hash['approved'] || hash[:approvals] || hash['approvals'] ||
                   hash[:approved_registrations] || hash['approved_registrations'] ||
                   hash[:allowed] || hash['allowed'] ||
                   hash[:registrations] || hash['registrations'] || hash[:expected] || hash['expected'] ||
                   hash[:manifest] || hash['manifest'] || []
        Ticket.new(
          id: id || hash[:id] || hash['id'] || hash[:ticket_id] || hash['ticket_id'] || hash[:order_id] || hash['order_id'],
          station_id: station_id || hash[:station_id] || hash['station_id'] || hash[:station] || hash['station'] ||
                      hash[:workstation_id] || hash['workstation_id'] || hash[:workstation] || hash['workstation'],
          bin_id: bin_id || hash[:bin_id] || hash['bin_id'] || hash[:bin] || hash['bin'] ||
                  hash[:container_id] || hash['container_id'] || hash[:box] || hash['box'],
          mode: mode,
          receiver: receiver,
          argument: argument,
          approved: Array(approved).map { |code| normalize_code(code) }.to_set,
          raw: hash
        )
      end

      def normalize_mode(value, hash)
        text = value.to_s
        return :path if text.match?(/path|路径|remove_children_by_path/i)
        return :object if text.match?(/object|对象|remove_children\z|by_object/i)

        if hash.key?(:object_path) || hash.key?('object_path') || hash.key?(:object) || hash.key?('object')
          :object
        elsif hash.key?(:path) || hash.key?('path') || hash.key?(:target_path) || hash.key?('target_path')
          :path
        else
          :path
        end
      end

      def normalize_code(code)
        code.is_a?(String) ? code : code.to_s
      end

      def normalize_stations(data, tickets)
        explicit = data[:stations] || data['stations'] || data[:workstations] || data['workstations']
        if explicit.is_a?(Array)
          explicit.map do |station|
            id = station[:id] || station['id'] || station[:station_id] || station['station_id']
            ticket_ids = station[:ticket_ids] || station['ticket_ids'] || station[:order_ids] || station['order_ids']
            if ticket_ids
              list = ticket_ids.map { |tid| tickets.find { |ticket| ticket.id == tid || ticket.id.to_s == tid.to_s } }.compact
            else
              list = tickets.select { |ticket| ticket.station_id == id || ticket.station_id&.to_s == id&.to_s }
            end
            { id: id, bin_id: station[:bin_id] || station['bin_id'], tickets: list }
          end
        else
          tickets.group_by(&:station_id).map do |station_id, list|
            { id: station_id, bin_id: list.first.bin_id, tickets: list }
          end
        end
      end

      def normalize_bins(data, stations, tickets)
        explicit = data[:bins] || data['bins']
        if explicit.is_a?(Array)
          explicit.map do |bin|
            id = bin[:id] || bin['id'] || bin[:bin_id] || bin['bin_id']
            bin_stations = stations.select do |station|
              station[:bin_id] == id || station[:bin_id]&.to_s == id&.to_s
            end
            if bin_stations.empty?
              bin_stations = tickets.select { |ticket| ticket.bin_id == id || ticket.bin_id&.to_s == id&.to_s }
                                    .group_by(&:station_id)
                                    .map { |sid, list| { id: sid, tickets: list } }
            end
            {
              id: id,
              path: bin[:path] || bin['path'],
              mirror_path: bin[:mirror_path] || bin['mirror_path'] || bin[:left_path] || bin['left_path'],
              stations: bin_stations
            }
          end
        else
          stations.group_by { |station| station[:bin_id] || station[:id] }
                  .map do |bin_id, bin_stations|
            { id: bin_id, path: nil, mirror_path: nil, stations: bin_stations }
          end
        end
      end

      def normalize_mirror(data)
        mirror = data[:mirror] || data['mirror']
        return { enabled: false } if mirror == false || mirror == :false

        left_path = data[:left_path] || data['left_path']
        right_path = data[:right_path] || data['right_path']
        if mirror.is_a?(Hash)
          {
            enabled: mirror.fetch(:enabled, true),
            left_path: mirror[:left_path] || mirror['left_path'] || left_path,
            right_path: mirror[:right_path] || mirror['right_path'] || right_path,
            unsafe_when_equal: mirror.fetch(:unsafe_when_equal, true)
          }
        else
          {
            enabled: mirror != false,
            left_path: left_path,
            right_path: right_path,
            unsafe_when_equal: true
          }
        end
      end
    end

    private

    def analyze_bin(bin)
      station_chains = bin[:stations].map { |station| station[:tickets] }
      action_count = station_chains.sum(&:size)
      world = World.new(@tree, @registration_specs)
      bin_index_path = bin[:path] ? world.index_path_for_path(bin[:path]) : nil
      mirror_signature = resolve_mirror_signature(world, bin)
      right_codes = registration_codes_under(bin_index_path)

      alternatives = []
      collect_prefixes(world, station_chains, Array.new(station_chains.size, 0), [], [], alternatives, bin_index_path, mirror_signature)

      {
        bin: bin,
        action_count: action_count,
        right_codes: right_codes,
        alternatives: alternatives
      }
    end

    def build_group_policy
      unless @group_ticket
        return { mode: :open, count_safe: true }
      end

      approved = @group_ticket.approved.map(&:to_s).to_set
      if @group_ticket.mode != :path || !mirror_active?
        baseline = World.new(@tree, @registration_specs)
        receipt = execute_ticket(baseline, @group_ticket)
        return { mode: :baseline, count_safe: receipt.ok, approved: approved }
      end

      left_index = safe_index_path(@mirror_config[:left_path] || 'group[0]')
      right_index = safe_index_path(@mirror_config[:right_path] || 'group[-1]')
      left_codes = registration_codes_under(left_index)
      right_codes = registration_codes_under(right_index)

      {
        mode: :mirror_path,
        approved: approved,
        left_codes: left_codes,
        right_codes: right_codes,
        diff_safe: approved == left_codes,
        eq_possible: approved >= left_codes && (approved - left_codes) <= right_codes
      }
    end

    def safe_index_path(path)
      self.class.index_path_for_path(@tree, path)
    rescue StandardError
      nil
    end

    def registration_codes_under(prefix_path)
      return Set.new unless prefix_path

      @registration_specs.filter_map do |spec|
        spec[:code].to_s if index_path_prefix?(spec[:index_path], prefix_path)
      end.to_set
    end

    def index_path_prefix?(path, prefix)
      return false unless path && prefix && path.size >= prefix.size

      path.first(prefix.size) == prefix
    end

    def eq_group_option_safe?(option, bin_right_codes)
      return false unless option[:matches_mirror]
      return false unless @group_policy[:eq_possible]

      approved = @group_policy[:approved]
      expected_removed = bin_right_codes - approved
      actual_removed = option[:removed_codes] || Set.new
      (actual_removed & bin_right_codes) == expected_removed
    end

    def resolve_mirror_signature(world, bin)
      path = bin[:mirror_path] || @mirror_config[:left_path]
      return nil unless path

      mirror_node = world.resolve_path(path)
      mirror_node ? world.structural_signature(mirror_node) : nil
    end

    def collect_prefixes(world, chains, progress, sequence, receipts, alternatives, bin_index_path, mirror_signature)
      bin_live = bin_index_path.nil? || world.chain_live?(chain_from_index(world, bin_index_path))
      current_signature = nil
      matches_mirror = false
      if bin_live
        bin_node = bin_index_path.nil? ? nil : node_at_index_path(world.tree, bin_index_path)
        current_signature = world.structural_signature(bin_node) if bin_node
        matches_mirror = if mirror_signature
                          current_signature == mirror_signature
                        else
                          sequence.empty?
                        end
      end

      child_choices = []
      terminal = progress.each_with_index.all? { |done, index| done >= chains[index].size }

      chains.each_with_index do |chain, station_index|
        next if progress[station_index] >= chain.size

        ticket = chain[progress[station_index]]
        receipt = execute_ticket(world, ticket)
        next unless receipt.ok

        change_set = world.last_change_set
        next_progress = progress.dup
        next_progress[station_index] += 1
        child = collect_prefixes(
          world,
          chains,
          next_progress,
          sequence + [ticket.id],
          receipts + [receipt],
          alternatives,
          bin_index_path,
          mirror_signature
        )
        world.undo_change_set(change_set)
        child_choices << [ticket.id, child]
      end

      if terminal
        suffix_count = 1
        suffix_witness = []
      else
        suffix_count = child_choices.sum { |(_ticket_id, child)| child[:suffix_count] }
        first_choice = child_choices.find { |(_ticket_id, child)| child[:suffix_count].positive? }
        suffix_witness = if first_choice
                           [first_choice[0]] + first_choice[1][:suffix_witness]
                         end
      end

      alternative = {
        progress: progress.dup,
        completed: progress.sum,
        sequence: sequence.dup,
        receipts: receipts.dup,
        removed_codes: receipts.flat_map { |receipt| receipt.ok ? receipt.removed : [] }.to_set,
        suffix_count: suffix_count,
        suffix_witness: suffix_witness,
        matches_mirror: matches_mirror,
        bin_live: bin_live,
        signature: current_signature
      }
      alternatives << alternative
      alternative
    end

    def chain_from_index(world, index_path)
      world.chain_for_index_path(world.tree, world.root_node, index_path)
    end

    def node_at_index_path(root, index_path)
      node = root
      index_path.each { |index| node = node.nodes[index] }
      node
    end

    def execute_ticket(world, ticket)
      previously_removed = world.removed_edge_ids.dup
      resolved_receiver = nil
      resolved_argument = nil
      error = nil
      details = {}

      receiver = if ticket.receiver.nil?
                   world.tree
                 elsif ticket.receiver.is_a?(Ox::Node)
                   clone_node_reference(world, ticket.receiver)
                 else
                   world.resolve_path(ticket.receiver.to_s)
                 end

      if receiver.nil?
        error = :receiver_not_found
      elsif !receiver.is_a?(Ox::Element)
        error = :receiver_not_element
      else
        resolved_receiver = receiver
        begin
          if ticket.mode == :object
            argument = resolve_object_argument(world, receiver, ticket.argument)
            if argument.nil?
              error = :argument_not_found
            else
              resolved_argument = argument
              receiver.remove_children(argument)
            end
          else
            path = ticket.argument.to_s
            matches = receiver.locate(path) rescue []
            if matches.nil? || matches.empty?
              error = :argument_not_found
            else
              resolved_argument = matches.first
              receiver.remove_children_by_path(path)
            end
          end
        rescue StandardError => e
          error = :"#{e.class.name}: #{e.message}"
        end
      end

      newly_removed, restorations = world.reconcile!
      world.last_change_set = { removed_ids: newly_removed, restorations: restorations }
      removed = world.codes_disappearing(newly_removed, previously_removed).to_a.map(&:to_s).sort
      approved = ticket.approved.map(&:to_s).sort
      ok = error.nil? && removed == approved

      StepReceipt.new(
        ticket_id: ticket.id,
        phase: ticket.id == :group ? :group : :local,
        ok: ok,
        removed: removed,
        approved: approved,
        error: error,
        details: details.merge(newly_removed_edges: newly_removed.to_a)
      )
    end

    def resolve_object_argument(world, receiver, argument)
      case argument
      when Ox::Node
        clone_node_reference(world, argument)
      when nil
        nil
      else
        text = argument.to_s
        world.resolve_path(text) || receiver.locate(text)&.first
      end
    end

    def clone_node_reference(world, original_node)
      world.node_for_ox(original_node)&.ox
    end

    def initial_position_dp
      {
        eq: [Rational(1)],
        safe_eq: [Rational(1)],
        diff: [Rational(0)],
        eq_witness: [[[]]],
        diff_witness: [[nil]]
      }
    end

    def combine_bin(dp, bin_result)
      action_count = bin_result[:action_count]
      max_k = dp[:eq].size - 1 + action_count
      eq = Array.new(max_k + 1, Rational(0))
      safe_eq = Array.new(max_k + 1, Rational(0))
      diff = Array.new(max_k + 1, Rational(0))
      eq_witness = Array.new(max_k + 1)
      diff_witness = Array.new(max_k + 1)

      mirror_policy = @group_policy[:mode] == :mirror_path
      diff_allowed = !mirror_policy || @group_policy[:diff_safe]

      option_groups = bin_result[:alternatives].group_by { |alternative| alternative[:completed] }
      option_groups.each do |completed, options|
        options.each do |option|
          suffix_count = option[:suffix_count]
          next if suffix_count <= 0

          weight = Rational(
            suffix_count,
            factorial(completed) * factorial(action_count - completed)
          )
          option_key = {
            bin_id: bin_result[:bin][:id],
            sequence: option[:sequence],
            suffix_witness: option[:suffix_witness],
            completed: completed,
            matches_mirror: option[:matches_mirror]
          }
          approval_safe = !mirror_policy || eq_group_option_safe?(option, bin_result[:right_codes])

          (0...dp[:eq].size).each do |previous_k|
            target_k = previous_k + completed

            if option[:matches_mirror]
              add_weight(eq, eq_witness, target_k, dp[:eq][previous_k], weight,
                         dp[:eq_witness][previous_k], option_key)
              if approval_safe
                safe_eq[target_k] += dp[:safe_eq][previous_k] * weight unless dp[:safe_eq][previous_k].zero?
              end
            elsif diff_allowed
              add_weight(diff, diff_witness, target_k, dp[:eq][previous_k], weight,
                         dp[:eq_witness][previous_k], option_key)
            end

            if diff_allowed
              add_weight(diff, diff_witness, target_k, dp[:diff][previous_k], weight,
                         dp[:diff_witness][previous_k], option_key)
            end
          end
        end
      end

      { eq: eq, safe_eq: safe_eq, diff: diff, eq_witness: eq_witness, diff_witness: diff_witness }
    end

    def add_weight(weights, witnesses, index, previous_weight, option_weight, previous_witness, option_key)
      return if previous_weight.nil? || previous_weight.zero?

      contribution = previous_weight * option_weight
      weights[index] += contribution
      witnesses[index] || Array(previous_witness) + [option_key] if contribution.positive?
    end

    def build_position(k, dp, total_actions)
      factor = factorial(k) * factorial(total_actions - k)
      safe_equal_weight = dp[:safe_eq][k] || Rational(0)
      different_weight = dp[:diff][k] || Rational(0)
      generic_weight = (dp[:eq][k] || Rational(0)) + different_weight
      count = if @group_policy[:mode] == :baseline && !@group_policy[:count_safe]
                0
              elsif @group_policy[:mode] == :mirror_path
                equal_weight = k == total_actions ? safe_equal_weight : Rational(0)
                ((equal_weight + different_weight) * factor).to_i
              else
                (generic_weight * factor).to_i
              end

      sequence = count.positive? ? build_global_witness(k) : nil

      Position.new(
        local_count: k,
        count: count,
        sequence: sequence,
        steps: nil,
        safe: count.positive?
      )
    end

    def mirror_active?
      @mirror_config[:left_path] || @mirror_config[:right_path] ||
        @bins.any? { |bin| bin[:mirror_path] } || @options[:mirror]
    end

    def factorial(number)
      (1..number.to_i).inject(1) { |product, value| product * value }
    end

    def build_witness_layers
      root_entry = { prev_k: 0, prev_state: :safe_eq, alt: nil }
      initial = { 0 => { eq: root_entry, safe_eq: root_entry, diff: nil } }
      layers = [initial]

      @bin_results.each do |bin_result|
        mirror_policy = @group_policy[:mode] == :mirror_path
        representatives = {}
        bin_result[:alternatives].each do |alt|
          next if alt[:suffix_count] <= 0

          completed = alt[:completed]
          reps = representatives[completed] ||= {
            structural_eq: nil, safe_eq: nil, diff: nil, any: nil
          }
          reps[:any] ||= alt
          if alt[:matches_mirror]
            reps[:structural_eq] ||= alt
            if !mirror_policy || eq_group_option_safe?(alt, bin_result[:right_codes])
              reps[:safe_eq] ||= alt
            end
          else
            reps[:diff] ||= alt
          end
        end

        previous = layers.last
        max_k = previous.keys.max + bin_result[:action_count]
        current = {}
        (0..max_k).each { |k| current[k] = { eq: nil, safe_eq: nil, diff: nil } }

        previous.each do |previous_k, states|
          representatives.each do |completed, reps|
            target_k = previous_k + completed

            if states[:eq] && reps[:structural_eq] && current[target_k][:eq].nil?
              current[target_k][:eq] = {
                prev_k: previous_k, prev_state: :eq, alt: reps[:structural_eq]
              }
            end

            if states[:safe_eq] && reps[:safe_eq] && current[target_k][:safe_eq].nil?
              current[target_k][:safe_eq] = {
                prev_k: previous_k, prev_state: :safe_eq, alt: reps[:safe_eq]
              }
            end

            if states[:eq] && reps[:diff] && current[target_k][:diff].nil?
              current[target_k][:diff] = {
                prev_k: previous_k, prev_state: :eq, alt: reps[:diff]
              }
            end

            if states[:diff]
              alt = reps[:any]
              if alt && current[target_k][:diff].nil?
                current[target_k][:diff] = {
                  prev_k: previous_k, prev_state: :diff, alt: alt
                }
              end
            end
          end
        end

        layers << current
      end
      layers
    end

    def build_global_witness(target_k)
      final_states = @witness_layers.last[target_k]
      return nil unless final_states

      state_name = if @group_policy[:mode] == :mirror_path
                     if @group_policy[:diff_safe] && final_states[:diff]
                       :diff
                     elsif @group_policy[:eq_possible] && final_states[:safe_eq]
                       :safe_eq
                     end
                   elsif @group_policy[:mode] == :baseline && !@group_policy[:count_safe]
                     nil
                   else
                     final_states[:eq] ? :eq : :diff
                   end
      return nil unless state_name

      entry = final_states[state_name]
      return nil unless entry

      selected = Array.new(@bin_results.size)
      k = target_k

      (@bin_results.size - 1).downto(0) do |bin_index|
        entry = @witness_layers[bin_index + 1][k][state_name]
        return nil unless entry&.dig(:alt)

        selected[bin_index] = entry[:alt]
        k = entry[:prev_k]
        state_name = entry[:prev_state]
      end

      prefix = []
      suffix = []
      selected.each do |option|
        prefix.concat(option[:sequence])
        suffix.concat(option[:suffix_witness] || [])
      end
      prefix + [@group_ticket&.id || :group] + suffix
    end

  end

  CancellationPlanner = CancellationScheduler
  CancelOrderScheduler = CancellationScheduler
  WithdrawalScheduler = CancellationScheduler
end

unless Object.const_defined?(:CancellationScheduler)
  CancellationScheduler = Ox::CancellationScheduler
end
unless Object.const_defined?(:CancellationPlanner)
  CancellationPlanner = Ox::CancellationPlanner
end
