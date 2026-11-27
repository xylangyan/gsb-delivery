#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'set'
require 'tmpdir'
require_relative '../errata'

# Tiny assertion harness (the bundle has no rspec in this environment).
class Case
  attr_reader :failures

  def initialize
    @failures = []
  end

  def check(name)
    yield
  rescue StandardError => e
    @failures << "#{name}: #{e.class}: #{e.message}"
  end

  def assert_equal(expected, actual, label = nil)
    return if expected == actual

    raise "#{label} expected #{expected.inspect}, got #{actual.inspect}"
  end

  def assert(condition, message)
    raise message unless condition
  end

  def refute(condition, message)
    raise message if condition
  end
end

def section(title)
  puts "== #{title}"
end

tc = Case.new

section 'IntSeq stays symbolic for long runs'
def random_seq(rng)
  parts = []
  cursor = rng.rand(0..10)
  rng.rand(1..3).times do
    step = rng.rand(1..6)
    count = rng.rand(1..12)
    parts << [cursor, step, count]
    cursor += step * count + rng.rand(1..5)
  end
  Errata::IntSeq.new(parts)
end

def brute_set(seq)
  seq.parts.flat_map { |first, step, count| count.times.map { |k| first + k * step } }.to_set
end

tc.check('randomized AP arithmetic matches brute-force integer sets') do
  rng = Random.new(11)
  300.times do
    a = random_seq(rng)
    b = random_seq(rng)
    brute_a = brute_set(a)
    brute_b = brute_set(b)
    tc.assert_equal((brute_a | brute_b).sort, a.union(b).to_a, 'union')
    tc.assert_equal((brute_a - brute_b).sort, a.subtract(b).to_a, 'subtract')
    tc.assert_equal((brute_a & brute_b).sort, a.intersect(b).to_a, 'intersect')
    tc.assert_equal(brute_a.size, a.size, 'size')
    sample = rng.rand(-20..120)
    tc.assert_equal(brute_a.include?(sample), a.include?(sample), 'include?')
  end
end
tc.check('million-row size without expansion') do
  seq = Errata::IntSeq.seq(1_000_000, 7, 1_000_000)
  raise 'wrong size' unless seq.size == 1_000_000
  raise 'membership' unless seq.include?(1_000_000 + 999_999 * 7)
  raise 'non-member' if seq.include?(1_000_001)
end

tc.check('subtract removes an intersecting AP symbolically') do
  base = Errata::IntSeq.seq(0, 1, 10)
  removed = Errata::IntSeq.seq(2, 3, 3) # 2, 5, 8
  tc.assert_equal([0, 1, 3, 4, 6, 7, 9], base.subtract(removed).to_a)
  tc.assert_equal([2, 5, 8], base.intersect(removed).to_a)
end

tc.check('union keeps duplicates only when explicitly contributed twice') do
  left = Errata::IntSeq.from_a([1, 2, 3])
  right = Errata::IntSeq.from_a([3, 4])
  tc.assert_equal([1, 2, 3, 4], left.union(right).to_a)
  tc.assert_equal([1, 1, 2, 3, 3],
                  Errata::IntSeq.flatten_sort([left, Errata::IntSeq.from_a([1, 3])]))
end

section 'Tables and cyclic templates'
table = Errata::BaseTable.new(
  id: 'T', materials: Errata::IntSeq.seq(100, 1, 5),
  categories: [:甲, 0, :甲, 0, :乙]
)
tc.check('raw category and ticket lookup') do
  tc.assert_equal(:乙, table.raw_category_at(4))
  tc.assert_equal(104, table.material_at(4))
  tc.assert_equal([1, 3], table.raw_positions_for(0).to_a)
  tc.assert_equal([0, 2], table.raw_positions_for(:甲).to_a)
  tc.assert_equal(4, table.position_of_material(104))
end

tc.check('cyclic short template on a long table') do
  long = Errata::BaseTable.new(
    id: 'LONG', materials: Errata::IntSeq.seq(1_000_000, 7, 10_000),
    categories: [0, 1]
  )
  tc.assert_equal(1, long.raw_category_at(9999))
  tc.assert_equal(5000, long.raw_positions_for(0).size)
  tc.assert_equal(1_000_000 + 9999 * 7, long.material_at(9999))
end

