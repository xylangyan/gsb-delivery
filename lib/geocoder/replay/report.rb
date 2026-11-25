require 'json'
require 'geocoder/replay/analysis'
require 'geocoder/replay/engine'
require 'geocoder/replay/errors'
require 'geocoder/replay/scenario'

module Geocoder
  module Replay
    ##
    # Report formatting for the local review entrypoint.
    ##
    class Report
      def initialize(analysis)
        @analysis = analysis
        @scenario = analysis.scenario
        @engine = analysis.engine
        @composite = nil
      end

      attr_writer :composite

      def to_s
        lines = []
        lines << "scenario: #{@scenario.source || '(in memory)'}"
        process_ids =
          if @composite&.independent?
            @composite.scenario.processes.keys
          else
            @scenario.processes.keys
          end
        lines << "processes: #{process_ids.join(', ')}"
        scenario = @composite&.independent? ? @composite.scenario : @scenario
        lines << "slots: #{scenario.slots.map { |slot| slot[:id] }.join(', ')}"
        lines << "bodies: #{scenario.bodies.keys.sort.join(', ')}"
        if @composite&.independent?
          lines << "components: #{@composite.components.map { |c| c.slot_ids.join('+') }.join(' | ')}"
        end
        lines << ''
        lines.concat(order_section)
        lines << ''
        lines.concat(trace_section)
        lines << ''
        lines.concat(provenance_section)
        lines << ''
        lines.concat(ambiguity_section)
        lines << ''
        lines.concat(status_section)
        lines.join("\n")
      end

      private

      def order_section
        return component_order_section if @composite&.independent?

        lines = []
        if @analysis.synthesized?
          lines << "synthesized order: #{@engine.order.map { |slot, op| "#{slot}:#{op + 1}" }.join(' ')}"
          lines << 'synthesized order result: consistent'
        elsif @analysis.declared?
          lines << "declared order: #{@engine.order.map { |slot, op| "#{slot}:#{op + 1}" }.join(' ')}"
          lines << "declared order result: #{@engine.ok? ? 'consistent' : 'INCONSISTENT'}"
        else
          lines << 'declared order: (none; no consistent order was found)'
        end
        lines << ''
        lines << 'consistent orders:'
        solve = @analysis.solve
        if solve.orders.empty?
          lines << "  none (visited #{solve.nodes} nodes" \
                   "#{solve.exhausted ? ', exhausted' : ', stopped at bound'})"
          if solve.closest && !solve.closest.empty?
            lines << "  closest branch: #{solve.closest.map { |slot, op| "#{slot}:#{op + 1}" }.join(' ')}"
          end
        else
          solve.orders.each_with_index do |order, index|
            lines << "  #{index + 1}. #{order.map { |slot, op| "#{slot}:#{op + 1}" }.join(' ')}"
          end
          lines << "  (visited #{solve.nodes} nodes" \
                   "#{solve.exhausted ? ', exhausted' : ', stopped at bound'})"
        end
        lines
      end

      def component_order_section
        lines = ['independent components (solved separately):']
        @composite.components.each do |component|
          analysis = component.analysis
          lines << "  component #{component.id} (slots #{component.slot_ids.join(', ')}):"
          lines << "    declared: #{analysis.declared? ? analysis.engine.order.map { |s, o| "#{s}:#{o + 1}" }.join(' ') : '(none)'}"
          lines << "    declared result: #{analysis.declared_ok? ? 'consistent' : 'INCONSISTENT'}"
          solve = analysis.solve
          if solve.orders.empty?
            lines << "    consistent orders: none (visited #{solve.nodes} nodes" \
                     "#{solve.exhausted ? ', exhausted' : ', stopped at bound'})"
            failure = analysis.failure
            if failure
              lines << "    earliest unexplained step: #{failure[:step]} (#{failure[:kind]})"
              lines << "    confirmed prefix: #{failure[:prefix].join(' ')}"
            end
            unreachable = analysis.unreachable
            if unreachable && !failure
              lines << "    deepest explained prefix: #{unreachable[:prefix].join(' ')}"
              lines << "    first step no branch could execute: #{unreachable[:step] || '(none)'}"
            end
          else
            solve.orders.first(3).each do |order|
              lines << "    order: #{order.map { |s, o| "#{s}:#{o + 1}" }.join(' ')}"
            end
            lines << "    #{solve.orders.size} consistent order(s) found" \
                     "#{solve.exhausted ? ' (exhausted)' : ' (bounded)'}"
          end
        end
        if @composite.consistent?
          lines << "  global combinations: #{@composite.product_count} " \
                   '(product of per-component counts; no global interleaving is required)'
        else
          lines << '  global combinations: 0 (at least one component has no consistent order)'
        end
        lines
      end

      def analyses
        @composite&.independent? ? @composite.components.map(&:analysis) : [@analysis]
      end

      def trace_section
        lines = ['step log:']
        if @composite&.independent?
          @composite.components.each do |component|
            lines << "  -- component #{component.id} --"
            component.analysis.engine.events.each do |event|
              lines << format('  %02d %-10s %s', event.sequence, event.step, event.message)
            end
          end
        else
          @analysis.engine.events.each do |event|
            lines << format('  %02d %-10s %s', event.sequence, event.step, event.message)
          end
        end
        lines
      end

      def provenance_section
        lines = ['result provenance:']
        records = analyses.flat_map { |analysis| analysis.engine.results }
        if records.empty?
          lines << '  (no result operations)'
        else
          records.each do |record|
            lines << format(
              '  %-8s %-24s candidate %-2d body=%-14s coords=%-24s cache_hit=%s',
              record[:step], record[:query].inspect, record[:candidate],
              record[:body], record[:coordinates].inspect, record[:marker].inspect
            )
          end
        end
        lines << ''
        lines << 'cache operations:'
        trace = analyses.flat_map { |analysis| analysis.engine.cache_trace }
        if trace.empty?
          lines << '  (none)'
        else
          trace.each do |event|
            lines << format('  %-8s %-6s %-48s %s', event[:process], event[:op], event[:key], event[:body])
          end
        end
        lines
      end

      def ambiguity_section
        focus = nil
        analyses.each do |analysis|
          focus = analysis.alternative_sources
          break if focus
        end
        return ['focused candidate: (none)'] if focus.nil?

        lines = ['focused candidate:']
        lines << "  step: #{focus[:step]}"
        lines << "  query: #{focus[:query].inspect}"
        lines << "  coordinates: #{focus[:coordinates].inspect}"
        lines << "  reported body: #{focus[:reported_body] || '(unknown)'}"
        lines << "  reported cache_hit: #{focus[:reported_marker].inspect}"
        lines << "  bodies with the same candidate at the same index: #{focus[:same_index_bodies].join(', ')}"
        if focus[:unique]
          lines << '  attribution: unique among the available bodies'
        else
          lines << "  attribution: NOT unique; other bodies explain it: #{focus[:other_matches].join(', ')}"
          if focus[:other_bodies_containing_coordinates].any?
            lines << "  also contain these coordinates at another index: " \
                     "#{focus[:other_bodies_containing_coordinates].join(', ')}"
          end
        end
        lines
      end

      def status_section
        return component_status_section if @composite&.independent?

        failure = @analysis.failure
        lines = ['status:']
        if failure
          lines << "  the declared order cannot be replayed"
          lines << "  earliest unexplained step: #{failure[:step]} (#{failure[:kind]})"
          lines << "  last confirmed step: #{failure[:last_successful] || '(none)'}"
          lines << "  confirmed prefix: #{failure[:prefix].join(' ')}"
          lines << "  detail: #{failure[:message]}"
        elsif @analysis.synthesized?
          lines << '  no order was declared; a consistent order was synthesized and replayed'
        elsif @analysis.declared_ok?
          lines << '  declared order is consistent'
        elsif @analysis.declared?
          lines << '  declared order could not be run'
        else
          lines << '  no declared order and no consistent order could be synthesized'
        end
        if @analysis.declared? && !@analysis.consistent?
          unreachable = @analysis.unreachable
          lines << '  no consistent global order exists'
          if unreachable
            lines << "  deepest explained prefix: #{unreachable[:prefix].join(' ')}"
            lines << "  first step no branch could execute: #{unreachable[:step] || '(none)'}" \
                     "#{unreachable[:kind] ? " (#{unreachable[:kind]})" : ''}"
            lines << "  detail: #{unreachable[:message]}" if unreachable[:message]
          end
        end
        lines
      end

      def component_status_section
        lines = ['status:']
        failures = @composite.failures
        if failures.empty? && @composite.consistent?
          lines << '  every component has a consistent order'
          lines << "  global combinations: #{@composite.product_count}"
          unless @composite.components.all? { |c| c.analysis.declared? }
            lines << '  at least one component had no declared order; a consistent order was synthesized'
          end
        elsif failures.any?
          lines << '  no global order: one or more components cannot be replayed'
          failures.each do |failure|
            lines << "  component #{failure[:component]}: earliest unexplained step #{failure[:step]} " \
                     "(#{failure[:kind]}), confirmed prefix: #{failure[:prefix].join(' ')}"
            unreachable = @composite.components.find { |c| c.id == failure[:component] }.analysis.unreachable
            if unreachable
              lines << "    first step no branch could execute: #{unreachable[:step] || '(none)'}"
            end
          end
          @composite.components.each do |component|
            analysis = component.analysis
            next if failures.any? { |failure| failure[:component] == component.id }

            lines << "  component #{component.id}: #{analysis.consistent? ? 'consistent' : 'has no consistent order'}"
          end
        else
          lines << '  no global order: the declared per-component orders fail and no alternative was found'
          @composite.components.each do |component|
            analysis = component.analysis
            lines << "  component #{component.id}: #{analysis.consistent? ? 'consistent' : 'has no consistent order'}"
          end
        end
        lines
      end
    end
  end
end
