# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Cost model for WKB exchange packets
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # The planner minimizes the total of:
      #
      # * a split cost for every additional packet opened for a reader,
      # * a per-packet fixed cost,
      # * a per-byte cost,
      # * rewrite costs for atoms that cannot be forwarded byte-for-byte.
      #
      # Costs may be numbers or callables. Callables receive the Reader (and the
      # byte count for per-byte costs).
      class CostModel
        attr_reader :rewrite_cost, :dimension_change_cost,
                    :srid_change_cost, :dialect_change_cost

        def initialize(split_cost: 0, rewrite_cost: 0, dimension_change_cost: nil,
                       srid_change_cost: nil, dialect_change_cost: nil,
                       per_packet: 0, per_byte: 0)
          @split_cost = split_cost
          @rewrite_cost = rewrite_cost
          @dimension_change_cost = dimension_change_cost.nil? ? rewrite_cost : dimension_change_cost
          @srid_change_cost = srid_change_cost.nil? ? rewrite_cost : srid_change_cost
          @dialect_change_cost = dialect_change_cost.nil? ? rewrite_cost : dialect_change_cost
          @per_packet = per_packet
          @per_byte = per_byte
        end

        def split(reader)
          evaluate(@split_cost, reader)
        end

        def packet_fixed(reader = nil)
          evaluate(@per_packet, reader)
        end

        def packet_bytes(bytes, reader = nil)
          evaluate(@per_byte, reader) * bytes
        end

        def rewrite(dimensions_changed: false, srid_changed: false, dialect_changed: false,
                    axis_remapped: false)
          cost = 0
          cost += @dimension_change_cost if dimensions_changed
          cost += @srid_change_cost if srid_changed
          cost += @dialect_change_cost if dialect_changed
          cost += @rewrite_cost if axis_remapped && !dimensions_changed && !dialect_changed
          cost
        end

        private

        def evaluate(spec, *args)
          return spec.call(*args.compact) if spec.respond_to?(:call)
          spec
        end
      end
    end
  end
end
