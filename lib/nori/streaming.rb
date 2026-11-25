require "digest"
require "fileutils"
require "json"
require "securerandom"

require "nori"

class Nori
  # Streaming, resumable XML record extraction.
  #
  # Nori#parse needs the complete document. {Nori::Streaming::Session} accepts
  # bytes as they arrive and hands back records as soon as their closing tag is
  # seen, keeping only the bytes it has not yet committed in a local spool.
  #
  #   session = Nori::Streaming::Session.new(
  #     :records => [{:path => %w[daily warehouse record]}],
  #     :strip_namespaces => true,
  #     :spool => "/var/spool/nori"
  #   )
  #
  #   session.feed(chunk) { |record| process(record) }
  #   session.acknowledge(record.id)
  #
  # A record's identity is its business id: the ledger assigns one unique id
  # per original record and can look a record back up by that id. The id is
  # stable across checkpoints, retries and late acknowledgements.
  module Streaming
    # Options this layer understands in addition to Nori's own option keys.
    OPTION_KEYS = [
      :records, :record, :record_name, :record_names, :record_path,
      :record_paths, :emit, :emit_on, :commit_on, :commit_at, :boundaries,
      :boundary, :spool, :spool_dir, :directory, :ledger, :ledger_path,
      :on_record, :options, :nori, :nori_options, :parser_options,
      :business_id, :id, :id_generator, :id_prefix, :include_pending,
      :replay_context, :buffer_size, :checkpoint, :on_checkpoint,
      :option_provider, :options_provider, :context, :keep_spool, :durable,
      :start_offset, :validate, :record_element, :record_elements,
      :commit_boundaries, :spool_path, :state, :state_path, :ledger_file,
      :callback, :handler, :sink, :on_deliver, :at, :on, :paths, :names,
      :elements, :boundary_path, :boundary_name, :receipt_id
    ].freeze

    # Builds a session (or a scanner/ledger pair) without the caller having to
    # know the internal class layout.
    def self.session(options = {})
      Session.new(options)
    end

    def self.stream(options = {})
      Session.new(options)
    end

    def self.parse(xml, options = {}, &block)
      session = Session.new(options)
      records = session.feed(xml.dup, &block)
      records + session.finish(&block)
    end

    def self.records(xml, options = {}, &block)
      parse(xml, options, &block)
    end

    def self.each_record(xml, options = {}, &block)
      parse(xml, options, &block)
    end

    # Nori option keys are safe to persist as long as the values are plain
    # data. A lambda or other callable is intentionally not serializable; the
    # session keeps those in memory and restores them from the caller's
    # options when a checkpoint is loaded.
    def self.serializable_options(options)
      options.each_with_object({}) do |(key, value), hash|
        case value
        when String, Symbol, Numeric, NilClass, TrueClass, FalseClass
          hash[key] = value
        when Array
          hash[key] = value.map { |entry| serializable_options_value(entry) }
        when Hash
          hash[key] = serializable_options(value)
        when Proc, Method
          next
        end
      end
    end

    def self.serializable_options_value(value)
      case value
      when String, Symbol, Numeric, NilClass, TrueClass, FalseClass
        value
      when Array
        value.map { |entry| serializable_options_value(entry) }
      when Hash
        serializable_options(value)
      end
    end

    # JSON turns Symbol values into Strings. Rehydrate the option values whose
    # domain is a symbol so a reloaded checkpoint parses the same way.
    SYMBOL_VALUE_KEYS = [:parser].freeze

    def self.rehydrate_options(options)
      options.each_with_object({}) do |(key, value), hash|
        key = key.to_sym
        value = value.to_sym if key == :parser && value.is_a?(String)
        hash[key] = value
      end
    end

    def self.callable_options(options)
      options.each_with_object({}) do |(key, value), hash|
        hash[key] = value if value.is_a?(Proc) || value.is_a?(Method)
      end
    end

    def self.nori_options(options)
      options.reject { |key, _| OPTION_KEYS.include?(key) }
    end

    def self.merge_nori_options(options)
      nested = options[:nori_options] || options[:parser_options] || options[:nori] || options[:options] || {}
      nori_options(options).merge(nested)
    end
  end
end

require "nori/streaming/errors"
require "nori/streaming/boundaries"
require "nori/streaming/record_boundary"
require "nori/streaming/scanner"
require "nori/streaming/fragment_parser"
require "nori/streaming/record"
require "nori/streaming/ledger"
require "nori/streaming/spool"
require "nori/streaming/session"
