# frozen_string_literal: false

require_relative "rss"
require_relative "1.0"
require_relative "parser"
require_relative "maker"
require_relative "content"
require_relative "dublincore"

module RSS
  ##
  # Reorders RSS 1.0 items from several feeds into one feed.
  #
  # RSS 1.0 keeps the original order in <tt>channel/items/rdf:Seq</tt>, not in
  # the order of the <tt>item</tt> elements. A feed may also mention the same
  # <tt>rdf:resource</tt> more than once. Blend therefore works with
  # _occurrences_ (one <tt>rdf:li</tt> position in a source Seq), not with bare
  # URLs. Callers map each occurrence to a logical slot explicitly:
  #
  #   blend = RSS::Blend.new
  #   blend.add_source("morning", morning_rss,
  #                    slots: ["x", "y", "a", "b"])
  #   blend.add_source("evening", evening_rss,
  #                    slots: ["a", "b"])
  #
  # Rules refer to slots. Hard rules must all hold. Preferences are soft and
  # have a weight; Blend chooses a maximum-weight acyclic set that also honours
  # every hard rule.
  #
  # == Example
  #
  #   blend.add_hard("a", "x")
  #   blend.add_hard("b", "x")
  #   blend.add_preference("x", "y", 7, source: "morning")
  #   blend.add_preference("y", "a", 4, source: "morning")
  #   blend.add_preference("y", "b", 5, source: "morning")
  #   result = blend.solve(reference: "morning")
  #   result.order  # => ["y", "a", "b", "x"]
  #   result.score  # => 9
  #
  # The 7 point x-before-y rule is dropped because keeping it would make the
  # 4 and 5 point rules (9 points) impossible. +dropped_rules+ explains that
  # conflict. A caller can name a source (or an explicit slot list) as
  # +reference+; among equally good orders Blend picks the one with fewest
  # pairwise inversions against that source's original Seq order.
  class Blend
    class Error < RSS::Error; end

    # Raised when occurrence-to-slot mapping is missing, ambiguous, or refers
    # to unknown slots.
    class MappingError < Error; end

    # Raised before solving when hard rules contain a cycle.
    class HardCycleError < Error
      attr_reader :cycles

      def initialize(cycles)
        @cycles = cycles
        description = cycles.collect(&:to_s).join("; ")
        super("contradictory hard constraints: #{description}")
      end
    end

    # Raised when an exact solution is required but a component is larger than
    # the configured exact solver limit.
    class NotOptimalError < Error; end

    # Raised when every slot is pending and no placeholder is requested, so
    # RSS 1.0 cannot be serialized with the required item element.
    class NoItemsError < Error; end

    # Raised by Result#verify_readback! when re-reading the generated feed
    # through RSS::Parser does not reproduce the planned Seq positions.
    class ReadbackError < Error
      attr_reader :report

      def initialize(report)
        @report = report
        super("generated feed could not be verified: #{report.errors.join('; ')}")
      end
    end

    # A hard or soft ordering edge between two slots.
    class Rule
      attr_reader :before, :after, :weight, :source, :id, :kind, :origin

      def initialize(before, after, weight: nil, source: nil, id: nil,
                     kind: :soft, origin: nil)
        @before = before.to_s
        @after = after.to_s
        @weight = weight
        @source = source.nil? ? nil : source.to_s
        @id = id
        @kind = kind
        @origin = origin
      end

      def hard?
        @kind == :hard
      end

      def soft?
        @kind == :soft
      end

      def pair
        [@before, @after]
      end

      def label
        if soft?
          "#{@before} -> #{@after} (#{@weight})"
        else
          "#{@before} -> #{@after} (hard)"
        end
      end

      def to_s
        attributes = []
        attributes << "source=#{@source}" if @source
        attributes << "id=#{@id}" if @id
        return label if attributes.empty?
        "#{label} [#{attributes.join(', ')}]"
      end
    end

    class HardCycle
      attr_reader :slots, :rules

      def initialize(slots, rules)
        @slots = slots
        @rules = rules
      end

      def to_s
        path = @slots.collect(&:to_s).join(" -> ")
        path << " -> #{@slots.first}" unless @slots.empty?
        details = @rules.collect(&:to_s)
        details.empty? ? path : "#{path} (#{details.join('; ')})"
      end
    end

    # One position in a source's original Seq.
    class Occurrence
      attr_reader :source, :index, :slot, :resource, :item

      def initialize(source:, index:, slot:, resource:, item:)
        @source = source
        @index = index
        @slot = slot
        @resource = resource
        @item = item
      end

      # One based position in the source Seq.
      def position
        @index + 1
      end

      def ready?
        !@item.nil?
      end

      def pending?
        @item.nil?
      end

      def fill(item)
        @item = item
      end

      def to_s
        "#{@source}:#{position} (#{@slot}, #{@resource})"
      end
    end

    class Source
      attr_reader :name, :rss, :channel, :occurrences

      def initialize(name:, rss:, channel:, occurrences:)
        @name = name
        @rss = rss
        @channel = channel
        @occurrences = occurrences
      end

      def slots
        @occurrences.collect(&:slot)
      end

      def resources
        @occurrences.collect(&:resource)
      end

      def pending_occurrences
        @occurrences.select(&:pending?)
      end

      def ready_occurrences
        @occurrences.select(&:ready?)
      end

      def to_s
        @name
      end
    end

    # One final state per logical slot.
    class Slot
      attr_reader :id, :occurrences, :state, :resource, :item, :warnings

      def initialize(id:, occurrences:, state:, resource:, item:,
                     warnings: [])
        @id = id
        @occurrences = occurrences
        @state = state
        @resource = resource
        @item = item
        @warnings = warnings
      end

      def ready?
        @state == :ready
      end

      def pending?
        @state == :pending
      end

      def sources
        @occurrences.collect(&:source).uniq
      end

      def resources
        @occurrences.collect(&:resource).uniq
      end

      def to_s
        "#{@id} (#{@state}, #{@resource})"
      end
    end

    # Why a soft rule is not present in the selected order.
    class DroppedRule
      attr_reader :rule, :reason, :path, :message, :conflicting_rules,
                  :conflicting_weight

      def initialize(rule:, reason:, path:, message:,
                     conflicting_rules: [], conflicting_weight: 0)
        @rule = rule
        @reason = reason
        @path = path
        @message = message
        @conflicting_rules = conflicting_rules
        @conflicting_weight = conflicting_weight
      end

      def before
        @rule.before
      end

      def after
        @rule.after
      end

      def weight
        @rule.weight
      end

      def to_s
        @message
      end
    end

    # A comparison implied by one source's original Seq order.
    class SourceOrderCheck
      attr_reader :source, :before, :after, :before_index, :after_index,
                  :satisfied, :reason, :path, :message, :rule

      def initialize(source:, before:, after:, before_index:, after_index:,
                     satisfied:, reason: nil, path: [], message: nil, rule: nil)
        @source = source
        @before = before
        @after = after
        @before_index = before_index
        @after_index = after_index
        @satisfied = satisfied
        @reason = reason
        @path = path
        @message = message
        @rule = rule
      end

      def satisfied?
        @satisfied
      end

      def unsatisfied?
        !@satisfied
      end

      def to_s
        @message || "#{@source}: #{@before} before #{@after} " \
                    "#{@satisfied ? 'kept' : 'dropped'}"
      end
    end

    # Result of re-reading the generated feed with RSS::Parser.
    class ReadbackReport
      attr_reader :positions, :resources, :item_resources, :pending_positions,
                  :errors, :strict, :actual_positions

      def initialize(positions:, resources:, item_resources:,
                     pending_positions:, errors:, strict:, actual_positions: {})
        @positions = positions
        @resources = resources
        @item_resources = item_resources
        @pending_positions = pending_positions
        @errors = errors
        @strict = strict
        @actual_positions = actual_positions
      end

      def ok?
        @errors.empty?
      end

      alias_method :valid?, :ok?

      def strict?
        @strict
      end

      def position(slot)
        @actual_positions[slot.to_s] || @positions[slot.to_s]
      end

      def to_s
        if ok?
          "readback ok: #{@positions.inspect}"
        else
          "readback failed: #{@errors.join('; ')}"
        end
      end
    end

    ##
    # Solved ordering and the generated RSS 1.0 feed.
    class Result
      attr_reader :order, :score, :slots, :selected_rules, :dropped_rules,
                  :reference_order, :optimal, :sources, :hard_rules,
                  :soft_rules

      def initialize(order:, score:, slots:, selected_rules:, dropped_rules:,
                     reference_order:, optimal:, sources:, hard_rules:,
                     soft_rules:, channel:, include_pending_placeholder:)
        @order = order
        @score = score
        @slots = slots
        @selected_rules = selected_rules
        @dropped_rules = dropped_rules
        @reference_order = reference_order
        @optimal = optimal
        @sources = sources
        @hard_rules = hard_rules
        @soft_rules = soft_rules
        @channel = channel
        @include_pending_placeholder = include_pending_placeholder
        @slot_by_id = {}
        @slots.each {|slot| @slot_by_id[slot.id] = slot}
        @positions = {}
        @order.each_with_index {|slot_id, i| @positions[slot_id] = i + 1}
        @source_order_checks = nil
      end

      def optimal?
        @optimal
      end

      def slot(id)
        @slot_by_id[id.to_s]
      end

      def position(id)
        @positions[id.to_s]
      end

      def positions
        @positions.dup
      end

      # Slots in the final Seq order.
      def ordered_slots
        @order.collect {|id| @slot_by_id[id] }
      end

      def resources
        ordered_slots.collect(&:resource)
      end

      def pending_slots
        ordered_slots.select(&:pending?)
      end

      def ready_slots
        ordered_slots.select(&:ready?)
      end

      def unsatisfied_rules
        @dropped_rules
      end

      alias_method :discarded_rules, :unsatisfied_rules

      # Checks implied by each source Seq. By default every pair of distinct
      # slots in that source is compared; pass <tt>pairs: :consecutive</tt> to
      # compare only adjacent occurrences.
      def source_order_checks(source: nil, pairs: :all)
        if source.nil? && pairs == :all
          return @source_order_checks ||= build_source_order_checks(nil, pairs)
        end
        build_source_order_checks(source, pairs)
      end

      alias_method :source_orders, :source_order_checks

      def diagnostics
        {
          :order => @order.dup,
          :score => @score,
          :optimal => @optimal,
          :dropped_rules => @dropped_rules.collect do |dropped|
            {
              :before => dropped.before,
              :after => dropped.after,
              :weight => dropped.weight,
              :reason => dropped.reason,
              :message => dropped.message,
              :path => dropped.path.collect(&:label),
            }
          end,
          :source_orders => source_order_checks.collect do |check|
            {
              :source => check.source,
              :before => check.before,
              :after => check.after,
              :satisfied => check.satisfied?,
              :reason => check.reason,
              :message => check.message,
            }
          end,
          :pending_slots => pending_slots.collect(&:id),
          :warnings => @slots.flat_map(&:warnings),
        }
      end

      def to_rss(include_pending_placeholder: nil)
        if include_pending_placeholder.nil?
          include_pending_placeholder = @include_pending_placeholder
        end

        if ordered_slots.empty?
          raise NoItemsError, "no slots: add at least one source before " \
                              "publishing"
        end

        has_body = ordered_slots.any?(&:ready?)
        if !has_body && !include_pending_placeholder
          raise NoItemsError,
                "all #{ordered_slots.size} slot(s) are pending; RSS 1.0 needs " \
                "at least one item element (pass include_pending_placeholder: " \
                "true to publish pending placeholders or fetch a body first)"
        end

        rss = RSS::RDF.new
        channel = RSS::RDF::Channel.new(channel_value(:about, "urn:rss-blend"))
        channel.title = channel_value(:title, "Blended RSS feed")
        channel.link = channel_value(:link, default_link)
        channel.description = channel_value(:description, default_description)

        items = RSS::RDF::Channel::Items.new
        ordered_slots.each do |slot|
          items.Seq.lis << RSS::RDF::Channel::Items::Seq::Li.new(slot.resource)
        end
        channel.items = items
        rss.channel = channel

        emitted = {}
        ordered_slots.each do |slot|
          next if emitted.key?(slot.resource)
          if slot.ready?
            rss.items << build_output_item(slot)
            emitted[slot.resource] = true
          elsif include_pending_placeholder
            rss.items << build_pending_item(slot)
            emitted[slot.resource] = true
          end
        end

        rss
      end

      def to_s
        to_rss.to_s
      end

      def readback(include_pending_placeholder: nil)
        if include_pending_placeholder.nil?
          include_pending_placeholder = @include_pending_placeholder
        end
        build_readback(:include_pending_placeholder =>
                         include_pending_placeholder)
      end

      def verify_readback!(include_pending_placeholder: nil)
        report = readback(:include_pending_placeholder =>
                            include_pending_placeholder)
        raise ReadbackError.new(report) unless report.ok?
        report
      end

      def report
        lines = []
        lines << "order: #{@order.join(' ')}"
        lines << "score: #{@score} " \
                 "(#{@optimal ? 'optimal' : 'not proven optimal'})"
        unless @dropped_rules.empty?
          lines << "dropped preferences:"
          @dropped_rules.each {|dropped| lines << "  - #{dropped.message}" }
        end
        warnings = @slots.flat_map(&:warnings)
        unless warnings.empty?
          lines << "warnings:"
          warnings.each {|warning| lines << "  - #{warning}" }
        end
        unless pending_slots.empty?
          lines << "pending slots:"
          pending_slots.each do |slot|
            lines << "  - #{slot.id}: #{slot.resource} (no returned body)"
          end
        end
        unsatisfied = source_order_checks.select(&:unsatisfied?)
        unless unsatisfied.empty?
          lines << "source orders not kept:"
          unsatisfied.each {|check| lines << "  - #{check.message}" }
        end
        lines.join("\n")
      end

      private
      def channel_value(name, default)
        value = @channel[name] || @channel[name.to_s]
        value.nil? ? default : value
      end

      def default_link
        source = @sources.find do |candidate|
          candidate.channel && candidate.channel.link &&
            !candidate.channel.link.empty?
        end
        source ? source.channel.link : "urn:rss-blend"
      end

      def default_description
        "Blended from #{@sources.size} RSS 1.0 feed(s)"
      end

      def build_output_item(slot)
        item = slot.item.dup
        item.about = slot.resource
        if item.title.nil? or item.title.empty?
          item.title = slot.id
        end
        if item.link.nil? or item.link.empty?
          item.link = slot.resource
        end
        item
      end

      def build_pending_item(slot)
        item = RSS::RDF::Item.new(slot.resource)
        item.title = "[pending] #{slot.id}"
        item.link = slot.resource
        item
      end

      def build_readback(include_pending_placeholder: @include_pending_placeholder)
        errors = []
        strict = true
        parsed = nil
        begin
          xml = to_rss(:include_pending_placeholder =>
                         include_pending_placeholder).to_s
        rescue NoItemsError => exception
          return ReadbackReport.new(:positions => @positions.dup,
                                    :resources => [],
                                    :item_resources => [],
                                    :pending_positions =>
                                      pending_slots.each_with_object({}) do |slot, hash|
                                        hash[slot.id] = @positions[slot.id]
                                      end,
                                    :errors => [exception.message],
                                    :strict => false)
        end
        begin
          parsed = RSS::Parser.parse(xml)
        rescue RSS::Error => exception
          strict = false
          begin
            parsed = RSS::Parser.parse(xml, false)
          rescue RSS::Error => fallback_exception
            errors << "parse failed: #{fallback_exception.class}: " \
                      "#{fallback_exception.message}"
          end
          if parsed.nil?
            errors << "strict parse failed: #{exception.class}: " \
                      "#{exception.message}"
          end
        end

        resources = []
        item_resources = []
        actual_positions = {}
        if parsed && parsed.channel && parsed.channel.items &&
            parsed.channel.items.Seq
          resources = parsed.channel.items.Seq.lis.collect(&:resource)
          resources.each_with_index do |resource, i|
            slot_id = planned_slot_for_position(i, resource)
            actual_positions[slot_id] = i + 1 if slot_id
          end
        end
        item_resources = parsed.items.collect(&:about) if parsed

        expected_resources = seq_resources_for_readback
        unless resources == expected_resources
          errors << "Seq resources differ: expected " \
                    "#{expected_resources.inspect}, got #{resources.inspect}"
        end

        expected_items = resources_for_readback(
          include_pending_placeholder: include_pending_placeholder)
        unless item_resources == expected_items
          errors << "item resources differ: expected #{expected_items.inspect}, " \
                    "got #{item_resources.inspect}"
        end

        pending_positions = {}
        pending_slots.each do |slot|
          pending_positions[slot.id] = @positions[slot.id]
        end

        ReadbackReport.new(:positions => @positions.dup,
                           :resources => resources,
                           :item_resources => item_resources,
                           :pending_positions => pending_positions,
                           :errors => errors,
                           :strict => strict,
                           :actual_positions => actual_positions)
      end

      def planned_slot_for_position(index, resource)
        slot = ordered_slots[index]
        return nil if slot.nil?
        return nil unless slot.resource == resource
        slot.id
      end

      def seq_resources_for_readback
        ordered_slots.collect(&:resource)
      end

      def resources_for_readback(include_pending_placeholder: false)
        slots = include_pending_placeholder ? ordered_slots : ready_slots
        slots.collect(&:resource).uniq
      end

      def build_source_order_checks(source_filter, pairs)
        selected_sources = @sources
        unless source_filter.nil?
          selected_sources = selected_sources.select do |source|
            source.name == source_filter.to_s
          end
        end

        checks = []
        selected_sources.each do |source|
          occurrences = source.occurrences
          pairs_to_check =
            if pairs == :all
              occurrence_pairs(occurrences)
            else
              occurrences.each_cons(2).to_a
            end
          pairs_to_check.each do |before_occurrence, after_occurrence|
            next if before_occurrence.slot == after_occurrence.slot
            checks << build_source_order_check(before_occurrence,
                                               after_occurrence)
          end
        end
        checks
      end

      def occurrence_pairs(occurrences)
        pairs = []
        occurrences.each_with_index do |before, i|
          ((i + 1)...occurrences.size).each do |j|
            pairs << [before, occurrences[j]]
          end
        end
        pairs
      end

      def build_source_order_check(before_occurrence, after_occurrence)
        before = before_occurrence.slot
        after = after_occurrence.slot
        satisfied = @positions[before] < @positions[after]
        if satisfied
          return SourceOrderCheck.new(:source => before_occurrence.source,
                                      :before => before,
                                      :after => after,
                                      :before_index => before_occurrence.position,
                                      :after_index => after_occurrence.position,
                                      :satisfied => true)
        end

        path = find_path(after, before)
        reason = path_reason(path)
        rule = matching_rule(before, after)
        message = explain_source_order(before_occurrence, after_occurrence,
                                       path, reason, rule)
        SourceOrderCheck.new(:source => before_occurrence.source,
                             :before => before,
                             :after => after,
                             :before_index => before_occurrence.position,
                             :after_index => after_occurrence.position,
                             :satisfied => false,
                             :reason => reason,
                             :path => path,
                             :message => message,
                             :rule => rule)
      end

      def matching_rule(before, after)
        exact = @soft_rules.find do |rule|
          rule.before == before && rule.after == after
        end
        return exact if exact
        @hard_rules.find do |rule|
          rule.before == before && rule.after == after
        end
      end

      def explain_source_order(before_occurrence, after_occurrence, path,
                               reason, rule)
        prefix = "#{before_occurrence.source}: Seq position " \
                 "#{before_occurrence.position} #{before_occurrence.slot} " \
                 "before position #{after_occurrence.position} " \
                 "#{after_occurrence.slot} was not kept"
        if rule && rule.soft?
          prefix << " (the #{rule.weight} point rule was rejected)"
        end
        "#{prefix}: #{reason_text(reason, path)}"
      end

      def reason_text(reason, path)
        case reason
        when :hard_conflict
          "it contradicts hard constraints #{path_labels(path)}"
        when :conflicts_with_selected_order
          "the selected order requires #{path_labels(path)}"
        when :unconstrained_tie_break
          "no explicit rule kept it; the reference/tie-break order placed " \
          "it later"
        else
          "not selected"
        end
      end

      def path_labels(path)
        path.collect(&:label).join(" -> ")
      end

      def path_reason(path)
        return :unconstrained_tie_break if path.nil? or path.empty?
        if path.all?(&:hard?)
          :hard_conflict
        else
          :conflicts_with_selected_order
        end
      end

      def find_path(from, to)
        return [] if from == to
        graph = Hash.new {|hash, key| hash[key] = [] }
        (@hard_rules + @selected_rules).each do |rule|
          graph[rule.before] << rule
        end

        queue = [from]
        visited = {from => true}
        previous = {}
        until queue.empty?
          node = queue.shift
          graph[node].each do |rule|
            next_node = rule.after
            next if visited[next_node]
            visited[next_node] = true
            previous[next_node] = rule
            if next_node == to
              path = []
              current = to
              while previous.key?(current)
                edge = previous[current]
                path.unshift(edge)
                current = edge.before
              end
              return path
            end
            queue << next_node
          end
        end
        nil
      end
    end

    attr_reader :source_list, :hard_rules, :soft_rules, :channel
    attr_accessor :reference, :exact_limit, :allow_heuristic,
                  :require_optimal, :include_pending_placeholder

    def initialize(channel: {}, reference: nil, exact_limit: 20,
                   allow_heuristic: true, require_optimal: false,
                   include_pending_placeholder: false, heuristic_width: 200)
      @source_list = []
      @sources_by_name = {}
      @hard_rules = []
      @soft_rules = []
      @source_preference_rules = []
      @channel = normalize_channel(channel)
      @reference = reference
      @exact_limit = exact_limit
      @allow_heuristic = allow_heuristic
      @require_optimal = require_optimal
      @include_pending_placeholder = include_pending_placeholder
      @heuristic_width = heuristic_width
      @slot_occurrences = {}
      @slot_order = []
    end

    def sources
      @source_list
    end

    def source(name)
      @sources_by_name[name.to_s]
    end

    def slots
      @slot_order.dup
    end

    def channel=(channel)
      @channel = normalize_channel(channel)
    end

    ##
    # Adds a source feed. +slots+ (or its alias +mapping+/+occurrences+) is
    # required, because a bare URL must never decide on its own that two
    # occurrences are the same slot.
    #
    # Accepted mappings:
    #
    # [Array]                       One slot id per Seq position.
    # [Hash with Integer keys]      Seq position (1 based unless 0 is used) =>
    #                               slot id.
    # [Hash with resource keys]     Resource URL => slot id, or an Array of
    #                               slot ids when that URL occurs repeatedly.
    # [:occurrences]                Give every occurrence its own slot id named
    #                               <tt>source:position</tt>.
    #
    # A +nil+ entry in an Array gives that occurrence its own
    # <tt>source:position</tt> slot.
    def add_source(name, feed, slot_mapping = nil, slots: nil, mapping: nil,
                   occurrences: nil, parse_options: {})
      source_name = name.to_s
      if @sources_by_name.key?(source_name)
        raise Error, "duplicate source name: #{source_name}"
      end

      mapping ||= slot_mapping
      mapping ||= slots
      mapping ||= occurrences
      if mapping.nil?
        raise MappingError,
              "source #{source_name} needs an explicit occurrence-to-slot " \
              "mapping; use slots: [...] (a bare URL cannot identify a slot)"
      end

      rss, channel = load_feed(feed, parse_options)
      resources = seq_resources(rss, source_name)
      slot_ids = normalize_mapping(source_name, resources, mapping)
      item_queues = build_item_queues(rss)
      seen_counts = Hash.new(0)

      occurrence_objects = []
      resources.each_with_index do |resource, index|
        slot_id = slot_ids[index]
        candidates = item_queues[resource]
        item = nil
        if candidates && !candidates.empty?
          ordinal = seen_counts[resource]
          item = candidates[[ordinal, candidates.size - 1].min]
          seen_counts[resource] += 1
        end
        occurrence = Occurrence.new(:source => source_name,
                                    :index => index,
                                    :slot => slot_id,
                                    :resource => resource,
                                    :item => item)
        occurrence_objects << occurrence
        @slot_order << slot_id unless @slot_occurrences.key?(slot_id)
        (@slot_occurrences[slot_id] ||= []) << occurrence
      end

      source = Source.new(:name => source_name,
                          :rss => rss,
                          :channel => channel,
                          :occurrences => occurrence_objects)
      @source_list << source
      @sources_by_name[source_name] = source
      source
    end

    alias_method :add_feed, :add_source

    ##
    # Adds a source from raw Seq resources, without an item body. This is
    # mainly useful for a source that has not been fetched yet; every position
    # is pending unless it is later filled by #fill_slot.
    def add_seq_source(name, resources, slot_mapping = nil, slots: nil,
                       mapping: nil)
      source_name = name.to_s
      if @sources_by_name.key?(source_name)
        raise Error, "duplicate source name: #{source_name}"
      end
      mapping ||= slot_mapping
      mapping ||= slots
      if mapping.nil?
        raise MappingError,
              "source #{source_name} needs an explicit occurrence-to-slot " \
              "mapping"
      end
      slot_ids = normalize_mapping(source_name, resources, mapping)
      occurrence_objects = resources.each_with_index.collect do |resource, index|
        occurrence = Occurrence.new(:source => source_name,
                                    :index => index,
                                    :slot => slot_ids[index],
                                    :resource => resource,
                                    :item => nil)
        @slot_order << occurrence.slot unless @slot_occurrences.key?(occurrence.slot)
        (@slot_occurrences[occurrence.slot] ||= []) << occurrence
        occurrence
      end
      source = Source.new(:name => source_name, :rss => nil, :channel => nil,
                          :occurrences => occurrence_objects)
      @source_list << source
      @sources_by_name[source_name] = source
      source
    end

    ##
    # Fills a pending slot with a fetched item. The slot's resource and
    # position stay as declared; another slot with the same name is never
    # borrowed as a substitute.
    def fill_slot(slot_id, item)
      occurrences = @slot_occurrences[slot_id.to_s]
      if occurrences.nil? or occurrences.empty?
        raise Error, "unknown slot: #{slot_id}"
      end
      unless item.respond_to?(:about)
        raise ArgumentError, "expected an RSS item, got #{item.class}"
      end
      if occurrences.all?(&:ready?)
        raise Error, "slot already has an item: #{slot_id}"
      end
      occurrences.each {|occurrence| occurrence.fill(item) unless occurrence.ready? }
      item
    end

    def add_hard(before, after, source: nil, id: nil)
      rule = Rule.new(before, after, :source => source, :id => id,
                      :kind => :hard)
      @hard_rules << rule
      rule
    end

    alias_method :add_hard_rule, :add_hard

    def add_preference(before, after, weight, source: nil, id: nil)
      validate_weight!(weight)
      rule = Rule.new(before, after, :weight => weight, :source => source,
                      :id => id, :kind => :soft)
      @soft_rules << rule
      rule
    end

    alias_method :add_soft_rule, :add_preference
    alias_method :prefer, :add_preference

    # Adds soft rules for each adjacent (or all) pair of distinct slots in a
    # source Seq. This is only a convenience; no rule is implied
    # automatically by adding a source.
    def add_source_preferences(name, weight: 1, pairs: :consecutive,
                               source: nil)
      source_object = source(name)
      raise Error, "unknown source: #{name}" if source_object.nil?
      validate_weight!(weight)

      if pairs == :all
        occurrence_pairs(source_object.occurrences)
      else
        source_object.occurrences.each_cons(2).to_a
      end.each do |before_occurrence, after_occurrence|
        next if before_occurrence.slot == after_occurrence.slot
        rule = add_preference(before_occurrence.slot, after_occurrence.slot,
                              weight,
                              :source => source || source_object.name,
                              :id => "source-order")
        @source_preference_rules << rule
      end
      @source_preference_rules
    end

    ##
    # Solves the ordering and returns a Result. Hard cycles are reported before
    # any feed is emitted.
    def solve(reference: nil, exact_limit: nil, allow_heuristic: nil,
              require_optimal: nil, channel: nil,
              include_pending_placeholder: nil)
      validate_rule_slots!
      cycles = hard_cycles
      raise HardCycleError.new(cycles) unless cycles.empty?

      exact_limit ||= @exact_limit
      allow_heuristic = @allow_heuristic if allow_heuristic.nil?
      require_optimal = @require_optimal if require_optimal.nil?
      include_pending_placeholder = @include_pending_placeholder if
        include_pending_placeholder.nil?
      reference ||= @reference
      channel_hash = @channel.merge(normalize_channel(channel))

      reference_order = build_reference_order(reference)
      slot_objects = build_slot_objects(reference)
      slot_ids = @slot_order.dup

      order, _score, optimal = solve_components(slot_ids, reference_order,
                                               exact_limit: exact_limit,
                                               allow_heuristic: allow_heuristic,
                                               require_optimal: require_optimal)

      selected_rules = @soft_rules.select do |rule|
        position(rule.before, order) < position(rule.after, order)
      end
      score = selected_rules.inject(0) {|sum, rule| sum + rule.weight }

      dropped_rules = build_dropped_rules(order, selected_rules)
      Result.new(:order => order,
                 :score => score,
                 :slots => slot_objects,
                 :selected_rules => selected_rules,
                 :dropped_rules => dropped_rules,
                 :reference_order => reference_order,
                 :optimal => optimal,
                 :sources => @source_list,
                 :hard_rules => @hard_rules,
                 :soft_rules => @soft_rules,
                 :channel => channel_hash,
                 :include_pending_placeholder => include_pending_placeholder)
    end

    def to_rss(**options)
      solve(**options).to_rss
    end

    def diagnostics(**options)
      solve(**options).diagnostics
    end

    # Public pre-flight check. Returns cycles; does not raise.
    def check_hard_cycles
      validate_rule_slots!
      hard_cycles
    end

    private
    def normalize_channel(channel)
      return {} if channel.nil?
      normalized = {}
      channel.each {|key, value| normalized[key.to_sym] = value }
      normalized
    end

    def validate_weight!(weight)
      unless weight.is_a?(Numeric) and weight > 0
        raise ArgumentError, "preference weight must be positive: #{weight.inspect}"
      end
    end

    def load_feed(feed, parse_options)
      rss =
        if feed.is_a?(RSS::RDF)
          feed
        else
          source = read_feed_source(feed)
          parse_rss(source, parse_options)
        end
      unless rss.is_a?(RSS::RDF)
        raise Error, "not an RSS 1.0 feed: #{rss.class}"
      end
      channel = rss.channel
      if channel.nil? or channel.items.nil? or channel.items.Seq.nil?
        raise Error, "RSS 1.0 feed has no channel/items/rdf:Seq"
      end
      [rss, channel]
    end

    def read_feed_source(feed)
      if feed.respond_to?(:read)
        feed.read
      elsif feed.is_a?(String)
        if feed.lstrip.start_with?("<")
          feed
        elsif File.file?(feed)
          File.binread(feed)
        else
          feed
        end
      else
        feed.to_s
      end
    end

    def parse_rss(source, parse_options)
      begin
        RSS::Parser.parse(source, parse_options)
      rescue RSS::Error
        options = {:validate => false}.merge(parse_options)
        RSS::Parser.parse(source, options)
      end
    end

    def seq_resources(rss, source_name)
      seq = rss.channel.items.Seq
      seq.lis.collect do |li|
        if li.resource.nil?
          raise Error, "source #{source_name} has a Seq entry without rdf:resource"
        end
        li.resource
      end
    end

    # A Seq may reference one rdf:about repeatedly while returning a single
    # item body; that body serves every occurrence. If a source returns more
    # than one item element with the same about, occurrences take them in
    # document order (extra occurrences reuse the last body). A slot with no
    # item body at all stays pending and is never filled from another slot.
    def build_item_queues(rss)
      queues = {}
      rss.items.each do |item|
        (queues[item.about] ||= []) << item
      end
      queues
    end

    def normalize_mapping(source_name, resources, mapping)
      case mapping
      when :occurrences, "occurrences"
        resources.each_index.collect do |index|
          "#{source_name}:#{index + 1}"
        end
      when Array
        if mapping.size != resources.size
          raise MappingError,
                "source #{source_name}: mapping has #{mapping.size} entries " \
                "but Seq has #{resources.size} positions"
        end
        mapping.each_with_index.collect do |slot, index|
          slot.nil? ? "#{source_name}:#{index + 1}" : slot.to_s
        end
      when Hash
        normalize_hash_mapping(source_name, resources, mapping)
      else
        raise MappingError,
              "source #{source_name}: unsupported mapping #{mapping.class}"
      end
    end

    def normalize_hash_mapping(source_name, resources, mapping)
      if mapping.empty?
        raise MappingError, "source #{source_name}: empty occurrence mapping"
      end

      integer_keys = mapping.keys.count {|key| key.is_a?(Integer) }
      if integer_keys == mapping.size
        return normalize_position_mapping(source_name, resources, mapping)
      elsif integer_keys > 0
        raise MappingError,
              "source #{source_name}: mapping keys must be all Seq " \
              "positions or all resources, not mixed"
      end

      normalize_resource_mapping(source_name, resources, mapping)
    end

    def normalize_position_mapping(source_name, resources, mapping)
      zero_based = mapping.key?(0)
      offset = zero_based ? 0 : 1
      normalized = {}
      mapping.each do |position, slot|
        normalized[position.to_i - offset] = slot
      end

      missing = (0...resources.size).reject {|index| normalized.key?(index) }
      extra = normalized.keys.reject {|index| index >= 0 && index < resources.size }
      unless missing.empty? && extra.empty?
        raise MappingError,
              "source #{source_name}: position mapping must cover " \
              "0...#{resources.size} exactly (missing #{missing.inspect}, " \
              "extra #{extra.inspect})"
      end

      (0...resources.size).collect do |index|
        slot = normalized[index]
        if slot.nil?
          raise MappingError,
                "source #{source_name}: Seq position #{index + 1} has no slot"
        end
        slot.to_s
      end
    end

    def normalize_resource_mapping(source_name, resources, mapping)
      normalized_keys = {}
      mapping.each do |resource, slot|
        normalized_keys[resource.to_s] = slot
      end

      # A scalar maps every occurrence of that resource to one slot. An Array
      # gives one slot per occurrence, in Seq order, which is how a repeated
      # URL can be split into distinct slots.
      queues = {}
      reused = {}
      normalized_keys.each do |resource, slot|
        if slot.is_a?(Array)
          queues[resource] = slot.collect(&:to_s)
        else
          reused[resource] = slot.to_s
        end
      end

      resources.collect.with_index do |resource, index|
        queue = queues[resource.to_s]
        if queue
          if queue.empty?
            raise MappingError,
                  "source #{source_name}: no slot mapping left for resource " \
                  "#{resource.inspect} at Seq position #{index + 1}; " \
                  "add another slot id for this occurrence"
          end
          queue.shift
        elsif reused.key?(resource.to_s)
          reused[resource.to_s]
        else
          raise MappingError,
                "source #{source_name}: no slot mapping for resource " \
                "#{resource.inspect} at Seq position #{index + 1}"
        end
      end
    end

    def occurrence_pairs(occurrences)
      pairs = []
      occurrences.each_with_index do |before, i|
        ((i + 1)...occurrences.size).each do |j|
          pairs << [before, occurrences[j]]
        end
      end
      pairs
    end

    def validate_rule_slots!
      known = {}
      @slot_occurrences.each_key {|slot| known[slot] = true}
      (@hard_rules + @soft_rules).each do |rule|
        [rule.before, rule.after].each do |slot|
          unless known[slot]
            raise Error, "rule refers to unknown slot #{slot.inspect}: #{rule}"
          end
        end
      end
    end

    def hard_cycles
      nodes = @slot_order.dup
      adjacency = {}
      nodes.each {|node| adjacency[node] = [] }
      @hard_rules.each do |rule|
        adjacency[rule.before] ||= []
        adjacency[rule.before] << rule.after
      end

      index = 0
      stack = []
      on_stack = {}
      indexes = {}
      lowlinks = {}
      components = []

      visit = lambda do |node|
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack << node
        on_stack[node] = true

        (adjacency[node] || []).each do |neighbor|
          if !indexes.key?(neighbor)
            visit.call(neighbor)
            lowlinks[node] = [lowlinks[node], lowlinks[neighbor]].min
          elsif on_stack[neighbor]
            lowlinks[node] = [lowlinks[node], indexes[neighbor]].min
          end
        end

        if lowlinks[node] == indexes[node]
          component = []
          begin
            member = stack.pop
            on_stack[member] = false
            component << member
          end until member == node
          components << component
        end
      end

      nodes.each {|node| visit.call(node) unless indexes.key?(node) }

      cycles = []
      components.each do |component|
        component_set = {}
        component.each {|node| component_set[node] = true}
        internal_rules = @hard_rules.select do |rule|
          component_set[rule.before] && component_set[rule.after]
        end
        self_loop = internal_rules.any? do |rule|
          rule.before == rule.after
        end
        next if component.size == 1 && !self_loop

        cycle_nodes, cycle_rules = find_hard_cycle(component, internal_rules)
        cycles << HardCycle.new(cycle_nodes, cycle_rules)
      end
      cycles
    end

    def find_hard_cycle(component, rules)
      adjacency = Hash.new {|hash, key| hash[key] = [] }
      rules.each do |rule|
        adjacency[rule.before] << [rule.after, rule]
      end

      component.each do |start|
        self_rule = rules.find do |rule|
          rule.before == start && rule.after == start
        end
        return [[start], [self_rule]] if self_rule
      end

      colors = {}
      node_stack = []
      edge_stack = []

      search = lambda do |node|
        colors[node] = :gray
        node_stack << node
        adjacency[node].each do |neighbor, rule|
          if colors[neighbor] == :gray
            index = node_stack.index(neighbor)
            return [node_stack[index..-1], edge_stack[index..-1] + [rule]]
          elsif colors[neighbor].nil?
            edge_stack << rule
            found = search.call(neighbor)
            return found if found
            edge_stack.pop
          end
        end
        colors[node] = :black
        node_stack.pop
        nil
      end

      component.each do |start|
        next if colors[start]
        found = search.call(start)
        return found if found
      end

      [component, rules]
    end

    def build_reference_order(reference)
      reference_slots =
        case reference
        when nil
          []
        when Source
          reference.slots
        when Array
          reference.collect(&:to_s)
        else
          source = source(reference)
          source ? source.slots : []
        end

      order = []
      reference_slots.each do |slot|
        order << slot unless order.include?(slot)
      end
      @slot_order.each do |slot|
        order << slot unless order.include?(slot)
      end
      order
    end

    def build_slot_objects(reference)
      @slot_order.collect do |slot_id|
        occurrences = @slot_occurrences[slot_id]
        occurrence_priority = occurrences.sort_by do |occurrence|
          [source_priority(occurrence.source, reference),
           occurrence.index]
        end
        ready_occurrence = occurrence_priority.find(&:ready?)
        resource_occurrence =
          if reference
            reference_occurrences = occurrences.select do |occurrence|
              reference_name =
                if reference.is_a?(Source)
                  reference.name
                elsif reference.is_a?(Array)
                  nil
                else
                  reference.to_s
                end
              reference_name && occurrence.source == reference_name
            end
            reference_occurrences.first || ready_occurrence || occurrences.first
          else
            ready_occurrence || occurrences.first
          end

        item = ready_occurrence ? ready_occurrence.item : nil
        resources = occurrences.collect(&:resource).uniq
        warnings = []
        if resources.size > 1
          warnings << "slot #{slot_id} is mapped from different resources " \
                      "#{resources.inspect}; using #{resource_occurrence.resource}"
        end
        Slot.new(:id => slot_id,
                 :occurrences => occurrences,
                 :state => item ? :ready : :pending,
                 :resource => resource_occurrence.resource,
                 :item => item,
                 :warnings => warnings)
      end
    end

    def source_priority(source_name, reference)
      if reference.is_a?(Source) && source_name == reference.name
        -1
      elsif !reference.is_a?(Array) && !reference.nil? &&
          source_name == reference.to_s
        -1
      else
        @source_list.index {|source| source.name == source_name } || 999_999
      end
    end

    def solve_components(slot_ids, reference_order, exact_limit:,
                         allow_heuristic:, require_optimal:)
      components = constraint_components(slot_ids)
      reference_rank = {}
      reference_order.each_with_index {|slot, i| reference_rank[slot] = i}
      reference_rank.default = reference_order.size

      optimal = true
      component_orders = []
      components.each do |component|
        component_ids = component.select {|slot| slot_ids.include?(slot) }
        if component_ids.size <= exact_limit
          order, = solve_exact(component_ids, reference_order)
        elsif require_optimal
          raise NotOptimalError,
                "component of #{component_ids.size} slots exceeds exact " \
                "limit #{exact_limit}; raise exact_limit or omit " \
                "require_optimal"
        elsif allow_heuristic
          order, = solve_heuristic(component_ids, reference_order)
          optimal = false
        else
          raise NotOptimalError,
                "component of #{component_ids.size} slots exceeds exact " \
                "limit #{exact_limit}"
        end
        component_orders << [order, component_ids]
      end

      # Components have no rules between them. Merge their internal orders by
      # repeatedly taking the available head with the smallest reference rank;
      # this keeps every component order intact and minimizes cross-component
      # inversions against the reference order.
      queues = component_orders.collect {|order, _| order.dup }
      order = []
      until queues.all?(&:empty?)
        best_queue = nil
        queues.each_with_index do |queue, index|
          next if queue.empty?
          if best_queue.nil? or
              reference_rank[queue.first] < reference_rank[queues[best_queue].first]
            best_queue = index
          end
        end
        order << queues[best_queue].shift
      end

      [order, 0, optimal]
    end

    # Weakly connected components of the slot graph induced by hard and soft
    # rules. Components are returned in first-appearance order.
    def constraint_components(slot_ids)
      parent = {}
      slot_ids.each {|slot| parent[slot] = slot}

      find_root = lambda do |slot|
        root = slot
        root = parent[root] while parent[root] != root
        while parent[slot] != root
          next_slot = parent[slot]
          parent[slot] = root
          slot = next_slot
        end
        root
      end

      (@hard_rules + @soft_rules).each do |rule|
        next unless parent.key?(rule.before) && parent.key?(rule.after)
        before_root = find_root.call(rule.before)
        after_root = find_root.call(rule.after)
        parent[after_root] = before_root unless before_root == after_root
      end

      groups = {}
      order = []
      slot_ids.each do |slot|
        root = find_root.call(slot)
        unless groups.key?(root)
          groups[root] = []
          order << root
        end
        groups[root] << slot
      end
      order.collect {|root| groups[root] }
    end

    def solve_exact(slot_ids, reference_order)
      size = slot_ids.size
      return [[], 0] if size == 0

      index_of = {}
      slot_ids.each_with_index {|slot, i| index_of[slot] = i}
      reference_rank = {}
      reference_order.each_with_index do |slot, i|
        reference_rank[slot] = i
      end
      reference_rank.default = reference_order.size
      ranks = slot_ids.collect {|slot| reference_rank[slot] }

      hard_predecessors = Array.new(size, 0)
      @hard_rules.each do |rule|
        before = index_of[rule.before]
        after = index_of[rule.after]
        next if before.nil? || after.nil?
        hard_predecessors[after] |= (1 << before)
      end

      incoming_soft = Array.new(size) { [] }
      @soft_rules.each do |rule|
        before = index_of[rule.before]
        after = index_of[rule.after]
        next if before.nil? || after.nil?
        incoming_soft[after] << [before, rule.weight]
      end

      greater_mask = Array.new(size, 0)
      size.times do |v|
        mask = 0
        size.times do |u|
          mask |= (1 << u) if ranks[u] > ranks[v]
        end
        greater_mask[v] = mask
      end

      state_count = 1 << size
      scores = Array.new(state_count)
      inversions = Array.new(state_count)
      parents = Array.new(state_count)
      scores[0] = 0
      inversions[0] = 0

      full_mask = state_count - 1
      mask = 0
      while mask <= full_mask
        current_score = scores[mask]
        unless current_score.nil?
          size.times do |candidate|
            bit = 1 << candidate
            next if (mask & bit) != 0
            next unless (hard_predecessors[candidate] & ~mask) == 0

            gain = 0
            incoming_soft[candidate].each do |before, weight|
              gain += weight if (mask & (1 << before)) != 0
            end
            added_inversions = popcount(mask & greater_mask[candidate])
            next_mask = mask | bit
            next_score = current_score + gain
            next_inversions = inversions[mask] + added_inversions

            old_score = scores[next_mask]
            if old_score.nil? or next_score > old_score or
                (next_score == old_score and
                 next_inversions < inversions[next_mask])
              scores[next_mask] = next_score
              inversions[next_mask] = next_inversions
              parents[next_mask] = candidate
            end
          end
        end
        mask += 1
      end

      if scores[full_mask].nil?
        raise HardCycleError.new([HardCycle.new([], @hard_rules)])
      end

      order = []
      mask = full_mask
      while mask != 0
        candidate = parents[mask]
        order.unshift(slot_ids[candidate])
        mask ^= (1 << candidate)
      end

      [order, scores[full_mask]]
    end

    def solve_heuristic(slot_ids, reference_order)
      size = slot_ids.size
      return [[], 0] if size == 0

      index_of = {}
      slot_ids.each_with_index {|slot, i| index_of[slot] = i}
      reference_rank = {}
      reference_order.each_with_index {|slot, i| reference_rank[slot] = i}
      reference_rank.default = reference_order.size
      ranks = slot_ids.collect {|slot| reference_rank[slot]}

      hard_predecessors = Array.new(size, 0)
      @hard_rules.each do |rule|
        before = index_of[rule.before]
        after = index_of[rule.after]
        next if before.nil? || after.nil?
        hard_predecessors[after] |= (1 << before)
      end

      incoming_soft = Array.new(size) { [] }
      @soft_rules.each do |rule|
        before = index_of[rule.before]
        after = index_of[rule.after]
        next if before.nil? || after.nil?
        incoming_soft[after] << [before, rule.weight]
      end

      beam = [[0, 0, 0, []]]
      full_mask = (1 << size) - 1
      size.times do
        next_beam = {}
        beam.each do |mask, score, inv, order|
          size.times do |candidate|
            bit = 1 << candidate
            next if (mask & bit) != 0
            next unless (hard_predecessors[candidate] & ~mask) == 0
            gain = 0
            incoming_soft[candidate].each do |before, weight|
              gain += weight if (mask & (1 << before)) != 0
            end
            next_mask = mask | bit
            next_score = score + gain
            next_inv = inv + order.count {|prior| ranks[prior] > ranks[candidate]}
            existing = next_beam[next_mask]
            if existing.nil? or next_score > existing[1] or
                (next_score == existing[1] and next_inv < existing[2])
              next_beam[next_mask] = [next_mask, next_score, next_inv,
                                      order + [candidate]]
            end
          end
        end
        beam = next_beam.values.sort_by do |state|
          [-state[1], state[2], state[3]]
        end.first(@heuristic_width)
        break if beam.empty?
      end

      best = beam.find {|mask, _, _, _| mask == full_mask}
      best ||= beam.first
      if best.nil?
        raise HardCycleError.new([HardCycle.new([], @hard_rules)])
      end
      order = best[3].collect {|candidate| slot_ids[candidate]}
      [order, best[1]]
    end

    def build_dropped_rules(order, selected_rules)
      selected_ids = {}
      selected_rules.each {|rule| selected_ids[rule.object_id] = true}

      @soft_rules.reject do |rule|
        selected_ids.key?(rule.object_id)
      end.collect do |rule|
        path = find_rule_path(rule.after, rule.before, selected_rules)
        conflicting_rules = rules_blocking(rule, selected_rules)
        conflicting_weight = conflicting_rules.inject(0) do |sum, conflict|
          sum + conflict.weight
        end
        if path && !path.empty?
          if path.all?(&:hard?)
            reason = :hard_conflict
            detail = "it contradicts hard constraints #{path_labels(path)}"
          else
            reason = :conflicts_with_selected_order
            detail = "the chosen order keeps #{path_labels(path)}"
          end
          unless conflicting_rules.empty?
            names = conflicting_rules.collect(&:label).join(" and ")
            detail << "; keeping #{rule.label} would require dropping " \
                      "#{names} worth #{conflicting_weight} point(s), while " \
                      "#{rule.label} is worth #{rule.weight}"
          end
        else
          reason = :unconstrained_tie_break
          detail = "the chosen order places #{rule.after} before #{rule.before} " \
                   "without an explicit contrary preference (tie-break)"
        end
        DroppedRule.new(:rule => rule,
                        :reason => reason,
                        :path => path || [],
                        :message => "#{rule} dropped: #{detail}",
                        :conflicting_rules => conflicting_rules,
                        :conflicting_weight => conflicting_weight)
      end
    end

    # Soft rules that would close a cycle if +candidate+ were also kept.
    def rules_blocking(candidate, selected_rules)
      selected_rules.select do |rule|
        next false if rule.object_id == candidate.object_id
        reachable?(rule.after, rule.before, @hard_rules + selected_rules) ||
          reachable?(candidate.after, candidate.before,
                     @hard_rules + [rule])
      end
    end

    def reachable?(from, to, rules)
      return true if from == to
      graph = Hash.new {|hash, key| hash[key] = [] }
      rules.each {|rule| graph[rule.before] << rule.after }
      queue = [from]
      visited = {from => true}
      until queue.empty?
        node = queue.shift
        graph[node].each do |neighbor|
          next if visited[neighbor]
          return true if neighbor == to
          visited[neighbor] = true
          queue << neighbor
        end
      end
      false
    end

    def find_rule_path(from, to, selected_rules)
      return [] if from == to
      graph = Hash.new {|hash, key| hash[key] = [] }
      (@hard_rules + selected_rules).each do |rule|
        graph[rule.before] << rule
      end

      queue = [from]
      visited = {from => true}
      previous = {}
      until queue.empty?
        node = queue.shift
        graph[node].each do |rule|
          next_node = rule.after
          next if visited[next_node]
          visited[next_node] = true
          previous[next_node] = rule
          if next_node == to
            path = []
            current = to
            while previous.key?(current)
              edge = previous[current]
              path.unshift(edge)
              current = edge.before
            end
            return path
          end
          queue << next_node
        end
      end
      nil
    end

    def path_labels(path)
      path.collect(&:label).join(" -> ")
    end

    def position(slot, order)
      order.index(slot) || order.size
    end

    def popcount(value)
      count = 0
      while value > 0
        value &= value - 1
        count += 1
      end
      count
    end
  end
end
