# frozen_string_literal: true

module Dry
  module Transformer
    module Reissue
      # Compresses sorted order numbers into the smallest sequence of
      # contiguous ranges and equal-step arithmetic sequences.
      #
      # Contiguous runs always win over wider arithmetic sequences; the
      # remaining isolated numbers are then grouped greedily by equal gaps.
      #
      # @example
      #   RangeCompressor.compress([1, 2, 3, 7, 9, 11, 20])
      #   # => [1..3, (7..11).step(2), (20..20)]
      #
      # @api public
      module RangeCompressor
        module_function

        # @param numbers [Enumerable<Integer>] order numbers
        #
        # @return [Array<Range, Enumerator::ArithmeticSequence>]
        #
        # @api public
        def compress(numbers)
          values = numbers.sort.uniq
          return [] if values.empty?

          ranges = []
          loose = []

          each_contiguous_run(values) do |run|
            if run.size > 1
              flush_loose(loose, ranges)
              ranges << (run.first..run.last)
            else
              loose.concat(run)
            end
          end

          flush_loose(loose, ranges)
          ranges
        end

        # Expands compressed ranges back into a sorted array of numbers.
        #
        # @param ranges [Enumerable<Range, Enumerator::ArithmeticSequence>]
        #
        # @return [Array<Integer>]
        #
        # @api public
        def expand(ranges)
          ranges.flat_map(&:to_a).sort.uniq
        end

        # Membership test that understands arithmetic sequences.
        #
        # `Range#cover?` cannot be used on an arithmetic sequence because it
        # ignores the step and would accept numbers never yielded by it.
        #
        # @param sequence [Range, Enumerator::ArithmeticSequence]
        # @param number [Integer]
        #
        # @return [Boolean]
        #
        # @api public
        def member?(sequence, number)
          sequence.include?(number)
        end

        # @api private
        def each_contiguous_run(values)
          index = 0
          while index < values.size
            stop = index + 1
            stop += 1 while stop < values.size && values[stop] == values[stop - 1] + 1
            yield values[index...stop]
            index = stop
          end
        end

        # @api private
        def flush_loose(loose, ranges)
          return if loose.empty?

          index = 0
          while index < loose.size
            if index + 1 < loose.size
              step = loose[index + 1] - loose[index]
              stop = index + 1
              stop += 1 while stop < loose.size &&
                               loose[stop] - loose[stop - 1] == step
              segment = loose[index...stop]
              ranges << arithmetic_sequence(segment, step)
              index = stop
            else
              ranges << (loose[index]..loose[index])
              index += 1
            end
          end
          loose.clear
        end

        # @api private
        def arithmetic_sequence(segment, step)
          return segment.first..segment.last if step == 1

          (segment.first..segment.last).step(step)
        end
      end
    end
  end
end
