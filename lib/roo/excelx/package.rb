# frozen_string_literal: true

require 'zip'
require 'nokogiri'
require 'uri'
require 'digest'

module Roo
  class Excelx
    # Read-only view of an OOXML package that resolves parts through the
    # relationship graph instead of guessing from file names or positions.
    class Package
      RELATIONSHIPS_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
      OFFICE_DOCUMENT_RELATIONSHIP = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
      STRICT_OFFICE_DOCUMENT_RELATIONSHIP = 'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument'
      DOCUMENT_RELATIONSHIP_NAMESPACES = [
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
        'http://purl.oclc.org/ooxml/officeDocument/relationships'
      ].freeze

      Relationship = Struct.new(
        :id, :type, :target, :target_mode, :owner, :resolved_target,
        keyword_init: true
      ) do
        def external?
          target_mode.to_s.casecmp('external').zero?
        end

        def type_name
          type.to_s.split('/').last
        end

        def type_family
          type.to_s[%r{/relationships/([^/]+)$}, 1] || type_name
        end

        def to_h
          {
            id: id,
            type: type,
            type_family: type_family,
            target: target,
            target_mode: target_mode,
            owner: owner,
            resolved_target: resolved_target,
            external: external?
          }
        end
      end

      Sheet = Struct.new(
        :name, :sheet_id, :state, :relationship_id, :relationship, :part, :kind,
        keyword_init: true
      ) do
        def hidden?
          state.to_s.casecmp('hidden').zero? || state.to_s.casecmp('veryHidden').zero?
        end
      end

      attr_reader :path

      def initialize(path_or_io)
        @path = path_or_io.respond_to?(:path) ? path_or_io.path : path_or_io
        @path = nil unless @path.is_a?(String)
        if path_or_io.respond_to?(:entries)
          @zip = path_or_io
          @owns_zip = false
        else
          @zip = Zip::File.open(@path)
          @owns_zip = true
        end
        @entries = {}
        @case_index = {}
        @zip.entries.each do |entry|
          next if entry.directory?

          name = normalize_name(entry.name)
          @entries[name] = entry
          @case_index[name.downcase] ||= name
        end
      end

      def close
        @zip&.close if @owns_zip && @zip.respond_to?(:close)
        @zip = nil
      end

      def names
        @entries.keys
      end

      def entry(name)
        normalized = normalize_name(name)
        @entries[normalized] || @entries[@case_index[normalized.downcase]]
      end

      def exist?(name)
        !entry(name).nil?
      end

      def read(name)
        found = entry(name)
        return nil unless found

        found.get_input_stream.read
      end

      def xml(name)
        content = read(name)
        return nil unless content

        Nokogiri::XML(content)
      end

      def content_types
        xml('[Content_Types].xml')
      end

      def relationships(owner)
        @relationships_cache ||= {}
        return @relationships_cache[owner] if @relationships_cache.key?(owner)

        @relationships_cache[owner] = extract_relationships(owner)
      end

      def extract_relationships(owner)
        rels_part = relationships_part_name(owner)
        return [] unless rels_part && exist?(rels_part)

        document = xml(rels_part)
        return [] unless document

        document.xpath('/rel:Relationships/rel:Relationship', 'rel' => RELATIONSHIPS_NS).map do |node|
          target = node['Target'].to_s
          Relationship.new(
            id: node['Id'],
            type: node['Type'],
            target: target,
            target_mode: node['TargetMode'],
            owner: owner,
            resolved_target: node['TargetMode'].to_s.casecmp('external').zero? ? target : resolve_target(owner, target)
          )
        end
      end

      def relationship(owner, id)
        relationships(owner).find { |relationship| relationship.id == id }
      end

      def office_document_part
        root_relationships = relationships(nil)
        relationship = root_relationships.find do |candidate|
          [OFFICE_DOCUMENT_RELATIONSHIP, STRICT_OFFICE_DOCUMENT_RELATIONSHIP].include?(candidate.type)
        end
        relationship ||= root_relationships.find { |candidate| candidate.type_name == 'officeDocument' }
        relationship&.resolved_target || 'xl/workbook.xml'
      end

      def workbook_part
        office_document_part
      end

      def workbook_xml
        document = workbook_doc&.dup
        document&.remove_namespaces!
      end

      def workbook_doc
        @workbook_doc ||= xml(workbook_part)
      end

      def relationship_id_for(node)
        DOCUMENT_RELATIONSHIP_NAMESPACES.each do |namespace|
          value = node.attribute_with_ns('id', namespace)&.value
          return value if value
        end

        node['id']
      end

      def sheets
        document = workbook_xml
        return [] unless document

        document.xpath('//sheet').map do |node|
          relationship_id = relationship_id_for(node)
          relationship = relationship(workbook_part, relationship_id)
          Sheet.new(
            name: node['name'],
            sheet_id: node['sheetId'],
            state: node['state'],
            relationship_id: relationship_id,
            relationship: relationship,
            part: relationship&.resolved_target,
            kind: relationship&.type_name
          )
        end
      end

      def external_references
        document = workbook_doc
        return [] unless document

        nodes = document.xpath(
          '//*[local-name()="externalReferences"]/*[local-name()="externalReference"]'
        )
        nodes.each_with_index.map do |node, index|
          relationship_id = relationship_id_for(node)
          relationship = relationship(workbook_part, relationship_id)
          {
            index: index + 1,
            relationship_id: relationship_id,
            relationship: relationship,
            part: relationship&.resolved_target
          }
        end
      end

      def content_type_map
        @content_type_map ||= begin
          map = {}
          document = content_types
          if document
            document.xpath('//*[local-name()="Override"]').each do |node|
              name = normalize_name(node['PartName'].to_s.sub(%r{\A/}, ''))
              map[name] = node['ContentType']
            end
          end
          map
        end
      end

      def default_content_type_map
        @default_content_type_map ||= begin
          map = {}
          document = content_types
          if document
            document.xpath('//*[local-name()="Default"]').each do |node|
              map[node['Extension'].to_s.downcase] = node['ContentType']
            end
          end
          map
        end
      end

      def content_type_for(part_name)
        normalized = normalize_name(part_name)
        content_type_map[normalized] ||
          default_content_type_map[File.extname(normalized).delete('.').downcase]
      end

      def digest_for(part_name)
        content = read(part_name)
        content && Digest::SHA256.hexdigest(content)
      end

      def relationships_part_name(owner)
        return '_rels/.rels' if owner.nil? || owner.to_s.empty?

        directory = File.dirname(owner)
        basename = File.basename(owner)
        directory == '.' ? "_rels/#{basename}.rels" : "#{directory}/_rels/#{basename}.rels"
      end

      def resolve_target(owner, target)
        target = target.to_s
        return nil if target.empty?

        target = target.split('#', 2).first || ''
        target = URI::DEFAULT_PARSER.unescape(target) if target.include?('%')

        if target.start_with?('/')
          normalize_name(target)
        else
          base = owner.nil? || owner.to_s.empty? ? '' : File.dirname(owner)
          normalize_name(base.empty? ? target : "#{base}/#{target}")
        end
      end

      def normalize_name(name)
        segments = name.to_s.tr('\\', '/').split('/')
        resolved = []
        segments.each do |segment|
          next if segment.empty? || segment == '.'

          if segment == '..'
            resolved.pop
          else
            resolved << segment
          end
        end
        resolved.join('/')
      end

      def self.open(path_or_io)
        package = new(path_or_io)
        return package unless block_given?

        begin
          yield package
        ensure
          package.close
        end
      end
    end
  end
end
