# frozen_string_literal: true

module Roo
  class Excelx
    # A small Hash-like value object so callers can use either hash access or
    # reader methods:
    #
    #   info[:formula]
    #   info.formula
    class FormulaInfo < Hash
      KNOWN_KEYS = %i[
        kind formula literal master shared_formula_index shared_formula_ref
        resolved unresolved_reason cache calculated value_source freshness
      ].freeze

      def initialize(values = {})
        super()
        update(values)
      end

      KNOWN_KEYS.each do |key|
        define_method(key) { self[key] }
      end

      def method_missing(name, *args)
        return self[name] if args.empty? && key?(name)

        super
      end

      def respond_to_missing?(name, include_private = false)
        key?(name) || super
      end
    end

    module SharedFormula
      # One <f t="shared" ref="..." si="...">definition. The formula is stored
      # at +coordinate+ and applies to every cell inside +ref+.
      class Definition
        attr_reader :index, :coordinate, :ref, :formula, :literal

        def initialize(index:, coordinate:, ref:, formula:, literal: formula)
          @index = index
          @coordinate = coordinate
          @ref = ref
          @formula = formula
          @literal = literal
        end

        def covers?(coordinate)
          first, last = ref.to_s.split(':', 2)
          return false if first.nil? || first.empty?

          start_coordinate = Roo::Utils.extract_coordinate(first)
          end_coordinate = last ? Roo::Utils.extract_coordinate(last) : start_coordinate

          rows = [start_coordinate.row, end_coordinate.row].minmax
          columns = [start_coordinate.column, end_coordinate.column].minmax

          coordinate.row.between?(rows[0], rows[1]) &&
            coordinate.column.between?(columns[0], columns[1])
        rescue ArgumentError
          false
        end
      end

      # Keeps every shared-formula master in the worksheet. Keeping a registry
      # lets the streaming reader resolve a dependent whose master is outside
      # the row window being streamed.
      class Registry
        def initialize
          @definitions = {}
        end

        def register(definition)
          (@definitions[definition.index] ||= []) << definition
          definition
        end

        def definitions_for(index)
          @definitions[index] || []
        end

        # Return the definition that actually covers +coordinate+.
        #
        # A later definition with the same shared-formula index supersedes an
        # earlier one. That is how a workbook that narrows a shared range is
        # represented: dependents inside the new ref use the new anchor, and a
        # cell outside it must not be reconstructed from the old, wider anchor.
        def definition_for(index, coordinate)
          definition = definitions_for(index).last
          return nil unless definition

          definition.covers?(coordinate) ? definition : nil
        end
      end
    end
  end
end
