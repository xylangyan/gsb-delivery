require "dotenv/handover/errors"
require "dotenv/handover/context"
require "dotenv/handover/manifest"
require "dotenv/handover/call"
require "dotenv/handover/trace"
require "dotenv/handover/runner"
require "dotenv/handover/verifier"
require "dotenv/handover/result"
require "dotenv/handover/solver"
require "dotenv/handover/cli"

module Dotenv
  # Rehearse a manifest of environment-collection steps against alternative
  # startup environments, without touching the caller's real +ENV+.
  module Handover
    class << self
      # Rehearse every registered route under +environment+ and +keys+.
      #
      # @param manifest [Manifest, Hash, String] parsed manifest, manifest hash, or path
      # @param environment [Hash, nil] inherited startup environment
      # @param keys [Array<String>, nil] inherited keys (defaults to environment keys)
      # @param files [Hash] file contents (or paths) named by the manifest
      # @param base_directory [String] directory used to resolve file names
      # @return [Result]
      def rehearse(manifest: nil, environment: {}, keys: nil, files: {}, base_directory: Dir.pwd, **options)
        manifest ||= options[:manifest_path]
        manifest = Manifest.load(manifest, base_directory: base_directory) unless manifest.is_a?(Manifest)
        runner = Runner.new(manifest, files: files, base_directory: base_directory)
        inherited = keys || (environment ? environment.keys : [])
        runner.rehearse(environment: environment || {}, keys: inherited)
      end

      # Convenience wrapper returning just the minimal inherited key roster.
      def minimal_keys(manifest: nil, environment: {}, keys: nil, files: {}, base_directory: Dir.pwd)
        rehearse(manifest: manifest, environment: environment, keys: keys,
          files: files, base_directory: base_directory).minimal_keys
      end
    end
  end
end
