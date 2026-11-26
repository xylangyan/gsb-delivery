require "optparse"
require "json"
require "dotenv/handover/errors"
require "dotenv/handover/manifest"

module Dotenv
  module Handover
    # Command-line entry point for rehearsals.
    module CLI
      module_function

      def run(argv = ARGV, out: $stdout, err: $stderr)
        options = {}
        parser = OptionParser.new do |opts|
          opts.banner = "Usage: dotenv-handover MANIFEST [options]"
          opts.separator ""
          opts.separator "Rehearse a manifest without touching the real ENV."
          opts.separator ""
          opts.on("-e", "--environment FILE", "JSON/YAML startup environment") { |v| options[:environment] = v }
          opts.on("-k", "--keys KEYS", Array, "Comma-separated keys to inherit") { |v| options[:keys] = v }
          opts.on("-f", "--files DIR", "Directory holding named .env files") { |v| options[:directory] = v }
          opts.on("--reference", "Also run from the full start environment") { options[:reference] = true }
          opts.on("--verify", "Verify every route against the full start environment") { options[:verify] = true }
          opts.on("--minimal", "Print the minimal inherited key roster") { options[:minimal] = true }
          opts.on("--json", "Emit JSON") { options[:json] = true }
          opts.on("-h", "--help", "Show this help") do
            out.puts opts
            return 0
          end
          opts.on("-v", "--version", "Show version") do
            out.puts "dotenv #{Dotenv::VERSION}"
            return 0
          end
        end

        begin
          parser.parse!(argv)
        rescue OptionParser::ParseError => e
          err.puts e.message
          return 2
        end

        manifest_path = argv.shift
        unless manifest_path
          err.puts parser
          return 2
        end

        begin
          manifest = Manifest.load(manifest_path)
          environment = load_environment(options[:environment], manifest)
          keys = options[:keys] || environment.keys
          result = Dotenv::Handover.rehearse(manifest: manifest, environment: environment,
            keys: keys, base_directory: options[:directory] || manifest.base_directory)
          verification = (options[:verify] || options[:reference]) ? result.verify : nil
          solution = options[:minimal] ? result.minimal_keys : nil
        rescue Handover::Error => e
          err.puts "dotenv-handover: #{e.message}"
          return 1
        end

        if options[:json]
          payload = result.to_h
          payload["verification"] = verification.to_h if verification
          payload["minimal_keys"] = solution.to_h if solution
          out.puts JSON.pretty_generate(payload)
        else
          out.puts result.routes.map { |route| describe_route(route) }
          out.puts verification.describe if verification
          out.puts solution.describe if solution
        end

        return 0 if solution.nil? || solution.feasible?
        1
      end

      def load_environment(path, manifest)
        return manifest.start_environment if path.nil?
        require "json"
        data = File.binread(path)
        parsed = begin
          JSON.parse(data)
        rescue JSON::ParserError
          require "yaml"
          YAML.safe_load(data, permitted_classes: [Symbol], aliases: true)
        end
        if parsed.is_a?(Hash) && parsed.key?("environment")
          parsed["environment"]
        else
          parsed
        end
      end

      def describe_route(route)
        parts = ["route:"]
        route.calls.each do |call|
          field_bits = call.fields.map do |name, field|
            "#{name}=#{field.found? ? field.value.inspect : "(absent)"}"
          end
          file_bits = call.files.join(",")
          exception = (call.exception && call.exception.present?) ? " !#{call.exception.class_name}@#{call.exception.line_number}" : ""
          parts << "  #{call.checkpoint.name}: #{call.operation} #{file_bits} " \
            "{#{field_bits.join(", ")}}" + exception
        end
        parts.join("\n")
      end
    end
  end
end
