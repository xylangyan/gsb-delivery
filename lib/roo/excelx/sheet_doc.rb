# frozen_string_literal: true

require 'forwardable'
require 'roo/excelx/extractor'
require 'roo/excelx/formula'
require 'roo/excelx/formula_translator'

module Roo
  class Excelx
    class SheetDoc < Excelx::Extractor
      extend Forwardable
      delegate [:workbook] => :@shared

      def initialize(path, relationships, shared, options = {})
        super(path)
        @shared = shared
        @options = options
        @relationships = relationships
      end

      def cells(relationships)
        @cells ||= extract_cells(relationships)
      end

      def hyperlinks(relationships)
        # If you're sure you're not going to need this hyperlinks you can discard it
        @hyperlinks ||= if @options[:no_hyperlinks] || !relationships.include_type?("hyperlink")
          {}
        else
          extract_hyperlinks(relationships)
        end
      end

      # Get the dimensions for the sheet.
      # This is the upper bound of cells that might
      # be parsed. (the document may be sparse so cell count is only upper bound)
      def dimensions
        @dimensions ||= extract_dimensions
      end

      # Yield each row xml element to caller
      def each_row_streaming(&block)
        Roo::Utils.each_element(@path, 'row', &block)
      end

      # Shared-formula masters are collected in a separate pass. A master may
      # live outside the row window currently being streamed, yet dependents
      # inside the window still need the master's formula and coordinates.
      def shared_formula_registry
        @shared_formula_registry ||= extract_shared_formula_registry
      end

      # Yield each cell as Excelx::Cell to caller for given
      # row xml
      def each_cell(row_xml)
        return [] unless row_xml
        row_xml.children.each do |cell_element|
          coordinate = ::Roo::Utils.extract_coordinate(cell_element["r"])
          hyperlinks = hyperlinks(@relationships)[coordinate]

          yield cell_from_xml(cell_element, hyperlinks, coordinate)
        end
      end

      private

      def cell_value_type(type, format)
        case type
        when 's'
          :shared
        when 'b'
          :boolean
        when 'str'
          :string
        when 'inlineStr'
          :inlinestr
        else
          Excelx::Format.to_type(format)
        end
      end

      def child_named(cell_xml, name)
        cell_xml.children.find { |child| child.name == name }
      end

      def extract_shared_formula_registry
        registry = SharedFormula::Registry.new

        Roo::Utils.each_element(@path, 'row') do |row_xml|
          row_xml.children.each do |cell_xml|
            next unless cell_xml.name == 'c'

            formula_xml = child_named(cell_xml, 'f')
            next unless formula_xml && formula_xml['t'] == 'shared'

            literal = formula_xml.content
            next if literal.nil? || literal.empty?

            coordinate = extract_coordinate_or_nil(cell_xml['r'])
            next unless coordinate

            registry.register(
              SharedFormula::Definition.new(
                index: formula_xml['si'],
                coordinate: coordinate,
                ref: formula_xml['ref'],
                formula: literal,
                literal: literal
              )
            )
          end
        end

        registry
      end

      def extract_coordinate_or_nil(reference)
        return nil if reference.nil? || reference.empty?

        ::Roo::Utils.extract_coordinate(reference)
      rescue ArgumentError
        nil
      end

      # Returns [formula, formula_info] for the <f> element. Shared dependents
      # are translated to this cell's actual coordinates. formula_info records
      # where the formula came from; the cache status is added later.
      def formula_for(formula_xml, coordinate)
        return [nil, nil] unless formula_xml

        type = formula_xml['t']
        literal = formula_xml.content

        case type
        when 'shared'
          shared_formula_for(formula_xml, coordinate, literal)
        when 'array', 'dataTable'
          info = build_formula_info(
            kind: type.to_sym,
            master: coordinate,
            literal: literal,
            formula: literal,
            resolved: true
          )
          [literal, info]
        else
          info = build_formula_info(
            kind: :explicit,
            master: coordinate,
            literal: literal,
            formula: literal,
            resolved: true
          )
          [literal, info]
        end
      end

      def shared_formula_for(formula_xml, coordinate, literal)
        index = formula_xml['si']
        ref = formula_xml['ref']

        if literal && !literal.empty?
          # The cell carrying the text is the master. Its formula is already
          # expressed at its own coordinates.
          info = build_formula_info(
            kind: :shared,
            master: coordinate,
            literal: literal,
            formula: literal,
            resolved: true,
            shared_formula_index: index,
            shared_formula_ref: ref
          )
          return [literal, info]
        end

        definition = shared_formula_registry.definition_for(index, coordinate)

        unless definition
          # A dependent whose master/ref does not cover this actual cell must
          # not be reconstructed from a different or older anchor. It still has
          # a formula identity, but no literal can be proven for this cell.
          info = build_formula_info(
            kind: :shared,
            master: nil,
            literal: nil,
            formula: nil,
            resolved: false,
            shared_formula_index: index,
            shared_formula_ref: nil,
            unresolved_reason: :shared_formula_out_of_range
          )
          return [nil, info]
        end

        translated = FormulaTranslator.translate(definition.formula, definition.coordinate, coordinate)
        info = build_formula_info(
          kind: :shared,
          master: definition.coordinate,
          literal: definition.literal,
          formula: translated,
          resolved: true,
          shared_formula_index: index,
          shared_formula_ref: definition.ref
        )
        [translated, info]
      end

      def build_formula_info(values)
        Excelx::FormulaInfo.new(values)
      end

      def cache_status_for(value_xml, inline_xml)
        return :present if inline_xml && !inline_xml.search('t').map(&:content).join.empty?
        return :missing unless value_xml

        value_xml.content.empty? ? :empty : :present
      end

      # Roo never recalculates formulas. A stored <v> is a cached result: it
      # cannot be verified against the formula, so it must not be presented as
      # a freshly calculated value. An empty <v> is an empty cached result,
      # not a cached zero; a missing <v> is no result at all.
      def annotate_cache(info, cache_status)
        info[:cache] = cache_status
        info[:calculated] = false
        info[:value_source] = cache_status == :present ? :cached : :none
        info[:freshness] = :unverified
      end

      def attach_formula_info(cell, formula_info)
        cell.formula_info = formula_info if formula_info && cell
        cell
      end

      # Internal: Creates a cell based on an XML clell..
      #
      # cell_xml - a Nokogiri::XML::Element. e.g.
      #             <c r="A5" s="2">
      #               <v>22606</v>
      #             </c>
      # hyperlink - a String for the hyperlink for the cell or nil when no
      #             hyperlink is present.
      # coordinate - a Roo::Excelx::Coordinate for the coordinate for the cell
      #              or nil to extract coordinate from cell_xml.
      # empty_cell - an Optional Boolean value.
      #
      # Examples
      #
      #    cells_from_xml(<Nokogiri::XML::Element>, nil, nil)
      #    # => <Excelx::Cell::String>
      #
      # Returns a type of <Excelx::Cell>.
      def cell_from_xml(cell_xml, hyperlink, coordinate, empty_cell=true)
        coordinate ||= ::Roo::Utils.extract_coordinate(cell_xml["r"])
        cell_xml_children = cell_xml.children
        return create_empty_cell(coordinate, empty_cell) if cell_xml_children.empty?

        # NOTE: This is error prone, to_i will silently turn a nil into a 0.
        #       This works by coincidence because Format[0] is General.
        style = cell_xml["s"].to_i
        formula_xml = child_named(cell_xml, 'f')
        value_xml = child_named(cell_xml, 'v')
        inline_xml = child_named(cell_xml, 'is')

        formula, formula_info = formula_for(formula_xml, coordinate)
        cache_status = cache_status_for(value_xml, inline_xml)
        annotate_cache(formula_info, cache_status) if formula_info

        if inline_xml
          content = inline_xml.search('t').map(&:content).join
          unless content.empty?
            return attach_formula_info(
              Excelx::Cell.cell_class(:string).new(content, formula, style, hyperlink, coordinate),
              formula_info
            )
          end
        end

        if value_xml && cache_status == :present
          format = style_format(style)
          value_type = cell_value_type(cell_xml["t"], format)

          return create_cell_from_value(value_type, value_xml, formula, format, style, hyperlink, coordinate, formula_info)
        end

        if formula_info
          # A formula whose cached <v> is empty or absent. Keep the cell and
          # its identity. An empty <v> is recorded as an empty cached value;
          # an absent <v> has no value at all. Neither is turned into 0 or
          # 1900-01-01.
          cached_value = cache_status == :empty ? "" : nil
          formula_cell = Excelx::Cell::Formula.new(
            cached_value,
            formula,
            [:numeric_or_formula, style_format(style).to_s],
            style,
            hyperlink,
            coordinate
          )
          formula_cell.formula_info = formula_info
          return formula_cell
        end

        create_empty_cell(coordinate, empty_cell)
      end

      def create_empty_cell(coordinate, empty_cell)
        if empty_cell
          Excelx::Cell::Empty.new(coordinate)
        end
      end

      def create_cell_from_value(value_type, cell, formula, format, style, hyperlink, coordinate, formula_info = nil)
        # NOTE: format.to_s can replace excelx_type as an argument for
        #       Cell::Time, Cell::DateTime, Cell::Date or Cell::Number, but
        #       it will break some brittle tests.
        excelx_type = [:numeric_or_formula, format.to_s]

        # NOTE: There are only a few situations where value != cell.content
        #       1. when a sharedString is used. value = sharedString;
        #          cell.content = id of sharedString
        #       2. boolean cells: value = 'TRUE' | 'FALSE'; cell.content = '0' | '1';
        #          But a boolean cell should use TRUE|FALSE as the formatted value
        #          and use a Boolean for it's value. Using a Boolean value breaks
        #          Roo::Base#to_csv.
        #       3. formula
        created_cell =
          case value_type
          when :shared
            cell_content = cell.content.to_i
            value = shared_strings.use_html?(cell_content) ? shared_strings.to_html[cell_content] : shared_strings[cell_content]
            Excelx::Cell.cell_class(:string).new(value, formula, style, hyperlink, coordinate)
          when :boolean, :string
            value = cell.content
            Excelx::Cell.cell_class(value_type).new(value, formula, style, hyperlink, coordinate)
          when :time, :datetime
            cell_content = cell.content.to_f
            # NOTE: A date will be a whole number. A time will have be > 1. And
            #      in general, a datetime will have decimals. But if the cell is
            #      using a custom format, it's possible to be interpreted incorrectly.
            #      cell_content.to_i == cell_content && standard_style?=> :date
            #
            #      Should check to see if the format is standard or not. If it's a
            #      standard format, than it's a date, otherwise, it is a datetime.
            #      @styles.standard_style?(style_id)
            #      STANDARD_STYLES.keys.include?(style_id.to_i)
            cell_type = if cell_content < 1.0
                          :time
                        elsif (cell_content - cell_content.floor).abs > 0.000001
                          :datetime
                        else
                          :date
                        end
            base_value = cell_type == :date ? base_date : base_timestamp
            Excelx::Cell.cell_class(cell_type).new(cell_content, formula, excelx_type, style, hyperlink, base_value, coordinate)
          when :date
            Excelx::Cell.cell_class(:date).new(cell.content, formula, excelx_type, style, hyperlink, base_date, coordinate)
          else
            Excelx::Cell.cell_class(:number).new(cell.content, formula, excelx_type, style, hyperlink, coordinate)
          end

        attach_formula_info(created_cell, formula_info)
      end

      def extract_hyperlinks(relationships)
        return {} unless (hyperlinks = doc.xpath('/worksheet/hyperlinks/hyperlink'))

        hyperlinks.each_with_object({}) do |hyperlink, hash|
          if relationship = relationships[hyperlink['id']]
            target_link = relationship['Target']
            target_link += "##{hyperlink['location']}" if hyperlink['location']

            Roo::Utils.coordinates_in_range(hyperlink["ref"].to_s) do |coord|
              hash[coord] = target_link
            end
          end
        end
      end

      def expand_merged_ranges(cells)
        # Extract merged ranges from xml
        merges = {}
        doc.xpath('/worksheet/mergeCells/mergeCell').each do |mergecell_xml|
          src, dst = mergecell_xml["ref"].split(/:/).map { |ref| ::Roo::Utils.ref_to_key(ref) }
          next unless cells[src]
          for row in src[0]..dst[0] do
            for col in src[1]..dst[1] do
              next if row == src[0] && col == src[1]
              merges[[row, col]] = src
            end
          end
        end
        # Duplicate value into all cells in merged range
        merges.each do |dst, src|
          cells[dst] = cells[src]
        end
      end

      def extract_cells(relationships)
        extracted_cells = {}
        empty_cell = @options[:empty_cell]

        doc.xpath('/worksheet/sheetData/row').each.with_index(1) do |row_xml, ycoord|
          row_xml.xpath('c').each.with_index(1) do |cell_xml, xcoord|
            r = cell_xml['r']
            coordinate =
              if r.nil?
                ::Roo::Excelx::Coordinate.new(ycoord, xcoord)
              else
                ::Roo::Utils.extract_coordinate(r)
              end

            cell = cell_from_xml(cell_xml, hyperlinks(relationships)[coordinate], coordinate, empty_cell)
            extracted_cells[coordinate] = cell if cell
          end
        end

        expand_merged_ranges(extracted_cells) if @options[:expand_merged_ranges]

        extracted_cells
      end

      def extract_dimensions
        Roo::Utils.each_element(@path, 'dimension') do |dimension|
          return dimension["ref"]
        end
      end

      def style_format(style)
        @shared.styles.style_format(style)
      end

      def base_date
        @shared.base_date
      end

      def base_timestamp
        @shared.base_timestamp
      end

      def shared_strings
        @shared.shared_strings
      end
    end
  end
end
