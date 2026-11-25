require "json"

class Nori
  module Streaming
    # Accepts a document in pieces and emits each record as soon as the
    # element selected by the caller's boundary specification closes.
    #
    # The session owns three pieces of durable state:
    #
    # * a {Spool} holding the bytes it has not been able to release yet,
    # * a {Scanner} snapshot holding any half-read token (text, CDATA, tag or
    #   attachment payload),
    # * a {Ledger} assigning one business id per original record.
    #
    # Saving a checkpoint is atomic. After it returns, the caller may let the
    # upstream delete the bytes covered by acknowledged receipts; the spool
    # never releases bytes belonging to an open or unacknowledged record.
    class Session
      attr_reader :spool, :ledger, :boundaries, :checkpoint_path
      attr_accessor :checkpoint_path

      def initialize(options = {}, extra = {})
        options = {} if options.nil?
        unless options.is_a?(Hash)
          options = { :records => options }.merge(extra)
        end
        @options = options.dup
        validate_options!(options) unless options[:validate] == false
        @boundaries = Boundaries.new(boundary_option(options))
        @nori_options = Streaming.merge_nori_options(options)
        @option_provider = options[:option_provider] || options[:options_provider]
        @on_record = options[:on_record]
        @id_generator = options[:business_id] || options[:id_generator] || options[:id]

        setup_storage(options)
        @ledger = build_ledger(options)
        @records_dir = @spool_path && File.join(File.dirname(@spool_path), "records")
        FileUtils.mkdir_p(@records_dir) if @records_dir
        @scanner = Scanner.new(@boundaries, @spool.start_offset)
        @doctype = nil
        @record_stack = []
        @records = []
        @next_sequence = 0
        @parse_failures = []
        @saved_position = @spool.end_offset
        load_checkpoint! if @checkpoint_path && File.exist?(@checkpoint_path)
      end

      # The absolute offset through which bytes have been received and stored.
      # It includes a half-read token, which has not produced a record yet.
      def stored_through
        @spool.end_offset
      end
      alias stored_offset stored_through
      alias bytes_received stored_through
      alias received_offset stored_through
      alias end_offset stored_through
      alias position stored_through
      alias offset stored_through

      # The first absolute offset still retained in the spool.
      def retained_from
        @spool.start_offset
      end
      alias retained_offset retained_from

      # The start of the half-read token, or the current end when no token is
      # pending. This is the precise point a checkpoint can safely describe.
      def pending_offset
        @scanner.pending_offset
      end

      # The absolute offset at which the next byte is expected. A caller can
      # resume a partially transmitted record with exactly this value.
      def next_offset
        @spool.end_offset
      end

      # The absolute offset through which everything has been checkpointed.
      def checkpoint_offset
        @saved_position
      end
      alias saved_offset checkpoint_offset
      alias resume_offset next_offset
      alias write_offset next_offset
      alias scan_position pending_offset

      def pending_bytes
        @scanner.pending_bytes
      end

      def pending?
        !@record_stack.empty? || @scanner.in_text?
      end

      def closed_record?
        !@record_stack.empty?
      end

      # Feed another piece of the transmission. Returns the records that
      # closed in this feed. A block receives each record as it closes.
      def feed(bytes, offset = nil, final = false, &block)
        if offset.is_a?(Hash)
          final = offset[:final] || offset[:finish] || offset[:end]
          offset = offset[:offset] || offset[:position] || offset[:at]
        end

        if bytes.respond_to?(:read) && !bytes.is_a?(String)
          return feed_io(bytes, :offset => offset, :final => final, &block)
        end

        bytes = bytes.to_s.b
        feed_bytes = bytes
        current_end = @spool.end_offset

        if offset && offset != current_end
          @spool.append_at(bytes, offset)
          # append_at may have dropped a prefix that was already received or
          # reclaimed. The scanner must see only the genuinely new tail, so its
          # position stays aligned with the spool.
          if offset >= current_end
            feed_bytes = bytes
          else
            consumed = current_end - offset
            feed_bytes = consumed >= bytes.bytesize ? "".b : bytes.byteslice(consumed, bytes.bytesize - consumed)
          end
        else
          @spool.append(bytes)
        end

        events = @scanner.feed(feed_bytes, final)
        records = process(events, &block)
        records
      end
      def <<(bytes)
        feed(bytes)
        self
      end

      alias push feed
      alias append feed
      alias write feed
      alias ingest feed

      # A convenience alias for callers that think in terms of the parser's
      # #parse method. Complete input is the normal case.
      def parse(bytes, options = {})
        feed(bytes, options) + finish
      end

      # Read +io+ to EOF in +chunk_size+ pieces, emitting records as they
      # close. Returns every record emitted during the read.
      def feed_io(io, options = {})
        chunk_size = options[:chunk_size] || options[:buffer_size] || 65_536
        final = options[:final] || options[:finish]
        offset = options[:offset]
        emitted = []
        while (chunk = io.read(chunk_size))
          emitted.concat(feed(chunk, :offset => offset))
          offset = nil
        end
        emitted.concat(feed("".b, :final => true)) if final
        emitted
      end

      # Finalize the transmission. Returns records that closed while flushing
      # and raises when a record or element is still open. An unclosed record
      # is never assigned a business id.
      def finish(&block)
        records = feed("".b, nil, true, &block)
        if !@record_stack.empty?
          raise ParseError.new(
            "Record #{@record_stack.last[:path].join('/')} is not closed; " \
            "bytes stored through #{stored_through}", stored_through
          )
        end
        unless @scanner.stack.empty?
          raise ParseError.new(
            "Element <#{@scanner.stack.last}> is not closed; bytes stored through #{stored_through}",
            stored_through
          )
        end
        records
      end
      alias finish! finish
      alias close finish
      alias flush finish
      alias end_document finish

      def each(&block)
        @records.each(&block)
      end

      def records
        @records.dup
      end
      alias delivered_records records
      alias emitted records
      alias output records

      def delivered
        @records.dup
      end

      def last
        @records.last
      end
      alias last_record last

      def receipts
        @records.map(&:credential)
      end
      alias credentials receipts
      alias receipt_credentials receipts

      def business_ids
        @records.map(&:id)
      end
      alias ids business_ids

      def receipt_for(record_or_id)
        id = record_or_id.respond_to?(:id) ? record_or_id.id : record_or_id
        record = self[id]
        record && record.credential
      end
      alias receipt receipt_for
      alias credential_for receipt_for

      def each_record(&block)
        @records.each(&block)
      end

      def [](id)
        @records.find { |record| record.id == id } || reconstruct_record(@ledger[id])
      end
      alias fetch []
      alias find_record []
      alias lookup []

      # Every record known to the ledger, whether delivered in this process or
      # recovered from a checkpoint.
      def all_records
        @ledger.entries.map { |entry| record_for_entry(entry) }.compact
      end

      def pending_records
        @ledger.pending.map { |entry| record_for_entry(entry) }.compact
      end
      alias unacknowledged_records pending_records
      alias unknown_records pending_records

      # Rebuild the records that were not acknowledged before the pause. The
      # last delivered-but-unacknowledged record is included even if later
      # records have since been closed.
      def recover
        pending_records
      end

      # Rebuild and yield the unacknowledged records, marking each as
      # delivered again. A record is never assigned a new id by this call.
      def replay(&block)
        records = pending_records
        records.each do |record|
          record.deliver!
          block.call(record) if block
        end
        records
      end
      alias recovered recover
      alias resume recover
      alias resume_records recover

      # Acknowledging is idempotent. A receipt that arrives after the next
      # record was handed out still belongs to the original record.
      def acknowledge(record_or_id, at = Time.now.utc)
        id = record_or_id.respond_to?(:id) ? record_or_id.id : record_or_id
        entry = @ledger.acknowledge(id, at)
        record = @records.find { |candidate| candidate.id == id }
        record.acknowledge! if record
        if record_or_id.respond_to?(:acknowledge!) && record_or_id != record
          record_or_id.acknowledge!
        end
        entry
      end
      alias ack acknowledge
      alias confirm acknowledge
      alias acknowledge_record acknowledge

      def deliver(record_or_id, at = Time.now.utc)
        id = record_or_id.respond_to?(:id) ? record_or_id.id : record_or_id
        entry = @ledger.deliver(id, at)
        record = @records.find { |candidate| candidate.id == id }
        record.deliver! if record
        entry
      end
      alias mark_delivered deliver

      def acknowledged?(record_or_id)
        id = record_or_id.respond_to?(:id) ? record_or_id.id : record_or_id
        @ledger.acknowledged?(id)
      end

      # Release the bytes covered by acknowledged receipts. The release point
      # is conservative: it never passes the start of an open or unacknowledged
      # record, nor the start of the half-read token.
      def discard_acknowledged!
        @spool.discard_before(safe_discard_offset)
        persist_if_possible
        retained_from
      end
      alias compact! discard_acknowledged!
      alias reclaim discard_acknowledged!
      alias reclaim! discard_acknowledged!
      alias release_acknowledged! discard_acknowledged!

      # Exposed so an upstream that keeps its own copy can ask exactly how far
      # the receiver has safely committed.
      def safe_discard_offset
        candidates = [@scanner.pending_offset]
        @record_stack.each { |context| candidates << context[:byte_start] }
        @ledger.pending.each { |entry| candidates << entry.byte_start }
        @records.each { |record| candidates << record.byte_start unless record.acknowledged? }
        candidates.compact.min || @spool.end_offset
      end

      def options
        @options
      end

      # Change the conversion options for records that have not started yet.
      # Open records keep the options they started with, and delivered records
      # are never re-read.
      def options=(new_options)
        @options = @options.merge(new_options)
        @nori_options = Streaming.merge_nori_options(@options)
        @option_provider = @options[:option_provider] || @options[:options_provider]
        @on_record = @options[:on_record] || @on_record
        @options
      end
      alias set_options options=
      alias update_options options=

      def with_options(changed = {})
        previous = @options
        previous_nori = @nori_options
        previous_provider = @option_provider
        previous_callback = @on_record
        self.options = changed
        yield self
      ensure
        @options = previous
        @nori_options = previous_nori
        @option_provider = previous_provider
        @on_record = previous_callback
      end

      # Persist the scanner tail, receipt ledger and spool window. The write is
      # atomic, so a crash leaves either the previous checkpoint or this one.
      def checkpoint!(path = nil)
        if path
          self.checkpoint_path = path
          adopt_checkpoint_storage(path)
        end
        persist_if_possible
        stored_through
      end
      alias save checkpoint!
      alias save! checkpoint!
      alias store checkpoint!
      alias snapshot checkpoint!

      def checkpoint
        state
      end

      def state
        {
          "version" => 1,
          "position" => @scanner.position,
          "boundaries" => @boundaries.to_a,
          "doctype" => @doctype,
          "spool_path" => @spool_path,
          "stored_through" => @spool.end_offset,
          "retained_from" => @spool.start_offset,
          "next_sequence" => @next_sequence,
          "scanner" => encode_scanner(@scanner.snapshot),
          "open_records" => @record_stack.map { |context| encode_open_record(context) },
          "records" => @records.map(&:marshal_metadata),
          "ledger" => @ledger.to_h,
          "options" => Streaming.serializable_options(@options),
          "nori_options" => Streaming.serializable_options(@nori_options)
        }
      end

      def self.load(path, options = {})
        session = allocate
        session.send(:initialize_from_checkpoint, path, options)
        session
      end
      class << self
        alias restore load
        alias resume load
        alias from_checkpoint load
      end

      def to_h
        state
      end

      # Raise the most recent record parse failure, if any. Parsing a record is
      # deferred to its close, so this is how a caller can tell that a record
      # was closed but its value could not be built.
      def parse_failures
        @parse_failures.dup
      end

      # Metadata for the bytes that have been stored but not closed yet.
      def progress
        {
          :stored_through => stored_through,
          :retained_from => retained_from,
          :position => stored_through,
          :pending_offset => pending_offset,
          :checkpoint_offset => @saved_position,
          :pending_bytes => pending_bytes,
          :pending => pending?,
          :open_record => @record_stack.last && {
            :name => @record_stack.last[:name],
            :path => @record_stack.last[:path].dup,
            :byte_start => @record_stack.last[:byte_start],
            :sequence => @record_stack.last[:sequence]
          },
          :records => @ledger.size,
          :unacknowledged => @ledger.pending.size
        }
      end
      alias status progress
      alias progress_info progress

      private

      def validate_options!(options)
        nori_keys = Streaming::FragmentParser::NORI_KEYS
        available = Streaming::OPTION_KEYS + nori_keys
        spurious = options.keys - available
        return if spurious.empty?
        raise ArgumentError, "Spurious options: #{spurious.inspect}\n" \
                             "Available options are: #{available.inspect}"
      end

      BOUNDARY_OPTION_KEYS = [
        :records, :record, :record_name, :record_names, :record_path,
        :record_paths, :emit, :emit_on, :commit_on, :commit_at,
        :boundaries, :boundary
      ].freeze

      def explicit_boundaries?(options)
        BOUNDARY_OPTION_KEYS.any? { |key| options.key?(key) }
      end

      def setup_storage(options)
        spool_option = options[:spool] || options[:spool_dir] || options[:directory]
        checkpoint_option = options[:checkpoint]

        if spool_option.nil? && checkpoint_option.is_a?(String)
          FileUtils.mkdir_p(File.dirname(checkpoint_option)) unless File.dirname(checkpoint_option) == "."
          @spool_path = "#{checkpoint_option}.bin"
          @checkpoint_path = checkpoint_option
          @spool = Spool.new(@spool_path, options[:start_offset] || 0)
          return
        end

        if spool_option.nil?
          @checkpoint_path = checkpoint_option if checkpoint_option.is_a?(String)
          @spool_path = nil
          @spool = Spool.new(nil, options[:start_offset] || 0)
          return
        end

        spool_option = spool_option.to_s
        if directory_spool?(spool_option)
          FileUtils.mkdir_p(spool_option)
          @spool_path = File.join(spool_option, "stream.bin")
          @checkpoint_path = checkpoint_option.is_a?(String) ? checkpoint_option : File.join(spool_option, "checkpoint.json")
        else
          FileUtils.mkdir_p(File.dirname(spool_option)) unless File.dirname(spool_option) == "."
          @spool_path = spool_option
          @checkpoint_path = checkpoint_option.is_a?(String) ? checkpoint_option : "#{spool_option}.checkpoint.json"
        end
        @spool = Spool.new(@spool_path, options[:start_offset] || 0)
      end

      def directory_spool?(path)
        File.directory?(path) || !path.match?(/\.[^\/]+\z/)
      end

      def build_ledger(options)
        ledger_option = options[:ledger] || options[:ledger_path]
        return ledger_option if ledger_option.is_a?(Ledger)
        if ledger_option
          Ledger.new(ledger_option.to_s, :id_generator => @id_generator)
        elsif @checkpoint_path
          Ledger.new(nil, :id_generator => @id_generator)
        else
          Ledger.new(nil, :id_generator => @id_generator)
        end
      end

      def load_checkpoint!
        state = JSON.parse(File.read(@checkpoint_path))
        if state["boundaries"] && !explicit_boundaries?(@options)
          @boundaries = Boundaries.new(state["boundaries"])
        end
        restore_scanner(decode_scanner(state["scanner"])) if state["scanner"]
        @spool.start_offset = state["retained_from"] if @spool.start_offset != state["retained_from"]
        @next_sequence = state["next_sequence"] || 0
        @doctype = state["doctype"]
        @options = merge_persisted_options(state["options"] || {}).merge(@options)
        @nori_options = merge_persisted_options(state["nori_options"] || {}).merge(@nori_options)
        @record_stack = Array(state["open_records"]).map { |hash| decode_open_record(hash) }
        @records = Array(state["records"]).map { |hash| decode_record(hash) }.compact
        @ledger = Ledger.new(nil, :id_generator => @id_generator)
        restore_ledger(state["ledger"]) if state["ledger"]
        @saved_position = state["stored_through"] || @spool.end_offset
      end

      def restore_scanner(snapshot)
        @scanner = Scanner.new(@boundaries, snapshot[:position] || 0).restore(snapshot)
      end

      def encode_scanner(snapshot)
        result = {}
        snapshot.each do |key, value|
          result[key.to_s] = value
        end
        result["pending"] = [snapshot[:pending]].pack("m0")
        result
      end

      def decode_scanner(hash)
        return nil unless hash
        snapshot = {}
        hash.each { |key, value| snapshot[key.to_sym] = value }
        snapshot[:pending] = (snapshot[:pending] || "").unpack1("m")
        snapshot[:pending] = snapshot[:pending].b
        snapshot
      end

      def encode_open_record(context)
        {
          "name" => context[:name],
          "path" => context[:path],
          "byte_start" => context[:byte_start],
          "sequence" => context[:sequence],
          "options" => Streaming.serializable_options(context[:options] || {}),
          "context" => {
            "namespaces" => context[:context][:namespaces],
            "xml_space" => context[:context][:xml_space]
          },
          "id" => context[:id]
        }
      end

      def decode_open_record(hash)
        {
          :name => hash["name"],
          :path => hash["path"],
          :byte_start => hash["byte_start"],
          :sequence => hash["sequence"],
          :options => merge_persisted_options(hash["options"] || {}),
          :context => {
            :namespaces => (hash["context"] || {})["namespaces"] || {},
            :xml_space => (hash["context"] || {})["xml_space"]
          },
          :id => hash["id"]
        }
      end

      def decode_record(hash)
        record = Record.new(
          :id => hash["id"],
          :sequence => hash["sequence"],
          :name => hash["name"],
          :path => hash["path"],
          :byte_start => hash["byte_start"],
          :byte_end => hash["byte_end"],
          :closed_at => hash["closed_at"],
          :options => merge_persisted_options(hash[:options] || hash["options"] || {}),
          :delivered => hash["delivered"],
          :acknowledged => hash["acknowledged"],
          :status => (hash["status"] || "delivered").to_sym,
          :parser => method(:parse_record),
          :context => {
            :namespaces => hash["context"] && hash["context"]["namespaces"] || {},
            :xml_space => hash["context"] && hash["context"]["xml_space"]
          },
          :digest => hash["digest"],
          :raw_bytes => nil,
          :raw_path => payload_path(hash["id"]),
          :value_path => value_payload_path(hash["id"])
        )
        record
      end

      def merge_persisted_options(persisted)
        Streaming.rehydrate_options(persisted).merge(Streaming.callable_options(@options || {}))
      end

      def restore_ledger(hash)
        @next_sequence = hash["next_sequence"].to_i if hash["next_sequence"]
        Array(hash["entries"]).each do |entry_hash|
          entry = Ledger::Entry.from_h(entry_hash)
          @ledger.send(:store_entry, entry)
        end
      end

      def process(events, &block)
        records = []
        events.each do |event|
          case event
          when Scanner::StartTag
            start_record(event)
          when Scanner::EndTag
            record = end_record(event)
            if record
              records << record
              notify(record, block)
            end
          when Scanner::Declaration
            @doctype = event.raw if event.kind == :doctype
          when Scanner::Text
            # The bytes are already in the spool; record extraction reads the
            # exact original range when the boundary closes.
          end
        end
        records
      end

      def start_record(event)
        boundary = @boundaries.match(event.path)
        return unless boundary

        sequence = next_sequence
        options = snapshot_options(sequence, event)
        @record_stack << {
          :name => event.name,
          :path => event.path.dup,
          :byte_start => event.offset,
          :sequence => sequence,
          :options => options,
          :context => {
            :namespaces => event.in_scope_namespaces.dup.freeze,
            :xml_space => event.in_scope_xml_space
          },
          :id => nil,
          :boundary => boundary
        }
      end

      def self.boundary_option(options)
        options[:records] || options[:record] || options[:record_name] ||
          options[:record_names] || options[:record_elements] || options[:record_element] ||
          options[:record_path] || options[:record_paths] ||
          options[:emit] || options[:emit_on] || options[:commit_on] ||
          options[:commit_at] || options[:commit_boundaries] ||
          options[:boundaries] || options[:boundary] || options[:boundary_path] ||
          options[:boundary_name] || options[:paths] || options[:names] ||
          options[:elements]
      end

      def end_record(event)
        context = @record_stack.last
        return nil unless context
        return nil unless context[:path] == event.path && context[:name] == event.name

        @record_stack.pop
        raw = raw_bytes(context[:byte_start], event.end_offset)
        context[:digest] = Digest::SHA256.hexdigest(raw)
        entry = @ledger.register(
          :sequence => context[:sequence],
          :name => context[:name],
          :path => context[:path],
          :byte_start => context[:byte_start],
          :byte_end => event.end_offset,
          :status => :delivered,
          :metadata => {
            :digest => context[:digest],
            :closed_at => Time.now.utc.iso8601,
            :options => Streaming.serializable_options(context[:options]),
            :namespaces => context[:context][:namespaces],
            :xml_space => context[:context][:xml_space]
          }
        )
        raw_path = persist_record_payload(entry.id, raw)
        value = parse_value(raw, context, entry.id)
        value_path = persist_record_value(entry.id, value, context[:options])
        record = Record.new(
          :id => entry.id,
          :sequence => context[:sequence],
          :name => context[:name],
          :path => context[:path],
          :byte_start => context[:byte_start],
          :byte_end => event.end_offset,
          :closed_at => Time.now.utc,
          :options => context[:options],
          :value => value,
          :status => :delivered,
          :parser => method(:parse_record),
          :digest => context[:digest],
          :raw_bytes => raw,
          :raw_path => raw_path,
          :value_path => value_path,
          :context => context[:context]
        )
        @records << record
        record
      end

      def next_sequence
        sequence = @next_sequence
        @next_sequence += 1
        sequence
      end

      def boundary_option(options)
        self.class.boundary_option(options)
      end

      def snapshot_options(sequence, event)
        base = @nori_options.dup
        if @option_provider && @option_provider.respond_to?(:call)
          supplied = @option_provider.call(
            :sequence => sequence,
            :name => event.name,
            :path => event.path.dup,
            :byte_start => event.offset,
            :options => base
          )
          if supplied.is_a?(Hash)
            supplied = Streaming.merge_nori_options(supplied)
            base = base.merge(supplied)
          end
        end
        base.dup
      end


      def raw_bytes(start, finish)
        if start < @spool.start_offset
          raise OffsetError, "Record bytes start at #{start}, but the retained window starts at #{@spool.start_offset}"
        end
        @spool.bytes(start, finish - start)
      end

      def payload_path(id)
        return nil unless @records_dir && id
        path = File.join(@records_dir, "#{id}.xml")
        File.exist?(path) ? path : nil
      end

      def value_payload_path(id)
        return nil unless @records_dir && id
        path = File.join(@records_dir, "#{id}.value")
        File.exist?(path) ? path : nil
      end

      def persist_record_payload(id, raw)
        return nil unless @records_dir
        path = File.join(@records_dir, "#{id}.xml")
        File.binwrite(path, raw)
        path
      end

      # A converter lambda cannot be written to a checkpoint. Reparsing the
      # retained bytes would then need the lambda again, so a record whose
      # options contain a callable also keeps the value it already produced.
      # Marshal covers Nori's value classes (StringWithAttributes, StringIOFile,
      # Time, Date, BigDecimal) as well as plain strings and hashes.
      def persist_record_value(id, value, options)
        return nil unless @records_dir
        return nil if Streaming.callable_options(options).empty?
        path = File.join(@records_dir, "#{id}.value")
        File.binwrite(path, Marshal.dump(value))
        path
      rescue TypeError
        nil
      end

      # Drop the retained copy of records that have already been acknowledged.
      # This is separate from reclaiming transmission bytes: an acknowledged
      # receipt normally no longer needs to be rebuilt, but the session keeps
      # its copy until the caller explicitly purges it.
      def purge_payloads!
        remove_acknowledged_payloads
        self
      end
      alias purge! purge_payloads!

      def remove_acknowledged_payloads
        return unless @records_dir && Dir.exist?(@records_dir)
        @ledger.entries.each do |entry|
          next unless entry.status.to_s == "acknowledged"
          [".xml", ".value"].each do |extension|
            path = File.join(@records_dir, "#{entry.id}#{extension}")
            File.delete(path) if File.exist?(path)
          end
        end
      end

      def parse_value(raw, context, id)
        options = context[:options]
        fragment = FragmentParser.new(options)
        value = fragment.parse(
          raw, context[:context], context[:name],
          :business_id => id, :prolog => preamble_for(context)
        )
        value
      rescue StandardError => e
        @parse_failures << e
        raise
      end

      def parse_record(record)
        context = record.context || {}
        raw = record.raw
        if raw.nil? && record.byte_start >= @spool.start_offset
          raw = @spool.bytes(record.byte_start, record.byte_end - record.byte_start)
        end
        return nil unless raw
        FragmentParser.new(record.options).parse(
          raw, context, record.name, :business_id => record.id
        )
      end

      # The record fragment needs the document type declaration when the
      # document defines internal entities or default attributes.
      def preamble_for(_context)
        @doctype
      end

      def notify(record, block)
        @on_record.call(record) if @on_record.respond_to?(:call)
        block.call(record) if block
      end

      def record_for_entry(entry)
        existing = @records.find { |record| record.id == entry.id }
        return existing if existing

        Record.new(
          :id => entry.id,
          :sequence => entry.sequence,
          :name => entry.name,
          :path => entry.path,
          :byte_start => entry.byte_start,
          :byte_end => entry.byte_end,
          :closed_at => entry.metadata && entry.metadata["closed_at"],
          :options => merge_persisted_options((entry.metadata || {})["options"] || {}),
          :acknowledged => entry.status.to_sym == :acknowledged,
          :status => entry.status.to_sym,
          :parser => method(:parse_record),
          :context => {
            :namespaces => (entry.metadata || {})["namespaces"] || {},
            :xml_space => (entry.metadata || {})["xml_space"]
          },
          :digest => (entry.metadata || {})["digest"],
          :raw_path => payload_path(entry.id)
        )
      end

      def reconstruct_record(entry)
        entry && record_for_entry(entry)
      end

      # A session can be created entirely in memory and given a checkpoint
      # path later. At that point it needs a durable spool next to the
      # checkpoint, otherwise a reload would see an empty window.
      def adopt_checkpoint_storage(path)
        return if @spool_path && @records_dir
        FileUtils.mkdir_p(File.dirname(path)) unless File.dirname(path) == "."
        @spool_path ||= "#{path}.bin"
        @records_dir ||= File.join(File.dirname(path), "records")
        FileUtils.mkdir_p(@records_dir)
        unless @spool.path
          @spool = Spool.new(@spool_path, @spool.start_offset, @spool.bytes)
        end
      end

      def persist_if_possible
        @ledger.save! if @ledger.path
        return unless @checkpoint_path
        dir = File.dirname(@checkpoint_path)
        FileUtils.mkdir_p(dir) unless dir == "." || Dir.exist?(dir)
        tmp = "#{@checkpoint_path}.tmp"
        File.write(tmp, JSON.pretty_generate(state))
        File.rename(tmp, @checkpoint_path)
        @saved_position = @spool.end_offset
      end

      def initialize_from_checkpoint(path, options)
        @checkpoint_path = path
        @options = options.dup
        validate_options!(options)
        state = JSON.parse(File.read(path))
        boundary_option = self.class.boundary_option(options) || state["boundaries"]
        @boundaries = Boundaries.new(boundary_option)
        @nori_options = Streaming.merge_nori_options(options)
        @option_provider = options[:option_provider] || options[:options_provider]
        @on_record = options[:on_record]
        @id_generator = options[:business_id] || options[:id_generator] || options[:id]
        @parse_failures = []

        spool_path = options[:spool] || options[:spool_dir] || options[:directory]
        spool_path = state["spool_path"] if spool_path.nil?
        spool_path = File.join(File.dirname(path), "stream.bin") if spool_path.nil?
        @spool_path = spool_path
        @spool = Spool.new(spool_path, state["retained_from"] || 0)
        @records_dir = File.join(File.dirname(spool_path), "records")
        FileUtils.mkdir_p(@records_dir)
        @ledger = Ledger.new(nil, :id_generator => @id_generator)
        restore_ledger(state["ledger"]) if state["ledger"]
        @next_sequence = state["next_sequence"] || 0
        @options = merge_persisted_options(state["options"] || {}).merge(options)
        @nori_options = merge_persisted_options(state["nori_options"] || {}).merge(Streaming.merge_nori_options(options))
        restore_scanner(decode_scanner(state["scanner"])) if state["scanner"]
        @record_stack = Array(state["open_records"]).map { |hash| decode_open_record(hash) }
        @records = Array(state["records"]).map { |hash| decode_record(hash) }.compact
        @doctype = state["doctype"]
        @saved_position = state["stored_through"] || @spool.end_offset
      end
    end

    Reader = Session
    Stream = Session
    IncrementalParser = Session
  end
end
