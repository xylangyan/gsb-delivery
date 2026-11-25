require 'digest'
require 'json'
require 'geocoder/replay/errors'

module Geocoder
  module Replay
    ##
    # One raw response body as captured from the transport.
    #
    class Body
      attr_reader :id, :raw, :code, :headers

      def initialize(id, raw, code: 200, headers: {})
        @id = id.to_s
        @raw = raw.to_s
        @code = code.to_i
        @headers = (headers || {}).each_with_object({}) { |(key, value), out| out[key.to_s.downcase] = value }
      end

      def digest
        Digest::SHA256.hexdigest(raw)[0, 16]
      end

      def parse_error
        self.class.parse_error(raw)
      end

      ##
      # Mirrors Geocoder::Lookup::Nominatim#results: an object becomes a
      # one-element array, an array is returned as-is, and unparseable bodies
      # produce no candidates.
      ##
      def candidates
        return [] if parse_error

        doc = JSON.parse(raw)
        return [] if doc.nil?

        doc.is_a?(Array) ? doc : [doc]
      end

      def to_h
        { id: id, digest: digest, code: code, bytes: raw.bytesize }
      end

      def self.parse_error(raw)
        return 'no body' if raw.nil?
        return 'empty body' if raw.strip.empty?
        return 'Bandwidth limit exceeded' if raw.include?('Bandwidth limit exceeded')

        JSON.parse(raw)
        nil
      rescue JSON::ParserError, TypeError => e
        e.message
      end
    end

    ##
    # Body definitions from a scenario, keyed by id. Inline payloads receive a
    # content-derived id so reports can name them.
    ##
    class BodyRegistry
      attr_reader :bodies

      def initialize
        @bodies = {}
      end

      def add(id, spec)
        body = build(id, spec)
        if @bodies.key?(body.id) && @bodies[body.id].raw != body.raw
          raise InputError, "body #{body.id.inspect} is defined twice with different content"
        end

        @bodies[body.id] = body
      end

      def build(id, spec)
        case spec
        when Body
          spec
        when String
          Body.new(id, spec)
        when Hash
          options = stringify(spec)
          raw = options['body'] || options['raw']
          raw = JSON.generate(options['json']) if raw.nil? && options.key?('json')
          raw = JSON.generate(options) if raw.nil? && !options.key?('code') && !options.key?('headers')
          raw = options.to_s if raw.nil?
          Body.new(id, raw, code: options['code'] || 200, headers: options['headers'] || {})
        when Array
          Body.new(id, JSON.generate(spec))
        else
          Body.new(id, spec.to_s)
        end
      end

      def fetch(id)
        @bodies.fetch(id.to_s) do
          raise InputError, "unknown body #{id.inspect} (known: #{@bodies.keys.sort.join(', ')})"
        end
      end

      def include?(id)
        @bodies.key?(id.to_s)
      end

      def find_by_raw(raw)
        @bodies.values.find { |body| body.raw == raw }
      end

      def resolve(spec, context: 'body')
        case spec
        when nil
          nil
        when Body
          spec
        when Hash
          options = stringify(spec)
          if options['body_id']
            fetch(options['body_id'])
          elsif options['body'] || options['raw'] || options['json']
            payload = options['body'] || options['raw'] || options['json']
            if payload.is_a?(String) && include?(payload) && !payload_like?(payload)
              fetch(payload)
            else
              add(anonymous_id(options), options)
            end
          else
            add(anonymous_id(options), options)
          end
        when String
          if include?(spec) && !payload_like?(spec)
            fetch(spec)
          else
            add(anonymous_id(spec), spec)
          end
        when Array
          add(anonymous_id(spec), spec)
        else
          add(anonymous_id(spec), spec.to_s)
        end
      end

      def anonymous_id(spec)
        "inline@#{Digest::SHA256.hexdigest(JSON.generate(spec))[0, 12]}"
      rescue StandardError
        "inline@#{Digest::SHA256.hexdigest(spec.inspect)[0, 12]}"
      end

      private

      def payload_like?(string)
        stripped = string.lstrip
        stripped.start_with?('[', '{', '<') || string.include?("\n")
      end

      def stringify(hash)
        (hash || {}).each_with_object({}) { |(key, value), out| out[key.to_s] = value }
      end
    end
  end
end
