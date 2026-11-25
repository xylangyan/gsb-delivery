require 'geocoder/lookups/nominatim'
require 'geocoder/configuration_hash'

module Geocoder
  module Replay
    ##
    # A Nominatim lookup whose configuration is supplied by the replay
    # scenario rather than by the global Geocoder singleton. Everything that
    # affects cache keys, response parsing and result objects is delegated to
    # the real lookup implementation.
    #
    class Lookup < Geocoder::Lookup::Nominatim
      DEFAULT_CONFIG = {
        timeout: 3,
        lookup: :nominatim,
        language: :en,
        host: 'nominatim.openstreetmap.org',
        use_https: true,
        api_key: nil,
        always_raise: [],
        http_headers: {},
        cache: nil,
        cache_options: { prefix: 'geocoder:', expiration: nil }
      }.freeze

      def initialize(config = {})
        @replay_config = DEFAULT_CONFIG.merge(symbolize(config))
        @replay_runtime = nil
        super()
      end

      attr_accessor :replay_runtime

      ##
      # Install the process cache on this lookup. Geocoder memoizes it in
      # @cache, so setting it here makes every fetch use the process store.
      ##
      def replay_cache!(cache)
        @cache = cache
        self
      end

      def replay_marker
        @cache_hit
      end

      def replay_marker=(value)
        @cache_hit = value
      end

      def cache_key_for(query)
        send(:cache_key, query)
      end

      def query_for(text, options = {})
        Geocoder::Query.new(text, options)
      end

      ##
      # Send the query through Geocoder's own fetch path. Cache lookups,
      # writes, and the @cache_hit marker are therefore the gem's, not a
      # parallel implementation.
      #
      def fetch_raw(query)
        send(:fetch_raw_data, query)
      end

      ##
      # Same parsing used by Nominatim#results, exposed for reporting.
      #
      def parse(raw)
        return nil if raw.nil?

        send(:parse_raw_data, raw.to_s)
      end

      def candidates_for(raw)
        doc = parse(raw)
        return [] if doc.nil?

        doc.is_a?(Array) ? doc : [doc]
      rescue StandardError
        []
      end

      def result_for(candidate)
        result_class.new(candidate)
      end

      def coordinates_for(candidate)
        result_for(candidate).coordinates
      rescue StandardError
        if candidate.is_a?(Hash) && candidate['lat'] && candidate['lon']
          [candidate['lat'].to_f, candidate['lon'].to_f]
        else
          []
        end
      end

      def address_for(candidate)
        result_for(candidate).address
      rescue StandardError
        candidate.is_a?(Hash) ? (candidate['display_name'] || candidate['address']) : candidate.to_s
      end

      private

      ##
      # Intercept the HTTP request made by fetch_raw_data and take the next
      # recorded response for this query from the process runtime.
      #
      def make_api_request(query)
        runtime = @replay_runtime
        raise Error, 'replay lookup has no runtime' unless runtime

        runtime.take_response(query.text, cache_key_for(query))
      end

      def configuration
        @replay_configuration ||= Geocoder::ConfigurationHash[
          @replay_config.each_with_object({}) { |(key, value), out| out[key] = value }
        ]
      end

      def symbolize(hash)
        (hash || {}).each_with_object({}) do |(key, value), out|
          out[key.to_sym] = value
        end
      end
    end
  end
end

