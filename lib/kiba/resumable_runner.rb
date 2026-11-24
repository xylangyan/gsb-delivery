require "digest"
require "securerandom"

module Kiba
  # Runner which can stop after any acknowledged destination write and be
  # started again from the latest checkpoint.
  #
  # The execution order is intentionally identical to StreamingRunner:
  #
  # * every source page is pushed, row by row, through all transforms;
  # * a transform may yield any number of rows and then return one row, all of
  #   which continue down the pipeline in that order;
  # * after the sources are exhausted, transforms are closed from the first
  #   layer to the last, and close rows flow through the remaining transforms.
  #
  # Progress is committed per output. An output is identified by a stable
  # write id computed from the job id, segment and output index. Destinations
  # are acknowledged one by one, so a stop between two destinations leaves the
  # first one acknowledged and the second one pending.
  module ResumableRunner
    extend self

    SOURCE_PAGE_METHODS = [:read_page, :next_page].freeze
    LANDED_METHODS = [:landed?, :write_landed?, :already_written?].freeze
    WRITE_ID_KEYWORDS = [:write_id, :id, :write_identifier].freeze
    WRITE_ID_POSITIONALS = [:write_id, :id, :write_identifier].freeze

    def run(control, checkpoint: nil)
      store = checkpoint_for(control, checkpoint)
      Session.new(control, store).run
    end

    def status(control, checkpoint: nil)
      store = checkpoint_for(control, checkpoint)
      Checkpoint::State.new(store.load)
    end

    def reset(control, checkpoint: nil)
      store = checkpoint_for(control, checkpoint)
      store.clear
      Checkpoint::State.new
    end

    private

    def checkpoint_for(control, explicit)
      candidate = explicit
      candidate ||= control.config.fetch(:kiba, {})[:checkpoint]
      candidate ||= control.config.fetch(:kiba, {})[:checkpoint_store]
      Checkpoint::Store.coerce(candidate) || Checkpoint::MemoryStore.new
    end

    # A single run. Kept as a class so that status/reset stay free of any
    # execution state.
    class Session
      def initialize(control, store)
        @control = control
        @store = store
        @config = control.config.fetch(:kiba, {})
        @state = @store.load
        @transforms = nil
        @destinations = nil
        @sources = nil
      end

      def run
        if @state && @state[:phase] == :completed
          return Checkpoint::State.new(@state)
        end

        @state ||= new_state
        validate_state!
        run_pre_processes
        instantiate_components
        restore_snapshots(@state[:snapshots])
        process_sources
        # process_sources leaves the transforms at the post-source state.
        process_closes
        close_destinations
        run_post_processes
        @state[:phase] = :completed
        @state[:snapshots] = nil
        @state[:pending_page] = nil
        @state[:pending_close] = nil
        persist
        Checkpoint::State.new(@state)
      end

      private

      def new_state
        {
          version: Checkpoint::STATE_VERSION,
          job_id: job_id,
          phase: :running,
          fingerprint: fingerprint,
          pre_processes_done: false,
          post_processes_done: false,
          source_index: 0,
          cursors: Array.new(@control.sources.size),
          finished_sources: Array.new(@control.sources.size, false),
          page_sequences: Array.new(@control.sources.size, 0),
          snapshots: nil,
          pending_page: nil,
          next_close_layer: 0,
          closed_layers: [],
          pending_close: nil,
          closed_destinations: [],
          destination_count: @control.destinations.size
        }
      end

      def job_id
        explicit = @config[:job_id] || @config[:id]
        explicit ? explicit.to_s : SecureRandom.uuid
      end

      def fingerprint
        {
          sources: @control.sources.map { |d| component_name(d[:klass]) },
          transforms: @control.transforms.map { |d| component_name(d[:klass]) },
          destinations: @control.destinations.map { |d| component_name(d[:klass]) }
        }
      end

      def component_name(klass)
        klass.respond_to?(:name) ? klass.name : klass.class.name
      end

      def validate_state!
        if @state[:version] != Checkpoint::STATE_VERSION
          fail Checkpoint::Error,
            "checkpoint version #{@state[:version].inspect} is not supported"
        end
        if @state[:fingerprint] && @state[:fingerprint] != fingerprint
          fail Checkpoint::Error,
            "checkpoint belongs to a different job definition"
        end
        if @state[:destination_count] != @control.destinations.size
          fail Checkpoint::Error,
            "checkpoint destination count does not match the job"
        end
      end

      def persist
        @store.save(@state)
      rescue TypeError => e
        fail Checkpoint::Error,
          "checkpoint cannot be serialized (does it contain non-Marshal-able " \
          "rows or transform state?): #{e.message}"
      end

      def run_pre_processes
        return if @state[:pre_processes_done]
        pre = StreamingRunner.to_instances(@control.pre_processes, true, false)
        pre.each(&:call)
        @state[:pre_processes_done] = true
        persist
      end

      def run_post_processes
        return if @state[:post_processes_done]
        post = StreamingRunner.to_instances(@control.post_processes, true, false)
        post.each(&:call)
        @state[:post_processes_done] = true
        persist
      end

      def instantiate_components
        @sources = StreamingRunner.to_instances(@control.sources)
        @transforms = StreamingRunner.to_instances(@control.transforms, true)
        @destinations = StreamingRunner.to_instances(@control.destinations)
      end

      # ---- snapshots -------------------------------------------------------

      SNAPSHOT_METHODS = [:snapshot, :save_state, :dump_state].freeze
      RESTORE_METHODS = [:restore, :load_state, :restore_state].freeze

      def snapshot_all
        @transforms.each_with_index.map do |transform, index|
          method = SNAPSHOT_METHODS.find { |name| transform.respond_to?(name) }
          next nil unless method

          snapshot = transform.public_send(method)
          begin
            # Keep a copy detached from the live transform: transforms often
            # return references to their buffers, and downstream transforms
            # may mutate those rows in place while this segment is delivered.
            snapshot = Marshal.load(Marshal.dump(snapshot))
          rescue TypeError => e
            fail Checkpoint::Error,
              "transform layer #{index} cannot be checkpointed: #{e.message}"
          end
          {state: snapshot}
        end
      end

      def restore_snapshots(snapshots)
        snapshots ||= []
        @transforms.each_with_index do |transform, index|
          next unless restore_transform?(transform)
          entry = snapshots[index]
          # nil means the layer did not declare a snapshot method, not that
          # its state was nil.
          next if entry.nil?
          # Hand each layer its own copy: a transform may keep a reference to
          # the restored state and this state may be restored again if the
          # process stops later in the same segment.
          state = deep_copy(entry[:state])
          restore_transform(transform, state)
        end
      end

      # A close-only transform (for example one which buffers rows and flushes
      # them on close) does not need a #process method.
      def process_row(transform, row, &block)
        if transform.respond_to?(:process)
          transform.process(row, &block)
        elsif transform.respond_to?(:call)
          transform.call(row, &block)
        else
          row
        end
      end

      def restore_transform?(transform)
        RESTORE_METHODS.any? { |name| transform.respond_to?(name) }
      end

      def restore_transform(transform, snapshot)
        method = RESTORE_METHODS.find { |name| transform.respond_to?(name) }
        transform.public_send(method, snapshot)
      end

      # ---- sources ---------------------------------------------------------

      def process_sources
        index = @state[:source_index]
        while index < @sources.size
          @state[:source_index] = index
          if @state[:finished_sources][index]
            index += 1
            @state[:source_index] = index
            persist
            next
          end

          page = @state[:pending_page]
          if page && page[:source_index] == index
            process_page(page)
          else
            page = begin_page(index)
            process_page(page)
          end

          if @state[:finished_sources][index]
            index += 1
            @state[:source_index] = index
            persist
          end
        end
        @state[:source_index] = @sources.size
        persist
      end

      def begin_page(source_index)
        cursor = @state[:cursors][source_index]
        rows, next_cursor = read_page(@sources[source_index], cursor)
        # Transforms commonly mutate rows in place. Keep an untouched copy in
        # the checkpoint so that a restart replays exactly the same input.
        rows = deep_copy(rows)
        sequence = @state[:page_sequences][source_index]
        @state[:page_sequences][source_index] = sequence + 1
        page = {
          source_index: source_index,
          segment_id: "s#{source_index}:p#{sequence}",
          cursor: cursor,
          next_cursor: next_cursor,
          rows: rows,
          done_count: 0,
          pending_output: nil
        }
        @state[:pending_page] = page
        @state[:snapshots] = snapshot_all
        persist
        page
      end

      def deep_copy(value)
        Marshal.load(Marshal.dump(value))
      rescue TypeError => e
        fail Checkpoint::Error,
          "checkpoint data must be Marshal-able (rows and transform state): #{e.message}"
      end

      def read_page(source, cursor)
        method = SOURCE_PAGE_METHODS.find { |name| source.respond_to?(name) }
        if method
          result = call_page_method(source, method, cursor)
          normalize_page(result)
        else
          # A source which has not opted in can only provide a single page.
          # It is still accepted so that a job can adopt resumability
          # component by component.
          rows = []
          source.each { |row| rows << row }
          [rows, nil]
        end
      end

      def call_page_method(source, method, cursor)
        parameters = source.method(method).parameters
        if parameters.empty?
          source.public_send(method)
        elsif parameters.any? { |kind, _| [:key, :keyreq].include?(kind) }
          keyword = parameters.find { |kind, name| name == :cursor }&.last || :cursor
          source.public_send(method, keyword => cursor)
        else
          source.public_send(method, cursor)
        end
      end

      def normalize_page(result)
        case result
        when nil
          [[], nil]
        when Checkpoint::Page
          [result.rows || [], result.next_cursor]
        when Array
          if result.size == 2 && result[0].is_a?(Array)
            [result[0], result[1]]
          else
            [result, nil]
          end
        when Hash
          rows = result[:rows] || result["rows"] || []
          cursor = if result.key?(:next_cursor)
            result[:next_cursor]
          elsif result.key?("next_cursor")
            result["next_cursor"]
          elsif result.key?(:cursor)
            result[:cursor]
          else
            result["cursor"]
          end
          [rows, cursor]
        else
          if result.respond_to?(:rows)
            cursor = if result.respond_to?(:next_cursor)
              result.next_cursor
            elsif result.respond_to?(:cursor)
              result.cursor
            end
            [result.rows || [], cursor]
          else
            fail Checkpoint::Error,
              "cannot understand page returned by source: #{result.inspect}"
          end
        end
      end

      # ---- page segments ---------------------------------------------------

      def process_page(page)
        restore_snapshots(@state[:snapshots])
        deliver_segment(page, :page) do |emit|
          # Work on a copy: page[:rows] is preserved in the checkpoint and is
          # the reference input used if this page has to be replayed.
          deep_copy(page[:rows]).each do |row|
            emit_through(0, row, emit, nil)
          end
        end
        finish_page(page)
      end

      def finish_page(page)
        index = page[:source_index]
        if page[:next_cursor] == page[:cursor] && !page[:next_cursor].nil?
          fail Checkpoint::Error,
            "source #{index} returned the same cursor #{page[:next_cursor].inspect} " \
            "for the next page; a resumable source must advance its cursor"
        end
        @state[:cursors][index] = page[:next_cursor]
        @state[:finished_sources][index] = page[:next_cursor].nil?
        @state[:pending_page] = nil
        @state[:snapshots] = snapshot_all
        persist
      end

      # ---- close segments --------------------------------------------------

      def process_closes
        layers = (0...@transforms.size).select do |layer|
          @transforms[layer].respond_to?(:close)
        end
        layers.each do |layer|
          @state[:next_close_layer] = layer
          next if @state[:closed_layers].include?(layer)

          segment = @state[:pending_close]
          if !(segment && segment[:layer] == layer)
            segment = {
              layer: layer,
              segment_id: "close#{layer}",
              done_count: 0,
              pending_output: nil
            }
            @state[:pending_close] = segment
            @state[:snapshots] = snapshot_all
            persist
          end
          process_close(segment)
        end
        @state[:next_close_layer] = @transforms.size
        persist
      end

      def process_close(segment)
        restore_snapshots(@state[:snapshots])
        layer = segment[:layer]
        transform = @transforms[layer]
        deliver_segment(segment, :close) do |emit|
          transform.close do |close_row|
            emit_through(layer + 1, close_row, emit, layer)
          end
        end

        # The close of this layer is now fully acknowledged. Record completion
        # and drop the segment in a single save so that a stop right here
        # cannot replay the close.
        @state[:pending_close] = nil
        @state[:closed_layers] << layer
        @state[:snapshots] = snapshot_all
        persist
      end

      # ---- output delivery -------------------------------------------------

      # Walks the output stream of a segment. Outputs already acknowledged are
      # replayed through the transforms but not written again. The output at
      # done_count is the potentially partially acknowledged one; outputs
      # after it are new.
      def deliver_segment(segment, kind)
        index = 0
        emit = lambda do |row, layer|
          if index < segment[:done_count]
            index += 1
            next
          end
          deliver_output(segment, kind, row, index, layer)
          index += 1
        end
        yield emit

        if segment[:pending_output]
          fail Checkpoint::Error,
            "checkpoint expected output ##{segment[:done_count]} of " \
            "#{segment[:segment_id]} but the job produced only #{index}"
        end
        if index != segment[:done_count]
          fail Checkpoint::Error,
            "checkpoint expected #{segment[:done_count]} acknowledged outputs " \
            "for #{segment[:segment_id]} but the job produced #{index}"
        end
      end

      def deliver_output(segment, kind, row, index, layer)
        pending = segment[:pending_output]
        if pending
          unless pending[:index] == index
            fail Checkpoint::Error,
              "checkpoint pending output ##{pending[:index]} does not match " \
              "replayed output ##{index} of #{segment[:segment_id]}"
          end
        else
          pending = {
            index: index,
            layer: layer,
            kind: kind,
            segment_id: segment[:segment_id],
            write_id: write_id(segment, index),
            digest: digest(row),
            destination_count: @destinations.size,
            destination_acks: {}
          }
          segment[:pending_output] = pending
          persist
        end

        if pending[:digest] && pending[:digest] != digest(row)
          fail Checkpoint::Error,
            "output ##{index} of #{segment[:segment_id]} is not reproducible: " \
            "its content changed since the checkpoint"
        end

        @destinations.each_with_index do |destination, destination_index|
          next if pending[:destination_acks][destination_index]

          if landed?(destination, pending[:write_id])
            pending[:destination_acks][destination_index] = true
            persist
            next
          end

          write_to(destination, row, pending[:write_id])
          pending[:destination_acks][destination_index] = true
          persist
        end

        segment[:done_count] = index + 1
        segment[:pending_output] = nil
        persist
      end

      def write_id(segment, index)
        "#{@state[:job_id]}:#{segment[:segment_id]}:o#{index}"
      end

      def digest(row)
        payload = begin
          Marshal.dump(row)
        rescue TypeError
          row.inspect
        end
        Digest::SHA256.hexdigest(payload)
      end

      def landed?(destination, write_id)
        method = LANDED_METHODS.find { |name| destination.respond_to?(name) }
        return false unless method
        !!destination.public_send(method, write_id)
      end

      def write_to(destination, row, write_id)
        parameters = destination.method(:write).parameters
        keyword = parameters.find do |kind, name|
          [:key, :keyreq].include?(kind) && WRITE_ID_KEYWORDS.include?(name)
        end
        keyrest = parameters.any? { |kind, _| kind == :keyrest }
        positional = parameters.find do |kind, name|
          [:req, :opt].include?(kind) && WRITE_ID_POSITIONALS.include?(name)
        end

        if keyword
          destination.write(row, keyword.last => write_id)
        elsif keyrest
          destination.write(row, write_id: write_id)
        elsif positional
          destination.write(row, write_id)
        else
          destination.write(row)
        end
      end

      # ---- closes ----------------------------------------------------------

      def close_destinations
        @destinations.each_with_index do |destination, index|
          next unless destination.respond_to?(:close)
          next if @state[:closed_destinations].include?(index)
          destination.close
          @state[:closed_destinations] << index
          persist
        end
      end

      # ---- pipeline traversal ---------------------------------------------

      # Mirrors StreamingRunner#transform_stream: for a transform, yielded
      # rows are passed downstream immediately, then the returned row (if any)
      # is passed downstream. +producer_layer+ tracks the layer which produced
      # the row currently travelling downstream.
      def emit_through(layer, row, emit, producer_layer = nil)
        if layer >= @transforms.size
          emit.call(row, producer_layer)
          return
        end

        transform = @transforms[layer]
        returned = process_row(transform, row) do |yielded_row|
          emit_through(layer + 1, yielded_row, emit, layer)
        end
        emit_through(layer + 1, returned, emit, layer) if returned
      end
    end
  end
end
