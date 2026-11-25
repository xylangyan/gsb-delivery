require 'geocoder/replay/errors'
require 'geocoder/replay/engine'
require 'geocoder/replay/solver'

module Geocoder
  module Replay
    ##
    # Analysis of one scenario:
    #
    #   * run the declared order (if any) and report every step
    #   * find all consistent interleavings, bounded
    #   * if none exists, name the earliest confirmed prefix and the step that
    #     first cannot be explained
    #   * for the focused candidate, list which bodies could explain it and
    #     whether the order makes the attribution unique
    #
    class Analysis
      MAX_ORDERS = 12
      MAX_NODES = 60_000

      attr_reader :scenario, :engine, :solver_result

      def initialize(scenario, max_orders: MAX_ORDERS, max_nodes: MAX_NODES, order: nil)
        @scenario = scenario
        @max_orders = max_orders
        @max_nodes = max_nodes
        @declared_order = order
      end

      def run
        @engine = Engine.new(@scenario)
        @declared = @declared_order || @scenario.order
        if @declared
          @engine.run(@declared)
        else
          solution = solve.orders.first
          if solution
            @synthesized = solution
            @engine.run(solution)
          end
        end
        self
      end

      def synthesized?
        !@synthesized.nil?
      end

      def declared?
        !@declared.nil?
      end

      def declared_ok?
        declared? && @engine&.ok?
      end

      def solve
        @solver_result ||= Solver.new(
          @scenario, max_orders: @max_orders, max_nodes: @max_nodes
        ).solve
      end

      def consistent?
        orders.any?
      end

      def orders
        solve.orders
      end

      ##
      # The declared order's successful prefix. When the order replays, it is
      # the whole order. When it fails, it stops at the last step that executed.
      # A synthesized order is already consistent, so it is its own prefix.
      ##
      def confirmed_prefix
        return @engine.order.dup if synthesized?
        return [] unless declared?
        return @engine.order.dup if declared_ok?

        last = @engine.violations.first&.dig(:last_successful)
        return [] if last.nil?

        index = @engine.order.index { |slot, operation| label(slot, operation) == last }
        index ? @engine.order.take(index + 1) : []
      end

      def failure
        return nil if synthesized?
        return nil if declared_ok?
        return nil unless declared?

        violation = @engine.violations.first
        return nil if violation.nil?

        {
          step: violation[:step],
          kind: violation[:kind],
          message: violation[:message],
          last_successful: violation[:last_successful],
          prefix: confirmed_prefix.map { |slot, operation| step_label(slot, operation) }
        }
      end

      ##
      # When no global order exists at all, report the deepest point any
      # explored branch reached and the step it could not execute next.
      ##
      def unreachable
        return nil if consistent?

        solve_result = solve
        if solve_result.orders.empty? && solve_result.nodes.zero? && solve_result.closest.nil?
          return { prefix: [], step: nil, kind: nil,
                   message: 'no branch could execute even the first operation' }
        end
        closest = solve_result.closest
        return nil if closest.nil?

        error = solve_result.closest_error
        next_pair = solve_result.closest_next
        next_step =
          if next_pair
            @engine.steps.find do |step|
              step.slot[:id] == next_pair[0].to_s && step.index == next_pair[1]
            end
          end
        {
          prefix: closest.map { |slot, operation| step_label(slot, operation) },
          step: next_step&.label,
          kind: error&.kind,
          message: error&.message,
          closest_length: closest.size
        }
      end

      ##
      # Sources that could have produced the focused result's coordinates.
      ## A body qualifies when it parses and the requested candidate exists
      ## with the reported coordinate pair.
      ##
      def alternative_sources(focus = @scenario.focus, engine: @engine)
        return nil if focus.nil?

        record = engine.results.find do |item|
          item[:step].start_with?("#{focus[:slot]}:") && item[:candidate] == focus[:candidate]
        end
        return nil if record.nil?

        coordinates = record[:coordinates]
        chosen = @scenario.body(record[:body]) if record[:body] && @scenario.registry.include?(record[:body])
        matches = @scenario.bodies.values.select do |body|
          next false unless body.parse_error.nil?

          body.candidates.any? { |candidate| candidate_coordinates(candidate) == coordinates }
        end
        # Candidate-index attribution: a body explains this result only if the
        # candidate at the same index also has the reported coordinates. This
        # is stricter than "contains these coordinates somewhere".
        same_index = matches.select do |body|
          body.candidates.size > record[:candidate] &&
            candidate_coordinates(body.candidates[record[:candidate]]) == coordinates
        end

        {
          step: record[:step],
          query: record[:query],
          candidate: record[:candidate],
          coordinates: coordinates,
          reported_body: chosen&.id,
          reported_marker: record[:marker],
          possible_bodies: matches.map(&:id),
          same_index_bodies: same_index.map(&:id),
          unique: same_index.size == 1 && chosen && same_index.first.id == chosen.id,
          other_matches: same_index.reject { |body| chosen && body.id == chosen.id }.map(&:id),
          other_bodies_containing_coordinates: (matches - same_index).reject { |body| chosen && body.id == chosen.id }.map(&:id)
        }
      end

      def step_label(slot_id, operation_index)
        "#{slot_id}:#{operation_index + 1}"
      end

      def candidate_coordinates(candidate)
        result = Geocoder::Result::Nominatim.new(candidate)
        result.coordinates
      rescue StandardError
        []
      end

    end
  end
end
