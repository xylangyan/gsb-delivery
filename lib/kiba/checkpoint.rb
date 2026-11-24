require "fileutils"

module Kiba
  module Checkpoint
    class Error < StandardError
    end

    STATE_VERSION = 1

    # A page of source rows together with the cursor to use to reopen the
    # following page. Cursors are opaque to Kiba and are persisted as-is.
    Page = Struct.new(:rows, :next_cursor)

    # Immutable-ish view over a persisted checkpoint. Callers can use this to
    # find out what is still pending without knowing the internal layout.
    class State
      def initialize(data = nil)
        @data = data || {}
      end

      def data
        @data
      end
      alias_method :to_h, :data

      def empty?
        @data.empty?
      end

      def phase
        (@data[:phase] || :new).to_sym
      end

      def completed?
        phase == :completed
      end

      def source_index
        @data[:source_index] || 0
      end

      def cursors
        (@data[:cursors] || []).dup
      end

      def source_cursor(index = source_index)
        (@data[:cursors] || [])[index]
      end

      def finished_sources
        (@data[:finished_sources] || []).dup
      end

      def segment
        @data[:pending_page] || @data[:pending_close]
      end

      def segment_kind
        return :page if @data[:pending_page]
        return :close if @data[:pending_close]
        nil
      end

      def segment_id
        segment && segment[:segment_id]
      end

      def done_count
        (segment && segment[:done_count]) || 0
      end

      def pending_source?
        !@data[:pending_page].nil?
      end

      def pending_close?
        !@data[:pending_close].nil?
      end

      def next_close_layer
        @data[:next_close_layer] || 0
      end

      def closed_layers
        (@data[:closed_layers] || []).dup
      end

      def job_id
        @data[:job_id]
      end

      # Details of the single output which may be partially acknowledged.
      def pending_output
        pending = segment && segment[:pending_output]
        return nil unless pending
        result = pending.dup
        result[:destination_acks] = (pending[:destination_acks] || {}).dup
        result
      end

      def pending?
        !pending_output.nil?
      end

      def pending_write_id
        pending_output && pending_output[:write_id]
      end

      # Zero-based transform layer which produced the pending output. A nil
      # layer means the output came straight from the source.
      def pending_layer
        pending_output && pending_output[:layer]
      end

      def pending_output_index
        pending_output && pending_output[:index]
      end

      def pending_kind
        pending_output && pending_output[:kind]
      end

      # A single hash describing what is waiting, suitable for logging or for
      # driving an operator UI. Layer is zero-based and matches the transform
      # order in the job.
      def pending_details
        output = pending_output
        return nil unless output
        {
          kind: output[:kind],
          segment_id: output[:segment_id],
          layer: output[:layer],
          output_index: output[:index],
          write_id: output[:write_id],
          destinations: pending_destinations,
          done_count: done_count
        }
      end

      def pending_source_index
        page = @data[:pending_page]
        page && page[:source_index]
      end

      # Destination indexes which have not been confirmed for the pending
      # output. An empty list but a present pending output means Kiba has not
      # persisted the all-confirmed transition yet.
      def pending_destinations
        output = pending_output
        return [] unless output
        acks = output[:destination_acks] || {}
        output.fetch(:destination_count, 0).times.reject { |i| acks[i] }
      end

      def closed_destinations
        (@data[:closed_destinations] || []).dup
      end

      def [](key)
        @data[key]
      end
    end

    # Base class for checkpoint stores. A store persists a single Hash and
    # must make each save atomic with respect to crashes.
    class Store
      def self.coerce(candidate)
        case candidate
        when nil
          nil
        when Store
          candidate
        when Hash
          HashStore.new(candidate)
        when String
          FileStore.new(candidate)
        else
          if candidate.respond_to?(:load) && candidate.respond_to?(:save)
            candidate
          elsif candidate.respond_to?(:read) && candidate.respond_to?(:write)
            IOStore.new(candidate)
          else
            fail Checkpoint::Error,
              "checkpoint must be a Hash, a path, or respond to #load and #save"
          end
        end
      end

      def load
        fail NotImplementedError
      end

      def save(_state)
        fail NotImplementedError
      end

      def clear
        fail NotImplementedError
      end

      def status
        State.new(load)
      end
    end

    class MemoryStore < Store
      def initialize(state = nil)
        @state = state
      end

      def load
        copy(@state)
      end

      def save(state)
        @state = copy(state)
      end

      def clear
        @state = nil
      end

      private

      def copy(value)
        return nil if value.nil?
        Marshal.load(Marshal.dump(value))
      rescue TypeError
        value
      end
    end

    # Backs a checkpoint with a caller-owned Hash. Saving replaces the
    # contents so the caller can keep inspecting the same object.
    class HashStore < Store
      def initialize(hash = {})
        @hash = hash
      end

      def load
        return nil if @hash.empty?
        Marshal.load(Marshal.dump(@hash))
      end

      def save(state)
        @hash.clear
        @hash.merge!(Marshal.load(Marshal.dump(state)))
      end

      def clear
        @hash.clear
      end

      def to_h
        @hash
      end
    end

    class FileStore < Store
      def initialize(path)
        @path = path.to_s
      end

      attr_reader :path

      def load
        return nil unless File.exist?(path)
        data = File.binread(path)
        return nil if data.empty?
        Marshal.load(data)
      rescue TypeError, ArgumentError => e
        raise Checkpoint::Error, "cannot read checkpoint #{path}: #{e.message}"
      end

      def save(state)
        dir = File.dirname(path)
        FileUtils.mkdir_p(dir) unless dir.empty? || Dir.exist?(dir)
        tmp = "#{path}.tmp.#{Process.pid}.#{rand(1_000_000)}"
        begin
          File.open(tmp, "wb") do |file|
            file.write(Marshal.dump(state))
            file.flush
            file.fsync
          end
          File.rename(tmp, path)
        ensure
          File.delete(tmp) if File.exist?(tmp)
        end
      end

      def clear
        File.delete(path) if File.exist?(path)
      end
    end

    # Useful for callers which already own an IO (for example a locked file).
    class IOStore < Store
      def initialize(io)
        @io = io
      end

      def load
        @io.rewind
        data = @io.read
        return nil if data.nil? || data.empty?
        Marshal.load(data)
      end

      def save(state)
        payload = Marshal.dump(state)
        @io.rewind
        @io.truncate(0)
        @io.write(payload)
        @io.flush
      end

      def clear
        @io.rewind
        @io.truncate(0)
        @io.flush
      end
    end

    def self.status(store)
      Store.coerce(store).status
    end
  end
end
