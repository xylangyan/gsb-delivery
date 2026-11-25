require 'geocoder/replay/errors'
require 'geocoder/replay/engine'

module Geocoder
  module Replay
    ##
    # Depth-first search for global interleavings consistent with the recorded
    # material.
    #
    # The search branches over which slot supplies the next operation, taking
    # each slot's operations in order. After every step it checkpoints all
    # process runtimes; a step that Geocoder cannot explain rolls back to the
    # checkpoint and that branch is abandoned immediately. Because the state is
    # rolled back, the number of visited nodes is proportional to real
    # conflicts, not to the factorial number of linear extensions.
    #
    # When you ask only whether a consistent order exists (find_first), the
    # first success wins. Otherwise up to max_orders are collected.
    #
    class Solver
      Result = Struct.new(
        :orders, :nodes, :exhausted, :bound, :closest, :closest_error, :closest_next, keyword_init: true
      )

      def initialize(scenario, max_orders: 12, max_nodes: 200_000, find_first: false)
        @scenario = scenario
        @max_orders = max_orders
        @max_nodes = max_nodes
        @find_first = find_first
      end

      def solve
        @engine = Engine.new(@scenario)
        @steps = @engine.steps
        @by_slot = @steps.group_by { |step| step.slot[:id] }
        @slot_order = @scenario.slots.map { |slot| slot[:id] }
        @positions = Hash.new(0)
        @order = []
        @solutions = []
        @nodes = 0
        @exhausted = true
        @closest = nil
        @closest_error = nil
        @closest_next = nil
        search
        Result.new(
          orders: @solutions,
          nodes: @nodes,
          exhausted: @exhausted,
          bound: @max_nodes,
          closest: @closest,
          closest_error: @closest_error,
          closest_next: @closest_next
        )
      end

      private

      def search
        if @order.size == @steps.size
          @solutions << @order.dup
          @closest = @order.dup
          return :done if @find_first || @solutions.size >= @max_orders

          return nil
        end

        if @nodes >= @max_nodes
          @exhausted = false
          return :bound
        end

        @slot_order.each do |slot_id|
          position = @positions[slot_id]
          next if position >= @by_slot.fetch(slot_id).size

          checkpoint = @engine.checkpoint
          error = @engine.step_once(slot_id, position)
          if error
            @engine.rollback(checkpoint)
            record_closest(error, slot_id, position) if @closest.nil? || @order.size > @closest.size
            next
          end

          @nodes += 1
          @positions[slot_id] += 1
          @order << [slot_id, position]
          outcome = search
          @order.pop
          @positions[slot_id] -= 1
          @engine.rollback(checkpoint)

          return :done if outcome == :done && (@find_first || @solutions.size >= @max_orders)
          return :bound if outcome == :bound
        end
        nil
      end

      def record_closest(error, slot_id, position)
        @closest = @order.dup
        @closest_error = error
        @closest_next = [slot_id, position]
      end
    end
  end
end