section 'Batches apply in night order with last-batch-wins'
b1 = Errata::Batch.new(
  id: 'B1', cost: 5,
  targets: { 'T' => { materials: Errata::IntSeq.from_a([101, 103]),
                      positions: Errata::IntSeq.from_a([1, 3]),
                      new_category: :甲 } }
)
b2 = Errata::Batch.new(
  id: 'B2', cost: 9,
  targets: { 'T' => { materials: Errata::IntSeq.point(104),
                      positions: Errata::IntSeq.point(4),
                      new_category: 0 } }
)
tc.check('view without batches keeps the raw template') do
  view = Errata::TableView.new(table, [])
  tc.assert_equal(:乙, view.raw_category_at(4))
  tc.assert_equal([1, 3], view.positions_for(0).to_a)
end

tc.check('selected batches retarget display sets and keep provenance') do
  view = Errata::TableView.new(table, [b1, b2])
  tc.assert_equal(0, view.raw_category_at(4))
  tc.assert_equal('B2', view.winner_at(4))
  tc.assert_equal(:甲, view.raw_category_at(1))
  tc.assert_equal('B1', view.winner_at(1))
  tc.assert_equal(nil, view.winner_at(0))
  tc.refute(view.positions_for(:乙).include?(4), '乙 must lose row 4')
end

section 'Replay semantics'
def replay_on(categories, args, op, fallback = nil, extra_orders = [], signatures = [])
  materials = Errata::IntSeq.seq(0, 1, categories.size)
  base = Errata::BaseTable.new(id: 'T', materials: materials, categories: categories)
  view = Errata::TableView.new(base, [])
  orders = [{ 'name' => 'step', 'table' => 'T', 'op' => op,
              'args' => args }.tap { |step| step['fallback_at'] = fallback if fallback }] + extra_orders
  replay = Errata::Replay.new({ 'T' => view }, orders)
  begin
    replay.run
    [replay.outputs['step'], replay]
  rescue Errata::OldLibraryError => e
    [[:raised, e.class], replay]
  end
end

tc.check('numeric value is a category first, position second') do
  output, = replay_on([:甲, 0, :甲, 0, :乙], [0], 'pos')
  tc.assert_equal([1, 3], output.tickets.map { |ticket| ticket })
  output3, = replay_on([:甲, 0, :甲, 0, :乙], [3], 'pos')
  tc.assert_equal(3, output3.tickets.first)
end

tc.check('duplicate query values are never merged') do
  output, = replay_on([:甲, 0, :甲, 0, :乙], [:甲, :甲], 'pos')
  tc.assert_equal([0, 0, 2, 2], output.tickets)
end

tc.check('single-result subset trips the old-library branch and falls back to at') do
  output, = replay_on([:甲, 0, :甲, 0, :乙], [:乙], 'subset', [4])
  tc.assert_equal(:乙, output.value)
  tc.assert_equal([4], output.tickets)
  tc.assert_equal('old_library', output.trace[:branch])
end

tc.check('a position on one table becomes a number used on the next sub-table') do
  categories = [:甲, 0, :甲, 0, :乙]
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 5),
                               categories: categories)
  view = Errata::TableView.new(base, [])
  orders = [
    { 'name' => 'n', 'table' => 'T', 'op' => 'pos', 'args' => [2] },
    { 'name' => 'sub', 'table' => 'T', 'op' => 'subset', 'args' => [0, 2] },
    { 'name' => 'next', 'table' => 'sub', 'op' => 'pos', 'args' => [{ 'ref' => 'n' }] }
  ]
  replay = Errata::Replay.new({ 'T' => view }, orders).run
  tc.assert_equal(2, replay.outputs['n'].value)
  tc.assert_equal([0, :甲, 0], replay.outputs['sub'].value)
  tc.assert_equal(2, replay.outputs['next'].value)
  tc.assert_equal([3], replay.outputs['next'].tickets)
  tc.assert_equal(1, replay.outputs['next'].trace[:layer])
  tc.assert(replay.outputs['next'].trace[:interpretations].join.include?('used as row position'),
             'trace must record where the number became a row position')
end

