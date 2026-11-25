require 'geocoder/cache_stores/generic'

module Geocoder
  module Replay
    ##
    # A hash-backed store implementing the four methods the Generic cache
    # adapter uses. Reads/writes are prefixed exactly as Geocoder does;
    # #delete is literal, because Geocoder::Cache#expire hands the caller's key
    # straight to the adapter.
    #
    class Store
      attr_reader :events

      def initialize
        @data = {}
        @events = []
        @record = false
      end

      def recording!
        @record = true
        self
      end

      def [](key)
        value = @data[key.to_s]
        @events << { op: :read, key: key.to_s, body: value } if @record
        value
      end

      ##
      # Seed an entry without recording an event. Used only for cache contents
      # that were already present when the process started.
      ##
      def seed(key, value)
        @data[key.to_s] = value
        value
      end

      def []=(key, value)
        @data[key.to_s] = value
        @events << { op: :write, key: key.to_s, body: value } if @record
        value
      end

      def delete(key)
        value = @data.delete(key.to_s)
        @events << { op: :delete, key: key.to_s, body: value } if @record
        value
      end

      def keys
        @data.keys
      end

      def raw(key)
        @data[key.to_s]
      end

      def to_h
        @data.dup
      end

      def replace(hash)
        @data = hash.dup
        self
      end

      def truncate_events(length)
        @events.slice!(length..-1) if @events.length > length
      end
    end

    ##
    # The cache of one process. The same Store backs the lookup's own cache
    # (through Geocoder::CacheStore::Generic), so every read/write/delete is
    # the one Geocoder performs.
    #
    class ProcessCache
      attr_reader :cache, :store, :prefix

      def initialize(prefix: 'geocoder:', record: false)
        @prefix = prefix.to_s
        @store = Store.new
        @store.recording! if record
        store_service = Geocoder::CacheStore::Generic.new(@store, { prefix: @prefix })
        @cache = Geocoder::Cache.new(store_service, {})
      end

      def write(url, raw)
        @cache[url] = raw
      end

      ##
      # Literal, like Geocoder::Cache#expire. If +key+ does not include the
      # configured prefix it will not match a prefixed entry, and the delete
      # is a no-op.
      ##
      def expire(key)
        @cache.expire(key)
      end

      def raw_entries
        @store.to_h
      end

      def events
        @store.events
      end
    end
  end
end
