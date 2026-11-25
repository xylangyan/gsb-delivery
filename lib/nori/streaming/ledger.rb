require "json"

class Nori
  module Streaming
    # The receive ledger ("收件账"). It assigns one unique business id to every
    # original record, keyed by the record's position in the transmission, and
    # can look the record back up by that id. The id is assigned exactly once:
    # replaying a checkpoint, retrying a delivery or acknowledging late all
    # return the same id and never mint a second one.
    class Ledger
      DEFAULT_PREFIX = "rec".freeze

      Entry = Struct.new(:id, :key, :sequence, :name, :path, :byte_start,
                         :byte_end, :status, :created_at, :updated_at,
                         :metadata) do
        def to_h
          {
            :id => id,
            :key => key,
            :sequence => sequence,
            :name => name,
            :path => path,
            :byte_start => byte_start,
            :byte_end => byte_end,
            :status => status.to_s,
            :created_at => created_at,
            :updated_at => updated_at,
            :metadata => metadata
          }
        end

        def self.from_h(hash)
          new(
            hash["id"] || hash[:id],
            hash["key"] || hash[:key],
            hash["sequence"] || hash[:sequence],
            hash["name"] || hash[:name],
            hash["path"] || hash[:path],
            hash["byte_start"] || hash[:byte_start],
            hash["byte_end"] || hash[:byte_end],
            (hash["status"] || hash[:status]).to_s.to_sym,
            hash["created_at"] || hash[:created_at],
            hash["updated_at"] || hash[:updated_at],
            hash["metadata"] || hash[:metadata] || {}
          )
        end
      end

      attr_reader :path

      def initialize(path = nil, options = {})
        @path = path
        @entries = {}
        @keys = {}
        @next_sequence = 0
        @prefix = options[:prefix] || DEFAULT_PREFIX
        @generator = options[:id_generator] || options[:business_id] || options[:id]
        load! if @path && File.exist?(@path)
      end

      def empty?
        @entries.empty?
      end

      def size
        @entries.size
      end

      def entries
        @entries.values.sort_by(&:sequence)
      end

      # The deterministic key for an original record. It identifies the record
      # by where it lives in the transmission, so replaying the same bytes
      # finds the existing entry instead of creating a duplicate.
      def self.key_for(sequence, name, path, byte_start)
        [sequence, name, Array(path).join("/"), byte_start].join("|")
      end

      # Returns the existing entry or creates one. The id is minted here and
      # nowhere else, which keeps it from being generated before the record is
      # actually closed.
      def register(attributes)
        key = attributes[:key] || self.class.key_for(
          attributes[:sequence], attributes[:name], attributes[:path], attributes[:byte_start]
        )
        existing_id = @keys[key]
        return @entries[existing_id] if existing_id

        sequence = attributes[:sequence] || @next_sequence
        @next_sequence = [@next_sequence, sequence + 1].max
        id = mint(sequence, attributes)
        now = Time.now.utc
        entry = Entry.new(
          id, key, sequence, attributes[:name], attributes[:path],
          attributes[:byte_start], attributes[:byte_end],
          (attributes[:status] || :closed).to_sym, now, now, attributes[:metadata] || {}
        )
        @entries[id] = entry
        @keys[key] = id
        persist!
        entry
      end

      def [](id_or_key)
        @entries[id_or_key] || find(id_or_key)
      end
      alias find_by_id []
      alias lookup []
      alias find_by_key []

      def find(key)
        id = @keys[key]
        id && @entries[id]
      end

      def by_sequence(sequence)
        entries.find { |entry| entry.sequence == sequence }
      end

      def by_key(key)
        find(key)
      end

      def find_by_range(start_offset, end_offset = nil)
        entries.find do |entry|
          end_offset ? entry.byte_start == start_offset && entry.byte_end == end_offset : entry.byte_start == start_offset
        end
      end
      alias find_by_byte_range find_by_range
      alias find_by_offset find_by_range

      def for_path(path)
        entries.select { |entry| entry.path == path }
      end

      def acknowledged?(id)
        entry = @entries[id]
        entry ? entry.status.to_s == "acknowledged" : false
      end

      # Late acknowledgements are legitimate: a warehouse receipt can arrive
      # after the next record was already handed out. Acknowledge is therefore
      # idempotent and records the acknowledgment against the original entry.
      def acknowledge(id, at = Time.now.utc)
        entry = @entries[id]
        raise UnknownReceipt, "Unknown record id #{id.inspect}" unless entry
        entry.status = :acknowledged
        entry.updated_at = at
        persist!
        entry
      end

      def deliver(id, at = Time.now.utc)
        entry = @entries[id]
        raise UnknownReceipt, "Unknown record id #{id.inspect}" unless entry
        entry.status = :delivered if entry.status.to_s == "closed"
        entry.updated_at = at
        persist!
        entry
      end

      def pending
        entries.select { |entry| %i[closed delivered].include?(entry.status.to_sym) }
      end

      def unacknowledged
        pending
      end

      def to_h
        {
          "version" => 1,
          "next_sequence" => @next_sequence,
          "entries" => entries.map(&:to_h)
        }
      end

      def save!
        persist!
      end

      def load!
        return self unless @path && File.exist?(@path)
        raw = File.read(@path)
        return self if raw.strip.empty?
        data = JSON.parse(raw)
        @next_sequence = data["next_sequence"] || 0
        Array(data["entries"]).each do |hash|
          entry = Entry.from_h(hash)
          @entries[entry.id] = entry
          @keys[entry.key] = entry.id
          @next_sequence = [@next_sequence, entry.sequence.to_i + 1].max
        end
        self
      end

      # Used by Session when restoring a persisted ledger. It bypasses id
      # minting because the restored entry already has its original id.
      def store_entry(entry)
        @entries[entry.id] = entry
        @keys[entry.key] = entry.id
        @next_sequence = [@next_sequence, entry.sequence.to_i + 1].max
        entry
      end

      private

      def mint(sequence, attributes)
        if @generator.respond_to?(:call)
          candidate = call_generator(sequence, attributes)
          return candidate.to_s unless candidate.nil?
        elsif @generator.is_a?(String) || @generator.is_a?(Symbol)
          @prefix = @generator.to_s
        end
        format("%s-%06d-%s", @prefix, sequence, SecureRandom.hex(4))
      end

      def call_generator(sequence, attributes)
        arity = @generator.respond_to?(:arity) ? @generator.arity : 1
        case arity
        when 0 then @generator.call
        when 1 then @generator.call(sequence)
        else @generator.call(sequence, attributes)
        end
      end

      def persist!
        return unless @path
        dir = File.dirname(@path)
        FileUtils.mkdir_p(dir) unless dir == "." || Dir.exist?(dir)
        tmp = "#{@path}.tmp"
        File.write(tmp, JSON.pretty_generate(to_h))
        File.rename(tmp, @path)
      end
    end
  end
end
