# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # One original Atom document, i.e. one file published by one source.
    #
    # The document is parsed with Nokogiri for namespace and base analysis, but
    # every entry body is sliced out of the original bytes. Sources are
    # identified by name, never by a link they share: a reprint can carry the
    # same URL and is still a different publication.
    class SourceDocument
      XML_BASE = "http://www.w3.org/XML/1998/namespace"
      ATOM_NAMESPACES = [
        "http://www.w3.org/2005/Atom",
        "https://www.w3.org/2005/Atom",
        "http://purl.org/atom/ns#"
      ].freeze
      LOCAL_HREF_ATTRIBUTES = %w[href src url poster data].freeze

      # A namespace binding an entry relies on. +local+ bindings are declared on
      # the entry element or below it, so they travel with the carried bytes.
      Namespace = Struct.new(:prefix, :href, :local, keyword_init: true) do
        def to_h
          { prefix: prefix, href: href, local: local }
        end
      end

      # An extension element, keyed by expanded name so an extension +content+
      # never collides with Atom's own +content+.
      Extension = Struct.new(:name, :namespace, :type, keyword_init: true) do
        def to_h
          { name: name, namespace: namespace, type: type }
        end
      end

      Entry = Struct.new(
        :source, :file, :index, :id, :version, :xml, :digest,
        :namespaces, :feed_base, :local_bases, :relative_references,
        :extensions, :line_range, :byte_range, keyword_init: true
      ) do
        def signed?
          !digest.nil? && !digest.empty?
        end

        def inherited_bindings
          namespaces.reject(&:local)
        end
      end

      attr_reader :name, :path, :uri, :bytes, :document, :entries, :feed_base

      def initialize(name, path, uri: nil)
        @name = name
        @path = path
        @uri = uri
        @bytes = File.binread(path)
        @document = Nokogiri::XML(@bytes)
        @feed_base = attribute_ns(@document.root, "base", XML_BASE) || uri
        @entries = build_entries
      end

      def all_entry_ids
        entries.map(&:id)
      end

      def entry_by_id(id)
        entries.find { |entry| entry.id == id }
      end

      private

      def build_entries
        raw = RawDocument.new(@bytes)
        nodes = direct_entry_nodes
        if nodes.length != raw.slices.length
          raise SlicingError, "entry count mismatch in #{name.inspect}: " \
                              "#{nodes.length} parsed vs #{raw.slices.length} sliced"
        end

        nodes.each_with_index.map do |node, index|
          build_entry(node, raw.slice(index), index)
        end
      end

      def build_entry(node, slice, index)
        Entry.new(
          source: name,
          file: path,
          index: index,
          id: text_of(node, "id"),
          version: version_of(node),
          xml: slice.xml,
          digest: nil,
          namespaces: namespace_bindings(node),
          feed_base: feed_base,
          local_bases: local_bases(node),
          relative_references: relative_references(node),
          extensions: extensions(node),
          line_range: line_range(slice),
          byte_range: [slice.start_byte, slice.end_byte]
        )
      end

      def direct_entry_nodes
        document.root.element_children.select { |child| local(child.name) == "entry" }
      end

      def namespace_bindings(node)
        used = {}
        node.traverse do |child|
          next unless child.element?

          record_namespace(used, child.namespace)
          child.attribute_nodes.each { |attribute| record_namespace(used, attribute.namespace) }
        end

        used.keys.map do |prefix, href|
          Namespace.new(prefix: prefix, href: href, local: declared_within?(node, prefix, href))
        end.reject { |binding| binding.href == XML_BASE }
      end

      def record_namespace(used, namespace)
        return if namespace.nil?

        used[[namespace.prefix, namespace.href]] = true
      end

      # A binding is local when the entry subtree itself declares it. A binding
      # inherited from the document element is not local: those bytes are not
      # carried, so the page has to declare the prefix again.
      def declared_within?(entry, prefix, href)
        entry.traverse do |node|
          return true if node.element? && binding_declared?(node, prefix, href)
        end
        false
      end

      def binding_declared?(node, prefix, href)
        node.namespace_definitions.any? do |definition|
          definition.prefix == prefix && definition.href == href
        end
      end

      # xml:base values declared on the entry or its descendants, outermost
      # first. The document element's own xml:base is reported as +feed_base+.
      def local_bases(node)
        values = []
        current = node
        until local(current.name) == "feed"
          value = attribute_ns(current, "base", XML_BASE)
          values.unshift(UriJoin.normalise(value)) unless value.nil?
          current = current.parent
        end
        values
      end

      def relative_references(node)
        references = []
        node.traverse do |child|
          next unless child.element?

          child.attribute_nodes.each do |attribute|
            value = attribute.value.to_s
            references << value if local_href?(attribute) && relative?(value)
          end
        end
        references.uniq
      end

      def local_href?(attribute)
        LOCAL_HREF_ATTRIBUTES.include?(local(attribute.name))
      end

      def extensions(node)
        list = []
        node.traverse do |child|
          next unless child.element?

          href = child.namespace&.href
          next if href.nil? || ATOM_NAMESPACES.include?(href)

          list << Extension.new(name: local(child.name), namespace: href, type: child["type"])
        end
        list
      end

      def version_of(node)
        %w[updated modified published issued created].each do |name|
          value = text_of(node, name)
          return value unless value.nil? || value.empty?
        end
        nil
      end

      def line_range(slice)
        start_line = line_at(slice.start_byte)
        end_line = line_at([slice.end_byte - 1, slice.start_byte].max)
        [start_line, end_line]
      end

      def line_at(byte)
        @bytes.byteslice(0, byte).count("\n") + 1
      end

      def attribute_ns(node, name, href)
        attribute = node.attribute_nodes.find do |candidate|
          candidate.name == name && candidate.namespace&.href == href
        end
        attribute&.value
      end

      def text_of(node, name)
        child = node.element_children.find { |element| local(element.name) == name }
        child&.text&.strip
      end

      def local(name)
        name.to_s.split(":").last
      end

      def relative?(value)
        return false if UriJoin.blank?(value)
        return false if value.start_with?("#")
        return false if value.match?(UriJoin::SCHEME)

        true
      end
    end
  end
end
