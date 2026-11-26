# frozen_string_literal: true

require 'json'

module Fluent
  module Logger
    module ReceiptAnalysis
      # Expands the long-archive repeat block lazily. A repeat block describes a
      # short run of operations that is replayed with a fixed second stride and
      # fresh original numbers (fixed byte width). Expansion is an Enumerator so
      # an archive of hundreds of millions of operations is never materialised
      # in memory.
      #
      # Inside a template an entry may name its own `"number"` (fixed literal),
      # inherit a fresh numbered one, or `"reuse"` the number assigned to an
      # earlier template slot (so one iteration can re-send an original).
      class RepeatExpander
        include Enumerable

        def initialize(template:, count:, stride:, start_time:, first_number:, identifier_width:)
          @template = template
          @count = Integer(count)
          @stride = Integer(stride)
          @start_time = Integer(start_time)
          @next_number = Integer(first_number)
          @identifier_width = identifier_width
        end

        def each
          return enum_for(:each) unless block_given?
          @count.times do |iteration|
            time_shift = iteration * @stride
            reuse = {}
            @template.each_with_index do |template_op, offset|
              kind = (template_op[:kind] || template_op['kind'] || :post).to_sym
              yield build(template_op, kind, time_shift, offset, reuse)
            end
          end
        end

        private

        def build(template_op, kind, time_shift, offset, reuse)
          time = Integer(template_op[:time] || template_op['time'] || 0) + time_shift
          case kind
          when :post
            slot = "#{kind}-#{offset}"
            number = if template_op.key?('reuse') || template_op.key?(:reuse)
                       reuse.fetch((template_op[:reuse] || template_op['reuse']).to_s) do
                         raise Error, 'repeat template reuses an unknown slot'
                       end
                     elsif template_op.key?('number') || template_op.key?(:number)
                       (template_op[:number] || template_op['number']).to_s
                     else
                       formatted = format_number(@next_number)
                       @next_number += 1
                       formatted
                     end
            reuse[slot] = number
            Operation.post(time, number, message: template_op[:message] || template_op['message'])
          when :close
            Operation.close(time)
          else
            raise Error, "Unsupported repeat operation kind: #{kind.inspect}"
          end
        end

        def format_number(number)
          number.to_s.rjust(@identifier_width, '0')
        end
      end

      # Parsed archive: messages, operations, the local fixture rules, the
      # certificate checkpoints, and any repeat block.
      class Archive
        attr_reader :tag, :identifier_width, :messages, :operations, :fixture,
                    :checkpoints, :requirements, :repeat, :source

        def initialize(tag: nil, identifier_width: DEFAULT_IDENTIFIER_WIDTH,
                       messages: {}, operations: [], fixture: nil, checkpoints: [],
                       requirements: nil, repeat: nil, source: nil)
          @tag = tag
          @identifier_width = identifier_width
          @messages = messages
          @operations = operations
          @fixture = fixture || Fixture::AlwaysComplete.new
          @checkpoints = checkpoints
          @requirements = requirements || []
          @repeat = repeat
          @source = source
        end

        def message_for(number)
          @messages[number] || bootstrap_message(number)
        end

        def bootstrap_message(number)
          Message.new(number: number, tag: @tag, record: { 'id' => number.to_s.rjust(@identifier_width, '0') },
                      identifier_format: nil)
        end

        # Resolves the operation stream for a run. Each :post operation carries
        # the Message it must pack, so packing always matches the archive record.
        # A repeat marker expands lazily in its place, preserving operation
        # position for checkpoints and close operations that follow it.
        def operation_stream
          return resolve_operations(@operations).to_enum unless @repeat || @operations.any? { |entry| repeat_marker?(entry) }

          repeat = @repeat || {}
          archive = self
          Enumerator.new do |yielder|
            @operations.each do |entry|
              if repeat_marker?(entry)
                spec = (entry[:_repeat] || entry['_repeat']).transform_keys(&:to_s)
                expander = RepeatExpander.new(
                  template: spec['template'] || [],
                  count: spec['count'] || repeat['count'],
                  stride: spec['stride'] || repeat['stride'],
                  start_time: spec['start_time'] || repeat['start_time'],
                  first_number: spec['first_number'] || repeat['first_number'],
                  identifier_width: @identifier_width
                )
                expander.each do |template_op|
                  yielder << archive.resolved_template_op(template_op)
                end
              else
                yielder << resolve(entry)
              end
            end
          end
        end

        def repeat_marker?(entry)
          entry.is_a?(Hash) && (entry.key?(:_repeat) || entry.key?('_repeat'))
        end

        # Structured expansion plan, used by the steady-state accelerator:
        #   prefix:   [Operation]            fixed operations before the marker
        #   repeat:   {count, stride, start_time, first_number, template}
        #   suffix:   [Operation]            fixed operations after the marker
        #   marker_op: global 1-based index of the marker (the first repeat op)
        # Only the first repeat marker participates in acceleration; a second
        # marker still expands lazily through +operation_stream+.
        def repeat_plan
          return nil unless @repeat || @operations.any? { |entry| repeat_marker?(entry) }
          index = @operations.index { |entry| repeat_marker?(entry) }
          return nil if index.nil?
          repeat = @repeat || {}
          spec = (@operations[index][:_repeat] || @operations[index]['_repeat']).transform_keys(&:to_s)
          prefix = @operations[0...index].map { |entry| resolve(entry) }
          suffix = @operations[(index + 1)..].map { |entry| resolve(entry) }
          plan_repeat = {
            'count' => Integer(spec['count'] || repeat['count'] || 0),
            'stride' => Integer(spec['stride'] || repeat['stride'] || 1),
            'start_time' => Integer(spec['start_time'] || repeat['start_time'] || 0),
            'first_number' => Integer(spec['first_number'] || repeat['first_number'] || 1),
            'template' => spec['template'] || []
          }
          { prefix: prefix, suffix: suffix,
            marker_op: prefix.length + 1,
            repeat: plan_repeat }
        end

        def resolved_template_op(operation)
          return operation unless operation.post?
          message = @messages[operation.number] || Message.new(
            number: operation.number, tag: @tag, time: operation.time,
            record: { 'id' => operation.number }, identifier_format: nil
          )
          Operation.post(operation.time, operation.number, message: message)
        end

        def resolve_operations(entries)
          entries.reject { |entry| repeat_marker?(entry) }.map { |entry| resolve(entry) }
        end

        def self.from_h(hash, source: nil)
          tag = hash['tag'] || hash[:tag]
          width = Integer(hash['identifier_width'] || hash[:identifier_width] || DEFAULT_IDENTIFIER_WIDTH)
          messages = parse_messages(hash['messages'] || hash[:messages] || {}, tag, width)
          body, repeat = parse_operations(hash, messages)
          fixture = build_fixture(hash['fixture'] || hash[:fixture])
          checkpoints = parse_checkpoints(hash['checkpoints'] || hash[:checkpoints])
          requirements = parse_requirements(hash['certificate'] || hash[:certificate], checkpoints)
          new(tag: tag, identifier_width: width, messages: messages,
              operations: body, fixture: fixture, checkpoints: checkpoints,
              requirements: requirements, repeat: repeat, source: source)
        end

        def self.from_json_file(path)
          from_h(JSON.parse(File.read(path)), source: path)
        end

        def self.parse_messages(raw, tag, width)
          messages = {}
          case raw
          when Hash
            raw.each do |key, value|
              number = key.to_s
              record = value.is_a?(Hash) ? symbolize_record(value) : { 'id' => number, 'payload' => value }
              record['id'] ||= format_number(number, width)
              messages[number] = Message.new(number: number, tag: tag, time: 0, record: record)
            end
          when Array
            raw.each_with_index do |value, index|
              number = format_number(index + 1, width)
              record = value.is_a?(Hash) ? symbolize_record(value) : { 'payload' => value }
              record['id'] ||= number
              messages[number] = Message.new(number: number, tag: tag, record: record)
            end
          end
          messages
        end

        def self.parse_operations(hash, messages)
          body = hash['operations'] || hash[:operations] || []
          repeat_hash = hash['repeat'] || hash[:repeat]
          repeat = nil
          if repeat_hash
            repeat_hash = repeat_hash.transform_keys(&:to_s)
            start_time = repeat_hash['start_time']
            first_number = repeat_hash['first_number']
            if start_time.nil?
              times = body.reject { |entry| entry.is_a?(Hash) && (entry.key?(:_repeat) || entry.key?('_repeat')) }
                          .map { |entry| entry[:time] || entry['time'] || 0 }
              start_time = times.empty? ? 0 : Integer(times.max)
            end
            if first_number.nil?
              numbers = body.reject { |entry| entry.is_a?(Hash) && (entry.key?(:_repeat) || entry.key?('_repeat')) }
                            .map { |entry| entry[:number] || entry['number'] }
                            .compact.map { |number| number.to_s.to_i }
              first_number = (numbers.max || 0) + 1
            end
            repeat = {
              'count' => Integer(repeat_hash['count'] || 0),
              'stride' => Integer(repeat_hash['stride'] || 1),
              'start_time' => Integer(start_time),
              'first_number' => Integer(first_number)
            }
          end
          [body, repeat]
        end

        def self.parse_checkpoints(raw)
          Array(raw).each_with_index.map do |entry, index|
            entry = entry.transform_keys(&:to_s) if entry.is_a?(Hash)
            Checkpoint.new(
              name: entry['name'] || entry[:name] || "cp#{index + 1}",
              index: entry['index'] || entry[:index],
              time: entry['time'] || entry[:time],
              number: entry['number'] || entry[:number],
              kind: entry['kind'] ? entry['kind'].to_sym : (entry[:kind] && entry[:kind].to_sym)
            )
          end
        end

        def self.parse_requirements(raw, checkpoints)
          return [] unless raw
          if raw.is_a?(Array)
            raw.map { |entry| parse_requirement(entry, checkpoints) }
          elsif raw.is_a?(Hash) && raw.key?('checkpoints')
            Array(raw['checkpoints']).map { |entry| parse_requirement(entry, checkpoints) }
          else
            [parse_requirement(raw, checkpoints)]
          end
        end

        def self.parse_requirement(raw, checkpoints)
          entry = raw.is_a?(Hash) ? raw.transform_keys(&:to_s) : {}
          req = Requirement.new(checkpoint: checkpoint_for(entry, checkpoints))
          if entry['numbers'].is_a?(Hash)
            entry['numbers'].each do |number, bounds|
              bounds = bounds.transform_keys(&:to_s) if bounds.is_a?(Hash)
              min = bounds ? (bounds['min'] || 0) : 0
              max = bounds && bounds.key?('max') ? bounds['max'] : nil
              req.require_number(number, min: min, max: max)
            end
          elsif entry['numbers']
            Array(entry['numbers']).each { |number| req.require_number(number.to_s, min: 1) }
          end
          req.callback_cap(Integer(entry['callback_cap'])) if entry['callback_cap']
          req.pending_cap(Integer(entry['pending_cap'])) if entry['pending_cap']
          Array(entry['forbidden']).each { |number| req.forbid(number) }
          req
        end

        def self.checkpoint_for(entry, checkpoints)
          if entry['checkpoint'].is_a?(Hash)
            hash = entry['checkpoint'].transform_keys(&:to_s)
            Checkpoint.new(name: hash['name'], index: hash['index'], time: hash['time'],
                           number: hash['number'], kind: hash['kind'] && hash['kind'].to_sym)
          else
            name = entry['checkpoint']
            checkpoints.find { |checkpoint| checkpoint.name == name } || Checkpoint.new(name: name)
          end
        end

        def self.build_fixture(raw)
          return Fixture::AlwaysComplete.new if raw.nil?
          case raw
          when Fixture::RuleTable, Fixture::Callable, Fixture::AlwaysComplete
            raw
          when Hash
            raw = raw.transform_keys(&:to_s)
            connect_entries = normalize_entries(raw['connect'] || raw['connects'], default_action: true)
            write_entries = normalize_entries(raw['write'] || raw['writes'], default_action: { outcome: :complete })
            disconnect_entries = normalize_entries(raw['disconnect'], default_action: { outcome: :drop })
            Fixture::RuleTable.new(connect_entries: connect_entries,
                                   write_entries: write_entries,
                                   disconnect_entries: disconnect_entries)
          else
            raise Error, "Unsupported fixture specification: #{raw.inspect}"
          end
        end

        def self.normalize_entries(raw, default_action:)
          return [] if raw.nil?
          Array(raw).map do |entry|
            if entry.is_a?(Hash)
              entry = entry.transform_keys(&:to_s)
              condition = entry['when'] || entry['if']
              if condition.is_a?(Hash)
                condition = condition.transform_keys(&:to_s)
                condition = parse_condition(condition)
              end
              action = entry.key?('then') ? entry['then'] : entry['action']
              action = parse_action(action)
              Fixture::RuleTable::Entry.new(condition: condition, action: action)
            else
              Fixture::RuleTable::Entry.new(condition: nil, action: parse_action(entry))
            end
          end
        end

        def self.parse_condition(hash)
          out = {}
          out[:position] = Array(hash['position']).map { |value| Integer(value) } if hash.key?('position')
          out[:time] = Array(hash['time']).map { |value| Integer(value) } if hash.key?('time')
          out[:pending_size] = Array(hash['pending_size']).map { |value| Integer(value) } if hash.key?('pending_size')
          out[:pending_size_at_least] = Integer(hash['pending_size_at_least']) if hash.key?('pending_size_at_least')
          out[:pending_size_below] = Integer(hash['pending_size_below']) if hash.key?('pending_size_below')
          out[:at_least] = parse_threshold(hash, 'at_least') if hash.key?('at_least')
          out[:below] = parse_threshold(hash, 'below') if hash.key?('below')
          out[:connection] = Array(hash['connection']) if hash.key?('connection')
          out
        end

        def self.parse_threshold(hash, key)
          value = hash[key]
          if value.is_a?(Array)
            [Integer(value[0]), value[1]]
          elsif value.is_a?(Hash)
            [Integer(value['threshold']), value['numbers']]
          else
            raise Error, "#{key} must pair a threshold with a class of numbers"
          end
        end

        def self.parse_action(action)
          return action if action.nil?
          return action if action.is_a?(Symbol) || action.is_a?(Integer)
          return action if action.is_a?(Hash)
          case action.to_s
          when 'complete', 'ok', 'accept', 'true' then :complete
          when 'fail', 'epipe', 'false', 'refuse' then :fail
          when 'eagain' then :eagain
          else action
          end
        end

        def self.symbolize_record(hash)
          hash.each_with_object({}) do |(key, value), acc|
            acc[key.to_s] = value
          end
        end

        def self.format_number(number, width)
          number.to_s.rjust(width, '0')
        end

        def resolve(operation)
          return operation if operation.is_a?(Operation)
          hash = operation.transform_keys(&:to_s)
          kind = (hash['kind'] || 'post').to_sym
          number = hash['number']
          time = Integer(hash['time'] || 0)
          case kind
          when :post
            message = message_for(number)
            Operation.post(time, message.number, message: message)
          when :close
            Operation.close(time)
          when :connect
            Operation.connect(time, hash['connection'] || 1)
          when :write
            Operation.write(time, outcome: (hash['outcome'] || :ok).to_sym)
          when :disconnect
            Operation.disconnect(time)
          else
            raise Error, "Unsupported archive operation kind: #{kind.inspect}"
          end
        end

        def next_number_after(operations)
          largest = 0
          operations.each do |op|
            next unless op.respond_to?(:number) && op.number
            value = op.number.to_s.to_i
            largest = value if value > largest
          end
          largest + 1
        end

        def start_time_of(operations)
          operations.empty? ? 0 : operations.map(&:time).max
        end
      end
    end
  end
end
