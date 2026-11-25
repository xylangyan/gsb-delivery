require 'geocoder/replay/analysis'
require 'geocoder/replay/errors'

module Geocoder
  module Replay
    ##
    # Analysis across independent components.
    #
    # Slots of one process are always coupled (one lookup, one @cache_hit
    # marker). Components are independent when their processes have separate
    # caches and no operation crosses between them. Each component is solved in
    # isolation; the global order then exists iff every component has at least
    # one consistent order, and its number of combinations is the product of
    # the component counts. A global enumeration is never required.
    #
    class CompositeAnalysis
      Component = Struct.new(:id, :process_ids, :slot_ids, :scenario, :analysis, keyword_init: true)

      attr_reader :scenario, :components

      def initialize(scenario, max_orders: Analysis::MAX_ORDERS, max_nodes: Analysis::MAX_NODES)
        @scenario = scenario
        @max_orders = max_orders
        @max_nodes = max_nodes
        @global_order = scenario.order
        @components = scenario.components.map do |component|
          subset = scenario.subset(
            component[:process_ids], shared_cache: component[:shared_cache]
          )
          Component.new(
            id: component[:id],
            process_ids: component[:process_ids],
            slot_ids: component[:slot_ids],
            scenario: subset,
            analysis: nil
          )
        end
      end

      def independent?
        @components.size > 1
      end

      def order_for(component)
        @global_order&.select { |slot_id, _| component.slot_ids.include?(slot_id) }
      end

      def run
        @components.map! do |component|
          Component.new(
            id: component.id,
            process_ids: component.process_ids,
            slot_ids: component.slot_ids,
            scenario: component.scenario,
            analysis: Analysis.new(
              component.scenario,
              max_orders: @max_orders,
              max_nodes: @max_nodes,
              order: order_for(component)
            ).run
          )
        end
        self
      end

      def consistent?
        @components.all? { |component| component.analysis.solve.orders.any? }
      end

      def product_count
        return 0 unless consistent?

        @components.inject(1) { |product, component| product * component.analysis.orders.size }
      end

      def declared_ok?
        @components.all? { |component| component.analysis.declared_ok? }
      end

      def replayable?
        @components.all? do |component|
          analysis = component.analysis
          analysis.declared_ok? || analysis.synthesized?
        end
      end

      def failures
        @components.filter_map do |component|
          failure = component.analysis.failure
          next nil unless failure

          failure.merge(component: component.id)
        end
      end

    end
  end
end
