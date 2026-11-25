require_relative "../reconcile/shift_reconciliation"
require_relative "../reconcile/small_log"

RSpec.describe ShiftReconciliation do
  let(:engine) { ShiftReconciliation::SmallLog.build.prepare }

  it "generates the expected sparse shift archive" do
    expect(engine.shifts.size).to eq(30)
    expect(engine.shifts.group_by(&:schedule_id).transform_values(&:size)).to include(
      "DAY" => 2,
      "SIM-A" => 1,
      "SIM-B" => 1,
      "RULE-DUP" => 1,
      "CANCEL" => 1,
      "RUN" => 20
    )
  end

  it "keeps same-second starts on separate schedules" do
    times = %w[SIM-A#1 SIM-B#1].map { |id| engine.shift(id).time }
    expect(times.uniq.size).to eq(1)
    expect(engine.shift("SIM-A#1").schedule_id).to eq("SIM-A")
    expect(engine.shift("SIM-B#1").schedule_id).to eq("SIM-B")
  end

  it "merges only same receipt numbers" do
    expect(engine.duplicate_receipt_count).to eq(1)
    expect(engine.receipts.map(&:id).count("R-DUP")).to eq(1)
    expect(engine.receipts.count { |receipt| receipt.time == ShiftReconciliation::TimeIO.parse("2026-11-19T09:00:05Z") }).to eq(2)
  end

  it "maximizes full-archive receipts and identifies the unavoidable missing shift" do
    expect(engine.hard_pairs.size).to eq(1)
    expect(engine.free_max_count + engine.hard_pairs.size).to eq(28)

    report = engine.full_report
    expect(report[:total_shifts]).to eq(30)
    expect(report[:fewest_missing]).to eq(2)
    expect(report[:most_missing]).to eq(2)
    expect(engine.earliest_unavoidable_missing.id).to eq("RUN#8")
    expect(engine.unavoidable_missing.map(&:id)).to eq(["RUN#8"])
    expect(engine.verify_pairs(report[:fewest_missing_pairs])).to be_empty
  end

  it "uses the AM-only receipt for AM after full-archive extension" do
    report = engine.window_report("2026-11-18T08:55:00Z", "2026-11-18T09:05:00Z")

    expect(report[:total_shifts]).to eq(1)
    expect(report[:fewest_missing]).to eq(0)
    expect(report[:most_missing]).to eq(0)

    [report[:fewest_missing_pairs], report[:most_missing_pairs]].each do |pairs|
      am_pair = pairs.find { |pair| pair.shift_id == "DAY#1" }
      pm_pair = pairs.find { |pair| pair.shift_id == "DAY#2" }
      expect(am_pair.receipt_id).to eq("R-AM-ONLY")
      expect(pm_pair.receipt_id).to eq("R-BOTH")
      expect(engine.verify_pairs(pairs)).to be_empty
    end
  end

  it "returns both traceable extremes when the same receipt can choose a shift" do
    report = engine.window_report("2026-11-18T17:55:00Z", "2026-11-18T18:05:00Z")

    expect(report[:fewest_missing]).to eq(0)
    expect(report[:most_missing]).to eq(1)

    minimum_pair = report[:fewest_missing_pairs].find { |pair| pair.receipt_id == "R-SWAP" }
    maximum_pair = report[:most_missing_pairs].find { |pair| pair.receipt_id == "R-SWAP" }
    expect(minimum_pair.shift_id).to eq("SWAP#1")
    expect(maximum_pair.shift_id).to eq("SWAP#2")

    expect(report[:fewest_missing_pairs].size).to eq(28)
    expect(report[:most_missing_pairs].size).to eq(28)
    expect(engine.verify_pairs(report[:fewest_missing_pairs])).to be_empty
    expect(engine.verify_pairs(report[:most_missing_pairs])).to be_empty
  end

  it "compresses segmented receipt pairings into original ranges" do
    report = engine.full_report
    groups = engine.compact_pairs(report[:fewest_missing_pairs])
    run_groups = groups.select { |group| group[:segment_key] == "RUN-SEGMENT" }

    expect(run_groups.map { |group| [group[:first_segment_index], group[:last_segment_index], group[:count]] })
      .to contain_exactly([0, 6, 7], [8, 19, 12])
  end

  it "loads compressed JSON archives" do
    archive = ShiftReconciliation::Archive.from_hash(
      "horizon" => {
        "start" => "2026-12-01T00:00:00Z",
        "end" => "2026-12-01T00:29:59Z"
      },
      "schedules" => [
        {
          "id" => "RUN",
          "hash" => {
            "start_time" => "2026-12-01T00:00:00Z",
            "rrules" => [
              {"rule_type" => "IceCube::MinutelyRule", "interval" => 10, "count" => 3}
            ]
          }
        }
      ],
      "receipts" => [
        {
          "key" => "SEG",
          "id_prefix" => "R-",
          "start" => "2026-12-01T00:00:00Z",
          "step" => 600,
          "count" => 3,
          "gaps" => [1],
          "schedule" => "RUN",
          "tolerance" => 30
        }
      ]
    )

    json_engine = archive.prepare
    report = json_engine.full_report
    expect(json_engine.shifts.size).to eq(3)
    expect(json_engine.receipts.size).to eq(2)
    expect(report[:fewest_missing]).to eq(1)
    expect(json_engine.earliest_unavoidable_missing.id).to eq("RUN#2")
  end

  it "matches extrema from all maximum matchings in small random graphs" do
    30.times do |seed|
      srand(seed)
      left_count = rand(0..5)
      right_count = rand(0..5)
      adjacency = Array.new(left_count) { (0...right_count).select { rand < 0.4 } }
      maximum = ShiftReconciliation::HopcroftKarp.new(adjacency, right_count).tap(&:maximize).matched_count

      all_matchings = [{}]
      adjacency.each_with_index do |rights, left|
        additions = []
        all_matchings.each do |matching|
          rights.each do |right|
            next if matching.value?(right)

            additions << matching.merge(left => right)
          end
        end
        all_matchings.concat(additions)
      end
      maximum_matchings = all_matchings.select { |matching| matching.size == maximum }

      (1 << left_count).times do |mask|
        preferred = (0...left_count).select { |left| mask[left] == 1 }
        outside = (0...left_count).to_a - preferred
        preferred_rank = ShiftReconciliation::HopcroftKarp.new(adjacency, right_count).maximize(preferred)
        outside_rank = ShiftReconciliation::HopcroftKarp.new(adjacency, right_count).maximize(outside)
        counts = maximum_matchings.map { |matching| matching.count { |left, _| preferred.include?(left) } }

        expect(preferred_rank).to eq(counts.max || 0)
        expect(maximum - outside_rank).to eq(counts.min || 0)
      end
    end
  end
end
