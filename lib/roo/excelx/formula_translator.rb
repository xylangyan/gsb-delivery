# frozen_string_literal: true

module Roo
  class Excelx
    # Translates the A1-style relative references in a shared formula from the
    # cell that stores the formula (the shared-formula master) to another cell
    # in the same shared group.
    #
    # OOXML stores the formula only once, on the master cell. The other cells
    # carry an empty <f t="shared" si="..."/> element. Excel calculates those
    # formulas by shifting every relative reference by the row/column offset
    # between the master and the dependent cell. Absolute references (the ones
    # prefixed with "$") do not move. References inside string literals and
    # sheet names must be left alone.
    module FormulaTranslator
      extend self

      MAX_COLUMN = 16_384
      MAX_ROW = 1_048_576

      # A sheet prefix is either quoted ('My Sheet'!) or unquoted (Sheet1!),
      # optionally preceded by an external-workbook marker ([Book.xlsx]).
      PREFIX_PATTERN = /(?:\[[^\]]*\])?(?:'(?:[^']|'')*'|[A-Za-z_\\][A-Za-z0-9_.\\]*)!/

      # Match one reference at the current position. The reference can be a
      # cell, a cell range, a whole-column range, or a whole-row range.
      REFERENCE_PATTERN = /
        (?<prefix>#{PREFIX_PATTERN})?
        (?<ref>
          \$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?
          |
          \$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}
          |
          \$?\d+:\$?\d+
        )
        (?![A-Za-z0-9_(!])
      /x

      CELL_PATTERN = /\A(\$?)([A-Za-z]{1,3})(\$?)(\d+)\z/
      COLUMN_PATTERN = /\A(\$?)([A-Za-z]{1,3})\z/
      ROW_PATTERN = /\A(\$?)(\d+)\z/

      def translate(formula, master_coordinate, target_coordinate)
        return formula if formula.nil? || formula.empty?

        row_delta = target_coordinate.row - master_coordinate.row
        column_delta = target_coordinate.column - master_coordinate.column
        return formula if row_delta.zero? && column_delta.zero?

        result = +""
        index = 0

        while index < formula.length
          character = formula[index]

          if character == '"'
            literal, index = consume_quoted(formula, index, '"')
            result << literal
          elsif character == "'"
            literal, index = consume_quoted(formula, index, "'")
            result << literal
            # A quoted sheet name is followed by "!". Consume it as part of
            # the opaque prefix so the reference immediately after it is still
            # translated while the name itself is preserved.
            if formula[index] == "!"
              result << "!"
              index += 1
            end
          else
            if reference_boundary?(formula, index)
              match = REFERENCE_PATTERN.match(formula, index)
              if match && match.begin(0) == index
                result << (match[:prefix] || "")
                result << translate_reference(match[:ref], row_delta, column_delta)
                index = match.end(0)
                next
              end
            end

            result << character
            index += 1
          end
        end

        result
      end

      private

      # A reference starts at the beginning of the formula or after an
      # operator, separator or opening bracket. Requiring this avoids treating
      # function names such as LOG10 or identifiers inside defined names as
      # cell references.
      def reference_boundary?(formula, index)
        return true if index.zero?

        formula[index - 1].match?(/[\s,+\-*\/^&=<>(){};:!%]/)
      end

      # Copy a quoted section, handling the doubled quote escape used by Excel
      # (e.g. "a""b" and 'O''Brien').
      def consume_quoted(formula, index, quote)
        start = index
        index += 1
        while index < formula.length
          if formula[index] == quote
            if formula[index + 1] == quote
              index += 2
            else
              index += 1
              break
            end
          else
            index += 1
          end
        end
        [formula[start...index], index]
      end

      def translate_reference(reference, row_delta, column_delta)
        parts = reference.split(":", -1)
        translated = parts.map do |part|
          case part
          when CELL_PATTERN
            translate_cell(part, row_delta, column_delta)
          when COLUMN_PATTERN
            translate_column(part, column_delta)
          when ROW_PATTERN
            translate_row(part, row_delta)
          else
            part
          end
        end

        return "#REF!" if translated.any?(&:nil?)

        translated.join(":")
      end

      def translate_cell(reference, row_delta, column_delta)
        match = CELL_PATTERN.match(reference)
        column_absolute = !match[1].empty?
        row_absolute = !match[3].empty?

        column = Roo::Utils.letter_to_number(match[2])
        row = match[4].to_i

        column += column_delta unless column_absolute
        row += row_delta unless row_absolute

        return nil if column < 1 || column > MAX_COLUMN || row < 1 || row > MAX_ROW

        "#{column_absolute ? '$' : ''}#{Roo::Utils.number_to_letter(column)}" \
          "#{row_absolute ? '$' : ''}#{row}"
      end

      def translate_column(reference, column_delta)
        match = COLUMN_PATTERN.match(reference)
        absolute = !match[1].empty?

        column = Roo::Utils.letter_to_number(match[2])
        column += column_delta unless absolute

        return nil if column < 1 || column > MAX_COLUMN

        "#{absolute ? '$' : ''}#{Roo::Utils.number_to_letter(column)}"
      end

      def translate_row(reference, row_delta)
        match = ROW_PATTERN.match(reference)
        absolute = !match[1].empty?

        row = match[2].to_i
        row += row_delta unless absolute

        return nil if row < 1 || row > MAX_ROW

        "#{absolute ? '$' : ''}#{row}"
      end
    end
  end
end
