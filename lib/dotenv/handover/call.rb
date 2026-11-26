require "dotenv/parser"
require "dotenv/handover/context"

module Dotenv
  module Handover
    # A value observed for a manifest-named field, with its exact bytes.
    class FieldValue
      attr_reader :name, :value

      def initialize(name, found:, value: nil)
        @name = name.to_s
        @found = found
        @value = value
      end

      def found?
        @found
      end

      def bytes
        return nil unless found? && !@value.nil?
        @value.to_s.b
      end

      def byte_array
        bytes&.bytes
      end

      def to_h
        {"name" => name, "found" => found?, "value" => @value, "bytes" => byte_array}
      end

      def ==(other)
        other.is_a?(FieldValue) && name == other.name && found? == other.found? &&
          bytes == other.bytes
      end

      def inspect
        "#<FieldValue #{name}=#{@value.inspect} found=#{found?}>"
      end
    end

    # The exception (if any) raised by one call.
    class ExceptionInfo
      attr_reader :class_name, :message, :line, :line_number, :filename

      def initialize(exception)
        return unless exception
        @class_name = exception.class.name
        @message = exception.message
        @line = exception.respond_to?(:line) ? exception.line : nil
        @line_number = exception.respond_to?(:line_number) ? exception.line_number : nil
        @filename = exception.respond_to?(:filename) ? exception.filename : nil
      end

      def present?
        !@class_name.nil?
      end

      def to_h
        return nil unless present?
        {
          "class" => class_name,
          "message" => message,
          "line" => line,
          "line_number" => line_number,
          "filename" => filename
        }
      end

      def ==(other)
        other.is_a?(ExceptionInfo) && to_h == other.to_h
      end

      def inspect
        "#<ExceptionInfo #{class_name}: #{message.inspect} line=#{line.inspect}##{line_number}>"
      end
    end

    # One environment variable changed by one call.
    class EnvValue
      attr_reader :key, :value

      # +defined+ distinguishes a key that is absent from one set to nil.
      def initialize(key, value, defined: true)
        @key = key.to_s
        @value = value
        @defined = defined
      end

      def defined_key?
        @defined
      end

      def bytes
        @value.nil? ? nil : @value.to_s.b
      end

      def to_h
        {"key" => key, "defined" => defined_key?, "value" => value, "bytes" => bytes&.bytes}
      end

      def ==(other)
        other.is_a?(EnvValue) && key == other.key && defined_key? == other.defined_key? &&
          bytes == other.bytes
      end
    end

    # The difference one call made to its isolated environment.
    class EnvDiff
      attr_reader :added, :changed, :removed

      def initialize(before, after)
        @added = {}
        @changed = {}
        @removed = {}
        after.each do |key, value|
          if !before.key?(key)
            @added[key] = EnvValue.new(key, value)
          elsif before[key] != value
            @changed[key] = [EnvValue.new(key, before[key]), EnvValue.new(key, value)]
          end
        end
        before.each_key do |key|
          @removed[key] = EnvValue.new(key, before[key]) unless after.key?(key)
        end
        @removed.transform_values! { |value| EnvValue.new(value.key, value.value) }
        @added = @added.sort.to_h
        @changed = @changed.sort.to_h
        @removed = @removed.sort.to_h
      end

      def any?
        !(@added.empty? && @changed.empty? && @removed.empty?)
      end

      def empty?
        !any?
      end

      def to_h
        {
          "added" => @added.transform_values(&:to_h),
          "changed" => @changed.transform_values { |(old, new)| [old.to_h, new.to_h] },
          "removed" => @removed.transform_values(&:to_h)
        }
      end

      def ==(other)
        other.is_a?(EnvDiff) && to_h == other.to_h
      end

      def keys
        @added.keys + @changed.keys + @removed.keys
      end

      # The shape Dotenv.update returns: added and changed keys with their
      # new values, removed keys mapped to nil.
      def env_hash
        values = @added.transform_values(&:value)
        @changed.each { |key, (_, new_value)| values[key] = new_value.value }
        @removed.each_key { |key| values[key] = nil }
        values
      end
    end

    # The result of performing one manifest step in one branch.
    class CallResult
      attr_reader :checkpoint, :operation, :files, :returned, :fields,
        :env_diff, :exception, :lookups, :env_after

      def initialize(checkpoint:, operation:, files:, returned:, fields:,
        env_diff:, exception:, lookups:, env_after:)
        @checkpoint = checkpoint
        @operation = operation
        @files = files
        @returned = returned
        @fields = fields
        @env_diff = env_diff
        @exception = exception
        @lookups = lookups
        @env_after = env_after
      end

      # Asserted fields win; otherwise expose a value from the parsed result
      # so destination conditions can read any returned field. A key that the
      # update diff reports with a nil value (removed) is treated as absent.
      def field(name)
        name = name.to_s
        return @fields[name] if @fields.key?(name)
        return nil unless @returned.key?(name)
        return FieldValue.new(name, found: false) if @returned[name].nil? && @operation != :parse
        FieldValue.new(name, found: true, value: @returned[name])
      end

      def halted?
        !@exception.present?
      end

      def to_h
        {
          "checkpoint" => @checkpoint.name,
          "operation" => @operation.to_s,
          "files" => @files,
          "fields" => @fields.transform_values(&:to_h),
          "env_diff" => @env_diff.to_h,
          "exception" => @exception.to_h,
          "lookups" => @lookups.map(&:to_h)
        }
      end

      def observable
        {
          "checkpoint" => @checkpoint.name,
          "operation" => @operation.to_s,
          "files" => @files,
          "fields" => @fields.transform_values(&:to_h),
          "env_diff" => @env_diff.to_h,
          "exception" => @exception.to_h
        }
      end
    end
  end
end
