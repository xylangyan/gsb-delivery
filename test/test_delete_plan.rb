# frozen_string_literal: true

require 'minitest/autorun'
require 'jsonpath'

class TestDeletePlan < Minitest::Test
  def test_keeps_single_delete_calls_when_empty_object_cleanup_changes_the_result
    document = { 'a' => [{}, 'A', 'B', 'C'] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[0]'])

    assert_equal ['$.a[2]', '$.a[0]'], plan.calls
    assert_equal [2], plan.handoffs
    assert_equal({ 2 => { 'a' => ['C'] } }, plan.snapshots)
    assert_equal plan.snapshots, plan.expected_snapshots
    assert plan.safe?
    assert_equal 1, plan.rejections.size
  end

  def test_merges_when_first_slot_is_null_and_cleanup_aligns
    document = { 'a' => [nil, 'A', 'B', 'C'] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[0]'])

    assert_equal ['$.a[2,0]'], plan.calls
    assert_equal({ 2 => { 'a' => ['C'] } }, plan.snapshots)
  end

  def test_merges_plain_array_indices_in_either_order
    document = { 'a' => %w[A B C D] }

    assert_equal ['$.a[1,0]'], JsonPath::DeletePlan.build(document, ['$.a[1]', '$.a[0]']).calls
    assert_equal ['$.a[2,0]'], JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[0]']).calls
    assert_equal ['$.a[0,2]'], JsonPath::DeletePlan.build(document, ['$.a[0]', '$.a[2]']).calls
  end

  def test_merges_adjacent_range_and_index_selectors
    document = { 'a' => %w[A B C D E] }

    assert_equal ['$.a[1:3,0]'], JsonPath::DeletePlan.build(document, ['$.a[1:3]', '$.a[0]']).calls
    assert_equal ['$.a[0,1:3]'], JsonPath::DeletePlan.build(document, ['$.a[0]', '$.a[1:3]']).calls
  end

  def test_does_not_merge_across_a_handoff
    document = { 'a' => %w[A B C D] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[1]', '$.a[0]'], :handoffs => [1, 2])

    assert_equal ['$.a[1]', '$.a[0]'], plan.calls
    assert_equal({ 1 => { 'a' => %w[A C D] }, 2 => { 'a' => %w[C D] } }, plan.snapshots)
  end

  def test_preserves_original_slots_of_equal_values
    document = { 'a' => ['A', 'B', 'A', 'B'] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[1]', '$.a[2]'])

    assert_equal ['$.a[1,2]'], plan.calls
    assert_equal %w[A A], plan.snapshots[2]['a']
  end

  def test_keeps_negative_indices_and_duplicates_native
    document = { 'a' => %w[A B C D E] }

    assert_equal ['$.a[-1,0]'], JsonPath::DeletePlan.build(document, ['$.a[-1]', '$.a[0]']).calls
    assert_equal ['$.a[2,2]'], JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[2]']).calls
  end

  def test_cleanup_in_other_arrays_stays_native
    document = {
      'a' => [{}, 'A', 'B', 'C'],
      'b' => [{}, 'X']
    }
    plan = JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[0]'])

    assert_equal ['$.a[2]', '$.a[0]'], plan.calls
    assert_equal({ 'a' => ['C'], 'b' => ['X'] }, plan.snapshots[2])
  end

  def test_non_adjacent_or_non_selection_operations_stay_separate
    document = { 'a' => %w[A B C D], 'b' => %w[X Y] }
    operations = ['$.a[1]', '$..a[*]', '$.a[0]', '$.b[0]']
    plan = JsonPath::DeletePlan.build(document, operations)

    expected = operations.dup
    expected[1] = '$..a[*]'
    assert_equal expected, plan.calls
    refute plan.calls.any? { |call| call.include?(',') }
  end

  def test_replay_carries_a_legal_original_document_and_first_difference
    document = { 'a' => [{}, 'A', 'B', 'C'] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[2]', '$.a[0]'])
    refute_empty plan.rejections
    replay = plan.rejections.first.replay

    assert_equal document, replay[:document]
    assert_equal ['$.a[2]', '$.a[0]'], replay[:operations]
    assert_equal 0, replay[:segment_start]
    assert_equal ['$.a[2]', '$.a[0]'], replay[:segment_operations]
    assert_equal ['$.a[2,0]'], replay[:attempted_calls]
    assert_equal '$.a[2,0]', replay[:merged_path]
    assert_equal({ 'a' => ['C'] }, replay[:expected_json])
    assert_equal({ 'a' => ['A', 'C'] }, replay[:actual_json])
    refute_equal replay[:expected_slots], replay[:actual_slots]
  end

  def test_long_manifest_is_planned_without_materializing_every_shape
    long = Array.new(5000) { |index| index.even? ? {} : "v#{index}" }
    document = { 'a' => long }
    operations = ['$.a[4998]', '$.a[4996]', '$.a[4994]', '$.a[4992]']

    plan = JsonPath::DeletePlan.build(document, operations, :max_candidates => 32)

    assert_operator plan.call_count, :<=, 4
    assert_equal plan.expected_snapshots, plan.snapshots
  end

  def test_validation_rejects_unknown_handoff
    document = { 'a' => %w[A B] }
    assert_raises(ArgumentError) do
      JsonPath::DeletePlan.build(document, ['$.a[0]', '$.a[1]'], :handoffs => [3])
    end
  end

  def test_plan_runs_through_the_real_delete_entry_point
    document = { 'items' => %w[A B C D E] }
    plan = JsonPath::DeletePlan.build(document, ['$.items[1:3]', '$.items[0]'])

    copy = Marshal.load(Marshal.dump(document))
    plan.calls.each { |path| copy = JsonPath.for(copy).delete(path).obj }

    assert_equal ['$.items[1:3,0]'], plan.calls
    assert_equal({ 'items' => %w[D E] }, copy)
    assert_equal copy, plan.snapshots[2]
  end

  def test_handoff_states_match_sequential_execution
    document = {
      'a' => [{}, 'A', 'B', 'C'],
      'b' => [{}, 'X']
    }
    operations = ['$.a[2]', '$.b[0]', '$.a[0]']
    plan = JsonPath::DeletePlan.build(document, operations, :handoffs => [1, 2, 3])

    expected = Marshal.load(Marshal.dump(document))
    operations.each_with_index do |path, index|
      expected = JsonPath.for(expected).delete(path).obj
      assert_equal expected, plan.snapshots[index + 1]
    end
  end

  def test_planned_calls_are_native_delete_expressions
    document = { 'items' => %w[A B C D E] }
    plan = JsonPath::DeletePlan.build(document,
                                      ['$.items[1:3]', '$.items[0]', '$.items[2]'],
                                      :handoffs => [2, 3])

    assert plan.calls.all? { |path| JsonPath.new(path).path.last.start_with?('[') }
    assert_equal ['$.items[1:3,0]', '$.items[2]'], plan.calls
  end

  def test_original_slots_are_exposed_at_handoffs
    document = { 'a' => ['A', 'B', 'A', 'B'] }
    plan = JsonPath::DeletePlan.build(document, ['$.a[1]', '$.a[2]'])

    assert_equal ['$.a[1,2]'], plan.calls
    slots = plan.handoff_slots[2]['entries']['a']['entries']
    assert_equal ['$.a[0]', '$.a[2]'], slots.map { |entry| entry['origin'] }
    assert_equal plan.handoff_slots, plan.expected_handoff_slots
  end

  def test_rejection_witness_replays_from_original_manifest
    document = { 'a' => [{}, 'A', 'B', 'C'] }
    operations = ['$.a[2]', '$.a[0]']
    plan = JsonPath::DeletePlan.build(document, operations)

    witness = plan.rejections.first.replay
    prefix = witness[:document]
    witness[:operations][0...witness[:segment_start]].each do |path|
      prefix = JsonPath.for(prefix).delete(path).obj
    end
    assert_equal witness[:segment_document], prefix

    actual = witness[:segment_document]
    witness[:attempted_calls].each do |path|
      actual = JsonPath.for(actual).delete(path).obj
    end
    assert_equal witness[:actual_json], actual
    refute_equal witness[:expected_json], actual
  end
end
