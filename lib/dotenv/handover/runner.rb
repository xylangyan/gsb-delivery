require "dotenv/missing_keys"
require "dotenv/parser"
require "dotenv/substitutions/variable"
require "dotenv/environment"
require "dotenv/handover/errors"
require "dotenv/handover/context"
require "dotenv/handover/manifest"
require "dotenv/handover/call"
require "dotenv/handover/trace"

module Dotenv
  module Handover
    # Executes manifest checkpoints against isolated environments.
    #
    # The runner never reads or writes the caller's real +ENV+: parsing and
    # variable substitution are parameterized to use the branch's Sandbox,
    # and each branch owns its own sandbox so writes never leak sideways.
    class Runner
      # Substitutions allowed while rehearsing: variable references only.
      REHEARSAL_SUBSTITUTIONS = [Dotenv::Substitutions::Variable].freeze

      # Upper bound on total branch expansions; guards runaway manifests.
      MAX_STATES = 10_000

      # Upper bound on steps in a single route; guards cycles.
      MAX_DEPTH = 500

      attr_reader :manifest, :files, :base_directory

      def initialize(manifest, files: {}, base_directory: nil)
        @manifest = manifest
        @base_directory = base_directory || manifest.base_directory || Dir.pwd
        @files = {}
        manifest.files.each { |name, spec| @files[name.to_s] = spec }
        files.each do |name, content|
          name = name.to_s
          if content.is_a?(FileSpec)
            @files[name] = content
          elsif content.is_a?(Hash)
            @files[name] = FileSpec.new(name,
              content: content["content"] || content[:content] || content["text"] || content[:text],
              path: content["path"] || content[:path],
              base_directory: @base_directory)
          else
            @files[name] = FileSpec.new(name, content: content.to_s)
          end
        end
      end

      # Rehearse all routes starting from +environment+.
      #
      # @return [Result]
      def rehearse(environment: {}, keys: nil)
        env = environment.each_with_object({}) do |(key, value), hash|
          hash[key.to_s] = value.nil? ? nil : value.to_s
        end
        inherited = keys || env.keys
        inherited = inherited.map(&:to_s)

        sandbox = Sandbox.new(env)
        start = manifest.start
        raise ManifestError, "manifest has no checkpoints" unless start
        root = Trace.new(env: sandbox, checkpoint: start,
          inherited_keys: inherited, label: start)

        results = explore(root)
        Result.new(manifest: manifest, routes: results, environment: env,
          inherited_keys: inherited, files: @files, base_directory: @base_directory)
      end

      # Execute one checkpoint on a sandbox and return a CallResult.
      def perform(checkpoint, env)
        before = env.to_h
        env.clear_lookups!
        returned = {}
        exception = nil

        env.with_label(checkpoint.name) do
          begin
            returned = invoke(checkpoint, env)
          rescue StandardError, ScriptError => e
            exception = ExceptionInfo.new(e)
          end
        end

        after = env.to_h
        diff = EnvDiff.new(before, after)

        # Dotenv#load and #overwrite return the update diff, not the parsed
        # hash; only #parse returns every parsed key.
        returned = diff.env_hash if checkpoint.operation != :parse

        fields = capture_fields(checkpoint, returned)
        env_fields = capture_env_fields(checkpoint, after)
        fields = fields.merge(env_fields)
        CallResult.new(
          checkpoint: checkpoint,
          operation: checkpoint.operation,
          files: checkpoint.files.dup,
          returned: returned,
          fields: fields,
          env_diff: diff,
          exception: exception,
          lookups: env.lookups_for(checkpoint.name),
          env_after: env
        )
      end

      # Evaluate a destination's conditions against a CallResult.
      def matches?(destination, call)
        destination.conditions.all? do |condition|
          ConditionEvaluator.new(condition, call).matches?
        end
      end

      private

      def invoke(checkpoint, env)
        case checkpoint.operation
        when :parse
          parse_files(checkpoint, env, overwrite: false, apply: false)
        when :load
          parse_files(checkpoint, env, overwrite: false, apply: true)
        when :overwrite
          parse_files(checkpoint, env, overwrite: true, apply: true)
        else
          raise ManifestError, "unknown operation #{checkpoint.operation.inspect}"
        end
      end

      def parse_files(checkpoint, env, overwrite:, apply:)
        ignore = checkpoint.options.fetch(:ignore, true)
        filenames = checkpoint.files.map { |name| resolve(name) }
        filenames = [resolve(".env")] if filenames.empty?

        # Mirrors Dotenv.parse: overwrite reverses the file list so the
        # first-listed file has the highest precedence.
        filenames = filenames.reverse if overwrite

        filenames.reduce({}) do |hash, filename|
          begin
            file = file_for(filename)
            # Rehearsals only perform plain variable substitution. Command
            # substitution is deliberately excluded so a manifest can never
            # execute a shell command that observes the caller's real ENV.
            parsed = Parser.call(file.bytes, overwrite: overwrite, env: env,
              substitutions: REHEARSAL_SUBSTITUTIONS)
          rescue Errno::ENOENT, Errno::EISDIR
            raise unless ignore
            parsed = nil
          end

          if parsed
            if checkpoint.options.key?(:select)
              names = Array(checkpoint.options[:select]).map(&:to_s)
              parsed = parsed.select { |key, _| names.include?(key) }
            end

            # Dotenv#load applies each file as soon as it is parsed, before
            # the next file is read. Mirror that so a later file raising a
            # FormatError still leaves earlier files' writes in place.
            if apply
              parsed.each do |key, value|
                if overwrite || !env.key?(key)
                  env[key] = value
                end
              end
            end
          end

          hash.merge!(parsed || {})
        end
      end

      def resolve(name)
        return name if @files.key?(name)
        path = ::File.expand_path(name, @base_directory)
        path
      end

      def file_for(name)
        if @files.key?(name)
          @files[name]
        else
          path = ::File.expand_path(name, @base_directory)
          raise FileError, "file not provided to rehearsal: #{name}" unless ::File.file?(path)
          FileSpec.new(name, path: path)
        end
      end

      # Only fields the manifest explicitly asserts (via `expect`) or names
      # in `capture` are recorded. Fields used solely to choose a destination
      # drive branching but are not treated as required output bytes.
      def capture_fields(checkpoint, returned)
        names = checkpoint.capture
        names ||= checkpoint.fields
        names = names.map(&:to_s).uniq
        names.each_with_object({}) do |name, fields|
          if returned.key?(name)
            fields[name] = FieldValue.new(name, found: true, value: returned[name])
          else
            fields[name] = FieldValue.new(name, found: false)
          end
        end
      end

      def capture_env_fields(checkpoint, env_hash)
        checkpoint.expect_env.each_with_object({}) do |(key, expected), fields|
          if env_hash.key?(key)
            fields[key] = FieldValue.new(key, found: true, value: env_hash[key])
          else
            fields[key] = FieldValue.new(key, found: false)
          end
        end
      end

      def explore(root)
        routes = []
        queue = [[root, 0]]
        seen = 0

        until queue.empty?
          trace, depth = queue.shift
          seen += 1
          raise ReplayError, "manifest expands beyond #{MAX_STATES} states" if seen > MAX_STATES
          raise ReplayError, "manifest exceeds #{MAX_DEPTH} steps" if depth > MAX_DEPTH

          checkpoint = manifest.checkpoint(trace.checkpoint)
          unless checkpoint
            # Unknown checkpoint names are terminal aliases (e.g. "finish",
            # "end"); "halt"/"stop" were already handled by the caller.
            routes << trace
            next
          end

          call = perform(checkpoint, trace.env)
          branch = trace.push(call)

          matching = checkpoint.destinations.select { |destination| matches?(destination, call) }
          primary = matching.reject(&:fallback?)
          fallback = checkpoint.destinations.select(&:fallback?) - primary
          chosen = primary.any? ? primary : fallback
          if chosen.empty?
            routes << branch
            next
          end

          chosen.each do |destination|
            if destination.to.empty? || destination.to == "halt" || destination.to == "stop"
              routes << branch
            else
              queue << [branch.fork.with_checkpoint(destination.to), depth + 1]
            end
          end
        end

        routes
      end

      # Evaluates one destination condition against one call.
      class ConditionEvaluator
        def initialize(condition, call)
          @condition = condition
          @call = call
        end

        def matches?
          case @condition.kind
          when :field then field_matches?
          when :env then env_matches?
          when :error then error_matches?
          when :any
            @condition.children.any? { |child| self.class.new(child, @call).matches? }
          when :all
            @condition.children.all? { |child| self.class.new(child, @call).matches? }
          when :not
            !self.class.new(@condition.children.first, @call).matches?
          else false
          end
        end

        private

        def field_matches?
          field = @call.field(@condition.source)
          return false unless field
          compare(field.found?, field.value)
        end

        def env_matches?
          key = @condition.source
          env = @call.env_after
          present = env.key?(key)
          case @condition.op
          when :present, :truthy
            present && !env[key].nil? && env[key] != ""
          when :absent, :nil
            !present
          when :empty
            !present || env[key].nil? || env[key].to_s.empty?
          else
            compare(present, env[key])
          end
        end

        def error_matches?
          error = @call.exception
          present = !error.nil? && error.present?
          case @condition.source
          when :any
            @condition.op == :absent ? !present : present
          when :line
            return false unless present
            compare(true, error.line)
          when :class
            return false unless present
            compare(true, error.class_name)
          else
            false
          end
        end

        def compare(present, actual)
          case @condition.op
          when :present, :truthy
            return present && actual != nil && actual != "" if @condition.kind == :field
            present
          when :absent, :nil
            !present
          when :empty
            !present || actual.nil? || actual.to_s.empty?
          when :eq
            present && actual.to_s == @condition.value.to_s
          when :ne
            !present || actual.to_s != @condition.value.to_s
          when :include
            present && actual.to_s.include?(@condition.value.to_s)
          when :matches
            return false unless present
            ::Regexp.new(@condition.value.to_s).match?(actual.to_s)
          else
            false
          end
        end
      end
    end
  end
end
