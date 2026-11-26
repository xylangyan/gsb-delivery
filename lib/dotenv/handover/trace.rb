require "dotenv/handover/call"

module Dotenv
  module Handover
    # An immutable snapshot of a running branch: the isolated environment,
    # the checkpoint just executed, and the accumulated trace.
    class Trace
      attr_reader :env, :checkpoint, :calls, :inherited_keys, :label

      def initialize(env:, checkpoint:, calls: [], inherited_keys: [], label: nil)
        @env = env
        @checkpoint = checkpoint
        @calls = calls
        @inherited_keys = inherited_keys
        @label = label
      end

      def with_env(env)
        Trace.new(env: env, checkpoint: checkpoint, calls: calls,
          inherited_keys: inherited_keys, label: label)
      end

      def push(call)
        Trace.new(env: call.env_after, checkpoint: call.checkpoint.name.to_s,
          calls: calls + [call], inherited_keys: inherited_keys, label: label)
      end

      # Return a copy positioned at +name+, used when a destination branch
      # leaves a checkpoint.
      def with_checkpoint(name)
        Trace.new(env: env, checkpoint: name.to_s, calls: calls,
          inherited_keys: inherited_keys, label: label)
      end

      def last
        calls.last
      end

      def field(name)
        last&.field(name)
      end

      def environment
        env
      end

      def fork
        Trace.new(env: env.fork, checkpoint: checkpoint, calls: calls.dup,
          inherited_keys: inherited_keys, label: label)
      end

      # A run of calls, reduced to the observations a manifest names.
      def steps
        calls.map(&:observable)
      end

      def step_signature
        calls.map do |call|
          [call.checkpoint.name, call.operation.to_s, call.files, call.fields.transform_values(&:to_h),
            call.env_diff.to_h, call.exception.to_h]
        end
      end

      def checkpoint_names
        calls.map { |call| call.checkpoint.name }
      end

      def selected_files
        calls.flat_map(&:files).uniq
      end

      def to_h
        {
          "label" => label,
          "steps" => calls.map(&:to_h),
          "environment" => env.to_h,
          "inherited_keys" => inherited_keys
        }
      end

      def inspect
        "#<Trace #{checkpoint&.name} calls=#{calls.size} env_keys=#{env.keys.size}>"
      end
    end
  end
end
