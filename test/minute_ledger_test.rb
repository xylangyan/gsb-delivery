require "minitest/autorun"
require "active_record"
require "groupdate"

ENV["TZ"] = "America/New_York"

# The ledger is a SQLite-backed subsystem; skip under the other adapter tasks.
if ENV["ADAPTER"].nil? || ENV["ADAPTER"] == "sqlite"
class MinuteLedgerTest < Minitest::Test
  class LedgerBase < ActiveRecord::Base
    self.abstract_class = true
  end

  class LedgerEvent < LedgerBase
    self.table_name = "events"
  end

  def setup
    LedgerBase.establish_connection adapter: "sqlite3", database: ":memory:"
    @connection = LedgerBase.connection
    @connection.create_table :events do |t|
      t.datetime :ts
      t.integer :amount
    end

    @ledger = Groupdate::MinuteLedger.new(connection: @connection)
    @m1 = utc_minute(2025, 11, 2, 5, 0)  # NY label 2025-11-02 01:00, total 7
    @m2 = utc_minute(2025, 11, 2, 5, 1)  # NY label 2025-11-02 01:01, total 3
    @m3 = utc_minute(2025, 11, 2, 6, 0)  # NY label 2025-11-02 01:00, total 9
  end

  def seed_archive!
    LedgerEvent.create!(ts: Time.at(@m1 * 60).utc, amount: 7)
    LedgerEvent.create!(ts: Time.at(@m2 * 60).utc, amount: 3)
    LedgerEvent.create!(ts: Time.at(@m3 * 60).utc, amount: 9)
    @ledger.build_index!(LedgerEvent, :ts, :amount)
    @v0 = @ledger.seed_version!
  end

  def test_archive_index_uses_raw_group_by_minute_buckets
    seed_archive!
    raw = LedgerEvent.group_by_minute(:ts, series: false).sum(:amount)
    assert_equal({ @m1 => 7, @m2 => 3, @m3 => 9 },
                 raw.transform_keys { |key| key.utc.to_i / 60 })

    index = @connection.select_all("SELECT minute, total FROM gd_minute_index ORDER BY minute").to_a
    assert_equal [[@m1, 7], [@m2, 3], [@m3, 9]],
                 index.map { |row| [row["minute"].to_i, row["total"].to_i] }
  end

  def test_sparse_display_collapses_dst_label_and_keeps_last_total
    seed_archive!
    rows = @ledger.display(@v0)

    assert_equal [
      { key: "2025-11-02 01:00", label: "2025-11-02 01:00", position: 0, total: 9,
        contributed_minute: @m3, source_minute: @m3 },
      { key: "2025-11-02 01:01", label: "2025-11-02 01:01", position: 1, total: 3,
        contributed_minute: @m2, source_minute: @m2 }
    ], rows
  end

  def test_ticket_revoke_reorders_display_while_base_version_stays_queryable
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke }, note: "jia")

    rows_v1 = @ledger.display(v1)
    assert_equal ["2025-11-02 01:01", "2025-11-02 01:00"], rows_v1.map { |row| row[:label] }
    assert_equal [3, 9], rows_v1.map { |row| row[:total] }
    assert_equal [@m2, @m3], rows_v1.map { |row| row[:source_minute] }

    rows_v0 = @ledger.display(@v0)
    assert_equal ["2025-11-02 01:00", "2025-11-02 01:01"], rows_v0.map { |row| row[:label] }
    assert_equal [9, 3], rows_v0.map { |row| row[:total] }
  end

  def test_ticket_restoring_bucket_restores_order_but_keeps_last_total
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke }, note: "jia")
    v2 = @ledger.submit_ticket!(base_version_id: v1, changes: { @m1 => 5 }, note: "yi")

    rows_v2 = @ledger.display(v2)
    assert_equal ["2025-11-02 01:00", "2025-11-02 01:01"], rows_v2.map { |row| row[:label] }
    first = rows_v2.first
    assert_equal 9, first[:total]
    assert_equal @m3, first[:source_minute]

    rows_v1_reopened = @ledger.display(v1)
    assert_equal ["2025-11-02 01:01", "2025-11-02 01:00"],
                 rows_v1_reopened.map { |row| row[:label] }

    ticket = @ledger.ticket_entries(v2)
    assert_equal 1, ticket.size
    assert_equal 0, ticket.first["position"]
    assert_equal @m1, ticket.first["minute"]
    assert_equal 5, ticket.first["total"]
    assert_equal 0, ticket.first["revoked"]
  end

  def test_diff_lists_reordering_once_per_key_with_positions_totals_and_sources
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke })
    v2 = @ledger.submit_ticket!(base_version_id: v1, changes: { @m1 => 5 })

    changes = @ledger.diff(@v0, v1)
    labels = changes.map { |change| change[:label] }
    assert_equal ["2025-11-02 01:00", "2025-11-02 01:01"], labels
    assert_equal labels.uniq, labels

    first = changes.find { |change| change[:label] == "2025-11-02 01:00" }
    assert_equal 0, first[:left][:position]
    assert_equal 1, first[:right][:position]
    assert_equal 9, first[:left][:total]
    assert_equal 9, first[:right][:total]
    assert_equal @m3, first[:left][:source_minute]
    assert_equal @m3, first[:right][:source_minute]

    second = changes.find { |change| change[:label] == "2025-11-02 01:01" }
    assert_equal 1, second[:left][:position]
    assert_equal 0, second[:right][:position]

    restored = @ledger.diff(v1, v2).map { |change| change[:label] }.sort
    assert_equal ["2025-11-02 01:00", "2025-11-02 01:01"], restored
  end

  def test_diff_flags_source_change_even_when_displayed_total_matches
    build_index_from([[@m1, 4], [@m3, 4]])
    base = @ledger.seed_version!
    revised = @ledger.submit_ticket!(base_version_id: base, changes: { @m3 => :revoke })

    changes = @ledger.diff(base, revised)
    assert_equal 1, changes.size
    change = changes.first
    assert_equal Groupdate::MinuteLedger.label_for(@m1), change[:label]
    assert_equal 4, change[:left][:total]
    assert_equal 4, change[:right][:total]
    assert_equal @m3, change[:left][:source_minute]
    assert_equal @m1, change[:right][:source_minute]
  end

  def test_real_zero_total_counts_as_data_in_sparse_and_fill
    zero_minute = utc_minute(2025, 11, 4, 10, 0)
    later_minute = utc_minute(2025, 11, 4, 10, 2)
    build_index_from([[zero_minute, 0], [later_minute, 8]])
    version = @ledger.seed_version!

    sparse = @ledger.display(version)
    assert_equal [0, 8], sparse.map { |row| row[:total] }
    assert_equal [zero_minute, later_minute], sparse.map { |row| row[:source_minute] }

    filled = @ledger.display(version, fill: true)
    gap = filled.find { |row| row[:position] == 1 }
    assert_equal 0, gap[:total]
    assert_equal zero_minute, gap[:source_minute]
  end

  def test_filled_display_uses_version_span_and_carries_forward
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke })

    filled_v0 = all_rows(@v0, fill: true)
    assert_equal @m3 - @m1 + 1, filled_v0.size
    gap = filled_v0.find { |row| row[:position] == 2 }
    assert_equal 3, gap[:total]
    assert_equal @m2, gap[:source_minute]
    assert_equal @m2, gap[:contributed_minute]
    far_gap = filled_v0.find { |row| row[:position] == 59 }
    assert_equal 3, far_gap[:total]
    assert_equal @m2, far_gap[:source_minute]
    last = filled_v0.last
    assert_equal 9, last[:total]
    assert_equal @m3, last[:source_minute]

    filled_v1 = all_rows(v1, fill: true)
    assert_equal @m3 - @m2 + 1, filled_v1.size
    assert_equal @m2, filled_v1.first[:source_minute]
  end

  def test_half_open_window_is_capped_at_16_rows
    seed_archive!
    page = @ledger.display(@v0, fill: true, window: 16...32)
    assert_equal 16, page.size
    assert_equal (16...32).to_a, page.map { |row| row[:position] }

    tail = @ledger.display(@v0, fill: true, window: 56...72)
    assert_equal 5, tail.size
    assert_equal (56...61).to_a, tail.map { |row| row[:position] }

    assert_equal [], @ledger.display(@v0, fill: true, window: 100...116)

    assert_raises(ArgumentError) { @ledger.display(@v0, window: 0..16) }
    assert_raises(ArgumentError) { @ledger.display(@v0, window: 0...17) }
    assert_raises(ArgumentError) { @ledger.display(@v0, window: -1...15) }
  end

  def test_fill_diff_marks_absent_side_nil
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke })
    changes = @ledger.diff(@v0, v1, fill: true, window: 0...16)

    removed = changes.find { |change| change[:key] == @m1 }
    refute_nil removed
    refute_nil removed[:left]
    assert_nil removed[:right]
  end

  def test_inspection_budget_for_ticket_display_and_diff
    seed_archive!
    minutes = (1..8).map { |offset| [utc_minute(2025, 12, 1, 0, 0) + offset, offset] }.to_h

    @ledger.reset_meter!
    @ledger.submit_ticket!(base_version_id: @v0, changes: minutes)
    assert_equal 8, @ledger.inspected_count
    assert @ledger.within_budget?

    @ledger.reset_meter!
    @ledger.display(@v0, fill: true, window: 0...16)
    assert_equal 16, @ledger.inspected_count

    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke })
    @ledger.reset_meter!
    @ledger.diff(@v0, v1)
    assert_equal 4, @ledger.inspected_count
    assert @ledger.within_budget?
  end

  def test_cached_single_item_lookups_are_still_metered
    seed_archive!
    minute = utc_minute(2025, 12, 10, 9, 0)
    @ledger.reset_meter!
    @ledger.submit_ticket!(base_version_id: @v0, changes: { minute => 11 })
    first = @ledger.inspected_count
    v1 = @ledger.versions.last["id"]
    @ledger.submit_ticket!(base_version_id: v1, changes: { minute => 12 })
    assert_equal first * 2, @ledger.inspected_count
  end

  def test_replay_and_audit_match_windowed_sql_for_every_version
    seed_archive!
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 => :revoke })
    v2 = @ledger.submit_ticket!(base_version_id: v1, changes: { @m1 => 5 })

    [@v0, v1, v2].each do |version|
      [false, true].each do |fill|
        assert @ledger.audit!(version, fill: fill)
        full = @ledger.replay(version, fill: fill)
        pages = full.each_slice(Groupdate::MinuteLedger::MAX_WINDOW_ROWS).with_index.map do |_, index|
          start_row = index * Groupdate::MinuteLedger::MAX_WINDOW_ROWS
          @ledger.display(version, fill: fill, window: start_row...(start_row + Groupdate::MinuteLedger::MAX_WINDOW_ROWS))
        end.flatten
        assert_equal full, pages
      end
    end
  end

  def test_large_archive_indexes_once_and_keeps_windowed_reads_bounded
    minutes = 2_000.times.map { |offset| [utc_minute(2026, 1, 1, 0, 0) + offset, offset % 7] }
    build_index_from(minutes)
    assert_raises(Groupdate::MinuteLedger::Error) { @ledger.build_index!(LedgerEvent, :ts, :amount) }

    version = @ledger.seed_version!
    assert_raises(Groupdate::MinuteLedger::Error) { @ledger.seed_version! }
    assert @ledger.audit!(version, fill: false)
    assert @ledger.audit!(version, fill: true)

    @ledger.reset_meter!
    page = @ledger.display(version, fill: true, window: 0...16)
    assert_equal 16, page.size
    assert_equal 16, @ledger.inspected_count
    assert @ledger.within_budget?
  end

  def test_ticket_validation
    seed_archive!
    nine = (1..9).map { |offset| [@m1 + offset, offset] }.to_h
    assert_raises(ArgumentError) { @ledger.submit_ticket!(base_version_id: @v0, changes: nine) }
    assert_raises(ArgumentError) { @ledger.submit_ticket!(base_version_id: @v0, changes: {}) }
    assert_raises(ArgumentError) { @ledger.submit_ticket!(base_version_id: @v0, changes: { @m2 => 1.5 }) }
    assert_raises(Groupdate::MinuteLedger::Error) do
      @ledger.submit_ticket!(base_version_id: @v0, changes: { @m1 + 400 => :revoke })
    end
    assert_raises(Groupdate::MinuteLedger::Error) do
      @ledger.submit_ticket!(base_version_id: 999, changes: { @m1 => 1 })
    end
  end

  def test_accepts_time_objects_as_minutes
    seed_archive!
    time = Time.at(@m1 * 60).utc
    v1 = @ledger.submit_ticket!(base_version_id: @v0, changes: { time => :revoke })
    assert_equal ["2025-11-02 01:01", "2025-11-02 01:00"],
                 @ledger.display(v1).map { |row| row[:label] }
  end

  private

  def all_rows(version, fill:)
    rows = []
    start_row = 0
    loop do
      page = @ledger.display(version, fill: fill, window: start_row...(start_row + Groupdate::MinuteLedger::MAX_WINDOW_ROWS))
      break if page.empty?
      rows.concat(page)
      start_row += Groupdate::MinuteLedger::MAX_WINDOW_ROWS
    end
    rows
  end

  def build_index_from(pairs)
    LedgerEvent.delete_all
    pairs.each do |minute, total|
      LedgerEvent.create!(ts: Time.at(minute * 60).utc, amount: total)
    end
    @ledger.build_index!(LedgerEvent, :ts, :amount)
  end

  def utc_minute(year, month, day, hour, minute)
    Time.utc(year, month, day, hour, minute).to_i / 60
  end
end
end
