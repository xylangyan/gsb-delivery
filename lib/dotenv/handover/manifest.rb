require "dotenv/handover/errors"

module Dotenv
  module Handover
    # A named .env file referenced by a manifest.
    #
    # +content+ is the file's bytes. When a manifest gives a path, the
    # manifest loader reads the bytes once and stores them here so rehearsal
    # is deterministic even if the file changes on disk afterwards.
    class FileSpec
      attr_reader :name, :content, :path

      def initialize(name, content: nil, path: nil, base_directory: nil)
        @name = name.to_s
        @path = path && base_directory ? ::File.expand_path(path.to_s, base_directory) : path&.to_s
        @content = content || (@path && ::File.binread(@path))
      end

      def bytes
        @content
      end

      def to_s
        @name
      end

      # A stable identity for the file, used when reporting selected files.
      def identity
        @path || @name
      end
    end

    # A named startup variant that a single roster must keep replayable.
    #
    # +expected+ is an optional list of route descriptors the case must
    # reach. Each descriptor is either an array of checkpoint names or a
    # hash with "route" (names) and optional "expect" (checkpoint => field
    # bytes). When omitted, the case's own full-environment run defines the
    # expected routes.
    ScenarioSpec = Struct.new(:name, :environment, :keys, :expected) do
      def to_h
        {"name" => name, "environment" => environment, "keys" => keys,
         "expected" => expected}
      end

      def inspect
        "#<ScenarioSpec #{name} keys=#{keys.inspect} expected=#{expected.inspect}>"
      end
    end

    # An expected value for a field returned by a step.
    #
    # Either +value+ or +bytes+ may be given. +bytes+ is the actual byte
    # sequence the field must equal, which lets a manifest be explicit about
    # line endings and trailing whitespace.
    class Expected
      attr_reader :value, :bytes

      def initialize(value: nil, bytes: nil, declared: false)
        @value = value
        @bytes = bytes
        @declared = declared
      end

      def declared?
        @declared
      end

      def empty?
        !declared?
      end

      def expected_value
        return @value if declared? && !@value.nil?
        return @bytes if declared? && !@bytes.nil?
        nil
      end

      def to_h
        {"value" => @value, "bytes" => @bytes, "declared" => declared?}
      end

      # Compare a captured FieldValue with this expectation.
      def matches?(field)
        return false if field.nil?
        if !declared?
          return field.found?
        end
        if !@bytes.nil?
          return field.found? && field.bytes == @bytes.to_s.b
        end
        if !@value.nil?
          return field.found? && field.value.to_s == @value.to_s
        end
        # Explicit nil expectation means the field must be absent.
        !field.found? || field.value.nil?
      end
    end

    # One "go to" edge from a checkpoint.
    class Destination
      attr_reader :to, :conditions, :fallback

      def initialize(to, conditions: [], fallback: false)
        @to = to.to_s
        @conditions = Array(conditions)
        @fallback = fallback
      end

      def fallback?
        @fallback
      end

      def to_h
        {"to" => @to, "conditions" => @conditions.map(&:to_h), "fallback" => @fallback}
      end

      def inspect
        "#<Destination #{@to}#{@fallback ? " (fallback)" : ""}>"
      end
    end

    # A condition attached to a destination.
    #
    # +kind+ is one of :field, :env, :error, :truthy, :missing.
    # +source+ names the returned field (for :field) or environment key
    # (for :env). +op+ is :eq, :ne, :include, :matches, :present, :absent,
    # :truthy, or :empty.
    class Condition
      attr_reader :kind, :source, :op, :value, :children

      def initialize(kind, source: nil, op: :truthy, value: nil, children: [])
        @kind = kind.to_sym
        @source = source
        @op = op.to_sym
        @value = value
        @children = children
      end

      def to_h
        {
          "kind" => @kind.to_s,
          "source" => @source,
          "op" => @op.to_s,
          "value" => @value,
          "children" => @children.map(&:to_h)
        }
      end

      def inspect
        "#<Condition #{@kind} #{@source.inspect} #{@op} #{@value.inspect}>"
      end
    end

    # A single step in a manifest.
    #
    # +operation+ is :parse, :load, or :overwrite. +files+ are file names.
    # +expect+ maps returned field names to Expected values. +destinations+
    # are alternatives evaluated in order. +capture+ names the keys checked
    # after this step (defaults to the keys listed by all destinations).
    class Checkpoint
      attr_reader :name, :operation, :files, :options, :expect, :destinations,
        :capture, :require_absent, :label, :expect_env

      def initialize(name, operation:, files: [], options: {}, expect: {}, destinations: [],
        capture: nil, require_absent: [], label: nil, expect_env: {})
        @name = name.to_s
        @operation = normalize_operation(operation)
        @files = Array(files).map(&:to_s)
        @options = options || {}
        @expect = expect || {}
        @destinations = Array(destinations)
        @capture = capture
        @require_absent = Array(require_absent).map(&:to_s)
        @label = label || @name
        @expect_env = expect_env || {}
      end

      def halt?
        @destinations.empty?
      end

      def fields
        @expect.keys
      end

      def terminal?
        @destinations.any? { |destination| destination.to.empty? || destination.to == "halt" }
      end

      private

      def normalize_operation(operation)
        value = operation.to_s.downcase
        case value
        when "parse" then :parse
        when "load" then :load
        when "overwrite", "overload" then :overwrite
        else
          raise ManifestError, "unknown operation #{operation.inspect} in checkpoint #{@name.inspect}"
        end
      end
    end

    # A manifest: startup environment declarations, named files, and steps.
    class Manifest
      attr_reader :checkpoints, :files, :start_environment, :required_keys,
        :optional_keys, :base_directory, :entry, :metadata, :reference,
        :scenario_specs

      def self.deep_stringify(value)
        case value
        when Hash
          value.each_with_object({}) { |(key, item), out| out[key.to_s] = deep_stringify(item) }
        when Array
          value.map { |item| deep_stringify(item) }
        else
          value
        end
      end

      def self.load(source, base_directory: Dir.pwd)
        case source
        when Manifest then source
        when Hash then from_hash(source, base_directory: base_directory)
        when String
          require "json"
          if ::File.file?(source)
            data = ::File.binread(source)
            parsed = begin
              JSON.parse(data)
            rescue JSON::ParserError
              require "yaml"
              YAML.safe_load(data, permitted_classes: [Symbol], aliases: true)
            end
            from_hash(parsed, base_directory: ::File.dirname(::File.expand_path(source)))
          else
            raise FileError, "manifest not found: #{source}"
          end
        else
          raise ManifestError, "cannot load manifest from #{source.class}"
        end
      end

      # Build a manifest from a plain Ruby hash. The schema is deliberately
      # forgiving: step lists may be an array or a name-keyed hash, keys may
      # be strings or symbols, and several spellings of each concept are
      # accepted.
      def self.from_hash(data, base_directory: Dir.pwd)
        raise ManifestError, "manifest must be a hash" unless data.is_a?(Hash)
        data = deep_stringify(data)
        manifest = new(base_directory: base_directory)
        manifest.send(:ingest, data)
        manifest
      end

      def initialize(base_directory: Dir.pwd)
        @base_directory = base_directory
        @checkpoints = {}
        @checkpoint_order = []
        @files = {}
        @start_environment = {}
        @required_keys = []
        @optional_keys = []
        @entry = nil
        @metadata = {}
        @reference = nil
        @scenario_specs = nil
      end

      def checkpoint(name)
        return name if name.is_a?(Checkpoint)
        name = name.to_s
        @checkpoints[name] || @checkpoints[name.to_sym]
      end

      def checkpoint_names
        @checkpoint_order.dup
      end

      def start
        @entry || @checkpoint_order.first
      end

      def file(name)
        name = name.to_s
        @files[name] || FileSpec.new(name, base_directory: @base_directory)
      end

      def add_file(name, content: nil, path: nil)
        @files[name.to_s] = FileSpec.new(name, content: content, path: path, base_directory: @base_directory)
      end

      def add_checkpoint(checkpoint)
        @checkpoints[checkpoint.name] = checkpoint
        @checkpoint_order << checkpoint.name unless @checkpoint_order.include?(checkpoint.name)
        checkpoint
      end

      def declared_keys
        (@required_keys + @optional_keys).uniq
      end

      def optional_key?(key)
        @optional_keys.include?(key.to_s)
      end

      def required_key?(key)
        @required_keys.include?(key.to_s)
      end

      def reference_key
        @reference
      end

      # The complete launch used as the comparison baseline.
      def reference_environment
        if @reference.is_a?(Hash)
          @reference.each_with_object({}) do |(key, value), hash|
            hash[key.to_s] = value.nil? ? nil : value.to_s
          end
        elsif scenarios_declared?
          cases.max_by { |scenario| scenario.keys.size }.environment
        else
          @start_environment
        end
      end

      def to_h
        {
          "entry" => start,
          "required_keys" => @required_keys,
          "optional_keys" => @optional_keys,
          "start_environment" => @start_environment,
          "checkpoints" => @checkpoint_order.map { |name| checkpoint_summary(checkpoint(name)) }
        }
      end

      def inspect
        "#<Manifest entry=#{start.inspect} checkpoints=#{@checkpoint_order.inspect}>"
      end

      private

      def ingest(data)
        @metadata = data["metadata"] || data["meta"] || {}
        @reference = data["reference"] || data["reference_environment"] || data["full_environment"]

        start_env = data["start_environment"] || data["environment"] || data["start"] ||
          data["inherited_environment"] || data["initial_environment"] || {}
        @start_environment = start_env.each_with_object({}) do |(key, value), hash|
          hash[key.to_s] = value.nil? ? nil : value.to_s
        end

        required = data["required_keys"] || data["required"] || data["must_inherit"] || []
        optional = data["optional_keys"] || data["optional"] || data["may_inherit"] || []
        @required_keys = Array(required).map(&:to_s)
        @optional_keys = Array(optional).map(&:to_s)

        ingest_files(data)
        ingest_scenarios(data)

        steps = data["checkpoints"] || data["steps"] || data["routes"] || data["stages"] || []
        if steps.is_a?(Hash)
          steps = steps.map { |name, spec| (spec || {}).merge("name" => name) }
        end

        steps.each { |spec| add_checkpoint(build_checkpoint(spec)) }

        entry = data["entry"] || data["start_checkpoint"] || data["first"] || data["entrypoint"]
        @entry = entry && entry.to_s
        @entry ||= @checkpoint_order.first
      end

      def ingest_files(data)
        source = data["files"] || data["env_files"] || data["documents"] || {}
        if source.is_a?(Array)
          source.each do |item|
            if item.is_a?(Hash)
              name = item["name"] || item["path"]
              add_file(name, content: item["content"], path: item["path"])
            else
              add_file(item)
            end
          end
        else
          source.each do |name, spec|
            case spec
            when Hash
              add_file(name, content: spec["content"] || spec["text"] || spec["body"],
                path: spec["path"])
            when nil
              add_file(name)
            else
              add_file(name, content: spec.to_s)
            end
          end
        end

        dir = data["directory"] || data["base_directory"] || data["root"]
        @base_directory = ::File.expand_path(dir.to_s, @base_directory) if dir
      end

      def build_checkpoint(spec)
        spec = stringify(spec)
        name = spec["name"] || spec["id"] || spec["checkpoint"] || spec["step"] || "step-#{@checkpoint_order.size + 1}"
        operation = spec["operation"] || spec["op"] || spec["kind"] || "parse"

        files = spec["files"] || spec["file"] || spec["filenames"] || []
        files = [files] unless files.is_a?(Array)

        options = spec["options"] || {}
        options = options.transform_keys(&:to_sym) if options.is_a?(Hash)

        expect = {}
        (spec["expect"] || spec["expects"] || spec["fields"] || spec["return"] || {}).each do |field, expected|
          expect[field.to_s] = build_expected(expected)
        end

        destinations = build_destinations(spec)
        capture = spec["capture"] || spec["capture_keys"] || spec["check_keys"]
        capture = Array(capture).map(&:to_s) if capture
        require_absent = spec["require_absent"] || spec["absent"] || []

        expect_env = {}
        (spec["expect_env"] || spec["environment"] || spec["env"] || {}).each do |key, expected|
          expect_env[key.to_s] = build_expected(expected)
        end

        Checkpoint.new(name, operation: operation, files: files, options: options,
          expect: expect, destinations: destinations, capture: capture,
          require_absent: Array(require_absent).map(&:to_s), label: spec["label"],
          expect_env: expect_env)
      end

      def build_expected(expected)
        case expected
        when Expected then expected
        when Hash
          expected = stringify(expected)
          if expected.key?("bytes")
            Expected.new(bytes: expected["bytes"], value: expected["value"], declared: true)
          elsif expected.key?("empty")
            Expected.new(value: "", declared: true)
          elsif expected.key?("value")
            Expected.new(value: expected["value"], declared: true)
          elsif expected.key?("nil")
            Expected.new(value: nil, declared: true)
          else
            Expected.new(value: expected, declared: true)
          end
        when nil
          Expected.new(value: nil, declared: true)
        else
          Expected.new(value: expected.to_s, declared: true)
        end
      end

      def build_destinations(spec)
        raw = spec["destinations"] || spec["next"] || spec["then"] || spec["branches"] || spec["goto"] || []

        case raw
        when Array
          raw.map do |entry|
            build_destination(entry, false)
          end
        when Hash
          # Two accepted hash forms:
          #   {"west" => {...conditions...}, "halt" => {...}}
          #   {when: {...}, to: "west"}
          if raw.keys.map(&:to_s).any? { |key| %w[to target next].include?(key) }
            [build_destination(raw, true)]
          else
            raw.map { |target, conditions| build_destination((conditions || {}).merge("to" => target), false) }
          end
        when String, Symbol
          [Destination.new(raw)]
        else
          []
        end
      end

      def build_destination(entry, fallback)
        case entry
        when String, Symbol
          Destination.new(entry, fallback: fallback)
        when nil
          Destination.new("", fallback: true)
        when Hash
          entry = stringify(entry)
          target = entry["to"] || entry["target"] || entry["next"] || entry["checkpoint"] || entry["name"] || ""
          conditions = build_conditions(entry)
          is_fallback = entry.key?("fallback") ? entry["fallback"] : fallback
          Destination.new(target, conditions: conditions, fallback: is_fallback)
        else
          raise ManifestError, "cannot read destination #{entry.inspect}"
        end
      end

      def build_conditions(entry)
        raw = entry["when"] || entry["conditions"] || entry["if"] || entry["condition"]
        conditions = case raw
        when nil
          # Shorthand keys on the destination itself.
          raw = entry
          []
        when Array
          raw
        when Hash
          [raw]
        else
          [{"raw" => raw}]
        end

        shorthand = []
        entry.each do |key, value|
          case key
          when "field"
            shorthand << {"field" => value, "op" => entry["op"] || "eq", "value" => entry["value"]}
          when "env"
            shorthand << {"env" => value, "op" => entry["op"] || "present", "value" => entry["value"]}
          when "error"
            shorthand << {"error" => value}
          when "error_line"
            shorthand << {"error_line" => value}
          when "error_kind", "error_class"
            shorthand << {"error_class" => value}
          when "truthy"
            shorthand << {"field" => value, "op" => "truthy"}
          when "missing"
            shorthand << {"field" => value, "op" => "absent"}
          when "present"
            shorthand << {"env" => value, "op" => "present"}
          end
        end

        (conditions + shorthand).map { |condition| build_condition(condition) }.compact
      end

      def build_condition(raw)
        case raw
        when Condition then raw
        when String
          # A bare string is a returned-field presence test.
          Condition.new(:field, source: raw, op: :present)
        when Hash
          raw = stringify(raw)
          if raw.key?("any") || raw.key?("or")
            list = raw["any"] || raw["or"]
            return Condition.new(:any, children: Array(list).map { |item| build_condition(item) }.compact)
          end
          if raw.key?("all") || raw.key?("and")
            list = raw["all"] || raw["and"]
            return Condition.new(:all, children: Array(list).map { |item| build_condition(item) }.compact)
          end
          if raw.key?("not")
            child = build_condition(raw["not"])
            return Condition.new(:not, children: [child].compact)
          end
          if raw.key?("raw")
            return build_condition(raw["raw"]) if raw["raw"].is_a?(Hash) || raw["raw"].is_a?(String)
            return nil
          end

          op = (raw["op"] || raw["operator"] || raw["cmp"] || "eq").to_s.downcase
          value = raw.key?("value") ? raw["value"] : raw["equals"]
          value = raw["equals"] if raw.key?("equals")

          if raw.key?("error_line") || raw.key?("line")
            line = raw["error_line"] || raw["line"]
            return Condition.new(:error, source: :line, op: op_for(op), value: line)
          end

          if raw.key?("error_class") || raw.key?("exception") || raw.key?("error_kind")
            klass = raw["error_class"] || raw["exception"] || raw["error_kind"]
            return Condition.new(:error, source: :class, op: op_for(op), value: klass)
          end

          if raw.key?("error") || raw.key?("raises")
            spec = raw.key?("error") ? raw["error"] : raw["raises"]
            if spec.is_a?(Hash)
              spec = stringify(spec)
              return Condition.new(:error, source: :line, op: :eq, value: spec["line"]) if spec.key?("line")
              return Condition.new(:error, source: :class, op: :eq, value: spec["class"] || spec["kind"]) if spec.key?("class") || spec.key?("kind")
            elsif spec == true
              return Condition.new(:error, source: :any, op: :present, value: true)
            elsif spec == false
              return Condition.new(:error, source: :any, op: :absent, value: false)
            elsif spec.is_a?(String)
              return Condition.new(:error, source: :line, op: :eq, value: spec)
            end
          end

          if raw.key?("field") || raw.key?("return") || raw.key?("key")
            field = raw["field"] || raw["return"] || raw["key"]
            return Condition.new(:field, source: field.to_s, op: op_for(op), value: value)
          end

          if raw.key?("env") || raw.key?("environment")
            key = raw["env"] || raw["environment"]
            return Condition.new(:env, source: key.to_s, op: op_for(op), value: value)
          end

          if raw.key?("truthy")
            return Condition.new(:field, source: raw["truthy"].to_s, op: :truthy)
          end

          if raw.key?("missing")
            return Condition.new(:field, source: raw["missing"].to_s, op: :absent)
          end

          nil
        else
          nil
        end
      end

      def op_for(op)
        case op
        when "=", "==", "eq", "equals" then :eq
        when "!=", "ne", "not" then :ne
        when "=~", "match", "matches", "regex" then :matches
        when "in", "includes", "include", "contains" then :include
        when "present", "exists", "defined", "truthy" then :present
        when "absent", "missing", "nil", "empty" then :absent
        else
          op.to_sym
        end
      end

      public

      # Declared startup variants, or one variant from the start environment.
      def cases
        @scenario_specs ||= begin
          env = @start_environment
          [ScenarioSpec.new("start", env, env.keys)]
        end
      end

      def scenarios_declared?
        !@scenario_specs.nil?
      end

      private

      def ingest_scenarios(data)
        raw = data["cases"] || data["scenarios"] || data["variants"] || data["handovers"]
        return if raw.nil?

        @scenario_specs = case raw
        when Array
          raw.each_with_index.map do |entry, index|
            entry = stringify(entry)
            name = entry["name"] || entry["id"] || "case-#{index + 1}"
            env = entry["environment"] || entry["start_environment"] || entry["values"] || @start_environment
            env = normalize_environment(env)
            keys = entry["keys"] || entry["inherited_keys"] || env.keys
            expected = normalize_expected(entry["expected"] || entry["expect_routes"] || entry["routes"])
            ScenarioSpec.new(name.to_s, env, Array(keys).map(&:to_s), expected)
          end
        when Hash
          raw.map do |name, spec|
            spec = stringify(spec || {})
            env = spec["environment"] || spec["start_environment"] || spec["values"] || spec
            env = normalize_environment(env)
            keys = spec["keys"] || spec["inherited_keys"] || env.keys
            expected = normalize_expected(spec["expected"] || spec["expect_routes"] || spec["routes"])
            ScenarioSpec.new(name.to_s, env, Array(keys).map(&:to_s), expected)
          end
        else
          raise ManifestError, "cases must be an array or hash"
        end
      end

      def normalize_expected(raw)
        return nil if raw.nil?
        list = raw.is_a?(Array) ? raw : [raw]
        list.map do |item|
          case item
          when Array
            {"route" => item.map(&:to_s), "expect" => {}}
          when String, Symbol
            {"route" => [item.to_s], "expect" => {}}
          when Hash
            item = stringify(item)
            route = item["route"] || item["path"] || item["checkpoints"] || []
            {
              "route" => Array(route).map(&:to_s),
              "expect" => item["expect"] || item["fields"] || {}
            }
          else
            raise ManifestError, "cannot read expected route #{item.inspect}"
          end
        end
      end

      def normalize_environment(env)
        return {} unless env.is_a?(Hash)
        env.each_with_object({}) do |(key, value), hash|
          next if %w[name id keys inherited_keys environment start_environment values].include?(key.to_s) &&
            value.is_a?(Hash)
          hash[key.to_s] = value.nil? ? nil : value.to_s
        end
      end

      def checkpoint_summary(checkpoint)
        {
          "name" => checkpoint.name,
          "operation" => checkpoint.operation.to_s,
          "files" => checkpoint.files,
          "fields" => checkpoint.fields,
          "destinations" => checkpoint.destinations.map(&:to_h)
        }
      end

      def stringify(hash)
        return hash unless hash.is_a?(Hash)
        hash.each_with_object({}) { |(key, value), out| out[key.to_s] = value }
      end
    end
  end
end
