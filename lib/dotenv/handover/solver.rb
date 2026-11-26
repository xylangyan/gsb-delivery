require "dotenv/handover/errors"
require "dotenv/handover/runner"
require "dotenv/handover/verifier"

module Dotenv
  module Handover
    # A single continuation result that a candidate roster failed to keep.
    class Conflict
      attr_reader :route, :checkpoint, :step_index, :mismatches, :case_name

      def initialize(route:, checkpoint:, step_index:, mismatches:, case_name: nil)
        @route = route
        @checkpoint = checkpoint
        @step_index = step_index
        @mismatches = mismatches
        @case_name = case_name
      end

      def to_h
        {
          "case" => case_name,
          "route" => @route.checkpoint_names,
          "checkpoint" => @checkpoint,
          "step_index" => @step_index,
          "mismatches" => @mismatches.map(&:to_h)
        }
      end

      def inspect
        "#<Conflict #{@checkpoint} step #{@step_index}: #{@mismatches.map(&:message).join("; ")}>"
      end
    end

    # One declared case's outcome for one candidate roster.
    class CaseCoverage
      attr_reader :case_name, :want, :got, :descriptor

      def initialize(case_name, want, got, descriptor)
        @case_name = case_name
        @want = want
        @got = got
        @descriptor = descriptor
      end

      def trial_route
        got
      end

      def ok?
        return false if got.nil?
        return true if descriptor.nil?

        checks = Array(descriptor["route"]).all? do |_name|
          true
        end
        return false unless checks
        return false unless got.checkpoint_names == Array(descriptor["route"]).map(&:to_s)
        return true if want.nil?

        # Compare anything the reference can vouch for: file selection,
        # environment changes, exceptions, and field bytes.
        want.calls.each_with_index.all? do |expected, index|
          actual = got.calls[index]
          actual.files == expected.files &&
            actual.env_diff == expected.env_diff &&
            actual.exception == expected.exception &&
            expected.fields.all? { |name, field| actual.field(name) == field }
        end
      end
    end

    # Finds the smallest inherited key set that keeps every registered route.
    #
    # The search space is reduced to the optional keys the reference run
    # actually consulted: a key that no route reads cannot influence any
    # continuation, so it never needs to be carried. The remaining keys are
    # searched by increasing set size and, within a size, in sorted-name
    # order, so the first successful roster is both minimum and the
    # lexicographically smallest.
    class Solver
      attr_reader :result, :reference

      # Above this many relevant optional keys the exact sub-set search is
      # replaced by a greedy cover. Reaching that many keys at once means a
      # manifest where nearly every optional key changes a route; a single
      # roster is then almost always the full set anyway.
      EXHAUSTIVE_LIMIT = 18

      def initialize(result, reference = nil)
        @result = result
        @reference = reference || result.reference
      end

      # @return [Solution]
      def solve
        required = result.manifest.required_keys.map(&:to_s).uniq
        optional = result.manifest.optional_keys.map(&:to_s).uniq - required

        candidates = (consulted_keys & optional).sort

        solution = nil
        if candidates.size <= EXHAUSTIVE_LIMIT
          (0..candidates.size).each do |size|
            candidates.combination(size) do |combo|
              keys = (required + combo).uniq.sort
              if covers_all?(keys)
                solution = keys
                break
              end
            end
            break if solution
          end
        else
          solution = greedy_cover(candidates, required)
        end

        if solution
          Solution.new(keys: solution, required: required, optional: optional,
            result: result, reference: reference, feasible: true)
        else
          best = best_effort(candidates, required)
          Solution.new(keys: nil, required: required, optional: optional,
            result: result, reference: reference, feasible: false,
            conflicts: conflicts_for(best || required), best_effort: best || required)
        end
      end

      # Every environment key consulted (present) across all full-start runs.
      def consulted_keys
        names = []
        references.each_value do |ref|
          ref.routes.each do |route|
            route.calls.each do |call|
              call.lookups.each { |lookup| names << lookup.key if lookup.found }
            end
          end
        end
        names.uniq
      end

      # One reference per declared case: that case replayed from its own
      # complete startup values. `Result#verify` separately compares against
      # the one complete launch.
      def references
        @references ||= result.manifest.cases.each_with_object({}) do |scenario, hash|
          hash[scenario.name] = build_reference(scenario)
        end
      end

      private

      def build_reference(scenario = nil)
        environment = scenario ? scenario.environment : result.reference_environment
        runner = Runner.new(result.manifest, files: result.files,
          base_directory: result.base_directory)
        run = runner.rehearse(environment: environment, keys: environment.keys)
        Reference.new(environment: environment, result: run, routes: run.routes,
          expected: scenario&.expected)
      end

      def environment_for(keys, base = nil)
        base ||= result.environment
        base.select { |key, _| keys.include?(key) }
      end

      def replay(keys, base = nil)
        runner = Runner.new(result.manifest, files: result.files,
          base_directory: result.base_directory)
        runner.rehearse(environment: environment_for(keys, base), keys: keys)
      end

      # Replay one case's environment restricted to +keys+.
      def replay_case(scenario, keys)
        replay(keys, scenario.environment)
      end

      # True when every reference route is reproduced with identical
      # observations by some route in a run using +keys+.
      def covers_all?(keys)
        result.manifest.cases.all? do |scenario|
          trial = replay_case(scenario, keys)
          coverage_for(scenario, trial).all?(&:ok?)
        end
      end

      # One CaseCoverage per declared case, comparing the restricted run
      # with either the full-environment reference or the case's declared
      # outcome.
      def coverage_for(scenario, trial)
        ref = references[scenario.name]
        if scenario.expected.nil?
          ref.routes.map do |want|
            got = trial.routes.find { |candidate| candidate.checkpoint_names == want.checkpoint_names }
            CaseCoverage.new(scenario.name, want, got, nil)
          end
        else
          scenario.expected.map do |descriptor|
            names = Array(descriptor["route"]).map(&:to_s)
            got = trial.routes.find { |candidate| candidate.checkpoint_names == names }
            fallback = ref.routes.find { |candidate| candidate.checkpoint_names == names }
            CaseCoverage.new(scenario.name, fallback, got, descriptor)
          end
        end
      end

      # Greedy cover for large candidate sets: repeatedly add the key that
      # makes the most additional declared outcomes reproducible, then drop
      # any key that is not needed. The result is a valid roster when one
      # exists; ties follow sorted name order.
      def greedy_cover(candidates, required)
        keys = required.sort
        return keys if covers_all?(keys)

        remaining = candidates.dup
        until remaining.empty?
          best = nil
          best_score = -1
          remaining.each do |key|
            score = coverage_count((keys + [key]).sort)
            if score > best_score
              best = key
              best_score = score
            end
          end
          break if best.nil?
          keys = (keys + [best]).sort
          remaining.delete(best)
          return keys if covers_all?(keys)
        end

        keys.reverse_each do |key|
          trial = keys - [key]
          keys = trial if covers_all?(trial)
        end
        keys
      end

      # For infeasible manifests, find a roster that covers as much as
      # possible, breaking ties by shorter and lexicographically smaller key
      # lists. Exhaustive for small candidate sets; otherwise greedy.
      def best_effort(candidates, required)
        if candidates.size <= 12
          best = required.sort
          best_score = coverage_count(best)
          subsets = [[]]
          candidates.each { |key| subsets += subsets.map { |subset| subset + [key] } }
          subsets.each do |subset|
            keys = (required + subset).uniq.sort
            next if keys == best
            score = coverage_count(keys)
            if score > best_score || (score == best_score && lexicographically_smaller?(keys, best))
              best = keys
              best_score = score
            end
          end
          return best
        end

        keys = required.sort
        best_score = coverage_count(keys)
        remaining = candidates.dup
        until remaining.empty?
          scored = remaining.map { |key| [coverage_count((keys + [key]).sort), key] }
          score, key = scored.max_by { |count, name| [count, name] }
          break if score <= best_score
          keys = (keys + [key]).sort
          best_score = score
          remaining.delete(key)
        end

        keys.reverse_each do |key|
          trial = keys - [key]
          keys = trial if coverage_count(trial) >= best_score
        end
        keys
      end

      def coverage_count(keys)
        total = 0
        result.manifest.cases.each do |scenario|
          trial = replay_case(scenario, keys)
          total += coverage_for(scenario, trial).count(&:ok?)
        end
        total
      rescue Error
        0
      end

      def lexicographically_smaller?(left, right)
        left.size < right.size || (left.size == right.size && (left <=> right) == -1)
      end

      # Report, per case and route, the earliest step the chosen roster
      # failed to reproduce.
      def conflicts_for(keys)
        conflicts = []
        result.manifest.cases.each do |scenario|
          trial = replay_case(scenario, keys)
          coverage_for(scenario, trial).each do |coverage|
            next if coverage.ok?

            got = coverage.got
            want = coverage.want
            if want.nil? && coverage.descriptor
              names = Array(coverage.descriptor["route"]).map(&:to_s)
              want = ExpectedRoute.new(names)
            end
            got = closest_route(trial.routes, want || coverage.descriptor["route"]) if got.nil?
            mismatches = mismatch_list(got, coverage.want,
              descriptor: coverage.descriptor)
            next if mismatches.empty?

            first = mismatches.first
            conflicts << Conflict.new(
              route: coverage.want || coverage.trial_route,
              checkpoint: first.checkpoint || "(route)",
              step_index: first.step_index,
              mismatches: mismatches,
              case_name: scenario.name
            )
          end
        end
        conflicts
      end

      ExpectedRoute = Struct.new(:checkpoint_names)

      def closest_route(routes, want)
        names = want.respond_to?(:checkpoint_names) ? want.checkpoint_names : Array(want).map(&:to_s)
        routes.max_by do |route|
          limit = [route.checkpoint_names.size, names.size].min
          (0...limit).take_while { |i| route.checkpoint_names[i] == names[i] }.size
        end
      end

      def mismatch_list(got, want, descriptor: nil)
        if want.nil? && descriptor.nil?
          route = got ? got.checkpoint_names : []
          return [] if route.empty?
        end
        if descriptor && want.nil?
          route = Array(descriptor["route"]).map(&:to_s)
          actual_names = got ? got.checkpoint_names : []
          return [] if actual_names == route

          divergent = 0
          limit = [actual_names.size, route.size].min
          divergent += 1 while divergent < limit && actual_names[divergent] == route[divergent]
          return [Struct.new(:checkpoint, :step_index, :message).new(
            route[divergent] || actual_names[divergent], divergent,
            "expected route #{route.inspect}, got #{actual_names.inspect}"
          )]
        end
        problems = []
        max = [got ? got.calls.size : 0, want.calls.size].max
        max.times do |index|
          actual = got && got.calls[index]
          expected = want.calls[index]

          if actual.nil?
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              expected.checkpoint.name, index, "route stops before #{expected.checkpoint.name}")
            next
          end
          if expected.nil?
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index, "route continues to #{actual.checkpoint.name}")
            next
          end

          if actual.checkpoint.name != expected.checkpoint.name
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index,
              "expected #{expected.checkpoint.name}, reached #{actual.checkpoint.name}")
            next
          end

          if actual.files != expected.files
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index,
              "files #{actual.files.inspect} != #{expected.files.inspect}")
          end
          (expected.fields.keys | actual.fields.keys).sort.each do |name|
            next if actual.field(name) == expected.field(name)
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index,
              "field #{name}: #{actual.field(name)&.value.inspect} != #{expected.field(name)&.value.inspect}")
          end
          if actual.env_diff != expected.env_diff
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index, "environment change differs")
          end
          if actual.exception != expected.exception
            problems << Struct.new(:checkpoint, :step_index, :message).new(
              actual.checkpoint.name, index,
              "exception #{actual.exception&.to_h.inspect} != #{expected.exception&.to_h.inspect}")
          end
        end
        problems
      end

      def search_large(candidates, required, size)
        # For large candidate sets, first test keys that are individually
        # necessary when removed from the working set. This bounds the
        # combination search without changing its answer.
        forced = candidates.select do |key|
          base = candidates - [key]
          !covers_all?(required + base)
        end
        forced = (forced & candidates).sort
        return nil if forced.size > size

        remaining = candidates - forced
        needed = size - forced.size
        remaining.combination(needed) do |combo|
          keys = (required + forced + combo).uniq.sort
          return keys if covers_all?(keys)
        end
        nil
      end
    end

    # The roster found by the solver.
    class Solution
      attr_reader :keys, :required, :optional, :result, :reference, :conflicts,
        :best_effort

      def initialize(keys:, required:, optional:, result:, reference:, feasible:, conflicts: [],
        best_effort: nil)
        @keys = keys
        @best_effort = best_effort
        @required = required
        @optional = optional
        @result = result
        @reference = reference
        @feasible = feasible
        @conflicts = conflicts
      end

      def feasible?
        @feasible
      end

      def inherited
        @keys || []
      end

      def optional_included
        (inherited - required)
      end

      def optional_excluded
        (optional - optional_included)
      end

      def to_h
        {
          "feasible" => feasible?,
          "keys" => inherited,
          "best_effort" => @best_effort,
          "required_keys" => required,
          "optional_keys" => optional_included,
          "excluded_optional_keys" => optional_excluded,
          "conflicts" => conflicts.map(&:to_h)
        }
      end

      def describe
        return "inherited keys: #{inherited.inspect}" if feasible?
        lines = ["no single roster keeps every registered route"]
        lines << "  closest roster: #{@best_effort.inspect}" if @best_effort
        conflicts.each do |conflict|
          prefix = conflict.case_name ? "case #{conflict.case_name} " : ""
          lines << "  #{prefix}#{conflict.checkpoint} (step #{conflict.step_index}): " \
            "#{conflict.mismatches.map(&:message).join("; ")}"
        end
        lines.join("\n")
      end

      def inspect
        "#<Handover::Solution feasible=#{feasible?} keys=#{inherited.inspect}>"
      end
    end
  end
end
