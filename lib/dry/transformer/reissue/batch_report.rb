# frozen_string_literal: true

module Dry
  module Transformer
    module Reissue
      # Result of reconciling one patch batch.
      #
      # @api public
      class BatchReport
        # @return [Integer] snapshot generation produced by the batch
        attr_reader :generation

        # @return [Array<Integer>] sorted order numbers changed by explicit
        #   patches; uniform rule segments stay unexpanded in {#ranges}
        attr_reader :changed_numbers

        # @return [Array<Range, Enumerator::ArithmeticSequence>] compressed
        #   reissue ranges; every changed number belongs to exactly one of them
        attr_reader :ranges

        # @api private
        def initialize(generation:, changed_numbers:, ranges:)
          @generation = generation
          @changed_numbers = changed_numbers
          @ranges = ranges
        end

        # Expands the compressed ranges on demand.
        #
        # @return [Array<Integer>]
        #
        # @api public
        def all_numbers
          RangeCompressor.expand(@ranges)
        end

        # @param number [Integer]
        #
        # @return [Boolean]
        #
        # @api public
        def cover?(number)
          @ranges.any? { |sequence| sequence.include?(number) }
        end
      end
    end
  end
end
