# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# A permitted output representation for one member and one reader
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # A Representation says how a member may be rewritten for a particular
      # reader. It never changes the source archive. Rewriting is only permitted
      # when the business manifest states one of these forms.
      #
      # +axes+ maps output axis => source axis, for example <tt>{ z: :m }</tt>
      # to deliver a measured value as the reader's Z coordinate.
      #
      # A representation that would drop a source axis must state
      # <tt>allow_drop: true</tt>. Otherwise the planner reports the missing
      # capability instead of silently losing data.
      class Representation
        attr_reader :dimensions, :axes, :allow_drop

        def initialize(dimensions:, axes: {}, allow_drop: false)
          @dimensions = Array(dimensions).map(&:to_sym).uniq.freeze
          @axes = (axes || {}).to_h { |out_axis, in_axis| [out_axis.to_sym, in_axis.to_sym] }.freeze
          @allow_drop = allow_drop ? true : false
        end

        def dimensions?
          !@dimensions.empty?
        end

        def allow_drop?
          @allow_drop
        end

        # True when this representation requires rewriting even if the reader
        # could otherwise take the original bytes.
        def rewriting?
          !@axes.empty? || !@dimensions.empty?
        end

        def ==(other)
          other.is_a?(Representation) &&
            dimensions == other.dimensions &&
            axes == other.axes &&
            allow_drop == other.allow_drop
        end
        alias eql? ==

        def hash
          [@dimensions, @axes, @allow_drop].hash
        end

        def inspect
          "#<#{self.class} dimensions=#{@dimensions.inspect} axes=#{@axes.inspect} " \
            "allow_drop=#{@allow_drop}>"
        end
      end
    end
  end
end
