# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Re-reads the emitted pages with Feedjira and checks that filing did not
    # change what the original entries said.
    #
    # The check is deliberately mechanical: it re-parses every page with the
    # same library the other team uses, then compares entry ids, extension
    # element types and enclosure targets against the selected originals. Signed
    # entries are additionally compared byte for byte against the source bytes.
    class Verifier
      Finding = Struct.new(:page, :kind, :detail, keyword_init: true) do
        def to_h
          { page: page, kind: kind, detail: detail }
        end
      end

      Result = Struct.new(:findings, keyword_init: true) do
        def ok?
          findings.empty?
        end

        def to_h
          { ok: ok?, findings: findings.map(&:to_h) }
        end
      end

      def self.call(rendered, selected:, resources: {})
        new(rendered, selected: selected, resources: resources).call
      end

      def initialize(rendered, selected:, resources: {})
        @rendered = rendered
        @selected = selected
        @resources = resources || {}
      end

      def call
        findings = []
        @rendered.each do |result|
          check_page(result, findings)
        end
        Result.new(findings: findings)
      end

      private

      def check_page(result, findings)
        feed = Feedjira.parse(result.xml)
        page = result.page

        if feed.entries.length != page.entries.length
          findings << finding(result, :entry_count, expected: page.entries.length, actual: feed.entries.length)
          return
        end

        page.entries.each_with_index do |item, index|
          parsed = feed.entries[index]
          original = item[:entry]

          check_identity(result, item, parsed, findings)
          check_extensions(result, item, original, findings)
          check_enclosure(result, item, parsed, findings)
          check_signed_bytes(result, item, original, findings)
        end
      end

      def check_identity(result, item, parsed, findings)
        return if parsed.entry_id == item[:id]

        findings << finding(result, :id_changed, expected: item[:id], actual: parsed.entry_id)
      end

      def check_extensions(result, item, original, findings)
        original_types = original.extensions.map { |extension| [extension.name, extension.namespace, extension.type] }
        node = entry_node(result.xml, index_of(result.page, item))
        actual_types = extension_types(node)
        return if actual_types == original_types

        findings << finding(result, :extension_type_changed, expected: original_types, actual: actual_types)
      end

      def entry_node(xml, index)
        document = Nokogiri::XML(xml)
        document.root.element_children.select { |child| child.name == "entry" }[index]
      end

      def extension_types(node)
        node.xpath(".//*").each_with_object([]) do |child, list|
          href = child.namespace&.href
          next if href.nil? || href == Renderer::ATOM_NAMESPACE

          list << [child.name, href, child["type"]]
        end
      end
      def check_enclosure(result, item, parsed, findings)
        targets = item[:attachment_targets]
        return if targets.nil? || targets.empty?

        # Entry bytes are unchanged, so the parser must still see every
        # original reference; the resource map points the offline reader at the
        # local file. Collect what Feedjira exposes for this entry.
        seen = descriptor_values(parsed)
        missing = item[:resolved_references].select { |_ref, absolute| targets.key?(absolute) }
                                           .keys
                                           .reject { |reference| seen.include?(reference) }
        return if missing.empty?

        findings << finding(result, :attachment_changed, expected: missing, actual: seen)
      end

      def descriptor_values(parsed)
        values = []
        values.concat(Array(parsed.links)) if parsed.respond_to?(:links)
        values << parsed.image if parsed.respond_to?(:image) && parsed.image
        values << parsed.url if parsed.respond_to?(:url) && parsed.url
        values.compact
      end

      def check_signed_bytes(result, item, original, findings)
        return unless original.signed?

        slice = entry_slice(result.xml, index_of(result.page, item))
        return if slice == original.xml

        findings << finding(result, :signed_bytes_changed, id: item[:id])
      end

      def entry_slice(xml, index)
        RawDocument.new(xml).slices[index]&.xml
      end

      def index_of(page, item)
        page.entries.index { |candidate| candidate.equal?(item) } || page.entries.index(item)
      end

      def finding(result, kind, detail)
        Finding.new(page: result.filename, kind: kind, detail: detail)
      end
    end
  end
end
