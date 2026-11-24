require 'concurrent/configuration'
require 'concurrent/atomic/atomic_reference'
require 'concurrent/atomic/count_down_latch'
require 'concurrent/atomic/thread_local_var'
require 'concurrent/collection/copy_on_write_observer_set'
require 'concurrent/concern/observable'
require 'concurrent/synchronization/lockable_object'

module Concurrent

  # `Agent` is inspired by Clojure's [agent](http://clojure.org/agents)
  # function. An agent is a shared, mutable variable providing independent,
  # uncoordinated, *asynchronous* change of individual values. Best used when
  # the value will undergo frequent, complex updates. Suitable when the result
  # of an update does not need to be known immediately. `Agent` is (mostly)
  # functionally equivalent to Clojure's agent, except where the runtime
  # prevents parity.
  #
  # Agents are reactive, not autonomous - there is no imperative message loop
  # and no blocking receive. The state of an Agent should be itself immutable
  # and the `#value` of an Agent is always immediately available for reading by
  # any thread without any messages, i.e. observation does not require
  # cooperation or coordination.
  #
  # Agent action dispatches are made using the various `#send` methods. These
  # methods always return immediately. At some point later, in another thread,
  # the following will happen:
  #
  # 1. The given `action` will be applied to the state of the Agent and the
  #    `args`, if any were supplied.
  # 2. The return value of `action` will be passed to the validator lambda,
  #    if one has been set on the Agent.
  # 3. If the validator succeeds or if no validator was given, the return value
  #    of the given `action` will become the new `#value` of the Agent. See
  #    `#initialize` for details.
  # 4. If any observers were added to the Agent, they will be notified. See
  #    `#add_observer` for details.
  # 5. If during the `action` execution any other dispatches are made (directly
  #    or indirectly), they will be held until after the `#value` of the Agent
  #    has been changed.
  #
  # If any exceptions are thrown by an action function, no nested dispatches
  # will occur, and the exception will be cached in the Agent itself. When an
  # Agent has errors cached, any subsequent interactions will immediately throw
  # an exception, until the agent's errors are cleared. Agent errors can be
  # examined with `#error` and the agent restarted with `#restart`.
  #
  # The actions of all Agents get interleaved amongst threads in a thread pool.
  # At any point in time, at most one action for each Agent is being executed.
  # Actions dispatched to an agent from another single agent or thread will
  # occur in the order they were sent, potentially interleaved with actions
  # dispatched to the same agent from other sources. The `#send` method should
  # be used for actions that are CPU limited, while the `#send_off` method is
  # appropriate for actions that may block on IO.
  #
  # Unlike in Clojure, `Agent` cannot participate in `Concurrent::TVar` transactions.
  #
  # ## Tracked Dispatches and Receipts
  #
  # The tracked variants of the send methods (`#send_tracked`,
  # `#send_off_tracked`, `#send_via_tracked` and their bang counterparts)
  # return an {Agent::Receipt} instead of a bare boolean. A receipt is the
  # batch-level acknowledgement of one piece of material: it reports the
  # outcome of the action which was directly dispatched (the root of the
  # batch) together with every follow-on action that was actually accepted,
  # i.e. every action nested by an already accepted action, directly or
  # transitively, on any Agent.
  #
  # Follow-on work is always attributed to the receipt of the action which
  # actually caused it to be enqueued. A second dispatch made by the same
  # thread, and work that happens to be interleaved into the same Agents from
  # other threads, keep their own receipts and never appear on this one.
  #
  # A follow-on action is held back and cannot touch its Agent until the
  # enclosing action has actually run and passed its own Agent's validation.
  # If the enclosing action raises or fails validation, none of the work it
  # dispatched is processed: the enclosing action is reported as rejected and
  # the held work is reported as cancelled. Once the enclosing action is
  # accepted, the held work is released and reported according to its own
  # fate.
  #
  # Waiting on a receipt never blocks an Agent action and may time out: the
  # same receipt can be queried again later. When a failed Agent is restarted,
  # queued work that is retained is still awaited to completion; work discarded
  # with `:clear_actions` is reported, item by item, as cancelled.
  #
  # ## Example
  #
  # ```
  # def next_fibonacci(set = nil)
  #   return [0, 1] if set.nil?
  #   set + [set[-2..-1].reduce{|sum,x| sum + x }]
  # end
  #
  # # create an agent with an initial value
  # agent = Concurrent::Agent.new(next_fibonacci)
  #
  # # send a few update requests
  # 5.times do
  #   agent.send{|set| next_fibonacci(set) }
  # end
  #
  # # wait for them to complete
  # agent.await
  #
  # # get the current value
  # agent.value #=> [0, 1, 1, 2, 3, 5, 8]
  # ```
  #
  # ## Observation
  #
  # Agents support observers through the {Concurrent::Observable} mixin module.
  # Notification of observers occurs every time an action dispatch returns and
  # the new value is successfully validated. Observation will *not* occur if the
  # action raises an exception, if validation fails, or when a {#restart} occurs.
  #
  # When notified the observer will receive three arguments: `time`, `old_value`,
  # and `new_value`. The `time` argument is the time at which the value change
  # occurred. The `old_value` is the value of the Agent when the action began
  # processing. The `new_value` is the value to which the Agent was set when the
  # action completed. Note that `old_value` and `new_value` may be the same.
  # This is not an error. It simply means that the action returned the same
  # value.
  #
  # ## Nested Actions
  #
  # It is possible for an Agent action to post further actions back to itself.
  # The nested actions will be enqueued normally then processed *after* the
  # outer action completes, in the order they were sent, possibly interleaved
  # with action dispatches from other threads. Nested actions never deadlock
  # with one another and a failure in a nested action will never affect the
  # outer action.
  #
  # Nested actions can be called using the Agent reference from the enclosing
  # scope or by passing the reference in as a "send" argument. Nested actions
  # cannot be post using `self` from within the action block/proc/lambda; `self`
  # in this context will not reference the Agent. The preferred method for
  # dispatching nested actions is to pass the Agent as an argument. This allows
  # Ruby to more effectively manage the closing scope.
  #
  # Prefer this:
  #
  # ```
  # agent = Concurrent::Agent.new(0)
  # agent.send(agent) do |value, this|
  #   this.send {|v| v + 42 }
  #   3.14
  # end
  # agent.value #=> 45.14
  # ```
  #
  # Over this:
  #
  # ```
  # agent = Concurrent::Agent.new(0)
  # agent.send do |value|
  #   agent.send {|v| v + 42 }
  #   3.14
  # end
  # ```
  #
  # @!macro agent_await_warning
  #
  #   **NOTE** Never, *under any circumstances*, call any of the "await" methods
  #   ({#await}, {#await_for}, {#await_for!}, and {#wait}) from within an action
  #   block/proc/lambda. The call will block the Agent and will always fail.
  #   Calling either {#await} or {#wait} (with a timeout of `nil`) will
  #   hopelessly deadlock the Agent with no possibility of recovery.
  #
  # @!macro thread_safe_variable_comparison
  #
  # @see http://clojure.org/Agents Clojure Agents
  # @see http://clojure.org/state Values and Change - Clojure's approach to Identity and Change
  class Agent < Synchronization::LockableObject
    include Concern::Observable

    ERROR_MODES = [:continue, :fail].freeze
    private_constant :ERROR_MODES

    AWAIT_FLAG = ::Object.new
    private_constant :AWAIT_FLAG

    AWAIT_ACTION = ->(value, latch) { latch.count_down; AWAIT_FLAG }
    private_constant :AWAIT_ACTION

    DEFAULT_ERROR_HANDLER = ->(agent, error) { nil }
    private_constant :DEFAULT_ERROR_HANDLER

    DEFAULT_VALIDATOR = ->(value) { true }
    private_constant :DEFAULT_VALIDATOR

    # Status of an action whose body completed and whose new value passed the
    # validator of the Agent it ran on.
    COMPLETED = :completed
    private_constant :COMPLETED

    # Status of an action whose body raised, or whose returned value failed the
    # validator of the Agent it ran on.
    REJECTED = :rejected
    private_constant :REJECTED

    # Status of an enqueued, not yet started action that was discarded before it
    # could run, either because the action that dispatched it was rejected or
    # because the failed Agent holding it was restarted with `:clear_actions`.
    CANCELLED = :cancelled
    private_constant :CANCELLED

    # Status of an action that is still queued or running.
    PENDING = :pending
    private_constant :PENDING

    Job = Struct.new(:action, :args, :executor, :caller, :receipt, :outcome,
                     :agent, :gate, :posted)
    private_constant :Job

    # Receipt and gate of the tracked action currently running on this thread.
    # These are shared across every Agent because an action running on one
    # Agent may dispatch follow-on work to a different Agent from the same
    # thread.
    CURRENT_RECEIPT = Concurrent::ThreadLocalVar.new(nil)
    private_constant :CURRENT_RECEIPT

    CURRENT_GATE = Concurrent::ThreadLocalVar.new(nil)
    private_constant :CURRENT_GATE

    # Holds the follow-on jobs dispatched during one tracked action until the
    # action's validation result is known. A closed gate blocks every job
    # carrying it from being posted to an executor.
    #
    # @!visibility private
    class Gate
      def initialize
        @open = Concurrent::AtomicReference.new(false)
        @jobs = []
      end

      def open?
        @open.value
      end

      def open!
        @open.value = true
      end

      def add(job)
        @jobs << job
      end

      def jobs
        @jobs.dup
      end
    end
    private_constant :Gate

    # One reported line of an {Agent::Receipt}: the fate of a single action
    # within the batch identified by the receipt.
    #
    # The root action is reported first. Every further entry is a follow-on
    # action that was actually accepted, attributed to the Agent it ran on.
    class ReceiptItem
      # @return [Agent] the Agent on which the action was dispatched
      attr_reader :agent
      # @return [Symbol] one of `:completed`, `:rejected`, `:cancelled`, `:pending`
      attr_reader :status
      # @return [Object] the validated new value when `:completed`
      attr_reader :value
      # @return [Exception, nil] the error raised by the action or validation
      attr_reader :error

      # @!visibility private
      def initialize(agent, status, value = nil, error = nil)
        @agent  = agent
        @status = status
        @value  = value
        @error  = error
      end

      # @return [Boolean] true when the action ran and passed validation
      def completed?
        status == COMPLETED
      end

      # @return [Boolean] true when the action raised or failed validation
      def rejected?
        status == REJECTED
      end

      # @return [Boolean] true when the action was discarded before running
      def cancelled?
        status == CANCELLED
      end

      # @return [Boolean] true when the action has not reached a final state yet
      def pending?
        status == PENDING
      end

      # @!visibility private
      def settle(status, value = nil, error = nil)
        @status = status
        @value  = value
        @error  = error
      end
    end

    # The batch acknowledgement returned by the tracked send methods. A receipt
    # covers one directly dispatched root action and every follow-on action it
    # actually caused, across every Agent involved.
    #
    # A receipt is queryable at any time. Until every attributed action has a
    # final state, waits may time out and the same receipt can be waited on
    # again later.
    class Receipt < Synchronization::LockableObject
      # @!visibility private
      def initialize
        super()
        @items      = []
        @root       = nil
        @finalized = false
      end

      # The outcome of the root action, once it has been processed.
      #
      # @return [Agent::ReceiptItem, nil] nil while the root action has not run
      def root
        synchronize { @root }
      end

      # A snapshot of every action attributed to this receipt at the time of the
      # call. The root action, when processed, is always first. Follow-on
      # actions appear in the order they were accepted.
      #
      # @return [Array<Agent::ReceiptItem>]
      def items
        synchronize { @items.dup }
      end

      # Blocks until every attributed action has reached a final state, or the
      # timeout elapses. Never raises on timeout; the same receipt can be waited
      # on again.
      #
      # @param [Float, nil] timeout the maximum number of seconds to wait,
      #   nil waits indefinitely
      # @return [Boolean] true when the receipt is complete, false on timeout
      def wait(timeout = nil)
        deadline = timeout ? Concurrent.monotonic_time + timeout.to_f : nil
        synchronize do
          loop do
            return true if @finalized
            remaining = deadline ? deadline - Concurrent.monotonic_time : nil
            return false if remaining && remaining <= 0
            ns_wait(remaining)
          end
        end
      end

      # @return [Boolean] true when every attributed action has a final state
      def complete?
        synchronize { @finalized }
      end

      # True only when the root action is settled and accepted, and every
      # accepted follow-on action is also completed. This is the acceptance
      # condition: the root is decided and every accepted follow-on has a
      # concrete, successful destination.
      #
      # @return [Boolean]
      def accepted?
        synchronize do
          @finalized && @root && @root.completed? && @items.all?(&:completed?)
        end
      end

      # @return [Array<Agent::ReceiptItem>] items that never reached completion
      def outstanding
        synchronize { @items.reject(&:completed?).dup }
      end

      # @!visibility private
      def add_pending(agent)
        synchronize do
          item = ReceiptItem.new(agent, PENDING)
          @items << item
          item
        end
      end

      # @!visibility private
      def mark_root(item)
        synchronize do
          @root = item
          @items.delete(item)
          @items.unshift(item)
        end
      end

      # @!visibility private
      def settle(item, status, value = nil, error = nil)
        synchronize { item.settle(status, value, error) }
      end

      # @!visibility private
      def finalize
        synchronize do
          @finalized = @root && !@root.pending? && @items.none?(&:pending?)
          ns_broadcast if @finalized
          @finalized
        end
      end
    end

    # Raised during action processing or any other time in an Agent's lifecycle.
    class Error < StandardError
      def initialize(message = nil)
        message ||= 'agent must be restarted before jobs can post'
        super(message)
      end
    end

    # Raised when a new value obtained during action processing or at `#restart`
    # fails validation.
    class ValidationError < Error
      def initialize(message = nil)
        message ||= 'invalid value'
        super(message)
      end
    end

    # The error mode this Agent is operating in. See {#initialize} for details.
    attr_reader :error_mode

    # Create a new `Agent` with the given initial value and options.
    #
    # The `:validator` option must be `nil` or a side-effect free proc/lambda
    # which takes one argument. On any intended value change the validator, if
    # provided, will be called. If the new value is invalid the validator should
    # return `false` or raise an error.
    #
    # The `:error_handler` option must be `nil` or a proc/lambda which takes two
    # arguments. When an action raises an error or validation fails, either by
    # returning false or raising an error, the error handler will be called. The
    # arguments to the error handler will be a reference to the agent itself and
    # the error object which was raised.
    #
    # The `:error_mode` may be either `:continue` (the default if an error
    # handler is given) or `:fail` (the default if error handler nil or not
    # given).
    #
    # If an action being run by the agent throws an error or doesn't pass
    # validation the error handler, if present, will be called. After the
    # handler executes if the error mode is `:continue` the Agent will continue
    # as if neither the action that caused the error nor the error itself ever
    # happened.
    #
    # If the mode is `:fail` the Agent will become {#failed?} and will stop
    # accepting new action dispatches. Any previously queued actions will be
    # held until {#restart} is called. The {#value} method will still work,
    # returning the value of the Agent before the error.
    #
    # @param [Object] initial the initial value
    # @param [Hash] opts the configuration options
    #
    # @option opts [Symbol] :error_mode either `:continue` or `:fail`
    # @option opts [nil, Proc] :error_handler the (optional) error handler
    # @option opts [nil, Proc] :validator the (optional) validation procedure
    def initialize(initial, opts = {})
      super()
      synchronize { ns_initialize(initial, opts) }
    end

    # The current value (state) of the Agent, irrespective of any pending or
    # in-progress actions. The value is always available and is non-blocking.
    #
    # @return [Object] the current value
    def value
      @current.value # TODO (pitr 12-Sep-2015): broken unsafe read?
    end

    alias_method :deref, :value

    # When {#failed?} and {#error_mode} is `:fail`, returns the error object
    # which caused the failure, else `nil`. When {#error_mode} is `:continue`
    # will *always* return `nil`.
    #
    # @return [nil, Error] the error which caused the failure when {#failed?}
    def error
      @error.value
    end

    alias_method :reason, :error

    # @!macro agent_send
    #
    #   Dispatches an action to the Agent and returns immediately. Subsequently,
    #   in a thread from a thread pool, the {#value} will be set to the return
    #   value of the action. Action dispatches are only allowed when the Agent
    #   is not {#failed?}.
    #
    #   The action must be a block/proc/lambda which takes 1 or more arguments.
    #   The first argument is the current {#value} of the Agent. Any arguments
    #   passed to the send method via the `args` parameter will be passed to the
    #   action as the remaining arguments. The action must return the new value
    #   of the Agent.
    #
    #   * {#send} and {#send!} should be used for actions that are CPU limited
    #   * {#send_off}, {#send_off!}, and {#<<} are appropriate for actions that
    #     may block on IO
    #   * {#send_via} and {#send_via!} are used when a specific executor is to
    #     be used for the action
    #
    #   @param [Array<Object>] args zero or more arguments to be passed to
    #     the action
    #   @param [Proc] action the action dispatch to be enqueued
    #
    #   @yield [agent, value, *args] process the old value and return the new
    #   @yieldparam [Object] value the current {#value} of the Agent
    #   @yieldparam [Array<Object>] args zero or more arguments to pass to the
    #     action
    #   @yieldreturn [Object] the new value of the Agent
    #
    # @!macro send_return
    #   @return [Boolean] true if the action is successfully enqueued, false if
    #     the Agent is {#failed?}
    def send(*args, &action)
      enqueue_action_job(action, args, Concurrent.global_fast_executor)
    end

    # @!macro agent_send
    #
    # @!macro send_bang_return_and_raise
    #   @return [Boolean] true if the action is successfully enqueued
    #   @raise [Concurrent::Agent::Error] if the Agent is {#failed?}
    def send!(*args, &action)
      raise Error.new unless send(*args, &action)
      true
    end

    # @!macro agent_send
    # @!macro send_return
    def send_off(*args, &action)
      enqueue_action_job(action, args, Concurrent.global_io_executor)
    end

    alias_method :post, :send_off

    # @!macro agent_send
    # @!macro send_bang_return_and_raise
    def send_off!(*args, &action)
      raise Error.new unless send_off(*args, &action)
      true
    end

    # @!macro agent_send
    # @!macro send_return
    # @param [Concurrent::ExecutorService] executor the executor on which the
    #   action is to be dispatched
    def send_via(executor, *args, &action)
      enqueue_action_job(action, args, executor)
    end

    # @!macro agent_send
    # @!macro send_bang_return_and_raise
    # @param [Concurrent::ExecutorService] executor the executor on which the
    #   action is to be dispatched
    def send_via!(executor, *args, &action)
      raise Error.new unless send_via(executor, *args, &action)
      true
    end

    # Dispatches an action to the Agent and returns immediately. Subsequently,
    # in a thread from a thread pool, the {#value} will be set to the return
    # value of the action. Appropriate for actions that may block on IO.
    #
    # @param [Proc] action the action dispatch to be enqueued
    # @return [Concurrent::Agent] self
    # @see #send_off
    def <<(action)
      send_off(&action)
      self
    end

    # Like {#send} but the returned {Agent::Receipt} accounts for this action
    # and every follow-on action it actually causes.
    #
    # @return [Agent::Receipt, nil] the receipt, or nil if the Agent is
    #   {#failed?} and the action could not be enqueued
    def send_tracked(*args, &action)
      enqueue_tracked_action_job(action, args, Concurrent.global_fast_executor)
    end

    # @see #send_tracked
    # @raise [Concurrent::Agent::Error] if the Agent is {#failed?}
    # @return [Agent::Receipt]
    def send_tracked!(*args, &action)
      receipt = send_tracked(*args, &action)
      raise Error.new unless receipt
      receipt
    end

    # Like {#send_off} but returns an {Agent::Receipt}, see {#send_tracked}.
    #
    # @return [Agent::Receipt, nil]
    def send_off_tracked(*args, &action)
      enqueue_tracked_action_job(action, args, Concurrent.global_io_executor)
    end

    # @see #send_off_tracked
    # @raise [Concurrent::Agent::Error] if the Agent is {#failed?}
    # @return [Agent::Receipt]
    def send_off_tracked!(*args, &action)
      receipt = send_off_tracked(*args, &action)
      raise Error.new unless receipt
      receipt
    end

    # Like {#send_via} but returns an {Agent::Receipt}, see {#send_tracked}.
    #
    # @param [Concurrent::ExecutorService] executor the executor on which the
    #   action is to be dispatched
    # @return [Agent::Receipt, nil]
    def send_via_tracked(executor, *args, &action)
      enqueue_tracked_action_job(action, args, executor)
    end

    # @see #send_via_tracked
    # @raise [Concurrent::Agent::Error] if the Agent is {#failed?}
    # @param [Concurrent::ExecutorService] executor the executor on which the
    #   action is to be dispatched
    # @return [Agent::Receipt]
    def send_via_tracked!(executor, *args, &action)
      receipt = send_via_tracked(executor, *args, &action)
      raise Error.new unless receipt
      receipt
    end

    # Blocks the current thread (indefinitely!) until all actions dispatched
    # thus far, from this thread or nested by the Agent, have occurred. Will
    # block when {#failed?}. Will never return if a failed Agent is {#restart}
    # with `:clear_actions` true.
    #
    # Returns a reference to `self` to support method chaining:
    #
    # ```
    # current_value = agent.await.value
    # ```
    #
    # @return [Boolean] self
    #
    # @!macro agent_await_warning
    def await
      wait(nil)
      self
    end

    # Blocks the current thread until all actions dispatched thus far, from this
    # thread or nested by the Agent, have occurred, or the timeout (in seconds)
    # has elapsed.
    #
    # @param [Float] timeout the maximum number of seconds to wait
    # @return [Boolean] true if all actions complete before timeout else false
    #
    # @!macro agent_await_warning
    def await_for(timeout)
      wait(timeout.to_f)
    end

    # Blocks the current thread until all actions dispatched thus far, from this
    # thread or nested by the Agent, have occurred, or the timeout (in seconds)
    # has elapsed.
    #
    # @param [Float] timeout the maximum number of seconds to wait
    # @return [Boolean] true if all actions complete before timeout
    #
    # @raise [Concurrent::TimeoutError] when timeout is reached
    #
    # @!macro agent_await_warning
    def await_for!(timeout)
      raise Concurrent::TimeoutError unless wait(timeout.to_f)
      true
    end

    # Blocks the current thread until all actions dispatched thus far, from this
    # thread or nested by the Agent, have occurred, or the timeout (in seconds)
    # has elapsed. Will block indefinitely when timeout is nil or not given.
    #
    # Provided mainly for consistency with other classes in this library. Prefer
    # the various `await` methods instead.
    #
    # @param [Float] timeout the maximum number of seconds to wait
    # @return [Boolean] true if all actions complete before timeout else false
    #
    # @!macro agent_await_warning
    def wait(timeout = nil)
      latch = Concurrent::CountDownLatch.new(1)
      enqueue_await_job(latch)
      latch.wait(timeout)
    end

    # Is the Agent in a failed state?
    #
    # @see #restart
    def failed?
      !@error.value.nil?
    end

    alias_method :stopped?, :failed?

    # When an Agent is {#failed?}, changes the Agent {#value} to `new_value`
    # then un-fails the Agent so that action dispatches are allowed again. If
    # the `:clear_actions` option is give and true, any actions queued on the
    # Agent that were being held while it was failed will be discarded,
    # otherwise those held actions will proceed. The `new_value` must pass the
    # validator if any, or `restart` will raise an exception and the Agent will
    # remain failed with its old {#value} and {#error}. Observers, if any, will
    # not be notified of the new state.
    #
    # Tracked jobs discarded by a clearing restart are reported on their
    # receipts as `:cancelled`, identifying the Agent (this one) whose queue
    # dropped them.
    #
    # @param [Object] new_value the new value for the Agent once restarted
    # @param [Hash] opts the configuration options
    # @option opts [Symbol] :clear_actions true if all enqueued but unprocessed
    #   actions should be discarded on restart, else false (default: false)
    # @return [Boolean] true
    #
    # @raise [Concurrent:AgentError] when not failed
    def restart(new_value, opts = {})
      clear_actions = opts.fetch(:clear_actions, false)
      synchronize do
        raise Error.new('agent is not failed') unless failed?
        raise ValidationError unless ns_validate(new_value)
        @current.value = new_value
        @error.value   = nil
        if clear_actions
          ns_cancel_queued_tracked_jobs
          @queue.clear
        end
        ns_post_next_job unless @queue.empty?
      end
      true
    end

    class << self

      # Blocks the current thread (indefinitely!) until all actions dispatched
      # thus far to all the given Agents, from this thread or nested by the
      # given Agents, have occurred. Will block when any of the agents are
      # failed. Will never return if a failed Agent is restart with
      # `:clear_actions` true.
      #
      # @param [Array<Concurrent::Agent>] agents the Agents on which to wait
      # @return [Boolean] true
      #
      # @!macro agent_await_warning
      def await(*agents)
        agents.each { |agent| agent.await }
        true
      end

      # Blocks the current thread until all actions dispatched thus far to all
      # the given Agents, from this thread or nested by the given Agents, have
      # occurred, or the timeout (in seconds) has elapsed.
      #
      # @param [Float] timeout the maximum number of seconds to wait
      # @param [Array<Concurrent::Agent>] agents the Agents on which to wait
      # @return [Boolean] true if all actions complete before timeout else false
      #
      # @!macro agent_await_warning
      def await_for(timeout, *agents)
        end_at = Concurrent.monotonic_time + timeout.to_f
        ok     = agents.length.times do |i|
          break false if (delay = end_at - Concurrent.monotonic_time) < 0
          break false unless agents[i].await_for(delay)
        end
        !!ok
      end

      # Blocks the current thread until all actions dispatched thus far to all
      # the given Agents, from this thread or nested by the given Agents, have
      # occurred, or the timeout (in seconds) has elapsed.
      #
      # @param [Float] timeout the maximum number of seconds to wait
      # @param [Array<Concurrent::Agent>] agents the Agents on which to wait
      # @return [Boolean] true if all actions complete before timeout
      #
      # @raise [Concurrent::TimeoutError] when timeout is reached
      # @!macro agent_await_warning
      def await_for!(timeout, *agents)
        raise Concurrent::TimeoutError unless await_for(timeout, *agents)
        true
      end
    end

    private

    def ns_initialize(initial, opts)
      @error_mode    = opts[:error_mode]
      @error_handler = opts[:error_handler]

      if @error_mode && !ERROR_MODES.include?(@error_mode)
        raise ArgumentError.new('unrecognized error mode')
      elsif @error_mode.nil?
        @error_mode = @error_handler ? :continue : :fail
      end

      @error_handler ||= DEFAULT_ERROR_HANDLER
      @validator     = opts.fetch(:validator, DEFAULT_VALIDATOR)
      @current       = Concurrent::AtomicReference.new(initial)
      @error         = Concurrent::AtomicReference.new(nil)
      @caller        = Concurrent::ThreadLocalVar.new(nil)
      @queue         = []

      self.observers = Collection::CopyOnNotifyObserverSet.new
    end

    def enqueue_action_job(action, args, executor)
      raise ArgumentError.new('no action given') unless action
      job = Job.new(action, args, executor, @caller.value || Thread.current.object_id,
                    nil, nil, self, nil, false)
      synchronize { ns_enqueue_job(job) }
    end

    def enqueue_tracked_action_job(action, args, executor)
      raise ArgumentError.new('no action given') unless action
      receipt = CURRENT_RECEIPT.value || Receipt.new
      gate    = CURRENT_GATE.value
      job     = Job.new(action, args, executor,
                        @caller.value || Thread.current.object_id,
                        receipt, nil, self, gate, false)
      synchronize do
        return nil if failed?
        ns_enqueue_job(job)
      end
      receipt
    end

    def enqueue_await_job(latch)
      synchronize do
        if (index = ns_find_last_job_for_thread)
          job = Job.new(AWAIT_ACTION, [latch], Concurrent.global_immediate_executor,
                        Thread.current.object_id, nil, nil, self, nil, false)
          ns_enqueue_job(job, index+1)
        else
          latch.count_down
          true
        end
      end
    end

    def ns_enqueue_job(job, index = nil)
      # a non-nil index means this is an await job
      return false if index.nil? && failed?
      index ||= @queue.length
      if job.receipt
        job.outcome = job.receipt.add_pending(self)
        job.gate.add(job) if job.gate
      end
      @queue.insert(index, job)
      # if this is the only job, post to executor
      ns_post_next_job if @queue.length == 1
      true
    end

    def ns_post_next_job
      return if failed?
      job = @queue.first
      return unless job
      return if job.gate && !job.gate.open?
      return if job.posted
      job.posted = true
      job.executor.post { execute_next_job }
    end

    def execute_next_job
      job       = synchronize { @queue.first }
      return unless job
      old_value = @current.value
      gate      = nil

      previous_caller  = @caller.value
      previous_receipt = CURRENT_RECEIPT.value
      previous_gate    = CURRENT_GATE.value
      @caller.value      = job.caller # for nested actions
      CURRENT_RECEIPT.value = job.receipt
      gate               = job.receipt ? Gate.new : nil
      CURRENT_GATE.value  = gate
      begin
        new_value = job.action.call(old_value, *job.args)
      ensure
        @caller.value        = previous_caller
        CURRENT_RECEIPT.value = previous_receipt
        CURRENT_GATE.value    = previous_gate
      end

      return if new_value == AWAIT_FLAG

      if ns_validate(new_value)
        @current.value = new_value
        observers.notify_observers(Time.now, old_value, new_value)
        ns_accept_tracked_job(job, new_value, gate) if job.receipt
      else
        error = ValidationError.new
        handle_error(error)
        ns_reject_tracked_job(job, error, gate) if job.receipt
      end
    rescue => error
      handle_error(error)
      ns_reject_tracked_job(job, error, gate) if job&.receipt
    ensure
      synchronize do
        @queue.shift
        unless failed? || @queue.empty?
          ns_post_next_job
        end
      end
    end

    # Record the accepted outcome of a tracked job and release every follow-on
    # job it dispatched; they were held until this action passed validation.
    def ns_accept_tracked_job(job, new_value, gate)
      receipt = job.receipt
      receipt.mark_root(job.outcome) if receipt.root.nil?
      receipt.settle(job.outcome, COMPLETED, new_value, nil)
      if gate
        gate.open!
        gate.jobs.each { |gated| gated.agent.release_gated_job(gated) }
      end
      receipt.finalize
    end

    # Record the rejected outcome of a tracked job. None of the follow-on jobs
    # it dispatched may run, so remove them from their Agents' queues and report
    # them as cancelled.
    def ns_reject_tracked_job(job, error, gate)
      receipt = job.receipt
      receipt.mark_root(job.outcome) if receipt.root.nil?
      if gate
        gate.jobs.each do |gated|
          gated.agent.cancel_gated_job(gated)
          receipt.settle(gated.outcome, CANCELLED) if gated.outcome
        end
      end
      receipt.settle(job.outcome, REJECTED, nil, error)
      receipt.finalize
    end

    # Restart with `:clear_actions`: every still-queued tracked job is dropped.
    # Settle its receipt item as cancelled so waiting receipts can finish.
    def ns_cancel_queued_tracked_jobs
      touched = {}
      @queue.each do |queued|
        receipt = queued.receipt
        next unless receipt
        receipt.settle(queued.outcome, CANCELLED) if queued.outcome
        touched[receipt] = true
      end
      touched.each_key(&:finalize)
    end

    def ns_validate(value)
      @validator.call(value)
    rescue
      false
    end

    def handle_error(error)
      # stop new jobs from posting
      @error.value = error if @error_mode == :fail
      @error_handler.call(self, error)
    rescue
      # do nothing
    end

    def ns_find_last_job_for_thread
      @queue.rindex { |job| job.caller == Thread.current.object_id }
    end

    public

    # @!visibility private
    #
    # Called by the Agent running the action that dispatched `job` once that
    # action has passed validation. Posts `job` if it is still the head of this
    # Agent's queue; otherwise the normal queue chain will pick it up.
    def release_gated_job(job)
      synchronize { ns_post_next_job if @queue.first.equal?(job) }
    end

    # @!visibility private
    #
    # Called by the Agent running the action that dispatched `job` when that
    # action was rejected. Removes the never-started job from this queue and
    # keeps the remaining queue moving.
    def cancel_gated_job(job)
      synchronize do
        removed = @queue.delete(job)
        ns_post_next_job if removed && !@queue.empty? && !failed?
      end
    end
  end
end
