require 'json'
require 'optparse'
require 'geocoder/replay/analysis'
require 'geocoder/replay/composite'
require 'geocoder/replay/engine'
require 'geocoder/replay/errors'
require 'geocoder/replay/report'
require 'geocoder/replay/scenario'

module Geocoder
  module Replay
    ##
    # Local review entrypoint. No network access: every response comes from
    # the scenario's in-memory bodies.
    ##
    module CLI
      module_function

      def run(argv, out = STDOUT, err = STDERR)
        options = { format: 'text', max_orders: Analysis::MAX_ORDERS, max_nodes: Analysis::MAX_NODES }
        parser = OptionParser.new do |opts|
          opts.banner = 'Usage: ruby bin/geocoder-replay SCENARIO [--order O] [--format text|json]'
          opts.separator ''
          opts.separator 'Replays a recorded cache/response/work-slot scenario with Geocoder.'
          opts.separator 'Every response is local; no geocoding service is contacted.'
          opts.separator ''
          opts.on('--order LIST', 'Comma-separated slot:operation steps (1-based)') do |list|
            options[:order] = list.split(',').map(&:strip)
          end
          opts.on('--format FORMAT', %w[text json], 'Output format (text or json)') do |format|
            options[:format] = format
          end
          opts.on('--max-orders N', Integer, 'Maximum consistent orders to collect') do |n|
            options[:max_orders] = n
          end
          opts.on('--max-nodes N', Integer, 'Search node bound') do |n|
            options[:max_nodes] = n
          end
          opts.on('--reproduce', 'Run only the recorded prefix needed for the focus, and print it') do
            options[:reproduce] = true
          end
          opts.on('--focus SLOT:CANDIDATE', 'Candidate to explain (overrides scenario focus)') do |value|
            slot, candidate = value.split(':', 2)
            raise InputError, 'focus must look like SLOT:CANDIDATE' unless slot && candidate

            options[:focus] = { slot: slot, candidate: Integer(candidate) }
          end
          opts.on('-h', '--help', 'Show this help') do
            out.puts opts
            return 0
          end
        end

        args = parser.parse(argv)
        path = args.shift
        unless path
          err.puts parser
          return 2
        end

        loaded = Scenario.load(path)
        scenario = loaded
        scenario = Scenario.new(reorder_data(scenario, options[:order]), source: loaded.source) if options[:order]
        scenario = Scenario.new(with_focus(scenario, options[:focus]), source: loaded.source) if options[:focus]

        composite = CompositeAnalysis.new(
          scenario, max_orders: options[:max_orders], max_nodes: options[:max_nodes]
        ).run
        analysis = composite.components.first.analysis

        if options[:format] == 'json'
          payload =
            if composite.independent?
              composite_json(composite)
            else
              json_report(analysis)
            end
          if options[:reproduce]
            payload = payload.merge('reproduction' => reproduction_replay(scenario, composite))
          end
          out.puts JSON.pretty_generate(payload)
        else
          report = Report.new(analysis)
          report.composite = composite
          text = report.to_s.dup
          if options[:reproduce]
            focus = scenario.focus
            if focus.nil?
              first_result = composite.independent? ? composite.components.first.analysis.engine.results.first : analysis.engine.results.first
              if first_result
                focus = { slot: first_result[:step].split(':').first, candidate: first_result[:candidate] }
              end
            end
            text += "\n\n" + reproduction_text(scenario, focus: focus)
          end
          out.puts text
        end
        ok = composite.independent? ? composite.replayable? : (analysis.declared_ok? || analysis.synthesized?)
        ok ? 0 : 1
      rescue InputError => e
        err.puts "input error: #{e.message}"
        2
      rescue Violation => e
        err.puts "replay violation: #{e.message}"
        3
      end

      ##
      # The scenario cut down to the operations needed to reach the focus, in
      # the declared order. It still uses in-memory bodies only.
      ##
      def reproduction(scenario, focus: nil)
        focus ||= scenario.focus
        return { 'note' => 'no focus declared' } unless focus
        order = scenario.order
        return { 'note' => 'no declared order; pass --order' } unless order

        cut = nil
        order.each_with_index do |(slot_id, index), position|
          next unless slot_id == focus[:slot]

          operation = scenario.slot(slot_id)[:operations][index]
          next unless operation[:op] == :result && operation[:candidate] == focus[:candidate]

          cut = position
          break
        end
        target = "#{focus[:slot]} candidate #{focus[:candidate]}"
        return { 'note' => "focus #{target} does not appear in the declared order" } unless cut

        steps = order.take(cut + 1)
        max_index = Hash.new(-1)
        steps.each do |slot_id, index|
          max_index[slot_id] = index if index > max_index[slot_id]
        end

        data = scenario.to_data
        data['slots'] = data['slots'].filter_map do |slot|
          limit = max_index[slot['id']]
          next nil if limit < 0

          slot.merge('operations' => slot['operations'].take(limit + 1))
        end
        data['order'] = steps.map { |slot_id, index| "#{slot_id}:#{index + 1}" }
        data.delete('focus')
        {
          'focus' => target,
          'steps' => data['order'],
          'bodies' => data['bodies'].values.map { |body| body['body'] },
          'scenario' => data
        }
      end

      ##
      # Build and actually run the minimal scenario, returning the trace so a
      # duty operator can see the interleaving without the geocoding service.
      ##
      def reproduction_replay(scenario, composite)
        data = reproduction(scenario)
        return data if data['note']

        mini = Scenario.new(data['scenario'], source: '(reproduction)')
        result = CompositeAnalysis.new(mini).run
        chosen = result.independent? ? result.components.first.analysis : result.components.first.analysis
        data.merge(
          'consistent' => result.consistent?,
          'events' => chosen.engine.events.map do |event|
            { sequence: event.sequence, step: event.step, kind: event.kind, message: event.message }
          end,
          'provenance' => chosen.engine.provenance
        )
      end

      def reproduction_text(scenario, focus: nil)
        data = reproduction(scenario, focus: focus)
        return "reproduction: #{data['note']}" if data['note']

        lines = ['reproduction (in memory, no geocoding service):']
        lines << "  focus: #{data['focus']}"
        lines << "  steps: #{data['steps'].join(' ')}"
        lines << "  bodies used: #{data['bodies'].size}"
        lines << '  run: ruby bin/geocoder-replay --format json --order \'' +
                 data['steps'].join(',') + '\' <scenario>'
        lines.join("\n")
      end

      def serializable(value)
        case value
        when Hash
          value.each_with_object({}) do |(key, item), out|
            out[key] = serializable(item)
          end
        when Array
          value.map { |item| serializable(item) }
        when Body
          value.to_h
        else
          value
        end
      end

      def composite_json(composite)
        {
          scenario: composite.scenario.source,
          independent: true,
          components: composite.components.map do |component|
            json_report(component.analysis).merge(
              component: component.id,
              slot_ids: component.slot_ids,
              process_ids: component.process_ids
            )
          end,
          consistent: composite.consistent?,
          product_count: composite.product_count
        }
      end

      def json_report(analysis)
        engine = analysis.engine
        {
          scenario: analysis.scenario.source,
          processes: analysis.scenario.processes.keys,
          slots: analysis.scenario.slots.map { |slot| slot[:id] },
          bodies: analysis.scenario.bodies.transform_values(&:to_h),
          declared_order: analysis.declared? ? engine.order.map { |slot, op| "#{slot}:#{op + 1}" } : nil,
          declared_consistent: analysis.declared_ok?,
          synthesized: analysis.synthesized?,
          events: engine.events.map do |event|
            {
              sequence: event.sequence,
              step: event.step,
              kind: event.kind,
              message: event.message,
              data: serializable(event.data)
            }
          end,
          violations: engine.violations,
          provenance: engine.provenance,
          cache_trace: engine.cache_trace,
          final_cache: engine.runtimes.transform_values do |runtime|
            runtime.cache.raw_entries.transform_values do |raw|
              body = analysis.scenario.registry.find_by_raw(raw)
              body ? body.id : "(unregistered)"
            end
          end,
          pending_responses: engine.runtimes.transform_values do |runtime|
            runtime.queue.map { |pending| pending.to_h }
          end,
          focus: analysis.alternative_sources,
          failure: analysis.failure,
          consistent_orders: analysis.orders.map do |order|
            order.map { |slot, op| "#{slot}:#{op + 1}" }
          end,
          solved: {
            nodes: analysis.solve.nodes,
            exhausted: analysis.solve.exhausted,
            bound: analysis.solve.bound
          },
          unreachable: analysis.unreachable
        }
      end

      ##
      # Rebuild a scenario's raw data with an explicit order. Operation strings
      # are 1-based ("s1:1"); Scenario parses them back into zero-based pairs.
      ##
      def reorder_data(scenario, order)
        data = scenario.to_data
        data['order'] = order
        data
      end

      def with_focus(scenario, focus)
        data = scenario.to_data
        data['focus'] = { 'slot' => focus[:slot], 'candidate' => focus[:candidate] }
        data
      end
    end
  end
end
