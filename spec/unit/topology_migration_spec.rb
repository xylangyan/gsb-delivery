# -*- coding: utf-8 -*-
# frozen_string_literal: true

require "spec_helper"

describe Bunny::RoutingMigration do
  # A topology equivalent to the one in the task:
  #
  #   E -> Q            (untouched business flow, routing key "audit")
  #   E -> X -> Q       (the route being moved)
  #   E -> Y -> X -> Q  (the older chain that keeps X alive)
  #   E -> R            (a different, untouched business flow)
  #
  # X is auto-delete. F describe is not used so the example group name
  # stays searchable.
  def example_topology
    {
      exchanges: [
        { name: "E", type: :direct },
        { name: "X", type: :direct, auto_delete: true },
        { name: "Y", type: :direct }
      ],
      queues: [
        { name: "Q" },
        { name: "R" }
      ],
      queue_bindings: [
        { source: "E", destination: "Q", routing_key: "audit" },
        { source: "X", destination: "Q", routing_key: "orders" },
        { source: "E", destination: "R", routing_key: "other" }
      ],
      exchange_bindings: [
        { source: "E", destination: "X", routing_key: "orders" },
        { source: "E", destination: "Y", routing_key: "orders" },
        { source: "Y", destination: "X", routing_key: "orders" }
      ]
    }
  end

  def snapshot(topology = example_topology)
    Bunny::RoutingMigration::Snapshot.new(**topology)
  end

  def planner(topology = example_topology, entry: "E")
    m = described_class.new(snapshot(topology), entry_exchanges: [entry])
    m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")
    m
  end

  describe "preview" do
    it "does not change the registry or the snapshot" do
      snap = snapshot
      m = described_class.new(snap, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R")

      before = snap.queue_bindings.map(&:to_s).sort
      m.preview

      expect(snap.queue_bindings.map(&:to_s).sort).to eq before
      expect(snap.exchange_bindings.size).to eq 3
    end

    it "reports both business routes through X, including the Y chain" do
      plan = planner.preview(cutover: :quiesced)

      affected = plan.affected_routes.map(&:to_s)
      expect(affected).to include("E -> X -> Q => E -> X -> R")
      expect(affected).to include("E -> Y -> X -> Q => E -> Y -> X -> R")
    end

    it "keeps the unrelated E -> Q and E -> R routes" do
      plan = planner.preview(cutover: :quiesced)

      preserved = plan.preserved_routes.map(&:to_s)
      expect(preserved).to include("E -> Q")
      expect(preserved).to include("E -> R")
    end

    it "binds the new destination before unbinding the last one so X is not auto-deleted" do
      plan = planner.preview(cutover: :quiesced)

      bind_index = plan.steps.index { |s| s.action == :bind && s.destination == "R" }
      unbind_index = plan.steps.index { |s| s.action == :unbind && s.destination == "Q" }

      expect(bind_index).to be < unbind_index
      expect(plan.auto_delete_hazard).to be true
      expect(plan.steps.last.action).to eq :verify
    end

    it "flags the auto-delete exchange and the incoming edges that would be lost" do
      plan = planner.preview(cutover: :quiesced)

      warning = plan.warnings.find { |w| w[:code] == :auto_delete_hazard }
      expect(warning).not_to be_nil
      expect(warning[:message]).to include("E -> X")
      expect(warning[:message]).to include("Y -> X")
      expect(plan.exchanges_affected).to eq ["X"]
    end

    it "is not feasible as a live swap because both bindings use the same direct routing key" do
      plan = planner.preview(cutover: :live)

      expect(plan).not_to be_feasible
      codes = plan.incompatibilities.map { |i| i[:code] }
      expect(codes).to include(:overlapping_bindings)
      expect(plan.duplicate_window?).to be true
      expect(plan.fallbacks.join(" ")).to include("quiesced")
    end

    it "is not feasible with a generation cutover either because a direct binding cannot be discriminated" do
      plan = planner.preview(cutover: :generation)

      expect(plan).not_to be_feasible
      expect(plan.incompatibilities.map { |i| i[:code] }).to include(:no_generation_discriminator)
    end

    it "is feasible with an explicit quiesced cutover" do
      plan = planner.preview(cutover: :auto)

      expect(plan.mode).to eq :quiesced
      expect(plan).to be_feasible
      expect(plan.requires_cutover?).to be true
    end

    it "chooses the live mode when the routing keys are disjoint" do
      topology = example_topology
      topology[:queue_bindings] = [
        { source: "E", destination: "Q", routing_key: "orders" },
        { source: "X", destination: "Q", routing_key: "orders" },
        { source: "E", destination: "R", routing_key: "other" }
      ]

      m = described_class.new(snapshot(topology), entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders.v2")
      plan = m.preview(cutover: :auto)

      expect(plan.mode).to eq :live
      expect(plan).to be_feasible
    end

    it "keeps E and Y edges when simulating the unsafe remove-first order for diagnostics" do
      plan = planner.preview(cutover: :generation)

      expect(plan).not_to be_feasible
      incompatible_paths = plan.incompatibilities.flat_map { |i| i[:paths] }.join(" ")
      expect(incompatible_paths).to include("E -> X -> Q")
      expect(incompatible_paths).to include("E -> Y -> X -> Q")
    end

    it "reports every mode with preview_all" do
      modes = planner.preview_all

      expect(modes.keys).to eq %i[live quiesced generation]
      expect(modes[:quiesced]).to be_feasible
    end

    it "exposes structured route impacts" do
      plan = planner.preview(cutover: :quiesced)
      impacts = plan.to_h[:route_impacts]

      rewired = impacts.select { |i| i[:status] == :rewired }
      expect(rewired.map { |i| i[:before_queue] }).to all(eq "Q")
      expect(rewired.map { |i| i[:after_queue] }).to all(eq "R")
      expect(impacts.select { |i| i[:status] == :preserved }.map { |i| i[:before_queue] })
        .to include("Q", "R")
    end

    it "raises when the source exchange is not bound to the old queue" do
      m = described_class.new(snapshot, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "missing", to: "R")

      expect { m.preview }.to raise_error(Bunny::TopologyMigrationError)
    end

    it "reports a missing destination queue unless it is declared by the plan" do
      m = described_class.new(snapshot, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "brand-new")

      plan = m.preview(cutover: :quiesced)
      expect(plan).not_to be_feasible
      expect(plan.incompatibilities.map { |i| i[:code] }).to include(:missing_destination)

      declared = described_class.new(snapshot, entry_exchanges: ["E"])
      declared.move_queue_binding(source: "X", from: "Q", to: "brand-new", declare_queue: true)
      plan = declared.preview(cutover: :quiesced)

      expect(plan).to be_feasible
      expect(plan.steps.first.action).to eq :declare_queue
    end
  end

  describe "apply with the local adapter" do
    it "moves X -> Q to X -> R and leaves X in place" do
      m = planner
      plan = m.preview(cutover: :quiesced)
      adapter = described_class::LocalAdapter.new(snapshot)

      executed = m.apply(plan, adapter)

      expect(executed).not_to be_empty
      expect(adapter.route(published_to: "E", routing_key: "orders")).to eq %w[R]
      expect(adapter.snapshot.exchange("X")).not_to be_nil
      expect(adapter.deleted_exchanges).to be_empty
    end

    it "removes X -> Q and adds X -> R" do
      m = planner
      adapter = described_class::LocalAdapter.new(snapshot)

      m.apply(m.preview(cutover: :quiesced), adapter)

      queue_bindings = adapter.snapshot.queue_bindings.map(&:to_s)
      expect(queue_bindings).to include("X -> R (rk=orders)")
      expect(queue_bindings).not_to include("X -> Q (rk=orders)")
    end

    it "preserves the E -> X and Y -> X incoming bindings" do
      m = planner
      adapter = described_class::LocalAdapter.new(snapshot)

      m.apply(m.preview(cutover: :quiesced), adapter)

      exchange_bindings = adapter.snapshot.exchange_bindings.map(&:to_s)
      expect(exchange_bindings).to include("E -> X (rk=orders)")
      expect(exchange_bindings).to include("Y -> X (rk=orders)")
    end

    it "refuses to apply an infeasible live plan" do
      m = planner
      adapter = described_class::LocalAdapter.new(snapshot)
      plan = m.preview(cutover: :live)

      expect { m.apply(plan, adapter) }.to raise_error(Bunny::IncompatibleTopology)
      expect(adapter.log).to be_empty
    end

    it "does not deliver a message to both Q and R when using quiesced cutover" do
      m = planner
      adapter = described_class::LocalAdapter.new(snapshot)
      plan = m.preview(cutover: :quiesced)

      # before the cutover a message only reaches Q
      expect(adapter.route(published_to: "E", routing_key: "orders")).to eq %w[Q]

      m.apply(plan, adapter)

      # after the cutover it only reaches R
      expect(adapter.route(published_to: "E", routing_key: "orders")).to eq %w[R]
    end
  end

  describe "auto-delete protection" do
    it "would delete X and lose E/Y edges if the old binding were removed first" do
      snap = snapshot
      adapter = described_class::LocalAdapter.new(snap)

      # deliberately unsupported order, to pin down the hazard being modelled
      adapter.unbind("X", "Q", routing_key: "orders")

      expect(adapter.deleted_exchanges).to eq ["X"]
      expect(adapter.snapshot.exchange("X")).to be_nil
      # the edges into X are gone; E -> Y itself remains
      expect(adapter.snapshot.exchange_bindings.map(&:to_s)).to eq ["E -> Y (rk=orders)"]
    end

    it "keeps X alive when the new binding is added first" do
      snap = snapshot
      adapter = described_class::LocalAdapter.new(snap)

      adapter.bind("X", "R", routing_key: "orders")
      adapter.unbind("X", "Q", routing_key: "orders")

      expect(adapter.deleted_exchanges).to be_empty
      expect(adapter.snapshot.exchange("X")).not_to be_nil
    end
  end

  describe "generation cutover" do
    def fanout_topology
      {
        exchanges: [
          { name: "E", type: :fanout },
          { name: "X", type: :fanout, auto_delete: true }
        ],
        queues: [{ name: "Q" }, { name: "R" }],
        queue_bindings: [
          { source: "E", destination: "Q", routing_key: nil },
          { source: "X", destination: "Q", routing_key: nil }
        ],
        exchange_bindings: [
          { source: "E", destination: "X", routing_key: nil }
        ]
      }
    end

    it "says a fanout exchange cannot discriminate generations" do
      m = described_class.new(snapshot(fanout_topology), entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R")
      plan = m.preview(cutover: :generation)

      expect(plan).not_to be_feasible
      codes = plan.incompatibilities.map { |i| i[:code] }
      expect(codes).to include(:no_generation_discriminator)
      expect(plan.fallbacks.join(" ")).to include("fanout")
    end

    it "accepts a topic exchange with a generation-specific routing key" do
      topology = {
        exchanges: [
          { name: "E", type: :topic },
          { name: "X", type: :topic, auto_delete: true }
        ],
        queues: [{ name: "Q" }, { name: "R" }],
        queue_bindings: [
          { source: "E", destination: "Q", routing_key: "orders.v1" },
          { source: "X", destination: "Q", routing_key: "orders.v1" }
        ],
        exchange_bindings: [
          { source: "E", destination: "X", routing_key: "orders.#" }
        ]
      }

      m = described_class.new(snapshot(topology), entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders.v2")
      plan = m.preview(cutover: :generation)

      expect(plan).to be_feasible
      expect(plan.steps.map(&:action)).to include(:barrier)
    end

    it "rejects a generation that cannot reach the exchange being re-pointed" do
      topology = {
        exchanges: [
          { name: "E", type: :topic },
          { name: "X", type: :topic, auto_delete: true }
        ],
        queues: [{ name: "Q" }, { name: "R" }],
        queue_bindings: [
          { source: "E", destination: "Q", routing_key: "orders.v1" },
          { source: "X", destination: "Q", routing_key: "orders.v1" }
        ],
        exchange_bindings: [
          { source: "E", destination: "X", routing_key: "orders.v1" }
        ]
      }

      m = described_class.new(snapshot(topology), entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders.v2")
      plan = m.preview(cutover: :generation)

      expect(plan).not_to be_feasible
      expect(plan.incompatibilities.map { |i| i[:code] }).to include(:unreachable_generation)
    end

    it "routes each generation to exactly one queue" do
      topology = {
        exchanges: [
          { name: "E", type: :topic },
          { name: "X", type: :topic, auto_delete: true }
        ],
        queues: [{ name: "Q" }, { name: "R" }],
        queue_bindings: [
          { source: "E", destination: "Q", routing_key: "orders.v1" },
          { source: "X", destination: "Q", routing_key: "orders.v1" }
        ],
        exchange_bindings: [
          { source: "E", destination: "X", routing_key: "orders.#" }
        ]
      }

      m = described_class.new(snapshot(topology), entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders.v2")
      adapter = described_class::LocalAdapter.new(snapshot(topology))

      m.apply(m.preview(cutover: :generation), adapter)

      # each generation reaches exactly one of Q and R
      expect(adapter.route(published_to: "E", routing_key: "orders.v1")).to eq %w[Q]
      expect(adapter.route(published_to: "E", routing_key: "orders.v2")).to eq %w[R]
    end
  end

  describe "tombstoned bindings" do
    it "reports a binding explicitly removed earlier as incompatible" do
      topo = example_topology
      reg = Bunny::TopologyRegistry.new
      topo[:exchanges].each { |x| reg.record_exchange_with(nil, x[:name], x[:type], true, !!x[:auto_delete], {}) }
      topo[:queues].each { |q| reg.record_queue_with(nil, q[:name], false, true, false, false, {}) }
      topo[:queue_bindings].each { |b| reg.record_queue_binding_with(nil, b[:source], b[:destination], b[:routing_key], {}) }
      topo[:exchange_bindings].each { |b| reg.record_exchange_binding_with(nil, b[:source], b[:destination], b[:routing_key], {}) }

      # an operator explicitly removed X -> R before
      reg.record_queue_binding_with(nil, "X", "R", "orders", {})
      reg.delete_recorded_queue_binding(nil, "X", "R", "orders", {})

      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      expect(m.preview(cutover: :quiesced)).not_to be_feasible
    end

    it "does not resurrect a tombstoned binding during apply" do
      topo = example_topology
      reg = Bunny::TopologyRegistry.new
      topo[:exchanges].each { |x| reg.record_exchange_with(nil, x[:name], x[:type], true, !!x[:auto_delete], {}) }
      topo[:queues].each { |q| reg.record_queue_with(nil, q[:name], false, true, false, false, {}) }
      topo[:queue_bindings].each { |b| reg.record_queue_binding_with(nil, b[:source], b[:destination], b[:routing_key], {}) }
      topo[:exchange_bindings].each { |b| reg.record_exchange_binding_with(nil, b[:source], b[:destination], b[:routing_key], {}) }
      reg.record_queue_binding_with(nil, "X", "R", "orders", {})
      reg.delete_recorded_queue_binding(nil, "X", "R", "orders", {})

      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      # forge a feasible plan and try to run it anyway
      plan = m.preview(cutover: :quiesced)
      step = Bunny::RoutingMigration::Step.new(
        id: "bind:X->R:orders", action: :bind, source: "X", destination: "R",
        routing_key: "orders", arguments: {}, phase: :prepare
      )
      forged = plan.dup
      forged.instance_variable_set(:@steps, [step])

      adapter = described_class::LocalAdapter.new(snapshot)
      expect { m.apply(forged, adapter) }.to raise_error(Bunny::IncompatibleTopology)
    end
  end

  describe "journaling and resume" do
    def registry_with(topology = example_topology)
      reg = Bunny::TopologyRegistry.new
      topology[:exchanges].each do |x|
        reg.record_exchange_with(nil, x[:name], x[:type], true, !!x[:auto_delete], {})
      end
      topology[:queues].each do |q|
        reg.record_queue_with(nil, q[:name], false, true, false, false, {})
      end
      topology[:queue_bindings].each do |b|
        reg.record_queue_binding_with(nil, b[:source], b[:destination], b[:routing_key], {})
      end
      topology[:exchange_bindings].each do |b|
        reg.record_exchange_binding_with(nil, b[:source], b[:destination], b[:routing_key], {})
      end
      reg
    end

    class FailingAdapter < Bunny::RoutingMigration::LocalAdapter
      attr_accessor :fail_on

      def unbind(*args, **kwargs)
        raise IOError, "simulated reconnect" if fail_on == :unbind

        super
      end
    end

    it "resumes after the last confirmed step and does not replay confirmed ones" do
      reg = registry_with
      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      adapter = FailingAdapter.new(Bunny::RoutingMigration::Snapshot.from_registry(reg))
      adapter.fail_on = :unbind

      plan = m.preview(cutover: :quiesced)
      expect { m.apply(plan, adapter, journal: "orders-repoint") }.to raise_error(IOError)

      expect(reg.confirmed_migration_steps("orders-repoint")).to include("barrier:quiesce", "bind:X->R:orders")
      expect(reg.confirmed_migration_steps("orders-repoint")).not_to include("unbind:X->Q:orders")
      expect(reg.in_flight_migration_step("orders-repoint")).to eq "unbind:X->Q:orders"

      adapter.fail_on = nil
      executed = m.resume("orders-repoint", adapter)

      expect(executed).to eq %w[unbind:X->Q:orders barrier:resume verify:X]
      expect(adapter.route(published_to: "E", routing_key: "orders")).to include("R")
      expect(adapter.route(published_to: "E", routing_key: "orders")).not_to include("Q")
      expect(adapter.deleted_exchanges).to be_empty
    end

    it "does not repeat work when apply is called again after a successful run" do
      reg = registry_with
      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      adapter = described_class::LocalAdapter.new(Bunny::RoutingMigration::Snapshot.from_registry(reg))
      plan = m.preview(cutover: :quiesced)
      m.apply(plan, adapter, journal: "orders-repoint")

      expect(m.apply(plan, adapter, journal: "orders-repoint")).to be_empty
    end

    it "refuses to reuse a journal for a different plan" do
      reg = registry_with
      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      adapter = described_class::LocalAdapter.new(Bunny::RoutingMigration::Snapshot.from_registry(reg))
      m.apply(m.preview(cutover: :quiesced), adapter, journal: "orders-repoint")

      m2 = described_class.new(reg, entry_exchanges: ["E"])
      m2.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders.changed")

      expect do
        m2.apply(m2.preview(cutover: :quiesced), adapter, journal: "orders-repoint")
      end.to raise_error(Bunny::TopologyMigrationConflict)
    end

    it "can resume after the registry was reset by a reconnect" do
      reg = registry_with
      m = described_class.new(reg, entry_exchanges: ["E"])
      m.move_queue_binding(source: "X", from: "Q", to: "R", routing_key: "orders")

      adapter = FailingAdapter.new(Bunny::RoutingMigration::Snapshot.from_registry(reg))
      adapter.fail_on = :unbind
      plan = m.preview(cutover: :quiesced)
      expect { m.apply(plan, adapter, journal: "orders-repoint") }.to raise_error(IOError)

      # a reconnect rebuilds the registry from the broker; the journal survives
      stored = reg.migration_journal_steps("orders-repoint")
      confirmed = reg.confirmed_migration_steps("orders-repoint")

      fresh = Bunny::TopologyRegistry.new
      fresh.begin_migration!("orders-repoint", plan.fingerprint, steps: stored)
      confirmed.each { |id| fresh.confirm_migration_step!("orders-repoint", id) }

      m2 = described_class.new(fresh, entry_exchanges: ["E"])
      adapter.fail_on = nil
      executed = m2.resume("orders-repoint", adapter)

      expect(executed).to eq %w[unbind:X->Q:orders barrier:resume verify:X]
      expect(adapter.route(published_to: "E", routing_key: "orders")).to include("R")
      expect(adapter.route(published_to: "E", routing_key: "orders")).not_to include("Q")
    end
  end

  describe "registry cascade on unbind" do
    it "records a tombstone that recovery can consult" do
      reg = Bunny::TopologyRegistry.new
      reg.record_exchange_with(nil, "X", :direct, true, false, {})
      reg.record_queue_with(nil, "Q", false, true, false, false, {})
      reg.record_queue_binding_with(nil, "X", "Q", "orders", {})

      reg.delete_recorded_queue_binding(nil, "X", "Q", "orders", {})

      expect(reg.queue_binding_tombstoned?("X", "Q", "orders", {})).to be true
      expect(reg.queue_binding_tombstoned?("X", "Q", "other", {})).to be false
    end
    it "removes an auto-delete exchange when its last queue binding is unregistered" do
      reg = Bunny::TopologyRegistry.new
      reg.record_exchange_with(nil, "X", :direct, true, true, {})
      reg.record_exchange_with(nil, "E", :direct, true, false, {})
      reg.record_queue_with(nil, "Q", false, true, false, false, {})
      reg.record_queue_binding_with(nil, "X", "Q", "orders", {})
      reg.record_exchange_binding_with(nil, "E", "X", "orders", {})

      reg.delete_recorded_queue_binding(nil, "X", "Q", "orders", {})

      expect(reg.exchanges).not_to have_key("X")
      expect(reg.exchange_bindings).to be_empty
      expect(reg.removed_queue_bindings.map { |b| [b.source, b.destination, b.routing_key] })
        .to include(["X", "Q", "orders"])
    end
    it "keeps a non-auto-delete exchange when its last queue binding is unregistered" do
      reg = Bunny::TopologyRegistry.new
      reg.record_exchange_with(nil, "X", :direct, true, false, {})
      reg.record_queue_with(nil, "Q", false, true, false, false, {})
      reg.record_queue_binding_with(nil, "X", "Q", "orders", {})

      reg.delete_recorded_queue_binding(nil, "X", "Q", "orders", {})

      expect(reg.exchanges).to have_key("X")
    end

    it "forgets the tombstone when the binding is re-established" do
      reg = Bunny::TopologyRegistry.new
      reg.record_exchange_with(nil, "X", :direct, true, false, {})
      reg.record_queue_with(nil, "Q", false, true, false, false, {})
      reg.record_queue_binding_with(nil, "X", "Q", "orders", {})
      reg.delete_recorded_queue_binding(nil, "X", "Q", "orders", {})

      reg.record_queue_binding_with(nil, "X", "Q", "orders", {})

      expect(reg.removed_queue_bindings).to be_empty
    end
  end
end
