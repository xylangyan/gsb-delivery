# encoding: utf-8
require 'test_helper'
require 'geocoder/replay'

##
# Unit tests for the local replay entrypoint. All responses are in memory.
#
class ReplayTest < Test::Unit::TestCase
  FIXTURE = File.expand_path('../fixtures/replay/three_slots.json', __dir__)

  def scenario
    Geocoder::Replay::Scenario.load(FIXTURE)
  end

  def test_key_is_computed_by_the_current_library
    lookup = Geocoder::Replay::Lookup.new(host: 'nominatim.local', use_https: false, language: 'en')
    query = lookup.query_for('Main Street')
    assert_equal 'https://nominatim.local/search?accept-language=en&addressdetails=1&format=json&q=Main+Street',
                 lookup.cache_key_for(query)
  end

  def test_replay_separates_coordinates_from_cache_hit_marker
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    assert analysis.declared_ok?, analysis.engine.violations.inspect

    first, second = analysis.engine.results
    assert_equal 'old-two', first[:body]
    assert_equal 'old-two', second[:body]
    assert_equal [10.0, 20.0], first[:coordinates]
    assert_equal [11.0, 21.0], second[:coordinates]
    assert_equal true, first[:marker]
    assert_equal false, second[:marker]
  end

  def test_unparseable_success_body_can_be_cached_and_still_yield_no_addresses
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    fetch = analysis.engine.fetches.find { |item| item[:step] == 's2:3' }
    refute_nil fetch[:parse_error]
    assert_equal '<html>', fetch[:raw][0, 6]
    assert_equal false, fetch[:marker]
    assert_equal [], fetch[:candidates]

    writes = analysis.engine.cache_trace.select { |event| event[:op] == :write }
    assert_equal ['unparseable'], writes.map { |event| event[:body] }

    s3 = analysis.engine.fetches.find { |item| item[:step] == 's3:1' }
    assert_equal true, s3[:marker]
    assert_equal [], s3[:candidates]
    assert_equal 'unparseable', s3[:body].id
  end

  def test_focused_candidate_attribution
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    focus = analysis.alternative_sources
    assert_equal 's1:3', focus[:step]
    assert_equal 'old-two', focus[:reported_body]
    assert_equal false, focus[:reported_marker]
    assert_equal ['old-two'], focus[:same_index_bodies]
    assert focus[:unique]
  end

  def test_declared_order_must_cover_every_operation_once
    data = scenario.to_data
    data['order'] = ['s1:1']
    s = Geocoder::Replay::Scenario.new(data)
    assert_raise(Geocoder::Replay::InputError) do
      Geocoder::Replay::Engine.new(s).run
    end
  end

  def test_cache_delete_uses_original_physical_key
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    deletes = analysis.engine.cache_trace.select { |event| event[:op] == :delete }
    assert_equal 1, deletes.size
    assert deletes.first[:key].start_with?('geocoder:')
    assert_equal 'geocoder:https://nominatim.local/search?accept-language=en&addressdetails=1&format=json&q=Main+Street',
                 deletes.first[:key]
  end

  def test_cache_hit_without_any_fetch_is_not_allowed
    runtime = Geocoder::Replay::Runtime.new(
      { id: 'p', label: 'p', prefix: 'geocoder:', config: {}, initial_cache: {} },
      Geocoder::Replay::BodyRegistry.new
    )
    error = assert_raise(Geocoder::Replay::Violation) do
      runtime.take_response('Main Street', 'k')
    end
    assert_equal :no_recorded_response, error.kind
  end

  def test_independent_components_are_solved_separately
    data = scenario.to_data
    data['processes'] << {
      'id' => 'p2',
      'prefix' => 'geocoder:',
      'config' => { 'host' => 'nominatim.local', 'use_https' => false, 'language' => 'en' },
      'initial_cache' => {}
    }
    data['slots'] << {
      'id' => 's4',
      'process' => 'p2',
      'operations' => [
        { 'op' => 'search', 'query' => 'Main Street' }
      ]
    }
    data['bodies']['other'] = { 'body' => '[]' }
    data['order'] = data['order'] + ['s4:1']
    s = Geocoder::Replay::Scenario.new(data)
    composite = Geocoder::Replay::CompositeAnalysis.new(s).run
    assert composite.independent?
    assert_equal 2, composite.components.size
  end

  def test_wrapping_can_change_only_the_marker
    # The same old body is wrapped twice; a miss in between changes only the
    # marker of the later wrapper, not its coordinates.
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    rows = analysis.engine.results.select { |row| row[:body] == 'old-two' }
    assert_equal 2, rows.size
    assert_equal rows.map { |row| row[:coordinates] }.uniq.size, 2
    assert_equal [true, false], rows.map { |row| row[:marker] }
  end

  def test_ambiguous_coordinates_report_alternative_bodies
    path = File.expand_path('../fixtures/replay/ambiguous_coordinates.json', __dir__)
    analysis = Geocoder::Replay::Analysis.new(Geocoder::Replay::Scenario.load(path)).run
    focus = analysis.alternative_sources
    refute focus[:unique]
    assert_equal %w[old-two same-coords], focus[:same_index_bodies].sort
    assert_equal ['same-coords'], focus[:other_matches]
  end

  def test_unsatisfiable_scenario_names_first_unexplained_step
    path = File.expand_path('../fixtures/replay/inconsistent_marker.json', __dir__)
    analysis = Geocoder::Replay::Analysis.new(Geocoder::Replay::Scenario.load(path)).run
    refute analysis.consistent?
    failure = analysis.failure
    assert_equal 's1:1', failure[:step]
    assert_equal :marker_mismatch, failure[:kind]
    unreachable = analysis.unreachable
    refute_nil unreachable
    assert_equal 's1:2', unreachable[:step]
    assert_equal %w[s2:1 s2:2 s1:1], unreachable[:prefix]
  end

  def test_two_batches_are_solved_separately
    path = File.expand_path('../fixtures/replay/two_batches.json', __dir__)
    composite = Geocoder::Replay::CompositeAnalysis.new(
      Geocoder::Replay::Scenario.load(path)
    ).run
    assert composite.independent?
    assert_equal 2, composite.components.size
    refute composite.consistent?
    failures = composite.failures
    assert_equal 1, failures.size
    assert_equal 'batch-b', failures.first[:component]
    assert_equal 't1:1', failures.first[:step]
    assert_equal 'batch-a', (composite.components.map(&:id) - %w[batch-b]).first
  end

  def test_reproduction_is_minimal_and_runs
    scenario = Geocoder::Replay::Scenario.load(FIXTURE)
    data = Geocoder::Replay::CLI.reproduction(scenario)
    assert_equal ['s1:1', 's1:2', 's2:1', 's2:2', 's2:3', 's1:3'], data['steps']
    replay = Geocoder::Replay::CLI.reproduction_replay(scenario, nil)
    assert replay['consistent']
    rows = replay['provenance']
    assert_equal [%w[s1:2 true], %w[s1:3 false]], rows.map { |row| [row[:step], row[:cache_hit].to_s] }
    assert_equal 6, data['scenario']['slots'].sum { |slot| slot['operations'].size }
  end

  def test_missing_order_is_synthesized
    path = File.expand_path('../fixtures/replay/three_slots_no_order.json', __dir__)
    analysis = Geocoder::Replay::Analysis.new(Geocoder::Replay::Scenario.load(path)).run
    refute analysis.declared?
    assert analysis.synthesized?
    assert analysis.engine.ok?
    rows = analysis.engine.results
    assert_equal %w[s1:2 s1:3], rows.map { |row| row[:step] }
    assert_equal [true, false], rows.map { |row| row[:marker] }
  end

  def test_flat_event_log_is_adapted_to_the_same_replay
    path = File.expand_path('../fixtures/replay/three_slots_events.json', __dir__)
    scenario = Geocoder::Replay::Scenario.load(path)
    analysis = Geocoder::Replay::Analysis.new(scenario).run
    assert analysis.declared_ok?, analysis.engine.violations.inspect
    rows = analysis.engine.results
    assert_equal [%w[s1:2 true], %w[s1:3 false]], rows.map { |row| [row[:step], row[:marker].to_s] }
    assert_equal %w[old-two old-two], rows.map { |row| row[:body] }
  end

  def test_flat_event_log_rejects_a_missing_target
    path = File.expand_path('../fixtures/replay/three_slots_events.json', __dir__)
    data = JSON.parse(File.read(path))
    data['events'][3].delete('query')
    assert_raise(Geocoder::Replay::InputError) do
      Geocoder::Replay::Scenario.new(data)
    end
  end

  def test_cli_rejects_an_order_that_omits_operations
    out = StringIO.new
    err = StringIO.new
    status = Geocoder::Replay::CLI.run([FIXTURE, '--order', 's1:1'], out, err)
    assert_equal 2, status
    assert_match(/order must list every operation exactly once/, err.string)
  end

  def test_cli_runs_without_network
    out = StringIO.new
    err = StringIO.new
    status = Geocoder::Replay::CLI.run([FIXTURE], out, err)
    assert_equal 0, status, err.string
    assert_match(/declared order is consistent/, out.string)
  end
end
