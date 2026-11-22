# frozen_string_literal: true

require 'digest'

module Roo
  class Excelx
    # Rereads a written copy and checks it cell by cell against the selected
    # sheets of the source, including comments, hyperlinks, and visible images.
    class Auditor
      CellMismatch = Struct.new(:sheet, :row, :column, :source, :copy, keyword_init: true) do
        def to_h
          {
            sheet: sheet,
            row: row,
            column: column,
            source: source,
            copy: copy
          }
        end
      end

      attr_reader :source_path, :copy_path, :sheet_names

      def initialize(source_path, copy_path, sheet_names:)
        @source_path = source_path
        @copy_path = copy_path
        @sheet_names = Array(sheet_names)
      end

      def report
        source = Roo::Excelx.new(source_path)
        copy = Roo::Excelx.new(copy_path)

        begin
          missing_sheets = sheet_names - copy.sheets
          raise ExportError, "copy is missing sheet(s): #{missing_sheets.join(', ')}" unless missing_sheets.empty?

          extra_sheets = copy.sheets - sheet_names
          raise ExportError, "copy contains unauthorized sheet(s): #{extra_sheets.join(', ')}" unless extra_sheets.empty?

          cell_differences = []
          comment_differences = []
          hyperlink_differences = []
          attachment_differences = []
          cells_checked = 0
          cells_compared = Hash.new(0)

          sheet_names.each do |sheet_name|
            checked, differences = compare_cells(source, copy, sheet_name)
            cells_checked += checked
            cells_compared[sheet_name] = checked
            cell_differences.concat(differences)
            comment_differences.concat(compare_comments(source, copy, sheet_name))
            hyperlink_differences.concat(compare_hyperlinks(source, copy, sheet_name))
            attachment_differences.concat(compare_images(source, copy, sheet_name))
          end

          {
            ok: cell_differences.empty? && comment_differences.empty? &&
                hyperlink_differences.empty? && attachment_differences.empty?,
            source: source_path,
            copy: copy_path,
            sheets: sheet_names,
            cells_checked: cells_checked,
            cells_compared_by_sheet: cells_compared,
            cell_differences: cell_differences.map(&:to_h),
            comment_differences: comment_differences,
            hyperlink_differences: hyperlink_differences,
            attachment_differences: attachment_differences
          }
        ensure
          source.close
          copy.close
        end
      end

      private

      def compare_cells(source, copy, sheet_name)
        differences = []
        checked = 0
        rows = [source.first_row(sheet_name), copy.first_row(sheet_name)].compact
        columns = [source.first_column(sheet_name), copy.first_column(sheet_name)].compact
        return [0, differences] if rows.empty? || columns.empty?

        last_row = [source.last_row(sheet_name), copy.last_row(sheet_name)].compact.max
        last_column = [source.last_column(sheet_name), copy.last_column(sheet_name)].compact.max

        rows.min.upto(last_row) do |row|
          columns.min.upto(last_column) do |column|
            checked += 1
            source_value = source.cell(row, column, sheet_name)
            copy_value = copy.cell(row, column, sheet_name)
            next if source_value == copy_value

            differences << CellMismatch.new(
              sheet: sheet_name,
              row: row,
              column: column,
              source: source_value,
              copy: copy_value
            )
          end
        end

        [checked, differences]
      end

      def compare_comments(source, copy, sheet_name)
        source_comments = source.comments(sheet_name)
        copy_comments = copy.comments(sheet_name)
        return [] if source_comments == copy_comments

        [{ sheet: sheet_name, source: source_comments, copy: copy_comments }]
      end

      def compare_hyperlinks(source, copy, sheet_name)
        differences = []
        rows = [source.first_row(sheet_name), copy.first_row(sheet_name)].compact
        columns = [source.first_column(sheet_name), copy.first_column(sheet_name)].compact
        return differences if rows.empty? || columns.empty?

        last_row = [source.last_row(sheet_name), copy.last_row(sheet_name)].compact.max
        last_column = [source.last_column(sheet_name), copy.last_column(sheet_name)].compact.max

        rows.min.upto(last_row) do |row|
          columns.min.upto(last_column) do |column|
            source_hyperlink = source.hyperlink(row, column, sheet_name)
            copy_hyperlink = copy.hyperlink(row, column, sheet_name)
            next if source_hyperlink == copy_hyperlink

            differences << {
              sheet: sheet_name,
              row: row,
              column: column,
              source: source_hyperlink,
              copy: copy_hyperlink
            }
          end
        end

        differences
      end

      def compare_images(source, copy, sheet_name)
        source_digests = image_digests(source.images(sheet_name))
        copy_digests = image_digests(copy.images(sheet_name))
        return [] if source_digests == copy_digests

        [{
          sheet: sheet_name,
          source: source_digests,
          copy: copy_digests
        }]
      end

      def image_digests(paths)
        paths.compact.map do |path|
          if path.respond_to?(:path) && File.file?(path.path)
            Digest::SHA256.file(path.path).hexdigest
          elsif path.is_a?(String) && File.file?(path)
            Digest::SHA256.file(path).hexdigest
          else
            path.to_s
          end
        end.sort
      end
    end
  end
end