section 'Cross-check against the real Daru::CategoricalIndex when loadable'
begin
  require 'daru/index/index'
  require 'daru/index/categorical_index'

  rng = Random.new(7)
  categories_pool = [:甲, :乙, 0, 1, 2]
  200.times do
    size = rng.rand(1..12)
    categories = Array.new(size) { categories_pool.sample(random: rng) }
    daru_index = Daru::CategoricalIndex.new(categories)
    base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, size),
                                 categories: categories)
    view = Errata::TableView.new(base, [])
    args = Array.new(rng.rand(1..3)) { categories_pool.sample(random: rng) }

    daru_pos = begin
      daru_index.pos(*args)
    rescue IndexError
      :index_error
    end
    mine_pos = begin
      replay = Errata::Replay.new({ 'T' => view }, []).run
      result = replay.send(:daru_pos, view, args, { interpretations: [] })
      result.is_a?(Integer) ? result : result.to_a
    rescue IndexError
      :index_error
    end
    tc.assert_equal(daru_pos, mine_pos, "pos #{args.inspect} on #{categories.inspect}")

    daru_subset = begin
      daru_index.subset(*args).to_a
    rescue NoMethodError, IndexError => e
      [:raised, e.class]
    end
    mine_subset = begin
      order = [{ 'name' => 'step', 'table' => 'T', 'op' => 'subset', 'args' => args }]
      replay = Errata::Replay.new({ 'T' => view }, order).run
      replay.outputs['step'].value
    rescue Errata::OldLibraryError
      [:raised, NoMethodError]
    rescue IndexError
      [:raised, IndexError]
    end
    tc.assert_equal(daru_subset, mine_subset, "subset #{args.inspect} on #{categories.inspect}")

    at_args = Array.new(rng.rand(1..3)) { rng.rand(-size...size) }
    daru_at = daru_index.at(*at_args)
    daru_at = daru_at.is_a?(Daru::CategoricalIndex) ? daru_at.to_a : daru_at
    order = [{ 'name' => 'step', 'table' => 'T', 'op' => 'at', 'args' => at_args }]
    mine = Errata::Replay.new({ 'T' => view }, order).run.outputs['step']
    mine_at = mine.scalar? ? mine.value : mine.value
    tc.assert_equal(daru_at, mine_at, "at #{at_args.inspect} on #{categories.inspect}")
  end
  puts 'real-daru cross-check: 200 randomized cases'
rescue LoadError
  puts 'real daru not loadable, skipped cross-check'
end

section 'at ranges and negative positions mirror Daru::Index'
tc.check('at with a range and negative index') do
  categories = [:甲, 0, :甲, 0, :乙]
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 5),
                               categories: categories)
  view = Errata::TableView.new(base, [])
  orders = [
    { 'name' => 'r', 'table' => 'T', 'op' => 'at', 'args' => [2..3] },
    { 'name' => 'neg', 'table' => 'T', 'op' => 'at', 'args' => [-1] },
    { 'name' => 'bad', 'table' => 'T', 'op' => 'at', 'args' => [9] }
  ]
  replay = Errata::Replay.new({ 'T' => view }, orders[0..1]).run
  tc.assert_equal([:甲, 0], replay.outputs['r'].value)
  tc.assert_equal(:乙, replay.outputs['neg'].value)
  begin
    Errata::Replay.new({ 'T' => view }, [orders[2]]).run
    raise 'expected IndexError'
  rescue IndexError
    :ok
  end
end

section 'Night order: later batch wins for the same material'
tc.check('the last batch touching a row decides the displayed category') do
  categories = [:甲, 0, :甲, 0, :乙]
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 5),
                               categories: categories)
  early = Errata::Batch.new(
    id: 'EARLY', cost: 1,
    targets: { 'T' => { materials: Errata::IntSeq.point(4),
                        positions: Errata::IntSeq.point(4), new_category: 0 } }
  )
  late = Errata::Batch.new(
    id: 'LATE', cost: 1,
    targets: { 'T' => { materials: Errata::IntSeq.point(4),
                        positions: Errata::IntSeq.point(4), new_category: :甲 } }
  )
  view = Errata::TableView.new(base, [early, late])
  tc.assert_equal(:甲, view.raw_category_at(4))
  tc.assert_equal('LATE', view.winner_at(4))
  reversed = Errata::TableView.new(base, [late, early])
  tc.assert_equal(0, reversed.raw_category_at(4))
  tc.assert_equal('EARLY', reversed.winner_at(4))
end

