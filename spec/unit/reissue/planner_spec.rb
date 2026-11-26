# frozen_string_literal: true

RSpec.describe Dry::Transformer::Reissue::Planner do
  subject(:planner) do
    described_class.new(
      orders: orders,
      ingredients: ingredients,
      procedures: procedures
    )
  end

  let(:orders) do
    [
      {order_no: 1, source_key: :k1, ingredients: [{stale: true}]},
      {order_no: 2, source_key: :k2},
      {order_no: 3, source_key: :k2},
      {order_no: 4, source_key: :k3}
    ]
  end

  let(:ingredients) do
    [
      {source_key: :k1, code: :A, name: "alpha"},
      {source_key: :k1, code: :B, name: "bravo"},
      {source_key: :k2, code: :A, name: "alpha"},
      {source_key: :k2, code: :B, name: "bravo"},
      {source_key: :k3, code: :C, name: "charlie"}
    ]
  end

  let(:procedures) do
    [
      {fingerprint: [:A, :B], body: "check AB", source_id: :src_ab},
      {fingerprint: [:B, :A], body: "check BA", source_id: :src_ba},
      {fingerprint: [:C], body: "check C", source_id: :src_c},
      {fingerprint: [], body: "check empty", source_id: :src_empty},
      {fingerprint: [:A, :B], body: "duplicate body", source_id: :src_ab_2},
      {fingerprint: [:B, :A], body: "duplicate body", source_id: :src_ba_2}
    ]
  end

  describe "two-pass combine assembly" do
    it "attaches ordered ingredients first and procedures by ordered fingerprint" do
      record = planner.record_for(1)

      expect(record[:ingredients].map { |row| row[:code] }).to eq([:A, :B])
      expect(record[:fingerprint]).to eq([:A, :B])
      expect(record[:procedures].map { |row| row[:source_id] })
        .to eq([:src_ab, :src_ab_2])
    end

    it "overwrites a same-named legacy list already present on the order" do
      record = planner.record_for(1)

      expect(record[:ingredients]).not_to include({stale: true})
      expect(record[:ingredients].size).to be(2)
    end

    it "treats differently ordered lists as different procedure keys" do
      ab = planner.record_for(2)
      planner.apply_batch(
        reorders: {k2: [:B, :A]}
      )
      ba = planner.record_for(2)

      expect(ab[:fingerprint]).to eq([:A, :B])
      expect(ba[:fingerprint]).to eq([:B, :A])
      expect(ba[:procedures].map { |row| row[:source_id] })
        .to eq([:src_ba, :src_ba_2])
    end

    it "re-numbers the occurrence sequence column on every materialized list" do
      planner.apply_batch(reorders: {k1: [:B, :A]})

      list = planner.ingredient_list_for(1)
      expect(list.map { |row| row[:seq] }).to eq([0, 1])
      expect(list.map { |row| row[:code] }).to eq([:B, :A])
    end

    it "binds an empty list to the procedure keyed by an empty fingerprint" do
      empty_planner = described_class.new(
        orders: [{order_no: 9, source_key: :empty}],
        ingredients: [],
        procedures: procedures
      )

      record = empty_planner.record_for(9)
      expect(record[:ingredients]).to eq([])
      expect(record[:procedures].map { |row| row[:source_id] }).to eq([:src_empty])
    end
  end

  describe "shared source keys" do
    it "attaches the same frozen list object to every order sharing a key" do
      order_two = planner.ingredient_list_for(2)
      order_three = planner.ingredient_list_for(3)

      expect(order_two).to eq(order_three)
      expect(order_two.equal?(order_three)).to be(true)
    end

    it "reissues every order number sharing a patched key as one contiguous block" do
      report = planner.apply_batch(reorders: {k2: [:B, :A]})

      expect(report.changed_numbers).to eq([2, 3])
      expect(report.ranges).to eq([2..3])
    end
  end

  describe "patch batches" do
    it "applies an insert, a withdrawal and a reordering atomically in one report" do
      report = planner.apply_batch(
        inserts: [{source_key: :k3, code: :D, name: "delta"}],
        withdraws: [{source_key: :k1, code: :A}],
        reorders: {k2: [:B, :A]}
      )

      expect(report.changed_numbers).to eq([1, 2, 3, 4])
      expect(report.ranges).to eq([1..4])

      expect(planner.diff_for(1)[:new_list].map { |row| row[:code] }).to eq([:B])
      expect(planner.diff_for(4)[:new_list].map { |row| row[:code] }).to eq([:C, :D])
    end

    it "reconfigures a subrange according to the list currently filled into orders" do
      report = planner.apply_batch(
        reorders: {k2: [:B, :A]}
      )

      expect(report.changed_numbers).to eq([2, 3])
      diff = planner.diff_for(2)
      expect(diff[:old_list].map { |row| row[:code] }).to eq([:A, :B])
      expect(diff[:new_list].map { |row| row[:code] }).to eq([:B, :A])
      expect(diff[:old_procedures].map { |row| row[:source_id] })
        .to eq([:src_ab, :src_ab_2])
      expect(diff[:new_procedures].map { |row| row[:source_id] })
        .to eq([:src_ba, :src_ba_2])
    end

    it "emits a provenance correction when identical procedure bodies hit different sources" do
      planner.apply_batch(reorders: {k1: [:B, :A]})
      diff = planner.diff_for(1)

      bodies_before = diff[:old_procedures].map { |row| row[:body] }
      bodies_after = diff[:new_procedures].map { |row| row[:body] }
      expect(bodies_before & bodies_after).to include("duplicate body")
      expect(diff[:old_procedures].map { |row| row[:source_id] })
        .to eq([:src_ab, :src_ab_2])
      expect(diff[:new_procedures].map { |row| row[:source_id] })
        .to eq([:src_ba, :src_ba_2])
      expect(diff[:changed]).to be(true)
    end

    it "reports only ranges that actually need resending" do
      report = planner.apply_batch(
        inserts: [{source_key: :k1, code: :X, name: "x-ray"}]
      )

      expect(report.changed_numbers).to eq([1])
      expect(report.ranges).to eq([1..1])
      expect(planner.changed_order_numbers).to eq([1])
      expect(planner.reissue_ranges).to eq([1..1])
    end

    it "raises when withdrawing a code that is not on the source key" do
      expect {
        planner.apply_batch(withdraws: [{source_key: :k3, code: :A}])
      }.to raise_error(Dry::Transformer::Reissue::Error, /missing codes/)
    end
  end

  describe "version history and shared combinations" do
    it "keeps previous generations readable while new lists resolve through them" do
      planner.apply_batch(reorders: {k1: [:B, :A]})

      expect(planner.record_for(1, generation: 0)[:fingerprint]).to eq([:A, :B])
      expect(planner.record_for(1, generation: 1)[:fingerprint]).to eq([:B, :A])
    end

    it "keeps sharing one frozen list when a new batch does not touch the key" do
      planner.apply_batch(reorders: {k1: [:B, :A]})

      expect(planner.ingredient_list_for(2, generation: 0))
        .equal?(planner.ingredient_list_for(2, generation: 1))
    end

    it "lets any number inside a reissue range fetch the actual old/new diff" do
      planner.apply_batch(reorders: {k2: [:B, :A]})

      [2, 3].each do |number|
        diff = planner.diff_for(number)
        expect(diff[:order_number]).to eq(number)
        expect(diff[:old_list].map { |row| row[:code] }).to eq([:A, :B])
        expect(diff[:new_list].map { |row| row[:code] }).to eq([:B, :A])
      end
    end
  end

  describe "uniform rule segments over order ranges" do
    let(:large_orders) do
      (1..large_size).map { |number| {order_no: number, source_key: "key-#{number}".to_sym} }
    end

    let(:large_ingredients) do
      (1..large_size).flat_map do |number|
        [
          {source_key: "key-#{number}".to_sym, code: :A},
          {source_key: "key-#{number}".to_sym, code: :B}
        ]
      end
    end

    let(:large_size) { 1000 }

    let(:large_planner) do
      described_class.new(
        orders: large_orders,
        ingredients: large_ingredients,
        procedures: procedures
      )
    end

    let(:swap_rule) do
      lambda do |parent_list, _source_key, rule_name|
        expect(rule_name).to eq(:swap_ab)
        parent_list.sort_by { |row| row[:code] == :A ? 1 : 0 }
      end
    end

    it "compresses a uniform segment into contiguous and equal-step ranges" do
      report = large_planner.apply_batch(
        rule: {
          name: :swap_ab,
          sequences: [(10..12), (20..30).step(2)],
          transform: swap_rule
        }
      )

      expect(report.ranges).to eq([10..12, (20..30).step(2)])
      expect(report.all_numbers).to eq([10, 11, 12, 20, 22, 24, 26, 28, 30])
      expect(report.cover?(22)).to be(true)
      expect(report.cover?(21)).to be(false)
    end

    it "materializes rule lists lazily and only for queried source keys" do
      large_planner.apply_batch(
        rule: {name: :swap_ab, sequences: [10..12], transform: swap_rule}
      )
      snapshot = large_planner.snapshots.last

      expect(snapshot.instance_variable_get(:@rule_memo)).to be_empty

      list = large_planner.ingredient_list_for(11)
      expect(list.map { |row| row[:code] }).to eq([:B, :A])
      expect(snapshot.instance_variable_get(:@rule_memo).keys).to eq([[:"key-11", 11]])
      expect(large_planner.ingredient_list_for(999).map { |row| row[:code] })
        .to eq([:A, :B])
    end

    it "streams subrange diffs without expanding the whole ledger" do
      large_planner.apply_batch(
        rule: {name: :swap_ab, sequences: [10..20], transform: swap_rule}
      )

      streamed = large_planner.each_diff((12..15)).to_a
      expect(streamed.map { |diff| diff[:order_number] }).to eq([12, 13, 14, 15])
      expect(streamed.map { |diff| diff[:rule] }).to all(eq(:swap_ab))
      expect(streamed.first[:new_list].map { |row| row[:code] }).to eq([:B, :A])
    end

    it "continues the same rule over the next segment in a later batch" do
      large_planner.apply_batch(
        rule: {name: :swap_ab, sequences: [10..12], transform: swap_rule}
      )
      continuation = large_planner.apply_batch(
        rule: {name: :swap_ab, sequences: [13..15], transform: swap_rule}
      )

      expect(continuation.generation).to be(2)
      expect(continuation.all_numbers).to eq([13, 14, 15])
      expect(large_planner.ingredient_list_for(11).map { |row| row[:code] })
        .to eq([:B, :A])
      expect(large_planner.ingredient_list_for(14).map { |row| row[:code] })
        .to eq([:B, :A])
    end

    it "covers only part of the orders sharing one source key without cross-talk" do
      shared = described_class.new(
        orders: [
          {order_no: 1, source_key: :shared},
          {order_no: 2, source_key: :shared},
          {order_no: 3, source_key: :shared}
        ],
        ingredients: [
          {source_key: :shared, code: :A},
          {source_key: :shared, code: :B}
        ],
        procedures: procedures
      )

      shared.apply_batch(rule: {name: :swap_ab, sequences: [2..2], transform: swap_rule})

      expect(shared.ingredient_list_for(1).map { |row| row[:code] }).to eq([:A, :B])
      expect(shared.ingredient_list_for(2).map { |row| row[:code] }).to eq([:B, :A])
      expect(shared.ingredient_list_for(3).map { |row| row[:code] }).to eq([:A, :B])
      expect(shared.reissue_ranges).to eq([2..2])
    end
  end

  describe "#verify!" do
    it "replays every batch and double-merges the whole ledger" do
      planner.apply_batch(
        inserts: [{source_key: :k3, code: :D, name: "delta"}],
        reorders: {k2: [:B, :A]}
      )
      planner.apply_batch(withdraws: [{source_key: :k1, code: :B}])

      expect(planner.verify!).to be(:verified)
    end
  end

  describe "#materialize" do
    it "attaches both levels for the whole order table" do
      table = planner.materialize

      expect(table.map { |row| row[:order_no] }).to eq([1, 2, 3, 4])
      expect(table.first[:procedures].map { |row| row[:source_id] })
        .to eq([:src_ab, :src_ab_2])
    end
  end

  describe "no-op patches" do
    it "excludes orders whose fingerprint and provenance do not actually change" do
      report = planner.apply_batch(reorders: {k1: [:A, :B]})

      expect(report.changed_numbers).to eq([])
      expect(report.ranges).to eq([])
      expect(planner.changed_order_numbers).to eq([])
    end
  end

  describe "registry integration" do
    it "exposes range compression as an array transformation function" do
      fn = Dry::Transformer::ArrayTransformations.t(:reissue_ranges)

      expect(fn.call([1, 2, 3, 7, 9, 11]))
        .to eq([1..3, (7..11).step(2)])
    end
  end
end
