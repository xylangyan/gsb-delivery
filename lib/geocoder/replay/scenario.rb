require 'json'
require 'yaml'
require 'geocoder/replay/errors'
require 'geocoder/replay/body'
require 'geocoder/replay/log_adapter'

module Geocoder
  module Replay
    ##
    # Validated description of one replay input file (JSON or YAML).
    #
    # Top-level keys:
    #
    #   bodies     raw Nominatim responses, keyed by a body id
    #   processes  worker processes: cache prefix, lookup config, initial cache
    #   slots      work slots; each belongs to one process and has ordered ops
    #   order      optional global order, as ["slot:operation", ...] strings
    #   focus      optional slot/candidate the report must explain
    #
    # Slots on the same process share one lookup instance and one cache.
    # Operations inside a slot are ordered; the order between slots is the
    # interleaving a replay must reconstruct.
    #
    # See README_REPLAY.md for the operation reference.
    #
    class Scenario
      PROCESS_KEYS = %w[id label initial_cache config prefix cache_group].freeze
      GROUP_KEYS = %w[id processes description].freeze
      SLOT_KEYS = %w[id process operations description].freeze
      SEARCH_KEYS = %w[op query body returns options cache_hit results].freeze
      FETCH_KEYS = %w[op query body returns options id].freeze
      WRAP_KEYS = %w[
        op query fetch candidate body cache_hit coordinates address
        evidence expected options
      ].freeze
      DELETE_KEYS = %w[op key query options].freeze
      WRITE_KEYS = %w[op key query options body].freeze
      RESPOND_KEYS = %w[op query body options].freeze
      NOTE_KEYS = %w[op message].freeze
      OPERATIONS = {
        'search' => SEARCH_KEYS,
        'fetch' => FETCH_KEYS,
        'respond' => RESPOND_KEYS,
        'result' => WRAP_KEYS,
        'wrap' => WRAP_KEYS,
        'cache_delete' => DELETE_KEYS,
        'cache_write' => WRITE_KEYS,
        'note' => NOTE_KEYS
      }.freeze

      attr_reader :processes, :slots, :warnings, :source, :focus, :groups

      def self.load(path)
        raise InputError, "scenario not found: #{path}" unless File.file?(path)

        text = File.read(path)
        data =
          if path.downcase.end_with?('.json')
            JSON.parse(text)
          else
            require 'yaml'
            YAML.safe_load(text, permitted_classes: [Symbol], aliases: true)
          end
        new(data, source: path)
      rescue JSON::ParserError => e
        raise InputError, "cannot parse #{path}: #{e.message}"
      rescue Psych::Exception => e
        raise InputError, "cannot parse #{path}: #{e.message}"
      end

      def initialize(data, source: nil)
        @source = source
        @warnings = []
        @data = stringify(data)
        raise InputError, 'scenario root must be an object' unless @data.is_a?(Hash)
        @data = LogAdapter.apply(@data)

        @registry = BodyRegistry.new
        load_bodies(@data['bodies'] || @data['responses'] || {})
        @processes = load_processes(@data['processes'] || [])
        @slots = load_slots(@data['slots'] || @data['tracks'] || [])
        @focus = load_focus(@data['focus'])
        @groups = load_groups(@data['groups'] || [])
        validate_candidates!
        validate_groups!
      end

      def registry
        @registry
      end

      def bodies
        @registry.bodies
      end

      def body(id)
        @registry.fetch(id)
      end

      def process(id)
        @processes.fetch(id.to_s) { raise InputError, "unknown process #{id.inspect}" }
      end

      def slot(id)
        @slots.find { |item| item[:id] == id.to_s } or raise InputError, "unknown slot #{id.inspect}"
      end

      ##
      # Declared order as (slot id, zero-based operation index) pairs.
      ##
      def order
        raw = @data['order']
        return nil if raw.nil?
        raise InputError, 'order must be an array of "slot:operation" strings' unless raw.is_a?(Array)

        raw.map do |entry|
          case entry
          when String
            slot_id, operation = entry.split(':', 2)
            raise InputError, "bad order entry #{entry.inspect}" unless slot_id && operation

            [slot_id, Integer(operation, 10) - 1]
          when Hash
            entry = stringify(entry)
            [entry['slot'].to_s, Integer(entry['operation'] || entry['index'], 10) - 1]
          when Array
            [entry[0].to_s, Integer(entry[1])]
          else
            raise InputError, "bad order entry #{entry.inspect}"
          end
        rescue ArgumentError, TypeError
          raise InputError, "bad order entry #{entry.inspect}"
        end
      end

      ##
      # Components that can be replayed independently. Slots of one process
      # are always coupled: they share the lookup instance and its @cache_hit
      # marker. Processes are coupled only when they declare the same
      # cache_group; by default each process has its own cache.
      ##
      ##
      # Components that can be replayed independently. A component either
      # contains one process with its own cache, or a declared group whose
      # processes share one cache. Slots of one process are always coupled
      # because they share the lookup instance and its @cache_hit marker.
      ##
      def components
        if @groups.empty?
          groups = Hash.new { |hash, key| hash[key] = [] }
          @processes.each_value { |process| groups[process[:cache_group]] << process[:id] }
          return groups.map do |group_id, process_ids|
            component(group_id, process_ids, shared_cache: false)
          end
        end

        @groups.map { |group| component(group[:id], group[:processes], shared_cache: true) }
      end

      ##
      # A scenario with the same bodies but only the named processes and their
      # slots. Used to solve independent components separately.
      #
      # When shared_cache is true the selected processes are collapsed into one
      # replay process, so the group really has a single cache and a single
      # lookup instance. Their slot ids stay distinct and their logs keep their
      # own order.
      ##
      def subset(process_ids, shared_cache: false)
        ids = process_ids.map(&:to_s)
        data = to_data
        data.delete('groups')
        if shared_cache && ids.size > 1
          data = collapse_shared_cache(data, ids)
        end
        data['processes'] = data['processes'].select { |process| ids.include?(process['id']) }
        data['slots'] = data['slots'].select { |slot| ids.include?(slot['process']) }
        data.delete('order')
        if @focus && data['slots'].any? { |slot| slot['id'] == @focus[:slot] }
          data['focus'] = { 'slot' => @focus[:slot], 'candidate' => @focus[:candidate] }
        else
          data.delete('focus')
        end
        Scenario.new(data, source: source)
      end

      def collapse_shared_cache(data, process_ids)
        survivor = data['processes'].find { |process| process['id'] == process_ids.first }
        merged_initial = process_ids.each_with_object({}) do |process_id, out|
          process = data['processes'].find { |item| item['id'] == process_id }
          (process['initial_cache'] || {}).each do |key, body_id|
            if out.key?(key) && out[key] != body_id
              raise InputError,
                    "group #{survivor['id']}: cache key #{key.inspect} is initialized to " \
                    "different bodies by processes sharing the cache"
            end
            out[key] = body_id
          end
        end
        data['processes'] = [
          survivor.merge(
            'id' => process_ids.first,
            'initial_cache' => merged_initial
          )
        ]
        data['slots'] = data['slots'].map do |slot|
          process_ids.include?(slot['process']) ? slot.merge('process' => process_ids.first) : slot
        end
        data
      end

      def to_data
        data = {
          'bodies' => bodies.transform_values do |body|
            { 'body' => body.raw, 'code' => body.code, 'headers' => body.headers }
          end,
          'processes' => @processes.values.map do |process|
            {
              'id' => process[:id],
              'label' => process[:label],
              'prefix' => process[:prefix],
              'cache_group' => process[:cache_group],
              'config' => process[:config].each_with_object({}) { |(key, value), out| out[key.to_s] = value },
              'initial_cache' => process[:initial_cache].transform_values(&:id)
            }
          end,
          'slots' => @slots.map do |slot|
            {
              'id' => slot[:id],
              'process' => slot[:process],
              'description' => slot[:description],
              'operations' => slot[:operations].map { |operation| operation_to_data(operation) }
            }
          end
        }
        order_strings = order&.map { |slot_id, index| "#{slot_id}:#{index + 1}" }
        data['order'] = order_strings if order_strings
        if @groups.any?
          data['groups'] = @groups.map do |group|
            { 'id' => group[:id], 'processes' => group[:processes] }
          end
        end
        data
      end

      def operation_to_data(operation)
        base = { 'op' => operation[:op].to_s }
        case operation[:op]
        when :search, :fetch
          base['query'] = operation[:query] if operation[:query]
          base['body'] = operation[:body].id if operation[:body]
          base['id'] = operation[:id] if operation[:id]
          base['cache_hit'] = operation[:cache_hit] unless operation[:cache_hit] == :unspecified
          base['results'] = operation[:results].map { |spec| result_to_data(spec) } if operation[:results]
        when :result
          base['query'] = operation[:query] if operation[:query]
          base['fetch'] = operation[:fetch] if operation[:fetch]
          base['candidate'] = operation[:candidate]
          base['body'] = operation[:body].id if operation[:body]
          base['cache_hit'] = operation[:cache_hit] unless operation[:cache_hit] == :unspecified
          base['coordinates'] = operation[:coordinates] if operation[:coordinates]
          base['address'] = operation[:address] if operation[:address]
          base['evidence'] = operation[:evidence] if operation[:evidence]
        when :respond
          base['query'] = operation[:query]
          base['body'] = operation[:body].id
        when :cache_delete
          base['key'] = operation[:key] if operation[:key]
          base['query'] = operation[:query] if operation[:query]
        when :cache_write
          base['key'] = operation[:key] if operation[:key]
          base['query'] = operation[:query] if operation[:query]
          base['body'] = operation[:body].id
        when :note
          base['message'] = operation[:message]
        end
        base
      end

      def result_to_data(spec)
        base = { 'candidate' => spec[:candidate] }
        base['body'] = spec[:body].id if spec[:body]
        base['cache_hit'] = spec[:cache_hit] unless spec[:cache_hit] == :unspecified
        base['coordinates'] = spec[:coordinates] if spec[:coordinates]
        base['address'] = spec[:address] if spec[:address]
        base['evidence'] = spec[:evidence] if spec[:evidence]
        base
      end

      private

      def load_bodies(spec)
        raise InputError, 'bodies must be an object keyed by body id' unless spec.is_a?(Hash)

        spec.each { |id, body_spec| @registry.add(id, body_spec) }
      end

      def load_processes(spec)
        raise InputError, 'processes must be a non-empty array' unless spec.is_a?(Array) && !spec.empty?

        spec.each_with_object({}) do |raw, out|
          entry = check_keys(raw, PROCESS_KEYS, 'process')
          id = entry['id'].to_s
          raise InputError, 'process id cannot be empty' if id.empty?
          raise InputError, "duplicate process #{id.inspect}" if out.key?(id)

          prefix = (entry['prefix'] || 'geocoder:').to_s
          out[id] = {
            id: id,
            label: (entry['label'] || id).to_s,
            prefix: prefix,
            cache_group: (entry['cache_group'] || id).to_s,
            config: symbolize(entry['config'] || {}),
            initial_cache: load_cache(entry['initial_cache'] || {}, prefix, "process #{id}")
          }
        end
      end

      def load_cache(raw, prefix, context)
        raise InputError, "#{context}: initial_cache must be an object" unless raw.is_a?(Hash)

        raw.each_with_object({}) do |(key, value), out|
          if value.is_a?(Hash)
            entry = stringify(value)
            body = @registry.resolve(entry['body'] || entry, context: "#{context} cache #{key}")
            physical = (entry['physical_key'] || key).to_s
          else
            body = @registry.resolve(value, context: "#{context} cache #{key}")
            physical = key.to_s
          end
          physical = "#{prefix}#{physical}" unless physical.start_with?(prefix)
          out[physical] = body
        end
      end

      def load_slots(spec)
        raise InputError, 'slots must be a non-empty array' unless spec.is_a?(Array) && !spec.empty?

        seen = {}
        spec.map do |raw|
          entry = check_keys(raw, SLOT_KEYS, 'slot')
          id = entry['id'].to_s
          raise InputError, 'slot id cannot be empty' if id.empty?
          raise InputError, "duplicate slot #{id.inspect}" if seen.key?(id)

          seen[id] = true
          process_id = entry['process'].to_s
          unless @processes.key?(process_id)
            raise InputError, "slot #{id}: unknown process #{process_id.inspect}"
          end
          operations = entry['operations']
          raise InputError, "slot #{id}: operations must be an array" unless operations.is_a?(Array)

          {
            id: id,
            process: process_id,
            description: entry['description'],
            operations: operations.map.with_index do |operation, index|
              load_operation(operation, "slot #{id} op #{index + 1}")
            end
          }
        end
      end

      def load_operation(raw, context)
        raise InputError, "#{context}: operation must be an object" unless raw.is_a?(Hash)

        entry = stringify(raw)
        op = entry['op'].to_s
        unless OPERATIONS.key?(op)
          raise InputError, "#{context}: unknown op #{op.inspect} " \
                            "(expected #{OPERATIONS.keys.join(', ')})"
        end
        check_keys(entry, OPERATIONS[op], context)
        query = entry['query']

        case op
        when 'search'
          raise InputError, "#{context}: search needs query" if query.nil?
          if entry['body'] && entry['returns']
            raise InputError, "#{context}: use body or returns, not both"
          end

          {
            op: :search,
            query: query,
            options: symbolize(entry['options'] || {}),
            body: optional_body(entry['body'] || entry['returns'], context),
            cache_hit: entry.key?('cache_hit') ? entry['cache_hit'] : :unspecified,
            results: load_results(entry['results'], context)
          }
        when 'fetch'
          raise InputError, "#{context}: fetch needs query" if query.nil?
          if entry['body'] && entry['returns']
            raise InputError, "#{context}: use body or returns, not both"
          end

          {
            op: :fetch,
            query: query,
            options: symbolize(entry['options'] || {}),
            body: optional_body(entry['body'] || entry['returns'], context),
            id: (entry['id'] && entry['id'].to_s)
          }
        when 'respond'
          raise InputError, "#{context}: respond needs query" if query.nil?
          raise InputError, "#{context}: respond needs body" if entry['body'].nil?

          {
            op: :respond,
            query: query,
            options: symbolize(entry['options'] || {}),
            body: @registry.resolve(entry['body'], context: "#{context} body")
          }
        when 'result', 'wrap'
          # With neither query nor fetch, the result is bound to the most
          # recent fetch in the same slot.
          {
            op: :result,
            query: query,
            options: symbolize(entry['options'] || {}),
            fetch: (entry['fetch'] && entry['fetch'].to_s),
            candidate: candidate_index(entry['candidate'], context),
            body: optional_body(entry['body'], context),
            cache_hit: entry.key?('cache_hit') ? entry['cache_hit'] : :unspecified,
            coordinates: coordinates(entry['coordinates'], context),
            address: entry['address'],
            evidence: entry['evidence']
          }
        when 'cache_delete'
          if entry['key'].nil? && query.nil?
            raise InputError, "#{context}: cache_delete needs key or query"
          end
          {
            op: :cache_delete,
            key: (entry['key'] && entry['key'].to_s),
            query: query,
            options: symbolize(entry['options'] || {})
          }
        when 'cache_write'
          if entry['key'].nil? && query.nil?
            raise InputError, "#{context}: cache_write needs key or query"
          end
          raise InputError, "#{context}: cache_write needs body" if entry['body'].nil?

          {
            op: :cache_write,
            key: (entry['key'] && entry['key'].to_s),
            query: query,
            options: symbolize(entry['options'] || {}),
            body: @registry.resolve(entry['body'], context: "#{context} body")
          }
        when 'note'
          { op: :note, message: entry['message'].to_s }
        end
      end

      def load_results(raw, context)
        return nil if raw.nil?
        raise InputError, "#{context}: results must be an array" unless raw.is_a?(Array)

        raw.map.with_index do |item, index|
          entry = check_keys(item, %w[candidate body cache_hit coordinates address evidence],
                             "#{context} result #{index + 1}")
          {
            op: :result,
            query: nil,
            candidate: candidate_index(entry['candidate'] || index, context),
            body: optional_body(entry['body'], context),
            cache_hit: entry.key?('cache_hit') ? entry['cache_hit'] : :unspecified,
            coordinates: coordinates(entry['coordinates'], context),
            address: entry['address'],
            evidence: entry['evidence']
          }
        end
      end

      def load_focus(raw)
        return nil if raw.nil?
        raise InputError, 'focus must be an object' unless raw.is_a?(Hash)

        entry = check_keys(raw, %w[slot candidate], 'focus')
        { slot: entry['slot'].to_s, candidate: candidate_index(entry['candidate'] || 0, 'focus') }
      end

      def candidate_index(value, context)
        index = Integer(value)
        raise InputError, "#{context}: candidate must be non-negative" if index.negative?

        index
      rescue ArgumentError, TypeError
        raise InputError, "#{context}: candidate must be an integer"
      end

      def coordinates(value, context)
        return nil if value.nil?
        unless value.is_a?(Array) && value.size == 2
          raise InputError, "#{context}: coordinates must be [lat, lon]"
        end

        value.map(&:to_f)
      end

      def optional_body(spec, context)
        return nil if spec.nil?

        @registry.resolve(spec, context: "#{context} body")
      end

      def load_groups(spec)
        raise InputError, 'groups must be an array' unless spec.is_a?(Array)

        seen = {}
        spec.map do |raw|
          entry = check_keys(raw, GROUP_KEYS, 'group')
          id = entry['id'].to_s
          raise InputError, 'group id cannot be empty' if id.empty?
          raise InputError, "duplicate group #{id.inspect}" if seen.key?(id)

          seen[id] = true
          processes = entry['processes']
          unless processes.is_a?(Array) && !processes.empty?
            raise InputError, "group #{id}: processes must be a non-empty array"
          end

          {
            id: id,
            processes: processes.map(&:to_s),
            description: entry['description']
          }
        end
      end

      def validate_groups!
        return if @groups.empty?

        seen = {}
        @groups.each do |group|
          group[:processes].each do |process_id|
            unless @processes.key?(process_id)
              raise InputError, "group #{group[:id]}: unknown process #{process_id.inspect}"
            end
            if seen.key?(process_id)
              raise InputError, "process #{process_id.inspect} is in groups #{seen[process_id]} and #{group[:id]}"
            end

            seen[process_id] = group[:id]
          end
        end
        missing = @processes.keys - seen.keys
        unless missing.empty?
          raise InputError, "process(es) not assigned to any group: #{missing.join(', ')}"
        end
      end

      def component(group_id, process_ids, shared_cache:)
        ids = process_ids.map(&:to_s)
        prefixes = ids.map { |id| @processes.fetch(id)[:prefix] }.uniq
        if shared_cache && prefixes.size > 1
          raise InputError,
                "group #{group_id}: processes sharing a cache must use the same prefix " \
                "(got #{prefixes.join(', ')})"
        end
        {
          id: group_id.to_s,
          process_ids: ids,
          slot_ids: @slots.select { |slot| ids.include?(slot[:process]) }.map { |slot| slot[:id] },
          shared_cache: shared_cache,
          prefix: prefixes.first
        }
      end

      def validate_candidates!
        @slots.each do |slot|
          slot[:operations].each_with_index do |operation, index|
            specs =
              case operation[:op]
              when :result
                [operation]
              when :search
                operation[:results] || []
              else
                []
              end
            specs.each do |spec|
              body = spec[:body]
              next unless body

              if body.candidates.size <= spec[:candidate]
                @warnings << format(
                  'slot %s op %d: body %s has %d candidate(s); candidate %d cannot come from it',
                  slot[:id], index + 1, body.id, body.candidates.size, spec[:candidate]
                )
              end
            end
          end
        end
      end

      def check_keys(entry, allowed, context)
        raise InputError, "#{context}: expected an object" unless entry.is_a?(Hash)

        entry = stringify(entry)
        unknown = entry.keys - allowed
        raise InputError, "#{context}: unknown key(s) #{unknown.join(', ')}" unless unknown.empty?

        entry
      end

      def stringify(value)
        case value
        when Hash
          value.each_with_object({}) { |(key, item), out| out[key.to_s] = stringify(item) }
        when Array
          value.map { |item| stringify(item) }
        else
          value
        end
      end

      def symbolize(hash)
        (hash || {}).each_with_object({}) do |(key, value), out|
          out[key.to_sym] = value
        end
      end
    end
  end
end
