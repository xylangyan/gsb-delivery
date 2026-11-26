module AASM
  class InstanceBase
    EXPLICIT_TARGET_KEYS = [:to, :to_state, :target, :target_state].freeze
    EXPLICIT_ARGS_KEYS = [
      :args, :params, :business_params, :arguments, :event_args,
      :event_params, :business_args, :with_args, :with, :payload
    ].freeze

    attr_accessor :from_state, :to_state, :current_event

    def initialize(instance, name=:default) # instance of the class including AASM, name of the state machine
      @instance = instance
      @name = name
      @event_contexts = []
    end

    def current_state
      @instance.aasm_read_state(@name)
    end

    def current_state=(state)
      @instance.aasm_write_state_without_persistence(state, @name)
    end

    def enter_initial_state
      state_name = determine_state_name(@instance.class.aasm(@name).initial_state)
      state_object = state_object_for_name(state_name)

      state_object.fire_callbacks(:before_enter, @instance)
      self.current_state = state_name
      state_object.fire_callbacks(:after_enter, @instance)

      state_name
    end

    def human_state
      state_object_for_name(current_state).display_name
    end

    def states(options={}, *args)
      if options.has_key?(:permitted)
        selected_events = events({:permitted => options[:permitted]}, *args)
        # An array of arrays. Each inner array represents the transitions that
        # transition from the current state for an event
        event_transitions = selected_events.map {|e| e.transitions_from_state(current_state) }

        # An array of :to transition states
        to_state_names = event_transitions.map do |transitions|
          return nil if transitions.empty?

          # Return the :to state of the first transition that is allowed (or not) or nil
          if options[:permitted]
            transition = transitions.find { |t| t.allowed?(@instance, *args) }
          else
            transition = transitions.find { |t| !t.allowed?(@instance, *args) }
          end
          transition ? transition.to : nil
        end.flatten.compact.uniq

        # Select states that are in to_state_names
        @instance.class.aasm(@name).states.select {|s| to_state_names.include?(s.name)}
      else
        @instance.class.aasm(@name).states
      end
    end

    def events(options={}, *args)
      state = options[:state] || current_state
      events = @instance.class.aasm(@name).events.select {|e| e.transitions_from_state?(state) }

      options[:reject] = Array(options[:reject])
      events.reject! { |e| options[:reject].include?(e.name) }

      if options.has_key?(:permitted)
        # filters the results of events_for_current_state so that only those that
        # are really currently possible (given transition guards) are shown.
        if options[:permitted]
          events.select! { |e| @instance.send("may_#{event_method_name(e.name)}?", *args) }
        else
          events.select! { |e| !@instance.send("may_#{event_method_name(e.name)}?", *args) }
        end
      end

      events
    end

    def permitted_transitions
      events(permitted: true).flat_map do |event|
        available_transitions = event.transitions_from_state(current_state)
        allowed_transitions = available_transitions.select { |t| t.allowed?(@instance) }

        allowed_transitions.map do |transition|
          { event: event.name, state: transition.to }
        end
      end
    end

    def state_object_for_name(name)
      obj = @instance.class.aasm(@name).states.find {|s| s.name == name}
      raise AASM::UndefinedState, "State :#{name} doesn't exist" if obj.nil?
      obj
    end

    def determine_state_name(state)
      case state
        when Symbol, String
          state
        when Proc
          state.call(@instance)
        else
          raise NotImplementedError, "Unrecognized state-type given. Expected Symbol, String, or Proc."
      end
    end

    def may_fire_event?(name, *args)
      if event = @instance.class.aasm(@name).state_machine.events[name.to_sym]
        explicit = explicit_event_arguments(args)
        if explicit
          to_state, business_args = resolve_explicit_arguments(name, explicit)
          !!event.may_fire?(@instance, explicit_target_value(to_state), *business_args)
        else
          !!event.may_fire?(@instance, *args)
        end
      else
        false # unknown event
      end
    end

    def fire(event_name, *args, &block)
      event_exists?(event_name)
      explicit = explicit_event_arguments(args)
      if explicit
        invoke_explicit_event(event_name, false, explicit, &block)
      else
        @instance.send(event_method_name(event_name), *args, &block)
      end
    end

    def fire!(event_name, *args, &block)
      event_exists?(event_name, true)
      explicit = explicit_event_arguments(args)
      if explicit
        invoke_explicit_event(event_name, true, explicit, &block)
      else
        @instance.send("#{event_method_name(event_name)}!", *args, &block)
      end
    end

    def with_event_context(event, from_state = nil, to_state = nil)
      context = event.is_a?(EventContext) ? event : EventContext.new(event, from_state, to_state)
      push_event_context(context)
      result = yield
      result
    ensure
      context = pop_event_context
      if @event_contexts.empty? && context
        @current_event = context.current_event
        @from_state = context.from_state
        @to_state = context.to_state
      end
    end

    def current_event
      context = current_event_context
      context ? context.current_event : @current_event
    end

    def current_event=(event)
      @current_event = event
    end

    def event_to_fire
      @current_event
    end

    def from_state
      context = current_event_context
      context ? context.from_state : @from_state
    end

    def from_state=(state)
      context = current_event_context
      if context
        context.from_state = state
      else
        @from_state = state
      end
    end

    def to_state
      context = current_event_context
      context ? context.to_state : @to_state
    end

    def to_state=(state)
      context = current_event_context
      if context
        context.to_state = state
      else
        @to_state = state
      end
    end

    def set_current_state_with_persistence(state)
      save_success = @instance.aasm_write_state(state, @name)
      self.current_state = state if save_success
      save_success
    end

    def last_event_context
      context = current_event_context
      context || @last_event_context
    end

    def build_event_context(event, to_state = nil)
      to_state = to_state.to_sym if to_state.respond_to?(:to_sym)
      EventContext.new(event, current_state, to_state)
    end

    def current_event_context
      current_context
    end

    private

    EventContext = Struct.new(:current_event, :from_state, :to_state)

    def explicit_event_arguments(args)
      return nil unless args.last.is_a?(Hash)

      options = args.last
      target_key = EXPLICIT_TARGET_KEYS.find { |key| options.key?(key) }
      args_key = EXPLICIT_ARGS_KEYS.find { |key| options.key?(key) }
      return nil unless target_key || args_key

      explicit_args = args_key ? explicit_arguments_for(options[args_key]) : []
      leading_args = args[0...-1]
      remaining_options = options.reject do |key, _value|
        target_key == key || args_key == key
      end
      explicit_args << remaining_options unless remaining_options.empty?

      {
        :to_state => target_key ? options[target_key] : nil,
        :args_key => args_key,
        :leading_args => leading_args,
        :args => explicit_args
      }
    end

    def explicit_arguments_for(value)
      return [] if value.nil?
      value.is_a?(Array) ? value : [value]
    end

    def explicit_target_value(state)
      return ::AASM::NO_VALUE if state.nil?
      state.respond_to?(:to_sym) ? state.to_sym : state
    end

    def invoke_explicit_event(event_name, bang, explicit, &block)
      to_state, business_args = resolve_explicit_arguments(event_name, explicit)
      options = {
        :persist => bang,
        :explicit => true,
        :to_state => to_state,
        :current_event => bang ? :"#{event_name}!" : event_name.to_sym
      }
      @instance.send(:aasm_fire_event, @name, event_name.to_sym, options, *business_args, &block)
    end

    def resolve_explicit_arguments(event_name, explicit)
      leading_args = explicit[:leading_args]
      to_state = explicit[:to_state]

      if to_state.nil? && explicit[:args_key] && leading_args.any? &&
         explicit_target_candidate?(event_name, leading_args.first)
        to_state = leading_args.first
        leading_args = leading_args[1..-1]
      end

      [to_state, leading_args + explicit[:args]]
    end

    def explicit_target_candidate?(event_name, candidate)
      return false unless candidate.respond_to?(:to_sym)
      event = @instance.class.aasm(@name).state_machine.events[event_name.to_sym]
      return false unless event
      event.transitions_from_state(current_state).any? do |transition|
        Array(transition.to).map(&:to_sym).include?(candidate.to_sym)
      end
    end

    def push_event_context(context)
      @event_contexts << context
      context
    end

    def pop_event_context
      context = @event_contexts.pop
      @last_event_context = context if context
      context
    end

    def current_context
      @event_contexts.last
    end

    def event_method_name(event_name)
      config = @instance.class.aasm(@name).state_machine.config
      if config.namespace
        ns = (config.namespace == true) ? @name : config.namespace
        "#{event_name}_#{ns}"
      else
        event_name.to_s
      end
    end

    def event_exists?(event_name, bang = false)
      event = @instance.class.aasm(@name).state_machine.events[event_name.to_sym]
      return true if event

      event_error = bang ? "#{event_name}!" : event_name
      raise AASM::UndefinedEvent, "Event :#{event_error} doesn't exist" if event.nil?
    end
  end
end
