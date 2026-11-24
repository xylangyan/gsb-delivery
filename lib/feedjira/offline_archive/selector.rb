# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Resolves what an issue should actually publish.
    #
    # Rules applied here:
    #
    # * only entries confirmed for display are candidates,
    # * a source may have several files (the original can republish the same
    #   Atom ID in a later edition); the archived copy is the newest version at
    #   or before the issue cutoff, unless the catalog pins a version,
    # * a retraction removes exactly the entry it points at, never a sibling
    #   that happens to share a link,
    # * a retraction forwarded by an aggregator still retracts the original it
    #   names, and the original publication source is kept as the effective
    #   source,
    # * a retraction pinned to a version retracts only that version. A newer
    #   version of the same Atom ID, published later, is left alone,
    # * an unpinned retraction retracts the newest version that existed when the
    #   notice was published.
    class Selector
      Selection = Struct.new(:issue, :entries, :retractions, :unselected, keyword_init: true)

      def self.call(issue, sources, verification: {}, resources: {})
        new(issue, sources, verification: verification, resources: resources).call
      end

      def initialize(issue, sources, verification: {}, resources: {})
        @issue = issue
        @sources = sources
        @verification = stringify(verification || {})
        @resources = stringify(resources || {})
      end

      def call
        selected, unselected = partition
        remaining, applied = apply_retractions(selected)
        Selection.new(
          issue: issue,
          entries: remaining,
          retractions: applied,
          unselected: unselected
        )
      end

      private

      attr_reader :issue

      def partition
        selected = []
        unselected = []

        issue.entries.each do |spec|
          reason = skip_reason(spec)
          if reason
            unselected << { spec: spec, reason: reason }
            next
          end

          candidates = candidates_for(spec)
          if candidates.empty?
            unselected << { spec: spec, reason: :source_entry_missing }
            next
          end

          entry = pick_version(candidates, spec.version)
          if after_cutoff?(entry)
            unselected << { spec: spec, reason: :after_cutoff }
            next
          end

          selected << decorate(entry, spec)
        end

        [selected, unselected]
      end

      def skip_reason(spec)
        return :not_confirmed unless spec.confirmed
        return :excluded unless spec.include

        nil
      end

      def candidates_for(spec)
        documents = @sources.fetch(spec.source) do
          raise CatalogError, "catalog names unknown source #{spec.source.inspect}"
        end
        documents.flat_map(&:entries).select { |entry| entry.id == spec.id }
      end

      # A pinned version wins when present. When it is absent, fall back to the
      # newest version at or before the cutoff, never to a later release.
      def pick_version(candidates, version)
        pinned = version && candidates.find { |entry| entry.version == version }
        return pinned if pinned

        eligible = candidates.reject { |entry| after_cutoff?(entry) }
        (eligible.empty? ? candidates : eligible).max_by { |entry| time(entry.version) }
      end

      def after_cutoff?(entry)
        return false if issue.cutoff.nil?

        value = time(entry.version)
        !value.nil? && value > issue.cutoff
      end

      def time(version)
        return nil if version.nil? || version.to_s.empty?

        Time.parse(version.to_s).utc
      rescue ArgumentError
        nil
      end

      def decorate(entry, spec)
        {
          source: entry.source,
          id: entry.id,
          version: entry.version,
          entry: entry,
          signature: verification_for(spec) || spec.signature,
          resources: spec.resources,
          resolved_references: resolved_references(entry)
        }
      end

      # The absolute original URL each relative reference resolves to, using
      # the inherited xml:base chain. This is what a resource map is keyed on.
      def resolved_references(entry)
        entry.relative_references.to_h do |reference|
          bases = []
          bases << entry.feed_base if entry.feed_base
          bases.concat(entry.local_bases)
          [reference, UriJoin.resolve_chain(bases, reference)]
        end
      end

      def verification_for(spec)
        entries = @verification["entries"] || {}
        [
          [spec.source, spec.id, spec.version].compact.join("|"),
          "#{spec.source}|#{spec.id}"
        ].each do |key|
          return entries[key] unless entries[key].nil?
        end
        nil
      end

      # Returns [remaining, applied]. Applied retractions pair the notice with
      # the exact selected item that was removed.
      def apply_retractions(selected)
        remaining = selected.dup
        applied = []

        issue.retractions.each do |notice|
          next unless notice.applies?

          index = target_index(remaining, notice)
          next if index.nil?

          item = remaining.delete_at(index)
          applied << {
            notice: notice,
            item: item
          }
        end

        [remaining, applied.map { |pair| retraction(pair) }]
      end

      def target_index(selected, notice)
        candidates = selected.each_index.select do |index|
          item = selected[index]
          item[:id] == notice.target_id &&
            (notice.target_source.nil? || item[:source] == notice.target_source)
        end
        return nil if candidates.empty?

        if notice.target_version
          candidates.find { |index| selected[index][:version] == notice.target_version }
        else
          newest_before_notice(candidates, selected, notice)
        end
      end

      def newest_before_notice(candidates, selected, notice)
        notice_time = notice.published_at
        return candidates.max_by { |index| time(selected[index][:version]) || Time.at(0) } if notice_time.nil?

        eligible = candidates.select do |index|
          value = time(selected[index][:version])
          value.nil? || value <= notice_time
        end
        (eligible.empty? ? candidates : eligible).max_by do |index|
          time(selected[index][:version]) || Time.at(0)
        end
      end

      def retraction(pair)
        notice = pair[:notice]
        item = pair[:item]
        Retraction.new(
          issue: issue.id,
          target_source: item[:source],
          target_id: item[:id],
          target_version: item[:version],
          published_by: notice.notice_source,
          effective_source: notice.effective_source,
          published_at: notice.published_at,
          forwarded: notice.forwarded,
          reason: notice.reason,
          object: notice.object
        )
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
