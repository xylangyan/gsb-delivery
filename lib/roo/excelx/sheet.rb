require 'forwardable'
module Roo
  class Excelx
    class Sheet
      extend Forwardable

      delegate [:styles, :workbook, :shared_strings, :rels_files, :sheet_files, :comments_files, :image_rels, :image_files, :drawing_files] => :@shared

      attr_reader :images

      def initialize(name, shared, sheet_index, options = {})
        @name = name
        @shared = shared
        @sheet_index = sheet_index
        @rels = Relationships.new(rels_files[sheet_index])
        @images = Images.new(drawing_rels_path).list
        @comments = Comments.new(comments_files[sheet_index])
        @sheet = SheetDoc.new(sheet_files[sheet_index], @rels, shared, options)
      end

      def cells
        @cells ||= @sheet.cells(@rels)
      end

      def present_cells
        @present_cells ||= begin
          warn %{
[DEPRECATION] present_cells is deprecated. Alternate:
  with activesupport    => cells[key].presence
  without activesupport => cells[key]&.presence
          }
          cells.select { |_, cell| cell&.presence }
        end
      end

      # Yield each row as array of Excelx::Cell objects
      # accepts options max_rows (int) (offset by 1 for header),
      # pad_cells (boolean) and offset (int)
      def each_row(options = {}, &block)
        row_count = 0
        options[:offset] ||= 0
        @sheet.each_row_streaming do |row|
          break if options[:max_rows] && row_count == options[:max_rows] + options[:offset] + 1
          if block_given? && !(options[:offset] && row_count < options[:offset])
            block.call(cells_for_row_element(row, options))
          end
          row_count += 1
        end
      end

      def row(row_number)
        first_column.upto(last_column).map do |col|
          cells[[row_number, col]]&.value
        end
      end

      def column(col_number)
        first_row.upto(last_row).map do |row|
          cells[[row, col_number]]&.value
        end
      end

      # returns the number of the first non-empty row
      def first_row
        @first_row ||= first_last_row_col[:first_row]
      end

      def last_row
        @last_row ||= first_last_row_col[:last_row]
      end

      # returns the number of the first non-empty column
      def first_column
        @first_column ||= first_last_row_col[:first_column]
      end

      # returns the number of the last non-empty column
      def last_column
        @last_column ||= first_last_row_col[:last_column]
      end

      def excelx_format(key)
        cell = cells[key]
        styles.style_format(cell.style).to_s if cell
      end

      def hyperlinks
        @hyperlinks ||= @sheet.hyperlinks(@rels)
      end

      def comments
        @comments.comments
      end

      def dimensions
        @sheet.dimensions
      end

      # returns the 1 based numbers of the hidden rows in this sheet
      def hidden_rows
        @sheet.hidden_rows
      end

      # returns every image placement of this sheet with the original
      # coordinates from its drawing anchor. The same media file can be
      # placed several times; each placement is returned on its own.
      def image_placements
        @image_placements ||= build_image_placements
      end

      # returns the image placements expressed in visible row numbers
      # (hidden rows are collapsed). Cropped pictures whose top-left cell
      # is hidden are not lent to a neighbouring row: they are dropped.
      def visible_image_placements
        image_placements.filter_map { |placement| placement.in_visible_rows(hidden_rows) }
      end

      private

      # Internal: the index of the drawing attached to this sheet, found by
      # following the sheet's own drawing relationship instead of assuming
      # that sheet N uses drawing N.
      def drawing_index
        @drawing_index ||= begin
          rel = @rels.to_a.values.find { |r| r['Type'].to_s.end_with?('/drawing') }
          target = rel && rel['Target']
          target && target[%r{drawing(\d+)\.xml}, 1]&.to_i
        end
      end

      def drawing_rels_path
        drawing_index && image_rels[drawing_index - 1]
      end

      def drawing_path
        drawing_index && drawing_files[drawing_index - 1]
      end

      def build_image_placements
        return [] unless drawing_path

        entries = Images.new(drawing_rels_path).entries
        Drawing.new(drawing_path).anchors.map do |anchor|
          relationship_id = anchor.embed || anchor.link
          entry = entries[relationship_id] || {}
          media = entry[:name]
          ImagePlacement.new(
            anchor: anchor.type,
            relationship_id: relationship_id,
            name: anchor.name,
            media: media,
            path: media && image_files.find { |file| file[media] },
            target: entry[:target],
            external: entry[:external] || !anchor.link.nil?,
            src_rect: anchor.src_rect,
            row: anchor.from && anchor.from[:row] + 1,
            col: anchor.from && anchor.from[:col] + 1,
            to_row: anchor.to && anchor.to[:row] + 1,
            to_col: anchor.to && anchor.to[:col] + 1,
            row_offset: anchor.from && anchor.from[:row_offset],
            col_offset: anchor.from && anchor.from[:col_offset],
            to_row_offset: anchor.to && anchor.to[:row_offset],
            to_col_offset: anchor.to && anchor.to[:col_offset],
            width: anchor.ext && anchor.ext[:cx],
            height: anchor.ext && anchor.ext[:cy]
          )
        end
      end

      # Take an xml row and return an array of Excelx::Cell objects
      # optionally pad array to header width(assumed 1st row).
      # takes option pad_cells (boolean) defaults false
      def cells_for_row_element(row_element, options = {})
        return [] unless row_element
        cell_col = 0
        cells = []
        @sheet.each_cell(row_element) do |cell|
          cells.concat(pad_cells(cell, cell_col)) if options[:pad_cells]
          cells << cell
          cell_col = cell.coordinate.column
        end
        cells
      end

      def pad_cells(cell, last_column)
        pad = []
        (cell.coordinate.column - 1 - last_column).times { pad << nil }
        pad
      end

      def first_last_row_col
        @first_last_row_col ||= begin
          first_row = last_row = first_col = last_col = nil

          cells.each do |(row, col), cell|
            next unless cell&.presence
            first_row ||= row
            last_row ||= row
            first_col ||= col
            last_col ||= col

            if row > last_row
              last_row = row
            elsif row < first_row
              first_row = row
            end

            if col > last_col
              last_col = col
            elsif col < first_col
              first_col = col
            end
          end

          {first_row: first_row, last_row: last_row, first_column: first_col, last_column: last_col}
        end
      end
    end
  end
end
