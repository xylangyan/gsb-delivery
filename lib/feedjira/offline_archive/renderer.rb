# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Writes plain Atom pages.
    #
    # Entry bodies are copied from the original bytes without any XML round
    # trip; the renderer never edits them, because a signed entry's signature
    # covers those exact bytes. What the renderer adds is limited to:
    #
    # * the Atom feed wrapper, including the page's +xml:base+,
    # * namespace declarations the entries inherited from their source,
    # * a per-page attachment map: relative references resolved against the
    #   original base and paired with the local offline file, when the caller
    #   supplied a mapping.
    #
    # Because the entry bytes are untouched, a relative enclosure keeps its
    # original reference. The page +xml:base+ and the attachment map together
    # tell the offline reader where the file actually is.
    class Renderer
      ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
      XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"

      PageResult = Struct.new(:page, :issue, :xml, :path, :filename, :attachments, keyword_init: true)

      def self.call(pages, issue:, resources: {}, verification: {})
        new(issue: issue, resources: resources, verification: verification).render(pages)
      end

      def initialize(issue:, resources: {}, verification: {})
        @issue = issue
        @resources = stringify(resources || {})
        @verification = stringify(verification || {})
      end

      def render(pages)
        pages.map do |page|
          body = build_feed(page)
          filename = page_filename(page)
          PageResult.new(
            page: page,
            issue: issue.id,
            xml: body,
            path: filename,
            filename: filename,
            attachments: attachments(page)
          )
        end
      end

      private

      attr_reader :issue

      def page_filename(page)
        "issue-#{slug(issue.id)}-#{format('%02d', page.index + 1)}.atom"
      end

      def slug(value)
        value.to_s.downcase.gsub(/[^a-z0-9]+/, "-").gsub(/\A-+|-+\z/, "")
      end

      def build_feed(page)
        lines = []
        lines << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        lines << "<feed#{feed_attributes(page)}>"
        lines << "  <title>#{escape(issue.id)} archive, page #{page.index + 1}</title>"
        lines << "  <id>#{escape("tag:offline-archive,#{issue.id},page-#{page.index + 1}")}</id>"
        lines << "  <updated>#{issue.cutoff.utc.iso8601}</updated>"
        page.entries.each { |item| lines << utf8(item[:entry].xml) }
        lines << "</feed>"
        lines.join("\n") + "\n"
      end

      def feed_attributes(page)
        attributes = +" xmlns=\"#{ATOM_NAMESPACE}\""
        attributes << " xml:base=\"#{escape(page.base)}\"" if page.base
        page.namespaces.each do |prefix, href|
          next if prefix.nil?

          attributes << " xmlns:#{prefix}=\"#{escape(href)}\""
        end
        attributes
      end

      def utf8(xml)
        xml.dup.force_encoding(Encoding::UTF_8)
      end

      # Relative references that the caller mapped to a local offline file,
      # keyed by the reference exactly as it appears in the entry bytes. Also
      # include the absolute resolution so callers can map either form.
      def attachments(page)
        result = {}
        page.entries.each do |item|
          entry = item[:entry]
          entry.relative_references.each do |reference|
            local = lookup_resource(item, entry, reference)
            next if local.nil?

            result[reference] ||= local
            result[resolve_reference(entry, reference)] ||= local
          end
        end
        result
      end

      def lookup_resource(item, entry, reference)
        absolute = resolve_reference(entry, reference)
        mapping = item[:attachment_targets]
        mapping = {} unless mapping.respond_to?(:[])
        @resources[reference] || @resources[absolute] || mapping[reference] || mapping[absolute]
      end

      def resolve_reference(entry, reference)
        bases = []
        bases << entry.feed_base if entry.feed_base
        bases.concat(entry.local_bases)
        UriJoin.resolve_chain(bases, reference)
      end

      def escape(value)
        value.to_s
             .gsub("&", "&amp;")
             .gsub("<", "&lt;")
             .gsub(">", "&gt;")
             .gsub("\"", "&quot;")
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
