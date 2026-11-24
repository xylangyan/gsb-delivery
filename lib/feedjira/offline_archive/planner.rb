# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Groups selected entries into as few pages as the constraints allow.
    #
    # Constraints, in the order they are enforced:
    #
    # * every entry in a +must_group+ is on one page,
    # * a page holds at most +page_limit+ entries,
    # * two entries that inherit the same prefix for different namespace URIs
    #   cannot share a page, unless the prefix is rebound inside one of the
    #   entries (then the carried bytes keep their own meaning and only the other
    #   binding is declared on the page),
    # * a page carries a single +xml:base+; entries whose relative references
    #   would resolve differently under that base are split,
    # * a signed entry whose signature scope covers content that was not
    #   selected, or that requires the whole original page, is reported to the
    #   reviewer instead of being filed.
    #
    # A requested group that cannot be placed is reported together with the
    # original entry bytes and the conflicting constraints; every other page is
    # still produced.
    class Planner
      Page = Struct.new(:index, :entries, :base, :namespaces, keyword_init: true)

      Unplaced = Struct.new(:kind, :entries, :constraints, keyword_init: true)

      def self.call(selection, page_limit:)
        new(selection, page_limit: page_limit).call
      end

      def initialize(selection, page_limit:)
        @selection = selection
        @page_limit = normalize_limit(page_limit)
      end

      def call
        items = @selection.entries.map { |item| enrich(item) }
        groups, loose = resolve_groups(items)
        pages = []
        unplaced = []

        groups.each { |group| place_group(group, pages, unplaced) }
        loose.each { |item| place_single(item, pages, unplaced) }

        [finalize(pages), unplaced]
      end

      private

      def normalize_limit(limit)
        value = limit.to_i
        value.positive? ? value : 1
      end

      def enrich(item)
        entry = item[:entry]
        item.merge(
          required_base: required_base(entry),
          base_dependent: base_dependent?(entry),
          inherited_bindings: entry.inherited_bindings,
          relative_references: entry.relative_references,
          signature_notice: signature_notice(item)
        )
      end

      # True when the entry's relative references depend on an inherited base.
      # An absolute local xml:base isolates the entry from the page base, so it
      # does not depend on it.
      def base_dependent?(entry)
        return false if entry.relative_references.empty?

        first = entry.local_bases.first
        !(first && first.match?(UriJoin::SCHEME))
      end

      # The base the entry needs on its page. A nil requirement is still a
      # requirement: a page carrying an unrelated base would re-base the entry's
      # relative references.
      def required_base(entry)
        base_dependent?(entry) ? entry.feed_base : nil
      end

      def signature_notice(item)
        signature = item[:signature]
        return nil if signature.nil?

        signature = stringify(signature)
        return { kind: :signature_unverified, message: "signature was not verified" } if signature["verified"] == false

        scope = signature["scope"]
        return nil if scope.nil?

        scope_conflict(item, scope)
      end

      def scope_conflict(item, scope)
        entry = item[:entry]
        if scope["whole_document"] || scope["whole_page"] || scope["whole_source"]
          return {
            kind: :signature_whole_page,
            message: "signature covers the whole original page",
            covered: coverage(item, entry.line_range)
          }
        end

        outside = outside_ranges(scope, entry.line_range)
        paths = Array(scope["unselected_paths"]) + Array(scope["covers_unselected"])
        if outside.empty? && paths.empty?
          nil
        else
          {
            kind: :signature_unselected,
            message: "signature also covers content that was not selected",
            covered: coverage(item, entry.line_range, outside, paths)
          }
        end
      end

      def outside_ranges(scope, own_range)
        ranges = Array(scope["line_ranges"])
        ranges.reject do |range|
          pair = Array(range)
          pair.length == 2 && pair[0].to_i >= own_range[0] && pair[1].to_i <= own_range[1]
        end
      end

      def coverage(item, line_range, outside = [], paths = [])
        entry = item[:entry]
        {
          source: item[:source],
          id: item[:id],
          version: item[:version],
          file: entry.file,
          entry_lines: line_range,
          entry_bytes: entry.byte_range,
          uncovered_lines: outside,
          unselected_paths: paths
        }
      end

      def resolve_groups(items)
        lookup = {}
        items.each { |item| lookup[key(item)] = item }

        groups = []
        claimed = {}

        @selection.issue.must_group.each do |members|
          resolved = members.map { |ref| lookup_item(lookup, ref) }
          missing = members.reject { |ref| lookup_item(lookup, ref) }
          group = { members: resolved.compact, missing: missing }
          groups << group
          group[:members].each { |item| claimed[key(item)] = true }
        end

        [groups, items.reject { |item| claimed[key(item)] }]
      end

      def key(item)
        [item[:source], item[:id], item[:version]]
      end

      # Catalog references are "source:id". The selected item is keyed by
      # source, id and version, so match on the first two elements.
      def normalize_ref(ref)
        case ref
        when Array then ref
        when Hash then [ref["source"], ref["id"]]
        else ref.to_s.split(":", 2)
        end
      end

      def lookup_item(lookup, ref)
        source, id = normalize_ref(ref)
        key = lookup.keys.find { |candidate| candidate[0] == source && candidate[1] == id }
        key && lookup[key]
      end

      def place_group(group, pages, unplaced)
        members = group[:members]
        if members.empty?
          unplaced << Unplaced.new(
            kind: :unknown_reference,
            entries: [],
            constraints: { rule: :must_group, missing: group[:missing] }
          )
          return
        end

        if group[:missing].any?
          unplaced << Unplaced.new(
            kind: :unknown_reference,
            entries: members,
            constraints: { rule: :must_group, missing: group[:missing] }
          )
          return
        end

        if members.length > @page_limit
          # The requested grouping cannot be honoured. Report it with the
          # original entries and the conflicting limit; the group is not split
          # across pages, because that would break the same-page requirement.
          unplaced << unplaced_group(members, :page_limit,
                                     rule: :must_group, page_limit: @page_limit,
                                     group_size: members.length)
          return
        end

        if (conflict = conflict_for(members))
          unplaced << unplaced_group(members, conflict[:kind], conflict[:details])
          return
        end

        target = pages.find do |page|
          page[:entries].length + members.length <= @page_limit && conflict_for(page[:entries] + members).nil?
        end

        if target
          target[:entries].concat(members)
          target[:base] = page_base(target[:entries])
        else
          pages << new_page(members)
        end
      end

      def place_single(item, pages, unplaced)
        if (notice = item[:signature_notice])
          unplaced << Unplaced.new(kind: notice[:kind], entries: [item], constraints: notice)
          return
        end

        target = pages.find do |page|
          page[:entries].length < @page_limit && conflict_for(page[:entries] + [item]).nil?
        end

        if target
          target[:entries] << item
          target[:base] = page_base(target[:entries])
        else
          pages << new_page([item])
        end
      end

      def unplaced_group(members, kind, details)
        Unplaced.new(
          kind: kind,
          entries: members,
          constraints: details.merge(rule: :must_group, group_size: members.length)
        )
      end

      # Returns nil when the members can share a page.
      def conflict_for(members)
        namespace_conflict(members) || base_conflict(members)
      end

      def namespace_conflict(members)
        by_prefix = Hash.new { |hash, prefix| hash[prefix] = [] }
        members.each do |item|
          item[:inherited_bindings].each do |binding|
            by_prefix[binding.prefix] << binding.href
          end
        end

        by_prefix.each do |prefix, hrefs|
          unique = hrefs.uniq
          next if unique.length <= 1

          return {
            kind: :namespace,
            details: { rule: :prefix_binding, prefix: prefix, hrefs: unique }
          }
        end

        nil
      end

      # A page carries one base. Every base dependent entry must therefore agree
      # on that base, and "no base" is itself a requirement: co-locating such an
      # entry with a page that has a base would re-base its references.
      def base_conflict(members)
        dependent = members.select { |item| item[:base_dependent] }
        bases = dependent.map { |item| item[:required_base] }.uniq
        return nil if bases.length <= 1

        {
          kind: :base,
          details: { rule: :single_page_base, bases: bases.map { |base| base || :none } }
        }
      end

      def new_page(members)
        { entries: members.dup, base: page_base(members) }
      end

      def page_base(entries)
        entries.select { |entry| entry[:base_dependent] }.map { |entry| entry[:required_base] }.compact.first
      end

      def finalize(pages)
        pages.each_with_index.map do |page, index|
          Page.new(
            index: index,
            entries: page[:entries],
            base: page[:base],
            namespaces: declarations(page[:entries])
          )
        end
      end

      # Page-level declarations needed for inherited bindings. Entry-local
      # rebindings are already part of the carried bytes and are skipped.
      def declarations(entries)
        result = {}
        entries.each do |item|
          item[:inherited_bindings].each do |binding|
            result[binding.prefix] ||= binding.href
          end
        end
        result
      end

      def stringify(value)
        case value
        when Hash
          value.each_with_object({}) { |(key, val), memo| memo[key.to_s] = stringify(val) }
        when Array
          value.map { |item| stringify(item) }
        else
          value
        end
      end
    end
  end
end
