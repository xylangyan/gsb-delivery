module Dotenv
  module Handover
    # A single discrepancy between a route and the full-environment reference.
    class Mismatch
      attr_reader :route, :step, :step_index, :aspect, :expected, :actual, :message

      def initialize(route:, step:, step_index:, aspect:, expected:, actual:, message:)
        @route = route
        @step = step
        @step_index = step_index
        @aspect = aspect
        @expected = expected
        @actual = actual
        @message = message
      end

      def checkpoint
        @step && @step["checkpoint"]
      end

      def to_h
        {
          "route" => @route,
          "step_index" => @step_index,
          "checkpoint" => checkpoint,
          "aspect" => @aspect.to_s,
          "expected" => @expected,
          "actual" => @actual,
          "message" => @message
        }
      end

      def inspect
        "#<Mismatch #{@aspect} at #{checkpoint || "route"} step #{@step_index}: #{@message}>"
      end
    end

    # Compares each replayed route with the full-environment reference.
    #
    # Comparison is per observation, in order: the selected files, every
    # manifest-named returned field (including exact bytes), the environment
    # change the call made, and any exception with its offending line.
    class Verifier
      attr_reader :result, :reference

      def initialize(result, reference)
        @result = result
        @reference = reference
      end

      # @return [Verification]
      def verify
        problems = []
        observations = {}

        result.routes.each_with_index do |route, index|
          expected_route = expected_route_for(route, index)
          if expected_route.nil?
            problems << Mismatch.new(route: index, step: nil, step_index: nil,
              aspect: :route, expected: "a registered reference route",
              actual: route.checkpoint_names,
              message: "route #{route.checkpoint_names.inspect} has no reference route")
            next
          end

          observations[index] = expected_route
          problems.concat(compare_route(index, route, expected_route))
        end

        Verification.new(result: result, reference: reference, mismatches: problems,
          observations: observations)
      end

      private

      # Routes are matched by checkpoint names first, then by position.
      def expected_route_for(route, index)
        names = route.checkpoint_names
        match = reference.routes.find { |candidate| candidate.checkpoint_names == names }
        match ||= reference.routes[index] if reference.routes[index]&.checkpoint_names == names
        return match if match

        # Allow prefix matches so a diverge-in-the-middle run is reported at
        # the first differing step instead of as an unknown route.
        reference.routes.max_by do |candidate|
          limit = [candidate.checkpoint_names.size, names.size].min
          (0...limit).take_while { |i| candidate.checkpoint_names[i] == names[i] }.size
        end
      end

      def compare_route(index, route, expected)
        problems = []
        max = [route.calls.size, expected.calls.size].max

        max.times do |step_index|
          actual = route.calls[step_index]
          want = expected.calls[step_index]

          if actual.nil?
            problems << Mismatch.new(route: index, step: want.observable, step_index: step_index,
              aspect: :step, expected: want.checkpoint.name, actual: nil,
              message: "route stopped before #{want.checkpoint.name}")
            next
          end

          if want.nil?
            problems << Mismatch.new(route: index, step: actual.observable, step_index: step_index,
              aspect: :step, expected: nil, actual: actual.checkpoint.name,
              message: "route continued to #{actual.checkpoint.name}")
            next
          end

          if actual.checkpoint.name != want.checkpoint.name
            problems << Mismatch.new(route: index, step: actual.observable, step_index: step_index,
              aspect: :checkpoint, expected: want.checkpoint.name, actual: actual.checkpoint.name,
              message: "expected #{want.checkpoint.name}, reached #{actual.checkpoint.name}")
          end

          problems.concat(compare_call(index, step_index, actual, want))
        end

        problems
      end

      def compare_call(index, step_index, actual, want)
        problems = []
        step = actual.observable

        if actual.files != want.files
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :files, expected: want.files, actual: actual.files,
            message: "selected files #{actual.files.inspect}, expected #{want.files.inspect}")
        end

        if actual.operation != want.operation
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :operation, expected: want.operation.to_s, actual: actual.operation.to_s,
            message: "operation #{actual.operation}, expected #{want.operation}")
        end

        (want.fields.keys | actual.fields.keys).sort.each do |name|
          expected_field = want.field(name)
          actual_field = actual.field(name)
          next if field_equal?(expected_field, actual_field)
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :field,
            expected: expected_field&.to_h, actual: actual_field&.to_h,
            message: "field #{name}: #{describe_field(actual_field)} != #{describe_field(expected_field)}")
        end

        # Enforce the manifest's own declared expectations for this step.
        want.checkpoint.expect.each do |name, expected|
          next unless expected.declared?
          field = actual.field(name)
          next if expected.matches?(field)
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :expectation,
            expected: expected.to_h, actual: field&.to_h,
            message: "field #{name}: #{describe_field(field)} does not satisfy the manifest expectation")
        end

        want.checkpoint.require_absent.each do |name|
          field = actual.field(name)
          next unless field&.found?
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :absence, expected: "absent", actual: field.to_h,
            message: "field #{name} must be absent")
        end

        unless actual.env_diff == want.env_diff
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :environment, expected: want.env_diff.to_h, actual: actual.env_diff.to_h,
            message: "environment change #{actual.env_diff.to_h.inspect} != #{want.env_diff.to_h.inspect}")
        end

        unless actual.exception == want.exception
          problems << Mismatch.new(route: index, step: step, step_index: step_index,
            aspect: :exception, expected: want.exception&.to_h, actual: actual.exception&.to_h,
            message: "exception #{describe_exception(actual.exception)} != #{describe_exception(want.exception)}")
        end

        problems
      end

      def field_equal?(expected, actual)
        return true if expected.nil? && actual.nil?
        return false if expected.nil? || actual.nil?
        expected == actual
      end

      def describe_field(field)
        return "missing" if field.nil?
        return "absent" unless field.found?
        "#{field.value.inspect} bytes=#{field.byte_array.inspect}"
      end

      def describe_exception(exception)
        return "none" unless exception&.present?
        "#{exception.class_name}(#{exception.message.inspect}) at #{exception.line.inspect}"
      end
    end

    # The outcome of comparing all routes to the reference.
    class Verification
      attr_reader :result, :reference, :mismatches, :observations

      def initialize(result:, reference:, mismatches:, observations:)
        @result = result
        @reference = reference
        @mismatches = mismatches
        @observations = observations
      end

      def ok?
        @mismatches.empty?
      end

      def failed?
        !ok?
      end

      def to_h
        {
          "ok" => ok?,
          "routes" => result.routes.size,
          "mismatches" => @mismatches.map(&:to_h)
        }
      end

      def describe
        lines = []
        if ok?
          lines << "all #{result.routes.size} route(s) match the full start environment"
        else
          lines << "#{@mismatches.size} mismatch(es) against the full start environment"
          @mismatches.each do |mismatch|
            lines << "  route #{mismatch.route} step #{mismatch.step_index} " \
              "(#{mismatch.checkpoint || "end"}): #{mismatch.message}"
          end
        end
        lines.join("\n")
      end
    end
  end
end