section 'Tie-break and any-match signatures'
tc.check('equal deferral cost prefers earlier approval numbers tonight') do
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 2),
                               categories: [:甲, :乙, 0])
  batch_a = Errata::Batch.new(
    id: 'A', cost: 4,
    targets: { 'T' => { materials: Errata::IntSeq.point(0),
                        positions: Errata::IntSeq.point(0), new_category: :丙 } }
  )
  batch_b = Errata::Batch.new(
    id: 'B', cost: 4,
    targets: { 'T' => { materials: Errata::IntSeq.point(1),
                        positions: Errata::IntSeq.point(1), new_category: :丁 } }
  )
  batches = [batch_a, batch_b]
  # Tonight needs a corrected category, obtainable only once A or B ships.
  need_correction = Errata::Signature.new(
    { 'id' => 'N', 'table' => 'T', 'tickets' => [0, 1],
      'allowed_results' => [':丙', ':丁'], 'match' => 'any' }, :tonight
  )
  # Legacy any-match needs at least one raw category left: fails only if both ship.
  keep_raw = Errata::Signature.new(
    { 'id' => 'L', 'table' => 'T', 'tickets' => [0, 1],
      'allowed_results' => [':甲', ':乙'], 'match' => 'any' }, :legacy
  )
  plan = Errata::Plan.new(tables: [base], batches: batches,
                          night: Errata::Night.new(batches),
                          signatures: [need_correction, keep_raw], work_orders: [])
  result = Errata::Solver.new(plan).solve
  tc.assert(result.feasible?, 'must be feasible')
  tc.assert_equal(4, result.deferral_cost)
  tc.assert_equal(['A'], result.selected)
  tc.assert_equal(['B'], result.deferred)
end

tc.check('any-match passes when the designated reading is obtainable') do
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 4),
                               categories: [:甲, 0, :乙, 0])
  view = Errata::TableView.new(base, [])
  slip = Errata::Signature.new({ 'id' => 'S', 'table' => 'T',
                                 'tickets' => [0, 1, 2],
                                 'allowed_results' => [':乙'], 'match' => 'any' }, :tonight)
  check = Errata::SignatureChecker.new({ 'T' => view }, nil, [slip], {}).check
  tc.assert_equal([], check.failures)
  strict = Errata::Signature.new({ 'id' => 'S2', 'table' => 'T',
                                   'tickets' => [0, 1, 2],
                                   'allowed_results' => [':乙'] }, :tonight)
  check = Errata::SignatureChecker.new({ 'T' => view }, nil, [strict], {}).check
  tc.assert_equal(%w[value_mismatch value_mismatch], check.failures.map(&:reason))
end

section 'Loader derives row positions from material arithmetic progressions'
tc.check('material AP on a long table maps to consecutive row positions') do
  Dir.mktmpdir(nil, '.gsb/tmp') do |dir|
    path = File.join(dir, 'plan.json')
    File.write(path, JSON.generate(
      'tables' => [{
        'id' => 'BIG',
        'materials' => { 'first' => 1_000_000, 'step' => 7, 'count' => 10_000 },
        'categories' => [0, 1]
      }],
      'batches' => [{
        'id' => 'B1', 'cost' => 2,
        'targets' => {
          'BIG' => {
            'materials' => { 'first' => 1_000_014, 'step' => 14, 'count' => 50 },
            'new_category' => 1
          }
        }
      }],
      'signatures' => { 'legacy' => [], 'tonight' => [] },
      'work_orders' => []
    ))
    plan = Errata::Loader.load(path)
    positions = plan.batches.first.positions_in('BIG')
    tc.assert_equal([2, 4, 6], positions.first(3))
    tc.assert_equal(50, positions.size)
    view = Errata::TableView.new(plan.tables.first, plan.batches)
    tc.assert_equal(1, view.raw_category_at(2))
  end
end

