# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Normalises the caller supplied catalog.
    #
    # The catalog is data only: it names the source files, says which entry
    # versions were confirmed for display, which entries a page must keep
    # together, and which retraction notices exist. It never contains entry
    # bodies, so moving the source directory cannot change what is filed.
    #
    # Shape (JSON friendly; every key shown is optional unless noted):
    #
    #   {
    #     "issues": [
    #       {
    #         "id": "issue-1",                     # required
    #         "cutoff": "2026-11-21T00:00:00Z",     # required
    #         "page_limit": 4,
    #         "sources": [
    #           { "name": "alpha", "file": "alpha-1120.atom",
    #             "uri": "https://alpha.example/2026/11/20.atom" }
    #         ],
    #         "entries": [
    #           { "source": "alpha", "id": "tag:alpha,2026:e1",
    #             "version": "2026-11-20T09:00:00Z", "confirmed": true,
    #             "signature": { "verified": true } }
    #         ],
    #         "must_group": [["alpha:tag:alpha,2026:e1", "alpha:tag:alpha,2026:e2"]],
    #         "retractions": [
    #           { "notice_source": "digest", "original_source": "alpha",
    #             "target_source": "alpha", "target_id": "tag:alpha,2026:e1",
    #             "target_version": "2026-11-20T09:00:00Z",
    #             "published_at": "2026-11-20T12:00:00Z",
    #             "effective": true, "forwarded": true, "reason": "..." }
    #         ]
    #       }
    #     ]
    #   }
    #
    # Signature verification and the original-resource to local-file map are
    # supplied separately by the caller so they can come from an external
    # verifier and an offline resource directory.
    class Catalog
      SourceSpec = Struct.new(:name, :file, :uri, keyword_init: true)

      EntrySpec = Struct.new(
        :source, :id, :version, :confirmed, :include, :signature, :resources, keyword_init: true
      )

      RetractionSpec = Struct.new(
        :notice_source, :original_source, :target_source, :target_id, :target_version,
        :published_at, :effective, :forwarded, :reason, :object, keyword_init: true
      ) do
        def applies?
          effective != false
        end

        def effective_source
          original_source || notice_source || target_source
        end
      end

      Issue = Struct.new(
        :id, :cutoff, :page_limit, :sources, :entries, :must_group, :retractions, keyword_init: true
      )

      attr_reader :issues, :verification, :resources

      def self.load(data, verification: {}, resources: {})
        new(data, verification: verification, resources: resources)
      end

      def initialize(data, verification: {}, resources: {})
        @data = stringify(data)
        @verification = stringify(verification || {})
        @resources = stringify(resources || {})
        @issues = Array(@data.fetch("issues", [])).map { |issue| build_issue(issue) }
      end

      def issue(id)
        issues.find { |candidate| candidate.id == id }
      end

      private

      def build_issue(issue)
        Issue.new(
          id: require_field(issue, "id"),
          cutoff: parse_time(require_field(issue, "cutoff")),
          page_limit: Integer(issue.fetch("page_limit", 0)),
          sources: Array(issue.fetch("sources", [])).map { |source| build_source(source) },
          entries: Array(issue.fetch("entries", [])).map { |entry| build_entry(entry) },
          must_group: Array(issue.fetch("must_group", [])).map { |group| Array(group) },
          retractions: Array(issue.fetch("retractions", [])).map { |notice| build_retraction(notice) }
        )
      end

      def build_source(source)
        SourceSpec.new(
          name: require_field(source, "name"),
          file: require_field(source, "file"),
          uri: presence(source["uri"])
        )
      end

      def build_entry(entry)
        EntrySpec.new(
          source: require_field(entry, "source"),
          id: require_field(entry, "id"),
          version: presence(entry["version"]),
          confirmed: entry.fetch("confirmed", true) != false,
          include: entry.fetch("include", true) != false,
          signature: entry["signature"],
          resources: entry["resources"] || {}
        )
      end

      def build_retraction(notice)
        published_at = presence(notice["published_at"])
        RetractionSpec.new(
          notice_source: presence(notice["notice_source"]),
          original_source: presence(notice["original_source"]),
          target_source: presence(notice["target_source"]),
          target_id: require_field(notice, "target_id"),
          target_version: presence(notice["target_version"]),
          published_at: published_at && parse_time(published_at),
          effective: notice.fetch("effective", true) != false,
          forwarded: notice.fetch("forwarded", false) == true,
          reason: presence(notice["reason"]),
          object: presence(notice["object"])
        )
      end

      def require_field(hash, key)
        value = hash[key]
        raise CatalogError, "catalog is missing #{key.inspect}" if value.nil? || value.to_s.empty?

        value
      end

      def presence(value)
        return nil if value.nil?

        string = value.to_s
        string.empty? ? nil : string
      end

      def parse_time(value)
        return value.utc if value.is_a?(Time)
        return nil if value.nil? || value.to_s.empty?

        Time.parse(value.to_s).utc
      rescue ArgumentError
        raise CatalogError, "catalog has an invalid timestamp #{value.inspect}"
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
