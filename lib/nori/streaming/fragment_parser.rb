require "nori"

class Nori
  module Streaming
    # Parses one record's raw bytes with the exact options that were in effect
    # when that record started.
    #
    # A record is cut out of a larger document, so declarations it inherited
    # from ancestors are no longer present in its bytes. The fragment is
    # therefore wrapped in a synthetic element that carries the in-scope
    # namespace declarations and the inherited xml:space value. The wrapper is
    # only scaffolding: the caller receives the value of the record element,
    # not the wrapper, and the record's own attributes are unchanged.
    class FragmentParser
      WRAPPER = "__nori_stream_record__".freeze

      # The option keys Nori#initialize understands. Streaming's own options
      # never reach Nori's validation.
      NORI_KEYS = [
        :strip_namespaces, :delete_namespace_attributes, :convert_tags_to,
        :convert_attributes_to, :empty_tag_value, :consistent_empty_tags,
        :advanced_typecasting, :convert_dashes_to_underscores, :scrub_xml,
        :standards, :serializable, :parser, :business_id
      ].freeze

      def initialize(options)
        @options = options
      end

      def parse(raw, context = {}, record_name = nil, metadata = {})
        options = effective_options(metadata)
        document = build_document(raw, context, metadata[:prolog])
        result = Nori.new(neutral_options(nori_options(options))).parse(document)
        extract(result, record_name, options)
      end

      # The key Nori emits for a raw element name under +options+.
      def self.record_key(name, options)
        Nori.hash_key(name.to_s, options)
      end

      private

      def effective_options(metadata)
        return @options unless metadata[:business_id]
        @options.merge(:business_id => metadata[:business_id])
      end

      def nori_options(options)
        options.select { |key, _| NORI_KEYS.include?(key) }
      end

      # The synthetic wrapper is internal scaffolding; a caller's tag
      # converter must never see it. The wrapper still has to go through Nori
      # for the outer parse, so shield exactly that one name.
      def neutral_options(options)
        converter = options[:convert_tags_to]
        return options unless converter.respond_to?(:call)

        options.merge(
          :convert_tags_to => lambda do |tag, business_id = nil|
            if tag.to_s == WRAPPER
              WRAPPER
            else
              context = options.merge(:business_id => business_id || options[:business_id])
              Nori.call_tag_converter(converter, tag, context)
            end
          end
        )
      end

      def build_document(raw, context, prolog = nil)
        wrapper = String.new(encoding: Encoding::BINARY)
        wrapper << prolog.b if prolog && !prolog.empty?
        wrapper << "<#{WRAPPER}"
        wrapper << namespace_declarations(context[:namespaces] || {})
        wrapper << " xml:space=\"preserve\"" if context[:xml_space].to_s == "preserve"
        wrapper << ">"
        wrapper << raw.b
        wrapper << "</#{WRAPPER}>"
        wrapper.force_encoding(Encoding::UTF_8)
      end

      def namespace_declarations(namespaces)
        declarations = String.new(encoding: Encoding::BINARY)
        namespaces.each do |prefix, uri|
          prefix = prefix.to_s
          next if prefix == "xml"
          name = prefix.empty? ? "xmlns" : "xmlns:#{prefix}"
          declarations << " #{name}=\"#{escape(uri)}\""
        end
        declarations
      end

      def extract(result, record_name, options)
        wrapped = result.is_a?(Hash) ? result[WRAPPER] : nil
        return nil unless wrapped.is_a?(Hash)
        return wrapped unless record_name

        key = self.class.record_key(record_name, options)
        return wrapped[key] if wrapped.key?(key)

        # A converter can reshape the record key beyond recognition. The
        # wrapper has exactly one child, so the only non-attribute key is the
        # record when the direct lookup misses.
        fallbacks = wrapped.keys.reject { |candidate| candidate.to_s.start_with?("@") }
        fallbacks.length == 1 ? wrapped[fallbacks.first] : nil
      end

      def escape(value)
        value.to_s.gsub("&", "&amp;").gsub('"', "&quot;").gsub("<", "&lt;").gsub(">", "&gt;")
      end
    end
  end
end
