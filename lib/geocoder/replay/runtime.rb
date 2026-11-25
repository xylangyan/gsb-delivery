require 'geocoder/replay/errors'
require 'geocoder/replay/lookup'
require 'geocoder/replay/store'

module Geocoder
  module Replay
    class HttpResponse
      attr_reader :body, :code

      def initialize(body, code: 200, headers: {})
        @body = body
        @code = code.to_i
        @headers = (headers || {}).each_with_object({}) { |(key, value), out| out[key.to_s.downcase] = value }
      end

      def [](key)
        @headers[key.to_s.downcase]
      end
    end

    class PendingResponse
      attr_reader :query, :key, :body, :code, :headers, :source

      def initialize(query, key, body, code: 200, headers: {}, source: nil)
        @query = query
        @key = key
        @body = body
        @code = code.to_i
        @headers = headers || {}
        @source = source
      end

      def to_h
        { query: query, key: key, body: body.to_h, code: code, source: source }
      end
    end

    ##
    # Per-process state. Slots on the same process share this object: one
    # lookup instance, one cache, one queue of responses that have arrived.
    #
    class Runtime
      attr_reader :id, :label, :prefix, :cache, :lookup, :queue, :fetches, :body_registry

      def initialize(process, body_registry, record: true)
        @id = process[:id]
        @label = process[:label] || @id
        @prefix = process[:prefix]
        @body_registry = body_registry
        @cache = ProcessCache.new(prefix: @prefix, record: record)
        process[:initial_cache].each { |physical_key, body| @cache.store.seed(physical_key, body.raw) }
        @lookup = Lookup.new(process[:config])
        @lookup.replay_runtime = self
        @lookup.replay_cache!(@cache.cache)
        @queue = []
        @fetches = {}
        @next_fetch_id = 0
        @last_fetch = nil
        @override = nil
      end

      def marker
        @lookup.replay_marker
      end

      def cache_key(query)
        @lookup.cache_key_for(query)
      end

      def enqueue(query, key, body, code: 200, headers: {}, source: nil)
        response = PendingResponse.new(query, key, body, code: code, headers: headers, source: source)
        @queue << response
        response
      end

      ##
      # Called from Lookup#make_api_request. A miss consumes the oldest queued
      # response for the same query (and same key when both are known).
      # Serving a response recorded for a different query is a hard violation:
      # it would silently invent an interleaving.
      ##
      def take_response(query, key)
        if @override
          response = @override
          @override = nil
          return response
        end

        index = @queue.index do |pending|
          pending.query.to_s == query.to_s && (pending.key.nil? || key.nil? || pending.key == key)
        end
        index ||= @queue.index { |pending| !key.nil? && pending.key == key }
        if index.nil?
          if @queue.empty?
            raise Violation.new(
              'no_recorded_response',
              "cache miss for #{query.inspect} (#{key}) but no recorded response is queued"
            )
          end

          pending = @queue.first
          raise Violation.new(
            'response_for_other_query',
            "cache miss for #{query.inspect} (#{key}) but the next recorded response is for " \
            "#{pending.query.inspect} (#{pending.key})"
          )
        end

        response = @queue.delete_at(index)
        HttpResponse.new(response.body.raw, code: response.code, headers: response.headers)
      end

      def with_override(body, code: 200, headers: {})
        previous = @override
        @override = HttpResponse.new(body.raw, code: code, headers: headers)
        yield
      ensure
        @override = previous
      end

      def record_fetch(query:, key:, raw:, body:, marker:, source:, parse_error:, candidates:, id: nil)
        fetch = {
          id: id || next_fetch_id,
          process: @id,
          query: query,
          key: key,
          raw: raw,
          body: body,
          source: source,
          marker: marker,
          parse_error: parse_error,
          candidates: candidates
        }
        @fetches[fetch[:id]] = fetch
        @last_fetch = fetch
        fetch
      end

      def fetch(id)
        @fetches.fetch(id.to_s) do
          raise Violation.new('unknown_fetch', "unknown fetch id #{id.inspect}")
        end
      end

      def last_fetch_for(query)
        query_s = query.nil? ? nil : query.to_s
        @fetches.values.reverse.find { |fetch| fetch[:query].to_s == query_s } || @last_fetch
      end

      def snapshot
        {
          store: @cache.raw_entries,
          marker: marker,
          queue: @queue.dup,
          fetches: @fetches.dup,
          next_fetch_id: @next_fetch_id,
          last_fetch: @last_fetch,
          override: @override,
          event_count: @cache.events.length
        }
      end

      def restore(snapshot)
        @cache.store.replace(snapshot[:store])
        @cache.store.truncate_events(snapshot[:event_count])
        @lookup.replay_marker = snapshot[:marker]
        @queue = snapshot[:queue].dup
        @fetches = snapshot[:fetches].dup
        @next_fetch_id = snapshot[:next_fetch_id]
        @last_fetch = snapshot[:last_fetch]
        @override = snapshot[:override]
      end

      def cache_events
        @cache.events
      end

      private

      def next_fetch_id
        @next_fetch_id += 1
        "f#{@next_fetch_id}"
      end
    end
  end
end
