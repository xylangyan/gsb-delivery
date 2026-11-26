# frozen_string_literal: true

module Dry
  module Transformer
    module Reissue
      # A uniform reconfiguration rule applied to a set of order-number
      # sequences in one patch batch.
      #
      # The transform is shared by every covered order; ingredient lists are
      # regenerated lazily from the parent snapshot, which lets a million-key
      # segment be handled batch-by-batch without materializing representative
      # orders up front.
      #
      # @api private
      class RuleSegment
        # @return [Symbol] rule name, e.g. +:reorder+
        attr_reader :name

        # @return [Array<Range, Enumerator::ArithmeticSequence>]
        attr_reader :number_sequences

        # @api private
        def initialize(name, number_sequences, transform)
          @name = name
          @number_sequences = number_sequences
          @transform = transform
        end

        # @param number [Integer]
        #
        # @return [Boolean]
        #
        # @api private
        def cover?(number)
          @number_sequences.any? { |sequence| sequence.include?(number) }
        end

        # Applies the rule to a parent ingredient list.
        #
        # @param parent_list [Array<Hash>] mutable copy of the parent list
        # @param source_key [Object]
        #
        # @return [Array<Hash>]
        #
        # @api private
        def apply(parent_list, source_key)
          @transform.call(parent_list, source_key, @name)
        end
      end
    end
  end
end
