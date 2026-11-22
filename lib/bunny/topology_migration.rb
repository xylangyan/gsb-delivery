# frozen_string_literal: true

require "set"
require "bunny/exceptions"

module Bunny
  # Plans and applies routing changes to the topology tracked by
  # {Bunny::TopologyRegistry}.
  #
  # A migration is previewed before anything is changed. The preview
  # reports the operations in the order they must happen, the business
  # routes they affect and the safety conditions under which the change
  # is allowed. {#preview} never talks to the broker and never mutates the
  # registry.
  #
  # Two properties are modelled explicitly because they are easy to get
  # wrong with auto-delete exchanges:
  #
  #  * removing the last outgoing binding of an auto-delete exchange makes
  #    the broker delete the exchange, which also removes all of its
  #    incoming bindings and can cascade further
  #  * adding a new destination before the old one is removed can deliver
  #    the same message to both destinations unless the bindings are
  #    disjoint or publishing is paused across the swap
  #
  # Three cutover modes are available:
  #
  #  * `:live` adds the new binding first and removes the old one right
  #    away. It is only feasible when no message can match both bindings.
  #  * `:quiesced` pauses publishers, swaps the bindings and resumes.
  #    This is the explicit cutover for overlapping bindings.
  #  * `:generation` uses a generation-specific routing key or headers so
  #    a message can only match the binding for its own generation. Older
  #    generations stay on the old queue until they are drained.
  #
  # @example Preview and apply an explicit cutover
  #   migration = Bunny::RoutingMigration.new(connection.topology_registry,
  #                                            entry_exchanges: ["events"])
  #   migration.move_queue_binding(source: "router", from: "orders", to: "orders.v2")
  #   plan = migration.preview(cutover: :auto)
  #   puts plan
  #   plan.feasible? # => true
  #   migration.apply(plan, adapter, journal: "orders-repoint")
  #
  # @see Bunny::TopologyRegistry
  class TopologyMigration
    # Base class for migration errors.
    Error = TopologyMigrationError

    # Raised when a migration is resumed with a different plan.
    JournalConflict = TopologyMigrationConflict

    # Raised when a route cannot be changed without violating one of the
    # safety requirements.
    IncompatibleTopology = Bunny::IncompatibleTopology

    # A declaration Bunny knows about.
    Exchange = Struct.new(:name, :type, :durable, :auto_delete, :arguments, keyword_init: true) do
      def auto_delete?
        !!auto_delete
      end

      def type_name
        (type || :direct).to_s
      end
    end

    # A queue declaration Bunny knows about.
    Queue = Struct.new(:name, :durable, :auto_delete, :exclusive, :arguments, keyword_init: true) do
      def auto_delete?
        !!auto_delete
      end
    end

    # A queue or exchange binding.
    Binding = Struct.new(:source, :destination, :routing_key, :arguments, :kind, keyword_init: true) do
      def queue_binding?
        kind == :queue
      end

      def exchange_binding?
        kind == :exchange
      end

      def to_s
        "#{source} -> #{destination}#{routing_key.nil? || routing_key.empty? ? "" : " (rk=#{routing_key})"}"
      end
    end

    # A single migration operation.
    Step = Struct.new(:id, :action, :source, :destination, :routing_key, :arguments,
                      :phase, :description, :options, keyword_init: true) do
      def barrier?
        action == :barrier
      end

      def to_s
        description || "#{action} #{source} -> #{destination}"
      end
    end

    # A route from an entry exchange to a queue. Exchanges are listed in
    # the order they are traversed.
    Route = Struct.new(:entry, :exchanges, :queue, :bindings, keyword_init: true) do
      def to_s
        ([self.entry] + Array(exchanges) + [queue]).join(" -> ")
      end

      def uses?(binding)
        bindings.include?(binding)
      end
    end

    # A before/after comparison for one route.
    RouteImpact = Struct.new(:before, :after, :status, keyword_init: true) do
      def to_s
        case status
        when :rewired then "#{before} => #{after}"
        when :lost then "#{before} (lost)"
        when :added then "#{after} (added)"
        when :preserved then before.to_s
        else "#{before} => #{after}"
        end
      end
    end

    # A topology snapshot used for previews.
    #
    # It can be built from a {Bunny::TopologyRegistry} or from plain
    # declarations, which makes it usable without a broker connection.
    class Snapshot
      attr_reader :exchanges, :queues, :queue_bindings, :exchange_bindings,
                  :removed_queue_bindings, :removed_exchange_bindings

      def self.from_registry(registry)
        new(
          exchanges: registry.exchanges.values.map do |x|
            Exchange.new(name: x.name, type: x.type, durable: x.durable,
                         auto_delete: x.auto_delete, arguments: x.arguments)
          end,
          queues: registry.queues.values.map do |q|
            Queue.new(name: q.name, durable: q.durable, auto_delete: q.auto_delete,
                      exclusive: q.exclusive, arguments: q.arguments)
          end,
          queue_bindings: build_bindings(registry.queue_bindings, :queue),
          exchange_bindings: build_bindings(registry.exchange_bindings, :exchange),
          removed_queue_bindings: build_bindings(registry.removed_queue_bindings, :queue),
          removed_exchange_bindings: build_bindings(registry.removed_exchange_bindings, :exchange)
        )
      end

      def self.build_bindings(values, kind)
        Array(values).map do |b|
          b.is_a?(Binding) ? b : binding_from(b, kind)
        end
      end

      def self.binding_from(b, kind)
        if b.is_a?(Binding)
          b
        elsif b.respond_to?(:source)
          Binding.new(source: b.source, destination: b.destination, routing_key: b.routing_key,
                      arguments: b.arguments, kind: kind)
        else
          values = b.to_h { |k, v| [k.to_sym, v] }
          Binding.new(source: values[:source], destination: values[:destination],
                      routing_key: values[:routing_key], arguments: values[:arguments],
                      kind: kind)
        end
      end

      def initialize(exchanges: [], queues: [],
                     queue_bindings: [], exchange_bindings: [],
                     removed_queue_bindings: [], removed_exchange_bindings: [])
        @exchanges = Array(exchanges).map { |x| x.is_a?(Exchange) ? x : Exchange.new(**symbolize(x)) }
                                    .to_h { |x| [x.name, x] }
        @queues = Array(queues).map { |q| q.is_a?(Queue) ? q : Queue.new(**symbolize(q)) }
                               .to_h { |q| [q.name, q] }
        @queue_bindings = Set.new(normalize_bindings(queue_bindings, :queue))
        @exchange_bindings = Set.new(normalize_bindings(exchange_bindings, :exchange))
        @removed_queue_bindings = Set.new(normalize_bindings(removed_queue_bindings, :queue))
        @removed_exchange_bindings = Set.new(normalize_bindings(removed_exchange_bindings, :exchange))
      end

      def dup
        self.class.new(
          exchanges: exchanges.values.map(&:dup),
          queues: queues.values.map(&:dup),
          queue_bindings: queue_bindings.map(&:dup),
          exchange_bindings: exchange_bindings.map(&:dup),
          removed_queue_bindings: removed_queue_bindings.map(&:dup),
          removed_exchange_bindings: removed_exchange_bindings.map(&:dup)
        )
      end

      def exchange(name)
        exchanges[name]
      end

      def queue(name)
        queues[name]
      end

      def outgoing(name)
        queue_bindings.select { |b| b.source == name } +
          exchange_bindings.select { |b| b.source == name }
      end

      def incoming_exchange_bindings(name)
        exchange_bindings.select { |b| b.destination == name }
      end

      def add_binding(binding)
        if binding.queue_binding?
          queue_bindings.add(binding)
          removed_queue_bindings.delete(binding)
        else
          exchange_bindings.add(binding)
          removed_exchange_bindings.delete(binding)
        end
      end

      def remove_binding(binding)
        if binding.queue_binding?
          !!queue_bindings.delete(binding)
        else
          !!exchange_bindings.delete(binding)
        end
      end

      def remove_queue_binding_permanently(binding)
        removed = remove_binding(binding)
        removed_queue_bindings.add(binding) if removed
        removed
      end

      def remove_exchange_binding_permanently(binding)
        removed = remove_binding(binding)
        removed_exchange_bindings.add(binding) if removed
        removed
      end

      def remove_exchange(name)
        exchanges.delete(name)
        @queue_bindings = Set.new(queue_bindings.reject { |b| b.source == name })
        @exchange_bindings = Set.new(exchange_bindings.reject { |b| b.source == name || b.destination == name })
      end

      def tombstoned?(binding)
        if binding.queue_binding?
          removed_queue_bindings.include?(binding)
        else
          removed_exchange_bindings.include?(binding)
        end
      end

      # Simulates the broker deleting auto-delete exchanges that have lost
      # their last outgoing binding. Returns the names of deleted exchanges.
      def cascade_auto_delete!(candidates)
        deleted = []
        pending = candidates.dup

        until pending.empty?
          name = pending.shift
          x = exchanges[name]
          next unless x && x.auto_delete?
          next unless outgoing(name).empty?

          sources = incoming_exchange_bindings(name).map(&:source)
          remove_exchange(name)
          deleted << name
          pending.concat(sources)
          pending.uniq!
        end

        deleted
      end

      def root_exchanges
        destinations = exchange_bindings.map(&:destination).to_set
        exchanges.keys.reject { |name| destinations.include?(name) }
      end

      def routes_from(entry)
        return [] unless exchange(entry)

        results = []

        walk = lambda do |name, chain, bindings, visited|
          outgoing(name).each do |b|
            if b.queue_binding?
              results << Route.new(entry: entry, exchanges: chain.dup,
                                   queue: b.destination, bindings: bindings + [b])
            elsif !visited.include?(b.destination)
              walk.call(b.destination, chain + [b.destination], bindings + [b],
                        visited + [b.destination])
            end
          end
        end

        walk.call(entry, [], [], [entry])
        results
      end

      def routes(entry_exchanges = nil)
        entries = entry_exchanges || root_exchanges
        routes = entries.flat_map { |entry| routes_from(entry) }
        routes.uniq do |r|
          [r.entry, r.exchanges, r.queue,
           r.bindings.map { |b| [b.source, b.destination, b.routing_key, b.arguments] }]
        end
      end

      private

      def normalize_bindings(values, kind)
        Array(values).map do |b|
          b.is_a?(Binding) ? b : self.class.binding_from(b, kind)
        end
      end

      def symbolize(hash)
        hash.to_h { |k, v| [k.to_sym, v] }
      end
    end

    # The result of {TopologyMigration#preview}.
    class Plan
      attr_reader :mode, :steps, :affected_routes, :preserved_routes,
                  :incompatibilities, :fallbacks, :warnings, :auto_delete_hazard,
                  :fingerprint, :source, :from, :to, :exchanges_affected

      def initialize(mode:, steps:, affected_routes:, preserved_routes:,
                     incompatibilities:, fallbacks:, warnings:, auto_delete_hazard:,
                     fingerprint:, source:, from:, to:, exchanges_affected: [])
        @mode = mode
        @steps = steps
        @affected_routes = affected_routes
        @preserved_routes = preserved_routes
        @incompatibilities = incompatibilities
        @fallbacks = fallbacks
        @warnings = warnings
        @auto_delete_hazard = auto_delete_hazard
        @fingerprint = fingerprint
        @source = source
        @from = from
        @to = to
        @exchanges_affected = exchanges_affected
      end

      def feasible?
        incompatibilities.empty?
      end

      def requires_cutover?
        steps.any?(&:barrier?)
      end

      def duplicate_window?
        warnings.any? { |w| w[:code] == :duplicate_window }
      end

      def to_h
        {
          mode: mode,
          feasible: feasible?,
          source: source,
          from: from,
          to: to,
          steps: steps.map { |s| { id: s.id, action: s.action, phase: s.phase, description: s.to_s } },
          affected_routes: affected_routes.map(&:to_s),
          preserved_routes: preserved_routes.map(&:to_s),
          route_impacts: (affected_routes + preserved_routes).map do |i|
            {
              status: i.status,
              before: i.before&.to_s,
              after: i.after&.to_s,
              before_queue: i.before&.queue,
              after_queue: i.after&.queue
            }
          end,
          incompatibilities: incompatibilities,
          fallbacks: fallbacks,
          warnings: warnings,
          auto_delete_hazard: auto_delete_hazard,
          exchanges_affected: exchanges_affected,
          fingerprint: fingerprint
        }
      end

      def to_s
        lines = []
        lines << "Routing migration #{source}: #{from} => #{to} (mode: #{mode})"
        lines << "Feasible: #{feasible? ? "yes" : "no"}"

        unless affected_routes.empty?
          lines << "Affected business routes:"
          affected_routes.each { |r| lines << "  - #{r}" }
        end

        unless preserved_routes.empty?
          lines << "Preserved business routes:"
          preserved_routes.each { |r| lines << "  - #{r}" }
        end

        unless steps.empty?
          lines << "Expected order:"
          steps.each_with_index { |s, i| lines << "  #{i + 1}. [#{s.phase}] #{s}" }
        end

        unless exchanges_affected.empty?
          lines << "Auto-delete exchanges at risk: #{exchanges_affected.join(", ")}"
        end

        unless warnings.empty?
          lines << "Warnings:"
          warnings.each { |w| lines << "  - #{w[:message]}" }
        end

        unless incompatibilities.empty?
          lines << "Incompatible paths:"
          incompatibilities.each { |i| lines << "  - #{i[:message]}" }

          unless fallbacks.empty?
            lines << "Allowed fallbacks:"
            fallbacks.each { |f| lines << "  - #{f}" }
          end
        end

        lines.join("\n")
      end
    end

    # A local, in-memory adapter that implements RabbitMQ's routing and
    # auto-delete semantics for tests and dry runs.
    #
    # It never opens a network connection.
    class LocalAdapter
      attr_reader :snapshot, :log, :deleted_exchanges

      def initialize(snapshot = Snapshot.new)
        @snapshot = snapshot.dup
        @log = []
        @deleted_exchanges = []
      end

      def declare_queue(name, opts = {})
        options = symbolize(opts)
        snapshot.queues[name] = Queue.new(
          name: name,
          durable: options.fetch(:durable, true),
          auto_delete: options.fetch(:auto_delete, false),
          exclusive: options.fetch(:exclusive, false),
          arguments: options[:arguments]
        )
        log << [:declare_queue, name]
        true
      end

      def declare_exchange(name, type, opts = {})
        options = symbolize(opts)
        snapshot.exchanges[name] = Exchange.new(
          name: name,
          type: type.to_s,
          durable: options.fetch(:durable, true),
          auto_delete: options.fetch(:auto_delete, false),
          arguments: options[:arguments]
        )
        log << [:declare_exchange, name]
        true
      end

      def bind(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        binding = Binding.new(source: source, destination: destination,
                              routing_key: routing_key, arguments: arguments, kind: kind)
        snapshot.add_binding(binding)
        log << [:bind, binding]
        true
      end

      def unbind(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        binding = Binding.new(source: source, destination: destination,
                              routing_key: routing_key, arguments: arguments, kind: kind)
        return false unless binding_present?(source, destination, routing_key: routing_key,
                                                              arguments: arguments, kind: kind)

        if kind == :queue
          snapshot.remove_queue_binding_permanently(binding)
        else
          snapshot.remove_exchange_binding_permanently(binding)
        end
        log << [:unbind, binding]

        # the broker deletes auto-delete exchanges that lost their last
        # outgoing binding, and that takes their incoming bindings with them
        @deleted_exchanges.concat(snapshot.cascade_auto_delete!([source]))
        true
      end

      def binding_present?(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        binding = Binding.new(source: source, destination: destination,
                              routing_key: routing_key, arguments: arguments, kind: kind)
        if kind == :queue
          snapshot.queue_bindings.include?(binding)
        else
          snapshot.exchange_bindings.include?(binding)
        end
      end

      def verify_migration!(step)
        raise IncompatibleTopology, "exchange #{step.source} no longer exists" unless snapshot.exchange(step.source)
        unless snapshot.outgoing(step.source).any? { |b| b.destination == step.destination }
          raise IncompatibleTopology, "step #{step.source} -> #{step.destination} is not satisfied after the migration"
        end

        true
      end

      # Routes a message using AMQP 0-9-1 semantics and returns the queue
      # names it reaches.
      #
      # @param published_to [String] entry exchange name
      # @param routing_key [String, nil]
      # @param headers [Hash]
      # @return [Array<String>]
      def route(published_to:, routing_key: nil, headers: {})
        visited = Set.new
        queues = Set.new

        walk = lambda do |exchange_name|
          next if visited.include?(exchange_name)

          visited.add(exchange_name)
          type = snapshot.exchange(exchange_name)&.type_name

          snapshot.outgoing(exchange_name).each do |b|
            next unless binding_matches?(type, b, routing_key, headers)

            if b.queue_binding?
              queues.add(b.destination)
            else
              walk.call(b.destination)
            end
          end
        end

        walk.call(published_to)
        queues.to_a.sort
      end

      private

      def binding_matches?(type, binding, routing_key, headers)
        case type
        when "fanout"
          true
        when "direct", nil
          normalize_key(binding.routing_key) == normalize_key(routing_key)
        when "topic"
          topic_match?(normalize_key(binding.routing_key), normalize_key(routing_key))
        when "headers"
          headers_match?(binding.arguments || {}, headers)
        else
          # unknown exchange types are treated conservatively
          true
        end
      end

      def normalize_key(key)
        key.nil? ? "" : key.to_s
      end

      def topic_match?(pattern, key)
        pa = pattern.split(".")
        ka = key.split(".")
        memo = {}

        rec = lambda do |i, j|
          cache_key = [i, j]
          return memo[cache_key] if memo.key?(cache_key)

          memo[cache_key] = if i == pa.length && j == ka.length
            true
          elsif i < pa.length && pa[i] == "#"
            rec.call(i + 1, j) || (j < ka.length && rec.call(i, j + 1))
          elsif j < ka.length && ka[j] == "#"
            rec.call(i, j + 1) || (i < pa.length && rec.call(i + 1, j))
          elsif i < pa.length && j < ka.length
            compatible = pa[i] == "*" || ka[j] == "*" || pa[i] == ka[j]
            compatible && rec.call(i + 1, j + 1)
          else
            false
          end
        end

        rec.call(0, 0)
      end

      def headers_match?(arguments, headers)
        match = (arguments["x-match"] || arguments[:'x-match'] || "all").to_s
        pairs = arguments.reject { |k, _| k.to_s == "x-match" }
        return true if pairs.empty?

        results = pairs.map do |k, v|
          headers.key?(k.to_s) && header_value_matches?(headers[k.to_s], v)
        end
        match == "any" ? results.any? : results.all?
      end

      def header_value_matches?(actual, expected)
        expected.is_a?(Array) ? expected.include?(actual) : actual == expected
      end

      def symbolize(hash)
        hash.to_h { |k, v| [k.to_sym, v] }
      end
    end

    # Applies migration steps using a real {Bunny::Channel}.
    #
    # Topology recording is done by the channel methods themselves, so the
    # registry stays the source of truth used by connection recovery.
    class ChannelAdapter
      attr_reader :channel, :registry

      # @param channel [Bunny::Channel]
      def initialize(channel)
        @channel = channel
        @registry = channel.connection.topology_registry
      end

      def declare_queue(name, opts = {})
        channel.queue_declare(name, symbolize(opts))
      end

      def declare_exchange(name, type, opts = {})
        channel.exchange_declare(name, type, symbolize(opts))
      end

      def bind(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        opts = { routing_key: routing_key, arguments: arguments }
        if kind == :queue
          channel.queue_bind(destination, source, opts)
        else
          channel.exchange_bind(source, destination, opts)
        end
      end

      def unbind(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        return false unless binding_present?(source, destination, routing_key: routing_key,
                                                              arguments: arguments, kind: kind)

        opts = { routing_key: routing_key, arguments: arguments }
        if kind == :queue
          channel.queue_unbind(destination, source, opts)
        else
          channel.exchange_unbind(source, destination, opts)
        end
      end

      def binding_present?(source, destination, routing_key: nil, arguments: nil, kind: :queue)
        bindings = kind == :queue ? registry.queue_bindings : registry.exchange_bindings
        bindings.any? do |b|
          b.source == source && b.destination == destination &&
            b.routing_key == routing_key && b.arguments == arguments
        end
      end

      def verify_migration!(step)
        unless channel.connection.exchange_exists?(step.source)
          raise IncompatibleTopology, "exchange #{step.source} no longer exists"
        end
        unless binding_present?(step.source, step.destination)
          raise IncompatibleTopology, "step #{step.source} -> #{step.destination} is not satisfied after the migration"
        end

        true
      end

      private

      def symbolize(hash)
        hash.to_h { |k, v| [k.to_sym, v] }
      end
    end

    attr_reader :registry, :entry_exchanges, :source, :from, :to,
                :routing_key, :arguments, :new_queue_options

    # @param registry [Bunny::TopologyRegistry, Snapshot]
    # @param entry_exchanges [Array<String>, nil] exchanges messages are published to,
    #   used to report business routes. When nil, exchanges without incoming
    #   exchange bindings are used.
    def initialize(registry, entry_exchanges: nil)
      @registry = registry
      @entry_exchanges = entry_exchanges
      @source = nil
      @from = nil
      @to = nil
      @routing_key = nil
      @arguments = nil
      @new_queue_options = nil
      @declare_new_queue = false
    end

    # Describes replacing the `source -> from` queue binding with
    # `source -> to`. The new binding copies the routing key and arguments
    # of the old one unless overridden.
    #
    # @param source [String] exchange to re-point
    # @param from [String] queue currently bound to the exchange
    # @param to [String] queue that should receive the traffic instead
    # @param routing_key [String, nil] routing key for the new binding
    # @param arguments [Hash, nil] arguments for the new binding
    # @param declare_queue [Boolean, Hash] declare the new queue, optionally with options
    # @return [self]
    def move_queue_binding(source:, from:, to:, routing_key: nil, arguments: nil,
                           declare_queue: false)
      @source = source
      @from = from
      @to = to
      @routing_key = routing_key
      @arguments = arguments
      @declare_new_queue = !!declare_queue
      @new_queue_options = declare_queue.is_a?(Hash) ? declare_queue : {}

      self
    end

    # Builds a preview without changing the registry or talking to the
    # broker.
    #
    # @param cutover [:auto, :live, :quiesced, :generation]
    # @return [Plan]
    def preview(cutover: :auto)
      snapshot = build_snapshot

      old_binding = find_queue_binding(snapshot, source, from)
      raise Error, "#{source} is not bound to #{from}" unless old_binding

      new_binding = build_new_binding(old_binding)
      type = exchange_type(snapshot, source)

      overlap = bindings_overlap?(type, old_binding, new_binding)
      hazard = auto_delete_hazard(snapshot, old_binding)
      tombstoned = snapshot.tombstoned?(new_binding)

      mode = cutover == :auto ? choose_mode(overlap: overlap) : cutover
      validate_mode!(mode)

      incompatibilities = []
      warnings = []
      fallbacks = []

      before_routes = snapshot.routes(entry_exchanges)
      after_snapshot = simulate(snapshot, old_binding, new_binding)

      # final state, regardless of how unsafe the transition is
      impacts = build_impacts(before_routes, after_snapshot.routes(entry_exchanges), old_binding, new_binding)
      affected_routes = impacts.reject { |i| i.status == :preserved }
      preserved_routes = impacts.select { |i| i.status == :preserved }

      if snapshot.exchange(source).nil?
        incompatibilities << incompatibility(
          :missing_source,
          "exchange #{source} is not part of the recorded topology"
        )
      end

      if snapshot.queue(to).nil? && !@declare_new_queue
        incompatibilities << incompatibility(
          :missing_destination,
          "queue #{to} is not declared; pass declare_queue: true or declare it before the migration",
          routes: affected_routes
        )
      end

      if tombstoned
        incompatibilities << incompatibility(
          :tombstoned_binding,
          "#{source} -> #{to} was explicitly removed earlier; a migration must not restore it silently",
          routes: affected_routes
        )
      end

      lost = impacts.select { |i| i.status == :lost }
      unless lost.empty?
        incompatibilities << incompatibility(
          :route_loss,
          "the final topology would lose #{lost.map(&:before).join(", ")}",
          routes: lost
        )
      end

      case mode
      when :live
        if overlap
          incompatibilities << incompatibility(
            :overlapping_bindings,
            "#{source} is a #{type} exchange and #{from}/#{to} bindings can match the same message; " \
            "a live swap would deliver it to both #{from} and #{to}",
            routes: affected_routes
          )
          warnings << {
            code: :duplicate_window,
            message: "#{from} and #{to} would both receive messages published during the swap"
          }
        end
      when :quiesced
        # an explicit cutover makes overlapping bindings safe
      when :generation
        unless generation_discriminated?(type, old_binding, new_binding)
          incompatibilities << incompatibility(
            :no_generation_discriminator,
            "#{source} is a #{type} exchange and the #{from}/#{to} bindings cannot be separated by " \
            "generation (routing key or headers), so both generations would reach both queues",
            routes: affected_routes
          )
        end
        unless generation_reaches_source?(snapshot, new_binding)
          incompatibilities << incompatibility(
            :unreachable_generation,
            "messages for the new generation (#{new_binding.routing_key.inspect}) cannot reach " \
            "#{source}: no incoming binding to #{source} matches them",
            routes: affected_routes
          )
        end
      end

      if hazard
        lost_if_removed_first = routes_through_exchange(snapshot, source)
        warnings << {
          code: :auto_delete_hazard,
          message: "#{source} is auto-delete and #{source} -> #{from} is its last outgoing binding. " \
                   "Removing it first would make the broker delete #{source}, taking the incoming " \
                   "bindings #{incoming_edges(snapshot, source)} with it and losing " \
                   "#{lost_if_removed_first.map(&:to_s).uniq.join(", ")}. The plan binds #{to} first."
        }
      end

      feasible = incompatibilities.empty?
      steps = build_steps(type: type, old_binding: old_binding, new_binding: new_binding,
                          hazard: hazard, overlap: overlap, mode: mode)

      if !feasible
        fallbacks.concat(allowed_fallbacks(type: type, hazard: hazard, overlap: overlap,
                                           tombstoned: tombstoned, mode: mode))
      end

      Plan.new(
        mode: mode,
        steps: steps,
        affected_routes: affected_routes,
        preserved_routes: preserved_routes,
        incompatibilities: incompatibilities,
        fallbacks: fallbacks.uniq,
        warnings: warnings,
        auto_delete_hazard: hazard,
        fingerprint: fingerprint(snapshot, old_binding, new_binding, mode),
        source: source, from: from, to: to,
        exchanges_affected: hazard ? [source] : []
      )
    end

    # @return [Hash<Symbol, Plan>]
    def preview_all
      %i[live quiesced generation].to_h { |mode| [mode, preview(cutover: mode)] }
    end

    # @return [Array<Symbol>]
    def feasible_modes
      preview_all.select { |_, plan| plan.feasible? }.keys
    end

    # Applies a feasible plan.
    #
    # The adapter must respond to `declare_queue`, `bind`, `unbind` and
    # `binding_present?`. When a journal name is given, the last confirmed
    # step is stored in the topology registry and a later run continues
    # after it instead of replaying the whole migration.
    #
    # @param plan [Plan]
    # @param adapter [#declare_queue, #bind, #unbind, #binding_present?]
    # @param journal [String, nil]
    # @param on_barrier [#call, nil] called for quiesce/drain barriers
    # @param allow_restart [Boolean] replace a journal whose plan changed
    # @return [Array<String>] ids of steps executed in this run
    def apply(plan, adapter, journal: nil, on_barrier: nil, allow_restart: false)
      unless plan.feasible?
        details = plan.incompatibilities.map { |i| i[:message] }
        raise IncompatibleTopology.new(details.join("; "), plan.incompatibilities)
      end

      if journal && !registry.respond_to?(:begin_migration!)
        raise ArgumentError, "journaling requires a Bunny::TopologyRegistry, not #{registry.class}"
      end

      confirmed = journal ? registry.begin_migration!(journal, plan.fingerprint,
                                                     steps: serialize_steps(plan.steps),
                                                     allow_restart: allow_restart) : Set.new
      executed = []

      plan.steps.each do |step|
        next if confirmed.include?(step.id)

        if step.barrier?
          registry.mark_migration_step_in_flight!(journal, step.id) if journal
          on_barrier.call(step) if on_barrier
          if journal
            registry.confirm_migration_step!(journal, step.id)
            confirmed.add(step.id)
          end
          executed << step.id
          next
        end

        registry.mark_migration_step_in_flight!(journal, step.id) if journal
        execute_step(step, adapter)

        if journal
          registry.confirm_migration_step!(journal, step.id)
          confirmed.add(step.id)
        end
        executed << step.id
      end

      executed
    end

    # Resumes a journaled migration after a reconnect.
    #
    # Uses the steps stored in the journal so it does not depend on the
    # live registry still describing the pre-migration topology. The step
    # that was in flight when the failure happened is retried; steps
    # confirmed before it are not replayed.
    #
    # @param journal [String]
    # @param adapter [#declare_queue, #bind, #unbind, #binding_present?]
    # @param on_barrier [#call, nil]
    # @return [Array<String>]
    def resume(journal, adapter, on_barrier: nil)
      unless registry.respond_to?(:migration_journal_steps)
        raise ArgumentError, "journaling requires a Bunny::TopologyRegistry, not #{registry.class}"
      end

      stored = registry.migration_journal_steps(journal)
      raise Error, "no migration journal named #{journal.inspect}" unless stored

      steps = deserialize_steps(stored)
      confirmed = Set.new(registry.confirmed_migration_steps(journal))
      executed = []

      steps.each do |step|
        next if confirmed.include?(step.id)

        if step.barrier?
          registry.mark_migration_step_in_flight!(journal, step.id)
          on_barrier.call(step) if on_barrier
          registry.confirm_migration_step!(journal, step.id)
          confirmed.add(step.id)
          executed << step.id
          next
        end

        registry.mark_migration_step_in_flight!(journal, step.id)
        execute_step(step, adapter)
        registry.confirm_migration_step!(journal, step.id)
        confirmed.add(step.id)
        executed << step.id
      end

      executed
    end

    # @return [String]
    def fingerprint(snapshot, old_binding, new_binding, mode)
      [
        "mode=#{mode}",
        "source=#{source}",
        "from=#{from}",
        "to=#{to}",
        "old=#{old_binding.routing_key.inspect}/#{old_binding.arguments.inspect}",
        "new=#{new_binding.routing_key.inspect}/#{new_binding.arguments.inspect}",
        "routes=#{snapshot.routes(entry_exchanges).map(&:to_s).sort.join("|")}"
      ].join("\n")
    end

    private

    def serialize_steps(steps)
      steps.map do |s|
        {
          "id" => s.id, "action" => s.action.to_s, "source" => s.source,
          "destination" => s.destination, "routing_key" => s.routing_key,
          "arguments" => s.arguments, "phase" => s.phase.to_s,
          "description" => s.description, "options" => s.options
        }
      end
    end

    def deserialize_steps(stored)
      stored.map do |s|
        Step.new(
          id: s["id"] || s[:id],
          action: (s["action"] || s[:action]).to_sym,
          source: s["source"] || s[:source],
          destination: s["destination"] || s[:destination],
          routing_key: s["routing_key"] || s[:routing_key],
          arguments: s["arguments"] || s[:arguments],
          phase: (s["phase"] || s[:phase]).to_sym,
          description: s["description"] || s[:description],
          options: s["options"] || s[:options]
        )
      end
    end

    def build_snapshot
      registry.is_a?(Snapshot) ? registry.dup : Snapshot.from_registry(registry)
    end

    def find_queue_binding(snapshot, exchange_name, queue_name)
      snapshot.queue_bindings.find do |b|
        b.source == exchange_name && b.destination == queue_name
      end
    end

    def build_new_binding(old_binding)
      Binding.new(
        source: source,
        destination: to,
        routing_key: routing_key.nil? ? old_binding.routing_key : routing_key,
        arguments: arguments.nil? ? old_binding.arguments : arguments,
        kind: :queue
      )
    end

    def exchange_type(snapshot, name)
      x = snapshot.exchange(name)
      x ? x.type_name : "unknown"
    end

    def choose_mode(overlap:)
      overlap ? :quiesced : :live
    end

    def validate_mode!(mode)
      return if %i[live quiesced generation].include?(mode)

      raise ArgumentError, "unknown cutover mode: #{mode.inspect}"
    end

    def generation_discriminated?(type, old_binding, new_binding)
      return false if type == "fanout"

      !bindings_overlap?(type, old_binding, new_binding)
    end

    # Checks that a message for the new generation can actually get to the
    # exchange whose outgoing binding is being added. Incoming bindings
    # are matched according to the type of the exchange they leave from.
    def generation_reaches_source?(snapshot, new_binding)
      incoming = snapshot.incoming_exchange_bindings(source)
      return true if incoming.empty?

      incoming.any? do |b|
        upstream_type = exchange_type(snapshot, b.source)
        bindings_overlap?(upstream_type, b, new_binding)
      end
    end

    # Conservative overlap detection: "can one message match both bindings?"
    def bindings_overlap?(type, old_binding, new_binding)
      case type
      when "fanout"
        true
      when "direct"
        normalize_key(old_binding.routing_key) == normalize_key(new_binding.routing_key)
      when "topic"
        topic_patterns_overlap?(normalize_key(old_binding.routing_key),
                                normalize_key(new_binding.routing_key))
      when "headers"
        headers_overlap?(old_binding.arguments || {}, new_binding.arguments || {})
      else
        true
      end
    end

    def normalize_key(key)
      key.nil? ? "" : key.to_s
    end

    def topic_patterns_overlap?(a, b)
      ta = a.split(".")
      tb = b.split(".")
      memo = {}

      rec = lambda do |i, j|
        key = [i, j]
        return memo[key] if memo.key?(key)

        memo[key] = if i == ta.length && j == tb.length
          true
        elsif i < ta.length && ta[i] == "#"
          rec.call(i + 1, j) || (j < tb.length && rec.call(i, j + 1))
        elsif j < tb.length && tb[j] == "#"
          rec.call(i, j + 1) || (i < ta.length && rec.call(i + 1, j))
        elsif i < ta.length && j < tb.length
          compatible = ta[i] == "*" || tb[j] == "*" || ta[i] == tb[j]
          compatible && rec.call(i + 1, j + 1)
        else
          false
        end
      end

      rec.call(0, 0)
    end

    def headers_overlap?(old_args, new_args)
      old_match = (old_args["x-match"] || old_args[:'x-match'] || "all").to_s
      new_match = (new_args["x-match"] || new_args[:'x-match'] || "all").to_s
      return true unless old_match == "all" && new_match == "all"

      old_headers = normalize_header_keys(old_args)
      new_headers = normalize_header_keys(new_args)

      (old_headers.keys & new_headers.keys).all? { |k| old_headers[k] == new_headers[k] }
    end

    def normalize_header_keys(args)
      args.reject { |k, _| k.to_s == "x-match" }.to_h { |k, v| [k.to_s, v] }
    end

    def auto_delete_hazard(snapshot, old_binding)
      x = snapshot.exchange(old_binding.source)
      return false unless x && x.auto_delete?

      outlets = snapshot.outgoing(old_binding.source)
      outlets.size == 1 && outlets.include?(old_binding)
    end

    def incoming_edges(snapshot, name)
      incoming = snapshot.incoming_exchange_bindings(name)
      return "none" if incoming.empty?

      incoming.map(&:to_s).join(", ")
    end

    def routes_through_exchange(snapshot, name)
      snapshot.routes(entry_exchanges).select do |route|
        route.bindings.any? { |b| b.source == name || b.destination == name }
      end
    end

    def simulate(snapshot, old_binding, new_binding)
      after = snapshot.dup
      after.add_binding(new_binding)
      after.remove_binding(old_binding)
      # the new binding keeps an auto-delete exchange alive, so this
      # should not delete anything; it is here to make that explicit
      after.cascade_auto_delete!([old_binding.source])
      after
    end

    def build_impacts(before_routes, after_routes, old_binding, new_binding)
      after_by_signature = Hash.new { |h, k| h[k] = [] }
      after_routes.each { |r| after_by_signature[route_signature(r)] << r }

      impacts = []

      before_routes.each do |before|
        signature = normalized_signature(before, old_binding, new_binding)
        after = after_by_signature[signature].shift

        if after
          status = before.uses?(old_binding) ? :rewired : :preserved
          impacts << RouteImpact.new(before: before, after: after, status: status)
        else
          impacts << RouteImpact.new(before: before, after: nil, status: :lost)
        end
      end

      after_by_signature.each_value do |routes|
        routes.each { |after| impacts << RouteImpact.new(before: nil, after: after, status: :added) }
      end

      impacts.uniq { |i| [i.status, i.before&.to_s, i.after&.to_s] }
    end

    def route_signature(route)
      [route.entry,
       route.bindings.map { |b| [b.source, b.destination, b.routing_key, b.arguments] },
       route.queue]
    end

    def normalized_signature(route, old_binding, new_binding)
      bindings = route.bindings.map { |b| b == old_binding ? new_binding : b }
      queue = route.bindings.include?(old_binding) ? new_binding.destination : route.queue
      [route.entry,
       bindings.map { |b| [b.source, b.destination, b.routing_key, b.arguments] },
       queue]
    end

    def build_steps(type:, old_binding:, new_binding:, hazard:, overlap:, mode:)
      steps = []
      declare_queue = @declare_new_queue

      if declare_queue
        steps << Step.new(
          id: "declare-queue:#{to}",
          action: :declare_queue,
          destination: to,
          options: @new_queue_options,
          phase: :prepare,
          description: "declare queue #{to}"
        )
      end

      if mode == :quiesced
        steps << barrier_step("quiesce", :prepare,
                              "pause publishers so no message is in flight across the swap")
      end

      steps << Step.new(
        id: "bind:#{source}->#{to}:#{new_binding.routing_key}",
        action: :bind,
        source: source,
        destination: to,
        routing_key: new_binding.routing_key,
        arguments: new_binding.arguments,
        phase: :prepare,
        description: "bind #{source} -> #{to} (before removing the old binding)"
      )

      if mode == :generation
        steps << barrier_step("drain:#{from}", :cutover,
                              "wait for the #{from} generation to drain")
      end

      steps << Step.new(
        id: "unbind:#{source}->#{from}:#{old_binding.routing_key}",
        action: :unbind,
        source: source,
        destination: from,
        routing_key: old_binding.routing_key,
        arguments: old_binding.arguments,
        phase: :cutover,
        description: "unbind #{source} -> #{from}"
      )

      if mode == :quiesced
        steps << barrier_step("resume", :cleanup, "resume publishers")
      end

      steps << Step.new(
        id: "verify:#{source}",
        action: :verify,
        source: source,
        destination: to,
        phase: :verify,
        description: "verify #{source} still exists and routes to #{to}"
      )

      steps
    end

    def barrier_step(name, phase, description)
      Step.new(
        id: "barrier:#{name}",
        action: :barrier,
        phase: phase,
        options: { barrier: name.to_sym },
        description: description
      )
    end

    def execute_step(step, adapter)
      case step.action
      when :declare_queue
        adapter.declare_queue(step.destination, step.options || {})
      when :declare_exchange
        adapter.declare_exchange(step.source, step.options || {})
      when :bind
        binding = Binding.new(source: step.source, destination: step.destination,
                              routing_key: step.routing_key, arguments: step.arguments,
                              kind: :queue)
        if tombstoned?(binding)
          raise IncompatibleTopology.new(
            "#{step.source} -> #{step.destination} was explicitly removed; refusing to restore it",
            [{ code: :tombstoned_binding, binding: binding.to_s }]
          )
        end

        adapter.bind(step.source, step.destination, routing_key: step.routing_key,
                                                    arguments: step.arguments)
      when :unbind
        if adapter.binding_present?(step.source, step.destination,
                                    routing_key: step.routing_key, arguments: step.arguments)
          adapter.unbind(step.source, step.destination, routing_key: step.routing_key,
                                                       arguments: step.arguments)
        end
      when :verify
        adapter.verify_migration!(step) if adapter.respond_to?(:verify_migration!)
      else
        raise Error, "unknown step action: #{step.action.inspect}"
      end
    end

    def tombstoned?(binding)
      if registry.respond_to?(:removed_queue_bindings)
        registry.removed_queue_bindings.include?(binding)
      else
        false
      end
    end

    def incompatibility(code, message, routes: [])
      {
        code: code,
        message: message,
        paths: routes.map(&:to_s).uniq
      }
    end

    def allowed_fallbacks(type:, hazard:, overlap:, tombstoned:, mode:)
      fallbacks = []

      if tombstoned
        fallbacks << "choose another destination queue, or re-create #{source} -> #{to} explicitly " \
                     "outside the migration before previewing again"
      end

      if hazard
        fallbacks << "keep the bind-before-unbind order so #{source} always has an outgoing binding; " \
                     "an explicit quiesced cutover does this"
      end

      if overlap
        fallbacks << "use cutover: :quiesced (pause publishers, bind #{source} -> #{to}, unbind " \
                     "#{source} -> #{from}, resume) so a message cannot reach both queues"

        if type == "fanout"
          fallbacks << "a fanout exchange cannot separate generations; use an explicit quiesced cutover " \
                       "or put a direct/topic/headers exchange in front of #{source}"
        else
          fallbacks << "use cutover: :generation with a generation-specific routing key or headers so " \
                       "each generation matches exactly one queue"
        end
      end

      unless mode == :generation
        fallbacks << "keep #{source} -> #{from} alongside #{source} -> #{to} only if duplicate delivery " \
                     "into both queues is acceptable"
      end

      fallbacks << "redeclare #{source} with auto_delete: false first if the exchange must survive " \
                   "without any outgoing binding (disruptive: requires deleting and recreating it)"

      fallbacks
    end
  end

  # Reads better at call sites focused on routing.
  RoutingMigration = TopologyMigration
end
