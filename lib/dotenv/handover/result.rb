require "dotenv/handover/errors"
require "dotenv/handover/call"
require "dotenv/handover/trace"

module Dotenv
  module Handover
    # Collects every route produced by a rehearsal.
    class Result
      attr_reader :manifest, :routes, :environment, :inherited_keys, :files, :base_directory

      def initialize(manifest:, routes:, environment:, inherited_keys:, files: {}, base_directory: nil)
        @manifest = manifest
        @routes = routes
        @environment = environment
        @inherited_keys = inherited_keys
        @files = files
        @base_directory = base_directory || manifest.base_directory
      end

      def empty?
        @routes.empty?
      end

      def size
        @routes.size
      end

      def to_h
        {
          "environment" => @environment,
          "inherited_keys" => @inherited_keys,
          "routes" => @routes.map(&:to_h)
        }
      end

      # Rehearse the same manifest from the full startup environment and
      # return a Reference. The reference is never the run under test: it is
      # the complete launch declared by the manifest (or the startup
      # environment when no complete case is declared).
      def reference
        @reference ||= build_reference
      end

      def reference_environment
        manifest.reference_environment
      end

      # Check every route against the full-environment reference.
      def verify
        Verifier.new(self, reference).verify
      end

      # Find the smallest inherited key set that reproduces every route.
      def minimal_keys
        Solver.new(self).solve
      end
      alias_method :inherit_keys, :minimal_keys

      # Exact bytes for every manifest-named file used by the rehearsal.
      def file_bytes
        manifest.files.each_with_object({}) do |(name, spec), out|
          out[name] = spec.bytes
        end
      end

      def inspect
        "#<Handover::Result routes=#{@routes.size} inherited=#{@inherited_keys.inspect}>"
      end

      private

      def build_reference
        environment = reference_environment
        runner = Runner.new(manifest, files: files, base_directory: base_directory)
        run = runner.rehearse(environment: environment, keys: environment.keys)
        Reference.new(environment: environment, result: run, routes: run.routes)
      end
    end

    # The full start environment plus the outcomes every route must reproduce.
    class Reference
      attr_reader :environment, :result, :routes, :expected

      def self.build(result)
        new(environment: result.environment, result: result, routes: result.routes)
      end

      def initialize(environment:, result:, routes:, expected: nil)
        @environment = environment
        @result = result
        @routes = routes
        @expected = expected
      end

      def inherited_keys
        @environment.keys
      end
    end
  end
end
