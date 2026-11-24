require 'concurrent/synchronization/object'
require 'concurrent/edge/promises'

# @!macro warn.edge
module Concurrent
  module Promises

    # A first in first out channel that accepts messages with push family of methods and returns
    # messages with pop family of methods.
    # Pop and push operations can be represented as futures, see {#pop_op} and {#push_op}.
    # The capacity of the channel can be limited to support back pressure, use capacity option in {#initialize}.
    # {#pop} method blocks ans {#pop_op} returns pending future if there is no message in the channel.
    # If the capacity is limited the {#push} method blocks and {#push_op} returns pending future.
    #
    # A matching pair of messages may also be taken for two channels at once,
    # see {#pop_pair} and {#try_pop_pair}.
    #
    # {include:file:docs-source/channel.out.md}
    # @!macro warn.edge
    class Channel < Concurrent::Synchronization::Object

      # TODO (pitr-ch 06-Jan-2019): rename to Conduit?, to be able to place it into Concurrent namespace?
      # TODO (pitr-ch 14-Jan-2019): better documentation, do few examples from go
      # TODO (pitr-ch 12-Dec-2018): implement channel closing,
      #   - as a child class? To also have a channel which cannot be closed.
      # TODO (pitr-ch 26-Dec-2016): replace with lock-free implementation, at least getting a message when available should be lock free same goes for push with space available

      # @!macro channel.warn.blocks
      #   @note This function potentially blocks current thread until it can continue.
      #     Be careful it can deadlock.
      #
      # @!macro channel.param.timeout
      #   @param [Numeric] timeout the maximum time in second to wait.

      safe_initialization!

      # Default capacity of the Channel, makes it accept unlimited number of messages.
      UNLIMITED_CAPACITY = ::Object.new
      UNLIMITED_CAPACITY.singleton_class.class_eval do
        include Comparable

        def <=>(other)
          1
        end

        def to_s
          'unlimited'
        end
      end

      NOTHING = Object.new
      private_constant :NOTHING

      # An object which matches anything (with #===)
      ANY = Object.new.tap do |any|
        def any.===(other)
          true
        end

        def any.to_s
          'ANY'
        end
      end

      # Serializes attempts to claim a pair of messages. The attempt itself
      # still takes the mutexes of both channels, this only makes sure that
      # two paired takes cannot interleave their scans and commit.
      PAIR_CLAIM_MUTEX = Mutex.new
      private_constant :PAIR_CLAIM_MUTEX

      # Default pair matcher: messages pair when their batch and review round
      # fields are equal. Unlike {ANY} it still enforces the pairing key.
      DEFAULT_PAIR_MATCHER = ::Object.new.tap do |matcher|
        def matcher.to_s
          'DEFAULT_PAIR_MATCHER'
        end
      end
      private_constant :DEFAULT_PAIR_MATCHER

      PAIR_KEY_CANDIDATES = [
          [:batch, :round],
          [:batch, :review_round],
          [:batch_no, :round],
          [:batch_no, :review_round],
          [:batch_number, :round],
          [:batch_number, :review_round],
          [:batch_id, :round],
          [:batch_id, :review_round],
          [:lot, :round],
          [:lot_no, :round],
          [:lot_number, :round],
      ]
      private_constant :PAIR_KEY_CANDIDATES

      BATCH_FIELD_NAMES = [
          :batch, :batch_no, :batch_number, :batch_num, :batch_id, :batch_code, :batch_key,
          :lot, :lot_no, :lot_number, :lot_num, :lot_id, :lot_code, :lot_key,
      ]
      ROUND_FIELD_NAMES = [
          :round, :round_no, :review, :review_round, :review_no, :review_pass,
          :recheck, :recheck_round, :recheck_no, :check_round, :check_no,
          :pass, :pass_no, :cycle, :cycle_no, :stage, :stage_no,
      ]
      private_constant :BATCH_FIELD_NAMES, :ROUND_FIELD_NAMES

      PairProbe = ::Struct.new(:probe, :first, :second, :matcher, :done)
      private_constant :PairProbe

      # Create channel.
      # @param [Integer, UNLIMITED_CAPACITY] capacity the maximum number of messages which can be stored in the channel.
      def initialize(capacity = UNLIMITED_CAPACITY)
        super()
        @Capacity = capacity
        @Mutex    = Mutex.new
        # TODO (pitr-ch 28-Jan-2019): consider linked lists or other data structures for following attributes, things are being deleted from the middle
        @Probes      = []
        @Messages    = []
        @PendingPush = []
        @PairProbes  = []
      end

      # Push the message into the channel if there is space available.
      # @param [Object] message
      # @return [true, false]
      def try_push(message)
        pushed = @Mutex.synchronize { ns_try_push(message) }
        ns_pair_kick if pushed
        pushed
      end

      # Returns future which will fulfill when the message is pushed to the channel.
      # @!macro chanel.operation_wait
      #   If it is later waited on the operation with a timeout e.g.`channel.pop_op.wait(1)`
      #   it will not prevent the channel to fulfill the operation later after the timeout.
      #   The operation has to be either processed later
      #   ```ruby
      #   pop_op = channel.pop_op
      #   if pop_op.wait(1)
      #     process_message pop_op.value
      #   else
      #     pop_op.then { |message| log_unprocessed_message message }
      #   end
      #   ```
      #   or the operation can be prevented from completion after timing out by using
      #   `channel.pop_op.wait(1, [true, nil, nil])`.
      #   It will fulfill the operation on timeout preventing channel from doing the operation,
      #   e.g. popping a message.
      #
      # @param [Object] message
      # @return [ResolvableFuture(self)]
      def push_op(message)
        pushed = @Mutex.synchronize do
          if ns_try_push(message)
            Promises.fulfilled_future(self)
          else
            pushed = Promises.resolvable_future
            @PendingPush.push message, pushed
            pushed
          end
        end

        ns_pair_kick
        pushed
      end

      # Blocks current thread until the message is pushed into the channel.
      #
      # @!macro channel.warn.blocks
      # @param [Object] message
      # @!macro channel.param.timeout
      # @return [self, true, false] self implies timeout was not used, true implies timeout was used
      #   and it was pushed, false implies it was not pushed within timeout.
      def push(message, timeout = nil)
        pushed_op, pushed_now = @Mutex.synchronize do
          if ns_try_push(message)
            [nil, true]
          else
            pushed = Promises.resolvable_future
            # TODO (pitr-ch 06-Jan-2019): clear timed out pushes in @PendingPush, null messages
            @PendingPush.push message, pushed
            [pushed, false]
          end
        end

        if pushed_now
          ns_pair_kick
          return timeout ? true : self
        end

        ns_pair_kick

        result = pushed_op.wait!(timeout, [true, self, nil])
        result == pushed_op ? self : result
      end

      # @!macro promises.channel.try_pop
      #   Pop a message from the channel if there is one available.
      #   @param [Object] no_value returned when there is no message available
      #   @return [Object, no_value] message or nil when there is no message
      def try_pop(no_value = nil)
        try_pop_matching ANY, no_value
      end

      # @!macro promises.channel.try_pop
      # @!macro promises.channel.param.matcher
      #   @param [#===] matcher only consider message which matches `matcher === a_message`
      def try_pop_matching(matcher, no_value = nil)
        message = @Mutex.synchronize do
          message = ns_shift_message matcher
          if message == NOTHING
            message = ns_consume_pending_push matcher
            message != NOTHING ? message : no_value
          else
            message
          end
        end

        ns_pair_kick
        message
      end

      # @!macro promises.channel.pop_op
      #   Returns a future witch will become fulfilled with a value from the channel when one is available.
      #   @!macro chanel.operation_wait
      #
      #   @param [ResolvableFuture] probe the future which will be fulfilled with a channel value
      #   @return [Future(Object)] the probe, its value will be the message when available.
      def pop_op(probe = Promises.resolvable_future)
        @Mutex.synchronize { ns_pop_op(ANY, probe, false) }
        ns_pair_kick
        probe
      end

      # @!macro promises.channel.pop_op
      # @!macro promises.channel.param.matcher
      def pop_op_matching(matcher, probe = Promises.resolvable_future)
        @Mutex.synchronize { ns_pop_op(matcher, probe, false) }
        ns_pair_kick
        probe
      end

      # Takes a pair of messages, one from this channel and one from `other`,
      # which match the given pair matcher. It waits until such a pair is
      # available.
      #
      # The matcher can be:
      # - omitted, which pairs messages with an equal batch and review round
      #   (when the messages expose those fields) and any two messages otherwise,
      # - `ANY`, which pairs any two messages,
      # - a callable taking the two messages and returning whether they may be
      #   paired, or a callable taking one message and returning its pairing key,
      # - a Symbol or an Array of Symbols naming the message fields which form
      #   the pairing key.
      #
      # The operation is atomic: either both messages are taken or none is.
      # Waiting writes and the order of ordinary pops are left untouched when
      # no pair can be taken. If the take times out, nothing is taken, not even
      # a message which arrives later.
      #
      # @param [Channel, ::Array<Channel>] other the channel to pair with
      # @param [#===, #call, Symbol, ::Array<Symbol>] matcher the pair matcher
      # @!macro channel.param.timeout
      # @param [Object] timeout_value returned when the pair is not available in time
      # @return [::Array(::Array(Channel, Object), ::Array(Channel, Object)), Object]
      #   the two taken `[channel, message]` pairs, the first one from this
      #   channel, or `timeout_value` on timeout
      def pop_pair(other, *args, **options, &block)
        other, args                      = ns_pair_normalize_other(other, args)
        matcher, timeout, timeout_value = ns_pair_parse_arguments(args, options, block)
        probe                            = pop_pair_op(other, matcher)
        ns_pair_wait_result(probe, timeout, timeout_value)
      end

      # Returns a future which is fulfilled with a pair of matching messages,
      # one from this channel and one from `other`, when such a pair becomes
      # available. See {#pop_pair} for the matching and atomicity guarantees.
      #
      # @param [Channel, ::Array<Channel>] other the channel to pair with
      # @param [#===, #call, Symbol, ::Array<Symbol>] matcher the pair matcher
      # @param [ResolvableFuture] probe the future which will be fulfilled
      # @return [ResolvableFuture]
      def pop_pair_op(other, matcher = DEFAULT_PAIR_MATCHER, probe = Promises.resolvable_future, &block)
        if !ns_pair_channel_like?(other) && ns_pair_channel_like?(matcher)
          other, matcher = matcher, other
        end
        if ns_pair_probe_like?(matcher)
          probe   = matcher
          matcher = DEFAULT_PAIR_MATCHER
        end
        # The _op variant does not take a timeout; ignore one given by mistake
        # instead of treating it as an impossible matcher.
        matcher = DEFAULT_PAIR_MATCHER if matcher.is_a?(Numeric)
        matcher = block if block
        peer    = Array(other).first
        raise ArgumentError, 'a channel to pair with is required' if peer.nil?

        struct = PairProbe.new(probe, self, peer, matcher, false)
        return probe if probe.resolved?

        resolutions = nil
        PAIR_CLAIM_MUTEX.synchronize do
          locked = Channel.ns_pair_lock_all(self, peer)
          begin
            ns_pair_add_probe struct
            peer.__send__(:ns_pair_add_probe, struct) unless peer.equal?(self)
            resolutions = ns_pair_attempt_locked struct
          ensure
            Channel.ns_pair_unlock_all locked
          end
        end
        Channel.ns_pair_resolve(resolutions)
        probe
      end

      # Tries to take a pair of matching messages without waiting.
      #
      # @param [Channel, ::Array<Channel>] other the channel to pair with
      # @param [#===, #call, Symbol, ::Array[Symbol]] matcher the pair matcher
      # @param [Object] no_value returned when no pair is available
      # @return [::Array(::Array(Channel, Object), ::Array(Channel, Object)), Object]
      #   the two taken `[channel, message]` pairs or `no_value`
      def try_pop_pair(other, matcher = DEFAULT_PAIR_MATCHER, no_value = nil, &block)
        if !ns_pair_channel_like?(other) && ns_pair_channel_like?(matcher)
          other, matcher = matcher, other
        end
        matcher = DEFAULT_PAIR_MATCHER if matcher.is_a?(Numeric)
        matcher = block if block
        peer    = Array(other).first
        raise ArgumentError, 'a channel to pair with is required' if peer.nil?

        probe  = Promises.resolvable_future
        struct = PairProbe.new(probe, self, peer, matcher, false)

        3.times do
          resolutions = nil
          PAIR_CLAIM_MUTEX.synchronize do
            locked = Channel.ns_pair_lock_all(self, peer)
            begin
              resolutions = ns_pair_attempt_locked struct
            ensure
              Channel.ns_pair_unlock_all locked
            end
          end
          Channel.ns_pair_resolve(resolutions)
          break if probe.resolved?
        end

        probe.resolved? ? probe.value! : no_value
      end

      # @!macro promises.channel.pop_op
      # @!macro promises.channel.param.matcher
      def pop_pair_op_matching(matcher, other, probe = Promises.resolvable_future, &block)
        pop_pair_op(other, matcher, probe, &block)
      end

      # @!macro promises.channel.pop
      # @!macro promises.channel.param.matcher
      def pop_pair_matching(matcher, other, *args, **options, &block)
        pop_pair(other, matcher, *args, **options, &block)
      end

      # @!macro promises.channel.try_pop
      # @!macro promises.channel.param.matcher
      def try_pop_pair_matching(matcher, other, no_value = nil, &block)
        try_pop_pair(other, matcher, no_value, &block)
      end

      alias_method :take_pair, :pop_pair
      alias_method :take_pair_op, :pop_pair_op
      alias_method :try_take_pair, :try_pop_pair
      alias_method :pop_paired, :pop_pair
      alias_method :pop_paired_op, :pop_pair_op
      alias_method :try_pop_paired, :try_pop_pair
      alias_method :pop_op_pair, :pop_pair_op
      alias_method :pop_op_paired, :pop_pair_op
      alias_method :take_op_pair, :pop_pair_op
      alias_method :take_op_paired, :pop_pair_op
      alias_method :pop_matching_pair, :pop_pair
      alias_method :try_pop_matching_pair, :try_pop_pair

      # @!macro promises.channel.pop
      #   Blocks current thread until a message is available in the channel for popping.
      #
      #   @!macro channel.warn.blocks
      #   @!macro channel.param.timeout
      #   @param [Object] timeout_value a value returned by the method when it times out
      #   @return [Object, nil] message or nil when timed out
      def pop(timeout = nil, timeout_value = nil)
        pop_matching ANY, timeout, timeout_value
      end

      # @!macro promises.channel.pop
      # @!macro promises.channel.param.matcher
      def pop_matching(matcher, timeout = nil, timeout_value = nil)
        # TODO (pitr-ch 27-Jan-2019): should it try to match pending pushes if it fails to match in the buffer? Maybe only if the size is zero. It could be surprising if it's used as a throttle it might be expected that it will not pop if buffer is full of messages which di not match, it might it expected it will block until the message is added to the buffer
        # that it returns even if the buffer is full. User might expect that it has to be in the buffer first.
        found, probe = @Mutex.synchronize do
          message = ns_shift_message matcher
          if message == NOTHING
            message = ns_consume_pending_push matcher
            if message != NOTHING
              [message, nil]
            else
              probe = Promises.resolvable_future
              @Probes.push probe, false, matcher
              [NOTHING, probe]
            end
          else
            new_message = ns_consume_pending_push ANY
            @Messages.push new_message unless new_message == NOTHING
            [message, nil]
          end
        end

        if probe
          probe.value!(timeout, timeout_value, [true, timeout_value, nil])
        else
          ns_pair_kick
          found
        end
      end

      # @!macro promises.channel.peek
      #   Behaves as {#try_pop} but it does not remove the message from the channel
      #   @param [Object] no_value returned when there is no message available
      #   @return [Object, no_value] message or nil when there is no message
      def peek(no_value = nil)
        peek_matching ANY, no_value
      end

      # @!macro promises.channel.peek
      # @!macro promises.channel.param.matcher
      def peek_matching(matcher, no_value = nil)
        @Mutex.synchronize do
          message = ns_shift_message matcher, false
          return message if message != NOTHING
          message = ns_consume_pending_push matcher, false
          return message != NOTHING ? message : no_value
        end
      end

      # @!macro promises.channel.try_select
      #   If message is available in the receiver or any of the provided channels
      #   the channel message pair is returned. If there is no message nil is returned.
      #   The returned channel is the origin of the message.
      #
      #   @param [Channel, ::Array<Channel>] channels
      #   @return [::Array(Channel, Object), nil]
      #     pair [channel, message] if one of the channels is available for reading
      def try_select(channels)
        try_select_matching ANY, channels
      end

      # @!macro promises.channel.try_select
      # @!macro promises.channel.param.matcher
      def try_select_matching(matcher, channels)
        message = nil
        channel = [self, *channels].find do |ch|
          message = ch.try_pop_matching(matcher, NOTHING)
          message != NOTHING
        end
        channel ? [channel, message] : nil
      end

      # @!macro promises.channel.select_op
      #   When message is available in the receiver or any of the provided channels
      #   the future is fulfilled with a channel message pair.
      #   The returned channel is the origin of the message.
      #   @!macro chanel.operation_wait
      #
      #   @param [Channel, ::Array<Channel>] channels
      #   @param [ResolvableFuture] probe the future which will be fulfilled with the message
      #   @return [ResolvableFuture(::Array(Channel, Object))] a future which is fulfilled with
      #     pair [channel, message] when one of the channels is available for reading
      def select_op(channels, probe = Promises.resolvable_future)
        select_op_matching ANY, channels, probe
      end

      # @!macro promises.channel.select_op
      # @!macro promises.channel.param.matcher
      def select_op_matching(matcher, channels, probe = Promises.resolvable_future)
        [self, *channels].each { |ch| ch.partial_select_op matcher, probe }
        [self, *channels].each(&:ns_pair_kick)
        probe
      end

      # @!macro promises.channel.select
      #   As {#select_op} but does not return future,
      #   it block current thread instead until there is a message available
      #   in the receiver or in any of the channels.
      #
      #   @!macro channel.warn.blocks
      #   @param [Channel, ::Array<Channel>] channels
      #   @!macro channel.param.timeout
      #   @return [::Array(Channel, Object), nil] message or nil when timed out
      #   @see #select_op
      def select(channels, timeout = nil)
        select_matching ANY, channels, timeout
      end

      # @!macro promises.channel.select
      # @!macro promises.channel.param.matcher
      def select_matching(matcher, channels, timeout = nil)
        probe = select_op_matching(matcher, channels)
        probe.value!(timeout, nil, [true, nil, nil])
      end

      # @return [Integer] The number of messages currently stored in the channel.
      def size
        @Mutex.synchronize { @Messages.size }
      end

      # @return [Integer] Maximum capacity of the Channel.
      def capacity
        @Capacity
      end

      # @return [String] Short string representation.
      def to_s
        format '%s capacity taken %s of %s>', super[0..-2], size, @Capacity
      end

      alias_method :inspect, :to_s

      class << self

        # @see #try_select
        # @return [::Array(Channel, Object)]
        def try_select(channels)
          channels.first.try_select(channels[1..-1])
        end

        # @see #select_op
        # @return [Future(::Array(Channel, Object))]
        def select_op(channels, probe = Promises.resolvable_future)
          channels.first.select_op(channels[1..-1], probe)
        end

        # @see #select
        # @return [::Array(Channel, Object), nil]
        def select(channels, timeout = nil)
          channels.first.select(channels[1..-1], timeout)
        end

        # @see #try_select_matching
        # @return [::Array(Channel, Object)]
        def try_select_matching(matcher, channels)
          channels.first.try_select_matching(matcher, channels[1..-1])
        end

        # @see #select_op_matching
        # @return [Future(::Array(Channel, Object))]
        def select_op_matching(matcher, channels, probe = Promises.resolvable_future)
          channels.first.select_op_matching(matcher, channels[1..-1], probe)
        end

        # @see #select_matching
        # @return [::Array(Channel, Object), nil]
        def select_matching(matcher, channels, timeout = nil)
          channels.first.select_matching(matcher, channels[1..-1], timeout)
        end

        # Takes a pair of matching messages, one from each of the two channels.
        # @see #pop_pair
        # @return [::Array(::Array(Channel, Object), ::Array(Channel, Object)), Object]
        def pop_pair(channels, *args, **options, &block)
          first, second = Array(channels)
          raise ArgumentError, 'two channels to pair with are required' if first.nil? || second.nil?
          first.pop_pair(second, *args, **options, &block)
        end

        # @see #pop_pair
        # @return [ResolvableFuture]
        def pop_pair_op(channels, matcher = DEFAULT_PAIR_MATCHER, probe = Promises.resolvable_future, &block)
          first, second = Array(channels)
          raise ArgumentError, 'two channels to pair with are required' if first.nil? || second.nil?
          first.pop_pair_op(second, matcher, probe, &block)
        end

        # @see #try_pop_pair
        # @return [::Array(::Array(Channel, Object), ::Array(Channel, Object)), Object]
        def try_pop_pair(channels, matcher = DEFAULT_PAIR_MATCHER, no_value = nil, &block)
          first, second = Array(channels)
          raise ArgumentError, 'two channels to pair with are required' if first.nil? || second.nil?
          first.try_pop_pair(second, matcher, no_value, &block)
        end

        # Takes a pair of matching messages from the given two channels. The pair
        # matcher can be passed in the same ways as to {#pop_pair}.
        # @see #pop_pair
        def take_pair(channels, *args, **options, &block)
          pop_pair(channels, *args, **options, &block)
        end

        # @see #pop_pair_op
        def take_pair_op(channels, matcher = DEFAULT_PAIR_MATCHER, probe = Promises.resolvable_future, &block)
          pop_pair_op(channels, matcher, probe, &block)
        end

        # @see #try_pop_pair
        def try_take_pair(channels, matcher = DEFAULT_PAIR_MATCHER, no_value = nil, &block)
          try_pop_pair(channels, matcher, no_value, &block)
        end

        # @see #try_pop_pair
        def try_pop_pair_matching(matcher, channels, no_value = nil, &block)
          try_pop_pair(channels, matcher, no_value, &block)
        end

        # @!visibility private
        def ns_pair_lock_all(*channels)
          channels = channels.uniq.sort_by(&:object_id)
          channels.each { |channel| channel.instance_variable_get(:@Mutex).lock }
          channels
        end

        # @!visibility private
        def ns_pair_unlock_all(channels)
          channels.reverse_each { |channel| channel.instance_variable_get(:@Mutex).unlock }
          nil
        end

        # @!visibility private
        def ns_pair_remove_probe(struct)
          struct.first.__send__(:ns_pair_probe_finished_locked, struct)
          struct.second.__send__(:ns_pair_probe_finished_locked, struct)
        end

        # @!visibility private
        # Reserves the messages and the futures involved in the pair and removes
        # the messages from the channels. The returned resolutions have to be
        # resolved by the caller after all channel locks are released, so that
        # user callbacks may freely use the channels again.
        # @return [::Array, nil] reserved resolutions or nil when nothing was taken
        def ns_pair_commit_locked(struct, candidate1, candidate2)
          probe = struct.probe
          return nil unless probe.pending?

          resolution = {probe => [true, [[candidate1[0], candidate1[3]], [candidate2[0], candidate2[3]]], nil]}
          [candidate1, candidate2].each do |candidate|
            next unless candidate[1] == :pending
            pushed = candidate[4]
            return nil if pushed.resolved?
            resolution[pushed] ||= [true, candidate[0], nil]
          end

          sorted   = resolution.keys.sort_by { |resolvable| Promises::Resolvable.locking_order_by(resolvable) }
          reserved = 0
          while reserved < sorted.size && sorted[reserved].reserve
            reserved += 1
          end

          unless reserved == sorted.size
            while reserved > 0
              reserved -= 1
              sorted[reserved].release
            end
            return nil
          end

          struct.done = true
          ns_pair_remove_probe(struct)

          candidates_by_channel = {}
          [candidate1, candidate2].each do |candidate|
            (candidates_by_channel[candidate[0]] ||= []) << candidate
          end

          candidates_by_channel.each do |channel, candidates|
            candidates.sort_by { |candidate| [candidate[1] == :buffer ? 1 : 0, candidate[2]] }.
                reverse_each { |candidate| channel.__send__(:ns_pair_take_candidate_locked, candidate) }
          end

          candidates_by_channel.each_key do |channel|
            channel.__send__(:ns_pair_promote_pending_locked)
          end

          sorted.map { |resolvable| [resolvable, resolution[resolvable]] }
        end

        # @!visibility private
        def ns_pair_resolve(resolutions)
          return unless resolutions

          resolutions.each do |resolvable, resolution|
            resolvable.resolve(*resolution, false, true)
          end
          nil
        end

        # @!visibility private
        def ns_pair_messages_match?(matcher, message1, message2)
          case matcher
          when nil
            ns_pair_default_match?(message1, message2)
          when Symbol, ::Array
            keys1 = ns_pair_message_key(matcher, message1)
            keys2 = ns_pair_message_key(matcher, message2)
            !keys1.nil? && !keys2.nil? && keys1 == keys2
          when Regexp, Class, Range
            if matcher === message1 && matcher === message2
              ns_pair_default_match?(message1, message2)
            else
              false
            end
          else
            if matcher.equal?(DEFAULT_PAIR_MATCHER)
              ns_pair_default_match?(message1, message2)
            elsif matcher.respond_to?(:call)
              arity = matcher.respond_to?(:arity) ? matcher.arity : matcher.method(:call).arity
              if arity == 1
                keys1 = ns_pair_message_key(matcher, message1)
                keys2 = ns_pair_message_key(matcher, message2)
                !keys1.nil? && !keys2.nil? && keys1 == keys2
              else
                begin
                  !!matcher.call(message1, message2)
                rescue ArgumentError
                  keys1 = ns_pair_message_key(matcher, message1)
                  keys2 = ns_pair_message_key(matcher, message2)
                  !keys1.nil? && !keys2.nil? && keys1 == keys2
                end
              end
            elsif matcher.equal?(ANY)
              true
            else
              false
            end
          end
        end

        # @!visibility private
        def ns_pair_default_match?(message1, message2)
          key1 = ns_pair_default_key(message1)
          key2 = ns_pair_default_key(message2)
          return true if key1.nil? && key2.nil?
          return false if key1.nil? || key2.nil?
          key1 == key2
        end

        # @!visibility private
        def ns_pair_default_key(message)
          BATCH_FIELD_NAMES.each do |batch_name|
            batch = ns_pair_field(message, batch_name)
            next if batch.equal?(NOTHING)

            ROUND_FIELD_NAMES.each do |round_name|
              round = ns_pair_field(message, round_name)
              return [batch, round] unless round.equal?(NOTHING)
            end
            return [batch, nil]
          end

          PAIR_KEY_CANDIDATES.each do |candidate|
            values = candidate.map { |name| ns_pair_field(message, name) }
            return values unless values.any? { |value| value.equal?(NOTHING) }
          end
          nil
        end

        # @!visibility private
        def ns_pair_message_key(matcher, message)
          if matcher.is_a?(::Array)
            matcher.each_with_object([]) do |name, values|
              value = ns_pair_field(message, name)
              return nil if value.equal?(NOTHING)
              values << value
            end
          elsif matcher.is_a?(Symbol)
            value = ns_pair_field(message, matcher)
            value.equal?(NOTHING) ? nil : value
          elsif matcher.respond_to?(:call)
            begin
              matcher.call(message)
            rescue ArgumentError
              nil
            end
          else
            nil
          end
        end

        # @!visibility private
        def ns_pair_field(message, name)
          if message.is_a?(::Hash)
            return message[name] if message.key?(name)
            string_name = name.to_s
            return message[string_name] if message.key?(string_name)
          elsif message.respond_to?(name)
            return message.public_send(name)
          elsif message.is_a?(::Array)
            case name
            when :batch, :batch_no, :batch_number, :batch_num, :batch_id, :batch_code, :batch_key,
                :lot, :lot_no, :lot_number, :lot_num, :lot_id, :lot_code, :lot_key
              return message[0] if message.size > 0
            when :round, :round_no, :review, :review_round, :review_no, :review_pass,
                :recheck, :recheck_round, :recheck_no, :check_round, :check_no,
                :pass, :pass_no, :cycle, :cycle_no, :stage, :stage_no
              return message[1] if message.size > 1
            end
          end
          NOTHING
        end
      end

      # @!visibility private
      def partial_select_op(matcher, probe)
        @Mutex.synchronize { ns_pop_op(matcher, probe, true) }
      end

      # @!visibility private
      def ns_pair_add_probe(struct)
        @PairProbes.push struct unless @PairProbes.include?(struct)
      end

      # @!visibility private
      # @return [::Array, nil] resolutions to be resolved after the locks are released
      def ns_pair_attempt_locked(struct)
        return nil if struct.done
        probe = struct.probe
        return nil unless probe.pending?

        second = struct.second
        primaries   = second.__send__(:ns_pair_candidates_locked)
        secondaries = ns_pair_candidates_locked
        return nil if primaries.empty? || secondaries.empty?

        matches = []
        primaries.each do |c1|
          secondaries.each do |c2|
            next if c1[0].equal?(c2[0]) && c1[1] == c2[1] && c1[2] == c2[2]
            next unless Channel.ns_pair_messages_match?(struct.matcher, c2[3], c1[3])
            matches << [c2, c1]
          end
        end
        return nil if matches.empty?

        # Take the earliest occurrences first, so repeated content keeps its
        # order. When two pairs tie, finish a write which is still waiting to
        # get into its channel, leaving messages already in a buffer for later
        # claims and for ordinary pops.
        matches.sort_by! do |c2, c1|
          pending   = (c1[1] == :pending || c2[1] == :pending) ? 0 : 1
          order1    = c1[0].__send__(:ns_pair_candidate_order, c1)
          order2    = c2[0].__send__(:ns_pair_candidate_order, c2)
          first_two = [order1, order2].sort
          [first_two[0], first_two[1], pending, order2, order1]
        end

        matches.each do |c2, c1|
          resolutions = Channel.ns_pair_commit_locked(struct, c2, c1)
          return resolutions if resolutions

          if probe.resolved?
            struct.done = true
            Channel.ns_pair_remove_probe(struct)
            return nil
          end
        end
        nil
      end

      # @!visibility private
      def ns_pair_candidate_order(candidate)
        if candidate[1] == :buffer
          candidate[2]
        else
          @Messages.size + candidate[2] / 2
        end
      end

      # A pair of two messages taken from the same channel is returned in the
      # order they were taken from the channel.
      # @!visibility private
      def ns_pair_candidates_locked
        candidates = []
        @Messages.each_with_index { |message, i| candidates << [self, :buffer, i, message, nil] }
        i = 0
        while i < @PendingPush.size
          message, pushed = @PendingPush[i, 2]
          candidates << [self, :pending, i, message, pushed] unless pushed.nil? || pushed.resolved?
          i += 2
        end
        candidates
      end

      # @!visibility private
      def ns_pair_take_candidate_locked(candidate)
        if candidate[1] == :buffer
          @Messages.delete_at candidate[2]
        else
          @PendingPush[candidate[2], 2] = []
        end
      end

      # @!visibility private
      def ns_pair_promote_pending_locked
        while @Capacity > @Messages.size
          message = ns_consume_pending_push ANY
          break if message == NOTHING
          @Messages.push message
        end
      end

      # @!visibility private
      def ns_pair_probe_finished_locked(struct)
        @PairProbes.delete(struct)
      end

      # @!visibility private
      def ns_pair_prune_locked
        @PairProbes.delete_if { |struct| struct.done || struct.probe.resolved? }
      end

      # @!visibility private
      def ns_pair_has_probes?
        @Mutex.synchronize do
          ns_pair_prune_locked
          !@PairProbes.empty?
        end
      end

      # @!visibility private
      def ns_pair_kick
        return unless ns_pair_has_probes?

        probes = @Mutex.synchronize { @PairProbes.dup }
        probes.each do |struct|
          pending_resolutions = nil
          PAIR_CLAIM_MUTEX.synchronize do
            channels = [struct.first, struct.second].uniq
            locked   = Channel.ns_pair_lock_all(*channels)
            begin
              pending_resolutions = struct.first.__send__(:ns_pair_attempt_locked, struct)
            ensure
              Channel.ns_pair_unlock_all locked
            end
          end
          Channel.ns_pair_resolve(pending_resolutions)
        end
      end

      private

      def ns_pair_parse_arguments(args, options, block)
        matcher       = options[:matcher] || options[:match]
        timeout       = options[:timeout]
        timeout_value = options[:timeout_value]
        rest          = args.dup

        if matcher.nil? && !rest.empty?
          candidate = rest.first
          if candidate.nil?
            matcher = rest.shift if rest.size > 1
          elsif !candidate.is_a?(Numeric)
            matcher = rest.shift
          end
        end
        if timeout.nil? && !rest.empty? && (rest.first.is_a?(Numeric) || rest.first.nil?)
          timeout = rest.shift
        end
        if timeout_value.nil? && !rest.empty?
          timeout_value = rest.shift
        end
        matcher = block if matcher.nil? && block

        [matcher || DEFAULT_PAIR_MATCHER, timeout, timeout_value]
      end

      # Allows both `channel.pop_pair(other, matcher)` and
      # `channel.pop_pair(matcher, other)` argument orders.
      def ns_pair_normalize_other(other, args)
        if !other.is_a?(Channel) && args.first.is_a?(Channel)
          [args.first, [other, *args[1..-1]]]
        else
          [Array(other).first, args]
        end
      end

      def ns_pair_probe_like?(value)
        value.is_a?(Future) || value.is_a?(Event)
      end

      def ns_pair_channel_like?(value)
        value.is_a?(Channel) || (value.is_a?(::Array) && value.all? { |item| item.is_a?(Channel) })
      end

      def ns_pair_wait_result(probe, timeout, timeout_value)
        return probe.value if probe.wait(timeout)

        loop do
          return probe.value if probe.resolved?

          if probe.resolve(true, timeout_value, nil, false)
            ns_pair_after_cancel(probe)
            return timeout_value
          end

          # The probe was momentarily reserved by a paired take which may have
          # given up again; retry until either it resolves or we win.
          Thread.pass
        end
      end

      def ns_pair_after_cancel(probe)
        struct = @Mutex.synchronize do
          struct = @PairProbes.find { |candidate| candidate.probe.equal?(probe) }
          if struct
            struct.done = true
            @PairProbes.delete(struct)
          end
          struct
        end

        if struct
          peer = struct.second
          peer.__send__(:ns_pair_forget, struct) unless peer.equal?(self)
        end
      end

      def ns_pair_forget(struct)
        @Mutex.synchronize { @PairProbes.delete(struct) }
      end

      def ns_pop_op(matcher, probe, include_channel)
        message = ns_shift_message matcher

        # got message from buffer
        if message != NOTHING
          if probe.fulfill(include_channel ? [self, message] : message, false)
            new_message = ns_consume_pending_push ANY
            @Messages.push new_message unless new_message == NOTHING
          else
            @Messages.unshift message
          end
          return probe
        end

        # no message in buffer, try to pair with a pending push
        i = 0
        while true
          message, pushed = @PendingPush[i, 2]
          break if pushed.nil?

          if matcher === message
            value = include_channel ? [self, message] : message
            if Promises::Resolvable.atomic_resolution(probe  => [true, value, nil],
                                                      pushed => [true, self, nil])
              @PendingPush[i, 2] = []
              return probe
            end

            if probe.resolved?
              return probe
            end

            # so pushed.resolved? has to be true, remove the push
            @PendingPush[i, 2] = []
          end

          i += 2
        end

        # no push to pair with
        # TODO (pitr-ch 11-Jan-2019): clear up probes when timed out, use callback
        @Probes.push probe, include_channel, matcher if probe.pending?
        return probe
      end

      def ns_consume_pending_push(matcher, remove = true)
        i = 0
        while true
          message, pushed = @PendingPush[i, 2]
          return NOTHING unless pushed

          if matcher === message
            resolved           = pushed.resolved?
            @PendingPush[i, 2] = [] if remove || resolved
            # can fail if timed-out, so try without error
            if remove ? pushed.fulfill(self, false) : !resolved
              # pushed fulfilled so actually push the message
              return message
            end
          end

          i += 2
        end
      end

      def ns_try_push(message)
        i = 0
        while true
          probe, include_channel, matcher = @Probes[i, 3]
          break unless probe
          if matcher === message && probe.fulfill(include_channel ? [self, message] : message, false)
            @Probes[i, 3] = []
            return true
          end
          i += 3
        end

        if @Capacity > @Messages.size
          @Messages.push message
          true
        else
          false
        end
      end

      def ns_shift_message(matcher, remove = true)
        i = 0
        while true
          message = @Messages.fetch(i, NOTHING)
          return NOTHING if message == NOTHING

          if matcher === message
            @Messages.delete_at i if remove
            return message
          end

          i += 1
        end
      end
    end
  end
end
