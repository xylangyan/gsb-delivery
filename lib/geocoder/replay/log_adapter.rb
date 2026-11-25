require 'geocoder/replay/errors'

module Geocoder
  module Replay
    ##
    # Converts a flat event log into the slot/operation scenario shape.
    #
    # Real audit material usually arrives as one ordered list of events with a
    # slot id and a sequence number, not as pre-grouped operations. This adapter
    # accepts that form:
    #
    #   {
    #     "bodies": { ... },
    #     "processes": [ { "id": "p1", "cache": { ... } } ],
    #     "events": [
    #       { "slot": "s1", "seq": 1, "type": "query",  "query": "Main St" },
    #       { "slot": "s2", "seq": 2, "type": "response", "query": "Main St", "body": "new" },
    #       { "slot": "s1", "seq": 3, "type": "result", "candidate": 0,
    #         "cache_hit": true, "coordinates": [1.0, 2.0] }
    #     ]
    #   }
    #
    # Event types accepted: query/search/fetch, response/receive/arrival,
    # result/wrap, cache_delete/delete/expire, cache_write/write, note.
    #
    # The global array order (or `seq` when every event has one) becomes the
    # declared order. Per-slot order is preserved and validated by the engine,
    # so an event log whose slot order contradicts itself is rejected.
    #
    class LogAdapter
      TYPE_MAP = {
        'query' => 'search',
        'search' => 'search',
        'fetch' => 'fetch',
        'response' => 'respond',
        'receive' => 'respond',
        'arrival' => 'respond',
        'respond' => 'respond',
        'result' => 'result',
        'wrap' => 'result',
        'cache_delete' => 'cache_delete',
        'delete' => 'cache_delete',
        'expire' => 'cache_delete',
        'cache_write' => 'cache_write',
        'write' => 'cache_write',
        'note' => 'note'
      }.freeze

      def self.apply(data)
        return data unless data.is_a?(Hash) && data['events'].is_a?(Array)

        new(data).to_scenario_data
      end

      def initialize(data)
        @data = data
        @events = data['events']
      end

      def to_scenario_data
        ordered = ordered_events
        slots = ordered.each_with_object({}) { |event, out| (out[slot_id(event)] ||= []) << event }
        slot_order = slots.keys
        operations = slots.transform_values do |events|
          events.map { |event| operation_for(event) }
        end

        result = {
          'bodies' => @data['bodies'] || @data['responses'] || {},
          'processes' => processes,
          'slots' => slot_order.map do |id|
            {
              'id' => id,
              'process' => process_for(slots.fetch(id).first),
              'description' => slots.fetch(id).first['description'],
              'operations' => operations.fetch(id)
            }
          end
        }
        counters = Hash.new(0)
        result['order'] = ordered.map do |event|
          slot = slot_id(event)
          counters[slot] += 1
          "#{slot}:#{counters[slot]}"
        end
        result['groups'] = @data['groups'] if @data['groups']
        result['focus'] = @data['focus'] if @data['focus']
        result
      end

      private

      def ordered_events
        events = @events.each_with_index.map do |raw, position|
          event = stringify(raw)
          event['_position'] = position
          event
        end
        if events.all? { |event| event.key?('seq') }
          events.sort_by { |event| [Integer(event['seq']), event['_position']] }
        else
          events.sort_by { |event| event['_position'] }
        end
      rescue ArgumentError, TypeError
        raise InputError, 'events: seq must be an integer'
      end

      def slot_id(event)
        id = event['slot'] || event['track'] || event['work_slot']
        raise InputError, "event #{event['_position']}: missing slot" if id.nil? || id.to_s.empty?

        id.to_s
      end

      def process_for(event)
        id = event['process'] || event['worker'] || event['pid']
        if id.nil? || id.to_s.empty?
          raise InputError, "event #{event['_position']}: missing process"
        end
        id.to_s
      end

      def operation_for(event)
        type = (event['type'] || event['op'] || event['kind']).to_s
        op = TYPE_MAP.fetch(type) do
          raise InputError, "event #{event['_position']}: unknown type #{type.inspect}"
        end

        operation = { 'op' => op }
        operation['query'] = event['query'] || event['q'] if event.key?('query') || event.key?('q')
        operation['body'] = event['body_id'] || event['body'] if event.key?('body_id') || event.key?('body')
        operation['key'] = event['key'] if event.key?('key')
        operation['cache_hit'] = event['cache_hit'] if event.key?('cache_hit')
        operation['candidate'] = event['candidate'] if event.key?('candidate')
        operation['coordinates'] = event['coordinates'] || event['coords'] if event.key?('coordinates') || event.key?('coords')
        operation['address'] = event['address'] if event.key?('address')
        operation['id'] = event['id'].to_s if event.key?('id') && op == 'fetch'
        operation['message'] = event['message'] if event.key?('message')
        operation['evidence'] = event['evidence'] if event.key?('evidence')

        # Respond events keyed by body only are valid; everything else needs a
        # target it can be bound to.
        if %w[search respond cache_delete cache_write result].include?(op)
          if op != 'result' && !operation.key?('query') && !operation.key?('key')
            raise InputError,
                  "event #{event['_position']} (#{type}): needs query or key"
          end
          if op == 'respond' && !operation.key?('body')
            raise InputError, "event #{event['_position']} (response): needs body"
          end
          if op == 'cache_write' && !operation.key?('body')
            raise InputError, "event #{event['_position']} (cache_write): needs body"
          end
        end
        operation
      end

      def processes
        declared = @data['processes']
        if declared.nil?
          ids = @events.map { |raw| process_for(stringify(raw)) }.uniq
          return ids.map do |id|
            { 'id' => id, 'prefix' => @data['prefix'] || 'geocoder:', 'initial_cache' => @data['cache'] || {} }
          end
        end

        declared.map do |raw|
          process = stringify(raw)
          process['initial_cache'] ||= process['cache'] || {}
          process
        end
      end

      def stringify(hash)
        (hash || {}).each_with_object({}) do |(key, value), out|
          out[key.to_s] = value.is_a?(Hash) ? stringify(value) : value
        end
      end
    end
  end
end
