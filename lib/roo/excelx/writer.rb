# frozen_string_literal: true

require 'nokogiri'
require 'zip'
require 'fileutils'
require 'set'

module Roo
  class Excelx
    # Writes a plan produced by Roo::Excelx::Exporter. Only parts listed in the
    # plan are emitted. Workbook metadata and relationship files are rewritten so
    # every emitted reference resolves inside the copy, except relationships that
    # were originally marked TargetMode="External".
    class Writer
      CONTENT_TYPES_PART = '[Content_Types].xml'
      ROOT_RELS_PART = '_rels/.rels'

      attr_reader :package, :plan

      def initialize(package, plan, prune_shared_strings: true)
        @package = package
        @plan = plan
        @prune_shared_strings = prune_shared_strings
      end

      def write(destination)
        destination = destination.to_s
        FileUtils.mkdir_p(File.dirname(destination))

        included = included_parts
        shared_strings = SharedStringsRewrite.new(package, plan, enabled: @prune_shared_strings)

        contents = {}
        included.each do |part|
          content = prepared_content(part[:part], shared_strings)
          next if content.nil?

          contents[part[:part]] = content
        end

        contents.delete(plan[:workbook_part])
        contents.delete(plan[:workbook_relationships_part])
        contents[CONTENT_TYPES_PART] = content_types_xml(included)
        contents[ROOT_RELS_PART] = root_relationships_xml(included)
        contents[plan[:workbook_part]] = workbook_xml
        contents[plan[:workbook_relationships_part]] = workbook_relationships_xml(included)

        verify_references!(contents, included)
        write_zip(destination, contents)

        {
          destination: destination,
          parts: contents.keys.sort,
          shared_strings: shared_strings.summary,
          external_relationships: external_relationships(included)
        }
      end

      private

      def included_parts
        @included_parts ||= begin
          seen = Set.new
          parts = []

          entries = (plan[:root_parts] || []) + plan[:workbook_parts] +
                    plan[:sheets].flat_map { |sheet| sheet[:parts] }

          entries.each do |part|
            name = part[:part]
            next unless name
            next if part[:external]
            next unless package.exist?(name)
            next if seen.include?(name)

            seen << name
            parts << part.merge(external_relationships: external_relationships_for(name))
          end

          parts
        end
      end

      def external_relationships_for(part_name)
        package.relationships(part_name).select(&:external?).map(&:to_h)
      end

      def external_relationships(included)
        included.flat_map { |part| part[:external_relationships] || [] }
                .uniq { |relationship| [relationship[:owner], relationship[:id], relationship[:target]] }
      end

      def prepared_content(part_name, shared_strings)
        if shared_strings.part_name == part_name
          shared_strings.content
        elsif shared_strings.sheet?(part_name)
          shared_strings.rewrite_sheet(package.read(part_name))
        else
          package.read(part_name)
        end
      end

      def workbook_xml
        document = package.xml(plan[:workbook_part])
        return nil unless document

        selected_relationship_ids = plan[:sheets].map { |sheet| sheet[:relationship_id] }
        excluded_sheet_names = plan[:excluded_sheets]

        document.xpath('//*[local-name()="sheets"]/*[local-name()="sheet"]').each do |node|
          relationship_id = package.relationship_id_for(node)
          node.remove unless selected_relationship_ids.include?(relationship_id)
        end

        document.xpath('//*[local-name()="pivotCaches"]').each(&:remove)

        document.xpath('//*[local-name()="definedNames"]/*[local-name()="definedName"]').each do |node|
          node.remove if references_excluded_sheet?(node.text, excluded_sheet_names)
        end
        document.xpath('//*[local-name()="definedNames"]').each(&:remove) if document.xpath('//*[local-name()="definedNames"]/*[local-name()="definedName"]').empty?

        document.xpath('//*[local-name()="workbookView"]').each do |node|
          node['activeTab'] = '0' if node['activeTab']
        end

        document.to_xml
      end

      def references_excluded_sheet?(formula, excluded_sheet_names)
        excluded_sheet_names.any? do |name|
          escaped = name.to_s.gsub("'", "''")
          formula.include?("#{name}!") || formula.include?("'#{escaped}'!")
        end
      end

      def workbook_relationships_xml(included)
        document = package.xml(plan[:workbook_relationships_part])
        return nil unless document

        kept_targets = included.map { |part| part[:part] }.to_set
        selected_relationship_ids = plan[:sheets].map { |sheet| sheet[:relationship_id] }.to_set

        document.xpath('//*[local-name()="Relationship"]').each do |node|
          relationship_id = node['Id']
          external = node['TargetMode'].to_s.casecmp('external').zero?
          target = external ? node['Target'] : package.resolve_target(plan[:workbook_part], node['Target'])

          keep = external || selected_relationship_ids.include?(relationship_id) || kept_targets.include?(target)
          node.remove unless keep
        end

        document.to_xml
      end

      def root_relationships_xml(included)
        source = package.xml(ROOT_RELS_PART)
        kept_targets = included.map { |part| part[:part] }.to_set
        kept_targets << plan[:workbook_part]

        relationships = []
        if source
          source.xpath('//*[local-name()="Relationship"]').each do |node|
            external = node['TargetMode'].to_s.casecmp('external').zero?
            target = external ? node['Target'] : package.resolve_target(nil, node['Target'])
            next unless external || kept_targets.include?(target)

            relationships << node
          end
        end

        unless relationships.any? { |node| node['Type'].to_s.end_with?('/officeDocument') }
          relationships.unshift(nil)
        end

        body = relationships.map do |node|
          if node.nil?
            %(  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="#{xml_escape(plan[:workbook_part])}"/>)
          else
            %(  <Relationship Id="#{xml_escape(node['Id'])}" Type="#{xml_escape(node['Type'])}" Target="#{xml_escape(node['Target'])}"#{node['TargetMode'] ? %( TargetMode="#{xml_escape(node['TargetMode'])}") : ''}/>)
          end
        end

        <<~XML
          <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          #{body.join("\n")}
          </Relationships>
        XML
      end

      def content_types_xml(included)
        overrides = {}
        defaults = package.default_content_type_map.dup
        defaults['rels'] ||= 'application/vnd.openxmlformats-package.relationships+xml'
        defaults['xml'] ||= 'application/xml'

        included.each do |part|
          part_name = part[:part]
          next if part_name == CONTENT_TYPES_PART || part_name.end_with?('.rels')

          content_type = part[:content_type] || package.content_type_for(part_name)
          extension = File.extname(part_name).delete('.').downcase
          if content_type
            overrides[part_name] = content_type
          elsif !defaults.key?(extension)
            overrides[part_name] = 'application/octet-stream'
          end
        end

        overrides[plan[:workbook_part]] ||= package.content_type_for(plan[:workbook_part]) ||
                                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'

        defaults_xml = defaults.map do |extension, content_type|
          %(  <Default Extension="#{xml_escape(extension)}" ContentType="#{xml_escape(content_type)}"/>)
        end

        overrides_xml = overrides.sort.map do |part_name, content_type|
          %(  <Override PartName="/#{xml_escape(part_name)}" ContentType="#{xml_escape(content_type)}"/>)
        end

        <<~XML
          <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          #{defaults_xml.join("\n")}
          #{overrides_xml.join("\n")}
          </Types>
        XML
      end

      def verify_references!(contents, included)
        included_names = included.map { |part| part[:part] }.to_set

        dangling = []
        contents.keys.grep(/\.rels\z/).each do |rels_part|
          owner = owner_for_rels(rels_part)
          next if owner.nil? || owner == plan[:workbook_part]
          next if rels_part == ROOT_RELS_PART

          document = Nokogiri::XML(contents[rels_part])
          document.xpath('//*[local-name()="Relationship"]').each do |node|
            next if node['TargetMode'].to_s.casecmp('external').zero?

            target = package.resolve_target(owner, node['Target'])
            dangling << { rels: rels_part, id: node['Id'], target: target } unless included_names.include?(target)
          end
        end

        return if dangling.empty?

        raise InconsistentCopyError,
              "copy would contain relationships to excluded parts: #{dangling.map { |entry| "#{entry[:rels]} -> #{entry[:target]}" }.join(', ')}"
      end

      def owner_for_rels(rels_part)
        return nil if rels_part == ROOT_RELS_PART

        directory = File.dirname(rels_part)
        basename = File.basename(rels_part, '.rels')
        directory == '_rels' ? basename : File.join(File.dirname(directory), basename)
      end

      def xml_escape(value)
        value.to_s
             .gsub('&', '&amp;')
             .gsub('<', '&lt;')
             .gsub('>', '&gt;')
             .gsub('"', '&quot;')
      end

      def write_zip(destination, contents)
        Zip::OutputStream.open(destination) do |zip|
          contents.keys.sort.each do |name|
            zip.put_next_entry(name)
            zip.write(contents[name].to_s)
          end
        end
      end
    end

    # Prunes shared strings to the indices actually used by selected sheets and
    # remaps the affected cell values. This keeps the copy self-consistent even
    # when the source sharedStrings part also holds text used only by sheets
    # outside the delivery scope.
    class SharedStringsRewrite
      attr_reader :part_name

      def initialize(package, plan, enabled: true)
        @package = package
        @plan = plan
        @enabled = enabled
        @part_name = plan[:workbook_parts]
                      .find { |part| part[:part].to_s.end_with?('sharedStrings.xml') }&.fetch(:part)
        @sheet_parts = plan[:sheets].map { |sheet| sheet[:part] }
        @occurrences = Hash.new(0)
        @mapping = {}
        @si_nodes = []

        collect_usage if @enabled
      end

      def sheet?(part_name)
        @sheet_parts.include?(part_name)
      end

      def rewrite_sheet(content)
        return content if @mapping.empty?

        content.gsub(%r{<c\b[^>]*\bt="s"[^>]*>.*?</c>}m) do |cell|
          cell.gsub(%r{<v>\s*(\d+)\s*</v>}) do
            old_index = Regexp.last_match(1).to_i
            new_index = @mapping[old_index]
            unless new_index
              raise InconsistentCopyError,
                    "shared string index #{old_index} used by a selected sheet is missing from the shared strings part"
            end
            "<v>#{new_index}</v>"
          end
        end
      end

      def content
        return nil unless @part_name
        return @package.read(@part_name) unless @enabled

        document = @package.xml(@part_name)
        return nil unless document

        root = document.root.dup
        root.children.remove

        @mapping.keys.sort_by { |old_index| @mapping[old_index] }.each do |old_index|
          source = @si_nodes[old_index]
          root.add_child(source.dup) if source
        end

        root['count'] = @occurrences.values.sum.to_s if root['count']
        root['uniqueCount'] = @mapping.size.to_s if root['uniqueCount']

        document.root = root
        document.to_xml
      end

      def summary
        return { part: @part_name, used: 0, kept: 0 } unless @part_name

        {
          part: @part_name,
          used: @occurrences.size,
          kept: @mapping.size
        }
      end

      private

      def collect_usage
        document = @package.xml(@part_name)
        @si_nodes = document ? document.xpath('//*[local-name()="si"]') : []

        used_order = []
        @sheet_parts.each do |sheet_part|
          content = @package.read(sheet_part)
          next unless content

          content.scan(%r{<c\b[^>]*\bt="s"[^>]*>.*?</c>}m).each do |cell|
            cell.scan(%r{<v>\s*(\d+)\s*</v>}).each do |match|
              index = match.first.to_i
              @occurrences[index] += 1
              used_order << index unless used_order.include?(index)
            end
          end
        end

        used_order.each_with_index do |old_index, new_index|
          @mapping[old_index] = new_index
        end
      end
    end
  end
end