section 'Solver matches an exhaustive subset enumeration'
tc.check('random instances: optimal cost and approval-order tie') do
  rng = Random.new(23)
  60.times do
    size = rng.rand(3..6)
    base = Errata::BaseTable.new(
      id: 'T', materials: Errata::IntSeq.seq(0, 1, size),
      categories: Array.new(size) { [:甲, :乙, 0].sample(random: rng) }
    )
    batch_count = rng.rand(2..4)
    batches = batch_count.times.map do |k|
      target_pos = rng.rand(0...size)
      Errata::Batch.new(
        id: ('A'.ord + k).chr, cost: rng.rand(1..5),
        targets: { 'T' => { materials: Errata::IntSeq.point(target_pos),
                            positions: Errata::IntSeq.point(target_pos),
                            new_category: [:丙, :丁, 1].sample(random: rng) } }
      )
    end
    slips = rng.rand(0..2).times.map do |k|
      ticket = rng.rand(0...size)
      Errata::Signature.new(
        { 'id' => "S#{k}", 'table' => 'T', 'tickets' => [ticket],
          'allowed_results' => [[:甲, :乙, 0, :丙].sample(random: rng)],
          'match' => rng.rand(2).zero? ? 'all' : 'any' },
        k.zero? ? :legacy : :tonight
      )
    end
    plan = Errata::Plan.new(tables: [base], batches: batches,
                            night: Errata::Night.new(batches),
                            signatures: slips, work_orders: [])
    result = Errata::Solver.new(plan).solve

    all_subsets = (0..batch_count).flat_map { |k| (0...batch_count).to_a.combination(k).to_a }
    feasible_sets = all_subsets.select do |chosen_indexes|
      ids = chosen_indexes.map { |idx| batches[idx].id }
      views = { 'T' => Errata::TableView.new(base, batches.values_at(*chosen_indexes)) }
      checker = Errata::SignatureChecker.new(views, nil, slips, {})
      checker.check.ok?
    end
    if feasible_sets.empty?
      tc.refute(result.feasible?, 'exhaustive says infeasible')
      next
    end
    best_cost = feasible_sets.map { |indexes| batches.reject.with_index { |_, idx| indexes.include?(idx) }.sum(&:cost) }.min
    tc.assert_equal(best_cost, result.deferral_cost, 'optimal deferral cost')
    # approval-order tie winner = feasible set lexicographically preferring inclusion
    winner = feasible_sets.min_by do |indexes|
      chosen = indexes.to_set
      [batches.reject.with_index { |_, idx| chosen.include?(idx) }.sum(&:cost),
       batches.map { |batch| chosen.include?(batch.id) ? 0 : 1 }]
    end
    tc.assert_equal(winner.map { |idx| batches[idx].id }.sort, result.selected.sort, 'tie selection')
  end
end

section 'Signatures distinguish tickets even when displayed values coincide'
plan = Errata::Loader.load(File.expand_path('../examples/infeasible_plan.json', __dir__))
result = Errata::Solver.new(plan).solve
tc.check('infeasible plan returns both conflicting slips as the core') do
  tc.refute(result.feasible?, 'plan must be infeasible')
  tc.assert_equal(%w[L_KEEP_YI N_WANT_ZERO], result.unsat_core.map(&:id).sort)
end

plan = Errata::Loader.load(File.expand_path('../examples/feasible_plan.json', __dir__))
result = Errata::Solver.new(plan).solve
tc.check('feasible plan minimizes deferral cost and forces irrelevant batches tonight') do
  tc.assert(result.feasible?, result.check_result.failures.map(&:detail).inspect)
  tc.assert_equal(['B3'], result.selected)
  tc.assert_equal(14, result.deferral_cost)
  tc.assert_equal([], result.check_result.failures)
end

tc.check('a same-value ticket swap is rejected, not counted as success') do
  base = Errata::BaseTable.new(id: 'T', materials: Errata::IntSeq.seq(0, 1, 2),
                               categories: [:甲, :甲, :乙])
  view = Errata::TableView.new(base, [])
  orders = [{ 'name' => 'jia', 'table' => 'T', 'op' => 'pos', 'args' => [:甲] }]
  replay = Errata::Replay.new({ 'T' => view }, orders).run
  slip = Errata::Signature.new({ 'id' => 'S', 'output' => 'jia',
                                 'tickets' => [1, 0] }, :legacy)
  check = Errata::SignatureChecker.new({ 'T' => view }, replay, [slip], {}).check
  tc.assert_equal(['ticket_mismatch'], check.failures.map(&:reason))
end

if tc.failures.empty?
  puts 'ALL TESTS PASSED'
  exit 0
else
  puts "FAILURES (#{tc.failures.size}):"
  tc.failures.each { |failure| puts "  - #{failure}" }
  exit 1
end