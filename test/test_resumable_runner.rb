require_relative "helper"
require_relative "support/test_enumerable_source"
require_relative "support/test_array_destination"
require_relative "support/test_resumable_components"

# End-to-end behaviour of the resumable runner: ordering, per-destination
# acknowledgement, close layers, restarts and the status view.
class TestResumableRunner < Kiba::Test
  def setup
    @checkpoint = {}
    @detail = TestLedgerDestination.new
    @audit = TestLedgerDestination.new
  end

  def store
    Kiba::Checkpoint::HashStore.new(@checkpoint)
  end

  def run_job(job)
    Kiba::ResumableRunner.run(job, checkpoint: store)
  end

  def status(job)
    Kiba::ResumableRunner.status(job, checkpoint: store)
  end

  def detail_ids(destination = @detail)
    destination.rows.map { |row| row[:id] }
  end

  def audit_ids(destination = @audit)
    destination.rows.map { |row| row[:id] }
  end

  # ---- ordering ---------------------------------------------------------

  def test_yield_twice_then_return_keeps_current_order
    source_rows = [{id: "row"}]
    plain_rows = []
    job = Kiba.parse do
      source TestEnumerableSource, source_rows
      transform TestYieldTwiceThenReturn
      destination TestArrayDestination, plain_rows
    end
    Kiba.run(job)

    resumed_rows = []
    resumed = Kiba.parse do
      source TestEnumerableSource, source_rows
      transform TestYieldTwiceThenReturn
      destination TestArrayDestination, resumed_rows
    end
    Kiba::ResumableRunner.run(resumed, checkpoint: Kiba::Checkpoint::MemoryStore.new)

    expected = [
      {id: "row-a"},
      {id: "row-b"},
      {id: "row-returned"}
    ]
    assert_equal expected, plain_rows
    assert_equal expected, resumed_rows
  end

  def test_close_row_flows_through_later_transforms_and_both_layers_are_kept
    detail = @detail
    job = Kiba.parse do
      transform TestStaticCloseEmitter, [{id: "tail", amount: 7}]
      transform TestBufferingTransform
      destination TestDestinationRef, detail
    end
    Kiba::ResumableRunner.run(job, checkpoint: Kiba::Checkpoint::MemoryStore.new)

    assert_equal [
      {id: "tail", amount: 7},
      {id: "close-tail"}
    ], @detail.rows.map { |row| row.reject { |k, _| k == :write_id } }
  end

  def test_close_layer_may_emit_many_rows_without_buffering_the_batch
    rows = []
    job = Kiba.parse do
      transform TestManyCloseRows, 200
      destination TestArrayDestination, rows
    end
    Kiba::ResumableRunner.run(job, checkpoint: Kiba::Checkpoint::MemoryStore.new)
    assert_equal 200, rows.size
  end

  def test_upstream_close_result_and_downstream_close_result_of_same_value_are_both_kept
    detail = @detail
    job = Kiba.parse do
      transform TestPassThroughCloseEmitter, [{id: "same", amount: 5}]
      transform TestStaticCloseEmitter, [{id: "same", amount: 5}]
      destination TestDestinationRef, detail
    end
    Kiba::ResumableRunner.run(job, checkpoint: Kiba::Checkpoint::MemoryStore.new)

    assert_equal [
      {id: "same", amount: 5},
      {id: "same", amount: 5}
    ], @detail.rows.map { |row| row.reject { |k, _| k == :write_id } }
    assert_equal 2, @detail.write_ids.uniq.size
  end

  # ---- per-destination acknowledgement ---------------------------------

  def test_resume_after_detail_ack_before_audit_does_not_rewrite_detail
    detail = @detail
    audit = TestFlakyDestination.new
    audit.fail_on_write = lambda { |row, _| row[:id] == "first" }

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "first"}, {id: "second"}], nil]}
      destination TestDestinationRef, detail
      destination TestDestinationRef, audit
    end

    assert_raises(RuntimeError) { run_job(job) }
    assert_equal %w[first], detail_ids
    assert_equal [], audit_ids(audit)

    audit.fail_on_write = nil
    run_job(job)

    assert_equal %w[first second], detail_ids
    assert_equal %w[first second], audit_ids(audit)
    assert_equal 0, detail.duplicates.size
  end

  def test_pending_status_reports_layer_output_and_destination
    detail = @detail
    gate = TestStoppingDestination.new
    gate.stop_after = 1
    audit = TestLedgerDestination.new(gate.rows, gate.write_ids)

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "first"}, {id: "second"}], nil]}
      transform TestYieldTwiceThenReturn
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    state = status(job)
    assert state.pending?
    assert_equal "s0:p0", state.segment_id
    assert_equal 0, state.pending_layer
    assert_equal 1, state.pending_output_index
    assert_equal [1], state.pending_destinations
    assert_match(/s0:p0:o1/, state.pending_write_id)
    assert_equal :page, state.segment_kind
    assert_equal 1, state.done_count
  end

  def test_in_doubt_write_is_probed_and_not_treated_as_done
    detail = @detail
    audit = TestFlakyDestination.new
    audit.fail_on_write = lambda { |row, _| row[:id] == "first" }

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "first"}, {id: "second"}], nil]}
      destination TestDestinationRef, detail
      destination TestDestinationRef, audit
    end

    assert_raises(RuntimeError) { run_job(job) }
    state = status(job)
    assert state.pending?
    assert_equal [1], state.pending_destinations
    write_id = state.pending_write_id

    audit.confirm!(write_id)
    audit.fail_on_write = nil
    run_job(job)

    assert_equal %w[second], audit_ids(audit)
    assert_equal 0, audit.duplicates.size
    assert_equal %w[first second], detail_ids
  end

  # ---- close + restart -------------------------------------------------

  def test_restart_mid_close_keeps_order_and_does_not_repeat_ack
    detail = @detail
    gate = TestStoppingDestination.new
    gate.stop_after = 2

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "row-1"}], nil]}
      transform TestStaticCloseEmitter, [{id: "close-1"}, {id: "close-2"}, {id: "close-3"}]
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    assert_equal %w[row-1 close-1 close-2], detail_ids

    state = status(job)
    assert_equal :close, state.segment_kind
    assert_equal 0, state.pending_layer
    assert_equal 1, state.pending_output_index

    gate.stop_after = nil
    run_job(job)

    assert_equal %w[row-1 close-1 close-2 close-3], detail_ids
    assert_equal 0, detail.duplicates.size
    assert_equal detail_ids, gate.rows.map { |r| r[:id] }
  end

  def test_close_output_that_is_split_downstream_is_committed_per_part
    detail = @detail
    gate = TestStoppingDestination.new
    gate.stop_after = 2

    job = Kiba.parse do
      transform TestStaticCloseEmitter, [{id: "tail"}]
      transform TestSplitTransform, 3
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    state = status(job)
    assert_equal :close, state.segment_kind
    assert_equal 1, state.pending_layer
    assert_equal 2, state.pending_output_index
    assert_equal %w[tail-1 tail-2 tail-3], detail_ids

    gate.stop_after = nil
    run_job(job)

    assert_equal %w[tail-1 tail-2 tail-3], detail_ids
    assert_equal 0, detail.duplicates.size
    assert_equal detail_ids, gate.rows.map { |r| r[:id] }
  end

  # ---- source cursors --------------------------------------------------

  def test_confirmed_pages_are_not_read_again_after_restart
    source = TestExpiringPagedSource.new(
      {
        nil => [[{id: "page-1"}], :cursor_2],
        :cursor_2 => [[{id: "page-2"}], :cursor_3],
        :cursor_3 => [[{id: "page-3"}], nil]
      }
    )
    detail = @detail
    gate = TestStoppingDestination.new
    # Stop on the audit destination while processing page 2, after page 1 was
    # fully acknowledged.
    gate.stop_after = 1

    job = Kiba.parse do
      source TestSourceRef, source
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    assert_equal %w[page-1 page-2], detail_ids

    # Page 1 has been acknowledged end to end; that cursor is now gone.
    source.discarded << nil

    gate.stop_after = nil
    run_job(job)

    assert_equal %w[page-1 page-2 page-3], detail_ids
    assert_equal 1, source.read_counts[nil]
  end

  def test_cursor_advanced_only_after_a_page_is_fully_acknowledged
    detail = @detail
    gate = TestStoppingDestination.new
    gate.stop_after = 1

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "a"}, {id: "b"}], :next], :next => [[], nil]}
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    # The first page (a, b) was acknowledged, but the checkpoint still holds
    # the page it was working on, so the cursor has not advanced yet.
    assert_nil status(job).source_cursor(0)

    gate.stop_after = nil
    run_job(job)
    assert_equal %w[a b], detail_ids
    assert_equal 0, detail.duplicates.size
    assert_nil status(job).source_cursor(0)
  end

  def test_multiple_sources_resume_independently
    detail = @detail
    first = TestPagedSource.new({nil => [[{id: "a1"}], :first_next], :first_next => [[{id: "a2"}], nil]})
    second = TestPagedSource.new({nil => [[{id: "b1"}], :second_next], :second_next => [[{id: "b2"}], nil]})
    gate = TestStoppingDestination.new
    gate.stop_after = 2

    job = Kiba.parse do
      source TestSourceRef, first
      source TestSourceRef, second
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    state = status(job)
    assert_equal 1, state.pending_source_index
    assert_equal %w[a1 a2 b1], detail_ids

    gate.stop_after = nil
    run_job(job)

    assert_equal %w[a1 a2 b1 b2], detail_ids
    assert_equal 0, detail.duplicates.size
  end

  def test_a_stalled_cursor_is_reported_instead_of_looping
    detail = @detail

    # A source returning the same cursor forever must not loop silently.
    table = {nil => [[{id: "a"}], :same], :same => [[{id: "b"}], :same]}
    stalled = TestPagedSource.new(table)
    job = Kiba.parse do
      source TestSourceRef, stalled
      destination TestDestinationRef, detail
    end
    error = assert_raises(Kiba::Checkpoint::Error) { run_job(job) }
    assert_match(/same cursor/, error.message)
  end

  def test_a_close_that_stops_after_emitting_part_of_its_rows_resumes_without_repeating
    detail = @detail
    gate = TestStoppingDestination.new
    # The source row is written to both, then detail takes the close row while
    # the second destination stops before taking it.
    gate.stop_after = 1

    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "source-row"}], nil]}
      transform TestBufferingTransform
      destination TestDestinationRef, detail
      destination TestDestinationRef, gate
    end

    assert_raises(RuntimeError) { run_job(job) }
    state = status(job)
    assert_equal :close, state.segment_kind
    assert_equal 0, state.pending_layer
    assert_equal 1, state.pending_output_index
    assert_equal [1], state.pending_destinations
    assert_equal %w[source-row close-tail], detail_ids
    assert_equal %w[source-row], gate.rows.map { |r| r[:id] }

    gate.stop_after = nil
    run_job(job)

    assert_equal %w[source-row close-tail], detail_ids
    assert_equal 0, detail.duplicates.size
    assert_equal detail_ids, gate.rows.map { |r| r[:id] }
    assert status(job).completed?
  end

  def test_status_does_not_execute_the_job
    instantiations = 0
    source_class = Class.new do
      define_method(:initialize) { instantiations += 1 }
      def each; end
    end
    job = Kiba.parse do
      source source_class
    end

    state = Kiba::ResumableRunner.status(job, checkpoint: store)
    assert_equal :new, state.phase
    assert_equal 0, instantiations
  end

  def test_completed_checkpoint_is_a_noop_on_next_run
    detail = @detail
    job = Kiba.parse do
      source TestPagedSource, {nil => [[{id: "a"}], nil]}
      destination TestDestinationRef, detail
    end

    run_job(job)
    state = status(job)
    assert state.completed?

    run_job(job)
    assert_equal %w[a], detail_ids
    assert_equal 0, detail.duplicates.size
  end

  def test_jobs_without_resumable_declarations_keep_using_streaming_runner
    job = Kiba.parse do
      source TestEnumerableSource, [1]
    end
    assert_equal Kiba::StreamingRunner, Kiba.runner_for(job)
  end

  def test_kiba_run_switches_to_resumable_when_a_checkpoint_is_given
    rows = []
    job = Kiba.parse do
      source TestEnumerableSource, [{id: "a"}]
      destination TestArrayDestination, rows
    end
    Kiba.run(job, checkpoint: Kiba::Checkpoint::MemoryStore.new)
    assert_equal [{id: "a"}], rows
  end
end
