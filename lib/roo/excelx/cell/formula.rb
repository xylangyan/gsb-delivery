# frozen_string_literal: true

module Roo
  class Excelx
    class Cell
      # A formula cell whose cached result is absent or empty.
      #
      # Excel workbooks are not required to store a cached result for a
      # formula. Roo cannot recalculate formulas, so there is nothing to
      # display in that case. The cell still exists, keeps its formula, and
      # reports that no calculated value is available.
      class Formula < Cell::Base
        attr_reader :value, :formula, :format, :cell_type, :cell_value, :coordinate

        attr_reader_with_default default_type: :formula

        def initialize(value, formula, excelx_type, style, link, coordinate)
          super
          @format = excelx_type&.last
          @value = value
        end

        def formatted_value
          @value.nil? ? "" : @value.to_s
        end

        alias_method :to_s, :formatted_value

        def empty?
          false
        end
      end
    end
  end
end
