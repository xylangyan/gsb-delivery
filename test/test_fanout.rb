require_relative "helper"
require_relative "support/fanout_recorder"
require_relative "support/test_enumerable_source"

# Covers the multi-route receiving interface requested by the work order:
# isolated groups, retained references, close-time reads, identity-based
# sharing, close markers and the fixed-byte copy budget.
class TestFanout < Kiba::Test
  include FanoutTestSupport

  def rec(tag, **options)
    Recorder.new(tag, **options)
  end

  def run_job(rows, work_order, transforms: [])
    mux = nil
    job = Kiba.parse do
      source TestEnumerableSource, rows
      transforms.each { |transform| transform.call(self) }
      destination Kiba::Fanout::Multiplexer, work_order, on_init: ->(instance) { mux = instance }
    end
    Kiba.run(job)
    mux
  end

  def sample_order(overrides = {})
    {
      order_id: "A",
      lines: [{amount: 10}, {amount: 20}],
      adjustments: [{amount: 1}]
    }.merge(overrides)
  end

  def test_picking_annotation_is_invisible_to_settlement_and_archive
    picking = rec("picking", action: ->(row) { row[:lines].each { |line| line[:picked] = true } })
    settlement = rec("settlement")
    archive = rec("archive", close_snapshot: true)

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest do
        node "lines", bytes: 48, whole: true
        node "adjustments", bytes: 16, whole: true
      end

      group :picking do
        destination picking.destination_class
        mutable "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end

      group :settlement do
        destination settlement.destination_class
        on_write reads: ["lines.*.amount"]
      end

      group :archive do
        destination archive.destination_class
        retain_first "lines"
        on_close reads: ["lines.*.amount"]
      end
    end

    run_job([sample_order], work_order)

    assert picking.rows.fetch(0)[:lines].all? { |line| line[:picked] }
    refute settlement.rows.fetch(0)[:lines].any? { |line| line.key?(:picked) }
    refute archive.rows.fetch(0)[:lines].any? { |line| line.key?(:picked) }
  end

  def test_same_registered_adjustments_reference_keeps_one_identity
    picking = rec("picking")
    review = rec("review")

    shared = [{amount: 1}]
    rows = [
      sample_order(order_id: "A", adjustments: shared),
      sample_order(order_id: "B", adjustments: shared)
    ]

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "adjustments", bytes: 16, whole: true }
      group :picking do
        destination picking.destination_class
        destination review.destination_class
        reference "adjustments"
      end
    end

    mux = run_job(rows, work_order)

    ids = (picking.rows + review.rows).map { |row| row[:adjustments_id] }
    # Both orders and both receivers share one retained list, but it is a
    # delivery-time snapshot rather than the mutable upstream original.
    assert_equal 1, ids.uniq.size
    refute_equal shared.__id__, ids.first
    assert_equal 0, mux.total[:total_bytes]
  end

  def test_equal_but_distinct_adjustments_are_not_merged
    picking = rec("picking")
    one = [{amount: 1}]
    two = [{amount: 1}]

    rows = [
      sample_order(order_id: "A", adjustments: one),
      sample_order(order_id: "B", adjustments: two)
    ]

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "adjustments", bytes: 16, whole: true }
      group :picking do
        destination picking.destination_class
        reference "adjustments"
      end
    end

    run_job(rows, work_order)

    ids = picking.rows.map { |row| row[:adjustments_id] }
    refute_equal ids[0], ids[1]
  end

  def test_archiver_retained_page_is_not_rewritten_by_later_rows
    shared = [{amount: 10}]
    archiver = rec("archive", close_snapshot: true)

    rows = [
      {order_id: "A", lines: shared},
      {order_id: "B", lines: shared}
    ]

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :archive do
        destination archiver.destination_class
        retain_first "lines"
        on_close reads: ["lines.*.amount"]
      end
    end

    run_job(rows, work_order)

    # The archiver keeps the reference it received first; the second delivery
    # cannot rewrite that page.
    assert_equal [10], archiver.rows.fetch(0)[:lines].map { |line| line[:amount] }
  end

  def test_transform_yield_then_return_delivers_both_variants_separately
    sink = rec("sink")

    transform = Class.new do
      def process(row)
        yield({order_id: row[:order_id], lines: [{amount: 1}]})
        row[:lines] = [{amount: 999}]
        {order_id: row[:order_id], lines: row[:lines]}
      end
    end

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      group :sink do
        destination sink.destination_class
      end
    end

    run_job([{order_id: "A", lines: []}], work_order, transforms: [
      ->(context) { context.transform transform }
    ])

    assert_equal [[1], [999]], sink.rows.map { |row| row[:lines].map { |line| line[:amount] } }
  end

  def test_close_yield_is_a_new_row_seen_only_by_groups_that_received_the_order
    gate = rec("gate")
    open = rec("open")

    transform = Class.new do
      def initialize(gate_order)
        @gate_order = gate_order
        @seen = false
      end

      def process(row)
        @seen = true if row[:order_id] == @gate_order
        row
      end

      def close
        yield({order_id: @gate_order, marker: true}) if @seen
      end
    end

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      group :gate do
        destination gate.destination_class
        close_only_if_received
      end

      group :open do
        destination open.destination_class
      end
    end

    run_job([{order_id: "A", lines: []}], work_order, transforms: [
      ->(context) { context.transform transform, "A" }
    ])

    assert_equal ["A", "A"], gate.rows.map { |row| row[:order_id] }
    assert_equal ["A", "A"], open.rows.map { |row| row[:order_id] }
    assert gate.rows.last[:marker] || gate.rows.last[:order_id] == "A"
  end

  def test_close_marker_for_unreceived_order_is_not_delivered
    gate = rec("gate")

    transform = Class.new do
      def process(row)
        row
      end

      def close
        yield({order_id: "Z", marker: true})
      end
    end

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      group :gate do
        destination gate.destination_class
        close_only_if_received
      end
    end

    run_job([{order_id: "A", lines: []}], work_order, transforms: [
      ->(context) { context.transform transform }
    ])

    assert_equal ["A"], gate.rows.map { |row| row[:order_id] }
  end

  def test_same_list_mounted_on_two_orders_still_gets_independent_writable_versions
    picking = rec("picking", action: ->(row) { row[:lines].each { |line| line[:picked] = true } })
    shared = [{amount: 10}]
    rows = [
      {order_id: "A", lines: shared},
      {order_id: "B", lines: shared}
    ]

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :picking do
        destination picking.destination_class
        mutable "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end
    end

    mux = run_job(rows, work_order)

    assert picking.rows.all? { |row| row[:lines].all? { |line| line[:picked] } }
    refute_equal picking.rows[0][:lines_id], picking.rows[1][:lines_id]
    assert_equal 96, mux.total[:total_bytes]
    assert_equal 2, mux.total[:versions].size
  end

  def test_group_required_mutation_is_visible_to_a_second_receiver_in_same_group
    first = rec("first", action: ->(row) { row[:lines].each { |line| line[:reviewed] = true } })
    second = rec("second")

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :picking do
        destination first.destination_class
        destination second.destination_class
        mutable "lines.*.reviewed"
        require_mutation "lines.*.reviewed"
        on_write writes: ["lines.*.reviewed"]
      end
    end

    mux = run_job([{order_id: "A", lines: [{amount: 1}]}], work_order)

    assert second.rows.fetch(0)[:lines].all? { |line| line[:reviewed] }
    assert_equal 48, mux.total[:total_bytes]
    assert_equal "in-group share across 2 receivers", mux.total[:versions].first[:determiner]
  end

  def test_cross_group_visibility_request_is_reported_with_both_original_orders
    settle = rec("settle")
    picking = rec("picking", action: ->(row) { row[:lines].each { |line| line[:picked] = true } })

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :picking do
        destination picking.destination_class
        mutable "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end
      group :settle do
        destination settle.destination_class
        require_from :picking, "lines.*.picked"
      end
    end

    mux = run_job([sample_order(order_id: "ORD-7")], work_order)

    refute_empty mux.conflicts
    conflict = mux.conflicts.first
    assert_equal :cross_group_requirement, conflict.kind
    observations = mux.conflict_observations(conflict)
    assert_equal %i[picking settle], observations.map { |observation| observation[:group] }.sort
    assert_equal ["ORD-7"], observations.map { |observation| observation[:order_id] }.uniq
  end

  def test_quota_is_checked_against_fixed_bytes_not_ruby_heap
    picking = rec("picking")
    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      self.conflict_mode = :raise
      quota bytes: 47
      manifest { node "lines", bytes: 48, whole: true }
      group :picking do
        destination picking.destination_class
        mutable "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end
    end

    error = assert_raises(Kiba::Fanout::ConflictError) do
      run_job([{order_id: "A", lines: [{amount: 1}]}], work_order)
    end
    assert_equal :quota_exceeded, error.conflicts.first.kind
  end

  def test_archiver_first_page_survives_upstream_mutation_of_same_list
    shared = [{amount: 10}]
    archiver = rec("archive", close_snapshot: true)

    mutate = Class.new do
      def process(row)
        row[:lines] << {amount: 99} if row[:order_id] == "B"
        row
      end
    end

    rows = [
      {order_id: "A", lines: shared},
      {order_id: "B", lines: shared}
    ]

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :archive do
        destination archiver.destination_class
        retain_first "lines"
        on_close reads: ["lines.*.amount"]
      end
    end

    run_job(rows, work_order, transforms: [->(context) { context.transform mutate }])

    assert_equal [10], archiver.rows.fetch(0)[:lines].map { |line| line[:amount] }
  end

  def test_close_marker_only_goes_to_its_named_group
    gate = rec("gate")
    other_gate = rec("other_gate")

    transform = Class.new do
      def process(row)
        row
      end

      def close
        yield({order_id: "A", marker: :closed})
      end
    end

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      group :gate do
        destination gate.destination_class
        close_only_if_received "A"
      end
      group :other_gate do
        destination other_gate.destination_class
        close_only_if_received "Z"
      end
    end

    run_job([{order_id: "A", lines: []}], work_order, transforms: [
      ->(context) { context.transform transform }
    ])

    assert_equal 2, gate.rows.size
    assert_equal 1, other_gate.rows.size
    assert_equal :closed, gate.rows.last[:marker]
    assert_nil other_gate.rows.last[:marker]
  end

  def test_report_lists_conflict_observations_and_budget_decisions
    picking = rec("picking")
    settle = rec("settle")

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest { node "lines", bytes: 48, whole: true }
      group :picking do
        destination picking.destination_class
        mutable "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end
      group :settle do
        destination settle.destination_class
        require_from :picking, "lines.*.picked"
      end
    end

    mux = run_job([{order_id: "R-1", lines: [{amount: 1}]}], work_order)
    report = mux.report

    assert_equal 1, report[:conflicts].size
    conflict = report[:conflicts].first
    assert_equal :cross_group_requirement, conflict[:kind]
    assert_equal %i[picking settle], conflict[:observations].map { |entry| entry[:group] }.sort
    assert_equal ["R-1"], conflict[:observations].map { |entry| entry[:order_id] }.uniq
    refute_empty report[:budget][:decisions]
  end

  def test_sample_scenario_reports_minimum_bytes_and_deciding_retained_version
    load File.expand_path("../examples/fanout_receiving.rb", __dir__)

    shared_lines = [{amount: 10}, {amount: 20}]
    shared_adjustments = [{reason: :price, amount: 5}]
    rows = [
      {order_id: "A-1", lines: shared_lines, adjustments: shared_adjustments},
      {order_id: "A-2", lines: shared_lines, adjustments: shared_adjustments}
    ]

    result = Sample.run(rows, close_marker_for: "A-1")
    mux = result[:mux]

    # Four delivered variants, each needing an independently mutable picking
    # line copy: 4 x 40. The archiver keeps only its first page and that page is
    # one retained read: 1 x 40. Equal-value adjustment lists are free
    # references, and the second order does not create a retained page.
    assert_equal 200, mux.minimum_copy_bytes

    retention = mux.budget[:decisions].find do |decision|
      decision[:group] == :archive
    end
    refute_nil retention
    assert_equal 40, retention[:bytes]
    assert_match(/retained reference/, retention[:determiner])

    picking = result[:picking].events
    dispatch = picking.find { |event| event[:order_id] == "A-1" && event[:phase] == :yielded }
    assert_equal [10, 20], dispatch[:lines].map { |line| line[:amount] }

    returned = picking.find { |event| event[:order_id] == "A-1" && event[:phase] == :returned }
    assert_equal [110, 120], returned[:lines].map { |line| line[:amount] }

    closed = result[:archive].events.last
    assert_equal "A-1", closed[:copied_at_close]
    assert_equal [10, 20], closed[:lines].map { |line| line[:amount] }
  end

  def test_retain_first_only_discounts_its_own_path
    archive = rec("archive")

    work_order = Kiba::Fanout::WorkOrder.build do
      order_key "order_id"
      manifest do
        node "lines", bytes: 10, whole: true
        node "adjustments", bytes: 5, whole: true
      end
      group :archive do
        destination archive.destination_class
        retain_first "lines"
        on_close reads: ["lines.*.amount", "adjustments.*.amount"]
      end
    end

    rows = [
      {order_id: "A", lines: [{amount: 1}], adjustments: [{amount: 7}]},
      {order_id: "B", lines: [{amount: 2}], adjustments: [{amount: 8}]}
    ]

    mux = run_job(rows, work_order)

    # lines: first page only (10). adjustments: both deliveries (5 + 5).
    assert_equal 20, mux.minimum_copy_bytes
    by_node = mux.budget[:decisions].group_by { |decision| decision[:node] }
    assert_equal 1, by_node.fetch("lines").size
    assert_equal 2, by_node.fetch("adjustments").size
  end

  def test_legacy_jobs_do_not_require_fanout
    rows = []
    Kiba.run do
      source TestEnumerableSource, [{a: 1}]
      destination Class.new {
        define_method(:initialize) { |target| @target = target }
        define_method(:write) { |row| @target << row }
      }, rows
    end
    assert_equal [{a: 1}], rows
  end
end
