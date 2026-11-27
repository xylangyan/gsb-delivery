# frozen_string_literal: true

require 'set'

module VolumeCombine
  # Resolves every internal reference (page link, bookmark) and every external
  # work order name against the physical output slots described by the
  # manifest.
  #
  # Routing rule for a reference issued from group +group+ toward
  # (source, page):
  #
  # 1. an actual occurrence inside the SAME group wins;
  # 2. otherwise only a manifest permit may redirect to an occurrence in the
  #    permitted group;
  # 3. anything else is a reported gap. A same-named page of another period is
  #    never used as an implicit fallback.
  #
  # The planner is the single decision engine. The combiner and the auditor
  # both consume its decision maps, so nothing can be interpreted twice.
  class Planner
    Decision = Struct.new(
      :ref_type, :key, :origin_label, :source, :source_page, :group,
      :original_kind, :original_target, :view,
      :output_slot, :basis, :detail,
      keyword_init: true
    )

    NameConflict = Struct.new(:name, :sources, keyword_init: true)
    Gap = Struct.new(:ref_type, :key, :origin_label, :source, :source_page, :group, :reason, keyword_init: true)

    attr_reader :link_decisions, :bookmark_decisions, :work_order_decisions,
                :name_conflicts, :gaps

    def initialize(manifest, raw_models)
      @manifest = manifest
      @raw_models = raw_models
      @link_decisions = {}
      @bookmark_decisions = {}
      @work_order_decisions = {}
      @gaps = []
      @name_conflicts = []
    end

    def plan
      analyze_name_conflicts
      plan_page_links
      plan_bookmarks
      plan_work_orders
      self
    end

    def occurrences(source, page_index)
      @manifest.slots.select { |slot| slot.source == source && slot.page_index == page_index }
    end

    # Core routing rule. Returns [slot_or_nil, basis, detail].
    def route(source, page_index, group)
      same_group = occurrences(source, page_index).select { |slot| slot.group == group }
      unless same_group.empty?
        return [same_group.first, :same_group, "group #{group} occurrence of #{source} p#{page_index + 1}"]
      end

      @manifest.permits.each do |permit|
        next unless permit.source == source &&
                    permit.page_index == page_index &&
                    permit.target_group == group
        substitute = occurrences(source, page_index).find { |slot| slot.group == permit.group }
        if substitute
          return [substitute, :permit,
                  "permit allows group #{group} to use the group #{permit.group} occurrence of #{source} p#{page_index + 1}"]
        end
      end

      [nil, :gap,
       "no same-group occurrence and no permitted substitute for #{source} p#{page_index + 1} from group #{group}"]
    end

    # Ordered, depth first list of bookmark decisions in the exact chapter and
    # sibling order the combiner emits. Unroutable nodes (gaps) are omitted,
    # matching the nodes dropped from the output outline.
    def bookmark_walk
      walk = []
      chapters = @manifest.slots.map { |slot| [slot.group, slot.source] }.uniq
      chapters.each do |group, source|
        model = @raw_models.fetch(source)
        model.outline_roots.each { |node| collect_bookmark(node, source, group, walk) }
      end
      walk
    end

    private

    def collect_bookmark(node, source, group, walk)
      decision = @bookmark_decisions[[group, source, node.id]]
      walk << decision if decision
      node.children.each { |child| collect_bookmark(child, source, group, walk) }
    end

    def analyze_name_conflicts
      seen = Hash.new { |hash, key| hash[key] = Set.new }
      @raw_models.each do |source, model|
        model.name_dests.each { |dest| seen[dest.name] << source }
      end
      @name_conflicts = seen.select { |_name, sources| sources.length > 1 }
                           .map { |name, sources| NameConflict.new(name: name, sources: sources.to_a.sort) }
    end

    def plan_page_links
      @manifest.slots.each do |slot|
        model = @raw_models.fetch(slot.source)
        model.links.each do |link|
          next unless link.page_index == slot.page_index
          key = [slot.slot, link.order]

          if link.kind == :uri
            @link_decisions[key] = Decision.new(
              ref_type: :uri, key: key,
              origin_label: "slot #{slot.slot} (#{slot.source} p#{slot.page_index + 1}) link ##{link.order + 1}",
              source: slot.source, source_page: slot.page_index + 1, group: slot.group,
              original_kind: :uri, original_target: link.target, view: nil,
              output_slot: slot.slot, basis: :external, detail: 'external URI, untouched'
            )
            next
          end

          target_page, view, kind, target = resolve_link_target(slot.source, link)
          resolved_slot, basis, detail = route(slot.source, target_page, slot.group)
          label = "slot #{slot.slot} (#{slot.source} p#{slot.page_index + 1}, group #{slot.group}) link ##{link.order + 1}"
          if resolved_slot
            @link_decisions[key] = Decision.new(
              ref_type: :link, key: key, origin_label: label,
              source: slot.source, source_page: slot.page_index + 1, group: slot.group,
              original_kind: kind, original_target: target, view: view,
              output_slot: resolved_slot.slot, basis: basis, detail: detail
            )
          else
            @gaps << Gap.new(ref_type: :link, key: key, origin_label: label,
                             source: slot.source, source_page: slot.page_index + 1, group: slot.group,
                             reason: detail)
          end
        end
      end
    end

    def resolve_link_target(source, link)
      case link.kind
      when :page_object, :integer_page
        [link.target, link.view, link.kind, link.target]
      when :name
        dest = @raw_models.fetch(source).name_dests.find { |entry| entry.name == link.target }
        raise "name #{link.target.inspect} missing in #{source}" unless dest
        [dest.page_index, dest.view, :name, link.target]
      else
        raise "unroutable link kind #{link.kind}"
      end
    end

    # One outline chapter per group; each chapter follows that group's source
    # outline tree so every bookmark gets a deterministic group context.
    def plan_bookmarks
      chapters = @manifest.slots.map { |slot| [slot.group, slot.source] }.uniq
      chapters.each do |group, source|
        model = @raw_models.fetch(source)
        model.outline_roots.each { |node| walk_bookmark(node, source, group) }
      end
    end

    def walk_bookmark(node, source, group)
      key = [group, source, node.id]
      unless node.target_page.nil?
        resolved_slot, basis, detail = route(source, node.target_page, group)
        label = "group #{group} bookmark #{node.title.dup.force_encoding('UTF-8')}"
        if resolved_slot
          @bookmark_decisions[key] = Decision.new(
            ref_type: :bookmark, key: key, origin_label: label,
            source: source, source_page: node.target_page + 1, group: group,
            original_kind: :page_object, original_target: "#{source} p#{node.target_page + 1}",
            view: node.view, output_slot: resolved_slot.slot, basis: basis, detail: detail
          )
        else
          @gaps << Gap.new(ref_type: :bookmark, key: key, origin_label: label,
                           source: source, source_page: node.target_page + 1, group: group,
                           reason: detail)
        end
      end
      node.children.each { |child| walk_bookmark(child, source, group) }
    end

    def plan_work_orders
      @manifest.work_orders.each do |order|
        key = order.name
        resolved_slot, basis, detail = route(order.source, order.page_index, order.group)
        label = "work order #{order.name.dup.force_encoding('UTF-8')}"
        if resolved_slot
          @work_order_decisions[key] = Decision.new(
            ref_type: :work_order, key: key, origin_label: label,
            source: order.source, source_page: order.page_index + 1, group: order.group,
            original_kind: :external_name, original_target: order.name, view: order.view,
            output_slot: resolved_slot.slot, basis: basis, detail: detail
          )
        else
          @gaps << Gap.new(ref_type: :work_order, key: key, origin_label: label,
                           source: order.source, source_page: order.page_index + 1, group: order.group,
                           reason: detail)
        end
      end
    end
  end
end
