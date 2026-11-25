# frozen_string_literal: false
#
# Offline reconciliation of tuple hand-offs recorded across Rinda endpoints.
#
# Replays the order information an archive preserved; it does not re-run a
# live TupleSpace. Every time used is the log's confirmed clock, and leases
# are resolved with the lease lengths the log recorded. The only Rinda code
# borrowed is the tuple/template matcher. See RECONCILE.md for the model.
#
require 'json'
require_relative 'rinda'

module Rinda
  module Reconcile

    CALL_METHODS = %w[read read_all take move write].freeze

    class LedgerError < StandardError; end

    # Raised when no single story explains the archive plus observations.
    class Conflict < StandardError
      attr_reader :line, :evidence

      def initialize(message, line: nil, evidence: [])
        super(message)
        @line = line
        @evidence = evidence
      end
    end

    def self.deep_copy(value)
      Marshal.load(Marshal.dump(value))
    end

    # Content comparison by this repo's Template#match, falling back to ==.
    def self.same_value?(template_value, tuple_value)
      Rinda::Template.new(template_value).match(tuple_value)
    rescue StandardError
      template_value == tuple_value
    end

    # One recorded endpoint call.
    #
    # id, endpoint, method are required. Then, per method:
    #   write        : tuple; recv names the endpoint registration
    #   take         : template; recv names the receipt; recv_value is the
    #                  value the caller received (absent if the log only kept
    #                  the template)
    #   move         : template, port; recv / recv_value as for take
    #   read         : template; value when it returned
    #   read_all     : template
    # Optional: lease (seconds or true), expires_at (log clock), source
    # (original-write registration a receipt points at), delivered (for an
    # errored move: true/false when the port push is known, absent when not),
    # acquired (log time the read waiter re-took the monitor), error, start,
    # offset, seq.
    class Event
      attr_reader :id, :endpoint, :method, :template, :tuple, :lease, :expires_at,
                  :port, :recv, :recv_value, :source, :value, :acquired, :error,
                  :delivered, :seq, :offset, :raw
      attr_accessor :start

      def initialize(raw, seq)
        @raw        = raw
        @id         = raw['id'] or raise LedgerError, "call ##{seq}: missing id"
        @endpoint   = raw['endpoint'] or raise LedgerError, "call #{@id}: missing endpoint"
        @method     = raw['method'].to_s
        unless CALL_METHODS.include?(@method)
          raise LedgerError, "call #{@id}: unknown method #{@method.inspect}"
        end
        @template   = raw['template']
        @tuple      = raw.key?('tuple') ? Reconcile.deep_copy(raw['tuple']) : nil
        @lease      = raw['lease']
        @expires_at = raw['expires_at']
        @port       = raw['port']
        @recv       = raw['recv']
        @recv_value = raw.key?('recv_value') ? Reconcile.deep_copy(raw['recv_value']) : nil
        @source     = raw['source']
        @value      = raw.key?('value') ? Reconcile.deep_copy(raw['value']) : nil
        @acquired   = raw['acquired']
        @error      = raw['error']
        @delivered  = raw['delivered']
        @seq        = raw['seq'] || seq
        @offset     = raw['offset']
        @start      = raw['start']
        validate!
      end

      def returns?;  @error.nil?; end
      def raises?;   !returns?;   end
      def write?;    @method == 'write'; end
      def read?;     @method == 'read'; end
      def read_all?; @method == 'read_all'; end
      def take?;     @method == 'take'; end
      def move?;     @method == 'move'; end
      def transfer?; take? || move?; end
      def timed?;    !@start.nil?; end

      def finish
        return nil if @start.nil?
        @offset.nil? ? @start : @start + @offset
      end

      # Deterministic tie-break for calls the clock does not separate.
      def start_key
        @start.nil? ? [Float::INFINITY, @seq] : [@start, @seq]
      end

      def matches(value)
        Reconcile.same_value?(@template, value)
      end

      # Expiry on the log's own clock. A relative lease is anchored at the
      # recorded start, never at Time.now.
      def expiry
        return @expires_at unless @expires_at.nil?
        return nil if @lease.nil?
        return 0 if @lease == true
        return Float::INFINITY if @lease.nil?
        return nil if @lease.is_a?(Numeric) && @lease.positive? && @start.nil?
        @start.nil? ? nil : @start + @lease
      end

      # A record that is already dead when it is written never enters the bag.
      def expired_at_entry?
        return true if @lease == true
        return true if @lease.is_a?(Numeric) && @lease <= 0
        exp = expiry
        !exp.nil? && !@start.nil? && exp <= @start
      end

      # Live at log-time +t+?
      def alive_at?(t)
        exp = expiry
        exp.nil? || t < exp
      end

      def to_s; "#{@id}(#{@method}@#{@endpoint})"; end

      private

      def validate!
        if write? && returns? && @tuple.nil?
          raise LedgerError, "call #{@id}: completed write without a tuple"
        end
        if read? && returns? && !@raw.key?('value')
          raise LedgerError, "call #{@id}: completed read without a value"
        end
        if transfer? && returns? && @recv.nil?
          raise LedgerError, "call #{@id}: completed #{@method} without a receipt"
        end
        if transfer? && @template.nil?
          raise LedgerError, "call #{@id}: #{@method} without a template"
        end
        if move? && @port.nil?
          raise LedgerError, "call #{@id}: move without a port"
        end
        if @offset && @offset.negative?
          raise LedgerError, "call #{@id}: negative call duration"
        end
      end
    end

    # A parsed archive.
    class Ledger
      attr_reader :raw, :events, :windows, :summary, :observed, :endpoints, :clock

      def self.load(path)
        new(JSON.parse(File.read(path)))
      end

      def initialize(raw)
        @raw       = raw
        @clock     = raw['clock'] || 'log-monotonic'
        @endpoints = raw['endpoints'] || []
        @summary   = raw['summary'] || []
        @observed  = raw['observed'] || []
        starts     = raw['starts'] || {}
        @events    = (raw['calls'] || []).each_with_index.map do |call, i|
          ev = Event.new(call, i)
          ev.start = starts[ev.id] if ev.start.nil? && starts.key?(ev.id)
          ev
        end
        @by_id   = {}
        @events.each { |ev| @by_id[ev.id] = ev }
        @windows = (raw['windows'] || []).map { |w| resolve_window(w) }
      end

      def event(id); @by_id.fetch(id); end
      def has?(id);  @by_id.key?(id); end
      def read_sources; @raw['read_sources'] || {}; end
      def before; @raw['before'] || []; end

      private

      def resolve_window(raw)
        ids = raw.is_a?(Hash) ? (raw['events'] || raw.values.flatten) : Array(raw)
        { events: ids.map { |id| @by_id.fetch(id) } }
      end
    end

    # A forced precedence edge between calls.
    Edge = Struct.new(:from, :to, :why)

    # The partial order the archive preserved.
    class Order
      attr_reader :events, :edges

      def initialize(events, edges)
        @events = events
        @edges  = edges
        @succ   = Hash.new { |h, k| h[k] = [] }
        edges.each { |e| @succ[e.from] << e.to }
      end

      def before?(a, b)
        return true if a == b
        stack = @succ[a].dup
        seen = {}
        until stack.empty?
          cur = stack.pop
          return true if cur == b
          next if seen[cur]
          seen[cur] = true
          stack.concat(@succ[cur])
        end
        false
      end

      # A cycle leaves calls that Kahn cannot discharge.
      def cyclic?
        !residual.empty?
      end

      def residual
        @residual ||= begin
          indeg = Hash.new(0)
          @events.each { |ev| indeg[ev.id] = 0 }
          @edges.each { |e| indeg[e.to] += 1 }
          ready = @events.map(&:id).select { |id| indeg[id].zero? }
          discharged = 0
          until ready.empty?
            id = ready.shift
            discharged += 1
            @succ[id].each do |to|
              indeg[to] -= 1
              ready << to if indeg[to].zero?
            end
          end
          return [] if discharged == @events.size
          @events.map(&:id).select { |id| indeg[id].positive? }
        end
      end

      # Tiers of calls no forced edge separates. Calls the archive only knows
      # as concurrent share a tier.
      def tiers
        @tiers ||= begin
          indeg = Hash.new(0)
          @edges.each { |e| indeg[e.to] += 1 }
          tier = {}
          ready = @events.sort_by { |e| e.start_key }.map(&:id).select { |id| indeg[id].zero? }
          level = 0
          until ready.empty?
            ready.each { |id| tier[id] = level }
            nxt = []
            ready.each do |id|
              @succ[id].each do |to|
                indeg[to] -= 1
                nxt << to if indeg[to].zero?
              end
            end
            ready = nxt.uniq
            level += 1
          end
          tier
        end
      end

      def tier(id); tiers[id]; end

      # One concrete total order consistent with every forced edge. Extra
      # `pairs` add constraints for one story; `prefer` nudges the tie-break.
      def linearize(pairs: [], prefer: [])
        preference = prefer.each_with_index.to_h
        succ = @succ.transform_values(&:dup)
        indeg = Hash.new(0)
        @events.each { |ev| indeg[ev.id] = 0 }
        @edges.each { |e| indeg[e.to] += 1 }
        pairs.each do |from, to|
          next if from == to
          succ[from] = (succ[from] || []) + [to]
          indeg[to] += 1
        end

        ready = @events.map(&:id).select { |id| indeg[id].zero? }
        out = []
        until ready.empty?
          ready.sort_by! do |id|
            ev = @events.find { |e| e.id == id }
            [preference.fetch(id, Float::INFINITY), ev.start_key, id]
          end
          id = ready.shift
          out << id
          (succ[id] || []).each do |to|
            indeg[to] -= 1
            ready << to if indeg[to].zero?
          end
        end
        # A cycle introduced by `pairs` leaves events out; fall back.
        return linearize if out.size != @events.size && !pairs.empty?
        out
      end

      # Does this order place every forced edge the right way round?
      def respects?(ids)
        pos = ids.each_with_index.to_h
        @edges.all? do |e|
          pa = pos[e.from]
          pb = pos[e.to]
          pa.nil? || pb.nil? || pa < pb
        end
      end

      def edge_evidence
        @edges.map { |e| { from: e.from, to: e.to, why: e.why } }
      end
    end

    # A receipt: a transfer that deposited a value.
    #
    # `value` is what the port received (`recv_value`), else what a take
    # returned (`value`), else the read-back of the tuple the log recorded.
    class Receipt
      attr_reader :event, :label, :group

      def initialize(event, group)
        @event = event
        @label = event.recv
        @group = group
      end

      def id;       @event.id; end
      def endpoint; @event.endpoint; end

      def value
        return @event.recv_value unless @event.recv_value.nil?
        return @event.value unless @event.value.nil?
        @event.tuple
      end

      def explicit_value?
        !@event.recv_value.nil? || !@event.value.nil?
      end

      def shared?; @group.size > 1; end
      def to_s;    "#{id}[#{label}]"; end
    end

    # One receipt and the original writes that could have supplied it.
    class Attribution
      attr_reader :receipt, :sources

      def initialize(receipt, sources)
        @receipt = receipt
        @sources = sources
      end

      def ids; @sources.map(&:id).uniq; end
      def unresolved?; @sources.empty?; end
    end

    # A returning read and the writes whose value it could have observed.
    # `live` separates writes that were takeable from writes that were already
    # expired at entry: a waiting read can still be handed the latter's value,
    # but a transfer never could.
    class ReadObservation
      attr_reader :event, :sources

      def initialize(event, sources)
        @event = event
        @sources = sources
      end

      def id; @event.id; end
      def value; @event.value; end

      def live_sources
        @sources.reject(&:expired_at_entry?)
      end

      # A read whose only candidate writes were dead at entry observed a value
      # that no take could ever have removed.
      def expired_only?
        !@sources.empty? && live_sources.empty?
      end

      def ghost?
        expired_only?
      end
    end

    # Outcome of one replay.
    class Result
      attr_reader :ledger, :order, :attributions, :groups, :scenarios, :reads, :conflict

      def initialize(ledger:, order:, attributions:, groups:, scenarios:, conflict:, reads: [])
        @ledger = ledger
        @order = order
        @attributions = attributions
        @groups = groups
        @scenarios = scenarios
        @reads = reads
        @conflict = conflict
      end

      def replayable?; @conflict.nil?; end

      # verdict per disputed label
      def verdict(label); @groups[label] && @groups[label][:verdict]; end
      def duplicate_labels; @groups.select { |_l, g| g[:verdict] == :duplicate }.keys; end
      def distinct_labels;  @groups.select { |_l, g| g[:verdict] == :distinct }.keys; end
      def ambiguous_labels; @groups.select { |_l, g| g[:verdict] == :ambiguous }.keys; end
      def conflict_labels;  @groups.select { |_l, g| g[:verdict] == :conflict }.keys; end

      # A read that observed only already-expired writes: the value was
      # notified to the waiter but never entered the bag.
      def ghost_reads
        @reads.select(&:expired_only?)
      end

      def read(id)
        @reads.find { |r| r.id == id }
      end
    end

    # Result of replaying a summary file line by line.
    class SummaryResult
      attr_reader :steps, :failed_line, :failed_result, :last

      def initialize(steps, failed_line, failed_result, last)
        @steps = steps
        @failed_line = failed_line
        @failed_result = failed_result
        @last = last
      end

      def ok?; @failed_line.nil?; end
    end
  end
end

require_relative 'reconcile/engine' unless defined?(Rinda::Reconcile::Engine)
