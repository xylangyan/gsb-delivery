# frozen_string_literal: true

module Roo
  class Excelx
    # Public: A single placement of an image on a worksheet. Placements are
    #         built from the actual drawing relationship of the sheet, the
    #         drawing anchors and their offsets, so the same media file can
    #         appear in several placements (even on different sheets) and
    #         each placement keeps its own coordinates.
    #
    #         Coordinates are 1 based, like the rest of Roo. Offsets and
    #         dimensions are in EMU, as stored in the drawing XML.
    class ImagePlacement
      attr_reader :anchor, :row, :col, :to_row, :to_col,
                  :row_offset, :col_offset, :to_row_offset, :to_col_offset,
                  :width, :height, :relationship_id, :name, :media, :path,
                  :target, :src_rect

      def initialize(anchor:, relationship_id:, name:, media:, path:, target:,
                     external:, src_rect:, row:, col:, to_row:, to_col:,
                     row_offset:, col_offset:, to_row_offset:, to_col_offset:,
                     width:, height:)
        @anchor = anchor
        @relationship_id = relationship_id
        @name = name
        @media = media
        @path = path
        @target = target
        @external = external
        @src_rect = src_rect
        @row = row
        @col = col
        @to_row = to_row
        @to_col = to_col
        @row_offset = row_offset
        @col_offset = col_offset
        @to_row_offset = to_row_offset
        @to_col_offset = to_col_offset
        @width = width
        @height = height
      end

      # Public: true when the picture only declares an external link. No
      #         media is extracted for external pictures.
      def external?
        !!@external
      end

      # Public: true when the picture is explicitly cropped (a:srcRect).
      def cropped?
        !@src_rect.nil?
      end

      # Public: true when the drawing relationship points to a media entry
      #         that is not part of the package. This is different from a
      #         sheet without images: the placement exists, but its media
      #         is missing.
      def missing?
        !external? && @path.nil?
      end

      # Public: true when the placement's anchor covers the given cell
      #         (1 based coordinates).
      def covers?(row, col)
        return false if @row.nil?

        row.between?(@row, @to_row || @row) && col.between?(@col, @to_col || @col)
      end

      # Public: a copy of the placement expressed in visible row numbers
      #         (hidden rows are collapsed), or nil when the placement
      #         cannot be attributed to a visible row. A cropped picture
      #         whose top-left cell is hidden is not lent to a neighbouring
      #         row: it keeps no visible-row attribution at all.
      def in_visible_rows(hidden_rows)
        return self if @row.nil?

        hidden = hidden_rows.sort
        visible_row = visible_row_index(@row, hidden)
        if visible_row.nil?
          return nil if cropped?

          visible_row = next_visible_row_index(@row, hidden)
          return nil unless visible_row
        end

        visible_to_row = @to_row && visible_row_index_floor(@to_row, hidden)
        with_coordinates(row: visible_row, to_row: visible_to_row)
      end

      protected

      def with_coordinates(row:, to_row:)
        dup.tap do |copy|
          copy.instance_variable_set(:@row, row)
          copy.instance_variable_set(:@to_row, to_row)
        end
      end

      private

      # 1 based index of the row among visible rows, nil when hidden.
      def visible_row_index(row, hidden)
        return nil if hidden.include?(row)

        visible_row_index_floor(row, hidden)
      end

      # Number of visible rows up to and including the given row.
      def visible_row_index_floor(row, hidden)
        row - hidden.count { |h| h <= row }
      end

      # Visible index of the first visible row strictly below the given row.
      def next_visible_row_index(row, hidden)
        candidate = row + 1
        candidate += 1 while hidden.include?(candidate)
        visible_row_index_floor(candidate, hidden)
      end
    end
  end
end
