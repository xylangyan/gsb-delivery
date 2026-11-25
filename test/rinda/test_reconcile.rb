# frozen_string_literal: false
require 'test/unit'
require 'json'
require 'rinda/reconcile'

class TestReconcile < Test::Unit::TestCase
  FIXTURES = File.expand_path('../fixtures/reconcile', __dir__)

  def ledger(name)
    Rinda::Reconcile::Ledger.load(File.join(FIXTURES, "#{name}.json"))
  end

  def replay(name)
    Rinda::Reconcile::Engine.new(ledger(name)).replay
  end

  def test_move_port_error_is_a_confirmed_duplicate
    result = replay('move_port_error')
    assert_true(result.replayable?)
    assert_equal(['REG-1+REG-2'], result.duplicate_labels)
    assert_equal([], result.distinct_labels)
    assert_equal([], result.ambiguous_labels)

    group = result.attributions.map { |a| [a.receipt.id, a.ids] }.to_h
    assert_equal(['w1'], group['m1'])
    assert_equal(['w1'], group['t1'])
  end

  def test_same_content_two_receipts_two_writes_is_distinct
    result = replay('distinct_same_content')
    assert_true(result.replayable?)
    assert_equal(['REG-1+REG-2'], result.distinct_labels)
    assert_equal([], result.duplicate_labels)
  end

  def test_identical_content_with_retained_entry_is_ambiguous
    result = replay('ambiguous_same_content')
    assert_true(result.replayable?)
    assert_equal(['REG-1+REG-2'], result.ambiguous_labels)
    assert_equal(2, result.scenarios.size)

    duplicate, distinct = result.scenarios
    assert_equal('single-original-hand-off-twice', duplicate['scenario'])
    assert_equal(1, duplicate['assignments'].values.uniq.size)

    assert_equal('two-original-writes', distinct['scenario'])
    assert_equal(2, distinct['assignments'].values.uniq.size)

    # Each story is a complete order over every call.
    all_ids = ledger('ambiguous_same_content').events.map(&:id).sort
    result.scenarios.each do |scenario|
      assert_equal(all_ids, scenario['order'].sort)
    end
  end

  def test_scenarios_do_not_mix_local_verdicts
    result = replay('ambiguous_same_content')
    result.scenarios.each do |scenario|
      # Each scenario assigns every receipt exactly once.
      assert_equal(%w[m1 t1], scenario['assignments'].keys.sort)
      # ... and never assigns one receipt to two different sources.
      sources = scenario['assignments'].values
      if scenario['scenario'] == 'single-original-hand-off-twice'
        assert_equal(1, sources.uniq.size)
      else
        assert_equal(2, sources.uniq.size)
      end
    end
  end

  def test_expired_write_can_be_seen_by_a_waiting_read
    result = replay('expired_write')
    assert_true(result.replayable?)
    read = result.read('rd1')
    assert_not_nil(read)
    assert_true(read.expired_only?)
    assert_equal(['wdead'], read.sources.map(&:id))
  end

  def test_expired_write_is_not_takeable
    result = replay('expired_ghost_not_takeable')
    assert_false(result.replayable?)
    assert_equal(1, result.conflict.evidence.size)
    evidence = result.conflict.evidence.first
    assert_equal('t1', evidence[:receipt])
    assert_match(/takeable/, evidence[:reason])
  end

  def test_read_sees_only_the_write_it_reacquired_after
    result = replay('read_two_writes')
    assert_true(result.replayable?)
    read = result.read('rd1')
    assert_equal(['job', 'B'], read.value)
    assert_equal(['wA', 'wB'], read.sources.map(&:id))
    # The ledger pins the read to wB; the read is forced to observe wB and is
    # therefore ordered after it.
    assert_true(result.order.before?('wB', 'rd1'))
    assert_equal([], result.scenarios)
  end

  def test_unpinned_read_wake_order_gives_two_valid_orders
    result = replay('read_order_ambiguous')
    assert_true(result.replayable?)
    assert_equal(2, result.scenarios.size)

    all_ids = ledger('read_order_ambiguous').events.map(&:id).sort
    assignments = result.scenarios.map { |s| s['assignments']['rd1'] }.sort
    assert_equal(%w[wA wB], assignments)

    result.scenarios.each do |scenario|
      assert_equal(all_ids, scenario['order'].sort)
      assert_true(result.order.respects?(scenario['order']))

      # A read that reacquired before the second write must be ordered
      # between the two; one that did not must be ordered after both.
      pos = scenario['order'].each_with_index.to_h
      if scenario['assignments']['rd1'] == 'wA'
        assert_true(pos['wA'] < pos['rd1'])
        assert_true(pos['rd1'] < pos['wB'])
      else
        assert_true(pos['wB'] < pos['rd1'])
      end
    end
  end

  def test_summary_reports_first_contradiction_with_its_line
    summary = Rinda::Reconcile::Engine.new(ledger('summary_conflict')).replay_summary
    assert_false(summary.ok?)
    assert_equal(3, summary.failed_line)
    evidence = summary.failed_result.conflict.evidence.first
    assert_equal('t2', evidence[:receipt])
    assert_equal(%w[w2 w1], evidence[:statements])
  end

  def test_summary_applies_lines_in_order
    summary = Rinda::Reconcile::Engine.new(ledger('summary_conflict')).replay_summary
    assert_equal([1, 2, 3], summary.steps.map { |s| s[:line] })
    assert_true(summary.steps[0][:result].replayable?)
    assert_true(summary.steps[1][:result].replayable?)
    assert_false(summary.steps[2][:result].replayable?)
  end

  def test_long_log_waiting_groups_do_not_cross_product
    path = File.join(FIXTURES, 'long_groups.json')
    result = Rinda::Reconcile::Engine.new(Rinda::Reconcile::Ledger.load(path)).replay
    assert_true(result.replayable?)
    # Two independent groups; each verdict is reached from its own events.
    assert_equal(['G1'], result.duplicate_labels)
    assert_equal(['G2'], result.distinct_labels)
    # Scenarios stay inside a group: no event from one group appears in the
    # other group's scenario.
    result.scenarios.each do |scenario|
      assert_true((scenario['order'] & %w[g2a g2b g2c g2d]).empty?)
    end
  end

  def test_two_normal_takes_of_one_write_conflict
    result = replay('double_take_one_write')
    assert_false(result.replayable?)
    assert_match(/no consistent source assignment/, result.conflict.message)
  end

  def test_each_endpoint_seeing_one_is_not_evidence_of_the_same_write
    # Two endpoints, one take each, identical values, one write. Seeing the
    # value once on each side does not prove one hand-off or two; with only
    # one possible write the archive has no consistent story.
    result = replay('two_ends_one_write')
    assert_false(result.replayable?)
    candidates = result.attributions.map { |a| [a.receipt.id, a.ids] }.to_h
    assert_equal({ 't1' => ['w1'], 't2' => ['w1'] }, candidates)
    assert_match(/no consistent source assignment/, result.conflict.message)
  end

  def test_summary_is_cumulative_and_stops_at_first_contradiction
    result = Rinda::Reconcile::Engine.new(ledger('summary_duplicate_then_distinct')).replay_summary
    assert_false(result.ok?)
    assert_equal(3, result.failed_line)
    evidence = result.failed_result.conflict.evidence.first
    assert_equal('t2', evidence[:receipt])
    assert_equal(%w[w1 w2], evidence[:statements])
  end

  def test_errored_move_known_not_delivered_is_not_a_duplicate
    result = replay('move_not_delivered')
    assert_false(result.replayable?)
    assert_equal([], result.duplicate_labels)
  end

  def test_lease_expiry_uses_log_clock
    result = replay('lease_expiry')
    assert_true(result.replayable?)
    # w1 (lease 5, written at 10) is long dead at the take; only w2 can supply
    # the value even though both carry the same tuple.
    receipt = result.attributions.find { |a| a.receipt.id == 't1' }
    assert_equal(['w2'], receipt.ids)
  end

  def test_ledger_rejects_incomplete_completed_take
    raw = JSON.parse(File.read(File.join(FIXTURES, 'smoke.json')))
    raw['calls'] << { 'id' => 'bad', 'endpoint' => 'dst', 'method' => 'take',
                      'template' => ['job', 'J'], 'start' => 40 }
    error = assert_raise(Rinda::Reconcile::LedgerError) do
      Rinda::Reconcile::Ledger.new(raw)
    end
    assert_match(/receipt/, error.message)
  end
end
